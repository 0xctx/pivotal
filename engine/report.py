"""The redacted summary of an agent run (F3b) and the report job.

The raw agent log is never uploaded. The one log-derived artifact is this summary: the last lines of
the log, with every held secret value replaced by [REDACTED:NAME]. The result is scanned again, and
if any form of a value survives (a wrapped base64 string, for one), the whole summary is withheld.

`python3 -m engine.report summary --log FILE --out FILE --secret NAME...` writes the summary. It
prints `ok` or `withheld`, or `failed: scan_incomplete` when a named secret is not set.

`python3 -I pivotal/engine/report.py post` is the factory's last job (F7: you hear about every run). It has
``issues: write``, runs no repo code and no model output, and holds no secret but the ntfy topic variable.
ack decides whether the run is quiet: when its ``quiet`` output (``QUIET``) is ``true`` this job prints one
line and writes nothing; the level is not read again. (A run trust refused never reaches the factory, so this
job does not run for it.) Otherwise it posts one outcome comment, sets the status labels (DELETE of the status
labels the issue holds, POST of the outcome label; never a whole-set PUT), sends one ntfy message, writes the
step summary, and exits red when the outcome is a failure or any of its own writes failed. A re-run does not post
an identical comment twice but still sends the message.

The outcome is decided by code from validated inputs, never by a model. ``ACK_RESULT`` and ``PLAN_RESULT``
are the ``needs.<job>.result`` values; anything but ``success`` is ``failed``. So are the later jobs' results,
``BUILD_RESULT``, ``REVIEW_RESULT``, ``ASSEMBLE_RESULT``, ``GATE_RESULT`` and ``PUBLISH_RESULT`` (``needs.<job>.result``) when the
workflow sets them (``MODELS_UP_RESULT`` and ``KAGGLE_WHY``, from the models-up job, add one fixed line and never change the outcome): a value that is present and not ``success`` is ``failed (<job>_failed)``. The build, review,
assemble, gate and publish jobs exist and are wired: the report job ``needs`` each and its env holds each ``*_RESULT``
(test_factory fails when a job exists without both). The publish job sets ``RESULT`` from its output. Nothing may be ``landed`` unless publish ran, and a failed gate job (a red slice) does not hide a landing publish made: see ``decide``. ``RESULT`` is one line of JSON (printable ASCII, so job.OUTPUT_RULES["result"] accepts it)
the pipeline's last job (publish) puts in its outputs, at most RESULT_MAX characters:

    {"status": "landed|ready|draft|failed", "why": "<token>", "built_by": "<worker:door/model[@effort]>",
     "reviewed_by": "<same>", "fell_back": [{"from": "<spec>", "to": "<spec>|no_route", "reason": "<skip reason>"}],
     "cost": "$1.25", "pr": 12, "sha": "<40 hex>"}

Every agent is a route spec (``route.clean_spec``: the grammar ``route.parse_entry`` accepts, so models with ``/``
pass) or ``none``. ``landed`` and ``ready`` need a ``built_by`` and a ``reviewed_by`` other than ``none``; ``ready``
needs a ``pr``; ``landed`` needs a ``pr`` or the landed ``sha``; a result without them is ``failed (bad_result)``.
``why`` is required for ``draft`` and ``failed`` and is one of WHY. An unknown key, status, why or shape, a
result that is missing, oversized or not JSON, is ``failed (no_result)`` or ``failed (bad_result)``: a result
that cannot be read is never landable (F5).

When ack or plan (or a later job) did not succeed the job never reads the issue and never prints its title: the
issue may have been edited after trust decided, which is why ack refused. It names ``issue #N`` and the fixed
table text, and sets labels by number. Only a run that got through ack and plan reads the title.

The message is the report's own, ``<repo> · <title> · <outcome> · built by X,
reviewed by Y[, fell back from Z (why)][, cost C] · <link>``, one line, built from the title (cleaned by
``job.clean_text``), the repo and numeric ids only; the next step comes from a fixed table and goes into the
comment and the summary. The link is the PR when the result names one, else the run.

The comment is not posted twice when a re-run (``RUN_ATTEMPT`` above 1) would post the same body the bot already
posted, and then no second message is sent either. Labels are set exactly: the issue's labels without the
four status labels, plus the one outcome label (landed, ready, or blocked for a draft or a failure).
The ntfy topic (``PIVOTAL_NTFY``) is never printed. Unset it is a logged skip; an invalid one or a failed send
is reported and makes the job red, and never stops the comment.
"""

import argparse
import bisect
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote

if __package__ in (None, ""):  # run as a script under python3 -I
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import build, job, plan, route  # noqa: E402
from engine.job import JobError  # noqa: E402

SUMMARY_LINES = 200


@dataclass
class Summary:
    text: str
    withheld: bool = False


# A base64 run for the left edge of a match stops at "=" (padding ends a string); on the right it
# runs through it.
_LEFT_RUN = re.compile(rb"[A-Za-z0-9+/_-]+")
_RIGHT_RUN = re.compile(rb"[A-Za-z0-9+/_=-]+")


