"""piv kaggle setup and rotate: the one endpoint key that every repo's models-up job uses.

The ngrok token and the key sit together in one private Kaggle dataset, ``<you>/pivotal-serve-secrets``,
which the serve notebook reads when it starts. The endpoint URL and the key go to each repo as secrets,
under the names init declares. Neither command pushes a kernel or starts a session: that is models-up's
job, behind the kernel status lock.

Every refusal happens before the first write. The inputs, the repo list and the dataset's state are read
and checked first. The key is generated with ``secrets``, goes to kaggle in a file that is 0600 inside a
fresh directory removed afterwards, and goes to gh on stdin. Nothing prints it: a line names it.
"""

import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from . import init
from .gh import Gh, GhFailed, PivError

DATASET = "pivotal-serve-secrets"  # the private dataset, <owner>/pivotal-serve-secrets
SERVE_FILE = "serve.json"  # what the serve notebook reads from the dataset: endpoint_url, endpoint_key, ngrok_authtoken
KAGGLE_USERNAME = "KAGGLE_USERNAME"  # the Kaggle account; kaggle's own credentials stay in the environment for it
KAGGLE_KEY = "KAGGLE_KEY"
NGROK_TOKEN = "NGROK_AUTHTOKEN"  # ngrok's own name for its token
URL_SECRET = "PIVOTAL_KAGGLE_URL"  # init's names for the repo secrets
KEY_SECRET = "PIVOTAL_KAGGLE_KEY"
KAGGLE_API_TOKEN = "KAGGLE_API_TOKEN"  # kaggle's name for an API token; an account key given as KAGGLE_KEY is not one
API_TOKEN_PREFIX = "KGAT_"  # the start of an API token: kaggle reads it only as KAGGLE_API_TOKEN (measured live)
KEY_BYTES = 32  # secrets.token_urlsafe(32): at least 32 random bytes, 43 characters
REPO_LIMIT = 1000  # gh repo list is asked for this many; a full answer is refused, never cut short

KAGGLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
ENDPOINT = re.compile(r"https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/?")
TOKEN = re.compile(r"[A-Za-z0-9_-]{20,200}")  # an ngrok authtoken: letters, digits, _ and -, never short
# What `kaggle datasets status` prints on stderr for a dataset that does not exist: the HTTP status and
# "Not Found" on its first line. Any other failure is raised, except a 403: that is checked against the listing.
NOT_FOUND = re.compile(r"(?=.*\b404\b)(?=.*not found)", re.IGNORECASE)
FORBIDDEN = re.compile(r"(?=.*\b403\b)(?=.*forbidden)", re.IGNORECASE)  # 403 and Forbidden on the first line
PRIVACY_UNVERIFIED = (
    "created without --public; confirm it shows as private in Kaggle before you rely on it: "
    "no kaggle read reports a dataset's privacy"
)
KAGGLE_PASSED = ("PATH", "HOME")  # and every KAGGLE_*; kaggle gets nothing else


class Kaggle(Gh):
    """The kaggle CLI through the same wrapper as gh: ``PIV_KAGGLE`` names the binary."""

    tool = "kaggle"
    words = 2  # a message names `datasets create`, not the flags after it


@dataclass(frozen=True)
class Request:
    owner: str  # the Kaggle account, so the dataset is <owner>/pivotal-serve-secrets
    url: str  # the endpoint, https://<the ngrok static domain>
    token: str = field(repr=False)  # the ngrok token: it goes to the dataset only, never to a repo or a message
    repos: tuple[str, ...] | None  # the repos named, or None for the repos with the pivotal topic
    owners: tuple[str, ...] = ()  # whose repos the topic listing covers; none means the signed-in user's


def request(env: Mapping[str, str], repos: list[str], *, owners: list[str] | None = None, only: bool = False,
            rotating: bool = False) -> Request:
    """The checked request. Every refusal is a usage error (exit 2), and none of them calls gh or kaggle."""
    owner = env.get(KAGGLE_USERNAME, "").strip()
    if not KAGGLE_NAME.fullmatch(owner):
        raise PivError(f"{KAGGLE_USERNAME} must be your Kaggle account name, the one in your profile URL", 2)
    url = env.get(URL_SECRET, "").strip()
    if not ENDPOINT.fullmatch(url):
        raise PivError(
            f"{URL_SECRET} must be the https address of your ngrok static domain, "
            "for example https://pivotal-serve.ngrok-free.app",
            2,
        )
    token = env.get(NGROK_TOKEN, "").strip()
    if not TOKEN.fullmatch(token):
        raise PivError(
            f"{NGROK_TOKEN} must be your ngrok authtoken: 20 or more letters, digits, _ or -, one line. "
            "It is never read back from the dataset, so every run needs it",
            2,
        )
    for repo in repos:
        if not init.valid_repo(repo):
            raise PivError("REPO must be owner/name, for example 0xctx/piv-sandbox", 2)
    for name in owners or []:
        if not init.OWNER.fullmatch(name):
            raise PivError("--owner must be a GitHub user or organisation name", 2)
    if rotating and repos and not only:
        raise PivError(
            "rotate with named repos leaves every other repo on the old key: add --only to accept that, "
            "or name none to rotate every repo with the pivotal topic",
            2,
        )
    if only and not repos:
        raise PivError("--only needs the repos to rotate named", 2)
    named = tuple(dict.fromkeys(repos)) if repos else None
    if named and owners and not only and not rotating:
        raise PivError("--owner lists repos; name repos or give --owner, not both", 2)
    return Request(owner, url.rstrip("/"), token, named, tuple(dict.fromkeys(owners or [])))


