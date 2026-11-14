"""The land job (F1, F4, F5, F6): "land it" re-verifies the seal, then moves the base by the sealed commit only.

This job MOVES THE BASE BRANCH ON A HUMAN'S COMMAND, so every check fails closed and happens before the first write (`refused: ...`,
exit 1). It holds the repo token (`contents`, `pull-requests`, `issues: write`; `actions` and `checks: read`) and runs only git and
gh on data it validated: no repo code, no model output, no checkout of the repo. The engine pattern of engine/publish.py: run as
`exec python3 -I pivotal/engine/land.py run`, non-dumpable first, the step's environment taken into a dict and `os.environ` emptied,
every child (`gh`, `/usr/bin/git`) handed a fixed environment with only the token that call needs.

THE STEPS (each is a function; the order is the plan's)

1. MAP THE TRIGGER (`resolve`). The caller's `trust` job decided before this job existed; this job asks `trust.decide` AGAIN on the
   event the caller passed on (`GITHUB_EVENT_PATH`), so a call that skipped the caller still lands nothing for a stranger. It must
   come out `trusted`, kind `land`, for the issue number the job was given. The level must not be `watch` (below). ``ISSUE`` is the
   issue: for a comment on a pull request, the issue the pull request closes (trust resolves it; the group is that issue's). The
   pull request's own number is the event's. If the comment is on a pull request, it must be OPEN, not a draft, from this
   repository, based on the default branch, from branch `pivotal/issue-<n>/stack`, and its body must say `Closes #<n>` on one
   line, exactly once, and that `n` must be ISSUE. If it is an issue, the open pull request on `pivotal/issue-<n>/stack` is the
   one (exactly one; a draft, none, two or a fork's is a refusal) and its body must say `Closes #<n>` too. Nothing here is a
   comment: a refusal is the log and a red job.
2. VERIFY THE SEAL (`find_seal`, `engine/seal.py`). The sealed commit is the PR's head. The `pivotal/gate` check runs on it are read
   with `filter=all` (the default `latest` would let a newer check run of that name from another workflow hide the genuine one),
   paginated with `gh api --paginate --slurp`, unwrapped from `{"total_count", "check_runs"}` and refused unless the listing has
   exactly `total_count` items (a truncated listing is a refusal, never a smaller truth). Each candidate `external_id` (a run id)
   is checked in turn: the run (`repos/<repo>/actions/runs/<id>`), its artifacts (paginated and unwrapped the same way, exactly one
   `land` and one `gate-<id>` per slice of the prefix, not expired, of THAT run, within a size cap), then the artifacts are
   downloaded with `gh run download <id> --name <artifact> --dir <fresh dir>` into this job's work directory (one artifact per
   call, so each lands directly in its own directory), the directories are checked to hold only regular files of the expected
   names, and `seal.verify` decides (app `github-actions`, workflow path `.github/workflows/pivotal.yml`, the run's check suite,
   land.json and every gate confirming the commit, the sha256 of land.json in the seal). A check run from another app or workflow,
   for another SHA, with another conclusion, or naming another run is ignored or fails its own candidate; the job refuses only
   when NO candidate verifies. seal.py's docstring says what its check-suite binding does NOT prove (who created the seal); this
   job checks the artifacts and the run beside it, but THAT DOES NOT ESTABLISH WHO CREATED THE SEAL: a second pivotal.yml run with
   a suite of its own and the same artifacts would pass every check here (test_land p3, a documented limitation). What keeps it out is
   the invariant that no job that runs repo code or model output holds `checks: write` (test_factory pins it; only the publish job
   does). A dedicated GitHub App slug is the operator's open decision. Land.json is hashed and compared with the seal's claim BEFORE it is
   parsed. The `land` artifact is the zip of land.json and the bundle; the job never reads the bundle, so an artifact over
   LAND_ARTIFACT_BYTES (64 MiB) is refused before it is downloaded. Then the commits are fetched from the repository
   (`refs/heads/pivotal/issue-<n>/stack`) into a scratch repository, and `publish.verify_branch` proves that the head is the
   sealed commit and that the chain base..head is exactly the commits of land.json up to it, in a straight line, none in `.github`.
3. THE BASE (`move`). The default branch's tip is read (`git ls-remote`). It must be `land.json`'s `base_sha` (the base the stack was
   built and gated on). Anything else is a MOVED base: nothing is pushed, a `repository_dispatch` of type `pivotal-fix` is sent
   (see "Re-gate" below) and the issue gets a comment that says so. Already at the sealed commit, or containing it, is landed
   already: no push. A base that is not `base_sha` but HOLDS the sealed commit (as the commit, on its first-parent chain, or as the second
   parent of a merge with the sealed tree) is landed already and is judged by step 5, not a re-gate.
4. THE PUSH (`move`). One plain `git push <sealed sha>:refs/heads/<default>` (`publish.push_args`: no `--force`, `+`, `--mirror`, `--delete`),
   at most once. If git refuses it, the branch is read again: at the sealed commit (the answer was lost), landed; moved (not at
   `base_sha`), a moved base as in 3; unchanged, and only then: `GET repos/<repo>/branches/<default>` (`is_protected`). A refused push
   is NOT evidence of protection (it may be transient), so unless the answer's `protected` is exactly true, the outcome is `rejected` and
   the operator decides. On a protected branch the pull request is merged through the API:
   `gh api --method PUT repos/<repo>/pulls/<n>/merge --raw-field sha=<sealed sha> --raw-field merge_method=merge`.
   `sha` pins the head: GitHub refuses (409) a pull request whose head moved (`--raw-field`: a sha of digits only must stay a string).
   `merge` (not squash or rebase) is the one method whose result CONTAINS the sealed commit. The merge commit is a commit no
   gate saw, so step 5 checks it (the answer's `sha`).
5. RE-READ (`check_base`). The default branch is FETCHED again into a ref of the scratch repository. Landed means: the base is the sealed
   commit, or holds it on its first-parent chain (somebody else's later work on top is fine), or holds it as the second parent of a merge
   whose tree IS the sealed tree; for the merge the API made, the commit its answer names must be on the base with parents exactly
   [the base read just before the merge, the sealed commit] and the sealed commit's tree. A base that holds the commit in any other way,
   or whose merge is not that commit, is `moved`: never "Landed". A merge answered as merged with no usable commit is `unverified`.
   Only if landed: a comment "Landed ...", the status label `pivotal:landed` instead of `pivotal:ready` (a failed removal of
   `pivotal:ready` is loud unless the issue no longer has it), and the issue closed (`PATCH repos/<repo>/issues/<n>` with `state=closed`).
   Otherwise the issue is left open and `ready`, and one comment says why, in fixed words: `rejected` (push and merge refused), `unprotected` (push refused, no
   evidence of protection, so no merge was tried), `queued` (the merge was accepted but the base does not contain the commit: a merge queue), `unknown` (the
   base could not be read), `missing` (a push that succeeded and a base without the commit), `moved` or `unverified` (above). The job is
   red for those. gh's own error text is never repeated anywhere (only the exit status is read; the one JSON read from a merge answer
   is `sha` and `merged`).

THE LEVEL. `ask`, `prepare` and `land` all let a HUMAN's "land it" land a sealed stack (at `ask` and `prepare` "you land it";
at `land` the pipeline lands by itself and a human command is still fine: publish's ready stack PR after a refused push is exactly
what this job lands). `watch` builds nothing, so there is nothing to land: refused. `job.level_allows` knows the kinds the
factory starts, and `land` is not one of them, so the rule is `job.may_land_by_hand` (every level that starts work). The seal
is what makes it safe: the land job lands nothing without a verified seal (`no_seal`, F4). Publish seals a full, trusted, READY
stack PR at `ask`, `prepare` and `land` when the run's effective level is not held (`engine/publish.py`, `Settings.may_seal`), so a
human's "land it" can land a stack sealed at `ask` or `prepare` as well as one the pipeline sealed at `land`. `watch` starts
nothing, so it seals nothing.

RE-GATE. "Re-dispatch the factory (a re-gate)" uses the caller's dispatch mechanism: `repository_dispatch`, type `pivotal-fix`,
built by `fix.payload`, `fix.request` and `fix.dispatch_argv` as publish builds a fix round: the keys of `trust.FIX_PIN_FIELDS`
(issue, slice, round, run_id, branch, head_sha, issue_pin) and a fixed `findings` text. Its `issue_pin` is round 0's pin, which the
land artifact must carry (`carried_pin`; assemble writes no such key yet). Until it does, the pin is "" and NOTHING is dispatched:
the comment says the pin is missing and the job is RED (R16). The pin is never read from the issue's live title and body: the
receiver would then take an edited text's pin for round 0's. The dispatch's `branch` is the stack branch, which trust refuses
(`trust.fix_payload_problem`: the branch must be the issue's own slice draft branch, `pivotal/issue-<n>/<slice>`), so a sent re-gate
starts no run; that is not fixed here (see the report). `GITHUB_TOKEN` may trigger `repository_dispatch`. NOT VERIFIED:
that GitHub delivers a token-made dispatch at all. The job stays RED (`land_outcome=regate`, exit 1) whatever the dispatch did, so no
run claims a re-gate that nobody makes. When the pin is carried and the branch is accepted, update this paragraph, the comment and
the exit status together.

CONCURRENCY. land.yml serializes land jobs per repository (`cancel-in-progress: false`) and the push is a fast-forward from a base
that was read just before, so two lands cannot both pass; the second finds the base moved and re-gates.

Run with: GH_TOKEN, GH_REPO, ISSUE, LEVEL (PIVOTAL_LEVEL), WORK_DIR (must not exist), GITHUB_OUTPUT, GITHUB_EVENT_NAME,
GITHUB_EVENT_PATH, GITHUB_SERVER_URL (the runner sets it), optionally PIV_GH (the tests' seam: an absolute path to a program
named `gh`; the workflow never sets it). Outputs: `land_outcome` (`landed`, `regate` or `not_landed`) and `sealed_sha`.

NOT VERIFIED (nothing here has run on real GitHub): that `app.slug`, the run `path`, `check_suite.id` and `check_suite_id` are
reported as seal.py reads them, that a check run created by a workflow job sits in that run's check suite, that
`gh run download --name <n> --dir <d>` extracts one artifact directly into `<d>`, that `--paginate --slurp` and `total_count`
behave as read here on these endpoints, how branch protection and a merge queue answer a push and the merge API, and that the
merge endpoint accepts `merge_method=merge` on the repo. Each mismatch is a refusal or a "not landed", never a landing.
"""

