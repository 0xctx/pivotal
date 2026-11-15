"""The loop ladder (F4): one test class per level, and the parity of the places that decide.

`engine/job.py` is the one place the ladder is decided (`read_level`, `standing`, `read_start`); ack, plan, publish and
`piv doctor` go through it or are held to it here. Nothing here has run on real GitHub: the protected-branch case uses a
pre-receive hook on a bare repository, which stands in for GitHub's rule and answers the way a rejected push does, not
byte for byte.
"""

import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import fakegh
import levelscan
import nolive  # noqa: F401  (no test sweeps the live /proc)
from test_ack import AckBase
from test_doctor import REPO as DOCTOR_REPO
from test_doctor import DoctorTest, ok_json
import test_plan_job
import test_reviewjob
from test_plan_job import PlanBase, preset_json
from test_stack import STACK, StackCase

from engine import assemble, job, lines, plan, publish, reviewjob

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))
from piv import init  # noqa: E402

# Values that are not a level. Case, padding, separators and near misses all refuse: a typo must not run.
NOT_LEVELS = ("Land", "LAND", "Watch", "PREPARE", " land", "land ", "prepare\n", "\task", "watch\r\n", "off", "none", "true",
              "0", "land,watch", "prepare;land", "lands", "wat", " ", "\n", "'land'", '"ask"', "ｌａｎｄ")
KINDS_OF = {"watch": (), "ask": ("go", "fix"), "prepare": job.KINDS, "land": job.KINDS}


