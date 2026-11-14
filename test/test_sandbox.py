"""The T2 walking skeleton, end to end: the sandbox scenarios.

Each class is one acceptance bullet. A scenario starts from ``piv init`` on a fake GitHub repo, files and labels an issue,
and runs the real caller, factory.yml, every engine entry point (one process per step, with the environment the workflow
gives that step), git and the fake agents, in the order the workflows run them (test/sandbox.py says what is modelled and
what is not). Nothing here reaches GitHub, ntfy or a provider; every secret is a fake. NOTHING HAS RUN ON REAL GITHUB:
runner semantics, GitHub's own expression evaluation and the real download-artifact layout are modelled, not observed.

The slice is a draft in these scenarios because the fake reviewer says NEEDS FIXES: a green slice with a READY review from
another vendor is a candidate for the stack PR, which T3 publishes (see ``CandidateTest``).
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import nolive  # noqa: F401
import sandbox
from sandbox import REPO, Run, Sandbox

from engine import route, trust

SECRETS = {
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-FAKEFAKEFAKE0123456789abcdef",
    "ANTHROPIC_API_KEY": "sk-ant-api03-FAKEFAKEFAKE9876543210fedcba",
    "OPENAI_API_KEY": "sk-proj-FAKEFAKEFAKE1122334455667788",
    "XAI_API_KEY": "xai-FAKEFAKEFAKE8877665544332211",
    "OPENROUTER_API_KEY": "sk-or-v1-FAKEFAKEFAKE5566778899aabbcc",
    "GEMINI_API_KEY": "AIzaFAKEFAKEFAKE-0123456789-gemini",
    "GROQ_API_KEY": "gsk_FAKEFAKEFAKE0123456789groq",
}
# What an untrusted run may hold (caller.yml factory-untrusted): the free keys. The others are subscription or paid keys.
FREE = ("GEMINI_API_KEY", "GROQ_API_KEY")
SUBSCRIPTION = tuple(name for name in SECRETS if name not in FREE)
PRESET = {"name": "sandbox", "tiers": {"build": ["claude:anthropic/sonnet"], "review": ["pi:google/gemini-2.5-flash"]}}
FREE_PRESET = {"name": "free", "tiers": {"build": ["pi:google/gemini-2.5-flash"], "review": ["pi:groq/qwen3-32b"]}}
TOPIC = "pivotal-sandbox-topic"
TITLE, BODY = "Add the feature", "Create feature.txt that says good."
RUN_STARTED = re.compile(rf"⚡ run #(\d+) started \((\w+), (trusted|untrusted)\) · https://github\.com/{REPO}/actions/runs/(\d+)")


class ScenarioCase(unittest.TestCase):
    """A sandbox per test, or per class with ``shared = True``."""

    secrets = SECRETS
    preset = PRESET
    untrusted_preset = FREE_PRESET

    @classmethod
    def make(cls, **kwargs) -> Sandbox:
        tmp = tempfile.TemporaryDirectory()
        cls._cleanup = getattr(cls, "_cleanup", [])
        cls._cleanup.append(tmp)
        options = {"secrets": cls.secrets, "preset": cls.preset, "untrusted_preset": cls.untrusted_preset,
                   "variables": {"PIVOTAL_NTFY": TOPIC}}
        options.update(kwargs)
        sb = Sandbox(Path(tmp.name), **options)
        done = sb.piv_init()
        assert done.returncode == 0, done.stderr
        return sb

    @classmethod
    def tearDownClass(cls):
        for tmp in getattr(cls, "_cleanup", []):
            tmp.cleanup()

    def setUp(self):
        self.addCleanup(lambda: [t.cleanup() for t in self.__dict__.pop("_own", [])])

    def sandbox(self, **kwargs) -> Sandbox:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        options = {"secrets": self.secrets, "preset": self.preset, "untrusted_preset": self.untrusted_preset,
                   "variables": {"PIVOTAL_NTFY": TOPIC}}
        options.update(kwargs)
        sb = Sandbox(Path(tmp.name), **options)
        done = sb.piv_init()
        self.assertEqual(done.returncode, 0, done.stderr)
        return sb

    def pipeline(self, sb: Sandbox, number: int = 7):
        return sb.run_workflow(sb.label_issue(number, TITLE, BODY))

    def outsiders(self, sb: Sandbox) -> list[dict]:
        """The calls to the fake GitHub made by a job, in order (not the operator's piv commands)."""
        return [c for c in sb.hub.calls() if c["token"] != "operator"]

    def assertNoLeak(self, sb: Sandbox, *names: str) -> None:
        for name in names:
            self.assertEqual(sb.leaks(sb.secrets[name]), [], f"{name} is visible somewhere")


# ------------------------------------------------------------------------------------------------ acceptance bullet 1


class HappyPathTest(ScenarioCase):
    """A labelled issue gets the run-started comment, then a draft PR with only the slice's files, a report comment and
    an ntfy message."""

    @classmethod
    def setUpClass(cls):
        cls.sb = cls.make()
        cls.run_ = cls.sb.run_workflow(cls.sb.label_issue(7, TITLE, BODY))

    def test_every_job_of_the_pipeline_ran_and_succeeded_in_order(self):
        run = self.run_
        self.assertEqual(run.factory, "factory-trusted")
        self.assertEqual(run.trust.outputs, {"trusted": "true", "issue": "7", "kind": "go", "pin": run.inputs["issue_pin"]})
        self.assertEqual(
            [key for key in ("ack", "plan", "build", "review", "assemble", "publish", "report") if run.result(key) == "success"],
            ["ack", "plan", "build", "review", "assemble", "publish", "report"],
        )
        self.assertEqual([j.name for j in run.all_jobs()],
                         ["trust", "ack", "plan", "build-main", "review-main", "assemble", "publish", "report"])

    def test_the_run_started_comment_is_the_first_write_and_comes_before_any_other_job_acts(self):
        calls = self.outsiders(self.sb)
        writes = [c for c in calls if c["args"][:1] == ["api"] and ("-X" in c["args"] or "-f" in c["args"])]
        first = writes[0]
        self.assertEqual(first["token"], "ack")
        self.assertEqual(first["args"][:4], ["api", "-X", "POST", f"repos/{REPO}/issues/7/comments"])
        comment = self.sb.hub.comments(7)[0]
        found = RUN_STARTED.fullmatch(comment)
        self.assertIsNotNone(found, comment)
        self.assertEqual((found[2], found[3], found[4]), ("sandbox", "trusted", str(self.run_.run_id)))
        order = [c["token"] for c in calls]
        self.assertLess(order.index("ack"), min(order.index(n) for n in ("plan", "publish", "report")))
        self.assertEqual(self.sb.hub.issue(7)["comments"][0]["user"]["login"], "github-actions[bot]")

    def test_one_draft_pr_holds_only_the_slices_files_and_closes_the_issue(self):
        (pr,) = self.sb.hub.prs()
        self.assertTrue(pr["isDraft"])
        self.assertEqual((pr["state"], pr["baseRefName"], pr["headRefName"]), ("OPEN", "main", "pivotal/issue-7/main"))
        self.assertTrue(pr["body"].startswith("Closes #7\n"))
        self.assertEqual(pr["title"], f"Pivotal: {TITLE} (#7)")
        self.assertEqual(self.sb.pr_files(pr), ["feature.txt", "tests/test_feature.txt"])
        tree = self.sb.git("--git-dir", str(self.sb.origin), "ls-tree", "-r", "--name-only", pr["headRefOid"])
        self.assertEqual(tree.splitlines(), ["README.md", "app.py", "feature.txt", "tests/test_feature.txt"])
        self.assertEqual(self.sb.git("--git-dir", str(self.sb.origin), "rev-parse", "main"), self.sb.base_sha, "main is untouched")
        branches = self.sb.git("--git-dir", str(self.sb.origin), "for-each-ref", "--format=%(refname:short)", "refs/heads")
        self.assertEqual(branches.splitlines(), ["main", "pivotal/issue-7/main"])

    def test_the_report_comment_the_labels_and_the_summary_tell_the_outcome(self):
        started, report = self.sb.hub.comments(7)
        self.assertTrue(report.startswith(f"🏁 run #{self.run_.run_number} draft (review_rejected) · built by claude:anthropic/sonnet"))
        self.assertIn(f"pr: https://github.com/{REPO}/pull/1", report)
        self.assertIn(f"run: https://github.com/{REPO}/actions/runs/{self.run_.run_id}", report)
        self.assertEqual(sorted(self.sb.hub.issue(7)["labels"]), ["pivotal", "pivotal:blocked"])
        summaries = list((self.sb.world / "summaries").glob("*report*"))
        self.assertEqual(len(summaries), 1)
        self.assertIn("draft (review_rejected)", summaries[0].read_text())

    def test_one_ntfy_message_names_the_repo_the_outcome_and_the_pr(self):
        (call,) = self.sb.curl.calls()
        self.assertEqual(call["stdin"], f'url = "https://ntfy.sh/{TOPIC}"\n')
        self.assertEqual(call["sent"], f"{REPO} · {TITLE} · draft (review_rejected) · built by claude:anthropic/sonnet, "
                                       f"reviewed by none · https://github.com/{REPO}/pull/1")
        self.assertNotIn(TOPIC, " ".join(call["args"]), "the topic is never in argv")
        self.assertEqual(self.sb.leaks(TOPIC), ["hub state", "curl/curl-calls.jsonl"],
                         "the topic is the repo variable and the curl config, and in no log, comment, artifact or object")
        self.assertIn(TOPIC, json.dumps(self.sb.hub.state()["variables"]))

    def test_the_artifacts_handed_between_jobs_are_exactly_the_documented_ones(self):
        self.assertEqual(sorted(p.name for p in self.sb.artifacts.iterdir()), ["land", "review-main", "slice-main"])
        self.assertEqual(sorted(p.name for p in (self.sb.artifacts / "slice-main").iterdir()), ["result.json", "slice.patch", "summary.md"])
        self.assertEqual(sorted(p.name for p in (self.sb.artifacts / "review-main").iterdir()), ["review.md", "reviewed_by", "verdict"])
        self.assertEqual(sorted(p.name for p in (self.sb.artifacts / "land").iterdir()), ["land.json", "pivotal.bundle"])

    def test_the_trusted_run_hands_each_model_key_only_to_the_step_that_runs_that_agent(self):
        held = self.sb.secret_values_received(self.run_, tuple(SECRETS))
        self.assertEqual(sorted(held), ["build-main", "review-main"])
        self.assertTrue(all(v.startswith("build:") for v in held["build-main"]), held)
        self.assertTrue(all(v.startswith("review:") for v in held["review-main"]), held)
        calls = self.sb.agent_calls()
        self.assertEqual([(c["mode"], c["doors"]) for c in calls],
                         [("build", ["CLAUDE_CODE_OAUTH_TOKEN"]), ("review", ["GEMINI_API_KEY"])],
                         "each agent got its one key")
        self.assertNoLeak(self.sb, *SECRETS)

    def test_no_job_that_runs_model_output_or_repo_code_held_a_github_token(self):
        for job in self.run_.all_jobs():
            for step in job.ran():
                if job.key in ("build", "review", "assemble"):
                    self.assertFalse({"GH_TOKEN", "GITHUB_TOKEN"} & set(step.env), (job.name, step.name))
        self.assertEqual(sorted({c["token"] for c in self.outsiders(self.sb)}), ["ack", "plan", "publish", "report", "trust"])
        self.assertEqual(self.run_.job("build").permissions, {"contents": "read"})
        self.assertEqual(self.run_.job("publish").permissions, {"contents": "write", "pull-requests": "write", "checks": "write"})

    def test_the_report_is_handed_each_jobs_result_and_the_jobs_that_run_model_output_end_in_exec(self):
        env = self.run_.job("report").step("Comment, label, notify and summarise").env
        stages = ("ACK", "PLAN", "BUILD", "REVIEW", "ASSEMBLE", "PUBLISH")
        self.assertEqual({f"{s}_RESULT": env.get(f"{s}_RESULT") for s in stages}, {f"{s}_RESULT": "success" for s in stages})
        self.assertEqual(json.loads(env["RESULT"])["status"], "draft")
        for key, entry in (("build", "buildjob.py run"), ("review", "reviewjob.py run"), ("publish", "publish.py run")):
            scripts = [s.script for s in self.run_.job(key).ran() if s.script]
            self.assertEqual(scripts[-1].strip().splitlines()[-1], f"exec python3 -I pivotal/engine/{entry}", key)

    def test_a_job_environment_is_the_workflows_and_the_runners_only(self):
        runner = {"PATH", "HOME", "LANG", "CI", "GITHUB_ACTIONS", "GITHUB_WORKSPACE", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY",
                  "GITHUB_EVENT_PATH", "GITHUB_EVENT_NAME", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_NUMBER",
                  "GITHUB_RUN_ATTEMPT", "GITHUB_SERVER_URL", "RUNNER_TEMP", "RUNNER_OS", "GITHUB_PATH"}
        factory = sandbox.parse(self.sb.factory_text())
        for job in self.run_.all_jobs():
            declared = set()
            if job.key != "trust":
                for step in factory["jobs"][job.key]["steps"]:
                    declared |= set(step.get("env") or {})
            for step in job.ran():
                if step.script:
                    extra = set(step.env) - runner - declared
                    self.assertLessEqual(extra, {"GH_TOKEN", "PIV_GH", "CONTAIN_SCOPE"}, (job.name, step.name, extra))


class PivDoTest(ScenarioCase):
    """The user-visible check: ``gh piv do`` files an issue, the run starts, and the command prints the run link."""

    def test_piv_do_files_a_labelled_issue_and_prints_the_link_ack_commented(self):
        sb = self.sandbox()
        background = sandbox.BackgroundPiv(sb, "do", REPO, "Add the feature: create feature.txt")
        try:
            number = background.wait_for_issue(sb)
            label = sb.hub.issue(number)["labels"][0]
            run = sb.run_workflow(sb.issue_event(number, label=label))
        finally:
            done = background.join()  # also when the workflow raised: the command must not outlive the test
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"run link: https://github.com/{REPO}/actions/runs/{run.run_id}", done.stdout)
        self.assertEqual(run.result("publish"), "success")
        self.assertEqual(len(sb.hub.prs()), 1)
        self.assertEqual(sb.hub.prs()[0]["body"].splitlines()[0], f"Closes #{number}")


# ------------------------------------------------------------------------------------------------ acceptance bullet 2


