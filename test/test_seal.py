"""The seal (F4): the check-run body publish sends and the verifier the land job runs, against forgeries.

The data the verifier reads from GitHub is built by hand here in the shapes of the REST API (check runs of a commit, a
workflow run, the artifacts of a run); nothing has run on real GitHub, so those shapes are unverified.
"""

import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

from test_assemble import ISSUE
from test_stack import REPO, RUN, SUITE, StackCase

from engine import assemble, seal

OTHER = 999


class SealFixture(StackCase):
    def fixture(self, *ids: str, bad: tuple[str, ...] = ()) -> None:
        self.out = self.stacked(*ids, bad=bad)
        self.land = assemble.read_land(self.out / assemble.LAND)
        self.h = self.heads(self.out)
        self.land_sha = hashlib.sha256((self.out / assemble.LAND).read_bytes()).hexdigest()
        self.sha = self.h[ids[-1]]
        self.ids = list(ids)

    def runs(self, sha: str | None = None, run: int = RUN, summary: str | None = None, **over) -> list[dict]:
        sha = sha or self.sha
        text = summary if summary is not None else seal.summary(self.land_sha, self.land["positions"][: self.ids.index(self.at(sha)) + 1])
        return [self.check_run(sha, run, output={"title": seal.TITLE, "summary": text}, **over)]

    def at(self, sha: str) -> str:
        return next(i for i, h in self.h.items() if h == sha)

    def run_object(self, **over) -> dict:
        return {"id": RUN, "path": ".github/workflows/pivotal.yml", "check_suite_id": SUITE, "repository": {"full_name": REPO},
                "head_branch": "main", "event": "issue_comment", **over}

    def artifacts(self, *names: str, run: int = RUN, **over) -> list[dict]:
        wanted = names or ("land", *(f"gate-{i}" for i in self.land["positions"]))
        return [{"name": n, "expired": False, "workflow_run": {"id": run}, **over} for n in wanted]

    def verify(self, **over) -> list[str]:
        args = dict(repo=REPO, default="main", sha=self.sha, external_id=str(RUN), check_runs=self.runs(), run=self.run_object(),
                    artifacts=self.artifacts(), land_dir=self.out, gates_dir=self.gates)
        args.update(over)
        return seal.verify(**args)

    def refuses(self, word: str, **over) -> None:
        with self.assertRaises(seal.SealError, msg=word) as caught:
            self.verify(**over)
        self.assertEqual(str(caught.exception), word)


class PayloadTest(SealFixture):
    def test_the_payload_and_its_text(self):
        body = seal.payload("a" * 40, 12, "b" * 64, ["one", "two"])
        self.assertEqual(body["name"], "pivotal/gate")
        self.assertEqual((body["status"], body["conclusion"], body["external_id"]), ("completed", "success", "12"))
        self.assertEqual(body["output"]["summary"], "The first 2 slice(s) of the stack passed the gate: one, two.\nland_sha256: " + "b" * 64 + "\n")
        self.assertTrue(seal.seal_text(body).decode("ascii").endswith("\n"))
        for args in (("a" * 39, 1, "b" * 64, ["x"]), ("a" * 40, 1, "b" * 63, ["x"]), ("a" * 40, 1, "b" * 64, [])):
            with self.assertRaises(seal.SealError):
                seal.payload(*args)

    def test_the_response_must_be_the_seal_that_was_asked_for(self):
        good = self.check_run("a" * 40, 5)
        self.assertEqual(seal.check_response(good, "a" * 40, 5), 77)
        for change in ({"head_sha": "b" * 40}, {"external_id": "6"}, {"name": "x"}, {"conclusion": "failure"}, {"status": "queued"},
                       {"app": {"slug": "evil"}}, {"app": None}, {"id": True}, {"id": 0}, {"id": "7"}):
            with self.assertRaises(seal.SealError, msg=change):
                seal.check_response({**good, **change}, "a" * 40, 5)
        for junk in (None, [], "x", 3):
            with self.assertRaises(seal.SealError):
                seal.check_response(junk, "a" * 40, 5)


