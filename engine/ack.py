"""The "run started" comment, the building label and the incident issue (the ack job).

The job holds ``issues: write`` and runs no repo code: it checks out only the engine and calls gh. It
reads its input from env, and the event from the file GITHUB_EVENT_PATH names (untrusted text stays in
files and env, F2). Run it as ``python3 -I pivotal/engine/ack.py``.

The level (``LEVEL``, the PIVOTAL_LEVEL variable) gates the whole job: when it does not start this kind, ack
prints one neutral line and exits green with nothing written, no comment, no label, no incident issue. A level
that is not one of the four refuses. On a re-run (``RUN_ATTEMPT`` above 1) it posts no second comment when the
bot already commented this run's link.

Every refusal comes before the first write: a kind, a trust flag, a pin or an incident that is not
readable stops the job with nothing posted. Then it writes in this order: the incident issue (only for
``incident`` and ``event``), the comment, the ``pivotal:building`` label. The comment is
``⚡ run #N started (<preset>, <trusted|untrusted>) · <link>`` and ``piv do`` parses it (RUN_STARTED in
gh-piv/piv/do.py). The link is ``https://github.com/<repo>/actions/runs/<run id>``. The status labels
are created by ``piv init``, not here: before the first write ack reads each label it will use, and a repo
without it is refused with a pointer to ``piv init`` (adding an unknown label only makes GitHub create it plain).

An incident is the failing workflow's name and head SHA, title and link only, never the failure text. The
fingerprint is the workflow (a hash of its name) and the head SHA, kept in a comment line of the issue
body. At most one incident is open per workflow: an open ``pivotal:incident`` issue with the same workflow
is reused, and its body is updated when the new failure is at another head SHA. Its run is commented on like any other. Concurrency serialises incidents
(the caller's group for them is the same), so two runs do not both create. An ``event`` carries
``client_payload`` {workflow, title, head_sha, link} (all optional: piv event sends kind, title and link, and trust.py
decides the sender). A ``head_sha`` that fails its check is dropped; a ``link`` that is given must be this repository's run
(``event_link_ok``: https://github.com/<repo>/actions/runs/<id>, with an optional /job/<id>), the one shape ``piv event`` refuses
anything else by too (gh-piv/piv/event.py) and ``lines.link_request`` builds from, or the event is refused before any write: it is
never replaced with the run's own link (I4). A ``link`` that is absent is the run's own link.

Outputs: ``issue``, the number the later jobs use, and ``quiet`` (``true`` when the level starts no run of this
kind, so nothing was written; the quiet exit writes only ``quiet``). The report job takes ``quiet`` from here rather
than reading the level again, so a level changed mid-run cannot leave the issue at ``pivotal:building``.
"""

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script under python3 -I
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import job, lines  # noqa: E402
from engine.job import JobError  # noqa: E402

LINK_PREFIX = "https://github.com/"
BOT = "github-actions[bot]"
MARKER = re.compile(r"<!-- pivotal-incident workflow=([0-9a-f]{16}) head=([0-9a-f]{40}|none) -->")
TITLE_MAX = 80
NAME_MAX = 60
MARKDOWN = frozenset("#*[]()<>`@")
# The run tail an event's link may carry: the run's id, and optionally a job's id, 1 to 20 digits each (lines.LINK_TAIL is the same).
# No query, fragment, attempt, issue or pull request: the link is the one lines.link_request takes from the issue body (I-A).
RUN_TAIL = r"/actions/runs/[0-9]{1,20}(?:/job/[0-9]{1,20})?"


@dataclass(frozen=True)
class Incident:
    workflow: str  # the workflow's name, cleaned for a title
    fingerprint: str  # hash of the workflow name
    head_sha: str
    link: str
    kind: str
    given: bool = False  # the event sent a link (an absent one is the pivotal run's own, which a reused body must not take)


def event_link_ok(repo: str, link: str) -> bool:
    """True when ``link`` is exactly this repository's run (RUN_TAIL): the one link an event may carry."""
    return re.fullmatch(re.escape(f"{LINK_PREFIX}{repo}") + RUN_TAIL, link) is not None


def run_link(repo: str, run_id: str) -> str:
    return f"{LINK_PREFIX}{repo}/actions/runs/{job.read_number(run_id, 'run id', job.RUN_ID_DIGITS)}"


def started_preset(env: Mapping[str, str], kind: str, trusted: bool, labels: list[str], caller: str) -> str:
    """The preset the run will build with, named before plan decides (R4): the line the labels pick, by the trust rule, is
    the one plan picks (engine/lines.py). Its own preset when it has one, else the caller's name. A line that does not
    resolve names the caller's: plan refuses that run loudly, so the comment never claims a preset the run would not use."""
    try:
        custom, _problems = lines.custom_lines(env.get("PRESET_JSON", ""))
        choice = lines.choose(kind, trusted, labels, custom)
        line = custom.get(choice.name) or lines.load_line(choice.name)
    except lines.LineError:
        return caller
    return line.preset or caller