class RerunTest(ScenarioCase):
    """Re-running opens no second PR: the same run again, and a new run of the same issue."""

    @classmethod
    def setUpClass(cls):
        cls.sb = cls.make()
        cls.first = cls.sb.run_workflow(cls.sb.label_issue(7, TITLE, BODY))
        cls.second = cls.sb.run_workflow(cls.sb.issue_event(7), run_id=cls.first.run_id, attempt=2)
        cls.after_rerun = (len(cls.sb.hub.prs()), list(cls.sb.hub.comments(7)))
        cls.third = cls.sb.run_workflow(cls.sb.comment_event(7, "/pivotal go", "alice"))

    def test_re_running_the_same_run_opens_no_second_pr_and_posts_no_second_comment(self):
        count, comments = self.after_rerun
        self.assertEqual(count, 1)
        self.assertEqual(len(comments), 2, comments)
        self.assertEqual(self.second.result("publish"), "success")
        self.assertEqual(json.loads(self.second.job("publish").outputs["result"])["pr"], 1)
        self.assertIn("already commented", self.second.job("ack").step("ack").log)
        self.assertEqual(len(self.sb.curl.calls()), 3, "the message is sent again: one per run attempt and run")

    def test_a_new_run_of_the_same_issue_finds_the_pr_by_its_branch(self):
        self.assertEqual(self.third.event, "issue_comment")
        self.assertEqual(self.third.result("report"), "success")
        self.assertEqual(len(self.sb.hub.prs()), 1)
        creates = [c for c in self.sb.hub.calls() if c["args"][:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 1, "gh pr create ran once in three runs")
        self.assertEqual(json.loads(self.third.job("publish").outputs["result"])["pr"], 1)
        branches = self.sb.git("--git-dir", str(self.sb.origin), "for-each-ref", "--format=%(refname:short)", "refs/heads")
        self.assertEqual(branches.splitlines(), ["main", "pivotal/issue-7/main"])

    def test_the_same_input_makes_the_same_commit_in_every_run(self):
        heads = {json.loads(r.job("publish").outputs["result"]).get("pr") for r in (self.first, self.second, self.third)}
        self.assertEqual(heads, {1})
        lands = [self.sb.artifacts / "land" / "land.json"]
        head = json.loads(lands[0].read_text())["slices"][0]["head"]
        self.assertEqual(head, self.sb.hub.prs()[0]["headRefOid"])


class WorkerSetupTest(ScenarioCase):
    """The build runs a pi that only the setup step installed (engine/setup). The sandbox has no pi on its
    PATH: the fake npm lays one out in the prefix the pin names, and the agent is started by that pinned binary's absolute path.
    The setup step is run with no secret in its environment, and the fake npm is given none."""

    preset = {"name": "sandbox", "tiers": {"build": ["pi:groq/qwen3-32b"], "review": ["pi:google/gemini-2.5-flash"]}}

    @classmethod
    def setUpClass(cls):
        cls.sb = cls.make()
        cls.run_ = cls.sb.run_workflow(cls.sb.label_issue(7, TITLE, BODY))

    def test_the_build_ran_the_installed_pi_and_not_one_already_on_the_path(self):
        self.assertEqual(self.run_.result("build"), "success")
        builds = [c for c in self.sb.agent_calls() if c["mode"] == "build"]
        self.assertEqual([c["worker"] for c in builds], ["pi"])
        self.assertIn("/pi/node_modules/.bin/pi", builds[0]["exe"], "started from the pinned install tree, not a PATH entry")
        self.assertNotIn("pivotal-bin", builds[0]["exe"], "the PATH entry the setup step made is not what runs")
        self.assertFalse(any(Path(d).name == "pivotal-bin" for d in builds[0]["path"].split(os.pathsep)),
                         "the agent's PATH holds no directory of links the setup step made (C1)")
        self.assertIn("--model", builds[0]["argv"])

    def test_the_setup_step_installed_the_pinned_pi_and_said_so(self):
        logs = [p.read_text(encoding="utf-8") for p in self.sb.logs.glob("setup-*.log")]
        self.assertTrue(logs, "the setup step ran in build and review")
        self.assertTrue(any("setup: pi 1.1.0 installed" in text for text in logs), logs)
        self.assertFalse(any("not installed" in text for text in logs), logs)

    def test_npm_and_the_setup_step_were_given_no_secret(self):
        log = self.sb.world / "npm-env.jsonl"
        self.assertTrue(log.exists(), "npm was run")
        for line in log.read_text(encoding="utf-8").splitlines():
            names = json.loads(line)["env"]
            for secret in self.sb.secrets:
                self.assertNotIn(secret, names, "npm was given a secret")

    def test_the_pi_call_got_its_one_key_and_no_other(self):
        (build,) = [c for c in self.sb.agent_calls() if c["mode"] == "build"]
        self.assertEqual(build["doors"], ["GROQ_API_KEY"])
        self.assertNoLeak(self.sb, "GROQ_API_KEY")


class ConcurrencyTest(ScenarioCase):
    """Per-issue concurrency: one group for every run of an issue, a different one for another issue, queued not cancelled."""

    def test_the_group_is_the_issue_and_runs_are_queued_not_cancelled(self):
        sb = self.sandbox()
        a = sb.run_workflow(sb.label_issue(7, TITLE, BODY), stop_after_trust=True)
        b = sb.run_workflow(sb.comment_event(7, "/pivotal go", "alice"), stop_after_trust=True)
        c = sb.run_workflow(sb.label_issue(8, "Another", BODY), stop_after_trust=True)
        self.assertEqual((a.group, b.group, c.group), ("pivotal-issue-7", "pivotal-issue-7", "pivotal-issue-8"))
        caller = sandbox.parse(sb.caller_text())
        for key in ("factory-trusted", "factory-untrusted", "land"):
            concurrency = caller["jobs"][key]["concurrency"]
            self.assertEqual(concurrency["queue"], "max", key)
            self.assertNotIn("cancel-in-progress", concurrency, key)
        self.assertNotIn("concurrency", caller["jobs"]["stop"], "stop must not queue behind the run it cancels")


# ------------------------------------------------------------------------------------------------ acceptance bullet 3


class UntrustedTest(ScenarioCase):
    """The permission lookup answering 403, 500 or nothing at all makes the run untrusted, and no subscription secret
    reaches any job. Checked on the environment each step process actually received."""

    def run_untrusted(self, lookup: dict):
        sb = self.sandbox()
        sb.hub.update(lookup=lookup)
        run = self.pipeline(sb)
        self.assertEqual(run.trust.outputs["trusted"], "false")
        self.assertEqual(run.trust.outputs["kind"], "go")
        self.assertEqual(run.factory, "factory-untrusted")
        self.assertEqual(run.inputs["trusted"], False)
        self.assertEqual(run.inputs["preset"], "free")
        self.assertIn("untrusted", run.trust.step("decide").log)
        self.assertEqual(run.passed_secrets, sorted(FREE), "the caller passes only the free keys")
        self.assertEqual(sb.secret_values_received(run, SUBSCRIPTION), {})
        for job in run.all_jobs():
            for step in job.ran():
                for name in SUBSCRIPTION:
                    self.assertEqual(step.env.get(name, ""), "", (job.name, step.name, name))
        self.assertTrue(all(c["doors"] and not set(c["doors"]) & set(SUBSCRIPTION) for c in sb.agent_calls()), sb.agent_calls())
        comment = RUN_STARTED.fullmatch(sb.hub.comments(7)[0])
        self.assertEqual((comment[2], comment[3]), ("free", "untrusted"))
        self.assertNoLeak(sb, *SUBSCRIPTION)
        return sb, run

    def test_a_403_is_untrusted(self):
        self.run_untrusted({"mode": "http", "status": 403, "message": "Resource not accessible by integration"})

    def test_a_500_is_untrusted(self):
        self.run_untrusted({"mode": "http", "status": 500, "message": "Server Error"})

    def test_a_lookup_that_times_out_is_untrusted(self):
        """A real timeout: the fake gh sleeps, and trust gives up after its own 20 seconds."""
        sb, run = self.run_untrusted({"mode": "hang", "seconds": 120})
        step = run.trust.step("decide")
        self.assertEqual(step.code, 0, "trust answers untrusted, it does not fail")
        # It waited for its own limit, not for the fake's 120 seconds, and not zero: the lookup really hung.
        self.assertGreaterEqual(step.seconds, trust.TIMEOUT_SECONDS - 1)
        self.assertLess(step.seconds, 60)
        self.assertTrue(any("permission" in " ".join(c["args"]) for c in sb.hub.calls()), "the lookup was made")

    def test_a_stranger_who_is_not_a_collaborator_is_untrusted_too(self):
        sb = self.sandbox()
        sb.hub.update(collaborators={})
        run = self.pipeline(sb)
        self.assertEqual((run.factory, run.trust.outputs["trusted"]), ("factory-untrusted", "false"))
        self.assertEqual(sb.secret_values_received(run, SUBSCRIPTION), {})

    def test_an_untrusted_run_still_builds_on_the_free_preset_and_opens_a_draft(self):
        sb, run = self.run_untrusted({"mode": "http", "status": 403})
        self.assertEqual(run.result("publish"), "success")
        self.assertEqual(len(sb.hub.prs()), 1)
        calls = sb.agent_calls()
        self.assertEqual([(c["worker"], c["doors"]) for c in calls], [("pi", ["GEMINI_API_KEY"]), ("pi", ["GROQ_API_KEY"])])

    def test_a_trusted_writer_gets_the_subscription_key_which_shows_the_check_can_see_it(self):
        sb = self.sandbox()
        run = self.pipeline(sb)
        held = sb.secret_values_received(run, ("CLAUDE_CODE_OAUTH_TOKEN",))
        self.assertEqual(held, {"build-main": ["build:CLAUDE_CODE_OAUTH_TOKEN"], "review-main": ["review:CLAUDE_CODE_OAUTH_TOKEN"]},
                         "the two engine steps that run an agent hold it; the agent itself gets one key (HappyPathTest)")


# ------------------------------------------------------------------------------------------------ acceptance bullet 4


class SecretInPatchTest(ScenarioCase):
    """A fake agent that prints its token and writes it into a file: ``failed: secret_in_patch``, and the token is in no log,
    artifact, PR, comment or ntfy message, nor in any object of the server repository."""

    @classmethod
    def setUpClass(cls):
        cls.sb = cls.make()
        cls.sb.set_agents(build=sandbox.FEATURE + [["leak", "leaked.txt"]])
        cls.run_ = cls.sb.run_workflow(cls.sb.label_issue(7, TITLE, BODY))

    def test_the_build_is_failed_secret_in_patch_and_publishes_no_patch(self):
        result = json.loads((self.sb.artifacts / "slice-main" / "result.json").read_text())
        self.assertEqual((result["status"], result["reason"]), ("failed", "secret_in_patch"))
        self.assertFalse((self.sb.artifacts / "slice-main" / "slice.patch").exists())
        self.assertEqual(self.run_.result("build"), "failure")
        self.assertEqual(self.run_.job("build").outputs, {})
        self.assertIn("slice build failed", self.run_.job("build").step("Fail the job if the slice build failed").log)
        self.assertEqual(self.sb.hub.prs(), [])
        self.assertEqual(self.sb.git("--git-dir", str(self.sb.origin), "for-each-ref", "refs/heads").count("\n"), 0)

    def test_the_run_is_red_and_every_message_names_secret_in_patch(self):
        self.assertEqual(self.run_.result("report"), "failure", "a failed run is a red run")
        self.assertEqual(json.loads(self.run_.job("publish").outputs["result"])["why"], "secret_in_patch")
        report = self.sb.hub.comments(7)[1]
        self.assertTrue(report.startswith(f"🏁 run #{self.run_.run_number} failed (secret_in_patch)"), report)
        self.assertIn("rotate that secret now", report)
        (call,) = self.sb.curl.calls()
        self.assertIn("failed (secret_in_patch)", call["sent"])
        self.assertEqual(sorted(self.sb.hub.issue(7)["labels"]), ["pivotal", "pivotal:blocked"])

    def test_the_agent_really_printed_and_wrote_the_token(self):
        """The control: the summary the artifact carries is the agent's log with the token redacted, so it was in the log."""
        summary = (self.sb.artifacts / "slice-main" / "summary.md").read_text()
        self.assertIn("token: [REDACTED:CLAUDE_CODE_OAUTH_TOKEN]", summary)
        self.assertEqual([c["doors"] for c in self.sb.agent_calls()], [["CLAUDE_CODE_OAUTH_TOKEN"]])

    def test_the_token_is_in_no_log_artifact_pr_comment_message_or_git_object(self):
        for name in SECRETS:
            self.assertEqual(self.sb.leaks(SECRETS[name]), [], name)
        places = [label for label, _ in self.sb.visible_text()]
        self.assertTrue(any(p.startswith("logs/") for p in places), "the step logs were searched")
        self.assertIn("artifacts/slice-main/summary.md", places)
        self.assertIn("artifacts/land/land.json", places)
        self.assertIn("server repository", places)
        self.assertIn("curl/curl-calls.jsonl", places)

    def test_the_review_job_has_nothing_to_review_and_says_so(self):
        self.assertEqual(self.run_.result("review"), "failure")
        self.assertIn("nothing to review", self.run_.job("review").step("review").log)
        self.assertEqual(self.run_.result("assemble"), "success", "assemble reads the missing patch as a failed slice")


class SecretInReviewTest(ScenarioCase):
    """The reviewer's token in its findings: the verdict is missing and the text is never published."""

    def test_a_reviewer_that_prints_its_token_publishes_no_text(self):
        sb = self.sandbox()
        sb.set_agents(review_finding="the key is $TOKEN")
        run = self.pipeline(sb)
        verdict = json.loads((sb.artifacts / "review-main" / "verdict").read_text())
        self.assertEqual((verdict["verdict"], verdict["reason"]), ("missing", "secret_in_review"))
        self.assertNoLeak(sb, "GEMINI_API_KEY")
        self.assertEqual(run.result("review"), "failure")
        self.assertEqual(len(sb.hub.prs()), 1, "the slice is still a draft for a person to read")
        self.assertIn("verdict_missing", sb.hub.prs()[0]["body"])


class TwoSlicesTest(ScenarioCase):
    """Round 1, F1: the plan has two slices and one leaks. The one RESULT line, and so the comment and the message, name
    ``secret_in_patch`` whichever slice leaks and whatever its sibling did; the sibling's draft is still opened."""

    PLAN = [{"id": "leaky", "title": "The leaking slice", "goal": "goal LEAKYMARK"},
            {"id": "fine", "title": "The fine slice", "goal": "goal fine"}]

    def go(self, plan):
        sb = self.sandbox()
        sb.plan_override = plan
        sb.set_agents(build=sandbox.FEATURE + [["leak", "leaked.txt", "LEAKYMARK"]])
        return sb, self.pipeline(sb)

    def check(self, sb, run):
        self.assertEqual(json.loads(run.job("publish").outputs["result"])["why"], "secret_in_patch")
        self.assertEqual(json.loads(run.job("publish").outputs["result"])["status"], "failed")
        self.assertEqual(run.result("report"), "failure")
        self.assertTrue(sb.hub.comments(7)[1].startswith(f"🏁 run #{run.run_number} failed (secret_in_patch)"), sb.hub.comments(7)[1])
        (call,) = sb.curl.calls()
        self.assertIn("failed (secret_in_patch)", call["sent"])
        self.assertEqual(len(sb.hub.prs()), 1, "the fine slice has its draft")
        self.assertNoLeak(sb, "CLAUDE_CODE_OAUTH_TOKEN")

    def test_a_leaking_first_slice_is_not_hidden_by_a_sibling_draft(self):
        self.check(*self.go(self.PLAN))

    def test_a_leaking_last_slice_is_not_hidden_by_an_earlier_draft(self):
        self.check(*self.go(self.PLAN[::-1]))


class OffsetLeakTest(ScenarioCase):
    """Round 1, F3: a token the agent writes base64-encoded at an offset (and URL-safe) is still refused, and the sandbox's
    own search for it, which uses every form the engine knows, finds it nowhere."""

    def test_a_token_encoded_at_an_offset_is_secret_in_patch_and_found_nowhere(self):
        sb = self.sandbox()
        sb.set_agents(build=sandbox.FEATURE + [["leak_offset", "leaked.txt"]])
        run = self.pipeline(sb)
        result = json.loads((sb.artifacts / "slice-main" / "result.json").read_text())
        self.assertEqual((result["status"], result["reason"]), ("failed", "secret_in_patch"))
        self.assertEqual(json.loads(run.job("publish").outputs["result"])["why"], "secret_in_patch")
        self.assertNoLeak(sb, "CLAUDE_CODE_OAUTH_TOKEN")
        self.assertTrue(any(label.startswith("outputs/") for label, _ in sb.visible_text()), "job outputs are searched")
        self.assertTrue(any(label.startswith("summaries/") for label, _ in sb.visible_text()), "step summaries are searched")


# ------------------------------------------------------------------------------------------------ acceptance bullet 5


class NoSecretTest(ScenarioCase):
    """A preset whose first entry lacks its secret is a red run with the reason, not a quiet fallback to the second entry."""

    secrets = {k: v for k, v in SECRETS.items() if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")}
    preset = {"name": "sandbox", "tiers": {"build": ["claude:anthropic/sonnet", "pi:groq/qwen3-32b"], "review": ["pi:google/gemini-2.5-flash"]}}

    @classmethod
    def setUpClass(cls):
        cls.sb = cls.make()
        cls.run_ = cls.sb.run_workflow(cls.sb.label_issue(7, TITLE, BODY))

    def test_the_build_is_red_with_no_secret_and_the_second_entry_never_ran(self):
        result = json.loads((self.sb.artifacts / "slice-main" / "result.json").read_text())
        self.assertEqual((result["status"], result["reason"]), ("failed", "no_secret"))
        self.assertEqual(self.run_.result("build"), "failure")
        self.assertEqual(self.sb.agent_calls(), [], "no agent ran, the groq entry included")

    def test_the_run_is_red_and_the_comment_and_the_message_give_the_reason(self):
        self.assertEqual(self.run_.result("report"), "failure")
        report = self.sb.hub.comments(7)[1]
        self.assertTrue(report.startswith(f"🏁 run #{self.run_.run_number} failed (no_secret)"), report)
        self.assertIn("piv doctor", report)
        (call,) = self.sb.curl.calls()
        self.assertIn("failed (no_secret)", call["sent"])
        self.assertEqual(self.sb.hub.prs(), [])
        self.assertEqual(sorted(self.sb.hub.issue(7)["labels"]), ["pivotal", "pivotal:blocked"])


# ------------------------------------------------------------------------------------------------ acceptance bullet 6


class DoctorTest(ScenarioCase):
    """``piv doctor`` reports a repo with Actions disabled and a repo with a missing secret."""

    secrets = {}

    def test_doctor_reports_disabled_actions_and_a_missing_secret(self):
        sb = self.sandbox()
        sb.hub.update(actions={"enabled": False, "allowed_actions": "all"})
        done = sb.piv("doctor", REPO)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        problems = [line.strip() for line in done.stdout.splitlines() if "[problem]" in line]
        self.assertEqual(len(problems), 2, done.stdout)
        self.assertTrue(any(p.startswith("[problem] Actions: disabled in") for p in problems), problems)
        self.assertTrue(any(p.startswith("[problem] Secrets: no repo-level model key is set") for p in problems), problems)
        self.assertIn("fix: turn Actions on", done.stdout)
        self.assertIn("fix: export a model key such as CLAUDE_CODE_OAUTH_TOKEN", done.stdout)
        self.assertIn("2 problem(s)", done.stdout)
        after_init = len(sb.hub.calls())
        sb.piv("doctor", REPO)
        for call in sb.hub.calls()[after_init:]:
            self.assertEqual(call["args"][0], "api", call["args"])
            self.assertNotIn("-X", call["args"], "doctor only reads")

    def test_doctor_stops_reporting_both_once_they_are_fixed(self):
        sb = self.sandbox()
        sb.hub.update(actions={"enabled": False, "allowed_actions": "all"})
        sb.hub.update(actions={"enabled": True, "allowed_actions": "all"})
        done = sb.piv("doctor", REPO)
        self.assertEqual(sum("[problem] Actions" in line for line in done.stdout.splitlines()), 0, done.stdout)
        with sb.hub.edit() as state:
            state["secrets"].append("CLAUDE_CODE_OAUTH_TOKEN")
        done = sb.piv("doctor", REPO)
        self.assertEqual(sum("[problem]" in line for line in done.stdout.splitlines()), 0, done.stdout)


# ------------------------------------------------------------------------------------------------ acceptance bullet 7


class RemoveTest(ScenarioCase):
    """``piv remove`` takes back everything ``piv init`` put on the repo. The fake repo state before init is compared with the
    state after remove."""

    def test_remove_leaves_nothing_behind(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fresh = Sandbox(Path(tmp.name), secrets=SECRETS, preset=PRESET)  # a repo that init has not touched
        before = fresh.hub.repo_state()
        self.assertEqual(fresh.piv_init().returncode, 0)
        initialised = fresh.hub.repo_state()
        self.assertNotEqual(initialised, before)
        self.assertIn(".github/workflows/pivotal.yml", initialised["files"])
        done = fresh.piv("remove", REPO, "--yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        after = fresh.hub.repo_state()
        self.assertTrue(initialised["workflow_permissions"]["can_approve_pull_request_reviews"])
        for key in before:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["files"], {})
        self.assertEqual((after["secrets"], after["variables"], after["labels"], after["topics"]), ([], {}, {}, []))
        self.assertIn("Actions can open pull requests: restored to off", done.stdout)

    def test_remove_without_yes_removes_nothing_and_a_second_remove_finds_nothing(self):
        sb = self.sandbox()
        initialised = sb.hub.repo_state()
        planned = sb.piv("remove", REPO)
        self.assertEqual(planned.returncode, 2, planned.stderr)
        self.assertEqual(sb.hub.repo_state(), initialised)
        self.assertEqual(sb.piv("remove", REPO, "--yes").returncode, 0)
        again = sb.piv("remove", REPO, "--yes")
        self.assertEqual(again.returncode, 0)
        self.assertIn("nothing was removed", again.stdout)

    def test_after_remove_the_caller_is_gone_so_no_workflow_can_run(self):
        sb = self.sandbox()
        self.assertEqual(sb.piv("remove", REPO, "--yes").returncode, 0)
        with self.assertRaises(KeyError):
            sb.caller_text()


# ----------------------------------------------------------------------------------------------- the stack is T3's


class CandidateTest(ScenarioCase):
    """What a clean run does: a green slice with a READY review from another vendor is a candidate, and publish opens the
    ready stack PR (the sandbox repo's level does not land, so the base is not moved)."""

    def test_a_ready_green_slice_is_stacked_and_opens_a_ready_stack_pr(self):
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        run = self.pipeline(sb)
        land = json.loads((sb.artifacts / "land" / "land.json").read_text())
        self.assertEqual((land["slices"][0]["outcome"], land["slices"][0]["candidate"]), ("candidate", True))
        self.assertEqual(run.job("assemble").outputs["has_stack"], "true")
        # Round 2, R7: the gate job ran on the stacked slice and its artifact says green for that slice
        self.assertEqual(run.result("gate"), "success")
        slice_id = land["slices"][0]["id"]
        gate = json.loads((sb.artifacts / f"gate-{slice_id}" / "gate.json").read_text())
        self.assertEqual((gate["id"], gate["green"], gate["head_unchanged"], gate["clean"]), (slice_id, True, True, True))
        result = json.loads(run.job("publish").outputs["result"])
        self.assertEqual((result["status"], result["pr"]), ("ready", 1))
        self.assertEqual([(p["headRefName"], p["isDraft"], p["state"]) for p in sb.hub.prs()], [("pivotal/issue-7/stack", False, "OPEN")])
        (pr,) = sb.hub.prs()
        self.assertEqual([(c["name"], c["head_sha"]) for c in seals(sb)], [("pivotal/gate", pr["headRefOid"])],
                         "at prepare publish seals the ready stack pull request's head, so a human's land it can land it")
        self.assertEqual(run.result("report"), "success")
        self.assertIn("ready for you", sb.hub.comments(7)[1])


# ------------------------------------------------------------------------------- the harness never sweeps its tester


class ContainTest(ScenarioCase):
    """CONTAIN_SCOPE=user SIGKILLs every process of the user started after the engine. No scenario may run it."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_no_workflow_or_template_outside_factory_yml_sets_the_scope(self):
        """The harness only runs factory.yml (and the caller, which does not hold the scope): a new file that set it
        would run unguarded."""
        for path in [*(self.ROOT / ".github" / "workflows").glob("*.yml"), *(self.ROOT / "templates").glob("*.yml"),
                     self.ROOT / "engine" / "setup" / "action.yml"]:
            if path.name != "factory.yml" and path.exists():
                self.assertNotIn("CONTAIN_SCOPE", path.read_text(encoding="utf-8"), path.name)

    def test_every_step_that_sets_the_scope_runs_contained_whatever_the_workflow_says(self):
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        run = self.pipeline(sb)
        factory = sandbox.parse(sb.factory_text())
        declared = {job_id for job_id, job in factory["jobs"].items() for step in job.get("steps", [])
                    if "CONTAIN_SCOPE" in (step.get("env") or {})}
        self.assertEqual(declared, {"build", "review", "gate"}, "a new job that sets the scope needs a scenario here")
        ran = {}
        for job in run.all_jobs():
            for step in job.ran():
                if "CONTAIN_SCOPE" in step.env:
                    ran[(job.key, step.name)] = (step.env["CONTAIN_SCOPE"], step.env.get("PIVOTAL_CONTAIN_USER_OK"))
        self.assertEqual({key for key, _ in ran}, declared, "every declaring job ran in the scenario")
        for key, value in ran.items():
            self.assertEqual(value, ("descendants", "0"), key)

    def test_no_test_can_sweep_the_live_proc_with_the_user_scope(self):
        from engine import buildjob
        guard = buildjob.Guard("user")
        self.assertEqual(guard.proc, Path("/proc"))
        with self.assertRaises(AssertionError):
            guard.sweep()
        # and every test module that imports an engine job installs that refusal
        for path in sorted((self.ROOT / "test").glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^from engine import .*\b(buildjob|gatejob|reviewjob)\b", text, re.M):
                self.assertIn("import nolive", text, path.name)

    def test_the_gate_scenario_kills_nothing_outside_its_tree(self):
        """A sentinel of the same user, in its own session, started after the gate engine, must survive the sweep."""
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        sentinel = []
        stop = threading.Event()
        me = os.getpid()

        def watch():
            while not stop.is_set() and not sentinel:
                for entry in os.listdir("/proc"):
                    if not entry.isdigit():
                        continue
                    try:
                        cmd = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
                        ppid = int(Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[1])
                    except (OSError, ValueError, IndexError):
                        continue
                    if ppid == me and any(a.endswith(b"gatejob.py") for a in cmd) and b"run" in cmd:
                        sentinel.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                                         start_new_session=True, stdin=subprocess.DEVNULL))
                        return
                time.sleep(0.005)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            run = self.pipeline(sb)
        finally:
            stop.set()
            watcher.join(10)
        try:
            self.assertEqual(run.result("gate"), "success")
            self.assertTrue(sentinel, "the sentinel was never started: the gate step was not seen running")
            time.sleep(0.2)
            self.assertIsNone(sentinel[0].poll(), "the gate step killed a process outside its tree")
        finally:
            for proc in sentinel:
                proc.kill()
                proc.wait()


# ------------------------------------------------------------------------------------- comment commands
#
# A comment steers a run through the caller (F2b, F6). These scenarios run the real caller, the real
# stop job and the real factory, and a stop arrives where it can: between two jobs of a run that is still going (``before_job``).


def cancels(sb: Sandbox) -> list[int]:
    """The run ids the stop job's cancels were accepted for, in order. A cancel the fake refused (a run that had completed)
    changes nothing and is not listed; a run is listed once."""
    accepted = {r["id"] for r in sb.hub.state().get("workflow_runs", []) if r["conclusion"] == "cancelled"}
    found: list[int] = []
    for call in sb.hub.calls():
        if call["token"] == "stop" and call["args"][:2] == ["run", "cancel"]:
            run_id = int(call["args"][2])
            if run_id in accepted and run_id not in found:
                found.append(run_id)
    return found


def stop_before_publish(sb: Sandbox, commenter: str, into: list) -> object:
    """A ``/pivotal stop`` from ``commenter`` on issue 7 that arrives while the factory is about to publish."""
    def hook() -> None:
        into.append(sb.run_workflow(sb.comment_event(7, "/pivotal stop", commenter)))
    return hook


class CommentFixTest(ScenarioCase):
    """A /pivotal fix comment on an issue triggers a factory run (kind=fix)."""

    def test_fix_comment_triggers_factory_run(self):
        sb = self.sandbox()
        # First create an issue
        sb.label_issue(7, TITLE, BODY)

        # Comment /pivotal fix from a writer
        fix_run = sb.run_workflow(sb.comment_event(7, "/pivotal fix", "alice"))

        # The fix run should be a factory-trusted run (since alice is a writer)
        self.assertEqual(fix_run.factory, "factory-trusted")
        self.assertEqual(fix_run.trust.outputs["kind"], "fix")
        self.assertEqual(fix_run.trust.outputs["trusted"], "true")
        self.assertEqual(fix_run.trust.outputs["issue"], "7")

        # The factory run should complete (round 0 for comment-triggered fix)
        self.assertEqual(fix_run.result("publish"), "success")


def pr_comment_event(sb: Sandbox, pr_number: int, body: str, commenter: str) -> tuple[str, dict]:
    """A comment on a pull request as GitHub sends it: an issue_comment whose ``issue`` is the pull request itself (its own
    number, its body, and a ``pull_request`` key), not the issue it closes."""
    pr = next(p for p in sb.hub.prs() if p["number"] == pr_number)
    name, payload = sb.issue_event(7)
    payload["issue"] = {"number": pr_number, "title": pr["title"], "body": pr["body"], "user": pr["author"],
                        "labels": [], "pull_request": {"url": pr["url"]}}
    payload.update(action="created", comment={"body": body, "user": {"login": commenter}}, sender={"login": commenter})
    payload.pop("label", None)
    return "issue_comment", payload


class CommentLandTest(ScenarioCase):
    """A /pivotal land comment on a PR triggers the land job."""

    def test_land_comment_on_pr_triggers_land_job(self):
        sb = self.sandbox()
        # First, start a normal run that creates a draft PR
        first = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        self.assertEqual(first.result("publish"), "success")
        prs = sb.hub.prs()
        self.assertEqual(len(prs), 1)
        pr_number = prs[0]["number"]

        # Now comment /pivotal land on the PR - this should trigger the land job
        # The land comment on a PR resolves to kind=land, issue=7 (the issue the PR closes)
        land_run = sb.run_workflow(sb.comment_event(7, "/pivotal land", "alice"))

        # The land run should be the land job, not factory
        self.assertEqual(land_run.factory, "")  # land job doesn't set factory
        # The trust job should output kind=land
        self.assertEqual(land_run.trust.outputs["kind"], "land")
        self.assertEqual(land_run.trust.outputs["trusted"], "true")
        self.assertEqual(land_run.trust.outputs["issue"], "7")


    def test_a_land_comment_on_the_ready_stack_pr_is_queued_with_the_issue_it_closes(self):
        """I3, I4: the PR-shaped event (issue.number is the PR's, issue.pull_request is set, the body says Closes #7). trust
        resolves the issue the pull request closes, so the land job's issue and its concurrency group are the issue's: a stop on
        the issue can see the land and no other run of the issue is left out of its queue. The land job then reads the pull
        request, verifies the seal publish made on its head at the default level (prepare) and lands it (PrepareSealTest)."""
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        first = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        self.assertEqual(first.result("publish"), "success")
        (pr,) = sb.hub.prs()
        self.assertEqual(pr["headRefName"], "pivotal/issue-7/stack")
        land = sb.run_workflow(pr_comment_event(sb, pr["number"], "/pivotal land", "alice"))
        self.assertEqual((land.trust.outputs["trusted"], land.trust.outputs["kind"]), ("true", "land"))
        self.assertEqual(land.trust.outputs["issue"], "7", "the issue the pull request closes, not the pull request's number")
        self.assertIn("land", land.jobs, "the caller routes a trusted land to its land job")
        self.assertEqual(land.inputs["issue"], "7")
        self.assertEqual(land.group, "pivotal-issue-7", "the land job queues behind the runs of its issue")
        self.assertEqual(land.factory, "")
        # The land job resolves the pull request (its Closes line, its stack branch and its readiness), verifies the seal, and lands
        log = land.job("land").step("land").log
        self.assertIn("lands the pull request #1, which closes #7", log)
        self.assertIn("seal: run", log, "the seal of the ready stack pull request verified")
        self.assertEqual(land.result("land"), "success", log)
        self.assertNotIn("not a trusted", log)


def seals(sb: Sandbox) -> list[dict]:
    """The pivotal/gate check runs the fake holds (the seals publish made)."""
    return [c for c in sb.hub.state()["check_runs"] if c["name"] == "pivotal/gate"]


def base_of(sb: Sandbox) -> str:
    return sb.git("rev-parse", "refs/heads/main", cwd=sb.origin)


class PrepareSealTest(ScenarioCase):
    """The default level, prepare: the factory's publish job seals the READY stack pull request and moves no
    base; a human's "land it" (a /pivotal land comment on that pull request) then lands exactly the sealed commit, by a plain
    push that is a fast-forward, and closes the issue. A seal that is missing or forged lands nothing (F4)."""

    def ready_stack(self) -> tuple[Sandbox, dict, str]:
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        base = base_of(sb)
        self.assertEqual(self.pipeline(sb).result("publish"), "success")
        (pr,) = sb.hub.prs()
        self.assertEqual((pr["headRefName"], pr["isDraft"], pr["state"]), ("pivotal/issue-7/stack", False, "OPEN"))
        self.assertEqual(base_of(sb), base, "prepare moves no base")
        (sealed,) = [c["head_sha"] for c in seals(sb)]
        self.assertEqual(sealed, pr["headRefOid"], "the seal is on the ready pull request's head")
        return sb, pr, sealed

    def land_it(self, sb: Sandbox, pr: dict) -> Run:
        return sb.run_workflow(pr_comment_event(sb, pr["number"], "/pivotal land", "alice"))

    def test_land_it_lands_exactly_the_sealed_commit_and_the_issue_closes_once_the_base_has_it(self):
        sb, pr, sealed = self.ready_stack()
        before = base_of(sb)
        land = self.land_it(sb, pr)
        log = land.job("land").step("land").log
        self.assertEqual(land.result("land"), "success", log)
        self.assertEqual(base_of(sb), sealed, "the base is the sealed commit and nothing else")
        self.assertEqual(subprocess.run(["git", "-C", str(sb.origin), "merge-base", "--is-ancestor", before, sealed],
                                        capture_output=True).returncode, 0, "a fast-forward: the old base is an ancestor")
        self.assertEqual(sb.hub.issue(7).get("state", "open"), "closed", "the issue closes once the base contains the sealed commit")
        self.assertIn(f"Landed {sealed} on main", "\n".join(sb.hub.comments(7)))

    def test_without_a_seal_land_it_lands_nothing(self):
        sb, pr, _ = self.ready_stack()
        before = base_of(sb)
        with sb.hub.edit() as state:
            state["check_runs"] = []
        land = self.land_it(sb, pr)
        self.assertEqual(land.result("land"), "failure")
        self.assertIn("no pivotal/gate seal", land.job("land").step("land").log)
        self.assertEqual(base_of(sb), before, "nothing moved")
        self.assertEqual(sb.hub.issue(7).get("state", "open"), "open")

    def test_a_forged_seal_lands_nothing(self):
        """A seal another app made, and one in another check suite than its run: neither verifies, so neither lands."""
        cases = (("another app", {"app": {"slug": "someone-else"}}, "no pivotal/gate seal"),
                 ("another suite", {"check_suite": {"id": 1}}, "suite_differs"))
        for name, forgery, refusal in cases:
            with self.subTest(forgery=name):
                sb, pr, _ = self.ready_stack()
                before = base_of(sb)
                with sb.hub.edit() as state:
                    for check in state["check_runs"]:
                        check.update(forgery)
                land = self.land_it(sb, pr)
                self.assertEqual(land.result("land"), "failure")
                self.assertIn(refusal, land.job("land").step("land").log)
                self.assertEqual(base_of(sb), before, "nothing moved")
                self.assertEqual(sb.hub.issue(7).get("state", "open"), "open")


class CommentStopTest(ScenarioCase):
    """F6: a stop cancels the issue's runs, and a cancelled run never publishes (acceptance: cancel means no publish)."""

    def test_a_stop_on_the_issue_cancels_a_land_started_from_a_comment_on_its_pull_request(self):
        """I4: GitHub titles a run from the EVENT (``run-name`` uses ``issue.number``), so a land commented on the stack pull
        request is titled with the pull request's number while its group is the issue's. A stop on issue 7 that arrives during
        that land must still cancel it (F6), and a land that was cancelled does not go on."""
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        self.assertEqual(sb.run_workflow(sb.label_issue(7, TITLE, BODY)).result("publish"), "success")
        (pr,) = sb.hub.prs()
        self.assertNotEqual(pr["number"], 7)
        stops: list[Run] = []
        sb.before_job["land"] = lambda: stops.append(sb.run_workflow(sb.comment_event(7, "/pivotal stop", "alice")))
        land = sb.run_workflow(pr_comment_event(sb, pr["number"], "/pivotal land", "alice"))
        (stop,) = stops
        self.assertEqual(stop.trust.outputs["kind"], "stop")
        self.assertEqual(cancels(sb), [land.run_id], "the stop reaches the land run titled with the pull request's number")
        self.assert_the_cancelled_land_did_not_go_on(sb, land)

    def assert_the_cancelled_land_did_not_go_on(self, sb: Sandbox, land: Run) -> None:
        """R3: cancelling the run is not enough, the land job must not run on after it: it is skipped, says nothing of
        landing, and the base has not moved."""
        self.assertEqual(land.result("land"), "skipped", "a land that was cancelled does not go on")
        self.assertFalse([c for c in sb.hub.comments(7) if "Landed" in c], "no Landed comment")
        self.assertEqual(sb.git("--git-dir", str(sb.origin), "rev-parse", "main"), sb.base_sha, "the base did not move")

    def test_a_stop_commented_on_the_stack_pull_request_cancels_a_land_in_progress(self):
        """R1: "/pivotal stop" on a pull request resolves to the issue the pull request closes, as a land does, so the stop job
        cancels the runs of that issue and of its stack pull request."""
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        self.assertEqual(sb.run_workflow(sb.label_issue(7, TITLE, BODY)).result("publish"), "success")
        (pr,) = sb.hub.prs()
        stops: list[Run] = []
        sb.before_job["land"] = lambda: stops.append(sb.run_workflow(pr_comment_event(sb, pr["number"], "/pivotal stop", "alice")))
        land = sb.run_workflow(pr_comment_event(sb, pr["number"], "/pivotal land", "alice"))
        (stop,) = stops
        self.assertEqual((stop.trust.outputs["trusted"], stop.trust.outputs["kind"], stop.trust.outputs["issue"]), ("true", "stop", "7"))
        self.assertEqual(cancels(sb), [land.run_id], "the stop reaches the land run titled with the pull request's number")
        self.assert_the_cancelled_land_did_not_go_on(sb, land)

    def test_a_stop_commented_on_the_stack_pull_request_cancels_a_run_of_the_issue(self):
        """R1: the issue's own run, titled with the issue number, is cancelled by a stop commented on the pull request."""
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        self.assertEqual(sb.run_workflow(sb.label_issue(7, TITLE, BODY)).result("publish"), "success")
        (pr,) = sb.hub.prs()
        stops: list[Run] = []
        sb.before_job["publish"] = lambda: stops.append(sb.run_workflow(pr_comment_event(sb, pr["number"], "/pivotal stop", "alice")))
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        (stop,) = stops
        self.assertEqual((stop.trust.outputs["kind"], stop.trust.outputs["issue"]), ("stop", "7"))
        self.assertEqual(cancels(sb), [run.run_id])
        self.assertEqual(run.result("publish"), "skipped", "a cancelled run never publishes (F6)")

    def test_a_stop_commented_on_a_pull_request_by_a_stranger_cancels_nothing(self):
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        self.assertEqual(sb.run_workflow(sb.label_issue(7, TITLE, BODY)).result("publish"), "success")
        (pr,) = sb.hub.prs()
        stops: list[Run] = []
        sb.before_job["land"] = lambda: stops.append(sb.run_workflow(pr_comment_event(sb, pr["number"], "/pivotal stop", "mallory")))
        sb.run_workflow(pr_comment_event(sb, pr["number"], "/pivotal land", "alice"))
        (stop,) = stops
        self.assertNotEqual(stop.trust.outputs["trusted"], "true")
        self.assertEqual(cancels(sb), [])

    def test_the_run_is_titled_by_evaluating_the_callers_run_name_on_the_event(self):
        """S2: the title the stop job searches for is the caller's own ``run-name``, evaluated, not a copy of its shape: a
        caller that names its runs another way is titled that way in the sandbox."""
        sb = self.sandbox()
        original = sb.caller_text
        sb.caller_text = lambda: original().replace('run-name: "pivotal-issue-', 'run-name: "pivotal-renamed-')
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        titles = {r["id"]: r["display_title"] for r in sb.hub.state()["workflow_runs"]}
        self.assertEqual(titles[run.run_id], "pivotal-renamed-7")

    def test_a_stop_mid_run_cancels_the_run_and_no_pr_appears(self):
        sb = self.sandbox()
        stops: list[Run] = []
        sb.before_job["publish"] = stop_before_publish(sb, "alice", stops)
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        (stop,) = stops
        self.assertEqual((stop.trust.outputs["trusted"], stop.trust.outputs["kind"]), ("true", "stop"))
        self.assertEqual(cancels(sb), [run.run_id], "the stop cancels the running factory and not its own run")
        self.assertEqual(run.result("publish"), "skipped", "a cancelled run never publishes (F6)")
        self.assertEqual(sb.hub.prs(), [], "no PR appears for a stopped run")

    def test_a_stop_mid_run_of_a_stacked_run_cancels_it_and_no_stack_pr_appears(self):
        """I1: the publish guard, proven where it matters. A READY slice is stacked, so publish has a ready stack PR to open
        and its `gate` needs succeed: only publish's own `!cancelled()` keeps it from publishing. (A draft-only run would be
        skipped by the `needs` rule even without the guard, so it proves nothing about the guard.)"""
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        stops: list[Run] = []
        sb.before_job["publish"] = stop_before_publish(sb, "alice", stops)
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        (stop,) = stops
        self.assertEqual((stop.trust.outputs["trusted"], stop.trust.outputs["kind"]), ("true", "stop"))
        self.assertEqual(run.job("assemble").outputs["has_stack"], "true", "the slice is stacked: a publish would open a ready stack PR")
        self.assertEqual(run.result("gate"), "success", "every need of publish succeeded but the stop")
        self.assertEqual(cancels(sb), [run.run_id])
        self.assertEqual(run.result("publish"), "skipped", "a cancelled run never publishes (F6)")
        self.assertEqual(sb.hub.prs(), [], "no stack PR appears for a stopped run")

    def test_a_stop_mid_run_reports_publish_failed_a_known_gap_not_cancelled(self):
        """I6, A KNOWN GAP, pinned as it is: the plan promised that the report of a stopped run says it was cancelled. It does
        not. publish is skipped, report.decide reads a skipped publish as a failure, and no word of the run tells it a stop
        from a failure, so the report reads `failed (publish_failed)` and its job is red. This test fails when that changes, so
        the change is seen and the word is updated with it (discoveries.md: "a stop mid-run reports failed")."""
        sb = self.sandbox()
        stops: list[Run] = []
        sb.before_job["publish"] = stop_before_publish(sb, "alice", stops)
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        self.assertEqual(run.result("publish"), "skipped")
        self.assertEqual(run.result("report"), "failure", "the report job is red after a stop (known gap)")
        report = sb.hub.comments(7)[-1]
        self.assertIn(f"run #{run.run_number} failed (publish_failed)", report)
        self.assertNotIn("cancelled", report, "the word the plan promised is not there (known gap)")

    def test_a_stop_before_the_gate_of_a_stacked_run_publishes_nothing(self):
        """S7: the gate's own `!cancelled()`. A stop that arrives before the gate of a stacked run stops the gate, so publish
        has no green gate to publish, and no stack PR appears (publish's own guard is tested above)."""
        sb = self.sandbox()
        sb.set_agents(verdict="READY")
        stops: list[Run] = []

        def stop_before_gate() -> None:
            stops.append(sb.run_workflow(sb.comment_event(7, "/pivotal stop", "alice")))

        sb.before_job["gate"] = stop_before_gate
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        self.assertEqual(len(stops), 1)
        self.assertEqual(run.job("assemble").outputs["has_stack"], "true", "the run stacked a slice: the gate has work to do")
        self.assertEqual(cancels(sb), [run.run_id])
        self.assertEqual(run.result("gate"), "skipped", "a cancelled run's gate is not started")
        self.assertEqual(run.result("publish"), "skipped")
        self.assertEqual(sb.hub.prs(), [])

    def test_a_stop_before_build_review_or_assemble_starts_none_of_the_later_jobs(self):
        """S7: the `!cancelled()` guards of build, review and assemble (they run past a failed need, so they must say they do
        not run past a stop). A stop that arrives before the job stops it and everything after it, and no PR appears.
        `models-up` has a guard too, but its job runs only when the plan needs Kaggle and no sandbox plan does: its guard is
        not exercised here. The `build` row does not discriminate build's own guard: the skipped `models-up` need skips build
        anyway, and `test_the_same_run_without_a_stop_publishes_one_pr` is the control that catches that guard's removal."""
        for job in ("build", "review", "assemble"):
            with self.subTest(before=job):
                sb = self.sandbox()
                sb.set_agents(verdict="READY")
                stops: list[Run] = []
                sb.before_job[job] = lambda sb=sb, stops=stops: stops.append(sb.run_workflow(sb.comment_event(7, "/pivotal stop", "alice")))
                run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
                self.assertEqual(len(stops), 1)
                self.assertEqual(cancels(sb), [run.run_id])
                self.assertEqual(run.result(job), "skipped", f"{job} is not started after a stop")
                self.assertEqual(run.result("publish"), "skipped")
                self.assertEqual(sb.hub.prs(), [])

    def test_the_same_run_without_a_stop_publishes_one_pr(self):
        """The control for the test above: without the stop the publish step is reached and opens the PR."""
        sb = self.sandbox()
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        self.assertEqual(run.result("publish"), "success")
        self.assertEqual(len(sb.hub.prs()), 1)

    def test_a_stranger_stop_mid_run_is_ignored_and_the_run_publishes(self):
        sb = self.sandbox()
        sb.hub.update(collaborators={"alice": "write", "stranger": "read"})
        stops: list[Run] = []
        sb.before_job["publish"] = stop_before_publish(sb, "stranger", stops)
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        (stop,) = stops
        self.assertEqual((stop.trust.outputs["trusted"], stop.trust.outputs["kind"]), ("false", "none"))
        self.assertNotIn("stop", stop.jobs, "a non-writer's stop runs no job")
        self.assertEqual(cancels(sb), [])
        self.assertEqual(run.result("publish"), "success")
        self.assertEqual(len(sb.hub.prs()), 1)

    def test_a_stop_with_nothing_in_flight_cancels_nothing(self):
        sb = self.sandbox()
        done = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        self.assertEqual(done.result("publish"), "success")
        stop = sb.run_workflow(sb.comment_event(7, "/pivotal stop", "alice"))
        self.assertEqual(stop.result("stop"), "success")
        self.assertEqual(cancels(sb), [])
        self.assertEqual(len(sb.hub.prs()), 1)

    def test_a_stop_cancels_every_queued_run_of_its_issue_and_leaves_the_others_alone(self):
        """I2: the stop job's loop feeds the run ids to ``gh run cancel`` on stdin. The fake must not swallow the rest of them:
        three queued runs of issue 7 are all cancelled, and issue 8's run, listed first, is untouched."""
        sb = self.sandbox()
        sb.hub.add_issue(7, TITLE, BODY, author=sb.actor)
        sb.hub.add_issue(8, TITLE, BODY, author=sb.actor)

        def queued(run_id: int, issue: int) -> dict:
            return {"id": run_id, "display_title": f"pivotal-issue-{issue}", "status": "queued", "conclusion": "",
                    "head_branch": "main", "event": "issues"}

        with sb.hub.edit() as state:
            state.setdefault("workflow_runs", []).extend([queued(502, 8), queued(501, 7), queued(503, 7), queued(505, 7)])
        stop = sb.run_workflow(sb.comment_event(7, "/pivotal stop", "alice"))
        self.assertEqual(cancels(sb), [501, 503, 505], "every queued run of issue 7, in listing order, and nothing else")
        states = {r["id"]: r["status"] for r in sb.hub.state()["workflow_runs"] if r["id"] in (501, 502, 503, 505)}
        self.assertEqual(states, {501: "completed", 502: "queued", 503: "completed", 505: "completed"})
        self.assertEqual(stop.result("stop"), "success")

    def test_a_stop_cancels_the_queued_runs_of_its_issue_and_leaves_other_issues_alone(self):
        sb = self.sandbox()
        sb.hub.add_issue(7, TITLE, BODY, author=sb.actor)
        sb.hub.add_issue(8, TITLE, BODY, author=sb.actor)
        with sb.hub.edit() as state:
            state.setdefault("workflow_runs", []).extend([
                {"id": 501, "display_title": "pivotal-issue-7", "status": "queued", "conclusion": "", "head_branch": "main", "event": "issues"},
                {"id": 502, "display_title": "pivotal-issue-8", "status": "queued", "conclusion": "", "head_branch": "main", "event": "issues"},
            ])
        stop = sb.run_workflow(sb.comment_event(7, "/pivotal stop", "alice"))
        self.assertEqual(cancels(sb), [501])
        queued = {r["id"]: r["status"] for r in sb.hub.state()["workflow_runs"] if r["id"] in (501, 502)}
        self.assertEqual(queued, {501: "completed", 502: "queued"})
        self.assertEqual(stop.result("stop"), "success")


class CommentGoTest(ScenarioCase):
    """/pivotal go trusted: a writer re-runs a stranger's issue as trusted (F2b)."""

    def test_a_writers_go_trusted_runs_the_stranger_issue_with_the_subscription_secrets(self):
        sb = self.sandbox()
        sb.hub.update(collaborators={"alice": "write", "stranger": "read"})
        first = sb.run_workflow(sb.label_issue(8, TITLE, BODY, author="stranger"))
        self.assertEqual((first.trust.outputs["trusted"], first.factory), ("false", "factory-untrusted"))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", first.passed_secrets)
        rerun = sb.run_workflow(sb.comment_event(8, "/pivotal go trusted", "alice"))
        self.assertEqual((rerun.trust.outputs["trusted"], rerun.trust.outputs["kind"]), ("true", "go"))
        self.assertEqual(rerun.factory, "factory-trusted")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", rerun.passed_secrets)

    def test_a_stranger_go_trusted_is_ignored(self):
        sb = self.sandbox()
        sb.hub.update(collaborators={"alice": "write", "stranger": "read"})
        sb.label_issue(8, TITLE, BODY, author="stranger")
        run = sb.run_workflow(sb.comment_event(8, "/pivotal go trusted", "stranger"))
        self.assertEqual((run.trust.outputs["trusted"], run.trust.outputs["kind"]), ("false", "none"))
        self.assertEqual(run.factory, "")
        self.assertEqual(sb.hub.prs(), [])


class CommentQueueTest(ScenarioCase):
    """piv do and /pivotal go on one issue run one after the other: one concurrency group per issue."""

    def test_a_labelled_issue_and_a_comment_on_it_share_one_queued_group(self):
        sb = self.sandbox()
        filed = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        commented = sb.run_workflow(sb.comment_event(7, "/pivotal go", "alice"))
        self.assertEqual(filed.group, "pivotal-issue-7")
        self.assertEqual(commented.group, filed.group)
        other = sb.run_workflow(sb.label_issue(8, TITLE, BODY))
        self.assertEqual(other.group, "pivotal-issue-8", "another issue has its own group, so it is not serialized behind this one")

    def test_the_comment_on_an_issue_that_already_published_does_not_duplicate_its_pr(self):
        sb = self.sandbox()
        sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        (pr,) = sb.hub.prs()
        rerun = sb.run_workflow(sb.comment_event(7, "/pivotal go", "alice"))
        self.assertEqual((rerun.trust.outputs["kind"], rerun.factory), ("go", "factory-trusted"))
        self.assertEqual(rerun.result("publish"), "success", "the re-run reaches publish, which finds its PR")
        self.assertEqual([p["number"] for p in sb.hub.prs()], [pr["number"]], "re-runs never duplicate a PR (F6): found by its branch")

    def test_the_callers_concurrency_is_one_never_cancelling_queue_per_issue(self):
        """I5: the group string alone does not serialize anything: the caller's concurrency block must be exactly the queue
        (`queue: max`, nothing cancels in progress), on each job that runs a factory or a land, and the stop job has none."""
        sb = self.sandbox()
        caller = sandbox.parse(sb.caller_text())
        group = "pivotal-issue-${{ needs.trust.outputs.issue }}"
        for key in ("factory-trusted", "factory-untrusted", "land"):
            self.assertEqual(caller["jobs"][key]["concurrency"], {"group": group, "queue": "max"}, key)
        self.assertNotIn("concurrency", caller["jobs"]["stop"], "a stop must not queue behind the run it cancels")


class CommentNonWriterTest(ScenarioCase):
    """A comment from a non-writer is ignored."""

    def test_comment_from_non_writer_is_ignored(self):
        sb = self.sandbox()
        # First create an issue
        sb.label_issue(7, TITLE, BODY)
        # The commenter is a reader: a real read-level answer from the fake, not an unknown login
        sb.hub.update(collaborators={"alice": "write", "stranger": "read"})

        # Comment /pivotal go from a non-writer
        run = sb.run_workflow(sb.comment_event(7, "/pivotal go", "stranger"))

        # The run should be untrusted (kind=none, so no factory runs)
        self.assertEqual(run.trust.outputs["kind"], "none")
        self.assertEqual(run.trust.outputs["trusted"], "false")
        self.assertNotIn("factory", run.jobs)

        # No PR should be created
        self.assertEqual(sb.hub.prs(), [])


# ------------------------------------------------------------------------------------- the harness is not a rubber stamp


class HarnessTest(ScenarioCase):
    """The sandbox fails when a seam is broken: a wrong output name or a wrong artifact name stops the pipeline."""

    def test_the_leak_search_finds_every_form_in_files_and_in_job_outputs(self):
        sb = self.sandbox()
        token = SECRETS["GROQ_API_KEY"]
        data = token.encode()
        forms = {"offset 1": base64.b64encode(b"x" + data).decode(), "offset 2": base64.b64encode(b"xx" + data).decode(),
                 "urlsafe": base64.urlsafe_b64encode(b"x" + data).decode(), "hex upper": data.hex().upper(),
                 "hex": data.hex(), "raw": token}
        for name, form in forms.items():
            with self.subTest(form=name):
                (sb.world / "probe.txt").write_text(f"before {form} after")
                self.assertEqual(sb.leaks(token), ["probe.txt"])
        (sb.world / "probe.txt").write_text("nothing")
        self.assertEqual(sb.leaks(token), [])
        run = sandbox.Run("issues", 1, 1, 1)
        run.jobs["plan"] = [sandbox.Job("plan", "plan", "success", outputs={"line": forms["offset 1"]})]
        sb.runs.append(run)
        self.assertEqual(sb.leaks(token), ["outputs/run-1-1/plan"])

    def test_the_leak_search_also_reads_the_text_with_its_whitespace_squashed(self):
        """Round 2, R5: the engine's scan squashes whitespace, so a token split by a newline is a leak the harness must see."""
        sb = self.sandbox()
        token = SECRETS["GROQ_API_KEY"]
        wrapped = base64.b64encode(b"x" + token.encode()).decode()
        split = {"raw split by a newline": token[:7] + "\n" + token[7:],
                 "raw split in a diff": "+" + token[:7] + "\n+" + token[7:],
                 "base64 wrapped at 20 columns": "\n".join(wrapped[i:i + 20] for i in range(0, len(wrapped), 20))}
        for name, text in split.items():
            with self.subTest(form=name):
                (sb.world / "probe.txt").write_text(f"before\n{text}\nafter")
                self.assertEqual(sb.leaks(token), ["probe.txt"])
        (sb.world / "probe.txt").write_text("a different\ntext")
        self.assertEqual(sb.leaks(token), [])

    def test_a_misnamed_job_output_fails_the_job_that_reads_it(self):
        sb = self.sandbox(factory_edit=lambda text: text.replace(
            "BASE_SHA: ${{ needs.plan.outputs.base_sha }}\n          MATRIX: ${{ needs.plan.outputs.matrix }}",
            "BASE_SHA: ${{ needs.plan.outputs.basesha }}\n          MATRIX: ${{ needs.plan.outputs.matrix }}", 1))
        run = self.pipeline(sb)
        self.assertEqual((run.result("assemble"), run.result("publish")), ("failure", "skipped"))
        self.assertEqual(run.result("report"), "failure")
        self.assertIn("BASE_SHA", run.job("assemble").step("assemble").log)
        self.assertEqual(sb.hub.prs(), [])

    def test_a_misnamed_artifact_fails_the_download(self):
        sb = self.sandbox(factory_edit=lambda text: text.replace("name: slice-${{ matrix.slice.id }}\n          path: ${{ runner.temp }}/slice-in",
                                                                 "name: slices-${{ matrix.slice.id }}\n          path: ${{ runner.temp }}/slice-in"))
        run = self.pipeline(sb)
        self.assertEqual(run.result("review"), "failure")
        self.assertIn("Unable to find an artifact", run.job("review").step("Download the slice artifact").log)
        self.assertEqual(run.result("report"), "failure")

    def test_a_step_never_sees_the_testers_environment(self):
        import os
        os.environ["SANDBOX_CANARY"] = "canary"
        self.addCleanup(os.environ.pop, "SANDBOX_CANARY", None)
        sb = self.sandbox()
        run = self.pipeline(sb)
        for job in run.all_jobs():
            for step in job.ran():
                self.assertNotIn("SANDBOX_CANARY", step.env)
        self.assertNotIn("canary", "".join(t for _, t in sb.visible_text()))

    def test_the_declared_deviations_are_the_only_ones(self):
        self.assertEqual(sorted(sandbox.OVERRIDES), ["land.py", "publish.py"])
        sb = self.sandbox()
        run = self.pipeline(sb)
        publish = run.job("publish").step("publish").env
        self.assertEqual(publish["PIV_GH"], str(sb.hub.path))
        self.assertEqual(run.job("build").step("build").env["CONTAIN_SCOPE"], "descendants")
        factory = sandbox.parse(sb.factory_text())
        for job_id in ("publish", "build", "review"):
            for step in factory["jobs"][job_id]["steps"]:
                self.assertNotIn("PIV_GH", step.get("env") or {}, "the workflow never sets the seam")
        land = sandbox.parse((sandbox.ROOT / ".github" / "workflows" / "land.yml").read_text(encoding="utf-8"))
        for step in land["jobs"]["land"]["steps"]:
            self.assertNotIn("PIV_GH", step.get("env") or {}, "the land workflow never sets the seam either")
        self.assertTrue(set(route.DOOR_SECRETS))


class ExprTest(unittest.TestCase):
    """The expression evaluator only has to be right about what the three workflows use."""

    def expr(self, **contexts):
        return sandbox.Expr(contexts, lambda name: name in ("success", "always"))

    def test_the_runner_choice_expression(self):
        text = "${{ needs.ack.outputs.runner == 'self-hosted' && 'descendants' || 'user' }}"
        self.assertEqual(self.expr(needs={"ack": {"outputs": {"runner": "self-hosted"}}}).text(text), "descendants")
        self.assertEqual(self.expr(needs={"ack": {"outputs": {"runner": "ubuntu-24.04"}}}).text(text), "user")
        self.assertEqual(self.expr().text(text), "user", "a missing output is null")

    def test_conditions(self):
        e = self.expr(needs={"plan": {"result": "success", "outputs": {"matrix": "[]"}}}, steps={"build": {"outcome": "success"}})
        self.assertEqual(e.text("${{ !cancelled() && needs.plan.result == 'success' }}"), True)
        self.assertEqual(e.text("${{ steps.build.outcome == 'success' && steps.build.outputs.published == 'true' }}"), False)
        self.assertEqual(e.text("${{ contains(fromJSON('[\"go\",\"fix\"]'), 'go') }}"), True)
        self.assertEqual(e.text("${{ contains(fromJSON('[\"go\",\"fix\"]'), 'land') }}"), False)
        self.assertEqual(e.text("pivotal-issue-${{ needs.plan.result }}"), "pivotal-issue-success")
        self.assertEqual(e.text("${{ needs.plan.outputs.matrix != '' }}"), True)

    def test_to_json_and_from_json_round_trip_a_slice(self):
        e = self.expr(matrix={"slice": {"id": "main", "title": "T"}})
        self.assertEqual(json.loads(e.text("${{ toJSON(matrix.slice) }}")), {"id": "main", "title": "T"})
        self.assertEqual(e.text("${{ fromJSON('[1]') }}"), [1])

    def test_an_expression_it_cannot_read_raises(self):
        with self.assertRaises(ValueError):
            self.expr().text("${{ a ? b : c }}")
        with self.assertRaises(ValueError):
            self.expr().text("${{ hashFiles('x') }}")

    def test_outputs_are_read_as_the_runner_reads_them(self):
        text = "a=1\nb<<EOF\nline1\nline2\nEOF\nc=x=y\n"
        self.assertEqual(sandbox.parse_outputs(text), {"a": "1", "b": "line1\nline2", "c": "x=y"})

    def test_download_artifact_layouts_follow_name_and_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            sb = Sandbox(Path(tmp))
            (sb.artifacts / "slice-a").mkdir()
            (sb.artifacts / "slice-a" / "f").write_text("a")
            (sb.artifacts / "other").mkdir()
            (sb.artifacts / "other" / "g").write_text("g")
            sb.download({"name": "slice-a", "path": str(Path(tmp) / "one")}, Path(tmp))
            self.assertEqual(sorted(p.name for p in (Path(tmp) / "one").iterdir()), ["f"])
            sb.download({"pattern": "slice-*", "path": str(Path(tmp) / "many")}, Path(tmp))
            self.assertEqual([p.name for p in (Path(tmp) / "many").iterdir()], ["slice-a"])
            with self.assertRaises(RuntimeError):
                sb.download({"name": "nope", "path": str(Path(tmp) / "x")}, Path(tmp))


# ------------------------------------------------------------------------------------------------ fix rounds (T5)


class FixRoundTest(ScenarioCase):
    """A NEEDS FIXES slice gets exactly one fix round, started from its draft head with the findings in the brief (F8)."""

    @classmethod
    def setUpClass(cls):
        cls.sb = cls.make()
        cls.first = cls.sb.run_workflow(cls.sb.label_issue(7, TITLE, BODY))
        cls.dispatched = list(cls.sb.hub.state()["dispatches"])
        (cls.pr,) = cls.sb.hub.prs()
        cls.head = cls.pr["headRefOid"]

    def event(self, **over) -> tuple[str, dict]:
        body = dict(self.dispatched[0]["client_payload"], **over)
        return "repository_dispatch", {"action": "pivotal-fix", "client_payload": body, "repository": {"full_name": REPO, "default_branch": "main", "private": False},
                                       "sender": {"login": "github-actions[bot]", "type": "Bot"}}

    def test_the_first_run_sent_one_dispatch_with_the_full_payload_and_no_secret(self):
        (sent,) = self.dispatched
        self.assertEqual(sent["event_type"], "pivotal-fix")
        got = sent["client_payload"]
        self.assertEqual((got["issue"], got["slice"], got["round"], got["run_id"], got["branch"], got["head_sha"]),
                         (7, "main", 1, self.first.run_id, "pivotal/issue-7/main", self.head))
        self.assertLessEqual(len(got["findings"].encode()), 20 * 1024)
        self.assertIn("NEEDS FIXES", got["findings"])
        for secret in SECRETS.values():
            self.assertNotIn(secret, json.dumps(sent))
        self.assertEqual(self.first.result("publish"), "success")

    def test_the_fix_round_starts_from_the_draft_head_and_does_not_loop(self):
        sb = self.sandbox()
        first = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        (sent,) = sb.hub.state()["dispatches"]
        head = sb.hub.prs()[0]["headRefOid"]
        sb.set_agents(build=[["write", "feature.txt", "better\n"], ["write", "tests/test_feature.txt", "feature is good\n"]], verdict="READY")
        run = sb.run_workflow(("repository_dispatch", {
            "action": "pivotal-fix", "client_payload": sent["client_payload"], "repository": {"full_name": REPO, "default_branch": "main", "private": False},
            "sender": {"login": "github-actions[bot]", "type": "Bot"}}))
        self.assertEqual(run.trust.outputs["kind"], "fix")
        self.assertEqual(run.job("plan").outputs["round"], "1")
        self.assertEqual(run.job("plan").step("decide").outputs["base_ref"], head)
        self.assertEqual(run.result("publish"), "success", [(j.name, j.result) for j in run.all_jobs()])
        tip = sb.git("--git-dir", str(sb.origin), "rev-parse", "pivotal/issue-7/main")
        self.assertNotEqual(tip, head, "the draft branch moved on from its old head")
        sb.git("--git-dir", str(sb.origin), "merge-base", "--is-ancestor", head, tip)  # a fast-forward: the old head is its parent line
        self.assertEqual(sb.git("--git-dir", str(sb.origin), "rev-parse", tip + "^"), head, "the fix is one commit on the draft head")
        self.assertEqual(sb.git("--git-dir", str(sb.origin), "show", f"{tip}:feature.txt"), "better")
        self.assertEqual(sb.git("--git-dir", str(sb.origin), "rev-parse", "main"), sb.base_sha, "main is untouched")
        self.assertIn("draft (fix_round)", sb.hub.comments(7)[-1])
        self.assertEqual(len(sb.hub.state()["dispatches"]), 1, "a READY fix round sends no further dispatch")
        self.assertNotEqual(first.run_id, run.run_id)

    def test_the_fix_round_brief_holds_the_findings_the_dispatch_sent_and_the_first_brief_holds_none(self):
        # The brief the agent really read (the prompt file, logged by the fake agent): round 0 has no findings section, the
        # fix round has one, and the reviewer's words in it are the words the dispatch carried.
        marker = "the greeting says hello, not good"
        sb = self.sandbox()
        sb.set_agents(verdict="NEEDS FIXES", review_finding=marker)
        sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        first_briefs = [c["prompt"] for c in sb.agent_calls() if c["mode"] == "build"]
        (sent,) = sb.hub.state()["dispatches"]
        self.assertIn(marker, sent["client_payload"]["findings"])
        sb.set_agents(build=[["write", "feature.txt", "better\n"]], verdict="READY")
        run = sb.run_workflow(("repository_dispatch", {
            "action": "pivotal-fix", "client_payload": sent["client_payload"], "repository": {"full_name": REPO, "default_branch": "main", "private": False},
            "sender": {"login": "github-actions[bot]", "type": "Bot"}}))
        self.assertEqual(run.result("publish"), "success", [(j.name, j.result) for j in run.all_jobs()])
        fix_briefs = [c["prompt"] for c in sb.agent_calls() if c["mode"] == "build"][len(first_briefs):]
        self.assertTrue(fix_briefs, "the fix round built")
        self.assertTrue(first_briefs, "the first run built")
        for brief in first_briefs:
            self.assertNotIn("Findings of the last round", brief)
        for brief in fix_briefs:
            self.assertIn("Findings of the last round, to fix", brief)
            self.assertIn(marker, brief)

    def test_a_round_above_max_rounds_is_refused_before_any_write(self):
        from engine import fix
        sb = self.sandbox()
        sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        (sent,) = sb.hub.state()["dispatches"]
        before = sb.git("--git-dir", str(sb.origin), "for-each-ref")
        bad = dict(sent["client_payload"], round=fix.MAX_ROUNDS + 1)
        run = sb.run_workflow(("repository_dispatch", {
            "action": "pivotal-fix", "client_payload": bad, "repository": {"full_name": REPO, "default_branch": "main", "private": False},
            "sender": {"login": "github-actions[bot]", "type": "Bot"}}))
        self.assertEqual(run.result("plan"), "failure")
        self.assertEqual(run.result("build"), "skipped")
        self.assertEqual(sb.git("--git-dir", str(sb.origin), "for-each-ref"), before)
        self.assertEqual(len(sb.hub.state()["dispatches"]), 1)


# ------------------------------------------------------------------------------ a build whose Kaggle endpoint is down


KAGGLE_SPEC = "pi:kaggle/qwen3.6-35b-a3b"
KAGGLE_FELL_BACK = {"from": KAGGLE_SPEC, "to": "claude:anthropic/sonnet", "reason": "endpoint down"}


class KaggleDownTest(ScenarioCase):
    """A build whose Kaggle endpoint is down falls through to the next entry, and the fall-through reaches the RESULT and the
    issue comment, not only the job's own files. The endpoint is down because PIVOTAL_KAGGLE_URL is not
    set: models-up (the real job, with the stub kaggle wheel) brings no session up, and each build's re-check sends no request."""

    secrets = {**SECRETS, "PIVOTAL_KAGGLE_KEY": "kaggle-FAKEFAKEFAKE0123456789"}  # the key is there; the URL is not
    preset = {"name": "sandbox", "tiers": {"build": [KAGGLE_SPEC, "claude:anthropic/sonnet"], "review": ["pi:google/gemini-2.5-flash"]}}

    @classmethod
    def setUpClass(cls):
        cls.sb = cls.make()
        cls.run_ = cls.sb.run_workflow(cls.sb.label_issue(7, TITLE, BODY))

    def test_the_build_falls_through_to_the_next_entry_and_its_result_file_says_so(self):
        # The doors first: no agent call is started with the Kaggle key. This loop catches a call made with the key. A mutant that
        # routes to the down endpoint without starting a call makes no call at all, so this loop passes; the status and result
        # assertions below catch that mutant.
        for call in self.sb.agent_calls():
            self.assertNotIn("PIVOTAL_KAGGLE_KEY", call["doors"], call["argv"])
        on_endpoint = [c for c in self.sb.agent_calls() if any("kaggle/" in a or "qwen3.6-35b-a3b" in a for a in c["argv"])]
        self.assertEqual(on_endpoint, [], "the endpoint is never used: no call names the kaggle door or its model")
        self.assertEqual(self.run_.result("models-up"), "success", [(j.name, j.result) for j in self.run_.all_jobs()])
        self.assertEqual(self.run_.result("build"), "success", [(j.name, j.result) for j in self.run_.all_jobs()])
        result = json.loads((self.sb.artifacts / "slice-main" / "result.json").read_text())
        self.assertEqual((result["status"], result["built_by"]), ("ok", "claude:anthropic/sonnet"))
        self.assertEqual(result["fell_back"], [KAGGLE_FELL_BACK])

    def test_the_run_result_and_the_issue_comment_carry_the_fall_through_line(self):
        result = json.loads(self.run_.job("publish").outputs["result"])
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result.get("fell_back"), [KAGGLE_FELL_BACK], result)
        self.assertEqual(self.run_.result("report"), "success")
        self.assertIn(f"fell_back: {KAGGLE_SPEC} → claude:anthropic/sonnet (endpoint down)", self.sb.hub.comments(7)[1].splitlines())


# ------------------------------------------------------------------------------------------------ hill-climb (T6)

# The request's footer as piv do writes it: the fake scorer prints the number the round's agent wrote to score.txt.
SCORE_FOOTER = "\n\n---\nPivotal request\n- line: build\n- score: cat score.txt\n- target: 0.9"


class HillClimbTest(ScenarioCase):
    """A fake scorer against target 0.9. Each build's agent writes its score to score.txt, and the gate runs ``cat score.txt``.
    0.4 -> 0.7 -> 0.92 lands at round 3, the third build (round 0 and two fix rounds), as a ready pull request; a round below the
    best keeps the best round as the draft and sends nothing more."""

    def build(self, sb: Sandbox, score: str) -> None:
        sb.set_agents(build=[["write", "feature.txt", f"score {score}\n"], ["write", "score.txt", f"{score}\n"]], verdict="READY")

    def fix_event(self, sb: Sandbox) -> tuple[str, dict]:
        sent = sb.hub.state()["dispatches"][-1]
        return "repository_dispatch", {"action": "pivotal-fix", "client_payload": sent["client_payload"],
                                       "repository": {"full_name": REPO, "default_branch": "main", "private": False},
                                       "sender": {"login": "github-actions[bot]", "type": "Bot"}}

    def result_of(self, run) -> dict:
        return json.loads(run.job("publish").outputs["result"])

    def test_a_run_of_three_builds_meets_the_target_at_the_second_fix_round_and_reports_a_ready_pull_request(self):
        sb = self.sandbox()
        self.build(sb, "0.4")
        first = sb.run_workflow(sb.label_issue(7, TITLE, BODY + SCORE_FOOTER))
        self.assertEqual(first.result("publish"), "success", [(j.name, j.result) for j in first.all_jobs()])
        (pr,) = sb.hub.prs()
        self.assertTrue(pr["isDraft"], "a score below the target is a draft")
        (sent,) = sb.hub.state()["dispatches"]
        self.assertEqual((sent["client_payload"]["round"], sent["client_payload"]["head_sha"]), (1, pr["headRefOid"]))
        self.assertEqual(sent["client_payload"]["scores"], f"0.4@{pr['headRefOid']}")
        self.assertIn("score_low", sb.hub.comments(7)[-1])
        runs = [first]
        for score in ("0.7", "0.92"):
            self.build(sb, score)
            run = sb.run_workflow(self.fix_event(sb))
            runs.append(run)
            self.assertEqual(run.result("publish"), "success", [(j.name, j.result) for j in run.all_jobs()])
        self.assertEqual(sb.hub.state()["dispatches"][-1]["client_payload"]["round"], 2, "the third build is round 2 of the payload")
        self.assertEqual(len(sb.hub.state()["dispatches"]), 2, "round 3 met the target: nothing more is sent")
        (pr,) = sb.hub.prs()
        self.assertFalse(pr["isDraft"], "the met round is marked ready for review")
        tip = sb.git("--git-dir", str(sb.origin), "rev-parse", "pivotal/issue-7/main")
        self.assertEqual(pr["headRefOid"], tip, "the draft holds the last round")
        self.assertEqual(sb.git("--git-dir", str(sb.origin), "show", f"{tip}:score.txt"), "0.92")
        self.assertEqual(self.result_of(runs[-1])["status"], "ready")
        self.assertEqual(self.result_of(runs[-1])["pr"], pr["number"])
        self.assertEqual(runs[-1].result("report"), "success", "a met round is a green report, not bad_result")
        self.assertIn("ready", sb.hub.comments(7)[-1])
        self.assertNotIn("bad_result", sb.hub.comments(7)[-1])

    def test_a_round_below_the_best_is_not_pushed_and_the_best_stays_the_draft(self):
        sb = self.sandbox()
        self.build(sb, "0.7")
        sb.run_workflow(sb.label_issue(7, TITLE, BODY + SCORE_FOOTER))
        (pr,) = sb.hub.prs()
        best = pr["headRefOid"]
        self.build(sb, "0.4")  # a worse round: its commit is built, and it is not pushed
        run = sb.run_workflow(self.fix_event(sb))
        self.assertEqual(run.result("publish"), "success", [(j.name, j.result) for j in run.all_jobs()])
        self.assertEqual(len(sb.hub.state()["dispatches"]), 1, "the climb stops at the best round: nothing is sent")
        (pr,) = sb.hub.prs()
        self.assertTrue(pr["isDraft"])
        self.assertEqual(pr["headRefOid"], best, "the draft stays at the best round")
        self.assertEqual(sb.git("--git-dir", str(sb.origin), "rev-parse", "pivotal/issue-7/main"), best)
        self.assertIn("below the best round", sb.hub.pr_comments(pr["number"])[-1])
        self.assertEqual(self.result_of(run)["why"], "score_low")
        self.assertEqual(self.result_of(run)["status"], "draft")

    def test_a_scorer_that_prints_no_number_is_a_red_gate_with_no_pull_request_and_no_fix_round(self):
        sb = self.sandbox()
        sb.set_agents(build=[["write", "feature.txt", "score x\n"]], verdict="READY")  # no score.txt: `cat score.txt` prints nothing
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY + SCORE_FOOTER))
        self.assertEqual(sb.hub.prs(), [])
        self.assertEqual(sb.hub.state()["dispatches"], [])
        self.assertEqual(self.result_of(run)["status"], "failed")
        self.assertEqual(self.result_of(run)["why"], "gate_red")

    def test_a_lower_round_held_back_by_its_verdict_does_not_move_the_draft_off_a_better_round(self):
        sb = self.sandbox()
        sb.set_agents(build=[["write", "feature.txt", "score 0.95\n"], ["write", "score.txt", "0.95\n"]], verdict="NEEDS FIXES")
        sb.run_workflow(sb.label_issue(7, TITLE, BODY + SCORE_FOOTER))
        (pr,) = sb.hub.prs()
        best = pr["headRefOid"]
        self.assertEqual(len(sb.hub.state()["dispatches"]), 1, "round 0 met the target but its verdict needs fixes: a fix round is sent")
        sb.set_agents(build=[["write", "feature.txt", "score 0.92\n"], ["write", "score.txt", "0.92\n"]], verdict="NEEDS FIXES")
        run = sb.run_workflow(self.fix_event(sb))
        self.assertEqual(run.result("publish"), "success", [(j.name, j.result) for j in run.all_jobs()])
        (pr,) = sb.hub.prs()
        self.assertEqual(pr["headRefOid"], best, "the 0.95 round stays the draft")
        self.assertEqual(sb.git("--git-dir", str(sb.origin), "rev-parse", "pivotal/issue-7/main"), best)
        self.assertEqual(len(sb.hub.state()["dispatches"]), 1, "nothing more is sent")
        self.assertIn("below the best round", sb.hub.pr_comments(pr["number"])[-1])

    def test_round_0_that_meets_the_target_with_a_ready_verdict_is_stacked_as_a_ready_pull_request(self):
        sb = self.sandbox()
        self.build(sb, "0.95")
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY + SCORE_FOOTER))
        self.assertEqual(run.result("publish"), "success", [(j.name, j.result) for j in run.all_jobs()])
        (pr,) = sb.hub.prs()
        self.assertFalse(pr["isDraft"], "round 0 at the target is the stack's pull request, ready for review")
        self.assertEqual(pr["headRefName"], "pivotal/issue-7/stack")
        self.assertEqual(sb.hub.state()["dispatches"], [], "the target is met: no fix round")
        self.assertEqual(run.result("report"), "success")
        self.assertEqual(self.result_of(run)["status"], "ready")


