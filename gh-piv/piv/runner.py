"""piv runner add and piv runner remove.

GitHub's own runner, as persistent systemd services under the dedicated user ``pv-runner``, with hooks
that clean up between jobs. Pivotal adds no daemon of its own: systemd supervises GitHub's ``run.sh``.

Everything real is a step in a plan, and a plan is data: ``build_plan`` and ``build_removal_plan`` read
the machine and change nothing, ``Plan.render`` prints it, and ``execute`` hands each step to an
``Executor``. Tests pass a fake executor, so nothing here touches the host unless the real one is used.
The default is to show the plan; ``--yes`` runs it, and only as root (it checks, it never escalates).

Every refusal (privileges, missing tools, a registration token gh will not give) happens before the
first change. The one-hour token is fetched once, given to ``config.sh`` through the environment of
that one process, and never put in an argument, a file, a unit or a message.
"""

import json
import os
import platform
import re
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .gh import Gh, PivError
from .init import NAME, OWNER

USER = "pv-runner"
HOME = "/home/pv-runner"
BASE = "/opt/pv-runner"
UNIT_PATH = "/etc/systemd/system/pv-runner@.service"
RUNNER_VERSION = "2.338.2"
# From the SHA-256 lines of the actions/runner v2.338.2 release notes. Change them with the version.
RUNNER_SHA256 = {
    "x64": "baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40",
    "arm64": "670adb2f268fe21dcbb58b3db0e0aa9d5b3bddfe2c651481336e561ff86de945",
}
ARCHITECTURES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
TOOLS = ("useradd", "runuser", "systemctl", "curl", "sha256sum", "tar", "chmod")
HOOK_SOURCE = Path(__file__).resolve().parent / "hooks" / "pv-clean.sh"
MAX_JOBS = 16
RUNNER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
INSTANCE = re.compile(r"runner-([0-9]+)")
GITHUB_URL = re.compile(r"https://github\.com/([^/]+)(?:/([^/]+))?/?")

# Printed by `add` and kept in docs/RUNNER.md; a test holds the two in step. `claude auth login` is
# from `claude auth --help`; `agy --help` has no login subcommand, so it signs in on first start.
LOGIN_LINES = (
    f"sudo -iu {USER}",
    "claude auth login",
    "codex login",
    "agy       # no login subcommand: sign in when it asks, then exit",
    "grok      # sign in when it asks, then exit",
)

SELF_HOSTED_RULES = (
    "Where a job runs is decided in factory.yml, never by a variable alone. Self-hosted is used only when",
    "  - PIVOTAL_RUNNER=self-hosted, and",
    "  - the run is trusted (engine/trust.py decides), and",
    "  - the repo is private, or PIVOTAL_RUNNER_PUBLIC=trusted-only is set.",
    "Untrusted work on a public repo always runs on GitHub-hosted VMs, so a stranger's issue never reaches",
    "this machine, which holds your logins. The exception: a writer's '/pivotal go trusted' comment makes that issue",
    "trusted, so read an issue before you say it. Offline runners do not fall back: a job just waits.",
)


@dataclass(frozen=True)
class Target:
    kind: str  # "org" or "repo"
    name: str  # "my-org" or "owner/repo"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.name}"

    @property
    def api(self) -> str:
        return f"{'orgs' if self.kind == 'org' else 'repos'}/{self.name}/actions/runners"


@dataclass(frozen=True)
class Machine:
    arch: str  # as the OS names it
    runner_arch: str  # x64 or arm64
    ram_gb: float
    disk_gb: float
    cpus: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Step:
    """One change. ``op`` is user, mkdir, write, run, rmtree, rmfile or rmdir; ``text`` is what is shown."""

    op: str
    text: str
    path: str = ""
    argv: tuple[str, ...] = ()
    content: str = ""
    mode: int = 0
    owner: str = "root"
    cwd: str = ""
    stdin: str = ""
    token: bool = False  # runs with the registration or removal token in its environment
    skip_if_exists: str = ""  # not run when this path exists (a re-run)
    only_if_exists: str = ""  # run only when this path exists
    tolerate_failure: bool = False


