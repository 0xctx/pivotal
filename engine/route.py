"""Presets, chains, doors, vendors and failure classification (F5).

A preset maps each tier to an ordered list of entries. An entry is one worker on one
door for one model: ``<worker>:<door>/<model>[@<effort>]``, for example
``pi:kaggle/qwen3.6-35b-a3b@high``. The door is where the call goes and names the secret
it needs. The vendor is whose model it is, and decides who may review it.

Nothing here reads the environment itself: callers pass it in, so every rule is testable.
``python3 -m engine.route [PRESET.json] [--tier build|review] [--builder SPEC]`` routes one tier
from the command line, reading the preset from the file or from PIVOTAL_PRESET_JSON. ``--builder``
routes the review tier without the builder's vendor. ``--check`` runs the cross-vendor rule instead.
``python3 -m engine.route --gate-log LOG`` prints how a test log is classified: always ``work``.
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

TIERS = ("build", "review")
REQUIRED_TIERS = ("build", "review")
# "lines" holds a repo's custom build lines (engine/lines.py validates them); only PIVOTAL_PRESET_JSON may carry it.
# "measured" lists the entries T0 has measured (docs/engines.md); an entry not listed is unverified. Absent means none.
PRESET_KEYS = frozenset({"name", "summary", "kaggle", "tiers", "lines", "measured"})

# Any one of the names in a door's tuple satisfies it.
DOOR_SECRETS = {
    "anthropic": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "google": ("GEMINI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "kaggle": ("PIVOTAL_KAGGLE_KEY",),
}

# Fixed-door workers use only their own door. pi and omp reach any door.
WORKER_DOORS = {
    "claude": ("anthropic",),
    "codex": ("openai",),
    "grok": ("xai",),
    "agy": ("google",),
    "pi": tuple(DOOR_SECRETS),
    "omp": tuple(DOOR_SECRETS),
}

# Workers whose headless flags T0 measured (docs/engines.md): -p --model <id>. Others are refused by the probe and
# must not be guessed into a shipped preset until T0 measures them.
MEASURED_WORKERS = frozenset({"claude", "agy", "pi", "omp"})

# A fixed-door worker is its vendor. pi and omp take the vendor from the model id.
WORKER_VENDOR = {"claude": "anthropic", "codex": "openai", "grok": "xai", "agy": "google"}
KNOWN_VENDORS = frozenset({"alibaba", "anthropic", "deepseek", "google", "openai", "xai"})
MODEL_VENDOR_PREFIXES = (
    ("qwen", "alibaba"),
    ("gpt-", "openai"),
    ("gemini", "google"),
    ("claude", "anthropic"),
    ("grok", "xai"),
    ("deepseek", "deepseek"),
)

SPEC = re.compile(r"(?P<worker>[a-z]+):(?P<door>[a-z]+)/(?P<model>[^@\s]+)(?:@(?P<effort>[a-z0-9]+))?")

SPEC_CHARS = "A-Za-z0-9._:+/-"  # the model characters SPEC_SAFE allows; the preset error message is worded from it
SPEC_HELP = "letters, digits and . _ : / + - (and @effort)"
SPEC_SAFE = re.compile(rf"[a-z]+:[a-z]+/[{SPEC_CHARS}]{{1,100}}(?:@[a-z0-9]{{1,16}})?")
# "not installed": the worker's CLI is absent. Without a manifest it is absent when it is not on the job's PATH; with one,
# the pins decide (workers.absent), so a same-named CLI elsewhere on PATH neither makes it present nor refuses the job.
# The entry falls through visibly instead of failing with exit 127 (docs/engines.md).
SKIP_REASONS = ("missing secret", "rate-limited", "endpoint down", "rejected token", "quota", "not installed")
WORK = "work"
AGENT_ERROR = "agent_error"

# Agent CLI output that means "skip this entry". The texts must come from T0's measured CLI
# output (docs/engines.md, not yet on main), so none is guessed here. Until they land, an agent
# failure without a matching text is AGENT_ERROR.
AGENT_SKIP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = ()


def _vendor(worker: str, model: str) -> str | None:
    if worker in WORKER_VENDOR:
        return WORKER_VENDOR[worker]
    head = model.split("/", 1)[0]
    if "/" in model and head in KNOWN_VENDORS:
        return head
    for prefix, vendor in MODEL_VENDOR_PREFIXES:
        if model.startswith(prefix):
            return vendor
    return None


@dataclass(frozen=True)
class Entry:
    worker: str
    door: str
    model: str
    effort: str | None = None

    @property
    def spec(self) -> str:
        return f"{self.worker}:{self.door}/{self.model}" + (f"@{self.effort}" if self.effort else "")

    @property
    def vendor(self) -> str | None:
        return _vendor(self.worker, self.model)


def parse_entry(spec: str) -> Entry:
    match = SPEC.fullmatch(spec)
    if match is None:
        raise ValueError(f"bad entry {spec!r}: want worker:door/model[@effort]")
    worker, door = match["worker"], match["door"]
    if worker not in WORKER_DOORS:
        raise ValueError(f"unknown worker {worker!r} in {spec!r}")
    if door not in WORKER_DOORS[worker]:
        raise ValueError(f"{worker} cannot use door {door!r} in {spec!r}")
    return Entry(worker, door, match["model"], match["effort"])


def clean_spec(text: object) -> str:
    """The spec of ``text`` re-emitted from ``parse_entry``, for text that is shown to people (a message, a comment).

    One grammar: whatever ``parse_entry`` accepts, models with ``/`` and ``:`` included, narrowed to characters
    that cannot forge markdown, a mention or a control sequence, and to model paths with no empty, ``.`` or ``..``
    segment (so no ``//``, no trailing ``/``). A string that fails either raises ValueError.
    """
    if not isinstance(text, str) or SPEC_SAFE.fullmatch(text) is None:
        raise ValueError("not a route spec that is safe to show")
    entry = parse_entry(text)
    if any(part in ("", ".", "..") for part in entry.model.split("/")):
        raise ValueError("not a route spec that is safe to show")
    return entry.spec


def valid_spec(text: object) -> bool:
    """True when ``clean_spec`` accepts ``text``: the check build's result.json and the preset load use."""
    try:
        clean_spec(text)
    except ValueError:
        return False
    return True


def chain(entries: Iterable[Entry]) -> list[Entry]:
    """Order a tier so the same model through another door comes before a different model.

    Models keep the place of their first entry, and doors keep their listed order within a model.
    """
    entries = list(entries)
    first: dict[str, int] = {}
    for entry in entries:
        first.setdefault(entry.model, len(first))
    return sorted(entries, key=lambda entry: first[entry.model])


@dataclass(frozen=True)
class Preset:
    name: str
    tiers: Mapping[str, tuple[Entry, ...]]  # listed order; call chain() to order them
    kaggle_model: str | None = None  # the preset's ``kaggle.model``: the model models-up serves
    measured: frozenset[str] = frozenset()  # the entry specs T0 measured; every other entry is unverified


def parse_preset(data: object) -> Preset:
    if not isinstance(data, Mapping):
        raise ValueError("preset must be a JSON object")
    unknown = set(data) - PRESET_KEYS
    if unknown:
        raise ValueError(f"unknown preset keys: {sorted(unknown)}")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("preset needs a non-empty name")
    raw_tiers = data.get("tiers")
    if not isinstance(raw_tiers, Mapping):
        raise ValueError("preset needs a tiers object")
    unknown_tiers = set(raw_tiers) - set(TIERS)
    if unknown_tiers:
        raise ValueError(f"unknown tiers: {sorted(unknown_tiers)}")
    missing = [tier for tier in REQUIRED_TIERS if tier not in raw_tiers]
    if missing:
        raise ValueError(f"missing tiers: {missing}")
    tiers = {}
    for tier, specs in raw_tiers.items():
        if not isinstance(specs, list) or not specs or not all(isinstance(s, str) for s in specs):
            raise ValueError(f"tier {tier!r} must be a non-empty list of entry strings")
        entries = tuple(parse_entry(s) for s in specs)
        for entry in entries:
            if not valid_spec(entry.spec):  # the one grammar a result and a report may echo: refuse it here, early
                raise ValueError(f"bad entry {entry.spec!r}: the model may use {SPEC_HELP} only")
        tiers[tier] = entries
    kaggle_model = _kaggle_model(data.get("kaggle"), tiers)
    return Preset(name, tiers, kaggle_model, _measured(data.get("measured"), tiers))


def _measured(raw: object, tiers: Mapping[str, tuple[Entry, ...]]) -> frozenset[str]:
    """The ``measured`` list: entry specs that appear in a tier, each once. Absent or empty: nothing is measured."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw) or len(set(raw)) != len(raw):
        raise ValueError('measured must be a list of distinct entry strings, for example ["pi:kaggle/qwen3.6-35b-a3b"]')
    specs = {entry.spec for entries in tiers.values() for entry in entries}
    unknown = [spec for spec in raw if spec not in specs]
    if unknown:
        raise ValueError(f"measured names entries that are not in a tier: {unknown}")
    return frozenset(raw)


KAGGLE_MODEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")  # kaggle/render.py's MODEL_ID: it is what picks the catalog entry


def _kaggle_model(raw: object, tiers: Mapping[str, tuple[Entry, ...]]) -> str | None:
    """The ``kaggle.model`` of a preset: absent, or an object with one valid model id that every ``kaggle`` door entry
    serves (a different model in a chain would be asked of a session that does not run it)."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {"model"} or not isinstance(raw["model"], str) \
            or not KAGGLE_MODEL.fullmatch(raw["model"]):
        raise ValueError('kaggle must be {"model": "<catalog model id>"}')
    for entries in tiers.values():
        for entry in entries:
            if entry.door == "kaggle" and entry.model != raw["model"]:
                raise ValueError(f"{entry.spec} is not the preset's kaggle.model {raw['model']!r}")
    return raw["model"]


def load_preset(path: Path) -> Preset:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, Mapping) and "lines" in data:
        raise ValueError("a preset file has no custom lines: they belong in PIVOTAL_PRESET_JSON")
    return parse_preset(data)


