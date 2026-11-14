"""A fake gh for tests: an executable that answers by argv substring and logs every call.

Tests point PIV_GH at ``FakeGh.path``, so the code under test runs the fake as a real subprocess
and never reaches GitHub. Routes are tried in the order they were added; the first whose ``match``
appears in the joined argv answers (with ``exact=True``, the joined argv must equal ``match``). A route
with several answers gives them in order and then keeps the last one, which is how a test makes a
setting read false and then true. Every call is logged as
``{"args": [...], "stdin": "..."}`` on one line of ``calls.jsonl``. A call that matches no route
exits 2, so a test that forgets a route fails instead of passing quietly.

The fake is strict about flags: it refuses any flag that the real command does not take (REAL_FLAGS),
with the same exit status gh uses for a usage error, so a test cannot pass on a flag gh lacks.
"""

import json
import os
import sys
from pathlib import Path

# The fields `gh pr list --json` accepts (gh 2.87.3 `gh pr list --help`). `--json` takes a comma-separated subset.
PR_LIST_JSON_FIELDS = frozenset(
    "additions assignees author autoMergeRequest baseRefName baseRefOid body changedFiles closed closedAt "
    "closingIssuesReferences comments commits createdAt deletions files fullDatabaseId headRefName headRefOid "
    "headRepository headRepositoryOwner id isCrossRepository isDraft labels latestReviews maintainerCanModify "
    "mergeCommit mergeStateStatus mergeable mergedAt mergedBy milestone number potentialMergeCommit projectCards "
    "projectItems reactionGroups reviewDecision reviewRequests reviews state statusCheckRollup title updatedAt url".split()
)

