"""采集层单元测试：URL 规范化、预筛匹配、解析器、入库合并语义。"""

import json
from datetime import datetime, timedelta, timezone

from rebas import db as database
from rebas.collect import arxiv, boards
from rebas.collect.base import KeywordMatcher, canonicalize_url
from rebas.config import Interest, Profile, Source
from rebas.models import RawItem


def make_profile(**kw):
    return Profile(
        board="academic", name="t",
        interests=(Interest(name="t", weight=5,
                            keywords=kw.get("keywords", ("RL", "agent", "world model"))),),
        blocklist=kw.get("blocklist", ()),
    )


def make_source(**kw):
    defaults = dict(id="t", board="academic", name="t", type="rss",
                    endpoint="http://x", content="headline",
                    fetch_interval_hours=4, enabled=True)
    return Source(**{**defaults, **kw})


class TestCanonicalize:
    def test_strips_tracking_and_fragment(self):
        url = "HTTPS://Example.com/A/b/?utm_source=x&id=3&fbclid=y#frag"
        assert canonicalize_url(url) == "https://example.com/A/b?id=3"

    def test_trailing_slash(self):
        assert canonicalize_url("http://a.com/x/") == canonicalize_url("http://a.com/x")


class TestKeywordMatcher:
    def test_word_boundary_and_suffix(self):
        m = KeywordMatcher(make_profile())
        assert m.matches("Offline RL benchmarks")          # 精确
        assert m.matches("RLHF for language models")       # 后缀扩展
        assert m.matches("Multi-agent systems")            # 前有连字符=词边界
        assert m.matches("World models for robotics")      # 词组
        assert not m.matches("The world is round")         # 词组不拆散
        assert not m.matches("curl and URL parsing")       # RL 不做子串误命中

    def test_blocklist_wins(self):
        m = KeywordMatcher(make_profile(blocklist=("survey",)))
        assert not m.matches("A survey of RL methods")


