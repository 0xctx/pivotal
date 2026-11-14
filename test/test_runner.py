"""Tests for `piv runner add` and `piv runner remove` (T8).

Nothing here changes the machine. The installation is a plan of steps: tests render it, and run it
against ``fakeexec.FakeExecutor``, which records instead of doing. The generated hook scripts run for
real, but only inside a temp directory fixture, on a fake work directory and fake leftover processes
that the test starts itself as its own user. ``RealExecutor`` is tried only on paths in a temp dir.
"""

import getpass
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))
sys.path.insert(0, str(ROOT / "test"))

import fakeexec  # noqa: E402
import fakegh  # noqa: E402
from piv import cli, runner  # noqa: E402
from piv.gh import PivError  # noqa: E402

GB = 2**30
TOKEN = "AATOKENSECRET0123456789"


def run_cli(args: list[str], gh: fakegh.FakeGh, executor: fakeexec.FakeExecutor):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(args, gh.env(), out, err, executor=executor)
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gh = fakegh.FakeGh(self.tmp / "gh")
        self.ex = fakeexec.FakeExecutor()

    def token_routes(self, kind="registration-token"):
        self.gh.route(f"actions/runners/{kind}", fakegh.ok(json.dumps({"token": TOKEN, "expires_at": "x"})))