# The flags each command takes, and whether each takes a value. Read from `gh <command> --help` in
# gh 2.87.3. A flag not listed here is refused. A value of True takes any value; a list takes one of its items; a
# frozenset takes a comma-separated subset of its items. Keep this in step with the help when gh is upgraded.
REAL_FLAGS = {
    "pr create": {
        "--assignee": True, "-a": True, "--base": True, "-B": True, "--body": True, "-b": True, "--body-file": True,
        "-F": True, "--draft": False, "-d": False, "--dry-run": False, "--editor": False, "-e": False, "--fill": False,
        "-f": False, "--fill-first": False, "--fill-verbose": False, "--head": True, "-H": True, "--label": True,
        "-l": True, "--milestone": True, "-m": True, "--no-maintainer-edit": False, "--project": True, "-p": True,
        "--recover": True, "--reviewer": True, "-r": True, "--template": True, "-T": True, "--title": True, "-t": True,
        "--web": False, "-w": False, "--repo": True, "-R": True, "--help": False,
    },
    "pr ready": {  # gh pr ready --help (gh 2.87.3)
        "--undo": False, "--repo": True, "-R": True, "--help": False,
    },
    "pr comment": {  # gh pr comment --help (gh 2.87.3): the comment is read from --body or --body-file
        "--body": True, "-b": True, "--body-file": True, "-F": True, "--create-if-none": False, "--delete-last": False,
        "--edit-last": False, "--editor": False, "-e": False, "--web": False, "-w": False, "--yes": False,
        "--repo": True, "-R": True, "--help": False,
    },
    "secret list": {
        "--app": True, "-a": True, "--env": True, "-e": True, "--jq": True, "-q": True, "--json": True,
        "--org": True, "-o": True, "--template": True, "-t": True, "--user": False, "-u": False,
        "--repo": True, "-R": True, "--help": False,
    },
    "secret set": {
        "--app": True, "-a": True, "--body": True, "-b": True, "--env": True, "-e": True,
        "--env-file": True, "-f": True, "--no-repos-selected": False, "--no-store": False,
        "--org": True, "-o": True, "--repos": True, "-r": True, "--user": False, "-u": False,
        "--visibility": True, "-v": True, "--repo": True, "-R": True, "--help": False,
    },
    "variable set": {
        "--body": True, "-b": True, "--env": True, "-e": True, "--env-file": True, "-f": True,
        "--org": True, "-o": True, "--repos": True, "-r": True, "--visibility": True, "-v": True,
        "--repo": True, "-R": True, "--help": False,
    },
    "secret delete": {
        "--app": True, "-a": True, "--env": True, "-e": True, "--org": True, "-o": True,
        "--user": False, "-u": False, "--repo": True, "-R": True, "--help": False,
    },
    "variable delete": {
        "--env": True, "-e": True, "--org": True, "-o": True, "--repo": True, "-R": True, "--help": False,
    },
    "label create": {
        "--color": True, "-c": True, "--description": True, "-d": True, "--force": False, "-f": False,
        "--repo": True, "-R": True, "--help": False,
    },
    "label delete": {"--yes": False, "--repo": True, "-R": True, "--help": False},
    "repo list": {
        "--archived": False, "--fork": False, "-q": True, "--jq": True, "--json": True, "-l": True,
        "--language": True, "-L": True, "--limit": True, "--no-archived": False, "--source": False,
        "-t": True, "--template": True, "--topic": True, "--visibility": True, "--help": False,
    },
    "repo create": {  # gh repo create --help (gh 2.87.3)
        "--add-readme": False, "--clone": False, "-c": False, "--description": True, "-d": True,
        "--disable-issues": False, "--disable-wiki": False, "--gitignore": True, "-g": True, "--homepage": True,
        "-h": True, "--include-all-branches": False, "--internal": False, "--license": True, "-l": True,
        "--private": False, "--public": False, "--push": False, "--remote": True, "-r": True, "--source": True,
        "-s": True, "--team": True, "-t": True, "--template": True, "-p": True, "--help": False,
    },
    "repo delete": {"--yes": False, "--help": False},  # gh repo delete --help (gh 2.87.3)
    "repo edit": {
        "--accept-visibility-change-consequences": False, "--add-topic": True, "--allow-forking": False,
        "--allow-update-branch": False, "--default-branch": True, "--delete-branch-on-merge": False,
        "--description": True, "-d": True, "--enable-advanced-security": False, "--enable-auto-merge": False,
        "--enable-discussions": False, "--enable-issues": False, "--enable-merge-commit": False,
        "--enable-projects": False, "--enable-rebase-merge": False, "--enable-secret-scanning": False,
        "--enable-secret-scanning-push-protection": False, "--enable-squash-merge": False,
        "--enable-wiki": False, "--homepage": True, "-h": True, "--remove-topic": True,
        "--template": False, "--visibility": True, "--help": False,
    },
    "issue create": {
        "--assignee": True, "-a": True, "--body": True, "-b": True, "--body-file": True, "-F": True,
        "--editor": False, "-e": False, "--label": True, "-l": True, "--milestone": True, "-m": True,
        "--project": True, "-p": True, "--recover": True, "--template": True, "-T": True,
        "--title": True, "-t": True, "--web": False, "-w": False, "--repo": True, "-R": True, "--help": False,
    },
    "issue edit": {
        "--add-assignee": True, "--add-label": True, "--add-project": True, "--body": True, "-b": True,
        "--body-file": True, "-F": True, "--milestone": True, "-m": True, "--remove-assignee": True,
        "--remove-label": True, "--remove-milestone": False, "--remove-project": True, "--title": True,
        "-t": True, "--repo": True, "-R": True, "--help": False,
    },
    "issue list": {
        "--app": True, "--assignee": True, "-a": True, "--author": True, "-A": True, "--jq": True, "-q": True,
        "--json": True, "--label": True, "-l": True, "--limit": True, "-L": True, "--mention": True,
        "--milestone": True, "-m": True, "--search": True, "-S": True, "--state": True, "-s": True,
        "--template": True, "-t": True, "--web": False, "-w": False, "--repo": True, "-R": True, "--help": False,
    },
    "pr list": {
        "--app": True, "--assignee": True, "-a": True, "--author": True, "-A": True, "--base": True, "-B": True,
        "--draft": False, "-d": False, "--head": True, "-H": True, "--jq": True, "-q": True,
        "--json": PR_LIST_JSON_FIELDS,
        "--label": True, "-l": True, "--limit": True, "-L": True, "--search": True, "-S": True,
        "--state": ["open", "closed", "merged", "all"], "-s": ["open", "closed", "merged", "all"], "--template": True, "-t": True, "--web": False, "-w": False, "--repo": True, "-R": True,
        "--help": False,
    },
    # `gh run download --help` (gh 2.87.3): -D/--dir, -n/--name, -p/--pattern, and the inherited -R/--repo.
    # `gh run cancel --help` (gh 2.87.3): --force, and the inherited -R/--repo.
    "run cancel": {
        "--force": False, "--repo": True, "-R": True, "--help": False,
    },
    "run download": {
        "--dir": True, "-D": True, "--name": True, "-n": True, "--pattern": True, "-p": True,
        "--repo": True, "-R": True, "--help": False,
    },
    "run list": {
        "--all": False, "-a": False, "--branch": True, "-b": True, "--commit": True, "-c": True, "--created": True,
        "--event": True, "-e": True, "--jq": True, "-q": True, "--json": True, "--limit": True, "-L": True,
        "--status": True, "-s": True, "--template": True, "-t": True, "--user": True, "-u": True,
        "--workflow": True, "-w": True, "--repo": True, "-R": True, "--help": False,
    },
    "search repos": {
        "--archived": False, "--created": True, "--followers": True, "--forks": True,
        "--good-first-issues": True, "--help-wanted-issues": True, "--include-forks": True, "--jq": True,
        "-q": True, "--json": True, "--language": True, "--license": True, "--limit": True, "-L": True,
        "--match": True, "--number-topics": True, "--order": True, "--owner": True, "--size": True,
        "--sort": True, "--stars": True, "--template": True, "-t": True, "--topic": True, "--updated": True,
        "--visibility": True, "--web": False, "-w": False, "--help": False,
    },
    "api": {
        "--cache": True, "--field": True, "-F": True, "--header": True, "-H": True, "--hostname": True,
        "--include": False, "-i": False, "--input": True, "--jq": True, "-q": True,
        "--method": ["GET", "POST", "PATCH", "PUT", "DELETE"], "-X": ["GET", "POST", "PATCH", "PUT", "DELETE"], "--paginate": False, "--preview": True, "-p": True, "--raw-field": True, "-f": True,
        "--silent": False, "--slurp": False, "--template": True, "-t": True, "--verbose": False,
        "--help": False,
    },
}

