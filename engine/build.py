"""The build job's secret scan (F3b) and the schema of its result.json.

The job itself is engine/buildjob.py. ``check_result`` is the one validator of result.json: buildjob
runs it before it writes the file, and the assemble job imports it to read a slice's artifact (a
result that fails it is a failed slice, F5).

After agent_run has killed the agent's process group, the job runs `python3 -m engine.build scan`
on the patch. The scan looks for every value the agent may have held: the names the job passes
(each must be set), and every job secret that is set in the environment. A value is looked
for raw, in hex (both cases), and in base64 and URL-safe base64 at each of the three byte
alignments. That catches a value written into a file, wrapped across lines, or inside a binary
file, where git writes base85 of zlib data (binary sections are decoded first).

The result is `ok`, `failed: secret_in_patch` or `failed: scan_incomplete`. Anything but `ok` is not
landable (F5), and the patch file is deleted. The CLI prints a secret's name and form, never its
value.

Recorded limits, outside what the scan promises: a value is not found in encodings other than the
ones above (`0x`, `\\x` or `%xx` escapes, base32, base85, reversed, lower-cased or concatenated
strings). A value under 12 bytes is never protected; it is only reported as skipped, by name.
`--secret` and `--token-env` are not linked, so the job must pass the same name to both. A process
that calls setsid itself survives the kill of the agent's process group.
"""

import argparse
import base64
import os
import re
import sys
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from engine import hill, plan, route

# A shorter value would match ordinary text, so it is named as skipped instead of scanned.
MIN_SECRET_BYTES = 12
# The most a binary section may inflate to. A larger one is not scanned, so it is incomplete.
MAX_BLOB_BYTES = 64 * 1024 * 1024

RESULT_OK = "ok"
RESULT_LEAK = "failed: secret_in_patch"
RESULT_INCOMPLETE = "failed: scan_incomplete"

# The secrets a job holds: every door secret, the GitHub tokens and the runner's Actions tokens.
JOB_SECRETS = (
    *(name for names in route.DOOR_SECRETS.values() for name in names),
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
)

# A base64 group holds 3 bytes. A value that starts `align` bytes into a group shares its first
# characters with the bytes before it, so they are dropped. The last, partial group shares its
# characters with the bytes after the value, so it is dropped too.
_B64_SKIP = (0, 2, 3)

_BLOB_HEADER = b"GIT binary patch"
_BLOB_SECTION = re.compile(rb"(?:literal|delta) \d+")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Header lines that carry a file name, and git's quoting of a name with unusual characters.
_NAME_LINES = (b"diff --git ", b"--- ", b"+++ ", b"rename ", b"copy ")
_QUOTED = re.compile(rb'"(?:[^"\\]|\\.)*"')
_ESCAPES = {b"t": b"\t", b"n": b"\n", b"a": b"\a", b"b": b"\b", b"f": b"\f", b"r": b"\r",
            b"v": b"\v"}
# A name worth showing in a message; anything else could be a value passed by mistake.
_SHOWABLE_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,47}")


RESULT_KEYS = frozenset(
    {"status", "reason", "agent", "gate", "built_by", "fell_back", "healed", "slice", "round", "workflow_change", "score", "target"}
)
# A build is `ok` or `failed`, never `empty`: a failure, an empty patch included, always has a reason.
STATUSES = ("ok", "failed")
# not_run: the agent never produced a commit, so the gate never ran.
GATES = ("green", "red", "none", "not_run")
LANDABLE_GATES = ("green", "none")
FAIL_REASONS = re.compile(
    r"timeout|error:[0-9]{1,3}|no_route|no_secret|secret_in_patch|scan_incomplete|gate_red|empty_patch|agent_no_output"
    r"|bad_history|patch_too_large|unsafe_attributes|bad_spec|interrupted|stray_process|engine_modified|internal"
)
_LABEL = re.compile(r"[ -~]{0,200}")
_AGENT = re.compile(r"[a-z0-9_-]{1,32}|")
MAX_FELL_BACK = 32


