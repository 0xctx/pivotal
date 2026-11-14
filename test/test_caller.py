"""The caller template, read as a file: triggers, jobs and secret wiring.

The engine is stdlib-only and so are the tests, so ``parse`` reads the YAML subset the template
uses: mappings, block sequences, flow lists, plain and quoted scalars, ``{}``, and folded or
literal block scalars. Anything else raises, so a construct this parser cannot read fails the
test rather than passing unread.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import trust

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh-piv"))
from piv import init  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CALLER = ROOT / "templates" / "caller.yml"

# Every secret Pivotal uses. Only factory-trusted receives them all.
PIVOTAL_SECRETS = {
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "PIVOTAL_KAGGLE_URL",
    "PIVOTAL_KAGGLE_KEY",
    "KAGGLE_USERNAME",
    "KAGGLE_KEY",
}
# The free keys and the Kaggle endpoint, the only secrets an untrusted run may hold (F2b).
UNTRUSTED_SECRETS = {"GEMINI_API_KEY", "GROQ_API_KEY", "PIVOTAL_KAGGLE_URL", "PIVOTAL_KAGGLE_KEY"}
FACTORY_JOBS = {"factory-trusted", "factory-untrusted"}
PIN = "0" * 40  # placeholder release SHA; piv init writes the real one (T12)

KEY = re.compile(r"([A-Za-z_][\w-]*):(?:\s+(.*))?$")

# A stand-in for gh on the stop job's path. It logs each call with the GH_REPO it saw. For ``api`` it
# answers the runs of the status in the path, filtered by the real --jq expression through jq, and it
# fails the listing or the cancel that the environment names. Anything else exits 2.
FAKE_GH = """\
import json
import os
import re
import subprocess
import sys

with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"args": sys.argv[1:], "repo": os.environ.get("GH_REPO")}) + "\\n")
args = sys.argv[1:]
if args[:2] == ["pr", "list"]:
    if os.environ.get("FAKE_GH_BROKEN_PRS"):
        sys.stderr.write("gh: Bad Gateway (HTTP 502)\\n")
        sys.exit(1)
    sys.stdout.write("".join(n + "\\n" for n in os.environ["FAKE_GH_STACK_PRS"].split(",") if n))
elif args[:1] == ["api"]:
    path = next(arg for arg in args if arg.startswith("repos/"))
    status = re.search(r"status=(\\w+)", path).group(1)
    if status in os.environ["FAKE_GH_BROKEN"].split(","):
        # FAKE_GH_FAIL_TIMES bounds the failures per status (a transient fault); unset, the status fails every time.
        times = os.environ.get("FAKE_GH_FAIL_TIMES")
        counter = os.environ["FAKE_GH_LOG"] + "." + status
        seen = int(open(counter).read()) if os.path.exists(counter) else 0
        with open(counter, "w") as fh:
            fh.write(str(seen + 1))
        if times is None or seen < int(times):
            sys.stderr.write("gh: Bad Gateway (HTTP 502)\\n")
            sys.exit(1)
    pages = json.loads(os.environ["FAKE_GH_PAGES"])
    page = {"workflow_runs": [{"id": run, "display_title": title} for run, title in pages.get(status, [])]}
    jq = args[args.index("--jq") + 1]
    done = subprocess.run(["jq", "-r", jq], input=json.dumps(page), capture_output=True, text=True, check=True)
    sys.stdout.write(done.stdout)
elif args[:2] == ["run", "cancel"]:
    if args[2] in os.environ["FAKE_GH_FAIL_CANCEL"].split(","):
        sys.stderr.write("gh: run is not cancellable (HTTP 409)\\n")
        sys.exit(1)
else:
    sys.exit(2)
