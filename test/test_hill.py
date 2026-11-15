"""The hill-climb rules (engine/hill.py): the footer, the command and target, the score file, the history and the met rule.

Every file the tests write is in their own temporary directory. No command here is run; the job tests do that with fake scorers.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from engine import hill

SHA = "a" * 40
SHA2 = "b" * 40


def body(score: str | None = "python3 score.py", target: str | None = "0.9", extra: str = "") -> str:
    """An issue body as piv do writes it: the text, then the footer."""
    footer = ["---", "Pivotal request", "- line: build"]
    if score is not None:
        footer.append(f"- score: {score}")
    if target is not None:
        footer.append(f"- target: {target}")
    return "\n\n".join(["Make the page look right.", "\n".join(footer) + extra]) + "\n"


class FooterTest(unittest.TestCase):
    def test_a_footer_with_a_score_and_a_target_is_the_goal(self):
        self.assertEqual(hill.read_goal(body()), hill.Goal("python3 score.py", "0.9"))

    def test_a_footer_without_a_score_has_no_goal(self):
        self.assertIsNone(hill.read_goal(body(score=None, target=None)))
        self.assertIsNone(hill.read_goal("just a request, no footer\n"))

    def test_a_score_without_a_target_or_a_target_without_a_score_is_refused(self):
        with self.assertRaises(hill.HillError):
            hill.read_goal(body(target=None))
        with self.assertRaises(hill.HillError):
            hill.read_goal(body(score=None))

    def test_two_score_lines_are_refused(self):
        text = body(extra="\n- score: other")
        with self.assertRaises(hill.HillError):
            hill.read_goal(text)

    def test_a_crlf_body_reads_the_same_goal(self):
        self.assertEqual(hill.read_goal(body().replace("\n", "\r\n")), hill.Goal("python3 score.py", "0.9"))

    def test_a_lone_cr_is_not_a_line_break_and_a_command_with_one_is_refused(self):
        with self.assertRaises(hill.HillError):
            hill.read_goal(body("python3 s.py\rrm -rf x"))
        with self.assertRaises(hill.HillError):  # every break is a lone CR: the body shows its pair on CR lines, and no LF split sees it
            hill.read_goal(body().replace("\n", "\r"))

    def test_a_body_whose_line_breaks_are_all_lone_cr_and_shows_a_score_request_is_refused(self):
        for text in ("---\rPivotal request\r- line: build\r- score: python3 s.py\r- target: 0.9\r",
                     "Do it.\r- score: x\r- target: 0.9",
                     "Do it.\r\r---\rPivotal request\r- score: cat s\r- target: 0.9\r"):
            with self.subTest(text=text), self.assertRaises(hill.HillError):
                hill.read_goal(text)

    def test_a_body_with_lone_cr_and_no_score_request_still_plans(self):
        self.assertIsNone(hill.read_goal("Do it.\rThen more.\r---\rPivotal request\r- line: build\r"))
        self.assertIsNone(hill.read_goal("Speed up.\r- target: mobile users\r"))

    def test_a_lone_score_line_is_refused_with_or_without_a_footer(self):
        for text in ("Do it.\n- score: x\n\n---\nPivotal request\n- line: build\n",
                     "Do it.\n- score: x\n",
                     "- Score: high priority\n",
                     "---\nPivotal request\n- line: build\n- score: x\n"):
            with self.subTest(text=text), self.assertRaises(hill.HillError):
                hill.read_goal(text)

    def test_a_footer_key_piv_does_not_write_is_not_checked_a_waiver(self):
        # Waiver: the footer's lines are not validated against the keys piv writes (line, mockup, score, target), because a body
        # may carry any other "- key: value" line (an ordinary note) and a misspelt "- scor:" is one. It plans with no score.
        # (Round 4, S2: a key one edit from score or target is refused, see LineBreakAndProseTest; any other key still plans.)
        for text in ("Do it.\n---\nPivotal request\n- line: build\n- note: ok\n",
                     "Do it.\n---\nPivotal request\n- line: build\n- scoring: cat s\n- goal: 0.9\n"):
            with self.subTest(text=text):
                self.assertIsNone(hill.read_goal(text))

    def test_an_earlier_footer_never_reaches_the_shell_and_the_last_footer_is_read(self):
        text = "t\n---\nPivotal request\n- score: rm -rf earlier\n- target: 0.5\n\n" + body("later cmd", "0.7")
        goal = hill.read_goal(text)
        self.assertEqual(goal, hill.Goal("later cmd", "0.7"))
        self.assertNotIn("earlier", goal.command)

    def test_a_long_whitespace_line_is_read_in_linear_time(self):
        import time
        for text in (" " * 40000 + "x\n", "Do it.\n" + " " * 40000 + "x\n\n---\nPivotal request\n- line: build\n" + " " * 40000 + "x\n",
                     "\t" * 40000 + "-\n"):
            start = time.monotonic()
            hill.read_goal(text)
            self.assertLess(time.monotonic() - start, 1.0)

    def test_trailing_spaces_on_the_footer_markers_are_ignored(self):
        text = body().replace("---\n", "---  \t\n").replace("Pivotal request\n", "Pivotal request \n")
        self.assertEqual(hill.read_goal(text), hill.Goal("python3 score.py", "0.9"))

    def test_a_score_or_target_line_under_no_footer_is_refused(self):
        for text in ("Do it.\n\n- score: python3 s.py\n- target: 0.9\n",
                     "Do it.\n---\nSomething else\n- target: 0.9\n",
                     "Do it.\n---\nPivotal request  x\n- score: a\n- target: 0.9\n"):
            with self.subTest(text=text), self.assertRaises(hill.HillError):
                hill.read_goal(text)

    def test_loosely_written_score_or_target_lines_in_or_around_the_footer_are_refused(self):
        pair = "- score: python3 s.py\n- target: 0.9"
        cases = {
            "no space": "---\nPivotal request\n- score:cat s\n- target:0.9\n",
            "star bullets": "---\nPivotal request\n* score: cat s\n* target: 0.9\n",
            "capitals": "---\nPivotal request\n- Score: cat s\n- Target: 0.9\n",
            "indented": "---\nPivotal request\n  - score: cat s\n  - target: 0.9\n",
            "tab": "---\nPivotal request\n-\tscore: cat s\n-\ttarget: 0.9\n",
            "extra target": f"---\nPivotal request\n{pair}\n- target:0.5\n",
            "extra loose score": f"---\nPivotal request\n{pair}\nscore: other\n",
            "later footer": f"Do it.\n---\nPivotal request\n{pair}\n\n---\nPivotal request\n- line: build\n",
            "score above a footer with none": f"Do it.\n{pair}\n\n---\nPivotal request\n- line: build\n",
            "score line outside a footer that has a score": f"Do it.\n- score: x\n---\nPivotal request\n{pair}\n",
            "loose pair, no footer": "Do it.\n* score: cat s\n* target: 0.9\n",
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(hill.HillError):
                hill.read_goal(text)

    def test_an_ordinary_bullet_named_target_or_score_still_plans(self):
        for text in ("Speed up the page.\n\n- target: mobile users\n",
                     "Speed up.\n- target: 50ms latency\n- Target: desktop\n",
                     "- target: mobile users\n\n" + body(score=None, target=None, extra="\n- note: ok")):
            with self.subTest(text=text):
                self.assertIsNone(hill.read_goal(text))

    def test_the_last_footer_is_the_one_read(self):
        text = "text\n---\nPivotal request\n- score: a\n- target: 0.5\n\n" + body("b", "0.7")
        self.assertEqual(hill.read_goal(text), hill.Goal("b", "0.7"))

    def test_targets_outside_above_zero_to_one_are_refused(self):
        for target in ("0", "0.0", "1.5", "2", "-0.1", "0.9999999", "nine", "", ".", "0.9 "):
            with self.subTest(target=target), self.assertRaises(hill.HillError):
                hill.read_goal(body(target=target))

    def test_the_boundary_targets_are_accepted(self):
        self.assertEqual(hill.check_target("1"), 1.0)
        self.assertEqual(hill.check_target("1.0"), 1.0)
        self.assertEqual(hill.check_target(".5"), 0.5)
        self.assertEqual(hill.check_target("0.000001"), 0.000001)

    def test_a_command_with_a_control_or_line_break_is_refused(self):
        for command in ("a\nb", "a\rb", "a\x00b", "a‮b", "a b", "", "   "):
            with self.subTest(command=repr(command)), self.assertRaises(hill.HillError):
                hill.check_command(command)

    def test_a_command_over_the_limit_is_refused(self):
        with self.assertRaises(hill.HillError):
            hill.check_command("x" * (hill.COMMAND_MAX + 1))
        self.assertEqual(hill.check_command("x" * hill.COMMAND_MAX), "x" * hill.COMMAND_MAX)

    def test_the_command_round_trips_through_the_job_output_form(self):
        for command in ("python3 score.py", "echo 0.5 | tee x; true", "printf 'é ü'"):
            encoded = hill.encode_command(command)
            self.assertRegex(encoded, hill.ENCODED.pattern)
            self.assertEqual(hill.decode_command(encoded), command)

    def test_an_encoded_command_that_is_not_base64_or_not_a_command_is_refused(self):
        for bad in ("", "not base64!", "YQ==\n", "/w==", "Cg=="):  # "/w==" is a non-UTF-8 byte; "Cg==" is a newline
            with self.subTest(bad=bad), self.assertRaises(hill.HillError):
                hill.decode_command(bad)

    def test_an_encoded_command_at_the_largest_size_still_fits_the_output_rule(self):
        command = "é" * hill.COMMAND_MAX  # 1000 characters, two bytes each in UTF-8
        self.assertRegex(hill.encode_command(command), hill.ENCODED.pattern)


class ScoreFileTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="hill-test-")
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def write(self, data: bytes, name: str = "score.txt") -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_one_number_from_zero_to_one_is_the_score(self):
        for data, value in ((b"0.92\n", 0.92), (b"1\n", 1.0), (b"0", 0.0), (b" .5 ", 0.5), (b"0.123456", 0.123456)):
            with self.subTest(data=data):
                self.assertEqual(hill.read_score(self.write(data)), value)

    def test_anything_else_in_the_file_is_no_score(self):
        for data in (b"", b"0.9\n0.8\n", b"1.5", b"-0.1", b"abc", b"0.9 x", b"\xff", b"nan", b"1e-3", b"0." + b"9" * 10,
                     b"0.9" + b" " * 100):
            with self.subTest(data=data):
                self.assertIsNone(hill.read_score(self.write(data)))

    def test_a_missing_file_a_symlink_and_a_directory_are_no_score(self):
        self.assertIsNone(hill.read_score(self.dir / "missing.txt"))
        real = self.write(b"0.9\n", "real.txt")
        link = self.dir / "link.txt"
        os.symlink(real, link)
        self.assertIsNone(hill.read_score(link))
        sub = self.dir / "sub"
        sub.mkdir()
        self.assertIsNone(hill.read_score(sub))

    def test_a_fifo_is_no_score_and_does_not_block(self):
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        self.assertIsNone(hill.read_score(fifo))

    def test_an_oversize_file_is_no_score(self):
        self.assertIsNone(hill.read_score(self.write(b"0." + b"1" * hill.SCORE_FILE_MAX)))


class CheckWithScoreTest(unittest.TestCase):
    def test_a_blank_check_leaves_only_the_score_part(self):
        self.assertEqual(hill.check_with_score("", "python3 s.py", "/tmp/o"), "(\npython3 s.py\n) > /tmp/o")

    def test_the_check_runs_first_in_its_own_subshell_and_the_score_after_it(self):
        text = hill.check_with_score("make test", "python3 s.py", "/tmp/o")
        self.assertEqual(text, "(\nmake test\n)\n(\npython3 s.py\n) > /tmp/o")

    def test_the_output_path_is_quoted(self):
        text = hill.check_with_score("", "s", "/tmp/with space/o'x")
        self.assertIn("'/tmp/with space/o'\"'\"'x'", text)


class MetTest(unittest.TestCase):
    def test_a_score_at_or_above_the_target_is_met(self):
        self.assertTrue(hill.met(0.9, "0.9"))
        self.assertTrue(hill.met(0.92, "0.9"))
        self.assertTrue(hill.met(1.0, "1"))

    def test_a_score_below_the_target_is_not_met(self):
        self.assertFalse(hill.met(0.4, "0.9"))
        self.assertFalse(hill.met(0.8999, "0.9"))

    def test_a_target_without_a_score_is_not_met_and_no_target_is_always_met(self):
        self.assertFalse(hill.met(None, "0.9"))
        self.assertTrue(hill.met(None, None))
        self.assertTrue(hill.met(None, ""))
        self.assertTrue(hill.met(0.0, None))


class HistoryTest(unittest.TestCase):
    def test_the_history_round_trips_in_order(self):
        entries = [(0.4, SHA), (0.7, SHA2), (0.92, SHA)]
        text = hill.encode_history(entries)
        self.assertEqual(text, f"0.4@{SHA};0.7@{SHA2};0.92@{SHA}")
        self.assertEqual(hill.decode_history(text), entries)

    def test_the_empty_history_is_no_entries_and_encodes_as_empty(self):
        self.assertEqual(hill.decode_history(""), [])
        self.assertEqual(hill.encode_history([]), "")

    def test_scores_are_written_with_at_most_six_decimals_and_no_zeros_to_spare(self):
        self.assertEqual(hill.score_text(1.0), "1")
        self.assertEqual(hill.score_text(0.0), "0")
        self.assertEqual(hill.score_text(0.4), "0.4")
        self.assertEqual(hill.score_text(0.1234567), "0.123457")

    def test_a_malformed_history_is_refused(self):
        for bad in (f"0.4@{SHA[:-1]}", f"0.4 @{SHA}", f"1.5@{SHA}", f"-1@{SHA}", f"0.4@{SHA.upper()}", f"0.4@{SHA};", "x",
                    f"0.4@{SHA},0.5@{SHA}", f"0.4@{SHA}\n", f"0.1234567@{SHA}"):
            with self.subTest(bad=bad), self.assertRaises(hill.HillError):
                hill.decode_history(bad)

    def test_more_entries_than_the_rounds_allow_are_refused(self):
        many = ";".join([f"0.1@{SHA}"] * (hill.MAX_ROUNDS + 2))
        with self.assertRaises(hill.HillError):
            hill.decode_history(many)

    def test_the_best_round_is_the_highest_score_and_the_earliest_on_a_tie(self):
        self.assertEqual(hill.best([(0.4, SHA), (0.7, SHA2), (0.6, SHA)]), (0.7, SHA2))
        self.assertEqual(hill.best([(0.7, SHA), (0.7, SHA2)]), (0.7, SHA))
        self.assertIsNone(hill.best([]))


class ClassifyTest(unittest.TestCase):
    """assemble.classify with a score: a green gate below the target is a draft (score_low); a fix round that meets it with
    a READY verdict is a draft that publish marks ready (score_met); round 0 that meets it stacks as before."""

    def result(self, round_number=0, score=None, target="", gate="green"):
        return {"status": "ok", "reason": "", "gate": gate, "round": round_number, "workflow_change": False,
                "score": score, "target": target}

    def test_a_green_gate_below_the_target_is_a_draft_that_a_fix_round_may_follow(self):
        from engine import assemble
        self.assertEqual(assemble.classify(self.result(0, 0.4, "0.9"), "green"), ("draft", "score_low"))
        self.assertEqual(assemble.classify(self.result(1, 0.7, "0.9"), "green"), ("draft", "score_low"))

    def test_no_score_with_a_target_is_below_the_target(self):
        from engine import assemble
        self.assertEqual(assemble.classify(self.result(0, None, "0.9"), "green"), ("draft", "score_low"))

    def test_round_zero_at_the_target_stacks_and_a_fix_round_at_it_is_ready_when_the_verdict_is_clean(self):
        from engine import assemble
        self.assertEqual(assemble.classify(self.result(0, 0.92, "0.9"), "green"), ("candidate", "stacked"))
        self.assertEqual(assemble.classify(self.result(2, 0.92, "0.9"), "green"), ("draft", "score_met"))
        self.assertEqual(assemble.classify(self.result(2, 0.92, "0.9"), "green", "verdict_missing"), ("draft", "verdict_missing"))

    def test_a_request_without_a_score_is_unchanged(self):
        from engine import assemble
        self.assertEqual(assemble.classify(self.result(0), "green"), ("candidate", "stacked"))
        self.assertEqual(assemble.classify(self.result(1), "green"), ("draft", "fix_round"))

    def test_a_red_gate_is_still_a_draft_for_its_own_reason(self):
        from engine import assemble
        self.assertEqual(assemble.classify(self.result(0, 0.95, "0.9", gate="red"), "green"), ("draft", "gate_red"))


class FixRoundScoresTest(unittest.TestCase):
    """plan.fix_round checks the score history: one entry per round built for a score run, and none for a request without one."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="hill-fix-")
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def run_fix(self, round_number, scores, scored):
        from engine import plan
        event = self.dir / "event.json"
        event.write_text(json.dumps({"client_payload": {
            "issue": 7, "slice": "main", "round": round_number, "run_id": 3, "branch": "pivotal/issue-7/main",
            "head_sha": SHA, "issue_pin": "a" * 64, "scores": scores, "findings": "x"}}), encoding="utf-8")
        return plan.fix_round({"GITHUB_EVENT_PATH": str(event)}, 7, hill.MAX_ROUNDS if scored else 1, scored=scored)

    def test_a_score_run_needs_one_entry_per_round_built_so_far(self):
        from engine import plan
        self.assertEqual(self.run_fix(2, f"0.4@{SHA};0.7@{SHA2}", True)[2], f"0.4@{SHA};0.7@{SHA2}")
        for bad in ("", f"0.4@{SHA}", f"0.4@{SHA};0.7@{SHA2};0.9@{SHA}"):
            with self.subTest(scores=bad), self.assertRaises(Exception) as caught:
                self.run_fix(2, bad, True)
            self.assertIsInstance(caught.exception, plan.JobError)

    def test_a_request_without_a_score_carries_no_history(self):
        from engine import plan
        self.assertEqual(self.run_fix(1, "", False)[2], "")
        with self.assertRaises(plan.JobError):
            self.run_fix(1, f"0.4@{SHA}", False)

    def test_a_score_run_may_go_to_the_hill_bound_and_no_further(self):
        from engine import plan
        history = ";".join(f"0.1@{SHA}" for _ in range(hill.MAX_ROUNDS))
        self.assertEqual(self.run_fix(hill.MAX_ROUNDS, history, True)[0], hill.MAX_ROUNDS)
        with self.assertRaises(plan.JobError):
            self.run_fix(hill.MAX_ROUNDS + 1, history + f";0.1@{SHA}", True)


