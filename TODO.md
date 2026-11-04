# TODO

Short-lived working list. Bugs and features with discussion belong in GitHub issues; this file is for what
the team (and its AI assistants) should know at a glance.

## Open

- [ ] Consumers hear about new tags: Dependabot's `uv` ecosystem can't update a tag-pinned `git+https`
  dependency (design 0001, open question 4). Until [dependabot-core#16151](https://github.com/dependabot/dependabot-core/pull/16151)
  lands, each consumer needs a scheduled workflow that opens a PR when interpose has a newer tag; it belongs
  in the template that generates consumers (template-python), not here. Revisit when #16151 merges: it
  covers `[tool.uv.sources]` `tag =` only.

## Decided against (don't re-propose without reading this first)

- pydantic-settings or dynaconf: heavy, their own precedence and `_FILE` rules, no per-setting `help` for the generated wiki (design 0001).
- Publishing to PyPI: consumers are our own repos and pin a GitHub tag (design 0001).
- A template-owned engine file instead of a library: covers settings only; `Secret`, metrics and the pipeline don't belong in every project (design 0001).
- A float setting type: no setting needs one; time settings are integer `_ms` (design 0001).
- `prometheus_client`: a dependency for a small format, its default collectors add series, and its label values are unbounded (design 0002).
- Folding undeclared label values into an `other` series: hides the bug; an undeclared value is a `ValueError` (design 0002).
- Defaults for the pipeline's semantics (`on_error`, `after_order`, `after_local`, `observe_failed`): a default hands one project's semantics to the next (design 0003).
- An interpose `Middleware` base class with hook methods: the consumers' hook vocabularies differ; the core takes `invoke` callables (design 0003).
- Mapping hook failures to a deny inside interpose: a 502 or an errno is transport; `HookFailed` carries what a mapper needs (design 0003).
- Retrying a failed hook, or killing the unit: undefined after side effects (mitm-proxy 0001); `"fail"` is a deny answer (design 0003).
- Built-in middleware (a `Snapshot`/TTL cache, ...) or preset policies: mechanism only, as sluice's 0002 rules (design 0003).
- Unknown keys as errors (or a strict switch in interpose): breaks rollbacks and shared config files; they warn on `Resolved.warnings`, and a project that wants errors raises on them (design 0009).
