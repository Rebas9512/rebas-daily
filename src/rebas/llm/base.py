"""llm 抽象层：后端协议 + JSON 输出的解析兜底。"""

from __future__ import annotations

import json
import logging
import re
from typing import Protocol

log = logging.getLogger(__name__)


class LLMError(Exception):
    """模型调用或输出解析失败（阶段幂等，重跑即可）。"""


class LLMBackend(Protocol):
    def complete(self, prompt: str, *, role: str = "default") -> str: ...
    # 可选扩展：支持图片附件的后端另接受 images=(本地文件路径, ...) 关键字参数
    # （codex exec -i）。complete_json 只在有图时传递，纯文本后端无需实现。


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

# 定点修补上限（按缺陷类分开计数，超过说明整体已坏，不再硬救）：
#   引号——一份输出里丢开引号的位置数，正常为 0~1；
#   反斜杠——公式多的稿一处 LaTeX 命令就是一处，几十处很正常（2026-09-05 实测 21 处），
#   上限放宽但仍有界，防止病态输入下无限打补丁。
_QUOTE_REPAIR_MAX = 5
_ESCAPE_REPAIR_MAX = 200
_STRUCTURAL = '"{}[],:'

# 合法 JSON 转义里恰好与 LaTeX 命令首字母撞车的五个：\b \f \n \r \t。模型漏写一个反斜杠
# 时解析器不报错，而是悄悄把 \beta 变成退格+"eta"、\frac 变成换页+"rac"、\tau 变成
# 制表符+"au"。数学区间里这五种控制字符后紧跟字母没有任何合法用法，可以安全补回反斜杠；
# 唯独 \n 是真换行的常客（多行 $$ 块），所以只认一张 LaTeX 命令表，字面拼得上才还原。
_LATEX_N_COMMANDS = frozenset("""
nabla ne neq neg ni not notin nu nmid nless ngtr nleq ngeq nleqslant ngeqslant nsim ncong
nequiv nparallel nsubseteq nsupseteq nsubset nsupset nexists nvdash nVdash nvDash nwarrow
nearrow newline nonumber nolimits notag natural nobreak norm
""".split())
_ESCAPE_PAIR_RE = re.compile(r"\\(.)", re.S)
_LETTERS_RE = re.compile(r"[A-Za-z]+")
# raw JSON 文本里的代码区（围栏 / 行内反引号）与数学区（$$…$$ / $…$）。raw 文本没有裸换行
# （都是 \n 两个字符），re.S 只是保险。奇数个反引号会让代码区吞掉后面的正文——方向是
# "少修"而非"误修"，可接受。
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`]*`", re.S)
_MATH_SPAN_RE = re.compile(r"\$\$.+?\$\$|\$[^$]+?\$", re.S)


