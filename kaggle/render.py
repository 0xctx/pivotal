"""Render kaggle/serve.ipynb.tmpl for one catalog candidate. Python stdlib only.

    python3 kaggle/render.py --model qwen3.6-35b-a3b > serve.ipynb

Two things are substituted, and only after they are checked:
  * ``__LIFECYCLE__``: the whole text of kaggle/lifecycle.py, so the notebook runs the code the tests run;
  * ``__SPEC__``: one Python string literal holding the spec as JSON (``json.dumps`` of ``json.dumps``, ASCII only).
The template is parsed as JSON first and the substitution happens inside the parsed cells, so no value can break
out of the notebook's JSON, and a value is data inside a string literal, so none can break out of the Python.

The catalog says what was measured. ``unmeasured`` is refused. ``partial`` and ``measured`` render, but a number the
catalog holds as null is never made up. The llama-server configuration (slots, per-slot context, flags) is the
catalog's ``serve`` field; a candidate without it is refused, and so is a per-slot context below ``MIN_AGENT_CTX``.
The time figures in the catalog were measured with an older configuration, not with ``serve`` (see each serve.source). The fetched things (ngrok, huggingface_hub, the llama.cpp commit and its sha256, the model revision) come from
kaggle/pins.json; a null pin refuses the render, so nothing unpinned reaches the process holding the keys.
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "gh-piv"))

from piv import serve  # noqa: E402  the dataset and file names are piv kaggle setup's, not repeated here

TEMPLATE = HERE / "serve.ipynb.tmpl"
LIFECYCLE = HERE / "lifecycle.py"
CATALOG = HERE / "catalog.json"

PINS = HERE / "pins.json"
MAX_SLOTS = 16
MAX_CTX = 1 << 20
# The least per-slot context an agent can work in. pi's own system prompt is about 1600 tokens, and 4096 per slot refused
# the first live agent request (2026-11-11): a served config below this floor is refused at render, not served silently.
MIN_AGENT_CTX = 16384
FLAG = re.compile(r"-{1,2}[A-Za-z][A-Za-z0-9-]{0,31}|[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
OWNED = {"-m", "--model", "--host", "--port", "--parallel", "-np", "-c", "--ctx-size", "--api-key", "--api-key-file",
         "--metrics", "--jinja", "--threads-http", "--path", "--ssl-key-file", "--ssl-cert-file"}  # the notebook's own
VERSION = re.compile(r"\d{1,4}(?:\.\d{1,4}){1,3}")
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")

MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
HF_OWNER = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
HF_FILE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
MODEL_PATH = re.compile(rf"({HF_OWNER}/{HF_OWNER})/((?:{HF_FILE}/)*{HF_FILE}\.gguf)")
FIELDS = {  # what each status must hold; anything else is null by the catalog's own rule
    "partial": ("model", "sysop_model_id", "time_to_healthy_s", "measured_on", "vendor"),
    "measured": ("model", "sysop_model_id", "quant", "context", "vram_gb", "tok_s", "time_to_healthy_s",
                 "measured_on", "vendor"),
}


class RenderError(Exception):
    """A refusal. Nothing is rendered."""


def candidates(catalog: dict) -> list[dict]:
    return [c for c in catalog.get("single_model_both_t4", []) if isinstance(c, dict)]


def pick(catalog: dict, model_id: str) -> dict:
    """The one single-model candidate the preset's model names. The two-small-models variant is not served."""
    if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
        raise RenderError("the model id must be lowercase letters, digits, '.', '_' or '-' (the preset's kaggle.model)")
    found = [c for c in candidates(catalog) if c.get("sysop_model_id") == model_id]
    if not found:
        known = ", ".join(sorted(str(c.get("sysop_model_id")) for c in candidates(catalog)))
        raise RenderError(f"{model_id} is not in the catalog's single-model candidates ({known})")
    if len(found) > 1:
        raise RenderError(f"{model_id} is in the catalog more than once")
    return found[0]


def pins_for(pins: dict, model_id: str) -> dict:
    """The validated pins, or a RenderError that names the one that is null or malformed."""
    if not isinstance(pins, dict):
        raise RenderError("pins must be an object (kaggle/pins.json)")
    llama = pins.get("llama_cpp") if isinstance(pins.get("llama_cpp"), dict) else {}
    revisions = pins.get("model_revisions") if isinstance(pins.get("model_revisions"), dict) else {}
    wanted = (("ngrok", pins.get("ngrok"), VERSION), ("huggingface_hub", pins.get("huggingface_hub"), VERSION),
              ("llama_cpp.commit", llama.get("commit"), HEX40), ("llama_cpp.sha256", llama.get("sha256"), HEX64),
              (f"model_revisions.{model_id}", revisions.get(model_id), HEX40))
    for name, value, shape in wanted:
        if value is None:
            raise RenderError(f"pin {name} is null in kaggle/pins.json: confirm it on a live run (models-up) and set it")
        if not isinstance(value, str) or not shape.fullmatch(value):
            raise RenderError(f"pin {name} is malformed")
    return {"ngrok": pins["ngrok"], "huggingface_hub": pins["huggingface_hub"], "llama_commit": llama["commit"],
            "llama_sha256": llama["sha256"], "revision": revisions[model_id]}


