"""JSON 数据导出 + Astro 构建：SQLite → web/data/ → （npm build）→ site/。

前端重构（2026-07-04 Pencil 设计定稿）后渲染层分两段：
- 本模块导出数据契约（JSON），Python 只管数据语义；
- web/（Astro + TS）消费 JSON 产出静态页，替代原 Jinja 渲染（render/site.py 暂留未删）。

渲染语义沿用 vault 06 §4：核查记录不上前端；速览有报道页则内链，
旧数据无报道页回退原文外链；供稿材料列在报道页底部。

数据契约（v1）：
- web/data/site.json          刊名、板块清单（含 EN 眉题）、期次索引
- web/data/issues/{date}.json 单期全量：各板块 headline/features/briefs，正文已转 HTML
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
from datetime import date as date_cls, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import html as html_lib

import latex2mathml.converter as l2m
import markdown as md_lib
import nh3

from rebas.config import PROJECT_ROOT, AppConfig, load_profile, load_sources

WEB_DIR = PROJECT_ROOT / "web"
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 板块眉题的英文名（设计稿 SECTION 01 — ACADEMIC 一行）；未列出的回退大写 id
_BOARD_EN = {
    "academic": "ACADEMIC",
    "repos": "OPEN SOURCE",
    "tech": "TECH",
    "data": "DATA",
    "finance": "BUSINESS",
    "quant": "QUANT",
    "art": "ART & DESIGN",
}
# raw_items.kind → 前端标签
_KIND_LABEL = {"paper": "paper", "article": "news", "repo": "repo"}
# 自动生成的社交卡片/论文首页缩略图（无信息量），不当配图用
_GENERIC_IMAGE_HOSTS = (
    "opengraph.githubassets.com",
    "cdn-thumbnails.huggingface.co",   # HF 论文页缩略图 = PDF 首页截图
)
# 纯论文板块不配图（论文没有编辑意义上的配图；次要信源插图也不该顶到头条）
_NO_IMAGE_BOARDS = {"academic"}


# markdown "extra" 会放行原始 HTML，而前端用 set:html 渲染正文——
# nh3 按白名单消毒（写稿是自家 LLM，但上游标题/摘要可能被转述进正文）
_MATHML_TAGS = {
    "math", "mrow", "mi", "mo", "mn", "msup", "msub", "msubsup", "mfrac",
    "msqrt", "mroot", "mover", "munder", "munderover", "mtext", "mspace",
    "mtable", "mtr", "mtd", "mstyle", "mpadded", "mphantom", "menclose",
    "mfenced", "semantics", "annotation",
}
_ALLOWED_TAGS = {
    "p", "h2", "h3", "h4", "ul", "ol", "li", "blockquote", "a", "code", "pre",
    "strong", "em", "br", "hr", "table", "thead", "tbody", "tr", "th", "td",
} | _MATHML_TAGS
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "math": {"display", "xmlns"},
    "mo": {"stretchy", "fence", "separator", "form", "accent", "movablelimits",
           "largeop", "lspace", "rspace", "minsize", "maxsize", "symmetric"},
    "mi": {"mathvariant"},
    "mn": {"mathvariant"},
    "mtext": {"mathvariant"},
    "mstyle": {"displaystyle", "scriptlevel", "mathvariant"},
    "mspace": {"width", "height", "depth"},
    "mfrac": {"linethickness"},
    "mover": {"accent"},
    "munder": {"accentunder"},
    "menclose": {"notation"},
    "mfenced": {"open", "close", "separators"},
    "mtable": {"columnalign", "rowalign", "columnspacing", "rowspacing"},
    "mtr": {"columnalign"},
    "mtd": {"columnalign", "columnspan", "rowspan"},
    "annotation": {"encoding"},
}

# ---- 公式渲染（构建期 LaTeX → MathML，浏览器原生显示，站点保持零 JS 零外链）----
# AI 写手输出公式的常见形态都接：$$…$$ / \[…\] / ```math 围栏（块级），$…$ / \(…\)（行内）。
# 解析失败降级为代码样式原文，绝不让一篇正文毁掉整次导出。
_CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.S)
_MATH_FENCE_RE = re.compile(r"```math\s*\n(.+?)\n```", re.S)   # 自身是围栏，先于代码保护处理
_DISPLAY_MATH_RE = re.compile(r"\$\$(?P<a>.+?)\$\$|\\\[(?P<b>.+?)\\\]", re.S)
_INLINE_MATH_RE = re.compile(r"\$(?P<a>[^$\n]{1,200}?)\$|\\\((?P<b>[^\n]+?)\\\)")
_CJK_RE = re.compile(r"[一-鿿　-〿＀-￯]")
_MATHISH_RE = re.compile(r"[A-Za-z\\^_={}]")


def _looks_like_math(tex: str) -> bool:
    """行内 $…$ 的防误伤门槛：像数学才转（"$100 million" 这类货币不碰）。"""
    tex = tex.strip()
    return bool(tex) and bool(_MATHISH_RE.search(tex)) and not _CJK_RE.search(tex)


def _tex_to_mathml(tex: str, display: bool) -> str:
    tex = tex.strip()
    try:
        return l2m.convert(tex, display="block" if display else "inline")
    except Exception:  # noqa: BLE001 —— 语法千奇百怪，失败降级不中断导出
        esc = html_lib.escape(tex)
        return (f"<pre><code>{esc}</code></pre>" if display
                else f"<code>{esc}</code>")


def _extract_math(text: str) -> tuple[str, dict[str, str]]:
    """把公式替换成纯字母占位符（markdown 不会碰），返回 (改写文本, 占位符→MathML)。
    代码块/行内代码里的 $ 原样保留（防止 shell/awk 示例被误判）。"""
    rendered: dict[str, str] = {}

    def _token(mathml: str) -> str:
        token = f"REBASMATHTOKEN{len(rendered)}X"
        rendered[token] = mathml
        return token

    text = _MATH_FENCE_RE.sub(
        lambda m: _token(_tex_to_mathml(m.group(1), display=True)), text)

    def _process(segment: str) -> str:
        def _display(m: re.Match) -> str:
            tex = next(g for g in m.groups() if g is not None)
            return _token(_tex_to_mathml(tex, display=True))

        def _inline(m: re.Match) -> str:
            tex = m.group("a") or m.group("b")
            explicit = m.group("b") is not None      # \(…\) 是明确的数学标记
            if not explicit and not _looks_like_math(tex):
                return m.group(0)
            return _token(_tex_to_mathml(tex, display=False))

        segment = _DISPLAY_MATH_RE.sub(_display, segment)
        return _INLINE_MATH_RE.sub(_inline, segment)

    parts, pos = [], 0
    for m in _CODE_SPAN_RE.finditer(text):       # 围栏/行内代码跳过公式抽取
        parts.append(_process(text[pos:m.start()]))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(_process(text[pos:]))
    return "".join(parts), rendered


def _md(text: str) -> str:
    text, math_map = _extract_math(text or "")
    html = md_lib.markdown(text, extensions=["extra"])
    for token, mathml in math_map.items():
        html = html.replace(token, mathml)
    return nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
                     link_rel="noopener")


def _weekday(iso: str) -> str:
    y, m, d = map(int, iso.split("-"))
    return _WEEKDAYS[date_cls(y, m, d).weekday()]


def _topic_page_name(issue_date: str, board: str, thread_key: str) -> str:
    # 文件名含 board：跨板块报道同一事件（thread_key 相同）时防止页面互相覆盖
    return f"topic-{issue_date}-{board}-{thread_key}.html"


def _topic_kind(items: list) -> str:
    """选题类型 = 成员条目 kind 的多数派，平票取首条（主编排序的首条是主材料）。"""
    kinds = [_KIND_LABEL.get(i["kind"], "news") for i in items]
    if not kinds:
        return "news"
    counts = {k: kinds.count(k) for k in kinds}
    best = max(counts.values())
    tied = [k for k, v in counts.items() if v == best]
    return kinds[0] if len(tied) > 1 else tied[0]


def _topic_meta(items: list) -> list[str]:
    """速览行/专题卡右侧的信号元数据（等宽小字），取成员条目聚合值。"""
    sig: dict = {}
    for i in items:
        try:
            s = json.loads(i["signals"] or "{}")
        except (TypeError, ValueError):
            continue
        for k, v in s.items():
            if isinstance(v, (int, float)):
                sig[k] = max(sig.get(k, 0), v)
    meta: list[str] = []
    if sig.get("oa_hindex"):
        meta.append(f"H {int(sig['oa_hindex'])}")
    if sig.get("hf_upvotes", 0) >= 5:
        meta.append(f"HF {int(sig['hf_upvotes'])}")
    if sig.get("stars_today"):
        meta.append(f"{int(sig['stars_today'])} STARS")
    if sig.get("hn_points"):
        meta.append(f"HN {int(sig['hn_points'])}")
    if len(items) > 1:
        meta.append(f"{len(items)} 信源")
    return meta[:3]


def _read_minutes(body_md: str) -> int:
    return max(1, round(len(body_md or "") / 400))


def _topic_image(article, items: list) -> str | None:
    """配图：优先取材阶段的 og:image（image_refs），回退条目采集图；
    过滤自动生成的社交卡片。无图返回 None → 前端渲染无图版式。

    相对路径防御：上游历史数据可能存了相对 URL（Polars 博客实际踩坑），
    条目图按条目链接补全，image_refs 无基准则丢弃。"""
    candidates: list[tuple[str, str | None]] = []
    if article is not None:
        candidates += [(u, None) for u in json.loads(article["image_refs"] or "[]")]
    candidates += [(i["image_url"], i["url"]) for i in items if i["image_url"]]
    for url, base in candidates:
        if not url or any(h in url for h in _GENERIC_IMAGE_HOSTS):
            continue
        if not url.startswith(("http://", "https://")):
            if not base:
                continue
            url = urllib.parse.urljoin(base, url)
        if url.startswith(("http://", "https://")):
            return url
    return None


def export_web(conn, conf: AppConfig, data_dir: Path | None = None) -> dict:
    """全量导出所有已出刊期次（幂等重建，数据量小无需增量）。"""
    data_dir = data_dir or WEB_DIR / "data"
    issues_dir = data_dir / "issues"
    if issues_dir.exists():
        shutil.rmtree(issues_dir)
    issues_dir.mkdir(parents=True)

    source_names = {s.id: s.name for s in load_sources()}
    boards_meta = []
    for b in conf.publish_boards:
        full = load_profile(b).name
        boards_meta.append({
            "id": b,
            "name": full.split("·")[0].strip(),  # "学术 · AI/ML" → "学术"
            "name_full": full,
            "en": _BOARD_EN.get(b, b.upper()),
        })

    # 发布闸门：明日刊白天分批备好（cron 四批模型），但只有 issue_date ≤ 今天的期次
    # 才上站——达拉斯 00:00 的一次零 token render 就是"翻牌"动作
    today = datetime.now(ZoneInfo(conf.timezone)).date().isoformat()
    all_issues = [r["issue_date"] for r in conn.execute(
        "SELECT issue_date FROM issues WHERE status IN ('written','rendered')"
        " AND issue_date <= ? ORDER BY issue_date", (today,)).fetchall()]

    # 保留窗口：最新一期往前 site_keep_days 天内出完整页面，更早的归档只存目。
    # 渲染期策略（数据库不动），调大 site_keep_days 重渲染即可整体找回。
    def _d(iso: str) -> date_cls:
        y, m, dd = map(int, iso.split("-"))
        return date_cls(y, m, dd)

    latest = all_issues[-1] if all_issues else None
    kept = {d for d in all_issues
            if latest and (_d(latest) - _d(d)).days < conf.site_keep_days}

    for no, issue_date in enumerate(all_issues, start=1):
        if issue_date not in kept:
            continue
        boards_ctx = []
        for bm in boards_meta:
            topics = conn.execute(
                "SELECT * FROM topics WHERE issue_date=? AND board=? ORDER BY"
                " CASE slot WHEN 'headline' THEN 0 ELSE 1 END, id",
                (issue_date, bm["id"])).fetchall()
            headline, features, briefs = None, [], []
            for t in topics:
                a = conn.execute("SELECT * FROM articles WHERE topic_id=?",
                                 (t["id"],)).fetchone()
                ids = json.loads(t["item_ids"])
                items = conn.execute(
                    f"SELECT * FROM raw_items WHERE id IN ({','.join('?' * len(ids))})",
                    ids).fetchall() if ids else []
                # 保持 item_ids 顺序（主编排序，首条为主材料）
                items.sort(key=lambda r: ids.index(r["id"]))
                has_body = bool(a and (a["body_md"] or "").strip())
                entry = {
                    "key": t["thread_key"],
                    "title": t["title"],
                    "kind": _topic_kind(items),
                    "slot": t["slot"] or "brief",
                    "is_feature": t["decision"] == "feature",
                    "summary": (a["card_summary"] if a else None) or t["reason"] or "",
                    "meta": _topic_meta(items),
                    "update_of": t["update_of_thread"],
                    # page 只在有正文时给出（与前端 filter(t.page && body_html) 契约对齐，防死链）
                    "page": _topic_page_name(issue_date, bm["id"], t["thread_key"])
                            if has_body else None,
                    "url": items[0]["url"] if items else None,
                    "source": source_names.get(items[0]["source_id"],
                                               items[0]["source_id"]) if items else "",
                    "image": None if bm["id"] in _NO_IMAGE_BOARDS
                             else _topic_image(a, items),
                }
                if has_body:
                    entry["body_html"] = _md(a["body_md"])
                    entry["read_minutes"] = _read_minutes(a["body_md"])
                    entry["sources"] = [{
                        "title": i["title"], "url": i["url"],
                        "source": source_names.get(i["source_id"], i["source_id"]),
                        "kind": _KIND_LABEL.get(i["kind"], "news"),
                    } for i in items]
                if t["decision"] == "feature":
                    if t["slot"] == "headline" and headline is None:
                        headline = entry
                    else:
                        features.append(entry)
                else:
                    briefs.append(entry)
            boards_ctx.append({**bm, "headline": headline,
                               "features": features, "briefs": briefs})
        prev_date = all_issues[no - 2] if no >= 2 else None
        next_date = all_issues[no] if no < len(all_issues) else None
        payload = {
            "date": issue_date, "no": no, "weekday": _weekday(issue_date),
            # 导航只指向窗口内的期次（归档期没有页面，链接会死）
            "prev": prev_date if prev_date in kept else None,
            "next": next_date if next_date in kept else None,
            "boards": boards_ctx,
        }
        (issues_dir / f"{issue_date}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    issue_index = []
    for i, d in enumerate(all_issues, start=1):
        titles = [r["title"] for r in conn.execute(
            "SELECT title FROM topics WHERE issue_date=? AND decision='feature'"
            " ORDER BY CASE slot WHEN 'headline' THEN 0 ELSE 1 END, id", (d,)).fetchall()]
        issue_index.append({"date": d, "no": i, "weekday": _weekday(d),
                            "titles": titles, "archived": d not in kept})
    site_meta = {
        "name": "Rebas Daily",
        "tagline": "PERSONAL AI DAILY — 自动选题 · 核查 · 撰写",
        "boards": boards_meta,
        "issues": issue_index,
        "latest": latest,
    }
    (data_dir / "site.json").write_text(
        json.dumps(site_meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"issues": len(all_issues), "boards": len(boards_meta)}


def build_site(conn, conf: AppConfig) -> dict:
    """导出 JSON 并跑 Astro 构建（web/ → site/）。零 token，替代原 Jinja 渲染。"""
    stats = export_web(conn, conf)
    # cron 的精简 PATH 常找不到 nvm 安装的 npm——显式解析并给出可操作的报错
    npm = os.environ.get("REBAS_NPM") or shutil.which("npm")
    if not npm:
        raise RuntimeError(
            "找不到 npm：cron 环境请在 crontab 里导出 PATH，或设置 REBAS_NPM=/path/to/npm")
    if not (WEB_DIR / "node_modules").exists():
        subprocess.run([npm, "install", "--no-fund", "--no-audit"],
                       cwd=WEB_DIR, check=True)
    r = subprocess.run([npm, "run", "--silent", "build"], cwd=WEB_DIR,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"astro build 失败：\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    for stray in conf.site_dir.glob("content-*.mjs"):  # Astro 内部产物，与站点无关
        stray.unlink()
    pages = len(list(conf.site_dir.glob("*.html")))
    return {**stats, "pages": pages}
