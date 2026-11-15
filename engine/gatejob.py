"""The gate job (`gate[k]`, F4): one stack prefix in, one `gate-<id>` artifact out.

The matrix has one job per slice on the stack (assemble's `positions`), each on a fresh VM. The job runs two steps,
``python3 -I pivotal/engine/gatejob.py prepare`` and ``... run``, with data in env and files (F2). It is ``contents:
read``, holds no GitHub token, no subscription secret and no other secret, and checks the repo out with
``persist-credentials: false`` (F1). It RUNS REPO CODE (the repo's setup and check), so the checkout, the stack's commits
and everything the check writes are hostile.

Inputs, all read by both steps through ``read_target``: the `land` artifact (``LAND_DIR``: land.json and pivotal.bundle,
made by assemble, which ran no repo code), the job output ``land_sha256`` (``LAND_SHA256``) that binds the downloaded
land.json to the assemble job, the run's ``ISSUE`` and ``BASE_SHA``, and the matrix ``ID``. land.json goes through
assemble.check_land; its issue and base must be this run's, its baseline green, and ``ID`` a stacked slice. The commit
to check is that slice's ``head``: the stack as it stood after the slice (assemble.py), so a prefix 1..k is exactly that
commit and the slices before it.

``prepare`` holds no secret and runs no repo code. It checks the bundle with assemble.check_bundle, fetches the stack
from it into the checkout of ``BASE_SHA`` (hardened git, objects verified with ``transfer.fsckObjects``), refuses a
commit that is not the stack's head for this prefix (the commits between the base and it are exactly the commits of the
slices up to this one, each earlier head is below it), and checks it out detached. It then proves the checkout is the
commit and is clean, before any repo code ran.

``run`` follows the engine pattern of engine/buildjob.py, whose helpers it uses: ``main`` makes the process non-dumpable
and empties ``os.environ`` first (so the check, the same uid, cannot read or write the engine's memory, which decides),
every file of ``engine/`` is digested before the check and compared after, processes that left the check's group are
swept, and the engine owns GITHUB_OUTPUT and the artifact. A job that holds any door secret is refused: it has none.

1. Re-read the target (the same checks) and prove the checkout is the commit and clean.
2. ``run_gate`` (engine/run.sh, the script read before the check ran) with SETUP and the line's CHECK
   (lines.gate_check): a stripped environment, its own session, a timeout (buildjob.GATE_SECONDS), the group killed.
   It prints green, red or none. ``none`` (an empty check) is never green.
3. Sweep strays, then ``lock_git`` (a config that names no command, no hooks, no ``.git/info``, no attribute driver
   the commit did not have) and look at the repo with git alone:
   ``head_unchanged``: ``git rev-parse HEAD`` is the commit. A check that committed, reset or checked out another ref
   is not.
   ``clean``: the refs (every ``for-each-ref`` entry, ``refs/replace``, tags, notes and stash included), the index with
   its flags, and the `.git` files of ``METADATA`` (HEAD, config, packed-refs, shallow, commondir, info, hooks,
   objects/info; a directory deeper than MAX_DEPTH is a plant) are exactly what
   they were before the check, the commit and tree ids of HEAD and the full listing of HEAD's tree (``git ls-tree -r -z
   --full-tree HEAD``) are what they were (a listing that fails is not green), AND the tree is the commit
   (``tree_is_clean``). git runs with replace refs off.
4. ``green`` is ``gate.is_green``: the check printed green AND HEAD is unchanged AND the tree is clean. The artifact
   ``gate.json`` (engine/gate.py) is made by the engine from those facts, published as the only file, read back, and
   only then does the engine write ``published=true`` and ``gate_ok``. The step exits 0 once it has published, even for
   a red gate, and a step after the upload fails the job when ``gate_ok`` is not ``true``: the prefix is then not
   landable and the run is red. A stray process, a modified engine or a late writer to GITHUB_OUTPUT publishes nothing
   (a missing gate artifact is not landable, F5).

Decision (review finding 5): files that the COMMITTED ``.gitignore`` ignores do not make the tree dirty, because
typical repos write caches and build output there. What a check may not do is ADD ignore rules: ``.git/info`` and the
global ignore file are not honoured (``core.excludesFile=/dev/null``, scratch HOME recreated), a changed ``.git/info`` or
config makes the gate not green, an untracked ``.gitignore`` is a plant, and an edit to a committed one shows as modified.

Decision (review round 2, N3): ``clean`` means what git means by it: the content git normalises from the work tree
matches the commit. A committed ``eol=lf`` or ``text`` attribute therefore lets a CRLF file count as clean. A byte-exact
status was rejected: it would turn every repo that converts line endings on checkout red. The attributes are the
commit's (``lock_git`` refuses a changed ``.gitattributes`` that names a driver, and ``.git/info/attributes`` is gone), so
a check cannot add a conversion rule of its own.

Not covered, as in the build job: a process that changes uid, one that outlives the step, the other non-config files of
``.git`` (``objects/info`` including ``alternates`` is digested, ``objects/pack`` and loose objects are not: the commit and
tree ids and the tree listing are snapshotted instead, and git's own hash check on read is a second layer), and an engine
run on a kernel without prctl (refused).
"""

