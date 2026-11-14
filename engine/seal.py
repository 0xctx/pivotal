"""The seal (F4): the `pivotal/gate` check run the publish job creates, and the verifier the land job uses.

The seal is a check run named ``pivotal/gate`` on SHA *m* (the last commit of the landable prefix) with ``external_id`` =
the id of the workflow run that made it. The publish job creates it (``payload``, ``check_response``) when every F4 condition holds:
the prefix was computed from the gate and review artifacts, the run is trusted, its effective level is not held and starts the run's
kind by a human's command (``ask``, ``prepare`` or ``land``; ``watch`` starts nothing; ``engine/publish.py``, ``Settings.may_seal``),
and the base was moved or the stack PR is open and READY. A re-run of the same run id may create a second seal; the verifier needs
only one that checks out, so a duplicate changes nothing.

``verify`` is what the land job calls. Its inputs are plain data it read from GitHub (``gh api``) and from the artifacts of
the run the seal names, all hostile until checked:

- ``check_runs``: the ``check_runs`` list of ``repos/<repo>/commits/<m>/check-runs``. A check run of the same name from any
  other app (``app.slug`` is not ``github-actions``), for another SHA, not completed and successful, or whose ``external_id``
  is not the run's id is IGNORED, not an error: it can neither seal nor block. There must be at least one that qualifies,
  and one that is in the named run's check suite (below).
- ``run``: ``repos/<repo>/actions/runs/<external_id>``. Its ``id`` is the external id, its repository is this one, its
  workflow ``path`` is exactly ``.github/workflows/pivotal.yml``, the caller the repo installs, its ``head_branch`` is the
  ``default`` branch the caller passes (a run of a writer's own copy of the workflow on a feature branch is refused:
  ``run_branch``; see ``check_run_identity``) and its ``event`` is one of RUN_EVENTS. That alone does NOT say who
  made the check run: ``external_id`` and the summary are text that any workflow of the repo with ``checks: write`` (all of
  them are ``github-actions``) can write, naming a pivotal run's id and its land hash. So the check run must also be
  bound to the run: ``check_run["check_suite"]["id"]`` must equal ``run["check_suite_id"]`` (both are integer fields of the
  REST API; ``same_suite``), else ``suite_differs``.

  WHAT THAT BINDING IS NOT: ``verify`` does NOT establish who created a seal. It only says the check run sits in the check
  suite the named run reports. Whether GitHub puts a check run that a workflow job creates through ``POST /check-runs`` into
  the suite of the workflow run that made the call, or into the app's suite for the check run's head SHA, is UNVERIFIED on real
  GitHub. In the first model another workflow run in the same suite could still pass; in the second the check refuses every
  genuine seal whose *m* is not the run's own head SHA, or passes any ``github-actions`` check run on *m* in the app's suite.
  OPEN QUESTION for a live GitHub run: which model holds, and whether a dedicated GitHub App (a check run whose ``app.slug`` only
  pivotal can have) can be pinned, which would settle the forger. Under the second model a same-app check run on *m* passes
  the binding and authorship is not established. WHAT ACTUALLY KEEPS A FORGED SEAL OUT is not this module: it is the
  invariant that no job that runs repo code or model output holds ``checks: write`` (only the publish job does, and it
  runs none; test_factory pins it). The app, the workflow path and the artifacts below are conditions a forger who can
  create a check run in a suite of his own, upload artifacts of his own and so match them all also meets
  (test_land.Round1Test.test_p3_known_limitation_...). A dedicated GitHub App slug, or another marker only pivotal can
  create, is the operator's open decision.
- ``artifacts``: the ``artifacts`` list of ``repos/<repo>/actions/runs/<external_id>/artifacts``. ``land`` and
  ``gate-<id>`` must each be there exactly once, not expired, and belong to that run (``workflow_run.id``): an artifact of
  another run is no confirmation.
- ``land_dir`` and ``gates_dir``: those artifacts, downloaded (``gates_dir`` holds ``gate-<id>/gate.json`` per slice).
  ``land.json`` goes through ``assemble.read_land``, every gate through ``gate.read_gate`` and ``gate.confirm``; each must be
  green; *m* must be the head of a stacked slice and every slice of the prefix up to it must have its green gate. The seal's
  ``output.summary`` carries ``land_sha256: <hex>`` and it must equal the sha256 of the downloaded land.json, which binds the
  seal to the exact land.json the publish job used.

The land job must hand ``verify`` the LISTS, not the REST objects: ``gh api`` answers ``{"total_count": n, "check_runs": [...]}``
and ``{"total_count": n, "artifacts": [...]}``, so it unwraps ``check_runs`` and ``artifacts`` itself, and it must paginate
(``--paginate``) before it does. ``verify`` only refuses a list of MAX_ITEMS (1000) or more, which is the most it can say:
it cannot see that a list was truncated, so a land job that does not paginate may miss the seal or an artifact (a refusal,
never an acceptance). Trust in the reviews is transitive: the verifier never reads ``review-<id>``; it trusts that the
publish job (which did, with ``review.accept``) only creates the seal for a prefix whose verdicts held. It cannot read the
level or the run's trust either. What it checks is that the seal's check run, the run that its ``external_id`` names (at the
pivotal.yml path of this repo) and that run's artifacts agree, and that the check run sits in that run's check suite; it does
not read who created the check run, so the suite binding is the only link between the two (see ``run`` above).

The result is the list of slice ids of the prefix the seal confirms. Anything else is a ``SealError`` with a fixed word.
What this module cannot prove (no run on real GitHub has happened): that the real API reports ``app.slug``, the run
``path``, ``check_suite.id`` and ``check_suite_id`` in exactly these forms, and that a genuine seal shares its run's suite. A mismatch is a refusal, never an acceptance.
"""

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from engine import assemble, gate

