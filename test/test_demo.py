"""piv demo (T9): the sandbox repo, its template, its three issues, and --clean.

Every gh call goes to the fake (test/fakegh.py), which logs it, and PIV_GH always names the fake, so no test
reaches GitHub. init.init and do.do are replaced where their own tests already cover their writes; the request
checks, the template, and the order of the writes are real.
"""

import argparse
import base64
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fakegh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))

from engine import route  # noqa: E402
from piv import cli, demo, do, init  # noqa: E402
from piv.gh import Gh, PivError  # noqa: E402

LOGIN = "demo-user"
REPO = f"{LOGIN}/piv-demo"
SHA = "1234567890abcdef1234567890abcdef12345678"
RUN_URLS = [f"https://github.com/{REPO}/actions/runs/{number}" for number in (1, 2, 3)]
CONTENTS = f"repos/{REPO}/contents/"
NO_MARKER = object()  # route_repo's marker answer for a repo with no marker file
# Logins that are not an owner's name in GitHub's shape: a newline, a path, a flag, a space, non-ASCII, empty.
BAD_LOGINS = ("a\nb", "../x", "a/b", "-lead", "x y", "\u00fc", "")
TAG = "v1.0.0"  # the release of the engine repo that the demo pins its caller to
PIN = "abcdef0123456789abcdef0123456789abcdef01"  # the commit that TAG points to
CALLER = (ROOT / "templates" / "caller.yml").read_text(encoding="utf-8")  # what init fetches and pins


class DemoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.gh = fakegh.FakeGh(self.root / "bin")
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    def piv(self, *args: str) -> int:
        return cli.main(["demo", *args], self.gh.env(), self.out, self.err)

    def calls(self, *words: str) -> list[dict]:
        """The calls whose argv begins with ``words``."""
        return [c for c in self.gh.calls() if c["args"][: len(words)] == list(words)]

    def puts(self) -> list[dict]:
        return [c for c in self.gh.calls() if "-X" in c["args"] and c["args"][c["args"].index("-X") + 1] == "PUT"]

    def writes(self) -> list[dict]:
        """Every call that may change GitHub: anything but a GET through ``gh api``."""
        return [c for c in self.gh.calls() if c["args"][0] != "api" or "-X" in c["args"]]

    def route_repo(
        self, *, exists: bool, marker=None, release: bool = True, create_ok: bool = True, fork=False, login: str = LOGIN
    ) -> None:
        """Answers for the demo repo. The repo route answers twice on a create: absent (the check), then present with
        its default branch (after ``repo create``). ``marker`` is the .piv-demo file's text, or NO_MARKER, or None
        for the demo's own marker. ``release=False`` makes the engine repo have no release; ``create_ok=False`` makes
        ``repo create`` fail. ``fork`` is the repo's fork field; None leaves the field out of the answer. ``login`` is
        what ``api user`` answers, and so the owner of the repo."""
        repo = f"{login}/{demo.NAME}"
        contents = f"repos/{repo}/contents/"
        info = {"default_branch": "main"}
        if fork is not None:
            info["fork"] = fork
        present = fakegh.ok(json.dumps(info))
        self.gh.route(f"api repos/{repo}", present if exists else fakegh.not_found(), present, exact=True)
        self.gh.route("api user", fakegh.ok(json.dumps({"login": login})), exact=True)
        latest = fakegh.ok(json.dumps({"tag_name": TAG})) if release else fakegh.not_found()
        self.gh.route(f"api repos/{init.ENGINE_REPO}/releases/latest", latest, exact=True)
        self.gh.route(f"api repos/{init.ENGINE_REPO}/commits/{TAG}", fakegh.ok(json.dumps({"sha": PIN})), exact=True)
        self.gh.route(f"{init.CALLER_PATH}?ref={PIN}", fakegh.ok(CALLER))
        created = fakegh.ok(f"https://github.com/{repo}\n") if create_ok else fakegh.fail("GraphQL: Name already exists\n")
        self.gh.route("repo create", created)
        self.gh.route("repo delete", fakegh.ok())
        self.gh.route("-X PUT", fakegh.ok("{}"))
        for path in demo.REPO_FILES:  # an existing file is read for its sha before it is written: only the README has one
            answer = fakegh.ok(json.dumps({"sha": SHA})) if path == "README.md" else fakegh.not_found()
            self.gh.route(f"api {contents}{path}", answer, exact=True)
        if marker is NO_MARKER:
            self.gh.route(f"{contents}{demo.MARKER}", fakegh.not_found())
        else:
            text = demo.MARKER_TEXT if marker is None else marker
            self.gh.route(f"{contents}{demo.MARKER}", fakegh.ok(text))


