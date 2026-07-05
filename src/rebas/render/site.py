"""静态站渲染：issue 页（首页/往期）、报道页（专题+速览）、归档页。

版面规则（vault 06 文档 §4 的编辑要求，2026-07 用户评审后调整）：
- 每篇入选选题都有报道页：速览链到自己的短报道（无报道的旧数据回退原文外链）；
- 无图专题用 thread_key 派生的色块兜底；报道页底部放供稿材料列表（原文外链在此）；
- 核查记录只留后端（喂 writer 的可信度标注），不上前端；首页有日期导航与归档。
所有页面平铺在 site/ 根目录，相对链接统一。
"""

from __future__ import annotations

import json
import shutil
from datetime import date as date_cls
from pathlib import Path

import markdown as md_lib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from rebas.config import AppConfig, load_profile, load_sources

_TEMPLATES = Path(__file__).parent / "templates"
_STATIC = Path(__file__).parent / "static"
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(_TEMPLATES),
                       autoescape=select_autoescape(["html", "j2"]))


def _md(text: str) -> str:
    return md_lib.markdown(text or "", extensions=["extra"])


def _hue(key: str) -> int:
    return sum(ord(c) for c in key) % 360


def _weekday(iso: str) -> str:
    y, m, d = map(int, iso.split("-"))
    return _WEEKDAYS[date_cls(y, m, d).weekday()]


def _topic_page_name(issue_date: str, thread_key: str) -> str:
    return f"topic-{issue_date}-{thread_key}.html"


def render_site(conn, conf: AppConfig, issue_date: str) -> dict:
    env = _env()
    out = conf.site_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "static").mkdir(exist_ok=True)
    shutil.copy(_STATIC / "style.css", out / "static" / "style.css")

    source_names = {s.id: s.name for s in load_sources()}
    board_names = {b: load_profile(b).name for b in conf.publish_boards}
    pages = 0

    all_issues = [r["issue_date"] for r in conn.execute(
        "SELECT issue_date FROM issues WHERE status IN ('written','rendered')"
        " ORDER BY issue_date").fetchall()]
    if issue_date not in all_issues:
        all_issues.append(issue_date)
        all_issues.sort()
    idx = all_issues.index(issue_date)
    prev_date = all_issues[idx - 1] if idx > 0 else None
    next_date = all_issues[idx + 1] if idx + 1 < len(all_issues) else None

    # ---------- issue 页 ----------
    boards_ctx = []
    for board in conf.publish_boards:
        topics = conn.execute(
            "SELECT * FROM topics WHERE issue_date=? AND board=? ORDER BY"
            " CASE slot WHEN 'headline' THEN 0 ELSE 1 END, id",
            (issue_date, board)).fetchall()
        features, briefs = [], []
        for t in topics:
            if t["decision"] == "feature":
                a = conn.execute("SELECT * FROM articles WHERE topic_id=?",
                                 (t["id"],)).fetchone()
                if a is None:
                    continue
                images = json.loads(a["image_refs"] or "[]")
                features.append({
                    "title": t["title"], "slot": t["slot"],
                    "card_summary": a["card_summary"],
                    "image": images[0] if images else None,
                    "hue": _hue(t["thread_key"] or t["title"]),
                    "mark": (t["thread_key"] or "r")[0].upper(),
                    "update_of": t["update_of_thread"],
                    "page": _topic_page_name(issue_date, t["thread_key"]),
                })
            else:
                a = conn.execute("SELECT id FROM articles WHERE topic_id=?",
                                 (t["id"],)).fetchone()
                ids = json.loads(t["item_ids"])
                item = conn.execute(
                    "SELECT url, source_id FROM raw_items WHERE id=?", (ids[0],)).fetchone()
                briefs.append({
                    "title": t["title"],
                    "page": _topic_page_name(issue_date, t["thread_key"]) if a else None,
                    "url": item["url"] if item else "#",
                    "source": source_names.get(item["source_id"], item["source_id"]) if item else "",
                    "reason": t["reason"],
                })
        boards_ctx.append({"name": board_names.get(board, board),
                           "features": features, "briefs": briefs})

    issue_html = env.get_template("issue.html.j2").render(
        issue_date=issue_date, weekday=_weekday(issue_date),
        prev_date=prev_date, next_date=next_date, boards=boards_ctx)
    (out / f"issue-{issue_date}.html").write_text(issue_html, encoding="utf-8")
    pages += 1
    if issue_date == all_issues[-1]:
        (out / "index.html").write_text(issue_html, encoding="utf-8")
        pages += 1

    # ---------- 报道页（专题+速览，有 article 才有页面） ----------
    for board in conf.publish_boards:
        topics = conn.execute(
            "SELECT * FROM topics WHERE issue_date=? AND board=?",
            (issue_date, board)).fetchall()
        for t in topics:
            a = conn.execute("SELECT * FROM articles WHERE topic_id=?", (t["id"],)).fetchone()
            if a is None:
                continue
            ids = json.loads(t["item_ids"])
            srcs = conn.execute(
                f"SELECT title, url, source_id FROM raw_items"
                f" WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
            images = json.loads(a["image_refs"] or "[]")
            html = env.get_template("topic.html.j2").render(
                issue_date=issue_date, board_name=board_names.get(board, board),
                topic=t, body_html=_md(a["body_md"]),
                image=images[0] if (images and t["decision"] == "feature") else None,
                sources=[{"title": s["title"], "url": s["url"],
                          "source": source_names.get(s["source_id"], s["source_id"])}
                         for s in srcs])
            (out / _topic_page_name(issue_date, t["thread_key"])).write_text(
                html, encoding="utf-8")
            pages += 1

    # ---------- 归档页 ----------
    archive = []
    for d in reversed(all_issues):
        titles = [r["title"] for r in conn.execute(
            "SELECT title FROM topics WHERE issue_date=? AND decision='feature'"
            " ORDER BY CASE slot WHEN 'headline' THEN 0 ELSE 1 END", (d,)).fetchall()]
        archive.append({"date": d, "feature_titles": titles or ["（无专题）"]})
    (out / "archive.html").write_text(
        env.get_template("archive.html.j2").render(issues=archive), encoding="utf-8")
    pages += 1

    return {"pages": pages, "features": sum(len(b["features"]) for b in boards_ctx),
            "briefs": sum(len(b["briefs"]) for b in boards_ctx)}
