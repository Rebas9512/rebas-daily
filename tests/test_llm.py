"""llm 抽象层测试：JSON 提取兜底 + complete_json 重试语义。"""

import pytest

from rebas.llm.base import LLMError, complete_json, extract_json


class TestExtractJson:
    def test_plain(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_code_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_leading_and_trailing_prose(self):
        text = '好的，以下是结果：\n{"scores": [{"id": 1}]}\n以上就是全部。'
        assert extract_json(text) == {"scores": [{"id": 1}]}

    def test_array(self):
        assert extract_json('[1, 2]') == [1, 2]

    def test_no_json_raises(self):
        with pytest.raises(LLMError):
            extract_json("抱歉，我无法完成")


class TestMissingQuoteRepair:
    """定点修补丢失的开引号（2026-09-03 repos 背调 `"term":localhost"` 事故）。"""

    def test_missing_opening_quote_in_value(self):
        # 生产原样：概念名开引号丢失，闭引号仍在，回喂重试两次都复现
        text = ('{"topics":[{"id":5400,"concepts":[{"term":"端口号","note":"编号"},'
                '{"term":localhost","note":"本机"}],"facts":[]}]}')
        out = extract_json(text)
        assert out["topics"][0]["concepts"][1] == {"term": "localhost", "note": "本机"}

    def test_missing_opening_quote_leading_dot(self):
        text = '{"concepts":[{"term":.localhost域名","note":"x"}]}'
        assert extract_json(text)["concepts"][0]["term"] == ".localhost域名"

    def test_missing_opening_quote_in_key(self):
        assert extract_json('{"a": 1, b": 2}') == {"a": 1, "b": 2}

    def test_multiple_defects_and_trailing_prose(self):
        text = '{"a":x","b":[{"c":y"}]}\n以上是结果。'
        assert extract_json(text) == {"a": "x", "b": [{"c": "y"}]}

    def test_structural_errors_still_raise(self):
        # 位置上是结构字符：不补引号、不改语义，照旧报错
        for bad in ('{"a": }', '[1, ]', '{"a": undefined}', "{'a': 1}"):
            with pytest.raises(LLMError):
                extract_json(bad)

    def test_literals_untouched(self):
        assert extract_json('{"a": true, "b": null, "c": -1.5}') == \
            {"a": True, "b": None, "c": -1.5}

    def test_repair_budget(self):
        # 超过上限（5 处）说明整体已坏：不再硬救
        text = "{" + ",".join(f'"k{i}":v{i}"' for i in range(6)) + "}"
        with pytest.raises(LLMError):
            extract_json(text)


class FakeBackend:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, prompt, *, role="default"):
        self.prompts.append(prompt)
        return self.outputs.pop(0)


def test_complete_json_retry_feeds_error_back():
    backend = FakeBackend(["这不是JSON", '{"ok": true}'])
    assert complete_json(backend, "任务", role="t") == {"ok": True}
    assert len(backend.prompts) == 2
    assert "无法解析" in backend.prompts[1]


def test_complete_json_exhausted_raises():
    backend = FakeBackend(["坏", "还是坏"])
    with pytest.raises(LLMError):
        complete_json(backend, "任务", role="t", retries=1)


def test_complete_json_images_passthrough():
    """images 仅在非空时传给后端——纯文本后端（无 images 参数）不受影响。"""
    class ImageBackend(FakeBackend):
        def __init__(self, outputs):
            super().__init__(outputs)
            self.images = []

        def complete(self, prompt, *, role="default", images=()):
            self.images.append(tuple(images))
            return super().complete(prompt, role=role)

    ib = ImageBackend(['{"ok": 1}'])
    complete_json(ib, "任务", images=["/tmp/a.jpg"])
    assert ib.images == [("/tmp/a.jpg",)]
    # 无图时不传 kwarg：老式后端（签名无 images）也能正常工作
    assert complete_json(FakeBackend(['{"ok": 2}']), "任务") == {"ok": 2}


