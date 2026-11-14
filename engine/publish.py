"""The publish job (F1, F4, F6): the `land` artifact to landed commits, stack and draft pull requests, and the seal.

This is the one job of the pipeline that holds a GitHub write token (`contents: write`, `pull-requests: write`, `checks: write`). It runs
only git and gh, on validated data, and no repo code and no model output: the repo is not even checked out. Everything
it reads is `land.json` and `pivotal.bundle`, the output of the assemble job, and that is hostile input until proven
otherwise (assemble's docstring states the contract; this module is the other side of it).

Before the first write, every one of these refuses the whole run (`refused: ...`, exit 1):

- `ASSEMBLE_RESULT` is not `success`;
- the sha256 of the downloaded `land.json` is not `LAND_SHA256`, the `land_sha256` OUTPUT of the assemble job (outputs
  travel through the workflow service, the artifact store does not, so the artifact is not trusted until they agree);
- `land.json` fails `assemble.read_land`/`check_land`, names another issue or base than the job's, or the artifact holds a
  file other than `land.json` and (when a draft exists) `pivotal.bundle`, or a link, FIFO or directory in their place;
- the bundle fails `assemble.check_bundle` (its sha256 and size, exactly the base as prerequisite, exactly the drafts as heads);
- a draft branch, once fetched from the bundle, is not at its head, is not exactly the commits of land.json in a straight
  line on `base_sha`, or touches a `.github` path in any commit (`--no-renames`, so a move out of it counts too). The
  `outcome` of land.json is never trusted for that; a workflow-file change is never pushed.

Only a `draft` slice has a branch. `blocked`, `conflict` and `failed` slices have none and are reported, not published.
`candidate: true` is NOT permission to land: only the landable prefix (below) is.

For each draft `pivotal/issue-<n>/<slice>`: look up pull requests by head branch (`gh pr list --head`, all states, a pull
request from a fork of the same branch name ignored). An answer of 20 items (the limit) may be cut off and fails the slice
(`gh_failed`) rather than risk a second draft. An OPEN pull request wins over closed ones; with none open, one closed or
merged at this very head counts; a closed one at another head is history. One that exists is left alone (`exists`), so a
re-run never opens a second one; if its head is not the head of land.json the slice fails (`pr_differs`) instead of being
changed. A title or body that holds the token is never sent (`secret_in_text`, `build.scan`). A pull request found closed
or merged is reported as `pr_closed`, not as an open draft. Otherwise the
branch is pushed (never forced; a remote branch at another commit is `branch_differs`, or `branch_stale` when a closed pull
request of the branch exists: the operator deletes that branch, no new branch name is ever made; one already at the head is
not pushed again) and a DRAFT pull request is opened against the default branch with `Closes #<issue>`, the reason from land.json and
a run marker in the body: `<!-- pivotal-run:{"run":..,"issue":..,"slice":..,"head":..} -->`. The URL `gh pr create` prints
is the pull request (the lookup after it is only for an answer that holds no URL). GitHub is the state: the marker
and the branch lookup are all a re-run needs. Whatever else goes wrong in a slice after a write is that slice's `unexpected`
failure: the other slices go on and the result is always written. A land whose only entries are `candidate`s is not a draft job: the stack below
is published (landed, or a stack pull request). A candidate outside a stack cannot exist: `check_land` makes `positions` exactly the
candidates, so `candidate_only` (still a word in report.WHY, for an old result) is never written by this job. The branch holds only the slice's commits on the base, so the pull request
shows only the slice's files.

Who gets here (operator decision): the caller grants the write ceiling of this job to the run of a stranger's `go` as well,
so the job never reads the trust decision. What it can do is bounded and pinned by tests: push `pivotal/issue-<n>/<slice>`
branches without force and open DRAFT pull requests, through `gh pr list` and `gh pr create` only.

Untrusted text: land.json carries only validated words (`REASON`, a route spec, a gate word, SHAs). The one free text is
the slice title from the plan's `MATRIX`, which goes into the pull request title through `job.clean_text` (one line, no
markdown, no mention, at most 60 characters). Every value in the body is printable ASCII, length-capped, in a code fence
that it cannot close. The body goes to gh as a file, never on the command line.

The engine pattern of engine/buildjob.py: run as `exec python3 -I pivotal/engine/publish.py run`, so this process is the
step process; it is made non-dumpable first, takes the step's environment into a dict and empties `os.environ` down to the
passthrough names, and hands each child (`gh`, `/usr/bin/git`) a fixed environment with only the token that call needs:
`GH_TOKEN` to gh, an `http.extraheader` (through `GIT_CONFIG_*`, not the command line) to the git calls that talk to the
remote. The git calls on the scratch repository (fetch from the bundle, rev-list, log) hold no token.

Run with these environment variables: GH_TOKEN, GH_REPO, ISSUE, BASE_SHA, ASSEMBLE_RESULT, LAND_SHA256, DEFAULT_BRANCH,
RUN_ID, RUN_NUMBER, RUN_ATTEMPT, LAND_DIR (the downloaded `land` artifact), WORK_DIR (must not exist), GITHUB_OUTPUT, KIND,
TRUSTED, LEVEL (PIVOTAL_LEVEL), REVIEWER (plan's output: a reviewer of another vendor exists for every builder; absent or not true/false refuses), HAS_STACK (assemble's output), GATE_RESULT and REVIEW_RESULT (`needs.<job>.result`: success,
failure or skipped), GATES_DIR, REVIEWS_DIR and SLICES_DIR (the downloaded `gate-*`, `review-*` and `slice-*` artifacts), and
optionally MATRIX (the plan's slices, for titles), PIV_GH (an absolute path to gh, the tests' seam; the workflow never sets
it; every gh call gets the fixed PATH `/usr/bin:/bin`) and GITHUB_SERVER_URL (set by the runner; `https://` or, for tests,
`file:///`), and ROUND (the plan job's `round` output: 0, or the fix round this run is; required, because the bound on the loop
must never rest on a default). The job output `result` is the one line of JSON that engine/report.py reads.

FIX ROUNDS (F8, engine/fix.py). After the stack and the drafts, a draft slice whose reason is a NEEDS FIXES verdict, a
conflict or (a score run) a score below the target (`fix.kind_of`; a red gate is a failed build with no branch, so it is no trigger) gets ONE `repository_dispatch` of type `pivotal-fix` with `{issue, slice, round: ROUND+1, run_id, branch,
head_sha, issue_pin, scores, findings}` (`issue_pin` is the plan job's output: the pin round 0 was trusted on, forwarded each round; no dispatch without a well-formed one) while `ROUND < fix.MAX_ROUNDS`, only from a TRUSTED run (a stranger's `go` never starts a trusted fix), only when the
slice's draft PR is OPEN at the head being sent, and never twice for a slice in one run. `findings` is built from the reviewer's text
(`review-<id>/review.md`, read through `review.load`), the end of the build's redacted summary (`slice-<id>/summary.md`: the build log's redaction, not the gate's log) and the score history, cleaned, quoted and capped at 20 KB (fix.findings_text); the request is scanned for the token and
is not sent if it holds it. It is sent with `fix.dispatch_argv` (`gh api --method POST repos/<repo>/dispatches --input <file>`, JSON said by Content-Type), which needs
`contents: write` on the token (the REST docs for "Create a repository dispatch event" name the Contents permission; the job holds it
already, so no new grant; NOT verified on a real repo). GITHUB_TOKEN may create this event and, unlike its other events, it starts a
workflow run (the same documented exception as workflow_dispatch; not verified here). Sending is best effort. RESULT's `"fix"` names every kind of problem that occurred, space-separated in the order
`failed unknown rerun` (`fix_result`, read back by report.py): `failed` for a slice that could not be sent (`dispatch_failed`: before
the request, or a refusal the server gave, `job.GhRefused`; `secret`, `unexpected`, `no_pin`); `unknown` for a dispatch whose answer
is not known (gh failed after the request may have been made: "may have been started"); `rerun` for a slice whose PR this run neither
opened nor updated (`not_new`): a re-run never sends twice, so it says an earlier run may have sent the round. Each is a problem the report
shows; it never changes `landed`, `partial_land`, a draft or a failure into anything else, and never fails the job. A fix round's own
slice is a draft on its own branch: publish fast-forwards the branch from the old head (the run's base) to the new one, never forced, and
the existing draft PR follows the branch (`update_pr`, status `updated`). A slice whose `ROUND` is already `fix.MAX_ROUNDS` ends as
`max_rounds` in the report. A request with no score has ONE fix round (`plan.DEFAULT_MAX_ROUNDS` = 1; a score run has `hill.MAX_ROUNDS`, see HILL-CLIMB below), and a fix round never lands: even READY it ends as a
draft PR for a person to merge.
HILL-CLIMB (engine/hill.py): in a request with a score, a draft that scored below the target (`score_low`) is a fix round while
`round < hill.MAX_ROUNDS`, and its dispatch carries `scores`, the history `score@head` of every round built, pinned. A round that
scored below the best so far is not pushed: the draft stays at the best round's head (`kept`, a comment says so) and the climb stops
(`below_best`: nothing is sent). A fix round that meets the target is marked ready for review (`score_met`, RESULT `ready`), never stacked; round 0 that meets it is stacked as usual.

THE STACK AND THE GATE (F4). With a stack, the bundle also holds `pivotal/issue-<n>/stack`; it is fetched and verified like a
draft (exactly the candidates' commits in a straight line on the base, no `.github` path). The landable prefix *m*
(`landable_prefix`) is then computed before any write: the longest run of stacked slices, from the first, for each of which
the downloaded `gate-<id>` is green for exactly the slice's head (`gate.read_gate` and `gate.confirm`: green, HEAD unchanged,
tree clean, the check not `none`), the `slice-<id>` artifact agrees with land.json, and `review-<id>` is a READY from another
vendor bound to the sha256 of that slice's patch and to the files the slice changes on the stack (`review.load`,
`review.accept`), and the baseline is green. Nothing is taken from `outcome`: every artifact is read again as hostile input,
and a missing, malformed or different one ends the prefix there (F5). A job whose review result is not `success` has no prefix.

A run MAY LAND (`Settings.may_land`) only if it is trusted, the level is `land` and `job.level_allows` starts its kind. The level here is `job.standing`'s (the one place the ladder is decided): `land` is held at `prepare` without a reviewer of another vendor (REVIEWER) and at `ask` without a green baseline (land.json's), and then the ready stack PR is opened with `why a pull request: held` and the reasons. Then
the base moves by one plain `git push <m>:refs/heads/<default>` (never forced: `push_args`; no flag, no `+`), and only when
the default branch is still at `BASE_SHA`. A refusal for any reason (it moved mid-run, a protected branch, not a
fast-forward) is not an error and is not retried (a push that fails is judged by reading the branch again: at *m* it was
accepted and only the answer was lost, so it is landed; when it cannot be read nobody knows, `ff_unknown`, red, no PR, no seal): the prefix goes into a READY stack pull request from
`pivotal/issue-<n>/stack` instead (`Closes #<issue>`, the reason in a fenced fact list, the slices left out named). Below the
land level the same ready stack PR is opened and nothing moves; a stranger's run (untrusted) gets a DRAFT stack PR. Slices
after *m* (the prefix shorter than the stack, or empty) are NOT lost: the whole stack goes on `pivotal/issue-<n>/stack` and one
DRAFT stack PR names the first slice that did not pass and why. A prefix that can land lands first (and is sealed); the
draft then holds the rest on top of it. A prefix that cannot land has no ready PR of its own (the branch cannot be both), only
that draft, and no seal. `draft` slices still get theirs. A re-run finds the stack PR by its branch, as the drafts are, and
opens no second. A draft stack PR that fails AFTER the base moved is the draft's failure, not the landing's: the RESULT still
says landed (`partial_land`), the job is red, and the report job lets the RESULT stand (report.decide).

A SLICE IS ONE COMMIT (F-1). The gate runs on the head of a stacked slice and the fast-forward moves every commit up to it, so
a slice of several commits would land commits no gate ever saw. The build job squashes the agent's commits and the heal commit
into one before it makes the patch; assemble makes a candidate with several commits a `draft` (`multi_commit`); the prefix here
stops at one (`multi_commit`) however land.json came to say candidate.

WHAT THE VERDICT BINDS (F-4). The review names the sha256 of the patch on the base and the files it changes. The commit that
lands is that patch re-applied on the stack (`git am --3way`). Decision: "the verdict names its SHA" means the patch sha256
plus the file set (checked here against the stack's diff), and assemble also refuses (`stack_differs`, a draft) a candidate
whose commit on the stack makes a different change than its commit on the base (`assemble.diff_shape`: +/- lines at zero
context, modes, statuses and the new blob of a binary file, not hunk offsets or blob ids that an earlier slice moves).
This job re-checks the file names of the stacked diff only; it trusts the content to the bundle that passed the land.json hash,
which only the assemble job wrote.

THE SEAL comes last and only when the run is trusted, its (effective) level is not held (job.standing), and it starts its kind:
`ask`, `prepare` or `land` and never `watch` (`Settings.may_seal`; at `ask` and `prepare` a human's "land it" is what moves the base, and the land job requires this seal). After the push succeeded (or the base already was at *m*), or after the
stack PR exists, is OPEN and is READY (a draft is no pull request to merge), `pivotal/gate` is created on *m* with `external_id` =
RUN_ID through `gh api --method POST repos/<repo>/check-runs --input <file>` (engine/seal.py; its `verify` is what the land
job runs; it compares `check_suite.id` of the check run with `check_suite_id` of the run, which has never been seen on real
GitHub and does not establish who made the seal: the land job must also require the app, the workflow path and the artifacts, see seal.py). After the base moved nothing that follows is
an error of the landing: a seal that cannot be created is red (`stack/seal` in the log, the job fails) and the draft for the rest of the stack
is made all the same, and the RESULT stays `landed` (or `partial_land`, or `partial_land_ready` when a READY stack PR holds the rest), because the job holds what it did in a dict
(`new_stack`), not in a return value that an exception would skip. If the RESULT itself cannot be written to the outputs, the
report job reads `publish_failed` although the base moved (a known gap): the line is printed to the log. Before the base moved, a seal that cannot be created is
`seal_failed`. Nothing here has run on real GitHub: branch protection, the check run's app identity, the `path` of a real run, the
check-suite ids and fast-forward semantics on a real remote are unverified.
"""

