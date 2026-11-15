"""piv remove: every write goes through gh, and gh is a fake here.

The fake answers each read with what the repo has, and logs every call, so a test checks what was
deleted, in what order, and that a refusal wrote nothing. The fake is not stateful: "nothing
Pivotal-made remains" is tested as "each name init can write is deleted when the repo has it".
"""

import base64
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

from piv import cli, init, remove  # noqa: E402
from piv.gh import Gh  # noqa: E402

REPO = "0xctx/piv-sandbox"
WORKFLOW = f"repos/{REPO}/contents/{init.WORKFLOW_PATH}"
SECRETS_LIST = f"api repos/{REPO}/actions/secrets?per_page=1"
VARIABLES_LIST = f"api repos/{REPO}/actions/variables?per_page=1"
BLOB = "b10b"
CALLER_TEMPLATE = (ROOT / "templates" / "caller.yml").read_text(encoding="utf-8")
PIN = "1" * 40
OTHER_PIN = "2" * 40
# The caller init writes at PIN. The template for PIN is what remove compares the file with.
PIVOTAL_CALLER = init.render_caller(CALLER_TEMPLATE, PIN)
TEMPLATE_AT_PIN = f"templates/caller.yml?ref={PIN}"
FOREIGN_CALLER = "name: someone else's workflow\n"
LABELS = [name for name, _colour, _text in init.LABELS]
GENERIC_SECRETS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def is_read(call: dict) -> bool:
    """A read is a GET through ``gh api``: no ``-X``, so no method other than GET."""
    return call["args"][0] == "api" and "-X" not in call["args"]


class RemoveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "bin")
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    def run_remove(self, repo: str = REPO, env: dict[str, str] | None = None, *, yes: bool = True,
                   extra: tuple[str, ...] = ()) -> int:
        argv = ["remove", repo, *(["--yes"] if yes else []), *extra]
        return cli.main(argv, self.gh.env() if env is None else env, self.out, self.err)

    def installed(self, *, caller: str | None = PIVOTAL_CALLER, secrets=(), variables=(), labels=(),
                  topics=("pivotal",), repo_exists: bool = True, replace: dict | None = None):
        """Routes for a repo that init set up: the names given are present, every other name is absent (a 404).

        Route order matters: the fake answers with the first route whose text appears in the call. A
        name that is a prefix of another (PIVOTAL_PRESET and PIVOTAL_PRESET_JSON, pivotal and
        pivotal:building) is routed after the longer one, so the longer name gets its own answer.
        """
        if caller is None:
            caller_now = fakegh.not_found()
        else:
            content = base64.b64encode(caller.encode("utf-8")).decode("ascii")
            caller_now = fakegh.ok(json.dumps({"sha": BLOB, "content": content}))
        if repo_exists:
            self.gh.route(f"api repos/{REPO}", fakegh.ok("{}"), exact=True)
        else:
            self.gh.route(f"api repos/{REPO}", fakegh.not_found(), exact=True)
        answers = {
            f"api {WORKFLOW}": [caller_now],
            f"-X DELETE {WORKFLOW}": [fakegh.ok("{}")],
            f"api repos/{REPO}/topics": [fakegh.ok(json.dumps({"names": list(topics)}))],
            f"repo edit {REPO} --remove-topic pivotal": [fakegh.ok()],
            SECRETS_LIST: [fakegh.ok('{"total_count": 0, "secrets": []}')],
            VARIABLES_LIST: [fakegh.ok('{"total_count": 0, "variables": []}')],
            TEMPLATE_AT_PIN: [fakegh.ok(CALLER_TEMPLATE)],
        }
        for name in init.SECRETS:
            answers[f"api repos/{REPO}/actions/secrets/{name}"] = [_present(name in secrets, name)]
            answers[f"secret delete {name} --repo {REPO}"] = [fakegh.ok()]
        for name in sorted(init.VARIABLES, key=len, reverse=True):
            answers[f"api repos/{REPO}/actions/variables/{name}"] = [_present(name in variables, name)]
            answers[f"variable delete {name} --repo {REPO}"] = [fakegh.ok()]
        for name in sorted(LABELS, key=len, reverse=True):
            answers[f"api repos/{REPO}/labels/{name}"] = [_present(name in labels, name)]
            answers[f"label delete {name} --repo {REPO}"] = [fakegh.ok()]
        if init.VAR_PRIOR_APPROVE in variables:  # the recorded 'false' is restored: the setting reads on, then off
            now = {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True}
            answers[f"api repos/{REPO}/actions/permissions/workflow"] = [
                fakegh.ok(json.dumps(now)), fakegh.ok(json.dumps({**now, "can_approve_pull_request_reviews": False}))]
            answers[f"api -X PUT repos/{REPO}/actions/permissions/workflow"] = [fakegh.ok("{}")]
        answers.update(replace or {})
        for needle, values in answers.items():
            self.gh.route(needle, *values)

    def calls(self, *words: str) -> list[dict]:
        """The calls whose argv begins with ``words``."""
        return [c for c in self.gh.calls() if c["args"][: len(words)] == list(words)]

    def writes(self) -> list[dict]:
        return [c for c in self.gh.calls() if not is_read(c)]

    def names(self, *words: str) -> list[str]:
        """The name each call deleted, in order: the third argument of ``secret delete NAME`` and the like."""
        return [c["args"][2] for c in self.calls(*words)]


