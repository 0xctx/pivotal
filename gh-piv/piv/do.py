"""piv do: file the labelled issue that starts a Pivotal run, and print its run link.

The request is checked, and everything it reads is read, before the first write, so a refusal writes
nothing. Then the mockup goes to the pivotal-assets branch, the issue is created, and the trigger label
is added last: the label is what starts the run, so no run starts before its issue is complete. The run
link comes from ack's comment on the issue, and ``do`` waits for it.

The text is untrusted data downstream. It reaches gh on stdin, and only its first line is a title.
"""

import base64
import hashlib
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import init
from .gh import Gh, GhFailed, PivError

LINES = init.LINES
DEFAULT_LINE = "build"
ASSET_BRANCH = "pivotal-assets"  # images for a mirror run are committed here
MOCKUP_DIR = "mockups"
# The first bytes each image type starts with. A file whose bytes do not match its extension is refused.
IMAGE_SIGNATURES = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff"}
IMAGE_EXTENSIONS = tuple(IMAGE_SIGNATURES)
IMAGE_MAX_BYTES = 10 * 1024 * 1024
TEXT_MAX = 8000
SCORE_MAX = 1000
BODY_MAX = 65536  # GitHub's limit on an issue body, in characters: the text and the footer together
TITLE_MAX = 120
# ASCII digits only: \d also matches other scripts' digits, which the footer's readers do not take.
TARGET = re.compile(r"0?\.[0-9]{1,6}|1(?:\.0{1,6})?")  # the engine's form, copied (engine/hill.py TARGET): test_do checks that they agree
ISSUE_URL = re.compile(r"/issues/([0-9]+)$")
# ack's comment: "⚡ run #N started (<preset>, <trusted/untrusted>) · <link>", written by github-actions.
# The link is on GitHub and ends at its last character that is not punctuation, so "…/runs/42." gives "…/runs/42".
ACK_LOGIN = "github-actions[bot]"
RUN_LINK_END = ".,;:!?)]}>'\"`*"
RUN_STARTED = re.compile(r"run #[0-9]+ started\b.*?(https://github\.com/\S*[^\s" + re.escape(RUN_LINK_END) + "])")
POLLS = 60
POLL_SECONDS = 5
POLL_FAILURES = 3  # failed reads of the comments in a row before do gives up; one transient error is not enough
NOT_AGAIN = "Do not file the request again: check the issue."


@dataclass(frozen=True)
class Image:
    path: str  # where the mockup lives on ASSET_BRANCH, named by its digest so the same file is written once
    data: bytes


@dataclass(frozen=True)
class Request:
    repo: str
    text: str
    line: str
    image: Image | None
    score: str | None
    target: str | None

    @property
    def title(self) -> str:
        return self.text.strip().splitlines()[0][:TITLE_MAX]

    @property
    def label(self) -> str:
        """The one trigger label: ``pivotal`` for the default line, ``pivotal:<line>`` for the others."""
        return "pivotal" if self.line == DEFAULT_LINE else f"pivotal:{self.line}"


def request(
    repo: str,
    text: str,
    *,
    line: str | None = None,
    image: str | None = None,
    score: str | None = None,
    target: str | None = None,
) -> Request:
    """The checked request. Every refusal is a usage error (exit 2), and none of them calls gh."""
    init.checked_repo(repo)
    if not text.strip():
        raise PivError("TEXT must say what to build", 2)
    if len(text) > TEXT_MAX:
        raise PivError(f"TEXT must be at most {TEXT_MAX} characters", 2)
    if "\x00" in text or not _is_text(text):
        raise PivError("TEXT must be UTF-8 text without NUL", 2)
    chosen = DEFAULT_LINE if line is None else line
    if chosen not in LINES:
        raise PivError(f"--line must be one of {', '.join(LINES)}", 2)
    if (score is None) != (target is None):
        raise PivError("--score and --target go together: the command and the number the gate needs", 2)
    if score is not None:
        init.one_line("--score", score)
        # splitlines() splits on more than the control characters one_line refuses (U+0085, U+2028, U+2029).
        if score.splitlines() != [score]:
            raise PivError("--score must be one non-empty line", 2)
        if len(score) > SCORE_MAX:
            raise PivError(f"--score must be at most {SCORE_MAX} characters", 2)
        if not TARGET.fullmatch(target or "") or not 0 < float(target) <= 1:
            raise PivError("--target must be a decimal above 0 and at most 1, with at most six decimals, for example 0.9", 2)
    checked = Request(repo, text.strip(), chosen, None if image is None else _image(image), score, target)
    if len(body(checked)) > BODY_MAX:
        raise PivError(f"the issue body must be at most {BODY_MAX} characters, the text and the footer together", 2)
    return checked