import base64
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

if __package__ in (None, ""):  # run as a script under python3 -I, which puts no directory on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import assemble, build, buildjob, fix, gate, hill, job, plan, report, review, seal  # noqa: E402
from engine.job import JobError  # noqa: E402

SAFE_PATH = "/usr/bin:/bin"
DEFAULT_SERVER = "https://github.com"
SERVER = re.compile(r"https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?|file:///[A-Za-z0-9_./-]{1,512}")
TOKEN = re.compile(r"[!-~]{1,4096}")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
PR_FIELDS = "number,isDraft,state,headRefName,headRefOid,isCrossRepository"
PR_STATES = ("OPEN", "CLOSED", "MERGED")
PR_LIMIT = 20
CREATED = re.compile(r"https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?/(?P<repo>[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})/pull/(?P<number>[1-9][0-9]{0,9})")
TITLE_PREFIX = "Pivotal: "
TITLE_MAX = 60
FENCE_MAX = 2000
# What a slice's reason in land.json means for report.WHY, when no pull request was opened. Anything else is build_failed.
WHY_OF = {
    "workflow_change": "workflow_change", "does_not_apply": "conflict", "secret_in_patch": "secret_in_patch",
    "no_route": "no_route", "no_secret": "no_secret", "gate_red": "gate_red", "baseline_red": "baseline_red",
    "agent_no_output": "agent_no_output",
}
# The same for a slice that did get a draft PR; any other reason (a gate or a verdict that stopped the stack) is no_stack.
DRAFT_WHY = {
    "baseline_red": "baseline_red", "gate_red": "gate_red", "gate_none": "gate_none", "verdict_needs_fixes": "review_rejected",
    "already_green": "already_green",  # an incident whose base already passes: its change is not needed (I1)
    "fix_round": "fix_round", "conflict": "conflict", "stack_differs": "stack_differs", "multi_commit": "multi_commit",
    "score_low": "score_low", "score_met": "below_best",  # a met round that is a draft here was kept back: a better round is the draft
    **{f"verdict_{w}": f"verdict_{w}" for w in ("missing", "invalid", "stale", "wrong_sha", "files", "wrong_builder")},
}
# What a failed slice's word means for report.WHY; any other failure is publish_failed.
FAIL_WHY = {
    "branch_stale": "branch_stale", "seal_failed": "seal_failed", "pr_differs": "pr_differs", "branch_differs": "branch_differs",
    "push_rejected": "push_rejected", "gh_failed": "gh_failed", "ff_unknown": "ff_unknown", "ready_failed": "ready_failed",
}
# Every word a failed RESULT of the publish job can carry (result_line: a stack's or a slice's FAIL_WHY, a slice's WHY_OF, or
# build_failed). Derived, so report.py cannot drift from the tables above.
PUBLISH_WHY = frozenset(FAIL_WHY.values()) | frozenset(WHY_OF.values()) | {"build_failed"}
OPENED, EXISTS, SKIPPED, UPDATED = "opened", "exists", "skipped", "updated"
KEPT = "kept"  # a hill-climb round that scored below the best so far: the draft stays at the best round's head (engine/hill.py)
JOB_RESULTS = ("success", "failure", "skipped")  # the results publish accepts for the gate and review jobs (cancelled never gets here)
LANDED, STACK_PR = "landed", "pr"
STACK_TITLE = "Pivotal: stack for #{issue} ({count} of {total} slices)"
# The words of why the stack is where it is: in the log, in the stack PR body. Fixed, never a value from an artifact.
STACK_REASONS = {
    "ff_rejected": "the default branch moved or refused the push, so the base was not moved",
    "level": "the repo's level does not land, so the base was not moved",
    "held": "the repo's level lands, but a requirement of it is not met (see the level held line), so the base was not moved",
    "untrusted": "this run was started by someone the repo does not trust, so nothing is landed or sealed",
    "rest": "the stack holds slices that did not pass the gate, so this draft is for them; whatever passed is not here unless the base took it",
}


class PublishError(Exception):
    """One slice could not be published. ``reason`` is a fixed word; the other slices go on."""

    def __init__(self, reason: str, existing: Mapping | None = None):
        super().__init__(reason)
        self.reason = reason
        self.existing = existing  # pr_differs: the pull request that is there ({"pr", "state", "draft"})


# ------------------------------------------------------------------------------------------------ settings


