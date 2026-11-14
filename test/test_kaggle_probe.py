import json
import os
import pathlib
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "spikes"))
import kaggle_probe  # noqa: E402

KEY = "test-key-123"


class FakeServe(BaseHTTPRequestHandler):
    """Answers 503 on /v1/models for the first `not_ready` polls, then 200."""

    not_ready = 0
    polls = 0

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self):
        return self.headers.get("Authorization") == f"Bearer {KEY}"

    def do_GET(self):
        if not self._authorised():
            return self._send(401, {"error": "bad key"})
        if self.path == "/v1/models":
            FakeServe.polls += 1
            if FakeServe.polls <= FakeServe.not_ready:
                return self._send(503, {"error": "loading"})
            return self._send(200, {"data": [{"id": "fake"}]})
        self._send(404, {})

    def do_POST(self):
        if not self._authorised():
            return self._send(401, {"error": "bad key"})
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path == "/v1/chat/completions":
            return self._send(200, {"usage": {"completion_tokens": 40}})
        self._send(404, {})


class KaggleProbeTest(unittest.TestCase):
    def setUp(self):
        FakeServe.not_ready = 2
        FakeServe.polls = 0
        self.server = HTTPServer(("127.0.0.1", 0), FakeServe)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_wait_healthy_counts_polls_until_200(self):
        ticks = iter([0.0, 5.0, 10.0, 15.0])
        seconds = kaggle_probe.wait_healthy(
            self.base, KEY, timeout=60, poll=5.0,
            clock=lambda: next(ticks), sleep=lambda _s: None,
        )
        self.assertEqual(seconds, 15.0)
        self.assertEqual(FakeServe.polls, 3)

    def test_wait_healthy_times_out(self):
        FakeServe.not_ready = 10**6
        ticks = iter([0.0, 5.0, 10.0, 15.0])
        with self.assertRaises(TimeoutError):
            kaggle_probe.wait_healthy(
                self.base, KEY, timeout=10, poll=5.0,
                clock=lambda: next(ticks), sleep=lambda _s: None,
            )

    def test_wrong_key_fails_at_once(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            kaggle_probe.wait_healthy(
                self.base, "wrong", timeout=60, poll=5.0,
                clock=lambda: 0.0, sleep=lambda _s: None,
            )
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(FakeServe.polls, 0)

    def test_measure_divides_reported_tokens_by_request_time(self):
        ticks = iter([100.0, 104.0])
        result = kaggle_probe.measure(
            self.base, KEY, "fake", 64, clock=lambda: next(ticks),
        )
        self.assertEqual(result, {"completion_tokens": 40, "seconds": 4.0, "tok_s": 10.0})

    def test_main_reads_key_from_named_env_var_and_prints_json(self):
        out = []
        with mock.patch.dict(os.environ, {"SPIKE_TEST_KEY": KEY}), \
                mock.patch("builtins.print", side_effect=lambda s: out.append(s)), \
                mock.patch.object(kaggle_probe, "wait_healthy", return_value=12.3), \
                mock.patch.object(kaggle_probe, "measure", return_value={"completion_tokens": 40, "seconds": 4.0, "tok_s": 10.0}):
            code = kaggle_probe.main(["--base-url", self.base, "--key-env", "SPIKE_TEST_KEY", "--model", "fake"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out[0]), {
            "model": "fake", "time_to_healthy_s": 12.3,
            "completion_tokens": 40, "seconds": 4.0, "tok_s": 10.0,
        })
        self.assertNotIn(KEY, out[0])

    def test_main_refuses_when_key_env_is_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.stderr"):
            code = kaggle_probe.main(["--base-url", self.base, "--key-env", "SPIKE_MISSING", "--model", "fake"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
