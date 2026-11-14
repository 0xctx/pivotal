# FAQ

Short answers to the questions people ask first. The rules themselves are in [docs/SECURITY.md](SECURITY.md).

> See the proven table in [README.md](../README.md).
>
> Every rule is checked against the code
> and its fake-agent tests. No minute, GPU-hour, cost or time in these docs is measured.

## Costs

**Does it cost anything when nothing is running?**
Between runs, nothing runs. After a run, a Kaggle session can stay up for a while and use Kaggle's GPU quota. Its exit
rules are in the Kaggle section below.

**What do runs cost on GitHub?**
- A **public** repo's runs use GitHub-hosted runners for free.
- A **private** repo's runs use your Actions minutes, from your plan's monthly quota. Overage is billed per minute.
  GitHub sets both, so check its current pricing for your plan.
- Your own runner runs only plan, build and gate, on your machine. Ack, models-up, review,
  assemble, publish, report, trust, stop and land stay on GitHub-hosted runners and use Actions minutes on a private
  repo. A quiet run keeps plan hosted too.
- How many minutes one run takes: **Not measured yet: the operator's live run fills this in.**

**What does Kaggle cost?**
Kaggle is free to use, under its weekly GPU limit. The plan records about 30 GPU-hours a week. Check Kaggle's current
limit, because Kaggle sets it. How many GPU-hours one run takes: **Not measured yet: the operator's live run fills this
in.**

**What about the model keys?**
- A Gemini key is a free tier for the shipped presets. The `free-kaggle` preset tries a Groq model first (`GROQ_API_KEY`), then Gemini, and its last reviewer is Gemini through OpenRouter, which bills per token.
- A Claude token uses your Claude plan's limits. Codex, Grok and OpenRouter keys bill their own usage.
- Pivotal does not meter spend across providers, so set a hard credit limit at each provider.
- The cost per run: **Not measured yet: the operator's live run fills this in.**

**Is any dollar figure in these docs?**
No. Prices change, so the docs point you to each provider's current pricing.

## Vendor terms and subscription tokens