@dataclass
class Settings:
    repo: str
    issue: int
    base: str
    land_sha256: str
    default_branch: str
    run_id: int
    server: str
    land_dir: Path
    work_dir: Path
    output: str
    token: str
    titles: dict[str, str]
    level: str
    kind: str
    trusted: bool
    has_stack: bool
    gate_result: str
    review_result: str
    gates_dir: Path
    reviews_dir: Path
    slices_dir: Path
    round: int
    issue_pin: str
    gh: str = "gh"
    target: str = ""  # the hill-climb's target text, "" for a request with no score (plan's output)
    scores: str = ""  # the score history of the rounds built before this one (plan's output, engine/hill.py)
    configured: str = ""  # PIVOTAL_LEVEL as set; ``level`` is what job.standing says the facts honour
    reviewer: bool = False
    check: bool = False
    planned: str = ""  # the level plan started the run at
    held: tuple[str, ...] = ()

    def with_baseline(self, baseline: str) -> "Settings":
        """Settings whose level is what every fact honours, the baseline of this run (land.json's) last: the only way to a
        level above ``watch``, so nothing lands before land.json is read."""
        st = job.standing(self.configured, check=self.check, baseline=baseline, reviewer=self.reviewer, planned=self.planned)
        return replace(self, level=st.effective, held=st.reasons)

    @property
    def may_land(self) -> bool:
        """The run may move the base and seal: a trusted run at a level that lands its kind (F4)."""
        return self.trusted and job.lands(self.level, self.kind)

    @property
    def may_seal(self) -> bool:
        """The run may seal a READY stack pull request a human will land: a trusted run at any level that starts its kind
        (ask, prepare, land; ``watch`` starts nothing) that job.standing does not hold. The seal says nothing about who moves
        the base: the land job decides."""
        return self.trusted and not self.held and job.may_land_by_hand(self.level) and job.level_allows(self.level, self.kind)


def _path(env: Mapping[str, str], name: str) -> Path:
    value = env.get(name, "")
    if not value or "\0" in value or not os.path.isabs(value):
        raise JobError(f"{name} must be an absolute path")
    return Path(value)


def read_default_branch(value: str) -> str:
    if (
        BRANCH.fullmatch(value) is None or ".." in value or "//" in value or "@{" in value
        or value.endswith(("/", ".", ".lock")) or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    ):
        raise JobError("DEFAULT_BRANCH is not a branch name")
    return value


def read_settings(env: Mapping[str, str]) -> Settings:
    """Every input, checked; nothing is written. Anything wrong is a JobError."""
    repo = job.read_repo(env)
    if env.get("ASSEMBLE_RESULT") != "success":
        raise JobError("the assemble job did not succeed, so there is nothing to publish")
    issue = job.read_number(env.get("ISSUE", ""), "ISSUE")
    base = env.get("BASE_SHA", "")
    if job.SHA.fullmatch(base) is None:
        raise JobError("BASE_SHA is not a commit SHA")
    land_sha = env.get("LAND_SHA256", "")
    if job.OUTPUT_RULES["land_sha256"].fullmatch(land_sha) is None:
        raise JobError("LAND_SHA256 is not the sha256 the assemble job output")
    token = env.get("GH_TOKEN", "")
    if TOKEN.fullmatch(token) is None:
        raise JobError("GH_TOKEN is not set or is not printable ASCII up to 4096 characters")
    server = env.get("GITHUB_SERVER_URL") or DEFAULT_SERVER
    if SERVER.fullmatch(server) is None:
        raise JobError("GITHUB_SERVER_URL is not an https URL")
    run_id = job.read_number(env.get("RUN_ID", ""), "RUN_ID", job.RUN_ID_DIGITS)
    job.read_number(env.get("RUN_NUMBER", ""), "RUN_NUMBER")
    job.read_number(env.get("RUN_ATTEMPT", "1"), "RUN_ATTEMPT")
    land_dir, work_dir = _path(env, "LAND_DIR"), _path(env, "WORK_DIR")
    if not land_dir.is_dir():
        raise JobError("LAND_DIR is not a directory")
    if os.path.lexists(work_dir):
        raise JobError("WORK_DIR already exists")
    titles: dict[str, str] = {}
    if env.get("MATRIX"):
        try:
            titles = {x.id: x.title for x in plan.parse_slices(env["MATRIX"])}
        except plan.SliceError as err:
            raise JobError(f"MATRIX is not the plan's slices: {err}") from None
    binary = env.get("PIV_GH", "gh")  # the test seam of every gh caller; the workflow never sets it
    if binary != "gh" and (not os.path.isabs(binary) or "\0" in binary):
        raise JobError("PIV_GH must be an absolute path")
    kind, trusted = job.read_kind(env)
    configured = job.read_level(env.get("LEVEL", ""))
    reviewer = {"true": True, "false": False}.get(env.get("REVIEWER", ""))
    if reviewer is None:
        raise JobError("REVIEWER is not the plan job's output")
    planned = job.read_planned_level(env.get("PLANNED_LEVEL", ""))
    check = job.check_present(env.get("CHECK", ""))
    has_stack = {"true": True, "false": False}.get(env.get("HAS_STACK", ""))
    if has_stack is None:
        raise JobError("HAS_STACK is not the assemble job's output")
    round_text = env.get("ROUND", "")
    if re.fullmatch(r"[0-9]{1,4}", round_text) is None:  # no default: a loop's bound must not rest on one
        raise JobError("ROUND is not the plan job's round output")
    gate_result, review_result = env.get("GATE_RESULT", ""), env.get("REVIEW_RESULT", "")
    if gate_result not in JOB_RESULTS or review_result not in JOB_RESULTS:
        raise JobError("GATE_RESULT and REVIEW_RESULT must be the results of the gate and review jobs")
    if (gate_result == "skipped") == has_stack:  # the gate runs exactly when assemble stacked something (F5)
        raise JobError("the gate job's result does not fit what the assemble job stacked")
    target, scores = env.get("TARGET", ""), env.get("SCORES", "")
    if target:
        try:
            hill.check_target(target)
            history = hill.decode_history(scores)
        except hill.HillError as err:
            raise JobError(f"score: {err}") from None
        if len(history) != int(round_text):  # plan's check: one entry per round built so far
            raise JobError("score: the history does not have one entry per round built so far")
    elif scores:
        raise JobError("score: a score history without a target")
    return Settings(
        repo, issue, base, land_sha, read_default_branch(env.get("DEFAULT_BRANCH", "")), run_id, server.rstrip("/"),
        land_dir, work_dir, job.read_output_path(env), token, titles, "watch", kind, trusted, has_stack,
        gate_result, review_result, _path(env, "GATES_DIR"), _path(env, "REVIEWS_DIR"), _path(env, "SLICES_DIR"),
        int(round_text), env.get("ISSUE_PIN", ""), binary,
        configured=configured, reviewer=reviewer, check=check, planned=planned, target=target, scores=scores,
    )


# ------------------------------------------------------------------------------------------------ the artifact


def load_land(s: Settings) -> tuple[dict, Path | None]:
    """The checked land.json and the path of the checked bundle (None without a draft). Raises JobError."""
    try:
        fd = assemble.open_dir(str(s.land_dir))
    except assemble.ArtifactError:
        raise JobError("LAND_DIR cannot be opened") from None
    try:
        names = set(os.listdir(fd))
        if assemble.LAND not in names or not names <= {assemble.LAND, assemble.BUNDLE}:
            raise JobError("the land artifact must hold land.json and at most pivotal.bundle")
        for name in names:  # a link, FIFO, device or directory in place of a file is refused before anything is read
            if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                raise JobError("the land artifact holds something that is not a regular file")
        try:
            raw = assemble.read_regular(Path(assemble.LAND), assemble.MAX_LAND_BYTES, fd)
        except assemble.ArtifactError as err:
            raise JobError(f"land.json is unreadable: {err.reason}") from None
    except OSError:
        raise JobError("the land artifact cannot be read") from None
    finally:
        os.close(fd)
    if hashlib.sha256(raw).hexdigest() != s.land_sha256:
        raise JobError("land.json is not the file the assemble job described (sha256 differs from its output)")
    try:
        land = assemble.read_land(s.land_dir / assemble.LAND)
        canonical = assemble.land_text(land)
    except assemble.LandError as err:
        raise JobError(f"land.json is invalid: {err}") from None
    if canonical != raw:
        raise JobError("land.json is not in the form the assemble job writes")
    if land["issue"] != s.issue or land["base_sha"] != s.base:
        raise JobError("land.json is for another issue or base than this run's")
    if land["has_stack"] != s.has_stack:
        raise JobError("land.json and the assemble job disagree about whether there is a stack")
    if land["bundle"] is None:
        if names != {assemble.LAND}:
            raise JobError("land.json describes no bundle, yet the artifact holds one")
        return land, None
    if assemble.BUNDLE not in names:
        raise JobError("the bundle land.json describes is missing")
    bundle = s.land_dir / assemble.BUNDLE
    try:
        assemble.check_bundle(land, bundle)
    except assemble.LandError as err:
        raise JobError(f"the bundle is not the one land.json describes: {err}") from None
    return land, bundle


# ------------------------------------------------------------------------------------------------ git


class Remote(assemble.Git):
    """assemble's hardened git on a scratch repository, plus the remote: the token reaches only the calls made through
    ``network``, as a header in ``GIT_CONFIG_*`` (never in argv). Every call checks objects it receives (fsck)."""

    def __init__(self, root: Path, base: str, server: str, repo: str, token: str):
        super().__init__(root, base)
        self.server, self.url = server, f"{server}/{repo}.git"
        self.token, self.authed = token, False

    def environment(self, when: int | None = None) -> dict[str, str]:
        env = super().environment(when)
        config = [("fetch.fsckObjects", "true"), ("transfer.fsckObjects", "true"), ("protocol.version", "2")]
        if self.authed and self.server.startswith("https://"):
            pair = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            config.append((f"http.{self.server}/.extraheader", f"AUTHORIZATION: basic {pair}"))
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for number, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{number}"], env[f"GIT_CONFIG_VALUE_{number}"] = key, value
        return env

    def network(self, *args: str) -> str:
        self.authed = True
        try:
            return self.out(*args)
        finally:
            self.authed = False


