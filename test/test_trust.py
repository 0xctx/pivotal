import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fakecli
from engine import trust as t

REPO = "you/pivotal-demo"


def labeled(author="stranger", label="pivotal", number=7):
    return {
        "action": "labeled",
        "issue": {"number": number, "user": {"login": author}},
        "label": {"name": label},
    }


def comment(body, commenter="maintainer", number=7, pull_request=False, author="stranger"):
    issue = {"number": number, "user": {"login": author}}
    if pull_request:
        issue["pull_request"] = {"url": "https://example.invalid/pulls/7"}
    return {"action": "created", "issue": issue, "comment": {"body": body, "user": {"login": commenter}}}


FAILED_SHA = "a" * 40


def workflow_run(run_changes=None, **changes):
    """A completed CI run as GitHub sends it: a failed push to the default branch of this repository, by alice. ``run_changes``
    replaces fields of the workflow_run object; ``changes`` replaces top-level fields of the event."""
    run = {
        "id": 555, "name": "CI", "event": "push", "head_branch": "main", "head_sha": FAILED_SHA, "conclusion": "failure",
        "actor": {"login": "alice"}, "head_repository": {"full_name": REPO},
    }
    run.update(run_changes or {})
    payload = {
        "action": "completed", "workflow_run": run,
        "repository": {"full_name": REPO, "default_branch": "main"}, "sender": {"login": "github-actions[bot]"},
    }
    payload.update(changes)
    return payload


def dispatch(sender="alice", sender_type="User", repository=REPO, client_payload=None, action="pivotal-event"):
    """A pivotal-event repository_dispatch as piv event sends it: the kind, a title and a link."""
    if client_payload is None:
        client_payload = {"kind": "incident", "title": "Checkout is down", "link": f"https://github.com/{REPO}/actions/runs/9"}
    return {
        "action": action, "sender": {"login": sender, "type": sender_type},
        "repository": {"full_name": repository}, "client_payload": client_payload,
    }


