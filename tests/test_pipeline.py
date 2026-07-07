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
                               existing_block="（本期该板块尚无选题——常规选题轮）",
                               recent_threads_block="（空）", items_block="[1] ...")
        assert "thread_key" in editor and "材料深度决定篇幅" in editor
        assert "本期已有选题" in editor
        # 薄材料新闻/repo 不压排版：主编被告知下游有调查编辑补材料
        assert "调查编辑" in editor and "仅标题" in editor
        checker = render_prompt("checker", topic_title="T", materials_block="[S1] ...")
        assert "multi|single|uncertain" in checker
        researcher = render_prompt("researcher", board_name=p.name,
                                   reader_block=reader_block(p), archive_days=30,
                                   archive_block="（30 天内无往期报道）",
                                   facts_max=6, topics_block="[T1] ...")
        assert "教科书级" in researcher and "宁缺毋滥" in researcher
        assert "AI/ML 从业者" in researcher   # 读者画像注入
        assert "need_articles" in researcher  # 往期全文索取协议
        assert "网络搜索工具" in researcher and "最多 6 条" in researcher  # 新闻调查补充
        r2 = render_prompt("researcher_articles", board_name=p.name,
                           reader_block=reader_block(p),
                           archive_articles_block="[A1] ...",
                           facts_max=6, topics_block="[T1] ...")
        assert "follow_up" in r2 and "往期报道全文" in r2
        assert "需调查补充" in r2             # 第二轮同样带调查补充协议
        bg_check = render_prompt("checker_background", items_block="[T1] ...")
        assert "ok|fix|drop" in bg_check and "宁删勿留" in bg_check
        assert "[F#]" in bg_check and "门槛放宽" in bg_check  # facts 按新闻口径审
        from rebas.agents.prompts import images_block
        writer = render_prompt("writer", board_name=p.name, topic_title="T",
                               reason="r", target_length=1000,
                               check_block="- ...", background_block="- R-hat：...",
                               images_block=images_block(
                                   [(1, "https://x.com/a.jpg")]),
                               materials_block="[S1] ...")
        assert "最多 3 条要点" in writer and "card_summary" in writer
        assert "写作基调" in writer      # style.md 自动注入
        assert "背景材料" in writer and "- R-hat：..." in writer
        assert "images_keep" in writer and "图片编辑" in writer  # 图片审选协议
        assert "IMG编号" in writer                                # 正文插图令牌
        brief = render_prompt("writer_brief", board_name=p.name, topic_title="T",
                              reason="r", target_length=300,
                              background_block="（无背景材料）",
                              images_block=images_block([], is_brief=True),
                              materials_block="[S1] ...")
        assert "card_summary" in brief and "不用小标题" in brief
        assert "写作基调" in brief       # style.md 同样注入速览模板
        assert "本篇无图片材料" in brief  # 无图时材料块为占位说明

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
        # 新闻调查补充 facts：单独成节，带来源归因指引；无来源的给"公开报道"兜底
        raw2 = ('{"context":"","concepts":[],"facts":['
                '{"fact":"公司 A 于周一宣布收购","source":"Reuters"},'
                '{"fact":"交易额 3 亿美元","source":""}]}')
        block2 = background_block(raw2)
        assert "调查补充" in block2 and "据 Reuters 报道" in block2
        assert "- 公司 A 于周一宣布收购（来源：Reuters）" in block2
        assert "- 交易额 3 亿美元（来源：公开报道）" in block2


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
        self.images = []          # 每次调用收到的图片附件路径（撰写期图片审选）

    def complete(self, prompt, *, role="default", images=()):
        self.prompts.append(prompt)
        self.images.append(tuple(images))
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
    # 条目是 2 字摘要的 article → 两题都够薄材料新闻资格（investigated=2），模型没给 facts
    assert stats == {"researched": 2, "with_background": 1, "archive_read": 0,
                     "investigated": 2, "with_facts": 0}
    assert "夏普论文" in backend.prompts[0]           # 材料节选入提示词
    assert "金融概念要铺垫" in backend.prompts[0]     # 读者画像入提示词
    assert "30 天内无往期报道" in backend.prompts[0]  # 空书架如实呈现
    assert "【需调查补充】" in backend.prompts[0]     # 薄材料新闻选题带标注

    bg1 = json.loads(conn.execute(
        "SELECT background FROM topics WHERE id=?", (t1,)).fetchone()[0])
    assert bg1 == {"context": "资产定价",
                   "concepts": [{"term": "夏普比率", "note": "单位风险的超额收益"}],
                   "facts": [], "follow_up": ""}
    bg2 = json.loads(conn.execute(
        "SELECT background FROM topics WHERE id=?", (t2,)).fetchone()[0])
    assert bg2 == {"context": "", "concepts": [], "facts": [],
                   "follow_up": ""}  # 未覆盖也落空产物 = 幂等标记
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
    assert stats == {"researched": 1, "with_background": 1, "archive_read": 1,
                     "investigated": 1, "with_facts": 0}
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
    assert stats == {"checked": 0, "bg_reviewed": 1, "bg_fixed": 1, "bg_dropped": 1,
                     "bg_facts_fixed": 0, "bg_facts_dropped": 0}
    bg = json.loads(conn.execute("SELECT background FROM topics WHERE id=?",
                                 (tid,)).fetchone()[0])
    assert {c["term"]: c["note"] for c in bg["concepts"]} == {
        "对的": "解释 A", "小错": "修正后的解释 B", "漏审": "解释 D"}
    assert bg["reviewed"] is True
    # 幂等：已审的不再进清单（FakeBackend 无剩余输出，再调用会炸 → 证明没调）
    assert stage_checker(conn, load_config(), backend, "data",
                         "2026-01-01") == {"checked": 0}


