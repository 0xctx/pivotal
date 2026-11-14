"""piv remove: undo piv init for one repo. Look before delete.

Nothing is deleted without ``--yes``: without it the reads run and the plan is printed. Every name
removed is one that init defines, and only when the repo has it. All reads happen before the first
write, so a refusal deletes nothing. The caller file is removed only when it is the file init writes
at its pin, byte for byte, unless ``--force-caller`` says to remove it anyway. The caller goes first,
so no run can start while its config is being removed, and a refused caller write (a protected
default branch) leaves everything else in place. Each step is idempotent: after a failure, a re-run
removes what is still set.

init allows Actions to open pull requests and keeps the value it replaced in the variable
PIVOTAL_PRIOR_CAN_APPROVE_PRS. remove puts that value back, reads it back, and then deletes the variable. A repo
without the variable (the setting was already on) keeps the setting as it is, and remove says so.
"""

import base64
from collections.abc import Callable
from dataclasses import dataclass

from . import init
from .gh import Gh, GhFailed, PivError

CALLER_MESSAGE = "Remove the Pivotal caller"
# init's secrets that are not Pivotal's own: provider keys and Kaggle credentials that a repo may already hold.
MAYBE_THE_REPOS_OWN = tuple(name for name in init.SECRETS if not name.startswith("PIVOTAL_"))
# Statuses that a refused caller delete can carry: 403 (no write, or protection), 409 (stale sha), 422 (a ruleset).
REFUSED_STATUSES = (403, 409, 422)


@dataclass(frozen=True)
class Plan:
    """What is set now, read before any write. ``caller_blob`` is the caller's sha, when it is to be removed."""

    caller_blob: str | None
    secrets: tuple[str, ...]
    variables: tuple[str, ...]
    labels: tuple[str, ...]
    topic: bool
    restore_approve: bool = False  # init changed "Actions can open pull requests" from off: put it back off

    @property
    def nothing_to_remove(self) -> bool:
        return self.caller_blob is None and not (self.secrets or self.variables or self.labels or self.topic)


def remove(repo: str, gh: Gh, say: Callable[[str], None], *, yes: bool = False, force_caller: bool = False) -> None:
    plan = _plan(repo, gh, force_caller)
    if plan.nothing_to_remove:
        say(f"piv remove {repo}: none of Pivotal's is set, so nothing was removed")
    else:
        _warn_maybe_the_repos_own(plan, say)
        if not yes:
            _would_remove(plan, say)
            raise PivError(f"nothing was removed: check the secrets named above, then run piv remove {repo} --yes", 2)
        _delete(repo, gh, plan, say)
    if not plan.restore_approve:
        say("Actions can open pull requests: left as it is, since piv init recorded no earlier value to restore")


def _plan(repo: str, gh: Gh, force_caller: bool) -> Plan:
    _check_repo(repo, gh)
    _check_can_list(repo, gh)
    return Plan(
        caller_blob=_caller_blob(repo, gh, force_caller),
        secrets=tuple(name for name in init.SECRETS if _present(gh, f"repos/{repo}/actions/secrets/{name}")),
        variables=tuple(name for name in init.VARIABLES if _present(gh, f"repos/{repo}/actions/variables/{name}")),
        labels=tuple(name for name, _colour, _text in init.LABELS if _present(gh, f"repos/{repo}/labels/{name}")),
        topic=_has_topic(repo, gh),
        restore_approve=variable_prior(repo, gh),
    )


def variable_prior(repo: str, gh: Gh) -> bool:
    """Whether init recorded that it turned "Actions can open pull requests" on from off."""
    value = init.variable_value(gh, repo, init.VAR_PRIOR_APPROVE)
    if value is not None and value != "false":
        raise PivError(
            f"{init.VAR_PRIOR_APPROVE} in {repo} reads {value!r}, and piv init only ever writes 'false'. Someone else set it, "
            "so the Actions setting cannot be restored from it. Nothing was removed and the variable was kept: check "
            f"'Actions can open pull requests' by hand, then delete the variable."
        )
    return value == "false"


