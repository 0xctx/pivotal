# Security: what runs where, and who can do what

This page says what Pivotal lets each party do, which job holds which GitHub permission and which secret, and how
to report a problem. Every rule here is in the code. The tests use fake agents (`test/fakecli.py`), a fake `gh` and a
fake `curl`, never a real provider, so a passing test shows what the code does with fakes. None of them has run on
real GitHub, Kaggle or ngrok yet.

> See the proven table in [README.md](../README.md).
>
> The facts below come from
> `.github/workflows/`, `templates/caller.yml`, `engine/` and `kaggle/`, and the repo's tests check the table
> against those files.

## The short version

- Pivotal runs inside your repo's GitHub Actions. It runs no server or daemon of its own. The Kaggle endpoint is an HTTP
  server behind an ngrok domain, and if you use your own runner, that runner is a service on your machine. Pivotal has
  no database: GitHub holds the state.
- A job that runs the model's output or your repo's code never holds a GitHub write token.
- A stranger's issue never receives your subscription tokens. Trust is decided before any secret is passed.
- Nothing lands unless the gate passes on the exact commit, a reviewer from another vendor answers READY, and your
  level allows it. Below the `land` level, you must also say "land it".

## The permission diagram

The caller is one file in your repo, `.github/workflows/pivotal.yml`, copied from `templates/caller.yml`. It
receives every trigger, and `trust` decides who the trigger is from before any job gets a secret.

```text
 label, comment, red CI on main, piv event, piv do
        │
        ▼
 caller: trust        contents, issues, pull-requests: read · no secrets · decides only
        │
        ├─ trusted go, fix, event, incident ──► factory-trusted   all Pivotal secrets
        ├─ a stranger's go ───────────────────► factory-untrusted  the 4 free secrets only
        ├─ trusted land (comment) ────────────► land              write, no secrets
        └─ trusted stop ──────────────────────► stop              actions write, no secrets

 factory.yml, one fresh GitHub-hosted runner per job unless you use your own runner (the engine is checked out at a pinned release from the public engine repo; the run's token cannot read a private one):

   ack ─► plan ─► models-up ─► build ─► review ─► assemble ─► gate ─► publish ─► report
   issues:w  read    read        read     read       read       read     WRITE     issues:w
                     (Kaggle     (keys)   (keys)     (no        (no      contents, PRs,
                      account)                        token)     token)  checks: write
```

A job that runs model output or repo code is `contents: read` with `persist-credentials: false`. Only `publish`
and the land job write to the repo's code and branches, and both run git and `gh` on data the earlier jobs produced
and that the engine has checked. `ack` opens or updates the incident issue and writes comments and labels; `report` writes comments and labels. The tables below are the same
facts in full.

### The caller (`templates/caller.yml`)