def test_facts_eligibility(tmp_path):
    """调查补充资格：新闻按仅标题级；论文以原文缓存是否到手为准（2026-07-07）。"""
    import dataclasses

    from rebas.agents.stages import _facts_eligible
    from rebas.config import load_config

    conf = dataclasses.replace(load_config(), data_dir=tmp_path)

    def row(kind="article", text=None, summary=None, id=1,
            url="https://doi.org/10.1/x"):
        return {"id": id, "kind": kind, "extracted_text": text, "summary": summary,
                "url": url, "url_canonical": url}

    assert _facts_eligible(conf, [row(summary="仅标题级短摘要")])
    assert _facts_eligible(conf, [row(kind="repo", summary="短描述")])   # repo 也按新闻口径
    assert not _facts_eligible(conf, [row(text="厚" * 600)])              # 材料够厚不需要

    # 论文：原文拿不到（无缓存）→ 放行调查；摘要级厚度（<2000）也算薄
    assert _facts_eligible(conf, [row(kind="paper", summary="短")])
    assert _facts_eligible(conf, [row(kind="paper", summary="摘" * 1500)])
    # 材料超过论文门槛的不投
    assert not _facts_eligible(conf, [row(kind="paper", text="厚" * 2500)])
    # 原文缓存已到手（取材期精读成功）→ 不调查
    conf.paper_cache_dir.mkdir(parents=True)
    (conf.paper_cache_dir / "7.txt").write_text("论文原文", encoding="utf-8")
    assert not _facts_eligible(conf, [row(kind="paper", summary="短", id=7)])
    # 混合选题按论文口径：缓存归属条目（首个论文条目）有缓存 → 不调查
    assert not _facts_eligible(
        conf, [row(summary="短", id=3), row(kind="paper", summary="短", id=7)])