import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

if __package__ in (None, ""):  # run as a script under python3 -I, which puts no directory on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import assemble, buildjob, fix, gate, job, publish, seal, trust  # noqa: E402
from engine.job import JobError  # noqa: E402

CHECK_QUERY = "check_name=pivotal%2Fgate&filter=all&per_page=100"
ARTIFACT_QUERY = "per_page=100"
# The `land` artifact is the zip of land.json AND the bundle, and `gh run download` cannot take one file of an artifact. This job
# reads only land.json, so it refuses an artifact whose zip is larger than this instead of downloading up to the bundle cap
# (1 GiB) onto the runner. A stack with a bigger bundle is refused (artifact_size); splitting the artifact is the real fix.
# THE EXTRACTION IS NOT BOUNDED: the cap is on the COMPRESSED size the API reports (`size_in_bytes`), and `gh run download`
# unzips on the runner with no limit on what comes out, so a zip bomb of 64 MiB could fill the runner's disk. The artifact is
# made by the pivotal.yml run that the seal names, on the default branch (seal.check_run_identity), so its maker is trusted code,
# not a stranger; a failed job on a full disk lands nothing (every check is before the first write). Accepted and recorded; a
# bound would need the zip fetched with `gh api` and read with zipfile.
LAND_ARTIFACT_BYTES = 64 * 1024 * 1024
GATE_ARTIFACT_BYTES = 64 * 1024
ARTIFACT_RETENTION_DAYS = 7  # factory.yml `retention-days` of the land and gate artifacts: an older stack cannot be landed
LAND_ARTIFACT = "land"  # the artifact's name; land.json is the file in it
# `gh run download` of an artifact of up to LAND_ARTIFACT_BYTES takes longer than job.GH_TIMEOUT_SECONDS (30 s, right for an API
# call): only the download calls get this one.
DOWNLOAD_TIMEOUT_SECONDS = 300
MERGE_METHOD = "merge"
FINDINGS = "The base moved after the stack was sealed. Re-gate the stack on the new base."
REGATE_NOT_SENT = {  # the comment's reason when no dispatch was sent; the job is red either way
    "pin": "The re-gate was not dispatched: the land artifact carries no pin of the issue text that round 0 was trusted on, and the "
           "factory refuses a fix round without one. Re-run the issue with /pivotal go trusted.",
    "dispatch": "The dispatch to re-gate the stack failed: re-run the issue with /pivotal go trusted.",
    "unknown": "The dispatch to re-gate the stack was made and GitHub's answer is not known, so a re-gate may have been started: look at "
               "the Actions tab before you re-run the issue with /pivotal go trusted.",
}
OUTCOMES = ("landed", "regate", "not_landed")
WHY_NOT = {
    "rejected": "the push was refused and so was the merge through the API (branch protection, a required review or required checks)",
    "unprotected": "the push was refused and the branch is not shown to be protected, so no merge was tried through the API (the refusal may be transient)",
    "queued": "the merge was accepted, but the base does not contain the commit yet (is it in a merge queue?)",
    "unknown": "the base could not be read again, so nobody can say whether it moved",
    "missing": "the push was accepted, yet the base does not contain the commit",
    "moved": "the base moved while the merge was made, or holds a commit no gate saw: the merge is not the sealed commit joined to the base that was read. "
             "THE BASE MAY HOLD AN UNREVIEWED MERGE COMMIT: look at it, then revert it or re-gate the stack on it",
    "unverified": "the merge was accepted, but its answer named no commit that could be checked",
}


