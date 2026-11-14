"""The docs (F9): README.md, docs/QUICKSTART.md, docs/EXAMPLES.md, docs/SECURITY.md, docs/FAQ.md name only
commands that exist, run the Quickstart's commands in order, name the secrets those commands need, and mark every number that is
not measured. Every doc links to the proven table in README.md.

Nothing here runs a command or reaches the network. The commands are parsed by the real `piv` parser (gh-piv/piv/cli.py),
and `test/docs.sh` runs `--help` on each subcommand the docs name.
"""

import argparse
import ast
import contextlib
import importlib.util
import io
import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gh-piv"))
sys.path.insert(0, str(ROOT))

from engine import ack, doccheck, hill, job, lines, modelsup, plan, route, trust  # noqa: E402
from engine import report as report_job  # noqa: E402
from piv import cli, do, event, init, presetcheck, serve, status  # noqa: E402
from piv.gh import PivError  # noqa: E402

README = ROOT / "README.md"
QUICKSTART = ROOT / "docs" / "QUICKSTART.md"
EXAMPLES = ROOT / "docs" / "EXAMPLES.md"
SECURITY = ROOT / "docs" / "SECURITY.md"
FAQ = ROOT / "docs" / "FAQ.md"
DEMO_TAPE = ROOT / "docs" / "demo.tape"
DOCS = (README, QUICKSTART, EXAMPLES, SECURITY, FAQ)
FENCE = re.compile(r"^ {0,3}```(\w*)\s*$")  # a list item's block is indented up to three spaces
PIV_WORDS = re.compile(r"\bpiv ([a-z][a-z-]*)")  # a `piv` followed by a word, in prose or code
PIV_SPAN = re.compile(r"`((?:gh )?piv [^`]*)`")  # an inline code span that starts with `piv ` or `gh piv `
SHELL_SEPARATORS = ("&&", "||", ";", "|")  # a line's commands are cut here, outside quotes

# The proven table: the README's first screen and the Quickstart's top carry it; every other doc links to it, and
# docs/engines.md's "Live record" is the source of truth (the operator's 2026-11-12 notes).
PROVEN_ROW = "Proven live (run id, date)"
FROZEN_ROW = "Built and tested with fakes, not run live"
NOT_BUILT_ROW = "Not built"
ROW_HEADINGS = (PROVEN_ROW, FROZEN_ROW, NOT_BUILT_ROW)
TABLE_FILES = (README, QUICKSTART)
LINK_FILES = (EXAMPLES, SECURITY, FAQ)
TABLE_LINK = "See the proven table in [README.md](../README.md)."
ENGINES = ROOT / "docs" / "engines.md"


def fenced_bash(path: Path) -> list[str]:
    """The lines of every ```bash block in the file, in order. Other blocks (output, text) are not commands."""
    lines, inside, kind = [], False, ""
    for line in path.read_text(encoding="utf-8").splitlines():
        fence = FENCE.match(line)
        if fence and not inside:
            inside, kind = True, fence.group(1)
        elif fence and inside:
            inside = False
        elif inside and kind == "bash":
            lines.append(line)
    return lines


def prose_spans(path: Path) -> list[str]:
    """The inline code spans of the prose (outside every fence) that start with `piv `, in order."""
    spans, inside = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            inside = not inside
        elif not inside:
            spans.extend(PIV_SPAN.findall(line))
    return spans


def bash_commands(line: str) -> list[list[str]]:
    """The words of each command in one bash line: the line is cut at &&, ||, ; and | (not inside quotes), a leading
    `$ ` prompt is dropped and a trailing `#` comment is cut. A segment that is not piv is returned too: the caller checks it."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments, current = [], []
    for word in lexer:
        if word in SHELL_SEPARATORS:
            segments.append(current)
            current = []
        else:
            current.append(word)
    segments.append(current)
    return [words[1:] if words[:1] == ["$"] else words for words in segments]


WRAPPERS = ("sudo", "env", "time")  # a word that runs the command after it: `sudo --preserve-env=GH_TOKEN gh piv ...` (I-8)
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def unwrapped(words: list[str]) -> list[str]:
    """The words after each leading sudo, env or time, with that wrapper's flags and (for env) its NAME=value settings dropped."""
    while words and words[0] in WRAPPERS:
        words = words[1:]
        while words and (words[0].startswith("-") or ASSIGNMENT.match(words[0])):
            words = words[1:]
    return words


def piv_commands(path: Path, prose: bool = True) -> list[list[str]]:
    """The argv after `piv` (or `gh piv`) of each command that runs piv in a bash block, then (with prose) each inline span
    that carries arguments. A span that is only a subcommand's name, such as `piv do`, is a name: PIV_WORDS checks it.
    Bash lines come first, in order, so prose=False is the order the Quickstart runs in."""
    commands = []
    for line in fenced_bash(path):
        for words in bash_commands(line):
            words = unwrapped(words)
            if words[:2] == ["gh", "piv"]:
                commands.append(words[2:])
            elif words[:1] == ["piv"]:
                commands.append(words[1:])
    if prose:
        for span in prose_spans(path):
            words = shlex.split(span)
            if words[:2] == ["gh", "piv"]:
                words = words[1:]
            if len(words) > 2:  # `piv` and a subcommand, then at least one argument
                commands.append(words[1:])
    return commands


def subcommands() -> set[str]:
    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("the piv parser has no subcommands")


DOCS_SH = ROOT / "test" / "docs.sh"
SUBS_LINE = re.compile(r"^subs=\((.*)\)$", re.MULTILINE)


def docs_sh_subcommands(script: str) -> set[str]:
    """The words of the subs=(...) array of test/docs.sh, quotes dropped."""
    match = SUBS_LINE.search(script)
    if match is None:
        raise AssertionError("test/docs.sh has no subs=(...) line")
    return set(match.group(1).replace('"', " ").split())


def missing_from_docs_sh(script: str) -> set[str]:
    """The piv subcommands the parser has that test/docs.sh does not run --help on."""
    return subcommands() - docs_sh_subcommands(script)


def without_subcommand(script: str, name: str) -> str:
    """A mutant of test/docs.sh whose subs=(...) array lacks ``name``."""
    match = SUBS_LINE.search(script)
    words = [word for word in match.group(1).split() if word.strip('"') != name]
    return script[: match.start(1)] + " ".join(words) + script[match.end(1):]


def section(text: str, heading: str) -> str:
    """The text under one `## ` heading, up to the next one."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start: end if end != -1 else len(text)]


class DocCommandsTest(unittest.TestCase):
    def test_every_documented_piv_command_parses_with_the_real_parser(self):
        parser = cli.build_parser()
        for path in DOCS:
            for words in piv_commands(path):
                with self.subTest(file=path.name, command=words):
                    if "--help" in words:
                        continue  # `gh piv --help` names no command; docs.sh runs it
                    try:
                        with contextlib.redirect_stderr(io.StringIO()):
                            parser.parse_args(words)
                    except SystemExit as stop:
                        self.fail(f"{path.name} shows a command the parser refuses (exit {stop.code}): piv {' '.join(words)}")

    def test_the_readme_init_block_is_parsed_though_the_list_indents_it(self):
        # README step 2 is a list item, so its ```bash block is indented; a fence at column 0 never matched it.
        self.assertIn(["init", "you/my-app", "--check", "npm test"], piv_commands(README))

    def test_the_readme_prose_spans_with_arguments_are_parsed(self):
        # README step 3 names a command in a code span, not in a fence: `piv do you/my-app "add CSV export"`.
        self.assertIn(["do", "you/my-app", "add CSV export"], piv_commands(README))

    def test_the_documented_init_do_and_kaggle_setup_pass_their_own_checks(self):
        # Each request builder checks its input before any gh or kaggle call (init.request, do.request, serve.request).
        # The env is the Quickstart's exports with stand-in values, so the names and the URL shape are the docs'.
        env = {
            "KAGGLE_USERNAME": "you",
            "PIVOTAL_KAGGLE_URL": "https://pivotal-serve.ngrok-free.app",
            "NGROK_AUTHTOKEN": "a" * 40,
        }
        parser = cli.build_parser()
        seen = set()
        for path in DOCS:
            for words in piv_commands(path):
                if not words or words[0] not in ("init", "do", "kaggle") or "--help" in words:
                    continue
                args = parser.parse_args(words)
                with self.subTest(command=words):
                    if args.command == "init":
                        init.request(args.repo, check=args.check, setup=args.setup, start=args.start, preset=args.preset,
                                     level=args.level, release=args.release, update=args.update)
                    elif args.command == "do":
                        do.request(args.repo, args.text, line=args.line, image=args.image, score=args.score,
                                   target=args.target)
                    else:
                        serve.request(env, args.repos, owners=args.owner, only=getattr(args, "only", False),
                                      rotating=args.kaggle_command == "rotate")
                seen.add(args.command)
        self.assertEqual(seen, {"init", "do", "kaggle"}, "the docs no longer show each of these commands")

    def test_every_piv_word_in_the_docs_is_a_real_subcommand(self):
        known = subcommands()
        for path in DOCS:
            text = path.read_text(encoding="utf-8")
            for word in PIV_WORDS.findall(text):
                with self.subTest(file=path.name, word=word):
                    self.assertIn(word, known, f"{path.name} names `piv {word}`, which is not a piv subcommand")

    def test_each_preset_and_level_the_docs_name_is_a_real_one(self):
        # piv refuses a bad preset or level only when it runs, so the docs must name real ones (fix round 2, S-1).
        presets, levels = [], []
        for path in DOCS:
            for words in piv_commands(path):
                if "--preset" in words:
                    at = words.index("--preset")
                    self.assertLess(at + 1, len(words), f"{path.name}: --preset has no value")
                    presets.append((path.name, words[at + 1]))
                if words[:1] == ["level"] and len(words) >= 3:
                    levels.append((path.name, words[2]))
        # QUICKSTART.md no longer uses --preset (defaults to one-key)
        for name, preset in presets:
            with self.subTest(file=name, preset=preset):
                self.assertTrue((ROOT / "presets" / f"{preset}.json").is_file(), f"{name} names --preset {preset}")
        for name, level in levels:
            with self.subTest(file=name, level=level):
                self.assertIn(level, init.LEVELS, f"{name} names piv level {level}")

    def test_piv_preset_check_names_only_shipped_presets(self):  # fix round 1, S-4: it reads presets/<name>.json only
        for path in DOCS:
            for words in piv_commands(path):
                if words[:2] == ["preset", "check"]:
                    for name in words[2:]:
                        if name != "--all" and not name.isupper():  # NAME is a placeholder, as the usage lines write it
                            with self.subTest(file=path.name, name=name):
                                self.assertIn(name, preset_names(), f"{path.name} checks {name}, which is not in presets/")

    def test_docs_sh_runs_help_on_every_subcommand_the_docs_name(self):
        script = (ROOT / "test" / "docs.sh").read_text(encoding="utf-8")
        loop = re.search(r"subs=\(([a-z \"]+)\)\nfor sub in \"\$\{subs\[@\]\}\"; do", script)
        self.assertIsNotNone(loop, "test/docs.sh has no subs=(...) array looped over as \"${subs[@]}\"")
        listed = set(loop.group(1).replace('"', " ").split())
        named = {words[0] for path in DOCS for words in piv_commands(path) if words and words[0] != "--help"}
        for path in DOCS:  # inline mentions too: a command the prose names must run as well
            named |= set(PIV_WORDS.findall(path.read_text(encoding="utf-8")))
        self.assertLessEqual(named, listed, "test/docs.sh does not run --help on: " + ", ".join(sorted(named - listed)))
        self.assertLessEqual(listed, subcommands(), "test/docs.sh names a subcommand piv does not have")

    def test_docs_sh_runs_help_on_exactly_the_parsers_subcommands(self):
        # The subset check above lets a new subcommand go missing; this holds the list to cli.build_parser() both ways.
        script = DOCS_SH.read_text(encoding="utf-8")
        self.assertEqual(missing_from_docs_sh(script), set(), "test/docs.sh does not run --help on these piv subcommands")
        self.assertEqual(docs_sh_subcommands(script) - subcommands(), set(), "test/docs.sh names a subcommand piv does not have")

    def test_a_mutant_that_drops_any_one_subcommand_from_docs_sh_is_caught(self):
        script = DOCS_SH.read_text(encoding="utf-8")
        self.assertGreaterEqual(len(subcommands()), 16)
        for name in sorted(subcommands()):
            with self.subTest(dropped=name):
                mutant = without_subcommand(script, name)
                # The delta: the mutant adds exactly this name to what is missing, whatever the real list lacks.
                added = missing_from_docs_sh(mutant) - missing_from_docs_sh(script)
                self.assertEqual(added, {name}, "the check does not catch a dropped subcommand")


class ParserTest(unittest.TestCase):
    """piv_commands reads each command a line runs, not only a line that starts with piv (fix round 2, I-4): one fixture per shape."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def parsed(self, body: str, prose: bool = True) -> list[list[str]]:
        path = self.dir / "fixture.md"
        path.write_text(body, encoding="utf-8")
        return piv_commands(path, prose=prose)

    def test_a_piv_command_after_and_and_is_parsed(self):
        self.assertIn(["doctor", "you/my-app", "--chek"], self.parsed("```bash\ncd my-app && piv doctor you/my-app --chek\n```\n"))

    def test_a_piv_command_after_each_other_shell_operator_is_parsed(self):
        for line in ("piv now || piv doctor you/my-app --chek", "piv now; piv doctor you/my-app --chek",
                     "piv now | piv doctor you/my-app --chek"):
            with self.subTest(line=line):
                self.assertIn(["doctor", "you/my-app", "--chek"], self.parsed(f"```bash\n{line}\n```\n"))

    def test_a_dollar_prompt_is_dropped_before_the_command(self):
        self.assertIn(["doctor", "you/my-app", "--chek"], self.parsed("```bash\n$ piv doctor you/my-app --chek\n```\n"))

    def test_a_gh_piv_command_after_a_prompt_or_an_operator_is_parsed(self):
        self.assertIn(["doctor", "you/my-app", "--chek"], self.parsed("```bash\n$ cd x && gh piv doctor you/my-app --chek\n```\n"))

    def test_an_inline_gh_piv_span_with_arguments_is_parsed(self):
        self.assertIn(["now", "--chek", "x"], self.parsed("Run `gh piv now --chek x` to see it.\n"))

    def test_a_shell_operator_inside_quotes_does_not_split_a_command(self):
        self.assertIn(["do", "you/my-app", "a && b; c"], self.parsed('```bash\npiv do you/my-app "a && b; c"\n```\n'))

    def test_a_non_piv_command_after_an_operator_is_not_a_piv_command(self):
        self.assertEqual(self.parsed("```bash\ncd my-app && npm test\n```\n"), [])

    def test_a_gh_piv_command_after_sudo_and_its_flags_is_parsed(self):  # fix round 1, I-8: RECIPES' runner install
        self.assertIn(["runner", "add", "--org", "your-org", "--jobs", "2", "--yes"],
                      self.parsed("```bash\nsudo --preserve-env=GH_TOKEN,HOME gh piv runner add --org your-org --jobs 2 --yes\n```\n"))

    def test_env_and_time_before_gh_piv_are_skipped_with_their_settings(self):  # fix round 1, I-8
        self.assertIn(["now", "--chek", "x"], self.parsed("```bash\nenv GH_TOKEN=abc gh piv now --chek x\n```\n"))
        self.assertIn(["doctor", "you/my-app", "--chek"], self.parsed("```bash\ntime -p gh piv doctor you/my-app --chek\n```\n"))

    def test_a_gh_piv_span_that_names_only_a_subcommand_is_a_name_not_a_command(self):
        self.assertEqual(self.parsed("Run `gh piv now` first.\n"), [])


