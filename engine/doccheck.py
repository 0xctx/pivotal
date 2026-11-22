"""The docs line's gate commands: links, fences and spelling of the Markdown files.

``python3 -I engine/doccheck.py [DIR]`` checks the Markdown files of DIR (default: the current directory)
and prints one ``path:line: message`` per problem, then exits 1. It exits 0 when nothing is wrong and 2 for a
bad call. The gate runs it after the repo's own check (``engine/lines.py`` GATES["docs"]).

- Files: the tracked Markdown files of a git checkout (``git ls-files -z -- '*.md'``, as paths under DIR), and the walk of
  the tree when DIR is not in a git checkout. The gate means the published docs are true, so an untracked scratch file
  (the operator's ``.sysop`` store in a local checkout) is not a doc and does not fail it; a clean CI checkout has none.
- Links: a relative ``[text](target)`` or ``[label]: target`` must name a file or directory that exists under
  DIR. A ``#fragment`` and a ``?query`` are cut off first, and the fragment is not checked. A URL with a
  scheme, a bare ``#fragment``, a ``//host`` link and a link inside a code fence or an inline code span are
  not checked: this gate makes no network call.
- Fences: a fence opened with at least three backticks or tildes must be closed by the same character, at
  least as long, with nothing after it.
- Spelling: no dictionary. A short list of common misspellings (MISSPELLED) and a word written twice in a row (not in a table row).
  Code fences and inline code are skipped.

Only regular files are read: a symlink is skipped, and a file over MAX_BYTES, or more than MAX_FILES files,
fails the check rather than passing unread.
"""

import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
MAX_FILES = 5000
SKIP_DIRS = frozenset({".git", ".sysop", "node_modules", ".venv", "__pycache__"})
GIT_LS_FILES = ("git", "ls-files", "-z", "--", "*.md")  # the tracked Markdown files, as paths relative to the cwd
GIT_ENV_DROP = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"})
READ_CHUNK = 65536
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE = re.compile(r"`+[^`]*`+")
LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]*)>?[^)\n]*\)")
REFERENCE = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*<?(\S+?)>?(?:\s.*)?$")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
WORD = re.compile(r"[A-Za-z']+")
TAG = re.compile(r"<[^<>\n]*>")
TWICE = re.compile(r"(?<![\w'])([A-Za-z]{2,})[ \t]+\1(?![\w'])", re.IGNORECASE)
TWICE_OK = frozenset({"had", "that"})  # "had had" and "that that" are English
MISSPELLED = {
    "teh": "the", "recieve": "receive", "recieved": "received", "seperate": "separate", "occured": "occurred",
    "occurence": "occurrence", "definately": "definitely", "accomodate": "accommodate", "adress": "address",
    "wich": "which", "thier": "their", "untill": "until", "enviroment": "environment", "dependancy": "dependency",
    "dependancies": "dependencies", "succesful": "successful", "succesfully": "successfully", "begining": "beginning",
    "existance": "existence", "refered": "referred", "neccessary": "necessary", "necesary": "necessary",
    "langauge": "language", "paramter": "parameter", "paramters": "parameters", "funtion": "function",
    "documention": "documentation", "documenation": "documentation", "arguement": "argument",
    "implmentation": "implementation", "overriden": "overridden", "writting": "writing", "calender": "calendar",
}


def walk_markdown(root: Path) -> Iterator[Path]:
    """The ``*.md`` files under root by walking the tree: the fallback for a directory that is not in a git checkout."""
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(n for n in names if n not in SKIP_DIRS)
        for name in sorted(files):
            if name.lower().endswith(".md"):
                yield Path(directory) / name


def plain_path(root: Path, name: str) -> Path | None:
    """A name ``git ls-files`` printed as a path under root, or None when it is not a plain relative path inside it.

    The index is repo data, not a promise: an absolute name, an empty name or one that climbs out of the tree is
    refused rather than read (F5), so the check never reads a file the tree does not hold.
    """
    if not name or os.path.isabs(name):
        return None
    local = os.path.normpath(name)
    if local == os.pardir or local.startswith(os.pardir + os.sep):
        return None
    return root / local