def verify_branch(git: Remote, entry: Mapping) -> None:
    """The fetched branch is what land.json says and touches no workflow file. Raises JobError (a refusal of the run)."""
    ref, base, head = f"refs/heads/{entry['branch']}", git.base, entry["head"]
    try:
        if git.out("rev-parse", "--verify", "-q", f"{ref}^{{commit}}") != head:
            raise JobError("a fetched branch is not at the head land.json names")
        chain = [line.split() for line in git.out("rev-list", "--parents", "--reverse", f"{base}..{head}").splitlines()]
        parent = base
        for fields, want in zip(chain, entry["commits"]):
            if fields != [want, parent]:
                raise JobError("a draft is not its commits of land.json in a straight line on the base")
            parent = want
        if len(chain) != len(entry["commits"]):
            raise JobError("a draft is not its commits of land.json in a straight line on the base")
        touched = git.run("log", "--no-renames", "--name-only", "-z", "--format=", f"{base}..{head}").stdout.split(b"\0")
    except assemble.GitError:
        raise JobError("a draft branch cannot be read") from None
    if any(assemble.is_workflow_path(t.decode("utf-8", "replace")) for t in touched if t):
        raise JobError("a draft touches .github: a workflow-file change is never pushed")


def stack_entry(land: Mapping) -> dict | None:
    """The stack branch as a ``verify_branch`` entry: its head is the last candidate's, its commits are every candidate's in
    order. None without a stack."""
    stacked = [next(x for x in land["slices"] if x["id"] == ident) for ident in land["positions"]]
    if not stacked:
        return None
    return {"branch": assemble.stack_branch(land["issue"]), "head": stacked[-1]["head"],
            "commits": [c for x in stacked for c in x["commits"]]}


def fetch_drafts(s: Settings, land: Mapping, bundle: Path | None) -> Remote | None:
    """A scratch repository holding the base, every draft branch and the stack branch, each verified. None without any."""
    drafts = [x for x in land["slices"] if x["outcome"] == "draft"]
    stack = stack_entry(land)
    if (not drafts and stack is None) or bundle is None:
        return None
    entries = [*drafts, *([stack] if stack else [])]
    git = Remote(s.work_dir, s.base, s.server, s.repo, s.token)
    s.work_dir.mkdir(mode=0o700)
    try:
        git.init()
        git.network("fetch", "-q", "--no-tags", "--no-recurse-submodules", git.url, s.base)
        if git.out("cat-file", "-t", s.base) != "commit":
            raise JobError("BASE_SHA is not a commit of the repository")
        git.run("fetch", "-q", "--no-tags", str(bundle), *(f"refs/heads/{x['branch']}:refs/heads/{x['branch']}" for x in entries))
    except assemble.GitError as err:
        raise JobError(f"the base or the bundle could not be fetched: {err}") from None
    for entry in entries:
        verify_branch(git, entry)
    return git


# ------------------------------------------------------------------------------------------------ the landable prefix


@dataclass
class Prefix:
    """The landable prefix of the stack: the longest run of slices, from the first, that passed every F4 condition."""
    ids: list[str]  # the slice ids in stack order
    sha: str  # m, the head of the last of them ("" for none)
    why_not: str  # why the prefix stops where it does ("" when it is the whole stack); a fixed word
    built_by: str = ""
    reviewed_by: str = ""
    total: int = 0
    tip: str = ""  # the head of the whole stack (the stack branch), which is m only when the prefix is all of it
    left: str = ""  # the first slice id that is not in the prefix ("" when it is all of the stack)


def slice_files(git: Remote, before: str, head: str) -> list[str]:
    """The files a slice changes on the stack, in the ``review.files`` form: both names of a rename, since the diff is
    made without rename detection."""
    out = git.run("diff", "--no-renames", "--name-only", "-z", f"{before}..{head}").stdout
    return review.files_of(name for name in out.split(b"\0") if name)


def check_gate_artifact(s: Settings, land: Mapping, ident: str) -> str:
    """"" when ``gate-<ident>`` is a green gate for exactly this slice's head, else the reason word."""
    if not os.path.lexists(s.gates_dir / f"gate-{ident}"):
        return "gate_missing"
    try:
        found = seal.read_gate_of(s.gates_dir, ident)
        gate.confirm(land, found)
    except gate.GateError:
        return "gate_invalid"
    if found["id"] != ident:
        return "gate_other"
    if not found["green"]:
        return {"red": "gate_red", "none": "gate_none"}.get(found["check"], "gate_dirty")
    return ""


def check_review_artifact(s: Settings, land: Mapping, entry: Mapping, result: Mapping, patch: bytes, files: list[str]) -> tuple[str, str]:
    """("", reviewer) when the slice's ``review-<id>`` is a READY from another vendor for exactly this patch and these
    files, else ("reason word", "")."""
    directory = s.reviews_dir / f"review-{entry['id']}"
    if not os.path.lexists(directory):
        return "review_missing", ""
    try:
        verdict = review.load(directory)
    except (review.ReviewError, OSError, RecursionError):
        return "review_invalid", ""
    if verdict["slice"] != entry["id"] or verdict["round"] != result["round"] or verdict["base"] != land["base_sha"]:
        return "review_stale", ""
    why = review.accept(verdict, patch_sha256=hashlib.sha256(patch).hexdigest(), files=files, built_by=result["built_by"])
    return (f"review_{why}" if assemble.REASON.fullmatch(f"review_{why}") else "review_invalid", "") if why else ("", verdict["reviewed_by"])


def landable_prefix(s: Settings, land: Mapping, git: Remote) -> Prefix:
    """m: the longest prefix of the stack where, for every slice, the gate artifact for exactly its head is green, the
    review is a READY of another vendor bound to its patch and files, the build result agrees with land.json, and the
    baseline and the build's own check were green and non-empty. Nothing is trusted from ``outcome``: every artifact is
    read again as hostile input. Never raises; anything that cannot be shown is the end of the prefix (F5)."""
    positions = land["positions"]
    prefix = Prefix([], "", "", total=len(positions))
    if not positions:
        return prefix
    prefix.tip = next(x for x in land["slices"] if x["id"] == positions[-1])["head"]
    prefix.left = positions[0]
    if land["baseline"] != "green":
        prefix.why_not = "baseline_red"
    elif s.review_result != "success":
        prefix.why_not = "review_failed"
    if prefix.why_not:
        return prefix
    try:
        slices_fd = assemble.open_dir(str(s.slices_dir))
    except assemble.ArtifactError:
        prefix.why_not = "slice_invalid"
        return prefix
    try:
        before = land["base_sha"]
        for ident in positions:
            entry = next(x for x in land["slices"] if x["id"] == ident)
            why, reviewer = slice_landable(s, land, git, slices_fd, entry, before)
            if why:
                prefix.why_not = why
                break
            before = entry["head"]
            prefix.ids.append(ident)
            prefix.sha = entry["head"]
            prefix.built_by = prefix.built_by or entry["built_by"]
            prefix.reviewed_by = prefix.reviewed_by or reviewer
    finally:
        os.close(slices_fd)
    prefix.left = positions[len(prefix.ids)] if len(prefix.ids) < len(positions) else ""
    return prefix


def slice_landable(s: Settings, land: Mapping, git: Remote, slices_fd: int, entry: Mapping, before: str) -> tuple[str, str]:
    """("", reviewer) when this stacked slice passes every condition, else (reason word, "")."""
    try:
        if entry["outcome"] != "candidate" or entry["gate"] != "green" or not entry["commits"] or entry["head"] != entry["commits"][-1]:
            return "not_candidate", ""
        if not assemble.one_commit(entry["commits"]):  # F4: only the slice head is gated, so a slice is exactly one commit
            return "multi_commit", ""
        why = check_gate_artifact(s, land, entry["id"])
        if why:
            return why, ""
        try:
            artifact = assemble.read_artifact(slices_fd, entry["id"])
        except (assemble.ArtifactError, assemble.SliceFailure):
            return "slice_invalid", ""
        result = artifact.result
        if (
            artifact.patch is None or result["status"] != "ok" or result["workflow_change"] or result["round"] != 0
            or result["gate"] != "green" or result["built_by"] != entry["built_by"] or not result["built_by"]
        ):
            return "slice_invalid", ""
        return check_review_artifact(s, land, entry, result, artifact.patch, slice_files(git, before, entry["head"]))
    except (assemble.GitError, OSError, ValueError, RecursionError):
        return "internal", ""


# ------------------------------------------------------------------------------------------------ pull requests


def gh_env(s: Settings, home: Path) -> dict[str, str]:
    """The environment of every gh call: the token, the repo and a fixed PATH, nothing else of the step's."""
    env = {
        "PATH": SAFE_PATH, "HOME": str(home), "GH_CONFIG_DIR": str(home / "gh"), "GH_TOKEN": s.token,
        "GH_REPO": s.repo, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1",
    }
    host = s.server.removeprefix("https://")
    if s.server.startswith("https://") and host != "github.com":
        env["GH_HOST"] = host
    return env


def list_prs(env: Mapping[str, str], s: Settings, branch: str) -> list[dict]:
    """The pull requests of ``branch`` in this repository, every state. A pull request from a fork with the same branch
    name is ignored. An answer that may be cut off at the limit, or of another shape, is a JobError."""
    data = job.gh_json(env, "pr", "list", "--repo", s.repo, "--head", branch, "--state", "all", "--limit", str(PR_LIMIT),
                       "--json", PR_FIELDS, binary=s.gh)
    if not isinstance(data, list) or len(data) >= PR_LIMIT:  # gh never returns more than the limit: a full answer may be cut
        raise JobError("gh pr list did not answer a short list")
    found = []
    for item in data:
        if not isinstance(item, dict) or set(item) != set(PR_FIELDS.split(",")):
            raise JobError("gh pr list answered a pull request of another shape")
        number, state, seen = item["number"], item["state"], item["headRefOid"]
        if (
            isinstance(number, bool) or not isinstance(number, int) or number < 1 or state not in PR_STATES
            or not isinstance(item["isDraft"], bool) or not isinstance(item["isCrossRepository"], bool)
            or not isinstance(item["headRefName"], str) or not isinstance(seen, str) or job.SHA.fullmatch(seen) is None
        ):
            raise JobError("gh pr list answered a pull request of another shape")
        if not item["isCrossRepository"] and item["headRefName"] == branch:
            found.append(item)
    return found


