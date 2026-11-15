"""piv demo: a public sandbox repo with a seeded bug and a red test, set up and filed.

``piv demo`` creates OWNER/piv-demo, writes the template (demo_template/repo) into it, inits it on the free-kaggle
preset, files three issues through ``piv do``'s code (a build fix, a build that adds a rule, a docs line), and
prints each run link. ``piv demo --clean`` deletes that repo, and only when its marker file says it is the demo: the
name is fixed and the owner is the authenticated user, so nothing else is ever deleted.

Every check that can be made before the create is made before it (the login, the requests, the existence check, the
template, init's release and pin checks). init's own checks on the new repo (the Actions setting read-back, the caller
write) and the reads after the create can still fail, and that failure names the way out: ``piv demo --clean`` once the
marker file is in, and ``gh repo delete`` before that, since --clean deletes only a repo whose marker is in. A fork is
never deleted.
"""

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import do, init
from .gh import Gh, GhFailed, PivError

NAME = "piv-demo"
MARKER = ".piv-demo"
MARKER_TEXT = "piv demo sandbox: `piv demo --clean` deletes this repo\n"
DESCRIPTION = "piv demo: a sandbox with a seeded bug and a red test (piv demo --clean deletes it)"
TEMPLATE = Path(__file__).resolve().parent / "demo_template"
REPO_FILES = (
    MARKER,
    "README.md",
    "shop/__init__.py",
    "shop/cart.py",
    "shop/receipt.py",
    "tests/test_cart.py",
    "tests/test_receipt.py",
)
CHECK = "python3 -m unittest discover -s tests"
PRESET = "free-kaggle"
# The secrets init gets, and no other: the Kaggle endpoint, the Kaggle account (models-up), and the doors free-kaggle's
# tiers name (engine/route.py DOOR_SECRETS: kaggle for Qwen, groq for the Groq reviewer, google for the Gemini reviewers,
# openrouter for the last reviewer). No Claude token: free-kaggle names no Claude entry.
# piv cannot import the engine, so test_demo holds this list to the engine's table and to the preset file.
PRESET_SECRETS = (
    init.KAGGLE_URL,
    init.KAGGLE_KEY,
    "KAGGLE_USERNAME",
    "KAGGLE_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
)


@dataclass(frozen=True)
class Issue:
    line: str
    text: str


# The three issues, text only: a build fix for the seeded bug (the red test), a build that adds a rule to the receipt,
# and a docs line. No issue attaches an image: a build line's mockup footer is read by no job, so an image would be
# promised and never seen.
ISSUES = (
    Issue(
        "build",
        "Fix the discount in shop/cart.py.\n\n"
        "`python3 -m unittest discover -s tests` fails: tests/test_cart.py expects 150 for a 25% discount on 120 and 80, "
        "and the code gives 175. Change shop/cart.py only.",
    ),
    Issue(
        "build",
        "Make the receipt in shop/receipt.py draw a rule of 24 dashes above the TOTAL line. "
        "Update tests/test_receipt.py to expect the rule.",
    ),
    Issue(
        "docs",
        "Document the discount rule in README.md: one short paragraph on how the percentage is applied to the subtotal, "
        "in cents.",
    ),
)


def run(gh: Gh, env: Mapping[str, str], say: Callable[[str], None], *, clean: bool = False) -> int:
    if clean:
        return _clean(gh, say)
    _create(gh, env, say)
    return 0


def _repo(gh: Gh) -> str:
    """The demo's name: the authenticated user's login and the fixed name, so no other repo is ever its target."""
    login = gh.api_json("user").get("login")
    if not isinstance(login, str) or not init.valid_repo(f"{login}/{NAME}"):
        raise PivError("gh api user gave no usable login, so the demo repo cannot be named")
    return f"{login}/{NAME}"


def _info(gh: Gh, repo: str) -> dict | None:
    """The repo's API answer, or None when it does not exist (a 404)."""
    try:
        return gh.api_json(f"repos/{repo}")
    except GhFailed as err:
        if err.status == 404:
            return None
        raise


def _file_sha(gh: Gh, repo: str, path: str) -> str | None:
    """The blob sha of a file on the default branch, or None when it is not there (a 404)."""
    try:
        data = gh.api_json(f"repos/{repo}/contents/{path}")
    except GhFailed as err:
        if err.status == 404:
            return None
        raise
    sha = data.get("sha")
    if not isinstance(sha, str) or not init.SHA.fullmatch(sha):
        raise PivError(f"gh api repos/{repo}/contents/{path} gave no blob sha")
    return sha


def _template() -> list[tuple[str, bytes]]:
    files = []
    for path in REPO_FILES:
        source = TEMPLATE / "repo" / path
        if not source.is_file():
            raise PivError(f"the demo template is missing {path}: reinstall piv before running piv demo", 1)
        files.append((path, source.read_bytes()))
    return files


