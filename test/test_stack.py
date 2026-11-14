"""The publish job's stack and seal (F4): the landable prefix, the fast-forward, the stack PR and the seal.

Real git against a bare "remote" on disk, a fake gh (test/fakegh.py, strict about the flags of the real gh) and the real
assemble job making land.json from hand-made slice artifacts, review verdicts and gate artifacts. Nothing here has run on
real GitHub: branch protection, the check run's app identity and fast-forward semantics on a real remote are unverified.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import fakegh
from test_assemble import ISSUE
from test_publish import BRANCH, REPO, TOKEN, PublishCase

from engine import assemble, gate, job, publish, report, seal

STACK = f"pivotal/issue-{ISSUE}/stack"
RUN = 4242
SUITE = 9001  # the check suite of the workflow run RUN: the check run's `check_suite.id` and the run's `check_suite_id`


class StackCase(PublishCase):
    def stacked(self, *ids: str, gates: bool = True, bad: tuple[str, ...] = ()) -> Path:
        """The real assemble job over one READY slice per id (each its own file); green gate artifacts for each unless
        ``gates`` is false; ``bad`` ids get a red one."""
        for ident in ids:
            self.ready(ident, {f"src/{ident}.txt": f"{ident}\n"})
        code, _, stdout, err = self.run_job(*ids)
        self.assertEqual(code, 0, stdout + err)
        out = Path(self.last_env["OUT_DIR"])
        if gates:
            for ident in ids:
                self.put_gate(out, ident, **({"check": "red", "green": False} if ident in bad else {}))
        return out

    def put_gate(self, out: Path, ident: str, **over) -> None:
        land = assemble.read_land(out / assemble.LAND)
        head = next(x["head"] for x in land["slices"] if x["id"] == ident)
        data = {"version": 2, "issue": ISSUE, "id": ident, "sha": head, "check": "green", "head_unchanged": True, "clean": True,
                "green": True, "score": None, "target": None, **over}
        directory = self.gates / f"gate-{ident}"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir()
        (directory / gate.GATE).write_text(json.dumps(data))

    def heads(self, out: Path) -> dict[str, str]:
        return {x["id"]: x["head"] for x in assemble.read_land(out / assemble.LAND)["slices"] if x["head"]}

    def main_tip(self) -> str:
        return self.remote_refs()["refs/heads/main"]

    def move_main(self) -> str:
        """Somebody else lands a commit on main."""
        self.commit({"elsewhere.txt": "moved\n"})
        self.git("push", "-q", str(self.bare), "HEAD:refs/heads/main")
        tip = self.git("rev-parse", "HEAD")
        self.git("reset", "-q", "--hard", self.base)
        return tip

    def protect_main(self) -> None:
        hook = self.bare / "hooks" / "pre-receive"
        hook.write_text('#!/bin/sh\nwhile read old new ref; do [ "$ref" = refs/heads/main ] && exit 1; done\nexit 0\n')
        hook.chmod(0o755)

    # --- gh

    def check_run(self, sha: str, run: int = RUN, **over) -> dict:
        return {"id": 77, "name": "pivotal/gate", "head_sha": sha, "external_id": str(run), "status": "completed",
                "conclusion": "success", "app": {"slug": "github-actions", "id": 15368}, "check_suite": {"id": SUITE}, **over}

    def route_seal(self, gh: fakegh.FakeGh, sha: str, *answers: dict, run: int = RUN) -> None:
        gh.route(f"api --method POST repos/{REPO}/check-runs ", *(answers or (fakegh.ok(json.dumps(self.check_run(sha, run))),)))

    def route_stack_pr(self, gh: fakegh.FakeGh, head: str, number: int = 20, draft: bool = False) -> None:
        flag = "--draft " if draft else ""
        self.route_list(gh, STACK, [], [self.pr(STACK, head, number, isDraft=draft)])
        gh.route(f"pr create --repo {REPO} {flag}--base main --head {STACK} ", fakegh.ok(f"https://github.com/{REPO}/pull/{number}\n"))

    def seals(self, gh: fakegh.FakeGh | None = None) -> list[dict]:
        return [c for c in (gh or self.gh).calls() if c["args"][:1] == ["api"]]

    def quiet(self, gh: fakegh.FakeGh | None = None, allow: tuple[str, ...] = ()) -> list[list[str]]:
        """The gh requests that are not the DRAFT stack pull request's lookup and creation: a run that must land and seal
        nothing makes none (it was `gh.calls() == []` before the rest of the stack was published)."""
        found = []
        for call in (gh or self.gh).calls():
            args = call["args"]
            on_stack = "--head" in args and args[args.index("--head") + 1] in (STACK, *allow)
            if on_stack and (args[:2] == ["pr", "list"] or (args[:2] == ["pr", "create"] and "--draft" in args)):
                continue
            found.append(args)
        return found

    def result(self, outputs: dict) -> report.Outcome:
        return report.parse_result(outputs["result"])

    def run_land(self, out: Path, last: str, **over) -> tuple[int, dict, str, str]:
        """A trusted run at the land level whose seal route answers for ``last``."""
        self.route_seal(self.gh, last)
        return self.publish(out, **{"LEVEL": "land", **over})


class LandTest(StackCase):
    def test_a_green_stack_at_the_land_level_fast_forwards_main_and_is_sealed(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        code, outputs, stdout, err = self.run_land(out, heads["two"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["two"], "main advanced to the head of the stack")
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..{heads['two']}", cwd=self.bare), "2")
        self.assertNotIn(f"refs/heads/{STACK}", self.remote_refs(), "no stack branch when the base moved")
        self.assertEqual([c["args"][:2] for c in self.gh.calls()], [["api", "--method"]], "no pull request, only the seal")
        result = self.result(outputs)
        self.assertEqual((result.status, result.sha, result.built_by), ("landed", heads["two"], "claude:anthropic/sonnet"))
        self.assertEqual(result.reviewed_by, "codex:openai/gpt-5")
        self.assertIn(f"stack: landed {heads['two']} on main (2 of 2 slices)", stdout)

    def test_the_seal_is_a_completed_check_run_on_m_with_the_run_id_as_external_id(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        land_sha = hashlib.sha256((out / assemble.LAND).read_bytes()).hexdigest()
        self.assertEqual(self.run_land(out, heads["two"])[0], 0)
        (call,) = self.seals()
        self.assertEqual(call["args"][:5], ["api", "--method", "POST", f"repos/{REPO}/check-runs", "--input"])
        self.assertEqual(len(call["args"]), 6)
        body = json.loads(call["input"])
        self.assertEqual(body, {
            "name": "pivotal/gate", "head_sha": heads["two"], "status": "completed", "conclusion": "success",
            "external_id": str(RUN),
            "output": {"title": "Pivotal gate passed",
                       "summary": f"The first 2 slice(s) of the stack passed the gate: one, two.\nland_sha256: {land_sha}\n"},
        })

    def test_the_seal_is_made_after_the_base_moved_never_before(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        seen = []
        real = publish.create_seal

        def spy(*a, **kw):
            seen.append(self.main_tip())
            return real(*a, **kw)

        with mock.patch.object(publish, "create_seal", spy):
            self.assertEqual(self.run_land(out, head)[0], 0)
        self.assertEqual(seen, [head], "main already stood at m when the seal was made")

    def test_the_push_is_a_plain_fast_forward_of_a_commit_never_forced(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        pushes = []
        real = publish.Remote.network

        def spy(git, *args):
            if args[0] == "push":
                pushes.append(list(args))
            return real(git, *args)

        with mock.patch.object(publish.Remote, "network", spy):
            self.assertEqual(self.run_land(out, heads["two"])[0], 0)
        url = f"file://{self.server}/{REPO}.git"
        self.assertEqual(pushes, [["push", "-q", "--no-recurse-submodules", url, f"{heads['two']}:refs/heads/main"]])
        for args in pushes:
            self.assertFalse([a for a in args if a.startswith(("--force", "--mirror", "--delete", "+", "--prune")) or a == "-f"])
            self.assertFalse([a for a in args[1:] if "+" in a.replace(url, "")])

    def test_push_args_refuses_anything_that_could_force_delete_or_leave_the_branches(self):
        sha = "a" * 40
        self.assertEqual(publish.push_args("u", sha, "refs/heads/main"), ["push", "-q", "--no-recurse-submodules", "u", f"{sha}:refs/heads/main"])
        for sha_, ref in (("a" * 39, "refs/heads/main"), ("main", "refs/heads/main"), (sha, "+refs/heads/main"),
                          (sha, "refs/heads/+main"), (sha, "refs/tags/v1"), (sha, "main"), (sha, "refs/heads/-f"), (sha, "")):
            with self.assertRaises(ValueError, msg=(sha_, ref)):
                publish.push_args("u", sha_, ref)

    def test_main_moved_before_the_run_is_a_ready_stack_pr_and_nothing_is_forced(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        moved = self.move_main()
        self.route_stack_pr(self.gh, heads["two"])
        code, outputs, stdout, err = self.run_land(out, heads["two"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), moved, "main is where the other commit left it")
        self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], heads["two"])
        (create,) = self.creates()
        self.assertNotIn("--draft", create, "a ready pull request")
        body = Path(create[create.index("--body-file") + 1]).read_text()
        self.assertTrue(body.startswith(f"Closes #{ISSUE}\n"))
        self.assertIn("why a pull request: ff_rejected", body)
        self.assertEqual(create[create.index("--head") + 1], STACK)
        self.assertEqual(len(self.seals()), 1, "the seal is made once the stack PR is open")
        result = self.result(outputs)
        self.assertEqual((result.status, result.pr), ("ready", 20))
        self.assertIn("stack: opening the stack pull request (ff_rejected)", stdout)

    def test_main_moving_between_the_check_and_the_push_is_a_ready_stack_pr_not_an_error(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        real = publish.Remote.network
        moved = []

        def racing(git, *args):
            answer = real(git, *args)
            if args[0] == "ls-remote" and args[-1] == "refs/heads/main" and not moved:
                moved.append(self.move_main())  # after we saw main at the base, before the push
            return answer

        self.route_stack_pr(self.gh, head)
        with mock.patch.object(publish.Remote, "network", racing):
            code, outputs, _, err = self.run_land(out, head)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), moved[0], "the real git refused the non-fast-forward")
        self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], head)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.result(outputs).status, "ready")

    def test_a_protected_main_refusing_the_push_is_a_ready_stack_pr(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.protect_main()
        self.route_stack_pr(self.gh, head)
        code, outputs, _, err = self.run_land(out, head)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.base)
        self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], head)
        self.assertEqual(self.result(outputs).status, "ready")

    def test_a_rejected_push_is_tried_once(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.protect_main()
        self.route_stack_pr(self.gh, head)
        pushes = []
        real = publish.Remote.network

        def spy(git, *args):
            if args[0] == "push":
                pushes.append(args[-1])
            return real(git, *args)

        with mock.patch.object(publish.Remote, "network", spy):
            self.run_land(out, head)
        self.assertEqual(pushes, [f"{head}:refs/heads/main", f"{head}:refs/heads/{STACK}"], "main once, then the stack branch")

    def test_a_rerun_after_the_base_moved_to_m_pushes_nothing_and_opens_no_pull_request(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.assertEqual(self.run_land(out, head)[0], 0)
        again = fakegh.FakeGh(self.root / "bin2")
        self.route_seal(again, head, run=4243)
        code, outputs, _, err = self.publish(out, again, LEVEL="land", RUN_ID="4243")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.creates(again), [])
        self.assertEqual(self.main_tip(), head)
        self.assertEqual(self.result(outputs).status, "landed")


class LevelTest(StackCase):
    def test_below_the_land_level_the_prefix_is_a_ready_stack_pr_and_nothing_moves(self):
        """Who gets a seal: ask and prepare seal the READY stack PR (a human's "land it" needs it); watch never does."""
        for level, sealed in (("watch", False), ("ask", True), ("prepare", True)):
            with self.subTest(level=level):
                self.setUp()
                out = self.stacked("one", "two")
                heads = self.heads(out)
                self.route_stack_pr(self.gh, heads["two"])
                self.route_seal(self.gh, heads["two"])
                code, outputs, _, err = self.publish(out, LEVEL=level)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.main_tip(), self.base)
                seals = self.seals()
                self.assertEqual(len(seals), 1 if sealed else 0)
                if sealed:
                    self.assertEqual(json.loads(seals[0]["input"])["head_sha"], heads["two"], "the seal is on the stack tip")
                (create,) = self.creates()
                self.assertNotIn("--draft", create)
                self.assertIn("why a pull request: level", Path(create[create.index("--body-file") + 1]).read_text())
                self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], heads["two"])
                self.assertEqual(self.result(outputs).status, "ready")

    def test_who_gets_a_seal_by_level_stack_trust_and_pull_request_state(self):
        for level in ("watch", "ask", "prepare", "land"):
            for full in (True, False):
                for trusted in (True, False):
                    for draft_pr in (False, True):
                        with self.subTest(level=level, full=full, trusted=trusted, draft_pr=draft_pr):
                            self.setUp()
                            out = self.stacked("one", "two", bad=() if full else ("two",))
                            heads = self.heads(out)
                            tip = heads["two"]
                            # an existing PR at the tip decides READY or draft; for a land level that cannot fast-forward
                            # (main moved) the stack PR is where the seal goes
                            self.move_main()
                            self.route_list(self.gh, STACK, [self.pr(STACK, tip, 20, isDraft=draft_pr)])
                            self.route_seal(self.gh, tip)
                            code, _, _, err = self.publish(out, LEVEL=level, TRUSTED="true" if trusted else "false", KIND="go")
                            self.assertEqual(code, 0, err)
                            want = full and trusted and not draft_pr and level != "watch"
                            self.assertEqual(len(self.seals()), 1 if want else 0)

    def test_a_held_level_never_seals(self):
        """`land` held to `watch` (no check) or by the facts: only what job.standing honours seals."""
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.route_stack_pr(self.gh, head)
        self.route_seal(self.gh, head)
        code, _, _, err = self.publish(out, LEVEL="land", CHECK="")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.seals(), [])

    def test_a_level_held_by_standing_never_seals_even_when_the_effective_level_would(self):
        """`land` held to `prepare` (no cross-vendor reviewer): the effective level is sealable, but job.standing holds it,
        so may_seal refuses. Goes RED if the standing/held check is removed from Settings.may_seal."""
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.route_stack_pr(self.gh, head)
        self.route_seal(self.gh, head)
        code, _, _, err = self.publish(out, LEVEL="land", REVIEWER="false", CHECK="make test")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.seals(), [], "held to prepare by no reviewer: no seal")

    def test_a_kind_not_allowed_at_the_effective_level_never_seals(self):
        """Defense in depth (S1): a run whose kind is not allowed at the effective level reaches the seal path with every
        other condition true (trusted, full stack, READY PR) and no seal is made. `ask` starts only go/fix, so an event run
        at `ask` must not seal. Goes RED if job.level_allows(self.level, self.kind) is removed from Settings.may_seal."""
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.route_stack_pr(self.gh, head)
        self.route_seal(self.gh, head)
        code, _, _, err = self.publish(out, LEVEL="ask", KIND="event", TRUSTED="true")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.seals(), [], "event is not allowed at ask: no seal")

    def test_a_stranger_never_lands_or_seals_and_gets_a_draft_stack_pr_even_at_the_land_level(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.route_stack_pr(self.gh, head, draft=True)
        code, outputs, _, err = self.publish(out, LEVEL="land", TRUSTED="false", KIND="go")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.base)
        self.assertEqual(self.seals(), [])
        (create,) = self.creates()
        self.assertIn("--draft", create)
        result = self.result(outputs)
        self.assertEqual((result.status, result.why), ("draft", "untrusted_stack"))

    def test_only_a_trusted_run_at_the_land_level_may_land(self):
        out = self.stacked("one")
        for level, trusted, kind, want in (
            ("land", "true", "go", True), ("land", "true", "fix", True), ("land", "false", "go", False),
            ("prepare", "true", "go", False), ("ask", "true", "go", False), ("watch", "true", "go", False),
        ):
            settings = publish.read_settings(self.penv(out, LEVEL=level, TRUSTED=trusted, KIND=kind)).with_baseline("green")
            self.assertEqual(settings.may_land, want, (level, trusted, kind))
            self.assertEqual(settings.may_seal, trusted == "true" and level != "watch", (level, trusted, kind))