class FakeGhTest(unittest.TestCase):
    """Each test runs the real gh lookup against a fake gh on PATH (F2b)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bins = fakecli.FakeBin(self.root / "bin")
        self.env = self.bins.env({"PATH": os.environ.get("PATH", "")})

    def tearDown(self):
        self._tmp.cleanup()

    def gh_permission(self, level):
        self.bins.add("gh", stdout=json.dumps({"permission": level}))

    def gh_fails(self, code, stderr):
        self.bins.add("gh", stderr=stderr, code=code)

    def gh_by_login(self, levels):
        """A gh that answers each login in levels; any other login gets HTTP 404."""
        self.bins.add_routed(
            "gh", {f"/collaborators/{login}/permission": (json.dumps({"permission": level}), 0)
                   for login, level in levels.items()}
        )

    def gh_for_pull(self, levels, *, number, body):
        """As gh_by_login, and pull request ``number`` reads as a pull request with this body (its Closes line, I4)."""
        routes = {f"/collaborators/{login}/permission": (json.dumps({"permission": level}), 0) for login, level in levels.items()}
        routes[f"/pulls/{number}"] = (json.dumps({"number": number, "body": body}), 0)
        self.bins.add_routed("gh", routes)


class PermissionLookupTest(FakeGhTest):
    def test_write_maintain_and_admin_are_trusted(self):
        for level in ("write", "maintain", "admin"):
            with self.subTest(level=level):
                self.gh_permission(level)
                permission = t.lookup_permission(REPO, "alice", env=self.env)
                self.assertEqual(permission, level)
                self.assertTrue(t.is_trusted(permission))

    def test_read_triage_and_none_are_untrusted(self):
        for level in ("read", "triage", "none"):
            with self.subTest(level=level):
                self.gh_permission(level)
                self.assertFalse(t.is_trusted(t.lookup_permission(REPO, "alice", env=self.env)))

    def test_403_is_untrusted(self):
        self.gh_fails(1, "gh: Must have admin rights to Repository. (HTTP 403)\n")
        self.assertIsNone(t.lookup_permission(REPO, "alice", env=self.env))
        self.assertFalse(t.is_trusted(t.lookup_permission(REPO, "alice", env=self.env)))

    def test_500_is_untrusted(self):
        self.gh_fails(1, "gh: Internal Server Error (HTTP 500)\n")
        self.assertIsNone(t.lookup_permission(REPO, "alice", env=self.env))

    def test_timeout_is_untrusted(self):
        self.bins.add("gh", stdout=json.dumps({"permission": "admin"}), sleep=5)
        started = time.monotonic()
        permission = t.lookup_permission(REPO, "alice", env=self.env, timeout=0.3)
        self.assertLess(time.monotonic() - started, 4)
        self.assertIsNone(permission)
        self.assertFalse(t.is_trusted(permission))

    def test_odd_answers_are_untrusted(self):
        odd = {
            "empty output": ("", 0),
            "not JSON": ("not json", 0),
            "JSON list": ('["write"]', 0),
            "no permission key": ('{"role_name": "write"}', 0),
            "permission not a string": ('{"permission": 5}', 0),
            "permission null": ('{"permission": null}', 0),
            "capitalised level": ('{"permission": "Write"}', 0),
            "padded level": ('{"permission": " write"}', 0),
            "unknown level": ('{"permission": "owner"}', 0),
            "good body but failed exit": ('{"permission": "admin"}', 3),
        }
        for name, (stdout, code) in odd.items():
            with self.subTest(answer=name):
                self.bins.add("gh", stdout=stdout, code=code)
                permission = t.lookup_permission(REPO, "alice", env=self.env)
                self.assertFalse(t.is_trusted(permission))

    def test_missing_gh_is_untrusted(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIsNone(t.lookup_permission(REPO, "alice", env={"PATH": str(empty)}))

    def test_malformed_repo_or_login_never_reaches_gh(self):
        log = self.root / "calls.jsonl"
        self.bins.add("gh", stdout=json.dumps({"permission": "admin"}), log=log)
        bad = [
            (REPO, "alice/../bob"),
            (REPO, "-alice"),
            (REPO, "al ice"),
            (REPO, ""),
            ("you/..", "alice"),
            ("you", "alice"),
            ("a/b/c", "alice"),
        ]
        for repo, actor in bad:
            with self.subTest(repo=repo, actor=actor):
                self.assertIsNone(t.lookup_permission(repo, actor, env=self.env))
        self.assertFalse(log.exists())


class ClosedIssueTest(FakeGhTest):
    """The one issue a pull request closes, read from its body (I4): the rule land.py also applies."""

    def test_one_closing_line_names_the_issue_and_trailing_blanks_are_allowed(self):
        self.gh_for_pull({}, number=8, body="x\nCloses #7 \t\nmore")
        self.assertEqual(t.closed_issue(REPO, 8, env=self.env), 7)

    def test_no_line_two_lines_a_prefix_and_a_non_text_body_name_no_issue(self):
        for body in ("Closes #7 and more", " Closes #7 indented", "closes #7\n", "Closes #0\n", "Closes #7\nCloses #7\n"):
            with self.subTest(body=body):
                self.gh_for_pull({}, number=8, body=body)
                self.assertIsNone(t.closed_issue(REPO, 8, env=self.env))
        self.gh_for_pull({}, number=8, body="Closes #7\r\nmore\r\n")
        self.assertEqual(t.closed_issue(REPO, 8, env=self.env), 7, "a body with CRLF line ends names its issue (N2)")
        self.gh_for_pull({}, number=8, body="Closes #7\r\nCloses #9\r\n")
        self.assertIsNone(t.closed_issue(REPO, 8, env=self.env))
        self.gh_for_pull({}, number=8, body=None)
        self.assertIsNone(t.closed_issue(REPO, 8, env=self.env), "a null body is an empty body: no closing line")
        self.gh_for_pull({}, number=8, body=5)
        self.assertIsNone(t.closed_issue(REPO, 8, env=self.env))

    def test_a_number_that_is_not_a_pull_request_number_names_no_issue(self):
        for number in (True, 0, -8, "8", None):
            with self.subTest(number=number):
                self.assertIsNone(t.closed_issue(REPO, number, env=self.env))
        self.assertIsNone(t.closed_issue("not-a-repo", 8, env=self.env))


class ResolveTest(unittest.TestCase):
    def test_pivotal_label_on_an_issue_is_go_for_its_author(self):
        self.assertEqual(t.resolve("issues", labeled()), (7, "stranger", "go"))
        self.assertEqual(t.resolve("issues", labeled(label="pivotal:mirror")), (7, "stranger", "go"))

    def test_the_four_line_labels_are_triggers(self):
        for line in ("build", "incident", "mirror", "docs"):
            with self.subTest(line=line):
                self.assertEqual(t.resolve("issues", labeled(label=f"pivotal:{line}"))[2], "go")

    def test_status_labels_are_not_triggers(self):
        for label in ("pivotal:building", "pivotal:ready", "pivotal:landed", "pivotal:blocked"):
            with self.subTest(label=label):
                self.assertEqual(t.resolve("issues", labeled(label=label)), (None, None, "none"))

    def test_other_labels_and_actions_resolve_to_none(self):
        self.assertEqual(t.resolve("issues", labeled(label="bug"))[2], "none")
        self.assertEqual(t.resolve("issues", labeled(label="pivotal:"))[2], "none")
        self.assertEqual(t.resolve("issues", labeled(label="pivotal-ish"))[2], "none")
        self.assertEqual(t.resolve("issues", {**labeled(), "action": "opened"})[2], "none")

    def test_comment_commands_are_resolved_for_the_commenter(self):
        self.assertEqual(t.resolve("issue_comment", comment("/pivotal go")), (7, "maintainer", "go"))
        self.assertEqual(t.resolve("issue_comment", comment("/pivotal go trusted"))[2], "go")
        self.assertEqual(t.resolve("issue_comment", comment("/pivotal fix"))[2], "fix")
        self.assertEqual(t.resolve("issue_comment", comment("/pivotal land"))[2], "land")
        self.assertEqual(t.resolve("issue_comment", comment("/pivotal stop"))[2], "stop")

    def test_text_that_is_not_a_command_resolves_to_none(self):
        for body in ["please /pivotal go", "/pivotal gone", "/pivotal", "/pivotal deploy", ""]:
            with self.subTest(body=body):
                self.assertEqual(t.resolve("issue_comment", comment(body))[2], "none")

    def test_only_land_and_stop_are_commands_on_a_pull_request_and_other_events_resolve_to_none(self):
        # resolve returns an issue number, and a pull request's is not one: the issue it closes is decide's to find (N3)
        self.assertEqual(t.resolve("issue_comment", comment("/pivotal land", number=8, pull_request=True)), (None, None, "none"))
        self.assertEqual(t.resolve("issue_comment", comment("/pivotal stop", number=8, pull_request=True)), (None, None, "none"))
        for word in ("go", "go trusted", "fix"):
            self.assertEqual(t.resolve("issue_comment", comment(f"/pivotal {word}", pull_request=True))[2], "none", word)
        self.assertEqual(t.resolve("repository_dispatch", {"action": "pivotal-fix"})[2], "none")
        self.assertEqual(t.resolve("workflow_run", {})[2], "none")
        self.assertEqual(t.resolve("pull_request", labeled())[2], "none")

    def test_unresolvable_parts_become_none(self):
        no_number = {**labeled(), "issue": {"user": {"login": "stranger"}}}
        no_author = {**labeled(), "issue": {"number": 7}}
        bad_number = {**labeled(), "issue": {"number": True, "user": {"login": "stranger"}}}
        for payload in (no_number, no_author, bad_number, {}):
            with self.subTest(payload=payload):
                self.assertEqual(t.resolve("issues", payload), (None, None, "none"))


class KindsTest(unittest.TestCase):
    def test_kinds_are_the_contract_set(self):
        self.assertEqual(t.KINDS, {"go", "fix", "land", "stop", "event", "incident", "none"})

    def test_every_output_kind_is_a_known_kind(self):
        samples = [
            ("issues", labeled()),
            ("issues", labeled(label="pivotal:building")),
            ("issue_comment", comment("/pivotal land")),
            ("issue_comment", comment("just chatting")),
            ("workflow_run", {}),
        ]
        for event_name, payload in samples:
            with self.subTest(event=event_name, payload=payload):
                self.assertIn(t.resolve(event_name, payload)[2], t.KINDS)
        for kind in t.KINDS:
            self.assertEqual(t.Decision(False, None, kind, "x").kind, kind)

    def test_a_kind_outside_the_set_is_refused(self):
        with self.assertRaises(ValueError):
            t.Decision(False, None, "deploy", "x")


class DecideTest(FakeGhTest):
    def test_stranger_labelled_issue_runs_untrusted_go(self):
        self.gh_permission("read")
        decision = t.decide("issues", labeled(), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (False, 7, "go"))

    def test_writer_labelled_issue_by_a_writer_author_is_trusted(self):
        self.gh_permission("write")
        decision = t.decide("issues", labeled(author="alice"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, 7, "go"))

    def test_failed_lookup_on_a_label_still_routes_go_but_untrusted(self):
        self.gh_fails(1, "HTTP 500\n")
        decision = t.decide("issues", labeled(), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (False, 7, "go"))

    def test_writer_comment_re_runs_as_trusted(self):
        self.gh_permission("maintain")
        decision = t.decide("issue_comment", comment("/pivotal go trusted"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, 7, "go"))

    def test_comment_from_a_non_writer_is_ignored(self):
        self.gh_permission("read")
        decision = t.decide("issue_comment", comment("/pivotal go trusted"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (False, None, "none"))

    def test_comment_when_the_lookup_fails_is_ignored(self):
        self.gh_fails(1, "HTTP 403\n")
        decision = t.decide("issue_comment", comment("/pivotal go"), REPO, env=self.env)
        self.assertEqual(decision.kind, "none")
        self.assertFalse(decision.trusted)

    def test_no_lookup_happens_when_nothing_resolves(self):
        log = self.root / "calls.jsonl"
        self.bins.add("gh", stdout=json.dumps({"permission": "admin"}), log=log)
        decision = t.decide("issue_comment", comment("just chatting"), REPO, env=self.env)
        self.assertEqual(decision.kind, "none")
        self.assertFalse(log.exists())


class IncidentTriggerTest(FakeGhTest):
    """T5: a failed push to the default branch (workflow_run) and a pivotal-event dispatch resolve to an incident or an event
    with no issue yet (ack files it). Each refusal below is kind none, so no factory job runs."""

    def assertNoTrigger(self, decision):
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (False, None, "none"), decision.reason)

    def test_a_failed_default_branch_push_by_a_writer_is_a_trusted_incident(self):
        self.gh_permission("write")
        decision = t.decide("workflow_run", workflow_run(), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, None, "incident"), decision.reason)

    def test_the_pusher_is_the_actor_whose_permission_decides(self):
        self.gh_by_login({"alice": "write", "bob": "read"})
        trusted = t.decide("workflow_run", workflow_run(), REPO, env=self.env)
        stranger = t.decide("workflow_run", workflow_run({"actor": {"login": "bob"}}), REPO, env=self.env)
        self.assertEqual((trusted.trusted, trusted.kind), (True, "incident"))
        self.assertEqual((stranger.trusted, stranger.kind), (False, "incident"))
        self.assertIn("read access", stranger.reason)

    def test_a_failed_lookup_is_an_untrusted_incident_that_gets_no_secret(self):
        self.gh_fails(1, "HTTP 500\n")
        decision = t.decide("workflow_run", workflow_run(), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind), (False, "incident"))

    def test_a_pull_request_run_that_fails_is_no_trigger_even_from_a_branch_named_main(self):
        # A fork PR from a branch called main: its run is a pull_request run, its head repository is the fork.
        self.gh_permission("admin")
        fork = workflow_run({"event": "pull_request", "head_repository": {"full_name": "someone/fork"}})
        self.assertNoTrigger(t.decide("workflow_run", fork, REPO, env=self.env))
        self.assertNoTrigger(t.decide("workflow_run", workflow_run({"event": "pull_request"}), REPO, env=self.env))
        self.assertNoTrigger(t.decide("workflow_run", workflow_run({"head_repository": {"full_name": "someone/fork"}}), REPO, env=self.env))

    def test_a_run_that_is_not_a_failed_default_branch_push_of_this_repository_is_no_trigger(self):
        self.gh_permission("admin")
        cases = {
            "not the default branch": workflow_run({"head_branch": "feature"}),
            "a success": workflow_run({"conclusion": "success"}),
            "a cancellation": workflow_run({"conclusion": "cancelled"}),
            "no conclusion": workflow_run({"conclusion": None}),
            "another repository": workflow_run(repository={"full_name": "you/other", "default_branch": "main"}),
            "a requested run": workflow_run(action="requested"),
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                self.assertNoTrigger(t.decide("workflow_run", payload, REPO, env=self.env))

    def test_only_a_failure_conclusion_is_an_incident_and_every_other_conclusion_is_refused(self):
        """S2: the conclusion is exactly "failure". Each other value, a missing key and a capitalised word are refused, each
        named, so a mutant that admits one of them fails its own subtest. The control is a real failure, which is an incident."""
        self.gh_permission("admin")
        refused = {
            "timed_out": "timed_out", "startup_failure": "startup_failure", "skipped": "skipped", "null": None,
            "absent": "absent", "capital Failure": "Failure", "trailing space": "failure ", "cancelled": "cancelled",
            "success": "success", "neutral": "neutral", "action_required": "action_required",
        }
        for name, value in refused.items():
            with self.subTest(conclusion=name):
                run = workflow_run()
                if value == "absent":
                    del run["workflow_run"]["conclusion"]
                else:
                    run["workflow_run"]["conclusion"] = value
                self.assertNoTrigger(t.decide("workflow_run", run, REPO, env=self.env))
                self.assertEqual(t.resolve("workflow_run", run, REPO)[2], "none")
        control = t.decide("workflow_run", workflow_run(), REPO, env=self.env)
        self.assertEqual((control.trusted, control.kind), (True, "incident"), control.reason)

    def test_a_workflow_run_without_a_repository_name_from_the_environment_is_no_trigger(self):
        self.gh_permission("admin")
        self.assertNoTrigger(t.decide("workflow_run", workflow_run(), "", env=self.env))

    def test_a_workflow_run_with_no_actor_login_is_refused(self):
        self.gh_permission("admin")
        for actor in (None, {}, {"login": "dependabot[bot]"}, {"login": ""}):
            with self.subTest(actor=actor):
                decision = t.decide("workflow_run", workflow_run({"actor": actor}), REPO, env=self.env)
                self.assertFalse(decision.trusted)
                self.assertEqual(decision.kind, "none")

    def test_a_pivotal_event_from_a_writer_is_a_trusted_event_with_no_issue(self):
        self.gh_permission("write")
        decision = t.decide("repository_dispatch", dispatch(), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, None, "event"), decision.reason)

    def test_the_kind_piv_event_sends_is_informational_and_a_dispatch_is_always_the_event_kind(self):
        # I-C: `piv event --kind` is sent in client_payload, and trust never reads it: every dispatch is kind event. So an
        # `--kind incident` event gets event semantics (title "Event: ...", no already_green rule). Pinned, not changed.
        self.gh_permission("write")
        for kind in ("incident", "event", "fix", "land", "stop", "bogus"):
            payload = {"kind": kind, "title": "Checkout is down", "link": f"https://github.com/{REPO}/actions/runs/9"}
            with self.subTest(kind=kind):
                decision = t.decide("repository_dispatch", dispatch(client_payload=payload), REPO, env=self.env)
                self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, None, "event"), decision.reason)
                self.assertEqual(t.resolve("repository_dispatch", dispatch(client_payload=payload), REPO), (None, "alice", "event"))

    def test_a_pivotal_event_from_a_stranger_is_an_untrusted_event(self):
        self.gh_permission("read")
        decision = t.decide("repository_dispatch", dispatch(sender="stranger"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind), (False, "event"))

    def test_a_pivotal_event_sent_with_the_repositorys_own_token_is_untrusted_without_a_lookup(self):
        # Inverted on purpose (I3): nothing in the pipeline sends an event with the repo's own token, so that sender is no
        # proof of a writer. The exemption stays for a fix round only (test_fix). No lookup happens either way.
        log = self.root / "calls.jsonl"
        self.bins.add("gh", stdout=json.dumps({"permission": "admin"}), log=log)
        decision = t.decide("repository_dispatch", dispatch(sender="github-actions[bot]", sender_type="Bot"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind, decision.issue), (False, "event", None), decision.reason)
        self.assertIn("own token", decision.reason)
        self.assertFalse(log.exists())

    def test_a_pivotal_event_for_another_repository_or_without_a_payload_is_no_trigger(self):
        self.gh_permission("admin")
        self.assertNoTrigger(t.decide("repository_dispatch", dispatch(repository="you/other"), REPO, env=self.env))
        self.assertNoTrigger(t.decide("repository_dispatch", dispatch(client_payload="not an object"), REPO, env=self.env))
        no_payload = {k: v for k, v in dispatch().items() if k != "client_payload"}
        self.assertNoTrigger(t.decide("repository_dispatch", no_payload, REPO, env=self.env))
        self.assertNoTrigger(t.decide("repository_dispatch", dispatch(), "", env=self.env))

    def test_a_pivotal_event_sender_that_is_not_a_login_is_no_trigger(self):
        self.gh_permission("admin")
        self.assertNoTrigger(t.decide("repository_dispatch", dispatch(sender="-bad"), REPO, env=self.env))

    def test_resolve_names_the_incident_and_the_event_with_no_issue(self):
        self.assertEqual(t.resolve("workflow_run", workflow_run(), REPO), (None, "alice", "incident"))
        self.assertEqual(t.resolve("repository_dispatch", dispatch(), REPO), (None, "alice", "event"))
        self.assertEqual(t.resolve("repository_dispatch", dispatch(sender="github-actions[bot]", sender_type="Bot"), REPO),
                         (None, "github-actions[bot]", "event"))

    def test_resolve_refuses_what_decide_refuses(self):
        self.assertEqual(t.resolve("workflow_run", workflow_run({"conclusion": "success"}), REPO), (None, None, "none"))
        self.assertEqual(t.resolve("workflow_run", workflow_run(), ""), (None, None, "none"))
        self.assertEqual(t.resolve("repository_dispatch", dispatch(repository="you/other"), REPO), (None, None, "none"))


class CommentEscalationTest(FakeGhTest):
    """On an issue whose author is not trusted, a comment runs trusted only with /pivotal go trusted."""

    def test_plain_writer_command_on_a_stranger_issue_is_not_trusted(self):
        self.gh_by_login({"maintainer": "maintain", "stranger": "read"})
        for command, kind in (("/pivotal go", "go"), ("/pivotal fix", "fix")):
            with self.subTest(command=command):
                decision = t.decide("issue_comment", comment(command), REPO, env=self.env)
                self.assertEqual((decision.trusted, decision.issue, decision.kind), (False, 7, kind))

    def test_go_trusted_by_a_writer_re_runs_a_stranger_issue_as_trusted(self):
        self.gh_by_login({"maintainer": "write", "stranger": "read"})
        decision = t.decide("issue_comment", comment("/pivotal go trusted"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, 7, "go"))

    def test_go_trusted_must_be_the_whole_first_line(self):
        self.gh_by_login({"maintainer": "write", "stranger": "read"})
        bodies = [
            "/pivotal go trusted please",
            "/pivotal go trusted-ish",
            "/pivotal go\ntrusted",
            "Note first\n/pivotal go trusted",
        ]
        for body in bodies:
            with self.subTest(body=body):
                decision = t.decide("issue_comment", comment(body), REPO, env=self.env)
                self.assertFalse(decision.trusted)

    def test_a_trusted_author_lets_a_writer_command_run_trusted(self):
        self.gh_by_login({"maintainer": "maintain", "alice": "write"})
        decision = t.decide("issue_comment", comment("/pivotal go", author="alice"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, 7, "go"))

    def test_a_failed_author_lookup_is_not_trusted(self):
        self.gh_by_login({"maintainer": "maintain"})  # the stranger's lookup gets HTTP 404
        comment_on_stranger = comment("/pivotal go", author="stranger")
        decision = t.decide("issue_comment", comment_on_stranger, REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (False, 7, "go"))

    def test_an_invalid_author_login_is_not_trusted_and_not_logged(self):
        self.gh_by_login({"maintainer": "maintain"})
        odd = comment("/pivotal go", author="odd name!")
        decision = t.decide("issue_comment", odd, REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (False, 7, "go"))
        self.assertNotIn("odd name!", decision.reason)

    def test_a_writer_stop_on_a_stranger_issue_is_trusted_by_the_commenter_alone(self):
        # The stranger has no route, so a lookup of the author would fail: stop never asks for it (R4).
        self.gh_by_login({"maintainer": "write"})
        decision = t.decide("issue_comment", comment("/pivotal stop"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, 7, "stop"))

    def test_a_writer_land_on_a_pull_request_is_trusted_by_the_commenter_alone(self):
        # The pull request's author is the bot that opened it: a lookup of it would fail, so land never asks for it.
        # The pull request is read for the issue it closes (I4), so it is routed too: the decision is the issue (7).
        self.gh_for_pull({"maintainer": "write"}, number=7, body="Closes #7\n")
        decision = t.decide("issue_comment", comment("/pivotal land", pull_request=True), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, 7, "land"))

    def test_a_land_on_a_pull_request_decides_the_issue_it_closes_not_the_pull_request(self):
        """I4: the pull request is #8 and closes #7. The decision (and so the land job's issue and group) is #7."""
        self.gh_for_pull({"maintainer": "write"}, number=8, body="The stack.\n\nCloses #7\n")
        decision = t.decide("issue_comment", comment("/pivotal land", number=8, pull_request=True), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, 7, "land"))

    def test_a_land_on_a_pull_request_that_closes_no_issue_or_two_is_refused(self):
        """Fail closed: no decision for the land job when the pull request does not name exactly one issue (I4)."""
        for body in ("No closing line here.\n", "Closes #7\nCloses #9\n", "Closes #7 and more\n", "Fixes #7\n", ""):
            with self.subTest(body=body):
                self.gh_for_pull({"maintainer": "write"}, number=8, body=body)
                decision = t.decide("issue_comment", comment("/pivotal land", number=8, pull_request=True), REPO, env=self.env)
                self.assertEqual((decision.trusted, decision.issue, decision.kind), (False, None, "none"))
                self.assertIn("exactly one issue", decision.reason)

    def test_a_land_on_a_pull_request_that_cannot_be_read_is_refused(self):
        self.gh_by_login({"maintainer": "write"})  # no route for the pull request: gh fails, as it does on a 404
        decision = t.decide("issue_comment", comment("/pivotal land", number=8, pull_request=True), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind), (False, "none"))

    def test_a_land_on_a_pull_request_from_a_non_writer_is_ignored_and_other_commands_on_it_are_not_commands(self):
        self.gh_by_login({"maintainer": "read"})
        decision = t.decide("issue_comment", comment("/pivotal land", pull_request=True), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind), (False, "none"))
        self.gh_by_login({"maintainer": "write"})
        for word in ("go", "go trusted", "fix"):
            decision = t.decide("issue_comment", comment(f"/pivotal {word}", pull_request=True), REPO, env=self.env)
            self.assertEqual((decision.trusted, decision.kind), (False, "none"), word)

    def test_a_writer_stop_on_a_pull_request_decides_the_issue_it_closes(self):
        """R1: "/pivotal stop" works on a pull request as a land does: the decision is the issue the pull request closes."""
        self.gh_for_pull({"maintainer": "write"}, number=8, body="The stack.\n\nCloses #7\n")
        decision = t.decide("issue_comment", comment("/pivotal stop", number=8, pull_request=True), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.issue, decision.kind), (True, 7, "stop"))

    def test_a_stop_on_a_pull_request_from_a_non_writer_is_ignored(self):
        self.gh_for_pull({"maintainer": "read"}, number=8, body="Closes #7\n")
        decision = t.decide("issue_comment", comment("/pivotal stop", number=8, pull_request=True), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind), (False, "none"))

    def test_a_stop_on_a_pull_request_that_closes_no_issue_or_two_or_cannot_be_read_is_refused(self):
        for body in ("No closing line.\n", "Closes #7\nCloses #9\n", ""):
            with self.subTest(body=body):
                self.gh_for_pull({"maintainer": "write"}, number=8, body=body)
                decision = t.decide("issue_comment", comment("/pivotal stop", number=8, pull_request=True), REPO, env=self.env)
                self.assertEqual((decision.trusted, decision.issue, decision.kind), (False, None, "none"))
                self.assertIn("exactly one issue", decision.reason)
        self.gh_by_login({"maintainer": "write"})  # no route for the pull request
        decision = t.decide("issue_comment", comment("/pivotal stop", number=8, pull_request=True), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind), (False, "none"))

    def test_a_land_on_an_issue_of_a_stranger_is_refused_and_points_to_the_pull_request_not_to_go_trusted(self):
        """/pivotal go trusted can never be the first line of a land comment, so the refusal must not offer it (F8)."""
        self.gh_by_login({"maintainer": "write", "stranger": "read"})
        decision = t.decide("issue_comment", comment("/pivotal land"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind), (False, "land"))
        self.assertNotIn("go trusted", decision.reason)
        self.assertIn("pull request", decision.reason)
        decision = t.decide("issue_comment", comment("/pivotal go"), REPO, env=self.env)
        self.assertIn("go trusted", decision.reason, "the other commands still say how a writer re-runs the issue")

    def test_a_stop_from_a_non_writer_is_ignored(self):
        self.gh_by_login({"maintainer": "read"})
        decision = t.decide("issue_comment", comment("/pivotal stop"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind), (False, "none"))

    def test_a_writer_comment_is_ignored_when_it_is_not_a_command(self):
        self.gh_by_login({"maintainer": "write", "stranger": "read"})
        decision = t.decide("issue_comment", comment("looks fine to me"), REPO, env=self.env)
        self.assertEqual((decision.trusted, decision.kind), (False, "none"))


