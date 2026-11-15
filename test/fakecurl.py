"""A fake curl for tests: an executable that logs its argv, its stdin and the file named by ``--data-binary @FILE``.

Tests put its directory first on PATH, so the report job runs it as a real subprocess and never reaches ntfy.
It exits with the code the test sets (0 by default) and prints nothing, as ``curl --silent`` does on success.
"""

import json
import sys
from pathlib import Path

# The flags the report job uses and whether each takes a value, from `curl --help all` (curl 8.11). A flag not
# listed is refused with curl's usage-error exit status (2). `-q` must come first, as in the real curl.
REAL_FLAGS = {
    "-q": False, "--disable": False, "-s": False, "--silent": False, "-S": False, "--show-error": False,
    "-f": False, "--fail": False, "-m": True, "--max-time": True, "--proto": True, "-K": True, "--config": True,
    "--data-binary": True,
}

_SCRIPT = """#!PYTHON
import json
import sys

FLAGS = __FLAGS__
argv = sys.argv[1:]


def refuse(message):
    sys.stderr.write("fake curl: " + message + "\\n")
    sys.exit(2)


i = 0
while i < len(argv):
    token = argv[i]
    i += 1
    if not token.startswith("-"):
        refuse("this fake takes no URL on argv: " + token)
    if token not in FLAGS:
        refuse("curl has no flag " + token + " that the report job may use")
    if token in ("-q", "--disable") and i != 1:
        refuse("-q must be the first argument")
    if FLAGS[token]:
        if i >= len(argv):
            refuse("flag " + token + " needs a value")
        i += 1
stdin = sys.stdin.read()
sent = None
for i, arg in enumerate(argv):
    if arg == "--data-binary" and i + 1 < len(argv) and argv[i + 1].startswith("@"):
        with open(argv[i + 1][1:], encoding="utf-8") as fh:
            sent = fh.read()
with open(__LOG__, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"args": argv, "stdin": stdin, "sent": sent}) + "\\n")
sys.stderr.write(__STDERR__)
sys.exit(__CODE__)
"""


class FakeCurl:
    def __init__(self, root: Path, code: int = 0, stderr: str = ""):
        self.dir = root
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "curl-calls.jsonl"
        script = (
            _SCRIPT.replace("#!PYTHON", "#!" + sys.executable)
            .replace("__FLAGS__", repr(REAL_FLAGS))
            .replace("__LOG__", repr(str(self.log)))
            .replace("__STDERR__", repr(stderr))
            .replace("__CODE__", str(code))
        )
        path = self.dir / "curl"
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