CHECK_NAME = "pivotal/gate"
APP_SLUG = "github-actions"
WORKFLOW_PATH = ".github/workflows/pivotal.yml"
TITLE = "Pivotal gate passed"
SUMMARY_LAND = re.compile(r"^land_sha256: ([0-9a-f]{64})$", re.MULTILINE)
EXTERNAL_ID = re.compile(r"[1-9][0-9]{0,15}")
# The events a pivotal.yml run can have: the caller's `on:` (templates/caller.yml). A genuine seal is made by a run of one of these;
# a run of any other event (push, pull_request, schedule) is no seal. Note the allowlist is weak on its own: a writer's run on a
# feature branch can use workflow_dispatch too, so the branch test below is the one that matters.
RUN_EVENTS = ("issues", "issue_comment", "repository_dispatch", "workflow_dispatch", "workflow_run")
MAX_ITEMS = 1000  # check runs or artifacts that are read; a longer list may be cut off and is refused


class SealError(ValueError):
    """The seal is not trusted. The message is a fixed word, never a value from GitHub."""


def summary(land_sha256: str, slices: Sequence[str]) -> str:
    """The check run's summary: fixed words, the land.json hash and the ids of the prefix (slice ids are validated words)."""
    return f"The first {len(slices)} slice(s) of the stack passed the gate: {', '.join(slices)}.\nland_sha256: {land_sha256}\n"


def payload(sha: str, run_id: int, land_sha256: str, slices: Sequence[str]) -> dict:
    """The JSON body of ``POST repos/<repo>/check-runs``: a completed, successful check run on ``sha``."""
    if assemble.SHA.fullmatch(sha) is None or assemble.SHA256.fullmatch(land_sha256) is None or not slices:
        raise SealError("seal_input")
    return {
        "name": CHECK_NAME, "head_sha": sha, "status": "completed", "conclusion": "success", "external_id": str(run_id),
        "output": {"title": TITLE, "summary": summary(land_sha256, slices)},
    }


def check_response(data: object, sha: str, run_id: int) -> int:
    """The id of the check run GitHub answered with, if it is the one that was asked for; else SealError."""
    if not isinstance(data, dict):
        raise SealError("seal_response")
    ident, app = data.get("id"), data.get("app")
    if (
        isinstance(ident, bool) or not isinstance(ident, int) or ident < 1
        or data.get("name") != CHECK_NAME or data.get("head_sha") != sha or data.get("external_id") != str(run_id)
        or data.get("conclusion") != "success" or data.get("status") != "completed"
        or not isinstance(app, dict) or app.get("slug") != APP_SLUG
    ):
        raise SealError("seal_response")
    return ident


