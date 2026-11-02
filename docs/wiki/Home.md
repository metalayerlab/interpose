# interpose

Shared mechanism for metalayerlab interceptors — the layered settings loader, Secret values, Prometheus text exposition, and a policy-configurable async middleware pipeline.

interpose is a small Python library for metalayerlab's interceptors (mitm-proxy, sluice). They depend on a GitHub release tag. v0.1 holds the settings engine and `Secret`. Each setting is declared once and read from a `_FILE` variable, the environment, a YAML file or its default, with strict types and a record of where each value came from:

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

## Table of Contents

**Getting started**: [Introduction](Introduction) · [Quick Start](Quick-Start)

**Using it**: [API Reference](API-Reference)
**Help**: [Troubleshooting](Troubleshooting) · [FAQ](Frequently-Asked-Questions) · [Security](Security)

**Developers**: [Contributing](Contributing) · [Architecture Internals](Architecture-Internals) · [Testing Guide](Testing-Guide) · [Release Process](Release-Process)