_SCRIPT = """#!PYTHON
import json
import os
import shutil
import subprocess
import sys

LOG = __LOG__
STATE = __STATE__
FLAGS = __FLAGS__
argv = sys.argv[1:]
stdin = sys.stdin.read()
entry = {"args": argv, "stdin": stdin}
# A -p folder (kaggle's upload folder) is recorded as it is at the call, with each file's mode, so a test
# can see what was uploaded. The folder is removed afterwards, so this is the only record of it.
folder = next(
    (argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in ("-p", "--path") and os.path.isdir(argv[i + 1])),
    None,
)
if folder is not None:
    files = {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        with open(path, encoding="utf-8") as fh:
            files[name] = {"content": fh.read(), "mode": oct(os.stat(path).st_mode & 0o777)}
    entry["folder"] = {"path": folder, "files": files}
# A `--input` file (the body of `gh api`) is recorded as it is at the call, as the folder is: the engine may remove it.
body = next((argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--input" and argv[i + 1] != "-"), None)
if body is not None and os.path.isfile(body):
    with open(body, encoding="utf-8") as fh:
        entry["input"] = fh.read()
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\\n")
# The whole environment of each call, for tests that check what a child was handed. Not in calls.jsonl.
with open(LOG + ".environments", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(dict(os.environ)) + "\\n")


def refuse(message):
    sys.stderr.write("fake __TOOL__: " + message + "\\n")
    sys.exit(2)


def check_flags(argv):
    \"\"\"Refuse a flag the real command does not take. A flag that takes a value skips that value.\"\"\"
    key = next((" ".join(argv[:n]) for n in (2, 1) if " ".join(argv[:n]) in FLAGS), None)
    if key is None:
        refuse("no such gh command: " + " ".join(argv[:2]))
    table = FLAGS[key]
    i = len(key.split())
    while i < len(argv):
        token = argv[i]
        i += 1
        if not token.startswith("-") or token == "-":
            continue
        name, equals, _ = token.partition("=")
        if name not in table:
            refuse(f"gh {key} has no flag {name}")
        if table[name]:
            if equals:
                value = token.partition("=")[2]
            else:
                if i >= len(argv):
                    refuse(f"gh {key}: flag {name} needs a value")
                value = argv[i]
                i += 1
            if isinstance(table[name], list) and value not in table[name]:
                refuse(f"gh {key}: flag {name} takes one of {table[name]}, not {value}")
            if isinstance(table[name], frozenset) and not set(value.split(",")) <= table[name]:
                refuse(f"gh {key}: flag {name} takes fields of {sorted(table[name])}, not {value}")


check_flags(argv)
line = " ".join(argv)
with open(STATE, encoding="utf-8") as fh:
    routes = json.load(fh)
for route in routes:
    if (route["match"] == line) if route.get("exact") else (route["match"] in line):
        answers = route["answers"]
        answer = answers[0]
        if len(answers) > 1:
            del answers[0]
            with open(STATE, "w", encoding="utf-8") as fh:
                json.dump(routes, fh)
        # An answer may carry what the real command does besides printing: `copy` puts the files of a directory into the
        # `--dir` of the call (what `gh run download` does), `effect` runs a command (a server-side change, such as a merge).
        if answer.get("copy"):
            target = next(argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in ("--dir", "-D"))
            os.makedirs(target, exist_ok=True)
            for name in os.listdir(answer["copy"]):
                source = os.path.join(answer["copy"], name)
                if os.path.isdir(source):
                    shutil.copytree(source, os.path.join(target, name), symlinks=True)
                else:
                    shutil.copy2(source, os.path.join(target, name), follow_symlinks=False)
        if answer.get("effect"):
            subprocess.run(answer["effect"], check=True, capture_output=True)
        sys.stdout.write(answer["stdout"])
        sys.stderr.write(answer["stderr"])
        sys.exit(answer["code"])
sys.stderr.write("fake __TOOL__: no route for " + line + "\\n")
sys.exit(2)
"""


