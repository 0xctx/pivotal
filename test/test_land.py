"""The land job (F4): engine/land.py and .github/workflows/land.yml.

The stack is the real thing: the real assemble job over hand-made slices, published by the real publish job at the `prepare`
level into a bare "remote" on disk (the stack branch and its ready pull request). The land job then runs in-process against that
remote with a fake gh (test/fakegh.py, strict about the flags of the real gh) that serves the REST objects the job reads, in
the shapes the API documents. Nothing here has run on real GitHub: branch protection, a merge queue, the check suite
attachment, the app slug and real pagination are unverified, and `gh run download` is a fake that copies a directory.
"""

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fakegh
from test_assemble import ISSUE
from test_caller import parse
from test_publish import REPO, TOKEN
from test_stack import RUN, STACK, SUITE, StackCase

import nolive  # noqa: F401  (no test sweeps the live /proc)
from engine import assemble, buildjob, fix, job, land, publish, seal, trust

PR = 20
# The linked issue's text: the factory pins this (trust.issue_pin), so the land job's re-gate pins the same text.
ISSUE_TEXT = {"title": "Ship the stack", "body": "The goal of the issue.\n"}
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
LAND_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "land.yml"


class LandCase(StackCase):
    def world(self, *ids: str) -> None:
        """A published stack: one READY slice per id, the stack branch on the remote and its ready pull request #20."""
        ids = ids or ("one", "two")
        self.out = self.stacked(*ids)
        heads = self.heads(self.out)
        self.sha = heads[ids[-1]]
        self.heads_ = heads
        self.route_stack_pr(self.gh, self.sha, PR)
        self.route_seal(self.gh, self.sha)  # prepare seals the READY stack PR (prepare-seal contract)
        code, _, _, err = self.publish(self.out, LEVEL="prepare")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.base)
        self.land = assemble.read_land(self.out / assemble.LAND)
        self.land_sha = hashlib.sha256((self.out / assemble.LAND).read_bytes()).hexdigest()
        self.n_lands = 0
        self.w = {
            "actor": "maintainer", "perms": {"maintainer": "write", "alice": "write"}, "on_pr": False, "event_body": "/pivotal land",
            "issue": {"number": ISSUE, "state": "open", **ISSUE_TEXT}, "open_prs": [self.pr(STACK, self.sha, PR, isDraft=False)],
            "pr": self.pr_json(), "check_runs": [self.seal_run()], "check_pages": None, "runs": {RUN: self.run_object()},
            "artifact_pages": None, "artifacts": self.artifacts(), "land_dir": self.out,
            "gate_dirs": {i: self.gates / f"gate-{i}" for i in ids}, "merge": None, "level": "prepare", "default": "main",
            "dispatch": None, "protected": False, "labels": [{"name": "pivotal:ready"}],
        }

    def protect_main(self) -> None:
        """The base refuses the push (a hook) and the branches API says it is protected."""
        super().protect_main()
        self.w["protected"] = True

    # --- real merge commits, made by hand in the bare remote (the fake gh runs the server side as an effect)

    def tree(self, rev: str) -> str:
        return self.git("rev-parse", f"{rev}^{{tree}}", cwd=self.bare)

    def forge(self, tree: str, *parents: str, message: str = "forged") -> str:
        env = {"GIT_AUTHOR_DATE": "2024-05-05T05:05:05 +0000", "GIT_COMMITTER_DATE": "2024-05-05T05:05:05 +0000"}
        args = ["commit-tree", tree, *[x for p in parents for x in ("-p", p)], "-m", message]
        return self.git(*args, cwd=self.bare, env=env)

    def merged(self, commit: str, **response) -> dict:
        """What the merge endpoint answers when it made ``commit`` the tip of main."""
        body = {"sha": commit, "merged": True, "message": "Pull Request successfully merged", **response}
        return fakegh.ok(json.dumps(body), effect=["git", "-C", str(self.bare), "update-ref", "refs/heads/main", commit])

    def genuine_merge(self) -> dict:
        return self.merged(self.forge(self.tree(self.sha), self.base, self.sha))

    # --- the REST objects

    def pr_json(self, **over) -> dict:
        data = {
            "number": PR, "state": "open", "draft": False, "merged": False, "body": f"Closes #{ISSUE}\n\nThe stack.\n",
            "head": {"ref": STACK, "sha": self.sha, "repo": {"full_name": REPO}}, "base": {"ref": "main", "repo": {"full_name": REPO}},
        }
        data.update(over)
        return data

    def run_object(self, **over) -> dict:
        return {"id": RUN, "path": ".github/workflows/pivotal.yml", "check_suite_id": SUITE, "repository": {"full_name": REPO},
                "head_branch": "main", "event": "issue_comment", **over}

    def seal_run(self, sha: str | None = None, run: int = RUN, summary: str | None = None, **over) -> dict:
        sha = sha or self.sha
        ids = self.land["positions"]
        text = summary if summary is not None else seal.summary(self.land_sha, ids[: ids.index(self.at(sha)) + 1] if self.at(sha) in ids else ids)
        return self.check_run(sha, run, output={"title": seal.TITLE, "summary": text}, **over)

    def at(self, sha: str) -> str:
        return next((i for i, h in self.heads_.items() if h == sha), "")

    def artifacts(self, *names: str, run: int = RUN, **over) -> list[dict]:
        wanted = names or ("land", *(f"gate-{i}" for i in self.land["positions"]))
        return [{"name": n, "expired": False, "workflow_run": {"id": run}, "size_in_bytes": 1000, **over} for n in wanted]

    # --- running

    def wire(self) -> fakegh.FakeGh:
        w, g = self.w, fakegh.FakeGh(self.root / f"land-bin{self.n_lands}")
        ok = fakegh.ok
        for login, perm in w["perms"].items():
            g.route(f"api repos/{REPO}/collaborators/{login}/permission", ok(json.dumps({"permission": perm})))
        g.route(f"api repos/{REPO}", ok(json.dumps({"default_branch": w["default"]})), exact=True)
        number = PR if w["on_pr"] else ISSUE
        issue = {"number": PR, "pull_request": {"url": "https://example.invalid"}} if w["on_pr"] else w["issue"]
        g.route(f"api repos/{REPO}/issues/{number}", ok(json.dumps(issue)), exact=True)
        g.route(f"pr list --repo {REPO} --head {STACK} ", ok(json.dumps(w["open_prs"])))
        g.route(f"api repos/{REPO}/pulls/{PR}", ok(json.dumps(w["pr"])), exact=True)
        head = w["pr"]["head"]["sha"] if isinstance(w["pr"], dict) and isinstance(w["pr"].get("head"), dict) else self.sha
        runs = w["check_runs"]
        pages = w["check_pages"] if w["check_pages"] is not None else [{"total_count": len(runs), "check_runs": runs}]
        g.route(f"api --paginate --slurp repos/{REPO}/commits/{head}/check-runs?check_name=pivotal%2Fgate&filter=all&per_page=100",
                ok(json.dumps(pages)), exact=True)
        for run_id, answer in w["runs"].items():
            g.route(f"api repos/{REPO}/actions/runs/{run_id}", answer if "stdout" in answer else ok(json.dumps(answer)), exact=True)
            listing = w["artifact_pages"] if w["artifact_pages"] is not None else [{"total_count": len(w["artifacts"]), "artifacts": w["artifacts"]}]
            g.route(f"api --paginate --slurp repos/{REPO}/actions/runs/{run_id}/artifacts?per_page=100", ok(json.dumps(listing)), exact=True)
            g.route(f"run download {run_id} --repo {REPO} --name land --dir ", ok(copy=w["land_dir"]))
            for ident, directory in w["gate_dirs"].items():
                g.route(f"run download {run_id} --repo {REPO} --name gate-{ident} --dir ", ok(copy=directory))
        g.route(f"api repos/{REPO}/branches/main", ok(json.dumps({"name": "main", "protected": w["protected"]})) if w["protected"] != "404" else fakegh.not_found(), exact=True)
        g.route(f"api --paginate --slurp repos/{REPO}/issues/{ISSUE}/labels?per_page=100", ok(json.dumps([w["labels"]])), exact=True)
        g.route(f"api --method POST repos/{REPO}/issues/{ISSUE}/comments", w.get("comment") or ok())
        g.route(f"api --method DELETE repos/{REPO}/issues/{ISSUE}/labels/pivotal%3Aready", w.get("delete") or ok())
        g.route(f"api --method POST repos/{REPO}/issues/{ISSUE}/labels", ok())
        g.route(f"api --method PATCH repos/{REPO}/issues/{ISSUE}", ok())
        g.route(f"api --method POST repos/{REPO}/dispatches", w["dispatch"] or ok())
        if w["merge"] is not None:
            g.route(f"api --method PUT repos/{REPO}/pulls/{PR}/merge --raw-field sha={self.sha} --raw-field merge_method=merge", w["merge"])
        return g

    def go(self, **over) -> tuple[int, dict, str, str]:
        """Run the land job in-process. ``over`` replaces environment variables (None removes one)."""
        self.n_lands += 1
        self.lgh = self.wire()
        run = self.root / f"land{self.n_lands}"
        run.mkdir()
        event = {
            "action": "created", "issue": {"number": PR if self.w["on_pr"] else ISSUE, "user": {"login": "alice"}},
            "comment": {"body": self.w["event_body"], "user": {"login": self.w["actor"]}},
        }
        if self.w["on_pr"]:
            event["issue"]["pull_request"] = {"url": "https://example.invalid"}
        (run / "event.json").write_text(json.dumps(event))
        (run / "output").write_text("")
        env = {
            "PATH": os.environ.get("PATH", ""), "PIV_GH": str(self.lgh.path), "GH_TOKEN": TOKEN, "GH_REPO": REPO,
            "ISSUE": str(ISSUE), "LEVEL": self.w["level"],  # the issue (for a pull request, the one it closes: trust's output, I4)
            "WORK_DIR": str(run / "work"), "GITHUB_OUTPUT": str(run / "output"), "GITHUB_EVENT_NAME": "issue_comment",
            "GITHUB_EVENT_PATH": str(run / "event.json"), "GITHUB_SERVER_URL": f"file://{self.server}",
        }
        env.update(over)
        env = {k: v for k, v in env.items() if v is not None}
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = job.run_job(land.land, env)
        outputs = dict(line.split("=", 1) for line in (run / "output").read_text().splitlines())
        return code, outputs, stdout.getvalue(), stderr.getvalue()

    def writes(self) -> list[list[str]]:
        found = []
        for call in self.lgh.calls():
            args = call["args"]
            if args[:1] == ["api"] and "--method" in args and args[args.index("--method") + 1] in WRITE_METHODS:
                found.append(args)
        return found

    def bodies(self) -> list[str]:
        return [a[a.index("--raw-field") + 1] for a in self.writes() if a[3:4] != [] and a[3].endswith("/comments")]

    def downloads(self) -> list[list[str]]:
        return [c["args"] for c in self.lgh.calls() if c["args"][:2] == ["run", "download"]]

    def refuses(self, text: str, **env) -> str:
        """The run refuses before it writes anything: no gh write, nothing pushed, no output."""
        before = self.remote_refs()
        code, outputs, _, err = self.go(**env)
        self.assertEqual(code, 1, err)
        self.assertIn("refused:", err)
        self.assertIn(text, err)
        self.assertEqual(self.writes(), [], "no write to GitHub")
        self.assertEqual(self.remote_refs(), before, "nothing pushed")
        self.assertEqual(outputs, {})
        return err

    def spy_push(self):
        pushes = []
        original = publish.Remote.network

        def spy(git, *args):
            if args[:1] == ("push",):
                pushes.append(list(args))
            return original(git, *args)

        return pushes, mock.patch.object(publish.Remote, "network", spy)