ARXIV_FIXTURE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Deep RL for Robots</title>
<link>https://arxiv.org/abs/2507.00001</link>
<description>arXiv:2507.00001 Announce Type: new\nAbstract: We study RL.</description>
<category>cs.LG</category></item>
<item><title>RL Paper Updated Again</title>
<link>https://arxiv.org/abs/2401.99999</link>
<description>arXiv:2401.99999 Announce Type: replace\nAbstract: Old RL paper.</description>
<category>cs.LG</category></item>
<item><title>Watching Paint Dry</title>
<link>https://arxiv.org/abs/2507.00002</link>
<description>arXiv:2507.00002 Announce Type: new\nAbstract: Nothing relevant.</description>
<category>cs.OH</category></item>
</channel></rss>"""


def test_arxiv_parser_filters():
    # prefilter=true（学术 arXiv 配置）：replace 类公告直接丢，不相关的计入预筛
    items, filtered = arxiv.parse_arxiv_rss(
        make_source(type="arxiv_rss", prefilter=True), ARXIV_FIXTURE,
        matcher=KeywordMatcher(make_profile()))
    assert [i.title for i in items] == ["Deep RL for Robots"]
    assert filtered == 1
    assert items[0].kind == "paper"
    assert items[0].url_canonical == "https://arxiv.org/abs/2507.00001"
    assert items[0].summary == "We study RL."


GH_FIXTURE = ("""junk<article class="Box-row">
<h2 class="h3 lh-condensed"><a href="/openai/codex-plugin-cc">x</a></h2>
<p class="col-9 color-fg-muted my-1 pr-4">A codex plugin.</p>
<span itemprop="programmingLanguage">Rust</span>
<span>2,804 stars today</span></article>""").encode()


def test_gh_trending_parser():
    items, _ = boards.parse_gh_trending(make_source(type="gh_trending"), GH_FIXTURE)
    assert len(items) == 1
    it = items[0]
    assert it.title == "openai/codex-plugin-cc"
    assert it.kind == "repo"
    assert it.signals == {"stars_today": 2804, "language": "Rust"}
    assert it.summary == "A codex plugin."


class TestInsertSemantics:
    def _item(self, **kw):
        defaults = dict(source_id="t", board="academic", url="https://arxiv.org/abs/1",
                        url_canonical="https://arxiv.org/abs/1", title="T", kind="paper")
        return RawItem(**{**defaults, **kw})

    def test_merge_signals(self, tmp_path):
        conn = database.init_db(tmp_path / "t.sqlite")
        assert database.insert_item(conn, self._item()) == "new"
        # HF papers 带热度信号进来 → 合并而非丢弃
        out = database.insert_item(conn, self._item(signals={"hf_upvotes": 42}))
        assert out == "merged"
        row = conn.execute("SELECT signals FROM raw_items").fetchone()
        assert "hf_upvotes" in row["signals"]
        # 完全重复 → dup
        assert database.insert_item(conn, self._item()) == "dup"

    def test_merge_image_urls(self, tmp_path):
        conn = database.init_db(tmp_path / "t.sqlite")
        database.insert_item(conn, self._item())
        # 升级前入库的旧条目：feed 再采带图库 → 回填
        out = database.insert_item(
            conn, self._item(image_urls=["https://x.com/1.jpg", "https://x.com/2.jpg"]))
        assert out == "merged"
        row = conn.execute("SELECT image_urls FROM raw_items").fetchone()
        assert json.loads(row["image_urls"]) == ["https://x.com/1.jpg",
                                                 "https://x.com/2.jpg"]
        # 已有图库不覆盖
        assert database.insert_item(
            conn, self._item(image_urls=["https://x.com/other.jpg"])) == "dup"

    def test_revive_window(self, tmp_path):
        conn = database.init_db(tmp_path / "t.sqlite")
        database.insert_item(conn, self._item())
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        conn.execute("UPDATE raw_items SET status='dropped', fetched_at=?", (old,))
        # 30 天后重新上榜 → revived 且状态复位
        out = database.insert_item(conn, self._item(), revive_days=14)
        assert out == "revived"
        assert conn.execute("SELECT status FROM raw_items").fetchone()["status"] == "new"
        # 未过窗口的处理过条目 → 保持 dup
        conn.execute("UPDATE raw_items SET status='dropped'")
        assert database.insert_item(conn, self._item(), revive_days=14) == "dup"


def test_all_images():
    """图库提取：顺序保留、噪声过滤、相对路径补全、WP 多尺寸归一去重、封顶。"""
    from rebas.collect.base import all_images

    html = (
        '<img src="https://cdn.x.com/a.jpg">'
        '<img src="https://cdn.x.com/a-150x150.jpg">'      # 同图缩略尺寸 → 去重
        '<img src="/uploads/b.png">'                        # 相对路径按 base 补全
        '<img src="https://x.com/logo.svg">'                # 矢量图标 → 过滤
        '<img src="data:image/gif;base64,xx">'              # data URI → 过滤
        '<img src="https://secure.gravatar.com/avatar/x.jpg">'  # 头像 → 过滤
        '<img src="https://cdn.x.com/c.jpg?w=1200">'
    )
    imgs = all_images(html, base_url="https://x.com/post")
    assert imgs == ["https://cdn.x.com/a.jpg", "https://x.com/uploads/b.png",
                    "https://cdn.x.com/c.jpg?w=1200"]
    # 无基准的相对路径丢弃；cap 生效
    assert all_images('<img src="/rel.jpg">') == []
    many = "".join(f'<img src="https://x.com/{i}.jpg">' for i in range(9))
    assert len(all_images(many)) == 6


def test_feed_gallery():
    """feed 解析的正文图库：媒体主图作种子 + 正文多图，统一去重进 image_urls。"""
    from rebas.collect.feeds import parse_feed

    rss = b"""<?xml version="1.0"?><rss version="2.0"
        xmlns:media="http://search.yahoo.com/mrss/"
        xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
        <item><title>Gallery Post</title><link>https://x.com/post</link>
        <media:thumbnail url="https://cdn.x.com/lead-300x200.jpg"/>
        <content:encoded><![CDATA[
          <img src="https://cdn.x.com/lead.jpg">
          <img src="https://cdn.x.com/two.jpg">
          <img src="https://cdn.x.com/three.jpg">
        ]]></content:encoded>
        </item></channel></rss>"""
    items, _ = parse_feed(make_source(board="design"), rss, conn=None, client=None)
    it = items[0]
    assert it.image_url == "https://cdn.x.com/lead-300x200.jpg"   # 单图口径不变
    # 图库：主图种子在前，与正文里的原尺寸版归一去重
    assert it.image_urls == ["https://cdn.x.com/lead-300x200.jpg",
                             "https://cdn.x.com/two.jpg", "https://cdn.x.com/three.jpg"]


def test_feed_kind_override():
    """期刊 TOC 源 kind="paper" 覆盖生效；缺省源保持 article（沉淀期口径依赖）。"""
    from rebas.collect.feeds import parse_feed

    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Some Paper</title><link>http://j.org/p1</link>
        <pubDate>Fri, 03 Jul 2026 00:00:00 GMT</pubDate></item>
        </channel></rss>"""
    journal = make_source(id="nature", kind="paper")
    items, _ = parse_feed(journal, rss, conn=None, client=None)
    assert items[0].kind == "paper"
    plain = make_source(id="blog")
    items, _ = parse_feed(plain, rss, conn=None, client=None)
    assert items[0].kind == "article"