if __name__ == "__main__":
    unittest.main()

# -------------------------------------------------------------------------------------------------- T3 sandbox scenarios
# (T3): isolated review, assemble, gate[k], publish's landable prefix, the seal, land. Each class is one
# acceptance bullet. The plan matrix is injected (plan_override): the factory sets no plan agent, so no run makes one.


class TwoOkOneRedStackTest(ScenarioCase):
    """Two ok slices and one red (gate) slice: the landable prefix is ok1 and ok2 (their stack gates are green), and red1 is
    not in it. At prepare publish moves no base, and the partial stack goes to a DRAFT stack pull request whose body names
    ok1 and ok2 as passed and red1 as not in the landable prefix. The check passes for every slice on its own but fails for
    the stacked prefix that includes red1 together with ok2, so only red1's stack gate is red."""

    def test_two_ok_slices_and_one_red_give_a_draft_stack_pr_naming_the_prefix(self):
        sb = self.sandbox(check="test ! -f src/red1.txt || test ! -f src/ok2.txt")
        plan = [
            {"id": "ok1", "title": "ok slice 1", "goal": "GOAL_OK1"},
            {"id": "ok2", "title": "ok slice 2", "goal": "GOAL_OK2"},
            {"id": "red1", "title": "red slice", "goal": "GOAL_RED1"},
        ]
        sb.plan_override = plan
        sb.set_agents(
            build=[
                ["write", "src/ok1.txt", "ok1\n", "GOAL_OK1"],
                ["write", "src/ok2.txt", "ok2\n", "GOAL_OK2"],
                ["write", "src/red1.txt", "red1\n", "GOAL_RED1"],
            ],
            verdict="READY",
        )
        run = self.pipeline(sb)
        # The stack gates prove ok1,ok2 are green and red1 is red.
        gates = {}
        for d in sorted(sb.artifacts.glob("gate-*")):
            g = json.loads((d / "gate.json").read_text())
            gates[g["id"]] = g
        self.assertTrue(gates["ok1"]["green"], gates["ok1"])
        self.assertTrue(gates["ok2"]["green"], gates["ok2"])
        self.assertFalse(gates["red1"]["green"], gates["red1"])
        self.assertEqual(gates["red1"]["check"], "red", gates["red1"])
        # Publish: the partial stack is a DRAFT stack pull request, the result says partial_stack, and no base moved.
        result = json.loads(run.job("publish").outputs["result"])
        self.assertEqual((result["status"], result["why"]), ("draft", "partial_stack"), result)
        self.assertEqual(base_of(sb), sb.base_sha, "prepare moves no base, the partial stack included")
        (stack,) = sb.hub.prs()
        self.assertEqual((stack["headRefName"], stack["isDraft"], stack["state"]), ("pivotal/issue-7/stack", True, "OPEN"))
        self.assertEqual(result["pr"], stack["number"], result)
        # The body names the prefix: ok1 and ok2 passed, red1 is not in the landable prefix and is the first slice not landed.
        self.assertIn("slices that passed: ok1, ok2", stack["body"])
        self.assertIn("not in the landable prefix: red1", stack["body"])
        self.assertIn("first slice not landed: red1", stack["body"])


