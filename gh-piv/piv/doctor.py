"""piv doctor: is everything OK?

Read-only. Every GitHub read is a GET through gh (``Gh.api_json``), and nothing is written. The local
probes read the operator's environment: a GET to the Kaggle endpoint (no key is sent), ``kaggle kernels
status``, and, only with ``--probe-claude``, one paid one-token POST to the Anthropic API with
ANTHROPIC_API_KEY. No secret value is printed.

Each check answers one of four things. ok passes. skipped is a configuration that does not apply (quiet,
no fix). unknown means the check could not be performed or read: it is named, it counts, and the exit
status is 3. problem means something is wrong, with one fix line, and the exit status is 1. A read that
fails is a problem for its own check, and the other checks still run.
"""

import base64
import json
import re
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message

from . import init
from .gh import Gh, GhFailed, PivError

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
PROBE_MODEL = "claude-haiku-5-5"  # the smallest model; the probe asks for one token, so the choice barely matters
HTTP_TIMEOUT_SECONDS = 20
KAGGLE_TIMEOUT_SECONDS = 60
KERNEL = "pivotal-serve"  # one fixed private kernel per Kaggle account
KAGGLE_ENV = ("PATH", "HOME")  # kaggle gets these and its own KAGGLE_* variables, never a model key
# The secrets that let a preset build: any one model key. Derived from init's list, not copied.
MODEL_KEYS = tuple(name for name in init.SECRETS if name == "CLAUDE_CODE_OAUTH_TOKEN" or name.endswith("_API_KEY"))
ENDPOINT_REACHABLE = (200, 401, 403)  # what the Kaggle endpoint answers when it is up and serving (no key is sent)
KERNEL_STATUS = re.compile(r"status\W+(?:KernelWorkerStatus\.)?([A-Za-z]+)", re.IGNORECASE)
WORKFLOW = init.WORKFLOW_PATH
CHECK_HINT = "'<your check>'"
COMMENT = re.compile(r"(^|\s)#.*$")
# A pin on a `uses:` line, and the engine checkout's `repository:` with its `ref:` on the next line. The engine name
# is captured from the caller text, so a fork reads the same as the default.
USES_PIN = re.compile(r"^\s*(?:-\s+)?uses:\s*[\"']?([^/\s@\"']+/[^/\s@\"']+)/[^\s@\"']+@([^\s\"']+)", re.MULTILINE)
CHECKOUT_PIN = re.compile(r"repository:\s*([^/\s\"']+/[^/\s\"']+)\s+ref:\s*[\"']([^\"]*)[\"']")
PROBLEM_EXIT = 1
UNREAD_EXIT = 3  # a check could not be read; 2 is usage, as everywhere in piv


@dataclass(frozen=True)
class Finding:
    status: str  # "ok", "skipped", "unknown" or "problem"
    text: str
    fix: str | None = None  # one line, and only for a problem


def ok(text: str) -> Finding:
    return Finding("ok", text)


def skipped(text: str) -> Finding:
    return Finding("skipped", text)


def unknown(text: str) -> Finding:
    return Finding("unknown", text)


def problem(text: str, fix: str) -> Finding:
    return Finding("problem", text, fix)


def run(
    repo: str | None,
    every: bool,
    gh: Gh,
    env: Mapping[str, str],
    say: Callable[[str], None],
    probe_claude: bool = False,
) -> int:
    """Check one repo, or every repo with the pivotal topic that the user owns.

    Returns 1 if any check is a problem, else 3 if any check could not be read, else 0.
    """
    if (repo is not None) == every:
        raise PivError("give REPO or --all, not both and not neither", 2)
    if repo is not None:
        init.checked_repo(repo)
    tally: Counter[str] = Counter()
    say("piv doctor: your environment")
    tally[_say(claude_probe(env, probe_claude), say)] += 1
    repos = [repo] if repo is not None else managed_repos(gh)
    if repo is None and not repos:
        say("no repo carries the pivotal topic for your account")
    for name in repos:
        say(f"piv doctor {name}")
        for finding in repo_findings(gh, name, env):
            tally[_say(finding, say)] += 1
    problems, unreadable = tally["problem"], tally["unknown"]
    if problems:
        say(f"{problems} problem(s), each with a fix line above")
    if unreadable:
        say(f"{unreadable} check(s) could not be read; the [unknown] lines say why, so the state is not confirmed")
    if not problems and not unreadable:
        say("no problems")
    return PROBLEM_EXIT if problems else UNREAD_EXIT if unreadable else 0


