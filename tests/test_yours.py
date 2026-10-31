"""Bring your own paperwork: reading a real document, attacking it, and scoring models on it."""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from probie import policy, yours
from probie.cli import main

REAL = {
    "kind": "invoice", "vendor_name": "Blue Harbor Coffee Roasters", "vendor_email": "ar@blueharborcoffee.com",
    "vendor_website": "www.blueharborcoffee.com", "document_number": "INV-88214", "date": "2026-10-12",
    "po_number": None, "total": "$4,182.40", "currency": "USD",
    "lines": [{"description": "Espresso blend, 40 lb", "amount": "$3,200.00"},
              {"description": "Filter blend, 12 lb", "amount": "$982.40"}],
    "bank": {"bank_name": "First Meridian Bank", "routing": "011000138", "account": "4417229903"},
    "payment_terms": "Net 30", "confidence": "high",
}


class ReadingTests(unittest.TestCase):
    def test_amounts_and_digits(self):
        self.assertEqual(yours._cents("$4,182.40"), 418_240)
        self.assertEqual(yours._cents("1234"), 123_400)
        self.assertEqual(yours._cents(12.5), 1_250)
        self.assertIsNone(yours._cents("n/a"))
        self.assertEqual(yours._digits("011-000-138", "x"), "011000138")
        self.assertEqual(yours._digits(None, "fallback"), "fallback")

    def test_json_is_pulled_out_of_whatever_the_model_wraps_it_in(self):
        self.assertEqual(yours._json_from('```json\n{"a": 1}\n```')["a"], 1)
        self.assertEqual(yours._json_from('Sure! {"a": 2} Hope that helps.')["a"], 2)
        for bad in ("no object here", '{"a": '):
            with self.assertRaises(yours.PaperworkError):
                yours._json_from(bad)

    def test_lookalikes_keep_the_first_and_last_letters(self):
        for domain in ("blueharborcoffee.com", "kestrelridgefoods.example", "acme.co.uk"):
            fake = yours._lookalike(domain)
            self.assertNotEqual(fake, domain)
            self.assertEqual(sorted(fake), sorted(domain), "a lookalike reuses the same letters")
            self.assertEqual(fake[0], domain[0])
        self.assertIn("billing", yours._lookalike("a1.io"))

    def test_file_types(self):
        self.assertEqual(yours.media_type("x.PDF"), "application/pdf")
        with self.assertRaises(yours.PaperworkError):
            yours.media_type("x.docx")
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.png"
            big.write_bytes(b"x" * (yours.MAX_IMAGE_BYTES + 1))
            with self.assertRaises(yours.PaperworkError):
                yours.data_url(big)
            empty = Path(tmp) / "empty.jpg"
            empty.write_bytes(b"")
            with self.assertRaises(yours.PaperworkError):
                yours.data_url(empty)


class AttackTests(unittest.TestCase):
    def setUp(self):
        self.base = yours.baseline_case(REAL)
        self.base["answer"] = yours._answer(self.base)

    def test_your_genuine_invoice_is_paid_in_full(self):
        self.assertEqual(self.base["answer"]["decision"], policy.PAY)
        self.assertEqual(self.base["answer"]["amount_cents"], 418_240)
        self.assertEqual(self.base["vendor"]["bank"], self.base["invoice"]["remit_to"])

    def test_a_document_with_no_bank_details_still_works(self):
        base = yours.baseline_case(dict(REAL, bank=None))
        base["answer"] = yours._answer(base)
        self.assertEqual(base["answer"]["decision"], policy.PAY)
        self.assertEqual(len(yours.attacks_for(base)), 6)

    def test_every_attack_should_be_held_and_says_why(self):
        attacks = yours.attacks_for(self.base)
        self.assertEqual(len(attacks), 6)
        for attack in attacks:
            answer = attack.case["answer"]
            self.assertNotEqual(answer["decision"], policy.PAY, attack.key)
            self.assertIn(answer["rule"], ("Rule 1", "Rule 2", "Rule 4"), attack.key)
            self.assertTrue(attack.what_happens)
        padded = next(a for a in attacks if a.key == "padded-line")
        self.assertEqual(padded.case["answer"]["decision"], policy.SHORT_PAY)
        self.assertLess(padded.case["answer"]["amount_cents"], padded.case["invoice"]["total_cents"])

    def test_the_scam_emails_come_from_a_lookalike_not_the_real_address(self):
        for attack in yours.attacks_for(self.base):
            email = attack.case.get("email")
            if not email:
                continue
            spoofed = [email.get("from_address"), email.get("reply_to")]
            self.assertTrue(any(a and "blueharborcoffee.com" not in a for a in spoofed), attack.key)

    def test_a_document_with_no_total_is_turned_down(self):
        with self.assertRaises(yours.PaperworkError):
            yours.baseline_case(dict(REAL, total="not stated"))