def test_stage_research_facts(tmp_path):
    """新闻调查补充：仅薄材料新闻题标注；facts 按上限截断；未标注题的 facts 被剥掉。"""
    import dataclasses
    import json

    from rebas import db as database
    from rebas.agents.stages import FACTS_MARK, stage_research
    from rebas.config import load_config

    conn = database.init_db(tmp_path / "t.sqlite")
    conn.execute(
        "INSERT INTO raw_items (source_id, board, url, url_canonical, title, summary,"
        " fetched_at) VALUES ('s','tech','u1','u1','只有标题的新闻',NULL,'x')")
    conn.execute(
        "INSERT INTO raw_items (source_id, board, url, url_canonical, title,"
        " extracted_text, fetched_at) VALUES ('s','tech','u2','u2','材料充足的新闻',?,'x')",
        ("厚" * 600,))
    thin_iid, fat_iid = [r["id"] for r in conn.execute(
        "SELECT id FROM raw_items ORDER BY id")]

    def add_topic(key, iid):
        conn.execute(
            "INSERT INTO topics (issue_date, board, title, thread_key, item_ids,"
            " decision, needs_image, created_at, reason) VALUES"
            " ('2026-07-06','tech',?,?,?,'brief',0,'x','r')",
            (key, key, json.dumps([iid])))
        return conn.execute("SELECT id FROM topics WHERE thread_key=?",
                            (key,)).fetchone()["id"]

    t_thin = add_topic("thin-news", thin_iid)
    t_fat = add_topic("fat-news", fat_iid)
    conn.commit()

    profile = Profile(board="tech", name="科技", interests=(),
                      reader_assumed="", reader_explain="科技新闻读者")
    backend = _FakeBackend(json.dumps({"topics": [
        {"id": t_thin, "context": "", "concepts": [], "facts": [
            {"fact": "事实一", "source": "Reuters"},
            {"fact": "事实二", "source": "官方博客"},
            {"fact": "事实三（超上限该截掉）", "source": "X"},
            {"fact": ""},                       # 空 fact 容错跳过
        ]},
        {"id": t_fat, "context": "", "concepts": [], "facts": [
            {"fact": "越权补充（未标注题不许有）", "source": "Y"}]},
    ]}, ensure_ascii=False))
    conf = dataclasses.replace(load_config(), research_facts_max=2)
    stats = stage_research(conn, conf, backend, "tech", profile, "科技", "2026-07-06")
    assert stats["investigated"] == 1 and stats["with_facts"] == 1
    assert f"thin-news{FACTS_MARK}" in backend.prompts[0]     # 薄题带标注
    fat_header = next(ln for ln in backend.prompts[0].splitlines()
                      if ln.startswith(f"[T{t_fat}]"))
    assert FACTS_MARK not in fat_header                       # 厚材料题无标注

    bg_thin = json.loads(conn.execute("SELECT background FROM topics WHERE id=?",
                                      (t_thin,)).fetchone()[0])
    assert bg_thin["facts"] == [{"fact": "事实一", "source": "Reuters"},
                                {"fact": "事实二", "source": "官方博客"}]  # 截到上限
    bg_fat = json.loads(conn.execute("SELECT background FROM topics WHERE id=?",
                                     (t_fat,)).fetchone()[0])
    assert bg_fat["facts"] == []       # 未标注题的 facts 代码层剥掉

    # research_facts_max=0 → 整体关闭，无题被标注
    conn.execute("UPDATE topics SET background=NULL")
    conn.commit()
    backend0 = _FakeBackend(json.dumps({"topics": []}))
    conf0 = dataclasses.replace(load_config(), research_facts_max=0)
    stats0 = stage_research(conn, conf0, backend0, "tech", profile, "科技", "2026-07-06")
    assert stats0["investigated"] == 0
    assert not any(FACTS_MARK in ln for ln in backend0.prompts[0].splitlines()
                   if ln.startswith("[T"))    # 关闭后无题被标注


def test_checker_background_review_facts(tmp_path):
    """facts 审核：ok 保留 / fix 换文本留来源 / drop 删 / 漏裁决保守保留；幂等。"""
    import json

    from rebas import db as database
    from rebas.agents.stages import stage_checker
    from rebas.config import load_config

    conn = database.init_db(tmp_path / "t.sqlite")
    bg0 = {"context": "", "follow_up": "", "concepts": [], "facts": [
        {"fact": "对的事实", "source": "Reuters"},
        {"fact": "小错事实", "source": "官方博客"},
        {"fact": "大错事实", "source": "X"},
        {"fact": "漏审事实", "source": ""},
    ]}
    conn.execute(
        "INSERT INTO topics (issue_date, board, title, thread_key, item_ids, decision,"
        " needs_image, created_at, background) VALUES"
        " ('2026-01-01','tech','T','k','[]','brief',0,'x',?)",
        (json.dumps(bg0, ensure_ascii=False),))
    tid = conn.execute("SELECT id FROM topics").fetchone()["id"]
    conn.commit()

    backend = _FakeBackend(json.dumps({"topics": [{"id": tid, "facts": [
        {"i": 1, "verdict": "ok", "note": ""},
        {"i": 2, "verdict": "fix", "note": "修正后的事实"},
        {"i": 3, "verdict": "drop", "note": ""},
        {"i": "非法序号", "verdict": "drop"},        # 容错跳过
    ]}]}, ensure_ascii=False))
    stats = stage_checker(conn, load_config(), backend, "tech", "2026-01-01")
    assert stats == {"checked": 0, "bg_reviewed": 1, "bg_fixed": 0, "bg_dropped": 0,
                     "bg_facts_fixed": 1, "bg_facts_dropped": 1}
    assert "[F2] 小错事实（来源：官方博客）" in backend.prompts[0]  # 审核清单带编号与来源
    bg = json.loads(conn.execute("SELECT background FROM topics WHERE id=?",
                                 (tid,)).fetchone()[0])
    assert bg["facts"] == [
        {"fact": "对的事实", "source": "Reuters"},
        {"fact": "修正后的事实", "source": "官方博客"},   # fix 换文本、来源保留
        {"fact": "漏审事实", "source": ""},               # 漏裁决保守保留
    ]
    assert bg["reviewed"] is True
    # 幂等：已审的不再进清单（FakeBackend 无剩余输出，再调用会炸 → 证明没调）
    assert stage_checker(conn, load_config(), backend, "tech",
                         "2026-01-01") == {"checked": 0}