@dataclass(frozen=True)
class Plan:
    target: Target | None
    machine: Machine | None
    steps: tuple[Step, ...]
    token_kind: str  # "registration-token" or "remove-token"
    verb: str  # "add" or "remove"

    def render(self) -> str:
        out: list[str] = []
        if self.verb == "add":
            assert self.target and self.machine
            out += account_advice(self.target) + [""]
            m = self.machine
            out.append(f"This machine: {m.arch} -> linux-{m.runner_arch}, {m.ram_gb:.1f} GB RAM, {m.disk_gb:.1f} GB free, {m.cpus} CPUs")
            out += [f"  warning: {w}" for w in m.warnings]
            out += ["", *SELF_HOSTED_RULES, ""]
            out += [f"After the install, log your subscription CLIs in as {USER}:", *[f"  {line}" for line in LOGIN_LINES], ""]
        else:
            out += [f"Removing the {USER} runners registered to {self.target.name}" if self.target else f"Removing what is left of the {USER} install (no runner-N directory found)", ""]
        out.append("Plan (nothing has been changed):")
        for number, step in enumerate(self.steps, 1):
            out.append(f"  {number}. {step.text}")
        out += ["", f"This is a dry run. To do it, run as root: gh piv runner {self.verb} ... --yes"]
        return "\n".join(out)


def account_advice(target: Target) -> list[str]:
    if target.kind == "repo":
        return [
            f"Target: the repo {target.name}. On a personal account a runner serves only ONE repo, so 10 repos",
            "would mean 10 runner processes. Move the managed repos into a free GitHub organization and add",
            "one organization runner (`gh piv runner add --org ORG`) to serve every repo in it.",
        ]
    return [
        f"Target: the organization {target.name}. One organization runner serves every repo in it.",
        "On GitHub Free, organization secrets do not reach private repos, so `piv init` still sets secrets per repo.",
        "The organization's runner group must allow the repos (public repos are off by default).",
    ]


def runner_arch(arch: str | None = None) -> tuple[str, str]:
    raw = arch or platform.machine()
    found = ARCHITECTURES.get(raw.lower())
    if found is None:
        raise PivError(f"architecture {raw!r} is not supported: GitHub's runner is built for x86_64 and aarch64 only", 2)
    return raw, found


def nearest_existing(path: str) -> str:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return str(probe)


def device_model() -> str:
    """The board's name from the device tree (a Raspberry Pi says so there); empty on other machines."""
    try:
        return Path("/proc/device-tree/model").read_bytes().split(b"\0")[0].decode("utf-8", "replace").strip()
    except OSError:
        return ""


def memory_max_mb(ram_gb: float, jobs: int) -> int:
    """Each instance's MemoryMax: 90% of the RAM, shared by the --jobs instances."""
    return int(ram_gb * 1024 * 0.9 / jobs)


