"""出刊管线编排：issues.status 记录推进位置，阶段幂等、可断点续跑。

2026-07-04 审查后加固（接 cron 前）：
- 断点续跑跨日：date 缺省时优先续跑最近一个未完成期次，防止跨日失败的期次永久卡死；
- force-stage 级联清理该阶段及下游的产物（否则被各阶段的幂等守卫架空），且只允许回拨不允许前跳；
- 板块级容错：单板块失败不中断其余板块，全部尝试后再决定是否推进状态；
- 进程锁：防 cron 与手动 publish 并发导致选题成双。
"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from rebas import db
from rebas.agents import stages
from rebas.collect.base import utcnow_iso
from rebas.config import load_config, load_profile
from rebas.llm import get_backend

# 阶段顺序与完成后的 issue 状态。research 在 checker 之前：
# 背调产物（教科书级概念解释）与供稿论断在核查阶段一并把关，错误背景不进撰写。
STAGES = ("enrich", "screen", "editor", "fetch", "research", "checker", "writer", "render")
STATUS_AFTER = {
    "enrich": "enriched", "screen": "screened", "editor": "edited", "fetch": "fetched",
    "research": "researched", "checker": "checked", "writer": "written", "render": "rendered",
}
STATUS_ORDER = ("pending", "enriched", "screened", "edited", "fetched",
                "researched", "checked", "written", "rendered")


def _today(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def _resume_or_today(conn, tz: str, log) -> str:
    """date 缺省时：存在未完成期次则先续跑它（cron 无人值守的自愈路径）。"""
    today = _today(tz)
    row = conn.execute(
        "SELECT issue_date FROM issues WHERE status != 'rendered'"
        " ORDER BY issue_date LIMIT 1").fetchone()
    if row and row["issue_date"] < today:
        log(f"[resume] 发现未完成期次 {row['issue_date']}，优先续跑"
            f"（完成后再跑一次 publish 出今天的刊）")
        return row["issue_date"]
    return today


def _rewind_products(conn, conf, issue_date: str, force_stage: str, log) -> None:
    """force-stage 级联清理：该阶段及下游的产物删除/回拨，否则幂等守卫会让重跑变空转。"""
    pos = STAGES.index(force_stage)
    cascade = STAGES[pos:]
    topic_item_ids = [
        json.loads(r["item_ids"])
        for r in conn.execute(
            "SELECT item_ids FROM topics WHERE issue_date=?", (issue_date,)).fetchall()
    ]
    flat_ids = sorted({i for ids in topic_item_ids for i in ids})

    if "writer" in cascade:
        conn.execute("DELETE FROM articles WHERE topic_id IN"
                     " (SELECT id FROM topics WHERE issue_date=?)", (issue_date,))
    if "research" in cascade:
        conn.execute("UPDATE topics SET background=NULL WHERE issue_date=?", (issue_date,))
    if "checker" in cascade:
        conn.execute("UPDATE topics SET check_notes=NULL WHERE issue_date=?", (issue_date,))
    if "editor" in cascade:
        conn.execute("DELETE FROM topics WHERE issue_date=?", (issue_date,))
        if flat_ids:  # 本期已消费的候选放回粗筛池
            conn.execute(
                f"UPDATE raw_items SET status='screened' WHERE status='selected'"
                f" AND id IN ({','.join('?' * len(flat_ids))})", flat_ids)
    if "screen" in cascade:
        clause, params = stages._window_clause(conf)
        cur = conn.execute(
            f"UPDATE raw_items SET status='new'"
            f" WHERE status IN ('screened','dropped','selected') AND {clause}", params)
        log(f"[force] 窗口内 {cur.rowcount} 条候选重置为待粗筛")
    conn.commit()


def run_publish(date: str | None = None, force_stage: str | None = None,
                boards: list[str] | None = None, refill: bool = False,
                log=print) -> str:
    """boards 过滤 = 部分模式（cron 分批备刊）：只跑指定板块的各阶段，
    不推进 issue 状态、不渲染——最后一批不带过滤跑全板块收尾（幂等守卫
    自动跳过已完成的板块，顺便补齐之前批次失败的），状态才正常推进。"""
    conf = load_config()
    conn = db.init_db(conf.db_path)

    partial = boards is not None
    if partial:
        unknown = set(boards) - set(conf.publish_boards)
        if unknown:
            raise ValueError(f"未知板块: {','.join(sorted(unknown))}"
                             f"（可选 {'/'.join(conf.publish_boards)}）")
        if force_stage:
            # 级联清理按整期生效（删 topics/articles/background 不分板块），
            # 与板块过滤组合会误伤未指定板块的产物——禁用该组合
            raise ValueError("--force-stage 不能与 --boards 同用："
                             "清理级联按整期生效，会误伤其他板块")
    active_boards = [b for b in conf.publish_boards if not partial or b in boards]

    # 进程锁：cron 与手动并发会让选题成双（存在性检查与插入非原子）
    lock_file = (conf.data_dir / "publish.lock").open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("另一个 publish 正在运行（data/publish.lock 被占用）") from None

    try:
        issue_date = date or _resume_or_today(conn, conf.timezone, log)

        conn.execute(
            "INSERT OR IGNORE INTO issues (issue_date, kind, status, updated_at)"
            " VALUES (?,?,?,?)",
            (issue_date, "daily", "pending", utcnow_iso()))
        conn.commit()
        status = conn.execute(
            "SELECT status FROM issues WHERE issue_date=?",
            (issue_date,)).fetchone()["status"]
        done_pos = STATUS_ORDER.index(status)

        if force_stage:
            if force_stage not in STAGES:
                raise ValueError(f"未知阶段: {force_stage}（可选 {'/'.join(STAGES)}）")
            # 只允许回拨：对还没跑到的阶段 force 会伪造完成状态、造成漏刊
            rewind_pos = STAGES.index(force_stage)
            if rewind_pos < done_pos:
                _rewind_products(conn, conf, issue_date, force_stage, log)
                done_pos = rewind_pos
            else:
                log(f"[force] 期次 {issue_date} 尚未跑到 {force_stage}（当前 {status}），"
                    f"按正常断点续跑处理")

        backend = get_backend(conf)

        for stage in STAGES:
            stage_pos = STATUS_ORDER.index(STATUS_AFTER[stage])
            if stage_pos <= done_pos:
                continue
            if stage == "render" and partial:
                log("[render] 部分模式跳过（发布闸门在收尾批 / 翻牌 render 处理）")
                continue
            if stage == "render":
                from rebas.render.export import build_site
                s = build_site(conn, conf)
                log(f"[render] {s}")
                status = STATUS_AFTER[stage]
                conn.execute(
                    "UPDATE issues SET status=?, updated_at=? WHERE issue_date=?",
                    (status, utcnow_iso(), issue_date))
                # 收尾瘦身：7 天前条目只留题录（见 04 文档 §4）；顶刊池源在池窗内豁免
                from datetime import timedelta, timezone

                from rebas.config import pooled_source_groups
                now_utc = datetime.now(timezone.utc)
                cutoff = (now_utc - timedelta(days=7)).isoformat(timespec="seconds")
                exemptions = {
                    (now_utc - timedelta(days=d)).isoformat(timespec="seconds"): ids
                    for d, ids in pooled_source_groups().items()}
                pruned = db.prune_texts(conn, cutoff, pool_exemptions=exemptions)
                if pruned:
                    log(f"[prune] 瘦身 {pruned} 条（>7 天前的大字段已清）")
                cache_cutoff = (datetime.now(timezone.utc)
                                - timedelta(days=30)).isoformat(timespec="seconds")
                db.prune_gnews_cache(conn, cache_cutoff)
                swept = stages.sweep_paper_cache(conf)
                if swept:
                    log(f"[prune] 论文原文缓存清扫 {swept} 个残留文件")
                swept_img = stages.sweep_image_cache(conf)
                if swept_img:
                    log(f"[prune] 审选图缓存清扫 {swept_img} 个残留文件")
                conn.commit()
                continue

            # 板块级容错：单板块失败记下来继续跑其余板块；
            # 有失败则不推进状态，下次续跑只补失败的板块（幂等守卫跳过已完成的）
            failures: list[str] = []
            for board in active_boards:
                try:
                    profile = load_profile(board)
                    board_name = profile.name
                    if stage == "enrich":
                        s = stages.stage_enrich(conn, conf, board)
                    elif stage == "screen":
                        s = stages.stage_screen(conn, conf, backend, board,
                                                profile, board_name)
                    elif stage == "editor":
                        s = stages.stage_editor(conn, conf, backend, board, profile,
                                                board_name, issue_date, refill=refill)
                    elif stage == "fetch":
                        s = stages.stage_fetch(conn, conf, board, issue_date)
                    elif stage == "checker":
                        s = stages.stage_checker(conn, conf, backend, board, issue_date)
                    elif stage == "research":
                        s = stages.stage_research(conn, conf, backend, board,
                                                  profile, board_name, issue_date)
                    elif stage == "writer":
                        s = stages.stage_writer(conn, conf, backend, board,
                                                board_name, issue_date)
                    log(f"[{stage}] {board}: {s}")
                except Exception as e:  # noqa: BLE001 —— 板块级隔离
                    conn.commit()  # 保住该板块已落库的部分进度
                    failures.append(f"{board}: {type(e).__name__}: {e}")
                    log(f"[{stage}] {board} 失败: {e}")
            if failures:
                raise RuntimeError(
                    f"[{stage}] {len(failures)} 个板块失败，状态停留在 {status}，"
                    f"重跑 publish 会从断点续跑：{'; '.join(failures)}")
            if partial:
                continue  # 部分模式不推进状态：收尾批全板块跑完才推进
            status = STATUS_AFTER[stage]
            conn.execute("UPDATE issues SET status=?, updated_at=? WHERE issue_date=?",
                         (status, utcnow_iso(), issue_date))
            conn.commit()

        return status
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        conn.close()
