"""piv kaggle setup and rotate: every kaggle and gh call goes to a fake, never to a service.

Both fakes log each call, so a test checks what was sent, where, and in what order, not only what came back.
The fake kaggle copies its upload folder into the log, which is how a test sees the dataset and its file modes.
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

import fakegh
import fakekaggle

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gh-piv"))

from engine import modelsup  # noqa: E402  (the second package with its own Kaggle environment)
from piv import cli, init, serve  # noqa: E402
from piv.gh import Gh, PivError  # noqa: E402

USER = "ada"
REF = f"{USER}/pivotal-serve-secrets"
URL = "https://pivotal-serve.ngrok-free.app"
TOKEN = "2ngrok-TOKENvalue_1234567890"
REPO = "0xctx/piv-sandbox"
OTHER = "0xctx/piv-web"
KGAT = "KGAT_0123456789abcdef0123456789abcdef"  # a kaggle API token: kaggle honours it only as KAGGLE_API_TOKEN
KEY_SHAPE = re.compile(r"[A-Za-z0-9_-]{43,}")
MISSING = fakegh.fail("kaggle: 404 Not Found: no such dataset\n")
# kaggle 2.2.4 answers 403 for a dataset that does not exist (measured live), so setup asks the account's own listing.
FORBIDDEN_STATUS = fakegh.fail("403 Client Error: Forbidden for url: https://www.kaggle.com/api/v1/datasets/status\n")
LISTING = "ref,title,size\n"
LISTED = LISTING + f"{REF},pivotal-serve-secrets,1\n"
NOT_LISTED = LISTING + "aa0008/other,other,1\n"


def topic_repos(*names: str) -> str:
    return json.dumps([{"nameWithOwner": name} for name in names])


class KaggleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.gh = fakegh.FakeGh(self.root / "bin")
        self.kaggle = fakekaggle.FakeKaggle(self.root / "kbin")
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **overrides: str | None) -> dict[str, str]:
        """The variables piv kaggle reads, plus both fakes. A value of None removes the variable."""
        env = {
            **self.gh.env(),
            **self.kaggle.env(),
            "KAGGLE_USERNAME": USER,
            "NGROK_AUTHTOKEN": TOKEN,
            "PIVOTAL_KAGGLE_URL": URL,
        }
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def piv(self, *args: str, env: dict[str, str] | None = None) -> int:
        return cli.main(["kaggle", *args], self.env() if env is None else env, self.out, self.err)

    def output(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def kinds(self, fake: fakegh.FakeGh) -> list[str]:
        """Each call as its first two words, in order: `secret set`, `datasets create` and so on."""
        return [" ".join(call["args"][:2]) for call in fake.calls()]

    def assert_no_kernel_call(self) -> None:
        """Setup and rotate never push a kernel or start a session."""
        for call in self.kaggle.calls():
            self.assertNotIn("kernels", call["args"], "piv kaggle must not touch a kernel")

    def secret_sets(self) -> dict[str, list[tuple[str, str]]]:
        """Every `gh secret set` as {repo: [(name, value sent on stdin)]}. A value must never be an argument."""
        sets: dict[str, list[tuple[str, str]]] = {}
        for call in self.gh.calls():
            if call["args"][:2] != ["secret", "set"]:
                continue
            for arg in call["args"]:
                self.assertFalse(arg.startswith("--body") or arg == "-b", f"{call['args']} passes a value as a flag")
            repo = call["args"][call["args"].index("--repo") + 1]
            sets.setdefault(repo, []).append((call["args"][2], call["stdin"]))
        return sets

    def uploaded(self, command: str, nth: int = -1) -> dict:
        """The folder kaggle was given for `datasets <command>`, as the fake saw it at the call."""
        calls = [call for call in self.kaggle.calls() if call["args"][:2] == ["datasets", command]]
        self.assertTrue(calls, f"no datasets {command} call")
        folder = calls[nth].get("folder")
        self.assertIsNotNone(folder, f"datasets {command} was given no folder")
        return folder

    def serve_file(self, folder: dict) -> dict:
        return json.loads(folder["files"][serve.SERVE_FILE]["content"])

    def routes_for(self, *, exists: bool, repos: str = "[]") -> None:
        """Answers for a clean run: the dataset's status, the topic's repos, and every write succeeding."""
        self.kaggle.route("datasets status", fakegh.ok("ready\n") if exists else MISSING)
        self.kaggle.route("datasets create", fakegh.ok())
        self.kaggle.route("datasets version", fakegh.ok())
        self.gh.route("repo list", fakegh.ok(repos))
        self.gh.route("secret list", fakegh.ok())
        self.gh.route("secret set", fakegh.ok())


class SetupTest(KaggleTest):
    def test_setup_creates_a_private_dataset_and_sets_the_endpoint_secrets(self):
        self.routes_for(exists=False)
        self.assertEqual(self.piv("setup", REPO), 0, self.err.getvalue())

        self.assertEqual(self.kinds(self.kaggle), ["datasets status", "datasets create"])
        self.assertEqual(self.kinds(self.gh), ["secret list", "secret set", "secret set"])
        create = self.kaggle.calls()[1]["args"]
        self.assertEqual(create[:2], ["datasets", "create"])
        self.assertIn("-q", create)
        self.assertNotIn("-u", create, "the dataset must be private")
        self.assertNotIn("--public", create)
        self.assertEqual(self.kaggle.calls()[0]["args"], ["datasets", "status", REF])
        self.assertEqual(self.gh.calls()[0]["args"], ["secret", "list", "--repo", REPO])

        folder = self.uploaded("create")
        metadata = json.loads(folder["files"]["dataset-metadata.json"]["content"])
        self.assertEqual(metadata["id"], REF)
        serve_file = self.serve_file(folder)
        self.assertEqual(serve_file["endpoint_url"], URL)
        self.assertEqual(serve_file["ngrok_authtoken"], TOKEN)
        key = serve_file["endpoint_key"]
        self.assertRegex(key, KEY_SHAPE)
        for name, file in folder["files"].items():
            self.assertEqual(file["mode"], "0o600", f"{name} must be 0600 from the start")

        sets = self.secret_sets()
        self.assertEqual(sets, {REPO: [(serve.URL_SECRET, URL), (serve.KEY_SECRET, key)]})

    def test_privacy_is_not_claimed_because_no_kaggle_read_confirms_it(self):
        self.routes_for(exists=False)
        self.assertEqual(self.piv("setup", REPO), 0)
        text = self.out.getvalue()
        self.assertIn("created without --public; confirm it shows as private in Kaggle before you rely on it", text)
        self.assertNotIn("created, private", text)
        self.assertIn("next: confirm", text)
        # The only reads the real kaggle offers for a dataset are status, files and list: none reports privacy.
        self.assertEqual(self.kinds(self.kaggle), ["datasets status", "datasets create"])

    def test_the_key_and_the_ngrok_token_never_reach_the_output_or_an_argument(self):
        self.routes_for(exists=False)
        self.assertEqual(self.piv("setup", REPO), 0)
        key = self.serve_file(self.uploaded("create"))["endpoint_key"]

        self.assertNotIn(key, self.output())
        self.assertNotIn(TOKEN, self.output())
        self.assertIn(serve.KEY_SECRET, self.output(), "a line names the key")
        for call in self.gh.calls() + self.kaggle.calls():
            self.assertNotIn(key, " ".join(call["args"]))
            self.assertNotIn(TOKEN, " ".join(call["args"]))
        # The ngrok token goes to the dataset only; no repo secret or gh call carries it.
        self.assertNotIn(TOKEN, json.dumps(self.gh.calls()))

    def test_the_upload_folder_is_removed_afterwards(self):
        self.routes_for(exists=False)
        self.assertEqual(self.piv("setup", REPO), 0)
        self.assertFalse(os.path.exists(self.uploaded("create")["path"]))

    def test_setup_never_pushes_a_kernel_or_reads_a_credential_file(self):
        self.routes_for(exists=False)
        self.piv("setup", REPO)
        self.assert_no_kernel_call()

    def test_an_existing_dataset_is_refused_before_any_write(self):
        self.routes_for(exists=True)
        self.assertEqual(self.piv("setup", REPO), 2)
        self.assertEqual(self.kinds(self.kaggle), ["datasets status"])
        self.assertEqual(self.kinds(self.gh), [])
        self.assertIn("piv kaggle rotate", self.err.getvalue())

    def test_a_status_failure_other_than_not_found_is_raised_and_nothing_is_written(self):
        self.kaggle.route("datasets status", fakegh.fail("kaggle: 401 Unauthorized\n"))
        self.gh.route("secret set", fakegh.ok())
        self.assertEqual(self.piv("setup", REPO), 1)
        self.assertEqual(self.kinds(self.kaggle), ["datasets status"])
        self.assertEqual(self.kinds(self.gh), [])

    def test_a_failed_dataset_create_shows_no_kaggle_detail_and_a_rerun_recovers(self):
        self.kaggle.route("datasets status", MISSING)
        self.kaggle.route(
            "datasets create", fakegh.fail("kaggle: boom, the upload failed\n"), fakegh.ok()
        )
        self.gh.route("secret list", fakegh.ok())
        self.gh.route("secret set", fakegh.ok())
        self.assertEqual(self.piv("setup", REPO), 1)
        self.assertIn("kaggle datasets create failed (exit 1)", self.err.getvalue())
        self.assertNotIn("boom", self.err.getvalue())
        self.assertIn("run piv kaggle setup again", self.err.getvalue())
        first = self.secret_sets()[REPO][1][1]

        self.out, self.err = io.StringIO(), io.StringIO()
        self.assertEqual(self.piv("setup", REPO), 0, self.err.getvalue())
        key = self.serve_file(self.uploaded("create"))["endpoint_key"]
        self.assertNotEqual(key, first, "the re-run makes a new key")
        self.assertEqual(self.secret_sets()[REPO][-1], (serve.KEY_SECRET, key), "the secrets match the dataset")

    def test_a_failed_first_secret_creates_no_dataset_and_a_rerun_recovers(self):
        self.kaggle.route("datasets status", MISSING)
        self.kaggle.route("datasets create", fakegh.ok())
        self.gh.route("secret list", fakegh.ok())
        self.gh.route(f"PIVOTAL_KAGGLE_URL --repo {REPO}", fakegh.fail("gh: HTTP 500\n"), fakegh.ok())
        self.gh.route("secret set", fakegh.ok())
        self.assertEqual(self.piv("setup", REPO, OTHER), 1)
        self.assertEqual(self.kinds(self.kaggle), ["datasets status"], "the dataset comes last")
        self.assertEqual([name for name, _ in self.secret_sets()[REPO]], [serve.URL_SECRET])
        self.assertNotIn(OTHER, self.secret_sets())
        self.assertIn("run piv kaggle setup again", self.err.getvalue())

        self.out, self.err = io.StringIO(), io.StringIO()
        self.assertEqual(self.piv("setup", REPO, OTHER), 0, self.err.getvalue())
        self.assertEqual(self.kinds(self.kaggle)[-1], "datasets create")
        key = self.serve_file(self.uploaded("create"))["endpoint_key"]
        for repo in (REPO, OTHER):
            self.assertEqual(self.secret_sets()[repo][-1], (serve.KEY_SECRET, key))

    def test_a_failed_later_secret_creates_no_dataset(self):
        self.kaggle.route("datasets status", MISSING)
        self.kaggle.route("datasets create", fakegh.ok())
        self.gh.route("secret list", fakegh.ok())
        self.gh.route(f"PIVOTAL_KAGGLE_KEY --repo {OTHER}", fakegh.fail("gh: HTTP 500\n"))
        self.gh.route("secret set", fakegh.ok())
        self.assertEqual(self.piv("setup", REPO, OTHER), 1)
        self.assertEqual(self.kinds(self.kaggle), ["datasets status"])
        self.assertEqual(len(self.secret_sets()[REPO]), 2)
        self.assertIn(f"{OTHER} did not get", self.err.getvalue())

    def test_an_unreachable_repo_is_found_before_the_first_write(self):
        self.routes_for(exists=False)
        self.gh.routes.insert(0, {"match": f"secret list --repo {OTHER}", "answers": [fakegh.fail("gh: HTTP 404\n")]})
        self.gh._save()
        self.assertEqual(self.piv("setup", REPO, OTHER), 1)
        self.assertEqual(self.kinds(self.gh), ["secret list", "secret list"])
        self.assertEqual(self.kinds(self.kaggle), ["datasets status"])
        self.assertIn(OTHER, self.err.getvalue())

    def test_with_no_repo_named_it_sets_the_secrets_on_every_repo_with_the_pivotal_topic(self):
        self.routes_for(exists=False, repos=topic_repos(REPO, OTHER))
        self.assertEqual(self.piv("setup"), 0)
        self.assertEqual(self.kinds(self.gh), ["repo list", "secret list", "secret list"] + ["secret set"] * 4)
        repo_list = self.gh.calls()[0]["args"]
        self.assertEqual(repo_list[:4], ["repo", "list", "--topic", init.TOPIC])
        self.assertEqual(set(self.secret_sets()), {REPO, OTHER})

    def test_an_owner_lists_that_owners_repos(self):
        self.routes_for(exists=False, repos=topic_repos(OTHER))
        self.assertEqual(self.piv("setup", "--owner", "0xctx"), 0, self.err.getvalue())
        self.assertEqual(self.gh.calls()[0]["args"][:5], ["repo", "list", "0xctx", "--topic", init.TOPIC])

    def test_with_no_repo_to_set_it_creates_the_dataset_and_says_so(self):
        self.routes_for(exists=False, repos="[]")
        self.assertEqual(self.piv("setup"), 0)
        self.assertEqual(self.kinds(self.gh), ["repo list"])
        self.assertIn("no repo secrets set", self.out.getvalue())
        self.assertEqual(self.kinds(self.kaggle), ["datasets status", "datasets create"])


class ForbiddenStatusSetupTest(KaggleTest):
    """Setup with a 403 on the status read: the account's own listing decides between an existing dataset and an absent one."""

    def forbidden(self, listing: dict) -> None:
        """The status read answers 403 and the listing answers ``listing``; the other routes of routes_for stay behind."""
        self.kaggle.route("datasets status", FORBIDDEN_STATUS, first=True)
        self.kaggle.route("datasets list", listing, first=True)

    def test_a_403_with_the_dataset_listed_means_it_exists_and_nothing_is_written(self):
        self.routes_for(exists=False)
        self.forbidden(fakegh.ok(LISTED))
        self.assertEqual(self.piv("setup", REPO), 2)
        self.assertIn("already exists", self.err.getvalue())
        self.assertIn("piv kaggle rotate", self.err.getvalue())
        self.assertEqual(self.kinds(self.kaggle), ["datasets status", "datasets list"])
        self.assertEqual(self.secret_sets(), {}, "no secret is set")
        self.assertEqual(self.kinds(self.gh), [])

    def test_a_403_with_the_dataset_not_listed_is_absent_and_setup_proceeds(self):
        self.routes_for(exists=False)
        self.forbidden(fakegh.ok(NOT_LISTED))
        self.assertEqual(self.piv("setup", REPO), 0, self.err.getvalue())
        self.assertEqual(self.kinds(self.kaggle), ["datasets status", "datasets list", "datasets create"])
        key = self.serve_file(self.uploaded("create"))["endpoint_key"]
        self.assertEqual(self.secret_sets(), {REPO: [(serve.URL_SECRET, URL), (serve.KEY_SECRET, key)]})

    def test_a_403_whose_listing_fails_is_an_error_and_nothing_is_written(self):
        self.routes_for(exists=False)
        self.forbidden(fakegh.fail("kaggle: 500 Server Error\n"))
        self.assertEqual(self.piv("setup", REPO), 1)
        self.assertIn("kaggle datasets list failed (exit 1)", self.err.getvalue())
        self.assertEqual(self.kinds(self.kaggle), ["datasets status", "datasets list"], "no dataset is created")
        self.assertEqual(self.secret_sets(), {})
        self.assertEqual(self.kinds(self.gh), [])

    def test_a_kgat_key_reaches_kaggle_as_its_api_token_on_every_call_and_never_gh(self):
        self.routes_for(exists=False)
        self.forbidden(fakegh.ok(NOT_LISTED))
        self.assertEqual(self.piv("setup", REPO, env=self.env(KAGGLE_KEY=KGAT)), 0, self.err.getvalue())
        kaggle_calls = list(zip(self.kaggle.calls(), self.kaggle.environments()))
        self.assertEqual([" ".join(call["args"][:2]) for call, _ in kaggle_calls],
                         ["datasets status", "datasets list", "datasets create"])
        for call, environment in kaggle_calls:
            self.assertEqual(environment.get("KAGGLE_API_TOKEN"), KGAT, f"{call['args'][:2]} must carry the API token")
        for environment in self.gh.environments():
            self.assertNotIn("KAGGLE_API_TOKEN", environment, "gh never gets the Kaggle API token")
            self.assertNotIn(KGAT, json.dumps(environment))


