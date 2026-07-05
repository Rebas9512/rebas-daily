"""M3 管线测试：提示词模板渲染 + 阶段编排逻辑（不打真模型）。"""

from rebas.agents.prompts import (
    background_block, check_block, profile_block, reader_block, render_prompt,
)
from rebas.config import Interest, Profile


def make_profile():
    return Profile(board="academic", name="学术 · AI/ML",
                   interests=(Interest("LLM", 5, ("LLM", "agent")),
                              Interest("世界模型", 5, ("world model",))),
                   reader_assumed="AI/ML 从业者",
                   reader_explain="子领域专门概念给一句白话")


class TestPromptTemplates:
    def test_all_templates_render(self):
        p = make_profile()
        pb = profile_block(p)
        assert "LLM（权重5）" in pb
        screen = render_prompt("screen", board_name=p.name, profile_block=pb,
                               count=2, items_block="[1] ...\n[2] ...")
        assert '{"scores"' in screen and "覆盖全部 2 条" in screen
        editor = render_prompt("editor", board_name=p.name, issue_date="2026-07-03",
                               count=2, profile_block=pb, feature_cap=4,
                               recent_threads_block="（空）", items_block="[1] ...")
        assert "thread_key" in editor and "材料深度决定篇幅" in editor
        checker = render_prompt("checker", topic_title="T", materials_block="[S1] ...")
        assert "multi|single|uncertain" in checker
        researcher = render_prompt("researcher", board_name=p.name,
                                   reader_block=reader_block(p), archive_days=30,
                                   archive_block="（30 天内无往期报道）",
                                   topics_block="[T1] ...")
        assert "教科书级" in researcher and "宁缺毋滥" in researcher
        assert "AI/ML 从业者" in researcher   # 读者画像注入
        assert "need_articles" in researcher  # 往期全文索取协议
        r2 = render_prompt("researcher_articles", board_name=p.name,
                           reader_block=reader_block(p),
                           archive_articles_block="[A1] ...", topics_block="[T1] ...")
        assert "follow_up" in r2 and "往期报道全文" in r2
        bg_check = render_prompt("checker_background", items_block="[T1] ...")
        assert "ok|fix|drop" in bg_check and "宁删勿留" in bg_check
        writer = render_prompt("writer", board_name=p.name, topic_title="T",
                               reason="r", target_length=1000,
                               check_block="- ...", background_block="- R-hat：...",
                               materials_block="[S1] ...")
        assert "最多 3 条要点" in writer and "card_summary" in writer
        assert "写作基调" in writer      # style.md 自动注入
        assert "背景材料" in writer and "- R-hat：..." in writer
        brief = render_prompt("writer_brief", board_name=p.name, topic_title="T",
                              reason="r", target_length=300,
                              background_block="（无背景材料）",
                              materials_block="[S1] ...")
        assert "card_summary" in brief and "不用小标题" in brief
        assert "写作基调" in brief       # style.md 同样注入速览模板

    def test_check_block_formats(self):
        raw = ('{"claims":[{"claim":"X 提升 2 倍","support":1,"confidence":"single"}],'
               '"notes":"注意营销表述"}')
        block = check_block(raw)
        assert "[single] X 提升 2 倍（1 个独立信源）" in block
        assert "注意营销表述" in block
        assert "单一信源" in check_block(None)

    def test_background_block_formats(self):
        raw = ('{"context":"MCMC 收敛诊断的实现权衡",'
               '"concepts":[{"term":"R-hat","note":"判断多条链是否一致的指标"}],'
               '"follow_up":"上期讲了实现障碍，这期量化代价"}')
        block = background_block(raw)
        assert "领域语境：MCMC 收敛诊断的实现权衡" in block
        assert "- R-hat：判断多条链是否一致的指标" in block
        assert "往期脉络（本刊此前报道过的事件线）：上期讲了实现障碍" in block
        assert background_block(None) == "（无背景材料）"
        assert background_block('{"context":"","concepts":[]}') == "（无背景材料）"


def test_thread_key_normalize():
    from rebas.agents.stages import _normalize_thread_key
    assert _normalize_thread_key("OpenAI GPT-5.6 Release!") == "openai-gpt-56-release"
    assert _normalize_thread_key("") == "untitled"


