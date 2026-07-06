"""rss / gnews_rss 采集器（feedparser），含 Google News 真实 URL 解析。"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse

import feedparser

from rebas import db
from rebas.collect.base import (
    HttpClient, canonicalize_url, content_hash, first_image, parse_date, strip_html,
)
from rebas.config import Source
from rebas.models import RawItem

# 每源每轮最多解析多少个未缓存的 gnews 链接（解析要两次请求，控制节奏，
# 剩余条目下轮继续——feed 里还会在）
GNEWS_RESOLVE_BUDGET = 25

_TS_RE = re.compile(r'data-n-a-ts="([^"]+)"')
_SG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
# batchexecute 响应里内层 JSON 是转义形式（\"garturlres\",\"https://...\"），两种都兼容
_RES_RE = re.compile(r'\\?"garturlres\\?",\\?"(https?://[^"\\]+)')


def resolve_gnews_url(client: HttpClient, link: str) -> str | None:
    """Google News 跳转 URL → 真实文章 URL（batchexecute 接口，2026-07-03 实测可用）。"""
    m = re.search(r"articles/([^?]+)", link)
    if not m:
        return None
    art_id = m.group(1)
    page = client.get(link)
    ts = _TS_RE.search(page.text)
    sg = _SG_RE.search(page.text)
    if not (ts and sg):
        return None
    inner = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
        'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
        f'"{art_id}",{ts.group(1)},"{sg.group(1)}"]'
    )
    resp = client.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        content=urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner]]])}),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    m = _RES_RE.search(resp.text)
    return m.group(1) if m else None


def _entry_image(entry, base_url: str = "") -> str | None:
    """条目配图；相对路径按条目链接补全（Polars 博客等 og/内文图给相对 URL，实际踩坑）。"""
    img = None
    # 两个 media 列表串联遍历（此前 or 短路：media_content 非空但全无 url 时漏 thumbnail）
    for media in [*(entry.get("media_content") or []),
                  *(entry.get("media_thumbnail") or [])]:
        if media.get("url"):
            img = media["url"]
            break
    if not img:
        for enc in entry.get("enclosures", []):
            if str(enc.get("type", "")).startswith("image/") and enc.get("href"):
                img = enc["href"]
                break
    if not img:
        html = ""
        if entry.get("content"):
            html = entry.content[0].get("value", "")
        img = first_image(html or entry.get("summary", ""))
    if img and not img.startswith(("http://", "https://")):
        img = urllib.parse.urljoin(base_url, img) if base_url else None
    return img


def parse_feed(source: Source, data: bytes, *, conn: sqlite3.Connection,
               client: HttpClient, **_) -> tuple[list[RawItem], int]:
    """解析 RSS/Atom feed。返回 (items, 跳过数)。gnews 源顺带解析真实 URL。"""
    parsed = feedparser.parse(data)
    items: list[RawItem] = []
    skipped = 0
    budget = GNEWS_RESOLVE_BUDGET
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        title = strip_html(entry.get("title", "")).strip()
        if not link or not title:
            continue
        author = entry.get("author")

        if source.type == "gnews_rss":
            # 标题常带 " - 媒体名" 后缀，去掉；发布方放 author
            publisher = (entry.get("source") or {}).get("title")
            if publisher and title.endswith(f" - {publisher}"):
                title = title[: -len(publisher) - 3].strip()
            author = author or publisher
            real = db.gnews_cache_get(conn, link)
            if real == "":           # 负缓存：解析失败过，不再耗预算（缓存清理后有远期重试）
                skipped += 1
                continue
            if real is None:
                if budget <= 0:
                    skipped += 1     # 本轮预算用完，下轮再解析
                    continue
                budget -= 1
                try:
                    real = resolve_gnews_url(client, link)
                except OSError:      # 网络/HTTP 错误（URLError 含在内）
                    real = None
                if real is None:
                    db.gnews_cache_put(conn, link, "")   # 负缓存，防预算被永久失败链接饿死
                    skipped += 1     # 解析失败不入库，避免污染去重
                    continue
                db.gnews_cache_put(conn, link, real)
            link = real

        content_html = ""
        if entry.get("content"):
            content_html = entry.content[0].get("value", "")
        summary = strip_html(entry.get("summary", ""), limit=2000) or None
        fulltext = None
        if source.content == "fulltext" and content_html:
            fulltext = strip_html(content_html)

        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            kind=source.kind,
            url=link,
            url_canonical=canonicalize_url(link),
            title=title,
            author=author,
            published_at=parse_date(
                entry.get("published_parsed") or entry.get("updated_parsed")
            ),
            summary=summary,
            extracted_text=fulltext,
            content_hash=content_hash(title),
            image_url=_entry_image(entry, base_url=link),
        ))
    return items, skipped


# ---- nitter_rss：X 时间线的 Nitter 镜像（2026-07-06，慢车道源）----
# 镜像实例（nitter.net）易死易换：条目 URL 一律改写回 x.com——刊物外链与
# 去重键不依赖镜像；配图是实例代理 URL 同样不可靠，直接丢弃。
_NITTER_HOST_RE = re.compile(r"^https?://nitter\.[^/]+", re.I)


def parse_nitter_rss(source: Source, data: bytes, *, conn=None, client=None,
                     **kw) -> tuple[list[RawItem], int]:
    items, extra = parse_feed(source, data, conn=conn, client=client, **kw)
    for it in items:
        it.url = _NITTER_HOST_RE.sub("https://x.com", it.url).removesuffix("#m")
        it.url_canonical = canonicalize_url(it.url)
        it.image_url = None
    return items, extra


# ---- truth_rss：Truth Social 归档站（trumpstruth.org，2026-07-06）----
# feed 标题是占位符（"[No Title] - Post from …"），真实内容在 description。
# 标题换成正文头部，粗筛/主编的候选行才有信息量；纯转发无正文的保持原样
# （粗筛自然低分淘汰）。
def parse_truth_rss(source: Source, data: bytes, *, conn=None, client=None,
                    **kw) -> tuple[list[RawItem], int]:
    items, extra = parse_feed(source, data, conn=conn, client=client, **kw)
    for it in items:
        if it.title.startswith("[No Title]") and it.summary:
            it.title = it.summary[:120]
            it.content_hash = content_hash(it.title)
    return items, extra