def pick_pr(found: Sequence[dict], head: str) -> dict | None:
    """An OPEN pull request wins (the lowest number), so a closed pull request of an earlier head never hides the live
    one; with none open, the lowest-numbered one whose head is ``head`` (a closed or merged draft of this very commit);
    a closed or merged one at another head is history and is not ours."""
    live = [x for x in found if x["state"] == "OPEN"] or [x for x in found if x["headRefOid"] == head]
    return min(live, key=lambda x: x["number"]) if live else None


def find_pr(env: Mapping[str, str], s: Settings, branch: str, head: str) -> dict | None:
    return pick_pr(list_prs(env, s, branch), head)


def fenced(text: object, limit: int = FENCE_MAX) -> str:
    """Untrusted text as data in a code fence: printable ASCII only (everything else, and every backtick, becomes a
    space), at most ``limit`` characters, so it can neither close the fence nor carry markdown, a mention or a control."""
    plain = "".join(c if " " <= c <= "~" and c != "`" else " " for c in str(text))[:limit]
    return f"```text\n{plain}\n```"


def pr_title(s: Settings, entry: Mapping) -> str:
    title = job.clean_text(s.titles.get(entry["id"]), f"slice {entry['id']}", TITLE_MAX)
    return f"{TITLE_PREFIX}{title} (#{s.issue})"


def pr_body(s: Settings, land: Mapping, entry: Mapping) -> str:
    marker = json.dumps({"run": s.run_id, "issue": s.issue, "slice": entry["id"], "head": entry["head"]}, separators=(",", ":"))
    facts = "\n".join([
        f"reason: {entry['reason']}", f"slice: {entry['id']}", f"built by: {entry['built_by'] or 'none'}",
        f"gate: {entry['gate'] or 'none'}", f"baseline: {land['baseline']}",
        f"candidate: {'yes' if entry['candidate'] else 'no'}", f"head: {entry['head']}",
    ])
    if entry["reason"] == "already_green":
        # The check passes on the base, not on the job that failed, so no gate claim is made (S9, I-B).
        verdict = ("The repo's check passes on the base; the CI run that failed may be a different job and may still be red, "
                   "so read this draft before closing the incident. It is not landed.")
    else:
        verdict = ("No review or gate has passed on it, so it is not landed and not landable as it stands: read it, then merge or "
                   "close it yourself.")
    return (
        f"Closes #{s.issue}\n\n"
        f"A draft from Pivotal for one slice of the issue. Its branch holds only that slice's commits on the base. {verdict}\n\n"
        f"{fenced(facts)}\n\n<!-- pivotal-run:{marker} -->\n"
    )


def open_pr(git: Remote, env: Mapping[str, str], s: Settings, *, branch: str, head: str, title: str, text: str, name: str,
            draft: bool) -> dict:
    """Push ``branch`` (never forced) and open a pull request for it, once. Returns {"status", "pr", "state"}; raises
    PublishError. The lookup by branch decides: an existing pull request is left alone, so a re-run opens no second one."""
    try:
        found = list_prs(env, s, branch)
        existing = pick_pr(found, head)
        if existing is not None:
            if existing["headRefOid"] != head:
                raise PublishError("pr_differs", {"pr": existing["number"], "state": existing["state"], "draft": existing["isDraft"]})
            return {"status": EXISTS, "pr": existing["number"], "state": existing["state"], "draft": existing["isDraft"]}
        if build.scan((title + "\n" + text).encode("utf-8"), ["GH_TOKEN"], {b"GH_TOKEN": s.token.encode()}).result != build.RESULT_OK:
            raise PublishError("secret_in_text")
        remote = git.network("ls-remote", git.url, f"refs/heads/{branch}").split()
        if remote and remote != [head, f"refs/heads/{branch}"]:
            # A branch left behind by a pull request that was closed at an older head is stale, not a stranger's.
            raise PublishError("branch_stale" if found else "branch_differs")
        if not remote:  # never forced: a branch that appeared since the lookup makes this a rejection
            try:
                git.network("push", "-q", "--no-recurse-submodules", git.url, f"{head}:refs/heads/{branch}")
            except assemble.GitError:
                raise PublishError("push_rejected") from None
        body = s.work_dir / f"body-{name}.md"
        assemble.write_new(body, text.encode("ascii"))
        created = job.gh(env, "pr", "create", "--repo", s.repo, *(["--draft"] if draft else []), "--base", s.default_branch,
                         "--head", branch, "--title", title, "--body-file", str(body), binary=s.gh)
        url = CREATED.fullmatch(created.strip())
        if url is not None and url["repo"] == s.repo:  # gh just made it: the URL it printed is the answer, whatever a list says
            return {"status": OPENED, "pr": int(url["number"]), "state": "OPEN", "draft": draft}
        made = find_pr(env, s, branch, head)
        if made is None or made["headRefOid"] != head or made["isDraft"] != draft:
            raise PublishError("pr_not_found")
        return {"status": OPENED, "pr": made["number"], "state": made["state"], "draft": made["isDraft"]}
    except assemble.GitError:
        raise PublishError("git_failed") from None
    except (JobError, OSError):
        raise PublishError("gh_failed") from None


def update_pr(git: Remote, env: Mapping[str, str], s: Settings, entry: Mapping) -> dict:
    """A fix round's draft: the branch of the slice goes from the old head (this run's base, which plan checked out) to the new one
    by a plain push (a fast-forward, never forced), and the OPEN pull request of the branch follows it. Nothing is opened:
    a branch with no open pull request at the old head, or one at any other head, is `pr_differs`. Returns {"status", "pr", "state",
    "draft"}; a re-run finds the branch and the pull request at the new head already and changes nothing."""
    branch, head, ref = entry["branch"], entry["head"], f"refs/heads/{entry['branch']}"
    try:
        live = [x for x in list_prs(env, s, branch) if x["state"] == "OPEN"]
        pr = min(live, key=lambda x: x["number"]) if live else None
        if pr is None or pr["headRefOid"] not in (s.base, head):
            raise PublishError("pr_differs", None if pr is None else {"pr": pr["number"], "state": pr["state"], "draft": pr["isDraft"]})
        done = {"pr": pr["number"], "state": "OPEN", "draft": pr["isDraft"]}
        if pr["headRefOid"] == head:
            return {"status": EXISTS, **done}
        tip = git.network("ls-remote", git.url, ref).split()
        if tip != [head, ref]:
            if tip != [s.base, ref]:  # the branch is not where the pull request was: never moved by this job
                raise PublishError("branch_differs")
            try:
                git.network("push", "-q", "--no-recurse-submodules", git.url, f"{head}:{ref}")
            except assemble.GitError:
                raise PublishError("push_rejected") from None
        fix_comment(env, s, done["pr"], head, entry["reason"])
        return {"status": UPDATED, **done}
    except assemble.GitError:
        raise PublishError("git_failed") from None
    except (JobError, OSError):
        raise PublishError("gh_failed") from None


def mark_ready(env: Mapping[str, str], s: Settings, done: dict) -> None:
    """Mark the pull request of a met fix round ready for review. A failure is PublishError (the branch and the pull request are as
    they were): the round is not reported as ready."""
    if not done.get("draft"):
        return
    try:
        job.gh(env, "pr", "ready", str(done["pr"]), "--repo", s.repo, binary=s.gh)
    except (JobError, OSError):
        raise PublishError("ready_failed") from None
    done["draft"] = False


def hill_best(s: Settings, entry: Mapping) -> tuple[float, str] | None:
    """(score, head) of the best round before this one, when this draft does not beat it: the draft then stays at that round's head,
    and nothing of this round is pushed. None when the draft is published (round 0, a draft that is not a score run's, or a round
    that beats the best so far). A tie keeps the earlier round (hill.best). Whatever the draft's reason is: a round that met the target
    but was held back by its verdict (or a conflict) must not move the draft off a better round either."""
    if not s.target or entry.get("score") is None:
        return None
    prior = hill.best(hill.decode_history(s.scores))
    if prior is None or entry["score"] > prior[0]:
        return None
    return prior


def keep_best(env: Mapping[str, str], s: Settings, entry: Mapping, best: tuple[float, str]) -> dict:
    """The draft of a round that is not the best: nothing is pushed. The pull request at the best round's head is the draft that
    stays, and a comment on it says what happened (best effort). ``state`` OPEN is what a fix round is sent from."""
    try:
        pr = find_pr(env, s, entry["branch"], best[1])
    except (JobError, OSError):
        raise PublishError("gh_failed") from None
    if pr is None or pr["state"] != "OPEN":
        raise PublishError("pr_differs", None if pr is None else {"pr": pr["number"], "state": pr["state"], "draft": pr["isDraft"]})
    text = (f"Pivotal round {s.round} (run {s.run_id}) scored {hill.score_text(entry['score'])}, below the best round's "
            f"{hill.score_text(best[0])}, so its commit was not pushed: the draft stays at the best round, {best[1]}. The climb stops here.\n")
    try:
        path = s.work_dir / f"kept-{pr['number']}.md"
        assemble.write_new(path, text.encode("ascii"))
        job.gh(env, "pr", "comment", str(pr["number"]), "--repo", s.repo, "--body-file", str(path), binary=s.gh)
    except (JobError, OSError):
        print(f"score: the comment on pull request #{pr['number']} could not be made (the draft was kept)", file=sys.stderr)
    return {"status": KEPT, "pr": pr["number"], "state": "OPEN", "draft": pr["isDraft"], "head": best[1]}


