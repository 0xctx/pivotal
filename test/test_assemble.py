"""The assemble job: engine/assemble.py against real git and hand-made artifacts.

Every test builds its own repository and artifact directory in a temporary directory. The job is run in-process; the
only program it may start is /usr/bin/git (a spy checks that). No test reaches GitHub or a model. The artifact directory
is hostile input, so the tests plant what a build job's agent could: links, FIFOs, duplicate keys, oversize files,
patches that name `..`, `.git`, `.github`, or write through a symlink.
"""

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import tracemalloc
import unittest
import zlib
from pathlib import Path
from unittest import mock

import nolive  # noqa: F401  (no test sweeps the live /proc)
from engine import assemble, build, buildjob, job, plan, review

ISSUE = 7
BASELINE_FILES = {
    "README.md": "base readme\n",
    "src/a.txt": "alpha\n",
    ".github/workflows/ci.yml": "name: ci\n",
    ".gitattributes": "* filter=evil\n",
    "tools/run.sh": "#!/bin/sh\ntouch RAN_REPO_CODE\n",
}
OK = {
    "status": "ok", "reason": "", "agent": "claude", "gate": "green", "built_by": "claude:anthropic/sonnet",
    "fell_back": [], "healed": False, "slice": "", "round": 0, "workflow_change": False, "score": None, "target": "",
}
FAKE_ENV = {
    "GIT_CONFIG_GLOBAL": "/nonexistent", "GIT_DIR": "/nonexistent/.git", "GIT_WORK_TREE": "/nonexistent",
    "GIT_AUTHOR_NAME": "Mallory", "GIT_COMMITTER_NAME": "Mallory", "GIT_COMMITTER_DATE": "2030-01-01T00:00:00",
    "GIT_EXTERNAL_DIFF": "/bin/false", "GIT_SSH_COMMAND": "/bin/false",
}


def clean_git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin", "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
    }


def mbox(*commits: tuple[str, str]) -> bytes:
    """A format-patch style mailbox from (subject, diff text) pairs, for diffs git itself would not produce."""
    out = b""
    for n, (subject, diff) in enumerate(commits, start=1):
        out += (
            f"From {n:040x} Mon Sep 17 00:00:00 2001\nFrom: Dev <dev@example.com>\nDate: Mon, 1 Jan 2024 00:00:00 +0000\n"
            f"Subject: [PATCH] {subject}\n\n---\n{diff}\n-- \n2.50.1\n\n"
        ).encode()
    return out


def new_file_diff(path: str, text: str = "x\n", mode: str = "100644") -> str:
    lines = text.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode {mode}\nindex 0000000..1111111\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def delta_hunk_text(kind: str, stream: bytes, declared: int | None = None) -> str:
    """One hunk of a `GIT binary patch` as git writes it: the size line, then zlib(stream) in length-prefixed base85 lines."""
    raw = zlib.compress(stream)
    lines = []
    for i in range(0, len(raw), 52):
        chunk = raw[i : i + 52]
        n = len(chunk)
        letter = chr(ord("A") + n - 1) if n <= 26 else chr(ord("a") + n - 27)
        lines.append(letter + base64.b85encode(chunk + b"\0" * (-n % 4)).decode())
    return f"{kind} {len(stream) if declared is None else declared}\n" + "\n".join(lines) + "\n\n"


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def copy_delta(source: int, copies: int) -> bytes:
    """A git delta stream: header (source size, target size) and `copies` ops `0x80`, each copying 0x10000 bytes from offset 0."""
    return varint(source) + varint(copies * 0x10000) + b"\x80" * copies


class AssembleCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.base = self.make_repo()
        self.slices = self.root / "slices"
        self.slices.mkdir()
        self.reviews = self.root / "reviews"
        self.reviews.mkdir()
        self.n_runs = 0

    # --- fixtures

    def git(self, *args: str, cwd: Path | None = None, env: dict | None = None, input: bytes | None = None) -> str:
        done = subprocess.run(
            ["/usr/bin/git", *args], cwd=cwd or self.repo, env={**clean_git_env(self.home), **(env or {})},
            capture_output=True, check=True, input=input,
        )
        return done.stdout.decode().strip()

    def make_repo(self) -> str:
        self.repo.mkdir()
        self.git("init", "-q")
        for name, text in BASELINE_FILES.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.repo / "tools/run.sh").chmod(0o755)
        self.git("add", "-A")
        self.git("commit", "-qm", "base", env={"GIT_COMMITTER_DATE": "2024-03-01T12:00:00 +0000"})
        return self.git("rev-parse", "HEAD")

    def commit(self, files: dict[str, str | None], message: str = "change", date: str = "2024-05-05T05:05:05 +0000",
               author: str = "Dev") -> None:
        for name, text in files.items():
            path = self.repo / name
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", message, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
                                                 "GIT_AUTHOR_NAME": author})

    def patch_of(self, *commits: dict[str, str | None], **kw) -> bytes:
        """The format-patch of commits made on top of the base, which is restored afterwards."""
        for n, files in enumerate(commits):
            self.commit(files, message=f"change {n}", **kw)
        patch = self.git("format-patch", "--stdout", "--binary", f"{self.base}..HEAD").encode() if False else subprocess.run(
            ["/usr/bin/git", "format-patch", "--stdout", "--binary", "--no-ext-diff", "--no-textconv", f"{self.base}..HEAD"],
            cwd=self.repo, env=clean_git_env(self.home), capture_output=True, check=True,
        ).stdout
        self.git("reset", "-q", "--hard", self.base)
        return patch

    def artifact(self, slice_id: str, patch: bytes | None = None, result: dict | None = None, files: dict | None = None):
        """Write slice-<id>/ the way the build job does (ok result, patch, summary), with overrides."""
        d = self.slices / f"slice-{slice_id}"
        d.mkdir()
        res = {**OK, "slice": slice_id, **(result or {})}
        (d / "result.json").write_text(json.dumps(res))
        (d / "summary.md").write_text("summary\n")
        if patch is not None:
            (d / "slice.patch").write_bytes(patch)
        for name, data in (files or {}).items():
            path = d / name
            if path.exists() or path.is_symlink():
                path.unlink()
            if data is not None:
                path.write_bytes(data)
        return d

    def review_of(self, slice_id: str, patch: bytes, names: list[str], verdict: str = "READY", *, sha: str | None = None,
                  reviewer: str = "codex:openai/gpt-5", write: bool = True, **over) -> dict:
        """Write review-<id>/ as the review job does (verdict, review.md, reviewed_by) for a READY or NEEDS FIXES verdict.
        ``names`` are plain ASCII file names; ``over`` replaces verdict fields (the artifact stays self-consistent)."""
        sha = sha or hashlib.sha256(patch).hexdigest()
        quoted = review.files_of(f.encode() for f in names)
        data = {
            "verdict": verdict, "reason": "", "slice": slice_id, "round": 0, "patch_sha256": sha, "base": self.base,
            "built_by": OK["built_by"], "reviewed_by": reviewer, "files": quoted, "fell_back": [], **over,
        }
        text = "".join([f"Reviewed: {sha}\n", *(f"Covered: {q}\n" for q in quoted), f"Verdict: {verdict}\n"])
        if write:
            d = self.reviews / f"review-{slice_id}"
            d.mkdir()
            (d / "verdict").write_text(json.dumps(data))
            (d / "review.md").write_text(text)
            (d / "reviewed_by").write_text(data["reviewed_by"] + "\n")
        return data

    def ready(self, slice_id: str, files: dict[str, str | None], *more: dict[str, str | None], **result) -> bytes:
        """A slice artifact with an ok result and a READY review for the patch of these commits; returns the patch."""
        patch = self.patch_of(files, *more)
        names = sorted({n for commit in (files, *more) for n in commit})
        self.artifact(slice_id, patch, result=result or None)
        self.review_of(slice_id, patch, names)
        return patch

    def matrix(self, *ids: str) -> str:
        return plan.matrix_json([plan.Slice(i, f"title {i}", f"goal {i}") for i in ids])

    def env(self, *ids: str, **over) -> dict[str, str]:
        self.n_runs += 1
        run = self.root / f"run{self.n_runs}"
        run.mkdir()
        env = {
            "ISSUE": str(ISSUE), "BASE_SHA": self.base, "MATRIX": self.matrix(*ids), "BASELINE": "green", "KIND": "go",
            "REPO_DIR": str(self.repo), "SLICES_DIR": str(self.slices),
            "REVIEWS_DIR": str(self.reviews), "OUT_DIR": str(run / "land"),
            "WORK_DIR": str(run / "work"), "GITHUB_OUTPUT": str(run / "output"),
        }
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def run_job(self, *ids: str, **over) -> tuple[int, dict[str, str], str, str]:
        env = self.env(*ids, **over)
        Path(env["GITHUB_OUTPUT"]).write_text("")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = assemble.main(["run"], env)
        outputs = dict(line.split("=", 1) for line in Path(env["GITHUB_OUTPUT"]).read_text().splitlines())
        self.last_env = env
        return code, outputs, out.getvalue(), err.getvalue()

    def land(self, env: dict | None = None) -> dict:
        return assemble.read_land(Path((env or self.last_env)["OUT_DIR"]) / assemble.LAND)

    def land_hash(self) -> str:
        return hashlib.sha256((Path(self.last_env["OUT_DIR"]) / assemble.LAND).read_bytes()).hexdigest()

    def entry(self, land: dict, slice_id: str) -> dict:
        return next(s for s in land["slices"] if s["id"] == slice_id)

    def tree_of(self, env: dict, ref: str) -> dict[str, str]:
        work = Path(env["WORK_DIR"]) / "repo"
        names = self.git("ls-tree", "-r", "--name-only", ref, cwd=work).splitlines()
        return {n: self.git("show", f"{ref}:{n}", cwd=work) + "\n" for n in names}


class DraftBranchTest(AssembleCase):
    def test_every_patched_slice_becomes_a_draft_branch_and_one_bundle(self):
        self.artifact("one", self.patch_of({"src/a.txt": "alpha\none\n"}))
        self.artifact("two", self.patch_of({"src/b.txt": "bee\n"}, {"src/b.txt": "bee\nbee\n"}))
        code, outputs, stdout, _ = self.run_job("one", "two")
        self.assertEqual(code, 0, stdout)
        land = self.land()
        self.assertEqual([s["id"] for s in land["slices"]], ["one", "two"])
        for s in land["slices"]:
            self.assertEqual((s["outcome"], s["reason"], s["candidate"]), ("draft", "verdict_missing", False))
            self.assertEqual(s["branch"], f"pivotal/issue-{ISSUE}/{s['id']}")
        one, two = self.entry(land, "one"), self.entry(land, "two")
        self.assertEqual(len(one["commits"]), 1)
        self.assertEqual(len(two["commits"]), 2)
        self.assertEqual(self.tree_of(self.last_env, one["head"])["src/a.txt"], "alpha\none\n")
        self.assertEqual(self.tree_of(self.last_env, two["head"])["src/b.txt"], "bee\nbee\n")
        self.assertNotIn("src/b.txt", self.tree_of(self.last_env, one["head"]), "each draft is cut from the base")
        # One bundle, holding exactly the two branches at the heads land.json names.
        out = Path(self.last_env["OUT_DIR"])
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted([assemble.BUNDLE, assemble.LAND]))
        assemble.check_bundle(land, out / assemble.BUNDLE)
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        self.assertEqual(self.git("rev-parse", f"refs/heads/{one['branch']}", cwd=work), one["head"])
        for e in (one, two):  # the bundle is usable: a repository that has the base can fetch the drafts from it
            self.assertEqual(
                self.git("ls-remote", str(out / assemble.BUNDLE), f"refs/heads/{e['branch']}").split()[0], e["head"])
        self.assertEqual(outputs, {"positions": "", "gate_matrix": "[]", "has_stack": "false", "land_sha256": self.land_hash(), "assembled": "true"})
        self.assertEqual((land["has_stack"], land["positions"], land["base_sha"], land["issue"]), (False, [], self.base, ISSUE))

    def test_the_drafts_follow_the_matrix_order(self):
        for name in ("zeta", "alpha", "mid"):
            self.artifact(name, self.patch_of({f"{name}.txt": "x\n"}))
        self.run_job("zeta", "alpha", "mid")
        self.assertEqual([s["id"] for s in self.land()["slices"]], ["zeta", "alpha", "mid"])

    def test_a_red_baseline_makes_a_draft_that_is_no_candidate(self):
        self.artifact("one", self.patch_of({"a.txt": "x\n"}))
        self.run_job("one", BASELINE="red")
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("draft", "baseline_red", False))
        self.assertEqual(self.land()["baseline"], "red")

    def test_gate_none_is_a_draft_not_a_candidate(self):
        self.ready("one", {"a.txt": "x\n"}, gate="none")
        self.run_job("one")
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"], e["candidate"], e["gate"]), ("draft", "gate_none", False, "none"))
        self.assertFalse(self.land()["has_stack"])

    def test_baseline_none_is_a_draft_not_a_candidate(self):
        self.ready("one", {"a.txt": "x\n"})
        self.run_job("one", BASELINE="none")
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("draft", "baseline_red", False))
        self.assertFalse(self.land()["has_stack"])

    def test_no_slice_with_a_patch_means_no_bundle(self):
        code, outputs, _, _ = self.run_job("one")
        self.assertEqual(code, 0)
        self.assertEqual(self.land()["bundle"], None)
        self.assertEqual([p.name for p in Path(self.last_env["OUT_DIR"]).iterdir()], [assemble.LAND])
        self.assertEqual(outputs["assembled"], "true")

    def test_a_binary_file_and_a_mode_change_survive(self):
        blob = bytes(range(256)) * 20
        (self.repo / "bin.dat").write_bytes(blob)
        self.git("add", "bin.dat")
        self.git("commit", "-qm", "bin")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "bin.dat").write_bytes(blob[::-1])
        self.git("commit", "-qam", "flip")
        patch = subprocess.run(["/usr/bin/git", "format-patch", "--stdout", "--binary", "HEAD~1..HEAD"], cwd=self.repo,
                               env=clean_git_env(self.home), capture_output=True, check=True).stdout
        self.git("reset", "-q", "--hard", self.base)
        self.artifact("one", patch)
        self.run_job("one")
        e = self.entry(self.land(), "one")
        self.assertEqual(e["outcome"], "draft")
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        got = subprocess.run(["/usr/bin/git", "show", f"{e['head']}:bin.dat"], cwd=work, capture_output=True,
                             env=clean_git_env(self.home)).stdout
        self.assertEqual(got, blob[::-1])