def _image(path: str) -> Image:
    """The mockup's bytes and its path on the asset branch. Read here, so a bad file is refused before any call."""
    extension = os.path.splitext(path)[1].lower()
    if extension not in IMAGE_EXTENSIONS:
        raise PivError(f"--image must be a {', '.join(IMAGE_EXTENSIONS)} file", 2)
    if not os.path.isfile(path):
        raise PivError(f"--image {path}: no such file", 2)
    try:
        with open(path, "rb") as fh:
            data = fh.read(IMAGE_MAX_BYTES + 1)
    except OSError as err:
        raise PivError(f"--image {path}: cannot read it ({err.strerror})", 2) from None
    if len(data) > IMAGE_MAX_BYTES:
        raise PivError(f"--image {path} is larger than {IMAGE_MAX_BYTES // (1024 * 1024)} MB", 2)
    if not data.startswith(IMAGE_SIGNATURES[extension]):
        raise PivError(f"--image {path} is not a real PNG or JPEG file, so it does not match its extension", 2)
    digest = hashlib.sha256(data).hexdigest()[:16]
    return Image(f"{MOCKUP_DIR}/{digest}{extension}", data)


def _is_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def body(req: Request) -> str:
    """The issue body: the author's text, then a footer the gate reads (line, mockup, score, target)."""
    footer = ["---", "Pivotal request", f"- line: {req.line}"]
    if req.image is not None:
        footer.append(f"- mockup: {req.image.path}")
    if req.score is not None:
        footer.append(f"- score: {req.score}")
        footer.append(f"- target: {req.target}")
    return "\n\n".join([req.text, "\n".join(footer)]) + "\n"


def do(
    req: Request,
    gh: Gh,
    say: Callable[[str], None],
    *,
    pause: Callable[[float], None] | None = None,
    polls: int | None = None,
) -> str:
    """File the issue, then wait for ack's run link, which is returned (``piv demo`` lists it).

    ``pause`` and ``polls`` default to the module's constants, read at call time so a test can shorten the wait.
    """
    wait = time.sleep if pause is None else pause
    budget = POLLS if polls is None else polls
    repo = req.repo
    say(f"piv do {repo}: line {req.line}, trigger label {req.label}")
    # Every read comes first, so a refusal here writes nothing.
    default = _default_branch(gh, repo)
    _need_label(gh, repo, req.label)
    asset_sha = _ref_sha(gh, repo, f"heads/{ASSET_BRANCH}")
    branch_from: str | None = None  # the commit a missing asset branch starts from
    present = False  # whether the mockup is already on the asset branch
    if req.image is not None:
        if asset_sha is None:
            branch_from = _ref_sha(gh, repo, f"heads/{default}")
            if branch_from is None:
                raise PivError(f"{repo} has no commit on {default} to start {ASSET_BRANCH} from")
        else:
            present = _file_present(gh, repo, req.image.path)
    # The writes: the mockup, then the issue, then the label that starts the run.
    if req.image is not None:
        _write_mockup(gh, repo, req.image, branch_from, present, say)
    url, number = _create_issue(gh, repo, req, say)
    _add_label(gh, repo, number, url, req.label)
    say(f"label {req.label} added: the run starts when ack comments")
    say("waiting for ack's run link")
    failures = 0  # consecutive failed reads; the issue exists, so a failed read is retried, not a clean miss
    for attempt in range(budget):
        try:
            link = _run_link(gh, repo, number)
        except PivError as err:
            failures += 1
            if failures == POLL_FAILURES:
                raise PivError(
                    f"issue #{number} is at {url}, but reading its comments failed {POLL_FAILURES} times in a row "
                    f"({err}). {NOT_AGAIN}"
                ) from err
            say(f"reading the comments failed ({err}); trying again")
        else:
            failures = 0
            if link is not None:
                say(f"run link: {link}")
                return link
        if attempt + 1 < budget:
            wait(POLL_SECONDS)
    raise PivError(
        f"issue #{number} is at {url}, but no run link was posted after {budget * POLL_SECONDS // 60} minutes; "
        f"the run may be waiting for a runner. {NOT_AGAIN}"
    )


