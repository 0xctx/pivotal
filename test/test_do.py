"""piv do: one labelled issue, its mockup, and the run link.

Every gh call goes to the fake (test/fakegh.py), which logs it, so a test checks what was sent and in
what order, not only what came back. No test reaches GitHub: PIV_GH always names the fake.
"""

import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fakegh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))

from piv import cli, do, init  # noqa: E402
from piv.gh import Gh  # noqa: E402
from engine import trust  # noqa: E402

REPO = "0xctx/piv-sandbox"
DEFAULT_SHA = "1234567890abcdef1234567890abcdef12345678"
ISSUE = 7
ISSUE_URL = f"https://github.com/{REPO}/issues/{ISSUE}"
RUN_URL = f"https://github.com/{REPO}/actions/runs/42"
ACK = {"user": {"login": "github-actions[bot]"}, "body": f"⚡ run #42 started (claude-sub, trusted) · {RUN_URL}"}
PNG = b"\x89PNG\r\n\x1a\n" + b"mockup bytes" * 4
JPEG = b"\xff\xd8\xff\xe0" + b"mockup bytes" * 4
DIGEST_PATH = f"mockups/{hashlib.sha256(PNG).hexdigest()[:16]}.png"
ASSET_REF = "git/ref/heads/pivotal-assets"
COMMENTS = f"repos/{REPO}/issues/{ISSUE}/comments"
WRITE_FLAGS = ("POST", "PUT", "PATCH", "DELETE")


def label(name: str) -> str:
    return f"repos/{REPO}/labels/{name}"


def is_write(call: dict) -> bool:
    args = call["args"]
    if args[:2] in (["issue", "create"], ["issue", "edit"]):
        return True
    return "-X" in args and args[args.index("-X") + 1] in WRITE_FLAGS


class DoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.gh = fakegh.FakeGh(self.root / "bin")
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.image = self.root / "mock.png"
        self.image.write_bytes(PNG)

    def tearDown(self):
        self._tmp.cleanup()

    def do(self, *args: str, env: dict[str, str] | None = None) -> int:
        return cli.main(["do", *args], self.gh.env() if env is None else env, self.out, self.err)

    def happy_path(self, *, asset_branch: bool = True, mockup_present: bool = False, comments=([ACK],), line="pivotal"):
        """Routes for a clean run. The first route whose needle is in the argv answers, so a test that needs a
        different answer adds its route with ``first=True``.

        ``comments`` gives each answer to the comments poll in turn, as batches (the last one repeats).
        """
        ack_answers = [fakegh.ok(json.dumps(list(batch))) for batch in comments] or [fakegh.ok("[]")]
        asset_answer = (
            fakegh.ok(json.dumps({"object": {"sha": DEFAULT_SHA}})) if asset_branch else fakegh.not_found()
        )
        self.gh.route(f"api repos/{REPO}/contents/{DIGEST_PATH}", *(
            [fakegh.ok("{}")] if mockup_present else [fakegh.not_found()]
        ))
        self.gh.route(f"api -X PUT repos/{REPO}/contents/mockups/", fakegh.ok("{}"))
        self.gh.route(f"api -X POST repos/{REPO}/git/refs", fakegh.ok("{}"))
        self.gh.route(ASSET_REF, asset_answer)
        self.gh.route(f"api repos/{REPO}/git/ref/heads/main", fakegh.ok(json.dumps({"object": {"sha": DEFAULT_SHA}})))
        self.gh.route(f"api repos/{REPO}/issues/{ISSUE}/comments", *ack_answers)
        self.gh.route("issue create", fakegh.ok(ISSUE_URL + "\n"))
        self.gh.route("issue edit", fakegh.ok())
        self.gh.route(label(line), fakegh.ok(json.dumps({"name": line})))
        # Last, since its needle is a prefix of every other repo route.
        self.gh.route(f"api repos/{REPO}", fakegh.ok(json.dumps({"default_branch": "main"})))

    def calls(self, *words: str) -> list[dict]:
        return [c for c in self.gh.calls() if c["args"][: len(words)] == list(words)]

    def writes(self) -> list[dict]:
        return [c for c in self.gh.calls() if is_write(c)]

    def create_call(self) -> dict:
        creates = self.calls("issue", "create")
        self.assertEqual(len(creates), 1, "expected exactly one issue create")
        return creates[0]

    def run_no_wait(self, *args: str) -> int:
        with mock.patch.object(do, "POLL_SECONDS", 0), mock.patch.object(do, "POLLS", 3):
            return self.do(*args)

    def comment_reads(self) -> list[dict]:
        return [c for c in self.gh.calls() if any(arg.startswith(COMMENTS) for arg in c["args"])]

    def assertRefused(self, *args: str, code: int = 2) -> str:
        """The refusal exits with the usage code and makes no write call, so it came before the first write."""
        self.assertEqual(self.do(*args), code, self.out.getvalue())
        self.assertEqual(self.writes(), [], "a refusal must come before the first write")
        return self.err.getvalue()


