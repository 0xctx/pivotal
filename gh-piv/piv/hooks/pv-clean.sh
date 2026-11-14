#!/usr/bin/env bash
# Between-jobs clean-up for one pv-runner instance. `piv runner add` installs
# this file twice, as job-started.sh and job-completed.sh, for ACTIONS_RUNNER_HOOK_JOB_STARTED and
# ACTIONS_RUNNER_HOOK_JOB_COMPLETED. It kills what the last job left behind in this instance's
# service, then empties this instance's work directory of workspaces. It touches nothing else. The
# runner's own _temp, _actions, _tool and _PipelineMapping stay in both modes.
set -euo pipefail

# The three settings below are what `piv runner add` installs. Tests render the file with others.
expected_user="pv-runner"
procs_file=""
mode="completed"

fail() {
    echo "pv-runner hook: $*" >&2
    exit 1
}

[ "$(id -un)" = "$expected_user" ] || fail "runs only as $expected_user, not $(id -un)"

# The work directory: this instance's, named by its unit. Anything else is refused.
work="${PV_RUNNER_WORK:-}"
[ -n "$work" ] || fail "PV_RUNNER_WORK is empty"
case "$work" in
    /*) ;;
    *) fail "PV_RUNNER_WORK is not an absolute path: $work" ;;
esac
[ ! -L "$work" ] || fail "the work directory is a symlink: $work"
[ -d "$work" ] || fail "the work directory does not exist: $work"
work="$(realpath -e -- "$work")"
home="$(realpath -m -- "${HOME:-/nonexistent}")"
[ "$work" != "/" ] || fail "refusing to clean /"
[ "$work" != "$home" ] || fail "refusing to clean the home directory $home"
[[ "$work" =~ ^/.+/runner-[0-9]+/_work$ ]] || fail "not a runner work directory (.../runner-N/_work): $work"
runner_dir="${work%/_work}"

# What may be killed: the processes of this instance's own systemd service (its cgroup), so a job
# on another instance is never touched. A process that daemonized is still in the cgroup.
if [ -n "$procs_file" ]; then
    list="$procs_file"
else
    cgroup="$(sed -n 's/^0:://p' /proc/self/cgroup)"
    [[ "$cgroup" =~ /pv-runner@[0-9]+\.service$ ]] || fail "not running in a pv-runner service (cgroup '$cgroup')"
    list="/sys/fs/cgroup$cgroup/cgroup.procs"
fi
[ -r "$list" ] || fail "cannot read the process list $list"

# Protected: this script and everything above it (the job's worker, and the hook's caller), and the
# runner itself (its launcher and listener, found by their path in this instance's directory).
protected=" "
pid=$$
while [ "$pid" -gt 1 ]; do
    protected="$protected$pid "
    ppid="$(awk '{ sub(/^.*\) /, ""); print $2 }' "/proc/$pid/stat" 2>/dev/null)" || break
    [ -n "$ppid" ] || break
    pid="$ppid"
done

alive() {
    local line state
    { read -r line < "/proc/$1/stat"; } 2>/dev/null || return 1
    state="${line##*) }"
    [ "${state%% *}" != "Z" ]
}

# Rescan, do not snapshot: a supervisor that traps TERM can start a new child before it dies. Each
# round re-reads the list and drops what must not be killed: PID 1, this script and its ancestors,
# processes of other owners, and the runner's own launcher and listener.
victims=()
scan() {
    local members pid cmdline
    victims=()
    mapfile -t members < "$list"
    for pid in "${members[@]}"; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        [ "$pid" -gt 1 ] || continue
        case "$protected" in *" $pid "*) continue ;; esac
        [ -O "/proc/$pid" ] || continue
        alive "$pid" || continue
        cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || continue
        case "$cmdline" in
            *"$runner_dir/run.sh"* | *"$runner_dir/bin/Runner."*) continue ;;
        esac
        victims+=("$pid")
    done
}

# Round 1 asks (TERM) and waits up to two seconds; later rounds KILL. Twelve rounds at most.
for round in 1 2 3 4 5 6 7 8 9 10 11 12; do
    scan
    [ "${#victims[@]}" -gt 0 ] || break
    if [ "$round" = 1 ]; then
        for pid in "${victims[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
        for _ in $(seq 20); do
            waiting=0
            for pid in "${victims[@]}"; do alive "$pid" && waiting=1; done
            [ "$waiting" = 1 ] || break
            sleep 0.1
        done
    else
        for pid in "${victims[@]}"; do kill -KILL "$pid" 2>/dev/null || true; done
        sleep 0.1
    fi
done
scan
[ "${#victims[@]}" -eq 0 ] || fail "job-$mode: ${#victims[@]} process(es) survived SIGKILL: ${victims[*]}"

# Read-only module caches and the like need write permission before they can go. chmod -R and find
# do not follow symlinks. Only workspaces go: the runner's own _temp, _actions, _tool and
# _PipelineMapping stay, so a repo called _foo is still wiped (and the Worker finishing a job keeps
# its files).
chmod -R u+rwX -- "$work" 2>/dev/null || true
find "$work" -mindepth 1 -maxdepth 1 ! -name _temp ! -name _actions ! -name _tool ! -name _PipelineMapping \
    -exec rm -rf --one-file-system -- {} +