def _restore_latex_escapes(text: str) -> tuple[str, int]:
    """raw JSON 文本内、数学区间里漏写反斜杠的 \\b \\f \\n \\r \\t 型 LaTeX 命令补回反斜杠。

    只在文档已确认转义不可靠（出现过非法转义并被修补）时由调用方启用：干净文档零改动。
    逐对扫描转义（`\\\\` 算一对、不会把已写对的 `\\\\t` 再加倍），代码区跳过。
    """
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        c = m.group(1)
        letters = _LETTERS_RE.match(m.string, m.end())
        if not letters:
            return m.group(0)
        if c in "bfrt" or (c == "n" and ("n" + letters.group(0)) in _LATEX_N_COMMANDS):
            count += 1
            return "\\\\" + c
        return m.group(0)

    def fix_prose(seg: str) -> str:
        return _MATH_SPAN_RE.sub(lambda m: _ESCAPE_PAIR_RE.sub(repl, m.group(0)), seg)

    out: list[str] = []
    pos = 0
    for m in _CODE_SPAN_RE.finditer(text):
        out.append(fix_prose(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fix_prose(text[pos:]))
    return "".join(out), count


def _backslash_at(text: str, pos: int) -> int:
    """解析器报错位置对应的反斜杠下标（不同错误类/实现指向反斜杠本身或其后一字符）。"""
    if pos < len(text) and text[pos] == "\\":
        return pos
    if 0 < pos <= len(text) and text[pos - 1] == "\\":
        return pos - 1
    return -1


def _loads_with_repair(text: str):
    """json.loads 加定点修补——按解析器报错位置修两类模型输出缺陷，绝不改语义。

    ① 丢开引号（2026-09-03 repos 背调 `"term":localhost"`）：闭引号仍在、只缺开头一个
       字符，"Expecting value" / "Expecting property name" 且位置不是结构字符时补一个引号。
    ② 漏写反斜杠（2026-09-05 quant 写作 LaTeX 转义忽双忽单 `\\\\gamma` 与 `\\delta` 混用）：
       "Invalid \\escape" / "Invalid \\uXXXX escape" 时把该处反斜杠加倍——非法转义在合法
       JSON 里本无意义，加倍只是把模型漏掉的那个字符补回去。文档一旦出现过②，同一份
       输出里 \\t \\b 之类"恰好合法"的漏写命令也很可能存在，再走一遍数学区间还原。
    裸控制字符（字符串里真换行/制表符）用 strict=False 直接吸收。其余错误（截断、
    结构性错误、单引号、undefined）原样抛出；修补若没让解析器前进也立即放弃。
    """
    quote_fixes = esc_fixes = 0
    last_pos = -1
    while True:
        try:
            obj = json.loads(text, strict=False)
            break
        except json.JSONDecodeError as err:
            if err.pos < last_pos:
                raise
            msg = err.msg
            if msg == "Expecting value" or msg.startswith("Expecting property name"):
                if (quote_fixes >= _QUOTE_REPAIR_MAX or err.pos >= len(text)
                        or text[err.pos] in _STRUCTURAL):
                    raise
                text = text[:err.pos] + '"' + text[err.pos:]
                quote_fixes += 1
            elif msg.startswith("Invalid \\escape") or msg.startswith("Invalid \\uXXXX escape"):
                p = _backslash_at(text, err.pos)
                if p < 0 or esc_fixes >= _ESCAPE_REPAIR_MAX:
                    raise
                text = text[:p] + "\\" + text[p:]
                esc_fixes += 1
            else:
                raise
            last_pos = err.pos
    restored = 0
    if esc_fixes:
        fixed, restored = _restore_latex_escapes(text)
        if restored:
            try:
                obj = json.loads(fixed, strict=False)
            except json.JSONDecodeError:
                restored = 0  # 还原层只会加反斜杠、理论上不会破坏合法性；万一破坏就退回未还原版
    if quote_fixes or esc_fixes:
        log.warning("[llm] JSON 定点修补：补引号 %d 处、补反斜杠 %d 处、公式转义还原 %d 处",
                    quote_fixes, esc_fixes, restored)
    return obj


def _describe_failure(candidate: str, err: json.JSONDecodeError | None) -> str:
    """解析失败的报错文案：带错误类与出错位置附近的原文片段——回喂重试时模型才知道错在哪。"""
    if err is None:
        return f"JSON 解析失败: {candidate[:200]}"
    lo, hi = max(0, err.pos - 60), min(len(err.doc), err.pos + 40)
    return (f"JSON 解析失败（{err.msg}，第 {err.pos} 字符附近：…{err.doc[lo:hi]}…）: "
            f"{candidate[:200]}")


def extract_json(text: str):
    """从模型输出中提取 JSON：剥 code fence → 找最外层 {} 或 []。"""
    text = text.strip()
    if not text.startswith(("{", "[")):
        # 只有整体不是裸 JSON 时才找 code fence，且 fence 内容须以 {/[ 开头才采信——
        # 裸 JSON 的字符串里本身可能含 markdown 代码块（开源板块代码示例），不能被当成包装层
        fence = _FENCE_RE.search(text)
        if fence and fence.group(1).lstrip().startswith(("{", "[")):
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
    first_err: json.JSONDecodeError | None = None
    while end > 0:
        cut = candidate.rfind(closer, 0, end)
        if cut < 0:
            break
        try:
            return _loads_with_repair(candidate[: cut + 1])
        except json.JSONDecodeError as err:
            first_err = first_err or err  # 首次（未回裁）的报错最能说明问题
            end = cut
    raise LLMError(_describe_failure(candidate, first_err))


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
            hint = ""
            if "escape" in str(err):
                hint = ("JSON 字符串里的反斜杠必须写成 \\\\（LaTeX 命令如 \\\\delta、\\\\frac），"
                        "并且全文一致。")
            prompt = (
                f"{prompt}\n\n[上一次输出无法解析为 JSON：{err}。{hint}"
                "请重新输出，只输出合法 JSON，不要任何其他文字。]"
            )
    raise last_err  # type: ignore[misc]
