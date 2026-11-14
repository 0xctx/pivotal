"""The assemble job: slice artifacts to draft branches, one bundle and land.json.

It reads the artifacts of the build jobs (`slice-<id>/` holding `result.json`, `slice.patch`, `summary.md`), classifies
every slice of the plan in matrix order, puts each patch on its own branch off the base commit and writes two files:
`pivotal.bundle` (every draft branch) and `land.json` (what the later `publish` job verifies). It RUNS NO REPO CODE: the
only program it starts is /usr/bin/git, on a scratch repository it made itself, with a clean configuration and no hooks,
so no config, hook, attribute driver or script of the repo or of a patch is ever consulted. It holds `contents: read` only.

The artifacts are hostile input (a build job's agent wrote them). Each slice directory is opened without following links,
holds only the three file names, each a regular file, and is size-capped. `result.json` goes through
`build.check_result`. A missing, malformed or oversized artifact is a `failed` slice with a reason, never a dropped one (F5).
The patch is applied with `git am --3way` after `git apply --numstat` named its paths: no absolute path, no `..`, no `.git`
component. The commits git makes are then recreated with `commit-tree` under one identity and dates derived from the base
commit (its committer time plus the commit's position in the slice), so two runs on the same input give the same commits.

Outcomes of a slice (`land.json`, `check_land`; the table in `classify`):

- `candidate`: an ok result with a patch, no workflow change, a green baseline, a green gate, and a
  verdict that `review.accept` takes (READY, for exactly this patch's sha256 and changed files, from a reviewer of another
  vendor, on this run's base and round), and the patch applied with `git am --3way` on top of the candidates before it.
  Candidates are stacked in matrix order on ONE branch, `pivotal/issue-<n>/stack`. The slice's `commits` are its commits on
  the stack and its `head` the stack as it stood after the slice. A baseline or gate of `none` (no check ran) is not
  green and makes a draft. The job output `positions` lists the candidates in stack order and `has_stack` is
  true when there is one.
- `draft`: the patch applied on the base and has a branch `pivotal/issue-<n>/<slice>` off it, and the reason says why it is
  not on the stack: `baseline_red`, `gate_<word>`, `verdict_missing`, `verdict_invalid`, `verdict_stale`,
  `verdict_needs_fixes` (and `verdict_wrong_sha`, `verdict_files`, `verdict_wrong_builder`), or `conflict` (a candidate whose
  patch does not apply on the stack; `git am --abort` ran and the stack is as it was).
- `blocked`: the change touches `.github/` (`--no-renames`, so a move out of it counts too), recomputed from the patch, never
  taken from the result's flag alone. Nothing is applied.
- `conflict`: the patch does not apply cleanly even on the base (`git am --abort` ran); there is no branch.
- `failed`: no usable artifact, or the build failed or was empty; the reason says which.

Not every slice has a branch: `conflict`, `blocked` and `failed` slices have none and are not in the bundle. The bundle holds
the draft branches and the stack branch, and `base_sha` is its only prerequisite. `candidate` is not permission to land: it
says the slice is on the stack. The gate (engine/gatejob.py) and the level decide. The `review-<id>` artifacts are hostile input as
the slice artifacts are: `review.load` reads them (exactly the three files, regular, validated, re-parsed) and a missing,
malformed or stale one is never a candidate (F5). The `files` of a verdict are reproduced here from the same apply: the names
git reads from the patch plus every file the apply added or removed.

What the `publish` job must do (engine/publish.py): require `needs.assemble.result == 'success'`; compare the
sha256 of the downloaded land.json with the job output `land_sha256` (the artifact travels beside the bundle and is not
bound to the job otherwise); run `check_land`/`check_bundle`; and, after fetching the bundle, re-run the `--no-renames`
`.github` check on `base_sha..head` of every branch it lands rather than trust `outcome`.

A fix round (result round > 0) is built on the draft's head, which plan makes the run's base (`base_ref`): its patch is one commit
on `base_sha`, and it is a `draft` named `fix_round` (or the reason the gate or the verdict gives), never a candidate, so it is never
stacked and never lands by itself. Publish fast-forwards the slice's own branch to it.

Run as `python3 -I pivotal/engine/assemble.py run` with these environment variables: ISSUE, BASE_SHA, MATRIX (the plan's
slices), BASELINE (green, red or none), REPO_DIR (a checkout holding BASE_SHA), SLICES_DIR (the downloaded `slice-*`
artifacts), REVIEWS_DIR (the downloaded `review-*` artifacts), OUT_DIR and WORK_DIR (neither may exist), GITHUB_OUTPUT. Job outputs: positions, gate_matrix (the same ids as a JSON array, `[]` without a stack), has_stack, land_sha256, assembled. Every refusal is raised before the first write.
"""

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script under python3 -I, which puts no directory on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import build, hill, job, plan, review, route, trust  # noqa: E402
from engine.job import JobError  # noqa: E402

GIT = "/usr/bin/git"
SAFE_PATH = "/usr/bin:/bin"
GIT_SECONDS = 300
IDENTITY = ("pivotal", "pivotal@users.noreply.github.com")

RESULT, PATCH, SUMMARY = "result.json", "slice.patch", "summary.md"
ARTIFACT_FILES = (RESULT, PATCH, SUMMARY)
MAX_RESULT_BYTES = 16 * 1024
MAX_PATCH_BYTES = 20 * 1024 * 1024  # engine/buildjob.py MAX_PATCH_BYTES (test_assemble checks that they agree)
MAX_COMMITS = 50  # per slice
MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
MAX_LAND_BYTES = 256 * 1024
LAND, BUNDLE = "land.json", "pivotal.bundle"
LAND_VERSION = 2  # 2: a slice carries its score `target` and the `reviewed_by` of a met fix round (engine/hill.py)