class VerifierTest(SealFixture):
    def test_a_genuine_seal_confirms_the_prefix_up_to_m(self):
        self.fixture("one", "two")
        self.assertEqual(self.verify(), ["one", "two"])

    def test_a_seal_on_an_earlier_slice_confirms_only_the_prefix_up_to_it(self):
        self.fixture("one", "two")
        sha = self.h["one"]
        self.assertEqual(self.verify(sha=sha, check_runs=self.runs(sha=self.h["one"]), artifacts=self.artifacts("land", "gate-one")), ["one"])

    def test_forged_check_runs_are_ignored_beside_the_genuine_one(self):
        self.fixture("one")
        forged = [
            *self.runs(app={"slug": "someone-else"}), *self.runs(run=OTHER), *self.runs(name="pivotal/other"),
            *self.runs(conclusion="failure"), *self.runs(status="in_progress"), *self.runs(external_id="1; rm"),
            *self.runs(external_id=None), "junk", None, {"name": "pivotal/gate"},
        ]
        genuine = self.runs()
        self.assertEqual(seal.candidates(forged + genuine, self.sha), [str(OTHER), str(RUN)])
        self.assertEqual(seal.candidates(forged, self.sha), [str(OTHER)], "another run's id is a candidate; its run is checked next")
        self.assertEqual(self.verify(check_runs=forged + genuine), ["one"])

    def test_a_check_run_from_another_app_alone_is_no_seal(self):
        self.fixture("one")
        self.refuses("no_seal", check_runs=self.runs(app={"slug": "someone-else"}))
        self.refuses("no_seal", check_runs=self.runs(app={}))
        self.refuses("no_seal", check_runs=self.runs(app="github-actions"))

    def test_a_seal_for_another_sha_is_no_seal(self):
        self.fixture("one", "two")
        self.refuses("no_seal", check_runs=self.runs(sha=self.h["one"]))
        self.refuses("no_seal", check_runs=self.runs(head_sha="f" * 40))
        self.refuses("sha_not_on_stack", sha="f" * 40, check_runs=self.runs(sha="f" * 40, summary=seal.summary(self.land_sha, ["one"])))

    def test_a_seal_made_by_another_run_than_the_one_asked_about_is_no_seal(self):
        self.fixture("one")
        self.refuses("no_seal", external_id=str(OTHER))
        self.refuses("no_seal", external_id="")
        self.refuses("no_seal", external_id="0")

    def test_n5_a_check_run_of_another_suite_than_the_named_runs_is_no_seal(self):
        """Any workflow with `checks: write` can create `pivotal/gate` on m with a pivotal run's id and land hash as its
        external_id and summary. What it cannot copy is the check suite it ran in. (Real REST fields; unverified on GitHub.)"""
        self.fixture("one")
        for suite in (SUITE + 1, None, "9001", True, 0, -1, 9001.0, [SUITE]):
            self.refuses("suite_differs", check_runs=self.runs(check_suite={"id": suite}))
        for junk in (None, {}, [], "x", SUITE):
            self.refuses("suite_differs", check_runs=self.runs(check_suite=junk))
        for suite in (SUITE + 1, None, "9001", True, 9001.0, 0):
            self.refuses("suite_differs", run=self.run_object(check_suite_id=suite))
        gone = self.runs()
        del gone[0]["check_suite"]
        self.refuses("suite_differs", check_runs=gone)
        gone_run = self.run_object()
        del gone_run["check_suite_id"]
        self.refuses("suite_differs", run=gone_run)

    def test_n5_a_forged_seal_of_another_suite_listed_first_does_not_shadow_the_genuine_one(self):
        self.fixture("one")
        forged = self.runs(check_suite={"id": SUITE + 1})
        self.assertEqual(self.verify(check_runs=forged + self.runs()), ["one"])
        self.refuses("suite_differs", check_runs=forged)

    def test_a_run_of_another_workflow_is_not_pivotal(self):
        self.fixture("one")
        for path in (".github/workflows/evil.yml", ".github/workflows/pivotal.yml@refs/heads/x", "pivotal.yml", "", None, 3):
            self.refuses("run_not_pivotal", run=self.run_object(path=path))

    def test_a_run_that_is_not_the_one_the_seal_names_or_of_another_repo_is_not_pivotal(self):
        self.fixture("one")
        self.refuses("run_not_pivotal", run=self.run_object(id=OTHER))
        self.refuses("run_not_pivotal", run=self.run_object(id=True))
        self.refuses("run_not_pivotal", run=self.run_object(repository={"full_name": "evil/repo"}))
        self.refuses("run_not_pivotal", run=self.run_object(repository=None))
        self.refuses("run_not_pivotal", run=[])

    def test_r1_a_run_on_a_branch_other_than_the_default_or_of_an_event_the_caller_lacks_is_no_seal(self):
        self.fixture("one")
        self.assertEqual(self.verify(), ["one"])
        self.refuses("run_branch", run=self.run_object(head_branch="feature-forged", event="workflow_dispatch"))
        for branch in ("feature", "Main", "", None, 7, ["main"]):
            self.refuses("run_branch", run=self.run_object(head_branch=branch))
        self.refuses("run_branch", default="trunk")
        self.refuses("run_branch", default="")
        for event in ("push", "pull_request", "schedule", "", None):
            self.refuses("run_event", run=self.run_object(event=event))
        for event in seal.RUN_EVENTS:
            self.assertEqual(self.verify(run=self.run_object(event=event)), ["one"])

    def test_an_artifact_of_another_run_is_no_confirmation(self):
        self.fixture("one", "two")
        self.refuses("artifact_other_run", artifacts=self.artifacts("land", "gate-one", run=OTHER) + self.artifacts("gate-two"))
        mixed = self.artifacts("land", "gate-two") + self.artifacts("gate-one", run=OTHER)
        self.refuses("artifact_other_run", artifacts=mixed)
        self.refuses("artifact_expired", artifacts=self.artifacts(expired=True))
        self.refuses("artifact_other_run", artifacts=self.artifacts(expired="true"))
        self.refuses("artifact_other_run", artifacts=[{"name": "land", "expired": False}, *self.artifacts("gate-one", "gate-two")])

    def test_a_missing_or_repeated_artifact_is_no_confirmation(self):
        self.fixture("one", "two")
        self.refuses("artifact_missing", artifacts=self.artifacts("land", "gate-one"))
        self.refuses("artifact_missing", artifacts=self.artifacts("gate-one", "gate-two"))
        self.refuses("artifact_missing", artifacts=self.artifacts() + self.artifacts("gate-one"))
        self.refuses("answer_shape", artifacts={"artifacts": []})

    def test_a_land_json_that_is_not_the_one_the_seal_describes_is_refused(self):
        self.fixture("one")
        self.refuses("land_differs", check_runs=self.runs(summary=seal.summary("0" * 64, ["one"])))
        self.refuses("land_differs", check_runs=self.runs(summary="no hash here"))
        # a land.json edited after the seal: valid, canonical, other bytes
        land = copy.deepcopy(self.land)
        land["baseline"] = "red"
        (self.out / assemble.LAND).write_bytes(assemble.land_text(land))
        self.refuses("land_differs")
        (self.out / assemble.LAND).write_text("{")
        self.refuses("land_differs")  # F6: the hash is compared before the bytes are parsed, so a bad hash is land_differs
        malformed = hashlib.sha256(b"{").hexdigest()
        self.refuses("land_invalid", check_runs=self.runs(summary=seal.summary(malformed, ["one"])))  # the hash holds: then it is parsed
        (self.out / assemble.LAND).unlink()
        self.refuses("land_invalid")

    def test_a_land_json_of_an_older_version_is_land_invalid_and_says_why(self):
        self.fixture("one")
        raw = json.dumps({**self.land, "version": 1}).encode()
        (self.out / assemble.LAND).write_bytes(raw)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.refuses("land_invalid", check_runs=self.runs(summary=seal.summary(hashlib.sha256(raw).hexdigest(), ["one"])))
        self.assertIn("version must be 2", err.getvalue())

    def test_f6_the_hash_is_compared_before_land_json_is_parsed(self):
        self.fixture("one")
        (self.out / assemble.LAND).write_text("{")
        with mock.patch.object(assemble, "read_land", side_effect=AssertionError("parsed before it was bound")):
            self.refuses("land_differs")

    def test_the_land_hash_must_be_one_whole_line_of_the_summary(self):
        self.fixture("one")
        good, bad = f"land_sha256: {self.land_sha}", "land_sha256: " + "0" * 64
        text = seal.summary(self.land_sha, ["one"])
        self.assertEqual(self.verify(check_runs=self.runs(summary=text)), ["one"])
        for name, summary in (
            ("an earlier look-alike", f"evil {good}\n{bad}\n"), ("a suffix", f"{good}0\n"), ("two lines, the good first", f"{good}\n{bad}\n"),
            ("two lines, the bad first", f"{bad}\n{good}\n"), ("the same line twice", f"{good}\n{good}\n"),
        ):
            with self.subTest(name):
                self.refuses("land_differs", check_runs=self.runs(summary=summary))

    def test_a_run_id_that_is_a_float_is_not_the_run(self):
        self.fixture("one")
        self.refuses("run_not_pivotal", run=self.run_object(id=float(RUN)))
        self.refuses("artifact_other_run", artifacts=self.artifacts(run=float(RUN)))
        self.assertEqual(self.verify(run=self.run_object(id=RUN)), ["one"])

    def test_the_workflow_path_is_the_one_piv_init_installs(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh-piv"))
        try:
            from piv import init
        finally:
            sys.path.pop(0)
        self.assertEqual(seal.WORKFLOW_PATH, init.WORKFLOW_PATH)

    def test_a_land_json_without_a_green_baseline_confirms_nothing(self):
        self.fixture("one")
        land = copy.deepcopy(self.land)
        land["baseline"] = "none"
        (self.out / assemble.LAND).write_bytes(assemble.land_text(land))
        text = hashlib.sha256((self.out / assemble.LAND).read_bytes()).hexdigest()
        self.refuses("baseline", check_runs=self.runs(summary=seal.summary(text, ["one"])))

    def test_f10_a_commit_that_is_not_the_head_of_a_stacked_slice_is_no_seal_even_with_a_matching_land_hash(self):
        """M4: the sealed-sha-on-stack check of seal.verify, tested here and not only through land.py (which repeats it)."""
        self.fixture("one", "two")
        inner = self.base  # a commit that is on no slice's head
        runs = [self.check_run(inner, RUN, output={"title": seal.TITLE, "summary": seal.summary(self.land_sha, ["one", "two"])})]
        self.refuses("sha_not_on_stack", sha=inner, check_runs=runs)

    def test_a_gate_that_does_not_confirm_the_sha_is_refused(self):
        self.fixture("one", "two")
        self.put_gate(self.out, "two", sha=self.h["one"])
        self.refuses("gate_invalid")
        self.put_gate(self.out, "two")
        self.put_gate(self.out, "one", check="red", green=False)
        self.refuses("gate_not_green")
        self.put_gate(self.out, "one", check="none", green=False)
        self.refuses("gate_not_green")
        self.put_gate(self.out, "one", id="two", sha=self.h["two"])
        self.refuses("gate_invalid")
        shutil.rmtree(self.gates / "gate-one")
        self.refuses("gate_invalid")

    def test_a_gate_directory_that_is_a_link_or_holds_more_is_refused(self):
        self.fixture("one")
        moved = self.root / "elsewhere"
        shutil.move(str(self.gates / "gate-one"), moved)
        (self.gates / "gate-one").symlink_to(moved)
        self.refuses("gate_invalid")
        (self.gates / "gate-one").unlink()
        shutil.move(str(moved), self.gates / "gate-one")
        (self.gates / "gate-one" / "x").write_text("x")
        self.refuses("gate_invalid")

    def test_the_answers_of_another_shape_are_refused_not_crashed_on(self):
        self.fixture("one")
        self.refuses("answer_shape", artifacts=None)
        with self.assertRaises(seal.SealError):
            seal.candidates({"check_runs": []}, self.sha)
        with self.assertRaises(seal.SealError):
            seal.candidates([{}] * seal.MAX_ITEMS, self.sha)
        self.refuses("no_seal", check_runs=[])
        self.refuses("sha", sha="HEAD")

    def test_the_verifier_agrees_with_what_publish_sent(self):
        """publish's own body, read back through the verifier as GitHub would list it."""
        self.fixture("one", "two")
        body = seal.payload(self.sha, RUN, self.land_sha, self.land["positions"])
        listed = {**self.check_run(self.sha), "output": {"title": body["output"]["title"], "summary": body["output"]["summary"]}}
        self.assertEqual(self.verify(check_runs=[listed]), ["one", "two"])
        self.assertTrue(os.path.isdir(self.gates))
        self.assertEqual(ISSUE, self.land["issue"])
        self.assertIsInstance(self.out, Path)
