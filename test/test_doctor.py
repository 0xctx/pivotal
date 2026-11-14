"""piv doctor: read-only checks, one fix line per problem.

Every GitHub read goes through the fake gh (test/fakegh.py), which refuses any flag the real command
lacks. The Anthropic API and the ngrok endpoint are a local HTTP server, and kaggle is a fake binary, so no
test reaches a provider. Each test's environment holds only what it names: a real token never leaks in.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import fakecli
import fakegh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))

from piv import cli, doctor, init  # noqa: E402

CALLER = (ROOT / "templates" / "caller.yml").read_text(encoding="utf-8")
SHA = "1234567890abcdef1234567890abcdef12345678"
ZEROS = "0" * 40
REPO = "0xctx/piv-sandbox"
OWNER = "0xctx"
PINNED_CALLER = CALLER.replace(init.ENGINE_PLACEHOLDER, init.ENGINE_REPO).replace(ZEROS, SHA)
LAUNCHER = ROOT / "gh-piv" / "gh-piv"
WORKFLOW = ".github/workflows/pivotal.yml"
SEARCH = f"search/repositories?q=topic%3Apivotal+user%3A{OWNER}"
SENTINEL_KEY = "sk-ant-SENTINEL-do-not-print"
SENTINEL_KAGGLE = "KAGGLE-SENTINEL-do-not-print"
SENTINEL_OAUTH = "oauth-SENTINEL-do-not-print"
UNREAD = 3  # the exit status for a check that could not be read (the contract names 3; 1 is a problem, 2 usage)
ENDPOINT_ENV = "PIVOTAL_KAGGLE_URL"
NO_ORG_KEY = "[problem] Secrets: no repo-level model key is set, and no organization model key is visible"
UNKNOWN_ORG = "[unknown] Secrets: no repo-level model key is set, and the organization's secrets could not be read"


def ok_json(data: object) -> dict:
    return fakegh.ok(json.dumps(data))


def contents(text: str) -> dict:
    return ok_json({"sha": "b10b", "content": base64.b64encode(text.encode("utf-8")).decode("ascii")})


def secrets(*names: str) -> dict:
    return ok_json({"total_count": len(names), "secrets": [{"name": name} for name in names]})


def fix_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.strip().startswith("fix:")]


def problem_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if "[problem]" in line]


class LocalHTTP:
    """A local stand-in for the Anthropic API and the ngrok endpoint: each request gets one status and body."""

    def __init__(self, status: int, body: bytes = b"{}", headers: dict | None = None):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self):
                size = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(size).decode("utf-8") if size else ""
                request = {"method": self.command, "path": self.path, "headers": self.headers, "body": data}
                outer.requests.append(request)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            do_GET = answer
            do_POST = answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def closed_url() -> str:
    """A URL on a port nothing listens on, so a connection is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.gh = fakegh.FakeGh(self.root / "bin")
        self.out = self.err = None

    def tearDown(self):
        self._tmp.cleanup()

    def doctor(self, *args: str, env: dict[str, str] | None = None) -> int:
        import io

        self.out, self.err = io.StringIO(), io.StringIO()
        return cli.main(["doctor", *args], self.gh.env() if env is None else env, self.out, self.err)

    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def healthy(self, repo: str = REPO, **replace: list[dict]) -> None:
        """Routes for a repo that passes every check. ``replace`` maps a path to the answers that replace it.

        The fake matches by substring, so the repo's own route comes last: a longer path is matched first.
        """
        base = {
            f"api repos/{repo}/actions/permissions": [ok_json({"enabled": True})],
            f"api repos/{repo}/contents/{WORKFLOW}": [contents(PINNED_CALLER)],
            f"api repos/{repo}/actions/secrets": [secrets("CLAUDE_CODE_OAUTH_TOKEN")],
            f"api repos/{repo}/actions/variables/PIVOTAL_RUNNER": [fakegh.not_found()],
            f"api repos/{repo}/actions/variables/PIVOTAL_LEVEL": [ok_json({"value": "prepare"})],
            f"api repos/{repo}/actions/variables/PIVOTAL_CHECK": [ok_json({"value": "make test"})],
            f"api repos/{repo}/actions/runners": [ok_json({"total_count": 0, "runners": []})],
            f"api users/{OWNER}/settings/billing/actions": [
                ok_json({"included_minutes": 2000, "total_minutes_used": 10})
            ],
        }
        base.update(replace)
        public = {"private": False, "owner": {"login": OWNER, "type": "User"}}
        repo_info = base.pop(f"api repos/{repo}", None) or [ok_json(public)]
        for path, answers in base.items():
            self.gh.route(path, *answers)
        self.gh.route(f"api repos/{repo}", *repo_info)

    def gh_calls(self) -> list[dict]:
        return self.gh.calls()

    def with_kaggle(self) -> dict:
        listed = secrets("CLAUDE_CODE_OAUTH_TOKEN", "PIVOTAL_KAGGLE_URL", "PIVOTAL_KAGGLE_KEY")
        return {f"api repos/{REPO}/actions/secrets": [listed]}

    def claude_api(self, api: "LocalHTTP"):
        """Point the paid probe at a local server, so no test reaches a provider. Every Claude probe test uses it."""
        return mock.patch.object(doctor, "ANTHROPIC_URL", api.url + "/v1/messages")


