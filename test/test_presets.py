"""Shipped presets and `piv preset check` (docs/engines.md).

The probe tests run fake agent CLIs (test/fakecli.py) as real subprocesses. Each fake logs its argv and its
environment, so a test can see what the probe sent and what the child received. Nothing here calls a provider.
"""

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fakecli

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))
sys.path.insert(0, str(ROOT))

from piv import cli, presetcheck  # noqa: E402
from engine import route  # noqa: E402

SHIPPED = ("free-kaggle", "claude-sub", "one-key")
# One distinct value per secret name, so a test can tell which secret a child received.
ALL_SECRETS = {name: f"value-of-{name}" for names in route.DOOR_SECRETS.values() for name in names}
CATALOG = ROOT / "kaggle" / "catalog.json"


def catalog_ids(catalog: dict, statuses=("partial", "measured")) -> dict[str, str]:
    """sysop_model_id -> status, for the single-model candidates whose status is one of ``statuses``."""
    return {c["sysop_model_id"]: c["status"] for c in catalog.get("single_model_both_t4", [])
            if isinstance(c, dict) and c.get("status") in statuses}


def poll(read, seconds: float):
    """The first truthy value of ``read()``, polled every 0.1s for up to ``seconds``; else its last (falsy) value."""
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(0.1)