class PrefixTest(StackCase):
    def run_prefix(self, out: Path, last: str | None = None, **over) -> tuple[int, dict, str, str]:
        if last:
            self.route_seal(self.gh, last)
        land = assemble.read_land(out / assemble.LAND)
        tip = self.heads(out)[land["positions"][-1]]
        self.route_stack_pr(self.gh, tip, draft=True)  # whatever does not land is published as a draft stack PR (F-2)
        return self.publish(out, **{"LEVEL": "land", **over})

    def test_a_red_gate_in_the_middle_ends_the_prefix_before_it(self):
        out = self.stacked("one", "two", "three", bad=("two",))
        heads = self.heads(out)
        code, outputs, stdout, err = self.run_prefix(out, heads["one"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["one"], "only the green prefix landed, not three, though three is green")
        self.assertIn("stack: 1 of 3 slices are landable (stops at: gate_red)", stdout)
        self.assertEqual(json.loads(self.seals()[0]["input"])["head_sha"], heads["one"])
        result = self.result(outputs)
        self.assertEqual((result.status, result.why), ("draft", "partial_land"))

    def test_a_missing_gate_artifact_ends_the_prefix_at_the_one_before(self):
        out = self.stacked("one", "two", "three")
        shutil.rmtree(self.gates / "gate-two")
        heads = self.heads(out)
        code, _, stdout, err = self.run_prefix(out, heads["one"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["one"])
        self.assertIn("(stops at: gate_missing)", stdout)

    def test_no_gate_artifact_at_all_lands_and_seals_nothing_and_publishes_the_stack_as_a_draft(self):
        out = self.stacked("one", "two", gates=False)
        code, outputs, stdout, err = self.run_prefix(out)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.main_tip(), self.quiet(), len(self.creates())), (self.base, [], 1))
        self.assertIn("--draft", self.creates()[0])
        self.assertIn("stack: 0 of 2 slices are landable (stops at: gate_missing)", stdout)
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("draft", "not_landable", 20))

    def test_a_gate_artifact_for_another_sha_is_no_match(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        self.put_gate(out, "two", sha=heads["one"])  # a green gate, for the first slice's commit
        code, _, stdout, err = self.run_prefix(out, heads["one"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["one"])
        self.assertIn("(stops at: gate_invalid)", stdout)

    def test_a_gate_artifact_of_another_slice_in_this_slices_place_is_no_match(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        shutil.rmtree(self.gates / "gate-two")
        shutil.copytree(self.gates / "gate-one", self.gates / "gate-two")
        code, _, stdout, err = self.run_prefix(out, heads["one"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["one"])
        self.assertIn("(stops at: gate_other)", stdout)

    def test_a_gate_that_is_not_green_in_any_way_is_not_green(self):
        for name, over, word in (
            ("none", {"check": "none", "green": False}, "gate_none"),
            ("dirty", {"clean": False, "green": False}, "gate_dirty"),
            ("moved", {"head_unchanged": False, "green": False}, "gate_dirty"),
            ("edited", {"check": "red", "green": True}, "gate_invalid"),
            ("none-edited", {"check": "none", "green": True}, "gate_invalid"),
            ("extra", {"extra": 1}, "gate_invalid"),
            ("string", {"green": "true"}, "gate_invalid"),
        ):
            with self.subTest(name):
                self.setUp()
                out = self.stacked("one")
                self.put_gate(out, "one", **over)
                code, _, stdout, err = self.run_prefix(out)
                self.assertEqual((code, self.main_tip(), self.quiet()), (0, self.base, []), err)
                self.assertIn(f"(stops at: {word})", stdout)

    def test_a_gate_artifact_that_is_a_link_a_fifo_or_oversized_is_no_gate(self):
        for name in ("link", "fifo", "big", "dirlink", "extra-file"):
            with self.subTest(name):
                self.setUp()
                out = self.stacked("one")
                target = self.gates / "gate-one"
                if name == "link":
                    real = self.root / "real.json"
                    shutil.move(str(target / gate.GATE), real)
                    (target / gate.GATE).symlink_to(real)
                elif name == "fifo":
                    (target / gate.GATE).unlink()
                    os.mkfifo(target / gate.GATE)
                elif name == "big":
                    (target / gate.GATE).write_text("{" + " " * 5000 + "}")
                elif name == "dirlink":
                    shutil.move(str(target), self.root / "elsewhere")
                    target.symlink_to(self.root / "elsewhere")
                else:
                    (target / "other").write_text("x")
                code, _, stdout, err = self.run_prefix(out)
                self.assertEqual((code, self.main_tip(), self.quiet()), (0, self.base, []), err)
                self.assertIn("(stops at: gate_invalid)", stdout)

    def rewrite_review(self, ident: str, names: list[str], **kw) -> None:
        patch = (self.slices / f"slice-{ident}" / "slice.patch").read_bytes()
        shutil.rmtree(self.reviews / f"review-{ident}")
        self.review_of(ident, patch, names, **kw)

    def test_a_review_that_is_not_ready_a_same_vendor_one_or_bound_to_something_else_ends_the_prefix(self):
        for name, change, word in (
            ("needs fixes", lambda: self.rewrite_review("one", ["src/one.txt"], verdict="NEEDS FIXES"), "review_needs_fixes"),
            ("same vendor", lambda: self.rewrite_review("one", ["src/one.txt"], reviewer="claude:anthropic/opus"), "review_invalid"),
            ("another patch", lambda: (self.slices / "slice-one" / "slice.patch").write_bytes(b"changed\n"), "review_wrong_sha"),
            ("another file list", lambda: self.rewrite_review("one", ["src/one.txt", "src/extra.txt"]), "review_files"),
            ("fewer files", lambda: self.rewrite_review("one", ["src/zzz.txt"]), "review_files"),
            ("another round", lambda: self.rewrite_review("one", ["src/one.txt"], round=1), "review_stale"),
            ("another base", lambda: self.rewrite_review("one", ["src/one.txt"], base="1" * 40), "review_stale"),
            ("another slice", lambda: self.rewrite_review("one", ["src/one.txt"], slice="two"), "review_stale"),
            ("another builder", lambda: self.rewrite_review("one", ["src/one.txt"], built_by="claude:anthropic/opus"), "review_wrong_builder"),
            ("missing", lambda: shutil.rmtree(self.reviews / "review-one"), "review_missing"),
            ("a link", lambda: (shutil.move(str(self.reviews / "review-one"), self.root / "r"),
                                (self.reviews / "review-one").symlink_to(self.root / "r")), "review_invalid"),
            ("an extra file", lambda: (self.reviews / "review-one" / "more").write_text("x"), "review_invalid"),
        ):
            with self.subTest(name):
                self.setUp()
                out = self.stacked("one")
                change()
                code, _, stdout, err = self.run_prefix(out)
                self.assertEqual((code, self.main_tip(), self.quiet()), (0, self.base, []), err)
                self.assertIn(f"(stops at: {word})", stdout)

    def test_a_review_failure_in_the_second_slice_keeps_the_first(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        self.rewrite_review("two", ["src/two.txt"], verdict="NEEDS FIXES")
        code, _, stdout, err = self.run_prefix(out, heads["one"])
        self.assertEqual((code, self.main_tip()), (0, heads["one"]), err)
        self.assertIn("1 of 2 slices are landable (stops at: review_needs_fixes)", stdout)

    def test_a_slice_artifact_that_disagrees_with_land_json_ends_the_prefix(self):
        for name, change, word in (
            ("another builder", lambda d: (d / "result.json").write_text(json.dumps({**json.loads((d / "result.json").read_text()), "built_by": "codex:openai/gpt-5"})), "slice_invalid"),
            ("no result", lambda d: (d / "result.json").unlink(), "slice_invalid"),
            ("no patch", lambda d: (d / "slice.patch").unlink(), "slice_invalid"),
            ("junk result", lambda d: (d / "result.json").write_text("{"), "slice_invalid"),
            ("an extra file", lambda d: (d / "evil").write_text("x"), "slice_invalid"),
        ):
            with self.subTest(name):
                self.setUp()
                out = self.stacked("one")
                change(self.slices / "slice-one")
                code, _, stdout, err = self.run_prefix(out)
                self.assertEqual((code, self.main_tip(), self.quiet()), (0, self.base, []), err)
                self.assertIn(f"(stops at: {word})", stdout)

    def test_a_baseline_that_is_not_green_lands_nothing(self):
        out = self.stacked("one")
        land = assemble.read_land(out / assemble.LAND)
        (out / assemble.LAND).write_bytes(assemble.land_text({**land, "baseline": "red"}))
        code, outputs, stdout, err = self.run_prefix(out)
        self.assertEqual((code, self.main_tip(), self.quiet()), (0, self.base, []), err)
        self.assertIn("(stops at: baseline_red)", stdout)
        self.assertEqual(self.result(outputs).why, "baseline_red")

    def test_a_review_job_that_did_not_succeed_has_no_prefix(self):
        out = self.stacked("one")
        code, outputs, stdout, err = self.run_prefix(out, REVIEW_RESULT="failure")
        self.assertEqual((code, self.main_tip(), self.quiet()), (0, self.base, []), err)
        self.assertIn("(stops at: review_failed)", stdout)

    def test_the_prefix_is_computed_before_any_write_and_an_empty_one_moves_only_the_stack_branch(self):
        out = self.stacked("one", gates=False)
        before = self.remote_refs()
        self.run_prefix(out)
        self.assertEqual(self.remote_refs(), {**before, f"refs/heads/{STACK}": self.heads(out)["one"]}, "main is untouched")


class SettingsTest(StackCase):
    def test_the_new_inputs_are_required_and_validated(self):
        out = self.stacked("one")
        for name, value, message in (
            ("LEVEL", "everything", "PIVOTAL_LEVEL"), ("KIND", None, "kind"), ("TRUSTED", None, "trusted"),
            ("TRUSTED", "yes", "trusted"), ("HAS_STACK", None, "HAS_STACK"), ("HAS_STACK", "maybe", "HAS_STACK"),
            ("GATE_RESULT", None, "GATE_RESULT"), ("GATE_RESULT", "cancelled", "GATE_RESULT"), ("REVIEW_RESULT", None, "REVIEW_RESULT"),
            ("REVIEW_RESULT", "cancelled", "REVIEW_RESULT"), ("GATES_DIR", None, "GATES_DIR"), ("GATES_DIR", "gates", "GATES_DIR"),
            ("REVIEWS_DIR", None, "REVIEWS_DIR"), ("SLICES_DIR", None, "SLICES_DIR"),
        ):
            with self.subTest(name=name, value=value):
                self.refused(out, message, **{name: value})

    def test_a_skipped_gate_is_accepted_only_when_nothing_was_stacked(self):
        out = self.stacked("one")
        self.refused(out, "gate job's result", GATE_RESULT="skipped")
        self.refused(out, "gate job's result", HAS_STACK="false")
        lone = self.assembled("lone")
        self.assertEqual(self.publish(lone, GATE_RESULT="skipped", HAS_STACK="false")[0], 1, "the draft has no PR route here")
        self.refused(lone, "gate job's result", GATE_RESULT="success")

    def test_land_json_and_the_assemble_output_must_agree_about_the_stack(self):
        out = self.stacked("one")
        self.refused(out, "disagree", HAS_STACK="false", GATE_RESULT="skipped")


class ResultTest(StackCase):
    def test_a_seal_that_cannot_be_made_is_red_even_after_the_base_moved(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.route_seal(self.gh, head, fakegh.fail("gh: Resource not accessible by integration (HTTP 403)\n"))
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1)
        self.assertIn("stack: the seal failed (seal_failed) after the base moved", err)
        self.assertEqual(self.main_tip(), head, "the push happened and is not undone: the log says so")
        result = self.result(outputs)
        self.assertEqual((result.status, result.sha), ("landed", head), "N1: the base moved, so the result says landed")

    def test_an_answer_that_is_not_the_seal_that_was_asked_for_is_a_failed_seal(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        for name, answer in (
            ("other sha", self.check_run("f" * 40)), ("other run", self.check_run(head, run=1)),
            ("other app", self.check_run(head, app={"slug": "someone-else"})), ("not success", self.check_run(head, conclusion="failure")),
            ("no id", {k: v for k, v in self.check_run(head).items() if k != "id"}), ("junk", [1]),
        ):
            with self.subTest(name):
                self.setUp()
                out = self.stacked("one")
                self.route_seal(self.gh, head, fakegh.ok(json.dumps(answer)))
                code, outputs, _, err = self.publish(out, LEVEL="land")
                self.assertEqual(code, 1)
                self.assertIn("the seal failed (seal_failed)", err)
                self.assertEqual(self.result(outputs).status, "landed")

    def test_no_seal_is_made_when_the_stack_pull_request_cannot_be_opened(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.move_main()
        self.route_list(self.gh, STACK, [])
        self.gh.route(f"pr create --repo {REPO} --base main --head {STACK} ", fakegh.fail("boom\n"))
        code, outputs, _, _ = self.publish(out, LEVEL="land")
        self.assertEqual((code, self.seals()), (1, []))
        self.assertEqual(self.result(outputs).why, "gh_failed")

    def test_no_seal_is_made_for_a_stack_pull_request_that_is_closed(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.move_main()
        self.route_list(self.gh, STACK, [self.pr(STACK, head, 5, state="CLOSED", isDraft=False)])
        code, outputs, _, _ = self.publish(out, LEVEL="land")
        self.assertEqual((code, self.seals(), self.creates()), (0, [], []))
        self.assertEqual(self.result(outputs).why, "pr_closed")

    def test_a_rerun_finds_the_stack_pull_request_by_its_branch_and_opens_no_second(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.move_main()
        self.route_stack_pr(self.gh, head)
        self.assertEqual(self.run_land(out, head)[0], 0)
        again = fakegh.FakeGh(self.root / "bin2")
        self.route_list(again, STACK, [self.pr(STACK, head, 20, isDraft=False)])
        self.route_seal(again, head, run=4243)
        code, outputs, stdout, err = self.publish(out, again, LEVEL="land", RUN_ID="4243")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.creates(again), [])
        self.assertIn("stack: exists (#20)", stdout)
        self.assertEqual(self.result(outputs).pr, 20)

    def test_a_branch_left_by_a_closed_stack_pull_request_at_another_head_is_stale(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.move_main()
        self.git("push", "-q", str(self.bare), f"{self.base}:refs/heads/{STACK}")
        self.route_list(self.gh, STACK, [self.pr(STACK, "e" * 40, 4, state="CLOSED", isDraft=False)])
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1)
        self.assertEqual(self.result(outputs).why, "branch_stale")
        self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], self.base, "never overwritten")

    def test_an_unexpected_error_in_the_stack_does_not_skip_the_drafts_or_the_result(self):
        self.ready("one", {"src/one.txt": "one\n"})
        self.artifact("lone", self.patch_of({"src/lone.txt": "l\n"}))  # no review: a draft, not a candidate
        code, _, stdout, err = self.run_job("one", "lone")
        self.assertEqual(code, 0, stdout + err)
        out = Path(self.last_env["OUT_DIR"])
        self.put_gate(out, "one")
        land = assemble.read_land(out / assemble.LAND)
        draft = next(x for x in land["slices"] if x["outcome"] == "draft")
        self.route_list(self.gh, draft["branch"], [], [self.pr(draft["branch"], draft["head"], 13)])
        self.route_create(self.gh, draft["branch"], 13)
        with mock.patch.object(publish, "publish_stack", side_effect=RuntimeError("boom")):
            code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1)
        self.assertIn("stack: failed (unexpected)", err)
        self.assertEqual(len(self.creates()), 1, "the draft was still opened")
        self.assertEqual(self.result(outputs).why, "publish_failed")

    def test_every_result_the_stack_can_write_is_one_report_reads_and_every_word_is_in_why(self):
        for status, why in (("draft", "partial_land"), ("draft", "partial_stack"), ("draft", "untrusted_stack"),
                            ("failed", "seal_failed"), ("failed", "not_landable"), ("draft", "pr_closed")):
            data = json.dumps({"status": status, "why": why, "built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5", "pr": 3})
            outcome = report.parse_result(data)
            self.assertEqual((outcome.status, outcome.why), (status, why))
        landed = report.parse_result(json.dumps({"status": "landed", "built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5", "sha": "a" * 40}))
        self.assertEqual(landed.status, "landed")
        for word in ("gate_red", "review_rejected", "candidate_only", "no_stack", "branch_stale", "pr_closed"):
            self.assertIn(word, report.WHY)


class TokenTest(StackCase):
    def test_the_token_is_in_no_git_argv_and_no_stack_text(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.move_main()
        self.route_stack_pr(self.gh, head)
        argvs = []
        real = subprocess.run

        def spy(cmd, *a, **kw):
            argvs.append([str(c) for c in cmd])
            return real(cmd, *a, **kw)

        with mock.patch("subprocess.run", spy):
            self.assertEqual(self.run_land(out, head)[0], 0)
        self.assertFalse([c for c in argvs if any(TOKEN in part for part in c)])
        for call in self.gh.calls():
            self.assertNotIn(TOKEN, json.dumps(call))


class CorrectionOneTest(StackCase):
    """The first correction round: each of these failed on the code of the first review (F-1 .. F-10)."""

    # --- F-1: a slice is one commit

    def test_a_two_commit_slice_never_moves_main_through_the_real_pipeline(self):
        self.ready("one", {"a.txt": "1\n"}, {"b.txt": "2\n"})
        code, _, stdout, err = self.run_job("one")
        self.assertEqual(code, 0, stdout + err)
        out = Path(self.last_env["OUT_DIR"])
        self.put_gate(out, "one")  # a green gate for the slice head: the old code landed both commits on this
        self.new_prs(out)
        code, outputs, stdout, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.base, "main did not move by one commit, let alone two")
        self.assertEqual(self.quiet(allow=(f"pivotal/issue-{ISSUE}/one",)), [])
        result = self.result(outputs)
        self.assertEqual((result.status, result.why), ("draft", "multi_commit"))
        self.assertEqual(self.remote_files(f"pivotal/issue-{ISSUE}/one"), {"a.txt", "b.txt"}, "its draft PR holds both commits")

    # --- F-3: a landed stack is reported landed even when a draft fails

    def landed_with_a_refused_draft(self) -> tuple[Path, str, dict]:
        self.ready("one", {"src/one.txt": "one\n"})
        self.artifact("lone", self.patch_of({"src/lone.txt": "l\n"}))  # no review: a draft, not a candidate
        code, _, stdout, err = self.run_job("one", "lone")
        self.assertEqual(code, 0, stdout + err)
        out = Path(self.last_env["OUT_DIR"])
        self.put_gate(out, "one")
        head = self.heads(out)["one"]
        draft = next(x for x in assemble.read_land(out / assemble.LAND)["slices"] if x["outcome"] == "draft")
        self.route_list(self.gh, draft["branch"], [])
        self.gh.route(f"pr create --repo {REPO} --draft --base main --head {draft['branch']} ", fakegh.fail("HTTP 502"))
        self.route_seal(self.gh, head)
        code, outputs, _, err = self.publish(out, LEVEL="land")
        return out, head, {"code": code, "outputs": outputs, "err": err}

    def test_a_landed_stack_whose_slice_draft_pull_request_failed_is_still_landed_in_the_result_and_the_report(self):
        out, head, ran = self.landed_with_a_refused_draft()
        self.assertEqual(ran["code"], 1, "the job is red: a draft was not published")
        self.assertEqual(self.main_tip(), head, "main moved")
        result = self.result(ran["outputs"])
        self.assertEqual((result.status, result.sha), ("landed", head))
        outcome = report.decide({"ack": "success", "plan": "success", "publish": "failure"}, ran["outputs"]["result"])
        self.assertEqual((outcome.status, outcome.sha), ("landed", head), "the issue is not labelled blocked while main has moved")
        self.assertIn("publish", outcome.note)
        self.assertFalse(outcome.red)

    def test_a_failed_publish_job_is_failed_unless_its_result_says_the_base_moved(self):
        landed = json.dumps({"status": "landed", "built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5", "sha": "a" * 40})
        ready = json.dumps({"status": "ready", "built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5", "pr": 5})
        partial = json.dumps({"status": "draft", "why": "partial_land", "built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5"})
        draft = json.dumps({"status": "draft", "why": "no_stack", "built_by": "claude:anthropic/sonnet", "reviewed_by": "none", "pr": 5})
        failed = json.dumps({"status": "failed", "why": "seal_failed"})
        up = {"ack": "success", "plan": "success", "publish": "failure"}
        self.assertEqual(report.decide(up, landed).status, "landed")
        self.assertEqual(report.decide(up, ready).status, "ready")
        self.assertEqual(report.decide(up, partial).why, "partial_land")
        self.assertEqual((report.decide(up, draft).status, report.decide(up, draft).why), ("failed", "publish_failed"))
        self.assertEqual(report.decide(up, failed).why, "seal_failed", "N2: the publish job's own word is the report's")
        self.assertEqual(report.decide(up, "").why, "publish_failed")
        self.assertEqual(report.decide(up, "{").why, "publish_failed")
        for stage in ("build", "review", "assemble"):  # no other job's failure is overridden by a result (the gate's is: R3-1)
            self.assertEqual(report.decide({**up, "publish": "success", stage: "failure"}, landed).why, f"{stage}_failed")
        self.assertEqual(report.decide({**up, "publish": "cancelled"}, landed).why, "cancelled")
        self.assertEqual(report.decide({**up, "publish": "skipped"}, landed).why, "publish_failed")

    # --- F-9: a push that was accepted whose answer was lost is landed

    def test_an_accepted_push_whose_response_was_lost_is_landed_and_sealed_not_a_stack_pull_request(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        real = publish.Remote.network

        def lost(git, *args):
            answer = real(git, *args)
            if args[0] == "push" and args[-1].endswith(":refs/heads/main"):
                raise publish.assemble.GitError("connection reset after the push was accepted")
            return answer

        with mock.patch.object(publish.Remote, "network", lost):
            code, outputs, stdout, err = self.run_land(out, heads["two"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["two"])
        self.assertEqual(self.creates(), [], "no pull request for a stack that is on main")
        self.assertEqual(len(self.seals()), 1)
        self.assertEqual(self.result(outputs).status, "landed")

    def test_a_push_that_failed_and_whose_state_cannot_be_read_is_neither_landed_nor_a_pull_request(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        real = publish.Remote.network
        seen = []

        def broken(git, *args):
            if args[0] == "push":
                seen.append("push")
                real(git, *args)
                raise publish.assemble.GitError("lost")
            if args[0] == "ls-remote" and seen:
                raise publish.assemble.GitError("cannot read")
            return real(git, *args)

        with mock.patch.object(publish.Remote, "network", broken):
            code, outputs, _, err = self.run_land(out, head)
        self.assertEqual(code, 1)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.seals(), [], "nothing is sealed on a guess")
        self.assertEqual(self.result(outputs).why, "ff_unknown")

    def test_a_push_that_failed_with_main_unchanged_is_still_a_stack_pull_request(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.protect_main()
        self.route_stack_pr(self.gh, head)
        code, outputs, _, err = self.run_land(out, head)
        self.assertEqual((code, self.result(outputs).status, self.main_tip()), (0, "ready", self.base), err)

    # --- F-2: the rest of the stack is published

    def test_a_partial_land_publishes_the_whole_stack_as_a_draft_stack_pull_request_naming_the_first_one_left_out(self):
        out = self.stacked("one", "two", "three", bad=("two",))
        heads = self.heads(out)
        self.route_stack_pr(self.gh, heads["three"], draft=True)
        self.route_seal(self.gh, heads["one"])
        code, outputs, stdout, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["one"], "the green prefix still lands")
        self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], heads["three"], "the stack branch holds every slice")
        (create,) = self.creates()
        self.assertIn("--draft", create)
        body = Path(create[create.index("--body-file") + 1]).read_text()
        self.assertIn("first slice not landed: two (gate_red)", body)
        self.assertEqual(json.loads(self.seals()[0]["input"])["head_sha"], heads["one"], "the seal is on m")
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("draft", "partial_land", 20))

    def test_a_partial_stack_that_cannot_land_is_one_draft_pull_request_of_the_whole_stack_and_no_seal(self):
        out = self.stacked("one", "two", bad=("two",))
        heads = self.heads(out)
        self.move_main()
        self.route_stack_pr(self.gh, heads["two"], draft=True)
        code, outputs, _, err = self.run_land(out, heads["one"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], heads["two"])
        (create,) = self.creates()
        self.assertIn("--draft", create)
        self.assertEqual(self.seals(), [], "no PR is at m, so there is nothing to seal")
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("draft", "partial_stack", 20))

    def test_an_empty_prefix_still_publishes_the_stack_as_a_draft_pull_request_that_names_why(self):
        out = self.stacked("one", "two", bad=("one",))
        heads = self.heads(out)
        self.route_stack_pr(self.gh, heads["two"], draft=True)
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.main_tip(), self.quiet()), (self.base, []))
        self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], heads["two"])
        (create,) = self.creates()
        body = Path(create[create.index("--body-file") + 1]).read_text()
        self.assertIn("first slice not landed: one (gate_red)", body)
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("draft", "gate_red", 20))

    def test_a_rerun_of_the_rest_finds_its_draft_pull_request_by_the_branch(self):
        out = self.stacked("one", "two", bad=("two",))
        heads = self.heads(out)
        self.route_stack_pr(self.gh, heads["two"], draft=True)
        self.route_seal(self.gh, heads["one"])
        self.assertEqual(self.publish(out, LEVEL="land")[0], 0)
        again = fakegh.FakeGh(self.root / "bin2")
        self.route_list(again, STACK, [self.pr(STACK, heads["two"], 20)])
        self.route_seal(again, heads["one"], run=4243)
        code, outputs, stdout, err = self.publish(out, again, LEVEL="land", RUN_ID="4243")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.creates(again), [])
        self.assertEqual(self.result(outputs).pr, 20)

    # --- F-5: the real state of an existing pull request

    def test_an_existing_stack_pull_request_that_is_a_draft_when_a_ready_one_was_wanted_is_said_so(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.route_list(self.gh, STACK, [self.pr(STACK, head, 20, isDraft=True)])
        self.route_seal(self.gh, head)
        code, outputs, stdout, err = self.publish(out, LEVEL="prepare")
        self.assertEqual(code, 0, err)
        self.assertIn("stack: exists (#20) is a draft", stdout)
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("draft", "stack_pr_draft", 20))

    def test_an_existing_ready_pull_request_never_makes_a_strangers_run_ready(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.route_list(self.gh, STACK, [self.pr(STACK, head, 20, isDraft=False)])
        code, outputs, stdout, err = self.publish(out, LEVEL="land", TRUSTED="false")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.result(outputs).status, self.result(outputs).why), ("draft", "untrusted_stack"))

    # --- F-6: failures that name the action

    def test_a_pull_request_of_the_branch_at_another_head_names_what_to_do(self):
        out = self.stacked("one")
        self.route_list(self.gh, STACK, [self.pr(STACK, "e" * 40, 4)])
        code, outputs, _, err = self.publish(out, LEVEL="prepare")
        self.assertEqual(code, 1)
        self.assertEqual(self.result(outputs).why, "pr_differs")
        self.assertTrue(report.WHY["pr_differs"].startswith("a pull request for the branch is already open"))
        self.assertIn("close it", report.WHY["pr_differs"])

    def test_every_publish_failure_has_its_own_why_that_names_the_action(self):
        for word in ("pr_differs", "branch_differs", "push_rejected", "gh_failed", "ff_unknown", "stack_pr_draft"):
            with self.subTest(word):
                self.assertIn(word, report.WHY)
                self.assertEqual(report.parse_result(json.dumps({"status": "failed", "why": word})).why, word)
        self.assertNotEqual(len({report.WHY[w] for w in ("pr_differs", "branch_differs", "push_rejected", "gh_failed")}), 1)
        for reason in ("pr_differs", "branch_differs", "push_rejected", "gh_failed", "ff_unknown"):
            self.assertEqual(publish.FAIL_WHY[reason], reason)

    # --- F-10: the land.json gate word and the exact stop words

    def test_a_candidate_whose_land_json_gate_word_is_not_green_is_not_landable(self):
        out = self.stacked("one")
        land = assemble.read_land(out / assemble.LAND)
        entry = land["slices"][0]
        settings = publish.read_settings(self.penv(out, LEVEL="land"))
        git = publish.Remote(settings.work_dir, settings.base, settings.server, settings.repo, settings.token)
        fd = assemble.open_dir(str(self.slices))
        try:
            for gate_word in ("red", "none", ""):
                self.assertEqual(publish.slice_landable(settings, land, git, fd, {**entry, "gate": gate_word}, self.base),
                                 ("not_candidate", ""), gate_word)
        finally:
            os.close(fd)


