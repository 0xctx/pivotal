"""`piv preset check` (docs/engines.md): static checks of a shipped preset, then one real call per entry.

The static checks use the engine's own rules (engine/route.py: the grammar, the vendors, the ``measured`` list) and the Kaggle
serve rule (kaggle/render.py: ``pick`` and ``spec_for``), so a rule is never copied here. The probe runs each entry with the
same argv the factory runs (route.worker_argv, T0-measured flags only) and a one-word prompt. A worker T0 has not measured
is a failure. A missing secret, a non-zero exit, a missing binary, a timeout or an empty answer is a failure, and every
failure makes the check exit 1 (F5). A child receives only the one secret its door needs, PATH and HOME.

An answer is "exit 0 with non-empty output" (docs/engines.md: auth failure text is UNMEASURED for every CLI, so the check
cannot distinguish a real answer from an error body that exits 0). ``ok`` means the probe ran, not that the model is
authenticated. Nothing here reads a secret value into an output line.
"""

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
PROMPT = "ok"  # one word: the probe asks for the shortest answer the flags allow (no output-token cap is measured)
TIMEOUT = 120.0  # seconds per entry; a Kaggle endpoint that is starting can take minutes, so this is generous
ENV_KEEP = ("PATH", "HOME")
SHOWN = 160  # characters of a CLI's own message that a failure line carries


def load_engine(root: Path):
    """The engine's route module and kaggle/render.py, both from ``root``. Raises if either will not load (fail closed)."""
    saved = list(sys.path)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from engine import route  # noqa: PLC0415  imported here so an import failure is a failure line, not a crash
        spec = importlib.util.spec_from_file_location("kaggle_render", root / "kaggle" / "render.py")
        render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(render)
    finally:
        sys.path[:] = saved  # render.py inserts gh-piv on import; leave no caller's import path changed (S1)
    return route, render


def unique_entries(preset) -> list:
    seen, entries = set(), []
    for tier in ("build", "review"):
        for entry in preset.tiers.get(tier, ()):
            if entry.spec not in seen:
                seen.add(entry.spec)
                entries.append(entry)
    return entries


