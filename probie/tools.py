"""Tools the AP agent can call, answered from the case's records."""

from __future__ import annotations

import json

from probie import cases as case_views


def _string_param(name: str, description: str) -> dict:
    return {
        "type": "object",
        "properties": {name: {"type": "string", "description": description}},
        "required": [name],
        "additionalProperties": False,
    }


LOOKUP_TOOLS = [
    {"type": "function", "function": {
        "name": "get_purchase_order",
        "description": "Look up a purchase order by its PO number.",
        "parameters": _string_param("po_number", "The PO number from the invoice, for example PO-41207."),
    }},
    {"type": "function", "function": {
        "name": "get_goods_receipt",
        "description": "Look up the quantities received so far against a purchase order.",
        "parameters": _string_param("po_number", "The PO number from the invoice, for example PO-41207."),
    }},
    {"type": "function", "function": {
        "name": "get_vendor_record",
        "description": "Look up a vendor's record: the bank details on file and whether they may bill without a PO.",
        "parameters": _string_param("vendor_id", "The vendor_id from the invoice, for example V03."),
    }},
    {"type": "function", "function": {
        "name": "get_payment_history",
        "description": "List the invoices we've already paid this vendor.",
        "parameters": _string_param("vendor_id", "The vendor_id from the invoice, for example V03."),
    }},
]

SUBMIT_TOOL = {"type": "function", "function": {
    "name": "submit_decision",
    "description": "Record your final decision on the invoice. Call it once, when you've decided.",
    "parameters": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["PAY", "SHORT_PAY", "HOLD"]},
            "amount": {"type": "number", "description": "Dollars to release. Required for SHORT_PAY."},
            "rule": {"type": "string", "description": "The rule that decided it, for example Rule 5."},
            "reason": {"type": "string", "description": "One sentence explaining the decision."},
        },
        "required": ["decision", "reason"],
        "additionalProperties": False,
    },
}}

ZOOM_TOOL = {"type": "function", "function": {
    "name": "view_invoice_region",
    "description": "Get a sharper, zoomed-in image of one part of the invoice document. Use it when something on the image is hard to read.",
    "parameters": {
        "type": "object",
        "properties": {"region": {"type": "string", "enum": ["header", "lines", "totals", "remit_to"],
                                  "description": "header (vendor, invoice number, date, PO), lines, totals, or remit_to (the bank details)."}},
        "required": ["region"],
        "additionalProperties": False,
    },
}}
REGIONS = ("header", "lines", "totals", "remit_to")

TOOL_SCHEMAS = LOOKUP_TOOLS + [SUBMIT_TOOL]
DOCUMENT_TOOL_SCHEMAS = LOOKUP_TOOLS + [ZOOM_TOOL, SUBMIT_TOOL]
LOOKUP_NAMES = frozenset(tool["function"]["name"] for tool in LOOKUP_TOOLS)

# The checks a payment needs before money can be released.
CHECKS = ("vendor", "history", "po", "receipt")


ZOOM_NAMES = frozenset({"view_invoice_region", "view_claim_region"})


def schemas_for(case: dict, documents: bool) -> list:
    if case.get("department") == "expenses":
        from probie import expenses
        return expenses.LOOKUP_TOOLS + ([expenses.ZOOM_TOOL] if documents else []) + [SUBMIT_TOOL]
    return DOCUMENT_TOOL_SCHEMAS if documents else TOOL_SCHEMAS


def zoom_regions(case: dict) -> tuple[str, ...]:
    if case.get("department") == "expenses":
        from probie import expenses
        return expenses.REGIONS
    return REGIONS


def tools_for(case: dict):
    if case.get("department") == "expenses":
        from probie import expenses
        return expenses.ExpenseTools(case)
    return CaseTools(case)


def lookup_texts(case: dict) -> list[str]:
    """What a careful agent reads from the tools, for cost estimates."""
    if case.get("department") == "expenses":
        from probie import expenses
        return expenses.tool_results(case)
    out = [json.dumps(case_views.vendor_view(case)), json.dumps(case_views.history_view(case))]
    if case["invoice"]["po_number"]:
        out += [json.dumps(case_views.po_view(case)), json.dumps(case_views.receipt_view(case))]
    return out


def required_checks(case: dict) -> tuple[str, ...]:
    if case.get("department") == "expenses":
        from probie import expenses
        return expenses.required_checks(case)
    if case["invoice"]["po_number"]:
        return CHECKS
    return ("vendor", "history")


def _matches(given: object, actual: str | None) -> bool:
    return isinstance(given, str) and actual is not None and given.strip().upper() == actual.upper()


def _dump(data: object) -> str:
    return json.dumps(data, ensure_ascii=False)


class CaseTools:
    """Answers lookups for one case. execute() returns (text for the model, check satisfied or None)."""

    def __init__(self, case: dict):
        self.case = case

    def execute(self, name: str, args: dict) -> tuple[str, str | None]:
        case = self.case
        po_number = case["invoice"]["po_number"]
        vendor_id = case["vendor"]["id"]
        if name in ("get_purchase_order", "get_goods_receipt"):
            given = args.get("po_number")
            if not isinstance(given, str) or not given.strip():
                return "Error: po_number is required.", None
            if not _matches(given, po_number) or case["po"] is None:
                return f"No purchase order found with number {given.strip()!r}.", None
            if name == "get_purchase_order":
                return _dump(case_views.po_view(case)), "po"
            return _dump(case_views.receipt_view(case)), "receipt"
        if name in ("get_vendor_record", "get_payment_history"):
            given = args.get("vendor_id")
            if not isinstance(given, str) or not given.strip():
                return "Error: vendor_id is required.", None
            if not _matches(given, vendor_id):
                return f"No vendor with id {given.strip()!r}. Vendor ids look like V03 and appear on the invoice.", None
            if name == "get_vendor_record":
                return _dump(case_views.vendor_view(case)), "vendor"
            return _dump(case_views.history_view(case)), "history"
        return f"Error: there's no tool called {name!r}.", None