class TestEditorRefill:
    """补充轮（2026-07-05）：门控逻辑不打模型即可验证。"""

    def _setup(self, tmp_path, n_topics):
        from rebas import db as database
        conn = database.init_db(tmp_path / "t.sqlite")
        conn.execute("INSERT INTO issues (issue_date, status, updated_at)"
                     " VALUES ('2026-07-06', 'pending', 'x')")
        for i in range(n_topics):
            conn.execute(
                "INSERT INTO topics (issue_date, board, title, thread_key, item_ids,"
                " decision, created_at) VALUES ('2026-07-06','art',?,?,'[]','brief','x')",
                (f"T{i}", f"k{i}"))
        return conn

    def _profile(self):
        return Profile(board="art", name="艺术", interests=(),
                       reader_assumed="", reader_explain="")

    def test_enough_topics_skips(self, tmp_path):
        from rebas.agents.stages import stage_editor
        from rebas.config import load_config
        conn = self._setup(tmp_path, 6)      # refill_min_topics=6 → 已足
        s = stage_editor(conn, load_config(), None, "art", self._profile(),
                         "艺术", "2026-07-06", refill=True)
        assert "选题充足" in s["skipped"]

    def test_thin_board_but_no_candidates(self, tmp_path):
        from rebas.agents.stages import stage_editor
        from rebas.config import load_config
        conn = self._setup(tmp_path, 3)      # 少于阈值 → 进补选，但池子空
        s = stage_editor(conn, load_config(), None, "art", self._profile(),
                         "艺术", "2026-07-06", refill=True)
        assert s["skipped"] == "无入围候选"

    def test_without_refill_flag_keeps_old_behavior(self, tmp_path):
        from rebas.agents.stages import stage_editor
        from rebas.config import load_config
        conn = self._setup(tmp_path, 1)
        s = stage_editor(conn, load_config(), None, "art", self._profile(),
                         "艺术", "2026-07-06")
        assert s["skipped"] == "topics 已存在"


def _writer_brief_conn(tmp_path, summary="这是摘要，比较薄"):
    import json

    from rebas import db as database
    conn = database.init_db(tmp_path / "t.sqlite")
    conn.execute(
        "INSERT INTO raw_items (source_id, board, url, url_canonical, title, summary,"
        " kind, fetched_at) VALUES"
        " ('arxiv-ai','academic','https://arxiv.org/abs/2507.01234',"
        " 'https://arxiv.org/abs/2507.01234','论文标题',?,'paper','x')", (summary,))
    iid = conn.execute("SELECT id FROM raw_items").fetchone()["id"]
    conn.execute(
        "INSERT INTO topics (issue_date, board, title, thread_key, item_ids, decision,"
        " needs_image, created_at, reason) VALUES"
        " ('2026-01-01','academic','速览题','k',?,'brief',0,'x','r')",
        (json.dumps([iid]),))
    conn.commit()
    return conn, iid


