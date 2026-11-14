"""The models-up job: one Kaggle serving session for every repo that needs one.

It runs only when the plan's routed chain holds a ``pi:kaggle/*`` entry (``kaggle_needed``). It holds no GitHub
permission and no GitHub token; its secrets are the Kaggle account (``KAGGLE_USERNAME``, ``KAGGLE_KEY``) and the
endpoint (``PIVOTAL_KAGGLE_URL``, ``PIVOTAL_KAGGLE_KEY``). It runs no model output and no repo code. The steps:

1. ``GET https://<domain>/v1/models`` with the key. If it answers, the live session is reused, whichever run started it.
2. Otherwise ``kaggle kernels status <you>/pivotal-serve``. ``queued`` or ``running`` means another run, in any repo, is
   starting it: do not push, wait for health. The kernel's status is the only cross-repo signal (Kaggle holds that state); check-then-push is not atomic.
3. Only when the endpoint is down and the kernel is idle (complete, error, cancelled, or never pushed), look once more
   at both, then ``kaggle kernels push`` the notebook ``kaggle/render.py`` renders for the preset's ``kaggle.model``
   (private, GPU T4 x2, internet on, the dataset ``<you>/pivotal-serve-secrets`` attached).
4. Poll for at most ``WAIT_SECONDS`` (12 minutes, the last look at the endpoint included). If it never answers, the Kaggle entries are skipped: ``kaggle_ok=false``
   and ``kaggle_why`` says why (``queued``, ``quota``, ``failed`` and the others in WHY). A refused push is a visible
   reason, never a retry: the job pushes at most once.

What this cannot do: the check and the push are two calls, so two runs that read an idle kernel in the same few seconds
can both push. The second look just before the push narrows that window, and the notebook closes it from the other
side: it exits at once when the ngrok domain is already bound (kaggle/serve.ipynb.tmpl), so a duplicate push costs a
few minutes of one more kernel version, not a second model.

Without ``KAGGLE_USERNAME`` and ``KAGGLE_KEY`` (an untrusted run: the caller passes it only the endpoint secrets, F2b) the
job can only reuse a live session. It reads no kernel status and pushes nothing.

Every answer of the endpoint is hostile data. It is read with a size and time cap, never followed to another host, and
reduced to ``up`` (a 200 whose body is a JSON object with a ``data`` list), ``unauthorized`` (401, 403), ``down`` (no
answer, 404, 5xx: ngrok answers 404 for a domain with no tunnel and llama-server 503 while it loads) or ``odd`` (a
redirect, or a 200 that is not that JSON, too big or too slow). ``unauthorized`` and ``odd`` stop the job at once with no
push: something answers on the domain that this key does not open.

``PIVOTAL_KEYLESS_CHECK=1`` (off by default) adds the live check docs/engines.md asks for before the first real
session: with the session up, every route of KEYLESS_ROUTES requested without the key must answer 401. Anything else
is ``kaggle_ok=false`` (``keyless_open``): the key does not cover that route.

The process is non-dumpable and takes its secrets out of ``os.environ`` first (the pattern of engine/buildjob.py); the
kaggle CLI gets the Kaggle account and nothing else, and nothing kaggle or the endpoint prints is echoed. The outputs are
``kaggle_ok`` (true/false) and ``kaggle_why``, a word of WHY. A bad configuration (a malformed URL or key) refuses with a
non-zero exit before anything is written or contacted.
"""

import contextlib
import http.client
import importlib.util
import json
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script under python3 -I, which puts no directory on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import job  # noqa: E402
from engine.job import JobError  # noqa: E402

ROOT = job.ROOT
KERNEL = "pivotal-serve"  # one fixed private kernel per Kaggle account
DATASET = "pivotal-serve-secrets"  # gh-piv/piv/serve.py DATASET: the private dataset the notebook reads