class IssueTest(DoTest):
    def test_files_a_labelled_issue_with_the_title_and_the_text_on_stdin(self):
        self.happy_path()
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 0, self.err.getvalue())
        create = self.create_call()
        self.assertEqual(create["args"][:2], ["issue", "create"])
        self.assertEqual(create["args"][create["args"].index("--title") + 1], "add CSV export")
        self.assertIn("--body-file", create["args"])
        self.assertEqual(create["args"][create["args"].index("--body-file") + 1], "-")
        self.assertEqual(create["args"][create["args"].index("--repo") + 1], REPO)
        self.assertNotIn("--body", create["args"])
        self.assertNotIn("-b", create["args"])
        self.assertIn("add CSV export", create["stdin"])
        self.assertIn("- line: build", create["stdin"])

    def test_the_trigger_label_is_added_after_the_issue_exists_with_add_label(self):
        self.happy_path()
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 0, self.err.getvalue())
        edits = self.calls("issue", "edit")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["args"][:2], ["issue", "edit"])
        self.assertEqual(edits[0]["args"][2], str(ISSUE))
        self.assertEqual(edits[0]["args"][edits[0]["args"].index("--add-label") + 1], "pivotal")
        order = self.gh.calls()
        self.assertLess(order.index(self.create_call()), order.index(edits[0]))

    def test_the_default_line_is_the_bare_pivotal_label(self):
        self.happy_path()
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 0, self.err.getvalue())
        self.assertEqual(self.calls("issue", "edit")[0]["args"][-1], "pivotal")

    def test_each_line_gets_exactly_its_own_trigger_label_and_the_engine_reads_it_as_a_trigger(self):
        init_labels = {name for name, _, _ in init.LABELS}
        for line in do.LINES:
            with self.subTest(line=line):
                self.gh = fakegh.FakeGh(self.root / line)  # a fresh fake: each line's routes and log stand alone
                self.happy_path(line=f"pivotal:{line}" if line != "build" else "pivotal")
                self.assertEqual(self.run_no_wait(REPO, "x", "--line", line), 0, self.err.getvalue())
                added = self.calls("issue", "edit")[0]["args"]
                name = added[added.index("--add-label") + 1]
                self.assertIn(name, init_labels)
                self.assertIsNotNone(trust.PIVOTAL_LABEL.fullmatch(name), name)
                self.assertEqual(len(self.calls("issue", "edit")), 1, "one label, so the trigger fires once")

    def test_the_body_carries_the_text_then_the_footer_the_gate_reads(self):
        self.happy_path()
        self.assertEqual(
            self.run_no_wait(REPO, "add CSV export", "--line", "mirror", "--score", "npm run score", "--target", "0.9"),
            0,
            self.err.getvalue(),
        )
        text = self.create_call()["stdin"]
        self.assertTrue(text.startswith("add CSV export"))
        for line in ("- line: mirror", "- score: npm run score", "- target: 0.9"):
            self.assertIn(line, text)
        self.assertNotIn("- mockup:", text)

    def test_the_title_is_the_first_line_shortened_and_the_rest_stays_in_the_body(self):
        self.happy_path()
        long_first = "x" * 200
        self.assertEqual(self.run_no_wait(REPO, f"{long_first}\nsecond line"), 0, self.err.getvalue())
        create = self.create_call()
        title = create["args"][create["args"].index("--title") + 1]
        self.assertEqual(title, "x" * do.TITLE_MAX)
        self.assertIn("second line", create["stdin"])

    def test_only_the_first_line_is_on_the_command_line_and_the_rest_is_on_stdin(self):
        self.happy_path()
        text = "add CSV export\nthe rest: a --body flag, and sk-LOOKS-LIKE-A-KEY in it"
        self.assertEqual(self.run_no_wait(REPO, text), 0, self.err.getvalue())
        for call in self.gh.calls():
            for arg in call["args"]:
                self.assertNotIn("sk-LOOKS", arg, call["args"])
        self.assertIn("sk-LOOKS-LIKE-A-KEY", self.create_call()["stdin"])

    def test_a_failed_label_add_names_the_issue_that_exists_and_the_missing_label(self):
        self.happy_path()
        self.gh.route("issue edit", fakegh.fail("could not add label: 'pivotal' not found\n", 1), first=True)
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 1)
        self.assertIn(f"issue #{ISSUE} is at {ISSUE_URL}", self.err.getvalue())
        self.assertIn("its label pivotal could not be added", self.err.getvalue())

    def test_an_issue_create_that_prints_no_url_is_reported_without_a_second_create(self):
        self.happy_path()
        self.gh.route("issue create", fakegh.ok("nothing useful\n"), first=True)
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 1)
        self.assertIn("did not print the issue's URL", self.err.getvalue())
        self.assertEqual(len(self.calls("issue", "create")), 1)


