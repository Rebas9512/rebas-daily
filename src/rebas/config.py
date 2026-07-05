"""配置加载：config.toml / sources.toml / profiles/*.toml。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 项目根：默认按源码位置推断（editable 安装），可用 REBAS_ROOT 覆盖
PROJECT_ROOT = Path(os.environ.get("REBAS_ROOT", Path(__file__).resolve().parents[2]))
CONFIG_DIR = PROJECT_ROOT / "config"
SECRETS_ENV = PROJECT_ROOT / ".secrets" / ".env"


def load_secrets(path: Path | None = None) -> dict[str, str]:
    """读 .secrets/.env（每行 KEY=VALUE，# 注释）。文件不存在返回空 dict。"""
    env = path or SECRETS_ENV
    secrets: dict[str, str] = {}
    if not env.exists():
        return secrets
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets


@dataclass(frozen=True)
class Source:
    id: str
    board: str
    name: str
    type: str                # rss | gnews_rss | hf_papers | hf_models | hn_algolia | gh_trending | reddit_oauth
    endpoint: str
    content: str             # fulltext | abstract | headline
    fetch_interval_hours: int
    enabled: bool = False
    prefilter: bool = False
    paywall: bool = False


@dataclass(frozen=True)
class Interest:
    name: str
    weight: int
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Profile:
    board: str
    name: str
    interests: tuple[Interest, ...]
    low_priority: tuple[str, ...] = ()
    blocklist: tuple[str, ...] = ()
    # [reader] 读者画像（背景调查阶段用）：assumed=已掌握不解释，explain=需要铺垫的方向。
    # 两者都空 = 该板块读者不需要背景调查（商业/艺术），research 阶段整体跳过。
    reader_assumed: str = ""
    reader_explain: str = ""

    def all_keywords(self) -> tuple[str, ...]:
        """预筛关键词全集（所有 interest 的并集）。"""
        return tuple(kw for it in self.interests for kw in it.keywords)


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    data_dir: Path
    site_dir: Path
    llm_backend: str
    codex_home: Path
    llm_roles: dict
    llm_call_gap: float
    llm_timeout: int
    publish_boards: tuple[str, ...]
    window_hours: int
    paper_settle_hours: int
    screen_batch: int
    screen_min_score: int
    screen_cap: int
    editor_top: int
    feature_cap: int
    brief_length: int
    site_keep_days: int
    paper_fulltext_max_chars: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rebas.sqlite"

    @property
    def paper_cache_dir(self) -> Path:
        """专题级论文原文的临时缓存（writer 精读用，用完即删，不进 DB）。"""
        return self.data_dir / "paper_cache"


def load_config() -> AppConfig:
    raw = tomllib.loads((CONFIG_DIR / "config.toml").read_text(encoding="utf-8"))
    general, llm, publish = raw["general"], raw["llm"], raw["publish"]
    return AppConfig(
        timezone=general["timezone"],
        data_dir=PROJECT_ROOT / general["data_dir"],
        site_dir=PROJECT_ROOT / general["site_dir"],
        llm_backend=llm["backend"],
        codex_home=PROJECT_ROOT / llm["codex_home"],
        llm_roles=dict(llm.get("roles", {})),
        llm_call_gap=float(llm.get("call_gap_seconds", 2.0)),
        llm_timeout=int(llm.get("timeout_seconds", 300)),
        publish_boards=tuple(publish["boards"]),
        window_hours=int(publish.get("window_hours", 48)),
        paper_settle_hours=int(publish.get("paper_settle_hours", 0)),
        screen_batch=int(publish.get("screen_batch", 50)),
        screen_min_score=int(publish.get("screen_min_score", 3)),
        screen_cap=int(publish.get("screen_cap", 400)),
        editor_top=int(publish.get("editor_top", 80)),
        feature_cap=int(publish.get("feature_cap", 4)),
        brief_length=int(publish.get("brief_length", 300)),
        site_keep_days=int(publish.get("site_keep_days", 7)),
        paper_fulltext_max_chars=int(publish.get("paper_fulltext_max_chars", 40_000)),
    )


def load_sources(enabled_only: bool = False) -> list[Source]:
    raw = tomllib.loads((CONFIG_DIR / "sources.toml").read_text(encoding="utf-8"))
    sources = [Source(**entry) for entry in raw["source"]]
    if enabled_only:
        sources = [s for s in sources if s.enabled]
    return sources


def load_profile(board: str) -> Profile:
    raw = tomllib.loads((CONFIG_DIR / "profiles" / f"{board}.toml").read_text(encoding="utf-8"))
    interests = tuple(
        Interest(name=i["name"], weight=i["weight"], keywords=tuple(i["keywords"]))
        for i in raw.get("interest", [])
    )
    reader = raw.get("reader", {})
    return Profile(
        board=raw["board"]["id"],
        name=raw["board"]["name"],
        interests=interests,
        low_priority=tuple(raw.get("low_priority", {}).get("keywords", [])),
        blocklist=tuple(raw.get("blocklist", {}).get("keywords", [])),
        reader_assumed=reader.get("assumed", "").strip(),
        reader_explain=reader.get("explain", "").strip(),
    )