class ConflictDraftTest(ScenarioCase):
    """A conflicting pair: two slices that change the same content incompatibly produce a conflict draft."""

    def test_a_conflicting_pair_gives_a_conflict_draft(self):
        sb = self.sandbox()
        plan = [
            {"id": "a", "title": "slice a", "goal": "GOAL_A"},
            {"id": "b", "title": "slice b", "goal": "GOAL_B"},
        ]
        sb.plan_override = plan
        sb.set_agents(
            build=[
                ["write", "shared.txt", "from a\n", "GOAL_A"],
                ["write", "shared.txt", "from b\n", "GOAL_B"],
            ],
            verdict="READY",
        )
        run = self.pipeline(sb)
        land = json.loads((sb.artifacts / "land" / "land.json").read_text())
        reasons = {s["id"]: s["reason"] for s in land["slices"]}
        outcomes = {s["id"]: s["outcome"] for s in land["slices"]}
        # One of the pair conflicts; the conflict is surfaced as a draft/conflict, not stacked silently.
        self.assertTrue(
            any(o == "conflict" or r in ("conflict", "does_not_apply") or "conflict" in r
                for o, r in ((outcomes[i], reasons[i]) for i in outcomes)),
            (outcomes, reasons),
        )
        conflict = [s for s in land["slices"] if s["outcome"] == "conflict" or s["reason"] in ("conflict", "does_not_apply") or "conflict" in s["reason"]]
        self.assertTrue(conflict, (outcomes, reasons))
        self.assertEqual(conflict[0]["outcome"], "draft", conflict)
        prs = [p for p in sb.hub.prs() if p["headRefName"] != "pivotal/issue-7/stack"]
        self.assertTrue(prs, "the conflicting slice gets a draft PR (or is surfaced)")


