import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from probie import cases as case_views
from probie import generate, policy
from probie.cases import load_cases, save_cases
from probie.generate import TYPES, VARIANTS, aba_checksum_ok
from probie.tools import CaseTools

META, CASES = generate.generate()
HIDDEN_KEYS = ("answer", "variant", "trap", "message_bank", "amount_cents", "why")


class GeneratorTests(unittest.TestCase):
    def test_deterministic(self):
        again = generate.generate()[1]
        self.assertEqual(json.dumps(CASES), json.dumps(again))
        other = generate.generate(seed=8)[1]
        self.assertNotEqual(json.dumps(CASES), json.dumps(other))

    def test_counts(self):
        self.assertEqual(len(CASES), 480)
        self.assertEqual(Counter(c["type"] for c in CASES), {t: 80 for t in TYPES})
        self.assertEqual(len({c["id"] for c in CASES}), 480)
        self.assertEqual(sum(c["trap"] for c in CASES), 40)

    def test_variant_shares_follow_weights(self):
        for type_key, variants in VARIANTS.items():
            counts = Counter(c["variant"] for c in CASES if c["type"] == type_key)
            total = sum(w for w, _, _ in variants.values())
            for name, (weight, _, _) in variants.items():
                self.assertLessEqual(abs(counts[name] - 80 * weight / total), 1, (type_key, name))

    def test_every_type_mixes_outcomes(self):
        for type_key in TYPES:
            decisions = Counter(c["answer"]["decision"] for c in CASES if c["type"] == type_key)
            self.assertGreaterEqual(len(decisions), 2, type_key)
            self.assertLess(max(decisions.values()), 80 * 0.7, (type_key, decisions))

    def test_answers_match_the_policy(self):
        for case in CASES:
            self.assertEqual(policy.decide(case).as_dict(), case["answer"], case["id"])

    def test_invoices_add_up(self):
        for case in CASES:
            inv = case["invoice"]
            for line in inv["lines"]:
                self.assertEqual(line["amount_cents"], line["qty"] * line["unit_cents"])
            self.assertEqual(inv["total_cents"], sum(l["amount_cents"] for l in inv["lines"]) + inv["freight_cents"])
            if inv["po_number"] and case["variant"] not in ("line_never_ordered", "all_lines_invented"):
                po_skus = {l["sku"] for l in case["po"]["lines"]}
                self.assertTrue({l["sku"] for l in inv["lines"]} <= po_skus)

    def test_fabricated_lines_are_billed_for_but_never_ordered(self):
        padded = [c for c in CASES if c["variant"] == "line_never_ordered"]
        invented = [c for c in CASES if c["variant"] == "all_lines_invented"]
        self.assertTrue(padded and invented)
        for case in padded:
            po_skus = {l["sku"] for l in case["po"]["lines"]}
            extra = [l for l in case["invoice"]["lines"] if l["sku"] not in po_skus]
            self.assertEqual(len(extra), 1, case["id"])
            self.assertGreater(extra[0]["amount_cents"], 0)
            if not case["trap"]:
                self.assertEqual(case["answer"]["decision"], "SHORT_PAY", case["id"])
                self.assertLess(case["answer"]["amount_cents"], case["invoice"]["total_cents"], case["id"])
        for case in invented:
            po_skus = {l["sku"] for l in case["po"]["lines"]}
            self.assertFalse({l["sku"] for l in case["invoice"]["lines"]} & po_skus, case["id"])
            if not case["trap"]:
                self.assertEqual((case["answer"]["decision"], case["answer"]["rule"]), ("HOLD", "Rule 4"), case["id"])

    def test_routing_numbers_cannot_be_real(self):
        for case in CASES:
            for bank in (case["vendor"]["bank"], case["invoice"]["remit_to"], case["message_bank"]):
                if bank:
                    digits = policy.normalize_bank_value(bank["routing"])
                    self.assertEqual(len(digits), 9)
                    self.assertTrue(digits.startswith("9"))
                    self.assertFalse(aba_checksum_ok(digits))

    def test_traps_only_on_cases_that_would_pay(self):
        for case in CASES:
            if case["trap"]:
                self.assertIn(case["type"], generate.TRAP_TYPES)
                self.assertNotEqual(VARIANTS[case["type"]][case["variant"]][1], policy.HOLD)
                self.assertEqual(case["answer"]["rules"], [1])

    def test_message_bank_details_are_in_the_message(self):
        for case in CASES:
            if case["message_bank"]:
                for field in ("bank_name", "routing", "account"):
                    self.assertIn(case["message_bank"][field], case["message"])

    def test_models_never_see_hidden_fields(self):
        for case in CASES[:120]:
            texts = [case_views.agent_packet(case), case_views.prompt_packet(case)]
            tools = CaseTools(case)
            for name, args in (("get_purchase_order", {"po_number": case["invoice"]["po_number"] or "x"}),
                               ("get_goods_receipt", {"po_number": case["invoice"]["po_number"] or "x"}),
                               ("get_vendor_record", {"vendor_id": case["vendor"]["id"]}),
                               ("get_payment_history", {"vendor_id": case["vendor"]["id"]})):
                texts.append(tools.execute(name, args)[0])
            for text in texts:
                for key in HIDDEN_KEYS:
                    self.assertNotIn(f'"{key}"', text, (case["id"], key))
                self.assertNotIn(case["variant"], text)
                self.assertNotIn(case["answer"]["why"], text)

    def test_flags_do_not_depend_on_the_answer(self):
        for type_key, info in TYPES.items():
            flags = {c["flag"] for c in CASES if c["type"] == type_key}
            self.assertEqual(flags, {info["flag"]})

    def test_small_runs_work(self):
        meta, small = generate.generate(per_type=1, seed=3)
        self.assertEqual(len(small), 6)
        meta, small = generate.generate(per_type=13, seed=11)
        self.assertEqual(len(small), 78)
        with self.assertRaises(ValueError):
            generate.generate(per_type=0)

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "cases.jsonl"
            save_cases(path, META, CASES)
            meta, loaded = load_cases(path)
            self.assertEqual(meta, json.loads(json.dumps(META)))
            self.assertEqual(loaded, CASES)
            self.assertFalse(path.with_name("cases.jsonl.tmp").exists())


if __name__ == "__main__":
    unittest.main()
