# TODO

Short-lived working list. Bugs and features with discussion belong in GitHub issues; this file is for what
the team (and its AI assistants) should know at a glance.

## Open

- [ ] Consumers hear about new tags: Dependabot's `uv` ecosystem can't update a tag-pinned `git+https`
  dependency (design 0001, open question 4). Until [dependabot-core#16151](https://github.com/dependabot/dependabot-core/pull/16151)
  lands, each consumer needs a scheduled workflow that opens a PR when interpose has a newer tag; it belongs
  in the template that generates consumers (template-python), not here. Revisit when #16151 merges: it
  covers `[tool.uv.sources]` `tag =` only.
- [ ] Design 0002 (metrics) and 0003 (pipeline core), after the consumers switch to v0.1.

## Decided against (don't re-propose without reading this first)

- pydantic-settings or dynaconf: heavy, their own precedence and `_FILE` rules, no per-setting `help` for the generated wiki (design 0001).
- Publishing to PyPI: consumers are our own repos and pin a GitHub tag (design 0001).
- A template-owned engine file instead of a library: covers settings only; `Secret`, metrics and the pipeline don't belong in every project (design 0001).
- A float setting type: no setting needs one; time settings are integer `_ms` (design 0001).