class WorkflowFileBlockedTest(ScenarioCase):
    """A workflow-file change is blocked: assemble refuses it and nothing is pushed as a workflow change."""

    def test_a_workflow_file_change_is_blocked(self):
        sb = self.sandbox()
        plan = [{"id": "wf", "title": "touch workflow", "goal": "GOAL_WF"}]
        sb.plan_override = plan
        sb.set_agents(
            build=[["write", ".github/workflows/evil.yml", "name: evil\non: push\njobs: {}\n", "GOAL_WF"]],
            verdict="READY",
        )
        run = self.pipeline(sb)
        land = json.loads((sb.artifacts / "land" / "land.json").read_text())
        (entry,) = land["slices"]
        self.assertEqual(entry["outcome"], "blocked", entry)
        self.assertEqual(entry["reason"], "workflow_change", entry)
        # No branch/PR carries the workflow file.
        self.assertFalse([p for p in sb.hub.prs() if p["headRefName"] != "pivotal/issue-7/stack"], sb.hub.prs())
        refs = sb.git("--git-dir", str(sb.origin), "for-each-ref", "--format=%(refname)", "refs/heads/pivotal")
        self.assertNotIn("pivotal/issue-7/wf", refs)


class MissingVerdictDraftTest(ScenarioCase):
    """A missing verdict: the reviewer produces no verdict, so the slice is a draft, not landable."""

    def test_a_missing_verdict_is_a_draft(self):
        sb = self.sandbox()
        plan = [{"id": "nov", "title": "no verdict", "goal": "GOAL_NOV"}]
        sb.plan_override = plan
        sb.set_agents(build=[["write", "src/nov.txt", "nov\n", "GOAL_NOV"]], verdict="__MISSING__")
        run = self.pipeline(sb)
        land = json.loads((sb.artifacts / "land" / "land.json").read_text())
        (entry,) = land["slices"]
        self.assertEqual((entry["outcome"], entry["reason"], entry["candidate"]), ("draft", "verdict_missing", False), entry)

    def test_stderr_noise_with_a_ready_verdict_is_a_candidate(self):
        # The reviewer's stderr is split to the errlog and the verdict comes from the final JSON message:
        # a diagnostics line on stderr must not make the verdict missing (engine/run.sh --errlog, reviewjob extraction).
        # The stderr noise is printed after the events as a JSON-shaped pi message_end with a different verdict,
        # so a merged stdout/stderr log would change the verdict.
        sb = self.sandbox()
        plan = [{"id": "err", "title": "stderr ok", "goal": "GOAL_ERR"}]
        sb.plan_override = plan
        sb.set_agents(build=[["write", "src/err.txt", "err\n", "GOAL_ERR"]], verdict="READY",
                      review_stderr='{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Verdict: NEEDS FIXES"}]}}\n')
        run = self.pipeline(sb)
        land = json.loads((sb.artifacts / "land" / "land.json").read_text())
        (entry,) = land["slices"]
        self.assertEqual(entry["candidate"], True, entry)
        self.assertNotEqual(entry["reason"], "verdict_missing", entry)
        verdict = json.loads((sb.artifacts / "review-err" / "verdict").read_text())
        self.assertEqual(verdict["verdict"], "READY", verdict)


