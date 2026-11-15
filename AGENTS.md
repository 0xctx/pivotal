# AGENTS.md

Pivotal is a serverless dark factory for repos: `engine/` runs the jobs, `.github/workflows/` wires them, `gh-piv/`
is the `piv` CLI, and `presets/`, `lines/`, `templates/`, `kaggle/` and `docs/` are what a repo and a reader are given.

## Commands

Run from the repo root. All of these must pass before a change lands.

- `python3 test/run_suite.py`: unit tests, in parallel (`-j N` or `PIVOTAL_TEST_JOBS` to change; by default one worker per CPU, at most 8). This is the check in `.claude/sysop.json`. When it does not refuse, it runs every module `python3 -m unittest discover -s test` runs: one process per module, or one per TestCase class for the modules in `SHARD_BY_CLASS`. It refuses before it starts, naming what it refused and running nothing, for a symlink to a directory under `test/`, a test module in a subdirectory (discovery may run either; the runner lists only the top level) and a directory it cannot read. That refusal can over-refuse: discovery does not run a subdirectory without an `__init__.py` either. Every shard must run exactly the tests discovery lists for it, and the run fails on any shard that fails, errors, crashes, times out, prints no result or runs another number of tests. A module whose tests cannot be listed is reported as a failed shard named after it; the other modules still run. The modules in `SERIAL` in `test/run_suite.py` run alone afterwards, each with its reason. A failure is reproduced with the command the run prints: `python3 -m unittest discover -s test -p <module>.py`, or `PYTHONPATH=test python3 -m unittest <module>.<Class>` for a class shard.
- `python3 -m unittest discover -s test`: the serial fallback, one process for every module (about 28 minutes). Use it to check the runner itself or when a parallel result is in doubt.
- `git ls-files -z -- '*.sh' | xargs -0 shellcheck`: shell lint.
- `actionlint -ignore 'unexpected key "queue" for "concurrency" section'`: workflow lint, discovering every workflow. Then the same with `templates/caller.yml`, because the pinned actionlint predates the `queue` key the caller and the publish job use.
- `zizmor .github engine/setup/action.yml templates/caller.yml`: workflow, action and caller security audit. Set `GH_TOKEN` for the online audits.
- `bash test/docs.sh`: the docs gate (F9). It runs `python3 -I engine/doccheck.py .` (links, code fences and spelling in every
  Markdown file), `--help` of each `piv` subcommand (the list is held to `cli.build_parser()` by `test/test_docs.py`), the
  UNMEASURED check on `docs/engines.md`, `spikes/kaggle_probe.py --help`, and `spikes/headless_probe.sh claude`. The probe runs
  `claude --version`, `--help` and an unknown flag when claude is on PATH; without it the probe reports `not installed` and
  passes. No live model call is made. CI runs it as its last step (`.github/workflows/ci.yml`). A failing step stops the run
  and names itself.

The CI pins its lint tools. To run them locally:

```
python3 -m venv .venv
.venv/bin/pip install shellcheck-py==0.11.0.1 actionlint-py==1.7.12.25 zizmor==1.31.1
export PATH="$PWD/.venv/bin:$PATH"
```

## Map

- `engine/`: Python ≥3.11 stdlib and bash, run inside jobs. `route.py`, `run.sh`, `build.py` (the secret
  scan), `report.py` (the redacted summary and the report job), `trust.py`, `plan.py`, `ack.py`, `where.py` (the runner choice), `job.py` (also the one decision of the loop ladder: `read_level`, `standing`, `read_start`; its docstring holds the table), `buildjob.py` (the build job), `review.py` (the verdict schema), `reviewjob.py` (the review job), `assemble.py` (the assemble job: the classification, the stack, `land.json`), `gatejob.py` (the gate job), `gate.py` (the `gate-<id>` artifact schema), `publish.py` (the publish job: the F4 gate, the fast-forward, the stack and draft PRs, the seal), `seal.py` (the `pivotal/gate` seal: its body and the land job's verifier), `modelsup.py` (the models-up job: one Kaggle session for every repo) and `land.py` (the land job: trigger to ready PR, seal verification, re-gate if the base moved, fast-forward or, on a protected branch only, a pinned API merge whose commit is checked, re-read before "landed") are implemented. The other
  modules are placeholders, and each file's first line names the slice that fills it.
- `engine/setup/`: the composite action that installs the pinned worker CLIs (`pins.json`, `install.py`, `action.yml`) on a
  hosted runner, from the build and review tiers of the preset (docs/engines.md). `engine/workers.py` is the manifest of those
  installs that the build and review jobs read before any agent starts.
- `.github/workflows/`: `ci.yml` is this repo's CI. `factory.yml` has the `ack`, `plan`, `models-up`, `build`, `review`, `assemble`, `gate`, `publish` and `report` jobs, and `land.yml` is the reusable land job (`/pivotal land`).
- `kaggle/`: `catalog.json` (what T0 measured, with each candidate's `serve` flags), `pins.json` (the pinned fetches; all null until a live run, and a null pin refuses the render), `serve.ipynb.tmpl` and `render.py` (the serve notebook for one catalog model; unmeasured is refused) and `lifecycle.py` (idle, hard-exit, bound-domain and redaction logic, copied whole into the notebook and unit-tested by `test/test_notebook.py`).
- `docs/`: the Markdown a reader sees (`QUICKSTART`, `DEMOS`, `RECIPES`, `GUIDE`, `SECURITY`, `FAQ`, `RUNNER`,
  `engines`) and `demo.tape`; `README.md` is at the root. `test/test_docs.py` holds them to the code, and
  `test/docs.sh` runs the commands they name.
- `presets/` (the shipped presets), `lines/` (the built-in line definitions), `templates/` (`caller.yml`, the one file
  `piv init` writes to each repo) and `gh-piv/` (the `piv` CLI) are in place.
- `test/`: unittest. `run_suite.py` is the parallel runner (the check): it runs each module in its own process and `test_run_suite.py` tests it. `fakecli.py` writes fake agent CLIs that tests run as real subprocesses. `fakehub.py` is a stateful fake `gh`, and `sandbox.py` runs the caller and `factory.yml` job by job against it (`test_sandbox.py`: the T2 scenarios).

## Ground rules

These come from the Never-build list and the invariants of the design.

- Python stdlib and bash only. No third-party packages in the engine and no lockfile.
- No AI in the control plane: decisions are workflow conditions and code.
- Third-party actions are pinned by commit SHA. Lint tools are pinned by version.
- A job that runs model output or repo code holds no GitHub write permission (F1). Untrusted text
  reaches scripts only through `env:` (F2).
- Fail closed: a missing or malformed plan, verdict or result is never landable (F5).
- Tests use fake CLIs. They never call a real provider.

## Working principles

- Reproduce a bug as a failing test before fixing it. Never weaken or delete a test to get green.
- Stage files by name. A commit message names the outcome.
- Documented commands run in CI, so the docs stay true (F9).