# The budgets (F8). timeout-minutes is derived from them: test_factory checks that they agree.
WAIT_SECONDS = 12 * 60  # the one wait of decide
POLL_SECONDS = 20
GRACE_SECONDS = 90  # after our own push, a terminal status may still be the previous run's
PROBE_SECONDS = 10  # one request, from the first send to the last byte: a total deadline (the connect and TLS handshake have only their own per-receive timeout)
KAGGLE_SECONDS = 60  # one `kaggle kernels status`
PUSH_SECONDS = 180
MAX_BODY = 64 * 1024
JOB_OVERHEAD_MINUTES = 8  # checkouts and the install of the kaggle CLI
# Worst case: two looks (a probe and a status each) before the push, the push, the keyless routes, the wait and the
# iteration that straddles its end.
JOB_MINUTES = math.ceil(
    (2 * (PROBE_SECONDS + KAGGLE_SECONDS) + PUSH_SECONDS + 5 * PROBE_SECONDS + WAIT_SECONDS + PROBE_SECONDS + KAGGLE_SECONDS) / 60
) + JOB_OVERHEAD_MINUTES

# The word a refused push writes, and the one the build and review jobs read as KAGGLE_WHY (engine/buildjob.py kaggle_quota_refused).
QUOTA_WHY = "quota"

# The word in kaggle_why. The first three are success; everything else is a visible reason the Kaggle entries are skipped.
WHY = {
    "live": "a live session answers: reusing it",
    "started": "this run started the session and it answers",
    "waited": "another run was starting the session and it answers",
    "no_endpoint": "the endpoint secrets PIVOTAL_KAGGLE_URL and PIVOTAL_KAGGLE_KEY are not set",
    "no_credentials": "the endpoint is down and this run holds no Kaggle account, so it cannot start a session",
    "no_model": "the endpoint is down and the preset names no single kaggle model to start",
    "rejected_key": "the endpoint answered 401 or 403: the key was rejected (rotated?), so no session is started",
    "odd_answer": "the endpoint answered something that is not a model server (redirect, wrong body, too big or too slow)",
    "kernel_unreadable": "the kernel's status could not be read, so nothing is pushed",
    "render_refused": "the serve notebook could not be rendered for this model (see the line above)",
    QUOTA_WHY: "Kaggle refused the push: the weekly GPU quota is used up until its reset",
    "push_failed": "Kaggle refused or failed the push",
    "queued": "the kernel stayed queued for 12 minutes",
    "not_answering": "the kernel is running but the endpoint did not answer in 12 minutes",
    "timeout": "the endpoint did not answer in 12 minutes",
    "failed": "the kernel ended before the endpoint answered",
    "keyless_open": "a route answered without the key (the keyless check failed): the session is not used",
}
OK_WHY = ("live", "started", "waited")

# The routes llama-server answers and the one it leaves open. Each of these, without the key, must be a 401.
KEYLESS_ROUTES = (
    ("GET", "/v1/models"), ("GET", "/props"), ("GET", "/slots"), ("GET", "/metrics"), ("POST", "/v1/chat/completions"),
)
KEYLESS_OPEN_ROUTE = "/health"  # not requested: the one route that may answer without the key

LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
# https://<domain> and nothing else: no port, user, path or query. At least two labels and a top label that starts with
# a letter, so an IP address (127.0.0.1, 169.254.169.254) is not a domain.
TLD = r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
URL = re.compile(rf"https://((?:{LABEL}\.)+{TLD})/?")
KAGGLE_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SECRET_VALUE = re.compile(r"[!-~]{8,300}")  # printable ASCII, no space: it goes into a header and an environment
MODEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
KERNEL_WORD = re.compile(r"KernelWorkerStatus\.([A-Z_]+)")
NOT_FOUND = re.compile(r"(?=.*\b404\b)(?=.*not found)", re.IGNORECASE | re.DOTALL)
# Measured live (kaggle 2.2.4): a kernel that was never pushed answers with this text and no 404.
NEVER_PUSHED = re.compile(r"Cannot access kernel '[^']+' \(Permission 'kernels\.get' was denied\)")
QUOTA = re.compile(r"quota|gpu[ -]hours?", re.IGNORECASE)
PUSHED = "successfully pushed"
KAGGLE_ENV_PASSED = ("PATH", "HOME")  # and the Kaggle account (KAGGLE_API_TOKEN too, for a KGAT_ key); nothing else
# kaggle's words for the kernel: queued and running mean a start is under way; a cancel that is not acknowledged still counts.
BUSY = {"QUEUED": "queued", "NEW_SCRIPT": "queued", "RUNNING": "running", "CANCEL_REQUESTED": "running"}
TERMINAL = {"COMPLETE": "complete", "ERROR": "error", "CANCEL_ACKNOWLEDGED": "cancelled"}