class StepOneTest(LandCase):
    """1. Map the trigger to the issue's ready pull request."""

    def test_an_issue_comment_lands_the_open_ready_stack_pull_request_of_that_issue(self):
        self.world()
        code, outputs, _, err = self.go()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.sha)
        self.assertEqual(outputs, {"land_outcome": "landed", "sealed_sha": self.sha})

    def test_a_pull_request_comment_uses_the_closes_line_of_the_pull_request(self):
        self.world()
        self.w["on_pr"] = True
        code, outputs, _, err = self.go()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.sha)
        self.assertEqual(outputs["land_outcome"], "landed")
        self.assertTrue(all(f"issues/{ISSUE}" in " ".join(a) or f"pulls/{PR}" in " ".join(a) for a in self.writes()), "the issue is the one the body closes")
        self.assertIn(f"repos/{REPO}/issues/{ISSUE}", " ".join(self.writes()[0]))

    def test_a_pull_request_body_with_crlf_line_endings_lands_by_its_closes_line(self):
        """S1: GitHub stores a body typed in the browser with CRLF; the refusal row for two CRLF lines shares its message with
        zero lines, so only a happy path tells a CRLF line that is read from one that is not."""
        self.world()
        self.w["on_pr"] = True
        self.w["pr"] = self.pr_json(body=f"The stack.\r\n\r\nCloses #{ISSUE}\r\n")
        code, outputs, _, err = self.go()
        self.assertEqual(code, 0, err)
        self.assertEqual(outputs["land_outcome"], "landed")

    def test_a_stranger_is_ignored_with_a_visible_reason_and_no_write(self):
        self.world()
        for perm in ("read", "none", "triage", "banana"):
            with self.subTest(perm=perm):
                self.w["perms"]["maintainer"] = perm
                self.refuses("not a trusted /pivotal land")

    def test_a_failed_permission_lookup_is_a_stranger(self):
        self.world()
        self.w["perms"] = {}
        self.refuses("not a trusted /pivotal land")

    def test_an_event_that_is_not_a_land_comment_for_this_issue_is_refused(self):
        self.world()
        for body in ("/pivotal go", "/pivotal stop", "looks good", "please /pivotal land"):
            with self.subTest(body=body):
                self.w["event_body"] = body
                self.refuses("not a trusted /pivotal land")
        self.w["event_body"] = "/pivotal land"
        self.refuses("not a trusted /pivotal land", GITHUB_EVENT_NAME="issues")
        self.refuses("not a trusted /pivotal land", ISSUE=str(ISSUE + 1))
        self.refuses("GITHUB_EVENT_PATH", GITHUB_EVENT_PATH=None)

    def test_a_land_comment_on_an_issue_of_a_stranger_needs_a_writer_author_or_the_explicit_phrase(self):
        self.world()
        self.w["perms"]["alice"] = "read"
        self.refuses("not a trusted /pivotal land")

    def test_a_draft_pull_request_is_never_landable(self):
        self.world()
        self.w["pr"] = self.pr_json(draft=True)
        self.w["open_prs"] = [self.pr(STACK, self.sha, PR, isDraft=True)]
        self.refuses("a draft is never landable")
        self.w["on_pr"] = True
        self.refuses("a draft is never landable")

    def test_no_pull_request_two_pull_requests_or_a_closed_one_is_a_refusal(self):
        self.world()
        self.w["open_prs"] = []
        self.refuses("has 0 open pull requests")
        self.w["open_prs"] = [self.pr(STACK, self.sha, PR), self.pr(STACK, self.sha, PR + 1)]
        self.refuses("has 2 open pull requests")
        self.w["open_prs"] = [self.pr(STACK, self.sha, PR, state="CLOSED"), self.pr(STACK, self.sha, PR + 1, state="MERGED")]
        self.refuses("has 0 open pull requests")
        self.w["open_prs"] = [self.pr(STACK, self.sha, PR)]
        for change in ({"state": "closed"}, {"merged": True, "state": "closed"}):
            self.w["pr"] = self.pr_json(**change)
            self.refuses("is not open")

    def test_a_pull_request_that_is_not_the_stack_of_its_issue_is_refused(self):
        self.world()
        self.w["on_pr"] = True
        slice_branch = f"pivotal/issue-{ISSUE}/one"
        for change, text in (
            ({"head": {"ref": slice_branch, "sha": self.sha, "repo": {"full_name": REPO}}}, "only a stack pull request lands"),
            ({"head": {"ref": f"pivotal/issue-{ISSUE + 1}/stack", "sha": self.sha, "repo": {"full_name": REPO}}}, "only a stack pull request lands"),
            ({"head": {"ref": STACK, "sha": self.sha, "repo": {"full_name": "evil/fork"}}}, "from and into this repository"),
            ({"head": {"ref": STACK, "sha": self.sha, "repo": None}}, "shape the REST API answers"),
            ({"base": {"ref": "release", "repo": {"full_name": REPO}}}, "not based on the default branch"),
            ({"base": {"ref": "main", "repo": {"full_name": "evil/fork"}}}, "from and into this repository"),
            ({"body": "no closing line"}, "Closes #<issue> on one line"),
            ({"body": f"Closes #{ISSUE}\nCloses #{ISSUE + 1}\n"}, "exactly once"),
            ({"body": f"Closes #{ISSUE}\r\nCloses #{ISSUE + 1}\r\n"}, "exactly once"),
            ({"body": f"Closes #{ISSUE} and more"}, "Closes #<issue> on one line"),
            ({"body": None, "state": "open"}, "Closes #<issue> on one line"),
            ({"head": {"ref": STACK, "sha": "abc", "repo": {"full_name": REPO}}}, "shape the REST API answers"),
            ({"draft": "false"}, "shape the REST API answers"),
        ):
            with self.subTest(change=change):
                self.w["pr"] = self.pr_json(**change)
                self.refuses(text)

    def test_the_issue_route_refuses_a_pull_request_that_closes_another_issue(self):
        self.world()
        self.w["pr"] = self.pr_json(body=f"Closes #{ISSUE + 1}\n")
        self.refuses("only a stack pull request lands")

    def test_a_pull_request_comment_lands_only_the_issue_the_pull_request_closes(self):
        """I4: the job is given the issue, and the pull request must close it. A pull request that closes another refuses,
        and nothing is written (trust decides it first, from the same Closes line)."""
        self.world()
        self.w["on_pr"] = True
        self.w["pr"] = self.pr_json(body=f"Closes #{ISSUE + 1}\n")
        self.refuses("not a trusted /pivotal land", ISSUE=str(ISSUE))

    def test_a_pull_request_that_moved_to_close_another_issue_after_trust_read_it_is_refused(self):
        """I4: trust read the pull request once; the land job reads it again. If the second read closes another issue
        (the body was edited between the two), the job refuses and writes nothing: the group it was queued in was the first's."""
        self.world()
        self.w["on_pr"] = True
        other = self.pr_json()["head"] | {"ref": assemble.stack_branch(ISSUE + 1)}
        self.w["pr"] = self.pr_json(body=f"Closes #{ISSUE + 1}\n", head=other)  # a valid stack pull request of another issue
        with mock.patch.object(trust, "closed_issue", return_value=ISSUE):
            self.refuses(f"closes #{ISSUE + 1}, not the issue #{ISSUE}")

    def test_the_level_watch_lands_nothing_and_a_misspelt_level_refuses(self):
        self.world()
        self.w["level"] = "watch"
        self.refuses("builds nothing")
        self.refuses("PIVOTAL_LEVEL", LEVEL="lnad")

    def test_every_human_level_lands_a_sealed_stack(self):
        for level in ("ask", "prepare", "land", ""):
            with self.subTest(level=level):
                self.setUp()
                self.world()
                self.w["level"] = level
                code, outputs, _, err = self.go()
                self.assertEqual((code, outputs["land_outcome"], self.main_tip()), (0, "landed", self.sha), err)

    def test_the_rest_of_the_inputs_are_validated_before_anything_is_read(self):
        self.world()
        for name, value in (("ISSUE", "0"), ("ISSUE", "7; rm"), ("ISSUE", ""), ("GH_REPO", "a/b/c"), ("GH_TOKEN", ""), ("GH_TOKEN", "a b"),
                            ("GITHUB_SERVER_URL", "http://evil"), ("WORK_DIR", "relative"), ("WORK_DIR", None),
                            ("GITHUB_OUTPUT", None), ("PIV_GH", "gh2"), ("PIV_GH", "/bin/true")):
            with self.subTest(name=name, value=value):
                before = len(self.lgh.calls()) if hasattr(self, "lgh") else 0
                code, outputs, _, err = self.go(**{name: value})
                self.assertEqual(code, 1, err)
                self.assertIn("refused:", err)
                self.assertEqual(self.writes(), [])
                self.assertEqual(outputs, {})
                self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], self.sha)
                del before
        existing = self.root / "exists"
        existing.mkdir()
        self.refuses("WORK_DIR already exists", WORK_DIR=str(existing))


