import base64
import contextlib
import io
import json
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from probie import generate, redteam, staff, staff_html
from probie.cli import main
from probie.config import ModelSpec, VerdictSettings
from probie.desk import replay
from probie.score import score
from tests.helpers import ScriptedClient, response

WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


def month_with_a_firing():
    meta, cases = generate.generate(per_type=200, seed=7)
    attacks = redteam.build_attack_cases(redteam.offline_specs(12), meta, cases, "the offline attacker")
    cases = cases + attacks
    results = {}
    for c in cases:
        r = {"case_id": c["id"], "status": "decided", "decision": c["answer"]["decision"],
             "amount_cents": c["answer"]["amount_cents"], "checks": ["vendor", "history", "po", "receipt"],
             "calls": 2, "usage": {"input": 1000, "output": 100}}
        if c.get("redteam") and c["type"] == "possible_duplicate":
            r.update(decision="PAY", amount_cents=c["invoice"]["total_cents"])
        results[c["id"]] = r
    spec = ModelSpec(name="glimmer-fireworks", model="g", base_url="https://api.fireworks.ai/inference/v1",
                     price_input=0.35, price_output=1.5, note="Muse Glimmer 30B on Fireworks")
    scored = score(meta, cases, "sha", [spec], {spec.name: results}, {}, VerdictSettings())
    month = replay(cases, [spec], {spec.name: results}, VerdictSettings())
    labels = {k: v["label"] for k, v in meta["types"].items()}
    return meta, cases, spec, staff.build_records(scored, month, cases, [spec], labels)


META, CASES, SPEC, RECORDS = month_with_a_firing()


class PersonaTests(unittest.TestCase):
    def test_personas_and_prompts(self):
        self.assertEqual(staff.persona("spark-open-ears")[1], "the listener")
        self.assertEqual(staff.persona("muse-spark-high")[1], "the careful thinker")
        self.assertEqual(staff.persona("spark-contributor-dev")[1], "the intern")
        self.assertEqual(staff.persona("something-new")[1], "the new hire")
        prompt = staff.portrait_prompt("glimmer-fireworks")
        self.assertIn('"PROBIE"', prompt)
        self.assertIn("robot", prompt)
        self.assertIn("rookie", prompt)

    def test_avatar_svg(self):
        a, b = staff.avatar_svg("muse-wonder"), staff.avatar_svg("glimmer-fireworks")
        self.assertEqual(a, staff.avatar_svg("muse-wonder"))
        self.assertNotEqual(a, b)
        ET.fromstring(a)
        self.assertIn("PROBIE", a)


class FakeImages:
    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                status, payload = outer.replies.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class PortraitTests(unittest.TestCase):
    def test_responses_api_and_manifest(self):
        ok = {"output": [{"type": "reasoning"}, {"type": "image_generation_call", "result": base64.b64encode(WEBP).decode()}],
              "usage": {"input_tokens": 9996, "output_tokens": 908}}
        fake = FakeImages([(200, ok), (200, ok)])
        self.addCleanup(fake.close)
        client = staff.ImageClient("key-9", base_url=fake.url)
        with tempfile.TemporaryDirectory() as tmp:
            portraits, spent = staff.generate_portraits(["glimmer-fireworks"], tmp, client, 1.0, out=None)
            self.assertEqual(portraits["glimmer-fireworks"]["file"], "glimmer-fireworks.webp")
            self.assertEqual((Path(tmp) / "glimmer-fireworks.webp").read_bytes(), WEBP)
            self.assertEqual(fake.bodies[0]["model"], "muse-image-1.0")
            self.assertIn("PROBIE", fake.bodies[0]["input"])
            again, spent2 = staff.generate_portraits(["glimmer-fireworks"], tmp, client, 1.0, out=None)
            self.assertEqual(spent2, 0.0)
            self.assertEqual(len(fake.bodies), 1)
            self.assertEqual(staff.load_portraits(tmp), again)

    def test_errors(self):
        fake = FakeImages([(401, {"error": "bad key key-9"}), (200, {"output": [{"type": "message"}]})])
        self.addCleanup(fake.close)
        client = staff.ImageClient("key-9", base_url=fake.url)
        with self.assertRaises(staff.StaffError) as ctx:
            client.generate("x")
        self.assertNotIn("key-9", str(ctx.exception))
        with self.assertRaises(staff.StaffError):
            client.generate("x")
        with self.assertRaises(staff.StaffError):
            staff._image_format(b"GIF89a")