class Refused(Exception):
    """A step that ends the job with a reason (a word of WHY) and, optionally, a fixed line to print."""

    def __init__(self, why: str, note: str = ""):
        super().__init__(why)
        self.why = why
        self.note = note


@dataclass(frozen=True)
class Config:
    domain: str
    key: str  # the endpoint key: a header value, nowhere else
    user: str  # "" without a Kaggle account
    kaggle_key: str
    model: str  # "" when the preset names no single kaggle model
    keyless: bool
    binary: str

    @property
    def can_push(self) -> bool:
        return bool(self.user and self.kaggle_key)


def read_config(env: Mapping[str, str], *, need_kaggle: bool = True) -> Config:
    """The checked inputs. A malformed value refuses (JobError) before anything is contacted; an unset one is a value
    the job can work around (the run then ends with a reason), not a mistake."""
    url = env.get("PIVOTAL_KAGGLE_URL", "").strip()
    domain = ""
    if url:
        match = URL.fullmatch(url)
        if match is None or len(url) > 270:
            raise JobError("PIVOTAL_KAGGLE_URL must be https://<domain> with no port, user, path or IP address")
        domain = match.group(1).rstrip("/").lower()
    key = env.get("PIVOTAL_KAGGLE_KEY", "")
    if key and not SECRET_VALUE.fullmatch(key):
        raise JobError("PIVOTAL_KAGGLE_KEY is not 8 to 300 printable characters")
    user, kaggle_key = "", ""
    if need_kaggle:
        user, kaggle_key = env.get("KAGGLE_USERNAME", "").strip(), env.get("KAGGLE_KEY", "")
        if user and not KAGGLE_USER.fullmatch(user):
            raise JobError("KAGGLE_USERNAME is not a Kaggle account name")
        if kaggle_key and not SECRET_VALUE.fullmatch(kaggle_key):
            raise JobError("KAGGLE_KEY is not 8 to 300 printable characters")
    model = env.get("KAGGLE_MODEL", "")
    if model and not MODEL.fullmatch(model):
        raise JobError("KAGGLE_MODEL is not a model id")
    keyless = env.get("PIVOTAL_KEYLESS_CHECK", "")
    if keyless not in ("", "0", "1"):
        raise JobError("PIVOTAL_KEYLESS_CHECK must be 1 or unset")
    return Config(domain, key if domain else "", user, kaggle_key, model, keyless == "1", env.get("PIV_KAGGLE") or "kaggle")


# --- the endpoint ---------------------------------------------------------------------------------------------------


def https_connection(domain: str) -> http.client.HTTPConnection:
    return http.client.HTTPSConnection(domain, timeout=PROBE_SECONDS, context=ssl.create_default_context())


Connect = Callable[[str], http.client.HTTPConnection]


def set_deadline(conn: http.client.HTTPConnection, deadline: float, clock: Callable[[], float]) -> None:
    """The socket waits at most what is left until ``deadline`` (a little over zero when it has passed)."""
    if conn.sock is not None:
        conn.sock.settimeout(max(0.01, deadline - clock()))


def cut(conn: http.client.HTTPConnection) -> None:
    """The total deadline has passed: shut the connection's socket down so any blocked receive ends at once."""
    sock = conn.sock
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            sock.close()