class SealTest(LandCase):
    """2. Verify the seal (F4) and that the pull request head is the sealed commit."""

    FORGERIES = (
        ("another app", lambda s: s.seal_run(app={"slug": "someone-else"})),
        ("no app", lambda s: s.seal_run(app=None)),
        ("another name", lambda s: s.seal_run(name="pivotal/other")),
        ("a name that only looks the same", lambda s: s.seal_run(name="pivotal/gate​")),
        ("a cyrillic lookalike of the name", lambda s: s.seal_run(name="pivotal/gаte")),
        ("another case of the name", lambda s: s.seal_run(name="Pivotal/Gate")),
        ("another sha", lambda s: s.seal_run(head_sha="f" * 40)),
        ("a conclusion that is not success", lambda s: s.seal_run(conclusion="failure")),
        ("neutral", lambda s: s.seal_run(conclusion="neutral")),
        ("in progress", lambda s: s.seal_run(status="in_progress", conclusion=None)),
        ("queued", lambda s: s.seal_run(status="queued")),
        ("an external id that is not a run id", lambda s: s.seal_run(external_id="4242; rm -rf")),
        ("a unicode digit external id", lambda s: s.seal_run(external_id="٤٢٤٢")),
        ("an external id with a space", lambda s: s.seal_run(external_id="4242 ")),
        ("an external id that is a number", lambda s: s.seal_run(external_id=4242)),
        ("a check run of another suite", lambda s: s.seal_run(check_suite={"id": SUITE + 1})),
        ("a summary with another land hash", lambda s: s.seal_run(summary=seal.summary("0" * 64, ["one", "two"]))),
        ("a summary with no hash", lambda s: s.seal_run(summary="all good")),
    )

    def test_a_check_run_that_differs_in_app_name_sha_conclusion_id_summary_or_suite_is_ignored(self):
        """Whatever of those fields a forger gets wrong on a check run of that name, it neither seals nor blocks. It does NOT
        cover a forger who sets all of them right in a suite of his own: see test_p3 below, a documented limitation."""
        self.world()
        genuine = self.seal_run()
        for label, forge in self.FORGERIES:
            with self.subTest(forged=label):
                forged = forge(self)
                self.w["check_runs"] = [forged]
                self.refuses("")  # alone it seals nothing
                self.w["check_runs"] = [forged, genuine, forged]
                code, outputs, _, err = self.go()
                self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), f"{label}: {err}")
                self.assertEqual(self.main_tip(), self.sha)
                self.git("update-ref", "refs/heads/main", self.base, cwd=self.bare)

    def test_a_forged_check_run_naming_another_run_whose_workflow_is_not_pivotal_is_ignored(self):
        self.world()
        self.w["runs"] = {OTHER: self.run_object(id=OTHER, path=".github/workflows/evil.yml"), RUN: self.run_object()}
        self.w["artifacts"] = self.artifacts(run=RUN)
        self.w["check_runs"] = [self.seal_run(run=OTHER, check_suite={"id": SUITE}), self.seal_run()]
        code, outputs, out, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), err)
        self.assertEqual(self.main_tip(), self.sha)

    def test_a_seal_naming_a_run_of_another_workflow_path_alone_refuses(self):
        self.world()
        for path in (".github/workflows/evil.yml", ".github/workflows/Pivotal.yml", ".github/workflows/pivotal.yml ", ".github/workflows/pіvotal.yml",
                     ".github/workflows/pivotal.yaml", "pivotal.yml", None, 7):
            with self.subTest(path=path):
                self.w["runs"] = {RUN: self.run_object(path=path)}
                self.refuses("run_not_pivotal")

    def test_a_run_of_another_repository_or_id_is_no_seal(self):
        self.world()
        for change in ({"repository": {"full_name": "evil/fork"}}, {"repository": None}, {"id": RUN + 1}, {"id": str(RUN)}, {"id": float(RUN)}):
            with self.subTest(change=change):
                self.w["runs"] = {RUN: self.run_object(**change)}
                self.refuses("run_not_pivotal")
        self.w["runs"] = {RUN: fakegh.not_found()}
        self.refuses("no seal on")

    def test_artifacts_of_another_run_expired_duplicate_missing_or_oversize_are_no_confirmation(self):
        self.world()
        base = self.artifacts()
        for label, listing, word in (
            ("another run", self.artifacts(run=OTHER), "artifact_other_run"),
            ("expired", self.artifacts(expired=True), "artifact_expired"),
            ("expired as a string", self.artifacts(expired="false"), "artifact_other_run"),
            ("no land", [a for a in base if a["name"] != "land"], "artifact_missing"),
            ("a gate missing", [a for a in base if a["name"] != "gate-two"], "artifact_missing"),
            ("land twice", base + [base[0]], "artifact_missing"),
            ("a gate twice", base + [base[1]], "artifact_missing"),
            ("a run id that is a float", self.artifacts(run=float(RUN)), "artifact_other_run"),
            ("a land artifact over the cap", self.artifacts(size_in_bytes=assemble.MAX_BUNDLE_BYTES * 2), "artifact_size"),
            ("a size that is not a number", self.artifacts(size_in_bytes="9"), "artifact_size"),
            ("a negative size", self.artifacts(size_in_bytes=-1), "artifact_size"),
        ):
            with self.subTest(label=label):
                self.w["artifacts"] = listing
                err = self.refuses(word)
                if word in ("artifact_size",):
                    self.assertEqual(self.downloads(), [], "an artifact over the cap is not even downloaded")
        self.w["artifacts"] = [{**a, "name": a["name"] + "​"} for a in base]
        self.refuses("artifact_missing")

    def test_a_listing_that_is_truncated_not_paginated_or_odd_is_refused_not_trusted(self):
        self.world()
        runs = [self.seal_run()]
        for label, pages in (
            ("fewer items than total_count", [{"total_count": 2, "check_runs": runs}]),
            ("pages that disagree", [{"total_count": 1, "check_runs": runs}, {"total_count": 2, "check_runs": []}]),
            ("a bare object, not a list of pages", {"total_count": 1, "check_runs": runs}),
            ("no pages", []),
            ("a page without total_count", [{"check_runs": runs}]),
            ("a total_count that is a string", [{"total_count": "1", "check_runs": runs}]),
            ("a total_count that is a bool", [{"total_count": True, "check_runs": runs}]),
            ("check_runs that is not a list", [{"total_count": 1, "check_runs": {}}]),
            ("a page that is not an object", [[runs[0]]]),
            ("a listing as long as the cap", [{"total_count": 1000, "check_runs": runs * 1000}]),
        ):
            with self.subTest(label=label):
                self.w["check_pages"] = pages
                self.refuses("check_runs")
        self.w["check_pages"] = None
        self.w["artifact_pages"] = [{"total_count": 99, "artifacts": self.w["artifacts"]}]
        self.refuses("artifacts: the listing is truncated")

    def test_the_seal_is_found_on_a_later_page_and_the_listing_is_read_with_flags_that_exist(self):
        self.world()
        forged = self.seal_run(app={"slug": "evil"})
        self.w["check_pages"] = [{"total_count": 3, "check_runs": [forged, forged]}, {"total_count": 3, "check_runs": [self.seal_run()]}]
        code, _, _, err = self.go()
        self.assertEqual(code, 0, err)
        reads = [c["args"] for c in self.lgh.calls() if c["args"][:3] == ["api", "--paginate", "--slurp"]]
        self.assertIn(f"repos/{REPO}/commits/{self.sha}/check-runs?check_name=pivotal%2Fgate&filter=all&per_page=100", [r[3] for r in reads])
        self.assertTrue(all(len(r) == 4 for r in reads), "no other flag")

    def test_a_gate_that_does_not_confirm_the_commit_or_is_not_green_is_no_seal(self):
        self.world()
        directory = self.root / "badgates"
        for label, over in (("a red gate", {"check": "red", "green": False}), ("another sha", {"sha": "f" * 40}),
                            ("a dirty tree", {"clean": False, "green": False}), ("an empty check", {"check": "none", "green": False})):
            with self.subTest(label=label):
                shutil.rmtree(directory, ignore_errors=True)
                shutil.copytree(self.gates / "gate-two", directory)
                data = json.loads((directory / "gate.json").read_text())
                (directory / "gate.json").write_text(json.dumps({**data, **over}))
                self.w["gate_dirs"] = {"one": self.gates / "gate-one", "two": directory}
                self.refuses("gate_")

    def test_a_downloaded_artifact_that_is_a_link_a_directory_or_holds_more_than_it_should_is_refused(self):
        self.world()
        evil = self.root / "evil"
        for label, build in (
            ("land.json as a link", lambda d: (d.mkdir(), (d / "land.json").symlink_to(self.out / "land.json"))),
            ("land.json as a directory", lambda d: (d.mkdir(), (d / "land.json").mkdir())),
            ("an extra file", lambda d: (shutil.copytree(self.out, d), (d / "extra.txt").write_text("x"))),
            ("a fifo", lambda d: (shutil.copytree(self.out, d), (d / "pivotal.bundle").unlink(), os.mkfifo(d / "pivotal.bundle"))),
            ("an empty artifact", lambda d: d.mkdir()),
            ("land.json oversize", lambda d: (d.mkdir(), (d / "land.json").write_bytes(b" " * (assemble.MAX_LAND_BYTES + 1)))),
            ("land.json not json", lambda d: (d.mkdir(), (d / "land.json").write_text("{"))),
        ):
            with self.subTest(label=label):
                shutil.rmtree(evil, ignore_errors=True)
                build(evil)
                self.w["land_dir"] = evil
                self.refuses("no seal on")

    def test_a_gate_artifact_that_is_a_link_or_holds_another_file_is_refused(self):
        self.world()
        for label, build in (
            ("a link", lambda d: (d.mkdir(), (d / "gate.json").symlink_to(self.gates / "gate-two" / "gate.json"))),
            ("two files", lambda d: (shutil.copytree(self.gates / "gate-two", d), (d / "more").write_text("x"))),
            ("a directory", lambda d: (d.mkdir(), (d / "gate.json").mkdir())),
        ):
            with self.subTest(label=label):
                bad = self.root / "badgate"
                shutil.rmtree(bad, ignore_errors=True)
                build(bad)
                self.w["gate_dirs"] = {"one": self.gates / "gate-one", "two": bad}
                self.refuses("no seal on")

    def test_a_seal_for_a_land_json_of_another_issue_is_refused(self):
        self.world()
        other = self.root / "otherland"
        shutil.copytree(self.out, other)
        land_json = {**self.land, "issue": ISSUE + 1}
        land_json["slices"] = [{**x, "branch": x["branch"].replace(f"issue-{ISSUE}", f"issue-{ISSUE + 1}") if x["branch"] else x["branch"]} for x in land_json["slices"]]
        try:
            (other / "land.json").write_bytes(assemble.land_text(land_json))
        except assemble.LandError:
            self.skipTest("land.json of another issue is not even representable")
        self.w["land_dir"] = other
        self.refuses("no seal on")

    def test_the_pull_request_head_must_be_the_sealed_commit(self):
        self.world()
        # The pull request says its head is the first slice, which is sealed by nobody.
        self.w["pr"] = self.pr_json(head={"ref": STACK, "sha": self.heads_["one"], "repo": {"full_name": REPO}})
        self.refuses("no pivotal/gate seal on")
        # The seal is on the head the pull request names, but the stack branch has since moved elsewhere.
        self.w["pr"] = self.pr_json()
        self.git("update-ref", f"refs/heads/{STACK}", self.heads_["one"], cwd=self.bare)
        self.refuses("not at the head land.json names")

    def test_a_stack_branch_with_a_commit_land_json_never_named_is_refused(self):
        self.world()
        self.commit({"smuggled.txt": "x\n"})
        smuggled = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "-f", str(self.bare), f"{smuggled}:refs/heads/{STACK}")
        self.git("reset", "-q", "--hard", self.base)
        self.w["pr"] = self.pr_json(head={"ref": STACK, "sha": smuggled, "repo": {"full_name": REPO}})
        self.refuses("no pivotal/gate seal on")

    def test_a_red_gate_of_an_earlier_slice_is_no_seal_for_a_later_head(self):
        self.world()
        directory = self.root / "redone"
        shutil.copytree(self.gates / "gate-one", directory)
        data = json.loads((directory / "gate.json").read_text())
        (directory / "gate.json").write_text(json.dumps({**data, "check": "red", "green": False}))
        self.w["gate_dirs"] = {"one": directory, "two": self.gates / "gate-two"}
        self.refuses("gate_not_green")

    def test_a_lookalike_of_the_seal_in_the_same_run_and_suite_listed_first_cannot_shadow_the_genuine_one(self):
        self.world()
        lookalike = self.seal_run(summary="land_sha256: " + "0" * 64 + "\n")
        self.w["check_runs"] = [lookalike, self.seal_run()]
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), err)

    def test_the_seal_is_one_candidate_among_several_and_the_first_that_verifies_wins(self):
        self.world()
        self.w["runs"] = {OTHER: fakegh.not_found(), RUN: self.run_object()}
        self.w["check_runs"] = [self.seal_run(run=OTHER), self.seal_run(run=RUN)]
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), err)


