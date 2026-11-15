"""engine/report.py post: the report job, against a fake gh and a fake curl.

No test reaches GitHub or ntfy. The job runs in-process for most tests and as a real subprocess in one.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from urllib.parse import unquote

import fakecurl
import fakegh
from jobcase import ENGINE_SHA, ISSUE, REPO, TITLE, JobCase, issue_json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh-piv"))

from engine import job, report, route  # noqa: E402
from piv import do, init  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUN_URL = f"https://github.com/{REPO}/actions/runs/42"
PR_URL = f"https://github.com/{REPO}/pull/12"
ISSUE_API = f"repos/{REPO}/issues/{ISSUE}"
TOPIC = "pivotal-secret-topic-7"
BUILT = "claude:anthropic/sonnet-5@high"
REVIEWED = "codex:openai/gpt-5@high"
GOOD = {"status": "landed", "built_by": BUILT, "reviewed_by": REVIEWED, "pr": 12}


def labelled(*names: str, title: str = TITLE) -> str:
    return issue_json(title=title, labels=[{"name": n} for n in names])


class ReportBase(JobCase):
    def setUp(self):
        super().setUp()
        self.curl = fakecurl.FakeCurl(self.root / "curlbin")
        self.summary = self.root / "summary.md"
        self.gh.route(f"--paginate {ISSUE_API}/comments", fakegh.ok(""))
        self.gh.route(f"{ISSUE_API}/comments", fakegh.ok("{}"))
        self.gh.route(f"-X DELETE {ISSUE_API}/labels/", fakegh.ok("[]"))
        self.gh.route(f"-X POST {ISSUE_API}/labels", fakegh.ok("[]"))
        self.gh.route(f"{ISSUE_API}/labels", fakegh.ok(self.status_names("pivotal:building")))
        self.gh.route(f"repos/{REPO}/labels/pivotal%3A", fakegh.ok("{}"))
        self.gh.route(ISSUE_API, fakegh.ok(labelled("pivotal", "pivotal:building")), exact=False)

    def env(self, **over):
        base = {
            "PATH": f"{self.curl.dir}{os.pathsep}{self.gh.dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "RUN_ATTEMPT": "1",
            "QUIET": "false",
            "ACK_RESULT": "success",
            "PLAN_RESULT": "success",
            "RESULT": json.dumps(GOOD),
            "PIVOTAL_NTFY": TOPIC,
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }
        base.update(over)
        return super().env(**base)

    def run_report(self, **over) -> int:
        with redirect_stderr(io.StringIO()) as err:
            code = report.main(["post"], self.env(**over))
        self.stderr = err.getvalue()
        return code

    def posted(self) -> list[str]:
        return [a.split("=", 1)[1] for c in self.gh.calls() for a in c["args"] if a.startswith("body=")]

    @staticmethod
    def status_names(*names: str) -> str:
        return "".join(f"{n}\n" for n in names)

    def label_writes(self) -> list[tuple[str, str]]:
        """(method, label) of each label write, in order: DELETE of a status label, POST of the outcome label."""
        found = []
        for c in self.gh.calls():
            args = c["args"]
            if "-X" not in args:
                continue
            method, path = args[args.index("-X") + 1], next(a for a in args if a.startswith("repos/"))
            if method == "DELETE":
                found.append((method, unquote(path.rsplit("/", 1)[1])))
            elif method == "POST" and path.endswith("/labels"):
                found.extend((method, a.split("=", 1)[1]) for a in args if a.startswith("labels[]="))
        return found

    def messages(self) -> list[str]:
        return [c["sent"] for c in self.curl.calls()]


class MessageFormatTest(ReportBase):
    def test_landed_is_the_section_8_6_line(self):
        self.assertEqual(self.run_report(), 0)
        self.assertEqual(
            self.messages(), [f"{REPO} · {TITLE_CLEAN} · landed · built by {BUILT}, reviewed by {REVIEWED} · {PR_URL}"]
        )

    def test_every_outcome_phrase(self):
        cases = [
            ({"status": "ready", "pr": 12}, "ready for you", PR_URL),
            ({"status": "draft", "why": "gate_red", "pr": 12}, "draft (gate_red)", PR_URL),
            ({"status": "failed", "why": "agent_error", "pr": 0}, "failed (agent_error)", RUN_URL),
        ]
        for extra, phrase, link in cases:
            with self.subTest(phrase=phrase):
                self.curl.log.unlink(missing_ok=True)
                self.run_report(RESULT=json.dumps({**GOOD, **extra}))
                self.assertEqual(
                    self.messages(),
                    [f"{REPO} · {TITLE_CLEAN} · {phrase} · built by {BUILT}, reviewed by {REVIEWED} · {link}"],
                )

    def test_fell_back_and_cost_are_in_the_message(self):
        fell = [{"from": "claude:anthropic/opus-5", "to": BUILT, "reason": "rate-limited"}]
        self.run_report(RESULT=json.dumps({**GOOD, "fell_back": fell, "cost": "$1.25"}))
        self.assertEqual(
            self.messages(),
            [
                f"{REPO} · {TITLE_CLEAN} · landed · built by {BUILT}, reviewed by {REVIEWED}, "
                f"fell back from claude:anthropic/opus-5 (rate-limited), cost $1.25 · {PR_URL}"
            ],
        )
        self.assertIn("fell_back: claude:anthropic/opus-5 → claude:anthropic/sonnet-5@high (rate-limited)", self.posted()[0])

    def test_the_message_is_one_line_and_has_the_run_or_pr_link_only(self):
        self.run_report(RESULT=json.dumps({**GOOD, "pr": 12}))
        (message,) = self.messages()
        self.assertNotIn("\n", message)
        self.assertTrue(message.endswith(f" · {PR_URL}"))

    def test_the_message_carries_no_issue_body_and_no_code(self):
        self.gh.route(
            ISSUE_API,
            fakegh.ok(issue_json(title="Add a thing", body="Line one SECRET-BODY-TEXT", labels=[])),
            first=True,
        )
        self.run_report()
        for text in (*self.messages(), *self.posted(), self.summary.read_text(encoding="utf-8")):
            self.assertNotIn("SECRET-BODY-TEXT", text)
            self.assertNotIn("Line two", text)

    def test_the_comment_is_not_a_run_started_comment_to_piv_do(self):
        self.run_report()
        self.assertIsNone(do.RUN_STARTED.search(self.posted()[0]))

    def test_the_comment_shape(self):
        self.run_report(RESULT=json.dumps({**GOOD, "status": "ready", "pr": 12}))
        self.assertEqual(
            self.posted(),
            [
                f"🏁 run #5 ready for you · built by {BUILT}, reviewed by {REVIEWED}\n"
                f"next: {report.NEXT['ready']}\npr: {PR_URL}\nrun: {RUN_URL}"
            ],
        )

    def test_a_long_fell_back_list_is_cut_in_the_message_but_listed_in_the_comment(self):
        fell = [{"from": f"claude:anthropic/m{i}", "to": BUILT, "reason": "endpoint down"} for i in range(6)]
        self.run_report(RESULT=json.dumps({**GOOD, "fell_back": fell}))
        self.assertIn("and 3 more fell back", self.messages()[0])
        self.assertEqual(self.posted()[0].count("fell_back: "), 6)

    def test_fall_through_items_the_result_could_not_carry_are_counted_in_the_message_and_the_comment(self):
        fell = [{"from": f"claude:anthropic/m{i}", "to": BUILT, "reason": "endpoint down"} for i in range(6)]
        self.run_report(RESULT=json.dumps({**GOOD, "fell_back": fell, "fell_back_more": 5}))
        self.assertIn("and 8 more fell back", self.messages()[0])  # 6 - 3 shown + 5 the result could not list
        self.assertEqual(self.posted()[0].count("fell_back: "), 6)
        self.assertIn("5 more fell back and are not listed", self.posted()[0])

    def test_every_why_has_a_next_step_and_the_next_step_is_in_the_comment(self):
        for why, step in report.WHY.items():
            with self.subTest(why=why):
                self.assertTrue(step and "\n" not in step)
        self.run_report(RESULT=json.dumps({**GOOD, "status": "draft", "why": "conflict"}))
        self.assertIn(f"next: {report.WHY['conflict']}", self.posted()[0])

    def test_an_already_green_draft_says_the_check_passes_and_the_failed_run_may_still_be_red(self):
        # I-B: already_green is keyed on the repo's check passing on the base, not on the CI job that failed. The job may be a
        # different one, and it may still be red, so the draft is read before the incident is closed. The comment says so.
        wording = ("the repo's check passes on the base; the CI run that failed may be a different job and may still be red, "
                   "so read this draft before closing the incident")
        self.assertEqual(report.WHY["already_green"], wording)
        self.run_report(RESULT=json.dumps({**GOOD, "status": "draft", "why": "already_green"}))
        self.assertIn(f"next: {wording}", self.posted()[0])
        self.assertNotIn("fixed before", self.posted()[0])


TITLE_CLEAN = job.clean_text(TITLE, "", report.TITLE_MAX)


class OutcomeTest(ReportBase):
    def test_a_missing_result_is_failed_no_result_and_red(self):
        self.assertEqual(self.run_report(RESULT=""), 1)
        self.assertIn("failed (no_result)", self.messages()[0])
        self.assertIn("failed (no_result)", self.posted()[0])

    def test_no_result_is_the_case_when_the_env_lacks_it(self):
        env = self.env()
        del env["RESULT"]
        with redirect_stderr(io.StringIO()):
            self.assertEqual(report.main(["post"], env), 1)
        self.assertIn("failed (no_result)", self.messages()[0])

    def test_a_malformed_or_oversized_result_is_failed_bad_result(self):
        bad = [
            "not json",
            "[]",
            "null",
            '{"status": "landed"',
            json.dumps({**GOOD, "status": "success"}),
            json.dumps({**GOOD, "status": "LANDED"}),
            json.dumps({**GOOD, "status": 1}),
            json.dumps({**GOOD, "extra": 1}),
            json.dumps({**GOOD, "built_by": "x\nfoo"}),
            json.dumps({**GOOD, "built_by": "@someone"}),
            json.dumps({**GOOD, "reviewed_by": ["a"]}),
            json.dumps({**GOOD, "status": "draft"}),
            json.dumps({**GOOD, "status": "draft", "why": "because I said so"}),
            json.dumps({**GOOD, "status": "failed", "why": "see [x](http://evil)"}),
            json.dumps({**GOOD, "why": "gate_red"}),
            json.dumps({**GOOD, "cost": "free"}),
            json.dumps({**GOOD, "pr": "12"}),
            json.dumps({**GOOD, "pr": True}),
            json.dumps({**GOOD, "pr": -1}),
            json.dumps({**GOOD, "fell_back": "x"}),
            json.dumps({**GOOD, "fell_back": [{"from": BUILT, "to": BUILT}]}),
            json.dumps({**GOOD, "fell_back": [{"from": BUILT, "to": BUILT, "reason": "bored"}]}),
            json.dumps({**GOOD, "fell_back": [{"from": "none", "to": BUILT, "reason": "rate-limited"}]}),
            json.dumps({**GOOD, "fell_back": [{"from": BUILT, "to": BUILT, "reason": "rate-limited"}] * (report.FELL_BACK_MAX + 1)}),
            json.dumps({**GOOD, "title": "x" * (report.RESULT_MAX + 1)}),
        ]
        for text in bad:
            with self.subTest(result=text[:60]):
                outcome = report.parse_result(text)
                self.assertEqual((outcome.status, outcome.why), ("failed", "bad_result"))
        code = self.run_report(RESULT=bad[0])
        self.assertEqual(code, 1)
        self.assertIn("failed (bad_result)", self.messages()[0])

    def test_a_failed_result_is_red_and_a_draft_is_not(self):
        self.assertEqual(self.run_report(RESULT=json.dumps({**GOOD, "status": "failed", "why": "no_route"})), 1)
        self.assertEqual(self.run_report(RESULT=json.dumps({**GOOD, "status": "draft", "why": "gate_red"})), 0)
        self.assertEqual(self.run_report(RESULT=json.dumps({**GOOD, "status": "ready"})), 0)

    def test_an_upstream_job_that_did_not_succeed_fails_the_run_whatever_the_result_says(self):
        for name, value, why in [
            ("ACK_RESULT", "failure", "ack_failed"),
            ("ACK_RESULT", "skipped", "ack_failed"),
            ("ACK_RESULT", "", "ack_failed"),
            ("ACK_RESULT", "maybe", "ack_failed"),
            ("PLAN_RESULT", "failure", "plan_failed"),
            ("PLAN_RESULT", "skipped", "plan_failed"),
            ("ACK_RESULT", "cancelled", "cancelled"),
            ("PLAN_RESULT", "cancelled", "cancelled"),
        ]:
            with self.subTest(name=name, value=value):
                self.curl.log.unlink(missing_ok=True)
                self.assertEqual(self.run_report(**{name: value}), 1)
                self.assertIn(f"failed ({why})", self.messages()[0])
                self.assertIn("built by none, reviewed by none", self.messages()[0])

    def test_unknown_statuses_are_failures_in_the_table_itself(self):
        ok = {"ack": "success", "plan": "success"}
        self.assertEqual(report.decide(ok, json.dumps(GOOD)).status, "landed")
        self.assertEqual(report.decide(ok, '{"status": "merged"}').why, "bad_result")

    def test_every_status_label_comes_from_piv_init(self):
        defined = {name for name, _c, _t in init.LABELS}
        self.assertLessEqual(set(report.STATUS_LABELS) | set(report.LABEL.values()), defined)
        self.assertEqual(set(report.STATUS_LABELS), set(job.label_name(n) for n in report.STATUS_LABELS))


class QuietAndRefusalTest(ReportBase):
    def test_a_run_ack_called_quiet_sends_nothing_visible(self):
        for kind in ["go", "event", "incident"]:
            with self.subTest(kind=kind):
                self.assertEqual(self.run_report(QUIET="true", KIND=kind, TRUSTED="true", PLAN_RESULT="skipped"), 0)
        self.assertEqual(self.gh.calls(), [])
        self.assertEqual(self.curl.calls(), [])
        self.assertFalse(self.summary.exists())

    def test_the_level_is_not_read_again_ack_decides_quiet(self):
        # A level that changed after ack started the run must not leave the issue at building.
        self.assertEqual(self.run_report(LEVEL="watch"), 0)
        self.assertEqual(len(self.messages()), 1)
        self.assertIn(("POST", "pivotal:landed"), self.label_writes())

    def test_a_quiet_value_that_is_not_true_or_false_refuses_and_writes_nothing(self):
        self.assertEqual(self.run_report(QUIET="yolo"), 1)
        self.assertEqual(self.gh.calls(), [])
        self.assertEqual(self.curl.calls(), [])

    def test_bad_wiring_refuses_before_any_write(self):
        for over in [{"GH_REPO": "no-slash"}, {"KIND": "land"}, {"TRUSTED": "maybe"}, {"ENGINE_SHA": "0" * 40},
                     {"RUN_ID": "x"}, {"RUN_NUMBER": ""}, {"RUN_ATTEMPT": "0"}]:
            with self.subTest(over=over):
                self.assertEqual(self.run_report(**over), 1)
        self.assertEqual(self.gh.calls(), [])
        self.assertEqual(self.curl.calls(), [])

    def test_a_run_whose_ack_failed_before_an_incident_issue_still_gets_a_message_but_no_comment(self):
        self.write_event({})
        code = self.run_report(KIND="incident", ISSUE="", ACK_RESULT="failure")
        self.assertEqual(code, 1)
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.label_writes(), [])
        self.assertEqual(len(self.messages()), 1)
        self.assertIn(" · no issue · failed (ack_failed) · ", self.messages()[0])

    def test_a_refused_go_gets_a_content_free_failure_on_its_issue(self):
        self.assertEqual(self.run_report(ACK_RESULT="failure", RESULT=""), 1)
        self.assertIn("failed (ack_failed)", self.posted()[0])
        self.assertEqual(self.label_writes(), [("DELETE", "pivotal:building"), ("POST", "pivotal:blocked")])

    def test_a_run_that_never_got_going_never_reads_or_prints_the_issue_title(self):
        # I2: an issue edited after trust decided makes ack refuse; the outcome must not echo the edited title.
        self.gh.route(ISSUE_API, fakegh.ok(labelled("pivotal", title="EVIL-EDITED-TITLE")), first=True)
        cases = [
            {"ACK_RESULT": "failure"},
            {"ACK_RESULT": "skipped"},
            {"PLAN_RESULT": "failure"},
            {"PLAN_RESULT": "skipped"},
            {"ACK_RESULT": "cancelled"},
        ]
        for over in cases:
            with self.subTest(over=over):
                self.gh.log.unlink(missing_ok=True)
                self.curl.log.unlink(missing_ok=True)
                self.assertEqual(self.run_report(**over), 1)
                self.assertEqual(self.messages()[0].split(" · ")[1], f"issue #{ISSUE}")
                for text in (*self.messages(), *self.posted(), self.stderr, self.summary.read_text(encoding="utf-8")):
                    self.assertNotIn("EVIL", text)
                self.assertFalse(
                    [c for c in self.gh.calls() if c["args"][:2] == ["api", ISSUE_API]],
                    "the issue itself is not read",
                )
                self.assertTrue(self.label_writes())

    def test_the_title_is_read_only_when_every_job_succeeded(self):
        self.run_report()
        self.assertTrue([c for c in self.gh.calls() if c["args"][:2] == ["api", ISSUE_API]])
        self.assertIn(f" · {TITLE_CLEAN} · ", self.messages()[0])


class HostileTitleTest(ReportBase):
    def titled(self, title: str) -> None:
        self.gh.route(ISSUE_API, fakegh.ok(labelled("pivotal", title=title)), first=True)

    def test_a_hostile_title_cannot_forge_a_line_mention_link_or_field(self):
        hostile = (
            "Fix it\n" + REPO + " · other · landed · built by x, reviewed by y · https://evil.example\r\n"
            "@everyone @bob [click](https://evil.example/x) <https://evil.example> www.evil.example "
            "‮⁦ \x1b[31m\x00 ![i](http://x) `code` | a · b"
        )
        self.titled(hostile)
        self.run_report()
        (message,) = self.messages()
        for text in (message, self.posted()[0], self.summary.read_text(encoding="utf-8")):
            self.assertNotIn("@everyone", text)
            self.assertNotIn("@bob", text)
            self.assertNotIn("evil.example/", text)
            self.assertNotIn("://", text.replace(RUN_URL, "").replace(PR_URL, ""))
            self.assertNotIn("www.", text)
            self.assertNotIn("‮", text)
            self.assertNotIn("\x1b", text)
            self.assertNotIn("](", text)
        self.assertNotIn("\n", message)
        self.assertEqual(message.count(" · "), 4, message)
        self.assertTrue(message.endswith(f" · {PR_URL}"))

    def test_a_long_title_is_bounded(self):
        self.titled("x" * 5000)
        self.run_report()
        self.assertLessEqual(len(self.messages()[0]), 400)

    def test_an_empty_or_non_string_title_falls_back_to_the_issue_number(self):
        for title in ["", " \n ", "@@@", None]:
            with self.subTest(title=title):
                self.curl.log.unlink(missing_ok=True)
                self.titled(title)
                self.run_report()
                self.assertIn(f" · issue #{ISSUE} · landed", self.messages()[0])

    def test_clean_text_helper(self):
        self.assertEqual(job.clean_text("a​b‮c", "f"), "a b c")
        self.assertEqual(job.clean_text("x" * 100, "f", 10), "x" * 10)
        self.assertEqual(job.clean_text(5, "f"), "f")
        self.assertEqual(job.clean_text("www.x.y", "f"), "www x.y")
        self.assertEqual(job.clean_text("a b c\td", "f"), "a b c d")

    def test_nothing_in_the_job_output_carries_the_topic(self):
        self.run_report()
        self.assertNotIn(TOPIC, self.stderr)
        self.assertNotIn(TOPIC, self.summary.read_text(encoding="utf-8"))
        for text in self.posted():
            self.assertNotIn(TOPIC, text)


class LabelTest(ReportBase):
    def test_labels_are_set_exactly_building_cleared_one_outcome_label(self):
        self.gh.route(
            f"{ISSUE_API}/labels", fakegh.ok(self.status_names("bug", "pivotal:building", "pivotal:ready")), first=True
        )
        self.run_report()
        self.assertEqual(
            self.label_writes(), [("DELETE", "pivotal:building"), ("DELETE", "pivotal:ready"), ("POST", "pivotal:landed")]
        )

    def test_labels_are_never_replaced_as_a_whole_set(self):
        self.run_report()
        for call in self.gh.calls():
            self.assertNotIn("PUT", call["args"])
            self.assertFalse([a for a in call["args"] if a.startswith("labels[]=") and "pivotal:landed" not in a])

    def test_each_outcome_maps_to_its_label(self):
        for extra, label in [
            ({"status": "ready"}, "pivotal:ready"),
            ({"status": "draft", "why": "gate_red"}, "pivotal:blocked"),
            ({"status": "failed", "why": "no_route"}, "pivotal:blocked"),
        ]:
            with self.subTest(label=label, extra=extra):
                self.run_report(RESULT=json.dumps({**GOOD, **extra}))
                self.assertEqual(self.label_writes()[-1], ("POST", label))
                self.assertNotIn(("DELETE", label), self.label_writes())

    def test_a_missing_label_is_reported_and_red_but_the_comment_and_message_still_go(self):
        self.gh.route(f"repos/{REPO}/labels/pivotal%3Alanded", fakegh.fail("gh: Not Found (HTTP 404)\n"), first=True)
        self.assertEqual(self.run_report(), 1)
        self.assertEqual(len(self.posted()), 1)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.label_writes(), [])
        self.assertIn("problem: the labels were not set", self.stderr)
        self.assertIn("problem:", self.summary.read_text(encoding="utf-8"))

    def test_the_labels_are_written_after_the_comment(self):
        self.run_report()
        methods = [c["args"] for c in self.gh.calls() if "-X" in c["args"]]
        self.assertEqual([m[m.index("-X") + 1] for m in methods], ["POST", "DELETE", "POST"])


class FailureTest(ReportBase):
    def test_a_comment_failure_is_red_and_still_labels_and_sends(self):
        self.gh.route("-X POST", fakegh.fail("boom\n"), first=True)
        self.assertEqual(self.run_report(), 1)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.label_writes()[-1], ("POST", "pivotal:landed"))
        self.assertIn("problem: the comment was not posted", self.stderr)

    def test_an_unreadable_issue_still_comments_and_sends_but_is_red(self):
        self.gh.route(ISSUE_API, fakegh.fail("boom\n"), first=True, exact=False)
        self.assertEqual(self.run_report(), 1)
        self.assertEqual(len(self.posted()), 1)
        self.assertIn(f" · issue #{ISSUE} · landed", self.messages()[0])
        self.assertEqual(self.label_writes(), [])

    def test_a_failed_ntfy_send_is_reported_red_and_never_hides_the_comment(self):
        self.curl = fakecurl.FakeCurl(self.root / "curlbin", code=22, stderr=f"curl: {TOPIC}\n")
        self.assertEqual(self.run_report(), 1)
        self.assertEqual(len(self.posted()), 1)
        self.assertIn("problem: ntfy: curl failed with exit 22", self.stderr)
        self.assertNotIn(TOPIC, self.stderr)
        self.assertIn("ntfy: curl failed", self.summary.read_text(encoding="utf-8"))

    def test_curl_that_cannot_run_is_reported(self):
        env = self.env(PATH=str(self.gh.dir))
        with redirect_stderr(io.StringIO()) as err:
            code = report.main(["post"], env)
        self.assertEqual(code, 1)
        self.assertIn("ntfy: curl could not run", err.getvalue())
        self.assertEqual(len(self.posted()), 1)

    def test_an_unset_topic_is_a_neutral_skip_and_green(self):
        self.assertEqual(self.run_report(PIVOTAL_NTFY=""), 0)
        self.assertEqual(self.curl.calls(), [])
        self.assertIn("PIVOTAL_NTFY is not set", self.stderr)
        self.assertEqual(len(self.posted()), 1)

    def test_an_invalid_topic_is_a_problem_and_never_printed(self):
        for topic in ["a b", "x/../y", "t\nopic", "a" * 65, 'q"; url="http://evil'.replace(" ", "")]:
            with self.subTest(topic=topic):
                self.assertEqual(self.run_report(PIVOTAL_NTFY=topic), 1)
                self.assertIn("not a valid topic", self.stderr)
                self.assertNotIn(topic, self.stderr)
        self.assertEqual(self.curl.calls(), [])

    def test_an_unwritable_summary_is_reported(self):
        self.assertEqual(self.run_report(GITHUB_STEP_SUMMARY=str(self.root / "no" / "dir" / "s.md")), 1)
        self.assertIn("step summary could not be written", self.stderr)

    def test_a_missing_summary_path_is_skipped(self):
        self.assertEqual(self.run_report(GITHUB_STEP_SUMMARY=""), 0)

    def test_an_eleven_digit_run_id_is_the_run_link_in_the_summary(self):
        # A real run id has 11 digits (measured live: 42942520152); the report reads it with RUN_ID_DIGITS.
        self.assertEqual(self.run_report(RUN_ID="42942520152", RESULT=json.dumps({**GOOD, "status": "draft", "why": "gate_red"})),
                         0, self.stderr)
        self.assertIn(f"https://github.com/{REPO}/actions/runs/42942520152", self.summary.read_text(encoding="utf-8"))

    def test_the_summary_has_the_outcome_and_the_next_step(self):
        self.run_report(RESULT=json.dumps({**GOOD, "status": "draft", "why": "gate_red"}))
        text = self.summary.read_text(encoding="utf-8")
        self.assertIn("## draft (gate_red)", text)
        self.assertIn(report.WHY["gate_red"], text)
        self.assertIn(RUN_URL, text)


class NtfyTransportTest(ReportBase):
    def test_the_topic_and_message_are_not_in_argv(self):
        self.run_report()
        (call,) = self.curl.calls()
        joined = " ".join(call["args"])
        self.assertNotIn(TOPIC, joined)
        self.assertNotIn(REPO, joined)
        self.assertEqual(call["stdin"], f'url = "https://ntfy.sh/{TOPIC}"\n')
        self.assertIn("--data-binary", call["args"])
        self.assertIn("=https", call["args"])
        self.assertTrue(call["sent"].startswith(f"{REPO} · "))

    def test_curl_does_not_get_the_github_token_or_the_topic_in_its_environment(self):
        env = {"GH_TOKEN": "t", "PIVOTAL_NTFY": TOPIC, "PATH": os.environ["PATH"]}
        seen = {}
        orig = subprocess.run

        def spy(args, **kw):
            seen.update(kw["env"])
            return orig(args, **kw)

        report.subprocess.run = spy
        try:
            self.assertIsNone(report.notify({**env, "PATH": f"{self.curl.dir}{os.pathsep}{env['PATH']}"}, "m"))
        finally:
            report.subprocess.run = orig
        self.assertNotIn("GH_TOKEN", seen)
        self.assertNotIn("PIVOTAL_NTFY", seen)


class RerunTest(ReportBase):
    def existing(self, body: str, login: str = "github-actions[bot]") -> None:
        self.gh.route(
            f"--paginate {ISSUE_API}/comments",
            fakegh.ok(json.dumps({"login": login, "body": body}) + "\n"),
            first=True,
        )

    def first_body(self) -> str:
        self.run_report()
        body = self.posted()[0]
        self.gh.calls()
        return body

    def test_a_rerun_with_the_same_outcome_posts_no_second_comment_but_still_sends_the_message(self):
        body = self.first_body()
        self.existing(body)
        before = len(self.posted()), len(self.messages())
        self.assertEqual(self.run_report(RUN_ATTEMPT="2"), 0)
        self.assertEqual((len(self.posted()), len(self.messages())), (before[0], before[1] + 1))
        self.assertIn("not posting it twice", self.stderr)

    def test_a_rerun_retries_a_push_that_failed_the_first_time(self):
        body = self.first_body()
        self.existing(body)
        self.curl = fakecurl.FakeCurl(self.root / "curlbin", code=22)
        self.curl.log.unlink()
        self.assertEqual(self.run_report(RUN_ATTEMPT="2"), 1)
        self.assertEqual(len(self.curl.calls()), 1)
        self.assertEqual(len(self.label_writes()), 4, "labels are set again, exactly")

    def test_a_rerun_with_a_different_outcome_posts_again(self):
        body = self.first_body()
        self.existing(body)
        self.run_report(RUN_ATTEMPT="2", RESULT=json.dumps({**GOOD, "status": "ready"}))
        self.assertEqual(len(self.posted()), 2)
        self.assertEqual(len(self.messages()), 2)

    def test_a_comment_by_someone_else_is_not_a_duplicate(self):
        body = self.first_body()
        self.existing(body, login="someone")
        self.run_report(RUN_ATTEMPT="2")
        self.assertEqual(len(self.posted()), 2)

    def test_the_first_attempt_never_reads_the_comments(self):
        self.run_report()
        self.assertFalse([c for c in self.gh.calls() if any(a.endswith("/comments?per_page=100") for a in c["args"])])

    def test_an_unreadable_comment_list_is_a_problem_and_the_message_still_goes(self):
        self.gh.route(f"--paginate {ISSUE_API}/comments", fakegh.ok("not json\n"), first=True)
        self.assertEqual(self.run_report(RUN_ATTEMPT="2"), 1)
        self.assertEqual(len(self.messages()), 1)


SPECS = [
    "claude:anthropic/sonnet-5@high",
    "codex:openai/gpt-5@high",
    "agy:google/gemini-3.8-flash",
    "pi:google/gemini-3.8-flash@high",
    "pi:groq/openai/gpt-oss-120b",
    "pi:openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "pi:openrouter/qwen/qwen3-coder:free@high",
    "omp:kaggle/qwen3.6-35b-a3b@high",
    "pi:kaggle/qwen3.6-35b-a3b",
]


def spec_sources() -> list[Path]:
    """Every file that writes a route spec down: the docs, the router's tests and the presets."""
    return [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md")),
            ROOT / "test" / "test_route.py", *sorted((ROOT / "presets").glob("*.json"))]


