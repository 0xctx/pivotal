"""factory.yml, read as a file: the ack and plan jobs' permissions, pins and wiring.

Uses the YAML subset reader of test_caller.py. These are the F1 and F2 rules of the plan, checked on the
workflow itself, plus the wiring between the workflow's env and the names the engine reads.
"""

import importlib.util
import json
import math
import re
import sys
import unittest
from pathlib import Path

from test_caller import parse

import nolive  # noqa: F401  (no test sweeps the live /proc)
from engine import buildjob, gatejob, modelsup, plan, reviewjob, route

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))
from piv import init  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FACTORY = ROOT / ".github" / "workflows" / "factory.yml"
SHA_PIN = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}")
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


def steps_of(job):
    return job["steps"]


LOCAL_SETUP = "./pivotal/engine/setup"  # the engine's composite action, from the engine checkout at engine_sha


def _load_install():
    """engine/setup/install.py as a module (its directory is not a package): the manifest name is read from it, not copied."""
    spec = importlib.util.spec_from_file_location("install_under_test", ROOT / "engine" / "setup" / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FactoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FACTORY.read_text(encoding="utf-8")
        cls.doc = parse(cls.text)
        cls.jobs = cls.doc["jobs"]

    def step(self, job_id, step_id):
        return next(s for s in steps_of(self.jobs[job_id]) if s.get("id") == step_id)

    def test_it_has_the_ack_plan_build_review_assemble_gate_publish_and_report_jobs(self):
        self.assertEqual(set(self.jobs), {"ack", "plan", "models-up", "build", "review", "assemble", "gate", "publish", "report"})

    def test_the_workflow_holds_no_permission_and_each_job_names_its_own(self):
        self.assertEqual(self.doc["permissions"], {})
        self.assertEqual(self.jobs["ack"]["permissions"], {"issues": "write"})
        self.assertEqual(self.jobs["plan"]["permissions"], {"contents": "read", "issues": "read"})
        self.assertEqual(self.jobs["report"]["permissions"], {"issues": "write"})
        self.assertEqual(self.jobs["build"]["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["review"]["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["assemble"]["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["publish"]["permissions"], {"contents": "write", "pull-requests": "write", "checks": "write"})
        self.assertEqual(self.jobs["gate"]["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["models-up"]["permissions"], {"contents": "read"})

    def test_f2_the_invariant_behind_the_seal_only_publish_holds_checks_write(self):
        """PINS the invariant the land job's trust in the `pivotal/gate` seal rests on. seal.verify binds a check run to its
        run's check suite but does not establish who created it (a second pivotal run with a suite of its own passes: see
        test_land's p3 test). What keeps a forged seal out is that nobody who runs repo code or model output can create a check
        run: only the publish job holds `checks: write`, and it runs neither. Every job names its permissions (none inherits
        a wider default), and the caller grants `checks: write` only to the jobs that call factory.yml and to nothing that
        runs repo code itself."""
        self.assertEqual(self.doc["permissions"], {})
        holders = set()
        for job_id, job_ in self.jobs.items():
            self.assertIn("permissions", job_, f"{job_id} must name its permissions")
            if job_["permissions"].get("checks") == "write":
                holders.add(job_id)
        self.assertEqual(holders, {"publish"})
        for job_id in ("plan", "build", "review", "gate", "assemble", "ack", "report"):
            self.assertNotIn(self.jobs[job_id]["permissions"].get("checks"), ("write",), f"{job_id} runs repo code or model output, or reads it")
        for step in steps_of(self.jobs["publish"]):
            self.assertNotIn("matrix", step)
        self.assertNotIn("repo", {s.get("with", {}).get("path") for s in steps_of(self.jobs["publish"])}, "publish checks out no repo")
        caller = parse((ROOT / "templates" / "caller.yml").read_text(encoding="utf-8"))
        for job_id, job_ in caller["jobs"].items():
            if job_.get("permissions", {}).get("checks") == "write":
                self.assertTrue(job_["uses"].startswith("__PIVOTAL_ENGINE__/.github/workflows/factory.yml@"), f"caller job {job_id} holds checks: write")
        land = parse((ROOT / ".github" / "workflows" / "land.yml").read_text(encoding="utf-8"))
        self.assertEqual(land["jobs"]["land"]["permissions"]["checks"], "read")
        for other in (ROOT / ".github" / "workflows").glob("*.yml"):
            if other.name in ("factory.yml", "land.yml"):
                continue
            doc = parse(other.read_text(encoding="utf-8"))
            for job_ in doc.get("jobs", {}).values():
                self.assertNotEqual(job_.get("permissions", {}).get("checks"), "write", other.name)

    def test_plan_build_review_and_gate_are_read_only(self):
        for job_id in ("plan", "build", "review", "gate"):
            self.assertNotIn("write", self.jobs[job_id]["permissions"].values())

    def test_ack_runs_no_repo_code(self):
        for step in steps_of(self.jobs["ack"]):
            uses = step.get("uses", "")
            if uses:
                self.assertTrue(uses.startswith("actions/checkout@"))
                self.assertEqual(step["with"]["repository"], "${{ inputs.engine_repo }}", "ack checks out the engine only")
            else:
                self.assertTrue(
                    step["run"].lstrip().startswith(("python3 -I pivotal/engine/ack.py", "python3 -I pivotal/engine/where.py", "# Fail closed", "[[")),
                    step["run"],
                )

    def test_report_runs_always_after_the_pipeline_and_runs_no_repo_code(self):
        job = self.jobs["report"]
        self.assertEqual(job["if"], "always()")
        self.assertEqual(job["needs"], ["ack", "plan", "models-up", "build", "review", "assemble", "gate", "publish"])
        for step in steps_of(job):
            if step.get("uses"):
                self.assertTrue(step["uses"].startswith("actions/checkout@"))
                self.assertEqual(step["with"]["repository"], "${{ inputs.engine_repo }}", "report checks out the engine only")
            else:
                self.assertTrue(
                    step["run"].lstrip().startswith(("python3 -I pivotal/engine/report.py post", "[[")), step["run"]
                )

    def test_report_takes_quiet_from_ack_and_not_the_level(self):
        env = steps_of(self.jobs["report"])[2]["env"]
        self.assertEqual(env["QUIET"], "${{ needs.ack.outputs.quiet }}")
        self.assertNotIn("LEVEL", env)

    def test_ack_outputs_quiet(self):
        self.assertEqual(self.jobs["ack"]["outputs"]["quiet"], "${{ steps.ack.outputs.quiet }}")

    def test_every_pipeline_job_that_exists_is_wired_into_report(self):
        """The forcing function for the next tickets: a job added here must be in report's needs and env.

        TODO(build, review, assemble, gate, publish tickets): each producer lands its job with an output, and adds
        ``<JOB>_RESULT: ${{ needs.<job>.result }}`` to report's env; the job that ends the pipeline also sets
        ``RESULT: ${{ needs.<job>.outputs.result }}`` (engine/report.py documents the shape). Until then RESULT is
        absent and every run reports ``failed (no_result)``.
        """
        report_job = self.jobs["report"]
        env = steps_of(report_job)[2]["env"]
        for stage in ("build", "review", "assemble", "gate", "publish"):
            if stage in self.jobs:
                with self.subTest(stage=stage):
                    self.assertIn(stage, report_job["needs"])
                    self.assertEqual(env[f"{stage.upper()}_RESULT"], f"${{{{ needs.{stage}.result }}}}")
        if "publish" in self.jobs:
            self.assertEqual(env["RESULT"], "${{ needs.publish.outputs.result }}")
            self.assertIn("result", self.jobs["publish"]["outputs"])
        else:
            self.assertNotIn("RESULT", env)

    def test_report_holds_no_secret_but_the_ntfy_topic_variable(self):
        env = steps_of(self.jobs["report"])[2]["env"]
        self.assertEqual(env["PIVOTAL_NTFY"], "${{ vars.PIVOTAL_NTFY }}")
        self.assertEqual(env["GH_TOKEN"], "${{ github.token }}")
        for name, value in env.items():
            self.assertNotIn("secrets.", value, name)

    def test_every_action_is_pinned_by_sha(self):
        uses = [s["uses"] for j in self.jobs.values() for s in steps_of(j) if "uses" in s]
        self.assertTrue(uses)
        for use in uses:
            with self.subTest(use=use):
                if use.split()[0] == LOCAL_SETUP:  # the engine's own action, in the engine checkout at the pinned engine_sha
                    continue
                self.assertRegex(use.split()[0], SHA_PIN)

    def test_the_only_local_action_is_the_worker_setup_of_build_and_review(self):
        local = [(job_id, s.get("id")) for job_id, job in self.jobs.items() for s in steps_of(job)
                 if s.get("uses", "").startswith("./")]
        self.assertEqual(sorted(local), [("build", "setup"), ("review", "setup")])
        for job_id in ("build", "review"):
            self.assertEqual(self.step(job_id, "setup")["uses"], LOCAL_SETUP)

    def test_the_worker_setup_runs_after_the_engine_checkout_and_before_the_agent_in_build_and_review(self):
        for job_id, agent in (("build", "build"), ("review", "review")):
            steps = steps_of(self.jobs[job_id])
            ids = [s.get("id") for s in steps]
            with self.subTest(job=job_id):
                self.assertLess(1, ids.index("setup"), "after the engine checkout (step 1)")
                self.assertLess(ids.index("setup"), ids.index(agent), "before the agent step")
                setup = steps[ids.index("setup")]
                self.assertEqual(set(setup["env"]), {"PRESET", "LINE", "PRESET_JSON"})
                self.assertNotIn("secrets.", str(setup))
                self.assertNotIn("github.token", str(setup))
                self.assertNotIn("persist-credentials", str(setup))

    def test_the_manifest_the_setup_writes_is_the_one_the_agent_steps_read(self):
        install = _load_install()
        for job_id in ("build", "review"):
            with self.subTest(job=job_id):
                agent = self.step(job_id, job_id)
                self.assertEqual(agent["env"]["PIVOTAL_WORKERS"], "${{ runner.temp }}/" + install.MANIFEST)

    def test_the_checkout_sha_is_the_one_ci_pins(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        pinned = re.search(r"actions/checkout@([0-9a-f]{40})", ci).group(1)
        for use in re.findall(r"actions/checkout@([0-9a-f]{40})", self.text):
            self.assertEqual(use, pinned)

    def test_no_checkout_keeps_credentials(self):
        for job_id, job in self.jobs.items():
            for step in steps_of(job):
                if step.get("uses", "").startswith("actions/checkout@"):
                    with self.subTest(job=job_id, step=step["name"]):
                        self.assertEqual(step["with"]["persist-credentials"], "false")

    def test_engine_sha_is_a_required_input_the_caller_passes(self):
        spec = self.doc["on"]["workflow_call"]["inputs"]["engine_sha"]
        self.assertEqual((spec["type"], spec["required"]), ("string", "true"))

    def test_engine_repo_is_a_required_input_the_caller_passes(self):
        spec = self.doc["on"]["workflow_call"]["inputs"]["engine_repo"]
        self.assertEqual((spec["type"], spec["required"]), ("string", "true"))

    def test_no_workflow_file_or_the_template_holds_the_old_literal(self):
        for name in ("factory.yml", "land.yml"):
            with self.subTest(file=name):
                self.assertNotIn(init.LEGACY_ENGINE, (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
        self.assertNotIn(init.LEGACY_ENGINE, (ROOT / "templates" / "caller.yml").read_text(encoding="utf-8"))

    def test_the_workflow_reads_no_job_context(self):
        # A reusable workflow cannot read its own SHA from the job context; the caller passes engine_sha.
        self.assertNotRegex(self.text, r"\$\{\{[^}]*\bjob\b")
        self.assertNotIn("workflow_sha", self.text)

    def test_the_engine_is_checked_out_at_the_callers_pinned_sha_and_failing_closed(self):
        for job_id in ("ack", "plan", "models-up", "build", "review", "assemble", "gate", "publish", "report"):
            named = self.step(job_id, "engine")
            self.assertEqual(named["env"], {"ENGINE_SHA": "${{ inputs.engine_sha }}"})
            self.assertIn("exit 1", named["run"])
            self.assertIn("0" * 40, named["run"], "the placeholder SHA is refused")
            self.assertIn("{40}", named["run"])
            checkout = steps_of(self.jobs[job_id])[1]
            self.assertEqual(checkout["with"]["repository"], "${{ inputs.engine_repo }}")
            self.assertEqual(checkout["with"]["ref"], "${{ steps.engine.outputs.sha }}")
            self.assertEqual(checkout["with"]["path"], "pivotal")

    def test_no_expression_is_inside_a_run_script(self):
        for job_id, job in self.jobs.items():
            for step in steps_of(job):
                with self.subTest(job=job_id, step=step.get("name")):
                    self.assertNotIn("${{", step.get("run", ""))

    def test_untrusted_values_reach_the_engine_through_env(self):
        ack = self.step("ack", "ack")["env"]
        for name in ("KIND", "TRUSTED", "ISSUE", "ISSUE_PIN", "PRESET"):
            self.assertRegex(ack[name], r"^\$\{\{ inputs\.\w+ \}\}$")
        self.assertEqual(ack["GH_TOKEN"], "${{ github.token }}")

    def test_the_baseline_step_holds_no_token_and_no_secret(self):
        step = self.step("plan", "baseline")
        for name, value in step["env"].items():
            with self.subTest(name=name):
                self.assertNotIn("token", value.lower())
                self.assertNotIn("secrets.", value)
        self.assertNotIn("GH_TOKEN", step["env"])

    def test_only_the_steps_that_run_an_agent_or_reach_kaggle_carry_secrets(self):
        for job_id, job in self.jobs.items():
            for step in steps_of(job):
                holds = "secrets." in str(step.get("env", {})) + str(step.get("with", {}))
                with self.subTest(job=job_id, step=step.get("name")):
                    self.assertEqual(holds, (job_id, step.get("id")) in (("build", "build"), ("review", "review"), ("models-up", "up")))

    def test_the_build_step_holds_exactly_the_door_secrets_and_no_github_token(self):
        env = self.step("build", "build")["env"]
        doors = {name for names in route.DOOR_SECRETS.values() for name in names}
        held = {name for name, value in env.items() if "secrets." in value}
        # The door secrets and the Kaggle endpoint's URL, which the endpoint re-check needs (engine/buildjob.py).
        self.assertEqual(held, doors | {"PIVOTAL_KAGGLE_URL"})
        for name in held:
            self.assertEqual(env[name], "${{ secrets." + name + " }}")
        self.assertNotIn("github.token", str(env))
        self.assertNotIn("GH_TOKEN", env)
        for step in steps_of(self.jobs["build"]):
            self.assertNotIn("github.token", str(step))

    def test_the_review_step_holds_exactly_the_door_secrets_and_no_github_token(self):
        env = self.step("review", "review")["env"]
        doors = {name for names in route.DOOR_SECRETS.values() for name in names}
        self.assertEqual({name for name, value in env.items() if "secrets." in value}, doors | {"PIVOTAL_KAGGLE_URL"})
        for name in doors:
            self.assertEqual(env[name], "${{ secrets." + name + " }}")
        self.assertNotIn("GH_TOKEN", env)
        for step in steps_of(self.jobs["review"]):
            self.assertNotIn("github.token", str(step))
        self.assertEqual(env["CONTAIN_SCOPE"], "user")
        self.assertEqual(env["PIVOTAL_CONTAIN_USER_OK"], "1")

    def test_review_runs_after_build_on_a_fresh_job_per_slice_and_is_not_fail_fast(self):
        review = self.jobs["review"]
        self.assertEqual(review["needs"], ["ack", "plan", "models-up", "build"])
        # review reads models-up's KAGGLE_WHY (a refused push skips its kaggle reviewers as quota), but its condition stays
        # build's without the models-up clause: a skipped or failed models-up never stops a review.
        self.assertEqual(review["if"], self.jobs["build"]["if"].replace(" && needs.models-up.result != 'cancelled'", ""))
        self.assertEqual(review["strategy"], self.jobs["build"]["strategy"])
        self.assertEqual(review["runs-on"], "ubuntu-24.04")

    def test_the_review_step_execs_the_engine_and_no_later_step_runs_engine_code(self):
        steps = steps_of(self.jobs["review"])
        index = next(i for i, s in enumerate(steps) if s.get("id") == "review")
        self.assertEqual(steps[index]["run"].strip().splitlines()[-1], "exec python3 -I pivotal/engine/reviewjob.py run")
        for later in steps[index + 1:]:
            self.assertNotIn("pivotal", later.get("run", ""), later)
        upload, last = steps[index + 1], steps[-1]
        self.assertEqual(upload["if"], "steps.review.outcome == 'success' && steps.review.outputs.published == 'true'")
        self.assertEqual(upload["with"]["name"], "review-${{ matrix.slice.id }}")
        self.assertEqual(upload["with"]["path"], "${{ runner.temp }}/review/out")
        self.assertEqual((upload["with"]["overwrite"], upload["with"]["if-no-files-found"]), ("true", "error"))
        self.assertEqual(last["if"], "steps.review.outcome == 'success' && steps.review.outputs.review_ok != 'true'")
        self.assertTrue(last["run"].rstrip().endswith("exit 1"))
        self.assertEqual(set(last["env"]), {"SLICE_ID"})

    def test_review_reads_the_slice_artifact_and_checks_out_the_commit_and_the_base(self):
        steps = steps_of(self.jobs["review"])
        prepare = self.step("review", "prepare")
        self.assertEqual(prepare["run"], "python3 -I pivotal/engine/reviewjob.py prepare")
        self.assertNotIn("secrets.", str(prepare))
        repo, base = (s for s in steps if s.get("uses", "").startswith("actions/checkout@") and s["with"]["path"] in ("repo", "base"))
        self.assertEqual((repo["with"]["ref"], repo["with"]["path"]), ("${{ steps.prepare.outputs.ref }}", "repo"))
        # R2: in a fix round base_sha is the previous round's draft head (model output); the context files come from the default branch
        self.assertEqual((base["with"]["ref"], base["with"]["path"]), ("${{ needs.plan.outputs.context_sha }}", "base"))
        download = next(s for s in steps if s.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["name"], "slice-${{ matrix.slice.id }}")
        env = self.step("review", "review")["env"]
        self.assertEqual(download["with"]["path"], env["SLICE_DIR"])
        self.assertEqual(env["BASE_DIR"], "${{ github.workspace }}/base")
        self.assertEqual(env["REPO_DIR"], "${{ github.workspace }}/repo")
        # the artifact is downloaded before the step that reads it, and before any secret is in play
        self.assertLess(steps.index(download), steps.index(self.step("review", "review")))

    def test_build_runs_after_a_successful_plan_with_a_matrix_and_is_not_fail_fast(self):
        build = self.jobs["build"]
        self.assertEqual(build["needs"], ["ack", "plan", "models-up"])
        self.assertTrue(build["if"].startswith("!cancelled() && needs.plan.result == 'success'"))
        self.assertIn("needs.models-up.result != 'cancelled'", build["if"], "a skipped or failed models-up does not stop a build")
        self.assertNotIn("always()", build["if"])
        self.assertIn("needs.plan.outputs.matrix != ''", build["if"])
        self.assertEqual(build["strategy"]["fail-fast"], "false")
        self.assertEqual(build["strategy"]["matrix"]["slice"], "${{ fromJSON(needs.plan.outputs.matrix) }}")

    def test_the_build_step_execs_the_engine_and_no_later_step_runs_engine_code(self):
        steps = steps_of(self.jobs["build"])
        index = next(i for i, s in enumerate(steps) if s.get("id") == "build")
        build = steps[index]
        self.assertEqual(build["run"].strip().splitlines()[-1], "exec python3 -I pivotal/engine/buildjob.py run")
        self.assertIn("'1'", build["env"]["PIVOTAL_CONTAIN_USER_OK"], "only a hosted runner may contain by user")
        # V: the agent may have edited the checkout, so nothing after the build step runs a file from it.
        for later in steps[index + 1:]:
            self.assertNotIn("pivotal", later.get("run", ""), later)
        upload, last = steps[index + 1], steps[-1]
        self.assertEqual(upload["if"], "steps.build.outcome == 'success' && steps.build.outputs.published == 'true'")
        # The engine's own `build_ok` decides; a killed engine leaves the build step failed, which fails the job.
        self.assertEqual(last["if"], "steps.build.outcome == 'success' && steps.build.outputs.build_ok != 'true'")
        self.assertTrue(last["run"].rstrip().endswith("exit 1"))
        self.assertEqual(set(last["env"]), {"SLICE_ID"})

    def test_the_matrix_is_capped_at_six_slices(self):
        self.assertEqual(plan.MAX_SLICES, 6)
        with self.assertRaises(plan.SliceError):
            plan.check_slices([{"id": f"s{i}", "title": "t", "goal": "g"} for i in range(7)])

    def test_the_build_repo_checkout_is_the_commit_prepare_names(self):
        checkout = steps_of(self.jobs["build"])[3]
        self.assertEqual(checkout["with"]["ref"], "${{ steps.prepare.outputs.ref }}")
        self.assertEqual(checkout["with"]["path"], "repo")

    def test_the_build_reads_its_agent_context_from_a_base_checkout_of_the_default_branch(self):
        # R15: a fix round's tree is the draft head, where round 0's patch may have changed CLAUDE.md and its kin; the build
        # restores them from this checkout (BASE_DIR) before its agent starts, as the review does.
        base = self.step_named("build", "Check out the base, the source of the agent-context files")
        self.assertEqual((base["with"]["ref"], base["with"]["path"]), ("${{ needs.plan.outputs.context_sha }}", "base"))
        self.assertEqual(base["with"]["persist-credentials"], "false")
        self.assertEqual(self.step_named("build", "Build the slice")["env"]["BASE_DIR"], "${{ github.workspace }}/base")

    def test_the_slice_artifact_upload_follows_the_plan(self):
        step = steps_of(self.jobs["build"])[-2]
        # Round 4 B: the output line alone is forgeable (the agent appends it and kills the engine).
        self.assertEqual(step["if"], "steps.build.outcome == 'success' && steps.build.outputs.published == 'true'")
        self.assertEqual(step["with"]["name"], "slice-${{ matrix.slice.id }}")
        self.assertEqual(step["with"]["overwrite"], "true")
        self.assertEqual(step["with"]["retention-days"], "7")
        self.assertEqual(step["with"]["if-no-files-found"], "error")
        # Only the out directory goes up; the raw logs are a sibling.
        build_env = self.step("build", "build")["env"]
        self.assertEqual(step["with"]["path"], build_env["WORK_DIR"].replace("${{ runner.temp }}/slice", "${{ runner.temp }}/slice/out"))
        self.assertNotIn("logs", step["with"]["path"])

    def test_r30_a_fix_rounds_context_sha_is_never_the_draft_head(self):
        # base_sha is the previous round's draft head in a fix round; context_sha must come from the default-branch checkout
        # first and may fall back to base_sha only when that step did not run (which is every round that is not a fix round).
        context = self.step("plan", "context")
        text = json.dumps(context["with"])
        for forbidden in ("base_ref", "head_sha", "base_sha", "steps.repo", "steps.baseline", "client_payload"):
            self.assertNotIn(forbidden, text)
        expression = self.jobs["plan"]["outputs"]["context_sha"]
        self.assertTrue(expression.startswith("${{ steps.context.outputs.commit ||"), expression)
        self.assertEqual(context["if"], "steps.decide.outputs.base_ref != ''", "it runs exactly when a draft head is the base")

    def test_the_repo_is_checked_out_only_when_work_is_allowed(self):
        for step_id in ("repo", "baseline"):
            self.assertEqual(self.step("plan", step_id)["if"], "steps.decide.outputs.matrix != ''")
        self.assertEqual(self.step("plan", "repo")["with"]["path"], "repo")

    def test_a_fix_round_gets_its_context_from_the_default_branch_and_runs_no_baseline(self):
        context = self.step("plan", "context")
        self.assertEqual(context["if"], "steps.decide.outputs.base_ref != ''")
        self.assertEqual(context["with"]["ref"], "${{ github.event.repository.default_branch }}")
        self.assertEqual(self.jobs["plan"]["outputs"]["context_sha"], "${{ steps.context.outputs.commit || steps.baseline.outputs.base_sha }}")
        self.assertEqual(self.step("plan", "baseline")["env"]["BASE_REF"], "${{ steps.decide.outputs.base_ref }}")

    def test_the_plan_outputs_are_the_ones_the_plan_names(self):
        self.assertEqual(set(self.jobs["plan"]["outputs"]), {"base_sha", "context_sha", "baseline", "matrix", "round", "issue_pin", "plan_why", "score", "target", "scores", "kaggle_needed", "kaggle_model", "line", "reviewer", "level"})
        self.assertEqual(set(self.jobs["ack"]["outputs"]), {"runner", "issue", "quiet"})

    def test_a_skipped_baseline_reads_as_none(self):
        self.assertIn("|| 'none'", self.jobs["plan"]["outputs"]["baseline"])

    def test_plan_needs_ack_and_reads_the_issue_ack_names(self):
        self.assertEqual(self.jobs["plan"]["needs"], "ack")
        self.assertEqual(self.step("plan", "decide")["env"]["ISSUE"], "${{ needs.ack.outputs.issue }}")

    def test_timeouts_are_derived_from_the_budgets(self):
        minutes = math.ceil(plan.BASELINE_SECONDS / 60) + plan.PLAN_JOB_OVERHEAD_MINUTES
        self.assertEqual(int(self.jobs["plan"]["timeout-minutes"]), minutes)
        self.assertEqual(int(self.jobs["ack"]["timeout-minutes"]), 5)
        self.assertEqual(int(self.jobs["build"]["timeout-minutes"]), buildjob.JOB_MINUTES)
        self.assertEqual(int(self.jobs["review"]["timeout-minutes"]), reviewjob.JOB_MINUTES)
        self.assertEqual(int(self.jobs["gate"]["timeout-minutes"]), gatejob.JOB_MINUTES)
        self.assertGreaterEqual(gatejob.JOB_MINUTES * 60, gatejob.GATE_SECONDS + gatejob.JOB_OVERHEAD_MINUTES * 60)
        # The setup step is inside the build and review timeouts too (F8): each budget carries its term, not only the agents.
        self.assertEqual(buildjob.JOB_MINUTES, math.ceil(((buildjob.MAX_ENTRY_TRIES + 1) * buildjob.AGENT_SECONDS
                                                          + 2 * buildjob.GATE_SECONDS) / 60)
                         + buildjob.JOB_OVERHEAD_MINUTES + buildjob.SETUP_MINUTES)
        self.assertEqual(reviewjob.JOB_MINUTES, math.ceil(reviewjob.MAX_ENTRY_TRIES * reviewjob.AGENT_SECONDS / 60)
                         + reviewjob.JOB_OVERHEAD_MINUTES + buildjob.SETUP_MINUTES)
        self.assertGreaterEqual(
            buildjob.JOB_MINUTES * 60,
            (buildjob.MAX_ENTRY_TRIES + 1) * buildjob.AGENT_SECONDS + 2 * buildjob.GATE_SECONDS
            + buildjob.JOB_OVERHEAD_MINUTES * 60,
        )

    def test_the_env_names_the_engine_reads_are_all_set_by_the_workflow(self):
        runner = {"GITHUB_OUTPUT", "GITHUB_EVENT_PATH", "GITHUB_EVENT_NAME", "GITHUB_STEP_SUMMARY", "GITHUB_SERVER_URL", "RUNNER_TEMP", "PATH"}
        sets = {
            ("ack", "ack"): {"GH_TOKEN", "GH_REPO", "KIND", "TRUSTED", "ISSUE", "ISSUE_PIN", "PRESET", "PRESET_JSON", "RUN_ID",
                           "RUN_NUMBER", "RUN_ATTEMPT", "ENGINE_SHA", "LEVEL", "CHECK", "RUNNER"},
            ("ack", "where"): {"RUNNER_VAR", "RUNNER_PUBLIC", "TRUSTED", "REPO_PRIVATE"},
            ("plan", "decide"): {"GH_TOKEN", "GH_REPO", "KIND", "TRUSTED", "ISSUE", "ISSUE_PIN", "PRESET", "PRESET_JSON", "LEVEL",
                                 "CHECK", "ENGINE_SHA"},
            ("plan", "baseline"): {"BASE_SHA", "BASE_REF", "REPO_DIR", "LOG", "SETUP", "CHECK", "LEVEL"},
            ("publish", "publish"): {"GH_TOKEN", "GH_REPO", "ISSUE", "BASE_SHA", "MATRIX", "ASSEMBLE_RESULT", "LAND_SHA256",
                                     "DEFAULT_BRANCH", "RUN_ID", "RUN_NUMBER", "RUN_ATTEMPT", "LAND_DIR", "WORK_DIR", "GATE_RESULT",
                                     "REVIEW_RESULT", "HAS_STACK", "KIND", "TRUSTED", "LEVEL", "PLANNED_LEVEL", "CHECK", "REVIEWER", "GATES_DIR", "REVIEWS_DIR", "SLICES_DIR", "ROUND", "ISSUE_PIN", "TARGET", "SCORES"},
            ("assemble", "assemble"): {"ISSUE", "BASE_SHA", "MATRIX", "BASELINE", "KIND", "REPO_DIR", "SLICES_DIR", "REVIEWS_DIR", "OUT_DIR", "WORK_DIR"},
            ("build", "prepare"): {"SLICE", "ROUND", "BASE_SHA", "ISSUE", "SCORE", "TARGET"},
            ("build", "build"): {"SLICE", "ROUND", "BASE_SHA", "ISSUE", "SCORE", "TARGET", "PRESET", "LINE", "PRESET_JSON", "SETUP", "CHECK", "REPO_DIR",
                                 "BASE_DIR", "WORK_DIR", "ENGINE_DIR", "CONTAIN_SCOPE", "PIVOTAL_CONTAIN_USER_OK", "PIVOTAL_KAGGLE_URL",
                                 "KAGGLE_WHY", "PIVOTAL_WORKERS",
                                 *(n for names in route.DOOR_SECRETS.values() for n in names)},
            ("gate", "prepare"): {"ID", "ISSUE", "BASE_SHA", "LAND_SHA256", "LAND_DIR", "REPO_DIR", "WORK_DIR"},
            ("gate", "gate"): {"ID", "ISSUE", "BASE_SHA", "SCORE", "TARGET", "LAND_SHA256", "LAND_DIR", "LINE", "PRESET_JSON", "SETUP", "CHECK", "REPO_DIR",
                               "WORK_DIR", "ENGINE_DIR", "CONTAIN_SCOPE", "PIVOTAL_CONTAIN_USER_OK"},
            ("review", "prepare"): {"SLICE", "ROUND", "BASE_SHA", "ISSUE", "SCORE"},
            ("review", "review"): {"SLICE", "ROUND", "BASE_SHA", "ISSUE", "SCORE", "PRESET", "LINE", "PRESET_JSON", "REPO_DIR", "BASE_DIR",
                                   "SLICE_DIR", "WORK_DIR", "ENGINE_DIR", "CONTAIN_SCOPE", "PIVOTAL_CONTAIN_USER_OK",
                                   "PIVOTAL_KAGGLE_URL", "KAGGLE_WHY", "PIVOTAL_WORKERS",
                                   *(n for names in route.DOOR_SECRETS.values() for n in names)},
        }
        report_env = {"GH_TOKEN", "GH_REPO", "KIND", "TRUSTED", "ISSUE", "RUN_ID", "RUN_NUMBER", "RUN_ATTEMPT", "ENGINE_SHA",
                      "QUIET", "ACK_RESULT", "PLAN_RESULT", "PLAN_WHY", "BUILD_RESULT", "REVIEW_RESULT", "ASSEMBLE_RESULT", "GATE_RESULT", "PUBLISH_RESULT", "RESULT", "HAS_STACK",
                      "PIVOTAL_NTFY", "MODELS_UP_RESULT", "KAGGLE_WHY"}
        self.assertEqual(set(steps_of(self.jobs["report"])[2]["env"]), report_env)
        self.assertEqual(steps_of(self.jobs["report"])[2]["env"]["HAS_STACK"], "${{ needs.assemble.outputs.has_stack }}")
        for key, names in sets.items():
            with self.subTest(step=key):
                self.assertEqual(set(self.step(*key)["env"]), names)

        def read(*files):
            code = "".join((ROOT / "engine" / f).read_text(encoding="utf-8") for f in files)
            return set(re.findall(r'env\.get\("([A-Z0-9_]+)"', code))

        # RESULT is the publish job's output line, the last of the pipeline.
        # The stage results (BUILD_RESULT and so on) are read by name in a loop, so this scan does not see them.
        # ACK_RESULT, PLAN_RESULT and BUILD_RESULT are read the same way, by f"{stage.upper()}_RESULT" over report.STAGES.
        stage_env = {"ACK_RESULT", "PLAN_RESULT", "BUILD_RESULT", "REVIEW_RESULT", "ASSEMBLE_RESULT", "GATE_RESULT", "PUBLISH_RESULT"}
        self.assertEqual(read("report.py", "job.py") - runner, (report_env - {"GH_TOKEN"} - stage_env) | {"RESULT"})
        self.assertEqual(steps_of(self.jobs["report"])[2]["env"]["KAGGLE_WHY"], "${{ needs.models-up.outputs.kaggle_why }}")
        self.assertIn("models-up", self.jobs["report"]["needs"])
        self.assertEqual(read("ack.py", "job.py") - runner, sets[("ack", "ack")] - {"GH_TOKEN"})
        self.assertEqual(read("where.py") - runner, sets[("ack", "where")])
        plan_sets = sets[("plan", "decide")] | sets[("plan", "baseline")]
        self.assertEqual(read("plan.py", "job.py") - runner, plan_sets - {"GH_TOKEN", "RUN_ID", "RUN_NUMBER"})
        # buildjob reads the build steps' names (the door secrets by the names route.py lists); PRESET_JSON is read by
        # plan.load_preset, which it calls.
        build_sets = sets[("build", "prepare")] | sets[("build", "build")]
        doors = {n for names in route.DOOR_SECRETS.values() for n in names}
        # PIVOTAL_KAGGLE_URL is read by modelsup.recheck, which buildjob.recheck_endpoint calls.
        self.assertEqual(read("buildjob.py", "modelsup.py") - runner - {"PIV_KAGGLE", "PIVOTAL_KEYLESS_CHECK", "KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_MODEL"} - doors,
                         build_sets - doors)
        self.assertEqual(read("modelsup.py") - runner, set(self.step("models-up", "up")["env"]))
        self.assertIn("PRESET_JSON", read("plan.py"))
        # reviewjob reads its own directories itself; the slice, round, preset and base come through buildjob and plan.
        own = {"SLICE_DIR", "BASE_DIR", "REPO_DIR", "WORK_DIR", "ENGINE_DIR", "CONTAIN_SCOPE"}
        review_sets = sets[("review", "prepare")] | sets[("review", "review")]
        self.assertEqual(read("reviewjob.py") - runner, own | {"PRESET", "LINE", "PRESET_JSON", "PIVOTAL_WORKERS"})
        self.assertLessEqual(read("reviewjob.py") - runner, review_sets)
        self.assertEqual(review_sets - doors - {"PRESET_JSON"} - read("reviewjob.py") - read("buildjob.py") - read("modelsup.py"), set())
        # assemble reads its five paths through _absolute(env, NAME), the rest by env.get.
        paths = set(re.findall(r'_absolute\(env, "([A-Z_]+)"', (ROOT / "engine" / "assemble.py").read_text(encoding="utf-8")))
        self.assertEqual((read("assemble.py") | paths) - runner, sets[("assemble", "assemble")])
        # publish reads GH_REPO through job.read_repo, KIND and TRUSTED through job.read_kind and its five paths through
        # _path(env, NAME), the rest by env.get.
        publish_paths = set(re.findall(r'_path\(env, "([A-Z_]+)"', (ROOT / "engine" / "publish.py").read_text(encoding="utf-8")))
        self.assertEqual((read("publish.py") | publish_paths | {"GH_REPO", "KIND", "TRUSTED"}) - runner - {"PIV_GH"}, sets[("publish", "publish")])
        # gatejob reads every name of both its steps itself (through _directory for the paths) and holds no door secret.
        gate_sets = sets[("gate", "prepare")] | sets[("gate", "gate")]
        gate_paths = set(re.findall(r'_directory\(env, "([A-Z_]+)"', (ROOT / "engine" / "gatejob.py").read_text(encoding="utf-8")))
        # SETUP, CHECK, SCORE and the hosted-runner flag are read by buildjob (the gate and refuse_shared_host it calls).
        # LAND_SHA256 has a digit, which read()'s pattern does not match: check it by hand.
        self.assertIn('env.get("LAND_SHA256"', (ROOT / "engine" / "gatejob.py").read_text(encoding="utf-8"))
        self.assertEqual((read("gatejob.py") | gate_paths | {"LAND_SHA256"}) - runner,
                         gate_sets - {"SETUP", "CHECK", "PIVOTAL_CONTAIN_USER_OK", "SCORE"})
        self.assertLessEqual({"SETUP", "CHECK", "PIVOTAL_CONTAIN_USER_OK", "SCORE"}, read("buildjob.py"))

    def test_assemble_is_read_only_runs_no_repo_code_and_holds_no_token_or_secret(self):
        job_ = self.jobs["assemble"]
        self.assertEqual(job_["permissions"], {"contents": "read"})
        self.assertEqual(job_["needs"], ["ack", "plan", "build", "review"])
        self.assertEqual(job_["if"], "!cancelled() && needs.plan.result == 'success' && needs.plan.outputs.matrix != ''")
        self.assertNotIn("always()", job_["if"])
        text = json.dumps(job_)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("github.token", text)
        self.assertNotIn("GH_TOKEN", text)
        for step in steps_of(job_):
            if step.get("uses", "").startswith("actions/checkout@"):
                self.assertEqual(step["with"]["persist-credentials"], "false")
            run = step.get("run", "")
            if run:  # the only engine code is assemble.py; nothing from the repo checkout is ever run
                self.assertTrue(run.lstrip().startswith(("python3 -I pivotal/engine/assemble.py run", "[[", "mkdir")), run)
                self.assertNotIn("repo/", run)
                self.assertNotIn("${{", run)
        self.assertEqual(self.step("assemble", "assemble")["run"], "python3 -I pivotal/engine/assemble.py run")

    def test_assemble_reads_the_plans_outputs_and_the_slice_artifacts_and_uploads_land(self):
        steps = steps_of(self.jobs["assemble"])
        env = self.step("assemble", "assemble")["env"]
        self.assertEqual(env["ISSUE"], "${{ needs.ack.outputs.issue }}")
        self.assertEqual(env["BASE_SHA"], "${{ needs.plan.outputs.base_sha }}")
        self.assertEqual(env["MATRIX"], "${{ needs.plan.outputs.matrix }}")
        self.assertEqual(env["BASELINE"], "${{ needs.plan.outputs.baseline }}")
        checkout = next(s for s in steps if s.get("name") == "Check out the base commit")
        self.assertEqual((checkout["with"]["ref"], checkout["with"]["path"]), ("${{ needs.plan.outputs.base_sha }}", "repo"))
        downloads = [s for s in steps if s.get("uses", "").startswith("actions/download-artifact@")]
        self.assertEqual([d["with"]["pattern"] for d in downloads], ["slice-*", "review-*"])
        for download, name in zip(downloads, ("SLICES_DIR", "REVIEWS_DIR")):
            self.assertNotIn("merge-multiple", download["with"], "each artifact keeps its own <kind>-<id> directory")
            self.assertEqual(download["with"]["path"], env[name])
            self.assertNotIn("github-token", download["with"])
        self.assertEqual(
            self.jobs["assemble"]["outputs"],
            {name: f"${{{{ steps.assemble.outputs.{name} }}}}" for name in ("positions", "gate_matrix", "has_stack", "land_sha256")},
        )
        upload = steps[-1]
        self.assertEqual(upload["if"], "steps.assemble.outputs.assembled == 'true'")
        self.assertEqual((upload["with"]["name"], upload["with"]["path"]), ("land", env["OUT_DIR"]))
        self.assertEqual((upload["with"]["overwrite"], upload["with"]["if-no-files-found"]), ("true", "error"))
        self.assertEqual(upload["with"]["retention-days"], "7")
        self.assertEqual(set(self.jobs["report"]["needs"]) & {"assemble"}, {"assemble"})
        self.assertEqual(steps_of(self.jobs["report"])[2]["env"]["ASSEMBLE_RESULT"], "${{ needs.assemble.result }}")

    def test_publish_is_the_only_job_with_a_write_permission_besides_ack_and_report(self):
        writers = {job_id for job_id, j in self.jobs.items() if "write" in j["permissions"].values()}
        self.assertEqual(writers, {"ack", "report", "publish"})
        self.assertEqual({job_id for job_id, j in self.jobs.items() if "contents" in j["permissions"] and j["permissions"]["contents"] == "write"}, {"publish"})
        for job_id, j in self.jobs.items():  # only publish and the jobs that talk to issues hold the GitHub token
            holds = "github.token" in json.dumps(j)
            self.assertEqual(holds, job_id in ("ack", "plan", "publish", "report"), job_id)

    def test_publish_runs_after_assemble_never_when_cancelled_and_one_at_a_time_per_repo(self):
        job_ = self.jobs["publish"]
        self.assertEqual(job_["needs"], ["ack", "plan", "review", "assemble", "gate"])
        self.assertEqual(job_["if"], (
            "!cancelled() && needs.assemble.result == 'success' && (needs.gate.result == 'success' || needs.gate.result == 'failure' "
            "|| (needs.gate.result == 'skipped' && needs.assemble.outputs.has_stack == 'false'))"))
        self.assertNotIn("always()", job_["if"])
        self.assertEqual(job_["concurrency"], {"group": "pivotal-publish-${{ github.repository }}", "cancel-in-progress": "false", "queue": "max"})
        self.assertEqual(job_["runs-on"], "ubuntu-24.04")
        self.assertEqual(job_["outputs"], {"result": "${{ steps.publish.outputs.result }}"})
        self.assertGreaterEqual(int(job_["timeout-minutes"]), 5)

    def test_publish_runs_no_repo_code_checks_out_no_repo_and_execs_the_engine(self):
        steps = steps_of(self.jobs["publish"])
        for step in steps:
            uses = step.get("uses", "")
            if uses.startswith("actions/checkout@"):
                self.assertEqual(step["with"]["repository"], "${{ inputs.engine_repo }}", "only the engine is checked out")
                self.assertEqual(step["with"]["persist-credentials"], "false")
            run = step.get("run", "")
            if run:
                self.assertTrue(run.lstrip().startswith(("# Fail closed", "[[", "mkdir -p", "exec python3 -I pivotal/engine/publish.py run")), run)
        publish = self.step("publish", "publish")
        self.assertEqual(publish["run"].strip().splitlines()[-1], "exec python3 -I pivotal/engine/publish.py run")
        self.assertIs(steps[-1], publish, "nothing runs after the step that holds the token")
        self.assertEqual(publish["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertNotIn("secrets.", json.dumps(self.jobs["publish"]))

    def test_publish_reads_assembles_outputs_and_the_land_artifact(self):
        env = self.step("publish", "publish")["env"]
        self.assertEqual(env["LAND_SHA256"], "${{ needs.assemble.outputs.land_sha256 }}")
        self.assertEqual(env["ASSEMBLE_RESULT"], "${{ needs.assemble.result }}")
        self.assertEqual(env["BASE_SHA"], "${{ needs.plan.outputs.base_sha }}")
        self.assertEqual(env["ISSUE"], "${{ needs.ack.outputs.issue }}")
        download = next(s for s in steps_of(self.jobs["publish"]) if s.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["name"], "land")
        self.assertEqual(download["with"]["path"], env["LAND_DIR"])
        self.assertNotIn("github-token", download["with"])
        downloads = [x for x in steps_of(self.jobs["publish"]) if x.get("uses", "").startswith("actions/download-artifact@")]
        self.assertEqual([(x["with"].get("name") or x["with"]["pattern"], x["with"]["path"]) for x in downloads], [
            ("land", env["LAND_DIR"]), ("gate-*", env["GATES_DIR"]), ("review-*", env["REVIEWS_DIR"]), ("slice-*", env["SLICES_DIR"])])
        for x in downloads:
            self.assertNotIn("github-token", x["with"])
        self.assertEqual(env["GATE_RESULT"], "${{ needs.gate.result }}")
        self.assertEqual(env["REVIEW_RESULT"], "${{ needs.review.result }}")
        self.assertEqual(env["HAS_STACK"], "${{ needs.assemble.outputs.has_stack }}")
        self.assertEqual(self.jobs["assemble"]["outputs"]["land_sha256"], "${{ steps.assemble.outputs.land_sha256 }}")

    def test_publish_reads_the_trust_decision_only_to_refuse_landing_and_names_no_gh_override(self):
        """The caller still grants a stranger's `go` the write ceiling of this job, so publish itself decides what a run may
        do. Since the stack ticket it reads `TRUSTED`, the kind and the level, and only to DENY: the base moves and the seal
        is made only for a trusted run at the land level (engine/publish.py `Settings.may_land`, test_stack.LevelTest); a
        stranger gets a draft stack PR. Nothing in the job sets the test seam."""
        job_ = self.jobs["publish"]
        self.assertEqual(job_["permissions"], {"contents": "write", "pull-requests": "write", "checks": "write"})
        env = self.step("publish", "publish")["env"]
        self.assertEqual((env["TRUSTED"], env["KIND"], env["LEVEL"]), ("${{ inputs.trusted }}", "${{ inputs.kind }}", "${{ vars.PIVOTAL_LEVEL }}"))
        self.assertNotIn("PIV_GH", json.dumps(job_), "the test seam is never set by the workflow")

    def test_the_report_job_reads_the_plans_own_refusal_word(self):
        self.assertEqual(steps_of(self.jobs["report"])[2]["env"]["PLAN_WHY"], "${{ needs.plan.outputs.plan_why }}")
        self.assertEqual(self.jobs["plan"]["outputs"]["plan_why"], "${{ steps.decide.outputs.plan_why }}")

    def test_models_up_runs_only_for_a_kaggle_chain_holds_only_the_kaggle_secrets_and_no_github_token(self):
        job_ = self.jobs["models-up"]
        self.assertEqual(job_["needs"], ["ack", "plan"])
        self.assertEqual(job_["if"], "!cancelled() && needs.plan.result == 'success' && needs.plan.outputs.kaggle_needed == 'true'")
        self.assertEqual(job_["runs-on"], "ubuntu-24.04", "a GitHub-hosted runner, never your own")
        self.assertEqual(int(job_["timeout-minutes"]), modelsup.JOB_MINUTES)
        self.assertEqual(job_["permissions"], {"contents": "read"})
        text = json.dumps(job_)
        for forbidden in ("github.token", "GH_TOKEN", "GITHUB_TOKEN", "_API_KEY", "OAUTH_TOKEN", "id-token", "PRESET", "ISSUE", "SLICE"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(
            set(re.findall(r"secrets\.(\w+)", text)), {"PIVOTAL_KAGGLE_URL", "PIVOTAL_KAGGLE_KEY", "KAGGLE_USERNAME", "KAGGLE_KEY"}
        )
        up = self.step("models-up", "up")
        self.assertEqual(up["run"].strip(), "exec python3 -I pivotal/engine/modelsup.py")
        self.assertEqual(up["env"]["KAGGLE_MODEL"], "${{ needs.plan.outputs.kaggle_model }}")
        self.assertEqual(up["env"]["PIVOTAL_KEYLESS_CHECK"], "${{ vars.PIVOTAL_KEYLESS_CHECK }}", "the live check is the repo's choice")
        for step in steps_of(job_):
            if step.get("uses", "").startswith("actions/checkout@"):
                self.assertEqual(step["with"]["persist-credentials"], "false")
            if step.get("id") != "up":
                self.assertNotIn("secrets.", json.dumps(step), "the secrets are on the one step that needs them")
        self.assertEqual(job_["outputs"]["kaggle_ok"], "${{ steps.up.outputs.kaggle_ok || 'false' }}")
        self.assertIn("kaggle==2.2.4", self.step_named("models-up", "Install the Kaggle CLI")["run"])
        # kaggle 2.2.4 imports six without declaring it: a clean venv crashed at import (measured live)
        self.assertIn("six==1.17.0", self.step_named("models-up", "Install the Kaggle CLI")["run"])

    def step_named(self, job_id, name):
        return next(s for s in steps_of(self.jobs[job_id]) if s.get("name") == name)

    def test_builds_and_reviews_get_the_endpoint_url_for_the_recheck(self):
        for job_id, step_id in (("build", "build"), ("review", "review")):
            self.assertEqual(self.step(job_id, step_id)["env"]["PIVOTAL_KAGGLE_URL"], "${{ secrets.PIVOTAL_KAGGLE_URL }}")

    def test_builds_and_reviews_get_models_ups_word_so_a_refused_push_skips_kaggle_as_quota(self):
        for job_id, step_id in (("build", "build"), ("review", "review")):
            with self.subTest(job=job_id):
                self.assertEqual(self.step(job_id, step_id)["env"]["KAGGLE_WHY"], "${{ needs.models-up.outputs.kaggle_why }}")
                self.assertIn("models-up", self.jobs[job_id]["needs"])
        self.assertEqual(self.jobs["models-up"]["outputs"]["kaggle_why"], "${{ steps.up.outputs.kaggle_why }}")

    def test_gate_is_read_only_holds_no_token_or_secret_and_runs_only_the_engine_on_the_repo(self):
        job_ = self.jobs["gate"]
        self.assertEqual(job_["permissions"], {"contents": "read"})
        self.assertEqual(job_["needs"], ["ack", "plan", "assemble"])
        self.assertEqual(job_["if"], "!cancelled() && needs.assemble.result == 'success' && needs.assemble.outputs.has_stack == 'true'")
        self.assertEqual(job_["runs-on"], self.jobs["build"]["runs-on"])
        self.assertEqual(job_["strategy"]["matrix"], {"id": "${{ fromJSON(needs.assemble.outputs.gate_matrix) }}"})
        self.assertEqual(job_["strategy"]["fail-fast"], "false")
        text = json.dumps(job_)
        for forbidden in ("secrets.", "github.token", "GH_TOKEN", "_API_KEY", "OAUTH_TOKEN", "KAGGLE", "id-token"):
            self.assertNotIn(forbidden, text)
        for step in steps_of(job_):
            if step.get("uses", "").startswith("actions/checkout@"):
                self.assertEqual(step["with"]["persist-credentials"], "false")
            run = step.get("run", "")
            if run:
                self.assertTrue(
                    run.lstrip().startswith(("python3 -I pivotal/engine/gatejob.py", "exec python3 -I pivotal/engine/gatejob.py", "[[", "echo")), run
                )
                self.assertNotIn("repo/", run)

    def test_gate_runs_the_check_in_the_engine_process_on_the_stack_commit_and_uploads_only_the_gate_artifact(self):
        env = self.step("gate", "gate")["env"]
        self.assertEqual(self.step("gate", "gate")["run"], "exec python3 -I pivotal/engine/gatejob.py run")
        self.assertEqual(self.step("gate", "prepare")["run"], "python3 -I pivotal/engine/gatejob.py prepare")
        for step_id in ("prepare", "gate"):
            step_env = self.step("gate", step_id)["env"]
            self.assertEqual(step_env["ID"], "${{ matrix.id }}")
            self.assertEqual(step_env["LAND_SHA256"], "${{ needs.assemble.outputs.land_sha256 }}")
            self.assertEqual(step_env["ISSUE"], "${{ needs.ack.outputs.issue }}")
            self.assertEqual(step_env["BASE_SHA"], "${{ needs.plan.outputs.base_sha }}")
        steps = steps_of(self.jobs["gate"])
        base = next(s for s in steps if s.get("name") == "Check out the base commit")
        self.assertEqual((base["with"]["ref"], base["with"]["path"]), ("${{ needs.plan.outputs.base_sha }}", "repo"))
        download = next(s for s in steps if s.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["name"], "land")
        self.assertEqual(download["with"]["path"], env["LAND_DIR"])
        self.assertNotIn("github-token", download["with"])
        upload = steps[-2]
        self.assertEqual(upload["if"], "steps.gate.outcome == 'success' && steps.gate.outputs.published == 'true'")
        self.assertEqual(upload["with"]["name"], "gate-${{ matrix.id }}")
        self.assertEqual(upload["with"]["path"], env["WORK_DIR"] + "/out")
        self.assertEqual((upload["with"]["overwrite"], upload["with"]["if-no-files-found"]), ("true", "error"))
        self.assertEqual(upload["with"]["retention-days"], "7")
        fail = steps[-1]
        self.assertEqual(fail["if"], "steps.gate.outcome == 'success' && steps.gate.outputs.gate_ok != 'true'")
        self.assertEqual(self.jobs["gate"]["name"], "gate ${{ matrix.id }}")

    def test_the_gate_matrix_is_only_ever_assembles_validated_output(self):
        self.assertEqual(self.jobs["assemble"]["outputs"]["gate_matrix"], "${{ steps.assemble.outputs.gate_matrix }}")
        self.assertNotIn("positions", json.dumps(self.jobs["gate"]["strategy"]))


class WorkerSetupActionTest(unittest.TestCase):
    """engine/setup/action.yml runs install.py with a bare step: the step holds no secret and no token (F1, F3)."""

    ACTION = ROOT / "engine" / "setup" / "action.yml"

    def test_the_inner_step_env_is_exactly_the_action_dir_and_the_file_names_no_secret_or_token(self):
        text = self.ACTION.read_text(encoding="utf-8")
        steps = parse(text)["runs"]["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(set(steps[0]["env"]), {"ACTION_DIR"})
        self.assertNotIn("secrets.", text)
        self.assertNotIn("github.token", text)




if __name__ == "__main__":
    unittest.main()
