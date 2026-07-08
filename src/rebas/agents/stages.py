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
    background_block, check_block, images_block, materials_block, profile_block,
    reader_block, render_prompt, signals_str,
)
from rebas.collect.base import (
    all_images, canonicalize_url, content_hash, first_image, make_client,
    strip_html, utcnow_iso,
)
from rebas.config import AppConfig, Profile, load_secrets, load_sources, pooled_source_groups
from rebas.llm import LLMBackend, LLMError, complete_json
from rebas.models import RawItem

_THREAD_KEY_RE = re.compile(r"[^a-z0-9-]+")
FETCH_TEXT_CAP = 20_000
THIN_MATERIAL_CHARS = 500     # 材料总量低于此 → 篇幅封顶
THIN_LENGTH_CAP = 600
PAPER_FACTS_THIN_CHARS = 2000  # 论文调查门槛：原文拿不到且材料不足摘要级厚度才联网补

_OG_IMAGE_RE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', re.I)

# ---- 论文原文精读（2026-07-05；2026-07-06 扩到速览）----
# 论文类选题在取材期抓 arXiv HTML 全文给 writer：专题精读（大上限、篇幅放宽），
# 速览也精读（小上限、篇幅仍短——摘要太薄读不出信息增量）。缓存按较大上限抓一次两用。
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


def _first_arxiv_item(rows):
    """选题里首个带 arXiv id 的论文条目 →(row, id)。期刊条目可能是抓不到原文的 DOI
    链接，同选题里的 arXiv 版不该被它挡住，故取首个"带 id"的。"""
    for r in rows:
        if r["kind"] == "paper":
            a = _arxiv_id(r["url"], r["url_canonical"])
            if a:
                return r, a
    return None, None


def _paper_cache_item(rows):
    """本选题精读缓存归属的条目（fetch 抓原文、writer 读/写完清缓存都按它，同一口径，
    共享条目不越界读删）：优先选题内自带 arXiv id 的论文条目；否则退到首个论文条目
    （顶刊 DOI，靠同题预印本兜底解析 arXiv id）。返回 row 或 None。"""
    target, _ = _first_arxiv_item(rows)
    if target is not None:
        return target
    return next((r for r in rows if r["kind"] == "paper"), None)


def _resolve_fulltext_arxiv_id(conn, item) -> str | None:
    """条目的 arXiv id：自带则直取；否则按标题在库里找**同题** arXiv 预印本兜底
    （顶刊 Nature/Science/JASA/AoS 是 DOI 论文抓不到原文，但同一篇常有 arXiv 预印本被
    独立采进库——精确同题=同一篇，无误配风险；短标题易撞不兜）。"""
    aid = _arxiv_id(item["url"], item["url_canonical"])
    if aid:
        return aid
    title = (item["title"] or "").strip()
    if len(title) < 12:
        return None
    for row in conn.execute(
            "SELECT url, url_canonical FROM raw_items"
            " WHERE title=? AND id!=? AND kind='paper'", (title, item["id"])):
        a = _arxiv_id(row["url"], row["url_canonical"])
        if a:
            return a
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


# 顶刊池源分组（出刊扩窗/清扫豁免/瘦身豁免共用），实现挪到 config 供采集层复用
_pooled_source_groups = pooled_source_groups


def _window_clause(conf: AppConfig, include_pool: bool = True,
                   pool_groups: dict[int, list[str]] | None = None) -> tuple[str, list]:
    """出刊取窗：window_hours 定下限；kind=paper 另有沉淀期上限（settle=0 时无效果）。

    沉淀期语义：论文发布满 paper_settle_hours 才入刊——等 OpenAlex 收录（实测 ~2 天）
    与社区热度累积。未满沉淀期的论文保持 new，后续期次自然消费。

    顶刊池扩窗（include_pool，2026-07-05）：pool_days>0 源的条目在 N 天池窗内
    始终可入候选，且不受沉淀期约束（期刊见刊即"已沉淀"，收录数据已随采集带入）。
    主编清扫处用 include_pool=False + 源排除，池内落选候选跨期保留。
    """
    now = datetime.now(timezone.utc)
    pub_cutoff = (now - timedelta(hours=conf.window_hours)).isoformat(timespec="seconds")
    settle_cutoff = (now - timedelta(hours=conf.paper_settle_hours)).isoformat(timespec="seconds")
    fetch_cutoff = (now - timedelta(hours=36)).isoformat(timespec="seconds")
    branches = [("((published_at IS NOT NULL AND published_at >= ?"
                 "   AND (kind != 'paper' OR published_at <= ?))"
                 " OR (published_at IS NULL AND fetched_at >= ?))")]
    params: list = [pub_cutoff, settle_cutoff, fetch_cutoff]
    if include_pool:
        groups = _pooled_source_groups() if pool_groups is None else pool_groups
        for days, ids in sorted(groups.items()):
            pool_cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
            ph = ",".join("?" * len(ids))
            branches.append(f"(source_id IN ({ph})"
                            f" AND (published_at >= ?"
                            f"      OR (published_at IS NULL AND fetched_at >= ?)))")
            params += [*ids, pool_cutoff, pool_cutoff]
    if len(branches) == 1:
        return branches[0], params
    return "(" + " OR ".join(branches) + ")", params


def _source_content_map() -> dict[str, str]:
    return {s.id: s.content for s in load_sources()}


