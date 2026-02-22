"""
Validation, logging, sanitization, and shared helpers.
Every function is pure or clearly side-effect-labeled.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite
import structlog

from app.models import AuditRecord

# ---------- Structured Logging ----------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if True else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger()


# ---------- ID Generation ----------

def generate_job_id() -> str:
    """Short, collision-resistant job ID for tracing."""
    return f"JOB-{uuid.uuid4().hex[:12].upper()}"


def generate_voucher_number() -> str:
    """Unique voucher number with timestamp prefix."""
    ts = int(time.time() * 1000) % 10_000_000
    return f"WA-{ts}-{uuid.uuid4().hex[:6].upper()}"


def generate_guid() -> str:
    """GUID for Tally duplicate prevention (RemoteID)."""
    return str(uuid.uuid4())


# ---------- Aadhaar Validation ----------

# Verhoeff algorithm tables for Aadhaar checksum
_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]

_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]

_VERHOEFF_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def validate_aadhaar_checksum(aadhaar: str) -> bool:
    """
    Validate 12-digit Aadhaar using the Verhoeff algorithm.
    UIDAI uses Verhoeff for the last digit as a check digit.
    Returns True if the checksum is valid.
    """
    if not re.match(r"^\d{12}$", aadhaar):
        return False

    c = 0
    # Process digits from right to left
    for i, digit_char in enumerate(reversed(aadhaar)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(digit_char)]]
    return c == 0


def mask_aadhaar(aadhaar: str) -> str:
    """
    Mask first 8 digits of Aadhaar for UIDAI compliance.
    '123456789012' → 'XXXX-XXXX-9012'
    """
    if len(aadhaar) != 12:
        return "XXXX-XXXX-0000"
    return f"XXXX-XXXX-{aadhaar[8:]}"


# ---------- Name Sanitization ----------

def sanitize_name_for_tally(name: str) -> str:
    """
    Clean name for Tally XML compatibility:
    - Title case
    - Remove chars that break XML (<, >, &, ', ")
    - Collapse multiple spaces
    - Strip leading/trailing whitespace
    Critical: preserves exact spelling from Aadhaar for cheque printing.
    """
    if not name:
        return ""
    # Remove XML-breaking characters
    cleaned = re.sub(r"[<>&'\"]", "", name)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Title case (preserves individual word spelling)
    cleaned = cleaned.title()
    return cleaned


def sanitize_for_xml(text: str) -> str:
    """Escape XML special characters in arbitrary text."""
    if not text:
        return ""
    replacements = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "'": "&apos;",
        '"': "&quot;",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


# ---------- SQLite Audit Log ----------

_DB_INIT_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    from_number TEXT NOT NULL,
    masked_aadhaar TEXT NOT NULL,
    party_name TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    voucher_type TEXT NOT NULL,
    amount REAL NOT NULL,
    tally_success INTEGER NOT NULL DEFAULT 0,
    cheque_generated INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_id ON audit_log(job_id);
CREATE INDEX IF NOT EXISTS idx_from_number ON audit_log(from_number);
"""


async def init_db(db_path: str) -> None:
    """Create audit table if it doesn't exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_DB_INIT_SQL)
        await db.commit()
    log.info("database_initialized", path=db_path)


async def insert_audit(db_path: str, record: AuditRecord) -> None:
    """Insert a single audit record. Never stores full Aadhaar."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT OR REPLACE INTO audit_log
               (job_id, from_number, masked_aadhaar, party_name,
                voucher_number, voucher_type, amount,
                tally_success, cheque_generated, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.job_id,
                record.from_number,
                record.masked_aadhaar,
                record.party_name,
                record.voucher_number,
                record.voucher_type,
                record.amount,
                int(record.tally_success),
                int(record.cheque_generated),
                record.error,
                record.created_at.isoformat(),
            ),
        )
        await db.commit()
    log.info("audit_record_saved", job_id=record.job_id)


async def get_audit_by_job(db_path: str, job_id: str) -> dict | None:
    """Retrieve a single audit record by job_id."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM audit_log WHERE job_id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