def _present(found: bool, name: str) -> dict:
    value = "false" if name == init.VAR_PRIOR_APPROVE else "set"  # init writes exactly 'false' there; remove refuses anything else
    return fakegh.ok(json.dumps({"name": name, "value": value})) if found else fakegh.not_found()


class RemoveDeletesTest(RemoveTest):
    def test_removes_every_name_init_writes_when_the_repo_has_them_all(self):
        self.installed(caller=PIVOTAL_CALLER, secrets=init.SECRETS, variables=init.VARIABLES, labels=LABELS)
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertEqual(self.names("secret", "delete"), list(init.SECRETS))
        self.assertEqual(self.names("variable", "delete"), list(init.VARIABLES))
        self.assertEqual(self.names("label", "delete"), LABELS)
        self.assertEqual(self.calls("repo", "edit")[0]["args"], ["repo", "edit", REPO, "--remove-topic", "pivotal"])

    def test_the_caller_is_deleted_first_by_its_blob_sha_with_no_content(self):
        self.installed(caller=PIVOTAL_CALLER, secrets=init.SECRETS, variables=init.VARIABLES, labels=LABELS)
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        first = self.writes()[0]
        self.assertEqual(first["args"][:3], ["api", "-X", "DELETE"])
        self.assertEqual(json.loads(first["stdin"]), {"message": remove.CALLER_MESSAGE, "sha": BLOB})

    def test_no_delete_carries_a_value(self):
        self.installed(caller=PIVOTAL_CALLER, secrets=init.SECRETS, variables=init.VARIABLES, labels=LABELS)
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        for call in self.writes():
            if call["args"][:2] != ["api", "-X"]:
                self.assertEqual(call["stdin"], "", call["args"])

    def test_only_the_names_that_are_set_are_deleted(self):
        present_secrets = (init.SECRETS[0], init.SECRETS[-1])
        self.installed(caller=None, secrets=present_secrets, variables=("PIVOTAL_LEVEL",), labels=("pivotal",))
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertEqual(self.names("secret", "delete"), list(present_secrets))
        self.assertEqual(self.names("variable", "delete"), ["PIVOTAL_LEVEL"])
        self.assertEqual(self.names("label", "delete"), ["pivotal"])
        self.assertEqual(self.calls("api", "-X", "DELETE"), [], "no caller file, so none to delete")

    def test_a_label_is_deleted_by_its_exact_name_not_by_a_prefix(self):
        self.installed(labels=("pivotal:building",), topics=())
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertEqual(self.names("label", "delete"), ["pivotal:building"])

    def test_a_variable_is_deleted_by_its_exact_name_not_by_a_prefix(self):
        self.installed(variables=("PIVOTAL_PRESET_JSON",), topics=())
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertEqual(self.names("variable", "delete"), ["PIVOTAL_PRESET_JSON"])

    def test_the_untrusted_preset_is_removed_as_a_pivotal_variable(self):
        self.installed(variables=(init.UNTRUSTED_PRESET,), topics=())
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertEqual(self.names("variable", "delete"), [init.UNTRUSTED_PRESET])

    def test_names_that_are_not_pivotals_are_never_looked_at_or_deleted(self):
        # No route exists for these names, so a look at one would exit 2 and fail the run.
        self.installed(caller=None, topics=("python",))
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        for call in self.gh.calls():
            self.assertFalse(any(word in ("OTHER_TOKEN", "bug", "MY_VAR") for word in call["args"]), call["args"])