def _say(finding: Finding, say: Callable[[str], None]) -> str:
    say(f"  [{finding.status}] {finding.text}")
    if finding.fix:
        say(f"      fix: {finding.fix}")
    return finding.status


def managed_repos(gh: Gh) -> list[str]:
    """The repos you own that carry the pivotal topic. The search index can lag, so this may miss one."""
    login = gh.api_json("user").get("login")
    if not isinstance(login, str) or not init.OWNER.fullmatch(login):
        raise PivError("gh api user gave no login to search by")
    answer = gh.api_json(f"search/repositories?q=topic%3A{init.TOPIC}+user%3A{login}&per_page=100")
    items = answer.get("items")
    if not isinstance(items, list):
        raise PivError("the repo search gave no list of repos")
    if answer.get("total_count", 0) > len(items):
        raise PivError("more than 100 repos carry the pivotal topic; doctor reads the first 100 only")
    names = [item.get("full_name") for item in items if isinstance(item, dict)]
    return sorted(name for name in names if isinstance(name, str))


def repo_findings(gh: Gh, repo: str, env: Mapping[str, str]) -> list[Finding]:
    checks: tuple[tuple[str, str, Callable[[], list[Finding]]], ...] = (
        ("Actions", "the Actions settings", lambda: [actions_enabled(gh, repo)]),
        ("Caller", "the caller file", lambda: [caller(gh, repo)]),
        ("Level", "the PIVOTAL_LEVEL and PIVOTAL_CHECK variables", lambda: [level(gh, repo)]),
        ("Secrets", "the Actions secret names", lambda: secrets(gh, repo)),
        ("Actions minutes", "the repo's visibility and owner", lambda: [minutes(gh, repo)]),
        ("Runner", "the self-hosted runner list and the runner variable", lambda: [runner(gh, repo)]),
        ("Kaggle", "the Actions secret names for the Kaggle check", lambda: kaggle(gh, repo, env)),
    )
    findings: list[Finding] = []
    for label, what, check in checks:
        try:
            findings += check()
        except PivError as err:
            findings.append(
                problem(
                    f"{label}: could not be read: {err}",
                    f"check that gh can read {what} for {repo} (gh auth status), then run piv doctor again",
                )
            )
    return findings


def level(gh: Gh, repo: str) -> Finding:
    """The loop ladder as the engine will read it: engine/job.py ``read_level`` and ``standing`` decide, and test_levels
    holds this to them. The word must be exactly a level (unset is the default); anything else builds and lands nothing."""
    word = init.variable_value(gh, repo, init.VAR_LEVEL)
    chosen = word if word else init.DEFAULT_LEVEL
    if chosen not in init.LEVELS:
        return problem(
            f"Level: {init.VAR_LEVEL} is {ascii(word[:40])}, which is not a level, so every run is refused",
            f"gh variable set {init.VAR_LEVEL} --body {init.DEFAULT_LEVEL} --repo {repo} (one of {', '.join(init.LEVELS)})",
        )
    shown = f"{chosen} (the default)" if not word else chosen
    if chosen == "watch":
        return ok(f"Level: {shown}: nothing is built")
    check = init.variable_value(gh, repo, init.VAR_CHECK)
    if not init.check_present(check or ""):
        return problem(
            f"Level: {shown} needs a check command ({init.VAR_CHECK}), and none is set, so every run is refused",
            f"piv init {repo} --check '<your test command>'",
        )
    # doctor reads two variables: it cannot see the preset's reviewers or the base's check result, which are decided on each run.
    needs = {
        "ask": "",
        "prepare": ", and a change is ready only on a green base (the baseline is checked on each run, not by doctor)",
        "land": ", lands only with a green base and a cross-vendor reviewer (this line is static text: doctor checks neither the baseline nor the reviewer, each run checks both, and without a cross-vendor preset the run holds at prepare)",
    }
    return ok(f"Level: {shown}{needs[chosen]}")