class PlanTest(Base):
    def test_dry_run_is_the_default_and_changes_nothing(self):
        for extra in ([], ["--dry-run"], ["--yes", "--dry-run"]):
            code, out, err = run_cli(["runner", "add", "--org", "acme", *extra], self.gh, self.ex)
            self.assertEqual((code, err), (0, ""))
            self.assertIn("Plan (nothing has been changed):", out)
            self.assertIn("create the OS user pv-runner", out)
            self.assertIn("pv-runner@1.service", out)
        self.assertFalse(self.ex.changed())
        self.assertEqual(self.gh.calls(), [])  # no token requested for a plan

    def test_user_visible_check_runs_as_a_process(self):
        done = subprocess.run(
            [sys.executable, str(ROOT / "gh-piv" / "gh-piv"), "runner", "add", "--org", "acme", "--dry-run"],
            capture_output=True, text=True, env=self.gh.env(), check=False,
        )
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertIn("Plan (nothing has been changed)", done.stdout)
        self.assertIn("claude auth login", done.stdout)

    def test_plan_is_text_with_everything_the_order_names(self):
        plan = runner.build_plan(runner.Target("org", "acme"), 2, "box", arch="x86_64", ram_bytes=8 * GB, disk_free_bytes=100 * GB, cpus=4)
        text = plan.render()
        for needle in (
            "One organization runner serves every repo", "pv-runner@2.service", "job-started.sh", "job-completed.sh",
            "SHA-256", "PIVOTAL_RUNNER=self-hosted", "the run is trusted", "PIVOTAL_RUNNER_PUBLIC=trusted-only",
            "Untrusted work on a public repo always runs on GitHub-hosted VMs", "/pivotal go trusted", "GitHub Free, organization secrets",
            "register box-1 to https://github.com/acme", "register box-2",
        ):
            self.assertIn(needle, text)
        for line in runner.LOGIN_LINES:
            self.assertIn(line, text)
        self.assertNotIn(TOKEN, text)

    def test_personal_repo_says_one_repo_and_recommends_an_organization(self):
        text = runner.build_plan(runner.Target("repo", "me/app"), 1, "box", arch="aarch64", ram_bytes=4 * GB, disk_free_bytes=50 * GB, cpus=4).render()
        self.assertIn("a runner serves only ONE repo", text)
        self.assertIn("free GitHub organization", text)

    def test_architecture_selects_the_archive_and_its_checksum(self):
        for arch, kind in (("x86_64", "x64"), ("amd64", "x64"), ("aarch64", "arm64"), ("arm64", "arm64")):
            plan = runner.build_plan(runner.Target("org", "acme"), 1, "b", arch=arch, ram_bytes=4 * GB, disk_free_bytes=50 * GB, cpus=2)
            self.assertEqual(plan.machine.runner_arch, kind)
            download = next(s for s in plan.steps if s.argv[:1] == ("curl",))
            self.assertIn(f"actions-runner-linux-{kind}-{runner.RUNNER_VERSION}.tar.gz", download.argv[-1])
            verify = next(s for s in plan.steps if s.argv[:1] == ("sha256sum",))
            self.assertTrue(verify.stdin.startswith(runner.RUNNER_SHA256[kind] + "  "))
            self.assertTrue(verify.stdin.rstrip().endswith(download.argv[-2]))  # checks the file curl wrote
        with self.assertRaises(PivError) as raised:
            runner.build_plan(runner.Target("org", "acme"), 1, "b", arch="riscv64", ram_bytes=4 * GB, disk_free_bytes=50 * GB, cpus=2)
        self.assertEqual(raised.exception.code, 2)

    def test_sizing_follows_the_table(self):
        def machine(jobs, ram, cpus, disk=200, arch="x86_64"):
            return runner.check_machine(jobs, arch=arch, ram_bytes=int(ram * GB), disk_free_bytes=disk * GB, cpus=cpus)

        self.assertEqual(machine(1, 3.7, 4, arch="aarch64").warnings, ())  # Pi 4/5, 4 GB: 1
        self.assertTrue(machine(2, 3.7, 4, arch="aarch64").warnings)  # ... not 2
        self.assertEqual(machine(1, 3.8, 2).warnings, ())  # 2 vCPU / 4 GB: 1
        self.assertIn("light", " ".join(machine(2, 3.8, 2).warnings))  # ... 2 if tests are light
        self.assertEqual(machine(2, 7.6, 4).warnings, ())  # 4 vCPU / 8 GB: 2-3
        self.assertEqual(machine(3, 7.6, 4).warnings, ())
        self.assertTrue(machine(4, 7.6, 4).warnings)
        self.assertTrue(machine(3, 7.6, 2).warnings)  # more instances than CPUs
        for jobs, ram in ((1, 1.0), (3, 4.0), (5, 8.0)):  # RAM that cannot carry the count at all
            with self.assertRaisesRegex(PivError, "RAM"):
                machine(jobs, ram, 8)
        with self.assertRaisesRegex(PivError, "disk"):
            machine(2, 8, 4, disk=9)
        self.assertTrue(machine(1, 8, 4, disk=7).warnings)
        for bad in (0, -1, runner.MAX_JOBS + 1):
            with self.assertRaises(PivError):
                machine(bad, 8, 4)

    def test_refused_machine_stops_before_anything_is_asked_or_changed(self):
        code, out, err = run_cli(["runner", "add", "--org", "acme", "--jobs", "9", "--yes"], self.gh, self.ex)
        self.assertEqual(code, 1)
        self.assertIn("RAM", err)
        self.assertFalse(self.ex.changed())
        self.assertEqual(self.gh.calls(), [])

    def test_a_raspberry_pi_is_recognised_by_its_device_tree_model(self):
        """S-5: an 8 GB arm64 Pi looks like an 8 GB arm64 server to the RAM table."""
        pi = runner.check_machine(2, arch="aarch64", ram_bytes=8 * GB, disk_free_bytes=100 * GB, cpus=4, model="Raspberry Pi 5 Model B Rev 1.0")
        self.assertTrue(any("Raspberry Pi" in w for w in pi.warnings))
        other = runner.check_machine(2, arch="aarch64", ram_bytes=8 * GB, disk_free_bytes=100 * GB, cpus=4, model="")
        self.assertFalse(any("Raspberry Pi" in w for w in other.warnings))
        one = runner.check_machine(1, arch="aarch64", ram_bytes=8 * GB, disk_free_bytes=100 * GB, cpus=4, model="Raspberry Pi 4")
        self.assertEqual(one.warnings, ())

    def test_target_naming(self):
        t = runner.parse_target
        self.assertEqual(t("acme", None, None), runner.Target("org", "acme"))
        self.assertEqual(t(None, "acme", None), runner.Target("org", "acme"))
        self.assertEqual(t("me/app", None, None), runner.Target("repo", "me/app"))
        self.assertEqual(t(None, None, "me/app"), runner.Target("repo", "me/app"))
        self.assertIsNone(t(None, None, None))
        for bad in (("a", "b", None), ("me/app", None, "me/app"), ("a/b/c", None, None), ("../x", None, None), (None, None, "app"), (None, "a b", None)):
            with self.assertRaises(PivError, msg=bad):
                t(*bad)

    def test_detected_target(self):
        gh = fakegh.FakeGh(self.tmp / "g1")
        gh.route("api user/orgs", fakegh.ok('[{"login": "acme"}]'))
        self.assertEqual(runner.detect_target(runner.Gh(str(gh.path), gh.env())), runner.Target("org", "acme"))
        gh = fakegh.FakeGh(self.tmp / "g2")
        gh.route("api user/orgs", fakegh.ok('[{"login": "a"}, {"login": "b"}]'))
        with self.assertRaisesRegex(PivError, "--org"):
            runner.detect_target(runner.Gh(str(gh.path), gh.env()))
        gh = fakegh.FakeGh(self.tmp / "g3")
        gh.route("api user/orgs", fakegh.ok("[]"))
        with self.assertRaisesRegex(PivError, "ONE repo"):
            runner.detect_target(runner.Gh(str(gh.path), gh.env()))
        gh = fakegh.FakeGh(self.tmp / "g4")
        gh.route("api user/orgs", fakegh.ok('{"message": "x"}'))
        with self.assertRaises(PivError):
            runner.detect_target(runner.Gh(str(gh.path), gh.env()))

    def test_runner_name_is_checked_because_it_reaches_argv(self):
        for bad in ("a b", "x;rm", "-x", "a" * 70):
            with self.assertRaises(PivError):
                runner.runner_name(bad)
        self.assertEqual(runner.runner_name("pi-5.local"), "pi-5.local")

    def test_unit_runs_as_pv_runner_and_names_the_hooks_the_plan_installs(self):
        unit = runner.unit_text(1000)
        self.assertIn("User=pv-runner", unit)
        self.assertIn("ExecStart=/opt/pv-runner/runner-%i/run.sh", unit)
        self.assertIn("Environment=PV_RUNNER_WORK=/opt/pv-runner/runner-%i/_work", unit)
        self.assertNotIn("KillMode", unit)  # the default control-group stop kills a job's leftovers too
        self.assertNotIn("token", unit.lower())
        plan = runner.build_plan(runner.Target("org", "acme"), 1, "b", arch="x86_64", ram_bytes=4 * GB, disk_free_bytes=50 * GB, cpus=2)
        written = {s.path for s in plan.steps if s.op == "write"}
        for var in ("ACTIONS_RUNNER_HOOK_JOB_STARTED", "ACTIONS_RUNNER_HOOK_JOB_COMPLETED"):
            line = next(l for l in unit.splitlines() if var in l)
            self.assertIn(line.split("=", 2)[2], written)
            self.assertTrue(line.endswith(".sh"))


