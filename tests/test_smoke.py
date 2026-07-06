"""M0 冒烟测试：配置可加载、画像可解析、schema 可建库。"""

import sqlite3

from rebas import config as cfg
from rebas import db as database


def test_load_config():
    conf = cfg.load_config()
    assert conf.llm_backend in ("codex_cli", "openai_api")
    assert "academic" in conf.publish_boards
    # search_roles 必须写在 [llm] 表内、[llm.roles] 表头之前——写错位置会被
    # 解析成 llm.roles.search_roles 而静默失效（背调调查新闻就退化成凭记忆编）
    assert conf.llm_search_roles == ("researcher",)
    assert "search_roles" not in conf.llm_roles


def test_load_sources():
    sources = cfg.load_sources()
    assert len(sources) > 30
    enabled = cfg.load_sources(enabled_only=True)
    assert all(s.enabled for s in enabled)
    # 2026-07-04 板块拆分：五板块都有启用源，且不超出配置的板块集合
    conf = cfg.load_config()
    assert {s.board for s in enabled} == set(conf.publish_boards)
    # 每个源的必填字段合法
    for s in sources:
        assert s.content in ("fulltext", "abstract", "headline"), s.id
        assert s.fetch_interval_hours > 0, s.id


def test_load_profiles():
    for board in ("academic", "repos", "tech", "data", "finance", "quant", "design", "art"):
        p = cfg.load_profile(board)
        assert p.board == board
        assert p.interests, board
        assert p.all_keywords(), board


def test_db_schema(tmp_path):
    conn = database.init_db(tmp_path / "t.sqlite")
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"raw_items", "topics", "articles", "issues", "fetch_state"} <= tables
    # url_canonical 唯一约束生效
    conn.execute(
        "INSERT INTO raw_items (source_id, board, url, url_canonical, title, fetched_at)"
        " VALUES ('t', 'academic', 'http://a', 'http://a', 'x', '2026-07-03')"
    )
    try:
        conn.execute(
            "INSERT INTO raw_items (source_id, board, url, url_canonical, title, fetched_at)"
            " VALUES ('t', 'academic', 'http://a?utm=1', 'http://a', 'y', '2026-07-03')"
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised
    conn.close()
