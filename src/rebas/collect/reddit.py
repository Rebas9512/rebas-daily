"""reddit_rss 采集器：Reddit 公开 RSS 通道（无凭证，2026-07-06 双端实测可用）。

官方 Data API 审批未下时的过渡通道，也是慢车道（pace_seconds）的首个用户：
无凭证预算约 1 请求/分钟/IP（x-ratelimit 实测），连发第 2 个即 429——
源必须标 pace_seconds，由调度器串行慢抓（rebas collect --paced 专用 cron）。

条目语义：
- 链接帖 → url 用外链（与 HN/HF/博客同题条目自然合并去重，Reddit 当发现层）；
- 自文帖/图帖 → url 用 Reddit 讨论页，selftext 进 summary（讨论页 VPS 上 403，
  fetch 阶段抓不到正文——薄材料自文帖由背调"新闻调查补充"兜底）。
"""

from __future__ import annotations

import re
import urllib.parse

import feedparser

from rebas.collect.base import canonicalize_url, content_hash, parse_date, strip_html
from rebas.config import Source
from rebas.models import RawItem

# content 里的 [link] 锚点：链接帖指外站，自文帖指自身讨论页，图帖指 redd.it 媒体
_LINK_RE = re.compile(r'<a href="([^"]+)">\s*\[link\]', re.I)
# selftext 块（Reddit 把 markdown 渲染包在 <div class="md"> 里）
_MD_RE = re.compile(r'<div class="md">(.*?)</div>', re.S)
# Reddit 自有域与媒体域：这些不算"外链"（媒体帖仍以讨论页为条目 URL）
_REDDIT_HOSTS = re.compile(
    r"(?:^|\.)(?:reddit\.com|redd\.it|redditmedia\.com|redditstatic\.com)$", re.I)


def _external_link(content_html: str) -> str | None:
    m = _LINK_RE.search(content_html)
    if not m:
        return None
    href = m.group(1)
    host = urllib.parse.urlsplit(href).netloc.lower()
    if not host or _REDDIT_HOSTS.search(host):
        return None
    return href


def parse_reddit_rss(source: Source, data: bytes, **_) -> tuple[list[RawItem], int]:
    """解析 subreddit 的 top.rss。返回 (items, 0)——榜单自带质量信号，不做关键词预筛。"""
    parsed = feedparser.parse(data)
    items: list[RawItem] = []
    for entry in parsed.entries:
        permalink = (entry.get("link") or "").strip()
        title = strip_html(entry.get("title", "")).strip()
        if not permalink or not title:
            continue
        content_html = entry.content[0].get("value", "") if entry.get("content") else ""
        url = _external_link(content_html) or permalink
        md = _MD_RE.search(content_html)
        summary = strip_html(md.group(1), limit=2000) if md else None
        image = None
        for media in entry.get("media_thumbnail") or []:
            if media.get("url"):
                image = media["url"]
                break
        author = (entry.get("author") or "").removeprefix("/u/") or None
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            kind=source.kind,
            url=url,
            url_canonical=canonicalize_url(url),
            title=title,
            author=author,
            published_at=parse_date(
                entry.get("published_parsed") or entry.get("updated_parsed")),
            summary=summary,
            content_hash=content_hash(title),
            image_url=image,
        ))
    return items, 0
