"""
repair.py — Category assignment + NULL/empty-field repair.

Shared by three callers:
  - ingest.py                        (category for freshly parsed articles)
  - scripts/backfill_categories.py   (one-time migration over existing rows)
  - main.py                          (startup backfill + 2h repair loop)

Rules:
  - Categories come from (in order): the article's stored LLM parse result
    (llm_answer JSON), a LOCAL keyword analysis of the stored article text
    (title/description/content/source), and only as a FINAL fallback one
    LLMPing request. Categories are never invented: the local classifier
    only assigns from the application's existing category vocabulary.
  - repair_articles() is idempotent: it only looks at rows returned by
    list_incomplete_articles(), never overwrites a valid category, and a
    failed repair is simply retried on the next pass.
  - The repair is lightweight by design: it does NOT mass-parse historical
    rows through the LLM. llm_* columns are filled only when the LLM
    fallback runs (the parse result is stored then, so nothing is wasted).
    New articles get their llm_* + category from normal ingestion.
  - description/content/source/author/image_url are NOT fabricated when
    absent — there is no source data to fill them from.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from config import Config
from database import (
    list_incomplete_articles,
    save_llm_parse,
    update_article_fields,
)
from llmping import build_article_prompt
from llmping import chat as llmping_chat
from models import Article

logger = logging.getLogger(__name__)

CATEGORY_MARKER_KEY = "category_backfill_done"

# A category is 1-3 short words. Anything longer/multi-line from the LLM is
# treated as unusable rather than stored.
_CATEGORY_WORD_RE = re.compile(r"^[a-z0-9][a-z0-9 '&/-]{0,39}$")


def _clean_category(text: Optional[str]) -> Optional[str]:
    """Normalize a candidate category; return None when it is not usable."""
    if not text or not isinstance(text, str):
        return None
    cat = " ".join(text.split()).strip().strip(".").lower()
    if not cat or len(cat) > 40 or " " in cat and cat.count(" ") > 2:
        return None
    return cat if _CATEGORY_WORD_RE.match(cat) else None


def extract_category(llm_answer: Optional[str]) -> Optional[str]:
    """
    Pull the category out of a stored LLMPing parse result.

    The parse prompt contract is a JSON object with a "category" field; the
    answer may still be fenced or malformed, so any parse failure just
    yields None (the repair pass will fall back to local analysis).
    """
    if not llm_answer:
        return None
    try:
        data = json.loads(llm_answer)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return _clean_category(data.get("category"))


def fallback_category(article: Article) -> Optional[str]:
    """
    Deterministic fallback: the provider's own category, normalized.

    Returns None when the provider did not supply one — no keyword guessing,
    no invented categories.
    """
    return _clean_category(article.category)


# ---------------------------------------------------------------------------
# Local category analysis — stored data only, existing vocabulary only
# ---------------------------------------------------------------------------

# Fixed keyword map over the categories this application already uses
# (the NewsData.io provider taxonomy plus values already present in the
# news table). The classifier NEVER invents names outside this set.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sports": (
        "sport", "sports", "nba", "nfl", "mlb", "nhl", "football", "soccer",
        "cricket", "tennis", "baseball", "hockey", "golf", "rugby", "boxing",
        "ufc", "formula 1", "olympics", "olympic", "paralympics", "fifa",
        "uefa", "ncaa", "playoffs", "playoff", "championship", "tournament",
        "league", "athlete", "stadium", "world cup", "goal", "coach",
    ),
    "technology": (
        "tech", "technology", "ai", "artificial intelligence", "software",
        "hardware", "app", "smartphone", "iphone", "android", "chip",
        "semiconductor", "cybersecurity", "hacker", "data breach", "startup",
        "google", "apple", "microsoft", "openai", "chatbot", "robot",
        "robotics", "internet", "cloud computing", "saas", "gadget",
        "quantum computing", "computer", "laptop", "social media",
    ),
    "business": (
        "business", "economy", "economic", "market", "markets", "stock",
        "stocks", "shares", "earnings", "revenue", "profit", "merger",
        "acquisition", "ipo", "layoffs", "ceo", "trade", "tariff",
        "tariffs", "inflation", "gdp", "recession", "wall street", "nasdaq",
        "company", "corporate", "retail", "consumer", "jobs", "unemployment",
        "real estate", "mortgage", "housing", "banking", "bank",
        "investment", "investor", "venture capital",
    ),
    "politics": (
        "politics", "political", "election", "elections", "senate",
        "congress", "parliament", "president", "presidential",
        "prime minister", "government", "policy", "legislation", "bill",
        "vote", "voting", "voter", "campaign", "democrats", "republicans",
        "governor", "impeachment", "sanctions", "referendum", "minister",
    ),
    "world": (
        "war", "ukraine", "gaza", "israel", "russia", "china", "united nations",
        "summit", "ceasefire", "military", "troops", "refugee", "refugees",
        "humanitarian", "conflict", "tensions", "embassy", "treaty", "nato",
    ),
    "health": (
        "health", "healthcare", "medical", "medicine", "hospital", "doctor",
        "patient", "disease", "virus", "vaccine", "vaccines", "cancer",
        "mental health", "fda", "clinical trial", "drug", "outbreak",
        "epidemic", "pandemic", "pharma", "pharmaceuticals", "therapy",
    ),
    "entertainment": (
        "entertainment", "movie", "movies", "film", "cinema", "hollywood",
        "celebrity", "celebrities", "music", "album", "concert", "actor",
        "actress", "singer", "netflix", "disney", "reality tv", "box office",
        "oscar", "oscars", "grammy", "emmy", "trailer", "gaming", "video game",
        "esports", "comics", "comic", "anime", "manga", "streaming",
    ),
    "science": (
        "science", "scientific", "research", "researchers", "study finds",
        "nasa", "space", "mars", "moon", "rocket", "satellite", "telescope",
        "physics", "biology", "chemistry", "genome", "dna", "discovery",
        "experiment", "archaeology", "fossil", "dinosaur", "species",
    ),
    "environment": (
        "environment", "environmental", "climate", "climate change",
        "global warming", "emissions", "carbon", "renewable", "solar",
        "pollution", "wildfire", "wildfires", "drought", "flood", "flooding",
        "conservation", "biodiversity", "extinction", "recycling",
        "sustainability", "national park", "oil spill", "deforestation",
    ),
    "energy": ("energy", "power grid", "electricity", "nuclear", "power plant"),
    "food": (
        "food", "recipe", "restaurant", "restaurants", "cooking", "chef",
        "cuisine", "dining", "grocery", "coffee", "wine", "brewery",
    ),
    "tourism": (
        "tourism", "travel", "traveler", "airline", "airlines", "flight",
        "flights", "hotel", "hotels", "vacation", "destination", "airport",
        "passport", "cruise", "tourist",
    ),
    "finance": (
        "finance", "financial", "crypto", "cryptocurrency", "bitcoin",
        "ethereum", "blockchain", "fintech", "digital currency", "defi",
    ),
}

# A few known publisher names carry a strong category signal on their own.
SOURCE_HINTS: dict[str, str] = {
    "sportingnews": "sports",
    "bleedingcool": "comics",
    "techcrunch": "technology",
    "theverge": "technology",
    "wired": "technology",
    "politico": "politics",
    "bloomberg": "business",
    "ehow": "health",
}

# Weighted scoring: title hits count most, then description/source, then
# content body hits. A category wins only with a clear margin — otherwise
# the classifier yields None and the row goes to the LLM fallback.
_TITLE_WEIGHT = 3
_DESC_WEIGHT = 2
_SOURCE_WEIGHT = 2
_CONTENT_WEIGHT = 1
_MIN_TOP_SCORE = 3
_MIN_MARGIN = 2

_KEYWORD_RES: dict[str, list[re.Pattern]] = {
    cat: [re.compile(r"\b" + re.escape(kw) + r"\b") for kw in kws]
    for cat, kws in CATEGORY_KEYWORDS.items()
}


def _count_hits(patterns: list[re.Pattern], text: str, weight: int) -> int:
    if not text:
        return 0
    total = 0
    for p in patterns:
        total += len(p.findall(text)) * weight
    return total


def classify_from_article_data(article: Article) -> Optional[str]:
    """
    Deterministic local category assignment from stored article data.

    Scores the existing category vocabulary against title, description,
    content and source. Returns the best category only when one clearly
    dominates; otherwise None (the caller may then use the LLM fallback).
    Never invents a category name outside CATEGORY_KEYWORDS.
    """
    title = (article.title or "").lower()
    desc = (article.description or "").lower()
    content = (article.content or "").lower()
    source = (article.source or "").lower()

    source_hint: Optional[str] = None
    for hint, cat in SOURCE_HINTS.items():
        if hint in source:
            source_hint = cat
            break

    scores: dict[str, int] = {}
    for cat, patterns in _KEYWORD_RES.items():
        score = (
            _count_hits(patterns, title, _TITLE_WEIGHT)
            + _count_hits(patterns, desc, _DESC_WEIGHT)
            + _count_hits(patterns, content, _CONTENT_WEIGHT)
            + _count_hits(patterns, source, _SOURCE_WEIGHT)
        )
        if score:
            scores[cat] = score

    if source_hint:
        scores[source_hint] = scores.get(source_hint, 0) + _SOURCE_WEIGHT * 2

    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_cat, top_score = ranked[0]
    if top_score < _MIN_TOP_SCORE:
        return None
    if len(ranked) > 1 and top_score - ranked[1][1] < _MIN_MARGIN:
        return None
    return top_cat


_CATEGORY_QUERY = (
    "Determine the single most fitting news category for this article. "
    "Reply with ONLY the category — lowercase, at most 2 words, no "
    "punctuation, no explanation.\n\n"
    "TITLE: {title}\n\n"
    "DESCRIPTION: {description}\n\n"
    "CONTENT: {content}"
)


def _build_category_query(cfg: Config, article: Article) -> str:
    """Build the compact category prompt from existing article fields only."""
    content = (article.content or article.description or "")[: cfg.llm_max_content_chars]
    return _CATEGORY_QUERY.format(
        title=article.title,
        description=article.description or "n/a",
        content=content or "n/a",
    )


async def fetch_category(cfg: Config, article: Article) -> Optional[str]:
    """
    Ask LLMPing for a category (final fallback only). One call, free-text
    answer, cleaned; a failure (or a rambling answer) returns None so the
    row is simply retried on the next repair pass.
    """
    res = await llmping_chat(cfg, query=_build_category_query(cfg, article))
    return _clean_category(res.answer)


@dataclass
class RepairResult:
    """Summary of one repair pass over the incomplete rows."""

    checked: int = 0             # incomplete rows inspected
    repaired: int = 0            # rows that got their category filled
    from_llm_answer: int = 0     # category extracted from stored llm_answer
    from_local: int = 0          # category from local stored-data analysis
    llm_fallback_used: int = 0   # rows that needed the final LLM fallback
    llm_called: int = 0          # LLM requests actually made this pass
    left_alone: int = 0          # rows with a valid category (llm data only)
    failed: int = 0              # rows whose repair errored (retried next pass)
    still_incomplete: int = 0    # rows left without a category after the pass

    def __str__(self) -> str:  # log-friendly
        return (
            f"checked={self.checked} repaired={self.repaired} "
            f"(llm_answer={self.from_llm_answer} local={self.from_local} "
            f"llm_fallback={self.llm_fallback_used}) left_alone={self.left_alone} "
            f"still_incomplete={self.still_incomplete} failed={self.failed} "
            f"llm_called={self.llm_called}"
        )


async def repair_articles(cfg: Config, conn: Any) -> RepairResult:
    """
    One idempotent, lightweight pass over every incomplete row.

    For each row with a missing/invalid category, in this order:
      1. extract from the stored llm_answer JSON        (no LLM request)
      2. local analysis of stored title/description/
         content/source against the existing vocabulary (no LLM request)
      3. FINAL fallback: one LLMPing request            (also stores llm_*)
    Rows that already have a valid category are left untouched even when
    their llm_* columns are missing — historical rows are never mass-parsed.
    Failures are logged, counted, and retried on the next pass.
    """
    result = RepairResult()
    rows = await asyncio.to_thread(list_incomplete_articles, conn)
    result.checked = len(rows)
    if rows:
        logger.info("[repair] %d incomplete row(s) found", len(rows))

    for idx, article in enumerate(rows, 1):
        category_valid = bool(article.category)
        if category_valid:
            # Present only because llm_* data is missing — the lightweight
            # repair leaves these alone (no LLM request, no category write).
            result.left_alone += 1
            continue

        try:
            # 1. Stored LLM parse result.
            category = extract_category(article.llm_answer)
            if category:
                result.from_llm_answer += 1
            else:
                # 2. Local analysis of the stored article data.
                category = classify_from_article_data(article)
                if category:
                    result.from_local += 1

            # 3. Final fallback — one LLM request. Rows that already store a
            #    complete LLM result get only the small category query (their
            #    llm_answer is never overwritten); rows without any LLM data
            #    get a full parse so llm_* and the category are filled in one
            #    request.
            if category is None:
                has_full_llm = bool(
                    article.llm_answer
                    and article.llm_provider
                    and article.llm_model
                    and article.llm_processed_at
                )
                if has_full_llm:
                    logger.info(
                        "[repair] (%d/%d) LLM category query for %s",
                        idx, len(rows), article.url,
                    )
                    category = await fetch_category(cfg, article)
                    result.llm_called += 1
                else:
                    logger.info(
                        "[repair] (%d/%d) LLM fallback parse for %s",
                        idx, len(rows), article.url,
                    )
                    res = await llmping_chat(
                        cfg, query=build_article_prompt(cfg, article)
                    )
                    await asyncio.to_thread(
                        save_llm_parse,
                        conn,
                        article.id,
                        res.answer,
                        res.provider,
                        res.model,
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                    result.llm_called += 1
                    category = extract_category(res.answer)
                    if category is None:
                        category = await fetch_category(cfg, article)
                result.llm_fallback_used += 1

            if category:
                await asyncio.to_thread(
                    update_article_fields,
                    conn,
                    article.id,
                    {"category": category},
                )
                result.repaired += 1
                logger.info(
                    "[repair] (%d/%d) %s → %r", idx, len(rows), article.url, category
                )
            else:
                result.still_incomplete += 1
        except Exception as exc:
            result.failed += 1
            logger.warning("[repair] failed for %s: %s — will retry next pass",
                           article.url, exc)

    logger.info("[repair] pass done: %s", result)
    return result