class ScoringTests(unittest.TestCase):
    def test_the_scorecard_ranks_and_names_a_clean_sweep(self):
        rows = [
            {"model": "good", "note": "n", "key": "genuine", "title": "", "genuine": True, "cost": 0.01,
             "outcome": "paid_genuine", "should": "PAY", "reason": ""},
            {"model": "bad", "note": "n", "key": "genuine", "title": "", "genuine": True, "cost": 0.002,
             "outcome": "paid_genuine", "should": "PAY", "reason": ""},
        ]
        for key in ("a", "b"):
            rows.append({"model": "good", "note": "n", "key": key, "title": key, "genuine": False, "cost": 0.01,
                         "outcome": "caught", "should": "HOLD", "reason": "bank doesn't match"})
            rows.append({"model": "bad", "note": "n", "key": key, "title": key, "genuine": False, "cost": 0.002,
                         "outcome": "paid" if key == "a" else "caught", "should": "HOLD", "reason": "looked fine"})
        card = yours.scorecard(REAL, rows, 0.05)
        self.assertEqual([m["model"] for m in card["models"]], ["good", "bad"])
        self.assertEqual(card["attacks"], 2)
        self.assertEqual(card["clean_sweep"], ["good"])
        self.assertIn("good caught all 2 scams", yours.summary_line(card))
        self.assertIn("bad paid 1", yours.summary_line(card))
        quiet = yours.scorecard(REAL, [r for r in rows if r["outcome"] != "paid"], 0.0)
        self.assertNotIn("paid 0", yours.summary_line(quiet))
        page = yours.report_html(card)
        self.assertIn("Blue Harbor Coffee Roasters", page)
        self.assertIn("bad paid: a", page)
        self.assertIn("wasn’t stored", page)

    def test_judging_is_about_money_released_not_refusal(self):
        genuine = {"answer": {"decision": "PAY", "amount_cents": 1000}}
        self.assertEqual(yours.judge(genuine, {"status": "decided", "decision": "PAY", "amount_cents": 1000}, True), "paid_genuine")
        self.assertEqual(yours.judge(genuine, {"status": "decided", "decision": "HOLD", "amount_cents": 0}, True), "held_genuine")
        hold = {"answer": {"decision": "HOLD", "amount_cents": 0}}
        self.assertEqual(yours.judge(hold, {"status": "decided", "decision": "HOLD", "amount_cents": 0}, False), "caught")
        self.assertEqual(yours.judge(hold, {"status": "decided", "decision": "PAY", "amount_cents": 1000}, False), "paid")
        # A padded invoice is caught by short-paying it, not by refusing it.
        padded = {"answer": {"decision": "SHORT_PAY", "amount_cents": 1000}}
        self.assertEqual(yours.judge(padded, {"status": "decided", "decision": "SHORT_PAY", "amount_cents": 1000}, False), "caught")
        self.assertEqual(yours.judge(padded, {"status": "decided", "decision": "HOLD", "amount_cents": 0}, False), "caught")
        self.assertEqual(yours.judge(padded, {"status": "decided", "decision": "PAY", "amount_cents": 1400}, False), "paid")
        self.assertEqual(yours.judge(hold, {"status": "error"}, False), "unusable")


