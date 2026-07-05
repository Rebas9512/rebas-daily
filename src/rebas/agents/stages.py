"""出刊管线各阶段的实现（编排见 rebas.pipeline）。

每个阶段幂等：已处理的对象自动跳过，失败后重跑从断点继续。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from rebas import db
from rebas.agents.prompts import (
    background_block, check_block, materials_block, profile_block,
    reader_block, render_prompt, signals_str,
)
from rebas.collect.base import first_image, make_client, strip_html, utcnow_iso
from rebas.config import AppConfig, Profile, load_secrets, load_sources
from rebas.llm import LLMBackend, complete_json

_THREAD_KEY_RE = re.compile(r"[^a-z0-9-]+")
FETCH_TEXT_CAP = 20_000
THIN_MATERIAL_CHARS = 500     # 材料总量低于此 → 篇幅封顶
THIN_LENGTH_CAP = 600

_OG_IMAGE_RE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', re.I)

# ---- 专题级论文原文精读（2026-07-05）----
# 专题/头条的论文选题在取材期抓 arXiv HTML 全文给 writer 精读；速览维持摘要。
# 原文只落 data/paper_cache/ 文件（writer 写完即删，prune 兜底清扫），DB 不存全文。
# 注意：enrich 阶段另有一个窄版 _ARXIV_ID_RE（新式 id、OpenAlex 反查用），勿混用
_FULLTEXT_ARXIV_ID_RE = re.compile(
    r"arxiv\.org/(?:abs|html|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z-]+(?:\.[A-Z]{2})?/[0-9]{7})",
    re.I)
_REFS_TAIL_RE = re.compile(r"\n(?:References|REFERENCES|Bibliography)\s*\n")


def _arxiv_id(*urls) -> str | None:
    for u in urls:
        m = _FULLTEXT_ARXIV_ID_RE.search(u or "")
        if m:
            return m.group(1)
    return None


def _strip_references(text: str) -> str:
    """截掉参考文献尾巴——只认出现在正文后半段的 References 标题，防误伤前文提及。"""
    for m in reversed(list(_REFS_TAIL_RE.finditer(text))):
        if m.start() > len(text) * 0.5:
            return text[:m.start()]
    return text


def _fetch_arxiv_fulltext(client, arxiv_id: str, max_chars: int) -> str | None:
    """arXiv 官方 HTML 优先（2024 起 LaTeX 稿多数有），ar5iv 兜底；都无则放弃降级摘要。"""
    import trafilatura  # 懒加载
    for base in ("https://arxiv.org/html/", "https://ar5iv.labs.arxiv.org/html/"):
        try:
            resp = client.get(base + arxiv_id)
            if resp.status_code != 200:
                continue
            text = (trafilatura.extract(resp.text) or "").strip()
            text = _strip_references(text)[:max_chars]
            if len(text) >= 2000:   # 太短说明只抓到占位页/摘要页，不算成功
                return text
        except Exception:  # noqa: BLE001 —— 单篇抓取失败不阻塞出刊，writer 降级用摘要
            continue
    return None


def load_paper_fulltext(conf: AppConfig, rows) -> dict[int, str]:
    """读取选题条目已缓存的论文原文，{item_id: text}。"""
    out = {}
    for r in rows:
        p = conf.paper_cache_dir / f"{r['id']}.txt"
        if p.exists():
            out[r["id"]] = p.read_text(encoding="utf-8")
    return out


def discard_paper_fulltext(conf: AppConfig, item_ids) -> None:
    for iid in item_ids:
        (conf.paper_cache_dir / f"{iid}.txt").unlink(missing_ok=True)


def sweep_paper_cache(conf: AppConfig, days: int = 3) -> int:
    """prune 兜底：清掉滞留缓存（writer 中途失败的残留），按文件 mtime。"""
    if not conf.paper_cache_dir.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for p in conf.paper_cache_dir.glob("*.txt"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def _window_clause(conf: AppConfig) -> tuple[str, list]:
    """出刊取窗：window_hours 定下限；kind=paper 另有沉淀期上限（settle=0 时无效果）。

    沉淀期语义：论文发布满 paper_settle_hours 才入刊——等 OpenAlex 收录（实测 ~2 天）
    与社区热度累积。未满沉淀期的论文保持 new，后续期次自然消费。
    """
    now = datetime.now(timezone.utc)
    pub_cutoff = (now - timedelta(hours=conf.window_hours)).isoformat(timespec="seconds")
    settle_cutoff = (now - timedelta(hours=conf.paper_settle_hours)).isoformat(timespec="seconds")
    fetch_cutoff = (now - timedelta(hours=36)).isoformat(timespec="seconds")
    clause = ("((published_at IS NOT NULL AND published_at >= ?"
              "   AND (kind != 'paper' OR published_at <= ?))"
              " OR (published_at IS NULL AND fetched_at >= ?))")
    return clause, [pub_cutoff, settle_cutoff, fetch_cutoff]


def _source_content_map() -> dict[str, str]:
    return {s.id: s.content for s in load_sources()}


def _depth(row, content_map: dict[str, str]) -> str:
    if row["extracted_text"]:
        return "全文"
    if content_map.get(row["source_id"]) == "fulltext":
        return "全文"
    if len(row["summary"] or "") >= 400 or content_map.get(row["source_id"]) == "abstract":
        return "摘要"
    return "仅标题"


# ---------- Stage 0 信号增补（代码，非 agent） ----------

_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})")
_OA_BATCH = 50                # OpenAlex OR 筛选一次最多 50 个值
_OA_INST_CAP = 40


def _openalex_get(client, path: str, params: dict, api_key: str) -> dict:
    from urllib.parse import urlencode
    q = dict(params)
    q["api_key"] = api_key
    resp = client.get(f"https://api.openalex.org/{path}?{urlencode(q)}")
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAlex {path} HTTP {resp.status_code}")
    return json.loads(resp.content)


def parse_openalex_works(payload: dict) -> dict[str, dict]:
    """works 响应 → {doi小写: {cites, inst, author_ids}}。

    author_ids 取前两位 + 末位作者（资深作者通常在末位），控制第二轮查询量。
    """
    out: dict[str, dict] = {}
    for w in payload.get("results", []):
        doi = (w.get("doi") or "").lower().removeprefix("https://doi.org/")
        if not doi:
            continue
        auths = w.get("authorships") or []
        picked = auths[:2] + (auths[-1:] if len(auths) > 2 else [])
        author_ids = [aid for a in picked
                      if (aid := ((a.get("author") or {}).get("id") or "").rsplit("/", 1)[-1])]
        insts = (auths[0].get("institutions") if auths else None) or []
        out[doi] = {
            "cites": int(w.get("cited_by_count") or 0),
            "inst": (insts[0].get("display_name") or "")[:_OA_INST_CAP] if insts else "",
            "author_ids": author_ids,
        }
    return out


def parse_openalex_authors(payload: dict) -> dict[str, int]:
    """authors 响应 → {author_id: h_index}。"""
    return {a["id"].rsplit("/", 1)[-1]: int((a.get("summary_stats") or {}).get("h_index") or 0)
            for a in payload.get("results", []) if a.get("id")}


def stage_enrich(conn, conf: AppConfig, board: str) -> dict:
    """出刊窗口内 arXiv 条目的外部指标增补（OpenAlex 作者 h 指数 / 机构 / 被引）。

    信号只做加分参考不做门槛；查不到（约 1 天收录时滞）就没有信号，不标记、
    下次 enrich 自动重试。无 API key 或单批网络失败都不阻塞出刊。
    """
    api_key = load_secrets().get("OpenAlexAPI")
    if not api_key:
        return {"skipped": "缺 .secrets/.env 的 OpenAlexAPI"}

    clause, params = _window_clause(conf)
    rows = conn.execute(
        f"SELECT id, url, signals FROM raw_items"
        f" WHERE board=? AND status='new' AND {clause}", [board, *params]).fetchall()
    targets = []                                  # (item_id, arxiv_id, signals)
    for r in rows:
        sig = json.loads(r["signals"] or "{}")
        if "oa_at" in sig:                        # 已命中过，不重查
            continue
        m = _ARXIV_ID_RE.search(r["url"] or "")
        if m:
            targets.append((r["id"], m.group(1), sig))
    if not targets:
        return {"input": 0, "hit": 0, "miss": 0, "errors": 0}

    stats = {"input": len(targets), "hit": 0, "miss": 0, "errors": 0}
    now = utcnow_iso()
    with make_client() as client:
        for i in range(0, len(targets), _OA_BATCH):
            batch = targets[i:i + _OA_BATCH]
            try:
                works = parse_openalex_works(_openalex_get(client, "works", {
                    "filter": "doi:" + "|".join(f"10.48550/arXiv.{ax}" for _, ax, _ in batch),
                    "select": "doi,cited_by_count,authorships",
                    "per-page": str(_OA_BATCH)}, api_key))
                author_ids = sorted({a for w in works.values() for a in w["author_ids"]})
                hindex: dict[str, int] = {}
                for j in range(0, len(author_ids), _OA_BATCH):
                    hindex.update(parse_openalex_authors(_openalex_get(client, "authors", {
                        "filter": "ids.openalex:" + "|".join(author_ids[j:j + _OA_BATCH]),
                        "select": "id,summary_stats",
                        "per-page": str(_OA_BATCH)}, api_key)))
                    time.sleep(0.2)
            except Exception:  # noqa: BLE001 —— 增补失败不阻塞出刊
                stats["errors"] += 1
                continue
            for iid, ax, sig in batch:
                w = works.get(f"10.48550/arxiv.{ax}".lower())
                if not w:
                    stats["miss"] += 1            # 未收录：不写库，下次重试
                    continue
                sig["oa_at"] = now
                hs = [hindex.get(a, 0) for a in w["author_ids"]]
                if hs:
                    sig["oa_hindex"] = max(hs)
                if w["inst"]:
                    sig["oa_inst"] = w["inst"]
                if w["cites"]:
                    sig["oa_paper_cites"] = w["cites"]
                conn.execute("UPDATE raw_items SET signals=? WHERE id=?",
                             (json.dumps(sig, ensure_ascii=False), iid))
                stats["hit"] += 1
            conn.commit()
            time.sleep(0.2)
    return stats


# ---------- Stage 1 粗筛 ----------

def stage_screen(conn, conf: AppConfig, backend: LLMBackend, board: str,
                 profile: Profile, board_name: str) -> dict:
    clause, params = _window_clause(conf)
    rows = conn.execute(
        f"SELECT id, kind, title, summary, source_id, signals FROM raw_items"
        f" WHERE board=? AND status='new' AND {clause}"
        f" ORDER BY fetched_at DESC LIMIT ?",
        [board, *params, conf.screen_cap]).fetchall()
    if not rows:
        return {"input": 0, "screened": 0, "dropped": 0, "unscored": 0}

    pblock = profile_block(profile)
    stats = {"input": len(rows), "screened": 0, "dropped": 0, "unscored": 0}
    for i in range(0, len(rows), conf.screen_batch):
        batch = rows[i:i + conf.screen_batch]
        lines = [
            f'[{r["id"]}] {r["kind"]} | {r["title"][:100]} | '
            f'{(r["summary"] or "")[:150]} | {r["source_id"]} | {signals_str(r["signals"])}'
            for r in batch
        ]
        prompt = render_prompt("screen", board_name=board_name, profile_block=pblock,
                               count=len(batch), items_block="\n".join(lines))
        result = complete_json(backend, prompt, role="screen")
        scores: dict[int, int] = {}
        for s in result.get("scores") or []:   # 字段级容错：单条非法不打穿管线
            try:
                scores[int(s["id"])] = max(0, min(10, int(float(s["score"]))))
            except (KeyError, TypeError, ValueError):
                continue
        for r in batch:
            score = scores.get(r["id"])
            if score is None:
                stats["unscored"] += 1        # 保持 new，下次重筛
                continue
            signals = json.loads(r["signals"] or "{}")
            signals["screen_score"] = score
            status = "screened" if score >= conf.screen_min_score else "dropped"
            conn.execute("UPDATE raw_items SET signals=?, status=? WHERE id=?",
                         (json.dumps(signals, ensure_ascii=False), status, r["id"]))
            stats["screened" if status == "screened" else "dropped"] += 1
        conn.commit()
    return stats


# ---------- Stage 2 主编 ----------

def _normalize_thread_key(key: str) -> str:
    key = (key or "").strip().lower().replace(" ", "-")
    return _THREAD_KEY_RE.sub("", key)[:80] or "untitled"


def stage_editor(conn, conf: AppConfig, backend: LLMBackend, board: str,
                 profile: Profile, board_name: str, issue_date: str) -> dict:
    if conn.execute("SELECT 1 FROM topics WHERE issue_date=? AND board=? LIMIT 1",
                    (issue_date, board)).fetchone():
        return {"skipped": "topics 已存在"}

    clause, params = _window_clause(conf)
    rows = conn.execute(
        f"SELECT id, kind, title, summary, source_id, signals, extracted_text"
        f" FROM raw_items WHERE board=? AND status='screened' AND {clause}"
        f" ORDER BY CAST(json_extract(signals,'$.screen_score') AS INTEGER) DESC"
        f" LIMIT ?", [board, *params, conf.editor_top]).fetchall()
    if not rows:
        return {"skipped": "无入围候选"}

    content_map = _source_content_map()
    candidate_ids = {r["id"] for r in rows}
    lines = []
    for r in rows:
        score = json.loads(r["signals"] or "{}").get("screen_score", "?")
        lines.append(
            f'[{r["id"]}] 粗筛{score}分 {_depth(r, content_map)} {r["kind"]} | '
            f'{r["title"][:110]} | {(r["summary"] or "")[:200]} | '
            f'{r["source_id"]} | {signals_str(r["signals"])}')

    recent = conn.execute(
        "SELECT DISTINCT thread_key, title FROM topics"
        " WHERE board=? AND issue_date >= date(?, '-7 day') AND issue_date < ?",
        (board, issue_date, issue_date)).fetchall()
    recent_block = "\n".join(f"- {r['thread_key']}: {r['title']}" for r in recent) \
        or "（空——近 7 天无出刊记录）"

    prompt = render_prompt(
        "editor", board_name=board_name, issue_date=issue_date, count=len(lines),
        profile_block=profile_block(profile), feature_cap=conf.feature_cap,
        recent_threads_block=recent_block, items_block="\n".join(lines))
    result = complete_json(backend, prompt, role="editor")

    now = utcnow_iso()
    features = briefs = 0
    selected_ids: set[int] = set()
    for t in result.get("topics") or []:
        if not isinstance(t, dict):        # 字段级容错：单条非法跳过
            continue
        item_ids = [i for i in (t.get("item_ids") or []) if i in candidate_ids]
        if not item_ids:
            continue
        decision = t.get("decision")
        if decision not in ("feature", "brief"):
            continue
        if decision == "feature":
            if features >= conf.feature_cap:
                decision = "brief"      # 超配额的降为速览
            else:
                features += 1
        cur = conn.execute(
            "INSERT OR IGNORE INTO topics (issue_date, board, title, thread_key,"
            " item_ids, decision, slot, target_length, needs_image, update_of_thread,"
            " reason, score, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (issue_date, board, (t.get("title") or "")[:200],
             _normalize_thread_key(t.get("thread_key") or ""),
             json.dumps(item_ids), decision,
             t.get("slot") if decision == "feature" else None,
             t.get("target_length") if decision == "feature" else None,
             1 if t.get("needs_image") else 0,
             t.get("update_of_thread"), (t.get("reason") or "")[:300], None, now))
        if cur.rowcount == 0:              # 撞 (issue_date,board,thread_key) 唯一索引
            if decision == "feature":
                features -= 1
            continue
        if decision == "brief":
            briefs += 1
        selected_ids.update(item_ids)

    if features + briefs == 0:
        # 主编空产出（合法 JSON 但零有效选题）：不消费候选、不推进状态，
        # 抛错让编排层记为板块失败，下次续跑重试（防静默空板+毁池）
        conn.rollback()
        raise RuntimeError(
            f"editor 零有效选题（notes: {(result.get('notes') or '')[:80]}）")

    conn.execute(
        f"UPDATE raw_items SET status='selected'"
        f" WHERE id IN ({','.join('?' * len(selected_ids))})", list(selected_ids))
    clause2, params2 = _window_clause(conf)
    conn.execute(
        f"UPDATE raw_items SET status='dropped'"
        f" WHERE board=? AND status='screened' AND {clause2}", [board, *params2])
    # layout 按板块合并（此前整体覆写，只有最后一个板块的 notes 存活）
    row = conn.execute("SELECT layout FROM issues WHERE issue_date=?",
                       (issue_date,)).fetchone()
    layout = json.loads(row["layout"] or "{}") if row else {}
    layout.setdefault("notes", {})
    if not isinstance(layout["notes"], dict):   # 兼容旧格式（字符串）
        layout["notes"] = {"_legacy": layout["notes"]}
    layout["notes"][board] = result.get("notes") or ""
    conn.execute(
        "UPDATE issues SET layout=?, updated_at=? WHERE issue_date=?",
        (json.dumps(layout, ensure_ascii=False), now, issue_date))
    conn.commit()
    return {"features": features, "briefs": briefs,
            "notes": (result.get("notes") or "")[:120]}


# ---------- Stage 3 取材（代码，非 agent） ----------

def stage_fetch(conn, conf: AppConfig, board: str, issue_date: str) -> dict:
    # 全部入选选题（专题+速览）都取材：材料已足够的条目会被跳过，速览基本零成本
    topics = conn.execute(
        "SELECT id, item_ids FROM topics WHERE issue_date=? AND board=?",
        (issue_date, board)).fetchall()
    item_ids = sorted({i for t in topics for i in json.loads(t["item_ids"])})
    if not item_ids:
        return {"fetched": 0, "skipped": 0, "failed": 0}

    import trafilatura  # 懒加载，启动快

    stats = {"fetched": 0, "skipped": 0, "failed": 0}
    with make_client() as client:
        for iid in item_ids:
            r = conn.execute(
                "SELECT id, url, summary, extracted_text, image_url FROM raw_items WHERE id=?",
                (iid,)).fetchone()
            if r["extracted_text"] or len(r["summary"] or "") >= THIN_MATERIAL_CHARS:
                stats["skipped"] += 1
                continue
            try:
                resp = client.get(r["url"])
                html = resp.text
                text = trafilatura.extract(html) or ""
                text = text.strip()[:FETCH_TEXT_CAP]
                image = r["image_url"]
                if not image:
                    m = _OG_IMAGE_RE.search(html)
                    image = m.group(1) if m else (first_image(html) or None)
                if image and not image.startswith(("http://", "https://")):
                    # og:image/内文图可能是相对路径，按页面 URL 补全
                    image = urllib.parse.urljoin(r["url"], image)
                if text:
                    conn.execute(
                        "UPDATE raw_items SET extracted_text=?, image_url=? WHERE id=?",
                        (text, image, iid))
                    stats["fetched"] += 1
                else:
                    stats["failed"] += 1
            except Exception:  # noqa: BLE001 —— 单条取材失败不阻塞出刊
                stats["failed"] += 1
            conn.commit()
            time.sleep(1.0)   # 取材礼貌间隔

        # 专题级论文精读：每个 feature 选题的首个论文条目抓 arXiv 原文进文件缓存
        if conf.paper_fulltext_max_chars > 0:
            feats = conn.execute(
                "SELECT id, item_ids FROM topics WHERE issue_date=? AND board=?"
                " AND decision='feature'", (issue_date, board)).fetchall()
            for t in feats:
                rows = _topic_items(conn, t)
                target = next((r for r in rows if r["kind"] == "paper"), None)
                aid = _arxiv_id(target["url"], target["url_canonical"]) if target else None
                if not aid:
                    continue
                conf.paper_cache_dir.mkdir(parents=True, exist_ok=True)
                path = conf.paper_cache_dir / f"{target['id']}.txt"
                if path.exists():
                    stats["paper_skipped"] = stats.get("paper_skipped", 0) + 1
                    continue
                text = _fetch_arxiv_fulltext(client, aid, conf.paper_fulltext_max_chars)
                if text:
                    path.write_text(text, encoding="utf-8")
                    stats["paper_fulltext"] = stats.get("paper_fulltext", 0) + 1
                else:
                    stats["paper_failed"] = stats.get("paper_failed", 0) + 1
                time.sleep(1.0)
    return stats


# ---------- Stage 4 核查 ----------

def _topic_items(conn, topic_row):
    ids = json.loads(topic_row["item_ids"])
    rows = conn.execute(
        f"SELECT id, title, summary, extracted_text, url, url_canonical, kind,"
        f" source_id, author, image_url"
        f" FROM raw_items WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    rows.sort(key=lambda r: ids.index(r["id"]))   # 保持主编排序（首条为主材料）
    return rows


def stage_checker(conn, conf: AppConfig, backend: LLMBackend, board: str,
                  issue_date: str) -> dict:
    topics = conn.execute(
        "SELECT * FROM topics WHERE issue_date=? AND board=? AND decision='feature'"
        " AND check_notes IS NULL", (issue_date, board)).fetchall()
    done = 0
    for t in topics:
        rows = _topic_items(conn, t)
        prompt = render_prompt("checker", topic_title=t["title"],
                               materials_block=materials_block(rows))
        result = complete_json(backend, prompt, role="checker")
        conn.execute("UPDATE topics SET check_notes=? WHERE id=?",
                     (json.dumps(result, ensure_ascii=False), t["id"]))
        conn.commit()
        done += 1
    stats = {"checked": done}
    stats.update(_review_backgrounds(conn, backend, board, issue_date))
    return stats


def _review_backgrounds(conn, backend: LLMBackend, board: str, issue_date: str) -> dict:
    """背景审核：背调产物的概念解释批量审校（全板块一次调用）。

    verdict 三档：ok 保留 / fix 换成修正文本 / drop 删除——讲错比不讲更糟。
    审校只裁决不新增（新增内容又成了未核查的知识）；漏裁决的概念保守保留。
    幂等：reviewed 标记；follow_up 出自本刊往期原文，不在审核范围。
    """
    pending = []
    for r in conn.execute(
            "SELECT id, title, background FROM topics WHERE issue_date=? AND board=?"
            " AND background IS NOT NULL", (issue_date, board)).fetchall():
        bg = json.loads(r["background"])
        if bg.get("concepts") and not bg.get("reviewed"):
            pending.append((r["id"], r["title"], bg))
    if not pending:
        return {}

    items_block = "\n\n".join(
        f"[T{tid}] {title}\n" + "\n".join(f"- {c['term']}：{c['note']}"
                                          for c in bg["concepts"])
        for tid, title, bg in pending)
    review = complete_json(
        backend, render_prompt("checker_background", items_block=items_block),
        role="checker")
    verdicts = {}
    for entry in review.get("topics", []):
        if not isinstance(entry, dict):
            continue
        for c in entry.get("concepts") or []:
            if isinstance(c, dict) and c.get("term"):
                verdicts[(entry.get("id"), str(c["term"]).strip())] = (
                    str(c.get("verdict") or "").strip(),
                    str(c.get("note") or "").strip())

    fixed = dropped = 0
    for tid, _title, bg in pending:
        kept = []
        for c in bg["concepts"]:
            verdict, note = verdicts.get((tid, c["term"]), ("", ""))
            if verdict == "drop":
                dropped += 1
                continue
            if verdict == "fix" and note:
                c = {"term": c["term"], "note": note}
                fixed += 1
            kept.append(c)
        bg["concepts"] = kept
        bg["reviewed"] = True
        conn.execute("UPDATE topics SET background=? WHERE id=?",
                     (json.dumps(bg, ensure_ascii=False), tid))
    conn.commit()
    return {"bg_reviewed": len(pending), "bg_fixed": fixed, "bg_dropped": dropped}


# ---------- Stage 3.5 背景调查 ----------

RESEARCH_EXCERPT_CHARS = 600   # 背景调查只需识别载重概念，材料节选即可
RESEARCH_EXCERPT_ITEMS = 3
ARCHIVE_DAYS = 30              # 往期报道查阅窗口
ARCHIVE_INDEX_CAP = 200        # 索引条数上限（标题级）
ARCHIVE_READ_CAP = 5           # 单次可索取全文的篇数上限
ARCHIVE_BODY_CHARS = 3000


def _archive_index(conn, board: str, issue_date: str):
    """往期报道索引（同板块、30 天窗口、已有成文的）——背调 agent 的书架。"""
    return conn.execute(
        "SELECT t.id, t.issue_date, t.title, t.thread_key, t.decision"
        " FROM topics t JOIN articles a ON a.topic_id = t.id"
        " WHERE t.board=? AND t.issue_date < ? AND t.issue_date >= date(?, ?)"
        " ORDER BY t.issue_date DESC, t.id LIMIT ?",
        (board, issue_date, issue_date, f"-{ARCHIVE_DAYS} day",
         ARCHIVE_INDEX_CAP)).fetchall()


def _archive_block(rows) -> str:
    if not rows:
        return f"（{ARCHIVE_DAYS} 天内无往期报道）"
    return "\n".join(
        f"[A{r['id']}] {r['issue_date']} {'专题' if r['decision'] == 'feature' else '速览'}"
        f" | {r['title']}（线索 {r['thread_key'] or '-'}）" for r in rows)


def _parse_research(result: dict) -> dict:
    """researcher 产物 → {topic_id: background}，字段级容错。"""
    by_id = {}
    for entry in result.get("topics", []):
        if not isinstance(entry, dict):       # 单条非法跳过
            continue
        concepts = [
            {"term": str(c["term"]).strip(), "note": str(c["note"]).strip()}
            for c in entry.get("concepts") or []
            if isinstance(c, dict) and c.get("term") and c.get("note")
        ]
        by_id[entry.get("id")] = {
            "context": str(entry.get("context") or "").strip(),
            "concepts": concepts[:4],
            "follow_up": str(entry.get("follow_up") or "").strip(),
        }
    return by_id


def stage_research(conn, conf: AppConfig, backend: LLMBackend, board: str,
                   profile: Profile, board_name: str, issue_date: str) -> dict:
    """为选题准备背景材料：教科书级概念解释 + 往期报道衔接（follow_up）。
    产物随后与供稿论断一并进核查（stage_checker 的背景审核），再交撰写。

    - 板块级开关：profile 无 [reader] 段 = 该板块读者不需要（商业/艺术），整体跳过；
    - 全板块一次批量调用（screen 同款），agent 对不需要背景的选题自甄别返回空列表；
    - 往期查阅两轮协议：第一轮给 30 天标题索引，agent 需要读全文时返回
      need_articles，第二轮附全文再产出最终背景（连续报道的 follow-up 不凭标题猜）；
    - 幂等：background 非 NULL 或已有报道的选题跳过；空产物也落库标记已处理。
    """
    if not profile.reader_assumed and not profile.reader_explain:
        return {"skipped": "板块未配置 [reader] 读者画像"}
    topics = conn.execute(
        "SELECT t.* FROM topics t LEFT JOIN articles a ON a.topic_id = t.id"
        " WHERE t.issue_date=? AND t.board=? AND t.background IS NULL AND a.id IS NULL",
        (issue_date, board)).fetchall()
    if not topics:
        return {"skipped": "无待调查选题"}

    blocks = []
    for t in topics:
        rows = _topic_items(conn, t)[:RESEARCH_EXCERPT_ITEMS]
        excerpts = "\n".join(
            f"  - {r['title']}：{(r['extracted_text'] or r['summary'] or '（仅标题）')[:RESEARCH_EXCERPT_CHARS]}"
            for r in rows)
        blocks.append(f"[T{t['id']}] {t['title']}\n"
                      f"入选理由: {t['reason'] or '（无）'}\n材料节选:\n{excerpts}")
    topics_block = "\n\n".join(blocks)
    archive = _archive_index(conn, board, issue_date)

    prompt = render_prompt(
        "researcher", board_name=board_name, reader_block=reader_block(profile),
        archive_days=ARCHIVE_DAYS, archive_block=_archive_block(archive),
        topics_block=topics_block)
    result = complete_json(backend, prompt, role="researcher")

    # 第二轮：agent 索取往期全文（只认索引里出现过的 id，封顶 ARCHIVE_READ_CAP）
    valid_ids = {r["id"] for r in archive}
    need = [i for i in result.get("need_articles") or []
            if isinstance(i, int) and i in valid_ids][:ARCHIVE_READ_CAP]
    read = 0
    if need:
        art_rows = conn.execute(
            f"SELECT t.id, t.issue_date, t.title, a.card_summary, a.body_md"
            f" FROM topics t JOIN articles a ON a.topic_id = t.id"
            f" WHERE t.id IN ({','.join('?' * len(need))})", need).fetchall()
        articles_block = "\n\n".join(
            f"[A{r['id']}] {r['issue_date']} | {r['title']}\n"
            f"卡片：{r['card_summary']}\n正文：\n{r['body_md'][:ARCHIVE_BODY_CHARS]}"
            for r in art_rows)
        read = len(art_rows)
        prompt2 = render_prompt(
            "researcher_articles", board_name=board_name,
            reader_block=reader_block(profile), archive_articles_block=articles_block,
            topics_block=topics_block)
        result = complete_json(backend, prompt2, role="researcher")

    by_id = _parse_research(result)
    with_bg = 0
    for t in topics:  # 未覆盖的选题也落空产物，幂等守卫才认账
        bg = by_id.get(t["id"], {"context": "", "concepts": [], "follow_up": ""})
        if bg["context"] or bg["concepts"] or bg["follow_up"]:
            with_bg += 1
        conn.execute("UPDATE topics SET background=? WHERE id=?",
                     (json.dumps(bg, ensure_ascii=False), t["id"]))
    conn.commit()
    return {"researched": len(topics), "with_background": with_bg,
            "archive_read": read}


# ---------- Stage 5 撰写 ----------

def stage_writer(conn, conf: AppConfig, backend: LLMBackend, board: str,
                 board_name: str, issue_date: str) -> dict:
    """每篇入选选题（专题+速览）都产出报道：专题长写，速览按 brief_length 短写。"""
    topics = conn.execute(
        "SELECT t.* FROM topics t"
        " LEFT JOIN articles a ON a.topic_id = t.id"
        " WHERE t.issue_date=? AND t.board=? AND a.id IS NULL",
        (issue_date, board)).fetchall()
    written = 0
    for t in topics:
        rows = _topic_items(conn, t)
        # 专题级论文原文（fetch 阶段缓存的精读材料）；速览不精读维持摘要
        fulltext = (load_paper_fulltext(conf, rows)
                    if t["decision"] != "brief" and conf.paper_fulltext_max_chars > 0
                    else {})
        material_total = sum(
            len(fulltext.get(r["id"]) or r["extracted_text"] or r["summary"] or "")
            for r in rows)
        t0 = time.time()
        if t["decision"] == "brief":
            prompt = render_prompt(
                "writer_brief", board_name=board_name, topic_title=t["title"],
                reason=t["reason"] or "（主编未附理由）",
                target_length=conf.brief_length,
                background_block=background_block(t["background"]),
                materials_block=materials_block(rows, per_item_limit=4000))
        else:
            target = t["target_length"] or 1000
            if material_total < THIN_MATERIAL_CHARS:
                target = min(target, THIN_LENGTH_CAP)
            prompt = render_prompt(
                "writer", board_name=board_name, topic_title=t["title"],
                reason=t["reason"] or "（主编未附理由）", target_length=target,
                check_block=check_block(t["check_notes"]),
                background_block=background_block(t["background"]),
                materials_block=materials_block(rows, per_item_limit=6000,
                                                fulltext=fulltext))
        result = complete_json(backend, prompt, role="writer")
        conn.execute(
            "INSERT INTO articles (topic_id, card_summary, body_md, credibility_notes,"
            " image_refs, model_meta, created_at) VALUES (?,?,?,?,?,?,?)",
            (t["id"], strip_html(result.get("card_summary", ""))[:200],
             result.get("body_md", ""), t["check_notes"],
             json.dumps([r["image_url"] for r in rows if r["image_url"]][:3]),
             json.dumps({"role": "writer", "elapsed_s": round(time.time() - t0, 1),
                         "material_chars": material_total}),
             utcnow_iso()))
        conn.commit()
        if fulltext:
            # 原文用完即删（省磁盘；断点续跑安全——写失败时文件保留供重试）
            discard_paper_fulltext(conf, fulltext.keys())
        written += 1
    return {"written": written}
