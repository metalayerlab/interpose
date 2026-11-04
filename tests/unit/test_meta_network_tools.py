"""The network-tools meta module (meta/network-tools, brain record 0019), run as the core runs it.

A fake core here checks the manifest against record 0019 §2 and runs each verb with §3's
argv, closed stdin and environment, then checks its exit code, stderr and output. GitHub is a
fake `gh` on PATH serving the recorded responses in meta_fixtures/gh.json: no network.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
META = ROOT / "meta"
MODULE = META / "network-tools"
FIXTURES = Path(__file__).resolve().parent / "meta_fixtures" / "gh.json"

sys.path.insert(0, str(META))
import network_tools  # noqa: E402  meta/ is not a package: it is on the path above

RECORDED: dict[str, Any] = json.loads(FIXTURES.read_text())
NOW = RECORDED["recorded_at"]

# Record 0019 §3's exit codes: 3 (refused) and 6 (broken) are never a module's own answer here.
EXITS = {0, 1, 2, 4, 5}
FAKE_GH = """#!{python}
import json, os, sys, time
key = " ".join(sys.argv[1:])
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_GH_SLEEP"):
    time.sleep(float(os.environ["FAKE_GH_SLEEP"]))
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("HTTP 502: Bad Gateway (https://api.github.com/graphql)\\n")
    sys.exit(1)
calls = json.load(open(os.environ["FAKE_GH_FIXTURES"]))["calls"]
if key not in calls:
    sys.stderr.write("fake gh: no fixture for " + key + "\\n")
    sys.exit(1)
