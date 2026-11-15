"""The Kaggle serve notebook (T4): the template, its renderer and the exit logic.

Nothing here starts Kaggle, ngrok, llama-server or a network connection. The rendered notebook's own cells are run
with a fake world (clock, tunnel, server, metrics), so the code under test is the code that would run on Kaggle.
What this cannot show is in docs/engines.md: a live session.
"""

import contextlib
import hashlib
import http.server
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "kaggle"))

import lifecycle  # noqa: E402
import render  # noqa: E402
from piv import serve  # noqa: E402  (render put gh-piv on the path)

CATALOG = json.loads((ROOT / "kaggle" / "catalog.json").read_text())
KEY = "k3yAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # 51 chars: token_urlsafe-shaped
TOKEN = "2ngrokTOKENvalue_1234567890abcdef"
DOMAIN = "pivotal-serve.ngrok-free.app"
MODEL = "qwen3.6-35b-a3b"
PINS = {  # test values: never the shipped kaggle/pins.json (those are 2026-11-01 public values, unconfirmed live)
    "ngrok": "1.4.0", "huggingface_hub": "0.30.2",
    "llama_cpp": {"commit": "a" * 40, "sha256": "b" * 64},
    "model_revisions": {"qwen3.6-35b-a3b": "c" * 40, "qwen3.8-27b": "d" * 40, "gemma-4-26b-a4b": "e" * 40},
}


def measured_catalog():
    cat = json.loads(json.dumps(CATALOG))
    cat["single_model_both_t4"][0].update(status="measured", context=65536, vram_gb=30.5, tok_s=100.0)  # 2 slots x 32768
    return cat


def notebook_namespace(model=MODEL):
    """Run every code cell but the last (which calls main) the way the kernel would, and return the namespace."""
    cells = [c for c in render.render(CATALOG, model, PINS)["cells"] if c["cell_type"] == "code"]
    ns: dict = {"__name__": "notebook"}
    for cell in cells[:-1]:
        exec(compile("".join(cell["source"]), "<cell>", "exec"), ns)
    return ns, cells


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.last = self.t

    def now(self):
        assert self.t >= self.last, "the injected clock must be monotonic"
        self.last = self.t
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class IdleClockTest(unittest.TestCase):
    def dog(self, **kw):
        self.clock = Clock()
        return lifecycle.Watchdog(self.clock.now, **kw)

    def test_idle_exit_comes_at_exactly_twenty_minutes_without_a_request(self):
        dog = self.dog()
        dog.ready()
        self.clock.t += 20 * 60 - 1
        self.assertIsNone(dog.verdict())
        self.clock.t += 1
        self.assertEqual(dog.verdict(), "idle")

    def test_a_request_resets_the_idle_clock_and_a_running_one_holds_it_off(self):
        dog = self.dog()
        dog.ready()
        dog.observe((10, False))  # baseline
        self.clock.t += 19 * 60
        dog.observe((11, False))  # a request was served
        self.clock.t += 19 * 60
        self.assertIsNone(dog.verdict())
        dog.observe((11, True))  # one is running
        self.clock.t += 19 * 60
        self.assertIsNone(dog.verdict())
        self.clock.t += 2 * 60
        self.assertEqual(dog.verdict(), "idle")

    def test_an_unreadable_sample_is_not_use(self):
        dog = self.dog()
        dog.ready()
        for _ in range(10):
            self.clock.t += 130
            dog.observe(None)
        self.assertEqual(dog.verdict(), "idle")

    def test_hard_exit_at_eleven_hours_even_while_busy(self):
        dog = self.dog()
        dog.ready()
        reason = lifecycle.watch(dog, lambda: (1, True), self.clock.sleep)
        self.assertEqual(reason, "hard")
        self.assertEqual(self.clock.t - dog.start, 11 * 3600)

    def test_hard_exit_counts_from_the_start_not_from_ready(self):
        dog = self.dog()
        self.clock.t += 3600  # an hour of loading
        dog.ready()
        reason = lifecycle.watch(dog, lambda: (1, True), self.clock.sleep)
        self.assertEqual(reason, "hard")
        self.assertEqual(self.clock.t - dog.start, 11 * 3600)

    def test_the_load_verdict_is_none_before_the_launch(self):
        dog = self.dog()
        self.clock.t += lifecycle.LOAD_S * 3  # a long build or download: not the server's load
        self.assertIsNone(dog.verdict())
        dog.launched()
        self.clock.t += lifecycle.LOAD_S
        self.assertEqual(dog.verdict(), "load")

    def test_a_dead_server_after_ready_is_an_error_not_idle(self):
        dog = self.dog()
        dog.ready()
        alive = iter([True, True, False])
        reason = lifecycle.watch(dog, lambda: (5, False), self.clock.sleep, alive=lambda: next(alive))
        self.assertEqual(reason, "error")
        self.assertLess(self.clock.t - dog.start, 60)  # at the next poll, not at twenty minutes

    def test_a_server_that_stays_alive_still_ends_idle(self):
        dog = self.dog()
        dog.ready()
        self.assertEqual(lifecycle.watch(dog, lambda: (5, False), self.clock.sleep, alive=lambda: True), "idle")

    def test_a_server_that_never_becomes_ready_is_a_load_failure_not_an_idle_exit(self):
        dog = self.dog()
        dog.launched()
        self.clock.t += lifecycle.LOAD_S - 1
        self.assertIsNone(dog.verdict())
        self.clock.t += 1
        self.assertEqual(dog.verdict(), "load")

    def test_watch_with_an_idle_server_ends_after_twenty_minutes_and_does_not_loop_again(self):
        dog = self.dog()
        dog.ready()
        calls = []
        reason = lifecycle.watch(dog, lambda: calls.append(1) or (5, False), self.clock.sleep)
        self.assertEqual(reason, "idle")
        self.assertAlmostEqual(self.clock.t - dog.start, 20 * 60, delta=lifecycle.POLL_S)

    def test_a_request_that_starts_just_before_the_deadline_keeps_the_session_up(self):
        dog = self.dog()
        dog.ready()
        start = self.clock.t
        busy_from = start + lifecycle.IDLE_S - 1  # begins one second before the idle deadline, runs for 4 s

        def sample():
            return (5, busy_from <= self.clock.t < busy_from + 4 + lifecycle.POLL_S)

        reason = lifecycle.watch(dog, sample, self.clock.sleep)
        self.assertEqual(reason, "idle")
        self.assertGreater(self.clock.t - start, lifecycle.IDLE_S + 1)  # the request pushed the exit out

    def test_a_counter_that_flips_between_two_values_does_not_keep_the_session_up(self):
        dog = self.dog()
        dog.ready()
        flip = iter(range(10**6))
        reason = lifecycle.watch(dog, lambda: (100 if next(flip) % 2 else 0, False), self.clock.sleep)
        self.assertEqual(reason, "idle")
        self.assertAlmostEqual(self.clock.t - dog.start, 20 * 60, delta=2 * lifecycle.POLL_S)

    def test_a_client_that_decodes_nothing_does_not_keep_the_session_up(self):
        dog = self.dog()
        dog.ready()
        self.assertEqual(lifecycle.watch(dog, lambda: (7, False), self.clock.sleep), "idle")  # GET /v1/models moves no counter

    def test_limits_are_the_planned_ones(self):
        self.assertEqual((lifecycle.IDLE_S, lifecycle.HARD_S), (1200, 39600))


