# Examples: recipes and demos you can run

See the proven table in [README.md](../README.md). Each example is built and tested with fakes; none has run on real GitHub. Only the commands and one sentence per item are kept.

One request is one slice: the plan job turns the request into one slice, checked against the slice rules, and hands it on.

The title is shown on the issue, up to its first 60 characters. The run does not read it: a link run gets a fixed title and the run link, never the alert's text.

---

## Recipes

### 1. Turn an alert into `piv event`

A receiver script calls `piv event` with a title and a link to a run of your repo.

```bash
repo="you/my-app"
title="Alert: ${ALERT_TITLE:0:100}"
run=$(gh run list --repo "$repo" --limit 1 --json url --jq '.[0].url')
gh piv event "$repo" --kind incident --title "$title" --link "$run"
```

At level `prepare` or `land` a run starts; at `ask` the event is quiet.

### 2. Keep several repos green

Init each repo with its check, then read all state from one place.

```bash
piv init you/app-one --check 'npm test' --preset free-kaggle
piv init you/app-two --check 'python3 -m pytest' --preset free-kaggle
piv doctor --all
piv now --owner you
piv digest --owner you --since 24
```

`piv digest` lists red mains, ready PRs and watchdog flags (15 minutes); `--json` emits JSON for a script.

### 3. A performance hill-climb

File an issue with a score command and target; each round runs the command after the check and stops when the score reaches the target.

```bash
piv do you/my-app "make the search endpoint faster" --score 'python3 bench.py' --target 0.9
```

The score command prints one number 0–1 (higher is better); up to five fix rounds run automatically.

### 4. Your own preset

Put a preset JSON in `PIVOTAL_PRESET_JSON` and select it by name.

```json
{"name":"my-mix","summary":"Qwen builds, Claude reviews","kaggle":{"model":"qwen3.6-35b-a3b"},"tiers":{"build":["pi:kaggle/qwen3.6-35b-a3b"],"review":["claude:anthropic/sonnet"]}}
```

```bash
PIVOTAL_PRESET_JSON="$(cat my-mix.json)" python3 -m engine.route --check
gh variable set PIVOTAL_PRESET_JSON --repo you/my-app < my-mix.json
gh piv preset you/my-app my-mix
```

The review tier must use a different vendor from the builder; `piv preset check` refuses an unmeasured worker.

### 5. Your own runner for subscription CLIs

Register a self-hosted runner that runs plan, build and gate on your machine.

```bash
gh piv runner add --org your-org --jobs 2 --dry-run
sudo --preserve-env=GH_TOKEN,HOME gh piv runner add --org your-org --jobs 2 --yes
gh variable set PIVOTAL_RUNNER --repo you/my-app --body self-hosted
gh variable set PIVOTAL_RUNNER_PUBLIC --repo you/my-app --body trusted-only
sudo -iu pv-runner
claude auth login
codex login
agy
grok
```

Only trusted work on private repos (or public with `trusted-only`) reaches your machine; the CLIs log in as `pv-runner`. A Kaggle session that a run started can stay up after the run and use GPU quota for 20 idle minutes or 11 hours.

---

## Demos

### 1. A free Kaggle build and review

A small change built by the Kaggle-served Qwen model, reviewed by Gemini, gated by your check.

```bash
piv init you/my-app --check 'npm test' --preset free-kaggle
piv do you/my-app "fix the README typo"
piv now
```

The build runs on `pi:kaggle/qwen3.6-35b-a3b`; the review falls back through Groq, then Gemini, then Gemini through OpenRouter, all through omp. A ready run's report says **ready for you**; then `piv land you/my-app` lands it.

### 2. A red CI fixed before you wake up

A failed `CI` workflow run on the default branch files an incident and builds a fix.

```bash
piv init you/my-app --check 'npm test' --preset free-kaggle
# push a breaking change to main
piv doctor you/my-app
piv digest --repo you/my-app
```

The report says `draft` with reason `baseline_red`; you read the draft and decide. The ack job logs `quiet: the level is ask, which starts no incident` and the report job logs `quiet: ack found the level starts no run of this kind`.

### 3. An incident webhook

Your monitoring calls `piv event` with a title and a link to a run of this repo.

```bash
piv event you/my-app --kind incident --title "Uptime check failed: checkout" --link https://github.com/you/my-app/actions/runs/123
```

An issue titled `Event: Uptime check failed: checkout` is filed and a run starts (at `prepare` or `land`). The event recipe logs `quiet: the level is ask, which starts no event`.

### 4. A backlog with `claude-sub` (optional, uses your Claude plan)

Several issues, each its own run, built by Claude Sonnet and reviewed by Gemini through omp. This preset is paid: it uses your
Claude plan, so it is not the `piv init` default.

```bash
piv init you/my-app --check 'npm test' --preset claude-sub
piv do you/my-app "add CSV export to the orders page"
piv do you/my-app "stop the nightly job from logging secrets"
piv digest --repo you/my-app
piv land you/my-app --all
```

Each `piv do` files its own issue and run; `piv land you/my-app --all` lands every ready issue.

### 5. Stop everything

Cancel all runs of a repo, or all repos you manage, and set them to `watch`.

```bash
piv stop you/my-app
piv stop --all
piv resume
```

`piv stop --all` records each repo's old level in `PIVOTAL_PRIOR_LEVEL`; `piv resume` restores them.