class ActionsEnabledTest(DoctorTest):
    def test_disabled_actions_is_a_problem_with_one_fix_line(self):
        self.healthy(**{f"api repos/{REPO}/actions/permissions": [ok_json({"enabled": False})]})
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn(f"[problem] Actions: disabled in {REPO}", self.out.getvalue())
        self.assertEqual(len(fix_lines(self.out.getvalue())), 1)
        self.assertIn("Actions", fix_lines(self.out.getvalue())[0])

    def test_disabled_actions_still_runs_the_other_checks(self):
        self.healthy(**{f"api repos/{REPO}/actions/permissions": [ok_json({"enabled": False})]})
        self.doctor(REPO)
        self.assertTrue(any(f"contents/{WORKFLOW}" in " ".join(c["args"]) for c in self.gh_calls()))
        self.assertIn("Caller: .github/workflows/pivotal.yml structure checked", self.out.getvalue())

    def test_a_healthy_repo_has_no_problem_and_the_unprobed_claude_token_is_unknown(self):
        self.healthy()
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertEqual(problem_lines(self.out.getvalue()), [])
        self.assertIn("Actions: enabled", self.out.getvalue())
        self.assertIn("[unknown] Claude token: not probed (pass --probe-claude", self.out.getvalue())
        self.assertIn("1 check(s) could not be read", self.out.getvalue())
        self.assertNotIn("no problems", self.out.getvalue())

    def test_an_unreadable_actions_setting_is_a_problem_and_the_other_checks_still_run(self):
        self.healthy(**{f"api repos/{REPO}/actions/permissions": [fakegh.fail("gh: Server Error (HTTP 500)\n")]})
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn("could not be read", self.out.getvalue())
        self.assertEqual(len(fix_lines(self.out.getvalue())), 1)
        self.assertIn("Caller", self.out.getvalue())


class MissingSecretTest(DoctorTest):
    def test_no_model_key_is_a_problem_and_the_fix_names_the_keys(self):
        self.healthy(**{f"api repos/{REPO}/actions/secrets": [secrets("KAGGLE_USERNAME")]})
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn("[problem] Secrets: no repo-level model key is set", self.out.getvalue())
        fix = fix_lines(self.out.getvalue())
        self.assertEqual(len(fix), 1)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", fix[0])
        self.assertIn("ANTHROPIC_API_KEY", fix[0])

    def test_a_model_key_set_passes(self):
        self.healthy(**{f"api repos/{REPO}/actions/secrets": [secrets("GROQ_API_KEY")]})
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertEqual(problem_lines(self.out.getvalue()), [])
        self.assertIn("a model key is set (GROQ_API_KEY)", self.out.getvalue())

    def test_a_kaggle_url_without_its_key_is_a_problem(self):
        listed = secrets("CLAUDE_CODE_OAUTH_TOKEN", "PIVOTAL_KAGGLE_URL")
        self.healthy(**{f"api repos/{REPO}/actions/secrets": [listed]})
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn("PIVOTAL_KAGGLE_URL is set but PIVOTAL_KAGGLE_KEY is not", self.out.getvalue())
        self.assertEqual(len(fix_lines(self.out.getvalue())), 1)

    def test_no_secret_value_is_printed(self):
        self.healthy(**{f"api repos/{REPO}/actions/secrets": [secrets("CLAUDE_CODE_OAUTH_TOKEN")]})
        env = self.gh.env(CLAUDE_CODE_OAUTH_TOKEN=SENTINEL_OAUTH, KAGGLE_KEY=SENTINEL_KAGGLE)
        self.doctor(REPO, env=env)
        for sentinel in (SENTINEL_OAUTH, SENTINEL_KAGGLE):
            self.assertNotIn(sentinel, self.text())


class CallerTest(DoctorTest):
    def caller_problem(self, text: str, expect: str) -> str:
        self.healthy(**{f"api repos/{REPO}/contents/{WORKFLOW}": [contents(text)]})
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn(expect, self.out.getvalue())
        fix = fix_lines(self.out.getvalue())
        self.assertEqual(len(fix), 1, self.out.getvalue())
        return fix[0]

    def test_a_missing_caller_is_a_problem_and_the_fix_runs_init(self):
        self.healthy(**{f"api repos/{REPO}/contents/{WORKFLOW}": [fakegh.not_found()]})
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn(f"[problem] Caller: {WORKFLOW} is missing", self.out.getvalue())
        self.assertIn(f"gh piv init {REPO}", fix_lines(self.out.getvalue())[0])

    def test_the_template_placeholder_pin_left_in_place_is_a_problem(self):
        fix = self.caller_problem(CALLER, "still has the template's placeholder pin")
        self.assertIn("--update", fix)

    def test_an_unpinned_uses_line_for_the_pivotal_repo_is_a_problem(self):
        text = PINNED_CALLER + "\n  uses: you/pivotal/.github/workflows/extra.yml@main\n"
        self.caller_problem(text, "not pinned to one commit SHA on a uses: line")

    def test_a_sha_in_a_comment_does_not_pin_an_unpinned_uses_line(self):
        text = PINNED_CALLER.replace(SHA, "main") + f"\n# uses: you/pivotal/.github/workflows/factory.yml@{SHA}\n"
        self.caller_problem(text, "not pinned to one commit SHA on a uses: line")

    def test_a_commented_out_jobs_key_with_a_pin_is_a_problem(self):
        text = "# on:\n# jobs:\n  # uses: you/pivotal/.github/workflows/factory.yml@" + SHA + "\n"
        self.caller_problem(text, "has no top-level on: key")

    def test_a_pin_that_is_not_a_commit_sha_is_a_problem(self):
        text = PINNED_CALLER.replace(SHA, "v1.0.0")
        self.caller_problem(text, "not pinned to one commit SHA")

    def test_a_caller_with_no_jobs_key_is_a_problem(self):
        text = PINNED_CALLER.replace("\njobs:", "\nnotjobs:")
        self.caller_problem(text, "has no top-level jobs: key")

    def test_a_tab_indented_caller_is_a_problem(self):
        self.caller_problem(PINNED_CALLER + "\n\tbad: tab\n", "a tab for indentation")

    def test_a_pinned_caller_passes_and_names_the_pin(self):
        self.healthy()
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertIn(
            f"Caller: {WORKFLOW} structure checked (not validated as YAML), engine: {init.ENGINE_REPO} pinned to {SHA[:12]}",
            self.out.getvalue(),
        )

    def test_a_caller_for_a_fork_is_ok_and_says_a_fork(self):
        fork = "a/b"
        text = init.render_caller(CALLER, SHA, fork)
        self.healthy(**{f"api repos/{REPO}/contents/{WORKFLOW}": [contents(text)]})
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertIn(f"engine: {fork} pinned to {SHA[:12]}", self.out.getvalue())
        self.assertIn("a fork", self.out.getvalue())


