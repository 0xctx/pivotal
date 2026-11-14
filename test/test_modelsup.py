"""models-up (engine/modelsup.py): reuse, the kernel status as the cross-repo lock, one push, a
12-minute poll, hostile endpoint answers, the grammar of what reaches argv, the keyless check and the secret scan.

Everything is a fake: test/fakekaggle.py's FakeKernel is a stateful `kaggle` executable (one kernel shared by every
caller, run as a real subprocess), and the endpoint is a local HTTP server. No test reaches Kaggle, ngrok or the network;
the real client is pointed at the local server with its `connect` parameter. The clock and sleep of the decision are injected.
"""

import contextlib
import http.client
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import fakekaggle
import nolive  # noqa: F401  (no test sweeps the live /proc)

from engine import buildjob, job, modelsup, plan, route

ROOT = Path(__file__).resolve().parent.parent
KEY = "endpoint-key-0123456789abcdef"
KAGGLE_KEY = "kaggle-account-key-fedcba9876543210"
USER = "alice"
MODEL = "qwen3.6-35b-a3b"
URL = "https://pivotal-serve.ngrok-free.dev"
MODELS = json.dumps({"object": "list", "data": [{"id": "x.gguf"}]})


class KaggleApiTokenTest(unittest.TestCase):
    def test_an_api_token_is_also_passed_as_kaggle_api_token(self):
        token = "KGAT_0123456789abcdef0123456789abcdef"
        self.assertEqual(modelsup.Kaggle("kaggle", {}, USER, token).env["KAGGLE_API_TOKEN"], token)

    def test_a_legacy_key_is_not_passed_as_an_api_token(self):
        self.assertNotIn("KAGGLE_API_TOKEN", modelsup.Kaggle("kaggle", {}, USER, KAGGLE_KEY).env)


class NeverPushedTest(unittest.TestCase):
    """kaggle's measured answer for a kernel that was never pushed is the one denial read as "none" (an idle kernel). Any
    other failure of a kernel status is "unknown", which pushes nothing."""

    def word(self, text: str) -> str:
        kaggle = modelsup.Kaggle("kaggle", {}, USER, KAGGLE_KEY)
        with mock.patch.object(kaggle, "run", return_value=(1, text)):
            return kaggle.word()

    def test_the_measured_never_pushed_sentence_is_none(self):
        self.assertEqual(self.word("Cannot access kernel 'alice/pivotal-serve' (Permission 'kernels.get' was denied). "
                                   "The most likely cause is a wrong kernel slug.\n"), "none")

    def test_another_permission_on_a_kernel_is_unknown_and_not_never_pushed(self):
        for text in ("Cannot access kernel 'alice/pivotal-serve' (Permission 'kernels.push' was denied)",
                     "Cannot access kernel 'alice/pivotal-serve' (Permission 'kernels.list' was denied)",
                     "Cannot access kernel 'alice/pivotal-serve'"):
            with self.subTest(text):
                self.assertEqual(self.word(text + "\n"), "unknown")

    def test_a_kernel_access_message_alone_is_unknown(self):
        self.assertEqual(self.word("Cannot access kernel\n"), "unknown")

    def test_a_permission_denial_that_is_not_about_a_kernel_is_unknown(self):
        for text in ("Permission 'kernels.get' was denied\n", "Permission 'datasets.get' was denied\n"):
            with self.subTest(text):
                self.assertEqual(self.word(text), "unknown")


class Endpoint:
    """A local model server. ``mode`` picks how /v1/models answers; ``kernel`` (a FakeKernel) makes it up only when the
    kernel's state file says so. Every request is recorded: (method, path, whether it carried the key)."""

    def __init__(self, mode: str = "down", kernel: fakekaggle.FakeKernel | None = None, other: "Endpoint | None" = None):
        self.mode, self.kernel, self.other = mode, kernel, other
        self.requests: list[tuple[str, str, bool]] = []
        self.open_routes: set[str] = set()  # routes that answer 200 without the key (the keyless check must catch them)
        self.bytes_sent = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def send_all(self, status, body=b"", headers=()):
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                keyed = self.headers.get("Authorization") == f"Bearer {KEY}"
                outer.requests.append((self.command, self.path, keyed))
                if self.path == "/health":
                    return self.send_all(200, b'{"status":"ok"}')
                if self.path != "/v1/models" or self.command != "GET":
                    if keyed or self.path in outer.open_routes:
                        return self.send_all(200, b"{}")
                    return self.send_all(401, b'{"error":"key"}')
                mode = outer.mode
                if outer.kernel is not None:
                    mode = "live" if outer.kernel.read()["up"] else "down"
                if mode == "live":
                    return self.send_all(200, MODELS.encode()) if keyed else self.send_all(401, b"{}")
                if mode == "down":
                    return self.send_all(404, b"<html>ERR_NGROK_3200</html>")
                if mode == "loading":
                    return self.send_all(503, b'{"error":{"code":503,"message":"Loading model"}}')
                if mode == "unauthorized":
                    return self.send_all(401, b'{"error":"bad key"}')
                if mode == "forbidden":
                    return self.send_all(403, b"{}")
                if mode == "nonjson":
                    return self.send_all(200, b"<html>welcome</html>")
                if mode == "nodata":
                    return self.send_all(200, b'{"models": []}')
                if mode == "emptydata":
                    return self.send_all(200, b'{"data": []}')
                if mode == "deep":
                    return self.send_all(200, b"[" * 100000)
                if mode == "redirect":
                    target = f"http://127.0.0.1:{outer.other.port}/v1/models" if outer.other else "https://evil.example/"
                    return self.send_all(302, b"", [("Location", target)])
                if mode == "trickle":  # a valid 200 whose body arrives one byte at a time
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    try:
                        for _ in range(100000):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.005)
                    except OSError:
                        pass
                    return None
                if mode == "huge":
                    chunk = b"x" * 65536
                    self.send_response(200)
                    self.send_header("Content-Length", str(1 << 28))
                    self.end_headers()
                    try:
                        for _ in range(4096):
                            self.wfile.write(chunk)
                            outer.bytes_sent += len(chunk)
                    except OSError:
                        pass
                    return None
                return self.send_all(500, b"")

            do_GET = do_POST = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def connect(self, _domain: str) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def keyed(self, path: str = "/v1/models") -> int:
        return sum(1 for _m, p, k in self.requests if p == path and k)


