import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from probie.budget import Budget, BudgetExceeded, Prices, Usage, extract_usage, upper_bound_tokens
from probie.cache import ResponseCache, request_key
from probie.config import ConfigError, config_from_dict, load_config, load_dotenv

REPO = Path(__file__).resolve().parent.parent


class UsageTests(unittest.TestCase):
    def test_openai_shape(self):
        u = extract_usage({"usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                                     "prompt_tokens_details": {"cached_tokens": 600},
                                     "completion_tokens_details": {"reasoning_tokens": 150}}})
        self.assertEqual(u, Usage(1000, 600, 200, 150))

    def test_other_shapes(self):
        self.assertEqual(extract_usage({"usage": {"input_tokens": 10, "output_tokens": 3}}), Usage(10, 0, 3, 0))
        self.assertEqual(extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 3, "cached_tokens": 4}}).cached_input_tokens, 4)
        self.assertEqual(extract_usage({"usage": {"prompt_tokens": 10, "prompt_cache_hit_tokens": 7}}).cached_input_tokens, 7)

    def test_garbage_is_zero_and_cache_is_capped(self):
        self.assertEqual(extract_usage({}), Usage())
        self.assertEqual(extract_usage({"usage": "lots"}), Usage())
        u = extract_usage({"usage": {"prompt_tokens": True, "completion_tokens": -5, "cached_tokens": "9"}})
        self.assertEqual(u, Usage())
        self.assertEqual(extract_usage({"usage": {"prompt_tokens": 5, "cached_tokens": 50}}).cached_input_tokens, 5)

    def test_round_trip(self):
        u = Usage(1, 2, 3, 4)
        self.assertEqual(Usage.from_dict(u.as_dict()), u)
        self.assertEqual(u + u, Usage(2, 4, 6, 8))


class PriceTests(unittest.TestCase):
    def test_cost(self):
        prices = Prices(input=4.0, cached_input=0.2, output=20.0)
        self.assertAlmostEqual(prices.cost(Usage(1_000_000, 400_000, 100_000)), 0.6 * 4 + 0.4 * 0.2 + 0.1 * 20)
        no_cache_rate = Prices(input=1.0, output=2.0)
        self.assertAlmostEqual(no_cache_rate.cost(Usage(1_000_000, 500_000, 0)), 1.0)

    def test_unpriced_and_hourly_calls_cost_nothing(self):
        self.assertEqual(Prices().cost(Usage(10**6, 0, 10**6)), 0.0)
        self.assertFalse(Prices(input=1.0).known)
        hourly = Prices(per_hour=3.0)
        self.assertTrue(hourly.known)
        self.assertEqual(hourly.cost(Usage(10**6, 0, 10**6)), 0.0)
        self.assertEqual(hourly.worst_case({"messages": []}, 100), 0.0)

    def test_worst_case_overestimates(self):
        payload = {"messages": [{"role": "user", "content": "x" * 4000}], "tools": [], "model": "m"}
        self.assertGreaterEqual(upper_bound_tokens(payload), 2000)
        prices = Prices(input=1.0, output=1.0)
        self.assertAlmostEqual(prices.worst_case(payload, 1000), (upper_bound_tokens(payload) + 1000) / 1e6)


class BudgetTests(unittest.TestCase):
    def test_reserve_settle_release(self):
        b = Budget(1.0)
        t = b.reserve(0.6)
        self.assertAlmostEqual(b.reserved, 0.6)
        with self.assertRaises(BudgetExceeded):
            b.reserve(0.5)
        b.settle(t, 0.1)
        self.assertAlmostEqual(b.spent, 0.1)
        self.assertEqual(b.reserved, 0.0)
        t2 = b.reserve(0.9)
        b.release(t2)
        self.assertAlmostEqual(b.spent, 0.1)
        self.assertEqual(b.reserved, 0.0)

    def test_never_exceeds_cap_across_threads(self):
        b = Budget(1.0)
        errors = []

        def worker():
            for _ in range(200):
                try:
                    token = b.reserve(0.01)
                except BudgetExceeded:
                    return
                b.settle(token, 0.01)
                errors.append(b.spent > 1.0 + 1e-9)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(any(errors))
        self.assertLessEqual(b.spent, 1.0 + 1e-9)
        self.assertGreater(b.spent, 0.98)

    def test_hourly_budget_uses_the_clock(self):
        now = [0.0]
        b = Budget(5.0, per_hour=10.0, clock=lambda: now[0])
        now[0] = 1800.0
        self.assertAlmostEqual(b.spent, 5.0)
        with self.assertRaises(BudgetExceeded):
            b.reserve(0.0)

    def test_cap_must_be_positive(self):
        with self.assertRaises(ValueError):
            Budget(0)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = ResponseCache(Path(self.dir.name) / "deep" / "cache.sqlite")
        self.addCleanup(self.cache.close)

    def test_key_is_stable_and_scoped(self):
        a = request_key("https://x", {"b": 1, "a": [1, {"y": 2, "x": 1}]})
        b = request_key("https://x", {"a": [1, {"x": 1, "y": 2}], "b": 1})
        self.assertEqual(a, b)
        self.assertNotEqual(a, request_key("https://y", {"b": 1, "a": [1, {"y": 2, "x": 1}]}))

    def test_put_get_overwrite(self):
        self.assertIsNone(self.cache.get("k"))
        self.cache.put("k", {"v": 1})
        self.cache.put("k", {"v": 2})
        self.assertEqual(self.cache.get("k"), {"v": 2})
        self.assertEqual(len(self.cache), 1)

    def test_damaged_entry_is_a_miss(self):
        self.cache._db.execute("INSERT INTO responses (key, response) VALUES ('bad', '{oops')")
        self.assertIsNone(self.cache.get("bad"))

    def test_threads(self):
        def worker(n):
            for i in range(50):
                self.cache.put(f"{n}-{i}", {"n": n, "i": i})
                self.assertEqual(self.cache.get(f"{n}-{i}"), {"n": n, "i": i})
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.cache), 300)