def cross_vendor_problems(preset: Preset) -> list[str]:
    """Each build entry needs a review entry from a different vendor. Unknown vendors fail."""
    reviewers = preset.tiers["review"]
    problems = []
    for builder in preset.tiers["build"]:
        if builder.vendor is None:
            problems.append(f"{builder.spec}: vendor unknown, so no reviewer can be shown to differ")
        elif not any(r.vendor and r.vendor != builder.vendor for r in reviewers):
            problems.append(f"no review entry outside {builder.vendor} for {builder.spec}")
    return problems


def review_chain(preset: Preset, builder: Entry) -> list[Entry]:
    """The review tier in order, without any entry from the builder's vendor."""
    if builder.vendor is None:
        return []
    return [r for r in chain(preset.tiers["review"]) if r.vendor and r.vendor != builder.vendor]


def secret_names(entry: Entry) -> tuple[str, ...]:
    return DOOR_SECRETS[entry.door]


def model_arg(entry: Entry) -> str:
    """The --model value: pi/omp take door/model (measured: ``--model <provider/id>``), others take the model id."""
    return f"{entry.door}/{entry.model}" if entry.worker in ("pi", "omp") else entry.model


def worker_argv(entry: Entry, prompt: str) -> list[str]:
    """The CLI argv for one headless call, using only T0-measured flags (docs/engines.md: ``-p --model <id>``).

    The probe and the factory must run the same command shape; both call this. Effort is not sent: no effort flag
    is measured for these CLIs (docs/engines.md).
    """
    return [entry.worker, "-p", "--model", model_arg(entry), prompt]