class AddTest(Base):
    def add(self, *extra, jobs=2):
        self.token_routes()
        return run_cli(["runner", "add", "--org", "acme", "--jobs", str(jobs), "--name", "box", "--yes", *extra], self.gh, self.ex)

    def setUp(self):
        super().setUp()
        # a machine the table accepts for 2 jobs, whatever this one is
        self.orig = runner.check_machine
        runner.check_machine = lambda jobs, **kw: self.orig(jobs, arch="x86_64", ram_bytes=8 * GB, disk_free_bytes=100 * GB, cpus=4)
        self.addCleanup(setattr, runner, "check_machine", self.orig)

    def test_installs_users_dirs_hooks_unit_runners_and_services_in_order(self):
        code, out, err = self.add()
        self.assertEqual((code, err), (0, ""))
        kinds = [c[0] if c[0] != "run" else Path(c[1][0]).name for c in self.ex.calls]
        self.assertEqual(kinds[0], "useradd")
        self.assertLess(kinds.index("curl"), kinds.index("sha256sum"))
        self.assertLess(kinds.index("sha256sum"), kinds.index("runuser"))  # nothing is unpacked before the checksum passes
        self.assertEqual(kinds.count("runuser"), 4)  # tar and config.sh, twice
        self.assertEqual(kinds[-3:], ["systemctl"] * 3)
        self.assertIn(("run", ("systemctl", "enable", "--now", "pv-runner@2.service")), self.ex.calls)
        self.assertIn("pv-runner", self.ex.users)
        self.assertEqual(self.ex.modes["/opt/pv-runner/hooks/job-started.sh"], 0o755)
        self.assertEqual(self.ex.dirs["/opt/pv-runner/hooks"], (0o755, "root"))  # pv-runner cannot edit its hooks
        self.assertEqual(self.ex.dirs["/opt/pv-runner/runner-2/_work"], (0o700, "pv-runner"))
        self.assertEqual(self.ex.files[runner.UNIT_PATH], runner.unit_text(3686))
        self.assertIn('mode="started"', self.ex.files["/opt/pv-runner/hooks/job-started.sh"])
        self.assertIn('mode="completed"', self.ex.files["/opt/pv-runner/hooks/job-completed.sh"])
        for line in runner.LOGIN_LINES:
            self.assertIn(line, out)
        for i in (1, 2):  # everything the runner and its tools run is as pv-runner, never root
            for entry in self.ex.run_log:
                if f"runner-{i}" in " ".join(entry["argv"]):
                    self.assertEqual(entry["argv"][:4], ("runuser", "-u", "pv-runner", "--"))

    def test_the_token_is_fetched_once_and_lives_only_in_one_process_environment(self):
        code, out, err = self.add()
        self.assertEqual(code, 0)
        self.assertEqual(len([c for c in self.gh.calls() if "registration-token" in " ".join(c["args"])]), 1)
        self.assertEqual(self.gh.calls()[0]["args"], ["api", "-X", "POST", "orgs/acme/actions/runners/registration-token"])
        configs = [e for e in self.ex.run_log if e["argv"][-1:] == ("--replace",)]
        self.assertEqual(len(configs), 2)
        for entry in configs:
            self.assertEqual(entry["env"], {"ACTIONS_RUNNER_INPUT_TOKEN": TOKEN})
        for entry in self.ex.run_log:
            if entry not in configs:
                self.assertEqual(entry["env"], {})
            self.assertNotIn(TOKEN, " ".join(entry["argv"]) + entry["stdin"] + entry["cwd"])
        everything = out + err + "".join(self.ex.files.values()) + json.dumps(self.gh.calls())
        self.assertNotIn(TOKEN, everything.replace(json.dumps({"token": TOKEN}), ""))

    def test_a_repo_target_asks_for_the_repo_token(self):
        self.token_routes()
        code, *_ = run_cli(["runner", "add", "--repo", "me/app", "--name", "box", "--yes"], self.gh, self.ex)
        self.assertEqual(code, 0)
        self.assertEqual(self.gh.calls()[0]["args"][-1], "repos/me/app/actions/runners/registration-token")
        self.assertIn("https://github.com/me/app", " ".join(self.ex.run_log[-3]["argv"]))

    def test_not_root_is_refused_before_anything(self):
        self.ex = fakeexec.FakeExecutor(euid=1000)
        code, out, err = self.add()
        self.assertEqual(code, 1)
        self.assertIn("must run as root", err)
        self.assertFalse(self.ex.changed())
        self.assertEqual(self.gh.calls(), [])

    def test_a_missing_tool_is_refused_before_anything(self):
        self.ex = fakeexec.FakeExecutor(tools=tuple(t for t in runner.TOOLS if t != "runuser"))
        code, out, err = self.add()
        self.assertEqual(code, 1)
        self.assertIn("runuser", err)
        self.assertFalse(self.ex.changed())
        self.assertEqual(self.gh.calls(), [])

    def test_a_token_gh_will_not_give_is_refused_before_any_change(self):
        self.gh.route("registration-token", fakegh.fail("gh: Not Found (HTTP 404)\n"))
        code, out, err = run_cli(["runner", "add", "--org", "acme", "--yes"], self.gh, self.ex)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 404", err)
        self.assertFalse(self.ex.changed())
        self.gh = fakegh.FakeGh(self.tmp / "g2")
        self.gh.route("registration-token", fakegh.ok("{}"))
        code, out, err = run_cli(["runner", "add", "--org", "acme", "--yes"], self.gh, self.ex)
        self.assertEqual(code, 1)
        self.assertIn("no registration-token", err)
        self.assertFalse(self.ex.changed())

    def test_a_checksum_mismatch_stops_before_unpacking_or_starting_anything(self):
        self.ex.sha_ok = False
        code, out, err = self.add()
        self.assertEqual(code, 1)
        self.assertIn("sha256sum failed", err)
        names = [Path(c[1][0]).name for c in self.ex.calls if c[0] == "run"]
        self.assertNotIn("runuser", names)
        self.assertNotIn("systemctl", names)
        self.assertNotIn(runner.UNIT_PATH, self.ex.files)

    def test_a_failure_message_never_carries_the_token(self):
        self.ex.fail["config.sh"] = f"bad token {TOKEN}"
        code, out, err = self.add()
        self.assertEqual(code, 1)
        self.assertIn("config.sh failed", err)
        self.assertNotIn(TOKEN, err + out)

    def test_a_rerun_skips_what_is_done_and_asks_for_no_token(self):
        self.assertEqual(self.add()[0], 0)
        before = len(self.ex.run_log)
        self.gh = fakegh.FakeGh(self.tmp / "g2")  # no routes: any gh call would fail
        code, out, err = run_cli(["runner", "add", "--org", "acme", "--jobs", "2", "--name", "box", "--yes"], self.gh, self.ex)
        self.assertEqual((code, err), (0, ""))
        again = [Path(e["argv"][0]).name for e in self.ex.run_log[before:]]
        self.assertNotIn("useradd", again)
        self.assertNotIn("runuser", again)  # already unpacked and registered
        self.assertEqual(again.count("systemctl"), 3)  # the services are still enabled
        self.assertEqual(self.gh.calls(), [])
        self.assertIn("skip (already done)", out)

    def registered(self, i, url="https://github.com/acme", name=None):
        self.ex.files[f"/opt/pv-runner/runner-{i}/.runner"] = json.dumps({"gitHubUrl": url, "agentName": name or f"box-{i}"})
        self.ex.dirs[f"/opt/pv-runner/runner-{i}"] = (0o700, "pv-runner")

    def refused(self, *args):
        before = list(self.ex.calls)
        code, out, err = run_cli(["runner", "add", *args, "--yes"], self.gh, self.ex)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.ex.calls, before)  # refused before the first change
        self.assertEqual(self.gh.calls(), [])
        self.assertIn("piv runner remove", err)
        return err

    def test_an_install_registered_elsewhere_is_refused_not_skipped(self):
        """I-3."""
        self.registered(1, url="https://github.com/other-org")
        self.assertIn("other-org", self.refused("--org", "acme", "--jobs", "1", "--name", "box"))
        self.assertIn("other-org", self.refused("--repo", "acme/app", "--jobs", "1", "--name", "box"))
        code, out, err = run_cli(["runner", "add", "--org", "acme", "--name", "box"], self.gh, self.ex)  # the dry run refuses too
        self.assertEqual(code, 1)

    def test_an_install_with_another_name_is_refused(self):
        self.registered(1, name="oldname-1")
        self.assertIn("oldname-1", self.refused("--org", "acme", "--jobs", "1", "--name", "box"))

    def test_an_unreadable_registration_is_refused(self):
        self.registered(1)
        self.ex.files["/opt/pv-runner/runner-1/.runner"] = "{not json"
        self.refused("--org", "acme", "--jobs", "1", "--name", "box")

    def test_instances_beyond_jobs_are_listed_and_left_alone(self):
        self.registered(1)
        self.registered(2)
        self.registered(3)
        self.token_routes()
        code, out, err = run_cli(["runner", "add", "--org", "acme", "--jobs", "1", "--name", "box"], self.gh, self.ex)
        self.assertEqual(code, 0, err)
        self.assertIn("runner-2, runner-3", out)
        self.assertIn("piv runner remove", out)
        self.assertEqual(self.ex.calls, [])

    def test_a_matching_install_is_still_skipped_as_done(self):
        self.registered(1)
        self.registered(2, url="https://github.com/acme/")
        code, out, err = run_cli(["runner", "add", "--org", "acme", "--jobs", "2", "--name", "box", "--yes"], self.gh, self.ex)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.gh.calls(), [])
        self.assertIn("skip (already done)", out)

    def test_the_unit_is_private_tmp_with_a_memory_ceiling_from_the_jobs_table(self):
        """S-2: 8 GB for 2 instances is under 4 GB each."""
        self.assertEqual(self.add()[0], 0)
        unit = self.ex.files[runner.UNIT_PATH]
        self.assertIn("PrivateTmp=yes", unit)
        self.assertIn("MemoryMax=3686M", unit)  # 8 GiB * 0.9 / 2
        self.assertEqual(runner.memory_max_mb(4.0, 1), 3686)

    def test_no_work_dir_is_made_by_root_config_creates_it(self):
        """I-2: root never touches anything inside the job user's directory but the runner's own files."""
        self.assertEqual(self.add()[0], 0)
        self.assertNotIn(("mkdir", "/opt/pv-runner/runner-1/_work", 0o700, "pv-runner"), self.ex.calls)

    def test_download_is_https_only_and_the_archive_is_world_readable(self):
        """S-3."""
        self.assertEqual(self.add()[0], 0)
        curl = next(e["argv"] for e in self.ex.run_log if e["argv"][0] == "curl")
        self.assertEqual(curl[curl.index("--proto") + 1], "=https")
        self.assertEqual(curl[curl.index("--proto-redir") + 1], "=https")
        kinds = [Path(e["argv"][0]).name for e in self.ex.run_log]
        self.assertLess(kinds.index("curl"), kinds.index("chmod"))
        self.assertLess(kinds.index("chmod"), kinds.index("runuser"))
        self.assertEqual(next(e["argv"] for e in self.ex.run_log if e["argv"][0] == "chmod")[:2], ("chmod", "0644"))

    def test_strict_fake_refuses_a_flag_the_real_command_lacks(self):
        with self.assertRaises(fakeexec.Refused):
            self.ex.run(("useradd", "--system-user", "x"))
        with self.assertRaises(fakeexec.Refused):
            self.ex.run(("runuser", "-u", "pv-runner", "--", "tar", "--bogus"))
        with self.assertRaises(fakeexec.Refused):
            self.ex.run(("rm", "-rf", "/"))
        with self.assertRaises(fakeexec.Refused):
            self.ex.run(("systemctl", "restart", "x"))