class RunLinkTest(DoTest):
    def test_prints_the_run_link_once_ack_comments(self):
        self.happy_path(comments=([], [ACK]))
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 0, self.err.getvalue())
        self.assertIn(f"run link: {RUN_URL}", self.out.getvalue())
        self.assertEqual(len(self.calls("api", f"repos/{REPO}/issues/{ISSUE}/comments?per_page=100")), 2)

    def test_returns_the_run_link_so_piv_demo_can_list_it(self):
        self.happy_path(comments=([ACK],))
        env = self.gh.env()
        gh = Gh(env["PIV_GH"], init.gh_environment(env))  # what cli.main builds: the real wrapper over the fake binary
        req = do.request(REPO, "add CSV export")
        self.assertEqual(do.do(req, gh, lambda line: None, pause=lambda seconds: None, polls=3), RUN_URL)

    def test_the_issue_url_is_printed_before_the_wait_so_a_timeout_still_names_it(self):
        self.happy_path(comments=())
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 1)
        self.assertIn(f"issue created: {ISSUE_URL}", self.out.getvalue())

    def test_no_run_link_within_the_polls_exits_1_and_says_not_to_file_again(self):
        self.happy_path(comments=())
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 1)
        message = self.err.getvalue()
        self.assertIn(f"issue #{ISSUE} is at {ISSUE_URL}", message)
        self.assertIn("Do not file the request again", message)
        self.assertNotIn("run link:", self.out.getvalue())

    def test_a_comment_from_another_login_or_without_a_run_is_not_the_run_link(self):
        impostor = {"user": {"login": "someone"}, "body": ACK["body"]}
        not_a_run = {"user": {"login": "github-actions[bot]"}, "body": "⚡ build failed · " + RUN_URL}
        self.happy_path(comments=([impostor, not_a_run],))
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 1)
        self.assertNotIn("run link:", self.out.getvalue())

    def test_a_listing_that_is_not_a_list_is_a_failure_not_a_guess(self):
        self.happy_path()
        self.gh.route(f"api repos/{REPO}/issues/{ISSUE}/comments", fakegh.ok('{"message": "oops"}'), first=True)
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 1)
        self.assertIn("something other than a list", self.err.getvalue())

    def test_a_comments_read_that_keeps_failing_names_the_issue_and_gives_up_after_three_tries(self):
        self.happy_path(comments=())
        self.gh.route(COMMENTS, fakegh.fail("gh: Server Error (HTTP 503)\n", 1), first=True)
        with mock.patch.object(do, "POLL_SECONDS", 0), mock.patch.object(do, "POLLS", 10):
            self.assertEqual(self.do(REPO, "add CSV export"), 1)
        message = self.err.getvalue()
        self.assertIn(f"issue #{ISSUE} is at {ISSUE_URL}", message)
        self.assertIn("HTTP 503", message)
        self.assertIn("Do not file the request again", message)
        self.assertEqual(len(self.comment_reads()), do.POLL_FAILURES, "gives up after three reads, not all ten polls")
        self.assertEqual(len(self.calls("issue", "edit")), 1, "the label went on before the reads failed")
        self.assertNotIn("run link:", self.out.getvalue())

    def test_a_comments_answer_that_is_not_json_is_a_failed_read_that_names_the_issue(self):
        self.happy_path(comments=())
        self.gh.route(COMMENTS, fakegh.ok("<html>not json</html>"), first=True)
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 1)
        self.assertIn(f"issue #{ISSUE} is at {ISSUE_URL}", self.err.getvalue())
        self.assertIn("Do not file the request again", self.err.getvalue())

    def test_two_transient_read_failures_then_a_good_comment_still_prints_the_run_link(self):
        self.happy_path(comments=())
        self.gh.route(
            COMMENTS,
            fakegh.fail("gh: Server Error (HTTP 502)\n", 1),
            fakegh.fail("gh: API rate limit exceeded (HTTP 403)\n", 1),
            fakegh.ok(json.dumps([ACK])),
            first=True,
        )
        with mock.patch.object(do, "POLL_SECONDS", 0), mock.patch.object(do, "POLLS", 10):
            self.assertEqual(self.do(REPO, "add CSV export"), 0, self.err.getvalue())
        self.assertIn(f"run link: {RUN_URL}", self.out.getvalue())
        self.assertEqual(len(self.comment_reads()), 3)

    def test_the_run_link_has_no_trailing_punctuation(self):
        body = f"run #42 started (claude-sub, trusted) · {RUN_URL}."
        punctuated = {"user": {"login": "github-actions[bot]"}, "body": body}
        self.happy_path(comments=([punctuated],))
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 0, self.err.getvalue())
        self.assertIn(f"run link: {RUN_URL}\n", self.out.getvalue())

    def test_a_link_that_is_not_on_github_is_not_the_run_link(self):
        elsewhere = {"user": {"login": "github-actions[bot]"}, "body": "run #42 started · https://evil.example/runs/42"}
        self.happy_path(comments=([elsewhere],))
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 1)
        self.assertNotIn("run link:", self.out.getvalue())

    def test_a_link_with_a_control_character_is_not_the_run_link(self):
        ansi = {"user": {"login": "github-actions[bot]"}, "body": f"run #42 started · {RUN_URL}\x1b[0m"}
        self.happy_path(comments=([ansi],))
        self.assertEqual(self.run_no_wait(REPO, "add CSV export"), 1)
        self.assertNotIn("run link:", self.out.getvalue())


