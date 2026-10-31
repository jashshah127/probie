import tempfile
import unittest
from pathlib import Path

from probie.agent import NUDGE_AGENT, NUDGE_PROMPT, CallContext, run_case
from probie.budget import Budget, BudgetExceeded, Prices
from probie.cache import ResponseCache
from probie.config import ModelSpec
from probie.providers import ProviderError
from probie.tools import TOOL_SCHEMAS
from tests.helpers import ScriptedClient, call, make_case, response, with_answer

CASE = with_answer(make_case())


def spec(**extra):
    data = dict(name="t", model="m", base_url="https://example.test/v1", params={"reasoning_effort": "low"})
    data.update(extra)
    return ModelSpec(**data)


def context(client, *, budget=None, cache=None, **spec_extra):
    return CallContext(spec(**spec_extra), client, budget or Budget(100.0), Prices(1.0, 0.5, 2.0), cache, "test")


def lookups_then(final_calls):
    return [
        response("Looking.", [call("get_vendor_record", {"vendor_id": "V01"}, "a1"),
                              call("get_payment_history", {"vendor_id": "V01"}, "a2")]),
        response(None, [call("get_purchase_order", {"po_number": "PO-40001"}, "b1"),
                        call("get_goods_receipt", {"po_number": "PO-40001"}, "b2")]),
        response(None, final_calls),
    ]


