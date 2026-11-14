"""The publish job (F1, F6): engine/publish.py against real git and a fake gh.

Every test builds its own repository, a bare "remote" on disk (the server URL is a file:// one) and a fake gh (test/fakegh.py,
strict about the flags of the real gh). The land artifact is made by the real assemble job from hand-made slice artifacts,
or forged by hand where a test needs what assemble would never write. Nothing here has run on real GitHub, and no test
reaches the network or a model.
"""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import fakegh
from test_assemble import ISSUE, AssembleCase, clean_git_env

import nolive  # noqa: F401  (no test sweeps the live /proc)
from engine import assemble, buildjob, job, plan, publish, report

REPO = "0xctx/piv-sandbox"
BRANCH = f"pivotal/issue-{ISSUE}/"
TOKEN = "ghs_writetokenvalue123"
HEADS = {"one": 11, "two": 12}


class PublishCase(AssembleCase):
    def setUp(self):
        super().setUp()
        self.server = self.root / "server"
        self.bare = self.server / "0xctx" / "piv-sandbox.git"
        self.bare.mkdir(parents=True)
        self.git("init", "-q", "--bare", str(self.bare), cwd=self.root)
        self.git("push", "-q", str(self.bare), f"{self.base}:refs/heads/main")
        self.gh = fakegh.FakeGh(self.root / "bin")
        self.n_pub = 0
        self.gates = self.root / "gates"
        self.gates.mkdir()

    # --- the remote

    def remote_refs(self) -> dict[str, str]:
        out = self.git("for-each-ref", "--format=%(objectname) %(refname)", cwd=self.bare)
        return {ref: sha for sha, ref in (line.split() for line in out.splitlines())}

    def remote_files(self, branch: str) -> set[str]:
        out = self.git("diff", "--name-only", "--no-renames", self.base, f"refs/heads/{branch}", cwd=self.bare)
        return set(out.splitlines())

    # --- the artifact

    def assembled(self, *ids: str, files: dict | None = None) -> Path:
        """Run the real assemble job over one patch per id and return its output directory."""
        for n, name in enumerate(ids):
            change = (files or {}).get(name, {f"src/{name}.txt": f"{name}\n"})
            self.artifact(name, self.patch_of(change))
        code, _, stdout, err = self.run_job(*ids)
        self.assertEqual(code, 0, stdout + err)
        return Path(self.last_env["OUT_DIR"])

    def forge(self, *commits: dict, slice_id: str = "evil", claimed: list[str] | None = None) -> Path:
        """A land artifact that assemble would never write: commits made by hand on the base, one draft branch."""
        for n, files in enumerate(commits):
            self.commit(files, message=f"forged {n}")
        head = self.git("rev-parse", "HEAD")
        made = self.git("rev-list", "--reverse", f"{self.base}..HEAD").split()
        branch = BRANCH + slice_id
        self.git("update-ref", f"refs/heads/{branch}", head)
        out = self.root / f"forged{self.n_pub}"
        out.mkdir()
        self.n_pub += 1
        self.git("bundle", "create", "-q", str(out / assemble.BUNDLE), f"^{self.base}", f"refs/heads/{branch}")
        data = (out / assemble.BUNDLE).read_bytes()
        entry = {
            "id": slice_id, "outcome": "draft", "reason": "verdict_missing", "candidate": False, "branch": branch, "head": head,
            "commits": claimed or made, "built_by": "claude:anthropic/sonnet", "gate": "green", "fell_back": [], "score": None,
            "target": "", "reviewed_by": "",
        }
        land = {
            "version": 2, "issue": ISSUE, "base_sha": self.base, "baseline": "green",
            "bundle": {"name": assemble.BUNDLE, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
            "has_stack": False, "positions": [], "slices": [entry],
        }
        (out / assemble.LAND).write_bytes(assemble.land_text(land))
        self.git("reset", "-q", "--hard", self.base)
        return out

    # --- gh

    def pr(self, branch: str, head: str, number: int = 11, **over) -> dict:
        return {"number": number, "isDraft": True, "state": "OPEN", "headRefName": branch, "headRefOid": head,
                "isCrossRepository": False, **over}

    def route_list(self, gh: fakegh.FakeGh, branch: str, *answers: list) -> None:
        gh.route(f"pr list --repo {REPO} --head {branch} ", *(fakegh.ok(json.dumps(a)) for a in answers))

    def route_create(self, gh: fakegh.FakeGh, branch: str, number: int) -> None:
        gh.route(f"pr create --repo {REPO} --draft --base main --head {branch} ",
                 fakegh.ok(f"https://github.com/{REPO}/pull/{number}\n"))

    def new_prs(self, out: Path, gh: fakegh.FakeGh | None = None) -> None:
        """Routes for a repository with no pull request yet: the list is empty, then (after the create) holds ours."""
        gh = gh or self.gh
        land = assemble.read_land(out / assemble.LAND)
        for entry in land["slices"]:
            if entry["outcome"] == "draft":
                number = HEADS.get(entry["id"], 13)
                self.route_list(gh, entry["branch"], [], [self.pr(entry["branch"], entry["head"], number)])
                self.route_create(gh, entry["branch"], number)

    # --- running

    def penv(self, out: Path, gh: fakegh.FakeGh | None = None, **over) -> dict[str, str]:
        gh = gh or self.gh
        self.n_pub += 1
        run = self.root / f"pub{self.n_pub}"
        run.mkdir()
        try:
            stacked = bool(assemble.read_land(out / assemble.LAND)["positions"])
        except assemble.LandError:  # a test that hands over a land.json that is not one
            stacked = False
        env = {
            "KIND": "go", "TRUSTED": "true", "LEVEL": "prepare", "REVIEWER": "true", "HAS_STACK": "true" if stacked else "false",
            "GATE_RESULT": "success" if stacked else "skipped", "REVIEW_RESULT": "success", "GATES_DIR": str(self.gates),
            "REVIEWS_DIR": str(self.reviews), "SLICES_DIR": str(self.slices),
            "PATH": os.environ.get("PATH", ""), "PIV_GH": str(gh.path), "GH_TOKEN": TOKEN, "GH_REPO": REPO, "ISSUE": str(ISSUE),
            "BASE_SHA": self.base, "MATRIX": self.matrix("one", "two"), "ASSEMBLE_RESULT": "success",
            "LAND_SHA256": hashlib.sha256((out / assemble.LAND).read_bytes()).hexdigest(), "DEFAULT_BRANCH": "main",
            "ROUND": "0", "ISSUE_PIN": "a" * 64, "RUN_ID": "4242", "RUN_NUMBER": "5", "RUN_ATTEMPT": "1", "LAND_DIR": str(out), "WORK_DIR": str(run / "work"),
            "GITHUB_OUTPUT": str(run / "output"), "GITHUB_SERVER_URL": f"file://{self.server}",
        }
        env.update(over)
        env.setdefault("PLANNED_LEVEL", env["LEVEL"] or "prepare")  # plan's level: what the run started at
        env.setdefault("CHECK", "make test")
        return {k: v for k, v in env.items() if v is not None}

    def publish(self, out: Path, gh: fakegh.FakeGh | None = None, **over) -> tuple[int, dict[str, str], str, str]:
        env = self.penv(out, gh, **over)
        if "GITHUB_OUTPUT" in env:
            Path(env["GITHUB_OUTPUT"]).write_text("")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = job.run_job(publish.publish, env)
        outputs = {}
        if "GITHUB_OUTPUT" in env:
            outputs = dict(line.split("=", 1) for line in Path(env["GITHUB_OUTPUT"]).read_text().splitlines())
        self.last_pub = env
        return code, outputs, stdout.getvalue(), stderr.getvalue()

    def calls(self, gh: fakegh.FakeGh | None = None, what: str = "") -> list[list[str]]:
        return [c["args"] for c in (gh or self.gh).calls() if " ".join(c["args"][:2]) == what or not what]

    def creates(self, gh: fakegh.FakeGh | None = None) -> list[list[str]]:
        return self.calls(gh, "pr create")

    def refused(self, out: Path, message: str, **over) -> None:
        """The run refuses with a message, before it writes anything: no push, no pull request, no output."""
        before = self.remote_refs()
        gh_before = len(self.gh.calls())
        code, outputs, _, err = self.publish(out, **over)
        self.assertEqual(code, 1, err)
        self.assertIn("refused:", err)
        self.assertIn(message, err)
        self.assertEqual(self.remote_refs(), before, "nothing was pushed")
        self.assertEqual(self.creates(), [])
        self.assertEqual(len(self.gh.calls()), gh_before, "gh was not even asked")
        self.assertEqual(outputs, {})


class DraftPrTest(PublishCase):
    def test_an_eleven_digit_run_id_is_read_and_each_draft_names_that_run(self):
        # A real run id has 11 digits (measured live: 42942520152); publish reads it with RUN_ID_DIGITS, so none is refused.
        out = self.assembled("one", "two", "blocked", files={
            "one": {"src/one.txt": "1\n"}, "two": {"src/two.txt": "2\n"}, "blocked": {".github/workflows/ci.yml": "name: x\n"},
        })
        self.new_prs(out)
        code, _, _, err = self.publish(out, RUN_ID="42942520152")
        self.assertEqual(code, 0, err)
        runs = []
        for create in self.creates():
            body = Path(create[create.index("--body-file") + 1]).read_text()
            runs.append(json.loads(body.split("<!-- pivotal-run:", 1)[1].split(" -->", 1)[0])["run"])
        self.assertEqual(len(runs), 2)
        self.assertEqual(set(runs), {42942520152})

    def test_each_draft_is_pushed_and_opened_once_as_a_draft_with_only_its_files(self):
        out = self.assembled("one", "two", "blocked", files={
            "one": {"src/one.txt": "1\n", "README.md": "r\n"}, "two": {"src/two.txt": "2\n"},
            "blocked": {".github/workflows/ci.yml": "name: x\n"},
        })
        self.assertEqual(sorted(self.remote_refs()), ["refs/heads/main"])
        self.new_prs(out)
        code, outputs, stdout, err = self.publish(out)
        self.assertEqual(code, 0, err)
        land = assemble.read_land(out / assemble.LAND)
        heads = {e["id"]: e["head"] for e in land["slices"]}
        refs = self.remote_refs()
        self.assertEqual(refs[f"refs/heads/{BRANCH}one"], heads["one"])
        self.assertEqual(refs[f"refs/heads/{BRANCH}two"], heads["two"])
        self.assertEqual(self.remote_files(BRANCH + "one"), {"src/one.txt", "README.md"})
        self.assertEqual(self.remote_files(BRANCH + "two"), {"src/two.txt"}, "a draft shows only its own slice's files")
        titles = {"one": "title one", "two": "title two"}
        for create in self.creates():
            branch = create[create.index("--head") + 1]
            slice_id = branch.rsplit("/", 1)[1]
            body_at = create[create.index("--body-file") + 1]
            self.assertEqual(create[:9], ["pr", "create", "--repo", REPO, "--draft", "--base", "main", "--head", branch])
            self.assertEqual(create[create.index("--title") + 1], f"Pivotal: {titles[slice_id]} (#{ISSUE})")
            self.assertEqual(sorted(a for a in create if a.startswith("--")), ["--base", "--body-file", "--draft", "--head", "--repo", "--title"])
            body = Path(body_at).read_text()
            self.assertTrue(body.startswith(f"Closes #{ISSUE}\n"))
            self.assertIn("reason: verdict_missing", body)
            self.assertIn(f"head: {heads[slice_id]}", body)
            marker = json.loads(body.split("<!-- pivotal-run:", 1)[1].split(" -->", 1)[0])
            self.assertEqual(marker, {"run": 4242, "issue": ISSUE, "slice": slice_id, "head": heads[slice_id]})
        self.assertEqual(len(self.creates()), 2)
        self.assertIn("slice blocked: skipped (blocked: workflow_change) has no branch", stdout)
        self.assertNotIn(f"refs/heads/{BRANCH}blocked", refs)
        result = report.parse_result(outputs["result"])
        self.assertEqual((result.status, result.why, result.pr, result.built_by), ("failed", "workflow_change", 11, "claude:anthropic/sonnet"),
                         "round 2, R1: the blocked sibling is the answer, the opened draft stays the link")
        self.assertIsNotNone(job.OUTPUT_RULES["result"].fullmatch(outputs["result"]))

    def test_the_gh_calls_use_only_flags_the_real_gh_has(self):
        """The fake refuses a flag the real command lacks (exit 2), so a run that passes used none."""
        out = self.assembled("one")
        self.new_prs(out)
        self.assertEqual(self.publish(out)[0], 0)
        self.assertEqual({tuple(a[:2]) for a in self.calls()}, {("pr", "list"), ("pr", "create")})
        listing = self.calls(what="pr list")[0]
        self.assertEqual(listing, ["pr", "list", "--repo", REPO, "--head", BRANCH + "one", "--state", "all", "--limit", "20",
                                   "--json", publish.PR_FIELDS])

    def test_a_rerun_opens_no_second_pull_request(self):
        out = self.assembled("one", "two")
        self.new_prs(out)
        self.assertEqual(self.publish(out)[0], 0)
        refs = self.remote_refs()
        # The second run, a new workflow run (new run id) over the same artifact, sees the pull requests the first opened.
        again = fakegh.FakeGh(self.root / "bin2")
        land = assemble.read_land(out / assemble.LAND)
        for e in land["slices"]:
            self.route_list(again, e["branch"], [self.pr(e["branch"], e["head"], HEADS[e["id"]])])
        code, outputs, stdout, err = self.publish(out, again, RUN_ID="4243", RUN_ATTEMPT="2")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.creates(again), [], "no second pull request")
        self.assertEqual({tuple(a[:2]) for a in self.calls(again)}, {("pr", "list")}, "a lookup by branch and nothing else")
        self.assertEqual(self.remote_refs(), refs)
        self.assertIn("slice one: exists (#11)", stdout)
        self.assertEqual(report.parse_result(outputs["result"]).pr, 11)

    def test_a_rerun_after_a_closed_or_merged_pull_request_does_not_open_another(self):
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        for state in ("CLOSED", "MERGED"):
            gh = fakegh.FakeGh(self.root / f"bin-{state}")
            self.route_list(gh, BRANCH + "one", [self.pr(BRANCH + "one", head, 9, state=state, isDraft=False)])
            code, _, stdout, _ = self.publish(out, gh)
            self.assertEqual((code, self.creates(gh)), (0, []), state)
            self.assertIn("exists (#9)", stdout)

    def test_a_pull_request_from_a_fork_with_the_same_branch_name_is_not_ours(self):
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        fork = self.pr(BRANCH + "one", "f" * 40, 3, isCrossRepository=True)
        self.route_list(self.gh, BRANCH + "one", [fork], [fork, self.pr(BRANCH + "one", head, 14)])
        self.route_create(self.gh, BRANCH + "one", 14)
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(report.parse_result(outputs["result"]).pr, 14)

    def test_a_branch_already_at_the_head_is_not_pushed_again_and_the_pull_request_is_opened(self):
        """The first run died between the push and the create."""
        out = self.assembled("one")
        self.new_prs(out)
        self.assertEqual(self.publish(out)[0], 0)
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        gh = fakegh.FakeGh(self.root / "bin2")
        self.route_list(gh, BRANCH + "one", [], [self.pr(BRANCH + "one", head, 11)])
        self.route_create(gh, BRANCH + "one", 11)
        refs = self.remote_refs()
        code, _, _, err = self.publish(out, gh)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.creates(gh)), 1)
        self.assertEqual(self.remote_refs(), refs)

    def test_a_pull_request_at_another_head_is_a_failure_and_changes_nothing(self):
        out = self.assembled("one", "two")
        land = assemble.read_land(out / assemble.LAND)
        one, two = land["slices"]
        self.route_list(self.gh, one["branch"], [self.pr(one["branch"], "a" * 40, 5)])
        self.route_list(self.gh, two["branch"], [], [self.pr(two["branch"], two["head"], 12)])
        self.route_create(self.gh, two["branch"], 12)
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 1)
        self.assertIn("slice one: failed (pr_differs)", err)
        self.assertEqual([c[c.index("--head") + 1] for c in self.creates()], [two["branch"]], "the other slice went on")
        self.assertNotIn(f"refs/heads/{one['branch']}", self.remote_refs())
        # the result still names the draft that was published, and the job is red anyway
        self.assertEqual(report.parse_result(outputs["result"]).pr, 12)

    def test_a_remote_branch_at_another_commit_is_never_overwritten(self):
        out = self.assembled("one")
        other = self.git("commit-tree", f"{self.base}^{{tree}}", "-p", self.base, "-m", "someone else")
        self.git("push", "-q", str(self.bare), f"{other}:refs/heads/{BRANCH}one")
        self.route_list(self.gh, BRANCH + "one", [])
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 1)
        self.assertIn("slice one: failed (branch_differs)", err)
        self.assertEqual(self.remote_refs()[f"refs/heads/{BRANCH}one"], other)
        self.assertEqual(self.creates(), [])
        self.assertEqual(json.loads(outputs["result"]), {"status": "failed", "why": "branch_differs"})

    def test_a_rejected_push_is_a_failure_and_no_pull_request_is_opened(self):
        hook = self.bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        out = self.assembled("one")
        self.route_list(self.gh, BRANCH + "one", [])
        code, _, _, err = self.publish(out)
        self.assertEqual(code, 1)
        self.assertIn("slice one: failed (push_rejected)", err)
        self.assertEqual(self.creates(), [])

    def test_a_failed_create_is_red_and_the_next_run_finishes_the_job(self):
        out = self.assembled("one")
        self.route_list(self.gh, BRANCH + "one", [])
        self.gh.route("pr create", fakegh.fail("boom"))
        code, _, _, err = self.publish(out)
        self.assertEqual((code, len(self.creates())), (1, 1))
        self.assertIn("slice one: failed (gh_failed)", err)
        self.assertIn(f"refs/heads/{BRANCH}one", self.remote_refs(), "the branch stays for the re-run")
        gh = fakegh.FakeGh(self.root / "bin2")
        self.new_prs(out, gh)
        code, _, _, err = self.publish(out, gh)
        self.assertEqual((code, len(self.creates(gh))), (0, 1), err)

    def test_a_pull_request_that_is_not_the_draft_that_was_asked_for_is_a_failure(self):
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        self.route_list(self.gh, BRANCH + "one", [], [self.pr(BRANCH + "one", head, 11, isDraft=False)])
        self.gh.route(f"pr create --repo {REPO} ", fakegh.ok("created\n"))  # no URL to trust: the lookup decides
        code, _, _, err = self.publish(out)
        self.assertEqual(code, 1)
        self.assertIn("pr_not_found", err)

    def test_gh_answers_of_another_shape_fail_the_slice(self):
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        good = self.pr(BRANCH + "one", head)
        for bad in ({"x": 1}, {**good, "number": True}, {**good, "number": 0}, {**good, "state": "open"}, {**good, "headRefOid": "abc"},
                    {**good, "isDraft": "yes"}, {**good, "extra": 1}, "text"):
            with self.subTest(bad=bad):
                gh = fakegh.FakeGh(self.root / f"bin-{abs(hash(str(bad)))}")
                self.route_list(gh, BRANCH + "one", [bad])
                code, _, _, err = self.publish(out, gh)
                self.assertEqual((code, self.creates(gh)), (1, []))
                self.assertIn("slice one: failed (gh_failed)", err)
        for text in ("not json", '{"a": 1}'):
            gh = fakegh.FakeGh(self.root / f"bin-{abs(hash(text))}")
            gh.route(f"pr list --repo {REPO} --head {BRANCH}one ", fakegh.ok(text))
            self.assertEqual(self.publish(out, gh)[0], 1)