def _run_bounds(pattern: re.Pattern[bytes], log: bytes) -> tuple[list[int], list[int]]:
    spans = [m.span() for m in pattern.finditer(log)]
    return [a for a, _ in spans], [b for _, b in spans]


def _redact(log: bytes, values: Mapping[str, bytes]) -> bytes:
    """Replace every form of every value, and the whole base64 run around a base64 form, in one
    linear pass: find each needle, widen the span over its run, merge the spans, rewrite once."""
    left_starts, _ = _run_bounds(_LEFT_RUN, log)
    right_starts, right_ends = _run_bounds(_RIGHT_RUN, log)
    spans: list[tuple[int, int, str]] = []
    for name, value in values.items():
        for needle, kind in build.forms(value).items():
            at = log.find(needle)
            while at != -1:
                start, end = at, at + len(needle)
                if kind.startswith("base64"):
                    i = bisect.bisect_right(left_starts, start) - 1
                    if i >= 0:
                        start = min(start, left_starts[i])
                    j = bisect.bisect_right(right_starts, end - 1) - 1
                    if j >= 0:
                        end = max(end, right_ends[j])
                spans.append((start, end, name))
                at = log.find(needle, at + 1)
    merged: list[list] = []
    for start, end, name in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, name])
    out = bytearray()
    pos = 0
    for start, end, name in merged:
        out += log[pos:start] + f"[REDACTED:{name}]".encode()
        pos = end
    out += log[pos:]
    return bytes(out)


def redacted_summary(
    log: bytes,
    values: Mapping[str, bytes],
    skipped: Sequence[str] = (),
    lines: int = SUMMARY_LINES,
) -> Summary:
    """The last `lines` lines of log with each held value replaced, or a withheld line."""
    redacted = _redact(log, values)
    leaks = build.leaks_in([redacted, build.squash(redacted)], values)
    if leaks:
        names = ", ".join(sorted({name for name, _ in leaks}))
        return Summary(f"summary withheld: {names} still appear in the log after redaction\n", True)
    text = redacted.decode("utf-8", errors="replace").splitlines()
    shown = text[-lines:]
    header = f"agent log: {len(text)} lines, last {len(shown)} shown; secrets redacted"
    if skipped:
        header += (
            f"; not redacted (shorter than {build.MIN_SECRET_BYTES} bytes): {', '.join(skipped)}"
        )
    return Summary("\n".join([header, *shown]) + "\n")


# ---- the report job ----

RESULT_MAX = job.RESULT_MAX
TITLE_MAX = 80
BOT = "github-actions[bot]"
NTFY_URL = "https://ntfy.sh/"
NTFY_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")
NTFY_TIMEOUT_SECONDS = 30
COST = re.compile(r"\$[0-9]{1,6}(?:\.[0-9]{1,4})?")
RESULT_KEYS = frozenset({"status", "why", "built_by", "reviewed_by", "fell_back", "fell_back_more", "cost", "pr", "sha", "fix"})
# publish's note on the fix-round dispatches: only a failure is said (the outcome is what it was; the loop did not go on).
FIX_FAILED_NOTE = ("a fix round was not started: publish could not send the pivotal-fix dispatch (read the publish log); "
                   "re-running publish never sends it again, so comment /pivotal fix on the issue to start a new run")
FIX_UNKNOWN_NOTE = "a fix round may have been started: publish's pivotal-fix dispatch got no answer (look at the Actions tab before you re-run)"
FIX_RERUN_NOTE = ("a fix round may have been started by an earlier run of publish: this run sent none, because a re-run never sends "
                  "twice (check the Actions tab for a pivotal-fix run before you re-run)")
FIX_WORDS = ("failed", "unknown", "rerun")  # RESULT "fix": one or more of these, space-separated (publish.fix_result)
# Six slices (F8), each a build and a review that skipped two entries, is 24: the cap a RESULT carries. Items beyond it are
# counted in RESULT's fell_back_more, never dropped without a word.
FELL_BACK_MAX = 24
FELL_BACK_MORE_MAX = 9999
SHOWN_FELL_BACK = 3
# The words of a failed publish RESULT that reach the report as they are: publish's own tables (FAIL_WHY, WHY_OF, build_failed).
# Read lazily (module attribute ``PUBLISH_WHY``): publish imports buildjob, which imports this module.
def publish_why() -> frozenset:
    from engine import publish  # noqa: PLC0415 - a cycle otherwise

    return publish.PUBLISH_WHY


def __getattr__(name: str):
    if name == "PUBLISH_WHY":
        return publish_why()
    raise AttributeError(name)


# The jobs report waits for, in pipeline order. ack and plan exist; the rest are later tickets and are read from
# <NAME>_RESULT only when the workflow sets it.
STAGES = ("ack", "plan", "build", "review", "assemble", "gate", "publish")
# The words the plan job's ``plan_why`` output may carry (engine/plan.py): the refusals that name themselves instead of
# reading as ``plan_failed``. Every one is a key of WHY; any other word, and none, leaves the generic reason.
PLAN_WHY = frozenset({plan.REQUEST_TOO_LARGE})

