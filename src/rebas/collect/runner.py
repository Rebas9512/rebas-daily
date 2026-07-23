"""采集调度：到期源筛选 → 并发抓取 → 解析入库 → fetch_state 更新。

网络请求在线程池并发；解析与写库在主线程串行（SQLite 单写者）。
"""

from __future__ import annotations

import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from rebas import db
from rebas.collect import arxiv, boards, feeds, hf, journals, reddit
from rebas.collect.base import FetchResult, KeywordMatcher, fetch_url, make_client, utcnow_iso
from rebas.config import Source, load_config, load_profile, load_sources

PARSERS = {
    "rss": feeds.parse_feed,
    "gnews_rss": feeds.parse_feed,
    "arxiv_rss": arxiv.parse_arxiv_rss,
    "hf_papers": hf.parse_papers,
    "hf_models": hf.parse_models,
    "hn_algolia": boards.parse_hn,
    "gh_trending": boards.parse_gh_trending,
    "openalex_journal": journals.parse_openalex_journal,
    "jmlr_volume": journals.parse_jmlr_volume,
    "reddit_rss": reddit.parse_reddit_rss,
    "nitter_rss": feeds.parse_nitter_rss,
    "truth_rss": feeds.parse_truth_rss,
}

# 榜单类源的"重新上榜"窗口：同一仓库/模型出榜超过 N 天后再上榜，重新进入待处理池
REVIVE_DAYS = {"gh_trending": 14, "hf_models": 14}

MAX_WORKERS = 8


@dataclass
class SourceStats:
    source_id: str
    status: str = "ok"          # ok | 304 | error | fallback | unsupported
    new: int = 0
    merged: int = 0
    revived: int = 0
    dup: int = 0
    filtered_out: int = 0       # 预筛丢弃（不入库）
    skipped: int = 0            # gnews 预算内未解析等
    error: str | None = None
    redirect: str | None = None  # 端点被重定向到的最终 URL（提示更新配置，dieline 类）

    def counts_line(self) -> str:
        if self.status == "error":
            return f"错误: {self.error}"
        if self.status == "304":
            return "未变化(304)"
        parts = [f"新增{self.new}"]
        if self.merged:
            parts.append(f"合并{self.merged}")
        if self.revived:
            parts.append(f"回榜{self.revived}")
        parts.append(f"重复{self.dup}")
        if self.filtered_out:
            parts.append(f"预筛除{self.filtered_out}")
        if self.skipped:
            parts.append(f"暂缓{self.skipped}")
        line = " ".join(parts)
        if self.status == "fallback":
            line = f"备用通道供给（主通道 {self.error}）: {line}"
        if self.redirect:
            line += f"（⚠ 重定向 → {self.redirect}，建议更新 endpoint 省一跳）"
        return line


def _is_due(conn, source: Source, now: datetime) -> bool:
    st = db.get_fetch_state(conn, source.id)
    if st is None or not st["last_fetch_at"]:
        return True
    last = datetime.fromisoformat(st["last_fetch_at"])
    return now - last >= timedelta(hours=source.fetch_interval_hours)


def _error_backoff_hours(interval: float, streak: int) -> float:
    """错误重试节律梯（回拨小时数，写进 last_fetch_at 让下次到期提前）：
    - 首错回拨整间隔 → 下一轮 collect 立即重试。偶发故障（Algolia 间歇 400 等）
      不再受批次网格错位摆布（07-09 实测：减半节律差 30 分钟错过批5，报错源
      白躺 9.5 小时等批1）；
    - 连败（2~7）回拨半间隔 → 现行减半节律，比正常周期更勤地探测恢复；
    - 长期死源（≥8 连败，约两天）不回拨 → 恢复正常间隔，不高频空打，
      admin 红牌 + 连败计数已经把问题亮出来了。"""
    if streak <= 1:
        return interval
    if streak < 8:
        return interval / 2
    return 0.0


