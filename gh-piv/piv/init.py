"""piv init: set one repo up for Pivotal (F2b, F3b).

Input is checked, and the release, the caller file and the variables are read, before the first
write, so a refusal writes nothing. Then the config goes in (variables, secrets, labels, topic, the
Actions setting), and the caller file goes last: no run can start before its config exists. Each
step is idempotent, so a re-run finishes what a failed one left.

A secret's value comes from the environment, goes to gh on stdin, and never reaches an argument, an
output line or an error. A secret that is absent or blank is skipped and named. ``piv doctor``
(doctor.py) is where a missing one is reported as a problem; init does not run it.
"""

import base64
import difflib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .gh import Gh, GhFailed, PivError

# The engine repo's name. piv init's --engine can point at a fork; every reference to the engine is built from
# the name at call time, never from a literal (the layout names the engine repo you/pivotal).
ENGINE_REPO = "0xctx/pivotal"
LEGACY_ENGINE = "you/pivotal"
CALLER_PATH = "templates/caller.yml"
WORKFLOW_PATH = ".github/workflows/pivotal.yml"
# The caller template's engine placeholder (templates/caller.yml). piv init replaces it with the engine name.
ENGINE_PLACEHOLDER = "__PIVOTAL_ENGINE__"
# The template's pin (templates/caller.yml). piv init replaces every occurrence with the release SHA.
PIN_PLACEHOLDER = "0" * 40
TOPIC = "pivotal"
# Every variable name Pivotal uses, defined once: init's writer and VARIABLES both take them from here.
VAR_CHECK = "PIVOTAL_CHECK"
VAR_SETUP = "PIVOTAL_SETUP"
VAR_START = "PIVOTAL_START"
VAR_PRESET = "PIVOTAL_PRESET"
VAR_LEVEL = "PIVOTAL_LEVEL"
# The value of "Actions can open pull requests" that init replaced ("false"), so piv remove can put it back.
VAR_PRIOR_APPROVE = "PIVOTAL_PRIOR_CAN_APPROVE_PRS"
# The level a repo had before `piv stop --all` set it to watch, so `piv resume` can put it back (control.py). It exists only
# between a stop and a resume, and piv remove deletes it.
VAR_PRIOR_LEVEL = "PIVOTAL_PRIOR_LEVEL"
UNTRUSTED_PRESET = "PIVOTAL_UNTRUSTED_PRESET"  # read by the caller's factory-untrusted job; init does not set it yet
# Every variable Pivotal uses. init writes the first five and VAR_PRIOR_APPROVE; piv remove deletes any of them that are set.
VARIABLES = (
    VAR_CHECK,
    VAR_SETUP,
    VAR_START,
    VAR_PRESET,
    VAR_LEVEL,
    VAR_PRIOR_APPROVE,
    VAR_PRIOR_LEVEL,
    UNTRUSTED_PRESET,
    "PIVOTAL_PRESET_JSON",
    "PIVOTAL_RUNNER",
    "PIVOTAL_RUNNER_PUBLIC",
)
LEVELS = ("watch", "ask", "prepare", "land")
DEFAULT_LEVEL = "prepare"  # the default level
_BLANK_COMMENT = re.compile(r"(^|[ \t\n\v\f\r;&])#.*$", re.MULTILINE)
_BLANK_SEPARATORS = re.compile(r"[ \t\n\v\f\r;&:]")


def check_present(text: str) -> bool:
    """Whether PIVOTAL_CHECK runs a command: engine/job.py ``check_present``, which is engine/run.sh's ``_blank_check`` negated.
    piv cannot import the engine, so this is a copy and test/test_levels.py holds it to both."""
    return bool(_BLANK_SEPARATORS.sub("", _BLANK_COMMENT.sub("", text)))
CHANGES_SHOWN = 10  # lines of a differing caller that a refusal names

# The Kaggle endpoint's two secrets. KAGGLE_KEY here is the endpoint's key, not the account's KAGGLE_KEY below.
KAGGLE_URL = "PIVOTAL_KAGGLE_URL"
KAGGLE_KEY = "PIVOTAL_KAGGLE_KEY"
RUNNER_VARIABLE = "PIVOTAL_RUNNER"  # a repo variable: "self-hosted" runs the factory on your own machine

# Every secret Pivotal uses, in the caller's order. The test binds this to the caller.
SECRETS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    KAGGLE_URL,
    KAGGLE_KEY,
    "KAGGLE_USERNAME",
    "KAGGLE_KEY",
)

