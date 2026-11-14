"""Request to one validated slice, and the plan job.

Plan output is untrusted, so the slice is checked here before a build job sees it (F5). Each
rule raises SliceError with a message that names the rule. Text is checked for control and format
characters: a title has no newline, CR or character of Unicode category Cc, Cf, Zl or Zp; a goal may
hold newline and tab and no other such character. Run ``python3 -m engine.plan`` with a JSON list of
slices on stdin to see the rule that rejects one.

The plan job runs two steps, ``python3 -I pivotal/engine/plan.py decide`` and ``... baseline``, with data
in env and files (F2). The job is ``contents: read`` and ``issues: read``. ``decide`` holds the token and
runs no repo code. ``baseline`` checks the repo out at the base, holds no token, and runs the repo's setup
and check only through ``run_gate`` of engine/run.sh, whose environment is stripped (F1).

``decide`` exits quietly when the level forbids the kind: empty ``matrix``, round 0, nothing read or
checked out, and the later steps skip. Otherwise it refuses (non-zero, nothing written) on a bad kind,
preset, pin or fix payload, then prints ``matrix``, ``round`` and ``kaggle_needed``. ``matrix`` is a JSON
list of one slice, from ``request_slices``. A request whose title or goal is over its bound (TITLE_MAX,
GOAL_MAX) is refused by name before any slice is made: ``plan_why`` is ``request_too_large`` in the job
outputs and the job exits 1, so the report names that refusal instead of the generic ``plan_failed``. A
person's text is normalised first (CRLF to LF) and control or format characters are removed
with a note, keeping ZWJ and ZWNJ between two non-space characters; a plan whose slices, as the ``matrix``
output, are over the output cap (``job.MATRIX_MAX``; an astral character counts 12) is refused.
``round`` is 0 for a run from an issue or a comment, and the payload's round for a ``pivotal-fix``
dispatch, which is refused when it is above ``max_rounds``, and ``base_ref`` is not written except for a fix round, where it is the
draft's ``head_sha``: the workflow checks the base out at it, so the whole fix run (baseline, build, review, assemble)
treats the draft's head as its base and the fix's patch is one commit on it (a fast-forward of the draft branch). ``baseline`` prints ``base_sha`` and
``baseline`` (green, red or none).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script under python3 -I
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import hill, job, lines, route  # noqa: E402
from engine.job import JobError  # noqa: E402

MAX_SLICES = 6  # F8
DEFAULT_MAX_ROUNDS = 1  # F8
# The baseline's budget (F8). The job's timeout-minutes is derived from it: test_factory checks they agree.
BASELINE_SECONDS = 900
PLAN_JOB_OVERHEAD_MINUTES = 5  # checkouts, the issue read and the output lines
ROOT = Path(__file__).resolve().parent.parent
RUN_SH = ROOT / "engine" / "run.sh"
SINGLE_SLICE_ID = "main"
SLICE_KEYS = frozenset({"id", "title", "goal"})
ID_RULE = "^[a-z0-9][a-z0-9-]{0,39}$"
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
RESERVED_IDS = frozenset({"stack", "base"})
FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})  # control, format, line and paragraph separators
TITLE_MAX = 72
GOAL_MAX = 4000
PLAN_MAX_BYTES = 256 * 1024  # a valid slice set is far smaller; more is not read
# The plan job's one refusal that names itself: the output word, and the word report.py reads back as the outcome's why.
PLAN_WHY_OUTPUT = "plan_why"
REQUEST_TOO_LARGE = "request_too_large"


class SliceError(ValueError):
    """A slice, slice set or fix round breaks a rule. The message names the rule."""


class MatrixTooLarge(SliceError):
    """The slices, as the plan job's ``matrix`` output, are over the output cap."""


class RequestTooLarge(SliceError):
    """The request's own text is over a bound, so no slice can be made from it (report's ``request_too_large``)."""


JOINERS = "\u200c\u200d"  # ZWNJ and ZWJ: part of emoji sequences and Persian words


def _joins(text: str, index: int) -> bool:
    """Whether text[index] is a ZWNJ or ZWJ between two non-space characters."""
    return text[index] in JOINERS and 0 < index < len(text) - 1 and not text[index - 1].isspace() and not text[index + 1].isspace()


@dataclass(frozen=True)
class Slice:
    id: str
    title: str
    goal: str


def matrix_json(slices: Sequence[Slice]) -> str:
    """The plan job's ``matrix`` output for these slices (printable ASCII, so an astral character is 12 characters)."""
    return json.dumps([asdict(item) for item in slices])