def test_codex_backend_image_flags(tmp_path, monkeypatch):
    """CodexBackend 图片附件：-i 按顺序拼进命令，附件顺序与提示词编号一致。"""
    import dataclasses
    from pathlib import Path
    from types import SimpleNamespace

    from rebas.config import load_config
    from rebas.llm import codex_cli

    monkeypatch.setattr(codex_cli, "find_codex_bin", lambda: "/bin/codex-fake")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text("ok")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    conf = dataclasses.replace(load_config(), data_dir=tmp_path)
    backend = codex_cli.CodexBackend(conf, {"default": "m"}, call_gap=0)
    assert backend.complete("hi", images=[tmp_path / "a.jpg", tmp_path / "b.png"]) == "ok"
    cmd = captured["cmd"]
    assert cmd.count("-i") == 2
    first = cmd.index("-i")
    assert cmd[first + 1].endswith("a.jpg") and cmd[first + 3].endswith("b.png")
    # 无图调用不带 -i
    backend.complete("hi2")
    assert "-i" not in captured["cmd"]


# ---------------------------------------------------------------------------
# 定点修补二轮：漏写反斜杠（2026-09-05 quant 写作 LaTeX 转义忽双忽单事故）
# ---------------------------------------------------------------------------
import json
import re
from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures"


class TestBackslashEscapeRepair:
    """模型在 JSON 字符串里写 LaTeX 时 `\\\\gamma` 与 `\\delta` 混用 → Invalid \\escape。"""

    def test_production_samples_parse(self):
        # 生产原样：原始输出 + 回喂重试各一份，同题稳定复现（回喂无效）
        samples = json.loads((_FIXTURES / "quant_writer_escapes_2026-09-05.json")
                             .read_text(encoding="utf-8"))
        for key in ("quant_writer_1", "quant_writer_2"):
            out = extract_json(samples[key])
            body = out["body_md"]
            assert set(out) == {"card_summary", "body_md", "images_keep"}
            assert "$\\delta=1/2$" in body            # 漏写的反斜杠补回 → 正文是单反斜杠 LaTeX
            assert "\\gamma" in body and "\\\\gamma" not in body   # 写对的 \\gamma 不被加倍
            assert re.search(r"\n(Minhyeok|Gatheral)", body)      # 公式外的真换行不动
            assert not re.search(r"[\x08\x0c\r\t]", body)         # 没有控制字符混进正文

    def test_single_invalid_escape(self):
        assert extract_json('{"a":"x\\delta y"}') == {"a": "x\\delta y"}

    def test_u_escape_not_hex(self):
        # \upsilon / \underbrace：解析器报 Invalid \uXXXX escape，位置指向 u（反斜杠在前一位）
        assert extract_json('{"a":"\\upsilon"}') == {"a": "\\upsilon"}
        assert extract_json('{"a":"x\\u12"}') == {"a": "x\\u12"}

    def test_escape_after_literal_backslash(self):
        # 前面已有写对的 \\，紧接着一个漏写的 \d：只补后者
        assert extract_json('{"a":"x\\\\\\delta"}') == {"a": "x\\\\delta"}

    def test_mixed_quote_and_escape_defects(self):
        assert extract_json('{"a":x\\delta","b":1}') == {"a": "x\\delta", "b": 1}

    def test_escape_budget(self):
        text = '{"a":"' + "\\d" * 201 + '"}'
        with pytest.raises(LLMError):
            extract_json(text)
        assert extract_json('{"a":"' + "\\d" * 200 + '"}') == {"a": "\\d" * 200}

    def test_truncation_still_raises(self):
        for bad in ('{"a":"xy', '{"a":"x\\', '{"a":"x\\u'):
            with pytest.raises(LLMError):
                extract_json(bad)

    def test_raw_control_chars_accepted(self):
        # 字符串里的裸制表符/换行（strict=False）：模型偶发不转义，语义就是那个字符
        assert extract_json('{"a":"x\ty\nz"}') == {"a": "x\ty\nz"}

    def test_error_message_carries_class_and_position(self):
        with pytest.raises(LLMError) as ei:
            extract_json('{"a":"' + "\\d" * 201 + '"}')
        msg = str(ei.value)
        assert "Invalid \\escape" in msg and "字符附近" in msg

    def test_retry_prompt_gets_escape_hint(self):
        backend = FakeBackend(['{"a":"' + "\\d" * 201 + '"}', '{"ok": 1}'])
        assert complete_json(backend, "任务", role="t") == {"ok": 1}
        assert "反斜杠必须写成" in backend.prompts[1]
        backend = FakeBackend(["这不是JSON", '{"ok": 1}'])
        complete_json(backend, "任务", role="t")
        assert "反斜杠必须写成" not in backend.prompts[1]

    def test_repair_logs_warning(self, caplog):
        with caplog.at_level("WARNING", logger="rebas.llm.base"):
            extract_json('{"a":"x\\delta"}')
        assert "JSON 定点修补" in caplog.text and "补反斜杠 1 处" in caplog.text