Probe = Callable[[Entry], str | None]


def make_probe(
    env: Mapping[str, str],
    *,
    down: frozenset[str] = frozenset(),
    limited: frozenset[str] = frozenset(),
    rejected: frozenset[str] = frozenset(),
    quota: bool = False,
    absent: frozenset[str] = frozenset(),
) -> Probe:
    """Return the skip reason for an entry, or None when it can run.

    The secret is checked first. ``quota`` is a refused Kaggle push for this run (models-up): every
    ``kaggle`` entry is then skipped as ``quota`` and never probed, and no other door is affected. ``absent`` holds the
    workers whose CLI the job cannot run (``not installed``). The sets hold specs that failed earlier in this run.
    """

    def probe(entry: Entry) -> str | None:
        if not any(env.get(name) for name in secret_names(entry)):
            return "missing secret"
        if quota and entry.door == "kaggle":
            return "quota"
        if entry.worker in absent:
            return "not installed"
        if entry.spec in down:
            return "endpoint down"
        if entry.spec in limited:
            return "rate-limited"
        if entry.spec in rejected:
            return "rejected token"
        return None

    return probe


@dataclass(frozen=True)
class FellBack:
    """One skipped entry. A result file or verdict carries it as the dict ``as_dict`` makes (checked by ``check_fell_back``);
    ``text`` is the one line a person reads, and every printed or commented fall-through is that line."""

    source: str
    target: str  # the next entry tried, or "no_route"
    reason: str

    @classmethod
    def from_dict(cls, item: Mapping[str, str]) -> "FellBack":
        return cls(item["from"], item["to"], item["reason"])

    def as_dict(self) -> dict[str, str]:
        return {"from": self.source, "to": self.target, "reason": self.reason}

    @property
    def text(self) -> str:
        return f"fell_back: {self.source} → {self.target} ({self.reason})"