def _check_repo(repo: str, gh: Gh) -> None:
    """The repo must be visible before anything else is read: a 404 here is one answer, not dozens of absent names."""
    try:
        gh.api_json(f"repos/{repo}")
    except GhFailed as err:
        if err.status == 404:
            raise PivError(f"repo not found or not visible to this token: {repo}. Nothing was removed.") from None
        raise


def _check_can_list(repo: str, gh: Gh) -> None:
    """The secret and variable lists must be readable: an absent name is a fact only when the token can read them."""
    for kind, path in (
        ("secrets", f"repos/{repo}/actions/secrets?per_page=1"),
        ("variables", f"repos/{repo}/actions/variables?per_page=1"),
    ):
        try:
            gh.api_json(path)
        except GhFailed as err:
            raise PivError(
                f"cannot read the Actions {kind} of {repo}, so nothing can be checked and nothing was removed: "
                f"{err}. The token needs read access to them."
            ) from err


def _present(gh: Gh, path: str) -> bool:
    """Whether the object at ``path`` exists (a 404 is absent). Any other failure is raised."""
    try:
        gh.api_json(path)
    except GhFailed as err:
        if err.status == 404:
            return False
        raise
    return True


def _caller_blob(repo: str, gh: Gh, force_caller: bool) -> str | None:
    """The caller's blob sha when it is to be removed, None when absent. An edited caller is refused unless forced."""
    path = f"repos/{repo}/contents/{init.WORKFLOW_PATH}"
    try:
        current = gh.api_json(path)
    except GhFailed as err:
        if err.status != 404:
            raise
        return None
    content = current.get("content")
    blob = current.get("sha")
    if not isinstance(content, str) or not isinstance(blob, str):
        raise PivError(f"gh api {path} gave no file to check; nothing was removed")
    if not force_caller and not _is_init_caller(repo, gh, content):
        raise PivError(
            f"{init.WORKFLOW_PATH} in {repo} is not the caller piv init writes at its pin, so it was edited or is not "
            f"Pivotal's. Nothing was removed. Delete it by hand, or run piv remove {repo} --yes --force-caller to "
            "delete it anyway."
        )
    return blob


def _is_init_caller(repo: str, gh: Gh, content: str) -> bool:
    """Whether the file is exactly the caller init writes at the pin it names (the release's template, rendered there).

    A file with no pin, two pins, or a pin to a commit that is not a Pivotal template is not Pivotal's, so it
    is not removed. A template that cannot be read is not guessed at either.
    """
    try:
        text = base64.b64decode(content).decode("utf-8")
    except ValueError:
        return False
    pins = init.ANY_USES.findall(text) + init.ANY_CHECKOUT.findall(text)
    engines = {engine for engine, _ in pins}
    shas = {sha for _, sha in pins}
    if len(engines) != 1 or len(shas) != 1:
        return False
    (engine,) = engines
    (sha,) = shas
    if not init.SHA.fullmatch(sha):
        return False
    try:
        template = gh.api_text(f"repos/{engine}/contents/{init.CALLER_PATH}?ref={sha}")
    except GhFailed as err:
        if err.status == 404:
            return False
        raise
    try:
        return init.render_caller(template, sha, engine) == text
    except PivError:
        return False


def _has_topic(repo: str, gh: Gh) -> bool:
    names = gh.api_json(f"repos/{repo}/topics").get("names")
    if not isinstance(names, list):
        raise PivError(f"gh api repos/{repo}/topics gave no topic names; nothing was removed")
    return init.TOPIC in names


def _warn_maybe_the_repos_own(plan: Plan, say: Callable[[str], None]) -> None:
    maybe = [name for name in plan.secrets if name in MAYBE_THE_REPOS_OWN]
    if maybe:
        say(
            "warning: these secrets may be the repo's own rather than Pivotal's, and piv remove cannot tell, since "
            f"piv init does not record what was set before: {', '.join(maybe)}"
        )