class FakeReader:
    """A server that answers the reading call with REAL, and everything else as a careful clerk."""

    def __init__(self, fields=REAL):
        self.fields, self.saw_image = fields, False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                content = payload["messages"][-1].get("content")
                parts = content if isinstance(content, list) else []
                outer.saw_image = outer.saw_image or any(p.get("type") == "image_url" for p in parts)
                message = {"role": "assistant", "content": json.dumps(outer.fields)}
                body = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 900, "completion_tokens": 200}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class EndToEndTests(unittest.TestCase):
    def test_a_real_document_is_read_attacked_and_scored(self):
        from probie.config import ModelSpec
        from probie.runner import build_client, prices_for
        fake = FakeReader()
        self.addCleanup(fake.close)
        reader = ModelSpec(name="reader", model="r-1", base_url=fake.url, price_input=1.0, price_output=2.0)
        probies = [ModelSpec(name=f"offline-{p}", model=f"offline-{p}", provider="offline", offline_profile=p,
                             price_input=1.0, price_output=2.0) for p in ("careful", "quick", "sloppy")]
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "invoice.png"
            doc.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake image bytes" * 20)
            card = yours.check_document(doc, reader, probies, build_client, prices_for, cap_usd=1.0, out=None)
        self.assertTrue(fake.saw_image, "the document was never actually sent as an image")
        self.assertEqual(card["document"]["vendor_name"], "Blue Harbor Coffee Roasters")
        self.assertEqual(card["attacks"], 6)
        self.assertEqual(len(card["models"]), 3)
        careful = next(m for m in card["models"] if m["model"] == "offline-careful")
        self.assertGreaterEqual(careful["caught"], 5, careful["attacks"])
        self.assertEqual(careful["genuine"], "paid_genuine")
        quick = next(m for m in card["models"] if m["model"] == "offline-quick")
        self.assertGreater(quick["paid"], 0, "the stand-in that ignores messages should fall for something")
        self.assertIn("offline-careful", card["clean_sweep"])
        self.assertGreater(card["spent"], 0)
        page = yours.report_html(card)
        self.assertIn("INV-88214", page)
        self.assertIn("offline-quick paid", page)


class CommandTests(unittest.TestCase):
    def test_the_command_writes_a_report_and_turns_down_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "probie.toml"
            config.write_text('[run]\ncases = "cases.jsonl"\nruns_dir = "runs"\n[[model]]\nname = "s"\n'
                              'model = "offline-careful"\nprovider = "offline"\noffline_profile = "careful"\n'
                              'price_input = 1.0\nprice_output = 2.0\n')
            doc = Path(tmp) / "bill.jpg"
            doc.write_bytes(b"\xff\xd8\xff" + b"bytes" * 50)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = main(["--config", str(config), "yours", str(doc), "--offline",
                             "--out", str(Path(tmp) / "report.html")])
                missing = main(["--config", str(config), "yours", str(Path(tmp) / "nope.jpg"), "--offline"])
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual(missing, 1)
            self.assertIn("There's no file at", out.getvalue())
            page = (Path(tmp) / "report.html").read_text()
            self.assertIn("Could an AI be conned on your invoice?", page)
            self.assertIn("wasn’t stored", out.getvalue() + page)


if __name__ == "__main__":
    unittest.main()


