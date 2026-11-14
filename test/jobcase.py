"""A base for the ack and plan job tests: a fake gh on PATH, a GITHUB_OUTPUT file and the event file.

The jobs run `gh` by name, so the fake's directory comes first on PATH. No test reaches GitHub.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

import fakegh

REPO = "0xctx/piv-sandbox"
ENGINE_SHA = "e" * 40
ISSUE = 7
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# Text that two implementations of the pin could hash differently: non-ASCII, CRLF, an emoji (a surrogate
# pair in JSON) and a lone quote.
TITLE = 'Fix the café menu "now" 🍕'
BODY = "Line one\r\nLine two — naïve ünï\r\n\r\nEmoji: 🚀 and 日本語\n"
# sha256 of the ASCII JSON array [TITLE, BODY], with \u escapes (surrogate pairs for the emoji), \r\n kept
# as escapes. Computed by hand-escaping, not by json, so another implementation can check itself against it.
PIN = "c46bb0d5d74c1957e956ff2f66f9ea79f95546e64d9721fdb5bd6fac380968e8"


def issue_json(number: int = ISSUE, title: str = TITLE, body: str | None = BODY, **extra) -> str:
    return json.dumps({"number": number, "title": title, "body": body, **extra})


class JobCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.gh = fakegh.FakeGh(self.root / "bin")
        self.out = self.root / "output"
        self.event = self.root / "event.json"

    def env(self, **over: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.gh.dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "GH_TOKEN": "fake-token",
            "GH_REPO": REPO,
            "KIND": "go",
            "TRUSTED": "true",
            "CHECK": "true",
            "ISSUE": str(ISSUE),
            "ISSUE_PIN": PIN,
            "PRESET": "claude-sub",
            "RUN_ID": "42",
            "RUN_NUMBER": "5",
            "ENGINE_SHA": ENGINE_SHA,
            "GITHUB_OUTPUT": str(self.out),
            "GITHUB_EVENT_PATH": str(self.event),
        }
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def write_event(self, data: dict) -> None:
        self.event.write_text(json.dumps(data), encoding="utf-8")

    def outputs(self) -> dict[str, str]:
        if not self.out.exists():
            return {}
        return dict(line.split("=", 1) for line in self.out.read_text(encoding="utf-8").splitlines())

    def writes(self) -> list[dict]:
        """The gh calls that write: an explicit write method on `gh api`."""
        found = []
        for call in self.gh.calls():
            args = call["args"]
            if any(a in WRITE_METHODS for a in args) or any(a in ("-f", "-F", "--field", "--raw-field") for a in args):
                found.append(call)
        return found
