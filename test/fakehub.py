"""A stateful fake GitHub, served as a ``gh`` executable (the sandbox scenarios of test_sandbox.py).

test/fakegh.py answers by argv substring and keeps no state. The walking skeleton needs a repo that changes as the
jobs write to it: a comment posted by ack is read back by report, a pull request opened by publish is found by the
re-run, ``piv remove`` is compared with the repo before ``piv init``. So the whole repo is one JSON file (``state.json``)
and every call reads it, answers and writes it back. The file is the "fake repo state" a test compares.

Like fakegh it is strict. A flag the real command does not take is refused (fakegh.REAL_FLAGS, read from ``gh <cmd>
--help``), an endpoint the fake does not know exits 2 ("no route"), and a call without a token exits 4 as gh does. It is
also strict about permissions: every token the harness hands a job is registered with that job's ``permissions:``, and
a write the token's permissions do not cover is HTTP 403. So a job that was given too little fails here, and a job
that holds a write permission it never uses can be seen in ``calls()``.

What a token's permissions do not gate here: ``git push`` and ``git fetch`` run against a ``file://`` origin and never reach
this program, so publish's ``contents: write`` is pinned by the job's registered permissions (test_sandbox) and not
refused by a push. Everything that goes through ``gh`` is gated, a read as well as a write.

Every call is logged as ``{"args", "stdin", "token"}`` on one line of ``calls.jsonl`` (``token`` is the name the token was
registered under, never its value). The permission lookup of trust can be made to fail with an HTTP error or to hang
(``state["lookup"]``), which is how the scenarios test 403, 500 and a timeout.

What it is not: GitHub. Pagination, rate limits, eventual consistency and the real wording of errors are not modelled.
"""

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR))

import fakegh  # noqa: E402  (REAL_FLAGS: one table of what each gh command takes)

BOT = "github-actions[bot]"
STATUS_LABEL_PERMISSIONS = {"issues": 1}
CALLER_WORKFLOW = "pivotal.yml"  # the caller piv init writes: the file of its runs (engine/seal.py WORKFLOW_PATH)
LEVELS = {"none": 0, "read": 1, "write": 2}
PR_STATES = ("OPEN", "CLOSED", "MERGED")

_STUB = """#!{python}
import os
import sys

sys.path.insert(0, {test_dir!r})
import fakehub

sys.exit(fakehub.cli(sys.argv[1:], fakehub.read_stdin(sys.argv[1:]), os.environ, {state!r}))
"""