class DeterminismTest(AssembleCase):
    def test_two_runs_on_the_same_input_give_identical_commits_bundle_and_land_json(self):
        patch = self.patch_of({"src/a.txt": "alpha\none\n"}, {"README.md": "changed\n"})
        self.artifact("one", patch)
        self.artifact("two", self.patch_of({"x.txt": "x\n"}))
        with mock.patch.dict(os.environ, FAKE_ENV):  # nothing of the caller's git environment may reach the commits
            self.run_job("one", "two")
            first = self.land(), (Path(self.last_env["OUT_DIR"]) / assemble.LAND).read_bytes(), \
                (Path(self.last_env["OUT_DIR"]) / assemble.BUNDLE).read_bytes()
        with mock.patch.dict(os.environ, {**FAKE_ENV, "GIT_COMMITTER_DATE": "2031-02-02T00:00:00", "TZ": "Asia/Tokyo"}):
            self.run_job("one", "two")
            second = self.land(), (Path(self.last_env["OUT_DIR"]) / assemble.LAND).read_bytes(), \
                (Path(self.last_env["OUT_DIR"]) / assemble.BUNDLE).read_bytes()
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1], "land.json is byte for byte the same")
        self.assertEqual(first[2], second[2], "so is the bundle")
        self.assertEqual(len(self.entry(first[0], "one")["commits"]), 2)

    def test_the_commits_do_not_depend_on_the_patchs_authors_or_dates(self):
        files = [{"src/a.txt": "alpha\none\n"}, {"README.md": "changed\n"}]
        self.artifact("one", self.patch_of(*files, date="2020-01-01T00:00:00 +0000", author="Alice"))
        self.run_job("one")
        first = self.entry(self.land(), "one")["commits"]
        shutil.rmtree(self.slices / "slice-one")
        self.artifact("one", self.patch_of(*files, date="2035-12-31T23:59:59 +0900", author="Bob"))
        self.run_job("one")
        self.assertEqual(self.entry(self.land(), "one")["commits"], first)

    def test_commit_dates_and_identity_are_fixed_from_the_base(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}, {"b.txt": "2\n"}, {"c.txt": "3\n"}))
        self.run_job("one")
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        base_time = int(self.git("show", "-s", "--format=%ct", self.base))
        commits = self.entry(self.land(), "one")["commits"]
        self.assertEqual(len(commits), 3)
        for n, sha in enumerate(commits, start=1):
            fields = self.git("show", "-s", "--format=%at|%ct|%an|%ae|%cn|%ce|%P", sha, cwd=work).split("|")
            parent = self.base if n == 1 else commits[n - 2]
            self.assertEqual(fields, [str(base_time + n)] * 2 + ["pivotal", "pivotal@users.noreply.github.com"] * 2 + [parent])
        self.assertEqual(self.git("show", "-s", "--format=%s", commits[1], cwd=work), "change 1")

    def test_a_different_patch_gives_a_different_commit(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}))
        self.run_job("one")
        first = self.entry(self.land(), "one")["head"]
        shutil.rmtree(self.slices / "slice-one")
        self.artifact("one", self.patch_of({"a.txt": "2\n"}))
        self.run_job("one")
        self.assertNotEqual(self.entry(self.land(), "one")["head"], first)

    def test_the_repos_own_configuration_hooks_and_drivers_never_run(self):
        # The patch is made first: the fixture's own commits must not run the hostile hooks it sets up next.
        self.artifact("one", self.patch_of({"src/a.txt": "alpha\nmore\n"}))
        marker = self.root / "marker"
        hooks = self.root / "hooks"
        hooks.mkdir()
        for name in ("post-applypatch", "applypatch-msg", "pre-applypatch", "post-checkout", "post-commit", "post-merge"):
            (hooks / name).write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
            (hooks / name).chmod(0o755)
            (self.repo / ".git" / "hooks" / name).write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
            (self.repo / ".git" / "hooks" / name).chmod(0o755)
        driver = self.root / "driver"
        driver.write_text(f"#!/bin/sh\necho driver >> {marker}\ncat\n")
        driver.chmod(0o755)
        self.git("config", "filter.evil.clean", str(driver))
        self.git("config", "filter.evil.smudge", str(driver))
        self.git("config", "core.hooksPath", str(hooks))
        self.git("config", "core.fsmonitor", str(driver))
        self.git("config", "diff.external", str(driver))
        (self.home / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks}\n[user]\n\tname = Evil\n")
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "GIT_EXTERNAL_DIFF": str(driver)}):
            code, *_ = self.run_job("one")
        self.assertEqual(code, 0)
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")
        self.assertFalse((self.repo / "RAN_REPO_CODE").exists())
        e = self.entry(self.land(), "one")
        self.assertEqual(e["outcome"], "draft")
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        self.assertEqual(self.git("show", "-s", "--format=%an", e["head"], cwd=work), "pivotal")

    def test_only_git_is_ever_started(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}))
        self.artifact("two", self.patch_of({".github/workflows/x.yml": "x\n"}))
        real = subprocess.run
        started = []

        def spy(command, *a, **kw):
            started.append(command[0])
            return real(command, *a, **kw)

        with mock.patch.object(subprocess, "run", spy):
            code, *_ = self.run_job("one", "two")
        self.assertEqual(code, 0)
        self.assertTrue(started)
        self.assertEqual(set(started), {assemble.GIT})
        self.assertEqual(assemble.GIT, "/usr/bin/git")


class WorkflowBlockTest(AssembleCase):
    def blocked(self, patch: bytes, **result) -> dict:
        self.artifact("one", patch, result=result or None)
        code, _, _, _ = self.run_job("one")
        self.assertEqual(code, 0)
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("blocked", "workflow_change", False))
        self.assertEqual((e["branch"], e["head"], e["commits"]), (None, None, []))
        self.assertIsNone(self.land()["bundle"])
        return e

    def test_a_workflow_file_change_is_blocked_even_when_the_result_says_no(self):
        self.blocked(self.patch_of({"src/a.txt": "alpha\n2\n", ".github/workflows/ci.yml": "name: pwned\n"}))

    def test_a_new_workflow_file_is_blocked(self):
        self.blocked(self.patch_of({".github/workflows/new.yml": "on: push\n"}))

    def test_a_deleted_workflow_file_is_blocked(self):
        self.blocked(self.patch_of({".github/workflows/ci.yml": None}))

    def test_a_rename_out_of_github_is_blocked_with_no_renames(self):
        # A rename carries the old path only in its header; `--no-renames` sees it as a delete.
        (self.repo / "docs").mkdir()
        self.git("mv", ".github/workflows/ci.yml", "docs/ci.yml")
        self.git("commit", "-qm", "move")
        patch = subprocess.run(["/usr/bin/git", "format-patch", "--stdout", "--binary", f"{self.base}..HEAD"],
                               cwd=self.repo, env=clean_git_env(self.home), capture_output=True, check=True).stdout
        self.assertIn(b"rename from .github/workflows/ci.yml", patch)
        self.git("reset", "-q", "--hard", self.base)
        self.blocked(patch)

    def test_a_workflow_change_in_the_first_of_two_commits_is_blocked_though_the_second_undoes_it(self):
        self.blocked(self.patch_of({".github/workflows/x.yml": "x\n"}, {".github/workflows/x.yml": None}))

    def test_other_cases_of_the_directory_are_blocked_too(self):
        self.blocked(mbox(("upper", new_file_diff(".GitHub/workflows/x.yml"))))

    def test_the_builds_own_flag_blocks_without_reading_the_patch(self):
        e = self.blocked(b"not a patch at all", workflow_change=True)
        self.assertEqual(e["built_by"], OK["built_by"])

    def test_a_blocked_slice_does_not_stop_the_others(self):
        self.artifact("bad", self.patch_of({".github/workflows/x.yml": "x\n"}))
        self.artifact("good", self.patch_of({"a.txt": "1\n"}))
        self.run_job("bad", "good")
        land = self.land()
        self.assertEqual([self.entry(land, i)["outcome"] for i in ("bad", "good")], ["blocked", "draft"])
        assemble.check_bundle(land, Path(self.last_env["OUT_DIR"]) / assemble.BUNDLE)


class ClassificationTest(AssembleCase):
    def test_a_failed_build_is_failed_with_its_own_reason(self):
        for reason in ("gate_red", "no_route", "error:3", "secret_in_patch", "empty_patch", "agent_no_output"):
            with self.subTest(reason=reason):
                d = self.slices / "slice-one"
                if d.exists():
                    shutil.rmtree(d)
                self.artifact("one", None, result={"status": "failed", "reason": reason, "gate": "red", "built_by": "", "agent": ""})
                self.run_job("one")
                e = self.entry(self.land(), "one")
                self.assertEqual((e["outcome"], e["reason"], e["branch"]), ("failed", reason, None))

    def test_a_failed_builds_stray_patch_is_never_applied(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}),
                      result={"status": "failed", "reason": "gate_red", "gate": "red", "built_by": "", "agent": ""})
        self.run_job("one")
        self.assertEqual(self.entry(self.land(), "one")["outcome"], "failed")
        self.assertIsNone(self.land()["bundle"])

    def test_a_fix_rounds_patch_is_a_draft_and_never_a_candidate(self):
        """A fix round is built on the draft's head (the run's base): a draft named fix_round when it passes, never on the stack."""
        patch = self.patch_of({"a.txt": "1\n"})
        self.artifact("one", patch, result={"round": 1})
        self.review_of("one", patch, ["a.txt"], round=1)
        self.run_job("one")
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"]), ("draft", "fix_round"))
        self.assertFalse(e["candidate"])
        self.assertFalse(self.land()["has_stack"])

    def test_round_0_that_meets_the_target_is_a_stacked_candidate_carrying_its_score_and_target(self):
        patch = self.patch_of({"a.txt": "x\n"})
        self.artifact("one", patch, result={"score": 0.95, "target": "0.9"})
        self.review_of("one", patch, ["a.txt"])
        self.run_job("one")
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("candidate", "stacked", True))
        self.assertEqual((e["score"], e["target"]), (0.95, "0.9"))

    def test_a_met_fix_round_is_draft_score_met_carrying_the_target_and_the_verdicts_reviewer(self):
        patch = self.patch_of({"a.txt": "x\n"})
        self.artifact("one", patch, result={"round": 1, "score": 0.95, "target": "0.9"})
        self.review_of("one", patch, ["a.txt"], round=1, reviewer="codex:openai/gpt-5")
        self.run_job("one")
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("draft", "score_met", False))
        self.assertEqual((e["score"], e["target"], e["reviewed_by"]), (0.95, "0.9", "codex:openai/gpt-5"))

    def test_a_fix_round_without_a_verdict_is_a_draft_that_says_so(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}), result={"round": 1})
        self.run_job("one")
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"]), ("draft", "verdict_missing"))

    def test_a_fix_round_with_a_red_gate_is_a_draft_red_again(self):
        self.assertEqual(assemble.classify({**OK, "round": 1, "gate": "red"}, "red", "")[0], "draft")
        self.assertEqual(assemble.classify({**OK, "round": 1}, "red", ""), ("draft", "fix_round"), "the base is the red head being fixed")
        self.assertEqual(assemble.classify({**OK, "round": 0}, "red", ""), ("draft", "baseline_red"))

    def test_an_incident_on_a_green_base_is_already_green_and_never_a_candidate(self):
        """I1: an incident (a red main) whose base now passes the repo's check: the change is not needed, so it is a draft
        named already_green, never a candidate. The failed CI job may be another one that is still red (I-B: the reason says so). Only an incident is kept back. A go, an event (its alert need not be a
        red main) and an issue labelled for the incident line are classified as before. A red base is still baseline_red, a
        fix round is still a fix round, and the gate and workflow rules still come first where they did."""
        ok = {**OK, "slice": "x"}
        self.assertEqual(assemble.classify(ok, "green", "", "incident"), ("draft", "already_green"))
        for kind in ("go", "fix", "land", "stop", "event", "none", ""):
            with self.subTest(kind=kind):
                self.assertEqual(assemble.classify(ok, "green", "", kind), ("candidate", "stacked"))
        self.assertEqual(assemble.classify(ok, "red", "", "incident"), ("draft", "baseline_red"))
        self.assertEqual(assemble.classify({**ok, "round": 1}, "green", "", "incident"), ("draft", "fix_round"))
        self.assertEqual(assemble.classify({**ok, "workflow_change": True}, "green", "", "incident"), ("blocked", "workflow_change"))

    def test_an_incident_on_a_green_base_is_a_draft_whose_log_names_the_base_sha(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}))
        code, outputs, stdout, stderr = self.run_job("one", KIND="incident")
        self.assertEqual(code, 0, stderr)
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("draft", "already_green", False))
        self.assertIn(f"slice one: draft (already_green at {self.base[:12]})", stdout)
        self.assertEqual(self.land()["positions"], [])

    def test_classify_table(self):
        ok = {**OK, "slice": "x"}
        failed = {**ok, "status": "failed", "reason": "gate_red", "gate": "red", "built_by": "", "agent": ""}
        table = [
            ((None, "green", ""), ("failed", "no_result")),
            ((failed, "green", ""), ("failed", "gate_red")),
            (({**failed, "reason": "empty_patch"}, "green", ""), ("failed", "empty_patch")),
            (({**ok, "workflow_change": True}, "green", ""), ("blocked", "workflow_change")),
            (({**ok, "workflow_change": True}, "red", ""), ("blocked", "workflow_change")),
            # A fix round is built on the draft's head (its base): a draft at best, never a candidate, and the baseline is not asked.
            (({**ok, "round": 1}, "green", ""), ("draft", "fix_round")),
            (({**ok, "round": 1}, "red", ""), ("draft", "fix_round")),
            (({**ok, "round": 1}, "green", "verdict_needs_fixes"), ("draft", "verdict_needs_fixes")),
            (({**ok, "round": 2, "gate": "red"}, "red", ""), ("draft", "gate_red")),
            ((ok, "red", ""), ("draft", "baseline_red")),
            ((ok, "red", "verdict_missing"), ("draft", "baseline_red")),
            (({**ok, "gate": "not_run"}, "green", ""), ("draft", "gate_not_run")),
            (({**ok, "gate": "red"}, "green", "verdict_missing"), ("draft", "gate_red")),
            ((ok, "green", "verdict_needs_fixes"), ("draft", "verdict_needs_fixes")),
            ((ok, "green", ""), ("candidate", "stacked")),
            ((ok, "none", ""), ("draft", "baseline_red")),
            ((ok, "", ""), ("draft", "baseline_red")),
            (({**ok, "gate": "none"}, "green", ""), ("draft", "gate_none")),
            (({**ok, "gate": "none"}, "none", ""), ("draft", "baseline_red")),  # no check at all
        ]
        for args, want in table:
            with self.subTest(args=args[1:], want=want):
                self.assertEqual(assemble.classify(*args), want)