def check_slice(data: object) -> Slice:
    """A valid Slice, or SliceError. ZWJ and ZWNJ pass only between two non-space characters (emoji
    sequences, Persian words); every other control or format character is refused."""
    if not isinstance(data, Mapping):
        raise SliceError("slice must be an object")
    keys = set(data)
    unknown = keys - SLICE_KEYS
    if unknown:
        raise SliceError(f"unknown keys: {sorted(unknown)}")
    missing = SLICE_KEYS - keys
    if missing:
        raise SliceError(f"missing keys: {sorted(missing)}")
    slice_id = data["id"]
    if not isinstance(slice_id, str):
        raise SliceError("id must be a string")
    if not ID_PATTERN.fullmatch(slice_id):
        raise SliceError(f"id must match {ID_RULE}")
    if slice_id in RESERVED_IDS:
        raise SliceError(f"id {slice_id!r} is reserved")
    return Slice(slice_id, _text(data, "title", TITLE_MAX, ""), _text(data, "goal", GOAL_MAX, "\n\t"))


def _text(data: Mapping, key: str, limit: int, allowed: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise SliceError(f"{key} must be a string")
    if not value.strip():
        raise SliceError(f"{key} must not be empty")
    if len(value) > limit:
        raise RequestTooLarge(f"{key} must be at most {limit} characters")
    for index, char in enumerate(value):
        if char in allowed or unicodedata.category(char) not in FORBIDDEN_CATEGORIES:
            continue
        if _joins(value, index):
            continue
        raise SliceError(f"{key} must not contain control or format characters (U+{ord(char):04X})")
    return value


def check_slices(data: object) -> tuple[Slice, ...]:
    if not isinstance(data, list):
        raise SliceError("slices must be a list")
    if not data:
        raise SliceError("at least one slice is needed")
    if len(data) > MAX_SLICES:
        raise SliceError(f"at most {MAX_SLICES} slices per run, got {len(data)}")
    slices = []
    for position, item in enumerate(data, start=1):
        try:
            slices.append(check_slice(item))
        except SliceError as err:
            raise SliceError(f"slice {position}: {err}") from err
    seen: set[str] = set()
    for position, item in enumerate(slices, start=1):
        if item.id in seen:
            raise SliceError(f"slice {position}: duplicate id {item.id!r}")
        seen.add(item.id)
    goals: set[str] = set()
    for position, item in enumerate(slices, start=1):
        goal = " ".join(item.goal.casefold().split())
        if goal in goals:
            raise SliceError(f"slice {position}: goal repeats an earlier slice's goal")
        goals.add(goal)
    if len(matrix_json(slices)) > job.MATRIX_MAX:
        raise MatrixTooLarge(f"the slices as the matrix output are over {job.MATRIX_MAX} characters")
    return tuple(slices)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # json keeps the last of two equal keys without a word, so a plan could hide a second id.
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SliceError(f"duplicate key {key!r}")
        result[key] = value
    return result


def parse_slices(text: str) -> tuple[Slice, ...]:
    if len(text) > PLAN_MAX_BYTES:
        raise SliceError(f"plan is larger than {PLAN_MAX_BYTES} bytes")
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as err:
        raise SliceError(f"not valid JSON: {err.msg}") from err
    except SliceError:
        raise
    except RecursionError as err:
        raise SliceError("not valid JSON: nested too deeply") from err
    except ValueError as err:  # a number with thousands of digits
        raise SliceError("not valid JSON: unreadable number") from err
    return check_slices(data)


def request_slices(title: str, goal: str) -> tuple[Slice, ...]:
    """One request is one slice. A title or goal over its bound is a RequestTooLarge, before any slice is made."""
    return (check_slice({"id": SINGLE_SLICE_ID, "title": title, "goal": goal}),)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_fix_round(round_number: object, max_rounds: object = DEFAULT_MAX_ROUNDS) -> int:
    if not _is_int(max_rounds) or max_rounds < 0:
        raise SliceError("max_rounds must be a non-negative integer")
    if not _is_int(round_number) or round_number < 1:
        raise SliceError("round must be a positive integer")
    if round_number > max_rounds:
        raise SliceError(f"round {round_number} is above max_rounds {max_rounds}")
    return round_number


def clean_human_text(text: str, keep_newlines: bool) -> tuple[str, int]:
    """(text, number removed): a person's text made fit for the slice rules. CRLF and CR become LF; control,
    format and line-separator characters are removed, except tab and LF where ``keep_newlines``, and a
    ZWNJ or ZWJ between two non-space characters. Model output never goes through here (parse_slices)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept, removed = [], 0
    for index, char in enumerate(text):
        if char in ("\n", "\t") and keep_newlines:
            kept.append(char)
        elif unicodedata.category(char) not in FORBIDDEN_CATEGORIES:
            kept.append(char)
        elif _joins(text, index):
            kept.append(char)
        else:
            removed += 1
    return "".join(kept), removed


def human_slice_text(title: str, body: str) -> tuple[str, str, list[str]]:
    """(title, goal, notes) for a request: see ``one_slice_text``, after ``clean_human_text``. Each note is a
    visible remark about text that was removed."""
    notes = []
    # Whitespace in the title (newline, tab, U+2028) is one space; the other forbidden characters go.
    title = " ".join(title.split())
    title, cut_title = clean_human_text(title, keep_newlines=False)
    body, cut_body = clean_human_text(body, keep_newlines=True)
    for what, count in (("title", cut_title), ("body", cut_body)):
        if count:
            notes.append(f"removed {count} control or format character{'' if count == 1 else 's'} from the request {what}")
    line, goal = one_slice_text(title, body)
    return line, goal, notes


def one_slice_text(title: str, body: str) -> tuple[str, str]:
    """The slice title (one line, at most TITLE_MAX characters) and goal (the body, or the title when it is empty)."""
    full = " ".join(title.split())
    line = full if len(full) <= TITLE_MAX else full[: TITLE_MAX - 1].rstrip() + "…"
    return line, body.strip() or full


def fix_round(env: Mapping[str, str], issue: int, max_rounds: int = DEFAULT_MAX_ROUNDS, scored: bool = False) -> tuple[int, str, str]:
    """(round, slice id, scores) of a pivotal-fix dispatch, from the event file. Anything off refuses (F8): a round that is not a
    whole number from 1 to ``max_rounds`` (so a missing, float, bool, negative, zero or huge one), a payload for another issue,
    a ``head_sha`` that is no commit SHA, a ``run_id`` that is no positive whole number, and a ``branch`` that is not the
    slice's own draft branch ``pivotal/issue-<issue>/<slice>``. ``scores`` is the score history (engine/hill.py): for a score run
    one entry per round built so far, and "" otherwise; anything else is refused."""
    try:
        with open(env.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        raise JobError("the fix event file is missing or not JSON") from None
    payload = event.get("client_payload") if isinstance(event, dict) else None
    if not isinstance(payload, dict):
        raise JobError("a fix round needs a client_payload")
    if payload.get("issue") != issue or not _is_int(payload.get("issue")):
        raise JobError("the fix round names another issue")
    head = payload.get("head_sha")
    if not isinstance(head, str) or job.SHA.fullmatch(head) is None:
        raise JobError("the fix round has no head_sha")
    run_id = payload.get("run_id")
    if not _is_int(run_id) or run_id < 1:
        raise JobError("the fix round has no run_id")
    try:
        number = check_fix_round(payload.get("round"), max_rounds)
        slice_id = check_slice({"id": payload.get("slice"), "title": "x", "goal": "x"}).id
    except SliceError as err:
        raise JobError(f"fix round: {err}") from None
    if payload.get("branch") != f"pivotal/issue-{issue}/{slice_id}":
        raise JobError("the fix round's branch is not the slice's draft branch")
    scores = payload.get("scores")
    if not isinstance(scores, str):
        raise JobError("the fix round has no score history")
    if not scored:
        if scores:
            raise JobError("the fix round carries a score history, but the request has no score")
    else:
        try:
            entries = hill.decode_history(scores)
        except hill.HillError as err:
            raise JobError(f"the fix round's score history: {err}") from None
        if len(entries) != number:
            raise JobError("the fix round's score history does not have one entry per round built so far")
    return number, slice_id, scores


def check_branch_tip(env: Mapping[str, str], repo: str, issue: int, slice_id: str, head: str) -> None:
    """A fix round starts from the draft's CURRENT head: the branch must still be at ``head_sha``. An old dispatch replayed after
    the draft moved on (a later round published a new head) names a head the branch no longer has, and is refused."""
    answer = job.gh_json(env, "api", f"repos/{repo}/git/ref/heads/pivotal/issue-{issue}/{slice_id}")
    tip = answer.get("object", {}).get("sha") if isinstance(answer, dict) and isinstance(answer.get("object"), dict) else None
    if tip != head:
        raise JobError("the fix round's head_sha is not the draft branch's head now: an old dispatch, or the draft moved on")


def load_preset(env: Mapping[str, str], name: str) -> route.Preset:
    """The preset by name: the repo's PIVOTAL_PRESET_JSON when it has that name, else presets/<name>.json."""
    custom = env.get("PRESET_JSON", "")
    try:
        if custom:
            preset = route.parse_preset(json.loads(custom))
            if preset.name == name:
                return preset
        return route.load_preset(ROOT / "presets" / f"{name}.json")
    except (OSError, ValueError):
        raise JobError(f"preset {name} is not defined: no PIVOTAL_PRESET_JSON of that name, no presets/{name}.json") from None


def kaggle_needed(preset: route.Preset) -> bool:
    return any(entry.door == "kaggle" for entries in preset.tiers.values() for entry in entries)


def kaggle_model(preset: route.Preset) -> str:
    """The model models-up serves: the preset's ``kaggle.model``, else the one model every ``kaggle`` entry names.
    Empty when there is none or the entries disagree, and models-up then refuses to push (it never guesses)."""
    if preset.kaggle_model:
        return preset.kaggle_model
    models = {entry.model for entries in preset.tiers.values() for entry in entries if entry.door == "kaggle"}
    only = next(iter(models)) if len(models) == 1 else ""
    return only if route.KAGGLE_MODEL.fullmatch(only) else ""


def decide(env: Mapping[str, str]) -> None:
    """The plan job's first step. See the module docstring."""
    repo = job.read_repo(env)
    kind, trusted = job.read_kind(env)
    out_path = job.read_output_path(env)
    job.read_engine_sha(env)
    started = job.read_start(env.get("LEVEL", ""), env.get("CHECK", ""), kind)
    if started is None:
        job.write_outputs(out_path, {"matrix": "", "round": "0", "kaggle_needed": "false"})
        return
    issue = job.read_number(env.get("ISSUE", ""), "issue")
    title, body, labels = job.read_issue(env, repo, issue)
    dispatch = job.read_dispatch(env, kind)
    job.check_pin(kind, env.get("ISSUE_PIN", ""), title, body, dispatch)
    number, slice_id, base_ref, scores = 0, SINGLE_SLICE_ID, "", ""
    issue_pin = env.get("ISSUE_PIN", "") if kind in job.PIN_KINDS else ""  # checked by check_pin above; publish forwards it to a fix round
    # The hill-climb (engine/hill.py): a footer score is run only for a trusted author, and a line with its own score takes none.
    try:
        score_goal = hill.read_goal(body)
    except hill.HillError as err:
        raise JobError(f"score: {err}") from None
    if score_goal is not None and not trusted:
        raise JobError("score: a request with a score is refused in an untrusted run: the score command runs only for a trusted author")
    # The line comes from the labels, then the trust rule (engine/lines.py). A refusal is loud, before any write.
    custom, problems = lines.custom_lines(env.get("PRESET_JSON", ""))
    for problem in problems:
        print(f"note: {problem}", file=sys.stderr)
    try:
        choice = lines.choose(kind, trusted, labels, custom)
        line = custom.get(choice.name) or lines.load_line(choice.name)
        lines.check_wired(line)
    except lines.LineError as err:
        raise JobError(str(err)) from None
    for note in choice.notes:
        print(note, file=sys.stderr)
    if score_goal is not None and line.score is not None:
        raise JobError("score: a line with its own score takes no score in the request's footer")
    # For lines with their own score configuration, set up defaults
    if line.score is not None and score_goal is None:
        if line.score.scorer == "image":
            score_goal = hill.Goal(lines.IMAGE_SCORE_COMMAND, f"{line.score.target}")  # {scorer} is the engine checkout's image.py (buildjob.score_command)
        else:
            raise JobError(f"score: unsupported scorer {line.score.scorer}")
    if kind == "fix" and env.get("GITHUB_EVENT_NAME") == "repository_dispatch":
        number, slice_id, scores = fix_round(env, issue, hill.round_bound(score_goal is not None, DEFAULT_MAX_ROUNDS), scored=score_goal is not None)
        base_ref = dispatch["client_payload"]["head_sha"]  # fix_round has checked it
        issue_pin = dispatch["client_payload"]["issue_pin"]  # job.check_pin has compared it with the live issue
        check_branch_tip(env, repo, issue, slice_id, base_ref)
    preset = load_preset(env, lines.preset_for(line, "" if line.preset else job.read_preset_name(env.get("PRESET", ""))))
    # Cross-vendor reviewer configuration is required for land: configured; the review job decides availability per slice.
    # publish gets the fact, and a repo at `land` without it is held at `prepare` there, said here as well. Nothing here stops the run.
    reviewer = not route.cross_vendor_problems(preset)
    held = job.early(started.level, check=True, reviewer=reviewer)
    if held.held:
        print(f"note: {held.note()}", file=sys.stderr)
    text_title, goal, notes = human_slice_text(title, body)
    if line.request == "link":  # the issue's own words never reach the brief of this line
        try:
            text_title, goal = lines.link_request(body, repo)
        except lines.LineError as err:
            raise JobError(str(err)) from None
        notes = []
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    try:
        (single,) = request_slices(text_title, goal)
        chosen = check_slice({**asdict(single), "id": slice_id})
    except RequestTooLarge as err:
        # A refusal that names itself in the report: the word is a fixed vocabulary, and nothing else is written.
        job.write_outputs(out_path, {PLAN_WHY_OUTPUT: REQUEST_TOO_LARGE})
        raise JobError(f"the request is too large: {err}") from None
    except SliceError as err:
        raise JobError(f"the request is not a valid slice: {err}") from None
    outputs = {
        "matrix": matrix_json([chosen]),
        "round": str(number),
        "kaggle_needed": "true" if kaggle_needed(preset) else "false",
        "line": line.name,
        "reviewer": "true" if reviewer else "false",
        "level": started.level,
        "issue_pin": issue_pin,
        # The hill-climb: the command (encoded), its target and the history of the rounds built so far; "" with no score.
        "score": hill.encode_command(score_goal.command) if score_goal is not None else "",
        "target": score_goal.target if score_goal is not None else "",
        "scores": scores,
    }
    if kaggle_needed(preset):  # the model models-up serves; the line is absent, and so empty, when no Kaggle entry is routed
        outputs["kaggle_model"] = kaggle_model(preset)
    if base_ref:  # only a fix round has one: the others check out the default branch
        outputs["base_ref"] = base_ref
    job.write_outputs(out_path, outputs)


def baseline(env: Mapping[str, str]) -> None:
    """The plan job's second step: setup and check on the base, through run_gate. This step holds no token."""
    out_path = job.read_output_path(env)
    sha = env.get("BASE_SHA", "")
    if job.SHA.fullmatch(sha) is None:
        raise JobError("BASE_SHA is not a commit SHA")
    repo_dir, log = env.get("REPO_DIR", ""), env.get("LOG", "")
    if not repo_dir or not Path(repo_dir).is_dir() or not log:
        raise JobError("REPO_DIR and LOG are needed")
    if env.get("BASE_REF", ""):
        # A fix round's base is the previous round's draft head: model output. Its setup and check do not run on the plan runner
        # (R3), and a fix round has no use for the answer; the step still prints base_sha for the jobs after it.
        print("baseline: skipped, a fix round's base is a draft head", file=sys.stderr)
        job.write_outputs(out_path, {"base_sha": sha, "baseline": "none"})
        return
    command = ["bash", str(RUN_SH), "run_gate", str(BASELINE_SECONDS), log, env.get("SETUP", ""), env.get("CHECK", "")]
    done = subprocess.run(command, capture_output=True, text=True, check=False, cwd=repo_dir, env=dict(env))
    word = done.stdout.strip()
    if done.returncode != 0:
        raise JobError(f"run_gate failed with exit {done.returncode}")
    if word not in ("green", "red", "none"):
        word = "red"  # an answer that is no verdict is never green (F5)
    if word != "green":
        try:
            lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        # The log is the repo's own output: a line that starts "::" would be a workflow command to the runner.
        # Control characters go too, so no escape can hide a leading "::".
        clean = ["".join(c for c in line if c.isprintable()) for line in lines]
        tail = [line for line in clean if not line.lstrip().startswith("::")][-30:]
        print("\n".join(["baseline log (last lines):", *tail]), file=sys.stderr)
    if "LEVEL" in env:  # the level's requirement, said in the log (the decision itself: job.standing, in publish; assemble classifies the slices)
        held = job.early(job.read_level(env["LEVEL"]), check=job.check_present(env.get("CHECK", "")), baseline=word)
        if held.held:
            print(f"note: {held.note()}", file=sys.stderr)
    job.write_outputs(out_path, {"base_sha": sha, "baseline": word})


JOBS = {"decide": decide, "baseline": baseline}


def main(argv: Sequence[str], stdin, env: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m engine.plan")
    parser.parse_args(argv)
    try:
        slices = parse_slices(stdin.read())
    except SliceError as err:
        print(f"rejected: {err}", file=sys.stderr)
        return 1
    print(f"ok: {len(slices)} slice{'' if len(slices) == 1 else 's'}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1] in JOBS:
        sys.exit(job.run_job(JOBS[sys.argv[1]], os.environ))
    sys.exit(main(sys.argv[1:], sys.stdin, os.environ))
