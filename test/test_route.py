import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fakecli
from engine import route as r

ROOT = Path(__file__).resolve().parent.parent
ALL_SECRETS = {name: "set" for names in r.DOOR_SECRETS.values() for name in names}


def entry(spec: str) -> r.Entry:
    return r.parse_entry(spec)


class ParseTest(unittest.TestCase):
    def test_parses_worker_door_model_and_effort(self):
        e = entry("pi:kaggle/qwen3.6-35b-a3b@high")
        self.assertEqual((e.worker, e.door, e.model, e.effort), ("pi", "kaggle", "qwen3.6-35b-a3b", "high"))
        self.assertEqual(e.spec, "pi:kaggle/qwen3.6-35b-a3b@high")
        self.assertEqual(e.vendor, "alibaba")

    def test_rejects_malformed_and_unknown_entries(self):
        for bad in ["pi:kaggle/", "pi kaggle/x", "nope:kaggle/x", "pi:nowhere/x", "claude:openai/gpt-x"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                r.parse_entry(bad)

    def test_vendor_comes_from_worker_or_model_id(self):
        self.assertEqual(entry("claude:anthropic/sonnet").vendor, "anthropic")
        self.assertEqual(entry("pi:openrouter/anthropic/claude-x").vendor, "anthropic")
        self.assertEqual(entry("pi:groq/openai/gpt-oss-120b").vendor, "openai")
        self.assertEqual(entry("pi:google/gemini-3.8-flash").vendor, "google")
        self.assertIsNone(entry("pi:openrouter/mystery").vendor)

    def test_omp_takes_its_vendor_from_the_model_id_like_pi(self):
        self.assertEqual(entry("omp:kaggle/qwen3.6-35b-a3b@high").vendor, "alibaba")
        self.assertEqual(entry("omp:openrouter/anthropic/claude-x").vendor, "anthropic")
        self.assertEqual(entry("omp:groq/openai/gpt-oss-120b").vendor, "openai")
        self.assertEqual(entry("omp:google/gemini-3.8-flash").vendor, "google")
        self.assertIsNone(entry("omp:openrouter/mystery").vendor)


class ChainTest(unittest.TestCase):
    def test_same_model_through_another_door_comes_before_a_different_model(self):
        ordered = r.chain([entry("pi:kaggle/A"), entry("pi:google/B"), entry("pi:openrouter/A")])
        self.assertEqual([e.spec for e in ordered], ["pi:kaggle/A", "pi:openrouter/A", "pi:google/B"])


class RouteTest(unittest.TestCase):
    def test_first_runnable_entry_wins_with_no_fallback(self):
        chain = r.chain([entry("pi:kaggle/A"), entry("pi:openrouter/A")])
        result = r.route(chain, r.make_probe(ALL_SECRETS))
        self.assertEqual((result.status, result.entry.spec, result.fell_back), ("ok", "pi:kaggle/A", ()))

    def test_door_fallback_tries_the_same_model_through_another_door_and_says_so(self):
        chain = r.chain([entry("pi:kaggle/A"), entry("pi:google/B"), entry("pi:openrouter/A")])
        probe = r.make_probe(ALL_SECRETS, down=frozenset({"pi:kaggle/A"}))
        result = r.route(chain, probe)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.entry.spec, "pi:openrouter/A")
        self.assertEqual(
            [f.text for f in result.fell_back],
            ["fell_back: pi:kaggle/A → pi:openrouter/A (endpoint down)"],
        )

    def test_every_skipped_entry_is_listed_in_order(self):
        env = {k: v for k, v in ALL_SECRETS.items() if k != "PIVOTAL_KAGGLE_KEY"}
        chain = [entry("pi:kaggle/A"), entry("pi:openrouter/B"), entry("pi:google/C")]
        probe = r.make_probe(env, limited=frozenset({"pi:openrouter/B"}))
        result = r.route(chain, probe)
        self.assertEqual(result.entry.spec, "pi:google/C")
        self.assertEqual(
            [f.text for f in result.fell_back],
            [
                "fell_back: pi:kaggle/A → pi:openrouter/B (missing secret)",
                "fell_back: pi:openrouter/B → pi:google/C (rate-limited)",
            ],
        )

    def test_no_runnable_entry_is_no_route_never_empty(self):
        chain = [entry("pi:kaggle/A"), entry("pi:google/B")]
        result = r.route(chain, r.make_probe({}))
        self.assertEqual(result.status, "no_route")
        self.assertIsNone(result.entry)
        self.assertEqual(result.fell_back[-1].target, "no_route")

    def test_first_build_entry_without_its_secret_is_red_not_a_silent_fallback(self):
        env = {k: v for k, v in ALL_SECRETS.items() if k != "PIVOTAL_KAGGLE_KEY"}
        chain = [entry("pi:kaggle/A"), entry("pi:google/B")]
        strict = r.route(chain, r.make_probe(env), strict_first=True)
        self.assertEqual(strict.status, "red")
        self.assertIn("PIVOTAL_KAGGLE_KEY", strict.reason)
        lenient = r.route(chain, r.make_probe(env), strict_first=False)
        self.assertEqual(lenient.status, "ok")
        self.assertEqual(lenient.entry.spec, "pi:google/B")

    def test_secret_is_checked_by_name_and_value(self):
        probe = r.make_probe({"GEMINI_API_KEY": ""})
        self.assertEqual(probe(entry("pi:google/B")), "missing secret")
        self.assertIsNone(r.make_probe({"GEMINI_API_KEY": "x"})(entry("pi:google/B")))


