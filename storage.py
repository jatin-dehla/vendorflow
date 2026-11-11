"""SQLite storage for VendorFlow demo run history.

Only completed runs (execution_status SUCCESS) are stored. The full record is kept
as JSON so old runs are shown exactly as they were produced.
NOTE: on free hosting this file is shared by all visitors and may be lost on restart.
"""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

DEFAULT_DB = os.environ.get("VENDORFLOW_DB", str(Path(__file__).with_name("vendorflow.db")))


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=None):
    with closing(_connect(db_path)) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                submitted_at_utc TEXT NOT NULL,
                legal_name TEXT,
                country TEXT,
                canonical_tax_id TEXT,
                decision TEXT NOT NULL,
                execution_status TEXT NOT NULL,
                summary TEXT,
                record_json TEXT NOT NULL
            )"""
        )


def save_run(record, db_path=None):
    """Insert one completed run. Raises on any database problem."""
    init_db(db_path)
    vendor = record["vendor"]
    reasons = record.get("internal_reasons") or [""]
    with closing(_connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO runs (run_id, submitted_at_utc, legal_name, country, canonical_tax_id,"
            " decision, execution_status, summary, record_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (record["run_id"], record["submitted_at_utc"], vendor["legal_name"],
             vendor["country"], vendor["canonical_tax_id"], record["decision"],
             record["execution_status"], reasons[0], json.dumps(record)),
        )


def find_prior_identity(country, canonical_tax_id, db_path=None):
    """Earliest stored non-rejected run with the same (country, tax_id), or None."""
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT run_id, decision, submitted_at_utc FROM runs WHERE country=? AND"
            " canonical_tax_id=? AND execution_status='SUCCESS' AND decision IN"
            " ('APPROVED','PENDING') ORDER BY submitted_at_utc, rowid LIMIT 1",
            (country, canonical_tax_id),
        ).fetchone()
    return dict(row) if row else None


def list_runs(limit=200, db_path=None):
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT run_id, submitted_at_utc, legal_name, country, decision, summary FROM runs"
            " ORDER BY submitted_at_utc DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id, db_path=None):
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT record_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row["record_json"]) if row else None


def counts(db_path=None):
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT decision, COUNT(*) AS n FROM runs WHERE execution_status='SUCCESS'"
            " GROUP BY decision").fetchall()
    by = {r["decision"]: r["n"] for r in rows}
    return {"total": sum(by.values()), "approved": by.get("APPROVED", 0),
            "pending": by.get("PENDING", 0), "rejected": by.get("REJECTED", 0)}


def clear_all(db_path=None):
    """Delete all saved demo runs. The denylist lives in code and is untouched."""
    init_db(db_path)
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM runs")
