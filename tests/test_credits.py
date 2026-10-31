import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from probie import generate, ledger
from probie.cli import main
from probie.config import ConfigError, ModelSpec, config_from_dict
from probie.runner import RunError, run_model

META, CASES = generate.generate(per_type=4, seed=5)


class PoolTests(unittest.TestCase):
    def test_pools_come_from_the_host(self):
        def pool(url, **extra):
            return ModelSpec(name="m", model="m", base_url=url, **extra).pool()
        self.assertEqual(pool("https://api.meta.ai/v1"), "meta")
        self.assertEqual(pool("https://inference.baseten.co/v1"), "baseten")
        self.assertEqual(pool("https://api.fireworks.ai/inference/v1"), "fireworks")
        self.assertEqual(pool("https://me--glimmer-serve.modal.run/v1"), "modal")
        self.assertIsNone(pool("https://example.test/v1"))
        self.assertIsNone(pool("https://notmeta.ai.evil.test/v1"))
        self.assertEqual(pool("https://example.test/v1", credit_pool="meta"), "meta")
        self.assertIsNone(ModelSpec(name="o", model="o", provider="offline", offline_profile="careful").pool())

    def test_credits_config(self):
        config = config_from_dict({"credits": {"meta": 150, "baseten": 100, "margin": 0.1}})
        self.assertEqual(config.credits, {"meta": 150.0, "baseten": 100.0})
        self.assertEqual(config.credit_margin, 0.1)
        for bad in ({"meta": -1}, {"margin": 0.6}, {"bad name": 5}, {"meta": "lots"}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                config_from_dict({"credits": bad})


class LedgerTests(unittest.TestCase):
    def test_record_and_sum(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger.record(tmp, pool="meta", model="a", usd=1.5, what="run")
            ledger.record(tmp, pool="meta", model="b", usd=0.25, what="ping")
            ledger.record(tmp, pool="fireworks", model="c", usd=2, what="run")
            ledger.record(tmp, pool="meta", model="a", usd=0, what="run")
            ledger.record(tmp, pool=None, model="d", usd=9, what="run")
            with (Path(tmp) / ledger.LEDGER).open("a") as f:
                f.write("{damaged\n")
            self.assertEqual(ledger.spent(tmp), {"meta": 1.75, "fireworks": 2.0})
            left = ledger.usable({"meta": 150.0, "modal": 100.0}, 0.05, tmp)
            self.assertAlmostEqual(left["meta"], 142.5 - 1.75)
            self.assertAlmostEqual(left["modal"], 95.0)


class CreditGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = config_from_dict({
            "run": {"budget_usd": 50.0},
            "credits": {"meta": 0.05, "margin": 0.0},
            "model": [{"name": "stand-in", "model": "offline-careful", "provider": "offline",
                       "offline_profile": "careful", "credit_pool": "meta", "price_input": 1.0, "price_output": 2.0}],
        }, base_dir=Path(self.tmp.name))
        self.spec = self.config.model("stand-in")
        self.lines = []

    def run_it(self, **kwargs):
        return run_model(self.config, self.spec, CASES, "sha", out=self.lines.append, concurrency=1, **kwargs)

    def test_run_is_capped_at_the_credit_left_and_recorded(self):
        summary = self.run_it()
        self.assertTrue(any("what's left of the meta credits" in line for line in self.lines))
        self.assertIn("spend cap", summary.stopped)
        self.assertLessEqual(summary.spent_usd, 0.05)
        self.assertAlmostEqual(ledger.spent(self.config.run.runs_dir)["meta"], summary.spent_usd, places=5)

    def test_used_up_credits_stop_the_run_unless_allowed(self):
        ledger.record(self.config.run.runs_dir, pool="meta", model="x", usd=0.05, what="run")
        with self.assertRaises(RunError) as ctx:
            self.run_it()
        self.assertIn("--past-credits", str(ctx.exception))
        summary = self.run_it(past_credits=True)
        self.assertEqual(summary.completed, 24)


class CreditsCommandTests(unittest.TestCase):
    def test_reports_left_and_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "probie.toml"
            config.write_text("""
[run]
cases = "cases.jsonl"
[credits]
meta = 150
[[model]]
name = "spark"
model = "muse-spark-1.3"
base_url = "https://api.meta.ai/v1"
api_key_env = "PROBIE_TEST_UNSET"
price_input = 1.25
price_output = 4.25
""")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--config", str(config), "generate", "--per-type", "2"]), 0)
                self.assertEqual(main(["--config", str(config), "credits"]), 0)
            text = out.getvalue()
            self.assertIn("meta: $0.00 spent of $150.00; $142.50 left to use", text)
            self.assertIn("spark: about $", text)
            self.assertIn("rough estimate", text)


if __name__ == "__main__":
    unittest.main()


class TotalLimitTests(unittest.TestCase):
    def test_total_limit_caps_every_pool_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_from_dict({
                "run": {"budget_usd": 50.0},
                "credits": {"total": 0.05, "meta": 100, "fireworks": 100, "margin": 0.0},
                "model": [{"name": "stand-in", "model": "offline-careful", "provider": "offline",
                           "offline_profile": "careful", "credit_pool": "fireworks", "price_input": 1.0, "price_output": 2.0}],
            }, base_dir=Path(tmp))
            self.assertEqual(config.credit_total, 0.05)
            spec = config.model("stand-in")
            spec.provider = "openai-compatible"  # treat it as paid for the ledger, but keep the offline client
            from probie.offline import OfflineClient
            lines = []
            ledger.record(config.run.runs_dir, pool="meta", model="x", usd=0.03, what="run")
            summary = run_model(config, spec, CASES, "sha", out=lines.append, concurrency=1,
                                client=OfflineClient(CASES, "careful"))
            self.assertTrue(any("limit on all API spend" in line for line in lines))
            self.assertLessEqual(summary.spent_usd, 0.02 + 1e-9)
            ledger.record(config.run.runs_dir, pool="other", model="y", usd=1.0, what="run")
            with self.assertRaises(RunError) as ctx:
                run_model(config, spec, CASES, "sha", out=lines.append, client=OfflineClient(CASES, "careful"))
            self.assertIn("limit on all API spend is reached", str(ctx.exception))

    def test_unknown_hosts_book_to_other(self):
        spec = ModelSpec(name="m", model="m", base_url="https://openrouter.ai/api/v1")
        self.assertEqual(ledger.pool_for(spec), "other")
        self.assertIsNone(ledger.pool_for(ModelSpec(name="o", model="o", provider="offline", offline_profile="quick")))

    def test_repo_config_has_the_149_limit(self):
        from probie.config import load_config
        config = load_config(Path(__file__).resolve().parent.parent / "probie.toml")
        self.assertEqual((config.credit_total, config.credits["meta"]), (149.0, 149.0))
