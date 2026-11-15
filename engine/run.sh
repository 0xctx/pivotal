#!/usr/bin/env bash
# agent_run (the model token only), run_gate (job environment, setsid, timeout) and strip_env (F3).
# Source this file, or run it as `bash engine/run.sh <function> ...`.
# Each function prints one result word and returns 0. Callers branch on the word: anything but
# `ok` or `green` is not landable.

# The door secrets of route.py, flattened. `--token-env` accepts only these. The parity test checks
# this list against route.DOOR_SECRETS.
_DOOR_SECRETS=(
  CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY OPENAI_API_KEY XAI_API_KEY GEMINI_API_KEY
  OPENROUTER_API_KEY GROQ_API_KEY PIVOTAL_KAGGLE_KEY
)

# A job's commands inherit only these variables. Every other variable is unset, so a secret that no
# list names cannot leak. _JOB_ENV_EXTRA is the declared extra set: names a job needs that are not
# secrets. _LOCALE_NAMES are the locale categories, named one by one: a prefix would pass LC_MYSECRET.
_JOB_ENV_NAMES=(PATH HOME LANG CI TMPDIR)
_JOB_ENV_EXTRA=(USER LOGNAME TERM TZ)
_LOCALE_NAMES=(
  LC_ALL LC_CTYPE LC_COLLATE LC_MESSAGES LC_NUMERIC LC_TIME LC_MONETARY LC_PAPER LC_NAME
  LC_ADDRESS LC_TELEPHONE LC_MEASUREMENT LC_IDENTIFICATION
)

# Seconds before a TERM-ignoring command gets KILL.
_KILL_AFTER=5

# The error:N result when a command's process group was never recorded or survived the kill. 70 is
# EX_SOFTWARE. The message on stderr says which failure it was.
_LOST_GROUP_RC=70

_usage() {
  echo "run.sh: $*" >&2
}

# _is_door NAME: true when NAME is one of the door secrets.
_is_door() {
  local door
  for door in "${_DOOR_SECRETS[@]}"; do
    [[ "$door" == "$1" ]] && return 0
  done
  return 1
}

# _job_env KEEP: sets _JOB_ENV to `env -i NAME=VALUE ...` with the passthrough names that are set,
# and KEEP too when it is named and set. Every other variable is dropped.
_job_env() {
  local keep="$1" name
  local -a assign=()
  for name in "${_JOB_ENV_NAMES[@]}" "${_JOB_ENV_EXTRA[@]}" "${_LOCALE_NAMES[@]}"; do
    [[ -n "${!name+x}" ]] && assign+=("$name=${!name}")
  done
  if [[ -n "$keep" && -n "${!keep+x}" ]]; then
    assign+=("$keep=${!keep}")
  fi
  _JOB_ENV=(env -i "${assign[@]}")
}

# strip_env KEEP CMD...: runs CMD with only the job's environment (see _job_env), plus the variable
# named KEEP (may be "").
strip_env() {
  local keep="$1"
  shift
  _job_env "$keep"
  "${_JOB_ENV[@]}" "$@"
}

# _group_alive GID: true while a process that is not a zombie is in process group GID. A zombie is
# already dead, and may linger after the kill until its parent reaps it.
_group_alive() {
  local stat fields state pgrp
  for stat in /proc/[0-9]*/stat; do
    read -r fields 2>/dev/null <"$stat" || continue
    fields="${fields##*) }"
    read -r state _ pgrp _ <<<"$fields"
    if [[ "$pgrp" == "$1" && "$state" != Z ]]; then
      return 0
    fi
  done
  return 1
}

# _run_group DIR LOG ERRLOG CMD...: runs CMD in its own session with the job environment (see
# _job_env), stdout to LOG and stderr to ERRLOG (use "" or the same path as LOG to merge them),
# then kills the session's process group. DIR holds the group record. Sets _RUN_RC to CMD's exit
# status. Returns 1, with a message on stderr, when the group was never recorded (CMD did not run)
# or a member survived the kill. Then the caller must not report ok or green.
_run_group() {
  local dir="$1" log="$2" errlog="$3" gid pid i
  shift 3
  _RUN_RC=0
  # The wrapper writes its own pid before it execs CMD. Under setsid that pid is the group id,
  # whether or not setsid forked. $! is not: under job control, setsid forks and $! is the parent.
  # stdin is /dev/null: a socket on stdin makes bash read ~/.bashrc, even from an empty environment.
  # shellcheck disable=SC2016 # $$ and $1 belong to the wrapper bash, not to this shell.
  if [[ -z "$errlog" || "$errlog" == "$log" ]]; then
    "${_JOB_ENV[@]}" setsid --wait bash -c 'echo "$$" >"$1" && shift && exec "$@"' run \
      "$dir/group" "$@" </dev/null >"$log" 2>&1 &
  else
    "${_JOB_ENV[@]}" setsid --wait bash -c 'echo "$$" >"$1" && shift && exec "$@"' run \
      "$dir/group" "$@" </dev/null >"$log" 2>"$errlog" &
  fi
  pid=$!
  wait "$pid" || _RUN_RC=$?
  gid=$(cat "$dir/group" 2>/dev/null)
  if [[ ! "$gid" =~ ^[1-9][0-9]*$ ]]; then
    echo "run.sh: the process group was never recorded, so the command did not run" >&2
    return 1
  fi
  kill -KILL -- "-$gid" 2>/dev/null
  for ((i = 0; i < 20; i++)); do
    _group_alive "$gid" || return 0
    sleep 0.1
  done
  echo "run.sh: process group $gid survived SIGKILL" >&2
  return 1
}