def gh_environment(env: Mapping[str, str]) -> dict[str, str]:
    """gh's environment: init's secrets removed, and the ngrok token and the Kaggle API token too: gh needs neither."""
    return {key: value for key, value in init.gh_environment(env).items() if key not in (NGROK_TOKEN, KAGGLE_API_TOKEN)}


def kaggle_environment(env: Mapping[str, str]) -> dict[str, str]:
    """kaggle's environment: PATH, HOME and its own KAGGLE_* variables, and nothing else (no GitHub token). An API
    token given as KAGGLE_KEY is also KAGGLE_API_TOKEN, the name kaggle honours (engine/modelsup.py does the same)."""
    kept = {key: value for key, value in env.items() if key in KAGGLE_PASSED or key.startswith("KAGGLE_")}
    if kept.get(KAGGLE_KEY, "").startswith(API_TOKEN_PREFIX):
        kept[KAGGLE_API_TOKEN] = kept[KAGGLE_KEY]
    return kept


def managed_repos(gh: Gh, owners: tuple[str, ...] = ()) -> list[str]:
    """The repos that carry the pivotal topic: the signed-in user's, or each --owner's. Read-only: `gh repo list`."""
    names: list[str] = []
    for owner in owners or ("",):
        names += _topic_repos(gh, owner)
    return list(dict.fromkeys(names))


def _topic_repos(gh: Gh, owner: str) -> list[str]:
    who = [owner] if owner else []
    answer = gh.run("repo", "list", *who, "--topic", init.TOPIC, "--json", "nameWithOwner", "--limit", str(REPO_LIMIT))
    try:
        rows = json.loads(answer)
    except ValueError:
        raise PivError("gh repo list did not answer with JSON") from None
    if not isinstance(rows, list):
        raise PivError("gh repo list did not answer with a list")
    names = []
    for row in rows:
        name = row.get("nameWithOwner") if isinstance(row, dict) else None
        if not isinstance(name, str) or not init.valid_repo(name):
            raise PivError("gh repo list gave a repository name that is not owner/name")
        names.append(name)
    if len(names) >= REPO_LIMIT:
        raise PivError(f"{REPO_LIMIT} or more repos carry the pivotal topic: name the repos to set the secrets on")
    return names


def dataset_exists(kaggle: Gh, ref: str) -> bool:
    """Read-only: `kaggle datasets status`. A failure whose first line says 404 and Not Found means no dataset, and a
    403 means none only when the account's own listing lacks it. Any other failure (a bad credential, a timeout, a
    message not recognised) is raised."""
    try:
        kaggle.run("datasets", "status", ref)
    except GhFailed as err:
        if NOT_FOUND.search(str(err)):
            return False
        if FORBIDDEN.search(str(err)):
            # Measured live (kaggle 2.2.4): a dataset that does not exist answers 403, not 404. A 403 counts as
            # absent only when the account's own listing, a second read, does not show the dataset.
            return _listed(kaggle, ref)
        raise
    return True


def _listed(kaggle: Gh, ref: str) -> bool:
    """True when `kaggle datasets list --mine` names the dataset. A failed listing is raised: absent is never a guess."""
    out = kaggle.run("datasets", "list", "--mine", "--csv", "-s", ref.split("/", 1)[1])
    return any(line.split(",", 1)[0].strip() == ref for line in str(out).splitlines())