import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

if __package__ in (None, ""):  # run as a script under python3 -I
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import assemble, buildjob, gate, hill, job, lines, plan  # noqa: E402
from engine.buildjob import StrayError  # noqa: E402
from engine.job import JobError  # noqa: E402

if __package__ in (None, ""):
    # Everything is imported. The checkout stays off the import path from here on.
    sys.path.remove(str(Path(__file__).resolve().parent.parent))

GATE_SECONDS = buildjob.GATE_SECONDS
JOB_OVERHEAD_MINUTES = 10  # checkouts, the bundle, the git calls and the upload
JOB_MINUTES = math.ceil(GATE_SECONDS / 60) + JOB_OVERHEAD_MINUTES
TAIL_LINES = 30
# GitHub tokens the job never holds either. ACTIONS_RUNTIME_TOKEN is not here: the runner injects it into every job.
TOKEN_NAMES = frozenset({"GH_TOKEN", "GITHUB_TOKEN"})
CHECKS = re.compile(r"green|red|none")


@dataclass(frozen=True)
class Target:
    land: dict
    id: str
    issue: int
    base: str
    ref: str  # the commit to check: the slice's head on the stack
    below: tuple[str, ...]  # the heads of the stacked slices before it, in order
    commits: int  # how many commits lie between the base and ref


def _directory(env: Mapping[str, str], name: str) -> Path:
    value = env.get(name, "")
    if not value or not os.path.isabs(value) or not os.path.isdir(value):
        raise JobError(f"{name} must be an absolute path of a directory")
    return Path(value)


def read_target(env: Mapping[str, str]) -> Target:
    """The stack prefix this job checks, from the downloaded land.json, or a JobError. Nothing here trusts the artifact
    beyond the checks: its sha256 is the job output's, the schema is assemble's, the run's issue and base match."""
    slice_id = env.get("ID", "")
    if plan.ID_PATTERN.fullmatch(slice_id) is None or slice_id in plan.RESERVED_IDS:
        raise JobError("ID is not a slice id")
    issue = job.read_number(env.get("ISSUE", ""), "ISSUE")
    base = env.get("BASE_SHA", "")
    if job.SHA.fullmatch(base) is None:
        raise JobError("BASE_SHA is not a commit SHA")
    want = env.get("LAND_SHA256", "")
    if assemble.SHA256.fullmatch(want) is None:
        raise JobError("LAND_SHA256 is not a sha256")
    land_dir = _directory(env, "LAND_DIR")
    try:
        data = assemble.read_regular(land_dir / assemble.LAND, assemble.MAX_LAND_BYTES)
    except assemble.ArtifactError:
        raise JobError("land.json is missing, too large or not a regular file") from None
    if sha256(data).hexdigest() != want:
        raise JobError("land.json is not the one assemble made: its sha256 differs from the assemble job's output")
    try:
        land = assemble.check_land(json.loads(data.decode("utf-8"), object_pairs_hook=plan._no_duplicate_keys))
    except assemble.LandError as err:
        raise JobError(f"land.json breaks the schema: {err}") from None
    except (ValueError, RecursionError):
        raise JobError("land.json is not valid JSON") from None
    if land["issue"] != issue or land["base_sha"] != base:
        raise JobError("land.json is for another issue or base than this run's")
    if land["baseline"] != "green":
        raise JobError("the baseline is not green, so nothing is gated (F4)")
    if slice_id not in land["positions"]:
        raise JobError("ID is not a slice on the stack")
    heads, count = [], 0
    for entry in land["slices"]:
        if entry["outcome"] != "candidate":
            continue
        heads.append(entry["head"])
        count += len(entry["commits"])
        if entry["id"] == slice_id:
            return Target(land, slice_id, issue, base, entry["head"], tuple(heads[:-1]), count)
    raise JobError("ID is not a slice on the stack")  # unreachable: check_land makes positions the candidates


