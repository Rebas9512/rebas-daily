"""arXiv 采集：RSS 日更（预筛前置）+ API 按日回填。"""

from __future__ import annotations

import re
import urllib.parse

import feedparser

from rebas.collect.base import (
    HttpClient, KeywordMatcher, canonicalize_url, content_hash, fetch_url,
    parse_date, strip_html,
)
from rebas.config import Source
from rebas.models import RawItem

_ANNOUNCE_RE = re.compile(r"Announce Type:\s*(\w+)")
_ABS_RE = re.compile(r"arxiv\.org/abs/([\w.\-/]+?)(?:v\d+)?$")

CATEGORIES = ["cs.AI", "cs.LG", "cs.CL", "cs.CV", "stat.ML"]
API_URL = "http://export.arxiv.org/api/query"
API_PAGE_SIZE = 200
API_MAX_RESULTS = 1000


def _endpoint_categories(source: Source) -> list[str]:
    """从 rss.arxiv.org/rss/<cat>+<cat> 端点反推分类（回填 API 用）。"""
    tail = source.endpoint.rstrip("/").rsplit("/", 1)[-1]
    cats = [c for c in tail.split("+") if "." in c or c.startswith("q-fin")]
    return cats or CATEGORIES


def arxiv_id_from_url(url: str) -> str | None:
    m = _ABS_RE.search(url.strip())
    return m.group(1) if m else None


def _make_item(source: Source, *, arxiv_id: str, title: str, abstract: str,
               author: str | None, published: str | None,
               categories: list[str]) -> RawItem:
    canonical = f"https://arxiv.org/abs/{arxiv_id}"
    return RawItem(
        source_id=source.id,
        board=source.board,
        kind="paper",
        url=canonical,
        url_canonical=canonicalize_url(canonical),
        title=title,
        author=author,
        published_at=published,
        summary=abstract[:4000] or None,
        content_hash=content_hash(title),
        signals={"arxiv_categories": categories} if categories else {},
    )


def parse_arxiv_rss(source: Source, data: bytes, *, matcher: KeywordMatcher,
                    **_) -> tuple[list[RawItem], int]:
    """解析 rss.arxiv.org 日更 feed。skip replace 类公告；预筛不中不入库。"""
    parsed = feedparser.parse(data)
    items: list[RawItem] = []
    filtered_out = 0
    for entry in parsed.entries:
        raw_summary = entry.get("summary", "")
        m = _ANNOUNCE_RE.search(raw_summary)
        if m and m.group(1) == "replace":       # 旧文重发不要
            continue
        arxiv_id = arxiv_id_from_url(entry.get("link", ""))
        if not arxiv_id:
            continue
        title = strip_html(entry.get("title", ""))
        abstract = strip_html(raw_summary.split("Abstract:", 1)[-1])
        if source.prefilter and not matcher.matches(f"{title}\n{abstract}"):
            filtered_out += 1                    # 预筛前置：不落库（小体量源关 prefilter 直通）
            continue
        items.append(_make_item(
            source,
            arxiv_id=arxiv_id,
            title=title,
            abstract=abstract,
            author=entry.get("author"),
            published=parse_date(entry.get("published_parsed")),
            categories=[t["term"] for t in entry.get("tags", []) if t.get("term")],
        ))
    return items, filtered_out


def backfill_day(source: Source, date: str, matcher: KeywordMatcher,
                 client: HttpClient) -> tuple[list[RawItem], int]:
    """arXiv API 按提交日期回填（调整画像关键词后的后悔药）。date: YYYY-MM-DD"""
    day = date.replace("-", "")
    cat_query = " OR ".join(f"cat:{c}" for c in _endpoint_categories(source))
    query = f"({cat_query}) AND submittedDate:[{day}0000 TO {day}2359]"
    items: list[RawItem] = []
    filtered_out = 0
    start = 0
    while start < API_MAX_RESULTS:
        url = (
            f"{API_URL}?search_query={urllib.parse.quote(query)}"
            f"&start={start}&max_results={API_PAGE_SIZE}&sortBy=submittedDate"
        )
        parsed = feedparser.parse(fetch_url(client, url).data)
        if not parsed.entries:
            break
        for entry in parsed.entries:
            arxiv_id = arxiv_id_from_url(entry.get("id", ""))
            if not arxiv_id:
                continue
            title = strip_html(entry.get("title", ""))
            abstract = strip_html(entry.get("summary", ""))
            if source.prefilter and not matcher.matches(f"{title}\n{abstract}"):
                filtered_out += 1
                continue
            items.append(_make_item(
                source,
                arxiv_id=arxiv_id,
                title=title,
                abstract=abstract,
                author=", ".join(a.get("name", "") for a in entry.get("authors", []))[:300] or None,
                published=parse_date(entry.get("published_parsed")),
                categories=[t["term"] for t in entry.get("tags", []) if t.get("term")],
            ))
        if len(parsed.entries) < API_PAGE_SIZE:
            break
        start += API_PAGE_SIZE
    return items, filtered_out