class RefusalTest(KaggleTest):
    def test_bad_input_is_refused_before_any_gh_or_kaggle_call(self):
        cases = {
            "no KAGGLE_USERNAME": (self.env(KAGGLE_USERNAME=None), ["setup", REPO]),
            "a KAGGLE_USERNAME with a space": (self.env(KAGGLE_USERNAME="a da"), ["setup", REPO]),
            "no PIVOTAL_KAGGLE_URL": (self.env(PIVOTAL_KAGGLE_URL=None), ["setup", REPO]),
            "a plain http URL": (self.env(PIVOTAL_KAGGLE_URL="http://pivotal-serve.ngrok-free.app"), ["setup", REPO]),
            "a URL with a path": (self.env(PIVOTAL_KAGGLE_URL=URL + "/v1/models"), ["setup", REPO]),
            "a URL with a newline": (self.env(PIVOTAL_KAGGLE_URL=URL + "\nx"), ["setup", REPO]),
            "no NGROK_AUTHTOKEN": (self.env(NGROK_AUTHTOKEN=None), ["setup", REPO]),
            "an ngrok token with a space": (self.env(NGROK_AUTHTOKEN="tok en-SECRETVALUE"), ["setup", REPO]),
            "a short ngrok token": (self.env(NGROK_AUTHTOKEN="SECRETVALUE"), ["rotate", "--only", REPO]),
            "rotate with a named repo and no --only": (self.env(), ["rotate", REPO]),
            "--only with no repo": (self.env(), ["rotate", "--only"]),
            "an owner that is not a name": (self.env(), ["rotate", "--owner", "a b"]),
            "setup with a repo and an owner": (self.env(), ["setup", REPO, "--owner", "0xctx"]),
            "a repo that is not owner/name": (self.env(), ["setup", "nope"]),
            "a repo with a traversal": (self.env(), ["rotate", "a/../b"]),
        }
        for label, (env, args) in cases.items():
            with self.subTest(label):
                self.out, self.err = io.StringIO(), io.StringIO()
                self.assertEqual(self.piv(*args, env=env), 2, self.err.getvalue())
                self.assertEqual(self.gh.calls(), [])
                self.assertEqual(self.kaggle.calls(), [])
                self.assertNotIn("SECRETVALUE", self.err.getvalue())

    def test_no_subcommand_is_a_usage_error(self):
        self.assertEqual(cli.main(["kaggle"], self.env(), self.out, self.err), 2)
        self.assertEqual(self.gh.calls(), [])
        self.assertEqual(self.kaggle.calls(), [])