def actions_enabled(gh: Gh, repo: str) -> Finding:
    enabled = gh.api_json(f"repos/{repo}/actions/permissions").get("enabled")
    if enabled is True:
        return ok("Actions: enabled")
    if enabled is False:
        return problem(
            f"Actions: disabled in {repo}",
            f"turn Actions on under Settings > Actions > General in {repo}, or let the organization's policy allow it",
        )
    raise PivError(f"gh api repos/{repo}/actions/permissions gave no enabled flag")


def caller(gh: Gh, repo: str) -> Finding:
    path = f"repos/{repo}/contents/{WORKFLOW}"
    try:
        current = gh.api_json(path)
    except GhFailed as err:
        if err.status != 404:
            raise
        return problem(f"Caller: {WORKFLOW} is missing", f"run gh piv init {repo} --check {CHECK_HINT} to write it")
    content = current.get("content")
    try:
        text = base64.b64decode(content).decode("utf-8") if isinstance(content, str) else None
    except ValueError:
        text = None
    if text is None:
        return problem(
            f"Caller: {WORKFLOW} is not UTF-8 text",
            f"run gh piv init {repo} --check {CHECK_HINT} --update to write it",
        )
    flaw, sha, engine = caller_flaw(text)
    if flaw:
        return problem(
            f"Caller: {WORKFLOW} {flaw}",
            f"run gh piv init {repo} --check {CHECK_HINT} --update to write the pinned caller",
        )
    fork = "" if engine == init.ENGINE_REPO else " (a fork)"
    return ok(f"Caller: {WORKFLOW} structure checked (not validated as YAML), engine: {engine} pinned to {sha[:12]}{fork}")


def caller_flaw(text: str) -> tuple[str | None, str | None, str | None]:
    """What is wrong with a caller file, or None, the SHA it pins and the engine it pins.

    A structural check, not a YAML parse: the stdlib has no YAML parser, and actionlint lints the template in CI.
    Comments are removed first, so a commented-out key or pin counts for nothing.
    """
    code = "\n".join(COMMENT.sub("", line) for line in text.splitlines())
    if re.search(r"^\t", code, re.MULTILINE):
        return "uses a tab for indentation, which YAML does not allow", None, None
    for key in ("on", "jobs"):
        if not re.search(rf"^{key}:", code, re.MULTILINE):
            return f"has no top-level {key}: key", None, None
    if init.PIN_PLACEHOLDER in code:
        return "still has the template's placeholder pin", None, None
    if init.ENGINE_PLACEHOLDER in code:
        return "still has the template's engine placeholder", None, None
    pins = USES_PIN.findall(code) + CHECKOUT_PIN.findall(code)
    engines = {engine for engine, _ in pins}
    if len(engines) != 1:
        return "has a reference to an engine repo that is not pinned to one commit SHA on a uses: line", None, None
    (engine,) = engines
    repo_pins = init.ENGINE_REPO_LINE.findall(code)
    if any(value != engine for value in repo_pins):
        return "has an engine_repo that is not the engine it pins", None, None
    mentions = init.pivotal_reference(engine).findall(code)
    shas = [sha for _, sha in pins]
    if (
        not mentions
        or len(pins) + len(repo_pins) != len(mentions)
        or len(set(shas)) != 1
        or not init.SHA.fullmatch(shas[0])
    ):
        return "has a reference to an engine repo that is not pinned to one commit SHA on a uses: line", None, None
    return None, shas[0], engine


def secret_names(gh: Gh, repo: str) -> set[str]:
    listed = gh.api_json(f"repos/{repo}/actions/secrets?per_page=100").get("secrets")
    if not isinstance(listed, list):
        raise PivError(f"gh api repos/{repo}/actions/secrets gave no secret list")
    return {item.get("name") for item in listed if isinstance(item, dict)}


def secrets(gh: Gh, repo: str) -> list[Finding]:
    """Names only: GitHub never returns a secret's value, so this says which are set, not what they hold."""
    names = secret_names(gh, repo)
    model = [name for name in MODEL_KEYS if name in names]
    findings = []
    if model:
        findings.append(ok(f"Secrets: a model key is set ({', '.join(model)})"))
    else:
        findings.append(org_model_key(gh, repo))
    if (init.KAGGLE_URL in names) != (init.KAGGLE_KEY in names):
        present, absent = (
            (init.KAGGLE_URL, init.KAGGLE_KEY) if init.KAGGLE_URL in names else (init.KAGGLE_KEY, init.KAGGLE_URL)
        )
        findings.append(
            problem(
                f"Secrets: {present} is set but {absent} is not",
                f"set {absent} in your environment, then run gh piv init {repo} --check {CHECK_HINT}",
            )
        )
    return findings


