# Aftershock — API: request/response schemas (§10.1).

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    doc: str | None = None
    chunk_id: str | None = None
    fact_key: str | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class AskResponse(BaseModel):
    answer_id: str
    question_id: str
    answer: str
    abstained: bool
    evidence: list[EvidenceItem]


class ImpactPreviewFile(BaseModel):
    path: str
    content: str
    op: Literal["added", "modified", "deleted"]


class ImpactPreviewRequest(BaseModel):
    """§10.1 POST /impact/preview — a dry run on uncommitted content, used
    by the MCP server / a CLI so a docs edit can be checked before a PR
    exists. Unlike the webhook flow, there's no real head_sha to key the
    scratch graph on, so one is synthesised per request."""

    files: list[ImpactPreviewFile]


class ImpactedAnswer(BaseModel):
    answer_id: str
    question: str
    tier: str
    via: list[str]
    verdict: str
    old_answer: str
    new_answer: str
    reason: str
    score: float | None = None


class ImpactPreviewResponse(BaseModel):
    facts_added: int
    facts_removed: int
    facts_modified: int
    tier_raw_counts: dict[str, int]
    truncated: bool
    impacted: list[ImpactedAnswer]
    timings_ms: dict[str, int]


class ChangesetSummary(BaseModel):
    changeset_id: str
    pr: int
    base_sha: str
    head_sha: str
    doc_ids: list[str]
    status: str
    facts_added: int
    facts_removed: int
    facts_modified: int
    changed_count: int
    now_abstains_count: int
    now_answers_count: int
    truncated: bool
    timings_ms: dict[str, int]
    entries: list[ImpactedAnswer]


class QuestionHistoryEntry(BaseModel):
    text: str
    commit_sha: str
    created_at: int


class HealthResponse(BaseModel):
    falkordb: bool
    main_graph: str
    demo_mode: bool
    detail: dict[str, Any] = Field(default_factory=dict)
