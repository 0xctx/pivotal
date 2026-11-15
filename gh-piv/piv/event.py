"""piv event: send the repository_dispatch that a webhook's alert is filed as an incident from.

The request is checked before any gh call, and the repo's caller file is read and checked before the one
write, so a refusal writes nothing. The dispatch carries the kind, a title and a link, never the alert's
text (F2). The caller's trust job decides on the dispatch's sender (trust.py, T5): this repository's own token, or
a writer, is trusted and the run files one incident issue (ack); anyone else's dispatch runs nothing.
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from . import do, init
from .gh import Gh, GhFailed, PivError

EVENT_TYPE = "pivotal-event"  # the caller listens for it (templates/caller.yml); fix rounds use pivotal-fix
# The kinds the caller's factory job runs for, from a dispatch (templates/caller.yml), and the ones trust.KINDS names.
KINDS = ("event", "incident")
TITLE_MAX = do.TITLE_MAX
# The one link rule, the same as engine/ack.py's event_link_ok and lines.LINK_TAIL: this repository's run,
# https://github.com/<repo>/actions/runs/<id>, with an optional /job/<id>, and nothing else. The run's own engine keeps a link
# only if it matches, and plan builds from one only if it matches, so a link this accepts is one ack keeps and plan takes
# (test_ack checks all three over one corpus). A link is refused here, not replaced by ack (I4).
LINK_PREFIX = "https://github.com/"
RUN_TAIL = r"/actions/runs/[0-9]{1,20}(?:/job/[0-9]{1,20})?"
# Categories a title must not carry: controls (Cc), format (Cf: zero-width, bidi, soft hyphen, tag characters),
# private use (Co), unassigned (Cn), and the line and paragraph separators (Zl, Zp). Every bidi control is Cf.
UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Zl", "Zp"})
# Blank-looking characters that are not separators: braille blank, and the Hangul fillers and halfwidth filler.
BLANK_LOOKING = frozenset("⠀ㅤᅟᅠﾠ")
VISIBLE_CATEGORY_STARTS = ("L", "N", "P", "S")  # a letter, number, punctuation or symbol
DISPATCH_TYPES = re.compile(r"repository_dispatch:\s*\n\s*types:\s*\[([^\]]*)\]")  # the caller's own list, in order
STATUS_MAX = 60  # a status line is shown in an error; a longer one is cut
STATUS_204 = re.compile(r"HTTP/\S+ 204( .*)?")
WRITE_ACCESS = "dispatch needs write access (repo scope, or fine-grained Contents: write)"


def link_refusal(repo: str) -> str:
    return (f"--link must be {LINK_PREFIX}{repo}/actions/runs/N, or with /job/N after it, for a run of this repository, with "
            "1 to 20 digits in each id and nothing else (no query, fragment, issue, pull request, attempt, other repository "
            "or other host); the run keeps only that link, and any other is refused")


@dataclass(frozen=True)
class Request:
    repo: str
    kind: str
    title: str
    link: str


def request(repo: str, *, kind: str, title: str, link: str) -> Request:
    """The checked request. Every refusal is a usage error (exit 2), and none of them calls gh."""
    init.checked_repo(repo)
    if kind not in KINDS:
        raise PivError(f"--kind must be one of {', '.join(KINDS)}", 2)
    title = title.strip()
    init.one_line("--title", title)
    if len(title) > TITLE_MAX:
        raise PivError(f"--title must be at most {TITLE_MAX} characters", 2)
    if any(_unsafe(char) for char in title):
        raise PivError(
            "--title must be one line of plain text: no control, format, private-use, unassigned or separator "
            "character, and no blank-looking character (only the ordinary space)",
            2,
        )
    if not any(unicodedata.category(char).startswith(VISIBLE_CATEGORY_STARTS) for char in title):
        raise PivError("--title must contain a letter, digit, punctuation or symbol", 2)
    _check_link(repo, link)
    return Request(repo, kind, title, link)


def _unsafe(char: str) -> bool:
    category = unicodedata.category(char)
    return category in UNSAFE_CATEGORIES or (category == "Zs" and char != " ") or char in BLANK_LOOKING


def _check_link(repo: str, link: str) -> None:
    if re.fullmatch(re.escape(f"{LINK_PREFIX}{repo}") + RUN_TAIL, link) is None:
        raise PivError(link_refusal(repo), 2)


def payload(req: Request) -> dict:
    """The dispatch body. The caller reads kind from client_payload. The title and link are all the issue carries."""
    return {"event_type": EVENT_TYPE, "client_payload": {"kind": req.kind, "title": req.title, "link": req.link}}


def send(req: Request, gh: Gh, say: Callable[[str], None]) -> None:
    """Send the dispatch. The caller file is checked first, so a repo without Pivotal is refused and nothing is sent."""
    _need_caller(gh, req.repo)
    try:
        # The body goes on stdin (--input -), so the title and the link never reach an argument. -i shows the status.
        answer = gh.api(f"repos/{req.repo}/dispatches", method="POST", body=payload(req), include=True)
    except GhFailed as err:
        hint = f"; {WRITE_ACCESS}" if err.status in (403, 404) else ""
        raise PivError(f"{EVENT_TYPE} was not sent to {req.repo}: {err}{hint}") from err
    status = _status_line(answer)
    if not STATUS_204.fullmatch(status):
        answered = f"'{status}'" if status else "no status line"
        raise PivError(f"{EVENT_TYPE} was not sent to {req.repo}: GitHub answered {answered}, not 204")
    say(f"sent: {EVENT_TYPE} to {req.repo}, kind {req.kind}")
    say("trust: the caller decides on the sender. A writer's or this repository's own dispatch files one incident issue; "
        "anyone else's runs nothing")


def _status_line(answer: str) -> str:
    lines = answer.splitlines()
    return lines[0].strip()[:STATUS_MAX] if lines else ""


def _need_caller(gh: Gh, repo: str) -> None:
    """The repo's caller must exist and listen for the event, or GitHub accepts the dispatch and nothing runs for it."""
    path = f"repos/{repo}/contents/{init.WORKFLOW_PATH}"
    try:
        current = gh.api_json(path)
    except GhFailed as err:
        if err.status == 404:
            raise _missing(gh, repo) from None
        if err.status == 403:
            raise PivError(
                f"piv event {repo} could not read {init.WORKFLOW_PATH} (HTTP 403): the token needs read access to {repo}"
            ) from None
        raise PivError(f"piv event {repo} could not read {init.WORKFLOW_PATH}: {err}") from err
    _check_caller(current, repo)