def fix_comment(env: Mapping[str, str], s: Settings, number: int, head: str, reason: str) -> None:
    """R13: the PR body is the round-0 one, so a fix push says on the PR what changed. Best effort: a comment that cannot be made
    is a line in the log, never a failure of the push that already happened. The text is ours, with no model or issue text in it.
    R17: "passed its check and review" only for `fix_round`; the draft's reason otherwise, as it is (a fixed word of the draft table)."""
    if reason == "score_met":
        outcome = "met the score target: it is not stacked, so merge it yourself (publish marks it ready for review after this comment)"
    else:
        outcome = ("passed its check and review" if reason == "fix_round" else f"did not pass: {reason}")
        outcome += ". It never lands by itself: read the diff, then merge it or close it yourself"
    text = (f"Pivotal pushed fix round {s.round} to this branch (run {s.run_id}); it is now at {head}. The body above is from round 0. "
            f"Fix round {s.round} {outcome}.\n")
    try:
        path = s.work_dir / f"comment-{number}.md"
        assemble.write_new(path, text.encode("ascii"))
        job.gh(env, "pr", "comment", str(number), "--repo", s.repo, "--body-file", str(path), binary=s.gh)
    except (JobError, OSError):
        print(f"fix round: the comment on pull request #{number} could not be made (the branch was pushed)", file=sys.stderr)


def publish_slice(git: Remote, env: Mapping[str, str], s: Settings, land: Mapping, entry: Mapping) -> dict:
    """Open the draft pull request of one draft slice, once (a fix round updates it instead: ``update_pr``). Returns
    {"status", "pr"}; raises PublishError. A fix round that met the score target (`score_met`) is then marked ready for review:
    it is never stacked, so landing it is still the person's own merge."""
    if s.round > 0:
        done = update_pr(git, env, s, entry)
        if entry["reason"] == "score_met":
            mark_ready(env, s, done)
        return done
    return open_pr(git, env, s, branch=entry["branch"], head=entry["head"], title=pr_title(s, entry),
                   text=pr_body(s, land, entry), name=entry["id"], draft=True)


# ------------------------------------------------------------------------------------------------ fix rounds


def read_text_file(directory_fd: int, name: str, limit: int) -> str:
    """A small regular file of an artifact as text (invalid UTF-8 replaced), or "" when it is not there or not readable."""
    try:
        return assemble.read_regular(Path(name), limit, directory_fd).decode("utf-8", "replace")
    except (assemble.ArtifactError, OSError):
        return ""


NOT_READABLE = "(not readable)"


def fix_inputs(s: Settings, ident: str) -> tuple[str, str]:
    """(the reviewer's text, the end of the build's summary) of one slice, both hostile: the review through ``review.load``
    (which proves it is the text the engine accepted), the summary as the build job's redacted file. A part that cannot be read
    is the visible marker ``(not readable)`` with a line in the log (R11), never an empty part that reads as "nothing to fix"."""
    verdict = NOT_READABLE
    directory = s.reviews_dir / f"review-{ident}"
    if os.path.lexists(directory):
        try:
            review.load(directory)
            verdict = (directory / "review.md").read_text(encoding="utf-8")
        except (review.ReviewError, OSError, ValueError, RecursionError):
            verdict = NOT_READABLE
    tail = NOT_READABLE
    try:
        outer = assemble.open_dir(str(s.slices_dir))
    except assemble.ArtifactError:
        outer = None
    if outer is not None:
        try:
            inner = assemble.open_dir(f"slice-{ident}", outer)
            try:
                tail = read_text_file(inner, assemble.SUMMARY, 256 * 1024) or NOT_READABLE
            finally:
                os.close(inner)
        except (assemble.ArtifactError, OSError):
            tail = NOT_READABLE
        finally:
            os.close(outer)
    for what, text in (("the reviewer's text", verdict), ("the build summary", tail)):
        if text == NOT_READABLE:
            print(f"fix round: slice {ident}: {what} could not be read, the findings say so", file=sys.stderr)
    return verdict, tail


def send_one(env: Mapping[str, str], s: Settings, entry: Mapping) -> str:
    """"sent", or the word that says why this slice's fix round was not sent. Nothing here raises."""
    ident = entry["id"]
    if fix.ISSUE_PIN.fullmatch(s.issue_pin) is None:  # F2b: a fix round without the pin of the text it was trusted on is not sent
        return "no_pin"
    try:
        verdict, tail = fix_inputs(s, ident)
        # The score history (engine/hill.py): every round built so far, this one last. Its lines are the findings' score section.
        history = [*hill.decode_history(s.scores), (entry["score"], entry["head"])] if s.target else []
        marks = [f"target {s.target}", *(f"round {i}: score {hill.score_text(sc)}" for i, (sc, _head) in enumerate(history))] if s.target else []
        findings = fix.findings_text(fix.kind_of(entry), verdict, tail, marks)
        data = fix.payload(issue=s.issue, slice_id=ident, round_number=s.round + 1, run_id=s.run_id, branch=entry["branch"],
                           head=entry["head"], issue_pin=s.issue_pin, scores=hill.encode_history(history), findings=findings)
        body = fix.request(data)
        if fix.leaks(body, s.token):
            return "secret"
        path = s.work_dir / f"fix-{ident}.json"
        assemble.write_new(path, body)
    except (JobError, OSError):
        return "dispatch_failed"  # before the request left: nothing was sent
    except Exception:  # noqa: BLE001 - best effort: nothing unexpected may skip the other slices, the result or the RESULT already earned
        return "unexpected"
    try:
        job.gh(env, *fix.dispatch_argv(s.repo, str(path)), binary=s.gh)
        return "sent"
    except job.GhRefused:
        return "dispatch_failed"  # R23: the server refused it, or gh never ran: nothing was started
    except (JobError, OSError):
        return "unknown"  # R7: the request was made and no good answer came back: the round may have been started
    except Exception:  # noqa: BLE001 - best effort: nothing unexpected may skip the other slices, the result or the RESULT already earned
        return "unexpected"


def send_fix_rounds(env: Mapping[str, str], s: Settings, land: Mapping, done: Mapping[str, dict], failures: Mapping[str, str],
                    words: dict[str, str] | None = None) -> dict[str, str]:
    """One dispatch per fix candidate, {slice: word}. See the module docstring. A word other than "sent" is why not:
    `max_rounds` (this run is the last round), `untrusted` (a stranger's run starts no trusted fix), `no_pr` (the slice's draft
    pull request is not open, or the slice failed to publish), `not_new` (this run neither opened nor updated the PR, so a re-run of
    publish does not send again), `unknown` (the request was made and its answer is not known), or a failure (`dispatch_failed`, `secret`, `unexpected`)."""
    words = {} if words is None else words  # the caller's dict, filled as each slice is decided (R27)
    max_rounds = hill.round_bound(bool(s.target), fix.MAX_ROUNDS)
    for entry in land["slices"]:
        if not fix.kind_of(entry):
            continue
        ident = entry["id"]
        if not fix.may_send(s.round, max_rounds):
            words[ident] = "max_rounds"
        elif not s.trusted:
            words[ident] = "untrusted"
        elif done.get(ident, {}).get("status") == KEPT:
            words[ident] = "below_best"  # the round was not pushed: the climb stops at the best round, which stays the draft
        elif ident in failures or done.get(ident, {}).get("state") != "OPEN":
            words[ident] = "no_pr"
        elif done[ident].get("status") not in (OPENED, UPDATED):
            words[ident] = "not_new"  # R7: this run neither opened nor updated the PR, so a re-run of publish sends nothing again (R19: RESULT says so)
        else:
            words[ident] = send_one(env, s, entry)
        say = print if words[ident] in FIX_QUIET else lambda m: print(m, file=sys.stderr)
        say(f"fix round: slice {ident} {'sent as round ' + str(s.round + 1) if words[ident] == 'sent' else 'not sent (' + words[ident] + ')'}")
    return words


FIX_QUIET = frozenset({"sent", "max_rounds", "untrusted", "no_pr", "not_new", "below_best"})  # said on stdout: not a failure
FIX_FAILED = frozenset({"dispatch_failed", "secret", "unexpected", "no_pin"})
FIX_UNKNOWN = frozenset({"unknown"})  # the dispatch was attempted and its answer is not known: RESULT "fix" word "unknown"
FIX_RERUN = frozenset({"not_new"})  # a re-run found the PR and sent nothing: an earlier run may have sent the round: RESULT "rerun"
# RESULT's "fix" is the words of the kinds of problem that occurred, space-separated, in this order (report.py reads them back).
FIX_RESULT_WORDS = (("failed", FIX_FAILED), ("unknown", FIX_UNKNOWN), ("rerun", FIX_RERUN))


def stopped_whys(words: Mapping[str, str]) -> dict[str, str]:
    """{slice: the draft's RESULT word} for a slice whose fix round was not sent for a reason the person must hear: the rounds ran
    out (`max_rounds`)."""
    return {k: "max_rounds" for k, v in words.items() if v == "max_rounds"}


def fix_result(words: Mapping[str, str]) -> str:
    """RESULT's "fix" for one run's {slice: word}: every kind of problem, not only the first one checked (R18). "" when none."""
    values = set(words.values())
    return " ".join(name for name, group in FIX_RESULT_WORDS if group & values)


# ------------------------------------------------------------------------------------------------ the stack


def push_args(url: str, sha: str, ref: str) -> list[str]:
    """The arguments of the one push that can move the base: ``<sha>:<ref>`` of a branch, as given, with no flag that forces,
    deletes, mirrors or leases and no ``+``. A fast-forward or git refuses it. Raises ValueError for anything else."""
    if assemble.SHA.fullmatch(sha) is None or not ref.startswith("refs/heads/") or ref.startswith("refs/heads/-") or "+" in ref:
        raise ValueError("not a plain push of a commit to a branch")
    return ["push", "-q", "--no-recurse-submodules", url, f"{sha}:{ref}"]