class MockupTest(DoTest):
    def test_a_missing_asset_branch_is_created_from_the_default_branch_head(self):
        self.happy_path(asset_branch=False)
        self.assertEqual(self.run_no_wait(REPO, "add CSV export", "--image", str(self.image)), 0, self.err.getvalue())
        posts = self.calls("api", "-X", "POST", f"repos/{REPO}/git/refs")
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0]["stdin"]), {"ref": "refs/heads/pivotal-assets", "sha": DEFAULT_SHA})

    def test_an_existing_asset_branch_is_used_and_not_created(self):
        self.happy_path(asset_branch=True)
        self.assertEqual(self.run_no_wait(REPO, "add CSV export", "--image", str(self.image)), 0, self.err.getvalue())
        self.assertEqual(self.calls("api", "-X", "POST", f"repos/{REPO}/git/refs"), [])

    def test_the_mockup_is_put_on_the_asset_branch_with_its_bytes_base64_on_stdin(self):
        self.happy_path()
        self.assertEqual(self.run_no_wait(REPO, "add CSV export", "--image", str(self.image)), 0, self.err.getvalue())
        puts = self.calls("api", "-X", "PUT", f"repos/{REPO}/contents/{DIGEST_PATH}")
        self.assertEqual(len(puts), 1)
        body = json.loads(puts[0]["stdin"])
        self.assertEqual(body["branch"], "pivotal-assets")
        self.assertEqual(base64.b64decode(body["content"]), PNG)
        for arg in puts[0]["args"]:
            self.assertNotIn("mockup bytes", arg)

    def test_the_mockup_path_is_named_by_its_digest_so_the_same_file_is_written_once(self):
        self.assertEqual(do.request(REPO, "x", image=str(self.image)).image.path, DIGEST_PATH)
        other = self.root / "other.jpg"
        other.write_bytes(JPEG)
        self.assertNotEqual(do.request(REPO, "x", image=str(other)).image.path, DIGEST_PATH)

    def test_an_existing_mockup_is_not_written_again(self):
        self.happy_path(mockup_present=True)
        self.assertEqual(self.run_no_wait(REPO, "add CSV export", "--image", str(self.image)), 0, self.err.getvalue())
        self.assertEqual(self.calls("api", "-X", "PUT"), [])
        self.assertIn("already on pivotal-assets", self.out.getvalue())

    def test_the_issue_body_names_the_mockup_path(self):
        self.happy_path()
        self.assertEqual(self.run_no_wait(REPO, "add CSV export", "--image", str(self.image)), 0, self.err.getvalue())
        self.assertIn(f"- mockup: {DIGEST_PATH}", self.create_call()["stdin"])

    def test_a_rejected_mockup_write_names_the_branch_and_says_nothing_was_filed(self):
        for status in (403, 422):
            with self.subTest(status=status):
                self.gh = fakegh.FakeGh(self.root / f"put{status}")
                self.err = io.StringIO()
                self.happy_path()
                self.gh.route(
                    f"api -X PUT repos/{REPO}/contents/mockups/",
                    fakegh.fail(f"gh: Repository rule violations found (HTTP {status})\n", 1),
                    first=True,
                )
                self.assertEqual(self.do(REPO, "x", "--image", str(self.image)), 1)
                message = self.err.getvalue()
                self.assertIn(f"{do.ASSET_BRANCH} on {REPO} refused the write (HTTP {status})", message)
                self.assertIn("protected or under a ruleset", message)
                self.assertIn("no issue was created", message)
                self.assertEqual(self.calls("issue", "create"), [], "the refused write comes before the issue")

    def test_a_refused_asset_branch_create_is_named_the_same_way(self):
        self.happy_path(asset_branch=False)
        self.gh.route(
            f"api -X POST repos/{REPO}/git/refs",
            fakegh.fail("gh: Resource not accessible by integration (HTTP 403)\n", 1),
            first=True,
        )
        self.assertEqual(self.do(REPO, "x", "--image", str(self.image)), 1)
        self.assertIn(f"{do.ASSET_BRANCH} on {REPO} refused the write (HTTP 403)", self.err.getvalue())
        self.assertEqual(self.calls("issue", "create"), [])

    def test_an_image_whose_bytes_are_not_its_extension_is_refused_before_any_write(self):
        self.happy_path()
        gif = self.root / "gif.png"
        gif.write_bytes(b"GIF89a" + b"x" * 20)
        jpg_named_png = self.root / "photo.jpg"
        jpg_named_png.write_bytes(PNG)
        for path in (gif, jpg_named_png):
            with self.subTest(path=path.name):
                self.assertIn("real PNG or JPEG", self.assertRefused(REPO, "x", "--image", str(path)))

    def test_a_jpeg_with_a_jpeg_signature_is_accepted(self):
        self.happy_path()
        photo = self.root / "photo.jpeg"
        photo.write_bytes(JPEG)
        self.assertEqual(self.run_no_wait(REPO, "add CSV export", "--image", str(photo)), 0, self.err.getvalue())


