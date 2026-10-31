import json
import tempfile
import unittest
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path

from probie import generate
from probie.config import ModelSpec, VerdictSettings
from probie.report import PLACEHOLDER, write_report
from probie.score import ABOVE, NOT_PROVEN, WITHIN, classify, score
from probie.tools import required_checks

META, CASES = generate.generate()
SETTINGS = VerdictSettings()
USAGE = {"input": 2000, "cached_input": 0, "output": 200, "reasoning": 0}


def perfect(case, **overrides):
    result = {"case_id": case["id"], "status": "decided", "decision": case["answer"]["decision"],
              "amount_cents": case["answer"]["amount_cents"], "rule": case["answer"]["rule"], "reason": "r",
              "checks": list(required_checks(case)), "calls": 3, "cached_calls": 0, "usage": USAGE,
              "latency_s": 1.5, "steps": []}
    result.update(overrides)
    return result


def spec(name, price=1.0, **extra):
    return ModelSpec(name=name, model=name, base_url="https://example.test/v1", price_input=price, price_output=price, **extra)


class ClassifyTests(unittest.TestCase):
    def test_outcomes(self):
        hold = next(c for c in CASES if c["answer"]["decision"] == "HOLD")
        short = next(c for c in CASES if c["answer"]["decision"] == "SHORT_PAY")
        self.assertEqual(classify(hold, perfect(hold))["outcome"], "correct")
        paid = classify(hold, perfect(hold, decision="PAY", amount_cents=hold["invoice"]["total_cents"]))
        self.assertEqual((paid["outcome"], paid["delta_cents"]), ("unsafe", hold["invoice"]["total_cents"]))
        less = classify(short, perfect(short, decision="HOLD", amount_cents=0))
        self.assertEqual(less["outcome"], "cautious")
        off_by_a_cent = classify(short, perfect(short, amount_cents=short["answer"]["amount_cents"] + 1))
        self.assertEqual(off_by_a_cent["outcome"], "unsafe")
        self.assertEqual(classify(hold, {"status": "unusable"})["outcome"], "unusable")
        self.assertIsNone(classify(hold, {"status": "error"})["outcome"])

    def test_unverified_payments(self):
        pay = next(c for c in CASES if c["answer"]["decision"] == "PAY" and c["invoice"]["po_number"])
        missing = classify(pay, perfect(pay, checks=["vendor", "po", "receipt"]))
        self.assertEqual((missing["unverified"], missing["missing_checks"]), (True, ["history"]))
        self.assertFalse(classify(pay, perfect(pay, checks=None))["unverified"])
        hold = next(c for c in CASES if c["answer"]["decision"] == "HOLD")
        self.assertFalse(classify(hold, perfect(hold, checks=[]))["unverified"])