class TestJournalCollectors:
    """顶刊通道：OpenAlex 目录解析 + JMLR 卷页解析（arXiv 映射）。"""

    def test_openalex_journal_parser(self):
        from rebas.collect.journals import parse_openalex_journal

        payload = {"results": [
            {"display_name": "Adaptive Robust Confidence Intervals",
             "publication_date": "2026-06-01",          # 期号日期（会超窗，不应采用）
             "created_date": "2026-07-03",
             "doi": "https://doi.org/10.1214/xx",
             "locations": [{"landing_page_url": "https://doi.org/10.1214/xx"},
                           {"landing_page_url": "https://arxiv.org/abs/2301.01234v2"}],
             "abstract_inverted_index": {"robust": [1], "Adaptive": [0], "intervals.": [2]},
             "authorships": [{"author": {"display_name": "A. Zhang"}},
                             {"author": {"display_name": "B. Li"}}],
             "cited_by_count": 7},
            {"display_name": "No arXiv Version Here",
             "created_date": "2026-07-02",
             "doi": "https://doi.org/10.1214/yy",
             "locations": [], "authorships": []},
        ]}
        src = make_source(id="oa-test", board="data", type="openalex_journal")
        items, _ = parse_openalex_journal(src, json.dumps(payload).encode())
        assert len(items) == 2
        a, b = items
        assert a.kind == "paper"
        assert a.url == "https://arxiv.org/abs/2301.01234"     # arXiv 版优先且剥 vN
        assert a.published_at.startswith("2026-07-03")          # created_date 而非期号日期
        assert a.summary == "Adaptive robust intervals."
        assert a.author == "A. Zhang 等"
        assert a.signals["venue"] == "t" or a.signals["venue"]  # venue=源名
        assert a.signals["oa_paper_cites"] == 7
        assert b.url == "https://doi.org/10.1214/yy"            # 无 arXiv 退回 DOI

    def test_jmlr_volume_parser(self):
        from rebas.collect.journals import parse_jmlr_volume

        html = b"""<dl>
        <dt>Known Old Paper</dt>
        <dd><b><i>X. Yu</i></b>; (1):1-10, 2026.
        <br>[<a href='/papers/v27/26-0001.html'>abs</a>]</dd>
        <dt>Certified Machine Unlearning</dt>
        <dd><b><i>H. Zou, A. Auddy</i></b>; (2):1-58, 2026.
        <br>[<a href='/papers/v27/26-0002.html'>abs</a>]</dd>
        </dl>"""

        class FakeResp:
            status_code = 200
            text = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
                <id>http://arxiv.org/abs/2506.09999v1</id>
                <title>Certified Machine  Unlearning</title></entry></feed>"""

        class FakeClient:
            def get(self, url):
                return FakeResp()

        src = make_source(id="jmlr", type="jmlr_volume",
                          endpoint="https://www.jmlr.org/papers/v27/")
        items, _ = parse_jmlr_volume(src, html, conn=None, client=FakeClient())
        assert len(items) == 2
        assert items[0].url == "https://www.jmlr.org/papers/v27/26-0001.html"  # 标题不匹配退回 JMLR 链接
        assert items[1].url == "https://arxiv.org/abs/2506.09999"              # 精确匹配（含多空格归一）
        assert items[1].signals["jmlr_url"].endswith("26-0002.html")
        assert items[1].author == "H. Zou 等"
        assert all(i.kind == "paper" and i.published_at is None for i in items)


def test_window_clause_journal_pool(tmp_path):
    """顶刊池：池源候选在 pool_days 内可入窗（含无日期条目），过期出窗；非池源不受影响。"""
    from rebas.agents.stages import _window_clause
    from rebas.config import load_config

    conn = database.init_db(tmp_path / "t.sqlite")
    now = datetime.now(timezone.utc)

    def put(source_id, days_ago, with_date=True, url_suffix=""):
        ts = (now - timedelta(days=days_ago)).isoformat(timespec="seconds")
        item = RawItem(source_id=source_id, board="academic", kind="paper",
                       url=f"http://x/{source_id}/{days_ago}{url_suffix}",
                       url_canonical=f"http://x/{source_id}/{days_ago}{url_suffix}",
                       title=f"{source_id}-{days_ago}",
                       published_at=ts if with_date else None)
        database.insert_item(conn, item)
        if not with_date:
            conn.execute("UPDATE raw_items SET fetched_at=? WHERE url_canonical=?",
                         (ts, item.url_canonical))
            conn.commit()

    put("jmlr", 10)                                     # 池内（10 天前发表）
    put("jmlr", 40)                                     # 池过期
    put("jmlr", 11, with_date=False, url_suffix="-nd")  # 池内（无日期按 fetched_at）
    put("arxiv-x", 10)                                  # 非池源同龄 → 出窗

    clause, params = _window_clause(load_config(), pool_groups={30: ["jmlr"]})
    got = {r["title"] for r in conn.execute(
        f"SELECT title FROM raw_items WHERE {clause}", params)}
    assert got == {"jmlr-10", "jmlr-11"}   # 有日期+无日期在池，过期与非池源出窗

    # include_pool=False（主编清扫口径）：池内旧条目不在窗，不会被扫
    clause2, params2 = _window_clause(load_config(), include_pool=False)
    got2 = {r["title"] for r in conn.execute(
        f"SELECT title FROM raw_items WHERE {clause2}", params2)}
    assert "jmlr-10" not in got2


def test_openalex_dup_merge_and_lookup():
    """同题重复记录合并（时效取新+arXiv 取有）；DOI-only 记录走 arXiv 标题检索兜底。"""
    from rebas.collect.journals import parse_openalex_journal

    payload = {"results": [
        # 同一篇论文的两条记录：新 DOI-only + 旧 arXiv 版（07-06 期 JASA 实况）
        {"display_name": "Anytime-Valid Inference in Linear Models",
         "created_date": "2026-06-24", "doi": "https://doi.org/10.1080/yy",
         "locations": [], "authorships": []},
        {"display_name": "Anytime Valid Inference in Linear Models",
         "created_date": "2022-10-20", "doi": "https://doi.org/10.1080/yy",
         "locations": [{"landing_page_url": "https://arxiv.org/abs/2210.08589"}],
         "authorships": [{"author": {"display_name": "M. Lindon"}}]},
        # 纯 DOI 记录 → 标题检索兜底命中
        {"display_name": "Lonely Paper Without Locations",
         "created_date": "2026-07-01", "doi": "https://doi.org/10.1080/zz",
         "locations": [], "authorships": []},
    ]}

    class FakeResp:
        status_code = 200
        text = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
            <id>http://arxiv.org/abs/2507.11111v2</id>
            <title>Lonely Paper Without  Locations</title></entry></feed>"""

    class FakeClient:
        def get(self, url):
            return FakeResp()

    src = make_source(id="oa-test", board="data", type="openalex_journal")
    items, _ = parse_openalex_journal(src, json.dumps(payload).encode(),
                                      conn=None, client=FakeClient())
    assert len(items) == 2                                     # 三条记录 → 两篇论文
    a = next(i for i in items if "2210.08589" in i.url)
    assert a.published_at.startswith("2026-06-24")             # 时效取最新记录
    assert a.signals["doi"] == "https://doi.org/10.1080/yy"    # DOI 保留在信号
    assert a.author == "M. Lindon"                             # 作者从旧记录补齐
    b = next(i for i in items if "Lonely" in i.title)
    assert b.url == "https://arxiv.org/abs/2507.11111"         # 检索兜底命中


