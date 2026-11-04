# Testing Guide

## Running the tests

```sh
uv run pytest                                   # unit tests, with an 80% coverage floor
uv run pytest tests/unit/test_settings.py -k env  # a subset
RUN_INTEGRATION=1 uv run pytest -m integration  # integration tests (real external systems)
```

CI runs the unit tests on every supported Python version, together with ruff, mypy (strict), the wiki checks and the manifest checks. All of them block a merge.

## Test layout

| Path | What goes there |
|---|---|
| `tests/unit/` | Fast and offline: no network, no real external systems. Most tests. |
| `tests/integration/` | Real dependencies, marked `@pytest.mark.integration`; skipped unless `RUN_INTEGRATION=1` |

| `tests/unit/test_settings.py` | Declarations, the lookup order and sources, coercion of each type from YAML, the environment and `_FILE`, the checks and their replacements (`one_of`'s declared spelling), secret settings, the typed accessors, layers, unknown keys, aliases, expansion, sections (`Section`'s freezing, equality and masking, `_FILE`, shapes, accessors, checks) and secret leaves, including decision E: a leaf in a control-plane layer is refused and its file never opened |
| `tests/unit/test_secret.py` | `Secret` masking (str, repr, f-strings, logging, exceptions) and `read_secret_dir` |
| `tests/unit/test_pipeline.py` | The guard (timing, timeouts and late results, failures, answers, `valid`), each phase, every `Policy` field (`skip_inherited` with instance hooks set before and after the build, and state-based stops), the cached `active`, and both consumers' semantics rebuilt on it (mitm-proxy fail-open, sluice onion and fail-closed) |
| `tests/unit/test_metrics.py` | Label sets enforced on every call, declaration checks, the text format, and parity with mitm-proxy's `/metrics` lines |

There are no fixtures beyond pytest's own: tests write YAML and secret files under `tmp_path` and pass `environ` as a plain dict, so the real environment never leaks in.

## Writing tests

- Every behaviour change needs a test that fails without it. A bug fix starts with the failing test.
- Assert outcomes (outputs, side effects, errors), not internal calls.
- Use placeholders for anything that looks real: `example.org`, `192.0.2.0/24`, fake tokens.
- Don't lower the coverage floor or skip a test to make CI pass.

- **Pass `environ`, don't patch `os.environ`.** `Settings.resolve(path, environ)` takes both, so a test controls every input. Use `monkeypatch.setenv` only to test the `os.environ` default itself.
- **Test each coercion case from both YAML and the environment.** YAML has already typed the value (`0.5` is a float, `yes` a bool), the environment hasn't; that difference is where the copies drifted.
- **Secrets:** assert the value is absent from `str()`, `repr()`, the exception message and `caplog.text`, not only that the mask is present.
- **Async tests** use `asyncio.run` directly: there is no pytest-asyncio dependency.
- **Coverage:** interpose is every consumer's settings engine, so keep it at 100% branch coverage even though the floor is 80%.
- **No timing assertions.** Performance is measured with `uv run benchmarks/bench-pipeline.py` (stdlib only; numbers on [Architecture Internals](Architecture-Internals)), never asserted in a test: timings vary by machine and runner.
- If `/tmp` is short of space, run with `--basetemp` under the repository (a git-ignored path).
