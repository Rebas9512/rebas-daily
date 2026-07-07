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
            return json.loads(candidate[: cut + 1])
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