def _depth(row, content_map: dict[str, str], conn=None) -> str:
    if row["extracted_text"]:
        return "全文"
    if content_map.get(row["source_id"]) == "fulltext":
        return "全文"
    # 论文在取材期会抓 arXiv 原文精读：自带 arXiv id 或库内有同题预印本的，预计能拿到全文
    # ——让主编按"全文档次"给篇幅/立专题，而非被源声明的摘要/标题档次误压（如 JMLR feed
    # 仅标题却必得全文精读）。仅编排期需要（传 conn），采集/粗筛期不判。
    if conn is not None and row["kind"] == "paper" and _resolve_fulltext_arxiv_id(conn, row):
        return "全文·精读"
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
        f"SELECT id, url, url_canonical, signals FROM raw_items"
        f" WHERE board=? AND status='new' AND {clause}", [board, *params]).fetchall()
    targets = []                                  # (item_id, arxiv_id, signals)
    for r in rows:
        sig = json.loads(r["signals"] or "{}")
        if "oa_at" in sig:                        # 已命中过，不重查
            continue
        # canonical 兜底：HF papers/期刊映射条目的 url 是落地页，arXiv 链接在 canonical
        m = (_ARXIV_ID_RE.search(r["url"] or "")
             or _ARXIV_ID_RE.search(r["url_canonical"] or ""))
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
                 profile: Profile, board_name: str, issue_date: str,
                 refill: bool = False) -> dict:
    """常规选题 + 板块栏目。栏目（经典鉴赏，classic_board 配置）在常规选题之后跑：
    不依赖候选池（淡日也出栏目）、自身幂等（本期已有即跳过）、失败不拖累常规选题
    （partial 批次与补充轮会自然重试）。"""
    stats = _stage_editor_regular(conn, conf, backend, board, profile, board_name,
                                  issue_date, refill=refill)
    if board == conf.classic_board:
        try:
            stats = {**stats, **_nominate_classic(conn, conf, backend, board,
                                                  issue_date)}
        except Exception as e:  # noqa: BLE001 —— 栏目级隔离，常规选题成果不陪葬
            conn.rollback()
            stats = {**stats, "classic": f"失败（下一批自愈重试）: {type(e).__name__}: {e}"}
    return stats


