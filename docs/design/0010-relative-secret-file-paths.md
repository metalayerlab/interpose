# 0010: Relative secret_file paths

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (v0.5.0)
Date: 2026-11-04
Authors: andronics; drafted by Claude
Approved: 2026-11-04 by the arbiter (question "Approve Interpose Record 0010 On Relative Secret Files").
Implemented: 2026-11-04, approved by the arbiter (question "Mark Interpose Record 0010 Implemented").

## Context

[0009](0009-settings-extensions.md) (Implemented, v0.3.0 to v0.4.1) added secret leaves to section
settings. Its rule, section 5:

> Inside a section, a mapping that is exactly `{secret_file: <path>}` is replaced, at resolve, by a
> `Secret` holding that file's contents, trailing newline stripped as for `_FILE`. An unreadable file is
> a `ConfigError` naming the section path and the file, never the contents.

and its decision E:

> **E. Control-plane values never read secret files.** Like C, a rule for remote layers: a
> `{secret_file: ...}` leaf is honoured only in a layer with `secret_files=True`, which only
> `read_file()` and a section's `_FILE` set. In any other layer, a remote payload above all, it is a
> `ConfigError` naming the section path and saying that secret files are read only from local
> configuration.

0009 doesn't say what a relative `<path>` is relative to. The code (`_secret_leaf`, v0.4.1) opens
`Path(file)` as written, so a relative path is read from the process's working directory
([#50](https://github.com/metalayerlab/interpose/issues/50), found in #46). The same YAML then works
or fails depending on where the service was started: `{secret_file: secrets/token}` beside
`/etc/myapp/config.yaml` is found when the service starts in `/etc/myapp`, and not when systemd starts
it in `/`.

The arbiter decided this as "Where Interpose Resolves A Relative Secret File" (2026-11-04), option (a):
resolve a relative path against the directory of the file that names it, ship it in interpose 0.5.0 as
a breaking change, and record it in a new record amending 0009, since an Implemented record is amended
by a new one, never rewritten (as 0009 amended 0001). This is that record. The code follows in its own
PR.

The arbiter approved this record on 2026-11-04 ("Approve Interpose Record 0010 On Relative Secret
Files"), option (a), with the three open questions answered as recommended.

Constraints, from 0009: secret leaves are read only from local configuration (decision E); errors name
paths, never contents; no I/O at import; stdlib and pyyaml only.

## Decision

This **amends 0009's `secret_file` rule** (section 5, and decision E's list of local layers).

### 1. A relative path resolves against the naming file's directory

- **A relative path** in a `{secret_file: <path>}` leaf resolves against the directory of the file
  that holds the leaf: the config file (`read_file()`, found from `path`, `<PREFIX>CONFIG` or
  `default_path`) or the section's `<PREFIX><KEY>_FILE`. The same applies to a leaf as a secret field's
  value in a list section's items, which resolve against the file their list came from. A layer the
  project builds has its own base, or none (section 2).
- **An absolute path** is unchanged.
- **Expansion comes first:** with `expand=True`, `${VAR}` in the path expands, and the result is
  then absolute or relative.
- **`~` is not expanded**, as today. A path whose first part starts with `~` (`~/token`, `~app/token`)
  is a `ConfigError`: `x.token: secret_file ~/token: '~' is not expanded; write the absolute path (from
  /etc/myapp/config.yaml)`. Today it silently means a directory named `~` under the working directory;
  resolving it against the config file's directory instead would be just as surprising. Refusing it
  leaves room to expand it later without changing what any working file means
  ([question 2](#open-questions)).
- **Symlinks are not resolved** in the base. The base is the naming file's directory as given
  (`/etc/myapp` for `/etc/myapp/config.yaml`), never its real path. A Kubernetes ConfigMap or Secret
  mount makes each file a symlink through `..data` into a timestamped directory that the kubelet
  replaces on every update; resolving through it would give a directory the operator never named, and
  one that disappears on the next update. The operator writes a relative path against the path they
  configured, so that is the base. The file itself is then opened normally, following symlinks as any
  open does.
- **A relative config path** (`MYAPP_CONFIG=config.yaml`) is made absolute against the working
  directory when the file is read (`Path.absolute()`, no symlink resolution), once, so the base never
  changes after that ([question 3](#open-questions)). This is the one place the working directory
  still counts, as it already does for finding the config file.

### 2. Where the value has no file: `Layer.base_dir`

Secret leaves are read only from a layer with `secret_files=True` (decision E): `read_file()`, a
section's `_FILE`, or a `Layer` the project builds itself with `secret_files=True`. Plain variables
can't hold a section at all (0009). The first two have a file; a project-built layer may not.

```python
@dataclass(frozen=True)
class Layer:
    data: Mapping[str, Any]
    source: str
    where: str
    origins: Mapping[str, str] = field(default_factory=dict)
    expand: bool = False
    secret_files: bool = False
    base_dir: Path | None = None  # new: where a relative secret_file resolves; None: nowhere
```

- **`base_dir: Path | None = None`**: the directory a relative `secret_file` path in this layer
  resolves against ([question 1](#open-questions)). `read_file()` sets it to the config file's directory (absolute, as above). A
  section's `_FILE` uses its own file's directory, whatever layer the rest came from. A project that
  builds a layer from a file it read itself sets it to that file's directory.
- **With `base_dir=None`, a relative path is a `ConfigError`** naming the leaf's path:
  `x.token: secret_file must be absolute here: this configuration has no file to resolve secrets/token
  against (from <where>)`. The file is never opened. A layer with `secret_files=False` still refuses
  every leaf first (decision E), whatever `base_dir` is.
- `base_dir` reads nothing and is never checked to exist; an unreadable file is the usual error.

### 3. Error messages

An error about a secret leaf names the leaf's path, the file as written and, for a relative one, the
resolved path, then the layer, never the contents:
`middleware.config.auth.token: cannot read secret_file secrets/token (/etc/myapp/secrets/token): No such
file or directory (from /etc/myapp/config.yaml)`. An absolute path is named once, as today.

## Alternatives considered

- **The working directory** (today). The same file means different things depending on where the
  service starts, and no operator writes a config file expecting that; it is the bug.
- **Refusing every relative path.** Safe and simple, but a config file and its secrets shipped together
  (a ConfigMap and Secret mounted side by side, a test fixture directory) can't be moved as a unit, and
  every other tool that reads a config file resolves its relative paths against it.
- **Resolving through symlinks** (`Path.resolve()` on the base). Gives a ConfigMap mount's timestamped
  `..2026_...` directory, which the operator never named and the kubelet removes on update, and breaks a
  config file symlinked in from elsewhere, where the operator writes paths against the link.
- **The layer's `where` as the base** instead of a new field. `where` is free text for messages
  (`"control plane (config v12)"`), not a path; parsing it would turn a message into an interface.

## Consequences

- **Breaking**, so interpose **0.5.0** (a 0.x minor, marked breaking): a relative `secret_file` path
  that worked from the working directory now resolves elsewhere, and a `~` path is an error. No known
  deployment uses relative paths: mitm-proxy, sluice and qbt-rules don't use secret leaves yet, and the
  examples use absolute `/run/secrets/...` paths. mitm-proxy and sluice take 0.5.0 through their tag
  check as usual.
- `Layer` gains `base_dir`, defaulting to `None`, so existing `Layer(...)` calls keep working; a
  project-built layer with `secret_files=True` and a relative leaf now needs it.
- The internal `_Found` carries the base down with `secret_files`, as it does `expand`.
- Tests: relative paths from the config file, a section's `_FILE` and a list item; a config path that
  is itself relative, resolved once; a symlinked config file (the base is the link's directory); `~`
  refused; `base_dir=None` refused unopened; messages name both paths and no contents.
- Wiki: Security (where a relative path resolves, and why not through symlinks), API-Reference
  (`Layer.base_dir`, `read_file()`, secret leaves), Troubleshooting (the two new errors, and the
  "cannot read secret_file" entry naming the resolved path), Architecture-Internals (secret leaves).
- This amends 0009's `secret_file` rule; 0009 itself isn't rewritten, and its index row points here.
  TODO.md's "Decided against" gains the working directory and resolving through symlinks.

## Open questions

Answered by the arbiter on 2026-11-04, each with the recommended answer.

1. **The field's name.** Recommend `base_dir: Path | None = None`: it says it is a directory, and
   `base` alone reads like a base layer in a list of layers. `relative_to` would also work but reads as
   a method. **Answer: `base_dir: Path | None = None`.**
2. **`~`.** Recommend refusing it, as in section 1: it has never been expanded, so nothing working
   depends on it, and an error now keeps expanding it later a non-breaking choice. The other options are
   expanding it (`Path.expanduser()`, which depends on the service user's home, rarely what a service
   wants) or treating it as an ordinary relative part (`<base>/~/token`, surprising). **Answer:
   refused,** a `ConfigError`.
3. **Should a relative config path's base be fixed at read time?** Recommend yes (`Path.absolute()` in
   `read_file()`), so a reload after the process changes directory resolves the same files.
   **Answer: yes,** made absolute once, when the file is read.
