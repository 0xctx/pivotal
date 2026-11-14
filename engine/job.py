"""What the ack and plan jobs share: gh reads and writes, the pin check, the level
ladder and the job output lines.

THE LEVEL LADDER (F4) has ONE implementation, here: ``read_level`` (the word), ``standing`` (the level a repo's facts
honour, for the land path: every fact required) and ``early`` (the same decision at the start of a run, when the reviewer
or the baseline is not known yet), ``level_allows`` and ``lands`` (what a level starts and what it may land) and
``check_present`` (whether PIVOTAL_CHECK runs a command: engine/run.sh's own ``_blank_check``). ack, plan and publish decide
through them, and ``piv doctor`` (which cannot import the engine) keeps a copy of the words and of ``check_present`` that
test/test_levels.py holds to these. That test also scans every engine module outside this one, subdirectories included, for a
level word used as a decision (a comparison, a display, a subscript, a branch, a match, a concatenation: test/levelscan.py),
so a second copy of the ladder or of the reviewer rule fails it.
Where the level is read: ack (``LEVEL``, ``CHECK``), plan decide (``LEVEL``, ``CHECK``; it emits ``level``), plan baseline (a
note only), publish (``LEVEL``, ``PLANNED_LEVEL`` = plan's ``level``, ``CHECK``, ``REVIEWER``, land.json's baseline) and
``piv doctor``. A level raised after plan started the run does not land it: publish holds at the lower of the two.

    level    starts work                                   lands                       requires (else it is held lower)
    watch    nothing; signals appear in the digest         nobody                      -
    ask      go, fix: the operator's chat, comment, label  the human (/pivotal land)   a check command
    prepare  as ask, plus event and incident (red CI,      the human, one "land it"    ask's, plus a green baseline
             webhooks), built before you check in          on a ready, green change
    land     as prepare                                    the pipeline, under the gate prepare's, plus a reviewer of a
                                                                                       different vendor configured in the
                                                                                       preset (the review job decides
                                                                                       availability per slice); a protected
                                                                                       branch means a PR, not a push

A level whose requirement is unmet is HELD at the next lower one, with a fixed reason word (``Standing.reasons``), never
silently: no check command holds everything at ``watch`` (ack and plan refuse loudly before any write); a baseline that is
not green holds ``prepare`` and ``land`` at ``ask`` (nothing is ready for one "land it": assemble drafts every slice); no
configured reviewer of a different vendor holds ``land`` at ``prepare`` (the review job decides availability per slice;
publish opens the ready stack PR and moves nothing). A
requirement whose fact is not known yet (the baseline at the plan job's start) is passed as ``None`` and not applied. The
word itself: unset or empty is the default, ``prepare``; anything else that is not exactly a level (a typo, ``Land``,
``land `` with padding, ``off``) REFUSES, loudly and before any write, so it builds and lands nothing (fail closed).

Both jobs run as ``python3 -I pivotal/engine/<job>.py``. Everything untrusted reaches them through env
and files, never through the command line (F2). A job refuses by raising ``JobError``. ``run_job`` turns
that into a message on stderr and exit 1, so a refusal is loud, and the callers make every check before
their first write.

The pin (``issue_pin`` input, from trust). The jobs read the issue again, compute ``trust.issue_pin`` on its
title and body and compare. For a ``go`` or ``fix`` run an empty pin and a pin that differs both refuse, for
trusted and untrusted runs alike: an untrusted ``go`` is read as data on the untrusted preset, which holds no
subscription secret and no write permission, but it is still the issue trust decided on. ``event`` and
``incident`` runs have no pin: ack creates their issue from a title and a link only.
"""

import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

if __package__ in (None, ""):  # run as a script under python3 -I, which puts no directory on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import hill, trust  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GH_TIMEOUT_SECONDS = 30
ROOT_LINK = "https://github.com/"
# The kinds the factory runs for. land and stop never reach it.
KINDS = ("go", "fix", "event", "incident")
PIN_KINDS = ("go", "fix")
PRESET_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
# 40 hex digits, and not the all-zero placeholder an unedited caller carries.
SHA = re.compile(r"(?!0{40})[0-9a-f]{40}")
# watch builds nothing. ask starts only your own work. prepare and land also start incidents and events.
LEVELS = ("watch", "ask", "prepare", "land")
DEFAULT_LEVEL = "prepare"
LEVEL_KINDS = {"watch": (), "ask": ("go", "fix"), "prepare": KINDS, "land": KINDS}