def ok(stdout: str = "", *, copy: str | Path | None = None, effect: list[str] | None = None) -> dict:
    """A successful answer. ``copy``: a directory whose files the call puts in its ``--dir``; ``effect``: a command it runs."""
    return {"stdout": stdout, "stderr": "", "code": 0, "copy": None if copy is None else str(copy), "effect": effect}


def fail(stderr: str, code: int = 1) -> dict:
    return {"stdout": "", "stderr": stderr, "code": code}


def not_found() -> dict:
    return fail("gh: Not Found (HTTP 404)\n")


class FakeGh:
    tool = "gh"
    env_name = "PIV_GH"  # the variable that names this fake for piv

    def __init__(self, root: Path, *, flags: dict | None = None):
        self.dir = root
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / self.tool
        self.log = self.dir / "calls.jsonl"
        self.state = self.dir / "routes.json"
        self.routes: list[dict] = []
        self._save()
        script = (
            _SCRIPT.replace("#!PYTHON", "#!" + sys.executable)
            .replace("__TOOL__", self.tool)
            .replace("__LOG__", repr(str(self.log)))
            .replace("__STATE__", repr(str(self.state)))
            .replace("__FLAGS__", repr(REAL_FLAGS if flags is None else flags))
        )
        self.path.write_text(script, encoding="utf-8")
        self.path.chmod(0o755)

    def route(self, match: str, *answers: dict, first: bool = False, exact: bool = False) -> None:
        """Add a route. ``first`` puts it ahead of the others, so a test can override a route a helper added."""
        entry = {"match": match, "answers": list(answers), "exact": exact}
        if first:
            self.routes.insert(0, entry)
        else:
            self.routes.append(entry)
        self._save()

    def _save(self) -> None:
        self.state.write_text(json.dumps(self.routes), encoding="utf-8")

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def environments(self) -> list[dict]:
        """The whole environment each call ran with, in order."""
        path = Path(str(self.log) + ".environments")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def env(self, **extra: str) -> dict[str, str]:
        """An environment that holds only what the test names, plus this fake under its own variable."""
        return {"PATH": os.environ.get("PATH", ""), self.env_name: str(self.path), **extra}
