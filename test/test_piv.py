"""piv init (F3b): every write goes through gh, and gh is a fake here.

The fake gh (test/fakegh.py) logs each call, so a test can check what was sent and where, not only
what came back. No test reaches GitHub: PIV_GH always names the fake.
"""

import base64
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fakegh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))

from piv import cli, init  # noqa: E402
from engine import trust  # noqa: E402

CALLER = (ROOT / "templates" / "caller.yml").read_text(encoding="utf-8")
SHA = "1234567890abcdef1234567890abcdef12345678"
ZEROS = "0" * 40
REPO = "0xctx/piv-sandbox"
WORKFLOW = f"repos/{REPO}/contents/.github/workflows/pivotal.yml"
PERMISSIONS = f"repos/{REPO}/actions/permissions/workflow"
LABELS = {
    "pivotal", "pivotal:building", "pivotal:ready", "pivotal:landed", "pivotal:blocked",
    "pivotal:build", "pivotal:incident", "pivotal:mirror", "pivotal:docs",
}
VARIABLES = init.VARIABLES  # every variable Pivotal uses, so each read below has an answer


def variable_now(value: str) -> dict:
    return fakegh.ok(json.dumps({"value": value}))


def assert_value_on_stdin(test: unittest.TestCase, call: dict) -> None:
    """A value goes to gh on stdin: no ``--body`` or ``-b`` flag, since gh reads the value from stdin then."""
    for arg in call["args"]:
        test.assertFalse(arg.startswith("--body") or arg == "-b", f"{call['args']} passes a value as a flag")
    test.assertNotIn("--body-file", call["args"])