def serve_for(entry: dict) -> dict:
    """slots, per-slot context and flags from the catalog's ``serve`` field, validated."""
    serve_cfg = entry.get("serve")
    if not isinstance(serve_cfg, dict):
        raise RenderError(f"candidate {entry.get('candidate')} has no serve field (slots, ctx, flags with their source)")
    slots, ctx, flags = serve_cfg.get("slots"), serve_cfg.get("ctx"), serve_cfg.get("flags")
    if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= MAX_SLOTS:
        raise RenderError(f"serve.slots must be a whole number from 1 to {MAX_SLOTS}")
    if isinstance(ctx, bool) or not isinstance(ctx, int) or not 256 <= ctx <= MAX_CTX:
        raise RenderError("serve.ctx must be a whole number of tokens between 256 and 1048576")
    if ctx < MIN_AGENT_CTX:
        raise RenderError(f"serve.ctx {ctx} is below {MIN_AGENT_CTX} tokens per slot: an agent's first request is about 1600 "
                          "tokens of system prompt before the task and the files it reads")
    if not isinstance(serve_cfg.get("source"), str) or not serve_cfg["source"].strip():
        raise RenderError("serve.source (where the flags were measured) is required")
    if not isinstance(flags, list) or not flags or len(flags) > 40 or not all(isinstance(f, str) and FLAG.fullmatch(f) for f in flags):
        raise RenderError("serve.flags must be a list of plain llama-server flags and values")
    if OWNED.intersection(flags):
        raise RenderError(f"serve.flags may not set {', '.join(sorted(OWNED.intersection(flags)))}: the notebook sets those")
    return {"slots": slots, "ctx": ctx, "flags": list(flags)}


def spec_for(entry: dict, pins: dict) -> dict:
    """The validated spec for one candidate, or a RenderError that names the field."""
    status = entry.get("status")
    if status == "unmeasured" or status not in FIELDS:
        raise RenderError(f"candidate {entry.get('candidate')} is {status!r}: only a partial or measured candidate renders")
    missing = [name for name in FIELDS[status] if entry.get(name) is None]
    if missing:
        raise RenderError(f"candidate {entry.get('candidate')} is {status} but lacks {', '.join(missing)}")
    path = MODEL_PATH.fullmatch(entry["model"]) if isinstance(entry["model"], str) else None
    if not path or ".." in entry["model"].split("/"):
        raise RenderError("model must be owner/repo/file.gguf with plain names")
    if entry.get("context") is not None and (isinstance(entry["context"], bool) or not isinstance(entry["context"], int)
                                              or not 256 <= entry["context"] <= MAX_CTX):
        raise RenderError("context must be a whole number of tokens between 256 and 1048576")
    cfg = serve_for(entry)
    if entry.get("context") is not None and entry["context"] != cfg["ctx"] * cfg["slots"]:
        raise RenderError(f"context {entry['context']} is not serve.ctx x serve.slots ({cfg['ctx']} x {cfg['slots']}): "
                          "the figure must describe what is served")
    return {
        "id": entry["sysop_model_id"],
        "repo": path.group(1),
        "file": path.group(2),
        "slots": cfg["slots"],
        "ctx": cfg["ctx"],
        "flags": cfg["flags"],
        "pins": pins_for(pins, entry["sysop_model_id"]),
        "serve_file": serve.SERVE_FILE,
    }


def _literal(spec: dict) -> str:
    """A Python string literal that json.loads turns back into the spec. ASCII only: every non-ASCII character is a
    \\uXXXX escape, which is a valid escape in both JSON and Python."""
    return json.dumps(json.dumps(spec, ensure_ascii=True, sort_keys=True), ensure_ascii=True)


def render(catalog: dict, model_id: str, pins: dict) -> dict:
    """The notebook (a dict, ready for json.dumps) for the preset's model."""
    entry = pick(catalog, model_id)
    spec = spec_for(entry, pins)
    notebook = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    lifecycle = LIFECYCLE.read_text(encoding="utf-8")
    used = {"__SPEC__": 0, "__LIFECYCLE__": 0}
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        text = "".join(cell["source"])
        if text.strip() == "__LIFECYCLE__":
            text = lifecycle
            used["__LIFECYCLE__"] += 1
        elif "__SPEC__" in text:
            text = text.replace("__SPEC__", _literal(spec))
            used["__SPEC__"] += 1
        cell["source"] = text.splitlines(keepends=True)
    if used != {"__SPEC__": 1, "__LIFECYCLE__": 1}:
        raise RenderError(f"the template must hold each placeholder exactly once, found {used}")
    return notebook


def render_text(catalog: dict, model_id: str, pins: dict) -> str:
    return json.dumps(render(catalog, model_id, pins), indent=1) + "\n"


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="Render the Kaggle serve notebook for a catalog model")
    parser.add_argument("--model", required=True, help="the preset's kaggle.model, for example qwen3.6-35b-a3b")
    parser.add_argument("--catalog", default=str(CATALOG))
    parser.add_argument("--pins", default=str(PINS), help="the pinned versions, commit and revisions (default kaggle/pins.json)")
    args = parser.parse_args(argv)
    try:
        catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
        pins = json.loads(Path(args.pins).read_text(encoding="utf-8"))
        text = render_text(catalog, args.model, pins)
    except (RenderError, OSError, ValueError) as err:
        print(f"render: {err}", file=sys.stderr)
        return 2
    out.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
