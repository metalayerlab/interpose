# Security

## Threat model

interpose is a library: it opens no ports and makes no network calls. What it protects is **secret configuration values on their way into a process**: proxy credentials, CA keys and API tokens read from `_FILE` variables, the environment, a YAML file or a secrets directory. The threat is accidental disclosure through logs, error messages, tracebacks, `repr()` in a debugger or a status page. A process that can read its own memory or its secrets' files is out of scope, as is protecting the files themselves (file modes, Kubernetes RBAC).

It trusts the files and environment it's pointed at. A config file can set any declared setting, so whoever can write it controls the consumer. With `Settings(expand=True)`, the file can also read any of the process's environment variables into a setting through `${VAR}`; it is local configuration, so that is no more than its writer could already do.

A `Layer` built from a remote payload (a control plane's managed config) is trusted less: **never set `expand=True` on one.** Expansion there would let the control plane read the instance's environment, secrets included, by sending `${API_TOKEN}` in a setting it later reads back. interpose can't tell a remote layer from a local one, so the rule is the project's to keep: `expand` defaults to false, and `read_file()` is the only place interpose sets it ([0009](https://github.com/metalayerlab/interpose/blob/main/docs/design/0009-settings-extensions.md), decision C).

**Never set `secret_files=True` on one either.** A section's `{secret_file: <path>}` leaf makes the process read a file into a `Secret`; from a remote payload, it would let the control plane name any file the process can read and have it loaded into configuration it may later read back. So a secret leaf is honoured only in a layer with `secret_files=True`, which only `read_file()` and a section's `_FILE` set. Anywhere else it is a `ConfigError` naming the section path and saying that secret files are read only from local configuration: the file is never opened, and the leaf is never kept as a plain mapping ([0009](https://github.com/metalayerlab/interpose/blob/main/docs/design/0009-settings-extensions.md), decision E). A project that builds a `Layer` from a remote payload leaves `secret_files` at its default, false.

**Where a relative secret file resolves.** A relative `{secret_file: <path>}` resolves against the directory of the file that holds the leaf: the config file's, or a section's `_FILE`'s. Never the working directory, so the same file means the same secrets wherever the service starts. The base is the directory as named (`/etc/myapp` for `/etc/myapp/config.yaml`), never resolved through symlinks: a Kubernetes ConfigMap or Secret mount links each file through `..data` into a timestamped directory that the kubelet replaces on every update, and a config file linked in from elsewhere is written against the link. A relative config path is made absolute once, when it is read, so a later `chdir` can't move its secrets. A layer with no file behind it (`base_dir=None`) refuses a relative path, unopened, and `~` is refused rather than expanded. Decision E still comes first: a remote layer's leaf is refused whatever its `base_dir` ([0010](https://github.com/metalayerlab/interpose/blob/main/docs/design/0010-relative-secret-file-paths.md)).

## Secrets

- The library reads secrets only from the files and variables its caller points it at, and never logs or stores them.
- CI scans every commit with gitleaks, and secrets in history fail the build.
- **`secret=True` settings resolve to `Secret`**, which prints as `********` in `str()`, `repr()`, f-strings, `%`-formatting, logging and exception messages. Only `reveal()` gives the value: call it at the point of use, never earlier.
- **Never `str()` a `Secret` to get its value**: you get `********`. That is a safe failure, but it's a bug.
- **Error messages never hold a secret setting's value**: a coercion error names the key and type only, and a `check`'s reason has the value masked. A check can't replace a secret setting's value, and `Resolved.str()` (or any accessor but `secret()`) on a secret setting is a `TypeError`, so the mask can't be sent as a credential. YAML syntax errors give a position, never the offending line.
- **Expansion and secrets.** A secret setting may be written `${API_TOKEN}` in an expanding file; it resolves to a `Secret` as any other. Variables and `_FILE` contents never expand, so a secret file holding `${` arrives as written. An expansion error names the key and the variable, never the variable's value or the text around it. A coercion error on an expanded value shows the text as written (`got 'https://user:${PASS}@host'`), not the expanded one, since a non-secret value can embed a secret variable; a check's reason has the expanded value replaced by the text as written, and for a secret setting, both show nothing. A check that echoes only part of its value (the host of a URL, say) can't be caught this way: a check should name the problem, not quote the value.
- **Secret leaves.** A section never holds a secret inline: write `{secret_file: /run/secrets/token}` and it resolves to a `Secret`, read from local configuration only (above). An unreadable file's error names the section path and the file as written (and, for a relative one, where it resolved), never its contents. A section's `repr()` masks every `Secret` in it, and a check's reason has every secret leaf's value masked, as written and inside a `repr()`. In a list section with fields, a `secret=True` field resolves to a `Secret` from an inline value, a secret leaf or an indexed `_FILE`; a leaf is read for a secret field only, and from a remote layer it is refused before anything is opened, whatever the field.
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
