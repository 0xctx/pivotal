"""pi's agent directory for one call (docs/engines.md; F3).

pi reads its provider config from ``<agent dir>/models.json`` (docs/models.md). A pi call gets a fresh 0700 agent
directory of its own, made by mkdtemp (never inside the repo checkout) and removed when the call ends, so no config of
the runner's user is read. For the ``kaggle`` door the file names the endpoint (``https://<domain>/v1``, with the ngrok
header that skips the browser warning) and the model. Its ``apiKey`` is the text ``$PIVOTAL_KAGGLE_KEY``: pi interpolates
it from its own environment, and run.sh gives that environment the one secret of the call. The key value is never
written to a file, an argv or a log. ``PI_CODING_AGENT_DIR`` names the directory, for the agent child only (run.sh).

The context a served model gets is the one ``kaggle/catalog.json`` records for it (``serve.ctx``, the per-slot context
the figures were measured with). The catalog has no measured context window of its own (``context`` is null), so the
served value is the one used, and ``maxTokens`` is half of it: a guess of this engine, not a measurement.
"""

import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from engine import modelsup, route
from engine.job import JobError

KEY_REF = "$PIVOTAL_KAGGLE_KEY"  # pi interpolates this from its environment at request time; the file holds the name only
PROVIDER = "kaggle"  # the door name: pi's model id for the preset's entry is kaggle/<model>
HEADERS = {"ngrok-skip-browser-warning": "1"}  # the free ngrok domain serves a warning page to browser-like clients
# pi's own settings for every call: a repo's `.pi/` resources (extensions, prompts, settings) are protected project resources,
# and with "never" pi skips them without asking. Set only in the agent directory, the one place pi reads it (docs/security.md).
SETTINGS = {"defaultProjectTrust": "never"}


def read_catalog(root: Path) -> Mapping | None:
    """kaggle/catalog.json as it was read before the agent started (the engine holds it, like its own files), or None when
    it is missing or not a JSON object. A kaggle call with no catalog is refused; a call with no kaggle door never needs it."""
    try:
        data = json.loads((root / "kaggle" / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def served_context(catalog: Mapping, model: str) -> int:
    """The per-slot context kaggle/catalog.json records for ``model`` (its ``sysop_model_id``). Refused when there is none."""
    for rows in catalog.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("sysop_model_id") == model:
                serve = row.get("serve")
                if isinstance(serve, dict) and type(serve.get("ctx")) is int and serve["ctx"] > 0:
                    return serve["ctx"]
    raise JobError(f"kaggle model {model} has no served context in kaggle/catalog.json")


def models_config(domain: str, model: str, context: int) -> dict:
    """The ``models.json`` document for the kaggle door: one provider, one model, the key by name only."""
    return {"providers": {PROVIDER: {
        "baseUrl": f"https://{domain}/v1",
        "api": "openai-completions",
        "apiKey": KEY_REF,
        "headers": dict(HEADERS),
        "models": [{"id": model, "name": model, "contextWindow": context, "maxTokens": max(1, context // 2),
                    "reasoning": False}],
    }}}


def _write_new(path: Path, data: bytes) -> None:
    # O_EXCL: the name must not exist, a link included. O_NOFOLLOW is belt and braces next to it: a redundant refusal of a link.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


@contextmanager
def call_dir(entry: route.Entry, env: Mapping[str, str], catalog: Mapping | None) -> Iterator[Path | None]:
    """The agent directory of one call: None for a worker that is not pi, else a fresh directory that holds settings.json
    (SETTINGS) and, for the kaggle door, models.json; removed when the call ends."""
    if entry.worker != "pi":
        yield None
        return
    directory = Path(tempfile.mkdtemp(prefix="pivotal-pi-"))
    try:
        _write_new(directory / "settings.json", (json.dumps(SETTINGS, indent=2) + "\n").encode("utf-8"))
        if entry.door == PROVIDER:
            endpoint = modelsup.read_config(env, need_kaggle=False)
            if not endpoint.domain:
                raise JobError("PIVOTAL_KAGGLE_URL is not set: pi cannot reach the kaggle endpoint")
            if catalog is None:
                raise JobError("kaggle/catalog.json cannot be read: no served context for the kaggle model")
            document = models_config(endpoint.domain, entry.model, served_context(catalog, entry.model))
            _write_new(directory / "models.json", (json.dumps(document, indent=2) + "\n").encode("utf-8"))
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