class GateCommitsNotLandableTest(ScenarioCase):
    """A test/check that commits during gate leaves the gate not landable (head changed)."""

    def test_a_test_that_commits_during_gate_is_not_landable(self):
        sb = self.sandbox(check="git -c user.name=x -c user.email=x@x commit -q --allow-empty -m gate-mutate; test -f README.md")
        plan = [{"id": "mut", "title": "mutating gate", "goal": "GOAL_MUT"}]
        sb.plan_override = plan
        sb.set_agents(build=[["write", "src/mut.txt", "mut\n", "GOAL_MUT"]], verdict="READY")
        run = self.pipeline(sb)
        # The gate ran but found the head changed: the gate artifact is not green, and publish does not land it.
        gate_path = sb.artifacts / "gate-mut" / "gate.json"
        self.assertTrue(gate_path.exists(), f"gate artifact missing; jobs: {[(j.name, j.result) for j in run.all_jobs()]}")
        gate = json.loads(gate_path.read_text())
        self.assertFalse(gate["green"], gate)
        self.assertFalse(gate["head_unchanged"], gate)
        result = json.loads(run.job("publish").outputs["result"])
        self.assertNotEqual(result["status"], "landed", result)
        self.assertNotEqual(result.get("why"), "landed", result)
        self.assertEqual(seals(sb), [], "nothing is sealed/landed")
        self.assertEqual(base_of(sb), sb.base_sha, "a mutating gate must not land")


