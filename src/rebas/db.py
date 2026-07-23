"""SQLite 存储：schema 定义与连接管理。

设计约定（见 vault: 00 项目规划 §6）：
- raw_items 是原始信息池，url_canonical 唯一约束负责跨源去重；
- topics / articles / issues 是出刊管线各阶段的产物，阶段幂等的依据；
- fetch_state 存各源的 conditional GET 状态（ETag / Last-Modified）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_items (
    id            INTEGER PRIMARY KEY,
    source_id     TEXT NOT NULL,
    board         TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'article',  -- article | paper | repo | issue
    url           TEXT NOT NULL,
    url_canonical TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    author        TEXT,
    published_at  TEXT,                              -- ISO8601
    fetched_at    TEXT NOT NULL,
    summary       TEXT,                              -- feed 自带摘要/导语
    extracted_text TEXT,                             -- 全文（feed 自带或 lazy fetch）
    content_hash  TEXT,
    signals       TEXT,                              -- JSON：upvotes/points/stars_today 等
    image_url     TEXT,
    image_urls    TEXT,                              -- JSON string[]：正文图库（艺术/设计多图排版用）
    status        TEXT NOT NULL DEFAULT 'new'        -- new | prefiltered_out | screened | selected | discarded
);
CREATE INDEX IF NOT EXISTS idx_raw_items_board_fetched ON raw_items(board, fetched_at);
CREATE INDEX IF NOT EXISTS idx_raw_items_status ON raw_items(status);

CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY,
    issue_date    TEXT NOT NULL,                     -- YYYY-MM-DD
    board         TEXT NOT NULL,
    title         TEXT NOT NULL,
    thread_key    TEXT,                              -- 事件线 slug：跨日去重与周榜的依据
    item_ids      TEXT NOT NULL,                     -- JSON int[]
    score         REAL,
    decision      TEXT NOT NULL,                     -- feature | brief | drop（专题/速览/弃）
    slot          TEXT,                              -- headline | regular
    target_length INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_issue ON topics(issue_date, board);
CREATE INDEX IF NOT EXISTS idx_topics_thread ON topics(thread_key);

CREATE TABLE IF NOT EXISTS articles (
    id                INTEGER PRIMARY KEY,
    topic_id          INTEGER NOT NULL REFERENCES topics(id),
    card_summary      TEXT NOT NULL,
    body_md           TEXT NOT NULL,
    credibility_notes TEXT,                          -- JSON：核查 agent 产物
    image_refs        TEXT,                          -- JSON
    model_meta        TEXT,                          -- JSON：模型/耗时/token
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    issue_date     TEXT PRIMARY KEY,                 -- YYYY-MM-DD
    kind           TEXT NOT NULL DEFAULT 'daily',    -- daily | weekly
    layout         TEXT,                             -- JSON：版面结构
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | edited | checked | written | rendered
    rendered_paths TEXT,                             -- JSON
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_state (
    source_id     TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    last_fetch_at TEXT,
    last_status   TEXT,                                -- ok | 304 | error | parse_error | fallback
    error_streak  INTEGER NOT NULL DEFAULT 0           -- 主通道连败计数（ok/304 归零）
);

CREATE TABLE IF NOT EXISTS gnews_cache (               -- Google News 跳转 URL → 真实 URL
    gnews_url   TEXT PRIMARY KEY,
    real_url    TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (               -- 管理后台账号（2026-07-05）
    email      TEXT PRIMARY KEY,
    pw_salt    TEXT NOT NULL,                          -- base64；scrypt 参数见 admin/auth.py
    pw_hash    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (                  -- 报道点赞/点踩（选题级，一题一票可改）
    topic_id   INTEGER PRIMARY KEY REFERENCES topics(id),
    vote       INTEGER NOT NULL,                       -- 1 赞 | -1 踩
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_views (                -- 主站信标原始行（2026-07-23，滚动保留见 runner.TRAFFIC_KEEP_DAYS）
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,                            -- UTC ISO
    date     TEXT NOT NULL,                            -- 刊历日（config.timezone），聚合口径
    hour     INTEGER NOT NULL,                         -- 刊历时（0-23），时段分布用
    visitor  TEXT NOT NULL,                            -- hash(日盐+IP+UA)，不落原始 IP/UA
    path     TEXT NOT NULL,
    title    TEXT,
    referrer TEXT,                                     -- 仅站外来路，只留 host
    country  TEXT,                                     -- CF-IPCountry 两位码
    device   TEXT                                      -- mobile | desktop
);
CREATE INDEX IF NOT EXISTS idx_page_views_date ON page_views(date);

CREATE TABLE IF NOT EXISTS traffic_daily (             -- page_views 滚出保留窗后的永久日聚合
    date   TEXT PRIMARY KEY,
    uv     INTEGER NOT NULL,
    pv     INTEGER NOT NULL,
    detail TEXT                                        -- JSON：sections/countries/devices 计数
);

CREATE TABLE IF NOT EXISTS traffic_zone_daily (        -- CF zone 日汇总（真人+爬虫），traffic-pull 写入
    date            TEXT PRIMARY KEY,
    requests        INTEGER,
    uniques         INTEGER,
    cached_requests INTEGER,
    bytes           INTEGER,
    fetched_at      TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")  # cron collect 与手动 publish 并发时等锁
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    """轻量迁移：列不存在则 ALTER TABLE 补上（保留既有数据）。"""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    # M3 增量列（早期建的库没有）
    _ensure_column(conn, "topics", "check_notes", "TEXT")
    _ensure_column(conn, "topics", "update_of_thread", "TEXT")
    _ensure_column(conn, "topics", "reason", "TEXT")   # 主编入选理由（brief 渲染直接用）
    _ensure_column(conn, "topics", "background", "TEXT")  # 背景调查产物（JSON，writer 铺垫用，不上前端）
    # 2026-07-04 审查加固
    _ensure_column(conn, "raw_items", "last_seen_at", "TEXT")  # 榜单 revive 的"出榜断档"口径
    # 2026-07-07 多图排版
    _ensure_column(conn, "raw_items", "image_urls", "TEXT")  # JSON string[] 正文图库
    # 2026-07-07 撰写期图片审选：{"kept": [[编号, url], ...]}，NULL=未审选（回退旧行为）
    _ensure_column(conn, "articles", "image_plan", "TEXT")
    # 2026-07-09 信息源自修复：连败计数（重试节律梯与 admin 病历展示的依据）
    _ensure_column(conn, "fetch_state", "error_streak", "INTEGER NOT NULL DEFAULT 0")
    conn.execute(  # 并发 publish 兜底：同期同板块同事件线只允许一条
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_issue_thread"
        " ON topics(issue_date, board, thread_key)")
    conn.commit()
    return conn


# ---------- raw_items ----------

def insert_item(conn: sqlite3.Connection, it, revive_days: int | None = None) -> str:
    """入库一条 RawItem。返回结果类型：
    new     首次入库
    merged  已存在，但合并了新的信号/摘要/图片（如 HF papers 补充 arXiv 条目的热度）
    revived 已存在且超出 revive 窗口（榜单类源重新上榜）→ 重置为 new 待处理
    dup     已存在，无事发生
    """
    import json
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    try:
        conn.execute(
            "INSERT INTO raw_items (source_id, board, kind, url, url_canonical, title,"
            " author, published_at, fetched_at, summary, extracted_text, content_hash,"
            " signals, image_url, image_urls, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new')",
            (it.source_id, it.board, it.kind, it.url, it.url_canonical, it.title,
             it.author, it.published_at, now.isoformat(timespec="seconds"),
             it.summary, it.extracted_text, it.content_hash,
             json.dumps(it.signals, ensure_ascii=False) if it.signals else None,
             it.image_url,
             json.dumps(it.image_urls) if it.image_urls else None),
        )
        return "new"
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id, fetched_at, last_seen_at, status, summary, image_url,"
            " image_urls, signals"
            " FROM raw_items WHERE url_canonical = ?", (it.url_canonical,)
        ).fetchone()
        if row is None:  # 竞态兜底
            return "dup"
        updates: list[str] = []
        params: list = []
        merged = False
        if it.signals:
            old = json.loads(row["signals"] or "{}")
            new_signals = {**old, **it.signals}
            if new_signals != old:
                updates.append("signals = ?")
                params.append(json.dumps(new_signals, ensure_ascii=False))
                merged = True
        if it.summary and not row["summary"]:
            updates.append("summary = ?")
            params.append(it.summary)
            merged = True
        if it.image_url and not row["image_url"]:
            updates.append("image_url = ?")
            params.append(it.image_url)
            merged = True
        if it.image_urls and not row["image_urls"]:
            # 图库回填：既有条目（升级前入库/其它源先采到）从 feed 正文补多图
            updates.append("image_urls = ?")
            params.append(json.dumps(it.image_urls))
            merged = True
        revived = False
        if revive_days is not None and row["status"] != "new":
            # 口径=出榜断档：上次在 feed 里出现距今 ≥N 天才算"重新上榜"。
            # 连续霸榜的条目每轮都会刷新 last_seen_at，不会被误 revive。
            cutoff = (now - timedelta(days=revive_days)).isoformat(timespec="seconds")
            last_seen = row["last_seen_at"] or row["fetched_at"] or ""
            if last_seen < cutoff:
                updates.extend(["fetched_at = ?", "status = 'new'"])
                params.append(now.isoformat(timespec="seconds"))
                revived = True
        updates.append("last_seen_at = ?")
        params.append(now.isoformat(timespec="seconds"))
        params.append(row["id"])
        conn.execute(f"UPDATE raw_items SET {', '.join(updates)} WHERE id = ?", params)
        return "revived" if revived else ("merged" if merged else "dup")


def prune_texts(conn: sqlite3.Connection, before_iso: str,
                pool_exemptions: dict[str, list[str]] | None = None) -> int:
    """瘦身：清空指定时间之前条目的大字段，只留题录（见 04 文档 §4）。

    pool_exemptions: {池窗截止时间: [source_id]} —— 顶刊池源在池窗内不瘦身，
    否则池内候选活 30 天、摘要第 8 天就被清空，之后入选只剩标题（2026-07-05 review 发现）。
    """
    where = "fetched_at < ? AND (summary IS NOT NULL OR extracted_text IS NOT NULL)"
    params: list = [before_iso]
    for cutoff, ids in (pool_exemptions or {}).items():
        ph = ",".join("?" * len(ids))
        where += f" AND NOT (source_id IN ({ph}) AND fetched_at >= ?)"
        params += [*ids, cutoff]
    cur = conn.execute(
        f"UPDATE raw_items SET summary = NULL, extracted_text = NULL WHERE {where}",
        params,
    )
    conn.commit()
    return cur.rowcount


# ---------- fetch_state ----------

def get_fetch_state(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fetch_state WHERE source_id = ?", (source_id,)
    ).fetchone()


def set_fetch_state(conn: sqlite3.Connection, source_id: str, *, etag: str | None,
                    last_modified: str | None, last_fetch_at: str, last_status: str,
                    error_streak: int = 0) -> None:
    conn.execute(
        "INSERT INTO fetch_state (source_id, etag, last_modified, last_fetch_at,"
        " last_status, error_streak)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(source_id) DO UPDATE SET etag=excluded.etag,"
        " last_modified=excluded.last_modified, last_fetch_at=excluded.last_fetch_at,"
        " last_status=excluded.last_status, error_streak=excluded.error_streak",
        (source_id, etag, last_modified, last_fetch_at, last_status, error_streak),
    )


# ---------- gnews_cache ----------
# real_url = "" 是负缓存标记：解析失败过，不再重复消耗解析预算；
# prune_gnews_cache 清理旧行时负缓存一并过期，等于给失败链接一个远期重试窗口。

def gnews_cache_get(conn: sqlite3.Connection, gnews_url: str) -> str | None:
    row = conn.execute(
        "SELECT real_url FROM gnews_cache WHERE gnews_url = ?", (gnews_url,)
    ).fetchone()
    return row["real_url"] if row else None


def prune_gnews_cache(conn: sqlite3.Connection, before_iso: str) -> int:
    """清理 N 天前的解析缓存（含负缓存），防止 cron 长期运行无限膨胀。"""
    cur = conn.execute(
        "DELETE FROM gnews_cache WHERE resolved_at < ?", (before_iso,))
    conn.commit()
    return cur.rowcount


def gnews_cache_put(conn: sqlite3.Connection, gnews_url: str, real_url: str) -> None:
    from datetime import datetime, timezone
    conn.execute(
        "INSERT OR REPLACE INTO gnews_cache (gnews_url, real_url, resolved_at) VALUES (?,?,?)",
        (gnews_url, real_url, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


# ---------- 流量监控（2026-07-23，见 admin/traffic.py） ----------

def traffic_rollup(conn: sqlite3.Connection, before_date: str) -> int:
    """把 before_date 之前的 page_views 聚成 traffic_daily 后删原始行（prune 调用）。

    永久留 uv/pv + 国家/设备计数；path/referrer 明细随原始行滚出——
    监控页的明细视图只看保留窗内，长期趋势只需要日聚合。
    """
    import json

    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM page_views WHERE date < ?", (before_date,))]
    for d in dates:
        uv, pv = conn.execute(
            "SELECT count(DISTINCT visitor), count(*) FROM page_views WHERE date=?",
            (d,)).fetchone()
        detail = {}
        for col in ("country", "device"):
            detail[col] = {r[0]: r[1] for r in conn.execute(
                f"SELECT {col}, count(*) FROM page_views WHERE date=?"
                f" AND {col} IS NOT NULL GROUP BY {col}", (d,))}
        conn.execute(
            "INSERT OR REPLACE INTO traffic_daily (date, uv, pv, detail) VALUES (?,?,?,?)",
            (d, uv, pv, json.dumps(detail, ensure_ascii=False)))
    cur = conn.execute("DELETE FROM page_views WHERE date < ?", (before_date,))
    conn.commit()
    return cur.rowcount
