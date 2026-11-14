import json
import pathlib
import unittest

CATALOG = pathlib.Path(__file__).resolve().parent.parent / "kaggle" / "catalog.json"
PINS = CATALOG.with_name("pins.json")
MEASURED_FIELDS = ("model", "quant", "context", "vram_gb", "tok_s", "time_to_healthy_s", "vendor")


def entries(catalog):
    yield from catalog["single_model_both_t4"]
    for entry in catalog["two_small_models"]:
        yield entry


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads(CATALOG.read_text())

    def test_has_two_to_three_single_model_candidates_and_one_pair(self):
        self.assertIn(len(self.catalog["single_model_both_t4"]), (2, 3))
        self.assertEqual(len(self.catalog["two_small_models"]), 1)

    def test_every_entry_says_whether_it_was_measured(self):
        for entry in entries(self.catalog):
            self.assertIn(entry["status"], ("unmeasured", "partial", "measured"), entry["candidate"])

    def test_unmeasured_entries_hold_no_numbers(self):
        for entry in entries(self.catalog):
            if entry["status"] != "unmeasured":
                continue
            self.assertIsNone(entry["measured_on"], entry["candidate"])
            self.assertIsNone(entry["time_to_healthy_s"], entry["candidate"])
            models = entry.get("models", [entry])
            for model in models:
                for field in ("model", "quant", "context", "vram_gb", "tok_s"):
                    self.assertIsNone(model[field], f"{entry['candidate']}.{field}")

    def test_partial_entries_are_timed_and_never_pass_off_a_sysop_speed_as_a_probe_speed(self):
        for entry in entries(self.catalog):
            if entry["status"] != "partial":
                continue
            self.assertIsNotNone(entry["measured_on"], entry["candidate"])
            self.assertIsNotNone(entry["time_to_healthy_s"], entry["candidate"])
            self.assertIsNone(entry["tok_s"], entry["candidate"])
            self.assertGreater(entry["tok_s_sysop"], 0, entry["candidate"])
            self.assertIn("not by spikes/kaggle_probe.py", entry["tok_s_source"], entry["candidate"])

    def test_second_session_answer_is_dated_or_unmeasured(self):
        second = self.catalog["second_session"]
        self.assertIn(second["status"], ("unmeasured", "measured"))
        if second["status"] == "measured":
            self.assertTrue(second["measured_on"] and second["answer"])

    def test_measured_entries_carry_every_field(self):
        for entry in entries(self.catalog):
            if entry["status"] != "measured":
                continue
            self.assertIsNotNone(entry["measured_on"], entry["candidate"])
            self.assertIsNotNone(entry["time_to_healthy_s"], entry["candidate"])
            for model in entry.get("models", [entry]):
                for field in MEASURED_FIELDS:
                    if field in model:
                        self.assertIsNotNone(model[field], f"{entry['candidate']}.{field}")
                self.assertGreater(model["tok_s"], 0, entry["candidate"])

    def test_every_rendered_candidate_carries_the_serve_configuration_it_was_measured_with(self):
        for entry in self.catalog["single_model_both_t4"]:
            if entry["status"] == "unmeasured":
                continue
            serve = entry["serve"]
            self.assertEqual(set(serve), {"slots", "ctx", "flags", "source"}, entry["candidate"])
            self.assertIsInstance(serve["slots"], int, entry["candidate"])
            self.assertIsInstance(serve["ctx"], int, entry["candidate"])
            self.assertTrue(serve["flags"] and all(isinstance(f, str) for f in serve["flags"]), entry["candidate"])
            self.assertIn("models.json", serve["source"], entry["candidate"])  # where the flags come from
            for flag in ("-ngl", "--split-mode", "--flash-attn", "-ctk", "-ctv"):
                self.assertIn(flag, serve["flags"], entry["candidate"])

    def test_every_served_context_is_one_an_agent_can_work_in(self):
        # pi's own system prompt is about 1600 tokens, and 4096 per slot refused the first live agent request (2026-11-11).
        for entry in self.catalog["single_model_both_t4"]:
            if entry["status"] == "unmeasured":
                continue
            self.assertGreaterEqual(entry["serve"]["ctx"], 16384, entry["candidate"])

    def test_the_35b_serves_the_agent_config_sysop_ran_and_each_served_config_says_what_is_unmeasured(self):
        for entry in self.catalog["single_model_both_t4"]:
            if entry["status"] == "unmeasured":
                continue
            source = entry["serve"]["source"]
            # time to healthy was taken with sysop's 2 x 32768 config (SERVE_SLOTS 2, SERVE_CTX 32768), for all three models
            self.assertIn("SERVE_CTX 32768", source, entry["candidate"])
            # tok/s was taken with the 8 x 4096 batch config, and that is named, not hidden
            self.assertIn("8 slots of 4096", source, entry["candidate"])
            self.assertIn("UNMEASURED", source, entry["candidate"])  # the served config has no speed in this repo
            if entry["sysop_model_id"] != "qwen3.6-35b-a3b":
                self.assertIn("no run of its own", source, entry["candidate"])  # 2 x 16384 is a pick, not a measurement
        qwen = next(e for e in self.catalog["single_model_both_t4"] if e["sysop_model_id"] == "qwen3.6-35b-a3b")
        self.assertEqual((qwen["serve"]["slots"], qwen["serve"]["ctx"]), (2, 32768))

    def test_the_pins_file_names_every_pin_and_a_model_revision_per_candidate(self):
        pins = json.loads(PINS.read_text())
        self.assertEqual(set(pins), {"schema", "note", "fetched", "confirmed_by_live_run", "ngrok", "huggingface_hub",
                                     "llama_cpp", "model_revisions"})
        self.assertEqual(set(pins["llama_cpp"]), {"tag", "commit", "sha256"})
        ids = {e["sysop_model_id"] for e in self.catalog["single_model_both_t4"]}
        self.assertEqual(set(pins["model_revisions"]), ids)

    def test_the_shipped_pins_say_plainly_they_are_current_on_a_date_and_not_confirmed_live(self):
        pins = json.loads(PINS.read_text())
        self.assertEqual(pins["fetched"], "2026-11-01")
        self.assertIs(pins["confirmed_by_live_run"], False)
        for words in ("CURRENT AS OF 2026-11-01", "NOT YET CONFIRMED BY A LIVE NOTEBOOK RUN", "models-up"):
            self.assertIn(words, pins["note"])

    def test_tok_s_cites_the_reference_models_json_and_its_dates_not_the_session_that_timed_the_start(self):
        for entry in self.catalog["single_model_both_t4"]:
            if entry["status"] == "unmeasured":
                continue
            who = entry["candidate"]
            self.assertEqual(entry["tok_s_measured_on"], "2026-10-06", who)  # sysop's models.json source line says so
            self.assertIn("models.json", entry["tok_s_source"], who)
            self.assertIn("2026-10-06", entry["tok_s_source"], who)
            self.assertNotIn("measured by sysop serve", entry["tok_s_source"], who)
            self.assertNotIn(f"measured by sysop serve (sysop.py serve up/list) on {entry['measured_on']}", entry["tok_s_source"], who)
            self.assertEqual(entry["measured_on"], "2026-11-01", who)  # time to healthy only
            self.assertIn(entry["measured_on"], entry["time_to_healthy_source"], who)

    def test_the_docs_date_the_tok_s_figures_to_models_json_and_keep_the_live_checks(self):
        text = (CATALOG.parent.parent / "docs" / "engines.md").read_text()
        self.assertIn("**tok/s was not measured on 2026-11-01.**", text)
        self.assertIn("2026-10-06", text)
        self.assertNotIn("tok/s is sysop's own recorded figure from `serve list`", text)
        self.assertIn("each of those five routes must return 401", text)

    def test_the_docs_say_which_config_each_timing_was_taken_with(self):
        # The two sentences of docs/engines.md that name the config behind each figure. The catalog's source lines say the
        # same; this pins the reader's document, which a revert to the old 8 x 4096 wording would otherwise leave unchecked.
        text = (CATALOG.parent.parent / "docs" / "engines.md").read_text()
        self.assertIn("The time to healthy figures were taken with sysop's 2 slots of 32768 config (`SERVE_SLOTS` 2, `SERVE_CTX` 32768), "
                      "for all three models.", text)
        self.assertIn("The tok/s figures were taken with sysop's 8 slots of 4096 throughput benchmark config, which refused any request "
                      "over 4096 tokens", text)

    def test_the_changelog_does_not_describe_two_slots_or_a_catalog_without_serve_settings(self):
        text = (CATALOG.parent.parent / "CHANGELOG.md").read_text()
        self.assertNotIn("--parallel 2", text)
        self.assertNotIn("never in the catalog", text)
        self.assertNotIn("slots (2)", text)


if __name__ == "__main__":
    unittest.main()