OTHER = 999


class BaseTest(LandCase):
    """3. The base: moved since the seal means a re-gate, not a landing."""

    def test_a_base_that_moved_since_the_seal_pushes_nothing_and_refuses_the_regate_without_a_round_zero_pin(self):
        # R16 (this test used to assert a dispatch whose pin was minted from the live issue text: its dispatch assertions now
        # live in RegateRequestTest, with the carried pin). The land artifact has no pin, so the re-gate is refused, nothing is pushed.
        self.world()
        moved = self.move_main()
        pushes, patch = self.spy_push()
        with patch:
            code, outputs, _, err = self.go()
        self.assertEqual(code, 1, "F5: the job is red: nothing was landed and no re-gate was made")
        self.assertIn("could not be dispatched", err)
        self.assertEqual(pushes, [], "no push at all")
        self.assertEqual(self.main_tip(), moved)
        self.assertEqual(outputs, {"land_outcome": "regate", "sealed_sha": self.sha})
        (text,) = self.bodies()
        self.assertIn("Not landed", text)
        self.assertIn(moved, text)
        self.assertNotIn("Landed", text)
        self.assertNotIn("was asked to re-gate", text, "F5: no claim that anything will act on the dispatch")
        self.assertIn("/pivotal go trusted", text)
        self.assertEqual([w for w in self.writes() if w[3].endswith(f"issues/{ISSUE}")], [], "the issue is not closed")

    def test_r16_a_moved_base_with_a_readable_issue_text_still_refuses_the_regate_with_the_pin_word(self):
        # R16: the land artifact carries no round-0 pin, so the re-gate must not mint one from the live title and body (the text
        # is readable here, which is the case that used to send). Nothing is dispatched, and the comment says why.
        self.world()
        self.move_main()
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "regate"), err)
        dispatches = [c for c in self.lgh.calls() if c["args"][:4] == ["api", "--method", "POST", f"repos/{REPO}/dispatches"]]
        self.assertEqual(dispatches, [], "an edited issue text must never be the pin the receiver trusts")
        (text,) = self.bodies()
        self.assertIn("was not dispatched", text)
        self.assertEqual(text.count(land.REGATE_NOT_SENT["pin"]), 1, "the comment carries the pin reason, not a stray word")
        self.assertNotIn("nothing receives it yet", text)

    def test_a_dispatch_that_fails_is_loud_and_still_says_nothing_landed(self):
        self.world()
        self.move_main()
        self.w["dispatch"] = fakegh.fail("HTTP 403\n")
        code, outputs, _, err = self.go()
        self.assertEqual(code, 1)
        self.assertIn("re-gate could not be dispatched", err)
        self.assertEqual(outputs["land_outcome"], "regate")
        self.assertIn("/pivotal go trusted", self.bodies()[0])

    def test_a_regate_whose_issue_text_cannot_be_read_sends_nothing_and_says_why(self):
        self.world()
        self.w["issue"] = {"number": ISSUE, "state": "open"}  # no title and no body: there is no pin to send (F2b, F5)
        self.move_main()
        pushes, patch = self.spy_push()
        with patch:
            code, outputs, _, err = self.go()
        dispatches = [c for c in self.lgh.calls() if c["args"][:4] == ["api", "--method", "POST", f"repos/{REPO}/dispatches"]]
        self.assertEqual(dispatches, [], "the receiver refuses a fix round without its pin, so none is sent")
        self.assertEqual(pushes, [])
        self.assertEqual((code, outputs["land_outcome"]), (1, "regate"), err)
        (text,) = self.bodies()
        self.assertIn("was not dispatched", text)
        self.assertIn("pin", text)
        self.assertNotIn("nothing receives it yet", text, "no dispatch was sent, so the comment makes no claim about a receiver")

    def test_a_base_that_already_holds_the_sealed_commit_is_landed_without_a_push(self):
        self.world()
        self.git("update-ref", "refs/heads/main", self.sha, cwd=self.bare)
        pushes, patch = self.spy_push()
        with patch:
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"], pushes), (0, "landed", []), err)

    def test_a_base_that_cannot_be_read_refuses_before_any_write(self):
        self.world()
        with mock.patch.object(publish, "remote_tip", side_effect=publish.PublishError("ff_unknown")):
            code, _, _, err = self.go()
        self.assertEqual(code, 1)
        self.assertIn("default branch cannot be read", err)
        self.assertEqual(self.writes(), [])

    def test_the_default_branch_name_is_validated(self):
        self.world()
        for name in ("", "-x", "a..b", "x.lock", "a b", None, 5):
            with self.subTest(name=name):
                self.w["default"] = name
                self.refuses("DEFAULT_BRANCH is not a branch name")