class LadderTest(unittest.TestCase):
    """The table of job.py's docstring, as data."""

    def test_the_ladder_is_the_plans(self):
        self.assertEqual(job.LEVELS, ("watch", "ask", "prepare", "land"))
        self.assertEqual(job.DEFAULT_LEVEL, "prepare")
        self.assertEqual(job.LEVEL_KINDS, KINDS_OF)

    def test_a_human_may_land_at_every_level_that_starts_work_and_at_no_other_word(self):
        self.assertEqual([level for level in job.LEVELS if job.may_land_by_hand(level)], ["ask", "prepare", "land"])
        for bad in NOT_LEVELS:
            with self.subTest(bad=bad):
                self.assertFalse(job.may_land_by_hand(bad))

    def test_unset_and_empty_are_the_default_and_nothing_else_is_accepted_loosely(self):
        self.assertEqual(job.read_level(""), "prepare")
        for level in job.LEVELS:
            self.assertEqual(job.read_level(level), level)
        for bad in NOT_LEVELS:
            with self.subTest(bad=bad), self.assertRaises(job.JobError):
                job.read_level(bad)

    def test_standing_holds_a_level_down_with_a_reason_and_never_up(self):
        def s(level, *, check=True, baseline="green", reviewer=True):
            return job.standing(level, check=check, baseline=baseline, reviewer=reviewer, planned=level)
        self.assertEqual(s("land"), job.Standing("land", "land", ()))
        self.assertEqual(s("land", reviewer=False), job.Standing("land", "prepare", ("no_cross_vendor_reviewer",)))
        self.assertEqual(s("land", baseline="red"), job.Standing("land", "ask", ("baseline_red",)))
        self.assertEqual(s("land", baseline="none", reviewer=False).reasons, ("no_cross_vendor_reviewer", "baseline_none"))
        self.assertEqual(s("prepare", baseline="red"), job.Standing("prepare", "ask", ("baseline_red",)))
        self.assertEqual(s("prepare", baseline="none").reasons, ("baseline_none",))
        self.assertEqual(s("prepare", baseline="bogus").reasons, ("baseline_red",), "an answer that is no verdict is red")
        self.assertEqual(s("ask", baseline="red"), job.Standing("ask", "ask", ()), "ask needs no green baseline")
        for level in ("ask", "prepare", "land"):
            self.assertEqual(s(level, check=False), job.Standing(level, "watch", ("no_check_command",)), level)
        self.assertEqual(s("watch", check=False), job.Standing("watch", "watch", ()))
        with self.assertRaises(job.JobError):
            s("Land")
        # the start of a run (early) may not know a fact yet: then it is not applied; a known one that is not true holds
        for level in job.LEVELS:
            self.assertEqual(job.early(level, check=True).effective, level)
        self.assertEqual(job.early("land", check=True, reviewer="yes").effective, "prepare", "only exactly True is a reviewer")
        self.assertEqual(job.early("land", check="yes").effective, "watch", "only exactly True is a check command")

    def test_standing_never_ranks_above_the_level_it_was_given(self):
        order = {name: i for i, name in enumerate(job.LEVELS)}
        for level in job.LEVELS:
            for planned in job.LEVELS:
                for check in (True, False):
                    for baseline in ("green", "red", "none"):
                        for reviewer in (True, False):
                            got = job.standing(level, check=check, baseline=baseline, reviewer=reviewer, planned=planned)
                            self.assertLessEqual(order[got.effective], min(order[level], order[planned]))
                            self.assertEqual(got.held, bool(got.reasons), (level, planned, check, baseline, reviewer))

    def test_the_words_of_piv_are_the_engines(self):
        self.assertEqual(init.LEVELS, job.LEVELS, "piv cannot import the engine, so a test holds its copy of the words to it")
        self.assertEqual(init.DEFAULT_LEVEL, job.DEFAULT_LEVEL)

    def test_the_land_path_takes_every_fact_and_a_missing_one_is_not_a_default(self):
        """F7: the function that decides landing has no None and no default for the reviewer, the baseline or the plan's level."""
        st = job.standing
        self.assertEqual(st("land", check=True, baseline="green", reviewer=True, planned="land"), job.Standing("land", "land", ()))
        for missing in ("baseline", "reviewer", "planned"):
            args = {"check": True, "baseline": "green", "reviewer": True, "planned": "land"}
            del args[missing]
            with self.subTest(missing=missing), self.assertRaises(TypeError):
                st("land", **args)
        for bad in (None, "yes", 1):
            with self.subTest(reviewer=bad), self.assertRaises(job.JobError):
                st("land", check=True, baseline="green", reviewer=bad, planned="land")
        for bad in (None, True, 1):
            with self.subTest(baseline=bad), self.assertRaises(job.JobError):
                st("land", check=True, baseline=bad, reviewer=True, planned="land")
        for bad in (None, "", "Land", "off"):
            with self.subTest(planned=bad), self.assertRaises(job.JobError):
                st("land", check=True, baseline="green", reviewer=True, planned=bad)
        self.assertFalse(hasattr(job.Standing, "starts"), "dead code: nothing asks a Standing what it starts")

    def test_the_start_of_a_run_may_not_know_the_reviewer_or_the_baseline_but_cannot_land(self):
        self.assertEqual(job.early("land", check=True).effective, "land")
        self.assertEqual(job.early("land", check=True, reviewer=False).effective, "prepare")
        self.assertEqual(job.early("land", check=False).effective, "watch")

    def test_publish_holds_at_the_lower_of_the_plans_level_and_its_own(self):
        """F3: a run planned at ask does not land because the operator set land mid-run."""
        order = {name: i for i, name in enumerate(job.LEVELS)}
        for planned in job.LEVELS:
            for now in job.LEVELS:
                with self.subTest(planned=planned, now=now):
                    got = job.standing(now, check=True, baseline="green", reviewer=True, planned=planned)
                    self.assertEqual(got.effective, min(planned, now, key=order.get))
                    self.assertEqual(got.held, order[planned] < order[now])
                    self.assertEqual(job.REASON_LEVEL_CHANGED in got.reasons, order[planned] < order[now])
                    self.assertEqual(got.level, now)

    def test_a_level_word_is_compared_nowhere_but_in_job_py(self):
        """F4: the AST scan finds no level word used as a decision anywhere under engine/, subdirectories included, except
        the table entries of TABLE_WORDS and the (file, role) pairs of ROLE_WORDS."""
        self.assertEqual(levelscan.engine_findings(ENGINE, job.LEVELS, TABLE_WORDS, ROLE_WORDS), {}, "a second copy of the ladder")

    def test_without_the_allowlists_exactly_the_known_entries_are_flagged(self):
        """The allowlists are not a loophole: without them, the four table entries and the role uses are flagged, and no other
        line of the engine is. Each flagged line is checked by its text, so a new flag anywhere fails here."""
        found = levelscan.engine_findings(ENGINE, job.LEVELS)
        self.assertEqual(sorted(found), sorted(KNOWN_WITHOUT_ALLOWLISTS))
        for rel, wanted in KNOWN_WITHOUT_ALLOWLISTS.items():
            with self.subTest(module=rel):
                lines = (ENGINE / rel).read_text(encoding="utf-8").splitlines()
                texts = [lines[number - 1].strip() for number in found[rel]]
                self.assertEqual(len(texts), len(wanted), texts)
                for text, want in zip(texts, wanted):
                    self.assertIn(want, text)

    def test_an_allowed_entry_is_exact_in_its_own_file_and_its_own_module_level_table(self):
        """Each escape is a finding: a substring of a file name, another file, another table, another key, a table in a
        function, a subscript store, and the same triple spelled as a concatenation."""
        for filename, source in (
            ("plan.py", 'JOBS = {"prepare": 1}'),
            ("buildjob_extra.py", 'JOBS = {"prepare": 1}'),
            ("buildjob.py", 'JOBS = {"land": 1}'),
            ("buildjob.py", 'OTHER = {"prepare": 1}'),
            ("buildjob.py", 'def f():\n    JOBS = {"prepare": 1}\n'),
            ("buildjob.py", 'JOBS["prepare"] = prepare'),
            ("buildjob.py", 'JOBS = {"pre" + "pare": 1}'),
            ("trust.py", 'KINDS = frozenset({"prepare"})'),
            ("trust.py", 'OTHER = frozenset({"land"})'),
        ):
            with self.subTest(filename=filename, source=source):
                self.assertTrue(levelscan.level_word_uses(source, job.LEVELS, filename, TABLE_WORDS), source)
        self.assertEqual(levelscan.level_word_uses('JOBS = {"prepare": 1}', job.LEVELS, "buildjob.py", TABLE_WORDS), [])
        self.assertEqual(levelscan.level_word_uses('KINDS = frozenset({"land"})', job.LEVELS, "trust.py", TABLE_WORDS), [])

    def test_the_scan_flags_each_shape_of_a_second_copy(self):
        for source in (
            'if level == "land": pass',
            'if "ask" != level: pass',
            'if level in ("watch", "ask"): pass',
            'if level in {"land"}: pass',
            'RANK = {"watch": 0, "ask": 1}',
            'RANK = ("watch", "ask", "prepare", "land")',
            'x = ["land"]',
            'x = RANK["prepare"]',
            'LAND = "land"',
            'x = (a := "land")',
            'KINDS = {"watch": (), "ask": ("go", "fix"), "prepare": (), "land": ()}',
            'KINDS = ("go",) if level == "land" else ()',
            'RANK = dict(zip("watch ask prepare land".split(), range(4)))',
            'x = level == job.LEVELS[-1]',
        ):
            with self.subTest(source=source):
                self.assertTrue(level_word_uses(source), source)
        for source in ('print("the land of plenty")', 'x = job.standing("land", check=True)', 'help = "one of watch, ask"',
                       'kind == "go"', 'f(level="land")'):
            with self.subTest(source=source):
                self.assertEqual(level_word_uses(source), [], source)

    def test_a_second_ladder_and_a_second_reviewer_rule_are_flagged_negative_fixtures(self):
        """F4: the two behaviour-identical second copies the first round's mutants M1' and M16 were: they pass every
        behaviour test, so only the scan can see them."""
        plan_src = (ROOT / "engine" / "plan.py").read_text(encoding="utf-8")
        ladder = plan_src + '\n_RANK = {"watch": 0, "ask": 1, "prepare": 2, "land": 3}\n'
        self.assertEqual(level_word_uses(plan_src, filename='plan.py'), [])
        self.assertTrue(level_word_uses(ladder, filename='plan.py'), "a second ladder in plan.py as a dict")
        publish_src = (ROOT / "engine" / "publish.py").read_text(encoding="utf-8")
        self.assertEqual(level_word_uses(publish_src), [])
        needle = "return self.trusted and job.lands(self.level, self.kind)"
        self.assertIn(needle, publish_src, "the fixture is derived from the real may_land")
        second = publish_src.replace(needle, 'return self.trusted and self.reviewer and self.level == "land"')
        self.assertTrue(level_word_uses(second), "a second reviewer rule in publish.py may_land")


ENGINE = ROOT / "engine"

# The table entries the scan lets through: the step table of each job, and trust's kinds. Exact (file, table, key)
# triples, relative to engine/: a substring of a file name, another file or table, or another key is a finding.
TABLE_WORDS = frozenset({
    ("buildjob.py", "JOBS", "prepare"),
    ("gatejob.py", "JOBS", "prepare"),
    ("reviewjob.py", "JOBS", "prepare"),
    ("trust.py", "KINDS", "land"),
})


