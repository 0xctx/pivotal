"""engine/doccheck.py: a checkout is scanned by the Markdown files git tracks, not by the untracked scratch files a
local tree holds (the operator's `.sysop` store), and a directory that is not a checkout is still walked. Also: the
repeated-word check must not count words that a code span separates (the docs line's gate)."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import doccheck  # noqa: E402

GIT = shutil.which("git")


def clean_env() -> dict[str, str]:
    """The environment a fixture's git runs in: no configuration of the operator, and no GIT_* that points elsewhere."""
    return {**{name: value for name, value in os.environ.items() if name not in doccheck.GIT_ENV_DROP},
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def problems(text: str) -> list[str]:
    return doccheck.check_text(text, ROOT / "a.md", ROOT)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """One git call in a fixture. No identity is configured or needed: `add` writes the index, it commits nothing."""
    return subprocess.run([GIT, *args], cwd=root, capture_output=True, text=True, env=clean_env())


def inside_git(root: Path) -> bool:
    """Whether git would read a repository above this directory."""
    return git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def write(root: Path, name: str, text: str = "# T\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FakeChild:
    """A git that has already written its answer, so the parse is tested without a repository or a process."""

    def __init__(self, data: bytes, status: int = 0):
        self.stdout = io.BytesIO(data)
        self.status = status

    def kill(self) -> None:
        pass

    def wait(self) -> int:
        return self.status


class RepeatedWordTest(unittest.TestCase):
    def test_a_code_span_between_two_words_is_not_a_repeat(self):
        # The text reads "in SERIAL in run_suite.py": the words are not written twice.
        self.assertEqual(problems("the modules in `SERIAL` in `test/run_suite.py` run alone\n"), [])

    def test_or_around_code_spans_is_not_a_repeat(self):
        # docs/RUNNER.md:122, a false positive before this fix: "read `environ` or `mem` or attach".
        self.assertEqual(problems("cannot read `<pid>/environ` or `mem` or attach\n"), [])

    def test_a_word_written_twice_in_prose_is_still_caught(self):
        found = problems("the word or or here\n")
        self.assertEqual(len(found), 1, found)
        self.assertIn("'or' is written twice", found[0])

    def test_a_word_written_twice_across_a_code_span_boundary_is_still_caught(self):
        found = problems("this is the the `x` code\n")
        self.assertEqual(len(found), 1, found)

    def test_the_repo_markdown_has_no_problems(self):
        out = io.StringIO()
        self.assertEqual(doccheck.check(ROOT, out), 0, out.getvalue())


class PlainPathTest(unittest.TestCase):
    """A name the index prints is a path under the root, or nothing: the index is repo data, not a promise (F5)."""

    def test_a_plain_relative_name_is_a_path_under_the_root(self):
        self.assertEqual(doccheck.plain_path(ROOT, "docs/QUICKSTART.md"), ROOT / "docs" / "QUICKSTART.md")
        self.assertEqual(doccheck.plain_path(ROOT, "./docs/QUICKSTART.md"), ROOT / "docs" / "QUICKSTART.md")
        self.assertEqual(doccheck.plain_path(ROOT, "docs/../README.md"), ROOT / "README.md")

    def test_a_name_that_is_not_a_plain_path_inside_the_root_is_refused(self):
        for name in ("", "/etc/passwd", "..", "../x.md", "docs/../../x.md"):
            with self.subTest(name=name):
                self.assertIsNone(doccheck.plain_path(ROOT, name))

    def test_a_refused_tracked_name_fails_the_check_and_is_not_skipped(self):
        out = io.StringIO()
        refused = ValueError("a tracked name is not a plain path inside the tree")
        with mock.patch.object(doccheck, "tracked_markdown", side_effect=refused):
            self.assertEqual(doccheck.check(ROOT, out), 1)
        self.assertIn("doccheck: a tracked name is not a plain path inside the tree", out.getvalue())


@unittest.skipUnless(GIT, "git is not installed")
class TrackedScanTest(unittest.TestCase):
    """A checkout is scanned by the files git tracks; a directory that is not a checkout is walked (the fallback)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.assertEqual(git(self.root, "init", "--quiet").returncode, 0, "git init failed in the fixture")

    def track(self, *names: str) -> None:
        for name in names:
            done = git(self.root, "add", "--", name)
            self.assertEqual(done.returncode, 0, done.stderr)

    def check(self) -> tuple[int, str]:
        out = io.StringIO()
        return doccheck.check(self.root, out), out.getvalue()

    def test_an_untracked_markdown_file_does_not_fail_the_gate(self):  # mutant: ignores git ls-files
        write(self.root, "docs/ok.md")
        self.track("docs/ok.md")
        write(self.root, "scratch.md", "# T\n\nSee [x](missing.md).\n\nThis teh sentence.\n")
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertNotIn("scratch.md", out, "an untracked file was reported as a doc")

    def test_a_tracked_markdown_file_fails_the_gate(self):  # mutant: the tracked scan returns no files
        write(self.root, "bad.md", "# T\n\nThis teh sentence.\n")
        self.track("bad.md")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("bad.md", out)
        self.assertIn("misspelled 'teh'", out)

    def test_a_tracked_markdown_file_under_docs_fails_the_gate(self):  # mutant: skips tracked files under docs/
        write(self.root, "docs/bad.md", "# T\n\nSee [x](missing.md).\n")
        self.track("docs/bad.md")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("docs/bad.md", out)

    def test_the_tracked_scan_reads_at_most_one_name_past_its_limit(self):
        for name in ("a.md", "b.md", "c.md"):
            write(self.root, name)
        self.track("a.md", "b.md", "c.md")
        self.assertEqual([path.name for path in doccheck.tracked_markdown(self.root, limit=2)], ["a.md", "b.md", "c.md"])

    def test_more_markdown_files_than_the_limit_refuse_the_check(self):
        write(self.root, "a.md")
        write(self.root, "b.md")
        self.track("a.md", "b.md")
        out = io.StringIO()
        with mock.patch.object(doccheck, "MAX_FILES", 1):
            self.assertEqual(doccheck.check(self.root, out), 1)
        self.assertIn("more than 1 Markdown files: not checked", out.getvalue())

    def test_a_git_directory_variable_of_the_callers_environment_is_ignored(self):  # mutant: the environment is passed on
        # The scan is of this tree: a GIT_DIR or GIT_WORK_TREE of the caller may not point git at another repository.
        write(self.root, "bad.md", "# T\n\nThis teh sentence.\n")
        self.track("bad.md")
        write(self.root, "scratch.md", "# T\n\nSee [x](missing.md).\n")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent", "GIT_WORK_TREE": "/nonexistent"}):
            self.assertEqual(doccheck.check(self.root, out), 1, out.getvalue())
        self.assertIn("bad.md", out.getvalue())
        self.assertNotIn("scratch.md", out.getvalue(), "the scan fell back to the walk and read an untracked file")


@unittest.skipUnless(GIT, "git is not installed")
class WalkFallbackTest(unittest.TestCase):
    """Without a checkout the tree is walked as it was before, and the store directories are skipped explicitly."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        if inside_git(self.root):
            self.skipTest("the temporary directory is inside a git checkout")

    def test_a_directory_that_is_not_a_checkout_is_walked(self):  # mutant: the fallback yields nothing
        write(self.root, "bad.md", "# T\n\nThis teh sentence.\n")
        out = io.StringIO()
        self.assertEqual(doccheck.check(self.root, out), 1, out.getvalue())
        self.assertIn("bad.md", out.getvalue())

    def test_a_machine_without_git_walks_the_tree(self):  # mutant: the fallback is gone when git cannot run
        write(self.root, "bad.md", "# T\n\nThis teh sentence.\n")
        out = io.StringIO()
        with mock.patch.object(doccheck, "GIT_LS_FILES", ("doccheck-no-such-git",)):
            self.assertEqual(doccheck.check(self.root, out), 1, out.getvalue())
        self.assertIn("bad.md", out.getvalue())

    def test_the_walk_skips_the_store_directories(self):  # mutant: SKIP_DIRS loses .sysop
        for name in ("a.md", "sub/b.md", ".sysop/brief.md", "node_modules/pkg.md", ".git/hooks.md", ".venv/lib.md"):
            write(self.root, name)
        found = sorted(str(path.relative_to(self.root)) for path in doccheck.markdown_files(self.root))
        self.assertEqual(found, ["a.md", "sub/b.md"])


class GitAnswerTest(unittest.TestCase):
    """The answer of ``git ls-files -z`` is read as data: every name is one NUL-terminated path, and the rest is refused."""

    def parse(self, data: bytes) -> list[Path] | None:
        with mock.patch.object(doccheck.subprocess, "Popen", return_value=FakeChild(data)):
            return doccheck.tracked_markdown(ROOT)

    def test_only_the_markdown_names_are_read(self):
        self.assertEqual([path.name for path in self.parse(b"docs/a.md\0notes.txt\0b.md\0")], ["a.md", "b.md"])

    def test_a_last_name_without_its_terminator_is_refused(self):  # mutant: the last name is dropped
        with self.assertRaisesRegex(ValueError, "is not terminated"):
            self.parse(b"a.md")

    def test_a_name_that_leaves_the_tree_is_refused(self):
        with self.assertRaisesRegex(ValueError, "is not a plain path inside the tree"):
            self.parse(b"../outside.md\0")


@unittest.skipUnless(GIT, "git is not installed")
class RepoScanTest(unittest.TestCase):
    def test_the_markdown_files_of_this_repository_are_exactly_the_tracked_ones(self):  # mutant: ignores git ls-files
        if not inside_git(ROOT):
            self.skipTest("the repository is not a git checkout here")
        tracked = subprocess.run([GIT, "ls-files", "-z", "--", "*.md"], cwd=ROOT, capture_output=True,
                                 env=clean_env())
        self.assertEqual(tracked.returncode, 0, tracked.stderr)
        names = sorted(os.fsdecode(raw) for raw in tracked.stdout.split(b"\0") if raw)
        self.assertIn("AGENTS.md", names)
        self.assertEqual(sorted(str(path.relative_to(ROOT)) for path in doccheck.markdown_files(ROOT)), names)


if __name__ == "__main__":
    unittest.main()