def model(**extra):
    data = {"name": "m1", "model": "muse-spark-1.3", "base_url": "https://api.meta.ai/v1",
            "api_key_env": "MODEL_API_KEY", "price_input": 1.25, "price_output": 4.25}
    data.update(extra)
    return data


class ConfigTests(unittest.TestCase):
    def test_repo_config_loads(self):
        config = load_config(REPO / "probie.toml")
        names = [m.name for m in config.models]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(m.priced() for m in config.models))
        self.assertEqual(config.run.cases, REPO / "data" / "cases.jsonl")
        dev = config.model("spark-contributor-dev")
        self.assertFalse(dev.include_in_report)

    def test_minimal_and_defaults(self):
        config = config_from_dict({"model": [model()]}, base_dir=Path("/base"))
        spec = config.model("m1")
        self.assertEqual((spec.mode, spec.max_tokens, spec.provider), ("agent", 4096, "openai-compatible"))
        self.assertEqual(config.run.runs_dir, Path("/base/runs"))
        self.assertEqual(config.verdict.min_agreement, 0.90)
        with self.assertRaises(ConfigError):
            config.model("nope")

    def test_rejects_mistakes(self):
        bad = [
            {"model": [model(price_imput=1.0)]},
            {"model": [model(), model()]},
            {"model": [model(mode="chat")]},
            {"model": [model(provider="magic")]},
            {"model": [model(base_url="api.meta.ai")]},
            {"model": [model(price_input="cheap")]},
            {"model": [model(price_input=-1)]},
            {"model": [model(max_tokens=2)]},
            {"model": [model(concurrency=0)]},
            {"model": [model(rpm=0)]},
            {"model": [model(json_mode="yes")]},
            {"model": [model(params={"messages": []})]},
            {"model": [model(params="x")]},
            {"model": [model(name="has space")]},
            {"model": [model(price_per_hour=2.0)]},
            {"model": [{"name": "off", "model": "x", "provider": "offline"}]},
            {"model": [{"model": "x"}]},
            {"model": {"name": "x"}},
            {"run": {"budget_usd": 0}},
            {"run": {"max_steps": 1}},
            {"run": {"surprise": 1}},
            {"verdict": {"min_agreement": 90}},
            {"extra": {}},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ConfigError):
                config_from_dict(data)

    def test_unknown_key_message_names_it(self):
        with self.assertRaises(ConfigError) as ctx:
            config_from_dict({"model": [model(price_imput=1.0)]})
        self.assertIn("price_imput", str(ctx.exception))

    def test_bad_toml(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[run\n")
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(ConfigError):
            load_config(f.name)
        with self.assertRaises(ConfigError):
            load_config("/nonexistent.toml")

    def test_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# comment\nPG_TEST_A=one\nexport PG_TEST_B='two words'\nPG_TEST_C=\"3\"\nPG_TEST_KEEP=new\nnot a line\n")
            with mock.patch.dict(os.environ, {"PG_TEST_KEEP": "old"}, clear=False):
                loaded = load_dotenv(path)
                self.assertEqual(os.environ["PG_TEST_A"], "one")
                self.assertEqual(os.environ["PG_TEST_B"], "two words")
                self.assertEqual(os.environ["PG_TEST_C"], "3")
                self.assertEqual(os.environ["PG_TEST_KEEP"], "old")
                self.assertEqual(sorted(loaded), ["PG_TEST_A", "PG_TEST_B", "PG_TEST_C"])
                for key in loaded:
                    del os.environ[key]
            self.assertEqual(load_dotenv(Path(tmp) / "missing.env"), [])


if __name__ == "__main__":
    unittest.main()
