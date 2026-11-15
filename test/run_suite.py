#!/usr/bin/env python3
"""Run the unit suite in parallel: stdlib only, shards of the modules discovery finds.

`python3 test/run_suite.py` is the check (`.claude/sysop.json`). Every module that
`python3 -m unittest discover -s test` runs is listed, or the run refuses before it starts (see
`unrunnable`: a symlink to a directory under test/, a test module in a subdirectory, a directory it
cannot read; a refusal runs nothing). Each listed module's TestCase classes are read in a child
process, and each shard runs in its own child process:

- a whole module (`python3 -m unittest discover -s test -p <module>.py`), or
- for a module in SHARD_BY_CLASS, one TestCase class (`PYTHONPATH=test python3 -m unittest
  <module>.<Class>`), so a slow module is spread over several workers. A module that defines
  setUpModule/tearDownModule, a load_tests hook, a `global` statement or assigns to another
  class of its own is never split: it runs as one shard.

Shards start longest first (RUNTIME_HINT, then the share of the module's tests), up to `jobs` at
once. The modules in SERIAL run alone afterwards, each with its reason. Every shard must report
exactly the tests it was planned with, and the planned tests are exactly discovery's, so a test is
never dropped or run twice. A shard fails when it fails, errors, is killed, times out, prints no
result or runs another number of tests. A module whose tests cannot be listed fails as a shard of
its own, named after the module with the reason; the other modules still run and are reported. A
shard's process group is killed when the shard exits, when it times out and on Ctrl-C or SIGTERM.
Every shard is armed with PR_SET_PDEATHSIG (Linux), so it dies with the runner even when the runner is
SIGKILLed. What a shard started does not die with it. After the run, every process still carrying this
run's PIVOTAL_RUN_TAG in its environment is SIGKILLed and counted (`swept N`). Not covered:
- a SIGKILLed runner: its shards die, but what they started (in their group or a session of their own)
  survives, since nothing sweeps after a SIGKILL. sysop's check timeout SIGKILLs the runner's process
  group, which reaches the runner and, through PDEATHSIG, its shards, but not what they started;
- a process that clears its environment: the sweep cannot find it. In its shard's group it is killed
  when its module exits; in a session of its own it survives the run, and the sweep reports `swept 0`.

Environment: PIVOTAL_TEST_JOBS (default: the CPU count, at most 8), PIVOTAL_TEST_TIMEOUT (seconds
per shard, 1500). Flags -j and --timeout override them. -j and PIVOTAL_TEST_JOBS are capped at 4 times
the CPU count, and the cap is printed on stderr. A timeout must be a finite number above zero.
--dir runs another directory (fixtures): the SERIAL and SHARD_BY_CLASS names it lacks are
dropped, and the drop is printed on stderr. The default test/ refuses such a name instead.
Exit status: 0 when every shard ran and passed, 1 otherwise, 2 for a bad flag or variable, 130 when
interrupted by Ctrl-C or SIGTERM (every test process group is killed and swept first).
"""

import argparse
import ast
import ctypes
import fnmatch
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = ROOT / "test"
MAX_JOBS = 8
JOBS_PER_CPU_CAP = 4  # -j and PIVOTAL_TEST_JOBS are capped at this many times the CPU count
DEFAULT_TIMEOUT = 1500
POLL_SECONDS = 0.02

# Modules that run alone, after the parallel group, because another module's processes or
# clock readings would change their answer. Each is named by what it does. They are never split.
SERIAL = (
    "test_reviewjob.py",  # scans /proc for reviewjob.py processes, reads their environ, races kills
    "test_runner.py",  # scans /proc for tagged processes, SIGKILLs stragglers, waits on pids
    "test_run.py",  # reads /proc/<pid>/stat, SIGKILLs by pid, asserts a call returns in under 20 s
    "test_gatejob.py",  # alarm-bounded waits that probe pids with kill(pid, 0)
    "test_modelsup.py",  # asserts on monotonic elapsed time around deadlines and sleeps
    "test_report.py",  # asserts a call returns in under 5 s
    "test_trust.py",  # asserts a call returns in under 4 s
    "test_fakehub.py",  # polls a fake gh server against a 30 s monotonic deadline
    "test_hill.py",  # asserts a call returns in under 1 s of monotonic time
    "test_presets.py",  # polls fake CLIs against monotonic deadlines; probes pids with kill(pid, 0) and /proc
    "test_score_image.py",  # kills and signals the scorer's app and browser, reads /proc, waits on monotonic deadlines
)