def static_problems(root: Path, preset, route, render) -> list[str]:
    """Every reason the preset cannot run as written, before anything is called. Empty when it can."""
    problems = [str(p) for p in route.cross_vendor_problems(preset)]
    for spec in sorted(preset.measured):
        entry = route.parse_entry(spec)
        if entry.worker not in route.MEASURED_WORKERS:
            problems.append(f"{spec}: marked measured, but T0 has not measured the {entry.worker} CLI's flags (docs/engines.md)")
    # Validate kaggle.model whenever it is set (S5), even if no entry currently uses the kaggle door.
    if preset.kaggle_model is not None or any(e.door == "kaggle" for e in unique_entries(preset)):
        if preset.kaggle_model is None:
            kaggle = next(e for e in unique_entries(preset) if e.door == "kaggle")
            problems.append(f"{kaggle.spec}: a kaggle entry needs the preset's kaggle: {{\"model\": \"<catalog model id>\"}}")
        else:
            try:
                catalog = json.loads((root / "kaggle" / "catalog.json").read_text(encoding="utf-8"))
                pins = json.loads((root / "kaggle" / "pins.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                return problems + [f"kaggle catalog or pins unreadable: {error}"]
            try:
                render.spec_for(render.pick(catalog, preset.kaggle_model), pins)
            except render.RenderError as error:
                model = preset.kaggle_model
                problems.append(f"kaggle.model {model}: {error}")
    return problems


def _shown(text: str, secrets: list[str]) -> str:
    """One line of a CLI's own message, with every secret value removed and control characters flattened."""
    line = next((part for part in text.splitlines() if part.strip()), "").strip()
    for secret in secrets:
        line = line.replace(secret, "<redacted>")
    line = re.sub(r"[\x00-\x1f\x7f]", " ", line)
    return line[:SHOWN]


def secret_values(env: Mapping[str, str], route) -> list[str]:
    """Every secret value present in ``env``, for redaction: a CLI may echo any of them, not only the one its door takes."""
    names = {name for group in route.DOOR_SECRETS.values() for name in group}
    return [env[name] for name in sorted(names) if env.get(name)]


def probe(entry, route, env, *, timeout: float) -> tuple[str | None, str]:
    """Run one entry's worker for one short call. Returns (failure text or None, the note for an ok line)."""
    spec = entry.spec
    if entry.worker not in route.MEASURED_WORKERS:
        return f"{spec}: unmeasured (T0 has not measured the {entry.worker} CLI's flags: docs/engines.md)", ""
    names = route.secret_names(entry)
    name = next((n for n in names if env.get(n)), None)
    if name is None:
        return f"{spec}: missing secret (set {' or '.join(names)})", ""
    child = {key: env[key] for key in ENV_KEEP if key in env}
    child[name] = env[name]
    argv = route.worker_argv(entry, PROMPT)  # same command shape the factory runs (I1)
    try:
        proc = subprocess.Popen(argv, env=child, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
    except FileNotFoundError:
        return f"{spec}: binary not found: {entry.worker} (not on PATH)", ""
    except OSError as error:
        return f"{spec}: could not run {entry.worker}: {error.strerror or type(error).__name__}", ""
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return f"{spec}: timed out after {timeout:g}s", ""
    except (OSError, ValueError) as error:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        return f"{spec}: could not run {entry.worker}: {getattr(error, 'strerror', None) or type(error).__name__}", ""
    if proc.returncode != 0:
        out = stderr if stderr and stderr.strip() else (stdout or "")
        return f"{spec}: exit {proc.returncode}: {_shown(out, secret_values(env, route))}", ""
    if not (stdout or "").strip():
        return f"{spec}: no output (exit 0; docs/engines.md: auth failure text is unmeasured)", ""
    return None, "answered (exit 0 with output; auth not verified without T0 failure text)"


def check_one(root: Path, name: str, env: Mapping[str, str], out: TextIO, err: TextIO, *, timeout: float) -> int:
    path = root / "presets" / f"{name}.json"
    if not path.is_file():
        print(f"failed: preset {name} not found", file=err)
        return 1
    try:
        route, render = load_engine(root)
    except Exception as error:  # noqa: BLE001  any failure to load the engine refuses the check (F5)
        print(f"failed: engine unavailable: {type(error).__name__}: {error}", file=err)
        return 1
    try:
        preset = route.load_preset(path)
    except (OSError, ValueError, RecursionError) as error:
        print(f"failed: bad preset {name}: {error}", file=err)
        return 1
    problems = static_problems(root, preset, route, render)
    if problems:
        for problem in problems:
            print(f"failed: {name}: {problem}", file=err)
        return 1
    failures = 0
    for entry in unique_entries(preset):
        failure, note = probe(entry, route, env, timeout=timeout)
        if failure is not None:
            print(f"failed: {failure}", file=err)
            failures += 1
        else:
            print(f"ok: {entry.spec} {note}", file=out)
    if failures:
        print(f"failed: preset {name}: {failures} entr{'y' if failures == 1 else 'ies'} did not pass", file=err)
        return 1
    print(f"ok: preset {name} passed check", file=out)
    return 0


def check_names(root: Path, names: list[str], env: Mapping[str, str], out: TextIO, err: TextIO, *,
                timeout: float = TIMEOUT) -> int:
    """Check each named preset. Exit 2 for no name or a name that is not [a-z0-9][a-z0-9-]*, before any work; 1 if any fails."""
    if not names:
        print("piv: no preset named: piv preset check NAME [NAME ...] (or --all)", file=err)
        return 2
    for name in names:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            print(f"piv: bad preset name {name!r}: want lowercase letters, digits and -, starting with a letter or digit", file=err)
            return 2
    results = [check_one(root, name, env, out, err, timeout=timeout) for name in names]
    return 1 if any(results) else 0
