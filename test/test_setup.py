"""engine/setup/install.py and pins.json: the pinned worker CLIs a job installs (docs/engines.md, F3, F5).

No test reaches the network or a registry. ``npm`` and ``node`` are fakes written into each test's own directory, and the
fake npm writes a file that holds the tree the real npm would write (the bin the pin names, a version that answers
``--version``). Every test runs install.main with its own RUNNER_TEMP, PATH and GITHUB_PATH.
"""

import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import nolive  # noqa: F401  (no test sweeps the live /proc)
from engine import buildjob, plan, route, workers
from engine.job import JobError

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "engine" / "setup"
PINS = json.loads((SETUP / "pins.json").read_text(encoding="utf-8"))
ENGINES_DOC = (ROOT / "docs" / "engines.md").read_text(encoding="utf-8")
SECRET_NAMES = ("PIVOTAL_KAGGLE_KEY", "OPENAI_API_KEY", "GH_TOKEN", "ANTHROPIC_API_KEY")


def load_install():
    spec = importlib.util.spec_from_file_location("install_under_test", SETUP / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load_install()

FAKE_NPM = """#!{python}
import json, os, pathlib, sys
args = sys.argv[1:]
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"argv": args, "env": sorted(os.environ)}}) + "\\n")
if {fail!r}:
    sys.stderr.write("npm ERR! simulated failure\\n")
    sys.exit(1)
prefix = pathlib.Path(args[args.index("--prefix") + 1])
spec = args[-1]
layout = {{
    "@earendil-works/pi-coding-agent@1.1.0": ("node_modules/.bin/pi", "1.1.0"),
    "@anthropic-ai/claude-code-linux-x64@2.1.312": ("node_modules/@anthropic-ai/claude-code-linux-x64/claude", "2.1.312 (Claude Code)"),
}}
rel, version = layout[spec]
if spec.startswith("@earendil") and {wrong!r}:
    version = "9.9.9"
target = prefix / rel
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text("#!" + sys.executable + "\\nimport sys\\nprint(" + repr(version) + ") if sys.argv[1:] == ['--version'] else None\\n", encoding="utf-8")
target.chmod(0o755)
"""

FAKE_NODE = """#!{python}
print({version!r})
"""


class Fakes:
    """A directory with a fake npm and a fake node first on PATH, and the log of what npm was given."""

    def __init__(self, test: unittest.TestCase, *, npm_fails=False, wrong_pi=False, node="v22.23.4"):
        self.dir = Path(tempfile.mkdtemp(prefix="setup-test-"))
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = self.dir / "fake-bin"
        self.bin.mkdir()
        self.log = self.dir / "npm.log"
        self.runner_temp = self.dir / "runner-temp"
        self.runner_temp.mkdir()
        self.github_path = self.dir / "github-path"
        self.github_path.write_text("", encoding="utf-8")
        self._write(self.bin / "npm", FAKE_NPM.format(python=sys.executable, log=str(self.log), fail=npm_fails, wrong=wrong_pi))
        self._write(self.bin / "node", FAKE_NODE.format(python=sys.executable, version=node))

    def _write(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        base = {"PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin", "RUNNER_TEMP": str(self.runner_temp),
                "GITHUB_PATH": str(self.github_path), "HOME": str(self.dir), "LANG": "C.UTF-8"}
        base.update(extra)
        return base

    def npm_calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def run_install(fakes: Fakes, env: dict, pins_path: Path = SETUP / "pins.json") -> tuple[int, str]:
    """(exit code, stdout and stderr together): a refusal is printed to stderr, a warning to stdout."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        try:
            code = install.main(env, pins_path)
        except JobError as err:
            print(f"refused: {err}")
            code = 1
    return code, out.getvalue()


PI_KAGGLE_PRESET = {
    "name": "custom", "kaggle": {"model": "qwen3.6-35b-a3b"},
    "tiers": {"build": ["pi:kaggle/qwen3.6-35b-a3b", "claude:anthropic/sonnet"],
              "review": ["agy:google/gemini-3.8-flash-high", "claude:anthropic/sonnet"]},
}


class PinsTest(unittest.TestCase):
    def test_every_worker_the_route_knows_is_pinned_or_named_unavailable(self):
        known = set(PINS["workers"]) | set(PINS["unavailable"])
        self.assertEqual(set(route.WORKER_DOORS) - known, set(), "a new worker needs a pin or an unavailable reason")

    def test_the_pins_do_not_name_a_worker_twice(self):
        self.assertEqual(set(PINS["workers"]) & set(PINS["unavailable"]), set())

    def test_agy_has_no_pin_and_no_shipped_preset_names_it(self):
        # agy is not supported (docs/engines.md): it is named unavailable, so the route still knows the word, and it
        # is pinned nowhere and used by no shipped preset.
        self.assertNotIn("agy", PINS["workers"])
        self.assertIn("agy", PINS["unavailable"])
        for path in sorted((ROOT / "presets").glob("*.json")):
            preset = route.load_preset(path)
            for tier in preset.tiers.values():
                self.assertNotIn("agy", {entry.worker for entry in tier}, path.stem)

    def test_every_shipped_preset_names_only_pinned_or_unavailable_workers(self):
        for path in sorted((ROOT / "presets").glob("*.json")):
            preset = route.load_preset(path)
            for tier in ("build", "review"):
                for entry in preset.tiers[tier]:
                    with self.subTest(preset=path.stem, spec=entry.spec):
                        self.assertIn(entry.worker, set(PINS["workers"]) | set(PINS["unavailable"]))

    def test_the_pinned_versions_are_the_ones_docs_engines_md_measured(self):
        self.assertIn("| `claude` | anthropic | 2.1.312 (Claude Code) |", ENGINES_DOC)
        self.assertIn("| `pi` | per model | 1.1.0 |", ENGINES_DOC)
        self.assertEqual(PINS["workers"]["claude"]["version"], "2.1.312")
        self.assertEqual(PINS["workers"]["pi"]["version"], "1.1.0")

    def test_the_unavailable_workers_say_why_in_docs_engines_md_terms(self):
        for worker, reason in PINS["unavailable"].items():
            with self.subTest(worker=worker):
                self.assertTrue(reason)
                self.assertTrue(re.search(r"docs/engines\.md|UNMEASURED|measured", reason))

    def test_the_pi_pin_keeps_the_node_minimum_that_pi_declares(self):
        self.assertEqual(PINS["workers"]["pi"]["node_min"], "22.19.0")


class ChainTest(unittest.TestCase):
    def test_the_workers_are_those_of_the_build_and_review_tiers_resolved_as_the_build_job_resolves_them(self):
        self.assertEqual(install.chain_workers({"PRESET": "free-kaggle"}), ["omp", "pi"])
        self.assertEqual(install.chain_workers({"PRESET": "claude-sub"}), ["claude", "omp"])
        self.assertEqual(install.chain_workers({"PRESET": "one-key"}), ["omp"])

    def test_a_custom_preset_in_pivotal_preset_json_is_read_too(self):
        env = {"PRESET": "custom", "PRESET_JSON": json.dumps(PI_KAGGLE_PRESET)}
        self.assertEqual(install.chain_workers(env), ["agy", "claude", "pi"])

    def test_an_unknown_preset_is_refused(self):
        with self.assertRaises(JobError):
            install.chain_workers({"PRESET": "no-such-preset"})


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.fakes = Fakes(self)

    def pinned(self, workers_map: dict) -> Path:
        path = self.fakes.dir / "pins.json"
        path.write_text(json.dumps({**PINS, "workers": workers_map}), encoding="utf-8")
        return path

    def run_main(self, **extra: str):
        env = self.fakes.env(PRESET_JSON=json.dumps(PI_KAGGLE_PRESET), PRESET="custom", **extra)
        return run_install(self.fakes, env)

    def test_installs_the_preset_workers_and_writes_the_manifest_without_a_path_entry(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        manifest = json.loads((self.fakes.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], workers.SCHEMA)
        self.assertIn("pi", manifest["workers"])
        self.assertIn("setup: pi 1.1.0 installed", out)
        self.assertIn("::warning::setup: agy not installed", out)
        self.assertNotIn("agy", manifest["workers"])
        self.assertEqual(self.fakes.github_path.read_text(encoding="utf-8"), "", "no step's PATH is changed by the setup step")
        entry = manifest["workers"]["pi"]
        self.assertEqual(entry["digest"], workers.digest(Path(entry["root"])))
        self.assertEqual(os.path.realpath(entry["bin"]), entry["bin"])
        self.assertTrue(entry["bin"].startswith(entry["root"] + os.sep))
        self.assertFalse((self.fakes.runner_temp / "pivotal-bin").exists(), "no directory of links is made for PATH")

    def test_a_pi_entry_only_in_the_review_tier_is_installed_on_a_hosted_runner(self):
        # The free preset's review reaches Gemini through pi (pi:google/...), and that entry is the only pi entry when the
        # build is not on pi. Setup must read the review tier too, or the review has no pinned pi to run.
        preset = {"name": "review-only-pi", "tiers": {
            "build": ["claude:anthropic/sonnet"],
            "review": ["agy:google/gemini-3.8-flash-high", "pi:google/gemini-3.8-flash"]}}
        env = self.fakes.env(PRESET_JSON=json.dumps(preset), PRESET=preset["name"])
        self.assertEqual(install.chain_workers(env), ["agy", "claude", "pi"])
        code, out = run_install(self.fakes, env)
        self.assertEqual(code, 0, out)
        self.assertIn("setup: pi 1.1.0 installed", out)
        self.assertIn("::warning::setup: agy not installed", out)
        manifest = json.loads((self.fakes.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertIn("pi", manifest["workers"])
        self.assertNotIn("agy", manifest["workers"])

    def test_the_manifest_lets_the_engine_load_the_install_and_verify_it(self):
        self.run_main()
        pins = workers.load(str(self.fakes.runner_temp / install.MANIFEST))
        self.assertIn("pi", pins)
        self.assertEqual(workers.verify("pi", pins), "")
        self.assertEqual(workers.absent(["pi", "agy"], pins, self.fakes.env()["PATH"]), frozenset({"agy"}))

    def test_npm_is_given_no_secret_and_no_token_even_when_the_job_holds_them(self):
        env = self.fakes.env(PRESET_JSON=json.dumps(PI_KAGGLE_PRESET), PRESET="custom", PIVOTAL_KAGGLE_KEY="kaggle-fake-0123456789",
                             OPENAI_API_KEY="sk-fake-0123456789", GH_TOKEN="ghp_fake_0123456789", ANTHROPIC_API_KEY="sk-ant-fake")
        code, out = run_install(self.fakes, env)
        self.assertEqual(code, 0, out)
        calls = self.fakes.npm_calls()
        self.assertTrue(calls)
        for call in calls:
            with self.subTest(argv=call["argv"]):
                for name in SECRET_NAMES:
                    self.assertNotIn(name, call["env"])
                self.assertIn("--ignore-scripts", call["argv"])

    def test_npm_pins_the_exact_version_and_installs_into_a_prefix_under_runner_temp(self):
        self.run_main()
        specs = [c["argv"][-1] for c in self.fakes.npm_calls()]
        self.assertIn("@earendil-works/pi-coding-agent@1.1.0", specs)
        for call in self.fakes.npm_calls():
            prefix = call["argv"][call["argv"].index("--prefix") + 1]
            self.assertTrue(prefix.startswith(str(self.fakes.runner_temp)), prefix)

    def test_a_cli_that_reports_another_version_is_left_out_with_a_warning_and_the_job_falls_through(self):
        fakes = Fakes(self, wrong_pi=True)
        self.fakes = fakes
        code, out = run_install(fakes, fakes.env(PRESET_JSON=json.dumps(PI_KAGGLE_PRESET), PRESET="custom"))
        self.assertEqual(code, 0)
        self.assertIn("::warning::setup: pi not installed: the installed CLI does not report the pinned version 1.1.0", out)
        manifest = json.loads((fakes.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("pi", manifest["workers"])

    def test_a_node_older_than_pi_declares_leaves_pi_out_with_the_reason(self):
        fakes = Fakes(self, node="v22.18.0")
        self.fakes = fakes
        code, out = run_install(fakes, fakes.env(PRESET_JSON=json.dumps(PI_KAGGLE_PRESET), PRESET="custom"))
        self.assertEqual(code, 0)
        self.assertIn("pi not installed: node 22.18.0 is older than the pinned minimum 22.19.0", out)
        self.assertFalse(any("@earendil-works" in c["argv"][-1] for c in fakes.npm_calls()), "no npm call for a worker it cannot run")

    def test_an_npm_failure_is_a_warning_not_a_run_of_a_missing_binary(self):
        fakes = Fakes(self, npm_fails=True)
        self.fakes = fakes
        code, out = run_install(fakes, fakes.env(PRESET_JSON=json.dumps(PI_KAGGLE_PRESET), PRESET="custom"))
        self.assertEqual(code, 0)
        self.assertIn("::warning::setup: pi not installed: npm install failed", out)
        manifest = json.loads((fakes.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(manifest["workers"], {})
        self.assertFalse((fakes.runner_temp / "pivotal-bin").exists(), "no directory of links when nothing installed")
        self.assertEqual(fakes.github_path.read_text(encoding="utf-8"), "")

    def test_a_worker_named_by_a_tier_but_not_pinned_refuses_the_install(self):
        pins = self.pinned({"claude": PINS["workers"]["claude"]})
        code, out = run_install(self.fakes, self.fakes.env(PRESET_JSON=json.dumps(PI_KAGGLE_PRESET), PRESET="custom"), pins)
        self.assertEqual(code, 1)
        self.assertIn("no pin in engine/setup/pins.json for pi", out)
        self.assertEqual(self.fakes.npm_calls(), [], "nothing is installed when the install is refused")

    def test_no_runner_temp_refuses_the_install(self):
        env = self.fakes.env(PRESET_JSON=json.dumps(PI_KAGGLE_PRESET), PRESET="custom")
        del env["RUNNER_TEMP"]
        code, _ = run_install(self.fakes, env)
        self.assertEqual(code, 1)

    def test_a_pins_file_of_the_wrong_shape_refuses_the_install(self):
        path = self.fakes.dir / "bad.json"
        path.write_text(json.dumps({"schema": 2, "workers": {}, "unavailable": {}}), encoding="utf-8")
        with self.assertRaises(JobError):
            install.read_pins(path)


def write_pins(fakes: Fakes, workers_map: dict) -> Path:
    path = fakes.dir / "pins.json"
    path.write_text(json.dumps({**PINS, "workers": workers_map}), encoding="utf-8")
    return path


class HardeningTest(unittest.TestCase):
    """install.py: a prefix never keeps an earlier job's files, a pinned binary stays inside its install, a line that does not
    resolve is a refusal, and the setup budget the jobs carry is the worst case install.py can take (F3, F5, F8)."""

    def setUp(self):
        self.fakes = Fakes(self)

    def env(self, **extra: str) -> dict[str, str]:
        return self.fakes.env(PRESET_JSON=json.dumps(PI_KAGGLE_PRESET), PRESET="custom", **extra)

    def test_a_leftover_in_the_prefix_is_removed_before_npm_and_never_reaches_the_digest(self):
        leftover = self.fakes.runner_temp / "pi" / "node_modules" / "planted.js"
        leftover.parent.mkdir(parents=True)
        leftover.write_text("exfiltrate\n", encoding="utf-8")
        code, out = run_install(self.fakes, self.env())
        self.assertEqual(code, 0, out)
        self.assertFalse(leftover.exists(), "the file an earlier job left is gone before npm runs")
        manifest = json.loads((self.fakes.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(manifest["workers"]["pi"]["digest"], workers.digest(self.fakes.runner_temp / "pi"))

    def test_a_pinned_binary_that_escapes_its_install_is_not_installed(self):
        outside = self.fakes.runner_temp / "outside" / "pi"
        outside.parent.mkdir()
        outside.write_text("#!" + sys.executable + "\nprint('1.1.0')\n", encoding="utf-8")
        outside.chmod(0o755)
        escaping = {**PINS["workers"]["pi"], "bin": "../outside/pi"}
        pins = write_pins(self.fakes, {"claude": PINS["workers"]["claude"], "pi": escaping})
        code, out = run_install(self.fakes, self.env(), pins)
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::setup: pi not installed: the pinned binary is not in the install", out)
        manifest = json.loads((self.fakes.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("pi", manifest["workers"])

    def test_a_line_that_does_not_resolve_is_a_refusal_with_exit_one_and_no_traceback(self):
        done = subprocess.run([sys.executable, "-I", str(SETUP / "install.py")], env=self.fakes.env(LINE="no-such-line"),
                              capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertTrue(done.stderr.startswith("refused: "), done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_the_job_setup_budget_is_the_worst_case_of_the_pinned_workers(self):
        self.assertEqual(buildjob.SETUP_MINUTES, math.ceil(install.setup_seconds() / 60))


class AgentPathTest(unittest.TestCase):
    """What PATH the agent gets after the setup step (fix round 2, C1). The setup step writes no PATH entry and creates no
    pivotal-bin, so the directory it used to create is on no PATH, and a name the agent plants there is never run. This test
    covers that directory only: a name planted in another writable directory on the job's PATH is not covered (a shell that
    looks it up can run it; filed as a discovery). A pinned CLI runs by its absolute path, and the shell of the next entry
    resolves names (and pi's `#!/usr/bin/env node`) through the PATH it is given."""

    def setUp(self):
        self.fakes = Fakes(self)

    def test_the_agents_path_holds_no_pivotal_bin_and_a_name_planted_there_is_not_run(self):
        env = self.fakes.env(PRESET_JSON=json.dumps(PI_KAGGLE_PRESET), PRESET="custom")
        code, out = run_install(self.fakes, env)
        self.assertEqual(code, 0, out)
        # The runner gives every later step the GITHUB_PATH lines first, then the PATH the job had.
        added = [line for line in self.fakes.github_path.read_text(encoding="utf-8").splitlines() if line]
        job_env = {"PATH": os.pathsep.join([*added, env["PATH"]]), "RUNNER_TEMP": str(self.fakes.runner_temp)}
        agent_path = buildjob.shell_env(job_env)["PATH"]
        planted = self.fakes.runner_temp / "pivotal-bin"  # the agent may create it: the test plays the agent
        planted.mkdir(exist_ok=True)
        marker = self.fakes.dir / "planted-ran"
        for name in ("node", "pivotal-planted-name"):
            (planted / name).write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            (planted / name).chmod(0o755)
        self.assertNotIn(str(planted), agent_path.split(os.pathsep), "the agent's PATH has no pivotal-bin in it")
        found = shutil.which("node", path=agent_path)
        self.assertFalse(found and found.startswith(str(planted)), f"a planted node is what `node` finds: {found}")
        subprocess.run(["/bin/sh", "-c", "pivotal-planted-name"], env={"PATH": agent_path}, capture_output=True, check=False,
                       stdin=subprocess.DEVNULL, timeout=60)
        self.assertFalse(marker.exists(), "the next entry's shell ran the name the agent planted")


class AvailabilityTest(unittest.TestCase):
    def test_a_worker_pinned_for_another_platform_is_unavailable_here_with_the_reason(self):
        self.assertEqual(PINS["workers"]["claude"]["platform"], "linux-x64")
        other = {**PINS["workers"]["claude"], "platform": "plan9-mips"}
        self.assertIn("pinned for plan9-mips", install.reason_unavailable("claude", other, os.defpath))


class DirectDownloadTest(unittest.TestCase):
    """Tests for the direct-download worker install (omp): sha256 verification, version check, download cap."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="setup-direct-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.runner_temp = self.dir / "runner-temp"
        self.runner_temp.mkdir()
        self.github_path = self.dir / "github-path"
        self.github_path.write_text("", encoding="utf-8")
        self.bin = self.dir / "fake-bin"
        self.bin.mkdir()
        self.home = self.dir / "home"
        self.home.mkdir()
        # A tiny fake binary that reports a version in the REAL omp format
        self.fake_omp = self.bin / "omp"
        self.fake_omp.write_text("#!" + sys.executable + "\nimport sys\nprint('omp/18.20.0') if sys.argv[1:] == ['--version'] else None\n", encoding="utf-8")
        self.fake_omp.chmod(0o755)
        # Minimal preset that only names omp in review tier
        self.preset = {"name": "omp-only", "tiers": {"build": ["claude:anthropic/sonnet"], "review": ["omp:google/gemini"]}}
        self.env = {"PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin", "RUNNER_TEMP": str(self.runner_temp),
                     "GITHUB_PATH": str(self.github_path), "HOME": str(self.home), "LANG": "C.UTF-8",
                     "PRESET": "omp-only", "PRESET_JSON": json.dumps(self.preset)}

    def _pins(self, omp_pin: dict) -> Path:
        path = self.dir / "pins.json"
        claude_pin = {"npm": "@anthropic-ai/claude-code-linux-x64", "version": "2.1.312", "bin": "node_modules/@anthropic-ai/claude-code-linux-x64/claude", "platform": "linux-x64"}
        path.write_text(json.dumps({"schema": 1, "workers": {"omp": omp_pin, "claude": claude_pin}, "unavailable": {}}), encoding="utf-8")
        return path

    def _run(self, pins_path: Path) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            try:
                code = install.main(self.env, pins_path)
            except JobError as err:
                print(f"refused: {err}")
                code = 1
        return code, out.getvalue()

    def test_sha256_mismatch_is_refused_with_warning_and_not_in_manifest(self):
        """A download whose sha256 differs from the pin is refused with ::warning:: and left out of the manifest."""
        # The fake binary content has a known sha256; we pin a different one.
        content = self.fake_omp.read_bytes()
        wrong_sha = hashlib.sha256(b"different content").hexdigest()
        pin = {"url": "file://" + str(self.fake_omp), "version": "18.20.0", "bin": "omp", "platform": "linux-x64",
                "sha256": wrong_sha}
        pins_path = self._pins(pin)
        code, out = self._run(pins_path)
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::setup: omp not installed: sha256 mismatch", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("omp", manifest["workers"], "omp must not be in manifest when sha256 mismatches")

    def test_download_failure_is_not_installed_never_runs_missing_binary(self):
        """A download failure (URLError, timeout) results in 'not installed', never a run of a missing binary."""
        pin = {"url": "http://127.0.0.1:9999/does-not-exist", "version": "18.20.0", "bin": "omp",
                "platform": "linux-x64", "sha256": "a" * 64}
        pins_path = self._pins(pin)
        code, out = self._run(pins_path)
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::setup: omp not installed: download failed", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("omp", manifest["workers"], "omp must not be in manifest when download fails")
        # claude is installed successfully, omp is not
        self.assertIn("claude", manifest["workers"])

    def test_good_download_installs_binary_executable_with_correct_version(self):
        """A good download installs: the binary is executable, lives under the install root, --version matches the pin."""
        content = self.fake_omp.read_bytes()
        correct_sha = hashlib.sha256(content).hexdigest()
        pin = {"url": "file://" + str(self.fake_omp), "version": "18.20.0", "bin": "omp",
                "platform": "linux-x64", "sha256": correct_sha}
        pins_path = self._pins(pin)
        code, out = self._run(pins_path)
        self.assertEqual(code, 0, out)
        self.assertIn("setup: omp 18.20.0 installed", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertIn("omp", manifest["workers"])
        entry = manifest["workers"]["omp"]
        # Check manifest shape matches npm workers: root, bin, digest
        self.assertEqual(set(entry.keys()), {"root", "bin", "digest"})
        self.assertTrue(entry["bin"].startswith(entry["root"] + os.sep))
        self.assertEqual(entry["digest"], workers.digest(Path(entry["root"])))
        # The binary is executable and at the right place
        bin_path = Path(entry["bin"])
        self.assertTrue(bin_path.is_file())
        self.assertTrue(os.access(bin_path, os.X_OK))
        # The binary reports the pinned version in the REAL omp format
        probe = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True, check=False,
                               timeout=10, stdin=subprocess.DEVNULL)
        self.assertEqual(probe.returncode, 0)
        self.assertEqual(probe.stdout.strip(), "omp/18.20.0")

    def test_wrong_version_is_refused(self):
        """A binary that reports a different version than the pin is refused."""
        # A fake binary that reports version 9.9.9 instead of 18.20.0
        wrong_binary = self.bin / "omp-wrong"
        wrong_binary.write_text("#!" + sys.executable + "\nimport sys\nprint('9.9.9') if sys.argv[1:] == ['--version'] else None\n", encoding="utf-8")
        wrong_binary.chmod(0o755)
        content = wrong_binary.read_bytes()
        correct_sha = hashlib.sha256(content).hexdigest()
        pin = {"url": "file://" + str(wrong_binary), "version": "18.20.0", "bin": "omp",
                "platform": "linux-x64", "sha256": correct_sha}
        pins_path = self._pins(pin)
        code, out = self._run(pins_path)
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::setup: omp not installed: the installed CLI does not report the pinned version 18.20.0", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("omp", manifest["workers"])

    def test_download_over_cap_is_refused_before_any_write(self):
        """A stream over MAX_DOWNLOAD_BYTES is refused before any write; the manifest has no omp entry."""
        # Create a fake binary larger than the cap by patching MAX_DOWNLOAD_BYTES to a small value
        large_content = b"x" * (100 * 1024)  # 100 KiB
        large_binary = self.bin / "omp-large"
        large_binary.write_bytes(large_content)
        large_binary.chmod(0o755)
        correct_sha = hashlib.sha256(large_content).hexdigest()
        pin = {"url": "file://" + str(large_binary), "version": "18.20.0", "bin": "omp",
                "platform": "linux-x64", "sha256": correct_sha}
        pins_path = self._pins(pin)
        # Patch the cap to 50 KiB for this test
        original_cap = install.MAX_DOWNLOAD_BYTES
        try:
            install.MAX_DOWNLOAD_BYTES = 50 * 1024
            code, out = self._run(pins_path)
        finally:
            install.MAX_DOWNLOAD_BYTES = original_cap
        self.assertEqual(code, 0, out)
        self.assertIn(f"::warning::setup: omp not installed: download exceeds {50 * 1024} bytes", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("omp", manifest["workers"])
        # The binary file should not have been written
        omp_dir = self.runner_temp / "omp"
        self.assertFalse((omp_dir / "omp").exists(), "no binary written when download exceeds cap")

    def test_omp_version_format_name_slash_version_installs(self):
        """A binary that reports 'omp/18.20.0' (the REAL omp format) installs successfully."""
        # This is already tested by test_good_download_installs_binary_executable_with_correct_version
        # since the fake_omp now prints 'omp/18.20.0', but we add this explicit test for clarity.
        content = self.fake_omp.read_bytes()
        correct_sha = hashlib.sha256(content).hexdigest()
        pin = {"url": "file://" + str(self.fake_omp), "version": "18.20.0", "bin": "omp",
                "platform": "linux-x64", "sha256": correct_sha}
        pins_path = self._pins(pin)
        code, out = self._run(pins_path)
        self.assertEqual(code, 0, out)
        self.assertIn("setup: omp 18.20.0 installed", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertIn("omp", manifest["workers"])

    def test_wrong_version_omp_slash_18_20_1_is_refused(self):
        """A binary that reports 'omp/18.20.1' (wrong version) is refused."""
        wrong_binary = self.bin / "omp-wrong"
        wrong_binary.write_text("#!" + sys.executable + "\nimport sys\nprint('omp/18.20.1') if sys.argv[1:] == ['--version'] else None\n", encoding="utf-8")
        wrong_binary.chmod(0o755)
        content = wrong_binary.read_bytes()
        correct_sha = hashlib.sha256(content).hexdigest()
        pin = {"url": "file://" + str(wrong_binary), "version": "18.20.0", "bin": "omp",
                "platform": "linux-x64", "sha256": correct_sha}
        pins_path = self._pins(pin)
        code, out = self._run(pins_path)
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::setup: omp not installed: the installed CLI does not report the pinned version 18.20.0", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("omp", manifest["workers"])

    def test_wrong_version_bare_18_20_1_is_refused(self):
        """A binary that reports '18.20.1' (bare wrong version) is refused."""
        wrong_binary = self.bin / "omp-wrong"
        wrong_binary.write_text("#!" + sys.executable + "\nimport sys\nprint('18.20.1') if sys.argv[1:] == ['--version'] else None\n", encoding="utf-8")
        wrong_binary.chmod(0o755)
        content = wrong_binary.read_bytes()
        correct_sha = hashlib.sha256(content).hexdigest()
        pin = {"url": "file://" + str(wrong_binary), "version": "18.20.0", "bin": "omp",
                "platform": "linux-x64", "sha256": correct_sha}
        pins_path = self._pins(pin)
        code, out = self._run(pins_path)
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::setup: omp not installed: the installed CLI does not report the pinned version 18.20.0", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("omp", manifest["workers"])

    def test_space_format_omp_space_18_20_0_is_refused(self):
        """A binary that reports 'omp 18.20.0' (space instead of slash) is refused."""
        wrong_binary = self.bin / "omp-wrong"
        wrong_binary.write_text("#!" + sys.executable + "\nimport sys\nprint('omp 18.20.0') if sys.argv[1:] == ['--version'] else None\n", encoding="utf-8")
        wrong_binary.chmod(0o755)
        content = wrong_binary.read_bytes()
        correct_sha = hashlib.sha256(content).hexdigest()
        pin = {"url": "file://" + str(wrong_binary), "version": "18.20.0", "bin": "omp",
                "platform": "linux-x64", "sha256": correct_sha}
        pins_path = self._pins(pin)
        code, out = self._run(pins_path)
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::setup: omp not installed: the installed CLI does not report the pinned version 18.20.0", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("omp", manifest["workers"])

    def test_empty_version_output_is_refused(self):
        """A binary that reports empty output is refused."""
        wrong_binary = self.bin / "omp-wrong"
        wrong_binary.write_text("#!" + sys.executable + "\nimport sys\nprint('') if sys.argv[1:] == ['--version'] else None\n", encoding="utf-8")
        wrong_binary.chmod(0o755)
        content = wrong_binary.read_bytes()
        correct_sha = hashlib.sha256(content).hexdigest()
        pin = {"url": "file://" + str(wrong_binary), "version": "18.20.0", "bin": "omp",
                "platform": "linux-x64", "sha256": correct_sha}
        pins_path = self._pins(pin)
        code, out = self._run(pins_path)
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::setup: omp not installed: the installed CLI does not report the pinned version 18.20.0", out)
        manifest = json.loads((self.runner_temp / install.MANIFEST).read_text(encoding="utf-8"))
        self.assertNotIn("omp", manifest["workers"])

    def test_mutant_whole_word_comparison_fails(self):
        """Red test: a mutant that compares the whole first word to the pin would accept 'omp/18.20.0' as equal to '18.20.0'.
        Our parse_version correctly extracts '18.20.0' from 'omp/18.20.0', so this test ensures we don't regress to
        comparing the whole word."""
        from engine.setup.install import parse_version
        # The mutant would do: words[0] == pin["version"] i.e. 'omp/18.20.0' == '18.20.0' -> False
        # Our code does: parse_version(words[0]) == pin["version"] i.e. '18.20.0' == '18.20.0' -> True
        self.assertEqual(parse_version("omp/18.20.0"), "18.20.0")
        self.assertNotEqual("omp/18.20.0", "18.20.0", "mutant whole-word comparison would fail here")
        # Also verify that a bare version still works
        self.assertEqual(parse_version("18.20.0"), "18.20.0")
        # And that garbage is rejected
        self.assertIsNone(parse_version("omp 18.20.0"))
        self.assertIsNone(parse_version(""))
        self.assertIsNone(parse_version("not-a-version"))


if __name__ == "__main__":
    unittest.main()
