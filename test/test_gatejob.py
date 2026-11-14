"""The gate job (`gate[k]`, F4): engine/gatejob.py `prepare` and `run`.

Both steps are called in-process against real git repositories (a stack made by hand, bundled the way assemble does),
the real engine/run.sh and checks that are shell commands. No test reaches GitHub or a provider. Nothing here has run on
real GitHub.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import nolive  # noqa: F401  (no test sweeps the live /proc)
from engine import assemble, buildjob, gate, gatejob, job

ISSUE = 7
STACK = assemble.stack_branch(ISSUE)
CLEAN_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, *args: str, input: bytes | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args], cwd=repo, check=True,
        capture_output=True, input=input, env={**os.environ, **CLEAN_ENV},
    ).stdout.decode().strip()


class GateCase(unittest.TestCase):
    # (slice id, [files it adds]); each slice is one commit unless it has two names in COMMITS_OF.
    SLICES = (("a", ("a.txt",)), ("b", ("b.txt",)))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q")
        (self.origin / "base.txt").write_text("base\n", encoding="utf-8")
        (self.origin / ".gitignore").write_text("build/\n", encoding="utf-8")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-qm", "base")
        self.base = git(self.origin, "rev-parse", "HEAD")
        self.make_stack()
        self.work = self.root / "work"
        self.output = self.root / "output"
        self.output.write_text("", encoding="utf-8")
        self.engine = self.root / "pivotal"
        shutil.copytree(buildjob.ROOT / "engine", self.engine / "engine", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(buildjob.ROOT / "lines", self.engine / "lines")
        # No test may sweep the live process table of this shared machine (scope `user` kills same-uid processes).
        self.live_user_guards = []
        real_init = buildjob.Guard.__init__

        def checked(guard, *args, **kw):
            real_init(guard, *args, **kw)
            if guard.scope == "user" and Path(guard.proc) == Path("/proc"):
                self.live_user_guards.append(guard)
                raise AssertionError("a Guard(scope='user') was built on the live /proc in a test")

        patcher = mock.patch.object(buildjob.Guard, "__init__", checked)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertEqual(self.live_user_guards, []))
        signal.signal(signal.SIGALRM, lambda *_: self.fail("the gate step hung"))
        signal.alarm(90)
        self.addCleanup(signal.alarm, 0)

    # --- the fixtures: a stack, its bundle, land.json and a checkout of the base ---

    def make_stack(self, extra: dict | None = None) -> None:
        """origin gets the stack branch (one commit per slice; `extra` maps a slice id to a callable run before its commit),
        a bundle, a land.json that describes them and a fresh checkout of the base alone."""
        git(self.origin, "checkout", "-q", "-B", STACK, self.base)
        self.heads: dict[str, str] = {}
        slices = []
        for slice_id, names in self.SLICES:
            for name in names:
                (self.origin / name).write_text(f"{slice_id}\n", encoding="utf-8")
            if extra and slice_id in extra:
                extra[slice_id](self.origin)
            git(self.origin, "add", "-A")
            git(self.origin, "commit", "-qm", f"slice {slice_id}")
            self.heads[slice_id] = git(self.origin, "rev-parse", "HEAD")
            slices.append(
                {"id": slice_id, "outcome": "candidate", "reason": "stacked", "candidate": True, "branch": STACK,
                 "head": self.heads[slice_id], "commits": [self.heads[slice_id]], "built_by": "claude:anthropic/sonnet",
                 "gate": "green", "fell_back": [], "score": None, "target": "", "reviewed_by": ""}
            )
        self.slices = slices
        self.land_dir = self.root / "land"
        shutil.rmtree(self.land_dir, ignore_errors=True)
        self.land_dir.mkdir()
        git(self.origin, "bundle", "create", "-q", str(self.land_dir / assemble.BUNDLE), f"^{self.base}", f"refs/heads/{STACK}")
        self.write_land()
        self.repo = self.root / "repo"
        shutil.rmtree(self.repo, ignore_errors=True)
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "fetch", "-q", str(self.origin), self.base)
        git(self.repo, "checkout", "-q", "--detach", self.base)

    def write_land(self, **over) -> None:
        bundle = (self.land_dir / assemble.BUNDLE).read_bytes()
        land = {
            "version": 2, "issue": ISSUE, "base_sha": self.base, "baseline": "green",
            "bundle": {"name": assemble.BUNDLE, "sha256": hashlib.sha256(bundle).hexdigest(), "bytes": len(bundle)},
            "has_stack": True, "positions": [s["id"] for s in self.slices], "slices": self.slices,
        }
        land.update(over)
        text = assemble.land_text(land)
        (self.land_dir / assemble.LAND).write_bytes(text)
        self.land = land
        self.land_sha = hashlib.sha256(text).hexdigest()

    def env(self, slice_id: str = "b", **over) -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"], "HOME": str(self.root), "ID": slice_id, "ISSUE": str(ISSUE), "BASE_SHA": self.base,
            "LAND_SHA256": self.land_sha, "LAND_DIR": str(self.land_dir), "REPO_DIR": str(self.repo),
            "WORK_DIR": str(self.work), "ENGINE_DIR": str(self.engine), "GITHUB_OUTPUT": str(self.output),
            "CHECK": "true", "SETUP": "",
        }
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def step(self, fn, **over) -> int:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = job.run_job(fn, self.env(**over))
            except SystemExit as exit_:
                code = exit_.code
        self.stdout, self.stderr, self.code = out.getvalue(), err.getvalue(), code
        return code

    def gate_job(self, slice_id: str = "b", check: str = "true", **over) -> dict:
        """prepare, then run, as the workflow does; asserts both exit 0 and returns the published gate.json."""
        self.assertEqual(self.step(gatejob.prepare, ID=slice_id, **over), 0, self.stderr)
        self.assertEqual(self.step(gatejob.run, ID=slice_id, CHECK=check, **over), 0, self.stderr)
        return self.gate()

    # --- reading the outcome ---

    @property
    def out(self) -> Path:
        return self.work / "out"

    def gate(self) -> dict:
        return gate.read_gate(self.out / gate.GATE)

    def outputs(self) -> dict[str, str]:
        lines = self.output.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines)

    def assert_not_green(self, doc: dict, **expected) -> None:
        self.assertFalse(doc["green"], doc)
        for key, value in expected.items():
            self.assertEqual(doc[key], value, doc)
        self.assertEqual(self.outputs(), {"published": "true", "gate_ok": "false"})

    def assert_refused(self, fn=gatejob.run, **over) -> None:
        """A refusal is loud (exit 1), happens before the first write, and leaves no artifact."""
        before = self.output.read_text(encoding="utf-8")
        self.assertEqual(self.step(fn, **over), 1, self.stderr)
        self.assertIn("refused:", self.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), before)
        self.assertFalse(self.out.exists())


class GreenTest(GateCase):
    def test_a_green_check_on_the_head_of_the_stack_is_a_green_gate_for_exactly_that_sha(self):
        doc = self.gate_job("b", check="test -f a.txt && test -f b.txt")
        self.assertEqual(
            doc, {"version": gate.GATE_VERSION, "issue": ISSUE, "id": "b", "sha": self.heads["b"], "check": "green", "head_unchanged": True,
                  "clean": True, "green": True, "score": None, "target": None},
        )
        self.assertEqual(self.outputs(), {"published": "true", "gate_ok": "true"})
        self.assertEqual(sorted(os.listdir(self.out)), ["gate.json"])
        gate.confirm(self.land, doc)  # what publish does with it

    def test_each_prefix_is_checked_at_its_own_commit_and_never_sees_a_later_slice(self):
        first = self.gate_job("a", check="test -f a.txt && test ! -e b.txt")
        self.assertEqual((first["sha"], first["green"]), (self.heads["a"], True))
        shutil.rmtree(self.work)
        self.output.write_text("", encoding="utf-8")
        git(self.repo, "checkout", "-q", "--detach", self.base)
        second = self.gate_job("b", check="test -f a.txt && test -f b.txt")
        self.assertEqual((second["sha"], second["green"]), (self.heads["b"], True))

    def test_a_check_may_write_what_the_committed_gitignore_ignores(self):
        doc = self.gate_job("b", check="mkdir -p build && echo x > build/out.txt")
        self.assertTrue(doc["green"], doc)

    def test_prepare_leaves_a_clean_detached_checkout_of_the_commit(self):
        self.assertEqual(self.step(gatejob.prepare, ID="a"), 0, self.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.heads["a"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertTrue((self.repo / "a.txt").exists())
        self.assertFalse((self.repo / "b.txt").exists())

    def test_the_check_sees_the_job_environment_and_no_secret(self):
        dump = self.root / "env.txt"
        env = {"SUBSCRIPTION_TOKEN": "tok-123", "ACTIONS_RUNTIME_TOKEN": "rt-123"}  # the runner injects the runtime token
        self.assertEqual(self.step(gatejob.prepare, ID="b", **env), 0, self.stderr)
        self.assertEqual(self.step(gatejob.run, ID="b", CHECK=f"env > {dump}", **env), 0, self.stderr)
        seen = dump.read_text(encoding="utf-8")
        for secret in ("tok-123", "rt-123"):
            self.assertNotIn(secret, seen)
        self.assertTrue(self.gate()["green"])

    def test_a_job_given_a_github_token_is_refused_before_anything_is_written(self):
        self.assertEqual(self.step(gatejob.prepare, ID="b"), 0, self.stderr)
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            with self.subTest(name):
                self.assert_refused(ID="b", **{name: "ghs_secret"})
                self.assertIn(name, self.stderr)
                self.assertNotIn("ghs_secret", self.stderr)

    def test_prepare_refuses_a_token_or_a_door_secret_too_before_it_touches_the_checkout(self):
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
            with self.subTest(name):
                self.assertEqual(self.step(gatejob.prepare, ID="b", **{name: "ghs_secret"}), 1, self.stderr)
                self.assertIn("refused:", self.stderr)
                self.assertIn(name, self.stderr)
                self.assertNotIn("ghs_secret", self.stderr)
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)  # nothing was fetched or checked out
                self.assertFalse(self.work.exists())

    def test_a_job_given_a_door_secret_is_refused_before_anything_is_written(self):
        self.assertEqual(self.step(gatejob.prepare, ID="b"), 0, self.stderr)
        self.assert_refused(ID="b", ANTHROPIC_API_KEY="sk-x")
        self.assertIn("ANTHROPIC_API_KEY", self.stderr)
        self.assertNotIn("sk-x", self.stderr)


class ShallowCheckoutTest(GateCase):
    """The workflow's checkout of the base is shallow (`actions/checkout` at its default fetch-depth 1), the base its root.
    The stack arrives as a bundle, which carries no parent of the base, so the gate reads the same as on a full checkout."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(self.repo)
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "fetch", "-q", "--depth=1", str(self.origin), self.base)
        git(self.repo, "checkout", "-q", "--detach", self.base)

    def test_a_green_check_on_a_shallow_checkout_of_the_base_is_the_same_green_gate(self):
        self.assertEqual(git(self.repo, "rev-parse", "--is-shallow-repository"), "true")
        doc = self.gate_job("b", check="test -f a.txt && test -f b.txt")
        self.assertEqual(
            doc, {"version": gate.GATE_VERSION, "issue": ISSUE, "id": "b", "sha": self.heads["b"], "check": "green", "head_unchanged": True,
                  "clean": True, "green": True, "score": None, "target": None},
        )
        self.assertEqual(self.outputs(), {"published": "true", "gate_ok": "true"})