# The (file, role) pairs the scan lets through: the trust command's kind and the artifact's name, which are not levels. A
# role is the text of the other side of a comparison, the target of an assignment, or a name passed to a call (levelscan.py).
# Each pair is exact: its file and its role, never a substring or a file-wide wildcard. A level compared in the same file has
# another role (`level`, `s.level`), so no pair admits it: test_the_allowlists_never_admit_a_level_in_the_same_file.
ROLE_WORDS = frozenset({
    ("land.py", "decision.kind"),  # the trust decision's kind: "/pivotal land" on a pull request, not a level
    ("land.py", "LAND_ARTIFACT"),  # the artifact's name, passed as data to listed() and download(), not a level
    ("trust.py", "kind"),  # the command kind a comment resolves to (land, stop, go...), not a level
    ("trust.py", "match.group(1)"),  # the command word the regex captured, before trust names it a kind
})

# Without the allowlists: the table entries and the role uses, by file, one entry per flagged line (its text, which contains it).
KNOWN_WITHOUT_ALLOWLISTS = {
    "buildjob.py": ['JOBS = {"prepare"'],
    "gatejob.py": ['JOBS = {"prepare"'],
    "reviewjob.py": ['JOBS = {"prepare"'],
    "trust.py": ['KINDS = frozenset({"go", "fix", "land"', 'match.group(1) != "land"', 'if kind == "land" and', 'if kind == "land":'],
    "land.py": ['LAND_ARTIFACT = "land"', 'decision.kind == "land"', 'listed(artifacts, LAND_ARTIFACT', 'download(hub, s, run_id, LAND_ARTIFACT'],
}


def level_word_uses(source: str, filename: str = "") -> list[int]:
    return levelscan.level_word_uses(source, job.LEVELS, filename, TABLE_WORDS, roles=ROLE_WORDS)


# One fixture per rule of levelscan.RULES. Each must be flagged by the full scan and by its own rule alone, so removing
# that rule from RULES fails its fixture (test/levelscan.py; the mutants were run in a scratch copy, see the build report).
SHAPES = (
    ("every level word in one string", levelscan.all_words_constant, 'RANK = dict(zip("watch ask prepare land".split(), range(4)))'),
    ("a comparison operand", levelscan.compare, 'if level == "land": pass'),
    ("a dict key or a display member", levelscan.display, 'x = {"land": 1}'),
    ("a subscript key", levelscan.subscript_key, 'x = RANK["prepare"]'),
    ("an arm of a conditional", levelscan.conditional, 'level = "land" if cond else "ask"'),
    ("an assigned value", levelscan.assigned, 'LAND = "land"'),
    ("a keyword name", levelscan.keyword_name, "x = dict(prepare=2, land=3)"),
    ("a match pattern", levelscan.match_pattern, 'match level:\n    case "land":\n        pass'),
    ("a subscript of the ladder", levelscan.levels_subscript, "if job.LEVELS[3] == cur: pass"),
    ("an index into the ladder", levelscan.levels_index, 'if job.LEVELS.index("prepare") == cur: pass'),
    ("the ladder iterated as data", levelscan.levels_as_data, "ORDER = {w: i for i, w in enumerate(job.LEVELS)}"),
    ("a concatenation of constants", levelscan.spelled_level, 'x = "lan" + "d"'),
)

# Spellings that build a level word from parts. Each is flagged by spelled_level alone.
CONCATENATIONS = (
    'a = "la"\nb = a + "nd"\nif level == b: pass',
    'x = "la"\nx += "nd"\nif level == x: pass',
    'x = "".join(["la", "nd"])',
    "x = f\"{'la'}nd\"",
    'x = "%sn%s" % ("la", "d")',
    'x = "{}{}".format("la", "nd")',
    'if level == ("la" + "nd"): pass',
    'KINDS = frozenset({"go", "la" + "nd"})',
    'x = ("la" if c else "lan") + "d"',
)


class ScanTest(unittest.TestCase):
    """F4: every shape of a second copy of the ladder is a finding, and each shape has its own fixture and rule."""

    def test_each_shape_is_flagged_and_its_own_rule_alone_flags_it(self):
        for name, rule, source in SHAPES:
            with self.subTest(shape=name):
                self.assertTrue(levelscan.level_word_uses(source, job.LEVELS, "x.py", TABLE_WORDS), source)
                self.assertTrue(levelscan.level_word_uses(source, job.LEVELS, "x.py", TABLE_WORDS, rules=(rule,)),
                                f"{name}: the rule {rule.__name__} does not flag it by itself")

    def test_a_concatenation_in_any_spelling_is_flagged(self):
        for source in CONCATENATIONS:
            with self.subTest(source=source):
                self.assertTrue(levelscan.level_word_uses(source, job.LEVELS, "x.py", TABLE_WORDS), source)
                self.assertTrue(levelscan.level_word_uses(source, job.LEVELS, "x.py", TABLE_WORDS, rules=(levelscan.spelled_level,)), source)

    def test_help_text_and_a_longer_string_are_not_flagged_by_any_rule(self):
        for source in ('print("the land of plenty")', 'x = "land" + "_x"', 'print("x" + "land of plenty")',
                       'x = "".join(["ask ", "me"])', 'if kind == "go" or f(levels=1): pass'):
            with self.subTest(source=source):
                self.assertEqual(levelscan.level_word_uses(source, job.LEVELS, "x.py", TABLE_WORDS), [], source)

    def test_the_allowlists_never_admit_a_level_in_the_same_file(self):
        """Each (file, role) pair is exact: the same role in another file, a file whose name contains the file, and a level
        compared in the same file are all flagged. A role's name compared or keyed as a level is flagged too."""
        for filename, source in (
            ("land.py", 'if s.level == "land": pass'),
            ("land.py", 'level = "land"'),
            ("land.py", 'if kind == "land": pass'),  # trust's role, in trust.py only
            ("land_extra.py", 'if decision.kind == "land": pass'),  # a file whose name contains land.py's
            ("trust.py", 'if decision.kind == "land": pass'),  # land's role, in land.py only
            ("trust.py", 'if s.level == "land": pass'),
            ("land.py", 'if decision.kind == "land" or s.level == "land": pass'),  # the allowed one beside a level: only the level
            ("land.py", 'LAND_ARTIFACT = "land"\nif s.level == LAND_ARTIFACT: pass'),  # a role's name compared as a level
            ("land.py", 'LAND_ARTIFACT = "land"\nx = {LAND_ARTIFACT: 1}'),  # a role's name as a dict key
            ("land.py", 'x = ("land", LAND_ARTIFACT)'),  # the constant in a display is not the allowed comparison
        ):
            with self.subTest(filename=filename, source=source):
                self.assertTrue(level_word_uses(source, filename), source)
        for filename, source in (
            ("land.py", 'if decision.kind == "land": pass'),
            ("trust.py", 'if kind == "land": pass'),
            ("trust.py", 'if match.group(1) != "land": pass'),
            ("land.py", 'LAND_ARTIFACT = "land"'),
            ("land.py", "listed(artifacts, LAND_ARTIFACT, run_id)"),  # a role's name passed as data
        ):
            with self.subTest(filename=filename, source=source):
                self.assertEqual(level_word_uses(source, filename), [], source)

    def test_a_real_level_comparison_added_to_land_py_or_trust_py_is_still_flagged(self):
        """The real modules are clean under the allowlists, and a plain level comparison put into either one is flagged on its
        own line: the allowed comparison, restored as a level, is flagged where it stood."""
        land_src = (ENGINE / "land.py").read_text(encoding="utf-8")
        trust_src = (ENGINE / "trust.py").read_text(encoding="utf-8")
        self.assertEqual(level_word_uses(land_src, "land.py"), [], "the real land.py is clean under the allowlists")
        self.assertEqual(level_word_uses(trust_src, "trust.py"), [], "the real trust.py is clean under the allowlists")
        needle = 'decision.kind == "land"'
        self.assertEqual(land_src.count(needle), 1)
        line = land_src[: land_src.index(needle)].count("\n") + 1
        self.assertEqual(level_word_uses(land_src.replace(needle, 's.level == "land"'), "land.py"), [line])
        appended = land_src + '\nif s.level == "land":\n    pass\n'
        self.assertEqual(level_word_uses(appended, "land.py"), [land_src.count("\n") + 2])
        appended = trust_src + '\nif level == "land":\n    pass\n'
        self.assertEqual(level_word_uses(appended, "trust.py"), [trust_src.count("\n") + 2])

    def test_the_scan_reaches_every_engine_module_subdirectories_included_and_skips_the_decider_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "score").mkdir()
            (root / "score" / "image.py").write_text('if level == "land": pass\n', encoding="utf-8")
            (root / "job.py").write_text('if level == "land": pass\n', encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "stale.py").write_text('if level == "land": pass\n', encoding="utf-8")
            self.assertEqual(levelscan.engine_findings(root, job.LEVELS), {"score/image.py": [1]})