@dataclass
class Settings:
    repo: str
    number: int
    level: str
    server: str
    work_dir: Path
    output: str
    token: str
    event_name: str
    event_path: str
    gh: str = "gh"


@dataclass
class Pr:
    number: int
    state: str
    draft: bool
    merged: bool
    body: str
    head_ref: str
    head_sha: str
    head_repo: str
    base_ref: str
    base_repo: str


@dataclass
class Sealed:
    """What a verified seal gives: the sha it is on, the run that made it, land.json and the slice ids of the prefix."""
    sha: str
    run_id: int
    land: dict
    prefix: list[str]


class Hub:
    """gh with a fixed environment and the one token, over validated arguments."""

    def __init__(self, s: Settings, env: Mapping[str, str]):
        self.s, self.env = s, dict(env)

    def json(self, *args: str) -> object:
        return job.gh_json(self.env, *args, binary=self.s.gh)

    def run(self, *args: str, timeout: int = job.GH_TIMEOUT_SECONDS) -> str:
        return job.gh(self.env, *args, binary=self.s.gh, timeout=timeout)

    def pages(self, path: str, key: str) -> list:
        """Every item of a paginated, wrapped listing (``{"total_count": n, key: [...]}``): ``--paginate --slurp`` gives one
        outer array of pages. Refused unless the items number exactly ``total_count`` (and fewer than seal.MAX_ITEMS)."""
        data = self.json("api", "--paginate", "--slurp", path)
        if not isinstance(data, list) or not data:
            raise JobError(f"{key}: the listing is not a list of pages")
        items: list = []
        total = None
        for page in data:
            count = page.get("total_count") if isinstance(page, dict) else None
            part = page.get(key) if isinstance(page, dict) else None
            if type(count) is not int or count < 0 or not isinstance(part, list) or (total is not None and count != total):
                raise JobError(f"{key}: a page of the listing is not of the expected shape")
            total = count
            items.extend(part)
        if len(items) != total or len(items) >= seal.MAX_ITEMS:
            raise JobError(f"{key}: the listing is truncated or too long to trust")
        return items


# ------------------------------------------------------------------------------------------------ settings


