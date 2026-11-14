"""The review verdict: what the review job publishes and what assemble reads back.

This module holds no job logic and runs no repo code. It is the schema validator both sides import: the review job
(engine/reviewjob.py) to turn a reviewer's raw output into a verdict, and the later `assemble` ticket to read the
artifact `review-<id>` again. The artifact is untrusted input for the next job (F5), so a reader calls ``load`` and
then ``accept``; anything off is ``missing``, and a missing verdict makes the slice a draft.

The artifact `review-<id>` holds exactly three regular files:

``verdict``      one JSON object (``check_verdict``): the verdict (``READY``, ``NEEDS FIXES`` or ``missing``), the
                 reason when it is missing, the slice and round, ``patch_sha256`` (the sha256 of the slice's
                 ``slice.patch``: the SHA the verdict names), ``base`` (the commit the patch applies to), the
                 builder and the reviewer, the changed files and the ``fell_back`` items (``{from, to, reason}``, route.FellBack).
``review.md``    the reviewer's text as the engine accepted it (control, format and ANSI characters removed). It ends
                 with the verdict line. For a missing verdict it is one fixed sentence and never the reviewer's text.
``reviewed_by``  the reviewer's route spec on one line, or ``none``.

What the reviewer must write (the brief, engine/prompts/review.md): one line ``Reviewed: <patch_sha256>``, one line
``Covered: <path>`` for every changed file, in the escaped form of ``quote_path``, and the exact last line
``Verdict: READY`` or ``Verdict: NEEDS FIXES``. ``parse`` checks all of it in code. The verdict is missing when
the output is empty, over ``MAX_REVIEW_BYTES`` (a longer one is not read, never cut), not text, has no verdict line
or more than one or one that is not last, names another SHA or none, or omits or invents a changed file. A
verdict is never taken from a hostile patch: only the reviewer's own last line counts, and a patch that makes the
reviewer quote a ``Verdict:`` line makes the output ambiguous, which is missing.

Notes for the reader (the assemble ticket). ``accept`` does not bind the base commit or the round: assemble must
check that the verdict's ``base`` and ``round`` are the run's own before it calls ``accept``. The ``files`` set is
what the apply in the review job made (the names git reads from the patch plus every entry the apply made or removed,
so a rename counts by both names): it cannot be read off the patch text alone, so assemble reproduces it from the
same apply on the same base and puts the names through ``files_of``, the one function that turns them into the
``files`` form. The SHA is the sha256 of ``slice.patch``, not a commit id.

``accept`` is the check for landing: READY, the SHA of the patch that is about to land, the files that patch changes,
and a reviewer whose vendor is not the builder's (the route spec decides the vendor, as in route.py).
"""

import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from engine import plan, route

VERDICTS = ("READY", "NEEDS FIXES")
MISSING = "missing"
VERDICT_KEYS = frozenset(
    {"verdict", "reason", "slice", "round", "patch_sha256", "base", "built_by", "reviewed_by", "files", "fell_back"}
)
FILES = ("verdict", "review.md", "reviewed_by")
NONE = "none"  # reviewed_by when nobody reviewed

MAX_REVIEW_BYTES = 64 * 1024  # a longer review is oversized, not cut
MAX_RAW_LOG_BYTES = 4 * 1024 * 1024  # the raw JSONL log before text extraction; a runaway stream is refused, not read whole
MAX_FILES = 300  # changed files a review can cover
MAX_PATH_CHARS = 1024  # a quoted path
MAX_VERDICT_BYTES = 512 * 1024  # the verdict file: MAX_FILES quoted paths fit well below this
MAX_FELL_BACK = 32
MISSING_NOTE = "No valid verdict: {reason}. The reviewer's output was not accepted.\n"

# The reasons a verdict is missing. Every one is a fixed word the engine chose; none carries reviewer text.
MISSING_REASONS = re.compile(
    r"no_route|timeout|error:[0-9]{1,3}|empty_review|agent_no_output|oversized|not_text|no_verdict|ambiguous_verdict|verdict_not_last"
    r"|no_sha|wrong_sha|coverage_incomplete|coverage_unknown|reviewer_wrote|secret_in_patch|secret_in_review"
    r"|scan_incomplete|patch_does_not_apply|unsafe_patch|unsafe_attributes|patch_too_large|too_many_files|bad_history"
    r"|bad_spec|interrupted|engine_modified|internal"
    # agent_error[:CODE][: TEXT]: a provider error, its optional 3-digit status, and an optional ": " and at most 160
    # characters of its redacted errorMessage, from the set reviewjob.agent_error_reason keeps.
    r"|agent_error(?::[0-9]{1,3})?(?:: [A-Za-z0-9 .,:;_/=()-]{1,160})?"
    # agent_exit_N (N from 1 to 255): the reviewer's word was error:N; an optional ": " and at most 160 characters of
    # its redacted errlog, from the set reviewjob.exit_reason keeps (nothing else is ever a reason's text).
    r"|agent_exit_(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9][0-9]|[1-9])(?:: [A-Za-z0-9 .,:;_/=()-]{1,160})?"
)

SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"(?!0{40})[0-9a-f]{40}")
_VERDICT_LINE = re.compile(r"Verdict: (READY|NEEDS FIXES)")
_VERDICT_PREFIX = re.compile(r"\s*Verdict\s*:", re.IGNORECASE)
_REVIEWED = re.compile(r"Reviewed: ([^\n]*)")
_COVERED = re.compile(r"Covered: ([^\n]*)")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_FORBIDDEN = frozenset({"Cf", "Zl", "Zp", "Cs", "Co", "Cn"})  # format, separators, surrogate, private use, unassigned
_PRINTABLE_ASCII = re.compile(r"[ -~]*")
_LABEL = re.compile(r"[ -~]{0,200}")


class ReviewError(ValueError):
    """An artifact or a verdict breaks the schema. The message names the rule and never repeats a value."""


def quote_path(raw: bytes) -> str:
    """A changed file's name as the reviewer is shown it and must write it back: printable ASCII, a backslash as
    ``\\\\`` and every other byte as ``\\xNN``. Deterministic and unambiguous, and a name cannot forge a line,
    markdown or a control sequence."""
    out = []
    for byte in raw:
        if byte == 0x5C:
            out.append("\\\\")
        elif 0x20 <= byte <= 0x7E:
            out.append(chr(byte))
        else:
            out.append(f"\\x{byte:02x}")
    return "".join(out)


def clean_text(text: str) -> str:
    """The reviewer's text with ANSI sequences, control characters (but not newline or tab) and format, separator
    and unassigned characters removed, and line ends made ``\\n``. What is written to review.md."""
    text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        c for c in text if c in "\n\t" or (unicodedata.category(c) not in _FORBIDDEN and unicodedata.category(c)[0] != "C")
    )


@dataclass(frozen=True)
class Parsed:
    verdict: str  # READY, NEEDS FIXES or MISSING
    reason: str  # "" unless missing
    text: str  # the cleaned text, for review.md; "" when missing


def _missing(reason: str) -> Parsed:
    return Parsed(MISSING, reason, "")


def parse(raw: bytes, patch_sha256: str, files: Sequence[str]) -> Parsed:
    """The reviewer's raw output to a verdict, or ``missing`` with the reason. ``files`` are the changed files in
    ``quote_path`` form. See the module docstring for every rule."""
    if len(raw) > MAX_REVIEW_BYTES:
        return _missing("oversized")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _missing("not_text")
    text = clean_text(text)
    text = text.rstrip("\n")
    if not text.strip():
        return _missing("empty_review")
    lines = text.split("\n")
    # Any line that looks like a verdict line counts: `Verdict: READY.` or ` verdict: ready` is not the exact line.
    marked = [i for i, line in enumerate(lines) if _VERDICT_PREFIX.match(line)]
    exact = [i for i in marked if _VERDICT_LINE.fullmatch(lines[i])]
    if not exact:
        return _missing("no_verdict")
    if len(marked) != 1:
        return _missing("ambiguous_verdict")
    if marked[0] != len(lines) - 1:
        return _missing("verdict_not_last")
    reviewed = [m[1] for line in lines if (m := _REVIEWED.fullmatch(line))]
    if not reviewed:
        return _missing("no_sha")
    if reviewed != [patch_sha256]:
        return _missing("wrong_sha")
    covered = [m[1] for line in lines if (m := _COVERED.fullmatch(line))]
    if set(covered) - set(files):
        return _missing("coverage_unknown")
    if set(files) - set(covered):
        return _missing("coverage_incomplete")
    return Parsed(lines[-1].split(": ", 1)[1], "", text + "\n")


def _printable(data: Mapping, key: str, limit: int = 200) -> str:
    value = data[key]
    if not isinstance(value, str) or len(value) > limit or _PRINTABLE_ASCII.fullmatch(value) is None:
        raise ReviewError(f"{key} must be short printable text")
    return value


def vendor_of(spec: str) -> str | None:
    return route.parse_entry(spec).vendor