class SecretsTest(DemoTest):
    def test_init_is_given_only_the_secrets_free_kaggle_needs(self):
        self.route_repo(exists=False)
        env = dict(self.gh.env(), CANARY_NOT_A_SECRET="canary", **{name: f"set-{name}" for name in init.SECRETS})
        with mock.patch.object(init, "init") as init_mock, mock.patch.object(do, "do", return_value=RUN_URLS[0]):
            self.assertEqual(cli.main(["demo"], env, self.out, self.err), 0, self.err.getvalue())
        given = init_mock.call_args.args[2]
        self.assertEqual(set(given), set(demo.PRESET_SECRETS))
        self.assertEqual(given["KAGGLE_KEY"], "set-KAGGLE_KEY")
        self.assertNotIn("CANARY_NOT_A_SECRET", given)
        self.assertNotIn("OPENAI_API_KEY", given)

    def test_the_preset_secrets_are_what_the_engine_doors_of_free_kaggle_need(self):
        tiers = json.loads((ROOT / "presets" / f"{demo.PRESET}.json").read_text(encoding="utf-8"))["tiers"]
        doors = {entry.split(":", 1)[1].split("/", 1)[0] for tier in tiers.values() for entry in tier}
        needed = {name for door in doors for name in route.DOOR_SECRETS[door]}
        # the kaggle door comes with the Kaggle endpoint's URL and the Kaggle account, which models-up uses
        self.assertEqual(doors, {"google", "groq", "openrouter", "kaggle"})
        self.assertEqual(set(demo.PRESET_SECRETS),
                         needed | {init.KAGGLE_URL, "KAGGLE_USERNAME", "KAGGLE_KEY"})


class WarningTest(DemoTest):
    def test_one_line_says_the_repo_will_be_public_before_the_create(self):
        self.route_repo(exists=False)
        events = []

        class RecordingGh(Gh):
            def run(self, *args, **kwargs):
                events.append(("gh", args[:2]))
                return super().run(*args, **kwargs)

        with mock.patch.object(init, "init"), mock.patch.object(do, "do", return_value=RUN_URLS[0]):
            code = demo.run(RecordingGh(str(self.gh.path), self.gh.env()), self.gh.env(),
                            lambda line: events.append(("say", line)))
        self.assertEqual(code, 0)
        warnings = [line for kind, line in events if kind == "say" and "PUBLIC" in line]
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("\n", warnings[0])
        self.assertLess(events.index(("say", warnings[0])), events.index(("gh", ("repo", "create"))))


class OwnerTest(DemoTest):
    def test_a_login_that_is_not_an_owner_name_is_refused_before_any_repo_call(self):
        for number, login in enumerate(BAD_LOGINS):
            for args in ((), ("--clean",)):
                with self.subTest(login=login, args=args):
                    self.gh = fakegh.FakeGh(self.root / f"bin-{number}-{len(args)}")
                    self.err = io.StringIO()
                    self.route_repo(exists=False, login=login)
                    self.assertNotEqual(self.piv(*args), 0)
                    self.assertIn("no usable login", self.err.getvalue())
                    self.assertEqual([c["args"] for c in self.gh.calls()], [["api", "user"]])