class StackTest(AssembleCase):
    """The classification table, one test per row, and the stack itself."""

    def row(self, slice_id: str = "one", **over) -> dict:
        self.run_job(slice_id, **over)
        return self.entry(self.land(), slice_id)

    # --- the rows

    def test_row_missing_or_invalid_result_is_failed(self):
        self.assertEqual(self.row()["outcome"], "failed")  # no artifact at all
        self.assertEqual(self.entry(self.land(), "one")["reason"], "no_artifact")
        self.artifact("one", None, files={"result.json": b"{not json"})
        e = self.row()
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("failed", "bad_result", False))

    def test_row_failed_and_empty_results_are_failed_whatever_the_review_says(self):
        for reason in ("no_route", "secret_in_patch", "gate_red", "empty_patch"):
            with self.subTest(reason=reason):
                shutil.rmtree(self.slices / "slice-one", ignore_errors=True)
                shutil.rmtree(self.reviews / "review-one", ignore_errors=True)
                patch = self.patch_of({"a.txt": "x\n"})
                self.artifact("one", patch, result={"status": "failed", "reason": reason, "gate": "red", "built_by": "", "agent": ""})
                self.review_of("one", patch, ["a.txt"])
                e = self.row()
                self.assertEqual((e["outcome"], e["reason"], e["candidate"], e["branch"]), ("failed", reason, False, None))
                self.assertFalse(self.land()["has_stack"])

    def test_row_patched_with_a_workflow_change_is_blocked_even_with_a_ready_review(self):
        for flag in (False, True):
            with self.subTest(flag=flag):
                shutil.rmtree(self.slices / "slice-one", ignore_errors=True)
                shutil.rmtree(self.reviews / "review-one", ignore_errors=True)
                patch = self.patch_of({".github/workflows/ci.yml": "name: other\n"})
                self.artifact("one", patch, result={"workflow_change": flag})
                self.review_of("one", patch, [".github/workflows/ci.yml"])
                e = self.row()
                self.assertEqual((e["outcome"], e["reason"], e["candidate"], e["branch"]), ("blocked", "workflow_change", False, None))
                self.assertIsNone(self.land()["bundle"])

    def test_row_baseline_not_green_is_a_draft_baseline_red_even_with_a_ready_review(self):
        self.ready("one", {"a.txt": "x\n"})
        e = self.row(BASELINE="red")
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("draft", "baseline_red", False))
        self.assertEqual(e["branch"], f"pivotal/issue-{ISSUE}/one")
        self.assertFalse(self.land()["has_stack"])

    def test_row_green_baseline_green_gate_ready_verdict_is_a_candidate_on_the_stack(self):
        patch = self.ready("one", {"a.txt": "x\n"})
        code, outputs, _, _ = self.run_job("one")
        self.assertEqual(code, 0)
        land = self.land()
        e = self.entry(land, "one")
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("candidate", "stacked", True))
        self.assertEqual(e["branch"], f"pivotal/issue-{ISSUE}/stack")
        self.assertEqual((land["positions"], land["has_stack"]), (["one"], True))
        self.assertEqual((outputs["positions"], outputs["gate_matrix"], outputs["has_stack"]), ("one", '["one"]', "true"))
        self.assertEqual(self.tree_of(self.last_env, e["head"])["a.txt"], "x\n")
        self.assertEqual(self.git("rev-parse", f"{e['head']}^", cwd=Path(self.last_env["WORK_DIR"]) / "repo"), self.base)
        self.assertEqual(hashlib.sha256(patch).hexdigest(), review.load(self.reviews / "review-one")["patch_sha256"])

    def test_row_green_baseline_and_a_gate_that_is_not_green_is_a_draft(self):
        # build.check_result only lets an ok result carry green or none, so the table row is reached through classify.
        self.assertEqual(assemble.classify({**OK, "slice": "x", "gate": "not_run"}, "green"), ("draft", "gate_not_run"))
        self.assertEqual(assemble.classify({**OK, "slice": "x", "gate": "red"}, "green"), ("draft", "gate_red"))
        patch = self.patch_of({"a.txt": "x\n"})
        self.artifact("one", patch, result={"gate": "red"})  # and the schema keeps such a result out of the job
        self.review_of("one", patch, ["a.txt"])
        e = self.row()
        self.assertEqual((e["outcome"], e["candidate"]), ("failed", False))

    def test_row_green_baseline_green_gate_and_a_verdict_that_is_not_ready_is_a_draft_naming_it(self):
        patch = self.patch_of({"a.txt": "x\n"})
        self.artifact("one", patch)
        self.review_of("one", patch, ["a.txt"], "NEEDS FIXES")
        e = self.row()
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("draft", "verdict_needs_fixes", False))
        self.assertEqual(e["branch"], f"pivotal/issue-{ISSUE}/one")

    def test_row_a_missing_verdict_artifact_or_a_missing_verdict_is_a_draft(self):
        patch = self.patch_of({"a.txt": "x\n"})
        self.artifact("one", patch)
        self.assertEqual(self.row()["reason"], "verdict_missing")  # no review-one directory
        self.review_of("one", patch, ["a.txt"], "missing", reason="no_route", reviewed_by="none", files=[])
        d = self.reviews / "review-one"
        (d / "review.md").write_text(review.MISSING_NOTE.format(reason="no_route"))
        e = self.row()
        self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("draft", "verdict_missing", False))

    def test_a_ready_verdict_for_another_patch_files_builder_base_slice_or_round_is_no_candidate(self):
        patch = self.patch_of({"a.txt": "x\n", "b.txt": "y\n"})
        cases = {
            "other patch": ({"sha": "f" * 64}, "verdict_wrong_sha"),
            "other files": ({"files": ["a.txt"]}, "verdict_files"),
            "other builder": ({"built_by": "claude:anthropic/opus"}, "verdict_wrong_builder"),
            "other base": ({"base": "b" * 40}, "verdict_stale"),
            "other slice": ({"slice": "two"}, "verdict_stale"),
            "other round": ({"round": 1}, "verdict_stale"),
        }
        for name, (over, reason) in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.slices / "slice-one", ignore_errors=True)
                shutil.rmtree(self.reviews / "review-one", ignore_errors=True)
                self.artifact("one", patch)
                over = dict(over)
                names = over.pop("files", ["a.txt", "b.txt"])
                self.review_of("one", patch, names, **over)
                e = self.row()
                self.assertEqual((e["outcome"], e["reason"], e["candidate"]), ("draft", reason, False))

    def test_the_files_are_those_of_the_apply_a_rename_counts_by_both_names(self):
        (self.repo / "old.txt").write_text("one\ntwo\nthree\nfour\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "more base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("mv", "old.txt", "new.txt")
        self.git("commit", "-qm", "rename")
        patch = subprocess.run(["/usr/bin/git", "format-patch", "--stdout", "--binary", "-M", "-1"], cwd=self.repo,
                               env=clean_git_env(self.home), capture_output=True, check=True).stdout
        self.git("reset", "-q", "--hard", "HEAD^")
        self.assertIn(b"rename from old.txt", patch)
        self.artifact("one", patch)
        self.review_of("one", patch, ["new.txt"])  # a review that missed the old name
        self.assertEqual(self.row()["reason"], "verdict_files")
        shutil.rmtree(self.reviews / "review-one")
        self.review_of("one", patch, ["old.txt", "new.txt"])
        self.assertEqual(self.row()["outcome"], "candidate")

    # --- the stack

    def test_candidates_are_stacked_in_matrix_order_on_one_branch(self):
        self.ready("zeta", {"z.txt": "z\n"})
        self.ready("alpha", {"a.txt": "a\nb\n"})
        self.ready("mid", {"m.txt": "m\n"})
        code, outputs, _, _ = self.run_job("zeta", "alpha", "mid")
        self.assertEqual(code, 0)
        land = self.land()
        self.assertEqual(land["positions"], ["zeta", "alpha", "mid"])
        self.assertEqual(outputs["positions"], "zeta,alpha,mid")
        self.assertEqual(outputs["gate_matrix"], '["zeta","alpha","mid"]')
        self.assertEqual(json.loads(outputs["gate_matrix"]), land["positions"])
        z, a, m = (self.entry(land, i) for i in ("zeta", "alpha", "mid"))
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        chain = self.git("rev-list", "--reverse", f"{self.base}..refs/heads/pivotal/issue-{ISSUE}/stack", cwd=work).split()
        self.assertEqual(chain, [*z["commits"], *a["commits"], *m["commits"]])
        self.assertEqual(len(chain), 3)
        self.assertEqual(self.git("rev-list", "--parents", "-1", z["commits"][0], cwd=work).split()[1:], [self.base])
        self.assertEqual(self.git("rev-list", "--parents", "-1", a["commits"][0], cwd=work).split()[1:], [z["head"]])
        tree = self.tree_of(self.last_env, m["head"])
        self.assertEqual((tree["z.txt"], tree["a.txt"], tree["m.txt"]), ("z\n", "a\nb\n", "m\n"))
        self.assertEqual(self.tree_of(self.last_env, z["head"]).get("a.txt"), None, "each head is the stack at its position")
        out = Path(self.last_env["OUT_DIR"])
        assemble.check_bundle(land, out / assemble.BUNDLE)
        self.assertEqual(
            self.git("ls-remote", str(out / assemble.BUNDLE), f"refs/heads/pivotal/issue-{ISSUE}/stack").split()[0], m["head"])
        times = [int(self.git("show", "-s", "--format=%ct", c, cwd=work)) for c in chain]
        base_time = int(self.git("show", "-s", "--format=%ct", self.base, cwd=work))
        self.assertEqual(times, [base_time + n for n in (1, 2, 3)])

    def test_two_ok_slices_and_one_red_give_one_stack_branch_and_one_draft(self):
        self.ready("one", {"a.txt": "1\n"})
        self.ready("two", {"b.txt": "2\n"})
        patch = self.patch_of({"c.txt": "3\n"})
        self.artifact("red", patch)
        self.review_of("red", patch, ["c.txt"], "NEEDS FIXES")
        self.run_job("one", "two", "red")
        land = self.land()
        self.assertEqual([(s["id"], s["outcome"]) for s in land["slices"]], [("one", "candidate"), ("two", "candidate"), ("red", "draft")])
        out = Path(self.last_env["OUT_DIR"])
        heads = assemble.bundle_header((out / assemble.BUNDLE).read_bytes())[1]
        self.assertEqual(sorted(heads), [f"refs/heads/pivotal/issue-{ISSUE}/red", f"refs/heads/pivotal/issue-{ISSUE}/stack"])
        stack_tree = self.tree_of(self.last_env, self.entry(land, "two")["head"])
        self.assertEqual(sorted(stack_tree), sorted([*BASELINE_FILES, "a.txt", "b.txt"]))
        self.assertNotIn("c.txt", stack_tree, "the draft is not on the stack")

    def test_a_conflict_on_the_stack_makes_that_slice_a_draft_and_the_stack_goes_on(self):
        self.ready("one", {"src/a.txt": "alpha one\n"})
        self.ready("two", {"src/a.txt": "alpha two\n"})  # applies on the base, not after one
        self.ready("three", {"c.txt": "3\n"})
        self.run_job("one", "two", "three")
        land = self.land()
        one, two, three = (self.entry(land, i) for i in ("one", "two", "three"))
        self.assertEqual((two["outcome"], two["reason"], two["candidate"]), ("draft", "conflict", False))
        self.assertEqual(two["branch"], f"pivotal/issue-{ISSUE}/two", "the conflict draft is cut from the base")
        self.assertEqual(self.tree_of(self.last_env, two["head"])["src/a.txt"], "alpha two\n")
        self.assertEqual(land["positions"], ["one", "three"])
        tree = self.tree_of(self.last_env, three["head"])
        self.assertEqual((tree["src/a.txt"], tree["c.txt"]), ("alpha one\n", "3\n"))
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        self.assertEqual(self.git("rev-parse", f"{three['commits'][0]}^", cwd=work), one["head"], "the leftover of the failed am is gone")
        assemble.check_bundle(land, Path(self.last_env["OUT_DIR"]) / assemble.BUNDLE)

    def test_a_slice_of_several_commits_is_a_draft_never_a_candidate(self):
        # F-1: only the slice head is gated, so a slice that is several commits must not reach the stack.
        self.ready("one", {"src/a.txt": "alpha one\n"})
        self.ready("two", {"d.txt": "d\n"}, {"e.txt": "e\n"})
        self.ready("three", {"c.txt": "3\n"})
        code, outputs, _, _ = self.run_job("one", "two", "three")
        self.assertEqual(code, 0)
        land = self.land()
        two = self.entry(land, "two")
        self.assertEqual((two["outcome"], two["reason"], two["candidate"]), ("draft", "multi_commit", False))
        self.assertEqual(two["branch"], f"pivotal/issue-{ISSUE}/two", "it still gets its draft, cut from the base")
        self.assertEqual(len(two["commits"]), 2)
        self.assertEqual(land["positions"], ["one", "three"])
        self.assertEqual(outputs["gate_matrix"], '["one","three"]')
        for ident in ("one", "three"):
            self.assertEqual(len(self.entry(land, ident)["commits"]), 1)
        self.assertNotIn("d.txt", self.tree_of(self.last_env, self.entry(land, "three")["head"]))

    def test_one_commit_says_what_a_candidate_is(self):
        self.assertTrue(assemble.one_commit(["a" * 40]))
        for commits in ([], ["a" * 40, "b" * 40]):
            self.assertFalse(assemble.one_commit(commits))

    def test_a_stacked_commit_that_changes_something_other_than_the_verified_patch_is_a_draft(self):
        # F-4: the verdict is bound to the patch on the base; the commit that lands must make the same change.
        self.ready("one", {"a.txt": "1\n"})
        self.ready("two", {"b.txt": "2\n"})
        real = assemble.apply_patch

        def sneaky(git, patch, patches, base_time, onto=None, before=0):
            made, files = real(git, patch, patches, base_time, onto, before)
            if onto is not None and before == 1:  # the second slice, on the stack: one more line than the patch has
                blob = git.out("hash-object", "-w", "--stdin", input=b"evil\n")
                git.run("update-index", "--add", "--cacheinfo", f"100644,{blob},evil.txt")
                tree = git.out("write-tree")
                made = [git.out("commit-tree", tree, "-p", made[-1], "-m", "x", when=base_time + 9)]
            return made, files

        with mock.patch.object(assemble, "apply_patch", sneaky):
            self.run_job("one", "two")
        land = self.land()
        two = self.entry(land, "two")
        self.assertEqual((two["outcome"], two["reason"], two["candidate"]), ("draft", "stack_differs", False))
        self.assertEqual(land["positions"], ["one"])
        self.assertNotIn("evil.txt", self.tree_of(self.last_env, self.entry(land, "one")["head"]))

    def test_a_legitimate_shift_of_hunks_by_an_earlier_slice_is_still_a_candidate(self):
        self.ready("one", {"src/a.txt": "zero\nalpha\n"})
        self.ready("two", {"src/a.txt": "alpha\nomega\n"})  # the same file, other lines: its offsets move on the stack
        self.run_job("one", "two")
        land = self.land()
        self.assertEqual(land["positions"], ["one", "two"])
        self.assertEqual(self.tree_of(self.last_env, self.entry(land, "two")["head"])["src/a.txt"], "zero\nalpha\nomega\n")

    def test_a_patch_that_does_not_apply_even_on_the_base_is_a_conflict_with_no_branch(self):
        patch = self.patch_of({"src/a.txt": "alpha changed\n"})
        self.artifact("one", patch.replace(b"-alpha", b"-gamma"))
        self.review_of("one", patch.replace(b"-alpha", b"-gamma"), ["src/a.txt"])
        e = self.row()
        self.assertEqual((e["outcome"], e["reason"], e["branch"]), ("conflict", "does_not_apply", None))

    def test_no_candidate_means_no_stack_branch_and_the_drafts_still_bundle(self):
        patch = self.patch_of({"a.txt": "x\n"})
        self.artifact("one", patch)
        code, outputs, _, _ = self.run_job("one")
        self.assertEqual((code, outputs["positions"], outputs["has_stack"]), (0, "", "false"))
        heads = assemble.bundle_header((Path(self.last_env["OUT_DIR"]) / assemble.BUNDLE).read_bytes())[1]
        self.assertEqual(list(heads), [f"refs/heads/pivotal/issue-{ISSUE}/one"])

    def test_a_blocked_slice_between_candidates_is_not_stacked(self):
        self.ready("one", {"a.txt": "1\n"})
        patch = self.patch_of({".github/workflows/x.yml": "x\n"})
        self.artifact("two", patch)
        self.review_of("two", patch, [".github/workflows/x.yml"])
        self.ready("three", {"c.txt": "3\n"})
        self.run_job("one", "two", "three")
        land = self.land()
        self.assertEqual(land["positions"], ["one", "three"])
        self.assertEqual(self.entry(land, "two")["outcome"], "blocked")
        self.assertNotIn(".github/workflows/x.yml", self.tree_of(self.last_env, self.entry(land, "three")["head"]))

    def test_the_stack_is_deterministic(self):
        self.ready("one", {"a.txt": "1\n"})
        self.ready("two", {"b.txt": "2\n"}, {"b.txt": "2\n2\n"})
        seen = []
        for _ in range(2):
            self.run_job("one", "two")
            out = Path(self.last_env["OUT_DIR"])
            seen.append(((out / assemble.LAND).read_bytes(), (out / assemble.BUNDLE).read_bytes()))
        self.assertEqual(seen[0], seen[1])

    def test_the_stack_does_not_depend_on_the_authors_or_dates_of_the_patches(self):
        heads = []
        for who, date in (("Ann", "2020-01-01T00:00:00 +0000"), ("Bob", "2031-12-31T23:59:59 +0000")):
            shutil.rmtree(self.slices)
            shutil.rmtree(self.reviews)
            self.slices.mkdir()
            self.reviews.mkdir()
            patch = self.patch_of({"a.txt": "1\n"}, author=who, date=date)
            self.artifact("one", patch)
            self.review_of("one", patch, ["a.txt"])
            self.run_job("one")
            heads.append(self.entry(self.land(), "one")["head"])
        self.assertEqual(heads[0], heads[1])

    # --- the review artifacts are hostile input

    def hostile(self, plant) -> dict:
        patch = self.patch_of({"a.txt": "x\n"})
        self.artifact("one", patch)
        self.review_of("one", patch, ["a.txt"])
        plant(self.reviews / "review-one")
        return self.row()

    def test_an_odd_review_artifact_is_never_a_candidate(self):
        def swap_with_symlink(d, name="verdict"):
            (d / name).rename(d.parent / "elsewhere")
            os.symlink(d.parent / "elsewhere", d / name)

        def fifo(d):
            (d / "review.md").unlink()
            os.mkfifo(d / "review.md")

        def whole_dir_link(d):
            d.rename(d.parent / "real")
            os.symlink(d.parent / "real", d)

        plants = {
            "symlinked verdict": swap_with_symlink,
            "fifo review.md": fifo,
            "symlinked directory": whole_dir_link,
            "extra file": lambda d: (d / "extra").write_text("x"),
            "missing reviewed_by": lambda d: (d / "reviewed_by").unlink(),
            "not json": lambda d: (d / "verdict").write_text("{"),
            "deep json": lambda d: (d / "verdict").write_text("[" * 100000),
            "duplicate keys": lambda d: (d / "verdict").write_text('{"verdict": "READY", "verdict": "READY"}'),
            "oversized verdict": lambda d: (d / "verdict").write_text(" " * (review.MAX_VERDICT_BYTES + 1)),
            "review.md edited": lambda d: (d / "review.md").write_text("Verdict: READY\n"),
            "reviewed_by edited": lambda d: (d / "reviewed_by").write_text("claude:anthropic/opus\n"),
            "same vendor": lambda d: (d / "verdict").write_text(
                (d / "verdict").read_text().replace("codex:openai/gpt-5", "claude:anthropic/opus")),
            "a directory for review.md": lambda d: (d / "review.md").unlink() or (d / "review.md").mkdir(),
        }
        for name, plant in plants.items():
            with self.subTest(name):
                shutil.rmtree(self.slices / "slice-one", ignore_errors=True)
                shutil.rmtree(self.reviews, ignore_errors=True)
                self.reviews.mkdir()
                e = self.hostile(plant)
                self.assertEqual((e["outcome"], e["candidate"]), ("draft", False), name)
                self.assertIn(e["reason"], ("verdict_invalid", "verdict_missing"), name)

    def test_the_review_artifact_of_another_slice_is_not_read_for_this_one(self):
        patch = self.patch_of({"a.txt": "x\n"})
        self.artifact("one", patch)
        self.review_of("two", patch, ["a.txt"])
        (self.reviews / "review-two").rename(self.reviews / "review-one")
        self.assertEqual(self.row()["reason"], "verdict_stale")

    def test_a_review_dir_that_is_missing_refuses_before_any_write(self):
        env = self.env("one", REVIEWS_DIR=str(self.root / "nope"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(assemble.main(["run"], env), 1)
        self.assertFalse(Path(env["OUT_DIR"]).exists() or Path(env["WORK_DIR"]).exists())
        env = self.env("one", REVIEWS_DIR=None)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(assemble.main(["run"], env), 1)

    def test_the_stack_applies_with_hooks_and_the_repos_configuration_never_running(self):
        (self.repo / ".git" / "hooks" / "post-applypatch").write_text("#!/bin/sh\ntouch RAN_HOOK\n")
        (self.repo / ".git" / "hooks" / "post-applypatch").chmod(0o755)
        self.ready("one", {"a.txt": "1\n"})
        self.ready("two", {"b.txt": "2\n"})
        self.run_job("one", "two")
        self.assertEqual(self.land()["positions"], ["one", "two"])
        self.assertFalse(list(self.root.rglob("RAN_*")))


class FailureTest(AssembleCase):
    """A missing, malformed, oversized or odd artifact is a failed slice with a reason, never a dropped one (F5)."""

    def failed(self, reason: str, slice_id: str = "one") -> dict:
        code, outputs, stdout, stderr = self.run_job(slice_id)
        self.assertEqual(code, 0, stderr)
        e = self.entry(self.land(), slice_id)
        self.assertEqual((e["outcome"], e["reason"], e["branch"], e["head"], e["commits"]), ("failed", reason, None, None, []))
        self.assertIn(f"slice {slice_id}: failed ({reason})", stdout)
        return e

    def test_a_missing_artifact_is_failed(self):
        self.failed("no_artifact")

    def test_a_missing_result_is_failed(self):
        self.artifact("one", b"")
        (self.slices / "slice-one" / "result.json").unlink()
        self.failed("no_result")

    def test_malformed_results_are_failed(self):
        bad = {
            "not json": b"{",
            "a list": b"[]",
            "a duplicate key": json.dumps(OK | {"slice": "one"}).replace('"status": "ok"', '"status": "ok", "status": "failed"').encode(),
            "an unknown key": json.dumps({**OK, "slice": "one", "extra": 1}).encode(),
            "a missing key": json.dumps({k: v for k, v in OK.items() if k != "gate"} | {"slice": "one"}).encode(),
            "a wrong type": json.dumps({**OK, "slice": "one", "workflow_change": "no"}).encode(),
            "gate red on ok": json.dumps({**OK, "slice": "one", "gate": "red"}).encode(),
            "not utf-8": b"\xff\xfe{}",
            "nul bytes": b'{"status": "ok\x00"}',
            "deeply nested": b"[" * (assemble.MAX_RESULT_BYTES - 1),
            "another slice's": json.dumps({**OK, "slice": "two"}).encode(),
        }
        for name, data in bad.items():
            with self.subTest(name):
                shutil.rmtree(self.slices / "slice-one", ignore_errors=True)
                self.artifact("one", self.patch_of({"a.txt": "1\n"}), files={"result.json": data})
                self.failed("bad_result")

    def test_an_oversized_result_is_failed_without_being_read(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}), files={"result.json": b" " * (assemble.MAX_RESULT_BYTES + 1)})
        self.failed("bad_artifact")

    def test_an_oversized_patch_is_failed(self):
        self.artifact("one", b"x" * 100)
        with mock.patch.object(assemble, "MAX_PATCH_BYTES", 50):
            self.failed("bad_artifact")

    def test_an_ok_result_without_a_patch_is_failed(self):
        self.artifact("one", None)
        self.failed("no_patch")

    def test_links_fifos_directories_and_extra_files_are_failed_and_nothing_is_followed(self):
        secret = self.root / "secret"
        secret.write_text(json.dumps({**OK, "slice": "one"}))
        patch = self.patch_of({"a.txt": "1\n"})
        cases = {}

        def symlinked(name):
            def make(d):
                (d / name).unlink()
                (d / name).symlink_to(secret)
            return make

        def fifo(name):
            def make(d):
                (d / name).unlink()
                os.mkfifo(d / name)
            return make

        def directory(name):
            def make(d):
                (d / name).unlink()
                (d / name).mkdir()
            return make

        cases["symlinked result"] = symlinked("result.json")
        cases["symlinked patch"] = symlinked("slice.patch")
        cases["symlinked summary"] = symlinked("summary.md")
        cases["fifo result"] = fifo("result.json")
        cases["fifo patch"] = fifo("slice.patch")
        cases["fifo summary"] = fifo("summary.md")
        cases["directory patch"] = directory("slice.patch")
        cases["extra file"] = lambda d: (d / "extra.txt").write_text("x")
        cases["extra directory"] = lambda d: (d / "sub").mkdir()
        for name, make in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.slices / "slice-one", ignore_errors=True)
                d = self.artifact("one", patch)
                make(d)
                self.failed("bad_artifact")

    def test_a_slice_directory_that_is_a_symlink_or_a_file_is_failed(self):
        real = self.artifact("real", self.patch_of({"a.txt": "1\n"}))
        (self.slices / "slice-one").symlink_to(real)
        self.failed("bad_artifact")
        (self.slices / "slice-one").unlink()
        (self.slices / "slice-one").write_text("x")
        self.failed("bad_artifact")

    def test_a_slices_dir_that_is_missing_refuses_before_any_write(self):
        code, outputs, _, stderr = self.run_job("one", SLICES_DIR=str(self.root / "nope"))
        self.assertEqual(code, 1)
        self.assertIn("SLICES_DIR", stderr)
        self.assertFalse(Path(self.last_env["OUT_DIR"]).exists())
        self.assertEqual(outputs, {})

    def test_artifacts_the_plan_did_not_ask_for_are_named_and_ignored(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}))
        self.artifact("stray", self.patch_of({"b.txt": "1\n"}))
        (self.slices / "weird\nname").mkdir()
        code, _, stdout, _ = self.run_job("one")
        self.assertEqual(code, 0)
        self.assertIn("ignored: slice-stray is not a slice of the plan", stdout)
        self.assertIn("ignored: an artifact with an odd name", stdout)
        self.assertNotIn("weird", stdout)
        self.assertEqual([s["id"] for s in self.land()["slices"]], ["one"])


