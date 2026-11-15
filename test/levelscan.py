"""The AST scan that keeps the loop ladder in one place (test_levels.py holds the tests; F4).

A level word is flagged where code uses it as a decision: a comparison operand, a dict key or a display member, a
subscript key, a conditional branch, an assigned value, a keyword name, a match pattern, an index into or an iteration
over the ladder's own tuple, or a string that code builds from constants and that spells one. Help text and messages are
not flagged: a level word that is a plain argument of a call, or a part of a longer string, is data.

Concatenation is never a way past the scan. The spelled strings of an expression are worked out from its constants:
``+``, ``%``, f-strings, ``str.join`` and ``str.format`` over constants, names bound to such values (every binding in the
module, ``+=`` included, flow-insensitively), and both arms of a conditional. A composite or a name whose spelled strings
include a level word is a finding wherever it sits.

Limits, so nobody reads more into it: a value that comes from another module, a parameter or a runtime source is not
joined; the spelled strings are capped per name. The behaviour tests in test_levels.py are the other half of the guard.

Each rule is one function in RULES. A rule's negative fixture in test_levels.py fails when that rule is removed.

Exemptions are exact and given by the caller, never found by the scan. ``allowed`` holds (file, table, key) triples of a
module-level table entry. ``roles`` holds (file, role) pairs for the three shapes of _exempt_roles: the other side of a
two-operand comparison, the target of a single assignment of a constant, and a name passed to a call. A role exempts
nothing else, so a level compared in the same file, under any other role, is still flagged.
"""

import ast
import itertools
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

DECIDER = "job.py"  # engine/job.py, at the top of engine/: the one module that decides the ladder
CAP = 512  # spelled strings kept per expression
COMBOS = 4096  # combinations examined per product


class Ctx:
    def __init__(self, tree: ast.AST, words: frozenset[str], exempt: frozenset[int], exempt_names: frozenset[int] = frozenset()):
        self.words = words
        self.exempt = exempt
        self.exempt_names = exempt_names
        self.env = _spelled_names(tree)

    def plain(self, node) -> bool:
        """A constant that is exactly a level word and is not one of the allowed table entries."""
        return (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in self.words
                and id(node) not in self.exempt)

    def spells_level(self, text: str) -> bool:
        """The word itself, or every level word in one string (a list written as text)."""
        parts = text.replace(",", " ").split()
        return text in self.words or (len(parts) == len(self.words) and set(parts) == self.words)

    def spelled(self, node) -> set[str]:
        return _spelled(node, self.env)


Rule = Callable[[ast.AST, Ctx], Iterable[int]]


def _is_levels(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "LEVELS") or (isinstance(node, ast.Name) and node.id == "LEVELS")


def _mentions_levels(node) -> bool:
    return any(_is_levels(part) for part in ast.walk(node))


# ---------------------------------------------------------------------------------------------------------- the rules

