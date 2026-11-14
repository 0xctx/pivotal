"""engine/run.sh: agent_run, run_gate and strip_env (F3).

Each test runs the real script as a subprocess, with fake agents and checks on PATH, the way a job
runs them. Secret values are fakes; no test calls a real provider.
"""

import json
import os
import shlex
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import fakecli
from engine import route

ROOT = Path(__file__).resolve().parent.parent
RUN_SH = str(ROOT / "engine" / "run.sh")

# Every door secret and GitHub token, each with a value that must never show up in a log.
SECRETS = {name: f"leak-{name.lower()}" for names in route.DOOR_SECRETS.values() for name in names}
SECRETS.update(GITHUB_TOKEN="leak-github-token", GH_TOKEN="leak-gh-token")
SECRETS["EXTRA_SERVICE_TOKEN"] = "leak-extra-service-token"  # not in any list: caught by pattern
ACTIONS = {
    "ACTIONS_RUNTIME_TOKEN": "leak-actions-runtime",
    "ACTIONS_RESULTS_URL": "leak-actions-results",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "leak-actions-idtoken",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "leak-actions-idurl",
}
# Names a name-list filter can miss: a bare TOKEN, a PAT, a lowercase name, and the runner's own
# files and socket. Each value must never reach a job.
SNEAKY = {
    "TOKEN": "leak-bare-token",
    "PASSWORD": "leak-password",
    "DB_PASSWD": "leak-db-passwd",
    "MYAPP_PAT": "leak-myapp-pat",
    "NPM_AUTH": "leak-npm-auth",
    "github_token": "leak-lower-github-token",
    "aws_secret_access_key": "leak-aws-secret",
    "SSH_AUTH_SOCK": "leak-ssh-auth-sock",
    "GITHUB_ENV": "leak-github-env",
    "GITHUB_PATH": "leak-github-path",
    "GITHUB_OUTPUT": "leak-github-output",
    "GITHUB_STATE": "leak-github-state",
    "GITHUB_STEP_SUMMARY": "leak-github-step-summary",
    "actions_runtime_token": "leak-lower-actions-runtime",
}
LEAKS = {**SECRETS, **ACTIONS, **SNEAKY}


def _names(env_dump: str) -> set[str]:
    """The variable names in an `env` dump, one per line."""
    return {line.split("=", 1)[0] for line in env_dump.splitlines()}


