"""The parallel runner (test/run_suite.py): fail closed, reap every child, same modules as discovery.

Fake test directories are fixtures in a temporary directory; the runner runs them as real
subprocesses, exactly as it runs the repo's modules. No network, no real /proc scan outside
the processes these tests start themselves.
"""

import io
import os
from collections import Counter
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import run_suite

TEST_DIR = Path(__file__).resolve().parent
# The runner's CLI with no SERIAL group, so a fake directory runs in one process pool.
# A process started in the background by a non-interactive shell (a landing check, nohup, cron) inherits SIGINT as
# ignored, and Python then never raises KeyboardInterrupt; the driver restores the terminal's Ctrl-C behaviour so the
# interrupt tests mean the same thing wherever the suite is launched.
DRIVER = (
    "import signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler); "
    "sys.path.insert(0, %r); import run_suite; "
    "sys.exit(run_suite.main(['--dir', sys.argv[1], '-j', '2'], serial=(), shard_by_class=()))" % str(TEST_DIR)
)

PASS = """\
import unittest


class PassTest(unittest.TestCase):
    def test_passes(self):
        self.assertEqual(1, 1)
"""

FIXTURES = {
    "test_pass.py": PASS,
    "test_fail.py": """\
import unittest


class FailTest(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2)
""",
    "test_error.py": """\
import unittest


class ErrorTest(unittest.TestCase):
    def test_errors(self):
        raise RuntimeError("boom")
""",
    "test_crash.py": """\
import os
import signal
import unittest


class CrashTest(unittest.TestCase):
    def test_kills_its_own_worker(self):
        os.kill(os.getpid(), signal.SIGKILL)
""",
    "test_silent.py": """\
import os

os._exit(0)  # exits 0 without a unittest result: the module did not run its tests
""",
}

# Starts, records its pid (and its child's) in the test directory, and waits to be killed.
VICTIM = """\
import os
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class VictimTest(unittest.TestCase):
    def test_waits_to_be_killed(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        with open(os.path.join(HERE, "..", "victim.pids"), "w") as fh:
            fh.write(f"{os.getpid()} {child.pid}")
        time.sleep(600)
"""


# Records its pid in the test directory's parent and sleeps: a shard with nothing else to kill.
SLEEPER = """\
import os
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class SleeperTest(unittest.TestCase):
    def test_sleeps(self):
        with open(os.path.join(HERE, "..", "sleeper.pid"), "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(600)
"""


# Starts a grandchild in a session of its own (the shard's process group cannot reach it), records its
# pid in the test directory's parent, and exits: the grandchild outlives its shard.
ESCAPES_GROUP = """\
import os
import subprocess
import sys
import unittest


class EscapeTest(unittest.TestCase):
    def test_starts_a_session_of_its_own(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                 start_new_session=True)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "escaped.pid")
        with open(path, "w") as fh:
            fh.write(str(child.pid))
"""


# Starts a grandchild in its own session, records both pids, and waits: the run is still in progress
# when the runner is sent SIGTERM.
ESCAPES_THEN_WAITS = """\
import os
import subprocess
import sys
import time
import unittest


class WaitTest(unittest.TestCase):
    def test_waits_mid_run(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                 start_new_session=True)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "escaped.pid")
        with open(path, "w") as fh:
            fh.write(f"{os.getpid()} {child.pid}")
        time.sleep(600)
"""


# Checks that the shard, and a child it starts, do not block SIGINT or SIGTERM (the runner blocks them only
# around its own spawn). The child has a SIGTERM handler and must exit 7 when the shard sends SIGTERM.
MASKS = """\
import signal
import subprocess
import sys
import unittest

BLOCKED = (1 << (signal.SIGINT - 1)) | (1 << (signal.SIGTERM - 1))
CHILD = (
    "import os, signal, time\\n"
    "signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(7))\\n"
    "print(open('/proc/self/status').read().split('SigBlk:')[1].split()[0], flush=True)\\n"
    "time.sleep(600)\\n"
)


def sig_blk(pid="self"):
    with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("SigBlk:"):
                return int(line.split()[1], 16)
    raise AssertionError("no SigBlk line")


class MaskTest(unittest.TestCase):
    def test_the_shard_does_not_block_sigint_or_sigterm(self):
        self.assertEqual(sig_blk() & BLOCKED, 0, "the shard inherited a blocked SIGINT or SIGTERM")

    def test_a_child_of_the_shard_gets_the_sigterm_sent_to_it(self):
        child = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True)
        try:
            mask = child.stdout.readline().strip()
            self.assertEqual(int(mask, 16) & BLOCKED, 0, "the child inherited a blocked SIGINT or SIGTERM")
            child.send_signal(signal.SIGTERM)
            self.assertEqual(child.wait(timeout=10), 7, "the child did not run its SIGTERM handler")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
"""


