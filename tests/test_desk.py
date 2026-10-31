import random
import unittest

from probie import generate
from probie.config import ModelSpec, VerdictSettings
from probie.desk import ALPHA, confidence_sequence, log_mixture, replay
from probie.stats import wilson
from probie.tools import required_checks

META, CASES = generate.generate(per_type=200, seed=7)
SETTINGS = VerdictSettings()
USAGE = {"input": 1000, "cached_input": 0, "output": 100, "reasoning": 0}


def perfect(case, **overrides):
    result = {"case_id": case["id"], "status": "decided", "decision": case["answer"]["decision"],
              "amount_cents": case["answer"]["amount_cents"], "checks": list(required_checks(case)),
              "calls": 3, "usage": USAGE}
    result.update(overrides)
    return result


def spec(name, price):
    return ModelSpec(name=name, model=name, base_url="https://e.test/v1", price_input=price, price_output=price)


class ConfidenceSequenceTests(unittest.TestCase):
    def test_clean_record_needs_158_decisions(self):
        self.assertGreater(confidence_sequence(0, 157)[1], 0.05)
        self.assertLessEqual(confidence_sequence(0, 158)[1], 0.05)
        self.assertGreater(confidence_sequence(158, 158)[0], 0.95)

    def test_wider_than_a_single_check(self):
        for k, n in ((0, 80), (5, 100), (90, 100), (50, 50)):
            low, high = confidence_sequence(k, n)
            w_low, w_high = wilson(k, n)
            self.assertLessEqual(low, w_low + 1e-9)
            self.assertGreaterEqual(high, w_high - 1e-9)
            self.assertTrue(low <= k / n <= high)

    def test_edges(self):
        self.assertEqual(confidence_sequence(0, 0), (0.0, 1.0))
        self.assertEqual(confidence_sequence(0, 10)[0], 0.0)
        self.assertEqual(confidence_sequence(10, 10)[1], 1.0)
        with self.assertRaises(ValueError):
            confidence_sequence(3, 2)

    def test_valid_when_checked_after_every_decision(self):
        # The point of a confidence sequence: look after every decision and the chance of ever
        # wrongly ruling out the true rate stays under 5%. A Wilson interval checked the same way doesn't.
        rng = random.Random(11)
        true_rate, sequences, steps = 0.05, 400, 250
        threshold = __import__("math").log(1 / ALPHA)
        ever_cs = ever_wilson = 0
        for _ in range(sequences):
            hits = 0
            broke_cs = broke_wilson = False
            for t in range(1, steps + 1):
                hits += rng.random() < true_rate
                if not broke_cs and log_mixture(hits, t, true_rate) >= threshold:
                    broke_cs = True
                if not broke_wilson:
                    low, high = wilson(hits, t)
                    broke_wilson = not (low <= true_rate <= high)
            ever_cs += broke_cs
            ever_wilson += broke_wilson
        self.assertLessEqual(ever_cs / sequences, 0.085)
        self.assertGreater(ever_wilson / sequences, 0.15)