class CheckPresentTest(unittest.TestCase):
    """F1: "there is a check command" is one predicate, and it is run.sh's own _blank_check."""

    CASES = (":", ";", "&", "# x", "make # x", " ", "\t", "make test", "", "\n", "# none yet", ": ; &", "true # x", "make;# x",
             "make\n# x", "# a\n# b\n:", "# a\nmake test", "echo #x", "echo#x", ";#x", "&#x", "make &", "make;", ": make", "\r\n",
             "\f", "\v:")

    @staticmethod
    def bash_blank(text: str) -> bool:
        done = subprocess.run(["bash", "-c", 'source "$1"; _blank_check "$2"', "bash", str(ROOT / "engine" / "run.sh"), text],
                              capture_output=True, text=True, check=False, timeout=30)
        assert done.returncode in (0, 1), done.stderr
        return done.returncode == 0

    def test_the_predicate_agrees_with_run_sh_on_every_case(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(job.check_present(text), not self.bash_blank(text))

    def test_pivs_copy_agrees_with_the_engines_and_with_run_sh(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(init.check_present(text), job.check_present(text))

    def test_the_known_answers(self):
        for text in (":", ";", "&", "# x", " ", "\t", "", "# none yet", ": ; &"):
            self.assertFalse(job.check_present(text), repr(text))
        for text in ("make test", "make # x", "make;# x", "echo#x"):
            self.assertTrue(job.check_present(text), repr(text))

    def test_read_start_refuses_a_check_that_runs_nothing(self):
        for text in self.CASES:
            with self.subTest(text=text), redirect_stderr(io.StringIO()):
                if job.check_present(text):
                    self.assertEqual(job.read_start("ask", text, "go"), job.Standing("ask", "ask"))
                else:
                    with self.assertRaises(job.JobError):
                        job.read_start("ask", text, "go")


class ParityTest(PlanBase, AckBase):
    """ack, plan and publish agree on every word, from the same table."""

    def test_ack_and_plan_refuse_every_word_that_read_level_refuses_before_any_write(self):
        for bad in NOT_LEVELS:
            with self.subTest(bad=bad):
                self.assertEqual(self.run_ack(LEVEL=bad), 1)
                self.assertEqual((self.writes(), self.outputs()), ([], {}))
                self.assertEqual(self.decide(LEVEL=bad), 1)
                self.assertEqual((self.gh.calls(), self.outputs()), ([], {}))

    def test_read_start_is_what_ack_and_plan_both_use(self):
        for level in ("", *job.LEVELS):
            for check in ("", "  ", ":", "# x", "make test"):
                for kind in job.KINDS:
                    with self.subTest(level=level, check=check, kind=kind), redirect_stderr(io.StringIO()):
                        want = job.read_level(level)
                        if not job.level_allows(want, kind):
                            self.assertIsNone(job.read_start(level, check, kind))
                        elif job.check_present(check):
                            self.assertEqual(job.read_start(level, check, kind), job.Standing(want, want))
                        else:
                            with self.assertRaises(job.JobError):
                                job.read_start(level, check, kind)

    def test_ack_and_plan_give_the_same_answer_to_every_level_check_and_kind(self):
        self.write_event({"workflow_run": {"name": "CI", "head_sha": "a" * 40, "id": 555}, "client_payload": {"workflow": "s"}})
        self.gh.route(f"repos/{test_plan_job.REPO}/issues/9", fakegh.ok(json.dumps({"number": 9, "title": "CI failed: CI", "body": "https://github.com/0xctx/piv-sandbox/actions/runs/9"})))
        for level in ("", *job.LEVELS):
            for check in ("", "make test"):
                for kind in job.KINDS:
                    with self.subTest(level=level, check=check, kind=kind):
                        over = {"LEVEL": level, "CHECK": check, "KIND": kind}
                        if kind in ("incident", "event"):
                            if job.level_allows(job.read_level(level), kind):
                                continue  # the allowed incident path has routes of its own (test_ack IncidentTest)
                            over.update(ISSUE="9", ISSUE_PIN="")
                        self.out.unlink(missing_ok=True)
                        ack_code = self.run_ack(**over)
                        ack_quiet = self.outputs().get("quiet") == "true"
                        self.out.unlink(missing_ok=True)
                        plan_code = self.decide(**over)
                        plan_quiet = self.outputs().get("matrix") == ""
                        self.assertEqual(ack_code == 1, plan_code == 1, "both refuse or neither does")
                        if ack_code == 0:
                            self.assertEqual(ack_quiet, plan_quiet, "both are quiet or neither is")


class DoctorParityTest(DoctorTest):
    def level_line(self, level: str | None, check: str | None) -> str:
        routes = {}
        routes[f"api repos/{DOCTOR_REPO}/actions/variables/PIVOTAL_LEVEL"] = [ok_json({"value": level}) if level is not None else fakegh.not_found()]
        routes[f"api repos/{DOCTOR_REPO}/actions/variables/PIVOTAL_CHECK"] = [ok_json({"value": check}) if check is not None else fakegh.not_found()]
        self.setUp()
        self.healthy(**routes)
        self.doctor(DOCTOR_REPO)
        (line,) = [x for x in self.out.getvalue().splitlines() if "Level:" in x]
        return line

    def test_doctor_judges_every_word_and_the_check_as_the_engine_does(self):
        for level in (None, "", *job.LEVELS, *NOT_LEVELS):
            for check in (None, "", "  ", ":", ";", "&", "# none yet", "make # x", "make test"):
                with self.subTest(level=level, check=check):
                    line = self.level_line(level, check)
                    try:
                        read = job.read_level(level or "")
                    except job.JobError:
                        self.assertTrue(line.strip().startswith("[problem]"), line)
                        continue
                    refused = job.early(read, check=job.check_present(check or "")).effective == "watch" and read != "watch"
                    self.assertEqual(line.strip().startswith("[problem]"), refused, line)


    def test_doctor_says_the_reviewer_and_the_baseline_are_checked_on_each_run(self):
        """F5: doctor cannot see the preset's reviewer or the base's check result, so a land that prints ok says so."""
        line = self.level_line("land", "make test")
        self.assertTrue(line.strip().startswith("[ok]"), line)
        self.assertIn("reviewer", line)
        self.assertIn("baseline", line)
        self.assertIn("each run", line)
        prepare = self.level_line("prepare", "make test")
        self.assertIn("baseline", prepare)
        self.assertIn("each run", prepare)


class PlanLevelOutputTest(PlanBase):
    def test_plan_emits_the_level_it_started_at(self):
        for level, want in (("", "prepare"), ("ask", "ask"), ("prepare", "prepare"), ("land", "land")):
            with self.subTest(level=level):
                self.out.unlink(missing_ok=True)
                self.assertEqual(self.decide(LEVEL=level), 0, self.stderr)
                self.assertEqual(self.outputs()["level"], want)

    def test_the_level_output_has_a_rule_that_accepts_exactly_the_four_words(self):
        rule = job.OUTPUT_RULES["level"]
        for word in job.LEVELS:
            self.assertIsNotNone(rule.fullmatch(word))
        for bad in ("", "Land", "land ", "land\nmatrix=[]", "off", "watch|ask"):
            self.assertIsNone(rule.fullmatch(bad), repr(bad))


class WatchTest(PlanBase, AckBase):
    """watch builds nothing."""

    def test_nothing_is_read_written_commented_or_built_for_any_kind(self):
        self.write_event({})
        for check in ("make test", ""):  # even a repo with no check is quiet at watch: the level is the first answer
            for kind in job.KINDS:
                with self.subTest(kind=kind, check=check):
                    self.gh.log.unlink(missing_ok=True)
                    self.out.unlink(missing_ok=True)
                    self.assertEqual(self.run_ack(LEVEL="watch", KIND=kind, CHECK=check), 0, self.stderr)
                    self.assertEqual(self.outputs(), {"quiet": "true"})
                    self.out.unlink()
                    self.assertEqual(self.decide(LEVEL="watch", KIND=kind, CHECK=check), 0, self.stderr)
                    self.assertEqual(self.outputs(), {"matrix": "", "round": "0", "kaggle_needed": "false"})
                    self.assertEqual(self.gh.calls(), [], "no read, no comment, no label, no issue")

    def test_the_quiet_exit_names_the_level_and_the_kind(self):
        self.assertEqual(self.decide(LEVEL="watch"), 0)
        self.assertIn("quiet: the level is watch, which starts no go", self.stderr)

    def test_watch_has_no_baseline_run_and_no_reviewer_output(self):
        self.assertEqual(self.decide(LEVEL="watch"), 0)
        self.assertNotIn("reviewer", self.outputs())
        self.assertFalse(job.level_allows("watch", "go"))


class AskTest(PlanBase, AckBase):
    """ask starts only on your triggers."""

    def test_go_and_fix_start(self):
        for kind in ("go", "fix"):
            with self.subTest(kind=kind):
                self.assertTrue(job.level_allows("ask", kind))
        self.assertEqual(self.run_ack(LEVEL="ask"), 0, self.stderr)
        self.assertEqual(self.outputs()["quiet"], "false")
        self.assertEqual(len(self.writes()), 2, "the run comment and the building label")

    def test_a_red_ci_or_webhook_incident_is_quiet_in_ack_and_in_plan(self):
        self.write_event({"workflow_run": {"name": "CI", "head_sha": "a" * 40, "id": 555}, "client_payload": {"workflow": "s"}})
        for kind in ("incident", "event"):
            with self.subTest(kind=kind):
                self.gh.log.unlink(missing_ok=True)
                self.out.unlink(missing_ok=True)
                self.assertEqual(self.run_ack(LEVEL="ask", KIND=kind, ISSUE="", ISSUE_PIN=""), 0, self.stderr)
                self.assertEqual(self.outputs(), {"quiet": "true"})
                self.out.unlink()
                self.assertEqual(self.decide(LEVEL="ask", KIND=kind, ISSUE="", ISSUE_PIN=""), 0, self.stderr)
                self.assertEqual(self.outputs()["matrix"], "")
                self.assertEqual(self.gh.calls(), [], "no incident issue, no comment, no label")

    def test_ask_needs_a_check_command_and_refuses_loudly_before_any_write_without_one(self):
        for check in ("", "   ", ":", ";", "&", "# none yet", "\t"):
            with self.subTest(check=check):
                self.assertEqual(self.run_ack(LEVEL="ask", CHECK=check), 1)
                self.assertIn("no_check_command", self.stderr)
                self.assertIn("PIVOTAL_CHECK", self.stderr)
                self.assertEqual((self.gh.calls(), self.outputs()), ([], {}))
                self.assertEqual(self.decide(LEVEL="ask", CHECK=check), 1)
                self.assertIn("no_check_command", self.stderr)
                self.assertEqual((self.gh.calls(), self.outputs()), ([], {}))

    def test_ask_does_not_need_a_green_baseline(self):
        self.assertEqual(job.early("ask", check=True, baseline="red").effective, "ask")
        self.assertEqual(job.early("ask", check=True, baseline="none").effective, "ask")


class PrepareTest(PlanBase, AckBase):
    """prepare needs a green baseline, and says so when it is not."""

    def test_the_default_level_starts_the_operators_work_and_incidents(self):
        for kind in job.KINDS:
            self.assertEqual(job.read_start("", "make test", kind), job.Standing("prepare", "prepare"))
        self.assertEqual(self.run_ack(LEVEL=""), 0, self.stderr)

    def test_prepare_needs_a_check_command_too(self):
        self.assertEqual(self.run_ack(LEVEL="prepare", CHECK=""), 1)
        self.assertEqual(self.decide(LEVEL="", CHECK=""), 1)
        self.assertEqual(self.gh.calls(), [])

    def test_a_red_or_missing_baseline_holds_prepare_at_ask_and_the_log_says_why(self):
        for check, word, reason in (("false", "red", "baseline_red"), ("", "none", "no_check_command"),
                                    (":", "none", "no_check_command"), ("# none yet", "none", "no_check_command")):
            with self.subTest(word=word):
                case = test_plan_job.BaselineTest("test_a_failing_check_is_red")
                case.setUp()
                self.addCleanup(case.doCleanups)
                self.assertEqual(case.baseline(check, LEVEL="prepare"), 0, case.stderr)
                self.assertEqual(case.outputs()["baseline"], word)
                self.assertIn(f"note: the level is prepare, held at {'ask' if job.check_present(check) else 'watch'}: {reason}", case.stderr)

    def test_a_none_baseline_with_a_check_set_is_held_by_the_baseline(self):
        self.assertEqual(job.early("prepare", check=True, baseline="none").reasons, ("baseline_none",))

    def test_a_green_baseline_is_not_held_and_adds_no_note(self):
        case = test_plan_job.BaselineTest("test_a_failing_check_is_red")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.assertEqual(case.baseline("true", LEVEL="prepare"), 0, case.stderr)
        self.assertNotIn("held at", case.stderr)

    def test_a_baseline_that_is_not_green_makes_every_slice_a_draft_with_a_reason_at_every_level(self):
        ok = {"status": "ok", "workflow_change": False, "round": 0, "gate": "green"}
        for baseline in ("red", "none", "", "Green", "green "):
            self.assertEqual(assemble.classify(ok, baseline), ("draft", "baseline_red"), baseline)
        self.assertEqual(assemble.classify(ok, "green"), ("candidate", "stacked"))


class PreparePublishTest(StackCase):
    """The publish side of prepare, on real git."""

    def test_prepare_opens_a_ready_stack_pr_seals_it_and_moves_nothing(self):
        # Contract change (prepare-seal): at the prepare level publish now seals the READY stack PR when the whole stack
        # passed, the run is trusted and the level is not held, so "land it" has a seal to land (F4). It still moves nothing.
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.route_stack_pr(self.gh, head)
        self.route_seal(self.gh, head)
        code, outputs, stdout, err = self.publish(out, LEVEL="prepare")
        self.assertEqual((code, self.main_tip()), (0, self.base), err)
        self.assertEqual(len(self.seals()), 1, "the ready stack PR is sealed so a human can land it")
        self.assertIn("why a pull request: level", Path(self.creates()[0][self.creates()[0].index("--body-file") + 1]).read_text())
        self.assertEqual(self.result(outputs).status, "ready")

    def test_prepare_with_a_red_baseline_lands_nothing_and_says_it_is_held_at_ask(self):
        out = self.stacked("one")
        land = assemble.read_land(out / assemble.LAND)
        (out / assemble.LAND).write_bytes(assemble.land_text({**land, "baseline": "red"}))
        self.route_stack_pr(self.gh, self.heads(out)["one"], draft=True)
        code, outputs, stdout, err = self.publish(out, LEVEL="prepare")
        self.assertEqual((code, self.main_tip(), self.seals()), (0, self.base, []), err)
        self.assertIn("level: prepare is held at ask: baseline_red", stdout)
        self.assertEqual(self.result(outputs).why, "baseline_red")

    def test_garbage_levels_never_land_in_publish(self):
        out = self.stacked("one")
        for bad in NOT_LEVELS:
            with self.subTest(bad=bad):
                code, outputs, _, err = self.publish(out, LEVEL=bad)
                self.assertEqual((code, outputs, self.main_tip(), self.seals()), (1, {}, self.base, []))
                self.assertEqual(self.gh.calls(), [])


class ReviewerFact(PlanBase):
    """The REVIEWER fact is computed on the preset the run builds with, and the review job routes with the same one."""

    def labelled(self, preset: str, lines_preset: str, tmp: Path, **over) -> int:
        """plan decide for the custom line `fancy`, which builds with `lines_preset` (a file under tmp/presets)."""
        custom = json.loads(preset)
        custom["lines"] = [{"name": "fancy", "summary": "s", "prompt": "build.md", "gate": "standard", "preset": lines_preset}]
        self.gh.route(test_plan_job.ISSUE_PATH, fakegh.ok(jobcase_issue(labels=[{"name": "pivotal:fancy"}])), first=True)
        with mock.patch.object(plan, "ROOT", tmp):
            return self.decide(PRESET=custom["name"], PRESET_JSON=json.dumps(custom), LEVEL="land", TRUSTED="true", **over)


def jobcase_issue(**extra) -> str:
    from jobcase import issue_json
    return issue_json(**extra)


class ReviewerParity(test_reviewjob.ReviewCase):
    """F2: plan's REVIEWER fact equals what the review job routes, for a line whose preset differs from the caller's."""

    GEMINI = {"name": "other", "tiers": {"build": [test_reviewjob.BUILT_BY], "review": ["agy:google/gemini"]}}
    SAME_VENDOR = {"name": "other", "tiers": {"build": [test_reviewjob.BUILT_BY], "review": ["claude:anthropic/opus"]}}

    def routed(self, caller: dict, line_preset: dict) -> tuple[str, bool]:
        """(plan's REVIEWER output, whether the review job found a reviewer) for the one run."""
        tmp = self.root / "plan-root"
        (tmp / "presets").mkdir(parents=True, exist_ok=True)
        (tmp / "presets" / "other.json").write_text(json.dumps(line_preset), encoding="utf-8")
        fact = ReviewerFact()
        fact.setUp()
        self.addCleanup(fact.doCleanups)
        self.assertEqual(fact.labelled(json.dumps(caller), "other", tmp), 0, fact.stderr)
        want = fact.outputs()
        self.assertEqual(want["line"], "fancy")
        self.agent(*test_reviewjob.GOOD)
        self.agent(*test_reviewjob.GOOD, name="claude")
        custom = {**caller, "lines": [{"name": "fancy", "summary": "s", "prompt": "build.md", "gate": "standard", "preset": "other"}]}
        self.reset()
        with mock.patch.object(plan, "ROOT", tmp):
            self.assertEqual(self.run_step(PRESET=caller["name"], PRESET_JSON=json.dumps(custom), LINE="fancy",
                                           CLAUDE_CODE_OAUTH_TOKEN="claude-token-0123456789"), 0, self.stderr)
        verdict = self.verdict()
        return want["reviewer"], verdict["reviewed_by"] != "none"

    def test_a_line_preset_without_a_reviewer_under_a_caller_preset_with_one(self):
        caller = {"name": "callerp", "tiers": {"build": [test_reviewjob.BUILT_BY], "review": ["agy:google/gemini"]}}
        fact, routed = self.routed(caller, self.SAME_VENDOR)
        self.assertEqual((fact, routed), ("false", False), "plan says no reviewer, so the review job must route none")

    def test_a_line_preset_with_a_reviewer_under_a_caller_preset_without_one(self):
        caller = {"name": "callerp", "tiers": {"build": [test_reviewjob.BUILT_BY], "review": ["claude:anthropic/opus"]}}
        fact, routed = self.routed(caller, self.GEMINI)
        self.assertEqual((fact, routed), ("true", True), "plan says a reviewer exists, so the review job must route one")


class LandTest(StackCase):
    """land needs a reviewer of a different vendor; a protected branch means a PR."""

    def land(self, out: Path, **over):
        heads = self.heads(out)
        self.route_seal(self.gh, heads[assemble.read_land(out / assemble.LAND)["positions"][-1]])
        return self.publish(out, **{"LEVEL": "land", **over})

    def test_a_cross_vendor_preset_has_a_reviewer_and_a_same_vendor_one_has_none(self):
        base = ReviewerFact()
        base.setUp()
        self.addCleanup(base.doCleanups)
        for preset, want in ((preset_json(), "true"), (json.dumps({"name": "claude-sub", "tiers": {
                "build": ["claude:anthropic/sonnet"], "review": ["claude:anthropic/opus"]}}), "false")):
            with self.subTest(want=want):
                base.out.unlink(missing_ok=True)
                self.assertEqual(base.decide(LEVEL="land", PRESET_JSON=preset), 0, base.stderr)
                self.assertEqual(base.outputs()["reviewer"], want)
                self.assertNotEqual(base.outputs()["matrix"], "", "the run is not stopped, the landing is held")
                if want == "false":
                    self.assertIn("note: the level is land, held at prepare: no_cross_vendor_reviewer", base.stderr)
                else:
                    self.assertNotIn("held at", base.stderr)

    def test_with_a_reviewer_a_green_stack_lands_and_is_sealed(self):
        out = self.stacked("one")
        code, outputs, stdout, err = self.land(out, REVIEWER="true")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.heads(out)["one"])
        self.assertEqual(self.result(outputs).status, "landed")
        self.assertEqual(len(self.seals()), 1)

    def test_without_a_reviewer_it_degrades_to_prepare_visibly_and_never_lands_or_seals(self):
        # Held level (job.standing): degrades to prepare, so it never lands. A held level never seals under the
        # prepare-seal contract, so an unreviewed stack gets no seal (the case below covers that explicitly).
        out = self.stacked("one")
        self.route_stack_pr(self.gh, self.heads(out)["one"])
        code, outputs, stdout, err = self.land(out, REVIEWER="false")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.main_tip(), self.seals()), (self.base, []))
        (create,) = self.creates()
        self.assertNotIn("--draft", create)
        body = Path(create[create.index("--body-file") + 1]).read_text()
        self.assertIn("why a pull request: held", body)
        self.assertIn("level held: land as prepare: no_cross_vendor_reviewer", body)
        self.assertIn("level: land is held at prepare: no_cross_vendor_reviewer", stdout)
        self.assertEqual(self.result(outputs).status, "ready")

    def test_a_degraded_to_prepare_run_without_a_cross_vendor_reviewer_must_not_seal(self):
        # The old case that must still hold (work order, prepare-seal correction): a land level held to prepare because
        # there is no different-vendor reviewer (job.standing) never seals, so nothing is landed on the strength of an
        # unreviewed stack. The level is not honoured as prepare for sealing because it is held.
        out = self.stacked("one")
        head = self.heads(out)["one"]
        self.route_stack_pr(self.gh, head)
        code, outputs, stdout, err = self.land(out, REVIEWER="false")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.base, "never lands")
        self.assertEqual(self.seals(), [], "a held level never seals")
        self.assertIn("no_cross_vendor_reviewer", stdout)

    def test_a_missing_or_garbled_reviewer_fact_refuses_before_any_write(self):
        out = self.stacked("one")
        for bad in ("", "True", "yes", "1", "true\n", None):
            with self.subTest(bad=bad):
                env = self.penv(out, LEVEL="land")
                if bad is None:
                    env.pop("REVIEWER")
                else:
                    env["REVIEWER"] = bad
                with self.assertRaises(job.JobError):
                    publish.read_settings(env)

    def test_a_protected_branch_at_the_land_level_is_a_pr_not_a_push(self):
        out = self.stacked("one")
        self.protect_main()
        heads = self.heads(out)
        self.route_stack_pr(self.gh, heads["one"])
        self.route_seal(self.gh, heads["one"])
        code, outputs, stdout, err = self.publish(out, LEVEL="land")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.base)
        self.assertIn("stack: opening the stack pull request (ff_rejected)", stdout)
        (create,) = self.creates()
        self.assertNotIn("--draft", create)
        result = self.result(outputs)
        self.assertEqual((result.status, result.pr), ("ready", 20))
        self.assertEqual(self.remote_refs()[f"refs/heads/{STACK}"], heads["one"])

    def with_baseline(self, out: Path, word: str) -> None:
        land = assemble.read_land(out / assemble.LAND)
        (out / assemble.LAND).write_bytes(assemble.land_text({**land, "baseline": word}))

    def test_a_red_baseline_holds_land_at_ask(self):
        out = self.stacked("one")
        self.with_baseline(out, "red")
        self.route_stack_pr(self.gh, self.heads(out)["one"], draft=True)
        code, outputs, stdout, err = self.land(out, REVIEWER="true")
        self.assertEqual((code, self.main_tip(), self.seals()), (0, self.base, []), err)
        self.assertIn("level: land is held at ask: baseline_red", stdout)
        self.assertEqual(self.result(outputs).why, "baseline_red")

    def none_baseline_held(self, check: str, held: str) -> None:
        out = self.stacked("one")
        self.with_baseline(out, "none")
        self.route_stack_pr(self.gh, self.heads(out)["one"], draft=True)
        code, outputs, stdout, err = self.land(out, REVIEWER="true", CHECK=check)
        self.assertEqual((code, self.main_tip(), self.seals()), (0, self.base, []), err)
        self.assertIn(held, stdout)

    def test_a_none_baseline_with_a_check_set_holds_land_at_ask(self):
        self.none_baseline_held("make test", "level: land is held at ask: baseline_none")

    def test_a_none_baseline_without_a_check_command_holds_land_at_watch(self):
        self.none_baseline_held("", "level: land is held at watch: no_check_command")

    def test_a_none_baseline_with_a_check_that_runs_nothing_holds_land_at_watch(self):
        self.none_baseline_held(":", "level: land is held at watch: no_check_command")

    def test_a_none_baseline_with_a_comment_only_check_holds_land_at_watch(self):
        self.none_baseline_held("# none yet", "level: land is held at watch: no_check_command")

    def test_a_missing_check_fact_holds_the_run_at_watch(self):
        """The check command is the publish job's input too (CHECK): none is the same as a blank one, never landable."""
        out = self.stacked("one")
        self.route_stack_pr(self.gh, self.heads(out)["one"])
        code, outputs, stdout, err = self.land(out, REVIEWER="true", CHECK=None)
        self.assertEqual((code, self.main_tip(), self.seals()), (0, self.base, []), err)
        self.assertIn("held at watch: no_check_command", stdout)

    def test_a_run_planned_at_ask_does_not_land_because_the_level_was_raised_mid_run(self):
        """F3: plan's level is carried, and publish holds at the lower of it and its own, saying so. The level is held,
        so under the prepare-seal contract it never lands and never seals (the case below covers that explicitly)."""
        out = self.stacked("one")
        self.route_stack_pr(self.gh, self.heads(out)["one"])
        code, outputs, stdout, err = self.land(out, REVIEWER="true", LEVEL="land", PLANNED_LEVEL="ask")
        self.assertEqual((code, self.main_tip(), self.seals()), (0, self.base, []), err)
        self.assertIn("level: land is held at ask: level_lowered_since_plan", stdout)
        (create,) = self.creates()
        body = Path(create[create.index("--body-file") + 1]).read_text()
        self.assertIn("level held: land as ask: level_lowered_since_plan", body)

    def test_a_level_raised_mid_run_must_not_land(self):
        # The old case that must still hold (work order, prepare-seal correction): a run planned at a lower level must
        # not land when the level was raised mid-run (F3, job.standing holds it). A held level never seals.
        out = self.stacked("one")
        self.route_stack_pr(self.gh, self.heads(out)["one"])
        code, outputs, stdout, err = self.land(out, REVIEWER="true", LEVEL="land", PLANNED_LEVEL="ask")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.main_tip(), self.base, "a level raised mid-run must not land")
        self.assertEqual(self.seals(), [], "a held level never seals")
        self.assertIn("level_lowered_since_plan", stdout)

    def test_a_level_lowered_after_plan_holds_too_and_an_equal_one_lands(self):
        out = self.stacked("one")
        self.assertTrue(publish.read_settings(self.penv(out, LEVEL="land", PLANNED_LEVEL="land", REVIEWER="true")).with_baseline("green").may_land)
        for planned, now in (("land", "prepare"), ("prepare", "land"), ("ask", "land"), ("watch", "land"), ("land", "ask")):
            with self.subTest(planned=planned, now=now):
                s = publish.read_settings(self.penv(out, LEVEL=now, PLANNED_LEVEL=planned, REVIEWER="true")).with_baseline("green")
                self.assertFalse(s.may_land)

    def test_a_missing_or_garbled_planned_level_refuses_before_any_write(self):
        out = self.stacked("one")
        for bad in ("", "Land", "land ", "yes", None):
            with self.subTest(bad=bad):
                env = self.penv(out, LEVEL="land")
                env.pop("PLANNED_LEVEL", None)
                if bad is not None:
                    env["PLANNED_LEVEL"] = bad
                with self.assertRaises(job.JobError):
                    publish.read_settings(env)

    def test_only_the_land_level_lands_whatever_the_other_facts_say(self):
        out = self.stacked("one")
        for level in ("watch", "ask", "prepare"):
            with self.subTest(level=level):
                self.assertFalse(publish.read_settings(self.penv(out, LEVEL=level, REVIEWER="true")).with_baseline("green").may_land)
        self.assertTrue(publish.read_settings(self.penv(out, LEVEL="land", REVIEWER="true")).with_baseline("green").may_land)
        self.assertFalse(publish.read_settings(self.penv(out, LEVEL="land", REVIEWER="false")).with_baseline("green").may_land)
        self.assertFalse(publish.read_settings(self.penv(out, LEVEL="land", REVIEWER="true")).may_land,
                         "before land.json's baseline is read the settings land nothing")