class CorrectionTwoTest(StackCase):
    """The review's second round (N1 to N7): each of these failed on d1e8a59."""

    def publish_failure(self, outputs: dict) -> report.Outcome:
        return report.decide({"ack": "success", "plan": "success", "publish": "failure"}, outputs["result"])

    # --- N1: a landed stack is landed, whatever happens after the base moved

    def test_n1_a_seal_that_fails_after_the_whole_stack_landed_is_still_landed(self):  # probe P1
        out = self.stacked("one", "two")
        heads = self.heads(out)
        self.gh.route(f"api --method POST repos/{REPO}/check-runs ", fakegh.fail("HTTP 500"))
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1, "red: the seal is missing")
        self.assertEqual(self.main_tip(), heads["two"])
        result = self.result(outputs)
        self.assertEqual((result.status, result.sha), ("landed", heads["two"]))
        outcome = self.publish_failure(outputs)
        self.assertEqual((outcome.status, outcome.sha), ("landed", heads["two"]), "main moved: the issue is not blocked")
        self.assertIn("publish", outcome.note)
        self.assertIn("seal_failed", err)

    def test_n1_a_seal_that_fails_after_a_partial_landing_still_publishes_the_rest_as_a_draft(self):  # probe P2
        out = self.stacked("one", "two", "three", bad=("two",))
        heads = self.heads(out)
        self.route_stack_pr(self.gh, heads["three"], draft=True)
        self.gh.route(f"api --method POST repos/{REPO}/check-runs ", fakegh.fail("HTTP 500"))
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1)
        self.assertEqual(self.main_tip(), heads["one"])
        self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], heads["three"], "the stack branch exists")
        (create,) = self.creates()
        self.assertIn("--draft", create)
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("draft", "partial_land", 20))
        self.assertEqual(self.publish_failure(outputs).why, "partial_land")

    def test_n1_an_unexpected_error_after_the_landing_reads_as_landed(self):  # probe P3
        out = self.stacked("one", "two")
        heads = self.heads(out)
        with mock.patch.object(publish, "create_seal", side_effect=RuntimeError("boom")):
            code, outputs, _, err = self.run_land(out, heads["two"])
        self.assertEqual(code, 1)
        self.assertEqual(self.main_tip(), heads["two"])
        self.assertEqual((self.result(outputs).status, self.result(outputs).sha), ("landed", heads["two"]))
        self.assertNotIn("Traceback", err)

    def test_n1_an_unexpected_error_in_the_rest_draft_after_the_landing_keeps_the_landing(self):
        out = self.stacked("one", "two", bad=("two",))
        heads = self.heads(out)
        self.route_seal(self.gh, heads["one"])
        real = publish.open_pr

        def boom(*a, **kw):
            raise RuntimeError("boom")

        with mock.patch.object(publish, "open_pr", boom):
            code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1)
        self.assertEqual(self.main_tip(), heads["one"])
        self.assertEqual((self.result(outputs).status, self.result(outputs).why), ("draft", "partial_land"))
        self.assertIs(real, publish.open_pr)
        self.assertIn("the draft pull request for the rest failed (unexpected)", err, "recorded as the draft's failure, after the landing")
        self.assertNotIn("stack: failed", err)

    def test_n1_an_error_that_escapes_publish_stack_after_the_base_moved_still_reads_landed(self):
        real = publish.publish_stack
        for error in (RuntimeError("after the landing"), publish.PublishError("gh_failed")):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                out = self.stacked("one", "two")
                head = self.heads(out)["two"]

                def escapes(*args, error=error, **kw):
                    real(*args, **kw)
                    raise error

                with mock.patch.object(publish, "publish_stack", escapes):
                    code, outputs, _, _ = self.run_land(out, head)
                self.assertEqual(code, 1)
                self.assertEqual((self.result(outputs).status, self.result(outputs).sha), ("landed", head))

    def test_n1_a_seal_that_fails_before_anything_moved_is_still_failed(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.move_main()
        self.route_stack_pr(self.gh, head)
        self.gh.route(f"api --method POST repos/{REPO}/check-runs ", fakegh.fail("HTTP 500"))
        code, outputs, _, _ = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1)
        self.assertEqual((self.result(outputs).status, self.result(outputs).why), ("failed", "seal_failed"))

    # --- N2: the publish job's word reaches the report

    def test_n2_every_failed_word_of_publish_is_the_reports_word_and_still_red(self):
        for word in sorted(set(publish.FAIL_WHY.values())):
            with self.subTest(word):
                result = json.dumps({"status": "failed", "why": word})
                outcome = report.decide({"ack": "success", "plan": "success", "publish": "failure"}, result)
                self.assertEqual((outcome.status, outcome.why), ("failed", word))
                self.assertTrue(outcome.red)
                self.assertIn(word, report.PUBLISH_WHY)

    def test_n2_a_pr_at_another_head_reads_pr_differs_in_the_report(self):
        out = self.stacked("one")
        self.route_list(self.gh, STACK, [self.pr(STACK, "e" * 40, 4)])
        code, outputs, _, _ = self.publish(out, LEVEL="prepare")
        self.assertEqual((code, self.publish_failure(outputs).why), (1, "pr_differs"))

    def test_n2_a_failed_result_with_any_other_word_is_publish_failed(self):
        up = {"ack": "success", "plan": "success", "publish": "failure"}
        # R3-5: a word publish can give a failed slice (gate_red, build_failed, ...) is the report's word; the rest is generic
        for word in ("cancelled", "partial_land", "no_result", "candidate_only", "publish_failed", "gate_failed"):
            self.assertEqual(report.decide(up, json.dumps({"status": "failed", "why": word})).why, "publish_failed", word)

    # --- N3: an existing READY pull request holding a slice that did not pass

    def test_n3_a_ready_stack_pull_request_holding_an_unpassed_slice_is_not_partial_stack(self):  # probe P4
        out = self.stacked("one", "two", bad=("two",))
        heads = self.heads(out)
        self.route_list(self.gh, STACK, [self.pr(STACK, heads["two"], 20, isDraft=False)])
        code, outputs, stdout, err = self.publish(out, LEVEL="prepare")
        self.assertEqual(code, 0, err)
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("draft", "partial_stack_ready", 20))
        self.assertIn("partial_stack_ready", report.WHY)
        self.assertNotIn("merge it", report.WHY["partial_stack_ready"].split(":")[0])
        self.assertIn("draft", stdout)

    def test_n3_a_ready_stack_pull_request_with_an_empty_prefix_is_not_the_gate_word_either(self):
        out = self.stacked("one", "two", bad=("one",))
        heads = self.heads(out)
        self.route_list(self.gh, STACK, [self.pr(STACK, heads["two"], 20, isDraft=False)])
        result = self.result(self.publish(out, LEVEL="prepare")[1])
        self.assertEqual(result.why, "partial_stack_ready")

    # --- N6: an open DRAFT stack pull request is no pull request to seal

    def test_n6_an_existing_draft_stack_pull_request_at_the_tip_gets_no_seal(self):
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.move_main()
        self.route_list(self.gh, STACK, [self.pr(STACK, head, 20, isDraft=True)])
        code, outputs, _, err = self.run_land(out, head)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.seals(), [], "a draft is not a PR the land job may merge")
        self.assertEqual(self.result(outputs).why, "stack_pr_draft")

    # --- N7: partial_land is a draft status

    def test_n7_only_a_draft_partial_land_stands_over_a_failed_publish_job(self):
        up = {"ack": "success", "plan": "success", "publish": "failure"}
        for status in ("failed", "ready"):
            result = json.dumps({"status": status, "why": "partial_land", "built_by": "claude:anthropic/sonnet",
                                 "reviewed_by": "codex:openai/gpt-5", "pr": 3})
            outcome = report.decide(up, result)
            self.assertNotEqual(outcome.why, "partial_land", status)
        draft = json.dumps({"status": "draft", "why": "partial_land", "built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5"})
        self.assertEqual(report.decide(up, draft).why, "partial_land")

    # --- N4: the one validator holds the one-commit rule

    def test_n4_a_forged_two_commit_candidate_is_not_a_land_json(self):  # probe P5
        out = self.stacked("one")
        land = assemble.read_land(out / assemble.LAND)
        entry = land["slices"][0]
        entry["commits"] = ["a" * 40, entry["head"]]
        for commits in (entry["commits"], []):
            entry["commits"] = commits
            with self.assertRaises(assemble.LandError):
                assemble.check_land(land)
        (out / assemble.LAND).write_bytes(json.dumps(land).encode())
        with self.assertRaises(assemble.LandError):
            assemble.read_land(out / assemble.LAND)

    def test_n4_a_two_commit_draft_is_still_a_valid_land_json(self):
        self.ready("one", {"a.txt": "1\n"}, {"b.txt": "2\n"})
        code, _, stdout, err = self.run_job("one")
        self.assertEqual(code, 0, stdout + err)
        land = assemble.read_land(Path(self.last_env["OUT_DIR"]) / assemble.LAND)
        self.assertEqual((land["slices"][0]["outcome"], len(land["slices"][0]["commits"])), ("draft", 2))

    def test_n4_the_prefix_still_stops_at_a_slice_of_two_commits_if_it_is_handed_one(self):
        out = self.stacked("one")
        land = assemble.read_land(out / assemble.LAND)
        entry = land["slices"][0]
        settings = publish.read_settings(self.penv(out, LEVEL="land"))
        git = publish.Remote(settings.work_dir, settings.base, settings.server, settings.repo, settings.token)
        fd = assemble.open_dir(str(self.slices))
        try:
            self.assertEqual(publish.slice_landable(settings, land, git, fd, {**entry, "commits": ["a" * 40, entry["head"]]}, self.base),
                             ("multi_commit", ""))
        finally:
            os.close(fd)

    # --- N9 (a): the stack-level draft that fails after the base moved (mutant M25)

    def test_n9_a_stack_draft_pull_request_that_fails_after_a_partial_landing_leaves_the_landing_alone(self):
        out = self.stacked("one", "two", bad=("two",))
        heads = self.heads(out)
        self.route_list(self.gh, STACK, [])
        self.gh.route(f"pr create --repo {REPO} --draft --base main --head {STACK} ", fakegh.fail("HTTP 502"))
        self.route_seal(self.gh, heads["one"])
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1, "red: the draft for the rest was not made")
        self.assertIn("stack/draft", err, "named as the stack's draft, not as a slice")
        self.assertIn("the draft pull request for the rest failed (gh_failed)", err)
        self.assertEqual(self.main_tip(), heads["one"])
        self.assertEqual(len(self.seals()), 1, "the landing was sealed")
        result = self.result(outputs)
        self.assertEqual((result.status, result.why), ("draft", "partial_land"))
        self.assertEqual(self.publish_failure(outputs).why, "partial_land")

    # --- N9 (c): what a partial stack pull request says it leaves out

    def test_n9_a_partial_stack_pull_request_names_what_it_leaves_out_and_says_so(self):
        out = self.stacked("one", "two", bad=("two",))
        heads = self.heads(out)
        self.move_main()
        self.route_stack_pr(self.gh, heads["two"], draft=True)
        code, outputs, _, err = self.run_land(out, heads["one"])
        self.assertEqual(code, 0, err)
        (create,) = self.creates()
        body = Path(create[create.index("--body-file") + 1]).read_text()
        self.assertIn("slices that passed: one not in the landable prefix: two first slice not landed: two (gate_red)", body)
        self.assertIn("why a pull request: rest", body)
        self.assertIn("did not pass the gate", body)
        self.assertEqual((self.result(outputs).status, self.result(outputs).why), ("draft", "partial_stack"))