def _stage_editor_regular(conn, conf: AppConfig, backend: LLMBackend, board: str,
                          profile: Profile, board_name: str, issue_date: str,
                          refill: bool = False) -> dict:
    """常规轮：板块无选题时做当日选题。补充轮（refill=True，收尾批用）：
    板块已有选题但少于 refill_min_topics 时，用白天新采集的候选补选不重复的新选题——
    给凌晨备刊时候选太薄的板块一个当日翻盘机会；选题已足则不动。"""
    existing = conn.execute(
        "SELECT thread_key, title, decision, slot FROM topics"
        " WHERE issue_date=? AND board=? ORDER BY id", (issue_date, board)).fetchall()
    if existing:
        if not refill:
            return {"skipped": "topics 已存在"}
        if not conf.refill_min_topics or len(existing) >= conf.refill_min_topics:
            return {"skipped": f"补充轮：已有 {len(existing)} 题，选题充足不补"}
    supplement = bool(existing)

    clause, params = _window_clause(conf)
    rows = conn.execute(
        f"SELECT id, kind, title, summary, source_id, signals, extracted_text,"
        f" url, url_canonical"
        f" FROM raw_items WHERE board=? AND status='screened' AND {clause}"
        f" ORDER BY CAST(json_extract(signals,'$.screen_score') AS INTEGER) DESC,"
        f"          fetched_at DESC"    # 同分新鲜优先：防顶刊池陈货挤占 editor_top 坑位
        f" LIMIT ?", [board, *params, conf.editor_top]).fetchall()
    if not rows:
        return {"skipped": "无入围候选"}

    content_map = _source_content_map()
    candidate_ids = {r["id"] for r in rows}
    lines = []
    for r in rows:
        score = json.loads(r["signals"] or "{}").get("screen_score", "?")
        lines.append(
            f'[{r["id"]}] 粗筛{score}分 {_depth(r, content_map, conn)} {r["kind"]} | '
            f'{r["title"][:110]} | {(r["summary"] or "")[:200]} | '
            f'{r["source_id"]} | {signals_str(r["signals"])}')

    recent = conn.execute(
        "SELECT DISTINCT thread_key, title FROM topics"
        " WHERE board=? AND issue_date >= date(?, '-7 day') AND issue_date < ?",
        (board, issue_date, issue_date)).fetchall()
    recent_block = "\n".join(f"- {r['thread_key']}: {r['title']}" for r in recent) \
        or "（空——近 7 天无出刊记录）"

    if supplement:
        existing_block = (
            "本轮是【补充轮】：本期该板块已定稿下列选题（可能已成文），白天新采集的候选"
            "补进来了。你只负责挑出与已有事件线**不重复**的新增选题；饱和度自己判断——"
            "没有值得补的就返回空 topics，绝不为凑数硬选。已有头条时不要再给 headline。\n"
            + "\n".join(f"- [{r['decision']}] {r['thread_key']}: {r['title']}"
                        for r in existing))
    else:
        existing_block = "（本期该板块尚无选题——常规选题轮）"

    prompt = render_prompt(
        "editor", board_name=board_name, issue_date=issue_date, count=len(lines),
        profile_block=profile_block(profile), feature_cap=conf.feature_cap,
        existing_block=existing_block,
        recent_threads_block=recent_block, items_block="\n".join(lines))
    result = complete_json(backend, prompt, role="editor")

    now = utcnow_iso()
    feat_total = sum(1 for r in existing if r["decision"] == "feature")
    has_headline = any(r["slot"] == "headline" for r in existing)
    new_feat = new_brief = 0
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
            if feat_total >= conf.feature_cap:
                decision = "brief"      # 超配额的降为速览（补充轮连同已有专题一起计数）
            else:
                feat_total += 1
        slot = t.get("slot") if decision == "feature" else None
        if slot == "headline" and has_headline:
            slot = "regular"            # 补充轮不夺已有头条
        cur = conn.execute(
            "INSERT OR IGNORE INTO topics (issue_date, board, title, thread_key,"
            " item_ids, decision, slot, target_length, needs_image, update_of_thread,"
            " reason, score, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (issue_date, board, (t.get("title") or "")[:200],
             _normalize_thread_key(t.get("thread_key") or ""),
             json.dumps(item_ids), decision, slot,
             t.get("target_length") if decision == "feature" else None,
             1 if t.get("needs_image") else 0,
             t.get("update_of_thread"), (t.get("reason") or "")[:300], None, now))
        if cur.rowcount == 0:              # 撞 (issue_date,board,thread_key) 唯一索引
            if decision == "feature":
                feat_total -= 1
            continue
        if decision == "feature":
            new_feat += 1
            if slot == "headline":
                has_headline = True
        else:
            new_brief += 1
        selected_ids.update(item_ids)

    features, briefs = new_feat, new_brief
    if features + briefs == 0:
        conn.rollback()
        if supplement:
            # 补充轮零新增是合法结论（没有值得补的）：不消费候选（screened 留给明天），
            # 不算失败
            return {"refill": "无值得补充的新选题",
                    "notes": (result.get("notes") or "")[:120]}
        # 常规轮空产出（合法 JSON 但零有效选题）：不消费候选、不推进状态，
        # 抛错让编排层记为板块失败，下次续跑重试（防静默空板+毁池）
        raise RuntimeError(
            f"editor 零有效选题（notes: {(result.get('notes') or '')[:80]}）")

    conn.execute(
        f"UPDATE raw_items SET status='selected'"
        f" WHERE id IN ({','.join('?' * len(selected_ids))})", list(selected_ids))
    # 落选清扫：顶刊池源豁免（池内候选跨期保留，随池窗自然过期）
    clause2, params2 = _window_clause(conf, include_pool=False)
    pooled_ids = [sid for ids in _pooled_source_groups().values() for sid in ids]
    pool_excl = (f" AND source_id NOT IN ({','.join('?' * len(pooled_ids))})"
                 if pooled_ids else "")
    conn.execute(
        f"UPDATE raw_items SET status='dropped'"
        f" WHERE board=? AND status='screened' AND {clause2}{pool_excl}",
        [board, *params2, *pooled_ids])
    # layout 按板块合并（此前整体覆写，只有最后一个板块的 notes 存活）
    row = conn.execute("SELECT layout FROM issues WHERE issue_date=?",
                       (issue_date,)).fetchone()
    layout = json.loads(row["layout"] or "{}") if row else {}
    layout.setdefault("notes", {})
    if not isinstance(layout["notes"], dict):   # 兼容旧格式（字符串）
        layout["notes"] = {"_legacy": layout["notes"]}
    note = result.get("notes") or ""
    if supplement and layout["notes"].get(board):
        note = f"{layout['notes'][board]} ｜ 补充轮：{note}"
    layout["notes"][board] = note
    conn.execute(
        "UPDATE issues SET layout=?, updated_at=? WHERE issue_date=?",
        (json.dumps(layout, ensure_ascii=False), now, issue_date))
    conn.commit()
    stats = {"features": features, "briefs": briefs,
             "notes": (result.get("notes") or "")[:120]}
    if supplement:
        stats["refill"] = f"补充 {features} 专题 {briefs} 速览"
    return stats


# ---------- Stage 2.5 经典鉴赏栏目（2026-07-07，主编提名 + 图片闸门） ----------

CLASSIC_ATTEMPTS = 3             # 提名重试上限：每次失败=这件作品拿不到可用图，换一件
CLASSIC_IMG_MIN_BYTES = 10_000   # 名作配图最低体量（过滤图标/占位图）
CLASSIC_TARGET_LENGTH = 1200
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _classic_day_rule(issue_date: str) -> str:
    from datetime import date as date_cls
    y, m, d = map(int, issue_date.split("-"))
    wd = date_cls(y, m, d).weekday()
    mode = "油画日（必须是油画作品）" if wd < 5 else "自由日（任何门类）"
    return f"今天是 {issue_date}（{_WEEKDAY_CN[wd]}）→ **{mode}**"


def _validate_classic_image(client, url: str) -> bool:
    """图片闸门：真实下载验证（Content-Type 是图、体量在栏目要求区间）。"""
    if not (url or "").startswith(("http://", "https://")):
        return False
    try:
        resp = client.get(url)
        ctype = (resp.headers.get("Content-Type")
                 or resp.headers.get("content-type") or "")
        return (resp.status_code == 200
                and ctype.split(";")[0].strip().lower() in _IMAGE_EXT
                and CLASSIC_IMG_MIN_BYTES <= len(resp.content) <= IMAGE_MAX_BYTES)
    except Exception:  # noqa: BLE001 —— 验证失败按"拿不到图"处理
        return False


