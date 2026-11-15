"""The pinned worker CLIs of a job, installed on a GitHub-hosted runner (engine/setup/action.yml; docs/engines.md).

The composite action runs this with ``python3 -I`` after the engine checkout. It holds no secret: npm and each installed
CLI run with a bare environment (PATH, a scratch HOME, the locale), and the step has none of the job's secrets. It installs
the workers that the preset's build and review tiers name, from pins.json, each into its own prefix under RUNNER_TEMP, and
checks the version the CLI reports against the pin. It writes ``RUNNER_TEMP/pivotal-workers.json``: the install directory,
the absolute path of the pinned binary and the digest of every installed tree, which the build and review jobs read before
any agent starts (engine/workers.py). Nothing is put on PATH: no GITHUB_PATH entry is written, so a later step finds no CLI
through it, and the jobs run a pinned CLI by its absolute path. What is guaranteed, and what is not, is in docs/engines.md
(the worker install).

A worker that cannot be installed (no pin for this platform, a node too old for it, an npm failure, a download failure, a
sha256 mismatch, a version other than the pin) is a ``::warning::`` and is left out of the manifest, so the job falls through
to the next entry with the reason ``not installed`` and never runs a missing binary (exit 127). A worker named by a tier but
absent from pins.json refuses the install (exit 1): a new worker is pinned before a preset may name it.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:  # the engine's own modules: the same resolution the jobs use
    sys.path.insert(0, str(ROOT))

from engine import job, lines, plan, workers  # noqa: E402
from engine.job import JobError  # noqa: E402

PINS = HERE / "pins.json"
NPM_SECONDS = 900
PROBE_SECONDS = 60
DOWNLOAD_SECONDS = 120
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024  # 400 MiB hard cap; the pinned omp binary is 287 MiB


def setup_seconds(count: int | None = None, pins_path: Path = PINS) -> int:
    """The worst case of the setup step for the pinned workers. If ``count`` is given, it is used as the number of
    workers (legacy call from the test); otherwise the actual pins are read to compute the worst case per worker type.
    An npm worker takes NPM_SECONDS + 2*PROBE_SECONDS; a direct-download worker takes DOWNLOAD_SECONDS + PROBE_SECONDS.
    The jobs budget this time in their own timeout (buildjob.SETUP_MINUTES; test_setup holds the two together, F8)."""
    if count is not None:
        # Legacy call: assume all are npm workers (the test passes len(PINS["workers"])).
        return count * (NPM_SECONDS + 2 * PROBE_SECONDS)
    data = read_pins(pins_path)
    total = 0
    for pin in data["workers"].values():
        if "npm" in pin:
            total += NPM_SECONDS + 2 * PROBE_SECONDS
        elif "url" in pin and "sha256" in pin:
            total += DOWNLOAD_SECONDS + PROBE_SECONDS
    return total
MANIFEST = "pivotal-workers.json"
VERSION_LINE = re.compile(r"\d+(?:\.\d+)*")


def parse_version(first_word: str) -> str | None:
    """Parse the version from the first word of --version output.
    
    Accepts:
    - Bare version: "18.20.0"
    - Name/version: "omp/18.20.0" (takes part after last '/')
    
    Returns the version string if it matches VERSION_LINE, else None.
    """
    if not first_word:
        return None
    # If the word contains '/', take the part after the last '/'
    if '/' in first_word:
        version = first_word.rsplit('/', 1)[-1]
    else:
        version = first_word
    return version if VERSION_LINE.fullmatch(version) else None
MINIMAL_LOCALE = ("LANG", "LC_ALL", "LC_CTYPE")


def read_pins(path: Path = PINS) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("workers"), dict) \
            or not isinstance(data.get("unavailable"), dict):
        raise JobError("engine/setup/pins.json has the wrong shape")
    return data


def chain_workers(env: dict) -> list[str]:
    """The workers the build and review tiers of the job's preset name, resolved exactly as the build job resolves them.
    A line or preset that does not resolve is a refusal (JobError), never a traceback."""
    try:
        line = lines.resolve(env.get("LINE") or lines.DEFAULT, env.get("PRESET_JSON", ""), ROOT)
        name = lines.preset_for(line, "" if line.preset else job.read_preset_name(env.get("PRESET", "")))
    except lines.LineError as err:
        raise JobError(str(err)) from None
    preset = plan.load_preset(env, name)
    return sorted({entry.worker for tier in ("build", "review") for entry in preset.tiers.get(tier, ())})


def bare_env(home: Path, path: str, env: dict) -> dict[str, str]:
    """The environment of npm and of every installed CLI: a fixed PATH, a scratch HOME, the locale, and nothing else."""
    keep = {"PATH": path, "HOME": str(home), "LANG": env.get("LANG", "C.UTF-8")}
    for name in MINIMAL_LOCALE:
        if env.get(name):
            keep[name] = env[name]
    keep["DISABLE_AUTOUPDATER"] = "1"
    return keep


def node_version(path: str) -> tuple[int, ...] | None:
    node = shutil.which("node", path=path)
    if node is None:
        return None
    done = subprocess.run([node, "--version"], capture_output=True, text=True, check=False, timeout=PROBE_SECONDS,
                          env={"PATH": path, "HOME": tempfile.gettempdir()}, stdin=subprocess.DEVNULL)
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", done.stdout)
    return tuple(int(part) for part in match.groups()) if done.returncode == 0 and match else None


def reason_unavailable(worker: str, pin: dict, path: str) -> str:
    """"" when this runner can install the worker, else the reason it cannot."""
    wanted = pin.get("platform")
    if wanted:
        machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), "")
        here = f"{platform.system().lower()}-{machine}"
        if here != wanted:
            return f"no pinned install for {here} (pinned for {wanted})"
    if pin.get("node_min"):
        have = node_version(path)
        want = tuple(int(part) for part in pin["node_min"].split("."))
        if have is None:
            return "node is not on PATH"
        if have < want:
            return f"node {'.'.join(map(str, have))} is older than the pinned minimum {pin['node_min']}"
    return ""


def _download_and_verify(url: str, expected_sha256: str, dest: Path, timeout: int) -> None:
    """Download a file from ``url`` to ``dest`` and verify its sha256. Raises JobError on failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            hasher = hashlib.sha256()
            total = 0
            chunks = []
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise JobError(f"download exceeds {MAX_DOWNLOAD_BYTES} bytes")
                hasher.update(chunk)
                chunks.append(chunk)
            data = b"".join(chunks)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as err:
        raise JobError(f"download failed: {err}") from None
    actual = hasher.hexdigest()
    if actual != expected_sha256:
        raise JobError(f"sha256 mismatch: expected {expected_sha256}, got {actual}")
    dest.write_bytes(data)
    dest.chmod(0o755)


