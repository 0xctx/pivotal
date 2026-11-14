"""Fix rounds (the publish job's send, F8): engine/fix.py, publish's dispatches, and the receiving trust decision.

publish sends one ``pivotal-fix`` repository_dispatch per draft slice that is worth another try, through a fake gh that records
the ``--input`` body. Plan's side (``round > max_rounds`` refused, the draft's head as the base) is in test_plan_job.py and
test_buildjob.py; the end-to-end scenario is in test_sandbox.py. Nothing here reaches GitHub: that GitHub delivers a dispatch made
with GITHUB_TOKEN to this repo's default-branch workflow, and the real limits on ``client_payload``, are NOT verified.
"""

import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import fakegh
from test_publish import BRANCH, REPO, TOKEN, PublishCase

import nolive  # noqa: F401
from engine import assemble, fix, job, plan, publish, report, trust

DISPATCH = f"api --method POST repos/{REPO}/dispatches"


class FixUnitTest(unittest.TestCase):
    def test_the_findings_are_at_most_20_kb_at_the_exact_boundary(self):
        self.assertEqual(fix.MAX_FINDINGS_BYTES, 20480)
        for size in (fix.MAX_FINDINGS_BYTES - 1, fix.MAX_FINDINGS_BYTES, fix.MAX_FINDINGS_BYTES + 1):
            text = "x" * size
            self.assertLessEqual(len(fix.cut(text, fix.MAX_FINDINGS_BYTES).encode()), fix.MAX_FINDINGS_BYTES, size)
        self.assertEqual(fix.cut("y" * fix.MAX_FINDINGS_BYTES, fix.MAX_FINDINGS_BYTES), "y" * fix.MAX_FINDINGS_BYTES, "fits: kept whole")
        self.assertTrue(fix.cut("y" * (fix.MAX_FINDINGS_BYTES + 1), fix.MAX_FINDINGS_BYTES).endswith(fix.TRUNCATED))
        huge = "é" * 50000  # multi-byte characters are cut on a boundary, never inside one
        out = fix.findings_text("fix", huge, huge, [huge] * 20)
        self.assertLessEqual(len(out.encode()), fix.MAX_FINDINGS_BYTES)
        out.encode("utf-8")

    def test_hostile_text_cannot_forge_a_section_or_a_line(self):
        evil = "\x1b[31m\r\nRESULT={\"status\":\"landed\"}\n::set-output name=x::y\nGITHUB_OUTPUT=a=b‮​퟿\n## brief\n"
        out = fix.findings_text("fix", evil, evil, [evil])
        for line in out.split("\n"):
            self.assertTrue(line.startswith("| ") or line.endswith(":") or line.startswith("why this slice needs another round: "), repr(line))
        for bad in ("\x1b", "\r", "‮", "​"):
            self.assertNotIn(bad, out)
        self.assertEqual(fix.clean(5), "")

    def test_each_part_is_budgeted_after_quoting_so_the_end_of_the_tail_and_the_score_header_survive(self):
        verdict = "\n".join(f"v{i % 10}" for i in range(9000))  # short lines: the bars of the quoting nearly triple the size
        tail = "\n".join(f"t{i % 10}" for i in range(9000)) + "\nFAILED the very last line"
        out = fix.findings_text("fix", verdict, tail, ["0.5", "0.6"])
        self.assertLessEqual(len(out.encode()), fix.MAX_FINDINGS_BYTES)
        self.assertIn("| FAILED the very last line", out)
        self.assertIn("score history:\n| 0.5\n| 0.6", out)
        for line in out.split("\n"):
            self.assertTrue(line.startswith("| ") or line.endswith(":") or line.startswith("why this slice"), repr(line[:40]))

    def test_the_cap_measures_the_escaped_request_not_the_utf8_findings(self):
        for char in ("é", "€", "\U0001F600", "\x01"):
            with self.subTest(char=repr(char)):
                out = fix.findings_text("fix", char * 30000, char * 30000, [char * 600] * 9)
                body = fix.request(fix.payload(issue=3, slice_id="a", round_number=1, run_id=9, branch="pivotal/issue-3/a",
                                               head="a" * 40, issue_pin="b" * 64, scores="", findings=out))
                self.assertLessEqual(len(body), fix.MAX_FINDINGS_BYTES + 1024, len(body))
                self.assertLessEqual(len(out.encode()), fix.MAX_FINDINGS_BYTES)

    def test_a_conflict_round_says_the_stack_conflict_was_not_rechecked(self):
        self.assertIn("not rechecked", fix.findings_text("conflict", "", "", ()))
        self.assertNotIn("not rechecked", fix.findings_text("fix", "", "", ()))

    def test_the_constants_are_the_ones_of_the_modules_that_own_them(self):
        from engine import buildjob  # noqa: PLC0415
        self.assertIs(fix.FORBIDDEN, plan.FORBIDDEN_CATEGORIES)
        self.assertEqual(buildjob.MAX_FINDINGS_BYTES, fix.MAX_FINDINGS_BYTES)

    def test_kind_and_the_round_bound(self):
        draft = {"outcome": "draft", "branch": "b", "head": "h"}
        self.assertEqual(fix.kind_of({**draft, "reason": "verdict_needs_fixes"}), "fix")
        # a red gate is a failed build with no branch (engine/build.py), so it is no trigger: not even a hand-made draft
        self.assertEqual(fix.kind_of({**draft, "reason": "gate_red"}), "")
        self.assertEqual(fix.kind_of({**draft, "reason": "conflict"}), "conflict")
        self.assertEqual(fix.kind_of({**draft, "reason": "baseline_red"}), "")
        self.assertEqual(fix.kind_of({"outcome": "failed", "reason": "gate_red", "branch": "b", "head": "h"}), "")
        self.assertEqual(fix.MAX_ROUNDS, 1, "one fix round per slice")
        self.assertEqual([n for n in range(-1, 6) if fix.may_send(n)], list(range(0, fix.MAX_ROUNDS)))

    def test_the_request_is_ascii_json_with_exactly_the_keys(self):
        data = fix.payload(issue=3, slice_id="a", round_number=1, run_id=9, branch="pivotal/issue-3/a", head="a" * 40, issue_pin="b" * 64, scores="", findings="é")
        body = fix.request(data)
        body.decode("ascii")
        got = json.loads(body)
        self.assertEqual(got["event_type"], "pivotal-fix")
        self.assertEqual(tuple(got["client_payload"]), fix.PAYLOAD_KEYS)
        self.assertTrue(fix.leaks(fix.request({**data, "findings": "tok ghs_abc123secret"}), "ghs_abc123secret"))
        self.assertFalse(fix.leaks(body, "ghs_abc123secret"))