class ScoreTest(GateCase):
    """The hill-climb in the gate job: the score command runs after the check, and the gate is green only at or above the target."""

    def scored(self, command: str, target: str = "0.9") -> dict:
        import base64
        return self.gate_job("b", check="true", SCORE=base64.b64encode(command.encode()).decode(), TARGET=target)

    def test_a_score_at_or_above_the_target_is_a_green_gate_that_carries_both(self):
        doc = self.scored("echo 0.92")
        self.assertEqual((doc["green"], doc["score"], doc["target"]), (True, 0.92, "0.9"))
        self.assertEqual(self.outputs(), {"published": "true", "gate_ok": "true"})

    def test_a_score_below_the_target_is_not_green(self):
        self.assert_not_green(self.scored("echo 0.4"), check="green", score=0.4, target="0.9")

    def test_a_scorer_that_prints_nothing_is_not_green(self):
        self.assert_not_green(self.scored("true"), check="red", score=None, target="0.9")

    def test_a_scorer_that_prints_a_word_is_not_green(self):
        self.assert_not_green(self.scored("echo nine"), check="red", score=None, target="0.9")

    def test_a_scorer_that_swaps_its_score_file_for_a_symlink_is_not_green(self):
        planted = self.root / "planted.txt"
        planted.write_text("0.99\n", encoding="utf-8")
        score_file = self.work / "logs" / "score-b.txt"
        self.assert_not_green(self.scored(f"rm -f {score_file}; ln -s {planted} {score_file}"), check="red", score=None, target="0.9")