def remote_tip(git: Remote, ref: str) -> list[str]:
    """``[sha, ref]`` of the remote branch, or [] when it does not exist. PublishError("ff_unknown") when it cannot be read:
    the caller cannot say whether the base moved."""
    try:
        return git.network("ls-remote", git.url, ref).split()
    except assemble.GitError:
        raise PublishError("ff_unknown") from None


def fast_forward(git: Remote, s: Settings, sha: str) -> bool:
    """Move the default branch from the base to ``sha`` with a plain push. False (nothing moved, nothing retried) when the
    default branch is no longer at the base or the push is refused (it moved since, it is protected, it is not a
    fast-forward): the caller opens the stack PR instead. A push that fails is judged by reading the branch again: at
    ``sha`` it was accepted and only the answer was lost, so it is landed; anywhere else it was refused. When the branch
    cannot be read, nobody knows whether the base moved: PublishError("ff_unknown"), no pull request, no seal."""
    ref = f"refs/heads/{s.default_branch}"
    tip = remote_tip(git, ref)
    if tip == [sha, ref]:  # a re-run after the push: it is landed already, nothing to push
        return True
    if tip != [s.base, ref]:
        return False
    try:
        git.network(*push_args(git.url, sha, ref))
    except ValueError:
        return False
    except assemble.GitError:
        return remote_tip(git, ref) == [sha, ref]
    return True


def stack_body(s: Settings, land: Mapping, prefix: Prefix, reason: str) -> str:
    tail = [i for i in land["positions"] if i not in prefix.ids]
    marker = json.dumps({"run": s.run_id, "issue": s.issue, "slice": assemble.STACK, "head": prefix.tip}, separators=(",", ":"))
    facts = "\n".join([
        f"why a pull request: {reason} ({STACK_REASONS[reason]})",
        *([f"level held: {s.configured} as {s.level}: {', '.join(s.held)}"] if s.held else []), f"slices that passed: {', '.join(prefix.ids) or 'none'}",
        f"not in the landable prefix: {', '.join(tail) if tail else 'none'}",
        f"first slice not landed: {prefix.left or 'none'} ({prefix.why_not or 'n/a'})", f"baseline: {land['baseline']}", f"head: {prefix.sha or prefix.tip}",
    ])
    return (
        f"Closes #{s.issue}\n\n"
        f"The stack of slices for the issue. Those that passed the gate on a fresh machine each, with a ready verdict from "
        f"another vendor's reviewer, are the landable prefix; the branch holds every slice of the stack on the base, in order.\n\n"
        f"{fenced(facts)}\n\n"
        f"<!-- pivotal-run:{marker} -->\n"
    )


def create_seal(env: Mapping[str, str], s: Settings, prefix: Prefix) -> int:
    """The `pivotal/gate` check run on m, with this run's id as its external_id. Raises PublishError("seal_failed")."""
    try:
        body = seal.payload(prefix.sha, s.run_id, s.land_sha256, prefix.ids)
        path = s.work_dir / "seal.json"
        assemble.write_new(path, seal.seal_text(body))
        answer = job.gh_json(env, "api", "--method", "POST", f"repos/{s.repo}/check-runs", "--input", str(path), binary=s.gh)
        return seal.check_response(answer, prefix.sha, s.run_id)
    except (seal.SealError, JobError, OSError):
        raise PublishError("seal_failed") from None


def new_stack(s: Settings) -> dict:
    """What the publish job knows of the stack it is publishing. The job holds this dict itself, so whatever happens in
    ``publish_stack`` after the base moved, the job still knows that it moved (N1)."""
    return {"how": STACK_PR, "pr": 0, "state": "", "draft": False, "rest_error": "", "seal_error": "",
            "trusted": s.trusted}


def seal_prefix(env: Mapping[str, str], s: Settings, prefix: Prefix, done: dict) -> None:
    """Seal m. After the base moved, a seal that cannot be made is recorded in ``done["seal_error"]`` and the run goes on
    (the rest of the stack still gets its draft); before that it raises PublishError("seal_failed")."""
    try:
        made = create_seal(env, s, prefix)
    except Exception as err:  # noqa: BLE001 - after the base moved nothing unexpected may skip the rest or the result
        if done["how"] != LANDED:
            raise
        done["seal_error"] = err.reason if isinstance(err, PublishError) else "unexpected"
        print(f"stack: the seal failed ({done['seal_error']}) after the base moved", file=sys.stderr)
        return
    print(f"stack: sealed (check run {made} on {prefix.sha})")


def publish_stack(git: Remote, env: Mapping[str, str], s: Settings, land: Mapping, prefix: Prefix, done: dict | None = None) -> dict:
    """Land the prefix and/or open the stack pull request, then seal. Fills and returns ``done`` (see ``new_stack``: "how",
    "pr", "state", "draft", "rest_error", "seal_error"); raises PublishError.

    The prefix is all of the stack: it lands by fast-forward, or, when it cannot, goes into a READY stack pull request
    (a DRAFT for a stranger's run). The prefix is shorter than the stack (or empty): the whole stack goes on
    `pivotal/issue-<n>/stack` and a DRAFT stack pull request names the first slice that did not pass and why; the prefix,
    if any, still lands by itself first, and there is no ready pull request at m (the branch cannot be both). The base
    moves (or the pull request opens) first and the seal comes next, only after that succeeded and only for a prefix and
    only when the pull request at m is open and READY, so a seal never exists for a prefix nothing was done with. Once
    the base moved, nothing that follows is an error of the landing: a seal that fails or a draft that fails is in
    ``seal_error`` or ``rest_error`` (the draft is made whether or not the seal worked), the caller keeps ``done`` and the
    result still says landed."""
    done = new_stack(s) if done is None else done
    partial = len(prefix.ids) < prefix.total
    landed = bool(prefix.ids) and s.may_land and fast_forward(git, s, prefix.sha)
    done["how"] = LANDED if landed else STACK_PR
    if landed:
        print(f"stack: landed {prefix.sha} on {s.default_branch} ({len(prefix.ids)} of {prefix.total} slices)")
        seal_prefix(env, s, prefix, done)
    if not landed or partial:
        reason = "untrusted" if not s.trusted else "rest" if partial else (
            ("held" if job.aims_to_land(s.configured) else "level") if not s.may_land else "ff_rejected")
        print(f"stack: opening the stack pull request ({reason})")
        draft = not s.trusted or partial
        count = len(prefix.ids)
        title = STACK_TITLE.format(issue=s.issue, count=count, total=prefix.total)
        try:
            made = open_pr(git, env, s, branch=assemble.stack_branch(s.issue), head=prefix.tip, title=title,
                           text=stack_body(s, land, prefix, reason), name="stack", draft=draft)
        except Exception as err:  # noqa: BLE001 - once the base moved, nothing unexpected may lose the landing
            if not landed:
                raise
            if isinstance(err, PublishError) and err.existing:  # a READY PR at an older head still holds the unpassed slices: name it
                done.update(err.existing)
            done["rest_error"] = err.reason if isinstance(err, PublishError) else "unexpected"  # the draft's failure, not the landing's
            print(f"stack: the draft pull request for the rest failed ({done['rest_error']})", file=sys.stderr)
        else:
            done.update(pr=made["pr"], state=made["state"], draft=made["draft"])
            print(f"stack: {made['status']} (#{made['pr']})"
                  + (f" is a {'draft' if made['draft'] else 'ready pull request'}, a {'draft' if draft else 'ready one'} was wanted"
                     if made["draft"] != draft else ""))
            if s.may_seal and not landed and not partial and made["state"] == "OPEN" and not made["draft"]:
                seal_prefix(env, s, prefix, done)
    return done


# ------------------------------------------------------------------------------------------------ the result


def why_of(reason: str) -> str:
    return WHY_OF.get(reason, "build_failed")


# A failed slice's reason that outranks every other in the one RESULT line: a secret in a patch must not be hidden by a
# healthy sibling's draft or by an earlier, plainer failure.
FAIL_PRIORITY = ("secret_in_patch", "no_secret")


def built_failures(land: Mapping, done: Mapping[str, dict]) -> list[tuple[str, str]]:
    """(why, built_by) of each slice land.json says went no further than a report (failed, blocked, conflict) and publish
    did not open, in slice order. A draft and a candidate are not failures."""
    return [(why_of(e["reason"]), e["built_by"] or "none") for e in land["slices"]
            if e["outcome"] not in ("draft", "candidate") and e["id"] not in done]


# What the first reason a prefix stops short means for report.WHY; anything else is not_landable.
PREFIX_WHY = {"gate_red": "gate_red", "baseline_red": "baseline_red", "review_needs_fixes": "review_rejected"}


def stack_result(land: Mapping, prefix: Prefix, stack: dict) -> dict:
    """The RESULT of a stack that was landed or opened as a pull request. What the pull request IS (its real state) decides,
    not what was asked for."""
    built = prefix.built_by or "none"
    data = {"built_by": built, "reviewed_by": prefix.reviewed_by or "none"}
    partial = len(prefix.ids) < prefix.total
    if stack["how"] == LANDED:
        if partial and stack["pr"] and stack["state"] == "OPEN" and not stack["draft"]:  # a READY pull request at the tip holds the unpassed slice
            data.update(status="draft", why="partial_land_ready", pr=stack["pr"])  # the base moved and a READY PR holds the rest
            return data
        data.update({"status": "draft", "why": "partial_land"} if partial else {"status": "landed", "sha": prefix.sha})
        if stack["pr"]:
            data["pr"] = stack["pr"]
        return data
    data["pr"] = stack["pr"]
    if stack["state"] != "OPEN":
        data.update(status="draft", why="pr_closed")
    elif not stack["trusted"]:
        data.update(status="draft", why="untrusted_stack")
    elif partial and not stack["draft"]:  # a READY pull request holds slices that did not pass: said so, whatever the prefix
        data.update(status="draft", why="partial_stack_ready")
    elif partial:
        data.update(status="draft", why="partial_stack" if prefix.ids else PREFIX_WHY.get(prefix.why_not, "not_landable"))
    elif stack["draft"]:
        data.update(status="draft", why="stack_pr_draft")
    else:
        data.update(status="ready")
    return data


