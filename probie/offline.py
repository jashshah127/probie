"""Offline stand-ins for real models. They are NOT Meta models.

They exist so the pipeline can run end to end with no API key and no cost: for
the tests, the demo report and a dry run before spending credits. Each one reads
the answer engine and makes a known kind of mistake, so the report has something
to find. Results from them are labelled as offline everywhere.

  careful: checks everything; sometimes treats a price exactly 2% over as too high.
  quick:   checks everything but never reads the vendor's email or hears its voicemail.
  sloppy:  skips the payment history, ignores messages, sometimes pays in full
           when it should short-pay.
"""

from __future__ import annotations

import copy
import json
import random
import re

from probie import documents, policy
from probie.money import fmt
from probie.policy import HOLD, PAY, SHORT_PAY

_CASE_ID = re.compile(r"Case ([A-Z]+-\d+)")


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class OfflineClient:
    def __init__(self, cases: list[dict], profile: str):
        if profile not in ("careful", "quick", "sloppy"):
            raise ValueError(f"unknown offline profile {profile!r}")
        self.cases = {case["id"]: case for case in cases}
        self.profile = profile

    def _case(self, payload: dict) -> dict:
        for message in payload.get("messages", []):
            if message.get("role") == "user":
                content = message.get("content") or ""
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
                match = _CASE_ID.search(content)
                if match and match.group(1) in self.cases:
                    return self.cases[match.group(1)]
        raise ValueError("offline model: no known case id in the conversation")

    def _decide(self, case: dict) -> policy.Answer:
        rng = random.Random(f"{self.profile}:{case['id']}")
        view = copy.deepcopy(case)
        if self.profile in ("quick", "sloppy"):
            view["message_bank"] = None
            view["voicemail"] = None
        if self.profile == "quick" and case.get("department") == "expenses":
            for line in view["invoice"]["lines"]:  # skims the receipts, so alcohol and minibar charges slip through
                for item in line.get("items", []):
                    item["excluded"] = False
        if self.profile == "sloppy":
            view["history"] = []
        answer = policy.decide(view)
        if self.profile == "careful" and case["variant"] == "at_tolerance" and answer.decision == PAY and rng.random() < 0.4:
            return self._strict_tolerance(case)
        if self.profile == "sloppy" and answer.decision == SHORT_PAY and rng.random() < 0.12:
            total = case["invoice"]["total_cents"]
            return policy.Answer(PAY, total, (), "Everything looks payable.")
        return answer

    @staticmethod
    def _strict_tolerance(case: dict) -> policy.Answer:
        po_prices = {line["sku"]: line["unit_cents"] for line in case["po"]["lines"]}
        payable = case["invoice"]["total_cents"]
        for line in case["invoice"]["lines"]:
            po_unit = po_prices[line["sku"]]
            if line["unit_cents"] * 100 == po_unit * 102:
                payable -= line["qty"] * (line["unit_cents"] - po_unit)
        return policy.Answer(SHORT_PAY, payable, (5,), "Paid the PO price on the line that was 2% over.")

    @staticmethod
    def _decision_fields(answer: policy.Answer) -> dict:
        fields = {"decision": answer.decision, "rule": answer.rule, "reason": answer.why}
        if answer.decision == SHORT_PAY:
            fields["amount"] = round(answer.amount_cents / 100, 2)
        return fields

    def _response(self, payload: dict, content: str | None, tool_calls: list | None) -> dict:
        messages = payload.get("messages", [])
        prompt_tokens = _tokens(json.dumps(messages)) + _tokens(json.dumps(payload.get("tools") or []))
        cached = _tokens(messages[0]["content"]) if len(messages) > 2 else 0
        out_text = (content or "") + json.dumps(tool_calls or [])
        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": f"offline-{self.profile}",
            "object": "chat.completion",
            "model": f"offline-{self.profile}",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 60 + _tokens(out_text),
                "prompt_tokens_details": {"cached_tokens": min(cached, prompt_tokens)},
            },
        }

    def _expense_step(self, payload: dict, case: dict, rounds: int, names: set) -> dict:
        claim = case["invoice"]
        calls: list[tuple[str, dict]] = []
        if rounds == 0:
            if "view_claim_region" in names and self.profile == "careful" and documents.quality_for(case["id"]) != "clean":
                calls.append(("view_claim_region", {"region": "receipts"}))
            calls.append(("get_employee_record", {"employee_id": claim["vendor_id"]}))
            if self.profile != "sloppy":
                calls.append(("get_reimbursement_history", {"employee_id": claim["vendor_id"]}))
            content = "Checking the HR record first."
        elif rounds == 1 and claim["po_number"]:
            calls.append(("get_travel_approval", {"approval_number": claim["po_number"]}))
            content = "Now the travel approval."
        else:
            answer = self._decide(case)
            calls.append(("submit_decision", self._decision_fields(answer)))
            content = f"Deciding {answer.decision}."
        tool_calls = [{"id": f"call_{rounds}_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                      for i, (n, a) in enumerate(calls)]
        return self._response(payload, content, tool_calls)

    def chat(self, payload: dict) -> dict:
        case = self._case(payload)
        if not payload.get("tools"):
            answer = self._decide(case)
            return self._response(payload, json.dumps(self._decision_fields(answer)), None)

        messages = payload["messages"]
        rounds = sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
        invoice = case["invoice"]
        calls: list[tuple[str, dict]] = []
        names = {t["function"]["name"] for t in payload.get("tools") or []}
        if case.get("department") == "expenses":
            return self._expense_step(payload, case, rounds, names)
        if rounds == 0:
            if ("view_invoice_region" in names and self.profile == "careful"
                    and documents.quality_for(case["id"]) != "clean"):
                calls.append(("view_invoice_region", {"region": "remit_to"}))
            calls.append(("get_vendor_record", {"vendor_id": invoice["vendor_id"]}))
            if self.profile != "sloppy":
                calls.append(("get_payment_history", {"vendor_id": invoice["vendor_id"]}))
            content = "Checking the vendor record first."
        elif rounds == 1 and invoice["po_number"]:
            calls.append(("get_purchase_order", {"po_number": invoice["po_number"]}))
            calls.append(("get_goods_receipt", {"po_number": invoice["po_number"]}))
            content = "Now the purchase order and what was received."
        else:
            answer = self._decide(case)
            calls.append(("submit_decision", self._decision_fields(answer)))
            amount = f" for {fmt(answer.amount_cents)}" if answer.decision == SHORT_PAY else ""
            content = f"Deciding {answer.decision}{amount}." if answer.decision != HOLD else "Holding this one."
        tool_calls = [
            {"id": f"call_{rounds}_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(calls)
        ]
        return self._response(payload, content, tool_calls)