def qualifies(item: object, sha: str) -> str | None:
    """The external id of a check run that could be the seal of ``sha``, else None. Nothing here raises."""
    if not isinstance(item, dict):
        return None
    app, output, external = item.get("app"), item.get("output"), item.get("external_id")
    if (
        item.get("name") != CHECK_NAME or item.get("head_sha") != sha or item.get("status") != "completed"
        or item.get("conclusion") != "success" or not isinstance(app, dict) or app.get("slug") != APP_SLUG
        or not isinstance(external, str) or EXTERNAL_ID.fullmatch(external) is None or not isinstance(output, dict)
        or not isinstance(output.get("summary"), str)
    ):
        return None
    return external


def claimed_hashes(item: Mapping) -> list[str]:
    """The sha256 values the check run's summary claims for land.json: whole lines, so exactly one is a claim and the first of
    several is none. ``item`` is a check run that ``qualifies`` (its summary is text)."""
    return SUMMARY_LAND.findall(item["output"]["summary"])


def _is_id(value: object, want: int) -> bool:
    """``value`` is the integer ``want``: not a bool, and not the float 4242.0 that compares equal to it."""
    return type(value) is int and value == want


def _one(items: Sequence, name: str, run_id: int) -> None:
    """Exactly one artifact of this name, not expired, made by this run."""
    found = [a for a in items if isinstance(a, dict) and a.get("name") == name]
    if len(found) != 1:
        raise SealError("artifact_missing")
    run = found[0].get("workflow_run")
    if not isinstance(run, dict) or not _is_id(run.get("id"), run_id):
        raise SealError("artifact_other_run")
    if found[0].get("expired") is True:
        raise SealError("artifact_expired")  # GitHub keeps artifacts 7 days here (factory.yml retention-days): an old seal
    if found[0].get("expired") is not False:
        raise SealError("artifact_other_run")


def check_run_identity(run: object, repo: str, run_id: int, default: str) -> None:
    """The run object is run ``run_id`` of THIS repository, of the caller's workflow path, on the DEFAULT branch and of an event
    the caller has. SealError: ``run_not_pivotal`` (id, path or repository), ``run_branch`` (head_branch is not ``default``) or
    ``run_event``. The land job calls this before it downloads anything of the run.

    ``head_branch``: a writer can push a branch with its own ``.github/workflows/pivotal.yml`` (``checks: write``), run it and
    create a ``pivotal/gate`` check run, so the path alone proves nothing; the workflow file of a run on a branch is that
    branch's. Only a run on the default branch is the workflow the repo's reviewers accepted. NOT VERIFIED LIVE: what
    ``head_branch`` a genuine ``issue_comment`` (or ``repository_dispatch``) run reports. GitHub runs those on the default
    branch's latest commit, and the field is expected to be the default branch name; if it is something else every genuine seal
    from such a run is refused (fails closed)."""
    repository = run.get("repository") if isinstance(run, dict) else None
    if (
        not isinstance(run, dict) or not _is_id(run.get("id"), run_id) or run.get("path") != WORKFLOW_PATH
        or not isinstance(repository, dict) or repository.get("full_name") != repo
    ):
        raise SealError("run_not_pivotal")
    if not isinstance(default, str) or not default or run.get("head_branch") != default:
        raise SealError("run_branch")
    if run.get("event") not in RUN_EVENTS:
        raise SealError("run_event")


def same_suite(item: Mapping, run: Mapping) -> bool:
    """The check run is in the check suite of the workflow run: ``check_run["check_suite"]["id"]`` equals
    ``run["check_suite_id"]``, both positive integers (not bools, not floats)."""
    suite = item.get("check_suite")
    ident = suite.get("id") if isinstance(suite, dict) else None
    return type(ident) is int and ident > 0 and _is_id(run.get("check_suite_id"), ident)


