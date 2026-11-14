"""Where the jobs run: the ``runner`` output the plan, build and report jobs use for runs-on.

Reads RUNNER_VAR (PIVOTAL_RUNNER), RUNNER_PUBLIC (PIVOTAL_RUNNER_PUBLIC), TRUSTED and REPO_PRIVATE from env and
writes ``runner=self-hosted`` or ``runner=ubuntu-24.04``. The rule is select_runner. It reads no issue text and
holds no token, and it never fails on a bad value: that is the hosted label. Run it as ``python3 -I pivotal/engine/where.py``.
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

if __package__ in (None, ""):  # run as a script under python3 -I
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import job  # noqa: E402



def select_runner(env: Mapping[str, str]) -> str:
    """Where the jobs run: ``self-hosted`` only when PIVOTAL_RUNNER is exactly that, the run
    is trusted, and the repo is private or PIVOTAL_RUNNER_PUBLIC is exactly ``trusted-only``. Anything else, a
    missing or malformed value included, is the GitHub-hosted label, so an unknown cannot reach your machine."""
    if env.get("RUNNER_VAR") != job.SELF_HOSTED or env.get("TRUSTED") != "true":
        return job.HOSTED
    if env.get("REPO_PRIVATE") == "true" or env.get("RUNNER_PUBLIC") == "trusted-only":
        return job.SELF_HOSTED
    return job.HOSTED


def run(env: Mapping[str, str]) -> None:
    job.write_outputs(job.read_output_path(env), {"runner": select_runner(env)})


def main(env: Mapping[str, str]) -> int:
    return job.run_job(run, env)


if __name__ == "__main__":
    sys.exit(main(os.environ))