class ResultPairingTest(unittest.TestCase):
    """build.check_result: a score and its target go together, and an ok result with a target has its score."""

    def ok(self, **over):
        from engine import build
        data = {"status": "ok", "reason": "", "agent": "claude", "gate": "green", "built_by": "claude:anthropic/sonnet",
                "fell_back": [], "healed": False, "slice": "main", "round": 1, "workflow_change": False,
                "score": 0.7, "target": "0.9", **over}
        return build.check_result(data)

    def test_a_score_with_its_target_is_accepted(self):
        self.assertEqual(self.ok()["score"], 0.7)
        self.assertEqual(self.ok(score=None, status="failed", reason="gate_red", agent="", built_by="", gate="red")["score"], None)

    def test_a_score_without_a_target_or_an_ok_target_without_a_score_is_refused(self):
        from engine import build
        for over in ({"target": ""}, {"score": None}, {"score": 1.5}, {"target": "2"}):
            with self.subTest(over=over), self.assertRaises(build.ResultError):
                self.ok(**over)


class RoundBoundChoiceTest(unittest.TestCase):
    def test_a_score_run_has_the_hill_bound_and_any_other_run_the_default(self):
        self.assertEqual(hill.round_bound(True, 1), hill.MAX_ROUNDS)
        self.assertEqual(hill.round_bound(False, 1), 1)
        self.assertEqual(hill.round_bound(False, 3), 3)


