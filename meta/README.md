# The network-tools meta module

`meta/network-tools` is the network-tools centre's module for `meta`, the front door to every
knowledge centre (brain record 0019). It is not part of the interpose library: the wheel and the
sdist leave `meta/` out. It is one executable Python script, standard library only, that
describes itself (`--meta-manifest`) and answers three read-only verbs about interpose, sluice
and mitm-proxy.

Register it on the network-tools coordinator's actor note as:

```yaml
meta_module: /pool/distributed/dev/interpose/meta/network-tools
```

## Verbs

Each verb gives plain output for people and `grep`, or one JSON value with `--json` (or
`META_JSON=1`). The core caches the whole output; the module keeps no cache of its own.

| Verb | Cache | Shows |
|---|---|---|
| `status` | 120 s | A summary line, then one line per check: `ok`, `warn`, `fail` or `unknown`, the check's name and a detail |
| `list prs\|releases\|repos` | 900 s | One name per line, sorted: `<repo>#<n>` for PRs, `<repo> <tag>` for releases |
| `show <repo>` | 120 s | `key: value` lines: the latest release, CI on `main`, open PRs and issues, the interpose pin, and the local checkout under `/pool/distributed/dev/<repo>` (branch and whether it is clean) |

`meta network-tools --help` lists the verbs, and `<verb> --help` gives one verb's usage.

## The status checks

| Check | ok | warn | fail |
|---|---|---|---|
| `ci <repo>`: the latest completed CI run on `main` | it passed | cancelled or skipped | it failed |
| `prs <repo>`: open PRs other than release PRs | none open over 3 days | any open over 3 days | |
| `release-pr <repo>`: the open release PR | none, or open up to 14 days | open over 14 days | |
| `pin <repo>`: the interpose pin in `pyproject.toml` on `main` (sluice, mitm-proxy) | the latest interpose release | behind (or ahead of) it | |

A release PR is one by `app/metalayerlab-stardate` whose title starts `chore(main): release`. A
check whose `gh` call failed is `unknown`, with one warning line on stderr.

## Exit codes

| Code | Means |
|---|---|
| 0 | All well: every status check ok |
| 1 | `status` found a check that is not ok |
| 2 | Usage: an unknown verb, argument or option |
| 4 | `gh` is missing, unreachable, failing or too slow for `META_TIMEOUT` |
| 5 | `show` of a repo that isn't one of the three |

Warnings and errors go to stderr, one line each, starting `meta network-tools:`.

## How it reads, and what it never does

- GitHub only through the `gh` CLI, with read-only calls: `run list`, `pr list`, `issue list`,
  `release list`, `release view`, and plain `gh api repos/…` reads. A guard refuses anything
  else before it runs.
- The local checkouts only with `git rev-parse` and `git --no-optional-locks status
  --porcelain`, which never changes them.
- Per-repo calls run in parallel, each with a timeout that fits inside `META_TIMEOUT`.
- It never prints, reads or asks for a token, key or cookie, and writes no file.

## Tests

`tests/unit/test_meta_network_tools.py` runs the module as the core does, through a fake core
that validates the manifest and every run's exit code, stderr and output. A fake `gh` on `PATH`
serves the recorded responses in `tests/unit/meta_fixtures/gh.json`, so CI never calls GitHub.