def check_machine(
    jobs: int,
    *,
    arch: str | None = None,
    ram_bytes: int | None = None,
    disk_free_bytes: int | None = None,
    cpus: int | None = None,
    model: str | None = None,
) -> Machine:
    """RAM and disk against ``jobs``, by the table below; a refusal comes before any change.

    Refused: a count the RAM or disk cannot carry at all (1.75 GB and 5 GB per instance). Warned: a count
    above the table (a Pi or 4 GB machine: 1, or 2 if tests are light; 8 GB: up to 3), or more
    instances than CPUs. Figures are what the OS reports, which runs a little under the nominal size.
    """
    if not 1 <= jobs <= MAX_JOBS:
        raise PivError(f"--jobs must be from 1 to {MAX_JOBS}, not {jobs}", 2)
    raw, kind = runner_arch(arch)
    if ram_bytes is None:
        ram_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    if disk_free_bytes is None:
        disk_free_bytes = shutil.disk_usage(nearest_existing(BASE)).free
    ram_gb, disk_gb = ram_bytes / 2**30, disk_free_bytes / 2**30
    cpu_count = cpus if cpus is not None else (os.cpu_count() or 1)
    if ram_gb < 1.75 * jobs:
        raise PivError(f"{ram_gb:.1f} GB of RAM cannot carry {jobs} runner instance(s): it needs {1.75 * jobs:.1f} GB (use fewer --jobs)")
    if disk_gb < 5 * jobs:
        raise PivError(f"{disk_gb:.1f} GB of free disk cannot carry {jobs} runner instance(s): it needs {5 * jobs} GB")
    warnings: list[str] = []
    if ram_gb < 3.5 and jobs > 1:
        warnings.append(f"{jobs} instances on {ram_gb:.1f} GB RAM: the table gives 1 (a Raspberry Pi 4/5 or a 4 GB machine; 2 only if tests are light)")
    elif ram_gb < 7 and jobs > 2:
        warnings.append(f"{jobs} instances on {ram_gb:.1f} GB RAM: the table gives 1, or 2 if tests are light")
    elif ram_gb < 7 and jobs == 2:
        warnings.append(f"2 instances on {ram_gb:.1f} GB RAM is fine only if your tests are light")
    if model is None:
        model = device_model()
    if "Raspberry Pi" in model and jobs > 1:
        warnings.append(f"this is a {model}: the table gives a Raspberry Pi 1 instance, whatever its RAM")
    if jobs > 3:
        warnings.append(f"{jobs} instances is more than the table's 2-3 for an 8 GB machine")
    if jobs > cpu_count:
        warnings.append(f"{jobs} instances on {cpu_count} CPU(s): jobs will compete for them")
    if disk_gb < 10 * jobs:
        warnings.append(f"{disk_gb:.1f} GB free disk is under the 10 GB per instance that builds comfortably need")
    return Machine(raw, kind, ram_gb, disk_gb, cpu_count, tuple(warnings))


def parse_target(value: str | None, org: str | None, repo: str | None) -> Target | None:
    """The target the user named, or None when they named none. Names are checked like `piv init`'s."""
    named = [item for item in (value, org, repo) if item]
    if len(named) > 1:
        raise PivError("name one target: a positional ORG or OWNER/REPO, --org or --repo", 2)
    if org or (value and "/" not in value):
        name = org or value or ""
        if not OWNER.fullmatch(name):
            raise PivError(f"{name!r} is not an organization name", 2)
        return Target("org", name)
    if repo or value:
        name = repo or value or ""
        owner, _, short = name.partition("/")
        if not (OWNER.fullmatch(owner) and NAME.fullmatch(short) and short not in (".", "..")):
            raise PivError(f"{name!r} is not OWNER/NAME", 2)
        return Target("repo", name)
    return None


def detect_target(gh: Gh) -> Target:
    """With no target named: the one organization you belong to, else an explanation (never a guess)."""
    try:
        orgs = json.loads(gh.api("user/orgs"))
        logins = sorted(o["login"] for o in orgs)
    except (ValueError, TypeError, KeyError):
        raise PivError("gh api user/orgs did not answer with a list of organizations") from None
    if len(logins) == 1:
        return Target("org", logins[0])
    if logins:
        raise PivError(f"you belong to several organizations ({', '.join(logins)}): name one with --org", 2)
    raise PivError("\n".join([*account_advice(Target("repo", "OWNER/NAME")), "Name the repo with --repo OWNER/NAME, or create the organization first."]), 2)


def runner_name(prefix: str | None) -> str:
    name = prefix or socket.gethostname().split(".")[0]
    if not RUNNER_NAME.fullmatch(name):
        raise PivError(f"the runner name {name!r} must be letters, digits, '.', '_' or '-' (set it with --name)", 2)
    return name