class RotateTest(KaggleTest):
    def test_rotate_replaces_the_key_in_the_dataset_and_in_every_repo(self):
        self.routes_for(exists=True, repos=topic_repos(REPO))
        self.assertEqual(self.piv("rotate", "--only", REPO), 0, self.err.getvalue())
        self.assertEqual(self.piv("rotate", "--only", REPO), 0, self.err.getvalue())

        first = self.serve_file(self.uploaded("version", nth=0))["endpoint_key"]
        second = self.serve_file(self.uploaded("version", nth=1))["endpoint_key"]
        self.assertNotEqual(first, second, "each rotation makes a new key")
        self.assertRegex(second, KEY_SHAPE)

        version = self.kaggle.calls()[1]["args"]
        self.assertIn("-d", version, "the old versions are deleted, so the old key leaves Kaggle")
        self.assertIn("-q", version)
        self.assertNotIn("-u", version)
        self.assertNotIn("--public", version)
        self.assertEqual(self.kinds(self.kaggle), ["datasets status", "datasets version"] * 2)
        self.assertEqual(self.serve_file(self.uploaded("version"))["ngrok_authtoken"], TOKEN)

        sets = self.secret_sets()[REPO]
        self.assertEqual(sets, [(serve.URL_SECRET, URL), (serve.KEY_SECRET, first),
                                (serve.URL_SECRET, URL), (serve.KEY_SECRET, second)])
        self.assertNotIn(second, self.output())
        self.assertNotIn(first, self.output())
        self.assertIn("confirm it shows as private in Kaggle", self.out.getvalue())

    def test_rotate_refuses_a_missing_dataset_before_any_write(self):
        self.routes_for(exists=False)
        self.assertEqual(self.piv("rotate"), 2)
        self.assertEqual(self.kinds(self.kaggle), ["datasets status"])
        self.assertEqual(self.kinds(self.gh), [])
        self.assertIn("piv kaggle setup", self.err.getvalue())

    def test_an_unreachable_repo_is_found_before_the_dataset_is_rewritten(self):
        self.routes_for(exists=True, repos=topic_repos(REPO, OTHER))
        self.gh.routes.insert(0, {"match": f"secret list --repo {OTHER}", "answers": [fakegh.fail("gh: HTTP 404\n")]})
        self.gh._save()
        self.assertEqual(self.piv("rotate"), 1)
        self.assertEqual(self.kinds(self.kaggle), ["datasets status"], "no version was written")
        self.assertNotIn("secret set", self.kinds(self.gh))
        self.assertIn(OTHER, self.err.getvalue())

    def test_a_failed_new_version_leaves_the_repos_on_their_key(self):
        self.kaggle.route("datasets status", fakegh.ok("ready\n"))
        self.kaggle.route("datasets version", fakegh.fail("kaggle: upload refused\n"))
        self.gh.route("repo list", fakegh.ok(topic_repos(REPO)))
        self.gh.route("secret list", fakegh.ok())
        self.gh.route("secret set", fakegh.ok())
        self.assertEqual(self.piv("rotate"), 1)
        self.assertNotIn("secret set", self.kinds(self.gh))
        self.assertIn("kaggle datasets version failed (exit 1)", self.err.getvalue())

    def test_the_summary_names_the_updated_the_failed_and_the_repos_left_on_the_old_key(self):
        third = "0xctx/piv-api"
        self.routes_for(exists=True, repos=topic_repos(REPO, OTHER, third))
        self.gh.routes.insert(0, {"match": f"PIVOTAL_KAGGLE_URL --repo {OTHER}", "answers": [fakegh.fail("gh: HTTP 403\n")]})
        self.gh._save()
        self.assertEqual(self.piv("rotate", "--only", REPO, OTHER), 1)
        out = self.out.getvalue()
        self.assertIn(f"{REPO}: secrets {serve.URL_SECRET} and {serve.KEY_SECRET} set", out)
        self.assertIn(f"dataset has the new key; these repos still hold the OLD key: {OTHER}, {third}", out)
        self.assertNotIn(f"OLD key: {REPO}", out)
        self.assertIn("rotate again", self.err.getvalue())

    def test_a_subset_leaves_the_others_listed_as_still_on_the_old_key(self):
        self.routes_for(exists=True, repos=topic_repos(REPO, OTHER))
        self.assertEqual(self.piv("rotate", "--only", REPO), 0, self.err.getvalue())
        self.assertIn(f"these repos still hold the OLD key: {OTHER}", self.out.getvalue())

    def test_a_full_rotation_says_every_known_repo_holds_the_new_key(self):
        self.routes_for(exists=True, repos=topic_repos(REPO, OTHER))
        self.assertEqual(self.piv("rotate"), 0, self.err.getvalue())
        self.assertNotIn("OLD key", self.out.getvalue())
        self.assertIn("all 2 repo(s)", self.out.getvalue())

    def test_the_default_listing_says_it_covers_only_the_signed_in_user_and_an_owner_widens_it(self):
        self.routes_for(exists=True, repos=topic_repos(REPO))
        self.assertEqual(self.piv("rotate"), 0)
        self.assertIn("only the repos of the signed-in GitHub user", self.out.getvalue())
        self.assertIn("--owner", self.out.getvalue())

        self.out = io.StringIO()
        self.assertEqual(self.piv("rotate", "--owner", "0xctx", "--owner", "acme"), 0, self.err.getvalue())
        self.assertNotIn("only the repos of the signed-in", self.out.getvalue())
        lists = [c["args"] for c in self.gh.calls() if c["args"][:2] == ["repo", "list"]]
        self.assertEqual([a[2] for a in lists[-2:]], ["0xctx", "acme"])

    def test_rotate_needs_the_ngrok_token_and_the_url_every_time(self):
        for name in ("NGROK_AUTHTOKEN", "PIVOTAL_KAGGLE_URL"):
            with self.subTest(name):
                self.assertEqual(self.piv("rotate", env=self.env(**{name: None})), 2)
        self.assertEqual(self.kaggle.calls(), [])


