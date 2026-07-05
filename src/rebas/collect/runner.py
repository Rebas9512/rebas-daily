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
from rebas.collect import arxiv, boards, feeds, hf
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
}

# 榜单类源的"重新上榜"窗口：同一仓库/模型出榜超过 N 天后再上榜，重新进入待处理池
REVIVE_DAYS = {"gh_trending": 14, "hf_models": 14}

MAX_WORKERS = 8


@dataclass
class SourceStats:
    source_id: str
    status: str = "ok"          # ok | 304 | error | unsupported
    new: int = 0
    merged: int = 0
    revived: int = 0
    dup: int = 0
    filtered_out: int = 0       # 预筛丢弃（不入库）
    skipped: int = 0            # gnews 预算内未解析等
    error: str | None = None

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
        return " ".join(parts)


def _is_due(conn, source: Source, now: datetime) -> bool:
    st = db.get_fetch_state(conn, source.id)
    if st is None or not st["last_fetch_at"]:
        return True
    last = datetime.fromisoformat(st["last_fetch_at"])
    return now - last >= timedelta(hours=source.fetch_interval_hours)


def run_collect(force: bool = False) -> list[SourceStats]:
    conf = load_config()
    conn = db.init_db(conf.db_path)
    now = datetime.now(timezone.utc)

    all_stats: list[SourceStats] = []
    sources = load_sources(enabled_only=True)
    supported = []
    for s in sources:
        if s.type in PARSERS:
            supported.append(s)
        else:
            all_stats.append(SourceStats(s.id, status="unsupported"))

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
                stats.status = "error"
                stats.error = f"{type(result).__name__}: {str(result)[:120]}"
                # 出错后下次到期时间减半：偶发故障不用空等满整个间隔
                retry_at = (now - timedelta(hours=s.fetch_interval_hours / 2))
                db.set_fetch_state(conn, s.id, etag=None, last_modified=None,
                                   last_fetch_at=retry_at.isoformat(timespec="seconds"),
                                   last_status="error")
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
            try:
                items, extra = PARSERS[s.type](
                    s, result.data,
                    matcher=matchers.get(s.board),
                    conn=conn, client=client,
                )
                if s.type in ("gnews_rss",):
                    stats.skipped = extra
                else:
                    stats.filtered_out = extra
                revive = REVIVE_DAYS.get(s.type)
                for it in items:
                    outcome = db.insert_item(conn, it, revive_days=revive)
                    setattr(stats, outcome, getattr(stats, outcome) + 1)
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


def run_prune(days: int, vacuum: bool = False) -> int:
    """瘦身：清空 N 天前条目的大字段（出刊窗口早已过去，只留题录）。"""
    conf = load_config()
    conn = db.init_db(conf.db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    count = db.prune_texts(conn, cutoff)
    # gnews 解析缓存 30 天过期（含负缓存），防 cron 长期运行无限膨胀
    cache_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    db.prune_gnews_cache(conn, cache_cutoff)
    if vacuum:
        conn.execute("VACUUM")
    conn.close()
    return count