def unit_text(memory_max: int) -> str:
    """The template unit. Its control-group stop kills a job's leftovers too; no token or secret is in it.

    ``memory_max`` is megabytes per instance (memory_max_mb), so the --jobs table is enforced, not just printed.
    """
    return f"""[Unit]
Description=GitHub Actions runner for Pivotal (instance %i)
After=network-online.target
Wants=network-online.target

[Service]
User={USER}
WorkingDirectory={BASE}/runner-%i
Environment=PV_RUNNER_WORK={BASE}/runner-%i/_work
Environment=ACTIONS_RUNNER_HOOK_JOB_STARTED={BASE}/hooks/job-started.sh
Environment=ACTIONS_RUNNER_HOOK_JOB_COMPLETED={BASE}/hooks/job-completed.sh
ExecStart={BASE}/runner-%i/run.sh
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=60
PrivateTmp=yes
MemoryMax={memory_max}M

[Install]
WantedBy=multi-user.target
"""


def hook_text(mode: str) -> str:
    """The clean-up script for ``started`` or ``completed``: pv-clean.sh with its mode line set."""
    text = HOOK_SOURCE.read_text(encoding="utf-8")
    line = 'mode="completed"'
    if text.count(line) != 1:
        raise PivError(f"{HOOK_SOURCE} has no single {line} line to set")
    return text.replace(line, f'mode="{mode}"')


def as_user(*argv: str) -> tuple[str, ...]:
    return ("runuser", "-u", USER, "--", *argv)


def build_plan(
    target: Target,
    jobs: int,
    name: str | None = None,
    *,
    arch: str | None = None,
    ram_bytes: int | None = None,
    disk_free_bytes: int | None = None,
    cpus: int | None = None,
    model: str | None = None,
    ex: "Executor | None" = None,
) -> Plan:
    """The whole installation as steps. Reads the machine; changes nothing; refuses an unfit machine
    and an existing install that is registered elsewhere or under another name."""
    machine = check_machine(jobs, arch=arch, ram_bytes=ram_bytes, disk_free_bytes=disk_free_bytes, cpus=cpus, model=model)
    prefix = runner_name(name)
    beyond = check_existing(target, jobs, prefix, ex) if ex else []
    archive = f"actions-runner-linux-{machine.runner_arch}-{RUNNER_VERSION}.tar.gz"
    url = f"https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/{archive}"
    dist, hooks = f"{BASE}/dist", f"{BASE}/hooks"
    steps = [
        Step("user", f"create the OS user {USER} (home {HOME}, no password) unless it exists",
             argv=("useradd", "--create-home", "--home-dir", HOME, "--shell", "/bin/bash", "--user-group", USER)),
        Step("mkdir", f"create {BASE} (root, 0755)", path=BASE, mode=0o755),
        Step("mkdir", f"create {hooks} (root, 0755: {USER} cannot edit its own hooks)", path=hooks, mode=0o755),
        Step("write", f"install {hooks}/job-started.sh: kill leftover processes of the instance, wipe its workspaces in _work (the runner's _temp, _actions, _tool stay)",
             path=f"{hooks}/job-started.sh", content=hook_text("started"), mode=0o755),
        Step("write", f"install {hooks}/job-completed.sh: kill leftover processes of the instance, wipe its workspaces in _work (the runner's _temp, _actions, _tool stay)",
             path=f"{hooks}/job-completed.sh", content=hook_text("completed"), mode=0o755),
        Step("mkdir", f"create {dist} (root, 0755)", path=dist, mode=0o755),
        Step("run", f"download GitHub's runner v{RUNNER_VERSION} ({machine.runner_arch}) from {url}",
             argv=("curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https", "--output", f"{dist}/{archive}", url)),
        Step("run", f"make the archive readable by {USER}", argv=("chmod", "0644", f"{dist}/{archive}")),
        Step("run", f"verify the archive against SHA-256 {RUNNER_SHA256[machine.runner_arch]}; stop if it differs",
             argv=("sha256sum", "--check", "--strict", "--status"), stdin=f"{RUNNER_SHA256[machine.runner_arch]}  {dist}/{archive}\n"),
    ]
    for i in range(1, jobs + 1):
        home = f"{BASE}/runner-{i}"
        steps += [
            Step("mkdir", f"create {home} ({USER}, 0700)", path=home, mode=0o700, owner=USER),
            Step("run", f"unpack the runner into {home} as {USER}", cwd=home, skip_if_exists=f"{home}/.runner",
                 argv=as_user("tar", "--extract", "--gzip", "--file", f"{dist}/{archive}", "--directory", home)),
            Step("run", f"register {prefix}-{i} to {target.url} as {USER} with the one-hour token (in the environment of that process only; it also creates _work)",
                 cwd=home, token=True, skip_if_exists=f"{home}/.runner",
                 argv=as_user(f"{home}/config.sh", "--unattended", "--url", target.url, "--name", f"{prefix}-{i}", "--work", "_work", "--replace")),
        ]
    steps.append(Step("write", f"install the systemd template unit {UNIT_PATH}", path=UNIT_PATH, content=unit_text(memory_max_mb(machine.ram_gb, jobs)), mode=0o644))
    if beyond:
        listed = ", ".join(f"runner-{i}" for i in beyond)
        steps.append(Step("note", f"installed and not part of --jobs {jobs}: {listed}. They stay as they are; `piv runner remove` takes all instances off before a smaller install"))
    steps.append(Step("run", "reload systemd", argv=("systemctl", "daemon-reload")))
    for i in range(1, jobs + 1):
        steps.append(Step("run", f"enable and start pv-runner@{i}.service (persistent: no re-registration after a reboot)",
                          argv=("systemctl", "enable", "--now", f"pv-runner@{i}.service")))
    return Plan(target, machine, tuple(steps), "registration-token", "add")


