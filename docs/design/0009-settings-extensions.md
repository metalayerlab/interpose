# 0009: Settings extensions

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Approved
Date: 2026-11-03
Authors: andronics; drafted by Claude
Approved: 2026-11-04 by the brain coordinator for Stephen.

## Context

qbt-rules is moving its configuration onto `interpose.settings` (v0.2.x) and had to build five things in
its own layer that every consumer will want. Each is an issue here. qbt-rules' current behaviour is in
the comments on #32 and #35 and in its `src/qbt_rules/config.py` (read at its v0.11.1):

- **[#32](https://github.com/metalayerlab/interpose/issues/32) `${VAR}` expansion.** qbt-rules expands
  `${VAR}` and `${VAR:-default}` in every string value of its YAML tree, inside longer strings too
  (`http://${HOST}:8080`), before typing (`${PORT:-5000}` is the int 5000 for an int setting). An unset
  `${VAR}` with no default becomes the empty string, silently; there is no escape.
- **[#33](https://github.com/metalayerlab/interpose/issues/33) Aliases.** A renamed setting keeps
  working under its old name, with a warning.
- **[#34](https://github.com/metalayerlab/interpose/issues/34) Unknown keys.** A misspelt or misplaced
  key is ignored today ([0001](0001-initial-design.md): "Unknown YAML keys are ignored"), so the setting
  silently keeps its default. qbt-rules warns, suggesting the closest key.
- **[#35](https://github.com/metalayerlab/interpose/issues/35) Lists of mappings and sections.**
  qbt-rules' `schedule` is a list of `{cron, context}` items, overridable by indexed variables
  `QBT_RULES_SCHEDULE_<N>_CRON` / `_CONTEXT` (each with its `_FILE` form), numbered from 0; the first
  missing `_CRON` ends the list. mitm-proxy's capture design (its 0012, Approved and merged) needs the
  same: `reverse`, a list of `{listen, upstream}`, and `middleware.config`, a mapping of free-form
  per-middleware options; its open question 1 was decided as a declared **section** setting here, in its
  own interpose record (this one). sluice will want per-middleware options too. Today a value is a
  scalar, a list of scalars or a `Secret`, and nested objects are a non-goal (the wiki's Introduction).
- **[#36](https://github.com/metalayerlab/interpose/issues/36) Provenance.** `resolve()` reads only a
  file path and an environment. A consumer that pre-processes its configuration can only hand results
  back as environment variables, so an error says `(from QBT_RULES_X)` rather than the file and key.
  mitm-proxy's managed mode (its 0002) has the same problem from the other side: managed values come
  from a control-plane payload, not a file.

The decisions below were approved by the brain coordinator for Stephen on 2026-11-03 (the shape of
each; the details are this record's). The brain coordinator approved this record for Stephen on
2026-11-04: decisions A to D and the recommended answers as written, with one condition, now decision E.

Constraints, all from [0001](0001-initial-design.md) and [0007](0007-check-replacements-and-typed-accessors.md):

- **Compatible by default.** A project that changes nothing sees the same values and sources, and the
  same errors except that an error about a file value names the file's path instead of `file`
  ([decision D](#authors-decisions)). An unknown key or prefixed variable now gives a warning (#34),
  which the project sees only if it reads `Resolved.warnings`. Every other new behaviour is declared by
  the project.
- **The lookup order holds:** `_FILE` > variable > file > default, per setting.
- **Values are never logged**, and a secret setting's value is never in a message. A warning names keys
  and variables, never values.
- **No I/O at import.** **Dependencies:** stdlib and pyyaml (`difflib` for suggestions).
- **No logging inside interpose.** It has none today; warnings are returned for the project to log.

## Decision

Five features, built in six PRs (see [Build order](#build-order)). The common pieces come first.

### Author's decisions

Four choices the issues left open, made by this record and flagged for the reviewer, and a fifth (E),
the condition of its approval. Each is detailed in the section named.

- **A. `${VAR}` expansion is opt-in per project.** `Settings(..., expand=True)`; off by default, so no
  existing `${` in a file changes meaning ([section 4](#4-var-expansion-32)).
- **B. Warnings are returned on `Resolved.warnings`.** interpose never logs, prints or raises them, and
  has no strict switch; the project logs them, or raises if it wants strictness
  ([Warnings on `Resolved`](#warnings-on-resolved-shared-by-33-34-and-35)).
- **C. Control-plane values never expand.** A `Layer` expands only with `expand=True`, which defaults to
  false; `read_file()` is the only place interpose sets it, for the local file. A project must not set it
  on a layer built from a remote payload: expansion there would let the control plane read the
  instance's environment. interpose can't tell a remote layer from a local one, so the rule is the
  project's to keep, and the Security wiki page says so ([section 1](#1-values-with-provenance-36)).
- **D. Changing file error messages is a breaking release.** An error about a file value names the path
  instead of `(from file)`. Code that matched the old text breaks, so the release that ships step 1 is
  marked breaking, though no value or source changes ([Consequences](#consequences)).
- **E. Control-plane values never read secret files.** Like C, a rule for remote layers: a
  `{secret_file: ...}` leaf is honoured only in a layer with `secret_files=True`, which only
  `read_file()` and a section's `_FILE` set. In any other layer, a remote payload above all, it is a
  `ConfigError` naming the section path and saying that secret files are read only from local
  configuration. It is never read, and never kept as a plain mapping. Otherwise a remote config could
  make the process read any local file it can see ([section 5](#5-sections-and-lists-of-sections-35)).

### Warnings on `Resolved` (shared by #33, #34 and #35)

```python
@dataclass(frozen=True)
class ConfigWarning:
    kind: Literal["unknown", "alias", "ignored"]
    name: str  # the key or variable it is about: "listen.prot", "SLUICE_LISTEN_PROT"
    message: str  # one line, no values; str() gives it

    def __str__(self) -> str: ...


@dataclass(frozen=True)
class Resolved:
    values: Mapping[str, Value]
    sources: Mapping[str, str]
    warnings: tuple[ConfigWarning, ...] = ()  # new; in the order found
```

- `resolve()` collects warnings and never prints them. The project logs each at WARNING (its CLI's
  `config` command prints them), on startup and on every reload.
- A project that wants strictness raises when `warnings` isn't empty; interpose has no strict switch
  (see [Alternatives](#alternatives-considered)).

### 1. Values with provenance (#36)

```python
@dataclass(frozen=True)
class Layer:
    data: Mapping[str, Any]  # a YAML-shaped tree: what yaml.safe_load gives
    source: str  # what `sources` says for a value from here: "file", "control plane"
    where: str  # for messages: "/etc/qbt-rules/config.yaml", "control plane (config v12)"
    origins: Mapping[str, str] = field(default_factory=dict)  # dotted key -> a finer "where"
    expand: bool = False  # expand ${VAR} in its strings (section 4)
    secret_files: bool = False  # allow {secret_file: ...} leaves; else an error (decision E)


class Settings:
    def read_file(
        self, path: Path | None = None, environ: Mapping[str, str] | None = None
    ) -> Layer:
        """The config file as a Layer (source "file", where = the path, expand = self.expand,
        secret_files = True).
        Path resolution, the missing-file and parse rules are resolve()'s."""

    def resolve(
        self,
        path: Path | None = None,
        environ: Mapping[str, str] | None = None,
        *,
        layers: Sequence[Layer] | None = None,
    ) -> Resolved: ...
```

- **`layers` replaces the file.** With `layers`, `resolve()` reads no file (`path` with `layers` is a
  `ValueError`). The order becomes `_FILE` > variable > `layers[0]` > `layers[1]` > ... > default. With
  no `layers`, `resolve(path, environ)` is `resolve(environ=environ, layers=[self.read_file(path,
  environ)])`, exactly today's behaviour.
- **Provenance in messages.** A coercion or check error for a value from a layer says where it came
  from: `listen.port: expected int, got 'x' (from /etc/sluice/config.yaml)`, using `origins[key]` when
  present, else `where`. Today a coercion error for a file value says `(from file)` and a check error
  names no source; both now name the path (decision D). `sources` keeps its values (`"file"` for the
  file layer), so code comparing against them is unaffected.
- **A consumer that pre-processes** (qbt-rules today) builds its own `Layer` with `origins`, instead of
  writing environment variables. With sections 2 to 5 built, qbt-rules needs none of its own
  pre-processing, so this is mostly for managed configuration and tests.
- **Managed settings.** mitm-proxy's managed mode passes `layers=[Layer(payload, source="control plane",
  where="control plane (config v12)"), settings.read_file()]`: the payload for its managed keys, then
  its local file, since `layers` replaces the file. Its rule that a managed key set locally is
  a `ConfigError` stays in mitm-proxy, which reads `sources`. A layer from a remote payload keeps
  `expand=False` (decision C): expanding `${VAR}` in it would let the control plane read the instance's
  environment. It also keeps `secret_files=False` (decision E): a `{secret_file: ...}` leaf in it is a
  `ConfigError`, never read.
- `data` is read, never kept or changed; `Resolved` holds coerced copies.

### 2. Unknown keys warn, with a suggestion (#34)

This **amends 0001's "Unknown YAML keys are ignored"**: they are still ignored for resolution, and now
each also gives an `unknown` warning.

- **Which keys.** After resolving, interpose walks every layer's tree. A dotted path is known if it is a
  declared key or alias (section 3), or a parent of one (`listen` for `listen.port`). Below a free-form
  section setting (section 5) nothing is checked: its content is the project's to validate. A typed list
  section's items are checked against its declared fields (section 5).
  Each unknown path is reported once, at its first unknown level (`listne`, not each key below it).
- **Which variables.** A variable that starts with the prefix and that no setting reads (its variable,
  `_FILE`, an alias's, a list section's indexed variables, `<PREFIX>CONFIG`) gives an `unknown` warning.
  A project that reads other prefixed variables itself names them: `Settings(..., extra_env=("SLUICE_X",
  ...))`. Only names are checked, never values.
- **Did you mean.** The closest declared key (or variable name), from `difflib.get_close_matches` with a
  cutoff of 0.75, considering current names only, never aliases:
  `unknown key 'listen.prot' in /etc/sluice/config.yaml: did you mean 'listen.port'?`. No close match:
  no suggestion.
- A non-string YAML key (a typo like `on:`, loaded as a bool) is reported by its YAML text.
- **Why a warning, not an error.** A config file outlives versions: an operator rolling back keeps a file
  with a newer key, and a shared file can carry keys for another tool. A warning catches the typo
  without breaking either. mitm-proxy's capture config (its 0012) treats an unknown key as a startup
  error; that is inside its own free-form section, which it validates itself, so the two don't conflict.

### 3. Aliases for renamed settings (#33)

```python
Setting("listen.port", 8080, "...", aliases=("port",))
```

- **`aliases`** (a tuple of dotted keys, default empty) are old names. Each is read everywhere its
  current name is: the YAML key, `<PREFIX><ALIAS>` and `<PREFIX><ALIAS>_FILE` (and, for a list section,
  its indexed variables). `Settings` rejects an alias that is malformed, repeats, equals any key, or
  reads a variable another setting or alias reads, as for keys today.
- **Lookup.** At each level of the order, the current name and the aliases are looked up together. If
  exactly one is set, it is used. If more than one is set at the same level, their coerced values are
  compared: equal is fine, different is `ConfigError("listen.port: set as both 'listen.port' and its old
  name 'port' (from <where>), to different values")`, never with the values. A higher level still
  beats a lower one whatever the name (an alias variable beats the current name in the file).
- **Warning.** Every alias that is set gives an `alias` warning, used or shadowed:
  `'port' (in /etc/sluice/config.yaml) is the old name of 'listen.port': rename it`.
- **`sources`** gives the name actually read: the alias's variable (`SLUICE_PORT`), or the layer's
  source (`"file"`) with the alias named in the warning.
- **Horizon.** interpose doesn't expire aliases. A project removes an alias in its next major release,
  and its changelog says so.
- Aliases work for every kind of setting, sections included. `Setting.aliases` is public, so a
  project's `config` command and wiki table can show "formerly `port`".

### 4. `${VAR}` expansion (#32)

```python
Settings("QBT_RULES_", (...), default_path=..., expand=True, expand_unset="empty")  # qbt-rules 0.x
```

- **Opt-in per project** (decision A): `expand: bool = False`. With it, the config file's layer (and
  a section's `_FILE`, section 5) expands; a `Layer` a project builds expands only with `expand=True`.
  Variables and scalar `_FILE` contents never expand: the shell already did, and a secret file holding
  `${` must arrive as written.
- **Syntax:** `${NAME}` and `${NAME:-default}`, `NAME` being `[A-Za-z_][A-Za-z0-9_]*`. `:-` takes the
  default when the variable is unset or empty, as in the shell ([question 1](#open-questions)). The
  default is literal text without `}` (no nested expansion). `$${` gives a literal `${`. Any other `${`
  (unclosed, a bad name) is a `ConfigError` naming the key: write `$${` for a literal. A `$` not
  followed by `{` is literal.
- **Unset, no default: an error by default** (`expand_unset="error"`). `ConfigError("url: ${HOST} is
  not set and has no default (from /etc/qbt-rules/config.yaml)")`. `expand_unset="empty"` opts in to
  expanding it to the empty string, qbt-rules' current behaviour, for its compatibility.
- **What expands:** every string scalar in the subtree of a setting that is read, including inside
  longer strings, list items and a section's leaves (keys never). Non-strings are untouched. Unknown keys
  aren't expanded, so a stray `${X}` in an ignored key can't fail a load. Typing comes after, so
  `${PORT:-5000}` resolves to the int 5000.
- **The environment** is `resolve()`'s `environ`, so tests stay hermetic. It is read on every resolve.
- **Secrets.** A secret setting may be written `${API_TOKEN}`; the variable is then the secret's source.
  Every expansion error names the variable, never its value. A coercion or check error on an expanded
  value shows the text as written (`got 'https://${HOST}:x'`), not the expanded one, since a non-secret
  value can embed a secret variable (`https://user:${PASS}@host`); for a secret setting, nothing.
- **Provenance:** `sources` stays the layer's (`"file"`); expansion is an implementation of the file.
- **Differences from qbt-rules,** all deliberate: unset is an error unless `expand_unset="empty"`; a
  variable set to the empty string gives the `:-` default (qbt-rules gives the empty string, and
  `expand_unset="empty"` doesn't change that: [question 1](#open-questions)); `$${` is an escape
  (qbt-rules would give `$` and the value); names are environment-variable names (qbt-rules takes
  anything but `}` and `:`, so a `${vars.x}` that qbt-rules expands to empty is a `ConfigError` here).

### 5. Sections and lists of sections (#35)

A section setting's value is a deep-frozen YAML subtree. It is declared like a list setting, with the
same default convention:

```python
Setting("middleware.config", "", "Per-middleware options, by middleware name.", is_section=True)
Setting("reverse", "", "Reverse-proxy listeners.", is_section=True, is_list=True)
```

- **`is_section=True`**: the value is a mapping. With `is_list=True` too, a list of mappings. The
  default must be `""` (an empty mapping, or an empty list); a section can't be `secret=True` (but see
  secret leaves below).
- **The value** is a `Section`: a read-only `Mapping[str, Node]`, nested mappings `Section` too, lists
  tuples, scalars as YAML loaded them (str, int, float, bool, date, `None`). A `Section` is hashable and
  equal to another with the same content, so a reload can compare old and new. A list section is a
  `tuple[Section, ...]`. `Value` gains both.
- **Accessors.** `Resolved.section(key)` gives a section setting's `Section` and `Resolved.sections(key)`
  a list section's tuple, raising `TypeError` naming the key for another kind, as 0007's typed accessors
  do (and they, in turn, refuse a section).
- **Sources of a value**, in the usual order:
  - `<PREFIX><KEY>_FILE` names a **YAML file** whose top level is the section (a mapping, or a list for
    a list section): for a Kubernetes ConfigMap or Secret mount. It expands when `expand` is on.
  - A plain variable is **not** read for a section (a mapping in a variable is unreadable); setting one is
    a `ConfigError` saying to use `_FILE`. The exception is a list section with declared fields (below).
  - The config file's subtree at the key. A section key may not be the parent of another setting's key.
  - Otherwise the empty default.
- **Free-form by default.** interpose checks only the shape: a mapping, a list of mappings, string keys
  (a non-string key is a `ConfigError` naming the path). The project validates the content in its
  `check` (which receives the `Section`, may return a replacement `Section`, and may raise as usual) or in
  `load_config`. This is all qbt-rules needs for `schedule`, and all `middleware.config` needs.
- **Secret leaves.** Inside a section, a mapping that is exactly `{secret_file: <path>}` is replaced, at
  resolve, by a `Secret` holding that file's contents, trailing newline stripped as for `_FILE`. An
  unreadable file is a `ConfigError` naming the section path and the file, never the contents. This
  is mitm-proxy 0012's rule, made generic: no secret is written inline in a section. Secret leaves are
  read in every section, not declared per setting ([question 3](#open-questions)), but only from a layer
  with `secret_files=True`; anywhere else one is a `ConfigError`, not silently ignored and never read
  ([decision E](#authors-decisions)). A `Section`'s `repr()` masks every `Secret` (it already does,
  `Secret` being masked).
- **Typed items (optional, per setting).** A list section may declare its item fields:

  ```python
  Setting(
      "schedule",
      "",
      "Cron schedule.",
      is_section=True,
      is_list=True,
      fields=(
          Setting("cron", "", "A 5-field cron expression.", required=True),
          Setting("context", "", "The context to enqueue.", required=True),
      ),
  )
  ```

  - A field is a `Setting` with a one-part key; its default sets its type, coercion and `check` are the
    usual ones, and `required=True` (allowed only on a field) makes a missing field an error. A field
    may be `secret=True` or a free-form section. An item resolves to a `Section` holding every field.
  - An unknown field in an item gives an `unknown` warning with a suggestion (section 2's rule, and
    [question 2](#open-questions)); errors name the item: `schedule[2].cron: ... (from
    /etc/qbt-rules/config.yaml)`.
  - **Indexed variables.** Only with fields: `<PREFIX><KEY>_<N>_<FIELD>` and its `_FILE`, for `N` from
    0. Item `N` exists when any of its field variables is set; the first `N` with none ends the list. A
    variable past the end (`_3_CRON` with no `_2_*`) gives an `ignored` warning. A required field missing
    from an indexed item is a `ConfigError` naming the variable (`QBT_RULES_SCHEDULE_1_CONTEXT is not
    set, but QBT_RULES_SCHEDULE_1_CRON is`). When any indexed variable is set, the list **replaces** the
    file's, never merges. `sources` gives `<PREFIX><KEY>_<N>_*` (`QBT_RULES_SCHEDULE_<N>_*`); messages
    name the exact variable. `Settings` rejects any other setting or alias whose variable fits a
    declared list section's indexed pattern (`QBT_RULES_SCHEDULE_0_CRON` can't also be a setting's).
  - qbt-rules' behaviour carries over, with two differences. A straggler variable now warns instead of
    being silently ignored. And an item ends the list only when none of its field variables is set:
    qbt-rules ends it at the first missing `_CRON`, so a lone `_1_CONTEXT` was ignored there and is a
    `ConfigError` here (interpose can't know which field marks an item).
- **Managed settings.** A section is managed or bootstrap like any setting; in a control-plane `Layer`,
  the payload's subtree is the value. A `{secret_file: ...}` leaf in a remote payload would make the
  instance read a local file the control plane names, so secret leaves are only read from local layers
  (the file and `_FILE`); a `Layer` must say `secret_files=True` to allow them (default false). In a
  layer without it, such a leaf is a `ConfigError` naming the section path and saying that secret files
  are read only from local configuration; the file is never opened ([decision E](#authors-decisions)).
- **Reload.** Resolution is the same on every call; a reload gets fresh `Section`s and secret-leaf
  contents, and compares by equality.
- **The wiki table.** The consumers' `scripts/gen-wiki.py` (template-owned, from template-python;
  interpose's own copy generates no settings table) reads `key`, `default`, `help`, `secret`, `is_list`
  and `env`, which keep their meaning. Today it describes every `is_list` setting as "A list: YAML, or
  comma-separated", which is wrong for a list section, so template-python's generator gains a section
  row: its type as "section" or "list of sections", and its variable as `_FILE` only (or the indexed
  form). That is a template-python change, asked for there when step 5 ships.

### Interface summary

| Name | Change |
|---|---|
| `Setting` | `aliases`, `is_section`, `fields`, `required` (fields only) |
| `Settings(...)` | `expand`, `expand_unset`, `extra_env`; `read_file()` |
| `Settings.resolve` | keyword `layers` |
| `Layer`, `ConfigWarning`, `Section` | new |
| `Resolved` | `warnings`; accessors `section(key)` and `sections(key)` |
| `Value` | adds `Section` and `tuple[Section, ...]` |
| Messages | file errors name the path instead of `file` |

### Build order

One PR per step, each with its tests and wiki changes, each released on its own:

1. **Values with provenance (#36):** `Layer`, `read_file`, `resolve(layers=)`, messages naming the
   path. First, because the others report through it.
2. **Unknown keys (#34):** `ConfigWarning`, `Resolved.warnings`, `extra_env`, the suggestions; the
   note in 0001 that this record adds updated from Approved to Implemented.
3. **Aliases (#33):** uses 2's warnings and 2's "known name" set.
4. **Expansion (#32):** `expand`, `expand_unset`, the escape.
5. **Free-form sections (#35):** `is_section`, `Section`, section `_FILE`, secret leaves, `section()`
   accessors. Its tests include decision E's: a `{secret_file: ...}` leaf in a layer without
   `secret_files` (a control-plane layer) raises `ConfigError` with that reason, and the file is not
   opened. mitm-proxy's capture step (a) can pin this release.
6. **Typed items and indexed variables (#35):** `fields`, `required`, the indexed form. The last
   qbt-rules needs to drop its own loader.

## Alternatives considered

- **Unknown keys as errors** (or a strict switch in interpose). Breaks rollbacks and shared files, see
  section 2. A project that wants errors raises on `Resolved.warnings`; a switch can be added later
  without breaking anyone.
- **Expansion on by default.** Changes the meaning of every existing `${` in a file. Opt-in costs one
  argument; turning it on by default can be reconsidered at 1.0.
- **Unset `${VAR}` expanding to empty by default** (qbt-rules' behaviour). A typo in a variable name then
  configures an empty value silently, the class of bug #34 fixes for keys. Kept as an opt-in.
- **Expanding variables and `_FILE` contents too.** The shell already expands variables, and a secret
  file must arrive as written.
- **A pluggable loader hook** (#36's other option). Every consumer would write its own provenance and
  error wording; a `Layer` is data, testable without code.
- **Sections from a plain variable as JSON or YAML.** Unreadable and easy to break in quoting; `_FILE`
  covers the mount case. Indexed variables cover qbt-rules' existing deployments.
- **Typed sections everywhere, no free-form.** qbt-rules validates its items itself, and
  `middleware.config` is per middleware by nature: a schema in interpose would duplicate each one.
- **Merging indexed variables into the file's list.** Which item wins is unclear; replacing is what every
  other setting's variable does.
- **Aliases in the project's `load_config`.** That is what qbt-rules does today; every consumer would
  repeat it, and the unknown-key check would warn about the old names.

TODO.md's "Decided against" gains the first, third and sixth.

## Consequences

- qbt-rules can drop its expansion, alias, unknown-key and schedule code and resolve once, with errors
  that name the file. Its `expand_unset="empty"` keeps 0.10.x configurations working; moving to the
  strict default is its own decision for a major release.
- mitm-proxy declares `reverse` (typed items) and `middleware.config` (free-form) and validates them
  in `config.py`; its managed mode can pass the control-plane payload as a `Layer`. sluice can do the
  same for per-middleware options.
- Every consumer sees unknown-key warnings on upgrade, once it logs `Resolved.warnings`; a typo found
  this way was silently ignored before. Until it logs them, nothing changes.
- **Breaking, in small ways:** file errors name the path instead of `file`; `Value` gains two types, so
  an exhaustive `match` on it needs two more cases. The release that ships step 1 is marked breaking
  (decision D), and so is the one that ships step 5.
- qbt-rules: a variable set to the empty string now gives a `${VAR:-default}` its default (question 1),
  a lone `SCHEDULE_<N>_CONTEXT` is now an error, and a `${...}` whose name isn't an environment
  variable name is now an error. Its changelog says so when it adopts steps 4 and 6.
- 0001's "Unknown YAML keys are ignored" is amended by this record (a note there points here); 0001
  itself isn't rewritten.
- Wiki: Introduction (sections leave the non-goals; nested objects are now a section), API-Reference
  (every new name), Architecture-Internals (layers, the order with aliases, expansion, unknown keys),
  Security (expansion and secrets, secret leaves, remote layers, and decision E: a secret leaf in a
  control-plane layer is an error and is never read), Quick-Start (a section and a warning),
  Troubleshooting (the "ignored key" entry becomes the warning), Testing-Guide.

## Open questions

Answered by the brain coordinator for Stephen on 2026-11-04, each with the recommended answer.

1. **Should `${VAR:-default}` use the default when `VAR` is set but empty?** Recommend yes, as the
   POSIX shell does: an empty variable is nearly always a mistake or a placeholder, and the shell rule is
   what operators expect from the syntax. qbt-rules does the opposite today: its source (v0.11.1,
   `_expand`) takes `env.get(name, default or "")`, so a variable set to `""` wins over the default.
   Its example config mentions the syntax without using `:-`, and its own deployments' files weren't
   reachable from here, so whether any rely on the empty value is unknown. If one does, it breaks on
   adopting step 4, and `expand_unset="empty"` doesn't cover it; qbt-rules checks before it adopts.
   **Answer: yes,** the default when set but empty, as the shell does.
2. **Should an unknown field inside a typed item warn, or be an error?** Recommend warn, the same rule
   as section 2. mitm-proxy 0012 wants errors for its capture options, which are free-form and
   validated by mitm-proxy; for `reverse`'s typed items it can raise on the warning. **Answer: warn.**
3. **Should secret leaves be on in every section, or declared per setting?** Recommend on in every
   section: sections are new, so no file changes meaning, and one rule is easier to document. The cost
   is that `{secret_file: ...}` alone in a mapping is reserved inside sections. **Answer: on in every
   section,** read from local layers only ([decision E](#authors-decisions)).