# Slow modules, split into one shard per TestCase class. Each is a module whose class shards
# need no shared state: see splittable(). test_sandbox and test_buildjob are here because their
# /proc use is pinned to their own children, not to the whole user.
SHARD_BY_CLASS = frozenset({
    "test_sandbox.py",
    "test_buildjob.py",
    "test_assemble.py",
    "test_land.py",
    "test_publish.py",
    "test_stack.py",
    "test_levels.py",
})

# Seconds one module took in a measured run (sum of its shards), used to start the longest first.
# A module not listed is assumed to take DEFAULT_HINT_SECONDS.
DEFAULT_HINT_SECONDS = 5.0
RUNTIME_HINT = {
    "test_sandbox.py": 783.0,
    "test_buildjob.py": 266.0,
    "test_reviewjob.py": 103.0,
}

RESULT = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
IDS = re.compile(r"^IDS (.*)$", re.MULTILINE)

# Runs in a child: loads one module exactly as discovery does and prints its test ids.
ENUMERATE = (
    "import json, sys, unittest\n"
    "test_dir, name = sys.argv[1], sys.argv[2]\n"
    "ids = []\n"
    "def walk(suite):\n"
    "    for item in suite:\n"
    "        if isinstance(item, unittest.TestSuite):\n"
    "            walk(item)\n"
    "        else:\n"
    "            ids.append(item.id())\n"
    "walk(unittest.defaultTestLoader.discover(start_dir=test_dir, pattern=name))\n"
    "print('IDS ' + json.dumps(ids))\n"
)


class PlanError(Exception):
    """The runner cannot list the tests of a module, so it cannot plan the run."""


@dataclass(frozen=True)
class Shard:
    module: str  # test_x.py
    cls: str  # the TestCase class, or "" for the whole module
    ids: tuple  # the test ids the shard must run
    module_total: int  # the number of tests in the whole module

    @property
    def expected(self):
        return len(self.ids)

    @property
    def label(self):
        return self.module if not self.cls else f"{self.module}::{self.cls}"

    @property
    def stem(self):
        return self.module[: -len(".py")]

    def argv(self, python, test_dir):
        if not self.cls:
            return [python, "-m", "unittest", "discover", "-s", str(test_dir), "-p", self.module]
        return [python, "-m", "unittest", f"{self.stem}.{self.cls}"]

    def reproduce(self, test_dir):
        rel = _relative(test_dir)
        if not self.cls:
            return f"python3 -m unittest discover -s {rel} -p {self.module}"
        return f"PYTHONPATH={rel} python3 -m unittest {self.stem}.{self.cls}"

    def hint(self):
        share = self.expected / self.module_total if self.module_total else 1.0
        return RUNTIME_HINT.get(self.module, DEFAULT_HINT_SECONDS) * share


@dataclass
class Run:
    key: str
    returncode: object  # int, or None when the child was never reaped
    timed_out: bool
    seconds: float
    text: str


@dataclass
class Outcome:
    label: str
    ok: bool
    tests: int
    seconds: float
    reason: str
    output: str
    reproduce: str


def discover_modules(test_dir):
    """The modules discovery imports: test*.py directly under test_dir (its default pattern)."""
    return sorted(p.name for p in Path(test_dir).glob("test*.py") if p.is_file())


def default_jobs():
    """One worker per CPU, at most MAX_JOBS."""
    return min(MAX_JOBS, os.cpu_count() or 1)


