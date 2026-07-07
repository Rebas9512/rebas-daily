"""提示词模板加载与公共块构造。

模板是 config/prompts/*.md 的 string.Template（$占位符——正文里的 JSON
花括号不受影响），用户可直接编辑模板调整文风与版面规则，不动代码。
"""

from __future__ import annotations

import json
from string import Template

from rebas.config import CONFIG_DIR, Profile

_SIGNAL_KEYS = ("hf_upvotes", "hn_points", "stars_today", "hf_likes",
                "hf_downloads", "lobsters_score",
                "oa_hindex", "oa_inst", "oa_paper_cites",   # OpenAlex 增补（enrich 阶段）
                "venue")                                    # 顶刊来源（主编可见=天然加权）


def render_prompt(name: str, **vars) -> str:
    if name in ("writer", "writer_brief"):  # 文风基调单独维护，注入撰写模板
        vars.setdefault(
            "style_block",
            (CONFIG_DIR / "prompts" / "style.md").read_text(encoding="utf-8").strip())
    tpl = Template((CONFIG_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8"))
    return tpl.substitute(**vars)


def profile_block(profile: Profile) -> str:
    lines = []
    for it in profile.interests:
        kws = "、".join(it.keywords[:6])
        lines.append(f"- {it.name}（权重{it.weight}）：{kws} 等")
    if profile.low_priority:
        lines.append(f"- 降权方向：{'、'.join(profile.low_priority)}")
    return "\n".join(lines)


def reader_block(profile: Profile) -> str:
    lines = []
    if profile.reader_assumed:
        lines.append(f"- 已掌握（不需要解释）：{profile.reader_assumed}")
    if profile.reader_explain:
        lines.append(f"- 需要铺垫：{profile.reader_explain}")
    return "\n".join(lines) or "（未配置——按聪明的技术读者、非本领域专家处理）"


def background_block(background_json: str | None) -> str:
    """背景调查 agent 的产物（经背景审核）转成撰写提示词里的背景材料块。"""
    if not background_json:
        return "（无背景材料）"
    bg = json.loads(background_json)
    lines = []
    if bg.get("context"):
        lines.append(f"领域语境：{bg['context']}")
    if bg.get("follow_up"):
        lines.append(f"往期脉络（本刊此前报道过的事件线）：{bg['follow_up']}")
    for c in bg.get("concepts", []):
        lines.append(f"- {c.get('term')}：{c.get('note')}")
    if bg.get("facts"):
        lines.append("调查补充（编辑部联网检索公开报道整理、经审核的**本篇事实**，"
                     "可作事实使用；首次引用按来源归因，如「据 Reuters 报道」）：")
        for f in bg["facts"]:
            lines.append(f"- {f.get('fact')}（来源：{f.get('source') or '公开报道'}）")
    return "\n".join(lines) or "（无背景材料）"


def images_block(numbered: list[tuple[int, str]], *, is_brief: bool = False) -> str:
    """撰写期图片审选的材料块（2026-07-07）。numbered: [(编号, 原始URL), ...]，
    图片文件已按同一顺序附在本次调用中。空列表 = 本篇无图片材料。"""
    if not numbered:
        return "（本篇无图片材料）"
    lines = [f"- 图{n}: {url}" for n, url, *_ in numbered]
    brief_note = ("速览短文一般留 1~2 张就够，不必正文插图（保留而未插入的图会展示在"
                  "报道页头图区）。" if is_brief else
                  "除头图外，保留的图请尽量都在正文中安置；保留而未插入的图会集中展示在"
                  "头图下方的图组里。")
    return (
        "以下图片已按编号顺序附在本次输入中，**请实际查看图片内容**再做取舍：\n"
        + "\n".join(lines) + "\n\n"
        "你同时是本篇的图片编辑：\n"
        "1. **审选**：只保留信息量高、与本篇内容直接相关、画质过得去的图；"
        "logo/banner/广告图/无关人物照/重复视角，删。宁缺毋滥——全部不留、"
        "走纯文字版式完全可以。\n"
        "2. **排序**：images_keep 按展示顺序给保留图的编号，**第一张是头图**"
        "（最能代表本篇的那张），版式会自动把它放在文首，不要在正文里再插它。\n"
        f"3. **正文插图**：{brief_note}插图方式=在最相关的段落之后独立一行写 "
        "`![一句图注](IMG编号)`（如 `![展览现场的主装置](IMG3)`；图注给读者看，"
        "说画面而不是复述正文，可留空）。没保留的图不要引用。"
    )


def signals_str(signals_json: str | None) -> str:
    sig = json.loads(signals_json or "{}")
    parts = [f"{k}={sig[k]}" for k in _SIGNAL_KEYS if k in sig]
    return " ".join(parts) or "-"


def materials_block(rows, *, per_item_limit: int = 3000,
                    fulltext: dict | None = None) -> str:
    """核查/撰写共用的供稿材料块。rows: raw_items 行。

    fulltext: {item_id: 论文原文}（专题级精读材料，fetch 阶段抓的 arXiv 全文）。
    命中的条目用原文替代摘要，不受 per_item_limit 截断（抓取期已按配置上限截断）。
    """
    ft = fulltext or {}
    blocks = []
    for i, r in enumerate(rows, 1):
        deep = ft.get(r["id"])
        if deep:
            label = "内容（论文原文精读材料——已抓取的全文，细节以此为准）"
            text = deep
        else:
            label = "内容"
            text = (r["extracted_text"] or r["summary"] or "（仅标题，无正文）")[:per_item_limit]
        blocks.append(
            f"[S{i}] 标题: {r['title']}\n来源: {r['source_id']} ({r['url']})\n"
            f"作者: {r['author'] or '未知'}\n{label}:\n{text}"
        )
    return "\n\n".join(blocks)


def check_block(check_notes_json: str | None) -> str:
    """把核查 agent 的 JSON 结果转成撰写提示词里的可读块。"""
    if not check_notes_json:
        return "（无核查数据——按单一信源谨慎措辞）"
    notes = json.loads(check_notes_json)
    lines = []
    for c in notes.get("claims", []):
        lines.append(f"- [{c.get('confidence')}] {c.get('claim')}（{c.get('support')} 个独立信源）")
    if notes.get("notes"):
        lines.append(f"备注：{notes['notes']}")
    return "\n".join(lines) or "（核查未产出论断）"
