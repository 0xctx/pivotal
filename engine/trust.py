"""The permission lookup, failing closed (F2b, the trust job).

``lookup_permission`` asks gh for the actor's permission on the repo. Only admin, maintain and
write are trusted. A failed call, a timeout and any other answer are untrusted, so the caller
passes no subscription secret unless this module says so.

A labelled issue is decided by its author. A comment is decided by its commenter, who must be a
writer. A ``stop`` on an issue is trusted on that alone, so a writer can stop any issue's runs. A ``land`` or a ``stop`` is
also a command a pull request comment can be (no other is): its ``issue`` output is the issue the pull request closes, read
from the pull request itself (``closed_issue``: one ``Closes #n`` line, exactly once), so the land job, its concurrency group
and the stop's runs are the issue's. A pull request that closes no issue, closes two, or cannot be read is refused, and the
refusal is silent: the reason is only in this step's stderr. Any other
command is then decided by the issue's author, unless the first line is ``/pivotal go trusted``
(F2b).

``resolve`` maps the event to an issue, the actor whose permission decides, and a kind. ``main``
prints the job outputs: trusted, issue, kind and pin (``issue_pin``, or ``fix_pin`` for a dispatch). An empty pin means
the factory refuses.

An incident (T5) is a ``workflow_run`` of this repository's CI that failed on a push to its default branch: the same
repository, the default branch and ``conclusion == failure`` (a fork PR named after the branch triggers nothing). Its actor is
the one who pushed, and that actor's permission decides, as for a comment. An event is a ``pivotal-event`` dispatch (``piv event``,
a webhook): a writer's sender decides, and the repo's own token does not (I3: nothing sends an event with it, so it is no
proof of a writer). Neither has an issue yet: the factory's ack job files it (ack.py), so
both print an empty ``issue`` and an empty pin, and nothing here reads the alert's text.

A ``pivotal-fix`` ``repository_dispatch`` is a fix round (T5). GitHub lets only an account with write access to THIS repo, or
this repo's own ``GITHUB_TOKEN``, create the event, and it runs the default branch's workflow in this repo, so a fork or a
stranger cannot send one. Trust still decides on the sender: ``github-actions[bot]`` of type Bot is the repo's own token;
any other sender must be a login whose permission is admin, maintain or write (the same lookup as a comment). The event must
also name this repository and carry a complete, well-formed payload (``fix_pin`` is not empty and ``fix_payload_problem``
finds nothing), or it is not a trigger. The round bound (``round > max_rounds``) is plan's refusal, which runs before any
secret is used. publish sends the event only for a trusted run, so a stranger's issue cannot start a trusted fix.

Nothing reads the environment except ``__main__``, and ``main`` takes it as an argument, so tests
pass their own PATH.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TextIO

TRUSTED = frozenset({"admin", "maintain", "write"})
# Levels a log line may name. Any other answer is logged as "unknown" (the answer is untrusted text).
KNOWN_PERMISSIONS = frozenset({"admin", "maintain", "write", "triage", "read", "none"})
# Every kind a trigger can resolve to. event and incident have no issue yet: ack files it (T5).
KINDS = frozenset({"go", "fix", "land", "stop", "event", "incident", "none"})
NO_ISSUE_KINDS = frozenset({"event", "incident"})
# The repository_dispatch action of an event, as piv event sends it (templates/caller.yml listens for it).
EVENT_TYPE = "pivotal-event"
# The workflow_run of a CI failure: the one event an incident comes from (T5).
INCIDENT_EVENT = "workflow_run"
TIMEOUT_SECONDS = 20

# A comment's command is the first word group of its body. A PR comment is not resolved here.
COMMAND = re.compile(r"/pivotal[ \t]+(go|fix|land|stop)(?![\w-])")
# The bare label, or pivotal:<line> for a build line. Status labels are not triggers.
PIVOTAL_LABEL = re.compile(r"pivotal(?::(?:build|incident|mirror|docs))?")
# The first line of a comment that lets a writer re-run someone else's issue as trusted (F2b).
EXPLICIT_TRUST = "/pivotal go trusted"
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
# The sender of a dispatch made with the workflow's own GITHUB_TOKEN (the publish job's fix rounds).
OWN_TOKEN_SENDER = ("github-actions[bot]", "Bot")
FIX_EVENT = "pivotal-fix"
BRANCH_OF = re.compile(r"pivotal/issue-([1-9][0-9]{0,9})/([a-z0-9][a-z0-9-]{0,39})")
SHA40 = re.compile(r"[0-9a-f]{40}")
SHA64 = re.compile(r"[0-9a-f]{64}")
REPO_PART = re.compile(r"[A-Za-z0-9_.-]+")
# The line that says which issue a pull request closes. Exactly one such line names the issue; land.py uses the same rule.
CLOSES = re.compile(r"^Closes #([1-9][0-9]{0,9})[ \t]*\r?$", re.MULTILINE)


@dataclass(frozen=True)
class Decision:
    trusted: bool
    issue: int | None
    kind: str  # one of KINDS
    reason: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}")


def _valid_repo(repo: object) -> bool:
    if not isinstance(repo, str):
        return False
    owner, slash, name = repo.partition("/")
    return bool(slash) and all(
        REPO_PART.fullmatch(part) is not None and part not in (".", "..") for part in (owner, name)
    )


def _valid_login(actor: object) -> bool:
    return isinstance(actor, str) and LOGIN.fullmatch(actor) is not None


def _api(path: str, *, env: Mapping[str, str] | None, timeout: float) -> object:
    """``gh api PATH`` parsed as JSON, or None for any failure: gh missing, a timeout, an error exit, or text that is not JSON."""
    try:
        done = subprocess.run(
            ["gh", "api", path],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=None if env is None else dict(env),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None  # gh is missing, timed out, or printed bytes that are not text
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def lookup_permission(
    repo: str, actor: str, *, env: Mapping[str, str] | None = None, timeout: float = TIMEOUT_SECONDS
) -> str | None:
    """The actor's permission on the repo, or None for any error, timeout or odd answer (F2b).

    The answer is the ``permission`` field, the legacy one (admin, write, read, none; GitHub also
    reports maintain and triage). ``role_name`` is not read, so a custom role name is never trusted.
    """
    if not _valid_repo(repo) or not _valid_login(actor):
        return None
    answer = _api(f"repos/{repo}/collaborators/{actor}/permission", env=env, timeout=timeout)
    permission = answer.get("permission") if isinstance(answer, dict) else None
    return permission if isinstance(permission, str) else None


def closed_issue(repo: str, number: int, *, env: Mapping[str, str] | None = None, timeout: float = TIMEOUT_SECONDS) -> int | None:
    """The one issue pull request ``number`` closes: its body has exactly one ``Closes #n`` line. None when there is no such
    line, when there are two, or when the pull request cannot be read (``pull-requests: read``, GET repos/{repo}/pulls/{n})."""
    if not _valid_repo(repo) or not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    pull = _api(f"repos/{repo}/pulls/{number}", env=env, timeout=timeout)
    body = pull.get("body") if isinstance(pull, dict) else None
    if body is None:
        body = ""
    if not isinstance(body, str):
        return None
    closes = CLOSES.findall(body)
    return int(closes[0]) if len(closes) == 1 else None


def is_trusted(permission: str | None) -> bool:
    return permission in TRUSTED


def _logged(permission: str) -> str:
    """The permission as a log line may name it: a known level, or "unknown" (the answer is text)."""
    return permission if permission in KNOWN_PERMISSIONS else "unknown"


def _dig(data: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _number(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def workflow_run_problem(payload: Mapping, repo: str) -> str:
    """"" when the workflow run is a failed push to this repository's default branch (an incident), else the rule it breaks.

    A fork PR whose branch is called main is a ``pull_request`` run from another repository, so it breaks the first rules that
    ask for a push and this repository. Trust checks it again here, not only in the caller's ``if:``."""
    if not _valid_repo(repo):
        return "invalid repository"
    run = _dig(payload, "workflow_run")
    if not isinstance(run, Mapping):
        return "the event has no workflow run"
    if _dig(payload, "action") != "completed":
        return "the workflow run is not completed"
    if _dig(payload, "repository", "full_name") != repo:
        return "the event names another repository"
    if _dig(run, "event") != "push":
        return "the workflow run is not a push"
    if _dig(run, "head_repository", "full_name") != repo:
        return "the workflow run is from another repository"
    default = _dig(payload, "repository", "default_branch")
    if not isinstance(default, str) or not default or _dig(run, "head_branch") != default:
        return "the workflow run is not on the default branch"
    if _dig(run, "conclusion") != "failure":
        return "the workflow run did not fail"
    return ""