def corpus_specs() -> list[str]:
    """Every route spec the repo writes down: the docs' examples, the router's tests and the presets."""
    found = set(SPECS)
    texts = [p.read_text(encoding="utf-8") for p in spec_sources()]
    for text in texts:
        for match in route.SPEC.finditer(text.replace("`", " ").replace('"', " ")):
            try:
                route.parse_entry(match[0])
            except ValueError:
                continue
            if re.search(r"[*<>]", match[0]):  # a placeholder in prose, such as pi:kaggle/*
                continue
            found.add(match[0])
    return sorted(found)


def placeholders() -> list[str]:
    """Specs route's loose grammar accepts in prose that are placeholders (``pi:kaggle/*``), not real models."""
    found = set()
    for path in spec_sources():
        text = path.read_text(encoding="utf-8").replace("`", " ")
        found |= {m[0] for m in route.SPEC.finditer(text) if re.search(r"[*<>]", m[0])}
    return sorted(found)


class SpecGrammarTest(ReportBase):
    """I1: report accepts the specs route accepts, so a good run is never reported red for its model name."""

    def test_the_corpus_is_not_empty_and_has_slashed_models(self):
        corpus = corpus_specs()
        self.assertGreaterEqual(len(corpus), len(SPECS))
        self.assertTrue([s for s in corpus if s.count("/") > 1])

    def test_placeholders_are_the_only_route_specs_the_corpus_leaves_out(self):
        for spec in placeholders():
            with self.subTest(spec=spec):
                self.assertEqual(report.parse_result(json.dumps({**GOOD, "built_by": spec})).why, "bad_result")

    def test_every_spec_in_the_repo_passes_as_built_by_reviewed_by_and_fell_back(self):
        for spec in corpus_specs():
            with self.subTest(spec=spec):
                result = {
                    **GOOD,
                    "built_by": spec,
                    "reviewed_by": spec,
                    "fell_back": [{"from": spec, "to": spec, "reason": "rate-limited"}],
                }
                o = report.parse_result(json.dumps(result))
                self.assertEqual((o.status, o.built_by, o.reviewed_by, o.fell_back[0][:2]), ("landed", spec, spec, (spec, spec)))
                self.assertEqual(route.parse_entry(spec).spec, spec)

    def test_a_spec_route_refuses_is_a_bad_result(self):
        for spec in ["claude:openai/gpt-5", "nowhere:x/y", "pi:groq", "pi:groq/", "pi:groq/a b", "pi:groq/a@b@c", "Claude:anthropic/x"]:
            with self.subTest(spec=spec):
                self.assertEqual(report.parse_result(json.dumps({**GOOD, "built_by": spec})).why, "bad_result")

    def test_a_spec_with_a_character_that_could_forge_text_is_refused(self):
        for spec in [
            "pi:groq/a\u202eb", "pi:groq/a\u200bb", "pi:groq/[x](http://e)", "pi:groq/a|b", "pi:groq/a·b",
            "pi:groq/a\x1bb", "pi:groq/a\nb", "pi:groq/@evil", "pi:groq/" + "a" * 200, "pi:groq/a<b>", "pi:groq/a`b",
        ]:
            with self.subTest(spec=spec):
                self.assertEqual(report.parse_result(json.dumps({**GOOD, "reviewed_by": spec})).why, "bad_result")
                fell = [{"from": spec, "to": BUILT, "reason": "rate-limited"}]
                self.assertEqual(report.parse_result(json.dumps({**GOOD, "fell_back": fell})).why, "bad_result")

    def test_the_spec_is_re_emitted_from_the_parsed_entry(self):
        self.assertEqual(route.clean_spec("pi:groq/openai/gpt-oss-120b@high"), route.parse_entry("pi:groq/openai/gpt-oss-120b@high").spec)
        with self.assertRaises(ValueError):
            route.clean_spec(5)