def check_verdict(data: object) -> dict:
    """The `verdict` file's object, if it follows the schema; else ReviewError. A valid verdict also has a
    reviewer from another vendor than the builder, so the cross-vendor rule is part of the schema."""
    if not isinstance(data, dict):
        raise ReviewError("verdict must be an object")
    if set(data) != VERDICT_KEYS:
        raise ReviewError("verdict keys are not exactly the schema's")
    if data["verdict"] not in (*VERDICTS, MISSING):
        raise ReviewError("verdict must be READY, NEEDS FIXES or missing")
    for key in ("reason", "slice"):
        _printable(data, key)
    if plan.ID_PATTERN.fullmatch(data["slice"]) is None or data["slice"] in plan.RESERVED_IDS:
        raise ReviewError("slice is not a slice id")
    rnd = data["round"]
    if not isinstance(rnd, int) or isinstance(rnd, bool) or not 0 <= rnd <= 9999:
        raise ReviewError("round must be a small non-negative integer")
    if not isinstance(data["patch_sha256"], str) or SHA256.fullmatch(data["patch_sha256"]) is None:
        raise ReviewError("patch_sha256 must be a sha256")
    if not isinstance(data["base"], str) or COMMIT.fullmatch(data["base"]) is None:
        raise ReviewError("base must be a commit SHA")
    for key in ("built_by", "reviewed_by"):
        value = data[key]
        if not isinstance(value, str) or (value != NONE or key == "built_by") and not route.valid_spec(value):
            raise ReviewError(f"{key} must be a route entry spec" + (" or none" if key == "reviewed_by" else ""))
    files = data["files"]
    if (
        not isinstance(files, list)
        or len(files) > MAX_FILES
        or not all(isinstance(f, str) and 0 < len(f) <= MAX_PATH_CHARS and _PRINTABLE_ASCII.fullmatch(f) for f in files)
        or len(set(files)) != len(files)
    ):
        raise ReviewError("files must be a short list of distinct quoted paths")
    try:
        route.check_fell_back(data["fell_back"], MAX_FELL_BACK)
    except ValueError as err:
        raise ReviewError(str(err)) from None
    if data["verdict"] == MISSING:
        if MISSING_REASONS.fullmatch(data["reason"]) is None:
            raise ReviewError("a missing verdict needs a reason from the list")
    else:
        if data["reason"]:
            raise ReviewError("a verdict has no reason")
        if not files or data["reviewed_by"] == NONE:
            raise ReviewError("a verdict covers files and names its reviewer")
        reviewer, builder = vendor_of(data["reviewed_by"]), vendor_of(data["built_by"])
        if reviewer is None or builder is None or reviewer == builder:
            raise ReviewError("the reviewer must be from a different vendor than the builder")
    return data


def read_regular(directory: Path, name: str, limit: int) -> bytes:
    path = directory / name
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise ReviewError(f"{name} is not a regular file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError:
        raise ReviewError(f"{name} cannot be read") from None
    if len(data) > limit:
        raise ReviewError(f"{name} is over {limit} bytes")
    return data


def load(directory: str | Path) -> dict:
    """Read an artifact `review-<id>` that was downloaded to ``directory``: exactly the three files, each a small
    regular file, the verdict valid, ``reviewed_by`` equal to the verdict's, and ``review.md`` parsing again, by the
    rules that accepted it, to the same verdict, SHA and coverage (or being the fixed note of a missing one). Raises ReviewError otherwise: the caller counts the
    slice's verdict as missing."""
    directory = Path(directory)
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise ReviewError("the review artifact is not a directory")
        names = sorted(os.listdir(directory))
    except OSError:
        raise ReviewError("the review artifact cannot be read") from None
    if names != sorted(FILES):
        raise ReviewError("the review artifact is not exactly verdict, review.md and reviewed_by")
    try:
        data = json.loads(read_regular(directory, "verdict", MAX_VERDICT_BYTES).decode("utf-8"),
                          object_pairs_hook=plan._no_duplicate_keys)
    except (ValueError, RecursionError, plan.SliceError):  # a deep `[[[...` is RecursionError, not ValueError
        raise ReviewError("verdict is not JSON") from None
    verdict = check_verdict(data)
    try:
        who = read_regular(directory, "reviewed_by", 400).decode("utf-8")
        text = read_regular(directory, "review.md", MAX_REVIEW_BYTES).decode("utf-8")
    except UnicodeDecodeError:
        raise ReviewError("a review file is not text") from None
    if who != verdict["reviewed_by"] + "\n":
        raise ReviewError("reviewed_by differs from the verdict's")
    if verdict["verdict"] == MISSING:
        if text != MISSING_NOTE.format(reason=verdict["reason"]):
            raise ReviewError("review.md is not the note of a missing verdict")
    else:
        # The text is read again by the rules that accepted it: same verdict, same SHA, same coverage.
        again = parse(text.encode("utf-8"), verdict["patch_sha256"], verdict["files"])
        if again.verdict != verdict["verdict"] or again.text != text:
            raise ReviewError("review.md does not carry the verdict, the SHA and the coverage")
    return verdict


def files_of(names: Iterable[bytes]) -> list[str]:
    """The ``files`` of a verdict from the raw names of the changed files: quoted, sorted, without repeats."""
    return sorted({quote_path(name) for name in names})


def accept(verdict: Mapping, *, patch_sha256: str, files: Sequence[str], built_by: str) -> str:
    """"" when this checked verdict lets the slice land, else the reason it does not. ``patch_sha256`` is the
    digest of the slice's patch as assemble holds it, ``files`` the files that patch changes in ``quote_path`` form
    and ``built_by`` the slice's result.json. Fail closed: anything but READY on exactly these is a reason."""
    try:
        check_verdict(dict(verdict))
    except (ReviewError, ValueError):
        return "invalid"
    if verdict["verdict"] != "READY":
        return "missing" if verdict["verdict"] == MISSING else "needs_fixes"
    if verdict["patch_sha256"] != patch_sha256:
        return "wrong_sha"
    if sorted(verdict["files"]) != sorted(files):
        return "files"
    if verdict["built_by"] != built_by:
        return "wrong_builder"
    return ""