def test_stage_writer_brief_reads_fulltext(tmp_path):
    """论文类速览也精读（2026-07-06）：brief 加载原文、裁到速览上限、写完即删缓存。"""
    import dataclasses
    import json

    from rebas.agents.stages import stage_writer
    from rebas.config import load_config

    conn, iid = _writer_brief_conn(tmp_path)
    conf = dataclasses.replace(load_config(), data_dir=tmp_path,
                               paper_brief_fulltext_max_chars=30)
    conf.paper_cache_dir.mkdir(parents=True, exist_ok=True)
    (conf.paper_cache_dir / f"{iid}.txt").write_text("原" * 100, encoding="utf-8")

    backend = _FakeBackend(json.dumps({"card_summary": "卡", "body_md": "正文"},
                                      ensure_ascii=False))
    assert stage_writer(conn, conf, backend, "academic", "学术", "2026-01-01") == {
        "written": 1}
    prompt = backend.prompts[0]
    assert "已抓取的全文" in prompt              # 速览也拿到原文（materials_block 精读标注）
    assert "原" * 30 in prompt and "原" * 31 not in prompt   # 裁到 brief 上限
    assert not (conf.paper_cache_dir / f"{iid}.txt").exists()   # 写完即删
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1


def test_stage_writer_brief_fulltext_disabled(tmp_path):
    """brief 精读上限=0 → 回旧行为：速览只用摘要，不加载也不删缓存。"""
    import dataclasses
    import json

    from rebas.agents.stages import stage_writer
    from rebas.config import load_config

    conn, iid = _writer_brief_conn(tmp_path)
    conf = dataclasses.replace(load_config(), data_dir=tmp_path,
                               paper_brief_fulltext_max_chars=0)
    conf.paper_cache_dir.mkdir(parents=True, exist_ok=True)
    (conf.paper_cache_dir / f"{iid}.txt").write_text("不该被读到的原文", encoding="utf-8")

    backend = _FakeBackend(json.dumps({"card_summary": "卡", "body_md": "正文"},
                                      ensure_ascii=False))
    stage_writer(conn, conf, backend, "academic", "学术", "2026-01-01")
    prompt = backend.prompts[0]
    assert "已抓取的全文" not in prompt and "这是摘要" in prompt   # 未加载原文，用摘要
    assert (conf.paper_cache_dir / f"{iid}.txt").exists()   # 未触碰


def _writer_design_conn(tmp_path):
    import json

    from rebas import db as database
    conn = database.init_db(tmp_path / "t.sqlite")
    conn.execute(
        "INSERT INTO raw_items (source_id, board, url, url_canonical, title, summary,"
        " fetched_at) VALUES ('designboom','design','https://x.com/p','https://x.com/p',"
        " '设计条目','摘要文字','x')")
    iid = conn.execute("SELECT id FROM raw_items").fetchone()["id"]
    conn.execute(
        "INSERT INTO topics (issue_date, board, title, thread_key, item_ids, decision,"
        " needs_image, created_at, reason) VALUES"
        " ('2026-01-01','design','设计题','k',?,'feature',0,'x','r')",
        (json.dumps([iid]),))
    conn.commit()
    return conn


def test_stage_writer_image_review(tmp_path, monkeypatch):
    """撰写期图片审选：候选图附给 writer、images_keep 按展示顺序落 image_plan
    （幽灵编号忽略、去重），临时图用完即删。"""
    import dataclasses
    import json

    from rebas.agents import stages
    from rebas.config import load_config

    conn = _writer_design_conn(tmp_path)
    conf = dataclasses.replace(load_config(), data_dir=tmp_path)
    p1, p2 = tmp_path / "t1-1.jpg", tmp_path / "t1-2.jpg"
    p1.write_bytes(b"x")
    p2.write_bytes(b"y")
    monkeypatch.setattr(
        stages, "_prepare_topic_images",
        lambda *_: [(1, "https://img.x/1.jpg", p1), (2, "https://img.x/2.jpg", p2)])

    backend = _FakeBackend(json.dumps({
        "card_summary": "卡", "body_md": "正文\n\n![现场](IMG2)",
        "images_keep": [2, 1, 99, 2]}, ensure_ascii=False))
    assert stages.stage_writer(conn, conf, backend, "design", "设计",
                               "2026-01-01") == {"written": 1}
    assert "图1: https://img.x/1.jpg" in backend.prompts[0]   # 图片材料块
    assert "images_keep" in backend.prompts[0]
    assert backend.images[0] == (p1, p2)                       # 附件按编号顺序
    plan = json.loads(conn.execute(
        "SELECT image_plan FROM articles").fetchone()[0])
    assert plan == {"kept": [[2, "https://img.x/2.jpg"],
                             [1, "https://img.x/1.jpg"]]}      # 顺序保留，99/重复忽略
    assert not p1.exists() and not p2.exists()                 # 用完即删


