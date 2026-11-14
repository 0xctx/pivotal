"""Fake CLIs for tests: executables that print scripted output and exit with a scripted code.

Tests put a FakeBin's directory first on PATH and call the fake by name, so the code under test
goes through the same subprocess path it uses in a job. With ``log=``, each call appends its argv
to that file as one JSON list per line.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_TEMPLATE = """#!{python}
import json
import os
import sys
import time

time.sleep({sleep})
sys.stdout.write({stdout})
sys.stderr.write({stderr})
if {log}:
    with open({log}, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
if {env_log}:
    with open({env_log}, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
sys.exit({code})
"""

_BROWSER = """#!{python}
import json
import sys
import time

source = {source}
if {log}:
    with open({log}, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
time.sleep({sleep})
sys.stderr.write({stdout})  # text on stderr: a browser's words are never the PNG on its stdout
if source is not None:  # the PNG is written to stdout whatever the exit code: a failing browser with a good PNG is a test case
    with open(source, "rb") as src:
        sys.stdout.buffer.write(src.read())
sys.stdout.flush()
sys.exit({code})
"""

_ROUTED = """#!{python}
import json
import sys

routes = json.loads({routes})
argv = " ".join(sys.argv[1:])
for needle, stdout, code in routes:
    if needle in argv:
        sys.stdout.write(stdout)
        sys.exit(code)
sys.stderr.write("gh: Not Found (HTTP 404)\\n")
sys.exit(1)
"""

# A fake that emits pi-shaped JSONL on stdout (--mode json) and an optional stderr string.
# The final assistant text is emitted as a message_end event with role "assistant".
# Event names from: ~/.local/lib/node_modules/@earendil-works/pi-coding-agent/docs/json.md
#   "message_end" (authoritative final message), "agent_start", "agent_end", "agent_settled"
# Message field shapes from: ~/.local/lib/node_modules/@earendil-works/pi-coding-agent/docs/message-types.md
#   AssistantMessage.role == "assistant", AssistantMessage.content is list of TextContent (type "text", field "text")
_PI_JSON = '''#!{python}
import json, sys, time
time.sleep({sleep})
sys.stderr.write({stderr})
sys.stderr.flush()
text = {text}
events = [
    {{"type": "session", "version": 3, "id": "fake-session", "timestamp": "2024-01-01T00:00:00.000Z", "cwd": "/"}},
    {{"type": "agent_start"}},
    {{"type": "turn_start"}},
    {{"type": "message_start", "message": {{"role": "user", "content": "prompt", "timestamp": 0}}}},
    {{"type": "message_end", "message": {{"role": "user", "content": "prompt", "timestamp": 0}}}},
    {{"type": "message_start", "message": {{"role": "assistant", "content": [], "stopReason": "pending", "timestamp": 0}}}},
    {{"type": "message_end", "message": {{"role": "assistant",
        "content": [{{"type": "text", "text": text}}],
        "stopReason": "stop", "api": "fake", "provider": "fake", "model": "fake",
        "usage": {{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                   "cost": {{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}}},
        "timestamp": 0}}}},
    {{"type": "turn_end", "message": {{"role": "assistant", "content": [{{"type": "text", "text": text}}],
        "stopReason": "stop", "api": "fake", "provider": "fake", "model": "fake",
        "usage": {{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                   "cost": {{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}}},
        "timestamp": 0}}, "toolResults": []}},
    {{"type": "agent_end", "messages": [], "willRetry": False}},
    {{"type": "agent_settled", "aborted": False}},
]
for ev in events:
    sys.stdout.write(json.dumps(ev) + "\\n")
sys.stdout.flush()
if {log}:
    with open({log}, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit({code})
'''

# A fake that emits claude-shaped JSON on stdout (--output-format json) and an optional stderr string.
_CLAUDE_JSON = '''#!{python}
import json, sys, time
time.sleep({sleep})
sys.stderr.write({stderr})
sys.stderr.flush()
text = {text}
doc = {{"type": "result", "subtype": "success", "result": text,
        "is_error": False, "cost_usd": 0, "duration_ms": 0, "session_id": "fake"}}
sys.stdout.write(json.dumps(doc) + "\\n")
sys.stdout.flush()
if {log}:
    with open({log}, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit({code})
'''


class FakeBin:
    def __init__(self, root: Path):
        self.dir = root
        self.dir.mkdir(parents=True, exist_ok=True)

    def add(
        self,
        name: str,
        *,
        stdout: str = "",
        stderr: str = "",
        code: int = 0,
        log: Path | None = None,
        env_log: Path | None = None,
        sleep: float = 0,
    ) -> Path:
        path = self.dir / name
        path.write_text(
            _TEMPLATE.format(
                python=sys.executable,
                stdout=repr(stdout),
                stderr=repr(stderr),
                log=repr(str(log)) if log else "None",
                env_log=repr(str(env_log)) if env_log else "None",
                code=int(code),
                sleep=float(sleep),
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def add_browser(
        self,
        name: str,
        *,
        source: bytes | None,
        stdout: str = "",
        code: int = 0,
        log: Path | None = None,
        sleep: float = 0,
    ) -> Path:
        """A fake headless browser: called as ``<browser> URL OUT WIDTH HEIGHT``, it writes ``source`` bytes to OUT
        (nothing when ``source`` is None) and exits with ``code``. ``stdout`` is printed, so a test can see it is not the score."""
        src = self.dir / (name + ".src")
        if source is not None:
            src.write_bytes(source)
        path = self.dir / name
        path.write_text(
            _BROWSER.format(
                python=sys.executable,
                source=repr(str(src)) if source is not None else "None",
                stdout=repr(stdout),
                code=int(code),
                log=repr(str(log)) if log else "None",
                sleep=float(sleep),
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def add_routed(self, name: str, routes: dict[str, tuple[str, int]]) -> Path:
        """A fake whose answer depends on the call: the first route whose key appears in argv wins.

        A call that matches no route exits 1 with HTTP 404, as gh does for an unknown user.
        """
        path = self.dir / name
        table = [[needle, stdout, code] for needle, (stdout, code) in routes.items()]
        path.write_text(
            _ROUTED.format(python=sys.executable, routes=repr(json.dumps(table))),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def add_pi_json(
        self,
        name: str,
        *,
        text: str,
        stderr: str = "",
        code: int = 0,
        log: Path | None = None,
        sleep: float = 0,
    ) -> Path:
        """A fake pi CLI that emits pi-shaped JSONL (--mode json) on stdout with the given final assistant text.
        stderr is written to stderr before the events. The fake logs its argv to log when given."""
        path = self.dir / name
        path.write_text(
            _PI_JSON.format(
                python=sys.executable,
                text=repr(text),
                stderr=repr(stderr),
                log=repr(str(log)) if log else "None",
                code=int(code),
                sleep=float(sleep),
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def add_claude_json(
        self,
        name: str,
        *,
        text: str,
        stderr: str = "",
        code: int = 0,
        log: Path | None = None,
        sleep: float = 0,
    ) -> Path:
        """A fake claude CLI that emits claude-shaped JSON (--output-format json) on stdout with the given final text.
        stderr is written to stderr before the JSON. The fake logs its argv to log when given."""
        path = self.dir / name
        path.write_text(
            _CLAUDE_JSON.format(
                python=sys.executable,
                text=repr(text),
                stderr=repr(stderr),
                log=repr(str(log)) if log else "None",
                code=int(code),
                sleep=float(sleep),
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def add_omp_json(
        self,
        name: str,
        *,
        text: str,
        stderr: str = "",
        code: int = 0,
        log: Path | None = None,
        sleep: float = 0,
    ) -> Path:
        """A fake omp CLI that emits omp-shaped JSONL (--mode json) on stdout with the given final assistant text.
        stderr is written to stderr before the events. The fake logs its argv to log when given.
        omp uses the same JSONL event structure as pi (message_end with role assistant)."""
        path = self.dir / name
        path.write_text(
            _PI_JSON.format(
                python=sys.executable,
                text=repr(text),
                stderr=repr(stderr),
                log=repr(str(log)) if log else "None",
                code=int(code),
                sleep=float(sleep),
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        env["PATH"] = os.pathsep.join([str(self.dir), env.get("PATH", "")])
        return env

    def run(
        self, name: str, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.dir / name), *args],
            capture_output=True,
            text=True,
            check=False,
            env=self.env() if env is None else env,
        )