class RegateRequestTest(unittest.TestCase):
    """R21 and R16: the re-gate's request is the fix round's shape, sent with the same argv as publish's (Content-Type JSON),
    and its pin is round 0's, carried in the land artifact: never the live issue text."""

    class Hub:
        def __init__(self):
            self.sent = []

        def run(self, *args, timeout=0):
            self.sent.append(args)
            return ""

    def test_the_regate_request_is_the_fix_shape_with_the_carried_pin_and_the_json_content_type(self):
        pin = "a" * 64
        found = land.Sealed(sha="b" * 40, run_id=7, land={"issue_pin": pin}, prefix=["one"])
        s = SimpleNamespace(repo=REPO, token="t" * 20)
        hub = self.Hub()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(land.regate(hub, s, ISSUE, found, Path(tmp), land.carried_pin(found)), "")
            path = Path(tmp) / "dispatch.json"
            body = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(hub.sent, [tuple(fix.dispatch_argv(REPO, str(path)))])
        self.assertIn("Content-Type: application/json", hub.sent[0])
        self.assertEqual(body["event_type"], "pivotal-fix")
        self.assertEqual(tuple(body["client_payload"]), fix.PAYLOAD_KEYS)
        self.assertEqual(body["client_payload"]["issue_pin"], pin)
        self.assertEqual((body["client_payload"]["branch"], body["client_payload"]["round"]),
                         (assemble.stack_branch(ISSUE), 1))

    def test_no_carried_pin_means_nothing_is_sent_and_the_word_is_pin(self):
        found = land.Sealed(sha="b" * 40, run_id=7, land={}, prefix=["one"])  # assemble writes no pin today
        hub = self.Hub()
        s = SimpleNamespace(repo=REPO, token="t" * 20)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(land.regate(hub, s, ISSUE, found, Path(tmp), land.carried_pin(found)), "pin")
        self.assertEqual(hub.sent, [])
        self.assertEqual(land.carried_pin(found), "")


    def test_a_dispatch_that_gh_refuses_is_the_word_dispatch_and_the_request_file_is_still_written(self):
        class Refused(RegateRequestTest.Hub):
            def run(self, *args, timeout=0):
                raise job.GhRefused("gh api was refused (HTTP 4xx) with exit 1")

        found = land.Sealed(sha="b" * 40, run_id=7, land={"issue_pin": "a" * 64}, prefix=["one"])
        s = SimpleNamespace(repo=REPO, token="t" * 20)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(land.regate(Refused(), s, ISSUE, found, Path(tmp), land.carried_pin(found)), "dispatch")
            self.assertTrue((Path(tmp) / "dispatch.json").exists())

    def test_r29_a_dispatch_whose_answer_is_not_known_is_unknown_and_its_comment_says_a_regate_may_have_started(self):
        class Silent(RegateRequestTest.Hub):
            def run(self, *args, timeout=0):
                raise job.JobError("gh api did not finish")

        found = land.Sealed(sha="b" * 40, run_id=7, land={"issue_pin": "a" * 64}, prefix=["one"])
        s = SimpleNamespace(repo=REPO, token="t" * 20)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(land.regate(Silent(), s, ISSUE, found, Path(tmp), land.carried_pin(found)), "unknown")
        self.assertIn("may have been started", land.REGATE_NOT_SENT["unknown"])
        self.assertNotIn("was not dispatched", land.REGATE_NOT_SENT["unknown"])


class PushTest(LandCase):
    """4. A plain fast-forward of the sealed commit; a protected branch is merged through the API with the sha pinned."""

    def test_the_push_is_one_plain_fast_forward_of_the_sealed_commit_never_forced(self):
        self.world()
        pushes, patch = self.spy_push()
        with patch:
            code, _, _, err = self.go()
        self.assertEqual(code, 0, err)
        (push,) = pushes
        self.assertEqual(push, publish.push_args(f"file://{self.server}/{REPO}.git", self.sha, "refs/heads/main"))
        self.assertEqual(push[-1], f"{self.sha}:refs/heads/main")
        for word in push:
            self.assertNotRegex(word, r"^-(-force|-mirror|-delete|f$|-force-with-lease|d$)")
        self.assertNotIn("+", "".join(push[2:]))
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..main", cwd=self.bare), "2")

    def test_a_branch_protected_against_the_push_is_merged_through_the_api_with_the_sealed_sha_pinned(self):
        """Acceptance: a protected branch merges only the pinned SHA."""
        self.world()
        self.protect_main()
        self.w["merge"] = self.genuine_merge()
        pushes, patch = self.spy_push()
        with patch:
            code, outputs, _, err = self.go()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(pushes), 1, "the push is tried once")
        (merge,) = [a for a in self.writes() if a[2] == "PUT"]
        self.assertEqual(merge, ["api", "--method", "PUT", f"repos/{REPO}/pulls/{PR}/merge", "--raw-field", f"sha={self.sha}", "--raw-field", "merge_method=merge"])
        self.assertEqual(outputs["land_outcome"], "landed")
        self.assertEqual(self.git("rev-parse", "main^2", cwd=self.bare), self.sha, "the sealed commit is the second parent")
        self.assertEqual(self.tree("main"), self.tree(self.sha))

    def test_a_merge_for_any_other_sha_is_not_what_the_job_asks_for(self):
        """The fake only answers the call that pins the sealed sha: a call with any other sha would meet no route (GitHub's 409)."""
        self.world()
        self.protect_main()
        self.w["merge"] = self.genuine_merge()
        code, _, _, err = self.go()
        self.assertEqual(code, 0, err)
        for call in self.lgh.calls():
            if "pulls/%d/merge" % PR in " ".join(call["args"]):
                shas = [a for a in call["args"] if a.startswith("sha=")]
                self.assertEqual(shas, [f"sha={self.sha}"])

    def test_a_pull_request_whose_head_moved_is_refused_by_the_pinned_merge(self):
        self.world()
        self.protect_main()
        self.w["merge"] = fakegh.fail("HTTP 409 head branch was modified\n")  # what GitHub answers for a sha that is not the head
        code, outputs, _, err = self.go()
        self.assertEqual(code, 1)
        self.assertEqual(outputs, {"land_outcome": "not_landed", "sealed_sha": self.sha})
        self.assertEqual(self.main_tip(), self.base)
        self.assertNotIn("HTTP 409", "".join(self.bodies()), "gh's error text is never repeated")

    def test_the_lost_answer_of_a_push_that_was_accepted_is_a_landing(self):
        self.world()
        original = publish.Remote.network

        def lost(git, *args):
            out = original(git, *args)
            if args[:1] == ("push",):
                raise assemble.GitError("git push failed with exit 1")
            return out

        with mock.patch.object(publish.Remote, "network", lost):
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"], self.main_tip()), (0, "landed", self.sha), err)
        self.assertEqual([w for w in self.writes() if w[2] == "PUT"], [], "no merge through the API")

    def test_a_push_refused_because_the_base_moved_meanwhile_is_a_regate_not_a_merge(self):
        self.world()
        original = publish.Remote.network
        moved = []

        def racing(git, *args):
            if args[:1] == ("push",):
                moved.append(self.move_main())
                raise assemble.GitError("git push failed with exit 1")
            return original(git, *args)

        with mock.patch.object(publish.Remote, "network", racing):
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"], self.main_tip()), (1, "regate", moved[0]), err)
        self.assertEqual([w for w in self.writes() if w[2] == "PUT"], [], "a moved base is never merged through the API")

    def test_a_push_whose_branch_cannot_be_read_afterwards_is_unknown_and_merges_nothing(self):
        self.world()
        original = publish.Remote.network
        state = {"pushed": False}

        def broken(git, *args):
            if args[:1] == ("push",):
                state["pushed"] = True
                raise assemble.GitError("git push failed with exit 1")
            if state["pushed"] and args[:1] == ("ls-remote",):
                raise assemble.GitError("git ls-remote failed with exit 128")
            return original(git, *args)

        with mock.patch.object(publish.Remote, "network", broken):
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
        self.assertIn("unknown", err)
        self.assertEqual([w for w in self.writes() if w[2] == "PUT"], [])

    def test_a_push_the_job_cannot_make_is_never_forced_as_a_fallback(self):
        self.world()
        self.protect_main()
        pushes, patch = self.spy_push()
        with patch:
            self.go()
        self.assertEqual(len(pushes), 1)
        self.assertNotIn("--force", pushes[0])


