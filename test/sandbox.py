"""A sandbox that runs the real pipeline the way the workflows do, with fakes only for the network (T2, test_sandbox.py).

What is real: the caller that ``piv init`` wrote (read back from the fake repo), ``.github/workflows/factory.yml`` read
from this checkout, every ``run:`` script of every step (run by bash with ``-eo pipefail``, as the runner does), every
engine entry point (one process per step, ``python3 -I pivotal/engine/...``), git (a bare "server" repository and real
clones, checkouts and pushes over ``file://``), run.sh, the fake agents' real subprocesses and the ``piv`` CLI.

What is evaluated here instead of by GitHub (``Expr``): the ``${{ }}`` expressions of ``if:``, ``env:``, ``with:``,
``outputs:``, ``runs-on:``, ``concurrency:`` and ``strategy:``. A job's environment is built only from its ``env:`` blocks
plus the variables the runner always sets, so an output name that does not match, an env variable the workflow forgot, or
a secret the caller did not pass is a real failure here. The needs/outputs graph, ``matrix`` fan-out, the artifact hand-over
(upload-artifact and download-artifact, with the layout of each: ``name:`` extracts into the path, ``pattern:`` into one
directory per artifact) and the job tokens (each registered with the job's ``permissions:`` at the fake GitHub) follow the
workflows.

What is faked: the network. ``fakehub.py`` is GitHub's API (``gh``), ``fakecurl.py`` is ntfy, ``fakecli.py``-style agents
stand for the model CLIs. The package index is a stub wheel of kaggle 2.2.4 (a pip config in each job's HOME, no network). The harness has these declared deviations from a real run, each tested in test_sandbox.py: ``PIV_GH`` for publish and land (each
runs gh with a fixed PATH, and the workflow never sets the seam), ``CONTAIN_SCOPE=descendants`` for build and review
(``user`` kills every process of the real user started after the engine, and a test must not do that),
``GITHUB_SERVER_URL`` is a ``file://`` directory of bare repositories, and the built-in presets are files the sandbox puts
in each engine checkout (``presets/`` is empty until T8). A cancel (a stop's ``gh run cancel``) lands between two jobs of a run,
never inside one: a job already running finishes, GitHub's signal to a running job is not modelled, and a job that a cancel
skips is ``skipped``, as GitHub reports a job whose ``if`` is false. Also declared, because they are shortcuts and a test can lean on them:
an empty checkout ``ref`` is read as ``main`` (GitHub reads it as the default branch, which is ``main`` here); the engine
checkout at ``ENGINE_SHA`` is a copy of this working tree without ``.git``, ``test``, ``docs``, ``kaggle`` and ``.claude``, not a
fetch of that commit; the fake ``gh`` and ``curl`` come first on ``PATH``; claude and pi are not on it until the engine's setup step installs them from a fake npm and node (no network, and the install is run as ``engine/setup/install.py``), and agy is not on it at all, as on a hosted runner, since it has no pinned install; every ``run:`` is bash with
``-eo pipefail``, which is GitHub's ``shell: bash`` and stricter than the default (``-e``) of a step that sets no shell; and
a multi-slice plan is injected at the plan job's ``matrix`` output (``plan_override``), because the factory sets no plan
agent and so no run makes one. Nothing else differs.

What it cannot show: nothing has run on real GitHub. Runner semantics (containers, images, the step environment the
runner really builds), GitHub's own evaluation of expressions, the real download-artifact layout and the real behaviour of
the checkout and artifact actions are modelled from their documentation, not observed. Timing ("within a minute") is a
property of the runner queue and is not measured; only the order is.

The world: ``world/`` is what outlives a job and what a person could read (server repos, the fake GitHub's visible state,
artifacts, step logs, outputs, the ntfy log); ``vm/`` is each job's own disk, gone when the job ends.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import fakecurl
import fakehub
from test_caller import parse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))
from piv import init  # noqa: E402

REPO = "0xctx/piv-sandbox"
ENGINE_SHA = "3" * 40
OPERATOR_TOKEN = "gho_operator"
SERVER_FACTORY = f"{init.ENGINE_REPO}/.github/workflows/factory.yml@{ENGINE_SHA}"
STEP_SECONDS = 600
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "alice", "GIT_AUTHOR_EMAIL": "alice@example.com", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_NAME": "alice", "GIT_COMMITTER_EMAIL": "alice@example.com",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}

# The harness's whole departure from the workflows, by the engine entry point of the step. Each is tested in test_sandbox.
OVERRIDES = {
    # publish and land run gh with a fixed PATH, where the real gh would be; PIV_GH is the seam of every gh caller.
    "publish.py": lambda sb: {"PIV_GH": str(sb.hub.path)},
    "land.py": lambda sb: {"PIV_GH": str(sb.hub.path)},
}
# CONTAIN_SCOPE=user SIGKILLs every process of the user that started after the engine: right on a fresh hosted VM, a
# disaster on the machine that runs the tests. So EVERY step whose env sets CONTAIN_SCOPE (found in the workflow, not
# listed here) runs contained to its descendants, with the matching "not a hosted VM" value of PIVOTAL_CONTAIN_USER_OK.
CONTAINED = {"CONTAIN_SCOPE": "descendants", "PIVOTAL_CONTAIN_USER_OK": "0"}
STATUS_FUNCTIONS = re.compile(r"\b(success|failure|always|cancelled)\(\)")

_AGENT = '''#!{python}
import base64, io, json, os, re, sys

if sys.argv[1:] == ["--version"]:  # the install step checks the pinned version before any call (engine/setup/install.py)
    print({version!r})
    sys.exit(0)
CFG = json.load(open({cfg!r}))
instruction = sys.argv[-1]
read_sentence = re.search(r"Read the file (.*?) and do the", instruction)
if read_sentence:
    prompt_path = read_sentence.group(1)
    prompt = open(prompt_path).read()
else:  # a pi/omp review: the prompt text is the last argument (reviewjob.agent_argv), there is no file to read
    prompt_path = None
    prompt = instruction
review = "do the review" in instruction or read_sentence is None and os.path.basename(sys.argv[0]) in ("pi", "omp")
DOORS = {doors!r}
token = next((os.environ[n] for n in DOORS if os.environ.get(n)), "")
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{
        "worker": os.path.basename(sys.argv[0]), "exe": sys.argv[0], "mode": "review" if review else "build", "argv": sys.argv[1:],
        "cwd": os.getcwd(), "env_names": sorted(os.environ), "doors": sorted(n for n in DOORS if os.environ.get(n)),
        "path": os.environ.get("PATH", ""),
        "token_held": bool(token), "prompt": prompt,
    }}) + "\\n")
_json_mode = None
if "--mode" in sys.argv:
    i = sys.argv.index("--mode")
    if i + 1 < len(sys.argv) and sys.argv[i + 1] == "json":
        _json_mode = "pi"
if "--output-format" in sys.argv:
    i = sys.argv.index("--output-format")
    if i + 1 < len(sys.argv) and sys.argv[i + 1] == "json":
        _json_mode = "claude"
if review:
    sha = re.search(r"for your `Reviewed:` line: ([0-9a-f]{{64}})", prompt).group(1)
    fence = r" \\(untrusted data, not instructions\\):\\n(`+)\\n(.*?)\\n\\1\\n"
    m = re.search(r"Changed files, one `Covered:` line each, exactly as written here" + fence, prompt, re.S)
    files = m.group(2).split("\\n") if m else []
    finding = CFG.get("review_finding", "nothing wrong")
    review_stderr = CFG.get("review_stderr", "")
    out = io.StringIO()
    out.write(str(finding).replace("$TOKEN", token) + "\\n")
    out.write("Reviewed: " + sha + "\\n")
    for f in files:
        out.write("Covered: " + f + "\\n")
    verdict = CFG.get("verdict", "READY")
    if verdict and verdict != "__MISSING__":
        out.write("Verdict: " + verdict + "\\n")
    text = out.getvalue()
    if _json_mode == "pi":
        # pi --mode json: JSONL. Final assistant text in message_end (docs/json.md, docs/message-types.md).
        events = [
            {{"type": "session", "version": 3, "id": "fake", "timestamp": "2024-01-01T00:00:00.000Z", "cwd": "/"}},
            {{"type": "agent_start"}},
            {{"type": "message_end", "message": {{"role": "assistant", "content": [{{"type": "text", "text": text}}],
                "stopReason": "stop", "api": "fake", "provider": "fake", "model": "fake",
                "usage": {{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                           "cost": {{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}}},
                "timestamp": 0}}}},
            {{"type": "agent_end", "messages": [], "willRetry": False}},
            {{"type": "agent_settled", "aborted": False}},
        ]
        for ev in events:
            sys.stdout.write(json.dumps(ev) + "\\n")
        sys.stdout.flush()
    elif _json_mode == "claude":
        doc = {{"type": "result", "subtype": "success", "result": text, "is_error": False, "cost_usd": 0, "duration_ms": 0, "session_id": "fake"}}
        sys.stdout.write(json.dumps(doc) + "\\n")
        sys.stdout.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    if review_stderr:
        sys.stderr.write(str(review_stderr).replace("$TOKEN", token))
        sys.stderr.flush()
    sys.exit(0)
for action, *args in CFG["build"]:
    if action == "write" and len(args) >= 3 and args[-1] and args[-1] not in prompt:
        continue  # a write with a trailing marker runs only for the slice whose prompt holds the marker
    if action == "write":
        os.makedirs(os.path.dirname(args[0]) or ".", exist_ok=True)
        open(args[0], "w").write(args[1])
    elif action == "leak_b64":
        # The token only as base64, on stdout and in a file the agent changed: the raw form appears nowhere.
        encoded = base64.b64encode(token.encode()).decode()
        print("encoded: " + encoded)
        open(args[0], "w").write(encoded + "\\n")
    elif action in ("leak", "leak_offset"):
        # The token on stdout and in a file the agent changed: in three forms, or ("leak_offset") base64 at offset 1.
        # A third argument limits the action to the slice whose goal holds it.
        if len(args) > 1 and args[1] not in prompt:
            continue
        data = token.encode()
        print("token: " + token)
        forms = [token, base64.b64encode(data).decode(), data.hex()]
        if action == "leak_offset":
            forms = [base64.b64encode(b"x" + data).decode(), base64.urlsafe_b64encode(b"xx" + data).decode()]
        open(args[0], "w").write("\\n".join(forms) + "\\n")
'''

# One build the scenarios share: a feature file and its test, nothing else.
FEATURE = [["write", "feature.txt", "good\n"], ["write", "tests/test_feature.txt", "feature is good\n"]]


class Expr:
    """The subset of GitHub's expression language that the three workflows use, over nested dict contexts.

    ``${{ }}`` inside a string is replaced by the value's text; a string that is one expression gives the value itself
    (a list for ``fromJSON``). A name that is not in a context is null, as GitHub treats it; an unbalanced or unknown
    construct raises, so a workflow edit this evaluator cannot read fails the scenario instead of passing unread.
    """

    TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>==|!=|&&|\|\||[!(),])|(?P<name>[A-Za-z_][\w.-]*)|(?P<num>\d+))")
    BLOCK = re.compile(r"\$\{\{(.*?)\}\}", re.S)

    def __init__(self, contexts: dict, status):
        self.contexts, self.status = contexts, status

    def text(self, value: object) -> object:
        """A workflow value (a string with expressions, or a scalar) evaluated."""
        if not isinstance(value, str):
            return value
        whole = self.BLOCK.fullmatch(value.strip())
        if whole:
            return self.eval(whole.group(1))
        return self.BLOCK.sub(lambda m: self.string(self.eval(m.group(1))), value)

    def string(self, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, indent=2)
        return str(value)

    def eval(self, source: str) -> object:
        self.tokens, self.pos, self.source = [], 0, source
        while self.pos < len(source.rstrip()):
            m = self.TOKEN.match(source, self.pos)
            if m is None:
                raise ValueError(f"cannot read the expression {source!r} at {source[self.pos:]!r}")
            self.tokens.append((m.lastgroup, m.group(m.lastgroup)))
            self.pos = m.end()
        self.at = 0
        value = self.or_()
        if self.at != len(self.tokens):
            raise ValueError(f"unread tokens in {source!r}")
        return value

    def peek(self) -> str | None:
        return self.tokens[self.at][1] if self.at < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        self.at += 1
        return self.tokens[self.at - 1]

    @staticmethod
    def truthy(value: object) -> bool:
        return not (value is None or value == "" or value == 0 or value is False)

    def or_(self) -> object:
        value = self.and_()
        while self.peek() == "||":
            self.take()
            right = self.and_()
            value = value if self.truthy(value) else right
        return value

    def and_(self) -> object:
        value = self.eq()
        while self.peek() == "&&":
            self.take()
            right = self.eq()
            value = right if self.truthy(value) else value
        return value

    def eq(self) -> object:
        value = self.unary()
        while self.peek() in ("==", "!="):
            op = self.take()[1]
            right = self.unary()
            same = self.loose(value) == self.loose(right)
            value = same if op == "==" else not same
        return value

    @staticmethod
    def loose(value: object) -> object:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return value.lower() if isinstance(value, str) else value

    def unary(self) -> object:
        if self.peek() == "!":
            self.take()
            return not self.truthy(self.unary())
        return self.primary()

    def primary(self) -> object:
        kind, text = self.take()
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return int(text)
        if text == "(":
            value = self.or_()
            self.expect(")")
            return value
        if kind == "name" and self.peek() == "(":
            self.take()
            args = []
            while self.peek() != ")":
                args.append(self.or_())
                if self.peek() == ",":
                    self.take()
            self.expect(")")
            return self.call(text, args)
        return self.lookup(text)

    def expect(self, text: str) -> None:
        if self.take()[1] != text:
            raise ValueError(f"expected {text!r} in {self.source!r}")

    def lookup(self, dotted: str) -> object:
        value: object = self.contexts
        for part in dotted.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def call(self, name: str, args: list) -> object:
        if name == "toJSON":
            return json.dumps(args[0], indent=2)
        if name == "fromJSON":
            return json.loads(args[0])
        if name == "contains":
            return args[1] in args[0] if isinstance(args[0], list) else str(args[1]).lower() in str(args[0]).lower()
        if name in ("success", "always", "cancelled", "failure"):
            return self.status(name)
        raise ValueError(f"function {name} is not modelled")


@dataclass
class Step:
    name: str
    id: str
    outcome: str  # success, failure or skipped
    env: dict = field(default_factory=dict)  # what the step process received (empty for a skipped or action step)
    code: int | None = None
    seconds: float = 0.0  # wall time of the script
    log: str = ""
    outputs: dict = field(default_factory=dict)
    script: str = ""


@dataclass
class Job:
    key: str
    name: str
    result: str
    outputs: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    permissions: dict = field(default_factory=dict)
    runs_on: str = ""
    token_name: str = ""

    def step(self, id_or_name: str) -> Step:
        return next(s for s in self.steps if id_or_name in (s.id, s.name))

    def ran(self) -> list:
        return [s for s in self.steps if s.outcome != "skipped"]


@dataclass
class Run:
    event: str
    run_id: int
    run_number: int
    attempt: int
    trust: Job | None = None
    factory: str = ""
    group: str = ""
    inputs: dict = field(default_factory=dict)
    passed_secrets: list = field(default_factory=list)
    jobs: dict = field(default_factory=dict)  # key -> Job, or key -> [Job] for a matrix

    def all_jobs(self) -> list:
        found = [self.trust] if self.trust else []
        for value in self.jobs.values():
            found += value if isinstance(value, list) else [value]
        return found

    def job(self, key: str) -> Job:
        value = self.jobs[key]
        return value[0] if isinstance(value, list) else value

    def result(self, key: str) -> str:
        value = self.jobs.get(key)
        if value is None:
            return "skipped"
        items = value if isinstance(value, list) else [value]
        if not items:
            return "skipped"
        return "success" if all(j.result == "success" for j in items) else "failure"


class Sandbox:
    """One sandbox repository on a fake GitHub, with its jobs' machines, artifacts and server."""

    def __init__(self, root: Path, *, secrets: dict[str, str] | None = None, preset: dict | None = None,
                 untrusted_preset: dict | None = None, check: str = "test -f README.md", actor: str = "alice",
                 variables: dict[str, str] | None = None, factory_edit=None):
        self.root = root
        self.world, self.vm = root / "world", root / "vm"
        self.world.mkdir(parents=True)
        self.vm.mkdir()
        self.secrets = dict(secrets or {})
        self.actor = actor
        self.hub = fakehub.FakeHub(self.world / "hub", REPO)
        self.curl = fakecurl.FakeCurl(self.world / "curl")
        self.agents = self.world / "agents"
        self.agents.mkdir()
        self.agent_log = self.agents / "calls.jsonl"
        self.behaviour = self.agents / "behaviour.json"
        self.artifacts = self.world / "artifacts"
        self.logs = self.world / "logs"
        self.artifacts.mkdir()
        self.logs.mkdir()
        self.server = self.world / "server"
        self.origin = self.server / f"{REPO}.git"
        self.counter = 0
        self.run_counter = 0
        self.preset = preset
        self.untrusted_preset = untrusted_preset
        # presets/ is empty until T8, so the untrusted preset is a file the sandbox puts in each engine checkout.
        self.builtin_presets = {untrusted_preset["name"]: untrusted_preset} if untrusted_preset else {}
        self.check = check
        self.extra_variables = dict(variables or {})
        self.plan_override: list[dict] | None = None  # a plan of several slices: the factory sets no plan agent, so no run makes one
        self.factory_edit = factory_edit  # a function over factory.yml's text: the mutation tests break a seam on purpose
        self.runs: list[Run] = []
        # job key -> a hook run just before the factory or land job of that key is scheduled (a stop that arrives mid-run)
        self.before_job: dict[str, Callable[[], None]] = {}
        self.set_agents(build=FEATURE)
        self.agent_bin = self.install_agents()
        self.npm_dir = self.install_npm_stubs()
        self.make_server()
        self.make_release()
        self.wheels = self.world / "wheels"
        make_stub_kaggle(self.wheels)
        self.hub.register_token(OPERATOR_TOKEN, "operator", "*")
        self.hub.update(origin=str(self.origin), collaborators={actor: "write"}, operator=actor, artifacts_dir=str(self.artifacts))

    # ------------------------------------------------------------------------------------------------ the server repo
    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
                              env={"PATH": SYSTEM_PATH, "HOME": str(self.root), **GIT_IDENTITY})
        if check and done.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr}")
        return done.stdout.strip()

    def make_server(self) -> None:
        self.origin.parent.mkdir(parents=True)
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.root / "seed"
        self.git("init", "-q", "-b", "main", str(seed))
        (seed / "README.md").write_text("# sandbox\n")
        (seed / "app.py").write_text("print('hello')\n")
        self.git("add", "-A", cwd=seed)
        self.git("commit", "-q", "-m", "seed", cwd=seed)
        self.git("push", "-q", str(self.origin), "main", cwd=seed)
        shutil.rmtree(seed)
        self.base_sha = self.git("--git-dir", str(self.origin), "rev-parse", "main")

    def make_release(self) -> None:
        """The engine repo as piv init sees it: a release, its commit and the caller template at that commit."""
        template = (ROOT / "templates" / "caller.yml").read_text(encoding="utf-8")
        self.hub.update(release={"latest": "v1.0.0", "commits": {"v1.0.0": ENGINE_SHA}, "templates": {ENGINE_SHA: template}})

    # ------------------------------------------------------------------------------------------------- the fake agents
    def set_agents(self, *, build: list | None = None, verdict="NEEDS FIXES", review_finding="nothing wrong",
                   review_stderr: str = "") -> None:
        """What the build agent does in the checkout (a list of actions) and what the reviewer prints. ``verdict`` is the
        word the reviewer prints (verdict ``__MISSING__`` prints no Verdict line). A ``write`` action may take a trailing
        marker and runs only when that marker appears in the prompt, which is how one slice's build differs from another's.
        ``leak_b64`` prints and writes the token only as base64. The default verdict makes the slice a draft: a READY review
        of a green slice is a candidate for the stack PR, which T3 publishes."""
        self.behaviour.write_text(json.dumps({
            "build": build if build is not None else FEATURE, "verdict": verdict, "review_finding": review_finding,
            "review_stderr": review_stderr}))

    def install_agents(self) -> Path:
        """The scripted CLIs. claude and pi are installed by the engine's setup step from the fake npm (install_npm_stubs), so
        they are not on the PATH until that step runs. agy has no pinned install (docs/engines.md) and is not on the PATH at all,
        as on a hosted runner: a preset that names it falls through as `not installed`. Returns the directory on the PATH."""
        from engine import route  # the door names come from the engine itself

        doors = sorted({n for names in route.DOOR_SECRETS.values() for n in names})
        self.agent_sources = self.agents / "src"
        self.agent_sources.mkdir(exist_ok=True)
        versions = {"claude": "2.1.312 (Claude Code)", "pi": "1.1.0"}
        for worker, version in versions.items():
            source = _AGENT.format(python=sys.executable, cfg=str(self.behaviour), log=str(self.agent_log), doors=doors,
                                   version=version)
            (self.agent_sources / worker).write_text(source)
        directory = self.agents / "bin"  # empty: every CLI a job runs comes from the pinned install (no agy: no pinned install)
        directory.mkdir(exist_ok=True)
        return directory

    def install_npm_stubs(self) -> Path:
        """A fake npm and node, first on the PATH of every job. The fake npm lays out what the pins name: the claude native
        binary and pi's bin, as real files in the prefix (what the real install makes), from the scripted CLIs. It refuses a
        call without --ignore-scripts, and it logs the environment names it was given (the setup step must hold no secret)."""
        directory = self.world / "npm-bin"
        directory.mkdir(exist_ok=True)
        layouts = {
            "@anthropic-ai/claude-code-linux-x64@2.1.312": ("node_modules/@anthropic-ai/claude-code-linux-x64/claude", str(self.agent_sources / "claude")),
            "@earendil-works/pi-coding-agent@1.1.0": ("node_modules/.bin/pi", str(self.agent_sources / "pi")),
        }
        npm = _FAKE_NPM.format(python=sys.executable, layouts=layouts, log=str(self.world / "npm-env.jsonl"))
        (directory / "npm").write_text(npm, encoding="utf-8")
        (directory / "node").write_text(_FAKE_NODE.format(python=sys.executable), encoding="utf-8")
        for name in ("npm", "node"):
            (directory / name).chmod(0o755)
        return directory

    def agent_calls(self) -> list[dict]:
        if not self.agent_log.exists():
            return []
        return [json.loads(line) for line in self.agent_log.read_text().splitlines()]

    # ------------------------------------------------------------------------------------------------------- piv
    def piv_env(self, **extra: str) -> dict[str, str]:
        env = {"PATH": SYSTEM_PATH, "HOME": str(self.root), "GH_TOKEN": OPERATOR_TOKEN, "PIV_GH": str(self.hub.path)}
        env.update(extra)
        return env

    def piv(self, *args: str, secrets: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
        """``gh piv ARGS`` as a real subprocess of the extension's entry file, with only the secrets named in its environment."""
        return subprocess.run([sys.executable, str(ROOT / "gh-piv" / "gh-piv"), *args], capture_output=True, text=True,
                              check=False, timeout=timeout, env=self.piv_env(**(secrets or {})))

    def piv_init(self, *, preset: str | None = "sandbox", extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        """piv init of the sandbox repo: its variables, the secrets this sandbox holds, the labels and the caller."""
        args = ["init", REPO, "--check", self.check, "--release", "v1.0.0", *extra]
        if preset:
            args += ["--preset", preset]
        done = self.piv(*args, secrets=self.secrets)
        if done.returncode == 0:
            self.set_variables()
        return done

    def set_variables(self) -> None:
        with self.hub.edit() as state:
            if self.preset is not None:
                state["variables"]["PIVOTAL_PRESET_JSON"] = json.dumps(self.preset)
            if self.untrusted_preset is not None:
                state["variables"]["PIVOTAL_UNTRUSTED_PRESET"] = self.untrusted_preset["name"]
            state["variables"].update(self.extra_variables)

    def caller_text(self) -> str:
        return base64.b64decode(self.hub.state()["files"][init.WORKFLOW_PATH]["content"]).decode("utf-8")

    # ----------------------------------------------------------------------------------------------------- events
    def issue_event(self, number: int, *, label: str = "pivotal") -> tuple[str, dict]:
        issue = self.hub.issue(number)
        return "issues", {
            "action": "labeled", "label": {"name": label},
            "issue": {"number": number, "title": issue["title"], "body": issue["body"], "user": issue["user"],
                      "labels": [{"name": n} for n in issue["labels"]]},
            "repository": {"full_name": REPO, "default_branch": "main", "private": False},
            "sender": issue["user"],
        }

    def comment_event(self, number: int, body: str, commenter: str) -> tuple[str, dict]:
        name, payload = self.issue_event(number)
        payload.update(action="created", comment={"body": body, "user": {"login": commenter}}, sender={"login": commenter})
        payload.pop("label")
        return "issue_comment", payload

    def label_issue(self, number: int, title: str, body: str, *, author: str | None = None) -> tuple[str, dict]:
        """File an issue and put the trigger label on it, as a person does; the event GitHub would send."""
        self.hub.add_issue(number, title, body, author=author or self.actor, labels=("pivotal",))
        return self.issue_event(number)

    # ------------------------------------------------------------------------------------------------- the workflow
    def factory_text(self) -> str:
        text = (ROOT / ".github" / "workflows" / "factory.yml").read_text(encoding="utf-8")
        return self.factory_edit(text) if self.factory_edit else text

    def run_workflow(self, event: tuple[str, dict], *, run_id: int | None = None, attempt: int = 1,
                     stop_after_trust: bool = False, trust_skipped: bool = False) -> Run:
        """One run of the caller on the event: trust, then the factory, land or stop job the caller's conditions choose.

        The run's record in the fake GitHub is ``in_progress`` until the run ends, so a stop that arrives while it runs
        finds it (``before_job``), and it is ``completed`` after: with ``cancelled`` if a stop cancelled it, else its outcome.
        A trigger the caller's trust ``if:`` turns away is a skipped trust job and nothing else, but only when the test
        says so (``trust_skipped``); any other skipped trust job is a failure of the test.
        """
        name, payload = event
        if run_id is None:
            self.run_counter += 1
            run_id = 1000 + self.run_counter
        run = Run(name, run_id, run_id - 999, attempt)
        event_path = self.world / f"event-{run_id}-{attempt}.json"
        event_path.write_text(json.dumps(payload))
        caller = parse(self.caller_text())
        self.runs.append(run)
        ctx = self.context(run, payload, event_path)
        ctx["secrets"] = dict(self.secrets)
        trust_def = caller["jobs"]["trust"]
        reached = Expr.truthy(Expr(ctx, lambda _n: True).text("${{ " + str(trust_def["if"]).strip() + " }}"))
        if not reached:
            if not trust_skipped:
                raise AssertionError("trust job skipped: the sandbox runs only triggers that reach it")
            # GitHub skips the trust job and so every job after it: the run exists, and nothing else happens.
            run.trust = Job(key="trust", name="trust", result="skipped")
            self._register_workflow_run(run, name, payload)
            self._finish_workflow_run(run)
            return run
        if trust_skipped:
            raise AssertionError("the test expected the trust job to be skipped, and the caller's condition reaches it")
        run.trust = self.run_job(run, "trust", trust_def, ctx, {}, label="trust")
        self._register_workflow_run(run, name, payload)
        outputs = run.trust.outputs
        needs = {"trust": {"result": run.trust.result, "outputs": outputs}}
        for key in ("factory-trusted", "factory-untrusted", "land", "stop"):
            definition = caller["jobs"][key]
            cctx = {**ctx, "needs": needs}
            if not Expr.truthy(Expr(cctx, lambda _n: run.trust.result == "success").text("${{ " + str(definition["if"]).strip() + " }}")):
                continue
            run.factory = key if key.startswith("factory") else ""
            if "concurrency" in definition:
                run.group = Expr(cctx, lambda _n: True).text(definition["concurrency"]["group"])
            expr = Expr(cctx, lambda _n: True)
            run.inputs = {k: expr.text(v) for k, v in definition.get("with", {}).items()}
            passed = {k: expr.text(v) for k, v in definition.get("secrets", {}).items()}
            run.passed_secrets = sorted(k for k, v in passed.items() if v)
            if key.startswith("factory"):
                assert definition["uses"] == SERVER_FACTORY, definition["uses"]
                if not stop_after_trust:
                    self.run_factory(run, payload, event_path, run.inputs, passed)
            elif key == "land":
                assert definition["uses"].startswith(f"{init.ENGINE_REPO}/.github/workflows/land.yml@"), definition["uses"]
                if not stop_after_trust:
                    self.run_land(run, payload, event_path, run.inputs)
            else:
                if not stop_after_trust:
                    self.run_stop(run, payload, event_path)
            break
        self._finish_workflow_run(run)
        return run

    def _register_workflow_run(self, run: Run, event_name: str, payload: dict) -> None:
        """Record the run where the stop job's listing finds it. Its title is the caller's ``run-name``, evaluated on the EVENT
        as GitHub does, by evaluating it (``pivotal-issue-<issue.number or client_payload.issue>``, ``none`` when neither), so a
        comment on a pull request is titled with the pull request's number, not the issue trust resolves."""
        title = Expr({"github": {"event": payload}}, lambda _n: True).text(parse(self.caller_text())["run-name"])
        with self.hub.edit() as state:
            state.setdefault("workflow_runs", []).append({
                "id": run.run_id,
                "display_title": title,
                "status": "in_progress",
                "conclusion": "",
                "head_branch": "main",
                "event": event_name,
                "path": f".github/workflows/{fakehub.CALLER_WORKFLOW}",  # the caller's file: what the land job's seal check reads
                "check_suite_id": 90000 + run.run_id,  # the suite of this run: a check run the run makes sits in it
            })

    def _finish_workflow_run(self, run: Run) -> None:
        """The run is over: ``completed``, with ``cancelled`` if a stop cancelled it, else ``failure`` or ``success``."""
        failed = any(job.result == "failure" for job in run.all_jobs())
        with self.hub.edit() as state:
            for record in state.get("workflow_runs", []):
                if record["id"] == run.run_id and record["status"] != "completed":
                    record["status"] = "completed"
                    record["conclusion"] = record["conclusion"] or ("failure" if failed else "success")

    def cancelled(self, run: Run) -> bool:
        """True once a ``gh run cancel`` of this run has been accepted (a stop job, in this run or another)."""
        return any(r["id"] == run.run_id and r["conclusion"] == "cancelled" for r in self.hub.state().get("workflow_runs", []))

    def context(self, run: Run, payload: dict, event_path: Path) -> dict:
        state = self.hub.state()
        return {
            "github": {
                "repository": REPO, "run_id": str(run.run_id), "run_number": str(run.run_number),
                "run_attempt": str(run.attempt), "event_name": run.event, "event": payload,
                "event_path": str(event_path), "server_url": f"file://{self.server}", "token": "",
            },
            "vars": dict(state["variables"]), "runner": {}, "inputs": {}, "needs": {}, "secrets": {},
        }

    def _before(self, key: str) -> None:
        """Run the hook a test set for this job, now: a stop that arrives while the run is in progress is one."""
        hook = self.before_job.pop(key, None)
        if hook is not None:
            hook()

    def run_factory(self, run: Run, payload: dict, event_path: Path, inputs: dict, secrets: dict) -> None:
        workflow = parse(self.factory_text())
        defined = {k: v.get("default", "") for k, v in workflow["on"]["workflow_call"]["inputs"].items()}
        inputs = {**defined, **inputs}
        base = self.context(run, payload, event_path)
        base.update(inputs=inputs, secrets=secrets)
        results: dict[str, dict] = {}
        pending = list(workflow["jobs"])
        while pending:
            key = next(k for k in pending if all(n in results for n in self.needs_of(workflow["jobs"][k])))
            self._before(key)
            pending.remove(key)
            definition = workflow["jobs"][key]
            needs = {n: results[n] for n in self.needs_of(definition)}
            ctx = {**base, "needs": needs}
            ok = all(r["result"] == "success" for r in needs.values())
            skipped_needs = any(r["result"] == "skipped" for r in needs.values())

            cancelled = self.cancelled(run)

            def status(name: str, ok=ok, cancelled=cancelled) -> bool:
                return {"success": ok and not cancelled, "failure": not ok, "always": True, "cancelled": cancelled}[name]

            condition = definition.get("if")
            wanted = ok and not skipped_needs and not cancelled
            if condition is not None:
                text = str(condition)
                value = Expr(ctx, status).text("${{ " + text + " }}")
                wanted = bool(Expr.truthy(value)) and (ok or bool(STATUS_FUNCTIONS.search(text)))
            if not wanted:
                results[key] = {"result": "skipped", "outputs": {}}
                run.jobs[key] = []
                continue
            matrix = definition.get("strategy", {}).get("matrix")
            if matrix is not None:
                (axis, source), = matrix.items()  # one axis: build/review "slice" (objects), gate "id" (strings)
                instances = Expr(ctx, status).text(source)
                jobs = [self.run_job(run, key, definition, {**ctx, "matrix": {axis: item}}, needs,
                                     label=f"{key}-{item['id'] if isinstance(item, dict) else item}") for item in instances]
                run.jobs[key] = jobs
                results[key] = {"result": "success" if all(j.result == "success" for j in jobs) else "failure",
                                "outputs": {}}
            else:
                one = self.run_job(run, key, definition, ctx, needs, label=key)
                run.jobs[key] = [one]
                if key == "plan" and self.plan_override is not None and one.outputs.get("matrix"):
                    one.outputs["matrix"] = json.dumps(self.plan_override, separators=(",", ":"))
                results[key] = {"result": one.result, "outputs": one.outputs}

    def run_land(self, run: Run, payload: dict, event_path: Path, inputs: dict) -> None:
        """Run the land.yml reusable workflow: its steps, job by job, as the caller's land job calls it."""
        land_workflow_path = ROOT / ".github" / "workflows" / "land.yml"
        assert land_workflow_path.exists(), "land.yml is missing: the caller's land job would call nothing (fail open)"
        workflow = parse(land_workflow_path.read_text(encoding="utf-8"))
        base = self.context(run, payload, event_path)
        base.update(inputs=inputs, secrets={})
        results: dict[str, dict] = {}
        pending = list(workflow["jobs"])
        while pending:
            key = next(k for k in pending if all(n in results for n in self.needs_of(workflow["jobs"][k])))
            self._before(key)
            pending.remove(key)
            definition = workflow["jobs"][key]
            needs = {n: results[n] for n in self.needs_of(definition)}
            ctx = {**base, "needs": needs}
            ok = all(r["result"] == "success" for r in needs.values())
            skipped_needs = any(r["result"] == "skipped" for r in needs.values())

            cancelled = self.cancelled(run)

            def status(name: str, ok=ok, cancelled=cancelled) -> bool:
                return {"success": ok and not cancelled, "failure": not ok, "always": True, "cancelled": cancelled}[name]

            condition = definition.get("if")
            wanted = ok and not skipped_needs and not cancelled
            if condition is not None:
                text = str(condition)
                value = Expr(ctx, status).text("${{ " + text + " }}")
                wanted = bool(Expr.truthy(value)) and (ok or bool(STATUS_FUNCTIONS.search(text)))
            if not wanted:
                results[key] = {"result": "skipped", "outputs": {}}
                run.jobs[key] = []
                continue
            one = self.run_job(run, key, definition, ctx, needs, label=key)
            run.jobs[key] = [one]
            results[key] = {"result": one.result, "outputs": one.outputs}

    def run_stop(self, run: Run, payload: dict, event_path: Path) -> None:
        """Run the stop job from the caller."""
        caller = parse(self.caller_text())
        definition = caller["jobs"]["stop"]
        base = self.context(run, payload, event_path)
        # github.workflow_ref format: owner/repo/.github/workflows/pivotal.yml@refs/heads/main
        base["github"]["workflow_ref"] = f"{REPO}/.github/workflows/pivotal.yml@refs/heads/main"
        base.update(inputs={}, secrets={})
        needs = {"trust": {"result": run.trust.result, "outputs": run.trust.outputs}}
        ctx = {**base, "needs": needs}
        ok = run.trust.result == "success"

        def status(name: str) -> bool:
            return {"success": ok, "failure": not ok, "always": True, "cancelled": False}[name]

        condition = definition.get("if")
        wanted = ok
        if condition is not None:
            text = str(condition)
            value = Expr(ctx, status).text("${{ " + text + " }}")
            wanted = bool(Expr.truthy(value)) and ok
        if not wanted:
            run.jobs["stop"] = []
            return
        one = self.run_job(run, "stop", definition, ctx, needs, label="stop")
        run.jobs["stop"] = [one]

    @staticmethod
    def needs_of(definition: dict) -> list[str]:
        needs = definition.get("needs", [])
        return [needs] if isinstance(needs, str) else list(needs)

    # ----------------------------------------------------------------------------------------------------- one job
    def run_job(self, run: Run, key: str, definition: dict, ctx: dict, needs: dict, *, label: str) -> Job:
        self.counter += 1
        directory = self.vm / f"{self.counter:03d}-{label}-a{run.attempt}"
        workspace, temp, home = directory / "workspace", directory / "temp", directory / "home"
        for d in (workspace, temp, home):
            d.mkdir(parents=True)
        # models-up installs kaggle==2.2.4 from the index: pip reads this config from the job's HOME, so the index is the
        # stub wheel and nothing is downloaded. It is a file, not an environment variable: every step's env is unchanged.
        (home / ".config" / "pip").mkdir(parents=True)
        (home / ".config" / "pip" / "pip.conf").write_text(f"[global]\nno-index = true\nfind-links = {self.wheels}\n", encoding="utf-8")
        permissions = dict(definition.get("permissions") or {})
        token = f"ghs_{self.counter:03d}_{label}"
        self.hub.register_token(token, label, permissions)
        ctx = {**ctx, "github": {**ctx["github"], "token": token, "workspace": str(workspace)},
               "runner": {"temp": str(temp)}, "steps": {}}
        job = Job(key, label, "success", permissions=permissions, token_name=label)
        runs_on = Expr(ctx, lambda _n: True).text(definition.get("runs-on", ""))
        job.runs_on = str(runs_on)
        failed = False

        def status(name: str) -> bool:
            return {"success": not failed, "failure": failed, "always": True, "cancelled": False}[name]

        expr = Expr(ctx, status)
        for number, definition_step in enumerate(definition["steps"]):
            sid = definition_step.get("id", "")
            name = str(expr.text(definition_step.get("name", sid or f"step {number}")))
            condition = definition_step.get("if")
            wanted = not failed
            if condition is not None:
                text = str(condition)
                wanted = Expr.truthy(expr.text("${{ " + text + " }}")) and (not failed or bool(STATUS_FUNCTIONS.search(text)))
            if not wanted:
                step = Step(name, sid, "skipped")
                job.steps.append(step)
                if sid:
                    ctx["steps"][sid] = {"outcome": "skipped", "outputs": {}}
                continue
            step = self.run_step(job, definition_step, expr, ctx, directory, workspace, temp, home, number, run)
            job.steps.append(step)
            if sid:
                ctx["steps"][sid] = {"outcome": step.outcome, "outputs": step.outputs}
            if step.outcome == "failure":
                failed = True
        job.result = "failure" if failed else "success"
        job.outputs = {k: str(Expr(ctx, status).text(v)) for k, v in (definition.get("outputs") or {}).items()}
        summary = temp / "summary.md"
        if summary.exists():
            (self.world / "summaries").mkdir(exist_ok=True)
            shutil.copy(summary, self.world / "summaries" / f"{directory.name}.md")
        rmtree(directory)  # the machine is gone; only the world remains
        return job

    def base_env(self, run: Run, event_path: Path, workspace: Path, temp: Path, home: Path, out: Path) -> dict[str, str]:
        # GITHUB_PATH: a directory a step appends is first on the PATH of every later step, as on the runner.
        github_path = temp / "github-path"
        github_path.touch()
        added = [line for line in github_path.read_text(encoding="utf-8").splitlines() if line]
        path = os.pathsep.join([*added, str(self.hub.dir), str(self.curl.dir), str(self.agent_bin), str(self.npm_dir), SYSTEM_PATH,
                                os.path.dirname(sys.executable)])
        return {
            "PATH": path, "HOME": str(home), "LANG": "C.UTF-8", "CI": "true", "GITHUB_ACTIONS": "true",
            "GITHUB_PATH": str(github_path),
            "GITHUB_WORKSPACE": str(workspace), "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(temp / "summary.md"),
            "GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": run.event, "GITHUB_REPOSITORY": REPO,
            "GITHUB_RUN_ID": str(run.run_id), "GITHUB_RUN_NUMBER": str(run.run_number),
            "GITHUB_RUN_ATTEMPT": str(run.attempt), "GITHUB_SERVER_URL": f"file://{self.server}",
            "RUNNER_TEMP": str(temp), "RUNNER_OS": "Linux",
        }

    def run_step(self, job: Job, definition: dict, expr: Expr, ctx: dict, directory: Path, workspace: Path, temp: Path,
                 home: Path, number: int, run: Run) -> Step:
        sid = definition.get("id", "")
        name = str(expr.text(definition.get("name", sid or f"step {number}")))
        out = temp / f"out-{number}"
        out.write_text("")
        step = Step(name, sid, "success")
        if "uses" in definition:
            with_ = {k: expr.text(v) for k, v in (definition.get("with") or {}).items()}
            uses_env = self.base_env(run, Path(ctx["github"]["event_path"]), workspace, temp, home, out)
            uses_env.update({k: expr.string(expr.text(v)) for k, v in (definition.get("env") or {}).items()})
            try:
                step.outputs = self.action(str(definition["uses"]), with_, workspace, temp, run, uses_env)
                step.log = f"uses {definition['uses'].split('@')[0]}"
            except Exception as err:  # an action that fails fails its step, as on the runner
                step.outcome, step.log = "failure", f"action failed: {err}"
            return step
        env = self.base_env(run, Path(ctx["github"]["event_path"]), workspace, temp, home, out)
        step_env = {k: expr.string(expr.text(v)) for k, v in (definition.get("env") or {}).items()}
        env.update(step_env)
        if "CONTAIN_SCOPE" in step_env:
            env.update(CONTAINED)
        script = str(definition["run"])
        for needle, extra in OVERRIDES.items():
            if needle in script:
                env.update(extra(self))
        step.env, step.script = dict(env), script
        script_path = temp / f"step-{number}.sh"
        script_path.write_text(script)
        started = time.monotonic()
        try:
            done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script_path)], cwd=workspace, env=env,
                                  capture_output=True, text=True, check=False, timeout=STEP_SECONDS, stdin=subprocess.DEVNULL)
            step.code, stdout, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired:
            step.code, stdout, stderr = 124, "", "step timed out"
        step.seconds = time.monotonic() - started
        step.log = f"$ {script.strip().splitlines()[-1] if script.strip() else ''}\n--- stdout\n{stdout}--- stderr\n{stderr}"
        (self.logs / f"{directory.name}-{number}-{sid or 'step'}.log").write_text(step.log)
        step.outputs = parse_outputs(out.read_text(encoding="utf-8", errors="replace"))
        step.outcome = "success" if step.code == 0 else "failure"
        return step

    # --------------------------------------------------------------------------------------------------- the actions
    def action(self, uses: str, with_: dict, workspace: Path, temp: Path, run: Run, env: dict) -> dict:
        kind = uses.split("@")[0]
        if kind == "./pivotal/engine/setup":
            return self.setup(workspace, temp, env)
        if kind == "actions/checkout":
            return self.checkout(with_, workspace)
        if kind == "actions/upload-artifact":
            return self.upload(with_, workspace)
        if kind == "actions/download-artifact":
            return self.download(with_, workspace)
        raise AssertionError(f"action {kind} is not modelled")

    def setup(self, workspace: Path, temp: Path, env: dict) -> dict:
        """engine/setup/action.yml: one shell step, ``python3 -I "$ACTION_DIR/install.py"``, with the step's names and the job's
        PATH and RUNNER_TEMP. Its exit is the step's; a failed install is a failed step, as on the runner."""
        action_dir = workspace / "pivotal" / "engine" / "setup"
        step_env = {**env, "ACTION_DIR": str(action_dir)}
        step_env.pop("GITHUB_OUTPUT", None)
        done = subprocess.run(["python3", "-I", str(action_dir / "install.py")], cwd=workspace, env=step_env, capture_output=True,
                              text=True, check=False, timeout=STEP_SECONDS, stdin=subprocess.DEVNULL)
        (self.logs / f"setup-{temp.name}.log").write_text(done.stdout + done.stderr, encoding="utf-8")
        if done.returncode != 0:
            raise RuntimeError(f"setup exited {done.returncode}: {done.stderr.strip()[-300:]}")
        return {}

    def checkout(self, with_: dict, workspace: Path) -> dict:
        target = workspace / str(with_.get("path", "."))
        repository = str(with_.get("repository", REPO))
        ref = str(with_.get("ref", "")) or "main"
        if repository == init.ENGINE_REPO:
            if ref != ENGINE_SHA:
                raise RuntimeError(f"{init.ENGINE_REPO} has no commit {ref}")
            shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", ".venv", "test", "docs", "*.pyc", ".claude"))
            # The built-in presets (T8) are files of the engine checkout; the repo's own preset is PIVOTAL_PRESET_JSON.
            for name, preset in self.builtin_presets.items():
                (target / "presets").mkdir(exist_ok=True)
                (target / "presets" / f"{name}.json").write_text(json.dumps(preset))
            return {"commit": ENGINE_SHA}
        if repository != REPO:
            raise RuntimeError(f"the sandbox has no repository {repository}")
        self.git("clone", "-q", f"file://{self.origin}", str(target))
        self.git("checkout", "-q", "--detach", ref, cwd=target)
        return {"commit": self.git("rev-parse", "HEAD", cwd=target)}

    def upload(self, with_: dict, workspace: Path) -> dict:
        source = Path(str(with_["path"]))
        files = [p for p in sorted(source.rglob("*")) if p.is_file() and not p.is_symlink()] if source.is_dir() else []
        if not files:
            if with_.get("if-no-files-found") == "error":
                raise RuntimeError("no files were found with the provided path")
            return {}
        target = self.artifacts / str(with_["name"])
        if target.exists():
            if not with_.get("overwrite"):
                raise RuntimeError("an artifact of this name exists")
            shutil.rmtree(target)
        for path in files:
            if any(part.startswith(".") for part in path.relative_to(source).parts):
                continue  # include-hidden-files defaults to false
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, destination)
        return {}

    def download(self, with_: dict, workspace: Path) -> dict:
        path = Path(str(with_["path"]))
        path.mkdir(parents=True, exist_ok=True)
        if "name" in with_:
            source = self.artifacts / str(with_["name"])
            if not source.is_dir():
                raise RuntimeError(f"Unable to find an artifact with the name: {with_['name']}")
            shutil.copytree(source, path, dirs_exist_ok=True)
            return {}
        pattern = re.compile(re.escape(str(with_["pattern"])).replace(r"\*", ".*"))
        for source in sorted(self.artifacts.iterdir()):
            if pattern.fullmatch(source.name):
                shutil.copytree(source, path / source.name, dirs_exist_ok=True)
        return {}

    # ------------------------------------------------------------------------------------------- what a person can see
    def leaks(self, *secrets: str) -> list[str]:
        """Every place in the world that holds any form of any of the secrets: the places, not the values."""
        from engine import build  # the engine's own list of the forms a value takes: every alignment, URL-safe, both hex cases

        forms = []
        for secret in secrets:
            forms += [needle.decode("ascii", errors="replace") for needle in build.forms(secret.encode())]
        found = []
        for label, text in self.visible_text():
            # The engine's scan also searches the text with its whitespace squashed (a value split by a newline, base64
            # wrapped at a column); a leak the engine reports must not be a place this search misses.
            data = text.encode("utf-8", errors="replace")
            views = [text, build.squash(data).decode("utf-8", errors="replace"),
                     build.squash(data, diff=True).decode("utf-8", errors="replace")]
            if any(form in view for form in forms for view in views):
                found.append(label)
        return found

    def visible_text(self) -> list[tuple[str, str]]:
        """Text of everything outliving a job, apart from GitHub's secret vault: files of the world (the fake GitHub's
        visible state in its place; step logs, step summaries and artifacts are files of it), every job's outputs, the contents of every object in the server repository and in each uploaded bundle."""
        pieces: list[tuple[str, str]] = []
        state = self.hub.state()
        state.pop("vault", None)
        state.pop("tokens", None)
        pieces.append(("hub state", json.dumps(state)))
        skip = {self.hub.state_path.resolve()}
        for path in sorted(self.world.rglob("*")):
            if path.is_file() and path.resolve() not in skip and not path.is_relative_to(self.server):
                label = str(path.relative_to(self.world))
                if path.suffix == ".bundle":
                    pieces.append((label, self.bundle_text(path)))
                else:
                    pieces.append((label, path.read_text(encoding="utf-8", errors="replace")))
        for run in self.runs:  # a job's outputs are read by later jobs and by the report; no file holds them
            for job in run.all_jobs():
                pieces.append((f"outputs/run-{run.run_number}-{run.attempt}/{job.name}", json.dumps(job.outputs)))
        pieces.append(("server repository", self.objects_text(self.origin)))
        return pieces

    def objects_text(self, git_dir: Path) -> str:
        done = subprocess.run(["git", "--git-dir", str(git_dir), "cat-file", "--batch-all-objects", "--batch"],
                              capture_output=True, check=False, env={"PATH": SYSTEM_PATH})
        return done.stdout.decode("utf-8", errors="replace")

    def bundle_text(self, bundle: Path) -> str:
        scratch = self.root / "unbundle"
        shutil.rmtree(scratch, ignore_errors=True)
        self.git("init", "-q", "--bare", str(scratch))
        self.git("--git-dir", str(scratch), "fetch", "-q", str(bundle), "refs/heads/*:refs/heads/*", check=False)
        text = self.objects_text(scratch)
        shutil.rmtree(scratch, ignore_errors=True)
        return text

    # -------------------------------------------------------------------------------------- facts about the world
    def pr_files(self, pr: dict) -> list[str]:
        """The files a pull request changes: its head branch against the base, in the server repository."""
        base = self.git("--git-dir", str(self.origin), "merge-base", self.base_sha, pr["headRefOid"])
        return self.git("--git-dir", str(self.origin), "diff", "--name-only", base, pr["headRefOid"]).splitlines()

    def secret_values_received(self, run: Run, names: tuple[str, ...]) -> dict[str, list[str]]:
        """Where a job process received a non-empty value for any of the named secrets: {job: [variable names]}."""
        values = {self.secrets[n]: n for n in names if self.secrets.get(n)}
        found: dict[str, list[str]] = {}
        for job in run.all_jobs():
            for step in job.ran():
                for variable, value in step.env.items():
                    if value in values:
                        found.setdefault(job.name, []).append(f"{step.id or step.name}:{variable}")
        return found


