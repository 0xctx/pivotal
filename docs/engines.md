# Models

> See the proven table in [README.md](../README.md).

> **Status: partial.** The Kaggle part is measured (time to healthy for three models, the second-session answer; tok/s is sysop's older figure from 2026-10-06, see below). The install of `claude`, `pi` and `omp` on a GitHub-hosted runner is **measured (hosted) 2026-11-11/2026-11-14** (about 20 s for claude/pi, 2 s download for omp on `ubuntu-24.04`, below); the headless call of every CLI in a hosted job is still **UNMEASURED: needs a pushed GitHub repo and provider keys in Actions.** Run `spikes/headless_probe.sh <cli> <outdir>` in a job per CLI (`claude`, `pi`, `codex`, `grok`, `omp`) to fill it. agy is not supported (below). The Kaggle serve configuration changed on 2026-11-11 to agent-sized values, and candidate A served the live build of run 43279525237 with 2 x 32768 on 2026-11-12; the tok/s of the served configuration is **UNMEASURED** (see Candidates).

## Open models first: what each shipped entry has measured

Omp is the default agent. The free path, `free-kaggle`, builds on the Kaggle-served Qwen through pi and reviews through free reviewer APIs
(Groq, Gemini, and Gemini through OpenRouter, which bills per token). Claude is optional and paid (`claude-sub`, your Claude plan),
and it is never the `piv init` default. agy is not supported.

| Preset | Entry | Worker | What is measured |
|---|---|---|---|
| `free-kaggle` | build `pi:kaggle/qwen3.6-35b-a3b` | pi | the Kaggle endpoint and pi's headless call (run 43279525237) |
| `free-kaggle` | review `omp:groq/openai/gpt-oss-120b`, `omp:google/gemini-3.6-flash` | omp | omp's headless call with each model, on a hosted runner (run 43562562687) |
| `free-kaggle` | review `omp:google/gemini-3-flash-preview`, `omp:openrouter/google/gemini-3-flash-preview` | omp | **UNMEASURED** through omp: no job has called these |
| `one-key` | build `omp:openrouter/deepseek/deepseek-v4-flash`; review `omp:openrouter/google/gemini-3-flash-preview` | omp | **UNMEASURED** through omp: omp as a builder (with tools, editing files) and OpenRouter through omp are not called in a job |
| `claude-sub` | build `claude:anthropic/sonnet`, `claude:anthropic/opus` | claude | the claude install is measured; its headless call in a job is **UNMEASURED** |
| `claude-sub` | review `omp:google/gemini-3.6-flash`, `omp:openrouter/google/gemini-3-flash-preview` | omp | as the `free-kaggle` rows above |

- **omp's Kaggle endpoint is UNMEASURED.** omp's custom-endpoint configuration for the Kaggle server is not measured yet, so no shipped preset names `omp:kaggle/...`. The build stays on pi.
- **omp as a builder is UNMEASURED.** The review runs omp with `--no-session --no-tools` (measured, run 43562562687). A build runs with tools, so it edits files, and that call is not measured through omp in a job.
- **agy is not supported.** Its headless call was unreliable on a hosted runner (below). It has no pin, no install, and no shipped preset names it.

## Live record

The runs this repo has made on real GitHub, Kaggle and ngrok, and what each proved. A run that is not here has not run live. The [README](../README.md) and the [Quickstart](QUICKSTART.md) carry the short form of this table.

| Run id | Date | Repo | What it proved | Numbers measured |
|---|---|---|---|---|
| 43279525237 | 2026-11-12 | `0xctx/piv-sandbox-private` | `trust`, `ack`, `plan`, `models-up` (a warm Kaggle session, reused) and `build`; `pi:kaggle/qwen3.6-35b-a3b` produced a real patch (a README typo) and the gate was green. | no tok/s, no per-run time or cost; the Kaggle session was warm and reused |
| 43171106784 | 2026-11-11 | `0xctx/piv-sandbox-private` | `models-up`: a cold Kaggle session needs about 31 minutes (llama.cpp is built from source and the 35B model is downloaded), past the job's 12-minute wait; nothing was built. | about 31 minutes cold against the 12-minute wait |
| 43092707402 | 2026-11-10/11 | `0xctx/piv-sandbox-private` | `review`: the job's steps ran and the no-valid-verdict step failed; the reason is not known, because GitHub's log and artifact APIs returned HTTP 502. | none recorded |
| 43562562687 | 2026-11-14 | `0xctx/piv-sandbox-private` | `omp` install and headless call measured: release asset download, sha256 verification, version, `--help` flags, JSONL output shape (same as pi), model calls via Groq and Google. Not run live through a factory run. | download 2 s, sha256 match, `--version` OK, JSONL events match pi |  

## How each number was got

| Label | Meaning |
|---|---|
| **measured (local)** | Run on the build host on 2026-10-27 with `spikes/headless_probe.sh` (no credentials, no network call). Raw output is kept outside the repo in the sysop store's `reports/spikes-raw/`. |
| **measured (sysop serve)** | Time to healthy and the second-session answer: run on 2026-11-01 with sysop's serve module against the operator's Kaggle account. (The tok/s figures are older: sysop's `models.json`, 2026-10-06.) |
| **UNMEASURED** | Not run. Do not fill it from a guess, from another vendor's docs or from a test log. |

`classify_failure` and the presets must not rely on any text or id below until its cell is measured in a hosted job.

## Agent CLIs

### Installed on the build host

| CLI | Vendor | Version (measured local) | Notes |
|---|---|---|---|
| `claude` | anthropic | 2.1.312 (Claude Code) | measured (local) |
| `agy` | google | 1.4.8 | measured (local); not supported (below) |
| `pi` | per model | 1.1.0 | measured (local) |

### Headless flags (measured local, from each `--help`)

These are the flags the installed version names. They are not yet proven to work in a job.

| CLI | Headless run | Output | Model | Permission and other flags |
|---|---|---|---|---|
| `claude` | `-p, --print` | `--output-format` (only with `--print`) | `--model <model>` | `--permission-mode`, `--dangerously-skip-permissions`, `--allowedTools`, `--max-budget-usd`, `--no-session-persistence`, `--bare` |
| `agy` | `-p, --print` (alias `--prompt`) | `--output-format text\|json\|stream-json` | `--model` | `--mode accept-edits\|auto\|plan`, `--print-timeout`, `--json-schema`, `--disable-slash-commands` |
| `pi` | `-p, --print` | `--mode text\|json\|rpc` | `--model <provider/id>` | `--no-session`, `--no-tools`, `--tools` (allowlist), `--provider` |
| `omp` | `-p, --print` | `--mode text\|json\|rpc` | `--model <provider/id>` | `--no-session`, `--no-tools`, `--thinking LEVEL`, `--session-dir DIR` |

> **Measurement note:** `omp`: `--no-session` and `--no-tools` are measured, run 43562562687. `pi`: `--no-session` and `--no-tools` are from --help, unmeasured. The review job passes both to `pi` and `omp` with the whole review prompt as the last argument, so one review is one model request.

### Exit codes and error texts

Measured local: an unknown flag, run with no credentials.

| CLI | Exit | Error text (first line) |
|---|---|---|
| `claude` | 1 | `error: unknown option '--no-such-flag-spike'` |
| `agy` | 2 | `flags provided but not defined: -no-such-flag-spike`, then the usage block |
| `pi` | 1 | `Error: Unknown option: --no-such-flag-spike` |
| `omp` | 1 | `Error: Unknown option: --no-such-flag-spike` |

Not yet measured, for any CLI:
- exit code and text for a success, an auth failure, a missing key, a bad model id, a timeout and a timeout cap;
- **rate-limit text and exit code**: UNMEASURED for every CLI, and so `classify_failure` has no text to match yet.

### Model ids

UNMEASURED for every CLI. Pivotal presets must take their ids from a probe run, never from memory.

**measured (local), 2026-11-11:** `pi --offline --list-models gemini-3.8` lists `google gemini-3.8-flash`. This was a local catalog check, not a live call. `google/gemini-3.8-flash-high` (an agy id the free-kaggle preset used before open models came first; agy is not supported) is not a pi model id; pi's id for that model is UNMEASURED.

### GitHub-hosted job per CLI

UNMEASURED: needs a pushed GitHub repo and provider keys in Actions. This needs a workflow run in a GitHub-hosted job with the operator's repo and secrets (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, the xAI key, `GEMINI_API_KEY`). The probe script is ready: `spikes/headless_probe.sh <cli> <outdir>`, and it records the version, the `--help` text and the unknown-flag exit code.

## Installed on a GitHub-hosted runner (engine/setup)

The build and review jobs install the CLIs their preset names, before the agent step, with the composite action `engine/setup`
(`uses: ./pivotal/engine/setup`, the engine's own checkout at the caller's `engine_sha`). `engine/setup/pins.json` holds the pins,
and `engine/setup/install.py` makes the installs. The step holds no secret: npm and each installed CLI get a bare environment.

| CLI | Install (in `pins.json`) | Pinned version | On a hosted runner |
|---|---|---|---|
| `claude` | npm `@anthropic-ai/claude-code-linux-x64`, the native binary, with `--ignore-scripts` | 2.1.312 | measured (hosted) 2026-11-11: installed in about 20 s on `ubuntu-24.04` |
| `pi` | npm `@earendil-works/pi-coding-agent`, with `--ignore-scripts`; needs node 22.19.0 or later (`node_min` in `pins.json`; the runner's node satisfied it on `ubuntu-24.04`) | 1.1.0 | measured (hosted) 2026-11-11: installed in about 20 s on `ubuntu-24.04` |
| `omp` | direct download `https://github.com/can1357/oh-my-pi/releases/download/v18.20.0/omp-linux-x64`, sha256 verified | 18.20.0 | measured (hosted) 2026-11-14: downloaded in about 2 s on `ubuntu-24.04` (run 43562562687) |
| `agy` | none: not supported, so no pin | none | not installed; no shipped preset names it |
| `codex`, `grok` | none | none | UNMEASURED; not installed |

**agy is not supported.** Its headless call was unreliable on a hosted runner. Measured on a hosted runner on 2026-11-11 (`ubuntu-latest`, x86_64, Linux, two runs on a throwaway branch of a sandbox repo, the same workflow each time, the probe script, removed with the open-first-defaults change and kept in git history): `https://antigravity.google/cli/install.sh` was fetched and run once in the probe's scratch `HOME` (not the runner's `~/.local/bin`), installing agy 1.5.6 there. In the first run the script advertised no versioned URL, only an auto-updater service URL (the probe searched advertised URLs and did not search for checksum text), so no pin is possible; the second run gave the same verdict. The headless call (`agy -p` with `GEMINI_API_KEY` and `modelProvider: gemini`, model `gemini-3.8-flash-high`) was not reliable: in the first run neither mode exited within 120 s (plain: exit 124, 19 bytes; under a pseudo-terminal: exit 124, 256 bytes, the output contained the substring `OK`; that run used the probe's substring check). In the second run, with the revised probe (whole-line `OK` match, key withheld from the install script, `script -e`), the plain call exited 0 with the line `OK` (3 bytes) and the pseudo-terminal call again exited 124 (260 bytes, no whole-line `OK`). Verdict `NOT_PINNABLE` in both runs (the parser, removed with the same change): the blocker is the missing pin, and the plain call hung in the first run and answered in the second, and the pseudo-terminal call hung in both, which is not a call to route a review to, so agy stays out of `engine/setup/pins.json`. Not measured: why the call hangs, more than two runs, or a login-based (OAuth) run.

Measured on a hosted runner on 2026-11-14 (`ubuntu-24.04`, x86_64, Linux, run 43562562687): the release asset `omp-linux-x64` from `github.com/can1357/oh-my-pi/releases/download/v18.20.0/` is 287484080 bytes, downloads in 2 s, sha256 `c8694be8c6e6a1f45e3636dd8a8e9eaa78878ad629a89000b403ec5e456d3f89` (equals `SHA256SUMS.txt`); `omp --version` prints `omp/18.20.0`. Headless flags (also from `--help`): `-p/--print`, `--mode text|json|rpc`, `--model PROVIDER/ID` (e.g. `groq/openai/gpt-oss-120b`, `google/gemini-3.6-flash`), `--no-session`, `--no-tools`, `--tools LIST`, `--thinking LEVEL`, `--session-dir DIR`; keys come from the same env vars as pi (`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`...). `omp -p --mode json --no-session --no-tools --model groq/openai/gpt-oss-120b 'Reply with the single word OK.'` exited 0 in 3 s with stderr empty; the JSONL events were session, agent_start, turn_start, message_start, message_end, message_start, message_update, message_end, turn_end, agent_end; the last assistant message_end has role assistant, content `[{type:text,text:OK}]`, stopReason stop (same shape as pi). `google/gemini-3.6-flash` gave the same in 10 s. omp keeps its config under `HOME/.omp`.

The claude version is the one in the table above. The `@anthropic-ai/claude-code` wrapper is not installed, because its native
binary is copied in by a postinstall script that `--ignore-scripts` skips; the platform package `claude-code-linux-x64` holds the
binary itself.

What is pinned is the top-level version of each CLI, and nothing below it. pi 1.1.0's `package.json` declares 23 direct
dependencies: 17 are exact versions and 6 are ranges, the `@earendil-works/*` packages at `^1.1.0`. `npm install` resolves those
six to the newest versions that match when the job runs, and the transitive tree under all of them is not pinned at all. The
tarball's root has no lockfile (its only `package-lock.json` files are under `examples/`). Its `overrides` entry (`protobufjs`) is
not applied when pi is installed under our prefix, because npm reads `overrides` from the root project only. The install's digest
records what was resolved, so a change after install is caught, but those versions are not pinned here. Vendoring a lockfile for
that tree is not done. The Kaggle CLI has the same accepted risk.

Node is the runner's, not the install's. pi runs under the `node` on the job's PATH, which is checked only once at install (its
version against `node_min`). Nothing re-checks it before a call.

What the install does not do:

- A worker that cannot be installed (no pin for the runner's platform, a node older than the pin, an npm failure, or a version that
  the CLI does not report as pinned) is a warning, and the job falls through with `fell_back: <entry> → <next> (not installed)`. It
  is never a missing binary (exit 127).
- A worker that a tier names but `pins.json` does not pin refuses the install (exit 1): a new worker is pinned before a preset names it.
- The install is measured on a GitHub-hosted runner (2026-11-11, `ubuntu-24.04`: `claude` and `pi` installed in about 20 s). The sandbox (`test/sandbox.py`) runs the step with a fake npm and a fake node, and no other runner image is measured.

**The install is checked before every agent call.** `install.py` writes `pivotal-workers.json` (the install directory and the
digest of every entry under it). A build or review job reads it once, before any agent starts (`engine/workers.py`, the variable
`PIVOTAL_WORKERS`). Each entry of a chain may run with a different door secret, and the install sits in a directory the agent's own
user can write, so before each agent call the job re-digests the install of that entry's worker. A changed install refuses the job
(`engine_modified`) and the next entry does not run. The digest covers every file, mode and link under the install directory, so
an agent that edits, adds or removes anything there is caught.

A pinned worker runs by its manifest's absolute `bin` path, never by its name through PATH. The setup step puts no directory on
PATH: it writes no GITHUB_PATH entry and makes no links. What the engine guarantees: the binary it starts for a pinned worker is
the one at the path the manifest names, and that install's digest is checked before the call. A file the agent planted in a
directory on the job's PATH is not what the engine starts for a pinned worker. This covers only the worker binary the engine
itself starts: a shebang or any other name a process looks up is not covered (below). A same-named CLI earlier on PATH does not
refuse the install. What the engine does not guarantee:

- Any name that a call's process, or engine/run.sh's own bare names (`env`, `setsid`, `timeout`, `mktemp`), looks up on the PATH
  and that no directory the job's user cannot write provides. `safe_path` orders the locked directories first and keeps the
  writable ones after them, so a name the agent plants in a writable directory on that PATH runs wherever no locked directory has
  one. The setup step's own directory is not on that PATH, so a name planted there never runs.
- The install's dependencies: pi's ranges and their tree are resolved when the job runs, and the digest records them only after
  (above).
- The node that pi runs under, an instance of the class above. pi's `#!/usr/bin/env node` finds `node` on the PATH of the call. A
  `node` that the agent plants in a writable directory on that PATH runs in place of the runner's node when it is found first.
  The engine does not check which `node` the call finds, and the digest does not cover node (above).
- The HOME of the agent's own processes. The agent's HOME is the job's real HOME, shared across the entries of one job
  (pre-existing): a planted `~/.gitconfig` or `~/.claude` settings file could reach the next entry's agent git and CLI. The
  engine's own git runs with `HOME=paths.home` and `GIT_CONFIG_GLOBAL=/dev/null`, so it does not read them. The per-entry key
  isolation covers the CLI binary, not HOME.


### Agent output (engine/run.sh)

`engine/run.sh` `agent_run` writes the command's stdout to `--log`. Without `--errlog`, stderr is merged into the log: this is what the build job does. The review job passes `--errlog` and runs the reviewer in JSON mode (`pi -p --mode json --model MODEL PROMPT`, `claude -p --output-format json --model MODEL PROMPT`; the headless flags above), so stderr goes to the errlog and the verdict is extracted from the final assistant message in the stdout JSON log (pi: the last `message_end` event whose `message.role` is `assistant`, taking text blocks from `message.content` — `docs/json.md`, `docs/message-types.md` in the pi package; claude: the `result` field of the JSON document). A diagnostics line on stderr therefore does not make the verdict missing. The raw JSON log is capped separately from the extracted review text; a log that is not that structure is a missing verdict (`not_text`). A trailing fence line after the verdict inside the extracted message is not tolerated (`verdict_not_last`): the brief forbids code fences and anything after the verdict line.

### pi and the Kaggle door

pi reads its provider config from `<agent dir>/models.json` (`docs/models.md` in the pi package). For each pi call the engine
(`engine/piagent.py`) makes a fresh 0700 agent directory with mkdtemp, outside the repo checkout, and removes it when the call ends.
`PI_CODING_AGENT_DIR` points pi at it, for the agent child only. For a `pi:kaggle/<model>` entry the file holds one provider:

- `baseUrl` is `https://<domain>/v1`, the endpoint of `PIVOTAL_KAGGLE_URL`, and `api` is `openai-completions`;
- `apiKey` is the text `$PIVOTAL_KAGGLE_KEY`. pi interpolates it from its own environment, and run.sh gives that environment the one
  secret of the call. The key value is in no file, argv or log, and a test scans the directory, the argv and the log for it;
- `headers` holds `ngrok-skip-browser-warning: 1`, so the free ngrok domain does not serve its browser warning page;
- `models` has the one model, with `contextWindow` set to the per-slot context `kaggle/catalog.json` records for it (`serve.ctx`:
  32768 for `qwen3.6-35b-a3b`, 16384 for the other two candidates, the agent-sized configs in the catalog; `render.py` refuses a per-slot context below 16384). The catalog has no measured context window (its `context` field is null), so the served value is the one used.
  `maxTokens` is half of it: a choice of the engine, not a measurement. A longer prompt than the served context is a failure the
  run sees as an agent error, not a measured limit.

Every agent call (claude and pi alike) gets `DISABLE_AUTOUPDATER=1`, so a CLI does not replace the version the job pinned with a
newer one. Every agent call also gets `PI_SKIP_VERSION_CHECK=1` (no latest-version request to pi.dev) and `PI_TELEMETRY=0` (no
install or update telemetry, and no provider attribution headers, as pi's `docs/environment-variables.md` describes). The gate and
every other command get none of these.

Each pi call's agent directory also holds `settings.json` with `"defaultProjectTrust": "never"`. pi reads that setting from the agent
directory only, and with `never` it skips a repo's `.pi/` resources (extensions, prompts, settings) without asking, as pi's
`docs/security.md` describes. That is not a complete boundary: pi reads the repo's project `sessionDir` before it resolves trust
(`docs/security.md`, "Project trust is not a complete startup boundary"), and the engine does not pin pi's session directory. pi still loads `AGENTS.md` and `CLAUDE.md` from the working directory whatever the trust setting. The
build and review jobs restore the default branch's copies of those files before the agent runs (R15), so the instructions an agent
reads are the default branch's.

## Kaggle serving (inference only)

### kaggle CLI (measured local)

| Command | Measured |
|---|---|
| `kaggle --version` | `Kaggle CLI 2.2.4` (the CLI also printed an outdated-version warning: 2.2.5 is available) |
| `kaggle kernels push -p <folder> [--accelerator ACC] [-t TIMEOUT]` | flags read from `--help` |
| `kaggle kernels status <owner>/<kernel>` | argument read from `--help` |
| bad args | exit 2 with usage text |

### Candidates, time to healthy and tok/s

**Two configurations are behind these figures, and neither is measured for the speed of what the endpoint serves.** The time to healthy figures were taken with sysop's 2 slots of 32768 config (`SERVE_SLOTS` 2, `SERVE_CTX` 32768), for all three models. The tok/s figures were taken with sysop's 8 slots of 4096 throughput benchmark config, which refused any request over 4096 tokens (`exceed_context_size_error`). That batch config is not the one the endpoint serves. pi's own system prompt was measured at about 1600 tokens live on 2026-11-11, from pi's own request, before the task and the files it reads. The endpoint serves 2 slots of 32768 for `qwen3.6-35b-a3b`, the config sysop's time to healthy was taken with and the context its agent jobs ran on that model with the same flags. It serves 2 slots of 16384 for `qwen3.8-27b` and `gemma-4-26b-a4b`: a conservative pick: sysop served both at 2 slots of 32768, and the 2 x 16384 config has no run of its own. **The tok/s of the served configuration is UNMEASURED: no tok/s has been taken with 2 x 32768 or 2 x 16384 in this repo, and the live run of 2026-11-12 showed that 2 x 32768 fits both T4s (run 43279525237), with the tok/s of that session unmeasured.**

Time to healthy was measured on 2026-11-01 with sysop's serve module (`sysop.py serve up <model>`, one model across both T4s, kernel `aa0008/sysop-serve`), once per model, with sysop's 2 slots of 32768 config for all three. `ready_s` is the time `serve up` took to get a healthy endpoint. **tok/s was not measured on 2026-11-01.** It is the figure in sysop's reference `scripts/kaggle/models.json`, which dates it to 2026-10-06 (A and B: "sysop batch 2026-10-06", 8 parallel, 48x256 tokens of code; C: "spike 2, 2026-10-06", 8 parallel, 64x256 tokens). It is not a measurement by `spikes/kaggle_probe.py`: the probe needs the endpoint key in an environment variable, and no key could be had without reading a credential file, which this task forbids. So `tok_s` stays null in `kaggle/catalog.json` and the figure sits in `tok_s_sysop`. Context and VRAM are not reported by `serve up`/`serve list`, so they are UNMEASURED.

| Cand. | Model (sysop id) | Quant | Time to healthy (2 x 32768 config) | tok/s (8 x 4096 batch config, sysop models.json, 2026-10-06, not the probe) | Context, VRAM |
|---|---|---|---|---|---|
| A | `unsloth/Qwen3.6-35B-A3B-GGUF` (`qwen3.6-35b-a3b`, MoE) | UD-Q4_K_M | 221 s | 111.1 | UNMEASURED |
| B | `unsloth/Qwen3.8-27B-GGUF` (`qwen3.8-27b`, dense) | UD-Q4_K_M | 169 s | 26.5 | UNMEASURED |
| C | `google/gemma-4-26B-A4B-it-qat-q4_0-gguf` (`gemma-4-26b-a4b`, MoE) | q4_0 QAT | 138 s | 133.7 | UNMEASURED |
| S | two small models, one per T4 | | UNMEASURED | UNMEASURED | not tested: sysop serve has no two-process mode |

Candidate A (`qwen3.6-35b-a3b`) served the live build of run 43279525237 (2026-11-12) and of the earlier run 43067000112 (2026-11-10/11) with the 2 x 32768 config; the tok/s of those sessions was not measured.

The "111-134 tok/s" is sysop's figure (A 111.1, C 133.7) from 2026-10-06; it is recorded with that provenance (`tok_s_source`, `tok_s_measured_on` in the catalog; `measured_on` dates time to healthy only), and is still not a `kaggle_probe.py` result. One run each: the times vary with Kaggle's queue and model download, so treat them as one sample.

GPU quota (printed by `serve up`): 0.81 h used / 29.19 h left at the first start, 0.96 h used / 29.04 h left at the start of the third; about 1.0 h used after the last `serve down`. The task used about 0.2 GPU hours, and every session was taken down.

### How the probe takes the numbers (still to run for tok/s)

`spikes/kaggle_probe.py --base-url https://<domain> --key-env <VAR> --model <id>` polls `GET /v1/models` for time to healthy, then times one `POST /v1/chat/completions`; tok/s is the reported completion tokens over the whole request time. The key comes from the named env var and is never printed. Its logic is covered by `test/test_kaggle_probe.py` against a local fake server; those tests prove the code, not a Kaggle number.

### Second concurrent GPU session

**Answered (measured 2026-11-01): a second concurrent GPU kernel is accepted.** While `aa0008/sysop-serve` (serving `gemma-4-26b-a4b`) was `RUNNING`, a second private script kernel `aa0008/spike-second-session` was pushed with `kaggle kernels push -p <dir> -t 120 --accelerator NvidiaTeslaT4`:
1. The push was accepted, exit 0: `Kernel version 1 successfully pushed.`
2. No refusal text was seen.
3. `kaggle kernels status` showed `KernelWorkerStatus.RUNNING` for both kernels at the same time; the second later reached `KernelWorkerStatus.COMPLETE` while the serve kernel was still `RUNNING`.

Limits of this test: the second kernel only slept 60 s and did not check that it was given a GPU (no `nvidia-smi`), so "accepted and ran" is shown, "both held a GPU" is not. It did not test a second model server, or what happens over a longer run or with a higher count. The test kernel was deleted afterwards. The "one fixed kernel per account" rule is therefore a design choice, not a Kaggle limit, as far as one concurrent pair shows.

### Serve notebook: what only a live session can show (models-up)

`kaggle/serve.ipynb.tmpl` ran nowhere; `test/test_notebook.py` runs its code against a fake world. These are the steps `models-up` does before the first real session, and what to do with the answers.

1. **The keyless check (decision, kept design).** `llama-server` is exposed through ngrok directly, with `--api-key-file` and no proxy in front (the sysop reference put a proxy there that refused everything but `/health`). Which routes `llama-server` answers without the key (`/v1/models`, `/props`, `/slots`, `/metrics`, `/v1/chat/completions`) is not known offline. Before the first real session, with the tunnel up and **no key sent**, request each of those routes through the public domain: **each of those five routes must return 401** (`/health` may answer without the key). The list of routes `llama-server` really serves must come from the pinned llama.cpp commit at the first live run (`kaggle/pins.json`); a route not in the five is not checked by anything here. Any other answer means the key does not cover that route: do not use the session, and put a refusing proxy in front.

   **Run it with the models-up job (implemented, OFF by default).** Set the repo variable `PIVOTAL_KEYLESS_CHECK` to `1` for one run. Once the session answers (reused, waited for or started), `engine/modelsup.py` requests `GET /v1/models`, `/props`, `/slots`, `/metrics` and `POST /v1/chat/completions` (an empty `{}` body) **without** an `Authorization` header and requires a 401 from each (`/health` is not requested). These five routes are all that is checked: the job does not enumerate what the server serves, so a green check says nothing about a route that is not named here. **The check is off by default, so `kaggle_ok=true` without it means only that a server answered `/v1/models` with the key and a model list; any server holding the key would pass.** A route that answers anything else, or does not answer, makes the job report `kaggle_ok=false` with `kaggle_why=keyless_open` and prints the route, so the builds skip the session. Its logic is covered by `test/test_modelsup.py` (`KeylessCheckTest`) against a local fake server; **the real answers of `llama-server` through ngrok are not known until a live run.** Unset the variable afterwards: the check costs five requests per run.

   **One session per start window (scope of the models-up acceptance).** `kaggle kernels status` is read, then `kernels push` runs: the two are not atomic and nothing outside Kaggle serialises repos (the caller's concurrency is per issue, and a lock outside Kaggle is not built). The serial case starts exactly one session: a run that looks after the first push sees the kernel queued or running and waits. Two runs that look in the same moment can both push; that duplicate is bounded by the notebook's bound-domain exit (the second kernel exits when ngrok says the static domain is already online) and is tested only as that bound (`RaceTest`). **Live check needed:** does a second `kernels push` of the same kernel while one is queued or running queue, cancel the first or refuse? It is not known offline. The reason a run's Kaggle entries were skipped (`kaggle_why`) reaches the run comment and step summary as one `kaggle:` line from the fixed vocabulary of `engine/modelsup.py`.
2. **The pins.** `kaggle/pins.json` holds the versions, llama.cpp commit and sha256, and model revisions that were current on 2026-11-01 (from PyPI, GitHub and Hugging Face). They are not confirmed by a live run: the first run is what confirms (or corrects) them; then set `confirmed_by_live_run`. The llama.cpp pin is a GitHub-generated tarball, whose bytes are not guaranteed to stay the same over time; a stale sha256 fails closed (the notebook refuses to build), and the fix is to re-pin. Only top-level packages are pinned (no constraints or hashes for their dependencies): the transitive risk is accepted for now.
3. **Flags and timings.** That the pinned llama.cpp accepts `--api-key-file`, `--metrics`, `--flash-attn on`, `-ctk/-ctv q8_0`; a build within the 3600 s limit; the ngrok SDK's text for an already-online domain (`ERR_NGROK_334`); that `close_tunnel` and the SDK print nothing past the redactor (both run inside the same capture, and need a live look).
4. **Idle.** Only a request that decodes tokens (or one running now) keeps the session up (F10): a `GET /v1/models` or `/health` does not. A client that wants the session kept sends a completion.
5. **The cached engine.** A `llama-server` found under `/kaggle/input` is used without a hash; that dataset is the operator's own and is trusted as such. Only the build from the pinned archive is checked. A session that builds one copies it to its working folder, so it is in the kernel's output: harvest it with `kaggle kernels output <you>/pivotal-serve -p <folder>` and upload that folder into the private dataset with `kaggle datasets version -p <folder> -m "cached llama-server"`, and a later session attaches `<you>/pivotal-serve-secrets` and skips the build. The copy is best effort: it prints `saved engine to output` and never fails a session that has an engine, so a missing `llama-server` in a harvested output is not a failure of the run that built it.
6. **A dead server.** If `llama-server` dies after `READY` the notebook exits as `error` (the kernel fails) and prints the masked log tail; it is not reported as idle.