# agent_run --max-time SECONDS --log FILE [--errlog FILE] --token-env DOOR [--agent-dir DIR] -- CMD...
# Runs CMD with the job environment plus the door secret DOOR, whose value comes from the caller's
# environment. stdout goes to FILE. When --errlog is given, stderr goes to that file; otherwise
# stderr is merged into FILE. Prints ok, timeout (exit 124), error:N, or no_route when the token is
# empty, in which case CMD never runs. The agent also gets DISABLE_AUTOUPDATER=1, so a CLI cannot
# replace the version the job pinned; PI_SKIP_VERSION_CHECK=1 and PI_TELEMETRY=0, so pi makes no
# version request and sends no telemetry; with --agent-dir it gets PI_CODING_AGENT_DIR=DIR (pi's
# config directory, a fresh one per call, made by the engine). None of these is set for any other command.
agent_run() {
  local max="" log="" errlog="" token_env="" agent_dir="" have_dir=0
  while (($#)); do
    case "$1" in
      --max-time) max="${2:-}"; shift 2 ;;
      --log) log="${2:-}"; shift 2 ;;
      --errlog) errlog="${2:-}"; shift 2 ;;
      --token-env) token_env="${2:-}"; shift 2 ;;
      --agent-dir) agent_dir="${2:-}"; have_dir=1; shift 2 ;;
      --) shift; break ;;
      *) _usage "agent_run: unknown argument $1"; return 2 ;;
    esac
  done
  if [[ ! "$max" =~ ^[1-9][0-9]*$ || -z "$log" ]] || ! _is_door "$token_env" || (($# == 0)); then
    _usage "agent_run --max-time SECONDS --log FILE [--errlog FILE] --token-env DOOR [--agent-dir DIR] -- CMD..."
    return 2
  fi
  if ((have_dir)) && [[ ! "$agent_dir" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    _usage "agent_run: --agent-dir must be an absolute path of plain characters"
    return 2
  fi
  if [[ -z "${!token_env:-}" ]]; then
    echo no_route
    return 0
  fi
  local dir grouped=0 umask_was
  _job_env "$token_env"
  _JOB_ENV+=("DISABLE_AUTOUPDATER=1" "PI_SKIP_VERSION_CHECK=1" "PI_TELEMETRY=0")
  if ((have_dir)); then
    _JOB_ENV+=("PI_CODING_AGENT_DIR=$agent_dir")
  fi
  # The log holds agent output, so it is created 0600 whatever the caller's umask.
  umask_was=$(umask)
  umask 077
  if dir=$(mktemp -d); then
    _run_group "$dir" "$log" "$errlog" timeout --kill-after="$_KILL_AFTER" "$max" "$@" && grouped=1
    rm -rf "$dir"
  fi
  umask "$umask_was"
  if ((grouped == 0)); then
    echo "error:$_LOST_GROUP_RC"
    return 0
  fi
  case "$_RUN_RC" in
    0) echo ok ;;
    124) echo timeout ;;
    *) echo "error:$_RUN_RC" ;;
  esac
}

# _blank_check TEXT: true when TEXT runs no command: only blanks, separators, `:` and comments.
_blank_check() {
  local rest
  rest=$(sed -E 's/(^|[[:space:];&])#.*$//' <<<"$1") || return 1
  rest="${rest//[[:space:];&:]/}"
  [[ -z "$rest" ]]
}

# _parses LOG NAME TEXT: true when TEXT parses with no diagnostic. The diagnostic, if any, goes to
# LOG under NAME. bash -n exits 0 on an open heredoc and only warns, so a warning counts as failure.
_parses() {
  local diag status=0
  diag=$(bash -n -c "$3" 2>&1) || status=$?
  if ((status != 0)) || [[ -n "$diag" ]]; then
    echo "run.sh: $2 does not parse: $diag" >>"$1"
    return 1
  fi
  return 0
}

# run_gate TIMEOUT LOG SETUP CHECK
# Runs SETUP, then CHECK, with only the job's environment (see _job_env), in its own session. Output
# goes to LOG, and the session's process group is killed afterwards. Prints green (CHECK ran to the
# end and exited 0), red (any other outcome, including a timeout, a SETUP that exits or execs before
# CHECK, and a SETUP or CHECK that does not parse) or none (CHECK runs no command, so SETUP does not
# run either).
run_gate() {
  if (($# != 4)) || [[ ! "$1" =~ ^[1-9][0-9]*$ || -z "$2" ]]; then
    _usage "run_gate TIMEOUT LOG SETUP CHECK"
    return 2
  fi
  local max="$1" log="$2" setup="$3" check="$4" dir marker q script
  if _blank_check "$check"; then
    echo none
    return 0
  fi
  if ! _parses "$log" SETUP "$setup" || ! _parses "$log" CHECK "$check"; then
    echo red
    return 0
  fi
  if ! dir=$(mktemp -d); then
    echo red
    return 0
  fi
  marker="$dir/checked"
  printf -v q '%q' "$marker"
  # SETUP runs in this shell, so an exit or exec in it ends the script before the marker. CHECK
  # runs in a subshell, so its own exit status decides, and the marker proves it reached the end.
  script="$setup"$'\n(\n'"$check"$'\n)\n'"printf '' >$q"
  local grouped=0
  _job_env ""
  _run_group "$dir" "$log" "" timeout --kill-after="$_KILL_AFTER" "$max" bash -o pipefail -ec "$script" &&
    grouped=1
  if ((grouped && _RUN_RC == 0)) && [[ -e "$marker" ]]; then
    echo green
  else
    echo red
  fi
  rm -rf "$dir"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-}" in
    agent_run | run_gate | strip_env) "$@" ;;
    *)
      _usage "usage: run.sh {agent_run|run_gate|strip_env} ..."
      exit 2
      ;;
  esac
fi