def _wiki_lead_image(client, wiki_title: str) -> str | None:
    """直图通道：Wikipedia pageimages API 取条目主图（1600px 缩略，
    upload.wikimedia.org 直链可外链，体量适配网页展示，不抓原始大图）。"""
    if not wiki_title:
        return None
    try:
        api = ("https://en.wikipedia.org/w/api.php?action=query&format=json"
               "&prop=pageimages&piprop=thumbnail&pithumbsize=1600&redirects=1"
               "&titles=" + urllib.parse.quote(wiki_title))
        data = json.loads(client.get(api).content)
        for page in (data.get("query") or {}).get("pages", {}).values():
            src = (page.get("thumbnail") or {}).get("source")
            if src:
                return src
    except Exception:  # noqa: BLE001 —— API 失败走主编直链兜底
        return None
    return None


def _nominate_classic(conn, conf: AppConfig, backend: LLMBackend, board: str,
                      issue_date: str) -> dict:
    """《经典鉴赏》：策展主编（联网）提名一件经典作品，周一至五油画/周末自由。

    图片闸门：直图通道（Wikipedia 主图 API）→ 主编自带直链兜底，两条都验证
    真实可下载；拿不到图 = 换一件重提（最多 CLASSIC_ATTEMPTS 次），与经典论文
    "代码验证可抓才成题"同构。成题产物 = 合成条目（url 指向 Wikipedia 条目，
    取材阶段自然抓正文当供稿材料、收页内图库；图直链进 image_urls 供撰写期审选）
    + feature 选题（thread_key 前缀 classic-，前端识别为栏目）。

    幂等：本期该板块已有 classic- 选题即跳过；背调照常补作品来历与细节
    （艺术板块=纯背景故事模式），材料薄时自动触发联网调查。"""
    if conn.execute(
            "SELECT 1 FROM topics WHERE issue_date=? AND board=?"
            " AND thread_key LIKE 'classic-%'", (issue_date, board)).fetchone():
        return {"classic": "已有栏目选题"}
    done = [r["title"] for r in conn.execute(
        "SELECT title FROM raw_items WHERE source_id='classic-art'"
        " ORDER BY id DESC LIMIT 300")]
    done_block = "\n".join(f"- {t}" for t in done) or "（栏目首期，还没有已鉴赏作品）"

    rejected: list[str] = []
    with make_client() as client:
        for _ in range(CLASSIC_ATTEMPTS):
            retry_block = ""
            if rejected:
                retry_block = ("**注意：下列作品刚提名过但拿不到可用图片，请换一件作品**"
                               "（换作品，不是换图链）：" + "、".join(rejected))
            prompt = render_prompt(
                "editor_classic", day_rule=_classic_day_rule(issue_date),
                done_block=done_block, retry_block=retry_block)
            result = complete_json(backend, prompt, role="classic")
            artwork = str(result.get("artwork") or "").strip()
            artist = str(result.get("artist") or "").strip()
            wiki_title = str(result.get("wiki_title") or "").strip()
            own_img = str(result.get("image_url") or "").strip()
            if not artwork or not (wiki_title or own_img):
                rejected.append(artwork or "（字段不全的提名）")
                continue

            images: list[str] = []
            for cand in (_wiki_lead_image(client, wiki_title), own_img):
                if cand and cand not in images \
                        and _validate_classic_image(client, cand):
                    images.append(cand)
            if not images:
                rejected.append(f"{artwork}（{artist}）")
                continue

            page_url = ("https://en.wikipedia.org/wiki/"
                        + urllib.parse.quote(wiki_title.replace(" ", "_"))
                        if wiki_title else images[0])
            item = RawItem(
                source_id="classic-art", board=board, kind="article",
                url=page_url, url_canonical=canonicalize_url(page_url),
                title=f"{artwork} — {artist}（{result.get('year') or '年代不详'}）",
                summary=str(result.get("reason") or "")[:500] or None,
                content_hash=content_hash(f"{artwork}{artist}"),
                image_url=images[0], image_urls=images)
            db.insert_item(conn, item)
            row = conn.execute("SELECT id FROM raw_items WHERE url_canonical=?",
                               (item.url_canonical,)).fetchone()
            conn.execute("UPDATE raw_items SET status='selected' WHERE id=?",
                         (row["id"],))

            key = _normalize_thread_key(
                str(result.get("thread_key") or f"classic-{wiki_title or artwork}"))
            if not key.startswith("classic-"):
                key = ("classic-" + key)[:80]
            cur = conn.execute(
                "INSERT OR IGNORE INTO topics (issue_date, board, title, thread_key,"
                " item_ids, decision, slot, target_length, needs_image,"
                " update_of_thread, reason, score, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (issue_date, board, (str(result.get("title") or "").strip()
                                     or artwork)[:200], key,
                 json.dumps([row["id"]]), "feature", "regular",
                 CLASSIC_TARGET_LENGTH, 1, None,
                 ("【经典鉴赏栏目】" + str(result.get("reason") or ""))[:300],
                 None, utcnow_iso()))
            conn.commit()
            if cur.rowcount == 0:
                return {"classic": f"撞事件线唯一索引，本期跳过（{key}）"}
            return {"classic": f"{artwork}（{artist}），图 {len(images)} 张"}
    return {"classic": f"放弃：{CLASSIC_ATTEMPTS} 次提名均拿不到可用图"
                       f"（{'、'.join(rejected)}）"}


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
                "SELECT id, url, summary, extracted_text, image_url, image_urls"
                " FROM raw_items WHERE id=?",
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
                # 正文图库（多图排版）：feed 级图库优先（已过滤），页面内文图补位
                gallery = json.loads(r["image_urls"] or "[]")
                for u in all_images(html, base_url=r["url"]):
                    if u not in gallery:
                        gallery.append(u)
                gallery = gallery[:6]
                if text:
                    conn.execute(
                        "UPDATE raw_items SET extracted_text=?, image_url=?,"
                        " image_urls=? WHERE id=?",
                        (text, image,
                         json.dumps(gallery) if gallery else None, iid))
                    stats["fetched"] += 1
                else:
                    stats["failed"] += 1
            except Exception:  # noqa: BLE001 —— 单条取材失败不阻塞出刊
                stats["failed"] += 1
            conn.commit()
            time.sleep(1.0)   # 取材礼貌间隔

        # 图库补抓（2026-07-07，图片审选板块）：材料够厚的条目上面的主路径不下载页面，
        # 而 gnews 等标题源的正文图库只在原文页上——图库为空的条目单独抓一次页面收图，
        # 只补 image_urls/image_url，不动 extracted_text。失败静默（审选自然回退单图/无图）。
        if board in conf.image_review_boards:
            for iid in item_ids:
                r = conn.execute(
                    "SELECT id, url, extracted_text, image_url, image_urls"
                    " FROM raw_items WHERE id=?", (iid,)).fetchone()
                if not r["extracted_text"] or r["image_urls"]:
                    continue     # 薄材料条目已走主路径；已有图库的不重抓
                try:
                    html = client.get(r["url"]).text
                    gallery = all_images(html, base_url=r["url"])
                    if r["image_url"] and r["image_url"] not in gallery:
                        gallery.insert(0, r["image_url"])
                    if gallery:
                        conn.execute(
                            "UPDATE raw_items SET image_urls=?,"
                            " image_url=COALESCE(image_url, ?) WHERE id=?",
                            (json.dumps(gallery[:6]), gallery[0], iid))
                        stats["gallery"] = stats.get("gallery", 0) + 1
                except Exception:  # noqa: BLE001 —— 单条补抓失败不阻塞出刊
                    pass
                conn.commit()
                time.sleep(1.0)

        # 论文精读：feature（专题精读）与 brief（速览精读，2026-07-06）选题的首个论文
        # 条目抓 arXiv 原文进文件缓存。缓存按两者较大上限抓一次，写作时速览再裁到小上限。
        # 已成稿的选题跳过——否则 writer 删缓存后，之后每轮自愈 publish 都会白抓一遍
        decisions = []
        if conf.paper_fulltext_max_chars > 0:
            decisions.append("feature")
        if conf.paper_brief_fulltext_max_chars > 0:
            decisions.append("brief")
        if decisions:
            fetch_cap = max(conf.paper_fulltext_max_chars,
                            conf.paper_brief_fulltext_max_chars)
            feats = conn.execute(
                f"SELECT id, item_ids FROM topics WHERE issue_date=? AND board=?"
                f" AND decision IN ({','.join('?' * len(decisions))}) AND NOT EXISTS"
                f" (SELECT 1 FROM articles a WHERE a.topic_id = topics.id)",
                (issue_date, board, *decisions)).fetchall()
            for t in feats:
                rows = _topic_items(conn, t)
                owner = _paper_cache_item(rows)
                aid = _resolve_fulltext_arxiv_id(conn, owner) if owner else None
                if not aid:
                    continue
                conf.paper_cache_dir.mkdir(parents=True, exist_ok=True)
                path = conf.paper_cache_dir / f"{owner['id']}.txt"
                if path.exists():
                    stats["paper_skipped"] = stats.get("paper_skipped", 0) + 1
                    continue
                text = _fetch_arxiv_fulltext(client, aid, fetch_cap)
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
        f" source_id, author, image_url, image_urls"
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
    """背景审核：背调产物（概念解释 + 新闻调查补充 facts）批量审校（全板块一次调用）。

    verdict 三档：ok 保留 / fix 换成修正文本 / drop 删除。概念条目宁删勿留——
    讲错比不讲更糟；facts 条目按新闻口径放宽（带来源的单方说法可保留，撰写会归因）。
    审校只裁决不新增（新增内容又成了未核查的知识）；漏裁决的条目保守保留。
    幂等：reviewed 标记；follow_up 出自本刊往期原文，不在审核范围。
    """
    pending = []
    for r in conn.execute(
            "SELECT id, title, background FROM topics WHERE issue_date=? AND board=?"
            " AND background IS NOT NULL", (issue_date, board)).fetchall():
        bg = json.loads(r["background"])
        if (bg.get("concepts") or bg.get("facts")) and not bg.get("reviewed"):
            pending.append((r["id"], r["title"], bg))
    if not pending:
        return {}

    def _entry_lines(bg: dict) -> str:
        lines = [f"- {c['term']}：{c['note']}" for c in bg.get("concepts") or []]
        lines += [f"- [F{i}] {f['fact']}（来源：{f.get('source') or '未注明'}）"
                  for i, f in enumerate(bg.get("facts") or [], 1)]
        return "\n".join(lines)

    items_block = "\n\n".join(
        f"[T{tid}] {title}\n{_entry_lines(bg)}" for tid, title, bg in pending)
    review = complete_json(
        backend, render_prompt("checker_background", items_block=items_block),
        role="checker")
    verdicts = {}
    fact_verdicts = {}
    for entry in review.get("topics", []):
        if not isinstance(entry, dict):
            continue
        for c in entry.get("concepts") or []:
            if isinstance(c, dict) and c.get("term"):
                verdicts[(entry.get("id"), str(c["term"]).strip())] = (
                    str(c.get("verdict") or "").strip(),
                    str(c.get("note") or "").strip())
        for f in entry.get("facts") or []:
            if not isinstance(f, dict):
                continue
            try:
                idx = int(f.get("i"))
            except (TypeError, ValueError):
                continue
            fact_verdicts[(entry.get("id"), idx)] = (
                str(f.get("verdict") or "").strip(),
                str(f.get("note") or "").strip())

    fixed = dropped = f_fixed = f_dropped = 0
    for tid, _title, bg in pending:
        kept = []
        for c in bg.get("concepts") or []:
            verdict, note = verdicts.get((tid, c["term"]), ("", ""))
            if verdict == "drop":
                dropped += 1
                continue
            if verdict == "fix" and note:
                c = {"term": c["term"], "note": note}
                fixed += 1
            kept.append(c)
        bg["concepts"] = kept
        kept_f = []
        for i, f in enumerate(bg.get("facts") or [], 1):
            verdict, note = fact_verdicts.get((tid, i), ("", ""))
            if verdict == "drop":
                f_dropped += 1
                continue
            if verdict == "fix" and note:
                f = {"fact": note, "source": f.get("source") or ""}
                f_fixed += 1
            kept_f.append(f)
        bg["facts"] = kept_f
        bg["reviewed"] = True
        conn.execute("UPDATE topics SET background=? WHERE id=?",
                     (json.dumps(bg, ensure_ascii=False), tid))
    conn.commit()
    return {"bg_reviewed": len(pending), "bg_fixed": fixed, "bg_dropped": dropped,
            "bg_facts_fixed": f_fixed, "bg_facts_dropped": f_dropped}