def _alive(pid: int) -> bool:
    """True while the process runs. A zombie has exited, so it does not count."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


class RunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bins = fakecli.FakeBin(self.root / "bin")
        # A job's environment: PATH with the fakes first, every secret and ACTIONS_* token set.
        self.env = self.bins.env({"PATH": os.environ["PATH"], "HOME": str(self.root), **LEAKS})

    def tearDown(self):
        self._tmp.cleanup()

    def run_fn(self, fn: str, *args: str, env: dict[str, str] | None = None):
        return subprocess.run(
            ["bash", RUN_SH, fn, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=self.env if env is None else env,
        )

    def leftover_pid(self, name: str) -> int:
        """The pid a check or agent wrote to a file: a background sleep it left running."""
        pidfile = self.root / name
        pid = int(pidfile.read_text(encoding="utf-8"))
        self.addCleanup(_kill_quietly, pid)
        return pid

    def run_fn_with_socket_on_stdin(self, fn: str, *args: str, env: dict[str, str]):
        """Runs fn from a driver script under job control, with a socket on stdin. Job control keeps
        a background job's stdin, so the launch inherits the socket. A script, not `bash -c`, so the
        shell itself does not read ~/.bashrc at startup."""
        driver = self.root / "driver.sh"
        call = " ".join(shlex.quote(arg) for arg in (fn, *args))
        driver.write_text(f"set -m\nsource {shlex.quote(RUN_SH)}\n{call}\n", encoding="utf-8")
        return _run_with_socket_stdin(["bash", str(driver)], env)

    def run_under_job_control(self, fn: str, *args: str):
        """Runs fn with job control on, as an interactive shell does. Each background job then leads
        its own process group, which is the case the group kill must still get right."""
        script = "set -m; source {}; {} {}".format(
            shlex.quote(RUN_SH), fn, " ".join(shlex.quote(arg) for arg in args)
        )
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False, timeout=60, env=self.env
        )


def _run_with_socket_stdin(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """Runs argv with a socket on stdin, as sshd or rsh would start a shell."""
    parent, child = socket.socketpair()
    try:
        return subprocess.run(
            argv, stdin=child.fileno(), capture_output=True, text=True, check=False, timeout=60,
            env=env,
        )
    finally:
        parent.close()
        child.close()


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def _leftover(pidfile: Path) -> str:
    # Starts a sleep that outlives the shell, then exits: the group kill must reap it. Its output
    # is dropped, so it does not hold a pipe open after the shell has exited.
    return f"sleep 300 >/dev/null 2>&1 & echo $! > {pidfile}"


class AgentRunTest(RunTest):
    def agent(self, *cmd: str, token: str = "CLAUDE_CODE_OAUTH_TOKEN", max_time: str = "30"):
        return self.run_fn(
            "agent_run",
            "--max-time",
            max_time,
            "--log",
            str(self.root / "agent.log"),
            "--token-env",
            token,
            "--",
            *cmd,
        )

    def test_ok_when_the_agent_exits_zero_and_its_output_goes_to_the_log(self):
        self.bins.add("claude", stdout="done\n")
        proc = self.agent("claude")
        self.assertEqual((proc.returncode, proc.stdout), (0, "ok\n"))
        self.assertEqual((self.root / "agent.log").read_text(encoding="utf-8"), "done\n")

    def test_the_agent_log_is_private_whatever_the_caller_umask(self):
        # The log holds agent output, so it is created 0600 even when the caller's umask is 000.
        self.bins.add("claude", stdout="done\n")
        log = self.root / "agent.log"
        proc = subprocess.run(
            ["bash", "-c", 'umask 000; exec bash "$0" agent_run --max-time 30 --log "$1" '
             '--token-env CLAUDE_CODE_OAUTH_TOKEN -- claude', RUN_SH, str(log)],
            capture_output=True, text=True, check=False, timeout=60, env=self.env,
        )
        self.assertEqual(proc.stdout, "ok\n")
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)

    def test_error_carries_the_exit_code(self):
        self.bins.add("claude", code=3, stderr="boom\n")
        proc = self.agent("claude")
        self.assertEqual(proc.stdout, "error:3\n")

    def test_errlog_receives_stderr_and_stdout_log_does_not(self):
        # When --errlog is given, stderr goes to errlog and stdout to --log (run.sh agent_run).
        # The step output and exit status are unchanged.
        self.bins.add("claude", stdout="out-line\n", stderr="warn-line\n")
        log = self.root / "agent.log"
        errlog = self.root / "agent.err"
        proc = self.run_fn(
            "agent_run", "--max-time", "30", "--log", str(log), "--errlog", str(errlog),
            "--token-env", "CLAUDE_CODE_OAUTH_TOKEN", "--", "claude",
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, "ok\n"))
        self.assertEqual(log.read_text(encoding="utf-8"), "out-line\n")
        self.assertEqual(errlog.read_text(encoding="utf-8"), "warn-line\n")
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(errlog).st_mode), 0o600)

    def test_error_when_the_command_is_missing(self):
        proc = self.agent("no-such-agent-binary")
        self.assertEqual(proc.stdout, "error:127\n")

    def agent_env(self, log: Path) -> dict:
        return json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["env"]

    def test_every_agent_call_gets_disable_autoupdater_and_no_agent_directory_unless_one_is_named(self):
        # A CLI that updates itself would run a version the job did not pin (docs/engines.md).
        log = self.root / "env.jsonl"
        self.bins.add("claude", env_log=log)
        self.assertEqual(self.agent("claude").stdout, "ok\n")
        seen = self.agent_env(log)
        self.assertEqual(seen.get("DISABLE_AUTOUPDATER"), "1")
        self.assertEqual(seen.get("PI_SKIP_VERSION_CHECK"), "1", "pi makes no version request")
        self.assertEqual(seen.get("PI_TELEMETRY"), "0", "pi sends no telemetry")
        self.assertNotIn("PI_CODING_AGENT_DIR", seen)

    def test_the_gate_gets_none_of_the_agent_only_variables(self):
        proc = self.run_fn("run_gate", "30", str(self.root / "gate.log"), "", "env")
        self.assertEqual(proc.stdout, "green\n")
        names = _names((self.root / "gate.log").read_text(encoding="utf-8"))
        for name in ("DISABLE_AUTOUPDATER", "PI_SKIP_VERSION_CHECK", "PI_TELEMETRY", "PI_CODING_AGENT_DIR"):
            self.assertNotIn(name, names, name)

    def test_with_an_agent_dir_the_agent_gets_pi_coding_agent_dir_and_only_its_one_secret(self):
        log = self.root / "env.jsonl"
        self.bins.add("pi", env_log=log)
        agent_dir = self.root / "pi-agent"
        agent_dir.mkdir(mode=0o700)
        proc = self.run_fn(
            "agent_run", "--max-time", "30", "--log", str(self.root / "agent.log"), "--token-env", "CLAUDE_CODE_OAUTH_TOKEN",
            "--agent-dir", str(agent_dir), "--", "pi",
        )
        self.assertEqual(proc.stdout, "ok\n")
        seen = self.agent_env(log)
        self.assertEqual(seen.get("PI_CODING_AGENT_DIR"), str(agent_dir))
        self.assertEqual(seen.get("CLAUDE_CODE_OAUTH_TOKEN"), self.env["CLAUDE_CODE_OAUTH_TOKEN"])
        for name in LEAKS:
            if name != "CLAUDE_CODE_OAUTH_TOKEN":
                self.assertNotIn(name, seen, name)

    def test_an_agent_dir_that_is_not_an_absolute_plain_path_is_refused_before_the_agent_runs(self):
        log = self.root / "env.jsonl"
        self.bins.add("pi", env_log=log)
        for bad in ("relative/dir", "/tmp/with space", "/tmp/$(id)", "/tmp/a\nb", "/tmp/a;b", ""):
            with self.subTest(bad=bad):
                proc = self.run_fn(
                    "agent_run", "--max-time", "30", "--log", str(self.root / "agent.log"), "--token-env",
                    "CLAUDE_CODE_OAUTH_TOKEN", "--agent-dir", bad, "--", "pi",
                )
                self.assertEqual(proc.returncode, 2)
                self.assertNotIn("ok", proc.stdout)
        self.assertFalse(log.exists(), "no agent ran")

    def test_timeout_when_the_agent_runs_past_max_time(self):
        started = time.monotonic()
        proc = self.agent("sleep", "30", max_time="1")
        self.assertEqual(proc.stdout, "timeout\n")
        self.assertLess(time.monotonic() - started, 20)

    def test_an_agent_that_ignores_term_is_not_ok(self):
        # The KILL escalation makes timeout exit 137, which is error:137 rather than timeout.
        # Either way it is not ok, so the slice fails.
        script = self.root / "stubborn.sh"
        script.write_text("#!/bin/bash\ntrap '' TERM\nsleep 30\n", encoding="utf-8")
        script.chmod(0o755)
        proc = self.agent(str(script), max_time="1")
        self.assertNotEqual(proc.stdout, "ok\n")
        self.assertEqual(proc.stdout, "error:137\n")

    def test_no_route_when_the_token_is_missing_and_the_agent_never_runs(self):
        calls = self.root / "calls.jsonl"
        self.bins.add("claude", log=calls)
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        proc = subprocess.run(
            ["bash", RUN_SH, "agent_run", "--max-time", "30", "--log", str(self.root / "a.log"),
             "--token-env", "CLAUDE_CODE_OAUTH_TOKEN", "--", "claude"],
            capture_output=True, text=True, check=False, timeout=60, env=env,
        )
        self.assertEqual(proc.stdout, "no_route\n")
        self.assertFalse(calls.exists())

    def test_the_agent_sees_only_its_own_token(self):
        # `env` is the agent: it prints the environment the agent would get into the log.
        proc = self.agent("env", token="CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(proc.stdout, "ok\n")
        seen = (self.root / "agent.log").read_text(encoding="utf-8")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=leak-claude_code_oauth_token", seen)
        for name, value in LEAKS.items():
            if name != "CLAUDE_CODE_OAUTH_TOKEN":
                with self.subTest(name=name):
                    self.assertNotIn(value, seen)
                    self.assertNotIn(name, _names(seen))

    def test_refuses_a_token_name_that_is_not_a_door(self):
        # Only the door secrets of route.py can be the agent's token. Any other name, even one the
        # caller has set, is refused before the agent runs.
        calls = self.root / "calls.jsonl"
        self.bins.add("claude", log=calls)
        for name in ("GITHUB_TOKEN", "GH_TOKEN", "EXTRA_SERVICE_TOKEN", "PATH", "claude_code_oauth_token"):
            with self.subTest(name=name):
                proc = self.agent("claude", token=name)
                self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertFalse(calls.exists())

    def test_kills_the_agents_leftover_process(self):
        pidfile = self.root / "agent.pid"
        proc = self.agent("bash", "-c", _leftover(pidfile))
        self.assertEqual(proc.stdout, "ok\n")
        self.assertFalse(_alive(self.leftover_pid("agent.pid")))

    def test_kills_the_agents_leftover_under_job_control(self):
        pidfile = self.root / "agent.pid"
        proc = self.run_under_job_control(
            "agent_run", "--max-time", "30", "--log", str(self.root / "agent.log"),
            "--token-env", "CLAUDE_CODE_OAUTH_TOKEN", "--", "bash", "-c", _leftover(pidfile),
        )
        self.assertEqual(proc.stdout, "ok\n")
        self.assertFalse(_alive(self.leftover_pid("agent.pid")))

    def test_a_socket_on_stdin_does_not_make_the_agent_read_bashrc(self):
        # R7, for the agent launch: the same setsid launch, so the same rule.
        home = self.root / "home"
        home.mkdir()
        marker = self.root / "bashrc-ran"
        (home / ".bashrc").write_text(f"touch {marker}\n", encoding="utf-8")
        self.bins.add("claude", stdout="done\n")
        env = {**self.env, "HOME": str(home)}
        proc = self.run_fn_with_socket_on_stdin(
            "agent_run", "--max-time", "30", "--log", str(self.root / "agent.log"),
            "--token-env", "CLAUDE_CODE_OAUTH_TOKEN", "--", "claude", env=env,
        )
        self.assertEqual(proc.stdout, "ok\n")
        self.assertFalse(marker.exists())

    def test_rejects_a_non_numeric_max_time(self):
        proc = self.agent("claude", max_time="soon")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")


class GateTest(RunTest):
    def gate(self, setup: str, check: str, timeout: str = "30"):
        return self.run_fn("run_gate", timeout, str(self.root / "gate.log"), setup, check)

    def test_green_when_setup_and_check_pass(self):
        proc = self.gate(f"touch {self.root}/setup", f"test -f {self.root}/setup")
        self.assertEqual(proc.stdout, "green\n")

    def test_red_when_the_check_fails(self):
        self.assertEqual(self.gate("", "exit 1").stdout, "red\n")

    def test_red_when_setup_fails_and_the_check_never_runs(self):
        mark = self.root / "check-ran"
        proc = self.gate("false", f"touch {mark}")
        self.assertEqual(proc.stdout, "red\n")
        self.assertFalse(mark.exists())

    def test_red_when_setup_exits_early_and_the_check_never_runs(self):
        mark = self.root / "check-ran"
        self.assertEqual(self.gate("exit 0", f"touch {mark}").stdout, "red\n")
        self.assertFalse(mark.exists())

    def test_red_when_setup_execs_and_the_check_never_runs(self):
        mark = self.root / "check-ran"
        self.assertEqual(self.gate("exec true", f"touch {mark}").stdout, "red\n")
        self.assertFalse(mark.exists())

    def test_red_when_setup_does_not_parse_and_the_check_never_runs(self):
        for setup in ('echo "', "cat <<EOF\nhi", "if true; then"):
            with self.subTest(setup=setup):
                mark = self.root / "check-ran"
                self.assertEqual(self.gate(setup, f"touch {mark}").stdout, "red\n")
                self.assertFalse(mark.exists())

    def test_red_when_the_check_does_not_parse(self):
        self.assertEqual(self.gate("", "echo )").stdout, "red\n")

    def test_none_when_the_check_only_comments_or_no_ops(self):
        mark = self.root / "setup-ran"
        for check in ("# nothing to run", ":", ": ; :", ": # note", " \n# note\n:\n"):
            with self.subTest(check=check):
                self.assertEqual(self.gate(f"touch {mark}", check).stdout, "none\n")
        self.assertFalse(mark.exists())

    def test_setup_state_reaches_the_check(self):
        check = f'test "$GATE_FLAG" = on && test "$PWD" = {self.root}'
        proc = self.gate(f"export GATE_FLAG=on; cd {self.root}", check)
        self.assertEqual(proc.stdout, "green\n")

    def test_the_checks_own_exit_status_decides(self):
        self.assertEqual(self.gate("", "exit 0").stdout, "green\n")
        self.assertEqual(self.gate("", "exit 2").stdout, "red\n")

    def test_red_when_the_check_times_out(self):
        started = time.monotonic()
        proc = self.gate("", "sleep 30", timeout="1")
        self.assertEqual(proc.stdout, "red\n")
        self.assertLess(time.monotonic() - started, 20)

    def test_none_when_the_check_is_empty_and_setup_does_not_run(self):
        mark = self.root / "setup-ran"
        self.assertEqual(self.gate(f"touch {mark}", "").stdout, "none\n")
        self.assertEqual(self.gate(f"touch {mark}", " \n\t").stdout, "none\n")
        self.assertFalse(mark.exists())

    def test_check_env_has_no_secret_and_no_actions_token(self):
        proc = self.gate("", "env")
        self.assertEqual(proc.stdout, "green\n")
        seen = (self.root / "gate.log").read_text(encoding="utf-8")
        self.assertNotIn("ACTIONS_", seen)
        for name, value in LEAKS.items():
            with self.subTest(name=name):
                self.assertNotIn(value, seen)
                self.assertNotIn(name, _names(seen))

    def test_keeps_the_names_a_job_needs(self):
        env = {
            **self.env,
            "CI": "true",
            "LANG": "C.UTF-8",
            "LC_TIME": "C",
            "TMPDIR": str(self.root),
            "TZ": "UTC",
        }
        proc = self.run_fn("run_gate", "30", str(self.root / "gate.log"), "", "env", env=env)
        self.assertEqual(proc.stdout, "green\n")
        seen = _names((self.root / "gate.log").read_text(encoding="utf-8"))
        for name in ("PATH", "HOME", "CI", "LANG", "LC_TIME", "TMPDIR", "TZ"):
            with self.subTest(name=name):
                self.assertIn(name, seen)

    def test_a_name_that_only_starts_with_lc_is_not_inherited(self):
        # R6: LC_* by prefix passed any name. Only the real locale categories may pass.
        env = {**self.env, "LC_ALL": "C", "LC_MYSECRET": "leak-lc-mysecret"}
        proc = self.run_fn("run_gate", "30", str(self.root / "gate.log"), "", "env", env=env)
        self.assertEqual(proc.stdout, "green\n")
        seen = (self.root / "gate.log").read_text(encoding="utf-8")
        self.assertIn("LC_ALL=C", seen)
        self.assertNotIn("leak-lc-mysecret", seen)
        self.assertNotIn("LC_MYSECRET", _names(seen))

    def test_every_real_locale_category_is_inherited(self):
        categories = (
            "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_NUMERIC", "LC_TIME",
            "LC_MONETARY", "LC_PAPER", "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE",
            "LC_MEASUREMENT", "LC_IDENTIFICATION",
        )
        env = {**self.env, **{name: "C" for name in categories}}
        proc = self.run_fn("run_gate", "30", str(self.root / "gate.log"), "", "env", env=env)
        self.assertEqual(proc.stdout, "green\n")
        seen = _names((self.root / "gate.log").read_text(encoding="utf-8"))
        for name in categories:
            with self.subTest(name=name):
                self.assertIn(name, seen)

    def test_a_socket_on_stdin_does_not_make_the_gate_read_bashrc(self):
        # R7: bash treats a shell whose stdin is a socket as remote and reads ~/.bashrc. The setsid
        # launch must not inherit stdin, so the marker must not appear.
        home = self.root / "home"
        home.mkdir()
        marker = self.root / "bashrc-ran"
        (home / ".bashrc").write_text(f"touch {marker}\n", encoding="utf-8")
        env = {**self.env, "HOME": str(home)}
        proc = self.run_fn_with_socket_on_stdin(
            "run_gate", "30", str(self.root / "gate.log"), "", "true", env=env
        )
        self.assertEqual(proc.stdout, "green\n")
        self.assertFalse(marker.exists())

    def test_kills_the_checks_leftover_process(self):
        pidfile = self.root / "gate.pid"
        self.assertEqual(self.gate("", _leftover(pidfile)).stdout, "green\n")
        self.assertFalse(_alive(self.leftover_pid("gate.pid")))

    def test_kills_the_leftover_process_when_the_check_times_out(self):
        pidfile = self.root / "gate.pid"
        proc = self.gate("", f"{_leftover(pidfile)}\nsleep 30", timeout="1")
        self.assertEqual(proc.stdout, "red\n")
        self.assertFalse(_alive(self.leftover_pid("gate.pid")))

    def test_kills_the_checks_leftover_under_job_control(self):
        pidfile = self.root / "gate.pid"
        proc = self.run_under_job_control(
            "run_gate", "30", str(self.root / "gate.log"), "", _leftover(pidfile)
        )
        self.assertEqual(proc.stdout, "green\n")
        self.assertFalse(_alive(self.leftover_pid("gate.pid")))


class RunGroupTest(RunTest):
    """_run_group and _group_alive, called directly with the job environment set."""

    def call(self, script: str):
        return subprocess.run(
            ["bash", "-c", f"source {shlex.quote(RUN_SH)}; _job_env ''; {script}"],
            capture_output=True, text=True, check=False, timeout=60, env=self.env,
        )

    def test_fails_loudly_and_runs_nothing_when_the_group_cannot_be_recorded(self):
        # The group is recorded before the command starts. Here the record cannot be written, so
        # the command must not run, and the failure must say so.
        mark = self.root / "ran"
        missing = shlex.quote(str(self.root / "no-such-dir"))
        log = shlex.quote(str(self.root / "group.log"))
        proc = self.call(f"_run_group {missing} {log} '' touch {mark}; echo rc=$?")
        self.assertIn("rc=1", proc.stdout)
        self.assertIn("never recorded", proc.stderr)
        self.assertFalse(mark.exists())

    def test_fails_loudly_when_a_group_survives_the_kill(self):
        # kill does nothing here, so the leftover outlives the group kill and must be reported.
        pidfile = self.root / "group.pid"
        leftover = shlex.quote(_leftover(pidfile))
        dir_ = shlex.quote(str(self.root))
        log = shlex.quote(str(self.root / "group.log"))
        proc = self.call(f"kill() {{ :; }}; _run_group {dir_} {log} '' bash -c {leftover}; echo rc=$?")
        self.assertIn("rc=1", proc.stdout)
        self.assertIn("survived", proc.stderr)
        self.assertTrue(_alive(self.leftover_pid("group.pid")))

    def test_group_alive_sees_a_live_group_and_not_a_dead_one(self):
        # Wait until setsid has made the sleep a group leader, or the group does not exist yet.
        proc = self.call(
            'setsid sleep 300 >/dev/null 2>&1 & p=$!; '
            'for _ in $(seq 200); do read -r -a f </proc/$p/stat && [[ ${f[4]} == "$p" ]] && break; '
            'sleep 0.02; done; '
            '_group_alive "$p" && echo alive; kill -KILL -- "-$p"; wait "$p"; '
            '_group_alive "$p" || echo gone'
        )
        self.assertEqual(proc.stdout, "alive\ngone\n")


class StripEnvTest(RunTest):
    def test_removes_every_door_secret_and_github_token(self):
        proc = self.run_fn("strip_env", "", "env")
        names = {line.split("=", 1)[0] for line in proc.stdout.splitlines()}
        for name in SECRETS:
            with self.subTest(name=name):
                self.assertNotIn(name, names)

    def test_removes_every_actions_name(self):
        stdout = self.run_fn("strip_env", "", "env").stdout
        names = {line.split("=", 1)[0] for line in stdout.splitlines()}
        self.assertFalse({n for n in names if n.startswith("ACTIONS_")})

    def test_keeps_only_the_named_variable(self):
        proc = self.run_fn("strip_env", "ANTHROPIC_API_KEY", "env")
        self.assertIn("ANTHROPIC_API_KEY=leak-anthropic_api_key", proc.stdout)
        self.assertNotIn("OPENAI_API_KEY=", proc.stdout)


class ParityTest(unittest.TestCase):
    def shell_array(self, array: str) -> set[str]:
        # run.sh keeps its own name lists. These checks fail when they drift from route.py.
        proc = subprocess.run(
            ["bash", "-c", f'source "{RUN_SH}" && printf "%s\\n" "${{{array}[@]}}"'],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return set(proc.stdout.split())

    def test_the_door_names_are_exactly_route_doors(self):
        expected = {name for names in route.DOOR_SECRETS.values() for name in names}
        self.assertEqual(self.shell_array("_DOOR_SECRETS"), expected)

    def test_the_locale_names_are_exactly_the_locale_categories(self):
        # POSIX locale categories plus the GNU extension LC_IDENTIFICATION; nothing else.
        expected = {
            "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_NUMERIC", "LC_TIME",
            "LC_MONETARY", "LC_PAPER", "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE",
            "LC_MEASUREMENT", "LC_IDENTIFICATION",
        }
        self.assertEqual(self.shell_array("_LOCALE_NAMES"), expected)

    def test_the_job_environment_names_no_secret(self):
        names = (
            self.shell_array("_JOB_ENV_NAMES")
            | self.shell_array("_JOB_ENV_EXTRA")
            | self.shell_array("_LOCALE_NAMES")
        )
        secrets = {name for names in route.DOOR_SECRETS.values() for name in names}
        secrets |= {"GH_TOKEN", "GITHUB_TOKEN"}
        self.assertFalse(names & secrets)
        self.assertFalse({name for name in names if name.startswith("ACTIONS_")})


if __name__ == "__main__":
    unittest.main()