class ReadOnlyHelpersTest(KaggleTest):
    def test_the_repo_list_is_one_read(self):
        self.gh.route("repo list", fakegh.ok(topic_repos(REPO)))
        gh = Gh(str(self.gh.path), self.env())
        self.assertEqual(serve.managed_repos(gh), [REPO])
        self.assertEqual(self.kinds(self.gh), ["repo list"])

    def test_the_reachability_check_only_lists_secrets(self):
        self.gh.route("secret list", fakegh.ok())
        gh = Gh(str(self.gh.path), self.env())
        serve._reachable(gh, [REPO, OTHER])
        self.assertEqual(self.kinds(self.gh), ["secret list", "secret list"])

    def test_the_dataset_check_is_one_read(self):
        self.kaggle.route("datasets status", MISSING)
        kaggle = serve.Kaggle(str(self.kaggle.path), self.env())
        self.assertFalse(serve.dataset_exists(kaggle, REF))
        self.assertEqual(self.kinds(self.kaggle), ["datasets status"])

    def test_a_404_not_found_is_absent_and_every_other_status_message_raises(self):
        kaggle = serve.Kaggle(str(self.kaggle.path), self.env())
        for message in ("kaggle: 404\n", "kaggle: Not Found\n", "kaggle: 500 Server Error\n",
                        "kaggle: 401 Unauthorized\n", "kaggle: connection reset\n", "\n"):
            with self.subTest(message):
                self.kaggle.routes.clear()
                self.kaggle.route("datasets status", fakegh.fail(message))
                self.kaggle._save()
                with self.assertRaises(PivError):
                    serve.dataset_exists(kaggle, REF)
        self.kaggle.routes.clear()
        self.kaggle.route("datasets status", fakegh.fail("404 Client Error: Not Found for url: https://x\n"))
        self.kaggle._save()
        self.assertFalse(serve.dataset_exists(kaggle, REF))

    def test_a_403_is_absent_only_when_the_accounts_own_listing_lacks_the_dataset(self):
        # Measured live: kaggle 2.2.4 answers 403 Forbidden, not 404, for a dataset that does not exist.
        kaggle = serve.Kaggle(str(self.kaggle.path), self.env())
        forbidden = fakegh.fail("403 Client Error: Forbidden for url: https://x\n")
        header = "ref,title,size\n"
        cases = (
            (header + "aa0008/other,other,1\n", False),
            (header + f"{REF},pivotal-serve-secrets,1\n", True),
            (header + f"x{REF},x,1\n", False),
        )
        for listing, exists in cases:
            with self.subTest(listing):
                self.kaggle.routes.clear()
                self.kaggle.route("datasets status", forbidden)
                self.kaggle.route("datasets list", fakegh.ok(listing))
                self.kaggle._save()
                self.assertIs(serve.dataset_exists(kaggle, REF), exists)
        self.kaggle.routes.clear()
        self.kaggle.route("datasets status", forbidden)
        self.kaggle.route("datasets list", fakegh.fail("kaggle: 500 Server Error\n"))
        self.kaggle._save()
        with self.assertRaises(PivError):
            serve.dataset_exists(kaggle, REF)

    def test_a_non_403_failure_that_says_forbidden_is_raised_even_with_an_empty_listing(self):
        # The anchor is the status 403, not the word: a 401 Forbidden is not the answer for a missing dataset, so no
        # listing is read to decide it, and an empty listing must not make it absent.
        kaggle = serve.Kaggle(str(self.kaggle.path), self.env())
        for message in ("kaggle: 401 Forbidden\n", "kaggle: 500 Forbidden\n", "kaggle: Forbidden\n"):
            with self.subTest(message):
                self.kaggle.routes.clear()
                self.kaggle.route("datasets status", fakegh.fail(message))
                self.kaggle.route("datasets list", fakegh.ok(""))
                self.kaggle._save()
                with self.assertRaises(PivError):
                    serve.dataset_exists(kaggle, REF)

    def test_a_repo_list_with_a_bad_name_is_refused(self):
        self.gh.route("repo list", fakegh.ok(topic_repos("bad name")))
        gh = Gh(str(self.gh.path), self.env())
        with self.assertRaises(PivError):
            serve.managed_repos(gh)

    def test_a_full_repo_list_is_refused_and_one_short_of_it_is_taken(self):
        limit = serve.REPO_LIMIT
        gh = Gh(str(self.gh.path), self.env())
        self.gh.route("repo list", fakegh.ok(topic_repos(*[f"o/r{n}" for n in range(limit)])))
        with self.assertRaises(PivError):
            serve.managed_repos(gh)
        self.gh.routes.clear()
        self.gh.route("repo list", fakegh.ok(topic_repos(*[f"o/r{n}" for n in range(limit - 1)])))
        self.gh._save()
        self.assertEqual(len(serve.managed_repos(gh)), limit - 1)


