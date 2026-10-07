#!/usr/bin/env python3
"""
One-shot: apply corrected categories from a JSON export to existing rows.

Takes every record of news_categorized.json and repairs ONLY the `category`
column of the matching Turso row (UPDATE news SET category = ? WHERE id = ?).

Safe by construction:
  - validates the JSON (id required, category required, no duplicate ids)
  - only touches rows that already exist (no inserts, no deletes)
  - parameterized SQL, single-column update, never rewrites other columns
  - idempotent: rows whose category already matches are skipped, so a
    repeated run performs zero updates
  - never drops/truncates/recreates the table, never touches the schema,
    never calls the LLM or ingestion, never prints credentials
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from database import open_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("apply_categories")

JSON_PATH = Path(__file__).resolve().parent.parent / "news_categorized.json"


def load_json(path: Path) -> list[dict[str, Any]]:
    """Validate the export; abort with a clear message on any bad record."""
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        sys.exit("ABORT: JSON root is not a list")

    errors: list[str] = []
    seen: Counter[str] = Counter()
    for i, rec in enumerate(records):
        rec_id = rec.get("id")
        rec_cat = rec.get("category")
        if not rec_id or not isinstance(rec_id, str):
            errors.append(f"record {i}: missing/empty id")
            continue
        if not rec_cat or not isinstance(rec_cat, str):
            errors.append(f"record {i}: missing/empty category (id={rec_id})")
        seen[rec_id] += 1

    dupes = [aid for aid, n in seen.items() if n > 1]
    if errors or dupes:
        for e in errors:
            log.error("validation: %s", e)
        for aid in dupes:
            log.error("validation: duplicate id %s (x%d)", aid, seen[aid])
        sys.exit(f"ABORT: {len(errors) + len(dupes)} JSON validation error(s)")

    return records


def main() -> int:
    records = load_json(JSON_PATH)
    log.info("JSON records: %d (all have id + category)", len(records))

    dist = Counter(rec["category"] for rec in records)
    log.info("category distribution (%d distinct categories):", len(dist))
    for cat, n in dist.most_common():
        log.info("  %-20s %d", cat, n)

    cfg = load_config()
    conn = open_db(cfg.turso_url, cfg.turso_token)

    try:
        # Existing rows — only these can ever be touched.
        db_rows: dict[str, str | None] = {
            r[0]: r[1] for r in conn.execute("SELECT id, category FROM news").fetchall()
        }
        log.info("existing Turso rows: %d", len(db_rows))

        matched = [r for r in records if r["id"] in db_rows]
        not_found = [r["id"] for r in records if r["id"] not in db_rows]
        log.info("matched ids in Turso: %d | not found: %d", len(matched), len(not_found))
        for aid in not_found:
            log.info("  not found in Turso: %s", aid)

        # Idempotence: skip rows whose category already equals the JSON value.
        to_update = [r for r in matched if db_rows[r["id"]] != r["category"]]
        unchanged = len(matched) - len(to_update)
        log.info(
            "updates needed: %d | already correct (skip): %d",
            len(to_update), unchanged,
        )

        replacements = sum(1 for r in to_update if db_rows[r["id"]])
        log.info(
            "rows whose EXISTING category was replaced: %d (JSON is source of truth)",
            replacements,
        )

        errors = 0
        for i, rec in enumerate(to_update, 1):
            try:
                cur = conn.execute(
                    "UPDATE news SET category = ? WHERE id = ?",
                    (rec["category"], rec["id"]),
                )
                if getattr(cur, "rowcount", 0) == 0:  # defensive: row vanished mid-run
                    log.warning("  no row updated for id %s", rec["id"])
                if i % 20 == 0:
                    log.info("  applied %d/%d updates...", i, len(to_update))
            except Exception as exc:
                errors += 1
                log.error("  update failed for id %s: %s", rec["id"], exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
        log.info("update errors: %d", errors)
        conn.commit()  # new connection handles inserts the same way

        # Verification — re-read every matched row and compare to the JSON.
        after: dict[str, str | None] = {
            r[0]: r[1] for r in conn.execute("SELECT id, category FROM news").fetchall()
        }
        mismatches = [r["id"] for r in matched if after.get(r["id"]) != r["category"]]

        log.info("--------------------------------------------------")
        log.info("JSON records:                     %d", len(records))
        log.info("existing Turso rows matched:      %d", len(matched))
        log.info("categories updated:               %d", len(to_update))
        log.info("IDs not found:                    %d", len(not_found))
        log.info("verification mismatches:          %d", len(mismatches))
        log.info("errors:                           %d", errors)
        total_rows_after = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        log.info(
            "row count unchanged:              %s (n=%d -> n=%d)",
            total_rows_after == len(db_rows), len(db_rows), total_rows_after,
        )
        log.info(
            "CATEGORY REPAIR: %s",
            "PASS" if not (mismatches or errors or len(not_found) > 0) else "NEEDS REVIEW",
        )
        return 0 if not (mismatches or errors) else 1
    finally:
        conn.close()
        log.info("done %s", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


if __name__ == "__main__":
    sys.exit(main())