class RepoScorerTest(GateCase):
    """The gate runs the engine's scorer, never the one the repo committed (buildjob.score_command anchors it to the engine
    checkout). A committed engine/score/image.py that prints 1, and a committed score.txt, cannot set the score."""

    def setUp(self):
        super().setUp()

        def committed_scorer(origin: Path) -> None:
            (origin / "engine" / "score").mkdir(parents=True, exist_ok=True)
            (origin / "engine" / "score" / "image.py").write_text("print('1')\n", encoding="utf-8")
            (origin / "score.txt").write_text("1\n", encoding="utf-8")

        self.make_stack(extra={"b": committed_scorer})  # the same stack, with the repo's own scorer in slice b's commit

    def test_the_repo_committed_scorer_that_prints_one_is_red_with_no_score(self):
        import base64
        self.assertEqual(git(self.origin, "show", f"{self.heads['b']}:engine/score/image.py"), "print('1')")
        command = base64.b64encode(b"python3 -I {scorer}").decode()
        doc = self.gate_job("b", check="true", SCORE=command, TARGET="0.9")
        self.assert_not_green(doc, check="red", score=None, target="0.9")


class NotLandableTest(GateCase):
    """The acceptance: a test that commits during gate is not landable."""

    def run_check(self, check: str, slice_id: str = "b") -> dict:
        return self.gate_job(slice_id, check=check)

    def test_a_check_that_commits_exits_0_and_is_not_green(self):
        doc = self.run_check("git -c user.name=x -c user.email=x@x commit -q --allow-empty -m sneaky")
        self.assert_not_green(doc, check="green", head_unchanged=False, clean=False, sha=self.heads["b"])
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD"), self.heads["b"])  # it really moved
        self.assertEqual(self.code, 0)

    def test_a_check_that_commits_a_change_is_not_green(self):
        doc = self.run_check("echo x > new.txt && git add -A && git -c user.name=x -c user.email=x@x commit -qm c")
        self.assert_not_green(doc, check="green", head_unchanged=False)

    def test_a_check_that_resets_to_an_earlier_commit_is_not_green(self):
        doc = self.run_check(f"git reset -q --hard {self.base}")
        self.assert_not_green(doc, check="green", head_unchanged=False)

    def test_a_check_that_checks_out_another_ref_is_not_green(self):
        doc = self.run_check(f"git checkout -q --detach {self.heads['a']}")
        self.assert_not_green(doc, check="green", head_unchanged=False)

    def test_a_check_that_commits_and_resets_the_work_tree_back_is_still_not_green(self):
        doc = self.run_check("git -c user.name=x -c user.email=x@x commit -q --allow-empty -m s && git status -s")
        self.assert_not_green(doc, head_unchanged=False)

    def test_a_check_that_modifies_a_tracked_file_is_not_green(self):
        self.assert_not_green(self.run_check("echo changed >> a.txt"), check="green", head_unchanged=True, clean=False)

    def test_a_check_that_leaves_an_untracked_file_is_not_green(self):
        self.assert_not_green(self.run_check("echo x > leftover.txt"), check="green", head_unchanged=True, clean=False)

    def test_a_check_that_deletes_a_tracked_file_or_changes_its_mode_is_not_green(self):
        self.assert_not_green(self.run_check("rm a.txt"), clean=False)
        self.reset_job()
        self.assert_not_green(self.run_check("chmod +x a.txt"), clean=False)

    def test_a_check_that_stages_a_change_is_not_green(self):
        self.assert_not_green(self.run_check("echo x > staged.txt && git add staged.txt"), clean=False)

    def test_a_failing_check_is_red_even_when_it_leaves_everything_alone(self):
        self.assert_not_green(self.run_check("exit 3"), check="red", head_unchanged=True, clean=True)

    def test_an_empty_check_is_never_green(self):
        for check in ("", "   \n", "# nothing", ":"):
            with self.subTest(check=check):
                self.reset_job()
                doc = self.run_check(check)
                self.assert_not_green(doc, check="none", head_unchanged=True, clean=True)

    def test_a_check_that_does_not_parse_or_whose_setup_exits_is_red(self):
        self.assert_not_green(self.run_check("if true; then"), check="red")
        self.reset_job()
        self.assertEqual(self.step(gatejob.prepare, ID="b"), 0, self.stderr)
        self.assertEqual(self.step(gatejob.run, ID="b", SETUP="exit 0", CHECK="true"), 0, self.stderr)
        self.assert_not_green(self.gate(), check="red")

    def test_a_check_that_times_out_is_red(self):
        with mock.patch.object(buildjob, "GATE_SECONDS", 2):
            doc = self.run_check("sleep 30")
        self.assert_not_green(doc, check="red")

    # --- hiding a change from git status ---

    def test_assume_unchanged_and_skip_worktree_do_not_hide_a_change(self):
        self.assert_not_green(self.run_check("git update-index --assume-unchanged a.txt && echo x >> a.txt"), clean=False)
        self.reset_job()
        self.assert_not_green(self.run_check("git update-index --skip-worktree a.txt && echo x >> a.txt"), clean=False)

    def test_an_info_exclude_does_not_hide_an_untracked_file(self):
        doc = self.run_check("mkdir -p .git/info && echo '*' > .git/info/exclude && echo x > hidden.txt")
        self.assert_not_green(doc, clean=False)

    def test_a_planted_gitignore_does_not_hide_files_or_itself(self):
        self.assert_not_green(self.run_check("echo '*' > .gitignore.tmp && mkdir d && echo '*' > d/.gitignore && echo x > d/y"), clean=False)
        self.reset_job()
        self.assert_not_green(self.run_check("mkdir d && echo '*' > d/.gitignore"), clean=False)

    def test_a_global_ignore_file_in_the_scratch_home_does_not_hide_a_file(self):
        ignore = self.work / "home" / ".config" / "git" / "ignore"
        doc = self.run_check(f"mkdir -p {ignore.parent} && echo '*' > {ignore} && echo x > hidden.txt")
        self.assert_not_green(doc, clean=False)

    def test_an_edit_to_the_committed_gitignore_shows(self):
        self.assert_not_green(self.run_check("echo '*' >> .gitignore"), clean=False)

    def test_a_nested_repository_is_not_clean(self):
        self.assert_not_green(self.run_check("mkdir n && git -C n init -q && echo x > n/f"), clean=False)

    def test_a_config_that_names_a_command_never_runs_it(self):
        marker = self.root / "ran"
        check = (
            f"printf '[core]\\n\\tfsmonitor = touch {marker}\\n[filter \"f\"]\\n\\tclean = touch {marker}\\n"
            f"[diff \"d\"]\\n\\ttextconv = touch {marker}\\n' >> .git/config; "
            "printf '* filter=f diff=d\\n' > .gitattributes"
        )
        doc = self.run_check(check)
        self.assert_not_green(doc, check="green")
        self.assertFalse(marker.exists(), "git ran a command the check configured")

    def test_a_hook_the_check_plants_never_runs(self):
        marker = self.root / "hook-ran"
        hook = f"#!/bin/sh\ntouch {marker}\n"
        check = f"mkdir -p .git/hooks && printf '{hook}' > .git/hooks/post-checkout && chmod +x .git/hooks/post-checkout"
        self.run_check(check)
        self.assertFalse(marker.exists())

    def test_a_git_dir_replaced_by_a_file_or_a_symlink_is_not_green(self):
        doc = self.run_check(f"mv .git ../moved-git && ln -s ../moved-git .git")
        self.assert_not_green(doc, head_unchanged=False, clean=False)
        shutil.rmtree(self.root / "moved-git", ignore_errors=True)

    # --- refs, git metadata and the index: a check may not change them either (review finding 1, 2) ---

    PLANT_REPLACE = (
        "echo changed >> b.txt && git add -A && t=$(git write-tree) && "
        "c=$(git -c user.name=x -c user.email=x@x commit-tree $t -m x) && git replace -f HEAD $c"
    )

    def test_a_replace_ref_cannot_hide_a_dirty_tree_from_the_clean_test(self):
        doc = self.run_check(self.PLANT_REPLACE)
        self.assert_not_green(doc, check="green", clean=False)

    def test_tree_is_clean_ignores_replace_refs(self):
        self.assertEqual(self.step(gatejob.prepare, ID="b"), 0, self.stderr)
        paths = buildjob.Paths(self.repo, self.work)
        self.assertTrue(gatejob.tree_is_clean(paths))
        (self.repo / "b.txt").write_text("dirty\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        tree = git(self.repo, "write-tree")
        fake = git(self.repo, "commit-tree", tree, "-m", "x")
        git(self.repo, "replace", "-f", "HEAD", fake)
        git(self.repo, "reset", "-q")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # the lie: git alone says clean
        self.assertFalse(gatejob.tree_is_clean(paths))

    def test_tree_is_clean_uses_a_fixed_excludes_setup(self):
        self.assertEqual(self.step(gatejob.prepare, ID="b"), 0, self.stderr)
        paths = buildjob.Paths(self.repo, self.work)
        ignore = self.root / "global-ignore"
        ignore.write_text("*\n", encoding="utf-8")
        with open(self.repo / ".git" / "config", "a", encoding="utf-8") as fh:
            fh.write(f"[core]\n\texcludesFile = {ignore}\n")
        (self.repo / "hidden.txt").write_text("x\n", encoding="utf-8")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # plain git is fooled
        self.assertFalse(gatejob.tree_is_clean(paths))

    def test_a_check_that_adds_a_ref_is_not_green(self):
        for name, check in (
            ("tag", "git tag sneaky"),
            ("annotated tag", "git -c user.name=x -c user.email=x@x tag -a -m m sneaky"),
            ("branch", "git branch sneaky"),
            ("note", "git -c user.name=x -c user.email=x@x notes add -m n"),
            ("other ref", "git update-ref refs/pivotal/x HEAD"),
        ):
            with self.subTest(name):
                self.reset_job()
                self.assert_not_green(self.run_check(check), check="green", head_unchanged=True, clean=False)

    def test_a_check_that_stashes_its_change_is_not_green(self):
        self.assert_not_green(self.run_check("echo changed >> a.txt && git stash -q"), check="green", clean=False)

    def test_a_check_that_writes_packed_refs_is_not_green(self):
        doc = self.run_check("printf '# pack-refs with: peeled fully-peeled sorted \\n' > .git/packed-refs")
        self.assert_not_green(doc, check="green", clean=False)

    def test_a_check_that_touches_git_metadata_is_not_green(self):
        for name, check in (
            ("info/exclude", "mkdir -p .git/info && echo 'x' > .git/info/exclude"),
            ("info/attributes", "mkdir -p .git/info && echo '*.txt -text' > .git/info/attributes"),
            ("config", "git config user.name evil"),
            ("hook", "mkdir -p .git/hooks && echo '#!/bin/sh' > .git/hooks/pre-commit"),
            ("shallow", f"echo {self.base} > .git/shallow"),
        ):
            with self.subTest(name):
                self.reset_job()
                self.assert_not_green(self.run_check(check), check="green", clean=False)

    def test_a_check_that_changes_only_the_index_is_not_green(self):
        for name, check in (
            ("skip-worktree", "git update-index --skip-worktree a.txt"),
            ("assume-unchanged", "git update-index --assume-unchanged a.txt"),
            ("intent-to-add", "echo x > n.txt && git add -N n.txt && rm n.txt"),
            ("removed from the index", "git rm -q --cached a.txt"),
        ):
            with self.subTest(name):
                self.reset_job()
                doc = self.run_check(check)
                self.assertFalse(doc["green"], (name, doc))

    def test_a_check_that_nests_directories_deeper_than_the_cap_under_git_info_is_not_green_and_still_publishes(self):
        # (A 1500-level tree cannot be made inside the check in reasonable time: its mkdir/cd take over a minute.
        # test_the_digest_of_a_deep_tree_does_not_recurse covers the depth that broke the recursive walk.)
        deep = f"mkdir -p .git/info && cd .git/info && i=0 && while [ $i -lt {gatejob.MAX_DEPTH + 8} ]; do mkdir d && cd d && i=$((i+1)); done"
        doc = self.run_check(deep)
        self.assert_not_green(doc, check="green", clean=False)
        self.assertEqual(self.code, 0)

    def test_the_digest_of_a_deep_tree_does_not_recurse(self):
        deep = self.root / "deep"
        deep.mkdir()
        fd = os.open(deep, os.O_RDONLY)
        for _ in range(1500):  # os.makedirs recurses too, so build it one level at a time
            os.mkdir("d", dir_fd=fd)
            nxt = os.open("d", os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        os.close(fd)
        entries: list = []
        gatejob._digest_path(deep, "deep", entries)
        self.assertEqual(entries[-1][1], "deep")
        self.assertLess(len(entries), gatejob.MAX_DEPTH + 3)

    # The object database. A check that rewrote a pack or a loose object so that HEAD's name maps to a commit whose tree
    # lists the edited file would make git say "clean" if git did not verify an object's hash on read. git 2.50.1 does
    # verify (and the runner's 2.43 was not available), so the forged object makes git fail, which is also not green: the
    # forge test below passes with or without the snapshot on this git. The three tests after it make the snapshot itself
    # observable with a git that lies, and fail without it.

    FORGE = """\
import os, subprocess, sys, zlib
def g(*args, **kw):
    return subprocess.run(["git", *args], check=True, capture_output=True, **kw).stdout
import glob
packs = sorted(glob.glob(".git/objects/pack/*.pack"))
datas = [open(name, "rb").read() for name in packs]
for name in packs:  # rewrite the object database: every pack becomes loose objects
    for ext in (".pack", ".idx", ".rev"):
        if os.path.exists(name[:-5] + ext):
            os.remove(name[:-5] + ext)
for data in datas:
    g("unpack-objects", "-q", input=data)
head = g("rev-parse", "HEAD").decode().strip()
loose = os.path.join(".git", "objects", head[:2], head[2:])
assert os.path.exists(loose), "the commit is not a loose object"
open("b.txt", "w").write("forged\\n")
blob = g("hash-object", "-w", "b.txt").decode().strip()
env = {**os.environ, "GIT_INDEX_FILE": os.path.join(".git", "forge-index")}
g("read-tree", "HEAD", env=env)
g("update-index", "--add", "--cacheinfo", f"100644,{blob},b.txt", env=env)
tree = g("write-tree", env=env).decode().strip()
os.remove(os.path.join(".git", "forge-index"))
lines = g("cat-file", "commit", head).split(b"\\n")
lines[0] = b"tree " + tree.encode()
body = b"\\n".join(lines)
data = zlib.compress(b"commit %d\\0" % len(body) + body)
new = loose + ".new"
open(new, "wb").write(data)
os.replace(new, loose)
"""

    def test_a_check_that_forges_the_head_commit_object_to_list_its_edit_is_not_green(self):
        forge = self.root / "forge.py"
        forge.write_text(self.FORGE, encoding="utf-8")
        doc = self.run_check(f"{shutil.which('python3')} {forge}")
        self.assert_not_green(doc, check="green")

    def test_a_check_that_repacks_the_object_database_without_changing_it_stays_green(self):
        doc = self.run_check("git repack -a -d -q -n")
        self.assertTrue(doc["green"], doc)

    def lying_git(self, want: str, needle: str, alter):
        """gatejob._git, except that the call with label ``want`` whose arguments hold ``needle`` answers ``alter(done)``."""
        real = gatejob._git

        def fake(paths, *args, label, text=False):
            done = real(paths, *args, label=label, text=text)
            return alter(done) if label == want and needle in args else done

        return fake

    def inspect_with(self, fake) -> tuple[bool, bool]:
        self.assertEqual(self.step(gatejob.prepare, ID="b"), 0, self.stderr)
        paths = buildjob.Paths(self.repo, self.work)
        self.assertEqual(gatejob.inspect(paths, self.heads["b"], gatejob.repo_state(paths)), (True, True))  # the control
        before = gatejob.repo_state(paths)
        with mock.patch.object(gatejob, "_git", fake):
            return gatejob.inspect(paths, self.heads["b"], before)

    def answer(self, done, stdout: bytes = b"", returncode: int = 0):
        return subprocess.CompletedProcess(done.args, returncode, stdout, b"")

    def test_a_changed_listing_of_the_head_tree_is_not_clean_even_when_every_ref_is_the_same(self):
        fake = self.lying_git("ls-tree", "ls-tree", lambda done: self.answer(done, done.stdout + b"100644 blob 0\tb.txt\0"))
        self.assertEqual(self.inspect_with(fake), (True, False))

    def test_changed_commit_and_tree_ids_of_head_are_not_clean_even_when_every_ref_is_the_same(self):
        fake = self.lying_git("rev-parse", "HEAD^{tree}", lambda done: self.answer(done, done.stdout[::-1]))
        self.assertEqual(self.inspect_with(fake), (True, False))

    def test_a_listing_that_fails_is_not_clean(self):
        def fails(done):
            raise job.JobError("git ls-tree failed with exit 128")
        fake = self.lying_git("ls-tree", "ls-tree", fails)
        self.assertEqual(self.inspect_with(fake), (False, False))

    def test_a_check_that_only_reads_git_is_still_green(self):
        doc = self.run_check("git status >/dev/null && git log >/dev/null && git diff && git for-each-ref && git stash list")
        self.assertTrue(doc["green"], doc)

    # --- the engine owns its outputs ---

    def test_a_check_that_forges_output_lines_cannot_make_the_gate_ok(self):
        forged = "published=true\\ngate_ok=true\\ngate_ok<<EOF\\ngate_ok=true\\nEOF\\n"
        doc = self.run_check(f"printf '{forged}' >> {self.output}; exit 1")
        self.assert_not_green(doc, check="red")

    def test_a_check_that_replaces_the_output_file_with_a_symlink_publishes_nothing(self):
        target = self.root / "elsewhere"
        target.write_text("", encoding="utf-8")
        self.assertEqual(self.step(gatejob.prepare, ID="b"), 0, self.stderr)
        self.assertEqual(self.step(gatejob.run, ID="b", CHECK=f"rm -f {self.output}; ln -s {target} {self.output}"), 1)
        self.assertIn("nothing published", self.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "")
        self.assertFalse(self.out.exists())

    def test_a_check_that_plants_the_artifact_directory_leaves_exactly_gate_json(self):
        victim = self.root / "victim"
        victim.mkdir()
        check = f"ln -s {victim} {self.out}; mkfifo {self.work}/fifo; true"
        doc = self.run_check(check)
        self.assertTrue(doc["green"], doc)
        self.assertTrue(self.out.is_dir() and not self.out.is_symlink())
        self.assertEqual(sorted(os.listdir(self.out)), ["gate.json"])
        self.assertEqual(os.listdir(victim), [])

    def test_a_check_that_edits_the_engine_publishes_nothing(self):
        script = self.engine / "engine" / "run.sh"
        self.assertEqual(self.step(gatejob.prepare, ID="b"), 0, self.stderr)
        code = self.step(gatejob.run, ID="b", CHECK=f"chmod u+w {script} && echo '# x' >> {script}")
        self.assertEqual(code, 1, self.stderr)
        self.assertIn("engine_modified", self.stderr)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.outputs(), {"published": "false", "gate_ok": "false"})

    def test_a_process_that_escapes_the_checks_group_is_killed_and_the_run_still_ends(self):
        pidfile = self.root / "pid"
        doc = self.run_check(f"setsid sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}; sleep 0.3")
        pid = int(pidfile.read_text(encoding="utf-8"))
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
            os.waitpid(pid, os.WNOHANG)
            os.kill(pid, 0)
        self.assertEqual(doc["check"], "green")

    def reset_job(self) -> None:
        """Back to the state before prepare, for a test that runs the job more than once."""
        buildjob.set_writable(self.engine, True)
        shutil.rmtree(self.work, ignore_errors=True)
        self.output.write_text("", encoding="utf-8")
        shutil.rmtree(self.repo, ignore_errors=True)
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "fetch", "-q", str(self.origin), self.base)
        git(self.repo, "checkout", "-q", "--detach", self.base)


