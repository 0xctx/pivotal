# Pivotal ⚡

Label an issue. A model builds it on GitHub's own machines, a different model reviews it, your tests gate it, and you say "land". No server, no database, nothing to run on your computer.

## How it works

```text
issue labelled
   ↓
GitHub runner builds
   ↓
another vendor's model reviews
   ↓
your tests gate
   ↓
PR ready for you
   ↓
you comment `/pivotal land`
```

## Try it

```bash
gh extension install 0xctx/gh-piv
piv init you/my-app --check 'npm test'   # once per repo
piv do you/my-app "add CSV export"       # or label any issue `pivotal`
piv land you/my-app                      # or comment /pivotal land
```

## What you need

A GitHub repo with Actions on, and one model key stored as a secret. The free path uses a Kaggle GPU for building and free reviewer keys (Groq, Gemini); there is also a one-key path through OpenRouter. Open models come first; Claude is optional and paid. Details: [Quickstart](docs/QUICKSTART.md).

## Status

Version 1. On the free path a labelled issue has run on real GitHub up to a ready pull request with a verified seal; landing it, the incident and event triggers and the self-hosted runners are not proven live yet. The full record, with run ids, is in [the live record](docs/engines.md#live-record).

## Docs

[Quickstart](docs/QUICKSTART.md) · [Security](docs/SECURITY.md) · [FAQ](docs/FAQ.md) · [Models](docs/engines.md) · [Examples](docs/EXAMPLES.md)
