# interpose

Shared mechanism for metalayerlab interceptors — a policy-configurable async middleware pipeline, the layered settings loader, Secret values, and Prometheus text exposition.

interpose is a small Python library for metalayerlab's interceptors (mitm-proxy, sluice), which depend on a GitHub release tag. It has three components: the pipeline, settings (with secrets) and metrics.

## Pipeline

One middleware loop, `interpose.pipeline`: the project keeps its own hooks and unit of work, and fail open or closed is policy, per project and per middleware.

- [API: `interpose.pipeline`](API-Reference#interposepipeline)
- [How the guard works](Architecture-Internals#pipeline)
- [Performance](Architecture-Internals#performance)
- [A hook "timed out" although it returned a result](Troubleshooting#a-hook-timed-out-although-it-returned-a-result)

## Settings

`interpose.settings` and `interpose.secret`. Each setting is declared once and read from a `_FILE` variable, the environment, a YAML file or its default, with strict types and a record of where each value came from:

```python
from pathlib import Path

from interpose.settings import Setting, Settings, port

SETTINGS = Settings(
    "MYAPP_",
    (Setting("listen.port", 8080, "Port.", check=port),),
    default_path=Path("/etc/myapp/config.yaml"),
)
r = SETTINGS.resolve(environ={"MYAPP_LISTEN_PORT": "9090"})
assert (r.values["listen.port"], r.sources["listen.port"]) == (9090, "MYAPP_LISTEN_PORT")
```

- [First run](Quick-Start#first-run)
- [API: `interpose.settings`](API-Reference#interposesettings) · [`interpose.secret`](API-Reference#interposesecret)
- [How a value is resolved](Architecture-Internals#settings)
- [Secrets](Security#secrets)
- [Common problems](Troubleshooting#common-problems)

## Metrics

Prometheus text exposition, `interpose.metrics`, with every label's values declared up front. The project serves the text.

- [API: `interpose.metrics`](API-Reference#interposemetrics)
- [How the text is rendered](Architecture-Internals#metrics)

## Table of Contents

**Getting started**: [Introduction](Introduction) · [Quick Start](Quick-Start)

**Using it**: [API Reference](API-Reference)
**Help**: [Troubleshooting](Troubleshooting) · [FAQ](Frequently-Asked-Questions) · [Security](Security)

**Developers**: [Contributing](Contributing) · [Architecture Internals](Architecture-Internals) · [Testing Guide](Testing-Guide) · [Release Process](Release-Process)