# Each job output and the only value shape it may have. A newline matches none of them.
MATRIX_MAX = 200000  # the longest matrix output, JSON in printable ASCII
RESULT_MAX = 20000  # the longest RESULT line report reads (engine/report.py)
OUTPUT_RULES: dict[str, re.Pattern[str]] = {
    "issue": re.compile(r"[1-9][0-9]{0,9}"),
    "base_sha": re.compile(r"(?:[0-9a-f]{40})?"),
    "ref": re.compile(r"[0-9a-f]{40}"),
    "baseline": re.compile(r"green|red|none"),
    "matrix": re.compile(rf"[ -~]{{0,{MATRIX_MAX}}}"),  # JSON with ensure_ascii: printable ASCII on one line
    "round": re.compile(r"[0-9]{1,4}"),
    "issue_pin": re.compile(r"(?:[0-9a-f]{64})?"),  # plan: the issue pin this run was trusted on (round 0's, for a fix round)
    "base_ref": re.compile(r"(?:[0-9a-f]{40})?"),  # plan: the commit the base is checked out at, a fix round's draft head
    # plan: the one refusal that names itself, a fixed word (engine/plan.py PLAN_WHY_OUTPUT); "" when there is none
    "plan_why": re.compile(r"[a-z_]{1,24}"),
    # plan: the hill-climb's score command (base64, engine/hill.py), its target and the score history of the rounds built; "" with no score
    "score": re.compile(rf"(?:{hill.ENCODED.pattern})?"),
    "target": re.compile(rf"(?:{hill.TARGET.pattern})?"),
    "scores": re.compile(r"[0-9.@;a-f]{0,400}"),
    "kaggle_needed": re.compile(r"true|false"),
    "kaggle_model": re.compile(r"(?:[a-z0-9][a-z0-9._-]{0,63})?"),  # plan: the model models-up serves, "" when none
    # models-up: whether an endpoint answers for the builds, and the one word that says why when it does not
    # (engine/modelsup.py WHY). The word is a fixed vocabulary, never text from Kaggle or the endpoint.
    "kaggle_ok": re.compile(r"true|false"),
    "kaggle_why": re.compile(r"[a-z_]{1,24}"),
    "quiet": re.compile(r"true|false"),
    "reviewer": re.compile(r"true|false"),  # plan: the preset can supply a reviewer of another vendor for every builder
    "level": re.compile(r"watch|ask|prepare|land"),  # plan: PIVOTAL_LEVEL as the run started with it (publish holds at the lower)
    # One line of JSON written with ensure_ascii (printable ASCII), at most RESULT_MAX characters.
    "result": re.compile(rf"[ -~]{{1,{RESULT_MAX}}}"),
    "published": re.compile(r"true|false"),
    "build_ok": re.compile(r"true|false"),
    "review_ok": re.compile(r"true|false"),
    "gate_ok": re.compile(r"true|false"),  # the gate job: true only for a green gate (engine/gatejob.py)
    "line": re.compile(r"[a-z0-9][a-z0-9-]{0,39}"),  # the build line (engine/lines.py), picked by plan
    "runner": re.compile(r"self-hosted|ubuntu-24\.04"),
    # assemble: the ids of the stacked slices, comma-joined (empty until the stack ticket), whether there is a stack, and
    # whether land.json and the bundle were made (the upload step reads it).
    "positions": re.compile(r"(?:[a-z0-9][a-z0-9-]{0,39}(?:,[a-z0-9][a-z0-9-]{0,39}){0,5})?"),
    "has_stack": re.compile(r"true|false"),
    # The same ids as a JSON array on one line, for the gate job's matrix (the expression language cannot split `positions`);
    # `[]` without a stack, never an empty string, so a consumer never parses nothing.
    "gate_matrix": re.compile(r'\[(?:"[a-z0-9][a-z0-9-]{0,39}"(?:,"[a-z0-9][a-z0-9-]{0,39}"){0,5})?\]'),
    "land_sha256": re.compile(r"[0-9a-f]{64}"),
    "assembled": re.compile(r"true|false"),
    # The land job (engine/land.py): what became of "land it", and the sealed commit it was about ("" when it never got that far).
    "land_outcome": re.compile(r"landed|regate|not_landed"),
    "sealed_sha": re.compile(r"(?:[0-9a-f]{40})?"),
}
HOSTED = "ubuntu-24.04"
SELF_HOSTED = "self-hosted"


