"""Build lines: a line is a recipe, a brief, a gate and optionally a score and a preset.

The four built-in lines are ``lines/<name>.json`` (the names come from ``piv init``'s LINES, the trigger labels
from its LABELS). A repo adds its own under the key ``lines`` of its ``PIVOTAL_PRESET_JSON`` variable. Both go
through one validator (``validate_line``):

- ``name``: ``[a-z0-9][a-z0-9-]{0,39}``. A custom line may not take a built-in's name.
- ``summary``: one printable line, at most SUMMARY_MAX characters.
- ``prompt``: a file name from PROMPTS, the briefs under ``engine/prompts/``, and nothing else: no path, no
  other file. The file must be a regular file of at most PROMPT_MAX bytes. A custom line picks one of them.
- ``gate``: a recipe name from GATES. ``standard`` is the repo's own check. ``docs`` adds the documented
  command list ``GATES["docs"]`` (links, fences, spelling: ``engine/doccheck.py``) after the repo's check.
  Both always run the repo's check first, so no recipe is weaker than the repo's own gate.
- ``score`` (optional): ``{"scorer": "image", "target": 0..1, "max_rounds": 1..10}``. The ``image`` scorer's code is wired
  (WIRED_SCORERS): engine/score/image.py prints the SSIM of a headless screenshot against a mockup, and the plan job's
  command is ``IMAGE_SCORE_COMMAND``, which the build and gate jobs anchor to the engine checkout. Its inputs are not supplied
  by any job yet (SUPPLIED_INPUTS is empty), so a line with it is refused at plan with ``line_not_wired``, and the message
  names the missing inputs. A scorer that is not in WIRED_SCORERS is refused the same way, rather than run without a score.
  ``max_rounds`` is validated and
  not read: the hill-climb's bound is ``hill.MAX_ROUNDS`` (a test holds the two equal), and the fix rounds of a failed review are bounded by ``plan.DEFAULT_MAX_ROUNDS``.
- ``preset`` (optional): a preset name this line builds with instead of the caller's.
- ``request`` (optional): ``issue`` (default) hands the agent the issue's title and body as data. ``link``
  hands it only a fixed title and the first github.com link in the body: the issue text never reaches the
  brief. ``incident`` is a ``link`` line, and its prompt has no place for alert text.

Unknown keys, duplicate keys, wrong types and oversize values are refused. The error messages name a rule and
never echo the file's text.

Which line runs. ``line_for_labels`` reads the issue's labels: exactly one line label (``pivotal:<line>`` for a
built-in or a defined custom line) picks it, none picks ``build``, two refuse. Status labels (``pivotal:building``
and the rest of piv init's labels) are not lines; any other ``pivotal:*`` label is ignored with a note. ``choose``
adds the trust rule: the ``issues: labeled`` payload names the issue's author and, in ``sender``, the person who
applied the label, but trust.py decides the whole run on the author (or a writer's ``/pivotal go trusted``) and
nothing pins the label, so an untrusted run only ever gets the ``build`` line (a visible ``fell_back:`` line),
and a failed incident or event run is the ``incident`` line whatever the labels say.
"""

import json
import os
import re
import shlex
import stat
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script under python3 -I
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import hill, job  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_INIT = job.piv_init()  # at import: the build job drops the checkout from sys.path before the agent starts