def test_prune_pool_exemption(tmp_path):
    """瘦身豁免：顶刊池源在池窗内保留摘要，非池源与出池条目照常清空。"""
    conn = database.init_db(tmp_path / "t.sqlite")
    now = datetime.now(timezone.utc)

    def put(source_id, days_ago):
        database.insert_item(conn, RawItem(
            source_id=source_id, board="data", kind="paper",
            url=f"http://x/{source_id}/{days_ago}", url_canonical=f"http://x/{source_id}/{days_ago}",
            title=f"{source_id}-{days_ago}", summary="摘要在此"))
        conn.execute("UPDATE raw_items SET fetched_at=? WHERE url_canonical=?",
                     ((now - timedelta(days=days_ago)).isoformat(timespec="seconds"),
                      f"http://x/{source_id}/{days_ago}"))
        conn.commit()

    put("oa-jasa", 10)     # 池内（10 天 < 30）→ 保留
    put("oa-jasa", 40)     # 出池 → 清空
    put("blog", 10)        # 非池源过 7 天 → 清空

    cutoff = (now - timedelta(days=7)).isoformat(timespec="seconds")
    pool_cut = (now - timedelta(days=30)).isoformat(timespec="seconds")
    n = database.prune_texts(conn, cutoff, pool_exemptions={pool_cut: ["oa-jasa"]})
    assert n == 2
    kept = {r["title"] for r in conn.execute(
        "SELECT title FROM raw_items WHERE summary IS NOT NULL")}
    assert kept == {"oa-jasa-10"}