# Leaves a child in its shard's process group and records its pid in the test directory's parent.
LEAVES_CHILD = """\
import os
import subprocess
import sys
import unittest


class LeaveTest(unittest.TestCase):
    def test_leaves_a_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "left.pid")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(child.pid))
"""

# Runs after the parallel group. The child the earlier module left must already be dead (or a zombie).
LATER_OBSERVES = """\
import os
import time
import unittest


def dead(pid):
    try:
        state = open(f"/proc/{pid}/stat", encoding="utf-8").read().rsplit(") ", 1)[1][0]
    except (OSError, IndexError):
        return True
    return state in "ZX"


class LaterTest(unittest.TestCase):
    def test_the_child_an_earlier_module_left_is_dead(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "left.pid")
        with open(path, encoding="utf-8") as fh:
            pid = int(fh.read())
        end = time.monotonic() + 10
        while not dead(pid) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertTrue(dead(pid), "a child left by an earlier module was still running when a later module ran")
"""


def _write_tree(root, files):
    test_dir = Path(root) / "test"
    test_dir.mkdir()
    for name, text in files.items():
        (test_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (test_dir / name).write_text(text, encoding="utf-8")
    return test_dir


def _alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state not in "ZX"


def _gone(pid, seconds=10):
    """SIGKILL is delivered asynchronously: a killed process may take a moment to leave the table."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _wait_for(path, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.05)
    return False


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_fake(self, test_dir, serial=(), jobs=3, timeout=60, shard_by_class=()):
        out = io.StringIO()
        code = run_suite.run(test_dir, jobs=jobs, serial=serial, timeout=timeout, out=out,
                             shard_by_class=shard_by_class)
        return code, out.getvalue()

    def test_a_passing_failing_erroring_crashing_and_silent_module_fail_the_run(self):
        test_dir = _write_tree(self.tmp.name, FIXTURES)
        code, text = self.run_fake(test_dir)
        self.assertEqual(code, 1, text)
        self.assertRegex(text, r"ok\s+test_pass\.py")
        self.assertRegex(text, r"FAIL\s+test_fail\.py\s+.*exit status 1")
        self.assertRegex(text, r"FAIL\s+test_error\.py\s+.*exit status 1")
        self.assertRegex(text, r"FAIL\s+test_crash\.py\s+.*killed by signal 9")
        self.assertRegex(text, r"FAIL\s+test_silent\.py\s+.*no unittest result line")
        self.assertIn("reproduce: python3 -m unittest discover -s", text)
        self.assertIn("-p test_fail.py", text)
        self.assertIn("boom", text)  # the failing module's own output is printed
        failed_line = [line for line in text.splitlines() if line.startswith("FAILED: ")]
        self.assertEqual(len(failed_line), 1, text)
        for name in ("test_fail.py", "test_error.py", "test_crash.py", "test_silent.py"):
            self.assertIn(name, failed_line[0])
        self.assertNotIn("test_pass.py", failed_line[0])

    def test_the_total_is_the_sum_discovery_reports_for_passing_modules(self):
        files = {"test_one.py": PASS, "test_two.py": PASS.replace("PassTest", "SecondTest")}
        test_dir = _write_tree(self.tmp.name, files)
        code, text = self.run_fake(test_dir, serial=("test_two.py",))
        self.assertEqual(code, 0, text)
        discovered = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(test_dir)],
            cwd=str(test_dir.parent), capture_output=True, text=True, check=False,
        )
        self.assertEqual(discovered.returncode, 0, discovered.stderr)
        self.assertIn("Ran 2 tests", discovered.stderr)
        self.assertIn("Ran 2 tests in", text)
        self.assertIn("OK", text)

    def test_a_serial_name_that_matches_no_module_fails_the_run(self):
        # A renamed module must not silently drop out of the run: the name is refused.
        test_dir = _write_tree(self.tmp.name, {"test_pass.py": PASS})
        code, text = self.run_fake(test_dir, serial=("test_gone.py",))
        self.assertEqual(code, 1, text)
        self.assertIn("SERIAL names no module", text)

    def test_serial_modules_start_only_after_the_parallel_group_has_finished(self):
        marker = "parallel_done"
        files = {
            "test_a_parallel.py": (
                "import time, unittest, pathlib\n"
                "class ParallelTest(unittest.TestCase):\n"
                "    def test_slow(self):\n"
                "        time.sleep(1.5)\n"
                f"        pathlib.Path(__file__).resolve().parent.joinpath('..', '{marker}').write_text('x')\n"
            ),
            "test_z_serial.py": (
                "import pathlib, unittest\n"
                "class SerialTest(unittest.TestCase):\n"
                "    def test_after(self):\n"
                f"        seen = pathlib.Path(__file__).resolve().parent.joinpath('..', '{marker}').exists()\n"
                "        self.assertTrue(seen, 'a serial module started before the parallel group finished')\n"
            ),
        }
        test_dir = _write_tree(self.tmp.name, files)
        code, text = self.run_fake(test_dir, serial=("test_z_serial.py",), jobs=2)
        self.assertEqual(code, 0, text)

    def test_a_module_that_outlives_the_timeout_is_killed_and_fails_the_run(self):
        hang = PASS.replace("def test_passes(self):\n        self.assertEqual(1, 1)",
                            "def test_hangs(self):\n        time.sleep(600)").replace("import unittest", "import time\nimport unittest")
        test_dir = _write_tree(self.tmp.name, {"test_hang.py": hang})
        started = time.monotonic()
        code, text = self.run_fake(test_dir, jobs=1, timeout=2)
        self.assertEqual(code, 1, text)
        self.assertIn("timed out after 2 s", text)
        self.assertLess(time.monotonic() - started, 60)

    def test_a_worker_killed_from_outside_makes_the_run_exit_non_zero(self):
        test_dir = _write_tree(self.tmp.name, {"test_victim.py": VICTIM, "test_pass.py": PASS})
        proc = subprocess.Popen(
            [sys.executable, "-c", DRIVER, str(test_dir)],
            cwd=str(TEST_DIR.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
        pids = Path(self.tmp.name) / "victim.pids"
        try:
            self.assertTrue(_wait_for(pids), "the victim never started")
            victim, _child = (int(x) for x in pids.read_text().split())
            os.kill(victim, signal.SIGKILL)
            out, _ = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertRegex(out, r"FAIL\s+test_victim\.py\s+.*killed by signal 9")

    def test_ctrl_c_kills_every_module_process_group_before_the_runner_exits(self):
        test_dir = _write_tree(self.tmp.name, {"test_victim.py": VICTIM, "test_pass.py": PASS})
        proc = subprocess.Popen(
            [sys.executable, "-c", DRIVER, str(test_dir)],
            cwd=str(TEST_DIR.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        pids = Path(self.tmp.name) / "victim.pids"
        try:
            self.assertTrue(_wait_for(pids), "the victim never started")
            victim, child = (int(x) for x in pids.read_text().split())
            proc.send_signal(signal.SIGINT)
            out, _ = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 130, out)
        self.assertTrue(_gone(victim), "the victim outlived the runner")
        self.assertTrue(_gone(child), "the victim's child outlived the runner")
        # The shards' own group kill did the work: the sweep found nothing left to kill.
        self.assertIn("swept 0 processes", out)

    def test_a_child_a_module_leaves_behind_is_killed_when_the_module_exits(self):
        leaves_child = (
            "import os, subprocess, sys, unittest\n"
            "class LeaveTest(unittest.TestCase):\n"
            "    def test_leaves_a_child(self):\n"
            "        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
            "        open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'left.pid'), 'w').write(str(child.pid))\n"
        )
        test_dir = _write_tree(self.tmp.name, {"test_leaves.py": leaves_child})
        code, text = self.run_fake(test_dir, jobs=1)
        self.assertEqual(code, 0, text)
        left = int((Path(self.tmp.name) / "left.pid").read_text())
        self.assertTrue(_gone(left), "a child left by the module outlived its run")

    def test_a_child_a_module_leaves_is_dead_when_a_later_module_runs(self):
        # The group kill at a module's exit is what ends the child before a later module looks; the sweep runs last.
        test_dir = _write_tree(self.tmp.name, {"test_leaves.py": LEAVES_CHILD, "test_later.py": LATER_OBSERVES})
        code, text = self.run_fake(test_dir, serial=("test_later.py",), jobs=1)
        self.assertEqual(code, 0, text)

    def test_a_shard_dies_with_the_runner_when_the_runner_is_sigkilled(self):
        test_dir = _write_tree(self.tmp.name, {"test_sleeper.py": SLEEPER})
        proc = subprocess.Popen(
            [sys.executable, "-c", DRIVER, str(test_dir)],
            cwd=str(TEST_DIR.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file = Path(self.tmp.name) / "sleeper.pid"
        victim = None
        try:
            self.assertTrue(_wait_for(pid_file), "the sleeper never started")
            victim = int(pid_file.read_text())
            proc.kill()  # SIGKILL: the runner gets no chance to kill its shards
            proc.wait(timeout=30)
            self.assertTrue(_gone(victim, 10), "the shard outlived the SIGKILLed runner")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if victim is not None and _alive(victim):
                try:
                    os.kill(victim, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_without_pdeathsig_the_runner_says_so_on_stderr_and_still_runs(self):
        test_dir = _write_tree(self.tmp.name, {"test_pass.py": PASS})
        err = io.StringIO()
        with mock.patch.object(run_suite, "_PRCTL", None), redirect_stderr(err):
            code, text = self.run_fake(test_dir)
        self.assertEqual(code, 0, text)
        self.assertIn("PR_SET_PDEATHSIG", err.getvalue())

    def test_a_grandchild_in_its_own_session_is_swept_when_the_run_ends(self):
        test_dir = _write_tree(self.tmp.name, {"test_escapes.py": ESCAPES_GROUP})
        code, text = self.run_fake(test_dir, jobs=1)
        escaped = None
        try:
            self.assertEqual(code, 0, text)
            escaped = int((Path(self.tmp.name) / "escaped.pid").read_text())
            self.assertTrue(_gone(escaped, 10), "a grandchild that left its group outlived the run")
            self.assertIn("swept 1 processes", text)
        finally:
            if escaped is not None and _alive(escaped):
                os.kill(escaped, signal.SIGKILL)

    def test_sigterm_mid_run_exits_non_zero_and_leaves_no_test_process_running(self):
        test_dir = _write_tree(self.tmp.name, {"test_waits.py": ESCAPES_THEN_WAITS})
        proc = subprocess.Popen(
            [sys.executable, "-c", DRIVER, str(test_dir)],
            cwd=str(TEST_DIR.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
        pids = Path(self.tmp.name) / "escaped.pid"
        shard = grandchild = None
        try:
            self.assertTrue(_wait_for(pids), "the module never started")
            shard, grandchild = (int(x) for x in pids.read_text().split())
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for pid in (shard, grandchild):
                if pid is not None and _alive(pid):
                    os.kill(pid, signal.SIGKILL)
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("interrupted", out)
        self.assertTrue(_gone(shard), "the shard outlived the SIGTERMed runner")
        self.assertTrue(_gone(grandchild), "a grandchild outlived the SIGTERMed runner")

    def test_a_test_module_in_a_subdirectory_fails_the_run_and_is_named(self):
        # Discovery runs a test*.py under a package directory; the runner lists only the top level, so it refuses.
        test_dir = _write_tree(self.tmp.name, {"test_pass.py": PASS, "nested/__init__.py": "",
                                               "nested/test_hidden.py": PASS})
        code, text = self.run_fake(test_dir)
        self.assertEqual(code, 1, text)
        self.assertIn("nested/test_hidden.py", text)

    def test_a_symlinked_test_package_fails_the_run_and_is_named(self):
        # discovery walks into a package a symlink points to; the runner lists only the top level, so it refuses.
        outside = Path(self.tmp.name) / "outside_pkg"
        outside.mkdir()
        (outside / "__init__.py").write_text("", encoding="utf-8")
        (outside / "test_inner.py").write_text(PASS, encoding="utf-8")
        test_dir = _write_tree(self.tmp.name, {"test_pass.py": PASS})
        (test_dir / "linked_pkg").symlink_to(outside, target_is_directory=True)
        code, text = self.run_fake(test_dir)
        self.assertEqual(code, 1, text)
        self.assertIn("linked_pkg", text)

    def test_a_directory_the_runner_cannot_read_fails_the_run_and_is_named(self):
        # A module hidden in a directory the walk cannot read must not pass unseen.
        test_dir = _write_tree(self.tmp.name, {"test_pass.py": PASS, "locked/test_hidden.py": PASS})
        locked = test_dir / "locked"
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        if os.access(locked, os.R_OK):
            self.skipTest("the directory is readable despite mode 0 (running as root)")
        code, text = self.run_fake(test_dir)
        self.assertEqual(code, 1, text)
        self.assertIn("cannot read", text)
        self.assertIn("locked", text)

    def test_an_interrupt_just_after_a_spawn_still_kills_that_child(self):
        test_dir = _write_tree(self.tmp.name, {"test_sleeper.py": SLEEPER})
        real_popen = subprocess.Popen
        spawned = []

        def popen(argv, *args, **kwargs):
            proc = real_popen(argv, *args, **kwargs)
            if "-c" not in argv:  # the shard, not the module listing
                spawned.append(proc.pid)
                os.kill(os.getpid(), signal.SIGTERM)  # the interrupt lands as the spawn returns
            return proc

        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch.object(run_suite.subprocess, "Popen", side_effect=popen), \
                    redirect_stdout(out), redirect_stderr(err):
                code = run_suite.main(["--dir", str(test_dir), "-j", "1"], serial=(), shard_by_class=())
            self.assertEqual(code, 130, out.getvalue())
            self.assertEqual(len(spawned), 1)
            self.assertTrue(_gone(spawned[0]), "a child spawned as the interrupt arrived outlived the runner")
            # The group kill reached it, so the sweep had nothing to do.
            self.assertIn("swept 0 processes", out.getvalue())
        finally:
            for pid in spawned:
                if _alive(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_a_shard_and_its_child_inherit_no_blocked_sigint_or_sigterm(self):
        # The spawn blocks SIGINT and SIGTERM; the shard and its child must get the runner's own mask back.
        test_dir = _write_tree(self.tmp.name, {"test_masks.py": MASKS})
        code, text = self.run_fake(test_dir, jobs=1)
        self.assertEqual(code, 0, text)
        self.assertIn("Ran 2 tests", text)

    def test_dir_runs_a_fake_directory_with_the_repository_serial_list(self):
        # main() passes the repository's SERIAL and SHARD_BY_CLASS: with --dir, the names the directory lacks drop out.
        test_dir = _write_tree(self.tmp.name, {"test_pass.py": PASS})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run_suite.main(["--dir", str(test_dir), "-j", "2"])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertNotIn("names no module", out.getvalue())
        self.assertIn("test_reviewjob.py", err.getvalue())  # the drop is said, not silent

    def test_bad_worker_counts_are_refused_before_anything_runs(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
            run_suite.main(["-j", "0"], serial=())
        self.assertEqual(refused.exception.code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
            run_suite.main(["--timeout", "0"], serial=())
        self.assertEqual(refused.exception.code, 2)

    def test_the_modules_run_are_exactly_the_modules_discovery_runs(self):
        import unittest as stdlib_unittest

        found = set()

        def walk(suite):
            for item in suite:
                if isinstance(item, stdlib_unittest.TestSuite):
                    walk(item)
                else:
                    found.add(type(item).__module__ + ".py")

        walk(stdlib_unittest.TestLoader().discover(str(TEST_DIR), pattern="test*.py"))
        self.assertEqual(set(run_suite.discover_modules(TEST_DIR)), found)
        self.assertLessEqual(set(run_suite.SERIAL), found)  # every SERIAL name is a discovered module
        self.assertIn("test_run_suite.py", found)
        self.assertNotIn("run_suite.py", run_suite.discover_modules(TEST_DIR))



SHARDED = """\
import os
import unittest

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ran.log")


def record(name):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(name + "\\n")


class OneTest(unittest.TestCase):
    def test_a(self):
        record("OneTest.test_a")

    def test_b(self):
        record("OneTest.test_b")


class TwoTest(unittest.TestCase):
    def test_a(self):
        record("TwoTest.test_a")


class ThreeTest(unittest.TestCase):
    def test_a(self):
        record("ThreeTest.test_a")
"""

KILLED = """\
import os
import signal
import unittest


class FineTest(unittest.TestCase):
    def test_fine(self):
        self.assertTrue(True)


class DiesTest(unittest.TestCase):
    def test_dies(self):
        os.kill(os.getpid(), signal.SIGKILL)
"""

FIXTURE_LEVEL = """\
import unittest


def setUpModule():
    pass


class FixtureTest(unittest.TestCase):
    def test_a(self):
        self.assertTrue(True)


class OtherTest(unittest.TestCase):
    def test_b(self):
        self.assertTrue(True)
"""

SHARDED_IDS = ["test_sharded.OneTest.test_a", "test_sharded.OneTest.test_b",
               "test_sharded.TwoTest.test_a", "test_sharded.ThreeTest.test_a"]


def _discovered_ids():
    import unittest as stdlib_unittest

    ids = []

    def walk(suite):
        for item in suite:
            if isinstance(item, stdlib_unittest.TestSuite):
                walk(item)
            else:
                ids.append(item.id())

    walk(stdlib_unittest.TestLoader().discover(str(TEST_DIR), pattern="test*.py"))
    return ids


_REAL = {}


def _real_plan():
    """The real repository's module ids and shards, listed once for every test in this module."""
    if not _REAL:
        modules = run_suite.discover_modules(TEST_DIR)
        module_ids = run_suite.enumerate_modules(TEST_DIR, modules, 4, 600)
        shards = run_suite.plan_shards(TEST_DIR, module_ids, run_suite.SHARD_BY_CLASS, run_suite.SERIAL)
        _REAL.update(module_ids=module_ids, shards=shards)
    return _REAL["module_ids"], _REAL["shards"]


class PlanTest(unittest.TestCase):
    def test_the_shards_run_exactly_the_test_ids_discovery_finds(self):
        _module_ids, shards = _real_plan()
        planned = Counter(test_id for shard in shards for test_id in shard.ids)
        self.assertEqual(planned, Counter(_discovered_ids()))  # each id once: none dropped, none doubled

    def test_each_sharded_module_is_one_shard_per_class_and_serial_modules_stay_whole(self):
        module_ids, shards = _real_plan()
        for module in run_suite.SHARD_BY_CLASS:
            mine = [s for s in shards if s.module == module]
            self.assertGreater(len(mine), 1, module)
            self.assertTrue(all(s.cls for s in mine), module)
            self.assertEqual(len({s.cls for s in mine}), len(mine), module)
            self.assertEqual(sum(s.expected for s in mine), len(module_ids[module]), module)
        for module in run_suite.SERIAL:
            mine = [s for s in shards if s.module == module]
            self.assertEqual([s.cls for s in mine], [""], module)

    def test_the_serial_and_sharded_sets_do_not_overlap_and_name_real_modules(self):
        modules = set(run_suite.discover_modules(TEST_DIR))
        self.assertFalse(set(run_suite.SERIAL) & set(run_suite.SHARD_BY_CLASS))
        self.assertLessEqual(set(run_suite.SHARD_BY_CLASS), modules)
        self.assertNotIn("test_sandbox.py", run_suite.SERIAL)  # sandbox and buildjob run in the parallel group
        self.assertNotIn("test_buildjob.py", run_suite.SERIAL)

    def test_a_module_with_module_level_fixtures_or_shared_state_is_never_split(self):
        base = "import unittest\n\n\nclass ATest(unittest.TestCase):\n    def test_a(self):\n        pass\n"
        cases = {
            "test_setup.py": base + "\n\ndef setUpModule():\n    pass\n",
            "test_teardown.py": base + "\n\ndef tearDownModule():\n    pass\n",
            "test_loader.py": base + "\n\ndef load_tests(loader, tests, pattern):\n    return tests\n",
            "test_global.py": base + "\n\ndef helper():\n    global STATE\n    STATE = 1\n",
            "test_shared.py": base + "\n\nclass BTest(unittest.TestCase):\n    def test_b(self):\n"
                                    "        ATest.flag = True\n",
        }
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, text in cases.items():
            path = Path(tmp.name) / name
            path.write_text(text, encoding="utf-8")
            self.assertFalse(run_suite.splittable(path), name)
        plain = Path(tmp.name) / "test_plain.py"
        plain.write_text(base + "\n\nclass BTest(unittest.TestCase):\n    def test_b(self):\n        pass\n",
                         encoding="utf-8")
        self.assertTrue(run_suite.splittable(plain))

    def test_a_listed_module_with_a_setUpModule_runs_as_one_shard(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        test_dir = _write_tree(tmp.name, {"test_fixture.py": FIXTURE_LEVEL})
        module_ids = {"test_fixture.py": ("test_fixture.FixtureTest.test_a", "test_fixture.OtherTest.test_b")}
        shards = run_suite.plan_shards(test_dir, module_ids, ("test_fixture.py",), ())
        self.assertEqual([(s.module, s.cls, s.expected) for s in shards], [("test_fixture.py", "", 2)])

    def test_the_longest_shards_start_first(self):
        _module_ids, shards = _real_plan()
        hints = [shard.hint() for shard in shards]
        self.assertEqual(hints, sorted(hints, reverse=True))
        # The slowest module is split into class shards, and a shard's hint is the module's measured
        # seconds times its share of the tests, so which class leads moves when tests are added to
        # other classes. What must hold is that the sandbox's biggest shards are among the first started.
        self.assertIn("test_sandbox.py", [shard.module for shard in shards[:12]])


# A module that reads the clock or the process table by name must be serial or say here why it is safe
# in the parallel group. The grep is the same text search a reader would do, so it also matches a
# comment: a false match needs a reason too.
CLOCK_OR_PROCESS = ("time.monotonic", "time.sleep", "os.kill", "killpg", "/proc")
PARALLEL_ALLOWED = {
    "test_assemble.py": "its only /proc mention is the nolive import comment; no clock, no kill",
    "test_buildjob.py": "sleeps while polling and kills only pids of children it spawned itself; asserts no wall-clock bound",
    "test_factory.py": "its only /proc mention is the nolive import comment; no clock, no kill",
    "test_land.py": "its only /proc mention is the nolive import comment; no clock, no kill",
    "test_levels.py": "its only /proc mention is the nolive import comment; no clock, no kill",
    "test_lines.py": "its only /proc mention is the nolive import comment; no clock, no kill",
    "test_piagent.py": "its only /proc mention is the nolive import comment; no clock, no kill",
    "test_publish.py": "its only /proc mention is the nolive import comment; no clock, no kill",
    "test_run_suite.py": "its kills and /proc reads are on processes it starts itself; its one wall-clock bound is 60 s",
    "test_sandbox.py": "its /proc scan is limited to children of the processes it starts; asserts no wall-clock bound",
    "test_setup.py": "its only /proc mention is the nolive import comment; no clock, no kill",
    "test_workers.py": "its only /proc mention is the nolive import comment; no clock, no kill",
}


class SerialGuardTest(unittest.TestCase):
    def test_a_module_that_reads_the_clock_or_the_process_table_is_serial_or_allowed(self):
        found = {}
        for module in run_suite.discover_modules(TEST_DIR):
            text = (TEST_DIR / module).read_text(encoding="utf-8")
            hits = [pattern for pattern in CLOCK_OR_PROCESS if pattern in text]
            if hits:
                found[module] = hits
        unguarded = sorted(m for m in found if m not in run_suite.SERIAL and m not in PARALLEL_ALLOWED)
        self.assertEqual(unguarded, [], "these modules use " + ", ".join(CLOCK_OR_PROCESS)
                         + " but are neither in SERIAL nor allowed here with a reason: "
                         + ", ".join(f"{m} {found[m]}" for m in unguarded))
        stale = sorted(m for m in PARALLEL_ALLOWED if m not in found)
        self.assertEqual(stale, [], "PARALLEL_ALLOWED names modules that no longer use those patterns")
        self.assertTrue(all(reason.strip() for reason in PARALLEL_ALLOWED.values()))


class ValidationTest(unittest.TestCase):
    def refuse(self, argv, env=None):
        """main() exits 2 with a message on stderr: the same status for a bad flag and a bad variable."""
        err = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), redirect_stderr(err), \
                mock.patch.object(run_suite, "run", return_value=0) as ran:
            with self.assertRaises(SystemExit) as refused:
                run_suite.main(argv, serial=())
        self.assertEqual(refused.exception.code, 2, err.getvalue())
        ran.assert_not_called()
        return err.getvalue()

    def test_a_superscript_digit_in_the_environment_is_refused_not_a_crash(self):
        self.assertIn("PIVOTAL_TEST_JOBS", self.refuse([], {"PIVOTAL_TEST_JOBS": "\u00b2"}))

    def test_a_bad_worker_count_in_the_environment_exits_2(self):
        for raw in ("0", "-1", "two", "1" * 5000):
            with self.subTest(raw=raw[:20]):
                self.assertIn("PIVOTAL_TEST_JOBS", self.refuse([], {"PIVOTAL_TEST_JOBS": raw}))

    def test_a_non_finite_or_bad_timeout_is_refused_with_status_2(self):
        for raw in ("nan", "inf", "-inf", "0", "soon"):
            with self.subTest(raw=raw):
                self.assertIn("PIVOTAL_TEST_TIMEOUT", self.refuse([], {"PIVOTAL_TEST_TIMEOUT": raw}))
        for raw in ("nan", "inf", "-1", "0"):
            with self.subTest(flag=raw):
                self.refuse(["--timeout", raw])

    def test_workers_are_capped_at_four_times_the_cpus_and_the_cap_is_said(self):
        err = io.StringIO()
        with mock.patch.object(run_suite.os, "cpu_count", return_value=2), redirect_stderr(err), \
                mock.patch.object(run_suite, "run", return_value=0) as ran:
            self.assertEqual(run_suite.main(["-j", "100"], serial=()), 0)
        self.assertEqual(ran.call_args.kwargs["jobs"], 8)
        self.assertIn("capped at 8", err.getvalue())

    def test_a_worker_count_under_the_cap_is_not_changed(self):
        err = io.StringIO()
        with mock.patch.object(run_suite.os, "cpu_count", return_value=2), redirect_stderr(err), \
                mock.patch.object(run_suite, "run", return_value=0) as ran:
            run_suite.main(["-j", "5"], serial=())
        self.assertEqual(ran.call_args.kwargs["jobs"], 5)
        self.assertEqual(err.getvalue(), "")


class WorkerTest(unittest.TestCase):
    def test_workers_default_to_the_cpu_count_capped_at_eight(self):
        with mock.patch.object(run_suite.os, "cpu_count", return_value=16):
            self.assertEqual(run_suite.default_jobs(), 8)
        with mock.patch.object(run_suite.os, "cpu_count", return_value=3):
            self.assertEqual(run_suite.default_jobs(), 3)
        with mock.patch.object(run_suite.os, "cpu_count", return_value=None):
            self.assertEqual(run_suite.default_jobs(), 1)

    def test_the_environment_sets_workers_and_the_flag_overrides_it(self):
        with mock.patch.dict(os.environ, {"PIVOTAL_TEST_JOBS": "3"}), \
                mock.patch.object(run_suite, "run", return_value=0) as ran:
            self.assertEqual(run_suite.main([], serial=()), 0)
            self.assertEqual(ran.call_args.kwargs["jobs"], 3)
            run_suite.main(["-j", "2"], serial=())
            self.assertEqual(ran.call_args.kwargs["jobs"], 2)


class JudgeTest(unittest.TestCase):
    SHARD = run_suite.Shard("test_x.py", "XTest", ("test_x.XTest.test_a", "test_x.XTest.test_b"), 5)

    def judge(self, text, rc=0, timed_out=False):
        run = run_suite.Run("test_x.py::XTest", rc, timed_out, 1.5, text)
        return run_suite.judge(self.SHARD, run, 60, TEST_DIR)

    def test_a_shard_that_runs_its_planned_tests_passes(self):
        outcome = self.judge("Ran 2 tests in 0.1s\n\nOK\n")
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.tests, 2)

    def test_a_shard_that_runs_another_number_of_tests_fails(self):
        outcome = self.judge("Ran 1 test in 0.1s\n\nOK\n")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "ran 1 tests, planned 2")

    def test_a_shard_killed_by_a_signal_fails_and_says_which(self):
        outcome = self.judge("", rc=-9)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "killed by signal 9")

    def test_a_timed_out_nonzero_or_silent_shard_fails(self):
        self.assertIn("timed out", self.judge("", rc=None, timed_out=True).reason)
        self.assertEqual(self.judge("Ran 2 tests in 0s\n\nFAILED\n", rc=1).reason, "exit status 1")
        self.assertIn("no unittest result line", self.judge("", rc=0).reason)

    def test_the_outcome_carries_the_command_that_reproduces_the_shard(self):
        outcome = self.judge("Ran 2 tests in 0s\n\nOK\n")
        self.assertEqual(outcome.reproduce, "PYTHONPATH=test python3 -m unittest test_x.XTest")