class DocsScriptTest(unittest.TestCase):
    def test_a_failing_step_stops_the_run_and_names_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test").mkdir()
            (root / "test" / "docs.sh").write_text(DOCS_SH.read_text(encoding="utf-8"), encoding="utf-8")
            (root / "spikes").mkdir()
            (root / "spikes" / "headless_probe.sh").write_text("echo probe broke\nexit 3\n", encoding="utf-8")
            done = subprocess.run(["bash", str(root / "test" / "docs.sh")], capture_output=True, text=True, timeout=120,
                                  check=False)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn('docs: FAILED at the step "headless probe"', done.stderr)
        self.assertIn("probe broke", done.stderr, "the failing step's own output is not shown")
        self.assertNotIn("docs ok", done.stdout)

    def test_every_command_in_docs_sh_runs_through_a_named_step(self):
        # A bare python3, bash or grep line would fail without its step's name, so each one must sit in step "NAME" ....
        for number, line in enumerate(DOCS_SH.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*(python3|bash|grep)\b", line):
                self.fail(f"test/docs.sh line {number} runs a command outside step: {line.strip()}")


class OperatorDocsTest(unittest.TestCase):
    def test_agents_md_lists_the_docs_gate_and_says_what_its_probe_needs(self):
        text = flat(AGENTS.read_text(encoding="utf-8"))
        self.assertIn("`bash test/docs.sh`", text)
        self.assertNotIn("joins the gate once docs exist", text)
        self.assertIn("`not installed`", text, "AGENTS.md does not say the headless probe passes without claude")
        self.assertNotIn("needs `claude`", text)

    def test_the_start_variable_is_written_and_no_workflow_reads_it(self):
        self.assertIn(init.VAR_START, init.VARIABLES)
        for path in [*sorted((ROOT / ".github" / "workflows").glob("*.yml")), ROOT / "templates" / "caller.yml"]:
            self.assertNotIn("PIVOTAL_START", path.read_text(encoding="utf-8"), path.name)

    def test_a_custom_line_label_never_triggers_and_only_the_four_built_in_lines_are_admitted(self):
        self.assertIsNone(trust.PIVOTAL_LABEL.fullmatch("pivotal:api-only"))
        self.assertEqual(set(init.LINES), {"build", "incident", "mirror", "docs"})


class QuickstartTest(unittest.TestCase):
    def test_the_quickstart_runs_its_piv_commands_in_order(self):
        names = [words[0] for words in piv_commands(QUICKSTART, prose=False) if words and words[0] != "--help"]
        self.assertEqual(names, ["init", "doctor", "do", "now", "land", "remove"])

    def test_the_quickstart_names_every_secret_its_steps_need(self):
        text = QUICKSTART.read_text(encoding="utf-8")
        known = set(init.SECRETS) | {serve.NGROK_TOKEN, serve.KAGGLE_USERNAME, serve.URL_SECRET}
        exported = re.findall(r"^export ([A-Z_]+)=", text, re.MULTILINE)
        self.assertTrue(exported, "the Quickstart exports no variables")
        self.assertLessEqual(set(exported), known, "the Quickstart exports a name piv does not read")
        for name in ("OPENROUTER_API_KEY",):
            self.assertIn(name, text)

    def test_the_quickstart_uses_a_preset_and_a_check_for_init(self):
        init_words = [w for w in piv_commands(QUICKSTART, prose=False) if w and w[0] == "init"]
        self.assertEqual(len(init_words), 1)
        self.assertIn("--check", init_words[0])
        self.assertNotIn("--preset", init_words[0])
        self.assertTrue((ROOT / "presets" / "one-key.json").is_file())

    def test_the_quickstart_carries_the_proven_table_and_says_its_times_are_not_measured(self):
        text = QUICKSTART.read_text(encoding="utf-8")
        self.assertEqual([row[0] for row in table_rows(text)], list(ROW_HEADINGS))
        self.assertIn("Not measured yet", text)

    def test_the_fifteen_minute_acceptance_is_a_target_and_not_claimed_as_measured(self):
        # The acceptance "a fresh reader finishes the Quickstart in <= 15 minutes" is the operator's live measurement.
        text = flat(QUICKSTART.read_text(encoding="utf-8"))
        self.assertIn("finish this Quickstart in 15 minutes or less", text)
        self.assertIn("a target, not a measurement", text)
        self.assertIn("**Not measured yet**", text)
        self.assertNotRegex(text, r"finished in \d+ minutes?|took \d+ minutes?")


class ReadmeTest(unittest.TestCase):
    def setUp(self):
        self.text = README.read_text(encoding="utf-8")

    def test_the_hero_line_is_there(self):  # mutant: the old "dark factory" tagline restored is red (the screen's own pins hold it)
        self.assertTrue(self.text.startswith("# Pivotal ⚡"))
        self.assertNotIn("dark factory", flat(self.text.split("\n## ", 1)[0]))
        self.assertIn('Label an issue; a model builds it in a fresh GitHub Actions machine, another company\'s model reviews '
                      'it, your tests gate it, and you say "land it". No server, no database, no AI deciding.',
                      flat(self.text.split("\n## ", 1)[0]))

    def test_the_gif_slot_is_marked_as_not_recorded(self):  # the first screen lists what is proven (the outline's step 6); the GIF is
        # recorded by the coordinator after LIVE 3.1, so the README says so in the live record's terms
        self.assertIn("after LIVE 3.1", flat(self.text))

    def test_there_are_three_steps(self):
        steps = [line for line in section(self.text, "## Three steps").splitlines() if re.match(r"^\d\. ", line)]
        self.assertEqual(len(steps), 3, steps)

    def test_the_ladder_names_all_four_levels(self):
        ladder = section(self.text, "## The ladder")
        for level in ("`watch`", "`ask`", "`prepare`", "`land`"):
            self.assertIn(level, ladder)
        self.assertIn("**near the loop (default)**", ladder)

    def test_the_status_line_wording_is_the_one_piv_now_prints(self):
        self.assertIn("agents working for you right now · ", self.text)
        self.assertIn("ready for you · ", self.text)
        self.assertIn("landed today (UTC)", self.text)

    def test_safety_and_costs_sections_exist(self):
        self.assertIn("## Safety, in plain words", self.text)
        self.assertIn("## Costs", self.text)

    def test_the_idle_line_does_not_say_no_server_runs_and_names_the_kaggle_session(self):  # I1 and I12, fix round 1
        costs = flat(section(self.text, "## Costs"))
        self.assertNotIn("No server runs", costs)
        self.assertIn("A Kaggle session that a run started can stay up after the run", costs)

    def test_costs_state_no_measured_number(self):
        costs = section(self.text, "## Costs")
        self.assertIn("Not measured yet", costs)
        # The only figure is Kaggle's quota, which the plan records and which Kaggle sets; the rest are design facts.
        self.assertEqual(re.findall(r"\d[\d.,]*", costs), ["30"], costs)

    def test_no_dollar_amount_appears_in_the_docs(self):
        for path in DOCS:
            self.assertIsNone(re.search(r"\$\d", path.read_text(encoding="utf-8")), path.name)

    def test_the_presets_it_names_ship(self):
        presets = section(self.text, "## Presets")
        for name in ("free-kaggle", "claude-sub", "one-key"):
            self.assertIn(f"`{name}`", presets)
            self.assertTrue((ROOT / "presets" / f"{name}.json").is_file(), name)

    def test_each_incident_and_event_trigger_row_says_it_is_built_and_not_run(self):
        rows = [line for line in section(self.text, "## Triggers").splitlines() if line.startswith(("| Red CI", "| A webhook"))]
        self.assertEqual(len(rows), 2, rows)
        for row in rows:
            self.assertIn("built, not yet run on real github", row.lower(), row)

    def test_demo_fake_is_never_shown_as_working(self):
        for path in DOCS:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "--fake" in line:
                    self.assertIn("not built", line, f"{path.name} names --fake without saying it is not built: {line!r}")


PLACEHOLDER = "Not measured yet"
# Each docs file's placeholders, pinned: a replaced one is a number somebody measured or guessed, and must say so.
PLACEHOLDER_PHRASES = {
    README: ("**Measured:** **Not measured yet.**",),
    QUICKSTART: ("**Not measured yet**, the first fresh reader's run fills them in.",),
    SECURITY: (),
    FAQ: (
        "How many minutes one run takes: **Not measured yet: the operator's live run fills this in.**",
        "How many GPU-hours one run takes: **Not measured yet: the operator's live run fills this in.**",
        "The cost per run: **Not measured yet: the operator's live run fills this in.**",
        "The time from a start to a healthy endpoint is **Not measured yet: the operator's live run fills this in.**",
    ),
    EXAMPLES: (),
}
# A number with a time or cost unit, and at most one word between: "6 minutes", "about 6 minutes", "20 idle minutes", "3-10 min",
# "6-minute builds", "2h" (a single-letter h, m or s counts only as a whole word; fix round 2, S-2).
# A timed number: digits, or a number word with a space or hyphen before its unit (fix round 1, S-8: "ten minutes"; "a" and "an"
# are articles, so they are not numbers), or a currency symbol with an amount ("€2", "$5"). The unit is a whole word.
UNITS = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|GPU-hours?|dollars?|cents?|USD|[hms])\b"
NUMBER_WORD = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|ninety|hundred)"
TIMED = re.compile(r"\b\d[\d.,]*(?:\s*-\s*\d[\d.,]*)?\s*-?\s*(?:[A-Za-z-]+\s+)?" + UNITS
                   + r"|\b" + NUMBER_WORD + r"(?:\s*-\s*|\s+)(?:[A-Za-z-]+\s+)?" + UNITS
                   + r"|[€£$]\s?\d[\d.,]*", re.IGNORECASE)
# Every timed number the docs may show. Each is a fact the code or the plan sets, not a measurement: the session limits
# (kaggle/lifecycle.py IDLE_S and HARD_S, tied below), Kaggle's quota (the plan), and the 15-minute target.
ALLOWED_TIMED = {
    README: {"20 idle minutes", "11 hours", "30 GPU-hours", "15 minutes"},
    QUICKSTART: {"15 minutes"},
    SECURITY: {"20 idle minutes", "11 hours"},
    FAQ: {"20 idle minutes", "11 hours", "30 GPU-hours", "15 minutes"},  # "15 minutes" is the Quickstart's own target
    EXAMPLES: {"15 minutes", "20 idle minutes", "11 hours"},
}


def timed_numbers(path: Path) -> list[str]:
    """Each timed number in the file, with its whitespace (a line break inside one) made one space."""
    return [" ".join(match.group(0).split()) for match in TIMED.finditer(path.read_text(encoding="utf-8"))]


def timed_in(text: str) -> list[str]:
    return [" ".join(match.group(0).split()) for match in TIMED.finditer(text)]


class TimedPatternTest(unittest.TestCase):
    def test_a_hyphenated_number_and_unit_is_timed(self):  # fix round 2, S-2
        self.assertEqual(timed_in("the runs take 6-minute builds"), ["6-minute"])

    def test_a_single_letter_unit_is_timed_only_as_a_whole_word(self):
        self.assertEqual(timed_in("a build can take 2h"), ["2h"])
        self.assertEqual(timed_in("a build can take 3 m"), ["3 m"])
        self.assertEqual(timed_in("the 2 hosts and 3 mirrors"), [])

    def test_a_number_word_and_unit_is_timed(self):  # fix round 1, S-8
        self.assertEqual(timed_in("the build takes ten minutes, or one hour"), ["ten minutes", "one hour"])
        self.assertEqual(timed_in("a second run, as ones and Actions minutes"), [], "articles and words that end in s are not timed")

    def test_a_currency_symbol_and_amount_is_timed(self):  # fix round 1, S-8
        self.assertEqual(timed_in("about €2 or $5 a run"), ["€2", "$5"])

    def test_the_allowed_numbers_still_match(self):
        self.assertEqual(timed_in("20 idle minutes, 11 hours and 15 minutes"), ["20 idle minutes", "11 hours", "15 minutes"])