# The labels init writes: name, colour (hex, no '#'), description.
LABELS = (
    ("pivotal", "5319e7", "A labelled issue starts a Pivotal run"),
    ("pivotal:building", "fbca04", "A Pivotal run is building this issue"),
    ("pivotal:ready", "0e8a16", "A Pivotal change is ready for you to land"),
    ("pivotal:landed", "1d76db", "A Pivotal change has landed"),
    ("pivotal:blocked", "b60205", "A Pivotal run is blocked and needs you"),
    # The trigger labels for the build lines. engine/trust.py reads these as triggers.
    ("pivotal:build", "0075ca", "A Pivotal run on the build line"),
    ("pivotal:incident", "d93f0b", "A Pivotal run on the incident line"),
    ("pivotal:mirror", "c2e0c6", "A Pivotal run on the mirror line, toward an attached mockup"),
    ("pivotal:docs", "bfdadc", "A Pivotal run on the docs line"),
)
# The build lines, each with its trigger label in LABELS. piv do takes its --line from here.
LINES = ("build", "incident", "mirror", "docs")

OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
PRESET = re.compile(r"[a-z][a-z0-9-]{0,39}")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
SHA = re.compile(r"[0-9a-f]{40}")
def pivotal_reference(engine: str) -> re.Pattern[str]:
    """Every mention of the engine repo, built from the name at call time (not a module literal)."""
    return re.compile(rf"(?<![\w.-]){re.escape(engine)}(?![\w.-])")


def pinned_reference(engine: str) -> re.Pattern[str]:
    """A pin of the engine: a ``uses:`` path of it at its ref, or its checkout's ``ref:`` after ``repository:``."""
    return re.compile(
        rf"(?<![\w.-]){re.escape(engine)}/[^\s@\"']+@([^\s\"']+)"
        rf"|repository:\s*{re.escape(engine)}\s+ref:\s*\"([^\"]*)\""
    )


# Every engine-shaped pin in a caller, whatever the repo. init's own check uses it to fail closed on a leftover
# literal or a second engine; the other readers derive the engine name from the same shapes.
ANY_USES = re.compile(
    r"^\s*(?:-\s+)?uses:\s*[\"']?([^/\s@\"']+/[^/\s@\"']+)/[^\s@\"']+@([^\s\"']+)", re.MULTILINE
)
ANY_CHECKOUT = re.compile(r"repository:\s*([^/\s\"']+/[^/\s\"']+)\s+ref:\s*[\"']([^\"]*)[\"']")
# The caller's engine_repo input (templates/caller.yml): the engine name, not a SHA. It is a mention that is pinned
# to the engine, so init counts it with the uses/checkout pins rather than calling it unpinned.
ENGINE_REPO_LINE = re.compile(r'^\s*engine_repo:\s*[\"\']?([^\"\'\s#]*)[\"\']?\s*$', re.MULTILINE)
# The engine_sha input the caller passes factory.yml: the same release SHA as the `uses:` pins.
ENGINE_SHA_LINE = re.compile(r"^\s*engine_sha:\s*\"?([^\s\"#]*)\"?\s*$", re.MULTILINE)


def valid_repo(repo: str) -> bool:
    """True for OWNER/NAME in GitHub's shape. Only the shape is checked; a repo that does not exist fails at gh."""
    owner, _, name = repo.partition("/")
    return bool(OWNER.fullmatch(owner) and NAME.fullmatch(name) and name not in (".", ".."))


@dataclass(frozen=True)
class Request:
    repo: str
    check: str
    setup: str | None
    start: str | None
    preset: str | None
    level: str | None  # None: keep the repo's level, or the default when it has none
    release: str | None
    update: bool  # replace a caller file that differs from the one this release writes
    engine: str = ENGINE_REPO  # the engine repo the caller pins (a fork, or the default)


@dataclass(frozen=True)
class VariableWrite:
    name: str
    value: str
    was: str | None  # None when the variable is unset


@dataclass(frozen=True)
class CallerWrite:
    text: str
    sha: str
    blob: str | None  # the file's blob sha, when it exists and must be replaced
    state: str  # "created", "updated" or "current"


def gh_environment(env: Mapping[str, str]) -> dict[str, str]:
    """The environment gh runs in: every Pivotal secret removed, so only ``secret set`` ever sees one."""
    return {key: value for key, value in env.items() if key not in SECRETS}


DEFAULT_PRESET = "one-key"


