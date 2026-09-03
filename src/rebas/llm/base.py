"""llm 抽象层：后端协议 + JSON 输出的解析兜底。"""

from __future__ import annotations

import json
import re
from typing import Protocol


class LLMError(Exception):
    """模型调用或输出解析失败（阶段幂等，重跑即可）。"""


class LLMBackend(Protocol):
    def complete(self, prompt: str, *, role: str = "default") -> str: ...
    # 可选扩展：支持图片附件的后端另接受 images=(本地文件路径, ...) 关键字参数
    # （codex exec -i）。complete_json 只在有图时传递，纯文本后端无需实现。


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

# 定点修补上限：一份输出里最多补几处丢失的引号（超过说明整体已坏，不再硬救）
_REPAIR_MAX = 5
_STRUCTURAL = '"{}[],:'


def _loads_with_repair(text: str):
    """json.loads 加定点修补——模型偶发丢掉字符串的**开引号**。

    2026-09-03 repos 背调连续两次在同一概念上吐出 `"term":localhost"`，把报错喂回去
    重试也复现，同一板块整批卡死。这类缺陷闭引号仍在，只缺开头一个字符，按解析器
    报错位置（"Expecting value" / "Expecting property name"）补一个引号再试即可；
    位置上是结构字符（如 `[1,]`、`{"a":}`）或其他类型的错误一律原样抛出，绝不改语义。
    """
    for _ in range(_REPAIR_MAX):
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            fixable = (err.msg == "Expecting value"
                       or err.msg.startswith("Expecting property name"))
            if not fixable or err.pos >= len(text) or text[err.pos] in _STRUCTURAL:
                raise
            text = text[:err.pos] + '"' + text[err.pos:]
    return json.loads(text)


def extract_json(text: str):
    """从模型输出中提取 JSON：剥 code fence → 找最外层 {} 或 []。"""
    text = text.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    if text.startswith(("{", "[")):
        candidate = text
    else:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise LLMError(f"输出中找不到 JSON: {text[:200]}")
        candidate = text[start:]
    # 从末尾往回裁到能解析为止（模型偶尔在 JSON 后面附赘述）
    end = len(candidate)
    closer = "}" if candidate[0] == "{" else "]"
    while end > 0:
        cut = candidate.rfind(closer, 0, end)
        if cut < 0:
            break
        try:
            return _loads_with_repair(candidate[: cut + 1])
        except json.JSONDecodeError:
            end = cut
    raise LLMError(f"JSON 解析失败: {candidate[:200]}")


def complete_json(backend: LLMBackend, prompt: str, *, role: str = "default",
                  retries: int = 1, images=()):
    """调用模型并解析 JSON；解析失败时把错误喂回去重试。

    images: 本地图片路径（撰写期图片审选）。仅在非空时传给后端，
    纯文本后端与既有测试桩不受影响。
    """
    kwargs = {"images": tuple(images)} if images else {}
    last_err: LLMError | None = None
    for attempt in range(retries + 1):
        text = backend.complete(prompt, role=role, **kwargs)
        try:
            return extract_json(text)
        except LLMError as err:
            last_err = err
            prompt = (
                f"{prompt}\n\n[上一次输出无法解析为 JSON：{err}。"
                "请重新输出，只输出合法 JSON，不要任何其他文字。]"
            )
    raise last_err  # type: ignore[misc]