class RemoveTest(Base):
    def installed(self):
        for i in (1, 2):
            self.ex.dirs[f"/opt/pv-runner/runner-{i}"] = (0o700, "pv-runner")
            self.ex.files[f"/opt/pv-runner/runner-{i}/.runner"] = "﻿" + json.dumps({"gitHubUrl": "https://github.com/acme"})
        self.ex.dirs["/opt/pv-runner/hooks"] = (0o755, "root")
        self.ex.files["/opt/pv-runner/hooks/job-started.sh"] = "x"
        self.ex.files["/opt/pv-runner/hooks/job-completed.sh"] = "x"
        self.ex.files[runner.UNIT_PATH] = "x"
        self.ex.users.add("pv-runner")

    def test_dry_run_shows_the_plan_and_changes_nothing(self):
        self.installed()
        code, out, err = run_cli(["runner", "remove"], self.gh, self.ex)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("registered to acme", out)
        self.assertIn("deregister runner 2", out)
        self.assertIn("the user pv-runner and its home /home/pv-runner stay", out)
        self.assertFalse(self.ex.changed())
        self.assertEqual(self.gh.calls(), [])

    def test_leftovers_are_reported_and_removed_even_without_a_runner_directory(self):
        """S-4: a partial add left the hooks, the archive and the unit."""
        for path in (runner.UNIT_PATH, "/opt/pv-runner/hooks/job-started.sh", "/opt/pv-runner/dist/a.tar.gz"):
            self.ex.files[path] = "x"
        code, out, err = run_cli(["runner", "remove"], self.gh, self.ex)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"delete {runner.UNIT_PATH}", out)
        self.assertNotIn("nothing to remove", out)
        self.assertFalse(self.ex.changed())
        code, out, err = run_cli(["runner", "remove", "--yes"], self.gh, self.ex)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.gh.calls(), [])  # nothing to deregister, so no token
        self.assertFalse(any(self.ex.exists(p) for p in (runner.UNIT_PATH, "/opt/pv-runner/dist/a.tar.gz")))

    def test_nothing_installed_is_not_an_error(self):
        code, out, err = run_cli(["runner", "remove", "--yes"], self.gh, self.ex)
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", out)
        self.assertFalse(self.ex.changed())

    def test_removes_in_order_with_one_token_and_keeps_the_user(self):
        self.installed()
        self.token_routes("remove-token")
        code, out, err = run_cli(["runner", "remove", "--yes"], self.gh, self.ex)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.gh.calls()[0]["args"], ["api", "-X", "POST", "orgs/acme/actions/runners/remove-token"])
        self.assertEqual(len(self.gh.calls()), 1)
        removes = [e for e in self.ex.run_log if e["argv"][-1] == "remove"]
        self.assertEqual([e["cwd"] for e in removes], ["/opt/pv-runner/runner-1", "/opt/pv-runner/runner-2"])
        for entry in removes:
            self.assertEqual(entry["env"], {"ACTIONS_RUNNER_INPUT_TOKEN": TOKEN})
            self.assertEqual(entry["argv"][:4], ("runuser", "-u", "pv-runner", "--"))
            self.assertNotIn(TOKEN, " ".join(entry["argv"]))
        calls = self.ex.calls
        for entry, home in zip(removes, ("/opt/pv-runner/runner-1", "/opt/pv-runner/runner-2")):
            self.assertLess(calls.index(("run", entry["argv"])), calls.index(("rmtree", home)))  # deregistered before deleted
        self.assertEqual(self.ex.files, {})
        self.assertEqual(self.ex.dirs, {})
        self.assertIn("pv-runner", self.ex.users)
        self.assertTrue(all(c[1].startswith("/opt/pv-runner") or c[1] == runner.UNIT_PATH for c in calls if c[0] in ("rmtree", "rmfile", "rmdir")))
        self.assertNotIn(TOKEN, out + err)

    def test_a_failed_deregistration_keeps_the_directory(self):
        self.installed()
        self.token_routes("remove-token")
        self.ex.fail["config.sh"] = "Runner is busy"
        code, out, err = run_cli(["runner", "remove", "--yes"], self.gh, self.ex)
        self.assertEqual(code, 1)
        self.assertIn("/opt/pv-runner/runner-1/.runner", self.ex.files)
        self.assertNotIn(("rmtree", "/opt/pv-runner/runner-1"), self.ex.calls)

    def test_not_root_and_unknown_target_are_refused(self):
        self.installed()
        self.ex._euid = 1000
        code, out, err = run_cli(["runner", "remove", "--yes"], self.gh, self.ex)
        self.assertEqual(code, 1)
        self.assertIn("must run as root", err)
        self.assertFalse(self.ex.changed())
        self.ex._euid = 0
        del self.ex.files["/opt/pv-runner/runner-1/.runner"], self.ex.files["/opt/pv-runner/runner-2/.runner"]
        code, out, err = run_cli(["runner", "remove", "--yes"], self.gh, self.ex)
        self.assertEqual(code, 2)
        self.assertIn("--org", err)
        self.assertFalse(self.ex.changed())

    def test_an_explicit_target_wins(self):
        self.installed()
        code, out, err = run_cli(["runner", "remove", "--repo", "me/app"], self.gh, self.ex)
        self.assertIn("registered to me/app", out)