def read_settings(env: Mapping[str, str]) -> Settings:
    repo = job.read_repo(env)
    number = job.read_number(env.get("ISSUE", ""), "ISSUE")
    level = job.read_level(env.get("LEVEL", ""))
    token = env.get("GH_TOKEN", "")
    if publish.TOKEN.fullmatch(token) is None:
        raise JobError("GH_TOKEN is not set or is not printable ASCII up to 4096 characters")
    server = env.get("GITHUB_SERVER_URL") or publish.DEFAULT_SERVER
    if publish.SERVER.fullmatch(server) is None:
        raise JobError("GITHUB_SERVER_URL is not an https URL")
    work_dir = publish._path(env, "WORK_DIR")
    if os.path.lexists(work_dir):
        raise JobError("WORK_DIR already exists")
    binary = env.get("PIV_GH", "gh")  # the test seam of every gh caller; the workflow never sets it
    if binary != "gh" and (not os.path.isabs(binary) or "\0" in binary or os.path.basename(binary) != "gh"):
        raise JobError("PIV_GH must be an absolute path to a program named gh")
    event_path = env.get("GITHUB_EVENT_PATH", "")
    if not event_path or "\0" in event_path:
        raise JobError("GITHUB_EVENT_PATH is not set")
    return Settings(repo, number, level, server.rstrip("/"), work_dir, job.read_output_path(env), token,
                    env.get("GITHUB_EVENT_NAME", ""), event_path, binary)


def gh_env(s: Settings, home: Path) -> dict[str, str]:
    """The environment of every gh call, and of the trust lookup (which runs the program named ``gh`` found on its PATH)."""
    env = {
        "PATH": publish.SAFE_PATH, "HOME": str(home), "GH_CONFIG_DIR": str(home / "gh"), "GH_TOKEN": s.token,
        "GH_REPO": s.repo, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1",
    }
    host = s.server.removeprefix("https://")
    if s.server.startswith("https://") and host != "github.com":
        env["GH_HOST"] = host
    return env


# ------------------------------------------------------------------------------------------------ 1. the trigger


def check_trust(s: Settings, env: Mapping[str, str]) -> None:
    """The caller's trust decision, asked again on the event it passed on."""
    lookup = dict(env)
    if s.gh != "gh":
        lookup["PATH"] = f"{os.path.dirname(s.gh)}:{publish.SAFE_PATH}"
    decision = trust.decide(s.event_name, trust._read_event(s.event_path), s.repo, env=lookup)
    if not (decision.trusted and decision.kind == "land" and decision.issue == s.number):
        raise JobError(f"not a trusted /pivotal land for #{s.number}: {decision.reason}")
    print(f"trust: {decision.reason}")


def read_pr(hub: Hub, s: Settings, number: int) -> Pr:
    data = hub.json("api", f"repos/{s.repo}/pulls/{number}")

    def dig(*path: str) -> object:
        cursor: object = data
        for key in path:
            cursor = cursor.get(key) if isinstance(cursor, dict) else None
        return cursor

    fields = {
        "state": dig("state"), "draft": dig("draft"), "merged": dig("merged"), "body": dig("body") or "",
        "head_ref": dig("head", "ref"), "head_sha": dig("head", "sha"), "head_repo": dig("head", "repo", "full_name"),
        "base_ref": dig("base", "ref"), "base_repo": dig("base", "repo", "full_name"),
    }
    if (
        not isinstance(data, dict) or dig("number") != number or type(number) is not int
        or not all(isinstance(fields[k], str) for k in ("state", "body", "head_ref", "head_sha", "head_repo", "base_ref", "base_repo"))
        or not isinstance(fields["draft"], bool) or not isinstance(fields["merged"], bool)
        or job.SHA.fullmatch(fields["head_sha"]) is None
    ):
        raise JobError(f"#{number} is not a pull request of the shape the REST API answers")
    return Pr(number, **fields)


def check_pr(s: Settings, pr: Pr, default: str) -> int:
    """The issue the pull request closes, if it is the OPEN, READY stack pull request of that issue (else JobError)."""
    closes = trust.CLOSES.findall(pr.body)
    if len(closes) != 1:
        raise JobError(f"pull request #{pr.number} must say Closes #<issue> on one line, exactly once")
    issue = int(closes[0])
    if pr.head_ref != assemble.stack_branch(issue):
        raise JobError(f"pull request #{pr.number} is not from {assemble.stack_branch(issue)}: only a stack pull request lands")
    if pr.head_repo != s.repo or pr.base_repo != s.repo:
        raise JobError(f"pull request #{pr.number} is not from and into this repository")
    if pr.base_ref != default:
        raise JobError(f"pull request #{pr.number} is not based on the default branch")
    if pr.state != "open" or pr.merged:
        raise JobError(f"pull request #{pr.number} is not open")
    if pr.draft:
        raise JobError(f"pull request #{pr.number} is a draft: a draft is never landable")
    return issue


def resolve(hub: Hub, s: Settings, default: str) -> tuple[int, Pr]:
    """(issue, the ready stack pull request). A comment on a pull request is the pull request's (its number is the event's, and
    ISSUE, the issue trust resolved from its Closes line, must be the issue it closes); a comment on an issue is the issue's."""
    event = trust._read_event(s.event_path)
    if trust._dig(event, "issue", "pull_request") is not None:
        number = trust._number(trust._dig(event, "issue", "number"))
        if number is None:
            raise JobError("the pull request of the comment has no number")
        pr = read_pr(hub, s, number)
        issue = check_pr(s, pr, default)
        if issue != s.number:
            raise JobError(f"pull request #{number} closes #{issue}, not the issue #{s.number} the job was given")
        return issue, pr
    data = hub.json("api", f"repos/{s.repo}/issues/{s.number}")
    if not isinstance(data, dict) or data.get("number") != s.number:
        raise JobError(f"#{s.number} is not an issue or pull request of {s.repo}")
    if "pull_request" in data:
        raise JobError(f"#{s.number} is a pull request, but the comment's event is not")
    branch = assemble.stack_branch(s.number)
    listed = [x for x in publish.list_prs(hub.env, s, branch) if x["state"] == "OPEN"]  # type: ignore[arg-type]
    if len(listed) != 1:
        raise JobError(f"issue #{s.number} has {len(listed)} open pull requests on {branch}: land needs exactly one")
    pr = read_pr(hub, s, listed[0]["number"])
    issue = check_pr(s, pr, default)
    if issue != s.number:
        raise JobError(f"the pull request on {branch} closes another issue")
    return issue, pr