class Clock:
    """An injected clock: sleeping advances it, nothing else does."""

    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.kernel = fakekaggle.FakeKernel(self.root / "bin")
        self.out = self.root / "output"
        self.out.write_text("")
        self.servers: list[Endpoint] = []

    def endpoint(self, mode="down", kernel=None, other=None) -> Endpoint:
        server = Endpoint(mode, kernel, other)
        self.servers.append(server)
        self.addCleanup(server.close)
        return server

    def env(self, **over) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(self.root), "GITHUB_OUTPUT": str(self.out),
            "PIVOTAL_KAGGLE_URL": URL, "PIVOTAL_KAGGLE_KEY": KEY, "KAGGLE_USERNAME": USER, "KAGGLE_KEY": KAGGLE_KEY,
            "KAGGLE_MODEL": MODEL, "PIV_KAGGLE": str(self.kernel.path),
        }
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def run_job(self, endpoint: Endpoint, **over) -> tuple[dict[str, str], str]:
        """`modelsup.run` in-process against the local endpoint, with the waits made short. (outputs, what it printed)."""
        printed = io.StringIO()
        with mock.patch.object(modelsup, "POLL_SECONDS", 0.01), mock.patch.object(modelsup, "GRACE_SECONDS", 0.0), mock.patch.object(modelsup, "PROBE_SECONDS", 1.0), \
                mock.patch.object(modelsup, "WAIT_SECONDS", over.pop("wait", 3.0)), contextlib.redirect_stdout(printed):
            modelsup.run(self.env(**over), connect=endpoint.connect)
        return self.outputs(), printed.getvalue()

    def outputs(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.out.read_text().splitlines())

    def pushes(self) -> int:
        return len(self.kernel.pushes())

    def kaggle_reads(self) -> int:
        return sum(1 for c in self.kernel.calls() if c["argv"][:2] == ["kernels", "status"])


class DecisionTest(unittest.TestCase):
    """The logic of decide with scripted answers and an injected clock."""

    def deps(self, endpoint, kernel, clock=None, pushes=None):
        clock = clock or Clock()
        pushes = pushes if pushes is not None else []
        answers, words = iter(endpoint), iter(kernel)
        last = {"a": "down", "w": "complete"}

        def ask():
            last["a"] = next(answers, last["a"])
            return last["a"]

        def read(timeout=None):
            last["w"] = next(words, last["w"])
            return last["w"]

        return modelsup.Deps(ask, read, lambda: pushes.append(1), clock, clock.sleep, lambda _t: None), pushes, clock

    def test_a_live_session_is_reused_without_reading_or_pushing(self):
        deps, pushes, _ = self.deps(["up"], [])
        deps.kernel = mock.Mock(side_effect=AssertionError("kaggle must not be asked"))
        self.assertEqual(modelsup.decide(deps), ("live", False))
        self.assertEqual(pushes, [])

    def test_a_running_kernel_means_wait_without_pushing(self):
        deps, pushes, clock = self.deps(["down", "down", "down", "up"], ["running"])
        self.assertEqual(modelsup.decide(deps), ("waited", False))
        self.assertEqual(pushes, [])
        self.assertGreater(clock.slept, 0)

    def test_a_queued_kernel_means_wait_without_pushing(self):
        deps, pushes, _ = self.deps(["down", "down", "up"], ["queued"])
        self.assertEqual(modelsup.decide(deps), ("waited", False))
        self.assertEqual(pushes, [])

    def test_an_idle_kernel_and_a_down_endpoint_pushes_once_then_waits(self):
        for idle in ("complete", "error", "cancelled", "none"):
            with self.subTest(idle=idle):
                deps, pushes, _ = self.deps(["down", "down", "down", "up"], [idle, idle, "running"])
                self.assertEqual(modelsup.decide(deps), ("started", True))
                self.assertEqual(pushes, [1])

    def test_a_push_by_another_run_between_the_two_looks_is_not_duplicated(self):
        deps, pushes, _ = self.deps(["down", "down", "up"], ["complete", "queued", "running"])
        self.assertEqual(modelsup.decide(deps), ("waited", False))
        self.assertEqual(pushes, [])

    def test_a_session_that_came_up_between_the_two_looks_is_reused(self):
        deps, pushes, _ = self.deps(["down", "up"], ["complete"])
        self.assertEqual(modelsup.decide(deps), ("live", False))
        self.assertEqual(pushes, [])

    def test_the_poll_ends_at_twelve_minutes_with_the_reason_it_saw(self):
        for words, why in ((["running"], "not_answering"), (["queued"], "queued"), (["unknown"], "timeout")):
            with self.subTest(words=words):
                deps, pushes, clock = self.deps(["down"], words)
                got = modelsup.decide(deps)
                self.assertEqual(got, (why, False) if words != ["unknown"] else ("kernel_unreadable", False))
                if words == ["unknown"]:
                    continue
                self.assertEqual(pushes, [])
                self.assertGreaterEqual(clock.slept, modelsup.WAIT_SECONDS - modelsup.PROBE_SECONDS)
                self.assertLessEqual(clock.slept, modelsup.WAIT_SECONDS)
        self.assertEqual(modelsup.WAIT_SECONDS, 12 * 60)

    def test_the_whole_wait_including_the_last_look_and_a_hung_status_read_ends_by_twelve_minutes(self):
        # F5: the probe takes its 10 s and a status read hangs to whatever it is given (60 s at most); several poll
        # lengths, so some sleep and some status read straddles the end
        for poll in (20, 45, 7, 90, 33):
            with self.subTest(poll=poll), mock.patch.object(modelsup, "POLL_SECONDS", poll):
                clock = Clock()
                asked = []

                def endpoint(clock=clock):
                    clock.now += modelsup.PROBE_SECONDS
                    return "down"

                def kernel(timeout=modelsup.KAGGLE_SECONDS, clock=clock, asked=asked):
                    asked.append(timeout)
                    clock.now += min(timeout, modelsup.KAGGLE_SECONDS)
                    return "running"

                deps = modelsup.Deps(endpoint, kernel, lambda: None, clock, clock.sleep, lambda _t: None)
                start = clock.now
                # the looks before the wait are budgeted apart
                self.assertEqual(modelsup.wait(deps, False, "running"), ("not_answering", False))
                self.assertLessEqual(clock.now - start, modelsup.WAIT_SECONDS)
                self.assertGreater(clock.now - start, modelsup.WAIT_SECONDS - 2 * modelsup.PROBE_SECONDS - 2 * poll - 60)

    def test_our_own_session_that_stays_queued_is_reported_queued_after_one_push(self):
        deps, pushes, clock = self.deps(["down"], ["complete", "complete", "queued"])
        self.assertEqual(modelsup.decide(deps), ("queued", True))
        self.assertEqual(pushes, [1])
        self.assertLess(clock.slept, modelsup.WAIT_SECONDS + modelsup.POLL_SECONDS * 2)

    def test_a_kernel_that_ends_before_the_endpoint_answers_is_failed_after_two_reads_and_the_grace(self):
        deps, pushes, clock = self.deps(["down"], ["complete", "complete", "error"])
        self.assertEqual(modelsup.decide(deps), ("failed", True))
        self.assertEqual(pushes, [1])
        self.assertGreaterEqual(clock.slept, modelsup.GRACE_SECONDS, "a status read right after the push may be the old run's")

    def test_a_refused_push_is_a_reason_and_not_a_retry(self):
        attempts = []

        def refuse():
            attempts.append(1)
            raise modelsup.Refused("quota")

        deps, _, clock = self.deps(["down"], ["complete"])
        deps.push = refuse
        self.assertEqual(modelsup.decide(deps), ("quota", False))
        self.assertEqual(attempts, [1])
        self.assertEqual(clock.slept, 0)

    def test_an_unreadable_kernel_status_is_never_a_reason_to_push(self):
        deps, pushes, _ = self.deps(["down"], ["unknown"])
        self.assertEqual(modelsup.decide(deps), ("kernel_unreadable", False))
        self.assertEqual(pushes, [])

    def test_without_a_kaggle_account_a_down_endpoint_is_a_reason(self):
        deps, _, _ = self.deps(["down"], [])
        deps.kernel = deps.push = None
        self.assertEqual(modelsup.decide(deps), ("no_credentials", False))

    def test_a_401_or_an_odd_answer_stops_at_once_with_no_kaggle_call(self):
        for answer, why in (("unauthorized", "rejected_key"), ("odd", "odd_answer")):
            with self.subTest(answer=answer):
                deps, pushes, _ = self.deps([answer], [])
                deps.kernel = mock.Mock(side_effect=AssertionError)
                self.assertEqual(modelsup.decide(deps), (why, False))
                self.assertEqual(pushes, [])

    def test_a_401_while_waiting_stops_the_wait(self):
        deps, _, clock = self.deps(["down", "unauthorized"], ["running"])
        self.assertEqual(modelsup.decide(deps), ("rejected_key", False))
        self.assertLess(clock.slept, modelsup.POLL_SECONDS * 3)

    def test_no_single_model_means_no_push(self):
        deps, pushes, _ = self.deps(["down"], ["complete"])
        self.assertEqual(modelsup.decide(deps, has_model=False), ("no_model", False))
        self.assertEqual(pushes, [])


