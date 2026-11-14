#!/usr/bin/env python3
"""Measure one Kaggle-served llama-server endpoint: time to healthy and tok/s.

Polls GET /v1/models until it answers 200 (time to healthy, counted from the
first poll), then times one POST /v1/chat/completions. tok/s is the completion
tokens the server reports divided by the whole request time, so prompt
processing is included. The key is read from the environment variable named
by --key-env and is never printed.

Usage: kaggle_probe.py --base-url https://<domain> --key-env SERVE_KEY \
           --model <id> [--max-tokens 256] [--timeout 720]
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def call(url, key, method="GET", body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read() or b"null")


def wait_healthy(base, key, timeout, poll=5.0, clock=time.monotonic, sleep=time.sleep):
    """Return seconds until /v1/models answers 200. A 401 or 403 fails at once."""
    start = clock()
    while True:
        try:
            status, _ = call(f"{base}/v1/models", key, timeout=10)
            if status == 200:
                return clock() - start
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise
        except OSError:
            pass
        if clock() - start >= timeout:
            raise TimeoutError(f"not healthy after {timeout:.0f}s")
        sleep(poll)


def measure(base, key, model, max_tokens, clock=time.monotonic):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Write one sentence about the sea."}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    start = clock()
    _, resp = call(f"{base}/v1/chat/completions", key, method="POST", body=body, timeout=300)
    seconds = clock() - start
    tokens = resp["usage"]["completion_tokens"]
    return {
        "completion_tokens": tokens,
        "seconds": round(seconds, 2),
        "tok_s": round(tokens / seconds, 1) if seconds > 0 else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-env", required=True, help="name of the env var holding the key")
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=720, help="seconds to wait for health")
    args = parser.parse_args(argv)

    key = os.environ.get(args.key_env)
    if not key:
        print(f"error: env var {args.key_env} is not set", file=sys.stderr)
        return 2
    base = args.base_url.rstrip("/")
    result = {"model": args.model}
    result["time_to_healthy_s"] = round(wait_healthy(base, key, args.timeout), 1)
    result.update(measure(base, key, args.model, args.max_tokens))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
