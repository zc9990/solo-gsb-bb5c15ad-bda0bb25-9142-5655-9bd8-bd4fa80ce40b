"""Pydantic request/response schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .engine import CONFLICT_POLICIES


class RuleIn(BaseModel):
    code: str = Field(..., min_length=1, examples=["R_HIGH_RISK"])
    name: str = Field("", description="Human readable name")
    description: str | None = None
    condition: str = Field(
        ...,
        min_length=1,
        description="Boolean expression, e.g. 'doc.age >= 18 and derived.risk == \"high\"'",
    )
    decision: dict[str, Any] = Field(
        default_factory=dict,
        description='Outputs written when the rule fires; values may be "= expr"',
    )
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 100


class RuleSetCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    conflict_policy: str = Field("priority")
    rules: list[RuleIn]


class RuleSetUpdate(BaseModel):
    """Modifying a rule set always creates a new version; old versions stay."""

    name: str | None = None
    description: str | None = None
    conflict_policy: str | None = None
    rules: list[RuleIn] | None = None


class DocumentIn(BaseModel):
    content: dict[str, Any]
    external_id: str | None = None


class DryEvaluateIn(BaseModel):
    conflict_policy: str = "priority"
    rules: list[RuleIn]
    document: dict[str, Any]


class RuleSetSummary(BaseModel):
    id: str
    name: str
    description: str | None
    current_version: int
    created_at: str
    updated_at: str


class VersionInfo(BaseModel):
    version: int
    conflict_policy: str
    rules: list[dict[str, Any]]
    created_at: str
    note: str | None


class ConflictOut(BaseModel):
    output: str
    policy: str
    winner: dict[str, Any]
    losers: list[dict[str, Any]]
    agreements: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Rules that wrote the same winning value (not really losers)",
    )
    contenders: list[dict[str, Any]]
    message: str


class HitOut(BaseModel):
    order: int
    code: str
    name: str
    round: int
    outputs: dict[str, Any]


class DecisionOut(BaseModel):
    stable: bool
    rounds: int
    evaluation_order: list[str]
    hit_order: list[str]
    hits: list[HitOut]
    outputs: dict[str, Any]
    conflicts: list[ConflictOut]
    warnings: list[dict[str, str]] = Field(default_factory=list)


def validate_policy(policy: str | None) -> str:
    if policy and policy not in CONFLICT_POLICIES:
        raise ValueError(f"conflict_policy must be one of: {', '.join(CONFLICT_POLICIES)}")
    return policy or "priority"
