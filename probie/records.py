"""The decision record: what was checked, what was decided, and whether we've seen it before.

Probie never keeps the document. It keeps the decision: a fingerprint of the file, the fields that
were read off it, which checks ran, and what the verdict was. That is kilobytes per invoice instead
of megabytes, it gives an auditor everything they ask for, and it leaves the file where it already
lives — in the ERP, the mailbox, the drive.

Two things fall out of it:

* **Duplicate detection.** The same file arriving twice, the same invoice number from the same
  vendor, or the same vendor, amount and date under a new number — the third being how duplicate
  billing usually looks, because a fraudster changes the number.
* **An audit trail.** One row per decision, exportable, showing what was known at the time.

This is a private record belonging to whoever ran the check, and it is deliberately NOT the public
benchmark: it is never synced anywhere, and the public game never writes to it, because the game
promises visitors that nothing about their document is kept.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

FILENAME = "records.sqlite"


def fingerprint(data: bytes) -> str:
    """A document's identity, without the document."""
    return hashlib.sha256(data).hexdigest()


def _norm_number(value) -> str:
    """Invoice numbers match the way the policy matches them: punctuation and case don't count."""
    return re.sub(r"[\s\-/.]", "", str(value or "")).upper()


def _norm_vendor(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


@dataclass(frozen=True)
class Match:
    """A reason to think this document has been seen before."""

    kind: str
    at: float
    decision: str
    document_number: str | None
    total: str | None

    @property
    def why(self) -> str:
        when = time.strftime("%d %b %Y", time.localtime(self.at))
        return {
            "same_file": f"the identical file was checked on {when}",
            "same_number": f"invoice {self.document_number} from this vendor was checked on {when}",
            "same_amount": f"the same vendor billed {self.total} on the same date, as {self.document_number}, on {when}",
        }[self.kind]


class Records:
    """One SQLite file of decisions. Not synced, not public."""

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL,
            fingerprint TEXT NOT NULL,
            filename TEXT,
            vendor_name TEXT,
            vendor_key TEXT,
            document_number TEXT,
            number_key TEXT,
            document_date TEXT,
            total TEXT,
            total_cents INTEGER,
            decision TEXT,
            amount_cents INTEGER,
            rule TEXT,
            why TEXT,
            checks TEXT,
            attacks_caught INTEGER,
            attacks_total INTEGER,
            models TEXT,
            reader TEXT,
            confidence TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_fingerprint ON decisions(fingerprint)")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_number ON decisions(vendor_key, number_key)")
        self.db.commit()

    # Duplicate detection.
    def seen_before(self, *, fingerprint: str, vendor_name=None, document_number=None, document_date=None,
                    total_cents=None) -> list[Match]:
        """Every reason to think this one has been through before, strongest first."""
        vendor_key, number_key = _norm_vendor(vendor_name), _norm_number(document_number)
        found: list[Match] = []
        with self.lock:
            rows = self.db.execute(
                "SELECT at, decision, document_number, total FROM decisions WHERE fingerprint = ? ORDER BY at DESC",
                (fingerprint,)).fetchall()
            found += [Match("same_file", *row) for row in rows]
            if vendor_key and number_key:
                rows = self.db.execute(
                    "SELECT at, decision, document_number, total FROM decisions "
                    "WHERE vendor_key = ? AND number_key = ? AND fingerprint != ? ORDER BY at DESC",
                    (vendor_key, number_key, fingerprint)).fetchall()
                found += [Match("same_number", *row) for row in rows]
            if vendor_key and total_cents and document_date:
                rows = self.db.execute(
                    "SELECT at, decision, document_number, total FROM decisions "
                    "WHERE vendor_key = ? AND total_cents = ? AND document_date = ? AND number_key != ? "
                    "AND fingerprint != ? ORDER BY at DESC",
                    (vendor_key, int(total_cents), str(document_date), number_key, fingerprint)).fetchall()
                found += [Match("same_amount", *row) for row in rows]
        return found

    # The record itself.
    def add(self, *, fingerprint: str, fields: dict, answer: dict, card: dict | None = None,
            filename: str | None = None, reader: str | None = None, checks: list[str] | None = None,
            total_cents: int | None = None) -> int:
        models = sorted({m["model"] for m in (card or {}).get("models", [])})
        best = max(((m["caught"], m.get("attacks") and len(m["attacks"]) or 0)
                    for m in (card or {}).get("models", [])), default=(None, None))
        with self.lock:
            cursor = self.db.execute(
                "INSERT INTO decisions (at, fingerprint, filename, vendor_name, vendor_key, document_number, "
                "number_key, document_date, total, total_cents, decision, amount_cents, rule, why, checks, "
                "attacks_caught, attacks_total, models, reader, confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), fingerprint, filename, fields.get("vendor_name"), _norm_vendor(fields.get("vendor_name")),
                 fields.get("document_number"), _norm_number(fields.get("document_number")), fields.get("date"),
                 fields.get("total"), total_cents, answer.get("decision"), answer.get("amount_cents"),
                 answer.get("rule"), answer.get("why"), json.dumps(checks or []), best[0], best[1],
                 json.dumps(models), reader, fields.get("confidence")))
            self.db.commit()
            return int(cursor.lastrowid)

    def trail(self, limit: int = 50) -> list[dict]:
        with self.lock:
            self.db.row_factory = sqlite3.Row
            rows = self.db.execute("SELECT * FROM decisions ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
            self.db.row_factory = None
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self.lock:
            return int(self.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])

    def export(self, path: str | Path) -> Path:
        """The audit trail as a CSV an auditor can open."""
        path = Path(path)
        rows = self.trail(limit=1_000_000)
        columns = ["at", "filename", "vendor_name", "document_number", "document_date", "total", "decision",
                   "rule", "why", "checks", "attacks_caught", "attacks_total", "models", "reader", "confidence",
                   "fingerprint"]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["checked_at"] + columns[1:])
            for row in rows:
                row = dict(row)
                row["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["at"]))
                for key in ("checks", "models"):
                    try:
                        row[key] = ", ".join(json.loads(row[key] or "[]"))
                    except json.JSONDecodeError:
                        pass
                writer.writerow([row.get(c) for c in columns])
        return path