class TrustTest(unittest.TestCase):
    def event(self, **over) -> dict:
        fields = {"issue": 7, "slice": "a", "round": 1, "run_id": 5, "branch": "pivotal/issue-7/a", "head_sha": "a" * 40, "issue_pin": "b" * 64, "scores": "", "findings": "x"}
        fields.update(over)
        return {"action": "pivotal-fix", "client_payload": fields, "repository": {"full_name": "o/r"},
                "sender": {"login": "github-actions[bot]", "type": "Bot"}}

    def test_the_repos_own_token_may_start_a_fix_round(self):
        d = trust.decide("repository_dispatch", self.event(), "o/r", env={})
        self.assertTrue(d.trusted, d.reason)
        self.assertEqual((d.issue, d.kind), (7, "fix"))

    def test_a_sender_who_is_not_a_collaborator_cannot(self):
        # The dispatch endpoint needs write access, so this only models a sender without it: trust looks it up and refuses.
        ev = self.event()
        ev["sender"] = {"login": "stranger", "type": "User"}
        d = trust.decide("repository_dispatch", ev, "o/r", env={"PATH": "/nonexistent"})
        self.assertFalse(d.trusted)

    def test_another_repository_and_a_spoofed_bot_name_do_not(self):
        ev = self.event()
        ev["repository"] = {"full_name": "evil/r"}
        self.assertFalse(trust.decide("repository_dispatch", ev, "o/r", env={}).trusted)
        ev = self.event()
        ev["sender"] = {"login": "github-actions[bot]", "type": "User"}
        self.assertFalse(trust.decide("repository_dispatch", ev, "o/r", env={"PATH": "/nonexistent"}).trusted)

    def test_a_payload_without_findings_is_not_a_trigger(self):
        ev = self.event()
        del ev["client_payload"]["findings"]
        self.assertFalse(trust.decide("repository_dispatch", ev, "o/r", env={}).trusted)

    def test_s3_a_payload_with_a_key_that_is_not_ours_is_not_a_trigger(self):
        ev = self.event()
        ev["client_payload"]["extra"] = "x"
        self.assertFalse(trust.decide("repository_dispatch", ev, "o/r", env={}).trusted)
        self.assertEqual(tuple(self.event()["client_payload"]), trust.PAYLOAD_KEYS)

    def test_a_malformed_payload_is_not_a_trigger(self):
        for over in ({"issue": "7"}, {"branch": "pivotal/issue-8/a"}, {"slice": "b"}, {"head_sha": "A" * 40}, {"head_sha": "abc"},
                     {"findings": 5}, {"findings": None}, {"round": None}, {"issue_pin": None}, {"issue_pin": "B" * 64}, {"issue_pin": "b" * 63}):
            with self.subTest(over=over):
                ev = self.event(**over)
                self.assertFalse(trust.decide("repository_dispatch", ev, "o/r", env={}).trusted)

    def test_the_round_is_plans_to_bound(self):
        for bad in (None, "1", 1.0, True, -1, 0, plan.DEFAULT_MAX_ROUNDS + 1, 10**30):
            with self.subTest(round=bad), self.assertRaises(plan.SliceError):
                plan.check_fix_round(bad)
        self.assertEqual(plan.check_fix_round(plan.DEFAULT_MAX_ROUNDS), plan.DEFAULT_MAX_ROUNDS)


