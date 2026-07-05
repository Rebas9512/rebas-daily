"""Hugging Face 采集：Daily Papers（论文榜单）与 Trending Models（模型榜单）。"""

from __future__ import annotations

import json

from rebas.collect.base import canonicalize_url, content_hash
from rebas.config import Source
from rebas.models import RawItem


def parse_papers(source: Source, data: bytes, **_) -> tuple[list[RawItem], int]:
    """HF Daily Papers。canonical 用 arXiv abs 链接——与 arXiv 源天然按论文 ID 合并，
    同一篇论文的 HF 热度信号会 merge 到已有条目上（db.insert_item 的 merged 语义）。"""
    entries = json.loads(data)
    items: list[RawItem] = []
    for e in entries:
        paper = e.get("paper") or {}
        pid = paper.get("id")
        title = (paper.get("title") or "").strip()
        if not pid or not title:
            continue
        canonical = f"https://arxiv.org/abs/{pid}"
        signals = {k: v for k, v in {
            "hf_upvotes": paper.get("upvotes"),
            "hf_comments": e.get("numComments"),
        }.items() if v is not None}
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            kind="paper",
            url=f"https://huggingface.co/papers/{pid}",
            url_canonical=canonicalize_url(canonical),
            title=title,
            author=(e.get("submittedBy") or {}).get("fullname"),
            published_at=e.get("publishedAt"),
            summary=(paper.get("summary") or e.get("summary") or "")[:4000] or None,
            content_hash=content_hash(title),
            signals=signals,
            image_url=e.get("thumbnail"),
        ))
    return items, 0


def parse_models(source: Source, data: bytes, **_) -> tuple[list[RawItem], int]:
    """HF Trending Models。kind=repo；同一模型持续在榜由 revive 窗口去重。"""
    entries = json.loads(data)
    items: list[RawItem] = []
    for m in entries:
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        url = f"https://huggingface.co/{mid}"
        signals = {k: v for k, v in {
            "hf_downloads": m.get("downloads"),
            "hf_likes": m.get("likes"),
            "hf_trending_score": m.get("trendingScore"),
            "pipeline_tag": m.get("pipeline_tag"),
        }.items() if v is not None}
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            kind="repo",
            url=url,
            url_canonical=canonicalize_url(url),
            title=mid,
            author=mid.split("/")[0] if "/" in mid else None,
            # 不设 published_at：createdAt 是模型创建日期，老模型进 trending 榜是常态，
            # 用它会被 96h 出刊窗口永久排除（2026-07-04 审查发现）；
            # 榜单条目与 gh_trending 一致走 fetched_at 36h 窗口
            published_at=None,
            summary=m.get("pipeline_tag"),
            content_hash=content_hash(mid),
            signals=signals,
        ))
    return items, 0
