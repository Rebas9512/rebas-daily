"""管线各阶段传递的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawItem:
    source_id: str
    board: str
    url: str
    url_canonical: str
    title: str
    kind: str = "article"          # article | paper | repo | issue
    author: str | None = None
    published_at: str | None = None
    summary: str | None = None
    extracted_text: str | None = None
    content_hash: str | None = None
    signals: dict = field(default_factory=dict)
    image_url: str | None = None
    id: int | None = None
    status: str = "new"


@dataclass
class Topic:
    issue_date: str
    board: str
    title: str
    item_ids: list[int]
    decision: str                  # feature | brief | drop
    slot: str | None = None        # headline | regular
    score: float | None = None
    target_length: int | None = None
    needs_image: bool = False
    id: int | None = None


@dataclass
class Article:
    topic_id: int
    card_summary: str
    body_md: str
    credibility_notes: dict = field(default_factory=dict)
    image_refs: list[str] = field(default_factory=list)
    model_meta: dict = field(default_factory=dict)
    id: int | None = None