def _install_npm(worker: str, pin: dict, base: Path, path: str, env: dict) -> dict:
    """Install an npm-based worker. Returns its manifest entry; raises JobError with the reason it could not."""
    home = base / "home" / worker
    home.mkdir(parents=True, exist_ok=True)
    prefix = base / worker
    shutil.rmtree(prefix, ignore_errors=True)
    prefix.mkdir(parents=True)
    npm = shutil.which("npm", path=path)
    if npm is None:
        raise JobError("npm is not on PATH")
    env = bare_env(home, path, env)
    argv = [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", str(prefix),
            f"{pin['npm']}@{pin['version']}"]
    done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=NPM_SECONDS, env=env,
                          cwd=str(home), stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise JobError(f"npm install failed (exit {done.returncode})")
    binary = Path(os.path.realpath(prefix / pin["bin"]))
    root = Path(os.path.realpath(prefix))
    if not binary.is_file() or not str(binary).startswith(str(root) + os.sep):
        raise JobError(f"the pinned binary is not in the install: {pin['bin']}")
    probe = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=False,
                           timeout=PROBE_SECONDS, env=env, cwd=str(home), stdin=subprocess.DEVNULL)
    words = probe.stdout.split()
    version = parse_version(words[0]) if words else None
    if probe.returncode != 0 or version is None or version != pin["version"]:
        raise JobError(f"the installed CLI does not report the pinned version {pin['version']}")
    return {"root": str(root), "bin": str(binary), "digest": workers.digest(root)}


def _install_direct(worker: str, pin: dict, base: Path, path: str, env: dict) -> dict:
    """Install a direct-download worker (omp). Returns its manifest entry; raises JobError with the reason it could not."""
    home = base / "home" / worker
    home.mkdir(parents=True, exist_ok=True)
    prefix = base / worker
    shutil.rmtree(prefix, ignore_errors=True)
    prefix.mkdir(parents=True)
    binary_path = prefix / pin["bin"]
    _download_and_verify(pin["url"], pin["sha256"], binary_path, DOWNLOAD_SECONDS)
    binary = Path(os.path.realpath(binary_path))
    root = Path(os.path.realpath(prefix))
    if not binary.is_file() or not str(binary).startswith(str(root) + os.sep):
        raise JobError(f"the pinned binary is not in the install: {pin['bin']}")
    probe = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=False,
                           timeout=PROBE_SECONDS, env=bare_env(home, path, env), cwd=str(home),
                           stdin=subprocess.DEVNULL)
    words = probe.stdout.split()
    version = parse_version(words[0]) if words else None
    if probe.returncode != 0 or version is None or version != pin["version"]:
        raise JobError(f"the installed CLI does not report the pinned version {pin['version']}")
    return {"root": str(root), "bin": str(binary), "digest": workers.digest(root)}


def install(worker: str, pin: dict, base: Path, path: str, env: dict) -> dict:
    """Install one pinned worker. Returns its manifest entry; raises JobError with the reason it could not."""
    if "npm" in pin:
        return _install_npm(worker, pin, base, path, env)
    if "url" in pin and "sha256" in pin:
        return _install_direct(worker, pin, base, path, env)
    raise JobError(f"pin for {worker} has no recognized install method (npm or url+sha256)")


def main(env: dict, pins_path: Path = PINS) -> int:
    temp = env.get("RUNNER_TEMP", "")
    if not temp or not Path(temp).is_absolute():
        print("RUNNER_TEMP is not set: nowhere to install the workers", file=sys.stderr)
        return 1
    base = Path(temp)
    pins = read_pins(pins_path)
    path = env.get("PATH", "/usr/bin:/bin")
    names = chain_workers(env)
    unknown = [n for n in names if n not in pins["workers"] and n not in pins["unavailable"]]
    if unknown:
        print(f"refused: no pin in engine/setup/pins.json for {', '.join(unknown)}: pin a worker before a preset names it",
              file=sys.stderr)
        return 1
    installed: dict[str, dict] = {}
    for worker in names:
        if worker in pins["unavailable"]:
            print(f"::warning::setup: {worker} not installed: {pins['unavailable'][worker]}")
            continue
        pin = pins["workers"][worker]
        try:
            if reason := reason_unavailable(worker, pin, path):
                raise JobError(reason)
            installed[worker] = install(worker, pin, base, path, env)
        except (JobError, subprocess.SubprocessError, OSError) as err:
            print(f"::warning::setup: {worker} not installed: {err}")
            continue
        print(f"setup: {worker} {pin['version']} installed")
    manifest = {"schema": workers.SCHEMA, "workers": installed}
    (base / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(dict(os.environ)))
    except JobError as err:
        print(f"refused: {err}", file=sys.stderr)
        sys.exit(1)