"""The review job (`review[i]`): one slice's patch in, one `review-<id>` artifact out.

The job runs two steps, ``python3 -I pivotal/engine/reviewjob.py prepare`` and ``... run``, with data in env and files
(F2). It is ``contents: read``, holds no GitHub token and checks the repo out with ``persist-credentials: false`` (F1).
It runs after ``build`` on a fresh VM, from three checkouts and one download: the engine (``ENGINE_DIR``), the
repo at the commit the builder built on (``REPO_DIR``; ``prepare`` prints it exactly as the build job's does), the base
commit of the run (``BASE_DIR``, the trusted source of the agent-context files) and the build's artifact
``slice-<id>`` (``SLICE_DIR``).

``prepare`` holds no secret and runs no repo code (buildjob.prepare). ``run`` holds the door secrets and handles the
patch, which is hostile data, so it follows the engine pattern of engine/buildjob.py, whose helpers it uses: ``main``
makes the process non-dumpable and empties ``os.environ`` first, the reviewer's single token reaches only the one
agent call, every file of ``engine/`` is digested before the agent starts and checked again before the engine answers,
processes that left the agent's group are swept, the engine owns GITHUB_OUTPUT and the artifact is made only by the
engine, from bytes, with the files read back and scanned for every held secret.

``run``, in order:

1. Read ``SLICE_DIR`` as untrusted input (F5): a directory of regular files only, ``result.json`` through
   build.check_result and for this slice and round, status ``ok``, a patch under the size cap. Anything else refuses
   (exit 1, nothing written): there is nothing to review.
2. The patch is scanned for every held secret, then its file names are read by ``git apply --numstat -z`` and checked
   (no absolute path, no ``..``, no ``.git`` entry; at most review.MAX_FILES files). It is applied to the repo with
   ``git apply`` under the hardened git of buildjob.git (hooks, fsmonitor and signing off, a clean config, a fixed
   PATH and identity, ``lock_git`` first), never committed. A symlink it creates must stay inside the tree.
3. The agent-context files (``buildjob.CONTEXT_NAMES``, ``CONTEXT_DIRS``, ``CONTEXT_PREFIXES``, wherever they are in the tree)
   are removed from the patched tree and restored from ``BASE_DIR``, so a hostile patch cannot instruct its reviewer.
   The patch's own changes to them stay in the diff the reviewer is given, as data to judge.
4. Route the ``review`` tier without the builder's vendor (route.review_chain, the builder's ``built_by`` is the
   checked result.json's). If no entry can run, the verdict is ``missing`` with the reason ``no_route``: never a
   same-vendor review (F5). Each skipped entry is a ``fell_back:`` line.
5. The brief (engine/prompts/review.md, read before the agent started) and the data are written to a 0600 prompt file:
   the slice title and goal, the changed files and the patch, each in a fenced block longer than any backtick run in
   it and marked "untrusted data". claude's argv holds a fixed sentence and the file's path. pi and omp get the file's
   text inline in argv, with --no-session and --no-tools, so one review is one model request; a prompt over
   MAX_INLINE_PROMPT_BYTES (96 KiB, the Linux limit for one argument is 128 KiB) is patch_too_large before any call.
   The agent runs in the
   patched tree, and the tree is read-only for it (a hindrance: it is the same user) and is digested before and after:
   a changed file, a created or removed one, a moved HEAD is ``reviewer_wrote`` and the verdict is missing.
6. The agent's log is the review. review.parse decides in code: the exact last line, the SHA of the patch, a
   ``Covered:`` line for every changed file and none for any other (see engine/review.py). The text is scanned for the
   secrets the job holds before it is written; a hit is ``secret_in_review`` and none of the text is published.
7. ``verdict``, ``review.md`` and ``reviewed_by`` are published (``publish``), read back and scanned again, and only
   then does the engine write ``published=true`` and ``review_ok`` (true only for READY or NEEDS FIXES). The step exits
   0 once it has published, even for a missing verdict, and a workflow step after the upload fails the job when
   ``review_ok`` is not ``true``: the slice is then a draft (assemble reads the artifact), and the run is red. A silent
   reviewer (exit 0, nothing printed, the tree unchanged) is a missing verdict whose reason ``agent_no_output`` review.md
   records; the land and report reason reads ``verdict_missing``, as for every missing verdict. A reviewer that exits
   non-zero is ``agent_exit_N`` (N its exit status), with the first line of its errlog after the held values are removed
   and only plain characters are kept, at most 160 (exit_reason). A provider error in the reviewer's log (stopReason error
   for pi and omp, is_error for claude) is skipped like a rate limit: the next reviewer is tried, and when the chain ends
   the reason is ``agent_error[:CODE][: TEXT]``, TEXT the errorMessage shown as the errlog line is (agent_error_reason).
   Nothing else of the errlog or the stdout log is shown.

Interface for the tickets that do not exist yet. ``assemble`` downloads ``review-<id>`` and calls ``review.load`` on
the directory (any ReviewError is a missing verdict), then ``review.accept`` with the sha256 of the ``slice.patch`` it is
about to land, the files that patch changes (``review.quote_path`` form) and the builder from ``result.json``; "" means
the slice may land. The SHA a verdict names is that digest, not a git commit id: the commit ids assemble creates depend
on the stack, the patch does not. ``publish`` and ``gate`` do not read this artifact.

Known limits, stated plainly. Read-only is enforced by detection (the digest) and a read-only tree, not by the CLI: the
per-worker read-only switch is a table, ``READ_ONLY_ARGS``, that stays empty until T0's measured CLI invocations land
(docs/engines.md); a wrong guessed flag would break a real review. run.sh splits the agent's stdout (the JSON log) and
stderr (the errlog) into separate files; the verdict is extracted from the final assistant message in the JSON log, so
a CLI that prints a diagnostics line to stderr after the verdict does not make the verdict missing. The agent reads files
as the job user and can read what the runner leaves readable (see buildjob.py on ptrace_scope, a container, a process
that changes uid). Nothing here has run on real GitHub.
"""

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):  # run as a script under python3 -I
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import build, buildjob, job, lines, piagent, plan, review, route, workers  # noqa: E402
from engine.buildjob import EngineModified, Paths, StrayError  # noqa: E402
from engine.job import JobError  # noqa: E402