def permission(*, can_open_prs: bool) -> str:
    return json.dumps(
        {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": can_open_prs}
    )


class PivTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.gh = fakegh.FakeGh(self.root / "bin")
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    def piv(self, *args: str, env: dict[str, str] | None = None) -> int:
        return cli.main(["init", *args], self.gh.env() if env is None else env, self.out, self.err)

    def happy_path(self, *, existing: str | None = None, permissions=(False, True), replace=None):
        """Routes for a clean run: the latest release resolves to SHA, and every write succeeds.

        ``permissions`` gives the Actions setting's answers in order (the last one repeats), and
        ``replace`` maps an exact needle to the answers that replace its default, so one step can
        fail without a second copy of the others.
        """
        if existing is None:
            caller_now = fakegh.not_found()
        else:
            blob = {"sha": "b10b", "content": base64.b64encode(existing.encode()).decode()}
            caller_now = fakegh.ok(json.dumps(blob))
        answers = {
            f"api repos/{init.ENGINE_REPO}/releases/latest": [fakegh.ok(json.dumps({"tag_name": "v1.0.0"}))],
            f"api repos/{init.ENGINE_REPO}/commits/v1.0.0": [fakegh.ok(json.dumps({"sha": SHA}))],
            f"templates/caller.yml?ref={SHA}": [fakegh.ok(CALLER)],
            f"api {WORKFLOW}": [caller_now],
            f"api -X PUT {WORKFLOW}": [fakegh.ok("{}")],
            f"api -X PUT {PERMISSIONS}": [fakegh.ok("{}")],
            "variable set": [fakegh.ok()],
            "secret set": [fakegh.ok()],
            "label create": [fakegh.ok()],
            "repo edit": [fakegh.ok()],
            f"api repos/{REPO}/actions/permissions/workflow": [
                fakegh.ok(permission(can_open_prs=value)) for value in permissions
            ],
            **{f"api repos/{REPO}/actions/variables/{name}": [fakegh.not_found()] for name in VARIABLES},
        }
        answers.update(replace or {})
        for needle, values in answers.items():
            self.gh.route(needle, *values)

    def calls(self, *words: str) -> list[dict]:
        """The calls whose argv begins with ``words``."""
        return [c for c in self.gh.calls() if c["args"][: len(words)] == list(words)]

    def caller_put(self) -> dict:
        puts = [c for c in self.gh.calls() if c["args"][:3] == ["api", "-X", "PUT"] and WORKFLOW in c["args"][3]]
        self.assertEqual(len(puts), 1, "expected exactly one write of the caller file")
        return json.loads(puts[0]["stdin"])


class InitWritesTest(PivTest):
    def test_writes_the_caller_pinned_to_the_release_with_no_placeholder_left(self):
        self.happy_path()
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        body = self.caller_put()
        written = base64.b64decode(body["content"]).decode("utf-8")
        expected = CALLER.replace(init.ENGINE_PLACEHOLDER, init.ENGINE_REPO).replace(ZEROS, SHA)
        self.assertEqual(written, expected)
        self.assertNotIn(ZEROS, written)
        self.assertNotIn(init.ENGINE_PLACEHOLDER, written)
        self.assertNotIn("sha", body)  # a new file carries no blob sha

    def test_an_existing_caller_is_updated_by_its_blob_sha_when_update_is_given(self):
        self.happy_path(existing="old: caller\n")
        self.assertEqual(self.piv(REPO, "--check", "npm test", "--update"), 0, self.err.getvalue())
        self.assertEqual(self.caller_put()["sha"], "b10b")

    def test_an_identical_caller_is_not_rewritten(self):
        existing = CALLER.replace(init.ENGINE_PLACEHOLDER, init.ENGINE_REPO).replace(ZEROS, SHA)
        self.happy_path(existing=existing)
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertEqual(self.calls("api", "-X", "PUT", WORKFLOW), [])
        self.assertIn("already current", self.out.getvalue())

    def test_sets_the_labels_the_plan_names_with_force(self):
        self.happy_path()
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        created = self.calls("label", "create")
        self.assertEqual({c["args"][2] for c in created}, LABELS)
        for call in created:
            self.assertIn("--force", call["args"])
            self.assertEqual(call["args"][call["args"].index("--repo") + 1], REPO)

    def test_the_untrusted_preset_is_named_as_not_set_because_init_has_no_value_for_it(self):
        self.happy_path()
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertIn("PIVOTAL_UNTRUSTED_PRESET: not set", self.out.getvalue())
        self.assertNotIn("PIVOTAL_UNTRUSTED_PRESET: was", self.out.getvalue())
        self.assertEqual([c for c in self.calls("variable", "set") if c["args"][2] == "PIVOTAL_UNTRUSTED_PRESET"], [])

    def test_adds_the_pivotal_topic(self):
        self.happy_path()
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertEqual(self.calls("repo", "edit")[0]["args"], ["repo", "edit", REPO, "--add-topic", "pivotal"])

    def test_sets_check_setup_start_preset_and_level_as_variables_read_from_stdin(self):
        self.happy_path()
        args = ["--check", "npm test", "--setup", "npm ci", "--start", "npm start",
                "--preset", "claude-sub", "--level", "ask"]
        self.assertEqual(self.piv(REPO, *args), 0, self.err.getvalue())
        values = {c["args"][2]: c["stdin"] for c in self.calls("variable", "set")}
        self.assertEqual(values, {
            "PIVOTAL_CHECK": "npm test",
            "PIVOTAL_SETUP": "npm ci",
            "PIVOTAL_START": "npm start",
            "PIVOTAL_PRESET": "claude-sub",
            "PIVOTAL_LEVEL": "ask",
            "PIVOTAL_PRIOR_CAN_APPROVE_PRS": "false",  # the value init replaced, kept for piv remove
        })
        for call in self.calls("variable", "set"):
            assert_value_on_stdin(self, call)

    def test_level_defaults_to_prepare_and_unset_optional_inputs_are_not_written(self):
        self.happy_path()
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        values = {c["args"][2]: c["stdin"] for c in self.calls("variable", "set")}
        self.assertEqual(values, {
            "PIVOTAL_CHECK": "npm test", "PIVOTAL_PRESET": "one-key", "PIVOTAL_LEVEL": "prepare",
            "PIVOTAL_PRIOR_CAN_APPROVE_PRS": "false",
        })

    def test_a_failed_step_stops_the_run_and_leaves_the_caller_unwritten(self):
        self.happy_path(replace={"label create": [fakegh.fail("gh: HTTP 500\n")]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 1)
        self.assertIn("label", self.err.getvalue())
        self.assertEqual(self.calls("api", "-X", "PUT"), [])
        self.assertEqual(self.calls("api", PERMISSIONS), [])


class SecretsTest(PivTest):
    def test_sets_only_declared_secrets_that_are_present_and_non_empty(self):
        self.happy_path()
        env = self.gh.env(
            CLAUDE_CODE_OAUTH_TOKEN="claude-token-value",
            PIVOTAL_KAGGLE_KEY="kaggle-key-value",
            OPENAI_API_KEY="",
            NOT_DECLARED_TOKEN="unrelated-value",
        )
        self.assertEqual(self.piv(REPO, "--check", "npm test", env=env), 0, self.err.getvalue())
        set_calls = self.calls("secret", "set")
        values = {c["args"][2]: c["stdin"] for c in set_calls}
        self.assertEqual(values, {
            "CLAUDE_CODE_OAUTH_TOKEN": "claude-token-value",
            "PIVOTAL_KAGGLE_KEY": "kaggle-key-value",
        })
        self.assertIn("OPENAI_API_KEY", self.out.getvalue())  # named as skipped, never valued

    def test_a_secret_value_is_never_on_a_command_line(self):
        self.happy_path()
        env = self.gh.env(CLAUDE_CODE_OAUTH_TOKEN="claude-token-value")
        self.assertEqual(self.piv(REPO, "--check", "npm test", env=env), 0, self.err.getvalue())
        for call in self.gh.calls():
            self.assertNotIn("claude-token-value", " ".join(call["args"]))

    def test_a_secret_goes_in_on_stdin_with_no_body_flag(self):
        self.happy_path()
        env = self.gh.env(CLAUDE_CODE_OAUTH_TOKEN="claude-token-value")
        self.assertEqual(self.piv(REPO, "--check", "npm test", env=env), 0, self.err.getvalue())
        sets = self.calls("secret", "set")
        self.assertEqual(len(sets), 1)
        assert_value_on_stdin(self, sets[0])
        self.assertEqual(sets[0]["stdin"], "claude-token-value")

    def test_no_output_carries_a_secret_value_even_when_gh_echoes_it(self):
        secret = "SUPER-SECRET-TOKEN-7731"
        self.happy_path(replace={"secret set": [fakegh.fail(f"gh: bad request, got {secret}\n")]})
        env = self.gh.env(CLAUDE_CODE_OAUTH_TOKEN=secret)
        self.assertEqual(self.piv(REPO, "--check", "npm test", env=env), 1)
        self.assertNotIn(secret, self.out.getvalue())
        self.assertNotIn(secret, self.err.getvalue())
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", self.err.getvalue())

    def test_success_output_carries_no_secret_value(self):
        self.happy_path()
        secret = "SUPER-SECRET-TOKEN-4242"
        env = self.gh.env(CLAUDE_CODE_OAUTH_TOKEN=secret, KAGGLE_KEY=secret)
        self.assertEqual(self.piv(REPO, "--check", "npm test", env=env), 0, self.err.getvalue())
        self.assertNotIn(secret, self.out.getvalue())
        self.assertNotIn(secret, self.err.getvalue())

    def test_gh_runs_without_the_pivotal_secrets_in_its_environment(self):
        env = {"PATH": "/usr/bin", "GH_TOKEN": "a-token-gh-needs", "KAGGLE_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "c"}
        scrubbed = init.gh_environment(env)
        self.assertEqual(scrubbed, {"PATH": "/usr/bin", "GH_TOKEN": "a-token-gh-needs"})

    def test_a_secret_that_is_not_utf8_text_is_named_and_not_sent_and_its_position_is_not_given(self):
        self.happy_path()
        env = self.gh.env(CLAUDE_CODE_OAUTH_TOKEN="ab\udcffcd")
        self.assertEqual(self.piv(REPO, "--check", "npm test", env=env), 1, self.err.getvalue())
        message = self.err.getvalue()
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", message)
        self.assertNotIn("position", message)
        self.assertNotIn("\udcff", message)
        self.assertEqual(self.calls("secret", "set"), [], "gh is not run with a value it cannot take")

    def test_the_secret_names_match_the_caller_and_nothing_else(self):
        declared = set(re.findall(r"secrets\.([A-Z][A-Z0-9_]*)", CALLER))
        self.assertEqual(set(init.SECRETS), declared)


class ActionsPermissionTest(PivTest):
    def test_reads_sets_and_reads_back_the_pr_setting(self):
        self.happy_path(permissions=(False, True))
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        puts = self.calls("api", "-X", "PUT", PERMISSIONS)
        self.assertEqual(len(puts), 1)
        self.assertEqual(json.loads(puts[0]["stdin"]),
                         {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True})
        self.assertEqual(len(self.calls("api", PERMISSIONS)), 2)  # before, and read back

    def test_already_allowed_is_read_back_and_not_written(self):
        self.happy_path(permissions=(True,))
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertEqual(self.calls("api", "-X", "PUT", PERMISSIONS), [])
        self.assertIn("can open pull requests", self.out.getvalue())

    def test_the_value_replaced_is_kept_in_a_variable_before_the_setting_is_written(self):
        self.happy_path(permissions=(False, True))
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        names = [c["args"][2] if c["args"][0] == "variable" else "PUT" for c in self.gh.calls()
                 if c["args"][0] == "variable" or c["args"][:3] == ["api", "-X", "PUT"] and PERMISSIONS in c["args"][3]]
        self.assertLess(names.index("PIVOTAL_PRIOR_CAN_APPROVE_PRS"), names.index("PUT"))

    def test_a_planted_value_is_overwritten_with_false_before_the_setting_is_turned_on(self):
        """Round 2, R3: init records false whatever the variable held (true, garbage), so remove can not be misled."""
        self.happy_path(
            permissions=(False, True),
            replace={f"api repos/{REPO}/actions/variables/PIVOTAL_PRIOR_CAN_APPROVE_PRS": [variable_now("true")]},
        )
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        sets = [c for c in self.calls("variable", "set") if c["args"][2] == "PIVOTAL_PRIOR_CAN_APPROVE_PRS"]
        self.assertEqual([c["stdin"] for c in sets], ["false"])

    def test_a_setting_that_was_already_on_records_nothing(self):
        self.happy_path(permissions=(True,))
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertNotIn("PIVOTAL_PRIOR_CAN_APPROVE_PRS", [c["args"][2] for c in self.calls("variable", "set")])

    def test_a_setting_that_does_not_stick_fails_loudly_and_writes_no_caller(self):
        self.happy_path(permissions=(False, False))
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 1)
        self.assertIn("did not stick", self.err.getvalue())
        self.assertEqual(self.calls("api", "-X", "PUT", WORKFLOW), [])

    def test_a_refused_read_fails_loudly_and_writes_no_caller(self):
        self.happy_path(replace={f"api repos/{REPO}/actions/permissions/workflow": [fakegh.fail("gh: Forbidden (HTTP 403)\n")]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 1)
        self.assertIn("403", self.err.getvalue())
        self.assertEqual(self.calls("api", "-X", "PUT", WORKFLOW), [])


class InputTest(PivTest):
    def assertRefused(self, *args: str, message: str):
        self.assertEqual(self.piv(*args), 2, self.err.getvalue())
        self.assertIn(message, self.err.getvalue())
        self.assertEqual(self.gh.calls(), [], "a refused request makes no gh call")

    def test_a_missing_check_exits_2_with_a_clear_message(self):
        self.assertRefused(REPO, message="--check is required")

    def test_an_empty_or_multiline_check_is_refused(self):
        self.assertRefused(REPO, "--check", "   ", message="--check")
        self.assertRefused(REPO, "--check", "npm test\nrm -rf", message="--check")

    def test_a_check_that_runs_nothing_is_refused_before_any_call(self):
        """F1: the same predicate as the engine and run.sh: a check of ':', ';', '&' or only a comment gates nothing."""
        for check in (":", ";", "&", "# x", ": ; &", "# none yet"):
            with self.subTest(check=check):
                self.assertRefused(REPO, "--check", check, message="--check")

    def test_a_malformed_repo_is_refused(self):
        for bad in ("my-app", "a/b/c", "../x", "owner/..", "owner/a b", "-o/r"):
            with self.subTest(repo=bad):
                self.assertRefused(bad, "--check", "npm test", message="owner/name")

    def test_a_check_that_is_not_utf8_text_is_refused_before_any_call(self):
        self.assertRefused(REPO, "--check", "npm\udcff test", message="--check must be UTF-8 text")

    def test_an_unknown_level_is_refused(self):
        self.assertRefused(REPO, "--check", "npm test", "--level", "fly", message="--level")

    def test_a_malformed_preset_is_refused(self):
        self.assertRefused(REPO, "--check", "npm test", "--preset", "Bad Preset", message="--preset")

    def test_a_malformed_release_is_refused(self):
        self.assertRefused(REPO, "--check", "npm test", "--release", "../etc", message="--release")

    def test_a_malformed_engine_is_refused(self):
        self.assertRefused(REPO, "--check", "npm test", "--engine", "nope", message="--engine")

    def test_an_unknown_command_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):  # argparse writes its usage to stderr itself
            self.assertEqual(cli.main(["destroy", REPO], self.gh.env(), self.out, self.err), 2)
        self.assertEqual(self.gh.calls(), [])


class ReleaseTest(PivTest):
    def test_defaults_to_the_latest_release_of_pivotal(self):
        self.happy_path()
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertEqual(len(self.calls("api", f"repos/{init.ENGINE_REPO}/releases/latest")), 1)
        self.assertIn("v1.0.0", self.out.getvalue())

    def test_an_explicit_tag_is_resolved_to_a_commit(self):
        self.happy_path(replace={f"api repos/{init.ENGINE_REPO}/commits/v0.9.0": [fakegh.ok(json.dumps({"sha": SHA}))]})
        self.assertEqual(self.piv(REPO, "--check", "npm test", "--release", "v0.9.0"), 0, self.err.getvalue())
        self.assertEqual(self.calls("api", f"repos/{init.ENGINE_REPO}/releases/latest"), [])

    def test_a_release_that_is_not_a_commit_fails_before_any_write(self):
        self.happy_path(replace={f"api repos/{init.ENGINE_REPO}/commits/v1.0.0": [fakegh.ok("{}")]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 1)
        self.assertEqual(self.calls("variable", "set"), [])

    def test_an_explicit_engine_is_resolved_and_every_mention_pins_to_it(self):
        engine = "a/b"
        self.happy_path(replace={
            f"api repos/{engine}/releases/latest": [fakegh.ok(json.dumps({"tag_name": "v1.0.0"}))],
            f"api repos/{engine}/commits/v1.0.0": [fakegh.ok(json.dumps({"sha": SHA}))],
        })
        self.assertEqual(self.piv(REPO, "--check", "npm test", "--engine", engine), 0, self.err.getvalue())
        written = base64.b64decode(self.caller_put()["content"]).decode("utf-8")
        self.assertNotIn(init.ENGINE_PLACEHOLDER, written)
        self.assertNotIn(init.ENGINE_REPO, written)
        pins = init.ANY_USES.findall(written) + init.ANY_CHECKOUT.findall(written)
        self.assertEqual({name for name, _ in pins}, {engine})
        self.assertEqual({sha for _, sha in pins}, {SHA})

    def test_a_template_with_no_placeholder_is_refused_before_any_write(self):
        self.happy_path(replace={f"templates/caller.yml?ref={SHA}": [fakegh.ok("name: no pins here\n")]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 1)
        self.assertIn("placeholder", self.err.getvalue())
        self.assertEqual(self.calls("variable", "set"), [])


class RenderTest(unittest.TestCase):
    """Every reference to the pivotal repo in the caller is the release's SHA, or the caller is not written."""

    def test_a_reference_left_unpinned_is_refused(self):
        template = CALLER.replace(f"land.yml@{ZEROS}", "land.yml@v1.0.0")
        with self.assertRaisesRegex(init.PivError, "not pinned"):
            init.render_caller(template, SHA)

    def test_a_reference_pinned_to_another_commit_is_refused(self):
        template = CALLER.replace(f"factory.yml@{ZEROS}", f"factory.yml@{'f' * 40}", 1)
        with self.assertRaisesRegex(init.PivError, "not pinned"):
            init.render_caller(template, SHA)

    def test_a_mention_of_the_repo_that_is_not_a_pin_is_refused(self):
        template = CALLER + f"\n# see {init.ENGINE_REPO} for the release notes\n"
        with self.assertRaisesRegex(init.PivError, "not pinned"):
            init.render_caller(template, SHA)

    def test_a_leftover_old_engine_literal_is_refused(self):
        template = CALLER + f"\n# see {init.LEGACY_ENGINE} for the release notes\n"
        with self.assertRaisesRegex(init.PivError, "leftover"):
            init.render_caller(template, SHA)

    def test_an_unreplaced_engine_placeholder_is_refused(self):
        # render_caller replaces the placeholder, so the gate is checked directly: a rendered caller must never carry it.
        text = CALLER.replace(init.ENGINE_PLACEHOLDER, init.ENGINE_REPO).replace(ZEROS, SHA)
        text += f"\n  extra: {init.ENGINE_PLACEHOLDER}\n"
        with self.assertRaisesRegex(init.PivError, "placeholder"):
            init._check_pins(text, SHA, init.ENGINE_REPO)

    def test_an_unpinned_reference_is_refused_when_rendered_with_a_custom_engine(self):
        template = CALLER.replace(f"land.yml@{ZEROS}", "land.yml@v1.0.0")
        with self.assertRaisesRegex(init.PivError, "not pinned"):
            init.render_caller(template, SHA, "a/b")

    def test_an_engine_sha_pinned_to_another_commit_is_refused(self):
        template = CALLER.replace(f'engine_sha: "{ZEROS}"', f'engine_sha: "{"f" * 40}"', 1)
        with self.assertRaisesRegex(init.PivError, "not pinned"):
            init.render_caller(template, SHA)

    def test_a_caller_with_every_reference_pinned_renders(self):
        expected = CALLER.replace(init.ENGINE_PLACEHOLDER, init.ENGINE_REPO).replace(ZEROS, SHA)
        self.assertEqual(init.render_caller(CALLER, SHA), expected)
        self.assertNotIn(init.ENGINE_PLACEHOLDER, expected)

    def test_every_mention_of_the_default_engine_is_pinned(self):
        rendered = init.render_caller(CALLER, SHA)
        pins = init.ANY_USES.findall(rendered) + init.ANY_CHECKOUT.findall(rendered)
        repo_pins = init.ENGINE_REPO_LINE.findall(rendered)
        self.assertEqual({engine for engine, _ in pins}, {init.ENGINE_REPO})
        self.assertEqual({sha for _, sha in pins}, {SHA})
        self.assertEqual(len(init.pivotal_reference(init.ENGINE_REPO).findall(rendered)), len(pins) + len(repo_pins))

    def test_every_mention_of_a_custom_engine_is_pinned_to_it(self):
        rendered = init.render_caller(CALLER, SHA, "a/b")
        self.assertNotIn(init.ENGINE_PLACEHOLDER, rendered)
        self.assertNotIn(init.LEGACY_ENGINE, rendered)
        pins = init.ANY_USES.findall(rendered) + init.ANY_CHECKOUT.findall(rendered)
        repo_pins = init.ENGINE_REPO_LINE.findall(rendered)
        self.assertEqual({engine for engine, _ in pins}, {"a/b"})
        self.assertEqual({sha for _, sha in pins}, {SHA})
        self.assertEqual(len(init.pivotal_reference("a/b").findall(rendered)), len(pins) + len(repo_pins))


class ExistingCallerTest(PivTest):
    def test_an_existing_caller_that_is_not_utf8_is_refused_without_a_write(self):
        raw = base64.b64encode(b"\xff\xfe\x00").decode()
        self.happy_path(replace={f"api {WORKFLOW}": [fakegh.ok(json.dumps({"sha": "b10b", "content": raw}))]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 1)
        self.assertIn("not UTF-8", self.err.getvalue())
        self.assertEqual(self.calls("variable", "set"), [])


class UnpinnedCallerTest(PivTest):
    def test_an_unpinned_caller_is_refused_before_any_write(self):
        template = CALLER.replace(f"land.yml@{ZEROS}", "land.yml@v1.0.0")
        self.happy_path(replace={f"templates/caller.yml?ref={SHA}": [fakegh.ok(template)]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 1)
        self.assertIn("not pinned", self.err.getvalue())
        writes = [
            c for c in self.gh.calls()
            if c["args"][0] in ("secret", "variable", "label", "repo") or "-X" in c["args"]
        ]
        self.assertEqual(writes, [])


class LauncherTest(PivTest):
    """The extension entry point, run as gh runs it: a subprocess, with the fake as PIV_GH."""

    LAUNCHER = ROOT / "gh-piv" / "gh-piv"

    def run_launcher(self, *args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.LAUNCHER), *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
        )

    def test_the_launcher_is_executable(self):
        self.assertTrue(os.access(self.LAUNCHER, os.X_OK))

    def test_user_visible_check_prints_the_steps_and_exits_0(self):
        self.happy_path()
        work = self.root / "work"
        work.mkdir()
        done = self.run_launcher("init", REPO, "--check", "npm test", cwd=work, env=self.gh.env())
        self.assertEqual(done.returncode, 0, done.stderr)
        for step in ("variable PIVOTAL_CHECK", "label pivotal", "topic pivotal", "caller", "not run"):
            self.assertIn(step, done.stdout)

    def test_a_missing_check_exits_non_zero_with_a_clear_message(self):
        done = self.run_launcher("init", REPO, cwd=self.root, env=self.gh.env())
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("--check is required", done.stderr)

    def test_init_writes_nothing_to_the_working_directory(self):
        self.happy_path()
        work = self.root / "work"
        work.mkdir()
        done = self.run_launcher("init", REPO, "--check", "npm test", cwd=work, env=self.gh.env())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(list(work.iterdir()), [])

    def test_a_gh_binary_that_does_not_exist_fails_with_a_message(self):
        done = self.run_launcher("init", REPO, "--check", "npm test", cwd=self.root,
                                 env=self.gh.env(PIV_GH="/nonexistent/gh"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot run /nonexistent/gh", done.stderr)


class RerunTest(PivTest):
    """A second run must not undo what someone changed: a caller file that differs, or a level already set."""

    def test_a_differing_caller_is_refused_without_update_and_nothing_is_written(self):
        self.happy_path(existing="old: caller\n")
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 1)
        self.assertIn("--update", self.err.getvalue())
        self.assertIn("old: caller", self.err.getvalue())  # what differs is named
        self.assertEqual(self.calls("variable", "set"), [])
        self.assertEqual(self.calls("secret", "set"), [])
        self.assertEqual(self.calls("label", "create"), [])
        self.assertEqual(self.calls("api", "-X", "PUT"), [])

    def test_the_existing_level_is_kept_when_no_level_is_given(self):
        self.happy_path(replace={f"api repos/{REPO}/actions/variables/PIVOTAL_LEVEL": [variable_now("watch")]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertNotIn("PIVOTAL_LEVEL", [c["args"][2] for c in self.calls("variable", "set")])
        self.assertIn("variable PIVOTAL_LEVEL: unchanged", self.out.getvalue())

    def test_an_explicit_level_replaces_an_existing_one_and_says_was_and_now(self):
        self.happy_path(replace={f"api repos/{REPO}/actions/variables/PIVOTAL_LEVEL": [variable_now("watch")]})
        self.assertEqual(self.piv(REPO, "--check", "npm test", "--level", "ask"), 0, self.err.getvalue())
        levels = [c for c in self.calls("variable", "set") if c["args"][2] == "PIVOTAL_LEVEL"]
        self.assertEqual([c["stdin"] for c in levels], ["ask"])
        self.assertIn("variable PIVOTAL_LEVEL: was watch, now ask", self.out.getvalue())

    def test_a_changed_variable_prints_was_and_now(self):
        self.happy_path(replace={f"api repos/{REPO}/actions/variables/PIVOTAL_CHECK": [variable_now("npm run old")]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertIn("variable PIVOTAL_CHECK: was npm run old, now npm test", self.out.getvalue())

    def test_a_variable_not_yet_set_says_it_was_unset(self):
        self.happy_path()
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertIn("variable PIVOTAL_CHECK: was unset, now npm test", self.out.getvalue())
        self.assertIn("variable PIVOTAL_LEVEL: was unset, now prepare", self.out.getvalue())

    def test_an_unchanged_variable_is_not_written_again(self):
        self.happy_path(replace={f"api repos/{REPO}/actions/variables/PIVOTAL_CHECK": [variable_now("npm test")]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 0, self.err.getvalue())
        self.assertNotIn("PIVOTAL_CHECK", [c["args"][2] for c in self.calls("variable", "set")])
        self.assertIn("variable PIVOTAL_CHECK: unchanged", self.out.getvalue())


class ProtectedBranchTest(PivTest):
    """A protected default branch refuses the caller after the config is in, so the run says what is left."""

    def assert_refused_caller_write(self, status: int) -> None:
        self.happy_path(replace={
            f"api -X PUT {WORKFLOW}": [fakegh.fail(f"gh: Repository rule violations (HTTP {status})\n")],
        })
        env = self.gh.env(CLAUDE_CODE_OAUTH_TOKEN="claude-token-value")
        self.assertEqual(self.piv(REPO, "--check", "npm test", env=env), 1)
        message = self.err.getvalue()
        self.assertIn(f"HTTP {status}", message)
        self.assertIn("pull request", message)
        self.assertIn("Secrets and variables are already set", message)
        self.assertEqual(len(self.calls("secret", "set")), 1, "the config was written before the caller")
        self.assertNotIn("claude-token-value", message)

    def test_a_protected_branch_refusal_409_says_the_secrets_are_set_and_a_pull_request_is_needed(self):
        self.assert_refused_caller_write(409)

    def test_a_refusal_403_says_the_same(self):
        self.assert_refused_caller_write(403)

    def test_any_other_failure_writing_the_caller_is_reported_as_is(self):
        self.happy_path(replace={f"api -X PUT {WORKFLOW}": [fakegh.fail("gh: Server Error (HTTP 500)\n")]})
        self.assertEqual(self.piv(REPO, "--check", "npm test"), 1)
        self.assertIn("HTTP 500", self.err.getvalue())
        self.assertNotIn("pull request", self.err.getvalue())


class FakeGhIsStrictTest(unittest.TestCase):
    """The fake refuses a flag the real gh does not have, so the piv tests can trust what they pass."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "bin")

    def tearDown(self):
        self._tmp.cleanup()

    def run_fake(self, *argv: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([str(self.gh.path), *argv], input=stdin, capture_output=True, text=True, check=False)

    def test_refuses_a_flag_the_real_secret_set_does_not_have(self):
        self.gh.route("secret set", fakegh.ok())
        done = self.run_fake("secret", "set", "NAME", "--repo", REPO, "--body-file", "-", stdin="v")
        self.assertEqual(done.returncode, 2)
        self.assertIn("has no flag --body-file", done.stderr)
        self.assertEqual(len(self.gh.calls()), 1, "the refused call is still logged, so a test can see it")

    def test_refuses_a_flag_the_real_variable_set_does_not_have(self):
        self.gh.route("variable set", fakegh.ok())
        done = self.run_fake("variable", "set", "NAME", "--repo", REPO, "--body-file", "-", stdin="v")
        self.assertEqual(done.returncode, 2)
        self.assertIn("has no flag --body-file", done.stderr)

    def test_refuses_an_unknown_gh_command(self):
        self.gh.route("destroy", fakegh.ok())
        done = self.run_fake("destroy", "everything")
        self.assertEqual(done.returncode, 2)
        self.assertIn("no such gh command", done.stderr)

    def test_accepts_the_flags_the_real_commands_have(self):
        self.gh.route("secret set", fakegh.ok())
        self.gh.route("variable set", fakegh.ok())
        self.gh.route("label create", fakegh.ok())
        self.gh.route("repo edit", fakegh.ok())
        self.gh.route("api", fakegh.ok())
        accepted = [
            ("secret", "set", "NAME", "--repo", REPO),
            ("variable", "set", "NAME", "--repo", REPO, "--body", "value"),
            ("label", "create", "pivotal", "--repo", REPO, "--color", "5319e7", "--description", "x", "--force"),
            ("repo", "edit", REPO, "--add-topic", "pivotal"),
            ("api", "-X", "PUT", WORKFLOW, "--input", "-"),
            ("api", "-H", "Accept: application/vnd.github.raw", f"repos/{REPO}/contents/x?ref={SHA}"),
        ]
        for argv in accepted:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_fake(*argv).returncode, 0)

    def test_a_flag_that_takes_a_value_skips_that_value(self):
        self.gh.route("label create", fakegh.ok())
        # `-f` is --force here, but a value for `--description`: it must not be read as a flag.
        done = self.run_fake("label", "create", "x", "--description", "-f", "--color", "5319e7")
        self.assertEqual(done.returncode, 0, done.stderr)


class LabelsMatchThePlanTest(unittest.TestCase):
    def test_the_plan_names_these_labels(self):
        self.assertEqual({label for label, _colour, _text in init.LABELS}, LABELS)

    def test_the_trigger_labels_are_the_ones_the_engine_reads_as_triggers(self):
        names = {label for label, _colour, _text in init.LABELS}
        triggers = {label for label in names if trust.PIVOTAL_LABEL.fullmatch(label)}
        self.assertEqual(triggers, {"pivotal", "pivotal:build", "pivotal:incident", "pivotal:mirror", "pivotal:docs"})


if __name__ == "__main__":
    unittest.main()
