"""The serve notebook's decisions, as pure functions and a loop whose clock, sleep and sampler are injected.

kaggle/render.py copies this file whole into a cell of the rendered notebook, so the code that runs on
Kaggle is the code test/test_notebook.py runs. Stdlib only. Nothing here touches the network, a process
or the disk (inference only, exits when idle, never chained).
"""

import re
import urllib.parse

IDLE_S = 20 * 60  # exit after 20 minutes without a request
HARD_S = 11 * 3600  # and at 11 hours regardless, from the start of the notebook (Kaggle's own cap is 12 h)
LOAD_S = 15 * 60  # a server that is not healthy by then is a failed start, not an idle one (a design bound)
POLL_S = 5
MIN_SECRET = 16  # a shorter value is not masked (it would blank ordinary words); the key is 43 characters
MASK = "[hidden]"

# llama-server's /metrics (enabled with --metrics): a counter that rises with every served request, and
# the number of requests being processed now. A request is use while it runs and after it ends.
# What counts as use is decided here: a request that decodes tokens. A GET /v1/models or /health, or any request
# that decodes nothing, is not use (the tunnel is not metered), so a client that only polls does not keep the
# session up. A client that wants the session kept must send a completion.
ACTIVITY = ("llamacpp:prompt_tokens_total", "llamacpp:tokens_predicted_total", "llamacpp:n_decode_total")
BUSY = "llamacpp:requests_processing"
# What ngrok says when the static domain is already online somewhere else: ERR_NGROK_334 is "endpoint already online".
BOUND = re.compile(r"ERR_NGROK_334|already\s+online|already\s+bound|endpoint\s+.{0,80}\s+is\s+already", re.IGNORECASE)


def domain_bound(message: str) -> bool:
    """True when ngrok refused the static domain because another session holds it (the cross-repo lock)."""
    return bool(BOUND.search(message))


def parse_activity(text: str) -> tuple[int, bool] | None:
    """(total of the served-work counters, whether a request is running) from llama-server's /metrics, or None
    when the text carries none of them. An unreadable answer is no activity: the idle clock keeps running."""
    seen: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2 and (parts[0] in ACTIVITY or parts[0] == BUSY):
            try:
                value = float(parts[1])
            except ValueError:
                continue
            if value == value and value >= 0:  # not NaN, not negative
                seen[parts[0]] = value
    if not any(name in seen for name in ACTIVITY):
        return None
    return int(sum(seen.get(name, 0) for name in ACTIVITY)), seen.get(BUSY, 0) > 0


class Watchdog:
    """The idle and hard deadlines. ``clock`` is any monotonic seconds function."""

    def __init__(self, clock, idle_s: float = IDLE_S, hard_s: float = HARD_S, load_s: float = LOAD_S):
        self.clock = clock
        self.idle_s, self.hard_s, self.load_s = idle_s, hard_s, load_s
        self.start = clock()
        self.launch_at: float | None = None
        self.ready_at: float | None = None
        self.last_use = self.start
        self.counter: int | None = None

    def launched(self) -> None:
        """llama-server starts now: the load budget counts from here, not from the start, so a long engine build or
        download (a cold build took 29 minutes) is not charged to the server's load."""
        self.launch_at = self.clock()

    def ready(self) -> None:
        """The model answers: the idle clock starts now, and the counter's present value is the baseline."""
        self.ready_at = self.last_use = self.clock()
        self.counter = None

    def observe(self, sample: tuple[int, bool] | None) -> None:
        """Feed one /metrics sample (None when it could not be read). A running request, or a counter that moved
        since the last sample, is use. The counter only rises; a lower value is a glitch, not use, and the highest
        value seen stays the baseline, so a counter that flips between two values cannot keep the session up."""
        if sample is None or self.ready_at is None:
            return
        counter, busy = sample
        if busy or (self.counter is not None and counter > self.counter):
            self.last_use = self.clock()
        if self.counter is None or counter > self.counter:
            self.counter = counter

    def verdict(self) -> str | None:
        """'hard', 'load', 'idle' or None. The hard limit wins, and it counts from the start, not from ready. Before
        the launch there is no load to time (the build and the download are bounded by their own limits)."""
        now = self.clock()
        if now - self.start >= self.hard_s:
            return "hard"
        if self.ready_at is None:
            if self.launch_at is None:
                return None
            return "load" if now - self.launch_at >= self.load_s else None
        if now - self.last_use >= self.idle_s:
            return "idle"
        return None


def watch(dog: Watchdog, sample, sleep, poll_s: float = POLL_S, alive=None) -> str:
    """Poll until a deadline passes and say which. ``sample()`` returns what parse_activity does; ``sleep(s)``
    advances the clock in a test; ``alive()`` is False once llama-server has died, which is 'error' (a dead server
    behind a live tunnel is a failure, not an idle one). Nothing here restarts anything: a verdict ends the loop."""
    while True:
        if alive is not None and not alive():
            return "error"
        dog.observe(sample())  # first: a request that began since the last poll must count before the deadline
        reason = dog.verdict()
        if reason:
            return reason
        sleep(poll_s)


def secret_forms(value: str) -> list[str]:
    """The ways a secret can show up in a line: as is, percent-encoded, and with plus for space."""
    forms = [value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)]
    return [form for form in dict.fromkeys(forms) if form]


def redact(text: str, secrets) -> str:
    """The text with every secret's forms masked. Secrets under MIN_SECRET characters are ignored."""
    forms = sorted({f for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET for f in secret_forms(s)},
                   key=len, reverse=True)
    for form in forms:
        text = text.replace(form, MASK)
    return text

