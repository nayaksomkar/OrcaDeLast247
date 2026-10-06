"""
models.py — Canonical data shapes for the Last247 service.

Article  : the record stored in the `news` table and returned by the API.
IngestionResult : summary of one ingestion run (returned by /api/stats and POST /api/ingest).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Article — API response shape (Pydantic for automatic JSON serialization)
# ---------------------------------------------------------------------------

class Article(BaseModel):
    """
    Normalized news record.

    Matches the `news` table columns 1-to-1. Fields with default="" are
    optional in the source data; they appear as empty string in DB and are
    omitted from JSON responses (exclude_unset / response_model_exclude_none).

    id          : 16-char hex — first 16 chars of SHA-256(url). Deterministic,
                  so the same URL always maps to the same primary key.
    title / url : always present, never empty (articles lacking either are
                  dropped during ingestion before they reach the DB).
    published_at: UTC datetime of original publication.
    fetched_at  : UTC datetime when this service stored the row.
    provider    : "newsapi" | "gnews" | "newsdata" | "webfetch"

    LLM parse result (filled by the ingestion LLM phase via LLMPing):
    llm_answer       : the parsed/structured answer returned by the LLM Brain,
                       stored as received. None until the article has been
                       processed.
    llm_provider     : LLMPing-reported provider used for the parse.
    llm_model        : LLMPing-reported model used for the parse.
    llm_processed_at : ISO 8601 UTC timestamp of when the parse was stored.
    """

    model_config = ConfigDict(
        # Serialize datetime as ISO 8601 strings (UTC, no timezone offset shown
        # as Go's RFC3339 emits "Z"). FastAPI uses this automatically.
        json_encoders={datetime: lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")},
    )

    id: str
    title: str
    description: Optional[str] = Field(default=None)
    content: Optional[str] = Field(default=None)
    url: str
    image_url: Optional[str] = Field(default=None)
    source: Optional[str] = Field(default=None)
    author: Optional[str] = Field(default=None)
    category: Optional[str] = Field(default=None)
    published_at: datetime
    fetched_at: datetime
    provider: str
    llm_answer: Optional[str] = Field(default=None)
    llm_provider: Optional[str] = Field(default=None)
    llm_model: Optional[str] = Field(default=None)
    llm_processed_at: Optional[str] = Field(default=None)


# ---------------------------------------------------------------------------
# IngestionResult — summary of one ingestion run (plain dataclass, not API-
# facing, but serialized to JSON by /api/stats and POST /api/ingest).
# ---------------------------------------------------------------------------

@dataclass
class IngestionResult:
    """
    Summary of one ingestion cycle returned by run_ingestion().

    provider   : name of the provider that won the fallback chain ("sample"
                 in SAMPLE_DATA mode, "" if all providers failed).
    total      : raw article count returned by the winning provider.
    inserted   : rows actually upserted into the DB.
    skipped    : articles dropped (no title/URL, in-run dupe, or DB error).
    deleted    : stale articles removed by the retention sweep at run end.
    parsed     : articles successfully processed by the LLM Brain (LLMPing) this run.
    parse_failed: articles whose LLM parse failed (upstream error/timeout) — the
                 article row remains stored with llm_answer=None.
    source_time: ISO 8601 UTC timestamp of when the provider was queried.
    """

    provider: str = ""
    total: int = 0
    inserted: int = 0
    skipped: int = 0
    deleted: int = 0
    parsed: int = 0
    parse_failed: int = 0
    source_time: str = field(
        default_factory=lambda: datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "total": self.total,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "parsed": self.parsed,
            "parse_failed": self.parse_failed,
            "source_time": self.source_time,
        }
