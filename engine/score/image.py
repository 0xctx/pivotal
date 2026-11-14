"""The mirror scorer: the SSIM of a headless browser's screenshot against a mockup, printed as one number.

Run by the gate as ``python3 -I <engine>/engine/score/image.py ARGS`` (the command is anchored to the engine checkout by
engine/buildjob.py, so a repo's own copy of this file is never run). Python stdlib only. Its inputs are not supplied by any
job yet: lines.check_wired refuses the mirror line until they are (lines.SCORER_INPUTS, lines.SUPPLIED_INPUTS).

Arguments (all required except --wait): ``--assets`` (the pivotal-assets checkout, an absolute real directory), ``--mockup``
(the mockup inside it, a relative path with no link and no ``..``), ``--browser`` (the headless browser CLI, an absolute
path), ``--start`` (the app's start command: its program at an absolute path, run without a shell, in ``--repo``),
``--url`` (the app's loopback http URL with a port), ``--repo`` (the app's directory, an absolute real directory), and
``--wait`` (seconds, 1 to 120, default 60).

The browser CLI contract: ``<browser> URL WIDTH HEIGHT``, run with the mockup's size. Exit 0 and a PNG on its STDOUT are a
screenshot; any other exit is a refusal. Its stderr is discarded. The scorer never gives the browser, or the app, a path to
write the screenshot to. The browser's stdout is a PIPE that the scorer reads, without blocking, while the browser runs (at
most MAX_FILE + 1 bytes, with a deadline). The strict framing (``decode_png``: the signature first, exactly one PNG, nothing
after IEND, the size the header says) refuses bytes before the signature, bytes that do not frame as PNG chunks, bytes after IEND, and a second PNG (a chunk with a valid CRC between IHDR and IDAT is accepted, and the pixels stay the browser's).
The old rewrite of a temporary file through /proc has no path left to reach: the screenshot is never a file.

What the framing does NOT stop. The app runs under the SAME uid as the scorer, so it can read the browser's PNG out of the pipe
itself (SIGSTOP the scorer, take the PNG, write its own), and a valid PNG it writes is scored. A setsid daemon the app leaves
behind is stopped when the scorer exits normally or on SIGTERM, SIGINT or SIGHUP; a scorer killed with SIGKILL runs no
teardown, and such a daemon survives it and can rewrite the score file after the scorer has exited. The defense is a different
uid for the app, with the scorer (or the job that runs it) still able to tear the app down under that uid. The factory must
set that before any live mirror run: until then the mirror line is refused at plan.

What it does, in order, and each step refuses (exit 1, nothing on stdout) on any failure:

1. Checks its arguments, and decodes the mockup (an 8-bit, non-interlaced PNG; see ``decode_png``). A bad mockup stops here,
   before anything runs.
2. Probes ``--url``: if anything already answers, it refuses (the app would not be the one scored). Then it starts the app in its
   own session and waits until the URL answers (``--wait`` seconds at most). An app that exits first, or never answers, is a
   refusal.
3. Runs the browser (above) while the app is up, and reads its stdout through the pipe. The app must still be running when
   the browser exits.
4. Stops the app's whole tree (its process group, every process descended from it, and every process whose parent chain leads
   to the scorer, see ``_stop_tree``) and reaps it, on every path (a SIGTERM, SIGINT or SIGHUP included: each raises SystemExit
   so this teardown runs). Only after that is the screenshot decoded.
   The spawn of the app and of the browser is inside a signal block (``_signals_held``): a SIGTERM, SIGINT or SIGHUP that lands
   right after a spawn is delivered once the child is bound, so it is stopped. The app and the browser start in their own
   session, so the job's group kill does not reach them, and each has PR_SET_PDEATHSIG set to SIGKILL: when the scorer dies,
   however it dies, the kernel kills both. The scorer is a child subreaper (PR_SET_CHILD_SUBREAPER), so an orphan of the app
   comes back to it and is killed at the app's teardown. SIGKILL cannot be caught, so a scorer killed with SIGKILL runs no
   teardown: a descendant that is not a direct child of the scorer (a setsid child of the app, say) is then left to the kernel,
   not stopped.
5. Decodes the screenshot: a refusal if it is not one PNG (bytes after its end chunk are refused), if its size is not the
   mockup's, or if its image data is not the size its header says (the inflate is capped at that size: a bomb is refused).
6. Computes the SSIM (``ssim``) and prints it.

The score is printed as one number in [0, 1] with at most six decimals (SSIM below 0 is 0; ``format_score``). Nothing is read
from the environment, the working directory or /tmp, and no default number exists.

SSIM: 7 x 7 uniform windows at every position (Wang et al. 2004 with a uniform window in place of the Gaussian 11 x 11), on
8-bit luma with C1 = (0.01 * 255)^2 and C2 = (0.03 * 255)^2. The sums are exact integers, so identical images give exactly 1.
Deviation from the paper's plain mean: only the windows where the MOCKUP has structure (its variance above STRUCTURE, a named
constant) are averaged. A flat window (a uniform white page, say) is not scored, so a blank page against a sparse mockup is
not the 0.99 that a plain mean gives. A mockup with no structured window is refused.

Time: the work is linear in the pixels, and pure Python. These figures are one host's: the build host (CPython 3.13.6, one core;
a worst case of two RGBA images with Paeth rows, the most the cap allows). At MAX_PIXELS (2,000,000 px, 1600 x 1250) the two
decodes take 5.2 s and 5.1 s and the SSIM 4.5 s, 14.8 s in all. A typical 1280 x 800 page (1,024,000 px) takes 8.2 s on that
host, and the review host reported a worst case of about 17 s at the cap. At the earlier cap of 4,200,000 px (2560 x 1640) the
same work took 35.4 s, which is why the cap is 2,000,000. Nothing else bounds the scorer's own work (the app's and the browser's
deadlines are separate), so the cap is the bound on it.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import http.client
import os
import select
import shlex
import signal
import stat
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from pathlib import Path

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}  # PNG colour type -> channels per pixel (8-bit)
MAX_PIXELS = 2_000_000  # the most pixels any one PNG may have (the mockup and the screenshot alike); see Time in the docstring
MAX_FILE = 64 * 1024 * 1024  # bytes read of any image file
WINDOW = 7
N = WINDOW * WINDOW
C1 = (0.01 * 255) ** 2
C2 = (0.03 * 255) ** 2
STRUCTURE = 1.0  # a mockup window whose variance (8-bit luma, squared) is at most this is flat: it is not scored (see ssim)
DEFAULT_WAIT = 60
MAX_WAIT = 120
BROWSER_SECONDS = 120
LOOPBACK = ("127.0.0.1", "localhost")


class Refused(Exception):
    """A refusal: the message names the rule (stderr), and the scorer exits 1 with nothing on stdout."""


# ---- PNG ----


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _luma(r: int, g: int, b: int) -> int:
    return (299 * r + 587 * g + 114 * b + 500) // 1000


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """(width, height, gray): the luma of an 8-bit, non-interlaced PNG, width * height bytes in row order.

    Colour types 0, 2, 3, 4 and 6 are read; a grey-with-alpha or RGBA pixel is composited over white, a palette index is
    looked up in PLTE (tRNS is not applied). Each chunk's checksum is verified. Anything else raises ValueError with a rule.
    """
    if data[:8] != SIGNATURE:
        raise ValueError("not a PNG file")
    pos, ihdr, palette, idat = 8, None, None, []
    while True:
        if pos + 12 > len(data):
            raise ValueError("PNG is truncated before its end chunk")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        end = pos + 8 + length
        if end + 4 > len(data):
            raise ValueError("PNG is truncated inside a chunk")
        body = data[pos + 8:end]
        (crc,) = struct.unpack(">I", data[end:end + 4])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise ValueError("PNG chunk checksum does not match")
        pos = end + 4
        if kind == b"IHDR":
            ihdr = body
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            if pos != len(data):
                raise ValueError("PNG has bytes after its end chunk")
            break
    if ihdr is None or len(ihdr) != 13 or not idat:
        raise ValueError("PNG has no header or no image data")
    width, height, depth, colour, compression, method, interlace = struct.unpack(">IIBBBBB", ihdr)
    if width < 1 or height < 1 or width * height > MAX_PIXELS:
        raise ValueError("PNG size is outside what the scorer reads")
    if depth != 8:
        raise ValueError("only 8-bit PNG is read (this one is not)")
    if colour not in CHANNELS:
        raise ValueError("PNG colour type is not one the scorer reads")
    if compression != 0 or method != 0:
        raise ValueError("PNG compression or filter method is not the standard one")
    if interlace != 0:
        raise ValueError("interlaced PNG is not read")
    lut = None
    if colour == 3:
        if palette is None or not palette or len(palette) % 3:
            raise ValueError("palette PNG has no usable palette")
        lut = [_luma(palette[i], palette[i + 1], palette[i + 2]) for i in range(0, len(palette), 3)]
    channels = CHANNELS[colour]
    stride = width * channels
    expected = height * (stride + 1)  # each row: a filter byte and its pixels
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(b"".join(idat), expected + 1)  # never more than one byte past the header's size
    except zlib.error:
        raise ValueError("PNG image data does not inflate") from None
    if len(raw) != expected or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise ValueError("PNG image data is not the size its header says")
    gray = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        kind = raw[y * (stride + 1)]
        line = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        cur = bytearray(stride)
        if kind == 0:
            cur[:] = line
        elif kind == 1:
            for i in range(stride):
                cur[i] = (line[i] + (cur[i - channels] if i >= channels else 0)) & 0xFF
        elif kind == 2:
            for i in range(stride):
                cur[i] = (line[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = cur[i - channels] if i >= channels else 0
                cur[i] = (line[i] + (left + prev[i]) // 2) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = cur[i - channels] if i >= channels else 0
                upper_left = prev[i - channels] if i >= channels else 0
                cur[i] = (line[i] + _paeth(left, prev[i], upper_left)) & 0xFF
        else:
            raise ValueError("PNG row has an unknown filter")
        gray += _row_gray(cur, colour, lut, width)
        prev = cur
    return width, height, bytes(gray)


def _row_gray(cur: bytearray, colour: int, lut: list[int] | None, width: int) -> bytes:
    if colour == 0:
        return bytes(cur)
    if colour == 4:
        return bytes((v * a + 255 * (255 - a) + 127) // 255 for v, a in zip(cur[0::2], cur[1::2]))
    if colour == 2:
        return bytes(_luma(r, g, b) for r, g, b in zip(cur[0::3], cur[1::3], cur[2::3]))
    if colour == 6:
        out = bytearray()
        for i in range(0, len(cur), 4):
            r, g, b, a = cur[i], cur[i + 1], cur[i + 2], cur[i + 3]
            out.append(_luma((r * a + 255 * (255 - a) + 127) // 255, (g * a + 255 * (255 - a) + 127) // 255,
                             (b * a + 255 * (255 - a) + 127) // 255))
        return bytes(out)
    # colour type 3: a palette index per pixel
    if lut is None or any(i >= len(lut) for i in cur):
        raise ValueError("palette PNG uses an index outside its palette")
    return bytes(lut[i] for i in cur)


# ---- SSIM ----


def _window_row(row_a, row_b, width: int):
    """For each window column: the sums over its N pixels of a, b, a*a, b*b and a*b (exact integers)."""
    sa, sb, saa, sbb, sab = [0], [0], [0], [0], [0]
    ta = tb = taa = tbb = tab = 0
    for x, y in zip(row_a, row_b):
        ta += x
        tb += y
        taa += x * x
        tbb += y * y
        tab += x * y
        sa.append(ta)
        sb.append(tb)
        saa.append(taa)
        sbb.append(tbb)
        sab.append(tab)
    cols = range(width - WINDOW + 1)
    return tuple([p[i + WINDOW] - p[i] for i in cols] for p in (sa, sb, saa, sbb, sab))


def _ssim_window(sa: int, sb: int, saa: int, sbb: int, sab: int) -> float:
    ma, mb = sa / N, sb / N
    va = saa / N - ma * ma
    vb = sbb / N - mb * mb
    cov = sab / N - ma * mb
    return ((2 * ma * mb + C1) * (2 * cov + C2)) / ((ma * ma + mb * mb + C1) * (va + vb + C2))


def ssim(width: int, height: int, a: bytes, b: bytes) -> float:
    """The mean SSIM of a (the screenshot) against b (the mockup), over the 7 x 7 windows where the MOCKUP has structure.

    A window whose mockup variance is at most STRUCTURE is flat and is not averaged: a page of uniform white windows would
    otherwise dominate the mean (a blank page against a mockup with one black square scored 0.99). A mockup with no structured
    window at all is refused, never scored."""
    if width < WINDOW or height < WINDOW:
        raise ValueError(f"images must be at least {WINDOW} x {WINDOW} pixels")
    if len(a) != width * height or len(b) != width * height:
        raise ValueError("the two images are not the same size")
    total, count = 0.0, 0
    window: deque = deque()
    sums = None
    for y in range(height):
        row = _window_row(a[y * width:(y + 1) * width], b[y * width:(y + 1) * width], width)
        if sums is None:
            sums = [list(col) for col in row]
        else:
            sums = [[s + r for s, r in zip(col, add)] for col, add in zip(sums, row)]
        window.append(row)
        if len(window) > WINDOW:
            old = window.popleft()
            sums = [[s - o for s, o in zip(col, sub)] for col, sub in zip(sums, old)]
        if len(window) == WINDOW:
            for cols in zip(*sums):
                sa_, sb_, saa_, sbb_, sab_ = cols
                mb = sb_ / N
                if sbb_ / N - mb * mb <= STRUCTURE:
                    continue  # flat in the mockup: nothing to compare
                total += _ssim_window(*cols)
                count += 1
    if count == 0:
        raise ValueError("the mockup has no structure to compare: every window is flat")
    return total / count


def format_score(value: float) -> str:
    """The printed form: one number in [0, 1], at most six decimals, no trailing zeros."""
    text = f"{min(max(value, 0.0), 1.0):.6f}".rstrip("0").rstrip(".")
    return text or "0"


# ---- the files and the app ----


def _absolute_dir(path: str, what: str) -> Path:
    if not os.path.isabs(path):
        raise Refused(f"{what} must be an absolute path")
    try:
        st = os.lstat(path)
    except OSError:
        raise Refused(f"{what} does not exist") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{what} must be a real directory, not a link")
    return Path(path)


def _read_regular(path: str, what: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Refused(f"{what} is missing, or a link") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{what} is not a regular file")
        data = os.read(fd, MAX_FILE + 1)
    except OSError:
        raise Refused(f"{what} could not be read") from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE:
        raise Refused(f"{what} is larger than the scorer reads")
    return data


def _mockup_path(assets: Path, name: str) -> str:
    parts = name.split("/")
    if not name or name.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\x00" in name:
        raise Refused("the mockup must be a relative path inside the assets, with no '..'")
    current = assets
    for part in parts:
        current = current / part
        try:
            st = os.lstat(current)
        except OSError:
            raise Refused("the mockup is missing") from None
        if stat.S_ISLNK(st.st_mode):
            raise Refused("the mockup path has a link on the way: refused")
    if not stat.S_ISREG(st.st_mode):
        raise Refused("the mockup is not a regular file")
    if not os.path.realpath(current).startswith(os.path.realpath(assets) + os.sep):
        raise Refused("the mockup path escapes the assets")
    return str(current)


STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
STOP_PASSES = 100  # the bound on the kill passes in _stop_tree (each pass is about 0.05 s)
STOP_PAUSE = 0.05


@contextlib.contextmanager
def _signals_held():
    """Blocks the stop signals for the body, and yields the mask the scorer had before (the children get that mask back).

    A stop signal that arrives in the block is delivered when the block ends, after the body has bound what it started, so
    no child can be spawned and left unnamed by a SIGTERM that lands between the fork and the assignment."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        yield old
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