class RemoveNothingTest(RemoveTest):
    def test_a_repo_with_none_of_pivotals_makes_no_write_and_says_so(self):
        self.installed(caller=None, topics=())
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertEqual(self.writes(), [])
        self.assertIn("none of Pivotal's is set, so nothing was removed", self.out.getvalue())

    def test_the_topic_is_not_written_when_it_is_not_set(self):
        self.installed(caller=None, topics=("python",))
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertEqual(self.calls("repo", "edit"), [], "the topic is not set, so nothing is written to it")

    def test_the_topic_is_removed_by_name_when_other_topics_are_set(self):
        self.installed(caller=None, topics=("pivotal", "python"))
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertEqual(len(self.calls("repo", "edit")), 1)
        self.assertEqual(self.calls("repo", "edit")[0]["args"][3:], ["--remove-topic", "pivotal"])


class RemoveConfirmTest(RemoveTest):
    """R1: nothing is deleted without --yes. The reads run, the plan is printed, and the exit is non-zero."""

    def test_without_yes_no_write_happens_and_the_plan_is_printed(self):
        self.installed(caller=PIVOTAL_CALLER, secrets=init.SECRETS, variables=init.VARIABLES, labels=LABELS)
        self.assertEqual(self.run_remove(yes=False), 2, self.err.getvalue())
        self.assertEqual(self.writes(), [], "no delete without --yes")
        self.assertIn("would remove", self.out.getvalue())
        self.assertIn("already absent", self.out.getvalue())
        self.assertIn("--yes", self.err.getvalue())

    def test_without_yes_the_generic_secret_names_are_named_as_maybe_the_repos_own(self):
        self.installed(caller=None, secrets=init.SECRETS, topics=())
        self.assertEqual(self.run_remove(yes=False), 2, self.err.getvalue())
        warning = [line for line in self.out.getvalue().splitlines() if line.startswith("warning:")]
        self.assertEqual(len(warning), 1, self.out.getvalue())
        for name in GENERIC_SECRETS:
            self.assertIn(name, warning[0])
        self.assertNotIn("PIVOTAL_KAGGLE_KEY", warning[0], "a Pivotal-only name is not a maybe")

    def test_with_yes_the_generic_secret_warning_is_printed_before_the_delete(self):
        self.installed(caller=None, secrets=init.SECRETS, topics=())
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertIn("warning:", self.out.getvalue())
        self.assertIn("ANTHROPIC_API_KEY", self.out.getvalue())
        self.assertEqual(len(self.names("secret", "delete")), len(init.SECRETS))

    def test_without_yes_a_repo_with_nothing_to_remove_exits_0_and_writes_nothing(self):
        self.installed(caller=None, topics=())
        self.assertEqual(self.run_remove(yes=False), 0, self.err.getvalue())
        self.assertEqual(self.writes(), [])
        self.assertIn("none of Pivotal's is set", self.out.getvalue())

    def test_without_yes_an_edited_caller_is_still_refused_before_anything(self):
        self.installed(caller=PIVOTAL_CALLER + "# mine\n", secrets=init.SECRETS)
        self.assertEqual(self.run_remove(yes=False), 1)
        self.assertEqual(self.writes(), [])