# Characters that open or end markdown, a mention, a link or a field of an outcome message. Replaced by a space.
TEXT_STRIPPED = frozenset("#*[]()<>`@|~\\!&:/\"'·")
_WWW = re.compile(r"(?i)\bwww\.")


class JobError(Exception):
    """A refusal or a failed read. The message is safe to print: it names a rule, never untrusted text."""


def run_job(job: Callable[[Mapping[str, str]], None], env: Mapping[str, str], stderr: TextIO | None = None) -> int:
    stderr = stderr or sys.stderr
    try:
        job(env)
    except JobError as err:
        print(f"refused: {err}", file=stderr)
        return 1
    return 0


def clean_text(text: object, fallback: str, limit: int = 60) -> str:
    """Untrusted text as data: one printable line of at most ``limit`` characters, or the fallback.

    Control, bidi and other format characters, markdown and link syntax, ``@`` and the message separator
    become spaces, and ``www.`` loses its dot, so the text cannot forge a line, a mention or a link.
    """
    if not isinstance(text, str):
        return fallback
    kept = "".join(
        " " if (not c.isprintable() or unicodedata.category(c) in ("Cf", "Zs", "Zl", "Zp") or c in TEXT_STRIPPED) else c
        for c in text
    )
    line = " ".join(_WWW.sub("www ", kept).split())[:limit].strip()
    return line or fallback


def level_allows(level: str, kind: str) -> bool:
    return kind in LEVEL_KINDS[level]


def lands(level: str, kind: str) -> bool:
    """The pipeline may move the base branch for a run of this kind at this (effective) level: only ``land`` does."""
    return level == LEVELS[-1] and level_allows(level, kind)


def aims_to_land(level: str) -> bool:
    """The repo is configured to land (whatever its facts allow)."""
    return level == LEVELS[-1]


def may_land_by_hand(level: str) -> bool:
    """A human's "land it" may land a sealed stack at this level: every level that starts work (``LEVEL_KINDS[level]`` is
    non-empty). ``watch`` starts nothing, so it is false for ``watch``; the kind the run starts is checked separately by
    ``level_allows``."""
    return level in LEVELS and bool(LEVEL_KINDS[level])


_BLANK_COMMENT = re.compile(r"(^|[ \t\n\v\f\r;&])#.*$", re.MULTILINE)
_BLANK_SEPARATORS = re.compile(r"[ \t\n\v\f\r;&:]")


def check_present(text: str) -> bool:
    """Whether the check command (PIVOTAL_CHECK) runs anything: the negation of engine/run.sh's ``_blank_check``, which
    run_gate answers ``none`` for. Comments (``#`` at a line start or after a blank, ``;`` or ``&``) go first, then blanks, ``;``,
    ``&`` and ``:``; what is left is a command. ``:``, ``;``, ``&`` and ``# none yet`` are no command. test_levels runs both."""
    return bool(_BLANK_SEPARATORS.sub("", _BLANK_COMMENT.sub("", text)))


def read_level(value: str) -> str:
    """The repo's level. Unset is the default (prepare). A value that is not a level refuses: a typo must not run."""
    level = value or DEFAULT_LEVEL
    if level not in LEVELS:
        raise JobError(f"PIVOTAL_LEVEL must be one of {', '.join(LEVELS)}")
    return level


def read_planned_level(value: str) -> str:
    """The level plan emitted (``level``): exactly a level, never the default, so a missing output holds nothing open."""
    if value not in LEVELS:
        raise JobError("PLANNED_LEVEL is not the plan job's output")
    return value


