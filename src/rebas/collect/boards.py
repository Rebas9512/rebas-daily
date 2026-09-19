"""榜单采集：Hacker News（Algolia）/ Lobsters / GitHub Trending / TabArena。"""

from __future__ import annotations

import csv
import html as html_mod
import io
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


# TabArena（2026-09-18）：表格机器学习的"活基准"榜。数据不扒 Gradio 页面，取 HF Space
# 仓库里的 website_leaderboard.csv——与站点同一份数字、纯 GET，路径即子集坐标，比扒
# 渲染产物稳得多（站点 tabarena.ai 本身就 302 到这个 Space）。默认子集
# entrants_models/imputation_yes/splits_all/tasks_all/datasets_all = 网站默认视图。
# 注：HF 经 CloudFront 回弱 etag（W/"…"），回传时它按强比较仍给 200——此源不走 304，
# 每轮整份 17KB 重解析，重复行走 dup，无妨。
_TA_LINK_RE = re.compile(r"^\[(.+)\]\((.+)\)$")          # Model 列是 markdown 链接
_TA_VARIANT_RE = re.compile(r"\s*\(([^()]+)\)$")          # 尾括号=变体 default/tuned/…
_TA_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TA_TOP_N = 20              # 与 openrouter Top-20 同口径：榜尾常青树天天入库只是噪音


def _ta_col(fields: list[str], prefix: str) -> str | None:
    """按列名前缀定位——表头带排序箭头 emoji（"Elo [⬆️]"），认死全名等于把解析
    绑在装饰字符上（openrouter 改版教训：按形状/前缀定位，别信固定键名锚点）。"""
    return next((f for f in fields if f.strip().startswith(prefix)), None)


def _ta_num(row: dict, col: str | None):
    if not col:
        return None
    try:
        return float((row.get(col) or "").strip())
    except (TypeError, ValueError):
        return None


def parse_tabarena(source: Source, data: bytes, **_) -> tuple[list[RawItem], int]:
    """TabArena 榜单 CSV：按方法聚合变体（取 Elo 最好的那个）、Elo 降序取前 N。

    身份用方法 slug 而非论文 URL：同一篇论文常挂多个方法（TabPFN-3.5 与
    TabPFN-3.5-Fast 同指 arXiv:2609.17895，TabPFN-2.6 与 RealTabPFN-2.5 同指
    arXiv:2511.08667），拿论文 URL 当 url_canonical 会被唯一约束静默吞掉后来者；
    论文/仓库链接照常放 url 供读者点。榜单语义（kind=repo、published_at=None、
    14 天回榜）与 gh_trending/hf_models/openrouter_rankings 对齐。
    """
    reader = csv.DictReader(io.StringIO(data.decode("utf-8", "ignore")))
    rows = list(reader)
    fields = list(reader.fieldnames or [])
    elo_col = _ta_col(fields, "Elo [")
    if not rows or "Model" not in fields or not elo_col:
        raise RuntimeError("TabArena CSV 缺 Model/Elo 列——榜单导出格式疑似改版")
    cols = {k: _ta_col(fields, p) for k, p in (
        ("score", "Score"), ("rank", "Rank"), ("improv", "Improvability"),
        ("train", "Median Train Time"), ("predict", "Median Predict Time"))}

    best: dict[str, dict] = {}
    for r in rows:
        m = _TA_LINK_RE.match((r.get("Model") or "").strip())
        elo = _ta_num(r, elo_col)
        if not m or elo is None:
            continue
        label, url = m.group(1).strip(), m.group(2).strip()
        vm = _TA_VARIANT_RE.search(label)
        name = _TA_VARIANT_RE.sub("", label).strip()
        slug = _TA_SLUG_RE.sub("-", name.lower()).strip("-")
        if not slug:
            continue
        prev = best.get(slug)
        if prev is None or elo > prev["elo"]:
            best[slug] = {"elo": elo, "name": name, "url": url, "slug": slug,
                          "variant": vm.group(1) if vm else None, "row": r}

    ranked = sorted(best.values(), key=lambda e: e["elo"], reverse=True)[:_TA_TOP_N]
    items: list[RawItem] = []
    for rank, e in enumerate(ranked, 1):
        r = e["row"]
        signals: dict = {"ta_rank": rank, "ta_elo": round(e["elo"])}
        parts = [f"第 {rank} 位", f"Elo {round(e['elo'])}"]
        klass = (r.get("TypeName") or "").strip()
        if klass:
            signals["ta_class"] = klass
            parts.append(klass)
        if e["variant"]:
            signals["ta_variant"] = e["variant"]
            parts.append(f"变体 {e['variant']}")
        for key, col, label, unit in (
                ("ta_score", cols["score"], "得分", ""),
                ("ta_improv_pct", cols["improv"], "距最优", "%"),
                ("ta_train_s1k", cols["train"], "训练", " 秒/千行"),
                ("ta_predict_s1k", cols["predict"], "预测", " 秒/千行")):
            v = _ta_num(r, col)
            if v is not None:
                signals[key] = round(v, 3)
                parts.append(f"{label} {v:g}{unit}")
        commercial = (r.get("Commercial") or "").strip().lower()
        if commercial in ("true", "false"):
            signals["ta_commercial"] = commercial == "true"
            parts.append("可商用" if commercial == "true" else "不可商用")
        host = e["url"].split("/")[2] if "://" in e["url"] else ""
        ident = f"https://tabarena.ai/?model={e['slug']}"
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            kind="repo",
            url=e["url"],                        # 读者点进去=论文/仓库原址
            url_canonical=canonicalize_url(ident),   # 身份=方法，不与同论文的兄弟方法撞
            title=e["name"],
            author=e["url"].split("/")[3] if host == "github.com" else None,
            published_at=None,                   # 榜单窗口语义：走 fetched_at
            summary="TabArena 表格模型榜（入库时快照，最新见信号）：" + "、".join(parts),
            content_hash=content_hash(e["slug"]),
            signals=signals,
        ))
    return items, len(rows) - len(items)
