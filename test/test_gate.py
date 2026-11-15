"""The gate artifact schema (engine/gate.py): exact keys, the green rule, and the checks publish and land will make."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from engine import assemble, gate

SHA = "a" * 40


def good(**over) -> dict:
    doc = {"version": gate.GATE_VERSION, "issue": 7, "id": "main", "sha": SHA, "check": "green", "head_unchanged": True, "clean": True,
           "green": True, "score": None, "target": None}
    doc.update(over)
    return doc


def land_for(heads=(SHA,), ids=("main",)) -> dict:
    slices = [
        {"id": i, "outcome": "candidate", "reason": "stacked", "candidate": True, "branch": assemble.stack_branch(7), "head": h,
         "commits": [h], "built_by": "claude:anthropic/sonnet", "gate": "green", "score": None, "target": "", "reviewed_by": ""}
        for i, h in zip(ids, heads)
    ]
    return {"version": 2, "issue": 7, "base_sha": "b" * 40, "baseline": "green", "bundle": {"name": "pivotal.bundle", "sha256": "0" * 64, "bytes": 1},
            "has_stack": True, "positions": list(ids), "slices": slices}


class SchemaTest(unittest.TestCase):
    def test_a_good_gate_passes_and_round_trips_through_its_bytes(self):
        self.assertEqual(gate.check_gate(good()), good())
        self.assertEqual(json.loads(gate.gate_text(good())), good())
        self.assertTrue(gate.gate_text(good()).isascii())

    def test_green_is_exactly_check_green_head_unchanged_and_clean(self):
        for check in gate.CHECKS:
            for head in (True, False):
                for clean in (True, False):
                    want = check == "green" and head and clean
                    with self.subTest(check=check, head=head, clean=clean):
                        doc = good(check=check, head_unchanged=head, clean=clean, green=want)
                        self.assertEqual(gate.check_gate(doc)["green"], want)
                        with self.assertRaises(gate.GateError):
                            gate.check_gate({**doc, "green": not want})

    def test_a_gate_that_says_green_when_the_head_moved_or_the_tree_is_dirty_is_refused(self):
        for over in ({"head_unchanged": False}, {"clean": False}, {"check": "red"}, {"check": "none"}):
            with self.subTest(over=over), self.assertRaises(gate.GateError):
                gate.check_gate(good(**over))

    def test_keys_are_exact(self):
        for key in gate.GATE_KEYS:
            doc = good()
            del doc[key]
            with self.subTest(missing=key), self.assertRaises(gate.GateError):
                gate.check_gate(doc)
        with self.assertRaises(gate.GateError):
            gate.check_gate({**good(), "extra": 1})
        for bad in (None, [], "x", 1, [good()]):
            with self.assertRaises(gate.GateError):
                gate.check_gate(bad)

    def test_every_field_has_one_type(self):
        bad = {
            "version": (1, 3, True, "2", None, 1.0), "issue": (0, -1, True, "7", 7.5, 10**10), "id": ("", "stack", "base", "A", "a b", "a" * 41, 1, None),
            "sha": ("", "A" * 40, "a" * 39, "0" * 40, "a" * 41, 1, None), "check": ("ok", "GREEN", "", None, True),
            "head_unchanged": (1, "true", None), "clean": (1, "true", None), "green": (1, "true", None),
            "score": (-0.1, 1.1, float("nan"), float("inf"), 10**400, -(10**400), "0.5", True, [], {}),
            "target": (0, 1.0, "0", "0.0", ".000000", "0.000000", "1.5", "0.9999999", True, "nine", []),
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(gate.GateError):
                    gate.check_gate(good(**{key: value}))

    def test_a_gate_of_another_version_says_so_whatever_its_keys(self):
        old = {k: v for k, v in good().items() if k not in ("target", "score")}
        old["version"] = 1
        with self.assertRaisesRegex(gate.GateError, "version must be"):
            gate.check_gate(old)

    def test_a_target_of_zero_in_any_spelling_is_refused_even_when_the_score_meets_it(self):
        for target in ("0.0", ".0", "0.000000", ".000000"):
            with self.subTest(target=target), self.assertRaises(gate.GateError):
                gate.check_gate(good(score=0.5, target=target, green=True))

    def test_a_score_is_null_or_a_number_from_0_to_1(self):
        for score in (None, 0, 0.0, 0.5, 1, 1.0):
            target = None if score is None else "0.1"
            self.assertEqual(gate.check_gate(good(score=score, target=target, green=score is None or score >= 0.1))["score"], score)

    def test_a_score_run_is_green_only_when_the_score_meets_the_target(self):
        self.assertTrue(gate.check_gate(good(score=0.9, target="0.9", green=True))["green"])
        self.assertFalse(gate.check_gate(good(score=0.4, target="0.9", green=False))["green"])
        for over in ({"score": 0.5, "target": "0.9"}, {"score": None, "target": "0.9"}):
            with self.subTest(over=over), self.assertRaises(gate.GateError):
                gate.check_gate(good(green=True, **over))  # a green score run needs a score at or above the target
        with self.assertRaises(gate.GateError):
            gate.check_gate(good(score=0.9, target=None, green=True))  # a score with no target is refused

    def test_the_check_must_be_green_for_a_score_run_too(self):
        with self.assertRaises(gate.GateError):
            gate.check_gate(good(check="red", score=0.95, target="0.9", green=True))

    def test_the_error_names_the_rule_and_never_repeats_a_value(self):
        try:
            gate.check_gate(good(id="secret-value-here ZZ"))
        except gate.GateError as err:
            self.assertNotIn("secret-value", str(err))


class ReadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "gate.json"

    def test_a_good_file_is_read(self):
        self.path.write_bytes(gate.gate_text(good()))
        self.assertEqual(gate.read_gate(self.path), good())

    def test_a_file_of_version_1_is_refused_by_the_version_and_not_called_invalid_json(self):
        old = {k: v for k, v in good().items() if k not in ("target", "score")}
        old["version"] = 1
        self.path.write_text(json.dumps(old))
        with self.assertRaisesRegex(gate.GateError, "version must be 2"):
            gate.read_gate(self.path)

    def test_a_missing_symlinked_fifo_directory_or_oversized_file_is_refused(self):
        with self.assertRaises(gate.GateError):
            gate.read_gate(self.path)
        real = self.dir / "real"
        real.write_bytes(gate.gate_text(good()))
        os.symlink(real, self.path)
        with self.assertRaises(gate.GateError):
            gate.read_gate(self.path)
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(gate.GateError):
            gate.read_gate(self.path)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(gate.GateError):
            gate.read_gate(self.path)
        self.path.rmdir()
        self.path.write_bytes(b" " * (gate.MAX_GATE_BYTES + 1))
        with self.assertRaises(gate.GateError):
            gate.read_gate(self.path)

    def test_duplicate_keys_deep_nesting_and_non_json_are_refused(self):
        text = gate.gate_text(good()).decode().replace('"clean": true,', '"clean": true, "clean": false,')
        for data in (text.encode(), b"[" * 5000, b"not json", b"\xff\xfe", b"{}", b"null"):
            self.path.write_bytes(data)
            with self.subTest(data=data[:20]), self.assertRaises(gate.GateError):
                gate.read_gate(self.path)

    def test_a_file_edited_to_say_green_is_refused(self):
        doc = good(check="red", green=False)
        self.path.write_text(json.dumps({**doc, "green": True}), encoding="utf-8")
        with self.assertRaises(gate.GateError):
            gate.read_gate(self.path)


class ConfirmTest(unittest.TestCase):
    def test_a_gate_made_for_another_target_than_the_slice_carries_is_refused(self):
        land = land_for()
        land["slices"][0]["target"] = "0.9"
        gate.confirm(land, good(score=0.95, target="0.9", green=True))
        for target, score in ((None, None), ("0.5", 0.95)):
            with self.subTest(target=target), self.assertRaises(gate.GateError):
                gate.confirm(land, good(score=score, target=target, green=True))
        with self.assertRaises(gate.GateError):  # a slice with no target and a gate that ran one
            gate.confirm(land_for(), good(score=0.95, target="0.9", green=True))

    def test_a_gate_for_the_head_of_a_stacked_slice_is_confirmed(self):
        gate.confirm(land_for(), good())

    def test_another_commit_issue_or_slice_is_refused(self):
        land = land_for(heads=(SHA, "c" * 40), ids=("main", "two"))
        gate.confirm(land, good(id="two", sha="c" * 40))
        for doc in (good(sha="d" * 40), good(sha="c" * 40), good(issue=8), good(id="three"), good(id="two")):
            with self.subTest(doc=doc), self.assertRaises(gate.GateError):
                gate.confirm(land, doc)

    def test_a_draft_is_not_a_stacked_slice(self):
        land = land_for()
        land["slices"].append({**copy.deepcopy(land["slices"][0]), "id": "other", "outcome": "draft", "candidate": False,
                               "branch": assemble.branch_name(7, "other"), "head": "e" * 40, "commits": ["e" * 40]})
        with self.assertRaises(gate.GateError):
            gate.confirm(land, good(id="other", sha="e" * 40))


if __name__ == "__main__":
    unittest.main()