class ResultError(ValueError):
    """A result.json breaks the schema. The message names the rule and never repeats a value."""


def check_result(data: object) -> dict:
    """The result.json of a slice, if it follows the schema; else ResultError.

    ``ok`` needs a named builder, a gate of green or none and no reason. ``failed`` needs a reason from
    FAIL_REASONS. Unknown or missing keys are rejected, so a result cannot carry anything else along.
    """
    if not isinstance(data, dict):
        raise ResultError("result must be an object")
    if set(data) != RESULT_KEYS:
        raise ResultError("result keys are not exactly the schema's")
    for key in ("status", "reason", "agent", "gate", "built_by", "slice"):
        if not isinstance(data[key], str) or _LABEL.fullmatch(data[key]) is None:
            raise ResultError(f"{key} must be short printable text")
    if _AGENT.fullmatch(data["agent"]) is None:
        raise ResultError("agent must be a worker name or empty")
    if data["built_by"] and not route.valid_spec(data["built_by"]):
        raise ResultError("built_by must be a route entry spec or empty")
    if not isinstance(data["workflow_change"], bool):
        raise ResultError("workflow_change must be true or false")
    if data["status"] not in STATUSES:
        raise ResultError(f"status must be one of {', '.join(STATUSES)}")
    if data["gate"] not in GATES:
        raise ResultError(f"gate must be one of {', '.join(GATES)}")
    if plan.ID_PATTERN.fullmatch(data["slice"]) is None or data["slice"] in plan.RESERVED_IDS:
        raise ResultError("slice is not a slice id")
    try:
        route.check_fell_back(data["fell_back"], MAX_FELL_BACK)
    except ValueError as err:
        raise ResultError(str(err)) from None
    if not isinstance(data["healed"], bool):
        raise ResultError("healed must be true or false")
    rnd = data["round"]
    if not isinstance(rnd, int) or isinstance(rnd, bool) or not 0 <= rnd <= 9999:
        raise ResultError("round must be a small non-negative integer")
    # The hill-climb (engine/hill.py): the target ("" for none) and the score the score command printed (null when none was read).
    # A score is only ever with a target, and an ok result with a target has its score: a green gate that did not read one is not green.
    target, score = data["target"], data["score"]
    if not isinstance(target, str) or (target and not hill.is_target(target)):
        raise ResultError("target must be a score target or empty")
    if score is not None and (not _is_number(score) or not 0 <= score <= 1):
        raise ResultError("score must be null or a number from 0 to 1")
    if score is not None and not target:
        raise ResultError("a score needs a target")
    if data["status"] == "ok" and target and score is None:
        raise ResultError("an ok result with a target has its score")
    if data["status"] == "ok":
        if data["reason"] or not data["agent"] or not data["built_by"] or data["gate"] not in LANDABLE_GATES:
            raise ResultError("an ok result has a builder, a landable gate and no reason")
    elif FAIL_REASONS.fullmatch(data["reason"]) is None:
        raise ResultError("a failed result needs a reason from the list")
    return data


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass
class Held:
    """The values to check, the names skipped for being short, and why the values are unreadable."""

    values: dict[str, bytes] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    problem: str = ""


@dataclass
class Scan:
    result: str
    leaks: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    problem: str = ""


def env_name(text: str) -> str:
    """argparse type: an environment variable name."""
    if not _ENV_NAME.fullmatch(text):
        raise argparse.ArgumentTypeError("not an environment variable name")
    return text