**Can I use my Claude or ChatGPT subscription with Pivotal?**
You can use your own token from your own plan, on your own repos. The `claude` worker takes `CLAUDE_CODE_OAUTH_TOKEN`,
which `claude setup-token` makes. The plan records that Anthropic's GitHub Actions page
([code.claude.com/docs/en/github-actions](https://code.claude.com/docs/en/github-actions)) describes setup-token for
personal use. This build has not confirmed it; whether your plan allows it is for you to check. OpenAI recommends API
keys for automation ([learn.chatgpt.com/docs/auth](https://learn.chatgpt.com/docs/auth)).

**What does a vendor's terms allow?**
Pivotal does not decide that. Each vendor sets its own terms for automated use. This build has not re-checked those
terms, so read them before you put a subscription token in Actions.

**Does Pivotal share, pool or resell a subscription?**
No. Your token is used only in your repo's jobs. Only the agent that needs it holds it. Pivotal does not block network
access, so a steered model could send it elsewhere; the scan covers the patch only and not every encoding. A stranger's
issue never gets it:
an untrusted run is passed only the four free secrets. A writer's `/pivotal go trusted` does give that one run the
trusted job's secrets, so approve only issues you have read.

**Can I use a Gemini key from the free tier?**
Yes. The shipped `free-kaggle` preset reviews with Gemini, and the Quickstart says where to get the key. Read the
provider's own terms for the free tier.

## Safety

**Is it safe to let a change land?**
A change lands only when all of these hold:
- your check passes on the exact commit, on a fresh GitHub-hosted runner, or on your own runner, which is not fresh.
  The check command is `PIVOTAL_CHECK`, and at `prepare` and `land` the base must also be green (the baseline);
- a reviewer from a different vendor answers READY on the patch. The commit that lands is that patch re-applied on the
  stack, so the review binds the patch's sha256 and the files it changes, not that commit;
- the land job re-checks the seal: a `pivotal/gate` check run that `publish` creates, and only when the run's artifacts
  confirm the commit. The land job checks that the check run is from the `github-actions` app, that its run is this
  repo's `.github/workflows/pivotal.yml` on the default branch, that the check run sits in that run's check suite, and
  that the artifacts confirm the commit;
- your level allows the landing. At the default level, `prepare`, you must still say "land it".

The four levels, and what each one lands:
- `watch`: nothing starts, so nothing lands.
- `ask`: work starts only on a `go` or `fix` that you ask for. You land it yourself, with `/pivotal land` or `piv land`.
- `prepare` (the default): as `ask`, and red CI and events start work too. You land it with one "land it" on a ready,
  green change.
- `land`: as `prepare`. The pipeline lands the change under the gate, and that needs a reviewer from another vendor in
  the preset. On a protected branch the push is refused and the pipeline opens a ready stack pull request, which "land
  it" merges through the API.

A level whose requirement is not met is held at the level below, and the run says why. An incident's fix is always a
draft, never a ready pull request: with a red base it is a draft with the reason `baseline_red`, and with a green base
it is a draft with the reason `already_green`.

What this does not prove: the model can change the tests, a weak check lets weak code through, and a review is a
second model's opinion. A landed change is a normal commit, so `git revert` undoes it.

> See the proven table in [README.md](../README.md).

These parts of the landing are not yet checked
on a live run:
- that the real API reports the check run's app slug, its run's workflow path, the check suite id and the run's check
  suite id in the forms the code reads;
- that a check run that a workflow job creates sits in that run's check suite. The land job does not establish who
  created a seal, and only the publish job holds `checks: write` by design. A dedicated GitHub App is an open decision;
- that `gh run download --name <n> --dir <d>` extracts one artifact directly into `<d>`, and that `--paginate --slurp`
  and `total_count` behave as the code reads them;
- how branch protection and a merge queue answer a push and the merge API, and that the merge endpoint accepts
  `merge_method=merge` on the repo;
- that a fast-forward push to the base behaves as the code expects on a real remote.

**Can a stranger make Pivotal do work in my repo?**
A stranger can open an issue, and nothing starts. A stranger alone cannot start work. A label from someone with triage
or write access starts a run, and the issue's author decides whether it is trusted. A writer's comment, a red CI run on
main that a writer pushed, and a `piv event` from a writer also start work. A stranger's issue runs only on the
untrusted preset, and only once you set `PIVOTAL_UNTRUSTED_PRESET`. Such a run can produce draft pull requests, and it
does not land by itself. It can land only after a writer approves it with `/pivotal go trusted`, and then it follows
your level like any trusted run. A writer can also mark a draft ready and merge it by hand in GitHub, which skips the gate. Such a run also spends your free-key quota and Kaggle endpoint time, and Pivotal does
not cap that. At the land level, a label applied by a triager to an issue whose author has write access can end in a
landed change with no further command, if the gate and the review pass. The full table is in
[docs/SECURITY.md](SECURITY.md).

**Can a stranger read my secrets?**
Not your subscription tokens and not your Kaggle account key. A stranger's issue, once a triager labels it or a writer
comments `/pivotal go`, and once you set `PIVOTAL_UNTRUSTED_PRESET`, runs with four free secrets: `GEMINI_API_KEY`,
`GROQ_API_KEY`, `PIVOTAL_KAGGLE_KEY` and `PIVOTAL_KAGGLE_URL`. The model in that run holds its own key in its
environment. Pivotal does not block network access and scans only the patch, so the issue's text could steer the model
to send a key elsewhere. Use free keys with a hard limit, or leave `PIVOTAL_UNTRUSTED_PRESET` unset. A failed or unclear
permission lookup counts as untrusted.

**What if something goes wrong while a run is going?**
- `piv stop you/my-app` cancels the runs in flight. A cancelled run never publishes.
- `piv level you/my-app watch` stops new runs from starting. `piv level you/my-app prepare` puts it back.
- `piv stop --all` sets every repo to `watch`, and `piv resume` undoes that.

**Where is the state kept?**
On GitHub: issues, labels, pull requests, check runs, repo variables and secrets, and run artifacts with a
seven-day retention. On Kaggle: the private dataset `<you>/pivotal-serve-secrets`, which holds the ngrok token and the
endpoint key, and the session. Nothing is kept on your machine unless you use your own runner: then the subscription CLIs keep their logins in
`pv-runner`'s home. `piv` holds no state of its own.

## Kaggle

**What does Kaggle run for Pivotal?**
One open model, behind a key, for inference only. The notebook runs one `llama-server`. Nothing else runs on Kaggle:
no agents, no git and no tests.

**How long does a Kaggle session run?**
After a run, it exits after 20 idle minutes, or at 11 hours, whichever comes first. A session that is not healthy within
15 minutes of loading is a failed start. It also exits at once if its domain is
already online in another session. Nothing restarts it on a timer.

**What are Kaggle's limits for this?**
- The weekly GPU limit (about 30 GPU-hours a week, per the plan; check Kaggle's current figure). A refused push skips
  the Kaggle entries in that run, and the run report says so. The next run tries the push again, so a run after
  Kaggle's weekly reset can use Kaggle.
- Kaggle's queues: a busy time can delay a start.
- The time from a start to a healthy endpoint is **Not measured yet: the operator's live run fills this in.** Pivotal's
  own live run is not measured. [docs/engines.md](engines.md) records a time to healthy taken on 2026-11-01 with sysop's
  serve module, which is a different measurement.
- Kaggle's acceptable-use terms limit use to personal machine-learning work. Whether this use fits them is for you to
  check. Pivotal's own use is one account, one model, inference only.

**Is the Kaggle endpoint protected?**
Its protection is the key, and the check that the other routes refuse a keyless request is off by default. See
[docs/SECURITY.md](SECURITY.md#kaggle).

## Using it

**Do I need a server?**
No server of Pivotal's own. The pipeline is GitHub Actions in your repo, and `piv` is a GitHub CLI extension that holds
no state. Two things can be a server or a service of yours: the Kaggle endpoint, an HTTP server behind an ngrok domain,
and your own runner, if you use one.

**Can I run it on my own machine?**
Yes, with your own runner. With it, plan, build and gate run on your machine, and only build's
subscription CLIs run there; review is GitHub-hosted. The other jobs stay on GitHub-hosted runners. Before you use it on a public repo, read
the "Where a job runs" section below, including its "One exception" paragraph.

**Where a job runs**
Decided in `.github/workflows/factory.yml`, not by a variable alone. Self-hosted is used only when all of these hold:

- `PIVOTAL_RUNNER=self-hosted`;
- the run is trusted;
- the repo is private, or `PIVOTAL_RUNNER_PUBLIC=trusted-only` is set.

Untrusted work on a public repo always runs on GitHub-hosted VMs, so a stranger's issue never reaches
a machine that holds your logins. The choice is made in the `ack` job (`engine/where.py`), which always runs hosted;
the `plan`, `build` and `gate` jobs take it as their `runs-on` (a quiet run, which starts nothing, keeps `plan` hosted).
`models-up`, `review`, `assemble`, `publish` and `report` are GitHub-hosted.

One exception to "a stranger's issue never reaches your machine": a writer's `/pivotal go trusted` comment makes that
issue trusted (`engine/trust.py`), so with `PIVOTAL_RUNNER_PUBLIC=trusted-only` its text reaches your runner. Say it
only for an issue you have read.

**Is there a demo?**
`piv demo` exists. The GIF for the README is not recorded: [docs/demo.tape](demo.tape) is the script that will record
it once `piv demo` has an instant no-Kaggle mode, which is not built, so a recording now is not possible.

**Which parts are built, and which are not?**
- Built, not yet run on real GitHub: the pipeline, the land job, the incident and event triggers, the Kaggle serve
  notebook, and the lines and presets.
- Not built: an instant no-Kaggle `piv demo` mode, so the GIF is not recorded yet.
- Not released: the `0xctx/pivotal` repo and its release, so `piv init` refuses until a release exists.

## Where next

- [docs/QUICKSTART.md](QUICKSTART.md): your first 15 minutes.
- [docs/SECURITY.md](SECURITY.md): who can do what, and the Kaggle rules.