# The reasons a run is a draft or a failure, and the next step for each: the only text a message or comment
# carries about why. A token not in this table is a bad result.
WHY = {
    "gate_red": "open the draft PR, read the gate log, and fix it or close it",
    "review_rejected": "open the draft PR and read the review verdict before deciding",
    "conflict": "the change conflicts with the base or the stack: rebase the draft PR or re-run after merging; if a draft PR is open it is unchecked against the stack",
    "workflow_change": "the change edits workflow files, which Pivotal never lands: review and apply it yourself",
    "agent_no_output": "the agent printed nothing and changed nothing: check the entry's endpoint (for a Kaggle entry, the models-up job's session) and re-run",
    "score_low": "the score is below the target: raise it by hand or re-run with a clearer goal",
    "below_best": "this round met the target but an earlier round scored higher, so that one stays the draft PR: read it, then merge or close it yourself",
    "ready_failed": "the pull request holds the fix, but it could not be marked ready for review: mark it ready yourself, or re-run publish",
    "fix_round": "a fix round passed its gate and its review, and the draft PR now holds the fix: read it, then merge it or mark it ready; a fix round never lands by itself, and a stack conflict that started it is not rechecked",
    "stack_differs": "the patch applied on the base but makes another change on the stack, so it was not stacked: the draft PR holds it, read it and rebase it yourself",
    "multi_commit": "the patch is more than one commit, which the stack does not take: the draft PR holds it, read it, then merge or close it yourself",
    "gate_none": "no check ran on the change (the repo has none or it did not run), so it was not stacked: set PIVOTAL_CHECK, or read the draft PR and decide yourself",
    "verdict_missing": "the draft PR is open and its review left no verdict: read the draft PR, or re-run for a review",
    "verdict_invalid": "the draft PR is open and its review verdict could not be read: read the draft PR, or re-run for a review",
    "verdict_stale": "the draft PR is open and its review verdict is for another round or base: read the draft PR, or re-run for a review",
    "verdict_wrong_sha": "the draft PR is open and its review verdict is for another patch: read the draft PR, or re-run for a review",
    "verdict_files": "the draft PR is open and its review verdict names other files than the patch changes: read the draft PR, or re-run for a review",
    "verdict_wrong_builder": "the draft PR is open and the review verdict is for a patch of another builder: read the draft PR, or re-run for a review",
    "max_rounds": "the fix rounds ran out: open the best draft PR and finish it by hand",
    "secret_in_patch": "the agent put a secret into its patch and the patch was discarded: rotate that secret now",
    "no_route": "no model in the preset could run: check the secrets and the preset with piv doctor",
    "no_secret": "the first model of the preset has no secret in this repo: set it with piv init or fix the preset, and check with piv doctor",
    "agent_error": "the agent failed: read the run log and re-run",
    "no_stack": "the draft PR is open and nothing has reviewed or checked it: read it, then merge or close it yourself",
    "pr_closed": "the draft PR was closed or merged earlier and is not open: reopen it by hand; a new run cannot open another for the same commit",
    "branch_stale": "a closed PR left its branch pivotal/issue-<n>/<slice> at an older commit and Pivotal never forces: delete that branch, then re-run",
    "candidate_only": "a candidate outside any stack was found and nothing was published for it (a hand-made land.json): read the run log",
    "not_landable": "no slice of the stack passed the gate (a check, a review or the baseline): nothing was landed; the draft stack PR holds it, read the gate and review artifacts of the run",
    "partial_land": "only the first slices of the stack passed the gate and they are on the default branch: the rest did not pass, read the run log and re-run for them",
    "partial_land_ready": "the first slices of the stack are on the default branch and a READY stack PR at the tip holds the slices that did not pass the gate: mark it as a draft or close it before anyone merges it, then re-run for the rest",
    "partial_stack_ready": "the stack PR holds slices that did not pass the gate and is marked ready: mark it as a draft or close it before anyone merges it; whatever passed is on the default branch or in that PR",
    "partial_stack": "only the first slices of the stack passed the gate and the stack PR holds just those: merge it, then re-run for the rest",
    "untrusted_stack": "a stranger started this run, so the stack PR is a draft and nothing was landed or sealed: read it, then merge or close it yourself",
    "seal_failed": "the change was landed or opened but the pivotal/gate check could not be created: read the run log; the land job will not trust it without the seal",
    "stack_pr_draft": "the stack passed the gate but its PR exists as a draft: mark it ready for review yourself, then merge it",
    "pr_differs": "a pull request for the branch is already open at another commit than this run's (not Pivotal's): close it, then re-run",
    "branch_differs": "the branch pivotal/issue-<n>/<slice or stack> exists at another commit and Pivotal never forces: delete it or rename it away, then re-run",
    "push_rejected": "GitHub rejected the push of the branch (it appeared since the check, or a rule blocks it): look at the branch and the rules, delete a stale branch, then re-run",
    "gh_failed": "gh could not list or open the pull request: check the token's pull-requests permission and that GitHub is up, then re-run",
    "ff_unknown": "the push to the default branch got no answer and the branch could not be read afterwards: look at the default branch before anything else, then re-run",
    "baseline_red": "the base branch fails its own check: fix the base, then re-run",
    "already_green": "the repo's check passes on the base; the CI run that failed may be a different job and may still be red, so read this draft before closing the incident",
    "publish_failed": "the result could not be published: read the run log and re-run",
    "build_failed": "the build job did not finish: read the run log and re-run",
    "review_failed": "the review job did not finish: read the run log and re-run",
    "assemble_failed": "the patches could not be assembled: read the run log and re-run",
    "gate_failed": "the gate job did not finish: read the run log and re-run",
    "ack_failed": "the run was refused or broke at its start: read the run log",
    "plan_failed": "the run could not be planned: read the run log",
    # plan's own word for the one refusal that names itself (engine/plan.py ``plan_why``): anything else is plan_failed.
    "request_too_large": "the issue's title or body is too long for one slice: cut the body to at most 4000 characters (and the title to 72), then run again",
    "cancelled": "the run was cancelled: re-run when ready",
    "no_result": "the pipeline gave no result: read the run log and re-run",
    "bad_result": "the pipeline's result could not be read: read the run log and re-run",
}
NEXT = {
    "landed": "nothing: it is on the default branch; check it when you like",
    "ready": "open the PR, check it, and land it",
}
LABEL = {"landed": "pivotal:landed", "ready": "pivotal:ready", "draft": "pivotal:blocked", "failed": "pivotal:blocked"}
STATUS_LABELS = ("pivotal:building", "pivotal:ready", "pivotal:landed", "pivotal:blocked")