class RemoveCallerIdentityTest(RemoveTest):
    """R2: the caller is removed only when it is the file init writes at its pin, byte for byte."""

    def test_a_caller_that_is_not_pivotals_is_refused_before_any_write(self):
        self.installed(caller=FOREIGN_CALLER, secrets=init.SECRETS, variables=init.VARIABLES, labels=LABELS)
        self.assertEqual(self.run_remove(), 1)
        self.assertIn(init.WORKFLOW_PATH, self.err.getvalue())
        self.assertIn("was edited", self.err.getvalue())
        self.assertIn("--force-caller", self.err.getvalue())
        self.assertEqual(self.writes(), [], "a refusal deletes nothing, not even the rest of Pivotal's config")
        self.assertTrue(all(is_read(c) for c in self.gh.calls()))

    def test_a_pivotal_notes_reference_is_not_a_pivotal_caller(self):
        caller = "jobs:\n  x:\n    uses: you/pivotal-notes/x@main\n"
        self.installed(caller=caller, secrets=init.SECRETS)
        self.assertEqual(self.run_remove(), 1)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.calls("api", TEMPLATE_AT_PIN), [], "no pin to check, so nothing is compared")

    def test_a_comment_that_mentions_the_repo_is_not_a_pivotal_caller(self):
        self.installed(caller=PIVOTAL_CALLER + "# see you/pivotal for the release notes\n", secrets=init.SECRETS)
        self.assertEqual(self.run_remove(), 1)
        self.assertIn("was edited", self.err.getvalue())
        self.assertEqual(self.writes(), [])

    def test_a_caller_the_user_edited_is_refused_and_kept(self):
        edited = PIVOTAL_CALLER.replace("name: pivotal", "name: my-pipeline")
        self.assertNotEqual(edited, PIVOTAL_CALLER)
        self.installed(caller=edited, secrets=init.SECRETS, variables=init.VARIABLES)
        self.assertEqual(self.run_remove(), 1)
        self.assertIn("was edited", self.err.getvalue())
        self.assertEqual(self.writes(), [])

    def test_a_caller_pinned_to_a_commit_that_is_not_pivotals_is_refused(self):
        other = init.render_caller(CALLER_TEMPLATE, OTHER_PIN)
        self.installed(
            caller=other,
            replace={f"templates/caller.yml?ref={OTHER_PIN}": [fakegh.not_found()]},
        )
        self.assertEqual(self.run_remove(), 1)
        self.assertIn("was edited", self.err.getvalue())
        self.assertEqual(self.writes(), [])

    def test_force_caller_deletes_an_edited_caller_and_reads_no_template(self):
        edited = PIVOTAL_CALLER + "# mine\n"
        self.installed(caller=edited)
        self.assertEqual(self.run_remove(extra=("--force-caller",)), 0, self.err.getvalue())
        first = self.writes()[0]
        self.assertEqual(json.loads(first["stdin"]), {"message": remove.CALLER_MESSAGE, "sha": BLOB})
        self.assertEqual(self.calls("api", TEMPLATE_AT_PIN), [])