# ---------- Stage 3.5 背景调查 ----------

RESEARCH_EXCERPT_CHARS = 600   # 背景调查只需识别载重概念，材料节选即可
RESEARCH_EXCERPT_ITEMS = 3
ARCHIVE_DAYS = 30              # 往期报道查阅窗口
ARCHIVE_INDEX_CAP = 200        # 索引条数上限（标题级）
ARCHIVE_READ_CAP = 5           # 单次可索取全文的篇数上限
ARCHIVE_BODY_CHARS = 3000
FACTS_MARK = "【需调查补充】"   # 薄材料新闻选题在背调清单里的标注


def _facts_eligible(conf: AppConfig, rows) -> bool:
    """调查补充的资格（2026-07-06 新闻/repo；2026-07-07 扩展到拿不到原文的论文）。

    - 非论文选题：供稿材料仅标题级时按新闻口径调查（repo 补 README/发布说明/社区讨论）；
    - 论文选题：原文精读**实在拿不到**时才放行——以取材期缓存文件为准（无 arXiv id、
      同题预印本兜底落空、抓取失败三种情况都表现为缓存缺失），此时联网检索期刊新闻稿/
      出版方页面/可靠媒体报道补信息量（付费墙顶刊常只有 ~50 字纯标题）。原文到手的
      论文材料已厚，维持不调查（严谨性走精读/核查线）。门槛比新闻宽（摘要级也算薄）。
    """
    total = sum(len(r["extracted_text"] or r["summary"] or "") for r in rows)
    if any(r["kind"] == "paper" for r in rows):
        owner = _paper_cache_item(rows)
        if owner is not None and (conf.paper_cache_dir / f"{owner['id']}.txt").exists():
            return False
        return total < PAPER_FACTS_THIN_CHARS
    return total < THIN_MATERIAL_CHARS


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