def run_collect(force: bool = False, paced: bool = False) -> list[SourceStats]:
    """一轮采集。双车道：常规轮只跑 pace_seconds=0 的源（并发）；
    paced=True 只跑慢车道源（串行 + 源间隔 pace_seconds，专用 cron 滴灌，
    见 docs/OPERATIONS.md）——Reddit 等按 IP 严格限速的源连发即 429，
    绝不能进 8 线程并发池。"""
    conf = load_config()
    conn = db.init_db(conf.db_path)
    now = datetime.now(timezone.utc)

    all_stats: list[SourceStats] = []
    sources = load_sources(enabled_only=True)
    supported = []
    for s in sources:
        if s.type not in PARSERS:
            all_stats.append(SourceStats(s.id, status="unsupported"))
        elif bool(s.pace_seconds) == paced:
            supported.append(s)

    due = [s for s in supported if force or _is_due(conn, s, now)]
    # 画像缺失只跳过该板块的源，不让整轮采集崩（新增板块忘建 profile 的兜底）
    matchers: dict[str, KeywordMatcher] = {}
    bad_boards: set[str] = set()
    for board in {s.board for s in due}:
        try:
            matchers[board] = KeywordMatcher(load_profile(board))
        except Exception as exc:  # noqa: BLE001
            bad_boards.add(board)
            all_stats.append(SourceStats(
                f"profile:{board}", status="error",
                error=f"画像加载失败，跳过该板块全部源: {type(exc).__name__}"))
    due = [s for s in due if s.board not in bad_boards]

    # 主线程读出 conditional GET 状态，线程只做网络。
    # --force 同时绕过 conditional GET（改画像后强制重筛时 304 会让重抓空转）
    jobs: list[tuple[Source, str | None, str | None]] = []
    for s in due:
        st = None if force else db.get_fetch_state(conn, s.id)
        jobs.append((s, st["etag"] if st else None, st["last_modified"] if st else None))

    def _fetch_retry_5xx(client_, endpoint, *, etag, last_modified):
        try:
            return fetch_url(client_, endpoint, etag=etag, last_modified=last_modified)
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600:   # quantpedia 等偶发 5xx：退避一次再试
                time.sleep(3)
                return fetch_url(client_, endpoint, etag=etag,
                                 last_modified=last_modified)
            raise

    fetched: dict[str, FetchResult | Exception] = {}
    with make_client() as client:
        if paced:
            # 慢车道：严格串行，源与源之间睡 pace_seconds（同域限速按 IP 计，
            # 并发毫无意义且互相打成 429）
            for i, (s, etag, lm) in enumerate(jobs):
                if i:
                    time.sleep(s.pace_seconds)
                for attempt in (0, 1):
                    try:
                        fetched[s.id] = _fetch_retry_5xx(client, s.endpoint,
                                                         etag=etag, last_modified=lm)
                        break
                    except urllib.error.HTTPError as exc:
                        # 429 = IP 配额被临近请求（手动跑/探测/上一轮尾部）烧掉：
                        # 睡满两个间隔重试一次，仍失败交错误路径（interval 减半自愈）
                        if attempt == 0 and exc.code == 429:
                            time.sleep(s.pace_seconds * 2)
                            continue
                        fetched[s.id] = exc
                        break
                    except Exception as exc:  # noqa: BLE001 —— 单源失败不中断整轮
                        fetched[s.id] = exc
                        break
        else:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futs = {
                    ex.submit(_fetch_retry_5xx, client, s.endpoint,
                              etag=etag, last_modified=lm): s
                    for s, etag, lm in jobs
                }
                for fut in as_completed(futs):
                    s = futs[fut]
                    try:
                        fetched[s.id] = fut.result()
                    except Exception as exc:  # noqa: BLE001 —— 单源失败不中断整轮
                        fetched[s.id] = exc

        # 解析 + 入库（串行）
        for s in due:
            stats = SourceStats(s.id)
            result = fetched[s.id]
            if isinstance(result, Exception):
                stats.error = f"{type(result).__name__}: {str(result)[:120]}"
                prev = db.get_fetch_state(conn, s.id)
                streak = ((prev["error_streak"] or 0) if prev else 0) + 1
                # 备用通道：主通道抛错同轮改走备用端点——供给不断、病历（连败计数）照记；
                # 备用轮不写 etag（conditional GET 状态不跨端点）
                if s.fallback_endpoint:
                    try:
                        fb = fetch_url(client, s.fallback_endpoint)
                        result = fb if fb.status != 304 else None
                    except Exception:  # noqa: BLE001 —— 备用也挂 → 走正常错误路径
                        result = None
                    if isinstance(result, FetchResult):
                        stats.status = "fallback"
                if stats.status != "fallback":
                    stats.status = "error"
                    # 重试节律梯：回拨 last_fetch_at 让下次到期提前（见 _error_backoff_hours）
                    retry_at = now - timedelta(hours=_error_backoff_hours(
                        s.fetch_interval_hours, streak))
                    db.set_fetch_state(conn, s.id, etag=None, last_modified=None,
                                       last_fetch_at=retry_at.isoformat(timespec="seconds"),
                                       last_status="error", error_streak=streak)
                    conn.commit()
                    all_stats.append(stats)
                    continue
            if result.status == 304:
                stats.status = "304"
                db.set_fetch_state(conn, s.id, etag=result.etag,
                                   last_modified=result.last_modified,
                                   last_fetch_at=now.isoformat(timespec="seconds"),
                                   last_status="304")
                conn.commit()
                all_stats.append(stats)
                continue
            on_fallback = stats.status == "fallback"
            parser_type = (s.fallback_type or s.type) if on_fallback else s.type
            try:
                items, extra = PARSERS[parser_type](
                    s, result.data,
                    matcher=matchers.get(s.board),
                    conn=conn, client=client,
                )
                if parser_type in ("gnews_rss",):
                    stats.skipped = extra
                else:
                    stats.filtered_out = extra
                revive = REVIVE_DAYS.get(parser_type)
                for it in items:
                    outcome = db.insert_item(conn, it, revive_days=revive)
                    setattr(stats, outcome, getattr(stats, outcome) + 1)
                if on_fallback:
                    db.set_fetch_state(conn, s.id, etag=None, last_modified=None,
                                       last_fetch_at=now.isoformat(timespec="seconds"),
                                       last_status="fallback", error_streak=streak)
                else:
                    # 端点被重定向（dieline 类：缺尾斜杠每轮吃一跳 301 易触发限流）→
                    # 日志提示更新配置；正常入库不受影响
                    if result.final_url and result.final_url != s.endpoint:
                        stats.redirect = result.final_url
                    # gnews 有暂缓条目时不落盘 etag：否则下轮 304 直接跳过解析，
                    # 暂缓条目要等 feed 内容变化才有机会重试（预算饿死的另一半）
                    keep_cond = not (s.type == "gnews_rss" and stats.skipped > 0)
                    db.set_fetch_state(conn, s.id,
                                       etag=result.etag if keep_cond else None,
                                       last_modified=result.last_modified if keep_cond else None,
                                       last_fetch_at=now.isoformat(timespec="seconds"),
                                       last_status="ok")
            except Exception as exc:  # noqa: BLE001 —— 解析失败同样不中断
                stats.status = "error"
                stats.error = f"{type(exc).__name__}: {str(exc)[:120]}"
                db.set_fetch_state(conn, s.id, etag=None, last_modified=None,
                                   last_fetch_at=now.isoformat(timespec="seconds"),
                                   last_status="parse_error")
            conn.commit()
            all_stats.append(stats)

    conn.close()
    return all_stats