class RemoveRefusalTest(RemoveTest):
    def test_a_failed_read_writes_nothing(self):
        self.installed(
            caller=PIVOTAL_CALLER,
            secrets=init.SECRETS,
            variables=init.VARIABLES,
            replace={f"api repos/{REPO}/actions/variables/PIVOTAL_CHECK": [fakegh.fail("gh: HTTP 500\n")]},
        )
        self.assertEqual(self.run_remove(), 1)
        self.assertIn("HTTP 500", self.err.getvalue())
        self.assertEqual(self.writes(), [])

    def test_a_refused_caller_write_on_a_protected_branch_removes_nothing_else(self):
        self.installed(
            caller=PIVOTAL_CALLER,
            secrets=init.SECRETS,
            variables=init.VARIABLES,
            labels=LABELS,
            replace={f"-X DELETE {WORKFLOW}": [fakegh.fail("gh: Resource not accessible (HTTP 403)\n")]},
        )
        self.assertEqual(self.run_remove(), 1)
        message = self.err.getvalue()
        self.assertIn("HTTP 403", message)
        self.assertIn("branch protection", message)
        self.assertIn("ruleset", message)
        self.assertIn("nothing else was removed", message)
        self.assertIn("pull request", message)
        self.assertEqual([c["args"][:3] for c in self.writes()], [["api", "-X", "DELETE"]])

    def test_a_409_on_the_caller_says_the_file_changed_and_to_run_again(self):
        self.installed(
            caller=PIVOTAL_CALLER,
            secrets=init.SECRETS,
            replace={f"-X DELETE {WORKFLOW}": [fakegh.fail("gh: is at abc but expected def (HTTP 409)\n")]},
        )
        self.assertEqual(self.run_remove(), 1)
        message = self.err.getvalue()
        self.assertIn("HTTP 409", message)
        self.assertIn("changed since piv remove read it", message)
        self.assertIn(f"run piv remove {REPO} --yes again", message)
        self.assertNotIn("branch protection", message)
        self.assertEqual([c["args"][:3] for c in self.writes()], [["api", "-X", "DELETE"]])

    def test_a_422_on_the_caller_is_reported_with_its_status_and_nothing_else_removed(self):
        self.installed(
            caller=PIVOTAL_CALLER,
            secrets=init.SECRETS,
            replace={f"-X DELETE {WORKFLOW}": [fakegh.fail("gh: Invalid request (HTTP 422)\n")]},
        )
        self.assertEqual(self.run_remove(), 1)
        message = self.err.getvalue()
        self.assertIn("HTTP 422", message)
        self.assertIn("nothing else was removed", message)
        self.assertIn("ruleset", message)
        self.assertEqual([c["args"][:3] for c in self.writes()], [["api", "-X", "DELETE"]])

    def test_a_failed_delete_stops_the_run_and_the_rest_is_left_for_a_re_run(self):
        first = init.SECRETS[0]
        self.installed(
            caller=PIVOTAL_CALLER,
            secrets=init.SECRETS,
            variables=init.VARIABLES,
            labels=LABELS,
            replace={f"secret delete {first} --repo {REPO}": [fakegh.fail("gh: HTTP 500\n")]},
        )
        self.assertEqual(self.run_remove(), 1)
        self.assertEqual(self.names("secret", "delete"), [first], "the run stops at the first failed delete")
        self.assertEqual(self.calls("variable", "delete"), [])
        self.assertEqual(self.calls("label", "delete"), [])
        self.assertEqual(self.calls("repo", "edit"), [])

    def test_a_failure_after_some_removals_says_what_was_removed_and_that_a_re_run_is_safe(self):
        first, second = init.SECRETS[0], init.SECRETS[1]
        self.installed(
            caller=PIVOTAL_CALLER,
            secrets=init.SECRETS,
            variables=init.VARIABLES,
            replace={f"secret delete {second} --repo {REPO}": [fakegh.fail("gh: HTTP 500\n")]},
        )
        self.assertEqual(self.run_remove(), 1)
        message = self.err.getvalue()
        self.assertIn("HTTP 500", message)
        self.assertIn("Already removed: caller", message)
        self.assertIn(f"secret {first}", message)
        self.assertNotIn(f"secret {second}", message.split("Already removed:")[1])
        self.assertIn(f"piv remove {REPO} --yes is safe", message)

    def test_a_bad_repo_is_refused_before_any_call(self):
        self.installed()
        self.assertEqual(self.run_remove("my-app"), 2)
        self.assertIn("owner/name", self.err.getvalue())
        self.assertEqual(self.gh.calls(), [], "a refused request makes no gh call")


class RemoveReadAccessTest(RemoveTest):
    """R4: a repo that is not visible is said so, and a token that cannot read a list is not read as absent."""

    def test_a_repo_that_is_not_found_is_refused_after_one_call(self):
        self.installed(repo_exists=False, secrets=init.SECRETS)
        self.assertEqual(self.run_remove(), 1)
        self.assertIn("repo not found or not visible", self.err.getvalue())
        self.assertEqual(len(self.gh.calls()), 1, "the repo probe is the only call")
        self.assertEqual(self.writes(), [])

    def test_a_token_that_cannot_read_secrets_is_reported_not_read_as_absent(self):
        self.installed(
            caller=PIVOTAL_CALLER,
            secrets=init.SECRETS,
            replace={SECRETS_LIST: [fakegh.fail("gh: Resource not accessible by personal access token (HTTP 403)\n")]},
        )
        self.assertEqual(self.run_remove(), 1)
        self.assertIn("cannot read the Actions secrets", self.err.getvalue())
        self.assertIn("HTTP 403", self.err.getvalue())
        self.assertEqual(self.writes(), [], "nothing is removed when a name cannot be checked")

    def test_a_token_that_cannot_read_variables_is_reported_not_read_as_absent(self):
        self.installed(
            caller=PIVOTAL_CALLER,
            variables=init.VARIABLES,
            replace={VARIABLES_LIST: [fakegh.fail("gh: Not Found (HTTP 404)\n")]},
        )
        self.assertEqual(self.run_remove(), 1)
        self.assertIn("cannot read the Actions variables", self.err.getvalue())
        self.assertEqual(self.writes(), [])