@dataclass(frozen=True)
class Outcome:
    status: str  # landed, ready, draft or failed
    why: str = ""
    built_by: str = "none"
    reviewed_by: str = "none"
    fell_back: tuple[tuple[str, str, str], ...] = ()
    fell_back_more: int = 0  # items beyond FELL_BACK_MAX that the RESULT could not list
    cost: str = ""
    pr: int = 0
    sha: str = ""
    fix_unknown: bool = False  # RESULT "fix": "unknown": the dispatch was made and its answer is not known
    fix_failed: bool = False  # RESULT "fix" "failed": a fix-round dispatch could not be sent; the outcome is unchanged
    fix_rerun: bool = False  # RESULT "fix" "rerun": a re-run found the fix round's PR and sent nothing: an earlier run may have
    note: str = ""  # a problem the outcome does not change but the report must show (the publish job failed after it moved the base)

    @property
    def phrase(self) -> str:
        return {"landed": "landed", "ready": "ready for you"}.get(self.status, f"{self.status} ({self.why})")

    @property
    def next_step(self) -> str:
        return NEXT.get(self.status) or WHY[self.why]

    @property
    def red(self) -> bool:
        return self.status == "failed"


def failed(why: str) -> Outcome:
    return Outcome("failed", why)


def parse_result(text: str) -> Outcome:
    """The Outcome of the RESULT line. Missing is ``no_result``, anything unreadable is ``bad_result``."""
    if not text.strip():
        return failed("no_result")
    if len(text) > RESULT_MAX:
        return failed("bad_result")
    try:
        data = json.loads(text)
    except ValueError:
        return failed("bad_result")
    try:
        return _read_result(data)
    except (KeyError, TypeError, ValueError):
        return failed("bad_result")


def _agent(who: object, allow_none: bool = True) -> str:
    """A route spec re-emitted from route.parse_entry, or ``none``; anything else is a ValueError."""
    if who == "none" and allow_none:
        return who
    return route.clean_spec(who)