# ------------------------------------------------------------------------------------------------ 2. the seal


def listed(items: Sequence, name: str, run_id: int, cap: int) -> None:
    """Exactly one artifact of this name, not expired, made by this run, within the size cap: checked BEFORE it is downloaded."""
    found = [a for a in items if isinstance(a, dict) and a.get("name") == name]
    if len(found) != 1:
        raise seal.SealError("artifact_missing")
    a = found[0]
    run, size = a.get("workflow_run"), a.get("size_in_bytes")
    if not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != run_id:
        raise seal.SealError("artifact_other_run")
    if a.get("expired") is True:
        raise seal.SealError("artifact_expired")  # the artifacts live ARTIFACT_RETENTION_DAYS days (factory.yml): re-run the factory
    if a.get("expired") is not False:
        raise seal.SealError("artifact_other_run")
    if type(size) is not int or not 0 <= size <= cap:
        raise seal.SealError("artifact_size")


def download(hub: Hub, s: Settings, run_id: int, name: str, dest: Path, allowed: frozenset[str]) -> None:
    """One artifact of one run into a fresh directory, then checked: only regular files, only the allowed names."""
    dest.mkdir(parents=True, mode=0o700)
    hub.run("run", "download", str(run_id), "--repo", s.repo, "--name", name, "--dir", str(dest), timeout=DOWNLOAD_TIMEOUT_SECONDS)
    try:
        names = os.listdir(dest)
        modes = [os.lstat(dest / n).st_mode for n in names]
    except OSError:
        raise seal.SealError("download_unreadable") from None
    if not set(names) <= allowed or not all(stat.S_ISREG(m) for m in modes):
        raise seal.SealError("download_shape")


def verify_candidate(hub: Hub, s: Settings, default: str, sha: str, external: str, check_runs: list, work: Path) -> Sealed:
    """One candidate seal, end to end. Raises seal.SealError (a word) or JobError (a gh failure)."""
    run_id = int(external)
    run = hub.json("api", f"repos/{s.repo}/actions/runs/{run_id}")
    # Before any artifact is listed or downloaded: the run is this repository's pivotal.yml on the DEFAULT branch, and one of the
    # check runs sits in its check suite. A run of a feature branch or another workflow costs nothing (no download).
    seal.check_run_identity(run, s.repo, run_id, default)
    own = [item for item in check_runs if seal.qualifies(item, sha) == external]
    if not any(seal.same_suite(item, run) for item in own):
        raise seal.SealError("suite_differs")
    artifacts = hub.pages(f"repos/{s.repo}/actions/runs/{run_id}/artifacts?{ARTIFACT_QUERY}", "artifacts")
    listed(artifacts, LAND_ARTIFACT, run_id, LAND_ARTIFACT_BYTES)
    base = work / f"run-{run_id}"
    land_dir, gates_dir = base / "land", base / "gates"
    download(hub, s, run_id, LAND_ARTIFACT, land_dir, frozenset({assemble.LAND, assemble.BUNDLE}))
    # seal.verify looks at the FIRST check run that names this run and sits in its suite. One check run at a time is handed
    # to it, so a lookalike of that name and id (same run, same suite, another summary) listed before the genuine one cannot
    # shadow it: the seal is the first that verifies, the others are ignored.
    try:  # bind before parse: land.json is hashed and compared with a seal's claim before any of it is read as JSON
        raw = assemble.read_regular(land_dir / assemble.LAND, assemble.MAX_LAND_BYTES)
    except assemble.ArtifactError:
        raise seal.SealError("land_invalid") from None
    if hashlib.sha256(raw).hexdigest() not in {h for item in own for h in seal.claimed_hashes(item)}:
        raise seal.SealError("land_differs")
    try:
        land = assemble.read_land(land_dir / assemble.LAND)
    except assemble.LandError as err:
        print(f"land.json: {err}", file=sys.stderr)
        raise seal.SealError("land_invalid") from None
    heads = [next(x for x in land["slices"] if x["id"] == ident)["head"] for ident in land["positions"]]
    if sha not in heads:
        raise seal.SealError("sha_not_on_stack")
    gates_dir.mkdir(mode=0o700)
    for ident in land["positions"][: heads.index(sha) + 1]:
        listed(artifacts, f"gate-{ident}", run_id, GATE_ARTIFACT_BYTES)
        download(hub, s, run_id, f"gate-{ident}", gates_dir / f"gate-{ident}", frozenset({gate.GATE}))
    failure = seal.SealError("no_seal")
    for item in own:
        try:
            prefix = seal.verify(repo=s.repo, default=default, sha=sha, external_id=external, check_runs=[item], run=run, artifacts=artifacts,
                                 land_dir=land_dir, gates_dir=gates_dir)
        except seal.SealError as err:
            failure = err
            continue
        return Sealed(sha, run_id, land, prefix)
    raise failure


