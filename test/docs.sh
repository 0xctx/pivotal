#!/usr/bin/env bash
# The commands the docs name must run, and the docs must still mark what is unmeasured (F9).
# CI runs this as its last step. The headless probe runs `claude --version`, `--help` and an unknown flag when claude is
# on PATH, and reports "not installed" and passes when it is not. No live model call is made.
set -euo pipefail

cd "$(dirname "$0")/.."
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
log="$out/step.log"

# step NAME COMMAND...: run one documented command with its output held back. A failing command prints that output and
# its name, and the run stops with exit 1, so a red run says which step broke.
step() {
  local name=$1
  shift
  if ! "$@" >"$log" 2>&1; then
    cat "$log" >&2
    echo "docs: FAILED at the step \"$name\": $*" >&2
    exit 1
  fi
}

step "headless probe" bash spikes/headless_probe.sh claude "$out"
step "kaggle probe --help" python3 spikes/kaggle_probe.py --help
step "kaggle/catalog.json is JSON" python3 -m json.tool kaggle/catalog.json
step "docs/engines.md marks UNMEASURED" grep -q 'UNMEASURED' docs/engines.md

# Every markdown file passes the docs line's checker: links, code fences and spelling.
step "doccheck of every Markdown file" python3 -I engine/doccheck.py .

# The piv CLI runs, and every subcommand the docs name answers --help. argparse exits before gh is built, so no GitHub
# call is made. test/test_docs.py holds this list to cli.build_parser(): a subcommand missing here is a red test.
step "gh piv --help" python3 gh-piv/gh-piv --help
subs=(init "do" kaggle land fix stop level now doctor remove preset event digest resume demo runner)
for sub in "${subs[@]}"; do
  step "gh piv $sub --help" python3 gh-piv/gh-piv "$sub" --help
done

echo "docs ok"