_FAKE_NPM = """#!{python}
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"argv": args, "env": sorted(os.environ)}}) + "\\n")
if "--ignore-scripts" not in args:
    sys.stderr.write("the fake npm only installs with --ignore-scripts\\n")
    sys.exit(2)
layouts = {layouts!r}
rel, source = layouts[args[-1]]
prefix = pathlib.Path(args[args.index("--prefix") + 1])
target = prefix / rel
target.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source, target)
target.chmod(0o755)
"""

_FAKE_NODE = """#!{python}
import sys
print("v22.23.4" if sys.argv[1:] == ["--version"] else "")
"""


def make_stub_kaggle(directory: Path) -> None:
    """The kaggle CLI that models-up installs (kaggle==2.2.4), as an empty stub wheel. The sandbox reaches no package index."""
    directory.mkdir(parents=True)
    with zipfile.ZipFile(directory / "kaggle-2.2.4-py3-none-any.whl", "w") as wheel:
        wheel.writestr("kaggle/__init__.py", "")
        wheel.writestr("kaggle-2.2.4.dist-info/METADATA", "Metadata-Version: 2.1\nName: kaggle\nVersion: 2.2.4\n")
        wheel.writestr("kaggle-2.2.4.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: sandbox\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr("kaggle-2.2.4.dist-info/RECORD", "")
    with zipfile.ZipFile(directory / "six-1.17.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("six.py", "")
        wheel.writestr("six-1.17.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: six\nVersion: 1.17.0\n")
        wheel.writestr("six-1.17.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: sandbox\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr("six-1.17.0.dist-info/RECORD", "")


def rmtree(path: Path) -> None:
    """Remove a job's disk even where the engine made files read-only."""
    for current, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(current, name), 0o700, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
    shutil.rmtree(path, ignore_errors=True)


def parse_outputs(text: str) -> dict[str, str]:
    """GITHUB_OUTPUT as the runner reads it: ``name=value`` lines and ``name<<DELIMITER`` blocks."""
    found: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            name, delimiter = line.split("<<", 1)
            block = []
            while i < len(lines) and lines[i] != delimiter:
                block.append(lines[i])
                i += 1
            i += 1
            found[name] = "\n".join(block)
        elif "=" in line:
            name, value = line.split("=", 1)
            found[name] = value
    return found


class BackgroundPiv:
    """``gh piv`` running in a thread, so a test can fire the workflow while the command waits for ack's comment."""

    def __init__(self, sandbox: Sandbox, *args: str):
        self.result: subprocess.CompletedProcess | None = None
        self.thread = threading.Thread(target=self._go, args=(sandbox, args), daemon=True)
        self.thread.start()

    def _go(self, sandbox: Sandbox, args: tuple[str, ...]) -> None:
        self.result = sandbox.piv(*args, timeout=300)

    def wait_for_issue(self, sandbox: Sandbox, timeout: float = 60) -> int:
        end = time.time() + timeout
        while time.time() < end:
            issues = sandbox.hub.state()["issues"]
            labelled = [int(n) for n, i in issues.items() if "pivotal" in i["labels"]]
            if labelled:
                return labelled[0]
            time.sleep(0.2)
        raise AssertionError("piv do never labelled an issue")

    def join(self) -> subprocess.CompletedProcess:
        self.thread.join(300)
        assert self.result is not None, "piv did not finish"
        return self.result