class AgentModeTests(unittest.TestCase):
    def test_full_flow(self):
        client = ScriptedClient(lookups_then([call("submit_decision", {"decision": "PAY", "reason": "All good.", "rule": "none"}, "c1")]))
        result = run_case(CASE, context(client), 8)
        self.assertEqual((result["status"], result["decision"], result["amount_cents"]), ("decided", "PAY", 60000))
        self.assertEqual(result["checks"], ["history", "po", "receipt", "vendor"])
        self.assertEqual(result["calls"], 3)
        self.assertEqual(result["usage"], {"input": 300, "cached_input": 0, "output": 60, "reasoning": 0})
        self.assertAlmostEqual(result["cost_usd"], 3 * (100 * 1.0 + 20 * 2.0) / 1e6)
        first = client.payloads[0]
        self.assertEqual(first["tools"], TOOL_SCHEMAS)
        self.assertEqual(first["tool_choice"], "auto")
        self.assertEqual(first["max_tokens"], 4096)
        self.assertEqual(first["reasoning_effort"], "low")
        roles = [m["role"] for m in client.payloads[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual([m.get("tool_call_id") for m in client.payloads[1]["messages"][3:]], ["a1", "a2"])
        self.assertEqual(len(client.payloads[2]["messages"]), 8)
        self.assertNotIn("answer", str(client.payloads[0]["messages"]))
        self.assertEqual(len(result["steps"]), 3)

    def test_lookups_in_the_deciding_response_do_not_count(self):
        client = ScriptedClient([response(None, [call("get_vendor_record", {"vendor_id": "V01"}, "a1"),
                                                 call("submit_decision", {"decision": "PAY", "reason": "x"}, "a2")])])
        result = run_case(CASE, context(client), 8)
        self.assertEqual(result["status"], "decided")
        self.assertEqual(result["checks"], [])

    def test_bad_arguments_and_unknown_tools_are_reported_back(self):
        client = ScriptedClient([
            response(None, [call("get_vendor_record", "{bad json", "a1"), call("wire_money", {}, "a2")]),
            response(None, [call("submit_decision", {"decision": "HOLD", "reason": "r"}, "b1")]),
        ])
        result = run_case(CASE, context(client), 8)
        self.assertEqual(result["decision"], "HOLD")
        replies = [m["content"] for m in client.payloads[1]["messages"] if m["role"] == "tool"]
        self.assertTrue(replies[0].startswith("Error: the arguments"))
        self.assertIn("no tool called", replies[1])
        self.assertEqual([t["ok"] for t in result["steps"][0]["tool_calls"]], [False, False])

    def test_invalid_submission_can_be_corrected(self):
        client = ScriptedClient([
            response(None, [call("submit_decision", {"decision": "SHORT_PAY", "reason": "r"}, "a1")]),
            response(None, [call("submit_decision", {"decision": "SHORT_PAY", "amount": 550.0, "reason": "r"}, "b1")]),
        ])
        result = run_case(CASE, context(client), 8)
        self.assertEqual((result["decision"], result["amount_cents"]), ("SHORT_PAY", 55000))
        reply = [m for m in client.payloads[1]["messages"] if m["role"] == "tool"][0]["content"]
        self.assertIn("SHORT_PAY needs an amount", reply)

    def test_invalid_until_the_step_limit(self):
        bad = call("submit_decision", {"decision": "SHORT_PAY", "reason": "r"}, "x")
        client = ScriptedClient([response(None, [bad]), response(None, [bad])])
        result = run_case(CASE, context(client), 2)
        self.assertEqual((result["status"], result["unusable"]), ("unusable", "missing_amount"))

    def test_json_in_text_is_accepted(self):
        client = ScriptedClient([response('Decision: {"decision": "HOLD", "reason": "bank"}')])
        result = run_case(CASE, context(client), 8)
        self.assertEqual((result["status"], result["decision"], result["checks"]), ("decided", "HOLD", []))

    def test_content_parts(self):
        client = ScriptedClient([response([{"type": "text", "text": '{"decision": "HOLD", "reason": "x"}'}])])
        self.assertEqual(run_case(CASE, context(client), 8)["decision"], "HOLD")

    def test_nudge_then_give_up(self):
        client = ScriptedClient([response("Let me think."), response("Still thinking.")])
        result = run_case(CASE, context(client), 8)
        self.assertEqual((result["status"], result["unusable"]), ("unusable", "no_decision"))
        self.assertEqual(client.payloads[1]["messages"][-1], {"role": "user", "content": NUDGE_AGENT})

    def test_out_of_tokens(self):
        client = ScriptedClient([response("", finish="length")])
        self.assertEqual(run_case(CASE, context(client), 8)["unusable"], "length")

    def test_step_limit(self):
        look = call("get_vendor_record", {"vendor_id": "V01"}, "x")
        client = ScriptedClient([response(None, [look])] * 3)
        result = run_case(CASE, context(client), 3)
        self.assertEqual((result["unusable"], result["calls"]), ("step_limit", 3))
        self.assertEqual(result["checks"], ["vendor"])

    def test_missing_call_ids_are_filled_in(self):
        client = ScriptedClient([
            response(None, [call("get_vendor_record", {"vendor_id": "V01"}, None)]),
            response(None, [call("submit_decision", {"decision": "HOLD", "reason": "r"}, "z")]),
        ])
        run_case(CASE, context(client), 8)
        messages = client.payloads[1]["messages"]
        self.assertEqual(messages[2]["tool_calls"][0]["id"], "call_0_0")
        self.assertEqual(messages[3]["tool_call_id"], "call_0_0")

    def test_second_submission_in_one_response_is_ignored(self):
        client = ScriptedClient([response(None, [call("submit_decision", {"decision": "HOLD", "reason": "a"}, "a1"),
                                                 call("submit_decision", {"decision": "PAY", "reason": "b"}, "a2")])])
        result = run_case(CASE, context(client), 8)
        self.assertEqual(result["decision"], "HOLD")
        self.assertEqual([t["ok"] for t in result["steps"][0]["tool_calls"]], [True, False])


class PromptModeTests(unittest.TestCase):
    def test_success_with_json_mode(self):
        client = ScriptedClient([response('{"decision": "SHORT_PAY", "amount": "$100.00", "reason": "r"}')])
        result = run_case(CASE, context(client, mode="prompt", json_mode=True), 8)
        self.assertEqual((result["decision"], result["amount_cents"], result["checks"]), ("SHORT_PAY", 10000, None))
        payload = client.payloads[0]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", payload)
        self.assertIn("Payment history:", payload["messages"][1]["content"])

    def test_nudge_then_success(self):
        client = ScriptedClient([response("no idea"), response('{"decision": "HOLD", "reason": "r"}')])
        result = run_case(CASE, context(client, mode="prompt"), 8)
        self.assertEqual(result["decision"], "HOLD")
        self.assertEqual(client.payloads[1]["messages"][-1], {"role": "user", "content": NUDGE_PROMPT})

    def test_invalid_and_missing(self):
        client = ScriptedClient([response('{"decision": "WHAT"}')])
        self.assertEqual(run_case(CASE, context(client, mode="prompt"), 8)["unusable"], "bad_decision")
        client = ScriptedClient([response("hmm"), response("still hmm")])
        self.assertEqual(run_case(CASE, context(client, mode="prompt"), 8)["unusable"], "no_decision")


class CacheAndBudgetTests(unittest.TestCase):
    def test_second_run_comes_from_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResponseCache(Path(tmp) / "c.sqlite")
            budget = Budget(100.0)
            final = [call("submit_decision", {"decision": "PAY", "reason": "ok"}, "c1")]
            first = run_case(CASE, context(ScriptedClient(lookups_then(final)), budget=budget, cache=cache), 8)
            spent = budget.spent
            second = run_case(CASE, context(ScriptedClient([]), budget=budget, cache=cache), 8)
            cache.close()
        self.assertEqual(second["cached_calls"], 3)
        self.assertEqual(first["decision"], second["decision"])
        self.assertAlmostEqual(first["cost_usd"], second["cost_usd"])
        self.assertAlmostEqual(budget.spent, spent)

    def test_budget_and_provider_errors_release_reservations(self):
        budget = Budget(0.000001)
        with self.assertRaises(BudgetExceeded):
            run_case(CASE, context(ScriptedClient([]), budget=budget), 8)
        self.assertEqual(budget.reserved, 0.0)
        budget = Budget(100.0)
        with self.assertRaises(ProviderError):
            run_case(CASE, context(ScriptedClient([ProviderError("down", retryable=True)]), budget=budget), 8)
        self.assertEqual(budget.reserved, 0.0)
        self.assertEqual(budget.spent, 0.0)


if __name__ == "__main__":
    unittest.main()
