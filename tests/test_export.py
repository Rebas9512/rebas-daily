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


def test_export_multi_images(tmp_path):
    """多图排版契约：艺术/设计板块图库 ≥2 → images 列表（含过滤去重）；
    其余板块只出单图 image；学术板块维持不配图。"""
    from rebas import db as database
    from rebas.config import load_config
    from rebas.render.export import export_web

    conn = database.init_db(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO issues (issue_date, kind, status, updated_at)"
                 " VALUES ('2026-01-01','daily','written','x')")

    def seed(board, key, image_urls=None, image_url=None):
        conn.execute(
            "INSERT INTO raw_items (source_id, board, url, url_canonical, title,"
            " fetched_at, image_url, image_urls) VALUES ('s',?,?,?,'条目','x',?,?)",
            (board, f"http://x.com/{key}", f"http://x.com/{key}", image_url,
             json.dumps(image_urls) if image_urls else None))
        iid = conn.execute("SELECT max(id) FROM raw_items").fetchone()[0]
        conn.execute(
            "INSERT INTO topics (issue_date, board, title, thread_key, item_ids,"
            " decision, needs_image, created_at, reason) VALUES"
            " ('2026-01-01',?,?,?,?,'brief',0,'x','r')",
            (board, key, key, json.dumps([iid])))

    gallery = ["https://img.x/1.jpg", "https://img.x/2.jpg",
               "https://opengraph.githubassets.com/auto.png",  # 自动卡片过滤
               "https://img.x/1.jpg"]                          # 重复去重
    seed("design", "d-multi", image_urls=gallery, image_url="https://img.x/1.jpg")
    seed("design", "d-single", image_url="https://img.x/only.jpg")
    seed("tech", "t-multi", image_urls=["https://img.x/a.jpg", "https://img.x/b.jpg"])
    conn.commit()

    conf = dataclasses.replace(load_config(), site_dir=tmp_path / "site")
    export_web(conn, conf, data_dir=tmp_path / "data")
    issue = json.loads((tmp_path / "data" / "issues" / "2026-01-01.json").read_text())
    boards = {b["id"]: b for b in issue["boards"]}

    briefs = {t["key"]: t for t in boards["design"]["briefs"]}
    assert briefs["d-multi"]["images"] == ["https://img.x/1.jpg", "https://img.x/2.jpg"]
    assert briefs["d-multi"]["image"] == "https://img.x/1.jpg"
    assert "images" not in briefs["d-single"]            # 单图不出列表
    assert briefs["d-single"]["image"] == "https://img.x/only.jpg"

    t = {x["key"]: x for x in boards["tech"]["briefs"]}["t-multi"]
    assert "images" not in t                              # 非多图板块维持单图
    assert t["image"] == "https://img.x/a.jpg"


def test_export_image_plan(tmp_path):
    """撰写期图片审选的渲染契约：kept 即展示集（顺序=展示顺序，首张=头图）；
    正文 IMG 令牌 → 内联 figure（图注进 figcaption），头图/未保留令牌移除；
    kept=[] 是有效裁决 → 无图版式；旧文章（无 image_plan）回退条目图库。"""
    from rebas import db as database
    from rebas.config import load_config
    from rebas.render.export import export_web

    conn = database.init_db(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO issues (issue_date, kind, status, updated_at)"
                 " VALUES ('2026-01-01','daily','written','x')")
    u1, u2 = "https://img.x/1.jpg", "https://img.x/2.jpg"

    def seed(key, image_plan=None, body_md="正文"):
        conn.execute(
            "INSERT INTO raw_items (source_id, board, url, url_canonical, title,"
            " fetched_at, image_url) VALUES ('s','design',?,?,'条目','x',"
            " 'https://img.x/fallback.jpg')",
            (f"http://x.com/{key}", f"http://x.com/{key}"))
        iid = conn.execute("SELECT max(id) FROM raw_items").fetchone()[0]
        conn.execute(
            "INSERT INTO topics (issue_date, board, title, thread_key, item_ids,"
            " decision, needs_image, created_at, reason) VALUES"
            " ('2026-01-01','design',?,?,?,'feature',0,'x','r')",
            (key, key, json.dumps([iid])))
        tid = conn.execute("SELECT max(id) FROM topics").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (topic_id, card_summary, body_md, image_refs,"
            " image_plan, created_at) VALUES (?,'卡',?,'[]',?,'x')",
            (tid, body_md, image_plan))

    seed("reviewed",
         image_plan=json.dumps({"kept": [[2, u2], [1, u1]]}),
         body_md="开头段\n\n![展览现场](IMG1)\n\n![x](IMG2)\n\n![y](IMG3)\n\n结尾段")
    seed("dropped-all", image_plan=json.dumps({"kept": []}))
    seed("legacy")   # 无 image_plan → 回退条目图
    seed("inline-token", image_plan=json.dumps({"kept": [[1, u1], [2, u2]]}),
         body_md="令牌写进段落中间 ![图注](IMG2) 不插图但要剥干净。")
    seed("md-image", body_md="未审选文章自写外源图 ![x](https://evil.example/x.jpg)")
    seed("bad-plan", image_plan='{"kept": [["坏", "https://img.x/z.jpg"]]}')
    conn.commit()

    conf = dataclasses.replace(load_config(), site_dir=tmp_path / "site")
    export_web(conn, conf, data_dir=tmp_path / "data")
    issue = json.loads((tmp_path / "data" / "issues" / "2026-01-01.json").read_text())
    board = next(b for b in issue["boards"] if b["id"] == "design")
    topics = {t["key"]: t for t in
              ([board["headline"]] if board["headline"] else []) + board["features"]}

    r = topics["reviewed"]
    assert r["image"] == u2 and r["images"] == [u2, u1]   # kept 顺序，首张=头图
    assert f'<img src="{u1}"' in r["body_html"]            # IMG1 内联成 figure
    assert "<figcaption>展览现场</figcaption>" in r["body_html"]
    assert u2 not in r["body_html"]                        # 头图令牌移除（防双显）
    assert "IMG3" not in r["body_html"]                    # 未保留令牌整行移除
    assert r["images_inline"] == [u1]
    assert "开头段" in r["body_html"] and "结尾段" in r["body_html"]

    d = topics["dropped-all"]
    assert d["image"] is None and "images" not in d        # 全弃 → 无图版式

    lg = topics["legacy"]
    assert lg["image"] == "https://img.x/fallback.jpg"     # 旧数据回退条目图

    it = topics["inline-token"]
    assert "IMG2" not in it["body_html"]                   # 段落中间令牌剥掉不插图
    assert u2 not in it["body_html"] and "images_inline" not in it
    assert "剥干净" in it["body_html"]                      # 正文其余部分完好

    mi = topics["md-image"]
    assert "<img" not in mi["body_html"]                   # 未审选文章不放行外源图
    assert "evil.example" not in mi["body_html"]

    bp = topics["bad-plan"]                                # 坏 plan 当未审选，不崩导出
    assert bp["image"] == "https://img.x/fallback.jpg"


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