def read_registration(ex: "Executor", home: str) -> tuple[str, str] | None:
    """(gitHubUrl, agentName) from an instance's .runner, None when it is not registered; ValueError when unreadable."""
    path = f"{home}/.runner"
    if not ex.exists(path):
        return None
    try:
        data = json.loads(ex.read_text(path))
        return str(data["gitHubUrl"]), str(data["agentName"])
    except (OSError, ValueError, KeyError, TypeError):
        raise PivError(f"cannot read {path}: remove the install first with `piv runner remove`") from None


def check_existing(target: Target, jobs: int, prefix: str, ex: "Executor") -> list[int]:
    """Refuse an install that would be skipped yet is not what was asked for. Returns the instances beyond ``jobs``."""
    found = ex.instances()
    for i in found:
        if i > jobs:
            continue
        registered = read_registration(ex, f"{BASE}/runner-{i}")
        if registered is None:
            continue
        url, agent = registered
        if url.rstrip("/").lower() != target.url.lower():
            raise PivError(f"runner-{i} is registered to {url}, not {target.url}: run `piv runner remove` first, then add again")
        if agent != f"{prefix}-{i}":
            raise PivError(f"runner-{i} is registered as {agent!r}, not {prefix}-{i!s}: run `piv runner remove` first, or use --name {agent.rsplit('-', 1)[0]}")
    return [i for i in found if i > jobs]


def build_removal_plan(target: Target | None, instances: list[int]) -> Plan:
    steps: list[Step] = []
    for i in instances:
        assert target
        home = f"{BASE}/runner-{i}"
        steps += [
            Step("run", f"stop and disable pv-runner@{i}.service", tolerate_failure=True,
                 argv=("systemctl", "disable", "--now", f"pv-runner@{i}.service")),
            Step("run", f"deregister runner {i} from {target.url} as {USER} with the one-hour token", cwd=home, token=True,
                 only_if_exists=f"{home}/.runner", argv=as_user(f"{home}/config.sh", "remove")),
            Step("rmtree", f"delete {home}", path=home),
        ]
    steps += [
        Step("rmfile", f"delete {UNIT_PATH}", path=UNIT_PATH),
        Step("run", "reload systemd", argv=("systemctl", "daemon-reload")),
        Step("rmfile", f"delete {BASE}/hooks/job-started.sh", path=f"{BASE}/hooks/job-started.sh"),
        Step("rmfile", f"delete {BASE}/hooks/job-completed.sh", path=f"{BASE}/hooks/job-completed.sh"),
        Step("rmdir", f"delete {BASE}/hooks if empty", path=f"{BASE}/hooks"),
        Step("rmtree", f"delete {BASE}/dist (the downloaded archive)", path=f"{BASE}/dist"),
        Step("rmdir", f"delete {BASE} if empty", path=BASE),
    ]
    steps.append(Step("note", f"the user {USER} and its home {HOME} stay, with the CLI logins in it (remove them yourself with userdel)"))
    return Plan(target, None, tuple(steps), "remove-token", "remove")