@dataclass(frozen=True)
class Standing:
    """The level a repo's facts honour: ``level`` is what PIVOTAL_LEVEL says, ``effective`` what the facts allow, and
    ``reasons`` the fixed words for each step it was held down (empty when it was not)."""

    level: str
    effective: str
    reasons: tuple[str, ...] = ()

    @property
    def held(self) -> bool:
        return self.effective != self.level

    def note(self) -> str:
        return f"the level is {self.level}, held at {self.effective}: {', '.join(self.reasons)}"


REASON_NO_CHECK = "no_check_command"
REASON_NO_REVIEWER = "no_cross_vendor_reviewer"
REASON_LEVEL_CHANGED = "level_lowered_since_plan"


def _decide(level: str, check: object, baseline: str | None, reviewer: bool | None, planned: str | None) -> Standing:
    """The one decision. ``None`` for baseline, reviewer or planned means not known yet and not applied; the public entrances
    decide who may pass that."""
    if level not in LEVELS:
        raise JobError(f"PIVOTAL_LEVEL must be one of {', '.join(LEVELS)}")
    effective, reasons = level, []
    if planned is not None and LEVELS.index(planned) < LEVELS.index(effective):
        effective = planned
        reasons.append(REASON_LEVEL_CHANGED)
    if effective != "watch" and check is not True:
        return Standing(level, "watch", (*reasons, REASON_NO_CHECK))
    if effective == "land" and reviewer is not None and reviewer is not True:
        effective = "prepare"
        reasons.append(REASON_NO_REVIEWER)
    if effective in ("prepare", "land") and baseline is not None and baseline != "green":
        effective = "ask"
        reasons.append(f"baseline_{baseline if baseline in ('red', 'none') else 'red'}")
    return Standing(level, effective, tuple(reasons))


def standing(level: str, *, check: bool, baseline: str, reviewer: bool, planned: str) -> Standing:
    """THE decision of what a level is worth where the base can move (publish; see the module docstring). Every fact is
    required: ``check`` (a check command is set, ``check_present``), ``baseline`` (green, red or none; anything but exactly
    ``green`` holds), ``reviewer`` (a bool: the preset supplies a reviewer of another vendor for every builder; anything but
    exactly True holds) and ``planned`` (the level plan started the run at: the run is held at the lower of the two). A fact
    that is missing or of the wrong type is a JobError, never a pass; a level that is not one of the four is one too."""
    if not isinstance(reviewer, bool) or not isinstance(check, bool):
        raise JobError("the reviewer and the check command must be known, as true or false")
    if not isinstance(baseline, str):
        raise JobError("the baseline must be known, as green, red or none")
    if planned not in LEVELS:
        raise JobError("the level plan started the run at must be known")
    return _decide(level, check, baseline, reviewer, planned)


def early(level: str, *, check: bool, reviewer: bool | None = None, baseline: str | None = None) -> Standing:
    """The same decision at the start of a run (ack, plan, doctor), when the baseline or the reviewer may not be known yet.
    It only says why a level is held in the log: it can never land anything, because the base only moves through ``standing``."""
    return _decide(level, check, baseline, reviewer, None)


def read_start(level_text: str, check_text: str, kind: str) -> Standing | None:
    """ack's and plan's one decision. None when the level starts no run of this kind (quiet: the caller writes nothing);
    the Standing when it does; a JobError when it would, but there is no check command (PIVOTAL_CHECK), before any write."""
    level = read_level(level_text)
    if not level_allows(level, kind):
        print(f"quiet: the level is {level}, which starts no {kind}", file=sys.stderr)
        return None
    held = early(level, check=check_present(check_text))
    if held.effective == "watch":
        raise JobError(f"{held.note()}: set the check command (PIVOTAL_CHECK, piv init --check), then run again")
    return held


def read_repo(env: Mapping[str, str]) -> str:
    repo = env.get("GH_REPO", "")
    if not trust._valid_repo(repo):
        raise JobError("GH_REPO is not owner/name")
    return repo


def read_kind(env: Mapping[str, str]) -> tuple[str, bool]:
    """(kind, trusted). Untrusted runs exist only for ``go``."""
    kind = env.get("KIND", "")
    if kind not in KINDS:
        raise JobError(f"kind must be one of {', '.join(KINDS)}")
    flag = env.get("TRUSTED", "")
    if flag not in ("true", "false"):
        raise JobError("trusted must be true or false")
    trusted = flag == "true"
    if not trusted and kind != "go":
        raise JobError(f"an untrusted run can only be a go, not {kind}")
    return kind, trusted