BUILTIN = tuple(_INIT.LINES)
DEFAULT = "build"
LABEL_NAMES = frozenset(label for label, _colour, _text in _INIT.LABELS)
LINE_LABELS = {f"pivotal:{name}": name for name in BUILTIN if f"pivotal:{name}" in LABEL_NAMES}
# The labels that report state, not a line: piv init's labels without the trigger and the line labels.
STATUS_LABELS = LABEL_NAMES - {"pivotal"} - set(LINE_LABELS)

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
PRESET_NAME = job.PRESET_NAME
PROMPTS = tuple(f"{name}.md" for name in BUILTIN)
PROMPT_MAX = 16 * 1024
FILE_MAX = 8 * 1024  # a line file
SPEC_MAX = 64 * 1024  # PIVOTAL_PRESET_JSON
CUSTOM_MAX = 8
SUMMARY_MAX = 200
REQUIRED = frozenset({"name", "summary", "prompt", "gate"})
OPTIONAL = frozenset({"score", "preset", "request"})
SCORE_KEYS = frozenset({"scorer", "target", "max_rounds"})
SCORERS = ("image",)
WIRED_SCORERS: frozenset[str] = frozenset({"image"})  # the mirror scorer's code is wired (engine/score/image.py); the footer's --score/--target hill-climb is engine/hill.py
# What a scorer needs from the factory. The image scorer needs three inputs that no job supplies yet: the app's start command
# (piv init writes PIVOTAL_START; no job reads it), the pivotal-assets checkout (no job checks it out) and a headless browser CLI
# (none is configured). Until a ticket supplies them (SUPPLIED_INPUTS), a line with that scorer is refused at plan: line_not_wired.
SCORER_INPUTS: dict[str, tuple[str, ...]] = {
    "image": ("the app's start command (the start input)", "the pivotal-assets mockup checkout", "a headless browser CLI"),
}
SUPPLIED_INPUTS: dict[str, frozenset[str]] = {"image": frozenset()}  # nothing supplies them yet: the mirror line is refused by default
# The image scorer's command: a template. {scorer} is replaced by the quoted absolute path of engine/score/image.py in the
# engine checkout the build and gate jobs run (buildjob.score_command), so a repo's own file or score.txt cannot set the score.
IMAGE_SCORE_COMMAND = "python3 -I {scorer}"
REQUESTS = ("issue", "link")
LINK_TITLE = "Fix the failure at the linked run"
# The incident goal: a run of this repo, https://github.com/<repo>/actions/runs/<id>, and nothing else.
LINK_TAIL = r"/actions/runs/[0-9]{1,20}(?:/job/[0-9]{1,20})?"

# The gate recipes: the commands each adds after the repo's check, as templates. {doccheck} is replaced by the
# quoted path of engine/doccheck.py. A recipe is data in this table, never a string from a line file.
GATES: dict[str, tuple[str, ...]] = {
    "standard": (),
    "docs": ("python3 -I {doccheck} .",),
}
# A built-in line whose gate is fixed: its file may not name another recipe.
REQUIRED_GATE = {"docs": "docs"}


class LineError(Exception):
    """A refusal. The message names a rule, never untrusted text."""


@dataclass(frozen=True)
class Score:
    scorer: str
    target: float
    max_rounds: int


@dataclass(frozen=True)
class Line:
    name: str
    summary: str
    prompt: str
    gate: str
    score: Score | None = None
    preset: str | None = None
    request: str = "issue"
    builtin: bool = False


@dataclass(frozen=True)
class Choice:
    name: str
    notes: tuple[str, ...] = ()


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _loads(text: str, what: str) -> object:
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        raise LineError(f"{what} is not valid JSON, or repeats a key") from None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _score(data: object) -> Score:
    if not isinstance(data, dict) or set(data) != SCORE_KEYS:
        raise LineError(f"score must be an object with exactly {', '.join(sorted(SCORE_KEYS))}")
    scorer, target, rounds = data["scorer"], data["target"], data["max_rounds"]
    if scorer not in SCORERS:
        raise LineError(f"score.scorer must be one of {', '.join(SCORERS)}")
    if not _is_number(target) or not 0 < target <= 1:
        raise LineError("score.target must be a number above 0 and at most 1")
    try:
        hill.check_target(f"{float(target)}")  # the text the plan job writes: refused here, not later at the score step
    except hill.HillError:
        raise LineError("score.target must be a number above 0 and at most 1, with at most six decimals") from None
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 10:
        raise LineError("score.max_rounds must be a whole number from 1 to 10")
    return Score(scorer, float(target), rounds)