class QuotaRouteTest(unittest.TestCase):
    """A refused Kaggle push (models-up's `quota`) skips the kaggle entries for the run, as a reason of
    its own, and never the entries of another door."""

    def test_quota_is_a_skip_reason_of_the_router(self):
        self.assertIn("quota", r.SKIP_REASONS)

    def test_quota_skips_only_kaggle_entries_and_says_quota(self):
        probe = r.make_probe(ALL_SECRETS, quota=True)
        self.assertEqual(probe(entry("pi:kaggle/A")), "quota")
        self.assertIsNone(probe(entry("pi:openrouter/A")))

    def test_without_quota_nothing_changes(self):
        self.assertIsNone(r.make_probe(ALL_SECRETS)(entry("pi:kaggle/A")))

    def test_a_missing_key_is_still_the_reason_before_quota(self):
        env = {k: v for k, v in ALL_SECRETS.items() if k != "PIVOTAL_KAGGLE_KEY"}
        self.assertEqual(r.make_probe(env, quota=True)(entry("pi:kaggle/A")), "missing secret")

    def test_quota_falls_through_to_free_keys_with_a_visible_line(self):
        chain = r.chain([entry("pi:kaggle/A"), entry("pi:openrouter/A")])
        result = r.route(chain, r.make_probe(ALL_SECRETS, quota=True))
        self.assertEqual((result.status, result.entry.spec), ("ok", "pi:openrouter/A"))
        self.assertEqual([f.text for f in result.fell_back], ["fell_back: pi:kaggle/A → pi:openrouter/A (quota)"])

    def test_quota_on_a_first_build_entry_is_not_red(self):
        chain = [entry("pi:kaggle/A"), entry("pi:openrouter/A")]
        result = r.route(chain, r.make_probe(ALL_SECRETS, quota=True), strict_first=True)
        self.assertEqual(result.status, "ok", "red is for a missing secret only, never for a refused quota")


class OutcomeTest(unittest.TestCase):
    def test_outcome_line_names_the_result(self):
        ok = r.route([entry("pi:openrouter/A")], r.make_probe(ALL_SECRETS))
        self.assertEqual(ok.outcome, "ok: pi:openrouter/A")
        none = r.route([entry("pi:kaggle/A")], r.make_probe({}))
        self.assertEqual(none.outcome, "failed: no_route")
        red = r.route([entry("pi:kaggle/A")], r.make_probe({}), strict_first=True)
        self.assertEqual(red.outcome, "red: first entry pi:kaggle/A has no secret: set PIVOTAL_KAGGLE_KEY")