class KeepBestTest(unittest.TestCase):
    """R3: a round below the best so far is never pushed, whatever the reason it is a draft for."""

    def kept(self, reason, score, history=((0.95, SHA),), target="0.9"):
        from types import SimpleNamespace
        from engine import publish
        s = SimpleNamespace(target=target, scores=hill.encode_history(list(history)) if history else "")
        return publish.hill_best(s, {"reason": reason, "score": score})

    def test_a_lower_round_is_kept_back_for_every_draft_reason(self):
        for reason in ("score_low", "score_met", "verdict_needs_fixes", "verdict_missing", "multi_commit", "conflict", "fix_round"):
            with self.subTest(reason=reason):
                self.assertEqual(self.kept(reason, 0.92), (0.95, SHA))

    def test_a_tie_keeps_the_earlier_round(self):
        self.assertEqual(self.kept("score_low", 0.95), (0.95, SHA))
        self.assertEqual(self.kept("score_met", 0.95), (0.95, SHA))

    def test_a_better_round_or_a_first_round_or_no_score_is_published(self):
        self.assertIsNone(self.kept("verdict_needs_fixes", 0.97))
        self.assertIsNone(self.kept("verdict_needs_fixes", 0.92, history=()))
        self.assertIsNone(self.kept("verdict_needs_fixes", None))
        self.assertIsNone(self.kept("verdict_needs_fixes", 0.5, target=""))