def started_comment(run_number: int, preset: str, trusted: bool, link: str, runner: str = job.HOSTED) -> str:
    who = "trusted" if trusted else "untrusted"
    # GitHub never falls back from an offline self-hosted runner: the job waits, so the comment says where it waits.
    wait = ", waiting for your runner" if runner == job.SELF_HOSTED else ""
    return f"⚡ run #{run_number} started ({preset}, {who}{wait}) · {link}"


def _clean(text: object, fallback: str) -> str:
    """One printable line of at most NAME_MAX characters with no markdown or mention, or the fallback."""
    if not isinstance(text, str):
        return fallback
    plain = "".join(c if c.isprintable() and c not in MARKDOWN else " " for c in text)
    line = " ".join(plain.split())[:NAME_MAX].strip()
    return line or fallback


def _event(path: str) -> Mapping:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        raise JobError("the event file is missing or not JSON") from None
    if not isinstance(data, dict):
        raise JobError("the event is not an object")
    return data


def read_incident(kind: str, event: Mapping, repo: str, fallback_link: str) -> Incident:
    """The incident an ``incident`` (workflow_run) or ``event`` (repository_dispatch) names, from the event file."""
    given_link = False
    if kind == "incident":
        run = event.get("workflow_run")
        run = run if isinstance(run, dict) else {}
        name = _clean(run.get("name"), "")
        head = run.get("head_sha")
        run_id = run.get("id")
        if not name or not isinstance(head, str) or job.SHA.fullmatch(head) is None:
            raise JobError("the failed run has no readable workflow name and head SHA")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
            raise JobError("the failed run has no readable id")
        link = f"{LINK_PREFIX}{repo}/actions/runs/{run_id}"
    else:
        payload = event.get("client_payload")
        payload = payload if isinstance(payload, dict) else {}
        name = _clean(payload.get("workflow"), "") or _clean(payload.get("title"), "event")
        head = payload.get("head_sha")
        head = head if isinstance(head, str) and job.SHA.fullmatch(head) else ""
        # A link that was not given is the pivotal run's own (nothing else to show). A link that WAS given is kept exactly or
        # refused: replacing it with the run's link would file an incident that points at the build itself (I4).
        given = payload.get("link")
        if given is None:
            link = fallback_link
        elif isinstance(given, str) and event_link_ok(repo, given):
            link = given
            given_link = True
        else:
            raise JobError(f"the event's link is not one piv event sends: {LINK_PREFIX}{repo}/actions/runs/<id>, optionally "
                           "/job/<id>, with 1 to 20 digits in each id and nothing else")
    # An event's fingerprint carries its kind, so an event named "CI" is not the incident of the CI workflow.
    seed = name if kind == "incident" else f"{kind}:{name}"
    return Incident(name, hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], head, link, kind, given_link)


def incident_body(incident: Incident) -> str:
    head = incident.head_sha or "none"
    what = "CI failed" if incident.kind == "incident" else "Event"
    at = f" at {incident.head_sha[:12]}" if incident.head_sha else ""
    marker = f"<!-- pivotal-incident workflow={incident.fingerprint} head={head} -->"
    return f"{what}{at}: {incident.link}\n\n{marker}\n"


def incident_title(incident: Incident) -> str:
    what = "CI failed" if incident.kind == "incident" else "Event"
    return f"{what}: {incident.workflow}"[:TITLE_MAX]


def body_link(body: str) -> str:
    """The link on the body's first line (incident_body writes it last), or "" when that line names none."""
    first = body.split("\n", 1)[0]
    return first.rsplit(" ", 1)[-1] if " " in first else ""


def find_open_incident(env: Mapping[str, str], repo: str, incident: Incident, label: str) -> tuple[int, str, str] | None:
    """(number, head SHA or "none", body) of the open incident issue for the same workflow, if any. Reading only."""
    out = job.gh(
        env,
        "api",
        "--paginate",
        f"repos/{repo}/issues?labels={label}&state=open&per_page=100",
        "--jq",
        '.[] | select(has("pull_request") | not) | {number, body}',
    )
    for line in out.splitlines():
        try:
            issue = json.loads(line)
        except ValueError:
            raise JobError("the open incident list is not JSON") from None
        body, number = issue.get("body"), issue.get("number")
        if not isinstance(body, str) or not isinstance(number, int):
            continue
        for found in MARKER.finditer(body):
            if found.group(1) == incident.fingerprint:
                return number, found.group(2), body
    return None


