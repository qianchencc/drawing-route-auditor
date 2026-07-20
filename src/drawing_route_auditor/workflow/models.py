from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


FactStatus = Literal["hit", "not_hit", "unable_to_judge", "conflict"]
ReaderRequestStatus = Literal["succeeded", "error"]
RecommendationStatus = Literal[
    "complete",
    "complete_with_candidates",
    "partial",
    "error",
]


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    region: str = Field(min_length=1)
    text: str = Field(min_length=1)


class FactObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_key: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    status: FactStatus
    value: str | float | bool | list[str] | None
    evidence: list[EvidenceRef]
    coverage_complete: bool

    @model_validator(mode="after")
    def validate_not_hit_coverage(self) -> FactObservation:
        if self.status == "not_hit" and not self.coverage_complete:
            raise ValueError("NOT_HIT 要求观察范围已完整覆盖")
        return self


class ReaderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reader_key: str = Field(min_length=1)
    observations: list[FactObservation]


class RequestedFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_key: str
    label: str
    subject_scope: str
    value_type: str
    allowed_values: list[str] | None
    judgement_definition: str
    hit_criteria: str | None
    not_hit_criteria: str | None
    coverage_requirement: str | None
    evidence_requirement: str | None


class ReaderPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reader_id: int
    reader_key: str
    label: str
    capability_definition: str
    sequence: int
    requested_features: list[RequestedFeature]


class ReaderExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reader_key: str
    reader_label: str
    status: ReaderRequestStatus
    response: ReaderResponse | None
    duration_seconds: float = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowProgress:
    stage: Literal["render", "readers", "evaluate", "assemble", "complete"]
    state: Literal["started", "updated", "completed", "failed"]
    message: str
    completed_readers: int = 0
    total_readers: int = 0
    reader_label: str | None = None
    duration_seconds: float | None = None


ProgressCallback = Callable[[WorkflowProgress], None]


class RuleMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_key: str
    branch_key: str
    rule_key: str
    decision_key: str
    question: str
    option_key: str
    option_label: str
    result_status: Literal["resolved", "candidate", "error"]
    outcome_type: str
    outcome_key: str
    outcome_value: object | None
    decisive_facts: list[str]
    reason: str
    missing_facts: list[str]
    priority: int = 0


class DecisionFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_key: str
    label: str
    status: FactStatus
    value: object | None
    evidence: list[EvidenceRef]


class OperationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_key: str
    decision_key: str
    question: str
    selected_option: str
    alternative_options: list[str]
    result_status: Literal["resolved", "candidate"]
    reason: str
    missing_facts: list[str]
    decisive_facts: list[DecisionFact]
    rule_version: int


class RouteOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(gt=0)
    operation_key: str
    process_name: str
    source_rule_keys: list[str]
    decisions: list[OperationDecision]


class RouteCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_candidate_id: str
    operations: list[RouteOperation]


class LocalIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["error", "candidates"]
    code: str
    location: str
    message: str
    missing_facts: list[str]


class RouteRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RecommendationStatus
    route: list[RouteOperation] | None
    route_candidates: list[RouteCandidate]
    local_issues: list[LocalIssue]


class DrawingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pdf_path: Path
    material_code: str | None = None


class WorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    drawing_input: DrawingInput
    drawing_sha256: str
    tree_key: str
    tree_version: int
    model_version: str
    prompt_template_version: str
    reader_executions: list[ReaderExecution]
    derived_facts: dict[str, object]
    rule_matches: list[RuleMatch]
    recommendation: RouteRecommendation
    elapsed_seconds: float
    render_seconds: float
    reader_seconds: float


@dataclass(frozen=True, slots=True)
class RenderedDrawing:
    drawing_sha256: str
    pages: tuple[Path, ...]
    duration_seconds: float
    cache_hit: bool


class ReaderAdapter(Protocol):
    async def read(
        self,
        plan: ReaderPlan,
        pages: tuple[Path, ...],
        subject_context: str,
    ) -> ReaderExecution: ...