class ClaudeProbeTest(DoctorTest):
    def key_env(self, key: str = SENTINEL_KEY, **extra: str) -> dict[str, str]:
        return self.gh.env(ANTHROPIC_API_KEY=key, **extra)

    def test_the_probe_is_off_by_default_and_makes_no_call(self):
        self.healthy()
        with LocalHTTP(200) as api, self.claude_api(api):
            self.assertEqual(self.doctor(REPO, env=self.key_env()), UNREAD, self.text())
        self.assertEqual(api.requests, [])
        self.assertIn("[unknown] Claude token: not probed (pass --probe-claude", self.out.getvalue())

    def test_an_accepted_key_is_ok_and_the_probe_is_one_token(self):
        self.healthy()
        with LocalHTTP(200, b'{"type": "message"}') as api, self.claude_api(api):
            self.assertEqual(self.doctor(REPO, "--probe-claude", env=self.key_env()), 0, self.text())
        self.assertEqual(len(api.requests), 1)
        request = api.requests[0]
        self.assertEqual(request["headers"].get("x-api-key"), SENTINEL_KEY)
        body = json.loads(request["body"])
        self.assertEqual(body["max_tokens"], 1)
        self.assertEqual(body["model"], doctor.PROBE_MODEL)
        self.assertIn("accepted", self.out.getvalue())

    def test_a_rejected_key_is_a_problem_and_the_key_is_not_printed(self):
        self.healthy()
        body = b'{"error": {"type": "authentication_error", "message": "invalid key ' + SENTINEL_KEY.encode() + b'"}}'
        with LocalHTTP(401, body) as api, self.claude_api(api):
            self.assertEqual(self.doctor(REPO, "--probe-claude", env=self.key_env()), 1, self.text())
        self.assertIn("[problem] Claude API key rejected (HTTP 401)", self.out.getvalue())
        self.assertEqual(len(fix_lines(self.out.getvalue())), 1)
        self.assertNotIn(SENTINEL_KEY, self.text())

    def test_a_model_not_found_is_unknown_and_not_called_a_rejected_key(self):
        self.healthy()
        with LocalHTTP(404, b'{"error": {"type": "not_found_error"}}') as api, self.claude_api(api):
            self.assertEqual(self.doctor(REPO, "--probe-claude", env=self.key_env()), UNREAD, self.text())
        self.assertIn(
            f"[unknown] Claude token: probe model {doctor.PROBE_MODEL} was not found (HTTP 404)", self.out.getvalue()
        )
        self.assertNotIn("rejected", self.out.getvalue())
        self.assertEqual(problem_lines(self.out.getvalue()), [])

    def test_an_unreachable_api_is_unknown_not_a_pass(self):
        self.healthy()
        with mock.patch.object(doctor, "ANTHROPIC_URL", closed_url() + "/v1/messages"):
            self.assertEqual(self.doctor(REPO, "--probe-claude", env=self.key_env()), UNREAD, self.text())
        self.assertIn("[unknown] Claude API: could not reach the Claude API", self.out.getvalue())
        self.assertNotIn(SENTINEL_KEY, self.text())

    def test_a_redirect_is_not_followed_so_the_key_never_reaches_another_host(self):
        self.healthy()
        with LocalHTTP(200) as elsewhere:
            with LocalHTTP(302, headers={"Location": elsewhere.url + "/v1/messages"}) as api, self.claude_api(api):
                self.assertEqual(self.doctor(REPO, "--probe-claude", env=self.key_env()), UNREAD, self.text())
        self.assertEqual(elsewhere.requests, [])
        self.assertEqual(len(api.requests), 1)
        self.assertIn("the probe answered HTTP 302, not 200", self.out.getvalue())
        self.assertNotIn(SENTINEL_KEY, self.text())

    def test_a_server_error_from_the_probe_is_unknown(self):
        self.healthy()
        with LocalHTTP(500) as api, self.claude_api(api):
            self.assertEqual(self.doctor(REPO, "--probe-claude", env=self.key_env()), UNREAD, self.text())
        self.assertIn("the probe answered HTTP 500, not 200, so the key was not confirmed", self.out.getvalue())

    def test_a_subscription_token_alone_is_not_sent_to_the_api(self):
        self.healthy()
        with LocalHTTP(200) as api, self.claude_api(api):
            env = self.gh.env(CLAUDE_CODE_OAUTH_TOKEN=SENTINEL_OAUTH)
            self.assertEqual(self.doctor(REPO, "--probe-claude", env=env), UNREAD, self.text())
        self.assertEqual(api.requests, [])
        self.assertIn("[unknown] Claude token: CLAUDE_CODE_OAUTH_TOKEN is not probed", self.out.getvalue())
        self.assertNotIn(SENTINEL_OAUTH, self.text())

    def test_no_token_in_the_environment_is_unknown_without_a_call(self):
        self.healthy()
        with LocalHTTP(200) as api, self.claude_api(api):
            self.assertEqual(self.doctor(REPO, "--probe-claude"), UNREAD, self.text())
        self.assertEqual(api.requests, [])
        self.assertIn("[unknown] Claude token: no ANTHROPIC_API_KEY in your environment to probe", self.out.getvalue())

    def test_the_local_url_override_is_gone(self):
        source = (ROOT / "gh-piv" / "piv" / "doctor.py").read_text(encoding="utf-8")
        self.assertNotIn("PIV_ANTHROPIC_URL", source)


