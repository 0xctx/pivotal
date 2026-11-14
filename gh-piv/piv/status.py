"""piv now and piv digest (F7): what is going on across the managed repos, read through gh.

Nothing is kept between runs; each run reads GitHub afresh. A repo is read whole or not at all. When any of
its gh calls fails, or gh answers with something that cannot be read or counted, the repo is unreadable: its
items are dropped, it is named with the reason, and the command exits 1. So an unreadable repo is never shown
as quiet.
"""

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from . import init
from .gh import Gh, PivError

TOPIC = "pivotal"
LIMIT = 1000  # the most items one list call returns; a full page could be cut short, so it is refused
MAIN_RUNS = 100  # the newest runs on the default branch, read for the red-main check
WATCHDOG_MINUTES = 15  # F7: a pivotal issue with no run, a building label with no run, or a run queued that long
NEW_HOURS = 24  # the default window for "new issues" in the digest
MAX_HOURS = 24 * 365  # the widest --since; a larger number overflows the date arithmetic
WORKFLOW = "pivotal"  # the caller's workflow name (templates/caller.yml); its run-name is pivotal-issue-N
RUN_TITLE = re.compile(r"pivotal-issue-(\d+)\Z")
PIVOTAL_RUNS = 200  # the newest pivotal runs read to tell whether an issue ever got a run
SEARCH_NOTE = "note: new issues and landed today are read through GitHub search, which can lag a few minutes"
STATUS_LABELS = frozenset({"pivotal:building", "pivotal:ready", "pivotal:landed", "pivotal:blocked"})
RED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
ISSUE_FIELDS = "number,title,url,createdAt,closedAt,updatedAt,labels"
PR_FIELDS = "number,title,url,isDraft,headRefName"
RUN_FIELDS = "databaseId,displayTitle,url,workflowName,conclusion,status,createdAt"
NOW_SECTIONS = ("building", "ready", "landed")
DIGEST_SECTIONS = (
    "new_issues",
    "red_mains",
    "ready_prs",
    "blocked",
    "no_run",
    "building_without_run",
    "queued_too_long",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Item:
    repo: str
    kind: str  # "issue", "pr" or "run"
    number: int  # an issue or pull request number, or a run's database id
    title: str
    url: str
    detail: str = ""


@dataclass(frozen=True)
class Unreadable:
    repo: str
    reason: str


@dataclass(frozen=True)
class Outcome:
    repos: list[str]
    unreadable: list[Unreadable]
    items: dict[str, list[Item]]  # section name to the items of the readable repos


def managed(gh: Gh, owner: str | None, named: list[str]) -> list[str]:
    """The repos to read: every named repo, and the repos of OWNER with the pivotal topic.

    The input is checked before gh runs. The search can lag, so a named repo is read even when the search misses it.
    """
    for repo in named:
        if not init.valid_repo(repo):
            raise PivError(f"--repo must be OWNER/NAME, for example 0xctx/piv-sandbox, not {repo!r}", 2)
    if owner is not None and not init.OWNER.fullmatch(owner):
        raise PivError(f"--owner must be a GitHub login, not {owner!r}", 2)
    if owner is None and not named:
        raise PivError(
            "name the repos to read: --owner OWNER (the repos with the pivotal topic) or --repo OWNER/NAME", 2
        )
    found = {repo.casefold(): repo for repo in reversed(named)}
    if owner is not None:
        answer = gh.run(
            "search", "repos", "--owner", owner, "--topic", TOPIC, "--json", "fullName", "--limit", str(LIMIT)
        )
        rows = _parse(answer)
        if len(rows) >= LIMIT:
            raise PivError(f"{owner} has {LIMIT} or more repos with the {TOPIC} topic; name them with --repo instead")
        for row in rows:
            name = _text(row, "fullName")
            found.setdefault(name.casefold(), name)
    if not found:
        raise PivError(f"no managed repos: {owner} has none with the {TOPIC} topic, and no --repo was given")
    return sorted(found.values(), key=str.casefold)


def collect(repos: list[str], sections: tuple[str, ...], read: Callable[[str], dict[str, list[Item]]]) -> Outcome:
    """Read each repo. A repo whose read raises is unreadable, and nothing it read is kept."""
    items: dict[str, list[Item]] = {section: [] for section in sections}
    unreadable: list[Unreadable] = []
    for repo in repos:
        try:
            got = read(repo)
        except PivError as err:
            unreadable.append(Unreadable(repo, str(err)))
            continue
        except Exception as err:  # a repo whose read breaks in any way is unreadable, never lost or quiet
            unreadable.append(Unreadable(repo, f"unexpected {type(err).__name__} while reading"))
            continue
        for section in sections:
            items[section].extend(got[section])
    return Outcome(repos, unreadable, items)


def read_now(gh: Gh, repo: str, today: str) -> dict[str, list[Item]]:
    return {
        "building": [_run_item(repo, row) for row in _pivotal(_runs(gh, repo, "--status", "in_progress", *_WF))],
        "ready": [_item(repo, row) for row in _issues(gh, repo, "--label", "pivotal:ready", "--state", "open")],
        "landed": [_item(repo, row) for row in _landed_on(gh, repo, today)],
    }


def read_digest(gh: Gh, repo: str, now: datetime, hours: int) -> dict[str, list[Item]]:
    since = now - timedelta(hours=hours)
    cutoff = now - timedelta(minutes=WATCHDOG_MINUTES)
    branch = _default_branch(gh, repo)

    new = [
        _item(repo, row)
        for row in _issues(gh, repo, "--state", "open", "--search", f"created:>={since.date().isoformat()}")
        if _when(row, "createdAt") >= since
    ]
    building = _issues(gh, repo, "--label", "pivotal:building", "--state", "open")
    queued = _pivotal(_runs(gh, repo, "--status", "queued", *_WF))
    active = queued + _pivotal(_runs(gh, repo, "--status", "in_progress", *_WF))
    seen = _issue_numbers(active + _pivotal(_runs(gh, repo, *_WF, limit=PIVOTAL_RUNS)))
    working = _issue_numbers(active)

    # An issue with the pivotal label and no status label is flagged when no pivotal run names it. updatedAt only
    # delays the flag (the label's own time is not in the list), so a comment can hide a stuck issue for 15 minutes.
    no_run = [
        _item(repo, row, f"no run started; last changed {row['updatedAt']}")
        for row in _issues(gh, repo, "--label", "pivotal", "--state", "open")
        if not _labels(row) & STATUS_LABELS and _int(row, "number") not in seen and _when(row, "updatedAt") <= cutoff
    ]
    building_without_run = [
        _item(repo, row, "no pivotal run in progress or queued for this issue")
        for row in building
        if _int(row, "number") not in working
    ]

    queued_too_long = []
    for run in queued:
        started = _when(run, "createdAt")
        if started <= cutoff:
            minutes = int((now - started).total_seconds() // 60)
            detail = f"queued {minutes} min"
            queued_too_long.append(_item(repo, run, detail, kind="run", number="databaseId", title="displayTitle"))

    ready = [
        _item(repo, row, f"branch {row['headRefName']}", kind="pr")
        for row in _rows(gh, "pr", repo, ["--state", "open"], PR_FIELDS)
        if _flag(row, "isDraft") is False and _text(row, "headRefName").startswith("pivotal/")
    ]
    return {
        "new_issues": new,
        "red_mains": _red_mains(gh, repo, branch),
        "ready_prs": ready,
        "blocked": [_item(repo, row) for row in _issues(gh, repo, "--label", "pivotal:blocked", "--state", "open")],
        "no_run": no_run,
        "building_without_run": building_without_run,
        "queued_too_long": queued_too_long,
    }


def now_line(outcome: Outcome) -> str:
    building = len(outcome.items["building"])
    agents = "agent" if building == 1 else "agents"
    ready = len(outcome.items["ready"])
    landed = len(outcome.items["landed"])
    return f"⚡ {building} {agents} working for you right now · {ready} ready for you · {landed} landed today (UTC)"


def now_lines(outcome: Outcome) -> list[str]:
    """The headline, then the repo count: always, so a quiet headline is never read without what was checked."""
    if outcome.unreadable:
        return [*unreadable_lines(outcome), now_line(outcome), SEARCH_NOTE]
    return [now_line(outcome), f"{len(outcome.repos)} repos checked, 0 unreadable", SEARCH_NOTE]


def unreadable_lines(outcome: Outcome) -> list[str]:
    if not outcome.unreadable:
        return []
    head = f"{len(outcome.unreadable)} of {len(outcome.repos)} repos unreadable:"
    return [head, *(_clean(f"  {bad.repo}: {bad.reason}") for bad in outcome.unreadable)]


def digest_text(outcome: Outcome) -> list[str]:
    checked = f"digest: {len(outcome.repos)} repos checked, {len(outcome.unreadable)} unreadable"
    items = outcome.items
    return [
        checked,
        *unreadable_lines(outcome),
        *_section("new issues", items["new_issues"]),
        *_section("red mains", items["red_mains"]),
        *_section("ready PRs", items["ready_prs"]),
        *_section("blocked", items["blocked"]),
        "watchdog (F7):",
        *_section("no run after 15 min", items["no_run"], indent="  "),
        *_section("building with no active run", items["building_without_run"], indent="  "),
        *_section("queued over 15 min", items["queued_too_long"], indent="  "),
        SEARCH_NOTE,
    ]


def digest_json(outcome: Outcome) -> dict[str, object]:
    items = {section: [asdict(item) for item in found] for section, found in outcome.items.items()}
    return {
        "repos_checked": len(outcome.repos),
        "repos_unreadable": len(outcome.unreadable),
        "unreadable": [{"repo": bad.repo, "reason": bad.reason} for bad in outcome.unreadable],
        "note": SEARCH_NOTE,
        "new_issues": items["new_issues"],
        "red_mains": items["red_mains"],
        "ready_prs": items["ready_prs"],
        "blocked": items["blocked"],
        "watchdog": {
            "no_run": items["no_run"],
            "building_without_run": items["building_without_run"],
            "queued_too_long": items["queued_too_long"],
        },
    }


def show_now(gh: Gh, owner: str | None, named: list[str], when: datetime, say: Callable[[str], None]) -> int:
    repos = managed(gh, owner, named)
    today = when.date().isoformat()
    outcome = collect(repos, NOW_SECTIONS, lambda repo: read_now(gh, repo, today))
    for line in now_lines(outcome):
        say(line)
    return 1 if outcome.unreadable else 0


def show_digest(
    gh: Gh,
    owner: str | None,
    named: list[str],
    when: datetime,
    hours: int,
    as_json: bool,
    say: Callable[[str], None],
) -> int:
    try:
        if not 1 <= hours <= MAX_HOURS:
            raise PivError(f"--since must be between 1 and {MAX_HOURS} hours", 2)
        repos = managed(gh, owner, named)
    except PivError as err:
        if as_json:  # a JSON consumer gets a document, never an empty stream
            failed = {"repos_checked": 0, "repos_unreadable": 0, "unreadable": [], "error": str(err)}
            say(json.dumps(failed, indent=2))
        raise
    outcome = collect(repos, DIGEST_SECTIONS, lambda repo: read_digest(gh, repo, when, hours))
    if as_json:
        say(json.dumps(digest_json(outcome), indent=2))
    else:
        for line in digest_text(outcome):
            say(line)
    return 1 if outcome.unreadable else 0


def _issues(gh: Gh, repo: str, *flags: str) -> list[dict]:
    return _rows(gh, "issue", repo, list(flags), ISSUE_FIELDS)


_WF = ("--workflow", WORKFLOW)


def _runs(gh: Gh, repo: str, *flags: str, limit: int | None = None) -> list[dict]:
    if limit is not None:  # the newest few: a full page is expected, so it is not refused
        answer = gh.run("run", "list", "-R", repo, *flags, "--json", RUN_FIELDS, "--limit", str(limit))
        return _parse(answer)
    return _rows(gh, "run", repo, list(flags), RUN_FIELDS)


def _pivotal(runs: list[dict]) -> list[dict]:
    """The runs of the pivotal workflow. The workflow flag selects them; this keeps an unrelated run out regardless."""
    return [run for run in runs if _text(run, "workflowName").casefold() == WORKFLOW]


def _issue_numbers(runs: list[dict]) -> set[int]:
    """The issues the runs belong to, from the caller's run-name pivotal-issue-N."""
    found = set()
    for run in runs:
        match = RUN_TITLE.match(_text(run, "displayTitle"))
        if match:
            found.add(int(match.group(1)))
    return found


def _landed_on(gh: Gh, repo: str, today: str) -> list[dict]:
    """The landed issues closed on TODAY (a UTC date). The search narrows the list; the check keeps it true."""
    rows = _issues(gh, repo, "--label", "pivotal:landed", "--state", "closed", "--search", f"closed:>={today}")
    return [row for row in rows if _text(row, "closedAt").startswith(today)]


def _red_mains(gh: Gh, repo: str, branch: str) -> list[Item]:
    """The newest completed run of each workflow on the default branch, when it concluded red.

    Only the newest MAIN_RUNS runs are read, so a workflow whose last run is older than that is not judged.
    """
    answer = gh.run("run", "list", "-R", repo, "--branch", branch, "--json", RUN_FIELDS, "--limit", str(MAIN_RUNS))
    newest: dict[object, dict] = {}
    for row in _parse(answer):
        if _text(row, "status") == "completed":
            newest.setdefault(row.get("workflowName"), row)
    red = []
    for row in newest.values():
        conclusion = _text(row, "conclusion")
        if conclusion in RED_CONCLUSIONS:
            detail = f"{row.get('workflowName')} on {branch} concluded {conclusion}"
            red.append(_item(repo, row, detail, kind="run", number="databaseId", title="displayTitle"))
    return red


def _rows(gh: Gh, kind: str, repo: str, flags: list[str], fields: str) -> list[dict]:
    answer = gh.run(kind, "list", "-R", repo, *flags, "--json", fields, "--limit", str(LIMIT))
    rows = _parse(answer)
    if len(rows) >= LIMIT:
        raise PivError(f"{kind} lists reached {LIMIT} items, so they cannot all be counted")
    return rows


def _default_branch(gh: Gh, repo: str) -> str:
    return _text(gh.api_json(f"repos/{repo}"), "default_branch")


def _parse(answer: str) -> list[dict]:
    try:
        rows = json.loads(answer)
    except ValueError:
        raise PivError("gh answered with something other than JSON") from None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise PivError("gh answered with something other than a list of objects")
    return rows


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise PivError(f"gh answered without a usable {key}")
    return value


def _flag(row: dict, key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise PivError(f"gh answered without a usable {key}")
    return value


def _int(row: dict, key: str) -> int:
    value = row.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PivError(f"gh answered without a usable {key}")
    return value


def _when(row: dict, key: str) -> datetime:
    try:
        when = datetime.fromisoformat(row.get(key))
    except (TypeError, ValueError):
        raise PivError(f"gh answered with a {key} that cannot be read") from None
    if when.tzinfo is None:
        raise PivError(f"gh answered with a {key} that has no time zone")
    return when


def _labels(row: dict) -> set[str]:
    labels = row.get("labels")
    if not isinstance(labels, list):
        raise PivError("gh answered with labels that cannot be read")
    if not all(isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels):
        raise PivError("gh answered with labels that cannot be read")
    return {label["name"] for label in labels}


def _item(
    repo: str,
    row: dict,
    detail: str = "",
    *,
    kind: str = "issue",
    number: str = "number",
    title: str = "title",
) -> Item:
    value = row.get(number)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PivError(f"gh answered with an item that has no usable {number}")
    return Item(repo, kind, value, _text(row, title), _text(row, "url"), detail)


def _section(title: str, items: list[Item], indent: str = "") -> list[str]:
    lines = [f"{indent}{title} ({len(items)})"]
    if not items:
        return [*lines, f"{indent}  none"]
    return [*lines, *(f"{indent}  {_line(item)}" for item in items)]


def _run_item(repo: str, row: dict) -> Item:
    return _item(repo, row, "in progress", kind="run", number="databaseId", title="displayTitle")


def _clean(text: str) -> str:
    """Escape control, format and line-separator characters, so GitHub text cannot move the cursor or forge a line."""
    return "".join(
        ch.encode("unicode_escape").decode() if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch
        for ch in text
    )


def _line(item: Item) -> str:
    ref = f"run {item.number}" if item.kind == "run" else f"#{item.number}"
    tail = f" ({item.detail})" if item.detail else ""
    return _clean(f"{item.repo} {ref} {item.title} {item.url}{tail}")