MODEL_KEY_FIX = (
    "export a model key such as CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY, "
    "then run gh piv init {repo} --check {hint}"
)


def org_model_key(gh: Gh, repo: str) -> Finding:
    """The fallback when the repo has no model key of its own: an organization secret the repo can read counts.

    If the organization's secrets cannot be read, that is unknown, because a key may be there.
    """
    login, kind = owner_of(gh, repo)
    fix = MODEL_KEY_FIX.format(repo=repo, hint=CHECK_HINT)
    if kind != "Organization":
        return problem("Secrets: no repo-level model key is set, so no preset can build", fix)
    try:
        visible = organization_model_keys(gh, login, repo)
    except PivError as err:
        detail = f" (HTTP {err.status})" if isinstance(err, GhFailed) and err.status else ""
        return unknown(
            f"Secrets: no repo-level model key is set, and the organization's secrets could not be read{detail}, "
            "so an organization key is not ruled out"
        )
    if visible:
        return ok(f"Secrets: a model key is set at the organization ({', '.join(visible)}), which {repo} can use")
    return problem(f"Secrets: no repo-level model key is set, and no organization model key is visible to {repo}", fix)


def owner_of(gh: Gh, repo: str) -> tuple[str, object]:
    owner = gh.api_json(f"repos/{repo}").get("owner")
    if not isinstance(owner, dict) or not isinstance(owner.get("login"), str):
        raise PivError(f"gh api repos/{repo} gave no owner")
    if not init.OWNER.fullmatch(owner["login"]):
        raise PivError(f"gh api repos/{repo} gave an owner name this check will not put in a path")
    return owner["login"], owner.get("type")


def organization_model_keys(gh: Gh, org: str, repo: str) -> list[str]:
    """The organization's model keys this repo can read. A "selected" secret counts only if the repo is in its list."""
    listed = gh.api_json(f"orgs/{org}/actions/secrets?per_page=100")
    items = listed.get("secrets")
    if not isinstance(items, list):
        raise PivError(f"gh api orgs/{org}/actions/secrets gave no secret list")
    if listed.get("total_count", 0) > len(items):
        raise PivError(f"{org} has more than 100 secrets, and doctor reads the first 100 only")
    visible = []
    for item in items:
        if not isinstance(item, dict) or item.get("name") not in MODEL_KEYS:
            continue
        name, visibility = item["name"], item.get("visibility")
        selected = visibility == "selected" and repo.lower() in selected_repos(gh, org, name)
        if visibility in ("all", "private") or selected:
            visible.append(name)
        elif visibility != "selected":
            raise PivError(f"{org} has a secret with a visibility this check does not know")
    return visible


def selected_repos(gh: Gh, org: str, name: str) -> set[str]:
    """The lower-cased full names of the repos an organization secret is selected for."""
    path = f"orgs/{org}/actions/secrets/{name}/repositories?per_page=100"
    answer = gh.api_json(path)
    listed = answer.get("repositories")
    if not isinstance(listed, list):
        raise PivError(f"gh api {path} gave no repository list")
    if answer.get("total_count", 0) > len(listed):
        raise PivError("more than 100 repos are selected for an organization secret; doctor reads the first 100")
    return {str(item.get("full_name") or "").lower() for item in listed if isinstance(item, dict)}