def test_openalex_url_change_guard(tmp_path):
    """已以 DOI 入库的论文，本轮合并出 arXiv URL → 跳过不重复入池。"""
    from rebas.collect.journals import parse_openalex_journal

    conn = database.init_db(tmp_path / "t.sqlite")
    from rebas.collect.base import content_hash
    database.insert_item(conn, RawItem(
        source_id="oa-test", board="data", kind="paper",
        url="https://doi.org/10.1080/yy", url_canonical="https://doi.org/10.1080/yy",
        title="Anytime-Valid Inference in Linear Models",
        content_hash=content_hash("Anytime-Valid Inference in Linear Models")))
    payload = {"results": [
        {"display_name": "Anytime-Valid Inference in Linear Models",
         "created_date": "2026-07-04", "doi": "https://doi.org/10.1080/yy",
         "locations": [{"landing_page_url": "https://arxiv.org/abs/2210.08589"}],
         "authorships": []},
    ]}
    src = make_source(id="oa-test", board="data", type="openalex_journal")
    items, _ = parse_openalex_journal(src, json.dumps(payload).encode(), conn=conn)
    assert items == []      # 跨 URL 重复被拦下


REDDIT_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
<entry>
  <author><name>/u/builder</name></author>
  <content type="html">&lt;table&gt;&lt;tr&gt;&lt;td&gt;&lt;span&gt;&lt;a href="https://huggingface.co/acme/model-x"&gt;[link]&lt;/a&gt;&lt;/span&gt; &lt;span&gt;&lt;a href="https://www.reddit.com/r/LocalLLaMA/comments/aaa/model_x/"&gt;[comments]&lt;/a&gt;&lt;/span&gt;&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</content>
  <link href="https://www.reddit.com/r/LocalLLaMA/comments/aaa/model_x/"/>
  <updated>2026-07-06T08:00:00+00:00</updated>
  <title>New open model X released</title>
  <media:thumbnail url="https://external-preview.redd.it/thumb1.png"/>