class RefusalTest(DoTest):
    """Every refusal exits with the usage code or the state code and makes no write call."""

    def test_a_bad_repo_refuses_with_no_call(self):
        self.assertRefused("nope", "x")
        self.assertEqual(self.gh.calls(), [])

    def test_empty_or_blank_text_is_refused(self):
        self.assertRefused(REPO, "   ")
        self.assertRefused(REPO, "")

    def test_text_longer_than_the_limit_is_refused(self):
        self.assertIn("at most", self.assertRefused(REPO, "x" * (do.TEXT_MAX + 1)))

    def test_text_with_a_nul_byte_is_refused(self):
        self.assertIn("NUL", self.assertRefused(REPO, "bad\x00text"))

    def test_an_unknown_line_is_refused(self):
        self.assertIn("--line must be one of", self.assertRefused(REPO, "x", "--line", "bogus"))

    def test_score_and_target_go_together(self):
        self.assertIn("go together", self.assertRefused(REPO, "x", "--score", "npm run score"))
        self.assertIn("go together", self.assertRefused(REPO, "x", "--target", "0.9"))

    def test_a_target_that_is_not_a_plain_decimal_is_refused(self):
        for bad in ("abc", "-0.5", "0.9\n", " 0.9", "1e3", "nan"):
            with self.subTest(target=bad):
                self.assertIn("--target must be", self.assertRefused(REPO, "x", "--score", "s", "--target", bad))

    def test_piv_and_the_engine_accept_exactly_the_same_targets(self):
        from engine import hill
        for target in ("0.9", ".9", "1", "1.0", "0.000001", "0.9999999", "01", "00.5", "1.5", "0", "0.0", "5", "1.", "0.", ".", "1.10"):
            with self.subTest(target=target):
                try:
                    hill.check_target(target)
                    engine_takes = True
                except hill.HillError:
                    engine_takes = False
                piv_takes = bool(do.TARGET.fullmatch(target)) and 0 < float(target) <= 1
                self.assertEqual(piv_takes, engine_takes)

    def test_a_target_in_non_ascii_digits_is_refused(self):
        # Python reads these as numbers, but a shell, jq or JavaScript reader of the footer would not.
        for bad in ("٣", "٠.٩", "０.９", "३.5"):
            with self.subTest(target=bad):
                self.assertIn("--target must be", self.assertRefused(REPO, "x", "--score", "s", "--target", bad))

    def test_a_score_with_a_newline_is_refused(self):
        self.assertIn("one non-empty line", self.assertRefused(REPO, "x", "--score", "a\nb", "--target", "0.9"))

    def test_a_score_with_a_line_separator_python_splits_on_is_refused(self):
        # str.splitlines() splits on these, so a reader of the footer would see a new line and a forged field.
        for separator in ("\x85", " ", " "):
            with self.subTest(separator=repr(separator)):
                score = f"npm run score{separator}- target: 0.1"
                self.assertIn("one non-empty line", self.assertRefused(REPO, "x", "--score", score, "--target", "0.9"))

    def test_a_score_longer_than_the_limit_is_refused(self):
        score = "s" * (do.SCORE_MAX + 1)
        message = self.assertRefused(REPO, "x", "--score", score, "--target", "0.9")
        self.assertIn(f"at most {do.SCORE_MAX} characters", message)

    def test_the_whole_issue_body_is_capped_not_only_the_text(self):
        # The text is short, but the footer (score and target) takes the body over the limit.
        with mock.patch.object(do, "BODY_MAX", 120):
            message = self.assertRefused(REPO, "x" * 60, "--score", "s" * 30, "--target", "0.9")
        self.assertIn("at most 120 characters", message)

    def test_an_image_with_another_extension_is_refused(self):
        bad = self.root / "mock.gif"
        bad.write_bytes(PNG)
        self.assertIn(".png", self.assertRefused(REPO, "x", "--image", str(bad)))

    def test_a_missing_image_is_refused(self):
        self.assertIn("no such file", self.assertRefused(REPO, "x", "--image", str(self.root / "gone.png")))

    def test_an_image_that_is_a_directory_is_refused(self):
        folder = self.root / "dir.png"
        folder.mkdir()
        self.assertIn("no such file", self.assertRefused(REPO, "x", "--image", str(folder)))

    def test_an_image_over_the_size_limit_is_refused(self):
        big = self.root / "big.png"
        with open(big, "wb") as fh:
            fh.truncate(do.IMAGE_MAX_BYTES + 1)
        self.assertIn("larger than", self.assertRefused(REPO, "x", "--image", str(big)))

    def test_a_missing_trigger_label_refuses_before_any_write_and_says_to_run_init(self):
        self.happy_path()
        self.gh.route(label("pivotal"), fakegh.not_found(), first=True)
        self.assertEqual(self.do(REPO, "x"), 1)
        self.assertIn(f"run piv init {REPO} first", self.err.getvalue())
        self.assertEqual(self.writes(), [])

    def test_a_missing_trigger_label_writes_no_mockup_either(self):
        self.happy_path(asset_branch=False)
        self.gh.route(label("pivotal"), fakegh.not_found(), first=True)
        self.assertEqual(self.do(REPO, "x", "--image", str(self.image)), 1)
        self.assertIn(f"run piv init {REPO} first", self.err.getvalue())
        self.assertEqual(self.writes(), [], "no branch and no mockup before the label is known to exist")

    def test_a_repo_that_cannot_be_read_refuses_before_any_write(self):
        self.happy_path()
        self.gh.route(f"api repos/{REPO}", fakegh.fail("gh: Not Found (HTTP 404)\n", 1), first=True)
        self.assertEqual(self.do(REPO, "x", "--image", str(self.image)), 1)
        self.assertEqual(self.writes(), [])


