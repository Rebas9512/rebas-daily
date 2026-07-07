"""专题级论文原文精读（2026-07-05）：arXiv id 提取、参考文献截尾、
文件缓存生命周期（load/discard/sweep）、materials_block 精读标注。不打网络。"""

import dataclasses
import os
import time

from rebas.agents.prompts import materials_block, render_prompt
from rebas.agents.stages import (
    _arxiv_id, _depth, _paper_cache_item, _resolve_fulltext_arxiv_id,
    _strip_references, discard_paper_fulltext, load_paper_fulltext, sweep_paper_cache,
)
from rebas.config import load_config


def make_conf(tmp_path):
    return dataclasses.replace(load_config(), data_dir=tmp_path)


class TestArxivId:
    def test_abs_and_canonical(self):
        assert _arxiv_id("https://arxiv.org/abs/2507.01234") == "2507.01234"
        assert _arxiv_id("http://arxiv.org/pdf/2507.01234") == "2507.01234"
        # HF papers 条目：url 是 HF 页，canonical 是 arXiv abs——第二参数兜住
        assert _arxiv_id("https://huggingface.co/papers/2507.01234",
                         "https://arxiv.org/abs/2507.01234") == "2507.01234"

    def test_old_style_and_miss(self):
        assert _arxiv_id("https://arxiv.org/abs/q-fin.TR/0701001") == "q-fin.TR/0701001"
        assert _arxiv_id("https://github.com/foo/bar") is None
        assert _arxiv_id(None, "") is None


class TestStripReferences:
    def test_cuts_tail_only(self):
        body = "正文 " * 500
        text = body + "\nReferences\n[1] Foo et al.\n[2] Bar et al."
        out = _strip_references(text)
        assert "Foo et al" not in out and out.startswith("正文")

    def test_early_mention_untouched(self):
        # References 出现在前半段（比如目录）不截
        text = "\nReferences\n" + "正文 " * 500
        assert _strip_references(text) == text


class TestPaperCacheItem:
    def test_prefers_in_topic_arxiv(self):
        rows = [
            {"id": 1, "kind": "paper", "url": "https://doi.org/10.1/x",
             "url_canonical": "https://doi.org/10.1/x"},
            {"id": 2, "kind": "paper", "url": "https://arxiv.org/abs/2507.01234",
             "url_canonical": ""},
        ]
        assert _paper_cache_item(rows)["id"] == 2      # 选题内 arXiv 版优先

    def test_falls_back_to_first_paper(self):
        rows = [
            {"id": 5, "kind": "news", "url": "https://x", "url_canonical": ""},
            {"id": 6, "kind": "paper", "url": "https://doi.org/10.1/y",
             "url_canonical": ""},
        ]
        assert _paper_cache_item(rows)["id"] == 6      # 无 arXiv → 首个论文条目（DOI）

    def test_none_when_no_paper(self):
        rows = [{"id": 9, "kind": "news", "url": "https://x", "url_canonical": ""}]
        assert _paper_cache_item(rows) is None