def all_words_constant(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """One string that is every level word, such as ``"watch ask prepare land".split()``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and ctx.spells_level(node.value) and node.value not in ctx.words:
        yield node.lineno


def compare(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    if isinstance(node, ast.Compare):
        for operand in (node.left, *node.comparators):
            if ctx.plain(operand):
                yield operand.lineno


def display(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """A dict key, or a member of a tuple, list or set display."""
    if isinstance(node, ast.Dict):
        members = [key for key in node.keys if key is not None]
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        members = node.elts
    else:
        return
    for member in members:
        if ctx.plain(member):
            yield member.lineno


def subscript_key(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """``RANK["prepare"]``: a level word as the key of a subscript."""
    if isinstance(node, ast.Subscript) and ctx.plain(node.slice):
        yield node.slice.lineno


def conditional(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """``"land" if cond else "ask"``: a level word as one arm of a conditional expression."""
    if isinstance(node, ast.IfExp):
        for arm in (node.body, node.orelse):
            if ctx.plain(arm):
                yield arm.lineno


def assigned(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """``level = "land"``, ``LAND: str = "land"``, ``(x := "land")``."""
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None and ctx.plain(node.value):
        yield node.value.lineno


def keyword_name(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """``dict(prepare=2, land=3)``: a level word as a keyword name (the value of ``f(level="land")`` is data)."""
    if isinstance(node, ast.keyword) and node.arg in ctx.words:
        yield node.lineno


def match_pattern(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """``match level: case "land":``, ``case land:`` (a capture) and ``case C(land=...)``."""
    if isinstance(node, ast.Match) and ctx.plain(node.subject):
        yield node.subject.lineno
    if isinstance(node, ast.match_case):
        for part in ast.walk(node.pattern):
            if ctx.plain(part):
                yield part.lineno
            elif isinstance(part, ast.MatchAs) and part.name in ctx.words:
                yield part.lineno
            elif isinstance(part, ast.MatchClass) and set(part.kwd_attrs) & ctx.words:
                yield part.lineno


def levels_subscript(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """``job.LEVELS[3]``, ``LEVELS[-1]``: any index into the ladder's tuple."""
    if isinstance(node, ast.Subscript) and _is_levels(node.value):
        yield node.lineno


def levels_index(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """``job.LEVELS.index("prepare")``: a level word passed to a method of the ladder's tuple."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _is_levels(node.func.value):
        for arg in node.args:
            if ctx.plain(arg):
                yield arg.lineno


def levels_as_data(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """The ladder used as data: ``enumerate(job.LEVELS)``, ``dict(zip(LEVELS, ...))``, ``for w in LEVELS``, a comprehension
    over it, an alias ``L = job.LEVELS`` and ``from engine.job import LEVELS``."""
    if isinstance(node, ast.Call):
        for arg in node.args:
            if _mentions_levels(arg):
                yield arg.lineno
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        if _mentions_levels(node.iter):
            yield node.iter.lineno
    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None and _mentions_levels(node.value):
        yield node.value.lineno
    elif isinstance(node, ast.ImportFrom) and any(alias.name == "LEVELS" for alias in node.names):
        yield node.lineno


def spelled_level(node: ast.AST, ctx: Ctx) -> Iterable[int]:
    """A string built from constants that spells a level word: ``"la" + "nd"``, ``f"{'la'}nd"``, ``"".join(...)``, a name
    bound to one, and every arm of a conditional that does. Constants alone are the rules above. A name an allowed role
    exempts is data only where it is passed to a call (``ctx.exempt_names``); compared or keyed, it is flagged."""
    if isinstance(node, ast.Name) and id(node) in ctx.exempt_names:
        return
    if isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call, ast.Name)) and not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)):
        if any(ctx.spells_level(text) for text in ctx.spelled(node)):
            yield node.lineno


RULES: tuple[Rule, ...] = (
    all_words_constant, compare, display, subscript_key, conditional, assigned, keyword_name, match_pattern,
    levels_subscript, levels_index, levels_as_data, spelled_level,
)


# ------------------------------------------------------------------------------------------------- the string folding

def _spelled(node, env: dict[str, set[str]], depth: int = 0) -> set[str]:
    """The strings ``node`` can be, from its constants alone. Empty when a part is not known."""
    if depth > 40:
        return set()
    d = depth + 1
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.Name):
        return env.get(node.id, set())
    if isinstance(node, ast.IfExp):
        return _spelled(node.body, env, d) | _spelled(node.orelse, env, d)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _product(lambda a, b: a + b, _spelled(node.left, env, d), _spelled(node.right, env, d))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        parts = [_spelled(arg, env, d) for arg in args]
        return _product(lambda fmt, *values: _format(fmt, values), _spelled(node.left, env, d), *parts)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append({part.value})
            elif isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None:
                parts.append(_spelled(part.value, env, d))
            else:
                return set()
        return _product(lambda *texts: "".join(texts), *parts) if parts else {""}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not node.keywords:
        base = _spelled(node.func.value, env, d)
        if node.func.attr == "join" and len(node.args) == 1 and isinstance(node.args[0], (ast.List, ast.Tuple)):
            items = [_spelled(elt, env, d) for elt in node.args[0].elts]
            if base and all(items):
                return _product(lambda sep, *parts: sep.join(parts), base, *items)
        if node.func.attr == "format":
            args = [_spelled(arg, env, d) for arg in node.args]
            if base and all(args):
                return _product(lambda fmt, *values: _format(fmt, values, method=True), base, *args)
    return set()


def _format(fmt: str, values, method: bool = False) -> str:
    """``fmt % values`` or ``fmt.format(*values)``; a format that does not fit its values spells nothing."""
    try:
        return fmt.format(*values) if method else fmt % tuple(values)
    except (IndexError, KeyError, TypeError, ValueError):
        return ""


def _product(combine, *sets: set[str]) -> set[str]:
    """Every ``combine(*choice)`` for one string from each set. A product past COMBOS choices is cut (a named limit)."""
    if not all(sets):
        return set()
    out: set[str] = set()
    for count, choice in enumerate(itertools.product(*sets)):
        if count >= COMBOS:
            break
        out.add(combine(*choice))
    return set(sorted(out)[:CAP])


def _bindings(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """Every value a plain name is bound to in the module. ``x += v`` binds ``x + v``."""
    out: dict[str, list[ast.AST]] = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id].append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None and isinstance(node.target, ast.Name):
            out[node.target.id].append(node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add) and isinstance(node.target, ast.Name):
            out[node.target.id].append(ast.BinOp(left=ast.Name(id=node.target.id, ctx=ast.Load()), op=ast.Add(), right=node.value))
    return out


def _spelled_names(tree: ast.AST) -> dict[str, set[str]]:
    """The spelled strings of each name, as a fixed point over every binding of the module (names may refer to each other)."""
    bindings = _bindings(tree)
    env: dict[str, set[str]] = {name: set() for name in bindings}
    for _ in range(64):
        changed = False
        for name, values in bindings.items():
            new: set[str] = set()
            for value in values:
                new |= _spelled(value, env)
            new = set(sorted(new)[:CAP])
            if new != env[name]:
                env[name], changed = new, True
        if not changed:
            break
    return env


# -------------------------------------------------------------------------------------------------------- the entry

def _table_members(value: ast.AST) -> list[ast.AST]:
    """The keys of a dict display, or the members of a set, tuple or list display, through ``frozenset(...)`` and kin."""
    if isinstance(value, ast.Dict):
        return [key for key in value.keys if key is not None]
    if isinstance(value, (ast.Set, ast.Tuple, ast.List)):
        return list(value.elts)
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("frozenset", "set", "tuple") and len(value.args) == 1:
        return _table_members(value.args[0])
    return []


def _exempt_entries(tree: ast.AST, filename: str, allowed: frozenset[tuple[str, str, str]]) -> frozenset[int]:
    """The constants that are an allowed (file, table, key) triple: a module-level table, and the exact key in it."""
    exempt: set[int] = set()
    for stmt in getattr(tree, "body", []):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            table = stmt.targets[0].id
            for member in _table_members(stmt.value):
                if isinstance(member, ast.Constant) and isinstance(member.value, str) and (filename, table, member.value) in allowed:
                    exempt.add(id(member))
    return frozenset(exempt)


def _exempt_roles(tree: ast.AST, filename: str, roles: frozenset[tuple[str, str]]) -> tuple[frozenset[int], frozenset[int]]:
    """The constants and the names that an allowed (file, role) pair exempts. A role is the source text of the other side of
    a two-operand comparison (``decision.kind == "land"`` has role ``decision.kind``), the target of a single assignment of a
    constant (``LAND_ARTIFACT = "land"`` has role ``LAND_ARTIFACT``), or a name passed to a call (``download(..., LAND_ARTIFACT)``).
    Only those three shapes exempt anything; a role matches its file exactly, and ``level == "land"`` has role ``level``."""
    named = {role for role_file, role in roles if role_file == filename}
    if not named:
        return frozenset(), frozenset()
    constants: set[int] = set()
    names: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.comparators) == 1:
            left, right = node.left, node.comparators[0]
            for constant, other in ((left, right), (right, left)):
                if isinstance(constant, ast.Constant) and isinstance(constant.value, str) and ast.unparse(other) in named:
                    constants.add(id(constant))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(getattr(node, "value", None), ast.Constant):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and ast.unparse(targets[0]) in named:
                constants.add(id(node.value))
        elif isinstance(node, ast.Call):
            for arg in (*node.args, *(kw.value for kw in node.keywords)):
                if isinstance(arg, ast.Name) and arg.id in named:
                    names.add(id(arg))
    return frozenset(constants), frozenset(names)


def level_word_uses(source: str, words: Iterable[str], filename: str = "", allowed: frozenset[tuple[str, str, str]] = frozenset(),
                    rules: tuple[Rule, ...] | None = None, roles: frozenset[tuple[str, str]] = frozenset()) -> list[int]:
    """The line numbers of ``source`` that use a level word as a decision (see the module docstring). ``allowed`` holds the
    exact (file, table, key) entries of a module-level table; ``roles`` the exact (file, role) pairs of _exempt_roles."""
    tree = ast.parse(source, filename=filename or "<engine>")
    role_constants, role_names = _exempt_roles(tree, filename, roles)
    ctx = Ctx(tree, frozenset(words), _exempt_entries(tree, filename, allowed) | role_constants, role_names)
    found: set[int] = set()
    for node in ast.walk(tree):
        for rule in (RULES if rules is None else rules):
            found.update(rule(node, ctx))
    return sorted(found)


def engine_findings(root: Path, words: Iterable[str], allowed: frozenset[tuple[str, str, str]] = frozenset(),
                    roles: frozenset[tuple[str, str]] = frozenset()) -> dict[str, list[int]]:
    """Every module under ``root``, subdirectories included, except the decider at its top: relative path -> flagged lines."""
    found: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or rel == DECIDER:
            continue
        lines = level_word_uses(path.read_text(encoding="utf-8"), words, rel, allowed, roles=roles)
        if lines:
            found[rel] = lines
    return found
