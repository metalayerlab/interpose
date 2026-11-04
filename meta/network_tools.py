"""The network-tools meta module (brain record 0019): `meta network-tools <verb>`.

  status          CI on main, open PRs, release PRs and the interpose pins; exit 1 when a check
                  is not ok
  list WHAT       prs | releases | repos, one name per line
  show REPO       one repo in detail: release, CI on main, PR and issue counts, the interpose pin
                  and the local checkout

One executable that describes itself (`--meta-manifest`, record 0019 §2), run by the meta core,
which caches whole outputs, redacts and logs. Standard library only, so it starts without uv.
GitHub is read only through the `gh` CLI, with read-only subcommands; the local checkouts are
read with `git rev-parse` and `git status --porcelain` and never changed. It writes nothing.

Exit codes (record 0019 §3): 0 all well, 1 a status that found a problem, 2 usage, 4 `gh`
unreachable or failing, 5 no such repo.

Overrides, for tests: META_NETWORK_TOOLS_NOW (an ISO 8601 time to treat as now) and
META_NETWORK_TOOLS_DEV (the folder holding the local checkouts; default /pool/distributed/dev).
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

CENTRE = "network-tools"
OWNER = "network-tools-coordinator"
ORG = "metalayerlab"
REPOS = ("interpose", "mitm-proxy", "sluice")
PINNED = ("mitm-proxy", "sluice")  # the repos that pin interpose
CI_WORKFLOW = "base-ci.yml"
RELEASE_AUTHOR = "app/metalayerlab-stardate"
RELEASE_TITLE = "chore(main): release"
PR_STALE_DAYS = 3
RELEASE_PR_STALE_DAYS = 14
DEFAULT_TIMEOUT = 20.0
WIDTH = 100
DEV_ROOT = "/pool/distributed/dev"
PIN = re.compile(
    r"interpose\s*@\s*git\+https://github\.com/metalayerlab/interpose(?:\.git)?@(v[0-9][\w.\-]*)"
)

EXIT_OK, EXIT_PROBLEM, EXIT_USAGE, EXIT_FAILED, EXIT_NOT_FOUND = 0, 1, 2, 4, 5
STATES = {"ok": 0, "unknown": 1, "warn": 2, "fail": 3}
COLOURS = {"ok": "32", "unknown": "37", "warn": "33", "fail": "31"}
LIST_WHAT = ("prs", "releases", "repos")

MANIFEST: dict[str, Any] = {
    "meta": 1,
    "centre": CENTRE,
    "owner": OWNER,
    "summary": "interpose, sluice and mitm-proxy: CI on main, PRs, releases and interpose pins",
    "verbs": {
        "status": {
            "summary": "CI on main, stale PRs, release PRs and interpose pins",
            "args": [],
            "complete": [],
            "cache": 120,
        },
        "list": {
            "summary": "prs | releases | repos",
            "args": ["what"],
            "complete": list(LIST_WHAT),
            "cache": 900,
        },
        "show": {
            "summary": "one repo: release, CI, PRs, issues, pin and local checkout",
            "args": ["repo"],
            "complete": "list repos",
            "cache": 120,
        },
    },
    "results": [1, 5],
}

USAGE = {
    "status": "usage: meta network-tools status [--json]",
    "list": "usage: meta network-tools list prs|releases|repos [--json]",
    "show": "usage: meta network-tools show REPO [--json]   (REPO: " + " | ".join(REPOS) + ")",
}

# The only gh subcommands this module may run: each one reads.
READ_ONLY = {
    ("pr", "list"),
    ("run", "list"),
    ("release", "list"),
    ("release", "view"),
    ("issue", "list"),
}


class Exit(Exception):
    """Stop with this exit code, after one stderr line (if any)."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GhError(Exception):
    """A gh call that failed, timed out or could not start."""


# ---------------------------------------------------------------- small helpers


def say(message: str) -> None:
    """One warning or error line on stderr, as record 0019 §3 asks."""
    print(f"meta {CENTRE}: {message}", file=sys.stderr)


