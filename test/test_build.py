"""engine/build.py: the secret scan of a patch (F3b).

The scan runs as the job runs it, `python3 -m engine.build scan`, on a patch file with the secret
values in the environment. The end-to-end tests run a fake agent through the real engine/run.sh
`agent_run`, in a real git repository. Secret values are fakes; no test calls a real provider.
"""

import base64
import os
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from engine import build

ROOT = Path(__file__).resolve().parent.parent
RUN_SH = str(ROOT / "engine" / "run.sh")
NAME = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN = b"leak-claude-oauth-0123456789abcdef"
# Its base64, wrapped at 8 characters, has a line that starts with "+", which the scan must join.
WRAP_VALUE = b"leak-\xee\xf9Z`\xe5aC\xd6\xc4;\xca\xd7-tail"


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    """A child environment: PATH and the given variables, so no real secret reaches a test."""
    return {"PATH": os.environ["PATH"], **extra}


def _patch(root: Path, name: str, data: bytes, message: str = "change") -> bytes:
    """A patch that adds name with data, made by git format-patch in a scratch repository."""
    repo = Path(tempfile.mkdtemp(dir=root))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run([*git, "add", "base.txt"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=repo, check=True)
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", message], cwd=repo, check=True)
    return subprocess.run(
        ["git", "format-patch", "-1", "--stdout"], cwd=repo, check=True, capture_output=True
    ).stdout


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def scan(self, patch: bytes, environ=None):
        env = {NAME.encode(): TOKEN} if environ is None else environ
        return build.scan(patch, [NAME], env)

    def scan_cli(self, patch: Path, *extra: str, env: dict[str, str] | None = None):
        environ = {NAME: TOKEN.decode()} if env is None else env
        return subprocess.run(
            [sys.executable, "-m", "engine.build", "scan", "--patch", str(patch),
             "--secret", NAME, *extra],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
            env=_clean_env(environ),
        )


class FormsTest(unittest.TestCase):
    def test_raw_hex_and_base64_forms_of_a_value(self):
        found = build.forms(TOKEN)
        self.assertEqual(found[TOKEN], "raw")
        self.assertEqual(found[TOKEN.hex().encode()], "hex")
        self.assertEqual(found[TOKEN.hex().upper().encode()], "hex-upper")
        # At alignment 0 the base64 is the start of the encoding, minus the last partial group.
        whole_groups = base64.b64encode(TOKEN)[: 4 * (len(TOKEN) // 3)]
        self.assertEqual(found[whole_groups], "base64")

    def test_base64_is_found_at_every_alignment_with_any_bytes_around_it(self):
        # The value may start at any offset in a stream, with any bytes before and after it, and
        # the stream may use either alphabet. Each case must contain one of the value's forms.
        rng = random.Random(7)
        for _ in range(400):
            value = bytes(rng.randrange(33, 127) for _ in range(rng.randint(12, 60)))
            before = bytes(rng.randrange(256) for _ in range(rng.randint(0, 5)))
            after = bytes(rng.randrange(256) for _ in range(rng.randint(0, 7)))
            for encode in (base64.b64encode, base64.urlsafe_b64encode):
                stream = encode(before + value + after).decode()
                with self.subTest(value=value, before=before, after=after):
                    found = [
                        needle for needle, kind in build.forms(value).items()
                        if kind.startswith("base64") and needle.decode() in stream
                    ]
                    self.assertTrue(found)

    def test_padded_and_unpadded_encodings_are_both_found(self):
        value = b"leak-13-bytes!"  # 13 bytes: the encoding ends with "=" padding
        padded = base64.b64encode(value).decode()
        self.assertTrue(padded.endswith("="))
        base64_needles = [n for n, kind in build.forms(value).items() if kind.startswith("base64")]
        for stream in (padded, padded.rstrip("=")):
            with self.subTest(stream=stream):
                self.assertTrue(any(n.decode() in stream for n in base64_needles))


class ScanTest(ScanTestCase):
    def test_a_clean_patch_is_ok(self):
        result = self.scan(_patch(self.root, "notes.txt", b"nothing here\n"))
        self.assertEqual((result.result, result.leaks), (build.RESULT_OK, []))

    def test_raw_token_in_an_added_line_is_a_leak(self):
        patch = _patch(self.root, "notes.txt", TOKEN + b"\n")
        result = self.scan(patch)
        self.assertEqual(result.result, build.RESULT_LEAK)
        self.assertIn((NAME, "raw"), result.leaks)

    def test_base64_token_is_a_leak(self):
        encoded = base64.b64encode(TOKEN) + b"\n"
        result = self.scan(_patch(self.root, "notes.txt", encoded))
        self.assertEqual(result.result, build.RESULT_LEAK)
        self.assertIn((NAME, "base64"), result.leaks)

    def test_hex_token_in_either_case_is_a_leak(self):
        for text in (TOKEN.hex().encode(), TOKEN.hex().upper().encode()):
            with self.subTest(text=text[:8]):
                result = self.scan(_patch(self.root, "notes.txt", text + b"\n"))
                self.assertEqual(result.result, build.RESULT_LEAK)
                self.assertTrue(any(form.startswith("hex") for _, form in result.leaks))

    def test_token_in_an_added_file_name_is_a_leak(self):
        name = "out-" + TOKEN.decode() + ".txt"
        result = self.scan(_patch(self.root, name, b"x\n"))
        self.assertEqual(result.result, build.RESULT_LEAK)
        self.assertIn((NAME, "raw"), result.leaks)

    def test_base64_wrapped_across_lines_is_a_leak(self):
        # Lines of 8 characters; git adds its "+" marker to each, and one line starts with "+".
        lines = textwrap.wrap(base64.b64encode(WRAP_VALUE).decode(), 8)
        self.assertTrue(any(line.startswith("+") for line in lines[1:]))
        body = "\n".join(lines) + "\n"
        patch = _patch(self.root, "notes.txt", body.encode("ascii"))
        result = self.scan(patch, environ={NAME.encode(): WRAP_VALUE})
        self.assertEqual(result.result, build.RESULT_LEAK)
        self.assertTrue(any(form.startswith("base64") for _, form in result.leaks))

    def test_hex_wrapped_across_lines_is_a_leak(self):
        lines = textwrap.wrap(TOKEN.hex(), 10)
        body = "\n".join(lines) + "\n"
        result = self.scan(_patch(self.root, "notes.txt", body.encode("ascii")))
        self.assertEqual(result.result, build.RESULT_LEAK)

    def test_token_in_a_binary_file_is_a_leak(self):
        # format-patch writes a binary file as base85 of zlib data. The scan must see inside it.
        data = b"\x00\x01binary\x02" + TOKEN + b"\x03"
        patch = _patch(self.root, "blob.bin", data)
        self.assertIn(b"GIT binary patch", patch)
        self.assertNotIn(TOKEN, patch)
        result = self.scan(patch)
        self.assertEqual(result.result, build.RESULT_LEAK)
        self.assertIn((NAME, "raw"), result.leaks)

    def test_token_inserted_into_a_binary_file_as_a_delta_is_a_leak(self):
        # A change to a file that exists in the base is sent as a delta. Its inserted bytes are in
        # the deflated delta stream, which the scan must also decode.
        base = random.Random(3).randbytes(4000)
        repo = self.root / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
        (repo / "big.bin").write_bytes(base)
        subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
        subprocess.run([*git, "commit", "-qm", "base"], cwd=repo, check=True)
        (repo / "big.bin").write_bytes(base[:100] + TOKEN + base[100:])
        subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
        subprocess.run([*git, "commit", "-qm", "change"], cwd=repo, check=True)
        patch = subprocess.run(
            ["git", "format-patch", "-1", "--stdout"], cwd=repo, check=True, capture_output=True
        ).stdout
        self.assertIn(b"\ndelta ", patch)
        self.assertNotIn(TOKEN, patch)
        result = self.scan(patch)
        self.assertEqual(result.result, build.RESULT_LEAK)

    def test_a_secret_wrapped_in_the_commit_message_is_a_leak(self):
        # A continuation line that starts with "+", "-" or a space must not lose that character.
        for first, rest in (("abcdefgh", "+ijklmnop-qrstuv"), ("abcdefgh", "-ijklmnop-qrstuv"),
                            ("abcdefgh", " ijklmnop-qrstuv")):
            value = (first + rest).replace(" ", "").encode()
            with self.subTest(rest=rest[:1]):
                patch = _patch(self.root, "notes.txt", b"clean\n", f"m\n\n{first}\n{rest}")
                result = self.scan(patch, environ={NAME.encode(): value})
                self.assertEqual(result.result, build.RESULT_LEAK)

    def test_a_multi_line_value_committed_as_a_file_is_a_leak(self):
        value = b"-----BEGIN KEY-----\nabcdefghijklmnop\n-----END KEY-----"
        result = self.scan(_patch(self.root, "key.pem", value + b"\n"), environ={NAME.encode(): value})
        self.assertEqual(result.result, build.RESULT_LEAK)
        self.assertIn((NAME, "raw"), result.leaks)

    def test_a_value_with_a_trailing_newline_is_caught_by_its_stripped_forms(self):
        stripped = TOKEN
        environ = {NAME.encode(): stripped + b"\n"}
        for text in (stripped, stripped.hex().encode(), base64.b64encode(stripped)):
            with self.subTest(text=text[:6]):
                result = self.scan(_patch(self.root, "notes.txt", text + b"\n"), environ=environ)
                self.assertEqual(result.result, build.RESULT_LEAK)

    def test_a_non_ascii_file_name_holding_the_value_is_a_leak(self):
        value = "tökén-ünïcode-0123".encode()
        patch = _patch(self.root, "out-" + value.decode() + ".txt", b"x\n")
        self.assertNotIn(value, patch)  # git quotes the name
        self.assertEqual(self.scan(patch, environ={NAME.encode(): value}).result, build.RESULT_LEAK)

    def test_a_value_split_across_directories_in_a_file_name_is_a_leak(self):
        patch = _patch(self.root, "leak-claude-/oauth-0123456789/abcdef.txt", b"x\n")
        self.assertEqual(self.scan(patch).result, build.RESULT_LEAK)

    def test_an_unset_secret_argument_that_is_a_value_is_not_printed(self):
        value = "ghp_realLookingValue0123456789"
        result = build.scan(b"clean\n", [value], {})
        self.assertEqual(result.result, build.RESULT_INCOMPLETE)
        self.assertNotIn(value, result.problem)

    def test_an_unset_secret_name_is_shown(self):
        self.assertIn(NAME, build.scan(b"clean\n", [NAME], {}).problem)

    def test_a_short_value_is_skipped_by_name_and_not_scanned(self):
        short = b"tok"  # below the floor: it would match ordinary text
        patch = _patch(self.root, "notes.txt", b"a tok in ordinary text\n")
        result = self.scan(patch, environ={NAME.encode(): short})
        self.assertEqual((result.result, result.leaks), (build.RESULT_OK, []))
        self.assertEqual(result.skipped, [NAME])

    def test_an_unset_named_secret_is_incomplete(self):
        result = self.scan(_patch(self.root, "notes.txt", b"x\n"), environ={})
        self.assertEqual(result.result, build.RESULT_INCOMPLETE)
        self.assertIn(NAME, result.problem)

    def test_a_malformed_binary_section_is_incomplete(self):
        patch = b"GIT binary patch\nliteral 3\nH!not base85 at all\n\n"
        result = self.scan(patch)
        self.assertEqual(result.result, build.RESULT_INCOMPLETE)

    def test_a_binary_section_with_no_data_is_incomplete(self):
        result = self.scan(b"GIT binary patch\nliteral 3\n\n")
        self.assertEqual(result.result, build.RESULT_INCOMPLETE)

    def test_a_blob_over_the_size_cap_is_incomplete(self):
        # A small zlib stream of zeros that inflates past the cap must not be read into memory.
        patch = _patch(self.root, "blob.bin", b"\x00" * 5000)
        with mock.patch.object(build, "MAX_BLOB_BYTES", 1000):
            result = self.scan(patch)
        self.assertEqual(result.result, build.RESULT_INCOMPLETE)

    def test_a_job_secret_set_in_the_environment_is_scanned_too(self):
        # GH_TOKEN was not named, but the job holds it, so a patch with its value is a leak.
        gh = b"leak-gh-token-0123456789abcdef"
        patch = _patch(self.root, "notes.txt", gh + b"\n")
        result = self.scan(patch, environ={NAME.encode(): TOKEN, b"GH_TOKEN": gh})
        self.assertEqual(result.result, build.RESULT_LEAK)
        self.assertIn(("GH_TOKEN", "raw"), result.leaks)

    def test_the_result_names_a_secret_but_never_prints_its_value(self):
        result = self.scan(_patch(self.root, "notes.txt", TOKEN + b"\n"))
        rendered = repr(result)
        self.assertIn(NAME, rendered)
        self.assertNotIn(TOKEN.decode(), rendered)


class ScanCliTest(ScanTestCase):
    def test_ok_keeps_the_patch(self):
        patch = self.root / "slice.patch"
        patch.write_bytes(_patch(self.root, "notes.txt", b"clean\n"))
        proc = self.scan_cli(patch)
        self.assertEqual((proc.returncode, proc.stdout.splitlines()[0]), (0, "ok"))
        self.assertTrue(patch.exists())

    def test_a_leak_prints_the_name_and_form_and_discards_the_patch(self):
        patch = self.root / "slice.patch"
        patch.write_bytes(_patch(self.root, "notes.txt", TOKEN + b"\n"))
        proc = self.scan_cli(patch)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout.splitlines()[0], "failed: secret_in_patch")
        self.assertIn(f"leak: {NAME} raw", proc.stdout)
        self.assertFalse(patch.exists())

    def test_no_output_carries_a_secret_value(self):
        patch = self.root / "slice.patch"
        patch.write_bytes(_patch(self.root, "notes.txt", TOKEN + b"\n"))
        proc = self.scan_cli(patch)
        self.assertEqual(proc.returncode, 1)
        for text in (proc.stdout, proc.stderr):
            self.assertNotIn(TOKEN.decode(), text)
            self.assertNotIn(base64.b64encode(TOKEN).decode(), text)
            self.assertNotIn(TOKEN.hex(), text)

    def test_an_unset_named_secret_discards_the_patch(self):
        patch = self.root / "slice.patch"
        patch.write_bytes(_patch(self.root, "notes.txt", b"clean\n"))
        proc = self.scan_cli(patch, env={})
        self.assertEqual(proc.stdout.splitlines()[0], "failed: scan_incomplete")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(patch.exists())

    def test_a_missing_patch_is_incomplete_and_fails(self):
        proc = self.scan_cli(self.root / "no-such.patch")
        self.assertEqual(proc.stdout.splitlines()[0], "failed: scan_incomplete")
        self.assertEqual(proc.returncode, 1)

    def test_refuses_a_secret_name_that_is_not_an_environment_name(self):
        patch = self.root / "slice.patch"
        patch.write_bytes(b"clean\n")
        proc = subprocess.run(
            [sys.executable, "-m", "engine.build", "scan", "--patch", str(patch),
             "--secret", "bad name"],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(patch.exists())

    def test_needs_at_least_one_named_secret(self):
        patch = self.root / "slice.patch"
        patch.write_bytes(b"clean\n")
        proc = subprocess.run(
            [sys.executable, "-m", "engine.build", "scan", "--patch", str(patch)],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)


class EndToEndTest(ScanTestCase):
    """A fake agent, run through the real run.sh agent_run in a git worktree, then the scan."""

    def agent_patch(self, agent_body: str) -> tuple[Path, Path, Path]:
        """Runs the fake agent, commits its work and writes the patch. Returns the patch, the
        agent log and the worktree."""
        bins = self.root / "bin"
        bins.mkdir()
        agent = bins / "claude"
        agent.write_text("#!/bin/bash\n" + agent_body, encoding="utf-8")
        agent.chmod(0o755)
        worktree = self.root / "worktree"
        worktree.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], cwd=worktree, check=True)
        (worktree / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run([*git, "add", "-A"], cwd=worktree, check=True)
        subprocess.run([*git, "commit", "-qm", "base"], cwd=worktree, check=True)
        log = self.root / "agent.log"
        env = {
            "PATH": f"{bins}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            NAME: TOKEN.decode(),
        }
        run = subprocess.run(
            ["bash", RUN_SH, "agent_run", "--max-time", "30", "--log", str(log),
             "--token-env", NAME, "--", "claude"],
            cwd=worktree, capture_output=True, text=True, check=False, timeout=120, env=env,
        )
        self.assertEqual(run.stdout, "ok\n")
        subprocess.run([*git, "add", "-A"], cwd=worktree, check=True)
        subprocess.run([*git, "commit", "-qm", "agent"], cwd=worktree, check=True)
        patch = self.root / "slice.patch"
        patch.write_bytes(
            subprocess.run(
                ["git", "format-patch", "-1", "--stdout"], cwd=worktree, check=True,
                capture_output=True,
            ).stdout
        )
        return patch, log, worktree

    def test_a_fake_agent_that_leaks_raw_base64_and_hex_is_caught(self):
        patch, _, _ = self.agent_patch(
            'echo "$CLAUDE_CODE_OAUTH_TOKEN"\n'
            '{\n'
            '  printf "%s\\n" "$CLAUDE_CODE_OAUTH_TOKEN"\n'
            '  printf "%s" "$CLAUDE_CODE_OAUTH_TOKEN" | od -An -tx1 | tr -d " \\n"; echo\n'
            '  printf "%s" "$CLAUDE_CODE_OAUTH_TOKEN" | base64 -w0; echo\n'
            '} > notes.txt\n'
        )
        proc = self.scan_cli(patch)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout.splitlines()[0], "failed: secret_in_patch")
        for form in ("raw", "hex", "base64"):
            self.assertIn(f"leak: {NAME} {form}", proc.stdout)
        self.assertFalse(patch.exists())

    def test_the_token_is_in_no_artifact_once_the_patch_is_discarded(self):
        patch, log, _ = self.agent_patch(
            'echo "$CLAUDE_CODE_OAUTH_TOKEN"\n'
            'printf "%s" "$CLAUDE_CODE_OAUTH_TOKEN" | base64 -w0 > notes.txt\n'
        )
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        proc = self.scan_cli(patch)
        self.assertEqual(proc.stdout.splitlines()[0], "failed: secret_in_patch")
        # The redacted summary is the only log-derived artifact. The raw log stays where it is.
        summary = subprocess.run(
            [sys.executable, "-m", "engine.report", "summary", "--log", str(log),
             "--out", str(artifacts / "summary.md"), "--secret", NAME],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
            env=_clean_env({NAME: TOKEN.decode()}),
        )
        self.assertEqual(summary.returncode, 0)
        self.assertTrue(log.read_bytes().count(TOKEN))  # the raw log does hold the token
        self.assertEqual(sorted(p.name for p in artifacts.iterdir()), ["summary.md"])
        for path in (artifacts / "summary.md", patch):
            if path.exists():
                self.assertNotIn(TOKEN, path.read_bytes())
        for text in (proc.stdout, proc.stderr, summary.stdout, summary.stderr):
            self.assertNotIn(TOKEN.decode(), text)

    def test_a_clean_agent_gives_an_ok_patch(self):
        patch, _, _ = self.agent_patch('echo done\nprintf "hello\\n" > notes.txt\n')
        proc = self.scan_cli(patch)
        self.assertEqual((proc.returncode, proc.stdout.splitlines()[0]), (0, "ok"))
        self.assertTrue(patch.exists())


if __name__ == "__main__":
    unittest.main()