def test_window_settle(tmp_path):
    """论文沉淀期：paper 未满 settle 不入窗；article 不受沉淀期影响；窗口下限仍生效。"""
    import dataclasses
    from datetime import datetime, timedelta, timezone

    from rebas import db as database
    from rebas.agents.stages import _window_clause
    from rebas.config import load_config

    conn = database.init_db(tmp_path / "t.sqlite")
    now = datetime.now(timezone.utc)

    def ago(h):
        return (now - timedelta(hours=h)).isoformat(timespec="seconds")

    items = [  # (标签, kind, published_at, fetched_at)
        ("paper-fresh", "paper", ago(10), ago(1)),      # 未满沉淀期 → 排除
        ("paper-settled", "paper", ago(60), ago(1)),    # 已沉淀 → 入窗
        ("paper-stale", "paper", ago(120), ago(1)),     # 超窗口下限 → 排除
        ("article-fresh", "article", ago(10), ago(1)),  # article 无沉淀期 → 入窗
        ("undated", "article", None, ago(10)),          # 无日期按 fetched_at → 入窗
    ]
    for tag, kind, pub, fet in items:
        conn.execute(
            "INSERT INTO raw_items (source_id, board, url, url_canonical, title,"
            " kind, published_at, fetched_at, status) VALUES ('s','academic',?,?,?,?,?,?,'new')",
            (f"http://e/{tag}", f"http://e/{tag}", tag, kind, pub, fet))
    conn.commit()

    conf = dataclasses.replace(load_config(), window_hours=96, paper_settle_hours=48)
    clause, params = _window_clause(conf)
    got = {r["title"] for r in conn.execute(
        f"SELECT title FROM raw_items WHERE {clause}", params)}
    assert got == {"paper-settled", "article-fresh", "undated"}

    conf0 = dataclasses.replace(conf, paper_settle_hours=0)
    clause, params = _window_clause(conf0)
    got = {r["title"] for r in conn.execute(
        f"SELECT title FROM raw_items WHERE {clause}", params)}
    assert "paper-fresh" in got                          # settle=0 恢复当天入刊


def test_pipeline_status_order():
    from rebas.pipeline import STAGES, STATUS_AFTER, STATUS_ORDER
    # 每个阶段的完成态在顺序表中单调递增
    positions = [STATUS_ORDER.index(STATUS_AFTER[s]) for s in STAGES]
    assert positions == sorted(positions)
    assert STATUS_ORDER[0] == "pending"
    # 背调在取材后、核查前：背调产物与供稿论断一并核查，错误背景不进撰写
    assert (STAGES.index("fetch") < STAGES.index("research")
            < STAGES.index("checker") < STAGES.index("writer"))


class _FakeBackend:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, prompt, *, role="default"):
        self.prompts.append(prompt)
        return self.outputs.pop(0)


def test_stage_research(tmp_path):
    """背景调查：板块级开关、批量落库（未覆盖也落空产物）、已写报道跳过、幂等。"""
    import json

    from rebas import db as database
    from rebas.agents.stages import stage_research
    from rebas.config import load_config

    conn = database.init_db(tmp_path / "t.sqlite")
    conn.execute(
        "INSERT INTO raw_items (source_id, board, url, url_canonical, title, summary,"
        " fetched_at) VALUES ('s','quant','u','u','夏普论文','摘要','x')")
    iid = conn.execute("SELECT id FROM raw_items").fetchone()["id"]

    def add_topic(key, decision):
        conn.execute(
            "INSERT INTO topics (issue_date, board, title, thread_key, item_ids,"
            " decision, needs_image, created_at, reason) VALUES"
            " ('2026-01-01','quant',?,?,?,?,0,'x','r')",
            (key, key, json.dumps([iid]), decision))
        return conn.execute("SELECT id FROM topics WHERE thread_key=?",
                            (key,)).fetchone()["id"]

    t1 = add_topic("t-one", "feature")
    t2 = add_topic("t-two", "brief")
    t3 = add_topic("t-done", "brief")
    conn.execute("INSERT INTO articles (topic_id, card_summary, body_md, created_at)"
                 " VALUES (?,'c','b','x')", (t3,))
    conn.commit()
    conf = load_config()

    # 无 [reader] 画像的板块整体跳过（商业/艺术）
    plain = Profile(board="finance", name="商业", interests=())
    assert "skipped" in stage_research(conn, conf, None, "finance", plain,
                                       "商业", "2026-01-01")

    profile = Profile(board="quant", name="量化", interests=(),
                      reader_assumed="懂 ML", reader_explain="金融概念要铺垫")
    backend = _FakeBackend(json.dumps({"topics": [
        {"id": t1, "context": "资产定价",
         "concepts": [{"term": "夏普比率", "note": "单位风险的超额收益"}]},
        "非法条目",                       # 字段级容错：非 dict 跳过
        {"id": 999999, "context": "", "concepts": []},   # 幽灵 id 忽略
    ]}, ensure_ascii=False))
    stats = stage_research(conn, conf, backend, "quant", profile, "量化", "2026-01-01")
    assert stats == {"researched": 2, "with_background": 1, "archive_read": 0}
    assert "夏普论文" in backend.prompts[0]           # 材料节选入提示词
    assert "金融概念要铺垫" in backend.prompts[0]     # 读者画像入提示词
    assert "30 天内无往期报道" in backend.prompts[0]  # 空书架如实呈现

    bg1 = json.loads(conn.execute(
        "SELECT background FROM topics WHERE id=?", (t1,)).fetchone()[0])
    assert bg1 == {"context": "资产定价",
                   "concepts": [{"term": "夏普比率", "note": "单位风险的超额收益"}],
                   "follow_up": ""}
    bg2 = json.loads(conn.execute(
        "SELECT background FROM topics WHERE id=?", (t2,)).fetchone()[0])
    assert bg2 == {"context": "", "concepts": [], "follow_up": ""}  # 未覆盖也落空产物 = 幂等标记
    assert conn.execute("SELECT background FROM topics WHERE id=?",
                        (t3,)).fetchone()[0] is None  # 已有报道的不调查

    assert "skipped" in stage_research(conn, conf, backend, "quant", profile,
                                       "量化", "2026-01-01")  # 再跑无事可做