class ReviewTest(unittest.TestCase):
    PRESET = r.parse_preset(
        {
            "name": "test-mixed",
            "tiers": {
                "build": ["pi:kaggle/qwen3.6-35b-a3b@high"],
                "review": [
                    "pi:groq/openai/gpt-oss-120b",
                    "pi:openrouter/qwen/qwen-coder",
                    "pi:google/gemini-3.8-flash",
                ],
            },
        }
    )

    def test_review_excludes_the_builders_vendor(self):
        builder = self.PRESET.tiers["build"][0]
        specs = [e.spec for e in r.review_chain(self.PRESET, builder)]
        self.assertEqual(specs, ["pi:groq/openai/gpt-oss-120b", "pi:google/gemini-3.8-flash"])
        self.assertTrue(all(e.vendor != "alibaba" for e in r.review_chain(self.PRESET, builder)))

    def test_unknown_builder_vendor_gets_no_reviewer(self):
        self.assertEqual(r.review_chain(self.PRESET, entry("pi:openrouter/mystery")), [])

    def test_review_excludes_the_builder_vendor_across_workers_and_doors(self):
        preset = r.parse_preset(
            {
                "name": "cross-worker",
                "tiers": {
                    "build": ["claude:anthropic/sonnet"],
                    "review": ["pi:openrouter/anthropic/claude-x", "omp:openrouter/anthropic/claude-y"],
                },
            }
        )
        self.assertEqual(r.review_chain(preset, preset.tiers["build"][0]), [])
        self.assertEqual(
            r.cross_vendor_problems(preset),
            ["no review entry outside anthropic for claude:anthropic/sonnet"],
        )

    def test_omp_builder_is_excluded_from_its_own_vendor_in_review(self):
        preset = r.parse_preset(
            {
                "name": "omp-mixed",
                "tiers": {
                    "build": ["omp:openrouter/anthropic/claude-x"],
                    "review": ["omp:openrouter/anthropic/claude-y", "pi:groq/openai/gpt-oss-120b"],
                },
            }
        )
        specs = [e.spec for e in r.review_chain(preset, preset.tiers["build"][0])]
        self.assertEqual(specs, ["pi:groq/openai/gpt-oss-120b"])
        self.assertEqual(r.cross_vendor_problems(preset), [])

    def test_cross_vendor_check_passes_when_each_build_entry_has_another_vendor(self):
        self.assertEqual(r.cross_vendor_problems(self.PRESET), [])

    def test_cross_vendor_check_fails_when_reviewers_share_the_builders_vendor(self):
        same = r.parse_preset(
            {
                "name": "same-vendor",
                "tiers": {"build": ["pi:kaggle/qwen3.6-35b-a3b"], "review": ["pi:openrouter/qwen/qwen-coder"]},
            }
        )
        problems = r.cross_vendor_problems(same)
        self.assertEqual(len(problems), 1)
        self.assertIn("outside alibaba", problems[0])


