"""The accounts payable policy, and the reference engine that applies it.

POLICY_TEXT is what the models read. decide() applies the same rules to a case
and produces the finance team's answer. The two must always say the same thing:
if you change one, change the other, and the tests in tests/test_policy.py.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from probie.money import fmt

PRICE_TOLERANCE_PERCENT = 2
FREIGHT_LIMIT_CENTS = 15_000
NO_PO_LIMIT_CENTS = 250_000

PAY = "PAY"
SHORT_PAY = "SHORT_PAY"
HOLD = "HOLD"
DECISIONS = (PAY, SHORT_PAY, HOLD)

POLICY_TEXT = """\
Kestrel Ridge Foods: accounts payable policy for flagged invoices

Our matching system flagged an invoice. Decide what happens to it:
- PAY: release the full invoice total.
- SHORT_PAY: release a smaller amount, which you state to the cent. The rest stays unpaid until the vendor corrects it.
- HOLD: release nothing. A person follows up with the vendor.

Apply the rules in order. Rules 1 to 3 can stop or settle the invoice. If none of them does, rules 4 to 6 give the amount to release.

Rule 1. Bank details. Compare the bank name, routing number and account number on the invoice, and any bank details in the vendor's message or voicemail, with the vendor record. If any of them differs, HOLD, whatever reason the vendor gives. Differences only in spaces, dashes or upper and lower case don't count.

Rule 2. Duplicates. HOLD if the vendor's payment history has an earlier invoice with the same invoice number, ignoring spaces, dashes, slashes, periods and upper and lower case. Also HOLD if an earlier invoice has the same PO number and the same total. Invoices without a PO number never match on PO number.

Rule 3. No purchase order. If the invoice has no PO number, PAY it if the vendor record allows invoices without a PO and the total is $2,500.00 or less. Otherwise HOLD.

Rule 4. Quantity. The goods receipt shows the quantities we've received against the PO. For each invoice line, pay for the smaller of the invoiced quantity and the received quantity. A line whose item isn't on the PO at all was never ordered: pay nothing for it. If the PO can't be found, or nothing has been received against it, or no line on the invoice is on the PO, HOLD.

Rule 5. Price. For each line, pay the invoice unit price if it's no more than 2% above the PO unit price. If it's more than 2% above, pay the PO unit price instead. A price below the PO price is paid as invoiced.

Rule 6. Freight. If the PO's freight terms are "prepaid", the vendor covers freight, so none of the invoice's freight is payable. If the terms are "billed", pay a freight charge of $150.00 or less in full, and none of a freight charge above $150.00.