# --- git ------------------------------------------------------------------------------------------------------------


def _git(paths: buildjob.Paths, *args: str, label: str, text: bool = False) -> subprocess.CompletedProcess:
    """buildjob.git, with a JobError that names ``label`` and the exit code, never the output."""
    try:
        done = buildjob.git(paths, *args, text=text)
    except (OSError, subprocess.SubprocessError):
        raise JobError(f"git {label} could not run") from None
    if done.returncode != 0:
        raise JobError(f"git {label} failed with exit {done.returncode}")
    return done


def tree_is_clean(paths: buildjob.Paths) -> bool:
    """True when the work tree is exactly the commit at HEAD: no change, no untracked file, nothing hidden.

    What could hide a change from ``git status``, and what is done about it (the check is the same uid as this process
    and may have written anything in ``.git``, the tree and the scratch HOME):

    - index flags (``assume-unchanged``, ``skip-worktree``) and a stale stat cache: the index is deleted and rebuilt
      from HEAD (``read-tree``), so every file is compared with the commit;
    - ignore rules: only the committed ``.gitignore`` files apply. ``.git/info`` is gone (``lock_git``), the config is
      known-good (no ``core.excludesFile``), the scratch HOME is recreated by the caller, and an UNTRACKED
      ``.gitignore`` anywhere is a plant and makes the tree dirty. A committed ``.gitignore`` the check edited shows
      as modified. Files that the committed rules ignore (a build directory) are not a change;
    - a nested repository, a changed mode or symlink, a deleted file, a submodule change: all show in ``status``
      (``--ignore-submodules=none``).

    Any git failure is "not clean"."""
    gitdir = paths.repo / ".git"
    try:
        for entry in os.scandir(gitdir):
            if entry.name in ("index", "index.lock") or entry.name.startswith("sharedindex."):
                buildjob.remove_path(Path(entry.path))
        _git(paths, "read-tree", "HEAD", label="read-tree")
        planted = _git(paths, "ls-files", "-o", "-z", "--", ":(glob)**/.gitignore", label="ls-files")
        if planted.stdout:
            return False
        status = _git(paths, "-c", "core.excludesFile=/dev/null", "status", "--porcelain=v1", "-z", "--untracked-files=normal",
                      "--ignore-submodules=none", label="status")
    except (JobError, OSError):
        return False
    return status.stdout == b""


# What a check may not change in the repository, besides the work tree: `.git` files that decide what git shows
# (read by bytes, never through git, because the check may have made the config run commands), and the ref and index
# state that git reports.
METADATA = ("HEAD", "config", "packed-refs", "shallow", "commondir", "info", "hooks", "objects/info")
BIG_FILE = 1 << 20


MAX_DEPTH = 32  # a deeper directory under `.git` is a plant: it is recorded as "deep" and not entered


def _digest_path(path: Path, rel: str, into: list) -> None:
    """Append what ``path`` is to ``into``, walking directories with a stack (no recursion limit to hit) down to MAX_DEPTH."""
    pending = [(path, rel, 0)]
    while pending:
        path, rel, depth = pending.pop()
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            into.append((rel, "absent"))
            continue
        if stat.S_ISDIR(info.st_mode):
            if depth >= MAX_DEPTH:
                into.append((rel, "deep"))
                continue
            into.append((rel, "dir"))
            # reversed, so the pops come out in sorted order
            pending.extend((path / name, f"{rel}/{name}", depth + 1) for name in sorted(os.listdir(path), reverse=True))
        elif stat.S_ISLNK(info.st_mode):
            into.append((rel, "link", os.readlink(path)))
        elif stat.S_ISREG(info.st_mode):
            if info.st_size > BIG_FILE:
                into.append((rel, "big", info.st_size))
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as fh:
                into.append((rel, "file", info.st_mode & 0o7777, sha256(fh.read(BIG_FILE + 1)).hexdigest()))
        else:
            into.append((rel, "special", stat.S_IFMT(info.st_mode)))