class PresetTest(unittest.TestCase):
    def test_unknown_keys_and_tiers_are_rejected(self):
        base = {"name": "p", "tiers": {"build": ["pi:google/B"], "review": ["pi:groq/openai/gpt-oss-120b"]}}
        with self.assertRaises(ValueError):
            r.parse_preset({**base, "surprise": 1})
        with self.assertRaises(ValueError):
            r.parse_preset({**base, "tiers": {**base["tiers"], "deploy": ["pi:google/B"]}})
        with self.assertRaises(ValueError):
            r.parse_preset({"name": "p", "tiers": {"build": ["pi:google/B"]}})

    def test_load_preset_reads_a_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(
                '{"name": "p", "tiers": {"build": ["pi:google/B"], "review": ["pi:groq/openai/gpt-oss-120b"]}}',
                encoding="utf-8",
            )
            self.assertEqual(r.load_preset(path).name, "p")

    def test_a_preset_that_is_not_a_json_object_is_refused(self):
        for bad in ([], "free-kaggle", 3, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                r.parse_preset(bad)


class CliTest(unittest.TestCase):
    PRESET = json.dumps(
        {
            "name": "cli-test",
            "tiers": {
                "build": ["pi:kaggle/A", "pi:openrouter/A"],
                "review": ["pi:groq/openai/gpt-oss-120b", "pi:google/gemini-3.8-flash"],
            },
        }
    )

    def run_cli(self, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        # Only PATH and the names the test sets reach the router, so the runner's own keys never leak in.
        return subprocess.run(
            [sys.executable, "-m", "engine.route", *args],
            cwd=ROOT,
            env={"PATH": os.environ.get("PATH", ""), **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_first_build_secret_missing_is_red_and_names_the_secret(self):
        proc = self.run_cli(env={"PIVOTAL_PRESET_JSON": self.PRESET, "OPENROUTER_API_KEY": "set"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            "red: first entry pi:kaggle/A has no secret: set PIVOTAL_KAGGLE_KEY",
        )

    def test_skipped_review_entry_is_printed_before_the_outcome(self):
        env = {"PIVOTAL_PRESET_JSON": self.PRESET, "GEMINI_API_KEY": "set"}
        proc = self.run_cli("--tier", "review", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            [
                "fell_back: pi:groq/openai/gpt-oss-120b → pi:google/gemini-3.8-flash (missing secret)",
                "ok: pi:google/gemini-3.8-flash",
            ],
        )

    def test_no_runnable_entry_prints_failed_no_route(self):
        proc = self.run_cli("--tier", "review", env={"PIVOTAL_PRESET_JSON": self.PRESET})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            [
                "fell_back: pi:groq/openai/gpt-oss-120b → pi:google/gemini-3.8-flash (missing secret)",
                "fell_back: pi:google/gemini-3.8-flash → no_route (missing secret)",
                "failed: no_route",
            ],
        )

    def test_no_preset_at_all_is_a_failure_not_a_silent_default(self):
        proc = self.run_cli(env={})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("failed: no preset", proc.stdout)

    def test_review_with_a_builder_excludes_the_builders_vendor(self):
        preset = json.dumps(
            {
                "name": "builder-test",
                "tiers": {
                    "build": ["pi:kaggle/qwen3.6-35b-a3b@high"],
                    "review": ["pi:openrouter/qwen/qwen-coder", "pi:google/gemini-3.8-flash"],
                },
            }
        )
        env = {"PIVOTAL_PRESET_JSON": preset, "GEMINI_API_KEY": "set"}
        proc = self.run_cli("--tier", "review", "--builder", "pi:kaggle/qwen3.6-35b-a3b@high", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["ok: pi:google/gemini-3.8-flash"])

    def test_builder_only_applies_to_the_review_tier_and_needs_a_known_vendor(self):
        wrong_tier = self.run_cli("--builder", "pi:kaggle/A", env={"PIVOTAL_PRESET_JSON": self.PRESET})
        self.assertEqual(wrong_tier.returncode, 1, wrong_tier.stderr)
        self.assertEqual(wrong_tier.stdout.strip(), "failed: --builder only applies to --tier review")
        unknown = self.run_cli(
            "--tier", "review", "--builder", "pi:openrouter/mystery", env={"PIVOTAL_PRESET_JSON": self.PRESET}
        )
        self.assertEqual(unknown.returncode, 1, unknown.stderr)
        self.assertIn("failed: builder vendor unknown", unknown.stdout)

    def test_check_rejects_a_same_vendor_preset_and_names_the_entry(self):
        same = json.dumps(
            {
                "name": "same-vendor",
                "tiers": {
                    "build": ["pi:kaggle/qwen3.6-35b-a3b@high"],
                    "review": ["pi:openrouter/qwen/qwen-coder"],
                },
            }
        )
        proc = self.run_cli("--check", env={"PIVOTAL_PRESET_JSON": same})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            ["failed: no review entry outside alibaba for pi:kaggle/qwen3.6-35b-a3b@high"],
        )

    def test_check_passes_when_each_build_entry_has_a_cross_vendor_reviewer(self):
        ok = json.dumps(
            {
                "name": "cross-vendor",
                "tiers": {
                    "build": ["pi:kaggle/qwen3.6-35b-a3b@high"],
                    "review": ["pi:openrouter/qwen/qwen-coder", "pi:groq/openai/gpt-oss-120b"],
                },
            }
        )
        proc = self.run_cli("--check", env={"PIVOTAL_PRESET_JSON": ok})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["ok: every build entry has a review entry from another vendor"])

    def test_check_reports_every_build_entry_without_a_cross_vendor_reviewer(self):
        two = json.dumps(
            {
                "name": "two-builders",
                "tiers": {
                    "build": ["pi:kaggle/qwen3.6-35b-a3b", "pi:openrouter/mystery"],
                    "review": ["pi:openrouter/qwen/qwen-coder"],
                },
            }
        )
        proc = self.run_cli("--check", env={"PIVOTAL_PRESET_JSON": two})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            [
                "failed: no review entry outside alibaba for pi:kaggle/qwen3.6-35b-a3b",
                "failed: pi:openrouter/mystery: vendor unknown, so no reviewer can be shown to differ",
            ],
        )

    def test_a_429_in_a_test_log_is_classified_as_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "test.log"
            log.write_text("test_api ... FAIL\nAssertionError: got HTTP 429\n", encoding="utf-8")
            proc = self.run_cli("--gate-log", str(log), env={})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "work")

    def test_a_gate_log_that_cannot_be_read_fails(self):
        proc = self.run_cli("--gate-log", "/nonexistent/test.log", env={})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertTrue(proc.stdout.startswith("failed: bad log:"), proc.stdout)