</entry>
<entry>
  <author><name>/u/asker</name></author>
  <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;I benchmarked five quant runtimes and here are the numbers...&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt; &lt;span&gt;&lt;a href="https://www.reddit.com/r/LocalLLaMA/comments/bbb/benchmarks/"&gt;[link]&lt;/a&gt;&lt;/span&gt;</content>
  <link href="https://www.reddit.com/r/LocalLLaMA/comments/bbb/benchmarks/"/>
  <updated>2026-07-06T09:30:00+00:00</updated>
  <title>Benchmark: five quant runtimes compared</title>
</entry>
<entry>
  <author><name>/u/imgposter</name></author>
  <content type="html">&lt;span&gt;&lt;a href="https://i.redd.it/pic.png"&gt;[link]&lt;/a&gt;&lt;/span&gt;</content>
  <link href="https://www.reddit.com/r/LocalLLaMA/comments/ccc/meme/"/>
  <updated>2026-07-06T10:00:00+00:00</updated>
  <title>Look at this chart</title>
  <media:thumbnail url="https://preview.redd.it/pic.png?width=640"/>
</entry>
</feed>"""


def test_reddit_rss_parser():
    """链接帖用外链（发现层合并去重）；自文帖用讨论页+selftext；redd.it 媒体不算外链。"""
    from rebas.collect.reddit import parse_reddit_rss

    src = make_source(id="reddit-t", type="reddit_rss", board="repos",
                      pace_seconds=90)
    items, filtered = parse_reddit_rss(src, REDDIT_FIXTURE)
    assert filtered == 0 and len(items) == 3
    link_post, self_post, img_post = items

    assert link_post.url == "https://huggingface.co/acme/model-x"   # 外链为条目 URL
    assert link_post.author == "builder"                            # /u/ 前缀剥掉
    assert link_post.image_url == "https://external-preview.redd.it/thumb1.png"
    assert link_post.summary is None                                # 链接帖无 selftext

    assert "reddit.com/r/LocalLLaMA/comments/bbb" in self_post.url  # 自文帖用讨论页
    assert "benchmarked five quant runtimes" in self_post.summary   # selftext 进 summary
    assert self_post.published_at == "2026-07-06T09:30:00+00:00"

    assert "reddit.com/r/LocalLLaMA/comments/ccc" in img_post.url   # redd.it 媒体≠外链


def test_paced_lane_source_parsing():
    """pace_seconds 配置加载 + 双车道分流语义（bool(pace) == paced）。"""
    from rebas.config import load_sources

    sources = load_sources(enabled_only=True)
    paced = [s for s in sources if s.pace_seconds > 0]
    assert {s.type for s in paced} == {"reddit_rss", "nitter_rss"}  # 慢车道成员
    assert all(s.pace_seconds >= 60 for s in paced)        # 实测限速 ~1req/min，间隔须 ≥60s
    fast = {s.type for s in sources if not s.pace_seconds}
    assert not fast & {"reddit_rss", "nitter_rss"}         # 限速源绝不进并发快车道


NITTER_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>
<title>Andrej Karpathy / @karpathy</title><link>https://nitter.net/karpathy</link>
<item>
  <title>LLM agents are basically new operating systems</title>
  <dc:creator>@karpathy</dc:creator>
  <link>https://nitter.net/karpathy/status/123456789#m</link>
  <pubDate>Mon, 06 Jul 2026 08:00:00 GMT</pubDate>
  <description>&lt;p&gt;LLM agents are basically new operating systems&lt;/p&gt;</description>
</item>
</channel></rss>"""