def forms(value: bytes) -> dict[bytes, str]:
    """Each byte string a value can take in a file, mapped to its form's name."""
    found: dict[bytes, str] = {}

    def add(needle: bytes, kind: str) -> None:
        if needle:
            found.setdefault(needle, kind)

    # The value as given, and without the whitespace around it (a pasted token may end in a newline).
    variants = [value]
    stripped = value.strip()
    if stripped != value and len(stripped) >= MIN_SECRET_BYTES:
        variants.append(stripped)
    for variant in variants:
        add(variant, "raw")
        add(variant.hex().encode(), "hex")
        add(variant.hex().upper().encode(), "hex-upper")
        for kind, encode in (
            ("base64", base64.b64encode),
            ("base64-urlsafe", base64.urlsafe_b64encode),
        ):
            for align in range(3):
                encoded = encode(b"\0" * align + variant)
                add(encoded[_B64_SKIP[align] : 4 * ((align + len(variant)) // 3)], kind)
    # A value of several lines is one run in a squashed view, where the whitespace is gone.
    joined = b"".join(value.split())
    if len(joined) >= MIN_SECRET_BYTES:
        add(joined, "raw")
    return found


def squash(data: bytes, *, diff: bool = False) -> bytes:
    """The text with whitespace removed. For a patch, the one-character marker at the start of each
    line goes too, so a value wrapped across lines is one run again."""
    lines = data.split(b"\n")
    if diff:
        lines = [line[1:] if line[:1] in (b"+", b"-", b" ") else line for line in lines]
    return b"".join(b"".join(line.split()) for line in lines)


def _unbase85(line: bytes) -> bytes:
    """One data line of a git binary patch: a length character, then base85 of the bytes."""
    head = line[:1]
    if not head.isalpha():
        raise ValueError("bad length character")
    length = head[0] - ord("A") + 1 if head.isupper() else head[0] - ord("a") + 27
    data = base64.b85decode(line[1:])
    if not 0 < length <= len(data):
        raise ValueError("bad length")
    return data[:length]


def _inflate(data: bytes) -> bytes:
    decoder = zlib.decompressobj()
    out = decoder.decompress(data, MAX_BLOB_BYTES + 1)
    if len(out) > MAX_BLOB_BYTES:
        raise ValueError("section too large to scan")
    if not decoder.eof:
        raise ValueError("section truncated")
    return out


def binary_sections(patch: bytes) -> list[bytes]:
    """The inflated content of every git binary patch section: the new file for `literal`, and the
    delta's instructions, with the bytes it inserts, for `delta`. Raises ValueError (or zlib.error)
    when a section cannot be decoded."""
    lines = patch.split(b"\n")
    sections: list[bytes] = []
    i = 0
    while i < len(lines):
        if lines[i] != _BLOB_HEADER:
            i += 1
            continue
        i += 1
        found = 0
        while i < len(lines) and _BLOB_SECTION.fullmatch(lines[i]):
            i += 1
            chunks = []
            while i < len(lines) and lines[i]:
                chunks.append(_unbase85(lines[i]))
                i += 1
            i += 1  # the blank line that ends the section
            sections.append(_inflate(b"".join(chunks)))
            found += 1
        if not found:
            raise ValueError("no section after the binary patch header")
    return sections


def _unquote_path(match: re.Match[bytes]) -> bytes:
    """A git C-quoted path (`"caf\\303\\251"`) as the bytes it names."""
    out = bytearray()
    body = match.group(0)[1:-1]
    i = 0
    while i < len(body):
        char = body[i : i + 1]
        if char != b"\\":
            out += char
            i += 1
            continue
        octal = re.match(rb"[0-7]{1,3}", body[i + 1 :])
        if octal:
            out.append(int(octal.group(), 8) & 0xFF)
            i += 1 + len(octal.group())
        else:
            escaped = body[i + 1 : i + 2]
            out += _ESCAPES.get(escaped, escaped)
            i += 2
    return bytes(out)


def file_names(patch: bytes) -> list[bytes]:
    """The file names in the patch's header lines, with git's quoting undone, and again with the
    "/" and whitespace removed, so a value split across directories is one run."""
    names = []
    for line in patch.split(b"\n"):
        if line.startswith(_NAME_LINES):
            names.append(_QUOTED.sub(_unquote_path, line))
    return [b"\n".join(names), b"".join(b"".join(n.split()).replace(b"/", b"") for n in names)]


def _views(patch: bytes) -> list[bytes]:
    """Every byte string the scan searches: the patch as text, each file name, and each binary
    section's content. The patch is squashed twice: with the diff markers removed, to join a value
    wrapped over added lines, and with them kept, which is what a commit message needs."""
    views = [patch, squash(patch, diff=True), squash(patch), *file_names(patch)]
    for blob in binary_sections(patch):
        views += [blob, squash(blob)]
    return views


def leaks_in(views: Iterable[bytes], values: Mapping[str, bytes]) -> list[tuple[str, str]]:
    """The (NAME, form) pairs whose form occurs in one of the views, sorted."""
    needles = [
        (name, needle, kind)
        for name, value in values.items()
        for needle, kind in forms(value).items()
    ]
    found: set[tuple[str, str]] = set()
    for view in views:
        for name, needle, kind in needles:
            if (name, kind) not in found and needle in view:
                found.add((name, kind))
    return sorted(found)


def redact(data: bytes, values: Mapping[str, bytes]) -> bytes:
    """``data`` with every form of every held value taken out (the forms of forms(), longest first). For text that is
    shown in a reason, where a leak is removed rather than refused: the same needles leaks_in searches for."""
    needles = sorted({needle for value in values.values() for needle in forms(value)}, key=len, reverse=True)
    for needle in needles:
        data = data.replace(needle, b"")
    return data


def held_values(named: Sequence[str], environ: Mapping[bytes, bytes]) -> Held:
    """The values to check. A named secret must be set; a job secret is checked when it is set."""
    held = Held()
    for name in dict.fromkeys([*named, *JOB_SECRETS]):
        value = environ.get(name.encode(), b"")
        if not value:
            if name in named:
                shown = name if _SHOWABLE_NAME.fullmatch(name) else "a named secret"
                held.problem = f"{shown} is not set, so the patch cannot be checked against it"
                return held
            continue
        if len(value) < MIN_SECRET_BYTES:
            held.skipped.append(name)
        else:
            held.values[name] = value
    return held


def scan(patch: bytes, named: Sequence[str], environ: Mapping[bytes, bytes]) -> Scan:
    """Checks the patch for the values of the named secrets and of the job's own secrets."""
    held = held_values(named, environ)
    if held.problem:
        return Scan(RESULT_INCOMPLETE, skipped=held.skipped, problem=held.problem)
    try:
        views = _views(patch)
    except (ValueError, zlib.error):
        return Scan(
            RESULT_INCOMPLETE,
            skipped=held.skipped,
            problem="a git binary patch section cannot be decoded and scanned",
        )
    leaks = leaks_in(views, held.values)
    if leaks:
        return Scan(RESULT_LEAK, leaks=leaks, skipped=held.skipped)
    return Scan(RESULT_OK, skipped=held.skipped)


def scan_file(path: Path, named: Sequence[str], environ: Mapping[bytes, bytes]) -> Scan:
    """Scans a patch file. Anything but ok deletes the file, so it cannot be shipped."""
    result = scan(path.read_bytes(), named, environ)
    if result.result != RESULT_OK:
        path.unlink(missing_ok=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m engine.build")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("scan", help="check a patch for the job's secret values")
    check.add_argument("--patch", required=True, type=Path, help="the patch file to check")
    check.add_argument(
        "--secret", required=True, action="append", type=env_name, metavar="NAME",
        help="a secret the agent held, by environment name; repeat for more",
    )
    args = parser.parse_args(argv)

    try:
        result = scan_file(args.patch, args.secret, os.environb)
    except OSError:
        result = Scan(RESULT_INCOMPLETE, problem="the patch cannot be read")
        args.patch.unlink(missing_ok=True)
    print(result.result)
    for name, form in result.leaks:
        print(f"leak: {name} {form}")
    for name in result.skipped:
        print(f"skipped: {name} (shorter than {MIN_SECRET_BYTES} bytes, not scanned)")
    if result.problem:
        print(f"problem: {result.problem}")
    return 0 if result.result == RESULT_OK else 1


if __name__ == "__main__":
    sys.exit(main())
