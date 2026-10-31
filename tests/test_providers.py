import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from probie.providers import HTTPChatClient, ProviderError, RateLimiter, check_response, retry_after_seconds

OK = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ready"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
SECRET = "sk-test-secret-123"


class FakeServer:
    """Replies with a scripted list of (status, headers, body, delay) and records requests."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                server.requests.append({"path": self.path, "headers": dict(self.headers),
                                        "body": json.loads(self.rfile.read(length))})
                status, headers, body, delay = server.script.pop(0) if server.script else (500, {}, b"empty", 0)
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    data = body if isinstance(body, bytes) else json.dumps(body).encode()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ClientTests(unittest.TestCase):
    def serve(self, script):
        server = FakeServer(script)
        self.addCleanup(server.close)
        return server

    def client(self, server, **kwargs):
        sleeps = []
        kwargs.setdefault("timeout", 5)
        client = HTTPChatClient(server.url + "/", SECRET, sleep=sleeps.append, **kwargs)
        return client, sleeps

    def test_success_and_request_shape(self):
        server = self.serve([(200, {}, OK, 0)])
        client, sleeps = self.client(server)
        payload = {"model": "muse-spark-1.3", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "low"}
        self.assertEqual(client.chat(payload), OK)
        request = server.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["body"], payload)
        self.assertEqual(sleeps, [])

    def test_no_key_sends_no_auth_header(self):
        server = self.serve([(200, {}, OK, 0)])
        HTTPChatClient(server.url, None, sleep=lambda s: None).chat({"model": "m", "messages": []})
        self.assertNotIn("Authorization", server.requests[0]["headers"])

    def test_retries_429_using_retry_after(self):
        server = self.serve([(429, {"Retry-After": "7"}, {"error": "slow down"}, 0), (200, {}, OK, 0)])
        client, sleeps = self.client(server)
        self.assertEqual(client.chat({"model": "m", "messages": []}), OK)
        self.assertEqual(sleeps, [7.0])
        self.assertEqual(len(server.requests), 2)

    def test_retries_5xx_with_backoff_then_gives_up(self):
        server = self.serve([(503, {}, b"busy", 0)] * 3)
        client, sleeps = self.client(server, max_retries=2)
        with self.assertRaises(ProviderError) as ctx:
            client.chat({"model": "m", "messages": []})
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.status, 503)
        self.assertIn("gave up after 3 attempts", str(ctx.exception))
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0, sleeps)

    def test_4xx_is_not_retried_and_hides_the_key(self):
        server = self.serve([(401, {}, {"error": {"message": f"bad key {SECRET}"}}, 0)])
        client, sleeps = self.client(server)
        with self.assertRaises(ProviderError) as ctx:
            client.chat({"model": "m", "messages": []})
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertIn("[redacted]", str(ctx.exception))
        self.assertEqual(sleeps, [])

    def test_invalid_json_and_bad_shapes_are_retried(self):
        server = self.serve([(200, {}, b"not json", 0), (200, {}, {"choices": []}, 0), (200, {}, OK, 0)])
        client, sleeps = self.client(server)
        self.assertEqual(client.chat({"model": "m", "messages": []}), OK)
        self.assertEqual(len(sleeps), 2)

    def test_error_object_with_200_is_not_retried(self):
        server = self.serve([(200, {}, {"error": {"message": "unknown model"}}, 0)])
        client, _ = self.client(server)
        with self.assertRaises(ProviderError) as ctx:
            client.chat({"model": "m", "messages": []})
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("unknown model", str(ctx.exception))

    def test_timeout_is_retried(self):
        server = self.serve([(200, {}, OK, 1.0), (200, {}, OK, 0)])
        client, sleeps = self.client(server, timeout=0.3)
        self.assertEqual(client.chat({"model": "m", "messages": []}), OK)
        self.assertEqual(len(sleeps), 1)

    def test_unreachable_host(self):
        client = HTTPChatClient("http://127.0.0.1:9/v1", SECRET, timeout=1, max_retries=1, sleep=lambda s: None)
        with self.assertRaises(ProviderError) as ctx:
            client.chat({"model": "m", "messages": []})
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("couldn't reach", str(ctx.exception))


class HelperTests(unittest.TestCase):
    def test_retry_after(self):
        self.assertIsNone(retry_after_seconds(None))
        self.assertIsNone(retry_after_seconds("soon"))
        self.assertEqual(retry_after_seconds("3"), 3.0)
        self.assertEqual(retry_after_seconds("-4"), 0.0)
        self.assertEqual(retry_after_seconds("9999"), 120.0)
        self.assertAlmostEqual(retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT", now=1792567670.0), 10.0, places=3)

    def test_check_response(self):
        self.assertEqual(check_response(OK), OK)
        for bad in ([], {"choices": None}, {"choices": [{"message": None}]}, {"choices": ["x"]}):
            with self.assertRaises(ProviderError):
                check_response(bad)

    def test_rate_limiter_spaces_requests(self):
        now = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(round(seconds, 6))

        limiter = RateLimiter(120, clock=lambda: now[0], sleep=sleep)
        for _ in range(3):
            limiter.wait()
        self.assertEqual(slept, [0.5, 1.0])
        self.assertIsNone(RateLimiter(None).wait())


if __name__ == "__main__":
    unittest.main()