class HostileCheckoutTest(GateCase):
    def test_a_commit_with_drivers_symlinks_and_odd_names_checks_out_and_gates_without_running_anything(self):
        marker = self.root / "driver-ran"

        def hostile(repo: Path) -> None:
            (repo / ".gitattributes").write_text(f"* filter=evil diff=evil merge=evil\n", encoding="utf-8")
            os.symlink("/etc/passwd", repo / "link")
            os.symlink("../../outside", repo / "dangling")
            (repo / "odd\nname.txt").write_text("x\n", encoding="utf-8")
            (repo / "-rf").write_text("x\n", encoding="utf-8")

        self.make_stack({"b": hostile})
        hook = self.repo / ".git" / "hooks"
        hook.mkdir(parents=True, exist_ok=True)
        (hook / "post-checkout").write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        (hook / "post-checkout").chmod(0o755)
        git(self.repo, "config", "filter.evil.smudge", f"touch {marker}; cat")
        git(self.repo, "config", "core.fsmonitor", f"touch {marker}")
        doc = self.gate_job("b", check="test -L link && test -f ./-rf")
        self.assertTrue(doc["green"], doc)
        self.assertFalse(marker.exists(), "a hook, filter or fsmonitor of the checkout ran")

    def test_a_check_that_follows_a_planted_symlink_into_the_engine_cannot_change_the_verdict(self):
        doc = self.gate_job("b", check=f"echo x > {self.root / 'outside'}")
        self.assertTrue(doc["green"], doc)