def request(
    repo: str,
    *,
    check: str | None,
    setup: str | None = None,
    start: str | None = None,
    preset: str | None = None,
    level: str | None = None,
    release: str | None = None,
    update: bool = False,
    engine: str | None = None,
) -> Request:
    """The checked request. Every refusal is a usage error (exit 2), and none of them calls gh."""
    checked_repo(repo)
    if engine is None:
        engine = ENGINE_REPO
    if not valid_repo(engine):
        raise PivError("--engine must be owner/name, for example 0xctx/pivotal", 2)
    checked_repo(repo)
    if check is None or not check_present(check):
        raise PivError("--check is required: the command that gates each change, for example --check 'npm test'", 2)
    for flag, value in (("--check", check), ("--setup", setup), ("--start", start)):
        if value is not None:
            one_line(flag, value)
    if level is not None and level not in LEVELS:
        raise PivError(f"--level must be one of {', '.join(LEVELS)}", 2)
    if preset is not None and not PRESET.fullmatch(preset):
        raise PivError("--preset must be a lowercase name such as claude-sub", 2)
    if release is not None and (not REF.fullmatch(release) or ".." in release):
        raise PivError(f"--release must be a tag or a commit SHA of {engine}", 2)
    if preset is None:
        preset = DEFAULT_PRESET
    return Request(repo, check, setup, start, preset, level, release, update, engine)


def checked_repo(repo: str) -> str:
    """REPO as owner/name, or a usage error (exit 2). Shared by every piv command that names a repo."""
    if not valid_repo(repo):
        raise PivError("REPO must be owner/name, for example 0xctx/piv-sandbox", 2)
    return repo