def pid_in(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def alive(pid: int) -> bool:
    """True while ``pid`` runs. A zombie (killed, not yet reaped) is not alive, although os.kill(pid, 0) still succeeds for it."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def read_lines(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class PresetFilesTest(unittest.TestCase):
    """The shipped files: the validator, the catalog and cross-vendor (static, no probe)."""

    def test_every_shipped_preset_parses_passes_cross_vendor_and_names_known_ids(self):
        # Every id left in a shipped preset is known (operator decision 2026-11-04/05): kaggle model in catalog.json,
        # Claude CLI aliases haiku/sonnet/opus (fleet), omp and pi OpenRouter/Groq/Google model ids (measured through omp
        # and pi in docs/engines.md). Sources cited in summaries/build report.
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        measured_kaggle = catalog_ids(catalog)
        paths = sorted((ROOT / "presets").glob("*.json"))
        self.assertEqual(sorted(p.stem for p in paths), sorted(SHIPPED))
        known_workers = {"pi", "claude", "omp"}  # only workers with T0-measured flags ship ids; agy is not supported
        for path in paths:
            with self.subTest(preset=path.stem):
                preset = route.load_preset(path)
                self.assertEqual(route.cross_vendor_problems(preset), [])
                for tier in preset.tiers.values():
                    for entry in tier:
                        self.assertIn(entry.worker, known_workers, f"{entry.spec} uses an unmeasured worker")
                        if entry.door == "kaggle":
                            self.assertIn(entry.model, measured_kaggle,
                                          f"{entry.spec} names a model the catalog does not measure")
                            self.assertEqual(entry.model, preset.kaggle_model)

    def test_free_kaggle_reviews_through_omp_groq_then_google_then_openrouter(self):
        # Open models first: the free preset's review is omp (the default agent) only. The first entry is a Groq model
        # (openai vendor, GROQ_API_KEY), then Gemini (google, GEMINI_API_KEY), then Gemini through OpenRouter
        # (OPENROUTER_API_KEY). No agy and no claude: agy is not supported and Claude is optional and paid.
        preset = route.load_preset(ROOT / "presets" / "free-kaggle.json")
        self.assertEqual([e.spec for e in preset.tiers["review"]],
                         ["omp:groq/openai/gpt-oss-120b", "omp:google/gemini-3.6-flash", "omp:google/gemini-3-flash-preview",
                          "omp:openrouter/google/gemini-3-flash-preview"])
        self.assertEqual([e.spec for e in preset.tiers["build"]], ["pi:kaggle/qwen3.6-35b-a3b"])

    def test_omp_is_the_default_worker_of_the_free_reviewers_and_one_key(self):
        free = route.load_preset(ROOT / "presets" / "free-kaggle.json")
        one_key = route.load_preset(ROOT / "presets" / "one-key.json")
        self.assertEqual({e.worker for e in free.tiers["review"]}, {"omp"})
        self.assertEqual({e.worker for e in one_key.tiers["build"] + one_key.tiers["review"]}, {"omp"})

    def test_claude_is_optional_paid_and_never_the_init_default(self):
        from piv import init  # noqa: PLC0415
        claude_sub = json.loads((ROOT / "presets" / "claude-sub.json").read_text(encoding="utf-8"))
        self.assertIn("optional, uses your Claude plan", claude_sub["summary"])
        self.assertTrue(all(e.startswith("omp:") for e in claude_sub["tiers"]["review"]))
        self.assertEqual(init.DEFAULT_PRESET, "one-key")
        for name in ("free-kaggle", "one-key"):
            with self.subTest(preset=name):
                preset = route.load_preset(ROOT / "presets" / f"{name}.json")
                self.assertNotIn("claude", {e.worker for tier in preset.tiers.values() for e in tier})

    def test_measured_field_names_tier_entries_only(self):
        base = {"name": "x", "tiers": {"build": ["pi:google/gemini-3.8-flash"], "review": ["agy:google/gemini-3.8-flash-high"]}}
        self.assertEqual(route.parse_preset({**base, "measured": []}).measured, frozenset())
        with self.assertRaises(ValueError):
            route.parse_preset({**base, "measured": ["pi:groq/openai/gpt-oss-120b"]})
        with self.assertRaises(ValueError):
            route.parse_preset({**base, "measured": "pi:google/gemini-3.8-flash"})

    def test_kaggle_entries_have_kaggle_model_in_every_preset_that_uses_them(self):
        for path in sorted((ROOT / "presets").glob("*.json")):
            with self.subTest(preset=path.stem):
                preset = route.load_preset(path)
                uses_kaggle = any(e.door == "kaggle" for tier in preset.tiers.values() for e in tier)
                self.assertEqual(uses_kaggle, preset.kaggle_model is not None)

    def test_kaggle_entry_without_kaggle_key_is_a_static_failure(self):
        data = {"name": "x", "tiers": {"build": ["pi:kaggle/qwen3.6-35b-a3b"], "review": ["agy:google/gemini-3.8-flash-high"]}}
        route_mod, render = presetcheck.load_engine(ROOT)
        problems = presetcheck.static_problems(ROOT, route_mod.parse_preset(data), route_mod, render)
        self.assertEqual(problems, ["pi:kaggle/qwen3.6-35b-a3b: a kaggle entry needs the preset's kaggle: "
                                    '{"model": "<catalog model id>"}'])

    def test_kaggle_model_validated_even_without_a_kaggle_door_entry(self):
        # S5: validate kaggle.model whenever it is set.
        data = {"name": "x", "kaggle": {"model": "ghost-model"},
                "tiers": {"build": ["agy:google/gemini-3.8-flash-high"], "review": ["claude:anthropic/sonnet"]}}
        route_mod, render = presetcheck.load_engine(ROOT)
        problems = presetcheck.static_problems(ROOT, route_mod.parse_preset(data), route_mod, render)
        self.assertTrue(any("ghost-model" in p and "not in the catalog" in p for p in problems), problems)


class PresetCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin = fakecli.FakeBin(self.bin_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def fake(self, worker, **kwargs):
        """A fake agent CLI that logs its argv and environment to <worker>.log / <worker>.env."""
        kwargs.setdefault("stdout", "ok\n")
        return self.bin.add(worker, log=self.root / f"{worker}.log", env_log=self.root / f"{worker}.env", **kwargs)

    def calls(self, worker) -> list:
        return read_lines(self.root / f"{worker}.log")

    def child_envs(self, worker) -> list:
        return [entry["env"] for entry in read_lines(self.root / f"{worker}.env")]

    def env(self, secrets=ALL_SECRETS, extra=None):
        base = dict(secrets)
        base.update(extra or {})
        return self.bin.env(base)

    def check(self, names, env, root=ROOT, timeout=5.0):
        out, err = io.StringIO(), io.StringIO()
        rc = presetcheck.check_names(root, names, env, out, err, timeout=timeout)
        return rc, out.getvalue(), err.getvalue()

    def copy_root(self) -> Path:
        dest = self.root / "repo"
        for sub in ("engine", "gh-piv", "kaggle", "presets"):
            shutil.copytree(ROOT / sub, dest / sub, ignore=shutil.ignore_patterns("__pycache__"))
        return dest

    # --- the probe: a real call per entry, same argv the factory runs (I1) --------------------------------------

    def test_every_shipped_preset_with_its_secrets_passes_check(self):
        for worker in ("pi", "claude", "omp"):
            self.fake(worker)
        for name in SHIPPED:
            with self.subTest(preset=name):
                rc, out, err = self.check([name], self.env())
                self.assertEqual(rc, 0, err)
                self.assertIn(f"ok: preset {name} passed check", out)

    def test_probe_argv_equals_worker_argv_for_every_entry_of_every_shipped_preset(self):
        # I1: the probe must run the same command shape the factory runs. route.worker_argv/agent_argv share it.
        for worker in ("pi", "claude", "omp"):
            self.fake(worker)
        route_mod, _ = presetcheck.load_engine(ROOT)
        from engine.buildjob import INSTRUCTION, agent_argv  # noqa: PLC0415
        for name in SHIPPED:
            with self.subTest(preset=name):
                rc, out, err = self.check([name], self.env())
                self.assertEqual(rc, 0, err)
                preset = route_mod.load_preset(ROOT / "presets" / f"{name}.json")
                for entry in presetcheck.unique_entries(preset):
                    expected = route_mod.worker_argv(entry, "ok")
                    self.assertIn(expected[1:], self.calls(entry.worker),
                                  f"{entry.spec}: probe ran wrong argv")
                    # Same builder as the factory: agent_argv differs only in the prompt (instruction vs "ok").
                    build = agent_argv(entry, Path("/tmp/prompt.md"))
                    self.assertEqual(build[0], expected[0])
                    self.assertEqual(build[1:-1], expected[1:-1])
                    self.assertEqual(build[-1], INSTRUCTION.format(path=Path("/tmp/prompt.md")))

    def test_probe_argv_equals_review_argv_for_every_entry_of_every_shipped_preset(self):
        # Round 5: the review job builds its agent command through route.worker_argv too, so the probe runs what the
        # review job runs as well as what the build job runs. Only the prompt differs (the probe sends "ok").
        # review-output-shape: the review job runs pi/claude in JSON mode for verdict extraction.
        # For pi/omp: argv is [worker, -p, --mode, json, --model, MODEL, --no-session, --no-tools, PROMPT_TEXT]
        # For claude: argv is [worker, -p, --output-format, json, --model, MODEL, INSTRUCTION]
        # For other workers: same as probe argv.
        for worker in ("pi", "claude", "omp"):
            self.fake(worker)
        route_mod, _ = presetcheck.load_engine(ROOT)
        from engine.reviewjob import INSTRUCTION as REVIEW_INSTRUCTION, agent_argv as review_agent_argv  # noqa: PLC0415
        for name in SHIPPED:
            with self.subTest(preset=name):
                rc, out, err = self.check([name], self.env())
                self.assertEqual(rc, 0, err)
                preset = route_mod.load_preset(ROOT / "presets" / f"{name}.json")
                for entry in presetcheck.unique_entries(preset):
                    probe = route_mod.worker_argv(entry, "ok")
                    self.assertIn(probe[1:], self.calls(entry.worker), f"{entry.spec}: probe ran wrong argv")
                    # For pi/omp, pass prompt_text="ok" to match the probe's prompt
                    review = review_agent_argv(entry, Path("/tmp/prompt.md"), prompt_text="ok")
                    expected = list(probe[:-1])  # probe argv without the "ok" prompt
                    if entry.worker == "pi":
                        # pi: insert --mode json after -p, then add --no-session --no-tools
                        expected[expected.index("-p") + 1:expected.index("-p") + 1] = ["--mode", "json"]
                        expected.extend(["--no-session", "--no-tools"])
                    elif entry.worker == "omp":
                        # omp: same as pi
                        expected[expected.index("-p") + 1:expected.index("-p") + 1] = ["--mode", "json"]
                        expected.extend(["--no-session", "--no-tools"])
                    elif entry.worker == "claude":
                        expected[expected.index("-p") + 1:expected.index("-p") + 1] = ["--output-format", "json"]
                    # The last element of review is the prompt text ("ok") for pi/omp; for other workers it's the INSTRUCTION sentence
                    self.assertEqual(review[:-1], expected, f"{entry.spec}: review job argv is not the probe's argv")
                    if entry.worker in ("pi", "omp"):
                        self.assertEqual(review[-1], "ok")
                    else:
                        self.assertEqual(review[-1], REVIEW_INSTRUCTION.format(path=Path("/tmp/prompt.md")))

    def test_probe_sends_measured_flags_model_and_one_word_prompt(self):
        self.fake("pi")
        self.fake("omp")
        self.fake("claude")
        rc, out, err = self.check(["free-kaggle"], self.env())
        self.assertEqual(rc, 0, err)
        # The probe runs each entry with worker_argv(entry, "ok"): [worker, -p, --model, MODEL, "ok"]
        # The fake agent logs argv[1:] = [-p, --model, MODEL, "ok"]
        self.assertEqual({tuple(c) for c in self.calls("pi")}, {
            ("-p", "--model", "kaggle/qwen3.6-35b-a3b", "ok"),  # the build: pi through the Kaggle endpoint
        })
        self.assertEqual({tuple(c) for c in self.calls("omp")}, {
            ("-p", "--model", "groq/openai/gpt-oss-120b", "ok"),  # the review's Groq model through omp
            ("-p", "--model", "google/gemini-3.6-flash", "ok"),  # the review's first Gemini through omp
            ("-p", "--model", "google/gemini-3-flash-preview", "ok"),  # the review's second Gemini through omp
            ("-p", "--model", "openrouter/google/gemini-3-flash-preview", "ok"),  # the review's Gemini through OpenRouter
        })
        self.assertEqual(self.calls("claude"), [], "free-kaggle names no Claude entry, so claude is never probed")

    def test_claude_entries_probe_with_the_model_id(self):
        self.fake("claude")
        self.fake("omp")
        self.fake("pi")
        rc, out, err = self.check(["claude-sub"], self.env())
        self.assertEqual(rc, 0, err)
        self.assertEqual({tuple(c) for c in self.calls("claude")}, {("-p", "--model", "sonnet", "ok"),
                                                                    ("-p", "--model", "opus", "ok")})

    def test_401_is_a_failure_with_its_exit_code(self):
        self.fake("pi", stdout="", stderr="401 Unauthorized: invalid api key\n", code=1)
        self.fake("omp")
        self.fake("claude")
        rc, out, err = self.check(["free-kaggle"], self.env())
        self.assertEqual(rc, 1)
        self.assertNotIn("ok: preset", out)
        self.assertIn("failed: pi:kaggle/qwen3.6-35b-a3b: exit 1: 401 Unauthorized", err)

    def test_exit_zero_with_no_output_is_a_failure(self):
        # I2: an exit-0 empty/error body is not "authenticated" (docs/engines.md: failure text unmeasured).
        self.fake("pi", stdout="")
        self.fake("omp")
        self.fake("claude")
        rc, out, err = self.check(["free-kaggle"], self.env())
        self.assertEqual(rc, 1)
        self.assertIn("failed: pi:kaggle/qwen3.6-35b-a3b: no output (exit 0;", err)

    def test_ok_line_says_exit_zero_with_output_not_authenticated(self):
        # I2: ok means exit 0 with output; auth not verified without T0 failure text.
        self.fake("pi")
        self.fake("omp")
        self.fake("claude")
        rc, out, err = self.check(["free-kaggle"], self.env())
        self.assertEqual(rc, 0, err)
        self.assertIn("answered (exit 0 with output; auth not verified without T0 failure text)", out)

    def test_missing_binary_is_a_failure(self):
        empty = self.root / "empty"
        empty.mkdir()
        env = dict(ALL_SECRETS, PATH=str(empty))
        rc, out, err = self.check(["claude-sub"], env)
        self.assertEqual(rc, 1)
        self.assertIn("failed: claude:anthropic/sonnet: binary not found: claude", err)

    def test_timeout_is_a_failure(self):
        self.fake("pi", sleep=3)
        self.fake("omp", sleep=3)
        self.fake("claude", sleep=3)
        rc, out, err = self.check(["free-kaggle"], self.env(), timeout=0.5)
        self.assertEqual(rc, 1)
        self.assertIn("timed out after 0.5s", err)

    def test_timeout_kills_the_process_group(self):
        # S2: a fake that spawns a child; after timeout the grandchild must not survive. The fake writes the grandchild's pid
        # as soon as it has spawned it, and the timeout is long enough for it to do so even under load (otherwise nothing is tested). The
        # grandchild is then polled for up to 10s, because a SIGKILLed process can take a moment to be reaped.
        marker = self.root / "grandchild.pid"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        (self.bin_dir / "pi").write_text(
            "#!/bin/sh\n"
            f"/usr/bin/sleep 30 & echo $! > {marker}\n"
            "/usr/bin/sleep 30\n", encoding="utf-8")
        (self.bin_dir / "pi").chmod(0o755)
        self.fake("agy")  # the review entry runs after the build times out
        repo = self.copy_root()
        (repo / "presets" / "one.json").write_text(json.dumps({
            "name": "one", "kaggle": {"model": "qwen3.6-35b-a3b"},
            "tiers": {"build": ["pi:kaggle/qwen3.6-35b-a3b"], "review": ["agy:google/gemini-3.8-flash-high"]},
        }), encoding="utf-8")
        rc, out, err = self.check(["one"], self.env(), root=repo, timeout=2.0)
        self.assertEqual(rc, 1)
        self.assertIn("timed out", err)
        pid = poll(lambda: pid_in(marker), 10)
        self.assertTrue(pid, "the fake never started its grandchild, so the kill was not tested")
        gone = poll(lambda: not alive(pid), 10)
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertTrue(gone, f"grandchild {pid} survived the timeout for 10s")

    def test_missing_secret_is_a_failure_and_nothing_runs(self):
        self.fake("pi")
        self.fake("omp")
        self.fake("claude")
        rc, out, err = self.check(["free-kaggle"], self.bin.env({}))
        self.assertEqual(rc, 1)
        self.assertIn("failed: pi:kaggle/qwen3.6-35b-a3b: missing secret (set PIVOTAL_KAGGLE_KEY)", err)
        self.assertNotIn("ok: preset", out)
        self.assertEqual(self.calls("pi"), [])

    def test_child_gets_only_its_own_secret_plus_path_and_home(self):
        self.fake("claude")
        self.fake("omp")
        self.fake("pi")
        env = {"PATH": self.env()["PATH"], "HOME": "/home/probe-test"}
        env.update(ALL_SECRETS)
        rc, out, err = self.check(["claude-sub"], env)
        self.assertEqual(rc, 0, err)
        for child in self.child_envs("claude"):
            child = {k: v for k, v in child.items() if k != "LC_CTYPE"}
            self.assertEqual(child, {"PATH": env["PATH"], "HOME": "/home/probe-test",
                                     "CLAUDE_CODE_OAUTH_TOKEN": ALL_SECRETS["CLAUDE_CODE_OAUTH_TOKEN"]})
        # claude-sub's review is omp through Gemini (GEMINI_API_KEY) and OpenRouter (OPENROUTER_API_KEY): each omp call gets
        # exactly the one key its entry's door names, and no other secret.
        omp_children = self.child_envs("omp")
        self.assertEqual(len(omp_children), 2, "the two omp review entries of claude-sub were each called once")
        for child in omp_children:
            secrets = {k: v for k, v in child.items() if k not in ("PATH", "HOME", "LC_CTYPE")}
            self.assertEqual(len(secrets), 1)
            self.assertIn(list(secrets)[0], ("GEMINI_API_KEY", "OPENROUTER_API_KEY"))

    def test_door_with_two_secret_names_passes_when_only_the_second_is_set(self):
        # I4: only ANTHROPIC_API_KEY set (second of anthropic door) must satisfy a claude entry.
        self.fake("claude")
        self.fake("omp")
        self.fake("pi")
        secrets = {k: v for k, v in ALL_SECRETS.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        rc, out, err = self.check(["claude-sub"], self.env(secrets=secrets))
        self.assertEqual(rc, 0, err)
        for child in self.child_envs("claude"):
            self.assertEqual(child.get("ANTHROPIC_API_KEY"), ALL_SECRETS["ANTHROPIC_API_KEY"])
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", child)

    def test_unmeasured_workers_fail_without_being_called(self):
        repo = self.copy_root()
        (repo / "presets" / "guessed.json").write_text(json.dumps({
            "name": "guessed",
            "tiers": {"build": ["codex:openai/gpt-5"], "review": ["grok:xai/grok-2"]},
        }), encoding="utf-8")
        self.fake("codex")
        self.fake("grok")
        rc, out, err = self.check(["guessed"], self.env(), root=repo)
        self.assertEqual(rc, 1)
        self.assertIn("failed: codex:openai/gpt-5: unmeasured", err)
        self.assertIn("failed: grok:xai/grok-2: unmeasured", err)
        self.assertEqual(self.calls("codex"), [])
        self.assertEqual(self.calls("grok"), [])

    def test_probe_redacts_a_secret_echoed_by_the_cli(self):
        secret = ALL_SECRETS["PIVOTAL_KAGGLE_KEY"]
        self.fake("pi", stdout="", stderr=f"bad key {secret}\n", code=1)
        self.fake("omp")
        self.fake("claude")
        rc, out, err = self.check(["free-kaggle"], self.env())
        self.assertEqual(rc, 1)
        self.assertNotIn(secret, out + err)

    def test_child_output_with_invalid_utf8_does_not_abort_whole_check(self):
        # S3: errors="replace" and per-entry catch; other entries still reported.
        script = "#!/bin/sh\nprintf '\\377ok'\n"
        path = self.bin_dir / "omp"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
        self.fake("pi")
        rc, out, err = self.check(["free-kaggle"], self.env())
        # the omp entries produced output (with replacement) so they pass; overall should still complete
        self.assertIn("pi:kaggle/qwen3.6-35b-a3b", out + err)
        self.assertIn("omp:groq/openai/gpt-oss-120b", out + err)

    # --- --all, names, refusals and static checks (I4) ----------------------------------------------------------

    def test_all_runs_every_shipped_preset(self):
        # I4: --all must check every preset, not be ignored.
        for worker in ("pi", "claude", "omp"):
            self.fake(worker)
        rc, out, err = self.check(sorted(SHIPPED), self.env())
        # same as --all; also test via CLI path
        out2, err2 = io.StringIO(), io.StringIO()
        rc2 = cli.main(["preset", "check", "--all"], self.env(), out2, err2)
        self.assertEqual(rc2, 0, err2.getvalue())
        for name in SHIPPED:
            self.assertIn(f"ok: preset {name} passed check", out2.getvalue())

    def test_all_with_names_is_refused(self):
        for worker in ("pi", "claude", "omp"):
            self.fake(worker)
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main(["preset", "check", "--all", "free-kaggle"], self.env(), out, err)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("ok: preset", out.getvalue())
        self.assertEqual(self.calls("pi"), [])  # refused before probing (I4)
        out2, err2 = io.StringIO(), io.StringIO()
        rc2 = cli.main(["preset", "check", "free-kaggle", "--all"], self.env(), out2, err2)
        self.assertNotEqual(rc2, 0)
        self.assertNotIn("ok: preset", out2.getvalue())

    def test_cross_vendor_refusal_in_check(self):
        # I4: a preset where build and review share a vendor is refused by static checks.
        repo = self.copy_root()
        (repo / "presets" / "same.json").write_text(json.dumps({
            "name": "same",
            "tiers": {"build": ["claude:anthropic/sonnet"], "review": ["claude:anthropic/opus"]},
        }), encoding="utf-8")
        self.fake("claude")
        rc, out, err = self.check(["same"], self.env(), root=repo)
        self.assertEqual(rc, 1)
        self.assertIn("failed: same:", err)
        self.assertIn("no review entry outside anthropic", err)
        self.assertEqual(self.calls("claude"), [])

    def test_unknown_preset_name_refused(self):
        rc, out, err = self.check(["does-not-exist"], self.env())
        self.assertEqual(rc, 1)
        self.assertEqual(err, "failed: preset does-not-exist not found\n")

    def test_bad_names_refused_before_any_probe(self):
        self.fake("pi")
        self.fake("omp")
        self.fake("claude")
        for bad in ("../kaggle/catalog", "Free-Kaggle", "-x", "a/b", ""):
            with self.subTest(name=bad):
                rc, out, err = self.check(["free-kaggle", bad], self.env())
                self.assertEqual(rc, 2)
                self.assertIn("bad preset name", err)
        self.assertEqual(self.calls("pi"), [])

    def test_no_names_is_a_usage_error(self):
        rc, out, err = self.check([], self.env())
        self.assertEqual(rc, 2)

    def test_malformed_preset_file_refused(self):
        repo = self.copy_root()
        (repo / "presets" / "bad.json").write_text("{not json", encoding="utf-8")
        rc, out, err = self.check(["bad"], self.env(), root=repo)
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("failed: bad preset bad: "), err)

    def test_deeply_nested_preset_is_refused_not_a_traceback(self):
        # S4: RecursionError on a deeply nested preset is reported as a bad preset.
        repo = self.copy_root()
        (repo / "presets" / "deep.json").write_text("[" * 50000 + "]" * 50000, encoding="utf-8")
        rc, out, err = self.check(["deep"], self.env(), root=repo)
        self.assertEqual(rc, 1)
        self.assertIn("failed: bad preset deep:", err)
        self.assertNotIn("Traceback", err)

    def test_preset_naming_unmeasured_model_refused(self):
        repo = self.copy_root()
        catalog = json.loads((repo / "kaggle" / "catalog.json").read_text(encoding="utf-8"))
        catalog["single_model_both_t4"].append({
            "candidate": "Z", "status": "unmeasured", "sysop_model_id": "qwen-unmeasured-test", "model": "x/y/z.gguf",
        })
        (repo / "kaggle" / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        (repo / "presets" / "bad-unmeasured.json").write_text(json.dumps({
            "name": "bad-unmeasured",
            "kaggle": {"model": "qwen-unmeasured-test"},
            "tiers": {"build": ["pi:kaggle/qwen-unmeasured-test"], "review": ["agy:google/gemini-3.8-flash-high"]},
        }), encoding="utf-8")
        self.fake("pi")
        self.fake("agy")
        rc, out, err = self.check(["bad-unmeasured"], self.env(), root=repo)
        self.assertEqual(rc, 1)
        self.assertIn("qwen-unmeasured-test", err)
        self.assertIn("unmeasured", err)
        self.assertEqual(self.calls("pi"), [])

    def test_kaggle_entry_not_in_catalog_refused(self):
        repo = self.copy_root()
        (repo / "presets" / "ghost.json").write_text(json.dumps({
            "name": "ghost",
            "kaggle": {"model": "qwen9-ghost"},
            "tiers": {"build": ["pi:kaggle/qwen9-ghost"], "review": ["agy:google/gemini-3.8-flash-high"]},
        }), encoding="utf-8")
        rc, out, err = self.check(["ghost"], self.env(), root=repo)
        self.assertEqual(rc, 1)
        self.assertIn("not in the catalog", err)

    def test_partial_kaggle_candidate_missing_a_field_refused(self):
        repo = self.copy_root()
        catalog = json.loads((repo / "kaggle" / "catalog.json").read_text(encoding="utf-8"))
        for c in catalog["single_model_both_t4"]:
            if c.get("sysop_model_id") == "qwen3.6-35b-a3b":
                c["time_to_healthy_s"] = None
        (repo / "kaggle" / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        self.fake("pi")
        self.fake("omp")
        self.fake("claude")
        rc, out, err = self.check(["free-kaggle"], self.env(), root=repo)
        self.assertEqual(rc, 1)
        self.assertIn("time_to_healthy_s", err)
        self.assertEqual(self.calls("pi"), [])

    def test_measured_claim_for_unmeasured_worker_refused(self):
        repo = self.copy_root()
        preset = json.loads((repo / "presets" / "free-kaggle.json").read_text(encoding="utf-8"))
        preset["tiers"]["review"].append("codex:openai/gpt-5")
        preset["tiers"]["build"].append("grok:xai/grok-2")  # keep cross-vendor plausible? easier to just test static path
        # simpler: use a crafted preset
        (repo / "presets" / "m.json").write_text(json.dumps({
            "name": "m", "measured": ["codex:openai/gpt-5"],
            "tiers": {"build": ["agy:google/gemini-3.8-flash-high"], "review": ["codex:openai/gpt-5"]},
        }), encoding="utf-8")
        self.fake("agy")
        self.fake("codex")
        rc, out, err = self.check(["m"], self.env(), root=repo)
        self.assertEqual(rc, 1)
        self.assertIn("marked measured, but T0 has not measured the codex CLI's flags", err)
        self.assertEqual(self.calls("codex"), [])

    def test_engine_unavailable_fails_closed(self):
        self.fake("pi")
        self.fake("omp")
        self.fake("claude")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"engine": None}):
            rc = cli.main(["preset", "check", "free-kaggle"], self.env(), out, err)
        self.assertEqual(rc, 1)
        self.assertIn("failed: engine unavailable", err.getvalue())
        self.assertNotIn("ok", out.getvalue())
        self.assertEqual(self.calls("pi"), [])

    def test_load_engine_restores_sys_path(self):
        # S1: render.py inserts gh-piv on every import; load_engine must leave sys.path unchanged.
        before = list(sys.path)
        presetcheck.load_engine(ROOT)
        presetcheck.load_engine(ROOT)
        self.assertEqual(sys.path, before)

    # --- the CLI surface ------------------------------------------------------------------------------------------

    def test_cli_check_of_free_kaggle_is_ok_with_its_secrets(self):
        self.fake("pi")
        self.fake("omp")
        self.fake("claude")
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main(["preset", "check", "free-kaggle"], self.env(), out, err)
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("ok: preset free-kaggle passed check", out.getvalue())

    def test_cli_check_without_secrets_is_refused_for_each_entry(self):
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main(["preset", "check", "free-kaggle"], self.bin.env({}), out, err)
        self.assertEqual(rc, 1)
        self.assertNotIn("ok: preset", out.getvalue())
        self.assertIn("missing secret", err.getvalue())

    def test_preset_setter_still_works(self):
        # Both sides of the rebase: "piv preset REPO PRESET" (main) and "piv preset check ..." (this branch) coexist.
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main(["preset"], {}, out, err)
        self.assertEqual(rc, 2)
        self.assertIn("usage", err.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