class MinutesTest(DoctorTest):
    def billing(self, **answer) -> dict:
        return {f"api users/{OWNER}/settings/billing/actions": [ok_json(answer)]}

    def test_no_minutes_left_on_a_private_repo_is_a_problem(self):
        private = [ok_json({"private": True, "owner": {"login": OWNER, "type": "User"}})]
        self.healthy(**{f"api repos/{REPO}": private, **self.billing(included_minutes=2000, total_minutes_used=2000)})
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn(
            "[problem] Actions minutes: none included left (2000 of 2000 used; the billing endpoint is unverified)",
            self.out.getvalue(),
        )
        self.assertEqual(len(fix_lines(self.out.getvalue())), 1)

    def test_minutes_left_on_a_private_repo_pass(self):
        private = [ok_json({"private": True, "owner": {"login": OWNER, "type": "User"}})]
        self.healthy(**{f"api repos/{REPO}": private, **self.billing(included_minutes=2000, total_minutes_used=10)})
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertEqual(problem_lines(self.out.getvalue()), [])
        self.assertIn("1990 of 2000 included minutes left (billing endpoint unverified)", self.out.getvalue())

    def test_a_public_repo_is_not_asked_about_minutes(self):
        self.healthy()
        self.doctor(REPO)
        self.assertIn("[skipped] Actions minutes: public repo", self.out.getvalue())
        self.assertFalse(any("billing" in " ".join(c["args"]) for c in self.gh_calls()))

    def test_billing_that_the_token_cannot_read_is_unknown_and_says_unverified(self):
        private = [ok_json({"private": True, "owner": {"login": OWNER, "type": "User"}})]
        forbidden = fakegh.fail("gh: Forbidden (HTTP 403)\n", 1)
        self.healthy(**{f"api repos/{REPO}": private, f"api users/{OWNER}/settings/billing/actions": [forbidden]})
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertIn(
            "[unknown] Actions minutes: could not be read (HTTP 403); the billing endpoint is unverified",
            self.out.getvalue(),
        )
        self.assertEqual(problem_lines(self.out.getvalue()), [])

    def test_every_billing_failure_status_is_unknown_not_a_pass_or_a_problem(self):
        private = [ok_json({"private": True, "owner": {"login": OWNER, "type": "User"}})]
        for status in (404, 410, 500):
            with self.subTest(status=status):
                self.gh = fakegh.FakeGh(self.root / f"bin{status}")  # a fresh fake: routes are first-match
                failure = fakegh.fail(f"gh: Error (HTTP {status})\n", 1)
                self.healthy(**{f"api repos/{REPO}": private, f"api users/{OWNER}/settings/billing/actions": [failure]})
                self.assertEqual(self.doctor(REPO), UNREAD, self.text())
                self.assertIn(f"[unknown] Actions minutes: could not be read (HTTP {status})", self.out.getvalue())

    def test_a_billing_answer_without_minute_counts_is_unknown_not_a_pass(self):
        private = [ok_json({"private": True, "owner": {"login": OWNER, "type": "User"}})]
        self.healthy(**{f"api repos/{REPO}": private, **self.billing(unexpected=True)})
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertIn("[unknown] Actions minutes: the billing answer has no minute counts", self.out.getvalue())