def find_seal(hub: Hub, s: Settings, default: str, issue: int, sha: str, work: Path) -> Sealed:
    """The seal of ``sha``, verified, or a JobError naming the words each candidate failed with."""
    check_runs = hub.pages(f"repos/{s.repo}/commits/{sha}/check-runs?{CHECK_QUERY}", "check_runs")
    try:
        candidates = seal.candidates(check_runs, sha)
    except seal.SealError as err:
        raise JobError(f"the check runs of {sha[:12]} cannot be read: {err}") from None
    if not candidates:
        raise JobError(f"no pivotal/gate seal on {sha[:12]}: nothing sealed this commit (a check run of that name from another app or run is ignored)")
    failed = []
    for external in candidates:
        try:
            found = verify_candidate(hub, s, default, sha, external, check_runs, work)
        except (seal.SealError, JobError) as err:
            failed.append(f"run {external}: {err}")
            continue
        if found.land["issue"] != issue:
            failed.append(f"run {external}: land_other_issue")
            continue
        return found
    raise JobError("no seal on " + sha[:12] + " verifies (" + "; ".join(failed) + ")")


# ------------------------------------------------------------------------------------------------ 3. to 5. the base


def fetch_stack(s: Settings, work: Path, found: Sealed) -> publish.Remote:
    """A scratch repository with the stack branch as the repository has it, proven to be the sealed prefix and nothing else."""
    land = found.land
    git = publish.Remote(work / "git", land["base_sha"], s.server, s.repo, s.token)
    (work / "git").mkdir(mode=0o700)
    branch = assemble.stack_branch(land["issue"])
    commits = [c for ident in found.prefix for c in next(x for x in land["slices"] if x["id"] == ident)["commits"]]
    try:
        git.init()
        git.network("fetch", "-q", "--no-tags", "--no-recurse-submodules", git.url, f"+refs/heads/{branch}:refs/heads/{branch}")
    except assemble.GitError as err:
        raise JobError(f"the stack branch could not be fetched: {err}") from None
    publish.verify_branch(git, {"branch": branch, "head": found.sha, "commits": commits})
    return git


def base_tip(git: publish.Remote, ref: str) -> str:
    """The sha of the remote branch ('' when there is none). JobError when it cannot be read."""
    try:
        tip = publish.remote_tip(git, ref)
    except publish.PublishError:
        raise JobError("the default branch cannot be read") from None
    if tip and (len(tip) != 2 or tip[1] != ref or job.SHA.fullmatch(tip[0]) is None):
        raise JobError("the default branch answered something that is not a commit")
    return tip[0] if tip else ""


def reread(git: publish.Remote, default: str, sha: str, ref: str | None = None) -> bool | None:
    """Fetch the default branch afresh (into ``ref``) and say whether it contains ``sha`` (None: it could not be read)."""
    ref = ref or f"refs/piv/base-{os.urandom(4).hex()}"
    try:
        git.network("fetch", "-q", "--no-tags", "--no-recurse-submodules", git.url, f"+refs/heads/{default}:{ref}")
        done = git.run("merge-base", "--is-ancestor", sha, ref, check=False)
    except assemble.GitError:
        return None
    return {0: True, 1: False}.get(done.returncode)


@dataclass
class Merge:
    """What the merge endpoint answered: whether it says merged, and the merge commit it names ('' when none usable). ``before``
    is the base the job read just before the merge: the merge commit's first parent must be it."""
    merged: bool
    commit: str
    before: str


def merge_through_api(hub: Hub, s: Settings, pr: Pr, sha: str, before: str) -> Merge | None:
    """Merge the pull request with its head pinned to ``sha``. None when GitHub refused (exit not 0)."""
    try:
        out = hub.run("api", "--method", "PUT", f"repos/{s.repo}/pulls/{pr.number}/merge", "--raw-field", f"sha={sha}",
                      "--raw-field", f"merge_method={MERGE_METHOD}")
    except JobError:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    data = data if isinstance(data, dict) else {}
    commit = data.get("sha")
    # Only an explicit "merged": false is a merge that has not happened (a queue). An accepted call whose answer says nothing
    # else is taken as merged, and then it must name a commit that checks out.
    return Merge(data.get("merged") is not False, commit if isinstance(commit, str) and job.SHA.fullmatch(commit) else "", before)


def is_protected(hub: Hub, s: Settings, default: str) -> bool:
    """Evidence that the default branch is protected: ``protected`` is true in ``GET repos/<repo>/branches/<branch>``. Anything
    else (a failed read, a missing or odd field) is no evidence."""
    try:
        data = hub.json("api", f"repos/{s.repo}/branches/{quote(default, safe='/')}")
    except JobError:
        return False
    return isinstance(data, dict) and data.get("protected") is True


