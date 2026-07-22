from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from drawing_route_auditor.decision_tree.definition import (
    ReaderSourceKind,
    SubjectScope,
    ValueType,
)


FactStatus = Literal["hit", "not_hit", "unable_to_judge", "conflict"]
ReaderRequestStatus = Literal["succeeded", "error"]
RecommendationStatus = Literal[
    "complete",
    "complete_with_candidates",
    "partial",
    "error",
]


def _without_nul(value: object) -> object:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_without_nul(item) for item in value]
    return value


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["drawing", "plm", "rule"] = "drawing"
    page: int | None = Field(default=None, ge=1)
    region: str | None = Field(default=None, min_length=1)
    text: str = Field(min_length=1)

    @field_validator("region", "text", mode="before")
    @classmethod
    def remove_nul_characters(cls, value: object) -> object:
        return _without_nul(value)

    @model_validator(mode="after")
    def validate_location(self) -> EvidenceRef:
        if self.source_type == "drawing" and (self.page is None or self.region is None):
            raise ValueError("图纸证据必须提供页码和区域")
        return self


class FactObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_key: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    status: FactStatus
    value: str | float | bool | list[str] | None
    evidence: list[EvidenceRef]
    coverage_complete: bool

    @field_validator("subject_ref", "value", mode="before")
    @classmethod
    def remove_nul_characters(cls, value: object) -> object:
        return _without_nul(value)

    @model_validator(mode="after")
    def validate_status_value(self) -> FactObservation:
        if self.status == "hit" and self.value is None:
            raise ValueError("HIT 必须提供 value")
        if self.status == "not_hit":
            if not self.coverage_complete:
                raise ValueError("NOT_HIT 要求观察范围已完整覆盖")
            if self.value == 0:
                self.value = False
            if self.value is not False:
                raise ValueError("NOT_HIT 的 value 必须为 false")
        if self.status in {"unable_to_judge", "conflict"} and self.value is not None:
            raise ValueError("UNABLE_TO_JUDGE 或 CONFLICT 的 value 必须为 null")
        return self


class DrawingEvidenceRef(EvidenceRef):
    source_type: Literal["drawing"] = "drawing"


class ReaderFactObservation(FactObservation):
    evidence: list[DrawingEvidenceRef]


class FactContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_key: str
    label: str
    source_kind: ReaderSourceKind
    subject_scope: SubjectScope
    value_type: ValueType
    allowed_values: list[str] | None


class ReaderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reader_key: str = Field(min_length=1)
    observations: list[ReaderFactObservation]

    @field_validator("observations", mode="before")
    @classmethod
    def normalize_observation_models(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [
            item.model_dump(mode="python")
            if isinstance(item, FactObservation)
            else item
            for item in value
        ]


class RequestedFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_key: str
    label: str
    subject_scope: SubjectScope
    value_type: ValueType
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
    page_inputs: list[str] = Field(default_factory=list)

    @field_validator("error_code", "error_message", mode="before")
    @classmethod
    def remove_nul_characters(cls, value: object) -> object:
        return _without_nul(value)


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
    subject_observations: list[FactObservation] = Field(default_factory=list)


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
    rule_revision: int


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


class WorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    drawing_input: DrawingInput
    drawing_sha256: str
    tree_key: str
    tree_revision: int
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