def _read_result(data: object) -> Outcome:
    if not isinstance(data, dict) or not set(data) <= RESULT_KEYS:
        raise ValueError("shape")
    status = data["status"]
    if status not in LABEL:
        raise ValueError("status")
    why = data.get("why", "")
    if status in ("draft", "failed"):
        if why not in WHY:
            raise ValueError("why")
    elif why != "":
        raise ValueError("why")
    built, reviewed = (_agent(data.get(key, "none")) for key in ("built_by", "reviewed_by"))
    cost = data.get("cost", "")
    if not isinstance(cost, str) or (cost and COST.fullmatch(cost) is None):
        raise ValueError("cost")
    pr = data.get("pr", 0)
    if isinstance(pr, bool) or not isinstance(pr, int) or not 0 <= pr <= 9_999_999_999:
        raise ValueError("pr")
    listed = data.get("fell_back", [])
    if not isinstance(listed, list) or len(listed) > FELL_BACK_MAX:
        raise ValueError("fell_back")
    more = data.get("fell_back_more", 0)
    if isinstance(more, bool) or not isinstance(more, int) or not 0 <= more <= FELL_BACK_MORE_MAX:
        raise ValueError("fell_back_more")
    fell = []
    for item in listed:
        if not isinstance(item, dict) or set(item) != {"from", "to", "reason"}:
            raise ValueError("fell_back item")
        src, dst, reason = item["from"], item["to"], item["reason"]
        src = _agent(src, allow_none=False)
        dst = dst if dst == "no_route" else _agent(dst, allow_none=False)
        if reason not in route.SKIP_REASONS:
            raise ValueError("fell_back reason")
        fell.append((src, dst, reason))
    sha = data.get("sha", "")
    if not isinstance(sha, str) or (sha and job.SHA.fullmatch(sha) is None):
        raise ValueError("sha")
    if status in ("landed", "ready"):
        if built == "none" or reviewed == "none":
            raise ValueError("no builder or reviewer")
        if not (pr or (sha and status == "landed")):
            raise ValueError("nothing to point at")
    fix = data.get("fix", "")
    words = fix.split(" ") if isinstance(fix, str) and fix else []
    if not isinstance(fix, str) or any(w not in FIX_WORDS for w in words) or len(set(words)) != len(words):
        raise ValueError("fix")
    return Outcome(status, why, built, reviewed, tuple(fell), more, cost, pr, sha, fix_failed="failed" in words,
                   fix_unknown="unknown" in words, fix_rerun="rerun" in words)


# The RESULTs that stand over a red job: the base moved ("moved") or a pull request was opened and the base did not ("opened").
def _stands(kept: Outcome) -> str:
    """"moved", "opened" or "": landed, ready, the draft `partial_land` and `partial_land_ready` say the base moved (the last
    also names a READY PR at the tip), `partial_stack_ready` says a READY PR was opened and nothing landed. Any other draft
    or failure says nothing moved."""
    if kept.status == "landed" or (kept.status == "draft" and kept.why in ("partial_land", "partial_land_ready")):
        return "moved"
    if kept.status == "ready" or (kept.status == "draft" and kept.why == "partial_stack_ready"):
        return "opened"
    return ""


def _stand_note(kept: Outcome, gate_failed: bool, publish_failed: bool) -> str:
    what = "it moved the base" if _stands(kept) == "moved" else "it opened a pull request (the base did not move)"
    if gate_failed and publish_failed:
        return (f"the gate job failed (a slice's gate was red or it did not finish) and the publish job failed after {what} "
                "(a draft or the seal was not made): read the gate log and the publish log")
    if gate_failed:
        return f"the gate job failed (a slice's gate was red or it did not finish), and publish went on (after {what}): read the gate log"
    return f"the publish job failed after {what} (a draft or the seal was not made): read its log"


def decide(upstream: Mapping[str, str | None], result: str, has_stack: str = "", plan_why: str = "") -> Outcome:
    """The outcome of ``_decide``, with the fix-round words of the RESULT carried over whichever outcome it is (R25): a
    dispatch publish sent, or could not send, is true whatever the run then came to."""
    outcome = _decide(upstream, result, has_stack, plan_why)
    kept = parse_result(result)
    return replace(outcome, fix_failed=outcome.fix_failed or kept.fix_failed,
                   fix_unknown=outcome.fix_unknown or kept.fix_unknown, fix_rerun=outcome.fix_rerun or kept.fix_rerun)


def _decide(upstream: Mapping[str, str | None], result: str, has_stack: str = "", plan_why: str = "") -> Outcome:
    """The outcome from the upstream job results and the RESULT line.

    ``upstream`` maps a stage name to its ``needs.<job>.result``; ack and plan are always there, the others only when
    the workflow wired them (None is not wired). A job that did not succeed fails the run, whatever the result says.
    The one exception: the gate job is skipped when assemble stacked nothing, so ``gate`` may be ``skipped`` when
    ``upstream["assemble"]`` is exactly ``success`` (checked here, not inferred from STAGES order, where an assemble
    that is not wired would pass) and it reported ``has_stack`` as exactly ``false``; any other value (empty, ``true``,
    garbage) keeps a skipped gate a failure. The second: a publish job that failed AFTER it moved the base (or opened the
    PR) writes its RESULT first, so a RESULT of `landed`, `ready`, `partial_land`, `partial_land_ready` or `partial_stack_ready` stands over the job's failure (the
    issue must not be labelled blocked while the default branch has moved), with ``Outcome.note`` saying that the job
    failed. Any other result, or a failure of any other job, keeps the failure.

    ``plan_why`` is the plan job's ``plan_why`` output: a word of its own for the one refusal that must name itself
    (``request_too_large``) instead of reading as ``plan_failed``. Any other word, and none at all, is ``plan_failed``.
    """
    for name in STAGES:
        value = upstream.get(name)
        if value is None and name not in ("ack", "plan"):
            continue
        if value == "cancelled":
            return failed("cancelled")
        if name == "gate" and value == "skipped" and has_stack == "false" and upstream.get("assemble") == "success":
            continue
        if name == "gate" and value == "failure" and upstream.get("publish") in ("success", "failure"):
            # A red slice makes the gate job red by design, and publish still lands the green prefix: a RESULT that says
            # the base moved or a pull request was opened stands over the gate's failure. Without one it is gate_failed.
            kept = parse_result(result)
            if _stands(kept):
                return replace(kept, note=_stand_note(kept, True, upstream.get("publish") == "failure"))
        if name == "publish" and value == "failure":
            kept = parse_result(result)  # the base may have moved before a later step of the job failed: the result says so
            if _stands(kept):
                return replace(kept, note=_stand_note(kept, False, True))
            if kept.status == "failed" and kept.why in publish_why():  # publish said what went wrong: that word, not a generic one
                return kept
        if value != "success":  # failure, skipped, empty, or a value that is none of the four
            if name == "plan" and plan_why in PLAN_WHY:
                return failed(plan_why)
            return _refine(failed(f"{name}_failed"), name, result)
    return parse_result(result)