class RemoveActionsSettingTest(RemoveTest):
    def test_the_actions_setting_is_left_as_it_is_and_named(self):
        """No recorded value (a repo init never changed the setting of): nothing to restore."""
        others = tuple(v for v in init.VARIABLES if v != init.VAR_PRIOR_APPROVE)
        self.installed(caller=PIVOTAL_CALLER, secrets=init.SECRETS, variables=others, labels=LABELS)
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        self.assertIn("Actions can open pull requests: left as it is", self.out.getvalue())
        self.assertEqual([c for c in self.gh.calls() if "permissions" in " ".join(c["args"])], [])


class RemoveRestoresActionsSettingTest(RemoveTest):
    PERMISSIONS = f"api repos/{REPO}/actions/permissions/workflow"
    PRIOR = f"api repos/{REPO}/actions/variables/{init.VAR_PRIOR_APPROVE}"

    def recorded(self, value: str, *, read_back: bool = False):
        self.installed(
            caller=PIVOTAL_CALLER, variables=(init.VAR_PRIOR_APPROVE,),
            replace={
                self.PRIOR: [fakegh.ok(json.dumps({"value": value}))],
                self.PERMISSIONS: [
                    fakegh.ok(json.dumps({"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True})),
                    fakegh.ok(json.dumps({"default_workflow_permissions": "write", "can_approve_pull_request_reviews": read_back})),
                ],
                f"api -X PUT repos/{REPO}/actions/permissions/workflow": [fakegh.ok("{}")],
            },
        )

    def test_the_recorded_value_is_put_back_keeping_the_default_permission_then_the_variable_is_deleted(self):
        self.recorded("false")
        self.assertEqual(self.run_remove(), 0, self.err.getvalue())
        puts = self.calls("api", "-X", "PUT", f"repos/{REPO}/actions/permissions/workflow")
        self.assertEqual(len(puts), 1)
        self.assertEqual(json.loads(puts[0]["stdin"]),
                         {"can_approve_pull_request_reviews": False, "default_workflow_permissions": "write"})
        writes = [c["args"][:3] for c in self.writes()]
        self.assertLess(writes.index(["api", "-X", "PUT"]), writes.index(["variable", "delete", init.VAR_PRIOR_APPROVE]))
        self.assertIn("restored to off", self.out.getvalue())

    def test_a_dry_run_restores_nothing(self):
        self.recorded("false")
        self.assertEqual(self.run_remove(yes=False), 2)
        self.assertEqual(self.writes(), [])

    def test_a_setting_that_does_not_read_back_off_fails_and_keeps_the_variable(self):
        self.recorded("false", read_back=True)
        self.assertEqual(self.run_remove(), 1)
        self.assertIn("reads back as True", self.err.getvalue())
        self.assertEqual(self.names("variable", "delete"), [])

    def test_a_recorded_value_other_than_false_is_refused_and_nothing_is_removed(self):
        """Round 2, R3: init only ever writes 'false'; anything else was planted, and exiting 0 would leave the setting on."""
        for value in ("true", "garbage", ""):
            with self.subTest(value=value):
                self.setUp()
                self.recorded(value)
                self.assertEqual(self.run_remove(), 1)
                self.assertIn(init.VAR_PRIOR_APPROVE, self.err.getvalue())
                self.assertEqual(self.writes(), [], "nothing removed, the variable kept")

    def test_a_dry_run_says_the_setting_would_be_restored(self):
        self.recorded("false")
        self.assertEqual(self.run_remove(yes=False), 2)
        self.assertIn("restored to off", self.out.getvalue())


class RemoveLauncherTest(RemoveTest):
    """The extension entry point, run as gh runs it: a subprocess, with the fake as PIV_GH."""

    LAUNCHER = ROOT / "gh-piv" / "gh-piv"

    def test_user_visible_check_prints_each_removal_and_exits_0(self):
        self.installed(caller=PIVOTAL_CALLER, secrets=init.SECRETS, variables=init.VARIABLES, labels=LABELS)
        work = Path(self._tmp.name) / "work"
        work.mkdir()
        done = subprocess.run(
            [str(self.LAUNCHER), "remove", REPO, "--yes"], cwd=work, env=self.gh.env(),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        for step in ("caller", "secret CLAUDE_CODE_OAUTH_TOKEN: removed", "variable PIVOTAL_CHECK: removed",
                     "label pivotal:building: removed", "topic pivotal: removed", "Actions can open pull requests"):
            self.assertIn(step, done.stdout)
        self.assertEqual(list(work.iterdir()), [], "remove writes nothing to the working directory")

    def test_a_bad_repo_exits_2_from_the_launcher(self):
        done = subprocess.run(
            [str(self.LAUNCHER), "remove", "my-app"], cwd=self.gh.dir, env=self.gh.env(),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(done.returncode, 2)
        self.assertIn("owner/name", done.stderr)


class RemoveFakeFlagsTest(unittest.TestCase):
    """The delete flags the fake takes are the ones `gh <cmd> --help` lists. An invented flag is refused."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "bin")

    def tearDown(self):
        self._tmp.cleanup()

    def run_fake(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.gh.path), *argv], input="", capture_output=True, text=True, check=False)

    def test_refuses_a_flag_the_real_secret_delete_does_not_have(self):
        self.gh.route("secret delete", fakegh.ok())
        done = self.run_fake("secret", "delete", "NAME", "--repo", REPO, "--force")
        self.assertEqual(done.returncode, 2)
        self.assertIn("has no flag --force", done.stderr)

    def test_accepts_the_flags_the_real_delete_commands_have(self):
        for route in ("secret delete", "variable delete", "label delete", "repo edit", "api"):
            self.gh.route(route, fakegh.ok())
        accepted = [
            ("secret", "delete", "NAME", "--repo", REPO),
            ("variable", "delete", "NAME", "--repo", REPO),
            ("label", "delete", "pivotal:building", "--repo", REPO, "--yes"),
            ("repo", "edit", REPO, "--remove-topic", "pivotal"),
            ("api", "-X", "DELETE", WORKFLOW, "--input", "-"),
        ]
        for argv in accepted:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_fake(*argv).returncode, 0)


class RemoveNamesTest(unittest.TestCase):
    """R3: init's writer and init.VARIABLES use one definition, and the caller reads only names in it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "bin")

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_variables_init_writes_are_in_the_list_remove_deletes(self):
        self.assertTrue({"PIVOTAL_CHECK", "PIVOTAL_SETUP", "PIVOTAL_START", "PIVOTAL_PRESET", "PIVOTAL_LEVEL"}
                        <= set(init.VARIABLES))

    def test_every_variable_init_writes_is_one_that_remove_deletes(self):
        self.gh.route("actions/variables/", fakegh.not_found())
        req = init.request(
            REPO, check="npm test", setup="npm ci", start="npm start", preset="claude-sub", level="ask",
        )
        writes = init._plan_variables(req, Gh(str(self.gh.path), self.gh.env()))
        self.assertEqual({w.name for w in writes}, {
            "PIVOTAL_CHECK", "PIVOTAL_SETUP", "PIVOTAL_START", "PIVOTAL_PRESET", "PIVOTAL_LEVEL",
        })
        self.assertLessEqual({w.name for w in writes}, set(init.VARIABLES))

    def test_every_variable_the_caller_reads_is_one_that_remove_deletes(self):
        used = set(re.findall(r"\bvars\.([A-Z][A-Z0-9_]*)", CALLER_TEMPLATE))
        self.assertIn("PIVOTAL_PRESET", used)
        self.assertLessEqual(used, set(init.VARIABLES), "a variable the caller reads that remove would miss")


if __name__ == "__main__":
    unittest.main()