class CorrectionThreeTest(StackCase):
    """The review's third round (R3-1 to R3-8): the R3-1, R3-2, R3-5 and R3-6 tests failed on e27b885."""

    UP = {"ack": "success", "plan": "success", "build": "success", "review": "success", "assemble": "success"}

    def decide(self, result: str, gate_job: str = "failure", publish_job: str = "success") -> report.Outcome:
        return report.decide({**self.UP, "gate": gate_job, "publish": publish_job}, result)

    # --- R3-1: a red gate job does not hide what publish did

    def test_r3_1_a_red_gate_with_the_green_prefix_landed_reads_partial_land_with_the_link(self):
        out = self.stacked("one", "two", "three", bad=("two",))
        heads = self.heads(out)
        self.route_seal(self.gh, heads["one"])
        self.route_stack_pr(self.gh, heads["three"], draft=True)
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["one"], "the base moved")
        outcome = self.decide(outputs["result"])
        self.assertEqual((outcome.status, outcome.why, outcome.pr), ("draft", "partial_land", 20))
        self.assertIn("gate", outcome.note)

    def test_r3_1_a_red_gate_does_not_hide_a_landed_or_ready_result(self):
        landed = json.dumps({"status": "landed", "built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5", "sha": "a" * 40})
        ready = json.dumps({"status": "ready", "built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5", "pr": 5})
        for publish_job in ("success", "failure"):
            self.assertEqual(self.decide(landed, publish_job=publish_job).status, "landed")
            self.assertEqual(self.decide(ready, publish_job=publish_job).status, "ready")
        self.assertIn("gate", self.decide(landed).note)

    def test_r3_1_a_red_gate_without_such_a_result_is_gate_failed(self):
        draft = json.dumps({"status": "draft", "why": "gate_red", "built_by": "none", "reviewed_by": "none", "pr": 3})
        other_draft = json.dumps({"status": "draft", "why": "partial_stack", "built_by": "none", "reviewed_by": "none", "pr": 3})
        for result in ("", "{", draft, other_draft, json.dumps({"status": "failed", "why": "seal_failed"})):
            with self.subTest(result=result):
                outcome = self.decide(result)
                self.assertEqual((outcome.status, outcome.why), ("failed", "gate_failed"))

    def test_r3_1_a_result_of_a_publish_job_that_did_not_run_is_no_landing(self):
        landed = json.dumps({"status": "landed", "built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5", "sha": "a" * 40})
        for publish_job in ("skipped", "cancelled", ""):
            self.assertEqual(self.decide(landed, publish_job=publish_job).why, "gate_failed", publish_job)

    # --- R3-2: a READY stack pull request at the tip holds the slice that did not pass

    def test_r3_2_a_partial_landing_with_a_ready_stack_pull_request_at_the_tip_is_partial_stack_ready(self):
        out = self.stacked("one", "two", "three", bad=("two",))
        heads = self.heads(out)
        self.route_seal(self.gh, heads["one"])
        self.route_list(self.gh, STACK, [self.pr(STACK, heads["three"], 20, isDraft=False)])
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["one"], "the green prefix landed")
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("draft", "partial_land_ready", 20))

    def test_r3_2_a_partial_landing_with_a_draft_stack_pull_request_stays_partial_land(self):
        out = self.stacked("one", "two", "three", bad=("two",))
        heads = self.heads(out)
        self.route_seal(self.gh, heads["one"])
        self.route_list(self.gh, STACK, [self.pr(STACK, heads["three"], 20, isDraft=True)])
        _, outputs, _, _ = self.publish(out, LEVEL="land")
        self.assertEqual((self.result(outputs).why, self.result(outputs).pr), ("partial_land", 20))

    def test_r3_2_a_whole_landing_is_not_touched_by_it(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        _, outputs, _, _ = self.run_land(out, heads["two"])
        self.assertEqual(self.result(outputs).status, "landed")

    # --- R3-6: the opened pull request stays the link when the stack's own seal fails

    def test_r3_6_a_ready_stack_pull_request_whose_seal_fails_keeps_its_link(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        self.move_main()
        self.route_stack_pr(self.gh, heads["two"])
        self.gh.route(f"api --method POST repos/{REPO}/check-runs ", fakegh.fail("HTTP 500"))
        code, outputs, _, _ = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1)
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("failed", "seal_failed", 20))

    def test_r3_6_a_stack_failure_before_any_pull_request_has_no_link(self):
        out = self.stacked("one")
        self.move_main()
        self.route_list(self.gh, STACK, [self.pr(STACK, "e" * 40, 4)])
        _, outputs, _, _ = self.publish(out, LEVEL="prepare")
        self.assertEqual((self.result(outputs).why, self.result(outputs).pr), ("pr_differs", 0))

    # --- R3-5: publish's own words, derived

    def test_r3_5_publish_why_is_every_word_a_failed_publish_result_can_carry(self):
        every = set(publish.FAIL_WHY.values()) | set(publish.WHY_OF.values()) | {"build_failed"}
        self.assertEqual(set(report.PUBLISH_WHY), every)
        self.assertEqual(set(publish.PUBLISH_WHY), every)
        up = {"ack": "success", "plan": "success", "publish": "failure"}
        for word in sorted(every):
            with self.subTest(word):
                outcome = report.decide(up, json.dumps({"status": "failed", "why": word, "built_by": "none"}))
                self.assertEqual((outcome.status, outcome.why), ("failed", word))

    def test_n1_residual_a_result_that_cannot_be_written_is_in_the_log_after_the_landing(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        with mock.patch.object(job, "write_outputs", side_effect=job.JobError("disk full")):
            code, outputs, _, err = self.run_land(out, heads["two"])
        self.assertEqual(code, 1)
        self.assertEqual(self.main_tip(), heads["two"], "main moved")
        self.assertEqual(outputs, {}, "no output was written: the report job reads publish_failed (a known gap)")
        self.assertEqual(report.decide({"ack": "success", "plan": "success", "publish": "failure"}, "").why, "publish_failed")
        self.assertIn("RESULT could not be written to the job outputs", err)
        self.assertIn(heads["two"], err, "the landing is in the log")


class CorrectionFourTest(StackCase):
    """The review's fourth round (R4-F1 to F5), through the real publish code path."""

    UP = {"ack": "success", "plan": "success", "build": "success", "review": "success", "assemble": "success"}

    def decide(self, result: str, gate_job: str = "success", publish_job: str = "success") -> report.Outcome:
        return report.decide({**self.UP, "gate": gate_job, "publish": publish_job}, result)

    def landed_with_ready_pr(self, head_key: str = "three") -> tuple[dict, dict]:
        out = self.stacked("one", "two", "three", bad=("two",))
        heads = self.heads(out)
        self.route_seal(self.gh, heads["one"])
        self.route_list(self.gh, STACK, [self.pr(STACK, heads["three"], 20, isDraft=False)])
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), heads["one"], "the green prefix landed")
        return outputs, heads

    # --- F1: a landed partial stack with a READY PR stands over a red gate or a failed publish job

    def test_f1_the_landed_partial_stack_stands_over_a_red_gate_and_a_failed_publish_job(self):
        outputs, _ = self.landed_with_ready_pr()
        for gate_job, publish_job in (("failure", "success"), ("success", "failure"), ("failure", "failure")):
            with self.subTest(gate=gate_job, publish=publish_job):
                outcome = self.decide(outputs["result"], gate_job, publish_job)
                self.assertEqual((outcome.status, outcome.why, outcome.pr), ("draft", "partial_land_ready", 20))
                self.assertIn("moved the base", outcome.note)

    def test_f1_a_ready_pull_request_that_landed_nothing_stands_and_says_the_base_did_not_move(self):
        out = self.stacked("one", "two", bad=("two",))
        heads = self.heads(out)
        self.route_list(self.gh, STACK, [self.pr(STACK, heads["two"], 20, isDraft=False)])
        before = self.main_tip()
        _, outputs, _, _ = self.publish(out, LEVEL="prepare")
        self.assertEqual(self.main_tip(), before, "nothing landed")
        self.assertEqual(self.result(outputs).why, "partial_stack_ready")
        for gate_job, publish_job in (("failure", "success"), ("success", "failure")):
            with self.subTest(gate=gate_job, publish=publish_job):
                outcome = self.decide(outputs["result"], gate_job, publish_job)
                self.assertEqual((outcome.status, outcome.why, outcome.pr), ("draft", "partial_stack_ready", 20))
                self.assertIn("the base did not move", outcome.note)

    def test_f1_nothing_reads_landed_unless_publish_wrote_it(self):
        """Stage results x RESULT shapes: only a publish that succeeded or failed (not skipped, cancelled, absent) lets a
        moved/opened word stand, and a RESULT of the non-standing kind never does."""
        outputs, _ = self.landed_with_ready_pr()
        agents = {"built_by": "claude:anthropic/sonnet", "reviewed_by": "codex:openai/gpt-5"}
        shapes = {
            "partial_land_ready": outputs["result"],
            "partial_stack_ready": json.dumps({"status": "draft", "why": "partial_stack_ready", "pr": 20, **agents}),
            "landed": json.dumps({"status": "landed", "sha": "a" * 40, **agents}),
            "partial_stack": json.dumps({"status": "draft", "why": "partial_stack", "pr": 20, **agents}),
            "failed": json.dumps({"status": "failed", "why": "seal_failed", "pr": 20}),
            "empty": "", "bad": "{",
        }
        standing = {"partial_land_ready", "partial_stack_ready", "landed"}
        for gate_job in ("success", "failure", "skipped", "cancelled", ""):
            for publish_job in ("success", "failure", "skipped", "cancelled", ""):
                for name, shape in shapes.items():
                    outcome = self.decide(shape, gate_job, publish_job)
                    ran = gate_job in ("success", "failure") and publish_job in ("success", "failure")
                    with self.subTest(gate=gate_job, publish=publish_job, shape=name):
                        if ran and name in standing:
                            self.assertEqual((outcome.status, outcome.why), ("landed", "") if name == "landed" else ("draft", name))
                        elif "failure" in (gate_job, publish_job) or not ran:
                            self.assertEqual(outcome.status, "failed")
                        if outcome.status == "landed":
                            self.assertEqual(name, "landed")
                            self.assertTrue(ran)

    def test_f1_the_old_word_alone_no_longer_means_the_base_moved(self):
        outputs, _ = self.landed_with_ready_pr()
        self.assertNotEqual(self.result(outputs).why, "partial_stack_ready")
        self.assertIn("partial_land_ready", report.WHY)

    # --- F2: a READY PR at an older head is named

    def test_f2_a_ready_pull_request_at_an_older_head_after_a_partial_landing_is_named(self):
        out = self.stacked("one", "two", "three", bad=("two",))
        heads = self.heads(out)
        self.route_seal(self.gh, heads["one"])
        self.route_list(self.gh, STACK, [self.pr(STACK, "e" * 40, 20, isDraft=False)])
        code, outputs, _, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1, err)
        self.assertEqual(self.main_tip(), heads["one"], "the green prefix landed")
        result = self.result(outputs)
        self.assertEqual((result.status, result.why, result.pr), ("draft", "partial_land_ready", 20))
        outcome = self.decide(outputs["result"], "failure", "failure")
        self.assertEqual((outcome.why, outcome.pr), ("partial_land_ready", 20))

    def test_f2_a_draft_pull_request_at_an_older_head_is_still_no_ready_word(self):
        out = self.stacked("one", "two", "three", bad=("two",))
        heads = self.heads(out)
        self.route_seal(self.gh, heads["one"])
        self.route_list(self.gh, STACK, [self.pr(STACK, "e" * 40, 20, isDraft=True)])
        _, outputs, _, _ = self.publish(out, LEVEL="land")
        self.assertEqual((self.result(outputs).why, self.result(outputs).pr), ("partial_land", 20))

    # --- F4: both logs named

    def test_f4_a_red_gate_and_a_failed_publish_name_both_logs(self):
        outputs, _ = self.landed_with_ready_pr()
        both = self.decide(outputs["result"], "failure", "failure")
        self.assertIn("gate log", both.note)
        self.assertIn("publish log", both.note)
        only_gate = self.decide(outputs["result"], "failure", "success")
        self.assertNotIn("publish log", only_gate.note)
        only_publish = self.decide(outputs["result"], "success", "failure")
        self.assertNotIn("gate log", only_publish.note)

    def test_f4_a_landed_result_names_both_logs_after_a_failed_draft(self):
        out = self.stacked("one", "two")
        heads = self.heads(out)
        self.gh.route(f"api --method POST repos/{REPO}/check-runs ", fakegh.fail("HTTP 500"))
        code, outputs, _, _ = self.publish(out, LEVEL="land")
        self.assertEqual(code, 1)
        both = self.decide(outputs["result"], "failure", "failure")
        self.assertEqual(both.status, "landed")
        self.assertIn("publish log", both.note)
        self.assertIn("gate log", both.note)