def read_regular(path: Path, limit: int) -> bytes:
    """The bytes of a regular file of at most ``limit`` bytes. A symlink, another kind of file (a FIFO would
    hang a read) and a larger file raise LineError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise LineError(f"{path.name} is missing or is a symlink") from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise LineError(f"{path.name} is not a regular file")
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise LineError(f"{path.name} is larger than {limit} bytes")
    return data


def prompt_text(prompt: str, root: Path = ROOT) -> str:
    """The brief of a prompt name from PROMPTS, from ``engine/prompts/``. Any other name refuses."""
    if prompt not in PROMPTS:
        raise LineError(f"prompt must be one of {', '.join(PROMPTS)}")
    try:
        return read_regular(root / "engine" / "prompts" / prompt, PROMPT_MAX).decode("utf-8")
    except UnicodeDecodeError:
        raise LineError(f"{prompt} is not UTF-8") from None


def validate_line(data: object, builtin: bool, root: Path = ROOT) -> Line:
    """A Line from parsed JSON, or LineError. ``builtin`` is for the files under ``lines/``; a custom line may
    not take a built-in's name."""
    if not isinstance(data, dict):
        raise LineError("a line must be a JSON object")
    missing = REQUIRED - set(data)
    if missing:
        raise LineError(f"a line needs {', '.join(sorted(REQUIRED))}")
    if set(data) - REQUIRED - OPTIONAL:
        raise LineError(f"a line has only the keys {', '.join(sorted(REQUIRED | OPTIONAL))}")
    name, summary, prompt, gate = data["name"], data["summary"], data["prompt"], data["gate"]
    if not isinstance(name, str) or NAME.fullmatch(name) is None:
        raise LineError("name must match [a-z0-9][a-z0-9-]{0,39}")
    if not builtin and name not in BUILTIN and f"pivotal:{name}" in LABEL_NAMES:
        raise LineError("a custom line may not take the name of a status label")
    if builtin != (name in BUILTIN):
        raise LineError("a custom line may not take a built-in line's name" if not builtin else "not a built-in line")
    if not isinstance(summary, str) or not summary or len(summary) > SUMMARY_MAX or not summary.isprintable():
        raise LineError(f"summary must be one printable line of at most {SUMMARY_MAX} characters")
    if not isinstance(prompt, str):
        raise LineError("prompt must be a string")
    prompt_text(prompt, root)  # in the allowlist, a regular file, small, UTF-8
    if not isinstance(gate, str) or gate not in GATES:
        raise LineError(f"gate must be one of {', '.join(GATES)}")
    if builtin and REQUIRED_GATE.get(name, gate) != gate:
        raise LineError(f"the {name} line keeps the {REQUIRED_GATE[name]} gate")
    score = _score(data["score"]) if "score" in data else None
    preset = data.get("preset")
    if "preset" in data and (not isinstance(preset, str) or PRESET_NAME.fullmatch(preset) is None):
        raise LineError("preset must be a preset name")
    request = data.get("request", "issue")
    if request not in REQUESTS:
        raise LineError(f"request must be one of {', '.join(REQUESTS)}")
    return Line(name, summary, prompt, gate, score, preset, request, builtin)


def load_line(name: str, root: Path = ROOT) -> Line:
    """The built-in line ``name``, from ``lines/<name>.json``."""
    if name not in BUILTIN:
        raise LineError("not a built-in line")
    data = _loads(read_regular(root / "lines" / f"{name}.json", FILE_MAX).decode("utf-8", errors="replace"), "a line file")
    line = validate_line(data, builtin=True, root=root)
    if line.name != name:
        raise LineError("a line file is named for another line")
    return line


def builtin_lines(root: Path = ROOT) -> dict[str, Line]:
    return {name: load_line(name, root) for name in BUILTIN}


def custom_lines(preset_json: str, root: Path = ROOT) -> tuple[dict[str, Line], list[str]]:
    """(lines, problems) from the ``lines`` key of PIVOTAL_PRESET_JSON. An entry that does not validate is left
    out and named in a problem by its position; the other entries stand. An unset variable has none."""
    if not preset_json.strip():
        return {}, []
    if len(preset_json.encode("utf-8")) > SPEC_MAX:
        return {}, [f"PIVOTAL_PRESET_JSON is over {SPEC_MAX} bytes: its custom lines are ignored"]
    try:
        data = _loads(preset_json, "PIVOTAL_PRESET_JSON")
    except LineError as err:
        return {}, [f"{err}: its custom lines are ignored"]
    raw = data.get("lines") if isinstance(data, dict) else None
    if raw is None:
        return {}, []
    if not isinstance(raw, list) or len(raw) > CUSTOM_MAX:
        return {}, [f"lines must be a list of at most {CUSTOM_MAX} objects: the custom lines are ignored"]
    found: dict[str, Line] = {}
    problems = []
    for index, entry in enumerate(raw):
        try:
            line = validate_line(entry, builtin=False, root=root)
            if line.name in found:
                raise LineError("two custom lines have one name")
        except LineError as err:
            problems.append(f"custom line {index + 1} is ignored: {err}")
            continue
        found[line.name] = line
    return found, problems