class RoundBoundTest(unittest.TestCase):
    """publish.send_fix_rounds: a score run sends a next round from every round below the bound, and the last round sends none."""

    def words(self, round_number):
        from types import SimpleNamespace
        from unittest import mock
        from engine import publish
        s = SimpleNamespace(target="0.9", round=round_number, trusted=True)
        land = {"slices": [{"id": "main", "outcome": "draft", "reason": "score_low", "branch": "pivotal/issue-7/main", "head": SHA}]}
        done = {"main": {"state": "OPEN", "status": publish.UPDATED if round_number else publish.OPENED, "pr": 1}}
        with mock.patch.object(publish, "send_one", return_value="sent"):
            return publish.send_fix_rounds({}, s, land, done, {})["main"]

    def test_the_run_before_the_last_round_sends_it_and_the_last_round_sends_none(self):
        self.assertEqual(self.words(hill.MAX_ROUNDS - 1), "sent")
        self.assertEqual(self.words(hill.MAX_ROUNDS), "max_rounds")
        self.assertEqual(self.words(hill.MAX_ROUNDS + 1), "max_rounds")


class StoppedWhyTest(unittest.TestCase):
    """A slice whose fix round was not sent because the rounds ran out is a draft with that reason in RESULT."""

    def test_a_round_that_ran_out_is_the_drafts_result_word(self):
        from engine import publish, report
        self.assertEqual(publish.stopped_whys({"main": "max_rounds"}), {"main": "max_rounds"})
        self.assertEqual(publish.stopped_whys({"main": "sent"}), {}, "a round that was sent stops nothing")
        entry = {"id": "main", "outcome": "draft", "reason": "score_low", "branch": "pivotal/issue-7/main", "head": SHA,
                 "built_by": "claude:anthropic/sonnet", "fell_back": []}
        done = {"main": {"status": "opened", "pr": 11, "state": "OPEN", "draft": True}}
        line = publish.result_line({"slices": [entry], "positions": []}, done, {}, stopped={"main": "max_rounds"})
        self.assertEqual(report.parse_result(line).why, "max_rounds")