OUTCOMES = ("candidate", "draft", "blocked", "conflict", "failed")
STACK = "stack"  # the last part of the stack branch; plan.RESERVED_IDS keeps it from being a slice id
BASELINES = ("green", "red", "none")
BRANCH_ROOT = "pivotal"
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
REASON = re.compile(r"[a-z0-9_:-]{1,40}")
_COMMIT_START = re.compile(rb"^From ", re.MULTILINE)  # git am splits a mailbox on any `From ` line, not only on a SHA
_BINARY_HUNK = re.compile(rb"(literal|delta) (.*)")  # the size a git binary patch hunk inflates to
_DELTA_HEADER_BYTES = 20  # a delta starts with two varints (source size, target size) of at most 10 bytes each
_CANONICAL_SIZE = re.compile(rb"[1-9][0-9]{0,8}|0")  # git also reads zero-padded and huge sizes, so only this form is counted
MAX_LITERAL_BYTES = 64 * 1024 * 1024  # the sum over one patch; a few KB of zlib can declare hundreds of MB

LAND_KEYS = frozenset({"version", "issue", "base_sha", "baseline", "bundle", "has_stack", "positions", "slices"})
SLICE_KEYS = frozenset(
    {"id", "outcome", "reason", "candidate", "branch", "head", "commits", "built_by", "gate", "fell_back", "score", "target",
     "reviewed_by"})
# A slice's fall-through: its build's items, then its review's (each at most build.MAX_FELL_BACK, see route.check_fell_back).
MAX_SLICE_FELL_BACK = 2 * build.MAX_FELL_BACK
BUNDLE_KEYS = frozenset({"name", "sha256", "bytes"})


class LandError(ValueError):
    """A land.json breaks the schema. The message names the rule and never repeats a value."""


class SliceFailure(Exception):
    """One slice cannot be a draft. ``outcome`` and ``reason`` are the fixed words that land.json carries."""

    def __init__(self, outcome: str, reason: str):
        super().__init__(f"{outcome}: {reason}")
        self.outcome, self.reason = outcome, reason


def branch_name(issue: int, slice_id: str) -> str:
    return f"{BRANCH_ROOT}/issue-{issue}/{slice_id}"


def stack_branch(issue: int) -> str:
    return branch_name(issue, STACK)


# ---------------------------------------------------------------------------------------------------- land.json


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sha(value: object) -> bool:
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def check_land(data: object) -> dict:
    """The land.json of an assemble run, if it follows the schema exactly; else LandError.

    This is the one validator: assemble runs it before it writes the file, and the publish job runs it (through
    ``read_land``) on the file it downloads. Exact keys, 40-hex lowercase SHAs, branches only of the form
    ``pivotal/issue-<n>/<slice>`` with the slice's own id, and the outcome decides which fields are filled.
    """
    if not isinstance(data, dict) or set(data) != LAND_KEYS:
        raise LandError("land.json keys are not exactly the schema's")
    if data["version"] != LAND_VERSION or not _is_int(data["version"]):
        raise LandError(f"version must be {LAND_VERSION}")
    issue = data["issue"]
    if not _is_int(issue) or not 1 <= issue <= 9999999999:
        raise LandError("issue must be a positive number")
    if not _sha(data["base_sha"]) or data["base_sha"] == "0" * 40:
        raise LandError("base_sha must be a commit SHA")
    if data["baseline"] not in BASELINES:
        raise LandError(f"baseline must be one of {', '.join(BASELINES)}")
    slices = data["slices"]
    if not isinstance(slices, list) or not 1 <= len(slices) <= plan.MAX_SLICES:
        raise LandError(f"slices must be a list of 1 to {plan.MAX_SLICES}")
    seen: set[str] = set()
    drafts: dict[str, str] = {}
    stacked: list[str] = []
    every_commit: set[str] = set()
    for item in slices:
        if not isinstance(item, dict) or set(item) != SLICE_KEYS:
            raise LandError("a slice's keys are not exactly the schema's")
        slice_id = item["id"]
        if not isinstance(slice_id, str) or plan.ID_PATTERN.fullmatch(slice_id) is None or slice_id in plan.RESERVED_IDS:
            raise LandError("a slice id is not a slice id")
        if slice_id in seen:
            raise LandError("a slice id is repeated")
        seen.add(slice_id)
        if item["outcome"] not in OUTCOMES:
            raise LandError(f"outcome must be one of {', '.join(OUTCOMES)}")
        if not isinstance(item["reason"], str) or REASON.fullmatch(item["reason"]) is None:
            raise LandError("reason must be a short word")
        if not isinstance(item["candidate"], bool) or item["candidate"] != (item["outcome"] == "candidate"):
            raise LandError("candidate must be true exactly for the outcome candidate")
        built_by = item["built_by"]
        if not isinstance(built_by, str) or (built_by and not route.valid_spec(built_by)):
            raise LandError("built_by must be a route entry spec or empty")
        if item["gate"] not in (*build.GATES, ""):
            raise LandError("gate must be a gate word or empty")
        try:
            route.check_fell_back(item["fell_back"], MAX_SLICE_FELL_BACK)
        except ValueError as err:
            raise LandError(f"fell_back: {err}") from None
        if item["score"] is not None and (not _is_number(item["score"]) or not 0 <= item["score"] <= 1):
            raise LandError("score must be null or a number from 0 to 1")
        if not isinstance(item["target"], str) or (item["target"] and not hill.is_target(item["target"])):
            raise LandError("target must be a score target or empty")
        reviewed_by = item["reviewed_by"]
        if not isinstance(reviewed_by, str) or (reviewed_by and not route.valid_spec(reviewed_by)) or (reviewed_by and item["reason"] != "score_met"):
            raise LandError("reviewed_by must be a route entry spec, and only a met fix round has one")
        commits = item["commits"]
        if not isinstance(commits, list) or len(commits) > MAX_COMMITS or not all(_sha(c) for c in commits):
            raise LandError("commits must be a short list of SHAs")
        if item["outcome"] in ("draft", "candidate"):
            want = stack_branch(issue) if item["outcome"] == "candidate" else branch_name(issue, slice_id)
            if item["branch"] != want:
                raise LandError("a draft's branch must be pivotal/issue-<n>/<its slice>, a candidate's the stack's")
            if not commits or len(set(commits)) != len(commits) or item["head"] != commits[-1]:
                raise LandError("a draft's head must be the last of its commits")
            if item["outcome"] == "candidate" and not one_commit(commits):
                raise LandError("a candidate is exactly one commit: only its head is gated")
            if every_commit & set(commits):
                raise LandError("a commit belongs to one slice")
            every_commit |= set(commits)
            if item["outcome"] == "candidate":
                stacked.append(slice_id)
            else:
                drafts[slice_id] = item["head"]
        else:
            if item["branch"] is not None or item["head"] is not None or commits:
                raise LandError("only a draft or a candidate has a branch, a head and commits")
    bundle = data["bundle"]
    if not drafts and not stacked:
        if bundle is not None:
            raise LandError("without a draft or a candidate there is no bundle")
    else:
        if not isinstance(bundle, dict) or set(bundle) != BUNDLE_KEYS or bundle["name"] != BUNDLE:
            raise LandError("a bundle's keys are not exactly the schema's")
        if not isinstance(bundle["sha256"], str) or SHA256.fullmatch(bundle["sha256"]) is None:
            raise LandError("bundle sha256 must be 64 hex digits")
        if not _is_int(bundle["bytes"]) or not 1 <= bundle["bytes"] <= MAX_BUNDLE_BYTES:
            raise LandError("bundle bytes must be a size")
    positions = data["positions"]
    if not isinstance(data["has_stack"], bool):
        raise LandError("has_stack must be true or false")
    if not isinstance(positions, list) or positions != stacked:
        raise LandError("positions must be exactly the candidates, in slice order")
    if data["has_stack"] != bool(positions):
        raise LandError("has_stack is true exactly when positions is not empty")
    return data