def _parse_research(result: dict, facts_max: int = 0) -> dict:
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
        facts = [
            {"fact": str(f["fact"]).strip(),
             "source": str(f.get("source") or "").strip()}
            for f in entry.get("facts") or []
            if isinstance(f, dict) and f.get("fact")
        ]
        by_id[entry.get("id")] = {
            "context": str(entry.get("context") or "").strip(),
            "concepts": concepts[:4],
            "facts": facts[:facts_max],
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
    - 调查补充（2026-07-06 新闻/repo，2026-07-07 扩展论文）：材料薄的选题标注
      【需调查补充】，agent 联网检索补充事实细节（facts，带来源），经背景审核后
      进撰写。新闻按事件口径；论文仅在原文精读实在拿不到时放行（见 _facts_eligible），
      检索期刊新闻稿/出版方页面/媒体报道；research_facts_max=0 整体关闭；
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

    investigate: set[int] = set()
    blocks = []
    for t in topics:
        rows = _topic_items(conn, t)
        if conf.research_facts_max > 0 and _facts_eligible(conf, rows):
            investigate.add(t["id"])
        excerpts = "\n".join(
            f"  - {r['title']}：{(r['extracted_text'] or r['summary'] or '（仅标题）')[:RESEARCH_EXCERPT_CHARS]}"
            for r in rows[:RESEARCH_EXCERPT_ITEMS])
        mark = FACTS_MARK if t["id"] in investigate else ""
        blocks.append(f"[T{t['id']}] {t['title']}{mark}\n"
                      f"入选理由: {t['reason'] or '（无）'}\n材料节选:\n{excerpts}")
    topics_block = "\n\n".join(blocks)
    archive = _archive_index(conn, board, issue_date)

    prompt = render_prompt(
        "researcher", board_name=board_name, reader_block=reader_block(profile),
        archive_days=ARCHIVE_DAYS, archive_block=_archive_block(archive),
        facts_max=conf.research_facts_max, topics_block=topics_block)
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
            facts_max=conf.research_facts_max, topics_block=topics_block)
        result = complete_json(backend, prompt2, role="researcher")

    by_id = _parse_research(result, conf.research_facts_max)
    with_bg = with_facts = 0
    for t in topics:  # 未覆盖的选题也落空产物，幂等守卫才认账
        bg = by_id.get(t["id"], {"context": "", "concepts": [], "facts": [],
                                 "follow_up": ""})
        if t["id"] not in investigate:
            bg["facts"] = []       # 未标注的选题不吃 facts——铁律在代码层兜底
        if bg["facts"]:
            with_facts += 1
        if bg["context"] or bg["concepts"] or bg["facts"] or bg["follow_up"]:
            with_bg += 1
        conn.execute("UPDATE topics SET background=? WHERE id=?",
                     (json.dumps(bg, ensure_ascii=False), t["id"]))
    conn.commit()
    return {"researched": len(topics), "with_background": with_bg,
            "investigated": len(investigate), "with_facts": with_facts,
            "archive_read": read}