class ParserTest(unittest.TestCase):
    def demo_parser(self) -> argparse.ArgumentParser:
        [subcommands] = [a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction)]
        return subcommands.choices["demo"]

    def test_the_demo_subcommand_takes_only_clean(self):
        flags = {flag for action in self.demo_parser()._actions for flag in action.option_strings}
        self.assertEqual(flags, {"-h", "--help", "--clean"})

    def test_no_argument_can_name_another_owner_or_repo(self):
        for flag in ("--owner", "--repo", "--user"):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
                cli.build_parser().parse_args(["demo", flag, "someone-else"])


class TemplateTest(unittest.TestCase):
    def test_the_marker_file_carries_the_marker_text(self):
        self.assertEqual((demo.TEMPLATE / "repo" / demo.MARKER).read_text(encoding="utf-8"), demo.MARKER_TEXT)

    def test_the_template_has_every_file_the_repo_gets_and_no_other(self):
        repo = demo.TEMPLATE / "repo"
        on_disk = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*") if path.is_file())
        self.assertEqual(sorted(demo.REPO_FILES), on_disk, "REPO_FILES is exactly the files under demo_template/repo")
        missing = [p for p in demo.REPO_FILES if not (repo / p).is_file()]
        self.assertEqual(missing, [])

    def test_the_seeded_bug_is_red_on_exactly_the_discount_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in demo.REPO_FILES:
                target = Path(tmp) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(demo.TEMPLATE / "repo" / path, target)
            done = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=tmp, capture_output=True, text=True, check=False, timeout=120,
                env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
            )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("FAILED (failures=1)", done.stderr)
        self.assertIn("test_a_25_percent_discount_on_120_and_80_is_150", done.stderr)