sys.stdout.write(calls[key]["stdout"])
sys.exit(calls[key]["exit"])
"""


# ---------------------------------------------------------------- the fake core


def validate_manifest(m: Any) -> list[str]:
    """What is wrong with a manifest by record 0019 §2, for this centre: [] when nothing."""
    problems: list[str] = []
    if not isinstance(m, dict):
        return ["not a JSON object"]
    if m.get("meta") != 1:
        problems.append("meta is not protocol 1")
    if m.get("centre") != "network-tools":
        problems.append("centre is not network-tools")
    if m.get("owner") != "network-tools-coordinator":
        problems.append("owner is not network-tools-coordinator")
    if not isinstance(m.get("summary"), str) or not m["summary"]:
        problems.append("no summary")
    verbs = m.get("verbs")
    if not isinstance(verbs, dict) or "status" not in verbs:
        return [*problems, "no verbs, or no status verb"]
    for name, spec in verbs.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
            problems.append(f"{name}: not a verb name")
        if name in {"launch", "stop", "restart", "help", "menu"}:
            problems.append(f"{name}: the core's own verb")
        if not isinstance(spec, dict) or not isinstance(spec.get("summary"), str):
            problems.append(f"{name}: no summary")
            continue
        cache = spec.get("cache", 0)
        if not isinstance(cache, int) or isinstance(cache, bool) or not 0 <= cache <= 86400:
            problems.append(f"{name}: cache is not whole seconds")
        if spec.get("writes") or spec.get("interactive"):
            problems.append(f"{name}: writes or interactive on a read-only module")
        args = spec.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            problems.append(f"{name}: args is not a list of names")
        comp = spec.get("complete")
        if isinstance(comp, str):
            words = comp.split()
            if not words or words[0] not in verbs or words[0] == name:
                problems.append(f"{name}: complete names no other verb")
        elif comp is not None and not (
            isinstance(comp, list) and all(isinstance(x, str) for x in comp)
        ):
            problems.append(f"{name}: complete is not a list or a verb")
    results = m.get("results", [1])
    if not isinstance(results, list) or not all(
        isinstance(r, int) and 0 <= r <= 6 for r in results
    ):
        problems.append("results is not a list of exit codes")
    return problems


@dataclass
class Run:
    code: int
    out: str
    err: str
    seconds: float
    calls: list[list[str]]

    def json(self) -> Any:
        return json.loads(self.out)


class FakeCore:
    """Runs the module as record 0019 §3 says the core does."""

    def __init__(self, tmp: Path, fixtures: dict[str, Any]) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH.format(python=sys.executable))
        gh.chmod(0o755)
        self.fixtures = tmp / "gh.json"
        self.fixtures.write_text(json.dumps(fixtures))
        self.log = tmp / "gh.log"
        self.cache = tmp / "cache"
        self.cache.mkdir(mode=0o700)
        self.dev = tmp / "dev"
        self.dev.mkdir()
        self.extra: dict[str, str] = {}
        self.path = f"{self.bin}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}/usr/bin:/bin"

    def set_fixtures(self, fixtures: dict[str, Any]) -> None:
        self.fixtures.write_text(json.dumps(fixtures))

    def manifest(self) -> Run:
        return self._run(["--meta-manifest"], {"META": "1", "META_CENTRE": "network-tools"}, 2)

    def run(self, verb: str, *args: str, as_json: bool = False, timeout: int = 20) -> Run:
        env = {
            "META": "1",
            "META_CENTRE": "network-tools",
            "META_VERB": verb,
            "META_CACHE_DIR": str(self.cache),
            "META_TIMEOUT": str(timeout),
        }
        argv = [verb, *args]
        if as_json:
            env["META_JSON"] = "1"
            argv.append("--json")
        return self._run(argv, env, timeout + 5)

    def _run(self, argv: list[str], meta_env: dict[str, str], timeout: float) -> Run:
        self.log.write_text("")
        env = {
            "PATH": self.path,
            "HOME": str(self.tmp),
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "FAKE_GH_FIXTURES": str(self.fixtures),
            "FAKE_GH_LOG": str(self.log),
            "META_NETWORK_TOOLS_NOW": NOW,
            "META_NETWORK_TOOLS_DEV": str(self.dev),
            **meta_env,
            **self.extra,
        }
        start = time.monotonic()
        r = subprocess.run(  # noqa: S603  the module under test, with argv from the tests
            [str(MODULE), *argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        calls = [json.loads(ln) for ln in self.log.read_text().splitlines() if ln]
        run = Run(r.returncode, r.stdout, r.stderr, time.monotonic() - start, calls)
        self._check_contract(run, argv, env.get("META_JSON") == "1")
        return run

    @staticmethod
    def _check_contract(run: Run, argv: list[str], as_json: bool) -> None:
        """What the core would hold any run to (§3)."""
        assert run.code in EXITS, f"exit {run.code} is not one this module may give"
        for line in run.err.splitlines():
            assert line.startswith("meta network-tools: "), f"stderr line {line!r}"
        assert "\033[" not in run.out, "colour on a pipe"
        if as_json and run.code in (0, 1):
            json.loads(run.out)  # exactly one JSON value
        elif "--meta-manifest" not in argv:
            for line in run.out.splitlines():
                assert len(line) <= 100, f"wider than 100: {line!r}"


def fixtures() -> dict[str, Any]:
    return copy.deepcopy(RECORDED)


def key(args: list[str]) -> str:
    return " ".join(args)


def set_pin(fx: dict[str, Any], repo: str, tag: str) -> None:
    k = key(network_tools.pyproject_args(repo))
    text = base64.b64decode(fx["calls"][k]["stdout"].strip()).decode()
    text = re.sub(r"(metalayerlab/interpose@)v[\d.]+", rf"\g<1>{tag}", text)
    fx["calls"][k]["stdout"] = base64.b64encode(text.encode()).decode() + "\n"


def all_ok(fx: dict[str, Any]) -> dict[str, Any]:
    latest = json.loads(fx["calls"][key(network_tools.release_args("interpose"))]["stdout"])
    for repo in network_tools.PINNED:
        set_pin(fx, repo, latest["tagName"])
    return fx


@pytest.fixture
def core(tmp_path: Path) -> Iterator[FakeCore]:
    yield FakeCore(tmp_path, fixtures())


def by_name(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in data["checks"]}


# ---------------------------------------------------------------- the manifest


def test_manifest_passes_the_fake_core(core: FakeCore) -> None:
    run = core.manifest()
    assert run.code == 0
    m = json.loads(run.out)
    assert validate_manifest(m) == []
    assert m["results"] == [1, 5]
    assert set(m["verbs"]) == {"status", "list", "show"}
    for spec in m["verbs"].values():
        assert {"summary", "args", "complete", "cache"} <= set(spec)
    assert m["verbs"]["status"]["cache"] == 120
    assert m["verbs"]["list"]["cache"] == 900
    assert m["verbs"]["show"] == {**m["verbs"]["show"], "cache": 120, "complete": "list repos"}
    assert sorted(m["verbs"]["list"]["complete"]) == ["prs", "releases", "repos"]


def test_manifest_is_quick_and_runs_no_gh(core: FakeCore) -> None:
    core.extra["FAKE_GH_SLEEP"] = "5"
    run = core.manifest()
    assert run.calls == []
    assert run.seconds < 2


def test_the_fake_core_rejects_a_bad_manifest() -> None:
    m = copy.deepcopy(network_tools.MANIFEST)
    m["verbs"]["show"]["writes"] = True
    m["verbs"]["list"]["complete"] = "nope"
    del m["verbs"]["status"]
    assert validate_manifest(m)
    assert validate_manifest({"meta": 2}) != []


def test_help_lists_the_verbs(core: FakeCore) -> None:
    run = core._run(["--help"], {"META": "1"}, 5)
    assert run.code == 0
    for verb in ("status", "list", "show"):
        assert re.search(rf"^\s+{verb}\b", run.out, re.M)
    assert core.run("show", "--help").out.startswith("usage: meta network-tools show")


# ---------------------------------------------------------------- status


def test_status_recorded_finds_the_pin_behind(core: FakeCore) -> None:
    run = core.run("status")
    assert run.code == 1
    lines = run.out.splitlines()
    assert lines[0].startswith("network-tools warn:")
    assert len(lines) == 1 + 3 * 3 + 2
    assert re.match(
        r"warn\s+pin sluice\s+interpose v0\.3\.0, behind the latest v0\.4\.1", lines[-1]
    )


def test_status_all_ok_exits_0(core: FakeCore) -> None:
    core.set_fixtures(all_ok(fixtures()))
    run = core.run("status", as_json=True)
    assert run.code == 0, run.out
    data = run.json()
    assert data["state"] == "ok"
    assert {c["state"] for c in data["checks"]} == {"ok"}
    assert set(data) == {"summary", "state", "checks"}


def test_status_failing_ci_stale_pr_and_old_release_pr(core: FakeCore) -> None:
    fx = all_ok(fixtures())
    ci = key(network_tools.ci_args("interpose"))
    runs = json.loads(fx["calls"][ci]["stdout"])
    runs[0]["conclusion"] = "failure"
    fx["calls"][ci]["stdout"] = json.dumps(runs)
    prs = key(network_tools.prs_args("mitm-proxy"))
    fx["calls"][prs]["stdout"] = json.dumps(
        [
            {
                "number": 40,
                "title": "feat: a new filter",
                "author": {"login": "someone"},
                "createdAt": "2026-10-30T10:00:00Z",
                "isDraft": False,
                "url": "u",
            },
            {
                "number": 41,
                "title": "fix: recent",
                "author": {"login": "someone"},
                "createdAt": "2026-11-03T10:00:00Z",
                "isDraft": False,
                "url": "u",
            },
            {
                "number": 39,
                "title": "chore(main): release 0.4.0",
                "author": {"login": "app/metalayerlab-stardate"},
                "createdAt": "2026-10-10T10:00:00Z",
                "isDraft": False,
                "url": "u",
            },
        ]
    )
    set_pin(fx, "sluice", "v0.2.2")
    core.set_fixtures(fx)
    run = core.run("status", as_json=True)
    assert run.code == 1
    data = run.json()
    assert data["state"] == "fail"
    checks = by_name(data)
    assert checks["ci interpose"]["state"] == "fail"
    assert checks["prs mitm-proxy"]["state"] == "warn"
    assert "#40 (4d)" in checks["prs mitm-proxy"]["detail"]
    assert "#41" not in checks["prs mitm-proxy"]["detail"]
    assert checks["release-pr mitm-proxy"]["state"] == "warn"
    assert checks["pin sluice"]["state"] == "warn"
    assert checks["pin mitm-proxy"]["state"] == "ok"
    assert checks["release-pr sluice"]["state"] == "ok"  # the recorded one, under 14 days
    assert checks["prs mitm-proxy"]["since"] == "2026-10-30T10:00:00+00:00"


def test_status_with_one_call_failing_marks_it_unknown(core: FakeCore) -> None:
    fx = all_ok(fixtures())
    del fx["calls"][key(network_tools.ci_args("sluice"))]
    core.set_fixtures(fx)
    run = core.run("status", as_json=True)
    assert run.code == 1
    assert by_name(run.json())["ci sluice"]["state"] == "unknown"
    assert "meta network-tools: gh run list exited 1" in run.err


def test_status_runs_only_read_only_gh_calls_in_parallel(core: FakeCore) -> None:
    core.extra["FAKE_GH_SLEEP"] = "1"
    run = core.run("status")
    assert len(run.calls) == 9
    assert run.seconds < 4  # nine one-second calls, run at once
    for args in run.calls:
        network_tools.check_read_only(args)


# ---------------------------------------------------------------- list


def test_list_repos_is_sorted_names(core: FakeCore) -> None:
    run = core.run("list", "repos")
    assert run.code == 0
    assert run.out.splitlines() == ["interpose", "mitm-proxy", "sluice"]
    assert run.calls == []


def test_list_prs_and_releases(core: FakeCore) -> None:
    prs = core.run("list", "prs")
    assert prs.out.splitlines() == ["sluice#76"]
    releases = core.run("list", "releases").out.splitlines()
    assert "interpose v0.4.1" in releases
    assert all(re.fullmatch(r"(interpose|mitm-proxy|sluice) v\d+\.\d+\.\d+", r) for r in releases)
    interpose = [r for r in releases if r.startswith("interpose ")]
    assert interpose == sorted(interpose, key=lambda r: network_tools.version(r))
    data = core.run("list", "prs", as_json=True).json()
    assert data[0]["name"] == "sluice#76"
    assert data[0]["release"] is True


def test_list_json_by_environment_only(core: FakeCore) -> None:
    core.extra["META_JSON"] = "1"
    run = core.run("list", "repos")
    assert [r["name"] for r in run.json()] == ["interpose", "mitm-proxy", "sluice"]


@pytest.mark.parametrize("args", [(), ("bogus",), ("repos", "extra"), ("repos", "--wide")])
def test_list_usage_is_2(core: FakeCore, args: tuple[str, ...]) -> None:
    run = core.run("list", *args)
    assert run.code == 2
    assert run.out == ""
    assert len(run.err.splitlines()) == 1


# ---------------------------------------------------------------- show


def test_show_sluice(core: FakeCore) -> None:
    repo = core.dev / "sluice"
    # git from PATH, as the module finds it: a scratch repo, not a real checkout.
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)  # noqa: S603, S607
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)  # noqa: S603
    (repo / "new.txt").write_text("x")
    run = core.run("show", "sluice")
    assert run.code == 0
    rows = dict(line.split(": ", 1) for line in run.out.splitlines())
    assert rows["release"] == "v0.2.5 (2026-11-02 19:07)"
    assert rows["ci_main"].startswith("ok: success")
    assert rows["open_prs"] == "1 (1 release)"
    assert rows["interpose_pin"] == "v0.3.0 (latest v0.4.1: warn)"
    assert rows["checkout_branch"] == "main"
    assert rows["checkout_clean"] == "no (1 changed)"
    data = core.run("show", "metalayerlab/sluice", as_json=True).json()
    assert data["name"] == "sluice"
    assert data["checkout"]["clean"] is False
    assert data["interpose_pin"] == {"pin": "v0.3.0", "latest": "v0.4.1", "state": "warn"}


def test_show_interpose_has_no_pin_and_a_missing_checkout(core: FakeCore) -> None:
    run = core.run("show", "interpose")
    assert run.code == 0
    assert "interpose_pin" not in run.out
    assert run.out.splitlines()[-1].startswith("checkout: ")
    data = core.run("show", "interpose", as_json=True).json()
    assert data["interpose_pin"] is None
    assert data["checkout"] == {**data["checkout"], "exists": False, "branch": None}


def test_show_unknown_repo_is_5(core: FakeCore) -> None:
    run = core.run("show", "sonar")
    assert run.code == 5
    assert run.calls == []
    assert "no repo 'sonar'" in run.err


def test_unknown_verb_and_option_are_2(core: FakeCore) -> None:
    assert core.run("shwo").code == 2
    assert core.run("status", "--wide").code == 2
    assert core.run("show").code == 2
    assert core._run([], {"META": "1"}, 5).code == 2


# ---------------------------------------------------------------- gh failing


@pytest.mark.parametrize("argv", [("status",), ("list", "prs"), ("show", "sluice")])
def test_gh_failing_is_4(core: FakeCore, argv: tuple[str, ...]) -> None:
    core.extra["FAKE_GH_FAIL"] = "1"
    run = core.run(*argv, as_json=True)
    assert run.code == 4
    assert run.out == ""
    assert "HTTP 502" in run.err


def test_gh_missing_is_4(core: FakeCore) -> None:
    lone = core.tmp / "lone"
    lone.mkdir()
    (lone / "python3").symlink_to(sys.executable)
    core.path = str(lone)
    run = core.run("status")
    assert run.code == 4
    assert "gh is not installed" in run.err


def test_gh_slower_than_meta_timeout_is_4(core: FakeCore) -> None:
    core.extra["FAKE_GH_SLEEP"] = "10"
    run = core.run("list", "prs", timeout=2)
    assert run.code == 4
    assert run.seconds < 5
    assert "timed out" in run.err


# ---------------------------------------------------------------- security (record 0019 §6)


FORBIDDEN = [
    r"gh auth token",
    r"auth\s*token",
    r"pass show",
    r"\bpr merge\b",
    r"\bpr create\b",
    r"\bissue create\b",
    r"\brelease create\b",
    r"-X\s*(POST|PATCH|PUT|DELETE)",
    r"--method",
    r'"merge"',
    r'"create"',
    r'"edit"',
    r'"delete"',
    r'"comment"',
    r'"close"',
]


def test_source_has_no_secret_reads_or_gh_writes() -> None:
    for path in META.iterdir():
        if path.is_file() and path.suffix != ".md":
            text = path.read_text()
            for pattern in FORBIDDEN:
                assert not re.search(pattern, text), f"{path.name}: {pattern}"


@pytest.mark.parametrize(
    "args",
    [
        ["api", "-X", "POST", "repos/metalayerlab/interpose/issues"],
        ["api", "repos/metalayerlab/interpose/issues", "-f", "title=x"],
        ["api", "graphql", "--jq", ".x"],
        ["pr", "merge", "1"],
        ["release", "delete", "v1"],
    ],
)
def test_gh_writes_are_refused(args: list[str]) -> None:
    with pytest.raises(network_tools.GhError):
        network_tools.check_read_only(args)


def test_meta_stays_out_of_the_package() -> None:
    build = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]
    assert build["targets"]["wheel"]["packages"] == ["src/interpose"]
    excluded = build["targets"]["sdist"]["exclude"]
    assert "/meta" in excluded
    assert "/tests/unit/test_meta_network_tools.py" in excluded
    assert "/tests/unit/meta_fixtures" in excluded
