import json
import subprocess
import sys
import unittest
from pathlib import Path

from engine import plan as p

ROOT = Path(__file__).resolve().parent.parent


def slice_dict(**changes) -> dict:
    base = {"id": "auth-login", "title": "Add login", "goal": "Add a login form."}
    return {**base, **changes}


def slices(*ids: str) -> list[dict]:
    return [slice_dict(id=i, goal=f"Build part {i}.") for i in ids]


class SliceRuleTest(unittest.TestCase):
    def assertRejected(self, data, rule: str):
        with self.assertRaisesRegex(p.SliceError, rule):
            p.check_slice(data)

    def test_a_valid_slice_is_accepted(self):
        self.assertEqual(p.check_slice(slice_dict()), p.Slice("auth-login", "Add login", "Add a login form."))

    def test_id_must_match_the_pattern(self):
        for ok in ["a", "0", "a-b-c", "x" * 40]:
            with self.subTest(ok=ok):
                self.assertEqual(p.check_slice(slice_dict(id=ok)).id, ok)
        for bad in ["", "-a", "A", "a_b", "a b", "a/b", "x" * 41, "a\n", "é"]:
            with self.subTest(bad=bad):
                self.assertRejected(slice_dict(id=bad), "id must match")

    def test_id_must_be_a_string(self):
        self.assertRejected(slice_dict(id=7), "id must be a string")

    def test_reserved_ids_are_refused(self):
        for reserved in ["stack", "base"]:
            with self.subTest(reserved=reserved):
                self.assertRejected(slice_dict(id=reserved), "reserved")

    def test_title_is_at_most_72_characters_and_not_empty(self):
        self.assertEqual(len(p.check_slice(slice_dict(title="t" * 72)).title), 72)
        self.assertRejected(slice_dict(title="t" * 73), "title must be at most 72 characters")
        self.assertRejected(slice_dict(title=""), "title must not be empty")
        self.assertRejected(slice_dict(title=3), "title must be a string")

    def test_goal_is_at_most_4000_characters_and_not_empty(self):
        self.assertEqual(len(p.check_slice(slice_dict(goal="g" * 4000)).goal), 4000)
        self.assertRejected(slice_dict(goal="g" * 4001), "goal must be at most 4000 characters")
        self.assertRejected(slice_dict(goal=" "), "goal must not be empty")
        self.assertRejected(slice_dict(goal=None), "goal must be a string")

    def test_control_and_format_characters_are_rejected_in_titles(self):
        for name, bad in {"NUL": "x\x00y", "newline": "x\ny", "CR": "x\ry", "tab": "x\ty", "ESC": "x\x1b[31m",
                          "bidi override": "x\u202eevil", "zero-width": "x\u200by", "line separator": "x\u2028y",
                          "paragraph separator": "x\u2029y"}.items():
            with self.subTest(name):
                self.assertRejected(slice_dict(title=bad), "title must not contain")

    def test_control_and_format_characters_are_rejected_in_goals_but_newline_and_tab_pass(self):
        for name, bad in {"NUL": "x\x00y", "CR": "x\ry", "ESC": "x\x1b[31m", "bidi override": "x\u202eevil",
                          "zero-width": "x\u200by", "line separator": "x\u2028y"}.items():
            with self.subTest(name):
                self.assertRejected(slice_dict(goal=bad), "goal must not contain")
        goal = "Zażółć gęślą jaźń:\n\t- one\n\t- two"
        self.assertEqual(p.check_slice(slice_dict(title="Dodaj logowanie é", goal=goal)).goal, goal)

    def test_unknown_keys_are_rejected(self):
        self.assertRejected(slice_dict(deps=["other"]), "unknown keys: \\['deps'\\]")

    def test_missing_keys_are_rejected(self):
        for key in ["id", "title", "goal"]:
            with self.subTest(key=key):
                data = slice_dict()
                del data[key]
                self.assertRejected(data, f"missing keys: \\['{key}'\\]")

    def test_a_slice_must_be_an_object(self):
        self.assertRejected(["auth-login", "Add login", "goal"], "slice must be an object")


class SliceSetRuleTest(unittest.TestCase):
    def test_at_most_six_slices_per_run(self):
        self.assertEqual(len(p.check_slices(slices("a", "b", "c", "d", "e", "f"))), 6)
        with self.assertRaisesRegex(p.SliceError, "at most 6 slices per run"):
            p.check_slices(slices("a", "b", "c", "d", "e", "f", "g"))

    def test_at_least_one_slice(self):
        with self.assertRaisesRegex(p.SliceError, "at least one slice"):
            p.check_slices([])

    def test_ids_must_be_unique(self):
        with self.assertRaisesRegex(p.SliceError, "duplicate id 'a'"):
            p.check_slices(slices("a", "b", "a"))

    def test_a_set_whose_matrix_output_is_over_the_cap_is_refused(self):
        # an astral character is 12 characters once json escapes it: six such goals pass the byte cap and not the output cap
        big = [slice_dict(id=f"s{i}", goal="\U0001F355" * (p.GOAL_MAX - 1) + str(i)) for i in range(6)]
        self.assertLess(len(json.dumps(big, ensure_ascii=False).encode()), p.PLAN_MAX_BYTES)
        with self.assertRaisesRegex(p.SliceError, f"over {p.job.MATRIX_MAX} characters"):
            p.check_slices(big)

    def test_a_bad_slice_is_named_by_position(self):
        with self.assertRaisesRegex(p.SliceError, "^slice 2: title must be at most 72"):
            p.check_slices([slice_dict(id="a"), slice_dict(id="b", title="t" * 73)])

    def test_the_set_must_be_a_list(self):
        with self.assertRaisesRegex(p.SliceError, "slices must be a list"):
            p.check_slices(slice_dict())


