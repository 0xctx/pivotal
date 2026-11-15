"""The one place piv runs gh (stateless: everything through gh). serve.py runs kaggle
through it too.

``PIV_GH`` names the gh binary, so a test puts a fake first. A failure raises ``GhFailed`` with the
tool's own first line of stderr, or with no detail when the caller asks for none. A value that goes in on
stdin is never in an argument or an error, so it cannot be echoed back from here.
"""

import json
import re
import subprocess

TIMEOUT_SECONDS = 60
HTTP_STATUS = re.compile(r"HTTP (\d{3})")
RAW = "application/vnd.github.raw"


class PivError(Exception):
    """A failure to report. ``code`` is the exit status: 2 for input the user can fix, 1 otherwise."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class GhFailed(PivError):
    """The tool exited non-zero. ``status`` is the HTTP status from its message, when it gave one."""

    def __init__(self, message: str, status: int | None):
        super().__init__(message)
        self.status = status


class Gh:
    tool = "gh"  # the name a message gives the binary
    words = 3  # how many leading arguments a message names

    def __init__(self, binary: str, env: dict[str, str]):
        self.binary = binary
        self.env = env

    def run(self, *args: str, stdin: str = "", echo_stderr: bool = True) -> str:
        command = " ".join(args[: self.words])
        try:
            payload = stdin.encode("utf-8")
        except UnicodeEncodeError:
            # The message names the command, which carries a secret's name, and never where the bad character is.
            raise PivError(
                f"{self.tool} {command}: its input is not valid UTF-8 text, so {self.tool} was not run"
            ) from None
        try:
            done = subprocess.run(
                [self.binary, *args],
                input=payload,
                capture_output=True,
                check=False,
                env=self.env,
                timeout=TIMEOUT_SECONDS,
            )
        except OSError as err:
            raise PivError(f"cannot run {self.binary}: {err.strerror}") from None
        except subprocess.TimeoutExpired:
            raise PivError(f"{self.tool} {command} timed out") from None
        stderr = done.stderr.decode("utf-8", errors="replace")
        if done.returncode != 0:
            status = HTTP_STATUS.search(stderr)
            detail = stderr.strip().splitlines()[0] if echo_stderr and stderr.strip() else ""
            message = f"{self.tool} {command} failed (exit {done.returncode})"
            raise GhFailed(f"{message}: {detail}" if detail else message, int(status.group(1)) if status else None)
        try:
            return done.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise PivError(f"{self.tool} {command} answered with output that is not UTF-8 text") from None

    def api(
        self, path: str, *, method: str = "GET", body: object = None, raw: bool = False, include: bool = False
    ) -> str:
        args = ["api"]
        if method != "GET":
            args += ["-X", method]
        if raw:
            args += ["-H", f"Accept: {RAW}"]
        args.append(path)
        if include:  # the status line and headers come first in the output
            args.append("-i")
        if body is None:
            return self.run(*args)
        return self.run(*args, "--input", "-", stdin=json.dumps(body))

    def api_json(self, path: str, *, method: str = "GET", body: object = None) -> dict:
        """A JSON object from the API. Anything else is a failure, never a guess (fail closed)."""
        answer = self.api(path, method=method, body=body)
        try:
            data = json.loads(answer)
        except ValueError:
            raise PivError(f"gh api {path} did not answer with JSON") from None
        if not isinstance(data, dict):
            raise PivError(f"gh api {path} answered with something other than an object")
        return data

    def api_list(self, path: str) -> list:
        """A JSON array from the API (a listing). Anything else is a failure, never a guess (fail closed)."""
        answer = self.api(path)
        try:
            data = json.loads(answer)
        except ValueError:
            raise PivError(f"gh api {path} did not answer with JSON") from None
        if not isinstance(data, list):
            raise PivError(f"gh api {path} answered with something other than a list")
        return data

    def api_text(self, path: str) -> str:
        return self.api(path, raw=True)