class FakeKaggleTest(KaggleTest):
    def test_the_fake_refuses_a_dir_mode_the_real_kaggle_lacks(self):
        import subprocess
        self.kaggle.route("datasets create", fakegh.ok())
        for mode, code in (("skip", 0), ("zip", 0), ("tar", 0), ("gzip", 2)):
            for flag in ("-r", "--dir-mode"):
                with self.subTest(mode=mode, flag=flag):
                    done = subprocess.run([str(self.kaggle.path), "datasets", "create", flag, mode],
                                          capture_output=True, check=False)
                    self.assertEqual(done.returncode, code, done.stderr)


class EnvironmentTest(unittest.TestCase):
    def test_the_secret_names_are_the_ones_init_declares(self):
        self.assertIn(serve.URL_SECRET, init.SECRETS)
        self.assertIn(serve.KEY_SECRET, init.SECRETS)
        self.assertIn(serve.KAGGLE_USERNAME, init.SECRETS)
        self.assertIn(serve.KAGGLE_KEY, init.SECRETS)

    def test_kaggle_gets_only_path_home_and_its_own_variables(self):
        env = {
            "PATH": "/usr/bin", "HOME": "/h", "KAGGLE_USERNAME": USER, "KAGGLE_KEY": "k", "KAGGLE_CONFIG_DIR": "/c",
            serve.NGROK_TOKEN: TOKEN, serve.URL_SECRET: URL, serve.KEY_SECRET: "key",
            "CLAUDE_CODE_OAUTH_TOKEN": "c", "GH_TOKEN": "g", "GITHUB_TOKEN": "g", "XAI_API_KEY": "x",
        }
        self.assertEqual(serve.kaggle_environment(env), {
            "PATH": "/usr/bin", "HOME": "/h", "KAGGLE_USERNAME": USER, "KAGGLE_KEY": "k", "KAGGLE_CONFIG_DIR": "/c",
        })

    def test_gh_gets_no_ngrok_token_and_none_of_the_secrets(self):
        env = {"PATH": "/usr/bin", "GH_TOKEN": "g", serve.NGROK_TOKEN: TOKEN, serve.KEY_SECRET: "key",
               "KAGGLE_KEY": "k", "KAGGLE_API_TOKEN": KGAT}
        self.assertEqual(serve.gh_environment(env), {"PATH": "/usr/bin", "GH_TOKEN": "g"})

    def test_a_kgat_key_is_the_api_token_in_both_packages_and_a_legacy_key_is_not(self):
        # The two packages keep separate environments (engine/modelsup.py and gh-piv/piv/serve.py), so this one test
        # builds both for the same key and holds them to the same rule: a KGAT_ key is also KAGGLE_API_TOKEN.
        base = {"PATH": "/usr/bin", "HOME": "/h"}
        for key, passed in ((KGAT, True), ("legacy-account-key", False)):
            with self.subTest(key=key):
                piv_env = serve.kaggle_environment({**base, "KAGGLE_USERNAME": USER, "KAGGLE_KEY": key})
                engine_env = modelsup.Kaggle("kaggle", base, USER, key).env
                self.assertEqual(piv_env.get("KAGGLE_KEY"), key)
                self.assertEqual(engine_env.get("KAGGLE_KEY"), key)
                self.assertEqual("KAGGLE_API_TOKEN" in piv_env, passed)
                self.assertEqual("KAGGLE_API_TOKEN" in engine_env, passed)
                if passed:
                    self.assertEqual(piv_env["KAGGLE_API_TOKEN"], engine_env["KAGGLE_API_TOKEN"])


if __name__ == "__main__":
    unittest.main()
