"""Keeping the scoreboard across restarts, and the benchmark built from it."""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from probie import store
from probie.cli import main
from probie.game import Scoreboard

CARD = {"models": [
    {"model": "glimmer", "attacks": [{"key": "padded-line", "outcome": "caught"},
                                     {"key": "email-bank-change", "outcome": "caught"}]},
    {"model": "spark", "attacks": [{"key": "padded-line", "outcome": "paid"},
                                   {"key": "email-bank-change", "outcome": "caught"}]},
]}


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "game.sqlite"
        self.scores = Scoreboard(self.path)

    def test_nothing_identifying_is_stored_about_a_document(self):
        self.scores.record_check(CARD, "high")
        with sqlite3.connect(self.path) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(checks)")}
            rows = db.execute("SELECT * FROM checks").fetchall()
        self.assertEqual(columns, {"id", "at", "run_id", "model", "attack", "outcome", "confidence"})
        self.assertEqual(len(rows), 4)
        blob = " ".join(str(v) for row in rows for v in row)
        for leak in ("Blue Harbor", "4417229903", "INV-", "$"):
            self.assertNotIn(leak, blob)

    def test_documents_are_counted_once_each_not_per_row(self):
        self.scores.record_check(CARD, "high")
        self.scores.record_check(CARD, "low")
        self.assertEqual(self.scores.benchmark()["documents_checked"], 2)

    def test_the_benchmark_combines_the_game_and_documents(self):
        self.scores.add("a", "glimmer", "V", "held", [], 0, 0.0, "w1", "A")
        self.scores.add("b", "spark", "V", "fired", ["urgency"], 100, 0.0, "w2", "B")
        self.scores.record_check(CARD, "high")
        data = self.scores.benchmark()
        glimmer = next(m for m in data["models"] if m["model"] == "glimmer")
        spark = next(m for m in data["models"] if m["model"] == "spark")
        self.assertEqual((glimmer["faced"], glimmer["beaten"]), (3, 0))
        self.assertEqual(glimmer["held_share"], 1.0)
        self.assertEqual((spark["faced"], spark["beaten"]), (3, 2))
        self.assertEqual([m["model"] for m in data["models"]], ["glimmer", "spark"])
        worst = data["attacks"][0]
        self.assertEqual((worst["attack"], worst["paid"]), ("padded-line", 1))

    def test_an_empty_scoreboard_says_so(self):
        self.assertEqual(self.scores.benchmark()["models"], [])

    def test_the_command_reads_the_scoreboard_without_building_a_game(self):
        config = Path(self.tmp.name) / "probie.toml"
        config.write_text('[run]\ncases = "cases.jsonl"\nruns_dir = "."\n[[model]]\nname = "s"\n'
                          'model = "offline-careful"\nprovider = "offline"\noffline_profile = "careful"\n'
                          'price_input = 1.0\nprice_output = 2.0\n')
        self.scores.add("a", "glimmer", "V", "held", [], 0, 0.0, "w", "A")
        self.scores.record_check(CARD, "high")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--config", str(config), "benchmark"])
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("glimmer", text)
        self.assertIn("documents checked", text)
        self.assertIn("padded-line", text)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in (store.ENV_REPO, *store.ENV_TOKEN)}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) for k, v in self.saved.items() if v is not None])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_it_stays_off_and_says_why_without_configuration(self):
        why = store.why_off()
        self.assertIsNotNone(why)
        self.assertIn("only kept while this container lives", why)
        path = Path(self.tmp.name) / "game.sqlite"
        Scoreboard(path)
        self.assertFalse(store.pull(path, out=None))
        self.assertFalse(store.push(path, out=None))
        keeper = store.Keeper(path)
        self.assertFalse(keeper.on)
        keeper.start()
        keeper.stop()  # a no-op that mustn't raise

    def test_it_knows_a_real_database_from_a_404_page(self):
        good = Path(self.tmp.name) / "good.sqlite"
        Scoreboard(good)
        self.assertTrue(store.looks_like_a_database(good))
        bad = Path(self.tmp.name) / "bad.sqlite"
        bad.write_text("<html>Not Found</html>")
        self.assertFalse(store.looks_like_a_database(bad))
        self.assertFalse(store.looks_like_a_database(Path(self.tmp.name) / "missing.sqlite"))

    def test_a_pull_never_overwrites_a_scoreboard_that_already_has_scores(self):
        os.environ[store.ENV_REPO] = "someone/probie-scores"
        os.environ["HF_TOKEN"] = "hf_fake"
        path = Path(self.tmp.name) / "game.sqlite"
        scores = Scoreboard(path)
        scores.add("a", "glimmer", "V", "held", [], 0, 0.0, "w", "A")
        self.assertGreater(path.stat().st_size, 0)
        self.assertFalse(store.pull(path, out=None), "a pull would have clobbered the live scoreboard")
        self.assertEqual(len(Scoreboard(path).feed()), 1)

    def test_the_game_starts_even_when_syncing_is_configured_but_unreachable(self):
        os.environ[store.ENV_REPO] = "someone/probie-scores"
        os.environ["HF_TOKEN"] = "hf_fake"
        from probie import game as game_mod
        from probie import generate
        from probie.config import ModelSpec, config_from_dict
        _, cases = generate.generate(per_type=6, seed=9)
        config = config_from_dict({"run": {"runs_dir": "runs"}, "model": [
            {"name": "s", "model": "offline-careful", "provider": "offline", "offline_profile": "careful",
             "price_input": 1.0, "price_output": 2.0}]}, base_dir=Path(self.tmp.name))
        spec = ModelSpec(name="offline-quick", model="offline-quick", provider="offline", offline_profile="quick",
                         price_input=1.0, price_output=2.0)
        game = game_mod.Game(config, cases, [spec])
        self.assertTrue(game.state()["open"])


if __name__ == "__main__":
    unittest.main()