def _create(gh: Gh, env: Mapping[str, str], say: Callable[[str], None]) -> None:
    repo = _repo(gh)
    # Every read and every check comes first: a refusal here writes nothing.
    if _info(gh, repo) is not None:
        raise PivError(
            f"{repo} already exists, so nothing was written. piv demo --clean deletes it once its {MARKER} marker "
            f"is in; otherwise gh repo delete {repo} --yes does",
            2,
        )
    files = _template()
    if MARKER_TEXT.encode("utf-8") != dict(files)[MARKER]:
        raise PivError(f"the demo template's {MARKER} does not carry the demo's marker text", 1)
    # init's own reads and refusals, run here so a refusal is before the first write: its release, the caller file at
    # that release, and the pin check. The init request pins that same release, so init cannot pick a later one.
    try:
        ref, sha = init.resolve_release(gh, None, init.ENGINE_REPO)
        init.render_caller(
            gh.api_text(f"repos/{init.ENGINE_REPO}/contents/{init.CALLER_PATH}?ref={sha}"), sha, init.ENGINE_REPO
        )
    except PivError as err:
        raise PivError(f"nothing was written: {err}", err.code) from err
    issues = [do.request(repo, issue.text, line=issue.line) for issue in ISSUES]
    init_req = init.request(repo, check=CHECK, preset=PRESET, release=sha)
    say(
        f"WARNING: {repo} will be PUBLIC, and its {len(ISSUES)} issues start real Pivotal runs on {PRESET}: "
        "real model and Kaggle calls, not a dry run. piv demo --clean deletes it."
    )
    say(f"piv demo: creating {repo} (public), on {PRESET}, with release {ref}")
    try:
        gh.run("repo", "create", repo, "--public", "--add-readme", "--description", DESCRIPTION)
    except PivError as err:
        # The create may have made the repo before it failed, so the repo may exist without its marker.
        raise PivError(f"{err}. {_way_out(repo, marked=False)}", err.code) from err
    except BaseException:
        # An interrupt during the create may have made the repo too: the way out is printed, then the interrupt goes on.
        say(f"piv demo: stopped during the create. {_way_out(repo, marked=False)}")
        raise
    say(f"created {repo}")
    marked = False  # whether the marker file is in the repo: only then does piv demo --clean delete it
    try:
        branch = _default_branch(gh, repo)
        for path, data in files:
            _put(gh, repo, branch, path, data, _file_sha(gh, repo, path))
            marked = marked or path == MARKER
        say(f"wrote {len(files)} files to {branch}")
        # init reads only the names it sets from what it is given, so this gives it exactly the preset's secrets
        init.init(init_req, gh, {name: env[name] for name in PRESET_SECRETS if name in env}, say)
        links = []
        for number, req in enumerate(issues, 1):
            say(f"issue {number} of {len(issues)}: {req.title}")
            links.append((number, req.title, do.do(req, gh, say)))
    except PivError as err:
        raise PivError(f"{err}. {repo} was created and is partly set up: {_way_out(repo, marked)}", err.code) from err
    except BaseException:
        # An interrupt or a signal after the create: the repo exists, so the way out is printed before it goes on.
        say(f"piv demo: stopped. {repo} was created and is partly set up: {_way_out(repo, marked)}")
        raise
    say("run links:")
    for number, title, link in links:
        say(f"  {number} of {len(links)} ({title}): {link}")


def _way_out(repo: str, marked: bool) -> str:
    """How to remove a half-made demo. piv demo --clean deletes only a repo whose marker piv wrote, so until the marker
    is in, the way out is the gh command, named in full."""
    if marked:
        return "piv demo --clean deletes it, then run piv demo again"
    return (
        f"{repo} has no {MARKER} file, so piv demo --clean will not delete it: "
        f"gh repo delete {repo} --yes deletes it, then run piv demo again"
    )


def _default_branch(gh: Gh, repo: str) -> str:
    name = gh.api_json(f"repos/{repo}").get("default_branch")
    if not isinstance(name, str) or not name:
        raise PivError(f"gh api repos/{repo} gave no default branch")
    return name


def _put(gh: Gh, repo: str, branch: str, path: str, data: bytes, sha: str | None) -> None:
    body: dict[str, str] = {
        "message": f"Add {path} to the piv demo",
        "content": base64.b64encode(data).decode("ascii"),
        "branch": branch,
    }
    if sha is not None:  # the README that --add-readme made is replaced, so its blob sha goes with the write
        body["sha"] = sha
    gh.api(f"repos/{repo}/contents/{path}", method="PUT", body=body)


def _clean(gh: Gh, say: Callable[[str], None]) -> int:
    repo = _repo(gh)
    info = _info(gh, repo)
    if info is None:
        say(f"no piv demo repo ({repo}): nothing to delete")
        return 0
    # A fork is a copy of someone's repo, so it is never the demo, marker or not. Anything but an explicit false is refused.
    if info.get("fork") is not False:
        raise PivError(f"{repo} is a fork, or its answer does not say it is not one, so it is not the demo: not deleted", 2)
    try:
        marker = gh.api_text(f"repos/{repo}/contents/{MARKER}")
    except GhFailed as err:
        if err.status == 404:
            raise PivError(f"{repo} has no {MARKER} file, so it is not a piv demo: not deleted", 2) from err
        raise
    if marker != MARKER_TEXT:
        raise PivError(f"{repo}'s {MARKER} is not the demo's marker text, so it is not a piv demo: not deleted", 2)
    say(f"deleting {repo}: its {MARKER} marker is the demo's")
    gh.run("repo", "delete", repo, "--yes")
    say(f"deleted {repo}")
    return 0