class ParseTest(unittest.TestCase):
    METRICS = (
        "# HELP llamacpp:prompt_tokens_total x\n# TYPE llamacpp:prompt_tokens_total counter\n"
        "llamacpp:prompt_tokens_total 120\nllamacpp:tokens_predicted_total 30\nllamacpp:n_decode_total 7\n"
        "llamacpp:requests_processing 1\nllamacpp:requests_deferred 0\n"
    )

    def test_counters_and_busy(self):
        self.assertEqual(lifecycle.parse_activity(self.METRICS), (157, True))
        self.assertEqual(lifecycle.parse_activity(self.METRICS.replace("processing 1", "processing 0")), (157, False))

    def test_anything_else_is_none(self):
        for text in ("", "garbage", "llamacpp:requests_processing 1\n", "llamacpp:n_decode_total nan\n",
                     "llamacpp:n_decode_total -3\n", "<html>502</html>"):
            self.assertIsNone(lifecycle.parse_activity(text), text)

    def test_domain_bound_messages(self):
        for text in ("failed to start tunnel: ERR_NGROK_334", "The endpoint 'https://x.ngrok-free.app' is already online.",
                     "domain already bound"):
            self.assertTrue(lifecycle.domain_bound(text), text)
        for text in ("ERR_NGROK_105 authentication failed", "connection reset", ""):
            self.assertFalse(lifecycle.domain_bound(text), text)

    def test_redact_masks_every_form_and_leaves_short_values(self):
        out = lifecycle.redact(f"a {KEY} b {KEY[:5]} c %s" % "x", [KEY, "short"])
        self.assertNotIn(KEY, out)
        self.assertIn("short", lifecycle.redact("short", ["short"]))
        odd = "ab cd/ef+gh" * 3
        self.assertNotIn("ab%20cd", lifecycle.redact("ab%20cd%2Fef%2Bgh" * 3, [odd]))
        self.assertNotIn("ab+cd", lifecycle.redact("ab+cd%2Fef%2Bgh" * 3, [odd]))


