"""前端数据契约测试：export_web 的 JSON 结构（web/ Astro 构建的输入）。

只测导出，不跑 npm 构建（保持测试无 Node 依赖）。
语义与 test_pipeline.test_render_site 同源：核查记录不上前端；
速览有报道则给 page 内链，旧数据 page=None 回退原文外链。
"""

import dataclasses
import json


def _seed(conn):
    conn.execute("INSERT INTO issues (issue_date, kind, status, updated_at)"
                 " VALUES ('2026-01-01','daily','written','x')")
    conn.execute(
        "INSERT INTO raw_items (source_id, board, kind, url, url_canonical, title,"
        " fetched_at, signals) VALUES ('hn-frontpage','academic','paper',"
        " 'http://orig.example/a','http://orig.example/a','速览原文','2026-01-01',"
        " '{\"oa_hindex\": 12, \"screen_score\": 8}')")
    item_id = conn.execute("SELECT id FROM raw_items").fetchone()["id"]
    conn.execute(
        "INSERT INTO topics (issue_date, board, title, thread_key, item_ids, decision,"
        " slot, needs_image, created_at, check_notes, reason) VALUES"
        " ('2026-01-01','academic','测试专题','test-topic',?,'feature','headline',0,'x',"
        " ?, '理由')",
        (json.dumps([item_id]),
         json.dumps({"claims": [{"claim": "论断A", "support": 1}], "notes": "备注"})))
    topic_id = conn.execute("SELECT id FROM topics").fetchone()["id"]
    conn.execute(
        "INSERT INTO topics (issue_date, board, title, thread_key, item_ids, decision,"
        " needs_image, created_at, reason) VALUES"
        " ('2026-01-01','academic','速览条目','brief-key',?,'brief',0,'x','十秒理由')",
        (json.dumps([item_id]),))
    brief_id = conn.execute(
        "SELECT id FROM topics WHERE thread_key='brief-key'").fetchone()["id"]
    conn.execute(
        "INSERT INTO topics (issue_date, board, title, thread_key, item_ids, decision,"
        " needs_image, created_at, reason) VALUES"
        " ('2026-01-01','academic','旧速览','brief-legacy',?,'brief',0,'x','老数据')",
        (json.dumps([item_id]),))
    conn.execute(
        "INSERT INTO articles (topic_id, card_summary, body_md, image_refs, created_at)"
        " VALUES (?,'卡片','## 小标题\n\n正文','[]','x')", (topic_id,))
    conn.execute(
        "INSERT INTO articles (topic_id, card_summary, body_md, image_refs, created_at)"
        " VALUES (?,'速览卡片','速览正文','[]','x')", (brief_id,))
    conn.commit()


def test_export_web(tmp_path):
    from rebas import db as database
    from rebas.config import load_config
    from rebas.render.export import export_web

    conn = database.init_db(tmp_path / "t.sqlite")
    _seed(conn)
    # 更早的一期：超出保留窗口（7 天）→ 应降为归档存目，且不出现在导航里
    conn.execute("INSERT INTO issues (issue_date, kind, status, updated_at)"
                 " VALUES ('2025-12-20','daily','written','x')")
    conn.commit()
    conf = dataclasses.replace(load_config(), site_dir=tmp_path / "site")
    data_dir = tmp_path / "data"
    stats = export_web(conn, conf, data_dir=data_dir)
    assert stats["issues"] == 2

    site = json.loads((data_dir / "site.json").read_text())
    assert site["latest"] == "2026-01-01"
    assert site["boards"][0]["id"] == "academic"
    assert site["boards"][0]["en"] == "ACADEMIC"

    # 保留窗口：归档期无页面数据、archived=true；窗口内期次导航不指向归档期
    assert not (data_dir / "issues" / "2025-12-20.json").exists()
    idx = {e["date"]: e for e in site["issues"]}
    assert idx["2025-12-20"]["archived"] is True
    assert idx["2026-01-01"]["archived"] is False
    assert idx["2026-01-01"]["titles"] == ["测试专题"]

    raw = (data_dir / "issues" / "2026-01-01.json").read_text()
    assert "论断A" not in raw and "check_notes" not in raw  # 核查记录不上前端

    issue = json.loads(raw)
    board = issue["boards"][0]
    hl = board["headline"]
    assert hl["title"] == "测试专题" and hl["slot"] == "headline"
    assert hl["kind"] == "paper"
    assert "H 12" in hl["meta"]                      # oa_hindex 信号 → 元数据
    assert "<h2>小标题</h2>" in hl["body_html"]       # markdown 已转 HTML
    assert hl["sources"][0]["url"] == "http://orig.example/a"

    briefs = {b["key"]: b for b in board["briefs"]}
    assert briefs["brief-key"]["page"] == "topic-2026-01-01-academic-brief-key.html"
    assert briefs["brief-legacy"]["page"] is None    # 旧数据无报道页
    assert briefs["brief-legacy"]["url"] == "http://orig.example/a"  # → 外链兜底


def test_math_rendering():
    """公式管线：LaTeX→MathML（构建期、零 JS）。块级/行内/围栏都接，
    货币与代码块防误伤，解析失败降级代码样式，nh3 白名单放行 MathML。"""
    from rebas.render.export import _md

    out = _md("$$\\widehat R^2 \\approx 1 + \\frac{M}{ESS}$$")
    assert 'display="block"' in out and "<mfrac>" in out   # 块级公式

    out = _md("其中 $M$ 是链数，\\(x_i\\) 是样本。")
    assert out.count('display="inline"') == 2              # 行内两种写法

    out = _md("```math\nE = mc^2\n```")
    assert "<msup>" in out                                 # math 围栏（先于代码保护）

    out = _md("融资 $100 million，估值涨了 $5。这个 $话里 有中文$ 不转。")
    assert "<math" not in out                              # 货币/中文防误伤

    out = _md("示例 `awk '$1==$2'`：\n```\necho $$PID\n```")
    assert "<math" not in out                              # 代码块内 $ 不碰

    out = _md("$$\\undefined@@macro{$$")
    assert "<math" not in out and "<code>" in out          # 坏语法降级不炸