def test_stage_research_archive_followup(tmp_path):
    """往期查阅两轮协议：索引→索取全文→follow_up 落库；幽灵 id 不外传。"""
    import json

    from rebas import db as database
    from rebas.agents.stages import stage_research
    from rebas.config import load_config

    conn = database.init_db(tmp_path / "t.sqlite")
    conn.execute(
        "INSERT INTO raw_items (source_id, board, url, url_canonical, title, summary,"
        " fetched_at) VALUES ('s','quant','u','u','新进展','摘要','x')")
    iid = conn.execute("SELECT id FROM raw_items").fetchone()["id"]
    # 往期（07-01）报道过同一事件线，已有成文
    conn.execute(
        "INSERT INTO topics (issue_date, board, title, thread_key, item_ids, decision,"
        " needs_image, created_at, reason) VALUES"
        " ('2026-07-01','quant','事件线首报','saga-key',?,'feature',0,'x','r')",
        (json.dumps([iid]),))
    past = conn.execute("SELECT id FROM topics WHERE issue_date='2026-07-01'"
                        ).fetchone()["id"]
    conn.execute("INSERT INTO articles (topic_id, card_summary, body_md, created_at)"
                 " VALUES (?,'旧卡','上期正文细节','x')", (past,))
    conn.execute(
        "INSERT INTO topics (issue_date, board, title, thread_key, item_ids, decision,"
        " needs_image, created_at, reason) VALUES"
        " ('2026-07-05','quant','事件线新篇','saga-key',?,'brief',0,'x','r')",
        (json.dumps([iid]),))
    cur = conn.execute("SELECT id FROM topics WHERE issue_date='2026-07-05'"
                       ).fetchone()["id"]
    conn.commit()

    profile = Profile(board="quant", name="量化", interests=(),
                      reader_assumed="懂 ML", reader_explain="金融要铺垫")
    backend = _FakeBackend(
        json.dumps({"need_articles": [past, 999999]}),   # 幽灵 id 被过滤
        json.dumps({"topics": [{"id": cur, "context": "", "concepts": [],
                                "follow_up": "上期讲了 X，这期新在 Y"}]},
                   ensure_ascii=False))
    stats = stage_research(conn, load_config(), backend, "quant", profile,
                           "量化", "2026-07-05")
    assert stats == {"researched": 1, "with_background": 1, "archive_read": 1}
    assert f"[A{past}]" in backend.prompts[0]      # 第一轮见标题索引
    assert "上期正文细节" in backend.prompts[1]    # 第二轮见全文
    bg = json.loads(conn.execute("SELECT background FROM topics WHERE id=?",
                                 (cur,)).fetchone()[0])
    assert bg["follow_up"] == "上期讲了 X，这期新在 Y"


def test_checker_background_review(tmp_path):
    """背景审核：ok 保留 / fix 换文本 / drop 删除 / 漏裁决保守保留；幂等。"""
    import json

    from rebas import db as database
    from rebas.agents.stages import stage_checker
    from rebas.config import load_config

    conn = database.init_db(tmp_path / "t.sqlite")
    bg0 = {"context": "", "follow_up": "", "concepts": [
        {"term": "对的", "note": "解释 A"},
        {"term": "小错", "note": "解释 B"},
        {"term": "大错", "note": "解释 C"},
        {"term": "漏审", "note": "解释 D"},
    ]}
    conn.execute(
        "INSERT INTO topics (issue_date, board, title, thread_key, item_ids, decision,"
        " needs_image, created_at, background) VALUES"
        " ('2026-01-01','data','T','k','[]','brief',0,'x',?)",
        (json.dumps(bg0, ensure_ascii=False),))
    tid = conn.execute("SELECT id FROM topics").fetchone()["id"]
    conn.commit()

    backend = _FakeBackend(json.dumps({"topics": [{"id": tid, "concepts": [
        {"term": "对的", "verdict": "ok", "note": ""},
        {"term": "小错", "verdict": "fix", "note": "修正后的解释 B"},
        {"term": "大错", "verdict": "drop", "note": ""},
    ]}]}, ensure_ascii=False))
    stats = stage_checker(conn, load_config(), backend, "data", "2026-01-01")
    assert stats == {"checked": 0, "bg_reviewed": 1, "bg_fixed": 1, "bg_dropped": 1}
    bg = json.loads(conn.execute("SELECT background FROM topics WHERE id=?",
                                 (tid,)).fetchone()[0])
    assert {c["term"]: c["note"] for c in bg["concepts"]} == {
        "对的": "解释 A", "小错": "修正后的解释 B", "漏审": "解释 D"}
    assert bg["reviewed"] is True
    # 幂等：已审的不再进清单（FakeBackend 无剩余输出，再调用会炸 → 证明没调）
    assert stage_checker(conn, load_config(), backend, "data",
                         "2026-01-01") == {"checked": 0}
