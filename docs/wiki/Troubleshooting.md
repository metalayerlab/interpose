# Troubleshooting

## Diagnostics

interpose logs nothing: values are never logged, and it has no logger to turn up. To see what was resolved, print `Resolved.sources` (safe: it's where each value came from), or the consumer's `config` command, which prints each key, its value (secrets masked) and its source.

Every error is a `ConfigError` with one line that names the key, the expected type and where the value came from: the config file's path (or, for a project that passes layers, the layer's description), `default` or a variable name.

## Common problems

### "expected int, got 0.5 (from /etc/myapp/config.yaml)"

The YAML file gives a float, or a bool or string, for an int setting. interpose refuses rather than truncating. Write a whole number. `"1_000"` from the environment is refused too; write `1000`.

### "expected bool, got 'enabled'"

A bool takes `1/true/yes/on` or `0/false/no/off`, in any case.

### "expected str, got ['a', 'b']"

A YAML list or mapping for a str setting. If the setting should hold several values, the project declares it with `is_list=True`.

### "expected list, got 5 (from /etc/myapp/config.yaml)"

A list setting takes a YAML list, or a string of comma-separated items. Write `[5]` or `"5"`.

### "cannot read MYAPP_X_FILE (/run/secrets/x): No such file or directory"

The `_FILE` variable is set but its file is missing or unreadable. A set `_FILE` variable always wins, so fix the path or unset the variable. Check the mount and the file's mode for the user the process runs as.

### "cannot parse /etc/myapp/config.yaml at line 3, column 9: ..."

The YAML is invalid there. The message gives no text from the file, because the line might hold a secret.

### "the top level must be a mapping"

The config file is a list or a single value. Its top level must be `key: value` pairs.

### A setting in the config file is ignored

Something earlier in the lookup order is set: a `_FILE` or environment variable for the same key. The source says which.

A key spelled wrongly, or nested under the wrong parent, is ignored too, and gives a warning on `Resolved.warnings`, such as `unknown key 'listen.prot' in /etc/myapp/config.yaml: did you mean 'listen.port'?`. A misspelt prefixed variable does the same: `unknown variable 'MYAPP_LISTEN_PROT' in the environment: ...`. interpose doesn't log warnings itself; if you see none, check that the project logs them.

### "unknown variable 'MYAPP_...' in the environment" for a variable the project reads

The project reads that prefixed variable itself, outside its settings. Name it in `Settings(..., extra_env=("MYAPP_...",))`.

### "'port' (in /etc/myapp/config.yaml) is the old name of 'listen.port': rename it"

The setting was renamed, and the configuration still uses its old name. The old name still works, but a later major release of the project may drop it: rename the key in the file, or the variable (`'MYAPP_PORT' (in the environment) is the old name of 'MYAPP_LISTEN_PORT'`). The warning comes whether the old name's value was used or shadowed by a higher level of the lookup order.

### "listen.port: set as both 'listen.port' and its old name 'port' (from ...), to different values"

The same setting is given under its current name and its old name at the same level (both in one file, or both as variables), with different values, so neither can be trusted to win. Remove the old name. Equal values are accepted, with the warning above.

### "upstream.url: ${HOST} is not set and has no default (from /etc/myapp/config.yaml)"

The project expands `${VAR}` in its config file, and the variable isn't in the environment. Set it, or give a default: `${HOST:-localhost}`, which is also used when the variable is set but empty. If the project passes `expand_unset="empty"`, an unset variable gives the empty string instead.

### "a ${ at character 9 that isn't ${NAME} or ${NAME:-default} ...: write $${ for a literal ${"

The project expands `${VAR}` in its config file, and a value has a `${` that isn't a variable: unclosed, empty, or a name with characters other than letters, digits and `_` (`${vars.x}`). Write `$${` where you mean a literal `${`. A `$` not followed by `{` needs no escape.

### A setting shows `${VAR}` instead of its value

The project doesn't expand variables in its config file (`Settings(expand=...)` is off by default), or the value came from an environment variable, a `_FILE` or a project-built layer, which never expand.

### "middleware.config: MYAPP_MIDDLEWARE_CONFIG is set, but a section is read from a YAML file, never a variable: ..."

The setting is a section (a YAML subtree), which can't be written in one environment variable. Put it in the config file, or in a YAML file named by `MYAPP_MIDDLEWARE_CONFIG_FILE`, and unset the plain variable: it is an error even beside the `_FILE`.

### "middleware.config.auth: secret files are read only from local configuration (the config file or a section's _FILE), not from control plane ..."

A section from a remote layer, such as a control plane's managed configuration, holds a `{secret_file: ...}` leaf. interpose never reads a file a remote payload names. Move the secret leaf to the local config file or the section's `_FILE`, or have the control plane send something the project resolves itself.

### "middleware.config.auth: cannot read secret_file /run/secrets/x: No such file or directory"

A secret leaf names a file that is missing or unreadable by the process's user. A relative path is relative to the working directory. The message never shows the file's contents.

### "middleware.config.true: a section's keys are strings, and this one is bool: quote it"

YAML reads `on`, `no`, `null`, `1` and the like as a bool, `None` or a number, and a section's keys must be strings. Quote the key: `"on": ...`.

### A secret shows as `********` where its value is needed

The code called `str()` on the `Secret`. Call `reveal()` at the point of use.

### "cannot read secret 'token' in /run/secrets: Permission denied"

`read_secret_dir` found a file it can't read. Every regular file in the directory must be readable; dot-entries are skipped.

### A hook "timed out" although it returned a result

`Failure.late` says why: `"overran"` means the deadline passed while the hook ran synchronous code (a blocking call, CPU work), so the timeout couldn't cancel it; `"swallowed"` means the hook was cancelled and caught `CancelledError` (or `BaseException`) and returned anyway. Either way the result came after the limit and was discarded. Move blocking work off the event loop (`asyncio.to_thread`, and note that the thread keeps running after a timeout), and let `CancelledError` propagate: do cleanup in `try/finally` or `async with`.
