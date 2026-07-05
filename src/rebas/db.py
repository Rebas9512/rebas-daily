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
    needs_image   INTEGER NOT NULL DEFAULT 0,
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
    last_status   TEXT
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
            " signals, image_url, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new')",
            (it.source_id, it.board, it.kind, it.url, it.url_canonical, it.title,
             it.author, it.published_at, now.isoformat(timespec="seconds"),
             it.summary, it.extracted_text, it.content_hash,
             json.dumps(it.signals, ensure_ascii=False) if it.signals else None,
             it.image_url),
        )
        return "new"
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id, fetched_at, last_seen_at, status, summary, image_url, signals"
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


def prune_texts(conn: sqlite3.Connection, before_iso: str) -> int:
    """瘦身：清空指定时间之前条目的大字段，只留题录（见 04 文档 §4）。"""
    cur = conn.execute(
        "UPDATE raw_items SET summary = NULL, extracted_text = NULL"
        " WHERE fetched_at < ? AND (summary IS NOT NULL OR extracted_text IS NOT NULL)",
        (before_iso,),
    )
    conn.commit()
    return cur.rowcount


# ---------- fetch_state ----------

def get_fetch_state(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fetch_state WHERE source_id = ?", (source_id,)
    ).fetchone()


def set_fetch_state(conn: sqlite3.Connection, source_id: str, *, etag: str | None,
                    last_modified: str | None, last_fetch_at: str, last_status: str) -> None:
    conn.execute(
        "INSERT INTO fetch_state (source_id, etag, last_modified, last_fetch_at, last_status)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(source_id) DO UPDATE SET etag=excluded.etag,"
        " last_modified=excluded.last_modified, last_fetch_at=excluded.last_fetch_at,"
        " last_status=excluded.last_status",
        (source_id, etag, last_modified, last_fetch_at, last_status),
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