def setup(req: Request, gh: Gh, kaggle: Gh, say: Callable[[str], None]) -> None:
    """The repo secrets first, the dataset last. Until the dataset exists nothing is finished, and running
    setup again is the recovery: it makes a new key and sets every secret again."""
    ref = f"{req.owner}/{DATASET}"
    if dataset_exists(kaggle, ref):
        # Setup never replaces a key silently: that is rotate's job, and it says so.
        raise PivError(
            f"{ref} already exists, so setup ran before. piv kaggle rotate replaces its key and the repo secrets",
            2,
        )
    repos = _targets(req, gh)
    _reachable(gh, repos)
    key = secrets.token_urlsafe(KEY_BYTES)
    if not repos:
        say("no repo secrets set: none was named, and no repo of yours carries the pivotal topic")
    for repo in repos:
        failed = _set_secrets(gh, repo, req.url, key)
        if failed:
            say(f"{repo}: not fully set ({failed})")
            raise PivError(
                f"{repo} did not get the endpoint values, so the dataset was not created. "
                "Nothing is lost: run piv kaggle setup again",
                1,
            )
        say(f"{repo}: secrets {URL_SECRET} and {KEY_SECRET} set")
    try:
        _publish(kaggle, ref, req, key, create=True)
    except PivError as err:
        raise PivError(f"{err}. The repo secrets are set but unused: run piv kaggle setup again", err.code) from None
    say(f"kaggle dataset {ref}: {PRIVACY_UNVERIFIED}. {NGROK_TOKEN} and {KEY_SECRET} are stored in it, not shown")
    say(f"next: confirm {ref} is private in Kaggle; {URL_SECRET} and {KEY_SECRET} are the secret names models-up reads")


def rotate(req: Request, gh: Gh, kaggle: Gh, say: Callable[[str], None]) -> None:
    ref = f"{req.owner}/{DATASET}"
    if not dataset_exists(kaggle, ref):
        raise PivError(f"{ref} does not exist: piv kaggle setup creates it first", 2)
    known = managed_repos(gh, req.owners)
    repos = list(req.repos) if req.repos is not None else known
    _reachable(gh, repos)  # every target answers before the dataset is rewritten
    key = secrets.token_urlsafe(KEY_BYTES)
    _publish(kaggle, ref, req, key, create=False)
    say(f"kaggle dataset {ref}: new version, {PRIVACY_UNVERIFIED}; the old versions are deleted. The new key is not shown")
    updated, failed = [], []
    for repo in repos:
        problem = _set_secrets(gh, repo, req.url, key)
        if problem:
            say(f"{repo}: not fully set ({problem})")
            failed.append(repo)
        else:
            say(f"{repo}: secrets {URL_SECRET} and {KEY_SECRET} set")
            updated.append(repo)
    if not req.owners:
        say("the topic listing covers only the repos of the signed-in GitHub user: for an organisation's repos, "
            "run rotate again with --owner ORG")
    stranded = failed + [repo for repo in known if repo not in updated and repo not in failed]
    if stranded:
        say(f"dataset has the new key; these repos still hold the OLD key: {', '.join(stranded)}")
    else:
        say(f"dataset and all {len(updated)} repo(s) known by the pivotal topic hold the new key")
    if failed:
        raise PivError(f"{len(failed)} repo(s) did not get the new key: {', '.join(failed)}. Fix them and rotate again", 1)


def _targets(req: Request, gh: Gh) -> list[str]:
    """The repos to set the secrets on: the ones named, or the repos with the pivotal topic."""
    return list(req.repos) if req.repos is not None else managed_repos(gh, req.owners)


def _reachable(gh: Gh, repos: list[str]) -> None:
    """Read-only, before the first write: `gh secret list` answers for every target, or none is touched."""
    unreachable = []
    for repo in repos:
        try:
            gh.run("secret", "list", "--repo", repo)
        except PivError:
            unreachable.append(repo)
    if unreachable:
        raise PivError(f"cannot reach the secrets of {', '.join(unreachable)}: nothing was written", 1)


def _publish(kaggle: Gh, ref: str, req: Request, key: str, *, create: bool) -> None:
    """Write the dataset: a new one, or a new version that deletes the old versions, so the old key leaves Kaggle."""
    with tempfile.TemporaryDirectory() as folder:
        _write(folder, "dataset-metadata.json", json.dumps({
            "id": ref,
            "title": "Pivotal serve secrets",
            "licenses": [{"name": "CC0-1.0"}],
        }))
        _write(folder, SERVE_FILE, json.dumps({
            "endpoint_url": req.url,
            "endpoint_key": key,
            "ngrok_authtoken": req.token,
        }))
        # Private by default: no -u. No detail is echoed, since a failure line could repeat the file's content.
        if create:
            kaggle.run("datasets", "create", "-p", folder, "-q", echo_stderr=False)
        else:
            version = ["datasets", "version", "-p", folder, "-m", "Rotate the endpoint key", "-d", "-q"]
            kaggle.run(*version, echo_stderr=False)


def _write(folder: str, name: str, text: str) -> None:
    # Created 0600 from the start, so the file never exists with a wider mode.
    fd = os.open(os.path.join(folder, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _set_secrets(gh: Gh, repo: str, url: str, key: str) -> str:
    """The endpoint URL and key for one repo. Empty when both are set, else what failed (never a value)."""
    try:
        gh.run("secret", "set", URL_SECRET, "--repo", repo, stdin=url, echo_stderr=False)
        gh.run("secret", "set", KEY_SECRET, "--repo", repo, stdin=key, echo_stderr=False)
    except PivError as err:
        return str(err)
    return ""