class RunnerTest(DoctorTest):
    PRIVATE = {"private": True, "owner": {"login": OWNER, "type": "User"}}

    def runner_routes(self, *runners: dict, kind: str = "self-hosted") -> dict:
        return {
            f"api repos/{REPO}": [ok_json(self.PRIVATE)],
            f"api repos/{REPO}/actions/variables/PIVOTAL_RUNNER": [ok_json({"value": kind})],
            f"api repos/{REPO}/actions/runners": [ok_json({"total_count": len(runners), "runners": list(runners)})],
        }

    def test_a_self_hosted_repo_with_no_online_runner_is_a_problem(self):
        self.healthy(**self.runner_routes({"status": "offline", "name": "pi"}))
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn("[problem] Runner: no self-hosted runner is online", self.out.getvalue())
        self.assertEqual(len(fix_lines(self.out.getvalue())), 1)

    def test_an_online_self_hosted_runner_passes(self):
        self.healthy(**self.runner_routes({"status": "online", "name": "pi"}))
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertEqual(problem_lines(self.out.getvalue()), [])
        self.assertIn("Runner: 1 self-hosted runner online", self.out.getvalue())

    def test_a_public_repo_without_trusted_only_runs_hosted_so_an_offline_runner_is_no_problem(self):
        public = {"private": False, "owner": {"login": OWNER, "type": "User"}}
        self.healthy(**{**self.runner_routes({"status": "offline", "name": "pi"}), f"api repos/{REPO}": [ok_json(public)]})
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertEqual(problem_lines(self.out.getvalue()), [])
        self.assertIn("Runner: GitHub-hosted runners (the repo is public", self.out.getvalue())
        self.assertFalse(any("actions/runners" in " ".join(c["args"]) for c in self.gh_calls()))

    def test_a_public_repo_with_trusted_only_still_needs_its_runner(self):
        public = {"private": False, "owner": {"login": OWNER, "type": "User"}}
        # routes are first-match by substring, so the longer variable name goes in before PIVOTAL_RUNNER
        self.gh.route(f"api repos/{REPO}/actions/variables/PIVOTAL_RUNNER_PUBLIC", ok_json({"value": "trusted-only"}))
        self.healthy(**{
            **self.runner_routes({"status": "offline", "name": "pi"}),
            f"api repos/{REPO}": [ok_json(public)],
        })
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn("[problem] Runner: no self-hosted runner is online", self.out.getvalue())

    def test_a_hosted_repo_does_not_ask_for_runners(self):
        self.healthy(**self.runner_routes(kind="hosted"))
        self.doctor(REPO)
        self.assertFalse(any("actions/runners" in " ".join(c["args"]) for c in self.gh_calls()))


class KaggleTest(DoctorTest):
    def kaggle(self, stdout: str = "", stderr: str = "", code: int = 0) -> Path:
        self.kaggle_log = self.root / "kaggle.jsonl"
        bin_dir = fakecli.FakeBin(self.root / "kbin")
        return bin_dir.add("kaggle", stdout=stdout, stderr=stderr, code=code, log=self.kaggle_log)

    def kaggle_env(self, binary: Path | None, **more: str) -> dict[str, str]:
        extra = {"KAGGLE_USERNAME": "dana", "KAGGLE_KEY": SENTINEL_KAGGLE, **more}
        if binary is not None:
            extra["PIV_KAGGLE"] = str(binary)
        return self.gh.env(**extra)

    def test_a_running_kernel_is_ok_and_kaggle_is_asked_about_the_kernel_by_name(self):
        binary = self.kaggle(stdout='User "dana/pivotal-serve" has status "KernelWorkerStatus.RUNNING"\n')
        self.healthy(**self.with_kaggle())
        with LocalHTTP(401) as endpoint:
            env = self.kaggle_env(binary, PIVOTAL_KAGGLE_URL=endpoint.url)
            self.assertEqual(self.doctor(REPO, env=env), UNREAD, self.text())  # quota is always unknown
        self.assertEqual(problem_lines(self.out.getvalue()), [])
        self.assertIn("Kaggle: kernel dana/pivotal-serve is running", self.out.getvalue())
        calls = [json.loads(line) for line in self.kaggle_log.read_text().splitlines()]
        self.assertEqual(calls, [["kernels", "status", "dana/pivotal-serve"]])

    def test_a_missing_kaggle_username_is_unknown_and_the_kernel_is_not_asked_for(self):
        binary = self.kaggle(stdout="running\n")
        self.healthy(**self.with_kaggle())
        env = self.kaggle_env(binary)
        del env["KAGGLE_USERNAME"]
        self.assertEqual(self.doctor(REPO, env=env), UNREAD, self.text())
        self.assertIn("[unknown] Kaggle: KAGGLE_USERNAME is not in your environment", self.out.getvalue())
        self.assertFalse(self.kaggle_log.exists())

    def test_a_kernel_in_error_is_a_problem_with_a_fix(self):
        binary = self.kaggle(stdout='User "dana/pivotal-serve" has status "error"\n')
        self.healthy(**self.with_kaggle())
        self.assertEqual(self.doctor(REPO, env=self.kaggle_env(binary)), 1, self.text())
        self.assertIn("[problem] Kaggle: kernel dana/pivotal-serve ended in error", self.out.getvalue())
        self.assertEqual(len(fix_lines(self.out.getvalue())), 1)

    def test_a_kaggle_refusal_is_a_problem_and_its_output_is_not_echoed(self):
        binary = self.kaggle(stderr=f"401 Unauthorized {SENTINEL_KAGGLE}\n", code=1)
        self.healthy(**self.with_kaggle())
        self.assertEqual(self.doctor(REPO, env=self.kaggle_env(binary)), 1, self.text())
        self.assertIn("could not read the kernel (exit 1)", self.out.getvalue())
        self.assertNotIn(SENTINEL_KAGGLE, self.text())

    def test_a_refusal_fix_line_names_only_things_that_exist(self):
        binary = self.kaggle(stderr="401\n", code=1)
        self.healthy(**self.with_kaggle())
        self.doctor(REPO, env=self.kaggle_env(binary))
        fix = fix_lines(self.out.getvalue())
        self.assertEqual(len(fix), 1)
        self.assertNotIn("piv kaggle setup", fix[0])
        self.assertIn("KAGGLE_USERNAME", fix[0])

    def test_kaggle_not_installed_is_unknown_because_a_kaggle_setup_is_expected(self):
        self.healthy(**self.with_kaggle())
        env = self.kaggle_env(self.root / "no-such-kaggle")
        self.assertEqual(self.doctor(REPO, env=env), UNREAD, self.text())
        self.assertIn(
            "[unknown] Kaggle: kaggle is not installed here, so the kernel is not checked", self.out.getvalue()
        )
        self.assertEqual(problem_lines(self.out.getvalue()), [])

    def test_quota_is_unknown_because_the_cli_cannot_read_it(self):
        self.healthy(**self.with_kaggle())
        self.doctor(REPO)
        self.assertIn("[unknown] Kaggle quota: the Kaggle CLI shows no GPU quota", self.out.getvalue())

    def test_a_repo_without_a_kaggle_url_secret_does_not_run_kaggle(self):
        binary = self.kaggle(stdout="running\n")
        self.healthy()
        self.doctor(REPO, env=self.kaggle_env(binary))
        self.assertFalse(self.kaggle_log.exists())


