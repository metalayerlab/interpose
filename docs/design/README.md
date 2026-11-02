# Design records

Why the code is the way it is. Copy [0000-template.md](0000-template.md) to `NNNN-short-title.md` before a
new feature, a new dependency, or a public-interface change, and add a line below. Status moves Proposed →
Approved (by a human) → Implemented; supersede an implemented record with a new one, never rewrite it.

**Index:** read this first, then open only the records relevant to your task. CI checks that every record is listed.

| # | Title | Status | Decision in one line |
|---|---|---|---|
| [0001](0001-initial-design.md) | Initial design | Implemented | v0.1 is settings and secrets: `Settings`/`Setting` with the template's lookup order and strict coercion, `Secret` and `read_secret_dir`; pyyaml only; consumers pin a GitHub tag; metrics (0002) and the pipeline core (0003) follow |
