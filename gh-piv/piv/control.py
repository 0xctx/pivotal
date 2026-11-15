"""piv land, piv fix, piv stop and piv resume: the terminal's side of the comment commands.

``land`` and ``fix`` comment ``/pivotal land`` or ``/pivotal fix`` on an issue, and ``stop`` comments ``/pivotal stop`` on
each issue with a run in flight, so the chat, a comment and the terminal reach the same trusted path (trust.py, caller.yml).
Nothing here decides whether a change lands: the land job checks the seal and the level (F4, F6). ``stop --all`` also sets
PIVOTAL_LEVEL to watch in every managed repo, so no new run starts, and ``resume`` puts back the level each repo had.

The stop keeps that level in the repo variable PIVOTAL_PRIOR_LEVEL, not on this machine, so piv stays stateless. A refusal
(a bad number, a closed issue, a level that is not a level) makes no write: every read comes before the first write.
"""

import re
from collections.abc import Callable
from urllib.parse import quote

from . import doctor, init, status
from .gh import Gh, GhFailed, PivError

LAND = "/pivotal land"
FIX = "/pivotal fix"
STOP = "/pivotal stop"
READY = "pivotal:ready"
WATCH = "watch"
ISSUE = re.compile(r"#?([1-9][0-9]{0,8})")
# The run statuses the caller's stop job cancels (templates/caller.yml): a run in any of them has no outcome yet.
IN_FLIGHT = ("queued", "in_progress", "waiting", "requested", "pending")
PAGE = 100  # the issues API's most items per page
PAGES = 20  # a listing of more than 2000 issues is refused, not cut short


def issue_number(text: str) -> int:
    """The issue number from ``12`` or ``#12``. Anything else is a usage error, and nothing is called."""
    match = ISSUE.fullmatch(text)
    if match is None:
        raise PivError("the issue must be a number, for example 12 or #12", 2)
    return int(match.group(1))


def land(gh: Gh, repo: str, issue: str | None, everything: bool, say: Callable[[str], None]) -> None:
    """Comment ``/pivotal land``: on ISSUE; on every ready issue with --all; or, with neither, on the one ready issue."""
    repo = init.checked_repo(repo)
    if everything and issue is not None:
        raise PivError("name one issue or --all, not both", 2)
    if everything:
        numbers = _labelled(gh, repo, READY)
        if not numbers:
            say(f"{repo}: no issue is ready to land")
            return
        _comment_all(gh, repo, numbers, LAND, say)
        return
    if issue is None:
        ready = _labelled(gh, repo, READY)
        if not ready:
            raise PivError(f"{repo}: no issue is ready to land: name one with #N, once a run has made it ready", 1)
        if len(ready) > 1:
            names = ", ".join(f"#{number}" for number in ready)
            raise PivError(f"{repo}: {len(ready)} issues are ready ({names}): name one with #N, or use --all", 2)
        number = ready[0]
    else:
        number = issue_number(issue)
        _open_issue(gh, repo, number)
    _post(gh, repo, number, LAND, say)


def fix(gh: Gh, repo: str, issue: str, say: Callable[[str], None]) -> None:
    """Comment ``/pivotal fix`` on an open issue: a round-0 run from the issue's own text (engine/fix.py)."""
    repo = init.checked_repo(repo)
    number = issue_number(issue)
    _open_issue(gh, repo, number)
    _post(gh, repo, number, FIX, say)


def stop(gh: Gh, repo: str | None, everything: bool, named: list[str], say: Callable[[str], None]) -> None:
    """Cancel the runs of one repo (``stop REPO``) or of every managed repo (``stop --all``, which also sets watch)."""
    if everything and repo is not None:
        raise PivError("name one repo or --all, not both", 2)
    if not everything and repo is None:
        raise PivError("name a repo, for example piv stop 0xctx/piv-sandbox, or --all for every managed repo", 2)
    if named and not everything:
        raise PivError("--repo goes with --all", 2)
    if everything:
        _stop_everywhere(gh, named, say)
        return
    repo = init.checked_repo(repo or "")
    numbers = in_flight(gh, repo)
    if not numbers:
        say(f"{repo}: no run is in flight: nothing to stop")
        return
    _comment_all(gh, repo, numbers, STOP, say)


def resume(gh: Gh, named: list[str], say: Callable[[str], None]) -> None:
    """Put back the level ``stop --all`` replaced with watch, in every managed repo that has a record of it."""
    repos = _everywhere(gh, named)
    # Every read first: a recorded level that is not a level refuses the whole resume before any write.
    plans = [(repo, _prior(gh, repo), _level(gh, repo)) for repo in repos]
    for repo, prior, level in plans:
        if prior is None:
            say(f"{repo}: not stopped by piv stop: nothing to restore")
        elif level != WATCH:
            # Someone changed the level after the stop. Resume undoes the watch the stop set, so it keeps what is there.
            gh.run("variable", "delete", init.VAR_PRIOR_LEVEL, "--repo", repo)
            say(f"{repo}: level is {level}, not watch: kept {level}; the record of the stop is removed")
        else:
            gh.run("variable", "set", init.VAR_LEVEL, "--repo", repo, stdin=prior)
            gh.run("variable", "delete", init.VAR_PRIOR_LEVEL, "--repo", repo)
            say(f"{repo}: level watch -> {prior}")


def in_flight(gh: Gh, repo: str) -> list[int]:
    """The issues that have a pivotal run in flight, from the caller's run-name pivotal-issue-N (the stop job's rule)."""
    found: set[int] = set()
    for state in IN_FLIGHT:
        runs = status._pivotal(status._runs(gh, repo, "--status", state, *status._WF))
        found |= status._issue_numbers(runs)
    return sorted(found)