PR_SET_PDEATHSIG = 1  # linux/prctl.h: the signal a child gets when its parent dies, however it dies
PR_SET_CHILD_SUBREAPER = 36  # an orphan of a descendant re-parents to this process, not to init
_LIBC: ctypes.CDLL | None = None


def _libc() -> ctypes.CDLL:
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(None, use_errno=True)
    return _LIBC


def _become_subreaper() -> None:
    """The scorer takes its orphans (see _stop_tree, sweep). A refusal if the kernel will not: the sweep is part of the teardown."""
    if _libc().prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise Refused("the scorer cannot take the orphans of its app (prctl): refused")


def _popen(argv: list[str], mask: set[int], *, cwd: Path | None = None, stdout=subprocess.DEVNULL) -> subprocess.Popen:
    """A process in its own session (its pid is its process group id), with the scorer's signal mask before the block.

    The child is SIGKILLed when the scorer dies (PR_SET_PDEATHSIG), however the scorer dies. The parent is checked after
    the signal is set: a scorer that died before that is seen here, and the child exits instead of running unguarded. A
    child that cannot get the signal exits too: the app is never started without it."""
    libc, parent = _libc(), os.getpid()

    def child_setup() -> None:
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
            os._exit(1)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)

    return subprocess.Popen(argv, cwd=None if cwd is None else str(cwd), stdin=subprocess.DEVNULL, stdout=stdout,
                            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, preexec_fn=child_setup)