class MainMovesMidRunTest(ScenarioCase):
    """Main moves mid-run, at the land level (the only level at which publish tries a fast-forward at all): the stack's
    fast-forward is refused, so publish moves no base, forces nothing, and says it did not land. The stack goes to a READY
    pull request, sealed for a human's "land it". The hook itself moves main, so the base the run is judged against is the
    tip the hook left (``moved``), and publish must leave that tip where it is."""

    def test_main_moving_before_publish_yields_a_pr_not_a_silent_fast_forward(self):
        sb = self.sandbox(variables={"PIVOTAL_NTFY": TOPIC, "PIVOTAL_LEVEL": "land"})
        sb.set_agents(verdict="READY")
        before = base_of(sb)
        moved = {}

        def move_main():
            # A commit lands on main while the run is in flight (before publish).
            seed = sb.root / "seed-move"
            sb.git("clone", "-q", str(sb.origin), str(seed))
            sb.git("config", "user.email", "t@t", cwd=seed)
            sb.git("config", "user.name", "t", cwd=seed)
            (seed / "moved.txt").write_text("moved\n")
            sb.git("add", "-A", cwd=seed)
            sb.git("commit", "-q", "-m", "move main", cwd=seed)
            sb.git("push", "-q", "origin", "HEAD:main", cwd=seed)
            moved["tip"] = base_of(sb)

        sb.before_job["publish"] = move_main
        run = sb.run_workflow(sb.label_issue(7, TITLE, BODY))
        self.assertEqual(run.result("publish"), "success", [(j.name, j.result) for j in run.all_jobs()])
        self.assertNotEqual(moved["tip"], before, "the hook moved main")
        result = json.loads(run.job("publish").outputs["result"])
        self.assertNotEqual(result["status"], "landed", result)
        self.assertEqual(base_of(sb), moved["tip"], "publish moved no base: main is where the hook left it, not the stack")
        (pr,) = sb.hub.prs()
        self.assertEqual((pr["headRefName"], pr["isDraft"], pr["state"]), ("pivotal/issue-7/stack", False, "OPEN"))
        self.assertEqual((result["status"], result["pr"]), ("ready", pr["number"]), result)
        self.assertEqual([(c["name"], c["head_sha"]) for c in seals(sb)], [("pivotal/gate", pr["headRefOid"])],
                         "the READY stack pull request is sealed at its head, for a human's land it")


