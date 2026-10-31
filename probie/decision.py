"""Turn what a model said into a decision we can score, or reject it."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from probie.money import parse_dollars
from probie.policy import DECISIONS, HOLD, PAY, SHORT_PAY

MAX_RULE = 80
MAX_REASON = 600


@dataclass(frozen=True)
class Decision:
    decision: str
    amount_cents: int  # money released
    rule: str
    reason: str
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class InvalidDecision(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _normalize_decision(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().upper().replace("-", "_").replace(" ", "_")
    return key if key in DECISIONS else None


def from_fields(fields: dict, invoice_total_cents: int) -> Decision:
    """Validate a decision given as fields (submit_decision arguments or a JSON reply)."""
    if not isinstance(fields, dict):
        raise InvalidDecision("bad_format", "the decision must be a JSON object")
    decision = _normalize_decision(fields.get("decision"))
    if decision is None:
        raise InvalidDecision("bad_decision", "decision must be PAY, SHORT_PAY or HOLD")
    rule = str(fields.get("rule") or "").strip()[:MAX_RULE]
    reason = str(fields.get("reason") or "").strip()[:MAX_REASON]
    if decision == HOLD:
        return Decision(HOLD, 0, rule, reason)
    if decision == PAY:
        return Decision(PAY, invoice_total_cents, rule, reason)
    if fields.get("amount") is None:
        raise InvalidDecision("missing_amount", "SHORT_PAY needs an amount in dollars")
    amount = parse_dollars(fields.get("amount"))
    if amount is None:
        raise InvalidDecision("bad_amount", "the amount must be a dollar figure such as 1234.56")
    if amount <= 0:
        raise InvalidDecision("bad_amount", "a SHORT_PAY amount must be more than zero; use HOLD to release nothing")
    if amount > invoice_total_cents:
        raise InvalidDecision("amount_above_total", "a SHORT_PAY amount can't be more than the invoice total")
    if amount == invoice_total_cents:
        return Decision(PAY, amount, rule, reason, note="short pay of the full total, scored as PAY")
    return Decision(SHORT_PAY, amount, rule, reason)


def parse_text(text: str, invoice_total_cents: int) -> Decision | None:
    """Find a decision object in free text. Uses the last JSON object that has a "decision".

    Returns None when there's no such object. Raises InvalidDecision when there is one
    but it isn't a valid decision.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    found: dict | None = None
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict) and "decision" in value:
            found = value
        index = text.find("{", end)
    if found is None:
        return None
    return from_fields(found, invoice_total_cents)