_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class Executor:
    """Everything that touches the host. The real one is below; tests pass a recording fake."""

    def euid(self) -> int:
        raise NotImplementedError

    def which(self, tool: str) -> bool:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        raise NotImplementedError

    def user_exists(self, user: str) -> bool:
        raise NotImplementedError

    def instances(self) -> list[int]:
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def run(self, argv: tuple[str, ...], *, cwd: str = "", stdin: str = "", env: dict[str, str] | None = None) -> None:
        raise NotImplementedError

    def make_dir(self, path: str, mode: int, owner: str) -> None:
        raise NotImplementedError

    def write_file(self, path: str, content: str, mode: int) -> None:
        raise NotImplementedError

    def remove_tree(self, path: str) -> None:
        raise NotImplementedError

    def remove_file(self, path: str) -> None:
        raise NotImplementedError

    def remove_dir_if_empty(self, path: str) -> None:
        raise NotImplementedError


class RealExecutor(Executor):
    base = BASE

    def euid(self) -> int:
        return os.geteuid()

    def which(self, tool: str) -> bool:
        return shutil.which(tool, path=_PATH) is not None

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def user_exists(self, user: str) -> bool:
        import pwd

        try:
            pwd.getpwnam(user)
        except KeyError:
            return False
        return True

    def instances(self) -> list[int]:
        try:
            names = os.listdir(self.base)
        except OSError:
            return []
        return sorted(int(m.group(1)) for m in map(INSTANCE.fullmatch, names) if m)

    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8-sig")

    def run(self, argv: tuple[str, ...], *, cwd: str = "", stdin: str = "", env: dict[str, str] | None = None) -> None:
        # A clean environment: the caller's GH_TOKEN and the like never reach what is run here.
        clean = {"PATH": _PATH, "LANG": "C.UTF-8", **(env or {})}
        try:
            done = subprocess.run(argv, input=stdin.encode(), capture_output=True, check=False, env=clean, cwd=cwd or None, timeout=900)
        except OSError as err:
            raise PivError(f"cannot run {argv[0]}: {err.strerror}") from None
        except subprocess.TimeoutExpired:
            raise PivError(f"{argv[0]} timed out") from None
        if done.returncode != 0:
            lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise PivError(f"{argv[0]} failed (exit {done.returncode})" + (f": {lines[0]}" if lines else ""))

    def make_dir(self, path: str, mode: int, owner: str) -> None:
        # The last component is opened without following a symlink and must be a directory; mode and
        # owner are set on that descriptor, so a swapped-in link to /etc is refused, never chmod'd.
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(path)
        except FileExistsError:
            pass
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        except OSError:
            raise PivError(f"refusing {path}: it is not a plain directory (a symlink or a file)") from None
        try:
            os.fchmod(fd, mode)
            if owner != "root":
                import pwd

                entry = pwd.getpwnam(owner)
                os.fchown(fd, entry.pw_uid, entry.pw_gid)
        finally:
            os.close(fd)

    def write_file(self, path: str, content: str, mode: int) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and renamed, so a running job never reads half a hook.
        part = target.with_name(target.name + ".part")
        part.write_text(content, encoding="utf-8")
        os.chmod(part, mode)
        os.replace(part, target)

    def remove_tree(self, path: str) -> None:
        # The plan names only paths under /opt/pv-runner; this is the second lock, not the first.
        target = Path(path)
        if not target.is_absolute() or len(target.parts) < 4 or target.is_symlink():
            raise PivError(f"refusing to delete {path}")
        if target.exists():
            shutil.rmtree(target)

    def remove_file(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)

    def remove_dir_if_empty(self, path: str) -> None:
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass  # not empty: something else lives there, so it stays



