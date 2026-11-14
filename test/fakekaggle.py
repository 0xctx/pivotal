"""A fake kaggle CLI for tests: the fake gh's machinery (test/fakegh.py) with the flags of the real `kaggle`.

Tests point PIV_KAGGLE at ``FakeKaggle.path``, so the code under test runs the fake as a real subprocess and
never reaches Kaggle. The flag tables are read from `kaggle datasets <command> --help` in kaggle 2.2.4. A
command or flag not listed is refused with a usage error, exactly as the fake gh refuses one it lacks.
"""

import json
import sys
from pathlib import Path

import fakegh

DIR_MODES = ["skip", "zip", "tar"]  # `-r {skip,zip,tar}` in the real help

# Each command the piv kaggle commands run, with the flags it takes and whether each takes a value.
REAL_FLAGS = {
    "datasets status": {"--format": True, "-h": False, "--help": False},
    # `kaggle datasets list --help` (kaggle 2.2.4): the flags the 403 check uses, and the rest of the real command's flags.
    "datasets list": {"--mine": False, "-m": False, "--csv": False, "-v": False, "--search": True, "-s": True,
                      "--user": True, "--page-size": True, "--page-token": True, "--page": True, "-p": True,
                      "--sort-by": True, "--size": True, "--file-type": True, "--license": True, "--tags": True,
                      "--format": True, "--max-size": True, "--min-size": True, "-h": False, "--help": False},
    "datasets create": {
        "-p": True, "--path": True, "-u": False, "--public": False, "-q": False, "--quiet": False,
        "-t": False, "--keep-tabular": False, "-r": DIR_MODES, "--dir-mode": DIR_MODES, "--ignore-patterns": True,
        "-h": False, "--help": False,
    },
    "datasets version": {
        "-m": True, "--message": True, "-p": True, "--path": True, "-q": False, "--quiet": False,
        "-t": False, "--keep-tabular": False, "-r": DIR_MODES, "--dir-mode": DIR_MODES, "-d": False,
        "--delete-old-versions": False, "--ignore-patterns": True, "-h": False, "--help": False,
    },
}


class FakeKaggle(fakegh.FakeGh):
    tool = "kaggle"
    env_name = "PIV_KAGGLE"

    def __init__(self, root: Path):
        super().__init__(root, flags=REAL_FLAGS)


# The two kernel commands models-up runs. Flags as `kaggle kernels push --help` and `kernels status --help` list them
# (docs/engines.md: `push -p <folder> [--accelerator ACC] [-t TIMEOUT]`; `status <owner>/<kernel>` takes no flag).
KERNEL_FLAGS = {
    "kernels status": {"-h": False, "--help": False},
    "kernels push": {"-p": True, "--path": True, "-t": True, "--timeout": True, "--accelerator": True, "-h": False, "--help": False},
}
REAL_FLAGS.update(KERNEL_FLAGS)

_KERNEL_SCRIPT = """#!PYTHON
import fcntl
import json
import os
import re
import sys

STATE = __STATE__
FLAGS = __FLAGS__
argv = sys.argv[1:]


def refuse(message):
    sys.stderr.write("fake kaggle: " + message + "\\n")
    sys.exit(2)


key = next((" ".join(argv[:2]) for _ in [0] if " ".join(argv[:2]) in FLAGS), None)
if key is None:
    refuse("no such command: " + " ".join(argv[:2]))
i = 2
while i < len(argv):
    token = argv[i]
    i += 1
    if token.startswith("-") and token != "-":
        if token not in FLAGS[key]:
            refuse(f"kaggle {key} has no flag {token}")
        if FLAGS[key][token]:
            i += 1
paths = [argv[j + 1] for j, a in enumerate(argv[:-1]) if a in ("-p", "--path")]

lock = open(STATE + ".lock", "a")
fcntl.flock(lock, fcntl.LOCK_EX)
with open(STATE, encoding="utf-8") as fh:
    st = json.load(fh)
log = {"argv": argv, "env": dict(os.environ)}
try:
    if key == "kernels status":
        st["status_calls"] += 1
        if st.get("status_error"):
            sys.stderr.write(st["status_error"] + "\\n")
            sys.exit(1)
        if st["script"]:
            word = st["script"].pop(0) if len(st["script"]) > 1 else st["script"][0]
            st["status"] = word
        # the endpoint comes up after enough status reads since a push
        if st.get("pushed_at_call") is not None and st["status_calls"] - st["pushed_at_call"] >= st["up_after"]:
            st["up"] = True
            st["status"] = "RUNNING"
        sys.stdout.write(f'{argv[2]} has status "KernelWorkerStatus.{st["status"]}"\\n')
    else:
        folder = paths[0]
        meta = json.load(open(os.path.join(folder, "kernel-metadata.json"), encoding="utf-8"))
        notebook = open(os.path.join(folder, meta["code_file"]), encoding="utf-8").read()
        log["metadata"], log["notebook"] = meta, notebook
        st["pushes"].append({"metadata": meta, "notebook": notebook})
        if st.get("push_error"):
            sys.stderr.write(st["push_error"] + "\\n")
            sys.exit(st.get("push_exit", 0))
        st["status"], st["pushed_at_call"] = "QUEUED", st["status_calls"]
        st["script"] = list(st.get("after_push", []))
        sys.stdout.write("Kernel version 1 successfully pushed.  Please check progress at https://www.kaggle.com/code/x\\n")
finally:
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    with open(STATE + ".calls", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(log) + "\\n")
"""


class FakeKernel:
    """A stateful fake `kaggle` for the kernel commands: ONE kernel whose status all callers share, in a locked JSON file.

    ``status`` prints the real form (``<ref> has status "KernelWorkerStatus.RUNNING"``). ``script`` is a list of statuses
    handed out one per read (the last one sticks), which is how a test plays another run's pushes. ``push`` records the
    folder (metadata and notebook) and sets QUEUED; after ``up_after`` further status reads the kernel is RUNNING and the
    file's ``up`` flag, which a fake endpoint reads, is true. ``push_error`` makes push fail with that text; ``after_push`` is the ``script`` the push leaves behind.
    """

    env_name = "PIV_KAGGLE"

    def __init__(self, root: Path, *, status: str = "COMPLETE", up_after: int = 1):
        self.dir = root
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "kaggle"
        self.state = self.dir / "kernel.json"
        self.write(status=status, script=[], up_after=up_after, up=False, pushes=[], status_calls=0, pushed_at_call=None)
        script = _KERNEL_SCRIPT.replace("#!PYTHON", "#!" + sys.executable).replace("__STATE__", repr(str(self.state)))
        self.path.write_text(script.replace("__FLAGS__", repr(KERNEL_FLAGS)), encoding="utf-8")
        self.path.chmod(0o755)

    def write(self, **fields) -> None:
        current = json.loads(self.state.read_text()) if self.state.exists() else {}
        current.update(fields)
        self.state.write_text(json.dumps(current), encoding="utf-8")

    def read(self) -> dict:
        return json.loads(self.state.read_text())

    def calls(self) -> list[dict]:
        path = Path(str(self.state) + ".calls")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def pushes(self) -> list[dict]:
        return self.read()["pushes"]