class ShardRunTest(unittest.TestCase):
    setUp = RunnerTest.setUp  # the same fresh temporary directory per test
    run_fake = RunnerTest.run_fake

    def test_each_class_of_a_sharded_module_runs_exactly_once(self):
        test_dir = _write_tree(self.tmp.name, {"test_sharded.py": SHARDED})
        code, text = self.run_fake(test_dir, shard_by_class=("test_sharded.py",))
        self.assertEqual(code, 0, text)
        log = (test_dir.parent / "ran.log").read_text(encoding="utf-8").split()
        self.assertEqual(sorted(log), sorted(["OneTest.test_a", "OneTest.test_b", "TwoTest.test_a",
                                              "ThreeTest.test_a"]))
        for cls in ("OneTest", "TwoTest", "ThreeTest"):
            self.assertIn(f"test_sharded.py::{cls}", text)
        self.assertIn("planned 4 tests", text)

    def test_a_class_shard_killed_mid_run_fails_the_run_and_names_its_command(self):
        test_dir = _write_tree(self.tmp.name, {"test_killed.py": KILLED})
        code, text = self.run_fake(test_dir, shard_by_class=("test_killed.py",))
        self.assertEqual(code, 1, text)
        self.assertIn("ok   test_killed.py::FineTest", text)
        self.assertIn("test_killed.py::DiesTest: killed by signal 9", text)
        self.assertIn("python3 -m unittest test_killed.DiesTest", text)

    def test_a_shard_that_never_reports_fails_the_run(self):
        test_dir = _write_tree(self.tmp.name, {"test_sharded.py": SHARDED})
        real = run_suite._run_pool

        def drop_one_class(tasks, jobs, timeout, emit=None):
            kept = [task for task in tasks if task[0] != "test_sharded.py::TwoTest"]
            return real(kept, jobs, timeout, emit=emit)

        with mock.patch.object(run_suite, "_run_pool", drop_one_class):
            code, text = self.run_fake(test_dir, shard_by_class=("test_sharded.py",))
        self.assertEqual(code, 1, text)
        self.assertIn("test_sharded.py::TwoTest", text)
        self.assertIn("did not run", text)

    def test_a_module_with_a_setUpModule_is_listed_as_one_whole_shard(self):
        test_dir = _write_tree(self.tmp.name, {"test_fixture.py": FIXTURE_LEVEL})
        code, text = self.run_fake(test_dir, shard_by_class=("test_fixture.py",))
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"(?m)^ok   test_fixture\.py ")
        self.assertNotIn("test_fixture.py::", text)


if __name__ == "__main__":
    unittest.main()