def already_announced(env: Mapping[str, str], repo: str, number: int, link: str) -> bool:
    """True when the bot already commented this run's link on the issue (a re-run). Reading only."""
    out = job.gh(
        env,
        "api",
        "--paginate",
        f"repos/{repo}/issues/{number}/comments?per_page=100",
        "--jq",
        ".[] | {login: .user.login, body}",
    )
    for line in out.splitlines():
        try:
            comment = json.loads(line)
        except ValueError:
            raise JobError("the comment list is not JSON") from None
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if comment.get("login") == BOT and isinstance(body, str) and body.startswith("⚡ run #") and body.endswith(f" · {link}"):
            return True
    return False


def run(env: Mapping[str, str]) -> None:
    repo = job.read_repo(env)
    kind, trusted = job.read_kind(env)
    job.read_engine_sha(env)
    if job.read_start(env.get("LEVEL", ""), env.get("CHECK", ""), kind) is None:
        # Nothing visible: no comment, no label, no incident issue. plan makes the same decision (job.read_start) and exits quietly.
        job.write_outputs(job.read_output_path(env), {"quiet": "true"})
        return
    caller = env.get("PRESET", "") or "unset"
    if caller != "unset":
        job.read_preset_name(caller)
    run_number = job.read_number(env.get("RUN_NUMBER", ""), "run number")
    attempt = job.read_number(env.get("RUN_ATTEMPT", "1"), "run attempt")
    link = run_link(repo, env.get("RUN_ID", ""))
    building = job.label_name("pivotal:building")
    # Reads and refusals first.
    create: Incident | None = None
    stale_head: str | None = None
    relink = False
    labels: list[str] = []  # the line's label, if the run has an issue: an incident or event picks its own line (plan's choice)
    if kind in job.PIN_KINDS:
        number = job.read_number(env.get("ISSUE", ""), "issue")
        title, body, labels = job.read_issue(env, repo, number)
        job.check_pin(kind, env.get("ISSUE_PIN", ""), title, body, job.read_dispatch(env, kind))
        job.require_label(env, repo, building)
    else:
        incident_label = job.label_name("pivotal:incident")
        incident = read_incident(kind, _event(env.get("GITHUB_EVENT_PATH", "")), repo, link)
        job.require_label(env, repo, building)
        job.require_label(env, repo, incident_label)
        found = find_open_incident(env, repo, incident, incident_label)
        number = found[0] if found is not None else 0
        create = incident if found is None else None
        if found is not None and incident.head_sha and found[1] != incident.head_sha:
            stale_head = found[1]
        # An event reused for another event names that event's link in the body, or plan builds the brief from the first one (F1).
        # Only a link the event gave: an absent one is the pivotal run's own, and it must not replace the first event's link.
        relink = found is not None and incident.kind == "event" and incident.given and body_link(found[2]) != incident.link
    announced = attempt > 1 and number > 0 and already_announced(env, repo, number, link)
    # Everything is known; check the output now so a bad value cannot follow a write.
    out_path = job.read_output_path(env)
    runner = env.get("RUNNER", job.HOSTED)
    if runner not in (job.HOSTED, job.SELF_HOSTED) or (runner == job.SELF_HOSTED and not trusted):
        raise JobError("the runner is not one the selection can give this run")
    comment = started_comment(run_number, started_preset(env, kind, trusted, labels, caller), trusted, link, runner)
    # Writes.
    if create is not None:
        made = job.gh_json(
            env,
            "api",
            "-X",
            "POST",
            f"repos/{repo}/issues",
            "-f",
            f"title={incident_title(create)}",
            "-f",
            f"body={incident_body(create)}",
            "-f",
            f"labels[]={incident_label}",
        )
        created = made.get("number") if isinstance(made, dict) else None
        if not isinstance(created, int) or isinstance(created, bool) or created < 1:
            raise JobError("the new incident issue has no number")
        number = created
    elif stale_head is not None or relink:
        # A later failure at a new head, or a second event with a new link, reuses the open issue: its body names the newest
        # head and the link this run was given. A same-head incident (workflow_run) is reused untouched.
        job.gh(env, "api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", f"body={incident_body(incident)}")
    if announced:
        print("this run's link is already commented; not posting it twice", file=sys.stderr)
    else:
        job.gh(env, "api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={comment}")
    job.gh(env, "api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={building}")
    job.write_outputs(out_path, {"issue": str(number), "quiet": "false"})


def main(env: Mapping[str, str]) -> int:
    return job.run_job(run, env)


if __name__ == "__main__":
    sys.exit(main(os.environ))