def dispatch_problem(payload: Mapping, repo: str) -> str:
    """"" when a pivotal-event dispatch names this repository and carries an object of fields, else the rule it breaks."""
    if not _valid_repo(repo):
        return "invalid repository"
    if _dig(payload, "repository", "full_name") != repo:
        return "the dispatch names another repository"
    if not isinstance(_dig(payload, "client_payload"), Mapping):
        return "the dispatch has no client_payload object"
    return ""


def _trigger(event_name: str, payload: Mapping, repo: str = "") -> tuple[int | None, object, str]:
    """(issue, actor, kind) before the actor is checked. kind is "none" when this is no trigger."""
    nothing = (None, None, "none")
    issue = _number(_dig(payload, "issue", "number"))
    action = _dig(payload, "action")
    if event_name == "issues" and action == "labeled":
        label = _dig(payload, "label", "name")
        if not (isinstance(label, str) and PIVOTAL_LABEL.fullmatch(label)):
            return nothing
        return issue, _dig(payload, "issue", "user", "login"), "go"
    if event_name == "issue_comment" and action == "created":
        body = _dig(payload, "comment", "body")
        match = COMMAND.match(body.lstrip()) if isinstance(body, str) else None
        if match is None:
            return nothing
        if _dig(payload, "issue", "pull_request") is not None and match.group(1) != "land" and match.group(1) != "stop":
            return nothing  # on a pull request only land and stop are commands; decide maps the pull request to its issue
        return issue, _dig(payload, "comment", "user", "login"), match.group(1)
    if event_name == "repository_dispatch" and action == FIX_EVENT:
        fields = _dig(payload, "client_payload")
        issue = _number(_dig(fields, "issue")) if isinstance(fields, Mapping) else None
        if fix_payload_problem(payload):
            return nothing
        return issue, _dig(payload, "sender", "login"), "fix"
    if event_name == "repository_dispatch" and action == EVENT_TYPE and not dispatch_problem(payload, repo):
        return None, _dig(payload, "sender", "login"), "event"
    if event_name == INCIDENT_EVENT and not workflow_run_problem(payload, repo):
        return None, _dig(payload, "workflow_run", "actor", "login"), "incident"
    return nothing


