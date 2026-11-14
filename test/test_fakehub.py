"""test/fakehub.py, the stateful fake GitHub of the sandbox scenarios: strict about flags and permissions, and it keeps state."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import fakehub

REPO = "0xctx/piv-sandbox"


class FakeHubTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = fakehub.FakeHub(Path(tmp.name), REPO)
        self.hub.register_token("op", "operator", "*")
        self.hub.register_token("reader", "reader", {"issues": "read"})
        self.hub.register_token("writer", "writer", {"issues": "write"})
        self.hub.register_token("secreader", "secreader", {"secrets": "read", "variables": "read"})
        self.hub.add_issue(1, "t", "b")

    def gh(self, token: str | None, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"]} | ({"GH_TOKEN": token} if token else {})
        return subprocess.run([str(self.hub.path), *args], input=stdin, capture_output=True, text=True, env=env, check=False)

    def test_state_is_kept_between_calls(self):
        self.assertEqual(self.gh("writer", "api", "-X", "POST", f"repos/{REPO}/issues/1/comments", "-f", "body=hello").returncode, 0)
        done = self.gh("reader", "api", "--paginate", f"repos/{REPO}/issues/1/comments", "--jq", ".[] | {login: .user.login, body}")
        self.assertEqual(json.loads(done.stdout), {"login": "github-actions[bot]", "body": "hello"})
        self.assertEqual(self.hub.comments(1), ["hello"])

    def test_a_token_without_the_permission_is_refused_with_403(self):
        done = self.gh("reader", "api", "-X", "POST", f"repos/{REPO}/issues/1/comments", "-f", "body=x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("HTTP 403", done.stderr)
        self.assertEqual(self.hub.comments(1), [])
        self.assertEqual(self.gh("reader", "pr", "list", "--repo", REPO).returncode, 1, "reading pull requests needs the permission too")

    def test_no_token_and_an_unknown_token_are_refused(self):
        self.assertEqual(self.gh(None, "api", f"repos/{REPO}/issues/1").returncode, 4)
        done = self.gh("stolen", "api", f"repos/{REPO}/issues/1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("HTTP 401", done.stderr)

    def test_a_flag_the_real_command_lacks_is_refused_before_anything_happens(self):
        done = self.gh("op", "pr", "create", "--repo", REPO, "--draught", "--head", "x")
        self.assertEqual(done.returncode, 2)
        self.assertIn("has no flag --draught", done.stderr)
        self.assertEqual(self.gh("op", "pr", "list", "--json", "nonsense").returncode, 2)
        self.assertEqual(self.gh("op", "pr", "list", "--state", "everything").returncode, 2)
        self.assertEqual(self.gh("op", "frobnicate").returncode, 2)

    def test_an_endpoint_the_fake_does_not_know_exits_2_not_404(self):
        done = self.gh("op", "api", f"repos/{REPO}/rulesets")
        self.assertEqual(done.returncode, 2)
        self.assertIn("no route", done.stderr)

    def test_the_permission_lookup_can_fail_with_an_http_error_or_hang(self):
        self.hub.update(collaborators={"alice": "write"})
        ok = self.gh("op", "api", f"repos/{REPO}/collaborators/alice/permission")
        self.assertEqual(json.loads(ok.stdout)["permission"], "write")
        self.hub.update(lookup={"mode": "http", "status": 500, "message": "Server Error"})
        done = self.gh("op", "api", f"repos/{REPO}/collaborators/alice/permission")
        self.assertEqual(done.returncode, 1)
        self.assertIn("HTTP 500", done.stderr)
        self.hub.update(lookup={"mode": "hang", "seconds": 30})
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run([str(self.hub.path), "api", f"repos/{REPO}/collaborators/alice/permission"], capture_output=True,
                           env={"PATH": os.environ["PATH"], "GH_TOKEN": "op"}, timeout=1, check=False)

    def test_a_secrets_value_is_never_logged_and_the_repo_state_hides_the_vault(self):
        self.assertEqual(self.gh("op", "secret", "set", "KEY", "--repo", REPO, stdin="s3cret-value").returncode, 0)
        self.assertNotIn("s3cret-value", self.hub.log.read_text())
        self.assertEqual(self.hub.repo_state()["secrets"], ["KEY"])
        self.assertNotIn("s3cret-value", json.dumps(self.hub.repo_state()))
        self.assertEqual(self.hub.state()["vault"], {"KEY": "s3cret-value"})
        self.assertEqual(self.gh("op", "secret", "delete", "KEY", "--repo", REPO).returncode, 0)
        self.assertEqual(self.hub.state()["vault"], {})

    def test_every_call_is_logged_with_the_name_of_its_token_never_its_value(self):
        self.hub.register_token("ghs_the-token-value", "ack", {"issues": "read"})
        self.gh("ghs_the-token-value", "api", f"repos/{REPO}/issues/1")
        self.assertEqual(self.hub.calls()[-1]["token"], "ack")
        self.assertNotIn("ghs_the-token-value", self.hub.log.read_text())

    def test_every_gh_route_refuses_a_token_without_its_permission(self):
        """Round 1, F6: reads and writes of secrets, variables, labels, topics, issues, contents, refs and the Actions setting."""
        before = self.hub.state()
        c = f"repos/{REPO}"
        calls = [
            ("secret", "set", "KEY", "--repo", REPO), ("secret", "delete", "KEY", "--repo", REPO),
            ("variable", "set", "V", "--repo", REPO), ("variable", "delete", "V", "--repo", REPO),
            ("label", "create", "pivotal", "--repo", REPO, "--force"), ("label", "delete", "pivotal", "--repo", REPO, "--yes"),
            ("repo", "edit", REPO, "--add-topic", "pivotal"),
            ("issue", "create", "--repo", REPO, "--title", "t", "--body", "b"),
            ("issue", "edit", "1", "--repo", REPO, "--add-label", "pivotal"),
            ("api", f"{c}/actions/secrets"), ("api", f"{c}/actions/variables/V"),
            ("api", f"{c}/actions/permissions/workflow"),
            ("api", "-X", "PUT", f"{c}/actions/permissions/workflow", "--input", "-"),
            ("api", f"{c}/git/ref/heads/main"), ("api", f"{c}/contents/a.txt"),
            ("api", "-X", "PUT", f"{c}/contents/a.txt", "--input", "-"),
            ("api", "-X", "DELETE", f"{c}/contents/a.txt", "--input", "-"),
        ]
        for args in calls:
            with self.subTest(args=args):
                done = self.gh("reader", *args, stdin="{}")
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertIn("HTTP 403", done.stderr)
        self.assertEqual({k: v for k, v in self.hub.state().items() if k != "tokens"},
                         {k: v for k, v in before.items() if k != "tokens"}, "a refused call changes nothing")

    def test_a_read_only_token_cannot_write_secrets_or_variables_through_the_api(self):
        """Round 2, R4: the read permission is not enough for a DELETE, PUT or POST on the Actions secrets and variables."""
        self.hub.update(variables={"V": "1"}, secrets=["KEY"])
        c = f"repos/{REPO}"
        for kind, name in (("secrets", "KEY"), ("variables", "V")):
            self.assertEqual(self.gh("secreader", "api", f"{c}/actions/{kind}/{name}").returncode, 0)
            for method in ("DELETE", "PUT", "POST", "PATCH"):
                with self.subTest(kind=kind, method=method):
                    done = self.gh("secreader", "api", "-X", method, f"{c}/actions/{kind}/{name}", "--input", "-", stdin="{}")
                    self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                    self.assertIn("HTTP 403", done.stderr)
        self.assertEqual((self.hub.state()["variables"], self.hub.state()["secrets"]), ({"V": "1"}, ["KEY"]))
        # a token that may write meets an unimplemented route, never a silent success
        done = self.gh("op", "api", "-X", "DELETE", f"{c}/actions/variables/V")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(self.hub.state()["variables"], {"V": "1"})

    def test_concurrent_calls_lose_no_write_and_never_corrupt_the_state(self):
        """Round 1, F4: every gh call is a process and jobs run side by side; each write is kept."""
        count = 12
        procs = [subprocess.Popen([str(self.hub.path), "api", "-X", "POST", f"repos/{REPO}/issues/1/comments", "-f", f"body=c{n}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env={"PATH": os.environ["PATH"], "GH_TOKEN": "writer"}) for n in range(count)]
        procs += [subprocess.Popen([str(self.hub.path), "api", f"repos/{REPO}/issues/1"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env={"PATH": os.environ["PATH"], "GH_TOKEN": "reader"})
                  for _ in range(count)]
        results = [(p.communicate(timeout=120), p.returncode) for p in procs]
        self.assertEqual([code for _, code in results], [0] * (2 * count), [err for (_, err), _ in results if err])
        self.assertEqual(sorted(self.hub.comments(1)), sorted(f"c{n}" for n in range(count)))
        self.assertEqual(len(self.hub.calls()), 2 * count)

    def test_a_read_writes_nothing_and_a_refused_call_changes_nothing(self):
        before = (self.hub.state_path.stat().st_ino, self.hub.state_path.read_bytes())
        self.assertEqual(self.gh("reader", "api", f"repos/{REPO}/issues/1").returncode, 0)
        self.assertEqual(self.gh("reader", "api", "-X", "POST", f"repos/{REPO}/issues/1/comments", "-f", "body=x").returncode, 1)
        self.assertEqual((self.hub.state_path.stat().st_ino, self.hub.state_path.read_bytes()), before)

    def test_a_hanging_lookup_does_not_hold_the_state_for_other_calls(self):
        self.hub.update(collaborators={"alice": "write"}, lookup={"mode": "hang", "seconds": 20})
        hung = subprocess.Popen([str(self.hub.path), "api", f"repos/{REPO}/collaborators/alice/permission"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env={"PATH": os.environ["PATH"], "GH_TOKEN": "op"})
        self.addCleanup(lambda: (hung.kill(), hung.wait(timeout=30)))
        deadline = time.monotonic() + 30
        while "permission" not in (self.hub.log.read_text(encoding="utf-8") if self.hub.log.exists() else "") \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        done = subprocess.run([str(self.hub.path), "api", f"repos/{REPO}/issues/1"], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "GH_TOKEN": "reader"}, timeout=15, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)


class RunsTest(FakeHubTest):
    """The runs listing that the caller's stop job reads, and ``gh run cancel`` that it calls (F6)."""

    LISTING = f"repos/{REPO}/actions/workflows/pivotal.yml/runs"

    def setUp(self):
        super().setUp()
        self.hub.update(workflow_runs=[
            {"id": 1001, "display_title": "pivotal-issue-7", "status": "in_progress", "conclusion": ""},
            {"id": 1002, "display_title": "pivotal-issue-7", "status": "queued", "conclusion": ""},
            {"id": 1003, "display_title": "pivotal-issue-8", "status": "queued", "conclusion": ""},
        ])

    def listed(self, status: str) -> list[int]:
        done = self.gh("op", "api", f"{self.LISTING}?status={status}&per_page=100")
        self.assertEqual(done.returncode, 0, done.stderr)
        return [r["id"] for r in json.loads(done.stdout)["workflow_runs"]]

    def test_the_listing_returns_the_runs_of_one_status(self):
        self.assertEqual(self.listed("in_progress"), [1001])
        self.assertEqual(self.listed("queued"), [1002, 1003])
        self.assertEqual(self.listed("completed"), [])

    def test_the_listing_is_the_callers_workflow_file_and_another_file_is_not_found(self):
        """S1: the runs are the caller's (pivotal.yml); the runs of another workflow file are not the same runs, so a 404."""
        self.assertEqual(self.gh("op", "api", f"repos/{REPO}/actions/workflows/factory.yml/runs?status=queued").returncode, 1)
        self.assertEqual(self.gh("op", "api", f"repos/{REPO}/actions/workflows/nope.yml/runs?status=queued").returncode, 1)
        self.assertEqual(self.gh("op", "api", f"{self.LISTING}?status=queued").returncode, 0)

    def test_the_caller_file_name_is_the_one_piv_init_writes(self):
        from engine import seal
        self.assertEqual(fakehub.CALLER_WORKFLOW, Path(seal.WORKFLOW_PATH).name)

    def test_the_listing_needs_actions_read(self):
        self.assertEqual(self.gh("writer", "api", f"{self.LISTING}?status=queued").returncode, 1)

    def test_a_cancel_completes_the_run_as_cancelled_and_only_that_run(self):
        done = self.gh("op", "run", "cancel", "1001", "--repo", REPO)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.listed("in_progress"), [])
        self.assertEqual(self.listed("queued"), [1002, 1003])
        run = next(r for r in self.hub.state()["workflow_runs"] if r["id"] == 1001)
        self.assertEqual((run["status"], run["conclusion"]), ("completed", "cancelled"))

    def test_a_cancel_of_a_completed_or_unknown_run_is_refused_and_changes_nothing(self):
        self.gh("op", "run", "cancel", "1001", "--repo", REPO)
        before = self.hub.state()
        self.assertEqual(self.gh("op", "run", "cancel", "1001", "--repo", REPO).returncode, 1)
        self.assertEqual(self.gh("op", "run", "cancel", "9999", "--repo", REPO).returncode, 1)
        self.assertEqual(self.hub.state(), before)

    def test_a_cancel_reads_no_stdin_so_the_ids_after_it_survive(self):
        """I2: the stop job pipes run ids to ``gh run cancel`` in a loop. A cancel that read stdin would take the rest."""
        done = self.gh("op", "run", "cancel", "1002", "--repo", REPO, stdin="1003\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.listed("queued"), [1003])
        self.assertEqual(self.hub.calls()[-2]["stdin"], "", "the stub logs the stdin it read: none for a cancel")
        self.assertEqual(self.gh("op", "run", "cancel", "1003", "--repo", REPO).returncode, 0)

    def test_only_the_calls_that_take_stdin_read_it(self):
        takes = [["secret", "set", "X"], ["variable", "set", "X"], ["issue", "create", "--body-file", "-"],
                 ["pr", "create", "--head", "b"],
                 ["issue", "comment", "7", "--body-file", "-"], ["pr", "comment", "8", "--body-file", "-"], ["api", "--method", "POST", "x", "--input", "-"], ["api", "--input=-", "x"]]
        keeps = [["run", "cancel", "1"], ["secret", "set", "X", "--body", "v"], ["variable", "set", "X", "--body", "v"],
                 ["issue", "create", "--body-file", "f"], ["pr", "create", "--body-file", "f"], ["api", "repos/a/b"],
                 ["api", "x", "--input", "f.json"], ["pr", "list"], ["issue", "comment", "7", "--body", "b"],
                 ["pr", "comment", "8", "--body-file", "f"]]
        self.assertEqual([fakehub.takes_stdin(a) for a in takes], [True] * len(takes))
        self.assertEqual([fakehub.takes_stdin(a) for a in keeps], [False] * len(keeps))

    def test_a_cancel_needs_actions_write_and_the_flags_gh_has(self):
        self.assertEqual(self.gh("reader", "run", "cancel", "1002", "--repo", REPO).returncode, 1)
        self.assertEqual(self.listed("queued"), [1002, 1003])
        self.assertEqual(self.gh("op", "run", "cancel", "1002", "--bogus").returncode, 2)


class PullTest(FakeHubTest):
    """GET repos/{repo}/pulls/{n}: what trust and the land job read of a pull request (I3, I4)."""

    def setUp(self):
        super().setUp()
        self.hub.register_token("prreader", "prreader", {"pull-requests": "read"})
        self.hub.update(prs=[{
            "number": 3, "title": "stack", "body": "Closes #1\n", "baseRefName": "main", "headRefName": "pivotal/issue-1/stack",
            "headRefOid": "a" * 40, "isDraft": False, "state": "OPEN", "isCrossRepository": False,
            "url": f"https://github.com/{REPO}/pull/3", "author": {"login": "github-actions[bot]"}, "updatedAt": "2026-01-01T00:00:00Z",
        }])

    def test_a_pull_request_is_read_in_the_rest_shape(self):
        done = self.gh("prreader", "api", f"repos/{REPO}/pulls/3")
        self.assertEqual(done.returncode, 0, done.stderr)
        pr = json.loads(done.stdout)
        self.assertEqual((pr["number"], pr["state"], pr["draft"], pr["merged"]), (3, "open", False, False))
        self.assertEqual((pr["body"], pr["head"]["ref"], pr["head"]["sha"]), ("Closes #1\n", "pivotal/issue-1/stack", "a" * 40))
        self.assertEqual((pr["head"]["repo"]["full_name"], pr["base"]["ref"]), (REPO, "main"))

    def test_it_needs_pull_requests_read_and_an_unknown_number_is_404(self):
        self.assertEqual(self.gh("reader", "api", f"repos/{REPO}/pulls/3").returncode, 1, "issues read is not pull-requests read")
        self.assertEqual(self.gh("prreader", "api", f"repos/{REPO}/pulls/9").returncode, 1)


if __name__ == "__main__":
    unittest.main()