class EndpointTest(DoctorTest):
    def probe(self, url: str | None) -> int:
        """Doctor on a repo with the Kaggle endpoint secret, and the endpoint's URL in the environment (or none)."""
        self.healthy(**self.with_kaggle())
        extra = {} if url is None else {ENDPOINT_ENV: url}
        return self.doctor(REPO, env=self.gh.env(**extra))

    def fresh_gh(self, name: str) -> None:
        self.gh = fakegh.FakeGh(self.root / name)  # a fresh fake per case: routes are first-match

    def test_a_served_endpoint_is_reachable_and_no_key_is_sent(self):
        for status in (200, 401, 403):
            with self.subTest(status=status):
                self.fresh_gh(f"bin{status}")
                with LocalHTTP(status) as endpoint:
                    self.probe(endpoint.url)
                self.assertIn(f"[ok] Endpoint: answered HTTP {status} (no key was sent)", self.out.getvalue())
                request = endpoint.requests[0]
                self.assertEqual(request["path"], "/v1/models")
                self.assertIsNone(request["headers"].get("Authorization"))
                self.assertIsNone(request["headers"].get("x-api-key"))

    def test_a_tunnel_that_is_up_with_no_kernel_serving_is_a_problem(self):
        tunnel = {"ngrok-error-code": "3200"}
        cases = ((404, {}), (500, {}), (502, {}), (200, tunnel), (401, tunnel))
        for index, (status, headers) in enumerate(cases):
            with self.subTest(status=status, headers=headers):
                self.fresh_gh(f"case{index}")
                with LocalHTTP(status, headers=headers) as endpoint:
                    self.assertEqual(self.probe(endpoint.url), 1, self.text())
                self.assertIn(
                    f"[problem] Endpoint: the tunnel is up but the Kaggle kernel is not serving (HTTP {status})",
                    self.out.getvalue(),
                )
                self.assertEqual(len(fix_lines(self.out.getvalue())), 1)

    def test_another_answer_is_a_problem_because_it_is_not_a_served_api(self):
        with LocalHTTP(400) as endpoint:
            self.assertEqual(self.probe(endpoint.url), 1, self.text())
        self.assertIn("[problem] Endpoint: answered HTTP 400, which is not a served API answer", self.out.getvalue())

    def test_an_endpoint_that_does_not_answer_is_a_problem_with_a_fix(self):
        self.assertEqual(self.probe(closed_url()), 1, self.text())
        self.assertIn("[problem] Endpoint: the ngrok domain did not answer", self.out.getvalue())
        self.assertEqual(len(fix_lines(self.out.getvalue())), 1)

    def test_a_url_that_is_not_http_is_a_problem(self):
        self.assertEqual(self.probe("ftp://example.invalid"), 1, self.text())
        self.assertIn(
            f"[problem] Endpoint: {ENDPOINT_ENV} in your environment is not an http(s) URL", self.out.getvalue()
        )

    def test_no_url_in_the_environment_is_unknown_for_a_repo_that_uses_kaggle(self):
        self.assertEqual(self.probe(None), UNREAD, self.text())
        self.assertIn(f"[unknown] Endpoint: {ENDPOINT_ENV} is not in your environment", self.out.getvalue())

    def test_a_repo_without_kaggle_is_not_probed_and_is_not_asked_about(self):
        self.healthy()
        with LocalHTTP(200) as endpoint:
            self.doctor(REPO, env=self.gh.env(**{ENDPOINT_ENV: endpoint.url}))
        self.assertEqual(endpoint.requests, [])
        self.assertNotIn("Endpoint", self.out.getvalue())


class AllReposTest(DoctorTest):
    SECOND = "0xctx/other"

    def search(self, *repos: str) -> dict:
        items = [{"full_name": name} for name in repos]
        return {f"api {SEARCH}": [ok_json({"total_count": len(items), "items": items})]}

    def test_all_checks_every_repo_with_the_topic_and_exits_1_if_any_has_a_problem(self):
        self.healthy(self.SECOND, **{f"api repos/{self.SECOND}/actions/permissions": [ok_json({"enabled": False})]})
        self.healthy(REPO)
        found = [{"full_name": REPO}, {"full_name": self.SECOND}]
        self.gh.route(f"api {SEARCH}", ok_json({"total_count": 2, "items": found}))
        self.gh.route("api user", ok_json({"login": OWNER}))
        self.assertEqual(self.doctor("--all"), 1, self.text())
        out = self.out.getvalue()
        self.assertIn(f"piv doctor {REPO}", out)
        self.assertIn(f"piv doctor {self.SECOND}", out)
        self.assertIn(f"[problem] Actions: disabled in {self.SECOND}", out)
        self.assertEqual(len(problem_lines(out)), len(fix_lines(out)))

    def test_all_with_no_repo_on_the_topic_says_so_and_the_unprobed_claude_token_is_unknown(self):
        self.gh.route("api user", ok_json({"login": OWNER}))
        self.gh.route(f"api {SEARCH}", ok_json({"total_count": 0, "items": []}))
        self.assertEqual(self.doctor("--all"), UNREAD, self.text())
        self.assertIn("no repo carries the pivotal topic", self.out.getvalue())

    def test_all_over_healthy_repos_exits_unread_not_clean(self):
        self.healthy(self.SECOND)
        self.healthy(REPO)
        found = [{"full_name": REPO}, {"full_name": self.SECOND}]
        self.gh.route(f"api {SEARCH}", ok_json({"total_count": 2, "items": found}))
        self.gh.route("api user", ok_json({"login": OWNER}))
        self.assertEqual(self.doctor("--all"), UNREAD, self.text())
        self.assertEqual(problem_lines(self.out.getvalue()), [])
        self.assertIn("1 check(s) could not be read", self.out.getvalue())