class ScoreTests(unittest.TestCase):
    def scored(self, results, specs, sessions=None, cases=CASES, meta=META):
        return score(meta, cases, "sha", specs, results, sessions or {}, SETTINGS)

    def test_perfect_model_is_within_everywhere(self):
        out = self.scored({"a": {c["id"]: perfect(c) for c in CASES}}, [spec("a")])
        model = out["models"][0]
        self.assertTrue(all(cell["verdict"] == WITHIN for cell in model["cells"].values()))
        self.assertEqual(model["overall"]["traps_caught"], 40)
        self.assertEqual(model["overall"]["median_latency_s"], 1.5)
        self.assertAlmostEqual(model["overall"]["cost_per_decision"], 2200 / 1e6)

    def test_one_overpayment_is_above(self):
        results = {c["id"]: perfect(c) for c in CASES}
        victim = next(c for c in CASES if c["type"] == "bank_change" and c["answer"]["decision"] == "HOLD")
        results[victim["id"]] = perfect(victim, decision="PAY", amount_cents=victim["invoice"]["total_cents"])
        cells = self.scored({"a": results}, [spec("a")])["models"][0]["cells"]
        self.assertEqual(cells["bank_change"]["verdict"], ABOVE)
        self.assertIn("1 decision released more money", cells["bank_change"]["verdict_reason"])
        self.assertEqual(cells["possible_duplicate"]["verdict"], WITHIN)

    def test_unchecked_payment_is_above(self):
        results = {c["id"]: perfect(c) for c in CASES}
        pay = next(c for c in CASES if c["type"] == "no_po" and c["answer"]["decision"] == "PAY")
        results[pay["id"]] = perfect(pay, checks=["vendor"])
        cells = self.scored({"a": results}, [spec("a")])["models"][0]["cells"]
        self.assertEqual(cells["no_po"]["verdict"], ABOVE)
        self.assertIn("without the checks", cells["no_po"]["verdict_reason"])

    def test_cautious_errors_can_leave_it_not_proven_or_above(self):
        results = {c["id"]: perfect(c) for c in CASES}
        pays = [c for c in CASES if c["type"] == "price_variance" and c["answer"]["decision"] != "HOLD"]
        for c in pays[:4]:
            results[c["id"]] = perfect(c, decision="HOLD", amount_cents=0)
        cells = self.scored({"a": results}, [spec("a")])["models"][0]["cells"]
        self.assertEqual(cells["price_variance"]["verdict"], NOT_PROVEN)
        for c in pays[4:16]:
            results[c["id"]] = perfect(c, decision="HOLD", amount_cents=0)
        cells = self.scored({"a": results}, [spec("a")])["models"][0]["cells"]
        self.assertEqual(cells["price_variance"]["verdict"], ABOVE)

    def test_fifty_clean_cases_are_not_enough(self):
        meta, cases = generate.generate(per_type=50, seed=2)
        out = self.scored({"a": {c["id"]: perfect(c) for c in cases}}, [spec("a")], cases=cases, meta=meta)
        cell = out["models"][0]["cells"]["bank_change"]
        self.assertEqual(cell["verdict"], NOT_PROVEN)
        self.assertIn("could still be 7.1%", cell["verdict_reason"])

    def test_ladder_picks_the_cheapest_within(self):
        results = {name: {c["id"]: perfect(c) for c in CASES} for name in ("dear", "cheap", "free")}
        specs = [spec("dear", 10.0), spec("cheap", 0.5), ModelSpec(name="free", model="free", base_url="https://e.test/v1")]
        out = self.scored(results, specs)
        self.assertEqual([m["name"] for m in out["models"]], ["dear", "cheap", "free"])
        self.assertTrue(all(pick["model"] == "cheap" for pick in out["ladder"].values()))

    def test_nobody_within_means_keep_with_people(self):
        results = {c["id"]: perfect(c, decision="PAY", amount_cents=c["invoice"]["total_cents"]) for c in CASES}
        out = self.scored({"a": results}, [spec("a")])
        self.assertTrue(all(pick["model"] is None for pick in out["ladder"].values()))

    def test_hourly_cost_is_spread_over_decisions(self):
        hourly = ModelSpec(name="h", model="h", base_url="https://e.test/v1", price_per_hour=3.6)
        results = {"h": {c["id"]: perfect(c) for c in CASES}}
        out = self.scored(results, [hourly], sessions={"h": [{"elapsed_s": 600}, {"elapsed_s": 400}]})
        self.assertAlmostEqual(out["models"][0]["overall"]["cost_usd"], 1.0)

    def test_errors_and_strangers_are_not_scored(self):
        results = {c["id"]: perfect(c) for c in CASES[:10]}
        results[CASES[10]["id"]] = {"case_id": CASES[10]["id"], "status": "error", "detail": "503"}
        results["KRF-9999"] = perfect(CASES[0], case_id="KRF-9999")
        model = self.scored({"a": results}, [spec("a")])["models"][0]
        self.assertEqual((model["coverage"], model["errors"]), (10, 1))


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.scripts = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("href", "").startswith("http"):
            self.links.append(attrs["href"])
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])


class ReportTests(unittest.TestCase):
    def test_template_has_one_placeholder(self):
        template = resources.files("probie").joinpath("report_template.html").read_text(encoding="utf-8")
        self.assertEqual(template.count(PLACEHOLDER), 1)

    def test_writes_html_and_json_safely(self):
        cases = json.loads(json.dumps(CASES))
        cases[0]["message"] = "</script><script>alert(1)</script> & <b>bold</b>"
        results = {"a": {c["id"]: perfect(c) for c in cases}}
        out = score(META, cases, "sha", [spec("a")], results, {}, SETTINGS)
        with tempfile.TemporaryDirectory() as tmp:
            html_path, json_path = Path(tmp) / "r.html", Path(tmp) / "r.json"
            write_report(html_path, json_path, out, META, cases, results)
            html = html_path.read_text(encoding="utf-8")
            summary = json.loads(json_path.read_text(encoding="utf-8"))
        template = resources.files("probie").joinpath("report_template.html").read_text(encoding="utf-8")
        self.assertNotIn(PLACEHOLDER, html)
        self.assertEqual(html.count("</script>"), template.count("</script>"))
        self.assertNotIn("<script>alert", html)
        self.assertIn("\\u003c/script\\u003e", html)
        self.assertNotIn("outcomes", summary)
        self.assertEqual(summary["models"][0]["name"], "a")
        collector = _Collector()
        collector.feed(html)
        self.assertEqual(collector.scripts, [])
        self.assertTrue(collector.links)
        self.assertTrue(all(link.startswith(("https://fonts.googleapis.com", "https://fonts.gstatic.com"))
                            for link in collector.links), collector.links)


if __name__ == "__main__":
    unittest.main()
