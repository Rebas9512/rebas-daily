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