class ExitCodeTest(DoctorTest):
    def test_a_problem_outranks_an_unread_check_and_the_footer_names_both(self):
        self.healthy(**{f"api repos/{REPO}/actions/permissions": [ok_json({"enabled": False})]})
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn("1 problem(s), each with a fix line above", self.out.getvalue())
        self.assertIn("1 check(s) could not be read", self.out.getvalue())

    def test_a_clean_run_with_the_claude_probe_passes_and_says_no_problems(self):
        self.healthy()
        with LocalHTTP(200) as api, mock.patch.object(doctor, "ANTHROPIC_URL", api.url + "/v1/messages"):
            code = self.doctor(REPO, "--probe-claude", env=self.gh.env(ANTHROPIC_API_KEY=SENTINEL_KEY))
        self.assertEqual(code, 0, self.text())
        self.assertIn("no problems", self.out.getvalue())
        self.assertNotIn("could not be read", self.out.getvalue())


class UsageTest(DoctorTest):
    def test_a_repo_and_all_together_exit_2_with_no_gh_call(self):
        self.assertEqual(self.doctor(REPO, "--all"), 2, self.text())
        self.assertEqual(self.gh_calls(), [])

    def test_neither_a_repo_nor_all_exits_2_with_no_gh_call(self):
        self.assertEqual(self.doctor(), 2, self.text())
        self.assertEqual(self.gh_calls(), [])
        self.assertIn("give REPO or --all", self.err.getvalue())

    def test_a_bad_repo_name_exits_2_with_no_gh_call(self):
        self.assertEqual(self.doctor("not-a-repo"), 2, self.text())
        self.assertEqual(self.gh_calls(), [])


class ReadOnlyTest(DoctorTest):
    def test_every_gh_call_is_a_get_with_no_field_or_body_flag(self):
        self.healthy()
        self.doctor(REPO)
        calls = self.gh_calls()
        self.assertTrue(calls, "the doctor made no gh calls")
        write_flags = {"-X", "--method", "-f", "-F", "--raw-field", "--field", "--input"}
        for call in calls:
            self.assertEqual(call["args"][0], "api", call["args"])
            self.assertFalse(write_flags & set(call["args"]), call["args"])
            self.assertEqual(call["stdin"], "", call["args"])

    def test_a_broken_repo_still_makes_only_reads(self):
        self.healthy(**{
            f"api repos/{REPO}/actions/permissions": [ok_json({"enabled": False})],
            f"api repos/{REPO}/contents/{WORKFLOW}": [fakegh.not_found()],
        })
        self.doctor(REPO)
        self.assertTrue(all(call["args"][0] == "api" and "-X" not in call["args"] for call in self.gh_calls()))