class ReadBackTest(LandCase):
    """5. Re-read the base: landed and closed only when the base contains the sealed commit."""

    def test_the_issue_is_commented_labelled_and_closed_when_the_base_contains_the_commit(self):
        self.world()
        code, outputs, _, err = self.go()
        self.assertEqual(code, 0, err)
        methods = [(a[2], a[3].removeprefix(f"repos/{REPO}/")) for a in self.writes()]
        self.assertEqual(methods, [("POST", f"issues/{ISSUE}/comments"), ("DELETE", f"issues/{ISSUE}/labels/pivotal%3Aready"),
                                   ("POST", f"issues/{ISSUE}/labels"), ("PATCH", f"issues/{ISSUE}")])
        self.assertEqual(self.bodies(), [f"body=Landed {self.sha} on main: the base contains the sealed commit."])
        close = self.writes()[-1]
        self.assertEqual(close[4:], ["--raw-field", "state=closed", "--raw-field", "state_reason=completed"])
        self.assertIn("labels[]=pivotal:landed", self.writes()[2])

    def test_the_issue_is_not_closed_until_the_base_contains_the_commit(self):
        """Acceptance. A merge the API accepted that the base does not show (a merge queue) closes nothing."""
        self.world()
        self.protect_main()
        self.w["merge"] = fakegh.ok(json.dumps({"merged": False, "message": "queued"}))
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
        self.assertEqual(self.main_tip(), self.base)
        self.assertEqual([w for w in self.writes() if w[2] in ("PATCH", "DELETE") or w[3].endswith("/labels")], [], "not closed, labels untouched")
        (text,) = self.bodies()
        self.assertTrue(text.startswith("body=Not landed: queued"), text)
        self.assertNotIn("Landed ", text)

    def test_a_refused_push_and_a_refused_merge_leave_the_issue_ready_and_say_why(self):
        self.world()
        self.protect_main()
        self.w["merge"] = fakegh.fail("HTTP 405 Review required\n")
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
        self.assertIn("not landed: rejected", err)
        (text,) = self.bodies()
        self.assertTrue(text.startswith("body=Not landed: rejected"), text)
        self.assertIn(self.sha, text)
        self.assertNotIn("Review required", text)
        self.assertEqual([w[2] for w in self.writes()], ["PUT", "POST"], "the merge that was tried, then the comment")

    def test_no_route_for_the_merge_is_a_refusal_by_github(self):
        self.world()
        self.protect_main()
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
        self.assertEqual(len(self.writes()), 2, "the merge that was tried and the comment")

    def test_a_push_the_base_does_not_show_is_not_landed(self):
        self.world()
        original = publish.Remote.network

        def silent(git, *args):
            if args[:1] == ("push",):
                return ""
            return original(git, *args)

        with mock.patch.object(publish.Remote, "network", silent):
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
        self.assertTrue(self.bodies()[0].startswith("body=Not landed: missing"))
        self.assertEqual([w for w in self.writes() if w[2] == "PATCH"], [])

    def test_a_base_that_cannot_be_read_again_is_unknown_not_landed(self):
        self.world()
        with mock.patch.object(land, "reread", return_value=None):
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
        self.assertTrue(self.bodies()[0].startswith("body=Not landed: unknown"))
        self.assertEqual([w for w in self.writes() if w[2] == "PATCH"], [])

    def test_the_reread_fetches_the_branch_afresh_and_asks_git_for_ancestry(self):
        self.world()
        seen = []
        original = publish.Remote.run

        def spy(git, *args, **kw):
            seen.append(args)
            return original(git, *args, **kw)

        with mock.patch.object(publish.Remote, "run", spy):
            self.go()
        merge_base = [a for a in seen if a[:1] == ("merge-base",)]
        self.assertEqual(len(merge_base), 1)
        self.assertEqual(merge_base[0][1:3], ("--is-ancestor", self.sha))
        self.assertRegex(merge_base[0][3], r"^refs/piv/base-[0-9a-f]{8}$")

    def test_the_reread_says_no_for_a_base_without_the_commit_and_none_for_an_unreadable_one(self):
        self.world()
        git = publish.Remote(self.root / "scratch", self.base, f"file://{self.server}", REPO, TOKEN)
        (self.root / "scratch").mkdir()
        git.init()
        git.network("fetch", "-q", "--no-tags", git.url, f"+refs/heads/{STACK}:refs/heads/{STACK}")
        self.assertIs(land.reread(git, "main", self.sha), False)
        self.assertIs(land.reread(git, "nope", self.sha), None)
        self.git("update-ref", "refs/heads/main", self.sha, cwd=self.bare)
        self.assertIs(land.reread(git, "main", self.sha), True)

    def test_a_landing_whose_close_fails_is_still_landed_and_loud(self):
        self.world()
        original = fakegh.FakeGh.route

        def route(gh, match, *answers, **kw):
            if match.startswith("api --method PATCH"):
                answers = (fakegh.fail("HTTP 500\n"),)
            return original(gh, match, *answers, **kw)

        with mock.patch.object(fakegh.FakeGh, "route", route):
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"], self.main_tip()), (1, "landed", self.sha), err)
        self.assertIn("landed, but", err)

    def test_a_ready_label_that_is_not_there_does_not_stop_the_landing(self):
        self.world()
        self.w["delete"] = fakegh.not_found()
        self.w["labels"] = []
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), err)
        self.assertEqual(self.writes()[-1][2], "PATCH")

    def test_f7_a_ready_label_that_cannot_be_removed_is_loud_not_swallowed(self):
        """A 403 on the DELETE leaves both labels on the issue: the label is still listed, so the job says so and is red
        (the landing itself stands: the issue is still closed)."""
        self.world()
        self.w["delete"] = fakegh.fail("gh: Resource not accessible by integration (HTTP 403)\n")
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"], self.main_tip()), (1, "landed", self.sha), err)
        self.assertIn("landed, but", err)
        self.assertIn("DELETE", err.replace("gh api --method DELETE", "DELETE"))
        self.assertEqual(self.writes()[-1][2], "PATCH", "the issue is still closed")

    def test_f7_a_failed_label_listing_after_a_failed_delete_is_loud_too(self):
        self.world()
        self.w["delete"] = fakegh.not_found()
        original = fakegh.FakeGh.route

        def route(gh, match, *answers, **kw):
            if "--slurp" in match and match.endswith("labels?per_page=100"):
                answers = (fakegh.fail("HTTP 500\n"),)
            return original(gh, match, *answers, **kw)

        with mock.patch.object(fakegh.FakeGh, "route", route):
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "landed"), err)
        self.assertIn("landed, but", err)