"""


def _strip_comment(text):
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _scalar(text):
    if text == "{}":
        return {}
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(item.strip()) for item in text[1:-1].split(",") if item.strip()]
    return text


class _Rows:
    def __init__(self, text):
        self.rows = []  # (indent, text) with comments and blank lines dropped
        for raw in text.splitlines():
            body = raw.strip()
            if body and not body.startswith("#"):
                self.rows.append((len(raw) - len(raw.lstrip(" ")), _strip_comment(body)))
        self.i = 0

    def peek(self):
        return self.rows[self.i] if self.i < len(self.rows) else None


def _block(rows, indent):
    head = rows.peek()
    if head is None or head[0] != indent:
        raise ValueError(f"expected a block at indent {indent}")
    if head[1] == "-" or head[1].startswith("- "):
        return _sequence(rows, indent)
    return _mapping(rows, indent)


def _mapping(rows, indent):
    out = {}
    while (head := rows.peek()) is not None and head[0] == indent:
        match = KEY.match(head[1])
        if match is None:
            raise ValueError(f"not a mapping key: {head[1]!r}")
        rows.i += 1
        out[match.group(1)] = _value(rows, indent, match.group(2) or "")
    if (head := rows.peek()) is not None and head[0] > indent:
        raise ValueError(f"unexpected indent before {head[1]!r}")
    return out


def _value(rows, indent, rest):
    if rest in (">-", ">", "|", "|-"):
        parts = []
        while (head := rows.peek()) is not None and head[0] > indent:
            parts.append(head[1])
            rows.i += 1
        text = (" " if rest.startswith(">") else "\n").join(parts)
        return text if rest.endswith("-") else text + "\n"  # "-" strips the final newline
    if rest:
        return _scalar(rest)
    head = rows.peek()
    if head is not None and head[0] > indent:
        return _block(rows, head[0])
    return None


def _sequence(rows, indent):
    items = []
    while (head := rows.peek()) is not None and head[0] == indent and head[1].startswith("-"):
        content = head[1][1:]
        text = content.lstrip()
        if not text:
            raise ValueError("unsupported empty list item")
        if KEY.match(text):
            # A mapping item: its first key sits in the column after "- ", and the rest follow it.
            column = indent + 1 + len(content) - len(text)
            rows.rows[rows.i] = (column, text)
            items.append(_mapping(rows, column))
        else:
            items.append(_scalar(text))
            rows.i += 1
    return items


def parse(text):
    rows = _Rows(text)
    if not rows.rows:
        raise ValueError("empty document")
    doc = _block(rows, rows.rows[0][0])
    if rows.peek() is not None:
        raise ValueError(f"unparsed line {rows.peek()[1]!r}")
    return doc


def _walk(node):
    """Every (key, value) pair in a parsed document, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _secret_refs(node):
    return set(re.findall(r"secrets\.(\w+)", json.dumps(node)))


class ParserTest(unittest.TestCase):
    def test_reads_the_subset_the_template_uses(self):
        doc = parse(
            "a: 1\n"
            "b:\n"
            "  c: [x, y]\n"
            "  d:\n"
            "    - name: first  # trailing comment\n"
            "      run: |\n"
            "        echo hi\n"
            "    - plain\n"
            "  e: >-\n"
            "    one\n"
            "    two\n"
            "  f: {}\n"
            "  g: \"pivotal #x\"\n"
        )
        self.assertEqual(doc["a"], "1")
        self.assertEqual(doc["b"]["c"], ["x", "y"])
        self.assertEqual(doc["b"]["d"], [{"name": "first", "run": "echo hi\n"}, "plain"])
        self.assertEqual(doc["b"]["e"], "one two")  # folded, stripped
        self.assertEqual(parse("k: |\n  a\n  b\n")["k"], "a\nb\n")  # literal, clipped
        self.assertEqual(doc["b"]["f"], {})
        self.assertEqual(doc["b"]["g"], "pivotal #x")

    def test_fails_loudly_on_a_construct_it_cannot_read(self):
        with self.assertRaises(ValueError):
            parse("a:\n  b: 1\n c: 2\n")