class PublishFixTest(PublishCase):
    def draft(self, reason: str = "verdict_needs_fixes", slice_id: str = "one") -> Path:
        out = self.forge({f"src/{slice_id}.txt": "x\n"}, slice_id=slice_id)
        land = assemble.read_land(out / assemble.LAND)
        land["slices"][0]["reason"] = reason
        (out / assemble.LAND).write_bytes(assemble.land_text(land))
        return out

    def open_pr(self, out: Path, *, existing: bool = False) -> dict:
        """The draft PR routes; ``existing`` is a later round, whose run finds the PR already open (``update_pr`` opens nothing)."""
        entry = assemble.read_land(out / assemble.LAND)["slices"][0]
        found = [self.pr(entry["branch"], entry["head"], 11)]
        self.route_list(self.gh, entry["branch"], *([found] if existing else [[], found]))
        self.route_create(self.gh, entry["branch"], 11)
        self.gh.route(DISPATCH, fakegh.ok(""))
        return entry

    def dispatches(self, gh: fakegh.FakeGh | None = None) -> list[dict]:
        return [c for c in (gh or self.gh).calls() if " ".join(c["args"]).startswith(DISPATCH)]

    def test_a_needs_fixes_draft_gets_one_dispatch_with_the_full_payload(self):
        out = self.draft()
        entry = self.open_pr(out)
        d = self.slices / "slice-one"
        d.mkdir(parents=True, exist_ok=True)
        (d / assemble.SUMMARY).write_text("gate output\nFAILED test_x\n")
        code, outputs, stdout, err = self.publish(out, ROUND="0")
        self.assertEqual(code, 0, err)
        sent = self.dispatches()
        self.assertEqual(len(sent), 1, self.gh.calls())
        self.assertEqual(sent[0]["args"][:4], ["api", "--method", "POST", f"repos/{REPO}/dispatches"])
        self.assertIn("Content-Type: application/json", sent[0]["args"], "R14: --input sends the file as JSON, said so")
        body = json.loads(sent[0]["input"])
        self.assertEqual(set(body), {"event_type", "client_payload"})
        got = body["client_payload"]
        self.assertEqual(tuple(got), fix.PAYLOAD_KEYS)
        self.assertEqual((got["issue"], got["slice"], got["round"], got["run_id"], got["branch"], got["head_sha"]),
                         (assemble.read_land(out / assemble.LAND)["issue"], "one", 1, 4242, entry["branch"], entry["head"]))
        self.assertEqual(got["issue_pin"], "a" * 64, "the round-0 issue pin is forwarded")
        self.assertIn("FAILED test_x", got["findings"])
        self.assertLessEqual(len(got["findings"].encode()), fix.MAX_FINDINGS_BYTES)
        self.assertNotIn(TOKEN, sent[0]["input"])
        self.assertIn("slice one sent as round 1", stdout)
        self.assertEqual(report.parse_result(outputs["result"]).status, "draft")

    def test_one_round_counts_up_and_the_next_stops(self):
        for current, expect in ((0, 1),):
            with self.subTest(round=current):
                out = self.draft(slice_id=f"s{current}")
                self.open_pr(out, existing=bool(current))
                self.assertEqual(self.publish(out, ROUND=str(current))[0], 0)
                self.assertEqual(json.loads(self.dispatches()[-1]["input"])["client_payload"]["round"], expect)
        out = self.draft(slice_id="last")
        self.open_pr(out, existing=True)
        before = len(self.dispatches())
        code, outputs, _, err = self.publish(out, ROUND=str(fix.MAX_ROUNDS))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.dispatches()), before, "F8: no dispatch past max_rounds")
        self.assertEqual(report.parse_result(outputs["result"]).why, "max_rounds")

    def test_conflict_drafts_are_fix_candidates_and_other_drafts_are_not(self):
        for reason, sends in (("gate_red", 0), ("conflict", 1), ("baseline_red", 0), ("verdict_missing", 0)):
            with self.subTest(reason=reason):
                gh = fakegh.FakeGh(self.root / f"bin-{reason}")
                out = self.draft(reason, slice_id=reason.replace("_", "-"))
                entry = assemble.read_land(out / assemble.LAND)["slices"][0]
                self.route_list(gh, entry["branch"], [], [self.pr(entry["branch"], entry["head"], 11)])
                self.route_create(gh, entry["branch"], 11)
                gh.route(DISPATCH, fakegh.ok(""))
                self.assertEqual(self.publish(out, gh)[0], 0)
                self.assertEqual(len(self.dispatches(gh)), sends)

    def test_a_run_without_a_well_formed_issue_pin_sends_nothing_and_says_so(self):
        for bad in ("", "x" * 64, "a" * 63):
            with self.subTest(pin=bad):
                gh = fakegh.FakeGh(self.root / f"bin-pin-{len(bad)}{bad[:1]}")
                out = self.draft(slice_id="pin" + str(len(bad)) + ("e" if bad else "n"))
                entry = assemble.read_land(out / assemble.LAND)["slices"][0]
                self.route_list(gh, entry["branch"], [], [self.pr(entry["branch"], entry["head"], 11)])
                self.route_create(gh, entry["branch"], 11)
                gh.route(DISPATCH, fakegh.ok(""))
                code, outputs, _, err = self.publish(out, gh, ISSUE_PIN=bad)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.dispatches(gh), [])
                self.assertIn("no_pin", err)
                self.assertEqual(json.loads(outputs["result"]).get("fix"), "failed")

    def test_a_part_that_cannot_be_read_says_so_in_the_findings_and_the_log(self):
        out = self.draft()  # no review artifact and no summary in this fixture's directories
        self.open_pr(out)
        code, _, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        findings = json.loads(self.dispatches()[0]["input"])["client_payload"]["findings"]
        self.assertEqual(findings.count("| (not readable)"), 2, findings)
        self.assertNotIn("(nothing)", findings)
        self.assertIn("the reviewer's text could not be read", err)
        self.assertIn("the build summary could not be read", err)

    def test_an_untrusted_run_sends_none(self):
        out = self.draft()
        self.open_pr(out)
        self.publish(out, TRUSTED="false")
        self.assertEqual(self.dispatches(), [])

    def test_a_dispatch_without_a_good_answer_is_unknown_not_not_started_and_changes_nothing_else(self):
        out = self.draft()
        self.open_pr(out)
        self.gh.route(DISPATCH, fakegh.fail("HTTP 502"), first=True)
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertIn("not sent (unknown)", err)
        result = json.loads(outputs["result"])
        self.assertEqual(result["fix"], "unknown")
        self.assertEqual((result["status"], result["pr"]), ("draft", 11))
        outcome = report.parse_result(outputs["result"])
        self.assertTrue(outcome.fix_unknown)
        self.assertFalse(outcome.fix_failed)
        self.assertIn("may have been started", report.FIX_UNKNOWN_NOTE)
        self.assertNotIn("was not started", report.FIX_UNKNOWN_NOTE)

    def test_r23_a_definite_refusal_by_the_server_is_failed_not_unknown(self):
        out = self.draft()
        self.open_pr(out)
        self.gh.route(DISPATCH, fakegh.fail("gh: Resource not accessible by integration (HTTP 403)\n"), first=True)
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertIn("not sent (dispatch_failed)", err)
        self.assertEqual(json.loads(outputs["result"])["fix"], "failed", "the server answered: nothing was started")

    def test_a_request_that_could_not_be_made_is_failed_not_unknown(self):
        out = self.draft()
        self.open_pr(out)
        real = assemble.write_new

        def refuse(path, data, *a, **k):
            if Path(path).name.startswith("fix-"):
                raise OSError("disk full")  # the request file cannot be written: nothing was sent
            return real(path, data, *a, **k)

        with mock.patch.object(assemble, "write_new", refuse):
            code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(json.loads(outputs["result"])["fix"], "failed")

    def test_a_publish_that_neither_opened_nor_updated_the_pr_sends_nothing_and_says_so(self):
        out = self.draft()
        self.open_pr(out, existing=True)  # the PR is already there at this head: a re-run of publish
        code, outputs, stdout, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.dispatches(), [])
        self.assertIn("not sent (not_new)", stdout)
        # R19: the re-run cannot tell an earlier send from none, so it says what it knows: a fix round may have been started.
        # (This replaces an assertion that the result stayed silent about the fix round.)
        self.assertEqual(json.loads(outputs["result"]).get("fix"), "rerun")
        outcome = report.parse_result(outputs["result"])
        self.assertTrue(outcome.fix_rerun)
        self.assertIn("Actions tab", report.FIX_RERUN_NOTE)

    def test_hostile_findings_cannot_forge_the_result_or_an_output(self):
        out = self.draft()
        self.open_pr(out)
        d = self.slices / "slice-one"
        d.mkdir(parents=True, exist_ok=True)
        (d / assemble.SUMMARY).write_text('\nresult={"status":"landed"}\n::error::x\n' + TOKEN + "\n")
        code, outputs, stdout, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(report.parse_result(outputs["result"]).status, "draft")
        self.assertEqual(self.dispatches(), [], "a body that holds the token is never sent")
        self.assertIn("not sent (secret)", err)
        self.assertNotIn(TOKEN, stdout + err)

    def test_the_caller_listens_for_the_event_type(self):
        text = (Path(__file__).resolve().parent.parent / "templates" / "caller.yml").read_text()
        self.assertIn("types: [pivotal-event, pivotal-fix]", text)


