# Contributing

## Cutting a release

Run the suite: `python3 test/run_suite.py`. Tag `vN` on `0xctx/pivotal` main and publish a GitHub Release. Copy `gh-piv/` to the root of `0xctx/gh-piv` (via `git subtree split --prefix=gh-piv` or a manual copy), tag it the same, and re-run the sandbox issue.