class RealExecutorTest(unittest.TestCase):
    """The real executor, on paths in a temp dir only. useradd, systemctl and /opt are never touched."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ex = runner.RealExecutor()

    def test_files_dirs_and_removal(self):
        self.ex.make_dir(str(self.tmp / "a" / "b"), 0o700, "root")
        self.assertEqual((self.tmp / "a" / "b").stat().st_mode & 0o777, 0o700)
        self.ex.write_file(str(self.tmp / "a" / "hook.sh"), "echo\n", 0o755)
        self.assertEqual((self.tmp / "a" / "hook.sh").stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.tmp / "a" / "hook.sh.part").exists())
        self.ex.remove_dir_if_empty(str(self.tmp / "a"))  # not empty: stays
        self.assertTrue((self.tmp / "a").exists())
        self.ex.remove_file(str(self.tmp / "a" / "hook.sh"))
        self.ex.remove_file(str(self.tmp / "a" / "hook.sh"))  # again: fine
        self.ex.remove_tree(str(self.tmp / "a" / "b"))
        self.ex.remove_dir_if_empty(str(self.tmp / "a"))
        self.assertFalse((self.tmp / "a").exists())

    def test_make_dir_refuses_a_symlink_and_a_file_and_leaves_the_victim_alone(self):
        """I-2: the job user can swap a directory it owns for a symlink; root must not chmod or chown through it."""
        victim = self.tmp / "victim"
        victim.mkdir()
        victim.chmod(0o755)
        before = victim.stat()
        (self.tmp / "link").symlink_to(victim)
        (self.tmp / "file").write_text("x")
        for name in ("link", "file"):
            with self.subTest(name), self.assertRaisesRegex(PivError, "not a plain directory"):
                self.ex.make_dir(str(self.tmp / name), 0o700, getpass.getuser())
        after = victim.stat()
        self.assertEqual((after.st_mode, after.st_uid, after.st_gid), (before.st_mode, before.st_uid, before.st_gid))
        self.ex.make_dir(str(self.tmp / "ok"), 0o700, getpass.getuser())  # a real directory still works, and twice
        self.ex.make_dir(str(self.tmp / "ok"), 0o750, getpass.getuser())
        self.assertEqual((self.tmp / "ok").stat().st_mode & 0o777, 0o750)

    def test_remove_tree_has_its_own_locks(self):
        for path in ("/", "/opt", "/opt/pv-runner", "relative/x/y/z"):
            with self.assertRaises(PivError):
                self.ex.remove_tree(path)
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "keep").write_text("x")
        (self.tmp / "link").symlink_to(victim)
        with self.assertRaises(PivError):
            self.ex.remove_tree(str(self.tmp / "link"))
        self.assertTrue((victim / "keep").exists())

    def test_run_gives_the_command_a_clean_environment_and_reports_failure(self):
        out = self.tmp / "out"
        os.environ["GH_TOKEN"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "GH_TOKEN", None)
        self.ex.run(("sh", "-c", 'printf "%s|%s" "$GH_TOKEN" "$ACTIONS_RUNNER_INPUT_TOKEN" > out'), cwd=str(self.tmp), env={"ACTIONS_RUNNER_INPUT_TOKEN": "t"})
        self.assertEqual(out.read_text(), "|t")
        with self.assertRaisesRegex(PivError, r"sh failed \(exit 3\): boom"):
            self.ex.run(("sh", "-c", "echo boom >&2; exit 3"))
        with self.assertRaisesRegex(PivError, "cannot run"):
            self.ex.run(("/nonexistent/tool",))

    def test_instances_reads_only_runner_n_names(self):
        for name in ("runner-1", "runner-10", "runner-x", "hooks", "dist", "runner-2.bak"):
            (self.tmp / name).mkdir()
        ex = runner.RealExecutor()
        ex.base = str(self.tmp)
        self.assertEqual(ex.instances(), [1, 10])
        ex.base = str(self.tmp / "missing")
        self.assertEqual(ex.instances(), [])


def render_hook(mode: str, dest: Path, *, procs_file: str = "", user: str | None = None) -> Path:
    """The installed hook, with the settings the fixture needs (this user, a fake process list)."""
    text = runner.hook_text(mode)
    for old, new in (('expected_user="pv-runner"', f'expected_user="{user or getpass.getuser()}"'), ('procs_file=""', f'procs_file="{procs_file}"')):
        assert old in text
        text = text.replace(old, new)
    dest.write_text(text, encoding="utf-8")
    return dest


class HookTest(unittest.TestCase):
    """The hook scripts, run for real in a temp fixture: a fake work dir and fake leftover processes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.work = self.tmp / "opt" / "runner-1" / "_work"
        self.work.mkdir(parents=True)
        self.procs = []
        self.addCleanup(self.reap)
        self.listing = self.tmp / "cgroup.procs"

    def reap(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def start(self, *argv: str) -> subprocess.Popen:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def hook(self, mode="completed", work=None, **kw) -> subprocess.CompletedProcess:
        script = render_hook(mode, self.tmp / f"job-{mode}.sh", procs_file=str(self.listing), **kw)
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "PV_RUNNER_WORK": str(self.work if work is None else work)}
        return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, check=False, timeout=60)

    def list_procs(self, *pids: int):
        self.listing.write_text("".join(f"{pid}\n" for pid in pids))

    def fill(self):
        (self.work / "repo" / "repo" / ".git").mkdir(parents=True)
        (self.work / "repo" / "repo" / "file.txt").write_text("x")
        (self.work / ".hidden").write_text("x")
        (self.work / "_temp").mkdir()
        (self.work / "_temp" / "runner-file").write_text("x")
        (self.work / "_actions").mkdir()
        locked = self.work / "cache" / "mod"
        locked.mkdir(parents=True)
        (locked / "ro.go").write_text("x")
        locked.chmod(0o500)  # a read-only module cache, as Go leaves
        (locked / "ro.go").chmod(0o400)

    def test_completed_wipes_the_workspaces_and_only_them(self):
        self.fill()
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "precious").write_text("x")
        (self.work / "link").symlink_to(outside)
        sibling = self.tmp / "opt" / "runner-1" / "config.sh"
        sibling.write_text("x")
        self.list_procs()
        done = self.hook("completed")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["_actions", "_temp"])  # the runner's own stay (S-1)
        self.assertTrue(self.work.is_dir())
        self.assertEqual((outside / "precious").read_text(), "x")  # a symlink is removed, not followed
        self.assertTrue(sibling.exists())
        self.assertTrue(self.home.is_dir())

    def test_a_repo_named_with_an_underscore_is_still_wiped(self):
        for mode in ("started", "completed"):
            (self.work / "_foo").mkdir()
            (self.work / "_foo" / "f").write_text("x")
            (self.work / "_tool").mkdir(exist_ok=True)
            self.list_procs()
            done = self.hook(mode)
            self.assertEqual((done.returncode, done.stderr), (0, ""))
            self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["_tool"])
            (self.work / "_tool").rmdir()

    def test_started_keeps_the_runners_own_directories(self):
        self.fill()
        self.list_procs()
        done = self.hook("started")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["_actions", "_temp"])
        self.assertTrue((self.work / "_temp" / "runner-file").exists())

    def test_a_process_left_behind_is_gone_before_the_next_job(self):
        leftover = self.start("sleep", "300")
        stubborn = self.start("bash", "-c", 'trap "" TERM; while :; do sleep 0.2; done')
        bystander = self.start("sleep", "300")  # same user, not in this instance's service
        listener = self.start("bash", "-c", 'exec -a "$0" sleep 300', str(self.tmp / "opt" / "runner-1" / "bin" / "Runner.Listener"))
        launcher = self.start("bash", "-c", 'exec -a "$0" sleep 300', str(self.tmp / "opt" / "runner-1" / "run.sh"))
        for _ in range(50):  # let the shells reach their trap and exec
            if all(Path(f"/proc/{p.pid}/cmdline").read_bytes() for p in (leftover, stubborn, bystander, listener, launcher)):
                break
            subprocess.run(["sleep", "0.05"], check=False)
        subprocess.run(["sleep", "0.3"], check=False)
        # the listing also names this test process, an ancestor of the hook: the hook must not kill it
        self.list_procs(leftover.pid, stubborn.pid, listener.pid, launcher.pid, os.getpid(), 1)
        (self.work / "repo").mkdir()
        done = self.hook("completed")
        self.assertEqual((done.returncode, done.stderr), (0, ""))  # and no "No such file" noise (S-7)
        self.assertEqual(leftover.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(stubborn.wait(timeout=5), -signal.SIGKILL)  # ignored TERM, so killed
        self.assertIsNone(bystander.poll())
        self.assertIsNone(listener.poll())  # the runner itself is never a victim
        self.assertIsNone(launcher.poll())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_a_respawning_supervisor_and_its_new_children_are_all_gone(self):
        """I-1: the kill set is rescanned, not a snapshot. The supervisor traps TERM and starts a new child."""
        tag = f"PV_FIXTURE_{os.getpid()}"
        supervisor = self.tmp / "supervisor.sh"
        supervisor.write_text('trap \'sleep 300 & echo $! >> "$KIDS"\' TERM\nsleep 300 &\necho $! >> "$KIDS"\nwhile :; do sleep 0.1; done\n')
        kids = self.tmp / "kids"
        kids.touch()
        proc = subprocess.Popen(["bash", str(supervisor)], env={"PATH": os.environ["PATH"], "KIDS": str(kids), tag: "1"},
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        stop = threading.Event()

        def publish():  # the fake cgroup.procs: every live process of this fixture, as the real one lists a service's
            while not stop.is_set():
                mine = []
                for entry in os.listdir("/proc"):
                    if entry.isdigit():
                        try:
                            if tag.encode() in Path(f"/proc/{entry}/environ").read_bytes():
                                mine.append(entry)
                        except OSError:
                            pass
                tmp = self.listing.with_suffix(".new")
                tmp.write_text("".join(f"{pid}\n" for pid in mine))
                os.replace(tmp, self.listing)
                time.sleep(0.02)

        thread = threading.Thread(target=publish)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(stop.set)
        self.addCleanup(self.kill_tagged, tag)
        for _ in range(100):
            if kids.read_text().strip():
                break
            time.sleep(0.05)
        time.sleep(0.3)
        done = self.hook("completed")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        proc.wait(timeout=5)
        time.sleep(0.2)
        self.assertEqual(self.tagged(tag), [], "a process of the respawner survived")
        self.assertGreaterEqual(len(kids.read_text().split()), 2)  # it did respawn

    def tagged(self, tag):
        found = []
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                try:
                    if tag.encode() in Path(f"/proc/{entry}/environ").read_bytes() and Path(f"/proc/{entry}/stat").read_text().rsplit(") ", 1)[1][0] != "Z":
                        found.append(int(entry))
                except OSError:
                    pass
        return found

    def kill_tagged(self, tag):  # the fixture cleans up after itself in every case
        for pid in self.tagged(tag):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def test_a_process_that_survives_every_round_fails_the_hook(self):
        stubborn = self.start("bash", "-c", 'trap "" TERM; while :; do sleep 0.1; done')
        time.sleep(0.2)
        self.list_procs(stubborn.pid)
        text = runner.hook_text("completed").replace("kill -KILL", "true")  # a stand-in for an unkillable process
        script = self.tmp / "unkillable.sh"
        script.write_text(text.replace('expected_user="pv-runner"', f'expected_user="{getpass.getuser()}"').replace('procs_file=""', f'procs_file="{self.listing}"'))
        (self.work / "keep").write_text("x")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "PV_RUNNER_WORK": str(self.work)}
        done = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, check=False, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertIn("survived", done.stderr)
        self.assertTrue((self.work / "keep").exists())  # nothing is wiped while a process lives

    def test_a_job_started_hook_kills_leftovers_too(self):
        leftover = self.start("sleep", "300")
        self.list_procs(leftover.pid)
        done = self.hook("started")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(leftover.wait(timeout=5), -signal.SIGTERM)

    def test_it_refuses_a_directory_that_is_not_a_runner_work_dir(self):
        self.fill()
        leftover = self.start("sleep", "300")
        self.list_procs(leftover.pid)
        elsewhere = self.tmp / "opt" / "runner-1" / "other"
        elsewhere.mkdir()
        (elsewhere / "file").write_text("x")
        link = self.tmp / "opt" / "runner-2"
        link.mkdir()
        (link / "_work").symlink_to(self.work)
        cases = {
            "empty": "",
            "root": "/",
            "home": str(self.home),
            "relative": "opt/runner-1/_work",
            "not _work": str(elsewhere),
            "no runner-N": str(self.tmp),
            "tmp": "/tmp",
            "symlink": str(link / "_work"),
            "missing": str(self.tmp / "opt" / "runner-9" / "_work"),
        }
        for label, path in cases.items():
            with self.subTest(label):
                done = self.hook("completed", work=path)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("pv-runner hook:", done.stderr)
                self.assertTrue((self.work / "repo" / "repo" / "file.txt").exists())
                self.assertTrue((elsewhere / "file").exists())
                self.assertIsNone(leftover.poll())  # nothing is killed when the directory is refused

    def test_it_refuses_when_the_work_directory_is_the_home_directory(self):
        self.fill()
        self.home = self.work
        done = self.hook("completed")
        self.assertEqual(done.returncode, 1)
        self.assertIn("home directory", done.stderr)
        self.assertTrue((self.work / "repo" / "repo" / "file.txt").exists())

    def test_it_runs_only_as_pv_runner(self):
        if getpass.getuser() == "pv-runner":
            self.skipTest("this is pv-runner")
        self.fill()
        done = self.hook("completed", user="pv-runner")
        self.assertEqual(done.returncode, 1)
        self.assertIn("runs only as pv-runner", done.stderr)
        self.assertTrue((self.work / "repo" / "repo" / "file.txt").exists())

    def test_without_a_service_cgroup_it_refuses_instead_of_guessing_what_to_kill(self):
        self.fill()
        text = runner.hook_text("completed").replace('expected_user="pv-runner"', f'expected_user="{getpass.getuser()}"')
        script = self.tmp / "job.sh"
        script.write_text(text)
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "PV_RUNNER_WORK": str(self.work)}
        done = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("not running in a pv-runner service", done.stderr)
        self.assertTrue((self.work / "repo" / "repo" / "file.txt").exists())

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_rendered_hooks_are_shellcheck_clean(self):
        for mode in ("started", "completed"):
            script = render_hook(mode, self.tmp / f"{mode}.sh", procs_file="/x/y")
            done = subprocess.run(["shellcheck", str(script)], capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stdout)

    def test_hook_text_needs_one_mode_line(self):
        self.assertIn('mode="started"', runner.hook_text("started"))
        self.assertNotIn('mode="completed"', runner.hook_text("started"))


if __name__ == "__main__":
    unittest.main()
