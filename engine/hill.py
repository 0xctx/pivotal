"""The hill-climb: a request with a score command and a target, from the issue footer ``piv do`` writes.

The footer is the last ``---`` / ``Pivotal request`` block of the issue body, and its lines are ``- score: <command>`` and
``- target: <number>``, together or not at all. ``read_goal`` parses it; a footer with one line without the other, or a bad
command or target, is a HillError. Nothing else here is read from the text.

- The command is one printable line, at most COMMAND_MAX characters. It runs in the build and gate jobs, after the repo's
  check, with the same stripped environment (``run_gate``). It prints ONE number, which the job writes to a file it made
  (``check_with_score``); ``read_score`` takes that file, a regular file of a few bytes, and no other output counts.
- The target is a number above 0 and at most 1, with at most six decimals (TARGET). Green requires ``score >= target`` (``met``).
- The score history is the payload's ``scores``: ``score@head`` entries, one per round already built, separated by ``;``.
  ``encode_history`` and ``decode_history`` are its only codec, and a text that does not decode is refused by the receiver.

This module imports no other engine module, so the plan, build, gate and publish jobs can all import it.
"""

import base64
import binascii
import os
import re
import shlex
import stat
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_ROUNDS = 5  # the hill-climb's bound on fix rounds of a score run (lines/mirror.json uses the same figure); F8's one-round bound is for verdict fixes
FOOTER_TITLE = "Pivotal request"
SCORE_PREFIX = "- score: "
TARGET_PREFIX = "- target: "
COMMAND_MAX = 1000
TARGET = re.compile(r"0?\.[0-9]{1,6}|1(?:\.0{1,6})?")  # the target text: above 0 and at most 1 (checked in read_goal)
SCORE_FILE = re.compile(r"(?:0|1)(?:\.[0-9]{1,6})?|\.[0-9]{1,6}")  # what the score command prints: one number from 0 to 1
SCORE_FILE_MAX = 64  # bytes: a number of at most a few characters, and nothing else a scorer may leave in the file
HISTORY_ENTRY = re.compile(r"(0|1|0\.[0-9]{1,6})@([0-9a-f]{40})")
FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})  # control, format, line and paragraph separators: never in a command
ENCODED = re.compile(r"[A-Za-z0-9+/]{0,5400}={0,2}")  # base64 of a command (up to COMMAND_MAX characters, four bytes each at most): one line


def round_bound(scored: bool, default: int) -> int:
    """The fix-round bound of a run: a score run's own (MAX_ROUNDS), else ``default`` (F8's, from the caller). The one place it is chosen."""
    return MAX_ROUNDS if scored else default


class HillError(ValueError):
    """A footer, command, target, score file or history that the rules refuse. The message names the rule, never a value."""


@dataclass(frozen=True)
class Goal:
    command: str  # the score command, one line
    target: str  # the target text, as the footer wrote it (TARGET)


def check_command(command: str) -> str:
    """The command if it is one printable, non-empty line of at most COMMAND_MAX characters, else HillError."""
    if not command.strip():
        raise HillError("the score command is empty")
    if len(command) > COMMAND_MAX:
        raise HillError(f"the score command is longer than {COMMAND_MAX} characters")
    if any(unicodedata.category(c) in FORBIDDEN_CATEGORIES for c in command):
        raise HillError("the score command has a control, format or line-break character: it must be one plain line")
    return command


def is_target(target: object) -> bool:
    """Whether ``target`` is a score target text: TARGET's form and above 0 (``0.0`` and ``.000000`` are zero). For the readers of a file."""
    return isinstance(target, str) and TARGET.fullmatch(target) is not None and float(target) > 0


def check_target(target: str) -> float:
    """The target as a number: above 0, at most 1, and the footer's text form (TARGET). Else HillError."""
    if TARGET.fullmatch(target) is None:
        raise HillError("the score target must be a number above 0 and at most 1, with at most six decimals")
    value = float(target)
    if not 0 < value <= 1:
        raise HillError("the score target must be a number above 0 and at most 1")
    return value