class NothingToPublishTest(PublishCase):
    def test_without_a_draft_nothing_is_fetched_pushed_or_asked_and_the_result_says_why(self):
        self.artifact("one", self.patch_of({".github/workflows/ci.yml": "name: x\n"}))
        code, _, _, _ = self.run_job("one")
        out = Path(self.last_env["OUT_DIR"])
        refs = self.remote_refs()
        code, outputs, stdout, err = self.publish(out, MATRIX=self.matrix("one"), WORK_DIR=str(self.root / "w"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.gh.calls(), [])
        self.assertEqual(self.remote_refs(), refs)
        self.assertFalse((self.root / "w").exists(), "no scratch repository was made")
        self.assertEqual(json.loads(outputs["result"]), {"status": "failed", "why": "workflow_change", "built_by": "claude:anthropic/sonnet"})
        self.assertEqual(report.parse_result(outputs["result"]).why, "workflow_change")
        self.assertIn("slice one: skipped (blocked: workflow_change) has no branch", stdout)

    def test_a_failed_build_is_a_failed_result_with_its_own_why(self):
        self.artifact("one", None, result={"status": "failed", "reason": "no_route", "gate": "not_run"})
        self.run_job("one")
        out = Path(self.last_env["OUT_DIR"])
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(report.parse_result(outputs["result"]).why, "no_route")

    def test_other_reasons_are_a_failed_build(self):
        self.artifact("one", None, result={"status": "failed", "reason": "timeout", "gate": "not_run"})
        self.run_job("one")
        code, outputs, _, _ = self.publish(Path(self.last_env["OUT_DIR"]))
        self.assertEqual((code, report.parse_result(outputs["result"]).why), (0, "build_failed"))

    def test_a_conflict_is_reported_as_one(self):
        self.artifact("one", self.patch_of({"README.md": "mine\n"}))
        self.git("commit", "--allow-empty", "-qm", "noop")
        (self.repo / "README.md").write_text("theirs\n")
        self.git("commit", "-qam", "theirs")
        self.base = self.git("rev-parse", "HEAD")
        self.run_job("one")
        out = Path(self.last_env["OUT_DIR"])
        self.assertEqual(self.entry(assemble.read_land(out / assemble.LAND), "one")["outcome"], "conflict")
        code, outputs, _, _ = self.publish(out, BASE_SHA=self.base)
        self.assertEqual((code, report.parse_result(outputs["result"]).why), (0, "conflict"))

    def test_a_red_baseline_draft_is_published_with_its_own_why(self):
        self.artifact("one", self.patch_of({"a.txt": "x\n"}))
        self.run_job("one", BASELINE="red")
        out = Path(self.last_env["OUT_DIR"])
        self.new_prs(out)
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(report.parse_result(outputs["result"]).why, "baseline_red")


class RefusalTest(PublishCase):
    """Whatever is wrong, the run says so and writes nothing: no push, no pull request, no output line."""

    def setUp(self):
        super().setUp()
        self.out = self.assembled("one", "two")
        self.new_prs(self.out)

    def test_a_377_char_token_with_dots_dashes_underscores_is_accepted(self):
        """A real GITHUB_TOKEN is 377 chars of printable ASCII with dots, dashes, underscores."""
        # ghp_ followed by 373 chars (377 total) - typical GitHub token format
        token = "ghp_" + "." * 93 + "-" * 93 + "_" * 93 + "a" * 94  # 4 + 93 + 93 + 93 + 94 = 377
        self.assertEqual(len(token), 377)
        self.assertTrue(publish.TOKEN.fullmatch(token), f"377-char token should match: {token[:50]}...")
        # Also test that the publish job accepts it
        code, _, _, err = self.publish(self.out, GH_TOKEN=token)
        self.assertEqual(code, 0, f"377-char token should be accepted: {err}")

    def test_a_4097_char_token_is_refused(self):
        token = "x" * 4097
        self.assertFalse(publish.TOKEN.fullmatch(token))
        self.refused(self.out, "GH_TOKEN", GH_TOKEN=token)

    def test_an_empty_token_is_refused(self):
        self.refused(self.out, "GH_TOKEN", GH_TOKEN="")

    def test_a_token_with_space_is_refused(self):
        self.refused(self.out, "GH_TOKEN", GH_TOKEN="a b")

    def test_a_token_with_newline_is_refused(self):
        self.refused(self.out, "GH_TOKEN", GH_TOKEN="t\n")

    def test_an_assemble_job_that_did_not_succeed(self):
        for value in ("failure", "skipped", "cancelled", "", "Success", None):
            with self.subTest(value=value):
                self.refused(self.out, "assemble job did not succeed", ASSEMBLE_RESULT=value)

    def test_a_land_json_that_is_not_the_file_the_assemble_job_described(self):
        land = json.loads((self.out / assemble.LAND).read_text())
        land["baseline"] = "none"
        (self.out / assemble.LAND).write_bytes(assemble.land_text(land))  # valid, but another file than the one hashed
        self.refused(self.out, "sha256 differs", LAND_SHA256=hashlib.sha256(b"original").hexdigest())

    def test_the_output_is_read_from_the_environment_and_only_in_its_form(self):
        for value in ("", "a" * 63, "A" * 64, "g" * 64, " " + "a" * 63, "a" * 64 + "\n", None):
            with self.subTest(value=value):
                self.refused(self.out, "LAND_SHA256", LAND_SHA256=value)

    def test_a_land_json_that_breaks_the_schema_even_when_its_hash_matches(self):
        for name, text in (("garbage", b"not json"), ("empty", b""), ("list", b"[]"), ("dup", b'{"version":1,"version":1}'),
                           ("huge", b"[" * 400000)):
            with self.subTest(name=name):
                (self.out / assemble.LAND).write_bytes(text)
                self.refused(self.out, "land.json", LAND_SHA256=hashlib.sha256(text).hexdigest())

    def test_a_valid_land_json_in_another_form_than_assemble_writes(self):
        land = json.loads((self.out / assemble.LAND).read_text())
        text = json.dumps(land).encode()
        (self.out / assemble.LAND).write_bytes(text)
        self.refused(self.out, "not in the form", LAND_SHA256=hashlib.sha256(text).hexdigest())

    def test_a_land_json_for_another_issue_or_base(self):
        self.refused(self.out, "another issue or base", ISSUE="8")
        self.refused(self.out, "another issue or base", BASE_SHA="b" * 40)

    def test_an_artifact_that_holds_more_or_less_than_land_json_and_the_bundle(self):
        (self.out / "extra.txt").write_text("x")
        self.refused(self.out, "land.json and at most pivotal.bundle")
        (self.out / "extra.txt").unlink()
        (self.out / assemble.BUNDLE).rename(self.root / "moved")
        self.refused(self.out, "bundle land.json describes is missing")

    def test_a_link_a_fifo_or_a_directory_in_place_of_a_file(self):
        bundle = self.out / assemble.BUNDLE
        data = bundle.read_bytes()
        bundle.unlink()
        os.symlink(self.root / "elsewhere", bundle)
        self.refused(self.out, "not a regular file")
        bundle.unlink()
        os.mkfifo(bundle)
        self.refused(self.out, "not a regular file")
        bundle.unlink()
        bundle.mkdir()
        self.refused(self.out, "not a regular file")
        bundle.rmdir()
        bundle.write_bytes(data)
        land = self.out / assemble.LAND
        text = land.read_bytes()
        land.unlink()
        (self.root / "real-land.json").write_bytes(text)
        os.symlink(self.root / "real-land.json", land)
        self.refused(self.out, "not a regular file")

    def test_a_bundle_that_is_not_the_one_land_json_describes(self):
        bundle = self.out / assemble.BUNDLE
        bundle.write_bytes(bundle.read_bytes() + b"x")
        self.refused(self.out, "bundle")

    def test_a_bundle_without_a_draft_to_describe(self):
        self.artifact("lone", self.patch_of({".github/x.yml": "a: b\n"}))
        out = self.root / "nodraft"
        out.mkdir()
        land = {"version": 2, "issue": ISSUE, "base_sha": self.base, "baseline": "green", "bundle": None, "has_stack": False,
                "positions": [], "slices": [{"id": "lone", "outcome": "blocked", "reason": "workflow_change", "candidate": False,
                                              "branch": None, "head": None, "commits": [], "built_by": "", "gate": "", "fell_back": [],
                                              "score": None, "target": "", "reviewed_by": ""}]}
        (out / assemble.LAND).write_bytes(assemble.land_text(land))
        (out / assemble.BUNDLE).write_bytes(b"# v2 git bundle\n\n")
        self.refused(out, "yet the artifact holds one")

    def test_a_missing_or_relative_directory_and_an_existing_work_dir(self):
        self.refused(self.out, "LAND_DIR must be an absolute path", LAND_DIR="land-in")
        self.refused(self.out, "LAND_DIR is not a directory", LAND_DIR=str(self.root / "nowhere"))
        self.refused(self.out, "WORK_DIR already exists", WORK_DIR=str(self.root))
        self.refused(self.out, "WORK_DIR must be an absolute path", WORK_DIR="work")

    def test_the_settings_are_validated(self):
        cases = {
            "GH_TOKEN": ("", None, "a b", "t\n", "x" * 4097), "GH_REPO": ("", "nope", "a/b/c", "a/b;c"),
            "ISSUE": ("", "0", "07", "x", "1\n"), "BASE_SHA": ("", "abc", "0" * 40, "A" * 40),
            "RUN_ID": ("", "x", "0"), "RUN_NUMBER": ("", "x"), "RUN_ATTEMPT": ("x", "0"),
            "DEFAULT_BRANCH": ("", None, "-x", "a..b", "a b", "x\n", ".hidden", "a.lock", "a/", "a//b", "a@{b", "a/.b", "a/b.lock/c"),
            "GITHUB_SERVER_URL": ("http://github.com", "ftp://x", "https://", "https://a b", "file://rel", "https://x/y;z"),
            "GITHUB_OUTPUT": ("", None),
            "MATRIX": ("not json", "[]", '[{"id": "a"}]'),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    env = self.penv(self.out, **{name: value})
                    self.assertNotEqual(self.run_env(env), 0)
                    self.assertEqual(self.creates(), [])
                    self.assertEqual(sorted(self.remote_refs()), ["refs/heads/main"])

    def run_env(self, env: dict) -> int:
        if env.get("GITHUB_OUTPUT"):
            Path(env["GITHUB_OUTPUT"]).write_text("")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return job.run_job(publish.publish, env)

    def test_a_base_the_remote_does_not_have(self):
        empty = self.server / "0xctx" / "empty.git"
        empty.mkdir()
        self.git("init", "-q", "--bare", str(empty), cwd=self.root)
        self.refused(self.out, "could not be fetched", GH_REPO="0xctx/empty")


class ForgedLandTest(PublishCase):
    """What assemble would never write, and publish must not push: the outcome of land.json is not trusted."""

    def refuse_forged(self, out: Path, message: str) -> None:
        self.refused(out, message, MATRIX=None)

    def test_a_workflow_change_in_a_draft_branch_is_never_pushed(self):
        self.refuse_forged(self.forge({".github/workflows/ci.yml": "name: evil\n"}), ".github")

    def test_a_case_variant_of_the_workflow_directory(self):
        self.refuse_forged(self.forge({".GitHub/workflows/ci.yml": "name: evil\n"}), ".github")

    def test_a_workflow_file_moved_out_is_still_a_workflow_change(self):
        self.refuse_forged(self.forge({".github/workflows/ci.yml": None, "docs/ci.yml": "name: ci\n"}), ".github")

    def test_a_workflow_change_in_the_first_of_two_commits_undone_by_the_second(self):
        self.refuse_forged(self.forge({".github/x.yml": "a: b\n"}, {".github/x.yml": None}), ".github")

    def test_a_draft_with_commits_other_than_the_ones_land_json_lists(self):
        out = self.forge({"a.txt": "1\n"}, {"b.txt": "2\n"})
        land = json.loads((out / assemble.LAND).read_text())
        head = land["slices"][0]["head"]
        land["slices"][0]["commits"] = [head]
        (out / assemble.LAND).write_bytes(assemble.land_text(land))
        self.refused(out, "straight line", MATRIX=None)

    def test_a_draft_cut_from_another_commit_than_the_base_is_refused(self):
        """Commits on an unrelated root: the bundle does not require the base."""
        root = self.git("commit-tree", f"{self.base}^{{tree}}", "-m", "unrelated root")
        self.git("update-ref", "refs/heads/side", root)
        self.git("checkout", "-q", "-B", "work", root)
        out = self.forge({"a.txt": "1\n"})
        # forge() committed on top of `root`, not the base: the bundle then has no usable prerequisite.
        self.refuse_forged(out, "bundle")

    def test_a_clean_forged_draft_is_published_so_the_refusals_above_are_the_checks(self):
        out = self.forge({"src/forged.txt": "ok\n"})
        entry = assemble.read_land(out / assemble.LAND)["slices"][0]
        self.route_list(self.gh, entry["branch"], [], [self.pr(entry["branch"], entry["head"], 21)])
        self.route_create(self.gh, entry["branch"], 21)
        code, _, _, err = self.publish(out, MATRIX=None)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_files(entry["branch"]), {"src/forged.txt"})
        self.assertEqual(self.creates()[0][self.creates()[0].index("--title") + 1], f"Pivotal: slice evil (#{ISSUE})")


class UntrustedTextTest(PublishCase):
    def test_a_hostile_slice_title_is_one_plain_line_in_the_pull_request_title(self):
        out = self.assembled("one")
        self.new_prs(out)
        hostile = "Fix [x](http://evil) @everyone `rm` ::set-output name=x::y www.a.io"
        matrix = json.dumps([{"id": "one", "title": hostile, "goal": "g"}])
        code, _, _, err = self.publish(out, MATRIX=matrix)
        self.assertEqual(code, 0, err)
        title = self.creates()[0][self.creates()[0].index("--title") + 1]
        self.assertTrue(title.startswith("Pivotal: Fix"))
        self.assertTrue(title.endswith(f" (#{ISSUE})"))
        for bad in ("\n", "\x1b", "‮", "@", "[", "]", "(http", "`", "::", "//"):
            self.assertNotIn(bad, title.removesuffix(f" (#{ISSUE})"))
        self.assertLessEqual(len(title), len(publish.TITLE_PREFIX) + publish.TITLE_MAX + len(f" (#{ISSUE})"))

    def test_a_title_with_control_bidi_and_a_long_tail_is_cleaned_by_the_title_function(self):
        """plan.parse_slices already refuses most of these; the title function does not rely on that."""
        s = mock.Mock(issue=ISSUE, titles={"one": "a\u202e\x1b[31m\nb [c](d) @e `f` " + "z" * 300})
        title = publish.pr_title(s, {"id": "one"})
        self.assertTrue(title.isprintable() and title.isascii(), title)
        for bad in ("@", "[", "]", "`", "(d)"):
            self.assertNotIn(bad, title)
        self.assertLessEqual(len(title), len(publish.TITLE_PREFIX) + publish.TITLE_MAX + len(f" (#{ISSUE})"))

    def test_a_title_the_matrix_does_not_have_falls_back_to_the_slice_id(self):
        out = self.assembled("one")
        self.new_prs(out)
        self.assertEqual(self.publish(out, MATRIX="")[0], 0)
        self.assertEqual(self.creates()[0][self.creates()[0].index("--title") + 1], f"Pivotal: slice one (#{ISSUE})")

    def test_fenced_text_cannot_close_its_fence_or_carry_a_control_character(self):
        for text in ("a```\n# heading\n```b", "x\x00y\x1bz‮", "é" * 5, "```" * 1000, "\n\n"):
            with self.subTest(text=text[:20]):
                block = publish.fenced(text)
                lines = block.split("\n")
                self.assertEqual((lines[0], lines[-1]), ("```text", "```"))
                self.assertEqual(block.count("```"), 2)
                self.assertTrue(all(" " <= c <= "~" or c == "\n" for c in block))
                self.assertLessEqual(len(block), publish.FENCE_MAX + 20)

    def test_the_body_keeps_hostile_values_inside_the_fence(self):
        s = mock.Mock(issue=ISSUE, run_id=1)
        entry = {"id": "one", "head": "a" * 40, "reason": "x```\n@everyone\n```\n# forged", "built_by": "[a](b)", "gate": "\x1b[2J",
                 "candidate": True}
        body = publish.pr_body(s, {"baseline": "green"}, entry)
        self.assertEqual(body.count("```"), 2)
        self.assertTrue(body.startswith(f"Closes #{ISSUE}\n"))
        self.assertTrue(body.isascii() and "\x1b" not in body)
        outside = body.split("```text", 1)[0] + body.rsplit("```", 1)[1]
        self.assertNotIn("@everyone", outside)
        self.assertNotIn("forged", outside)

    def test_an_already_green_draft_says_the_check_passes_and_makes_no_gate_claim(self):
        # S9: already_green is keyed on the repo's check passing on the base. Its draft says so in its own sentence, and it
        # does not say that no review or gate has passed (that claim is for the other drafts).
        s = mock.Mock(issue=ISSUE, run_id=1)
        entry = {"id": "one", "head": "a" * 40, "reason": "already_green", "built_by": "claude-sub", "gate": "", "candidate": False}
        body = publish.pr_body(s, {"baseline": "green"}, entry)
        self.assertIn("The repo's check passes on the base; the CI run that failed may be a different job and may still be red, "
                      "so read this draft before closing the incident.", body)
        self.assertNotIn("No review or gate has passed", body)
        other = publish.pr_body(s, {"baseline": "green"}, {**entry, "reason": "conflict"})
        self.assertIn("No review or gate has passed on it", other)
        self.assertNotIn("The repo's check passes on the base", other)


class SecretTest(PublishCase):
    def test_a_child_gets_the_token_it_needs_and_none_of_the_steps_other_secrets(self):
        out = self.assembled("one")
        self.new_prs(out)
        env = self.penv(out, OPENAI_API_KEY="sk-leak", PIVOTAL_KAGGLE_KEY="kaggle-leak", GITHUB_TOKEN="other-leak")
        Path(env["GITHUB_OUTPUT"]).write_text("")
        done = subprocess.run(["python3", "-I", str(Path(publish.__file__)), "run"], env=env, capture_output=True, text=True,
                              cwd=self.root, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = self.gh.environments()
        self.assertEqual(len(seen), len(self.gh.calls()))
        for child in seen:
            self.assertEqual(child["GH_TOKEN"], TOKEN)
            self.assertEqual(child["GH_REPO"], REPO)
            self.assertEqual(child["GH_PROMPT_DISABLED"], "1")
            self.assertNotIn("sk-leak", json.dumps(child))
            self.assertNotIn("kaggle-leak", json.dumps(child))
            self.assertNotIn("other-leak", json.dumps(child))
            self.assertNotIn("GITHUB_OUTPUT", child)
            self.assertTrue(child["HOME"].startswith(str(Path(env["WORK_DIR"]))), "gh keeps its config in the scratch directory")
        self.assertNotIn(TOKEN, done.stdout + done.stderr)
        self.assertTrue(Path(env["GITHUB_OUTPUT"]).read_text().startswith("result="))

    def test_main_makes_the_process_unreadable_and_empties_the_environment_before_any_work(self):
        seen = {}

        def capture(function, env):
            seen["env"], seen["os"] = dict(env), dict(os.environ)
            return 0

        with mock.patch.dict(os.environ, {"GH_TOKEN": TOKEN, "PATH": "/usr/bin", "OPENAI_API_KEY": "sk-x"}, clear=True), \
                mock.patch.object(buildjob, "make_unreadable") as unreadable, mock.patch.object(job, "run_job", side_effect=capture):
            self.assertEqual(publish.main(["run"], os.environ), 0)
        unreadable.assert_called_once_with()
        self.assertEqual(seen["env"]["GH_TOKEN"], TOKEN)
        self.assertEqual(seen["os"], {"PATH": "/usr/bin"}, "no secret stays in os.environ")

    def test_main_refuses_when_the_process_cannot_be_made_unreadable(self):
        with mock.patch.object(buildjob, "make_unreadable", side_effect=job.JobError("no prctl")), \
                contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.object(job, "run_job") as run:
            self.assertEqual(publish.main(["run"], {}), 1)
        run.assert_not_called()
        self.assertIn("refused", err.getvalue())

    def test_main_takes_one_known_argument(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(publish.main([], {}), 2)
            self.assertEqual(publish.main(["run", "x"], {}), 2)
            self.assertEqual(publish.main(["land"], {}), 2)

    def test_git_gets_the_token_as_a_header_only_for_the_calls_to_the_remote_and_never_in_argv(self):
        git = publish.Remote(self.root / "scratch", self.base, "https://github.com", REPO, TOKEN)
        plain = git.environment()
        self.assertNotIn(TOKEN, json.dumps(plain))
        self.assertNotIn("authorization", json.dumps(plain).lower())
        self.assertEqual(plain["GIT_CONFIG_COUNT"], "3")
        self.assertEqual({plain[f"GIT_CONFIG_KEY_{n}"] for n in range(3)}, {"fetch.fsckObjects", "transfer.fsckObjects", "protocol.version"})
        git.authed = True
        authed = git.environment()
        self.assertEqual(authed["GIT_CONFIG_COUNT"], "4")
        self.assertEqual(authed["GIT_CONFIG_KEY_3"], "http.https://github.com/.extraheader")
        self.assertEqual(authed["GIT_CONFIG_VALUE_3"].split()[:2], ["AUTHORIZATION:", "basic"])
        self.assertEqual(git.url, f"https://github.com/{REPO}.git")
        self.assertEqual(authed["PATH"], "/usr/bin:/bin")
        self.assertEqual(authed["GIT_CONFIG_GLOBAL"], "/dev/null")
        local = publish.Remote(self.root / "scratch", self.base, f"file://{self.server}", REPO, TOKEN)
        local.authed = True
        self.assertNotIn(TOKEN, json.dumps(local.environment()), "a file remote needs no header")

    def test_the_token_is_not_in_any_git_command_line(self):
        out = self.assembled("one")
        self.new_prs(out)
        seen = []
        real = subprocess.run

        def spy(command, *a, **kw):
            seen.append(command)
            return real(command, *a, **kw)

        with mock.patch.object(subprocess, "run", side_effect=spy):
            self.assertEqual(self.publish(out)[0], 0)
        self.assertTrue(seen)
        self.assertNotIn(TOKEN, json.dumps(seen))
        for command in seen:
            self.assertIn(command[0], ("/usr/bin/git", "gh", str(self.gh.path)), "only git and gh are started")
            self.assertNotIn("--force", command)
            self.assertNotIn("-f", command[:6])
            self.assertFalse(any(a.startswith("+") for a in command), "no forced refspec")


class ScriptTest(PublishCase):
    def test_the_script_runs_as_the_workflow_runs_it_and_a_second_run_opens_nothing(self):
        out = self.assembled("one", "two")
        self.new_prs(out)
        script = Path(publish.__file__)
        env = self.penv(out)
        Path(env["GITHUB_OUTPUT"]).write_text("earlier=1\n")
        first = subprocess.run(["python3", "-I", str(script), "run"], env=env, capture_output=True, text=True, cwd=self.root, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        lines = Path(env["GITHUB_OUTPUT"]).read_text().splitlines()
        self.assertEqual(lines[0], "earlier=1")
        self.assertEqual(report.parse_result(lines[1].removeprefix("result=")).pr, 11)
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(self.creates()), 2)
        # run two: the pull requests exist now
        again = fakegh.FakeGh(self.root / "bin2")
        for e in assemble.read_land(out / assemble.LAND)["slices"]:
            self.route_list(again, e["branch"], [self.pr(e["branch"], e["head"], HEADS[e["id"]])])
        env = self.penv(out, again, RUN_ID="4243")
        Path(env["GITHUB_OUTPUT"]).write_text("")
        second = subprocess.run(["python3", "-I", str(script), "run"], env=env, capture_output=True, text=True, cwd=self.root, check=False)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.creates(again), [])
        self.assertEqual(Path(env["GITHUB_OUTPUT"]).read_text().splitlines()[0].split("=", 1)[0], "result")

    def test_a_refusal_is_a_nonzero_exit_and_no_output(self):
        out = self.assembled("one")
        env = self.penv(out, ASSEMBLE_RESULT="failure")
        Path(env["GITHUB_OUTPUT"]).write_text("")
        done = subprocess.run(["python3", "-I", str(Path(publish.__file__)), "run"], env=env, capture_output=True, text=True,
                              cwd=self.root, check=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("refused:", done.stderr)
        self.assertEqual(Path(env["GITHUB_OUTPUT"]).read_text(), "")
        self.assertEqual(self.gh.calls(), [])


class FakeGhIsStrictTest(unittest.TestCase):
    """The fake must refuse what the real gh refuses, or a test could pass on a flag that does not exist (a repeated bug)."""

    def run_gh(self, *args: str) -> subprocess.CompletedProcess:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            gh = fakegh.FakeGh(Path(tmp))
            gh.route("pr", fakegh.ok("[]"))
            return subprocess.run([str(gh.path), *args], capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)

    def test_pr_create_takes_the_flags_the_real_one_has(self):
        ok = self.run_gh("pr", "create", "--repo", REPO, "--draft", "--base", "main", "--head", "b", "--title", "t", "--body-file", "f")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        for bad in ("--draft-pr", "--ready", "--state", "--json", "--body-text", "--branch", "--fill-all"):
            with self.subTest(flag=bad):
                done = self.run_gh("pr", "create", bad, "x")
                self.assertEqual(done.returncode, 2)
                self.assertIn("has no flag", done.stderr)

    def test_pr_list_json_takes_only_real_fields_and_state_only_real_states(self):
        self.assertEqual(self.run_gh("pr", "list", "--json", publish.PR_FIELDS, "--state", "all").returncode, 0)
        self.assertEqual(self.run_gh("pr", "list", "--json", "number,isDrafts").returncode, 2)
        self.assertEqual(self.run_gh("pr", "list", "--json", "headRefOid,bogus").returncode, 2)
        self.assertEqual(self.run_gh("pr", "list", "--state", "everything").returncode, 2)
        self.assertEqual(self.run_gh("pr", "list", "--head", "b", "--limit", "5").returncode, 0)
        self.assertEqual(self.run_gh("pr", "list", "--branch", "b").returncode, 2)

    def test_every_field_publish_asks_for_is_a_real_pr_list_field(self):
        self.assertLessEqual(set(publish.PR_FIELDS.split(",")), fakegh.PR_LIST_JSON_FIELDS)


class ReportFitTest(unittest.TestCase):
    def test_every_result_publish_writes_is_one_report_reads(self):
        for data in (
            {"status": "draft", "why": "no_stack", "built_by": "claude:anthropic/sonnet", "reviewed_by": "none", "pr": 3},
            {"status": "draft", "why": "baseline_red", "built_by": "none", "reviewed_by": "none", "pr": 3},
            {"status": "failed", "why": "publish_failed"},
            {"status": "failed", "why": "build_failed", "built_by": "none"},
        ):
            self.assertEqual(report.parse_result(json.dumps(data)).status, data["status"], data)
        for why in set(publish.WHY_OF.values()) | {"no_stack", "publish_failed", "build_failed"}:
            self.assertIn(why, report.WHY)

    def test_a_silent_agent_is_reported_as_agent_no_output_not_as_a_build_that_did_not_finish(self):
        self.assertEqual(publish.why_of("agent_no_output"), "agent_no_output")
        self.assertIn("agent_no_output", report.WHY)
        text = report.WHY["agent_no_output"]
        self.assertIn("printed nothing and changed nothing", text)
        # agent_no_output is emitted for any door, so the place to look is the entry's endpoint; for Kaggle it is the models-up job's session
        self.assertIn("check the entry's endpoint (for a Kaggle entry, the models-up job's session)", text)
        self.assertNotIn("check the Kaggle session", text)  # the Kaggle session is named only for a Kaggle entry
        self.assertNotIn("run log", text)  # nothing is logged for the agent or its endpoint, so the text must not send you to one

    def test_a_slice_without_a_secret_is_reported_as_no_secret_not_as_a_build_that_did_not_finish(self):
        """Integration (sandbox-t2): the first entry of a preset has no secret; the reason has to reach the comment."""
        self.assertEqual(publish.why_of("no_secret"), "no_secret")
        self.assertIn("no_secret", report.WHY)
        self.assertIn("piv doctor", report.WHY["no_secret"])

    def test_publish_is_a_stage_report_waits_for(self):
        self.assertEqual(report.STAGES[-1], "publish")
        self.assertEqual(report.decide({"ack": "success", "plan": "success", "publish": "failure"}, "").why, "publish_failed")
        self.assertEqual(report.decide({"ack": "success", "plan": "success", "publish": "cancelled"}, "").why, "cancelled")

    def test_the_slice_limit_keeps_the_result_line_short(self):
        self.assertLess(len(json.dumps({"status": "draft", "why": "no_stack", "built_by": "x" * 200, "reviewed_by": "none", "pr": 9999999999})),
                        job.RESULT_MAX)
        self.assertEqual(plan.MAX_SLICES, 6)


class UntrustedRunTest(PublishCase):
    """Operator decision (review finding 2): a stranger's `go` reaches publish with the write token, so the ceiling of what
    the job can do is pinned here. It opens draft PRs for `pivotal/issue-<n>/<slice>` branches and nothing else: no
    non-draft PR, no push elsewhere, no gh command besides `pr list` and `pr create`, no force."""

    def test_the_job_pushes_pivotal_branches_and_opens_drafts_and_does_nothing_else(self):
        out = self.assembled("one", "two")
        self.new_prs(out)
        before = set(self.remote_refs())
        code, _, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertEqual({tuple(a[:2]) for a in self.calls()}, {("pr", "list"), ("pr", "create")})
        creates = self.creates()
        self.assertEqual(len(creates), 2)
        for args in creates:
            self.assertIn("--draft", args)
            self.assertNotIn("--web", args)
        self.assertEqual(set(self.remote_refs()) - before, {f"refs/heads/{BRANCH}one", f"refs/heads/{BRANCH}two"})
        self.assertEqual({r for r in before} - set(self.remote_refs()), set(), "no ref was removed")


class CorrectionRound1Test(PublishCase):
    """The review's findings 1 and 3 to 9 (finding 2 is an operator decision, pinned in UntrustedRunTest)."""

    def test_a_full_pr_list_answer_is_refused_as_possibly_cut_off(self):
        """Finding 1: gh never returns more than --limit, so an answer of exactly PR_LIMIT items may be a cut one."""
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        forks = [self.pr(BRANCH + "one", "f" * 40, n, isCrossRepository=True) for n in range(1, publish.PR_LIMIT + 1)]
        self.assertEqual(len(forks), 20)
        self.route_list(self.gh, BRANCH + "one", forks, forks + [self.pr(BRANCH + "one", head, 40)])
        self.route_create(self.gh, BRANCH + "one", 40)
        before = self.remote_refs()
        code, _, _, err = self.publish(out)
        self.assertEqual(code, 1)
        self.assertIn("slice one: failed (gh_failed)", err)
        self.assertEqual(self.creates(), [], "no second draft is opened behind a full page")
        self.assertEqual(self.remote_refs(), before)

    def test_an_answer_one_short_of_the_limit_is_still_read(self):
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        forks = [self.pr(BRANCH + "one", "f" * 40, n, isCrossRepository=True) for n in range(1, publish.PR_LIMIT - 1)]
        self.route_list(self.gh, BRANCH + "one", forks, forks + [self.pr(BRANCH + "one", head, 40)])
        self.route_create(self.gh, BRANCH + "one", 40)
        self.assertEqual(self.publish(out)[0], 0)
        self.assertEqual(len(self.creates()), 1)

    def test_a_duplicate_ref_in_the_bundle_header_is_refused(self):
        """Finding 3."""
        sha = "a" * 40
        header = f"# v2 git bundle\n-{sha}\n{sha} refs/heads/x\n{'b' * 40} refs/heads/x\n\n".encode()
        with self.assertRaisesRegex(assemble.LandError, "twice"):
            assemble.bundle_header(header)
        ok = f"# v2 git bundle\n-{sha}\n{sha} refs/heads/x\n{'b' * 40} refs/heads/y\n\n".encode()
        self.assertEqual(assemble.bundle_header(ok)[1], {"refs/heads/x": sha, "refs/heads/y": "b" * 40})

    def test_every_gh_call_runs_with_a_fixed_path(self):
        """Finding 5: the step's PATH (and a directory a step put on it) never reaches gh."""
        out = self.assembled("one")
        self.new_prs(out)
        code, _, _, err = self.publish(out, PATH="/opt/evil:/usr/bin")
        self.assertEqual(code, 0, err)
        paths = {e["PATH"] for e in self.gh.environments()}
        self.assertEqual(paths, {"/usr/bin:/bin"})

    def test_an_open_pull_request_wins_over_a_closed_one_of_an_earlier_head(self):
        """Finding 7: a closed PR at head A (number 3) and the open one at head B (number 9) is the open one."""
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        old = self.pr(BRANCH + "one", "a" * 40, 3, state="CLOSED", isDraft=False)
        self.route_list(self.gh, BRANCH + "one", [old, self.pr(BRANCH + "one", head, 9)])
        code, outputs, stdout, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertIn("slice one: exists (#9)", stdout)
        self.assertEqual(self.creates(), [])
        self.assertEqual(report.parse_result(outputs["result"]).pr, 9)

    def test_a_closed_pull_request_of_an_earlier_head_is_history_and_a_new_draft_is_opened(self):
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        old = self.pr(BRANCH + "one", "a" * 40, 3, state="CLOSED", isDraft=False)
        self.route_list(self.gh, BRANCH + "one", [old], [old, self.pr(BRANCH + "one", head, 9)])
        self.route_create(self.gh, BRANCH + "one", 9)
        code, _, stdout, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertIn("slice one: opened (#9)", stdout)

    def test_a_pull_request_that_is_closed_is_not_reported_as_an_open_draft(self):
        """Finding 8."""
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        for state in ("CLOSED", "MERGED"):
            gh = fakegh.FakeGh(self.root / f"bin-{state}")
            self.route_list(gh, BRANCH + "one", [self.pr(BRANCH + "one", head, 9, state=state, isDraft=False)])
            code, outputs, _, err = self.publish(out, gh)
            self.assertEqual(code, 0, err)
            outcome = report.parse_result(outputs["result"])
            self.assertEqual((outcome.status, outcome.why, outcome.pr), ("draft", "pr_closed", 9), state)
            self.assertNotIn("is open", outcome.next_step)
        open_one = fakegh.FakeGh(self.root / "bin-open")
        self.route_list(open_one, BRANCH + "one", [self.pr(BRANCH + "one", head, 9)])
        self.assertEqual(report.parse_result(self.publish(out, open_one)[1]["result"]).why, "verdict_missing")  # R6: the cause is kept, not no_stack

    def test_a_title_or_body_that_holds_the_token_is_never_sent(self):
        """Finding 9."""
        out = self.assembled("one")
        self.new_prs(out)
        before = self.remote_refs()
        matrix = plan.matrix_json([plan.Slice("one", TOKEN, "goal one")])
        code, _, _, err = self.publish(out, MATRIX=matrix)
        self.assertEqual(code, 1)
        self.assertIn("secret_in_text", err)
        self.assertEqual(self.creates(), [])
        self.assertNotIn(TOKEN, json.dumps(self.gh.calls()))
        self.assertEqual(self.remote_refs(), before, "nothing was pushed")
        self.assertEqual({tuple(a[:2]) for a in self.calls()}, {("pr", "list")})


class FallBackResultTest(unittest.TestCase):
    """The RESULT carries every slice's fall-through items, in slice order, as report.parse_result reads them: at most
    report.FELL_BACK_MAX (more is a bad result there), and no key at all when there is none."""

    BUILD = {"from": "pi:kaggle/A", "to": "claude:anthropic/sonnet", "reason": "endpoint down"}
    REVIEW = {"from": "agy:google/gemini", "to": "codex:openai/gpt-5", "reason": "rate-limited"}

    def land(self, *slices: tuple) -> dict:
        """slices of (id, outcome, reason, fell_back)."""
        return {"slices": [{"id": i, "outcome": o, "reason": r, "built_by": "claude:anthropic/sonnet", "fell_back": f}
                           for i, o, r, f in slices]}

    def test_a_draft_result_carries_the_items(self):
        done = {"one": {"status": "opened", "pr": 5, "state": "OPEN"}}
        line = json.loads(publish.result_line(self.land(("one", "draft", "verdict_needs_fixes", [self.BUILD])), done, {}))
        self.assertEqual((line["status"], line["why"]), ("draft", "review_rejected"))
        self.assertEqual(line["fell_back"], [self.BUILD])

    def test_a_failure_carries_the_items(self):
        line = json.loads(publish.result_line(self.land(("one", "failed", "gate_red", [self.BUILD])), {}, {}))
        self.assertEqual((line["status"], line["why"]), ("failed", "gate_red"))
        self.assertEqual(line["fell_back"], [self.BUILD])

    def test_every_slices_items_follow_in_slice_order(self):
        land = self.land(("one", "failed", "gate_red", [self.BUILD]), ("two", "draft", "verdict_missing", [self.REVIEW]))
        line = json.loads(publish.result_line(land, {}, {}))
        self.assertEqual(line["fell_back"], [self.BUILD, self.REVIEW])

    def test_six_slices_with_a_build_and_a_review_fall_through_each_keep_all_twelve_items(self):
        # R4: the reachable case (F8: six slices, a build and a review item each, here two of each) loses nothing.
        slices = [(f"s{n}", "failed", "gate_red", [self.BUILD, self.REVIEW]) for n in range(6)]
        text = publish.result_line(self.land(*slices), {}, {})
        self.assertEqual(len(json.loads(text)["fell_back"]), 12)
        self.assertNotIn("fell_back_more", json.loads(text))
        outcome = report.parse_result(text)
        self.assertEqual((len(outcome.fell_back), outcome.fell_back_more), (12, 0))

    def test_items_beyond_the_cap_are_counted_not_silently_dropped(self):
        many = [self.BUILD] * (report.FELL_BACK_MAX + 7)
        text = publish.result_line(self.land(("one", "failed", "gate_red", many)), {}, {})
        data = json.loads(text)
        self.assertEqual((len(data["fell_back"]), data["fell_back_more"]), (report.FELL_BACK_MAX, 7))
        outcome = report.parse_result(text)
        self.assertEqual((outcome.status, outcome.why, len(outcome.fell_back), outcome.fell_back_more),
                         ("failed", "gate_red", report.FELL_BACK_MAX, 7))

    def test_no_items_no_key(self):
        line = json.loads(publish.result_line(self.land(("one", "failed", "gate_red", [])), {}, {}))
        self.assertNotIn("fell_back", line)


class ResultLineTest(unittest.TestCase):
    def land(self, outcome: str, reason: str) -> dict:
        return {"slices": [{"id": "one", "outcome": outcome, "reason": reason, "built_by": "claude:anthropic/sonnet", "fell_back": []}]}

    def test_a_draft_reason_that_stopped_the_stack_keeps_its_own_word(self):
        done = {"one": {"status": "opened", "pr": 5, "state": "OPEN"}}
        for reason, why in (("baseline_red", "baseline_red"), ("gate_red", "gate_red"), ("verdict_needs_fixes", "review_rejected"),
                            ("verdict_missing", "verdict_missing"), ("verdict_invalid", "verdict_invalid"),
                            ("verdict_stale", "verdict_stale"), ("verdict_wrong_sha", "verdict_wrong_sha"),
                            ("verdict_files", "verdict_files"), ("verdict_wrong_builder", "verdict_wrong_builder"),
                            ("conflict", "conflict"), ("stack_differs", "stack_differs"), ("multi_commit", "multi_commit"),
                            ("gate_none", "gate_none"), ("fix_round", "fix_round"),
                            ("some_new_reason", "no_stack")):
            self.assertEqual(json.loads(publish.result_line(self.land("draft", reason), done, {}))["why"], why, reason)

    def test_every_draft_word_has_its_own_report_text_and_none_claims_nothing_reviewed_it(self):
        for why in set(publish.DRAFT_WHY.values()):
            self.assertIn(why, report.WHY, why)
        for why in ("conflict", "stack_differs", "multi_commit", "verdict_missing", "gate_none"):
            self.assertNotIn("nothing has reviewed", report.WHY[why], why)
        self.assertIn("not rechecked", report.WHY["fix_round"])

    def test_n10_a_candidate_outside_a_stack_is_not_a_land_json_so_candidate_only_is_never_written(self):
        """It was documented as reachable by a hand-made land.json; check_land makes `positions` exactly the candidates."""
        data = {"version": 2, "issue": ISSUE, "base_sha": "a" * 40, "baseline": "green", "has_stack": False, "positions": [],
                "bundle": {"name": "pivotal.bundle", "sha256": "b" * 64, "bytes": 5},
                "slices": [{"id": "one", "outcome": "candidate", "reason": "stacked", "candidate": True, "branch": f"pivotal/issue-{ISSUE}/stack",
                            "head": "c" * 40, "commits": ["c" * 40], "built_by": "claude:anthropic/sonnet", "gate": "green", "fell_back": [],
                            "score": None, "target": "", "reviewed_by": ""}]}
        with self.assertRaises(assemble.LandError):
            assemble.check_land(data)
        line = json.loads(publish.result_line(self.land("candidate", "stacked"), {}, {}))
        self.assertEqual((line["status"], line["why"]), ("failed", "build_failed"))

    def test_a_leaking_slice_is_the_answer_whatever_its_siblings_did(self):
        """Round 1, F1: one RESULT line for N slices, and a secret in any of them must not be hidden."""
        def slc(name: str, outcome: str, reason: str) -> dict:
            return {"id": name, "outcome": outcome, "reason": reason, "built_by": f"w:{name}", "fell_back": []}

        opened = {"fine": {"status": "opened", "pr": 5, "state": "OPEN"}}
        cases = (
            ([slc("leaky", "failed", "secret_in_patch"), slc("fine", "draft", "verdict_needs_fixes")], opened, {},
             "secret_in_patch"),
            ([slc("fine", "draft", "verdict_needs_fixes"), slc("leaky", "failed", "secret_in_patch")], opened, {},
             "secret_in_patch"),
            ([slc("cand", "candidate", "stacked"), slc("leaky", "failed", "secret_in_patch")], {}, {}, "secret_in_patch"),
            ([slc("plain", "failed", "no_route"), slc("nosecret", "failed", "no_secret")], {}, {}, "no_secret"),
            ([slc("nosecret", "failed", "no_secret"), slc("leaky", "failed", "secret_in_patch")], {}, {}, "secret_in_patch"),
            ([slc("cand", "candidate", "stacked"), slc("plain", "failed", "no_route")], {}, {}, "no_route"),
            ([slc("leaky", "failed", "secret_in_patch"), slc("stale", "draft", "verdict_needs_fixes")], {},
             {"stale": "branch_stale"}, "secret_in_patch"),
        )
        for slices, done, failures, why in cases:
            with self.subTest(why=why, slices=[x["id"] for x in slices]):
                line = json.loads(publish.result_line({"slices": slices}, done, failures))
                self.assertEqual((line["status"], line["why"]), ("failed", why))
                self.assertEqual(report.parse_result(json.dumps(line)).status, "failed")

    def test_a_failure_is_the_answer_whatever_the_order_of_the_slices(self):
        """Round 2, R1: an open draft, a closed pull request or a candidate never hides a plain failure."""
        def slc(name: str, outcome: str, reason: str) -> dict:
            return {"id": name, "outcome": outcome, "reason": reason, "built_by": f"w:{name}", "fell_back": []}

        opened = {"d": {"status": "opened", "pr": 5, "state": "OPEN"}}
        closed = {"d": {"status": "exists", "pr": 5, "state": "CLOSED"}}
        draft = slc("d", "draft", "verdict_needs_fixes")
        cases = (
            (slc("f", "failed", "no_route"), opened, "no_route"),
            (slc("f", "failed", "gate_red"), opened, "gate_red"),
            (slc("f", "blocked", "workflow_change"), opened, "workflow_change"),
            (slc("f", "conflict", "does_not_apply"), opened, "conflict"),
            (slc("f", "failed", "no_route"), closed, "no_route"),
            (slc("f", "blocked", "workflow_change"), {}, "workflow_change"),
        )
        for other, done, why in cases:
            for pair in ((draft, other), (other, draft)):
                with self.subTest(why=why, order=[x["id"] for x in pair], done=done):
                    line = json.loads(publish.result_line({"slices": list(pair)}, done, {}))
                    self.assertEqual((line["status"], line["why"]), ("failed", why))
        cand = slc("c", "candidate", "stacked")
        for pair in ((cand, cases[2][0]), (cases[2][0], cand)):
            with self.subTest(order=[x["id"] for x in pair]):
                line = json.loads(publish.result_line({"slices": list(pair)}, {}, {}))
                self.assertEqual((line["status"], line["why"]), ("failed", "workflow_change"))
        # a publish failure of a draft slice also outranks a sibling's draft, in either order
        sibling = slc("e", "draft", "verdict_needs_fixes")
        for pair in ((draft, sibling), (sibling, draft)):
            line = json.loads(publish.result_line({"slices": list(pair)}, {"d": opened["d"]}, {"e": "branch_stale"}))
            self.assertEqual((line["status"], line["why"]), ("failed", "branch_stale"))


class CorrectionRound2Test(PublishCase):
    """The review's findings N1 to N4 (round 2)."""

    def title_matrix(self, **titles: str) -> str:
        return plan.matrix_json([plan.Slice(i, titles.get(i, f"title {i}"), f"goal {i}") for i in ("one", "two")])

    def test_n1_a_non_ascii_title_on_either_slice_still_opens_its_draft(self):
        for where in ("one", "two"):
            with self.subTest(where=where):
                self.setUp()
                out = self.assembled("one", "two")
                self.new_prs(out)
                code, outputs, stdout, err = self.publish(out, MATRIX=self.title_matrix(**{where: "Café support \u2603 日本"}))
                self.assertEqual(code, 0, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(len(self.creates()), 2)
                self.assertIn("slice one: opened", stdout)
                self.assertIn("slice two: opened", stdout)
                self.assertTrue(outputs["result"])

    def test_n1_an_unexpected_error_after_the_push_is_that_slices_failure_and_the_run_goes_on(self):
        out = self.assembled("one", "two")
        self.new_prs(out)
        real = assemble.write_new
        calls = []

        def boom(path, data):
            calls.append(path)
            if len(calls) == 1:
                raise RuntimeError("unexpected")
            return real(path, data)

        with mock.patch.object(assemble, "write_new", boom):
            code, outputs, stdout, err = self.publish(out)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("slice one: failed (unexpected)", err)
        self.assertIn("slice two: opened", stdout)
        self.assertIn(f"refs/heads/{BRANCH}one", self.remote_refs(), "the push of the failed slice is on record")
        self.assertEqual(report.parse_result(outputs["result"]).pr, 12, "the result is still written")

    def test_n2_a_branch_left_at_an_old_head_by_a_closed_pull_request_is_refused_by_name(self):
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        old = self.git("commit-tree", f"{self.base}^{{tree}}", "-p", self.base, "-m", "the earlier head")
        self.git("push", "-q", str(self.bare), f"{old}:refs/heads/{BRANCH}one")
        closed = self.pr(BRANCH + "one", old, 3, state="CLOSED", isDraft=False)
        self.route_list(self.gh, BRANCH + "one", [closed])
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 1)
        self.assertIn("slice one: failed (branch_stale)", err)
        self.assertEqual(self.remote_refs()[f"refs/heads/{BRANCH}one"], old, "never forced")
        self.assertEqual([r for r in self.remote_refs() if r != "refs/heads/main"], [f"refs/heads/{BRANCH}one"], "no new branch name")
        self.assertEqual(self.creates(), [])
        outcome = report.parse_result(outputs["result"])
        self.assertEqual((outcome.status, outcome.why), ("failed", "branch_stale"))
        self.assertIn("delete that branch", outcome.next_step)
        self.assertNotIn("start a new run", report.WHY["pr_closed"])
        # a branch at another commit with no pull request at all is still a stranger's
        gh = fakegh.FakeGh(self.root / "bin-plain")
        self.route_list(gh, BRANCH + "one", [])
        code, outputs, _, err = self.publish(out, gh)
        self.assertIn("slice one: failed (branch_differs)", err)
        self.assertEqual(report.parse_result(outputs["result"]).why, "branch_differs")
        self.assertNotEqual(head, old)

    def test_n3_a_candidate_without_a_gate_artifact_is_not_landable_and_not_a_publish_failure(self):
        """Was `candidate_only` before the stack ticket; the stack is now judged, and a missing gate ends the prefix at 0."""
        self.ready("one", {"a.txt": "x\n"})
        self.run_job("one")
        out = Path(self.last_env["OUT_DIR"])
        self.assertEqual(assemble.read_land(out / assemble.LAND)["slices"][0]["outcome"], "candidate")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        self.route_list(self.gh, f"pivotal/issue-{ISSUE}/stack", [], [self.pr(f"pivotal/issue-{ISSUE}/stack", head, 20)])
        self.gh.route(f"pr create --repo {REPO} --draft --base main --head pivotal/issue-{ISSUE}/stack ",
                      fakegh.ok(f"https://github.com/{REPO}/pull/20\n"))
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        outcome = report.parse_result(outputs["result"])
        self.assertEqual((outcome.status, outcome.why, outcome.pr), ("draft", "not_landable", 20), "the stack is a draft PR now (F-2)")

    def test_n4_a_created_url_is_the_answer_even_when_the_list_is_full(self):
        out = self.assembled("one")
        full = [self.pr(BRANCH + "one", "f" * 40, n, isCrossRepository=True) for n in range(1, publish.PR_LIMIT + 1)]
        self.route_list(self.gh, BRANCH + "one", [], full)
        self.route_create(self.gh, BRANCH + "one", 77)
        code, outputs, stdout, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertIn("slice one: opened (#77)", stdout)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(report.parse_result(outputs["result"]).pr, 77)
        self.assertEqual(len(self.calls(what="pr list")), 1, "no lookup after the create")

    def test_n4_a_created_url_of_another_repository_is_not_trusted(self):
        out = self.assembled("one")
        head = assemble.read_land(out / assemble.LAND)["slices"][0]["head"]
        self.route_list(self.gh, BRANCH + "one", [], [self.pr(BRANCH + "one", head, 11)])
        self.gh.route(f"pr create --repo {REPO} ", fakegh.ok("https://github.com/other/repo/pull/5\n"))
        code, outputs, _, err = self.publish(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(report.parse_result(outputs["result"]).pr, 11)


if __name__ == "__main__":
    unittest.main()