def pin_of(title, body):
    return hashlib.sha256(json.dumps([title, body]).encode("ascii")).hexdigest()


class IssuePinTest(FakeGhTest):
    """The pin is sha256 of the JSON array [title, body] of the issue. An empty pin means refuse (R3)."""

    def run_main(self, event_name, payload):
        event_file = self.root / "event.json"
        event_file.write_text(json.dumps(payload), encoding="utf-8")
        env = {**self.env, "GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": str(event_file)}
        env["GITHUB_REPOSITORY"] = REPO
        out, err = io.StringIO(), io.StringIO()
        t.main(env, out, err)
        return dict(line.split("=", 1) for line in out.getvalue().splitlines())

    @staticmethod
    def issue(title="Add a feature", body="Please add it.", author="alice"):
        payload = labeled(author=author)
        payload["issue"].update(title=title, body=body)
        return payload

    def test_pin_is_the_sha256_of_the_issue_title_and_body(self):
        self.gh_permission("admin")
        outputs = self.run_main("issues", self.issue())
        self.assertEqual(outputs["pin"], pin_of("Add a feature", "Please add it."))

    def test_the_pin_moves_with_the_body(self):
        self.gh_permission("admin")
        before = self.run_main("issues", self.issue(body="first"))["pin"]
        after = self.run_main("issues", self.issue(body="second"))["pin"]
        self.assertNotEqual(before, after)

    def test_a_null_body_is_an_empty_body_not_a_refusal(self):
        self.gh_permission("admin")
        outputs = self.run_main("issues", self.issue(body=None))
        self.assertEqual(outputs["pin"], pin_of("Add a feature", ""))

    def test_a_missing_title_gives_an_empty_pin_which_means_refuse(self):
        self.gh_permission("admin")
        outputs = self.run_main("issues", self.issue(title=None))
        self.assertEqual(outputs["pin"], "")
        self.assertEqual(outputs["trusted"], "true")  # trust still decides; the factory refuses on the pin

    def test_a_comment_pins_the_issue_not_the_comment(self):
        self.gh_by_login({"maintainer": "write", "alice": "write"})
        payload = comment("/pivotal go", commenter="maintainer", author="alice")
        payload["issue"].update(title="Add a feature", body="Please add it.")
        outputs = self.run_main("issue_comment", payload)
        self.assertEqual(outputs["pin"], pin_of("Add a feature", "Please add it."))

    def test_an_untrusted_go_is_pinned_too(self):
        self.gh_permission("read")
        outputs = self.run_main("issues", self.issue())
        self.assertEqual((outputs["trusted"], outputs["kind"]), ("false", "go"))
        self.assertEqual(outputs["pin"], pin_of("Add a feature", "Please add it."))

    def test_no_pin_is_printed_when_nothing_resolves(self):
        self.gh_permission("admin")
        outputs = self.run_main("workflow_run", {"action": "completed"})
        self.assertEqual(outputs["pin"], "")


