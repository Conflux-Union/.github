#!/usr/bin/env python3
"""Collect org commit stats and patch profile/README.md + stats.json.

Runs under GitHub Actions with GITHUB_TOKEN. Outputs:
    profile/README.md  - human-facing, marker-delimited sections rewritten
    profile/stats.json - machine-readable numbers used by shields.io badges
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ORG = os.environ.get("ORG", "Conflux-Union")
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "90"))
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "profile" / "README.md"
STATS_JSON = ROOT / "profile" / "stats.json"
OVERRIDES = ROOT / "profile" / "members_override.json"

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


def load_overrides() -> tuple[set[str], set[str]]:
    if not OVERRIDES.exists():
        return set(), set()
    data = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    extra = {str(x) for x in data.get("extra_members", [])}
    exclude = {str(x) for x in data.get("exclude_authors", [])}
    return extra, exclude


def commits_since(repo: str, since_iso: str) -> list[dict]:
    return paginate(
        f"/repos/{ORG}/{repo}/commits",
        {"since": since_iso},
        cap=10,
    )


def repo_languages(repo: str) -> dict[str, int]:
    data = api(f"/repos/{ORG}/{repo}/languages")
    return data if isinstance(data, dict) else {}


def render_stats_block(
    total: int,
    repo_count: int,
    active_repos: int,
    member_count: int,
    window: int,
) -> str:
    return (
        "| 指标 | 数值 |\n"
        "|---|---|\n"
        f"| 近 {window} 天提交总数 | **{total}** |\n"
        f"| 公开仓库总数 | **{repo_count}** |\n"
        f"| 活跃仓库数({window}d) | **{active_repos}** |\n"
        f"| 成员总数 | **{member_count}** |\n"
    )


def render_ranking_block(
    ranking: list[tuple[str, int]],
    members: set[str],
    window: int,
) -> str:
    if not ranking:
        return f"_近 {window} 天暂无提交记录。_\n"
    lines = [
        "| 排名 | 成员 | 提交数 |",
        "|---:|:---|---:|",
    ]
    for i, (login, count) in enumerate(ranking, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
        tag = "" if login in members else " _(外部贡献者)_"
        safe = login.replace("[", "\\[").replace("]", "\\]")
        lines.append(
            f"| {medal} | [@{safe}](https://github.com/{login}){tag} | {count} |"
        )
    return "\n".join(lines) + "\n"


def render_daily_chart(daily: dict[str, int], window: int) -> str:
    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=i) for i in range(window - 1, -1, -1)]

    if window <= 45:
        values = [daily.get(d.isoformat(), 0) for d in days]
        labels = [f'"{d.strftime("%m-%d")}"' for d in days]
        title = f"近 {window} 天每日提交数"
    else:
        bucket_size = 7
        offset = len(days) % bucket_size
        buckets: list[tuple[str, int]] = []
        if offset:
            head = days[:offset]
            buckets.append((
                head[0].strftime("%m-%d"),
                sum(daily.get(d.isoformat(), 0) for d in head),
            ))
        for i in range(offset, len(days), bucket_size):
            chunk = days[i : i + bucket_size]
            buckets.append((
                chunk[0].strftime("%m-%d"),
                sum(daily.get(d.isoformat(), 0) for d in chunk),
            ))
        labels = [f'"{lbl}"' for lbl, _ in buckets]
        values = [v for _, v in buckets]
        title = f"近 {window} 天每周提交数(按自然周聚合)"

    y_max = max(values + [1])
    y_top = y_max + max(1, y_max // 5)
    return (
        "```mermaid\n"
        "xychart-beta\n"
        f'    title "{title}"\n'
        f"    x-axis [{', '.join(labels)}]\n"
        f'    y-axis "提交数" 0 --> {y_top}\n'
        f"    bar [{', '.join(str(v) for v in values)}]\n"
        f"    line [{', '.join(str(v) for v in values)}]\n"
        "```\n"
    )


def render_languages_block(lang_bytes: dict[str, int], top_n: int = 8) -> str:
    if not lang_bytes:
        return "_暂无可用的语言数据。_\n"
    total = sum(lang_bytes.values())
    items = sorted(lang_bytes.items(), key=lambda x: -x[1])
    top = items[:top_n]
    others = sum(v for _, v in items[top_n:])
    pie_lines = ["```mermaid", 'pie showData', '    title 语言占比(字节数,活跃仓库加权)']
    for name, b in top:
        safe = name.replace('"', '')
        pie_lines.append(f'    "{safe}" : {b}')
    if others > 0:
        pie_lines.append(f'    "Others" : {others}')
    pie_lines.append("```")

    table_lines = ["", "| 排名 | 语言 | 占比 | 字节数 |", "|---:|:---|---:|---:|"]
    for i, (name, b) in enumerate(top, 1):
        pct = b / total * 100
        table_lines.append(f"| #{i} | {name} | {pct:.1f}% | {b:,} |")
    if others > 0:
        pct = others / total * 100
        table_lines.append(f"| — | 其他 | {pct:.1f}% | {others:,} |")

    return "\n".join(pie_lines) + "\n" + "\n".join(table_lines) + "\n"


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
    api_members = list_members()
    extra_members, exclude_authors = load_overrides()
    member_set = set(api_members) | extra_members
    members = sorted(member_set, key=str.lower)
    print(
        f"members: api={len(api_members)} override=+{len(extra_members)} "
        f"total={len(members)} excluded_bots={len(exclude_authors)}",
        file=sys.stderr,
    )
    if len(api_members) <= 5:
        print(
            "WARN: only public members returned from API — token likely lacks "
            "read:org scope or is a repo-scoped GITHUB_TOKEN. Private members "
            "will be missed unless listed in profile/members_override.json.",
            file=sys.stderr,
        )

    author_counts: Counter[str] = Counter()
    daily_counts: dict[str, int] = defaultdict(int)
    lang_bytes: Counter[str] = Counter()
    total = 0
    active_repos = 0
    repo_commit_count: dict[str, int] = {}

    for repo in repos:
        name = repo["name"]
        try:
            commits = commits_since(name, since_iso)
        except HTTPError as e:
            print(f"skip {name}: HTTP {e.code}", file=sys.stderr)
            continue

        if commits:
            active_repos += 1
            repo_commit_count[name] = len(commits)

        for c in commits:
            author = (c.get("author") or {}).get("login")
            if not author:
                author = ((c.get("commit") or {}).get("author") or {}).get("name") or "unknown"
            if author in exclude_authors:
                continue
            total += 1
            author_counts[author] += 1

            date_str = ((c.get("commit") or {}).get("author") or {}).get("date", "")
            if len(date_str) >= 10:
                daily_counts[date_str[:10]] += 1

    for name, commit_count in repo_commit_count.items():
        try:
            langs = repo_languages(name)
        except HTTPError as e:
            print(f"skip languages for {name}: HTTP {e.code}", file=sys.stderr)
            continue
        for lang, b in langs.items():
            lang_bytes[lang] += b * commit_count

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
    text = patch(text, "CHART_DAILY", render_daily_chart(daily_counts, WINDOW_DAYS))
    text = patch(text, "RANKING", render_ranking_block(ranking, member_set, WINDOW_DAYS))
    text = patch(text, "LANGUAGES", render_languages_block(dict(lang_bytes)))
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
                "daily": dict(daily_counts),
                "languages": dict(lang_bytes),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"commits={total} active_repos={active_repos} "
        f"members={len(members)} languages={len(lang_bytes)}"
    )


if __name__ == "__main__":
    main()
