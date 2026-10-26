"""Typed boundaries between a language model, retrieval, and media execution."""

from __future__ import annotations

from typing import Annotated, Literal
import math
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Asset(StrictModel):
    id: Identifier
    path: str
    duration: float = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str
    attribution: str
    license: str


class Segment(StrictModel):
    id: Identifier
    asset_id: Identifier
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    transcript: str = ""
    frame: str | None = None
    visual_usable: bool = True
    source: Literal["asr", "subtitles", "synthetic_fixture", "visual_only"] = "visual_only"

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end <= self.start:
            raise ValueError("segment end must be greater than start")
        return self


class Goal(StrictModel):
    id: Identifier
    query: str = Field(min_length=1, max_length=600)
    channel: Literal["visual", "dialogue", "mixed"] = "visual"


class Brief(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    budget_seconds: float = Field(default=18, ge=2, le=90)
    max_clips: int = Field(default=5, ge=1, le=12)
    chronological: bool = True
    asset_ids: list[Identifier] = Field(default_factory=list, max_length=20)


class QuerySpec(StrictModel):
    goals: list[Goal] = Field(min_length=1, max_length=8)
    budget_seconds: float = Field(ge=2, le=90)
    max_clips: int = Field(ge=1, le=12)
    chronological: bool = True
    asset_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    planner: Literal["explicit", "ollama", "transformers"] = "explicit"

    @model_validator(mode="after")
    def unique_goals(self):
        if len({g.id for g in self.goals}) != len(self.goals):
            raise ValueError("duplicate goal IDs")
        return self


class JobRequest(StrictModel):
    brief: Brief
    mode: Literal["lexical", "dense", "hybrid", "adaptive"] = "lexical"
    provider: Literal["explicit", "transformers", "ollama"] = "explicit"
    job_id: Identifier | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)


class Hit(StrictModel):
    segment: Segment
    score: float
    lexical_score: float = 0
    dense_score: float | None = None
    evidence: list[str] = Field(default_factory=list)


class EditClip(StrictModel):
    segment_id: Identifier
    asset_id: Identifier
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    goal_ids: list[Identifier] = Field(min_length=1)
    relevance: float = Field(ge=0)


class EditPlan(StrictModel):
    version: Literal[1] = 1
    collection_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    spec: QuerySpec
    clips: list[EditClip] = Field(min_length=1, max_length=12)
    warnings: list[str] = Field(default_factory=list)
    retrieval_mode: Literal["lexical", "dense", "hybrid", "adaptive"]

    @property
    def duration(self):
        return sum(c.end - c.start for c in self.clips)


def explicit_spec(brief: Brief) -> QuerySpec:
    """Offline syntax is explicit: each semicolon/newline is an editing goal.

    This is deliberately labelled 'explicit', never presented as an LLM result.
    """
    parts = [p.strip() for p in re.split(r"[;；\n]", brief.text) if p.strip()]
    if not 1 <= len(parts) <= 8:
        raise ValueError("use one to eight semicolon-separated visual or dialogue goals")
    goals = []
    for i, part in enumerate(parts):
        channel = "visual"
        for prefix in ["visual:", "dialogue:", "mixed:"]:
            if part.lower().startswith(prefix):
                channel = prefix[:-1]
                part = part[len(prefix) :].strip()
                break
        goals.append(Goal(id=f"g{i+1}", query=part, channel=channel))
    return QuerySpec(
        goals=goals,
        budget_seconds=brief.budget_seconds,
        max_clips=brief.max_clips,
        chronological=brief.chronological,
        asset_ids=brief.asset_ids,
        planner="explicit",
    )