class RaceTest(Case):
    def test_serial_case_a_run_that_looks_after_the_first_pushed_starts_no_second_session(self):
        # What this proves: B looks after A pushed (the kernel is queued or running), so exactly one push. It does NOT prove
        # the concurrent case: see the next test and docs/engines.md (one push per start window).
        endpoint = self.endpoint(kernel=self.kernel)
        self.kernel.write(status="COMPLETE", up_after=4)
        results = {}

        def deps(name):
            kaggle = modelsup.Kaggle(str(self.kernel.path), self.env(), USER, KAGGLE_KEY)
            cfg = modelsup.read_config(self.env())
            clock = Clock()

            def sleep(seconds):
                clock.sleep(seconds)
                if name == "a" and "b" not in results and not started_b:  # run B starts while A waits for its session
                    started_b.append(1)
                    results["b"] = modelsup.decide(deps("b"))
            return modelsup.Deps(lambda: modelsup.endpoint_state(cfg, connect=endpoint.connect), kaggle.word,
                                 lambda: modelsup.push_notebook(kaggle, USER, MODEL), clock, sleep, lambda _t: None)

        started_b: list[int] = []
        results["a"] = modelsup.decide(deps("a"))
        self.assertEqual(results["a"], ("started", True))
        self.assertEqual(results["b"], ("waited", False))
        self.assertEqual(self.pushes(), 1, "one kernel push for two runs")

    def test_a_run_that_starts_after_a_session_is_up_reuses_it_and_reads_no_kernel(self):
        endpoint = self.endpoint(kernel=self.kernel)
        self.kernel.write(status="RUNNING", up=True)
        outputs, _ = self.run_job(endpoint)
        self.assertEqual(outputs, {"kaggle_ok": "true", "kaggle_why": "live"})
        self.assertEqual((self.pushes(), self.kernel.calls()), (0, []))

    def test_concurrent_runs_each_push_at_most_once_and_all_end_with_a_session_without_claiming_one_push(self):
        # F2: check-then-push is not atomic and nothing serialises repos (the caller's concurrency is per issue), so two runs
        # that look in the same instant may BOTH push. The acceptance is "one push per start window; a duplicate push is
        # bounded by the notebook's bound-domain exit (live check needed)". This test asserts only that bound (at most one
        # push per run) and that every run ends with a session; it does not assert one push. The "<= runs" bound holds by
        # construction (decide() pushes at most once); the test proves it only for this wiring and does not show the race is closed.
        endpoint = self.endpoint(kernel=self.kernel)
        self.kernel.write(status="COMPLETE", up_after=3)
        outputs = []

        def one():
            kaggle = modelsup.Kaggle(str(self.kernel.path), self.env(), USER, KAGGLE_KEY)
            cfg = modelsup.read_config(self.env())
            deps = modelsup.Deps(lambda: modelsup.endpoint_state(cfg, connect=endpoint.connect), kaggle.word,
                                 lambda: modelsup.push_notebook(kaggle, USER, MODEL), sleep=lambda _s: time.sleep(0.01),
                                 say=lambda _t: None)
            outputs.append(modelsup.decide(deps)[0])

        threads = [threading.Thread(target=one) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertLessEqual(self.pushes(), len(threads), "at most one push per run")
        self.assertGreaterEqual(self.pushes(), 1)
        self.assertTrue(all(why in modelsup.OK_WHY for why in outputs), outputs)


class JobTest(Case):
    def test_endpoint_down_and_kernel_idle_pushes_the_rendered_notebook_once_and_succeeds(self):
        endpoint = self.endpoint(kernel=self.kernel)
        outputs, printed = self.run_job(endpoint)
        self.assertEqual(outputs, {"kaggle_ok": "true", "kaggle_why": "started"})
        self.assertEqual(self.pushes(), 1)
        push = self.kernel.pushes()[0]
        meta = push["metadata"]
        self.assertEqual(meta["id"], f"{USER}/pivotal-serve")
        self.assertEqual((meta["is_private"], meta["enable_gpu"], meta["enable_internet"]), (True, True, True))
        self.assertEqual(meta["machine_shape"], "NvidiaTeslaT4")
        self.assertEqual(meta["dataset_sources"], [f"{USER}/pivotal-serve-secrets"])
        notebook = json.loads(push["notebook"])
        self.assertEqual(notebook["nbformat"], 4)
        self.assertIn(MODEL, push["notebook"])
        pushed = next(c for c in self.kernel.calls() if c["argv"][:2] == ["kernels", "push"])
        self.assertEqual(pushed["argv"][:3], ["kernels", "push", "-p"])
        self.assertFalse(Path(pushed["argv"][3]).exists(), "the folder is removed after the push")
        self.assertIn("models-up: ok (started)", printed)

    def test_a_running_kernel_is_waited_for_and_never_pushed_to(self):
        endpoint = self.endpoint(kernel=self.kernel)
        self.kernel.write(status="RUNNING", up_after=3, pushed_at_call=0)
        outputs, _ = self.run_job(endpoint)
        self.assertEqual(outputs, {"kaggle_ok": "true", "kaggle_why": "waited"})
        self.assertEqual(self.pushes(), 0)

    def test_the_poll_limit_skips_the_kaggle_entries_with_the_reason(self):
        endpoint = self.endpoint("down")
        self.kernel.write(status="QUEUED")
        outputs, printed = self.run_job(endpoint, wait=0.3)
        self.assertEqual(outputs, {"kaggle_ok": "false", "kaggle_why": "queued"})
        self.assertEqual(self.pushes(), 0)
        self.assertIn("::warning::models-up: the Kaggle entries are skipped (queued)", printed)
        self.kernel.write(status="RUNNING")
        outputs, _ = self.run_job(endpoint, wait=0.3)
        self.assertEqual(outputs["kaggle_why"], "not_answering")

    def test_a_quota_refusal_is_visible_and_pushes_nothing_twice(self):
        endpoint = self.endpoint("down")
        self.kernel.write(push_error="Kernel push error: Maximum weekly GPU quota of 30.00 hours reached.", push_exit=1)
        outputs, printed = self.run_job(endpoint)
        self.assertEqual(outputs, {"kaggle_ok": "false", "kaggle_why": "quota"})
        self.assertEqual(self.pushes(), 1, "one attempt, no retry loop")
        self.assertIn("(quota)", printed)

    def test_any_other_push_failure_is_push_failed(self):
        endpoint = self.endpoint("down")
        self.kernel.write(push_error="500 Server Error", push_exit=1)
        self.assertEqual(self.run_job(endpoint)[0]["kaggle_why"], "push_failed")
        self.kernel.write(push_error="", push_exit=0)  # exit 0 without "successfully pushed" is not a push either
        self.assertEqual(self.pushes(), 1)

    def test_the_success_words_with_a_failing_exit_code_are_not_a_push(self):
        endpoint = self.endpoint("down")
        self.kernel.write(push_error="Kernel version 1 successfully pushed.", push_exit=1)
        self.assertEqual(self.run_job(endpoint)[0], {"kaggle_ok": "false", "kaggle_why": "push_failed"})
        self.assertEqual(self.pushes(), 1)

    def test_an_exit_zero_without_the_success_words_runs_the_job_and_is_push_failed(self):
        endpoint = self.endpoint("down")
        self.kernel.write(push_error="Kernel push error: something odd happened", push_exit=0)
        self.assertEqual(self.run_job(endpoint)[0], {"kaggle_ok": "false", "kaggle_why": "push_failed"})
        self.assertEqual(self.pushes(), 1)

    def test_only_quota_and_gpu_hours_wording_is_quota(self):
        for text, why in (("Maximum weekly GPU quota of 30 hours reached", "quota"), ("You have used all your GPU hours", "quota"),
                          ("429 rate limit exceeded", "push_failed"), ("file size limit exceeded", "push_failed"),
                          ("Kernel push error: title length limit", "push_failed")):
            with self.subTest(text=text):
                self.out.write_text("")
                endpoint = self.endpoint("down")
                self.kernel.write(push_error=text, push_exit=1)
                self.assertEqual(self.run_job(endpoint)[0]["kaggle_why"], why)

    def test_kaggle_printing_an_error_with_exit_zero_is_not_a_push(self):
        endpoint = self.endpoint("down")
        self.kernel.write(push_error="Kernel push error: Maximum weekly GPU quota reached", push_exit=0)
        self.assertEqual(self.run_job(endpoint)[0], {"kaggle_ok": "false", "kaggle_why": "quota"})

    def test_the_live_text_for_a_kernel_never_pushed_is_idle(self):
        endpoint = self.endpoint(kernel=self.kernel)
        self.kernel.write(status_error="Cannot access kernel 'alice/pivotal-serve' (Permission 'kernels.get' was denied). The most likely cause is a wrong kernel slug.")
        self.run_job(endpoint, wait=0.3)
        self.assertEqual(self.pushes(), 1)

    def test_a_kernel_that_was_never_pushed_is_idle(self):
        endpoint = self.endpoint(kernel=self.kernel)
        self.kernel.write(status_error="404 Client Error: Not Found for url: https://www.kaggle.com/api/v1/kernels/status")
        # after the push the fake still fails the status read: the first push has happened, the wait cannot see the kernel
        outputs, _ = self.run_job(endpoint, wait=0.3)
        self.assertEqual(self.pushes(), 1)
        self.assertEqual(outputs["kaggle_ok"], "false")

    def test_an_unreadable_kernel_status_pushes_nothing(self):
        endpoint = self.endpoint("down")
        self.kernel.write(status_error="403 Forbidden")
        self.assertEqual(self.run_job(endpoint)[0], {"kaggle_ok": "false", "kaggle_why": "kernel_unreadable"})
        self.assertEqual(self.pushes(), 0)
        self.kernel.write(status_error="")

    def test_an_unreadable_status_word_pushes_nothing(self):
        endpoint = self.endpoint("down")
        self.kernel.write(status="SOMETHING_NEW")
        self.assertEqual(self.run_job(endpoint)[0]["kaggle_why"], "kernel_unreadable")
        self.assertEqual(self.pushes(), 0)

    def test_an_untrusted_run_has_no_kaggle_account_and_only_reuses_a_live_session(self):
        endpoint = self.endpoint("down")
        outputs, _ = self.run_job(endpoint, KAGGLE_USERNAME=None, KAGGLE_KEY=None)
        self.assertEqual(outputs, {"kaggle_ok": "false", "kaggle_why": "no_credentials"})
        self.assertEqual(self.kernel.calls(), [])
        self.out.write_text("")
        live = self.endpoint("live")
        outputs, _ = self.run_job(live, KAGGLE_USERNAME=None, KAGGLE_KEY=None)
        self.assertEqual(outputs, {"kaggle_ok": "true", "kaggle_why": "live"})
        self.assertEqual(self.kernel.calls(), [])

    def test_one_half_of_the_account_is_no_account(self):
        endpoint = self.endpoint("down")
        self.assertEqual(self.run_job(endpoint, KAGGLE_KEY=None)[0]["kaggle_why"], "no_credentials")

    def test_without_the_endpoint_secrets_there_is_nothing_to_probe(self):
        endpoint = self.endpoint("live")
        outputs, _ = self.run_job(endpoint, PIVOTAL_KAGGLE_URL=None, PIVOTAL_KAGGLE_KEY=None)
        self.assertEqual(outputs, {"kaggle_ok": "false", "kaggle_why": "no_endpoint"})
        self.assertEqual((endpoint.requests, self.kernel.calls()), ([], []))

    def test_no_model_in_the_plan_means_no_push(self):
        endpoint = self.endpoint("down")
        self.assertEqual(self.run_job(endpoint, KAGGLE_MODEL="")[0]["kaggle_why"], "no_model")
        self.assertEqual(self.pushes(), 0)

    def test_an_unmeasured_model_is_refused_by_the_renderer_and_never_pushed(self):
        endpoint = self.endpoint("down")
        outputs, printed = self.run_job(endpoint, KAGGLE_MODEL="not-in-the-catalog")
        self.assertEqual(outputs, {"kaggle_ok": "false", "kaggle_why": "render_refused"})
        self.assertIn("render refused", printed)
        self.assertEqual(self.pushes(), 0)

    def test_a_null_pin_refuses_the_render_and_pushes_nothing(self):
        endpoint = self.endpoint("down")
        pins = json.loads((ROOT / "kaggle" / "pins.json").read_text())
        pins["ngrok"] = None
        fake_root = self.root / "root"
        shutil_tree = fake_root / "kaggle"
        shutil_tree.mkdir(parents=True)
        for name in ("catalog.json", "serve.ipynb.tmpl", "lifecycle.py", "render.py"):
            (shutil_tree / name).write_bytes((ROOT / "kaggle" / name).read_bytes())
        (shutil_tree / "pins.json").write_text(json.dumps(pins))
        os.symlink(ROOT / "gh-piv", fake_root / "gh-piv")
        with mock.patch.object(modelsup, "ROOT", fake_root):
            outputs, printed = self.run_job(endpoint)
        self.assertEqual(outputs["kaggle_why"], "render_refused")
        self.assertIn("ngrok", printed)
        self.assertEqual(self.pushes(), 0)

    def test_a_kernel_that_errors_after_our_push_ends_the_wait_as_failed(self):
        endpoint = self.endpoint("down")
        self.kernel.write(after_push=["QUEUED", "ERROR"], up_after=10**6)
        outputs, _ = self.run_job(endpoint, wait=5.0)
        self.assertEqual(outputs["kaggle_why"], "failed")
        self.assertEqual(self.pushes(), 1)


class HostileEndpointTest(Case):
    def state(self, endpoint: Endpoint, **over) -> str:
        cfg = modelsup.read_config(self.env(**over))
        return modelsup.endpoint_state(cfg, connect=endpoint.connect)

    def test_each_answer_is_reduced_to_one_state(self):
        cases = {"live": "up", "down": "down", "loading": "down", "unauthorized": "unauthorized", "forbidden": "unauthorized",
                 "nonjson": "odd", "nodata": "odd", "emptydata": "odd", "deep": "odd", "redirect": "odd", "huge": "odd"}
        for mode, expected in cases.items():
            with self.subTest(mode=mode):
                server = self.endpoint(mode)
                if mode == "live":
                    server.kernel = self.kernel
                    self.kernel.write(up=True)
                self.assertEqual(self.state(server), expected)

    def test_a_redirect_is_not_followed_and_the_key_goes_nowhere_else(self):
        other = self.endpoint("live")
        server = self.endpoint("redirect", other=other)
        outputs, _ = self.run_job(server)
        self.assertEqual(outputs, {"kaggle_ok": "false", "kaggle_why": "odd_answer"})
        self.assertEqual(other.requests, [], "the other host was never contacted")
        self.assertEqual(self.kernel.calls(), [])

    def test_a_401_stops_the_job_before_kaggle_is_asked(self):
        server = self.endpoint("unauthorized")
        outputs, _ = self.run_job(server)
        self.assertEqual(outputs, {"kaggle_ok": "false", "kaggle_why": "rejected_key"})
        self.assertEqual((self.kernel.calls(), self.pushes()), ([], 0))

    def test_a_huge_body_is_cut_off_after_the_cap(self):
        server = self.endpoint("huge")
        self.assertEqual(self.state(server), "odd")
        time.sleep(0.2)
        self.assertLess(server.bytes_sent, 8 * 1024 * 1024, "the client stopped reading near the cap")

    def test_a_body_that_drips_past_the_time_cap_is_odd(self):
        ticks = iter(range(0, 100000, 20))  # each read of the clock is twenty seconds later
        server = self.endpoint("live")
        server.kernel = self.kernel
        self.kernel.write(up=True)
        cfg = modelsup.read_config(self.env())
        got = modelsup.endpoint_state(cfg, connect=server.connect, clock=lambda: float(next(ticks)))
        self.assertEqual(got, "odd")

    def test_a_body_that_trickles_a_byte_at_a_time_ends_at_the_deadline_on_the_real_clock(self):
        # F3: read(4096) used to block until 4096 bytes arrived (about 20 s at 5 ms a byte against a 1 s cap).
        server = self.endpoint("trickle")
        cfg = modelsup.read_config(self.env())
        with mock.patch.object(modelsup, "PROBE_SECONDS", 1.0):
            started = time.monotonic()
            got = modelsup.endpoint_state(cfg, connect=server.connect)
            elapsed = time.monotonic() - started
        self.assertEqual(got, "odd")
        self.assertLess(elapsed, 3.0)

    def test_a_server_that_goes_silent_before_the_response_line_or_after_the_headers_ends_at_the_deadline_too(self):
        import socket
        for head, expected in ((b"", "down"), (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n", "odd")):
            with self.subTest(sent=head[:12]):
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                self.addCleanup(listener.close)
                port = listener.getsockname()[1]
                held = []

                def accept(listener=listener, head=head, held=held):
                    conn, _ = listener.accept()
                    held.append(conn)  # kept open and silent
                    conn.recv(65536)
                    if head:
                        conn.sendall(head)

                threading.Thread(target=accept, daemon=True).start()
                connect = lambda _d, port=port: http.client.HTTPConnection("127.0.0.1", port, timeout=30)  # noqa: E731 - long own timeout
                cfg = modelsup.read_config(self.env())
                with mock.patch.object(modelsup, "PROBE_SECONDS", 1.0):
                    started = time.monotonic()
                    got = modelsup.endpoint_state(cfg, connect=connect)
                    elapsed = time.monotonic() - started
                for conn in held:
                    conn.close()
                self.assertEqual(got, expected)
                self.assertLess(elapsed, 3.0)

    def trickle(self, lead: bytes, lines: list[bytes], gap: float):
        """A server that sends ``lead`` and then one chunk every ``gap`` seconds (a header or status-line trickle). Returns connect."""
        import socket
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        stop = threading.Event()
        self.addCleanup(stop.set)

        def accept():
            conn, _ = listener.accept()
            try:
                conn.recv(65536)
                conn.sendall(lead)
                for piece in lines:
                    if stop.wait(gap):
                        return
                    conn.sendall(piece)
            except OSError:
                pass
            finally:
                conn.close()

        threading.Thread(target=accept, daemon=True).start()
        # a long own socket timeout: only the total deadline can cut the exchange short
        return lambda _d: http.client.HTTPConnection("127.0.0.1", port, timeout=30)

    def timed_state(self, connect) -> tuple[str, float]:
        cfg = modelsup.read_config(self.env())
        with mock.patch.object(modelsup, "PROBE_SECONDS", 1.0):
            started = time.monotonic()
            got = modelsup.endpoint_state(cfg, connect=connect)
            return got, time.monotonic() - started

    def test_headers_that_trickle_one_line_at_a_time_end_at_the_total_deadline(self):
        # F1 (round 2): every receive arrives inside the per-receive timeout, so only a total deadline can stop it (was 50 s)
        connect = self.trickle(b"HTTP/1.1 200 OK\r\n", [b"X-A: 1\r\n"] * 60, 0.5)
        got, elapsed = self.timed_state(connect)
        self.assertIn(got, ("down", "odd"))
        self.assertLess(elapsed, 3.0)

    def test_a_status_line_that_trickles_a_byte_at_a_time_ends_at_the_total_deadline(self):
        # was 20 s: HTTP/1.1 200 OK\r\n one byte every 0.5 s
        connect = self.trickle(b"", [bytes([b]) for b in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"], 0.5)
        got, elapsed = self.timed_state(connect)
        self.assertIn(got, ("down", "odd"))
        self.assertLess(elapsed, 3.0)

    def test_a_fast_answer_is_unaffected_by_the_total_deadline(self):
        server = self.endpoint("live")
        server.kernel = self.kernel
        self.kernel.write(up=True)
        got, elapsed = self.timed_state(server.connect)
        self.assertEqual(got, "up")
        self.assertLess(elapsed, 1.0)

    def test_the_timer_is_cancelled_when_the_exchange_ends(self):
        server = self.endpoint("live")
        server.kernel = self.kernel
        self.kernel.write(up=True)
        before = threading.active_count()
        self.timed_state(server.connect)
        time.sleep(0.1)
        self.assertLessEqual(threading.active_count(), before)

    def test_the_body_read_gets_only_what_is_left_of_the_deadline(self):
        # one byte arrives half way through the allowance, then silence: the read after it must end at the deadline
        # (2 s), not a whole allowance later
        import socket
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        held = []

        def accept():
            conn, _ = listener.accept()
            held.append(conn)
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            time.sleep(1.0)
            conn.sendall(b"x")

        threading.Thread(target=accept, daemon=True).start()
        connect = lambda _d: http.client.HTTPConnection("127.0.0.1", port, timeout=30)  # noqa: E731
        cfg = modelsup.read_config(self.env())
        with mock.patch.object(modelsup, "PROBE_SECONDS", 2.0):
            started = time.monotonic()
            got = modelsup.endpoint_state(cfg, connect=connect)
            elapsed = time.monotonic() - started
        for conn in held:
            conn.close()
        self.assertEqual(got, "odd")
        self.assertLess(elapsed, 2.5)

    def test_a_non_json_answer_is_never_a_reason_to_push(self):
        server = self.endpoint("nonjson")
        self.assertEqual(self.run_job(server)[0]["kaggle_why"], "odd_answer")
        self.assertEqual(self.pushes(), 0)

    def test_an_unreachable_endpoint_is_down(self):
        dead = self.endpoint("down")
        port = dead.port
        dead.close()
        cfg = modelsup.read_config(self.env())
        got = modelsup.endpoint_state(cfg, connect=lambda _d: http.client.HTTPConnection("127.0.0.1", port, timeout=1))
        self.assertEqual(got, "down")

    def test_the_key_is_sent_only_as_a_bearer_header_to_the_domain(self):
        server = self.endpoint("live")
        server.kernel = self.kernel
        self.kernel.write(up=True)
        self.run_job(server)
        self.assertEqual(server.requests, [("GET", "/v1/models", True)])


class GrammarTest(Case):
    def refused(self, **over):
        with self.assertRaises(job.JobError):
            modelsup.read_config(self.env(**over))

    def test_the_url_is_https_and_a_domain_only(self):
        for url in ("http://a.ngrok-free.dev", "https://a.ngrok-free.dev:8443", "https://u:p@a.ngrok-free.dev",
                    "https://a.ngrok-free.dev/v1", "https://a.ngrok-free.dev?x=1", "https://a.ngrok-free.dev#x",
                    "https://127.0.0.1", "https://169.254.169.254", "https://localhost", "https://[::1]", "https://a_b.dev",
                    "https://a.b.dev\nhttps://c.d", "https://-a.b.dev", "https://a.b.dev.", "https://" + "a" * 64 + ".dev",
                    "https://a.b.dev/..", "ftp://a.b.dev", "https://a.b.dev@evil.example", "https://a.b.d\x00ev"):
            with self.subTest(url=url):
                self.refused(PIVOTAL_KAGGLE_URL=url)

    def test_a_good_url_is_taken_in_lower_case_without_the_slash(self):
        cfg = modelsup.read_config(self.env(PIVOTAL_KAGGLE_URL="https://Pivotal-Serve.ngrok-free.dev/"))
        self.assertEqual(cfg.domain, "pivotal-serve.ngrok-free.dev")

    def test_the_kaggle_account_never_reaches_argv_unless_it_is_a_name(self):
        for user in ("a/b", "-rf", "a b", "a;b", "a\nb", "a" * 65, "$(x)", "a..b", "../x", "a%2fb"):
            with self.subTest(user=user):
                self.refused(KAGGLE_USERNAME=user)

    def test_keys_are_printable_and_one_line(self):
        for bad in ("short", "has space in it 123", "line\nbreak123456", "tab\t123456789", "x" * 301, "é" * 12):
            with self.subTest(bad=bad):
                self.refused(PIVOTAL_KAGGLE_KEY=bad)
                self.refused(KAGGLE_KEY=bad)

    def test_the_model_and_the_keyless_switch_are_checked(self):
        for model in ("Q/../x", "a b", "-x", "a" * 65, "x\n"):
            self.refused(KAGGLE_MODEL=model)
        for value in ("yes", "true", "2", " 1"):
            self.refused(PIVOTAL_KEYLESS_CHECK=value)

    def test_the_job_refuses_loudly_before_anything_is_written(self):
        env = {k: v for k, v in self.env(PIVOTAL_KAGGLE_URL="https://127.0.0.1").items() if k not in ("PIV_KAGGLE",)}
        done = subprocess.run([sys.executable, "-I", str(ROOT / "engine" / "modelsup.py")], env=env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertIn("refused: PIVOTAL_KAGGLE_URL", done.stderr)
        self.assertEqual(self.out.read_text(), "")
        self.assertNotIn(KEY, done.stdout + done.stderr)

    def test_the_script_runs_with_no_endpoint_and_says_why(self):
        env = {k: v for k, v in self.env(PIVOTAL_KAGGLE_URL=None, PIVOTAL_KAGGLE_KEY=None).items()}
        done = subprocess.run([sys.executable, "-I", str(ROOT / "engine" / "modelsup.py")], env=env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs(), {"kaggle_ok": "false", "kaggle_why": "no_endpoint"})
        self.assertIn("::warning::models-up", done.stdout)

    def test_usage(self):
        self.assertEqual(modelsup.main(["modelsup.py", "extra"], {}), 2)


class KeylessCheckTest(Case):
    def live(self) -> Endpoint:
        server = self.endpoint("live")
        server.kernel = self.kernel
        self.kernel.write(up=True)
        return server

    def test_it_is_off_by_default(self):
        server = self.live()
        self.run_job(server)
        self.assertEqual([r for r in server.requests if not r[2]], [])

    def test_when_on_every_route_but_health_is_requested_without_the_key_and_must_be_401(self):
        server = self.live()
        outputs, _ = self.run_job(server, PIVOTAL_KEYLESS_CHECK="1")
        self.assertEqual(outputs, {"kaggle_ok": "true", "kaggle_why": "live"})
        keyless = [(m, p) for m, p, k in server.requests if not k]
        self.assertEqual(keyless, list(modelsup.KEYLESS_ROUTES))
        self.assertNotIn("/health", [p for _m, p, _k in server.requests])
        self.assertEqual([r for r in server.requests if r[2] is False and r[1] == "/v1/models"][0][:2], ("GET", "/v1/models"))

    def test_a_route_that_answers_without_the_key_fails_the_check(self):
        for route_ in ("/props", "/slots", "/metrics", "/v1/chat/completions"):
            with self.subTest(route=route_):
                self.out.write_text("")
                server = self.live()
                server.open_routes = {route_}
                outputs, printed = self.run_job(server, PIVOTAL_KEYLESS_CHECK="1")
                self.assertEqual(outputs, {"kaggle_ok": "false", "kaggle_why": "keyless_open"})
                self.assertIn(route_, printed)

    def test_a_404_or_other_non_401_answer_is_not_closed(self):
        server = self.live()
        cfg = modelsup.read_config(self.env(PIVOTAL_KEYLESS_CHECK="1"))
        for status in (200, 403, 404, 500):
            with self.subTest(status=status):
                with mock.patch.object(modelsup, "request", return_value=(status, None)):
                    self.assertEqual(modelsup.keyless_open(cfg, connect=server.connect), [p for _m, p in modelsup.KEYLESS_ROUTES])
        with mock.patch.object(modelsup, "request", return_value=(401, None)):
            self.assertEqual(modelsup.keyless_open(cfg, connect=server.connect), [])

    def test_an_unreachable_route_is_not_shown_closed(self):
        server = self.live()
        cfg = modelsup.read_config(self.env(PIVOTAL_KEYLESS_CHECK="1"))
        dead = lambda _d: http.client.HTTPConnection("127.0.0.1", 1, timeout=1)  # noqa: E731
        self.assertEqual(modelsup.keyless_open(cfg, connect=dead), [p for _m, p in modelsup.KEYLESS_ROUTES])
        self.assertEqual(modelsup.keyless_open(cfg, connect=server.connect), [])

    def test_the_check_does_not_run_when_there_is_no_session(self):
        server = self.endpoint("unauthorized")
        self.run_job(server, PIVOTAL_KEYLESS_CHECK="1")
        self.assertEqual([r for r in server.requests if r[1] != "/v1/models"], [])


class SecretScanTest(Case):
    def test_no_key_is_in_argv_a_log_an_output_the_pushed_folder_or_the_wrong_environment(self):
        server = self.endpoint(kernel=self.kernel)
        self.kernel.write(push_error="", status="COMPLETE")
        outputs, printed = self.run_job(server)
        self.assertEqual(outputs["kaggle_ok"], "true")
        calls = self.kernel.calls()
        self.assertGreaterEqual(len(calls), 3)
        for call in calls:
            self.assertNotIn(KEY, json.dumps(call["argv"]))
            self.assertNotIn(KAGGLE_KEY, json.dumps(call["argv"]), "the account key is in kaggle's environment only")
            self.assertNotIn(KEY, json.dumps(call["env"]), "kaggle never gets the endpoint key")
            self.assertEqual(call["env"].get("KAGGLE_KEY"), KAGGLE_KEY)
            self.assertEqual(call["env"].get("KAGGLE_USERNAME"), USER)
            self.assertFalse({"GITHUB_OUTPUT", "PIVOTAL_KAGGLE_KEY", "PIVOTAL_KAGGLE_URL", "GH_TOKEN"} & set(call["env"]))
        pushed = self.kernel.pushes()[0]
        for secret in (KEY, KAGGLE_KEY):
            self.assertNotIn(secret, pushed["notebook"])
            self.assertNotIn(secret, json.dumps(pushed["metadata"]))
            self.assertNotIn(secret, printed)
            self.assertNotIn(secret, self.out.read_text())

    def test_what_kaggle_prints_is_never_echoed(self):
        server = self.endpoint("down")
        self.kernel.write(push_error=f"boom {KEY} {KAGGLE_KEY}", push_exit=1)
        _, printed = self.run_job(server)
        for secret in (KEY, KAGGLE_KEY, "boom"):
            self.assertNotIn(secret, printed)

    def test_the_outputs_are_the_validated_shape(self):
        self.assertEqual(set(job.OUTPUT_RULES) & {"kaggle_ok", "kaggle_why", "kaggle_model"}, {"kaggle_ok", "kaggle_why", "kaggle_model"})
        for why in modelsup.WHY:
            job.write_outputs(str(self.out), {"kaggle_ok": "false", "kaggle_why": why})
        for bad in ("a\nkaggle_ok=true", "Has Caps", "x" * 25, ""):
            with self.assertRaises(job.JobError):
                job.write_outputs(str(self.out), {"kaggle_why": bad})
        with self.assertRaises(job.JobError):
            job.write_outputs(str(self.out), {"kaggle_ok": "yes"})

    def test_a_planted_output_file_is_appended_to_not_trusted(self):
        # This step runs no model output and no repo code, so GITHUB_OUTPUT holds only the engine's own lines.
        server = self.endpoint("live")
        server.kernel = self.kernel
        self.kernel.write(up=True)
        self.run_job(server)
        self.assertEqual(self.out.read_text().splitlines(), ["kaggle_ok=true", "kaggle_why=live"])


class RecheckTest(Case):
    def test_a_build_recheck_is_one_probe_with_the_key(self):
        server = self.endpoint("live")
        server.kernel = self.kernel
        self.kernel.write(up=True)
        self.assertEqual(modelsup.recheck(self.env(), connect=server.connect), "up")
        self.assertEqual(server.requests, [("GET", "/v1/models", True)])
        self.assertEqual(self.kernel.calls(), [])

    def test_a_recheck_that_does_not_see_a_model_server_is_down(self):
        for mode in ("down", "unauthorized", "nonjson", "redirect", "loading"):
            with self.subTest(mode=mode):
                self.assertNotEqual(modelsup.recheck(self.env(), connect=self.endpoint(mode).connect), "up")
        server = self.endpoint("live")
        for over in ({"PIVOTAL_KAGGLE_URL": None}, {"PIVOTAL_KAGGLE_KEY": None}, {"PIVOTAL_KAGGLE_URL": "https://127.0.0.1"},
                     {"PIVOTAL_KAGGLE_KEY": "short"}):
            self.assertNotEqual(modelsup.recheck(self.env(**over), connect=server.connect), "up", over)
        self.assertEqual(server.requests, [])

    def test_a_recheck_names_what_it_saw(self):
        words = {"live": "up", "unauthorized": "unauthorized", "down": "down", "nonjson": "odd", "redirect": "odd"}
        for mode, word in words.items():
            with self.subTest(mode=mode):
                self.assertEqual(modelsup.recheck(self.env(), connect=self.endpoint(mode).connect), word)
        self.assertEqual(modelsup.recheck(self.env(PIVOTAL_KAGGLE_URL=None)), "down")

    def test_the_build_hook_asks_modelsup(self):
        entry = route.parse_entry("pi:kaggle/qwen")
        with mock.patch.object(modelsup, "recheck", return_value="down") as asked:
            self.assertEqual(buildjob.recheck_endpoint(entry, {"A": "b"}), "down")
        asked.assert_called_once_with({"A": "b"})
        with mock.patch.object(modelsup, "recheck", return_value="up"):
            self.assertEqual(buildjob.recheck_endpoint(entry, {}), "up")

    def test_a_build_with_no_endpoint_secret_falls_through_as_endpoint_down(self):
        entries = (route.parse_entry("pi:kaggle/qwen"), route.parse_entry("pi:groq/llama"))
        env = {"PIVOTAL_KAGGLE_KEY": KEY, "GROQ_API_KEY": "g" * 20}
        down, rejected = buildjob.kaggle_down(env, entries)
        chosen = route.route(list(entries), route.make_probe(env, down=down, rejected=rejected), strict_first=True)
        self.assertEqual(chosen.entry.spec, "pi:groq/llama")
        self.assertEqual([f.text for f in chosen.fell_back], ["fell_back: pi:kaggle/qwen → pi:groq/llama (endpoint down)"])


class PlanOutputTest(unittest.TestCase):
    def preset(self, **extra):
        return route.parse_preset({"name": "p", "tiers": {"build": ["pi:kaggle/qwen3.6-35b-a3b@high", "pi:groq/x"],
                                                          "review": ["pi:groq/y"]}, **extra})

    def test_the_presets_kaggle_model_is_kept(self):
        self.assertEqual(self.preset(kaggle={"model": "qwen3.6-35b-a3b"}).kaggle_model, "qwen3.6-35b-a3b")
        self.assertIsNone(self.preset().kaggle_model)

    def test_a_malformed_or_inconsistent_kaggle_key_is_refused(self):
        for bad in ("qwen", {}, {"model": 3}, {"model": "A/B"}, {"model": "qwen3.6-35b-a3b", "x": 1}, {"model": "gemma"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.preset(kaggle=bad)

    def test_plan_names_the_model_from_the_preset_or_the_one_kaggle_entry(self):
        self.assertEqual(plan.kaggle_model(self.preset(kaggle={"model": "qwen3.6-35b-a3b"})), "qwen3.6-35b-a3b")
        self.assertEqual(plan.kaggle_model(self.preset()), "qwen3.6-35b-a3b")
        mixed = route.parse_preset({"name": "m", "tiers": {"build": ["pi:kaggle/a", "pi:kaggle/b"], "review": ["pi:groq/y"]}})
        self.assertEqual(plan.kaggle_model(mixed), "", "two models and no kaggle.model: models-up never guesses")
        none = route.parse_preset({"name": "n", "tiers": {"build": ["pi:groq/a"], "review": ["pi:groq/y"]}})
        self.assertEqual(plan.kaggle_model(none), "")

    def test_the_budget_is_twelve_minutes_of_poll_inside_the_job_timeout(self):
        self.assertGreaterEqual(modelsup.JOB_MINUTES * 60, modelsup.WAIT_SECONDS + modelsup.PUSH_SECONDS)


if __name__ == "__main__":
    unittest.main()