class MeasuredTest(unittest.TestCase):
    def test_each_docs_file_has_exactly_its_placeholders(self):
        for path, phrases in PLACEHOLDER_PHRASES.items():
            text = path.read_text(encoding="utf-8")
            flat = " ".join(text.split())  # a phrase may wrap across a line
            with self.subTest(file=path.name):
                self.assertEqual(text.count(PLACEHOLDER), len(phrases), f"{path.name} has {text.count(PLACEHOLDER)} placeholders")
                for phrase in phrases:
                    self.assertIn(phrase, flat)

    def test_no_timed_number_is_shown_except_the_allowed_facts(self):
        for path in DOCS:
            with self.subTest(file=path.name):
                shown = set(timed_numbers(path))
                self.assertEqual(sorted(shown - ALLOWED_TIMED[path]), [], f"{path.name} shows a measured-looking number")
                self.assertLessEqual(ALLOWED_TIMED[path], shown, "an allowed number is no longer in the file: remove it from the list")

    def test_the_allowed_session_limits_are_the_lifecycle_constants(self):
        spec = importlib.util.spec_from_file_location("lifecycle", ROOT / "kaggle" / "lifecycle.py")
        lifecycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lifecycle)
        self.assertEqual(lifecycle.IDLE_S, 20 * 60, "README says 20 idle minutes")
        self.assertEqual(lifecycle.HARD_S, 11 * 3600, "README says 11 hours")


AGENTS = ROOT / "AGENTS.md"


def flat(text: str) -> str:
    """The text with every run of whitespace made one space, so a phrase may wrap across lines."""
    return " ".join(text.split())


def faq_answer(text: str, question: str) -> str:
    """The answer under one bold question of the FAQ: from its line to the next question or `## ` heading."""
    start = text.index(f"**{question}**") + len(f"**{question}**")
    ends = [i for i in (text.find("\n**", start), text.find("\n## ", start)) if i != -1]
    return flat(text[start: min(ends) if ends else len(text)])


def caller_secrets(job: str) -> set[str]:
    """The names a job of templates/caller.yml passes as secrets (the piv init writes this file)."""
    text = (ROOT / "templates" / "caller.yml").read_text(encoding="utf-8")
    block = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  \w)", text, re.MULTILINE | re.DOTALL)
    assert block, f"templates/caller.yml has no {job} job"
    return set(re.findall(r"^      ([A-Z_]+): \$\{\{ secrets\.", block.group(1), re.MULTILINE))