class CreateTest(DemoTest):
    def stopped_lines(self) -> list[str]:
        """The lines piv demo prints when an interrupt stops it; each names the way out."""
        return [line for line in self.out.getvalue().splitlines() if line.startswith("piv demo: stopped")]

    def create(self):
        """Run piv demo with init and do replaced. init records how many template files were written when it runs."""
        self.puts_at_init: list[int] = []

        def init_stub(req, gh, env, say):
            self.puts_at_init.append(len(self.puts()))

        links = iter(RUN_URLS)
        with mock.patch.object(init, "init", side_effect=init_stub) as init_mock, \
                mock.patch.object(do, "do", side_effect=lambda req, gh, say: next(links)) as do_mock:
            code = self.piv()
        return code, init_mock, do_mock

    def test_reads_init_needs_come_before_the_repo_is_made_then_it_creates_writes_and_inits_on_free_kaggle(self):
        self.route_repo(exists=False)
        code, init_mock, _ = self.create()
        self.assertEqual(code, 0, self.err.getvalue())
        [create] = self.calls("repo", "create")
        self.assertEqual(
            create["args"],
            ["repo", "create", REPO, "--public", "--add-readme", "--description", demo.DESCRIPTION],
        )
        self.assertEqual(len(self.puts()), len(demo.REPO_FILES))
        # init's release, commit and caller reads are checked before the create, so no refusal of theirs writes anything
        reads = [
            self.calls("api", f"repos/{init.ENGINE_REPO}/releases/latest")[0],
            self.calls("api", f"repos/{init.ENGINE_REPO}/commits/{TAG}")[0],
            self.calls("api", "-H", "Accept: application/vnd.github.raw",
                       f"repos/{init.ENGINE_REPO}/contents/{init.CALLER_PATH}?ref={PIN}")[0],
        ]
        for read in reads:
            self.assertLess(self.gh.calls().index(read), self.gh.calls().index(create))
        self.assertLess(self.gh.calls().index(create), self.gh.calls().index(self.puts()[0]))
        self.assertEqual(self.puts_at_init, [len(demo.REPO_FILES)], "init runs only after every file is written")
        init_req = init_mock.call_args.args[0]
        self.assertEqual((init_req.repo, init_req.check, init_req.preset, init_req.level, init_req.release),
                         (REPO, demo.CHECK, "free-kaggle", None, PIN))

    def test_a_refused_init_check_writes_nothing_because_it_is_made_before_the_repo(self):
        self.route_repo(exists=False, release=False)
        self.assertEqual(self.piv(), 1)
        self.assertIn("nothing was written", self.err.getvalue())
        self.assertIn(f"{init.ENGINE_REPO} has no release yet", self.err.getvalue())
        self.assertEqual(self.calls("repo", "create"), [])
        self.assertEqual(self.writes(), [], "no call that changes GitHub is made before the refusal")

    def refused_caller_writes_nothing(self, template: str, reason: str) -> None:
        """init's caller template, replaced by ``template``, is refused before the repo is created: exit 1, nothing
        written, and init and do never run."""
        self.route_repo(exists=False)
        self.gh.route(f"{init.CALLER_PATH}?ref={PIN}", fakegh.ok(template), first=True)
        with mock.patch.object(init, "init") as init_mock, mock.patch.object(do, "do") as do_mock:
            self.assertEqual(self.piv(), 1, self.err.getvalue())
        self.assertIn("nothing was written", self.err.getvalue())
        self.assertIn(reason, self.err.getvalue())
        self.assertEqual(self.writes(), [], "no call that changes GitHub is made before the refusal")
        init_mock.assert_not_called()
        do_mock.assert_not_called()

    def test_a_caller_with_no_pin_placeholder_is_refused_before_the_repo_is_made(self):
        template = CALLER.replace(init.PIN_PLACEHOLDER, "")
        self.assertNotIn(init.PIN_PLACEHOLDER, template, "the probe removed the placeholder")
        self.refused_caller_writes_nothing(template, "no pin placeholder")

    def test_a_caller_with_an_unpinned_pivotal_reference_is_refused_before_the_repo_is_made(self):
        unpinned = f"__PIVOTAL_ENGINE__/.github/workflows/land.yml@{init.PIN_PLACEHOLDER}"
        template = CALLER.replace(unpinned, "__PIVOTAL_ENGINE__/.github/workflows/land.yml@main")
        self.assertNotEqual(template, CALLER, "the probe changed one reference to @main")
        self.refused_caller_writes_nothing(template, "not pinned")

    def test_each_file_is_written_with_the_template_bytes_base64_on_stdin(self):
        self.route_repo(exists=False)
        self.create()
        written = {}
        for call in self.puts():
            path = call["args"][3][len(CONTENTS):]
            written[path] = json.loads(call["stdin"])
        self.assertEqual(sorted(written), sorted(demo.REPO_FILES))
        for path, body in written.items():
            self.assertEqual(base64.b64decode(body["content"]), (demo.TEMPLATE / "repo" / path).read_bytes(), path)
            self.assertEqual(body["branch"], "main")
        self.assertEqual(written["README.md"]["sha"], SHA, "the README --add-readme made is replaced by its blob sha")
        self.assertNotIn("sha", written["shop/cart.py"])
        self.assertIn(b"return subtotal - discount_percent", base64.b64decode(written["shop/cart.py"]["content"]))

    def test_files_three_issues_and_prints_their_run_links(self):
        self.route_repo(exists=False)
        code, _, do_mock = self.create()
        self.assertEqual(code, 0, self.err.getvalue())
        requests = [call.args[0] for call in do_mock.call_args_list]
        self.assertEqual([req.line for req in requests], ["build", "build", "docs"])
        self.assertTrue(all(req.image is None for req in requests), "no issue attaches an image")
        self.assertTrue(all("- mockup:" not in do.body(req) for req in requests), "no footer names a mockup")
        self.assertTrue(all(req.repo == REPO for req in requests))
        output = self.out.getvalue()
        self.assertIn("run links:", output)
        for number, url in enumerate(RUN_URLS, 1):
            self.assertIn(f"  {number} of 3 (", output)
            self.assertIn(url, output)

    def test_an_existing_repo_is_refused_and_nothing_is_written(self):
        self.route_repo(exists=True)
        code, init_mock, do_mock = self.create()
        self.assertEqual(code, 2)
        self.assertIn("already exists", self.err.getvalue())
        self.assertIn("piv demo --clean", self.err.getvalue())
        self.assertEqual(self.calls("repo", "create"), [])
        self.assertEqual(self.puts(), [])
        init_mock.assert_not_called()
        do_mock.assert_not_called()

    def test_an_existing_repo_message_also_names_gh_repo_delete_for_a_repo_with_no_marker(self):
        self.route_repo(exists=True)
        self.assertEqual(self.piv(), 2)
        self.assertIn(f"gh repo delete {REPO} --yes", self.err.getvalue())
        self.assertIn(demo.MARKER, self.err.getvalue(), "the message says which marker --clean needs")

    def test_a_failure_after_the_repo_exists_says_so_and_names_clean(self):
        self.route_repo(exists=False)
        with mock.patch.object(init, "init"), \
                mock.patch.object(do, "do", side_effect=PivError("issue create failed (exit 1)")):
            code = self.piv()
        self.assertEqual(code, 1)
        self.assertIn(f"{REPO} was created and is partly set up", self.err.getvalue())
        self.assertIn("piv demo --clean deletes it", self.err.getvalue())
        self.assertEqual(len(self.calls("repo", "create")), 1)

    def test_a_failed_repo_create_names_the_gh_delete_because_clean_needs_the_marker(self):
        self.route_repo(exists=False, create_ok=False)
        self.assertEqual(self.piv(), 1)
        self.assertIn("gh repo create", self.err.getvalue(), "the failed call is named")
        self.assertIn(f"gh repo delete {REPO} --yes", self.err.getvalue())
        self.assertIn("piv demo --clean will not delete it", self.err.getvalue())
        self.assertNotIn("piv demo --clean deletes it", self.err.getvalue())
        self.assertEqual(self.puts(), [])

    def test_a_failure_before_the_marker_is_written_names_the_gh_delete_not_clean(self):
        self.route_repo(exists=False)
        self.gh.route(f"-X PUT {CONTENTS}{demo.MARKER}", fakegh.fail("gh: boom (HTTP 500)\n"), first=True)
        self.assertEqual(self.piv(), 1)
        self.assertIn(f"{REPO} was created and is partly set up", self.err.getvalue())
        self.assertIn(f"gh repo delete {REPO} --yes", self.err.getvalue())
        self.assertNotIn("piv demo --clean deletes it", self.err.getvalue())

    def test_an_interrupt_after_the_marker_is_written_prints_the_way_out_and_then_stops(self):
        self.route_repo(exists=False)
        with mock.patch.object(init, "init", side_effect=KeyboardInterrupt) as init_mock, \
                mock.patch.object(do, "do") as do_mock:
            with self.assertRaises(KeyboardInterrupt):
                self.piv()
        self.assertEqual(len(self.puts()), len(demo.REPO_FILES), "the marker is written before init is reached")
        self.assertEqual(init_mock.call_count, 1)
        do_mock.assert_not_called()
        stopped = self.stopped_lines()
        self.assertEqual(len(stopped), 1, self.out.getvalue())
        self.assertIn(f"{REPO} was created and is partly set up", stopped[0])
        self.assertIn("piv demo --clean deletes it, then run piv demo again", stopped[0])

    def test_an_interrupt_during_the_repo_create_prints_the_gh_delete_way_out_and_then_stops(self):
        self.route_repo(exists=False)

        class InterruptedCreate(Gh):
            def run(self, *args, **kwargs):
                if args[:2] == ("repo", "create"):
                    raise KeyboardInterrupt
                return super().run(*args, **kwargs)

        say = lambda line: self.out.write(line + "\n")  # noqa: E731
        with self.assertRaises(KeyboardInterrupt):
            demo.run(InterruptedCreate(str(self.gh.path), self.gh.env()), self.gh.env(), say)
        self.assertEqual(self.puts(), [])
        stopped = self.stopped_lines()
        self.assertEqual(len(stopped), 1, self.out.getvalue())
        self.assertIn(f"gh repo delete {REPO} --yes", stopped[0])
        self.assertNotIn("piv demo --clean deletes it", stopped[0], "no marker is in, so --clean is not the way out")

    def test_the_repo_is_named_by_the_authenticated_login_whoever_it_is(self):
        self.route_repo(exists=False, login="other-user")
        code, _, _ = self.create()
        self.assertEqual(code, 0, self.err.getvalue())
        [create] = self.calls("repo", "create")
        self.assertEqual(create["args"][2], "other-user/piv-demo")

    def test_a_refused_issue_is_refused_before_the_repo_is_made(self):
        self.route_repo(exists=False)
        with mock.patch.object(demo, "ISSUES", (demo.Issue("nonsense", "Fix it."),)):
            self.assertEqual(self.piv(), 2)
        self.assertEqual(self.calls("repo", "create"), [])
        self.assertEqual(self.puts(), [])