def _missing(gh: Gh, repo: str) -> PivError:
    """Why the caller read found nothing: the repo is not there, or the token cannot see it; or it has no caller."""
    try:
        gh.api_json(f"repos/{repo}")
    except GhFailed as err:
        if err.status == 404:
            return PivError(f"repo {repo} was not found, or you have no access to it")
        return PivError(f"piv event {repo} could not check the repo: {err}")
    return PivError(f"{init.WORKFLOW_PATH} is not in {repo}: run piv init {repo} first")


def _check_caller(current: dict, repo: str) -> None:
    """The caller must list pivotal-event and be pinned to a release. A placeholder pin runs nothing (fail closed)."""
    text = init.decoded_text(current, f"repos/{repo}/contents/{init.WORKFLOW_PATH}")
    listed = DISPATCH_TYPES.search(text)
    if listed is None or EVENT_TYPE not in [item.strip() for item in listed.group(1).split(",")]:
        raise PivError(
            f"{init.WORKFLOW_PATH} in {repo} does not listen for {EVENT_TYPE}: "
            f"run piv init {repo} --update --check CMD to replace it"
        )
    pins = [sha for _, sha in init.ANY_USES.findall(text) + init.ANY_CHECKOUT.findall(text)]
    if not pins or any(pin == init.PIN_PLACEHOLDER or not init.SHA.fullmatch(pin) for pin in pins):
        raise PivError(
            f"{init.WORKFLOW_PATH} in {repo} is not pinned to a release: "
            f"run piv init {repo} --update --check CMD to replace it"
        )