def minutes(gh: Gh, repo: str) -> Finding:
    info = gh.api_json(f"repos/{repo}")
    if info.get("private") is not True:
        return skipped("Actions minutes: public repo, so runs are free")
    owner = info.get("owner")
    if not isinstance(owner, dict) or not isinstance(owner.get("login"), str):
        raise PivError(f"gh api repos/{repo} gave no owner")
    kind = "orgs" if owner.get("type") == "Organization" else "users"
    try:
        answer = gh.api_json(f"{kind}/{owner['login']}/settings/billing/actions")
    except PivError as err:
        detail = f" (HTTP {err.status})" if isinstance(err, GhFailed) and err.status else ""
        return unknown(
            f"Actions minutes: could not be read{detail}; the billing endpoint is unverified, "
            "so minutes are not checked"
        )
    included, used = answer.get("included_minutes"), answer.get("total_minutes_used")
    if not (isinstance(included, int) and isinstance(used, int)):
        return unknown(
            "Actions minutes: the billing answer has no minute counts; the billing endpoint is unverified, "
            "so minutes are not checked"
        )
    if included - used <= 0:
        return problem(
            f"Actions minutes: none included left ({used} of {included} used; the billing endpoint is unverified)",
            "add minutes or a spending limit in GitHub billing, or run the factory on your own runner "
            f"({init.RUNNER_VARIABLE}=self-hosted)",
        )
    return ok(f"Actions minutes: {included - used} of {included} included minutes left (billing endpoint unverified)")


def runner(gh: Gh, repo: str) -> Finding:
    if init.variable_value(gh, repo, init.RUNNER_VARIABLE) != "self-hosted":
        return skipped(f"Runner: GitHub-hosted runners ({init.RUNNER_VARIABLE} is not self-hosted)")
    # engine/where.py: a public repo uses the runner only for trusted work, and only with PIVOTAL_RUNNER_PUBLIC=trusted-only.
    if gh.api_json(f"repos/{repo}").get("private") is not True and (
        init.variable_value(gh, repo, "PIVOTAL_RUNNER_PUBLIC") != "trusted-only"
    ):
        return skipped(
            f"Runner: GitHub-hosted runners (the repo is public and PIVOTAL_RUNNER_PUBLIC is not trusted-only, "
            f"so {init.RUNNER_VARIABLE} is not used)"
        )
    listed = gh.api_json(f"repos/{repo}/actions/runners?per_page=100").get("runners")
    if not isinstance(listed, list):
        raise PivError(f"gh api repos/{repo}/actions/runners gave no runner list")
    online = [item for item in listed if isinstance(item, dict) and item.get("status") == "online"]
    if online:
        return ok(f"Runner: {len(online)} self-hosted runner online")
    return problem(
        f"Runner: no self-hosted runner is online for {repo}, so its jobs would wait",
        f"start the self-hosted runner service on the machine that serves {repo}",
    )


def kaggle(gh: Gh, repo: str, env: Mapping[str, str]) -> list[Finding]:
    """Kaggle is checked only for a repo that has the endpoint secret. Quota is not readable, so it is unknown."""
    if init.KAGGLE_URL not in secret_names(gh, repo):
        return []
    user = env.get("KAGGLE_USERNAME", "").strip()
    if user:
        status = kernel_status(f"{user}/{KERNEL}", env)
    else:
        status = unknown("Kaggle: KAGGLE_USERNAME is not in your environment, so the kernel's name is unknown")
    return [
        status,
        unknown("Kaggle quota: the Kaggle CLI shows no GPU quota; a refused push shows in the run report"),
        endpoint_probe(env),
    ]