class CleanTest(DemoTest):
    def test_deletes_the_demo_repo_when_its_marker_says_so(self):
        self.route_repo(exists=True)
        self.assertEqual(self.piv("--clean"), 0, self.err.getvalue())
        self.assertEqual([c["args"] for c in self.calls("repo", "delete")], [["repo", "delete", REPO, "--yes"]])
        self.assertIn(f"deleted {REPO}", self.out.getvalue())

    def test_a_repo_whose_marker_is_other_text_is_not_deleted(self):
        self.route_repo(exists=True, marker="some other project\n")
        self.assertEqual(self.piv("--clean"), 2)
        self.assertIn("not the demo's marker", self.err.getvalue())
        self.assertEqual(self.calls("repo", "delete"), [])

    def test_a_marker_with_extra_text_is_not_deleted(self):
        self.route_repo(exists=True, marker=demo.MARKER_TEXT + "extra text\n")
        self.assertEqual(self.piv("--clean"), 2)
        self.assertIn("not the demo's marker", self.err.getvalue())
        self.assertEqual(self.calls("repo", "delete"), [])

    def test_a_marker_without_its_newline_is_not_deleted(self):
        self.route_repo(exists=True, marker=demo.MARKER_TEXT.rstrip("\n"))
        self.assertEqual(self.piv("--clean"), 2)
        self.assertEqual(self.calls("repo", "delete"), [])

    def test_a_repo_with_no_marker_file_is_not_deleted(self):
        self.route_repo(exists=True, marker=NO_MARKER)
        self.assertEqual(self.piv("--clean"), 2)
        self.assertIn("not a piv demo", self.err.getvalue())
        self.assertEqual(self.calls("repo", "delete"), [])

    def test_a_fork_with_the_demo_marker_is_not_deleted(self):
        self.route_repo(exists=True, fork=True)
        self.assertEqual(self.piv("--clean"), 2)
        self.assertIn("is a fork", self.err.getvalue())
        self.assertEqual(self.calls("repo", "delete"), [])

    def test_an_answer_with_no_fork_field_is_not_deleted(self):
        self.route_repo(exists=True, fork=None)
        self.assertEqual(self.piv("--clean"), 2)
        self.assertEqual(self.calls("repo", "delete"), [])

    def test_with_no_demo_repo_clean_says_so_and_deletes_nothing(self):
        self.route_repo(exists=False)
        self.assertEqual(self.piv("--clean"), 0)
        self.assertIn("nothing to delete", self.out.getvalue())
        self.assertEqual(self.calls("repo", "delete"), [])


class ModuleTest(unittest.TestCase):
    def test_the_issue_texts_are_non_empty_within_the_limit_and_name_a_line_that_exists(self):
        for issue in demo.ISSUES:
            self.assertLessEqual(len(issue.text), do.TEXT_MAX)
            self.assertTrue(issue.text.strip().splitlines()[0])
            self.assertIn(issue.line, init.LINES)

    def test_the_demo_is_on_the_free_kaggle_preset_that_ships(self):
        self.assertEqual(demo.PRESET, "free-kaggle")
        self.assertTrue((ROOT / "presets" / f"{demo.PRESET}.json").is_file())


if __name__ == "__main__":
    unittest.main()