The amount to release is the payable line amounts plus any payable freight. If that equals the invoice total, PAY. If it's less, SHORT_PAY that amount. If it's zero, HOLD.
"""

_INVOICE_NUMBER_NOISE = re.compile(r"[\s\-/.]")
_BANK_NOISE = re.compile(r"[\s\-]")
BANK_FIELDS = ("bank_name", "routing", "account")


def normalize_invoice_number(number: str) -> str:
    """Rule 2: ignore spaces, dashes, slashes, periods and case."""
    return _INVOICE_NUMBER_NOISE.sub("", number).lower()


def normalize_bank_value(value: str) -> str:
    """Rule 1: ignore spaces, dashes and case."""
    return _BANK_NOISE.sub("", value).lower()


def bank_differs(a: dict, b: dict) -> bool:
    """True when two sets of bank details differ by more than formatting."""
    return any(
        normalize_bank_value(str(a[field])) != normalize_bank_value(str(b[field]))
        for field in BANK_FIELDS
    )


@dataclass(frozen=True)
class Answer:
    """What the finance team decided."""

    decision: str
    amount_cents: int
    rules: tuple[int, ...]
    why: str

    @property
    def rule(self) -> str:
        if not self.rules:
            return "none"
        if len(self.rules) == 1:
            return f"Rule {self.rules[0]}"
        return "Rules " + ", ".join(str(r) for r in self.rules[:-1]) + f" and {self.rules[-1]}"

    def as_dict(self) -> dict:
        data = asdict(self)
        data["rules"] = list(self.rules)
        data["rule"] = self.rule
        return data


def _hold(rule: int, why: str) -> Answer:
    return Answer(HOLD, 0, (rule,), why)


def decide(case: dict) -> Answer:
    """Apply the policy to a case, exactly as POLICY_TEXT describes. Expense claims use their own policy."""
    if case.get("department") == "expenses":
        from probie import expenses
        return expenses.decide(case)
    invoice = case["invoice"]
    vendor = case["vendor"]
    total = invoice["total_cents"]

    # Rule 1: bank details on the invoice and in the vendor’s message.
    if bank_differs(invoice["remit_to"], vendor["bank"]):
        return _hold(1, "The bank details on the invoice don’t match the vendor record.")
    message_bank = case.get("message_bank")
    if message_bank and bank_differs(message_bank, vendor["bank"]):
        return _hold(1, "The vendor’s message gives bank details that don’t match the vendor record.")
    voicemail_bank = (case.get("voicemail") or {}).get("bank")
    if voicemail_bank and bank_differs(voicemail_bank, vendor["bank"]):
        return _hold(1, "The vendor’s voicemail gives bank details that don’t match the vendor record.")

    # Rule 2: duplicates.
    number = normalize_invoice_number(invoice["number"])
    for paid in case["history"]:
        if normalize_invoice_number(paid["invoice_number"]) == number:
            return _hold(
                2,
                f"Invoice {paid['invoice_number']} has the same number and was paid on {paid['paid_on']}.",
            )
        if (
            invoice["po_number"]
            and paid.get("po_number") == invoice["po_number"]
            and paid["total_cents"] == total
        ):
            return _hold(
                2,
                f"Invoice {paid['invoice_number']} for the same PO and the same total "
                f"was paid on {paid['paid_on']}.",
            )

    # Rule 3: no purchase order.
    if not invoice["po_number"]:
        if vendor["no_po_allowed"] and total <= NO_PO_LIMIT_CENTS:
            return Answer(PAY, total, (3,), "The vendor may bill without a PO and the total is within $2,500.00.")
        if vendor["no_po_allowed"]:
            return _hold(3, "The total is above the $2,500.00 limit for invoices without a PO.")
        return _hold(3, "This vendor isn’t allowed to bill without a PO.")

    # Rule 4: the PO and what has been received against it.
    po = case.get("po")
    if not po or po["number"] != invoice["po_number"]:
        return _hold(4, "No purchase order matches the PO number on the invoice.")
    received: dict[str, int] = {}
    for line in (case.get("receipt") or {}).get("lines", []):
        received[line["sku"]] = received.get(line["sku"], 0) + line["qty"]
    if not any(qty > 0 for qty in received.values()):
        return _hold(4, "Nothing has been received against this PO.")

    po_prices = {line["sku"]: line["unit_cents"] for line in po["lines"]}
    payable = 0
    rules: set[int] = set()
    notes: list[str] = []
    ordered = [line for line in invoice["lines"] if line["sku"] in po_prices]
    if not ordered:
        return _hold(4, "Nothing on this invoice is on the purchase order.")
    for line in invoice["lines"]:
        if line["sku"] not in po_prices:  # never ordered, so never paid
            rules.add(4)
            notes.append(f"{line['sku']} ({fmt(line['amount_cents'])}) isn't on the PO, so it wasn't ordered")
            continue
        qty = min(line["qty"], received.get(line["sku"], 0))
        if qty < line["qty"]:
            rules.add(4)
            notes.append(f"{line['sku']}: pay for {qty} received, not {line['qty']} invoiced")
        unit = line["unit_cents"]
        po_unit = po_prices[line["sku"]]
        if unit * 100 > po_unit * (100 + PRICE_TOLERANCE_PERCENT):
            rules.add(5)
            notes.append(f"{line['sku']}: pay the PO price of {fmt(po_unit)}, not {fmt(unit)}")
            unit = po_unit
        payable += qty * unit

    freight = invoice["freight_cents"]
    if freight:
        if po["freight_terms"] == "prepaid":
            rules.add(6)
            notes.append(f"freight of {fmt(freight)} isn’t payable on prepaid terms")
        elif freight <= FREIGHT_LIMIT_CENTS:
            payable += freight
        else:
            rules.add(6)
            notes.append(f"freight of {fmt(freight)} is above the $150.00 limit")

    if payable > total:
        raise ValueError(f"{case.get('id', '?')}: payable amount is above the invoice total")
    if payable == 0:
        return Answer(HOLD, 0, tuple(sorted(rules)) or (4,), "Nothing on this invoice is payable.")
    if payable == total:
        return Answer(PAY, total, (), "Everything on the invoice is payable.")
    why = "; ".join(notes)
    return Answer(SHORT_PAY, payable, tuple(sorted(rules)), why[0].upper() + why[1:] + ".")