class ClassifyTest(unittest.TestCase):
    def test_a_429_inside_a_test_log_is_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            bins = fakecli.FakeBin(Path(tmp) / "bin")
            bins.add(
                "check",
                stdout="test_api ... FAIL\n",
                stderr="AssertionError: got HTTP 429\n",
                code=1,
            )
            proc = bins.run("check")
        log = proc.stdout + proc.stderr
        self.assertIn("429", log)
        self.assertEqual(r.classify_failure("gate", log), r.WORK)

    def test_agent_text_is_a_skip_only_when_a_known_pattern_matches(self):
        pattern = ((re.compile(r"429"), "rate-limited"),)
        text = "Error: 429 too many requests"
        self.assertEqual(r.classify_failure("agent", text, pattern), "rate-limited")
        self.assertEqual(r.classify_failure("agent", text), r.AGENT_ERROR)

    def test_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            r.classify_failure("planner", "anything")

    def test_a_gate_log_is_work_even_when_an_agent_pattern_would_match(self):
        pattern = ((re.compile(r"429"), "rate-limited"),)
        self.assertEqual(r.classify_failure("gate", "HTTP 429", pattern), r.WORK)


class RateLimitRunTest(unittest.TestCase):
    # The CLI's rate-limit text is UNMEASURED (docs/engines.md), so these tests inject a pattern.
    # They prove the mechanism only: the text comes from a fake agent's output, not from a test log.
    PATTERN = ((re.compile(r"429"), "rate-limited"),)

    def test_rate_limit_text_from_the_agent_cli_skips_the_entry_for_the_rest_of_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            bins = fakecli.FakeBin(Path(tmp) / "bin")
            bins.add("agent", stderr="Error: 429 too many requests\n", code=1)
            proc = bins.run("agent")
        reason = r.classify_failure("agent", proc.stdout + proc.stderr, self.PATTERN)
        self.assertEqual(reason, "rate-limited")
        limited = r.note_failure(frozenset(), entry("pi:openrouter/A"), reason)
        result = r.route(
            [entry("pi:openrouter/A"), entry("pi:google/B")], r.make_probe(ALL_SECRETS, limited=limited)
        )
        self.assertEqual(result.entry.spec, "pi:google/B")
        self.assertEqual(
            [f.text for f in result.fell_back], ["fell_back: pi:openrouter/A → pi:google/B (rate-limited)"]
        )

    def test_only_a_rate_limit_is_remembered_for_the_run(self):
        self.assertEqual(r.note_failure(frozenset(), entry("pi:openrouter/A"), "missing secret"), frozenset())
        self.assertEqual(r.note_failure(frozenset(), entry("pi:openrouter/A"), "rate-limited"), {"pi:openrouter/A"})


