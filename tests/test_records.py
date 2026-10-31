"""The decision record: duplicate detection and the audit trail, with no documents kept."""

import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path

from probie import records as records_mod
from probie.cli import main
from probie.records import Records, fingerprint

FIELDS = {"vendor_name": "Blue Harbor Coffee Roasters", "document_number": "INV-88214", "date": "2026-10-12",
          "total": "$4,182.40", "confidence": "high"}
ANSWER = {"decision": "PAY", "amount_cents": 418_240, "rule": "none", "why": "Everything on the invoice is payable."}
CARD = {"models": [{"model": "glimmer", "caught": 6, "attacks": [{"key": "a"}] * 6},
                   {"model": "spark", "caught": 4, "attacks": [{"key": "a"}] * 6}]}


class FingerprintTests(unittest.TestCase):
    def test_the_same_bytes_give_the_same_fingerprint_and_nothing_else_does(self):
        self.assertEqual(fingerprint(b"invoice"), fingerprint(b"invoice"))
        self.assertNotEqual(fingerprint(b"invoice"), fingerprint(b"invoice "))
        self.assertEqual(len(fingerprint(b"x")), 64)

    def test_invoice_numbers_match_the_way_the_policy_matches_them(self):
        self.assertEqual(records_mod._norm_number("INV-88214"), records_mod._norm_number("inv 88.214"))
        self.assertEqual(records_mod._norm_number("A/1"), "A1")
        self.assertEqual(records_mod._norm_vendor("Blue Harbor Coffee!"), "blueharborcoffee")


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.records = Records(Path(self.tmp.name) / "records.sqlite")
        self.records.add(fingerprint="aaa", fields=FIELDS, answer=ANSWER, card=CARD, filename="invoice.pdf",
                         reader="muse-spark-low", checks=["vendor", "history"], total_cents=418_240)

    def test_the_identical_file_again(self):
        found = self.records.seen_before(fingerprint="aaa")
        self.assertEqual([m.kind for m in found], ["same_file"])
        self.assertIn("the identical file was checked on", found[0].why)

    def test_the_same_invoice_number_under_a_new_file(self):
        found = self.records.seen_before(fingerprint="bbb", vendor_name="blue harbor coffee roasters",
                                         document_number="inv 88214")
        self.assertEqual([m.kind for m in found], ["same_number"])
        self.assertIn("INV-88214", found[0].why)

    def test_the_same_vendor_amount_and_date_under_a_different_number(self):
        """How duplicate billing usually looks: the fraudster changes the number."""
        found = self.records.seen_before(fingerprint="ccc", vendor_name="Blue Harbor Coffee Roasters",
                                         document_number="INV-99999", document_date="2026-10-12",
                                         total_cents=418_240)
        self.assertEqual([m.kind for m in found], ["same_amount"])
        self.assertIn("billed $4,182.40 on the same date", found[0].why)

    def test_a_genuinely_new_invoice_matches_nothing(self):
        self.assertEqual(self.records.seen_before(fingerprint="ddd", vendor_name="Alder Creek Dairy",
                                                  document_number="AC-1", document_date="2026-10-13",
                                                  total_cents=9_900), [])

    def test_a_different_vendor_with_the_same_number_is_not_a_duplicate(self):
        self.assertEqual(self.records.seen_before(fingerprint="eee", vendor_name="Someone Else",
                                                  document_number="INV-88214"), [])


class TrailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.records = Records(Path(self.tmp.name) / "records.sqlite")

    def test_what_a_row_holds_and_what_it_doesnt(self):
        self.records.add(fingerprint="aaa", fields=FIELDS, answer=ANSWER, card=CARD, filename="invoice.pdf",
                         reader="muse-spark-low", checks=["vendor", "history"], total_cents=418_240)
        row = self.records.trail()[0]
        self.assertEqual((row["vendor_name"], row["document_number"], row["decision"]),
                         ("Blue Harbor Coffee Roasters", "INV-88214", "PAY"))
        self.assertEqual((row["attacks_caught"], row["attacks_total"]), (6, 6))
        self.assertEqual(row["fingerprint"], "aaa")
        self.assertNotIn("document", row, "a row must never carry the document")
        self.assertNotIn("bank", " ".join(str(v) for v in row.values()).lower())

    def test_export_is_a_csv_an_auditor_can_read(self):
        for i in range(3):
            self.records.add(fingerprint=f"f{i}", fields=dict(FIELDS, document_number=f"INV-{i}"), answer=ANSWER,
                             card=CARD, filename=f"{i}.pdf", reader="r", checks=["vendor"], total_cents=100)
        out = self.records.export(Path(self.tmp.name) / "trail.csv")
        lines = out.read_text().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("checked_at,filename,vendor_name"))
        self.assertIn("glimmer, spark", lines[1])
        self.assertEqual(self.records.count(), 3)

    def test_the_command_shows_and_exports_the_trail(self):
        config = Path(self.tmp.name) / "probie.toml"
        config.write_text('[run]\ncases = "cases.jsonl"\nruns_dir = "."\n[[model]]\nname = "s"\n'
                          'model = "offline-careful"\nprovider = "offline"\noffline_profile = "careful"\n'
                          'price_input = 1.0\nprice_output = 2.0\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(main(["--config", str(config), "records"]), 0)
        self.assertIn("No decisions recorded yet", out.getvalue())
        Records(Path(self.tmp.name) / records_mod.FILENAME).add(
            fingerprint="aaa", fields=FIELDS, answer=ANSWER, card=CARD, filename="invoice.pdf", reader="r",
            checks=["vendor"], total_cents=418_240)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(main(["--config", str(config), "records"]), 0)
            self.assertEqual(main(["--config", str(config), "records", "--export",
                                   str(Path(self.tmp.name) / "t.csv")]), 0)
        text = out.getvalue()
        self.assertIn("Blue Harbor Coffee Roasters", text)
        self.assertIn("caught 6/6", text)
        self.assertIn("No documents are kept", text)
        self.assertTrue((Path(self.tmp.name) / "t.csv").exists())


class TheGameKeepsNothingTests(unittest.TestCase):
    """The public page promises a visitor's document leaves no trace. That has to stay true."""

    def test_a_visitors_upload_writes_no_decision_record(self):
        import base64
        import json
        import urllib.request
        from probie import game as game_mod
        from probie import generate
        from probie.config import ModelSpec, config_from_dict
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from test_yours import FakeReader

        fake = FakeReader()
        self.addCleanup(fake.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _, cases = generate.generate(per_type=6, seed=12)
        config = config_from_dict({"run": {"runs_dir": "runs"}, "model": [
            {"name": "x", "model": "offline-careful", "provider": "offline", "offline_profile": "careful",
             "price_input": 1.0, "price_output": 2.0}]}, base_dir=Path(tmp.name))
        models = [ModelSpec(name="reader", model="r", base_url=fake.url, invoice_input="document",
                            price_input=1.0, price_output=2.0),
                  ModelSpec(name="offline-quick", model="offline-quick", provider="offline",
                            offline_profile="quick", price_input=1.0, price_output=2.0)]
        game = game_mod.Game(config, cases, models)
        server = game_mod.serve(game, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        body = json.dumps({"document": "data:image/png;base64," +
                           base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 300).decode()}).encode()
        req = urllib.request.Request(base + "/api/yours", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            attempt_id = json.loads(r.read())["id"]
        for _ in range(400):
            with urllib.request.urlopen(f"{base}/api/yours/{attempt_id}") as r:
                view = json.loads(r.read())
            if view["status"] == "done":
                break
            time.sleep(0.05)
        self.assertIsNone(view["error"], view["error"])
        self.assertFalse((config.run.runs_dir / records_mod.FILENAME).exists(),
                         "the public game wrote a decision record for a visitor's document")
        # The anonymous benchmark is still written, because it holds no document details.
        self.assertGreater(game.scores.benchmark()["documents_checked"], 0)


if __name__ == "__main__":
    unittest.main()