def splittable(path):
    """True when the module's TestCase classes share no state, so each can run in its own process."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
            "setUpModule", "tearDownModule", "load_tests",
        ):
            return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            return False
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id in classes):
                    return False
    return True


def unrunnable(test_dir):
    """What discovery may run under test_dir that the runner does not list: (stray, linked, unreadable).

    stray: test*.py files below the top level, which discovery imports and the runner does not run.
    linked: symlinks to directories, which discovery walks into as packages. os.walk does not follow
    them, so a symlink loop cannot stall the walk, and each is named instead of entered. unreadable:
    paths the walk could not read, so a module hidden there is not passed unseen.
    """
    stray, linked, unreadable = [], [], []

    def cannot_read(err):
        unreadable.append(str(err.filename or test_dir))

    for root, dirs, files in os.walk(test_dir, onerror=cannot_read):
        here = Path(root)
        for name in dirs:
            if (here / name).is_symlink():
                linked.append((here / name).relative_to(test_dir).as_posix())
        if here != test_dir:
            stray += [(here / name).relative_to(test_dir).as_posix() for name in files
                      if fnmatch.fnmatch(name, "test*.py") and (here / name).is_file()]
    return sorted(stray), sorted(linked), sorted(unreadable)


def _relative(path):
    """The path relative to the repository root, for the commands a failure prints."""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


PR_SET_PDEATHSIG = 1


def _load_prctl():
    """prctl(2) from the C library, or None where the platform has no PR_SET_PDEATHSIG."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None


_PRCTL = _load_prctl()


def _child_preexec(parent, mask):
    """A preexec_fn for a child: it gets back the signal mask the runner had, and dies with the runner.

    The runner blocks SIGINT and SIGTERM around its spawn, and a fork inherits the block, so the child
    first restores `mask` (the mask from before the block): a shard and what it starts can receive those
    signals. Then, where prctl exists, PR_SET_PDEATHSIG is armed before the exec. If the runner (parent)
    died first, the child has been re-parented already (getppid differs) and exits at once.
    """
    def arm():
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        if _PRCTL is not None:
            _PRCTL(PR_SET_PDEATHSIG, int(signal.SIGKILL))
        if os.getppid() != parent:
            os._exit(1)

    return arm


def _spawn(argv, env, out, mask):
    """Start one child in its own session, armed to die with the runner.

    `mask` is the signal mask the runner had before it blocked SIGINT and SIGTERM for this spawn; the
    child gets it back. The caller holds the block across this call and records the child in the same
    window, so a signal cannot land between Popen returning and the child being recorded.
    """
    return subprocess.Popen(
        argv,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
        preexec_fn=_child_preexec(os.getpid(), mask),
    )


