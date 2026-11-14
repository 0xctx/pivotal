import json
import tempfile
import unittest
from pathlib import Path

import fakecli


class FakeCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bins = fakecli.FakeBin(self.root / "bin")

    def tearDown(self):
        self._tmp.cleanup()

    def test_prints_scripted_output_and_exit_code(self):
        self.bins.add("agent", stdout="done\n", stderr="warn: slow\n", code=3)
        proc = self.bins.run("agent")
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (3, "done\n", "warn: slow\n"))

    def test_logs_each_call_argv_when_asked(self):
        log = self.root / "calls.jsonl"
        self.bins.add("agent", log=log)
        self.bins.run("agent", "--print", "task with spaces")
        self.bins.run("agent")
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls, [["--print", "task with spaces"], []])

    def test_is_found_first_on_path_when_env_is_built_from_it(self):
        self.bins.add("agent", stdout="fake\n")
        env = self.bins.env({"PATH": "/usr/bin"})
        self.assertEqual(env["PATH"].split(":")[0], str(self.bins.dir))


if __name__ == "__main__":
    unittest.main()
