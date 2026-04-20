#!/usr/bin/env python3
"""Collect org commit stats and patch profile/README.md + stats.json.

Runs under GitHub Actions with GITHUB_TOKEN. Two outputs:
    profile/README.md  - human-facing, marker-delimited sections rewritten
    profile/stats.json - machine-readable numbers used by shields.io badges
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ORG = os.environ.get("ORG", "Conflux-Union")
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "30"))
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "profile" / "README.md"
STATS_JSON = ROOT / "profile" / "stats.json"

if not TOKEN:
    sys.exit("GITHUB_TOKEN is required")


def api(path: str, params: dict | None = None) -> object:
    url = f"https://api.github.com{path}"
    if params:
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        url += ("&" if "?" in url else "?") + qs
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"{ORG}-profile-stats",
        },
    )
    for attempt in range(4):
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 409:  # empty repo
                return []
            if e.code in (403, 429) and attempt < 3:
                reset = int(e.headers.get("X-RateLimit-Reset", "0") or 0)
                wait = max(5, reset - int(time.time())) if reset else 30
                time.sleep(min(wait, 60))
                continue
            raise
    raise RuntimeError(f"giving up on {path}")


def paginate(path: str, params: dict | None = None, cap: int = 20) -> list:
    out: list = []
    p = dict(params or {})
    p.setdefault("per_page", 100)
    for page in range(1, cap + 1):
        p["page"] = page
        batch = api(path, p)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < p["per_page"]:
            break
    return out


def list_repos() -> list[dict]:
    repos = paginate(f"/orgs/{ORG}/repos", {"type": "public"})
    return [r for r in repos if not r.get("archived") and not r.get("disabled")]


def list_members() -> list[str]:
    members = paginate(f"/orgs/{ORG}/members")
    return [m["login"] for m in members]


def commits_since(repo: str, since_iso: str) -> list[dict]:
    return paginate(
        f"/repos/{ORG}/{repo}/commits",
        {"since": since_iso},
        cap=10,
    )


def render_stats_block(
    total: int,
    repo_count: int,
    active_repos: int,
    member_count: int,
    window: int,
) -> str:
    return (
        "| Metric | Value |\n"
        "|---|---|\n"
        f"| Commits in last {window} days | **{total}** |\n"
        f"| Public repositories | **{repo_count}** |\n"
        f"| Active repositories ({window}d) | **{active_repos}** |\n"
        f"| Members | **{member_count}** |\n"
    )


def render_ranking_block(
    ranking: list[tuple[str, int]],
    members: set[str],
    window: int,
) -> str:
    if not ranking:
        return f"_No commits recorded in the last {window} days._\n"
    lines = [
        "| Rank | Member | Commits |",
        "|---:|:---|---:|",
    ]
    for i, (login, count) in enumerate(ranking, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
        tag = "" if login in members else " _(outside contributor)_"
        lines.append(
            f"| {medal} | [@{login}](https://github.com/{login}){tag} | {count} |"
        )
    return "\n".join(lines) + "\n"


def patch(text: str, marker: str, content: str) -> str:
    pattern = re.compile(
        rf"<!-- {marker}:START -->.*?<!-- {marker}:END -->",
        re.DOTALL,
    )
    replacement = f"<!-- {marker}:START -->\n{content}\n<!-- {marker}:END -->"
    new, n = pattern.subn(replacement, text)
    if n == 0:
        raise RuntimeError(f"marker {marker} not found in README")
    return new


def main() -> None:
    since_dt = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    repos = list_repos()
    members = list_members()
    member_set = set(members)

    author_counts: Counter[str] = Counter()
    total = 0
    active_repos = 0

    for repo in repos:
        name = repo["name"]
        try:
            commits = commits_since(name, since_iso)
        except HTTPError as e:
            print(f"skip {name}: HTTP {e.code}", file=sys.stderr)
            continue
        if commits:
            active_repos += 1
        for c in commits:
            total += 1
            author = (c.get("author") or {}).get("login")
            if not author:
                author = ((c.get("commit") or {}).get("author") or {}).get("name") or "unknown"
            author_counts[author] += 1

    ranking_members = [
        (login, author_counts.get(login, 0))
        for login in members
        if author_counts.get(login, 0) > 0
    ]
    extras = [
        (login, count)
        for login, count in author_counts.items()
        if login not in member_set and count > 0
    ]
    ranking = sorted(
        ranking_members + extras,
        key=lambda x: (-x[1], x[0].lower()),
    )

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    text = README.read_text(encoding="utf-8")
    text = patch(
        text,
        "STATS",
        render_stats_block(total, len(repos), active_repos, len(members), WINDOW_DAYS),
    )
    text = patch(text, "RANKING", render_ranking_block(ranking, member_set, WINDOW_DAYS))
    text = patch(text, "UPDATED", updated)
    README.write_text(text, encoding="utf-8")

    STATS_JSON.write_text(
        json.dumps(
            {
                "org": ORG,
                "window_days": WINDOW_DAYS,
                "recent_commits": total,
                "active_repos": active_repos,
                "public_repos": len(repos),
                "members": len(members),
                "updated_at": updated,
                "ranking": [
                    {"login": login, "commits": count} for login, count in ranking
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"commits={total} active_repos={active_repos} members={len(members)}")


if __name__ == "__main__":
    main()