def land_text(land: Mapping) -> bytes:
    """The bytes of land.json: validated, ASCII, fixed key order, so equal inputs give equal files."""
    check_land(land)
    return (json.dumps(land, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def read_land(path: Path) -> dict:
    """land.json from a downloaded artifact: a regular file within the cap, JSON without duplicate keys, then
    ``check_land``. For the publish job. Anything else is a LandError."""
    try:
        data = read_regular(Path(path), MAX_LAND_BYTES)
    except ArtifactError as err:
        raise LandError(f"land.json is unreadable: {err}") from None
    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=plan._no_duplicate_keys)
    except (ValueError, RecursionError):
        raise LandError("land.json is not valid JSON") from None
    return check_land(parsed)  # outside the try: its LandError names the rule (a version, say)


BUNDLE_HEADER_BYTES = 64 * 1024


def bundle_header(data: bytes) -> tuple[set[str], dict[str, str]]:
    """(prerequisite SHAs, {ref: sha}) from the header of a git bundle (v2 or v3), read without running git.
    Raises LandError for anything else."""
    head, sep, _ = data[:BUNDLE_HEADER_BYTES].partition(b"\n\n")
    lines = head.split(b"\n")
    if not sep or lines[0] not in (b"# v2 git bundle", b"# v3 git bundle"):
        raise LandError("the bundle has no readable header")
    needs: set[str] = set()
    heads: dict[str, str] = {}
    for raw in lines[1:]:
        line = raw.decode("utf-8", "replace")
        if line.startswith("@"):
            if line != "@object-format=sha1":
                raise LandError("the bundle uses a format that is not sha1")
        elif line.startswith("-"):
            needs.add(line[1:].split(" ", 1)[0])
        else:
            sha, _, ref = line.partition(" ")
            if ref in heads:
                raise LandError("the bundle names a ref twice")
            heads[ref] = sha
    return needs, heads


def check_bundle(land: Mapping, bundle: Path) -> None:
    """The bundle is the one land.json describes: its size and sha256 match, it requires exactly ``base_sha``, and it
    holds exactly the draft branches and the stack branch at exactly their heads. For the publish job, which then fetches from it. Raises
    LandError."""
    check_land(land)
    if land["bundle"] is None:
        raise LandError("land.json describes no bundle")
    try:
        data = read_regular(Path(bundle), MAX_BUNDLE_BYTES)
    except ArtifactError as err:
        raise LandError(f"the bundle is unreadable: {err}") from None
    if len(data) != land["bundle"]["bytes"] or hashlib.sha256(data).hexdigest() != land["bundle"]["sha256"]:
        raise LandError("the bundle is not the one land.json describes")
    needs, heads = bundle_header(data)
    if needs != {land["base_sha"]}:
        raise LandError("the bundle must require exactly the base commit")
    drafts = {f"refs/heads/{s['branch']}": s["head"] for s in land["slices"] if s["outcome"] == "draft"}
    if land["positions"]:  # the stack branch stands at the last candidate
        last = next(s for s in land["slices"] if s["id"] == land["positions"][-1])
        drafts[f"refs/heads/{stack_branch(land['issue'])}"] = last["head"]
    if heads != drafts:
        raise LandError("the bundle's heads are not the drafts of land.json")


# ------------------------------------------------------------------------------------------- reading artifacts


class ArtifactError(Exception):
    """A file or directory of the artifacts is not what it must be. ``reason`` is a fixed word."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def read_regular(path: Path, limit: int, dir_fd: int | None = None) -> bytes:
    """The bytes of a regular file of at most ``limit`` bytes, opened without following a link or blocking on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        raise ArtifactError("missing") from None
    except OSError:
        raise ArtifactError("not_a_file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactError("not_a_file")
        if info.st_size > limit:
            raise ArtifactError("too_large")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ArtifactError("too_large")
    return data


def open_dir(path: str, dir_fd: int | None = None) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        raise ArtifactError("missing") from None
    except OSError:
        raise ArtifactError("not_a_directory") from None


@dataclass
class Artifact:
    result: dict
    patch: bytes | None


def read_artifact(slices_fd: int, slice_id: str) -> Artifact:
    """The checked artifact of one slice. Raises ArtifactError (the directory or a file is wrong or absent) or
    SliceFailure (the result is not valid, or names another slice)."""
    fd = open_dir(f"slice-{slice_id}", slices_fd)
    try:
        names = os.listdir(fd)
        if not set(names) <= set(ARTIFACT_FILES):
            raise ArtifactError("unexpected_file")
        for name in names:  # a symlink, FIFO, device or directory in place of a file is refused before anything is read
            if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                raise ArtifactError("not_a_file")
        try:
            text = read_regular(Path(RESULT), MAX_RESULT_BYTES, fd).decode("utf-8")
            result = build.check_result(json.loads(text, object_pairs_hook=plan._no_duplicate_keys))
        except ArtifactError as err:
            raise SliceFailure("failed", "no_result" if err.reason == "missing" else "bad_artifact") from None
        except (ValueError, RecursionError):  # not UTF-8, not JSON, a duplicate key, or ResultError (a ValueError)
            raise SliceFailure("failed", "bad_result") from None
        if result["slice"] != slice_id:
            raise SliceFailure("failed", "bad_result")
        patch = None
        if result["status"] == "ok" and not result["workflow_change"]:
            try:
                patch = read_regular(Path(PATCH), MAX_PATCH_BYTES, fd)
            except ArtifactError as err:
                raise SliceFailure("failed", "no_patch" if err.reason == "missing" else "bad_artifact") from None
        return Artifact(result, patch)
    finally:
        os.close(fd)


def list_artifacts(slices_fd: int, wanted: Sequence[str]) -> list[str]:
    """The artifact directories nobody asked for, as a printable list of notes (the plan's slices are the only ones used)."""
    notes = []
    for name in sorted(os.listdir(slices_fd)):
        match = re.fullmatch(rf"slice-({plan.ID_PATTERN.pattern})", name)
        if match and match.group(1) in wanted:
            continue
        notes.append(f"ignored: {name if match else 'an artifact with an odd name'} is not a slice of the plan")
    return notes


# ------------------------------------------------------------------------------------------------------- git


class GitError(Exception):
    """git failed where it must not. The message names the command, never its output."""


class Git:
    """/usr/bin/git on a scratch repository, with nothing inherited: a fixed environment (no GIT_* variable, a scratch
    HOME, no global or system config, no attribute file), hooks off and one identity. The commit times are set per call."""

    def __init__(self, root: Path, base: str):
        self.root = Path(root)
        self.repo = self.root / "repo"
        self.home = self.root / "home"
        self.base = base

    def environment(self, when: int | None = None) -> dict[str, str]:
        env = {
            "PATH": SAFE_PATH,
            "HOME": str(self.home),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_AUTHOR_NAME": IDENTITY[0],
            "GIT_AUTHOR_EMAIL": IDENTITY[1],
            "GIT_COMMITTER_NAME": IDENTITY[0],
            "GIT_COMMITTER_EMAIL": IDENTITY[1],
        }
        if when is not None:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{when} +0000"
        return env

    def run(self, *args: str, input: bytes | None = None, when: int | None = None, check: bool = True,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        command = [
            GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null",
            "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "gc.auto=0", "-c", "pack.threads=1",
            "-c", "core.quotePath=false", *args,
        ]
        try:
            done = subprocess.run(
                command, input=input, capture_output=True, check=False, timeout=GIT_SECONDS,
                env=self.environment(when), cwd=cwd or (self.repo if self.repo.is_dir() else self.root),
            )
        except (OSError, subprocess.SubprocessError):
            raise GitError(f"git {args[0]} could not run") from None
        if check and done.returncode != 0:
            raise GitError(f"git {args[0]} failed with exit {done.returncode}")
        return done

    def out(self, *args: str, **kw) -> str:
        return self.run(*args, **kw).stdout.decode("utf-8", "replace").strip()

    def init(self) -> None:
        self.home.mkdir(mode=0o700)
        self.run("init", "-q", str(self.repo), cwd=self.root)

    def fetch_base(self, source: Path) -> int:
        """Fetch the base commit from ``source`` and return its committer time. Only objects arrive: none of the source's
        configuration, hooks or refs. ``--depth=1`` makes the base a shallow root here, which lets it be the root of a
        shallow ``source`` (a checkout at the default fetch depth): git refuses to take a shallow root without a depth. No
        parent of the base is fetched, and nothing in assemble reads one."""
        self.run("fetch", "-q", "--depth=1", "--no-tags", "--no-recurse-submodules", str(source), self.base)
        if self.out("cat-file", "-t", self.base) != "commit":
            raise GitError("BASE_SHA is not a commit")
        return int(self.out("show", "-s", "--format=%ct", self.base))

    def reset(self, ref: str | None = None) -> None:
        """Back to ``ref`` (the base by default), whatever a failed `am` left: a leftover rebase directory, files, an index."""
        ref = ref or self.base
        self.run("am", "--abort", check=False)
        shutil.rmtree(self.repo / ".git" / "rebase-apply", ignore_errors=True)
        self.run("checkout", "-q", "-f", "--detach", ref)
        self.run("reset", "-q", "--hard", ref)
        self.run("clean", "-ffdxq")


def numstat_paths(git: Git, patch: bytes) -> list[str]:
    """Every path `git apply --numstat -z` reports for the patch, as git itself parses it. A rename or copy that git
    reports with both names yields both, but a 100% rename lists only its destination, so this list is not the
    authority for renames: the post-am `--no-renames` check on what git really committed is."""
    done = git.run("apply", "--numstat", "-z", input=patch, check=False)
    if done.returncode != 0:
        raise SliceFailure("conflict", "does_not_apply")
    parts = done.stdout.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    paths: list[bytes] = []
    i = 0
    while i < len(parts):
        fields = parts[i].split(b"\t", 2)
        if len(fields) != 3:
            raise SliceFailure("failed", "bad_patch")
        if fields[2] == b"":  # a rename or copy: the old and the new name follow
            paths += parts[i + 1 : i + 3]
            i += 3
        else:
            paths.append(fields[2])
            i += 1
    names = []
    for raw in paths:
        try:
            names.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise SliceFailure("failed", "unsafe_path") from None
    return names


def unsafe_path(name: str) -> bool:
    parts = name.split("/")
    return (
        not name
        or name.startswith("/")
        or "\0" in name
        or any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts)
    )


def is_workflow_path(name: str) -> bool:
    return name.split("/", 1)[0].casefold() == ".github"


def hunk_stream(lines: Sequence[bytes]) -> bytes:
    """The zlib bytes of one hunk's data lines, decoded as git does (a length letter, then 5 characters per 4 bytes).
    Anything git would refuse is bad_patch."""
    raw = b""
    for line in lines:
        n = line[:1]
        length = n[0] - 64 if n and 65 <= n[0] <= 90 else n[0] - 70 if n and 97 <= n[0] <= 122 else 0
        if length < 1 or len(line) != 1 + (length + 3) // 4 * 5:
            raise SliceFailure("failed", "bad_patch")
        try:
            raw += base64.b85decode(line[1:])[:length]
        except (ValueError, binascii.Error):
            raise SliceFailure("failed", "bad_patch") from None
    return raw


def inflate_exact(raw: bytes, size: int) -> bytes:
    """What the hunk inflates to, which must be exactly ``size`` bytes and end the stream, as git requires. At most
    ``size + 1`` bytes are ever produced, so a bomb costs nothing here; any other outcome is bad_patch."""
    inflater = zlib.decompressobj()
    try:
        data = inflater.decompress(raw, size + 1)
    except zlib.error:
        raise SliceFailure("failed", "bad_patch") from None
    if len(data) != size or not inflater.eof:
        raise SliceFailure("failed", "bad_patch")
    return data


def delta_target(stream: bytes) -> int:
    """The size a `delta` hunk produces: the second varint of its header, not the hunk's own size (a copy op with no size
    bytes copies 0x10000, so a one-byte op writes 64 KB). A header longer than 20 bytes (two varints of 10) or that does
    not end is bad_patch."""
    sizes, shift, value = [], 0, 0
    for byte in stream[:_DELTA_HEADER_BYTES]:
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            sizes.append(value)
            shift = value = 0
            if len(sizes) == 2:
                return sizes[1]
    raise SliceFailure("failed", "bad_patch")


def scan_mails(git: Git, patch: bytes) -> list[bytes]:
    """The patch text of every mail `git am` would apply, after checking what each `GIT binary patch` makes git inflate:
    the forward hunk's size and, for a `delta`, the size it produces (``delta_target``), summed over the patch against
    MAX_LITERAL_BYTES before each hunk is inflated, and each inflated with a bound (``inflate_exact``). The reverse hunk,
    which `git am` never applies but keeps in memory until it exits, is inflated the same way and summed on its own
    (``reverse_total``) against the same cap. Not the raw mailbox: each
    mail goes through `git mailsplit -b` (as `git am` runs it) and `git mailinfo`, and only the patch text mailinfo
    extracts is read, so a commit message that says `GIT binary patch` or `delta x` is not a hunk and cannot hide one.
    The header is matched by prefix (git accepts `GIT binary patch\\r`); a hunk is the `literal`/`delta` line right after
    it. A size that is not canonical is bad_patch."""
    scratch = git.root / "scan"
    shutil.rmtree(scratch, ignore_errors=True)
    mails = scratch / "mails"
    mails.mkdir(parents=True, mode=0o700)
    git.run("mailsplit", f"-o{mails}", "-b", input=patch, cwd=scratch)
    total = reverse_total = 0
    patches: list[bytes] = []
    for name in sorted(os.listdir(mails)):
        with open(mails / name, "rb") as mail:
            done = git.run("mailinfo", str(scratch / "msg"), str(scratch / "patch"), input=mail.read(), cwd=scratch,
                           check=False)
        if done.returncode != 0:  # git am would refuse this mail too
            raise SliceFailure("failed", "bad_patch")
        text = (scratch / "patch").read_bytes()
        patches.append(text)
        lines = text.split(b"\n")
        i = 0
        while i < len(lines):
            if not lines[i].startswith(b"GIT binary patch"):
                i += 1
                continue
            i += 1
            for forward in (True, False):
                m = _BINARY_HUNK.fullmatch(lines[i]) if i < len(lines) else None
                if m is None:
                    break
                if not _CANONICAL_SIZE.fullmatch(m.group(2)):
                    raise SliceFailure("failed", "bad_patch")
                size = int(m.group(2))
                if forward:
                    total += size
                else:
                    reverse_total += size
                if total > MAX_LITERAL_BYTES or reverse_total > MAX_LITERAL_BYTES or size > MAX_LITERAL_BYTES:
                    raise SliceFailure("failed", "patch_too_large")
                start = i + 1
                i = start
                while i < len(lines) and lines[i] != b"":  # the hunk's data lines, ended by a blank line
                    i += 1
                data = inflate_exact(hunk_stream(lines[start:i]), size)
                if forward:
                    if m.group(1) == b"delta":
                        total += delta_target(data)
                        if total > MAX_LITERAL_BYTES:
                            raise SliceFailure("failed", "patch_too_large")
                i += 1
    shutil.rmtree(scratch, ignore_errors=True)
    return patches


def apply_patch(git: Git, patch: bytes, patches: Sequence[bytes], base_time: int, onto: str | None = None,
                before: int = 0) -> tuple[list[str], list[bytes]]:
    """Apply the patch on ``onto`` (the base by default) and return (the new commits' SHAs, oldest first, recreated under
    the fixed identity and dates; the changed files). ``patches`` is what ``scan_mails`` made of the patch. ``before`` is
    the number of commits already on the stack, so the dates keep counting up from the base's. The files are the names git
    reads from the patch plus every file the apply added or removed: the set the review job reproduces for its verdict.
    Raises SliceFailure (``blocked``, ``conflict`` or ``failed``) before any branch exists."""
    onto = onto or git.base
    if len(_COMMIT_START.findall(patch)) > MAX_COMMITS:
        raise SliceFailure("failed", "too_many_commits")
    git.reset(onto)
    names = [n for text in patches if text.strip() for n in numstat_paths(git, text)]
    if any(unsafe_path(n) for n in names):
        raise SliceFailure("failed", "unsafe_path")
    if any(is_workflow_path(n) for n in names):
        raise SliceFailure("blocked", "workflow_change")
    done = git.run("am", "--3way", "--quiet", input=patch, when=base_time + before + 1, check=False)
    if done.returncode != 0:
        git.run("am", "--abort", check=False)
        raise SliceFailure("conflict", "does_not_apply")
    # The same test again on what git really committed (`--no-renames`, every commit of the series, not their net sum).
    touched = git.run("log", "--no-renames", "--name-only", "-z", "--format=", f"{onto}..HEAD").stdout.split(b"\0")
    if any(is_workflow_path(t.decode("utf-8", "replace")) for t in touched if t):
        raise SliceFailure("blocked", "workflow_change")
    made = git.out("rev-list", "--reverse", f"{onto}..HEAD").split()
    if not made or len(made) > MAX_COMMITS:
        raise SliceFailure("failed", "bad_patch")
    made_files = git.run("diff", "--no-renames", "--diff-filter=AD", "--name-only", "-z", onto, "HEAD").stdout.split(b"\0")
    files = list(dict.fromkeys([*(n.encode("utf-8") for n in names), *(f for f in made_files if f)]))
    parent, rewritten = onto, []
    for position, commit in enumerate(made, start=1):
        raw = git.run("cat-file", "commit", commit).stdout
        message = raw.partition(b"\n\n")[2]
        tree = git.out("rev-parse", f"{commit}^{{tree}}")
        parent = git.out("commit-tree", tree, "-p", parent, "-F", "-", input=message, when=base_time + before + position)
        rewritten.append(parent)
    return rewritten, files


# --------------------------------------------------------------------------------------------- classification


def classify(result: Mapping | None, baseline: str, verdict: str = "", kind: str = "") -> tuple[str, str]:
    """(outcome, reason) of a checked result: the table in `classify`, row by row, in this order.
    A request with a score (engine/hill.py)
    adds two rows after the gate: a score below the target is `draft`/`score_low` (a fix round may follow); a fix round whose
    score meets the target and whose verdict is READY is `draft`/`score_met`, which publish marks ready (a fix round is never stacked; round 0 at the target is `candidate`/stacked).

    ``None`` is a result that could not be read. ``verdict`` is the reason the slice's verdict does not let it stack, or
    "" when it does (or has not been looked at yet: the caller asks first with "" and, when that gives ``candidate``,
    reads the verdict and asks again). A failed or empty build is ``failed`` with its own reason; a change to workflow
    files is ``blocked``; then a baseline that is not exactly green (red, none, anything) makes a ``draft``; an incident (``kind``
    "incident", a red main) whose base passes the repo's check is a ``draft`` named ``already_green``: the check passes on the base,
    so the change is not needed and is never a candidate (I1). The failed CI run may be another job, still red: the reason says so (I-B). Then a gate that is not exactly green
    and the verdict make a ``draft``; anything else is a ``candidate``. A repo with no check has baseline and gate ``none``:
    nothing ran, so nothing is a candidate (F4). The table's ``empty`` outcome is ``failed``/``empty_patch`` here (an empty patch
    never lands). The ``gate other`` row is reached only through this function: ``build.check_result`` already
    turns a red or not_run gate into ``failed``/``bad_result``, so no real ok result carries one. A fix round's patch is built on the previous head, which the fix run takes as its base (plan's ``base_ref``), so it is classified like any
    other patch but is a ``draft`` at best (``fix_round``) and never a candidate, and the baseline is not asked of it."""
    if result is None:
        return "failed", "no_result"
    if result["status"] != "ok":
        return "failed", result["reason"]
    if result["workflow_change"]:
        return "blocked", "workflow_change"
    fix = result["round"] != 0
    if baseline != "green" and not fix:  # a fix round's base is the draft's head, which was red or rejected: it is what is being fixed
        return "draft", "baseline_red"
    if kind == "incident" and not fix:  # the base passes the repo's check: the failed CI job may differ and be red (I1, I-B)
        return "draft", "already_green"
    if result["gate"] != "green":
        return "draft", f"gate_{result['gate']}"
    target = result.get("target", "")
    if target and not hill.met(result.get("score"), target):
        return "draft", "score_low"
    if verdict:
        return "draft", verdict
    # A fix round is built on the draft's head (its base here), so the patch has no place on the stack: it is the next head of
    # that slice's own branch, a fast-forward that publish pushes and whose pull request it updates. It never lands by itself,
    # except as a ready pull request once its score meets the target (`score_met`).
    if fix:
        return "draft", ("score_met" if target else "fix_round")
    return "candidate", "stacked"


def review_fell_back(reviews_dir: Path, slice_id: str, result: Mapping, base: str) -> list[dict]:
    """The fall-through items of the slice's review, for the RESULT, whatever the slice's outcome: [] when there is no review
    for this slice, round and base, or it does not load. Only the items are read here; verdict_reason decides the verdict."""
    directory = reviews_dir / f"review-{slice_id}"
    if not os.path.lexists(directory):
        return []
    try:
        verdict = review.load(directory)
    except (review.ReviewError, OSError, RecursionError):
        return []
    if verdict["slice"] != slice_id or verdict["round"] != result["round"] or verdict["base"] != base:
        return []
    return list(verdict["fell_back"])


def reviewer_of(reviews_dir: Path, slice_id: str) -> str:
    """The ``reviewed_by`` of the slice's verdict ("" when it cannot be read): only asked after ``verdict_reason`` accepted it."""
    try:
        return review.load(reviews_dir / f"review-{slice_id}")["reviewed_by"]
    except (review.ReviewError, OSError, RecursionError):
        return ""


def verdict_reason(reviews_dir: Path, slice_id: str, result: Mapping, base: str, patch: bytes, files: Sequence[str]) -> str:
    """"" when the slice's `review-<id>` artifact is a READY for exactly this patch, else the reason (a word of land.json).
    The artifact is hostile input: ``review.load`` validates it, and a verdict for another slice, round or base is stale.
    Anything that is not a clean READY is a reason; nothing here can raise."""
    directory = reviews_dir / f"review-{slice_id}"
    if not os.path.lexists(directory):
        return "verdict_missing"
    try:
        verdict = review.load(directory)
    except (review.ReviewError, OSError, RecursionError):
        return "verdict_invalid"
    if verdict["slice"] != slice_id or verdict["round"] != result["round"] or verdict["base"] != base:
        return "verdict_stale"
    why = review.accept(verdict, patch_sha256=hashlib.sha256(patch).hexdigest(), files=files, built_by=result["built_by"])
    if not why:
        return ""
    reason = "verdict_" + why
    return reason if REASON.fullmatch(reason) else "verdict_invalid"


def one_commit(commits: Sequence[str]) -> bool:
    """A candidate is exactly one commit (F4): the gate is run on a stack prefix, and "commit k of the stack" has to be
    "slice k" for the gate of the head to be the gate of every commit the fast-forward moves."""
    return len(commits) == 1


def diff_shape(git: Git, before: str, after: str) -> bytes:
    """What a commit changes, apart from where: the +/- lines at zero context (no hunk offsets, no blob ids, since an
    earlier slice may legitimately move or change what a hunk sits in), the mode and status of each path, and the new blob
    of a binary file. Two commits with the same shape make the same change."""
    text = git.run("diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv", before, after).stdout
    kept = [line for line in text.split(b"\n") if not line.startswith((b"index ", b"@@"))]
    stat = git.run("diff", "--numstat", "-z", "--no-renames", before, after).stdout
    binary = {rec.split(b"\t", 2)[2] for rec in stat.split(b"\0") if rec.startswith(b"-\t-\t")}
    fields = git.run("diff", "--raw", "--no-abbrev", "-z", "--no-renames", before, after).stdout.split(b"\0")
    raw, i = [], 0
    while i + 1 < len(fields) and fields[i].startswith(b":"):
        mode_a, mode_b, _, blob, status = fields[i][1:].split(b" ")
        path = fields[i + 1]
        raw.append(b" ".join([mode_a, mode_b, status, path, blob if path in binary else b""]))
        i += 2
    return b"\n".join([*kept, b"--raw", *raw])


@dataclass
class Stack:
    """The candidates so far: the tip commit (the base while empty) and how many commits are on it."""
    tip: str
    commits: int = 0


def assemble_slice(git: Git, slices_fd: int, reviews_dir: Path, issue: int, slice_: plan.Slice, baseline: str,
                   base_time: int, stack: Stack, kind: str = "") -> dict:
    """The land.json entry of one slice. A draft has its branch made in ``git``'s repository; a candidate is put on
    ``stack`` (which moves) and has no branch of its own."""
    entry = {
        "id": slice_.id, "outcome": "failed", "reason": "internal", "candidate": False, "branch": None,
        "head": None, "commits": [], "built_by": "", "gate": "", "fell_back": [], "score": None, "target": "", "reviewed_by": "",
    }
    result = None
    try:
        artifact = read_artifact(slices_fd, slice_.id)
        result = artifact.result
        entry["built_by"], entry["gate"] = result["built_by"], result["gate"]
        entry["fell_back"] = list(result["fell_back"]) + review_fell_back(reviews_dir, slice_.id, result, git.base)
        entry["score"], entry["target"] = result.get("score"), result.get("target", "")
        outcome, reason = classify(result, baseline, kind=kind)
        if outcome in ("candidate", "draft"):
            if artifact.patch is None:
                raise SliceFailure("failed", "no_patch")
            patches = scan_mails(git, artifact.patch)
            commits, files = apply_patch(git, artifact.patch, patches, base_time)  # on the base: the draft, and the files
            if outcome == "candidate" or reason in ("fix_round", "score_met"):
                why = verdict_reason(reviews_dir, slice_.id, result, git.base, artifact.patch, review.files_of(files))
                if not why and not one_commit(commits):
                    why = "multi_commit"
                outcome, reason = classify(result, baseline, why, kind)
            if outcome == "candidate":
                try:
                    onstack, _ = apply_patch(git, artifact.patch, patches, base_time, stack.tip, stack.commits)
                except SliceFailure as err:
                    if err.outcome == "blocked":
                        raise
                    outcome, reason = "draft", "conflict"  # the patch applied on the base, so any other failure here is the stack
                else:
                    if not one_commit(onstack) or diff_shape(git, git.base, commits[-1]) != diff_shape(git, stack.tip, onstack[-1]):
                        # The verdict is bound to the patch on the base; the commit that lands must make that same change.
                        outcome, reason = "draft", "stack_differs"
                    else:
                        stack.tip, stack.commits = onstack[-1], stack.commits + len(onstack)
                        entry.update(branch=stack_branch(issue), head=onstack[-1], commits=onstack)
            if outcome == "draft":
                branch = branch_name(issue, slice_.id)
                git.run("update-ref", f"refs/heads/{branch}", commits[-1], "0" * 40)
                entry.update(branch=branch, head=commits[-1], commits=commits)
        if reason == "score_met":
            entry["reviewed_by"] = reviewer_of(reviews_dir, slice_.id)  # the READY verdict that let the round through
        entry.update(outcome=outcome, reason=reason, candidate=outcome == "candidate")
    except ArtifactError as err:
        entry.update(outcome="failed", reason="no_artifact" if err.reason == "missing" else "bad_artifact")
    except SliceFailure as err:
        entry.update(outcome=err.outcome, reason=err.reason, candidate=False)
    except (GitError, OSError, ValueError):
        entry.update(outcome="failed", reason="internal", candidate=False, branch=None, head=None, commits=[])
    return entry


# ------------------------------------------------------------------------------------------------------- job


@dataclass
class Settings:
    issue: int
    base: str
    slices: tuple[plan.Slice, ...]
    baseline: str
    kind: str  # the run's kind (trust.KINDS): an incident's green base is already_green, every other kind is classified as before
    repo_dir: Path
    slices_dir: Path
    reviews_dir: Path
    out_dir: Path
    work_dir: Path
    output: str


def _absolute(env: Mapping[str, str], name: str) -> Path:
    value = env.get(name, "")
    if not value or "\0" in value or not os.path.isabs(value):
        raise JobError(f"{name} must be an absolute path")
    return Path(value)


def read_settings(env: Mapping[str, str]) -> Settings:
    """Every input, checked; nothing is written. Anything wrong is a JobError."""
    issue = job.read_number(env.get("ISSUE", ""), "ISSUE")
    base = env.get("BASE_SHA", "")
    if job.SHA.fullmatch(base) is None:
        raise JobError("BASE_SHA is not a commit SHA")
    try:
        slices = plan.parse_slices(env.get("MATRIX", ""))
    except plan.SliceError as err:
        raise JobError(f"MATRIX is not the plan's slices: {err}") from None
    baseline = env.get("BASELINE", "")
    if baseline not in BASELINES:
        raise JobError(f"BASELINE must be one of {', '.join(BASELINES)}")
    kind = env.get("KIND", "")  # the run's kind, from the caller: a missing or unknown one is refused, never read as "not an incident"
    if kind not in trust.KINDS:
        raise JobError(f"KIND must be one of {', '.join(sorted(trust.KINDS))}")
    repo_dir, slices_dir = _absolute(env, "REPO_DIR"), _absolute(env, "SLICES_DIR")
    reviews_dir = _absolute(env, "REVIEWS_DIR")
    out_dir, work_dir = _absolute(env, "OUT_DIR"), _absolute(env, "WORK_DIR")
    for name, path in (("OUT_DIR", out_dir), ("WORK_DIR", work_dir)):
        if os.path.lexists(path):
            raise JobError(f"{name} already exists")
    for name, path in (("REPO_DIR", repo_dir), ("SLICES_DIR", slices_dir), ("REVIEWS_DIR", reviews_dir)):
        if not path.is_dir():
            raise JobError(f"{name} is not a directory")
    return Settings(issue, base, slices, baseline, kind, repo_dir, slices_dir, reviews_dir, out_dir, work_dir,
                    job.read_output_path(env))


def write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def assemble(env: Mapping[str, str]) -> None:
    s = read_settings(env)
    git = Git(s.work_dir, s.base)
    s.work_dir.mkdir(mode=0o700)
    git.init()
    try:
        base_time = git.fetch_base(s.repo_dir)
    except GitError as err:
        raise JobError(f"the base commit cannot be read from REPO_DIR: {err}") from None
    try:
        slices_fd = open_dir(str(s.slices_dir))
    except ArtifactError:
        raise JobError("SLICES_DIR cannot be opened") from None
    try:
        for note in list_artifacts(slices_fd, [x.id for x in s.slices]):
            print(note)
        stack = Stack(s.base)
        entries = [assemble_slice(git, slices_fd, s.reviews_dir, s.issue, x, s.baseline, base_time, stack, s.kind) for x in s.slices]
    finally:
        os.close(slices_fd)
    positions = [e["id"] for e in entries if e["outcome"] == "candidate"]
    branches = [e["branch"] for e in entries if e["outcome"] == "draft"]
    if positions:
        git.run("update-ref", f"refs/heads/{stack_branch(s.issue)}", stack.tip, "0" * 40)
        branches.append(stack_branch(s.issue))
    bundle = None
    s.out_dir.mkdir(mode=0o755)
    if branches:
        target = s.out_dir / BUNDLE
        try:
            git.run("bundle", "create", "-q", str(target), f"^{s.base}", *(f"refs/heads/{b}" for b in branches))
            data = read_regular(target, MAX_BUNDLE_BYTES)
        except (GitError, ArtifactError):
            shutil.rmtree(s.out_dir, ignore_errors=True)
            raise JobError("the bundle could not be made") from None
        bundle = {"name": BUNDLE, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    land = {
        "version": LAND_VERSION, "issue": s.issue, "base_sha": s.base, "baseline": s.baseline, "bundle": bundle,
        "has_stack": bool(positions), "positions": positions, "slices": entries,
    }
    try:
        text = land_text(land)
    except LandError as err:
        shutil.rmtree(s.out_dir, ignore_errors=True)
        raise JobError(f"land.json would be invalid: {err}") from None
    write_new(s.out_dir / LAND, text)
    for entry in entries:
        # already_green names the base it passed on: the reason is a short word in land.json, so the sha is said here (I1).
        why = f"already_green at {s.base[:12]}" if entry["reason"] == "already_green" else entry["reason"]
        print(f"slice {entry['id']}: {entry['outcome']} ({why})")
    job.write_outputs(s.output, {
        "positions": ",".join(positions), "gate_matrix": json.dumps(positions, separators=(",", ":")),
        "has_stack": "true" if positions else "false", "land_sha256": hashlib.sha256(text).hexdigest(), "assembled": "true",
    })


JOBS = {"run": assemble}


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    if len(argv) != 1 or argv[0] not in JOBS:
        print(f"usage: assemble.py {{{'|'.join(JOBS)}}}", file=sys.stderr)
        return 2
    return job.run_job(JOBS[argv[0]], environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