def metadata_state(paths: buildjob.Paths) -> str:
    """The `.git` files that decide what git shows, as one digest. No git runs, so it is safe before ``lock_git``."""
    entries: list = []
    for name in METADATA:
        _digest_path(paths.repo / ".git" / name, name, entries)
    return sha256(repr(entries).encode()).hexdigest()


def git_state(paths: buildjob.Paths) -> bytes:
    """Every ref (``for-each-ref`` lists loose and packed, tags, notes, stash and replace refs alike), the index with its
    flags, the commit and tree ids of HEAD and the listing of HEAD's tree, as git reports them with replace refs off. The
    last two pin the object database: a check that rewrote a pack or a loose object so that HEAD's name maps to another
    commit changes them, whatever the refs say. Call after ``lock_git``. A failure (a listing too) is a JobError."""
    refs = _git(paths, "for-each-ref", "--format=%(refname)%00%(objecttype)%00%(objectname)", label="for-each-ref").stdout
    staged = _git(paths, "ls-files", "-s", "-t", "-z", label="ls-files").stdout
    flags = _git(paths, "ls-files", "-v", "-z", label="ls-files").stdout
    ids = _git(paths, "rev-parse", "HEAD^{commit}", "HEAD^{tree}", label="rev-parse").stdout
    listing = _git(paths, "ls-tree", "-r", "-z", "--full-tree", "HEAD", label="ls-tree").stdout
    return b"\0\0".join((refs, staged, flags, ids, listing))


def repo_state(paths: buildjob.Paths) -> tuple[str, bytes]:
    """(metadata_state, git_state): taken before the check on a locked repo, and after it, metadata before ``lock_git``."""
    return metadata_state(paths), git_state(paths)


def head_is(paths: buildjob.Paths, ref: str) -> bool:
    """``git rev-parse HEAD`` is ``ref``. Any failure is False."""
    try:
        done = _git(paths, "rev-parse", "--verify", "-q", "HEAD^{commit}", label="rev-parse", text=True)
    except JobError:
        return False
    return done.stdout.strip() == ref


def inspect(paths: buildjob.Paths, ref: str, before: tuple[str, bytes]) -> tuple[bool, bool]:
    """(head_unchanged, clean) after the check ran. ``before`` is ``repo_state`` taken just before it. ``clean`` needs the
    refs, the index, and the `.git` files of ``METADATA`` to be what they were AND the tree to be the commit
    (``tree_is_clean``); ``head_unchanged`` is HEAD alone. git is made safe first (``lock_git``) and the scratch HOME,
    which the check could have written (a global ignore file), is replaced. If git cannot be made safe, both are False."""
    try:
        metadata = metadata_state(paths)  # before lock_git, which replaces config, info and hooks
        if buildjob.lock_git(paths, ref):
            return False, False
        buildjob.remove_path(paths.home)
        paths.home.mkdir(mode=0o700)
        same = (metadata, git_state(paths)) == before
    except (JobError, OSError):
        return False, False
    return head_is(paths, ref), tree_is_clean(paths) and same


# --- prepare --------------------------------------------------------------------------------------------------------


def prepare(env: Mapping[str, str]) -> None:
    """The first step: the stack's commit for this prefix, checked out in REPO_DIR. No secret, no repo code."""
    refuse_secrets(env)
    target = read_target(env)
    repo, work, land_dir = _directory(env, "REPO_DIR"), env.get("WORK_DIR", ""), _directory(env, "LAND_DIR")
    if not work or not os.path.isabs(work):
        raise JobError("WORK_DIR must be an absolute path")
    bundle = land_dir / assemble.BUNDLE
    try:
        assemble.check_bundle(target.land, bundle)
    except assemble.LandError as err:
        raise JobError(f"the bundle is not land.json's: {err}") from None
    buildjob.check_tools()
    paths = buildjob.Paths(repo, Path(work))
    paths.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if buildjob.lock_git(paths, target.base):
        raise JobError("the checkout's .gitattributes names a driver")
    if not head_is(paths, target.base):
        raise JobError("the checkout is not at BASE_SHA")
    stack = f"refs/heads/{assemble.stack_branch(target.issue)}"
    _git(paths, "-c", "transfer.fsckObjects=true", "fetch", "-q", "--no-tags", "--no-recurse-submodules", str(bundle), stack,
         label="fetch")
    tip = _git(paths, "rev-parse", "--verify", "FETCH_HEAD^{commit}", label="rev-parse", text=True).stdout.strip()
    if assemble.SHA.fullmatch(tip) is None:
        raise JobError("the bundle's stack is not a commit")
    _git(paths, "merge-base", "--is-ancestor", target.ref, tip, label="merge-base")
    for below in (target.base, *target.below):
        _git(paths, "merge-base", "--is-ancestor", below, target.ref, label="merge-base")
    counted = _git(paths, "rev-list", "--count", f"{target.base}..{target.ref}", label="rev-list", text=True).stdout.strip()
    if counted != str(target.commits):
        raise JobError("the commits below the head are not exactly the commits of the slices up to it")
    _git(paths, "checkout", "-q", "--detach", target.ref, label="checkout")
    if not head_is(paths, target.ref) or not tree_is_clean(paths):
        raise JobError("the checkout is not the stack commit, clean")
    print(f"gate {target.id}: checked out {target.ref} ({target.commits} commits above the base)")