# The stages whose failure is a failed slice, which publish names in RESULT: the job is red by design (build, review) or
# because it carried the failure on (assemble, gate), so its own word says less than publish's.
SLICE_STAGES = ("build", "review", "assemble", "gate")
# The reasons publish gives a failed slice (engine/publish.py WHY_OF and FAIL_WHY, bar the generic build_failed): the only
# ones that may replace ``<stage>_failed``. candidate_only and publish_failed say less about the slice, not more.
SLICE_FAILURE_WHY = frozenset({
    "workflow_change", "conflict", "secret_in_patch", "no_route", "no_secret", "gate_red", "baseline_red", "branch_stale",
    "agent_no_output",
})


def _refine(outcome: Outcome, stage: str, result: str) -> Outcome:
    """A failed slice stage keeps the reason publish found (secret_in_patch, no_secret, ...) instead of ``<stage>_failed``.

    Only ever a failure for another failure, and only by a reason in SLICE_FAILURE_WHY: a result that is a draft, landed,
    ready, unreadable, generic or `candidate_only` leaves ``<stage>_failed`` as it is, and a failure of ack or plan is never refined, so a result can not make a run green or
    hide that it never got going. The fall-through items of the RESULT are kept for every stage, publish included."""
    found = parse_result(result)
    if stage not in SLICE_STAGES or found.status != "failed" or found.why not in SLICE_FAILURE_WHY:
        # The reason stays, the facts of the run do not: the fall-through that led here is shown beside it (R5).
        return replace(outcome, fell_back=found.fell_back, fell_back_more=found.fell_back_more)
    return found


def outcome_line(o: Outcome) -> str:
    """``<outcome> · built by X, reviewed by Y[, fell back from Z (why)][, cost C]``, the part after the title."""
    who = f"built by {o.built_by}, reviewed by {o.reviewed_by}"
    for src, _dst, reason in o.fell_back[:SHOWN_FELL_BACK]:
        who += f", fell back from {src} ({reason})"
    unseen = max(len(o.fell_back) - SHOWN_FELL_BACK, 0) + o.fell_back_more
    if unseen:
        who += f", and {unseen} more fell back"
    if o.cost:
        who += f", cost {o.cost}"
    return f"{o.phrase} · {who}"


def link_of(repo: str, o: Outcome, run: str) -> str:
    """The PR when the outcome names one, else the landed commit, else the run: always built from validated parts."""
    if o.pr:
        return f"{job.ROOT_LINK}{repo}/pull/{o.pr}"
    if o.sha:
        return f"{job.ROOT_LINK}{repo}/commit/{o.sha}"
    return run


def message_text(repo: str, title: str, o: Outcome, link: str) -> str:
    return f"{repo} · {title} · {outcome_line(o)} · {link}"


def kaggle_line(env: Mapping[str, str]) -> str:
    """One fixed line about the models-up job, or "" when it did not run (skipped or not wired).

    ``MODELS_UP_RESULT`` is ``needs.models-up.result`` and ``KAGGLE_WHY`` its ``kaggle_why`` output. Only a word of
    ``modelsup.WHY`` is ever printed (its fixed text, never the value), so the output cannot carry text from anywhere else;
    an empty or unknown word (the up step died before it wrote one) says so. This line informs: it never changes the
    outcome, so nothing becomes landed or green by a word here (decide() does not see it).
    """
    result = env.get("MODELS_UP_RESULT", "")
    if result in ("", "skipped"):
        return ""
    from engine import modelsup  # noqa: PLC0415 - modelsup imports job

    word = env.get("KAGGLE_WHY", "")
    if word in modelsup.WHY:
        # "ok" only when the job itself succeeded: a word left by a job that then failed or was cancelled is not ok.
        good = result == "success" and word in modelsup.OK_WHY
        line = f"kaggle: {'ok' if good else 'not ok'} ({word}): {modelsup.WHY[word]}"
        if result != "success":
            line += f" [the models-up job ended {result}]"
        if good:
            line += " [the keyless route check is off by default: ok means a server answered with the key]"
        return line
    ended = f"with {result}" if result in ("success", "failure", "cancelled") else "unclear"
    return f"kaggle: the models-up job ended {ended} and recorded no reason; each build checked the endpoint again"