def test_nitter_rss_parser():
    """条目 URL 改写回 x.com（镜像实例易死，外链与去重不依赖它）；实例代理图丢弃。"""
    from rebas.collect.feeds import parse_nitter_rss

    src = make_source(id="x-t", type="nitter_rss", board="tech", pace_seconds=120)
    items, _ = parse_nitter_rss(src, NITTER_FIXTURE, conn=None, client=None)
    assert len(items) == 1
    it = items[0]
    assert it.url == "https://x.com/karpathy/status/123456789"      # 改写 + 剥 #m
    assert it.url_canonical.startswith("https://x.com/")
    assert it.image_url is None                                     # 代理图不入库
    assert it.author == "@karpathy"


TRUTH_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Donald J. Trump</title>
<item>
  <title>[No Title] - Post from July 6, 2026</title>
  <link>https://trumpstruth.org/statuses/39861</link>
  <description>Tariffs on semiconductor imports will be announced next week. Companies building in America pay NOTHING!</description>
  <pubDate>Mon, 06 Jul 2026 16:03:25 +0000</pubDate>
</item>
<item>
  <title>[No Title] - Post from July 6, 2026</title>
  <link>https://trumpstruth.org/statuses/39860</link>
  <description></description>
  <pubDate>Mon, 06 Jul 2026 15:00:00 +0000</pubDate>