def table_block(text: str) -> list[str]:
    """The first Markdown table in the text, as its lines (a header, a separator, then its rows)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("|")), None)
    assert start is not None, "the text has no table"
    end = start
    while end + 1 < len(lines) and lines[end + 1].lstrip().startswith("|"):
        end += 1
    return lines[start:end + 1]


def table_rows(text: str) -> list[list[str]]:
    """The cells of each data row (the header and the separator are dropped) of the first table in the text."""
    return [[cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")] for line in table_block(text)[2:]]


def run_ids(text: str) -> list[str]:
    """Every run id (nine or more digits) in the text."""
    return re.findall(r"\b\d{9,}\b", text)


class ProvenTableTest(unittest.TestCase):
    """The README and the Quickstart carry the three-row proven table; every other doc links to it, and the run ids in the
    README's first row are each in docs/engines.md's Live record (the operator's 2026-11-12 notes)."""

    def test_the_table_is_under_twelve_lines(self):  # mutant: a twelve-line table is red
        for path in TABLE_FILES:
            with self.subTest(file=path.name):
                self.assertLess(len(table_block(path.read_text(encoding="utf-8"))), 12)

    def test_both_files_carry_the_three_row_headings(self):  # mutant: a dropped or renamed row heading is red
        for path in TABLE_FILES:
            with self.subTest(file=path.name):
                self.assertEqual([row[0] for row in table_rows(path.read_text(encoding="utf-8"))], list(ROW_HEADINGS))

    def test_the_readmes_first_row_run_ids_are_in_the_engines_live_record(self):  # mutant: an invented run id in the first row is red
        first = table_rows(README.read_text(encoding="utf-8"))[0]
        ids = run_ids(" ".join(first))
        self.assertTrue(ids, first)
        recorded = set(run_ids(section(ENGINES.read_text(encoding="utf-8"), "## Live record")))
        for run_id in ids:
            self.assertIn(run_id, recorded, f"the README's first row names {run_id}, which the Live record does not")

    def test_the_engines_live_record_has_a_row_per_run(self):  # mutant: a dropped live-record row is red
        block = table_block(section(ENGINES.read_text(encoding="utf-8"), "## Live record"))
        self.assertIn("Run id", block[0])
        self.assertIn("Date", block[0])
        rows = table_rows("\n".join(block))
        for run_id in run_ids(README.read_text(encoding="utf-8")):
            self.assertTrue(any(row and row[0] == run_id for row in rows), run_id)

    def test_every_other_file_links_to_the_proven_table(self):  # mutant: the old block restored in place of the link is red
        for path in LINK_FILES:
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(TABLE_LINK, flat(text))
                self.assertNotIn("**What has run live**", text)

    def test_no_file_still_carries_the_old_live_block(self):  # mutant: the old block restored in any guide is red
        for path in doccheck.markdown_files(ROOT):
            if path.name == "CHANGELOG.md":  # its bullets are history, and the new bullet is checked above
                continue
            with self.subTest(file=str(path)):
                self.assertNotIn("**What has run live**", path.read_text(encoding="utf-8"))

    def test_no_file_says_nothing_has_run_on_real_github_outside_the_changelog(self):  # mutant: one old banner restored is red
        # The old banners, in any of their wordings: "Nothing has run", "Nothing here has run", "Nothing in this guide has run".
        old = re.compile(r"Nothing (?:here |in this guide )?has run on real GitHub")
        for path in doccheck.markdown_files(ROOT):
            if path.name == "CHANGELOG.md":  # its bullets are history, and the new bullet is checked above
                continue
            with self.subTest(file=str(path)):
                self.assertIsNone(old.search(path.read_text(encoding="utf-8")))


def live_record_run_ids() -> set[str]:
    """Every run id in the `Run id` column of docs/engines.md's Live record: a run the record shows, not one it only mentions."""
    rows = table_rows("\n".join(table_block(section(ENGINES.read_text(encoding="utf-8"), "## Live record"))))
    return {row[0] for row in rows if row and row[0] in run_ids(row[0])}


def screen_commands(text: str) -> list[list[str]]:
    """Every `piv` command of the bash blocks in one string, with the `piv` word dropped (piv_commands' own shape)."""
    return [words[1:] for words in (shlex.split(line) for line in fenced_bash_lines(text)) if words[:1] == ["piv"]]


def fenced_bash_lines(text: str) -> list[str]:
    """The lines of every ```bash block in one string (fenced_bash reads a file)."""
    lines, inside, kind = [], False, ""
    for line in text.splitlines():
        fence = FENCE.match(line)
        if fence and not inside:
            inside, kind = True, fence.group(1)
        elif fence and inside:
            inside = False
        elif inside and kind == "bash":
            lines.append(line)
    return lines


class FirstScreenTest(unittest.TestCase):
    """README.md's first screen (07-launch-narrative.md, "The first screen"): the one-sentence pitch, the proven table, the
    install line, three commands, the free path as the on-ramp, and the live record. The contract's four pins are here: the
    install line, the three commands, the table, and a link to a recorded run. Each is red against the mutant named on its line."""

    def setUp(self):
        self.text = README.read_text(encoding="utf-8")
        self.screen = self.text.split("\n## The ladder", 1)[0]

    def test_the_screen_starts_with_the_pitch_sentence(self):  # mutant: the "dark factory" tagline restored is red
        text = flat(self.screen)
        self.assertIn('Label an issue; a model builds it in a fresh GitHub Actions machine, another company\'s model reviews '
                      'it, your tests gate it, and you say "land it". No server, no database, no AI deciding.', text)
        self.assertNotIn("dark factory", text)

    def test_the_install_line_is_the_published_extension(self):  # mutant: the install line removed from the screen is red
        self.assertIn("gh extension install 0xctx/gh-piv", self.screen)

    def test_the_three_commands_are_in_the_screen(self):  # mutant: one of init, do or land dropped from the screen is red
        fenced = screen_commands(self.screen)
        self.assertIn(["init", "you/my-app", "--check", "npm test"], fenced)
        self.assertIn(["do", "you/my-app", "add CSV export"], fenced)
        self.assertIn(["land", "you/my-app"], fenced)
        for words in fenced:
            if words[:1] == ["extension"]:
                self.assertEqual(words, ["extension", "install", "0xctx/gh-piv"], words)

    def test_the_free_path_is_the_on_ramp_and_the_paid_path_is_named(self):  # mutant: the on-ramp sentence dropped is red
        text = flat(self.screen)
        self.assertIn("Kaggle's GPUs and a free Gemini key", text)
        self.assertIn("the on-ramp and the demo", text)
        self.assertIn("`claude-sub`", text)

    def test_the_table_is_in_the_screen_and_links_a_recorded_run(self):  # mutant: an invented run id in the screen's first row is red
        rows = table_rows("\n".join(table_block(self.screen)))
        self.assertEqual([row[0] for row in rows], list(ROW_HEADINGS))
        first = rows[0]
        ids = run_ids(" ".join(first))
        self.assertTrue(ids, first)
        recorded = live_record_run_ids()
        for run_id in ids:
            self.assertIn(run_id, recorded, f"the README's first row names {run_id}, which the Live record does not")

    def test_the_screen_links_the_live_record(self):  # mutant: the live-record link replaced by a bare word is red
        self.assertIn("(docs/engines.md#live-record)", self.screen)

    def test_the_screen_claims_no_run_the_live_record_does_not_show(self):  # mutant: "the review ran live" added to the screen is red
        # The only runs the screen may name are the run ids the Live record's Run id column carries; docs-proven-table's pin
        # holds the first row, this one every id in the screen.
        recorded = live_record_run_ids()
        for run_id in run_ids(self.screen):
            self.assertIn(run_id, recorded, f"the screen names run {run_id}, which the Live record does not")
        text = flat(self.screen).lower()
        for claim in ("the review ran live", "the review ran on real github", "landed on real github",
                      "published on real github", "assembled on real github"):
            self.assertNotIn(claim, text)


class ClaimsTest(unittest.TestCase):
    """The docs say what the code does: each claim below is one a reviewer checked against the code (fix round 1)."""

    def setUp(self):
        self.readme = README.read_text(encoding="utf-8")
        self.quick = QUICKSTART.read_text(encoding="utf-8")

    def test_the_incident_and_event_triggers_are_built_and_not_run_live(self):
        triggers = flat(section(self.readme, "## Triggers")).lower()
        self.assertIn("built, not yet run on real github", triggers)
        self.assertNotIn("built before you check in", flat(self.readme))
        self.assertIn("`--kind` is required but informational", triggers)

    def test_the_red_ci_row_names_the_workflow_the_caller_filters_on(self):
        # The caller's workflow_run filter is the one name in templates/caller.yml, and piv init never rewrites it (fix round 2).
        caller = (ROOT / "templates" / "caller.yml").read_text(encoding="utf-8")
        names = re.findall(r"^    workflows: \[([^\]]*)\]", caller, re.MULTILINE)
        self.assertEqual(names, ["CI"], "templates/caller.yml no longer filters on the workflow named CI")
        rows = [line for line in section(self.readme, "## Triggers").splitlines() if line.startswith("| Red CI")]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("a failed run of the workflow named `CI`", rows[0])
        self.assertIn("A repo whose CI workflow has another name gets none.", rows[0])
        # One open incident per workflow (ack files it; a second red run of the same workflow updates it), not one per failure.
        self.assertIn("files an `incident` issue, or updates the open one for the same workflow, with a title and a link", rows[0])

    def test_the_cost_line_does_not_claim_each_cli_reports_its_cost(self):  # S-5
        self.assertNotIn("reports its own cost", flat(section(self.readme, "## Costs")))

    def test_the_quickstart_says_init_does_not_refuse_without_a_preset(self):  # S-6
        text = flat(self.quick)
        self.assertNotIn("It is required too", text)
        self.assertIn("No `--preset` is needed: `piv init` defaults to the shipped `one-key` preset", text)

    def test_rotate_sets_the_key_on_your_repos_and_names_owner_for_an_organisation(self):  # S-7
        # The Quickstart no longer documents piv kaggle rotate (one-key preset doesn't need Kaggle).
        # This test is kept for the README which still mentions it.
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("sets the new key on every repo with the `pivotal` topic", text)
        self.assertIn("sets the new key on your repos with the `pivotal` topic", text)
        self.assertIn("`--owner ORG`", text)

    def test_the_no_release_step_names_the_release_flag_and_the_way_out(self):  # S-8
        text = flat(self.quick)
        self.assertIn('stops with "has no release yet"', text)
        self.assertIn("Pass `--release` with a commit SHA of `0xctx/pivotal`", text)

    def test_the_docs_row_names_the_misspelling_list_and_not_full_spelling(self):  # S-10
        rows = [line for line in self.readme.splitlines() if line.startswith("| `docs`")]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("a short list of misspellings and doubled words (not full spelling)", rows[0])

    def test_the_quickstart_says_a_moved_base_is_not_re_gated_by_the_pipeline(self):
        text = flat(self.quick)
        self.assertNotIn("re-runs the gate before it lands", text)
        self.assertIn("Pivotal does not push", text)
        self.assertIn("/pivotal go trusted", text)

    def test_an_untrusted_run_gets_only_the_secrets_the_caller_passes_it(self):
        names = caller_secrets("factory-untrusted")
        self.assertEqual(names, {"GEMINI_API_KEY", "GROQ_API_KEY", "PIVOTAL_KAGGLE_URL", "PIVOTAL_KAGGLE_KEY"})
        safety = flat(section(self.readme, "## Safety, in plain words"))
        for name in names:
            self.assertIn(name, safety, f"the safety section does not say that an untrusted run gets {name}")
        self.assertIn("PIVOTAL_UNTRUSTED_PRESET", safety)
        self.assertNotIn("Strangers can't spend your keys", safety)

    def test_a_non_writers_comment_does_nothing_and_the_untrusted_preset_is_named(self):
        triggers = flat(section(self.readme, "## Triggers"))
        self.assertNotIn("Anyone else's comment does nothing, or runs", triggers)
        self.assertIn("A comment from anyone without write access does nothing", triggers)
        self.assertIn("PIVOTAL_UNTRUSTED_PRESET", triggers)

    def test_a_label_is_decided_by_the_issues_author_not_by_whoever_applied_it(self):
        # trust.py decides a labeled issue on its author's permission (fix round 2, I-3); anyone who can label can start one.
        triggers = flat(section(self.readme, "## Triggers"))
        self.assertIn("A label is decided by the issue's author, not by whoever applied it", triggers)
        self.assertIn("anyone who can apply labels (triage or write) can start a run on an issue whose author has write access",
                      triggers)
        # Fix round 2, F6: nothing checks the issue's state (engine/job.py read_issue, engine/trust.py), so "open" was false.
        self.assertNotIn("on an open issue", triggers)
        self.assertIn("A labelled or commented closed issue also starts a run, and closing an issue does not stop a labelled run: "
                      "use `piv stop` or `piv level`.", triggers)
        self.assertNotIn("Only people with write access can start work", flat(self.readme))

    def test_the_ladder_does_not_say_only_you_start_work(self):
        ladder = flat(section(self.readme, "## The ladder"))
        self.assertNotIn("only you: a label", ladder)
        self.assertNotIn("you, plus a red CI run", ladder)

    def test_an_untrusted_run_is_passed_the_free_keys_and_used_only_with_the_untrusted_preset(self):
        # The caller passes the four names to every untrusted run; plan reads PIVOTAL_UNTRUSTED_PRESET and goes red without it (fix round 2, S-4).
        triggers = flat(section(self.readme, "## Triggers"))
        self.assertIn("Every such run is passed the free keys and the Kaggle endpoint", triggers)
        self.assertIn("used only once you set `PIVOTAL_UNTRUSTED_PRESET`", triggers)
        self.assertIn("without it, a stranger's issue goes red at plan", triggers)
        safety = flat(section(self.readme, "## Safety, in plain words"))
        self.assertIn("are used only once you set `PIVOTAL_UNTRUSTED_PRESET`", safety)

    def test_unclear_means_not_landed_not_a_draft(self):
        safety = flat(section(self.readme, "## Safety, in plain words"))
        self.assertIn("Unclear means not landed", safety)
        self.assertNotIn("Unclear means a draft", safety)

    def test_a_protected_branch_is_refused_then_merged_by_land_it_through_the_api_pinned_to_the_sealed_commit(self):
        ladder = flat(section(self.readme, "## The ladder"))
        self.assertNotIn("a protected branch gets a pull request instead", ladder)
        self.assertNotIn("on a protected branch it merges the pull request", ladder)
        self.assertIn("on a protected branch the push is refused and the pipeline opens a ready stack pull request; "
                      "`land it` (`piv land`) merges it through the API, pinned to the sealed commit", ladder)

    def test_only_plan_build_and_gate_follow_the_runner_choice_and_the_docs_say_so(self):  # I4: derived from factory.yml
        jobs = job_blocks(FACTORY.read_text(encoding="utf-8"))
        runs_on = {job: re.search(r"^    runs-on: (.*)$", block, re.MULTILINE).group(1) for job, block in jobs.items()}
        self_hosted = {job for job, value in runs_on.items() if "outputs.runner" in value}
        self.assertEqual(self_hosted, {"plan", "build", "gate"}, runs_on)
        self.assertEqual(set(runs_on) - self_hosted, {"ack", "models-up", "review", "assemble", "publish", "report"})
        for doc in (README, FAQ):
            self.assertIn("runs only plan, build and gate", flat(doc.read_text(encoding="utf-8")), doc.name)

    def test_the_draft_reason_is_quoted_as_the_report_prints_it(self):
        report = (ROOT / "engine" / "report.py").read_text(encoding="utf-8")
        quoted = "open the draft PR and read the review verdict before deciding"
        self.assertIn(quoted, report)
        self.assertIn(quoted, flat(self.quick))
        self.assertNotIn("verdict needs fixes", self.quick)

    def test_the_sample_output_prints_the_issue_before_its_label(self):
        block = flat(section(self.quick, "## 5. File the task"))
        self.assertLess(block.index("issue created: "), block.index("label pivotal added"))
        self.assertLess(block.index("piv do you/my-app: line build"), block.index("issue created: "))

    def test_fix_starts_a_new_run_from_the_issue_not_from_the_draft(self):
        self.assertIn("starts a new run from the issue's own text, not from the draft", flat(self.quick))

    def test_clean_up_names_the_provider_and_kaggle_secrets_init_set(self):
        clean = flat(section(self.quick, "## Clean up"))
        self.assertIn("provider keys", clean)
        self.assertIn("Kaggle credentials", clean)

    def test_step_one_names_the_repo_only_once_it_exists(self):
        steps = section(self.readme, "## Three steps")
        first = flat(steps.split("\n2. ", 1)[0])
        self.assertIn("The Quickstart makes a demo repo and runs without Kaggle", first)

    def test_the_pivotal_line_labels_are_the_four_the_trust_job_admits(self):
        self.assertIn("`pivotal:build`, `pivotal:incident`, `pivotal:mirror` or `pivotal:docs`", flat(self.readme))
        self.assertNotIn("`pivotal:<line>` for another line", self.readme)
        for label in ("pivotal:build", "pivotal:incident", "pivotal:mirror", "pivotal:docs"):
            self.assertIsNotNone(trust.PIVOTAL_LABEL.fullmatch(label), label)
        self.assertIsNone(trust.PIVOTAL_LABEL.fullmatch("pivotal:custom"))

    def test_the_quickstart_says_which_commands_are_parsed(self):
        self.assertIn("parses each one in a bash block or a code span", flat(self.quick))

    def test_free_models_are_named_beside_the_paid_agents(self):  # the first screen names the on-ramp and the paid path (the outline's step 5)
        screen = flat(self.readme.split("\n## The ladder", 1)[0])
        self.assertIn("Kaggle's GPUs and a free Gemini key", screen)
        self.assertIn("`claude-sub`", screen)

    def test_a_workflow_dispatch_starts_nothing(self):
        # workflow_dispatch is listed in the caller but trust.resolve returns "none" for it (no handler in _trigger).
        self.assertEqual(trust.resolve("workflow_dispatch", {}, "you/my-app")[2], "none")


CALLER = ROOT / "templates" / "caller.yml"
FACTORY = ROOT / ".github" / "workflows" / "factory.yml"
LAND_WORKFLOW = ROOT / ".github" / "workflows" / "land.yml"
JOB_KEY = re.compile(r"(?m)^  ([a-z][a-z-]*):\n")  # a job's key: two spaces, then the name (nested keys have more)
PERMISSION = re.compile(r"([a-z-]+): (read|write)")
SECRET_NAME = re.compile(r"`?\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b`?")  # backticked or bare (fix round 2, S12): a name has an underscore
TOKEN_IN_A_STEP = re.compile(r"GH_TOKEN|github\.token")

PERMISSIONS_BLOCK = re.compile(r"(?m)^    permissions:\n((?:      (?:#[^\n]*|[a-z-]+: \S+)\n)+)")  # job level; comments may sit inside


def job_blocks(text: str) -> dict[str, str]:
    """Each job of a workflow file, by name: its text from its key to the next job's key."""
    parts = JOB_KEY.split("\n" + text.split("\njobs:\n", 1)[1])
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def job_facts(block: str):
    """A job's (permissions, secret names, has a GitHub token in a step), or None when it has no permissions block."""
    found = PERMISSIONS_BLOCK.search(block)
    if not found:
        return None
    return set(PERMISSION.findall(found.group(1))), set(re.findall(r"secrets\.([A-Z_]+)", block)), bool(TOKEN_IN_A_STEP.search(block))


def doc_rows(text: str, start: str) -> dict[str, list[str]]:
    """The table rows under the `### ` heading that starts with START, by job name (the first cell, in backticks)."""
    for part in re.split(r"(?m)^### ", section(text, "## The permission diagram"))[1:]:
        if part.startswith(start):
            rows = {}
            for line in part.splitlines():
                if line.startswith("| `"):
                    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                    rows[cells[0].strip("`")] = cells
            return rows
    return {}


def factory_diagram(text: str) -> list[tuple[str, str]]:
    """The factory chain of SECURITY's permission diagram: (job, annotation) for each job, where the annotation is the
    word in the row under the job's name, from the job's column to the next job's column (fix round 1, I16)."""
    lines = section(text, "## The permission diagram").split("### The caller", 1)[0].splitlines()
    chain = next(line for line in lines if "ack ─►" in line)
    row = lines[lines.index(chain) + 1]
    jobs = [(m.start(), m.group(0)) for m in re.finditer(r"[a-z][a-z-]*", chain)]
    words = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", row)]
    out = []
    for index, (start, job) in enumerate(jobs):
        end = jobs[index + 1][0] if index + 1 < len(jobs) else len(row) + 1
        out.append((job, " ".join(word for at, word in words if start <= at < end)))
    return out


def permission_problems(doc: str, caller: str, factory: str, land: str) -> list[str]:
    """Every way the security table disagrees with the workflows. An empty list means the table is the code's facts."""
    problems = []
    groups = (("The caller", job_blocks(caller)), ("The reusable", {**job_blocks(factory), **job_blocks(land)}))
    for start, code in groups:
        rows = doc_rows(doc, start)
        if set(rows) != set(code):
            problems.append(f"{start}: the table lists {sorted(rows)}, the workflows define {sorted(code)}")
        for job in sorted(set(rows) & set(code)):
            cells = rows[job]
            facts = job_facts(code[job])
            if len(cells) != 5:
                problems.append(f"{job}: a row has {len(cells)} cells, not 5")
                continue
            if facts is None:
                problems.append(f"{job}: the workflow gives it no permissions block")
                continue
            perms, secrets, has_token = facts
            if set(PERMISSION.findall(cells[1])) != perms:
                problems.append(f"{job}: the permissions in the table are not the workflow's {sorted(perms)}")
            named = set() if cells[2] == "none" else set(SECRET_NAME.findall(cells[2]))
            if named != secrets:
                problems.append(f"{job}: the secrets in the table are not the workflow's {sorted(secrets)}")
            if has_token == ("no token" in cells[3]):  # "no token" must say what the workflow does, and only that
                problems.append(f"{job}: the token cell disagrees with the workflow (a token in a step: {has_token})")
            writes = any(value == "write" for _, value in perms)
            if not cells[4].startswith(("yes", "no")):
                problems.append(f"{job}: the repo-code cell must start with yes or no")
            if writes and not cells[4].startswith("no"):  # F1: a job with a write permission runs no repo code
                problems.append(f"{job}: holds a write permission and is listed as running repo code or model output")
    return problems


def mutate_job(text: str, job: str, old: str, new: str) -> str:
    """The workflow text with ONE occurrence of OLD replaced inside the named job's block."""
    start = text.index(f"\n  {job}:\n")
    end = re.compile(r"\n  [a-z][a-z-]*:\n").search(text, start + 1)
    end = end.start() if end else len(text)
    block = text[start:end]
    assert block.count(old) >= 1, f"the mutant of {job} found no {old!r}"
    return text[:start] + block.replace(old, new, 1) + text[end:]


def mutate_row(text: str, job: str, old: str, new: str) -> str:
    """The security table with ONE occurrence of OLD replaced in the first row of the named job."""
    lines = text.split("\n")
    matches = [i for i, line in enumerate(lines) if line.startswith(f"| `{job}` |")]
    assert matches, f"the table has no row for {job}"
    lines[matches[0]] = lines[matches[0]].replace(old, new, 1)
    return "\n".join(lines)


class PermissionTableTest(unittest.TestCase):
    """docs/SECURITY.md's permission tables are checked against the three workflow files, job by job (F1, F2b)."""

    def setUp(self):
        self.texts = (SECURITY.read_text(encoding="utf-8"), CALLER.read_text(encoding="utf-8"),
                      FACTORY.read_text(encoding="utf-8"), LAND_WORKFLOW.read_text(encoding="utf-8"))

    def check(self, *texts: str) -> list[str]:
        return permission_problems(*(texts or self.texts))

    def test_every_job_of_the_workflows_has_its_row_and_the_row_is_the_code_s_fact(self):
        self.assertEqual(self.check(), [])

    def test_the_jobs_that_run_model_output_or_repo_code_hold_no_write_permission(self):  # F1, read from the workflow itself
        factory = job_blocks(self.texts[2])
        for job in ("plan", "models-up", "build", "review", "assemble", "gate"):
            perms = job_facts(factory[job])[0]
            self.assertFalse(any(value == "write" for _, value in perms), f"{job} holds {sorted(perms)}")
        self.assertEqual(job_facts(factory["publish"])[0], {("contents", "write"), ("pull-requests", "write"), ("checks", "write")})

    def test_the_diagram_annotation_of_each_factory_job_is_the_write_permission_of_its_workflow_block(self):  # I16
        factory = job_blocks(self.texts[2])
        diagram = factory_diagram(self.texts[0])
        self.assertEqual([job for job, _ in diagram],
                         ["ack", "plan", "models-up", "build", "review", "assemble", "gate", "publish", "report"])
        for job, annotation in diagram:
            perms = job_facts(factory[job])[0]
            writes = any(value == "write" for _, value in perms)
            with self.subTest(job=job):
                self.assertEqual(bool(re.search(r"WRITE|:w\b", annotation)), writes,
                                 f"the diagram shows {annotation!r} for {job}; its permissions are {sorted(perms)}")

    def test_a_mutant_that_widens_publish_to_read_is_caught(self):
        # The table says publish writes; a workflow that stops writing must turn the check red, so the table cannot drift.
        mutant = mutate_job(self.texts[2], "publish", "      contents: write", "      contents: read")
        self.assertNotEqual(mutant, self.texts[2])
        self.assertTrue(self.check(self.texts[0], self.texts[1], mutant, self.texts[3]))

    def test_a_mutant_that_gives_build_a_write_token_is_caught(self):
        # build runs model output and repo code, so a write permission there is the F1 violation.
        mutant = mutate_job(self.texts[2], "build", "      contents: read", "      contents: write")
        self.assertTrue(self.check(self.texts[0], self.texts[1], mutant, self.texts[3]))

    def test_a_mutant_that_puts_a_token_in_build_is_caught(self):
        mutant = mutate_job(self.texts[2], "build", "CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}",
                            "CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n          GH_TOKEN: ${{ github.token }}")
        self.assertTrue(self.check(self.texts[0], self.texts[1], mutant, self.texts[3]))

    def test_a_mutant_that_says_the_writing_job_runs_repo_code_is_caught(self):
        mutant = mutate_row(self.texts[0], "publish", "| no: only git", "| yes: only git")
        self.assertNotEqual(mutant, self.texts[0])
        self.assertTrue(self.check(mutant, *self.texts[1:]))

    def test_a_mutant_that_drops_a_secret_from_the_table_is_caught(self):
        mutant = mutate_row(self.texts[0], "build", "`PIVOTAL_KAGGLE_KEY`, ", "")
        self.assertNotEqual(mutant, self.texts[0])
        self.assertTrue(self.check(mutant, *self.texts[1:]))

    def test_a_bare_secret_name_in_a_permission_cell_is_matched_and_dropping_it_is_caught(self):  # S12, fix round 2
        bare = mutate_row(self.texts[0], "build", "`PIVOTAL_KAGGLE_KEY`, ", "PIVOTAL_KAGGLE_KEY, ")
        self.assertNotEqual(bare, self.texts[0])
        self.assertEqual(self.check(bare, *self.texts[1:]), [], "an unbackticked name the workflow passes is still that name")
        dropped = mutate_row(bare, "build", "PIVOTAL_KAGGLE_KEY, ", "")
        self.assertNotEqual(dropped, bare)
        self.assertTrue(self.check(dropped, *self.texts[1:]), "dropping a bare name from the table must be caught")

    def test_a_mutant_that_drops_a_row_is_caught(self):
        lines = [line for line in self.texts[0].split("\n") if not line.startswith("| `ack` |")]
        self.assertTrue(self.check("\n".join(lines), *self.texts[1:]))

    def test_a_mutant_that_drops_a_permission_from_the_untrusted_caller_row_is_caught(self):
        mutant = mutate_row(self.texts[0], "factory-untrusted", "`checks: write`, ", "")
        self.assertNotEqual(mutant, self.texts[0])
        self.assertTrue(self.check(mutant, *self.texts[1:]))

    def test_a_mutant_that_drops_write_from_the_land_job_is_caught(self):
        # land is in two places: the caller's `land` (its row is in the caller table) and land.yml's (in the reusable table).
        mutant = mutate_job(self.texts[3], "land", "      contents: write", "      contents: read")
        self.assertNotEqual(mutant, self.texts[3])
        self.assertTrue(self.check(self.texts[0], self.texts[1], self.texts[2], mutant))


class SecurityTest(unittest.TestCase):
    def setUp(self):
        self.text = SECURITY.read_text(encoding="utf-8")

    def test_the_sections_the_order_names_are_there(self):
        for heading in ("## The permission diagram", "## Who can do what", "## What an issue's text can do", "## Kaggle",
                        "## Secrets", "## Landing", "## Limits you should know", "## Reporting a problem"):
            self.assertIn(heading, self.text)

    def test_the_permission_diagram_is_a_code_block_that_names_the_jobs(self):
        diagram = self.text.split("## The permission diagram", 1)[1].split("### The caller", 1)[0]
        self.assertIn("```text", diagram)
        for job in ("trust", "factory-trusted", "factory-untrusted", "ack", "plan", "models-up", "build", "review",
                    "assemble", "gate", "publish", "report"):
            self.assertIn(job, diagram, job)

    def test_the_kaggle_rules_are_all_four_inference_keyed_self_stopping_personal(self):
        kaggle = flat(section(self.text, "## Kaggle"))
        for rule in ("**Inference only.**", "**Keyed.**", "**Self-stopping.**", "**Personal.**"):
            self.assertIn(rule, kaggle)

    def test_the_kaggle_terms_are_left_for_the_operator_to_check_not_claimed_as_kept_to(self):  # S5, fix round 2
        kaggle = flat(section(self.text, "## Kaggle"))
        self.assertIn("whether this use fits Kaggle's terms is for you to check.", kaggle)
        self.assertNotIn("Pivotal keeps to that", self.text)
        faq = flat(section(FAQ.read_text(encoding="utf-8"), "## Kaggle"))
        self.assertIn("Whether this use fits them is for you to check.", faq)
        self.assertNotIn("Pivotal keeps to that", faq)

    def test_the_keyless_check_is_named_off_by_default_and_the_variable_is_in_the_code(self):
        kaggle = flat(section(self.text, "## Kaggle"))
        self.assertIn("PIVOTAL_KEYLESS_CHECK", kaggle)
        self.assertIn("off by default", kaggle)
        self.assertIn("PIVOTAL_KEYLESS_CHECK", (ROOT / "engine" / "modelsup.py").read_text(encoding="utf-8"))

    def test_the_kaggle_session_limits_are_the_code_s_constants(self):
        kaggle = flat(section(self.text, "## Kaggle"))
        self.assertIn("20 idle minutes", kaggle)
        self.assertIn("11 hours", kaggle)

    def test_the_stranger_cannot_get_a_subscription_token_and_the_untrusted_secrets_are_the_caller_s(self):
        names = caller_secrets("factory-untrusted")
        self.assertEqual(names, {"GEMINI_API_KEY", "GROQ_API_KEY", "PIVOTAL_KAGGLE_KEY", "PIVOTAL_KAGGLE_URL"})
        for name in names:
            self.assertIn(name, self.text)
        self.assertIn("never receives your subscription tokens", flat(self.text))

    def test_the_stranger_row_and_the_untrusted_paragraph_say_the_run_model_holds_the_free_keys(self):  # C1, fix round 1
        rows = [line for line in self.text.splitlines() if line.startswith("| Anyone who can open an issue |")]
        self.assertEqual(len(rows), 1, rows)
        self.assertNotIn("see a secret", rows[0])  # the old cell: a stranger's run sees the four free keys in its model
        # Fix round 2, F1: the claim was false. engine/run.sh and engine/buildjob.py give each agent call the ONE free key of its
        # route's door; the four secrets reach the job, and one of them reaches the model.
        self.assertNotIn("holds the four free keys", rows[0])
        self.assertIn("that run's model holds only the one free key of its route's door, one agent call at a time", rows[0])
        paragraph = flat(self.text)
        self.assertIn("its model holds its own free key in its environment", paragraph)
        self.assertIn("never gets your subscription tokens or your Kaggle account key", paragraph)

    def test_the_fresh_machine_and_the_fast_forward_claims_name_the_hosted_runner_and_the_merge_check(self):  # I2, I3
        flatten = flat(self.text)
        self.assertIn("one fresh GitHub-hosted runner per job unless you use your own runner", flatten)
        self.assertNotIn("one fresh runner per job", flatten)
        self.assertNotIn("on a fresh machine", flatten)  # a runner that is not fresh is named as such
        self.assertIn("fast-forward** to the gated commit, the last commit of the landable prefix", flatten)  # bold ends here
        self.assertIn("the land job checks that its tree is the gated commit's tree", flatten)
        # Fix round 2, F3: the base also moves by an API merge on a protected branch, so "only" was false.
        self.assertNotIn("The base moves only by a plain fast-forward", flatten)
        self.assertIn("or on a protected branch by an API merge (below).", flatten)

    def test_no_server_or_daemon_is_claimed_for_pivotal_but_the_kaggle_endpoint_and_your_runner_are_named(self):  # I1
        flatten = flat(self.text)
        self.assertNotIn("It has no server, no daemon", flatten)
        self.assertIn("It runs no server or daemon of its own.", flatten)
        self.assertIn("The Kaggle endpoint is an HTTP server behind an ngrok domain", flatten)
        self.assertIn("that runner is a service on your machine", flatten)

    def test_ack_and_report_write_only_comments_labels_and_the_incident_issue(self):  # S2, fix round 2
        flatten = flat(section(self.text, "## The permission diagram"))
        self.assertIn("`ack` opens or updates the incident issue and writes comments and labels; `report` writes comments and labels.",
                      flatten)
        self.assertNotIn("`ack` and `report` write only comments and labels.", flatten)
        ack = (ROOT / "engine" / "ack.py").read_text(encoding="utf-8")
        self.assertIn("the incident issue", ack)

    def test_a_fork_pull_request_starts_nothing_on_its_own_and_land_refuses_it(self):  # S1, fix round 2
        row = [line for line in self.text.splitlines() if line.startswith("| A pull request from a fork |")]
        self.assertEqual(len(row), 1, row)
        self.assertIn("a fork PR's own events start nothing; a comment on one is decided like any comment", row[0])
        self.assertIn("land refuses a fork PR", row[0])
        # engine/land.py refuses a pull request whose head repository is another one (the fork check the row names).
        self.assertIn("if pr.head_repo != s.repo or pr.base_repo != s.repo:", (ROOT / "engine" / "land.py").read_text(encoding="utf-8"))

    def test_the_label_and_comment_rules_are_stated_as_the_code_decides_them(self):
        flatten = flat(self.text)
        self.assertIn("is decided by the issue's author's permission", flatten)
        # Fix round 2, F6: no check of the issue's state exists, so a closed issue starts a run and closing one stops nothing.
        self.assertNotIn("an open issue", flatten)
        self.assertIn("apply the `pivotal` label to an issue, open or closed.", flatten)
        self.assertIn("A labelled or commented closed issue also starts a run, and closing an issue does not stop a labelled run: "
                      "use `piv stop` or `piv level`.", flatten)
        self.assertIn("/pivotal go trusted", flatten)
        self.assertIn("SHA-256", flatten)
        self.assertIn("comes from a writer, or it is ignored", flatten)

    def test_the_seal_is_checked_for_the_app_and_the_workflow(self):
        flatten = flat(self.text)
        self.assertIn("`github-actions` app", flatten)
        self.assertIn("pivotal.yml", flatten)

    def test_the_seal_names_what_the_land_job_checks_and_what_it_does_not_prove(self):  # I5, fix round 1
        flatten = flat(self.text)
        for check in ("from the `github-actions` app", "this repo's `.github/workflows/pivotal.yml` on the default branch",
                      "sits in that run's check suite", "the run's artifacts confirm the commit"):
            self.assertIn(check, flatten)
        self.assertIn("the app name alone does not show who made the seal", flatten)
        self.assertIn("A dedicated GitHub App is an open decision.", flatten)
        # Fix round 2, F2: the "is ignored" claim was false (engine/seal.py:26-31 says the check-suite link is unverified).
        self.assertNotIn("Another workflow's check of the same name is ignored.", flatten)
        self.assertIn("Under the check-suite link, which is not verified on real GitHub, another workflow's check of the same name "
                      "may pass (engine/seal.py:26-31).", flatten)

    def test_a_triagers_label_at_the_land_level_is_named_in_the_row_and_the_rule(self):  # I6, fix round 1
        sentence = "can end in a landed change with no further command, if the gate and the review pass"
        self.assertEqual(flat(self.text).count(sentence), 2, "the labels row and the trust rule must both say it")

    def test_a_stranger_run_spends_quota_and_nothing_caps_it(self):  # I7, fix round 1
        self.assertIn("It spends your free-key quota and your Kaggle endpoint time, and Pivotal does not cap that", flat(self.text))

    def test_the_mirror_limit_is_named_as_open(self):  # I8 and I10, fix round 1
        flatten = flat(self.text)
        self.assertIn("The mirror line is refused at plan until its scorer's inputs are supplied.", flatten)
        self.assertIn("its scorer runs as the same user as the build, which runs the agent, so the model could forge the score. "
                      "This is not closed.", flatten)

    def test_the_keyless_switch_is_the_repo_variable_models_up_reads_on_every_run(self):  # S3, fix round 1
        kaggle = flat(section(self.text, "## Kaggle"))
        self.assertIn("the repo variable `vars.PIVOTAL_KEYLESS_CHECK`, which models-up reads on every run", kaggle)
        self.assertNotIn("for a run)", kaggle)
        factory = (ROOT / ".github" / "workflows" / "factory.yml").read_text(encoding="utf-8")
        self.assertIn("PIVOTAL_KEYLESS_CHECK: ${{ vars.PIVOTAL_KEYLESS_CHECK }}", factory)

    def test_a_trusted_run_on_your_runner_is_every_trusted_run_under_trusted_only(self):  # S5, fix round 1
        flatten = flat(self.text)
        self.assertIn("every trusted run goes to that runner, not only the issues a writer approved with `/pivotal go trusted`",
                      flatten)
        self.assertNotIn("that same approval sends", flatten)

    def test_the_tests_are_said_to_use_fake_agents_and_not_to_be_the_proof(self):  # S10, fix round 1
        self.assertIn("The tests use fake agents (`test/fakecli.py`)", self.text)
        self.assertTrue((ROOT / "test" / "fakecli.py").is_file())
        self.assertNotIn("The tests are the proof", self.text)

    def test_a_leaked_key_has_a_rotate_step_and_a_remove_step(self):  # S11, fix round 1
        leaked = flat(section(self.text, "## Reporting a problem"))
        self.assertIn("**If a key may have leaked,** rotate it at its provider first.", leaked)
        self.assertIn("`piv kaggle rotate`", leaked)
        self.assertIn("`piv remove REPO --yes`", leaked)

    def test_the_phrases_the_landing_rules_are_known_by_stay_as_written(self):  # I16, fix round 1
        flatten = flat(self.text)
        self.assertIn("A push is never forced.", flatten)
        self.assertIn("A moved base is not re-gated.", flatten)
        self.assertIn("trusts it only if", flatten)

    def test_an_issues_text_does_not_say_a_change_never_lands_on_its_own_at_the_land_level(self):  # F4, fix round 2
        flatten = flat(section(self.text, "## What an issue's text can do"))
        self.assertNotIn("The change is not landed on its own:", flatten)  # false at the land level
        self.assertIn("Below the land level, the change is not landed on its own:", flatten)
        self.assertIn('At the land level a green change with a READY review lands with no further command (see Landing); '
                      'below it someone must say "land it".', flatten)

    def test_the_draft_pull_request_and_the_stop_sentences_are_pinned(self):  # S12, fix round 2
        flatten = flat(self.text)
        self.assertIn("It never moves the base, never seals a commit, and never lands.", flatten)  # a draft's run
        self.assertIn("cancelled never publishes and never lands.", flatten)  # a stopped run
        self.assertIn("Pivotal does not block network access, so the issue's text could steer that model to send a key elsewhere.",
                      flatten)  # the untrusted run's model

    def test_only_the_trusted_caller_job_passes_every_secret(self):  # S12, fix round 2
        self.assertIn("Only the trusted caller job passes every secret.", flat(self.text))
        self.assertGreater(len(caller_secrets("factory-trusted")), len(caller_secrets("factory-untrusted")))  # the caller's own facts

    def test_the_known_limits_say_what_has_run_live(self):
        self.assertIn(TABLE_LINK, flat(self.text))
        self.assertIn("The live run is proven through `build` only", flat(self.text))
        self.assertIn("43279525237", self.text)

    def test_no_piv_set_or_secrets_command_is_named(self):  # neither exists: the table of commands is piv's own
        self.assertNotRegex(self.text, r"`piv (set|secrets)\b")

    def test_the_reporting_section_names_no_contact_it_does_not_have(self):
        reporting = flat(section(self.text, "## Reporting a problem"))
        self.assertIn("has no published security contact yet", reporting)
        self.assertIn("This repository does not configure it for you", reporting)
        self.assertNotIn("@", reporting)


class FaqTest(unittest.TestCase):
    def setUp(self):
        self.text = FAQ.read_text(encoding="utf-8")

    def test_the_stranger_secrets_answer_says_what_the_run_holds_not_that_nothing_is_passed(self):  # C1, fix round 1
        answer = faq_answer(self.text, "Can a stranger read my secrets?")
        self.assertTrue(answer.startswith("Not your subscription tokens and not your Kaggle account key."), answer)
        self.assertNotIn("No. Trust is decided", self.text)
        for name in caller_secrets("factory-untrusted"):  # the four names are the caller's, not a list typed here
            self.assertIn(name, answer)

    def test_the_stranger_secrets_answer_says_the_model_holds_its_key_and_pivotal_does_not_block_network(self):  # C1, fix round 1
        answer = faq_answer(self.text, "Can a stranger read my secrets?")
        self.assertIn("The model in that run holds its own key in its environment.", answer)
        self.assertIn("Pivotal does not block network access", answer)

    def test_the_faq_links_to_the_proven_table(self):  # I16, fix round 1
        self.assertEqual(self.text.count(TABLE_LINK), 2)
        self.assertNotIn("**What has run live**", self.text)

    def test_the_questions_the_order_names_are_answered(self):
        self.assertIn("## Costs", self.text)  # public repos free; private minutes; Kaggle quota
        self.assertIn("## Vendor terms and subscription tokens", self.text)
        self.assertIn("Is it safe to let a change land?", self.text)
        self.assertIn("Kaggle's limits", self.text)

    def test_your_own_runner_is_said_to_run_only_plan_build_and_gate(self):  # I4, fix round 1
        costs = flat(section(self.text, "## Costs"))
        self.assertIn("runs only plan, build and gate, on your machine", costs)
        self.assertNotIn("uses your machine and no Actions minutes", self.text)
        self.assertNotIn("stop using Actions minutes", self.text)

    def test_only_builds_clis_run_on_your_runner_and_review_is_hosted(self):  # S6, fix round 2
        answer = faq_answer(self.text, "Can I run it on my own machine?")
        self.assertIn("plan, build and gate run on your machine, and only build's subscription CLIs run there; review is GitHub-hosted.",
                      answer)
        self.assertNotIn("so your subscription CLIs run there", answer)
        jobs = job_blocks(FACTORY.read_text(encoding="utf-8"))  # review is the model job that is not on the runner choice
        self.assertNotIn("outputs.runner", re.search(r"^    runs-on: (.*)$", jobs["review"], re.MULTILINE).group(1))

    def test_the_server_answer_names_the_kaggle_endpoint_and_your_runner_as_the_only_services(self):  # I1, fix round 1
        answer = faq_answer(self.text, "Do I need a server?")
        self.assertTrue(answer.startswith("No server of Pivotal's own."), answer)
        for part in ("Kaggle endpoint", "an HTTP server behind an ngrok domain", "your own runner"):
            self.assertIn(part, answer)

    def test_between_runs_nothing_runs_and_a_kaggle_session_can_stay_up_after_one(self):  # I12, fix round 1
        answer = faq_answer(self.text, "Does it cost anything when nothing is running?")
        self.assertTrue(answer.startswith("Between runs, nothing runs."), answer)
        self.assertIn("a Kaggle session can stay up", answer)
        self.assertNotIn("no server stays up", self.text)

    def test_the_kaggle_exit_rules_say_they_apply_after_a_run(self):  # I12, fix round 1
        answer = faq_answer(self.text, "How long does a Kaggle session run?")
        self.assertIn("After a run, it exits after 20 idle minutes", answer)
        self.assertIn("15 minutes of loading", answer)

    def test_a_stranger_alone_cannot_start_work_and_a_run_never_lands_by_itself(self):  # S1, I6, I7, fix round 1
        answer = faq_answer(self.text, "Can a stranger make Pivotal do work in my repo?")
        self.assertIn("A stranger alone cannot start work.", answer)
        self.assertIn("a red CI run on main that a writer pushed", answer)
        self.assertIn("a `piv event` from a writer", answer)
        self.assertIn("does not land by itself", answer)
        self.assertIn("It can land only after a writer approves it with `/pivotal go trusted`", answer)
        self.assertIn("spends your free-key quota and Kaggle endpoint time, and Pivotal does not cap that", answer)
        self.assertIn("A writer can also mark a draft ready and merge it by hand in GitHub, which skips the gate.", answer)  # S3
        self.assertIn("A label from someone with triage or write access starts a run", answer)  # S12, fix round 2
        self.assertIn("At the land level, a label applied by a triager to an issue whose author has write access can end in a "
                      "landed change with no further command", answer)
        self.assertNotIn("nothing lands from it", self.text)

    def test_a_refused_kaggle_push_skips_the_kaggle_entries_in_that_run_only(self):  # S4, fix round 1
        limits = flat(section(self.text, "## Kaggle"))
        self.assertIn("A refused push skips the Kaggle entries in that run, and the run report says so.", limits)
        self.assertIn("The next run tries the push again", limits)
        self.assertNotIn("until the weekly reset", limits)

    def test_free_kaggle_includes_groq_and_faq_mentions_it(self):  # S6, fix round 1
        self.assertIn("GROQ_API_KEY", self.text)
        free_kaggle = (ROOT / "presets" / "free-kaggle.json").read_text(encoding="utf-8").lower()
        self.assertIn("groq", free_kaggle)
        self.assertIn("gpt-oss-120b", free_kaggle)
        # Other shipped presets do not use groq
        for preset in (ROOT / "presets").glob("*.json"):
            if preset.stem != "free-kaggle":
                self.assertNotIn("groq", preset.read_text(encoding="utf-8").lower(), preset.name)

    def test_a_vendor_token_goes_only_to_its_vendors_api(self):  # S7, fix round 1
        vendor = flat(section(self.text, "## Vendor terms and subscription tokens"))
        # Fix round 2, F5: "goes only to its vendor's API" was false: nothing blocks network access (no egress control in the repo).
        self.assertIn("Your token is used only in your repo's jobs. Only the agent that needs it holds it. Pivotal does not block "
                      "network access, so a steered model could send it elsewhere; the scan covers the patch only and not every "
                      "encoding.", vendor)
        self.assertNotIn("it goes only to its vendor's API", vendor)
        self.assertNotIn("it is not sent to anyone else", vendor)

    def test_the_time_to_healthy_placeholder_says_pivotals_own_live_run_is_not_measured(self):  # S8, fix round 1
        flatten = flat(self.text)
        self.assertIn("Pivotal's own live run is not measured.", flatten)
        self.assertIn("records a time to healthy taken on 2026-11-01 with sysop's serve module", flatten)

    def test_the_costs_name_no_number_but_the_kaggle_quota(self):
        costs = section(self.text, "## Costs")
        self.assertLessEqual(set(re.findall(r"\d[\d.,]*", costs)), {"30"}, costs)  # the Kaggle quota is the only figure

    def test_the_landing_answer_names_the_gate_the_seal_and_the_level(self):
        answer = flat(section(self.text, "## Safety"))
        self.assertIn("exact commit", answer)
        self.assertIn("READY", answer)
        self.assertIn("pivotal/gate", answer)
        self.assertIn("say \"land it\"", answer)

    def test_the_landing_answer_says_what_each_level_lands_and_that_the_review_binds_the_patch(self):  # I11, fix round 1
        answer = faq_answer(self.text, "Is it safe to let a change land?")
        for level in job.LEVELS:  # each level is its own list item, not a word that happens to appear
            self.assertIn(f"- `{level}`", answer)
        self.assertIn("The pipeline lands the change under the gate", answer)
        self.assertIn("a reviewer from a different vendor answers READY on the patch", answer)
        self.assertIn("The commit that lands is that patch re-applied on the stack", answer)
        self.assertIn(f"The check command is `{init.VAR_CHECK}`", answer)
        self.assertIn("at `prepare` and `land` the base must also be green", answer)
        self.assertNotIn("answers READY on that commit", self.text)

    def test_the_landing_answer_names_what_is_not_verified_and_the_app_is_an_open_decision(self):  # I5 and I11, fix round 1
        flatten = flat(section(self.text, "## Safety"))
        self.assertIn(TABLE_LINK, flatten)
        self.assertIn("These parts of the landing are not yet checked on a live run:", flatten)
        for item in ("check suite id", "`gh run download --name <n> --dir <d>`", "`--paginate --slurp`", "`merge_method=merge`",
                     "fast-forward push", "how branch protection and a merge queue answer a push"):
            self.assertIn(item, flatten)
        self.assertIn("A dedicated GitHub App is an open decision", flatten)
        self.assertIn("from the `github-actions` app, that its run is this repo's `.github/workflows/pivotal.yml` on the default branch",
                      flatten)

    def test_an_incident_fix_is_always_a_draft_and_the_reasons_are_the_ones_assemble_writes(self):  # I9, fix round 1
        flatten = flat(self.text)
        self.assertIn("An incident's fix is always a draft, never a ready pull request", flatten)
        self.assertIn("with a red base it is a draft with the reason `baseline_red`", flatten)
        self.assertIn("with a green base it is a draft with the reason `already_green`", flatten)
        assemble = (ROOT / "engine" / "assemble.py").read_text(encoding="utf-8")
        self.assertIn('return "draft", "baseline_red"', assemble)
        self.assertIn('return "draft", "already_green"', assemble)

    def test_the_setup_token_claim_is_the_plans_and_this_build_has_not_confirmed_it(self):  # I14, fix round 1
        vendor = flat(section(self.text, "## Vendor terms and subscription tokens"))
        self.assertIn("The plan records that Anthropic's GitHub Actions page", vendor)
        self.assertIn("describes setup-token for personal use. This build has not confirmed it; whether your plan allows it is "
                      "for you to check.", vendor)
        self.assertNotIn("supports it for personal use", vendor)

    def test_the_state_answer_names_the_kaggle_dataset_and_session_as_well_as_github(self):  # I13, fix round 1
        answer = faq_answer(self.text, "Where is the state kept?")
        self.assertIn("On Kaggle: the private dataset `<you>/pivotal-serve-secrets`, which holds the ngrok token and the endpoint key, "
                      "and the session.", answer)
        self.assertIn("pivotal-serve-secrets", (ROOT / "gh-piv" / "piv" / "serve.py").read_text(encoding="utf-8"))
        # Fix round 2, S7: "Nothing is kept on your machine" was too wide: a self-hosted runner keeps CLI logins in pv-runner's home.
        self.assertNotIn("Nothing is kept on your machine, and", answer)
        self.assertIn("Nothing is kept on your machine unless you use your own runner: then the subscription CLIs keep their logins in "
                      "`pv-runner`'s home.", answer)

    def test_the_vendor_answer_does_not_claim_the_vendors_terms(self):
        vendor = flat(section(self.text, "## Vendor terms and subscription tokens"))
        self.assertIn("Each vendor sets its own terms", vendor)
        self.assertIn("has not re-checked those terms", vendor)

    def test_the_build_status_names_what_is_not_built(self):
        status = flat(section(self.text, "## Using it"))
        self.assertIn("an instant no-Kaggle `piv demo` mode", status)


class DemoTapeTest(unittest.TestCase):
    def setUp(self):
        self.lines = DEMO_TAPE.read_text(encoding="utf-8").splitlines()

    def test_the_header_says_the_tape_is_not_recorded_and_why(self):
        head = " ".join(self.lines[:3])
        self.assertIn("Not recorded yet", head)
        self.assertIn("refused", head)

    def test_every_typed_command_is_a_real_piv_command(self):
        parser = cli.build_parser()
        typed = [line[len('Type "'):-1] for line in self.lines if line.startswith('Type "')]
        self.assertTrue(typed)
        for command in typed:
            words = shlex.split(command)
            with self.subTest(command=command):
                self.assertEqual(words[0], "piv")
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(words[1:])

    def test_each_wait_waits_for_text_the_command_prints(self):
        demo_source = (ROOT / "gh-piv" / "piv" / "demo.py").read_text(encoding="utf-8")
        waits = [re.search(r"Wait\+Screen@\S+ /(.+)/$", line).group(1) for line in self.lines if line.startswith("Wait+Screen")]
        self.assertEqual(waits, ["run links:", "deleted"])
        self.assertIn('say("run links:")', demo_source)
        self.assertIn('say(f"deleted {repo}")', demo_source)

    def test_the_output_is_the_gif_the_readme_says_it_will_be(self):
        self.assertIn("Output docs/demo.gif", self.lines)


class NamesTest(unittest.TestCase):
    """What the two new docs name must exist: each variable is read by the code, each repo file and directory is there."""

    CODE_DIRS = ("engine", ".github", "templates", "kaggle", "gh-piv", "presets")
    VARIABLE = re.compile(r"\b(?:PIVOTAL|KAGGLE|GEMINI|GROQ|OPENAI|ANTHROPIC|CLAUDE|XAI|OPENROUTER|NGROK)_[A-Z0-9_]*[A-Z0-9]\b")
    REPO_FILE = re.compile(r"`([\w./-]+\.(?:py|md|yml|json|sh|tmpl|ipynb|tape))`")
    REPO_DIR = re.compile(r"`([\w.-]+/)`")

    def setUp(self):
        self.docs = {path: path.read_text(encoding="utf-8") for path in (SECURITY, FAQ)}

    def test_every_variable_or_secret_the_new_docs_name_is_read_by_the_code(self):
        code = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                         for name in self.CODE_DIRS for path in (ROOT / name).rglob("*") if path.is_file())
        named = {name for text in self.docs.values() for name in self.VARIABLE.findall(text)}
        self.assertTrue(named)
        for name in sorted(named):
            with self.subTest(name=name):
                self.assertIn(name, code, f"a doc names {name}, which the code never reads")

    def test_every_repo_file_and_directory_the_new_docs_name_exists(self):
        for path, text in self.docs.items():
            for token in self.REPO_FILE.findall(text):
                if token.endswith("workflows/pivotal.yml"):
                    continue  # the caller file in YOUR repo, not one of ours
                with self.subTest(file=path.name, token=token):
                    self.assertTrue((ROOT / token).is_file(), f"{path.name} names {token}, which is not in the repo")
            for token in self.REPO_DIR.findall(text):
                with self.subTest(file=path.name, token=token):
                    self.assertTrue((ROOT / token).is_dir(), f"{path.name} names {token}, which is not in the repo")


