"""The preflight: does `ping` catch a model that can't do what a run needs?"""

import contextlib
import io
import shutil
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from probie.cli import main


class FakeProvider:
    """An OpenAI-compatible server whose behaviour each test chooses."""

    def __init__(self, *, reads_images=True, calls_tools=True):
        self.reads_images, self.calls_tools = reads_images, calls_tools
        self.saw_image = False
        self.invoice_number = ""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                content = payload["messages"][-1].get("content")
                parts = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
                has_image = any(p.get("type") == "image_url" for p in parts)
                text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
                outer.saw_image = outer.saw_image or has_image
                message = outer.answer(payload, text, has_image)
                body = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 50, "completion_tokens": 10}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def answer(self, payload: dict, text: str, has_image: bool) -> dict:
        if "invoice number" in text.lower():
            return {"role": "assistant", "content": self.invoice_number if self.reads_images else "I can't see any image."}
        if payload.get("tools"):
            if not self.calls_tools:
                return {"role": "assistant", "content": "I would hold this invoice."}
            names = {t["function"]["name"] for t in payload["tools"]}
            if "submit_decision" in names and len(names) == 1:
                return {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "submit_decision", "arguments": json.dumps({"decision": "HOLD", "reason": "connection test"})}}]}
            return {"role": "assistant", "tool_calls": [{"id": "c2", "type": "function", "function": {
                "name": "submit_decision", "arguments": json.dumps({"decision": "HOLD", "reason": "the bank details don't match"})}}]}
        return {"role": "assistant", "content": "ready"}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class PingTests(unittest.TestCase):
    """Rendering an invoice is slow, so one set of cases and documents is shared by every test."""

    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        base = Path(cls.shared.name)
        os.environ["PROBIE_TEST_KEY"] = "k"
        (base / "probie.toml").write_text('[run]\ncases = "cases.jsonl"\ndocuments = "documents"\nruns_dir = "runs"\n'
                                          '[[model]]\nname = "m"\nmodel = "m-1"\nbase_url = "http://127.0.0.1:9/v1"\n'
                                          'api_key_env = "PROBIE_TEST_KEY"\nprice_input = 1.0\nprice_output = 2.0\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            assert main(["--config", str(base / "probie.toml"), "generate", "--per-type", "2"]) == 0
            assert main(["--config", str(base / "probie.toml"), "render"]) == 0
        cls.cases = [json.loads(l) for l in (base / "cases.jsonl").read_text().splitlines()[1:]]

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()
        os.environ.pop("PROBIE_TEST_KEY", None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        base = Path(self.shared.name)
        shutil.copy(base / "cases.jsonl", self.dir / "cases.jsonl")
        shutil.copytree(base / "documents", self.dir / "documents")

    def write_config(self, url: str, invoice_input: str = "document") -> Path:
        path = self.dir / "probie.toml"
        path.write_text(f'''[run]
cases = "cases.jsonl"
documents = "documents"
runs_dir = "runs"
[[model]]
name = "m"
model = "m-1"
base_url = "{url}"
api_key_env = "PROBIE_TEST_KEY"
invoice_input = "{invoice_input}"
price_input = 1.0
price_output = 2.0
''')
        return path

    def run_cli(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--config", str(self.dir / "probie.toml"), *argv])
        return code, out.getvalue()

    def test_a_good_model_passes_every_check(self):
        fake = FakeProvider()
        self.addCleanup(fake.close)
        self.write_config(fake.url)
        fake.invoice_number = self.cases[0]["invoice"]["number"]
        code, out = self.run_cli("ping", "--model", "m")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("[ok  ]"), 4, out)
        self.assertIn("is ready to run", out)
        self.assertTrue(fake.saw_image, "vision was never actually tested")

    def test_a_model_that_cant_read_the_image_fails_and_says_what_to_change(self):
        fake = FakeProvider(reads_images=False)
        self.addCleanup(fake.close)
        self.write_config(fake.url)
        code, out = self.run_cli("ping", "--model", "m")
        self.assertEqual(code, 1, out)
        self.assertIn("[FAIL] Reads the invoice image", out)
        self.assertIn('invoice_input = "fields"', out)
        self.assertIn("isn't ready", out)

    def test_a_model_that_cant_call_tools_says_use_prompt_mode(self):
        fake = FakeProvider(calls_tools=False)
        self.addCleanup(fake.close)
        self.write_config(fake.url, invoice_input="fields")
        code, out = self.run_cli("ping", "--model", "m")
        self.assertEqual(code, 1, out)
        self.assertIn("[FAIL] Tool calling", out)
        self.assertIn('mode = "prompt"', out)
        self.assertIn("skipped, this model gets the invoice as fields", out)

    def test_an_unreachable_model_reports_every_check_without_crashing(self):
        self.write_config("http://127.0.0.1:9/v1")
        code, out = self.run_cli("ping", "--model", "m")
        self.assertEqual(code, 1, out)
        self.assertIn("[FAIL] Reachable", out)
        self.assertIn("One whole case", out)  # it keeps going instead of stopping at the first failure

    def test_all_checks_every_model_and_writes_a_report(self):
        good = FakeProvider()
        blind = FakeProvider(reads_images=False)
        self.addCleanup(good.close)
        self.addCleanup(blind.close)
        good.invoice_number = self.cases[0]["invoice"]["number"]
        (self.dir / "probie.toml").write_text(f"""[run]
cases = "cases.jsonl"
documents = "documents"
runs_dir = "runs"
[[model]]
name = "sharp"
model = "m-1"
base_url = "{good.url}"
api_key_env = "PROBIE_TEST_KEY"
invoice_input = "document"
price_input = 1.0
price_output = 2.0
[[model]]
name = "blind"
model = "m-2"
base_url = "{blind.url}"
api_key_env = "PROBIE_TEST_KEY"
invoice_input = "document"
price_input = 1.0
price_output = 2.0
[[model]]
name = "keyless"
model = "m-3"
base_url = "{good.url}"
api_key_env = "PROBIE_MISSING_KEY"
price_input = 1.0
price_output = 2.0
""")
        code, out = self.run_cli("ping", "--all")
        self.assertEqual(code, 1, out)          # one model is broken, so this fails a script
        self.assertIn("Ready: sharp", out)
        self.assertIn("Not ready: blind", out)
        self.assertIn("set PROBIE_MISSING_KEY", out)
        self.assertIn("The whole preflight cost", out)
        report = (self.dir / "runs" / "preflight.txt").read_text()
        self.assertIn("Probie preflight, 3 models", report)
        self.assertIn("[FAIL] Reads the invoice image", report)
        self.assertIn('invoice_input = "fields"', report)
        self.assertNotIn("PROBIE_TEST_KEY=", report)

    def test_all_passes_when_every_model_is_ready(self):
        good = FakeProvider()
        self.addCleanup(good.close)
        good.invoice_number = self.cases[0]["invoice"]["number"]
        self.write_config(good.url)
        code, out = self.run_cli("ping", "--all", "--report", str(self.dir / "pf.txt"))
        self.assertEqual(code, 0, out)
        self.assertIn("Ready: m", out)
        self.assertIn("Not ready: none", out)
        self.assertTrue((self.dir / "pf.txt").exists())

    def test_ping_needs_a_model_or_all(self):
        self.write_config("http://127.0.0.1:9/v1")
        code, out = self.run_cli("ping")
        self.assertEqual(code, 1)
        self.assertIn("--all", out)

    def test_ping_records_what_it_spent(self):
        fake = FakeProvider()
        self.addCleanup(fake.close)
        self.write_config(fake.url)
        fake.invoice_number = self.cases[0]["invoice"]["number"]
        self.run_cli("ping", "--model", "m")
        spend = (self.dir / "runs" / "spend.jsonl").read_text()
        self.assertIn('"what": "ping"', spend)


if __name__ == "__main__":
    unittest.main()