def _proc_table() -> dict[int, tuple[int, int, int, str]]:
    """pid -> (parent pid, process group, start time, state) for every process /proc lists (Linux; stdlib only)."""
    table = {}
    with os.scandir("/proc") as entries:
        names = [entry.name for entry in entries if entry.name.isdigit()]
    for name in names:
        try:
            with open(f"/proc/{name}/stat", encoding="ascii", errors="replace") as fh:
                text = fh.read()
            fields = text.rsplit(")", 1)[1].split()  # the command name may hold a ')': split after the last one
            table[int(name)] = (int(fields[1]), int(fields[2]), int(fields[19]), fields[0])
        except (OSError, IndexError, ValueError):
            continue  # it exited while it was read
    return table


def _descendants(root: int, table: dict) -> set[int]:
    """The processes descended from ``root`` through the parent links (a process whose parent died is not among them)."""
    children: dict[int, list[int]] = {}
    for pid, entry in table.items():
        children.setdefault(entry[0], []).append(pid)
    found: set[int] = set()
    todo = [root]
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in found:
                found.add(child)
                todo.append(child)
    found.discard(root)
    return found


def _tree_of(root: int, table: dict) -> set[int]:
    """The processes descended from ``root`` through the parent links, and those in its process group."""
    found = _descendants(root, table)
    found.update(pid for pid, entry in table.items() if entry[1] == root)
    found.discard(root)
    return found


