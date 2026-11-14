"""engine/doccheck.py: the repeated-word check must not count words that a code span separates (the docs line's gate)."""

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import doccheck  # noqa: E402


def problems(text: str) -> list[str]:
    return doccheck.check_text(text, ROOT / "a.md", ROOT)


class RepeatedWordTest(unittest.TestCase):
    def test_a_code_span_between_two_words_is_not_a_repeat(self):
        # The text reads "in SERIAL in run_suite.py": the words are not written twice.
        self.assertEqual(problems("the modules in `SERIAL` in `test/run_suite.py` run alone\n"), [])

    def test_or_around_code_spans_is_not_a_repeat(self):
        # docs/RUNNER.md:122, a false positive before this fix: "read `environ` or `mem` or attach".
        self.assertEqual(problems("cannot read `<pid>/environ` or `mem` or attach\n"), [])

    def test_a_word_written_twice_in_prose_is_still_caught(self):
        found = problems("the word or or here\n")
        self.assertEqual(len(found), 1, found)
        self.assertIn("'or' is written twice", found[0])

    def test_a_word_written_twice_across_a_code_span_boundary_is_still_caught(self):
        found = problems("this is the the `x` code\n")
        self.assertEqual(len(found), 1, found)

    def test_the_repo_markdown_has_no_problems(self):
        out = io.StringIO()
        self.assertEqual(doccheck.check(ROOT, out), 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
