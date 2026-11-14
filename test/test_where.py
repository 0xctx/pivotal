"""Where the jobs run: where.select_runner, engine/where.py and the runs-on wiring in factory.yml."""

import itertools
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_caller import parse
from test_factory import FACTORY

from engine import where

ROOT = Path(__file__).resolve().parent.parent
HOSTED, SELF = "ubuntu-24.04", "self-hosted"


def env(runner="self-hosted", trusted="true", private="true", public="") -> dict[str, str]:
    return {"RUNNER_VAR": runner, "TRUSTED": trusted, "REPO_PRIVATE": private, "RUNNER_PUBLIC": public}


class SelectRunnerTest(unittest.TestCase):
    def test_self_hosted_needs_all_three_conditions(self):
        cases = [
            (env(private="true"), SELF),
            (env(private="false", public="trusted-only"), SELF),
            (env(private="true", public="trusted-only"), SELF),
            (env(private="false"), HOSTED),
            (env(trusted="false", private="true"), HOSTED),
            (env(trusted="false", private="false", public="trusted-only"), HOSTED),
            (env(runner="", private="true"), HOSTED),
            (env(runner="hosted", private="true"), HOSTED),
        ]
        for given, want in cases:
            with self.subTest(given=given):
                self.assertEqual(where.select_runner(given), want)

    def test_an_untrusted_run_is_never_self_hosted_whatever_else_is_set(self):
        for runner, private, public in itertools.product(("self-hosted", "", "x"), ("true", "false", ""), ("trusted-only", "", "yes")):
            with self.subTest(runner=runner, private=private, public=public):
                self.assertEqual(where.select_runner(env(runner, "false", private, public)), HOSTED)

    def test_anything_unreadable_is_hosted(self):
        for bad in ({}, env(private=""), env(private="True"), env(private="null"), env(trusted="True"), env(trusted=""),
                    env(runner="Self-Hosted"), env(runner="self-hosted ", private="true"), env(private="false", public="Trusted-Only")):
            with self.subTest(bad=bad):
                self.assertEqual(where.select_runner(bad), HOSTED)


class WhereScriptTest(unittest.TestCase):
    def run_where(self, given):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            proc = subprocess.run([sys.executable, "-I", str(ROOT / "engine" / "where.py")], capture_output=True, text=True,
                                  env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out), **given}, check=False)
            return proc.returncode, out.read_text(encoding="utf-8") if out.exists() else ""

    def test_an_untrusted_issue_on_a_public_repo_with_self_hosted_set_runs_hosted(self):
        self.assertEqual(self.run_where(env(trusted="false", private="false")), (0, f"runner={HOSTED}\n"))
        self.assertEqual(self.run_where(env(trusted="false", private="false", public="trusted-only")), (0, f"runner={HOSTED}\n"))

    def test_a_trusted_run_on_a_private_repo_is_self_hosted(self):
        self.assertEqual(self.run_where(env()), (0, f"runner={SELF}\n"))

    def test_a_trusted_run_on_a_public_repo_needs_trusted_only(self):
        self.assertEqual(self.run_where(env(private="false")), (0, f"runner={HOSTED}\n"))
        self.assertEqual(self.run_where(env(private="false", public="trusted-only")), (0, f"runner={SELF}\n"))

    def test_an_empty_environment_is_hosted(self):
        self.assertEqual(self.run_where({}), (0, f"runner={HOSTED}\n"))


PLAN_RUNS_ON = "${{ needs.ack.outputs.quiet == 'true' && 'ubuntu-24.04' || needs.ack.outputs.runner }}"


class WorkflowWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = parse(FACTORY.read_text(encoding="utf-8"))["jobs"]

    def test_ack_and_report_are_always_hosted_and_the_others_take_the_choice(self):
        self.assertEqual(self.jobs["ack"]["runs-on"], HOSTED)
        self.assertEqual(self.jobs["report"]["runs-on"], HOSTED)
        self.assertEqual(self.jobs["build"]["runs-on"], "${{ needs.ack.outputs.runner }}")
        self.assertEqual(self.jobs["plan"]["runs-on"], PLAN_RUNS_ON)  # a quiet run needs no runner
        for job_id in ("plan", "build"):
            self.assertIn("ack", self.jobs[job_id]["needs"])

    def test_the_choice_is_made_in_ack_from_the_variables_the_trust_flag_and_the_repo(self):
        self.assertEqual(self.jobs["ack"]["outputs"]["runner"], "${{ steps.where.outputs.runner }}")
        step = next(s for s in self.jobs["ack"]["steps"] if s.get("id") == "where")
        self.assertEqual(step["run"], "python3 -I pivotal/engine/where.py")
        self.assertEqual(step["env"], {
            "RUNNER_VAR": "${{ vars.PIVOTAL_RUNNER }}",
            "RUNNER_PUBLIC": "${{ vars.PIVOTAL_RUNNER_PUBLIC }}",
            "TRUSTED": "${{ inputs.trusted }}",
            "REPO_PRIVATE": "${{ github.event.repository.private }}",
        })

    def test_the_build_contains_by_descendants_on_a_self_hosted_runner(self):
        build = next(s for s in self.jobs["build"]["steps"] if s.get("id") == "build")["env"]
        self.assertEqual(build["CONTAIN_SCOPE"], "${{ needs.ack.outputs.runner == 'self-hosted' && 'descendants' || 'user' }}")
        self.assertEqual(build["PIVOTAL_CONTAIN_USER_OK"], "${{ needs.ack.outputs.runner == 'self-hosted' && '0' || '1' }}")


if __name__ == "__main__":
    unittest.main()