def _live(table: dict, pid: int, start: int) -> bool:
    """Whether ``pid`` is still the process that started at ``start`` (a reused pid is not), and is not a zombie."""
    entry = table.get(pid)
    return entry is not None and entry[2] == start and entry[3] != "Z"


def _stop_tree(proc: subprocess.Popen, *, sweep: bool = False) -> None:
    """SIGKILL a started process's whole tree, then reap it.

    The tree is the process group (the process's own session) and every process descended from the process. The
    descendants are found through the parent links while the process is alive and remembered by start time, so a child
    that left the group (setsid) and was reparented when its parent died is still killed. With ``sweep`` (the app, once the
    browser is done; the scorer is a child subreaper, see _become_subreaper) every process whose parent chain leads to the
    scorer is in the tree as well: an orphan of the app is killed here, after the app has exited too.

    Each pass scans the process table, then signals the group, whatever the scan found: a scan that fails (OSError) knows
    no descendants in that pass, and a stop that lands in the scan (SystemExit) is raised again only after the teardown has
    finished. The group is not signalled before the scan on purpose: once the process is dead its children are reparented,
    and a child that is only found through its parent's link would be missed. Passes repeat until no member of the tree is
    left (a zombie counts as gone), and are bounded: a tree that will not stop, or a teardown that cannot be checked, is a
    refusal."""
    known: dict[int, int] = {}  # pid -> start time
    state: dict = {"interrupted": None}
    for _ in range(STOP_PASSES):
        table = _scan_for_stop(state)
        if table is not None:
            _remember(proc, table, known, sweep)
        _signal_group(proc.pid)
        if table is not None:
            _kill_known(table, known)
        time.sleep(STOP_PAUSE)
        table = _scan_for_stop(state)
        if table is None:
            continue
        _remember(proc, table, known, sweep)
        group_left = any(entry[1] == proc.pid and entry[3] != "Z" for entry in table.values())
        if proc.poll() is not None and not group_left and not any(_live(table, p, s) for p, s in known.items()):
            _raise_interrupted(state)
            return
    _raise_interrupted(state)
    raise Refused("a process of the run did not stop")