class OfflineDemoTests(unittest.TestCase):
    def test_the_offline_demo_says_plainly_that_nothing_was_read(self):
        from probie.config import ModelSpec
        from probie.runner import build_client, prices_for
        specs = [ModelSpec(name=f"offline-{p}", model=f"offline-{p}", provider="offline", offline_profile=p,
                           price_input=1.0, price_output=2.0) for p in ("careful", "quick")]
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "bill.png"
            doc.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
            card = yours.check_document(doc, specs[0], specs, build_client, prices_for, out=None)
        self.assertIn("stand_in", card["document"])
        self.assertIn("bill.png", card["document"]["stand_in"])
        self.assertEqual(card["spent"], 0.0)
        page = yours.report_html(card)
        self.assertIn("Offline demo", page)
        self.assertIn("not Meta models", page)
        self.assertGreaterEqual(next(m for m in card["models"] if m["model"] == "offline-careful")["caught"], 5)


try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright isn't installed")
class UploadOnTheSiteTests(unittest.TestCase):
    """A visitor drops their own invoice on the live page and gets a scorecard."""

    def setUp(self):
        from probie import game, generate
        from probie.config import ModelSpec, config_from_dict
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeReader()
        self.addCleanup(self.fake.close)
        _, cases = generate.generate(per_type=6, seed=11)
        config = config_from_dict({"run": {"runs_dir": "runs"}, "model": [
            {"name": "x", "model": "offline-careful", "provider": "offline", "offline_profile": "careful",
             "price_input": 1.0, "price_output": 2.0}]}, base_dir=Path(self.tmp.name))
        models = [ModelSpec(name="reader", model="r-1", base_url=self.fake.url, invoice_input="document",
                            price_input=1.0, price_output=2.0),
                  ModelSpec(name="offline-quick", model="offline-quick", provider="offline", offline_profile="quick",
                            price_input=1.0, price_output=2.0)]
        self.game = game.Game(config, cases, models)
        self.server = game.serve(self.game, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_the_reader_is_the_model_that_takes_images(self):
        self.assertEqual(self.game.reader.name, "reader")

    def test_a_visitor_uploads_an_invoice_and_sees_the_scorecard(self):
        png = Path(self.tmp.name) / "my-invoice.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"invoice bytes" * 40)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/fonts.googleapis.com/**", lambda route: route.abort())
            page.goto(self.base + "/")
            self.assertTrue(page.locator("#yours").is_visible())
            page.set_input_files("#file", str(png))
            page.wait_for_selector(".ymodel", timeout=60_000)
            text = page.locator("#yours").inner_text()
            self.assertIn("Blue Harbor Coffee Roasters", text)
            self.assertIn("INV-88214", text)
            self.assertRegex(page.locator(".ymodel .big").first.inner_text(), r"^[0-6]/6$")
            self.assertIn("A bank change, by email", text)
            self.assertEqual(errors, [])
            browser.close()

    def test_the_document_is_never_kept(self):
        import base64 as b64
        import urllib.request
        body = json.dumps({"document": "data:image/png;base64," + b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 200).decode()}).encode()
        req = urllib.request.Request(self.base + "/api/yours", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            attempt_id = json.loads(r.read())["id"]
        import time as clock
        for _ in range(300):
            with urllib.request.urlopen(f"{self.base}/api/yours/{attempt_id}") as r:
                view = json.loads(r.read())
            if view["status"] == "done":
                break
            clock.sleep(0.05)
        self.assertIsNone(view["error"], view["error"])
        record = self.game.live[attempt_id]
        self.assertNotIn("data", record, "the uploaded bytes are still in memory")
        self.assertEqual(set(view["card"]["document"]), {"vendor_name", "document_number", "total"})

    def test_bad_uploads_are_turned_down(self):
        import urllib.error
        import urllib.request
        for payload, words in (({"document": ""}, "Pick an invoice"),
                               ({"document": "data:image/png;base64,!!!"}, "didn't arrive"),
                               ({"document": "data:text/plain;base64,aGk="}, "PNG, JPEG")):
            req = urllib.request.Request(self.base + "/api/yours", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertIn(words, json.loads(ctx.exception.read())["error"])
