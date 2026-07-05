"""专题级论文原文精读（2026-07-05）：arXiv id 提取、参考文献截尾、
文件缓存生命周期（load/discard/sweep）、materials_block 精读标注。不打网络。"""

import dataclasses
import os
import time

from rebas.agents.prompts import materials_block, render_prompt
from rebas.agents.stages import (
    _arxiv_id, _strip_references, discard_paper_fulltext, load_paper_fulltext,
    sweep_paper_cache,
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
            background_block="（无背景材料）",
            materials_block=materials_block(self.ROWS, fulltext={1: "全文"}))
        assert "论文原文精读材料" in prompt and "以原文为准" in prompt