class RepoDocsTest(unittest.TestCase):
    def test_every_markdown_file_in_the_repo_passes_doccheck(self):
        out = io.StringIO()
        self.assertEqual(doccheck.check(ROOT, out), 0, out.getvalue())


KAGGLE_SPEC = "pi:kaggle/qwen3.6-35b-a3b"


def secret_env(with_kaggle_key: bool) -> dict[str, str]:
    """Every door's secret set to a stand-in, and PIVOTAL_KAGGLE_KEY set or not."""
    env = {name: "x" for names in route.DOOR_SECRETS.values() for name in names if name != "PIVOTAL_KAGGLE_KEY"}
    if with_kaggle_key:
        env["PIVOTAL_KAGGLE_KEY"] = "k" * 8
    return env


def route_strictness(source: str) -> list[bool]:
    """For each route.route call in the source, whether it passes strict_first=True."""
    flags = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "route.route":
            strict = {kw.arg: kw.value for kw in node.keywords}.get("strict_first")
            flags.append(isinstance(strict, ast.Constant) and strict.value is True)
    return flags


class ShippedRouteTest(unittest.TestCase):
    """The docs' claims about the build's first entry, run through route.route with strict_first=True as engine/buildjob.py
    calls it (fix round 1, I-1): without PIVOTAL_KAGGLE_KEY the run is red at once; with it, the build falls back only when
    the endpoint is down or refuses the key."""

    def test_buildjob_passes_strict_first_to_its_one_route_call(self):  # fix round 2, S-d: the call these claims assume
        self.assertEqual(route_strictness((ROOT / "engine" / "buildjob.py").read_text(encoding="utf-8")), [True])

    def test_a_build_route_without_strict_first_is_named(self):  # fix round 2, S-d: the mutant
        self.assertEqual(route_strictness("chosen = route.route(entries, probe, strict_first=False)"), [False])
        self.assertEqual(route_strictness("chosen = route.route(entries, probe)"), [False])

    # claude-plus-free was removed from presets/ (open models first). Its build tier stays here as a fixture: the route
    # rule it tests (a Kaggle first entry, then a Claude fallback) still exists, and no shipped preset has that shape now.
    FIXTURES = {"claude-plus-free": {"name": "claude-plus-free", "kaggle": {"model": "qwen3.6-35b-a3b"},
                                     "tiers": {"build": ["pi:kaggle/qwen3.6-35b-a3b", "claude:anthropic/sonnet"],
                                               "review": ["claude:anthropic/sonnet", "pi:kaggle/qwen3.6-35b-a3b"]}}}

    def build_chain(self, name: str) -> list:
        if name in self.FIXTURES:
            return route.chain(route.parse_preset(self.FIXTURES[name]).tiers["build"])
        return route.chain(route.load_preset(ROOT / "presets" / f"{name}.json").tiers["build"])

    def test_without_the_kaggle_key_the_first_build_entry_is_red_and_names_the_secret(self):
        for name in ("free-kaggle", "claude-plus-free"):
            with self.subTest(preset=name):
                result = route.route(self.build_chain(name), route.make_probe(secret_env(False)), strict_first=True)
                self.assertEqual(result.status, "red")
                self.assertIn("PIVOTAL_KAGGLE_KEY", result.outcome)
                self.assertEqual(result.fell_back, (), "a red first entry falls back to nothing")

    def test_with_the_key_and_the_endpoint_up_the_build_runs_on_kaggle(self):
        for name in ("free-kaggle", "claude-plus-free"):
            with self.subTest(preset=name):
                result = route.route(self.build_chain(name), route.make_probe(secret_env(True)), strict_first=True)
                self.assertEqual(result.outcome, f"ok: {KAGGLE_SPEC}")

    def test_with_the_key_a_down_endpoint_falls_back_only_where_a_claude_entry_follows(self):
        down = frozenset({KAGGLE_SPEC})
        free = route.route(self.build_chain("free-kaggle"), route.make_probe(secret_env(True), down=down), strict_first=True)
        self.assertEqual(free.outcome, "failed: no_route", "free-kaggle has no build entry after the Kaggle one")
        claude = route.route(self.build_chain("claude-plus-free"), route.make_probe(secret_env(True), down=down), strict_first=True)
        self.assertEqual(claude.outcome, "ok: claude:anthropic/sonnet")
        self.assertEqual([item.as_dict() for item in claude.fell_back],
                         [{"from": KAGGLE_SPEC, "to": "claude:anthropic/sonnet", "reason": "endpoint down"}])