def candidates(check_runs: object, sha: str) -> list[str]:
    """The distinct external ids (run ids, as text) of the check runs that could be the seal of ``sha``, in the order seen.
    The land job fetches the run of each and calls ``verify`` per id; the first that verifies is the seal."""
    if not isinstance(check_runs, list) or len(check_runs) >= MAX_ITEMS:
        raise SealError("answer_shape")
    found: list[str] = []
    for item in check_runs:
        external = qualifies(item, sha)
        if external is not None and external not in found:
            found.append(external)
    return found


def verify(
    *, repo: str, default: str, sha: str, external_id: str, check_runs: object, run: object, artifacts: object,
    land_dir: Path, gates_dir: Path,
) -> list[str]:
    """The slice ids of the prefix the seal on ``sha`` made by run ``external_id`` confirms, or SealError. Check runs
    with another external id are ignored. See the module docstring."""
    if not isinstance(artifacts, list) or len(artifacts) >= MAX_ITEMS:
        raise SealError("answer_shape")
    if assemble.SHA.fullmatch(sha) is None:
        raise SealError("sha")
    if EXTERNAL_ID.fullmatch(external_id) is None or external_id not in candidates(check_runs, sha):
        raise SealError("no_seal")
    run_id = int(external_id)
    check_run_identity(run, repo, run_id, default)
    _one(artifacts, "land", run_id)
    seal = next((item for item in check_runs if qualifies(item, sha) == external_id and same_suite(item, run)), None)
    if seal is None:  # a check run of this name, sha and id, but not made inside the named run's check suite
        raise SealError("suite_differs")
    try:  # bind before parse: the bytes are hashed and compared with the seal's claim before any of them is read as JSON
        raw = assemble.read_regular(Path(land_dir) / assemble.LAND, assemble.MAX_LAND_BYTES)
    except assemble.ArtifactError:
        raise SealError("land_invalid") from None
    if claimed_hashes(seal) != [hashlib.sha256(raw).hexdigest()]:
        raise SealError("land_differs")
    try:
        land = assemble.read_land(Path(land_dir) / assemble.LAND)
    except assemble.LandError as err:
        print(f"land.json: {err}", file=sys.stderr)  # the word is all the caller keeps; the rule (a version, say) is for the log
        raise SealError("land_invalid") from None
    if assemble.land_text(land) != raw:
        raise SealError("land_differs")
    heads = [next(s for s in land["slices"] if s["id"] == ident)["head"] for ident in land["positions"]]
    if sha not in heads:
        raise SealError("sha_not_on_stack")
    prefix = land["positions"][: heads.index(sha) + 1]
    if land["baseline"] != "green":
        raise SealError("baseline")
    for ident in prefix:
        _one(artifacts, f"gate-{ident}", run_id)
        try:
            found = read_gate_of(Path(gates_dir), ident)
            gate.confirm(land, found)
        except gate.GateError:
            raise SealError("gate_invalid") from None
        if found["id"] != ident:
            raise SealError("gate_invalid")
        if not found["green"] or found["check"] != "green":
            raise SealError("gate_not_green")
    return list(prefix)


def read_gate_of(gates_dir: Path, ident: str) -> dict:
    """``gate-<ident>/gate.json`` under ``gates_dir``, each step opened without following a link (the artifacts are
    hostile input). Raises gate.GateError."""
    try:
        outer = assemble.open_dir(str(gates_dir))
    except assemble.ArtifactError:
        raise gate.GateError("the gate artifacts cannot be opened") from None
    try:
        try:
            inner = assemble.open_dir(f"gate-{ident}", outer)
        except assemble.ArtifactError:
            raise gate.GateError("the gate artifact is missing") from None
        try:
            if sorted(os.listdir(inner)) != [gate.GATE]:
                raise gate.GateError("the gate artifact is not exactly gate.json")
            return gate.read_gate(Path(gate.GATE), inner)
        finally:
            os.close(inner)
    finally:
        os.close(outer)


def seal_text(data: Mapping) -> bytes:
    """The request body as the bytes ``gh api --input`` sends: ASCII JSON."""
    return (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode("ascii")