class PatchTest(AssembleCase):
    def one(self, patch: bytes) -> dict:
        self.artifact("one", patch)
        code, _, _, stderr = self.run_job("one")
        self.assertEqual(code, 0, stderr)
        e = self.entry(self.land(), "one")
        self.assertEqual((e["branch"], e["head"], e["commits"]), (None, None, []))
        self.assertIsNone(self.land()["bundle"])
        return e

    def test_a_patch_that_does_not_apply_is_a_conflict_with_no_branch_and_the_others_go_on(self):
        stale = self.patch_of({"src/a.txt": "alpha\nfrom the agent\n"})
        self.artifact("stale", stale)
        self.artifact("good", self.patch_of({"b.txt": "1\n"}))
        # The base moved under the slice: the context the patch was cut against is gone.
        (self.repo / "src/a.txt").write_text("rewritten completely\nnothing alike\n")
        self.git("commit", "-qam", "moved")
        self.base = self.git("rev-parse", "HEAD")
        self.run_job("stale", "good")
        land = self.land()
        self.assertEqual(self.entry(land, "stale")["outcome"], "conflict")
        self.assertEqual(self.entry(land, "stale")["reason"], "does_not_apply")
        self.assertEqual(self.entry(land, "stale")["branch"], None)
        self.assertEqual(self.entry(land, "good")["outcome"], "draft", "the base moved, but the second patch still applies")
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        self.assertNotIn("rebase-apply", os.listdir(work / ".git"))
        refs = self.git("for-each-ref", "--format=%(refname)", cwd=work).splitlines()
        self.assertEqual(refs, [f"refs/heads/pivotal/issue-{ISSUE}/good"])

    def test_a_patch_applies_on_a_base_that_moved_elsewhere_in_the_file(self):
        # The patch changes line 2 of a file; the base changed line 20 since.
        (self.repo / "long.txt").write_text("".join(f"line {i}\n" for i in range(1, 30)))
        self.git("add", "long.txt")
        self.git("commit", "-qm", "long")
        self.base = self.git("rev-parse", "HEAD")
        self.artifact("one", self.patch_of({"long.txt": "".join(f"line {i}\n" for i in range(1, 30)).replace("line 2\n", "two\n")}))
        text = "".join(f"line {i}\n" for i in range(1, 30)).replace("line 20\n", "twenty\n")
        (self.repo / "long.txt").write_text(text)
        self.git("commit", "-qam", "base moved")
        base2 = self.git("rev-parse", "HEAD")
        self.run_job("one", BASE_SHA=base2)
        e = self.entry(self.land(), "one")
        self.assertEqual(e["outcome"], "draft")
        merged = self.tree_of(self.last_env, e["head"])["long.txt"]
        self.assertIn("two\n", merged)
        self.assertIn("twenty\n", merged)

    def test_garbage_is_not_a_draft(self):
        for name, data in {"empty": b"\n", "text": b"hello world\n", "binary": os.urandom(64)}.items():
            with self.subTest(name):
                shutil.rmtree(self.slices / "slice-one", ignore_errors=True)
                e = self.one(data)
                self.assertIn(e["outcome"], ("conflict", "failed"))

    def test_too_many_commits_are_failed(self):
        patch = mbox(*[(f"c{i}", new_file_diff(f"f{i}.txt")) for i in range(assemble.MAX_COMMITS + 1)])
        e = self.one(patch)
        self.assertEqual((e["outcome"], e["reason"]), ("failed", "too_many_commits"))

    def test_hostile_paths_are_failed_and_nothing_is_written_outside(self):
        victim = self.root / "victim"
        victim.mkdir()
        cases = {
            "dotdot": new_file_diff("../victim/pwned"),
            "nested dotdot": new_file_diff("src/../../victim/pwned"),
            "absolute": new_file_diff(f"{victim}/pwned"),
            "git hook": new_file_diff(".git/hooks/post-checkout", "#!/bin/sh\ntouch /tmp/pwned\n", "100755"),
            "git config": new_file_diff(".git/config"),
            "git upper": new_file_diff(".GIT/hooks/x"),
            "nested git": new_file_diff("sub/.git/config"),
            "dot component": new_file_diff("src/./a.txt"),
        }
        for name, diff in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.slices / "slice-one", ignore_errors=True)
                e = self.one(mbox(("hostile", diff)))
                self.assertEqual((e["outcome"], e["reason"]), ("failed", "unsafe_path"))
                self.assertFalse((victim / "pwned").exists())
                self.assertFalse(Path("/tmp/pwned").exists())

    def test_unsafe_path_is_named_when_git_parses_the_names(self):
        e = self.one(mbox(("hostile", new_file_diff(".git/hooks/post-checkout", "x\n", "100755"))))
        self.assertEqual((e["outcome"], e["reason"]), ("failed", "unsafe_path"))

    def test_a_patch_cannot_write_through_a_symlink_it_made_earlier(self):
        victim = self.root / "victim"
        victim.mkdir()
        link = f"diff --git a/link b/link\nnew file mode 120000\nindex 0000000..2222222\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+{victim}\n\\ No newline at end of file\n"
        e = self.one(mbox(("link", link), ("through", new_file_diff("link/pwned"))))
        self.assertIn(e["outcome"], ("failed", "conflict"))
        self.assertEqual(list(victim.iterdir()), [])

    def test_a_symlink_into_the_tree_is_just_data(self):
        link = "diff --git a/ln b/ln\nnew file mode 120000\nindex 0000000..2222222\n--- /dev/null\n+++ b/ln\n@@ -0,0 +1 @@\n+/etc/passwd\n\\ No newline at end of file\n"
        self.artifact("one", mbox(("link", link)))
        self.run_job("one")
        e = self.entry(self.land(), "one")
        self.assertEqual(e["outcome"], "draft")
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        self.assertEqual(self.git("ls-tree", e["head"], "ln", cwd=work).split()[0], "120000")

    def test_a_patch_that_adds_attributes_or_executables_runs_nothing(self):
        marker = self.root / "marker"
        patch = self.patch_of({".gitattributes": f"* filter=evil diff=evil merge=evil\n", "run.sh": f"#!/bin/sh\ntouch {marker}\n",
                               "Makefile": f"all:\n\ttouch {marker}\n"})
        self.artifact("one", patch)
        self.run_job("one")
        self.assertEqual(self.entry(self.land(), "one")["outcome"], "draft")
        self.assertFalse(marker.exists())
        self.assertFalse((Path(self.last_env["WORK_DIR"]) / "repo" / "RAN_REPO_CODE").exists())

    def test_a_leftover_rebase_directory_of_a_failed_slice_does_not_poison_the_next(self):
        self.artifact("bad", mbox(("x", "diff --git a/zzz b/zzz\n--- a/zzz\n+++ b/zzz\n@@ -1 +1 @@\n-a\n+b\n")))
        self.artifact("good", self.patch_of({"b.txt": "1\n"}))
        self.run_job("bad", "good")
        land = self.land()
        self.assertEqual((self.entry(land, "bad")["outcome"], self.entry(land, "good")["outcome"]), ("conflict", "draft"))