# ---------- Stage 5 撰写 ----------

# 撰写期图片审选（2026-07-07）：候选图下载后附给 writer（codex -i 多模态）实际查看
_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png",
              "image/webp": ".webp", "image/gif": ".gif"}
_CARD_IMG_TOKEN_RE = re.compile(r"!\[[^\]\n]*\]\(\s*IMG\d+\s*\)")
IMAGE_REVIEW_CAP = 6            # 每篇最多附几张候选图
IMAGE_MAX_BYTES = 8_000_000     # 单图字节上限（防超大原图浪费附件与 token）
IMAGE_MIN_BYTES = 2_000         # 单图字节下限（躲过 URL 过滤的小图标/跟踪像素）


def _prepare_topic_images(conf: AppConfig, topic_id: int, rows) -> list:
    """图片审选的候选准备：与导出层同口径收集候选 → 下载到临时缓存。

    返回 [(编号, 原始URL, 本地Path)]，编号=附件顺序（1 起，与提示词对应）。
    下载失败/非图片/超限的候选直接剔除（对读者大概率也加载不出来）；
    全失败或无候选返回空列表 = 本篇不审选（image_plan 留 NULL 回退旧行为）。"""
    from rebas.render.export import _topic_images  # 懒加载：渲染依赖不拖进常规路径
    candidates = _topic_images(None, rows, cap=IMAGE_REVIEW_CAP)
    out = []
    with make_client() as client:
        for url in candidates:
            try:
                resp = client.get(url)
                ctype = (resp.headers.get("Content-Type")
                         or resp.headers.get("content-type") or "")
                ctype = ctype.split(";")[0].strip().lower()
                ext = _IMAGE_EXT.get(ctype)
                if resp.status_code != 200 or not ext \
                        or not IMAGE_MIN_BYTES <= len(resp.content) <= IMAGE_MAX_BYTES:
                    continue
                conf.image_cache_dir.mkdir(parents=True, exist_ok=True)
                path = conf.image_cache_dir / f"t{topic_id}-{len(out) + 1}{ext}"
                path.write_bytes(resp.content)
                out.append((len(out) + 1, url, path))
            except Exception:  # noqa: BLE001 —— 单图失败不阻塞撰写
                continue
    return out


def sweep_image_cache(conf: AppConfig, days: int = 3) -> int:
    """prune 兜底：清掉审选图残留（writer 中途失败时留下的），按文件 mtime。"""
    if not conf.image_cache_dir.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for p in conf.image_cache_dir.iterdir():
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def _parse_image_plan(result: dict, numbered: list) -> str | None:
    """writer 的 images_keep（编号列表，展示顺序）→ image_plan JSON。
    只认本次附过的编号、去重；numbered 为空 = 未审选，返回 None（NULL）。"""
    if not numbered:
        return None
    by_n = dict((n, u) for n, u, *_ in numbered)
    kept, seen = [], set()
    for n in result.get("images_keep") or []:
        if isinstance(n, int) and n in by_n and n not in seen:
            seen.add(n)
            kept.append([n, by_n[n]])
    return json.dumps({"kept": kept}, ensure_ascii=False)