# --- run ------------------------------------------------------------------------------------------------------------


def fail_closed(paths: buildjob.Paths, reason: str, out_path: str) -> int:
    """Publish nothing: buildjob.fail_closed's rule, with this job's outputs (see there for why the output line is
    enough). `out` is renamed aside and removed, and the engine's last word is published=false and gate_ok=false."""
    out = paths.out
    try:
        if os.path.lexists(out):
            os.rename(out, out.with_name(f".out-old-{os.urandom(6).hex()}"))
    except OSError:
        pass
    try:
        buildjob.remove_path(out)
    except BaseException:  # noqa: BLE001 - nothing more can be done about it; it is not in the artifact name
        pass
    try:
        buildjob.write_final(out_path, {"published": "false", "gate_ok": "false"})
    except BaseException:  # noqa: BLE001
        pass
    print(f"nothing published: {reason}", file=sys.stderr)
    return 1


def log_tail(log: Path) -> list[str]:
    """The last lines of the check's log for the job log. Every line is prefixed, so none can start with `::` and be read
    as a workflow command, and control characters are dropped (buildjob.gate_tail)."""
    return [f"| {line}" for line in buildjob.gate_tail(log).splitlines()[-TAIL_LINES:]]


def make_gate(target: Target, word: str, head_unchanged: bool, clean: bool, score: float | None = None, goal: str | None = None) -> dict:
    """The gate of one commit. ``score`` and ``goal`` (the target text) are None for a request with no score; green needs the score
    at or above the target (gate.is_green), so a score run's gate with no score is not green."""
    word = word if CHECKS.fullmatch(word) else "red"  # an answer that is no verdict is never green (F5)
    return {
        "version": gate.GATE_VERSION, "issue": target.issue, "id": target.id, "sha": target.ref, "check": word,
        "head_unchanged": head_unchanged, "clean": clean,
        "green": gate.is_green(word, head_unchanged, clean, score, goal), "score": score, "target": goal,
    }


def _work(env: Mapping[str, str], paths: buildjob.Paths, target: Target, engine_dir: Path, out_path: str) -> int:
    before = repo_state(paths)  # the repo as prepare left it and lock_git made it; the check must leave exactly this
    buildjob.set_writable(engine_dir, False)  # a hindrance for the check, not a boundary: it runs as this user
    word, internal = "red", False
    score, goal = None, None
    try:
        try:
            command = buildjob.score_command(env, engine_dir)  # refused (JobError, red) when the score environment is off
            goal = env.get("TARGET") if command else None
            word = buildjob._gate(env, paths, target.id)
            if word == "green" and goal is not None:
                score = hill.read_score(buildjob._score_file(paths, target.id))  # _gate made it green only with one number
        except StrayError:
            return fail_closed(paths, "stray_process", out_path)
        except buildjob.EngineModified:
            return fail_closed(paths, "engine_modified", out_path)
        except KeyboardInterrupt:
            return fail_closed(paths, "interrupted", out_path)
        except BaseException:  # noqa: BLE001 - whatever broke, the gate is red and says so
            word, internal = "red", True
        try:
            paths.sweep()
        except StrayError:
            return fail_closed(paths, "stray_process", out_path)
        head_unchanged, clean = inspect(paths, target.ref, before)
    finally:
        buildjob.set_writable(engine_dir, True)
    try:
        paths.engine.verify()  # the last look at the engine before it answers: every file
    except buildjob.EngineModified:
        return fail_closed(paths, "engine_modified", out_path)
    try:
        doc = make_gate(target, word, head_unchanged, clean, score, goal)
        files = {gate.GATE: gate.gate_text(doc)}
        buildjob.publish(paths, files)
        if paths.sweep():  # a process was alive while the file was written: it may have written too
            return fail_closed(paths, "stray_process", out_path)
        if why := buildjob.verify_published(paths, files, [], env):
            return fail_closed(paths, why, out_path)
    except StrayError:
        return fail_closed(paths, "stray_process", out_path)
    except BaseException:  # noqa: BLE001 - whatever went wrong, no half-made directory is uploaded
        return fail_closed(paths, "internal", out_path)
    try:
        buildjob.write_final(out_path, {"published": "true", "gate_ok": "true" if doc["green"] else "false"})
    except JobError as err:  # a symlink, a non-file or a late writer: nothing the runner reads can be trusted
        return fail_closed(paths, str(err), out_path)
    reasons = [
        *([] if doc["check"] == "green" else [f"check_{doc['check']}"]),
        *([] if head_unchanged else ["head_moved"]), *([] if clean else ["tree_dirty"]), *(["internal"] if internal else []),
    ]
    print(f"gate {target.id}: {'green' if doc['green'] else 'not green'} at {target.ref}" + (f" ({', '.join(reasons)})" if reasons else ""))
    if not doc["green"]:
        print("\n".join(["check log (last lines):", *log_tail(paths.logs / f"gate-{target.id}.log")]))
    return 0  # published; the step after the upload fails the job when `gate_ok` is not true