class TestLatexEscapeRestore:
    """\\b \\f \\n \\r \\t 恰好是合法转义：漏写反斜杠时解析器不报错，命令被悄悄吞成控制字符。

    还原只对已出现非法转义（转义不可靠）的文档启用，且只在数学区间内。
    """

    def test_clean_document_untouched(self):
        # 没有任何非法转义 → 门控关闭：哪怕数学区间里有 \t 也照原样解析（不做猜测）
        assert extract_json('{"m":"$\\theta$"}') == {"m": "$\theta$"}

    def test_gated_restore_inside_math(self):
        raw = '{"m":"$\\delta \\theta \\beta \\frac{1}{2} \\rho \\nabla f \\neq 0$"}'
        assert extract_json(raw) == {"m": "$\\delta \\theta \\beta \\frac{1}{2} \\rho \\nabla f \\neq 0$"}

    def test_display_math_and_dictionary_gate_for_n(self):
        raw = ('{"m":"$\\delta$ $$\\nabla_x g = 0$$ $$\\nx = 1$$ $$\\n\\\\begin{aligned}a\\\\end{aligned}$$"}')
        out = extract_json(raw)["m"]
        assert "$$\\nabla_x g = 0$$" in out         # 命令表里的 nabla 还原
        assert "$$\nx = 1$$" in out                 # nx 不是命令：真换行保留
        assert "$$\n\\begin{aligned}" in out        # 换行后是反斜杠：不动

    def test_outside_math_and_code_untouched(self):
        raw = ('{"m":"\\nMinhyeok $\\delta$\\n\\tnote","c":"`\\tcode $x$` and ```\\n\\tfmt.Println($y)\\n```",'
               '"d":"$\\delta$ price $5 then \\tab"}')
        out = extract_json(raw)
        assert out["m"] == "\nMinhyeok $\\delta$\n\tnote"          # 公式外的换行/制表符不动
        assert out["c"] == "`\tcode $x$` and ```\n\tfmt.Println($y)\n```"   # 代码区不动
        assert out["d"] == "$\\delta$ price $5 then \tab"           # 伪数学区（货币 $）里无命中也不动

    def test_already_doubled_not_redoubled(self):
        # 已写对的 \\tau（raw 为两个反斜杠）逐对扫描时算一对，不会被再加倍
        assert extract_json('{"m":"$\\delta \\\\tau$"}') == {"m": "$\\delta \\tau$"}


class TestFenceInsideJsonString:
    """code fence 只在整体不是裸 JSON 时才算包装层；字符串里的 markdown 代码块不能被抓走。"""

    def test_bare_json_with_inner_code_fence(self):
        raw = '{"body_md":"示例：\\n```python\\nprint({1: 2})\\n```\\n完"}'
        assert extract_json(raw)["body_md"] == "示例：\n```python\nprint({1: 2})\n```\n完"

    def test_wrapped_and_prose_then_fenced(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('结果如下：\n```json\n{"a": 1}\n```\n以上。') == {"a": 1}

    def test_prose_then_bare_json_with_inner_fence(self):
        raw = '结果：{"b":"```sh\\necho hi\\n```"} 完毕'
        assert extract_json(raw) == {"b": "```sh\necho hi\n```"}