class HygieneTest(LandCase):
    def test_the_token_is_in_no_git_argv_no_comment_and_no_output(self):
        self.world()
        argvs = []
        original = subprocess.run

        def spy(cmd, *a, **kw):
            argvs.append([str(x) for x in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)])
            return original(cmd, *a, **kw)

        with mock.patch.object(subprocess, "run", spy):
            code, outputs, stdout, err = self.go()
        self.assertEqual(code, 0, err)
        self.assertFalse([a for a in argvs if TOKEN in " ".join(a)])
        for call in self.lgh.calls():
            self.assertNotIn(TOKEN, json.dumps(call))
        self.assertNotIn(TOKEN, stdout + err + json.dumps(outputs))

    def test_every_gh_child_gets_the_token_and_a_fixed_environment_and_only_gh_is_reached_through_the_seam(self):
        self.world()
        self.go()
        for env in self.lgh.environments():
            self.assertEqual(env["GH_TOKEN"], TOKEN)
            self.assertEqual(env["PATH"].split(":")[-2:], ["/usr/bin", "/bin"]) if env["PATH"] != "/usr/bin:/bin" else None
            self.assertNotIn("PIV_GH", env)
            self.assertFalse([k for k in env if k.startswith(("GITHUB_", "INPUT_", "RUNNER_"))], "nothing of the step's environment leaks")

    def test_main_empties_the_environment_of_the_token_before_any_child_and_refuses_what_cannot_hide_it(self):
        self.world()
        run = self.root / "mainrun"
        run.mkdir()
        environ = {"GH_TOKEN": TOKEN, "PATH": "/usr/bin:/bin", "GH_REPO": REPO, "ISSUE": "0", "WORK_DIR": str(run / "w"),
                   "GITHUB_OUTPUT": str(run / "o"), "GITHUB_EVENT_PATH": "x"}
        with mock.patch.object(buildjob, "make_unreadable"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(land.main(["run"], environ), 1)
        self.assertNotIn("GH_TOKEN", environ)
        with mock.patch.object(buildjob, "make_unreadable", side_effect=job.JobError("no")), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(land.main(["run"], {"GH_TOKEN": TOKEN}), 1)
        self.assertIn("refused", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(land.main([], {}), 2)
            self.assertEqual(land.main(["publish"], {}), 2)

    def test_the_outputs_are_declared_with_a_rule_and_the_job_writes_only_those(self):
        self.assertIn("land_outcome", job.OUTPUT_RULES)
        self.assertEqual(set(land.OUTCOMES), {"landed", "regate", "not_landed"})
        for word in land.OUTCOMES:
            self.assertTrue(job.OUTPUT_RULES["land_outcome"].fullmatch(word))
        for bad in ("", "Landed", "landed\nx=1", "pushed"):
            self.assertFalse(job.OUTPUT_RULES["land_outcome"].fullmatch(bad))

    def test_the_comments_hold_only_validated_words_and_shas(self):
        self.world()
        self.protect_main()
        self.w["merge"] = fakegh.fail("@everyone [click](http://evil) `x`\n")
        self.go()
        for text in self.bodies():
            self.assertRegex(text, r"^body=[A-Za-z0-9 .,:;()/_-]+$")

    def test_the_job_runs_no_repo_code_and_no_other_program_than_git_and_gh(self):
        text = Path(land.__file__).read_text()
        self.assertNotIn("os.system", text)
        self.assertNotIn("shell=True", text)
        self.assertNotIn("subprocess", text.split('"""', 2)[2], "git and gh run through publish/assemble/job only")


class Round1Test(LandCase):
    """Correction round 1: the probes of the review (P1 to P4) against real git in a bare remote and the strict fake gh."""

    def reset_main(self) -> None:
        self.git("update-ref", "refs/heads/main", self.base, cwd=self.bare)

    def intervening(self) -> str:
        return self.forge(self.tree(self.base), self.base, message="intervening")

    def not_landed(self, word: str, **env) -> str:
        code, outputs, out, err = self.go(**env)
        self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
        (text,) = self.bodies()
        self.assertTrue(text.startswith(f"body=Not landed: {word}"), text)
        self.assertNotIn("Landed ", text)
        self.assertEqual([w for w in self.writes() if w[2] in ("PATCH", "DELETE") or w[3].endswith("/labels")], [], "not closed, labels untouched")
        return err

    # --- F1 / P2: the merge commit is checked, not just "contains the sealed commit"

    def test_p2_a_merge_commit_that_is_not_the_sealed_tree_joined_to_the_base_read_is_never_landed(self):
        self.world()
        self.protect_main()
        sealed_tree, base_tree = self.tree(self.sha), self.tree(self.base)
        other = base_tree  # a tree no gate saw
        x = self.intervening()
        cases = (
            ("an intervening commit and another tree", lambda: self.merged(self.forge(other, x, self.sha))),
            ("the right parents and another tree", lambda: self.merged(self.forge(other, self.base, self.sha))),
            ("an intervening first parent and the sealed tree", lambda: self.merged(self.forge(sealed_tree, x, self.sha))),
            ("the parents swapped", lambda: self.merged(self.forge(sealed_tree, self.sha, self.base))),
            ("a third parent", lambda: self.merged(self.forge(sealed_tree, self.base, self.sha, x))),
            ("the sealed commit as the only parent", lambda: self.merged(self.forge(sealed_tree, self.sha))),
        )
        for label, answer in cases:
            with self.subTest(label):
                self.reset_main()
                self.w["merge"] = answer()
                self.not_landed("moved")  # each run builds a fresh fake gh, so only its own writes are counted

    def test_p2_a_response_naming_a_merge_commit_that_is_not_on_the_base_is_never_landed(self):
        self.world()
        self.protect_main()
        genuine = self.forge(self.tree(self.sha), self.base, self.sha)
        forged = self.forge(self.tree(self.base), self.intervening(), self.sha)
        self.w["merge"] = fakegh.ok(json.dumps({"sha": genuine, "merged": True}), effect=["git", "-C", str(self.bare), "update-ref", "refs/heads/main", forged])
        self.not_landed("moved")

    def test_a_merge_answer_with_no_commit_to_check_is_unverified_not_landed(self):
        self.world()
        self.protect_main()
        for body in ({"merged": True}, {"merged": True, "sha": "abc"}, {"merged": True, "sha": 7}, {"merged": True, "sha": "G" * 40}):
            with self.subTest(body=body):
                self.reset_main()
                self.w["merge"] = fakegh.ok(json.dumps(body), effect=["git", "-C", str(self.bare), "update-ref", "refs/heads/main", self.sha])
                self.not_landed("unverified")
        self.reset_main()
        self.w["merge"] = fakegh.ok("not json", effect=["git", "-C", str(self.bare), "update-ref", "refs/heads/main", self.sha])
        self.not_landed("unverified")

    def test_a_genuine_merge_is_landed_and_the_merge_commit_is_the_one_the_response_named(self):
        self.world()
        self.protect_main()
        self.w["merge"] = self.genuine_merge()
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), err)
        self.assertEqual(self.git("rev-list", "--parents", "-n1", "main", cwd=self.bare).split()[1:], [self.base, self.sha])

    def test_the_merge_is_read_back_after_a_further_commit_landed_on_top_of_it(self):
        """The base may move on after the merge: the merge commit is still found on the first-parent chain."""
        self.world()
        self.protect_main()
        m = self.forge(self.tree(self.sha), self.base, self.sha)
        child = self.forge(self.tree(self.base), m, message="later")
        self.w["merge"] = fakegh.ok(json.dumps({"sha": m, "merged": True}), effect=["git", "-C", str(self.bare), "update-ref", "refs/heads/main", child])
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), err)

    # --- F4 / P1: a base that already contains the sealed commit

    def test_p1_a_base_that_contains_the_sealed_commit_as_an_ancestor_is_landed_without_a_push_or_a_regate(self):
        self.world()
        child = self.forge(self.tree(self.base), self.sha, message="later work")
        self.git("update-ref", "refs/heads/main", child, cwd=self.bare)
        pushes, patch = self.spy_push()
        with patch:
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"], pushes), (0, "landed", []), err)
        self.assertEqual([c for c in self.lgh.calls() if "dispatches" in " ".join(c["args"])], [])
        self.assertEqual(self.main_tip(), child)

    def test_a_base_that_holds_the_sealed_commit_through_a_merge_is_landed_only_with_the_sealed_tree(self):
        self.world()
        good = self.forge(self.tree(self.sha), self.base, self.sha)
        bad = self.forge(self.tree(self.base), self.base, self.sha)
        self.git("update-ref", "refs/heads/main", good, cwd=self.bare)
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), err)
        self.git("update-ref", "refs/heads/main", bad, cwd=self.bare)
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
        self.assertTrue(self.bodies()[-1].startswith("body=Not landed: moved"))
        self.assertEqual([c for c in self.lgh.calls() if "dispatches" in " ".join(c["args"])], [], "the base holds the commit: no re-gate")

    def test_a_base_that_moved_without_the_sealed_commit_is_still_a_regate(self):
        self.world()
        moved = self.move_main()
        code, outputs, _, err = self.go()
        self.assertEqual((outputs["land_outcome"], self.main_tip()), ("regate", moved), err)

    # --- F3 / P4: the API merge only on evidence that the branch is protected

    def refuse_push_once(self):
        original = publish.Remote.network
        state = {"n": 0}

        def flaky(git, *args):
            if args[:1] == ("push",):
                state["n"] += 1
                raise assemble.GitError("git push failed with exit 1")  # a transient refusal: nothing was pushed
            return original(git, *args)

        return mock.patch.object(publish.Remote, "network", flaky)

    def test_p4_a_push_refused_on_an_unprotected_base_is_rejected_and_never_merged_through_the_api(self):
        self.world()
        self.w["merge"] = self.genuine_merge()
        with self.refuse_push_once():
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
        self.assertEqual([w for w in self.writes() if w[2] == "PUT"], [], "a merge commit where a fast-forward was planned")
        self.assertEqual(self.main_tip(), self.base)
        self.assertTrue(self.bodies()[0].startswith("body=Not landed: unprotected"))
        self.assertNotIn("and so was the merge", self.bodies()[0], "R8: no claim that the API refused a merge nobody asked for")
        self.assertIn("no merge was tried", self.bodies()[0])

    def test_without_evidence_of_protection_there_is_no_merge(self):
        self.world()
        self.w["merge"] = self.genuine_merge()
        for label, flag in (("a 404", "404"), ("a string", "true"), ("a number", 1), ("null", None)):
            with self.subTest(label):
                self.w["protected"] = flag
                with self.refuse_push_once():
                    code, outputs, _, err = self.go()
                self.assertEqual((code, outputs["land_outcome"]), (1, "not_landed"), err)
                self.assertEqual([w for w in self.writes() if w[2] == "PUT"], [])

    def test_the_protection_is_read_with_a_get_of_the_default_branch_and_nothing_else_is_asked(self):
        self.world()
        self.protect_main()
        self.w["merge"] = self.genuine_merge()
        self.go()
        reads = [c["args"] for c in self.lgh.calls() if c["args"][:2] == ["api", f"repos/{REPO}/branches/main"]]
        self.assertEqual(reads, [["api", f"repos/{REPO}/branches/main"]])

    def test_a_protection_that_is_not_read_when_the_push_succeeds(self):
        self.world()
        self.go()
        self.assertEqual([c for c in self.lgh.calls() if "branches" in " ".join(c["args"])], [])

    # --- F2 / P3: a documented limitation

    def test_p3_known_limitation_a_second_pivotal_run_with_its_own_suite_and_matching_artifacts_is_accepted(self):
        """DOCUMENTED LIMITATION, not a guarantee. seal.verify binds a check run to the run its external_id names (the
        check suite), not to who created it. A second run of .github/workflows/pivotal.yml in a suite of its own that
        uploaded the same land and gate artifacts, and holds checks: write, can make a seal this job accepts. What keeps that
        from happening is the invariant pinned in test_factory (no job that runs repo code or model output holds
        checks: write; only publish does), not this code. OPEN QUESTION for the operator: a dedicated GitHub App slug (or
        another marker only pivotal can create) would close it. If this test starts failing, the verifier got stronger:
        update it and the build report."""
        self.world()
        self.w["runs"] = {OTHER: self.run_object(id=OTHER, check_suite_id=SUITE + 5)}
        self.w["artifacts"] = self.artifacts(run=OTHER)
        self.w["check_runs"] = [self.seal_run(run=OTHER, check_suite={"id": SUITE + 5})]
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), err)

    # --- F5: the re-gate

    def test_f5_the_regate_comment_does_not_claim_a_receiver_and_the_job_is_red(self):
        self.world()
        self.move_main()
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "regate"), err)
        self.assertIn("could not be dispatched", err)
        self.assertNotIn("was asked", self.bodies()[0])

    # --- F6: bind before parse

    def test_f6_a_bad_hash_reports_land_differs_even_when_land_json_is_malformed(self):
        self.world()
        broken = self.root / "broken"
        broken.mkdir()
        (broken / "land.json").write_text("{")
        self.w["land_dir"] = broken
        self.assertNotIn("land_invalid", self.refuses("land_differs"))
        # a hash that does match is parsed, and then it is invalid
        ids = self.land["positions"]
        self.w["check_runs"] = [self.seal_run(summary=seal.summary(hashlib.sha256(b"{").hexdigest(), ids))]
        self.refuses("land_invalid")

    def test_a_land_json_of_an_older_version_is_refused_with_the_version_named(self):
        self.world()
        old = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, old)
        raw = json.dumps({**self.land, "version": 1}).encode()
        (Path(old) / "land.json").write_bytes(raw)
        self.w["land_dir"] = Path(old)
        self.w["check_runs"] = [self.seal_run(summary=seal.summary(hashlib.sha256(raw).hexdigest(), self.land["positions"]))]
        self.assertIn("version must be 2", self.refuses("land_invalid"))

    # --- F9: the download is bounded

    def test_f9_the_land_artifact_is_bounded_well_below_the_bundle_cap_and_refused_before_the_download(self):
        self.assertLess(land.LAND_ARTIFACT_BYTES, assemble.MAX_BUNDLE_BYTES)
        self.world()
        self.w["artifacts"] = self.artifacts("land", *(f"gate-{i}" for i in self.land["positions"]))
        self.w["artifacts"][0]["size_in_bytes"] = land.LAND_ARTIFACT_BYTES + 1
        self.refuses("artifact_size")
        self.assertEqual(self.downloads(), [])
        self.w["artifacts"][0]["size_in_bytes"] = land.LAND_ARTIFACT_BYTES
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (0, "landed"), err)