FIX_LINE = "fix: "


def without_fix_lines(body: str) -> str:
    """A comment body without its fix-round lines: a re-run of the same outcome is the same comment, whatever fix words
    the first run and the re-run came to (the first run sent the round, the re-run found it sent)."""
    return "\n".join(line for line in body.splitlines() if not line.startswith(FIX_LINE))


def comment_body(run_number: int, o: Outcome, run: str, pr_link: str, kaggle: str = "") -> str:
    lines = [f"🏁 run #{run_number} {outcome_line(o)}"]
    lines += [route.FellBack(src, dst, reason).text for src, dst, reason in o.fell_back]  # the one line route.py prints
    if o.fell_back_more:
        lines.append(f"{o.fell_back_more} more fell back and are not listed")
    if kaggle:
        lines.append(kaggle)
    lines += [f"{FIX_LINE}{note}" for flag, note in ((o.fix_failed, FIX_FAILED_NOTE), (o.fix_unknown, FIX_UNKNOWN_NOTE),
                                                 (o.fix_rerun, FIX_RERUN_NOTE)) if flag]
    lines.append(f"next: {o.next_step}")
    if pr_link:
        lines.append(f"pr: {pr_link}")
    lines.append(f"run: {run}")
    return "\n".join(lines)


def summary_text(title: str, o: Outcome, comment: str, problems: Sequence[str]) -> str:
    out = [f"## {o.phrase}", "", f"**{title}**", ""]
    out += [f"- {line}" for line in comment.splitlines()[1:]]
    out += [f"- problem: {p}" for p in problems if p not in comment]  # a fix note is already a line of the comment
    return "\n".join(out) + "\n"


def already_posted(env: Mapping[str, str], repo: str, number: int, body: str) -> bool:
    """True when the bot already posted exactly this body on the issue (a re-run of the same outcome). Reading only."""
    out = job.gh(
        env, "api", "--paginate", f"repos/{repo}/issues/{number}/comments?per_page=100",
        "--jq", ".[] | {login: .user.login, body}",
    )
    for line in out.splitlines():
        try:
            comment = json.loads(line)
        except ValueError:
            raise JobError("the comment list is not JSON") from None
        if isinstance(comment, dict) and comment.get("login") == BOT and isinstance(comment.get("body"), str) \
                and without_fix_lines(comment["body"]) == without_fix_lines(body):
            return True
    return False


