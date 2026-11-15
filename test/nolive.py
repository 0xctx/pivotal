"""No test may run the user-scope stray sweep against the live /proc: it SIGKILLs every process of the tester's user that
started after the engine (other test suites, the developer's shell). Importing this module makes ``Guard.sweep`` refuse
that combination; a test that wants the user scope points ``buildjob.PROC`` at a fake root (test_buildjob's fake_proc)."""

from pathlib import Path

from engine import buildjob

_sweep = buildjob.Guard.sweep


def sweep(self):
    if self.scope == "user" and Path(self.proc).resolve() == Path("/proc"):
        raise AssertionError("a test reached the user-scope sweep against the live /proc")
    return _sweep(self)


if buildjob.Guard.sweep is _sweep:
    buildjob.Guard.sweep = sweep