class Round2Test(LandCase):
    """Correction round 2 (review R1 to R9)."""

    def test_r1_a_seal_whose_run_is_not_on_the_default_branch_is_refused_and_nothing_is_downloaded_pushed_or_merged(self):
        self.world()
        for label, change, word in (
            ("a feature branch run by dispatch", {"head_branch": "feature-forged", "event": "workflow_dispatch"}, "run_branch"),
            ("a feature branch", {"head_branch": "feature-forged"}, "run_branch"),
            ("no branch", {"head_branch": None}, "run_branch"),
            ("another case of the branch", {"head_branch": "Main"}, "run_branch"),
            ("the default branch but a push event", {"event": "push"}, "run_event"),
            ("no event", {"event": None}, "run_event"),
        ):
            with self.subTest(label):
                self.w["runs"] = {RUN: self.run_object(**change)}
                self.refuses(word)
                self.assertEqual(self.downloads(), [], "R3: nothing of the run is downloaded")

    def test_r1_the_default_branch_is_the_repositorys_not_a_constant(self):
        self.world()
        self.w["default"] = "trunk"
        self.w["pr"] = self.pr_json(base={"ref": "trunk", "repo": {"full_name": REPO}})
        self.w["runs"] = {RUN: self.run_object(head_branch="main")}
        self.refuses("run_branch")

    def test_r3_a_foreign_run_is_not_downloaded_from(self):
        self.world()
        for label, run in (
            ("another workflow", self.run_object(id=OTHER, path=".github/workflows/evil.yml")),
            ("another repository", self.run_object(id=OTHER, repository={"full_name": "evil/fork"})),
            ("a feature branch", self.run_object(id=OTHER, head_branch="feature-forged")),
            ("another suite", self.run_object(id=OTHER, check_suite_id=SUITE + 5)),
        ):
            with self.subTest(label):
                self.w["runs"] = {OTHER: run}
                self.w["artifacts"] = self.artifacts(run=OTHER)
                self.w["check_runs"] = [self.seal_run(run=OTHER, check_suite={"id": SUITE})]
                self.refuses("no seal on")
                self.assertEqual(self.downloads(), [], "no `gh run download` for a run whose identity was not checked")
                self.assertEqual([c for c in self.lgh.calls() if "artifacts" in " ".join(c["args"])], [], "not even listed")

    def test_r4_an_expired_artifact_has_its_own_word_and_is_not_downloaded(self):
        self.world()
        self.w["artifacts"] = self.artifacts(expired=True)
        self.refuses("artifact_expired")
        self.assertEqual(self.downloads(), [])
        self.assertEqual(land.ARTIFACT_RETENTION_DAYS, 7)
        self.assertIn("retention-days: 7", (LAND_YML.parent / "factory.yml").read_text())

    def test_r5_only_the_artifact_downloads_get_the_long_timeout(self):
        self.world()
        seen = []
        original = subprocess.run

        def spy(argv, *args, **kwargs):
            seen.append((argv[1:3], kwargs.get("timeout")))
            return original(argv, *args, **kwargs)

        with mock.patch.object(job.subprocess, "run", spy):
            code, _, _, err = self.go()
        self.assertEqual(code, 0, err)
        downloads = [t for a, t in seen if a == ["run", "download"]]
        others = {t for a, t in seen if a != ["run", "download"] and a[:1] == ["api"]}
        self.assertEqual(len(downloads), 3, "land and two gates")
        self.assertEqual(set(downloads), {land.DOWNLOAD_TIMEOUT_SECONDS})
        self.assertGreater(land.DOWNLOAD_TIMEOUT_SECONDS, job.GH_TIMEOUT_SECONDS)
        self.assertIn(job.GH_TIMEOUT_SECONDS, others)
        self.assertLess(max(others), land.DOWNLOAD_TIMEOUT_SECONDS, "an API call keeps a short timeout")

    def test_r2_a_refused_push_over_a_base_that_holds_the_sealed_commit_is_landed_not_regated(self):
        self.world()
        original = publish.Remote.network
        child = []

        def racing(git, *args):
            if args[:1] == ("push",):  # somebody landed the sealed commit (and more) while this push was refused
                child.append(self.forge(self.tree(self.base), self.sha, message="later work"))
                self.git("update-ref", "refs/heads/main", child[0], cwd=self.bare)
                raise assemble.GitError("git push failed with exit 1")
            return original(git, *args)

        with mock.patch.object(publish.Remote, "network", racing):
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"], self.main_tip()), (0, "landed", child[0]), err)
        self.assertEqual([c for c in self.lgh.calls() if "dispatches" in " ".join(c["args"])], [], "no re-gate")
        self.assertTrue(self.bodies()[0].startswith("body=Landed "), self.bodies())

    def test_r2_a_comment_that_fails_after_a_regate_still_writes_the_outcome(self):
        self.world()
        self.move_main()
        self.w["comment"] = fakegh.fail("HTTP 403\n")
        code, outputs, _, err = self.go()
        self.assertEqual((code, outputs), (1, {"land_outcome": "regate", "sealed_sha": self.sha}), err)
        self.assertIn("the comment failed", err)

    def test_r8_an_unreadable_containment_is_worded_as_unread_and_not_as_a_known_move(self):
        self.world()
        moved = self.move_main()
        with mock.patch.object(land, "reread", return_value=None):
            code, outputs, _, err = self.go()
        self.assertEqual((code, outputs["land_outcome"]), (1, "regate"), err)
        (text,) = self.bodies()
        self.assertIn("could not be read", text)
        self.assertNotIn("so it moved after the seal", text)
        self.assertIn(moved, text)
        self.assertEqual(self.main_tip(), moved)

    def test_r8_the_wrong_merge_word_names_the_unreviewed_merge_commit_to_revert_or_regate(self):
        self.world()
        bad = self.forge(self.tree(self.base), self.base, self.sha)
        self.git("update-ref", "refs/heads/main", bad, cwd=self.bare)
        code, outputs, _, err = self.go()
        self.assertEqual(outputs["land_outcome"], "not_landed", err)
        self.assertIn("UNREVIEWED MERGE COMMIT", self.bodies()[-1])
        self.assertIn("revert", self.bodies()[-1])
        self.assertIn("re-gate", self.bodies()[-1])

    def test_r7_run_id_is_not_read_and_not_passed(self):
        self.assertNotIn("RUN_ID", LAND_YML.read_text())
        self.assertNotIn("RUN_ID", Path(land.__file__).read_text())
        self.world()
        self.assertFalse(hasattr(land.read_settings({
            "GH_TOKEN": TOKEN, "GH_REPO": REPO, "ISSUE": "1", "LEVEL": "land", "WORK_DIR": str(self.root / "nope"),
            "GITHUB_OUTPUT": str(self.root / "out"), "GITHUB_EVENT_PATH": "x"}), "run_id"))


class WorkflowTest(LandCase):
    def setUp(self):
        super().setUp()
        self.text = LAND_YML.read_text()
        self.doc = parse(self.text)
        self.job = self.doc["jobs"]["land"]

    def test_one_job_with_only_the_permissions_the_caller_grants_and_none_at_workflow_level(self):
        self.assertEqual(set(self.doc["jobs"]), {"land"})
        self.assertEqual(self.doc["permissions"], {})
        self.assertEqual(self.job["permissions"], {"actions": "read", "checks": "read", "contents": "write", "issues": "write", "pull-requests": "write"})
        caller = parse((Path(LAND_YML).parent.parent.parent / "templates" / "caller.yml").read_text())
        self.assertEqual(caller["jobs"]["land"]["permissions"], self.job["permissions"])

    def test_it_declares_what_the_caller_passes_and_no_secret(self):
        call = self.doc["on"]["workflow_call"] if "on" in self.doc else self.doc[True]["workflow_call"]
        self.assertEqual(set(call["inputs"]), {"issue", "engine_sha", "engine_repo"})
        self.assertNotIn("secrets", call)

    def test_land_is_serialised_per_repository_and_never_cancels_a_running_land(self):
        group = self.job["concurrency"]
        self.assertIn("github.repository", group["group"])
        self.assertEqual(group["cancel-in-progress"], "false")
        self.assertNotIn("inputs.issue", group["group"])

    def test_only_the_pinned_checkout_runs_and_it_keeps_no_credentials(self):
        uses = [s["uses"] for s in self.job["steps"] if "uses" in s]
        self.assertEqual(len(uses), 1)
        self.assertRegex(uses[0], r"^actions/checkout@[0-9a-f]{40}$")
        checkout = next(s for s in self.job["steps"] if "uses" in s)
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(checkout["with"]["repository"], "${{ inputs.engine_repo }}")
        self.assertEqual(checkout["with"]["path"], "pivotal")
        self.assertEqual(checkout["with"]["ref"], "${{ steps.engine.outputs.sha }}")
        engine = next(s for s in self.job["steps"] if s.get("id") == "engine")
        self.assertEqual(engine["env"], {"ENGINE_SHA": "${{ inputs.engine_sha }}"})
        self.assertIn("0" * 40, engine["run"])
        self.assertIn("exit 1", engine["run"])

    def test_no_expression_is_inside_a_run_script_and_the_engine_is_the_step_process(self):
        for step in self.job["steps"]:
            self.assertNotIn("${{", step.get("run", ""))
        step = next(s for s in self.job["steps"] if s.get("id") == "land")
        self.assertEqual(step["run"], "exec python3 -I pivotal/engine/land.py run")

    def test_the_step_env_is_exactly_what_the_engine_reads_and_the_runner_supplies_the_rest(self):
        step = next(s for s in self.job["steps"] if s.get("id") == "land")
        self.assertEqual(set(step["env"]), {"GH_TOKEN", "GH_REPO", "ISSUE", "LEVEL", "WORK_DIR"})
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(step["env"]["ISSUE"], "${{ inputs.issue }}")
        self.assertEqual(step["env"]["LEVEL"], "${{ vars.PIVOTAL_LEVEL }}")
        read = set(re.findall(r'env\.get\("([A-Z_]+)"', Path(land.__file__).read_text())) | {"GH_TOKEN", "GH_REPO", "ISSUE", "WORK_DIR"}
        runner = {"GITHUB_OUTPUT", "GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "GITHUB_SERVER_URL", "PIV_GH"}
        self.assertLessEqual(read, set(step["env"]) | runner)
        self.assertNotIn("PIV_GH", self.text)

    def test_the_outputs_are_declared_at_both_levels_and_consumed_by_name(self):
        step = next(s for s in self.job["steps"] if s.get("id") == "land")
        self.assertEqual(self.job["outputs"], {"outcome": "${{ steps.land.outputs.land_outcome }}", "sealed_sha": "${{ steps.land.outputs.sealed_sha }}"})
        call = self.doc["on"]["workflow_call"] if "on" in self.doc else self.doc[True]["workflow_call"]
        self.assertEqual({k: v["value"] for k, v in call["outputs"].items()}, {"outcome": "${{ jobs.land.outputs.outcome }}", "sealed_sha": "${{ jobs.land.outputs.sealed_sha }}"})
        for name in ("land_outcome", "sealed_sha"):
            self.assertIn(name, job.OUTPUT_RULES)
        self.assertIsNotNone(step)

    def test_a_cancelled_run_never_lands(self):
        self.assertNotIn("cancelled()", self.text.replace("`if: !cancelled()`", ""))
        self.assertNotIn("always()", self.text)
        for step in self.job["steps"]:
            self.assertNotIn("if", step)
        self.assertNotIn("if", self.job)
