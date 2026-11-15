"""A fake host for the runner tests: it records every change and performs none.

``FakeExecutor`` stands in for ``runner.RealExecutor``. Files and directories live in a dict, so a test
reads back what ``piv runner add`` would have written, and nothing touches the machine. Like fakegh, it
is strict: a command that is not one the code is known to use, or a flag the real command does not
take (COMMAND_FLAGS, read from `--help` and the man pages on this machine), raises ``Refused``, so a
test cannot pass on a flag that does not exist.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh-piv"))

from piv import runner  # noqa: E402
from piv.gh import PivError  # noqa: E402

# Flag -> takes a value. useradd 4.x --help, runuser 2.41 --help, tar 1.35 --help, sha256sum 9.x --help,
# curl 8.x --help (--proto, --proto-redir), chmod (coreutils: MODE FILE), systemctl 257 --help. config.sh is GitHub's runner script: its options are those of
# the "Add a self-hosted runner" instructions (--unattended --url --name --work --replace), and its
# token comes from ACTIONS_RUNNER_INPUT_TOKEN, never from --token.
COMMAND_FLAGS = {
    "useradd": {"--create-home": False, "--home-dir": True, "--shell": True, "--user-group": False},
    "runuser": {"-u": True, "--user": True},
    "curl": {"--fail": False, "--silent": False, "--show-error": False, "--location": False, "--output": True,
             "--proto": True, "--proto-redir": True},
    "chmod": {},
    "sha256sum": {"--check": False, "--strict": False, "--status": False},
    "tar": {"--extract": False, "--gzip": False, "--file": True, "--directory": True},
    "systemctl": {"--now": False},
    "config.sh": {"--unattended": False, "--url": True, "--name": True, "--work": True, "--replace": False},
}
SYSTEMCTL_VERBS = {"daemon-reload": 0, "enable": 1, "disable": 1}


class Refused(AssertionError):
    pass


def check_command(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Validate argv against the tables, following runuser into the command it runs. Returns the inner argv."""
    name = Path(argv[0]).name
    if name not in COMMAND_FLAGS:
        raise Refused(f"fake host: {argv[0]} is not a command piv is known to run")
    table = COMMAND_FLAGS[name]
    i, positional = 1, []
    while i < len(argv):
        token = argv[i]
        i += 1
        if name == "runuser" and token == "--":
            return check_command(tuple(argv[i:]))
        if token.startswith("-") and token != "-":
            if token not in table:
                raise Refused(f"fake host: {name} has no flag {token}")
            if table[token]:
                if i >= len(argv):
                    raise Refused(f"fake host: {name} {token} needs a value")
                i += 1
        else:
            positional.append(token)
    if name == "systemctl":
        if not positional or positional[0] not in SYSTEMCTL_VERBS or len(positional) != 1 + SYSTEMCTL_VERBS[positional[0]]:
            raise Refused(f"fake host: systemctl {positional} is not a form piv uses")
    if name == "chmod" and (len(positional) != 2 or not re.fullmatch(r"[0-7]{3,4}", positional[0])):
        raise Refused(f"fake host: chmod {positional} is not a form piv uses")
    if name == "runuser":
        raise Refused("fake host: runuser without -- COMMAND")
    return argv


class FakeExecutor(runner.Executor):
    def __init__(self, euid: int = 0, tools: tuple[str, ...] = runner.TOOLS):
        self._euid = euid
        self.tools = set(tools)
        self.users: set[str] = set()
        self.files: dict[str, str] = {}
        self.dirs: dict[str, tuple[int, str]] = {}
        self.modes: dict[str, int] = {}
        self.calls: list[tuple] = []  # (kind, ...) of every change, in order
        self.run_log: list[dict] = []
        self.fail: dict[str, str] = {}  # command name -> stderr line it fails with
        self.sha_ok = True

    # reads
    def euid(self) -> int:
        return self._euid

    def which(self, tool: str) -> bool:
        return tool in self.tools

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    def user_exists(self, user: str) -> bool:
        return user in self.users

    def instances(self) -> list[int]:
        found = set()
        for path in (*self.dirs, *self.files):
            m = re.match(r"/opt/pv-runner/runner-([0-9]+)(/|$)", path)
            if m:
                found.add(int(m.group(1)))
        return sorted(found)

    def read_text(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path].removeprefix("\ufeff")  # the real one reads utf-8-sig

    # changes
    def run(self, argv: tuple[str, ...], *, cwd: str = "", stdin: str = "", env: dict[str, str] | None = None) -> None:
        inner = check_command(argv)
        name = Path(inner[0]).name
        self.calls.append(("run", argv))
        self.run_log.append({"argv": argv, "cwd": cwd, "stdin": stdin, "env": dict(env or {})})
        if name in self.fail:
            raise PivError(f"{name} failed (exit 1): {self.fail[name]}")
        if name == "useradd":
            self.users.add(inner[-1])
        elif name == "curl":
            self.files[inner[inner.index("--output") + 1]] = "archive"
        elif name == "sha256sum" and not self.sha_ok:
            raise PivError("sha256sum failed (exit 1)")
        elif name == "tar":
            self.files[inner[inner.index("--directory") + 1] + "/config.sh"] = "config"
        elif name == "config.sh" and "remove" not in inner:
            self.files[cwd + "/.runner"] = json.dumps({"gitHubUrl": inner[inner.index("--url") + 1], "agentName": inner[inner.index("--name") + 1]})
            self.dirs[cwd + "/_work"] = (0o700, "pv-runner")  # config.sh makes the work folder itself

    def make_dir(self, path: str, mode: int, owner: str) -> None:
        self.calls.append(("mkdir", path, mode, owner))
        self.dirs[path] = (mode, owner)

    def write_file(self, path: str, content: str, mode: int) -> None:
        self.calls.append(("write", path, mode))
        self.files[path] = content
        self.modes[path] = mode

    def remove_tree(self, path: str) -> None:
        self.calls.append(("rmtree", path))
        for table in (self.files, self.dirs):
            for key in [k for k in table if k == path or k.startswith(path + "/")]:
                del table[key]

    def remove_file(self, path: str) -> None:
        self.calls.append(("rmfile", path))
        self.files.pop(path, None)

    def remove_dir_if_empty(self, path: str) -> None:
        self.calls.append(("rmdir", path))
        if not any(k.startswith(path + "/") for k in (*self.files, *self.dirs)):
            self.dirs.pop(path, None)

    def changed(self) -> bool:
        return bool(self.calls)
