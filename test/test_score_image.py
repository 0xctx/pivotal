"""The mirror scorer (engine/score/image.py) and the gate that reads it.

The scorer runs as a real subprocess. Its browser is a fake CLI (test/fakecli.py) that writes a known PNG, its app is a
sleeping process, and its URL is a loopback HTTP server this module starts. The PNGs are written here by a small encoder,
so every image the tests compare is known. Nothing reaches a provider, a real browser or the network.
"""

import argparse
import http.server
import os
import re
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

import fakecli
import nolive  # noqa: F401  (no test sweeps the live /proc)
from engine import buildjob, hill, lines
from engine.score import image

ROOT = Path(__file__).resolve().parent.parent
SCORER = ROOT / "engine" / "score" / "image.py"
SERVER: http.server.ThreadingHTTPServer | None = None


# ---- a PNG encoder for the tests (stdlib only): every filter type, and the colour types the scorer reads ----


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def _filtered(kind: int, row: bytes, prev: bytes, bpp: int) -> bytes:
    out = bytearray()
    for i, x in enumerate(row):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        pred = (0, a, b, (a + b) // 2, _paeth(a, b, c))[kind]
        out.append((x - pred) & 0xFF)
    return bytes(out)


CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def png(width: int, height: int, pixel, color_type: int = 0, palette: bytes = b"", filters=(0,)) -> bytes:
    """A non-interlaced 8-bit PNG. ``pixel(x, y)`` returns the channel values (a palette index for type 3)."""
    bpp = CHANNELS[color_type]
    raw, prev = bytearray(), bytes(width * bpp)
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row += bytes(pixel(x, y))
        kind = filters[y % len(filters)]
        raw.append(kind)
        raw += _filtered(kind, bytes(row), prev, bpp)
        prev = bytes(row)
    body = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
    if palette:
        body += _chunk(b"PLTE", palette)
    return body + _chunk(b"IDAT", zlib.compress(bytes(raw))) + _chunk(b"IEND", b"")


def gradient(x: int, y: int) -> tuple[int, int, int]:
    v = (x * 10 + y * 5) % 256
    return (v, (v * 3) % 256, (255 - v) % 256)


def gray_gradient(x: int, y: int) -> tuple[int]:
    return ((x * 10 + y * 5) % 256,)


MOCKUP_W, MOCKUP_H = 24, 16


def mockup_png() -> bytes:
    return png(MOCKUP_W, MOCKUP_H, gradient, 2)


def gone(pid: int) -> bool:
    """Whether a process is gone: no such pid, or a zombie (killed and no longer running, awaiting its parent's reap)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state == "Z"


def _kill_from_pidfile(pidfile: Path) -> None:
    """Kill the process whose pid a test wrote to ``pidfile`` (a cleanup: a process left behind by a failing test)."""
    try:
        os.kill(int(pidfile.read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def sleeper_app(root: Path, marker: Path) -> str:
    """A start command for an app that writes ``marker`` and sleeps. A script file, not ``-c``: a path in a -c quote breaks shlex."""
    script = root / f"sleeper-{marker.name}.py"
    script.write_text("import sys, time\nopen(sys.argv[1], 'w').close()\ntime.sleep(120)\n", encoding="utf-8")
    return f"{sys.executable} {script} {marker}"


TREE_CHILD = '''import os, subprocess, sys, time
# a child that starts a grandchild (in its group), writes the grandchild's pid to argv[1] atomically, then sleeps
grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1] + ".tmp", "w").write(str(grand.pid))
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(120)
'''


KILLER_APP = '''import http.server, os, sys, threading, time
# argv: pidfile, port, started (the file the browser creates), signal number. It serves the URL, and once the browser has
# started it signals its parent (the scorer) and sleeps: only the kernel can stop it when the scorer dies.
with open(sys.argv[1], "w") as fh:
    fh.write(str(os.getpid()))
server = http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), http.server.SimpleHTTPRequestHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
while not os.path.exists(sys.argv[3]):
    time.sleep(0.02)
os.kill(os.getppid(), int(sys.argv[4]))
time.sleep(120)
'''


DOUBLE_FORK_APP = '''import http.server, os, subprocess, sys, threading, time
# argv: the grandchild's pidfile, port, started. The double fork: a child in its own session starts a grandchild and exits, so
# the grandchild is an orphan. The app serves, and exits (os._exit: no cleanup of its own) once the browser has started.
CHILD = "import subprocess, sys; g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); open(sys.argv[1], 'w').write(str(g.pid))"
subprocess.run([sys.executable, "-c", CHILD, sys.argv[1]], start_new_session=True, check=True)
server = http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), http.server.SimpleHTTPRequestHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
while not os.path.exists(sys.argv[3]):
    time.sleep(0.02)
os._exit(0)
'''


FORGER = '''import http.server, os, sys, threading, time
# argv: marker, port, the mockup's path, mode (loop | once). The app serves the URL, then forges what it can open.
open(sys.argv[1], "w").write(str(os.getpid()))
mockup = open(sys.argv[3], "rb").read()
server = http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), http.server.SimpleHTTPRequestHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def pipe_link(pid):
    try:
        return os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        return None


def children_of_scorer():
    parent = os.getppid()  # this app is the scorer's child; so is the browser
    found = []
    for name in os.listdir("/proc"):
        if name.isdigit() and int(name) != os.getpid():
            try:
                fields = open(f"/proc/{name}/stat").read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(fields[1]) == parent:
                found.append(int(name))
    return found


def forge():
    browsers = children_of_scorer()
    stdout_links = {pipe_link(pid) for pid in browsers} - {None}
    wrote = False
    for pid in [os.getppid()] + browsers:
        try:
            names = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in names:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
                if link.endswith("(deleted)"):  # the old temporary file: rewrite it from 0, then its length
                    handle = os.open(f"/proc/{pid}/fd/{fd}", os.O_WRONLY)
                    os.pwrite(handle, mockup, 0)
                    os.ftruncate(handle, len(mockup))
                    os.close(handle)
                    wrote = True
                elif link in stdout_links:  # the browser's stdout pipe: one valid PNG appended to it
                    handle = os.open(f"/proc/{pid}/fd/{fd}", os.O_WRONLY | os.O_NONBLOCK)
                    os.write(handle, mockup)
                    os.close(handle)
                    wrote = True
            except OSError:
                continue
    return wrote


if sys.argv[4] == "loop":
    while True:
        forge()
        time.sleep(0.002)
else:
    while not forge():
        time.sleep(0.01)
    while True:
        time.sleep(1)
'''


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def setUpModule():
    global SERVER
    handler = type("Ok", (http.server.BaseHTTPRequestHandler,), {
        "do_GET": lambda self: (self.send_response(200), self.end_headers(), self.wfile.write(b"ok")),
        "log_message": lambda self, *args: None,
    })
    SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def tearDownModule():
    if SERVER is not None:
        SERVER.shutdown()
        SERVER.server_close()


class Scene:
    """One temporary world: an assets checkout with the mockup, a repo, a fake browser and a fake app, and a run helper."""

    def __init__(self, root: Path):
        self.root = root
        self.assets = root / "assets"
        self.repo = root / "repo"
        self.assets.mkdir()
        self.repo.mkdir()
        self.bins = fakecli.FakeBin(root / "bin")
        self.runs = 0
        self.write_mockup(mockup_png())
        self.port = free_port()  # the default app serves this port; the module's SERVER is a stranger, never the default app
        self.url = f"http://127.0.0.1:{self.port}/"
        self.serve = root / "serve.py"
        self.serve.write_text(
            "import http.server, sys\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), http.server.SimpleHTTPRequestHandler).serve_forever()\n",
            encoding="utf-8",
        )

    def write_mockup(self, data: bytes, name: str = "mockup.png") -> None:
        (self.assets / name).parent.mkdir(parents=True, exist_ok=True)
        (self.assets / name).write_bytes(data)

    def run(self, *, shot_source=None, browser_code=0, browser_stdout="", mockup="mockup.png", browser=None,
            start=None, url=None, wait="5", cwd=None, extra=(), browser_sleep=0):
        """Run the scorer. ``shot_source`` is what the fake browser writes (None: nothing); the defaults are a good run."""
        self.runs += 1
        run_dir = self.root / f"run-{self.runs}"
        run_dir.mkdir()
        if browser is None:
            fake = self.bins.add_browser(f"browser-{self.runs}", source=shot_source, stdout=browser_stdout, code=browser_code,
                                         sleep=browser_sleep)
            browser = str(fake)
        if start is None:
            start = f"{sys.executable} {self.serve} {self.port}"
        argv = [sys.executable, "-I", str(SCORER), "--assets", str(self.assets), "--mockup", mockup,
                "--browser", browser, "--start", start, "--url", url or self.url, "--repo", str(self.repo),
                "--wait", wait, *extra]
        return subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd or self.root,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, timeout=300)


class ScorerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)  # registered first, so it runs LAST: the kill cleanups read pid files in it
        self.scene = Scene(Path(self._tmp.name))

    def assertScore(self, proc, low=None, high=None):
        """A run that prints one number in [0, 1] with at most six decimals, and exits 0."""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, r"\A(?:0|1|0\.[0-9]{1,6}|\.[0-9]{1,6})\n\Z")
        value = float(proc.stdout)
        if low is not None:
            self.assertGreaterEqual(value, low)
        if high is not None:
            self.assertLess(value, high)
        return value

    def assertRefused(self, proc):
        """A failure: a non-zero exit and nothing at all on stdout (no number, no default, no partial output)."""
        self.assertNotEqual(proc.returncode, 0, "a failure must exit non-zero")
        self.assertEqual(proc.stdout, "", "a failure must print nothing on stdout")

    # ---- the score ----

    def test_identical_images_score_one(self):
        self.assertEqual(self.scene.run(shot_source=mockup_png()).stdout, "1\n")

    def test_different_images_score_below_point_nine(self):
        inverted = png(MOCKUP_W, MOCKUP_H, lambda x, y: tuple(255 - c for c in gradient(x, y)), 2)
        self.assertScore(self.scene.run(shot_source=inverted), high=0.9)

    def test_a_screenshot_close_to_the_mockup_reaches_point_nine(self):
        def near(x, y):
            return tuple(min(255, c + 2) for c in gradient(x, y)) if (x, y) in {(3, 3), (10, 7), (20, 12)} else gradient(x, y)
        self.assertScore(self.scene.run(shot_source=png(MOCKUP_W, MOCKUP_H, near, 2)), low=0.9)

    def test_the_score_is_a_number_and_not_the_browsers_output(self):
        proc = self.scene.run(shot_source=mockup_png(), browser_stdout="0.123\n")
        self.assertEqual(proc.stdout, "1\n")

    def test_a_score_file_in_the_working_directory_is_not_read(self):
        cwd = self.scene.root / "cwd"
        cwd.mkdir()
        (cwd / "score.txt").write_text("1\n", encoding="utf-8")
        inverted = png(MOCKUP_W, MOCKUP_H, lambda x, y: tuple(255 - c for c in gradient(x, y)), 2)
        self.assertScore(self.scene.run(shot_source=inverted, cwd=cwd), high=0.9)

    def test_a_grayscale_and_a_palette_screenshot_are_read(self):
        self.scene.write_mockup(png(MOCKUP_W, MOCKUP_H, gray_gradient, 0), "gray.png")
        self.assertScore(self.scene.run(shot_source=png(MOCKUP_W, MOCKUP_H, gray_gradient, 0), mockup="gray.png"), low=0.999)
        palette = bytes(v for i in range(4) for v in (i * 60, i * 30, 255 - i * 60))
        self.scene.write_mockup(png(MOCKUP_W, MOCKUP_H, lambda x, y: ((x + y) % 4,), 3, palette), "pal.png")
        self.assertScore(self.scene.run(shot_source=png(MOCKUP_W, MOCKUP_H, lambda x, y: ((x + y) % 4,), 3, palette),
                                        mockup="pal.png"), low=0.999)

    # ---- every failure: non-zero, nothing on stdout ----

    def test_a_corrupt_screenshot_is_refused(self):
        self.assertRefused(self.scene.run(shot_source=b"not a png at all"))

    def test_a_truncated_screenshot_is_refused(self):
        self.assertRefused(self.scene.run(shot_source=mockup_png()[:60]))

    def test_a_screenshot_the_browser_did_not_write_is_refused(self):
        self.assertRefused(self.scene.run(shot_source=None))

    def test_a_browser_failure_is_refused_even_when_it_wrote_a_good_png(self):
        # The premise: the fake browser really writes the good PNG to its stdout before it fails with code 3.
        fake = self.scene.bins.add_browser("premise", source=mockup_png(), code=3)
        premise = subprocess.run([str(fake), self.scene.url, "1", "1"], capture_output=True, check=False)
        self.assertEqual((premise.returncode, premise.stdout), (3, mockup_png()))
        self.assertRefused(self.scene.run(shot_source=mockup_png(), browser_code=3))

    def test_a_browser_that_prints_a_score_and_fails_is_refused(self):
        self.assertRefused(self.scene.run(shot_source=None, browser_code=1, browser_stdout="1\n"))

    def test_a_corrupt_mockup_is_refused(self):
        self.scene.write_mockup(b"\x89PNG\r\n\x1a\n-garbage-", "bad.png")
        self.assertRefused(self.scene.run(shot_source=mockup_png(), mockup="bad.png"))

    def test_a_jpeg_named_as_a_png_mockup_is_refused(self):
        self.scene.write_mockup(b"\xff\xd8\xff\xe0" + b"\x00" * 40, "photo.png")
        self.assertRefused(self.scene.run(shot_source=mockup_png(), mockup="photo.png"))

    def test_a_missing_mockup_is_refused(self):
        self.assertRefused(self.scene.run(shot_source=mockup_png(), mockup="absent.png"))

    def test_a_symlinked_mockup_is_refused(self):
        (self.scene.assets / "link.png").symlink_to(self.scene.assets / "mockup.png")
        self.assertRefused(self.scene.run(shot_source=mockup_png(), mockup="link.png"))

    def test_a_mockup_under_a_symlinked_directory_is_refused(self):
        outside = self.scene.root / "outside"
        outside.mkdir()
        (outside / "mockup.png").write_bytes(mockup_png())
        (self.scene.assets / "dir").symlink_to(outside, target_is_directory=True)
        self.assertRefused(self.scene.run(shot_source=mockup_png(), mockup="dir/mockup.png"))

    def test_a_mockup_path_that_escapes_the_assets_is_refused(self):
        (self.scene.root / "outside.png").write_bytes(mockup_png())
        self.assertRefused(self.scene.run(shot_source=mockup_png(), mockup="../outside.png"))
        self.assertRefused(self.scene.run(shot_source=mockup_png(), mockup=str(self.scene.root / "outside.png")))

    def test_a_screenshot_of_another_size_is_refused(self):
        self.assertRefused(self.scene.run(shot_source=png(12, 8, gray_gradient, 0)))

    def test_an_app_that_exits_at_start_is_refused(self):
        self.assertRefused(self.scene.run(shot_source=mockup_png(), start=f"{sys.executable} -c 'import sys; sys.exit(3)'",
                                          url=f"http://127.0.0.1:{free_port()}/"))

    def test_an_app_that_never_answers_is_refused(self):
        self.assertRefused(self.scene.run(shot_source=mockup_png(), url=f"http://127.0.0.1:{free_port()}/", wait="2"))

    def test_a_stranger_already_answering_the_url_is_refused_before_the_app_starts(self):
        # The SERVER of this module answers the scene's URL: the app would not be the one scored, so nothing runs.
        marker = self.scene.root / "started"
        # The app stays alive (it writes its marker, then sleeps), so only the probe before the start can refuse it.
        start = sleeper_app(self.scene.root, marker)
        stranger = f"http://127.0.0.1:{SERVER.server_address[1]}/"
        self.assertRefused(self.scene.run(shot_source=mockup_png(), start=start, url=stranger))
        self.assertFalse(marker.exists(), "the app must not start when the URL already answers")

    def test_a_server_that_answers_non_http_bytes_is_refused_by_name(self):
        # A socket server answers every connection with bytes that are not HTTP: http.client raises BadStatusLine, which is not
        # an OSError. The probe refuses it by name (an uncaught exception would be a crash, not a refusal), before any app starts.
        port = self.garbage_server()
        marker = self.scene.root / "started"
        proc = self.scene.run(shot_source=mockup_png(), start=sleeper_app(self.scene.root, marker),
                              url=f"http://127.0.0.1:{port}/")
        self.assertRefused(proc)
        self.assertIn("not HTTP", proc.stderr)
        self.assertFalse(marker.exists(), "the app must not start when the URL answers with non-HTTP bytes")
        with self.assertRaisesRegex(image.Refused, "not HTTP"):
            image._answers(f"http://127.0.0.1:{port}/")

    def garbage_server(self) -> int:
        """A loopback socket server that answers each connection with a line that is not an HTTP status line."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)

        def answer():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                with conn:
                    conn.sendall(b"this is not HTTP\r\n")

        threading.Thread(target=answer, daemon=True).start()
        return listener.getsockname()[1]

    def test_an_app_that_never_binds_its_port_is_refused_with_no_answer_from_anyone(self):
        # Nothing answers the URL, the app sleeps and never binds it: refused at the wait, with the app stopped.
        self.assertRefused(self.scene.run(shot_source=mockup_png(), start=f"{sys.executable} -c 'import time; time.sleep(60)'",
                                          url=f"http://127.0.0.1:{free_port()}/", wait="2"))

    def test_a_url_that_is_not_loopback_is_refused_before_anything_runs(self):
        marker = self.scene.root / "started"
        start = sleeper_app(self.scene.root, marker)
        self.assertRefused(self.scene.run(shot_source=mockup_png(), url="http://example.com/", start=start))
        self.assertFalse(marker.exists())

    def test_a_relative_browser_path_is_refused(self):
        self.assertRefused(self.scene.run(shot_source=mockup_png(), browser="browser"))

    def test_a_png_left_at_an_old_screenshot_path_is_not_read(self):
        # There is no screenshot path any more: a good PNG sitting where one used to be is never read; the browser wrote nothing.
        run_dir = self.scene.root / "stale"
        run_dir.mkdir()
        (run_dir / "shot.png").write_bytes(mockup_png())
        argv = [sys.executable, "-I", str(SCORER), "--assets", str(self.scene.assets), "--mockup", "mockup.png",
                "--browser", str(self.scene.bins.add_browser("b-stale", source=None)), "--start",
                f"{sys.executable} -c 'import time; time.sleep(60)'", "--url", self.scene.url, "--repo",
                str(self.scene.repo), "--wait", "5"]
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120)
        self.assertRefused(proc)

    def test_an_app_that_exits_during_the_browser_run_is_refused(self):
        # The app answers the scorer's probe, then exits while the (slow) browser runs: no score from a dead app.
        app = self.scene.root / "once.py"
        port = free_port()
        app.write_text(
            "import http.server, sys, time\n"
            "server = http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), http.server.SimpleHTTPRequestHandler)\n"
            "server.handle_request()\n"  # the probe is answered, then the app is gone
            "time.sleep(0.5)\n",
            encoding="utf-8",
        )
        proc = self.scene.run(shot_source=mockup_png(), start=f"{sys.executable} {app} {port}",
                              url=f"http://127.0.0.1:{port}/", browser_sleep=1)
        self.assertRefused(proc)

    def test_a_pipe_whose_bytes_are_taken_is_refused_by_the_deadline_not_read_forever(self):
        # select reports the browser's pipe readable, and then another process takes the bytes before the scorer reads them
        # (the test forces that: select always answers ready), and the browser never writes again. A blocking read waits for
        # the browser, which sleeps 120 s; the read must give up, and the deadline refuses (1 s here, 2 s of slack).
        pidfile, browser = self.scene.root / "silent-browser.pid", self.scene.root / "silent-browser"
        browser.write_text(f"#!{sys.executable}\nimport os, time\n"
                           f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(120)\n", encoding="utf-8")
        browser.chmod(0o755)
        self.addCleanup(_kill_from_pidfile, pidfile)
        outcome = {}

        def run():
            try:
                image._browser_png(str(browser), "http://127.0.0.1:9/", 1, 1, seconds=1)
            except BaseException as err:  # recorded for the assertions below; a thread must not swallow it silently
                outcome["error"] = err

        always_ready = types.SimpleNamespace(select=lambda readable, writable, errored, timeout=None: (readable, [], []))
        with mock.patch.object(image, "select", always_ready):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(1 + 2)
        self.assertFalse(thread.is_alive(), "the scorer blocked on an empty pipe instead of refusing at the deadline")
        self.assertIsInstance(outcome.get("error"), image.Refused)
        self.assertIn("did not finish in time", str(outcome["error"]))

    def test_a_child_of_the_app_in_its_group_is_killed_with_the_app(self):
        # The app starts a child in its own process group and serves; after the score the child is gone too.
        marker, port = self.scene.root / "child", free_port()
        app = self.scene.root / "parent.py"
        app.write_text(
            "import http.server, os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2])), http.server.SimpleHTTPRequestHandler).serve_forever()\n",
            encoding="utf-8",
        )
        proc = self.scene.run(shot_source=mockup_png(), start=f"{sys.executable} {app} {marker} {port}",
                              url=f"http://127.0.0.1:{port}/", browser=None, extra=())
        self.assertScore(proc, low=0.999)
        self.assertTrue(gone(int(marker.read_text())), "the app's child must not outlive the score")

    def assertGone(self, pid: int):
        """The process is gone within 10 s (a zombie counts as gone: killed, awaiting its reap)."""
        deadline = time.monotonic() + 10
        while not gone(pid):
            self.assertLess(time.monotonic(), deadline, f"process {pid} survived the stop")
            time.sleep(0.1)

    def test_a_setsid_child_and_its_child_of_the_app_are_killed_with_the_app(self):
        # The app starts a child in its OWN session (start_new_session), which starts a grandchild, and serves. The group kill
        # does not reach either: the tree is walked through the parent links while the app is alive, and both are killed.
        marker, port = self.scene.root / "tree-child", free_port()
        self.addCleanup(_kill_from_pidfile, Path(str(marker) + ".grand"))
        self.addCleanup(_kill_from_pidfile, marker)
        child_script = self.scene.root / "tree_child.py"
        child_script.write_text(TREE_CHILD, encoding="utf-8")
        app = self.scene.root / "tree_app.py"
        app.write_text(
            "import http.server, os, subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, {str(child_script)!r}, {str(marker) + '.grand'!r}], start_new_session=True)\n"
            f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
            f"while not os.path.exists({str(marker) + '.grand'!r}): time.sleep(0.05)\n"
            f"http.server.ThreadingHTTPServer(('127.0.0.1', {port}), http.server.SimpleHTTPRequestHandler).serve_forever()\n",
            encoding="utf-8",
        )
        proc = self.scene.run(shot_source=mockup_png(), start=f"{sys.executable} {app}", url=f"http://127.0.0.1:{port}/")
        self.assertScore(proc, low=0.999)
        self.assertGone(int(marker.read_text()))
        self.assertGone(int(Path(str(marker) + ".grand").read_text()))

    def test_a_browser_that_spawns_a_child_is_killed_with_its_tree_on_timeout(self):
        # The browser starts a child in its own session, which starts a grandchild, and holds its stdout: it is still running at
        # the deadline. The refusal comes, and the whole tree is gone (killed by the scorer, not left to init).
        marker = self.scene.root / "browser-child"
        self.addCleanup(_kill_from_pidfile, Path(str(marker) + ".grand"))
        self.addCleanup(_kill_from_pidfile, marker)
        child_script = self.scene.root / "tree_child.py"
        child_script.write_text(TREE_CHILD, encoding="utf-8")
        browser = self.scene.root / "tree-browser"
        browser.write_text(
            f"#!{sys.executable}\n"
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, {str(child_script)!r}, {str(marker) + '.grand'!r}], start_new_session=True)\n"
            f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        browser.chmod(0o755)
        with self.assertRaisesRegex(image.Refused, "did not finish in time"):
            image._browser_png(str(browser), "http://127.0.0.1:9/", 1, 1, seconds=2)
        self.assertGone(int(marker.read_text()))
        self.assertGone(int(Path(str(marker) + ".grand").read_text()))

    def test_a_scorer_killed_by_its_app_takes_the_app_and_the_browser_with_it(self):
        # SIGKILL, and SIGUSR1 (no handler, so no teardown either): the scorer dies without running _stop_tree. The app and
        # the browser are stopped by the kernel (PR_SET_PDEATHSIG), not by the scorer. The app signals once the browser runs.
        for sig in (signal.SIGKILL, signal.SIGUSR1):
            with self.subTest(signal=sig.name):
                self._scorer_killed_mid_run(sig)

    def _scorer_killed_mid_run(self, sig: signal.Signals) -> None:
        root = self.scene.root / f"killed-{sig.name}"
        root.mkdir()
        app_pid, browser_pid, started, port = root / "app.pid", root / "browser.pid", root / "browser.started", free_port()
        app = root / "killer.py"
        app.write_text(KILLER_APP, encoding="utf-8")
        browser = root / "browser"
        browser.write_text(f"#!{sys.executable}\nimport os, time\n"
                           f"open({str(browser_pid)!r}, 'w').write(str(os.getpid()))\n"
                           f"open({str(started)!r}, 'w').close()\ntime.sleep(120)\n", encoding="utf-8")
        browser.chmod(0o755)
        self.addCleanup(_kill_from_pidfile, app_pid)
        self.addCleanup(_kill_from_pidfile, browser_pid)
        proc = self.scene.run(start=f"{sys.executable} {app} {app_pid} {port} {started} {int(sig)}",
                              url=f"http://127.0.0.1:{port}/", browser=str(browser))
        self.assertEqual(proc.returncode, -int(sig), proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertGone(int(app_pid.read_text()))
        self.assertGone(int(browser_pid.read_text()))

    def test_an_app_that_double_forks_a_setsid_child_that_outlives_it_is_killed_before_the_scorer_exits(self):
        # The app starts a setsid child, which starts a grandchild and exits: the grandchild outlives its parents. Then the app
        # exits while the browser runs. The scorer's sweep (its children, orphans included, after the app is gone) must kill
        # the grandchild before the scorer exits: checked the moment the scorer has exited, with no wait.
        root = self.scene.root / "double-fork"
        root.mkdir()
        grand_pid, started, port = root / "grand.pid", root / "browser.started", free_port()
        app = root / "double_fork.py"
        app.write_text(DOUBLE_FORK_APP, encoding="utf-8")
        browser = self.scene.bins.add_browser("double-fork-browser", source=mockup_png(), log=started, sleep=1)
        self.addCleanup(_kill_from_pidfile, grand_pid)
        proc = self.scene.run(start=f"{sys.executable} {app} {grand_pid} {port} {started}",
                              url=f"http://127.0.0.1:{port}/", browser=str(browser))
        self.assertRefused(proc)  # the app is gone while the browser runs: no score from it
        self.assertTrue(gone(int(grand_pid.read_text())), "the scorer exited with the app's grandchild still running")

    def test_a_stop_signal_during_the_browser_run_stops_the_app_and_prints_nothing(self):
        # SIGTERM (a cancelled job), SIGHUP (a dropped terminal) or SIGINT while the slow browser runs: the scorer's teardown
        # still stops the app's tree, and nothing is printed.
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=sig.name):
                self._stop_during_browser(sig)

    def _stop_during_browser(self, sig: signal.Signals) -> None:
        marker, port, log = self.scene.root / f"pid-{sig.name}", free_port(), self.scene.root / f"browser-{sig.name}.log"
        app = self.scene.root / f"app-{sig.name}.py"
        app.write_text(
            "import http.server, os, sys\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2])), http.server.SimpleHTTPRequestHandler).serve_forever()\n",
            encoding="utf-8",
        )
        browser = self.scene.bins.add_browser(f"slow-{sig.name}", source=mockup_png(), log=log, sleep=60)
        argv = [sys.executable, "-I", str(SCORER), "--assets", str(self.scene.assets), "--mockup", "mockup.png",
                "--browser", str(browser), "--start", f"{sys.executable} {app} {marker} {port}",
                "--url", f"http://127.0.0.1:{port}/", "--repo", str(self.scene.repo), "--wait", "5"]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            while not log.exists():  # the browser has started: the app is up and the score is in progress
                self.assertLess(time.monotonic(), deadline, "the browser never started")
                time.sleep(0.05)
            proc.send_signal(sig)
            out, _err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(out, "", "a stopped run prints nothing on stdout")
        pid = int(marker.read_text())
        self.addCleanup(lambda: gone(pid) or os.kill(pid, signal.SIGKILL))  # a failing run must not leave the app behind
        self.assertGone(pid)

    def forger(self, mode: str):
        """Run a forging app beside a blank browser, and return the scorer's process.

        The browser writes a blank page to its stdout at once and then holds for 0.5 s before it exits, so the forger has the
        whole run in which to rewrite. The app finds the scorer's descriptors through os.getppid() and /proc/<ppid>/fd, and the
        browser's (its children, found in /proc by parent link) through /proc/<pid>/fd. Where the screenshot goes, it either
        rewrites a deleted file (the old temporary file: pwrite the mockup at 0, then truncate) or writes the mockup into the
        browser's stdout pipe (``loop``: every 2 ms; ``once``: one valid PNG as soon as the pipe is seen)."""
        app = self.scene.root / f"forger-{mode}.py"
        port, marker = free_port(), self.scene.root / f"forger-{mode}.pid"
        app.write_text(FORGER, encoding="utf-8")
        browser = self.scene.root / f"blank-browser-{mode}"
        blank = png(MOCKUP_W, MOCKUP_H, lambda x, y: (255,), 0)
        browser.write_text(f"#!{sys.executable}\n"
                           "import sys, time\n"
                           f"sys.stdout.buffer.write({blank!r})\n"
                           "sys.stdout.flush()\n"
                           "time.sleep(0.5)\n", encoding="utf-8")
        browser.chmod(0o755)
        start = f"{sys.executable} {app} {marker} {port} {self.scene.assets / 'mockup.png'} {mode}"
        proc = self.scene.run(start=start, url=f"http://127.0.0.1:{port}/", browser=str(browser))
        self.addCleanup(lambda: marker.exists() and self._kill_pid(marker))
        return proc

    def _kill_pid(self, marker: Path) -> None:
        try:
            os.kill(int(marker.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass

    def assertTrueOrRefused(self, proc):
        """A forgery can only make the run refuse: the browser's true (low) score, or a refusal, never a high one."""
        if proc.returncode == 0:
            self.assertScore(proc, high=0.9)
        else:
            self.assertRefused(proc)

    def test_an_app_that_forges_the_screenshot_through_proc_cannot_change_the_score(self):
        # The forgery of the review: the app rewrites every descriptor it can open that holds the screenshot, every 2 ms, while
        # the browser (blank) runs. The old temporary file showed up as a deleted link and scored 1; a pipe cannot be rewritten.
        self.assertTrueOrRefused(self.forger("loop"))

    def test_an_app_that_writes_a_valid_forged_png_into_the_browsers_stdout_is_refused(self):
        # One valid PNG (the mockup) is written into the browser's stdout pipe. The browser's own blank PNG follows it, so
        # the stream holds bytes after the first PNG's end chunk: refused, never the forged number.
        self.assertRefused(self.forger("once"))

    def test_bytes_before_or_after_the_real_png_are_refused(self):
        # Append-only: an app can only add bytes around the browser's PNG. Either addition is refused by the framing.
        good = mockup_png()
        self.assertRefused(self.scene.run(shot_source=b"junk" + good))
        self.assertRefused(self.scene.run(shot_source=good + b"junk"))
        self.assertRefused(self.scene.run(shot_source=good + good))

    def test_a_stop_signal_that_lands_right_after_the_spawn_still_stops_the_app(self):
        # A SIGTERM, SIGINT or SIGHUP that arrives after Popen returns but before the app is bound must not leak it. The spawn
        # is inside a signal block: the signal is held, and delivered once the app is bound, so score()'s teardown stops it.
        # The three are named here, not read from STOP_SIGNALS: dropping one from that tuple must fail this test.
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=sig.name):
                self._stop_lands_after_spawn(sig)

    def test_a_scorer_that_cannot_take_its_orphans_is_refused_before_it_starts_the_app(self):
        # The sweep at the app's teardown needs the subreaper flag: without it the scorer refuses, and no app is started.
        args = argparse.Namespace(assets=str(self.scene.assets), mockup="mockup.png", browser="/bin/true",
                                  start=f"{sys.executable} -c pass", url="http://127.0.0.1:9/", repo=str(self.scene.repo), wait=5)
        no_subreaper = types.SimpleNamespace(prctl=lambda *args: -1)
        with mock.patch.object(image, "_libc", return_value=no_subreaper), mock.patch.object(image, "_popen") as popen, \
                self.assertRaisesRegex(image.Refused, "cannot take the orphans"):
            image.score(args, reaper=True)
        popen.assert_not_called()

    def test_the_stop_signals_are_sigterm_sigint_and_sighup(self):
        # The signals the scorer stops on (each raises SystemExit, so the teardown runs). Changing this set changes what a
        # cancelled run leaves behind: a deliberate edit must come with the test that pins it.
        self.assertEqual(set(image.STOP_SIGNALS), {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})

    def _stop_lands_after_spawn(self, sig: signal.Signals) -> None:
        marker, port = self.scene.root / f"race-pid-{sig.name}", free_port()
        app = self.scene.root / f"race_app-{sig.name}.py"
        app.write_text(
            "import http.server, os, sys\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2])), http.server.SimpleHTTPRequestHandler).serve_forever()\n",
            encoding="utf-8",
        )
        browser = self.scene.bins.add_browser(f"race-browser-{sig.name}", source=mockup_png())
        args = argparse.Namespace(assets=str(self.scene.assets), mockup="mockup.png", browser=str(browser),
                                  start=f"{sys.executable} {app} {marker} {port}", url=f"http://127.0.0.1:{port}/",
                                  repo=str(self.scene.repo), wait=5)
        real_popen, spawned = image._popen, []

        def racing_popen(argv, mask, **kwargs):
            proc = real_popen(argv, mask, **kwargs)
            spawned.append(proc)
            signal.pthread_kill(threading.get_ident(), sig)  # right after the spawn, before the app is bound
            return proc

        previous = signal.signal(sig, image._terminated)
        try:
            with mock.patch.object(image, "_popen", racing_popen), self.assertRaises(SystemExit):
                image.score(args)
        finally:
            signal.signal(sig, previous)
        self.assertEqual(len(spawned), 1, "the app was spawned once")
        self.assertGone(spawned[0].pid)

    def test_no_arguments_is_refused(self):
        proc = subprocess.run([sys.executable, "-I", str(SCORER)], capture_output=True, text=True, check=False, timeout=60)
        self.assertRefused(proc)

    def test_the_app_is_stopped_after_the_score(self):
        # The app writes its pid before it serves, and serves the URL the scorer waits for: the pid exists before the browser runs.
        marker, port = self.scene.root / "pid", free_port()
        app = self.scene.root / "app.py"
        app.write_text(
            "import http.server, os, sys\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2])), http.server.SimpleHTTPRequestHandler).serve_forever()\n",
            encoding="utf-8",
        )
        start = f"{sys.executable} {app} {marker} {port}"
        self.assertScore(self.scene.run(shot_source=mockup_png(), start=start, url=f"http://127.0.0.1:{port}/"), low=0.999)
        pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class StopTreeTest(unittest.TestCase):
    """_stop_tree: the group is signalled whatever the process-table scan does (a failed or interrupted scan must not skip it)."""

    def sleeper(self) -> subprocess.Popen:
        proc = image._popen([sys.executable, "-c", "import time; time.sleep(120)"], set())
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def flaky_scan(self, first_error: BaseException):
        """A process-table scan that raises ``first_error`` on its first call, and is the real scan after that."""
        real, calls = image._proc_table, []

        def scan():
            calls.append(1)
            if len(calls) == 1:
                raise first_error
            return real()

        return mock.patch.object(image, "_proc_table", scan)

    def test_a_stop_inside_the_scan_still_leaves_the_app_dead_and_is_raised_after_the_teardown(self):
        app = self.sleeper()
        with self.flaky_scan(SystemExit(1)), self.assertRaises(SystemExit):
            image._stop_tree(app)
        self.assertTrue(gone(app.pid), "a stop that lands in the scan must not leave the app running")

    def test_a_failed_scan_still_leaves_the_app_dead(self):
        app = self.sleeper()
        with self.flaky_scan(OSError("no /proc")):
            image._stop_tree(app)
        self.assertTrue(gone(app.pid), "a failed scan must not leave the app running")

    def test_a_scan_that_never_works_is_a_refusal_not_a_stop(self):
        # The group is killed, but the descendants cannot be checked on any pass: the teardown is not claimed as done.
        app = self.sleeper()
        with mock.patch.object(image, "_proc_table", side_effect=OSError("no /proc")), \
                mock.patch.object(image, "STOP_PASSES", 3), self.assertRaisesRegex(image.Refused, "did not stop"):
            image._stop_tree(app)
        self.assertTrue(gone(app.pid))


class ImageFunctionTest(unittest.TestCase):
    """The pure functions: the decoder and the SSIM."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def work_path(self, name: str) -> Path:
        return Path(self._tmp.name) / name

    def test_identical_images_are_exactly_one(self):
        grays = bytes((x * 37 + y * 11) % 256 for y in range(20) for x in range(30))
        self.assertEqual(image.ssim(30, 20, grays, grays), 1.0)

    def test_the_decoder_reads_every_filter_and_colour_type(self):
        for color_type in (0, 2, 3, 4, 6):
            palette = bytes(v for i in range(4) for v in (i * 60, i * 30, 255 - i * 60))
            if color_type == 3:
                pixel, palette_arg = (lambda x, y: ((x + y) % 4,)), palette
            elif color_type in (0, 4):
                pixel, palette_arg = (lambda x, y: (gray_gradient(x, y)[0],) + ((200,) if color_type == 4 else ())), b""
            elif color_type == 6:
                pixel, palette_arg = (lambda x, y: gradient(x, y) + (128,)), b""
            else:
                pixel, palette_arg = gradient, b""
            data = png(7, 5, pixel, color_type, palette_arg, filters=(0, 1, 2, 3, 4))
            width, height, gray = image.decode_png(data)
            self.assertEqual((width, height, len(gray)), (7, 5, 35), color_type)

    def test_each_filter_type_decodes_to_the_pixels_that_were_encoded(self):
        # Filters 0 to 4 on a gray gradient: the decoded pixels are exactly the encoded ones (a Paeth upper-left of 0 fails here).
        def irregular(x, y):  # not linear: the Paeth predictor's upper-left term changes which neighbour it picks
            return ((x * 73 + y * 151 + x * y * 19) ^ (x * y * 7)) % 256,
        for pattern in (gray_gradient, irregular):
            expected = bytes(pattern(x, y)[0] for y in range(6) for x in range(9))
            for kind in range(5):
                with self.subTest(pattern=pattern.__name__, filter=kind):
                    _, _, gray = image.decode_png(png(9, 6, pattern, 0, filters=(kind,)))
                    self.assertEqual(gray, expected)
            _, _, mixed = image.decode_png(png(9, 6, pattern, 0, filters=(0, 1, 2, 3, 4)))
            self.assertEqual(mixed, expected)

    def test_colour_types_decode_to_their_known_luma(self):
        # Pixel values and their luma (299 r + 587 g + 114 b + 500) // 1000, worked out by hand: 76, 150, 29, 18.
        rgb = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 20, 30)]
        for kind in range(5):
            with self.subTest(filter=kind):
                self.assertEqual(image.decode_png(png(4, 1, lambda x, y: rgb[x], 2, filters=(kind,)))[2], bytes([76, 150, 29, 18]))
        self.assertEqual(image.decode_png(png(1, 1, lambda x, y: (77,), 0))[2], bytes([77]))
        # Grey with alpha is composited over white: (v * a + 255 * (255 - a) + 127) // 255, so 200 at alpha 128 is 227.
        grey_alpha = [(200, 128), (200, 255), (200, 0), (30, 64)]
        self.assertEqual(image.decode_png(png(4, 1, lambda x, y: grey_alpha[x], 4, filters=(1,)))[2], bytes([227, 200, 255, 199]))
        # RGBA: (255, 0, 0) at alpha 128 composites to (255, 127, 127), luma 165; (100, 150, 200) at alpha 64 is luma 226.
        self.assertEqual(image.decode_png(png(1, 1, lambda x, y: (255, 0, 0, 128), 6, filters=(4,)))[2], bytes([165]))
        self.assertEqual(image.decode_png(png(1, 1, lambda x, y: (100, 150, 200, 64), 6, filters=(3,)))[2], bytes([226]))

    def test_a_palette_index_decodes_to_the_luma_of_its_palette_colour_not_one_channel(self):
        palette = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])  # red, green, blue, white
        self.assertEqual(image.decode_png(png(4, 1, lambda x, y: (x,), 3, palette, filters=(2,)))[2], bytes([76, 150, 29, 255]))

    def test_a_colour_pixel_decodes_to_its_luma(self):
        width, height, gray = image.decode_png(png(4, 1, lambda x, y: (255, 0, 0), 2))
        self.assertEqual(gray, bytes([(299 * 255 + 500) // 1000]) * 4)

    def test_a_blank_page_against_a_sparse_mockup_is_below_the_target(self):
        # The mockup is white with one 10 x 10 black square. A blank page is white everywhere: the uniform white windows
        # must not dominate the mean (they scored 0.99 when every window counted), so the blank page is below the target.
        width, height = 200, 120
        mockup = bytes(0 if (100 <= x < 110 and 50 <= y < 60) else 255 for y in range(height) for x in range(width))
        blank = bytes([255]) * (width * height)
        self.assertLess(image.ssim(width, height, blank, mockup), 0.95)

    def test_a_page_without_a_nav_bar_against_a_mockup_with_one_is_below_the_target(self):
        width, height = 200, 120
        bar = bytes((x * 37 + y * 11) % 2 * 200 + 20 if y < 8 else 255 for y in range(height) for x in range(width))
        without = bytes([255]) * (width * height)
        self.assertLess(image.ssim(width, height, without, bar), 0.95)

    def test_a_mockup_with_no_structure_at_all_is_refused(self):
        # Every window is flat: there is nothing to compare, so no number (a refusal, never 1 or 0).
        flat = bytes([128]) * (20 * 20)
        with self.assertRaisesRegex(ValueError, "no structure"):
            image.ssim(20, 20, flat, flat)

    def test_a_different_image_is_below_point_nine(self):
        a = bytes(range(0, 256, 2)) * 1
        b = bytes(255 - v for v in a)
        self.assertLess(image.ssim(16, 8, a, b), 0.9)

    def test_format_score_clamps_to_zero_and_one_and_prints_at_most_six_decimals(self):
        self.assertEqual(image.format_score(1.2), "1")
        self.assertEqual(image.format_score(1.0), "1")
        self.assertEqual(image.format_score(0.9999999), "1")
        self.assertEqual(image.format_score(-0.3), "0")
        self.assertEqual(image.format_score(0.0), "0")
        self.assertEqual(image.format_score(0.5), "0.5")
        self.assertEqual(image.format_score(0.1234567), "0.123457")

    def test_a_sixteen_bit_png_is_refused_for_its_depth(self):
        # A valid PNG (valid CRCs) of 16-bit gray: the refusal must name the depth, not a checksum.
        raw = b"".join(b"\x00" + b"\x00\x01" * 4 for _ in range(4))
        data = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 16, 0, 0, 0, 0))
                + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))
        with self.assertRaisesRegex(ValueError, "8-bit"):
            image.decode_png(data)

    def test_a_png_with_bytes_after_its_end_chunk_is_refused(self):
        # A second image appended after IEND (a stream spliced onto the browser's output) is not one PNG: refused.
        with self.assertRaisesRegex(ValueError, "after its end"):
            image.decode_png(mockup_png() + mockup_png())

    def test_a_png_that_inflates_far_past_its_header_is_refused_before_it_is_inflated(self):
        # A bomb: 1 GiB of zeros in about 1 MB of zlib data, under which the header says a 16 x 4 gray image (68 bytes of
        # rows). The decoder must stop at the header's size, not allocate 1 GiB: it runs under a 512 MiB address-space cap,
        # and the refusal must be the size rule, not a MemoryError.
        bomb = self.work_path("bomb.png")
        compressor, parts = zlib.compressobj(1), []
        zeros = bytes(1 << 20)
        for _ in range(1024):
            parts.append(compressor.compress(zeros))
        parts.append(compressor.flush())
        data = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 4, 8, 0, 0, 0, 0))
                + _chunk(b"IDAT", b"".join(parts)) + _chunk(b"IEND", b""))
        bomb.write_bytes(data)
        script = (
            "import resource, sys\n"
            "resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from engine.score import image\n"
            "try:\n"
            "    image.decode_png(open(sys.argv[2], 'rb').read())\n"
            "except ValueError as err:\n"
            "    print('refused:', err)\n"
            "except MemoryError:\n"
            "    print('memory')\n"
        )
        proc = subprocess.run([sys.executable, "-I", "-c", script, str(ROOT), str(bomb)], capture_output=True, text=True,
                              check=False, timeout=120)
        self.assertEqual(proc.stdout.strip(), "refused: PNG image data is not the size its header says", proc.stderr)

    def header_only_png(self, width: int, height: int, color_type: int = 0, interlace: int = 0, depth: int = 8,
                        raw: bytes | None = None, palette: bytes | None = None) -> bytes:
        """A PNG from its header and raw bytes (no encoder): for sizes, colour types and interlace the encoder never makes."""
        data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, interlace))
        if palette is not None:
            data += _chunk(b"PLTE", palette)
        if raw is not None:
            data += _chunk(b"IDAT", zlib.compress(raw))
        return data + _chunk(b"IEND", b"")

    def test_a_png_one_pixel_over_the_pixel_cap_is_refused_for_its_size(self):
        # The cap is 2,000,000 pixels (pinned here by its value, so a raised cap fails): a frame one pixel over it is refused
        # for its size, before any row is decoded.
        over = 2_000_001
        with self.assertRaisesRegex(ValueError, "outside what the scorer reads"):
            image.decode_png(self.header_only_png(over, 1, raw=b"\x00" * (over + 1)))

    def test_a_png_exactly_at_the_pixel_cap_is_read(self):
        # 1000 rows of 2000 grey pixels: exactly MAX_PIXELS, every row filter 0, read in full.
        width, height = 2000, 1000
        self.assertEqual(image.MAX_PIXELS, width * height, "the cap is 2,000,000 pixels")
        raw = (b"\x00" + bytes(width)) * height
        decoded = image.decode_png(self.header_only_png(width, height, raw=raw))
        self.assertEqual((decoded[0], decoded[1], len(decoded[2])), (width, height, 2_000_000))

    def test_a_zero_size_png_is_refused(self):
        # A zero width (and a zero height) is refused for its size; a valid stream of one filter byte is not a picture.
        for width, height in ((0, 1), (1, 0)):
            with self.subTest(width=width, height=height), self.assertRaisesRegex(ValueError, "outside what the scorer reads"):
                image.decode_png(self.header_only_png(width, height, raw=b"\x00"))

    def test_an_interlaced_png_is_refused(self):
        # Adam7 interlace is not read: a valid image with the interlace flag set is refused by name, not decoded as flat rows.
        with self.assertRaisesRegex(ValueError, "interlaced PNG is not read"):
            image.decode_png(self.header_only_png(2, 2, interlace=1, raw=b"\x00" * 6))

    def test_a_palette_png_without_a_palette_is_refused(self):
        # Colour type 3 needs a PLTE chunk: without one there is no luma to read a palette index as.
        with self.assertRaisesRegex(ValueError, "no usable palette"):
            image.decode_png(self.header_only_png(2, 1, color_type=3, raw=b"\x00\x00\x01"))

    def test_a_png_whose_image_data_runs_on_past_its_end_is_refused(self):
        # A valid stream of one more row than the header says: refused for its size, after at most one row past the end.
        raw = b"\x00" + bytes(4) + b"\x00" + bytes(4)
        data = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 1, 8, 0, 0, 0, 0))
                + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))
        with self.assertRaisesRegex(ValueError, "size its header says"):
            image.decode_png(data)

    def test_a_png_with_a_bad_crc_is_refused(self):
        data = bytearray(png(4, 4, lambda x, y: (1,), 0))
        data[data.index(b"IDAT") + 6] ^= 0xFF  # one byte of the IDAT data: its checksum no longer matches
        with self.assertRaisesRegex(ValueError, "checksum"):
            image.decode_png(bytes(data))


class AnchoredScorerTest(unittest.TestCase):
    """The gate reads the anchored scorer, never a file the build agent committed (F5)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        self.repo = self.work / "repo"
        (self.repo / "engine" / "score").mkdir(parents=True)
        # What a build agent could commit: its own scorer that prints 1, and a score file that says 1.
        (self.repo / "engine" / "score" / "image.py").write_text("print('1')\n", encoding="utf-8")
        (self.repo / "score.txt").write_text("1\n", encoding="utf-8")
        self.engine = self.work / "engine-checkout"
        (self.engine / "engine").mkdir(parents=True)
        (self.engine / "engine" / "score").mkdir()
        (self.engine / "engine" / "score" / "image.py").write_bytes(SCORER.read_bytes())

    def tearDown(self):
        self._tmp.cleanup()

    def gate_score(self, command_template: str) -> float | None:
        env = {"SCORE": hill.encode_command(command_template), "TARGET": "0.9"}
        command = buildjob.score_command(env, self.engine)
        out = self.work / "score.out"
        check = hill.check_with_score("", command, str(out))
        subprocess.run(["bash", "-c", check], cwd=self.repo, check=False, capture_output=True, timeout=120)
        return hill.read_score(out)

    def test_the_score_command_names_the_engine_checkout_by_absolute_path(self):
        env = {"SCORE": hill.encode_command("python3 -I {scorer}"), "TARGET": "0.9"}
        command = buildjob.score_command(env, self.engine)
        self.assertEqual(command, "python3 -I " + shlex.quote(str(self.engine / "engine" / "score" / "image.py")))

    def test_a_scorer_the_repo_committed_cannot_set_the_score(self):
        # The engine's scorer, given no arguments, refuses (no number); the repo's own file and score.txt print 1.
        self.assertIsNone(self.gate_score("python3 -I {scorer}"))

    def test_the_old_relative_command_would_read_the_repo_file(self):
        # A control: the relative form runs the repo's own scorer and gets its 1, so the setup above does detect a cwd-relative command.
        self.assertEqual(self.gate_score("python3 -I engine/score/image.py"), 1.0)

    def test_the_score_command_with_no_scorer_placeholder_is_unchanged(self):
        env = {"SCORE": hill.encode_command("echo 0.5"), "TARGET": "0.9"}
        self.assertEqual(buildjob.score_command(env, self.engine), "echo 0.5")

    def test_the_default_mirror_line_scores_with_the_image_scorer(self):
        line = lines.load_line("mirror")
        self.assertEqual(line.score.scorer, "image")
        self.assertEqual(lines.IMAGE_SCORE_COMMAND, "python3 -I {scorer}")


if __name__ == "__main__":
    unittest.main()
