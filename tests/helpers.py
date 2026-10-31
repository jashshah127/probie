"""Helpers shared by the tests."""

from __future__ import annotations

import copy
import json

from probie import policy

BANK = {"bank_name": "Ridgeway Community Bank", "routing": "912345670", "account": "0083321940"}


def make_case(**changes) -> dict:
    """A clean PO invoice that should be paid in full: two lines, everything received, no freight."""
    case = {
        "id": "KRF-9001",
        "type": "price_variance",
        "variant": "within_tolerance",
        "trap": False,
        "flag": "Unit price above the purchase order.",
        "invoice": {
            "number": "ACP-10001",
            "vendor_id": "V01",
            "vendor_name": "Alder Creek Produce",
            "date": "2026-10-10",
            "po_number": "PO-40001",
            "lines": [
                {"sku": "A", "description": "Item A", "qty": 10, "unit_cents": 5000, "amount_cents": 50000},
                {"sku": "B", "description": "Item B", "qty": 4, "unit_cents": 2500, "amount_cents": 10000},
            ],
            "freight_cents": 0,
            "total_cents": 60000,
            "remit_to": dict(BANK),
        },
        "message": None,
        "po": {
            "number": "PO-40001",
            "vendor_id": "V01",
            "date": "2026-09-30",
            "freight_terms": "billed",
            "lines": [
                {"sku": "A", "description": "Item A", "qty": 10, "unit_cents": 5000},
                {"sku": "B", "description": "Item B", "qty": 4, "unit_cents": 2500},
            ],
        },
        "receipt": {"po_number": "PO-40001", "date": "2026-10-08", "lines": [{"sku": "A", "qty": 10}, {"sku": "B", "qty": 4}]},
        "vendor": {"id": "V01", "name": "Alder Creek Produce", "bank": dict(BANK), "no_po_allowed": False},
        "history": [{"invoice_number": "ACP-09990", "po_number": "PO-39990", "total_cents": 12345, "paid_on": "2026-08-01"}],
        "message_bank": None,
    }
    case = copy.deepcopy(case)
    for key, value in changes.items():
        case[key] = value
    return case


def retotal(case: dict) -> dict:
    invoice = case["invoice"]
    for line in invoice["lines"]:
        line["amount_cents"] = line["qty"] * line["unit_cents"]
    invoice["total_cents"] = sum(line["amount_cents"] for line in invoice["lines"]) + invoice["freight_cents"]
    return case


def with_answer(case: dict) -> dict:
    case["answer"] = policy.decide(case).as_dict()
    return case


def response(content=None, tool_calls=None, *, finish="stop", prompt_tokens=100, completion_tokens=20, cached=0):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "prompt_tokens_details": {"cached_tokens": cached}},
    }


def call(name: str, args, call_id: str | None = "c1"):
    arguments = args if isinstance(args, str) else json.dumps(args)
    tc = {"type": "function", "function": {"name": name, "arguments": arguments}}
    if call_id is not None:
        tc["id"] = call_id
    return tc


class ScriptedClient:
    """Returns prepared responses in order and records every payload it was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def chat(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        if not self.responses:
            raise AssertionError("the scripted client ran out of responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