class ThinResultTest(ReportBase):
    """I3: landed and ready need a builder, a reviewer and something to point at."""

    def why(self, **result) -> tuple[str, str]:
        o = report.parse_result(json.dumps(result))
        return o.status, o.why

    def test_a_bare_status_is_bad_result(self):
        for status in ("landed", "ready"):
            self.assertEqual(self.why(status=status), ("failed", "bad_result"))
            self.assertEqual(self.run_report(RESULT=json.dumps({"status": status})), 1)
        self.assertIn("failed (bad_result)", self.messages()[0])

    def test_landed_and_ready_need_non_none_agents(self):
        for status in ("landed", "ready"):
            for key in ("built_by", "reviewed_by"):
                for value in ("none", None):
                    with self.subTest(status=status, key=key, value=value):
                        result = {**GOOD, "status": status, key: value}
                        if value is None:
                            del result[key]
                        self.assertEqual(self.why(**result), ("failed", "bad_result"))

    def test_ready_needs_a_pr(self):
        self.assertEqual(self.why(**{**GOOD, "status": "ready", "pr": 0}), ("failed", "bad_result"))
        self.assertEqual(self.why(**{k: v for k, v in {**GOOD, "status": "ready"}.items() if k != "pr"}), ("failed", "bad_result"))

    def test_landed_needs_a_pr_or_a_landed_sha(self):
        bare = {k: v for k, v in GOOD.items() if k != "pr"}
        self.assertEqual(self.why(**bare), ("failed", "bad_result"))
        self.assertEqual(self.why(**bare, sha="a" * 40), ("landed", ""))
        self.assertEqual(self.why(**bare, sha="a" * 39), ("failed", "bad_result"))
        self.assertEqual(self.why(**bare, sha="A" * 40), ("failed", "bad_result"))

    def test_a_landed_sha_is_linked_to_the_commit(self):
        bare = {k: v for k, v in GOOD.items() if k != "pr"}
        self.run_report(RESULT=json.dumps({**bare, "sha": "a" * 40}))
        self.assertTrue(self.messages()[0].endswith(f" · https://github.com/{REPO}/commit/{'a' * 40}"))

    def test_draft_and_failed_keep_working_with_no_agents(self):
        self.assertEqual(self.why(status="draft", why="gate_red"), ("draft", "gate_red"))
        self.assertEqual(self.why(status="failed", why="no_route"), ("failed", "no_route"))