def stage_writer(conn, conf: AppConfig, backend: LLMBackend, board: str,
                 board_name: str, issue_date: str) -> dict:
    """每篇入选选题（专题+速览）都产出报道：专题长写，速览按 brief_length 短写。"""
    topics = conn.execute(
        "SELECT t.* FROM topics t"
        " LEFT JOIN articles a ON a.topic_id = t.id"
        " WHERE t.issue_date=? AND t.board=? AND a.id IS NULL",
        (issue_date, board)).fetchall()
    written = 0
    # 精读缓存按"主论文条目"归属，但主编偶发把同一论文选进多个选题（无跨选题条目去重）。
    # 引用计数：同一缓存条目被几个待写选题当作主论文，写完最后一个才清——先写的选题不能
    # 删掉后写选题仍需要的原文（否则那篇被静默降级为摘要版，白费 fetch 抓的原文）。
    topic_rows = {t["id"]: _topic_items(conn, t) for t in topics}
    ft_refs: dict[int, int] = {}
    for t in topics:
        own = _paper_cache_item(topic_rows[t["id"]])
        if own:
            ft_refs[own["id"]] = ft_refs.get(own["id"], 0) + 1
    review_images = board in conf.image_review_boards
    for t in topics:
        rows = topic_rows[t["id"]]
        is_brief = t["decision"] == "brief"
        # 论文原文精读材料（fetch 阶段缓存）：专题用完整上限，速览裁到较小上限——
        # 速览仍是速览，够抓到方法要点与关键数字即可（2026-07-06 论文类速览也精读）。
        # 只认本选题"自己的"主论文条目的缓存（与 fetch 同口径），共享条目不越界读/删。
        ft_cap = (conf.paper_brief_fulltext_max_chars if is_brief
                  else conf.paper_fulltext_max_chars)
        own = _paper_cache_item(rows)
        fulltext = load_paper_fulltext(conf, [own]) if (ft_cap > 0 and own) else {}
        if fulltext:
            fulltext = {k: v[:ft_cap] for k, v in fulltext.items()}
        material_total = sum(
            len(fulltext.get(r["id"]) or r["extracted_text"] or r["summary"] or "")
            for r in rows)
        # 新闻调查补充的 facts 也是可用事实材料——计入总量，防薄材料封顶误伤
        if t["background"]:
            material_total += sum(
                len(f.get("fact") or "")
                for f in json.loads(t["background"]).get("facts") or [])
        # 图片审选（2026-07-07）：候选图下载附给 writer 实际查看——留哪些（可全弃）、
        # 正文何处插图由它定；下载全失败则本篇不审选（image_plan=NULL 回退旧行为）
        numbered = _prepare_topic_images(conf, t["id"], rows) if review_images else []
        img_paths = [p for _, _, p in numbered]

        def _build_prompt(img_block: str) -> str:
            if is_brief:
                return render_prompt(
                    "writer_brief", board_name=board_name, topic_title=t["title"],
                    reason=t["reason"] or "（主编未附理由）",
                    target_length=conf.brief_length,
                    background_block=background_block(t["background"]),
                    images_block=img_block,
                    materials_block=materials_block(rows, per_item_limit=4000,
                                                    fulltext=fulltext))
            target = t["target_length"] or 1000
            if material_total < THIN_MATERIAL_CHARS:
                target = min(target, THIN_LENGTH_CAP)
            if fulltext and conf.paper_deepread_length:
                # 精读专题：篇幅放宽，目标是把论文讲明白（材料有原文撑得起）
                target = max(target, conf.paper_deepread_length)
            return render_prompt(
                "writer", board_name=board_name, topic_title=t["title"],
                reason=t["reason"] or "（主编未附理由）", target_length=target,
                check_block=check_block(t["check_notes"]),
                background_block=background_block(t["background"]),
                images_block=img_block,
                materials_block=materials_block(rows, per_item_limit=6000,
                                                fulltext=fulltext))

        t0 = time.time()
        try:
            prompt = _build_prompt(images_block(numbered, is_brief=is_brief))
            try:
                result = complete_json(backend, prompt, role="writer",
                                       images=img_paths)
            except LLMError:
                if not img_paths:
                    raise
                # 带图调用失败（附件/多模态偶发问题）→ 摘图重试一次，本篇降级不审选
                numbered = []
                result = complete_json(
                    backend, _build_prompt(images_block([])), role="writer")
        finally:
            for p in img_paths:
                p.unlink(missing_ok=True)
        # 防御性清洗：文末孤立的空标题标记（模型输出截断残渣，避免渲染出空标题）
        body_md = re.sub(r"(?:\n#{1,6}[ \t]*)+\s*$", "", (result.get("body_md") or "")).rstrip()
        # 卡片摘要防漏插图令牌（令牌只在正文里有意义，导出期才解析）
        card = _CARD_IMG_TOKEN_RE.sub("", result.get("card_summary", ""))
        conn.execute(
            "INSERT INTO articles (topic_id, card_summary, body_md, credibility_notes,"
            " image_refs, image_plan, model_meta, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (t["id"], strip_html(card)[:200],
             body_md, t["check_notes"],
             json.dumps([r["image_url"] for r in rows if r["image_url"]][:3]),
             _parse_image_plan(result, numbered),
             json.dumps({"role": "writer", "elapsed_s": round(time.time() - t0, 1),
                         "material_chars": material_total,
                         **({"images_reviewed": len(numbered)} if numbered else {})}),
             utcnow_iso()))
        conn.commit()
        if own:
            ft_refs[own["id"]] -= 1
        # 原文用完即删（省磁盘；断点续跑安全——写失败时文件保留供重试）；
        # 但仅当没有其它待写选题还把它当主论文时才删（引用计数归零）
        if fulltext and own and ft_refs.get(own["id"], 0) <= 0:
            discard_paper_fulltext(conf, fulltext.keys())
        written += 1
    return {"written": written}