class TestResolveArxivId:
    def _conn(self, tmp_path):
        from rebas import db
        return db.init_db(tmp_path / "t.sqlite")

    def _add(self, conn, title, arxiv, kind="paper"):
        conn.execute(
            "INSERT INTO raw_items (source_id, board, url, url_canonical, title,"
            " kind, fetched_at) VALUES ('s','data',?,?,?,?,'x')",
            (arxiv, arxiv, title, kind))
        conn.commit()

    def test_own_arxiv(self, tmp_path):
        item = {"id": 1, "url": "https://arxiv.org/abs/2507.01234",
                "url_canonical": "", "title": "T"}
        assert _resolve_fulltext_arxiv_id(self._conn(tmp_path), item) == "2507.01234"

    def test_same_title_twin(self, tmp_path):
        conn = self._conn(tmp_path)
        title = "A Provably Efficient Estimator For Heavy Tailed Series"
        self._add(conn, title, "https://arxiv.org/abs/2410.12936")
        item = {"id": 999, "url": "https://doi.org/10.1214/x",
                "url_canonical": "", "title": title}
        assert _resolve_fulltext_arxiv_id(conn, item) == "2410.12936"

    def test_no_twin_returns_none(self, tmp_path):
        item = {"id": 999, "url": "https://doi.org/10.1214/x", "url_canonical": "",
                "title": "Some Unique Published Title With No Preprint Anywhere"}
        assert _resolve_fulltext_arxiv_id(self._conn(tmp_path), item) is None

    def test_short_title_not_matched(self, tmp_path):
        conn = self._conn(tmp_path)
        self._add(conn, "AI", "https://arxiv.org/abs/2410.99999")
        item = {"id": 999, "url": "https://doi.org/10.1/x", "url_canonical": "",
                "title": "AI"}
        assert _resolve_fulltext_arxiv_id(conn, item) is None      # 标题过短不兜

    def test_non_paper_twin_ignored(self, tmp_path):
        conn = self._conn(tmp_path)
        title = "A News Article Mentioning An arXiv Paper Link Here"
        self._add(conn, title, "https://arxiv.org/abs/2410.55555", kind="news")
        item = {"id": 999, "url": "https://doi.org/10.1/z", "url_canonical": "",
                "title": title}
        assert _resolve_fulltext_arxiv_id(conn, item) is None      # 非 paper 同题不兜


class TestDepthMarker:
    """主编编排期材料深度：论文预计能拿到 arXiv 原文的标「全文·精读」，让主编按全文
    档次排版，而非被源声明的摘要/标题档次误压（JMLR feed 仅标题却必得精读）。"""
    CMAP = {"arxiv": "abstract", "ft-src": "fulltext", "paywall": "headline"}

    def _conn(self, tmp_path):
        from rebas import db
        return db.init_db(tmp_path / "t.sqlite")

    def _row(self, **kw):
        base = {"extracted_text": None, "source_id": "arxiv", "kind": "paper",
                "url": "", "url_canonical": "", "title": "", "id": 1, "summary": ""}
        base.update(kw)
        return base

    def test_extracted_text_is_fulltext(self, tmp_path):
        assert _depth(self._row(extracted_text="正文"), self.CMAP,
                      self._conn(tmp_path)) == "全文"

    def test_source_fulltext(self, tmp_path):
        assert _depth(self._row(source_id="ft-src"), self.CMAP,
                      self._conn(tmp_path)) == "全文"

    def test_arxiv_paper_marked_deepread(self, tmp_path):
        # 源声明 abstract、摘要还短，但自带 arXiv id → 预计精读全文
        r = self._row(url="https://arxiv.org/abs/2507.01234", summary="短")
        assert _depth(r, self.CMAP, self._conn(tmp_path)) == "全文·精读"

    def test_journal_with_twin_deepread(self, tmp_path):
        conn = self._conn(tmp_path)
        title = "A Fully Specified Distinct Journal Paper Title"
        conn.execute(
            "INSERT INTO raw_items (source_id, board, url, url_canonical, title,"
            " kind, fetched_at) VALUES ('arxiv','data',"
            "'https://arxiv.org/abs/2410.12936','',?, 'paper','x')", (title,))
        conn.commit()
        r = self._row(url="https://doi.org/10.1/x", source_id="paywall",
                      title=title, summary="摘" * 500, id=999)
        assert _depth(r, self.CMAP, conn) == "全文·精读"

    def test_paywall_no_twin_stays_thin(self, tmp_path):
        conn = self._conn(tmp_path)
        r_abs = self._row(url="https://doi.org/10.1/x", source_id="paywall",
                          summary="摘" * 500, id=9, title="Unique Paywalled No Preprint")
        assert _depth(r_abs, self.CMAP, conn) == "摘要"       # 长摘要撑起摘要档
        r_thin = self._row(url="https://doi.org/10.1/y", source_id="paywall",
                           summary="太短", id=8, title="Another Unique Paywalled Thin")
        assert _depth(r_thin, self.CMAP, conn) == "仅标题"

    def test_conn_none_no_deepread(self, tmp_path):
        # 采集/粗筛期不传 conn → 论文不判精读，回落源声明档次
        r = self._row(url="https://arxiv.org/abs/2507.01234", summary="短")
        assert _depth(r, self.CMAP, None) == "摘要"           # 源 content=abstract

    def test_non_paper_not_deepread(self, tmp_path):
        r = self._row(kind="news", url="https://arxiv.org/abs/2507.01234",
                      source_id="paywall", summary="摘" * 500, title="News Links An arXiv Paper")
        assert _depth(r, self.CMAP, self._conn(tmp_path)) == "摘要"


