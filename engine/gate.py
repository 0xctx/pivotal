"""The gate artifact (`gate[k]`, F4): what one gate job says about one stack prefix.

``gate-<id>/gate.json`` is written by engine/gatejob.py after it checked out the head of the stack at slice ``<id>``
and ran the repo's check on it. The later ``publish`` and ``land`` jobs read it as hostile input (it was made in a job
that ran repo code), so they import the validators here and never trust a file that has not been through them:

- ``check_gate`` / ``read_gate``: exact keys, the types, and the one rule that matters: ``green`` is true exactly when
  the check said ``green``, HEAD is unchanged and the tree is clean. A file that says otherwise is refused, so a gate
  that was edited to say green is not green (F5).
- ``confirm``: the gate belongs to ``land.json``. Its ``id`` is a stacked slice, its ``issue`` is the run's and its
  ``sha`` is that slice's head, the commit the gate job was asked to check. Anything else is a different commit and is
  refused.

Keys: ``version``, ``issue``, ``id``, ``sha`` (the 40-hex commit that was checked), ``check`` (what ``run_gate``
printed: green, red or none; ``none`` is an empty check and is never green), ``head_unchanged`` (``git rev-parse HEAD``
after the check is ``sha``), ``clean`` (``git status --porcelain`` after the check is empty, see
engine/gatejob.py ``tree_is_clean``), ``green``, and ``score`` (null for a request with no score; else the number from 0 to 1
the score command printed) and ``target`` (null, or the request's target text: engine/hill.py; green needs the score at or above it). A prefix is landable only with a ``green`` gate; the longest all-green
prefix with matching SHAs is the publish job's choice, not this module's.
"""

import json
import math
from collections.abc import Mapping
from pathlib import Path

from engine import assemble, hill, plan

GATE = "gate.json"
GATE_VERSION = 2  # 2: the hill-climb's `target` key (engine/hill.py); a score is green only at or above it
MAX_GATE_BYTES = 4 * 1024
CHECKS = ("green", "red", "none")
GATE_KEYS = frozenset({"version", "issue", "id", "sha", "check", "head_unchanged", "clean", "green", "score", "target"})


class GateError(ValueError):
    """A gate.json breaks the schema, or does not belong to its land.json. The message names the rule, never a value."""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_green(check: str, head_unchanged: bool, clean: bool, score: float | None = None, target: str | None = None) -> bool:
    """The one definition of a green gate: the check passed and left HEAD and the tree as it found them, and, for a request with a
    score, the score the command printed meets the target (engine/hill.py ``met``: no score is not met)."""
    return check == "green" and head_unchanged is True and clean is True and hill.met(score, target)


def check_gate(data: object) -> dict:
    """The gate.json of a gate job, if it follows the schema exactly; else GateError."""
    if not isinstance(data, dict):
        raise GateError("gate.json keys are not exactly the schema's")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != GATE_VERSION:  # first: a gate of an older version has other keys
        raise GateError(f"version must be {GATE_VERSION}")
    if set(data) != GATE_KEYS:
        raise GateError("gate.json keys are not exactly the schema's")
    issue = data["issue"]
    if not isinstance(issue, int) or isinstance(issue, bool) or not 1 <= issue <= 9999999999:
        raise GateError("issue must be a positive number")
    slice_id = data["id"]
    if not isinstance(slice_id, str) or plan.ID_PATTERN.fullmatch(slice_id) is None or slice_id in plan.RESERVED_IDS:
        raise GateError("id is not a slice id")
    if not assemble._sha(data["sha"]) or data["sha"] == "0" * 40:
        raise GateError("sha must be a commit SHA")
    if data["check"] not in CHECKS:
        raise GateError(f"check must be one of {', '.join(CHECKS)}")
    for name in ("head_unchanged", "clean", "green"):
        if not isinstance(data[name], bool):
            raise GateError(f"{name} must be true or false")
    score, target = data["score"], data["target"]
    if score is not None and not (_is_number(score) and 0 <= score <= 1 and math.isfinite(score)):
        raise GateError("score must be null or a number from 0 to 1")
    if target is not None and not hill.is_target(target):
        raise GateError("target must be null or a score target")
    if score is not None and target is None:
        raise GateError("a score needs a target")
    if data["green"] != is_green(data["check"], data["head_unchanged"], data["clean"], score, target):
        raise GateError("green must be true exactly when the check is green, HEAD is unchanged, the tree is clean and any score meets the target")
    return data


def gate_text(gate: Mapping) -> bytes:
    """The bytes of gate.json: validated, ASCII, fixed key order."""
    check_gate(gate)
    return (json.dumps(gate, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def read_gate(path: Path, dir_fd: int | None = None) -> dict:
    """gate.json from a downloaded artifact: a regular file within the cap, JSON without duplicate keys, then
    ``check_gate``. For the publish job. Anything else is a GateError. ``dir_fd``: ``path`` is relative to this open directory
    (so no component above it can be a link someone planted)."""
    try:
        data = assemble.read_regular(Path(path), MAX_GATE_BYTES, dir_fd)
    except assemble.ArtifactError as err:
        raise GateError(f"gate.json is unreadable: {err}") from None
    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=plan._no_duplicate_keys)
    except (ValueError, RecursionError):
        raise GateError("gate.json is not valid JSON") from None
    return check_gate(parsed)  # outside the try: its GateError names the rule


def confirm(land: Mapping, gate: Mapping) -> None:
    """The gate is the one land.json asks for: a stacked slice's head, on this run's issue. Raises GateError.

    ``land`` must already have been through ``assemble.check_land`` (``assemble.read_land``)."""
    check_gate(gate)
    if gate["issue"] != land["issue"]:
        raise GateError("the gate is for another issue")
    if gate["id"] not in land["positions"]:
        raise GateError("the gate is for a slice that is not on the stack")
    entry = next(s for s in land["slices"] if s["id"] == gate["id"])
    if gate["sha"] != entry["head"]:
        raise GateError("the gate checked another commit than the slice's head")
    if gate["target"] != (entry["target"] or None):
        raise GateError("the gate was made for another score target than the slice's")