class FellBackShapeTest(unittest.TestCase):
    """A fall-through is one structured item ({from, to, reason}) in every result file and verdict: the text line is only
    what a person reads, rendered from the item by `FellBack.text`, the one format every consumer prints."""

    ITEM = {"from": "pi:kaggle/A", "to": "pi:google/B", "reason": "endpoint down"}

    def test_an_item_renders_as_the_one_text_line_and_round_trips_as_a_dict(self):
        fell = r.FellBack.from_dict(self.ITEM)
        self.assertEqual(fell, r.FellBack("pi:kaggle/A", "pi:google/B", "endpoint down"))
        self.assertEqual(fell.as_dict(), self.ITEM)
        self.assertEqual(fell.text, "fell_back: pi:kaggle/A → pi:google/B (endpoint down)")

    def test_a_list_of_items_is_checked_and_returned_as_items(self):
        self.assertEqual(
            r.check_fell_back([self.ITEM, {**self.ITEM, "to": "no_route"}], 32),
            (r.FellBack("pi:kaggle/A", "pi:google/B", "endpoint down"), r.FellBack("pi:kaggle/A", "no_route", "endpoint down")),
        )
        self.assertEqual(r.check_fell_back([], 32), ())

    def test_a_string_line_or_an_unknown_key_or_reason_or_spec_is_refused(self):
        bad = [
            "fell_back: pi:kaggle/A → pi:google/B (endpoint down)",
            {**self.ITEM, "extra": "x"},
            {"from": "pi:kaggle/A", "to": "pi:google/B"},
            {**self.ITEM, "reason": "bored"},
            {**self.ITEM, "from": "none"},
            {**self.ITEM, "to": "pi:google/B" + "\n@everyone"},  # built at run time: no spec-shaped text with a newline in this file
            {**self.ITEM, "from": None},
        ]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                r.check_fell_back([item], 32)

    def test_the_list_has_a_limit_and_is_a_list(self):
        with self.assertRaises(ValueError):
            r.check_fell_back([self.ITEM] * 33, 32)
        for bad in ("x", {"from": "pi:kaggle/A"}, None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                r.check_fell_back(bad, 32)


class NotInstalledTest(unittest.TestCase):
    """A worker whose CLI the job cannot run is a skip with the reason `not installed`, checked after the secret and the quota
    and before any endpoint, and it falls through visibly (F5)."""

    def test_not_installed_is_one_of_the_skip_reasons_a_result_may_carry(self):
        self.assertIn("not installed", r.SKIP_REASONS)

    def test_the_probe_says_not_installed_for_an_absent_worker_only(self):
        probe = r.make_probe(ALL_SECRETS, absent=frozenset({"pi"}))
        self.assertEqual(probe(entry("pi:groq/qwen3-32b")), "not installed")
        self.assertIsNone(probe(entry("claude:anthropic/sonnet")))

    def test_the_secret_is_still_checked_first(self):
        probe = r.make_probe({}, absent=frozenset({"pi"}))
        self.assertEqual(probe(entry("pi:groq/qwen3-32b")), "missing secret")

    def test_an_absent_worker_falls_through_to_the_next_entry_and_the_fall_back_names_it(self):
        chain = [entry("pi:groq/qwen3-32b"), entry("claude:anthropic/sonnet")]
        result = r.route(chain, r.make_probe(ALL_SECRETS, absent=frozenset({"pi"})))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.entry.spec, "claude:anthropic/sonnet")
        self.assertEqual([f.as_dict() for f in result.fell_back],
                         [{"from": "pi:groq/qwen3-32b", "to": "claude:anthropic/sonnet", "reason": "not installed"}])
        self.assertEqual(result.fell_back[0].text, "fell_back: pi:groq/qwen3-32b → claude:anthropic/sonnet (not installed)")

    def test_a_result_that_names_not_installed_validates_and_a_made_up_reason_does_not(self):
        ok = [{"from": "pi:groq/qwen3-32b", "to": "claude:anthropic/sonnet", "reason": "not installed"}]
        self.assertEqual(len(r.check_fell_back(ok, 8)), 1)
        with self.assertRaises(ValueError):
            r.check_fell_back([{**ok[0], "reason": "not there"}], 8)


