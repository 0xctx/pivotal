"""The worker CLIs a job runs, as engine/setup/install.py installed them (F3).

The install is made by a step that holds no secret, into RUNNER_TEMP, which the agent's own user can write. The entries
of one job run with different door secrets, so a CLI that one entry's agent changed would receive the next entry's key.
So a job reads the manifest once, before any agent starts (its path is the ``PIVOTAL_WORKERS`` variable), keeps the
digests in memory, and checks a worker's tree again before every agent call. A change is a refusal of the job
(``engine_modified``), never a fall-through to the next entry.

A pinned worker runs by its manifest's absolute ``bin`` (``command``), never by its bare name, so what the agent's user
plants in a directory on PATH decides nothing that the engine runs for it, and a same-named CLI earlier on PATH is not a
refusal. The setup step puts no directory on PATH (engine/setup/install.py). What this does not cover is listed in
docs/engines.md: the install's dependencies, the node that pi's ``#!/usr/bin/env node`` finds on the call's PATH, and HOME.

Without the manifest, a job trusts its PATH as it always did: a runner that brings its own CLIs is its operator's to
trust. A worker with no pin is "not installed" when a manifest is loaded.
"""

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from engine.job import JobError

MANIFEST_ENV = "PIVOTAL_WORKERS"
SCHEMA = 1
MANIFEST_MAX = 64 * 1024
NOT_INSTALLED = "not installed"
MODIFIED = "engine_modified"
WORKER = re.compile(r"[a-z]+")
HEX64 = re.compile(r"[0-9a-f]{64}")
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Pin:
    root: str  # the install's directory (the npm prefix), absolute
    bin: str  # the real path of the CLI's binary, inside root
    digest: str  # digest(root) when the install was made


def digest(root: Path) -> str:
    """A sha256 over every entry under root, sorted: its relative path, kind, mode and content (a file's own sha256)
    or link text. A symlink is recorded by its text and never followed; a socket, FIFO or device is a refusal."""
    outer = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted([*dirnames, *filenames]):
            path = Path(dirpath, name)
            rel = path.relative_to(root).as_posix()
            info = os.lstat(path)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                body = b"l:" + os.readlink(path).encode("utf-8", "surrogateescape")
            elif stat.S_ISDIR(info.st_mode):
                body = b"d:"
            elif stat.S_ISREG(info.st_mode):
                inner = hashlib.sha256()
                with open(path, "rb") as fh:
                    for block in iter(lambda: fh.read(CHUNK), b""):
                        inner.update(block)
                body = b"f:" + inner.hexdigest().encode()
            else:
                raise JobError(f"a worker's install holds an entry that is not a file, a link or a directory: {rel}")
            outer.update(f"{rel}\0{mode:o}\0".encode("utf-8", "surrogateescape") + body + b"\n")
    return outer.hexdigest()


def load(path: str) -> dict[str, Pin] | None:
    """The pins of the manifest at ``path`` (the job's PIVOTAL_WORKERS), or None when it is empty. A manifest that cannot be
    read, or does not have exactly the shape install.py writes, refuses the job (JobError): it is never half trusted."""
    if not path:
        return None
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MANIFEST_MAX:
            raise JobError(f"{MANIFEST_ENV} is not a regular file of a sane size")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise JobError(f"{MANIFEST_ENV} cannot be read") from None
    if not isinstance(data, dict) or set(data) != {"schema", "workers"} or data["schema"] != SCHEMA \
            or not isinstance(data["workers"], dict):
        raise JobError(f"{MANIFEST_ENV} has the wrong shape")
    pins: dict[str, Pin] = {}
    for name, item in data["workers"].items():
        if not isinstance(name, str) or not WORKER.fullmatch(name) or not isinstance(item, dict) \
                or set(item) != {"root", "bin", "digest"}:
            raise JobError(f"{MANIFEST_ENV} has a bad worker entry")
        root, bin_, sha = item["root"], item["bin"], item["digest"]
        if not (isinstance(root, str) and isinstance(bin_, str) and root.startswith("/") and bin_.startswith(root + "/")) \
                or not (isinstance(sha, str) and HEX64.fullmatch(sha)):
            raise JobError(f"{MANIFEST_ENV} has a bad worker entry")
        pins[name] = Pin(root, bin_, sha)
    return pins


def command(argv: list[str], pins: Mapping[str, Pin] | None) -> list[str]:
    """``argv`` with its worker (argv[0]) replaced by the pinned binary's absolute path. Without a manifest, argv is
    returned as it is: the bare name, resolved through PATH as before. A worker with no pin is refused (JobError)."""
    if pins is None:
        return list(argv)
    pin = pins.get(argv[0])
    if pin is None:
        raise JobError(f"{argv[0]} has no pinned install: it cannot run")
    return [pin.bin, *argv[1:]]


def absent(workers: Iterable[str], pins: Mapping[str, Pin] | None, path: str) -> frozenset[str]:
    """The workers a job cannot run (`not installed`): not on PATH without a manifest, or not pinned with one. A pinned
    worker is resolved by the manifest, so a same-named CLI elsewhere on PATH does not make it absent or refuse the job."""
    missing = set()
    for worker in workers:
        if pins is None:
            if shutil.which(worker, path=path) is None:
                missing.add(worker)
        elif worker not in pins:
            missing.add(worker)
    return frozenset(missing)


def verify(worker: str, pins: Mapping[str, Pin] | None) -> str:
    """"" when the worker's install still holds the digest the manifest recorded and its pinned binary is still at the
    path the manifest names; MODIFIED when it does not; NOT_INSTALLED when the manifest does not pin the worker at all.
    Three distinct values, and callers compare each with its constant: a NOT_INSTALLED is a non-empty string, so a test for
    truth would call it a change. Called before each agent call: an earlier entry's agent may have changed the tree.
    Without a manifest there is nothing to compare."""
    if pins is None:
        return ""
    pin = pins.get(worker)
    if pin is None:
        return NOT_INSTALLED
    try:
        if os.path.realpath(pin.bin) != pin.bin or not os.path.isfile(pin.bin) or digest(Path(pin.root)) != pin.digest:
            return MODIFIED
    except (OSError, JobError):
        return MODIFIED
    return ""