class MainTest(FakeGhTest):
    def run_main(self, event_name, payload, *, path=None, repo=REPO):
        event_file = self.root / "event.json"
        if path is None:
            event_file.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
            path = str(event_file)
        env = {**self.env, "GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": path}
        if repo is not None:
            env["GITHUB_REPOSITORY"] = repo
        out, err = io.StringIO(), io.StringIO()
        code = t.main(env, out, err)
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_faked_500_prints_untrusted(self):
        # The user-visible check: the trust tool with a faked 500 says untrusted.
        self.gh_fails(1, "gh: Internal Server Error (HTTP 500)\n")
        code, lines, err = self.run_main("issues", labeled())
        self.assertEqual(code, 0)
        self.assertIn("untrusted", err)
        self.assertEqual(lines, ["trusted=false", "issue=7", "kind=go", "pin="])

    def test_writer_prints_trusted_outputs(self):
        self.gh_permission("admin")
        code, lines, err = self.run_main("issues", labeled(author="alice"))
        self.assertEqual(code, 0)
        self.assertTrue(err.startswith("trusted:"))
        self.assertEqual(lines, ["trusted=true", "issue=7", "kind=go", "pin="])

    def test_the_log_names_only_known_levels(self):
        self.bins.add("gh", stdout=json.dumps({"permission": "admin\nforged: trusted"}))
        code, lines, err = self.run_main("issues", labeled(author="alice"))
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "trusted=false")
        self.assertIn("unknown", err)
        self.assertNotIn("forged", err)

    def test_a_login_that_fails_validation_says_invalid_login(self):
        self.gh_permission("admin")
        for author in ("dependabot[bot]", "bot_user", "-leading"):
            with self.subTest(author=author):
                code, lines, err = self.run_main("issues", labeled(author=author))
                self.assertEqual(lines, ["trusted=false", "issue=", "kind=none", "pin="])
                self.assertIn("invalid login", err)

    def test_an_unexpected_error_still_prints_the_fail_closed_outputs(self):
        with mock.patch.object(t, "decide", side_effect=RuntimeError("boom")):
            code, lines, err = self.run_main("issues", labeled())
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["trusted=false", "issue=", "kind=none", "pin="])
        self.assertEqual(err.strip(), "untrusted: internal error (RuntimeError)")

    def test_unresolvable_input_is_untrusted_with_no_kind(self):
        self.gh_permission("admin")
        cases = [
            ("issues", None, {"path": str(self.root / "missing.json")}),
            ("issues", "{not json", {}),
            ("issues", "[1, 2]", {}),
            ("issues", labeled(), {"repo": None}),
            ("workflow_run", {"action": "completed"}, {}),
        ]
        for event_name, payload, kwargs in cases:
            with self.subTest(event=event_name, kwargs=kwargs):
                code, lines, err = self.run_main(event_name, payload, **kwargs)
                self.assertEqual(code, 0)
                self.assertEqual(lines[0], "trusted=false")
                self.assertEqual(lines[2], "kind=none")
                self.assertTrue(err.startswith("untrusted:"))

    def test_an_incident_prints_no_issue_and_no_pin_because_ack_files_the_issue(self):
        self.gh_permission("admin")
        code, lines, err = self.run_main("workflow_run", workflow_run())
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["trusted=true", "issue=", "kind=incident", "pin="])

    def test_a_pivotal_event_prints_no_issue_and_no_pin(self):
        self.gh_permission("write")
        code, lines, err = self.run_main("repository_dispatch", dispatch())
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["trusted=true", "issue=", "kind=event", "pin="])
        self.assertTrue(err.startswith("trusted:"), err)

    def test_a_fork_pr_run_prints_no_trigger(self):
        self.gh_permission("admin")
        fork = workflow_run({"event": "pull_request", "head_repository": {"full_name": "someone/fork"}})
        code, lines, err = self.run_main("workflow_run", fork)
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["trusted=false", "issue=", "kind=none", "pin="])
        self.assertTrue(err.startswith("untrusted:"), err)


if __name__ == "__main__":
    unittest.main()
