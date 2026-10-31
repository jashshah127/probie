import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from probie import __version__
from probie.cli import main

CONFIG = """
[run]
cases = "cases.jsonl"
runs_dir = "runs"
cache = "runs/cache.sqlite"
budget_usd = 100

[[model]]
name = "stand-in"
model = "offline-careful"
provider = "offline"
offline_profile = "careful"
price_input = 1.0
price_output = 2.0

[[model]]
name = "remote"
model = "muse-spark-1.3"
base_url = "https://api.meta.ai/v1"
api_key_env = "PROBIE_TEST_MISSING_KEY"
price_input = 1.25
price_output = 4.25
"""


def run(*argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "probie.toml"
        self.config.write_text(CONFIG)
        os.environ.pop("PROBIE_TEST_MISSING_KEY", None)

    def cli(self, *argv, **kwargs):
        return run("--config", str(self.config), *argv, **kwargs)

    def test_generate_and_overwrite_protection(self):
        code, out, _ = self.cli("generate", "--per-type", "3")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.dir / "cases.jsonl").exists())
        self.assertIn("Wrote 27 cases", out)
        code, _, err = self.cli("generate", "--per-type", "3")
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.cli("generate", "--per-type", "3", "--force")[0], 0)

    def test_run_needs_yes_without_a_terminal(self):
        self.cli("generate", "--per-type", "2")
        code, _, err = self.cli("run", "--model", "stand-in")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)

    def test_run_report_and_resume(self):
        self.cli("generate", "--per-type", "3")
        code, out, err = self.cli("run", "--model", "stand-in", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("27 of 27 cases finished", out)
        self.assertIn("about $", out)
        code, out, _ = self.cli("run", "--model", "stand-in", "--yes")
        self.assertIn("every case already has a result", out)
        html, js = self.dir / "r.html", self.dir / "r.json"
        code, out, err = self.cli("report", "--out", str(html), "--json", str(js))
        self.assertEqual(code, 0, err)
        self.assertTrue(html.exists() and js.exists())
        self.assertIn("Bank details:", out)

    def test_report_with_nothing_run(self):
        self.cli("generate", "--per-type", "2")
        code, _, err = self.cli("report", "--out", str(self.dir / "r.html"))
        self.assertEqual(code, 1)
        self.assertIn("No results", err)

    def test_models_estimate_ping(self):
        self.cli("generate", "--per-type", "2")
        code, out, _ = self.cli("models")
        self.assertEqual(code, 0)
        self.assertIn("no key needed", out)
        self.assertIn("missing PROBIE_TEST_MISSING_KEY", out)
        code, out, _ = self.cli("estimate", "--all")
        self.assertEqual(code, 0)
        self.assertIn("remote: about $", out)
        code, out, _ = self.cli("ping", "--model", "stand-in")
        self.assertIn("nothing to reach", out)
        code, _, err = self.cli("ping", "--model", "remote")
        self.assertEqual(code, 1)
        self.assertIn("PROBIE_TEST_MISSING_KEY", err)

    def test_errors_are_clean(self):
        code, _, err = self.cli("run", "--model", "nope", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No case file", err)
        self.cli("generate", "--per-type", "2")
        code, _, err = self.cli("run", "--model", "nope", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Models: stand-in, remote", err)
        self.assertEqual(self.cli("run", "--model", "stand-in", "--limit", "0")[0], 2)
        self.assertEqual(self.cli("run", "--model", "stand-in", "--budget", "-1")[0], 2)
        code, _, err = run("--config", str(self.dir / "missing.toml"), "models")
        self.assertEqual(code, 1)
        self.assertIn("No config", err)

    def test_version(self):
        code, out, _ = run("--version")
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)

    def test_demo(self):
        code, out, err = run("demo", "--out-dir", str(self.dir / "demo"), "--per-type", "10")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.dir / "demo" / "report.html").exists())
        self.assertIn("not Meta models", out)


if __name__ == "__main__":
    unittest.main()


class LearnedWaveTests(unittest.TestCase):
    """`redteam --learn` reads the results on disk and writes a wave aimed at what worked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "probie.toml").write_text(
            '[run]\ncases = "cases.jsonl"\nruns_dir = "runs"\n[[model]]\nname = "quick"\nmodel = "offline-quick"\n'
            'provider = "offline"\noffline_profile = "quick"\nprice_input = 1.0\nprice_output = 2.0\n')

    def run_cli(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--config", str(self.dir / "probie.toml"), *argv])
        return code, out.getvalue()

    def test_the_whole_two_wave_flow(self):
        import json as _json
        self.assertEqual(self.run_cli("generate", "--per-type", "30")[0], 0)
        self.assertEqual(self.run_cli("redteam", "--attacker", "offline", "--count", "12")[0], 0)

        code, out = self.run_cli("redteam", "--attacker", "offline", "--count", "8", "--learn")
        self.assertEqual(code, 1, out)
        self.assertIn("nothing to learn from", out)

        self.assertEqual(self.run_cli("run", "--model", "quick", "--yes")[0], 0)
        code, out = self.run_cli("redteam", "--attacker", "offline", "--count", "8", "--learn")
        self.assertEqual(code, 0, out)
        self.assertIn("Wave 2:", out)
        self.assertIn("The attacker gets to see that", out)

        lines = (self.dir / "cases.jsonl").read_text().splitlines()
        meta = _json.loads(lines[0])["_meta"]
        cases = [_json.loads(line) for line in lines[1:]]
        attacks = [c for c in cases if c.get("redteam")]
        self.assertEqual(len(attacks), 20)
        self.assertEqual(sorted(meta["redteam"]["waves"]), ["1", "2"])
        self.assertFalse(meta["redteam"]["waves"]["1"]["learned"])
        self.assertTrue(meta["redteam"]["waves"]["2"]["learned"])
        self.assertEqual(sum(1 for c in attacks if c["redteam"]["wave"] == 2), 8)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        self.assertTrue(all(c["answer"]["rule"] == "Rule 1" for c in attacks))

    def test_a_first_wave_cant_be_learned_from_nothing(self):
        self.assertEqual(self.run_cli("generate", "--per-type", "8")[0], 0)
        code, out = self.run_cli("redteam", "--attacker", "offline", "--count", "4", "--learn")
        self.assertEqual(code, 1)
        self.assertIn("no first wave to learn from", out)

    def test_a_plain_second_run_still_warns_about_replacing(self):
        self.assertEqual(self.run_cli("generate", "--per-type", "8")[0], 0)
        self.assertEqual(self.run_cli("redteam", "--attacker", "offline", "--count", "4")[0], 0)
        code, out = self.run_cli("redteam", "--attacker", "offline", "--count", "4")
        self.assertEqual(code, 1)
        self.assertIn("--replace", out)
        self.assertIn("--learn", out)