def _kill_group(proc):
    """SIGKILL the child's process group (it leads one: start_new_session) and anything left in it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _child_env(test_dir, tag=""):
    path = [str(test_dir)] + [p for p in [os.environ.get("PYTHONPATH", "")] if p]
    return dict(os.environ, PYTHONUNBUFFERED="1", PYTHONPATH=os.pathsep.join(path), PIVOTAL_RUN_TAG=tag)


SWEEP_PASSES = 5


def sweep(tag):
    """SIGKILL every process that carries this run's tag in its environment; return how many.

    Every child of a run inherits PIVOTAL_RUN_TAG, so a process a module started and left running is
    found whatever became of its parent: a grandchild in its own session, or one re-parented to init
    when its shard exited. Only the tag identifies it: a re-parented process has lost its parent link,
    and the tag is unique to this run, so a reused pid is never mistaken for one. A child that clears
    its environment is not found.
    """
    if not tag:
        return 0
    needle = f"PIVOTAL_RUN_TAG={tag}".encode()
    swept = 0
    for _ in range(SWEEP_PASSES):  # a killed process may have started another before it died
        found = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == os.getpid():
                continue
            try:
                environ = Path("/proc", entry, "environ").read_bytes()
            except OSError:  # gone, or not ours to read
                continue
            if needle in environ.split(b"\0"):
                found.append(int(entry))
        if not found:
            break
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
                swept += 1
            except OSError:  # gone already, or not ours
                pass
        time.sleep(0.05)
    return swept


def _run_pool(tasks, jobs, timeout, emit=None):
    """Run tasks, at most jobs at once. Each task is (key, argv, env). Returns one Run per task.

    Every child's process group is killed before this returns, on every exit path.
    """
    pending = deque(tasks)
    running = []  # (key, proc, out, started)
    done = []
    try:
        while pending or running:
            while pending and len(running) < jobs:
                key, argv, env = pending.popleft()
                out = tempfile.TemporaryFile()
                before = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
                try:
                    proc = _spawn(argv, env, out, before)
                    running.append((key, proc, out, time.monotonic()))
                except BaseException:
                    out.close()
                    raise
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, before)
            time.sleep(POLL_SECONDS)
            for entry in list(running):
                key, proc, out, started = entry
                timed_out = False
                if proc.poll() is None:
                    if time.monotonic() - started < timeout:
                        continue
                    timed_out = True
                    _kill_group(proc)
                    proc.wait()
                running.remove(entry)
                _kill_group(proc)  # a test may leave children; none outlive their module
                seconds = time.monotonic() - started
                out.seek(0)
                text = out.read().decode("utf-8", "replace")
                out.close()
                run = Run(key, proc.returncode, timed_out, seconds, text)
                done.append(run)
                if emit is not None:
                    emit(run)
    finally:
        for _key, proc, out, _started in running:
            _kill_group(proc)
            proc.wait()
            out.close()
    return done


def _read_listing(run, timeout):
    """(test ids, "") for a module whose list was read, or ((), reason) when it cannot be listed."""
    if run.timed_out:
        return (), f"cannot list its tests: timed out after {timeout:g} s"
    if run.returncode is not None and run.returncode < 0:
        return (), f"cannot list its tests: killed by signal {-run.returncode}"
    if run.returncode != 0:
        return (), f"cannot list its tests: exit status {run.returncode}"
    found = IDS.findall(run.text)
    if not found:
        return (), "cannot list its tests (exit 0): no unittest result line"
    try:
        ids = tuple(json.loads(found[-1]))
    except ValueError:
        return (), "cannot list its tests: the test list is malformed"
    if not ids:
        return (), "cannot list its tests: discovery finds no tests in it"
    return ids, ""


def list_modules(test_dir, modules, jobs, timeout, tag=""):
    """Read each module's test ids: (ids by module, failed outcome by module).

    A module that cannot be listed is a failed outcome named after it, with its reproduction command
    and the output it printed. The other modules are still listed and run.
    """
    env = _child_env(test_dir, tag)
    tasks = [(m, [sys.executable, "-c", ENUMERATE, str(test_dir), m], env) for m in modules]
    ids, refused = {}, {}
    for run in _run_pool(tasks, jobs, timeout):
        listed, reason = _read_listing(run, timeout)
        if reason:
            reproduce = Shard(run.key, "", (), 0).reproduce(test_dir)
            refused[run.key] = Outcome(run.key, False, 0, run.seconds, reason, run.text, reproduce)
        else:
            ids[run.key] = listed
    return ids, refused


def enumerate_modules(test_dir, modules, jobs, timeout):
    """Map each module to the test ids discovery finds in it. Raises PlanError when one cannot be listed."""
    ids, refused = list_modules(test_dir, modules, jobs, timeout)
    if refused:
        raise PlanError("; ".join(f"{o.label}: {o.reason}" for o in refused.values()))
    return {m: ids[m] for m in modules}


def plan_shards(test_dir, module_ids, shard_by_class=SHARD_BY_CLASS, serial=SERIAL):
    """The shards: one per module, or one per class for a splittable module in shard_by_class."""
    shards = []
    for module, ids in module_ids.items():
        stem = module[: -len(".py")]
        classes = {}
        splits = module in shard_by_class and module not in serial and splittable(Path(test_dir) / module)
        for test_id in ids:
            parts = test_id.split(".")
            if splits and len(parts) == 3 and parts[0] == stem:
                classes.setdefault(parts[1], []).append(test_id)
            else:
                splits = False
                break
        if splits:
            for cls, cls_ids in classes.items():
                shards.append(Shard(module, cls, tuple(cls_ids), len(ids)))
        else:
            shards.append(Shard(module, "", tuple(ids), len(ids)))
    return sorted(shards, key=lambda s: (-s.hint(), s.label))


def judge(shard, run, timeout, test_dir):
    """The outcome of one shard run: fails on any exit other than a clean run of its planned tests."""
    found = RESULT.findall(run.text)
    tests = int(found[-1]) if found else 0
    rc = run.returncode
    if run.timed_out:
        reason = f"timed out after {timeout:g} s"
    elif rc is not None and rc < 0:
        reason = f"killed by signal {-rc}"
    elif rc != 0:
        reason = f"exit status {rc}"
    elif not found:
        reason = "no unittest result line: the shard did not run its tests"
    elif tests != shard.expected:
        reason = f"ran {tests} tests, planned {shard.expected}"
    else:
        reason = ""
    return Outcome(shard.label, reason == "", tests, run.seconds, reason, run.text, shard.reproduce(test_dir))


def _show(outcome, say):
    status = "ok" if outcome.ok else "FAIL"
    line = f"{status:<4} {outcome.label:<44} {outcome.tests:>5} tests {outcome.seconds:8.1f}s"
    if not outcome.ok:
        line += f"  {outcome.reason}"
    say(line)
    if outcome.ok:
        return
    say(f"\n=== {outcome.label}: {outcome.reason}")
    say(outcome.output.rstrip("\n"))
    say(f"=== reproduce: {outcome.reproduce}\n")


def run(test_dir=TEST_DIR, jobs=None, serial=SERIAL, shard_by_class=(),
        timeout=DEFAULT_TIMEOUT, out=None):
    """Run every module discovery finds, sharded, unless it refuses (see unrunnable).

    Return 0 when every shard ran and passed, else 1.

    However the run ends, the processes it started and left running are swept at the end.
    """
    out = out if out is not None else sys.stdout
    jobs = default_jobs() if jobs is None else jobs
    if _PRCTL is None:
        print("run_suite: PR_SET_PDEATHSIG is unavailable on this platform: a shard outlives the runner "
              "if the runner is SIGKILLed", file=sys.stderr, flush=True)
    test_dir = Path(test_dir).resolve()

    def say(text):
        print(text, file=out, flush=True)

    tag = uuid.uuid4().hex
    try:
        return _execute(test_dir, jobs, serial, shard_by_class, timeout, tag, say)
    finally:
        say(f"swept {sweep(tag)} processes the run left running")


def _execute(test_dir, jobs, serial, shard_by_class, timeout, tag, say):
    modules = discover_modules(test_dir)
    if not modules:
        say(f"FAIL no test modules under {test_dir}")
        return 1
    stray, linked, unreadable = unrunnable(test_dir)
    if unreadable:  # fail closed: a path the walk could not read may hide a module
        say(f"FAIL cannot read {', '.join(unreadable)} under {test_dir}")
        return 1
    if linked:  # discovery walks into a symlinked package; the runner lists only the top level, so it refuses
        say(f"FAIL {', '.join(linked)}: symlinks to directories under {test_dir}, which the runner does not run")
        return 1
    if stray:  # discovery may import these; the runner lists only the top level, so it refuses
        say(f"FAIL {', '.join(stray)}: test modules in a subdirectory of {test_dir}, which the runner does not run")
        return 1
    serial_set = set(serial)
    unknown = sorted(serial_set - set(modules))
    if unknown:
        say(f"FAIL SERIAL names no module under {test_dir}: {', '.join(unknown)}")
        return 1
    unknown = sorted(set(shard_by_class) - set(modules))
    if unknown:
        say(f"FAIL SHARD_BY_CLASS names no module under {test_dir}: {', '.join(unknown)}")
        return 1

    started = time.monotonic()
    module_ids, refused = list_modules(test_dir, modules, jobs, timeout, tag)
    shards = plan_shards(test_dir, module_ids, shard_by_class, serial_set)
    planned = sum(s.expected for s in shards)
    parallel = [s for s in shards if s.module not in serial_set]
    alone = [s for s in shards if s.module in serial_set]
    say(f"planned {planned} tests in {len(module_ids)} modules as {len(shards)} shards: "
        f"{len(parallel)} with {jobs} workers, {len(alone)} alone"
        + (f"; {len(refused)} cannot be listed" if refused else ""))

    by_key = {}

    def finish(run_result):
        shard = by_key[run_result.key]
        outcome = judge(shard, run_result, timeout, test_dir)
        _show(outcome, say)
        results.append(outcome)

    results = []
    for outcome in refused.values():  # fail closed: reported as a failed shard, the others still run
        _show(outcome, say)
        results.append(outcome)
    for group, workers in ((parallel, jobs), (alone, 1)):
        tasks = []
        for shard in group:
            by_key[shard.label] = shard
            tasks.append((shard.label, shard.argv(sys.executable, test_dir), _child_env(test_dir, tag)))
        _run_pool(tasks, workers, timeout, emit=finish)

    ran = {o.label for o in results}
    for shard in shards:
        if shard.label not in ran:  # fail closed: a shard that never reported is a failure
            outcome = Outcome(shard.label, False, 0, 0.0, "did not run", "", shard.reproduce(test_dir))
            _show(outcome, say)
            results.append(outcome)
    failed = [o for o in results if not o.ok]
    tests = sum(o.tests for o in results if o.ok)
    wall = time.monotonic() - started
    say(f"\nRan {tests} tests in {wall:.1f}s across {len(modules)} modules, {len(shards)} shards "
        f"({sum(1 for s in shards if s.cls)} by class), planned {planned}")
    if failed:
        say("FAILED: " + ", ".join(o.label for o in failed))
        return 1
    say("OK")
    return 0


def _count(name, raw, parser):
    """A worker count from the environment: ASCII digits, at least 1. Anything else is a usage error."""
    if not (raw.isascii() and raw.isdigit()):
        parser.error(f"{name} must be a positive integer, not {raw!r}")
    try:
        value = int(raw)
    except ValueError:  # more digits than int() reads
        parser.error(f"{name} must be a positive integer, not {raw[:20]!r}...")
    if value < 1:
        parser.error(f"{name} must be a positive integer, not {raw!r}")
    return value


def _seconds(label, value, parser):
    """A timeout in seconds: a finite number above zero. nan, inf and words are usage errors."""
    try:
        seconds = float(value)
    except ValueError:
        parser.error(f"{label} must be a number of seconds, not {value!r}")
    if not math.isfinite(seconds) or seconds <= 0:
        parser.error(f"{label} must be a positive, finite number of seconds, not {value!r}")
    return seconds


def main(argv=None, serial=SERIAL, shard_by_class=SHARD_BY_CLASS):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "-j", "--jobs", type=int, default=None,
        help="shards run at once (default: $PIVOTAL_TEST_JOBS, else the CPU count, at most 8)",
    )
    parser.add_argument(
        "--timeout", type=float, default=None,
        help=f"seconds one shard may run (default: $PIVOTAL_TEST_TIMEOUT or {DEFAULT_TIMEOUT})",
    )
    parser.add_argument("--dir", default=str(TEST_DIR), help="test directory (default: test/)")
    args = parser.parse_args(argv)
    if Path(args.dir).resolve() != TEST_DIR.resolve():
        # A fake or other directory: the repository's names it lacks are dropped (and said). The default
        # directory keeps run()'s refusal of a name that matches no module.
        dropped = sorted(n for n in set(serial) | set(shard_by_class) if not (Path(args.dir) / n).is_file())
        if dropped:
            print(f"run_suite: --dir {args.dir} has no {', '.join(dropped)}: dropped from SERIAL and "
                  "SHARD_BY_CLASS", file=sys.stderr, flush=True)
        serial = tuple(n for n in serial if n not in dropped)
        shard_by_class = tuple(n for n in shard_by_class if n not in dropped)
    if args.jobs is None and os.environ.get("PIVOTAL_TEST_JOBS") is not None:
        args.jobs = _count("PIVOTAL_TEST_JOBS", os.environ["PIVOTAL_TEST_JOBS"], parser)
    if args.jobs is not None:
        if args.jobs < 1:
            parser.error("--jobs must be at least 1")
        cap = JOBS_PER_CPU_CAP * (os.cpu_count() or 1)
        if args.jobs > cap:
            print(f"run_suite: {args.jobs} workers capped at {cap} ({JOBS_PER_CPU_CAP} times the CPU count)",
                  file=sys.stderr, flush=True)
            args.jobs = cap
    if args.timeout is None and os.environ.get("PIVOTAL_TEST_TIMEOUT") is not None:
        args.timeout = _seconds("PIVOTAL_TEST_TIMEOUT", os.environ["PIVOTAL_TEST_TIMEOUT"], parser)
    elif args.timeout is not None:
        args.timeout = _seconds("--timeout", args.timeout, parser)
    if args.timeout is None:
        args.timeout = DEFAULT_TIMEOUT

    def _terminate(_signum, _frame):
        raise KeyboardInterrupt  # unwinds through _run_pool, which kills every child group

    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        return run(args.dir, jobs=args.jobs, serial=serial, shard_by_class=shard_by_class,
                   timeout=args.timeout)
    except KeyboardInterrupt:
        print("interrupted: the test processes were killed and swept", file=sys.stderr, flush=True)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
