"""piv preset and piv level: every write goes through gh, and gh is the fake here.

A refusal makes no gh call at all. The preset check reads the repo's secret names before it writes, and
a secret's value never reaches an argument, an output line, an error or gh's environment.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import fakegh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))

from piv import cli, init  # noqa: E402

REPO = "0xctx/piv-sandbox"
SECRETS_PATH = f"repos/{REPO}/actions/secrets?per_page=100"
PRESET = init.VAR_PRESET
LEVEL = init.VAR_LEVEL
SECRET_VALUE = "sk-live-do-not-print-0123456789"  # a fake value: the tests check that it never shows


def secrets_listed(*names: str) -> dict:
    return fakegh.ok(json.dumps({"secrets": [{"name": name} for name in names]}))


class VarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "bin")
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    def piv(self, *args: str, env: dict[str, str] | None = None) -> int:
        return cli.main(list(args), self.gh.env() if env is None else env, self.out, self.err)

    def listed(self, *names: str) -> None:
        self.gh.route(SECRETS_PATH, secrets_listed(*names))
        self.gh.route("variable set", fakegh.ok())

    def writes(self) -> list[dict]:
        return [c for c in self.gh.calls() if c["args"][:2] == ["variable", "set"]]

    def reads(self) -> list[dict]:
        return [c for c in self.gh.calls() if c["args"][:1] == ["api"]]


class PresetRefusalTest(VarTest):
    def test_a_name_that_is_not_a_preset_is_refused_before_any_gh_call(self):
        self.listed()
        for name in ("Claude Sub", "claude_sub", "Claude-sub", "", "x" * 41):
            with self.subTest(name=name):
                self.assertEqual(self.piv("preset", REPO, name), 2)
                self.assertIn("PRESET must be a lowercase name", self.err.getvalue())
        self.assertEqual(self.gh.calls(), [])

    def test_a_repo_that_is_not_owner_slash_name_is_refused_before_any_gh_call(self):
        self.listed()
        self.assertEqual(self.piv("preset", "not-a-repo", "claude-sub"), 2)
        self.assertIn("REPO must be owner/name", self.err.getvalue())
        self.assertEqual(self.gh.calls(), [])

    def test_the_secret_list_that_cannot_be_read_stops_the_write(self):
        self.gh.route(SECRETS_PATH, fakegh.fail("gh: Server Error (HTTP 500)\n"))
        self.gh.route("variable set", fakegh.ok())
        self.assertEqual(self.piv("preset", REPO, "claude-sub"), 1)
        self.assertEqual(self.writes(), [])

    def test_a_secret_list_without_a_list_stops_the_write(self):
        self.gh.route(SECRETS_PATH, fakegh.ok("{}"))
        self.gh.route("variable set", fakegh.ok())
        self.assertEqual(self.piv("preset", REPO, "claude-sub"), 1)
        self.assertEqual(self.writes(), [])


class PresetWarningTest(VarTest):
    def test_a_complete_repo_gets_no_warning_and_the_preset_is_written(self):
        self.listed(*init.SECRETS)
        self.assertEqual(self.piv("preset", REPO, "claude-sub"), 0, self.err.getvalue())
        self.assertNotIn("warning", self.out.getvalue())
        self.assertIn("none of Pivotal's secrets is missing", self.out.getvalue())
        self.assertEqual(
            [(c["args"], c["stdin"]) for c in self.writes()],
            [(["variable", "set", PRESET, "--repo", REPO], "claude-sub")],
        )

    def test_a_missing_secret_is_named_as_a_warning_and_the_preset_is_still_written(self):
        self.listed("CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(self.piv("preset", REPO, "claude-sub"), 0, self.err.getvalue())
        warnings = [line for line in self.out.getvalue().splitlines() if line.startswith("warning: secret ")]
        self.assertEqual(
            [line.split()[2] for line in warnings],
            [name for name in init.SECRETS if name != "CLAUDE_CODE_OAUTH_TOKEN"],
        )
        self.assertEqual(len(self.writes()), 1)

    def test_the_warnings_come_before_the_write(self):
        self.listed("CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(self.piv("preset", REPO, "claude-sub"), 0, self.err.getvalue())
        text = self.out.getvalue()
        self.assertLess(text.index("warning: secret ANTHROPIC_API_KEY"), text.index(f"variable {PRESET}: set to"))

    def test_a_missing_secret_says_to_set_it_in_the_environment_when_it_is_not_there(self):
        self.listed()
        self.piv("preset", REPO, "claude-sub", env=self.gh.env())
        self.assertIn(
            f"warning: secret ANTHROPIC_API_KEY is not set on {REPO}; set it in your environment, "
            f"then gh piv init {REPO} --check '<your check>' writes it",
            self.out.getvalue(),
        )

    def test_a_secret_that_is_only_in_the_environment_is_still_missing_from_the_repo(self):
        self.listed()
        env = self.gh.env(ANTHROPIC_API_KEY=SECRET_VALUE)
        self.assertEqual(self.piv("preset", REPO, "claude-sub", env=env), 0, self.err.getvalue())
        self.assertIn(
            f"warning: secret ANTHROPIC_API_KEY is not set on {REPO}; it is in your environment, "
            f"so gh piv init {REPO} --check '<your check>' writes it",
            self.out.getvalue(),
        )

    def test_a_blank_secret_in_the_environment_is_not_advised_as_present(self):
        self.listed()
        env = self.gh.env(ANTHROPIC_API_KEY="   ")
        self.piv("preset", REPO, "claude-sub", env=env)
        self.assertIn("warning: secret ANTHROPIC_API_KEY is not set on", self.out.getvalue())
        self.assertIn("set it in your environment, then", self.out.getvalue())

    def test_no_secret_value_reaches_output_errors_or_any_gh_call(self):
        self.listed()
        env = self.gh.env(ANTHROPIC_API_KEY=SECRET_VALUE, CLAUDE_CODE_OAUTH_TOKEN=SECRET_VALUE)
        self.assertEqual(self.piv("preset", REPO, "claude-sub", env=env), 0, self.err.getvalue())
        self.assertNotIn(SECRET_VALUE, self.out.getvalue())
        self.assertNotIn(SECRET_VALUE, self.err.getvalue())
        self.assertNotIn(SECRET_VALUE, json.dumps(self.gh.calls()))

    def test_gh_runs_without_any_pivotal_secret_in_its_environment(self):
        self.listed()
        env = self.gh.env(ANTHROPIC_API_KEY=SECRET_VALUE, KAGGLE_KEY=SECRET_VALUE)
        self.piv("preset", REPO, "claude-sub", env=env)
        environments = self.gh.environments()
        self.assertTrue(environments)
        for seen in environments:
            self.assertNotIn("ANTHROPIC_API_KEY", seen)
            self.assertNotIn("KAGGLE_KEY", seen)

    def test_the_value_goes_on_stdin_and_never_as_an_argument(self):
        self.listed(*init.SECRETS)
        self.piv("preset", REPO, "claude-sub")
        for call in self.writes():
            self.assertNotIn("claude-sub", call["args"])
            self.assertEqual(call["stdin"], "claude-sub")


class LevelTest(VarTest):
    def test_a_level_outside_the_four_is_refused_before_any_gh_call(self):
        self.gh.route("variable set", fakegh.ok())
        for name in ("auto", "Land", "stop", ""):
            with self.subTest(name=name):
                self.assertEqual(self.piv("level", REPO, name), 2)
                self.assertIn("LEVEL must be one of watch, ask, prepare, land", self.err.getvalue())
        self.assertEqual(self.gh.calls(), [])

    def test_a_bad_repo_is_refused_before_any_gh_call(self):
        self.gh.route("variable set", fakegh.ok())
        self.assertEqual(self.piv("level", "no-slash", "land"), 2)
        self.assertEqual(self.gh.calls(), [])

    def test_each_level_is_written_to_pivotal_level_on_stdin(self):
        self.gh.route("variable set", fakegh.ok())
        for name in init.LEVELS:
            with self.subTest(level=name):
                self.assertEqual(self.piv("level", REPO, name), 0, self.err.getvalue())
        writes = self.writes()
        self.assertEqual([c["stdin"] for c in writes], list(init.LEVELS))
        for call in writes:
            self.assertEqual(call["args"][2], LEVEL)
            self.assertEqual(call["args"][3:], ["--repo", REPO])

    def test_a_level_does_not_read_or_check_secrets(self):
        self.gh.route("variable set", fakegh.ok())
        self.assertEqual(self.piv("level", REPO, "land"), 0, self.err.getvalue())
        self.assertEqual(self.reads(), [])
        self.assertIn(f"variable {LEVEL}: set to land", self.out.getvalue())

    def test_a_failed_write_is_reported_and_exits_non_zero(self):
        self.gh.route("variable set", fakegh.fail("gh: Forbidden (HTTP 403)\n"))
        self.assertEqual(self.piv("level", REPO, "watch"), 1)
        self.assertIn("HTTP 403", self.err.getvalue())