class ParseSlicesTest(unittest.TestCase):
    def test_parses_a_json_list_of_slices(self):
        text = json.dumps(slices("a", "b"))
        self.assertEqual([s.id for s in p.parse_slices(text)], ["a", "b"])

    def test_invalid_json_is_rejected(self):
        with self.assertRaisesRegex(p.SliceError, "not valid JSON"):
            p.parse_slices("[{")

    def test_a_duplicate_key_is_rejected_not_last_one_wins(self):
        text = '[{"id": "a", "id": "b", "title": "t", "goal": "g"}]'
        with self.assertRaisesRegex(p.SliceError, "duplicate key 'id'"):
            p.parse_slices(text)

    def test_the_top_level_must_be_a_list(self):
        with self.assertRaisesRegex(p.SliceError, "slices must be a list"):
            p.parse_slices(json.dumps(slice_dict()))


class HostileSliceSetTest(unittest.TestCase):
    def test_goals_that_are_copies_are_rejected(self):
        copy = [slice_dict(id="a", goal="Do  the thing."), slice_dict(id="b", goal="do the THING.")]
        with self.assertRaisesRegex(p.SliceError, "slice 2: goal repeats"):
            p.check_slices(copy)

    def test_deep_nesting_is_rejected_not_a_crash(self):
        with self.assertRaisesRegex(p.SliceError, "not valid JSON"):
            p.parse_slices("[" * 200000)

    def test_a_huge_number_is_rejected(self):
        with self.assertRaisesRegex(p.SliceError, "not valid JSON"):
            p.parse_slices("[" + "9" * 20000 + "]")

    def test_oversized_output_is_rejected(self):
        with self.assertRaisesRegex(p.SliceError, "larger than"):
            p.parse_slices(json.dumps([slice_dict(goal="g" * 4000)]) + " " * p.PLAN_MAX_BYTES)

    def test_reserved_ids_in_a_slice_set_are_rejected(self):
        with self.assertRaisesRegex(p.SliceError, "reserved"):
            p.parse_slices(json.dumps(slices("a", "stack")))


class RequestSliceTest(unittest.TestCase):
    def test_one_request_yields_one_slice(self):
        result = p.request_slices("Fix the login bug", "Login fails on Safari.")
        self.assertEqual(result, (p.Slice(p.SINGLE_SLICE_ID, "Fix the login bug", "Login fails on Safari."),))

    def test_the_single_slice_follows_the_same_rules(self):
        with self.assertRaisesRegex(p.SliceError, "title must be at most 72"):
            p.request_slices("t" * 73, "goal")
        with self.assertRaisesRegex(p.SliceError, "goal must be at most 4000"):
            p.request_slices("title", "g" * 4001)

    def test_a_request_over_a_bound_is_refused_by_name_not_as_a_bad_slice(self):
        # decide tells this refusal from the others: it is the one that names itself in the report.
        for title, goal in (("t" * 73, "goal"), ("title", "g" * 4001)):
            with self.subTest(title=len(title)), self.assertRaises(p.RequestTooLarge):
                p.request_slices(title, goal)
        self.assertTrue(issubclass(p.RequestTooLarge, p.SliceError))
        self.assertEqual(p.REQUEST_TOO_LARGE, "request_too_large")


class FixRoundTest(unittest.TestCase):
    def test_rounds_up_to_max_rounds_are_accepted(self):
        self.assertEqual(p.check_fix_round(1), 1)
        self.assertEqual(p.check_fix_round(2, max_rounds=2), 2)
        self.assertEqual(p.check_fix_round(3, max_rounds=3), 3)

    def test_the_default_is_one_fix_round(self):
        self.assertEqual(p.DEFAULT_MAX_ROUNDS, 1)

    def test_round_above_max_rounds_is_refused(self):
        with self.assertRaisesRegex(p.SliceError, "round 2 is above max_rounds 1"):
            p.check_fix_round(2)
        with self.assertRaisesRegex(p.SliceError, "round 4 is above max_rounds 3"):
            p.check_fix_round(4, max_rounds=3)

    def test_round_must_be_a_positive_integer(self):
        for bad in [0, -1, "1", 1.0, True, None]:
            with self.subTest(bad=bad), self.assertRaises(p.SliceError):
                p.check_fix_round(bad)

    def test_max_rounds_must_be_a_non_negative_integer(self):
        with self.assertRaisesRegex(p.SliceError, "max_rounds must be a non-negative integer"):
            p.check_fix_round(1, max_rounds="2")


class CommandLineTest(unittest.TestCase):
    def run_plan(self, stdin: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "engine.plan"],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )

    def test_a_good_slice_set_is_ok(self):
        proc = self.run_plan(json.dumps(slices("a")))
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "ok: 1 slice"))

    def test_a_bad_slice_prints_the_rule_that_rejected_it(self):
        proc = self.run_plan(json.dumps([slice_dict(id="Bad_ID")]))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("rejected: slice 1: id must match ^[a-z0-9][a-z0-9-]{0,39}$", proc.stderr)


if __name__ == "__main__":
    unittest.main()
