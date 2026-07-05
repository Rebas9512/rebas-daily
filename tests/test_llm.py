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