</item>
</channel></rss>"""


def test_truth_rss_parser():
    """占位标题换成正文头部（粗筛候选行才有信息量）；纯转发无正文保持原样。"""
    from rebas.collect.feeds import parse_truth_rss

    src = make_source(id="truth-t", type="truth_rss", board="finance")
    items, _ = parse_truth_rss(src, TRUTH_FIXTURE, conn=None, client=None)
    assert len(items) == 2
    assert items[0].title.startswith("Tariffs on semiconductor imports")
    assert items[0].content_hash != items[1].content_hash   # 换标题后哈希重算
    assert items[1].title.startswith("[No Title]")          # 无正文保持原样


# ---- 信息源自修复兜底（2026-07-09）----

RSS_FALLBACK_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>HN RSS</title>
<item><title>New LLM agent framework released</title>
  <link>https://example.com/llm-agent</link>
  <pubDate>Thu, 09 Jul 2026 12:00:00 GMT</pubDate></item>
<item><title>10 Best Coffee Shops in Berlin</title>
  <link>https://example.com/coffee</link>
  <pubDate>Thu, 09 Jul 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_error_backoff_ladder():
    """重试节律梯：首错回拨整间隔（下一轮 collect 立即重试，不受批次网格错位摆布）
    → 连败半间隔加密探测 → 长期死源（≥8）回正常间隔不高频空打。"""
    from rebas.collect.runner import _error_backoff_hours

    assert _error_backoff_hours(11, 1) == 11
    assert _error_backoff_hours(11, 2) == 5.5
    assert _error_backoff_hours(11, 7) == 5.5
    assert _error_backoff_hours(11, 8) == 0.0


def test_parse_feed_prefilter():
    """RSS 通道预筛（备用通道场景）：prefilter 源按画像关键词过滤，非 prefilter 不受影响。"""
    from rebas.collect.base import KeywordMatcher
    from rebas.collect.feeds import parse_feed
    from rebas.config import Interest, Profile

    matcher = KeywordMatcher(Profile(board="tech", name="t", interests=(
        Interest(name="AI", weight=5, keywords=("LLM", "agent")),)))
    src = make_source(id="hn", type="rss", prefilter=True)
    items, skipped = parse_feed(src, RSS_FALLBACK_FIXTURE, conn=None, client=None,
                                matcher=matcher)
    assert [i.title for i in items] == ["New LLM agent framework released"]
    assert skipped == 1
    items, skipped = parse_feed(make_source(id="hn2", type="rss"),
                                RSS_FALLBACK_FIXTURE, conn=None, client=None,
                                matcher=matcher)
    assert len(items) == 2 and skipped == 0


def test_runner_fallback_channel(tmp_path, monkeypatch):
    """备用通道：主通道抛错同轮改走备用端点（解析器按 fallback_type）——条目照常
    入库、last_status=fallback、连败计数照记；主通道恢复后 ok 归零 + 重定向提示。"""
    import dataclasses
    import urllib.error

    from rebas import db as database
    from rebas.collect import runner
    from rebas.collect.base import FetchResult
    from rebas.config import Interest, Profile, load_config

    src = make_source(id="hn-t", board="tech", type="hn_algolia",
                      endpoint="https://primary/api", prefilter=True,
                      fallback_type="rss", fallback_endpoint="https://fallback/rss")
    profile = Profile(board="tech", name="科技", interests=(
        Interest(name="AI", weight=5, keywords=("LLM", "agent")),))
    conf = dataclasses.replace(load_config(), data_dir=tmp_path)
    monkeypatch.setattr(runner, "load_config", lambda: conf)
    monkeypatch.setattr(runner, "load_sources", lambda enabled_only=False: [src])
    monkeypatch.setattr(runner, "load_profile", lambda b: profile)

    def fake_fetch(client, url, *, etag=None, last_modified=None, retries=2):
        if url == "https://primary/api":
            raise urllib.error.HTTPError(url, 400, "Bad Request", None, None)
        return FetchResult(status=200, data=RSS_FALLBACK_FIXTURE)
    monkeypatch.setattr(runner, "fetch_url", fake_fetch)

    s = {x.source_id: x for x in runner.run_collect()}["hn-t"]
    assert s.status == "fallback" and s.new == 1 and s.filtered_out == 1
    assert "备用通道" in s.counts_line()
    conn = database.connect(conf.db_path)
    st = conn.execute("SELECT * FROM fetch_state WHERE source_id='hn-t'").fetchone()
    assert st["last_status"] == "fallback" and st["error_streak"] == 1
    conn.close()

    # 主通道恢复（带一跳重定向）→ ok、连败归零、日志提示更新 endpoint
    monkeypatch.setattr(runner, "fetch_url", lambda client, url, **kw: FetchResult(
        status=200, data=b'{"hits": []}', final_url="https://primary/api/"))
    s = {x.source_id: x for x in runner.run_collect(force=True)}["hn-t"]
    assert s.status == "ok" and s.redirect == "https://primary/api/"
    assert "建议更新 endpoint" in s.counts_line()
    conn = database.connect(conf.db_path)
    st = conn.execute("SELECT * FROM fetch_state WHERE source_id='hn-t'").fetchone()
    assert st["last_status"] == "ok" and st["error_streak"] == 0
    conn.close()


def test_error_streak_accumulates_and_fast_retry(tmp_path, monkeypatch):
    """无备用通道的源：连败计数累计；首错后 last_fetch_at 回拨整间隔 → 立即再到期。"""
    import dataclasses
    import urllib.error
    from datetime import datetime, timezone

    from rebas import db as database
    from rebas.collect import runner
    from rebas.config import Interest, Profile, load_config

    src = make_source(id="s1", board="tech", type="rss",
                      endpoint="https://dead/feed", fetch_interval_hours=11)
    profile = Profile(board="tech", name="科技", interests=(
        Interest(name="AI", weight=5, keywords=("LLM",)),))
    conf = dataclasses.replace(load_config(), data_dir=tmp_path)
    monkeypatch.setattr(runner, "load_config", lambda: conf)
    monkeypatch.setattr(runner, "load_sources", lambda enabled_only=False: [src])
    monkeypatch.setattr(runner, "load_profile", lambda b: profile)

    def dead_fetch(client, url, **kw):
        raise urllib.error.HTTPError(url, 500, "Internal", None, None)
    monkeypatch.setattr(runner, "fetch_url", dead_fetch)

    runner.run_collect()
    conn = database.connect(conf.db_path)
    st = conn.execute("SELECT * FROM fetch_state WHERE source_id='s1'").fetchone()
    assert st["error_streak"] == 1
    # 首错：回拨整间隔 → 不等下个周期，下一轮 collect 立即到期重试
    assert runner._is_due(conn, src, datetime.now(timezone.utc))
    conn.close()

    runner.run_collect()          # 第二轮（已到期）再失败
    conn = database.connect(conf.db_path)
    st = conn.execute("SELECT * FROM fetch_state WHERE source_id='s1'").fetchone()
    assert st["error_streak"] == 2
    conn.close()
