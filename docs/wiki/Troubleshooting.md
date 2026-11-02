# Troubleshooting

## Diagnostics

interpose logs nothing: values are never logged, and it has no logger to turn up. To see what was resolved, print `Resolved.sources` (safe: it's where each value came from), or the consumer's `config` command, which prints each key, its value (secrets masked) and its source.

Every error is a `ConfigError` with one line that names the key, the expected type and where the value came from: `file`, `default` or a variable name.

## Common problems

### "expected int, got 0.5 (from file)"

The YAML file gives a float, or a bool or string, for an int setting. interpose refuses rather than truncating. Write a whole number. `"1_000"` from the environment is refused too; write `1000`.

### "expected bool, got 'enabled'"

A bool takes `1/true/yes/on` or `0/false/no/off`, in any case.

### "expected str, got ['a', 'b']"

A YAML list or mapping for a str setting. If the setting should hold several values, the project declares it with `is_list=True`.

### "expected list, got 5 (from file)"

A list setting takes a YAML list, or a string of comma-separated items. Write `[5]` or `"5"`.

### "cannot read MYAPP_X_FILE (/run/secrets/x): No such file or directory"

The `_FILE` variable is set but its file is missing or unreadable. A set `_FILE` variable always wins, so fix the path or unset the variable. Check the mount and the file's mode for the user the process runs as.

### "cannot parse /etc/myapp/config.yaml at line 3, column 9: ..."

The YAML is invalid there. The message gives no text from the file, because the line might hold a secret.

### "the top level must be a mapping"

The config file is a list or a single value. Its top level must be `key: value` pairs.

### A setting in the config file is ignored

Something earlier in the lookup order is set: a `_FILE` or environment variable for the same key. The source says which. A key spelled wrongly, or nested under the wrong parent, is ignored too.

### A secret shows as `********` where its value is needed

The code called `str()` on the `Secret`. Call `reveal()` at the point of use.

### "cannot read secret 'token' in /run/secrets: Permission denied"

`read_secret_dir` found a file it can't read. Every regular file in the directory must be readable; dot-entries are skipped.

### A hook "timed out" although it returned a result

`Failure.late` says why: `"overran"` means the deadline passed while the hook ran synchronous code (a blocking call, CPU work), so the timeout couldn't cancel it; `"swallowed"` means the hook was cancelled and caught `CancelledError` (or `BaseException`) and returned anyway. Either way the result came after the limit and was discarded. Move blocking work off the event loop (`asyncio.to_thread`, and note that the thread keeps running after a timeout), and let `CancelledError` propagate: do cleanup in `try/finally` or `async with`.