class RefusalTest(AssembleCase):
    def refused(self, text: str, **over) -> None:
        code, outputs, stdout, stderr = self.run_job("one", **over)
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn(text, stderr)
        self.assertFalse(Path(self.last_env["OUT_DIR"]).exists(), "a refusal makes no output")
        self.assertFalse(Path(self.last_env["WORK_DIR"]).exists(), "a refusal makes no scratch repository")
        self.assertEqual(outputs, {}, "a refusal writes no output")

    def test_bad_inputs_refuse_before_the_first_write(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}))
        cases = [
            ("ISSUE", {"ISSUE": "0"}), ("ISSUE", {"ISSUE": "7; rm"}), ("ISSUE", {"ISSUE": None}),
            ("BASE_SHA", {"BASE_SHA": "abc"}), ("BASE_SHA", {"BASE_SHA": "0" * 40}), ("BASE_SHA", {"BASE_SHA": self.base.upper()}),
            ("MATRIX", {"MATRIX": ""}), ("MATRIX", {"MATRIX": "[]"}), ("MATRIX", {"MATRIX": '[{"id":"stack","title":"t","goal":"g"}]'}),
            ("MATRIX", {"MATRIX": '[{"id":"../x","title":"t","goal":"g"}]'}),
            ("BASELINE", {"BASELINE": "yes"}), ("BASELINE", {"BASELINE": ""}), ("BASELINE", {"BASELINE": None}),
            ("KIND", {"KIND": None}), ("KIND", {"KIND": ""}), ("KIND", {"KIND": "Incident"}), ("KIND", {"KIND": "red-main"}),
            ("REPO_DIR", {"REPO_DIR": "relative"}), ("REPO_DIR", {"REPO_DIR": str(self.root / "nope")}),
            ("OUT_DIR", {"OUT_DIR": str(self.slices)}),
        ]
        for name, over in cases:
            with self.subTest(over):
                code, outputs, _, stderr = self.run_job("one", **over)
                self.assertEqual(code, 1)
                self.assertIn(name, stderr)
                self.assertEqual(outputs, {})
                if "OUT_DIR" not in over:
                    self.assertFalse(Path(self.last_env["OUT_DIR"]).exists())

    def test_matrix_is_validated_by_the_plans_own_rules(self):
        code, *_ , stderr = self.run_job("one", MATRIX=json.dumps([{"id": f"s{i}", "title": "t", "goal": "g"} for i in range(7)]))
        self.assertEqual(code, 1)

    def test_an_existing_work_dir_is_refused(self):
        work = self.root / "taken"
        work.mkdir()
        code, outputs, _, stderr = self.run_job("one", WORK_DIR=str(work))
        self.assertEqual((code, outputs), (1, {}))
        self.assertIn("WORK_DIR already exists", stderr)

    def test_a_base_the_repo_does_not_hold_refuses(self):
        code, outputs, _, stderr = self.run_job("one", BASE_SHA="1" * 40)
        self.assertEqual((code, outputs), (1, {}))
        self.assertIn("base commit", stderr)
        self.assertFalse(Path(self.last_env["OUT_DIR"]).exists())

    def test_usage(self):
        self.assertEqual(assemble.main([], {}), 2)
        self.assertEqual(assemble.main(["bundle"], {}), 2)