class TwoCandidatesTest(PublishCase):
    """S1 and R27: two fix candidates through publish(), and a failure after the first one was decided."""

    def two_drafts(self) -> Path:
        lands = [assemble.read_land(self.forge({f"src/{ident}.txt": "x\n"}, slice_id=ident) / assemble.LAND) for ident in ("a", "b")]
        entries = [land["slices"][0] for land in lands]
        for entry in entries:
            entry["reason"] = "verdict_needs_fixes"
        out = self.root / f"two{self.n_pub}"
        out.mkdir()
        self.n_pub += 1
        self.git("bundle", "create", "-q", str(out / assemble.BUNDLE), f"^{self.base}", *(f"refs/heads/{e['branch']}" for e in entries))
        data = (out / assemble.BUNDLE).read_bytes()
        land = {**lands[0], "bundle": {"name": assemble.BUNDLE, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
                "slices": entries}
        (out / assemble.LAND).write_bytes(assemble.land_text(land))
        for number, entry in zip((11, 12), entries):
            self.route_list(self.gh, entry["branch"], [], [self.pr(entry["branch"], entry["head"], number)])
            self.route_create(self.gh, entry["branch"], number)
        self.gh.route(DISPATCH, fakegh.ok(""), fakegh.ok(""))
        return out

    def test_s1_two_candidates_each_get_one_dispatch(self):
        code, outputs, stdout, err = self.publish(self.two_drafts())
        self.assertEqual(code, 0, err)
        sent = [c for c in self.gh.calls() if " ".join(c["args"]).startswith(DISPATCH)]
        self.assertEqual(sorted(json.loads(c["input"])["client_payload"]["slice"] for c in sent), ["a", "b"])
        self.assertNotIn("fix", json.loads(outputs["result"]))

    def test_r27_a_failure_after_the_first_decision_keeps_its_word_and_marks_only_the_undecided(self):
        real, calls = publish.send_one, []

        def once(*args):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("late")
            return real(*args)

        out = self.two_drafts()
        seen, real_result = [], publish.fix_result
        with mock.patch.object(publish, "send_one", once), mock.patch.object(
                publish, "fix_result", lambda words: seen.append(dict(words)) or real_result(words)):
            code, outputs, stdout, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [{"a": "sent", "b": "unexpected"}], "a keeps its word; only the undecided b is unexpected")
        self.assertIn("slice a sent as round 1", stdout)
        self.assertEqual(sum(1 for c in self.gh.calls() if " ".join(c["args"]).startswith(DISPATCH)), 1, "a was sent")
        self.assertIn("fix round: failed (unexpected)", err)

    def test_r27_the_words_decided_before_the_failure_are_left_in_the_callers_dict(self):
        out = self.two_drafts()
        land = assemble.read_land(out / assemble.LAND)
        s = publish.read_settings(self.penv(out, None))
        words = {}
        with mock.patch.object(publish, "send_one", side_effect=["sent", RuntimeError("late")]):
            with self.assertRaises(RuntimeError):
                publish.send_fix_rounds({}, s, land, {e["id"]: {"state": "OPEN", "status": publish.OPENED, "pr": 1} for e in land["slices"]}, {}, words)
        self.assertEqual(words, {"a": "sent"})