if __package__ in (None, ""):
    sys.path.remove(str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR_DEFAULT = ROOT
# The engine files the post-agent steps use. Read once, before the agent starts (see buildjob.Engine).
ENGINE_FILES = ("engine/run.sh", "engine/prompts/review.md")

# The budgets (F8). timeout-minutes is derived from them: test_factory checks that they agree. The worst case is
# MAX_ENTRY_TRIES agent runs that each ended in a skip; the overhead covers checkouts, the artifact download, git, the
# digests and the upload.
AGENT_SECONDS = 900
MAX_ENTRY_TRIES = buildjob.MAX_ENTRY_TRIES
JOB_OVERHEAD_MINUTES = 10
JOB_MINUTES = math.ceil(MAX_ENTRY_TRIES * AGENT_SECONDS / 60) + JOB_OVERHEAD_MINUTES + buildjob.SETUP_MINUTES  # the setup step

RESULT, PATCH, SUMMARY = "result.json", "slice.patch", "summary.md"
SLICE_FILES = frozenset({RESULT, PATCH, SUMMARY})
MAX_RESULT_BYTES = 64 * 1024
MAX_PROMPT_PATCH_BYTES = 512 * 1024  # a larger patch is not put in a brief: the verdict is missing (patch_too_large)
MAX_INLINE_PROMPT_BYTES = 96 * 1024  # Linux argv limit is 128 KiB; prompt text inline for pi/omp is capped here
WALK_SECONDS = 120  # the digest of the whole work tree

# The read-only switch per worker. Empty until T0's measured CLI invocations land (see the module docstring).
READ_ONLY_ARGS: dict[str, tuple[str, ...]] = {}
INSTRUCTION = "Read the file {path} and do the review it describes. Print the review as your answer. Then stop."


# --- the input artifact -------------------------------------------------------------------------------------


def read_input(directory: str, slice_: plan.Slice, number: int) -> tuple[dict, bytes]:
    """The build's artifact as (checked result, patch), or JobError. Untrusted data: regular files only, no link, no
    FIFO, no extra name, every size capped, and a result that is for this slice and round and is ok."""
    path = Path(directory) if directory else None
    try:
        if path is None or not stat.S_ISDIR(os.lstat(path).st_mode):
            raise JobError("SLICE_DIR is not a directory")
        names = set(os.listdir(path))
    except OSError:
        raise JobError("SLICE_DIR cannot be read") from None
    if RESULT not in names or not names <= SLICE_FILES:
        raise JobError("the slice artifact is not result.json, slice.patch and summary.md")
    try:
        data = json.loads(review.read_regular(path, RESULT, MAX_RESULT_BYTES).decode("utf-8"),
                          object_pairs_hook=plan._no_duplicate_keys)
        result = build.check_result(data)
        if result["slice"] != slice_.id or result["round"] != number:
            raise JobError("the slice artifact is for another slice or round")
        if result["status"] != "ok":
            raise JobError("the build did not succeed: there is nothing to review")
        if PATCH not in names:
            raise JobError("the slice artifact has no patch")
        patch = review.read_regular(path, PATCH, buildjob.MAX_PATCH_BYTES)
    except (build.ResultError, review.ReviewError) as err:  # their messages name a rule and never repeat a value
        raise JobError(f"the slice artifact is not valid: {err}") from None
    except (ValueError, RecursionError, plan.SliceError):  # JSON (a deep `[[[` too), UTF-8 or a repeated key
        raise JobError("the slice artifact is not valid") from None
    if not patch:
        raise JobError("the slice patch is empty")
    return result, patch


# --- the patch ----------------------------------------------------------------------------------------------


def shown_whole(patch: bytes) -> bool:
    """True when the brief shows exactly the bytes that are applied and hashed: valid UTF-8 with no character the
    brief would drop (control, CR, bidi, zero-width, format), so a hidden character cannot reach the tree unseen."""
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(c in "\n\t" or c.isprintable() for c in text)


class Refused(Exception):
    """The verdict is missing, for a reason from review.MISSING_REASONS."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# The agent_exit_N reason (exit_reason). The errlog is read whole only up to EXIT_READ_BYTES; a larger one has no text.
EXIT_READ_BYTES = 64 * 1024
EXIT_CHARS = 160
AGENT_EXIT_WORD = re.compile(r"error:([1-9][0-9]{0,2})")
_EXIT_KEEP = re.compile(r"[A-Za-z0-9 .,:;_/=()-]")


def shown_text(data: bytes, values: Mapping[str, bytes]) -> str:
    """The first non-empty line of ``data``, with every held value removed (build.redact), then only the characters of
    _EXIT_KEEP, whitespace collapsed and at most EXIT_CHARS long. "" when there is no such line."""
    line = next((part for part in build.redact(data, values).split(b"\n") if part.strip()), b"")
    spaced = re.sub(r"\s", " ", line.decode("utf-8", errors="replace"))
    return " ".join("".join(_EXIT_KEEP.findall(spaced)).split())[:EXIT_CHARS].strip()


def exit_excerpt(errlog: Path, values: Mapping[str, bytes]) -> str:
    """shown_text of an agent's errlog. "" when the errlog cannot be read through the capped reader. Only the errlog is
    read; the stdout log is never read for this."""
    try:
        data = review.read_regular(errlog.parent, errlog.name, EXIT_READ_BYTES)
    except review.ReviewError:
        return ""
    return shown_text(data, values)


def agent_error_reason(error: str, message: bytes, held: build.Held) -> str:
    """The reason of a run whose reviewer reported a provider error: ``error`` (``agent_error`` or ``agent_error:CODE``),
    then ``: TEXT`` when the message has a line to show (shown_text). When a held value is unreadable or too short to
    hold (build.held_values), the redaction cannot be trusted, so the text is left out (fail closed)."""
    text = ""
    if message and not held.problem and not held.skipped:
        text = shown_text(message, held.values)
    return error + (f": {text}" if text else "")


def exit_reason(reason: str, errlog: Path | None, held: build.Held) -> str:
    """The reason of a run whose last agent word is error:N (N from 1 to 255): ``agent_exit_N``, then ``: TEXT`` when the
    errlog has a line to show. Any other reason is returned unchanged. When a held value is unreadable or too short to
    hold (build.held_values), the redaction cannot be trusted, so the reason is the bare word (fail closed)."""
    match = AGENT_EXIT_WORD.fullmatch(reason)
    if match is None or int(match.group(1)) > 255:
        return reason
    text = ""
    if errlog is not None and not held.problem and not held.skipped:
        text = exit_excerpt(errlog, held.values)
    return f"agent_exit_{match.group(1)}" + (f": {text}" if text else "")


def git(paths: Paths, *args: str, patch: bytes | None = None):
    """buildjob.git, with the patch on stdin when there is one: the same environment and the same -c settings."""
    env = buildjob.git_env(paths)
    return subprocess.run(
        [buildjob.SYSTEM_TOOLS["git"], *buildjob.GIT_SAFE, *buildjob.IDENTITY, *args], cwd=paths.repo, env=env,
        capture_output=True, check=False, timeout=buildjob.GIT_SECONDS,
        **({"input": patch} if patch is not None else {"stdin": subprocess.DEVNULL}),
    )


def numstat_paths(out: bytes) -> list[bytes]:
    """The paths in ``git apply --numstat -z``: ``added TAB deleted TAB path NUL``, and for a rename or copy
    ``added TAB deleted TAB NUL old NUL new NUL``. Both names of a rename are changed files. Raises ValueError."""
    tokens = out.split(b"\0")
    found: list[bytes] = []
    i = 0
    while i < len(tokens):
        token, i = tokens[i], i + 1
        if not token:
            continue
        fields = token.split(b"\t", 2)
        if len(fields) != 3:
            raise ValueError("unreadable numstat")
        if fields[2]:
            found.append(fields[2])
        else:
            if i + 2 >= len(tokens) or not tokens[i] or not tokens[i + 1]:
                raise ValueError("unreadable numstat")
            found += tokens[i:i + 2]
            i += 2
    return list(dict.fromkeys(found))


def unsafe_name(raw: bytes) -> bool:
    """A name that is empty, absolute, holds an empty, ``.`` or ``..`` segment, a NUL, or any ``.git`` segment."""
    if not raw or raw.startswith(b"/") or b"\0" in raw:
        return True
    return any(part in (b"", b".", b"..") or part.lower() == b".git" for part in raw.split(b"/"))


def changed_files(paths: Paths, patch: bytes) -> list[bytes]:
    """The files the patch changes, from git's own reading of it. Refused: a name outside the tree, too many files,
    a patch git cannot read."""
    done = git(paths, "apply", "--numstat", "-z", patch=patch)
    if done.returncode != 0:
        raise Refused("patch_does_not_apply")
    try:
        names = numstat_paths(done.stdout)
    except ValueError:
        raise Refused("patch_does_not_apply") from None
    if not names:
        raise Refused("patch_does_not_apply")
    if any(unsafe_name(n) for n in names):
        raise Refused("unsafe_patch")
    if len(names) > review.MAX_FILES:
        raise Refused("too_many_files")
    return names


def entries(repo: Path) -> set[bytes]:
    """The relative names of every non-directory entry of the work tree outside ``.git`` (a link to a directory is
    one). Nothing is followed."""
    found: set[bytes] = set()
    for top, dirs, files in os.walk(repo, onerror=buildjob.walk_error):
        if top == str(repo):
            dirs[:] = [d for d in dirs if d != ".git"]
        base = os.path.relpath(top, repo)
        for name in [*files, *(d for d in dirs if os.path.islink(os.path.join(top, d)))]:
            found.add(os.fsencode(name if base == "." else os.path.join(base, name)))
    return found


def apply_patch(paths: Paths, patch: bytes, declared: list[bytes]) -> list[bytes]:
    """Apply to the work tree, not the index, and commit nothing. Returns the changed files: the names git reads
    from the patch and every entry the apply made or removed (``numstat`` lists a rename by its new name only). A
    symlink the patch creates must stay inside the tree."""
    before = entries(paths.repo)
    done = git(paths, "apply", "--binary", "--whitespace=nowarn", patch=patch)
    if done.returncode != 0:
        raise Refused("patch_does_not_apply")
    names = list(dict.fromkeys([*declared, *sorted(before ^ entries(paths.repo))]))
    if len(names) > review.MAX_FILES:
        raise Refused("too_many_files")
    for raw in names:
        full = paths.repo / os.fsdecode(raw)
        try:
            info = os.lstat(full)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(full)
            if target.startswith("/") or ".." in target.split("/"):
                raise Refused("unsafe_patch")
    return names


# --- the read-only check ------------------------------------------------------------------------------------


def tree_state(repo: Path) -> str:
    """sha256 over every entry of the work tree outside ``.git``: its path, type, execute bit and content (a link's
    target). Nothing is followed. A tree the walk cannot finish raises OSError."""
    digest = hashlib.sha256()
    deadline = time.monotonic() + WALK_SECONDS
    for top, dirs, files in os.walk(repo, onerror=buildjob.walk_error):
        if time.monotonic() > deadline:
            raise OSError("the work tree took too long to read")
        dirs.sort()
        if top == str(repo):
            dirs[:] = [d for d in dirs if d != ".git"]
        for name in sorted([*dirs, *files]):
            path = os.path.join(top, name)
            info = os.lstat(path)
            kind = stat.S_IFMT(info.st_mode)
            digest.update(os.fsencode(os.path.relpath(path, repo)) + b"\0" + f"{kind:o} {int(bool(info.st_mode & 0o111))}\0".encode())
            if stat.S_ISREG(info.st_mode):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as fh:
                    while chunk := fh.read(1 << 20):
                        digest.update(chunk)
            elif stat.S_ISLNK(info.st_mode):
                digest.update(os.fsencode(os.readlink(path)))
            digest.update(b"\1")
    return digest.hexdigest()


# --- the brief ----------------------------------------------------------------------------------------------


class ReviewEngine(buildjob.Engine):
    """buildjob.Engine over this job's files: run.sh and the review brief are held as read before the agent started."""

    def __init__(self, root: Path, line: "lines.Line | None" = None):
        self.root = root
        self.line = line or lines.load_line(lines.DEFAULT, root)  # only its preset is used here: the brief is always review.md
        self.data = {name: self._read(root / name) for name in ENGINE_FILES}
        self.script = self.data["engine/run.sh"].decode("utf-8")
        self.brief = self.data["engine/prompts/review.md"].decode("utf-8")
        self.tree = buildjob.tree_digest(root / "engine")
        self.catalog = piagent.read_catalog(root)  # kaggle/catalog.json, read once here, before any agent can change it


def write_prompt(path: Path, brief: str, slice_: plan.Slice, patch_sha: str, files: list[str], patch: bytes,
                 values: Mapping[str, bytes]) -> None:
    """The prompt file. The title, the goal, the file names and the patch are data in fenced blocks; the engine's own
    lines (the SHA to write back) are outside them."""
    text = patch.decode("utf-8", errors="replace")
    parts = [
        brief,
        buildjob.data_block("Request title", slice_.title, values),
        buildjob.data_block("Request goal", slice_.goal, values),
        f"The patch SHA-256, for your `Reviewed:` line: {patch_sha}\n",
        buildjob.data_block("Changed files, one `Covered:` line each, exactly as written here", "\n".join(files), values),
        buildjob.data_block("The patch, as a diff against the checkout's base", text, values),
    ]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


def agent_argv(entry: route.Entry, prompt: Path, pins: "Mapping[str, workers.Pin] | None" = None,
               prompt_text: str | None = None) -> list[str]:
    # JSON mode: the verdict is extracted from the final assistant message, not from whatever the CLI
    # prints after it. pi/omp: `pi/omp -p --mode json --model MODEL --no-session --no-tools PROMPT_TEXT`
    # (docs/engines.md Headless flags). claude: `claude --output-format json --model MODEL PROMPT`
    # (docs/engines.md Headless flags). Only T0-measured flags are used. The read-only switch
    # (READ_ONLY_ARGS, empty until measured) keeps its place before the JSON flag.
    extra_before: tuple[str, ...] = READ_ONLY_ARGS.get(entry.worker, ())
    if entry.worker in ("pi", "omp"):
        # pi/omp: pass the prompt text inline (up to MAX_INLINE_PROMPT_BYTES), not a read-the-file sentence.
        # --no-session and --no-tools are measured for omp (run 43562562687); for pi they are from --help, unmeasured.
        text = prompt_text if prompt_text is not None else INSTRUCTION.format(path=prompt)
        argv = [entry.worker, *extra_before, "-p", "--mode", "json", "--model", route.model_arg(entry),
                "--no-session", "--no-tools", text]
    elif entry.worker == "claude":
        argv = [entry.worker, *extra_before, "-p", "--output-format", "json", "--model", route.model_arg(entry),
                INSTRUCTION.format(path=prompt)]
    else:
        # Other workers fall back to text mode via route.worker_argv (the build job's shape).
        argv = route.worker_argv(entry, INSTRUCTION.format(path=prompt))
        if extra_before:
            argv = [argv[0], *extra_before, *argv[1:]]
    return workers.command(argv, pins)


# --- the review ---------------------------------------------------------------------------------------------


@dataclass
class ReviewPaths(Paths):
    base: Path = Path("/nonexistent")  # the trusted checkout of the run's base commit


@dataclass
class State:
    slice_id: str
    round: int
    base: str
    built_by: str
    patch_sha: str = ""
    files: list[str] = field(default_factory=list)  # quoted, sorted
    entry: route.Entry | None = None
    fell_back: list[dict] = field(default_factory=list)  # the route's fall-through items (route.FellBack.as_dict)
    token_name: str = ""
    logs: list[Path] = field(default_factory=list)  # stdout (JSON) agent logs, in order. Never uploaded.
    errlogs: list[Path] = field(default_factory=list)  # stderr agent logs, in order. Never uploaded.
    parsed: review.Parsed | None = None
    before: str = ""  # tree_state of the prepared tree


def _environb(env: Mapping[str, str]) -> dict[bytes, bytes]:
    return buildjob._environb(env)


def _extract_review_text(log: Path, entry: "route.Entry | None") -> "tuple[bytes, str]":
    """Extract the final assistant text from a JSON-mode agent log. Returns (text_bytes, reason).

    For pi and omp (--mode json), reads the JSONL stdout log capped at review.MAX_RAW_LOG_BYTES; extracts
    the text field from the content of the last ``message_end`` event whose message.role is
    ``assistant`` (pi/omp docs json.md, message-types.md:
      event type "message_end" with field "message"; AssistantMessage with role
      "assistant" and content list of TextContent blocks with type "text" and field "text").
    For claude (--output-format json), reads the single JSON document and returns the "result" field.
    For other workers, reads the log as-is (text mode, unchanged behaviour).
    When parsing fails the reason is "not_text"; on success reason is "".
    When the last assistant message_end has stopReason 'error' (pi/omp) or the JSON claude result
    has is_error true, the reason is "agent_error:CODE" where CODE is a 3-digit status code
    from errorMessage (pi/omp) or absent (claude).
    """
    worker = entry.worker if entry is not None else ""
    # Read the raw log, capped at MAX_RAW_LOG_BYTES (a runaway JSONL stream is refused, not read whole).
    try:
        raw = review.read_regular(log.parent, log.name, review.MAX_RAW_LOG_BYTES)
    except review.ReviewError:
        return b"", "not_text"

    if worker in ("pi", "omp"):
        # pi/omp --mode json: JSONL, one JSON object per line. The authoritative final assistant text
        # is in the last ``message_end`` event (json.md: "This is the authoritative final message")
        # whose message.role == "assistant". Collect text blocks from message.content.
        # Event shape: {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"..."}],...}}
        # (json.md, message-types.md: AssistantMessage.content is (TextContent|ThinkingContent|ToolCall)[])
        # An error event has stopReason "error", empty content, and an errorMessage field.
        last_text: str | None = None
        last_stop_reason: str | None = None
        last_error_message: str | None = None
        try:
            for line in raw.split(b"\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    return b"", "not_text"
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != "message_end":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                # Concatenate all TextContent blocks (type "text", field "text").
                parts = [b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
                last_text = "".join(parts)
                last_stop_reason = msg.get("stopReason")
                last_error_message = msg.get("errorMessage")
        except Exception:  # noqa: BLE001
            return b"", "not_text"
        if last_text is None:
            return b"", "not_text"
        if last_stop_reason == "error":
            # Extract 3-digit status code from errorMessage, e.g. "503" from "high demand (503)"
            code = None
            if last_error_message:
                match = re.search(r"\b([45]\d{2})\b", last_error_message)
                if match:
                    code = match.group(1)
            detail = f"agent_error:{code}" if code else "agent_error"
            return b"", detail
        return last_text.encode("utf-8", errors="replace"), ""

    if worker == "claude":
        # claude --output-format json: a single JSON document with a "result" field holding the
        # final assistant text, and an "is_error" field indicating provider error.
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                return b"", "not_text"
            if obj.get("is_error"):
                return b"", "agent_error"
            result = obj.get("result")
            if not isinstance(result, str):
                return b"", "not_text"
            return result.encode("utf-8", errors="replace"), ""
        except (json.JSONDecodeError, ValueError, Exception):  # noqa: BLE001
            return b"", "not_text"

    # Other workers: text mode; return the raw log as-is (unchanged behaviour).
    return raw, ""


def inline_prompt(path: Path) -> str:
    """The prompt file's text, for pi/omp's argv: the same bytes the file holds, read through the capped reader. A
    prompt over MAX_INLINE_PROMPT_BYTES is refused before any model call (patch_too_large); any other failure is
    refused as internal. Never an exception: the caller turns a Refused into a missing verdict."""
    try:
        if os.lstat(path).st_size > MAX_INLINE_PROMPT_BYTES:
            raise Refused("patch_too_large")
        data = review.read_regular(path.parent, path.name, MAX_INLINE_PROMPT_BYTES)
    except (OSError, review.ReviewError):
        raise Refused("internal") from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("not_text") from None


def _agent(env, paths, state, slice_, patch, values) -> str:
    write_prompt(paths.prompt, paths.engine.brief, slice_, state.patch_sha, state.files, patch, values)
    prompt_text = None
    if state.entry and state.entry.worker in ("pi", "omp"):
        prompt_text = inline_prompt(paths.prompt)
    n = len(state.logs) + 1
    log = paths.logs / f"agent-{n}.log"
    errlog = paths.logs / f"agent-{n}.err"
    state.logs.append(log)
    state.errlogs.append(errlog)
    with piagent.call_dir(state.entry, env, paths.engine.catalog) as config:  # pi's fresh agent dir, removed after the call
        extra = ("--agent-dir", str(config)) if config is not None else ()
        word = buildjob.shell(
            env, paths, AGENT_SECONDS, "agent_run", "--max-time", str(AGENT_SECONDS), "--log", str(log),
            "--errlog", str(errlog), "--token-env", state.token_name, *extra, "--",
            *agent_argv(state.entry, paths.prompt, paths.pins, prompt_text),
            token_name=state.token_name,
        )
    return word if buildjob.AGENT_WORD.fullmatch(word) else "error:70"


def _choose_and_run(env, paths, state, slice_, patch, chain, builder, ref) -> str:
    """Route, run the reviewer and take the next entry when its own log says to skip this one. "" when an agent
    finished ok, else the reason the verdict is missing."""
    quota = buildjob.kaggle_quota_refused(env)  # a refused push skips the kaggle reviewers as `quota`, and none is probed
    down, rejected = (frozenset(), frozenset()) if quota else buildjob.kaggle_down(env, chain)
    limited: frozenset[str] = frozenset()
    absent = buildjob.installed_absent(env, paths, chain)
    last_retriable_error: str = ""  # the last provider error: the reason when the chain ends with no verdict
    for attempt in range(MAX_ENTRY_TRIES):
        probe = route.make_probe(env, down=down, limited=limited, rejected=rejected, quota=quota, absent=absent)
        chosen = route.route(chain, probe)
        state.fell_back = [f.as_dict() for f in chosen.fell_back]
        if chosen.status != "ok":
            # Chain exhausted: if a reviewer reported a provider error, that error is the reason, not no_route
            if last_retriable_error:
                return last_retriable_error
            return "no_route"
        entry = chosen.entry
        # The cross-vendor rule once more, on the entry itself: the chain was built without the builder's vendor.
        if entry.vendor is None or entry.vendor == builder.vendor:
            return "no_route"
        state.entry = entry
        state.token_name = next(n for n in route.secret_names(entry) if env.get(n))
        if attempt:
            _again(paths, patch, ref, state)  # the failed reviewer's writes must not cost this one its verdict (R3)
        held = build.held_values([state.token_name], _environb(env))
        if reason := buildjob.install_refusal(entry, paths):  # an earlier entry's agent changed the CLI this entry would run
            return reason
        word = _agent(env, paths, state, slice_, patch, held.values)
        # A provider error in the JSON log, for both ok and error:N exits, is skipped like a rate limit: the next entry
        # is tried. When the chain ends, the last error is the reason (agent_error_reason shows its code and text).
        error, message = _check_agent_error(state.logs[-1], state.entry)
        if error:
            limited |= {entry.spec}
            last_retriable_error = agent_error_reason(error, message, held)
            continue
        if word == "ok":
            return ""
        text = buildjob._tail_bytes(state.logs[-1]).decode("utf-8", errors="replace")  # the last 4 MiB; a FIFO or a link reads as "", never waited on
        why = route.classify_failure("agent", text, route.AGENT_SKIP_PATTERNS)
        if why == "rate-limited":
            limited |= {entry.spec}
        elif why == "endpoint down":
            down |= {entry.spec}
        elif why == "rejected token":
            rejected |= {entry.spec}
        elif entry.door == "kaggle" and (seen := buildjob.recheck_endpoint(entry, env)) != "up":
            gone, refused = buildjob.endpoint_skips(seen, chain)  # the session ended while the reviewer ran: every Kaggle entry falls through (R3)
            down, rejected = down | gone, rejected | refused
        else:
            return word
    # Loop exhausted (MAX_ENTRY_TRIES): if we had a retriable agent_error, report it
    if last_retriable_error:
        return last_retriable_error
    return "no_route"


def _check_agent_error(log: Path, entry: "route.Entry | None") -> tuple[str, bytes]:
    """Check if the agent log reports a provider error (stopReason error for pi/omp, is_error for claude). Returns
    (error, message): error is ``agent_error:CODE`` (CODE a 4xx or 5xx status in the pi/omp errorMessage) or
    ``agent_error``; message is that errorMessage, or the claude result, as UTF-8. ("", b"") when there is no error."""
    worker = entry.worker if entry is not None else ""
    try:
        raw = review.read_regular(log.parent, log.name, review.MAX_RAW_LOG_BYTES)
    except review.ReviewError:
        return "", b""
    if worker in ("pi", "omp"):
        last_stop_reason: str | None = None
        last_error_message: object = None
        try:
            for line in raw.split(b"\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    return "", b""
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != "message_end":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                last_stop_reason = msg.get("stopReason")
                last_error_message = msg.get("errorMessage")
        except Exception:  # noqa: BLE001
            return "", b""
        if last_stop_reason == "error":
            message = _text_bytes(last_error_message)
            code = None
            match = re.search(r"\b([45]\d{2})\b", message.decode("utf-8", errors="replace"))
            if match:
                code = match.group(1)
            return (f"agent_error:{code}" if code else "agent_error"), message
    elif worker == "claude":
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError, Exception):  # noqa: BLE001
            return "", b""
        if isinstance(obj, dict) and obj.get("is_error"):
            return "agent_error", _text_bytes(obj.get("result"))
    return "", b""


def _text_bytes(value: object) -> bytes:
    """A JSON string field as UTF-8 for shown_text; anything else (a missing or non-string field) is no text."""
    return value.encode("utf-8", errors="replace") if isinstance(value, str) else b""


def _prepare(paths, patch: bytes, ref: str, state: State) -> None:
    """The tree a reviewer is shown: `ref` with the patch applied, the agent context restored, git locked, read-only.
    Sets state.files and state.before (the digest the tree must still have after the reviewer)."""
    if buildjob.lock_git(paths, ref):
        raise Refused("unsafe_attributes")
    head = buildjob.git(paths, "rev-parse", "HEAD", text=True)
    if head.returncode != 0 or head.stdout.strip() != ref:
        raise Refused("bad_history")
    names = apply_patch(paths, patch, changed_files(paths, patch))
    state.files = review.files_of(names)
    try:
        buildjob.restore_context(paths.repo, paths.base)  # shared with the build (R15)
    except OSError:
        raise Refused("internal") from None
    if unsafe := buildjob.lock_git(paths, ref):  # a driver the patch names is refused before a model sees the tree
        raise Refused(unsafe)
    try:
        state.before = tree_state(paths.repo)
    except OSError:
        raise Refused("internal") from None
    buildjob.set_writable(paths.repo, False)  # a hindrance for the reviewer, not a boundary: it runs as this user


def _again(paths, patch: bytes, ref: str, state: State) -> None:
    """Before a further reviewer: whatever the failed one wrote is gone and the prepare sequence is redone (R3, round 3)."""
    paths.sweep()  # a process the failed reviewer left behind must not write into the tree being reset
    paths.engine.verify()
    try:
        buildjob.set_writable(paths.repo, True)
        if buildjob.lock_git(paths, ref):  # no config or attributes of the failed reviewer's making runs under git
            raise Refused("unsafe_attributes")
        buildjob.git_ok(paths, "reset", "-q", "--hard", ref)
        buildjob.git_ok(paths, "clean", "-qffdx")
    except (JobError, OSError):
        raise Refused("internal") from None
    _prepare(paths, patch, ref, state)


def review_slice(env: Mapping[str, str], paths: Paths, slice_: plan.Slice, ref: str, result: dict, patch: bytes,
                 state: State) -> review.Parsed:
    """Steps 2 to 6 of the module docstring. Always returns a Parsed: READY, NEEDS FIXES or missing with a reason."""
    try:
        return _review(env, paths, slice_, ref, result, patch, state)
    except Refused as why:
        return review.Parsed(review.MISSING, why.reason, "")


def _review(env, paths: ReviewPaths, slice_, ref, result, patch, state) -> review.Parsed:
    try:
        # The preset the run builds with (plan's REVIEWER fact is computed on it): the line's own, else the caller's.
        line = lines.resolve(env.get("LINE") or lines.DEFAULT, env.get("PRESET_JSON", ""), Path(env.get("ENGINE_DIR") or ENGINE_DIR_DEFAULT))
        preset = plan.load_preset(env, lines.preset_for(line, "" if line.preset else job.read_preset_name(env.get("PRESET", ""))))
        builder = route.parse_entry(result["built_by"])
    except (JobError, ValueError, lines.LineError):
        raise Refused("bad_spec") from None
    reviewers = route.review_chain(preset, builder)
    if not all(route.valid_spec(e.spec) for e in reviewers):
        raise Refused("bad_spec")
    scanned = build.scan(patch, [], _environb(env))
    if scanned.result == build.RESULT_LEAK:
        raise Refused("secret_in_patch")
    if scanned.result != build.RESULT_OK:
        raise Refused("scan_incomplete")
    if len(patch) > MAX_PROMPT_PATCH_BYTES:
        raise Refused("patch_too_large")
    if not shown_whole(patch):
        raise Refused("not_text")
    state.patch_sha = hashlib.sha256(patch).hexdigest()
    _prepare(paths, patch, ref, state)
    reason = _choose_and_run(env, paths, state, slice_, patch, reviewers, builder, ref)
    paths.sweep()  # a process the reviewer left behind must not touch the tree the digest reads
    paths.engine.verify()
    if reason:
        errlog = state.errlogs[-1] if state.errlogs else None
        raise Refused(exit_reason(reason, errlog, build.held_values([state.token_name], _environb(env))))
    if unsafe := buildjob.lock_git(paths, ref):
        raise Refused(unsafe)
    after_head = buildjob.git(paths, "rev-parse", "HEAD", text=True)
    try:
        after = tree_state(paths.repo)
    except OSError:
        raise Refused("internal") from None
    if after_head.returncode != 0 or after_head.stdout.strip() != ref or after != state.before:
        raise Refused("reviewer_wrote")
    # agent_no_output: the stdout log is empty AND the errlog (if present) is also empty.
    # For JSON-mode workers the stdout log holds JSONL, so "empty" means no non-whitespace content.
    errlog = state.errlogs[-1] if state.errlogs else None
    if buildjob.silent_log(state.logs[-1]) and (errlog is None or buildjob.silent_log(errlog)):
        raise Refused("agent_no_output")
    # Extract the final assistant text from the JSON stdout log.
    raw, extract_reason = _extract_review_text(state.logs[-1], state.entry)
    if extract_reason:
        raise Refused(extract_reason)
    if len(raw) > review.MAX_REVIEW_BYTES:
        raise Refused("oversized")
    if not raw:
        raise Refused("agent_no_output")
    scan = build.scan(raw, [state.token_name], _environb(env))
    if scan.result == build.RESULT_LEAK:
        raise Refused("secret_in_review")
    if scan.result != build.RESULT_OK:
        raise Refused("scan_incomplete")
    parsed = review.parse(raw, state.patch_sha, state.files)
    if parsed.text:  # the cleaned text is what is published: removing format characters may join a hidden secret
        scan = build.scan(parsed.text.encode("utf-8"), [state.token_name], _environb(env))
        if scan.result == build.RESULT_LEAK:
            raise Refused("secret_in_review")
        if scan.result != build.RESULT_OK:
            raise Refused("scan_incomplete")
    return parsed


# --- the artifact -------------------------------------------------------------------------------------------


def make_verdict(state: State, parsed: review.Parsed) -> dict:
    spec = state.entry.spec if state.entry and parsed.verdict != review.MISSING else review.NONE
    data = {
        "verdict": parsed.verdict, "reason": parsed.reason, "slice": state.slice_id, "round": state.round,
        "patch_sha256": state.patch_sha or "0" * 64, "base": state.base, "built_by": state.built_by,
        "reviewed_by": spec, "files": state.files,
        "fell_back": state.fell_back,
    }
    return review.check_verdict(data)


def make_files(state: State, parsed: review.Parsed) -> tuple[dict[str, bytes], dict]:
    """The three files of the artifact, as bytes, and the checked verdict they carry. A verdict the schema refuses
    becomes a missing one (``internal``), never an exception."""
    try:
        verdict = make_verdict(state, parsed)
    except review.ReviewError:
        parsed = review.Parsed(review.MISSING, "internal", "")
        state.files = []
        verdict = make_verdict(state, parsed)
    text = parsed.text if parsed.verdict != review.MISSING else review.MISSING_NOTE.format(reason=parsed.reason)
    files = {
        "verdict": (json.dumps(verdict, indent=2) + "\n").encode(),
        "review.md": text.encode("utf-8"),
        "reviewed_by": (verdict["reviewed_by"] + "\n").encode(),
    }
    return files, verdict


def fail_closed(paths: Paths, reason: str, out_path: str) -> int:
    """Publish nothing: buildjob.fail_closed's rule, with this job's outputs (see there for why the output line is
    enough). `out` is renamed aside and removed, and the engine's last word is published=false."""
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
        buildjob.write_final(out_path, {"published": "false", "review_ok": "false"})
    except BaseException:  # noqa: BLE001
        pass
    print(f"nothing published: {reason}", file=sys.stderr)
    return 1


def _work(env, paths: ReviewPaths, slice_, ref, result, patch, state, engine_dir: Path, out_path: str) -> int:
    """_publish_work, then the fall-through lines whatever its end (buildjob._work: S2)."""
    try:
        return _publish_work(env, paths, slice_, ref, result, patch, state, engine_dir, out_path)
    finally:
        for item in state.fell_back:
            print(route.FellBack.from_dict(item).text)


def _publish_work(env, paths: ReviewPaths, slice_, ref, result, patch, state, engine_dir: Path, out_path: str) -> int:
    buildjob.set_writable(engine_dir, False)  # a hindrance for the agent, not a boundary: it runs as this user
    parsed = review.Parsed(review.MISSING, "internal", "")
    try:
        try:
            parsed = review_slice(env, paths, slice_, ref, result, patch, state)
        except StrayError:
            return fail_closed(paths, "stray_process", out_path)
        except EngineModified:
            parsed = review.Parsed(review.MISSING, "engine_modified", "")
        except KeyboardInterrupt:
            parsed = review.Parsed(review.MISSING, "interrupted", "")
        except BaseException:  # noqa: BLE001 - UnicodeDecodeError, MemoryError and the rest: a missing verdict
            parsed = review.Parsed(review.MISSING, "internal", "")
        try:
            paths.sweep()
        except StrayError:
            return fail_closed(paths, "stray_process", out_path)
    finally:
        buildjob.set_writable(engine_dir, True)
        buildjob.set_writable(paths.repo, True)
    try:
        paths.engine.verify()  # the last look at the engine before it answers: every file
    except EngineModified:
        parsed = review.Parsed(review.MISSING, "engine_modified", "")
    names = [state.token_name] if state.token_name else []
    try:
        files, verdict = make_files(state, parsed)
        buildjob.publish(paths, files)
        if paths.sweep():  # a process was alive while the files were written: it may have written too
            return fail_closed(paths, "stray_process", out_path)
        if why := buildjob.verify_published(paths, files, names, env):
            return fail_closed(paths, why, out_path)
    except StrayError:
        return fail_closed(paths, "stray_process", out_path)
    except BaseException:  # noqa: BLE001 - whatever went wrong, no half-made directory is uploaded
        return fail_closed(paths, "internal", out_path)
    ok = verdict["verdict"] != review.MISSING
    try:
        buildjob.write_final(out_path, {"published": "true", "review_ok": "true" if ok else "false"})
    except JobError as err:  # a symlink, a non-file or a late writer: nothing the runner reads can be trusted
        return fail_closed(paths, str(err), out_path)
    print(f"verdict: {verdict['verdict']}" + (f" ({verdict['reason']})" if verdict["reason"] else ""))
    print(f"reviewed_by: {verdict['reviewed_by']}")
    return 0  # published; the step after the upload fails the job when `review_ok` is not true


def run(env: Mapping[str, str]) -> None:
    """The second step. See the module docstring."""
    slice_ = buildjob.read_slice(env)
    number, ref, _findings = buildjob.read_round(env, slice_.id)
    out_path = job.read_output_path(env)
    result, patch = read_input(env.get("SLICE_DIR", ""), slice_, number)
    repo, base, work = env.get("REPO_DIR", ""), env.get("BASE_DIR", ""), env.get("WORK_DIR", "")
    if not repo or not Path(repo).is_dir() or not base or not Path(base).is_dir() or not work:
        raise JobError("REPO_DIR, BASE_DIR and WORK_DIR are needed")
    scope = env.get("CONTAIN_SCOPE", "descendants")
    if scope not in ("descendants", "user"):
        raise JobError("CONTAIN_SCOPE must be descendants or user")
    if scope == "user":
        buildjob.refuse_shared_host(env)
    if env.get("LINE") == "":  # set but empty: the plan job always names a line, so this is malformed (F5), a job error
        raise JobError("LINE is empty: the plan job always names a line")
    buildjob.check_tools()
    paths = ReviewPaths(Path(repo), Path(work), base=Path(base))
    try:
        engine_dir = Path(env.get("ENGINE_DIR") or ENGINE_DIR_DEFAULT)
        # LINE is the plan job's validated output, as in the build job: the line's own preset is the one that built the slice.
        line = lines.resolve(env.get("LINE") or lines.DEFAULT, env.get("PRESET_JSON", ""), engine_dir)
        paths.engine = ReviewEngine(engine_dir, line)
        paths.pins = workers.load(env.get("PIVOTAL_WORKERS", ""))  # the installed CLIs, before any agent: a bad manifest refuses
    except lines.LineError as err:
        raise JobError(str(err)) from None
    except EngineModified:
        raise JobError("an engine file is missing or is not a regular file") from None
    for directory in (paths.logs, paths.home):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.guard = buildjob.Guard(scope)
    state = State(slice_.id, number, ref, result["built_by"])
    previous = signal.signal(signal.SIGTERM, buildjob._terminated)
    try:
        code = _work(env, paths, slice_, ref, result, patch, state,
                     Path(env.get("ENGINE_DIR") or ENGINE_DIR_DEFAULT), out_path)
    finally:
        signal.signal(signal.SIGTERM, previous)
    if code:
        raise SystemExit(code)


JOBS = {"prepare": buildjob.prepare, "run": run}


def main(argv: list[str], environ) -> int:
    if len(argv) != 2 or argv[1] not in JOBS:
        print(f"usage: reviewjob.py {{{'|'.join(JOBS)}}}", file=sys.stderr)
        return 2
    if argv[1] == "run":
        # Before anything else: this process holds the step's secrets and its descendants are the agent's ancestors.
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