class RenderTest(unittest.TestCase):
    def test_renders_a_partial_candidate_with_the_serve_configuration_not_a_made_up_measurement(self):
        nb = render.render(CATALOG, MODEL, PINS)
        self.assertEqual(nb["nbformat"], 4)
        text = "\n".join("".join(c["source"]) for c in nb["cells"])
        self.assertNotIn("__SPEC__", text)
        self.assertNotIn("__LIFECYCLE__", text)
        spec = render.spec_for(CATALOG["single_model_both_t4"][0], PINS)
        self.assertEqual(spec["repo"], "unsloth/Qwen3.6-35B-A3B-GGUF")
        self.assertEqual(spec["file"], "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf")
        serve_cfg = CATALOG["single_model_both_t4"][0]["serve"]
        self.assertEqual((spec["slots"], spec["ctx"], spec["flags"]), (serve_cfg["slots"], serve_cfg["ctx"], serve_cfg["flags"]))
        self.assertIsNone(CATALOG["single_model_both_t4"][0]["context"])  # the catalog is not edited to fit

    def test_a_measured_candidate_renders_and_its_serve_ctx_is_the_per_slot_context(self):
        cat = measured_catalog()
        slots = cat["single_model_both_t4"][0]["serve"]["slots"]
        cat["single_model_both_t4"][0]["serve"]["ctx"] = 16384
        cat["single_model_both_t4"][0]["context"] = 16384 * slots
        spec = render.spec_for(cat["single_model_both_t4"][0], PINS)
        self.assertEqual(spec["ctx"], 16384)
        self.assertIn("16384", "".join(render.render(cat, MODEL, PINS)["cells"][2]["source"]))

    def test_every_candidate_serves_an_agent_sized_context_and_the_notebook_starts_with_it(self):
        for entry in CATALOG["single_model_both_t4"]:
            model = entry["sysop_model_id"]
            ns, _ = notebook_namespace(model)
            spec = ns["SPEC"]
            self.assertGreaterEqual(spec["ctx"], render.MIN_AGENT_CTX, model)
            argv = ns["server_argv"]("/b/llama-server", "/m.gguf", "/k/key", spec)
            self.assertEqual(argv[argv.index("--parallel") + 1], str(entry["serve"]["slots"]), model)
            self.assertEqual(argv[argv.index("-c") + 1], str(entry["serve"]["slots"] * entry["serve"]["ctx"]), model)

    def test_a_per_slot_context_below_the_agent_floor_is_refused_and_named(self):
        for ctx in (4096, render.MIN_AGENT_CTX - 1):
            cat = json.loads(json.dumps(CATALOG))
            cat["single_model_both_t4"][0]["serve"]["ctx"] = ctx
            with self.assertRaisesRegex(render.RenderError, "below"):
                render.render(cat, MODEL, PINS)
        cat = json.loads(json.dumps(CATALOG))
        cat["single_model_both_t4"][0]["serve"]["ctx"] = render.MIN_AGENT_CTX
        render.render(cat, MODEL, PINS)  # the floor itself is served

    def test_every_catalog_partial_candidate_renders_and_every_code_cell_compiles(self):
        for entry in CATALOG["single_model_both_t4"]:
            nb = render.render(CATALOG, entry["sysop_model_id"], PINS)
            for cell in nb["cells"]:
                if cell["cell_type"] == "code":
                    compile("".join(cell["source"]), entry["candidate"], "exec")

    def test_the_rendered_server_argv_carries_the_catalogs_measured_flags_and_slots(self):
        ns, _ = notebook_namespace()
        spec = ns["SPEC"]
        argv = ns["server_argv"]("/b/llama-server", "/m.gguf", "/k/key", spec)
        flags = CATALOG["single_model_both_t4"][0]["serve"]["flags"]
        start = argv.index(flags[0])
        self.assertEqual(argv[start:start + len(flags)], flags)
        self.assertEqual(argv[argv.index("--parallel") + 1], str(spec["slots"]))
        self.assertEqual(argv[argv.index("-c") + 1], str(spec["slots"] * spec["ctx"]))
        for flag in ("-ngl", "--split-mode", "--flash-attn", "-ctk", "-ctv"):
            self.assertIn(flag, argv)
        self.assertNotIn("999", argv)

    def test_the_serve_field_has_a_source_and_is_validated(self):
        for change in ({"flags": None}, {"flags": []}, {"flags": ["-ngl", "99; id"]}, {"flags": ["--api-key", "x"]},
                       {"flags": ["--host", "0.0.0.0"]}, {"flags": ["-c", "5"]}, {"slots": 0}, {"slots": True},
                       {"slots": 99}, {"source": ""}, {"source": None}):
            cat = json.loads(json.dumps(CATALOG))
            cat["single_model_both_t4"][0]["serve"].update(change)
            with self.assertRaises(render.RenderError, msg=str(change)):
                render.render(cat, MODEL, PINS)
        cat = json.loads(json.dumps(CATALOG))
        del cat["single_model_both_t4"][0]["serve"]
        with self.assertRaisesRegex(render.RenderError, "serve"):
            render.render(cat, MODEL, PINS)

    def test_a_measured_context_must_be_what_is_served_and_url_style_flags_are_refused(self):
        cat = measured_catalog()
        entry = cat["single_model_both_t4"][0]
        slots, ctx = entry["serve"]["slots"], entry["serve"]["ctx"]
        self.assertEqual(entry["context"], slots * ctx)
        render.render(cat, MODEL, PINS)
        entry["context"] = 16384  # says 16384, serves 2 x 32768
        with self.assertRaisesRegex(render.RenderError, "context"):
            render.render(cat, MODEL, PINS)
        for flags in (["-hf", "evil/repo"], ["-mu", "https://evil.example/m.gguf"], ["https://evil.example/m.gguf"],
                      ["-ngl", "99", "--hf-repo", "x/y"]):
            cat = json.loads(json.dumps(CATALOG))
            cat["single_model_both_t4"][0]["serve"]["flags"] = flags
            with self.assertRaises(render.RenderError, msg=str(flags)):
                render.render(cat, MODEL, PINS)

    def test_a_null_context_no_longer_falls_back_to_an_invented_number(self):
        cat = json.loads(json.dumps(CATALOG))
        self.assertIsNone(cat["single_model_both_t4"][0]["context"])
        self.assertFalse(hasattr(render, "CTX") or hasattr(render, "SLOTS"))

    def test_a_null_or_malformed_pin_refuses_the_render_and_names_the_pin(self):
        def with_pins(**change):
            pins = json.loads(json.dumps(PINS))
            for dotted, value in change.items():
                node = pins
                *path, last = dotted.split("__")
                for part in path:
                    node = node[part]
                node[last] = value
            return pins

        bad = [("ngrok", None), ("huggingface_hub", None), ("llama_cpp__commit", None), ("llama_cpp__sha256", None),
               ("model_revisions__qwen3.6-35b-a3b", None), ("ngrok", "latest"), ("ngrok", "1.4.0; id"), ("huggingface_hub", ""),
               ("llama_cpp__commit", "master"), ("llama_cpp__commit", "A" * 40), ("llama_cpp__sha256", "b" * 63),
               ("model_revisions__qwen3.6-35b-a3b", "main"), ("model_revisions__qwen3.6-35b-a3b", 7)]
        for name, value in bad:
            with self.assertRaises(render.RenderError, msg=f"{name}={value!r}"):
                render.render(CATALOG, MODEL, with_pins(**{name: value}))
        with self.assertRaises(render.RenderError):
            render.render(CATALOG, MODEL, None)
        with self.assertRaisesRegex(render.RenderError, "model_revisions.qwen3.6-35b-a3b"):
            render.render(CATALOG, MODEL, with_pins(**{"model_revisions__qwen3.6-35b-a3b": None}))

    def test_the_shipped_pins_render_every_candidate_and_are_not_the_test_values(self):
        shipped = json.loads((ROOT / "kaggle" / "pins.json").read_text())
        for entry in CATALOG["single_model_both_t4"]:
            nb = render.render(CATALOG, entry["sysop_model_id"], shipped)
            spec = render.spec_for(entry, shipped)
            self.assertEqual(spec["pins"]["revision"], shipped["model_revisions"][entry["sysop_model_id"]])
            self.assertTrue(nb["cells"])
        self.assertNotEqual(shipped["ngrok"], PINS["ngrok"])
        self.assertNotEqual(shipped["llama_cpp"]["commit"], PINS["llama_cpp"]["commit"])
        # and a null in the shipped file would still refuse
        shipped["llama_cpp"]["sha256"] = None
        with self.assertRaisesRegex(render.RenderError, "null"):
            render.render(CATALOG, MODEL, shipped)

    def test_the_pins_reach_the_notebook_and_nothing_is_fetched_unpinned(self):
        ns, cells = notebook_namespace()
        self.assertEqual(ns["SPEC"]["pins"], {"ngrok": "1.4.0", "huggingface_hub": "0.30.2", "llama_commit": "a" * 40,
                                              "llama_sha256": "b" * 64, "revision": "c" * 40})
        text = "".join(cells[2]["source"])
        for unpinned in ("refs/heads/master", "master.tar.gz", "--depth 1"):
            self.assertNotIn(unpinned, text)
        self.assertIn("revision=sys.argv[3]", text)

    def test_refuses_unmeasured_and_the_unshipped_pair_and_unknown_ids(self):
        cat = json.loads(json.dumps(CATALOG))
        cat["single_model_both_t4"][0]["status"] = "unmeasured"
        with self.assertRaisesRegex(render.RenderError, "unmeasured"):
            render.render(cat, MODEL, PINS)
        for bad in ("S", "nope", "", None, "Qwen", "a b"):
            with self.assertRaises(render.RenderError):
                render.render(CATALOG, bad, PINS)

    def test_refuses_a_partial_or_measured_candidate_with_a_null_field_it_needs(self):
        for field in ("model", "sysop_model_id", "time_to_healthy_s", "measured_on", "vendor"):
            cat = json.loads(json.dumps(CATALOG))
            entry = cat["single_model_both_t4"][0]
            entry[field] = None
            with self.assertRaises(render.RenderError, msg=field):
                render.spec_for(entry, PINS)
        cat = measured_catalog()
        cat["single_model_both_t4"][0]["tok_s"] = None
        with self.assertRaisesRegex(render.RenderError, "tok_s"):
            render.render(cat, MODEL, PINS)

    def test_a_hostile_catalog_value_cannot_break_out_of_the_json_or_the_python_string(self):
        hostile = [
            'unsloth/x/f"); import os; os.system("id"); ("a.gguf',
            "unsloth/x/f\n__import__('os').system('id')#.gguf",
            "unsloth/x/f'''; boom; '''.gguf",
            "unsloth/x/a.gguf import os",
            "unsloth/x/__SPEC__.gguf",
            "unsloth/x/../../etc/a.gguf",
            "unsloth/x/f\\\".gguf",
            "unsloth/x/{{ x }}.gguf",
            "unsloth/x/é.gguf",
            "/x/y.gguf",
            "unsloth/x/a.bin",
            "unsloth/x/" + "a" * 300 + ".gguf",
        ]
        for model in hostile:
            cat = json.loads(json.dumps(CATALOG))
            cat["single_model_both_t4"][0]["model"] = model
            with self.assertRaises(render.RenderError, msg=model):
                render.render(cat, MODEL, PINS)
        for ctx in ("32768", True, 1.5, 10, 10**9, [1], {"a": 1}):
            for where in ("context", "serve.ctx"):
                cat = json.loads(json.dumps(CATALOG))
                target = cat["single_model_both_t4"][0]
                (target["serve"] if where == "serve.ctx" else target)[where.split(".")[-1]] = ctx
                with self.assertRaises(render.RenderError, msg=f"{where} {ctx!r}"):
                    render.render(cat, MODEL, PINS)

    def test_the_literal_survives_any_string_and_runs_as_data(self):
        for text in ('"; import os #', "'''", "\\", "\n", " ", "é\U0001F600", "__SPEC__", "\x00"):
            literal = render._literal({"x": text})
            self.assertTrue(literal.isascii() and "\n" not in literal)
            ns: dict = {}
            exec(f"import json\nv = json.loads({literal})", ns)
            self.assertEqual(ns["v"], {"x": text})

    def test_the_other_cells_are_unchanged_by_a_value_that_looks_like_a_placeholder(self):
        cat = json.loads(json.dumps(CATALOG))
        cat["single_model_both_t4"][0]["notes"] = "__LIFECYCLE__ __SPEC__"
        nb = render.render(cat, MODEL, PINS)
        self.assertEqual(sum("".join(c["source"]).count("__SPEC__") for c in nb["cells"]), 0)

    def test_the_template_has_each_placeholder_once_and_the_lifecycle_cell_is_the_module(self):
        raw = (ROOT / "kaggle" / "serve.ipynb.tmpl").read_text()
        self.assertEqual(raw.count("__SPEC__"), 1)
        self.assertEqual(raw.count("__LIFECYCLE__"), 1)
        nb = render.render(CATALOG, MODEL, PINS)
        self.assertEqual("".join(nb["cells"][1]["source"]), (ROOT / "kaggle" / "lifecycle.py").read_text())

    def test_the_cli_prints_a_notebook_with_the_llama_server_and_exit_logic_and_refuses_cleanly(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            pins_file = Path(tmp) / "pins.json"
            pins_file.write_text(json.dumps(PINS))
            self.assertEqual(render.main(["--model", MODEL, "--pins", str(pins_file)], out), 0)
            err = io.StringIO()
            null_file = Path(tmp) / "null.json"
            null_file.write_text(json.dumps(dict(PINS, ngrok=None)))
            with contextlib.redirect_stderr(err):  # a null pin refuses
                self.assertEqual(render.main(["--model", MODEL, "--pins", str(null_file)], io.StringIO()), 2)
            self.assertIn("pin ngrok is null", err.getvalue())
            self.assertEqual(render.main(["--model", MODEL], io.StringIO()), 0)  # the shipped pins render
        text = "".join("".join(c["source"]) for c in json.loads(out.getvalue())["cells"])
        for needle in ("llama-server", "--api-key-file", "--parallel", "--split-mode", "--flash-attn", "q8_0", "IDLE_S = 20 * 60",
                       "HARD_S = 11 * 3600", "ngrok.forward", "domain_bound"):
            self.assertIn(needle, text)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(render.main(["--model", "nope"], io.StringIO()), 2)
        self.assertIn("render:", err.getvalue())

    def test_the_notebook_reads_the_file_and_names_piv_kaggle_setup_writes(self):
        written = {}

        class Capture:
            def run(self, *args, **kw):
                folder = args[args.index("-p") + 1]
                written.update(json.loads((Path(folder) / serve.SERVE_FILE).read_text()))

        req = serve.Request("ada", "https://" + DOMAIN, TOKEN, None)
        serve._publish(Capture(), "ada/" + serve.DATASET, req, KEY, create=True)
        ns, _ = notebook_namespace()
        self.assertEqual(ns["SPEC"]["serve_file"], serve.SERVE_FILE)
        self.assertEqual(ns["parse_secrets"](json.dumps(written)), (DOMAIN, KEY, TOKEN))


class FakeWorld:
    """A world for notebook.Io: no network, no process, a clock that only moves when the notebook sleeps."""

    def __init__(self, ns, *, tunnel_error=None, samples=None, healthy_after=0, leak=None, setup_s=0, exit_early=False, tunnel_s=0, die_at=None):
        self.clock = Clock()
        self.setup_s, self.exit_early, self.tunnel_s, self.die_at = setup_s, exit_early, tunnel_s, die_at
        self.calls, self.argv, self.key_file_seen = [], None, None
        self.tunnel_error, self.samples, self.healthy_after, self.leak = tunnel_error, samples, healthy_after, leak
        outer = self

        class World(ns["Io"]):
            def now(self):
                return outer.clock.now()

            def sleep(self, seconds):
                outer.clock.sleep(seconds)

            def read_secrets(self):
                return DOMAIN, KEY, TOKEN

            def open_tunnel(self, port, token, domain):
                outer.calls.append("tunnel")
                outer.clock.t += outer.tunnel_s
                if outer.tunnel_error:
                    raise RuntimeError(outer.tunnel_error)

            def close_tunnel(self):
                outer.calls.append("close")

            def engine(self):
                outer.calls.append("engine")
                outer.clock.t += outer.setup_s  # a cold llama.cpp build takes 29 minutes
                return "/x/llama-server"

            def download(self, repo, file):
                outer.calls.append("download")
                return "/x/model.gguf"

            def launch(self, argv):
                outer.calls.append("launch")
                outer.argv = list(argv)
                path = argv[argv.index("--api-key-file") + 1]
                outer.key_file_seen = (Path(path).read_text(), stat.S_IMODE(os.stat(path).st_mode),
                                       stat.S_IMODE(os.stat(Path(path).parent).st_mode))
                outer.key_dir = Path(path).parent
                return outer.Proc(outer.exit_early, outer)

            def log_tail(self):
                return outer.leak or "server log"

            def healthy(self, port):
                outer.healthy_after -= 1
                return outer.healthy_after < 0

            def gpu_memory(self):
                return [7000, 7000]

            def sample(self, port, key):
                if callable(outer.samples):
                    return outer.samples(outer.clock.now())
                return outer.samples

        self.io = World(ns["SPEC"])

    class Proc:
        terminated = False

        def __init__(self, exited=False, world=None):
            self.exited, self.world = exited, world

        def poll(self):
            if self.world is not None and self.world.die_at is not None and self.world.clock.t >= self.world.die_at:
                return 137
            return 1 if self.exited else (None if not self.terminated else 0)

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True


class NotebookRunTest(unittest.TestCase):
    def run_main(self, **kw):
        ns, cells = notebook_namespace()
        world = FakeWorld(ns, **kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            reason = ns["main"](world.io)
        return reason, world, out.getvalue(), ns, cells

    def test_a_bound_domain_exits_at_once_before_any_engine_model_or_server(self):
        for text in ("ERR_NGROK_334", "The endpoint 'https://x' is already online."):
            reason, world, out, *_ = self.run_main(tunnel_error=text)
            self.assertEqual(reason, "bound")
            self.assertEqual(world.calls, ["tunnel", "close"])
            self.assertEqual(world.clock.t, 1000.0)  # not one second slept
            self.assertIn("EXIT bound", out)

    def test_any_other_tunnel_failure_is_an_error_not_a_bound_exit(self):
        reason, world, out, *_ = self.run_main(tunnel_error="ERR_NGROK_105 bad authtoken " + TOKEN)
        self.assertEqual(reason, "error")
        self.assertNotIn("launch", world.calls)
        self.assertNotIn(TOKEN, out)

    def test_an_idle_session_exits_after_twenty_minutes_and_stops_the_server(self):
        reason, world, out, *_ = self.run_main(samples=(5, False), healthy_after=3)
        self.assertEqual(reason, "idle")
        self.assertEqual(world.calls.count("launch"), 1)  # nothing restarts it
        self.assertEqual(world.calls[-1], "close")
        self.assertIn("EXIT idle", out)
        # healthy_after 3 polls x 3 s of loading, then twenty minutes
        self.assertAlmostEqual(world.clock.t - 1000.0, 9 + 1200, delta=10)

    def test_requests_keep_it_up_and_it_still_leaves_at_eleven_hours(self):
        counter = lambda t: (int((t - 1000) // 600), False)  # a request every ten minutes
        reason, world, out, *_ = self.run_main(samples=counter)
        self.assertEqual(reason, "hard")
        self.assertAlmostEqual(world.clock.t - 1000.0, 11 * 3600, delta=10)
        self.assertEqual(world.calls.count("launch"), 1)

    def test_a_cold_build_longer_than_the_load_budget_still_reaches_ready_then_idle(self):
        reason, world, out, *_ = self.run_main(samples=(5, False), setup_s=1740, healthy_after=3)
        self.assertEqual(reason, "idle")
        self.assertIn("READY", out)
        self.assertNotIn("EXIT load", out)

    def test_the_load_budget_counts_from_the_launch_not_from_the_start(self):
        reason, world, out, *_ = self.run_main(setup_s=1740, healthy_after=10**9)
        self.assertEqual(reason, "load")
        self.assertAlmostEqual(world.clock.t - 1000.0, 1740 + lifecycle.LOAD_S, delta=10)

    def test_a_server_that_dies_after_ready_is_an_error_with_a_masked_tail_not_an_idle_exit(self):
        leak = f"srv: killed {KEY}"
        reason, world, out, ns, cells = self.run_main(samples=(5, False), healthy_after=1, die_at=1000.0 + 300, leak=leak)
        self.assertEqual(reason, "error")
        self.assertIn("EXIT error", out)
        self.assertNotIn("EXIT idle", out)
        self.assertNotIn(KEY, out)
        self.assertIn("killed", out)
        self.assertLess(world.clock.t - 1000.0, 400)  # not twenty minutes later
        self.assertEqual(world.calls[-1], "close")

    def test_the_hard_limit_is_checked_right_after_the_tunnel_too(self):
        reason, world, out, *_ = self.run_main(tunnel_s=lifecycle.HARD_S + 1)
        self.assertEqual(reason, "hard")
        self.assertEqual(world.calls, ["tunnel", "close"])  # no engine, no download, no server

    def test_the_hard_limit_is_checked_between_setup_steps_not_only_after_launch(self):
        reason, world, out, *_ = self.run_main(setup_s=lifecycle.HARD_S + 1)
        self.assertEqual(reason, "hard")
        self.assertEqual(world.calls, ["tunnel", "engine", "close"])  # no download, no server
        self.assertIn("EXIT hard", out)

    def test_a_server_that_exits_while_loading_prints_a_masked_log_tail(self):
        leak = f"srv: api_keys: {KEY}\nAuthorization: Bearer {KEY}"
        reason, world, out, *_ = self.run_main(exit_early=True, healthy_after=10**9, leak=leak)
        self.assertEqual(reason, "error")
        self.assertIn("llama-server exited", out)
        self.assertNotIn(KEY, out)
        self.assertIn(lifecycle.MASK, out)

    def test_a_server_that_never_gets_healthy_fails_at_the_load_limit(self):
        reason, world, out, *_ = self.run_main(healthy_after=10**9)
        self.assertEqual(reason, "load")
        self.assertAlmostEqual(world.clock.t - 1000.0, lifecycle.LOAD_S, delta=10)

    def test_the_key_is_in_no_log_line_no_argv_and_only_in_a_private_file_removed_at_the_end(self):
        leak = f"srv: api_keys: {KEY}\nAuthorization: Bearer {KEY}\ntoken {TOKEN}"
        for samples, healthy in (((1, False), 0), (None, 10**9)):  # a clean exit and a failed start both print the log
            reason, world, out, *_ = self.run_main(samples=samples, healthy_after=healthy, leak=leak)
            for secret in (KEY, TOKEN):
                self.assertNotIn(secret, out)
                self.assertNotIn(secret, " ".join(world.argv))
        self.assertIn("--api-key-file", world.argv)
        self.assertNotIn("--api-key", [a for a in world.argv if a != "--api-key-file"])
        self.assertEqual(world.key_file_seen, (KEY, 0o600, 0o700))
        self.assertFalse(world.key_dir.exists())

    def test_an_exception_that_carries_the_key_is_printed_masked(self):
        ns, _ = notebook_namespace()
        world = FakeWorld(ns)

        def boom(argv):
            raise RuntimeError(f"cannot start with {KEY} and {TOKEN}")

        world.io.launch = boom
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ns["main"](world.io), "error")
        self.assertIn("EXIT error", out.getvalue())
        self.assertNotIn(KEY, out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertIn(lifecycle.MASK, out.getvalue())

    def test_the_real_say_masks_what_was_hidden(self):
        ns, _ = notebook_namespace()
        real = ns["Io"](ns["SPEC"])
        real.hide(KEY)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            real.say(f"x {KEY} y")
        self.assertEqual(out.getvalue().strip(), f"x {lifecycle.MASK} y")

    def test_a_cpu_fallback_is_an_error_not_a_session(self):
        ns, _ = notebook_namespace()
        world = FakeWorld(ns, samples=(1, False))
        world.io.gpu_memory = lambda: [0, 0]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ns["main"](world.io), "error")

    def test_server_argv_splits_across_gpus_with_parallel_slots_and_never_carries_the_key(self):
        ns, _ = notebook_namespace()
        flags = CATALOG["single_model_both_t4"][0]["serve"]["flags"]
        argv = ns["server_argv"]("/b/llama-server", "/m.gguf", "/k/key", {"slots": 2, "ctx": 32768, "flags": flags})
        self.assertEqual(argv[argv.index("--parallel") + 1], "2")
        self.assertEqual(argv[argv.index("-c") + 1], str(2 * 32768))
        self.assertEqual(argv[argv.index("--split-mode") + 1], "layer")
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")  # only ngrok reaches it
        self.assertIn("--api-key-file", argv)

    def test_malformed_secret_files_name_no_value(self):
        ns, _ = notebook_namespace()
        good = {"endpoint_url": "https://" + DOMAIN, "endpoint_key": KEY, "ngrok_authtoken": TOKEN}
        cases = [dict(good, endpoint_key="short" + KEY[:3]), dict(good, ngrok_authtoken=None),
                 dict(good, endpoint_url="http://" + DOMAIN), dict(good, endpoint_url="https://a/b; rm"), [1], "x"]
        for case in cases:
            with self.assertRaises(ns["Failure"]) as ctx:
                ns["parse_secrets"](json.dumps(case))
            self.assertNotIn(KEY, str(ctx.exception))
        with self.assertRaises(ns["Failure"]):
            ns["parse_secrets"]("{not json " + KEY)

    def test_the_last_cell_fails_the_kernel_on_a_failed_exit_and_not_on_a_planned_one(self):
        ns, cells = notebook_namespace()
        last = "".join(cells[-1]["source"])
        for reason, raises in (("idle", False), ("hard", False), ("bound", False), ("load", True), ("error", True)):
            ns["main"] = lambda reason=reason: reason
            if raises:
                with self.assertRaises(RuntimeError):
                    exec(last, ns)
            else:
                exec(last, ns)

    def test_the_notebook_runs_no_agent_git_or_test_command(self):
        text = "\n".join("".join(c["source"]) for c in render.render(CATALOG, MODEL, PINS)["cells"])
        for word in ("git clone", "git push", "git commit", "git pull", "unittest", "pytest", "claude", "codex", "apt-get", "crontab", "systemd"):
            self.assertNotIn(word, text)


@contextlib.contextmanager
def fd_capture():
    """Point the process's real fds 1 and 2 at a file: shows what native code (the ngrok SDK) writes behind print."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    with tempfile.TemporaryFile() as fh:
        os.dup2(fh.fileno(), 1)
        os.dup2(fh.fileno(), 2)
        box = types.SimpleNamespace(text="")
        try:
            yield box
        finally:
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
            fh.seek(0)
            box.text = fh.read().decode("utf-8", "replace")


def tarball(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc"
                tar.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class RealIoTest(unittest.TestCase):
    """The real Io methods, with the process, the network and the clock replaced at the edge."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ns, _ = notebook_namespace()
        self.root, self.tmp = Path(self.dir.name) / "kaggle", Path(self.dir.name) / "tmp"
        self.tmp.mkdir()
        self.io = self.ns["Io"](dict(self.ns["SPEC"]), root=str(self.root), tmp=str(self.tmp))
        self.io.hide(KEY, TOKEN)

    def completed(self, rc=0, out="", err=""):
        return subprocess.CompletedProcess([], rc, out, err)

    def test_the_tunnel_is_installed_pinned_and_native_output_is_masked(self):
        calls = []
        fake = types.ModuleType("ngrok")

        def forward(port, authtoken, domain):
            os.write(1, f"native stdout {authtoken}\n".encode())
            os.write(2, f"native stderr {authtoken}\n".encode())
            print("python print", authtoken)
            threads.append(threading.current_thread())
            return listener

        class Listener:
            def url(self):
                return "https://" + DOMAIN

        listener, threads = Listener(), []
        fake.forward = forward
        out = io.StringIO()
        with mock.patch.object(subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or self.completed()), \
                mock.patch.dict(sys.modules, {"ngrok": fake}), fd_capture() as fds, contextlib.redirect_stdout(out):
            self.io.open_tunnel(8080, TOKEN, DOMAIN)
        self.assertNotIn(TOKEN, fds.text)  # nothing reached the real fds
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertIn(lifecycle.MASK, out.getvalue())
        self.assertIn("native stdout", out.getvalue())
        self.assertIn("ngrok==1.4.0", calls[0][0])
        self.assertIs(self.io.tunnel, listener)
        self.assertIsNot(threads[0], threading.main_thread())  # no running event loop there (measured live)

    def test_a_forward_that_returns_a_task_is_not_an_open_tunnel(self):
        fake = types.ModuleType("ngrok")
        fake.forward = lambda port, authtoken, domain: object()  # an un-awaited asyncio Task has no url()
        with mock.patch.object(subprocess, "run", lambda argv, **kw: self.completed()), \
                mock.patch.dict(sys.modules, {"ngrok": fake}):
            with self.assertRaises(self.ns["Failure"]):
                self.io.open_tunnel(8080, TOKEN, DOMAIN)

    def test_an_error_from_forward_reaches_the_caller(self):
        fake = types.ModuleType("ngrok")

        def forward(port, authtoken, domain):
            raise RuntimeError("ERR_NGROK_334 the endpoint is already online")

        fake.forward = forward
        with mock.patch.object(subprocess, "run", lambda argv, **kw: self.completed()), \
                mock.patch.dict(sys.modules, {"ngrok": fake}):
            with self.assertRaises(RuntimeError):
                self.io.open_tunnel(8080, TOKEN, DOMAIN)

    def test_a_forward_that_outlasts_the_wait_is_a_failure_and_not_a_hang(self):
        release = threading.Event()
        self.addCleanup(release.set)  # lets the daemon thread finish once the test has seen its failure
        fake = types.ModuleType("ngrok")

        class Listener:
            def url(self):
                return "https://" + DOMAIN

        def forward(port, authtoken, domain):
            release.wait(10)  # longer than the wait the test allows: a join with no bound would wait this out
            return Listener()

        fake.forward = forward
        with mock.patch.object(subprocess, "run", lambda argv, **kw: self.completed()), \
                mock.patch.dict(sys.modules, {"ngrok": fake}), mock.patch.dict(self.ns, {"NGROK_S": 0.2}):
            with self.assertRaises(self.ns["Failure"]) as caught:
                self.io.open_tunnel(8080, TOKEN, DOMAIN)
        self.assertEqual(str(caught.exception), "ngrok did not return an open tunnel")

    def test_the_archive_download_runs_in_a_subprocess_with_the_time_limit(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            Path(argv[-1]).write_bytes(b"archive")  # the child writes the file
            return self.completed()

        with mock.patch.object(subprocess, "run", run):
            data = self.io.fetch("https://example.invalid/a.tar.gz", 123)
        self.assertEqual(data, b"archive")
        self.assertEqual(calls[0][1]["timeout"], 123)
        self.assertIn("https://example.invalid/a.tar.gz", calls[0][0])
        self.assertFalse((self.tmp / "llama.tar.gz").exists())  # removed afterwards

    def test_a_server_that_trickles_is_cut_at_the_limit_and_a_fast_one_is_read(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                if self.path == "/fast":
                    self.wfile.write(b"x" * 100000)
                    return
                try:
                    for _ in range(100000):
                        self.wfile.write(b"y")
                        self.wfile.flush()
                        threading.Event().wait(0.1)  # a byte every tenth of a second
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertEqual(len(self.io.fetch(base + "/fast", 30)), 100000)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.io.fetch(base + "/slow", 2)
        self.assertFalse((self.tmp / "llama.tar.gz").exists())

    def test_closing_the_tunnel_is_masked_too(self):
        fake = types.ModuleType("ngrok")

        def kill():
            os.write(1, f"native bye {TOKEN}\n".encode())
            print("py bye", TOKEN)

        fake.kill = kill
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"ngrok": fake}), fd_capture() as fds, contextlib.redirect_stdout(out):
            self.io.close_tunnel()
        self.assertNotIn(TOKEN, fds.text)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertIn("native bye", out.getvalue())

    def test_a_failed_setup_command_shows_a_masked_tail_cut_after_masking(self):
        text = KEY + "y" * 1980  # the key sits across the 2000-character cut
        with mock.patch.object(subprocess, "run", lambda *a, **kw: self.completed(2, "", text)):
            with self.assertRaises(self.ns["Failure"]) as ctx:
                self.io.run(["cmake", "--build"], 10)
        self.assertNotIn(KEY[-20:], str(ctx.exception))
        self.assertIn("exited 2", str(ctx.exception))

    def test_the_server_log_tail_is_masked_before_it_is_cut(self):
        (self.tmp / "server.log").write_text("z" * 5000 + KEY + "y" * 1980)
        tail = self.io.log_tail()
        self.assertNotIn(KEY[-20:], tail)
        self.assertNotIn(KEY[:20], tail)
        self.assertLessEqual(len(tail), 2000)
        self.assertEqual(self.ns["Io"](self.ns["SPEC"], root="/nope", tmp="/nope").log_tail(), "")

    def test_every_setup_step_has_a_time_limit(self):
        data = tarball([("llama.cpp-x/", b"", "dir"), ("llama.cpp-x/CMakeLists.txt", b"project()", "file")])
        self.io.spec["pins"] = dict(self.io.spec["pins"], llama_sha256=hashlib.sha256(data).hexdigest())
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return self.completed(0, "/tmp/model/f.gguf\n")

        self.io.fetch = lambda url, limit: data if "/archive/" + "a" * 40 + ".tar.gz" in url and limit > 0 else b""
        with mock.patch.object(subprocess, "run", run):
            self.io.engine()
            path = self.io.download("owner/repo", "f.gguf")
        self.assertEqual(path, "/tmp/model/f.gguf")
        self.assertTrue(calls)
        for argv, kw in calls:
            self.assertIsInstance(kw.get("timeout"), int, argv)
            self.assertGreater(kw["timeout"], 0)
        self.assertTrue((self.tmp / "llama.cpp" / "CMakeLists.txt").exists())
        download = calls[-1][0]
        self.assertEqual(download[-3:-1], ["f.gguf", "c" * 40])  # the revision is passed
        self.assertIn("huggingface_hub==0.30.2", calls[-2][0])

    def test_an_archive_that_does_not_match_its_pin_is_refused_before_anything_is_built(self):
        calls = []
        self.io.fetch = lambda url, limit: tarball([("x/f", b"data", "file")])
        with mock.patch.object(subprocess, "run", lambda *a, **kw: calls.append(a) or self.completed()):
            with self.assertRaisesRegex(self.ns["Failure"], "sha256"):
                self.io.engine()
        self.assertEqual(calls, [])
        self.assertFalse((self.tmp / "llama.cpp").exists())

    def test_an_archive_cannot_write_outside_its_directory_or_plant_a_link(self):
        evil = tarball([("top/../../escape.txt", b"x", "file")])
        with self.assertRaises(self.ns["Failure"]):
            self.ns["unpack"](evil, str(self.tmp / "a"))
        self.assertFalse((self.tmp / "escape.txt").exists())
        link = tarball([("top/ok.txt", b"ok", "file"), ("top/etc", b"", "link"), ("top/etc/passwd", b"x", "file")])
        dest = self.tmp / "b"
        dest.mkdir()
        try:
            self.ns["unpack"](link, str(dest))
        except self.ns["Failure"]:
            pass
        self.assertTrue((dest / "ok.txt").exists())
        self.assertFalse((dest / "etc").is_symlink())

    def test_a_cached_engine_is_copied_and_the_build_is_skipped(self):
        cached = self.root / "input" / "engine" / "llama-server"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"binary")
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no build")):
            path = self.io.engine()
        self.assertEqual(Path(path).read_bytes(), b"binary")
        self.assertTrue(os.access(path, os.X_OK))

    def pinned_archive(self):
        """A tiny llama.cpp archive that matches the test pin, and a fetch that hands it back."""
        data = tarball([("llama.cpp-x/", b"", "dir"), ("llama.cpp-x/CMakeLists.txt", b"project()", "file")])
        self.io.spec["pins"] = dict(self.io.spec["pins"], llama_sha256=hashlib.sha256(data).hexdigest())

        def fetch(url, limit):
            return data if "/archive/" + "a" * 40 + ".tar.gz" in url and limit > 0 else b""

        self.io.fetch = fetch

    def test_a_built_engine_is_saved_to_the_working_folder_and_says_so(self):
        # The kernel's working folder is its output: one harvested copy removes the build from every later cold start.
        self.pinned_archive()

        def run(argv, **kw):
            if argv[1] == "--build":
                built = Path(argv[2]) / "bin" / "llama-server"  # the build directory is argv[2]
                built.parent.mkdir(parents=True, exist_ok=True)
                built.write_bytes(b"built engine")
            return self.completed()

        out = io.StringIO()
        with mock.patch.object(subprocess, "run", run), contextlib.redirect_stdout(out):
            path = self.io.engine()
        self.assertEqual(Path(path).read_bytes(), b"built engine")  # the session still serves the build it made
        saved = self.root / "working" / "llama-server"
        self.assertEqual(saved.read_bytes(), b"built engine")
        self.assertEqual(out.getvalue().strip(), "saved engine to output")

    def test_a_cached_engine_is_not_built_and_saves_nothing(self):
        # The cached branch reads /kaggle/input: it builds nothing, so it has nothing to harvest and writes no output.
        cached = self.root / "input" / "engine" / "llama-server"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"harvested engine")
        out = io.StringIO()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no build")), contextlib.redirect_stdout(out):
            self.io.engine()
        self.assertFalse((self.root / "working").exists())
        self.assertEqual(out.getvalue(), "")

    def test_a_copy_that_fails_does_not_fail_a_session_with_an_engine(self):
        # Best effort: a session that built an engine must serve it even when the working folder cannot be written.
        self.pinned_archive()

        def run(argv, **kw):
            if argv[1] == "--build":
                built = Path(argv[2]) / "bin" / "llama-server"
                built.parent.mkdir(parents=True, exist_ok=True)
                built.write_bytes(b"built engine")
            return self.completed()

        self.root.mkdir(parents=True)
        (self.root / "working").write_bytes(b"a file where the folder must go")  # makedirs refuses
        out = io.StringIO()
        with mock.patch.object(subprocess, "run", run), contextlib.redirect_stdout(out):
            self.assertEqual(Path(self.io.engine()).read_bytes(), b"built engine")
        self.assertEqual(out.getvalue(), "")

    def test_read_secrets_wants_exactly_one_serve_file(self):
        with self.assertRaises(self.ns["Failure"]):
            self.io.read_secrets()
        first = self.root / "input" / "ds" / self.io.spec["serve_file"]
        first.parent.mkdir(parents=True)
        first.write_text(json.dumps({"endpoint_url": "https://" + DOMAIN, "endpoint_key": KEY, "ngrok_authtoken": TOKEN}))
        self.assertEqual(self.io.read_secrets(), (DOMAIN, KEY, TOKEN))
        second = self.root / "input" / "ds2" / self.io.spec["serve_file"]
        second.parent.mkdir()
        second.write_text(first.read_text())  # a valid second copy: the count is what must refuse it
        with self.assertRaises(self.ns["Failure"]):
            self.io.read_secrets()

    def test_the_server_process_sees_the_inherited_library_path_and_the_cuda_runtime(self):
        # Behavioural: the child process reads LD_LIBRARY_PATH itself, so the inherited value and the CUDA directory
        # must both be in what it gets (launch passes its environment; a bare Popen would lose the CUDA directory).
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/inherited/lib"}):
            proc = self.io.launch([sys.executable, "-c", "import os;print(os.environ['LD_LIBRARY_PATH'])"])
            self.assertEqual(proc.wait(timeout=30), 0)
        self.io.log.close()
        seen = (self.tmp / "server.log").read_text(encoding="utf-8")
        self.assertIn("/inherited/lib", seen)
        self.assertIn("/usr/local/cuda/lib64", seen)

    def test_launch_health_and_metrics_against_a_local_server(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["auth"] = self.headers.get("Authorization")
                if self.path == "/health":
                    code, body = 200, b"ok"
                elif self.path == "/metrics" and self.headers.get("Authorization") == f"Bearer {KEY}":
                    code, body = 200, TestMetrics.encode()
                else:
                    code, body = 401, b"no"
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        TestMetrics = ParseTest.METRICS
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.assertTrue(self.io.healthy(port))
        self.assertEqual(self.io.sample(port, KEY), (157, True))
        self.assertIsNone(self.io.sample(port, "wrong" * 5))
        free = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        dead = free.server_address[1]
        free.server_close()
        self.assertFalse(self.io.healthy(dead))
        self.assertIsNone(self.io.sample(dead, KEY))
        proc = self.io.launch([sys.executable, "-c", "print('server says hi')"])
        self.assertEqual(proc.wait(timeout=30), 0)
        self.io.log.close()
        self.assertIn("server says hi", self.io.log_tail())


class TunnelInNotebookTest(unittest.TestCase):
    def test_the_tunnel_is_opened_in_a_plain_thread_and_must_be_a_listener(self):
        # Measured live: in a notebook session ngrok.forward returns an un-awaited asyncio Task, not a listener.
        text = (Path(__file__).resolve().parent.parent / "kaggle" / "serve.ipynb.tmpl").read_text(encoding="utf-8")
        opening = text.split("def open_tunnel", 1)[1].split("def close_tunnel", 1)[0]
        self.assertIn("threading.Thread(target=open_it", opening)
        self.assertIn("ngrok did not return an open tunnel", opening)
        self.assertIn("import threading", text)


class LaunchEnvironmentTest(unittest.TestCase):
    def test_the_server_is_launched_with_the_cuda_runtime_on_the_library_path(self):
        # Measured live: libcudart.so.12 was not found in a notebook session's environment.
        text = (Path(__file__).resolve().parent.parent / "kaggle" / "serve.ipynb.tmpl").read_text(encoding="utf-8")
        launch = text.split("def launch", 1)[1].split("def log_tail", 1)[0]
        self.assertIn("/usr/local/cuda/lib64", launch)
        self.assertIn("dist-packages/nvidia/*/lib", launch)


if __name__ == "__main__":
    unittest.main()
