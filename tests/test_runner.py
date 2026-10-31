import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from probie import generate
from probie.config import ModelSpec, config_from_dict
from probie.offline import OfflineClient
from probie.providers import ProviderError
from probie.runner import RunError, build_client, read_results, results_path, run_model

META, CASES = generate.generate(per_type=4, seed=5)
SHA = "test-cases-sha"


def make_config(tmp, **run):
    data = {"run": {"runs_dir": "runs", "cache": "runs/cache.sqlite", "budget_usd": 50.0, **run},
            "model": [{"name": "careful", "model": "offline-careful", "provider": "offline", "offline_profile": "careful",
                       "price_input": 1.0, "price_output": 2.0}]}
    return config_from_dict(data, base_dir=Path(tmp))


class Raising:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def chat(self, payload):
        self.calls += 1
        raise self.error


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name)
        self.spec = self.config.model("careful")
        self.quiet = lambda line: None

    def run_it(self, **kwargs):
        kwargs.setdefault("out", self.quiet)
        return run_model(self.config, self.spec, CASES, SHA, **kwargs)

    def test_full_run_then_resume(self):
        summary = self.run_it(concurrency=8)
        self.assertEqual((summary.todo, summary.completed, summary.decided, summary.stopped), (24, 24, 24, None))
        rf = read_results(results_path(self.config, self.spec))
        self.assertEqual(len(rf.results), 24)
        self.assertEqual(rf.header["cases_sha256"], SHA)
        self.assertEqual(len(rf.sessions), 1)
        again = self.run_it()
        self.assertEqual(again.todo, 0)

    def test_limit_then_finish(self):
        self.assertEqual(self.run_it(limit=5).completed, 5)
        self.assertEqual(self.run_it().completed, 19)
        self.assertEqual(len(read_results(results_path(self.config, self.spec)).results), 24)

    def test_changed_setup_needs_fresh(self):
        self.run_it(limit=3)
        self.spec.max_tokens = 1000
        with self.assertRaises(RunError):
            self.run_it()
        summary = self.run_it(fresh=True)
        self.assertEqual(summary.completed, 24)
        backups = list(Path(self.tmp.name, "runs").glob("careful.*.bak.jsonl"))
        self.assertEqual(len(backups), 1)

    def test_spend_cap_stops_cleanly(self):
        summary = self.run_it(budget_usd=0.03, concurrency=1)
        self.assertIn("spend cap", summary.stopped)
        self.assertGreater(summary.completed, 0)
        self.assertLess(summary.completed, 24)
        self.assertLessEqual(summary.spent_usd, 0.03)
        rf = read_results(results_path(self.config, self.spec))
        self.assertEqual(rf.sessions[-1]["stopped"], summary.stopped)

    def test_cap_too_small_for_one_call(self):
        summary = self.run_it(budget_usd=0.000001)
        self.assertEqual(summary.completed, 0)
        self.assertIn("spend cap", summary.stopped)

    def test_needs_prices(self):
        self.spec.price_input = None
        with self.assertRaises(RunError):
            self.run_it()

    def test_refused_request_stops_the_run(self):
        client = Raising(ProviderError("HTTP 400: bad parameter", status=400))
        summary = self.run_it(client=client, concurrency=1)
        self.assertEqual(summary.completed, 0)
        self.assertIn("refused", summary.stopped)
        self.assertEqual(client.calls, 1)

    def test_repeated_failures_stop_and_are_retried_later(self):
        client = Raising(ProviderError("HTTP 503 (gave up)", status=503, retryable=True))
        summary = self.run_it(client=client, concurrency=1)
        self.assertEqual(summary.errors, 5)
        self.assertIn("in a row failed", summary.stopped)
        rf = read_results(results_path(self.config, self.spec))
        self.assertEqual({r["status"] for r in rf.results.values()}, {"error"})
        self.assertEqual(self.run_it().completed, 24)

    def test_internal_errors_surface_after_a_clean_stop(self):
        with self.assertRaises(RunError):
            self.run_it(client=Raising(RuntimeError("boom")), concurrency=1)
        rf = read_results(results_path(self.config, self.spec))
        self.assertIn("internal error", rf.sessions[-1]["stopped"])

    def test_partial_last_line_is_skipped(self):
        self.run_it(limit=2)
        path = results_path(self.config, self.spec)
        with path.open("a") as f:
            f.write('{"case_id": "KRF-0003", "sta')
        self.assertEqual(len(read_results(path).results), 2)

    def test_not_a_results_file(self):
        path = Path(self.tmp.name) / "x.jsonl"
        path.write_text(json.dumps({"case_id": "KRF-0001"}) + "\n")
        with self.assertRaises(RunError):
            read_results(path)

    def test_cache_makes_reruns_free(self):
        first = self.run_it()
        second = self.run_it(fresh=True)
        self.assertGreater(first.spent_usd, 0)
        self.assertEqual(second.spent_usd, 0)

    def test_build_client(self):
        self.assertIsInstance(build_client(self.spec, CASES), OfflineClient)
        remote = ModelSpec(name="r", model="m", base_url="https://example.test/v1", api_key_env="PROBIE_TEST_NO_SUCH_KEY")
        with mock.patch.dict("os.environ", {}, clear=False):
            with self.assertRaises(RunError) as ctx:
                build_client(remote, CASES)
        self.assertIn("PROBIE_TEST_NO_SUCH_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class SubsetTests(unittest.TestCase):
    def setUp(self):
        from probie import redteam
        meta, month = generate.generate(per_type=30, seed=6)
        self.cases = month + redteam.build_attack_cases(redteam.offline_specs(12), meta, month, "t")

    def test_sample_is_stratified_spread_and_stable(self):
        from probie.runner import choose
        picked = choose(self.cases, sample=60)
        month = [c for c in picked if not c.get("redteam")]
        self.assertEqual(len(month), 60)
        self.assertEqual({c["type"] for c in month}, set(generate.TYPES))
        self.assertTrue(all(sum(1 for c in month if c["type"] == t) == 10 for t in generate.TYPES))
        self.assertEqual(sum(1 for c in picked if c.get("redteam")), 12)
        dates = sorted(c["invoice"]["date"] for c in month)
        self.assertLess(dates[0], "2026-10-04")
        self.assertGreater(dates[-1], "2026-10-18")
        self.assertEqual([c["id"] for c in picked], [c["id"] for c in choose(self.cases, sample=60)])
        self.assertEqual(len(choose(self.cases, only="redteam")), 12)
        self.assertEqual(len(choose(self.cases, only="voicemails")), sum(1 for c in self.cases if c.get("voicemail")))
        with self.assertRaises(RunError):
            choose(self.cases, only="everything")

    def test_a_sample_resumes_into_a_full_run(self):
        from probie.runner import choose
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            spec = config.model("careful")
            ids = {c["id"] for c in choose(self.cases, sample=24)}
            first = run_model(config, spec, self.cases, "sha", only_ids=ids, out=lambda line: None)
            self.assertEqual(first.completed, len(ids))
            rest = run_model(config, spec, self.cases, "sha", out=lambda line: None)
            self.assertEqual(rest.completed, len(self.cases) - len(ids))


class GivingUpEarlyTests(unittest.TestCase):
    """A model that answers but never decides shouldn't be able to spend the whole budget."""

    class Useless:
        """Replies politely, never calls a tool. Every case comes back unusable."""

        def __init__(self):
            self.calls = 0

        def chat(self, payload):
            self.calls += 1
            return {"choices": [{"message": {"role": "assistant", "content": "I'd rather not say."}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10}}

    def test_a_run_stops_after_a_dozen_unusable_cases(self):
        from probie import generate
        from probie.runner import MAX_CONSECUTIVE_UNUSABLE
        _, cases = generate.generate(per_type=40, seed=3)
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            client = self.Useless()
            lines = []
            summary = run_model(config, config.model("careful"), cases, "sha", out=lines.append,
                                concurrency=1, client=client)
        self.assertIsNotNone(summary.stopped)
        self.assertIn("no usable answer", summary.stopped)
        self.assertIn("ping --model", summary.stopped)
        self.assertLess(summary.completed, 40, "it kept going after everything came back unusable")
        self.assertGreaterEqual(summary.unusable, MAX_CONSECUTIVE_UNUSABLE)

    def test_the_count_resets_when_a_case_works(self):
        from probie import generate
        _, cases = generate.generate(per_type=20, seed=4)
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            summary = run_model(config, config.model("careful"), cases, "sha", out=lambda line: None,
                                client=OfflineClient(cases, "careful"))
        self.assertIsNone(summary.stopped)
        self.assertEqual(summary.completed, len(cases))


class SubsetPaperworkTests(unittest.TestCase):
    """A sample that skips the voicemail cases shouldn't need the voicemails recorded."""

    def test_a_sample_without_voicemails_runs_before_they_are_recorded(self):
        from probie import generate
        from probie.runner import choose
        from probie.config import ModelSpec
        meta, cases = generate.generate(per_type=40, seed=5)
        self.assertTrue([c for c in cases if c.get("voicemail")], "this test needs voicemail cases")
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            spec = ModelSpec(name="hears", model="offline-careful", provider="offline", offline_profile="careful",
                             voicemail_input="transcript", price_input=1.0, price_output=2.0)
            no_voicemails = {c["id"] for c in cases if not c.get("voicemail")}
            picked = {c["id"] for c in choose(cases, sample=20)} & no_voicemails
            summary = run_model(config, spec, cases, "sha", only_ids=picked, out=lambda line: None,
                                client=OfflineClient(cases, "careful"))
            self.assertEqual(summary.completed, len(picked))
            # The full set still refuses, because now it really does need them.
            with self.assertRaises(RunError) as ctx:
                run_model(config, spec, cases, "sha", out=lambda line: None,
                          client=OfflineClient(cases, "careful"))
            self.assertIn("aren't ready", str(ctx.exception))