class GhAnswerTest(unittest.TestCase):
    """R23: job.gh tells a refusal the server gave (an HTTP 4xx in gh's message), and a gh that never ran, from an answer
    that is not known (a failure with no HTTP status, or a timeout)."""

    def fake(self, script: str) -> str:
        """A fake gh: a shell script that prints and exits as the test says (the one binary job.gh runs)."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "gh"
        path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def test_r23_an_http_4xx_is_a_refusal_and_a_5xx_or_a_plain_failure_is_not(self):
        for script, refused in (("echo 'gh: Not Found (HTTP 404)' >&2; exit 1", True),
                                ("echo 'gh: Forbidden (HTTP 403)' >&2; exit 1", True),
                                ("echo 'gh: Server Error (HTTP 502)' >&2; exit 1", False),
                                ("echo 'error connecting to api.github.com' >&2; exit 1", False)):
            with self.subTest(script=script):
                with self.assertRaises(job.JobError) as caught:
                    job.gh({"PATH": os.environ["PATH"]}, "api", "x", binary=self.fake(script))
                self.assertEqual(isinstance(caught.exception, job.GhRefused), refused)

    def test_r26_an_answer_that_is_not_utf8_is_still_an_answer_not_a_gh_that_could_not_run(self):
        env = {"PATH": os.environ["PATH"]}
        self.assertIn("ok", job.gh(env, "api", "x", binary=self.fake("printf 'ok \\377\\376'")))
        with self.assertRaises(job.JobError) as caught:
            job.gh(env, "api", "x", binary=self.fake("printf 'gh: \\377 (HTTP 502)' >&2; exit 1"))
        self.assertNotIsInstance(caught.exception, job.GhRefused, "a possibly accepted request is not 'not started'")
        with self.assertRaises(job.GhRefused) as caught:
            job.gh(env, "api", "x", binary=self.fake("printf 'gh: \\377 (HTTP 404)' >&2; exit 1"))
        self.assertNotIn("could not run", str(caught.exception))

    def test_r23_a_gh_that_cannot_start_is_a_refusal_and_one_that_times_out_is_not(self):
        with self.assertRaises(job.GhRefused):
            job.gh({"PATH": os.environ["PATH"]}, "api", "x", binary="/nonexistent/gh-for-test")
        with self.assertRaises(job.JobError) as caught:
            job.gh({"PATH": os.environ["PATH"]}, "api", "x", binary=self.fake("sleep 5"), timeout=1)
        self.assertNotIsInstance(caught.exception, job.GhRefused, "a request that may have been made is not a refusal")


class ResultWordsTest(unittest.TestCase):
    """R18: every kind of fix-round problem reaches the RESULT, not only the first one that is checked."""

    def test_r18_failed_and_unknown_are_both_in_the_result_and_the_report(self):
        self.assertEqual(publish.fix_result({"one": "dispatch_failed", "two": "unknown"}), "failed unknown")
        self.assertEqual(publish.fix_result({"one": "unknown", "two": "sent"}), "unknown")
        self.assertEqual(publish.fix_result({"one": "untrusted", "two": "max_rounds"}), "")
        line = json.dumps({"status": "draft", "why": "review_rejected", "built_by": "claude:anthropic/sonnet",
                           "reviewed_by": "agy:google/gemini", "pr": 11, "fix": "failed unknown"})
        outcome = report.parse_result(line)
        self.assertTrue(outcome.fix_failed and outcome.fix_unknown and not outcome.fix_rerun)
        bad = report.parse_result(line.replace("failed unknown", "failed nonsense"))
        self.assertEqual((bad.status, bad.why), ("failed", "bad_result"), "a word that is not one of ours is a bad RESULT")


class PinFieldTest(unittest.TestCase):
    """R24: the receiver's pinned fields are derived from the payload's keys, not kept as a second list."""

    def test_r24_the_pinned_fields_are_the_payload_keys_without_findings(self):
        self.assertIs(trust.PAYLOAD_KEYS, fix.PAYLOAD_KEYS)
        self.assertEqual(trust.FIX_PIN_FIELDS, trust.PAYLOAD_KEYS[:-1])
        self.assertEqual(trust.PAYLOAD_KEYS[-1:], ("findings",))


class UpdatePrTest(PublishCase):
    """R13: a fix round's `update_pr` against a real bare remote: the push, the comment and each refusal."""

    def setUp(self):
        super().setUp()
        self.gh = fakegh.FakeGh(self.root / "bin-update")

    def draft_at_old_head(self, slice_id: str = "one", *, remote_at: str | None = None, pr_at: str | None = None, hook: str = "") -> tuple[Path, dict]:
        out = self.forge({f"src/{slice_id}.txt": "x\n"}, slice_id=slice_id)
        entry = assemble.read_land(out / assemble.LAND)["slices"][0]
        entry_branch = entry["branch"]
        self.git("update-ref", f"refs/heads/{entry_branch}", remote_at or self.base, cwd=self.bare)
        if hook:
            hook_path = self.bare / "hooks" / "pre-receive"
            hook_path.parent.mkdir(exist_ok=True)
            hook_path.write_text(hook)
            hook_path.chmod(0o755)
        if pr_at is not None:
            self.route_list(self.gh, entry_branch, [self.pr(entry_branch, pr_at, 11)])
        else:
            self.route_list(self.gh, entry_branch, [])
        self.gh.route("pr comment 11", fakegh.ok(""))
        return out, entry

    def comments(self) -> list[list[str]]:
        return self.calls(self.gh, "pr comment")

    def test_a_fix_push_moves_the_branch_and_the_pr_gets_a_comment(self):
        out, entry = self.draft_at_old_head(pr_at=self.base)
        code, outputs, _, err = self.publish(out, self.gh, ROUND="1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_refs()[f"refs/heads/{entry['branch']}"], entry["head"])
        self.assertEqual(len(self.comments()), 1)
        self.assertEqual(self.comments()[0][2], "11")
        self.assertEqual(self.creates(self.gh), [])
        self.assertEqual(report.parse_result(outputs["result"]).status, "draft")

    def test_r17_only_a_fix_round_that_passed_says_it_passed(self):
        for reason, words in (("fix_round", "passed its check and review"), ("gate_red", "did not pass: gate_red"),
                              ("verdict_missing", "did not pass: verdict_missing")):
            with self.subTest(reason=reason):
                out, entry = self.draft_at_old_head(pr_at=self.base, slice_id=reason.replace("_", "-"))
                land = assemble.read_land(out / assemble.LAND)
                land["slices"][0]["reason"] = reason
                (out / assemble.LAND).write_bytes(assemble.land_text(land))
                texts: list[str] = []
                real = assemble.write_new

                def capture(path, data, *a, **k):
                    if Path(path).name.startswith("comment-"):
                        texts.append(data.decode("ascii"))
                    return real(path, data, *a, **k)

                with mock.patch.object(assemble, "write_new", capture):
                    code, _, _, err = self.publish(out, self.gh, ROUND="1")
                self.assertEqual(code, 0, err)
                (text,) = texts
                self.assertIn(words, text)
                if reason != "fix_round":
                    self.assertNotIn("passed", text, "a round that did not pass is not said to have passed")

    def test_a_comment_that_fails_changes_nothing(self):
        out, entry = self.draft_at_old_head(pr_at=self.base)
        self.gh.route("pr comment 11", fakegh.fail("HTTP 500"), first=True)
        code, outputs, _, err = self.publish(out, self.gh, ROUND="1")
        self.assertEqual(code, 0, err)
        self.assertIn("could not be made", err)
        self.assertEqual(self.remote_refs()[f"refs/heads/{entry['branch']}"], entry["head"])
        self.assertEqual(report.parse_result(outputs["result"]).status, "draft")

    def test_no_open_pr_is_pr_differs_and_pushes_nothing(self):
        out, entry = self.draft_at_old_head()
        before = self.remote_refs()
        code, outputs, _, err = self.publish(out, self.gh, ROUND="1")
        self.assertEqual(self.remote_refs(), before)
        self.assertEqual(report.parse_result(outputs["result"]).why, "pr_differs", err)
        self.assertEqual(self.comments(), [])

    def test_a_pr_at_another_commit_is_pr_differs(self):
        out, entry = self.draft_at_old_head(pr_at="d" * 40)
        before = self.remote_refs()
        _, outputs, _, _ = self.publish(out, self.gh, ROUND="1")
        self.assertEqual(self.remote_refs(), before)
        self.assertEqual(report.parse_result(outputs["result"]).why, "pr_differs")

    def test_a_branch_that_is_not_where_the_pr_was_is_branch_differs_and_never_forced(self):
        other = self.git("rev-parse", "HEAD")  # a commit that is neither the old nor the new head
        self.commit({"other.txt": "o\n"}, message="other")
        other = self.git("rev-parse", "HEAD")
        self.git("push", "-q", str(self.bare), f"{other}:refs/heads/other-side")
        self.git("reset", "-q", "--hard", self.base)
        out, entry = self.draft_at_old_head(remote_at=other, pr_at=self.base)
        _, outputs, _, _ = self.publish(out, self.gh, ROUND="1")
        self.assertEqual(self.remote_refs()[f"refs/heads/{entry['branch']}"], other)
        self.assertEqual(report.parse_result(outputs["result"]).why, "branch_differs")

    def test_a_push_the_remote_refuses_is_push_rejected(self):
        out, entry = self.draft_at_old_head(pr_at=self.base, hook="#!/bin/sh\nexit 1\n")
        _, outputs, _, _ = self.publish(out, self.gh, ROUND="1")
        self.assertEqual(self.remote_refs()[f"refs/heads/{entry['branch']}"], self.base)
        self.assertEqual(report.parse_result(outputs["result"]).why, "push_rejected")
        self.assertEqual(self.comments(), [])


if __name__ == "__main__":
    unittest.main()