def fix_payload_problem(payload: Mapping) -> str:
    """"" when the dispatch event is a well-formed fix round for this repository's own issue branch, else the rule it breaks.
    ``round`` is only checked to be a positive integer: the bound is plan's."""
    fields = _dig(payload, "client_payload")
    if not isinstance(fields, Mapping) or not fix_pin(payload):
        return "the fix payload is missing a field or has one of the wrong type"
    if set(fields) != set(PAYLOAD_KEYS):
        return "the fix payload has a key that is not one of ours, or lacks one"
    found = BRANCH_OF.fullmatch(fields["branch"])
    if found is None or int(found[1]) != fields["issue"] or found[2] != fields["slice"]:
        return "the branch is not the draft branch of the issue's slice"
    if SHA40.fullmatch(fields["head_sha"]) is None:
        return "head_sha is not a commit SHA"
    if SHA64.fullmatch(fields["issue_pin"]) is None:
        return "issue_pin is not a sha256 digest"
    if not isinstance(fields.get("scores"), str):  # present and text: a payload without it is not one this engine sent
        return "scores is missing or is not text"
    if not isinstance(fields.get("findings"), str):  # present and text: a payload without it is not one this engine sent
        return "findings is missing or is not text"
    return ""


def _is_command_on_pull_request(kind: str, payload: object) -> bool:
    """A "/pivotal land" or "/pivotal stop" commented on a pull request: its number is not an issue's, so only ``decide``
    resolves it."""
    if kind == "land" and _dig(payload, "issue", "pull_request") is not None:
        return True
    if kind == "stop" and _dig(payload, "issue", "pull_request") is not None:
        return True
    return False