| Job | Permissions | Secrets | GitHub token in a step | Runs repo code or model output? |
|---|---|---|---|---|
| `trust` | `contents: read`, `issues: read`, `pull-requests: read` | none | a read token, in the one step that runs `engine/trust.py` | no |
| `factory-trusted` | the ceiling `checks: write`, `contents: write`, `issues: write`, `pull-requests: write`; `.github/workflows/factory.yml` narrows it per job | all eleven Pivotal secrets: `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `KAGGLE_KEY`, `KAGGLE_USERNAME`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `PIVOTAL_KAGGLE_KEY`, `PIVOTAL_KAGGLE_URL`, `XAI_API_KEY` | no token in this job | no: it calls `.github/workflows/factory.yml` |
| `factory-untrusted` | the ceiling `checks: write`, `contents: write`, `issues: write`, `pull-requests: write`, narrowed the same way | four free secrets only: `GEMINI_API_KEY`, `GROQ_API_KEY`, `PIVOTAL_KAGGLE_KEY`, `PIVOTAL_KAGGLE_URL` | no token in this job | no: it calls `.github/workflows/factory.yml` |
| `land` | `actions: read`, `checks: read`, `contents: write`, `issues: write`, `pull-requests: write` | none | no token in this job | no: it calls `.github/workflows/land.yml` |
| `stop` | `actions: write`, `pull-requests: read` | none | a token, in the one step that cancels runs | no |

### The reusable workflows (`.github/workflows/factory.yml`, `.github/workflows/land.yml`)

| Job | Permissions | Secrets | GitHub token in a step | Runs repo code or model output? |
|---|---|---|---|---|
| `ack` | `issues: write` | none | a write token, in the one step that runs `engine/ack.py` | no: only the engine and `gh` run |
| `plan` | `contents: read`, `issues: read` | none | a read token, in the decide step (engine code); the baseline step has none | yes: the baseline step runs the repo's check, with a stripped environment |
| `models-up` | `contents: read` | `KAGGLE_KEY`, `KAGGLE_USERNAME`, `PIVOTAL_KAGGLE_KEY`, `PIVOTAL_KAGGLE_URL` | no token in this job | no: the engine and the `kaggle` CLI run |
| `build` | `contents: read` | `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `PIVOTAL_KAGGLE_KEY`, `PIVOTAL_KAGGLE_URL`, `XAI_API_KEY` | no token in this job | yes: the agent and the repo's setup and check run here, with secrets stripped from their environment |
| `review` | `contents: read` | `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `PIVOTAL_KAGGLE_KEY`, `PIVOTAL_KAGGLE_URL`, `XAI_API_KEY` | no token in this job | yes: a model runs here, and its output is read as data |
| `assemble` | `contents: read` | none | no token in any step | no: it runs no repo code |
| `gate` | `contents: read` | none | no token in any step | yes: the repo's check runs here, with secrets stripped |
| `publish` | `contents: write`, `pull-requests: write`, `checks: write` | none | a write token: the one job that writes to the repo's code and branches | no: only git and `gh` run, on validated artifacts, and the repo is not checked out |
| `report` | `issues: write` | none | a write token, in the one step that comments | no: only the engine, `gh` and `curl` run |
| `land` | `actions: read`, `checks: read`, `contents: write`, `issues: write`, `pull-requests: write` | none | a token, in the one step that runs `engine/land.py` | no: the repo is not checked out |

The rule that matters: every job that runs model output or repo code is read-only and holds no GitHub write token.
`build` and `review` hold model keys, so they get no GitHub token at all. `models-up` holds the full Kaggle account
key, so it gets no GitHub token either. `plan` holds only a read token, in the step that runs the engine.

## Who can do what

| Who | Can | Cannot |
|---|---|---|
| Anyone who can open an issue | open an issue; nothing starts | start a trusted run, reach a subscription token or the Kaggle account key, land, or move a branch. Once a label or a writer's `/pivotal go` starts an untrusted run and `PIVOTAL_UNTRUSTED_PRESET` is set, that run's model holds only the one free key of its route's door, one agent call at a time |
| Someone who can apply labels (triage or write) | apply the `pivotal` label to an issue, open or closed. The run is decided by the issue's **author**, not by the labeller. At the land level, a label on an issue whose author has write access can end in a landed change with no further command, if the gate and the review pass | make a run trusted when the author has no write access: that run gets only the untrusted preset's keys |
| Someone without write access | nothing: a comment from them is ignored | anything else, including `/pivotal go` |
| A writer (admin, maintain or write) | `/pivotal go`, `/pivotal fix`, `/pivotal land`, `/pivotal stop`; `/pivotal go trusted` on a stranger's issue; set `piv level`; set variables and secrets | land a change that has no seal (the land job checks the seal) |
| A pull request from a fork | nothing: a fork PR's own events start nothing; a comment on one is decided like any comment | land refuses a fork PR |

Three rules decide the trusted flag, and all three are in `engine/trust.py`:

- **A label** is decided by the issue's author's permission. Anyone who can apply labels can start a run on an issue
  whose author has write access. A labelled or commented closed issue also starts a run, and closing an issue does not
  stop a labelled run: use `piv stop` or `piv level`. At the land level, a label applied by a triager to an issue whose author has
  write access can end in a landed change with no further command, if the gate and the review pass.
- **A comment** comes from a writer, or it is ignored. A `stop` is trusted on the commenter alone. Any other
  command, such as `go`, is decided by the issue's author, unless the first line is `/pivotal go trusted`.
- **`/pivotal go trusted`** lets a writer approve a stranger's issue. The approval covers the issue's title and body
  exactly as they were when the writer commented: `plan` compares a SHA-256 of them with the live issue and refuses
  a run whose text has changed since. Only a writer can give that approval, and an edited issue needs a new one.

A stranger's issue, once a triager applies the label, runs only on the untrusted preset, and only if you set
`PIVOTAL_UNTRUSTED_PRESET`. A writer's plain `/pivotal go` on a stranger's issue gives an untrusted run too: only
`/pivotal go trusted` makes it trusted. Without `PIVOTAL_UNTRUSTED_PRESET` the run goes red at plan. Every such run is
passed the four free secrets (`GEMINI_API_KEY`, `GROQ_API_KEY`, `PIVOTAL_KAGGLE_KEY`, `PIVOTAL_KAGGLE_URL`), and its
model holds its own free key in its environment. Pivotal does not block network access, so the issue's text could steer
that model to send a key elsewhere. Such a run never gets your subscription tokens or your Kaggle account key. It
spends your free-key quota and your Kaggle endpoint time, and Pivotal does not cap that: set a hard limit at each
provider.

## What an issue's text can do

The issue text is the brief the model builds from. Pivotal treats that text as data, not as instructions to the
pipeline: it reaches scripts only through `env:` and files, never inside a `${{ }}` expression in a `run:` step.

The model still has a real capability. It can change any file the check allows, including the tests. A weak check
lets weak code through, and a reviewer from another vendor is a second opinion, not a proof. A model that follows
instructions hidden in an issue can therefore write a bad change. Below the land level, the change is not landed on its own: it is a draft
or a ready pull request that needs the gate and your level, and a draft says why it is a draft. At the land level a
green change with a READY review lands with no further command (see Landing); below it someone must say "land it".

A stranger's run can still push draft branches named `pivotal/issue-<n>/...` and open draft pull requests, because
`publish` runs for every run that gets that far. It never moves the base, never seals a commit, and never lands.

## Kaggle

Pivotal uses Kaggle for one thing: serving one open model for inference, to your own runs, behind a key.

- **Inference only.** The notebook (`kaggle/serve.ipynb.tmpl`) runs one `llama-server`. Nothing else runs on Kaggle:
  no agents, no git and no tests. Kaggle's acceptable-use terms limit use to personal machine-learning work; whether
  this use fits Kaggle's terms is for you to check.
- **Keyed.** `llama-server` runs with `--api-key-file`, and the endpoint's key is the one value the runs present.
  **The check that five named routes refuse a keyless request is off by default.** It is switched on by the repo
  variable `vars.PIVOTAL_KEYLESS_CHECK`, which models-up reads on every run: `1` turns it on, `0` or unset leaves it off,
  and any other value refuses the run. Those routes are `/v1/models`, `/props`, `/slots`, `/metrics` and `/v1/chat/completions`. Until that check has run on a live session, `kaggle_ok=true` means only that a
  server answered `/v1/models` with the key, and any server holding the key would pass.
- **Self-stopping.** The session exits after 20 idle minutes, and at 11 hours, whichever comes first. It also exits at
  once if its static domain is already online somewhere else, which is the lock that stops two sessions on one
  domain. Nothing restarts it on a timer, and a session is never chained to stay up.
- **Personal.** The session runs under your Kaggle account and your ngrok domain. The ngrok token and the endpoint key
  sit in a private Kaggle dataset, `<you>/pivotal-serve-secrets`, which `piv kaggle setup` creates. The command cannot
  check that the dataset is private, so confirm it in Kaggle. Kaggle's own terms apply to you.

Two limits belong here too. The `models-up` job holds your full Kaggle account key, `KAGGLE_KEY`, on the one step that
needs it, so a repo that sets that secret trusts Pivotal with the whole account. And `models-up` reuses a live session
whichever run started it, so one repo's run can use another repo's session. That sharing is the design.

## Secrets

- **Where they live.** Repo secrets. `piv init` sets the ones it finds in your environment, and `piv remove` takes them
  back off. Nothing is printed.
- **Who gets them.** Only the trusted caller job passes every secret. The untrusted job passes the four free ones.
  A job gets only the names listed in the table above.
- **Inside a job.** The model's own command gets its own key and nothing else. The setup and check commands run with
  every secret and every `ACTIONS_*` token removed, in their own process group, which is killed after the step.
- **Before anything is published.** The build step scans its patch for every secret value it held, in raw, hex and
  base64 forms. A match fails the slice, and the patch is discarded. Raw agent logs are never uploaded. Artifacts hold
  only the patch and a redacted summary.
- **Limits of the scan.** It does not find a value written in another encoding, such as a `\x` escape, base32 or
  reversed text, and it does not protect a value shorter than 12 bytes. These limits are recorded in
  `engine/build.py`.

## Landing

- **The base moves by a plain fast-forward** to the gated commit, the last commit of the landable prefix, or on a
  protected branch by an API merge (below). Each
  commit up to it passed the gate for its exact SHA, on a fresh GitHub-hosted runner, or on your own runner, which is
  not fresh. **A push is never forced.** On a protected branch the push is refused, and `piv land` merges the pull
  request through the API instead, pinned to the sealed commit. The merge commit is not the sealed commit, so the land
  job checks that its tree is the gated commit's tree before it says "landed".
- **The seal** is a check run named `pivotal/gate` on the commit. The land job trusts it only if the check run is from
  the `github-actions` app, the run it names is this repo's `.github/workflows/pivotal.yml` on the default branch, the
  check run sits in that run's check suite, and the run's artifacts confirm the commit. Under the check-suite link, which is not verified on real GitHub, another
  workflow's check of the same name may pass (engine/seal.py:26-31). What this does not prove: every workflow of this repo with `checks: write` is the
  `github-actions` app, so the app name alone does not show who made the seal, and the check-suite link is not verified
  on real GitHub. A dedicated GitHub App is an open decision.
- **A moved base is not re-gated.** If the base has moved since the seal, the land job goes red, pushes nothing, and
  says why. Re-run the issue with `/pivotal go trusted` to get a new run from the issue's text.
- **On a protected branch,** the push is refused, and the pipeline opens a ready stack pull request. `piv land` then
  merges it through the API, pinned to the sealed commit.
- **Stop means stop.** A run that `/pivotal stop` or `piv stop` cancelled never publishes and never lands.

## Limits you should know

- The live run is proven through `build` only: run 43279525237 (2026-11-12) ran `trust`, `ack`, `plan`, `models-up` and
  `build` on real GitHub, Kaggle and ngrok, and `review`, `assemble`, the gate job, `publish` and `land` have not run.
- The keyless check for the Kaggle endpoint is off by default (see Kaggle above).
- A writer's `/pivotal go trusted` gives a run every secret of the trusted job. Read the issue before you approve it.
- With `PIVOTAL_RUNNER_PUBLIC=trusted-only` on your own runner, every trusted run goes to that runner, not only the
  issues a writer approved with `/pivotal go trusted`, so their issue text reaches your machine. The runner holds your
  subscription CLI logins in `pv-runner`'s home and runs plan, build and gate on your machine.
- The mirror line is refused at plan until its scorer's inputs are supplied. If it is enabled, its scorer runs as the
  same user as the build, which runs the agent, so the model could forge the score. This is not closed.
- A red `main` pushed by a bot or a read-only user starts nothing and tells nobody.
- A landed change is a normal commit, and you can revert it.

## Reporting a problem

Pivotal has no published security contact yet. The repository is not released, so the usual
channels do not exist. Until it is public:

- Do not post exploit details in a public issue.
- Tell the person who runs your copy of Pivotal, directly.

Once the repository is public, use GitHub's private vulnerability reporting, which the repository's owner turns on in
its settings. This repository does not configure it for you. Say what you saw, which job and file it concerns, and
how to reproduce it with the fake-agent tests in `test/`.

**If a key may have leaked,** rotate it at its provider first. For the Kaggle endpoint key, `piv kaggle rotate` replaces
it in the private dataset and in the secrets of your repos with the `pivotal` topic. `piv remove REPO --yes` deletes the secrets `piv init` set on that
repo, the provider keys and the Kaggle credentials included, after a warning that some may be the repo's own.

## See also

- [docs/FAQ.md](FAQ.md): costs, vendor terms, and whether it is safe to let a change land.
- The invariants F1 to F9 that these rules come from.