# Names outside command syntax, and #fragments (fix round 1, I-5 and Z). A backticked name is checked against the code's own
# tables; a heading's #fragment must be the GitHub anchor of a heading in its file.

# The test seams: named by test/ only (fix round 2, S-d), never by a user doc.
SEAM_NAMES = frozenset({"PIV_GH", "PIV_KAGGLE", "PIVOTAL_TEST_JOBS", "PIVOTAL_TEST_TIMEOUT"})
EXAMPLE_LABELS = frozenset({"pivotal:api-only", "pivotal:custom"})  # the deliberate examples of a custom label
# The engine repo constant the own-model recipe (EXAMPLES.md) tells you to repoint (gh-piv/piv/init.py).
CODE_NAMES = frozenset({"ENGINE_REPO"})
PATH_WORD = re.compile(r"[^\s'\"`$()\[\]{};,=|&]+")  # a word of a command, a line or a span; a path is one word (a placeholder's <> stay)
EXTENSION = re.compile(r"\.[A-Za-z0-9]+$")  # a dot and letters or digits to the end of the word
VARIABLE_WORD = re.compile(r"PIV(?:OTAL)?_[A-Z][A-Z_]*")  # a variable named anywhere in a fenced line, not only in a span
USER_REPO_PATHS = frozenset({".github/workflows/pivotal.yml"})  # piv init writes this into the user's repo, not this one
VARIABLE_NAME = re.compile(r"PIV(?:OTAL)?_[A-Z][A-Z_]*")
LABEL_NAME = re.compile(r"pivotal:[a-z][a-z-]*")
COMMAND_NAME = re.compile(r"/pivotal(?: [a-z]+)+")
KEBAB_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
LINK_TARGET = re.compile(r"\]\(\s*<?([^)\s>]*)")