def resolve(event_name: str, payload: Mapping, repo: str = "") -> tuple[int | None, str | None, str]:
    """(number, actor, kind) for the event, from the event alone. Anything not resolvable is (None, None, "none"), and so is
    a land or a stop on a pull request, whose number is not an issue's: only ``decide`` resolves the issue it closes. An
    incident or an event has no issue yet, so its number is None; ``repo`` is the repository the event must name."""
    issue, actor, kind = _trigger(event_name, payload, repo)
    if kind == "none" or (issue is None and kind not in NO_ISSUE_KINDS):
        return None, None, "none"
    if _is_command_on_pull_request(kind, payload):
        return None, None, "none"
    if event_name == "repository_dispatch" and _dig(payload, "sender", "login") == OWN_TOKEN_SENDER[0]:
        return issue, OWN_TOKEN_SENDER[0], kind  # the bot's name is no login; _dispatch decides on it
    if not _valid_login(actor):
        return None, None, "none"
    return issue, actor, kind


def _first_line(payload: Mapping) -> str:
    body = _dig(payload, "comment", "body")
    return "" if not isinstance(body, str) else body.lstrip().split("\n", 1)[0].rstrip()


def _refused(reason: str) -> Decision:
    return Decision(False, None, "none", reason)


def _verdict(issue: int, kind: str, login: str, permission: str | None) -> Decision:
    """Trusted when the login's permission is trusted; otherwise untrusted with the level logged."""
    if is_trusted(permission):
        return Decision(True, issue, kind, f"{login} has {permission} access")
    if permission is None:
        return Decision(False, issue, kind, f"permission lookup for {login} failed or was unknown")
    return Decision(False, issue, kind, f"{login} has {_logged(permission)} access, not write")


