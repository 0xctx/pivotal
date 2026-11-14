"""piv land, fix, stop and resume: the comment commands and the level switch, against the fake gh.

Every call goes through PIV_GH, which is always the fake (test/fakegh.py, strict about flags), so nothing reaches
GitHub. A refusal must make no write call: the tests check the write calls, not only the exit status.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import fakegh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))

from piv import cli, init  # noqa: E402

OWNER = "0xctx"
REPO = "0xctx/piv-sandbox"
OTHER = "0xctx/other-app"
LEVEL = f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}"
PRIOR = f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}"


def ok_json(value) -> dict:
    return fakegh.ok(json.dumps(value))


def variable(value: str | None) -> dict:
    return fakegh.not_found() if value is None else ok_json({"value": value})


def is_write(call: dict) -> bool:
    """A write: a comment (POST, PUT, PATCH or DELETE through ``gh api -X``), or a variable set or delete."""
    args = call["args"]
    if "-X" in args and args[args.index("-X") + 1] != "GET":
        return True
    return args[:2] in (["variable", "set"], ["variable", "delete"])


def comment_targets(calls: list[dict]) -> list[tuple[str, str]]:
    """(issue number, body) for each comment posted, in order."""
    posted = []
    for call in calls:
        if "-X" in call["args"] and call["args"][call["args"].index("-X") + 1] == "POST":
            path = next(arg for arg in call["args"] if arg.startswith("repos/"))
            number = path.split("/issues/")[1].split("/")[0]
            posted.append((number, json.loads(call["stdin"])["body"]))
    return posted


class ControlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "bin")
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.gh.route("run list", fakegh.ok("[]"))  # nothing in flight unless a test says otherwise

    def tearDown(self):
        self._tmp.cleanup()

    def piv(self, *argv: str) -> int:
        with contextlib.redirect_stderr(io.StringIO()):  # argparse writes its own usage errors to stderr
            return cli.main(list(argv), self.gh.env(), self.out, self.err)

    def writes(self) -> list[list[str]]:
        return [call["args"] for call in self.gh.calls() if is_write(call)]

    def managed(self, *repos: str) -> None:
        self.gh.route("api user", ok_json({"login": OWNER}))
        items = [{"full_name": name} for name in repos]
        self.gh.route("search/repositories", ok_json({"total_count": len(items), "items": items}))

    def ready(self, *rows: dict) -> None:
        self.gh.route("labels=pivotal%3Aready", fakegh.ok(json.dumps(list(rows))))

    def issue(self, number: int, *, state: str = "open", pull: bool = False) -> None:
        body = {"number": number, "state": state}
        if pull:
            body["pull_request"] = {"url": "https://api.github.com/x"}
        self.gh.route(f"api repos/{REPO}/issues/{number}", ok_json(body), exact=True)

    def in_flight(self, status: str, *titles: str) -> None:
        rows = [{"displayTitle": title, "workflowName": "pivotal"} for title in titles]
        self.gh.route(f"--status {status}", fakegh.ok(json.dumps(rows)), first=True)


class LandTest(ControlTest):
    def test_land_on_a_named_issue_comments_the_land_command(self):
        self.issue(12)
        self.gh.route("issues/12/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("land", REPO, "#12"), 0)
        self.assertEqual(comment_targets(self.gh.calls()), [("12", "/pivotal land")])
        self.assertIn(f"{REPO}#12: commented /pivotal land", self.out.getvalue())

    def test_a_plain_number_names_the_same_issue(self):
        self.issue(12)
        self.gh.route("issues/12/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("land", REPO, "12"), 0)
        self.assertEqual(comment_targets(self.gh.calls()), [("12", "/pivotal land")])

    def test_land_with_no_issue_comments_on_the_one_ready_issue(self):
        # The user-visible check: `piv land my-app` leaves /pivotal land on the one issue that is ready.
        self.ready({"number": 7})
        self.gh.route("issues/7/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("land", REPO), 0)
        self.assertEqual(comment_targets(self.gh.calls()), [("7", "/pivotal land")])

    def test_land_with_no_issue_refuses_when_several_are_ready(self):
        self.ready({"number": 7}, {"number": 9})
        self.assertEqual(self.piv("land", REPO), 2)
        self.assertEqual(self.writes(), [])
        self.assertIn("#7, #9", self.err.getvalue())

    def test_land_with_no_issue_refuses_when_none_is_ready(self):
        self.ready()
        self.assertEqual(self.piv("land", REPO), 1)
        self.assertEqual(self.writes(), [])

    def test_land_all_comments_on_each_ready_issue_and_skips_pull_requests(self):
        self.ready({"number": 3}, {"number": 5, "pull_request": {}})
        self.gh.route("issues/3/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("land", REPO, "--all"), 0)
        self.assertEqual(comment_targets(self.gh.calls()), [("3", "/pivotal land")])

    def test_land_all_with_nothing_ready_says_so_and_writes_nothing(self):
        self.ready()
        self.assertEqual(self.piv("land", REPO, "--all"), 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("no issue is ready", self.out.getvalue())

    def test_land_all_reads_every_page_of_ready_issues(self):
        first = [{"number": n} for n in range(1, 101)]
        self.gh.route("labels=pivotal%3Aready&per_page=100&page=2", ok_json([{"number": 101}]))
        self.gh.route("labels=pivotal%3Aready&per_page=100&page=1", fakegh.ok(json.dumps(first)))
        self.gh.route("/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("land", REPO, "--all"), 0)
        self.assertEqual(len(comment_targets(self.gh.calls())), 101)

    def test_land_refuses_a_closed_issue_a_pull_request_and_a_missing_one_before_any_write(self):
        cases = [
            ("closed", {"state": "closed"}, 1),
            ("pull", {"pull": True}, 2),
        ]
        for label, kwargs, code in cases:
            with self.subTest(label):
                self.gh = fakegh.FakeGh(Path(self._tmp.name) / label)
                self.issue(12, **kwargs)
                self.gh.route("issues/12/comments", fakegh.ok("{}"))
                self.assertEqual(self.piv("land", REPO, "#12"), code)
                self.assertEqual(self.writes(), [])
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "missing")
        self.gh.route(f"api repos/{REPO}/issues/12", fakegh.not_found(), exact=True)
        self.assertEqual(self.piv("land", REPO, "#12"), 1)
        self.assertEqual(self.writes(), [])

    def test_land_usage_errors_write_nothing_and_call_nothing(self):
        for argv in (
            ("land", REPO, "#1", "--all"),
            ("land", REPO, "abc"),
            ("land", REPO, "0"),
            ("land", REPO, "#1234567890"),
            ("land", "not-a-repo", "#1"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.piv(*argv), 2)
        self.assertEqual(self.gh.calls(), [])

    def test_a_comment_that_fails_is_named_and_the_others_still_post(self):
        self.ready({"number": 3}, {"number": 4})
        self.gh.route("issues/3/comments", fakegh.fail("gh: Server Error (HTTP 500)\n"))
        self.gh.route("issues/4/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("land", REPO, "--all"), 1)
        self.assertEqual([number for number, _ in comment_targets(self.gh.calls())], ["3", "4"])
        self.assertIn("#3", self.err.getvalue())
        self.assertNotIn("#4", self.err.getvalue())


class FixTest(ControlTest):
    def test_fix_comments_the_fix_command_on_an_open_issue(self):
        self.issue(12)
        self.gh.route("issues/12/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("fix", REPO, "#12"), 0)
        self.assertEqual(comment_targets(self.gh.calls()), [("12", "/pivotal fix")])

    def test_fix_refuses_a_pull_request_and_a_closed_issue(self):
        self.issue(12, pull=True)
        self.gh.route("issues/12/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("fix", REPO, "#12"), 2)
        self.assertEqual(self.writes(), [])
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "closed")
        self.issue(12, state="closed")
        self.gh.route("issues/12/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("fix", REPO, "#12"), 1)
        self.assertEqual(self.writes(), [])


class StopTest(ControlTest):
    def test_stop_comments_the_stop_command_on_each_issue_in_flight_once(self):
        self.in_flight("queued", "pivotal-issue-4")
        self.in_flight("in_progress", "pivotal-issue-4", "pivotal-issue-9", "unrelated title")
        for state in ("waiting", "requested", "pending"):
            self.in_flight(state)
        self.gh.route("issues/4/comments", fakegh.ok("{}"))
        self.gh.route("issues/9/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("stop", REPO), 0)
        self.assertEqual(comment_targets(self.gh.calls()), [("4", "/pivotal stop"), ("9", "/pivotal stop")])
        listed = [call["args"][call["args"].index("--status") + 1] for call in self.gh.calls() if "--status" in call["args"]]
        self.assertEqual(listed, ["queued", "in_progress", "waiting", "requested", "pending"])

    def test_stop_with_nothing_in_flight_writes_nothing(self):
        self.assertEqual(self.piv("stop", REPO), 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("nothing to stop", self.out.getvalue())

    def test_stop_needs_one_repo_or_all_and_refuses_both(self):
        self.assertEqual(self.piv("stop"), 2)
        self.assertEqual(self.piv("stop", REPO, "--all"), 2)
        self.assertEqual(self.piv("stop", REPO, "--repo", OTHER), 2)
        self.assertEqual(self.gh.calls(), [])

    def test_stop_all_sets_watch_everywhere_and_records_each_prior_level(self):
        # The acceptance item: stop --all sets watch in every managed repo, and each prior level is recorded first.
        self.managed(REPO, OTHER)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("land"))
        self.gh.route(f"repos/{OTHER}/actions/variables/{init.VAR_LEVEL}", variable(None))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable(None))
        self.gh.route(f"repos/{OTHER}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable(None))
        self.gh.route("variable set", fakegh.ok(""))
        self.assertEqual(self.piv("stop", "--all"), 0)
        sets = [
            (call["args"][2], call["args"][call["args"].index("--repo") + 1], call["stdin"])
            for call in self.gh.calls()
            if call["args"][:2] == ["variable", "set"]
        ]
        # Each repo: the record of its prior level, then watch. An unset level is recorded as the default it runs at.
        expected = {
            (init.VAR_PRIOR_LEVEL, REPO, "land"),
            (init.VAR_LEVEL, REPO, "watch"),
            (init.VAR_PRIOR_LEVEL, OTHER, "prepare"),
            (init.VAR_LEVEL, OTHER, "watch"),
        }
        self.assertEqual(set(sets), expected)
        self.assertEqual(len(sets), len(expected))
        for repo in (REPO, OTHER):
            names = [name for name, target, _ in sets if target == repo]
            self.assertEqual(names, [init.VAR_PRIOR_LEVEL, init.VAR_LEVEL], "the record is written before the level")

    def test_stop_all_refuses_an_invalid_level_before_any_write(self):
        self.managed(REPO, OTHER)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("Land"))
        self.gh.route(f"repos/{OTHER}/actions/variables/{init.VAR_LEVEL}", variable("ask"))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable(None))
        self.gh.route(f"repos/{OTHER}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable(None))
        self.gh.route("variable set", fakegh.ok(""))
        self.assertEqual(self.piv("stop", "--all"), 2)
        self.assertEqual(self.writes(), [], "a bad level in one repo must stop the whole stop before any write")

    def test_stop_all_on_a_repo_already_watch_keeps_its_record(self):
        self.managed(REPO)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("watch"))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable("land"))
        self.gh.route("variable set", fakegh.ok(""))
        self.assertEqual(self.piv("stop", "--all"), 0)
        self.assertEqual(self.writes(), [], "a second stop must not overwrite the level resume restores")
        self.assertIn("piv resume restores land", self.out.getvalue())

    def test_stop_all_on_a_repo_already_watch_with_no_record_writes_nothing(self):
        self.managed(REPO)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("watch"))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable(None))
        self.assertEqual(self.piv("stop", "--all"), 0)
        self.assertEqual(self.writes(), [])

    def test_stop_all_refuses_when_no_repo_is_managed(self):
        self.managed()
        self.assertEqual(self.piv("stop", "--all"), 1)
        self.assertEqual(self.writes(), [])

    def test_stop_all_reads_a_repo_the_search_missed_when_named(self):
        self.managed(REPO)
        self.gh.route(f"repos/{OTHER}/actions/variables/{init.VAR_LEVEL}", variable("prepare"))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("prepare"))
        self.gh.route(f"actions/variables/{init.VAR_PRIOR_LEVEL}", variable(None))
        self.gh.route("variable set", fakegh.ok(""))
        self.assertEqual(self.piv("stop", "--all", "--repo", OTHER), 0)
        targets = {call["args"][call["args"].index("--repo") + 1] for call in self.gh.calls() if call["args"][:2] == ["variable", "set"]}
        self.assertEqual(targets, {REPO, OTHER})

    def test_stop_all_also_cancels_the_runs_it_finds_after_the_watch(self):
        self.managed(REPO)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("land"))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable(None))
        self.gh.route("variable set", fakegh.ok(""))
        self.in_flight("in_progress", "pivotal-issue-6")
        self.gh.route("issues/6/comments", fakegh.ok("{}"))
        self.assertEqual(self.piv("stop", "--all"), 0)
        self.assertEqual(comment_targets(self.gh.calls()), [("6", "/pivotal stop")])
        calls = self.gh.calls()
        last_level_write = max(i for i, c in enumerate(calls) if c["args"][:2] == ["variable", "set"])
        first_comment = next(i for i, c in enumerate(calls) if "POST" in c["args"])
        self.assertLess(last_level_write, first_comment, "the level is watch before any run is stopped")

    def test_stop_all_reports_a_repo_it_could_not_finish_and_still_does_the_others(self):
        self.managed(REPO, OTHER)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("land"))
        self.gh.route(f"repos/{OTHER}/actions/variables/{init.VAR_LEVEL}", variable("land"))
        self.gh.route(f"actions/variables/{init.VAR_PRIOR_LEVEL}", variable(None))
        self.gh.route(f"variable set {init.VAR_PRIOR_LEVEL} --repo {REPO}", fakegh.fail("gh: Server Error (HTTP 500)\n"))
        self.gh.route("variable set", fakegh.ok(""))
        self.assertEqual(self.piv("stop", "--all"), 1)
        sets = [call["args"][call["args"].index("--repo") + 1] for call in self.gh.calls() if call["args"][:2] == ["variable", "set"]]
        self.assertIn(OTHER, sets, "the other repo still gets its watch")
        self.assertIn(REPO, self.err.getvalue())


class ResumeTest(ControlTest):
    def test_resume_restores_the_recorded_level_and_clears_the_record(self):
        self.managed(REPO)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("watch"))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable("land"))
        self.gh.route("variable set", fakegh.ok(""))
        self.gh.route("variable delete", fakegh.ok(""))
        self.assertEqual(self.piv("resume"), 0)
        writes = [call for call in self.gh.calls() if is_write(call)]
        self.assertEqual(
            [call["args"][:3] for call in writes],
            [["variable", "set", init.VAR_LEVEL], ["variable", "delete", init.VAR_PRIOR_LEVEL]],
            "the level is restored before the record is removed",
        )
        self.assertEqual(writes[0]["stdin"], "land")

    def test_resume_keeps_a_level_changed_since_the_stop_and_clears_the_record(self):
        self.managed(REPO)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("ask"))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable("land"))
        self.gh.route("variable set", fakegh.ok(""))
        self.gh.route("variable delete", fakegh.ok(""))
        self.assertEqual(self.piv("resume"), 0)
        names = [call["args"][:2] for call in self.gh.calls() if is_write(call)]
        self.assertEqual(names, [["variable", "delete"]], "the level set after the stop is not overwritten")

    def test_resume_without_a_record_writes_nothing(self):
        self.managed(REPO)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable(None))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("watch"))
        self.assertEqual(self.piv("resume"), 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("not stopped by piv stop", self.out.getvalue())

    def test_resume_refuses_a_record_that_is_not_a_level_before_any_write(self):
        self.managed(REPO, OTHER)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable("land"))
        self.gh.route(f"repos/{OTHER}/actions/variables/{init.VAR_PRIOR_LEVEL}", variable("maybe"))
        self.gh.route(f"actions/variables/{init.VAR_LEVEL}", variable("watch"))
        self.assertEqual(self.piv("resume"), 2)
        self.assertEqual(self.writes(), [])

    def test_stop_all_then_resume_round_trip_restores_each_repo(self):
        # The acceptance item end to end: the reads of the second command see what the first one wrote.
        self.managed(REPO)
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_LEVEL}", variable("land"), variable("watch"))
        self.gh.route(f"repos/{REPO}/actions/variables/{init.VAR_PRIOR_LEVEL}", fakegh.not_found(), variable("land"))
        self.gh.route("variable set", fakegh.ok(""))
        self.gh.route("variable delete", fakegh.ok(""))
        self.assertEqual(self.piv("stop", "--all"), 0)
        self.assertEqual(self.piv("resume"), 0)
        restored = [
            call["stdin"] for call in self.gh.calls()
            if call["args"][:2] == ["variable", "set"] and call["args"][2] == init.VAR_LEVEL and call["stdin"] == "land"
        ]
        self.assertEqual(restored, ["land"])
        self.assertIn(["variable", "delete", init.VAR_PRIOR_LEVEL, "--repo", REPO], [call["args"] for call in self.gh.calls()])


class RecordTest(unittest.TestCase):
    def test_the_prior_level_is_one_of_the_variables_remove_deletes(self):
        self.assertIn(init.VAR_PRIOR_LEVEL, init.VARIABLES)
        self.assertEqual(init.VAR_PRIOR_LEVEL, "PIVOTAL_PRIOR_LEVEL")


if __name__ == "__main__":
    unittest.main()
