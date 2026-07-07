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