class StageResultTest(ReportBase):
    """I3: the later jobs' results. Every one of them is wired (build, review, assemble, gate, publish); a result that is not success fails the run."""

    def test_the_stages_are_documented_in_the_module(self):
        for stage in report.STAGES:
            self.assertIn(f"{stage.upper()}_RESULT", report.__doc__)

    def test_a_failed_plan_names_its_own_refusal_and_nothing_else(self):
        """The size refusal is the plan job's own word: it names itself, and the next step is the request's own (engine/plan.py
        ``plan_why``). Any other word, and none, is the generic plan_failed: a refusal the job did not name never invents one."""
        from engine import plan

        self.assertEqual(set(report.PLAN_WHY), {plan.REQUEST_TOO_LARGE})
        self.assertLessEqual(report.PLAN_WHY, set(report.WHY))
        upstream = {"ack": "success", "plan": "failure"}
        o = report.decide(upstream, "", plan_why=plan.REQUEST_TOO_LARGE)
        self.assertEqual((o.status, o.why), ("failed", "request_too_large"))
        self.assertIn("too long for one slice", o.next_step)
        for word in ("", "maybe", "plan_failed", "request_too_large\n"):
            with self.subTest(plan_why=word):
                o = report.decide(upstream, "", plan_why=word)
                self.assertEqual((o.status, o.why), ("failed", "plan_failed"))
        # a plan that did not fail keeps its result, whatever word its output carries
        o = report.decide({"ack": "success", "plan": "success"}, json.dumps(GOOD), plan_why=plan.REQUEST_TOO_LARGE)
        self.assertEqual((o.status, o.why), ("landed", ""))
        # and the word reaches the message the job posts
        self.assertEqual(self.run_report(PLAN_RESULT="failure", PLAN_WHY=plan.REQUEST_TOO_LARGE), 1)
        self.assertIn("failed (request_too_large)", self.messages()[0])
        self.assertIn("next: the issue's title or body is too long for one slice", self.posted()[0])

    def test_a_wired_stage_that_did_not_succeed_fails_with_its_own_reason(self):
        for stage in ("build", "review", "assemble", "gate"):
            for value in ("failure", "skipped", "", "maybe"):
                with self.subTest(stage=stage, value=value):
                    o = report.decide({"ack": "success", "plan": "success", stage: value}, json.dumps(GOOD))
                    self.assertEqual((o.status, o.why), ("failed", f"{stage}_failed"))

    def test_a_publish_job_that_failed_after_landing_is_reported_landed_with_the_failure_shown(self):
        # F-3: main has moved, so the issue is labelled landed, not blocked; the failed job stays visible and the report job red.
        self.run_report(PUBLISH_RESULT="failure", RESULT=json.dumps({"status": "landed", "built_by": BUILT, "reviewed_by": REVIEWED, "sha": "a" * 40}))
        self.assertIn(("POST", "pivotal:landed"), self.label_writes())
        self.assertNotIn(("POST", "pivotal:blocked"), self.label_writes())
        self.assertIn("the publish job failed after it moved the base", self.summary.read_text())
        self.assertIn("problem: the publish job failed", self.stderr)

    def test_a_publish_job_that_failed_without_landing_is_still_blocked(self):
        self.run_report(PUBLISH_RESULT="failure", RESULT=json.dumps({"status": "failed", "why": "seal_failed"}))
        self.assertIn(("POST", "pivotal:blocked"), self.label_writes())

    def test_a_cancelled_stage_is_cancelled(self):
        self.assertEqual(report.decide({"ack": "success", "plan": "success", "gate": "cancelled"}, json.dumps(GOOD)).why, "cancelled")

    def test_a_failed_build_keeps_the_reason_publish_found(self):
        """Integration (sandbox-t2): the build job is red for a failed slice by design, so the stage failure used to hide the
        reason publish put in RESULT, and a secret in the patch read as "the build job did not finish"."""
        for stage in ("build", "review", "assemble", "gate"):
            for why in ("secret_in_patch", "no_secret", "no_route", "workflow_change"):
                with self.subTest(stage=stage, why=why):
                    result = json.dumps({"status": "failed", "why": why, "built_by": "none"})
                    o = report.decide({"ack": "success", "plan": "success", stage: "failure"}, result)
                    self.assertEqual((o.status, o.why), ("failed", why))
                    self.assertTrue(o.red)

    def test_only_a_reason_publish_gives_a_failed_slice_replaces_the_stage_failure(self):
        """Round 1, F10: a failed result of another kind (a lone candidate, a generic failure) says less than ``<stage>_failed``."""
        from engine import publish

        # The two relations, each in both directions (round 3, R3-4: round 2 had edited this to a one-way subtraction).
        self.assertEqual(set(publish.WHY_OF.values()) | {"branch_stale"}, set(report.SLICE_FAILURE_WHY),
                         "a failed slice's stage failure is replaced by exactly publish's WHY_OF words and branch_stale")
        self.assertEqual(set(report.PUBLISH_WHY), set(publish.FAIL_WHY.values()) | set(publish.WHY_OF.values()) | {"build_failed"},
                         "a failed publish result keeps exactly publish's own words")
        self.assertLessEqual(report.SLICE_FAILURE_WHY, set(report.WHY))
        for stage in ("build", "assemble"):
            for why in ("candidate_only", "publish_failed", "build_failed", "no_result", "bad_result", "cancelled", "gate_failed"):
                with self.subTest(stage=stage, why=why):
                    o = report.decide({"ack": "success", "plan": "success", stage: "failure"},
                                      json.dumps({"status": "failed", "why": why, "built_by": "none"}))
                    self.assertEqual((o.status, o.why), ("failed", f"{stage}_failed"))

    def test_a_result_never_turns_a_failed_stage_green_or_hides_an_earlier_failure(self):
        for stage in ("build", "assemble"):
            for result in (json.dumps(GOOD), json.dumps({"status": "draft", "why": "no_stack", "built_by": "none", "pr": 3}),
                           json.dumps({"status": "failed", "why": "publish_failed"}), "", "{}"):
                with self.subTest(stage=stage, result=result):
                    o = report.decide({"ack": "success", "plan": "success", stage: "failure"}, result)
                    self.assertEqual((o.status, o.why), ("failed", f"{stage}_failed"))
        for upstream in ({"ack": "failure", "plan": "success", "build": "failure"},
                         {"ack": "success", "plan": "failure", "build": "failure"}):
            o = report.decide(upstream, json.dumps({"status": "failed", "why": "secret_in_patch"}))
            self.assertEqual(o.why, "ack_failed" if upstream["ack"] == "failure" else "plan_failed")
        o = report.decide({"ack": "success", "plan": "success", "build": "cancelled"}, json.dumps({"status": "failed", "why": "secret_in_patch"}))
        self.assertEqual(o.why, "cancelled")

    def test_a_failed_stage_keeps_the_fall_through_items_of_whatever_result_publish_wrote(self):
        # R5: the outage that caused a generic failure or a red review draft must reach the comment, beside the failure.
        item = {"from": "pi:kaggle/qwen3.6-35b-a3b", "to": BUILT, "reason": "endpoint down"}
        for stage, result in (
            ("build", {"status": "failed", "why": "build_failed"}),
            ("build", {"status": "failed", "why": "publish_failed"}),
            ("review", {"status": "draft", "why": "verdict_missing", "pr": 3}),
            ("build", {"status": "failed", "why": "gate_red"}),
            ("assemble", json.loads(json.dumps(GOOD))),
        ):
            with self.subTest(stage=stage, result=result):
                text = json.dumps({**result, "fell_back": [item], "fell_back_more": 2})
                o = report.decide({"ack": "success", "plan": "success", stage: "failure"}, text)
                self.assertEqual((o.status, o.why), ("failed", result.get("why") if result.get("why") == "gate_red" else f"{stage}_failed"))
                self.assertEqual((o.fell_back, o.fell_back_more), ((("pi:kaggle/qwen3.6-35b-a3b", BUILT, "endpoint down"),), 2))

    def test_a_failed_publish_job_keeps_the_fall_through_items_of_its_result(self):
        # R4 (round 3): publish_failed rendered with fell_back=() though the RESULT carried the items.
        item = {"from": "pi:kaggle/qwen3.6-35b-a3b", "to": BUILT, "reason": "endpoint down"}
        text = json.dumps({"status": "failed", "why": "publish_failed", "fell_back": [item], "fell_back_more": 3})
        o = report.decide({"ack": "success", "plan": "success", "publish": "failure"}, text)
        self.assertEqual((o.status, o.why), ("failed", "publish_failed"))
        self.assertEqual((o.fell_back, o.fell_back_more), ((("pi:kaggle/qwen3.6-35b-a3b", BUILT, "endpoint down"),), 3))

    def test_a_hostile_fell_back_more_makes_the_result_bad(self):
        # S2: bool, negative, over the cap, float, string, null and list are all refused.
        for hostile in (True, -1, report.FELL_BACK_MORE_MAX + 1, 1.5, "5", None, [1]):
            with self.subTest(more=hostile):
                o = report.parse_result(json.dumps({**GOOD, "fell_back_more": hostile}))
                self.assertEqual((o.status, o.why), ("failed", "bad_result"))

    def test_the_comment_of_a_generic_build_failure_lists_the_fall_through(self):
        item = {"from": "pi:kaggle/qwen3.6-35b-a3b", "to": BUILT, "reason": "endpoint down"}
        self.run_report(BUILD_RESULT="failure", RESULT=json.dumps({"status": "failed", "why": "build_failed", "fell_back": [item]}))
        self.assertIn("failed (build_failed)", self.posted()[0])
        self.assertIn("fell_back: pi:kaggle/qwen3.6-35b-a3b", self.posted()[0])

    def test_wired_stages_that_succeeded_leave_the_result_to_decide(self):
        results = {name: "success" for name in report.STAGES}
        self.assertEqual(report.decide(results, json.dumps(GOOD)).status, "landed")

    def test_a_skipped_gate_is_not_a_failure_only_when_assemble_stacked_nothing(self):
        base = {name: "success" for name in report.STAGES}
        skipped = {**base, "gate": "skipped"}
        self.assertEqual(report.decide(skipped, json.dumps(GOOD), "false").status, "landed")
        for has_stack in ("", "true", "TRUE", "false\n", "maybe"):
            with self.subTest(has_stack=has_stack):
                o = report.decide(skipped, json.dumps(GOOD), has_stack)
                self.assertEqual((o.status, o.why), ("failed", "gate_failed"))
        for assemble in ("failure", "skipped", "", "cancelled"):
            with self.subTest(assemble=assemble):
                o = report.decide({**skipped, "assemble": assemble}, json.dumps(GOOD), "false")
                self.assertEqual(o.status, "failed")
        # an assemble that is not wired says nothing about the stack: STAGES order alone does not excuse the gate
        unwired = {name: "success" for name in report.STAGES if name != "assemble"}
        o = report.decide({**unwired, "gate": "skipped"}, json.dumps(GOOD), "false")
        self.assertEqual((o.status, o.why), ("failed", "gate_failed"))
        # only the gate is excused, and only when skipped
        # R3-1: a red gate job stands over nothing but a RESULT that says publish moved the base or opened the PR
        nothing_moved = json.dumps({"status": "draft", "why": "no_stack", "built_by": "none", "reviewed_by": "none", "pr": 3})
        self.assertEqual(report.decide({**base, "gate": "failure"}, nothing_moved, "false").why, "gate_failed")
        self.assertEqual(report.decide({**base, "gate": "failure"}, json.dumps(GOOD), "false").status, "landed")
        self.assertEqual(report.decide({**base, "review": "skipped"}, json.dumps(GOOD), "false").why, "review_failed")

    def test_the_job_reads_has_stack_from_its_env_var(self):
        stages = dict(ACK_RESULT="success", PLAN_RESULT="success", BUILD_RESULT="success", REVIEW_RESULT="success",
                      ASSEMBLE_RESULT="success", GATE_RESULT="skipped")
        self.assertEqual(self.run_report(**stages, HAS_STACK="false"), 0)
        self.curl.log.unlink()
        self.assertEqual(self.run_report(**stages, HAS_STACK="true"), 1)
        self.assertIn("failed (gate_failed)", self.messages()[0])

    def test_the_job_reads_each_stage_from_its_env_var(self):
        self.assertEqual(self.run_report(GATE_RESULT="failure"), 1)
        self.assertIn("failed (gate_failed)", self.messages()[0])
        self.curl.log.unlink()
        self.assertEqual(self.run_report(GATE_RESULT="success", BUILD_RESULT="success"), 0)

    def test_every_stage_has_a_reason_with_a_next_step(self):
        for stage in report.STAGES:
            self.assertIn(f"{stage}_failed", report.WHY)