class HostedRunnerReviewTest(unittest.TestCase):
    """The free preset's review on a GitHub-hosted runner: omp is installed (engine/setup/pins.json), agy is not and no
    preset names it, the repo holds no Claude secret, and only PIVOTAL_KAGGLE_KEY and GEMINI_API_KEY are set. The review
    must reach Gemini through omp, the first entry it can run."""

    BUILDER = "pi:kaggle/qwen3.6-35b-a3b"

    def review(self, env):
        # `absent` is built from pins.json "unavailable" directly, not from workers.absent.
        preset = r.load_preset(ROOT / "presets" / "free-kaggle.json")
        unavailable = json.loads((ROOT / "engine" / "setup" / "pins.json").read_text(encoding="utf-8"))["unavailable"]
        chain = r.review_chain(preset, entry(self.BUILDER))
        absent = frozenset(e.worker for e in chain if e.worker in unavailable)
        return chain, r.route(chain, r.make_probe(env, absent=absent))

    def test_with_only_the_two_keys_the_review_routes_to_gemini_through_omp(self):
        env = {"PIVOTAL_KAGGLE_KEY": "set", "GEMINI_API_KEY": "set"}
        chain, result = self.review(env)
        self.assertEqual(result.outcome, "ok: omp:google/gemini-3.6-flash")
        self.assertEqual(result.entry.vendor, "google", "a different vendor from the alibaba builder")
        self.assertEqual([f.as_dict() for f in result.fell_back],
                         [{"from": "omp:groq/openai/gpt-oss-120b", "to": "omp:google/gemini-3.6-flash",
                           "reason": "missing secret"}])

    def test_without_the_gemini_key_no_entry_runs_and_every_skip_is_named(self):
        env = {"PIVOTAL_KAGGLE_KEY": "set"}
        chain, result = self.review(env)
        self.assertEqual(result.status, "no_route")
        self.assertEqual(result.outcome, "failed: no_route")
        self.assertEqual([f.as_dict() for f in result.fell_back], [
            {"from": "omp:groq/openai/gpt-oss-120b", "to": "omp:google/gemini-3.6-flash", "reason": "missing secret"},
            {"from": "omp:google/gemini-3.6-flash", "to": "omp:google/gemini-3-flash-preview", "reason": "missing secret"},
            {"from": "omp:google/gemini-3-flash-preview", "to": "omp:openrouter/google/gemini-3-flash-preview",
             "reason": "missing secret"},
            {"from": "omp:openrouter/google/gemini-3-flash-preview", "to": "no_route", "reason": "missing secret"},
        ])

    def test_no_review_entry_of_the_free_preset_is_agy_or_claude(self):
        preset = r.load_preset(ROOT / "presets" / "free-kaggle.json")
        self.assertEqual({e.worker for e in preset.tiers["review"]}, {"omp"})


if __name__ == "__main__":
    unittest.main()