def _would_remove(plan: Plan, say: Callable[[str], None]) -> None:
    """The dry run's plan: what would be removed, and what is already absent."""
    would: list[str] = []
    absent: list[str] = []
    if plan.caller_blob is not None:
        would.append(f"caller {init.WORKFLOW_PATH}")
    else:
        absent.append(f"caller {init.WORKFLOW_PATH}")
    for kind, every, set_now in (
        ("secret", init.SECRETS, plan.secrets),
        ("variable", init.VARIABLES, plan.variables),
        ("label", tuple(name for name, _colour, _text in init.LABELS), plan.labels),
    ):
        would += [f"{kind} {name}" for name in set_now]
        absent += [f"{kind} {name}" for name in every if name not in set_now]
    if plan.topic:
        would.append(f"topic {init.TOPIC}")
    else:
        absent.append(f"topic {init.TOPIC}")
    if plan.restore_approve:
        would.append("setting Actions can open pull requests (restored to off)")
    say(f"would remove: {', '.join(would) or 'nothing'}")
    say(f"already absent: {', '.join(absent) or 'nothing'}")


def _delete(repo: str, gh: Gh, plan: Plan, say: Callable[[str], None]) -> None:
    done: list[str] = []
    if plan.caller_blob is not None:
        # The first write: a refusal here leaves everything else in place.
        _remove_caller(repo, gh, plan.caller_blob, say)
        done.append(f"caller {init.WORKFLOW_PATH}")
    try:
        for name in plan.secrets:
            gh.run("secret", "delete", name, "--repo", repo)
            say(f"secret {name}: removed")
            done.append(f"secret {name}")
        for name in plan.variables:
            if name == init.VAR_PRIOR_APPROVE and plan.restore_approve:
                _restore_approve(repo, gh, say)
            gh.run("variable", "delete", name, "--repo", repo)
            say(f"variable {name}: removed")
            done.append(f"variable {name}")
        for name in plan.labels:
            gh.run("label", "delete", name, "--repo", repo, "--yes")
            say(f"label {name}: removed")
            done.append(f"label {name}")
        if plan.topic:
            gh.run("repo", "edit", repo, "--remove-topic", init.TOPIC)
            say(f"topic {init.TOPIC}: removed")
            done.append(f"topic {init.TOPIC}")
    except PivError as err:
        already = ", ".join(done) or "nothing"
        raise PivError(
            f"{err}. Already removed: {already}. Re-running piv remove {repo} --yes is safe: it removes only what "
            "is still set.",
            err.code,
        ) from err


def _restore_approve(repo: str, gh: Gh, say: Callable[[str], None]) -> None:
    """Put "Actions can open pull requests" back off, keeping the default permission, and read it back."""
    path = f"repos/{repo}/actions/permissions/workflow"
    body: dict[str, object] = {"can_approve_pull_request_reviews": False}
    current = gh.api_json(path)
    if "default_workflow_permissions" in current:
        body["default_workflow_permissions"] = current["default_workflow_permissions"]
    gh.api(path, method="PUT", body=body)
    after = gh.api_json(path).get("can_approve_pull_request_reviews")
    if after is not False:
        raise PivError(f"Actions could not be put back as before in {repo}: the setting reads back as {after!r}")
    say("Actions can open pull requests: restored to off, as it was before piv init")


def _remove_caller(repo: str, gh: Gh, blob: str, say: Callable[[str], None]) -> None:
    try:
        body = {"message": CALLER_MESSAGE, "sha": blob}
        gh.api(f"repos/{repo}/contents/{init.WORKFLOW_PATH}", method="DELETE", body=body)
    except GhFailed as err:
        if err.status not in REFUSED_STATUSES:
            raise
        raise PivError(_refusal(err.status, repo)) from err
    say(f"caller {init.WORKFLOW_PATH}: removed")



def _refusal(status: int, repo: str) -> str:
    where = f"{init.WORKFLOW_PATH} from {repo}"
    if status == 409:
        return (
            f"HTTP 409 removing {where}: the file changed since piv remove read it. nothing else was removed; "
            f"run piv remove {repo} --yes again."
        )
    if status == 403:
        return (
            f"HTTP 403 removing {where}: the token cannot write the default branch, or branch protection or a ruleset "
            "is refusing the change; nothing else was removed. The caller needs a pull request that deletes it."
        )
    return (
        f"HTTP 422 removing {where}: GitHub refused the change, which a ruleset or branch protection can do. "
        "nothing else was removed. The caller needs a pull request that deletes it."
    )
