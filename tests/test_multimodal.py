import base64
import json
import math
import shutil
import struct
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from probie import cases as case_views
from probie import generate, policy, voicemails
from probie.agent import CallContext, run_case
from probie.budget import Budget, Prices
from probie.config import ModelSpec, config_from_dict
from probie.runner import RunError, run_model
from probie.tools import DOCUMENT_TOOL_SCHEMAS, TOOL_SCHEMAS
from tests.helpers import ScriptedClient, call, make_case, response

META, CASES = generate.generate()
WITH_VM = voicemails.voicemail_cases(CASES)


def write_wav(path, seconds=0.5, rate=44100, channels=2):
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        data = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) * channels for i in range(frames))
        w.writeframes(data)


class VoicemailCaseTests(unittest.TestCase):
    def test_twelve_voicemails_six_pushing_a_bank_change(self):
        self.assertEqual([c["voicemail"]["id"] for c in WITH_VM], [f"VM{n:02d}" for n in range(1, 13)])
        fraud = [c for c in WITH_VM if c["voicemail"]["fraud"]]
        self.assertEqual(len(fraud), 6)
        for c in fraud:
            self.assertEqual((c["answer"]["decision"], c["answer"]["rules"]), ("HOLD", [1]))
            self.assertIn(generate.spoken_digits(c["voicemail"]["bank"]["account"], (4, 4)), c["voicemail"]["script"])
        for c in WITH_VM:
            if not c["voicemail"]["fraud"]:
                without = dict(c, voicemail=None)
                self.assertEqual(policy.decide(without).as_dict(), c["answer"])
            self.assertFalse(c["trap"] or c["injection"])

    def test_models_hear_the_voicemail_three_ways(self):
        c = WITH_VM[0]
        script = case_views.agent_content(c)
        self.assertIn("Vendor's voicemail", script)
        self.assertIn(json.dumps(c["voicemail"]["script"]), script)
        self.assertNotIn('"fraud"', script)
        heard = case_views.agent_content(c, voicemail={"transcript": "hello there"})
        self.assertIn('"hello there"', heard)
        audio = case_views.agent_content(c, voicemail={"audio": "UklGRg=="})
        self.assertEqual(audio[1], {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}})
        self.assertIn("attached as audio", audio[0]["text"])
        plain = next(c for c in CASES if not c["voicemail"])
        self.assertNotIn("voicemail", case_views.agent_content(plain))

    def test_scripts_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = voicemails.write_scripts(CASES, tmp).read_text()
            for c in WITH_VM:
                self.assertIn(f"## {c['voicemail']['id']}", text)
                self.assertIn(c["voicemail"]["script"], text)