class UserVisibleTest(DoctorTest):
    def test_the_launcher_prints_the_fix_lines_for_a_broken_repo_and_exits_1(self):
        self.healthy(**{
            f"api repos/{REPO}/actions/permissions": [ok_json({"enabled": False})],
            f"api repos/{REPO}/actions/secrets": [secrets()],
            f"api repos/{REPO}/contents/{WORKFLOW}": [fakegh.not_found()],
        })
        work = self.root / "work"
        work.mkdir()
        done = subprocess.run(
            [str(LAUNCHER), "doctor", REPO], cwd=work, env=self.gh.env(), capture_output=True, text=True, check=False
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("[problem] Actions: disabled", done.stdout)
        self.assertIn("[problem] Secrets: no repo-level model key is set", done.stdout)
        self.assertIn("[problem] Caller:", done.stdout)
        self.assertEqual(len(problem_lines(done.stdout)), len(fix_lines(done.stdout)))
        self.assertEqual(list(work.iterdir()), [], "doctor wrote to the working directory")


class OrganizationSecretsTest(DoctorTest):
    """A repo owned by an organization may take its model key from an organization secret (R4)."""

    def org_repo(self, **replace: list[dict]) -> None:
        org = {"private": False, "owner": {"login": OWNER, "type": "Organization"}}
        base = {f"api repos/{REPO}": [ok_json(org)], f"api repos/{REPO}/actions/secrets": [secrets()]}
        self.healthy(**base, **replace)

    def org_secrets(self, *items: dict) -> dict:
        listed = {"total_count": len(items), "secrets": list(items)}
        return {f"api orgs/{OWNER}/actions/secrets?per_page=100": [ok_json(listed)]}

    def test_a_model_key_from_the_organization_the_repo_can_see_passes(self):
        self.org_repo(**self.org_secrets({"name": "ANTHROPIC_API_KEY", "visibility": "all"}))
        self.doctor(REPO)
        self.assertIn("[ok] Secrets: a model key is set at the organization (ANTHROPIC_API_KEY)", self.out.getvalue())
        self.assertEqual(problem_lines(self.out.getvalue()), [])

    def test_a_selected_organization_key_counts_only_for_a_repo_in_its_list(self):
        selected = {"name": "ANTHROPIC_API_KEY", "visibility": "selected"}
        self.org_repo(
            **self.org_secrets(selected),
            **{f"api orgs/{OWNER}/actions/secrets/ANTHROPIC_API_KEY/repositories": [
                ok_json({"total_count": 1, "repositories": [{"full_name": REPO}]})
            ]},
        )
        self.doctor(REPO)
        self.assertIn("a model key is set at the organization (ANTHROPIC_API_KEY)", self.out.getvalue())

        self.gh = fakegh.FakeGh(self.root / "other")
        self.org_repo(
            **self.org_secrets(selected),
            **{f"api orgs/{OWNER}/actions/secrets/ANTHROPIC_API_KEY/repositories": [
                ok_json({"total_count": 1, "repositories": [{"full_name": "0xctx/elsewhere"}]})
            ]},
        )
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn(NO_ORG_KEY, self.out.getvalue())

    def test_a_selected_list_with_a_null_name_does_not_crash_and_does_not_match(self):
        selected = {"name": "ANTHROPIC_API_KEY", "visibility": "selected"}
        self.org_repo(
            **self.org_secrets(selected),
            **{f"api orgs/{OWNER}/actions/secrets/ANTHROPIC_API_KEY/repositories": [
                ok_json({"total_count": 1, "repositories": [{"full_name": None}]})
            ]},
        )
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn(NO_ORG_KEY, self.out.getvalue())

    def test_an_organization_whose_secrets_cannot_be_read_is_unknown_not_a_problem(self):
        denied = fakegh.fail("gh: Resource not accessible by integration (HTTP 403)\n", 1)
        self.org_repo(**{f"api orgs/{OWNER}/actions/secrets?per_page=100": [denied]})
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertIn(UNKNOWN_ORG + " (HTTP 403)", self.out.getvalue())
        self.assertIn("an organization key is not ruled out", self.out.getvalue())
        self.assertEqual(problem_lines(self.out.getvalue()), [])

    def test_an_organization_with_no_model_key_is_a_problem_naming_the_repo_level_gap(self):
        self.org_repo(**self.org_secrets({"name": "GROQ_OTHER", "visibility": "all"}))
        self.assertEqual(self.doctor(REPO), 1, self.text())
        self.assertIn(NO_ORG_KEY, self.out.getvalue())
        self.assertEqual(len(fix_lines(self.out.getvalue())), 1)

    def test_a_user_owned_repo_does_not_ask_the_organization_endpoint(self):
        self.healthy()
        self.doctor(REPO)
        self.assertFalse(any(call["args"][1].startswith("orgs/") for call in self.gh_calls() if len(call["args"]) > 1))

    def test_an_unknown_visibility_is_unknown_not_a_pass(self):
        self.org_repo(**self.org_secrets({"name": "ANTHROPIC_API_KEY", "visibility": "something-new"}))
        self.assertEqual(self.doctor(REPO), UNREAD, self.text())
        self.assertIn(UNKNOWN_ORG, self.out.getvalue())


class ReadFailureFixTest(DoctorTest):
    def test_each_read_failure_has_its_own_fix_line(self):
        self.healthy(**{
            f"api repos/{REPO}/actions/permissions": [fakegh.fail("gh: Server Error (HTTP 500)\n")],
            f"api repos/{REPO}/contents/{WORKFLOW}": [fakegh.fail("gh: Server Error (HTTP 500)\n")],
        })
        self.doctor(REPO)
        fixes = fix_lines(self.out.getvalue())
        self.assertEqual(len(fixes), 2, self.out.getvalue())
        self.assertEqual(len(set(fixes)), 2, "two read failures share one fix line")
        self.assertIn("Actions settings", fixes[0])
        self.assertIn("caller file", fixes[1])


class SharedDefinitionsTest(unittest.TestCase):
    def test_the_model_keys_are_init_secrets_and_not_a_copy(self):
        self.assertTrue(set(doctor.MODEL_KEYS) <= set(init.SECRETS))
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", doctor.MODEL_KEYS)
        self.assertNotIn("KAGGLE_KEY", doctor.MODEL_KEYS)

    def test_the_kaggle_and_runner_names_are_init_definitions_and_doctor_copies_none(self):
        self.assertIn(init.KAGGLE_URL, init.SECRETS)
        self.assertIn(init.KAGGLE_KEY, init.SECRETS)
        self.assertEqual(init.KAGGLE_URL, "PIVOTAL_KAGGLE_URL")
        self.assertEqual(init.KAGGLE_KEY, "PIVOTAL_KAGGLE_KEY")
        self.assertEqual(init.RUNNER_VARIABLE, "PIVOTAL_RUNNER")

        source = (ROOT / "gh-piv" / "piv" / "doctor.py").read_text(encoding="utf-8")
        for literal in ('"PIVOTAL_KAGGLE_URL"', '"PIVOTAL_KAGGLE_KEY"', '"PIVOTAL_RUNNER"'):
            self.assertNotIn(literal, source)


if __name__ == "__main__":
    unittest.main()
