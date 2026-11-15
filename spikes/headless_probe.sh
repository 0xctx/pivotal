#!/usr/bin/env bash
# Probe one agent CLI without credentials: its version, the --help text
# (where the headless flags are named) and the exit code and error text of
# an unknown flag. Live headless calls and rate-limit texts need the
# operator's keys and run only in the GitHub-hosted spike job.
#
# Usage: spikes/headless_probe.sh <cli> <outdir>
set -u

cli=${1:?usage: headless_probe.sh <cli> <outdir>}
out=${2:?usage: headless_probe.sh <cli> <outdir>}
mkdir -p "$out" || exit 2

if ! bin=$(command -v "$cli"); then
  echo "$cli: not installed"
  exit 0
fi

# probe <label> <args...>: run with no stdin, a 30 s cap, and record the exit code.
probe() {
  local label=$1
  shift
  timeout 30 "$cli" "$@" </dev/null >"$out/$cli.$label.txt" 2>&1
  local rc=$?
  echo "$cli $label exit=$rc bytes=$(wc -c <"$out/$cli.$label.txt")"
}

echo "$cli binary=$bin"
probe version --version
probe help --help
probe unknown_flag --no-such-flag-spike