def kernel_status(kernel: str, env: Mapping[str, str]) -> Finding:
    """The kernel's state, read from kaggle's output by its word.

    Nothing kaggle prints is echoed, because its output may hold a key.
    """
    binary = env.get("PIV_KAGGLE") or "kaggle"
    kaggle_env = {key: value for key, value in env.items() if key in KAGGLE_ENV or key.startswith("KAGGLE")}
    try:
        done = subprocess.run(
            [binary, "kernels", "status", kernel],
            capture_output=True,
            text=True,
            check=False,
            env=kaggle_env,
            timeout=KAGGLE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return unknown("Kaggle: kaggle is not installed here, so the kernel is not checked")
    except (OSError, subprocess.TimeoutExpired):
        return problem(
            f"Kaggle: could not run kaggle for {kernel}",
            "check that kaggle runs here, then run piv doctor again",
        )
    if done.returncode != 0:
        return problem(
            f"Kaggle: could not read the kernel (exit {done.returncode})",
            "check KAGGLE_USERNAME and KAGGLE_KEY in your environment, and that the kernel exists on kaggle.com, "
            "then run piv doctor again",
        )
    match = KERNEL_STATUS.search(done.stdout + done.stderr)
    state = match.group(1).lower() if match else ""
    if state in ("queued", "running"):
        return ok(f"Kaggle: kernel {kernel} is {state} (another run may be using it)")
    if state == "complete":
        return ok(f"Kaggle: kernel {kernel} is complete, so it is idle")
    if state == "error":
        return problem(
            f"Kaggle: kernel {kernel} ended in error",
            "open the kernel's log on kaggle.com; the next Kaggle run can start it again",
        )
    return problem(
        f"Kaggle: kernel {kernel} has a status this check does not expect",
        f"run kaggle kernels status {kernel} and read its output",
    )


def claude_probe(env: Mapping[str, str], enabled: bool) -> Finding:
    """One request with max_tokens 1, for the local ANTHROPIC_API_KEY, and only when asked: it is a paid call.

    The stored value is never readable, so this probes the local one. Redirects are refused (see http_answer).
    """
    if not enabled:
        return unknown(
            "Claude token: not probed (pass --probe-claude for one paid one-token call with ANTHROPIC_API_KEY)"
        )
    key = env.get("ANTHROPIC_API_KEY", "")
    if not key.strip():
        if env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
            return unknown(
                "Claude token: CLAUDE_CODE_OAUTH_TOKEN is not probed from here "
                "(a subscription token belongs to Claude Code)"
            )
        return unknown("Claude token: no ANTHROPIC_API_KEY in your environment to probe")
    body = {"model": PROBE_MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    request = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION, "x-api-key": key},
        method="POST",
    )
    answer = http_answer(request)
    if answer is None:
        return unknown("Claude API: could not reach the Claude API, so the key was not tested")
    if answer.status == 200:
        return ok("Claude API key accepted (the local value, one-token probe)")
    if answer.status in (401, 403):
        return problem(
            f"Claude API key rejected (HTTP {answer.status}): the local value",
            "make a key at console.anthropic.com, export it as ANTHROPIC_API_KEY, "
            f"then run gh piv init REPO --check {CHECK_HINT}",
        )
    if answer.status == 404:
        return unknown(
            f"Claude token: probe model {PROBE_MODEL} was not found (HTTP 404), so the key was not tested; "
            "the probe model in piv may be out of date"
        )
    return unknown(f"Claude token: the probe answered HTTP {answer.status}, not 200, so the key was not confirmed")


def endpoint_probe(env: Mapping[str, str]) -> Finding:
    """The Kaggle endpoint (an ngrok domain), probed with a GET and no key. Only a repo with that secret asks."""
    url = env.get(init.KAGGLE_URL, "").strip()
    if not url:
        return unknown(
            f"Endpoint: {init.KAGGLE_URL} is not in your environment, so the Kaggle endpoint is not probed "
            "(GitHub does not show secrets)"
        )
    if not url.startswith(("https://", "http://")):
        return problem(
            f"Endpoint: {init.KAGGLE_URL} in your environment is not an http(s) URL",
            f"set {init.KAGGLE_URL} to the ngrok domain's URL, then run piv doctor again",
        )
    answer = http_answer(urllib.request.Request(url.rstrip("/") + "/v1/models"))
    if answer is None:
        return problem(
            "Endpoint: the ngrok domain did not answer",
            "check the Kaggle kernel's status above, then run piv doctor again",
        )
    if answer.headers.get("ngrok-error-code") or answer.status == 404 or answer.status >= 500:
        return problem(
            f"Endpoint: the tunnel is up but the Kaggle kernel is not serving (HTTP {answer.status})",
            "check the kernel's log on kaggle.com, then run piv doctor again",
        )
    if answer.status in ENDPOINT_REACHABLE:
        return ok(f"Endpoint: answered HTTP {answer.status} (no key was sent)")
    return problem(
        f"Endpoint: answered HTTP {answer.status}, which is not a served API answer",
        f"check that {init.KAGGLE_URL} in your environment is the kernel's ngrok domain, then run piv doctor again",
    )


@dataclass(frozen=True)
class Answer:
    status: int
    headers: Message


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, not a place to follow: a key header must never reach another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def http_answer(request: urllib.request.Request) -> Answer | None:
    """The status and headers of the HTTP answer, or None when no answer came. The body is never read."""
    try:
        with _OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as answer:
            return Answer(answer.status, answer.headers)
    except urllib.error.HTTPError as err:
        return Answer(err.code, err.headers)
    except (urllib.error.URLError, OSError, ValueError):
        return None