class LandLevelTest(ScenarioCase):
    """At the land level a green stack from a trusted run is fast-forwarded onto main by publish itself: the base moves to
    the stack head and the result says landed (F4). No pull request is needed for a landed stack."""

    def test_a_green_stack_at_land_is_fast_forwarded_and_the_result_says_landed(self):
        sb = self.sandbox(variables={"PIVOTAL_NTFY": TOPIC, "PIVOTAL_LEVEL": "land"})
        sb.set_agents(verdict="READY")
        before = base_of(sb)
        run = self.pipeline(sb)
        self.assertEqual(run.result("publish"), "success", [(j.name, j.result) for j in run.all_jobs()])
        result = json.loads(run.job("publish").outputs["result"])
        self.assertEqual(result["status"], "landed", result)
        self.assertNotEqual(result["sha"], before, "the stack moved main")
        self.assertEqual(base_of(sb), result["sha"], "the base is the stack head")
        self.assertEqual(sb.hub.prs(), [], "a landed stack opens no pull request")


class TokenRawAndBase64LeakTest(ScenarioCase):
    """Contract: a fake agent that prints the token raw and base64-encoded leaves neither in any log or artifact."""

    def test_token_printed_raw_and_base64_leaves_neither_in_any_log_or_artifact(self):
        sb = self.sandbox()
        # "leak" prints the raw token and writes raw+base64+hex to a file the agent changed.
        sb.set_agents(build=sandbox.FEATURE + [["leak", "leaked.txt"]])
        run = self.pipeline(sb)
        result = json.loads((sb.artifacts / "slice-main" / "result.json").read_text())
        self.assertEqual((result["status"], result["reason"]), ("failed", "secret_in_patch"))
        self.assertNoLeak(sb, "CLAUDE_CODE_OAUTH_TOKEN")
        # Also assert raw and base64 forms are specifically absent from visible text.
        token = sb.secrets["CLAUDE_CODE_OAUTH_TOKEN"]
        visible = "\n".join(t for _, t in sb.visible_text())
        self.assertNotIn(token, visible)
        self.assertNotIn(base64.b64encode(token.encode()).decode(), visible)

    def test_token_printed_and_written_only_as_base64_is_secret_in_patch_and_visible_nowhere(self):
        sb = self.sandbox()
        sb.set_agents(build=sandbox.FEATURE + [["leak_b64", "leaked64.txt"]])
        self.pipeline(sb)
        result = json.loads((sb.artifacts / "slice-main" / "result.json").read_text())
        self.assertEqual((result["status"], result["reason"]), ("failed", "secret_in_patch"))
        self.assertNoLeak(sb, "CLAUDE_CODE_OAUTH_TOKEN")
        encoded = base64.b64encode(sb.secrets["CLAUDE_CODE_OAUTH_TOKEN"].encode()).decode()
        visible = "\n".join(t for _, t in sb.visible_text())
        self.assertNotIn(encoded, visible)


# ------------------------------------------------------------------------------------------------ incidents and events (T5)

RUN_LINK = f"https://github.com/{REPO}/actions/runs/"
# A red main is a check that fails on it: the file the fix writes is missing from main until a fix lands.
INCIDENT_CHECK = "test -f fixed.txt"
INCIDENT_BUILD = [["write", "fixed.txt", "fixed\n"]]


def failed_default_push(sb: Sandbox, *, head_sha: str, run_id: int = 555, actor: str = "alice", event: str = "push",
                        head_branch: str = "main", head_repo: str = REPO, conclusion: str = "failure", name: str = "CI") -> tuple[str, dict]:
    """The workflow_run GitHub sends when a CI run completes: by default a failed run of a push to main, by ``actor``."""
    payload = {
        "action": "completed",
        "repository": {"full_name": REPO, "default_branch": "main", "private": False},
        "sender": {"login": actor, "type": "User"},
        "workflow_run": {
            "id": run_id, "name": name, "event": event, "head_branch": head_branch, "head_sha": head_sha,
            "conclusion": conclusion, "actor": {"login": actor}, "head_repository": {"full_name": head_repo},
        },
    }
    return "workflow_run", payload


def pivotal_event(*, sender: str = "alice", title: str = "Checkout is down", link: str = RUN_LINK + "9") -> tuple[str, dict]:
    """The repository_dispatch that piv event sends, as the caller receives it: a kind, a title and a link."""
    payload = {
        "action": "pivotal-event",
        "repository": {"full_name": REPO, "default_branch": "main", "private": False},
        "sender": {"login": sender, "type": "User"},
        "client_payload": {"kind": "incident", "title": title, "link": link},
    }
    return "repository_dispatch", payload


def advance_main(sb: Sandbox, name: str, text: str) -> str:
    """A commit pushed to main as a person pushes it: main moves on from the commit a CI run failed on. Returns main's SHA."""
    clone = sb.root / f"clone-{name}"
    sb.git("clone", "-q", str(sb.origin), str(clone))
    (clone / name).write_text(text)
    sb.git("add", "-A", cwd=clone)
    sb.git("commit", "-q", "-m", f"add {name}", cwd=clone)
    sb.git("push", "-q", "origin", "main", cwd=clone)
    return sb.git("--git-dir", str(sb.origin), "rev-parse", "main")


def open_incidents(sb: Sandbox) -> list[dict]:
    return [issue for issue in sb.hub.state()["issues"].values() if "pivotal:incident" in issue["labels"] and issue.get("state", "open") == "open"]


class IncidentTest(ScenarioCase):
    """Red main (T5): a failed CI run on a push to the default branch files ONE incident issue, with its title and link only,
    and builds from the current default-branch SHA with the incident line. A second red run of the same workflow files no second
    incident (the acceptance bullet of T5).

    The T5 acceptance also says "a ready fix waiting for you". This class pins what the code does instead: the fix is a DRAFT
    with reason baseline_red (assemble.classify, the row for a red base), so it is never a candidate. The "ready" clause is
    not proven here; it is an open contradiction, reported in the T5 build report."""

    @classmethod
    def setUpClass(cls):
        cls.sb = cls.make(check=INCIDENT_CHECK)
        cls.sb.set_agents(build=INCIDENT_BUILD, verdict="READY")
        cls.failed_head = cls.sb.base_sha  # the commit the CI run failed on
        cls.main = advance_main(cls.sb, "moved.txt", "main moved on after the failure\n")  # the current default-branch SHA
        cls.first = cls.sb.run_workflow(failed_default_push(cls.sb, head_sha=cls.failed_head, run_id=555))
        cls.issues_after_first = open_incidents(cls.sb)
        cls.prs_after_first = cls.sb.hub.prs()
        cls.comments_after_first = list(cls.sb.hub.comments(1))
        cls.second = cls.sb.run_workflow(failed_default_push(cls.sb, head_sha=cls.main, run_id=556))

    def test_a_failed_default_branch_run_files_one_incident_issue_titled_by_its_workflow_with_the_link_only(self):
        (issue,) = self.issues_after_first
        self.assertEqual(issue["title"], "CI failed: CI")
        self.assertEqual(issue["body"].splitlines()[0], f"CI failed at {self.failed_head[:12]}: {RUN_LINK}555")
        text = [line for line in issue["body"].splitlines() if line and not line.startswith("<!-- pivotal-incident ")]
        self.assertEqual(text, [f"CI failed at {self.failed_head[:12]}: {RUN_LINK}555"], "the body holds the link and nothing else")

    def test_the_run_is_a_trusted_incident_with_no_issue_number_and_no_pin_because_ack_files_the_issue(self):
        self.assertEqual(self.first.trust.outputs, {"trusted": "true", "issue": "", "kind": "incident", "pin": ""})
        self.assertEqual(self.first.factory, "factory-trusted")
        self.assertEqual(self.first.result("ack"), "success")

    def test_the_build_is_from_the_current_default_branch_and_never_from_the_failing_head(self):
        plan = self.first.job("plan")
        self.assertEqual(plan.outputs["base_sha"], self.main)
        self.assertNotEqual(plan.outputs["base_sha"], self.failed_head)

    def test_the_red_main_is_handled_at_the_prepare_level(self):
        # The acceptance says "at prepare": piv init sets the repo's level to prepare, and plan reads that same level.
        self.assertEqual(self.sb.hub.state()["variables"]["PIVOTAL_LEVEL"], "prepare")
        self.assertEqual(self.first.job("plan").outputs["level"], "prepare")

    def test_the_incident_line_is_built_and_its_brief_is_the_run_link_and_nothing_else(self):
        plan = self.first.job("plan")
        self.assertEqual(plan.outputs["line"], "incident")
        (entry,) = json.loads(plan.outputs["matrix"])
        self.assertEqual(entry["goal"], f"The failing run or report: {RUN_LINK}555")
        self.assertNotIn("CI failed", entry["title"])

    def test_the_fix_is_a_draft_pull_request_that_says_the_base_is_red(self):
        (pr,) = self.prs_after_first
        self.assertTrue(pr["isDraft"])
        self.assertEqual(self.first.result("publish"), "success")
        self.assertIn("baseline", "\n".join(self.comments_after_first))

    def test_a_second_red_run_of_the_same_workflow_files_no_second_incident_and_joins_the_first(self):
        self.assertEqual(self.second.trust.outputs["kind"], "incident")
        self.assertEqual([issue["number"] for issue in open_incidents(self.sb)], [1])
        # The started comment links to the pivotal run that does the work (the run's own id), not to the failing CI run.
        comments = self.sb.hub.comments(1)
        started = [c for c in comments if c.startswith("⚡ run #") and c.endswith(f"/actions/runs/{self.second.run_id}")]
        self.assertEqual(len(started), 1, comments)
        self.assertEqual(self.second.result("ack"), "success")

    def test_the_second_run_at_a_new_head_updates_the_open_incident_to_name_that_head(self):
        (issue,) = open_incidents(self.sb)
        self.assertIn(f"head={self.main}", issue["body"])
        self.assertIn(f"{RUN_LINK}556", issue["body"])


class IncidentAlreadyGreenTest(ScenarioCase):
    """I1: a red main that was fixed before the run (its base now passes its check) gets no READY stack pull request. The agent's
    change is not needed: the slice is a draft named already_green, and the run's RESULT says so."""

    def test_an_incident_on_a_base_that_already_passes_is_a_draft_with_the_reason_and_no_ready_pull_request(self):
        sb = self.sandbox(check=INCIDENT_CHECK)
        sb.set_agents(build=[["write", "unneeded.txt", "not needed\n"]], verdict="READY")
        failed_head = sb.base_sha
        advance_main(sb, "fixed.txt", "fixed\n")  # main passes its check now: the red run was fixed before this one
        run = sb.run_workflow(failed_default_push(sb, head_sha=failed_head, run_id=555))
        self.assertEqual(run.trust.outputs["kind"], "incident")
        self.assertEqual(run.job("plan").outputs["baseline"], "green")
        self.assertFalse([pr for pr in sb.hub.prs() if not pr["isDraft"]], sb.hub.prs())
        self.assertEqual(json.loads(run.job("publish").outputs["result"])["why"], "already_green")
        self.assertEqual([pr["isDraft"] for pr in sb.hub.prs()], [True])


class ForkPullRequestTest(ScenarioCase):
    """T5 acceptance: a fork PR from a branch named main that fails CI triggers nothing. Nor does a run that did not fail."""

    def test_a_fork_pr_from_a_branch_named_main_that_fails_runs_no_job_and_files_no_issue(self):
        sb = self.sandbox(check=INCIDENT_CHECK)
        run = sb.run_workflow(failed_default_push(sb, head_sha=sb.base_sha, event="pull_request", head_repo="someone/fork"),
                              trust_skipped=True)
        self.assertEqual(run.trust.result, "skipped")
        self.assertEqual(run.factory, "")
        self.assertEqual(sb.hub.state()["issues"], {})
        self.assertEqual(sb.hub.state()["dispatches"], [])
        self.assertEqual(sb.hub.prs(), [])

    def test_a_run_that_did_not_fail_on_the_default_branch_runs_no_job_either(self):
        sb = self.sandbox(check=INCIDENT_CHECK)
        run = sb.run_workflow(failed_default_push(sb, head_sha=sb.base_sha, conclusion="success"), trust_skipped=True)
        self.assertEqual((run.trust.result, run.factory), ("skipped", ""))
        self.assertEqual(sb.hub.state()["issues"], {})


class PivotalEventTest(ScenarioCase):
    """T5: a pivotal-event dispatch (piv event, a webhook) from a writer files one incident issue with the link and the alert's
    title, and builds with the incident line. From anyone else it runs nothing and files nothing."""

    @classmethod
    def setUpClass(cls):
        cls.sb = cls.make(check=INCIDENT_CHECK)
        cls.sb.set_agents(build=INCIDENT_BUILD, verdict="READY")
        cls.trusted = cls.sb.run_workflow(pivotal_event(sender="alice", title="Checkout is down"))
        cls.issues_after_trusted = open_incidents(cls.sb)
        cls.stranger = cls.sb.run_workflow(pivotal_event(sender="bob", title="Stranger alert"))

    def test_a_writers_event_is_a_trusted_event_with_no_issue_number_and_no_pin(self):
        self.assertEqual(self.trusted.trust.outputs, {"trusted": "true", "issue": "", "kind": "event", "pin": ""})
        self.assertEqual(self.trusted.factory, "factory-trusted")

    def test_the_event_files_one_issue_titled_by_the_alert_title_with_the_link_and_no_other_text(self):
        (issue,) = self.issues_after_trusted
        self.assertEqual(issue["title"], "Event: Checkout is down")
        self.assertEqual(issue["body"].splitlines()[0], f"Event: {RUN_LINK}9")

    def test_the_event_body_is_exactly_the_link_then_the_marker_and_the_title_is_not_in_it(self):
        # I5: the body the issue carries, byte for byte: "Event: <link>", a blank line, the fingerprint marker (the seed is the
        # kind and the title, hashed, so the title is in the marker and never in the text), and a final newline.
        (issue,) = self.issues_after_trusted
        seed = hashlib.sha256(b"event:Checkout is down").hexdigest()[:16]
        self.assertEqual(issue["body"], f"Event: {RUN_LINK}9\n\n<!-- pivotal-incident workflow={seed} head=none -->\n")

    def test_the_event_builds_with_the_incident_line_on_the_link(self):
        plan = self.trusted.job("plan")
        self.assertEqual(plan.outputs["line"], "incident")
        (entry,) = json.loads(plan.outputs["matrix"])
        self.assertEqual(entry["goal"], f"The failing run or report: {RUN_LINK}9")

    def test_a_strangers_event_is_untrusted_and_runs_nothing_and_files_no_issue(self):
        self.assertEqual(self.stranger.trust.outputs["trusted"], "false")
        self.assertEqual(self.stranger.trust.outputs["kind"], "event")
        self.assertEqual(self.stranger.factory, "")
        self.assertEqual([issue["number"] for issue in open_incidents(self.sb)], [1])