def check_base(git: publish.Remote, default: str, sha: str, merge: Merge | None = None) -> str:
    """Fetch the default branch afresh and judge it: "landed" (it holds the sealed commit as the commit itself or on its
    first-parent chain, or as the second parent of a merge whose tree IS the sealed tree; for a merge the API made, that merge
    commit: named by the answer, on the base, parents exactly [the base read before the merge, the sealed commit], and the
    sealed tree), "absent" (it does not contain the commit), "moved" (it holds the commit, or the answer named a merge, in a
    way that is none of those: a commit no gate saw may be on the base) or "unknown" (it could not be read), or "unverified"
    (the API said merged and named no commit to check)."""
    if merge is not None and merge.merged and not merge.commit:
        return "unverified"
    ref = f"refs/piv/base-{os.urandom(4).hex()}"
    contained = reread(git, default, sha, ref)
    if contained is None:
        return "unknown"
    try:
        tip = git.out("rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
        if merge is not None and merge.merged:
            return "landed" if merge_is_sealed(git, ref, merge, sha) else "moved"
        if not contained:
            return "absent"
        if tip == sha:
            return "landed"
        chain = [line.split() for line in git.out("rev-list", "--first-parent", "--parents", ref, f"^{sha}").splitlines()]
        for fields in chain:
            if fields[1:2] == [sha]:
                return "landed"  # the sealed commit was fast-forwarded; what is on top of it is somebody else's later work
            if len(fields) == 3 and fields[2] == sha:
                return "landed" if tree_of(git, fields[0]) == tree_of(git, sha) else "moved"
    except assemble.GitError:
        return "unknown"
    return "moved"


def tree_of(git: publish.Remote, commit: str) -> str:
    return git.out("rev-parse", "--verify", "-q", f"{commit}^{{tree}}")


def merge_is_sealed(git: publish.Remote, ref: str, merge: Merge, sha: str) -> bool:
    """The merge commit the API named is on the fetched base, joins the base read before the merge to the sealed commit (in
    that order, nothing else) and has the sealed commit's tree."""
    if git.run("merge-base", "--is-ancestor", merge.commit, ref, check=False).returncode != 0:
        return False
    fields = git.out("rev-list", "--parents", "-n1", merge.commit).split()
    return fields == [merge.commit, merge.before, sha] and tree_of(git, merge.commit) == tree_of(git, sha)


@dataclass
class Moved:
    """The base is no longer where the stack was gated: how it was found. ``unread``: whether it holds the sealed commit could
    not be read, so it is a re-gate by default and the comment says that, not that the base is known to have moved without it."""
    tip: str
    unread: bool = False


def moved_or_held(git: publish.Remote, default: str, sha: str, tip: str) -> Moved | str:
    """The base is at ``tip``, which is not where the stack was gated: ``"pushed"`` when it already holds the sealed commit (the
    re-read judges it), else Moved (``unread`` when git could not say)."""
    verdict = check_base(git, default, sha)
    return Moved(tip, verdict == "unknown") if verdict in ("absent", "unknown") else "pushed"


def move(git: publish.Remote, hub: Hub, s: Settings, pr: Pr, found: Sealed, default: str) -> str | Moved | Merge:
    """Steps 3 and 4. Returns "pushed" (the base is at the commit or holds it: pushed now, or already there; the re-read decides),
    "unprotected" (the push was refused and there is no evidence of protection: no merge was tried), "rejected" (the merge API refused), "unknown" (the branch
    could not be read after a refused push), a Merge (the API accepted a merge) or Moved. Nothing is retried."""
    ref = f"refs/heads/{default}"
    tip, sha, base = base_tip(git, ref), found.sha, found.land["base_sha"]
    if tip == sha:
        return "pushed"
    if tip != base:
        # A base that is not where the stack was gated may still HOLD the stack (it landed, or was merged): that is not a re-gate.
        return moved_or_held(git, default, sha, tip)
    try:
        git.network(*publish.push_args(git.url, sha, ref))
        return "pushed"
    except ValueError:
        return "rejected"
    except assemble.GitError:
        pass
    try:  # the answer may have been lost: judge by the branch
        again = base_tip(git, ref)
    except JobError:
        return "unknown"
    if again == sha:
        return "pushed"
    if again != base:  # the same question as above: a base that holds the sealed commit is landed, not re-gated
        return moved_or_held(git, default, sha, again)
    if not is_protected(hub, s, default):
        return "unprotected"  # a refused push is not evidence of protection: no merge commit where a fast-forward was planned
    return merge_through_api(hub, s, pr, sha, again) or "rejected"


# ------------------------------------------------------------------------------------------------ the answers


def comment(hub: Hub, s: Settings, issue: int, text: str) -> None:
    hub.run("api", "--method", "POST", f"repos/{s.repo}/issues/{issue}/comments", "--raw-field", f"body={text}")


def land_labels(hub: Hub, s: Settings, issue: int) -> None:
    ready, landed = job.label_name("pivotal:ready"), job.label_name("pivotal:landed")
    failed: JobError | None = None
    try:
        hub.run("api", "--method", "DELETE", f"repos/{s.repo}/issues/{issue}/labels/{quote(ready, safe='')}")
    except JobError as err:
        # gh's error text is not read, so a 404 (not there: nothing to remove) is told from any other failure (a 403, a 5xx) by
        # asking whether the issue still has the label: if it does, or that cannot be read, the failure is real.
        try:
            names = [x.get("name") for page in hub.json("api", "--paginate", "--slurp", f"repos/{s.repo}/issues/{issue}/labels?per_page=100")
                     if isinstance(page, list) for x in page if isinstance(x, dict)]
            still = ready in names
        except JobError:
            still = True
        if still:
            failed = err
    hub.run("api", "--method", "POST", f"repos/{s.repo}/issues/{issue}/labels", "--raw-field", f"labels[]={landed}")
    if failed is not None:
        raise JobError(f"the label {ready} could not be removed: {failed}")


def close_issue(hub: Hub, s: Settings, issue: int) -> None:
    hub.run("api", "--method", "PATCH", f"repos/{s.repo}/issues/{issue}", "--raw-field", "state=closed",
            "--raw-field", "state_reason=completed")


def carried_pin(found: Sealed) -> str:
    """Round 0's pin of the issue text, as the land artifact carries it, or "" (assemble writes no such key yet). It is never
    minted from the live title and body (R16): an edited text's pin would pass for round 0's."""
    pin = found.land.get("issue_pin")
    return pin if isinstance(pin, str) else ""


def regate(hub: Hub, s: Settings, issue: int, found: Sealed, work: Path, pin: str) -> str:
    """Dispatch the factory again (see the module docstring), built as publish builds a fix round: ``fix.payload``, ``fix.request``
    and ``fix.dispatch_argv``, so the keys are the receiver's ``trust.FIX_PIN_FIELDS`` plus ``findings``. "" when GitHub accepted
    the dispatch, else the reason it was not sent: ``pin`` (no round-0 pin) or ``dispatch``, or ``unknown`` (the answer is not known)."""
    if fix.ISSUE_PIN.fullmatch(pin) is None:
        return "pin"
    body = fix.request(fix.payload(
        issue=issue, slice_id=found.prefix[0], round_number=1, run_id=found.run_id, branch=assemble.stack_branch(issue),
        head=found.sha, issue_pin=pin, scores="", findings=FINDINGS))
    if fix.leaks(body, s.token):
        return "dispatch"
    path = work / "dispatch.json"
    assemble.write_new(path, body)
    try:
        hub.run(*fix.dispatch_argv(s.repo, str(path)))
    except job.GhRefused:
        return "dispatch"  # nothing was started
    except JobError:
        return "unknown"  # the request may have been made (a timeout, a failure with no HTTP status)
    return ""


def finish(env: Mapping[str, str], s: Settings, outcome: str, sha: str) -> None:
    job.write_outputs(s.output, {"land_outcome": outcome, "sealed_sha": sha})


def land(env: Mapping[str, str]) -> None:
    s = read_settings(env)
    if not job.may_land_by_hand(s.level):
        raise JobError(f"the level is {s.level}, which builds nothing, so there is nothing to land")
    s.work_dir.mkdir(mode=0o700)
    home = s.work_dir / "home"
    home.mkdir(mode=0o700)
    genv = gh_env(s, home)
    hub = Hub(s, genv)
    check_trust(s, genv)
    info = hub.json("api", f"repos/{s.repo}")
    default = publish.read_default_branch(info.get("default_branch") if isinstance(info, dict) and isinstance(info.get("default_branch"), str) else "")
    issue, pr = resolve(hub, s, default)
    sha = pr.head_sha
    found = find_seal(hub, s, default, issue, sha, s.work_dir)
    git = fetch_stack(s, s.work_dir, found)
    print(f"seal: run {found.run_id} sealed {sha} ({len(found.prefix)} slice(s): {', '.join(found.prefix)}); pull request #{pr.number}")
    # Every check is above this line. From here the job writes.
    result = move(git, hub, s, pr, found, default)
    problems: list[str] = []
    if isinstance(result, Moved):
        failure = regate(hub, s, issue, found, s.work_dir, carried_pin(found))
        sent = failure == ""
        text = (
            f"Not landed: {default} is at {result.tip or 'a state that cannot be read'}, not at {found.land['base_sha']} where "
            f"{sha} was gated, " + (
                "and whether it holds the sealed commit could not be read; " if result.unread else "so it moved after the seal; ") +
            "nothing was pushed. " + (
                "A re-gate was dispatched (a repository_dispatch of type pivotal-fix), but the factory refuses a dispatch that names "
                "the stack branch, so no run is expected to start from it. Re-run the issue with /pivotal go trusted." if sent else
                REGATE_NOT_SENT[failure])
        )
        try:
            comment(hub, s, issue, text)
        except JobError as err:
            problems.append(str(err))
        finish(env, s, "regate", sha)
        print(f"land: {default} moved; re-gate " + ("dispatched" if sent else "not dispatched (" + failure + ")"))
        # Red either way: trust refuses the stack branch (see the module docstring), so a dispatch that was made starts no run,
        # and a green job would claim a re-gate that nobody makes.
        raise JobError(("the base moved; the re-gate was dispatched, but the factory refuses a dispatch that names the stack branch" if sent
                        else "the base moved and the re-gate could not be dispatched")
                       + (f"; and the comment failed: {problems[0]}" if problems else ""))
    verdict = check_base(git, default, sha, result if isinstance(result, Merge) else None)
    if verdict == "landed":
        print(f"land: {default} contains {sha}")
        for step in (lambda: comment(hub, s, issue, f"Landed {sha} on {default}: the base contains the sealed commit."),
                     lambda: land_labels(hub, s, issue), lambda: close_issue(hub, s, issue)):
            try:
                step()
            except JobError as err:
                problems.append(str(err))
        finish(env, s, "landed", sha)
        if problems:
            raise JobError("landed, but: " + "; ".join(problems))
        return
    if verdict in ("unknown", "moved", "unverified"):
        word = verdict
    else:  # "absent": the sealed commit is not on the base
        word = "queued" if isinstance(result, Merge) else result if result in WHY_NOT else "missing"
    try:
        comment(hub, s, issue, f"Not landed: {word} ({WHY_NOT[word]}). The issue stays ready; the sealed commit is {sha}.")
    except JobError as err:
        problems.append(str(err))
    finish(env, s, "not_landed", sha)
    raise JobError(f"not landed: {word}" + (f"; and the comment failed: {problems[0]}" if problems else ""))


JOBS = {"run": land}


def main(argv: Sequence[str], environ) -> int:
    if len(argv) != 1 or argv[0] not in JOBS:
        print(f"usage: land.py {{{'|'.join(JOBS)}}}", file=sys.stderr)
        return 2
    try:  # before anything else: this process holds the write token
        buildjob.make_unreadable()
    except JobError as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1
    return job.run_job(JOBS[argv[0]], buildjob.take_environment(environ))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
