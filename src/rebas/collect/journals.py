"""顶刊采集器：OpenAlex 期刊目录 + JMLR 卷页解析（官方 feed 已死顶刊的兜底通道）。

设计（2026-07-05）：
- openalex_journal：endpoint 是 OpenAlex works API 静态 URL（sort=publication_date:desc）。
  published_at 用 **created_date（OpenAlex 收录时间）**——期刊 publication_date 是
  期号日期（AoS 双月刊号在被收录时早已超出 96h 窗口），"顶刊新收录"才是日刊的时效语义。
- jmlr_volume：JMLR 不走 Crossref，OpenAlex 收录停在 2021 → 直接解析官网卷页
  （<dl> 列表按发表序号升序，取末尾 N 条即最新）；条目无日期 → fetched_at 36h 兜底窗口。
- 两者都尽量把条目映射到 arXiv 版本（OpenAlex locations / arXiv 标题精确检索）：
  url=arxiv abs 后 enrich 反查与专题精读直接可用，且与在库 arXiv 预印本条目按
  url_canonical 自动合并——顶刊收录信号挂上原条目，等于天然加权。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET

from rebas.collect.base import HttpClient, canonicalize_url, content_hash, parse_date, strip_html
from rebas.config import Source
from rebas.models import RawItem

_ARXIV_LOC_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", re.I)
# JMLR 卷页条目：<dt>标题</dt> ... <i>作者</i> ... href='/papers/vN/id.html'
_JMLR_ENTRY_RE = re.compile(
    r"<dt>(?P<title>.*?)</dt>\s*<dd>.*?<i>(?P<authors>.*?)</i>.*?"
    r"href='(?P<abs>/papers/v\d+/[^']+\.html)'", re.S)
JMLR_TAKE_LAST = 25          # 卷页全量数百条，只看末尾最新的 N 条
_ARXIV_API = "https://export.arxiv.org/api/query"
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _invert_abstract(inv: dict | None) -> str | None:
    """OpenAlex abstract_inverted_index → 摘要文本（词→位置倒排的还原）。"""
    if not inv:
        return None
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    text = " ".join(pos[i] for i in sorted(pos))
    return strip_html(text, limit=2000) or None


def _arxiv_url_from_work(work: dict) -> str | None:
    for loc in work.get("locations") or []:
        for field in ("landing_page_url", "pdf_url"):
            m = _ARXIV_LOC_RE.search(str(loc.get(field) or ""))
            if m:
                return f"https://arxiv.org/abs/{m.group(1)}"
    return None


def parse_openalex_journal(source: Source, data: bytes, *,
                           conn: sqlite3.Connection | None = None,
                           client: HttpClient | None = None, **_) -> tuple[list[RawItem], int]:
    payload = json.loads(data)
    items: list[RawItem] = []
    for work in payload.get("results") or []:
        title = strip_html(work.get("display_name") or "").strip()
        url = _arxiv_url_from_work(work) or (work.get("doi") or "")
        if not title or not url:
            continue
        authors = [a.get("author", {}).get("display_name")
                   for a in (work.get("authorships") or [])]
        authors = [a for a in authors if a]
        signals: dict = {"venue": source.name}
        if work.get("cited_by_count"):
            signals["oa_paper_cites"] = work["cited_by_count"]
        items.append(RawItem(
            source_id=source.id, board=source.board, kind="paper",
            url=url, url_canonical=canonicalize_url(url), title=title,
            author=(authors[0] + (" 等" if len(authors) > 1 else "")) if authors else None,
            published_at=parse_date(work.get("created_date")),
            summary=_invert_abstract(work.get("abstract_inverted_index")),
            content_hash=content_hash(title),
            signals=signals,
        ))
    return items, 0


def _arxiv_lookup_by_title(client: HttpClient, title: str) -> str | None:
    """arXiv 标题精确检索 → abs URL。标题归一比较（去标点/大小写），不确定不认领。"""
    norm = re.sub(r"[^a-z0-9 ]", "", title.lower())
    query = urllib.parse.urlencode({
        "search_query": f'ti:"{re.sub(r"[^A-Za-z0-9 ]", " ", title)}"',
        "max_results": "5",
    })
    resp = client.get(f"{_ARXIV_API}?{query}")
    if resp.status_code != 200:
        return None
    root = ET.fromstring(resp.text)
    for entry in root.findall(f"{_ATOM_NS}entry"):
        cand = re.sub(r"[^a-z0-9 ]", "", (entry.findtext(f"{_ATOM_NS}title") or "").lower())
        if " ".join(cand.split()) != " ".join(norm.split()):
            continue
        for link in entry.findall(f"{_ATOM_NS}id"):
            m = _ARXIV_LOC_RE.search(link.text or "")
            if m:
                return f"https://arxiv.org/abs/{m.group(1)}"
    return None


def parse_jmlr_volume(source: Source, data: bytes, *,
                      conn: sqlite3.Connection | None = None,
                      client: HttpClient | None = None, **_) -> tuple[list[RawItem], int]:
    html = data.decode("utf-8", "replace")
    entries = _JMLR_ENTRY_RE.findall(html)[-JMLR_TAKE_LAST:]
    items: list[RawItem] = []
    for title_html, authors, abs_path in entries:
        title = strip_html(title_html).strip()
        if not title:
            continue
        jmlr_url = urllib.parse.urljoin(source.endpoint, abs_path)
        url = jmlr_url
        # 已在库的条目不再做 arXiv 检索（每轮末尾 N 条大多是旧面孔，省 API 调用）
        known = conn is not None and conn.execute(
            "SELECT 1 FROM raw_items WHERE url_canonical=? OR content_hash=? LIMIT 1",
            (canonicalize_url(jmlr_url), content_hash(title))).fetchone()
        if not known and client is not None:
            try:
                url = _arxiv_lookup_by_title(client, title) or jmlr_url
                time.sleep(1)                     # arXiv API 礼仪
            except Exception:                     # noqa: BLE001 —— 检索失败退回 JMLR 链接
                url = jmlr_url
        author_list = [a.strip() for a in strip_html(authors).split(",") if a.strip()]
        signals: dict = {"venue": source.name}
        if url != jmlr_url:
            signals["jmlr_url"] = jmlr_url
        items.append(RawItem(
            source_id=source.id, board=source.board, kind="paper",
            url=url, url_canonical=canonicalize_url(url), title=title,
            author=(author_list[0] + (" 等" if len(author_list) > 1 else "")) if author_list else None,
            published_at=None,                    # 卷页无日期：fetched_at 36h 兜底窗口
            summary=None,
            content_hash=content_hash(title),
            signals=signals,
        ))
    return items, 0
