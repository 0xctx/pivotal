"""The fix-round dispatch (the publish job's send, F8): what publish sends and what the next run reads.

A draft slice that stopped on a NEEDS FIXES verdict (`fix`) or a patch that did not apply on the stack
(`conflict`) gets ONE ``repository_dispatch`` of type ``pivotal-fix`` while ``round < MAX_ROUNDS`` (a request with a score: a round per dispatch, to ``hill.MAX_ROUNDS``). The payload is
``{issue, slice, round: round+1, run_id, branch, head_sha, issue_pin, findings}``; ``issue_pin`` is round 0's pin over the issue's
title and body (``trust.issue_pin``), forwarded unchanged each round: plan refuses a fix round when the live issue no longer has it. The receiving side is ``trust.py`` (who may start
it, and the pin over the fields), ``plan.py`` (``fix_round``: ``round > MAX_ROUNDS`` is refused, the branch must be the
slice's draft branch and still at ``head_sha``) and ``buildjob.py`` (the findings reach the brief as a fenced data block).

``findings`` is UNTRUSTED TEXT: the reviewer's text, the last lines of the build's redacted summary (``summary.md``: the build log's
redaction; the gate's own log is NOT in it, F9) and the score history. It is cleaned (no control, bidi or format character), every line of it is quoted with a bar so
no line can start a section of ours, each part is capped after the quoting, and the whole is at most ``MAX_FINDINGS_BYTES`` (20 KB), measured as the ASCII-escaped
JSON string the request carries (``size``), which is never below its UTF-8 size. A part that cannot be read is ``(not readable)``.
The payload carries no secret: it is scanned for the token before it is sent (``leaks``), and nothing from the environment is
put in it. The factory acts on ``findings`` only as data in a fenced block; it is not part of the pin.

A ``/pivotal fix`` comment is NOT a fix round: it is a round-0 run from the issue's own text on the default branch (round 0,
no draft head, no findings). Only this dispatch carries a round.

A request with a score (engine/hill.py) has its own bound, ``hill.MAX_ROUNDS``, which the callers pass to ``may_send``. The ``score.max_rounds``
of a line (lines/*.json, engine/lines.py) is validated and not read: no scorer line is wired.

This module only builds and checks. Sending is ``publish.send_fix_rounds``.
"""

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence

from engine import build, hill, plan, trust

EVENT_TYPE = "pivotal-fix"
MAX_ROUNDS = plan.DEFAULT_MAX_ROUNDS  # F8: the one bound, shared by publish (send) and plan (refuse)
MAX_FINDINGS_BYTES = 20 * 1024  # the one cap of the send and the receive
PAYLOAD_KEYS = trust.PAYLOAD_KEYS  # the receiver's contract: trust pins all but findings (R24)
ISSUE_PIN = re.compile(r"[0-9a-f]{64}")
# The reasons in land.json of a draft that is worth one more try, and what each is called in the log.
# A score below the target is a fix reason too (engine/hill.py): the round is sent while the score run has rounds left.
FIX_REASONS = {"verdict_needs_fixes": "fix", "conflict": "conflict", "score_low": "score"}  # no gate_red: a red build is `failed`, with no branch
# The part of the findings each source may take, in bytes. They add up to well below MAX_FINDINGS_BYTES.
VERDICT_BYTES, TAIL_BYTES, SCORE_BYTES = 9 * 1024, 8 * 1024, 512
TRUNCATED = "[cut]"
FORBIDDEN = plan.FORBIDDEN_CATEGORIES  # one list of the characters no text of ours may carry


def kind_of(entry: Mapping) -> str:
    """``fix`` or ``conflict`` for a draft slice that deserves a fix round, else ""."""
    if entry.get("outcome") != "draft" or not entry.get("branch") or not entry.get("head"):
        return ""
    return FIX_REASONS.get(entry.get("reason"), "")


def may_send(round_number: int, max_rounds: int = MAX_ROUNDS) -> bool:
    """Whether a run at ``round_number`` may start the next one (F8). The next round is ``round_number + 1``. A score run's bound is
    its own (``hill.MAX_ROUNDS``), passed in by the caller that knows the run is one."""
    return 0 <= round_number < max_rounds


def clean(text: object) -> str:
    """Untrusted text as printable lines: CRLF and CR are LF, tab is a space, and control, bidi, format, line and paragraph
    separator characters are dropped."""
    if not isinstance(text, str):
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return "".join(c for c in text if c == "\n" or unicodedata.category(c) not in FORBIDDEN)