class LandSchemaTest(AssembleCase):
    def valid(self) -> dict:
        self.artifact("one", self.patch_of({"a.txt": "1\n"}))
        self.artifact("two", self.patch_of({".github/workflows/x.yml": "x\n"}))
        self.artifact("three", None, result={"status": "failed", "reason": "gate_red", "gate": "red", "built_by": "", "agent": ""})
        self.run_job("one", "two", "three")
        return self.land()

    def test_a_real_land_json_is_valid_and_round_trips(self):
        land = self.valid()
        self.assertEqual(assemble.check_land(copy.deepcopy(land)), land)
        text = (Path(self.last_env["OUT_DIR"]) / assemble.LAND).read_bytes()
        self.assertEqual(text, assemble.land_text(land))
        self.assertTrue(text.isascii() and text.endswith(b"\n"))
        self.assertEqual(list(json.loads(text)), ["version", "issue", "base_sha", "baseline", "bundle", "has_stack", "positions", "slices"])

    def test_every_mutation_is_refused(self):
        land = self.valid()
        one = land["slices"][0]
        two = land["slices"][1]
        sha_other = "a" * 40

        def mutate(fn):
            data = copy.deepcopy(land)
            fn(data)
            return data

        cases = {
            "not an object": [],
            "extra key": mutate(lambda d: d.update(extra=1)),
            "missing key": mutate(lambda d: d.pop("baseline")),
            "version 1": mutate(lambda d: d.update(version=1)),
            "version 3": mutate(lambda d: d.update(version=3)),
            "version true": mutate(lambda d: d.update(version=True)),
            "issue 0": mutate(lambda d: d.update(issue=0)),
            "issue bool": mutate(lambda d: d.update(issue=True)),
            "issue string": mutate(lambda d: d.update(issue="7")),
            "short base": mutate(lambda d: d.update(base_sha="abc")),
            "upper base": mutate(lambda d: d.update(base_sha=land["base_sha"].upper())),
            "zero base": mutate(lambda d: d.update(base_sha="0" * 40)),
            "bad baseline": mutate(lambda d: d.update(baseline="maybe")),
            "no slices": mutate(lambda d: d.update(slices=[])),
            "seven slices": mutate(lambda d: d.update(slices=[dict(d["slices"][0], id=f"s{i}", branch=f"pivotal/issue-7/s{i}") for i in range(7)])),
            "slice extra key": mutate(lambda d: d["slices"][0].update(extra=1)),
            "slice missing key": mutate(lambda d: d["slices"][0].pop("gate")),
            "reserved id": mutate(lambda d: d["slices"][1].update(id="stack")),
            "traversal id": mutate(lambda d: d["slices"][1].update(id="../x")),
            "repeated id": mutate(lambda d: d["slices"][1].update(id="one")),
            "bad outcome": mutate(lambda d: d["slices"][0].update(outcome="landed")),
            "reason with space": mutate(lambda d: d["slices"][0].update(reason="a b")),
            "reason empty": mutate(lambda d: d["slices"][0].update(reason="")),
            "reason newline": mutate(lambda d: d["slices"][0].update(reason="a\nb")),
            "candidate int": mutate(lambda d: d["slices"][0].update(candidate=1)),
            "candidate blocked": mutate(lambda d: d["slices"][1].update(candidate=True)),
            "branch of another slice": mutate(lambda d: d["slices"][0].update(branch="pivotal/issue-7/two")),
            "branch of another issue": mutate(lambda d: d["slices"][0].update(branch="pivotal/issue-8/one")),
            "branch traversal": mutate(lambda d: d["slices"][0].update(branch="pivotal/issue-7/../one")),
            "branch main": mutate(lambda d: d["slices"][0].update(branch="main")),
            "draft without branch": mutate(lambda d: d["slices"][0].update(branch=None)),
            "draft without head": mutate(lambda d: d["slices"][0].update(head=None)),
            "head not last commit": mutate(lambda d: d["slices"][0].update(head=sha_other)),
            "draft without commits": mutate(lambda d: d["slices"][0].update(commits=[])),
            "commit uppercase": mutate(lambda d: d["slices"][0].update(commits=[one["head"].upper()], head=one["head"].upper())),
            "commit repeated": mutate(lambda d: d["slices"][0].update(commits=[one["head"], one["head"]])),
            "commits string": mutate(lambda d: d["slices"][0].update(commits=one["head"])),
            "blocked with branch": mutate(lambda d: d["slices"][1].update(branch="pivotal/issue-7/two")),
            "blocked with head": mutate(lambda d: d["slices"][1].update(head=sha_other)),
            "failed with commits": mutate(lambda d: d["slices"][2].update(commits=[sha_other])),
            "built_by shell": mutate(lambda d: d["slices"][0].update(built_by="a;b")),
            "target not a number": mutate(lambda d: d["slices"][0].update(target="nine")),
            "target above one": mutate(lambda d: d["slices"][0].update(target="1.5")),
            "target int": mutate(lambda d: d["slices"][0].update(target=0.9)),
            "target zero": mutate(lambda d: d["slices"][0].update(target="0.0")),
            "target zero, six decimals": mutate(lambda d: d["slices"][0].update(target=".000000")),
            "reviewer on a plain draft": mutate(lambda d: d["slices"][0].update(reviewed_by="claude:anthropic/sonnet")),
            "reviewer shell": mutate(lambda d: d["slices"][0].update(reviewed_by="a;b", reason="score_met")),
            "reviewer int": mutate(lambda d: d["slices"][0].update(reviewed_by=1)),
            "gate unknown": mutate(lambda d: d["slices"][0].update(gate="blue")),
            "bundle null with a draft": mutate(lambda d: d.update(bundle=None)),
            "bundle name": mutate(lambda d: d["bundle"].update(name="../x.bundle")),
            "bundle sha": mutate(lambda d: d["bundle"].update(sha256="abc")),
            "bundle bytes zero": mutate(lambda d: d["bundle"].update(bytes=0)),
            "bundle bytes bool": mutate(lambda d: d["bundle"].update(bytes=True)),
            "bundle extra key": mutate(lambda d: d["bundle"].update(extra=1)),
            "bundle without drafts": mutate(lambda d: d.update(slices=[two, land["slices"][2]])),
            "stack flag only": mutate(lambda d: d.update(has_stack=True)),
            "positions only": mutate(lambda d: d.update(positions=["one"])),
            "position not a draft": mutate(lambda d: d.update(positions=["two"], has_stack=True)),
            "position repeated": mutate(lambda d: d.update(positions=["one", "one"], has_stack=True)),
            "has_stack int": mutate(lambda d: d.update(has_stack=0)),
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(assemble.LandError):
                    assemble.check_land(data)

    def stacked(self) -> dict:
        self.ready("one", {"a.txt": "1\n"})
        self.ready("two", {"b.txt": "2\n"})
        self.artifact("three", self.patch_of({"c.txt": "3\n"}))
        self.run_job("one", "two", "three")
        return self.land()

    def test_a_stack_land_json_is_valid_and_every_mutation_of_it_is_refused(self):
        land = self.stacked()
        self.assertEqual(assemble.check_land(copy.deepcopy(land)), land)
        one, two, three = land["slices"]
        stack = f"pivotal/issue-{ISSUE}/stack"

        def mutate(fn):
            data = copy.deepcopy(land)
            fn(data)
            return data

        cases = {
            "candidate flag off": mutate(lambda d: d["slices"][0].update(candidate=False)),
            "draft with the flag": mutate(lambda d: d["slices"][2].update(candidate=True)),
            "candidate on its own branch": mutate(lambda d: d["slices"][0].update(branch=f"pivotal/issue-{ISSUE}/one")),
            "draft on the stack branch": mutate(lambda d: d["slices"][2].update(branch=stack)),
            "candidate on another issue's stack": mutate(lambda d: d["slices"][0].update(branch="pivotal/issue-8/stack")),
            "candidate without commits": mutate(lambda d: d["slices"][0].update(commits=[], head=None)),
            "candidate head is not its last commit": mutate(lambda d: d["slices"][0].update(head="a" * 40)),
            "a commit in two slices": mutate(lambda d: d["slices"][1].update(commits=one["commits"], head=one["head"])),
            "positions empty": mutate(lambda d: d.update(positions=[], has_stack=False)),
            "positions reversed": mutate(lambda d: d.update(positions=["two", "one"])),
            "positions short": mutate(lambda d: d.update(positions=["one"])),
            "positions names a draft": mutate(lambda d: d.update(positions=["one", "two", "three"])),
            "positions repeated": mutate(lambda d: d.update(positions=["one", "one", "two"])),
            "has_stack off": mutate(lambda d: d.update(has_stack=False)),
            "a lone candidate with no bundle": mutate(lambda d: d.update(slices=[one], positions=["one"], bundle=None)),
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(assemble.LandError):
                    assemble.check_land(data)
        self.assertEqual((two["branch"], three["branch"]), (stack, f"pivotal/issue-{ISSUE}/three"))

    def test_read_land_refuses_odd_files(self):
        self.valid()
        out = Path(self.last_env["OUT_DIR"])
        good = (out / assemble.LAND).read_bytes()
        d = self.root / "dl"
        d.mkdir()
        cases = {
            "missing": None,
            "duplicate key": good.replace(b'"issue": 7', b'"issue": 7, "issue": 8', 1),
            "not json": b"{",
            "too big": b" " * (assemble.MAX_LAND_BYTES + 1),
            "not utf-8": b"\xff",
        }
        for name, data in cases.items():
            with self.subTest(name):
                path = d / name.replace(" ", "_")
                if data is not None:
                    path.write_bytes(data)
                with self.assertRaises(assemble.LandError):
                    assemble.read_land(path)
        link = d / "link"
        link.symlink_to(out / assemble.LAND)
        with self.assertRaises(assemble.LandError):
            assemble.read_land(link)
        fifo = d / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(assemble.LandError):
            assemble.read_land(fifo)
        self.assertEqual(assemble.read_land(out / assemble.LAND)["issue"], ISSUE)

    def test_check_bundle_refuses_a_tampered_or_mismatched_bundle(self):
        land = self.valid()
        out = Path(self.last_env["OUT_DIR"])
        bundle = out / assemble.BUNDLE
        assemble.check_bundle(land, bundle)
        data = bundle.read_bytes()
        d = self.root / "dl"
        d.mkdir()
        flipped = d / "flipped"
        flipped.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with self.assertRaises(assemble.LandError):
            assemble.check_bundle(land, flipped)
        # Another bundle of the same repository, size and hash recorded as if it were this one's.
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        other = d / "other.bundle"
        branch = land["slices"][0]["branch"]
        self.git("branch", "extra", land["slices"][0]["head"], cwd=work)
        self.git("bundle", "create", "-q", str(other), f"^{self.base}", f"refs/heads/{branch}", "refs/heads/extra", cwd=work)
        wrong = copy.deepcopy(land)
        wrong["bundle"] = {"name": assemble.BUNDLE, "sha256": hashlib.sha256(other.read_bytes()).hexdigest(),
                           "bytes": other.stat().st_size}
        with self.assertRaises(assemble.LandError):
            assemble.check_bundle(wrong, other)
        # A bundle without the base as its prerequisite.
        full = d / "full.bundle"
        self.git("bundle", "create", "-q", str(full), f"refs/heads/{branch}", cwd=work)
        wrong["bundle"] = {"name": assemble.BUNDLE, "sha256": hashlib.sha256(full.read_bytes()).hexdigest(),
                           "bytes": full.stat().st_size}
        with self.assertRaises(assemble.LandError):
            assemble.check_bundle(wrong, full)
        with self.assertRaises(assemble.LandError):
            assemble.check_bundle(land, d / "missing")
        (d / "empty").write_bytes(b"")
        with self.assertRaises(assemble.LandError):
            assemble.check_bundle(land, d / "empty")

    def test_the_patch_size_cap_is_the_build_jobs(self):
        self.assertEqual(assemble.MAX_PATCH_BYTES, buildjob.MAX_PATCH_BYTES)

    def test_the_slice_and_branch_rules_are_the_plans(self):
        self.assertIs(plan.ID_PATTERN.fullmatch("x" * 40) is not None, True)
        for slice_id in ("a", "a-b", "x" * 40, "0"):
            self.assertEqual(assemble.branch_name(7, slice_id), f"pivotal/issue-7/{slice_id}")
        self.assertEqual(assemble.SLICE_KEYS & {"id"}, {"id"})
        self.assertIn(job.OUTPUT_RULES["has_stack"].pattern, ("true|false",))


class FallThroughTest(AssembleCase):
    """A slice's fall-through is its build's items and then its review's, whatever the outcome: the land.json entry carries
    them to publish, which writes them into the RESULT (report.py reads them there). A verdict that is not for this slice,
    round and base carries none of its items."""

    BUILD = {"from": "pi:kaggle/qwen3.6-35b-a3b", "to": "claude:anthropic/sonnet", "reason": "endpoint down"}
    REVIEW = {"from": "agy:google/gemini", "to": "codex:openai/gpt-5", "reason": "rate-limited"}

    def test_a_build_fall_through_is_on_the_entry_of_a_draft_and_of_a_failure(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}), result={"fell_back": [self.BUILD]})
        self.artifact("two", None, result={"status": "failed", "reason": "gate_red", "gate": "red", "built_by": "", "agent": "",
                                           "fell_back": [self.BUILD]})
        self.run_job("one", "two")
        land = self.land()
        self.assertEqual([self.entry(land, s)["fell_back"] for s in ("one", "two")], [[self.BUILD], [self.BUILD]])

    def test_a_review_fall_through_follows_the_builds_on_the_entry(self):
        patch = self.patch_of({"a.txt": "1\n"})
        self.artifact("one", patch, result={"fell_back": [self.BUILD]})
        self.review_of("one", patch, ["a.txt"], verdict="NEEDS FIXES", fell_back=[self.REVIEW])
        self.run_job("one")
        entry = self.entry(self.land(), "one")
        self.assertEqual((entry["outcome"], entry["reason"]), ("draft", "verdict_needs_fixes"))
        self.assertEqual(entry["fell_back"], [self.BUILD, self.REVIEW])

    def test_a_review_fall_through_is_read_when_the_baseline_keeps_the_slice_a_draft(self):
        patch = self.patch_of({"a.txt": "1\n"})
        self.artifact("one", patch, result={"fell_back": [self.BUILD]})
        self.review_of("one", patch, ["a.txt"], fell_back=[self.REVIEW])
        self.run_job("one", BASELINE="red")
        entry = self.entry(self.land(), "one")
        self.assertEqual((entry["outcome"], entry["reason"]), ("draft", "baseline_red"))
        self.assertEqual(entry["fell_back"], [self.BUILD, self.REVIEW])

    def test_a_verdict_for_another_round_carries_none_of_its_items(self):
        patch = self.patch_of({"a.txt": "1\n"})
        self.artifact("one", patch, result={"fell_back": [self.BUILD]})
        self.review_of("one", patch, ["a.txt"], verdict="NEEDS FIXES", round=1, fell_back=[self.REVIEW])
        self.run_job("one")
        self.assertEqual(self.entry(self.land(), "one")["fell_back"], [self.BUILD])

    def test_check_land_refuses_a_fall_through_that_is_a_line_or_not_a_structured_item(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}), result={"fell_back": [self.BUILD]})
        self.run_job("one")
        land = self.land()
        for bad in (["fell_back: pi:kaggle/A → claude:anthropic/sonnet (endpoint down)"], [{**self.BUILD, "reason": "bored"}]):
            with self.subTest(fell_back=bad), self.assertRaises(assemble.LandError):
                assemble.check_land({**land, "slices": [{**land["slices"][0], "fell_back": bad}]})