def decide(
    event_name: str,
    payload: Mapping,
    repo: str,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> Decision:
    """Trust for the trigger. A comment from an untrusted commenter is ignored (kind none)."""
    issue, actor, kind = _trigger(event_name, payload, repo)
    if kind == "none":
        return _refused(_no_trigger_reason(event_name, payload, repo))
    if not _valid_repo(repo):
        return _refused("invalid repository")
    if kind in NO_ISSUE_KINDS:
        return _no_issue(event_name, payload, repo, kind, actor, env, timeout)
    if event_name == "repository_dispatch" and issue is not None:
        return _dispatch(payload, repo, issue, "fix", env, timeout)
    if not _valid_login(actor):
        return _refused("invalid login")
    if issue is None:
        return _refused("invalid issue number")
    permission = lookup_permission(repo, actor, env=env, timeout=timeout)
    if event_name != "issue_comment":
        return _verdict(issue, kind, actor, permission)
    if not is_trusted(permission):
        return _refused(f"ignored: commenter {actor} is not trusted")
    if _is_command_on_pull_request(kind, payload):
        # A land or a stop on a pull request: its author is the bot that opened it, so the commenter alone decides. What lands
        # is decided by the seal the land job verifies (F4), never by who asked. The decision is the issue the pull request
        # closes: the land job's group and the stop's runs are that issue's, and a pull request that closes none, or two, is
        # refused (fail closed).
        closed = closed_issue(repo, issue, env=env, timeout=timeout)
        if closed is None:
            return _refused(f"pull request #{issue} must close exactly one issue: Closes #<issue> on one line, exactly once "
                            "(or it cannot be read)")
        if kind == "stop":
            return Decision(True, closed, kind, f"{actor} has {permission} access and stops the runs of the pull request #{issue}, which closes #{closed}")
        return Decision(True, closed, kind, f"{actor} has {permission} access and lands the pull request #{issue}, which closes #{closed}")
    if kind == "stop":
        # Stopping needs no trusted author: a writer's stop is trusted on the commenter alone (R4).
        return Decision(True, issue, kind, f"{actor} has {permission} access and stops the issue's runs")
    if _first_line(payload) == EXPLICIT_TRUST:
        return Decision(True, issue, kind, f"{actor} re-ran the issue with {EXPLICIT_TRUST}")
    author = _dig(payload, "issue", "user", "login")
    if _valid_login(author):
        author_permission = lookup_permission(repo, author, env=env, timeout=timeout)
        verdict = _verdict(issue, kind, author, author_permission)
    else:
        verdict = Decision(False, issue, kind, "invalid login for the issue author")
    if verdict.trusted:
        return verdict
    if kind == "land":
        # EXPLICIT_TRUST can never be the first line of a land comment (the command that matches first is go), so offering it
        # here would be a dead end. The same stack lands through its pull request: "/pivotal land" commented there.
        return Decision(False, issue, kind, f"{verdict.reason}; the stack lands through a /pivotal land comment on its pull request")
    rerun = f"a writer can re-run it with {EXPLICIT_TRUST}"
    return Decision(False, issue, kind, f"{verdict.reason}; {rerun}")


def _no_trigger_reason(event_name: str, payload: Mapping, repo: str) -> str:
    """Why a trigger is no trigger, for the log: the first rule an incident or an event breaks, else the old phrase."""
    if event_name == INCIDENT_EVENT:
        return f"not an incident: {workflow_run_problem(payload, repo)}"
    if event_name == "repository_dispatch" and _dig(payload, "action") == EVENT_TYPE:
        return f"not an event: {dispatch_problem(payload, repo)}"
    return "not a pivotal trigger"


def _no_issue(event_name: str, payload: Mapping, repo: str, kind: str, actor: object, env: Mapping[str, str] | None,
              timeout: float) -> Decision:
    """Trust for an incident (a failed default-branch CI run, decided by its pusher) or an event (a pivotal-event dispatch,
    decided by its sender). Neither has an issue yet: ack files it, so the decision carries none."""
    if event_name == "repository_dispatch":
        return _dispatch(payload, repo, None, kind, env, timeout)
    if not _valid_login(actor):
        return _refused("invalid login for the workflow run's actor")
    return _verdict(None, kind, actor, lookup_permission(repo, actor, env=env, timeout=timeout))


def _dispatch(payload: Mapping, repo: str, issue: int | None, kind: str, env: Mapping[str, str] | None, timeout: float) -> Decision:
    """Trust for a dispatch: a fix round (``kind`` fix, with its issue) or an event (``kind`` event, no issue). From this repo.
    A fix round is trusted when the repo's own token sent it (the publish job does) or when a writer did. An event is trusted
    only when a writer sent it: no job sends one with the own token, so that sender is no proof and the event runs nothing (I3)."""
    if dispatch_problem(payload, repo):
        return _refused("the dispatch names another repository or has no fields")
    actor = _dig(payload, "sender", "login")
    if (actor, _dig(payload, "sender", "type")) == OWN_TOKEN_SENDER:
        if kind == "fix":
            return Decision(True, issue, kind, "a fix round sent with this repository's own token")
        return Decision(False, None, kind, "an event sent with this repository's own token is not trusted: nothing sends one, "
                                           "so the sender proves no writer")
    if not _valid_login(actor):
        return _refused("invalid login")
    return _verdict(issue, kind, actor, lookup_permission(repo, actor, env=env, timeout=timeout))


def _read_event(path: str | None) -> Mapping:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def issue_pin(payload: Mapping) -> str:
    """The pin of the event's issue: sha256 of the JSON array [title, body], or "" if unreadable.

    The factory reads the issue again before a run, computes the same value with this function, and
    refuses on a mismatch, so a body edited after trust decided never runs as trusted (R3, F2b). An
    empty pin means refuse. GitHub sends a null body for an empty issue, which is an empty body.
    """
    title = _dig(payload, "issue", "title")
    body = _dig(payload, "issue", "body")
    if body is None:
        body = ""
    if not isinstance(title, str) or not isinstance(body, str):
        return ""
    return hashlib.sha256(json.dumps([title, body]).encode("ascii")).hexdigest()


# The keys of a pivotal-fix client_payload, in order (fix.PAYLOAD_KEYS is this tuple). The pin covers all but the last:
# `findings` is text the factory never acts on as an instruction, so it is not pinned. `scores` (the hill-climb's history,
# engine/hill.py) is pinned: it decides which head the round builds from.
PAYLOAD_KEYS = ("issue", "slice", "round", "run_id", "branch", "head_sha", "issue_pin", "scores", "findings")
FIX_PIN_FIELDS = PAYLOAD_KEYS[:-1]  # derived, never a second list (R24)


def fix_pin(payload: Mapping) -> str:
    """The pin of a ``pivotal-fix`` dispatch, which carries no issue object: sha256 of the JSON array
    [issue, slice, round, run_id, branch, head_sha, issue_pin, scores] taken from ``client_payload``, or "" if any is unreadable.

    The factory computes it again from the same event (job.check_pin) and refuses on a mismatch, so a payload
    changed after trust decided, a round included, never runs. ``findings`` is not pinned: the factory does not
    act on it as an instruction. A ``/pivotal fix`` comment keeps the title-and-body pin (issue_pin).
    """
    fields = _dig(payload, "client_payload")
    if not isinstance(fields, Mapping):
        return ""
    values = [fields.get(name) for name in FIX_PIN_FIELDS]
    ints = (values[0], values[2], values[3])
    if any(_number(v) is None for v in ints) or not all(isinstance(v, str) and v for v in (values[1], values[4], values[5], values[6])):
        return ""
    if not isinstance(values[7], str):  # scores: text, possibly ""
        return ""
    return hashlib.sha256(json.dumps(values).encode("ascii")).hexdigest()


def main(env: Mapping[str, str], stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    """Print trusted, issue, kind and pin as job outputs. The verdict word goes to stderr."""
    pin = ""
    try:
        payload = _read_event(env.get("GITHUB_EVENT_PATH"))
        decision = decide(env.get("GITHUB_EVENT_NAME", ""), payload, env.get("GITHUB_REPOSITORY", ""), env=env)
        if decision.issue is not None:
            pin = issue_pin(payload)
            if decision.kind == "fix" and env.get("GITHUB_EVENT_NAME") == "repository_dispatch":
                pin = fix_pin(payload)
    except Exception as exc:  # an unexpected failure still prints the fail-closed outputs (F2b)
        decision = _refused(f"internal error ({type(exc).__name__})")
    print(f"trusted={'true' if decision.trusted else 'false'}", file=stdout)
    print(f"issue={'' if decision.issue is None else decision.issue}", file=stdout)
    print(f"kind={decision.kind}", file=stdout)
    print(f"pin={pin}", file=stdout)
    verdict = "trusted" if decision.trusted else "untrusted"
    print(f"{verdict}: {decision.reason}", file=stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(os.environ))