def prose_lines(text: str) -> list[tuple[int, str]]:
    """(number, line) of each line outside a code fence."""
    lines, inside = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            inside = not inside
        elif not inside:
            lines.append((number, line))
    return lines


def fenced_lines(text: str) -> list[tuple[int, str]]:
    """(number, line) of each line inside a code fence."""
    lines, inside = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            inside = not inside
        elif inside:
            lines.append((number, line))
    return lines


def path_words(text: str) -> list[str]:
    """Each word that looks like a file: a "/" and an extension. A `:line` suffix is dropped, so `file.py:21` names file.py."""
    found = []
    for word in PATH_WORD.findall(text):
        word = re.sub(r":\d+$", "", word)
        if "/" in word and "://" not in word and EXTENSION.search(word):
            found.append(word)
    return found


def variable_names() -> set[str]:
    return set(init.VARIABLES) | workflow_names(r"\bvars\.(PIVOTAL_[A-Z_]+)")


def secret_names() -> set[str]:
    return set(init.SECRETS) | workflow_names(r"\bsecrets\.([A-Z_]+)")


def input_names() -> set[str]:
    return (called_names("factory.yml", "    inputs:\n", "    secrets:\n")
            | called_names("land.yml", "    inputs:\n", "    outputs:\n")
            | called_names("land.yml", "    outputs:\n", "\npermissions:"))


def label_names() -> set[str]:
    return {label for label, _colour, _text in init.LABELS}


def preset_names() -> set[str]:
    return {path.stem for path in (ROOT / "presets").glob("*.json")}


def line_names() -> set[str]:
    return {path.stem for path in (ROOT / "lines").glob("*.json")}


def comment_commands() -> set[str]:
    words = re.search(r"\(([a-z|]+)\)", trust.COMMAND.pattern).group(1).split("|")
    return {f"/pivotal {word}" for word in words} | {trust.EXPLICIT_TRUST}


def workflow_names(pattern: str) -> set[str]:
    """The names one capture group of ``pattern`` finds in each workflow file and in the caller template."""
    names = set()
    for path in [*sorted((ROOT / ".github" / "workflows").glob("*.yml")), ROOT / "templates" / "caller.yml"]:
        names |= set(re.findall(pattern, path.read_text(encoding="utf-8")))
    return names


def called_names(file: str, start: str, end: str) -> set[str]:
    """The input or output names (six spaces, one colon) of one block of a reusable workflow's ``workflow_call``."""
    text = (ROOT / ".github" / "workflows" / file).read_text(encoding="utf-8")
    block = text.split(start, 1)[1].split(end, 1)[0]
    return set(re.findall(r"^      ([a-z_]+):\n", block, re.MULTILINE))


def allowed_names(path: Path | None) -> frozenset[str]:
    """The names a file may show beyond the code's variables and secrets: the test seams in test/, the engine constant in EXAMPLES.md."""
    if path is None:
        return frozenset()
    names = set()
    if path.resolve().is_relative_to(ROOT / "test"):
        names |= SEAM_NAMES
    if path.name == "EXAMPLES.md":
        names |= CODE_NAMES
    return frozenset(names)


def path_problem(span: str) -> str:
    """Why a path-like code span names no file of this repository, or "" when it does or is not checked here."""
    if span.startswith("~/") or "<" in span or span in USER_REPO_PATHS:  # the user's home, a placeholder, the user's own repo
        return ""
    if "*" in span:
        return "" if list(ROOT.glob(span)) else f"`{span}` matches no file"
    return "" if (ROOT / span).exists() else f"`{span}` names no file in the repository"