@unittest.skipUnless(shutil.which("jq"), "the stop step filters runs with jq, as gh does")
class StopRunTest(unittest.TestCase):
    """Runs the stop step's script under bash, with the fake gh above (F6)."""

    REPO = "you/pivotal-demo"
    WORKFLOW_REF = "you/pivotal-demo/.github/workflows/pivotal.yml@refs/heads/main"
    # run id and display title per status: run 900 is this run; 104 and 105 belong to issue 8; 107 is titled with the number of
    # issue 7's stack pull request (12), as a land commented on that pull request is.
    PAGES = {
        "queued": [[101, "pivotal-issue-7"], [102, "pivotal-issue-7"], [105, "pivotal-issue-8"], [106, "pivotal-issue-70"]],
        "in_progress": [[900, "pivotal-issue-7"], [103, "pivotal-issue-7"], [104, "pivotal-issue-8"], [107, "pivotal-issue-12"]],
    }
    VALUES = {
        "${{ github.token }}": "token",
        "${{ github.repository }}": REPO,
        "${{ github.workflow_ref }}": WORKFLOW_REF,
        "${{ needs.trust.outputs.issue }}": "7",
        "${{ github.run_id }}": "900",
    }

    def setUp(self):
        self._tmp = None
        self.fresh()
        self.addCleanup(lambda: self._tmp.cleanup())

    def fresh(self):
        """A new scratch directory with its fake gh; the previous one is removed, so a subtest loop leaks none."""
        if self._tmp is not None:
            self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        gh = self.bin_dir / "gh"
        gh.write_text("#!" + sys.executable + "\n" + FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        self.log = self.root / "gh.jsonl"

    def run_stop(self, *, fail_cancel=(), broken=(), fail_times=None, stack_prs="12", broken_prs=False):
        step = parse(CALLER.read_text(encoding="utf-8"))["jobs"]["stop"]["steps"][0]
        env = {key: self.VALUES[value] for key, value in step["env"].items()}
        env.update(
            PATH=f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            FAKE_GH_LOG=str(self.log),
            FAKE_GH_PAGES=json.dumps(self.PAGES),
            FAKE_GH_BROKEN=",".join(broken),
            FAKE_GH_STACK_PRS=stack_prs,
            FAKE_GH_BROKEN_PRS="1" if broken_prs else "",
            FAKE_GH_FAIL_CANCEL=",".join(fail_cancel),
            LISTING_RETRY_SLEEP="0",  # the step's retry sleeps this long; the test does not wait
        )
        if fail_times is not None:
            env["FAKE_GH_FAIL_TIMES"] = str(fail_times)
        script = self.root / "stop.sh"
        script.write_text(step["run"], encoding="utf-8")
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", str(script)],  # the step sets pipefail itself
           
            env=env, capture_output=True, text=True, check=False,
        )
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return done, calls

    @staticmethod
    def cancelled(calls):
        return [call["args"][2] for call in calls if call["args"][:2] == ["run", "cancel"]]

    def test_every_gh_call_runs_against_this_repository(self):
        done, calls = self.run_stop()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(calls)
        for call in calls:
            with self.subTest(args=call["args"]):
                self.assertEqual(call["repo"], self.REPO)

    def test_runs_are_listed_for_the_workflow_file_that_is_running(self):
        done, calls = self.run_stop()
        self.assertEqual(done.returncode, 0, done.stderr)
        paths = [arg for call in calls if call["args"][:1] == ["api"] for arg in call["args"] if arg.startswith("repos/")]
        self.assertEqual(len(paths), 5)  # queued, in_progress, waiting, requested, pending
        for path in paths:
            with self.subTest(path=path):
                self.assertRegex(path, r"^repos/you/pivotal-demo/actions/workflows/pivotal\.yml/runs\?status=\w+&per_page=100$")

    def test_cancels_only_this_issues_other_runs(self):
        done, calls = self.run_stop()
        self.assertEqual(done.returncode, 0, done.stderr)
        # not 900 (this run), not issue 8 or 70; 107 is titled with the stack pull request's number (I4)
        self.assertEqual(self.cancelled(calls), ["101", "102", "103", "107"])

    def test_without_a_stack_pull_request_only_the_issue_titled_runs_are_cancelled(self):
        done, calls = self.run_stop(stack_prs="")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.cancelled(calls), ["101", "102", "103"])

    def test_the_stack_pull_requests_are_looked_up_by_the_issues_stack_branch(self):
        done, calls = self.run_stop()
        (lookup,) = [c["args"] for c in calls if c["args"][:2] == ["pr", "list"]]
        self.assertIn("pivotal/issue-7/stack", lookup)

    def test_a_failed_pull_request_lookup_still_cancels_the_issue_titled_runs_and_then_fails_the_step(self):
        """R2: one 502 on the lookup must not leave the issue's own runs running: they are cancelled, then the step is red."""
        done, calls = self.run_stop(broken_prs=True)
        self.assertEqual(self.cancelled(calls), ["101", "102", "103"], "the runs titled with the issue; 107 needs the lookup")
        self.assertNotEqual(done.returncode, 0, "the step is red: a stop that could not look up the stack pull request is not clean")
        self.assertIn("502", done.stderr)

    def test_a_failed_cancel_does_not_stop_the_later_cancels(self):
        done, calls = self.run_stop(fail_cancel=["101"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.cancelled(calls), ["101", "102", "103", "107"])
        self.assertIn("could not cancel run 101", done.stderr)

    def test_a_transient_failure_of_one_listing_is_retried_and_every_run_is_cancelled(self):
        """R4: a 5xx that clears within three attempts leaves no run running and the step green."""
        for broken in ("queued", "in_progress"):
            for fail_times in (1, 2):
                with self.subTest(broken=broken, fail_times=fail_times):
                    self.fresh()
                    done, calls = self.run_stop(broken=[broken], fail_times=fail_times)
                    self.assertEqual(sorted(self.cancelled(calls)), ["101", "102", "103", "107"])
                    self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_persistent_failure_of_one_listing_is_tried_three_times_then_the_other_statuses_are_cancelled_and_the_step_fails(self):
        """R4: three attempts, then the status counts as failed: the later statuses are still listed and the step is red."""
        for broken, expected in (("queued", ["103", "107"]), ("in_progress", ["101", "102"])):
            with self.subTest(broken=broken):
                self.fresh()
                done, calls = self.run_stop(broken=[broken])
                self.assertEqual(self.cancelled(calls), expected)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("502", done.stderr)
                listed = [a for c in calls if c["args"][:1] == ["api"] for a in c["args"] if a.startswith("repos/")]
                self.assertEqual(len(listed), 7, "three attempts on the broken status, one on each of the other four")
                self.assertEqual(len([p for p in listed if f"status={broken}&" in p]), 3)


class ReusableContractTest(unittest.TestCase):
    """The caller's with: and secrets: must match what factory.yml and land.yml declare (R2)."""

    @classmethod
    def setUpClass(cls):
        cls.jobs = parse(CALLER.read_text(encoding="utf-8"))["jobs"]

    @staticmethod
    def declared(workflow):
        call = parse((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))["on"]["workflow_call"]
        return call.get("inputs", {}), call.get("secrets", {})

    def test_the_caller_passes_only_declared_inputs_and_secrets(self):
        for workflow, job_ids in (("factory.yml", FACTORY_JOBS), ("land.yml", {"land"})):
            inputs, secrets = self.declared(workflow)
            for job_id in job_ids:
                with self.subTest(workflow=workflow, job=job_id):
                    job = self.jobs[job_id]
                    self.assertLessEqual(set(job.get("with", {})), set(inputs))
                    self.assertLessEqual(set(job.get("secrets", {})), set(secrets))

    def test_every_required_input_and_secret_is_supplied(self):
        for workflow, job_ids in (("factory.yml", FACTORY_JOBS), ("land.yml", {"land"})):
            inputs, secrets = self.declared(workflow)
            required_inputs = {name for name, spec in inputs.items() if spec.get("required") == "true"}
            required_secrets = {name for name, spec in secrets.items() if spec.get("required") == "true"}
            for job_id in job_ids:
                with self.subTest(workflow=workflow, job=job_id):
                    job = self.jobs[job_id]
                    self.assertLessEqual(required_inputs, set(job.get("with", {})))
                    self.assertLessEqual(required_secrets, set(job.get("secrets", {})))

    def test_factory_types_trusted_as_a_boolean_and_declares_every_pivotal_secret(self):
        inputs, secrets = self.declared("factory.yml")
        self.assertEqual(inputs["trusted"]["type"], "boolean")
        self.assertEqual(set(inputs), {"trusted", "kind", "issue", "issue_pin", "engine_sha", "engine_repo", "preset"})
        # Each caller passes a subset (factory-untrusted only four), so no secret is required.
        self.assertEqual(set(secrets), PIVOTAL_SECRETS)
        self.assertFalse([name for name, spec in secrets.items() if spec.get("required") == "true"])

    def test_land_declares_the_issue_it_is_given(self):
        inputs, secrets = self.declared("land.yml")
        self.assertEqual(set(inputs), {"issue", "engine_sha", "engine_repo"})
        self.assertEqual(inputs["issue"]["required"], "true")
        self.assertEqual(inputs["engine_sha"]["required"], "true")
        self.assertEqual(secrets, {})


class CallerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CALLER.read_text(encoding="utf-8")
        cls.doc = parse(cls.text)
        cls.jobs = cls.doc["jobs"]

    def steps(self, job_id):
        return self.jobs[job_id].get("steps", [])

    def test_jobs_are_the_planned_five(self):
        self.assertEqual(
            set(self.jobs),
            {"trust", "factory-trusted", "factory-untrusted", "land", "stop"},
        )

    def test_the_template_holds_the_engine_placeholder_and_no_literal(self):
        self.assertNotIn(init.LEGACY_ENGINE, self.text)
        self.assertIn(init.ENGINE_PLACEHOLDER, self.text)
        self.assertEqual(self.jobs["trust"]["steps"][0]["with"]["repository"], init.ENGINE_PLACEHOLDER)

    def test_every_reusable_workflow_gets_the_engine_repo_input(self):
        for job_id in (*FACTORY_JOBS, "land"):
            with self.subTest(job=job_id):
                self.assertEqual(self.jobs[job_id]["with"]["engine_repo"], init.ENGINE_PLACEHOLDER)

    def test_no_workflow_file_or_the_template_holds_the_old_literal(self):
        for name in ("factory.yml", "land.yml"):
            with self.subTest(file=name):
                self.assertNotIn(init.LEGACY_ENGINE, (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
        self.assertNotIn(init.LEGACY_ENGINE, self.text)

    def test_triggers_are_the_planned_ones(self):
        on = self.doc["on"]
        self.assertEqual(
            set(on), {"issues", "issue_comment", "repository_dispatch", "workflow_dispatch", "workflow_run"}
        )
        self.assertEqual(on["issues"]["types"], ["labeled"])
        self.assertEqual(on["issue_comment"]["types"], ["created"])
        self.assertEqual(on["repository_dispatch"]["types"], ["pivotal-event", "pivotal-fix"])
        self.assertEqual(on["workflow_run"]["types"], ["completed"])
        self.assertTrue(on["workflow_run"]["workflows"], "workflow_run names no CI workflow")

    def test_workflow_run_acts_only_for_a_default_branch_push_failure(self):
        condition = self.jobs["trust"]["if"]
        self.assertTrue(condition.startswith("github.event_name != 'workflow_run' ||"))
        for conjunct in (
            "github.event.workflow_run.event == 'push'",
            "github.event.workflow_run.head_repository.full_name == github.repository",
            "github.event.workflow_run.head_branch == github.event.repository.default_branch",
            "github.event.workflow_run.conclusion == 'failure'",
        ):
            with self.subTest(conjunct=conjunct):
                self.assertIn(conjunct, condition)

    def test_no_job_builds_from_head_sha(self):
        self.assertNotIn("head_sha", self.text)

    def test_every_job_after_trust_needs_trust(self):
        for job_id in self.jobs:
            if job_id != "trust":
                with self.subTest(job=job_id):
                    self.assertEqual(self.jobs[job_id]["needs"], "trust")

    def test_default_permissions_are_empty(self):
        self.assertEqual(self.doc["permissions"], {})

    def test_trust_has_no_secrets_and_reads_only(self):
        trust_job = self.jobs["trust"]
        # pull-requests: read is the one addition, for a land comment's pull request (I4); it is read, never write.
        self.assertEqual(trust_job["permissions"], {"contents": "read", "issues": "read", "pull-requests": "read"})
        self.assertEqual(_secret_refs(trust_job), set())
        self.assertNotIn("secrets", trust_job)

    def test_trust_runs_the_reviewed_engine_and_exports_its_outputs(self):
        self.assertEqual(set(self.jobs["trust"]["outputs"]), {"trusted", "issue", "kind", "pin"})
        checkout, decide = self.steps("trust")
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["repository"], init.ENGINE_PLACEHOLDER)
        self.assertEqual(checkout["with"]["ref"], PIN)
        self.assertIn("python3 -I pivotal/engine/trust.py >> \"$GITHUB_OUTPUT\"", decide["run"])

    def test_factory_trusted_gets_every_pivotal_secret(self):
        self.assertEqual(_secret_refs(self.jobs["factory-trusted"]), PIVOTAL_SECRETS)

    def test_factory_untrusted_gets_only_the_free_secrets(self):
        refs = _secret_refs(self.jobs["factory-untrusted"])
        self.assertEqual(refs, UNTRUSTED_SECRETS)
        self.assertFalse(refs - PIVOTAL_SECRETS)

    def test_factory_untrusted_passes_exactly_the_four_untrusted_secrets(self):
        # A guard: this holds already, and it pins the exact name-to-secret mapping, not only the set.
        secrets = self.jobs["factory-untrusted"]["secrets"]
        self.assertEqual(set(secrets), {"GEMINI_API_KEY", "GROQ_API_KEY", "PIVOTAL_KAGGLE_URL", "PIVOTAL_KAGGLE_KEY"})
        for name, value in secrets.items():
            with self.subTest(secret=name):
                self.assertEqual(value, f"${{{{ secrets.{name} }}}}")

    def test_land_and_stop_get_no_secrets(self):
        self.assertEqual(_secret_refs(self.jobs["land"]), set())
        self.assertEqual(_secret_refs(self.jobs["stop"]), set())

    def test_secrets_beyond_the_untrusted_set_reach_only_factory_trusted(self):
        trusted_only = PIVOTAL_SECRETS - UNTRUSTED_SECRETS
        for job_id, job in self.jobs.items():
            with self.subTest(job=job_id):
                refs = _secret_refs(job)
                if job_id == "factory-trusted":
                    self.assertTrue(refs >= trusted_only)
                else:
                    self.assertFalse(refs & trusted_only)

    def test_no_job_inherits_every_secret(self):
        for job_id, job in self.jobs.items():
            with self.subTest(job=job_id):
                self.assertNotEqual(job.get("secrets"), "inherit")

    def test_each_factory_job_passes_its_secrets_as_a_mapping(self):
        for job_id in FACTORY_JOBS:
            with self.subTest(job=job_id):
                self.assertIsInstance(self.jobs[job_id]["secrets"], dict)

    def test_factory_trusted_runs_on_the_trusted_go_fix_event_and_incident_kinds(self):
        self.assertEqual(
            self.jobs["factory-trusted"]["if"],
            "needs.trust.outputs.trusted == 'true' && "
            "contains(fromJSON('[\"go\",\"fix\",\"event\",\"incident\"]'), needs.trust.outputs.kind)",
        )

    def test_factory_untrusted_runs_only_for_an_untrusted_go(self):
        self.assertEqual(
            self.jobs["factory-untrusted"]["if"],
            "needs.trust.outputs.trusted != 'true' && needs.trust.outputs.kind == 'go'",
        )

    def test_land_and_stop_run_only_for_a_trusted_trigger(self):
        self.assertEqual(
            self.jobs["land"]["if"],
            "needs.trust.outputs.trusted == 'true' && needs.trust.outputs.kind == 'land'",
        )
        self.assertEqual(
            self.jobs["stop"]["if"],
            "needs.trust.outputs.trusted == 'true' && needs.trust.outputs.kind == 'stop'",
        )

    def test_kind_literals_are_known_kinds(self):
        literals = set(re.findall(r"kind\s*==\s*'([^']*)'", self.text))
        for listed in re.findall(r"fromJSON\('\[([^\]]*)\]'\)", self.text):
            literals |= {item.strip().strip('"') for item in listed.split(",")}
        self.assertTrue(literals, "no kind literal found")
        self.assertLessEqual(literals, trust.KINDS)

    def test_concurrency_is_per_issue_and_queued_on_the_work_jobs(self):
        for job_id in ("factory-trusted", "factory-untrusted", "land"):
            with self.subTest(job=job_id):
                self.assertEqual(
                    self.jobs[job_id]["concurrency"],
                    {"group": "pivotal-issue-${{ needs.trust.outputs.issue }}", "queue": "max"},
                )

    def test_trust_and_stop_take_no_concurrency_group(self):
        # stop must not queue behind the run it cancels, so no group is set for the whole workflow.
        self.assertNotIn("concurrency", self.doc)
        for job_id in ("trust", "stop"):
            with self.subTest(job=job_id):
                self.assertNotIn("concurrency", self.jobs[job_id])

    def test_ci_lints_the_caller_with_actionlint_and_zizmor(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertRegex(ci, r"actionlint -ignore 'unexpected key \"queue\"[^\n]*templates/caller\.yml")
        self.assertRegex(ci, r"zizmor [^\n]*templates/caller\.yml")

    def test_ci_still_lints_every_workflow_by_discovery(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        lines = [line.strip() for line in ci.splitlines() if line.strip().startswith("actionlint")]
        # one run names no file, so actionlint discovers every workflow under .github/workflows (factory.yml included)
        self.assertIn("actionlint -ignore 'unexpected key \"queue\" for \"concurrency\" section'", lines)
        self.assertNotIn("actionlint .github/workflows/ci.yml", lines)

    def test_the_factory_receives_the_trust_decision_and_the_pin(self):
        for job_id in FACTORY_JOBS:
            with self.subTest(job=job_id):
                with_ = self.jobs[job_id]["with"]
                # A boolean expression, so the factory's typed trusted input gets a real boolean.
                self.assertEqual(with_["trusted"], "${{ needs.trust.outputs.trusted == 'true' }}")
                self.assertEqual(with_["kind"], "${{ needs.trust.outputs.kind }}")
                self.assertEqual(with_["issue"], "${{ needs.trust.outputs.issue }}")
                self.assertEqual(with_["issue_pin"], "${{ needs.trust.outputs.pin }}")
                self.assertNotIn("issue_updated_at", with_)

    def test_untrusted_preset_comes_from_its_variable(self):
        self.assertEqual(
            self.jobs["factory-untrusted"]["with"]["preset"], "${{ vars.PIVOTAL_UNTRUSTED_PRESET }}"
        )
        self.assertEqual(self.jobs["factory-trusted"]["with"]["preset"], "${{ vars.PIVOTAL_PRESET }}")

    def test_stop_cancels_only_this_issues_runs(self):
        self.assertEqual(
            self.doc["run-name"],
            "pivotal-issue-${{ github.event.issue.number || github.event.client_payload.issue || 'none' }}",
        )
        self.assertEqual(self.jobs["stop"]["permissions"], {"actions": "write", "pull-requests": "read"})  # I4: the one read, no write
        (cancel,) = self.steps("stop")
        self.assertEqual(cancel["env"]["GH_REPO"], "${{ github.repository }}")
        self.assertEqual(cancel["env"]["WORKFLOW_REF"], "${{ github.workflow_ref }}")
        self.assertIn("env.ISSUE", cancel["run"])
        self.assertIn("gh run cancel", cancel["run"])
        self.assertIn("--paginate", cancel["run"])
        self.assertIn("per_page=100", cancel["run"])
        self.assertNotIn("pivotal.yml", cancel["run"])
        self.assertNotIn("${{", cancel["run"])
        self.assertRegex(cancel["run"], r'gh run cancel "\$run" \|\| ')

    def test_land_needs_only_the_permissions_it_uses(self):
        self.assertEqual(
            self.jobs["land"]["permissions"],
            {"actions": "read", "checks": "read", "contents": "write", "issues": "write", "pull-requests": "write"},
        )

    def test_every_action_and_workflow_is_pinned_by_sha(self):
        uses = [value for key, value in _walk(self.doc) if key == "uses"]
        self.assertTrue(uses)
        for value in uses:
            with self.subTest(uses=value):
                self.assertRegex(value, r"@[0-9a-f]{40}$")

    def test_engine_checkout_and_reusable_workflows_share_one_pin(self):
        pins = {value.rsplit("@", 1)[1] for key, value in _walk(self.doc)
                if key == "uses" and init.ENGINE_PLACEHOLDER in value}
        self.assertEqual(pins, {PIN})
        self.assertEqual(self.steps("trust")[0]["with"]["ref"], PIN)

    def test_engine_sha_is_the_same_pin_as_the_uses_references(self):
        for job_id in (*FACTORY_JOBS, "land"):
            with self.subTest(job=job_id):
                self.assertEqual(self.jobs[job_id]["with"]["engine_sha"], PIN)
        sha = "1234567890abcdef1234567890abcdef12345678"
        rendered = parse(init.render_caller(CALLER.read_text(encoding="utf-8"), sha))
        uses = {v.rsplit("@", 1)[1] for k, v in _walk(rendered) if k == "uses" and init.ENGINE_REPO in v}
        engine = {rendered["jobs"][j]["with"]["engine_sha"] for j in (*FACTORY_JOBS, "land")}
        self.assertEqual(uses, {sha})
        self.assertEqual(engine, {sha})

    def test_untrusted_text_reaches_scripts_only_through_env(self):
        for job_id, job in self.jobs.items():
            for step in job.get("steps", []):
                if "run" in step:
                    with self.subTest(job=job_id, step=step.get("name")):
                        self.assertNotIn("${{", step["run"])

    def test_checkouts_persist_no_credentials(self):
        for job_id, job in self.jobs.items():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    with self.subTest(job=job_id):
                        self.assertEqual(step["with"]["persist-credentials"], "false")


if __name__ == "__main__":
    unittest.main()