def tracked_markdown(root: Path, limit: int = MAX_FILES) -> list[Path] | None:
    """The tracked Markdown files under root, from ``git ls-files``, or None when root is not in a git checkout.

    At most ``limit + 1`` names are read and the child is killed past that, so a repository cannot make the check run
    on without end; ``check`` then refuses with the same message a walk of too many files gets. A name that is not a
    plain relative path inside root raises ValueError rather than being skipped.
    """
    try:
        child = subprocess.Popen([*GIT_LS_FILES], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={name: value for name, value in os.environ.items() if name not in GIT_ENV_DROP})
    except OSError:  # no git, or no directory to run it in: the walk below is the fallback
        return None
    found: list[Path] = []
    tail = b""
    stopped = ""  # the refusal of a name that is not a plain path, or ""
    full = False  # more names than the limit: the child is killed and the caller refuses
    try:
        while not stopped and not full:
            chunk = child.stdout.read(READ_CHUNK)
            if not chunk:
                break
            tail += chunk
            *whole, tail = tail.split(b"\0")
            for raw in whole:
                path = plain_path(root, os.fsdecode(raw))
                if path is None:
                    stopped = f"a tracked name is not a plain path inside the tree: {raw!r}"
                    break
                if path.name.lower().endswith(".md"):
                    found.append(path)
                    if len(found) > limit:
                        full = True
                        break
    finally:
        if stopped or full:
            child.kill()
        status = child.wait()
        child.stdout.close()
    if not stopped and not full and tail:  # git ends every name with NUL; a last name without one is a truncated answer
        stopped = f"a tracked name is not terminated: {tail[:200]!r}"
    if stopped:
        raise ValueError(stopped)
    if status != 0 and not full:
        return None  # git failed: not a repository, an unreadable index. The walk below is the fallback.
    return found


def markdown_files(root: Path) -> Iterator[Path]:
    """The Markdown files of root: the tracked ones in a git checkout, else the walk of the tree.

    Raises ValueError when the index names a path that is not a plain relative path inside root.
    """
    tracked = tracked_markdown(root)
    if tracked is not None:
        yield from tracked
        return
    yield from walk_markdown(root)


def read_text(path: Path) -> str | None:
    """The file's text, or None for a symlink or another kind of file. Raises ValueError when it is too big."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        data = fh.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"larger than {MAX_BYTES} bytes")
    return data.decode("utf-8", errors="replace")


def check_text(text: str, path: Path, root: Path) -> list[str]:
    """The problems of one file, as ``line: message`` strings."""
    problems = []
    opener: tuple[str, int, int] | None = None  # (character, length, line)
    for number, line in enumerate(text.splitlines(), 1):
        fence = FENCE.match(line)
        if opener is not None:
            if fence and fence.group(1)[0] == opener[0] and len(fence.group(1)) >= opener[1] and not fence.group(2).strip():
                opener = None
            continue
        if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
            opener = (fence.group(1)[0], len(fence.group(1)), number)
            continue
        # A code span becomes NUL, not a space: the words either side of it are not next to each other, so
        # "in `SERIAL` in" is not "in in" (the repeat check is TWICE, which needs spaces only).
        prose = INLINE.sub("\x00", line)
        targets = [m.group(1) for m in LINK.finditer(prose)]
        ref = REFERENCE.match(prose)
        if ref:
            targets.append(ref.group(1))
        for target in targets:
            problem = bad_link(target, path, root)
            if problem:
                problems.append(f"{number}: {problem}")
        prose = TAG.sub(" ", prose)
        for word in WORD.findall(prose):
            if word.lower() in MISSPELLED:
                problems.append(f"{number}: misspelled {word.lower()!r}, write {MISSPELLED[word.lower()]!r}")
        if not prose.lstrip().startswith("|"):  # a table row repeats words by design
            for twice in TWICE.finditer(prose):
                if twice.group(1).lower() not in TWICE_OK:
                    problems.append(f"{number}: the word {twice.group(1).lower()!r} is written twice")
    if opener is not None:
        problems.append(f"{opener[2]}: a code fence opened here is never closed")
    return sorted(problems, key=lambda p: int(p.split(":", 1)[0]))


def bad_link(target: str, path: Path, root: Path) -> str:
    if not target or target.startswith(("#", "//")) or SCHEME.match(target):
        return ""
    local = re.split(r"[#?]", target, maxsplit=1)[0]
    if not local:
        return ""
    base = root if local.startswith("/") else path.parent
    resolved = Path(os.path.normpath(base / local.lstrip("/")))
    if root != resolved and root not in resolved.parents:
        return f"link {target!r} leaves the repository"
    if not os.path.lexists(resolved):
        return f"link {target!r} names no file"
    return ""


def check(root: Path, out) -> int:
    root = Path(os.path.normpath(root.resolve()))
    count = bad = 0
    try:
        for path in markdown_files(root):
            count += 1
            if count > MAX_FILES:
                print(f"more than {MAX_FILES} Markdown files: not checked", file=out)
                return 1
            shown = path.relative_to(root)
            try:
                text = read_text(path)
            except ValueError as err:
                print(f"{shown}: {err}", file=out)
                bad += 1
                continue
            if text is None:
                continue
            for problem in check_text(text, path, root):
                print(f"{shown}:{problem}", file=out)
                bad += 1
    except ValueError as err:  # the index named a path that is not inside the tree
        print(f"doccheck: {err}", file=out)
        return 1
    print(f"doccheck: {count} Markdown file{'' if count == 1 else 's'}, {bad} problem{'' if bad == 1 else 's'}", file=out)
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and not Path(argv[1]).is_dir()):
        print("usage: doccheck.py [DIR]", file=sys.stderr)
        return 2
    return check(Path(argv[1] if len(argv) == 2 else "."), sys.stdout)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