def name_problems(text: str, path: Path | None = None) -> list[str]:
    """Each backticked name outside a command that the code does not have, each heading that names an unknown preset, and each
    variable or path in a fenced line (a command, fix round 2, S-d) that the code or the repo does not have."""
    problems = []
    variables = variable_names() | secret_names() | allowed_names(path)
    labels = label_names() | EXAMPLE_LABELS
    commands = comment_commands()
    for number, line in prose_lines(text):
        for span in CODE_SPAN.findall(line):
            if VARIABLE_NAME.fullmatch(span) and span not in variables:
                problems.append(f"{number}: `{span}` is not a variable or secret the code has")
            elif LABEL_NAME.fullmatch(span) and span not in labels:
                problems.append(f"{number}: `{span}` is not a label the code has")
            elif COMMAND_NAME.fullmatch(span) and span not in commands:
                problems.append(f"{number}: `{span}` is not a comment command the code has")
            for word in path_words(span):
                if path_problem(word):
                    problems.append(f"{number}: {path_problem(word)}")
            if line.startswith("#") and KEBAB_NAME.fullmatch(span) and span not in preset_names():
                problems.append(f"{number}: the heading names `{span}`, which is not a shipped preset")
    for number, line in fenced_lines(text):
        for name in VARIABLE_WORD.findall(line):
            if name not in variables:
                problems.append(f"{number}: `{name}` is not a variable or secret the code has")
        for word in path_words(line):
            if path_problem(word):
                problems.append(f"{number}: {path_problem(word)}")
    return problems


def github_slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation out (letters, digits, hyphen and underscore stay), spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    """The anchors of a file's headings. A repeated heading gets -1, -2 and so on, as GitHub numbers them."""
    seen, found = {}, set()
    for _number, line in prose_lines(text):
        heading = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
        if heading:
            slug = github_slug(heading.group(1))
            count = seen.get(slug, 0)
            found.add(slug if count == 0 else f"{slug}-{count}")
            seen[slug] = count + 1
    return found


def fragment_problems(path: Path) -> list[str]:
    """Each link in the file whose #fragment names no heading of its target (a Markdown file, or the file itself)."""
    problems, text = [], path.read_text(encoding="utf-8")
    for number, line in prose_lines(text):
        for target in LINK_TARGET.findall(CODE_SPAN.sub(" ", line)):
            file_part, _, fragment = target.partition("#")
            if not fragment or re.match(r"[a-z]+:", target):
                continue
            dest = path if file_part == "" else path.parent / file_part
            if dest.suffix != ".md" or not dest.is_file():
                continue  # a missing file is doccheck's problem
            if fragment not in anchors(dest.read_text(encoding="utf-8")):
                problems.append(f"{number}: #{fragment} is not a heading of {dest.relative_to(ROOT)}")
    return problems


class NamesAndFragmentsTest(unittest.TestCase):
    def test_every_backticked_name_outside_a_command_is_one_the_code_has(self):  # I-5
        for path in DOCS:
            with self.subTest(file=path.name):
                self.assertEqual(name_problems(path.read_text(encoding="utf-8"), path), [])

    def test_every_fragment_names_a_heading_of_its_file(self):  # Z
        for path in DOCS:
            with self.subTest(file=path.name):
                self.assertEqual(fragment_problems(path), [])

    def test_github_anchors_follow_the_slug_rules(self):
        self.assertEqual(github_slug("7. Land it"), "7-land-it")
        self.assertEqual(github_slug("### `piv preset`, and `piv preset check`".lstrip("# ")), "piv-preset-and-piv-preset-check")
        self.assertEqual(github_slug("Your own line, preset or model"), "your-own-line-preset-or-model")
        self.assertTrue(hasattr(init, "ENGINE_REPO"), "CODE_NAMES names a constant init.py no longer has")
        self.assertEqual(anchors("# A\n## A\n```\n# not a heading\n```\n"), {"a", "a-1"})

    def test_a_name_that_the_code_does_not_have_is_named(self):
        self.assertEqual(len(name_problems("Set `PIVOTAL_PRESET_JSONS` first.\n")), 1)
        self.assertEqual(name_problems("Set `PIVOTAL_PRESET_JSON` first.\n"), [])
        self.assertEqual(len(name_problems("Run `/pivotal halt`.\n")), 1)
        self.assertEqual(len(name_problems("Label `pivotal:mirrored`.\n")), 1)
        self.assertEqual(len(name_problems("## Six with `claude-plus-fre`\n")), 1)
        self.assertEqual(len(name_problems("See `engine/hil.py`.\n")), 1)
        self.assertEqual(name_problems("See `engine/hill.py` and `pivotal:api-only`.\n"), [])

    def test_a_fenced_variable_or_path_the_code_does_not_have_is_named(self):  # fix round 2, S-d
        self.assertEqual(len(name_problems("```bash\ngh variable set PIVOTAL_RUNNER_PUBLC --body x\n```\n")), 1)
        self.assertEqual(len(name_problems('```bash\nPIVOTAL_PRESET_JSONS="$(cat my-mix.json)"\n```\n')), 1)
        self.assertEqual(len(name_problems("```bash\npython3 kaggle/rendr.py\n```\n")), 1)
        self.assertEqual(name_problems("```bash\ngh variable set PIVOTAL_LEVEL --body ask\npython3 kaggle/render.py\n```\n"), [])

    def test_a_path_span_with_a_line_number_or_an_unknown_extension_is_checked(self):  # fix round 2, S-d
        self.assertEqual(len(name_problems("See `gh-piv/piv/initt.py:21`.\n")), 1)
        self.assertEqual(len(name_problems("See `engine/buildjob.pyy`.\n")), 1)
        self.assertEqual(name_problems("See `engine/buildjob.py:21` and `kaggle/render.py`.\n"), [])

    def test_a_seam_or_code_name_is_accepted_only_where_its_scope_allows_it(self):  # fix round 2, S-d
        self.assertEqual(len(name_problems("Set `PIVOTAL_TEST_JOBS` to pick the level.\n", README)), 1)
        self.assertEqual(name_problems("Set `PIVOTAL_TEST_JOBS` to pick the shards.\n", ROOT / "test" / "notes.md"), [])
        self.assertEqual(len(name_problems("Set `PIV_GH` first.\n", README)), 1)
        self.assertEqual(name_problems("Set `PIV_GH` first.\n", ROOT / "test" / "notes.md"), [])


# Fix round 2 (I-1): `piv level REPO LEVEL` is a setter (gh-piv/piv/var.py `level`), so no docs sentence may present it as a
# check. The sentence that introduces a command is the words after the last sentence end, in its paragraph; a fenced command
# takes the paragraph above its fence, which may be separated from the fence by a blank line.
LEVEL_COMMAND = re.compile(r"\bpiv level\b")
CHECK_WORDS = re.compile(r"\b(?:check|checks|checking|see|look at)\b", re.IGNORECASE)
SENTENCE_END = re.compile(r"[.!?;:](?=\s|$)")


def introducing_sentence(text: str) -> str:
    """The last sentence of the text, with its final punctuation dropped."""
    return SENTENCE_END.split(text.rstrip(" \t\n:."))[-1]


def level_checks(text: str) -> list[str]:
    """Each `piv level` command that its introducing sentence presents as a check (check, see, look at)."""
    found, paragraph, introducer, fenced = [], [], "", False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            if not fenced and paragraph:
                introducer, paragraph = "\n".join(paragraph), []
            fenced = not fenced
        elif fenced:
            if LEVEL_COMMAND.search(line) and CHECK_WORDS.search(introducing_sentence(introducer)):
                found.append(f"{number}: {line.strip()}")
        elif not line.strip():
            if paragraph:
                introducer, paragraph = "\n".join(paragraph), []
        else:
            for match in LEVEL_COMMAND.finditer(line):
                before = "\n".join([*paragraph, line[:match.start()]])
                if CHECK_WORDS.search(introducing_sentence(before)):
                    found.append(f"{number}: {line.strip()}")
            paragraph.append(line)
    return found


class LevelCommandIsNotACheckTest(unittest.TestCase):
    def test_no_docs_sentence_presents_a_level_command_as_a_check(self):  # fix round 2, I-1
        for path in DOCS:
            with self.subTest(file=path.name):
                self.assertEqual(level_checks(path.read_text(encoding="utf-8")), [])

    def test_the_old_demo_text_that_called_a_setter_a_check_is_named(self):  # fix round 2, I-1: the mutant
        old = ("Nothing to run once set up. A push to the default branch that makes `CI` fail starts the run. You can check the level and\n"
               "the watchdog at any time:\n\n```bash\npiv level you/my-app prepare\npiv digest --repo you/my-app\n```\n")
        self.assertEqual(len(level_checks(old)), 1)

    def test_a_level_command_after_see_or_look_at_is_named_and_one_after_a_sentence_end_is_not(self):
        self.assertEqual(len(level_checks("To see the state, run `piv level you/my-app watch`.\n")), 1)
        self.assertEqual(len(level_checks("Look at it:\n\n```bash\npiv level you/my-app ask\n```\n")), 1)
        self.assertEqual(level_checks("Check the digest. Then set it with `piv level you/my-app watch`.\n"), [])
        self.assertEqual(level_checks("You can check the level with:\n\n```bash\npiv doctor you/my-app\n```\n"), [])


# Fix round 2 (I-2): an event's title never reaches the agent. A link run gets the fixed title and the run link (lines.link_request),
# and the issue shows "Event: " and at most 60 characters of the title (ack.read_incident, ack.NAME_MAX). Each sentence that said
# the run reads the title, or that the title should say what happened, is gone.
TITLE_SENTENCE = ("The title is shown on the issue, up to its first 60 characters. The run does not read it: a link run gets a "
                  "fixed title and the run link, never the alert's text.")
OLD_TITLE_SENTENCES = ("so the title is what the run sees", "The title and the link are all the run sees",
                       "The title and link are all the issue carries", "so the title should say what happened")


class IncidentTitleTest(unittest.TestCase):
    def test_the_demos_and_recipes_say_the_title_is_shown_and_the_run_does_not_read_it(self):  # fix round 2, I-2
        for path in (EXAMPLES,):
            with self.subTest(file=path.name):
                self.assertIn(TITLE_SENTENCE, flat(path.read_text(encoding="utf-8")))

    def test_no_doc_says_the_run_reads_the_event_title_or_that_the_title_should_say_what_happened(self):  # fix round 2, I-2
        for path in DOCS:
            text = flat(path.read_text(encoding="utf-8"))
            for old in OLD_TITLE_SENTENCES:
                with self.subTest(file=path.name, sentence=old):
                    self.assertNotIn(old, text)


# One request is one slice: the plan job turns the request into one slice, checked against the slice rules, and hands it on.
# The preset grammar has no plan tier (engine/route.py TIERS), so a preset that names one is refused, and no guide says otherwise.
OLD_PLAN_SENTENCES = ("splits the task into slices", "is split by plan only when it has no score",
                      "the plan falls back to a single slice, and the report says so", "the plan falls back to one slice, with a `fell_back:` line")


class OneSliceTest(unittest.TestCase):
    def test_every_guide_says_one_request_is_one_slice(self):
        for path in (README, QUICKSTART, EXAMPLES):
            with self.subTest(file=path.name):
                self.assertIn("one slice", flat(path.read_text(encoding="utf-8")).lower())

    def test_no_guide_says_the_plan_job_splits_or_names_a_tier_that_is_gone(self):
        for path in DOCS:
            text = flat(path.read_text(encoding="utf-8"))
            for old in OLD_PLAN_SENTENCES + ("plan tier", "plan-tier"):
                with self.subTest(file=path.name, sentence=old):
                    self.assertNotIn(old, text.lower())


# Fix round 2 (S-b): the quiet lines the guides quote are the ones the code prints. ack and plan write the first (job.read_start);
# the report job writes the second (engine/report.py, when its QUIET is true). A quiet run starts nothing.
QUIET_INCIDENT = "quiet: the level is ask, which starts no incident"
QUIET_EVENT = "quiet: the level is ask, which starts no event"
QUIET_REPORT = "quiet: ack found the level starts no run of this kind"


class QuietLineTest(unittest.TestCase):
    def test_the_quiet_lines_the_code_prints_are_the_ones_the_guides_quote(self):  # fix round 2, S-b
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(job.read_start("ask", "npm test", "incident"))
            self.assertIsNone(job.read_start("ask", "npm test", "event"))
        self.assertEqual(err.getvalue().splitlines(), [QUIET_INCIDENT, QUIET_EVENT])
        err = io.StringIO()
        env = {"GH_REPO": "you/my-app", "KIND": "incident", "TRUSTED": "true", "QUIET": "true", "ENGINE_SHA": "a" * 40}
        with contextlib.redirect_stderr(err):
            self.assertEqual(report_job.report(env), 0)
        self.assertEqual(err.getvalue().splitlines(), [QUIET_REPORT])

    def test_the_examples_quote_both_the_ack_line_and_the_report_line_and_the_event_line(self):  # fix round 2, S-b
        examples = flat(EXAMPLES.read_text(encoding="utf-8"))
        self.assertIn(f"`{QUIET_INCIDENT}`", examples)
        self.assertIn(f"`{QUIET_REPORT}`", examples)
        self.assertNotIn("and nothing else", examples.split(QUIET_INCIDENT, 1)[1].split(".", 1)[0])
        self.assertIn(f"`{QUIET_EVENT}`", examples)
        self.assertNotIn("the log says the event starts no run", examples)


if __name__ == "__main__":
    unittest.main()