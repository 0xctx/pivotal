"""engine/report.py: the redacted summary, the only log-derived artifact (F3b).

The raw agent log is never uploaded. The summary replaces every form of every held value with
[REDACTED:NAME], then is scanned again, and is withheld whole if anything still matches.
"""

import base64
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from engine import build, report

ROOT = Path(__file__).resolve().parent.parent
NAME = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN = b"leak-claude-oauth-0123456789abcdef"


def _forms_of(value: bytes) -> list[str]:
    return [form.decode("ascii") for form in build.forms(value)]


class SummaryTest(unittest.TestCase):
    def test_raw_hex_and_base64_are_replaced_by_the_name(self):
        log = b"raw " + TOKEN + b"\nhex " + TOKEN.hex().encode() + b"\nb64 " \
            + base64.b64encode(TOKEN) + b"\n"
        text = report.redacted_summary(log, {NAME: TOKEN}).text
        self.assertIn(f"[REDACTED:{NAME}]", text)
        for form in _forms_of(TOKEN):
            self.assertNotIn(form, text)

    def test_a_trailing_newline_secret_is_redacted_whole(self):
        value = TOKEN + b"\n"
        stripped = TOKEN
        log = (b"raw " + stripped + b"\nb64 " + base64.b64encode(stripped) + b"\nhex "
               + stripped.hex().encode() + b"\n")
        summary = report.redacted_summary(log, {NAME: value})
        self.assertFalse(summary.withheld)
        for form in (*_forms_of(stripped), *_forms_of(value)):
            self.assertNotIn(form, summary.text)

    def test_the_whole_base64_run_around_a_match_is_redacted(self):
        stream = base64.b64encode(b"zz" + TOKEN + b"-suffix-more")
        summary = report.redacted_summary(b"b64 " + stream + b"\n", {NAME: TOKEN})
        self.assertIn(f"b64 [REDACTED:{NAME}]\n", summary.text)

    def test_a_megabyte_line_without_a_secret_is_redacted_in_linear_time(self):
        b64 = base64.b64encode(os.urandom(750_000))
        hexed = os.urandom(500_000).hex().encode()
        for log in (b64, hexed, b"A" * 1_000_000):
            start = time.monotonic()
            summary = report.redacted_summary(log + b"\n", {NAME: TOKEN})
            self.assertLess(time.monotonic() - start, 5)
            self.assertFalse(summary.withheld)

    def test_a_secret_inside_a_megabyte_line_leaves_no_fragment(self):
        filler_b64 = base64.b64encode(os.urandom(375_000))
        filler_hex = os.urandom(250_000).hex().encode()
        embedded = {
            "raw": TOKEN,
            "hex": TOKEN.hex().encode(),
            "base64": base64.b64encode(b"zz" + TOKEN),
            "urlsafe": base64.urlsafe_b64encode(b"zz" + TOKEN),
        }
        for kind, piece in embedded.items():
            for filler in (filler_b64, filler_hex):
                log = filler + piece + filler + b"\n"
                start = time.monotonic()
                summary = report.redacted_summary(log, {NAME: TOKEN})
                self.assertLess(time.monotonic() - start, 5, kind)
                self.assertFalse(summary.withheld, kind)
                self.assertIn(f"[REDACTED:{NAME}]", summary.text, kind)
                for form in _forms_of(TOKEN):
                    self.assertNotIn(form, summary.text, kind)

    def test_the_summary_keeps_only_the_last_lines(self):
        log = b"".join(b"line %d\n" % n for n in range(500))
        summary = report.redacted_summary(log, {NAME: TOKEN})
        self.assertIn("500 lines, last 200 shown", summary.text)
        self.assertIn("line 499", summary.text)
        self.assertNotIn("line 299\n", summary.text)
        self.assertFalse(summary.withheld)

    def test_a_base64_string_wrapped_across_lines_withholds_the_summary(self):
        # Redaction matches whole strings, so a wrapped encoding survives it and the second scan
        # must withhold the summary rather than let it through.
        wrapped = "\n".join(textwrap.wrap(base64.b64encode(TOKEN).decode(), 8))
        summary = report.redacted_summary(wrapped.encode() + b"\n", {NAME: TOKEN})
        self.assertTrue(summary.withheld)
        self.assertIn("withheld", summary.text)
        self.assertNotIn(base64.b64encode(TOKEN)[:10].decode(), summary.text)

    def test_skipped_names_are_listed_in_the_summary(self):
        summary = report.redacted_summary(b"ok\n", {NAME: TOKEN}, skipped=["SHORT_ONE"])
        self.assertIn("not redacted (shorter than 12 bytes): SHORT_ONE", summary.text)


class SummaryCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, *args: str, env: dict[str, str]):
        return subprocess.run(
            [sys.executable, "-m", "engine.report", "summary", *args],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=60,
            env={"PATH": os.environ["PATH"], **env},
        )

    def test_writes_the_redacted_summary_and_no_value(self):
        log = self.root / "agent.log"
        log.write_bytes(b"printed " + TOKEN + b"\n")
        out = self.root / "summary.md"
        proc = self.cli("--log", str(log), "--out", str(out), "--secret", NAME,
                        env={NAME: TOKEN.decode()})
        self.assertEqual((proc.returncode, proc.stdout.splitlines()[0]), (0, "ok"))
        written = out.read_bytes()
        self.assertIn(f"[REDACTED:{NAME}]".encode(), written)
        self.assertNotIn(TOKEN, written)
        self.assertNotIn(TOKEN, proc.stdout.encode() + proc.stderr.encode())

    def test_an_unset_named_secret_writes_nothing(self):
        log = self.root / "agent.log"
        log.write_bytes(b"x\n")
        out = self.root / "summary.md"
        proc = self.cli("--log", str(log), "--out", str(out), "--secret", NAME, env={})
        self.assertEqual((proc.returncode, proc.stdout.splitlines()[0]),
                         (1, "failed: scan_incomplete"))
        self.assertFalse(out.exists())

    def test_a_withheld_summary_is_reported_and_the_value_is_not_written(self):
        log = self.root / "agent.log"
        log.write_bytes("\n".join(textwrap.wrap(base64.b64encode(TOKEN).decode(), 8)).encode())
        out = self.root / "summary.md"
        proc = self.cli("--log", str(log), "--out", str(out), "--secret", NAME,
                        env={NAME: TOKEN.decode()})
        self.assertEqual((proc.returncode, proc.stdout.splitlines()[0]), (0, "withheld"))
        self.assertNotIn(base64.b64encode(TOKEN)[:10], out.read_bytes())


if __name__ == "__main__":
    unittest.main()
