# Security

## Threat model

interpose is a library: it opens no ports and makes no network calls. What it protects is **secret configuration values on their way into a process**: proxy credentials, CA keys and API tokens read from `_FILE` variables, the environment, a YAML file or a secrets directory. The threat is accidental disclosure through logs, error messages, tracebacks, `repr()` in a debugger or a status page. A process that can read its own memory or its secrets' files is out of scope, as is protecting the files themselves (file modes, Kubernetes RBAC).

It trusts the files and environment it's pointed at. A config file can set any declared setting, so whoever can write it controls the consumer.

## Secrets

- The library reads secrets only from the files and variables its caller points it at, and never logs or stores them.
- CI scans every commit with gitleaks, and secrets in history fail the build.
- **`secret=True` settings resolve to `Secret`**, which prints as `********` in `str()`, `repr()`, f-strings, `%`-formatting, logging and exception messages. Only `reveal()` gives the value: call it at the point of use, never earlier.
- **Never `str()` a `Secret` to get its value**: you get `********`. That is a safe failure, but it's a bug.
- **Error messages never hold a secret setting's value**: a coercion error names the key and type only, and a `check`'s reason has the value masked. A check can't replace a secret setting's value, and `Resolved.str()` (or any accessor but `secret()`) on a secret setting is a `TypeError`, so the mask can't be sent as a credential. YAML syntax errors give a position, never the offending line.
- **Prefer `_FILE` for secrets** (Docker and Kubernetes secrets) over environment variables, which child processes inherit and `/proc/<pid>/environ` exposes. `read_secret_dir` reads a whole secrets directory for values that aren't settings.
- Nothing is logged. `Resolved.sources` says where each value came from, and is safe to show.

## Hardening

- **Supply chain:** dependencies are pinned in the lock file and updated weekly by Dependabot. GitHub Actions are pinned to commit SHAs.
- **One runtime dependency**, pyyaml, used through `yaml.safe_load` only: no arbitrary object construction from a config file.
- **Strict input handling:** a value that isn't the declared type is an error, never converted. A config file that isn't a mapping is refused.
- **Constant-time comparison** for `Secret` equality.
- **Consumers pin a tag.** Dependabot can't update a tag-pinned git dependency and looks the bare name `interpose` up on PyPI instead. interpose isn't published there, so treat any Dependabot PR that bumps `interpose` as suspect: it would be someone else's package ([FAQ](Frequently-Asked-Questions#does-dependabot-update-interpose)).

## Reporting a vulnerability

Report privately through [GitHub security advisories](https://github.com/metalayerlab/interpose/security/advisories/new), never in a public issue.