def result_line(land: Mapping, done: Mapping[str, dict], failures: Mapping[str, str], prefix: Prefix | None = None,
                stack: Mapping | None = None, stopped: Mapping[str, str] = {}) -> str:
    """The RESULT line engine/report.py reads, one answer whatever the slice order. A landed stack comes first (the base
    moved: landed stays landed). Then failures, so a healthy sibling or an opened stack never hides one: the stack's own
    (``failures["stack"]``); a slice that failed with a FAIL_PRIORITY reason; else the first slice, by slice order, that
    publish failed or that land.json says failed, was blocked or conflicted; the pull request that was opened stays the
    link shown. Only then the opened stack, a draft for the first slice with an OPEN pull request, else `pr_closed` for
    the first whose pull request is closed or merged, else the first slice.

    Every RESULT carries the fall-through items of the run (fell_back_items), a landed or opened stack included.
    """
    data = _result(land, done, failures, prefix, stack, stopped)
    items, more = fell_back_items(land)
    if items:
        data["fell_back"] = items
    if more:
        data["fell_back_more"] = more
    return json.dumps(data, separators=(",", ":"))


def fell_back_items(land: Mapping) -> tuple[list[dict], int]:
    """The fall-through items of every slice, in slice order, as the RESULT carries them, and how many did not fit.
    report.parse_result reads at most report.FELL_BACK_MAX items and refuses the whole RESULT beyond that, so the first
    that many travel and the rest are counted (fell_back_more), never dropped silently."""
    items = [item for entry in land["slices"] for item in entry["fell_back"]]
    return items[: report.FELL_BACK_MAX], min(max(len(items) - report.FELL_BACK_MAX, 0), report.FELL_BACK_MORE_MAX)


# What a RESULT is built from: the outcome of the run, as a dict; result_line writes it.
def _result(land: Mapping, done: Mapping[str, dict], failures: Mapping[str, str], prefix: Prefix | None,
            stack: Mapping | None, stopped: Mapping[str, str]) -> dict:
    if stack is not None and prefix is not None and stack["how"] == LANDED:  # the base moved: landed, whatever failed after it
        return stack_result(land, prefix, dict(stack))
    if "stack" in failures:  # nothing moved and the stack could not be opened or sealed: that is the answer, whatever the drafts did
        data = {"status": "failed", "why": FAIL_WHY.get(failures["stack"], "publish_failed")}
        if stack is not None and stack["pr"]:  # the pull request that was opened before the seal failed stays the link
            data["pr"] = stack["pr"]
        return data
    failed = built_failures(land, done)
    # The failure is the answer, but the pull request that was opened is still the link the comment shows.
    opened = [e for e in land["slices"] if e["id"] in done]
    shown = next((done[e["id"]]["pr"] for e in opened if done[e["id"]]["state"] == "OPEN"),
                 done[opened[0]["id"]]["pr"] if opened else 0)
    extra = {"pr": shown} if shown else {}
    for why in FAIL_PRIORITY:
        for found, built in failed:
            if found == why:
                return {"status": "failed", "why": why, "built_by": built, **extra}
    for entry in land["slices"]:
        if entry["id"] in failures:
            data = {"status": "failed", "why": FAIL_WHY.get(failures[entry["id"]], "publish_failed"), **extra}
            return data
        if entry["outcome"] not in ("draft", "candidate") and entry["id"] not in done:
            data = {"status": "failed", "why": why_of(entry["reason"]), "built_by": entry["built_by"] or "none", **extra}
            return data
    if stack is not None and prefix is not None:
        return stack_result(land, prefix, dict(stack))
    for entry in land["slices"]:
        if entry["id"] in done and done[entry["id"]]["state"] == "OPEN":
            built = entry["built_by"] or "none"
            if entry["reason"] == "score_met" and entry["reviewed_by"] and not done[entry["id"]]["draft"]:  # a met fix round: ready for review, never stacked
                data = {"status": "ready", "built_by": built, "reviewed_by": entry["reviewed_by"], "pr": done[entry["id"]]["pr"]}
                return data
            why = stopped.get(entry["id"]) or DRAFT_WHY.get(entry["reason"], "no_stack")
            data = {"status": "draft", "why": why, "built_by": built, "reviewed_by": "none", "pr": done[entry["id"]]["pr"]}
            return data
    closed = next((e for e in land["slices"] if e["id"] in done), None)
    if closed is not None:
        data = {"status": "draft", "why": "pr_closed", "built_by": closed["built_by"] or "none", "reviewed_by": "none",
                "pr": done[closed["id"]]["pr"]}
    else:
        first = land["slices"][0]
        if land.get("positions"):  # a stack that could not be landed or opened, whatever the reason, is not a build failure
            why = PREFIX_WHY.get(prefix.why_not if prefix else "", "not_landable")
            first = next(x for x in land["slices"] if x["id"] == land["positions"][0])
        else:  # no stack: check_land makes every candidate a position, so this is a slice that has no branch
            why = why_of(first["reason"])
        data = {"status": "failed", "why": why, "built_by": first["built_by"] or "none"}
    return data


def publish(env: Mapping[str, str]) -> None:
    s = read_settings(env)
    land, bundle = load_land(s)
    s = s.with_baseline(land["baseline"])
    if s.held:
        print(f"level: {s.configured} is held at {s.level}: {', '.join(s.held)}")
    git = fetch_drafts(s, land, bundle)  # every refusal of the run is raised above this line
    done: dict[str, dict] = {}
    failures: dict[str, str] = {}
    prefix: Prefix | None = None
    stack: dict | None = None
    if git is not None:
        gh = gh_env(s, git.home)
        prefix = landable_prefix(s, land, git)
        if land["positions"]:
            print(f"stack: {len(prefix.ids)} of {prefix.total} slices are landable" + (f" (stops at: {prefix.why_not})" if prefix.why_not else ""))
        if land["positions"]:
            held = new_stack(s)  # the job keeps this: an error after the base moved cannot make the landing unknown
            stack = held
            try:
                publish_stack(git, gh, s, land, prefix, held)
            except PublishError as err:
                failures["stack"] = err.reason
                print(f"stack: failed ({err.reason})", file=sys.stderr)
            except Exception:  # noqa: BLE001 - the base may have moved: nothing unexpected may skip the drafts or the result
                failures["stack"] = "unexpected"
                print("stack: failed (unexpected)", file=sys.stderr)
        for entry in land["slices"]:
            if entry["outcome"] != "draft":
                print(f"slice {entry['id']}: {SKIPPED} ({entry['outcome']}: {entry['reason']}) has no branch")
                continue
            try:
                best = hill_best(s, entry)
                done[entry["id"]] = publish_slice(git, gh, s, land, entry) if best is None else keep_best(gh, s, entry, best)
                print(f"slice {entry['id']}: {done[entry['id']]['status']} (#{done[entry['id']]['pr']})")
            except PublishError as err:
                failures[entry["id"]] = err.reason
                print(f"slice {entry['id']}: failed ({err.reason})", file=sys.stderr)
            except Exception:  # noqa: BLE001 - writes have happened: nothing unexpected may skip the other slices or the result
                done.pop(entry["id"], None)
                failures[entry["id"]] = "unexpected"
                print(f"slice {entry['id']}: failed (unexpected)", file=sys.stderr)
    else:
        for entry in land["slices"]:
            print(f"slice {entry['id']}: {SKIPPED} ({entry['outcome']}: {entry['reason']}) has no branch")
    if stack is not None and stack["rest_error"]:
        failures["stack/draft"] = stack["rest_error"]  # not a slice id (plan.ID_PATTERN has no slash)
    if stack is not None and stack["seal_error"]:
        failures["stack/seal"] = stack["seal_error"]
    words: dict[str, str] = {}
    if git is not None:
        try:  # after the drafts, whatever they did: best effort, and nothing here changes what the RESULT already says
            send_fix_rounds(gh, s, land, done, failures, words)
        except Exception:  # noqa: BLE001 - the base may have moved: the dispatches are never worth the answer
            for entry in land["slices"]:  # the words already decided stay; only an undecided candidate is "unexpected"
                if fix.kind_of(entry) and entry["id"] not in words:
                    words[entry["id"]] = "unexpected"
            print("fix round: failed (unexpected)", file=sys.stderr)
    line = result_line(land, done, failures, prefix, stack, stopped_whys(words))
    if fix_words := fix_result(words):
        line = json.dumps({**json.loads(line), "fix": fix_words}, separators=(",", ":"))
    try:
        job.write_outputs(s.output, {"result": line})
    except Exception:  # noqa: BLE001 - the base may have moved: the answer must reach somebody
        # Known gap: with no output the report job reads publish_failed, although main may have moved. The line is in the log.
        print(f"RESULT could not be written to the job outputs: {line}", file=sys.stderr)
        raise
    if failures:
        raise JobError(f"{len(failures)} slice(s) could not be published: {', '.join(sorted(failures))}")


JOBS = {"run": publish}


def main(argv: Sequence[str], environ) -> int:
    if len(argv) != 1 or argv[0] not in JOBS:
        print(f"usage: publish.py {{{'|'.join(JOBS)}}}", file=sys.stderr)
        return 2
    try:  # before anything else: this process holds the write token
        buildjob.make_unreadable()
    except JobError as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1
    return job.run_job(JOBS[argv[0]], buildjob.take_environment(environ))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