def _skipped(step: Step, ex: Executor) -> bool:
    if step.skip_if_exists and ex.exists(step.skip_if_exists):
        return True
    if step.only_if_exists and not ex.exists(step.only_if_exists):
        return True
    return step.op == "user" and ex.user_exists(USER)


def execute(plan: Plan, gh: Gh, ex: Executor, say: Callable[[str], None]) -> None:
    """Run a plan. Everything that can be refused is refused before the first step changes anything."""
    if ex.euid() != 0:
        raise PivError(f"this changes users, /opt and systemd, so it must run as root (piv will not escalate): sudo --preserve-env=GH_TOKEN,HOME gh piv runner {plan.verb} ... --yes")
    missing = [tool for tool in TOOLS if not ex.which(tool)]
    if missing:
        raise PivError(f"needed and not found: {', '.join(missing)}")
    token = ""
    if any(step.token and not _skipped(step, ex) for step in plan.steps):
        # The one request that can fail on permissions (404/403: not an admin), and it fails before any change.
        assert plan.target
        token = str(gh.api_json(f"{plan.target.api}/{plan.token_kind}", method="POST").get("token") or "")
        if not token:
            raise PivError(f"GitHub gave no {plan.token_kind} for {plan.target.name}")
    for step in plan.steps:
        if step.op == "note":
            say(step.text)
            continue
        if _skipped(step, ex):
            say(f"skip (already done): {step.text}")
            continue
        say(step.text)
        try:
            _apply(step, ex, {"ACTIONS_RUNNER_INPUT_TOKEN": token} if step.token else None)
        except PivError as err:
            if step.tolerate_failure:
                say(f"  ignored: {err}")
                continue
            raise PivError(str(err).replace(token, "***") if token else str(err), err.code) from None
    say("done.")
    if plan.verb == "add":
        say(f"Now log your subscription CLIs in as {USER}:")
        for line in LOGIN_LINES:
            say(f"  {line}")


def _apply(step: Step, ex: Executor, env: dict[str, str] | None) -> None:
    if step.op in ("user", "run"):
        ex.run(step.argv, cwd=step.cwd, stdin=step.stdin, env=env)
    elif step.op == "mkdir":
        ex.make_dir(step.path, step.mode, step.owner)
    elif step.op == "write":
        ex.write_file(step.path, step.content, step.mode)
    elif step.op == "rmtree":
        ex.remove_tree(step.path)
    elif step.op == "rmfile":
        ex.remove_file(step.path)
    elif step.op == "rmdir":
        ex.remove_dir_if_empty(step.path)
    else:
        raise PivError(f"unknown step {step.op}")


def removal_target(explicit: Target | None, instances: list[int], ex: Executor) -> Target:
    """The target to deregister from: the one named, else the one the installed runners are registered to."""
    if explicit:
        return explicit
    found: set[tuple[str, str]] = set()
    for i in instances:
        try:
            url = json.loads(ex.read_text(f"{BASE}/runner-{i}/.runner")).get("gitHubUrl", "")
        except (OSError, ValueError, AttributeError):
            continue
        match = GITHUB_URL.fullmatch(url) if isinstance(url, str) else None
        if match:
            found.add(("repo", f"{match[1]}/{match[2]}") if match[2] else ("org", match[1]))
    if len(found) != 1:
        raise PivError("cannot tell where the runners are registered: name it with --org ORG or --repo OWNER/NAME", 2)
    kind, name = found.pop()
    return Target(kind, name)
