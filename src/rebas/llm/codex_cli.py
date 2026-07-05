"""codex_cli 后端：经 Codex CLI 无头模式消化 ChatGPT 订阅额度。

调用方式与 scripts/verify_codex.sh 一致（2026-07-03 实测可用）：
  CODEX_HOME=<项目>/.codex codex exec --skip-git-repo-check -s read-only \
      --output-last-message <tmp> -m <model> <prompt>
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from rebas.config import AppConfig
from rebas.llm.base import LLMError

_RATE_LIMIT_MARKERS = ("rate limit", "429", "too many requests", "usage limit")
_RATE_LIMIT_BACKOFF = 300  # 撞限速后等 5 分钟再试一次


def find_codex_bin() -> str:
    path = shutil.which("codex")
    if path:
        return path
    candidates = sorted(glob.glob(
        os.path.expanduser("~/.vscode/extensions/openai.chatgpt-*/bin/linux-x86_64/codex")
    ))
    if candidates:
        return candidates[-1]
    raise LLMError("找不到 codex 二进制（PATH 与 VSCode 扩展目录均无）")


class CodexBackend:
    def __init__(self, conf: AppConfig, roles: dict[str, str], *,
                 call_gap: float = 2.0, timeout: int = 300):
        self.codex_home = conf.codex_home
        self.roles = roles
        self.call_gap = call_gap
        self.timeout = timeout
        self.bin = find_codex_bin()
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self._last_call + self.call_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _mark_call_done(self) -> None:
        # gap 从调用返回时刻起算——按开始时刻算的话单次调用普遍超过 gap，节流形同虚设
        self._last_call = time.monotonic()

    def complete(self, prompt: str, *, role: str = "default") -> str:
        model = self.roles.get(role) or self.roles.get("default")
        for attempt in (0, 1):
            self._throttle()
            with tempfile.NamedTemporaryFile(
                mode="r", suffix=".txt", prefix="rebas_llm_", delete=False
            ) as tmp:
                out_path = Path(tmp.name)
            try:
                proc = subprocess.run(
                    [self.bin, "exec", "--skip-git-repo-check", "-s", "read-only",
                     "--output-last-message", str(out_path),
                     *(["-m", model] if model else []), prompt],
                    capture_output=True, text=True, timeout=self.timeout,
                    env={**os.environ, "CODEX_HOME": str(self.codex_home)},
                )
                if proc.returncode == 0:
                    text = out_path.read_text(encoding="utf-8").strip()
                    if text:
                        return text
                    raise LLMError(f"codex 正常退出但无输出（role={role}）")
                stderr = (proc.stderr or "").lower()
                if attempt == 0 and any(m in stderr for m in _RATE_LIMIT_MARKERS):
                    time.sleep(_RATE_LIMIT_BACKOFF)   # 限速 → 长退避重试一次
                    continue
                raise LLMError(
                    f"codex exec 失败（role={role}, rc={proc.returncode}）: "
                    f"{(proc.stderr or '')[-300:]}"
                )
            except subprocess.TimeoutExpired:
                raise LLMError(f"codex exec 超时（>{self.timeout}s, role={role}）") from None
            finally:
                self._mark_call_done()
                out_path.unlink(missing_ok=True)
        raise LLMError(f"codex exec 限速重试后仍失败（role={role}）")