class Refuse(Exception):
    """A call the fake answers with an error: ``status`` is the HTTP status (0 for a usage or local failure)."""

    def __init__(self, status: int, message: str, code: int = 1):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def new_state(repo: str, *, default_branch: str = "main", owner_type: str = "User") -> dict:
    owner = repo.split("/")[0]
    return {
        "repo": repo,
        "exists": True,
        "default_branch": default_branch,
        "private": False,
        "owner": {"login": owner, "type": owner_type},
        "actions": {"enabled": True, "allowed_actions": "all"},
        "workflow_permissions": {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
        "secrets": [],  # names; the values are in "vault", which is GitHub's, not the repo's visible state
        "vault": {},
        "variables": {},
        "labels": {},
        "topics": [],
        "files": {},  # path -> {"sha", "content"} (base64), the files the contents API writes
        "issues": {},  # number -> {"number", "title", "body", "labels": [names], "user": {"login"}, "comments": [...]}
        "prs": [],
        "next_pr": 1,
        "tokens": {},  # token -> {"name", "permissions": {"issues": "write", ...}} ("*" for the operator)
        "lookup": {},  # the permission lookup of trust: {"mode": "http", "status": 403} or {"mode": "hang"}
        "collaborators": {},  # login -> permission word
        "dispatches": [],  # the repository_dispatch events created: {"event_type", "client_payload", "token"}
        "release": {"latest": "v1.0.0", "commits": {}, "templates": {}},  # the engine repo, for piv init
        "origin": "",  # the bare repository behind pull requests (their head commits and files)
        "operator": "operator",
        "artifacts_dir": "",  # the artifacts of the runs: one subdirectory per artifact name, its files flat (the sandbox sets it)
        "check_runs": [],  # the check runs POSTed to repos/{repo}/check-runs (app github-actions, in the suite of their run)
    }


# --------------------------------------------------------------------------------------------------------- the fake


class FakeHub:
    tool = "gh"
    env_name = "PIV_GH"

    def __init__(self, root: Path, repo: str, **kwargs: object):
        self.dir = root
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "gh"
        self.log = self.dir / "calls.jsonl"
        self.state_path = self.dir / "state.json"
        self.repo = repo
        self.save(new_state(repo, **kwargs))
        self.path.write_text(_STUB.format(python=sys.executable, test_dir=str(TEST_DIR), state=str(self.state_path)))
        self.path.chmod(0o755)

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save(self, state: dict) -> None:
        write_state(self.state_path, json.dumps(state, sort_keys=True))

    @contextlib.contextmanager
    def edit(self):
        """The state, for a change that reads it first: read, change, write, all under the lock."""
        with locked(self.state_path):
            state = self.state()
            yield state
            self.save(state)

    def update(self, **changes: object) -> None:
        with locked(self.state_path):
            state = self.state()
            state.update(changes)
            self.save(state)

    def register_token(self, token: str, name: str, permissions: dict | str) -> None:
        with locked(self.state_path):
            state = self.state()
            state["tokens"][token] = {"name": name, "permissions": permissions}
            self.save(state)

    def add_issue(self, number: int, title: str, body: str, *, author: str = "alice", labels: tuple[str, ...] = ()) -> None:
        with locked(self.state_path):
            state = self.state()
            state["issues"][str(number)] = {
                "number": number, "title": title, "body": body, "labels": list(labels),
                "user": {"login": author}, "comments": [],
            }
            self.save(state)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def repo_state(self) -> dict:
        """The part of the state a person could see on GitHub, without the vault and the tokens: what piv init
        writes and piv remove must take back."""
        state = self.state()
        return {k: state[k] for k in (
            "secrets", "variables", "labels", "topics", "files", "actions", "workflow_permissions",
        )}

    def issue(self, number: int) -> dict:
        return self.state()["issues"][str(number)]

    def comments(self, number: int) -> list[str]:
        return [c["body"] for c in self.issue(number)["comments"]]

    def pr_comments(self, number: int) -> list[str]:
        """The comments made on a pull request (gh pr comment), in order."""
        return list(self.state().get("pr_comments", {}).get(str(number), []))

    def prs(self) -> list[dict]:
        """The pull requests, with an OPEN one's head read from its branch (the head follows the branch, as on GitHub)."""
        state = self.state()
        for pr in state["prs"]:
            if pr["state"] == "OPEN":
                pr["headRefOid"] = branch_head(state["origin"], pr["headRefName"]) or pr["headRefOid"]
        return state["prs"]


# ------------------------------------------------------------------------------------------------------------ the cli


@contextlib.contextmanager
def locked(state_path: Path):
    """Exclusive access to the state for one read-modify-write: every call is a process, and jobs run side by side."""
    with open(state_path.with_suffix(".lock"), "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def write_state(path: Path, text: str) -> None:
    """Replace the file whole through a temp name no other process uses, so a reader sees the old or the new text."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def cli(argv: list[str], stdin: str, env: dict, state_path: str) -> int:
    """One gh call. The lock covers the read, the answer and the write, so two calls never lose each other's change."""
    path = Path(state_path)
    with locked(path):
        code, out, sleep = _call(argv, stdin, env, path)
    if sleep:  # a hanging lookup holds up its own caller and no one else
        time.sleep(sleep)
    sys.stdout.write(out)
    return code


def _call(argv: list[str], stdin: str, env: dict, path: Path) -> tuple[int, str, float]:
    log = path.with_name("calls.jsonl")
    text = path.read_text(encoding="utf-8")
    state = json.loads(text)
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
    named = state["tokens"].get(token, {}).get("name", "unregistered" if token else "none")
    with open(log, "a", encoding="utf-8") as fh:
        # A secret's value is GitHub's, not a log line: the real gh keeps nothing of it either.
        fh.write(json.dumps({"args": argv, "stdin": "<value>" if argv[:2] == ["secret", "set"] else stdin, "token": named}) + "\n")
    handler = None
    try:
        problem = flags_problem(argv)
        if problem:
            raise Refuse(0, problem, 2)
        if not token:
            raise Refuse(0, "To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN environment variable.", 4)
        if token not in state["tokens"]:
            raise Refuse(401, "Bad credentials", 1)
        handler = Handler(state, state["tokens"][token]["permissions"], env)
        out = handler.run(argv, stdin)
    except Refuse as err:
        suffix = f" (HTTP {err.status})" if err.status else ""
        sys.stderr.write(f"gh: {err.message}{suffix}\n")
        return err.code, "", handler.sleep if handler else 0.0
    finally:
        new = json.dumps(state, sort_keys=True)
        if new != json.dumps(json.loads(text), sort_keys=True):  # a read changes nothing, so it writes nothing
            write_state(path, new)
    return 0, out, handler.sleep


def flags_problem(argv: list[str]) -> str:
    """The first flag in argv that the real command does not take (fakegh.REAL_FLAGS), or an empty string."""
    flags = fakegh.REAL_FLAGS
    key = next((" ".join(argv[:n]) for n in (2, 1) if " ".join(argv[:n]) in flags), None)
    if key is None:
        return "no such gh command: " + " ".join(argv[:2])
    table = flags[key]
    i = len(key.split())
    while i < len(argv):
        token = argv[i]
        i += 1
        if not token.startswith("-") or token == "-":
            continue
        name, equals, value = token.partition("=")
        if name not in table:
            return f"gh {key} has no flag {name}"
        if table[name]:
            if not equals:
                if i >= len(argv):
                    return f"gh {key}: flag {name} needs a value"
                value = argv[i]
                i += 1
            if isinstance(table[name], list) and value not in table[name]:
                return f"gh {key}: flag {name} takes one of {table[name]}, not {value}"
            if isinstance(table[name], frozenset) and not set(value.split(",")) <= table[name]:
                return f"gh {key}: flag {name} takes fields of {sorted(table[name])}, not {value}"
    return ""


def takes_stdin(argv: list[str]) -> bool:
    """Whether the real gh reads its standard input for this call: a secret or variable value with no --body, an issue or pull
    request body or comment from ``--body-file -`` (or a pull request with no body at all), and ``api --input -``. Only those: the stub reads
    stdin for nothing else, so a loop that feeds ids to ``gh run cancel`` (the stop job) keeps every id it has not reached."""
    if argv[:1] == ["api"]:
        return "--input=-" in argv or any(a == "--input" and argv[i + 1:i + 2] == ["-"] for i, a in enumerate(argv))
    body_files, bodies = flag_values(argv, "--body-file", "-F"), flag_values(argv, "--body", "-b")
    if argv[:2] in (["secret", "set"], ["variable", "set"]):
        return not bodies
    if argv[:2] in (["issue", "create"], ["issue", "edit"], ["issue", "comment"], ["pr", "create"], ["pr", "edit"], ["pr", "comment"]):
        return body_files == ["-"] or (argv[:2] == ["pr", "create"] and not body_files and not bodies)
    return False


def read_stdin(argv: list[str]) -> str:
    """The stub's standard input: read only when the call takes it (takes_stdin)."""
    return sys.stdin.read() if takes_stdin(argv) else ""


def branch_head(origin: str, branch: str) -> str:
    """The commit a branch of the fake origin points to, or "" (no origin, or no such branch)."""
    if not origin:
        return ""
    done = subprocess.run(["git", "--git-dir", origin, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                          capture_output=True, text=True, check=False)
    return done.stdout.strip()


def flag_values(argv: list[str], *names: str) -> list[str]:
    found = []
    for i, token in enumerate(argv):
        name, equals, value = token.partition("=")
        if name in names:
            found.append(value if equals else (argv[i + 1] if i + 1 < len(argv) else ""))
    return found


# ------------------------------------------------------------------------------------------------------- the handlers


class Handler:
    def __init__(self, state: dict, permissions: dict | str, env: dict):
        self.s, self.perms, self.env = state, permissions, env
        self.sleep = 0.0  # seconds the caller waits for its answer, after the state is saved

    # -- permissions
    def need(self, scope: str, level: str) -> None:
        if self.perms == "*" or (scope, level) == ("metadata", "read"):  # GitHub grants metadata read to every token
            return
        if LEVELS[self.perms.get(scope, "none")] < LEVELS[level]:
            raise Refuse(403, f"Resource not accessible by integration: needs {scope}: {level}", 1)

    # -- dispatch
    def run(self, argv: list[str], stdin: str) -> str:
        words = argv[:2]
        if argv[0] == "api":
            return self.api(argv[1:], stdin)
        method = getattr(self, "cmd_" + "_".join(words), None)
        if method is None:
            raise Refuse(0, "no route for " + " ".join(argv), 2)
        return method(argv[2:], stdin)

    def repo_arg(self, args: list[str]) -> str:
        repos = flag_values(args, "--repo", "-R")
        return repos[0] if repos else self.env.get("GH_REPO", self.s["repo"])

    def check_repo(self, repo: str) -> None:
        if repo != self.s["repo"] or not self.s["exists"]:
            raise Refuse(404, "Not Found", 1)

    # -- gh subcommands
    def cmd_secret_set(self, args: list[str], stdin: str) -> str:
        repo = self.repo_arg(args)
        self.check_repo(repo)
        self.need("secrets", "write")
        name = args[0]
        if name not in self.s["secrets"]:
            self.s["secrets"].append(name)
        self.s["vault"][name] = stdin
        return ""

    def cmd_secret_delete(self, args: list[str], stdin: str) -> str:
        self.check_repo(self.repo_arg(args))
        self.need("secrets", "write")
        if args[0] not in self.s["secrets"]:
            raise Refuse(404, "Not Found", 1)
        self.s["secrets"].remove(args[0])
        self.s["vault"].pop(args[0], None)
        return ""

    def cmd_variable_set(self, args: list[str], stdin: str) -> str:
        self.check_repo(self.repo_arg(args))
        self.need("variables", "write")
        values = flag_values(args, "--body", "-b")
        self.s["variables"][args[0]] = values[0] if values else stdin
        return ""

    def cmd_variable_delete(self, args: list[str], stdin: str) -> str:
        self.check_repo(self.repo_arg(args))
        self.need("variables", "write")
        if args[0] not in self.s["variables"]:
            raise Refuse(404, "Not Found", 1)
        del self.s["variables"][args[0]]
        return ""

    def cmd_label_create(self, args: list[str], stdin: str) -> str:
        self.check_repo(self.repo_arg(args))
        self.need("issues", "write")
        colour, text = flag_values(args, "--color", "-c"), flag_values(args, "--description", "-d")
        self.s["labels"][args[0]] = {"color": colour[0] if colour else "ededed", "description": text[0] if text else ""}
        return ""

    def cmd_label_delete(self, args: list[str], stdin: str) -> str:
        self.check_repo(self.repo_arg(args))
        self.need("issues", "write")
        if args[0] not in self.s["labels"]:
            raise Refuse(404, "Not Found", 1)
        del self.s["labels"][args[0]]
        for issue in self.s["issues"].values():
            issue["labels"] = [x for x in issue["labels"] if x != args[0]]
        return ""

    def cmd_repo_edit(self, args: list[str], stdin: str) -> str:
        self.check_repo(args[0] if args and not args[0].startswith("-") else self.repo_arg(args))
        self.need("administration", "write")
        for topic in flag_values(args, "--add-topic"):
            if topic not in self.s["topics"]:
                self.s["topics"].append(topic)
        for topic in flag_values(args, "--remove-topic"):
            if topic in self.s["topics"]:
                self.s["topics"].remove(topic)
        return ""

    def cmd_issue_create(self, args: list[str], stdin: str) -> str:
        repo = self.repo_arg(args)
        self.check_repo(repo)
        self.need("issues", "write")
        title = flag_values(args, "--title", "-t")[0]
        body_files = flag_values(args, "--body-file", "-F")
        body = stdin if body_files == ["-"] else (flag_values(args, "--body", "-b") or [""])[0]
        number = max([int(n) for n in self.s["issues"]] + [0]) + 1
        self.s["issues"][str(number)] = {
            "number": number, "title": title, "body": body, "labels": flag_values(args, "--label", "-l"),
            "user": {"login": self.s["operator"]}, "comments": [],
        }
        return f"https://github.com/{repo}/issues/{number}\n"

    def cmd_issue_edit(self, args: list[str], stdin: str) -> str:
        self.check_repo(self.repo_arg(args))
        self.need("issues", "write")
        issue = self.s["issues"].get(args[0]) or self.refuse_missing()
        for label in flag_values(args, "--add-label"):
            if label not in self.s["labels"]:
                raise Refuse(0, f"could not add label: '{label}' not found", 1)
            if label not in issue["labels"]:
                issue["labels"].append(label)
        return ""

    @staticmethod
    def refuse_missing() -> dict:
        raise Refuse(404, "Not Found", 1)

    def cmd_pr_list(self, args: list[str], stdin: str) -> str:
        self.check_repo(self.repo_arg(args))
        self.need("pull-requests", "read")
        heads, states = flag_values(args, "--head", "-H"), flag_values(args, "--state", "-s")
        limit = int((flag_values(args, "--limit", "-L") or ["30"])[0])
        fields = (flag_values(args, "--json") or ["number"])[0].split(",")
        want = states[0] if states else "open"
        found = []
        for pr in self.s["prs"]:
            if heads and pr["headRefName"] != heads[0]:
                continue
            if want == "open" and pr["state"] != "OPEN" or want == "closed" and pr["state"] != "CLOSED":
                continue
            if want == "merged" and pr["state"] != "MERGED":
                continue
            if pr["state"] == "OPEN":  # GitHub's head follows the branch: a pushed fix round moves the open pull request's head too
                pr["headRefOid"] = branch_head(self.s["origin"], pr["headRefName"]) or pr["headRefOid"]
            found.append({k: pr[k] for k in fields})
        jq = (flag_values(args, "--jq", "-q") or [""])[0]
        if jq:  # as gh does: the expression runs over the JSON list and its results are printed raw
            done = subprocess.run(["jq", "-r", jq], input=json.dumps(found[:limit]), capture_output=True, text=True, check=False)
            if done.returncode != 0:
                raise Refuse(0, "jq: " + done.stderr.strip(), 1)
            return done.stdout
        return json.dumps(found[:limit]) + "\n"

    def cmd_pr_ready(self, args: list[str], stdin: str) -> str:
        """gh pr ready <number> [--undo]: the draft flag of an OPEN pull request follows (the hill-climb marks a met fix round ready)."""
        self.check_repo(self.repo_arg(args))
        self.need("pull-requests", "write")
        number = args[0] if args and not args[0].startswith("-") else ""
        pr = next((p for p in self.s["prs"] if str(p["number"]) == number and p["state"] == "OPEN"), None)
        if pr is None:
            raise Refuse(0, f"no open pull request {number}", 1)
        pr["isDraft"] = "--undo" in args
        return ""

    def cmd_pr_comment(self, args: list[str], stdin: str) -> str:
        """gh pr comment <number> --body-file F: the body is kept under the pull request's number."""
        self.check_repo(self.repo_arg(args))
        self.need("pull-requests", "write")
        number = args[0] if args and not args[0].startswith("-") else ""
        if not any(str(p["number"]) == number for p in self.s["prs"]):
            raise Refuse(0, f"no pull request {number}", 1)
        body_files = flag_values(args, "--body-file", "-F")
        body = Path(body_files[0]).read_text(encoding="utf-8") if body_files and body_files[0] != "-" else stdin
        if (flag_values(args, "--body", "-b") or [None])[0] is not None:
            body = flag_values(args, "--body", "-b")[0]
        self.s.setdefault("pr_comments", {}).setdefault(number, []).append(body)
        return ""

    def cmd_pr_create(self, args: list[str], stdin: str) -> str:
        repo = self.repo_arg(args)
        self.check_repo(repo)
        self.need("pull-requests", "write")
        head, base = flag_values(args, "--head", "-H")[0], (flag_values(args, "--base", "-B") or [self.s["default_branch"]])[0]
        title = (flag_values(args, "--title", "-t") or [""])[0]
        body_files = flag_values(args, "--body-file", "-F")
        body = Path(body_files[0]).read_text(encoding="utf-8") if body_files and body_files[0] != "-" else stdin
        if (flag_values(args, "--body", "-b") or [None])[0] is not None:
            body = flag_values(args, "--body", "-b")[0]
        oid = self.rev(head)
        if not oid:
            raise Refuse(0, f"head branch {head} is not on the remote", 1)
        if any(p["headRefName"] == head and p["state"] == "OPEN" for p in self.s["prs"]):
            raise Refuse(0, f"a pull request for branch {head} already exists", 1)
        number = self.s["next_pr"]
        self.s["next_pr"] += 1
        self.s["prs"].append({
            "number": number, "title": title, "body": body, "baseRefName": base, "headRefName": head,
            "headRefOid": oid, "isDraft": "--draft" in args or "-d" in args, "state": "OPEN",
            "isCrossRepository": False, "url": f"https://github.com/{repo}/pull/{number}",
            "author": {"login": BOT}, "updatedAt": "2026-01-01T00:00:00Z",
        })
        return f"https://github.com/{repo}/pull/{number}\n"

    def rev(self, branch: str) -> str:
        return branch_head(self.s["origin"], branch)

    # -- gh api
    def api(self, args: list[str], stdin: str) -> str:
        method, fields, raw_fields, path, jq, paginate = "GET", {}, {}, "", "", False
        header_accept, body_json, slurp = "", None, False
        i = 0
        value_flags = ("-X", "--method", "-F", "--field", "-f", "--raw-field", "--jq", "-q", "--input", "-H", "--header",
                       "--hostname", "--cache", "--preview", "-p", "--template", "-t")
        while i < len(args):
            token, i = args[i], i + 1
            name, equals, value = token.partition("=")
            if name in value_flags:
                if not equals or name in ("-f", "-F"):
                    value = args[i]
                    i += 1
                if name in ("-X", "--method"):
                    method = value
                elif name in ("--jq", "-q"):
                    jq = value
                elif name == "--input":
                    body_json = json.loads(stdin) if value == "-" else json.loads(Path(value).read_text())
                elif name in ("-H", "--header"):
                    if value.lower().startswith("accept:"):
                        header_accept = value.split(":", 1)[1].strip()
                elif name in ("-f", "--raw-field", "-F", "--field"):
                    key, _, val = value.partition("=")
                    (raw_fields if name in ("-f", "--raw-field") else fields)[key] = val
            elif token == "--paginate":
                paginate = True
            elif token == "--slurp":
                slurp = True
            elif token.startswith("-"):
                continue
            else:
                path = token
        if body_json is None and (fields or raw_fields):
            body_json = {}
            for key, val in {**fields, **raw_fields}.items():
                if key.endswith("[]"):
                    body_json.setdefault(key[:-2], []).append(val)
                else:
                    body_json[key] = val
        answer = self.route(method, path, body_json or {}, header_accept)
        if isinstance(answer, str):
            return answer
        if paginate and slurp:  # gh --paginate --slurp: one array with a page per response (one page here)
            answer = [answer]
        text = json.dumps(answer)
        if jq:
            done = subprocess.run(["jq", "-c", "-r", jq], input=text, capture_output=True, text=True, check=False)
            if done.returncode != 0:
                raise Refuse(0, "jq: " + done.stderr.strip(), 1)
            return done.stdout
        return text + "\n"

    def route(self, method: str, path: str, body: dict, accept: str) -> object:
        path, _, query = path.lstrip("/").partition("?")
        query_map = parse_qs(query)
        parts = [unquote(p) for p in path.split("/")]
        repo = "/".join(parts[1:3]) if parts[0] == "repos" else ""
        rest = parts[3:]
        if path == "user":
            return {"login": self.s["operator"]}
        if repo == "0xctx/pivotal" or repo.startswith("you/pivotal"):
            return self.pivotal(parts, query_map)
        if parts[0] != "repos":
            raise Refuse(0, f"no route for {method} /{path}", 2)
        self.check_repo(repo)
        key = (method, "/".join(rest))
        if key == ("GET", ""):
            self.need("metadata", "read")
            return {"full_name": repo, "default_branch": self.s["default_branch"], "private": self.s["private"],
                    "owner": self.s["owner"], "id": 1}
        if rest == ["issues"] and method in ("GET", "POST"):
            return self.issue_list(method, body, query_map)
        if rest[:1] == ["issues"] and len(rest) >= 2:
            return self.issues(method, rest, body, query_map)
        if rest[:2] == ["actions", "permissions"]:
            return self.actions_permissions(method, rest, body)
        if rest[:2] == ["actions", "workflows"]:
            return self.workflow_runs(method, rest, query_map)
        if rest[:2] == ["actions", "runs"] and method == "GET" and (len(rest) == 3 or rest[3:] == ["artifacts"]):
            return self.run_listing(rest, query_map)
        if key == ("POST", "check-runs"):
            return self.make_check_run(body)
        if rest[:1] == ["commits"] and len(rest) == 3 and rest[2] == "check-runs" and method == "GET":
            return self.check_runs_of(rest[1], query_map)
        if rest[:2] in (["actions", "secrets"], ["actions", "variables"]):
            return self.secrets_variables(method, rest, query_map)
        if rest[:1] == ["labels"] and len(rest) == 2 and method == "GET":
            self.need("issues", "read")
            if rest[1] not in self.s["labels"]:
                raise Refuse(404, "Not Found", 1)
            return {"name": rest[1], **self.s["labels"][rest[1]]}
        if rest[:2] == ["git", "ref"] and method == "GET":
            self.need("contents", "read")
            sha = self.rev("/".join(rest[2:]).removeprefix("heads/"))
            if not sha:
                raise Refuse(404, "Not Found", 1)
            return {"object": {"sha": sha}}
        if key == ("POST", "dispatches"):
            return self.dispatch(body)
        if key == ("GET", "topics"):
            return {"names": self.s["topics"]}
        if rest[:1] == ["contents"]:
            return self.contents("/".join(rest[1:]), method, body)
        if rest[:1] == ["collaborators"] and len(rest) == 3 and rest[2] == "permission" and method == "GET":
            return self.lookup(rest[1])
        if rest[:1] == ["pulls"] and len(rest) == 2 and rest[1].isdigit() and method == "GET":
            return self.pull(int(rest[1]))
        raise Refuse(0, f"no route for {method} /{path}", 2)

    def pull(self, number: int) -> dict:
        """GET repos/{repo}/pulls/{n}: a pull request in the REST shape the engine reads (pull-requests read, F1)."""
        self.need("pull-requests", "read")
        pr = next((p for p in self.s["prs"] if p["number"] == number), None)
        if pr is None:
            raise Refuse(404, "Not Found", 1)
        repo = "someone/fork" if pr["isCrossRepository"] else self.s["repo"]
        return {
            "number": pr["number"], "state": "open" if pr["state"] == "OPEN" else "closed", "draft": pr["isDraft"],
            "merged": pr["state"] == "MERGED", "body": pr["body"], "title": pr["title"], "html_url": pr["url"],
            "head": {"ref": pr["headRefName"], "sha": pr["headRefOid"], "repo": {"full_name": repo}},
            "base": {"ref": pr["baseRefName"], "repo": {"full_name": self.s["repo"]}},
        }

    def dispatch(self, body: dict) -> str:
        """POST /repos/{repo}/dispatches as GitHub takes it: Contents write; a body of exactly ``event_type`` (1 to 100 characters)
        and an optional ``client_payload`` object of at most 10 top-level properties and 65535 characters of JSON; 204 with no body."""
        self.need("contents", "write")
        extra = set(body) - {"event_type", "client_payload"}
        event = body.get("event_type")
        payload = body.get("client_payload", {})
        if extra or not isinstance(event, str) or not 1 <= len(event) <= 100 or not isinstance(payload, dict):
            raise Refuse(422, "Validation Failed", 1)
        if len(payload) > 10 or len(json.dumps(payload)) > 65535:
            raise Refuse(422, "No more than 10 properties are allowed; the payload is too large", 1)
        self.s["dispatches"].append({"event_type": event, "client_payload": payload})
        return ""

    def lookup(self, login: str) -> dict:
        """The permission lookup trust makes. ``state["lookup"]`` makes it fail like GitHub does, or hang."""
        mode = self.s["lookup"]
        if mode.get("mode") == "http":
            raise Refuse(mode["status"], mode.get("message", "error"), 1)
        if mode.get("mode") == "hang":
            self.sleep = float(mode.get("seconds", 300))  # served after the lock is released (cli)
        word = self.s["collaborators"].get(login)
        if word is None:
            raise Refuse(404, "Not Found", 1)
        return {"permission": word, "user": {"login": login}}

    def pivotal(self, parts: list[str], query: dict) -> object:
        release = self.s["release"]
        rest = "/".join(parts[3:])
        if rest == "releases/latest":
            return {"tag_name": release["latest"]}
        if parts[3] == "commits":
            sha = release["commits"].get(parts[4])
            if sha is None:
                raise Refuse(404, "Not Found", 1)
            return {"sha": sha}
        if parts[3] == "contents":
            ref = (query.get("ref") or [""])[0]
            text = release["templates"].get(ref)
            if text is None:
                raise Refuse(404, "Not Found", 1)
            return text
        raise Refuse(0, "no route for " + repo + "/" + rest, 2)

    def issue_list(self, method: str, body: dict, query: dict) -> object:
        """GET repos/{repo}/issues: the open issues that carry every label asked for, pull requests left out (ack reads its
        open incidents this way). POST repos/{repo}/issues: a new issue, with unknown labels made plain, as GitHub does (ack
        files an incident this way)."""
        if method == "GET":
            self.need("issues", "read")
            wanted = {name for value in query.get("labels", []) for name in value.split(",") if name}
            found = []
            for number in sorted(self.s["issues"], key=int):
                issue = self.s["issues"][number]
                if issue.get("state", "open") != "open" or not wanted <= set(issue["labels"]):
                    continue
                found.append({k: issue[k] for k in ("number", "title", "body", "user")} | {
                    "labels": [{"name": n} for n in issue["labels"]]})
            return found
        self.need("issues", "write")
        title, text, labels = body.get("title"), body.get("body", ""), body.get("labels", [])
        if not isinstance(title, str) or not title or not isinstance(text, str) or not isinstance(labels, list):
            raise Refuse(422, "Validation Failed", 1)
        number = max([int(n) for n in self.s["issues"]] + [0]) + 1
        for name in labels:
            if name not in self.s["labels"]:
                self.s["labels"][name] = {"color": "ededed", "description": ""}
        self.s["issues"][str(number)] = {
            "number": number, "title": title, "body": text, "labels": list(labels), "user": {"login": BOT}, "comments": [],
        }
        return {"number": number, "title": title, "body": text, "html_url": f"https://github.com/{self.s['repo']}/issues/{number}"}

    def issues(self, method: str, rest: list[str], body: dict, query: dict) -> object:
        number = rest[1]
        issue = self.s["issues"].get(number)
        sub = "/".join(rest[2:])
        if method == "GET":
            self.need("issues", "read")
        else:
            self.need("issues", "write")
        if issue is None:
            raise Refuse(404, "Not Found", 1)
        if sub == "" and method == "GET":
            return {k: issue[k] for k in ("number", "title", "body", "user")} | {
                "labels": [{"name": n} for n in issue["labels"]]}
        if sub == "" and method == "PATCH":
            for key in ("title", "body"):
                if key in body:
                    issue[key] = body[key]
            if body.get("state") in ("open", "closed"):  # the land job closes the issue this way (state=closed)
                issue["state"] = body["state"]
            return {"number": issue["number"]}
        if sub == "comments" and method == "GET":
            return issue["comments"]
        if sub == "comments" and method == "POST":
            comment = {"user": {"login": BOT}, "body": body.get("body", "")}
            issue["comments"].append(comment)
            return comment
        if sub == "labels" and method == "GET":
            return [{"name": n} for n in issue["labels"]]
        if sub == "labels" and method == "POST":
            for name in body.get("labels", []):
                if name not in self.s["labels"]:
                    self.s["labels"][name] = {"color": "ededed", "description": ""}  # GitHub makes an unknown one plain
                if name not in issue["labels"]:
                    issue["labels"].append(name)
            return [{"name": n} for n in issue["labels"]]
        if rest[2:3] == ["labels"] and len(rest) == 4 and method == "DELETE":
            if rest[3] not in issue["labels"]:
                raise Refuse(404, "Label does not exist", 1)
            issue["labels"].remove(rest[3])
            return []
        raise Refuse(0, f"no route for {method} issues/{number}/{sub}", 2)

    def actions_permissions(self, method: str, rest: list[str], body: dict) -> object:
        self.need("administration", "write" if method == "PUT" else "read")
        if rest == ["actions", "permissions"]:
            return dict(self.s["actions"])
        if rest == ["actions", "permissions", "workflow"]:
            if method == "PUT":
                self.s["workflow_permissions"].update(body)
                return ""
            return dict(self.s["workflow_permissions"])
        raise Refuse(0, "no route for actions/" + "/".join(rest), 2)

    def workflow_runs(self, method: str, rest: list[str], query: dict) -> object:
        """GET repos/{repo}/actions/workflows/{file}/runs?status=S: the runs of one status, in one page (pagination is not
        modelled). Every run of the repo is listed here; the caller filters by ``display_title`` with jq, as it does on GitHub."""
        if method != "GET" or len(rest) != 4 or rest[3] != "runs":
            raise Refuse(0, f"no route for {method} actions/{'/'.join(rest)}", 2)
        if rest[2] != CALLER_WORKFLOW:  # the runs of another workflow file are not these runs (S1): not found
            raise Refuse(404, "Not Found", 1)
        self.need("actions", "read")
        wanted = (query.get("status") or [""])[0]
        runs = [dict(r) for r in self.s.get("workflow_runs", []) if wanted in ("", r["status"])]
        return {"total_count": len(runs), "workflow_runs": runs}

    def find_run(self, ident: str) -> dict | None:
        return next((r for r in self.s.get("workflow_runs", []) if str(r["id"]) == ident), None)

    def run_listing(self, rest: list[str], query: dict) -> object:
        """GET repos/{repo}/actions/runs/{id} and .../artifacts: what the land job reads of the run that made a seal. Each
        artifact of the sandbox is listed for that run (one run uploads them in these scenarios; the workflow_run id is the
        run asked for, not the run that uploaded it, which the fake does not record)."""
        self.need("actions", "read")
        run = self.find_run(rest[2])
        if run is None:
            raise Refuse(404, "Not Found", 1)
        if len(rest) == 3:
            return {"id": run["id"], "path": run.get("path", ""), "head_branch": run["head_branch"], "event": run["event"],
                    "check_suite_id": run.get("check_suite_id"), "repository": {"full_name": self.s["repo"]},
                    "status": run["status"], "conclusion": run["conclusion"]}
        root = Path(self.s.get("artifacts_dir") or "")
        items = []
        if root.is_dir():
            for number, directory in enumerate(sorted(p for p in root.iterdir() if p.is_dir()), 1):
                files = [f for f in directory.rglob("*") if f.is_file()]
                items.append({"id": number, "name": directory.name, "size_in_bytes": sum(f.stat().st_size for f in files),
                              "expired": False, "workflow_run": {"id": run["id"]}})
        return {"total_count": len(items), "artifacts": items}

    def cmd_run_download(self, args: list[str], stdin: str) -> str:
        """``gh run download <id> --name <n> --dir <d>``: the files of one artifact of the run, extracted into the directory."""
        self.need("actions", "read")
        self.check_repo(self.repo_arg(args))
        if self.find_run(args[0]) is None:
            raise Refuse(0, f"could not find any workflow run with ID {args[0]}", 1)
        name, directory = (flag_values(args, "--name") or [""])[0], (flag_values(args, "--dir") or [""])[0]
        source = Path(self.s.get("artifacts_dir") or "") / name
        if not name or not source.is_dir():
            raise Refuse(0, "no valid artifacts found to download", 1)
        for item in source.iterdir():
            if not item.is_file() or item.is_symlink():
                raise Refuse(0, "an artifact holds a file that is not a regular file", 1)
            shutil.copyfile(item, Path(directory) / item.name)
        return ""

    def make_check_run(self, body: dict) -> dict:
        """POST repos/{repo}/check-runs, as GitHub answers a job: the app is github-actions, and the check run sits in the check
        suite of the workflow run its external_id names (the run that made the call, in the scenarios of test_sandbox)."""
        self.need("checks", "write")
        if not all(isinstance(body.get(k), str) and body[k] for k in ("name", "head_sha", "status", "external_id")):
            raise Refuse(422, "Validation Failed", 1)
        run = self.find_run(body["external_id"])
        item = {"id": 1 + len(self.s["check_runs"]), "name": body["name"], "head_sha": body["head_sha"], "status": body["status"],
                "conclusion": body.get("conclusion"), "external_id": body["external_id"], "app": {"slug": "github-actions"},
                "check_suite": {"id": run.get("check_suite_id", 0) if run else 0}, "output": body.get("output") or {}}
        self.s["check_runs"].append(item)
        return item

    def check_runs_of(self, sha: str, query: dict) -> dict:
        """GET repos/{repo}/commits/{sha}/check-runs?check_name=N: the check runs of that name on that commit (one page)."""
        self.need("checks", "read")
        name = (query.get("check_name") or [""])[0]
        runs = [dict(c) for c in self.s["check_runs"] if c["head_sha"] == sha and (not name or c["name"] == name)]
        return {"total_count": len(runs), "check_runs": runs}

    def cmd_run_cancel(self, args: list[str], stdin: str) -> str:
        """``gh run cancel <id>``: a run that has not completed is completed with conclusion cancelled, as GitHub does
        once the cancel is accepted. A run already completed is refused."""
        self.need("actions", "write")
        ident, skip = "", False
        for token in args:
            if skip:
                skip = False
            elif token in ("-R", "--repo"):
                skip = True
            elif not token.startswith("-"):
                ident = token
        self.check_repo(self.repo_arg(args))
        runs = self.s.get("workflow_runs", [])
        run = next((r for r in runs if str(r["id"]) == ident), None)
        if run is None:
            raise Refuse(0, f"could not find any workflow run with ID {ident}", 1)
        if run["status"] == "completed":
            raise Refuse(0, "Cannot cancel a workflow run that has completed.", 1)
        run["status"], run["conclusion"] = "completed", "cancelled"
        return ""

    def secrets_variables(self, method: str, rest: list[str], query: dict) -> object:
        self.need("secrets" if rest[1] == "secrets" else "variables", "read" if method == "GET" else "write")
        if method != "GET":
            raise Refuse(0, f"no route for {method} actions/{'/'.join(rest[1:])}", 2)
        kind = rest[1]
        names = self.s["secrets"] if kind == "secrets" else sorted(self.s["variables"])
        if len(rest) == 2:
            items = [{"name": n} for n in names] if kind == "secrets" else [
                {"name": n, "value": self.s["variables"][n]} for n in names]
            per_page = int((query.get("per_page") or ["30"])[0])
            return {"total_count": len(items), kind: items[:per_page]}
        name = rest[2]
        if name not in names:
            raise Refuse(404, "Not Found", 1)
        if kind == "secrets":
            return {"name": name}
        return {"name": name, "value": self.s["variables"][name]}

    def contents(self, file: str, method: str, body: dict) -> object:
        files = self.s["files"]
        self.need("contents", "read" if method == "GET" else "write")
        if method == "GET":
            if file not in files:
                raise Refuse(404, "Not Found", 1)
            return {"sha": files[file]["sha"], "content": files[file]["content"], "path": file}
        have = files.get(file)
        if method == "PUT":
            if have is not None and body.get("sha") != have["sha"]:
                raise Refuse(409 if "sha" in body else 422, "sha does not match", 1)
            content = body.get("content", "")
            files[file] = {"sha": hashlib.sha1(content.encode()).hexdigest(), "content": content}
            return {"content": {"path": file}}
        if method == "DELETE":
            if have is None:
                raise Refuse(404, "Not Found", 1)
            if body.get("sha") != have["sha"]:
                raise Refuse(409, "sha does not match", 1)
            del files[file]
            return {}
        raise Refuse(0, f"no route for {method} contents/{file}", 2)


def encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
