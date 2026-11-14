"""The `piv` command line. `gh piv` and the `piv` alias both reach ``main``.

``main`` takes its argv and environment, so tests pass both and a fake gh. A failure prints one line
to stderr and returns its exit code. Nothing printed here includes a secret value.
"""

import argparse
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import control, demo, do, doctor, event, init, presetcheck, remove, runner, serve, status, var
from .gh import Gh, PivError

# The repo root: this file is gh-piv/piv/cli.py, so presets/, engine/ and kaggle/ are two levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--owner", metavar="OWNER", help="read the repos of OWNER that have the pivotal topic")
    parser.add_argument(
        "--repo", action="append", default=[], metavar="OWNER/NAME",
        help="read this repo too; repeat for more (the topic search can lag, so name repos that matter)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gh piv", description="Set repos up for Pivotal, through gh and kaggle.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    setup = commands.add_parser("init", help="set one repo up for Pivotal (once per repo)")
    setup.add_argument("repo", metavar="REPO", help="owner/name, for example 0xctx/piv-sandbox")
    setup.add_argument("--check", help="the command that gates each change, for example 'npm test' (required)")
    setup.add_argument("--setup", help="a command that prepares the checkout before the check")
    setup.add_argument("--start", help="a command that starts the app, for the checks that need it running")
    setup.add_argument("--preset", help="the model preset, for example claude-sub (PIVOTAL_PRESET)")
    setup.add_argument("--level", help="watch, ask, prepare or land (default: prepare)")
    setup.add_argument("--release", help="a tag or commit of 0xctx/pivotal to pin the caller to (default: its latest release)")
    setup.add_argument("--engine", metavar="OWNER/REPO", help="the engine repo to pin the caller to (default: 0xctx/pivotal)")
    setup.add_argument("--update", action="store_true", help="replace the caller file if it differs from the release's")
    demo_cmd = commands.add_parser(
        "demo", help="make the public sandbox repo, file its three issues and print their run links (--clean deletes it)"
    )
    demo_cmd.add_argument("--clean", action="store_true", help="delete the sandbox repo, if its marker says it is the demo")
    filing = commands.add_parser("do", help="file a labelled issue that starts a Pivotal run, and print its run link")
    filing.add_argument("repo", metavar="REPO", help="owner/name, for example 0xctx/piv-sandbox")
    filing.add_argument(
        "text",
        metavar="TEXT",
        help="what to build, as the issue's text. Text that starts with - goes after --: piv do REPO -- '-fix'",
    )
    filing.add_argument("--line", help="build, incident, mirror or docs (default: build)")
    filing.add_argument(
        "--image", metavar="FILE", help="a mockup (png, jpg or jpeg), committed to the pivotal-assets branch"
    )
    filing.add_argument("--score", metavar="CMD", help="a command the gate runs to print one score (needs --target)")
    filing.add_argument("--target", help="the score the gate needs, for example 0.9 (needs --score)")
    land_cmd = commands.add_parser("land", help="comment /pivotal land on a ready issue (--all: every ready issue)")
    land_cmd.add_argument("repo", metavar="REPO", help="owner/name, for example 0xctx/piv-sandbox")
    land_cmd.add_argument(
        "issue", nargs="?", metavar="#ISSUE",
        help="the issue to land, for example '#12' (default: the one ready issue; quote #, or the shell reads it as a comment)",
    )
    land_cmd.add_argument("--all", action="store_true", help="land every ready issue of REPO")
    fix_cmd = commands.add_parser("fix", help="comment /pivotal fix on an open issue (a round-0 run from its text)")
    fix_cmd.add_argument("repo", metavar="REPO", help="owner/name, for example 0xctx/piv-sandbox")
    fix_cmd.add_argument("issue", metavar="#ISSUE", help="the issue to fix, for example '#12'")
    stop_cmd = commands.add_parser(
        "stop", help="cancel the runs of REPO (a /pivotal stop on each issue in flight); --all: every repo, and watch until resume"
    )
    stop_cmd.add_argument("repo", nargs="?", metavar="REPO", help="owner/name: the repo whose runs stop")
    stop_cmd.add_argument(
        "--all", action="store_true", help="every managed repo: set PIVOTAL_LEVEL to watch (until piv resume), and stop its runs"
    )
    stop_cmd.add_argument(
        "--repo", dest="named", action="append", default=[], metavar="OWNER/NAME",
        help="with --all, read this repo too; repeat for more (the topic search can lag)",
    )
    resume_cmd = commands.add_parser("resume", help="put back the level that piv stop --all set to watch")
    resume_cmd.add_argument(
        "--repo", dest="named", action="append", default=[], metavar="OWNER/NAME",
        help="read this repo too; repeat for more (the topic search can lag)",
    )
    alert = commands.add_parser("event", help="send the pivotal-event dispatch for an alert; the run files its incident issue")
    alert.add_argument("repo", metavar="REPO", help="owner/name, for example 0xctx/piv-sandbox")
    alert.add_argument(
        "--kind",
        required=True,
        help="event or incident: informational only. The dispatch always runs as an event (trust decides, and never reads "
        "this), so incident gets event semantics: the title is \"Event: ...\" and there is no already_green rule",
    )
    alert.add_argument("--title", required=True, help="the issue's title: one line, at most 120 characters")
    alert.add_argument(
        "--link",
        required=True,
        help="https://github.com/REPO/actions/runs/N of this repo's run the alert is about, optionally /job/N after it; "
        "the alert's own text is never sent",
    )
    check = commands.add_parser("doctor", help="check that a repo, or every repo with the pivotal topic, is set up and running")
    check.add_argument("repo", nargs="?", metavar="REPO", help="owner/name, for example 0xctx/piv-sandbox (or --all)")
    check.add_argument("--all", action="store_true", help="check every repo with the pivotal topic that you own")
    check.add_argument(
        "--probe-claude", action="store_true", help="make one paid one-token Claude API call with ANTHROPIC_API_KEY"
    )
    undo = commands.add_parser(
        "remove", help="undo init for one repo: the caller, labels, topic, variables and secrets"
    )
    undo.add_argument("repo", metavar="REPO", help="owner/name, for example 0xctx/piv-sandbox")
    undo.add_argument("--yes", action="store_true", help="delete; without it, only the plan is printed")
    undo.add_argument("--force-caller", action="store_true", help="delete the caller file even if it was edited")
    preset = commands.add_parser(
        "preset", help="set PIVOTAL_PRESET (REPO PRESET) or probe shipped presets (check NAME... | --all)"
    )
    preset.add_argument("args", nargs="*", metavar="ARG", help="REPO PRESET, or: check NAME [NAME ...]")
    preset.add_argument("--all", action="store_true", help="with check: probe every shipped preset")
    level_cmd = commands.add_parser("level", help="set the loop level (PIVOTAL_LEVEL): watch, ask, prepare or land")
    level_cmd.add_argument("repo", metavar="REPO", help="owner/name, for example 0xctx/piv-sandbox")
    level_cmd.add_argument("name", metavar="LEVEL", help="watch, ask, prepare or land")
    now = commands.add_parser("now", help="what is going on: agents working, ready, landed today")
    _repo_options(now)
    digest = commands.add_parser("digest", help="new issues, red mains, ready PRs, blocked items and the watchdog")
    _repo_options(digest)
    digest.add_argument("--json", action="store_true", help="print the digest as JSON")
    digest.add_argument(
        "--since", type=int, default=status.NEW_HOURS, metavar="HOURS",
        help=f"a new issue opened within this many hours (default {status.NEW_HOURS})",
    )
    kaggle = commands.add_parser("kaggle", help="set up or rotate the Kaggle endpoint key (once per account)")
    kaggle_commands = kaggle.add_subparsers(dest="kaggle_command", metavar="COMMAND", required=True)
    for name, text in (
        ("setup", "create the private dataset and set the endpoint secrets (once)"),
        ("rotate", "replace the endpoint key in the dataset and in the repo secrets"),
    ):
        sub = kaggle_commands.add_parser(name, help=text)
        sub.add_argument(
            "repos",
            nargs="*",
            metavar="REPO",
            help="owner/name to set the secrets on (default: your repos with the pivotal topic)",
        )
        sub.add_argument(
            "--owner",
            action="append",
            default=[],
            help="list the pivotal-topic repos of this user or organisation (repeatable); default: yours only",
        )
        if name == "rotate":
            sub.add_argument(
                "--only",
                action="store_true",
                help="accept that repos not named keep the old key (needed when REPO is given)",
            )

    runner_cmd = commands.add_parser("runner", help="run GitHub's runner on this machine, as the user pv-runner")
    actions = runner_cmd.add_subparsers(dest="action", metavar="ACTION", required=True)
    add = actions.add_parser("add", help="install persistent runner services (shows the plan; --yes does it, as root)")
    add.add_argument("target", nargs="?", metavar="ORG|OWNER/REPO", help="where to register (default: your one organization)")
    add.add_argument("--org", help="register one organization-level runner, which serves every repo in it")
    add.add_argument("--repo", help="register to one repo (a personal account's runner serves only one)")
    add.add_argument("--jobs", type=int, default=1, metavar="N", help="runner instances, one job each (default: 1)")
    add.add_argument("--name", help="runner name prefix (default: this machine's host name)")
    rem = actions.add_parser("remove", help="deregister and uninstall the runner services (shows the plan; --yes does it, as root)")
    rem.add_argument("target", nargs="?", metavar="ORG|OWNER/REPO", help="where they are registered (default: read from the runners)")
    rem.add_argument("--org", help="the organization the runners are registered to")
    rem.add_argument("--repo", help="the repo the runners are registered to")
    for sub in (add, rem):
        sub.add_argument("--dry-run", action="store_true", help="show the plan and change nothing (the default)")
        sub.add_argument("--yes", action="store_true", help="do the plan; needs root")

    return parser


def runner_command(args: argparse.Namespace, gh: Gh, executor: runner.Executor, stdout: TextIO) -> None:
    say = lambda line: print(line, file=stdout)  # noqa: E731
    named = runner.parse_target(args.target, args.org, args.repo)
    if args.action == "add":
        plan = runner.build_plan(named or runner.detect_target(gh), args.jobs, args.name, ex=executor)
    else:
        instances = executor.instances()
        left = [p for p in (runner.UNIT_PATH, f"{runner.BASE}/hooks", f"{runner.BASE}/dist", runner.BASE) if executor.exists(p)]
        if not instances and not left:
            say(f"No {runner.USER} runner is installed under {runner.BASE}: nothing to remove.")
            return
        target = runner.removal_target(named, instances, executor) if instances else named
        plan = runner.build_removal_plan(target, instances)
    if args.yes and not args.dry_run:
        runner.execute(plan, gh, executor, say)
    else:
        say(plan.render())


def main(
    argv: list[str],
    env: Mapping[str, str],
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    clock: Callable[[], datetime] = status.utc_now,
    executor: runner.Executor | None = None,
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    def say(line: str) -> None:
        print(line, file=stdout)

    try:
        gh = Gh(env.get("PIV_GH") or "gh", init.gh_environment(env))

        if args.command == "init":
            req = init.request(
                args.repo,
                check=args.check,
                setup=args.setup,
                start=args.start,
                preset=args.preset,
                level=args.level,
                release=args.release,
                update=args.update,
                engine=args.engine,
            )
            init.init(req, gh, env, say)
        elif args.command == "do":
            req = do.request(
                args.repo,
                args.text,
                line=args.line,
                image=args.image,
                score=args.score,
                target=args.target,
            )
            do.do(req, gh, say)
        elif args.command == "demo":
            return demo.run(gh, env, say, clean=args.clean)
        elif args.command == "land":
            control.land(gh, args.repo, args.issue, args.all, say)
        elif args.command == "fix":
            control.fix(gh, args.repo, args.issue, say)
        elif args.command == "stop":
            control.stop(gh, args.repo, args.all, args.named, say)
        elif args.command == "resume":
            control.resume(gh, args.named, say)
        elif args.command == "event":
            req = event.request(args.repo, kind=args.kind, title=args.title, link=args.link)
            event.send(req, gh, say)
        elif args.command == "doctor":
            return doctor.run(args.repo, args.all, gh, env, say, probe_claude=args.probe_claude)
        elif args.command == "remove":
            remove.remove(
                init.checked_repo(args.repo),
                gh,
                say,
                yes=args.yes,
                force_caller=args.force_caller,
            )
        elif args.command == "preset":
            return _handle_preset(args, env, gh, say, stdout, stderr)
        elif args.command == "level":
            var.level(args.repo, args.name, gh, say)
        elif args.command == "now":
            return status.show_now(gh, args.owner, args.repo, clock(), say)
        elif args.command == "digest":
            return status.show_digest(gh, args.owner, args.repo, clock(), args.since, args.json, say)
        elif args.command == "kaggle":
            kaggle_req = serve.request(
                env,
                args.repos,
                owners=args.owner,
                only=getattr(args, "only", False),
                rotating=args.kaggle_command == "rotate",
            )
            gh = Gh(env.get("PIV_GH") or "gh", serve.gh_environment(env))
            kaggle = serve.Kaggle(env.get("PIV_KAGGLE") or "kaggle", serve.kaggle_environment(env))
            if args.kaggle_command == "setup":
                serve.setup(kaggle_req, gh, kaggle, say)
            else:
                serve.rotate(kaggle_req, gh, kaggle, say)
        elif args.command == "runner":
            runner_command(args, gh, executor or runner.RealExecutor(), stdout)
        return 0
    except PivError as err:
        print(f"piv: {err}", file=stderr)
        return err.code


def _handle_preset(args: argparse.Namespace, env: Mapping[str, str], gh: Gh, say: Callable[[str], None],
                   stdout: TextIO, stderr: TextIO) -> int:
    """`piv preset REPO PRESET` sets PIVOTAL_PRESET (main). `piv preset check NAME [NAME...]` or `--all` probes shipped presets."""
    words = list(args.args)
    if args.all or (words and words[0] == "check"):
        names = words[1:] if words and words[0] == "check" else words
        if args.all:
            if names:
                print("piv: --all takes no names", file=stderr)
                return 2
            names = sorted(path.stem for path in (REPO_ROOT / "presets").glob("*.json"))
        return presetcheck.check_names(REPO_ROOT, names, env, stdout, stderr)
    if len(words) == 2:
        var.preset(words[0], words[1], env, gh, say)
        return 0
    print("piv: usage: piv preset REPO PRESET, or piv preset check NAME [NAME ...] or piv preset check --all", file=stderr)
    return 2