LOOSE = re.compile(r"\s*(?:[-*+]\s*)?(score|target)\s*[:\uff1a](.*)", re.IGNORECASE)  # a line that reads like a score or target line, however written
BULLETED = re.compile(r"\s*[-*+]\s*(score|target)\s*[:\uff1a](.*)", re.IGNORECASE)  # the same with a bullet: the form a request writes it in
NUMERIC = re.compile(r"\s*[0-9.]+\s*")
KEY = re.compile(r"\s*(?:[-*+]\s*)?([^\s:\uff1a]+)\s*[:\uff1a]")  # the key of a footer line
BREAKS = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")  # every line break check_command and FORBIDDEN_CATEGORIES know


def _shows_score(lines: Sequence[str]) -> bool:
    """Whether lines show a score request in the bulleted form: any bulleted score line, or a bulleted target line that is a bare
    number. A prose ``Score: ...`` line, a fenced ``score: 0.5`` and a ``- target: mobile users`` bullet show none; a bulleted
    ``- score: high priority`` does (refused loudly: fail closed)."""
    found = [BULLETED.match(line) for line in lines]
    keys = {m.group(1).lower() for m in found if m}
    return "score" in keys or any(m and m.group(1).lower() == "target" and NUMERIC.fullmatch(m.group(2)) for m in found)


def _score_lines(lines: Sequence[str]) -> list[str]:
    """The bulleted lines that are score lines or bare-number target lines (what the two line splits must agree on, line for line)."""
    found = [(line, BULLETED.match(line)) for line in lines]
    return [line for line, m in found if m and (m.group(1).lower() == "score" or NUMERIC.fullmatch(m.group(2)))]


def _near_miss(line: str) -> bool:
    """Whether a footer line has a key one edit from ``score`` or ``target`` (a misspelling) that is not the exact key."""
    found = KEY.match(line)
    if not found:
        return False
    key = found.group(1).lower()
    return any(key != word and _within_one(key, word) for word in ("score", "target"))


def _within_one(a: str, b: str) -> bool:
    """Whether a and b differ by at most one insertion, deletion, substitution or adjacent transposition."""
    if abs(len(a) - len(b)) > 1:
        return False
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    if len(a) == len(b) and a[i:i + 2] == b[i:i + 2][::-1] and a[i + 2:] == b[i + 2:]:  # an adjacent transposition
        return True
    return a[i + 1:] == b[i:] or a[i:] == b[i + 1:] or a[i + 1:] == b[i + 1:]


def read_goal(body: str) -> Goal | None:
    """The score goal of an issue body: None when it asks for no score, else the Goal. A footer that names one half of the pair, a
    second line of either, a footer line that reads like a score or target line (any case, bullet or indent, a fullwidth colon, a
    key one edit from ``score`` or ``target``) but is not the exact form, or a bad command or target is a HillError (fail closed:
    no score is not a pass). A footer key further from both words is not seen (a waiver). Outside the footer only the bulleted form
    (``- score: ...``, ``* ...``, ``+ ...``; or a bulleted bare-number target) and the line directly above the footer, however
    written, are refused; a prose ``Score: ...`` line or a fenced ``score: 0.5`` is text. A body whose line breaks are not all LF
    and whose score-line count differs between the two splits (a break such as U+2028 or VT hides or adds one) is refused."""
    lines = body.replace("\r\n", "\n").split("\n")  # a web-UI edit may leave CRLF; a lone CR stays in its line and check_command refuses it
    if _score_lines(BREAKS.split(body)) != _score_lines(lines):
        raise HillError("the request's line breaks are not all LF and hide or add a score line: use LF line breaks")
    marks = [line.rstrip(" \t") for line in lines]  # trailing blanks on the two marker lines are not part of them
    starts = [i for i in range(len(lines) - 1) if marks[i] == "---" and marks[i + 1] == FOOTER_TITLE]
    if not starts:
        if _shows_score(lines):
            raise HillError("a score or target line is in the request but no footer holds it: the footer is the last '---' / 'Pivotal request' block")
        return None
    rest = lines[starts[-1] + 2:]
    commands = [line[len(SCORE_PREFIX):] for line in rest if line.startswith(SCORE_PREFIX)]
    targets = [line[len(TARGET_PREFIX):] for line in rest if line.startswith(TARGET_PREFIX)]
    if sum(1 for line in rest if LOOSE.match(line) or _near_miss(line)) != len(commands) + len(targets):
        raise HillError("a score or target line of the footer is not written exactly as '- score: <command>' / '- target: <number>'")
    before = lines[:starts[-1] + 2]
    earlier = set()  # an earlier footer is read as text: its own lines are not stray lines (the last footer is the one read)
    for n, start in enumerate(starts[:-1]):
        earlier.update(range(start + 2, starts[n + 1]))
    above = starts[-1] > 0 and starts[-1] - 1 not in earlier and LOOSE.match(lines[starts[-1] - 1]) is not None  # the line directly above, however written
    if not commands and not targets:
        if above or _shows_score(before):
            raise HillError("a score or target line is in the request but the last footer holds none: the footer is the last '---' / 'Pivotal request' block")
        return None
    if len(commands) != 1 or len(targets) != 1:
        raise HillError("the request's footer needs one score line and one target line, together")
    if above or _shows_score([line for i, line in enumerate(before) if i not in earlier]):
        raise HillError("a score or target line is outside the footer that holds the score")
    check_target(targets[0])
    return Goal(check_command(commands[0]), targets[0])