def request(domain: str, path: str, key: str | None, *, method: str = "GET", connect: Connect = https_connection,
            clock: Callable[[], float] = time.monotonic) -> tuple[int | None, bytes | None]:
    """(status, body). status is None when nothing answered. The body is read only for a 200, with a size and a time cap,
    and is None when it was cut off. A redirect is never followed: its status is returned as it is."""
    headers = {"User-Agent": "pivotal-models-up", "Accept": "application/json", "Connection": "close"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    payload = None
    if method == "POST":
        payload, headers["Content-Type"] = b"{}", "application/json"
    try:
        conn = connect(domain)
    except (OSError, http.client.HTTPException):
        return None, None
    deadline = clock() + PROBE_SECONDS
    # A TOTAL deadline for the whole exchange: the socket timeout below is per receive, so a server that trickles the status
    # line or the headers inside each timeout would never be cut. At the deadline the socket is shut down, and the blocked
    # read fails with an OSError, which is no answer (or, after a 200, a body that never finished).
    timer = threading.Timer(PROBE_SECONDS, cut, (conn,))
    timer.daemon = True
    timer.start()
    try:
        conn.request(method, path, body=payload, headers=headers)
        # The deadline is on the socket: a read that would wait past it times out instead of blocking (F3). It is set before the
        # response line is read and again before every body read, to what is left.
        set_deadline(conn, deadline, clock)
        response = conn.getresponse()
        if response.status != 200:
            return response.status, None
        chunks, size = [], 0
        while True:
            set_deadline(conn, deadline, clock)
            try:
                chunk = response.read1(4096)
            except OSError:  # the deadline (TimeoutError) or a reset after the headers: a body that never finished is no answer
                return 200, None
            if not chunk:
                return 200, b"".join(chunks)
            size += len(chunk)
            if size > MAX_BODY or clock() > deadline:
                return 200, None
            chunks.append(chunk)
    except (OSError, http.client.HTTPException, ValueError):
        return None, None
    finally:
        timer.cancel()
        conn.close()


def endpoint_state(cfg: Config, *, connect: Connect = https_connection, clock: Callable[[], float] = time.monotonic) -> str:
    """up, unauthorized, down or odd, for `GET /v1/models` with the key."""
    status, body = request(cfg.domain, "/v1/models", cfg.key, connect=connect, clock=clock)
    if status is None:
        return "down"
    if status in (401, 403):
        return "unauthorized"
    if 300 <= status < 400:
        return "odd"
    if status != 200:
        return "down"
    if body is None:
        return "odd"
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return "odd"
    models = data.get("data") if isinstance(data, dict) else None
    return "up" if isinstance(models, list) and 1 <= len(models) <= 64 else "odd"


def keyless_open(cfg: Config, *, connect: Connect = https_connection, clock: Callable[[], float] = time.monotonic) -> list[str]:
    """The routes of KEYLESS_ROUTES that did not answer 401 without the key (an unreachable one counts as open: it was not
    shown to be closed). Empty means the key covers them all."""
    bad = []
    for method, path in KEYLESS_ROUTES:
        status, _ = request(cfg.domain, path, None, method=method, connect=connect, clock=clock)
        if status != 401:
            bad.append(path)
    return bad


# --- kaggle ---------------------------------------------------------------------------------------------------------


class Kaggle:
    """The kaggle CLI with the Kaggle account and nothing else in its environment. Its output is read, never printed."""

    def __init__(self, binary: str, env: Mapping[str, str], user: str, key: str):
        self.binary = binary
        self.env = {name: env[name] for name in KAGGLE_ENV_PASSED if name in env}
        self.env.update({"KAGGLE_USERNAME": user, "KAGGLE_KEY": key})
        if key.startswith("KGAT_"):
            # Measured live (kaggle 2.2.4): an API token (KGAT_...) given as KAGGLE_KEY authenticates as nobody and
            # reads empty lists; the CLI honours it only as KAGGLE_API_TOKEN.
            self.env["KAGGLE_API_TOKEN"] = key
        self.ref = f"{user}/{KERNEL}"

    def run(self, *args: str, timeout: float) -> tuple[int, str]:
        try:
            done = subprocess.run([self.binary, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  errors="replace", check=False, timeout=timeout, env=self.env)
        except (OSError, ValueError, subprocess.SubprocessError):
            return -1, ""
        return done.returncode, done.stdout + "\n" + done.stderr

    def word(self, timeout: float = KAGGLE_SECONDS) -> str:
        """The kernel's state: queued, running, complete, error, cancelled, none (never pushed) or unknown."""
        code, text = self.run("kernels", "status", self.ref, timeout=min(timeout, KAGGLE_SECONDS))
        if code != 0:
            return "none" if code > 0 and (NOT_FOUND.search(text) or NEVER_PUSHED.search(text)) else "unknown"
        found = KERNEL_WORD.search(text)
        if found is None:
            return "unknown"
        return BUSY.get(found.group(1)) or TERMINAL.get(found.group(1)) or "unknown"


def render_notebook(model: str) -> str:
    """kaggle/render.py's notebook for the catalog model, or Refused. Unmeasured candidates and null pins are its refusals."""
    spec = importlib.util.spec_from_file_location("pivotal_kaggle_render", ROOT / "kaggle" / "render.py")
    if spec is None or spec.loader is None:
        raise Refused("render_refused", "kaggle/render.py cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        catalog = json.loads((ROOT / "kaggle" / "catalog.json").read_text(encoding="utf-8"))
        pins = json.loads((ROOT / "kaggle" / "pins.json").read_text(encoding="utf-8"))
        return module.render_text(catalog, model, pins)
    except module.RenderError as err:  # worded from catalog fields and rules, never from a secret
        raise Refused("render_refused", f"render refused: {err}") from None
    except (OSError, ValueError):
        raise Refused("render_refused", "kaggle/catalog.json or kaggle/pins.json cannot be read") from None


def push_notebook(kaggle: Kaggle, user: str, model: str, render: Callable[[str], str] = render_notebook) -> None:
    """Render, write the folder (0700, removed after) and `kaggle kernels push` once. Refused or failed is a Refused."""
    text = render(model)
    metadata = {
        "id": f"{user}/{KERNEL}", "title": KERNEL, "code_file": "serve.ipynb", "language": "python",
        "kernel_type": "notebook", "is_private": True, "enable_gpu": True, "enable_internet": True,
        "machine_shape": "NvidiaTeslaT4", "dataset_sources": [f"{user}/{DATASET}"],
        "competition_sources": [], "kernel_sources": [], "model_sources": [],
    }
    folder = Path(tempfile.mkdtemp(prefix="pivotal-serve-"))
    try:
        (folder / "serve.ipynb").write_text(text, encoding="utf-8")
        (folder / "kernel-metadata.json").write_text(json.dumps(metadata, indent=1), encoding="utf-8")
        code, output = kaggle.run("kernels", "push", "-p", str(folder), timeout=PUSH_SECONDS)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if code == 0 and PUSHED in output:
        return
    raise Refused(QUOTA_WHY if QUOTA.search(output) else "push_failed")


# --- the decision ---------------------------------------------------------------------------------------------------


@dataclass
class Deps:
    """What the decision touches. The job fills it with the endpoint and the kaggle CLI; a test fills it with fakes."""

    endpoint: Callable[[], str]
    kernel: Callable[..., str] | None  # None without a Kaggle account; the wait passes ``timeout=`` (seconds left)
    push: Callable[[], None] | None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    say: Callable[[str], None] = print


def decide(deps: Deps, has_model: bool = True) -> tuple[str, bool]:
    """(why, pushed). The whole models-up rule. Pushes at most once; never loops on a refusal."""
    first = deps.endpoint()
    if first == "up":
        return "live", False
    if first in ("unauthorized", "odd"):
        return ("rejected_key" if first == "unauthorized" else "odd_answer"), False
    if deps.kernel is None or deps.push is None:
        return "no_credentials", False
    word = deps.kernel()
    if word == "unknown":
        return "kernel_unreadable", False
    pushed = False
    if word in ("queued", "running"):
        deps.say("models-up: the kernel is " + word + ": another run is starting it, so nothing is pushed")
    else:
        # idle. Look once more at both: another run may have pushed since the first look.
        again = deps.endpoint()
        if again == "up":
            return "live", False
        if again in ("unauthorized", "odd"):
            return ("rejected_key" if again == "unauthorized" else "odd_answer"), False
        word = deps.kernel()
        if word == "unknown":
            return "kernel_unreadable", False
        if word not in ("queued", "running"):
            if not has_model:
                return "no_model", False
            try:
                deps.push()
            except Refused as err:
                if err.note:
                    deps.say("models-up: " + err.note)
                return err.why, False
            pushed = True
            deps.say("models-up: pushed the serve notebook")
            word = "queued"
    return wait(deps, pushed, word)


def wait(deps: Deps, pushed: bool, word: str) -> tuple[str, bool]:
    """Poll for WAIT_SECONDS. The endpoint is probed first on every turn, so a session that came up is never missed."""
    start = deps.clock()
    # The whole wait, the last look at the endpoint included, fits in WAIT_SECONDS: sleeps and the status read are cut to
    # what is left, and PROBE_SECONDS stay in reserve for the next look.
    deadline = start + WAIT_SECONDS - PROBE_SECONDS
    ended = 0
    while True:
        answer = deps.endpoint()
        if answer == "up":
            return ("started" if pushed else "waited"), pushed
        if answer in ("unauthorized", "odd"):
            return ("rejected_key" if answer == "unauthorized" else "odd_answer"), pushed
        left = deadline - deps.clock()
        if left <= 0:
            break
        deps.sleep(min(POLL_SECONDS, left))
        left = deadline - deps.clock()
        if left <= 0:
            continue  # one last look at the endpoint, no status read
        seen = deps.kernel(timeout=left) if deps.kernel else "unknown"
        if seen in ("queued", "running"):
            word, ended = seen, 0
        elif seen in ("complete", "error", "cancelled", "none") and (not pushed or deps.clock() - start >= GRACE_SECONDS):
            ended += 1  # twice in a row: one read of Kaggle's state is not a verdict
            if ended >= 2:
                return "failed", pushed
        else:
            ended = 0
    return {"queued": "queued", "running": "not_answering"}.get(word, "timeout"), pushed


def build_deps(cfg: Config, env: Mapping[str, str], connect: Connect = https_connection) -> Deps:
    kaggle = Kaggle(cfg.binary, env, cfg.user, cfg.kaggle_key) if cfg.can_push else None
    return Deps(
        endpoint=lambda: endpoint_state(cfg, connect=connect),
        kernel=kaggle.word if kaggle else None,
        push=(lambda: push_notebook(kaggle, cfg.user, cfg.model)) if kaggle else None,
    )


def run(env: Mapping[str, str], deps: Deps | None = None, connect: Connect = https_connection) -> None:
    out_path = job.read_output_path(env)
    cfg = read_config(env)
    if not cfg.domain or not cfg.key:
        why, ok = "no_endpoint", False
    else:
        deps = deps or build_deps(cfg, env, connect)
        why, _pushed = decide(deps, has_model=bool(cfg.model))
        ok = why in OK_WHY
        if ok and cfg.keyless:
            bad = keyless_open(cfg, connect=connect)
            if bad:
                print("models-up: answered without the key: " + ", ".join(bad))
                why, ok = "keyless_open", False
    print(f"models-up: {'ok' if ok else 'not ok'} ({why}): {WHY[why]}")
    if not ok:
        print(f"::warning::models-up: the Kaggle entries are skipped ({why})")
    job.write_outputs(out_path, {"kaggle_ok": "true" if ok else "false", "kaggle_why": why})


def recheck(env: Mapping[str, str], connect: Connect = https_connection) -> str:
    """Each build and review re-checks the endpoint when it starts: one probe with the key, no kaggle, no push.
    Returns what `endpoint_state` saw: up, unauthorized, odd or down. An unset or malformed URL or key is down."""
    try:
        cfg = read_config(env, need_kaggle=False)
    except JobError:
        return "down"
    usable = bool(cfg.domain and cfg.key)
    return endpoint_state(cfg, connect=connect) if usable else "down"


def main(argv: list[str], environ) -> int:
    if len(argv) != 1:
        print("usage: modelsup.py", file=sys.stderr)
        return 2
    from engine import buildjob  # the engine pattern: this process holds the secrets, so it is non-dumpable first

    try:
        buildjob.make_unreadable()
    except JobError as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1
    env = buildjob.take_environment(environ)
    return job.run_job(run, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