class KaggleLineTest(ReportBase):
    """F1: the models-up reason reaches the report as one fixed line, and never changes the outcome."""

    def line(self, **over) -> list[str]:
        self.run_report(**over)
        return [ln for ln in self.posted()[-1].splitlines() if ln.startswith("kaggle:")]

    def test_every_word_of_the_vocabulary_prints_its_fixed_text_in_comment_and_summary(self):
        from engine import modelsup
        for word, text in modelsup.WHY.items():
            with self.subTest(word=word):
                self.summary.unlink(missing_ok=True)
                found = self.line(MODELS_UP_RESULT="success", KAGGLE_WHY=word)
                self.assertEqual(len(found), 1)
                self.assertIn(f"({word}): {text}", found[0])
                self.assertIn(found[0], self.summary.read_text(encoding="utf-8"))

    def test_ok_is_printed_only_when_the_job_succeeded_and_the_word_is_a_success_word(self):
        from engine import modelsup
        for result in ("success", "failure", "cancelled"):
            for word in modelsup.WHY:
                with self.subTest(result=result, word=word):
                    found = self.line(MODELS_UP_RESULT=result, KAGGLE_WHY=word)[0]
                    good = result == "success" and word in modelsup.OK_WHY
                    self.assertEqual(found.startswith("kaggle: ok "), good, found)
                    self.assertEqual("[the models-up job ended" in found, result != "success", found)

    def test_a_success_line_says_the_keyless_check_is_off_by_default(self):
        found = self.line(MODELS_UP_RESULT="success", KAGGLE_WHY="live")[0]
        self.assertIn("keyless route check is off by default", found)
        self.assertNotIn("keyless", self.line(MODELS_UP_RESULT="success", KAGGLE_WHY="quota")[0])

    def test_no_line_when_models_up_was_skipped_or_not_wired(self):
        self.assertEqual(self.line(MODELS_UP_RESULT="skipped"), [])
        self.assertEqual(self.line(), [])

    def test_an_empty_or_unknown_word_is_said_plainly_and_never_echoed(self):
        for result in ("failure", "success", "cancelled"):
            for word in ("", "quota; echo hi", "x" * 500, "ok\nkaggle: fake line"):
                with self.subTest(result=result, word=word):
                    found = self.line(MODELS_UP_RESULT=result, KAGGLE_WHY=word)
                    self.assertEqual(len(found), 1)
                    self.assertIn("recorded no reason", found[0])
                    if word:
                        self.assertNotIn(word.split("\n")[0], self.posted()[-1])

    def test_the_line_never_makes_a_run_landed_or_green(self):
        code = self.run_report(MODELS_UP_RESULT="success", KAGGLE_WHY="started", PUBLISH_RESULT="failure", BUILD_RESULT="failure")
        self.assertEqual(code, 1)
        self.assertIn("failed (build_failed)", self.messages()[0])
        self.assertNotIn("landed", self.messages()[0])


