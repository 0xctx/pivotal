# Quickstart: your first 15 minutes

You will give a demo repo a README with a typo, file one task against it, watch Pivotal fix it, and land the fix.

The proven line, in one table; [docs/engines.md](engines.md#live-record) is the source of truth.

| Status | What |
|---|---|
| **Proven live (run id, date)** | Run 43279525237 (2026-11-12, `0xctx/piv-sandbox-private`, preset `free-kaggle`): `trust`, `ack`, `plan`, `models-up` and `build` ran on real GitHub, Kaggle and ngrok; `pi:kaggle/qwen3.6-35b-a3b` produced a real patch (a README typo) and the gate was green. |
| **Built and tested with fakes, not run live** | `review`, `assemble`, the gate job, `publish` and `land`; the incident and event triggers; the self-hosted runner tiers; `piv demo`. Run 43279525237's review got a 503 from Google (`agent_no_output`) and run 43092707402's review failed behind an HTTP 502 from GitHub's log and artifact APIs. No time, GPU-hour or cost figure is measured. |
| **Not built** | the `models` subcommand of `piv`; the mirror scorer (the line is refused at plan); the land job's re-gate after a moved base; an instant no-Kaggle `piv demo` mode; a `LICENSE`; and the `v1.0.0` tag. |
>
> Every command is checked against the code and its fake-agent tests, and the repo's `test/test_docs.py` parses each one in a bash block or a code span.
> The times are targets, not measurements: **Not measured yet**, the first fresh reader's run fills them in.
> The target is that a fresh reader can finish this Quickstart in 15 minutes or less: a target, not a measurement.

## What you need

- **GitHub:** a repo with Actions enabled. Private repos use your Actions minutes; public repos are free.
- **One provider API key** set as a secret for the preset you choose:
  - `one-key` (default): `OPENROUTER_API_KEY`, one key that reaches models from different vendors, through omp (the default agent).
  - `claude-sub` (optional, paid, uses your Claude plan): `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`.
  - `free-kaggle` (open models first, the free path): a Kaggle endpoint, `GROQ_API_KEY` and `GEMINI_API_KEY`; see [engines.md](engines.md).
- Nothing else. agy is not supported, and Claude is never needed for the default preset.

The Kaggle free-GPU path is an optional upgrade needing your own Kaggle account and an ngrok domain via `piv kaggle setup`.

Put this in your shell for the commands below. `piv init` reads it from your environment, sends it to GitHub, and never prints it.

```bash
export OPENROUTER_API_KEY=...                    # your OpenRouter key (for the one-key preset)
```

## 1. Install the extension

```bash
gh extension install 0xctx/gh-piv
gh piv --help
```

The commands below say `piv`. `gh piv` runs the same thing, and `piv` on its own needs a shell alias: `alias piv='gh piv'` in your profile.

Nobody clones the engine: `piv init` pins the caller to a release of `0xctx/pivotal`.

### Where it runs

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

## 2. Create a demo repo with a typo

```bash
gh repo create you/my-app --private --clone
cd my-app
printf '# my-app\n\nA small demo app. Its readme has a teh typo.\n' > README.md
git add README.md
git commit -m "Add a demo readme"
git push -u origin HEAD
```

A private repo's runs use your GitHub Actions minutes. A public repo's runs are free, but strangers can file issues on it,
so keep the demo private. You do not clone the engine: `piv init` pins the caller to a release of `0xctx/pivotal`.

## 3. (Optional) Kaggle for free GPU

The `one-key` preset runs entirely on GitHub-hosted runners with no GPU. If you want free GPU for the build tier,
add a Kaggle endpoint later with `piv kaggle setup you/my-app` (see `piv kaggle setup --help`). This step is not required
for the Quickstart.

## 4. Set the repo up for Pivotal

```bash
piv init you/my-app --check 'test -s README.md'
piv doctor you/my-app
```

- `--check` is the command that gates each change. It is required, and `test -s README.md` (the README is not empty)
  is enough for this demo. A real repo uses its test command, such as `npm test`.
- No `--preset` is needed: `piv init` defaults to the shipped `one-key` preset (one OpenRouter key, cheap, not free,
  not run live).
- `piv init` adds the labels and the `pivotal` topic, sets the variables and the secrets it finds in your environment,
  lets Actions open pull requests, and writes `.github/workflows/pivotal.yml`. It does not run the check; the first run does.
- `piv doctor` checks the result. Fix every line marked as a problem, each has a fix line under it.

If `piv init` stops with "has no release yet", Pivotal has not published its first release. Pass `--release` with a
commit SHA of `0xctx/pivotal` to pin one yourself, or wait for the release: this step cannot finish without one.

### What you do once per repo

`piv init owner/repo --check 'your test command'` is the only command, run once from any machine with `gh` logged in (your laptop, a GitHub Codespace, a cloud shell). It works through the GitHub API and needs no clone of the target repo.

It sets:
- one workflow file: `.github/workflows/pivotal.yml` (pinned to a release of `0xctx/pivotal`)
- labels: `pivotal`, `pivotal:building`, `pivotal:ready`, `pivotal:landed`, `pivotal:blocked`, `pivotal:build`, `pivotal:incident`, `pivotal:mirror`, `pivotal:docs`
- the `pivotal` topic
- repo variables: `PIVOTAL_CHECK`, `PIVOTAL_SETUP`, `PIVOTAL_START`, `PIVOTAL_PRESET`, `PIVOTAL_LEVEL`, `PIVOTAL_PRIOR_CAN_APPROVE_PRS`
- secrets found in your environment: `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `PIVOTAL_KAGGLE_URL`, `PIVOTAL_KAGGLE_KEY`, `KAGGLE_USERNAME`, `KAGGLE_KEY`
- allows Actions to open pull requests

### Day to day, in the browser or the GitHub app

Open an issue and add the `pivotal` label; read the report comment and the pull request; comment `/pivotal land` (or merge the ready PR). `piv do` and `piv land` are shortcuts for the same two actions.

### Alerts

GitHub notifies you of the report comment as it does for any comment on your issue; for a phone push set the repo variable `PIVOTAL_NTFY` to a topic name and subscribe to it in the ntfy app (implemented and tested with a fake, not run live).

## 5. File the task

```bash
piv do you/my-app "fix the README typo"
```

The output looks like this (the link is different each time):

```text
piv do you/my-app: line build, trigger label pivotal
issue created: https://github.com/you/my-app/issues/<n>
label pivotal added: the run starts when ack comments
waiting for ack's run link
run link: https://github.com/you/my-app/actions/runs/<id>
```

`piv do` files a new issue, adds the `pivotal` label, which starts the run, and waits for the run's first comment.

## 6. Watch the run

```bash
piv now
```

It prints the headline, with the counts filled in:

```text
⚡ <N> agents working for you right now · <N> ready for you · <N> landed today (UTC)
```

Open the run link. Its first comment says `run #N started (one-key, trusted)`. The run then goes through its stages:

1. **plan**: it runs your check on the base and turns the task into one slice, checked against the slice rules.
2. **build**: the OpenRouter-served deepseek model changes the repo. Then your check runs on the change.
3. **review**: a different vendor, google (Gemini via OpenRouter), reads the change and answers READY or NEEDS FIXES.
4. **assemble**, **gate** and **publish**: the change is checked again on a fresh machine for that exact commit, and a
   pull request is opened.

The run ends in one of three states, and the report comment says which one and why:

- **ready for you**: a pull request that is green and reviewed. Go to step 7.
- **draft**: a pull request kept back from landing, with the reason the report gives, such as "open the draft PR and read
  the review verdict before deciding". To try again, `piv fix you/my-app '#1'` starts a new run from the issue's own text,
  not from the draft pull request.
- **failed**: the reason is in the comment. Read `piv doctor you/my-app` for the setup.

A missing or malformed plan or result opens no pull request, and that change is not landed.

## 7. Land it

```bash
piv land you/my-app
```

This comments `/pivotal land` on the one ready issue (add `'#1'` to name it). Pivotal lands only the commit it sealed. If the
base branch has moved since then, Pivotal does not push: nothing lands, the issue stays ready, and the land job goes red with a
comment that says why. The re-gate is not wired yet, so re-run the issue with `/pivotal go trusted` (a comment on the issue),
which starts a new run from the issue's text, and land that run's pull request. When the base contains the sealed commit, the
issue gets a "landed" comment and closes. Open `README.md` on the default branch: the typo should now read `the`, not `teh`.

## Clean up

```bash
piv remove you/my-app --yes
```

This undoes `piv init` for the repo: the workflow file, the labels, the topic, the variables, and the secrets init set. Those
secrets can include your provider keys and your Kaggle credentials, which the repo may have held before; `piv remove` cannot
tell them from the repo's own, so it warns about them before it removes them. It does not delete the repo. Delete that with `gh repo delete you/my-app --yes` once you are done with it.

## When something is wrong

- `piv doctor you/my-app` names each problem and its fix.
- `piv stop you/my-app` cancels the runs in flight.
- `piv level you/my-app watch` stops new runs from starting. `piv level you/my-app prepare` puts it back.