def refuse_secrets(env: Mapping[str, str]) -> None:
    """Both steps hold no secret: a door secret or a GitHub token in the environment is refused, by name only."""
    held = sorted((buildjob.DOOR_NAMES | TOKEN_NAMES) & {name for name, value in env.items() if value})
    if held:
        raise JobError("the gate job holds no secret, but it was given " + ", ".join(held))


def run(env: Mapping[str, str]) -> None:
    """The second step. See the module docstring."""
    refuse_secrets(env)
    target = read_target(env)
    out_path = job.read_output_path(env)
    repo, work = _directory(env, "REPO_DIR"), env.get("WORK_DIR", "")
    if not work or not os.path.isabs(work):
        raise JobError("WORK_DIR must be an absolute path")
    scope = env.get("CONTAIN_SCOPE", "descendants")
    if scope not in ("descendants", "user"):
        raise JobError("CONTAIN_SCOPE must be descendants or user")
    if scope == "user":
        buildjob.refuse_shared_host(env)
    buildjob.check_tools()
    paths = buildjob.Paths(repo, Path(work))
    engine_dir = Path(env.get("ENGINE_DIR") or buildjob.ENGINE_DIR_DEFAULT)
    try:
        # LINE is the plan job's validated output, as in the build job: its recipe adds commands to the check.
        line = lines.resolve(env.get("LINE") or lines.DEFAULT, env.get("PRESET_JSON", ""), engine_dir)
        lines.check_wired(line)
        paths.engine = buildjob.Engine(engine_dir, line)
    except lines.LineError as err:
        raise JobError(str(err)) from None
    except buildjob.EngineModified:
        raise JobError("an engine file is missing or is not a regular file") from None
    for directory in (paths.logs, paths.home):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if buildjob.lock_git(paths, target.ref):
        raise JobError("the checkout's .gitattributes names a driver")
    if not head_is(paths, target.ref) or not tree_is_clean(paths):
        raise JobError("the checkout is not the stack commit, clean: prepare did not leave it so")
    paths.guard = buildjob.Guard(scope)
    previous = signal.signal(signal.SIGTERM, buildjob._terminated)
    try:
        code = _work(env, paths, target, engine_dir, out_path)
    finally:
        signal.signal(signal.SIGTERM, previous)
    if code:
        raise SystemExit(code)


JOBS = {"prepare": prepare, "run": run}


def main(argv: list[str], environ) -> int:
    if len(argv) != 2 or argv[1] not in JOBS:
        print(f"usage: gatejob.py {{{'|'.join(JOBS)}}}", file=sys.stderr)
        return 2
    if argv[1] == "run":
        # Before anything else: the check is this process's descendant and the same uid.
        try:
            buildjob.make_unreadable()
        except JobError as err:
            print(f"refused: {err}", file=sys.stderr)
            return 1
        env = buildjob.take_environment(environ)
    else:
        env = dict(environ)
    return job.run_job(JOBS[argv[1]], env)


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