def _default_branch(gh: Gh, repo: str) -> str:
    name = gh.api_json(f"repos/{repo}").get("default_branch")
    if not isinstance(name, str) or not name:
        raise PivError(f"gh api repos/{repo} gave no default branch")
    return name


def _need_label(gh: Gh, repo: str, label: str) -> None:
    """The label must exist, so the add that starts the run cannot fail on a missing label after the issue is made."""
    try:
        gh.api_json(f"repos/{repo}/labels/{label}")
    except GhFailed as err:
        if err.status == 404:
            raise PivError(f"label {label} is not set up in {repo}: run piv init {repo} first") from None
        raise


def _ref_sha(gh: Gh, repo: str, ref: str) -> str | None:
    """The commit a branch points to, or None when the branch does not exist (a 404)."""
    try:
        data = gh.api_json(f"repos/{repo}/git/ref/{ref}")
    except GhFailed as err:
        if err.status == 404:
            return None
        raise
    obj = data.get("object")
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not init.SHA.fullmatch(sha):
        raise PivError(f"gh api repos/{repo}/git/ref/{ref} gave no commit sha")
    return sha


def _file_present(gh: Gh, repo: str, path: str) -> bool:
    try:
        gh.api_json(f"repos/{repo}/contents/{path}?ref={ASSET_BRANCH}")
    except GhFailed as err:
        if err.status == 404:
            return False
        raise
    return True


def _write_mockup(
    gh: Gh, repo: str, image: Image, branch_from: str | None, present: bool, say: Callable[[str], None]
) -> None:
    try:
        if branch_from is not None:
            gh.api(
                f"repos/{repo}/git/refs", method="POST", body={"ref": f"refs/heads/{ASSET_BRANCH}", "sha": branch_from}
            )
            say(f"branch {ASSET_BRANCH}: created")
        if present:
            say(f"mockup {image.path}: already on {ASSET_BRANCH}")
            return
        body = {
            "message": f"Add the mockup {image.path} for a piv do request",
            "content": base64.b64encode(image.data).decode("ascii"),
            "branch": ASSET_BRANCH,
        }
        gh.api(f"repos/{repo}/contents/{image.path}", method="PUT", body=body)
    except GhFailed as err:
        # 403 and 422 are how GitHub refuses a write to a protected branch or under a ruleset. Nothing is filed yet.
        if err.status not in (403, 422):
            raise
        raise PivError(
            f"{ASSET_BRANCH} on {repo} refused the write (HTTP {err.status}): it may be protected or under a ruleset. "
            "Nothing was filed: no issue was created."
        ) from err
    say(f"mockup {image.path}: committed to {ASSET_BRANCH}")


def _create_issue(gh: Gh, repo: str, req: Request, say: Callable[[str], None]) -> tuple[str, int]:
    # The body goes on stdin (--body-file -), so a long or untrusted text never reaches an argument.
    out = gh.run("issue", "create", "--repo", repo, "--title", req.title, "--body-file", "-", stdin=body(req))
    url = out.strip().splitlines()[-1].strip() if out.strip() else ""
    match = ISSUE_URL.search(url)
    if match is None:
        raise PivError(f"gh issue create did not print the issue's URL; check {repo} before filing it again")
    say(f"issue created: {url}")
    return url, int(match.group(1))


def _add_label(gh: Gh, repo: str, number: int, url: str, label: str) -> None:
    try:
        gh.run("issue", "edit", str(number), "--repo", repo, "--add-label", label)
    except GhFailed as err:
        raise PivError(
            f"issue #{number} is at {url}, but its label {label} could not be added, so no run starts: {err}"
        ) from err


def _run_link(gh: Gh, repo: str, number: int) -> str | None:
    """The link in ack's comment, or None while there is none.

    Only the bot's comment counts, and its text is never printed, only the link.
    """
    for comment in gh.api_list(f"repos/{repo}/issues/{number}/comments?per_page=100"):
        user = comment.get("user") if isinstance(comment, dict) else None
        login = user.get("login") if isinstance(user, dict) else None
        text = comment.get("body") if isinstance(comment, dict) else None
        if login != ACK_LOGIN or not isinstance(text, str):
            continue
        match = RUN_STARTED.search(text)
        # A link with a control character in it is not a link to show (isprintable is False for those).
        if match is not None and match.group(1).isprintable():
            return match.group(1)
    return None
