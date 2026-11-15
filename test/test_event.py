"""piv event: the repository_dispatch that a webhook's alert is sent as.

Every gh call goes to the fake (test/fakegh.py), which logs it and refuses a flag the real gh does not
take. No test reaches GitHub: PIV_GH always names the fake.
"""

import base64
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fakegh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))

from piv import cli, event, init  # noqa: E402
from engine import trust  # noqa: E402

REPO = "0xctx/piv-sandbox"
TITLE = "Sentry: checkout returns 500"
LINK = f"https://github.com/{REPO}/actions/runs/4242"  # the one host piv event keeps a link to (I4: the rule ack keeps)
SHA = "a" * 40  # a release pin, as piv init writes it
DISPATCHES = f"repos/{REPO}/dispatches"
CALLER = f"repos/{REPO}/contents/{init.WORKFLOW_PATH}"
REPO_PATH = f"repos/{REPO}"  # the probe a 404 on the caller makes; it must come after the two routes above
PAYLOAD = {"event_type": "pivotal-event", "client_payload": {"kind": "incident", "title": TITLE, "link": LINK}}
NO_CONTENT = "HTTP/2.0 204 No Content\r\nDate: Wed, 28 Oct 2026 00:00:00 GMT\r\n\r\n"  # gh api -i, as GitHub sends it
LAUNCHER = ROOT / "gh-piv" / "gh-piv"
WRITE_FLAGS = ("POST", "PUT", "PATCH", "DELETE")
TEMPLATE = (ROOT / "templates" / "caller.yml").read_text(encoding="utf-8")
LISTS_EVENT = "types: [pivotal-event, pivotal-fix]"


def is_write(call: dict) -> bool:
    args = call["args"]
    return "-X" in args and args[args.index("-X") + 1] in WRITE_FLAGS


def caller_file(text: str) -> str:
    """The GET answer for the caller: its content, base64, as the contents API gives it."""
    return json.dumps({"name": "pivotal.yml", "sha": "b" * 40, "content": base64.b64encode(text.encode()).decode()})


class EventTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.gh = fakegh.FakeGh(self.root / "bin")
        self.out = io.StringIO()
        self.err = io.StringIO()
        # Routes are tried in order: the caller and the dispatch come before the repo probe, which matches both.
        self.gh.route(CALLER, fakegh.ok(caller_file(init.render_caller(TEMPLATE, SHA))))
        self.gh.route(DISPATCHES, fakegh.ok(NO_CONTENT))
        self.gh.route(REPO_PATH, fakegh.ok('{"full_name": "0xctx/piv-sandbox"}'))

    def tearDown(self):
        self._tmp.cleanup()

    def send(self, **fields: str) -> int:
        values = {"repo": REPO, "kind": "incident", "title": TITLE, "link": LINK, **fields}
        argv = ["event", values["repo"], "--kind", values["kind"], "--title", values["title"], "--link", values["link"]]
        return cli.main(argv, self.gh.env(), self.out, self.err)

    def writes(self) -> list[dict]:
        return [c for c in self.gh.calls() if is_write(c)]

    def reset_output(self) -> None:
        self.out, self.err = io.StringIO(), io.StringIO()

    def test_sends_one_pivotal_event_dispatch_with_the_kind_title_and_link(self):
        self.assertEqual(self.send(), 0, self.err.getvalue())
        writes = self.writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["args"], ["api", "-X", "POST", DISPATCHES, "-i", "--input", "-"])
        self.assertEqual(json.loads(writes[0]["stdin"]), PAYLOAD)

    def test_the_title_and_link_go_in_the_body_and_never_in_argv(self):
        self.send()
        args = self.writes()[0]["args"]
        self.assertFalse(any(TITLE in arg or LINK in arg for arg in args), args)

    def test_a_204_is_success_and_the_output_says_what_the_dispatch_does_and_does_not_do(self):
        self.assertEqual(self.send(), 0, self.err.getvalue())
        self.assertIn(f"pivotal-event to {REPO}, kind incident", self.out.getvalue())
        self.assertIn("trust: the caller decides on the sender", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_the_first_output_line_says_what_was_sent_and_not_that_an_issue_is_filed(self):
        self.assertEqual(self.send(), 0, self.err.getvalue())
        lines = self.out.getvalue().splitlines()
        self.assertEqual(lines[0], f"sent: pivotal-event to {REPO}, kind incident")

    def test_the_output_says_a_trusted_sender_files_an_issue_and_an_untrusted_one_runs_nothing(self):
        self.send()
        self.assertIn("files one incident issue", self.out.getvalue())
        self.assertIn("runs nothing", self.out.getvalue())

    def test_the_title_is_stripped_before_it_is_sent(self):
        self.assertEqual(self.send(title="  Uptime: api down  "), 0, self.err.getvalue())
        self.assertEqual(json.loads(self.writes()[0]["stdin"])["client_payload"]["title"], "Uptime: api down")

    def test_a_title_at_the_limit_is_sent(self):
        self.assertEqual(self.send(title="t" * event.TITLE_MAX), 0, self.err.getvalue())

    def test_a_run_link_of_this_repo_is_sent_at_the_digit_limits(self):
        # The one shape lines.link_request builds from (and ack keeps): this repo's run, with an optional job, ids of 1 to 20 digits.
        for link in (LINK, f"{LINK}/job/7", f"https://github.com/{REPO}/actions/runs/{'9' * 20}",
                     f"https://github.com/{REPO}/actions/runs/1/job/{'9' * 20}"):
            with self.subTest(link=link):
                self.reset_output()
                self.assertEqual(self.send(link=link), 0, self.err.getvalue())

    def test_the_refusal_names_the_digit_limits_the_rule_enforces(self):
        # S1: the message and the rule agree. The message says 1 to 20 digits in each id; the rule takes 1 and 20 and refuses 0 and 21.
        self.assertIn("1 to 20 digits", event.link_refusal(REPO))
        for digits, sent in (("", False), ("9", True), ("9" * 20, True), ("9" * 21, False)):
            with self.subTest(digits=len(digits)):
                self.reset_output()
                calls = len(self.gh.calls())
                code = self.send(link=f"https://github.com/{REPO}/actions/runs/{digits}")
                self.assertEqual(code, 0 if sent else 2, self.err.getvalue())
                if not sent:
                    self.assertEqual(len(self.gh.calls()), calls, "a refused link makes no gh call")

    def test_a_link_that_is_not_exactly_a_run_of_this_repo_is_refused_before_any_call(self):
        # Before this, piv event sent a link ack kept but lines.link_request refused after the issue was filed (I-A). Each link
        # here was accepted by the old character-set rule: a query, a fragment, an issue, a pull request, an attempt, another
        # repository, no run id, a 21-digit id, a trailing slash.
        bad = [
            f"{LINK}?x=1", f"{LINK}#top", f"https://github.com/{REPO}/issues/3", f"https://github.com/{REPO}/pull/5",
            f"https://github.com/{REPO}/actions/runs/9/attempts/2", "https://github.com/other/repo/actions/runs/9",
            f"https://github.com/{REPO}/actions/runs/", f"https://github.com/{REPO}/actions/runs/x",
            f"https://github.com/{REPO}/actions/runs/9/job/", f"https://github.com/{REPO}/actions/runs/9/job/1/x",
            f"https://github.com/{REPO}/actions/runs/{'9' * 21}", f"https://github.com/{REPO}/actions/runs/1/job/{'9' * 21}",
            f"https://github.com/{REPO}/actions/runs/9/", f"https://github.com/{REPO}/actions/runs/9.", f"https://github.com/{REPO}",
            f"https://github.com/{REPO}/", f"https://github.com/{REPO}/actions/runs/9 ", f"https://github.com/{REPO}_/actions/runs/9",
            f"https://github.com/{REPO}x/actions/runs/9",
        ]
        for link in bad:
            with self.subTest(link=link):
                self.reset_output()
                self.assertEqual(self.send(link=link), 2, self.err.getvalue())
                self.assertIn(f"--link must be https://github.com/{REPO}/actions/runs/N", self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_the_kinds_are_the_ones_the_caller_runs_a_factory_for(self):
        listed = re.search(r"fromJSON\('\[([^\]]*)\]'\)", TEMPLATE).group(1)
        self.assertLessEqual(set(event.KINDS), set(json.loads(f"[{listed}]")))
        self.assertLessEqual(set(event.KINDS), trust.KINDS)

    def test_both_caller_kinds_send_their_own_kind(self):
        for kind in event.KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(self.send(kind=kind), 0, self.err.getvalue())
                self.assertEqual(json.loads(self.writes()[-1]["stdin"])["client_payload"]["kind"], kind)

    def test_any_other_kind_is_refused_before_any_call(self):
        for kind in ("go", "fix", "land", "stop", "none", "Incident", ""):
            with self.subTest(kind=kind):
                self.assertEqual(self.send(kind=kind), 2)
                self.assertEqual(self.gh.calls(), [])

    def test_a_bad_repo_is_refused_before_any_call(self):
        for repo in ("piv-sandbox", "/piv-sandbox", "0xctx/", "0xctx/a/b", "0xctx/.."):
            with self.subTest(repo=repo):
                self.assertEqual(self.send(repo=repo), 2)
                self.assertEqual(self.gh.calls(), [])

    def test_a_blank_multi_line_or_unsafe_title_is_refused_before_any_call(self):
        bad = [
            "", "   ", "a\nb", "a\rb", "a\tb", "a\x00b", "a\x1bb", "a\x7fb", "a\x85b", "a\x9bb",
            "a\udcffb",
            "t" * (event.TITLE_MAX + 1),
        ]
        for title in bad:
            with self.subTest(title=title):
                self.assertEqual(self.send(title=title), 2, self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_bidi_control_is_refused_before_any_call(self):
        for title in ("a‮b", "a⁦b", "a‎b", "a؜b", "a⁩b"):
            with self.subTest(title=title):
                self.assertEqual(self.send(title=title), 2, self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_format_private_use_or_unassigned_character_is_refused_before_any_call(self):
        # Cf (zero-width space, word joiner, BOM, soft hyphen, tag characters), Co (private use), Cn (unassigned).
        bad = ["a​b", "a⁠b", "a﻿b", "a­b", "a\U000e0041b", "ab", "a͸b"]
        for title in bad:
            with self.subTest(title=title):
                self.assertEqual(self.send(title=title), 2, self.err.getvalue())
                self.assertIn("--title must be one line of plain text", self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_non_space_separator_and_a_blank_looking_letter_are_refused_before_any_call(self):
        # Zs other than the ordinary space; braille blank and the Hangul fillers, which are letters or symbols.
        bad = ["a b", "a b", "a　b", "a⠀b", "aㅤb", "aᅟb", "aᅠb", "aﾠb"]
        for title in bad:
            with self.subTest(title=title):
                self.assertEqual(self.send(title=title), 2, self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_title_with_no_visible_character_is_refused_before_any_call(self):
        for title in ("​", "​⁠", "⠀", "ㅤ", "́", "  "):
            with self.subTest(title=title):
                self.assertEqual(self.send(title=title), 2, self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_normal_title_with_ordinary_spaces_and_non_ascii_letters_is_sent(self):
        for title in ("Sentry: café 500 — checkout ✓", "Uptime: 北京 api down", "Erreur: données 5xx"):
            with self.subTest(title=title):
                self.reset_output()
                self.assertEqual(self.send(title=title), 0, self.err.getvalue())

    def test_a_link_that_is_not_a_github_link_is_refused_before_any_call(self):
        bad = [
            "", "not a link", "http://github.com/0xctx/x", "ftp://github.com/0xctx/x", "javascript:alert(1)", "https://",
            "https:///x", "https://github.com", "https://github.com/", "https://github.com/a b",
            "https://github.com/a\nb", "https://github.com/ä", "https://github.com/‮", "https://[::1/x",
            "https://github.com/" + "a" * 251,
        ]
        for link in bad:
            with self.subTest(link=link):
                self.assertEqual(self.send(link=link), 2, self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_link_to_any_other_host_is_refused_even_when_it_is_a_public_https_site(self):
        # The host is github.com and no other: a Sentry or status page link is a valid https URL that the run would not keep,
        # so piv event refuses it instead of sending a link ack replaces (I4).
        bad = [
            "https://sentry.io/organizations/acme/issues/4242/", "https://8.8.8.8/x", "https://status.example.com:8443/x",
            "https://localhost/x", "https://127.0.0.1/x", "https://169.254.169.254/latest", "https://api.github.com/x",
            "https://gist.github.com/x", "https://github.com.evil.example/x",
        ]
        for link in bad:
            with self.subTest(link=link):
                self.assertEqual(self.send(link=link), 2, self.err.getvalue())
                self.assertIn(f"--link must be https://github.com/{REPO}/actions/runs/N", self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_link_with_userinfo_is_refused_before_any_call(self):
        # S4: userinfo is refused on the run link of this repo too, not only on a path that is no run.
        for link in ("https://user:pw@github.com/x", "https://github.com@evil.example/x",
                     f"https://user:pw@github.com/{REPO}/actions/runs/9", f"https://user@github.com/{REPO}/actions/runs/9"):
            with self.subTest(link=link):
                self.assertEqual(self.send(link=link), 2, self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_link_with_a_port_is_refused_before_any_call(self):
        for link in ("https://github.com:99999/x", "https://github.com:abc/x", "https://github.com:0/x", "https://github.com:443/x"):
            with self.subTest(link=link):
                self.assertEqual(self.send(link=link), 2, self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_link_with_a_character_outside_the_kept_set_is_refused_before_any_call(self):
        # Markdown and quoting characters, brackets, a backslash, and the characters ack's charset leaves out.
        bad = [
            "https://github.com/)[x](http://evil", "https://github.com/<x>", 'https://github.com/"x',
            "https://github.com/`x`", "https://github.com/a\\b", "https://github.com/{x}", "https://github.com/[x]",
            "https://github.com/(x)", "https://github.com/a@b", "https://github.com/a!b", "https://github.com/a,b",
            "https://github.com/a'b", "https://github.com/a*b", "https://github.com/a;b", "https://github.com/a$b",
        ]
        for link in bad:
            with self.subTest(link=link):
                self.assertEqual(self.send(link=link), 2, self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])

    def test_a_repo_without_the_caller_is_refused_and_nothing_is_sent(self):
        self.gh.route(CALLER, fakegh.not_found(), first=True)
        self.assertEqual(self.send(), 1)
        self.assertIn("piv init", self.err.getvalue())
        self.assertEqual(self.writes(), [])
        self.assertNotIn("sent", self.out.getvalue())

    def test_a_missing_repo_is_not_reported_as_a_missing_caller(self):
        self.gh.route(REPO_PATH, fakegh.not_found(), first=True)  # the caller route goes in front of it, below
        self.gh.route(CALLER, fakegh.not_found(), first=True)
        self.assertEqual(self.send(), 1)
        self.assertIn("not found, or you have no access", self.err.getvalue())
        self.assertNotIn("piv init", self.err.getvalue())
        self.assertEqual(self.writes(), [])

    def test_a_probe_that_fails_for_another_reason_sends_nothing(self):
        self.gh.route(REPO_PATH, fakegh.fail("gh: Server Error (HTTP 500)\n"), first=True)
        self.gh.route(CALLER, fakegh.not_found(), first=True)
        self.assertEqual(self.send(), 1)
        self.assertEqual(self.writes(), [])

    def test_a_caller_read_that_is_forbidden_names_the_repo_and_sends_nothing(self):
        self.gh.route(CALLER, fakegh.fail("gh: Resource not accessible by integration (HTTP 403)\n"), first=True)
        self.assertEqual(self.send(), 1)
        self.assertIn(f"piv event {REPO} could not read {init.WORKFLOW_PATH}", self.err.getvalue())
        self.assertIn("HTTP 403", self.err.getvalue())
        self.assertIn("read access", self.err.getvalue())
        self.assertEqual(self.writes(), [])

    def test_a_caller_read_that_fails_sends_nothing(self):
        self.gh.route(CALLER, fakegh.fail("gh: Server Error (HTTP 500)\n"), first=True)
        self.assertEqual(self.send(), 1)
        self.assertEqual(self.writes(), [])

    def test_the_only_read_before_the_write_is_a_get_of_the_caller(self):
        self.send()
        self.assertEqual(self.gh.calls()[0]["args"], ["api", CALLER])

    def test_a_caller_that_does_not_listen_for_pivotal_event_is_refused_and_nothing_is_sent(self):
        text = init.render_caller(TEMPLATE, SHA).replace(LISTS_EVENT, "types: [pivotal-fix]")
        self.gh.route(CALLER, fakegh.ok(caller_file(text)), first=True)
        self.assertEqual(self.send(), 1)
        self.assertIn("does not listen for pivotal-event", self.err.getvalue())
        self.assertIn("piv init", self.err.getvalue())
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.gh.calls(), [{"args": ["api", CALLER], "stdin": ""}])  # the dry path reads nothing else

    def test_a_caller_pinned_to_the_placeholder_zeros_is_refused_and_nothing_is_sent(self):
        self.gh.route(CALLER, fakegh.ok(caller_file(TEMPLATE)), first=True)
        self.assertEqual(self.send(), 1)
        self.assertIn("not pinned to a release", self.err.getvalue())
        self.assertEqual(self.writes(), [])
        self.assertEqual(len(self.gh.calls()), 1)

    def test_a_caller_with_no_pin_at_all_is_refused_and_nothing_is_sent(self):
        text = "on:\n  repository_dispatch:\n    types: [pivotal-event]\n"
        self.gh.route(CALLER, fakegh.ok(caller_file(text)), first=True)
        self.assertEqual(self.send(), 1)
        self.assertIn("not pinned to a release", self.err.getvalue())
        self.assertEqual(self.writes(), [])

    def test_a_caller_that_is_not_json_or_has_no_content_is_refused_and_nothing_is_sent(self):
        for answer in ("not json", json.dumps({"name": "pivotal.yml"})):
            with self.subTest(answer=answer):
                self.gh.route(CALLER, fakegh.ok(answer), first=True)
                self.reset_output()
                self.assertEqual(self.send(), 1)
                self.assertEqual(self.writes(), [])

    def test_a_refused_dispatch_exits_1_and_says_it_was_not_sent(self):
        for status in (422, 404):
            with self.subTest(status=status):
                self.gh.route(DISPATCHES, fakegh.fail(f"gh: Failed (HTTP {status})\n"), first=True)
                self.reset_output()
                self.assertEqual(self.send(), 1)
                self.assertIn("was not sent", self.err.getvalue())
                self.assertIn(f"HTTP {status}", self.err.getvalue())
                self.assertNotIn("sent (", self.out.getvalue())

    def test_a_dispatch_refused_for_access_says_it_needs_write_access(self):
        for status in (403, 404):
            with self.subTest(status=status):
                self.gh.route(DISPATCHES, fakegh.fail(f"gh: Resource not accessible (HTTP {status})\n"), first=True)
                self.reset_output()
                self.assertEqual(self.send(), 1)
                self.assertIn("dispatch needs write access (repo scope, or fine-grained Contents: write)", self.err.getvalue())
                self.assertIn(f"HTTP {status}", self.err.getvalue())
                self.assertNotIn("sent (", self.out.getvalue())

    def test_a_422_does_not_get_the_write_access_hint(self):
        self.gh.route(DISPATCHES, fakegh.fail("gh: Validation Failed (HTTP 422)\n"), first=True)
        self.assertEqual(self.send(), 1)
        self.assertNotIn("write access", self.err.getvalue())

    def test_a_dispatch_answer_that_is_not_204_is_not_a_success(self):
        answers = {
            "HTTP/2.0 200 OK\r\n\r\n{}": "200 OK",
            "HTTP/2.0 202 Accepted\r\n\r\n": "202 Accepted",
            "": "no status line",
        }
        for stdout, shown in answers.items():
            with self.subTest(stdout=stdout):
                self.gh.route(DISPATCHES, fakegh.ok(stdout), first=True)
                self.reset_output()
                self.assertEqual(self.send(), 1)
                self.assertIn("was not sent", self.err.getvalue())
                self.assertIn(shown, self.err.getvalue())
                self.assertNotIn("sent (", self.out.getvalue())

    def test_the_trust_on_a_dispatch_is_an_event_untrusted_when_the_repos_own_token_sent_it(self):
        # Inverted on purpose (I3): piv event is sent with a writer's token, and nothing in the pipeline sends an event with
        # the repo's own GITHUB_TOKEN, so that sender proves no writer and the event runs nothing. No lookup is made (env={}).
        payload = {
            "action": "pivotal-event", "client_payload": PAYLOAD["client_payload"], "repository": {"full_name": REPO},
            "sender": {"login": "github-actions[bot]", "type": "Bot"},
        }
        decision = trust.decide("repository_dispatch", payload, REPO, env={})
        self.assertFalse(decision.trusted)
        self.assertEqual((decision.kind, decision.issue), ("event", None))

    def test_the_trust_on_a_dispatch_with_no_repository_is_none(self):
        payload = {"action": "pivotal-event", "client_payload": PAYLOAD["client_payload"]}
        decision = trust.decide("repository_dispatch", payload, REPO, env={})
        self.assertFalse(decision.trusted)
        self.assertEqual(decision.kind, "none")


class EventLauncherTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.gh = fakegh.FakeGh(self.root / "bin")
        self.gh.route(CALLER, fakegh.ok(caller_file(init.render_caller(TEMPLATE, SHA))))
        self.gh.route(DISPATCHES, fakegh.ok(NO_CONTENT))

    def tearDown(self):
        self._tmp.cleanup()

    def run_launcher(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(LAUNCHER), *args], cwd=cwd, env=self.gh.env(), capture_output=True, text=True, check=False
        )

    def test_user_visible_check_sends_the_dispatch_and_exits_0(self):
        work = self.root / "work"
        work.mkdir()
        done = self.run_launcher("event", REPO, "--kind", "incident", "--title", TITLE, "--link", LINK, cwd=work)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("sent: pivotal-event to"), done.stdout)
        self.assertEqual(json.loads(self.gh.calls()[-1]["stdin"]), PAYLOAD)

    def test_without_a_title_it_exits_2_with_a_usage_message_and_calls_nothing(self):
        done = self.run_launcher("event", REPO, "--kind", "incident", "--link", LINK, cwd=self.root)
        self.assertEqual(done.returncode, 2)
        self.assertIn("--title", done.stderr)
        self.assertEqual(self.gh.calls(), [])

    def test_writes_nothing_to_the_working_directory(self):
        work = self.root / "work"
        work.mkdir()
        self.run_launcher("event", REPO, "--kind", "incident", "--title", TITLE, "--link", LINK, cwd=work)
        self.assertEqual(list(work.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