class FactoryWiringTest(unittest.TestCase):
    """The workflow hands the deciders the same variables."""

    def test_every_place_that_decides_reads_the_level_and_the_check_from_the_repo_variables(self):
        text = (ROOT / ".github/workflows/factory.yml").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"LEVEL: \$\{\{ vars\.PIVOTAL_LEVEL \}\}", text)), 4, "ack, plan decide, plan baseline, publish")
        self.assertEqual(len(re.findall(r"CHECK: \$\{\{ vars\.PIVOTAL_CHECK \}\}", text)), 6, "ack, plan x2, build, gate, publish")
        self.assertIn("REVIEWER: ${{ needs.plan.outputs.reviewer }}", text)
        self.assertIn("reviewer: ${{ steps.decide.outputs.reviewer }}", text)
        self.assertIn("PLANNED_LEVEL: ${{ needs.plan.outputs.level }}", text)
        self.assertIn("level: ${{ steps.decide.outputs.level }}", text)

    def test_the_publish_job_reads_the_check_and_the_review_job_the_line(self):
        text = (ROOT / ".github/workflows/factory.yml").read_text(encoding="utf-8")
        review_step = text[text.index("      - name: Review the slice"):]
        review_step = review_step[: review_step.index("\n      - name:", 10)]
        self.assertIn("LINE: ${{ needs.plan.outputs.line }}", review_step)
        end = text.index("python3 -I pivotal/engine/publish.py run")
        publish_step = text[text.rindex("      - name:", 0, end): end]
        self.assertIn("CHECK: ${{ vars.PIVOTAL_CHECK }}", publish_step)


if __name__ == "__main__":
    unittest.main()
