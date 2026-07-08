"""
db/report_store.py — SQLite-backed persistent storage for Argus reports.

Replaces the file-based reports/risk_commit-{sha}.json pattern.

Design:
- WAL mode: multiple concurrent readers, single writer — safe for multi-user server
- (program_id, sha) PRIMARY KEY: strict customer isolation, no cross-tenant leakage
- Write timeout 30s: concurrent writes queue safely instead of crashing
- DB path: ARGUS_DB_PATH env var (default data/argus.db) — point at persistent
  volume on Eris so data survives container restarts

Tables:
  risk_reports   — one row per (customer, SHA), full JSON + markdown
  failure_reports — one row per (customer, program_id), latest failure analysis
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────

def _db_path() -> str:
    """
    Resolve DB path. On Eris set ARGUS_DB_PATH=/persistent/argus/argus.db
    so the file survives container restarts. Defaults to data/argus.db.
    """
    path = os.getenv("ARGUS_DB_PATH", "data/argus.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


# ── Connection (thread-local so each thread gets its own connection) ──────────

_local = threading.local()


def _conn() -> sqlite3.Connection:
    """Return a per-thread SQLite connection with WAL mode enabled."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(
            _db_path(),
            timeout=30,           # wait up to 30s for write lock (multi-user safety)
            check_same_thread=False,
        )
        _local.conn.row_factory = sqlite3.Row
        # WAL: readers never block writers, writers never block readers
        _local.conn.execute("PRAGMA journal_mode=WAL")
        # Synchronous=NORMAL: safe with WAL, faster than FULL
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        _ensure_schema(_local.conn)
    return _local.conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS risk_reports (
            program_id   TEXT    NOT NULL,
            sha          TEXT    NOT NULL,
            customer     TEXT    NOT NULL DEFAULT '',
            risk_level   TEXT,
            confidence   INTEGER,
            report_json  TEXT    NOT NULL,
            md_text      TEXT,
            created_at   TEXT    NOT NULL,
            PRIMARY KEY (program_id, sha)
        );

        CREATE INDEX IF NOT EXISTS idx_risk_reports_sha
            ON risk_reports (sha);

        CREATE INDEX IF NOT EXISTS idx_risk_reports_customer
            ON risk_reports (program_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS failure_reports (
            program_id   TEXT    NOT NULL PRIMARY KEY,
            customer     TEXT    NOT NULL DEFAULT '',
            report_json  TEXT    NOT NULL,
            md_text      TEXT,
            updated_at   TEXT    NOT NULL
        );
    """)
    conn.commit()


# ── Public API — Risk Reports ─────────────────────────────────────────────────

def save_risk_report(
    program_id: str,
    sha: str,
    report_json: dict,
    md_text: str = "",
    customer: str = "",
) -> None:
    """
    Upsert a risk assessment for (program_id, sha).

    Customer isolation: program_id is the outer partition key — HDFC (16360)
    and IDFC (19905) results can never overwrite each other even if SHA collides.
    The `customer` field is a human-readable label for debugging only.

    Thread-safe: SQLite WAL + 30s timeout handles concurrent writes gracefully.
    """
    risk_level  = report_json.get("risk_level", "")
    confidence  = report_json.get("confidence_score") or 0
    created_at  = datetime.now(timezone.utc).isoformat()

    _conn().execute(
        """
        INSERT INTO risk_reports
            (program_id, sha, customer, risk_level, confidence, report_json, md_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (program_id, sha) DO UPDATE SET
            customer    = excluded.customer,
            risk_level  = excluded.risk_level,
            confidence  = excluded.confidence,
            report_json = excluded.report_json,
            md_text     = excluded.md_text,
            created_at  = excluded.created_at
        """,
        (
            program_id, sha, customer, risk_level, confidence,
            json.dumps(report_json), md_text, created_at,
        ),
    )
    _conn().commit()


def load_risk_report(
    program_id: str,
    sha: str,
) -> Optional[tuple[dict, str]]:
    """
    Load a risk assessment for (program_id, sha).

    Returns (report_json_dict, md_text) or None if not found.
    Customer isolation is guaranteed — only returns records for this program_id.
    """
    row = _conn().execute(
        "SELECT report_json, md_text FROM risk_reports WHERE program_id = ? AND sha = ?",
        (program_id, sha),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["report_json"]), (row["md_text"] or "")
    except (json.JSONDecodeError, TypeError):
        return None


def list_recent_reports(
    program_id: str,
    limit: int = 20,
) -> list[dict]:
    """
    List the most recent risk reports for a customer.
    Returns lightweight summaries (no full JSON) for display in history views.
    Customer isolation: scoped to program_id.
    """
    rows = _conn().execute(
        """
        SELECT sha, risk_level, confidence, created_at
        FROM risk_reports
        WHERE program_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (program_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_risk_report(program_id: str, sha: str) -> bool:
    """Delete a specific report. Returns True if a row was deleted."""
    cur = _conn().execute(
        "DELETE FROM risk_reports WHERE program_id = ? AND sha = ?",
        (program_id, sha),
    )
    _conn().commit()
    return cur.rowcount > 0


# ── Public API — Failure Analysis Reports ────────────────────────────────────

def save_failure_report(
    program_id: str,
    report_json: dict,
    md_text: str = "",
    customer: str = "",
) -> None:
    """
    Upsert the latest failure analysis report for a customer.
    One row per program_id — replaces the latest_report_{program_id}.json files.
    """
    _conn().execute(
        """
        INSERT INTO failure_reports (program_id, customer, report_json, md_text, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (program_id) DO UPDATE SET
            customer    = excluded.customer,
            report_json = excluded.report_json,
            md_text     = excluded.md_text,
            updated_at  = excluded.updated_at
        """,
        (program_id, customer, json.dumps(report_json), md_text,
         datetime.now(timezone.utc).isoformat()),
    )
    _conn().commit()


def load_failure_report(program_id: str) -> Optional[tuple[dict, str]]:
    """Load the latest failure analysis report for a customer."""
    row = _conn().execute(
        "SELECT report_json, md_text FROM failure_reports WHERE program_id = ?",
        (program_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["report_json"]), (row["md_text"] or "")
    except (json.JSONDecodeError, TypeError):
        return None


# ── Migration: import existing file-based reports ────────────────────────────

def migrate_from_files(
    reports_dir: str = "reports",
    customer_map: Optional[dict] = None,
) -> dict:
    """
    One-time migration: read existing risk_commit-*.json files and insert into DB.

    customer_map: {sha_prefix: program_id} — if None, program_id defaults to "unknown".
    Returns {"migrated": N, "skipped": N, "errors": N}.
    """
    customer_map = customer_map or {}
    migrated = skipped = errors = 0

    for json_file in Path(reports_dir).glob("risk_commit-*.json"):
        sha_part = json_file.stem.replace("risk_commit-", "")
        md_file  = json_file.with_suffix(".md")
        md_text  = md_file.read_text(encoding="utf-8") if md_file.exists() else ""

        # Try to find program_id from customer_map
        program_id = "unknown"
        for prefix, pid in customer_map.items():
            if sha_part.startswith(prefix[:8]):
                program_id = pid
                break

        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            sha  = data.get("commit_sha") or sha_part

            # Skip if already in DB
            if load_risk_report(program_id, sha):
                skipped += 1
                continue

            save_risk_report(
                program_id=program_id,
                sha=sha,
                report_json=data,
                md_text=md_text,
            )
            migrated += 1
        except Exception:
            errors += 1

    return {"migrated": migrated, "skipped": skipped, "errors": errors}


# ── Maintenance ───────────────────────────────────────────────────────────────

def db_stats() -> dict:
    """Return counts per customer for monitoring."""
    rows = _conn().execute(
        "SELECT program_id, COUNT(*) as cnt FROM risk_reports GROUP BY program_id"
    ).fetchall()
    return {r["program_id"]: r["cnt"] for r in rows}


def purge_old_reports(program_id: str, keep_latest: int = 200) -> int:
    """
    Delete old reports beyond the most recent `keep_latest` for a customer.
    Prevents unbounded growth. Returns number of rows deleted.
    """
    cur = _conn().execute(
        """
        DELETE FROM risk_reports
        WHERE program_id = ? AND sha NOT IN (
            SELECT sha FROM risk_reports
            WHERE program_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        )
        """,
        (program_id, program_id, keep_latest),
    )
    _conn().commit()
    return cur.rowcount
