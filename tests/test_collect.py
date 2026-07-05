"""采集层单元测试：URL 规范化、预筛匹配、解析器、入库合并语义。"""

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