class ResultOutputTest(unittest.TestCase):
    """S3: the producer's output obeys job.OUTPUT_RULES, sized for RESULT_MAX."""

    def test_result_is_an_output_with_a_size_cap(self):
        rule = job.OUTPUT_RULES["result"]
        self.assertIsNotNone(rule.fullmatch(json.dumps(GOOD)))
        self.assertIsNone(rule.fullmatch("a" * (job.RESULT_MAX + 1)))
        self.assertIsNone(rule.fullmatch('{"status":\n"landed"}'))
        self.assertIsNone(rule.fullmatch(json.dumps({"built_by": "é"}, ensure_ascii=False)))
        self.assertEqual(report.RESULT_MAX, job.RESULT_MAX)

    def test_write_outputs_refuses_an_oversized_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            job.write_outputs(str(out), {"result": json.dumps(GOOD)})
            with self.assertRaises(job.JobError):
                job.write_outputs(str(out), {"result": "x" * (job.RESULT_MAX + 1)})
            self.assertEqual(out.read_text(encoding="utf-8").count("result="), 1)

    def test_quiet_is_an_output(self):
        self.assertIsNotNone(job.OUTPUT_RULES["quiet"].fullmatch("true"))
        self.assertIsNone(job.OUTPUT_RULES["quiet"].fullmatch("yes"))