def notify(env: Mapping[str, str], message: str) -> str | None:
    """Send the message to the ntfy topic. None when sent or skipped, else the problem (never naming the topic).

    The topic goes to curl through a config on stdin and the message through a file, so neither is in argv.
    """
    topic = env.get("PIVOTAL_NTFY", "")
    if not topic:
        print("ntfy: PIVOTAL_NTFY is not set; no push message", file=sys.stderr)
        return None
    if NTFY_TOPIC.fullmatch(topic) is None:
        return "ntfy: PIVOTAL_NTFY is not a valid topic name"
    clean_env = {k: v for k, v in env.items() if k not in ("GH_TOKEN", "PIVOTAL_NTFY")}
    try:
        with tempfile.TemporaryDirectory(dir=env.get("RUNNER_TEMP") or None) as tmp:
            path = Path(tmp) / "message"
            path.write_text(message, encoding="utf-8")
            done = subprocess.run(
                ["curl", "-q", "--silent", "--show-error", "--fail", "--max-time", "20", "--proto", "=https",
                 "--config", "-", "--data-binary", f"@{path}"],
                input=f'url = "{NTFY_URL}{topic}"\n', capture_output=True, text=True, check=False,
                timeout=NTFY_TIMEOUT_SECONDS, env=clean_env,
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        return "ntfy: curl could not run"
    if done.returncode != 0:
        return f"ntfy: curl failed with exit {done.returncode}"
    return None


def set_labels(env: Mapping[str, str], repo: str, number: int, label: str) -> None:
    """Delete the status labels the issue holds except the outcome label, then add the outcome label.

    Only the issue's label names are read (not its title); a label a person added meanwhile is never touched,
    because nothing replaces the whole set.
    """
    job.require_label(env, repo, label)
    held = job.gh(env, "api", "--paginate", f"repos/{repo}/issues/{number}/labels?per_page=100", "--jq", ".[].name")
    names = held.splitlines()
    for name in STATUS_LABELS:
        if name in names and name != label:
            job.gh(env, "api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{quote(name, safe='')}")
    job.gh(env, "api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={label}")


def read_quiet(env: Mapping[str, str]) -> bool:
    """ack's ``quiet`` output: ``true``, ``false``, or empty when ack never wrote it (it failed first)."""
    value = env.get("QUIET", "")
    if value not in ("", "true", "false"):
        raise JobError("QUIET must be true or false")
    return value == "true"


def read_upstream(env: Mapping[str, str]) -> dict[str, str | None]:
    """Each stage's job result from ``<STAGE>_RESULT``: ack and plan always, the later stages only when set."""
    found: dict[str, str | None] = {}
    for name in STAGES:
        found[name] = env.get(f"{name.upper()}_RESULT")
        if found[name] is None and name in ("ack", "plan"):
            found[name] = ""
    return found


def report(env: Mapping[str, str]) -> int:
    repo = job.read_repo(env)
    job.read_kind(env)
    quiet = read_quiet(env)
    job.read_engine_sha(env)
    if quiet:
        print("quiet: ack found the level starts no run of this kind", file=sys.stderr)
        return 0
    run_number = job.read_number(env.get("RUN_NUMBER", ""), "run number")
    attempt = job.read_number(env.get("RUN_ATTEMPT", "1"), "run attempt")
    run_id = job.read_number(env.get("RUN_ID", ""), "run id", job.RUN_ID_DIGITS)
    run = f"{job.ROOT_LINK}{repo}/actions/runs/{run_id}"
    for label in STATUS_LABELS:
        job.label_name(label)
    upstream = read_upstream(env)
    outcome = decide(upstream, env.get("RESULT", ""), env.get("HAS_STACK", ""), env.get("PLAN_WHY", ""))
    # A run that never got going (ack or plan did not succeed) may be one ack refused because the issue was
    # edited: its title is never read or printed, only the number and the fixed text.
    got_going = upstream["ack"] == "success" and upstream["plan"] == "success"
    try:
        number = job.read_number(env.get("ISSUE", ""), "issue")
    except JobError:
        number = 0
    problems: list[str] = [outcome.note] if outcome.note else []
    if outcome.fix_failed:
        problems.append(FIX_FAILED_NOTE)
    if outcome.fix_unknown:
        problems.append(FIX_UNKNOWN_NOTE)
    # A note is shown and never makes the job red: a re-run that sent nothing is the normal case (R19).
    notes: list[str] = [FIX_RERUN_NOTE] if outcome.fix_rerun else []
    title = f"issue #{number}" if number else "no issue"
    if number and got_going:
        try:
            data = job.gh_json(env, "api", f"repos/{repo}/issues/{number}")
            if isinstance(data, dict) and isinstance(data.get("title"), str) and "pull_request" not in data:
                title = job.clean_text(data["title"], title, TITLE_MAX)
        except JobError as err:
            problems.append(f"the issue could not be read: {err}")
    target = link_of(repo, outcome, run)
    pr_link = "" if target == run else target
    comment = comment_body(run_number, outcome, run, pr_link, kaggle_line(env))
    message = message_text(repo, title, outcome, target)
    print(f"outcome: {message}", file=sys.stderr)
    if number:
        try:
            if attempt > 1 and already_posted(env, repo, number, comment):
                print("this outcome is already commented; not posting it twice", file=sys.stderr)
            else:
                job.gh(env, "api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={comment}")
        except JobError as err:
            problems.append(f"the comment was not posted: {err}")
        try:
            set_labels(env, repo, number, LABEL[outcome.status])
        except JobError as err:
            problems.append(f"the labels were not set: {err}")
    else:
        print("no issue to comment on or label", file=sys.stderr)
    problem = notify(env, message)
    if problem:
        problems.append(problem)
    path = env.get("GITHUB_STEP_SUMMARY", "")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(summary_text(title, outcome, comment, problems + notes))
        except OSError:
            problems.append("the step summary could not be written")
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    return 1 if outcome.red or problems else 0


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m engine.report")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("post", help="the report job: one comment, one message, the labels, the step summary")
    summary = commands.add_parser("summary", help="write the redacted summary of an agent log")
    summary.add_argument("--log", required=True, type=Path, help="the raw agent log")
    summary.add_argument("--out", required=True, type=Path, help="the summary file to write")
    summary.add_argument(
        "--secret", required=True, action="append", type=build.env_name, metavar="NAME",
        help="a secret the agent held, by environment name; repeat for more",
    )
    args = parser.parse_args(argv)
    if args.command == "post":
        code = 1
        try:
            code = report(os.environ if env is None else env)
        except JobError as err:
            print(f"refused: {err}", file=sys.stderr)
        return code

    held = build.held_values(args.secret, os.environb)
    if held.problem:
        print("failed: scan_incomplete")
        print(f"problem: {held.problem}")
        return 1
    try:
        log = args.log.read_bytes()
    except OSError:
        print("failed: scan_incomplete")
        print("problem: the agent log cannot be read")
        return 1
    result = redacted_summary(log, held.values, held.skipped)
    args.out.write_text(result.text, encoding="utf-8")
    print("withheld" if result.withheld else "ok")
    for name in held.skipped:
        print(f"skipped: {name} (shorter than {build.MIN_SECRET_BYTES} bytes, not redacted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
