"""OpenRouter 采集（2026-08-26）：用量趋势榜 + 新模型上架。

两路数据、两个源类型，条目按 url_canonical（openrouter.ai/<model-id>）天然跨源合并：
- openrouter_rankings：/rankings 页内嵌的 Top-20 用量榜（日 tokens/请求数/环比增速）。
  榜单语义同 gh_trending/hf_models——published_at=None 走 fetched_at 窗口、14 天 revive、
  重复上榜 merge 刷新信号；增速（or_growth_pct）是"趋势事件"的核心信号，交给粗筛/主编判断。
- openrouter_models：官方目录 API（/api/v1/models），created 时间戳过滤出新上架模型
  （含 stealth 预览模型），published_at=created 走正常出刊窗口，永不 revive。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from rebas.collect.base import canonicalize_url, content_hash
from rebas.config import Source
from rebas.models import RawItem

# rankings 页是 Next.js flight 流：数据在 self.__next_f.push([1,"…"]) 的转义字符串里，
# 拼接解码后取 "rankingData":[…]（2026-08-26 实测）。构建产物改版会破——找不到就抛错
# 走 error 路径（admin 可见连败），绝不静默返回空。
_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.S)
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")   # permaslug 带版本日期后缀，剥掉=模型页 slug
_MODELS_LOOKBACK_DAYS = 7                  # 目录只出最近上架的（96h 出刊窗 + 缓冲）


def _fmt_tokens(tokens_b: float) -> str:
    return f"{tokens_b / 1000:.1f}T" if tokens_b >= 1000 else f"{tokens_b:.0f}B"


def parse_openrouter_rankings(source: Source, data: bytes, **_) -> tuple[list[RawItem], int]:
    chunks = _FLIGHT_RE.findall(data.decode("utf-8", "ignore"))
    blob = "".join(c.encode("utf-8", "backslashreplace").decode("unicode_escape", "replace")
                   for c in chunks)
    i = blob.find('"rankingData":[')
    if i < 0:
        raise RuntimeError("rankings 页未找到 rankingData——Next.js 构建产物疑似改版")
    arr, _end = json.JSONDecoder().raw_decode(blob[i + len('"rankingData":'):])

    def _tokens(e) -> int:
        return (e.get("total_completion_tokens") or 0) + (e.get("total_prompt_tokens") or 0)

    # 同一模型的多个带日期版本（deepseek-v4-flash-20260423/-20260731）剥后缀后同 slug：
    # 先聚合——tokens/请求求和=模型真实总用量，增速取主导版本（tokens 最大者）的；
    # 不聚合的话批内 merge 会让旧版本的差名次覆盖新版本信号
    by_slug: dict[str, dict] = {}
    for e in arr:
        if not e.get("model_permaslug"):
            continue
        slug = _DATE_SUFFIX_RE.sub("", e["model_permaslug"])
        agg = by_slug.setdefault(slug, {"tokens": 0, "requests": 0,
                                        "change": None, "_dom": -1})
        agg["tokens"] += _tokens(e)
        agg["requests"] += e.get("count") or 0
        if _tokens(e) > agg["_dom"]:
            agg["_dom"] = _tokens(e)
            agg["change"] = e.get("change")

    # 名次自己按日 tokens 排——页面数组顺序带前端排序状态，不可依赖
    ranked = sorted(by_slug.items(), key=lambda kv: kv[1]["tokens"], reverse=True)
    items: list[RawItem] = []
    for rank, (slug, agg) in enumerate(ranked, 1):
        tokens_b = round(agg["tokens"] / 1e9, 1)
        signals: dict = {"or_rank": rank, "or_tokens_b": tokens_b}
        if agg["requests"]:
            signals["or_requests_m"] = round(agg["requests"] / 1e6, 1)
        parts = [f"第 {rank} 位", f"日 {_fmt_tokens(tokens_b)} tokens"]
        if agg["change"] is not None:
            signals["or_growth_pct"] = round(agg["change"] * 100)
            parts.append(f"较上期 {signals['or_growth_pct']:+d}%")
        url = f"https://openrouter.ai/{slug}"
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            kind="repo",
            url=url,
            url_canonical=canonicalize_url(url),
            title=slug,
            author=slug.split("/")[0] if "/" in slug else None,
            # 榜单条目不设 published_at：与 gh_trending/hf_models 同走 fetched_at 窗口
            published_at=None,
            summary="OpenRouter 用量趋势榜（入库时快照，最新见信号）：" + "、".join(parts),
            content_hash=content_hash(slug),
            signals=signals,
        ))
    return items, 0


def parse_openrouter_models(source: Source, data: bytes, **_) -> tuple[list[RawItem], int]:
    obj = json.loads(data)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_MODELS_LOOKBACK_DAYS)).timestamp()
    items: list[RawItem] = []
    for m in obj.get("data", []):
        mid = m.get("id")
        created = m.get("created") or 0
        if not mid or created < cutoff:
            continue
        signals = {}
        if m.get("context_length"):
            signals["or_context"] = m["context_length"]
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            kind="repo",
            url=f"https://openrouter.ai/{mid}",
            url_canonical=canonicalize_url(f"https://openrouter.ai/{mid}"),
            title=m.get("name") or mid,
            author=mid.split("/")[0] if "/" in mid else None,
            published_at=datetime.fromtimestamp(created, tz=timezone.utc)
                                 .isoformat(timespec="seconds"),
            summary=(m.get("description") or "")[:500] or None,
            content_hash=content_hash(mid),
            signals=signals,
        ))
    return items, 0