class TargetRefusalTest(GateCase):
    """Everything read_target and prepare refuse, each before the first write."""

    def test_an_id_that_is_not_a_stacked_slice_or_not_an_id_is_refused(self):
        for bad in ("zzz", "stack", "base", "A", "a/b", "a b", "", "../a", "a\nb", "x" * 41):
            with self.subTest(id=bad):
                self.assert_refused(gatejob.prepare, ID=bad)

    def test_a_missing_or_malformed_input_name_is_refused(self):
        for name, value in (("ISSUE", "x"), ("ISSUE", "0"), ("BASE_SHA", "0" * 40), ("BASE_SHA", "abc"), ("LAND_SHA256", "x"),
                            ("LAND_SHA256", None), ("LAND_DIR", None), ("LAND_DIR", "relative"), ("REPO_DIR", None)):
            with self.subTest(name=name, value=value):
                self.assert_refused(gatejob.prepare, **{name: value})

    def test_a_land_json_that_is_not_the_one_the_assemble_job_output_is_refused(self):
        self.assert_refused(gatejob.prepare, LAND_SHA256="0" * 64)
        self.assertIn("sha256", self.stderr)
        self.assert_refused(gatejob.run, LAND_SHA256="f" * 64)

    def test_a_land_json_that_is_edited_after_assemble_is_refused(self):
        path = self.land_dir / assemble.LAND
        path.write_bytes(path.read_bytes().replace(b'"baseline": "green"', b'"baseline": "green" '))
        self.assert_refused(gatejob.prepare)

    def test_a_land_json_that_is_a_symlink_a_fifo_a_directory_or_too_large_is_refused(self):
        path = self.land_dir / assemble.LAND
        real = path.read_bytes()
        for make in (lambda: (path.unlink(), os.symlink(self.root / "elsewhere", path)),
                     lambda: (path.unlink(), os.mkfifo(path)),
                     lambda: (path.unlink(), path.mkdir()),
                     lambda: (path.unlink(), path.write_bytes(b" " * (assemble.MAX_LAND_BYTES + 1))),
                     lambda: path.unlink()):
            (self.root / "elsewhere").write_bytes(real)
            with self.subTest():
                make()
                self.assert_refused(gatejob.prepare, LAND_SHA256=hashlib.sha256(real).hexdigest())
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                else:
                    path.unlink(missing_ok=True)
                path.write_bytes(real)

    def test_a_land_json_with_duplicate_keys_or_the_wrong_schema_is_refused(self):
        path = self.land_dir / assemble.LAND
        for text in (path.read_text(encoding="utf-8").replace('"version": 2,', '"version": 2, "version": 2,'),
                     '{"version": 1}', "[]", "not json", "[" * 5000):
            with self.subTest(text=text[:30]):
                path.write_bytes(text.encode())
                self.assert_refused(gatejob.prepare, LAND_SHA256=hashlib.sha256(text.encode()).hexdigest())

    def test_a_land_json_of_another_issue_or_base_is_refused(self):
        self.assert_refused(gatejob.prepare, ISSUE="8")
        self.assert_refused(gatejob.prepare, BASE_SHA="1" * 40)

    def test_a_baseline_that_is_not_green_gates_nothing(self):
        for baseline in ("red", "none"):
            with self.subTest(baseline=baseline):
                self.write_land(baseline=baseline)
                self.assert_refused(gatejob.prepare, LAND_SHA256=self.land_sha)

    def test_a_slice_that_is_not_on_the_stack_is_refused(self):
        draft = {**self.slices[1], "outcome": "draft", "reason": "gate_red", "candidate": False,
                 "branch": assemble.branch_name(ISSUE, "b")}
        stacked = [self.slices[0]]
        bundle = (self.land_dir / assemble.BUNDLE).read_bytes()
        git(self.origin, "update-ref", f"refs/heads/{assemble.branch_name(ISSUE, 'b')}", self.heads["b"])
        git(self.origin, "update-ref", f"refs/heads/{STACK}", self.heads["a"])
        (self.land_dir / assemble.BUNDLE).unlink()
        git(self.origin, "bundle", "create", "-q", str(self.land_dir / assemble.BUNDLE), f"^{self.base}",
            f"refs/heads/{STACK}", f"refs/heads/{assemble.branch_name(ISSUE, 'b')}")
        self.slices = [stacked[0], draft]
        self.write_land(positions=["a"])
        self.assertEqual(self.step(gatejob.prepare, ID="a", LAND_SHA256=self.land_sha), 0, self.stderr)
        self.assert_refused(gatejob.prepare, ID="b", LAND_SHA256=self.land_sha)
        self.assertIn("not a slice on the stack", self.stderr)
        self.assertTrue(bundle)

    def test_a_bundle_that_is_not_the_one_land_json_describes_is_refused(self):
        bundle = self.land_dir / assemble.BUNDLE
        data = bundle.read_bytes()
        bundle.write_bytes(data + b"x")
        self.assert_refused(gatejob.prepare)
        bundle.unlink()
        self.assert_refused(gatejob.prepare)
        os.symlink(self.root / "elsewhere", bundle)
        self.assert_refused(gatejob.prepare)

    def test_a_stack_with_a_commit_land_json_does_not_list_is_refused(self):
        """A bundle whose heads are right but whose history has an extra commit under the slice: count mismatch."""
        git(self.origin, "checkout", "-q", "-B", STACK, self.base)
        (self.origin / "hidden.txt").write_text("h\n", encoding="utf-8")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-qm", "unlisted")
        heads = {}
        slices = []
        for slice_id, names in self.SLICES:
            for name in names:
                (self.origin / name).write_text(f"{slice_id}\n", encoding="utf-8")
            git(self.origin, "add", "-A")
            git(self.origin, "commit", "-qm", f"slice {slice_id}")
            heads[slice_id] = git(self.origin, "rev-parse", "HEAD")
            slices.append({**self.slices[0], "id": slice_id, "head": heads[slice_id], "commits": [heads[slice_id]]})
        self.slices = slices
        (self.land_dir / assemble.BUNDLE).unlink()
        git(self.origin, "bundle", "create", "-q", str(self.land_dir / assemble.BUNDLE), f"^{self.base}", f"refs/heads/{STACK}")
        self.write_land()
        self.assert_refused(gatejob.prepare, ID="a", LAND_SHA256=self.land_sha)
        self.assertIn("exactly the commits", self.stderr)

    def test_a_checkout_that_is_not_at_the_base_is_refused(self):
        other = self.root / "other"
        other.mkdir()
        git(other, "init", "-q")
        (other / "z").write_text("z\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "z")
        self.assert_refused(gatejob.prepare, REPO_DIR=str(other))

    def test_run_refuses_a_checkout_prepare_did_not_leave_at_the_commit_and_clean(self):
        self.assert_refused(gatejob.run)  # no prepare: HEAD is the base
        self.assertEqual(self.step(gatejob.prepare), 0, self.stderr)
        (self.repo / "dirty.txt").write_text("x\n", encoding="utf-8")
        self.assert_refused(gatejob.run)

    def test_bad_paths_and_scope_are_refused(self):
        self.assertEqual(self.step(gatejob.prepare), 0, self.stderr)
        self.assert_refused(WORK_DIR="relative")
        self.assert_refused(WORK_DIR=None)
        self.assert_refused(GITHUB_OUTPUT=None)
        self.assert_refused(CONTAIN_SCOPE="everything")
        self.assert_refused(CONTAIN_SCOPE="user", PIVOTAL_CONTAIN_USER_OK=None)
        self.assert_refused(LINE="nonesuch")
        self.assert_refused(ENGINE_DIR=str(self.root / "nowhere"))


class ValidateTest(unittest.TestCase):
    def test_make_gate_never_turns_a_non_word_into_green(self):
        target = gatejob.Target({}, "a", 7, "1" * 40, "2" * 40, (), 1)
        for word in ("", "GREEN", "green\n", "ok", "yes"):
            doc = gatejob.make_gate(target, word, True, True)
            self.assertEqual((doc["check"], doc["green"]), ("red", False), word)
        self.assertTrue(gatejob.make_gate(target, "green", True, True)["green"])
        self.assertFalse(gatejob.make_gate(target, "none", True, True)["green"])

    def test_the_log_tail_cannot_start_a_workflow_command(self):
        log = Path(tempfile.mkdtemp()) / "log"
        self.addCleanup(shutil.rmtree, log.parent)
        log.write_text("::set-output name=x::y\n::error::boom\n\x1b[0m::add-mask::s\nfine\n", encoding="utf-8")
        for line in gatejob.log_tail(log):
            self.assertTrue(line.startswith("| "), line)
            self.assertFalse(line.startswith("::"))

    def test_the_log_tail_reads_only_the_end_of_a_huge_log(self):
        log = Path(tempfile.mkdtemp()) / "log"
        self.addCleanup(shutil.rmtree, log.parent)
        log.write_bytes(b"x" * (buildjob.GATE_TAIL_BYTES * 50) + b"\nlast line\n")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole log read")):
            self.assertEqual(gatejob.log_tail(log)[-1], "| last line")

    def test_the_job_minutes_are_derived_from_the_gate_budget(self):
        self.assertEqual(gatejob.JOB_MINUTES, -(-buildjob.GATE_SECONDS // 60) + gatejob.JOB_OVERHEAD_MINUTES)

    def test_main_refuses_a_bad_usage(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gatejob.main(["gatejob.py"], {}), 2)
            self.assertEqual(gatejob.main(["gatejob.py", "nope"], {}), 2)


if __name__ == "__main__":
    unittest.main()