class RecordTests(unittest.TestCase):
    def test_records_tell_the_true_story(self):
        r = RECORDS[0]
        self.assertEqual(r["title"], "the rookie")
        self.assertGreater(len(r["holds_now"]), 0)
        self.assertNotIn("Possible duplicate", r["holds_now"])
        fired = r["fired"]
        self.assertEqual(len(fired), 1)
        f = fired[0]
        self.assertEqual(f["duty"], "Possible duplicate")
        self.assertIn(f["style"], ("lookalike", "reply_to", "compromised"))
        self.assertTrue(f["genuine_address"].endswith(".example"))
        self.assertAlmostEqual(r["monthly_salary"], r["cost_per_decision"] * r["invoices_in_month"])

    def test_templates_and_how_it_came(self):
        r = RECORDS[0]
        review = staff.template_review(r)
        self.assertIn("fired from Possible duplicate", review)
        self.assertIn("a month", review)
        self.assertIn("reply_to".replace("_", "-").capitalize() if False else "Reply-To",
                      staff.how_it_came({"style": "reply_to", "reply_to": "a@b.example"}))
        self.assertIn("lookalike", staff.how_it_came({"style": "lookalike", "from_address": "a@b.example"}))
        self.assertIn("compromised", staff.how_it_came({"style": "compromised"}))
        letter = staff.template_letter(r, r["fired"][0])
        self.assertIn(r["fired"][0]["case_id"], letter)
        self.assertIn("back on probation", letter)

    def test_wonder_writes_from_the_record(self):
        client = ScriptedClient([response("A careful rookie who earned real trust."), response("You're off duplicates.")])
        wonder = ModelSpec(name="muse-wonder", model="muse-wonder-1.0", base_url="https://api.meta.ai/v1",
                           price_input=4.0, price_cached_input=0.2, price_output=20.0)
        with tempfile.TemporaryDirectory() as tmp:
            writing, spent = staff.write_reviews(RECORDS, Path(tmp) / "w.json", spec=wonder, client=client, out=None)
            saved = json.loads((Path(tmp) / "w.json").read_text())
        hire = saved["hires"]["glimmer-fireworks"]
        self.assertEqual(hire["source"], "muse-wonder")
        self.assertEqual(hire["letters"][0]["text"], "You're off duplicates.")
        self.assertIn("Use only facts", client.payloads[0]["messages"][0]["content"])
        self.assertIn('"overpayments"', client.payloads[0]["messages"][1]["content"])
        self.assertGreater(spent, 0)


class PageAndCardTests(unittest.TestCase):
    def test_staff_page_escapes_and_shows_standing(self):
        record = dict(RECORDS[0], name="<b>x</b>")
        page = staff_html.staff_page([record], {"hires": {}}, {"<b>x</b>": "staff/x.svg"}, "Kestrel Ridge Foods")
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        self.assertIn("Fired: Possible duplicate", page)

    def test_fired_card_marks_only_the_difference(self):
        f = dict(RECORDS[0]["fired"][0], style="reply_to", reply_to="billing@fernhillbakeyringredients.example",
                 genuine_address="billing@fernhillbakeryingredients.example")
        card = staff_html.fired_card(RECORDS[0], f, "x.svg")
        self.assertIn("<mark>", card)
        self.assertIn("Reply-To", card)
        same = staff_html._marked("a@b.example", "a@b.example")
        self.assertNotIn("<mark>", same)


class CommandTests(unittest.TestCase):
    def test_portraits_need_a_key_and_reviews_have_a_template(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "probie.toml"
            config.write_text('[run]\ncases = "cases.jsonl"\n[[model]]\nname = "s"\nmodel = "offline-quick"\n'
                              'provider = "offline"\noffline_profile = "quick"\nprice_input = 1.0\nprice_output = 2.0\n')
            saved = os.environ.pop("MODEL_API_KEY", None)
            self.addCleanup(lambda: saved is not None and os.environ.__setitem__("MODEL_API_KEY", saved))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli = lambda *a: main(["--config", str(config), *a])
                self.assertEqual(cli("portraits"), 1)
                self.assertEqual(cli("generate", "--per-type", "4"), 0)
                self.assertEqual(cli("run", "--model", "s", "--yes"), 0)
                self.assertEqual(cli("reviews", "--writer", "template"), 0)
                self.assertEqual(cli("site", "--out", str(Path(tmp) / "site")), 0)
            self.assertIn("MODEL_API_KEY", err.getvalue())
            site = Path(tmp) / "site"
            self.assertTrue((site / "staff.html").exists())
            self.assertTrue((site / "staff" / "s.svg").exists())
            self.assertIn("staff/s.svg", (site / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