class ReadyResultTest(unittest.TestCase):
    """R2: a met fix round's RESULT is `ready` and the report job reads it as ready: it names the round's reviewer."""

    def slice_entry(self, reviewed_by="pi:google/gemini-3.5-flash"):
        return {"id": "main", "outcome": "draft", "reason": "score_met", "candidate": False, "branch": "pivotal/issue-7/main",
                "head": SHA, "commits": [SHA], "built_by": "claude:anthropic/sonnet", "gate": "green", "fell_back": [], "score": 0.92,
                "target": "0.9", "reviewed_by": reviewed_by}

    def line(self, entry):
        from engine import publish
        land = {"slices": [entry], "positions": []}
        done = {"main": {"status": "updated", "pr": 11, "state": "OPEN", "draft": False}}
        return publish.result_line(land, done, {})

    def test_a_met_fix_round_names_its_reviewer_and_the_report_job_reads_it_as_ready(self):
        from engine import report
        data = json.loads(self.line(self.slice_entry()))
        self.assertEqual((data["status"], data["reviewed_by"]), ("ready", "pi:google/gemini-3.5-flash"))
        self.assertEqual(report.parse_result(self.line(self.slice_entry())).status, "ready")

    def test_a_met_round_that_was_kept_back_says_it_scored_below_the_best(self):
        from engine import publish, report
        land = {"slices": [self.slice_entry()], "positions": []}
        done = {"main": {"status": publish.KEPT, "pr": 11, "state": "OPEN", "draft": True}}
        line = publish.result_line(land, done, {})
        self.assertEqual((json.loads(line)["status"], json.loads(line)["why"]), ("draft", "below_best"))
        self.assertEqual(report.parse_result(line).why, "below_best")

    def test_a_met_round_without_a_reviewer_is_not_called_ready(self):
        self.assertNotEqual(json.loads(self.line(self.slice_entry(reviewed_by="")))["status"], "ready")