def size(text: str) -> int:
    """The bytes ``text`` takes inside the request: its ASCII-escaped JSON string, without the quotes. It is never below the
    UTF-8 size, so a text within ``MAX_FINDINGS_BYTES`` here is within it in UTF-8 too (the receiver's check, buildjob)."""
    return len(json.dumps(text, ensure_ascii=True)) - 2


def cut(text: str, limit: int, *, keep_end: bool = False) -> str:
    """At most ``limit`` escaped bytes (``size``) of ``text`` (the start, or the end with ``keep_end``), on a character boundary.
    A cut text ends (or starts) with the visible mark ``[cut]``; a text that fits is returned as it is."""
    if size(text) <= limit:
        return text
    room = max(0, limit - size(TRUNCATED) - size("\n"))
    chars = reversed(text) if keep_end else text
    taken, used = [], 0
    for c in chars:
        used += size(c)
        if used > room:
            break
        taken.append(c)
    piece = "".join(reversed(taken) if keep_end else taken)
    return f"{TRUNCATED}\n{piece}" if keep_end else f"{piece}\n{TRUNCATED}"


def quoted(text: object, limit: int, *, keep_end: bool = False) -> str:
    """The cleaned text with every line behind ``| `` so that no line of it can begin a section, in at most ``limit`` escaped bytes
    AFTER the quoting (the bars count): the text is cut until the quoted whole fits, so the part keeps its start (or its end)."""
    body = clean(text).strip("\n")
    room = limit
    while True:
        piece = cut(body, room, keep_end=keep_end)
        out = "\n".join("| " + line for line in piece.split("\n")) if piece.strip() else "| (nothing)"
        over = size(out) - limit
        if over <= 0 or room <= 0:
            return out
        room = max(0, room - max(1, over))


def findings_text(kind: str, verdict: object, tail: object, scores: Sequence[object] = ()) -> str:
    """The ``findings`` of the payload. Sections named by us; everything else is quoted untrusted text. At most
    ``MAX_FINDINGS_BYTES`` bytes whatever the inputs are."""
    marks = [quoted(s, SCORE_BYTES // 8) for s in list(scores)[-8:]]  # the last eight, each short
    parts = [
        f"why this slice needs another round: {kind}",
        *(["the patch did not apply on the stack; this round changes only the slice's own branch, so the stack conflict is not rechecked:"
           " a READY verdict says nothing about it"] if kind == "conflict" else []),
        "the reviewer's text:", quoted(verdict, VERDICT_BYTES),
        "the end of the build's summary:", quoted(tail, TAIL_BYTES, keep_end=True),
        "score history:", "\n".join(marks) if marks else "| (none: this request has no score)",
    ]
    return cut("\n".join(parts), MAX_FINDINGS_BYTES)


def payload(*, issue: int, slice_id: str, round_number: int, run_id: int, branch: str, head: str, issue_pin: str,
            scores: str, findings: str) -> dict:
    """The ``client_payload``: exactly ``PAYLOAD_KEYS``. ``round_number`` is the NEXT round; ``scores`` the history (engine/hill.py)."""
    values = (issue, slice_id, round_number, run_id, branch, head, issue_pin, scores, findings)
    return dict(zip(PAYLOAD_KEYS, values, strict=True))


def request(data: Mapping) -> bytes:
    """The body of ``POST /repos/{repo}/dispatches`` as ASCII JSON: ``{"event_type", "client_payload"}``."""
    return (json.dumps({"event_type": EVENT_TYPE, "client_payload": dict(data)}, ensure_ascii=True) + "\n").encode("ascii")


def dispatch_argv(repo: str, path: str) -> list[str]:
    """The ``gh`` arguments that send the request file at ``path`` (``request``) to ``repo``'s dispatches: one shape for publish
    (a fix round) and the land job (a re-gate), JSON said so (R14)."""
    return ["api", "--method", "POST", f"repos/{repo}/dispatches", "-H", "Content-Type: application/json", "--input", path]


def leaks(body: bytes, token: str) -> bool:
    """True when the request body holds the token in any form the secret scan knows, or the scan cannot say."""
    return build.scan(body, ["GH_TOKEN"], {b"GH_TOKEN": token.encode()}).result != build.RESULT_OK