RUN_ID_DIGITS = 20  # the same bound as the run link in lines.py, ack.py and event.py


def read_number(text: str, what: str, digits: int = 10) -> int:
    """A positive number of at most ``digits`` digits. 10 holds an issue number; a run id has 11 digits live
    (measured: 42942520152), so a run id is read with RUN_ID_DIGITS."""
    if re.fullmatch(r"[1-9][0-9]{0,%d}" % (digits - 1), text) is None:
        raise JobError(f"{what} is not a positive number")
    return int(text)


def read_preset_name(value: str) -> str:
    if PRESET_NAME.fullmatch(value) is None:
        raise JobError("the preset is unset or not a preset name")
    return value


class GhRefused(JobError):
    """gh did not run, or the server answered with a refusal (an HTTP 4xx in gh's message): nothing was done. Any other
    JobError from gh may have been done (a timeout, a failure with no HTTP status), so a caller must not read it as "not sent"."""


GH_REFUSAL = re.compile(r"\(HTTP 4[0-9]{2}\)")  # gh's own message for an API answer, e.g. "gh: Not Found (HTTP 404)"


def gh(env: Mapping[str, str], *args: str, binary: str = "gh", timeout: int = GH_TIMEOUT_SECONDS) -> str:
    """Run gh with the job's env and return stdout. Any failure is a JobError that names the command, not the output.
    ``binary`` is "gh", found on the PATH of ``env``, unless a caller names an absolute path. A GhRefused is a failure
    that did not start (the program could not be run) or that the server refused (gh's HTTP 4xx); its text is ours."""
    try:
        done = subprocess.run(
            [binary, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",  # an answer that is not UTF-8 is still an answer: a decode error must not read as "could not run" (R26)
            check=False,
            timeout=timeout,
            env=dict(env),
        )
    except (OSError, ValueError):
        raise GhRefused(f"gh {' '.join(args[:3])} could not run") from None  # the program never started
    except subprocess.SubprocessError:
        raise JobError(f"gh {' '.join(args[:3])} did not finish") from None  # a timeout: the request may have been made
    if done.returncode != 0:
        if GH_REFUSAL.search(done.stderr or ""):
            raise GhRefused(f"gh {' '.join(args[:3])} was refused (HTTP 4xx) with exit {done.returncode}")
        raise JobError(f"gh {' '.join(args[:3])} failed with exit {done.returncode}")
    return done.stdout


def gh_json(env: Mapping[str, str], *args: str, binary: str = "gh") -> object:
    try:
        return json.loads(gh(env, *args, binary=binary))
    except ValueError:
        raise JobError(f"gh {' '.join(args[:3])} did not answer JSON") from None


def fetch_issue(env: Mapping[str, str], repo: str, number: int) -> tuple[str, str]:
    """(title, body) of the issue. A pull request, another number or an odd shape is refused."""
    title, body, _labels = read_issue(env, repo, number)
    return title, body


def read_issue(env: Mapping[str, str], repo: str, number: int) -> tuple[str, str, list[str]]:
    """(title, body, label names) of the issue, from one read. A label entry that is not a named object is
    skipped: labels only ever pick among fixed lines, so an odd one selects nothing."""
    data = gh_json(env, "api", f"repos/{repo}/issues/{number}")
    if not isinstance(data, dict) or data.get("number") != number or "pull_request" in data:
        raise JobError(f"#{number} is not an issue of {repo}")
    title, body = data.get("title"), data.get("body")
    if body is None:
        body = ""
    if not isinstance(title, str) or not isinstance(body, str):
        raise JobError(f"issue #{number} has no readable title and body")
    raw = data.get("labels")
    names = [x["name"] for x in raw if isinstance(x, dict) and isinstance(x.get("name"), str)] if isinstance(raw, list) else []
    return title, body, names


def read_engine_sha(env: Mapping[str, str]) -> str:
    sha = env.get("ENGINE_SHA", "")
    if SHA.fullmatch(sha) is None:
        raise JobError("ENGINE_SHA is not a release commit SHA (the all-zero placeholder is refused)")
    return sha


def read_dispatch(env: Mapping[str, str], kind: str) -> Mapping | None:
    """The event of a ``pivotal-fix`` dispatch (a fix run from repository_dispatch), else None."""
    if kind != "fix" or env.get("GITHUB_EVENT_NAME") != "repository_dispatch":
        return None
    try:
        with open(env.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        raise JobError("the fix event file is missing or not JSON") from None
    return event if isinstance(event, dict) else {}


def check_pin(kind: str, pin: str, title: str, body: str, dispatch: Mapping | None = None) -> None:
    """Refuse a go or fix whose issue is not the one trust pinned. Other kinds have no pin.

    A fix from a dispatch has no issue object: its pin covers the payload's issue, slice, round, run_id, branch,
    head_sha and issue_pin (``trust.fix_pin``), checked against the event the job sees, and the payload's ``issue_pin`` (round 0's
    pin) must still be the pin of the live issue. Any other go or fix is pinned to the issue's title and body.
    """
    if kind not in PIN_KINDS:
        return
    if not pin:
        raise JobError("the pin is empty, so the issue or fix is not the one trust decided on")
    if dispatch is not None:
        if trust.fix_pin(dispatch) != pin:
            raise JobError("the fix payload differs from the one trust decided on")
        # The payload carries round 0's issue pin: a fix run's goal is the issue's text, so the live text must still be the one
        # trust decided on (F2b), however many rounds ago.
        if trust.issue_pin({"issue": {"title": title, "body": body}}) != dispatch["client_payload"]["issue_pin"]:
            raise JobError("the issue changed after trust decided: label or comment on it again")
        return
    if trust.issue_pin({"issue": {"title": title, "body": body}}) != pin:
        raise JobError("the issue changed after trust decided: label or comment on it again")


def require_label(env: Mapping[str, str], repo: str, name: str) -> None:
    """Refuse when the repo has no such label: adding an unknown one makes GitHub create it plain and grey."""
    try:
        gh(env, "api", f"repos/{repo}/labels/{quote(name, safe='')}")
    except JobError:
        raise JobError(f"the label {name} is missing or unreadable: run piv init on this repo") from None


def piv_init():
    """gh-piv/piv/init.py, the single definition of the labels and the lines. Import it at the top of a module that
    needs it: the build job removes the checkout from sys.path before the agent starts."""
    path = str(ROOT / "gh-piv")
    sys.path.insert(0, path)
    try:
        from piv import init
    finally:
        sys.path.remove(path)
    return init


def label_name(name: str) -> str:
    """A Pivotal label by its name in piv init's single definition (gh-piv/piv/init.py), or a JobError."""
    init = piv_init()
    if name not in {label for label, _colour, _text in init.LABELS}:
        raise JobError(f"{name} is not a label that piv init creates")
    return name


def read_output_path(env: Mapping[str, str]) -> str:
    path = env.get("GITHUB_OUTPUT", "")
    if not path:
        raise JobError("GITHUB_OUTPUT is not set")
    return path


def write_outputs(path: str, outputs: Mapping[str, str], replace: bool = False) -> None:
    """Append ``name=value`` lines to the GITHUB_OUTPUT file, after every value is checked.

    A name or value that breaks its rule writes nothing, so no newline can start a second output.

    ``replace`` is for a step that runs model output before it writes: the file is then emptied first, and a
    symlink or a non-regular file is refused. GITHUB_OUTPUT is per step and holds only this step's lines, so
    truncating it removes nothing legitimate, and it removes what an agent planted (a forged line, or a
    ``name<<DELIMITER`` that would swallow the engine's own lines).
    """
    lines = []
    for name, value in outputs.items():
        rule = OUTPUT_RULES.get(name)
        if rule is None or not isinstance(value, str) or rule.fullmatch(value) is None:
            raise JobError(f"output {name} has no valid value")
        lines.append(f"{name}={value}\n")
    if replace:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as err:
            raise JobError(f"GITHUB_OUTPUT cannot be replaced: {err.strerror}") from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise JobError("GITHUB_OUTPUT is not a regular file")
            fh.write("".join(lines))
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(lines))
