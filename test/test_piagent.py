"""engine/piagent.py: pi's agent directory for one call, and the rule that the Kaggle key never reaches a file (F3).

The key is a distinctive fake value; every test scans what it can see (the directory, the file modes, the documents) for it.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import nolive  # noqa: F401  (no test sweeps the live /proc)
from engine import modelsup, piagent, route
from engine.job import JobError
from test_modelsup import KEY as ENDPOINT_KEY
from test_modelsup import URL as ENDPOINT_URL

ROOT = Path(__file__).resolve().parent.parent
CATALOG = json.loads((ROOT / "kaggle" / "catalog.json").read_text(encoding="utf-8"))
KAGGLE = route.parse_entry("pi:kaggle/qwen3.6-35b-a3b")
ENV = {"PIVOTAL_KAGGLE_URL": ENDPOINT_URL, "PIVOTAL_KAGGLE_KEY": ENDPOINT_KEY}


def domain_of(url: str) -> str:
    return modelsup.read_config({"PIVOTAL_KAGGLE_URL": url}, need_kaggle=False).domain


class ModelsConfigTest(unittest.TestCase):
    def setUp(self):
        self.doc = piagent.models_config("example.ngrok-free.app", "qwen3.6-35b-a3b", 4096)
        self.provider = self.doc["providers"]["kaggle"]

    def test_the_key_is_named_and_never_written_as_a_value(self):
        self.assertEqual(self.provider["apiKey"], "$PIVOTAL_KAGGLE_KEY")
        self.assertEqual(piagent.KEY_REF, "$PIVOTAL_KAGGLE_KEY")

    def test_the_endpoint_is_the_domain_under_v1_over_the_openai_completions_api(self):
        self.assertEqual(self.provider["baseUrl"], "https://example.ngrok-free.app/v1")
        self.assertEqual(self.provider["api"], "openai-completions")

    def test_the_ngrok_header_skips_the_browser_warning(self):
        self.assertEqual(self.provider["headers"], {"ngrok-skip-browser-warning": "1"})

    def test_the_one_model_carries_the_served_context_and_half_of_it_as_the_output_cap(self):
        self.assertEqual(self.provider["models"], [{
            "id": "qwen3.6-35b-a3b", "name": "qwen3.6-35b-a3b", "contextWindow": 4096, "maxTokens": 2048, "reasoning": False,
        }])

    def test_the_context_window_is_the_catalog_serve_ctx_for_every_candidate_and_the_output_cap_is_half(self):
        for row in CATALOG["single_model_both_t4"]:
            model = row["sysop_model_id"]
            spec = piagent.models_config("example.ngrok-free.app", model, piagent.served_context(CATALOG, model))
            entry = spec["providers"]["kaggle"]["models"][0]
            self.assertEqual(entry["contextWindow"], row["serve"]["ctx"], model)
            self.assertEqual(entry["maxTokens"], row["serve"]["ctx"] // 2, model)
            self.assertTrue(0 < entry["maxTokens"] < entry["contextWindow"], model)

    def test_the_served_context_comes_from_the_catalog_and_an_unknown_model_is_refused(self):
        self.assertEqual(piagent.served_context(CATALOG, "qwen3.6-35b-a3b"), 32768)
        with self.assertRaises(JobError):
            piagent.served_context(CATALOG, "no-such-model")
        with self.assertRaises(JobError):
            piagent.served_context({"single": [{"sysop_model_id": "m", "serve": {"ctx": 0}}]}, "m")
        with self.assertRaises(JobError):
            piagent.served_context({"single": [{"sysop_model_id": "m", "serve": {"ctx": True}}]}, "m")

    def test_the_real_catalog_is_read_and_is_a_dict(self):
        self.assertIsInstance(piagent.read_catalog(ROOT), dict)
        self.assertIsNone(piagent.read_catalog(Path("/nonexistent-pivotal-root")))


class CallDirTest(unittest.TestCase):
    def secret_text(self, directory: Path) -> str:
        """Every byte under the directory, as text: what a scan for the key sees."""
        parts = []
        for path in directory.rglob("*"):
            if path.is_file():
                parts.append(path.read_bytes().decode("utf-8", "replace"))
        return "\n".join(parts)

    def test_a_kaggle_call_gets_a_fresh_0700_directory_with_a_0600_models_file_and_no_key(self):
        with piagent.call_dir(KAGGLE, ENV, CATALOG) as directory:
            self.assertIsNotNone(directory)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
            models = directory / "models.json"
            self.assertEqual(stat.S_IMODE(os.stat(models).st_mode), 0o600)
            document = json.loads(models.read_text(encoding="utf-8"))
            self.assertEqual(document["providers"]["kaggle"]["baseUrl"], f"https://{domain_of(ENDPOINT_URL)}/v1")
            self.assertNotIn(ENDPOINT_KEY, self.secret_text(directory), "the key value is in no file")
            self.assertEqual(sorted(p.name for p in directory.iterdir()), ["models.json", "settings.json"])

    def test_the_directory_is_removed_when_the_call_ends_and_on_a_failure(self):
        with piagent.call_dir(KAGGLE, ENV, CATALOG) as directory:
            kept = directory
        self.assertFalse(kept.exists())
        with self.assertRaises(RuntimeError):
            with piagent.call_dir(KAGGLE, ENV, CATALOG) as directory:
                kept = directory
                raise RuntimeError("the agent failed")
        self.assertFalse(kept.exists())

    def test_a_pi_call_on_another_door_gets_only_the_settings_file(self):
        other = route.parse_entry("pi:openai/gpt-x")
        with piagent.call_dir(other, ENV, CATALOG) as directory:
            self.assertEqual(sorted(p.name for p in directory.iterdir()), ["settings.json"])

    def test_every_pi_call_refuses_repo_project_resources_without_asking(self):
        # pi reads `defaultProjectTrust` from the agent directory only: "never" skips a repo's .pi/ resources in print mode.
        for entry in (KAGGLE, route.parse_entry("pi:openai/gpt-x")):
            with self.subTest(entry=entry.spec), piagent.call_dir(entry, ENV, CATALOG) as directory:
                document = json.loads((directory / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(document, {"defaultProjectTrust": "never"})
                self.assertEqual(stat.S_IMODE(os.stat(directory / "settings.json").st_mode), 0o600)

    def test_each_pi_call_gets_its_own_directory_and_none_is_shared(self):
        seen = []
        for _ in range(2):
            with piagent.call_dir(KAGGLE, ENV, CATALOG) as directory:
                seen.append(directory)
                self.assertTrue(directory.is_dir())
        self.assertEqual(len(set(seen)), 2, "two calls, two directories")
        self.assertFalse(any(d.exists() for d in seen), "each is removed when its call ends")

    def test_a_planted_link_is_never_followed_when_a_config_file_is_written(self):
        # The write refuses an existing name (O_EXCL) and never follows a link at it (O_NOFOLLOW): the target is untouched.
        # O_NOFOLLOW is belt and braces: O_EXCL already refuses a link, since it is an existing name. The test pins both.
        with tempfile.TemporaryDirectory(prefix="piagent-test-") as tmp:
            outside = Path(tmp) / "outside.txt"
            outside.write_text("untouched\n", encoding="utf-8")
            link = Path(tmp) / "models.json"
            os.symlink(outside, link)
            with self.assertRaises(OSError):
                piagent._write_new(link, b"planted\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched\n")

    def test_a_worker_that_is_not_pi_gets_no_directory(self):
        with piagent.call_dir(route.parse_entry("claude:anthropic/sonnet"), ENV, CATALOG) as directory:
            self.assertIsNone(directory)

    def test_a_kaggle_call_without_an_endpoint_or_a_catalog_is_refused_before_anything_is_written(self):
        with self.assertRaises(JobError):
            with piagent.call_dir(KAGGLE, {"PIVOTAL_KAGGLE_KEY": ENDPOINT_KEY}, CATALOG):
                pass
        with self.assertRaises(JobError):
            with piagent.call_dir(KAGGLE, ENV, None):
                pass

    def test_the_directory_is_outside_the_working_tree_and_under_the_temp_root(self):
        with piagent.call_dir(KAGGLE, ENV, CATALOG) as directory:
            self.assertFalse(str(directory).startswith(str(ROOT)))
            self.assertTrue(directory.name.startswith("pivotal-pi-"))


if __name__ == "__main__":
    unittest.main()
