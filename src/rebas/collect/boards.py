"""榜单采集：Hacker News（Algolia）/ Lobsters / GitHub Trending。"""

from __future__ import annotations

import html as html_mod
import json
import re

from rebas.collect.base import (
    KeywordMatcher, canonicalize_url, content_hash, strip_html,
)
from rebas.config import Source
from rebas.models import RawItem

_GH_REPO_RE = re.compile(r'<h2 class="h3 lh-condensed">.*?href="/([^"]+)"', re.S)
_GH_DESC_RE = re.compile(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', re.S)
_GH_LANG_RE = re.compile(r'itemprop="programmingLanguage">\s*([^<]+?)\s*<')
_GH_STARS_RE = re.compile(r"([\d,]+) stars today")


def parse_hn(source: Source, data: bytes, *, matcher: KeywordMatcher,
             **_) -> tuple[list[RawItem], int]:
    """HN Algolia（dict 带 hits）与 Lobsters（list）双格式。prefilter=true 时预筛。"""
    obj = json.loads(data)
    hits = obj.get("hits", []) if isinstance(obj, dict) else obj
    items: list[RawItem] = []
    filtered_out = 0
    for h in hits:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        if isinstance(obj, dict):  # HN Algolia
            url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            signals = {"hn_points": h.get("points"), "hn_comments": h.get("num_comments")}
            published = h.get("created_at")
        else:                      # Lobsters
            url = h.get("url") or h.get("comments_url", "")
            signals = {"lobsters_score": h.get("score"), "lobsters_comments": h.get("comment_count")}
            published = h.get("created_at")
        if not url:
            continue
        if source.prefilter and not matcher.matches(title):
            filtered_out += 1
            continue
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            url=url,
            url_canonical=canonicalize_url(url),
            title=title,
            published_at=published,
            content_hash=content_hash(title),
            signals={k: v for k, v in signals.items() if v is not None},
        ))
    return items, filtered_out


def parse_gh_trending(source: Source, data: bytes, **_) -> tuple[list[RawItem], int]:
    """GitHub Trending 页面解析（无官方 API；结构 2026-07-03 实测稳定）。"""
    blocks = data.decode("utf-8", "ignore").split('<article class="Box-row')[1:]
    items: list[RawItem] = []
    for block in blocks:
        m = _GH_REPO_RE.search(block)
        if not m:
            continue
        repo = m.group(1).strip()
        url = f"https://github.com/{repo}"
        desc_m = _GH_DESC_RE.search(block)
        lang_m = _GH_LANG_RE.search(block)
        stars_m = _GH_STARS_RE.search(block)
        signals = {}
        if stars_m:
            signals["stars_today"] = int(stars_m.group(1).replace(",", ""))
        if lang_m:
            signals["language"] = html_mod.unescape(lang_m.group(1))
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            kind="repo",
            url=url,
            url_canonical=canonicalize_url(url),
            title=repo,
            author=repo.split("/")[0],
            summary=strip_html(desc_m.group(1), limit=500) if desc_m else None,
            content_hash=content_hash(repo),
            signals=signals,
        ))
    return items, 0