class FakeStrictnessTest(unittest.TestCase):
    """The fake refuses a flag the real command does not take, so these tests cannot pass on a flag gh lacks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gh = fakegh.FakeGh(Path(self._tmp.name) / "bin")
        self.gh.route("issue create", fakegh.ok())

    def tearDown(self):
        self._tmp.cleanup()

    def run_fake(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.gh.path), *argv], input="", capture_output=True, text=True, check=False)

    def test_refuses_a_flag_that_gh_issue_create_does_not_have(self):
        self.assertEqual(self.run_fake("issue", "create", "--labels", "x").returncode, 2)

    def test_accepts_the_body_file_flag_and_the_stdin_dash(self):
        self.assertEqual(self.run_fake("issue", "create", "--body-file", "-").returncode, 0)

    def test_refuses_an_issue_edit_flag_that_does_not_exist(self):
        self.gh.route("issue edit", fakegh.ok())
        self.assertEqual(self.run_fake("issue", "edit", "7", "--add-labels", "x").returncode, 2)

    def test_accepts_add_label_on_issue_edit(self):
        self.gh.route("issue edit", fakegh.ok())
        self.assertEqual(self.run_fake("issue", "edit", "7", "--add-label", "pivotal").returncode, 0)


class LauncherTest(DoTest):
    """The extension entry point, run as gh runs it: a subprocess, with the fake as PIV_GH."""

    LAUNCHER = ROOT / "gh-piv" / "gh-piv"

    def run_launcher(self, *args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.LAUNCHER), *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
        )

    def test_user_visible_check_prints_a_labelled_issue_and_a_run_link_and_exits_0(self):
        self.happy_path()
        work = self.root / "work"
        work.mkdir()
        done = self.run_launcher("do", REPO, "add CSV export", cwd=work, env=self.gh.env())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"issue created: {ISSUE_URL}", done.stdout)
        self.assertIn(f"run link: {RUN_URL}", done.stdout)

    def test_without_text_it_exits_2_with_a_usage_message(self):
        done = self.run_launcher("do", REPO, cwd=self.root, env=self.gh.env())
        self.assertEqual(done.returncode, 2)
        self.assertIn("TEXT", done.stderr)
        self.assertEqual(self.gh.calls(), [])

    def test_writes_nothing_to_the_working_directory(self):
        self.happy_path()
        work = self.root / "work"
        work.mkdir()
        before = set(os.listdir(work))
        self.run_launcher("do", REPO, "add CSV export", cwd=work, env=self.gh.env())
        self.assertEqual(set(os.listdir(work)), before)


class LabelTableTest(unittest.TestCase):
    def test_every_line_maps_to_a_label_init_creates(self):
        names = {name for name, _, _ in init.LABELS}
        for line in do.LINES:
            with self.subTest(line=line):
                self.assertIn(do.request(REPO, "x", line=line).label, names)

    def test_the_lines_are_the_ones_init_defines_not_a_copy(self):
        self.assertIs(do.LINES, init.LINES)


class HelpTest(DoTest):
    def test_a_text_that_starts_with_a_dash_follows_a_double_dash(self):
        self.happy_path()
        self.assertEqual(self.run_no_wait(REPO, "--", "-fix the export"), 0, self.err.getvalue())
        args = self.create_call()["args"]
        self.assertEqual(args[args.index("--title") + 1], "-fix the export")

    def test_the_help_says_a_text_that_starts_with_a_dash_follows_double_dash(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["do", "--help"])
        self.assertIn("after --", " ".join(out.getvalue().split()))


if __name__ == "__main__":
    unittest.main()