def one_line(flag: str, value: str) -> None:
    """A value that must be one non-empty line of UTF-8 text (a command, or a variable's value)."""
    if not value.strip() or _has_control(value):
        raise PivError(f"{flag} must be one non-empty line", 2)
    if not _is_text(value):
        raise PivError(f"{flag} must be UTF-8 text", 2)


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _is_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def resolve_release(gh: Gh, release: str | None, engine: str = ENGINE_REPO) -> tuple[str, str]:
    """The release's tag or ref, and the full commit SHA it points to.

    With no ``release``, this is the latest release of the engine repo. A repo with no release yet
    says so, and asks for one to be named.
    """
    if release is None:
        try:
            ref = gh.api_json(f"repos/{engine}/releases/latest").get("tag_name")
        except GhFailed as err:
            if err.status != 404:
                raise
            raise PivError(f"{engine} has no release yet; pass --release REF to pin one") from None
        if not isinstance(ref, str) or not REF.fullmatch(ref):
            raise PivError(f"the latest release of {engine} has no usable tag name")
    else:
        ref = release
    sha = gh.api_json(f"repos/{engine}/commits/{ref}").get("sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise PivError(f"--release {ref} is not a commit of {engine}")
    return ref, sha


def render_caller(template: str, sha: str, engine: str = ENGINE_REPO) -> str:
    """The caller with its engine name and pin replaced. A template without the placeholder is refused (fail closed)."""
    if PIN_PLACEHOLDER not in template:
        raise PivError(f"{CALLER_PATH} at {sha[:12]} has no pin placeholder; refusing to write an unpinned caller")
    rendered = template.replace(ENGINE_PLACEHOLDER, engine).replace(PIN_PLACEHOLDER, sha)
    _check_pins(rendered, sha, engine)
    return rendered


def _check_pins(text: str, sha: str, engine: str) -> None:
    """Every mention of the engine must be a pin to ``sha``: a ``uses:`` path or the engine checkout's ref.

    A leftover placeholder, a leftover old literal, or a mention that is not one of those pins cannot be checked,
    so it counts as unpinned and the caller is refused. A second engine is refused too: the caller must pin one.
    """
    if ENGINE_PLACEHOLDER in text:
        raise PivError(f"{CALLER_PATH} still has the {ENGINE_PLACEHOLDER} placeholder; refusing to write it")
    if engine != LEGACY_ENGINE and re.search(rf"(?<![\w.-]){re.escape(LEGACY_ENGINE)}(?![\w.-])", text):
        raise PivError(f"{CALLER_PATH} has a leftover {LEGACY_ENGINE} reference; refusing to write it")
    repo_pins = ENGINE_REPO_LINE.findall(text)
    if any(value != engine for value in repo_pins):
        raise PivError(f"{CALLER_PATH} has an engine_repo that is not {engine}; refusing to write it")
    mentions = len(pivotal_reference(engine).findall(text))
    uses = ANY_USES.findall(text) + ANY_CHECKOUT.findall(text)
    engines = {name for name, _ in uses}
    shas = [sha for _, sha in uses]
    engine_shas = ENGINE_SHA_LINE.findall(text)
    if (
        engines != {engine}
        or len(uses) + len(repo_pins) != mentions
        or any(pin != sha for pin in shas + engine_shas)
    ):
        raise PivError(
            f"{CALLER_PATH} has a reference to {engine} that is not pinned to {sha[:12]}; refusing to write it"
        )


def init(req: Request, gh: Gh, env: Mapping[str, str], say: Callable[[str], None]) -> None:
    ref, sha = resolve_release(gh, req.release, req.engine)
    template = gh.api_text(f"repos/{req.engine}/contents/{CALLER_PATH}?ref={sha}")
    caller = _plan_caller(req.repo, gh, render_caller(template, sha, req.engine), sha, req.update)
    variables = _plan_variables(req, gh)
    say(f"piv init {req.repo}: release {ref} (commit {sha[:12]}), check {req.check!r}")
    _set_variables(req.repo, gh, variables, say)
    # init has no value for the untrusted preset, so it names the gap rather than inventing one.
    say(
        f"variable {UNTRUSTED_PRESET}: not set by piv init, which has no value for it yet; "
        "untrusted issues need it set by hand"
    )
    _set_secrets(req.repo, gh, env, say)
    _set_labels(req.repo, gh, say)
    _add_topic(req.repo, gh, say)
    _allow_actions_prs(req.repo, gh, say)
    _write_caller(req.repo, gh, caller, say)
    say(f"doctor: not run by init; check the repo with gh piv doctor {req.repo}")
    say("baseline run: not run by init")


def variable_value(gh: Gh, repo: str, name: str) -> str | None:
    """The variable's value, or None when it is unset (a 404). Any other failure is raised."""
    path = f"repos/{repo}/actions/variables/{name}"
    try:
        value = gh.api_json(path).get("value")
    except GhFailed as err:
        if err.status == 404:
            return None
        raise
    if not isinstance(value, str):
        raise PivError(f"gh api {path} gave no value")
    return value


def _plan_variables(req: Request, gh: Gh) -> list[VariableWrite]:
    """Each variable to set, with its value now. ``PIVOTAL_LEVEL`` keeps the repo's level unless --level says so."""
    wanted = [
        (VAR_CHECK, req.check),
        (VAR_SETUP, req.setup),
        (VAR_START, req.start),
        (VAR_PRESET, req.preset),
    ]
    writes = [
        VariableWrite(name, value, variable_value(gh, req.repo, name)) for name, value in wanted if value is not None
    ]
    level_now = variable_value(gh, req.repo, VAR_LEVEL)
    if req.level is not None:
        level = req.level
    elif level_now is not None:
        level = level_now
    else:
        level = DEFAULT_LEVEL
    writes.append(VariableWrite(VAR_LEVEL, level, level_now))
    return writes


def _set_variables(repo: str, gh: Gh, writes: list[VariableWrite], say: Callable[[str], None]) -> None:
    for write in writes:
        if write.was == write.value:
            say(f"variable {write.name}: unchanged ({write.value})")
            continue
        gh.run("variable", "set", write.name, "--repo", repo, stdin=write.value)
        say(f"variable {write.name}: was {write.was or 'unset'}, now {write.value}")


def _set_secrets(repo: str, gh: Gh, env: Mapping[str, str], say: Callable[[str], None]) -> None:
    for name in SECRETS:
        value = env.get(name, "")
        if not value.strip():
            say(f"secret {name}: not in the environment, skipped")
            continue
        # echo_stderr is off: a failed write must not show gh's message, in case it repeats the value.
        gh.run("secret", "set", name, "--repo", repo, stdin=value, echo_stderr=False)
        say(f"secret {name}: set")


def _set_labels(repo: str, gh: Gh, say: Callable[[str], None]) -> None:
    for label, colour, text in LABELS:
        gh.run("label", "create", label, "--repo", repo, "--color", colour, "--description", text, "--force")
        say(f"label {label}: created or updated")


def _add_topic(repo: str, gh: Gh, say: Callable[[str], None]) -> None:
    gh.run("repo", "edit", repo, "--add-topic", TOPIC)
    say(f"topic {TOPIC}: added")


def _allow_actions_prs(repo: str, gh: Gh, say: Callable[[str], None]) -> None:
    """Read, change if needed, then read back and fail if the setting did not stick (read-modify-verify).

    The value replaced (off) is written to the repo variable VAR_PRIOR_APPROVE before the change, over any value already
    there, so piv remove can restore it. A re-run finds the setting on and writes nothing. A setting that was already on records nothing: there is nothing to restore.
    """
    path = f"repos/{repo}/actions/permissions/workflow"
    before = gh.api_json(path)
    if before.get("can_approve_pull_request_reviews") is True:
        say("Actions can open pull requests: already allowed")
    else:
        # Always "false", even over a value already there: the setting is not on, so off is what remove must restore, and a
        # variable someone planted (true, garbage) must not decide it.
        gh.run("variable", "set", VAR_PRIOR_APPROVE, "--repo", repo, stdin="false")
        body = {"can_approve_pull_request_reviews": True}
        if "default_workflow_permissions" in before:
            body["default_workflow_permissions"] = before["default_workflow_permissions"]
        gh.api(path, method="PUT", body=body)
        say("Actions can open pull requests: set")
    after = gh.api_json(path).get("can_approve_pull_request_reviews")
    if after is not True:
        raise PivError(
            f"Actions could not be allowed to open pull requests in {repo}: the setting did not stick "
            f"(it reads back as {after!r}). Check the repo's and the organization's Actions settings."
        )


def _plan_caller(repo: str, gh: Gh, text: str, sha: str, update: bool) -> CallerWrite:
    """What to do with the caller file, decided before any write. A differing file needs ``--update``."""
    path = f"repos/{repo}/contents/{WORKFLOW_PATH}"
    try:
        current = gh.api_json(path)
    except GhFailed as err:
        if err.status != 404:
            raise
        return CallerWrite(text, sha, None, "created")
    existing = decoded_text(current, path)
    if existing == text:
        return CallerWrite(text, sha, None, "current")
    blob = current.get("sha")
    if not isinstance(blob, str):
        raise PivError(f"gh api {path} gave no file sha to update")
    if not update:
        raise PivError(
            f"{WORKFLOW_PATH} in {repo} differs from the caller this release writes; "
            f"nothing was written. Run again with --update to replace it:\n{_changes(existing, text)}"
        )
    return CallerWrite(text, sha, blob, "updated")


def _write_caller(repo: str, gh: Gh, write: CallerWrite, say: Callable[[str], None]) -> None:
    short = write.sha[:12]
    if write.state == "current":
        say(f"caller {WORKFLOW_PATH}: already current")
        return
    if write.state == "created":
        message = f"Add the Pivotal caller at {short}"
    else:
        message = f"Update the Pivotal caller to {short}"
    body: dict[str, str] = {"message": message, "content": _b64(write.text)}
    if write.blob is not None:
        body["sha"] = write.blob
    try:
        gh.api(f"repos/{repo}/contents/{WORKFLOW_PATH}", method="PUT", body=body)
    except GhFailed as err:
        if err.status not in (403, 409):
            raise
        # A protected default branch refuses a direct commit with one of these. The config is already in.
        raise PivError(
            f"HTTP {err.status} writing {WORKFLOW_PATH} to {repo}: the default branch is probably protected "
            f"(or the token cannot write it), so the caller needs a pull request. Secrets and variables are "
            f"already set. Open a pull request that adds {WORKFLOW_PATH} pinned to {short}; piv init does not "
            f"open one yet."
        ) from err
    say(f"caller {WORKFLOW_PATH}: committed ({write.state}), pinned to {short}")


def _changes(existing: str, text: str) -> str:
    """The first lines that differ, as a diff. The two file headers are dropped; they name nothing."""
    diff = list(difflib.unified_diff(existing.splitlines(), text.splitlines(), lineterm="", n=0))[2:]
    shown = [f"  {line}" for line in diff[:CHANGES_SHOWN]]
    if len(diff) > CHANGES_SHOWN:
        shown.append(f"  ... and {len(diff) - CHANGES_SHOWN} more")
    return "\n".join(shown)


def decoded_text(current: dict, path: str) -> str:
    """The text of a contents API answer. Shared by piv init and piv event, which reads the same caller file."""
    content = current.get("content", "")
    if not isinstance(content, str):
        raise PivError(f"gh api {path} gave no file content")
    try:
        return base64.b64decode(content).decode("utf-8")
    except ValueError:
        raise PivError(f"{WORKFLOW_PATH} is not UTF-8 text, so it cannot be compared; it was not written") from None


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
