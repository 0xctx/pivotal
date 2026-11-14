# Pivotal ⚡

**Label an issue; a model builds it in a fresh GitHub Actions machine, another company's model reviews it, your tests gate it, and you say "land it". No server, no database, no AI deciding.**

What runs is in [the live record](docs/engines.md#live-record). The status of each job is in the table below; nothing outside the first row has run on real GitHub.

| Status | What |
|---|---|
| **Proven live (run id, date)** | Run 43279525237 (2026-11-12, `0xctx/piv-sandbox-private`, preset `free-kaggle`): `trust`, `ack`, `plan`, `models-up` and `build` ran on real GitHub, Kaggle and ngrok; `pi:kaggle/qwen3.6-35b-a3b` produced a real patch (a README typo) and the gate was green. |
| **Built and tested with fakes, not run live** | `review`, `assemble`, the gate job, `publish` and `land`; the incident and event triggers; the self-hosted runner tiers; `piv demo`. Run 43279525237's review got a 503 from Google (`agent_no_output`) and run 43092707402's review failed behind an HTTP 502 from GitHub's log and artifact APIs. No time, GPU-hour or cost figure is measured. |
| **Not built** | the `models` subcommand of `piv`; the mirror scorer (the line is refused at plan); the land job's re-gate after a moved base; an instant no-Kaggle `piv demo` mode; a `LICENSE`; and the `v1.0.0` tag. |

## Install

```bash
gh extension install 0xctx/gh-piv
```

## Where it runs

Nothing runs on your computer. Labelling an issue starts GitHub Actions on GitHub's own machines; they build, review and gate the change and open a pull request. You do not clone anything to use it and you do not run a server.

```text
issue labelled
   ↓
GitHub runner builds
   ↓
another vendor's model reviews
   ↓
your tests gate
   ↓
PR ready for you
   ↓
you comment `/pivotal land`
```

## Three commands

```bash
piv init you/my-app --check 'npm test'
```
```bash
piv do you/my-app "add CSV export"
```
```bash
piv land you/my-app
```

The free path (Kaggle's GPUs and a free Gemini key, preset `free-kaggle`) is the on-ramp and the demo. Open models come first: omp, the default agent, reviews through free reviewer APIs. Claude is optional and paid: `claude-sub` uses your Claude plan, on repos you own. agy is not supported.

The real sandbox run and its pull request will be linked here after LIVE 0.1; the GIF after LIVE 3.1.

What is proven: [the live record](docs/engines.md#live-record).

## ⚡ What's going on?

```bash
piv now
```

```text
⚡ <N> agents working for you right now · <N> ready for you · <N> landed today (UTC)
```

`piv now` reads your repos and prints that line, plus how many repos it checked. Nothing else runs in the background.

## What you do once per repo

`piv init owner/repo --check 'your test command'` is the only command, run once from any machine with `gh` logged in (your laptop, a GitHub Codespace, a cloud shell). It works through the GitHub API and needs no clone of the target repo.

It sets:
- one workflow file: `.github/workflows/pivotal.yml` (pinned to a release of `0xctx/pivotal`)
- labels: `pivotal`, `pivotal:building`, `pivotal:ready`, `pivotal:landed`, `pivotal:blocked`, `pivotal:build`, `pivotal:incident`, `pivotal:mirror`, `pivotal:docs`
- the `pivotal` topic
- repo variables: `PIVOTAL_CHECK`, `PIVOTAL_SETUP`, `PIVOTAL_START`, `PIVOTAL_PRESET`, `PIVOTAL_LEVEL`, `PIVOTAL_PRIOR_CAN_APPROVE_PRS`
- secrets found in your environment: `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `PIVOTAL_KAGGLE_URL`, `PIVOTAL_KAGGLE_KEY`, `KAGGLE_USERNAME`, `KAGGLE_KEY`
- allows Actions to open pull requests

### Manual setup from the browser

If you prefer not to run `piv init`, do the same in the web UI:
1. Settings → Actions → General → Workflow permissions → allow "GitHub Actions can create and approve pull requests"
2. Settings → Secrets and variables → Actions → Variables tab → New repository variable for each variable above
3. Settings → Secrets and variables → Actions → Secrets tab → New repository secret for each secret your preset needs
4. Repository main page → gear next to About → Topics → add `pivotal`
5. Issues tab → Labels → New label → create the nine labels listed above
6. Create `.github/workflows/pivotal.yml` with the caller file from a release of `0xctx/pivotal` (the `templates/caller.yml` at that tag, with `__PIVOTAL_ENGINE__` replaced by `0xctx/pivotal` and the pin placeholder replaced by the release commit SHA)

GitHub's menu names change from time to time; if one is missing, search the repo settings for the item's name. Step 6 cannot be done entirely in the browser without copying the rendered caller; the caller must be pinned to a release SHA.

## Day to day, in the browser or the GitHub app

Open an issue and add the `pivotal` label; read the report comment and the pull request; comment `/pivotal land` (or merge the ready PR). `piv do` and `piv land` are shortcuts for the same two actions.

## Three steps

1. **Accounts, once.** An OpenRouter key (for the `one-key` preset, the default) or, optionally, a Claude token (for the paid `claude-sub`) and `gh` logged in. If you want free GPU for the build tier, add a Kaggle endpoint later with `piv kaggle setup you/my-app` (see `piv kaggle rotate` which sets the new key on your repos with the `pivotal` topic; use `--owner ORG` for an organisation's repos). The Quickstart makes a demo repo and runs without Kaggle.
2. **Set up a repo, once.**
   ```bash
   piv init you/my-app --check 'npm test'
   ```
   `--check` is the command that gates every change. No `--preset` is needed: `piv init` defaults to the shipped `one-key` preset (one OpenRouter key, cheap, not free, not run live).
3. **Label an issue**, or file one from a terminal: `piv do you/my-app "add CSV export"`. Pivotal builds it, another vendor reviews it, your check gates it, and at the default level it waits for "land it": `piv land you/my-app`.

The step-by-step version, with a demo repo, is [docs/QUICKSTART.md](docs/QUICKSTART.md).

## The ladder

How much Pivotal does without you is one repo variable, `PIVOTAL_LEVEL`, set with `piv level`.

| Level | Name | What starts work | Who lands |
|---|---|---|---|
| `watch` | — | nothing is built; signals show in `piv digest` | — |
| `ask` | in the loop | a label (decided by the issue's author) or a writer's `/pivotal go` comment | you: "land it" |
| `prepare` | **near the loop (default)** | a label or a writer's comment, plus a red CI run on main and a `piv event` (both built, not yet run on real GitHub) | you: "land it" on a ready, reviewed, green change |
| `land` | out of the loop | as `prepare` | the pipeline, under the gate; on a protected branch the push is refused and the pipeline opens a ready stack pull request; `land it` (`piv land`) merges it through the API, pinned to the sealed commit |

`piv stop` cancels the runs of a repo, and `piv stop --all` sets every repo to `watch` until `piv resume`.

## Triggers

Each trigger becomes a GitHub issue, so every run has the same record, pipeline and gate.

| Trigger | How it starts |
|---|---|
| A labelled issue | the `pivotal` label, or `pivotal:build`, `pivotal:incident`, `pivotal:mirror` or `pivotal:docs`; any other line label never triggers |
| A comment | `/pivotal go`, `/pivotal fix`, `/pivotal land`, `/pivotal stop`, from someone with write access |
| Red CI on main | a failed run of the workflow named `CI` for a push to the default branch files an `incident` issue, or updates the open one for the same workflow, with a title and a link, never the failure text. A push by a bot or a read-only user starts nothing and tells nobody. A repo whose CI workflow has another name gets none. Built, not yet run on real GitHub. |
| A webhook or log alert | `piv event you/my-app --kind incident --title … --link …` files one event issue with the title and link only. `--kind` is required but informational: every dispatch runs as an event. Built, not yet run on real GitHub. |
| Another agent or a script | `piv do` |

A comment from anyone without write access does nothing. A label is decided by the issue's author, not by whoever applied
it, so anyone who can apply labels (triage or write) can start a run on an issue whose author has write access. A labelled or commented closed issue also starts a run, and closing an issue does not stop a labelled run: use `piv stop` or `piv level`. At the land level, such a label can end in a landed change with no further command,
if the gate and the review pass. An issue whose author lacks write access runs only on the untrusted preset. Every such run is passed the free keys and the
Kaggle endpoint, but they are used only once you set `PIVOTAL_UNTRUSTED_PRESET` (`piv init` does not set it); without it,
a stranger's issue goes red at plan. Such a run never gets your subscription tokens. It spends your free-key quota and
Kaggle endpoint time, and Pivotal does not cap that.

## Lines

A line is a recipe: a brief, a gate, and optionally a score and a preset.

| Line | Recipe | Status |
|---|---|---|
| `build` (default) | the standard brief: implement the request, with tests, then stop | defined in `lines/build.json`; not run live |
| `incident` | reproduce, find the root cause, make the minimal fix with a regression test | defined in `lines/incident.json`; not run live |
| `docs` | prose changes; the gate checks links, code fences, a short list of misspellings and doubled words (not full spelling) | defined in `lines/docs.json`; not run live |
| `mirror` | a hill-climb toward an attached mockup | refused at plan (`line_not_wired`) until its scorer's inputs are supplied |

## Presets

A preset is the model choice, in one word: `--preset free-kaggle`. A preset is an ordered fallback per tier (build and
review). Every skipped entry is named in the run report, as `fell_back`, so a fallback is never silent. One request is one slice:
the plan job runs it, checks it against the slice rules and hands it on.

Open models come first. The default agent is omp; pi runs the Kaggle-served build. agy is not supported (its headless call
was unreliable on a hosted runner), and Claude is optional and paid.

| Preset | You need | Builds | Reviews |
|---|---|---|---|
| `free-kaggle` (the free path) | the Kaggle endpoint, a Groq key and a Gemini key; the last reviewer also needs an OpenRouter key | a Kaggle-served Qwen model, through pi | gpt-oss-120b on Groq, then Gemini (Google), then Gemini through OpenRouter; all through omp |
| `one-key` (the default) | an OpenRouter key | deepseek via OpenRouter, through omp | Gemini (Google) via OpenRouter, through omp |
| `claude-sub` (optional, uses your Claude plan) | a Claude token (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`), and a Gemini key or an OpenRouter key | Claude Sonnet, then Opus | Gemini (Google), then Gemini through OpenRouter; all through omp |

The reviewer must be a different vendor from the builder. `piv preset check NAME` probes each entry with a single one-word
call to its model, so it spends a little quota. A custom preset goes in the repo variable `PIVOTAL_PRESET_JSON`.

## Safety, in plain words

These rules are in the code and have fake-agent tests. None of them has run on real GitHub, Kaggle or ngrok yet.

- **Strangers cannot reach your subscription tokens.** Trust is decided before any secret is passed. A failed or unclear
  permission lookup counts as untrusted. An untrusted run is passed only the free keys (`GEMINI_API_KEY`, `GROQ_API_KEY`) and the
  Kaggle endpoint (`PIVOTAL_KAGGLE_URL`, `PIVOTAL_KAGGLE_KEY`), and they are used only once you set `PIVOTAL_UNTRUSTED_PRESET`.
- **The model never holds a GitHub write token.** Every job that runs model output or your code has read-only permission.
- **Secrets stay narrow.** The model's token is set only on the model's own command. Your check runs with every secret removed.
- **Nothing lands by itself without the gate and the level.** The change must pass your check on the exact commit, and
  a reviewer from another vendor must answer READY. Below `land`, you must also say "land it".
- **Unclear means not landed.** A missing or malformed plan, verdict or result leaves the change unlanded: at most a draft
  pull request, and sometimes none.
- **Stop means stop.** A cancelled run never publishes.
- **Limits.** A run has at most six slices, and at most one fix round by default.
- **Kaggle serves inference only**, for your own use, behind a key. The session exits after 20 idle minutes and at 11
  hours, and nothing restarts it on a timer. Kaggle's terms apply.

What the rules don't cover: the model can change any file your check allows, including the tests. A weak check lets weak
code through, and a review is a second model's opinion, not a proof. A landed change is a normal commit, so you can revert it.

## Costs

- **Idle is free.** Nothing runs until there is work. A Kaggle session that a run started can stay up after the run and
  use GPU quota; [docs/SECURITY.md](docs/SECURITY.md#kaggle) gives its limits.
- **GitHub Actions:** public repos' runs are free. Private repos' runs use your Actions minutes. Your own runner
  runs only plan, build and gate, on your machine. The other jobs stay on
  GitHub-hosted runners and use Actions minutes on a private repo.
- **Kaggle:** free to use, under Kaggle's weekly GPU limit. The plan records about 30 GPU-hours a week; check Kaggle's
  current limit.
- **Model keys:** a Gemini free key, or your Claude plan's limits. You should set a hard credit limit at each provider.
- **Measured:** **Not measured yet.** Minutes per run, GPU-hours per run, cost per run, and the results of the overnight
  run on two repos are not measured. The operator's overnight run fills them in. Until then, no number in these docs is measured.

## More

- [docs/QUICKSTART.md](docs/QUICKSTART.md): your first 15 minutes, from accounts to landing.
- [docs/EXAMPLES.md](docs/EXAMPLES.md): recipes and demos you can run today.
- [docs/SECURITY.md](docs/SECURITY.md): what runs where, who can do what, the Kaggle rules, and how to report a problem.
- [docs/FAQ.md](docs/FAQ.md): costs, vendor terms, and whether it is safe to let a change land.
- [docs/engines.md](docs/engines.md): what has been measured, and what is UNMEASURED.
- [CHANGELOG.md](CHANGELOG.md): what has landed, and what has not.

## Develop

See [AGENTS.md](AGENTS.md) for the commands that gate every change.