def now() -> datetime:
    fixed = os.environ.get("META_NETWORK_TOOLS_NOW")
    return parse_time(fixed) if fixed else datetime.now(UTC)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value: str | None) -> str | None:
    """A GitHub time as ISO 8601 with its offset."""
    return parse_time(value).isoformat() if value else None


def age_days(value: str) -> float:
    return (now() - parse_time(value)).total_seconds() / 86400


def age_words(value: str) -> str:
    seconds = max((now() - parse_time(value)).total_seconds(), 0)
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def stamp(value: str) -> str:
    return parse_time(value).astimezone().strftime("%Y-%m-%d %H:%M")


def clip(line: str) -> str:
    return line if len(line) <= WIDTH else line[: WIDTH - 1] + "…"


def version(tag: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", tag))


def full(repo: str) -> str:
    return f"{ORG}/{repo}"


def dev_root() -> str:
    return os.environ.get("META_NETWORK_TOOLS_DEV") or DEV_ROOT


def is_release_pr(pr: dict[str, Any]) -> bool:
    author = (pr.get("author") or {}).get("login", "")
    return bool(author == RELEASE_AUTHOR and str(pr.get("title", "")).startswith(RELEASE_TITLE))


# ---------------------------------------------------------------- gh, read-only


@dataclass
class Budget:
    """The seconds left of META_TIMEOUT, shared out among the gh calls."""

    deadline: float = field(default_factory=lambda: time.monotonic() + budget_seconds())

    def left(self) -> float:
        return max(self.deadline - time.monotonic(), 0.5)


def budget_seconds() -> float:
    try:
        total = float(os.environ.get("META_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        total = DEFAULT_TIMEOUT
    return max(total - 1, 1)  # a second kept back to print before the core kills us


def check_read_only(args: Sequence[str]) -> None:
    """Refuse any gh call that could write (record 0019 §6)."""
    if args[0] == "api":
        # gh api is a GET unless given a method or fields; only --jq is allowed beside the path.
        flags = [a for a in args[1:] if a.startswith("-")]
        if any(f != "--jq" for f in flags) or not args[1].startswith("repos/"):
            raise GhError(f"refused a gh api call that is not a plain read: {args[1]}")
        return
    if tuple(args[:2]) not in READ_ONLY:
        raise GhError(f"refused gh {' '.join(args[:2])}: not a read")


def gh(args: Sequence[str], budget: Budget) -> str:
    check_read_only(args)
    env = {
        **os.environ,
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "NO_COLOR": "1",
        "GH_PAGER": "",
        "PAGER": "cat",
    }
    try:
        r = subprocess.run(
            ["gh", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=budget.left(),
            env=env,
            check=False,
        )
    except FileNotFoundError as e:
        raise GhError("gh is not installed or not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise GhError(f"gh {args[0]} {args[1]} timed out") from e
    if r.returncode != 0:
        first = (r.stderr.strip().splitlines() or [""])[0][:80]
        raise GhError(f"gh {args[0]} {args[1]} exited {r.returncode}: {first}".rstrip(": "))
    return r.stdout


def gh_json(args: Sequence[str], budget: Budget) -> Any:
    out = gh(args, budget)
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise GhError(f"gh {args[0]} {args[1]} printed no JSON") from e


# The calls, one builder each, so the tests' recorded fixtures are keyed by the same argv.


def ci_args(repo: str) -> list[str]:
    return [
        "run",
        "list",
        "-R",
        full(repo),
        "--branch",
        "main",
        "--workflow",
        CI_WORKFLOW,
        "--status",
        "completed",
        "--limit",
        "1",
        "--json",
        "conclusion,createdAt,displayTitle,headSha,url",
    ]


def prs_args(repo: str) -> list[str]:
    return [
        "pr",
        "list",
        "-R",
        full(repo),
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,author,createdAt,isDraft,url",
    ]


def release_args(repo: str) -> list[str]:
    return ["release", "view", "-R", full(repo), "--json", "tagName,publishedAt"]


def releases_args(repo: str) -> list[str]:
    return [
        "release",
        "list",
        "-R",
        full(repo),
        "--limit",
        "100",
        "--json",
        "tagName,publishedAt,isLatest,isPrerelease,isDraft",
    ]


def issues_args(repo: str) -> list[str]:
    return [
        "issue",
        "list",
        "-R",
        full(repo),
        "--state",
        "open",
        "--limit",
        "1000",
        "--json",
        "number",
    ]


def pyproject_args(repo: str) -> list[str]:
    return ["api", f"repos/{full(repo)}/contents/pyproject.toml", "--jq", ".content"]


def fetch(calls: dict[str, list[str]], budget: Budget) -> dict[str, Any]:
    """Run the calls in parallel: {key: parsed JSON, or the GhError it raised}."""

    def one(args: list[str]) -> Any:
        if args[0] == "api":
            return gh(args, budget)
        return gh_json(args, budget)

    out: dict[str, Any] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(calls), 1)) as pool:
        futures = {key: pool.submit(one, args) for key, args in calls.items()}
        for key, fut in futures.items():
            try:
                out[key] = fut.result()
            except GhError as e:
                out[key] = e
    return out


def need(results: dict[str, Any], key: str) -> Any:
    value = results[key]
    if isinstance(value, GhError):
        raise Exit(EXIT_FAILED, str(value))
    return value


def all_failed(results: dict[str, Any]) -> bool:
    return bool(results) and all(isinstance(v, GhError) for v in results.values())


def read_pin(content_b64: str) -> str | None:
    text = base64.b64decode(content_b64.strip() or b"").decode("utf-8", "replace")
    m = PIN.search(text)
    return m.group(1) if m else None


# ---------------------------------------------------------------- the checks


Check = dict[str, Any]


def check(name: str, state: str, detail: str, since: str | None = None) -> Check:
    c: Check = {"name": name, "state": state, "detail": detail}
    if since:
        c["since"] = iso(since)
    return c


def ci_check(repo: str, value: Any) -> Check:
    name = f"ci {repo}"
    if isinstance(value, GhError):
        return check(name, "unknown", str(value))
    if not value:
        return check(name, "unknown", "no completed CI run on main")
    run = value[0]
    conclusion = run.get("conclusion") or "unknown"
    created = run.get("createdAt")
    when = f", {age_words(created)}" if created else ""
    detail = f"{conclusion}{when}: {run.get('displayTitle', '')}"
    if conclusion == "success":
        state = "ok"
    elif conclusion in ("failure", "timed_out", "startup_failure"):
        state = "fail"
    else:
        state = "warn"
    return check(name, state, detail, created)


def pr_checks(repo: str, value: Any) -> list[Check]:
    if isinstance(value, GhError):
        return [
            check(f"prs {repo}", "unknown", str(value)),
            check(f"release-pr {repo}", "unknown", str(value)),
        ]
    others = [p for p in value if not is_release_pr(p)]
    releases = [p for p in value if is_release_pr(p)]
    stale = sorted(
        (p for p in others if age_days(p["createdAt"]) > PR_STALE_DAYS),
        key=lambda p: p["createdAt"],
    )
    if stale:
        listed = ", ".join(f"#{p['number']} ({int(age_days(p['createdAt']))}d)" for p in stale)
        prs = check(
            f"prs {repo}",
            "warn",
            f"{len(stale)} of {len(others)} open over {PR_STALE_DAYS} days: {listed}",
            stale[0]["createdAt"],
        )
    else:
        prs = check(f"prs {repo}", "ok", f"{len(others)} open, none over {PR_STALE_DAYS} days")
    if not releases:
        rel = check(f"release-pr {repo}", "ok", "none open")
    else:
        oldest = min(releases, key=lambda p: p["createdAt"])
        days = age_days(oldest["createdAt"])
        state = "warn" if days > RELEASE_PR_STALE_DAYS else "ok"
        rel = check(
            f"release-pr {repo}",
            state,
            f"#{oldest['number']} {oldest['title']}, open {int(days)}d",
            oldest["createdAt"],
        )
    return [prs, rel]


def pin_check(repo: str, pyproject: Any, latest: Any) -> Check:
    name = f"pin {repo}"
    for value in (pyproject, latest):
        if isinstance(value, GhError):
            return check(name, "unknown", str(value))
    pin = read_pin(str(pyproject))
    tag = latest.get("tagName")
    if not pin:
        return check(name, "unknown", "no interpose pin in pyproject.toml on main")
    if not tag:
        return check(name, "unknown", f"pins {pin}; interpose has no release")
    if version(pin) == version(tag):
        return check(name, "ok", f"interpose {pin}, the latest")
    if version(pin) < version(tag):
        return check(name, "warn", f"interpose {pin}, behind the latest {tag}")
    return check(name, "warn", f"interpose {pin}, ahead of the latest release {tag}")


def worst(checks: list[Check]) -> str:
    return max((c["state"] for c in checks), key=lambda s: STATES[s], default="unknown")


def status_data(budget: Budget) -> dict[str, Any]:
    calls: dict[str, list[str]] = {}
    for repo in REPOS:
        calls[f"ci:{repo}"] = ci_args(repo)
        calls[f"prs:{repo}"] = prs_args(repo)
    calls["release:interpose"] = release_args("interpose")
    for repo in PINNED:
        calls[f"pyproject:{repo}"] = pyproject_args(repo)
    results = fetch(calls, budget)
    if all_failed(results):
        raise Exit(EXIT_FAILED, str(next(iter(results.values()))))
    for value in results.values():
        if isinstance(value, GhError):
            say(str(value))
    checks: list[Check] = []
    for repo in REPOS:
        checks.append(ci_check(repo, results[f"ci:{repo}"]))
        checks.extend(pr_checks(repo, results[f"prs:{repo}"]))
    for repo in PINNED:
        checks.append(pin_check(repo, results[f"pyproject:{repo}"], results["release:interpose"]))
    state = worst(checks)
    counts = {s: sum(c["state"] == s for c in checks) for s in STATES}
    parts = [f"{counts[s]} {s}" for s in ("fail", "warn", "unknown", "ok") if counts[s]]
    summary = f"{state}: {', '.join(parts)} across {len(REPOS)} repos"
    return {"summary": summary, "state": state, "checks": checks}


# ---------------------------------------------------------------- the verbs


@dataclass
class Out:
    """Where a verb's output goes, and how."""

    as_json: bool
    colour: bool
    lines: list[str] = field(default_factory=list)

    def line(self, text: str) -> None:
        self.lines.append(clip(text))


def paint(state: str, colour: bool) -> str:
    padded = f"{state:<7}"
    return f"\033[{COLOURS[state]}m{padded}\033[0m" if colour else padded


def verb_status(args: list[str], out: Out) -> tuple[int, Any]:
    if args:
        raise Exit(EXIT_USAGE, f"status takes no arguments ({USAGE['status']})")
    data = status_data(Budget())
    code = (
        EXIT_OK
        if data["state"] == "ok" and all(c["state"] == "ok" for c in data["checks"])
        else EXIT_PROBLEM
    )
    if not out.as_json:
        out.line(f"network-tools {data['summary']}")
        width = max(len(c["name"]) for c in data["checks"])
        for c in data["checks"]:
            text = f"{c['state']:<7} {c['name']:<{width}}  {c['detail']}"
            text = clip(text)
            if out.colour:
                text = paint(c["state"], True) + text[7:]
            out.lines.append(text)
    return code, data


def list_items(what: str, budget: Budget) -> list[dict[str, Any]]:
    if what == "repos":
        return [{"name": r, "repo": full(r), "checkout": f"{dev_root()}/{r}"} for r in REPOS]
    builder = prs_args if what == "prs" else releases_args
    results = fetch({r: builder(r) for r in REPOS}, budget)
    items: list[dict[str, Any]] = []
    for repo in REPOS:
        for x in need(results, repo):
            if what == "prs":
                items.append(
                    {
                        "name": f"{repo}#{x['number']}",
                        "repo": full(repo),
                        "number": x["number"],
                        "title": x.get("title", ""),
                        "author": (x.get("author") or {}).get("login"),
                        "created_at": iso(x.get("createdAt")),
                        "draft": bool(x.get("isDraft")),
                        "release": is_release_pr(x),
                        "url": x.get("url"),
                        "_key": (repo, x["number"]),
                    }
                )
            else:
                items.append(
                    {
                        "name": f"{repo} {x['tagName']}",
                        "repo": full(repo),
                        "tag": x["tagName"],
                        "published_at": iso(x.get("publishedAt")),
                        "latest": bool(x.get("isLatest")),
                        "prerelease": bool(x.get("isPrerelease")),
                        "_key": (repo, version(x["tagName"])),
                    }
                )
    items.sort(key=lambda i: i.pop("_key"))
    return items


def verb_list(args: list[str], out: Out) -> tuple[int, Any]:
    if len(args) != 1 or args[0] not in LIST_WHAT:
        raise Exit(EXIT_USAGE, f"list needs one of {', '.join(LIST_WHAT)} ({USAGE['list']})")
    items = list_items(args[0], Budget())
    for i in items:
        out.line(i["name"])
    return EXIT_OK, items


def checkout(repo: str, budget: Budget) -> dict[str, Any]:
    """The local checkout, read with git rev-parse and git status only: never changed."""
    path = f"{dev_root()}/{repo}"
    info: dict[str, Any] = {
        "path": path,
        "exists": os.path.isdir(path),
        "branch": None,
        "clean": None,
        "changes": None,
    }
    if not info["exists"]:
        return info

    def git(*a: str) -> str | None:
        try:
            # --no-optional-locks: git status would otherwise refresh the index, a write.
            r = subprocess.run(
                ["git", "--no-optional-locks", "-C", path, *a],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=budget.left(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    porcelain = git("status", "--porcelain")
    info["branch"] = branch.strip() if branch else None
    if porcelain is not None:
        changes = [ln for ln in porcelain.splitlines() if ln.strip()]
        info["clean"], info["changes"] = not changes, len(changes)
    return info


def show_data(repo: str, budget: Budget) -> dict[str, Any]:
    calls = {
        "release": release_args(repo),
        "ci": ci_args(repo),
        "prs": prs_args(repo),
        "issues": issues_args(repo),
    }
    if repo in PINNED:
        calls["pyproject"] = pyproject_args(repo)
        calls["latest"] = release_args("interpose")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        local = pool.submit(checkout, repo, budget)
        results = fetch(calls, budget)
        local_info = local.result()
    release = need(results, "release")
    runs = need(results, "ci")
    prs = need(results, "prs")
    issues = need(results, "issues")
    ci = ci_check(repo, runs)
    data: dict[str, Any] = {
        "name": repo,
        "repo": full(repo),
        "release": {"tag": release.get("tagName"), "published_at": iso(release.get("publishedAt"))},
        "ci_main": {
            "state": ci["state"],
            "detail": ci["detail"],
            "conclusion": runs[0].get("conclusion") if runs else None,
            "created_at": iso(runs[0].get("createdAt")) if runs else None,
            "url": runs[0].get("url") if runs else None,
        },
        "open_prs": len(prs),
        "open_release_prs": sum(is_release_pr(p) for p in prs),
        "open_issues": len(issues),
        "interpose_pin": None,
        "checkout": local_info,
    }
    if repo in PINNED:
        pin = read_pin(str(need(results, "pyproject")))
        latest = need(results, "latest").get("tagName")
        pc = pin_check(repo, need(results, "pyproject"), need(results, "latest"))
        data["interpose_pin"] = {"pin": pin, "latest": latest, "state": pc["state"]}
    return data


def verb_show(args: list[str], out: Out) -> tuple[int, Any]:
    if len(args) != 1:
        raise Exit(EXIT_USAGE, f"show needs one repo ({USAGE['show']})")
    repo = args[0].removeprefix(f"{ORG}/")
    if repo not in REPOS:
        raise Exit(EXIT_NOT_FOUND, f"no repo {args[0]!r} in network-tools ({', '.join(REPOS)})")
    data = show_data(repo, Budget())
    if not out.as_json:
        rel = data["release"]
        when = f" ({stamp(rel['published_at'])})" if rel["published_at"] else ""
        pin = data["interpose_pin"]
        co = data["checkout"]
        rows = [
            ("name", data["name"]),
            ("repo", data["repo"]),
            ("release", f"{rel['tag'] or 'none'}{when}"),
            ("ci_main", f"{data['ci_main']['state']}: {data['ci_main']['detail']}"),
            ("open_prs", f"{data['open_prs']} ({data['open_release_prs']} release)"),
            ("open_issues", str(data["open_issues"])),
        ]
        if pin is not None:
            rows.append(
                (
                    "interpose_pin",
                    f"{pin['pin'] or 'none'} (latest {pin['latest'] or 'none'}: {pin['state']})",
                )
            )
        rows.append(("checkout", co["path"] if co["exists"] else f"{co['path']} (missing)"))
        if co["exists"]:
            rows.append(("checkout_branch", co["branch"] or "unknown"))
            clean = (
                "unknown"
                if co["clean"] is None
                else "yes"
                if co["clean"]
                else f"no ({co['changes']} changed)"
            )
            rows.append(("checkout_clean", clean))
        for key, value in rows:
            out.line(f"{key}: {value}")
    return EXIT_OK, data


VERBS: dict[str, Callable[[list[str], Out], tuple[int, Any]]] = {
    "status": verb_status,
    "list": verb_list,
    "show": verb_show,
}


def help_text() -> str:
    lines = [f"usage: meta {CENTRE} <verb> [args] [--json]", "", MANIFEST["summary"], "", "verbs:"]
    for name, spec in MANIFEST["verbs"].items():
        args = " ".join(a.upper() for a in spec["args"])
        lines.append(f"  {(name + ' ' + args).strip():<12} {spec['summary']}")
    lines += ["", "exit: 0 all well, 1 a problem found, 2 usage, 4 gh failed, 5 no such repo"]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args == ["--meta-manifest"]:
            print(json.dumps(MANIFEST))
            return EXIT_OK
        if not args:
            raise Exit(EXIT_USAGE, f"no verb: one of {', '.join(VERBS)} (--help lists them)")
        if args[0] in ("-h", "--help", "help"):
            print(help_text())
            return EXIT_OK
        verb, rest = args[0], args[1:]
        if verb not in VERBS:
            raise Exit(EXIT_USAGE, f"unknown verb {verb!r}: one of {', '.join(VERBS)}")
        if "-h" in rest or "--help" in rest:
            print(USAGE[verb])
            return EXIT_OK
        as_json = os.environ.get("META_JSON") == "1" or "--json" in rest
        rest = [a for a in rest if a != "--json"]
        for a in rest:
            if a.startswith("-"):
                raise Exit(EXIT_USAGE, f"unknown option {a!r} ({USAGE[verb]})")
        colour = sys.stdout.isatty() and "NO_COLOR" not in os.environ and not as_json
        out = Out(as_json=as_json, colour=colour)
        code, data = VERBS[verb](rest, out)
        if as_json:
            print(json.dumps(data, ensure_ascii=False))
        elif out.lines:
            print("\n".join(out.lines))
        return code
    except Exit as e:
        if e.message:
            say(e.message)
        return e.code
    except GhError as e:
        say(str(e))
        return EXIT_FAILED
