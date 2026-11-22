"""piv upgrade: move many callers to one engine release (the release rule, F5).

The other way to move a caller is ``piv init REPO --check 'your check command' --update``, which asks for the check
command again and reads every variable on the way. This reads the caller each repo has, renders it at one release, and
writes it only when it differs: one line per repo, ``current``, ``updated <old sha12> -> <new sha12>`` or
``refused: <why>``, and exit 1 when any repo was refused or unreadable. A second run prints ``current`` for every repo.

Nothing else is touched: no variable, no secret, no label, no topic. The release is resolved once
(``init.resolve_release``) and every caller is rendered by ``init.render_caller`` and planned by ``init.plan_caller``,
so a caller that cannot be fully pinned is refused before it is written, and the commit message names the pin it
replaced and the pin it wrote. The engine name of a caller comes from doctor's parse (``doctor.caller_flaw``), so a
fork keeps its own name and only its pin moves.

A repo is read and written one at a time: a repo that cannot be read, or whose caller is not Pivotal's, is refused by
name and the others still run. Nothing here is a cross-repo hub: it is a loop of per-repo writes from the operator's
shell, and no state is kept between runs.
"""

from collections.abc import Callable

from . import doctor, init, status
from .gh import Gh, GhFailed, PivError


def managed(repo: str | None, owner: str | None, gh: Gh) -> list[str]:
    """The repos to move: the one REPO named, or every repo of OWNER with the pivotal topic (``status.managed``).

    The input is checked before gh runs, so a call that names neither (or both) is a usage error (exit 2).
    """
    if (repo is None) == (owner is None):
        raise PivError("give REPO or --owner, not both and not neither", 2)
    if repo is not None:
        return [init.checked_repo(repo)]
    # The search's answers are not repo names until they are checked: every path below is built from one, and this
    # command writes.
    return [_repo_name(name) for name in status.managed(gh, owner, [])]


def _repo_name(name: str) -> str:
    """A repo name the topic search returned, in GitHub's shape. Anything else is a usage error (exit 2)."""
    if not init.valid_repo(name):
        raise PivError(f"--owner gave {name!r}, which is not owner/name; nothing was written", 2)
    return name


def upgrade(repos: list[str], release: str | None, gh: Gh, say: Callable[[str], None]) -> int:
    """Move every repo's caller to one release, one line per repo. Returns 1 when any repo was refused."""
    # The release's commit, resolved once and before the first repo, so no repo is half-moved by a bad release.
    sha = init.resolve_release(gh, release)[1]
    templates: dict[str, str] = {}  # one read of a caller template per engine, not per repo
    refused = 0
    for repo in repos:
        try:
            engine, pinned, current = read_caller(gh, repo)
            if engine not in templates:
                templates[engine] = gh.api_text(f"repos/{engine}/contents/{init.CALLER_PATH}?ref={sha}")
            rendered = init.render_caller(templates[engine], sha, engine)
            write = init.plan_caller(repo, gh, rendered, sha, True, current)
            if write.state == "current":
                say(line(repo, "current", engine))
                continue
            if write.state != "updated":
                # "created": the caller read above was there, so this cannot happen unless it was deleted meanwhile.
                # A repo with no caller is not Pivotal's to write.
                raise PivError(f"{init.WORKFLOW_PATH} is missing in {repo}, so there is no caller to move")
            init.put_caller(
                repo,
                gh,
                write,
                message=f"Update the Pivotal caller from {pinned[:12]} to {sha[:12]}",
                advice=(
                    f"Nothing else was changed. Open a pull request that updates {init.WORKFLOW_PATH} to "
                    f"{sha[:12]}; piv upgrade does not open one yet."
                ),
            )
            say(line(repo, f"updated {pinned[:12]} -> {sha[:12]}", engine))
        except PivError as err:
            refused += 1
            say(line(repo, f"refused: {err}", None))
    return 1 if refused else 0


def read_caller(gh: Gh, repo: str) -> tuple[str, str, dict]:
    """The repo's caller: the engine it pins, the commit it pins, and the contents API answer it came from.

    A repo with no caller, or a caller that is not Pivotal's (doctor's parse says what is wrong with it), is refused:
    nothing is written for it. The pin is the one the caller carries, so the line can name what was replaced, and the
    answer goes to ``init.plan_caller`` so the file is not read twice.
    """
    path = f"repos/{repo}/contents/{init.WORKFLOW_PATH}"
    try:
        current = gh.api_json(path)
    except GhFailed as err:
        if err.status == 404:
            raise PivError(f"{init.WORKFLOW_PATH} is missing in {repo}, so it is not set up by piv init") from None
        raise
    flaw, pinned, engine = doctor.caller_flaw(init.decoded_text(current, path))
    if flaw is not None or pinned is None or engine is None:
        raise PivError(f"caller {init.WORKFLOW_PATH} in {repo} {flaw or 'pins no engine to one commit'}")
    return engine, pinned, current


def line(repo: str, state: str, engine: str | None) -> str:
    """One line for one repo: the state, and the fork's name when the caller pins one, since a fork keeps its engine."""
    fork = "" if engine is None or engine == init.ENGINE_REPO else f" (fork: {engine})"
    return f"piv upgrade {repo}: {state}{fork}"