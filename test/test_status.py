"""piv now and piv digest (F7): read through a fake gh, and never a write.

Every test runs the real code against the fake (test/fakegh.py), which refuses any flag the real gh lacks. The
clock is fixed at NOW, so the 15-minute watchdog and "today" are decided by the test, not by the wall clock.
"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakegh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))

from piv import cli, status  # noqa: E402

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
TODAY = "2026-10-27"
LAUNCHER = ROOT / "gh-piv" / "gh-piv"


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(minutes: int) -> str:
    return stamp(NOW - timedelta(minutes=minutes))


def issue(number: int, *, labels=(), updated=None, created=None, closed=None) -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "url": f"https://github.com/t/{number}",
        "createdAt": created or ago(600),
        "closedAt": closed,
        "updatedAt": updated or ago(600),
        "labels": [{"name": name} for name in labels],
    }


def run(number: int, *, workflow="Pivotal", state="completed", conclusion="success", created=None,
        title=None) -> dict:
    return {
        "databaseId": number,
        "displayTitle": title or f"Run {number}",
        "url": f"https://github.com/t/actions/runs/{number}",
        "workflowName": workflow,
        "conclusion": conclusion if state == "completed" else "",
        "status": state,
        "createdAt": created or ago(5),
    }


def pr(number: int, *, draft=False, head="pivotal/issue-1/stack") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/t/pull/{number}",
        "isDraft": draft,
        "headRefName": head,
    }


def ok_json(value) -> dict:
    return fakegh.ok(json.dumps(value))


def route_repo(gh: fakegh.FakeGh, name: str, *, building=(), ready=(), landed=(), blocked=(), pivotal=(),
               new=(), prs=(), main=(), in_progress=(), queued=(), pivotal_runs=(), branch="main") -> None:
    """Answer every read a good repo makes. The matches end in a flag, so one repo's routes never match another's."""
    routes = [
        (f"api repos/{name}", ok_json({"default_branch": branch})),
        (f"-R {name} --label pivotal:building --state open", ok_json(list(building))),
        (f"-R {name} --label pivotal:ready --state open", ok_json(list(ready))),
        (f"-R {name} --label pivotal:landed --state closed", ok_json(list(landed))),
        (f"-R {name} --label pivotal:blocked --state open", ok_json(list(blocked))),
        (f"-R {name} --label pivotal --state open", ok_json(list(pivotal))),
        (f"-R {name} --state open --search created:", ok_json(list(new))),
        (f"pr list -R {name} --state open", ok_json(list(prs))),
        (f"-R {name} --branch {branch}", ok_json(list(main))),
        (f"-R {name} --status in_progress", ok_json(list(in_progress))),
        (f"-R {name} --status queued", ok_json(list(queued))),
        (f"-R {name} --workflow pivotal --json", ok_json(list(pivotal_runs))),
    ]
    for match, answer in routes:
        gh.route(match, answer)


def route_unreadable(gh: fakegh.FakeGh, name: str) -> None:
    gh.route(f"api repos/{name}", fakegh.not_found())
    gh.route(f"-R {name} ", fakegh.not_found())


class StatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "bin")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), self.gh.env(), out, err, clock=lambda: NOW)
        return code, out.getvalue(), err.getvalue()

    def now(self, *repos: str) -> tuple[int, str, str]:
        args = [part for name in repos for part in ("--repo", name)]
        return self.run_cli("now", *args)

    def digest(self, *args: str, repos: tuple[str, ...] = ("a/x",)) -> tuple[int, str, str]:
        named = [part for name in repos for part in ("--repo", name)]
        return self.run_cli("digest", *named, *args)

    def assert_read_only(self):
        """Every call the commands made is a read: a list, a search, or a GET of a repo."""
        for call in self.gh.calls():
            args = call["args"]
            self.assertEqual(call["stdin"], "", f"{args} sent stdin")
            if args[0] == "api":
                self.assertNotIn("-X", args, args)
                self.assertNotIn("--input", args, args)
                self.assertTrue(args[1].startswith("repos/"), args)
            else:
                self.assertIn(args[0], {"issue", "pr", "run", "search"}, args)
                self.assertIn(args[1], {"list", "repos"}, args)


class NowTest(StatusTest):
    def test_counts_agents_ready_and_landed_today_across_repos(self):
        route_repo(self.gh, "a/x", in_progress=[run(1, state="in_progress"), run(2, state="in_progress")],
                   ready=[issue(3)], landed=[issue(4, closed=ago(30))])
        route_repo(self.gh, "a/y", in_progress=[run(5, state="in_progress")], landed=[issue(6, closed=ago(60))])
        code, out, err = self.now("a/x", "a/y")
        self.assertEqual(code, 0, err)
        headline = "⚡ 3 agents working for you right now · 1 ready for you · 2 landed today (UTC)"
        self.assertEqual(out.splitlines(), [headline, "2 repos checked, 0 unreadable", status.SEARCH_NOTE])
        self.assert_read_only()

    def test_one_unreadable_repo_is_named_counted_and_exits_non_zero(self):
        route_repo(self.gh, "a/x", in_progress=[run(1, state="in_progress")], ready=[issue(2)])
        route_unreadable(self.gh, "a/broken")
        code, out, err = self.now("a/x", "a/broken")
        self.assertEqual(code, 1)
        self.assertIn("1 of 2 repos unreadable:", out)
        self.assertIn("a/broken:", out)
        self.assertIn("⚡ 1 agent working for you right now · 1 ready for you · 0 landed today (UTC)", out)
        self.assertEqual(err, "")

    def test_a_repo_that_fails_on_a_later_call_is_unreadable_as_a_whole(self):
        # The failing route must come before the good one, because the fake answers with the first match.
        self.gh.route("-R a/y --label pivotal:ready", fakegh.fail("gh: HTTP 502 (HTTP 502)\n"))
        route_repo(self.gh, "a/x", in_progress=[run(1, state="in_progress")])
        route_repo(self.gh, "a/y", in_progress=[run(2, state="in_progress")])
        code, out, _ = self.now("a/x", "a/y")
        self.assertEqual(code, 1)
        self.assertIn("1 of 2 repos unreadable:", out)
        # a/y's building issue was read, but a/y is unreadable, so it is not counted
        self.assertIn("⚡ 1 agent working for you right now", out)

    def test_a_malformed_answer_makes_the_repo_unreadable_not_quiet(self):
        route_repo(self.gh, "a/x", in_progress=[run(1, state="in_progress")])
        self.gh.route("-R a/bad --status in_progress", fakegh.ok("not json"))
        self.gh.route("-R a/bad --label pivotal:ready", fakegh.ok("[]"))
        self.gh.route("-R a/bad --label pivotal:landed", fakegh.ok("[]"))
        code, out, _ = self.now("a/x", "a/bad")
        self.assertEqual(code, 1)
        self.assertIn("1 of 2 repos unreadable:", out)
        self.assertIn("a/bad: gh answered with something other than JSON", out)

    def test_a_list_at_the_cap_is_refused_as_unreadable_not_counted_short(self):
        full = [run(n, state="in_progress") for n in range(1, status.LIMIT + 1)]
        route_repo(self.gh, "a/x", in_progress=full)
        code, out, _ = self.now("a/x")
        self.assertEqual(code, 1)
        self.assertIn("1 of 1 repos unreadable:", out)
        self.assertIn(f"a/x: run lists reached {status.LIMIT} items", out)

    def test_the_search_for_an_owner_is_read_and_merged_with_named_repos(self):
        self.gh.route("search repos --owner a --topic pivotal", ok_json([{"fullName": "a/x"}, {"fullName": "a/z"}]))
        route_repo(self.gh, "a/x", in_progress=[run(1, state="in_progress")])
        route_repo(self.gh, "a/y", in_progress=[run(2, state="in_progress")])
        route_repo(self.gh, "a/z", in_progress=[run(3, state="in_progress")])
        code, out, _ = self.run_cli("now", "--owner", "a", "--repo", "a/y", "--repo", "a/x")
        self.assertEqual(code, 0)
        self.assertIn("⚡ 3 agents working", out)
        read = {call["args"][3] for call in self.gh.calls() if call["args"][:2] == ["run", "list"]}
        self.assertEqual(read, {"a/x", "a/y", "a/z"}, "a repo named twice is read once")

    def test_no_repo_named_is_a_usage_error_with_no_gh_call(self):
        code, _, err = self.run_cli("now")
        self.assertEqual(code, 2)
        self.assertIn("name the repos to read", err)
        self.assertEqual(self.gh.calls(), [])

    def test_a_malformed_repo_is_refused_before_any_gh_call(self):
        code, _, err = self.now("not-a-repo")
        self.assertEqual(code, 2)
        self.assertIn("--repo must be OWNER/NAME", err)
        self.assertEqual(self.gh.calls(), [])

    def test_no_managed_repo_found_exits_1_and_says_why(self):
        self.gh.route("search repos --owner a --topic pivotal", ok_json([]))
        code, _, err = self.run_cli("now", "--owner", "a")
        self.assertEqual(code, 1)
        self.assertIn("no managed repos: a has none with the pivotal topic", err)

    def test_a_failed_repo_search_exits_1_and_reads_nothing(self):
        self.gh.route("search repos --owner a --topic pivotal", fakegh.fail("gh: rate limit (HTTP 403)\n"))
        code, out, err = self.run_cli("now", "--owner", "a")
        self.assertEqual(code, 1)
        self.assertIn("gh search repos", err)
        self.assertEqual(out, "")
        self.assertEqual(len(self.gh.calls()), 1)

    def test_landed_yesterday_is_not_landed_today(self):
        route_repo(self.gh, "a/x", landed=[issue(1, closed=ago(60 * 13)), issue(2, closed=ago(10))])
        code, out, _ = self.now("a/x")
        self.assertEqual(code, 0)
        self.assertIn("1 landed today", out)

    def test_agents_working_counts_in_progress_pivotal_runs_not_labels(self):
        # Fixture from R1:
        # a pivotal:building label with no run gives 0;
        # two in-progress pivotal runs give 2;
        # an in-progress run of an unrelated workflow (CI) gives 0.
        route_repo(self.gh, "a/orphaned", building=[issue(1, labels=["pivotal:building"])])
        route_repo(self.gh, "a/active", in_progress=[
            run(10, workflow="Pivotal", state="in_progress"),
            run(11, workflow="Pivotal", state="in_progress"),
        ])
        route_repo(self.gh, "a/unrelated", in_progress=[
            run(20, workflow="CI", state="in_progress"),
        ])
        code, out, _ = self.now("a/orphaned")
        self.assertEqual(code, 0)
        self.assertIn("⚡ 0 agents working", out)

        code, out, _ = self.now("a/active")
        self.assertEqual(code, 0)
        self.assertIn("⚡ 2 agents working", out)

        code, out, _ = self.now("a/unrelated")
        self.assertEqual(code, 0)
        self.assertIn("⚡ 0 agents working", out)

    def test_search_backed_reads_say_they_can_lag(self):
        route_repo(self.gh, "a/x", landed=[issue(1, closed=ago(10))])
        code, out, _ = self.now("a/x")
        self.assertEqual(code, 0)
        self.assertIn("can lag", out)

    def test_repos_deduped_casefolded(self):
        route_repo(self.gh, "a/x", ready=[issue(1)])
        code, out, _ = self.now("a/x", "A/X")
        self.assertEqual(code, 0)
        calls = [c["args"] for c in self.gh.calls() if "-R" in c["args"]]
        repos_called = {args[args.index("-R") + 1].casefold() for args in calls}
        self.assertEqual(repos_called, {"a/x"})
        self.assertIn("1 repos checked, 0 unreadable", out)

    def test_now_says_utc_and_prints_unreadable_first(self):
        route_repo(self.gh, "a/x", in_progress=[run(1, workflow="Pivotal", state="in_progress")])
        route_unreadable(self.gh, "a/broken")
        code, out, _ = self.now("a/x", "a/broken")
        self.assertEqual(code, 1)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "1 of 2 repos unreadable:")
        self.assertTrue(lines[1].startswith("  a/broken:"))
        self.assertIn("landed today (UTC)", lines[2])


class DigestTest(StatusTest):
    def test_a_pivotal_issue_with_no_run_after_15_minutes_is_flagged(self):
        route_repo(self.gh, "a/x", pivotal=[
            issue(1, labels=["pivotal"], updated=ago(16)),
            issue(2, labels=["pivotal"], updated=ago(5)),
            issue(3, labels=["pivotal", "pivotal:building"], updated=ago(60)),
        ])
        code, out, _ = self.digest()
        self.assertEqual(code, 0)
        self.assertIn("no run after 15 min (1)", out)
        self.assertIn("a/x #1 Issue 1", out)
        self.assertNotIn("a/x #2", out)
        self.assertNotIn("a/x #3", out)

    def test_a_trigger_label_alone_does_not_count_as_a_run(self):
        route_repo(self.gh, "a/x", pivotal=[issue(1, labels=["pivotal", "pivotal:build"], updated=ago(40))])
        code, out, _ = self.digest("--json")
        self.assertEqual(json.loads(out)["watchdog"]["no_run"][0]["number"], 1)

    def test_building_with_no_active_run_is_flagged(self):
        route_repo(self.gh, "a/x", building=[issue(4, labels=["pivotal:building"])])
        _, out, _ = self.digest("--json")
        flagged = json.loads(out)["watchdog"]["building_without_run"]
        self.assertEqual([item["number"] for item in flagged], [4])
        self.assertIn("no pivotal run in progress or queued", flagged[0]["detail"])

    def test_building_with_an_in_progress_run_is_not_flagged(self):
        route_repo(self.gh, "a/x", building=[issue(4, labels=["pivotal:building"])],
                   in_progress=[run(90, state="in_progress", title="pivotal-issue-4")])
        _, out, _ = self.digest("--json")
        self.assertEqual(json.loads(out)["watchdog"]["building_without_run"], [])

    def test_a_run_queued_over_15_minutes_is_flagged_and_a_new_one_is_not(self):
        queued = [run(70, state="queued", created=ago(20)), run(71, state="queued", created=ago(5))]
        route_repo(self.gh, "a/x", queued=queued)
        _, out, _ = self.digest("--json")
        stuck = json.loads(out)["watchdog"]["queued_too_long"]
        found = [(item["kind"], item["number"], item["detail"]) for item in stuck]
        self.assertEqual(found, [("run", 70, "queued 20 min")])

    def test_red_main_is_the_newest_completed_run_of_each_workflow(self):
        route_repo(self.gh, "a/x", main=[
            run(10, workflow="Pivotal", conclusion="failure"),       # newest Pivotal: red
            run(9, workflow="CI", conclusion="success"),             # newest CI: green
            run(8, workflow="CI", conclusion="failure"),             # older CI: ignored
            run(7, workflow="Lint", state="in_progress"),            # not completed: skipped
            run(6, workflow="Lint", conclusion="timed_out"),         # newest completed Lint: red
        ])
        _, out, _ = self.digest("--json")
        red = json.loads(out)["red_mains"]
        self.assertEqual(sorted(item["number"] for item in red), [6, 10])
        self.assertIn("Pivotal on main concluded failure", {item["detail"] for item in red})

    def test_a_cancelled_run_on_main_is_not_red(self):
        route_repo(self.gh, "a/x", main=[run(3, conclusion="cancelled")])
        _, out, _ = self.digest("--json")
        self.assertEqual(json.loads(out)["red_mains"], [])

    def test_ready_prs_are_open_non_draft_pivotal_branches(self):
        route_repo(self.gh, "a/x", prs=[pr(1), pr(2, draft=True), pr(3, head="feature/x")])
        _, out, _ = self.digest("--json")
        self.assertEqual([item["number"] for item in json.loads(out)["ready_prs"]], [1])

    def test_new_issues_are_those_opened_within_the_window(self):
        route_repo(self.gh, "a/x", new=[issue(1, created=ago(60)), issue(2, created=ago(60 * 30))])
        _, out, _ = self.digest("--json")
        self.assertEqual([item["number"] for item in json.loads(out)["new_issues"]], [1])

    def test_since_narrows_the_window_for_new_issues(self):
        route_repo(self.gh, "a/x", new=[issue(1, created=ago(120))])
        _, out, _ = self.digest("--json", "--since", "1")
        self.assertEqual(json.loads(out)["new_issues"], [])

    def test_since_below_one_hour_is_refused_before_any_gh_call(self):
        code, _, err = self.digest("--since", "0")
        self.assertEqual(code, 2)
        self.assertIn("--since must be between 1 and", err)
        self.assertEqual(self.gh.calls(), [])

    def test_blocked_items_are_open_pivotal_blocked_issues(self):
        route_repo(self.gh, "a/x", blocked=[issue(8, labels=["pivotal:blocked"])])
        _, out, _ = self.digest("--json")
        self.assertEqual([item["number"] for item in json.loads(out)["blocked"]], [8])

    def test_the_json_reports_repos_checked_and_repos_unreadable_with_one_unreadable_repo(self):
        route_repo(self.gh, "a/x")
        route_repo(self.gh, "a/y")
        route_unreadable(self.gh, "a/broken")
        code, out, _ = self.digest("--json", repos=("a/x", "a/broken", "a/y"))
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["repos_checked"], 3)
        self.assertEqual(report["repos_unreadable"], 1)
        self.assertEqual([bad["repo"] for bad in report["unreadable"]], ["a/broken"])

    def test_the_text_names_the_unreadable_repo_and_never_says_quiet(self):
        route_repo(self.gh, "a/x")
        route_unreadable(self.gh, "a/broken")
        code, out, _ = self.run_cli("digest", "--repo", "a/x", "--repo", "a/broken")
        self.assertEqual(code, 1)
        self.assertIn("digest: 2 repos checked, 1 unreadable", out)
        self.assertIn("1 of 2 repos unreadable:", out)
        self.assertIn("a/broken:", out)
        self.assertNotIn("quiet", out.lower())

    def test_a_clean_digest_exits_0_and_says_each_section_is_empty(self):
        route_repo(self.gh, "a/x")
        code, out, _ = self.run_cli("digest", "--repo", "a/x")
        self.assertEqual(code, 0)
        self.assertIn("digest: 1 repos checked, 0 unreadable", out)
        self.assertIn("no run after 15 min (0)", out)
        self.assertNotIn("unreadable:", out)

    def test_a_failed_read_of_a_repos_main_makes_that_repo_unreadable_and_drops_its_items(self):
        # The failing route must come before the good one, because the fake answers with the first match.
        self.gh.route("-R a/x --branch main", fakegh.fail("gh: HTTP 500 (HTTP 500)\n"))
        route_repo(self.gh, "a/x", main=[run(1, conclusion="failure")], prs=[pr(5)])
        route_repo(self.gh, "a/y", prs=[pr(6)])
        code, out, _ = self.digest("--json", repos=("a/x", "a/y"))
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["repos_unreadable"], 1)
        self.assertEqual([bad["repo"] for bad in report["unreadable"]], ["a/x"])
        self.assertEqual(report["red_mains"], [], "a/x is unreadable, so its red main is not reported")
        self.assertEqual([item["number"] for item in report["ready_prs"]], [6], "only a/y's ready PR is reported")

    def test_an_item_gh_cannot_read_makes_the_repo_unreadable(self):
        route_repo(self.gh, "a/x", pivotal=[{"number": 1, "labels": [], "updatedAt": "not a time", "url": "u",
                                              "title": "t", "createdAt": ago(1)}])
        code, out, _ = self.digest("--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["repos_unreadable"], 1)
        self.assertIn("a/x", report["unreadable"][0]["repo"])
        self.assertIn("updatedAt that cannot be read", report["unreadable"][0]["reason"])

    def test_a_field_with_the_wrong_type_is_unreadable(self):
        bad = {"number": 1, "title": "t", "url": "u", "isDraft": "no", "headRefName": "pivotal/x"}
        route_repo(self.gh, "a/x", prs=[bad])
        code, out, _ = self.digest("--json")
        self.assertEqual(code, 1)
        self.assertIn("a/x", [bad["repo"] for bad in json.loads(out)["unreadable"]])

    def test_the_digest_makes_only_reads(self):
        route_repo(self.gh, "a/x", building=[issue(1, labels=["pivotal:building"])])
        code, _, err = self.digest()
        self.assertEqual(code, 0, err)
        self.assertTrue(self.gh.calls())
        self.assert_read_only()

    def test_building_without_run_reported_when_only_unrelated_ci_run_in_progress(self):
        # Two workflows: a building issue, a CI run in progress, no pivotal run => the issue is reported
        route_repo(self.gh, "a/x", building=[issue(4, labels=["pivotal:building"])],
                   in_progress=[run(99, workflow="CI", state="in_progress")])
        code, out, _ = self.digest("--json")
        self.assertEqual(code, 0)
        flagged = json.loads(out)["watchdog"]["building_without_run"]
        self.assertEqual([item["number"] for item in flagged], [4])

    def test_building_without_run_tied_to_own_run_by_display_title(self):
        # Building issue #4 and #5; active pivotal run for #4 only => #5 reported, #4 not
        route_repo(self.gh, "a/x",
                   building=[issue(4, labels=["pivotal:building"]), issue(5, labels=["pivotal:building"])],
                   in_progress=[run(90, workflow="Pivotal", state="in_progress", title="pivotal-issue-4")])
        code, out, _ = self.digest("--json")
        self.assertEqual(code, 0)
        flagged = json.loads(out)["watchdog"]["building_without_run"]
        self.assertEqual([item["number"] for item in flagged], [5])

    def test_no_run_not_flagged_if_active_pivotal_run_exists_for_issue(self):
        route_repo(self.gh, "a/x",
                   pivotal=[
                       issue(1, labels=["pivotal"], updated=ago(20)),
                       issue(2, labels=["pivotal"], updated=ago(20)),
                   ],
                   in_progress=[run(91, workflow="Pivotal", state="in_progress", title="pivotal-issue-1")])
        code, out, _ = self.digest("--json")
        self.assertEqual(code, 0)
        flagged = json.loads(out)["watchdog"]["no_run"]
        self.assertEqual([item["number"] for item in flagged], [2])

    def test_naive_timestamp_rejected_as_unreadable(self):
        route_repo(self.gh, "a/x", new=[{"number": 1, "title": "t", "url": "u", "createdAt": "2026-10-27T11:00:00"}])
        code, out, _ = self.digest("--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["repos_unreadable"], 1)

    def test_since_above_max_hours_is_refused_before_any_gh_call(self):
        code, _, err = self.digest("--since", "99999999999")
        self.assertEqual(code, 2)
        self.assertIn("--since must be between 1 and", err)
        self.assertEqual(self.gh.calls(), [])

    def test_unexpected_exception_during_repo_read_makes_it_unreadable(self):
        # Even if read raises an arbitrary exception (e.g. KeyError), collect treats the repo as unreadable
        route_repo(self.gh, "a/x", prs=[{"number": 1, "title": "t", "url": "u", "isDraft": False}])
        code, out, _ = self.digest("--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["repos_unreadable"], 1)

    def test_any_exception_from_a_repo_read_makes_only_that_repo_unreadable(self):
        route_repo(self.gh, "a/x")
        route_repo(self.gh, "a/y")
        real = status.read_digest

        def broken(gh, repo, now, hours):
            if repo == "a/y":
                raise KeyError("boom")
            return real(gh, repo, now, hours)

        with unittest.mock.patch.object(status, "read_digest", broken):
            code, out, _ = self.digest("--json", repos=("a/x", "a/y"))
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual((data["repos_checked"], data["repos_unreadable"]), (2, 1))
        self.assertEqual(data["unreadable"][0]["repo"], "a/y")

    def test_control_bidi_and_line_separators_sanitized_in_text_output(self):
        bad_title = "Title\x1b[31mwith\resc\nand\u202eoverride\u2028line"
        route_repo(self.gh, "a/x", prs=[pr(1, head="pivotal/x")], blocked=[
            {"number": 10, "title": bad_title, "url": "https://github.com/t/10",
             "createdAt": ago(600), "updatedAt": ago(20), "labels": [{"name": "pivotal:blocked"}]}
        ])
        code, out, _ = self.run_cli("digest", "--repo", "a/x")
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("\u202e", out)
        self.assertNotIn("\u2028", out)
        self.assertIn("Title\\x1b[31mwith\\resc\\nand\\u202eoverride\\u2028line", out)

    def test_unreadable_reason_sanitized_in_text_output(self):
        self.gh.route("api repos/a/bad", fakegh.fail("gh: bad\x1b[31m\nforged line\u202e\u2028\n"))
        code, out, _ = self.run_cli("digest", "--repo", "a/bad")
        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        self.assertNotIn("\u2028", out)

    def test_digest_json_on_preflight_failure_prints_json_document(self):
        self.gh.route("search repos --owner a --topic pivotal", fakegh.fail("gh: rate limit (HTTP 403)\n"))
        code, out, err = self.run_cli("digest", "--owner", "a", "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["repos_checked"], 0)
        self.assertEqual(data["repos_unreadable"], 0)
        self.assertIn("error", data)
        self.assertIn("gh search repos", data["error"])
        self.assertIn("gh search repos", err)


class FlagsTest(StatusTest):
    """The fake refuses a flag the real gh lacks, so these reads can be trusted to run against the real CLI."""

    def test_the_fake_refuses_a_write_flag_on_a_list(self):
        self.gh.route("issue list", fakegh.ok("[]"))
        # The fake reads stdin, so it gets an empty one here rather than whatever the test process inherited.
        done = subprocess.run([str(self.gh.path), "issue", "list", "-R", "a/x", "--body", "v"],
                              input="", capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 2)
        self.assertIn("has no flag --body", done.stderr)

    def test_every_read_flag_the_status_code_passes_is_one_the_real_gh_takes(self):
        # A flag the fake refuses makes its repo unreadable, so a clean run proves each flag is in REAL_FLAGS.
        route_repo(
            self.gh, "a/x", new=[issue(1, created=ago(5))], prs=[pr(2)], main=[run(3)],
            queued=[run(4, state="queued")],
        )
        code, out, _ = self.digest("--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["repos_unreadable"], 0)


class LauncherStatusTest(StatusTest):
    """The extension entry point as gh runs it: a subprocess, with the fake as PIV_GH."""

    def test_user_visible_check_one_unreadable_repo_reads_1_of_n_and_exits_non_zero(self):
        route_repo(self.gh, "a/x", building=[issue(1)])
        route_unreadable(self.gh, "a/broken")
        work = Path(self._tmp.name) / "work"
        work.mkdir()
        done = subprocess.run(
            [str(LAUNCHER), "now", "--repo", "a/x", "--repo", "a/broken"],
            cwd=work, env=self.gh.env(), capture_output=True, text=True, check=False,
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("1 of 2 repos unreadable:", done.stdout)
        self.assertEqual(list(work.iterdir()), [], "status writes nothing to the working directory")


if __name__ == "__main__":
    unittest.main()
