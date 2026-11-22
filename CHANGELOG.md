# Changelog

## Unreleased

- A merge after the base moved sends a re-gate instead of only stopping: when the base has moved past the sha the stack
  was sealed on, the land job pushes nothing, sends a `repository_dispatch` of type `pivotal-regate` carrying the issue,
  round 0's pin, the issue's own stack branch, the sealed sha and the base it was gated on, and the land job stays red.
  `trust` accepts that dispatch only from this repository's own token, for the issue's own stack branch and the live issue's
  pin, and resolves it to a `regate` kind that no level starts yet (`job.NOT_STARTED`), so the run it starts reaches the
  factory and stops: no comment, no label, an empty plan matrix, and no job after it builds anything. The replay of the stack
  on the new base is the next slice (land-regate-replay). Built and tested with fakes, not run live.

- `piv doctor REPO` now compares the caller's pin with the engine's releases: `pin: <sha12> (release vX.Y.Z)` when the
  pin is the latest release's commit, a problem with one fix line when it is behind (`pin: <sha12> (behind: latest is
  vX.Y.Z at <sha12>; run piv upgrade REPO)`) or when the engine has releases and the pin is none of them, and an
  `[unknown]` line when the release list cannot be read. `piv doctor --all` reads the release list once for every repo.
- `piv upgrade [REPO | --owner OWNER] [--release REF]` moves many callers to one engine release: it reads each repo's
  caller, re-renders it at one release with the engine name that caller pins (a fork keeps its fork) and writes it only
  when it differs, one line per repo (`current`, `updated <old sha12> -> <new sha12>` or `refused: <why>`), exit 1 when
  any repo was refused, and no variable, secret or label is touched. CONTRIBUTING.md's "Releases and pins" and the FAQ
  now say so instead of naming the command as not built. Built and tested with fakes, not run live.

- The docs state the release rule: a published tag (`v1`, `v1.1`) is never moved, deleted or force-pushed once another
  repo may pin it, and a fix ships as a new tag on top of the history; `piv init --release <tag-or-sha>` pins and
  `piv init --update` moves a caller's pin, `piv doctor` prints the pin, and an upgrade command that moves many callers at
  once is named as not built. CONTRIBUTING.md gains "Releases and pins", the FAQ answers "What does pinned mean? Why a
  commit?", and SECURITY.md says why the caller pins a commit (a reused workflow runs the pinned code only).
- The docs gate (`engine/doccheck.py`, and the walks it shares with `test/test_docs.py`) checks the Markdown files a git
  checkout tracks (`git ls-files -z -- '*.md'`), so an untracked scratch file in a local worktree (the operator's
  `.sysop` store) is not a doc and no longer fails the gate; a directory that is not a checkout is still walked, with
  `.git`, `.sysop`, `node_modules`, `.venv` and `__pycache__` skipped explicitly. A clean CI checkout scans the same
  files it scanned before.

- A reviewer whose reply misses the reply format (no verdict line, a verdict that is not the last line, an ambiguous verdict, a wrong or missing `Reviewed` or `Covered` line, an empty or non-text message) no longer ends the review: the next reviewer is tried, the skip is a `fell_back` line with reason `bad_format`, and a chain that ends with no verdict keeps the last reviewer's reason and a filtered excerpt of its reply's last line (`no_verdict: TEXT`). A secret in the reply, a write to the tree and an oversized reply stay terminal. The chain is still bounded by `MAX_ENTRY_TRIES`.
- The land job accepts a seal whose check run sits in the `github-actions` app's check suite for the sealed commit, not in the workflow run's own suite. Measured on real GitHub (run 43646262167, 2026-11-14), a check run a job creates sits in the app's suite for its head SHA, so the old same-suite rule refused every genuine seal (`piv land` said `suite_differs`). The land job reads that suite with one `GET repos/<repo>/check-suites/<id>` through its own client, with the `checks: read` it already has, before anything is downloaded; a suite that is not the app's or not for the sealed commit, or that GitHub does not answer for, is refused, and a lookalike that names one cannot block the genuine seal. The run's own `check_suite_id` is no longer compared.
- A run of one slice no longer fails every slice as `no_artifact`: `actions/download-artifact` extracts a single downloaded artifact straight into its directory, not into `slice-<id>/`, `review-<id>/` or `gate-<id>/`, and the assemble, publish and fix-round jobs now read either layout (`assemble.artifact_dir`). A directory holding the files directly is read only when exactly one artifact of that kind is planned; with two or more it is ambiguous and not read, so its review counts as missing and its gate or slice as invalid. A link in place of a file is still refused.
- A provider error from a reviewer (a pi or omp `stopReason` error, or claude `is_error`) now falls through to the next reviewer whatever its status, as a 429 or 5xx already did; a 4xx or an error without a code no longer ends the review at once. When the chain ends, the reason is `agent_error[:CODE][: TEXT]`: TEXT is the provider's error message (claude: its result), with held secrets removed, only plain characters kept and at most 160 characters, as an `agent_exit_N` line is. Nothing else of the log is shown or uploaded.
- The publish and land jobs accept a `GH_TOKEN` of up to 4096 printable ASCII characters (was 255), matching the real GitHub token length of 377 characters with dots, dashes, and underscores. The error message now says "GH_TOKEN is not set or is not printable ASCII up to 4096 characters".
- A reviewer that exits non-zero but whose stdout JSON contains a provider error (stopReason error for pi/omp, is_error for claude) with status 429 or 5xx is now treated as a skip (rate-limited): the chain falls through to the next reviewer, and if the chain ends the reason is `agent_error:CODE`. Other provider errors and non-JSON exits keep the existing `agent_exit_N` reason.
- The assemble job reads a base that is the root of a shallow checkout (`actions/checkout` at its default fetch depth): the base is fetched with `--depth=1`. Before, the job refused the run with "the base commit cannot be read from REPO_DIR".
- A reviewer that exits non-zero without a verdict is missing as `agent_exit_N` (N its exit status, 1 to 255) and, when its errlog has a line to show, `agent_exit_N: TEXT`: the first non-empty line of the errlog with the held secret values removed, only plain characters kept, at most 160 characters. Nothing else of the logs is shown or uploaded.
- Open models first: omp is the default worker of the free reviewers (`free-kaggle`) and of `one-key`; agy is not supported (no pin, its probe spike and test removed); `claude-plus-free` is removed; `claude-sub` is optional and paid, with omp reviews, and is not the init default.
- omp version parsing: `_install_direct` (and `_install_npm`) now accepts the REAL omp `--version` format `omp/18.20.0` (name/version) and bare `18.20.0`; the fake omp in tests emits `omp/18.20.0`; refusal tests cover wrong version (`omp/18.20.1`, `18.20.1`), space format (`omp 18.20.0`), and empty output; red test against whole-word comparison mutant.
- A pi or omp review is one model request: the whole prompt goes inline with `--no-session --no-tools` (up to 96 KiB; larger is `patch_too_large`), not a read-the-file sentence that made several requests; not run live yet.
- Docs restructured to four pages (README, QUICKSTART, SECURITY, FAQ) plus Models (engines.md) and Examples (EXAMPLES.md); DEMOS, RECIPES, GUIDE, RUNNER, RELEASE merged or deleted; CHANGELOG cut to one line per release; all links fixed; test_docs and docs.sh updated.
- ZeroClaw moved out of this repository.
- Docs restructured to four pages (README, QUICKSTART, SECURITY, FAQ) plus Models (engines.md) and Examples (EXAMPLES.md); DEMOS, RECIPES, GUIDE, RUNNER, RELEASE, ZEROCLAW merged or deleted; CHANGELOG cut to one line per release; all links fixed; test_docs and docs.sh updated.
- omp (oh-my-pi) supported as a measured worker: pinned release binary install with sha256 verification, routed like pi (any door, JSONL output), review output extracted identically to pi; not run live through a factory run.
