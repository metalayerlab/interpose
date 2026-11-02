# Design records

Why the code is the way it is. Copy [0000-template.md](0000-template.md) to `NNNN-short-title.md` before a
new feature, a new dependency, or a public-interface change, and add a line below. Status moves Proposed →
Approved (by a human) → Implemented; supersede an implemented record with a new one, never rewrite it.

**Index:** read this first, then open only the records relevant to your task. CI checks that every record is listed.

| # | Title | Status | Decision in one line |
|---|---|---|---|
| [0001](0001-initial-design.md) | Initial design | Implemented | v0.1 is settings and secrets: `Settings`/`Setting` with the template's lookup order and strict coercion, `Secret` and `read_secret_dir`; pyyaml only; consumers pin a GitHub tag; metrics (0002) and the pipeline core (0003) follow |
| [0002](0002-metrics.md) | Metrics | Implemented | `interpose.metrics`: mitm-proxy's hand-written Prometheus text exposition (`Counter`, `Gauge`, `Histogram`, `Registry`), with every label limited to a declared `Label` value set; no dependency, no transport |
| [0003](0003-pipeline-core.md) | Pipeline core | Implemented; decisions confirmed in 0004 | `interpose.pipeline`: the shared loop and the guard around every hook (timing, timeout, failure); a project `Policy` with no defaults for the deliberate differences (fail open/closed, after-hook order, after hooks on local answers, observers on failed middleware) and per-middleware `on_error` and timeout; reconciles both projects' "Decided against" lists |
| [0004](0004-pipeline-core-decisions.md) | Pipeline core decisions | Implemented | Stephen's answers to 0003's eleven decisions (all confirmed as built; consumer-side follow-ups noted), and decision 6's fix: `skip_inherited` counts a hook set on the instance as an override and still asks `until` after a hook it leaves out (#12) |