class CapTest(AssembleCase):
    def failed(self, patch: bytes, reason: str) -> None:
        self.artifact("one", patch)
        code, _, _, stderr = self.run_job("one")
        self.assertEqual(code, 0, stderr)
        e = self.entry(self.land(), "one")
        self.assertEqual((e["outcome"], e["reason"], e["branch"]), ("failed", reason, None))

    def draft(self, patch: bytes) -> None:
        self.artifact("one", patch)
        code, _, _, stderr = self.run_job("one")
        self.assertEqual(code, 0, stderr)
        e = self.entry(self.land(), "one")
        self.assertEqual(e["outcome"], "draft")

    def test_from_lines_of_any_form_count_towards_the_commit_cap(self):
        """`git am` splits on any `From ` line: 2000 `From nobody` messages must not slip past the pre-count."""
        patch = b"".join(
            b"From nobody Mon Sep 17 00:00:00 2001\nFrom: D <d@example.com>\nSubject: [PATCH] x\n\n---\n\n" for _ in range(assemble.MAX_COMMITS + 1)
        )
        self.failed(patch, "too_many_commits")

    def test_a_tiny_binary_patch_declaring_a_huge_literal_is_refused(self):
        diff = (
            "diff --git a/b.bin b/b.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\n"
            f"literal {assemble.MAX_LITERAL_BYTES + 1}\nH\n\nliteral 0\nHcmV?d00001\n"
        )
        patch = mbox(("big", diff))
        self.assertLess(len(patch), 1000)
        self.failed(patch, "patch_too_large")

    MEGABYTE = 1024 * 1024

    def sized(self, name: str, kind: str, size: int) -> str:
        """A file whose forward hunk really inflates to ``size`` zeros (a `delta` of copy ops writing that much), so the
        scanner inflates it instead of stopping at its declared size."""
        stream = bytes(size) if kind == "literal" else copy_delta(0, size // 0x10000)
        return (
            f"diff --git a/{name}.bin b/{name}.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\n"
            + delta_hunk_text(kind, stream) + "literal 0\nHcmV?d00001\n\n"
        )

    def test_the_literals_of_all_files_are_summed(self):
        half = self.MEGABYTE // 2 + 1
        with mock.patch.object(assemble, "MAX_LITERAL_BYTES", self.MEGABYTE):
            self.failed(mbox(("two", self.sized("a", "literal", half) + self.sized("b", "literal", half))), "patch_too_large")

    def binary(self, *sizes, kind="literal", name="b"):
        return (
            f"diff --git a/{name}.bin b/{name}.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\n"
            + "".join(
                (delta_hunk_text("delta", copy_delta(0, 0), n) if kind == "delta" else f"{kind} {n}\nH\n\n") for n in sizes
            ) + "\n"
        )

    def test_a_zero_padded_literal_is_refused(self):
        self.failed(mbox(("pad", self.binary("0" * 12 + str(70 * 1024 * 1024), 0))), "bad_patch")

    def test_a_zero_padded_delta_is_refused(self):
        self.failed(mbox(("pad", self.binary("007", kind="delta"))), "bad_patch")

    def test_a_size_of_ten_digits_is_refused(self):
        self.failed(mbox(("big", self.binary("1" + "0" * 9))), "bad_patch")

    def test_the_deltas_of_all_files_are_summed_with_the_literals(self):
        patch = mbox(("two", self.sized("a", "literal", self.MEGABYTE // 2 + 1) + self.sized("c", "delta", 9 * 0x10000)))
        with mock.patch.object(assemble, "MAX_LITERAL_BYTES", self.MEGABYTE):
            self.failed(patch, "patch_too_large")

    def test_a_huge_delta_is_refused(self):
        self.failed(mbox(("big", self.binary(assemble.MAX_LITERAL_BYTES + 1, kind="delta"))), "patch_too_large")

    def test_words_in_a_commit_message_are_not_binary_sizes(self):
        patch = mbox(("x", new_file_diff("a.txt"))).replace(
            b"\n---\n", b"\ndelta force the retry\nliteral meaning is kept\n\n---\n", 1
        )
        self.assertIn(b"delta force the retry", patch)
        self.draft(patch)

    def test_a_message_with_the_same_words_does_not_hide_a_padded_size_in_a_hunk(self):
        patch = mbox(("x", self.binary("007", kind="delta"))).replace(
            b"\n---\n", b"\ndelta 5\nliteral 5\n\n---\n", 1
        )
        self.failed(patch, "bad_patch")

    def real_mbox(self, message: str, data: bytes, *flags: str) -> bytes:
        """What `git format-patch --binary` writes for one commit that adds b.bin, made in a scratch repository."""
        work = self.root / "mk"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        self.git("init", "-q", cwd=work)
        (work / "keep").write_text("k\n")
        self.git("add", "keep", cwd=work)
        self.git("commit", "-qm", "base", cwd=work)
        (work / "b.bin").write_bytes(data)
        self.git("add", "b.bin", cwd=work)
        self.git("commit", "-qm", message, cwd=work)
        return subprocess.run(
            ["/usr/bin/git", "format-patch", "--binary", *flags, "-1", "--stdout"], cwd=work, env=clean_git_env(self.home),
            capture_output=True, check=True,
        ).stdout

    def huge(self) -> bytes:
        return bytes(assemble.MAX_LITERAL_BYTES + 1)  # zeros: a few hundred KB of zlib that git am inflates to 64 MB

    def test_a_crlf_binary_header_is_still_a_header(self):
        """git am applies `GIT binary patch\\r`; an exact-match scanner read no hunk and wrote the blob."""
        mail = self.real_mbox("x", self.huge())
        i = mail.rindex(b"GIT binary patch\n")
        self.assertIn(b"literal %d\n" % len(self.huge()), mail[i:])
        self.failed(mail[:i] + b"GIT binary patch\r\n" + mail[i + 17 :], "patch_too_large")

    def test_a_binary_header_is_matched_by_prefix(self):
        """Whatever follows the words on the header line, the hunk after it is counted (the safe side of git's own reading)."""
        patch = mbox(("x", self.binary("007"))).replace(b"GIT binary patch\n", b"GIT binary patch \n")
        self.assertIn(b"GIT binary patch \n", patch)
        self.failed(patch, "bad_patch")

    def test_a_message_that_looks_like_a_hunk_cannot_swallow_the_real_one(self):
        """The message ends in `---` with no blank line before the real diff; skipping to a blank line ate the real header."""
        mail = self.real_mbox("x\n\nGIT binary patch\nliteral 0\nnotes\n", self.huge())
        start = mail.index(b"---\n") + 4
        mail = mail[:start] + mail[mail.index(b"diff --git") :]  # no diffstat, so no blank line
        self.assertNotIn(b"\n\ndiff --git", mail[:start + 20])
        self.failed(mail, "patch_too_large")

    def test_a_message_with_padded_sizes_after_a_binary_header_is_a_draft(self):
        mail = self.real_mbox("x\n\nGIT binary patch\nliteral 007\ndelta x\nliteral 99999999999\n", b"\x00\x01" * 50)
        self.assertIn(b"literal 007", mail)
        self.draft(mail)

    def test_a_padded_size_in_the_second_hunk_is_found(self):
        self.failed(mbox(("x", self.binary(5, "007"))), "bad_patch")

    def amplifying_mbox(self, copies: int, stream: bytes | None = None) -> bytes:
        """Two mails: the first adds b.bin (64 KB, a real format-patch), the second a delta on it whose `copies` ops of one
        byte each write copies * 64 KB. The second mail is hand-made, so its blob hash is the real one of the result."""
        base = bytes(range(256)) * 256
        mail = self.real_mbox("add the base", base, "--full-index")
        out = hashlib.sha1(b"blob %d\0" % (copies * 0x10000))
        for _ in range(copies):
            out.update(base)
        diff = (
            f"diff --git a/b.bin b/b.bin\nindex {hashlib.sha1(b'blob 65536' + chr(0).encode() + base).hexdigest()}..{out.hexdigest()} 100644\n"
            "GIT binary patch\n" + delta_hunk_text("delta", copy_delta(len(base), copies) if stream is None else stream)
            + delta_hunk_text("literal", b"")
        )
        return mail + mbox(("amplify", diff))

    def test_a_delta_that_copies_a_base_into_a_huge_output_is_refused(self):
        """The hunk's own size is the delta stream (a few bytes); its target size is what git writes (2 MB, cap 1 MB)."""
        patch = self.amplifying_mbox(32)
        self.assertLess(len(patch), 4000)
        with mock.patch.object(assemble, "MAX_LITERAL_BYTES", 1024 * 1024):
            self.failed(patch, "patch_too_large")

    def test_a_delta_under_the_cap_is_a_draft_and_git_writes_what_it_declared(self):
        patch = self.amplifying_mbox(4)
        with mock.patch.object(assemble, "MAX_LITERAL_BYTES", 1024 * 1024):
            self.draft(patch)
        work = Path(self.last_env["WORK_DIR"]) / "repo"
        size = int(self.git("cat-file", "-s", "pivotal/issue-7/one:b.bin", cwd=work))
        self.assertEqual(size, 4 * 0x10000)

    def test_a_delta_target_is_read_from_the_header_not_from_the_hunk_size(self):
        """The hunk's own size is the stream (a few bytes); only the second header varint says what it writes."""
        self.assertEqual(assemble.delta_target(copy_delta(5, 3)), 3 * 0x10000)

    def hunk_failure(self, diff_hunks: str, n: int) -> tuple[str, str]:
        diff = "diff --git a/b.bin b/b.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\n" + diff_hunks
        self.artifact(f"s{n}", mbox(("x", diff)))
        code, _, _, stderr = self.run_job(f"s{n}")
        self.assertEqual(code, 0, stderr)
        e = self.entry(self.land(), f"s{n}")
        return e["outcome"], e["reason"]

    def test_a_malformed_delta_hunk_is_bad_patch(self):
        stream = copy_delta(5, 3)
        good = delta_hunk_text("delta", stream).splitlines()
        size = len(stream)
        cases = {
            "not base85": [f"delta {size}", "H~~~~~~~~~~", ""],
            "length letter wrong": [f"delta {size}", "A" + good[1][1:], ""],
            "not a zlib stream": [f"delta {size}", "F" + base64.b85encode(b"abcdefgh").decode()[:10], ""],
            "truncated header": delta_hunk_text("delta", b"\x05").splitlines() + [""],
            "target varint never ends": delta_hunk_text("delta", b"\x05" + b"\xff" * 22).splitlines() + [""],
            "no data": [f"delta {size}", ""],
            "line longer than its letter says": [f"delta {size}", good[1] + "00000", ""],
        }
        for n, (name, hunk) in enumerate(cases.items()):
            with self.subTest(name):
                hunk = [x for x in hunk if x != ""] + [""]
                self.assertEqual(self.hunk_failure("\n".join(hunk) + "\n", n), ("failed", "bad_patch"))

    def bomb(self, kind: str = "literal", declared: int = 10) -> str:
        """A hunk that declares ``declared`` bytes and inflates to 4 MB (about 4 KB of zlib)."""
        return delta_hunk_text(kind, bytes(4 * self.MEGABYTE), declared) + "literal 0\nHcmV?d00001\n\n"

    def test_a_bomb_behind_a_small_declared_size_is_bad_patch_and_never_reaches_apply(self):
        seen = []
        real = assemble.Git.run

        def spy(git, *args, **kw):
            seen.append(args[0])
            return real(git, *args, **kw)

        for n, kind in enumerate(("literal", "delta")):
            with self.subTest(kind), mock.patch.object(assemble.Git, "run", spy):
                seen.clear()
                self.assertEqual(self.hunk_failure(self.bomb(kind), n), ("failed", "bad_patch"))
                self.assertNotIn("apply", seen)
                self.assertNotIn("am", seen)

    def test_inflating_a_bomb_is_bounded_by_the_declared_size(self):
        raw = zlib.compress(bytes(4 * self.MEGABYTE))
        tracemalloc.start()
        try:
            with self.assertRaises(assemble.SliceFailure):
                assemble.inflate_exact(raw, 10)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, self.MEGABYTE // 4)

    def test_a_hunk_must_inflate_to_exactly_its_size_and_end_its_stream(self):
        raw = zlib.compress(b"abcdef")
        self.assertEqual(assemble.inflate_exact(raw, 6), b"abcdef")
        cases = {"too short": (raw, 7), "too long": (raw, 5), "stream cut off": (raw[:-4], 6), "empty": (b"", 6),
                 "not zlib": (b"not zlib at all", 6)}
        for name, (data, size) in cases.items():
            with self.subTest(name), self.assertRaises(assemble.SliceFailure) as err:
                assemble.inflate_exact(data, size)
            self.assertEqual(err.exception.reason, "bad_patch")

    def test_a_delta_header_of_more_than_twenty_bytes_is_bad_patch(self):
        twenty = b"\x80" * 9 + b"\x00" + b"\x81" + b"\x80" * 8 + b"\x00"
        self.assertEqual(len(twenty), 20)
        self.assertEqual(assemble.delta_target(twenty), 1)
        with self.assertRaises(assemble.SliceFailure) as err:
            assemble.delta_target(b"\x80" * 9 + b"\x00" + b"\x80" * 10 + b"\x01")
        self.assertEqual(err.exception.reason, "bad_patch")

    def test_a_bomb_in_a_commit_message_is_not_given_to_apply(self):
        """Only the patch text `git am` applies reaches `git apply --numstat`; the message is not in it."""
        message = "x\n\nGIT binary patch\n" + self.bomb()
        mail = self.real_mbox(message, b"\x00\x01" * 50)
        inputs = []
        real = assemble.Git.run

        def spy(git, *args, **kw):
            if args[0] == "apply":
                inputs.append(kw["input"])
            return real(git, *args, **kw)

        with mock.patch.object(assemble.Git, "run", spy):
            self.draft(mail)
        self.assertEqual(len(inputs), 1)
        self.assertIn(b"b.bin", inputs[0])
        self.assertNotIn(b"m.bin", inputs[0])
        self.assertLess(len(inputs[0]), 1000)

    def test_the_reverse_hunk_is_not_counted_but_is_checked(self):
        """git am never applies the reverse hunk: a change whose reverse is as big as the cap by itself is not summed."""
        def hunks(forward: str, reverse: str) -> str:
            return f"{forward}\n\n{reverse}\n\n"
        header = "diff --git a/b.bin b/b.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\n"
        forward = delta_hunk_text("literal", bytes(self.MEGABYTE // 2 + 1))
        with tempfile.TemporaryDirectory() as tmp:
            git = assemble.Git(Path(tmp) / "w", "0" * 40)
            git.root.mkdir()
            git.init()
            with mock.patch.object(assemble, "MAX_LITERAL_BYTES", self.MEGABYTE):
                big = delta_hunk_text("literal", bytes(self.MEGABYTE // 2 + 1))
                patches = assemble.scan_mails(git, mbox(("x", header + forward + big)))
                self.assertEqual(len(patches), 1)
                for name, reverse in (("padded", "literal 007\nH"), ("over the cap", f"literal {self.MEGABYTE + 1}\nH")):
                    with self.subTest(name), self.assertRaises(assemble.SliceFailure):
                        assemble.scan_mails(git, mbox(("x", header + forward + reverse + "\n\n")))

    def reversed_files(self, count: int, size: int) -> str:
        """``count`` files whose forward hunk is tiny and whose reverse hunk really inflates to ``size`` zeros."""
        return "".join(
            f"diff --git a/f{n}.bin b/f{n}.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\n"
            + delta_hunk_text("literal", b"x") + delta_hunk_text("literal", bytes(size))
            for n in range(count)
        )

    def test_the_reverse_hunks_of_all_files_are_summed_apart_from_the_forward_ones(self):
        """git keeps every reverse buffer of a mail until it exits: several under the cap one by one are too many."""
        seen = []
        real = assemble.Git.run

        def spy(git, *args, **kw):
            seen.append(args[0])
            return real(git, *args, **kw)

        third = self.MEGABYTE * 4 // 10
        with mock.patch.object(assemble, "MAX_LITERAL_BYTES", self.MEGABYTE), mock.patch.object(assemble.Git, "run", spy):
            self.failed(mbox(("many", self.reversed_files(3, third))), "patch_too_large")
            self.assertNotIn("apply", seen)
            self.assertNotIn("am", seen)

    def test_reverse_hunks_under_the_cap_in_sum_pass_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(assemble, "MAX_LITERAL_BYTES", self.MEGABYTE):
            git = assemble.Git(Path(tmp) / "w", "0" * 40)
            git.root.mkdir()
            git.init()
            patches = assemble.scan_mails(git, mbox(("few", self.reversed_files(2, self.MEGABYTE * 4 // 10))))
        self.assertEqual(len(patches), 1)

    def test_a_reverse_hunk_must_inflate_to_exactly_its_size(self):
        """A reverse `literal 10` over a 4 MB stream is bad_patch, not something left for git am to refuse."""
        diff = (
            "diff --git a/b.bin b/b.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\n"
            + delta_hunk_text("literal", b"x") + delta_hunk_text("literal", bytes(4 * self.MEGABYTE), 10)
        )
        self.failed(mbox(("x", diff)), "bad_patch")

    def test_a_real_binary_modification_of_64k_is_a_draft(self):
        work = self.root / "mk2"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        self.git("init", "-q", cwd=work)
        for i, data in enumerate((os.urandom(64 * 1024), os.urandom(64 * 1024))):
            (work / "b.bin").write_bytes(data)
            self.git("add", "b.bin", cwd=work)
            self.git("commit", "-qm", f"c{i}", cwd=work)
        mail = subprocess.run(
            ["/usr/bin/git", "format-patch", "--binary", "-2", "--stdout"], cwd=work, env=clean_git_env(self.home),
            capture_output=True, check=True,
        ).stdout
        self.assertEqual(mail.count(b"GIT binary patch"), 2)
        self.draft(mail)

    def test_a_mail_without_a_from_line_is_scanned_as_git_am_reads_it(self):
        """`git am` runs `mailsplit -b`: a mail that starts with its headers is a mail, and its cap is checked."""
        mail = self.real_mbox("x", self.huge())
        bare = mail[mail.index(b"\n") + 1 :]
        self.assertTrue(bare.startswith(b"From: "))
        self.failed(bare, "patch_too_large")


class ParityTest(unittest.TestCase):
    def test_every_build_failure_reason_is_a_land_reason(self):
        text = build.FAIL_REASONS.pattern
        words = [w for w in text.split("|")]
        for word in words:
            word = word.replace("error:[0-9]{1,3}", "error:123")
            self.assertIsNotNone(build.FAIL_REASONS.fullmatch(word), word)
            self.assertIsNotNone(assemble.REASON.fullmatch(word), word)
        for fixed in ("no_artifact", "bad_artifact", "no_result", "bad_result", "no_patch", "fix_round", "no_stack",
                      "baseline_red", "workflow_change", "does_not_apply", "unsafe_path", "bad_patch", "internal",
                      "too_many_commits", "patch_too_large", "stacked", "conflict", "gate_not_run", "verdict_missing",
                      "verdict_invalid", "verdict_stale", "verdict_needs_fixes", "verdict_wrong_sha", "verdict_files",
                      "verdict_wrong_builder"):
            self.assertIsNotNone(assemble.REASON.fullmatch(fixed), fixed)

    def test_the_artifact_name_rule_is_the_plans_id_rule(self):
        self.assertEqual(assemble.list_artifacts.__module__, "engine.assemble")
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("slice-" + "a" * 40, "slice-" + "a" * 41, "slice-A", "slice-a-b"):
                os.mkdir(Path(tmp) / name)
            fd = os.open(tmp, os.O_RDONLY)
            try:
                notes = assemble.list_artifacts(fd, ["a" * 40, "a" * 41, "a-b"])
            finally:
                os.close(fd)
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(n.startswith("ignored: ") for n in notes))
        self.assertEqual(plan.ID_PATTERN.fullmatch("a" * 41), None)

    def test_the_positions_rule_allows_exactly_the_plans_slice_limit(self):
        rule = job.OUTPUT_RULES["positions"]
        self.assertIsNotNone(rule.fullmatch(",".join(["a"] * plan.MAX_SLICES)))
        self.assertIsNone(rule.fullmatch(",".join(["a"] * (plan.MAX_SLICES + 1))))


class ScriptTest(AssembleCase):
    def test_the_script_runs_as_the_workflow_runs_it_and_two_runs_agree(self):
        """`python3 -I engine/assemble.py run`, twice, in separate processes: the same commits, the same files."""
        self.artifact("one", self.patch_of({"src/a.txt": "alpha\n1\n"}, {"README.md": "r\n"}))
        self.artifact("two", self.patch_of({".github/workflows/ci.yml": "name: x\n"}))
        script = Path(assemble.__file__)
        seen = []
        for _ in range(2):
            env = self.env("one", "two")
            Path(env["GITHUB_OUTPUT"]).write_text("")
            done = subprocess.run(["python3", "-I", str(script), "run"], env={"PATH": os.environ["PATH"], **env},
                                  capture_output=True, text=True, cwd=self.root, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("slice two: blocked (workflow_change)", done.stdout)
            out = Path(env["OUT_DIR"])
            seen.append(((out / assemble.LAND).read_bytes(), (out / assemble.BUNDLE).read_bytes()))
            digest = hashlib.sha256(seen[-1][0]).hexdigest()
            self.assertEqual(Path(env["GITHUB_OUTPUT"]).read_text(), f"positions=\ngate_matrix=[]\nhas_stack=false\nland_sha256={digest}\nassembled=true\n")
        self.assertEqual(seen[0], seen[1])
        land = json.loads(seen[0][0])
        self.assertEqual([(s["id"], s["outcome"]) for s in land["slices"]], [("one", "draft"), ("two", "blocked")])


class OutputTest(AssembleCase):
    def test_outputs_are_valid_per_the_job_rules(self):
        for name, value in (("positions", ""), ("positions", "a,b-c"), ("has_stack", "false"), ("land_sha256", "a" * 64), ("assembled", "true")):
            self.assertIsNotNone(job.OUTPUT_RULES[name].fullmatch(value))
        for value in ("", "a" * 63, "A" * 64, "a" * 64 + "\n"):
            self.assertIsNone(job.OUTPUT_RULES["land_sha256"].fullmatch(value), value)
        for value in ("a\nb", "a,", ",a", "A", "a b", "../x", ",".join(["a"] * 7)):
            self.assertIsNone(job.OUTPUT_RULES["positions"].fullmatch(value), value)

    def test_the_gate_matrix_rule_takes_a_json_array_of_slice_ids_and_nothing_else(self):
        rule = job.OUTPUT_RULES["gate_matrix"]
        for value in ('[]', '["a"]', '["a","b-c"]', json.dumps(["a"] * 6, separators=(",", ":"))):
            self.assertIsNotNone(rule.fullmatch(value), value)
        for value in ("", '["a", "b"]', '["A"]', '["a"]\n', '["a\\"]', '["a"],["b"]', '[1]', '["a","b"', "a,b",
                      json.dumps(["a"] * 7, separators=(",", ":")), '["a"]"x"', '["../x"]', '["a\nb"]', '["' + "a" * 41 + '"]'):
            self.assertIsNone(rule.fullmatch(value), value)

    def test_the_output_file_is_appended_to_not_replaced(self):
        self.artifact("one", self.patch_of({"a.txt": "1\n"}))
        env = self.env("one")
        Path(env["GITHUB_OUTPUT"]).write_text("earlier=1\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(assemble.main(["run"], env), 0)
        digest = hashlib.sha256((Path(env["OUT_DIR"]) / assemble.LAND).read_bytes()).hexdigest()
        self.assertEqual(
            Path(env["GITHUB_OUTPUT"]).read_text(), f"earlier=1\npositions=\ngate_matrix=[]\nhas_stack=false\nland_sha256={digest}\nassembled=true\n"
        )


class ShallowBaseTest(AssembleCase):
    """The base is the root of a shallow clone, as the workflow's checkout makes it (`actions/checkout` at its default
    fetch-depth 1). REPO_DIR is that clone, and the base has no parent in it."""

    def setUp(self):
        super().setUp()
        self.commit({"src/a.txt": "beta\n"}, message="tip")  # the source's second commit becomes the base
        self.base = self.git("rev-parse", "HEAD")
        self.shallow = self.root / "shallow"
        self.git("clone", "-q", "--depth", "1", f"file://{self.repo}", str(self.shallow), cwd=self.root)

    def test_fetch_base_reads_the_root_of_a_shallow_clone(self):
        (self.root / "work").mkdir()  # assemble() makes the work directory before it inits the repository
        git = assemble.Git(self.root / "work", self.base)
        git.init()
        self.assertEqual(git.fetch_base(self.shallow), int(self.git("show", "-s", "--format=%ct", self.base)))
        self.assertEqual(git.out("cat-file", "-t", self.base), "commit")

    def test_the_run_on_a_shallow_repo_gives_the_land_of_the_full_repo(self):
        self.artifact("one", self.patch_of({"src/a.txt": "alpha\n"}))  # a draft: no review
        self.ready("two", {"src/b.txt": "bee\n"})  # a candidate: a READY review
        code, _, stdout, _ = self.run_job("one", "two", REPO_DIR=str(self.shallow))
        self.assertEqual(code, 0, stdout)
        land = self.land()
        self.assertEqual([(s["id"], s["outcome"]) for s in land["slices"]], [("one", "draft"), ("two", "candidate")])
        self.assertTrue(land["has_stack"])
        self.assertEqual(land["base_sha"], self.base)
        shallow = (Path(self.last_env["OUT_DIR"]) / assemble.LAND).read_bytes()
        code, _, stdout, _ = self.run_job("one", "two")  # the same slices, REPO_DIR the full repository at the same base
        self.assertEqual(code, 0, stdout)
        self.assertEqual((Path(self.last_env["OUT_DIR"]) / assemble.LAND).read_bytes(), shallow)


if __name__ == "__main__":
    unittest.main()