def check_fell_back(data: object, limit: int) -> tuple[FellBack, ...]:
    """The fall-through items of a result file or verdict: a list of at most ``limit`` dicts, each with exactly the keys
    from, to and reason, a route spec (``to`` may also be ``no_route``) and a reason from SKIP_REASONS. ValueError otherwise."""
    if not isinstance(data, list) or len(data) > limit:
        raise ValueError("fell_back must be a short list")
    items = []
    for item in data:
        if not isinstance(item, dict) or set(item) != {"from", "to", "reason"}:
            raise ValueError("a fell_back item has exactly from, to and reason")
        source, target, reason = item["from"], item["to"], item["reason"]
        if not valid_spec(source) or not (target == "no_route" or valid_spec(target)) or reason not in SKIP_REASONS:
            raise ValueError("a fell_back item names a route spec, a target and a skip reason")
        items.append(FellBack(source, target, reason))
    return tuple(items)


@dataclass(frozen=True)
class Route:
    status: str  # "ok", "red" or "no_route"
    entry: Entry | None
    fell_back: tuple[FellBack, ...] = ()
    reason: str = ""

    @property
    def outcome(self) -> str:
        if self.status == "ok":
            return f"ok: {self.entry.spec}"
        if self.status == "red":
            return f"red: {self.reason}"
        return "failed: no_route"


def route(entries: Iterable[Entry], probe: Probe, *, strict_first: bool = False) -> Route:
    """Walk an ordered chain and stop at the first entry that can run (F5).

    With strict_first, a first entry with no secret makes the run red instead of falling back:
    a preset that names a key it doesn't have is misconfigured, and must not degrade quietly.
    """
    skipped: list[tuple[Entry, str]] = []
    for index, entry in enumerate(entries):
        reason = probe(entry)
        if reason is None:
            return Route("ok", entry, _fell_back(skipped, entry.spec))
        if strict_first and index == 0 and reason == "missing secret":
            names = " or ".join(secret_names(entry))
            return Route("red", None, reason=f"first entry {entry.spec} has no secret: set {names}")
        skipped.append((entry, reason))
    return Route("no_route", None, _fell_back(skipped, None), "no entry in the chain could run")


