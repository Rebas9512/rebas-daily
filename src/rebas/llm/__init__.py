"""rebas.llm — 模型调用抽象层。"""

from rebas.config import AppConfig
from rebas.llm.base import LLMBackend, LLMError, complete_json, extract_json


def get_backend(conf: AppConfig) -> LLMBackend:
    if conf.llm_backend == "codex_cli":
        from rebas.llm.codex_cli import CodexBackend
        return CodexBackend(conf, conf.llm_roles,
                            call_gap=conf.llm_call_gap, timeout=conf.llm_timeout,
                            search_roles=conf.llm_search_roles)
    if conf.llm_backend == "openai_api":
        raise NotImplementedError("openai_api 后端待接线（切换预留位）")
    raise ValueError(f"未知 llm backend: {conf.llm_backend}")


__all__ = ["get_backend", "complete_json", "extract_json", "LLMBackend", "LLMError"]