class FakeCurlTest(ReportBase):
    """S3: the fake curl is as strict about flags as the real one, and the job disables .curlrc."""

    def test_the_job_runs_curl_with_q_first(self):
        self.run_report()
        self.assertEqual(self.curl.calls()[0]["args"][0], "-q")

    def test_a_flag_curl_does_not_take_is_refused(self):
        for flags in (["--data-binar", "@x"], ["--nope"], ["-Z9"]):
            with self.subTest(flags=flags):
                done = subprocess.run([str(self.curl.dir / "curl"), "-q", *flags], capture_output=True, text=True, check=False)
                self.assertEqual(done.returncode, 2)
                self.assertIn("fake curl", done.stderr)

    def test_a_value_flag_without_its_value_is_refused(self):
        done = subprocess.run([str(self.curl.dir / "curl"), "-q", "--max-time"], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 2)


class SubprocessTest(ReportBase):
    def test_it_runs_as_a_script_under_python_dash_i(self):
        env = self.env(GITHUB_STEP_SUMMARY=str(self.summary), ENGINE_SHA=ENGINE_SHA)
        done = subprocess.run(
            [sys.executable, "-I", str(ROOT / "engine" / "report.py"), "post"],
            env=env, capture_output=True, text=True, check=False, cwd=self.root,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self.messages()), 1)
        self.assertNotIn(TOPIC, done.stdout + done.stderr)

    def test_the_summary_subcommand_still_exists(self):
        done = subprocess.run(
            [sys.executable, "-m", "engine.report", "summary", "--help"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(done.returncode, 0)


class RouteSeamTest(unittest.TestCase):
    def test_the_fell_back_reasons_are_the_routers(self):
        for reason in route.SKIP_REASONS:
            item = {"from": BUILT, "to": "no_route", "reason": reason}
            self.assertEqual(report.parse_result(json.dumps({**GOOD, "fell_back": [item]})).fell_back[0][2], reason)

    def test_a_router_fell_back_line_maps_onto_the_comment_line(self):
        fell = route.FellBack(BUILT, "no_route", "rate-limited")
        o = report.parse_result(json.dumps({**GOOD, "fell_back": [{"from": BUILT, "to": "no_route", "reason": "rate-limited"}]}))
        self.assertIn(fell.text, report.comment_body(1, o, RUN_URL, ""))

    def test_a_refused_kaggle_push_is_a_quota_fall_through_line_and_a_kaggle_line_that_says_so(self):
        # The run report says so: the skipped Kaggle entry is a `quota` line, and the models-up line
        # names the refused push as its reason. Neither changes the outcome.
        fell = route.FellBack("pi:kaggle/qwen3.6-35b-a3b", BUILT, "quota")
        o = report.parse_result(json.dumps({**GOOD, "fell_back": [{"from": fell.source, "to": BUILT, "reason": "quota"}]}))
        self.assertEqual(o.status, GOOD["status"])
        body = report.comment_body(1, o, RUN_URL, "", report.kaggle_line({"MODELS_UP_RESULT": "success", "KAGGLE_WHY": "quota"}))
        self.assertIn(fell.text, body)
        self.assertIn("kaggle: not ok (quota): Kaggle refused the push", body)


class FixNoteTest(ReportBase):
    """R19: the rerun note is shown, but a re-run that sent nothing is the normal case and is not a red report job."""

    DRAFT = {**GOOD, "status": "draft", "why": "review_rejected"}

    def test_a_rerun_note_is_shown_and_the_report_job_is_green(self):
        self.assertEqual(self.run_report(RESULT=json.dumps({**self.DRAFT, "fix": "rerun"})), 0, self.stderr)
        self.assertIn("note: " + report.FIX_RERUN_NOTE, self.stderr)
        self.assertNotIn("problem: " + report.FIX_RERUN_NOTE, self.stderr)
        self.assertIn(report.FIX_RERUN_NOTE, self.summary.read_text(encoding="utf-8"))

    def test_an_unknown_fix_round_is_still_a_red_problem(self):
        self.assertEqual(self.run_report(RESULT=json.dumps({**self.DRAFT, "fix": "unknown"})), 1, self.stderr)
        self.assertIn("problem: " + report.FIX_UNKNOWN_NOTE, self.stderr)


    NOT_STANDING = {"status": "draft", "why": "no_stack", "built_by": "none", "reviewed_by": "none", "pr": 3}

    def comment_of(self) -> str:
        (body,) = self.posted()
        return body

    def test_every_fix_word_reaches_the_issue_comment(self):
        # S2: the failed, unknown and rerun words are in the comment the operator reads, not only the step summary.
        for word, note in (("failed", report.FIX_FAILED_NOTE), ("unknown", report.FIX_UNKNOWN_NOTE), ("rerun", report.FIX_RERUN_NOTE)):
            with self.subTest(word=word):
                self.run_report(RESULT=json.dumps({**self.DRAFT, "fix": word}))
                self.assertIn(note, self.posted()[-1])

    def test_a_gate_failure_that_does_not_stand_still_says_the_fix_round_may_have_started(self):
        # R25: decide() returned a fresh Outcome here, so the flags were lost.
        self.assertEqual(self.run_report(GATE_RESULT="failure", PUBLISH_RESULT="success",
                                         RESULT=json.dumps({**self.NOT_STANDING, "fix": "unknown"})), 1, self.stderr)
        self.assertIn("gate_failed", self.comment_of())
        self.assertIn(report.FIX_UNKNOWN_NOTE, self.comment_of())

    def test_a_publish_failure_with_an_unlisted_why_still_says_the_fix_round_was_not_started(self):
        self.assertEqual(self.run_report(PUBLISH_RESULT="failure",
                                         RESULT=json.dumps({**self.NOT_STANDING, "fix": "failed"})), 1, self.stderr)
        self.assertIn("publish_failed", self.comment_of())
        self.assertIn(report.FIX_FAILED_NOTE, self.comment_of())

    def test_a_rerun_whose_fix_words_differ_from_the_first_runs_is_not_commented_twice(self):
        first = {**self.DRAFT, "fix": "unknown"}
        self.run_report(RESULT=json.dumps(first))
        (posted,) = self.posted()
        self.gh.route(f"--paginate {ISSUE_API}/comments", fakegh.ok(json.dumps({"login": report.BOT, "body": posted}) + "\n"), first=True)
        self.run_report(RESULT=json.dumps({**self.DRAFT, "fix": "rerun"}), RUN_ATTEMPT="2")
        self.assertEqual(len(self.posted()), 1, "the same outcome is not posted again because only the fix lines differ")

    def test_r28_the_failed_note_names_the_operators_next_step(self):
        self.assertIn("never sends it again", report.FIX_FAILED_NOTE)
        self.assertIn("/pivotal fix", report.FIX_FAILED_NOTE)

    def test_decide_carries_the_flags_over_every_outcome(self):
        base = {name: "success" for name in report.STAGES}
        result = json.dumps({**self.NOT_STANDING, "fix": "failed unknown rerun"})
        for upstream in ({**base, "gate": "failure"}, {**base, "publish": "failure"}, {**base, "build": "failure"}):
            with self.subTest(upstream=upstream):
                o = report.decide(upstream, result, "false")
                self.assertEqual((o.fix_failed, o.fix_unknown, o.fix_rerun), (True, True, True))
        self.assertEqual(report.decide({**base, "build": "cancelled"}, result, "false").fix_unknown, True)


if __name__ == "__main__":
    unittest.main()