class ReplayTests(unittest.TestCase):
    def test_perfect_model_is_cleared_at_its_158th_decision_per_duty(self):
        month = replay(CASES, [spec("a", 1.0)], {"a": {c["id"]: perfect(c) for c in CASES}}, SETTINGS)
        promotions = [e for e in month["events"] if e["kind"] == "promoted"]
        self.assertEqual(sorted(e["duty"] for e in promotions), sorted(generate.TYPES))
        self.assertTrue(all(e["after"] == 158 for e in promotions))
        self.assertEqual(month["summary"]["to_ai"], 6 * (200 - 158))
        self.assertEqual(month["summary"]["desk_overpaid_cents"], 0)
        self.assertTrue(all(cell["cleared"] for cell in month["final"]["a"].values()))

    def test_an_overpayment_demotes_and_resets(self):
        order = sorted(CASES, key=lambda c: (c["invoice"]["date"], c["id"]))
        duty_cases = [c for c in order if c["type"] == "bank_change"]
        victim = next(c for c in duty_cases[170:] if c["answer"]["decision"] == "HOLD")
        results = {c["id"]: perfect(c) for c in CASES}
        results[victim["id"]] = perfect(victim, decision="PAY", amount_cents=victim["invoice"]["total_cents"])
        month = replay(CASES, [spec("a", 1.0)], {"a": results}, SETTINGS)
        demotions = [e for e in month["events"] if e["kind"] == "demoted"]
        self.assertEqual(len(demotions), 1)
        self.assertEqual((demotions[0]["case_id"], demotions[0]["duty"]), (victim["id"], "bank_change"))
        cell = month["final"]["a"]["bank_change"]
        self.assertFalse(cell["cleared"])
        self.assertEqual(cell["strikes"], 1)
        self.assertLess(cell["n"], 158)
        # The desk had cleared this model, so the overpayment went through. That's the risk it measures.
        self.assertEqual(month["summary"]["desk_overpayments"], 1)

    def test_unchecked_payment_counts_as_a_strike(self):
        pay = next(c for c in CASES if c["type"] == "no_po" and c["answer"]["decision"] == "PAY")
        results = {c["id"]: perfect(c) for c in CASES}
        results[pay["id"]] = perfect(pay, checks=["vendor"])
        month = replay(CASES, [spec("a", 1.0)], {"a": results}, SETTINGS)
        self.assertEqual(month["final"]["a"]["no_po"]["strikes"], 1)

    def test_routing_prefers_the_cheapest_cleared_model_and_beats_the_baseline(self):
        cheap_bad = {c["id"]: (perfect(c, decision="PAY", amount_cents=c["invoice"]["total_cents"])
                               if c["trap"] else perfect(c)) for c in CASES}
        results = {"dear": {c["id"]: perfect(c) for c in CASES},
                   "mid": {c["id"]: perfect(c) for c in CASES},
                   "cheap": cheap_bad}
        specs = [spec("dear", 10.0), spec("mid", 2.0), spec("cheap", 0.5)]
        month = replay(CASES, specs, results, SETTINGS)
        routed = {e["to"] for e in month["routing"] if e["to"]}
        self.assertIn("mid", routed)
        self.assertNotIn("dear", routed)
        self.assertEqual(month["baseline_model"], "cheap")
        s = month["summary"]
        self.assertGreater(s["baseline_overpayments"], 0)
        self.assertLess(s["desk_overpaid_cents"], s["baseline_overpaid_cents"])
        self.assertEqual(s["to_ai"] + s["to_person"], 1200)
        self.assertEqual(sum(d["invoices"] for d in month["days"]), 1200)

    def test_models_without_results_are_left_out(self):
        month = replay(CASES[:50], [spec("a", 1.0), spec("ghost", 1.0)],
                       {"a": {c["id"]: perfect(c) for c in CASES[:50]}}, SETTINGS)
        self.assertEqual(month["models"], ["a"])
        self.assertEqual(month["summary"]["to_person"], 50)


if __name__ == "__main__":
    unittest.main()


class ConsistencyTests(unittest.TestCase):
    def test_a_different_second_answer_is_a_strike(self):
        results = {c["id"]: perfect(c) for c in CASES}
        order = sorted(CASES, key=lambda c: (c["invoice"]["date"], c["id"]))
        target = next(c for c in order if c["type"] == "short_shipment" and c["answer"]["decision"] == "SHORT_PAY")
        repeats = {c["id"]: perfect(c) for c in CASES}
        repeats[target["id"]] = perfect(target, decision="HOLD", amount_cents=0)
        month = replay(CASES, [spec("a", 1.0)], {"a": results}, SETTINGS, repeats={"a": repeats})
        self.assertEqual(month["final"]["a"]["short_shipment"]["strikes"], 1)
        steady = replay(CASES, [spec("a", 1.0)], {"a": results}, SETTINGS, repeats={"a": {c["id"]: perfect(c) for c in CASES}})
        self.assertEqual(steady["final"]["a"]["short_shipment"]["strikes"], 0)

    def test_second_pass_writes_its_own_file_and_skips_the_cache(self):
        import tempfile
        from pathlib import Path
        from probie.config import config_from_dict
        from probie.runner import read_results, results_path, run_model
        meta, small = generate.generate(per_type=2, seed=4)
        with tempfile.TemporaryDirectory() as tmp:
            config = config_from_dict({"run": {"budget_usd": 10.0}, "model": [{
                "name": "s", "model": "offline-careful", "provider": "offline", "offline_profile": "careful",
                "price_input": 1.0, "price_output": 1.0}]}, base_dir=Path(tmp))
            first = run_model(config, config.model("s"), small, "sha", out=lambda line: None)
            second = run_model(config, config.model("s"), small, "sha", repeat=2, out=lambda line: None)
            self.assertEqual(first.completed, second.completed)
            self.assertGreater(second.spent_usd, 0)
            self.assertTrue(results_path(config, config.model("s"), 2).name.endswith("s@2.jsonl"))
            self.assertEqual(len(read_results(results_path(config, config.model("s"), 2)).results), len(small))