def line_for_labels(labels: Iterable[object], custom: Mapping[str, Line] | None = None) -> Choice:
    """The line the labels pick: the one line label, ``build`` for none, a LineError for two or more.

    Only an exact ``pivotal:<name>`` label of a built-in or a defined custom line counts. A status label is not a
    line. Any other ``pivotal:*`` label is ignored with a note. Label text is never echoed."""
    by_label = {**LINE_LABELS, **{f"pivotal:{name}": name for name in (custom or {})}}
    found: list[str] = []
    ignored = 0
    for label in labels:
        if not isinstance(label, str):
            continue
        name = by_label.get(label)
        if name is not None:
            if name not in found:
                found.append(name)
        elif label.startswith("pivotal:") and label not in STATUS_LABELS:
            ignored += 1
    notes = (f"note: {ignored} pivotal:* label{'s' if ignored != 1 else ''} name no line and {'are' if ignored != 1 else 'is'} ignored",) if ignored else ()
    if len(found) > 1:
        raise LineError(f"the issue has {len(found)} line labels ({', '.join(sorted(found))}): keep exactly one")
    return Choice(found[0] if found else DEFAULT, notes)


def choose(kind: str, trusted: bool, labels: Iterable[object], custom: Mapping[str, Line] | None = None) -> Choice:
    """The line for a run: see the module docstring. A refusal is a LineError."""
    if kind in ("incident", "event"):
        return Choice("incident")
    choice = line_for_labels(labels, custom)
    if trusted or choice.name == DEFAULT:
        return choice
    note = f"fell_back: line: the run is untrusted, so it uses the {DEFAULT} line, not {choice.name}"
    return Choice(DEFAULT, (*choice.notes, note))


def resolve(name: str, preset_json: str = "", root: Path = ROOT) -> Line:
    """The line named by the plan job's ``line`` output, built-in or custom, or a LineError."""
    if NAME.fullmatch(name) is None:
        raise LineError("the line name is not a line name")
    if name in BUILTIN:
        return load_line(name, root)
    found, _problems = custom_lines(preset_json, root)
    if name not in found:
        raise LineError("no such line")
    return found[name]


def check_wired(line: Line) -> None:
    """Refuse a line whose scorer is not wired or whose inputs the factory does not supply: ``line_not_wired``. It does not
    fall back to build, because a line with a score run without one would pass. Plan, build and gate all call this."""
    if line.score is None:
        return
    scorer = line.score.scorer
    if scorer not in WIRED_SCORERS:
        raise LineError(f"line_not_wired: the {line.name} line needs the {scorer} scorer, which is not wired yet")
    missing = [name for name in SCORER_INPUTS.get(scorer, ()) if name not in SUPPLIED_INPUTS.get(scorer, frozenset())]
    if missing:
        raise LineError(f"line_not_wired: the {line.name} line needs the {scorer} scorer, and the factory does not supply yet: "
                        + "; ".join(missing))


def link_request(body: str, repo: str) -> tuple[str, str]:
    """(title, goal) of a ``link`` line: a fixed title and the first run link of ``repo`` in the issue body.
    Nothing else of the issue reaches the brief. No such link refuses."""
    # The link ends at a space, a carriage return, a line feed or the end of the body. Not \s: that also admits a tab (S3).
    found = re.search(r"https://github\.com/" + re.escape(repo) + LINK_TAIL + r"(?=[ \r\n]|\Z)", body)
    if found is None:
        raise LineError(f"incident_no_link: the issue body has no link to a run of {repo}, so there is nothing to give the agent")
    return LINK_TITLE, f"The failing run or report: {found.group(0)}"


def gate_check(line: Line, repo_check: str, engine_dir: Path) -> str:
    """The CHECK for run_gate: the repo's own check (in a subshell, so an ``exit`` in it ends only itself), then
    the recipe's commands. A blank repo check leaves the recipe's commands alone, and a standard line with a
    blank check stays blank: run_gate says ``none``."""
    extra = [c.format(doccheck=shlex.quote(str(engine_dir / "engine" / "doccheck.py"))) for c in GATES[line.gate]]
    if not extra:
        return repo_check
    if not repo_check.strip():
        return "\n".join(extra)
    return "(\n" + repo_check + "\n)\n" + "\n".join(extra)


def preset_for(line: Line, name: str) -> str:
    """The preset the run builds with: the line's own, else the caller's."""
    return line.preset or name