def _scan_for_stop(state: dict) -> dict | None:
    """The process table for one stop pass, or None. A failed scan knows nothing this pass; a stop that lands in the scan is
    kept in ``state`` (the first one), so the teardown can finish before it is raised."""
    try:
        return _proc_table()
    except OSError:
        return None
    except SystemExit as err:
        if state["interrupted"] is None:  # the first stop is the one raised
            state["interrupted"] = err
        return None


def _raise_interrupted(state: dict) -> None:
    if state["interrupted"] is not None:
        raise state["interrupted"]


def _remember(proc: subprocess.Popen, table: dict, known: dict[int, int], sweep: bool) -> None:
    """Add the processes of a stop to ``known`` (pid -> start time). The tree of a live process is found through the parent
    links, which lead to it only while it is alive; with ``sweep`` every process descended from the scorer is added."""
    found: set[int] = set()
    if proc.poll() is None:
        found |= _tree_of(proc.pid, table)
    if sweep:
        found |= _descendants(os.getpid(), table)
    known.update({pid: table[pid][2] for pid in found if pid in table})


def _kill_known(table: dict, known: dict[int, int]) -> None:
    for pid, start in known.items():
        if _live(table, pid, start):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def _signal_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _answers(url: str) -> bool:
    """Whether anything answers ``url`` (an HTTP status of any kind counts). No proxy: the URL is loopback."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=2):
            return True
    except urllib.error.HTTPError:
        return True  # it answered (with an error status): a server is up
    except (urllib.error.URLError, OSError):
        return False  # nothing listens, or the connection was refused or reset
    except http.client.HTTPException:
        # Something answered with bytes that are not HTTP (a BadStatusLine, say): a named refusal, never an uncaught exception.
        raise Refused("the URL answered with bytes that are not HTTP") from None


def _wait_for_app(proc: subprocess.Popen, url: str, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while True:
        if proc.poll() is not None:
            raise Refused("the app exited before it answered")
        if _answers(url):
            if proc.poll() is not None:  # an answer while the app is already gone is not the app's
                raise Refused("the app exited before it answered")
            return
        if time.monotonic() >= deadline:
            raise Refused("the app did not answer its URL in time")
        time.sleep(0.2)


def _check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK or parts.port is None:
        raise Refused("the URL must be an http URL on loopback with a port")
    return url


def _drain(proc: subprocess.Popen, deadline: float) -> bytes:
    """The browser's stdout, read as it is written: at most MAX_FILE + 1 bytes, and a refusal past MAX_FILE.

    The pipe is non-blocking (``_browser_png`` sets it), so a read never waits. A read that finds nothing (BlockingIOError)
    goes round the loop again: select said the pipe was readable, and another process may have taken the bytes since. The
    deadline is checked on every pass. The browser's exit is sampled BEFORE the read, so every byte it wrote is in the pipe
    by the time an empty read counts as its end. The read ends at EOF, or when the browser has exited and nothing is
    waiting in the pipe. A browser that is still running past ``deadline`` is a refusal (the caller stops it)."""
    fd = proc.stdout.fileno()
    data = bytearray()
    while True:
        if len(data) > MAX_FILE:
            raise Refused("the screenshot is larger than the scorer reads")
        if time.monotonic() >= deadline:
            raise Refused("the browser did not finish in time")
        exited = proc.poll() is not None
        select.select([fd], [], [], 0.1)  # only a wait: the read below decides what is there
        try:
            chunk = os.read(fd, min(65536, MAX_FILE + 1 - len(data)))
        except BlockingIOError:
            chunk = None  # no data yet, or it was taken by another reader
        if chunk == b"":
            break  # EOF
        if chunk:
            data += chunk
        elif exited:
            break  # the browser has exited and its pipe is empty
    return bytes(data)


def _browser_png(browser: str, url: str, width: int, height: int, seconds: float = BROWSER_SECONDS) -> bytes:
    """Run the browser CLI and return its stdout (the PNG), read through a pipe while the browser runs.

    The pipe holds no path, and the bytes are only read; nothing is written to a file the app could reach. Any exit other
    than 0 is a refusal, whatever the browser wrote. However this returns or raises, the browser's whole tree is stopped
    (``_stop_tree``) and reaped before the caller goes on."""
    deadline = time.monotonic() + seconds
    proc = None
    try:
        with _signals_held() as mask:
            proc = _popen([browser, url, str(width), str(height)], mask, stdout=subprocess.PIPE)
        os.set_blocking(proc.stdout.fileno(), False)  # a read never waits on the browser (see _drain)
        data = _drain(proc, deadline)
        code = proc.wait(timeout=max(deadline - time.monotonic(), 0.0))
    except subprocess.TimeoutExpired:
        raise Refused("the browser did not finish in time") from None
    except OSError:
        raise Refused("the browser could not be run") from None
    finally:
        if proc is not None:
            _stop_tree(proc)
            proc.stdout.close()
    if code != 0:
        raise Refused("the browser failed")
    return data


def score(args: argparse.Namespace, *, reaper: bool = False) -> float:
    """The SSIM of the run. ``reaper`` is the scorer as its own process (main): it takes its orphans, and the app's teardown
    sweeps them. An in-process caller (a test) is not a reaper: a sweep of its own process would kill its other children."""
    if reaper:
        _become_subreaper()
    repo = _absolute_dir(args.repo, "the repo")
    assets = _absolute_dir(args.assets, "the assets checkout")
    if not 1 <= args.wait <= MAX_WAIT:
        raise Refused(f"--wait must be from 1 to {MAX_WAIT} seconds")
    mockup_file = _mockup_path(assets, args.mockup)
    width, height, mockup = decode_png(_read_regular(mockup_file, "the mockup"))
    if width < WINDOW or height < WINDOW:
        raise Refused("the mockup is smaller than the scorer's window")
    if not os.path.isabs(args.browser) or not os.path.isfile(args.browser) or not os.access(args.browser, os.X_OK):
        raise Refused("--browser must be an executable file at an absolute path")
    try:
        start = shlex.split(args.start)
    except ValueError:
        raise Refused("--start is not a command") from None
    if not start or not os.path.isabs(start[0]):
        raise Refused("--start must begin with the absolute path of its program")
    url = _check_url(args.url)
    if _answers(url):  # a stranger on the port would be the one scored: refused before the app is started
        raise Refused("something already answers the URL: it would not be the app that is scored")
    app, png = None, None
    try:
        with _signals_held() as mask:  # a stop signal during the spawn is delivered once the app is bound, and stops it
            app = _popen(start, mask, cwd=repo)
        _wait_for_app(app, url, args.wait)
        png = _browser_png(args.browser, url, width, height)
        if app.poll() is not None:
            raise Refused("the app stopped while the screenshot was taken")
    finally:
        if app is not None:
            _stop_tree(app, sweep=reaper)  # the app's whole tree is stopped and reaped before the screenshot is decoded
    shot_w, shot_h, shot_gray = decode_png(png)
    if (shot_w, shot_h) != (width, height):
        raise Refused("the screenshot is not the size of the mockup")
    return ssim(width, height, shot_gray, mockup)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="image.py", description="Print the SSIM of a headless screenshot against a mockup.",
                                allow_abbrev=False)
    for flag in ("assets", "mockup", "browser", "start", "url", "repo"):
        p.add_argument(f"--{flag}", required=True)
    p.add_argument("--wait", type=int, default=DEFAULT_WAIT)
    return p


def _terminated(signum, frame) -> None:
    """SIGTERM, SIGINT and SIGHUP end the run as a SystemExit, so score()'s finally stops the app before the exit."""
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)  # a usage error exits 2 with its message on stderr only
    for sig in STOP_SIGNALS:
        signal.signal(sig, _terminated)
    try:
        value = score(args, reaper=True)
    except Refused as err:
        print(f"score refused: {err}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as err:
        print(f"score refused: {err}", file=sys.stderr)
        return 1
    sys.stdout.write(format_score(value) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