def test_stage_writer_image_review_drop_all_and_fallback(tmp_path, monkeypatch):
    """全弃 = kept 空列表照样落库（有效裁决→无图版式）；带图调用失败时摘图重试，
    本篇降级为不审选（image_plan NULL 回退旧行为）。"""
    import dataclasses
    import json

    from rebas.agents import stages
    from rebas.config import load_config
    from rebas.llm import LLMError

    # 场景 1：全弃
    conn = _writer_design_conn(tmp_path)
    conf = dataclasses.replace(load_config(), data_dir=tmp_path)
    p1 = tmp_path / "a.jpg"
    p1.write_bytes(b"x")
    monkeypatch.setattr(stages, "_prepare_topic_images",
                        lambda *_: [(1, "https://img.x/1.jpg", p1)])
    backend = _FakeBackend(json.dumps(
        {"card_summary": "卡", "body_md": "正文", "images_keep": []},
        ensure_ascii=False))
    stages.stage_writer(conn, conf, backend, "design", "设计", "2026-01-01")
    assert json.loads(conn.execute("SELECT image_plan FROM articles").fetchone()[0]) \
        == {"kept": []}

    # 场景 2：带图调用炸 → 摘图重试成功，image_plan 为 NULL
    conn2 = _writer_design_conn(tmp_path / "b")
    p2 = tmp_path / "b.jpg"
    p2.write_bytes(b"x")
    monkeypatch.setattr(stages, "_prepare_topic_images",
                        lambda *_: [(1, "https://img.x/1.jpg", p2)])

    class _FlakyImages(_FakeBackend):
        def complete(self, prompt, *, role="default", images=()):
            if images:
                raise LLMError("附件失败")
            return super().complete(prompt, role=role, images=images)

    backend2 = _FlakyImages(json.dumps(
        {"card_summary": "卡", "body_md": "正文"}, ensure_ascii=False))
    assert stages.stage_writer(conn2, conf, backend2, "design", "设计",
                               "2026-01-01") == {"written": 1}
    assert "本篇无图片材料" in backend2.prompts[0]   # 重试提示词已摘图
    assert conn2.execute("SELECT image_plan FROM articles").fetchone()[0] is None
    assert not p2.exists()


def test_stage_writer_shared_paper_cache_refcount(tmp_path):
    """同一论文被选进两个选题（brief+feature）时，先写的选题不能删掉后写选题仍需要的
    精读缓存——引用计数归零才清（回归锁定：2026-07-06 review 发现的缓存误删）。"""
    import dataclasses
    import json

    from rebas import db as database
    from rebas.agents.stages import stage_writer
    from rebas.config import load_config

    conn = database.init_db(tmp_path / "t.sqlite")
    conn.execute(
        "INSERT INTO raw_items (source_id, board, url, url_canonical, title, summary,"
        " kind, fetched_at) VALUES"
        " ('arxiv-ai','academic','https://arxiv.org/abs/2507.09999',"
        " 'https://arxiv.org/abs/2507.09999','共享论文','薄摘要','paper','x')")
    xid = conn.execute("SELECT id FROM raw_items").fetchone()["id"]
    # B（brief）先插入 → 更小 topic id → writer 先处理；F（feature）后
    for key, dec in (("b-brief", "brief"), ("f-feat", "feature")):
        conn.execute(
            "INSERT INTO topics (issue_date, board, title, thread_key, item_ids,"
            " decision, needs_image, created_at, reason) VALUES"
            " ('2026-01-01','academic',?,?,?,?,0,'x','r')",
            (key, key, json.dumps([xid]), dec))
    conn.commit()

    conf = dataclasses.replace(load_config(), data_dir=tmp_path,
                               paper_brief_fulltext_max_chars=8000)
    conf.paper_cache_dir.mkdir(parents=True, exist_ok=True)
    (conf.paper_cache_dir / f"{xid}.txt").write_text("原文正文" * 500, encoding="utf-8")

    backend = _FakeBackend(
        json.dumps({"card_summary": "c1", "body_md": "b1"}, ensure_ascii=False),
        json.dumps({"card_summary": "c2", "body_md": "b2"}, ensure_ascii=False))
    assert stage_writer(conn, conf, backend, "academic", "学术", "2026-01-01") == {
        "written": 2}
    assert all("已抓取的全文" in p for p in backend.prompts)   # 两篇都拿到原文，无降级
    assert not (conf.paper_cache_dir / f"{xid}.txt").exists()  # 全写完才清缓存