def _stop_everywhere(gh: Gh, named: list[str], say: Callable[[str], None]) -> None:
    repos = _everywhere(gh, named)
    # Every read first, so a level that is not a level refuses before any write.
    plans = [(repo, _level(gh, repo), _prior(gh, repo)) for repo in repos]
    problems: list[str] = []
    for repo, level, prior in plans:
        try:
            if level == WATCH:
                # Already watch. A record from an earlier stop stays: it still holds the level resume must restore.
                note = f"; piv resume restores {prior}" if prior is not None else ""
                say(f"{repo}: already watch{note}")
                continue
            # The record goes first: a failure after it leaves the level as it was, and resume restores the same value.
            gh.run("variable", "set", init.VAR_PRIOR_LEVEL, "--repo", repo, stdin=level)
            gh.run("variable", "set", init.VAR_LEVEL, "--repo", repo, stdin=WATCH)
            say(f"{repo}: level {level} -> watch; piv resume restores {level}")
        except PivError as err:
            problems.append(repo)
            say(f"{repo}: level not set: {err}")
    # The runs come after the level, so a run that started before the watch is found and stopped.
    for repo in repos:
        try:
            numbers = in_flight(gh, repo)
            if not numbers:
                say(f"{repo}: no run is in flight")
                continue
            _comment_all(gh, repo, numbers, STOP, say)
        except PivError as err:
            problems.append(repo)
            say(f"{repo}: runs not stopped: {err}")
    if problems:
        raise PivError(f"stop --all did not finish in {len(set(problems))} repo(s): {', '.join(sorted(set(problems)))}")


def _everywhere(gh: Gh, named: list[str]) -> list[str]:
    """Every managed repo: the repos you own with the pivotal topic, and any named with --repo (the search can lag)."""
    names = set(doctor.managed_repos(gh))
    names.update(init.checked_repo(name) for name in named)
    if not names:
        raise PivError("no managed repos: none of your repos carries the pivotal topic; name them with --repo", 1)
    return sorted(names)


def _level(gh: Gh, repo: str) -> str:
    """The repo's level now. Unset or empty is the default, as the engine reads it; anything else that is not a level refuses."""
    value = init.variable_value(gh, repo, init.VAR_LEVEL)
    if not value:
        return init.DEFAULT_LEVEL
    if value not in init.LEVELS:
        raise PivError(
            f"{repo}: {init.VAR_LEVEL} is not a level ({', '.join(init.LEVELS)}): fix it, then run again. Nothing was changed.",
            2,
        )
    return value


def _prior(gh: Gh, repo: str) -> str | None:
    """The level a stop recorded, or None when there is no record. A record that is not a level refuses."""
    value = init.variable_value(gh, repo, init.VAR_PRIOR_LEVEL)
    if value is None:
        return None
    if value not in init.LEVELS:
        raise PivError(
            f"{repo}: {init.VAR_PRIOR_LEVEL} is not a level ({', '.join(init.LEVELS)}): fix it, then run again. Nothing was changed.",
            2,
        )
    return value


def _open_issue(gh: Gh, repo: str, number: int) -> None:
    """The issue must exist, be open and be an issue, not a pull request, before a comment is posted."""
    try:
        data = gh.api_json(f"repos/{repo}/issues/{number}")
    except GhFailed as err:
        if err.status == 404:
            raise PivError(f"{repo} has no issue #{number}", 1) from None
        raise
    if "pull_request" in data:
        raise PivError(f"#{number} in {repo} is a pull request: name the issue it closes", 2)
    if data.get("state") != "open":
        raise PivError(f"#{number} in {repo} is not open: nothing was commented", 1)


def _labelled(gh: Gh, repo: str, label: str) -> list[int]:
    """The open issues with the label, in ascending order. The issues API lists pull requests too, so they are left out."""
    numbers: set[int] = set()
    for page in range(1, PAGES + 1):
        rows = gh.api_list(f"repos/{repo}/issues?state=open&labels={quote(label, safe='')}&per_page={PAGE}&page={page}")
        for row in rows:
            if not isinstance(row, dict):
                raise PivError(f"gh api repos/{repo}/issues answered with something other than issues")
            if "pull_request" in row:
                continue
            number = row.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise PivError(f"gh api repos/{repo}/issues gave an issue without a usable number")
            numbers.add(number)
        if len(rows) < PAGE:
            return sorted(numbers)
    raise PivError(f"{repo} has more than {PAGE * PAGES} open issues with {label}; that many are not read at once")


def _post(gh: Gh, repo: str, number: int, text: str, say: Callable[[str], None]) -> None:
    gh.api(f"repos/{repo}/issues/{number}/comments", method="POST", body={"body": text})
    say(f"{repo}#{number}: commented {text}")


def _comment_all(gh: Gh, repo: str, numbers: list[int], text: str, say: Callable[[str], None]) -> None:
    """Comment on each issue. One that fails is named and the rest still get theirs; any failure is raised at the end."""
    failed: list[int] = []
    for number in numbers:
        try:
            _post(gh, repo, number, text, say)
        except GhFailed as err:
            failed.append(number)
            say(f"{repo}#{number}: not commented: {err}")
    if failed:
        names = ", ".join(f"#{number}" for number in failed)
        raise PivError(f"{repo}: {len(failed)} of {len(numbers)} comments did not post: {names}")