class LineBreakAndProseTest(unittest.TestCase):
    """Round 4: line breaks other than LF hide nothing (R13); a prose or fenced score line plans (R14); the near-miss keys (S2)."""

    BREAKS = ("\u2028", "\u2029", "\x0b", "\x0c", "\x85", "\x1c", "\x1d", "\x1e")

    def test_a_non_lf_line_break_that_hides_a_score_request_is_refused(self):
        for brk in self.BREAKS:
            text = brk.join(["Do it.", "---", "Pivotal request", "- line: build", "- score: cat s", "- target: 0.9\n"])
            with self.subTest(brk=repr(brk)), self.assertRaises(hill.HillError):
                hill.read_goal(text)

    def test_a_second_pair_hidden_after_a_non_lf_break_is_refused(self):
        for brk in ("\r",) + self.BREAKS:
            text = body() + "Note." + brk + "- score: rm -rf x" + brk + "- target: 0.1"
            with self.subTest(brk=repr(brk)), self.assertRaises(hill.HillError):
                hill.read_goal(text)
        with self.assertRaises(hill.HillError):  # the LF form was already refused
            hill.read_goal(body() + "Note.\n- score: rm -rf x\n- target: 0.1")

    def test_a_non_lf_break_with_no_score_request_still_plans(self):
        for brk in self.BREAKS:
            with self.subTest(brk=repr(brk)):
                self.assertIsNone(hill.read_goal(f"Do it.{brk}Then more.{brk}- target: mobile users\n"))
                self.assertEqual(hill.read_goal(body() + f"Then more.{brk}and more."), hill.Goal("python3 score.py", "0.9"))

    def test_a_prose_score_line_or_a_fenced_score_plans(self):
        for text in ("Improve the page.\nScore: the leaderboard shows ranks\n",
                     "Improve.\n```yaml\nscore: 0.5\n```\n",
                     "Improve.\n```yaml\nscore: 0.5\ntarget: 0.9\n```\n\n---\nPivotal request\n- line: build\n",
                     "Score: ranks\n\n" + body(score=None, target=None)):
            with self.subTest(text=text):
                self.assertIsNone(hill.read_goal(text))
        self.assertEqual(hill.read_goal("Score: ranks\n\n" + body()), hill.Goal("python3 score.py", "0.9"))

    def test_a_bulleted_lone_score_and_a_prose_score_directly_above_a_footer_are_refused(self):
        for text in ("Do it.\n- score: x\n", "Do it.\n+ Score: high priority\n",
                     "Do it.\nScore: ranks\n---\nPivotal request\n- line: build\n",
                     "Do it.\nscore: 0.5\n---\nPivotal request\n- score: a\n- target: 0.9\n"):
            with self.subTest(text=text), self.assertRaises(hill.HillError):
                hill.read_goal(text)

    def test_a_target_bullet_that_is_not_a_number_plans_under_a_score_footer(self):
        text = "Speed up.\n- target: mobile users\n\n" + body()
        self.assertEqual(hill.read_goal(text), hill.Goal("python3 score.py", "0.9"))

    def test_a_footer_key_one_edit_from_score_or_target_or_a_fullwidth_colon_is_refused(self):
        pre = "Do it.\n---\nPivotal request\n- line: build\n"
        for line in ("- scor: cat s", "- targt: 0.9", "- scores: cat s", "- Targets: 0.9", "- score\uff1a cat s", "- target\uff1a 0.9"):
            with self.subTest(line=line), self.assertRaises(hill.HillError):
                hill.read_goal(pre + line + "\n")
            with self.subTest(pair=line), self.assertRaises(hill.HillError):
                hill.read_goal(pre + "- score: a\n- target: 0.9\n" + line + "\n")
        self.assertIsNone(hill.read_goal(pre + "- note: ok\n- mockup: x.png\n"))

    def test_a_balanced_pair_of_hidden_score_lines_is_refused(self):
        """Round 5 R13: the count of score lines is equal under both splits, the lines are not."""
        pair = ["Score\u2028: a", "Score\u2028: b", "Note.\u2028- score: rm -rf x", "- target: 0.1"]
        for text in ("\n".join(pair) + "\n\n" + body(),
                     "Score\u2028: cat s\nNote.\u2028- score: rm -rf x\n\n" + body()):
            with self.subTest(text=text), self.assertRaises(hill.HillError):
                hill.read_goal(text)

    def test_ordinary_prose_after_a_non_lf_break_plans(self):
        """Round 5 R14: a prose Score: or Target: line after a break is text, with no footer."""
        for text in ("Fix X\x0bScore: 3 of 5 done", "Fix X\u2028Target: 0.5", "Fix X\u2028score: 0.5\n"):
            with self.subTest(text=text):
                self.assertIsNone(hill.read_goal(text))

    def test_an_earlier_footers_last_line_is_text_not_a_line_above_the_last_footer(self):
        """Round 5 S3: the line directly above the last footer is checked unless it is an earlier footer's own line."""
        first = "t\n---\nPivotal request\n- line: x\n- score: rm -rf earlier"
        self.assertEqual(hill.read_goal(first + "\n---\nPivotal request\n- score: a\n- target: 0.9\n"), hill.Goal("a", "0.9"))
        with self.assertRaises(hill.HillError):
            hill.read_goal("t\n- score: rm\n---\nPivotal request\n- score: a\n- target: 0.9\n")

    def test_transposed_footer_keys_are_refused_and_a_scope_or_store_key_is_too(self):
        """Round 5 S1 and S2: an adjacent transposition is a near miss; so is a key one edit from score (the accepted trade-off)."""
        pre = "Do it.\n---\nPivotal request\n- line: build\n"
        for line in ("- socre: cat s", "- targte: 0.9", "- scope: web only", "- store: main"):
            with self.subTest(line=line), self.assertRaises(hill.HillError):
                hill.read_goal(pre + line + "\n")


if __name__ == "__main__":
    unittest.main()