def encode_command(command: str) -> str:
    """The job-output form of a command: base64 of its UTF-8, so no character of it reaches GITHUB_OUTPUT as text."""
    return base64.b64encode(check_command(command).encode("utf-8")).decode("ascii")


def decode_command(text: str) -> str:
    """The command of a job output: strict base64 of a checked command. "" is no command (not a goal). Else HillError."""
    if not text:
        raise HillError("no score command")
    if ENCODED.fullmatch(text) is None:
        raise HillError("the score command is not encoded")
    try:
        return check_command(base64.b64decode(text, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HillError("the score command is not encoded") from None


def check_with_score(check: str, command: str, out: str) -> str:
    """The CHECK for run_gate: the repo's check in a subshell (as lines.gate_check leaves it), then the score command in its own
    subshell with its stdout written to ``out``. A blank check leaves the score alone. Both subshells end the script on failure, so
    a failing check or scorer is a red gate; the job reads ``out`` only after a green gate."""
    part = "(\n" + command + "\n) > " + shlex.quote(out)
    if not check.strip():
        return part
    return "(\n" + check + "\n)\n" + part


def read_score(path: Path) -> float | None:
    """The one number a score command printed (its file), or None: a file that is missing, a symlink, not a regular file, longer
    than SCORE_FILE_MAX, not one number from 0 to 1, or not ASCII. Nothing is followed (O_NOFOLLOW)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, SCORE_FILE_MAX + 1)  # a regular file: one read of at most the cap plus one byte
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > SCORE_FILE_MAX:
        return None
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if SCORE_FILE.fullmatch(text) is None:
        return None
    value = float(text)
    return value if 0 <= value <= 1 else None


def met(score: float | None, target: float | str | None) -> bool:
    """Green requires the score: a target with no score is not met (fail closed), and no target is always met."""
    if target is None or target == "":
        return True
    return score is not None and float(score) >= float(target)


def score_text(value: float) -> str:
    """The history's form of a score: six decimals at most, trailing zeros and a bare point dropped (0.4, 1, 0)."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def encode_history(entries: Sequence[tuple[float, str]]) -> str:
    """The payload's ``scores`` text: ``score@head`` for each round built so far, ``;``-separated. "" for none."""
    return ";".join(f"{score_text(score)}@{head}" for score, head in entries)


def decode_history(text: str, limit: int = MAX_ROUNDS + 1) -> list[tuple[float, str]]:
    """The entries of a ``scores`` text, in order. "" is none. Anything that is not exactly the form, or more than ``limit``
    entries, is a HillError."""
    if not isinstance(text, str):
        raise HillError("scores must be text")
    if text == "":
        return []
    entries = []
    for part in text.split(";"):
        found = HISTORY_ENTRY.fullmatch(part)
        if found is None:
            raise HillError("scores is not a score history")
        entries.append((float(found.group(1)), found.group(2)))
    if len(entries) > limit:
        raise HillError("scores has more entries than the rounds allow")
    return entries


def best(entries: Sequence[tuple[float, str]]) -> tuple[float, str] | None:
    """The best round of a history: the highest score, the earliest on a tie (a later round must beat it to replace it)."""
    chosen = None
    for score, head in entries:
        if chosen is None or score > chosen[0]:
            chosen = (score, head)
    return chosen