def run_backfill(date: str, source_id: str | None = None) -> list[SourceStats]:
    """arXiv 按日回填（YYYY-MM-DD）。默认回填全部启用的 arXiv 源；source_id 可指定单源。"""
    conf = load_config()
    conn = db.init_db(conf.db_path)
    sources = [s for s in load_sources() if s.type == "arxiv_rss" and s.enabled
               and (source_id is None or s.id == source_id)]
    all_stats: list[SourceStats] = []
    with make_client() as client:
        for source in sources:
            matcher = KeywordMatcher(load_profile(source.board))
            stats = SourceStats(f"backfill:{source.id}@{date}")
            items, stats.filtered_out = arxiv.backfill_day(source, date, matcher, client)
            for it in items:
                outcome = db.insert_item(conn, it)
                setattr(stats, outcome, getattr(stats, outcome) + 1)
            all_stats.append(stats)
    conn.commit()
    conn.close()
    return all_stats


TRAFFIC_KEEP_DAYS = 90   # 信标原始行保留窗；窗外聚成 traffic_daily 永久留（2026-07-23）


def run_prune(days: int, vacuum: bool = False) -> int:
    """瘦身：清空 N 天前条目的大字段（出刊窗口早已过去，只留题录）。"""
    from zoneinfo import ZoneInfo

    from rebas.config import pooled_source_groups

    conf = load_config()
    conn = db.init_db(conf.db_path)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    exemptions = {(now - timedelta(days=d)).isoformat(timespec="seconds"): ids
                  for d, ids in pooled_source_groups().items()}
    count = db.prune_texts(conn, cutoff, pool_exemptions=exemptions)
    # gnews 解析缓存 30 天过期（含负缓存），防 cron 长期运行无限膨胀
    cache_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    db.prune_gnews_cache(conn, cache_cutoff)
    # 流量原始行滚动聚合（date 列是刊历日，截止线也按刊历时区算）
    traffic_cutoff = (now.astimezone(ZoneInfo(conf.timezone))
                      - timedelta(days=TRAFFIC_KEEP_DAYS)).strftime("%Y-%m-%d")
    db.traffic_rollup(conn, traffic_cutoff)
    if vacuum:
        conn.execute("VACUUM")
    conn.close()
    return count