def _fell_back(skipped: list[tuple[Entry, str]], final: str | None) -> tuple[FellBack, ...]:
    targets = [entry.spec for entry, _ in skipped[1:]] + [final or "no_route"]
    return tuple(FellBack(entry.spec, target, reason) for (entry, reason), target in zip(skipped, targets))


def note_failure(limited: frozenset[str], entry: Entry, reason: str) -> frozenset[str]:
    """Carry a rate-limited entry into the rest of the run, for the probe's ``limited`` set.

    Other reasons are not kept: a missing secret or a down endpoint is checked again on the next call.
    """
    if reason != "rate-limited":
        return limited
    return limited | {entry.spec}


def classify_failure(
    source: str,
    text: str,
    patterns: tuple[tuple[re.Pattern[str], str], ...] = AGENT_SKIP_PATTERNS,
) -> str:
    """Classify a failure from its own log.

    source "gate": the check's log is the work under test, so any failure in it is WORK, whatever
    the text says. A 429 in a test fixture is a test, not a rate limit.
    source "agent": only a known skip text is a skip reason. Anything else is AGENT_ERROR.
    """
    if source == "gate":
        return WORK
    if source != "agent":
        raise ValueError(f"unknown failure source {source!r}")
    for pattern, reason in patterns:
        if pattern.search(text):
            return reason
    return AGENT_ERROR


def main(argv: list[str], env: Mapping[str, str]) -> int:
    """Route one tier and print each fallback, then the outcome. Exit 0 only when a route is ok.

    With --check, print each build entry that lacks a cross-vendor reviewer, and exit 0 only when
    there are none. With --gate-log, classify a test log instead and print the result. Only secret
    names are printed, never their values.
    """
    parser = argparse.ArgumentParser(prog="python3 -m engine.route")
    parser.add_argument(
        "preset", nargs="?", type=Path, help="preset JSON file (default: $PIVOTAL_PRESET_JSON)"
    )
    parser.add_argument("--tier", default="build", choices=TIERS)
    parser.add_argument("--builder", help="with --tier review: the build entry whose vendor is excluded")
    parser.add_argument("--check", action="store_true", help="check the cross-vendor rule and exit")
    parser.add_argument("--gate-log", type=Path, metavar="LOG", help="classify a test log: prints work")
    args = parser.parse_args(argv)
    if args.gate_log is not None:
        try:
            text = args.gate_log.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            print(f"failed: bad log: {error}")
            return 1
        print(classify_failure("gate", text))
        return 0
    try:
        if args.preset is not None:
            preset = load_preset(args.preset)
        elif env.get("PIVOTAL_PRESET_JSON"):
            preset = parse_preset(json.loads(env["PIVOTAL_PRESET_JSON"]))
        else:
            print("failed: no preset: pass a file or set PIVOTAL_PRESET_JSON")
            return 1
    except (OSError, ValueError) as error:
        print(f"failed: bad preset: {error}")
        return 1
    if args.check:
        problems = cross_vendor_problems(preset)
        for problem in problems:
            print(f"failed: {problem}")
        if not problems:
            print("ok: every build entry has a review entry from another vendor")
        return 1 if problems else 0
    if args.builder is not None and args.tier != "review":
        print("failed: --builder only applies to --tier review")
        return 1
    entries = preset.tiers.get(args.tier)
    if entries is None:
        print(f"failed: preset {preset.name} has no {args.tier} tier")
        return 1
    ordered = chain(entries)
    if args.builder is not None:
        try:
            builder = parse_entry(args.builder)
        except ValueError as error:
            print(f"failed: bad builder: {error}")
            return 1
        if builder.vendor is None:
            print(f"failed: builder vendor unknown, so no review can be shown to differ: {builder.spec}")
            return 1
        ordered = review_chain(preset, builder)
    result = route(ordered, make_probe(env), strict_first=args.tier == "build")
    for fell in result.fell_back:
        print(fell.text)
    print(result.outcome)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