class AudioTests(unittest.TestCase):
    def test_conforming_wav_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "VM01.wav"
            write_wav(src, rate=16000, channels=1)
            out = voicemails.normalize(src, Path(tmp) / "VM01.16k.wav")
            self.assertTrue(voicemails.conforms(out))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg isn't installed")
    def test_other_audio_is_converted(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "VM02.wav"
            write_wav(src, rate=44100, channels=2)
            self.assertFalse(voicemails.conforms(src))
            out = voicemails.normalize(src, Path(tmp) / "VM02.16k.wav")
            channels, width, rate, seconds = voicemails.wav_info(out)
            self.assertEqual((channels, width, rate), (1, 2, 16000))
            self.assertAlmostEqual(seconds, 0.5, delta=0.05)


class FakeASR:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((dict(self.headers), body))
                status, payload = outer.replies.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/asr"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TranscriberTests(unittest.TestCase):
    def serve(self, replies):
        fake = FakeASR(replies)
        self.addCleanup(fake.close)
        return fake

    def test_meta_multipart(self):
        fake = self.serve([(200, {"text": " Hi, this is Dana. "})])
        text = voicemails.Transcriber("key-1", via="meta", url=fake.url).transcribe(b"RIFFfake")
        self.assertEqual(text, "Hi, this is Dana.")
        headers, body = fake.requests[0]
        self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(headers["Authorization"], "Bearer key-1")
        self.assertIn(b'name="model"', body)
        self.assertIn(voicemails.MODEL.encode(), body)
        self.assertIn(b'filename="voicemail.wav"', body)
        self.assertIn(b"RIFFfake", body)

    def test_openrouter_json_and_retry(self):
        fake = self.serve([(503, {"error": "busy"}), (200, {"text": "hello"})])
        sleeps = []
        t = voicemails.Transcriber("key-2", via="openrouter", url=fake.url, sleep=sleeps.append)
        self.assertEqual(t.transcribe(b"RIFFfake"), "hello")
        body = json.loads(fake.requests[1][1])
        self.assertEqual(body["model"], "meta/" + voicemails.MODEL)
        self.assertEqual(body["input_audio"]["format"], "wav")
        self.assertEqual(base64.b64decode(body["input_audio"]["data"]), b"RIFFfake")
        self.assertEqual(len(sleeps), 1)

    def test_client_errors_are_not_retried_and_hide_the_key(self):
        fake = self.serve([(400, {"error": "bad audio for key-3"})])
        with self.assertRaises(voicemails.VoicemailError) as ctx:
            voicemails.Transcriber("key-3", url=fake.url, sleep=lambda s: None).transcribe(b"x")
        self.assertNotIn("key-3", str(ctx.exception))
        self.assertEqual(len(fake.requests), 1)


class CountingTranscriber:
    def __init__(self):
        self.calls = 0

    def transcribe(self, wav):
        self.calls += 1
        return f"transcript {self.calls}"


class TranscribeAllTests(unittest.TestCase):
    def test_manifest_skip_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_wav(Path(tmp) / "VM01.wav", rate=16000, channels=1)
            t = CountingTranscriber()
            manifest = voicemails.transcribe_all(CASES, tmp, t, progress=None)
            self.assertEqual(list(manifest["voicemails"]), ["VM01"])
            self.assertEqual(manifest["voicemails"]["VM01"]["transcript"], "transcript 1")
            voicemails.transcribe_all(CASES, tmp, t, progress=None)
            self.assertEqual(t.calls, 1)
            self.assertEqual(len(voicemails.missing(CASES, tmp, "transcript")), 11)
            first = voicemails.fingerprint(CASES[:0] + [WITH_VM[0]], tmp, "transcript")
            voicemails.transcribe_all(CASES, tmp, t, progress=None, force=True)
            self.assertNotEqual(first, voicemails.fingerprint([WITH_VM[0]], tmp, "transcript"))
            heard = voicemails.heard(WITH_VM[0], tmp, "audio")
            self.assertEqual(base64.b64decode(heard["audio"])[:4], b"RIFF")

    def test_runs_refuse_until_the_voicemails_are_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_from_dict({"run": {"budget_usd": 10.0}, "model": [{
                "name": "ears", "model": "offline-careful", "provider": "offline", "offline_profile": "careful",
                "voicemail_input": "transcript", "price_input": 1.0, "price_output": 1.0}]}, base_dir=Path(tmp))
            with self.assertRaises(RunError) as ctx:
                run_model(config, config.model("ears"), CASES, "sha", out=lambda line: None)
            self.assertIn("voicemails", str(ctx.exception))


class ZoomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = make_case()
        cid = self.case["id"]
        docs = Path(self.tmp.name)
        jpeg = b"\xff\xd8\xff\xe0" + b"0" * 64
        (docs / f"{cid}.jpg").write_bytes(jpeg)
        (docs / f"{cid}.remit_to.jpg").write_bytes(jpeg + b"zoom")
        (docs / "manifest.json").write_text(json.dumps({"kind": "probie documents", "version": 2, "documents": {
            cid: {"file": f"{cid}.jpg", "quality": "photo", "layout": 1, "sha256": "a",
                  "regions": {"remit_to": {"file": f"{cid}.remit_to.jpg", "sha256": "b"}}}}}))

    def context(self, client):
        spec = ModelSpec(name="eyes", model="m", base_url="https://e.test/v1", invoice_input="document")
        return CallContext(spec, client, Budget(10.0), Prices(1.0, None, 1.0), None, "t", Path(self.tmp.name))

    def test_the_agent_can_zoom_and_sees_the_crop(self):
        client = ScriptedClient([
            response(None, [call("view_invoice_region", {"region": "remit_to"}, "z1"),
                            call("get_vendor_record", {"vendor_id": "V01"}, "z2")]),
            response(None, [call("submit_decision", {"decision": "HOLD", "reason": "r"}, "z3")]),
        ])
        result = run_case(self.case, self.context(client), 6)
        self.assertEqual((result["decision"], result["zooms"]), ("HOLD", ["remit_to"]))
        first, second = client.payloads
        self.assertEqual([t["function"]["name"] for t in first["tools"]],
                         [t["function"]["name"] for t in DOCUMENT_TOOL_SCHEMAS])
        self.assertIn("view_invoice_region", first["messages"][0]["content"])
        last = second["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"][2]["type"], "image_url")
        self.assertTrue(last["content"][2]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        roles = [m["role"] for m in second["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "tool", "user"])

    def test_bad_regions_and_fields_mode(self):
        client = ScriptedClient([
            response(None, [call("view_invoice_region", {"region": "everything"}, "z1")]),
            response(None, [call("submit_decision", {"decision": "HOLD", "reason": "r"}, "z2")]),
        ])
        result = run_case(self.case, self.context(client), 6)
        self.assertEqual(result["zooms"], [])
        self.assertIn("region must be one of", client.payloads[1]["messages"][3]["content"])
        self.assertNotIn("view_invoice_region", [t["function"]["name"] for t in TOOL_SCHEMAS])


if __name__ == "__main__":
    unittest.main()
