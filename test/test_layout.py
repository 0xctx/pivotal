import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The layout. Files that a later slice creates (catalog.json, serve.ipynb.tmpl)
# are not listed until their slice lands.
FILES = [
    ".github/workflows/factory.yml",
    ".github/workflows/land.yml",
    ".github/workflows/ci.yml",
    "engine/setup/action.yml",
    "engine/route.py",
    "engine/trust.py",
    "engine/plan.py",
    "engine/run.sh",
    "engine/ack.py",
    "engine/build.py",
    "engine/review.py",
    "engine/assemble.py",
    "engine/gate.py",
    "engine/publish.py",
    "engine/report.py",
    "engine/land.py",
    "engine/prompts/ci-rules.md",
    "engine/prompts/build.md",
    "engine/prompts/review.md",
    "engine/prompts/incident.md",
    "engine/prompts/mirror.md",
    "engine/prompts/docs.md",
    "engine/score/image.py",
    "templates/caller.yml",
    "kaggle/serve.ipynb.tmpl",
    "README.md",
    "AGENTS.md",
    "CHANGELOG.md",
    ".claude/sysop.json",
]

DIRS = [
    "presets",
    "lines",
    "kaggle",
    "gh-piv",
    "docs",
    "test",
]


class LayoutTest(unittest.TestCase):
    def test_every_planned_file_exists(self):
        missing = [p for p in FILES if not (ROOT / p).is_file()]
        self.assertEqual(missing, [])

    def test_every_planned_directory_exists(self):
        missing = [d for d in DIRS if not (ROOT / d).is_dir()]
        self.assertEqual(missing, [])

    def test_every_python_module_parses(self):
        for path in FILES:
            if path.endswith(".py"):
                with self.subTest(path=path):
                    ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)


if __name__ == "__main__":
    unittest.main()
