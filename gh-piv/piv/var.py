"""piv preset and piv level: set one repo's Pivotal variables.

Stateless: each command reads what it needs through gh and writes one repo variable through gh. A value
goes to gh on stdin, never in an argument. ``piv models`` is not here: no variable a model
would set is defined, so there is nothing for it to write yet.
"""

from collections.abc import Callable, Mapping

from . import doctor, init
from .gh import Gh, PivError

CHECK_HINT = "'<your check>'"


def _set(repo: str, gh: Gh, name: str, value: str, say: Callable[[str], None]) -> None:
    gh.run("variable", "set", name, "--repo", repo, stdin=value)
    say(f"variable {name}: set to {value}")


def preset(repo: str, name: str, env: Mapping[str, str], gh: Gh, say: Callable[[str], None]) -> None:
    """Preset check first (a read, no write): every refusal above it writes nothing, and a missing secret is a warning.

    A secret counts as set when the repo holds it. The environment only changes the advice: a secret that is in
    it is one ``piv init`` can write, so the warning says so. The preset is written even when secrets are missing,
    since runs fall back visibly; the warning is the operator's chance to fix it first.
    """
    init.checked_repo(repo)
    if not init.PRESET.fullmatch(name):
        raise PivError("PRESET must be a lowercase name such as claude-sub", 2)
    names = doctor.secret_names(gh, repo)
    missing = [secret for secret in init.SECRETS if secret not in names]
    for secret in missing:
        if env.get(secret, "").strip():
            advice = f"it is in your environment, so gh piv init {repo} --check {CHECK_HINT} writes it"
        else:
            advice = f"set it in your environment, then gh piv init {repo} --check {CHECK_HINT} writes it"
        say(f"warning: secret {secret} is not set on {repo}; {advice}")
    if not missing:
        say(f"secrets: none of Pivotal's secrets is missing on {repo}")
    _set(repo, gh, init.VAR_PRESET, name, say)


def level(repo: str, name: str, gh: Gh, say: Callable[[str], None]) -> None:
    init.checked_repo(repo)
    if name not in init.LEVELS:
        raise PivError(f"LEVEL must be one of {', '.join(init.LEVELS)}", 2)
    _set(repo, gh, init.VAR_LEVEL, name, say)