class TestCacheLifecycle:
    def test_load_discard_sweep(self, tmp_path):
        conf = make_conf(tmp_path)
        conf.paper_cache_dir.mkdir(parents=True)
        rows = [{"id": 1}, {"id": 2}]
        (conf.paper_cache_dir / "1.txt").write_text("论文全文内容", encoding="utf-8")

        ft = load_paper_fulltext(conf, rows)
        assert ft == {1: "论文全文内容"}

        discard_paper_fulltext(conf, ft.keys())
        assert not (conf.paper_cache_dir / "1.txt").exists()
        discard_paper_fulltext(conf, [99])  # 不存在的安静跳过

    def test_sweep_only_old(self, tmp_path):
        conf = make_conf(tmp_path)
        conf.paper_cache_dir.mkdir(parents=True)
        old, fresh = conf.paper_cache_dir / "1.txt", conf.paper_cache_dir / "2.txt"
        old.write_text("旧"), fresh.write_text("新")
        stale = time.time() - 4 * 86400
        os.utime(old, (stale, stale))
        assert sweep_paper_cache(conf, days=3) == 1
        assert not old.exists() and fresh.exists()

    def test_sweep_no_dir(self, tmp_path):
        assert sweep_paper_cache(make_conf(tmp_path / "nope")) == 0


class TestMaterialsBlock:
    ROWS = [
        {"id": 1, "title": "Paper A", "source_id": "arxiv-ai-combined",
         "url": "https://arxiv.org/abs/2507.01234", "author": "张三",
         "extracted_text": None, "summary": "这是摘要" * 100},
        {"id": 2, "title": "News B", "source_id": "hf-daily-papers",
         "url": "https://x", "author": None,
         "extracted_text": None, "summary": "短摘要"},
    ]

    def test_fulltext_replaces_and_uncapped(self):
        deep = "全文精读内容" * 2000   # 远超 per_item_limit
        block = materials_block(self.ROWS, per_item_limit=100, fulltext={1: deep})
        assert "论文原文精读材料" in block
        assert deep in block                       # 精读材料不受 per_item_limit 截断
        assert "短摘要" in block                   # 其余条目照旧
        assert block.count("论文原文精读材料") == 1  # 只标注命中条目

    def test_without_fulltext_unchanged(self):
        block = materials_block(self.ROWS, per_item_limit=100)
        assert "论文原文精读材料" not in block
        assert ("这是摘要" * 100)[:100] in block

    def test_writer_template_renders_with_fulltext(self):
        # 改提示词模板后必跑模板渲染测试（2026-07-04 $ 转义教训）
        prompt = render_prompt(
            "writer", board_name="学术", topic_title="T", reason="R",
            target_length=1200, check_block="（无核查数据）",
            background_block="（无背景材料）", images_block="（本篇无图片材料）",
            materials_block=materials_block(self.ROWS, fulltext={1: "全文"}))
        assert "论文原文精读材料" in prompt and "以原文为准" in prompt

    def test_brief_template_renders_with_fulltext(self):
        # 论文类速览也精读（2026-07-06）：速览模板带原文时给"挑最有价值一点"指引
        prompt = render_prompt(
            "writer_brief", board_name="学术", topic_title="T", reason="R",
            target_length=300, background_block="（无背景材料）",
            images_block="（本篇无图片材料）",
            materials_block=materials_block(self.ROWS, fulltext={1: "全文"}))
        assert "论文原文精读材料" in prompt
        assert "这是论文类速览" in prompt and "保持简短" in prompt
