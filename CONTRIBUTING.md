# Contributing

## Cutting a release

Run the suite: `python3 test/run_suite.py`. Tag `vN` on `0xctx/pivotal` main and publish a GitHub Release. Copy `gh-piv/` to the root of `0xctx/gh-piv` (via `git subtree split --prefix=gh-piv` or a manual copy), tag it the same, and re-run the sandbox issue.

## Releases and pins

A published tag is immutable. Once another repo may pin it, `v1`, `v1.1` and every later tag is never moved, deleted
or force-pushed: every caller pins a commit, and a caller pinned to a commit that is gone (a rewritten history, a tag
and its commit deleted) fails with "ref not found" at its first job. A fix ships as a new tag on top of the history
(`v1.1` after `v1`), never as a rewritten one. The one rewrite that is allowed is before anyone else pins: while the
tag is your own and no other repo's caller names it, move it as often as you like.

`piv init REPO --release v1.1` pins the commit that tag points at; `--release` also takes a commit SHA, and without it
`piv init` pins the latest release. `piv doctor REPO` prints the caller's pin: the engine and the first twelve
characters of the commit, and compares that commit with the latest release: a caller that is behind is a problem with
a fix line.

To move a repo to a new release, re-run `piv init REPO --check 'your check command' --update`. To move many at once,
`piv upgrade --owner OWNER` re-pins every caller with the `pivotal` topic, `piv upgrade REPO` moves one, and
`--release REF` names the tag or commit instead of the latest release: one line per repo, `current`,
`updated <old sha12> -> <new sha12>` or `refused: <why>`, and exit 1 when any repo was refused. A caller that pins a fork
keeps its fork, and no variable, secret or label is touched.
