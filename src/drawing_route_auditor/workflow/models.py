from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


FactStatus = Literal[
    "hit",
    "not_hit",
    "unable_to_judge",
    "conflict",
    "missing_due_to_reader_failure",
]
FlowStatus = Literal["complete", "partial", "error", "skipped"]
IssueKind = Literal["error", "candidates"]
ExecutionState = Literal["ready", "blocked", "conditional", "invalid"]
NecessityStatus = Literal["confirmed_required", "conditional"]


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
    value: str | float | bool | None
    evidence: list[EvidenceRef]
    coverage_complete: bool

    @model_validator(mode="after")
    def not_hit_requires_complete_coverage(self) -> FactObservation:
        if self.status == "not_hit" and not self.coverage_complete:
            raise ValueError("NOT_HIT requires complete observation coverage")
        return self


class ReaderOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_key: str = Field(min_length=1)
    process: str = Field(min_length=1)
    content: str
    targets: list[str]
    necessity_status: NecessityStatus
    execution_state: ExecutionState
    blocked_by: list[str]


class RouteConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_operation: str = Field(min_length=1)
    after_operation: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FlowIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: IssueKind
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    affected_operation_keys: list[str]
    missing_facts: list[str]
    candidate_options: list[str]


class FlowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_id: str = Field(min_length=1)
    status: FlowStatus
    observations: list[FactObservation]
    operations: list[ReaderOperation]
    constraints: list[RouteConstraint]
    issues: list[FlowIssue]


class AssembledOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_key: str
    flow_id: str
    process: str
    content: str
    targets: list[str]
    necessity_status: NecessityStatus
    execution_state: ExecutionState
    blocked_by: list[str]
    sequence: int | None = None
    lineage: dict[str, object]


class RouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "partial", "error"]
    operations: list[AssembledOperation]
    constraints: list[RouteConstraint]
    issues: list[FlowIssue]
    committable_operation_keys: list[str]
    blocked_operation_keys: list[str]


class ReaderExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_result: FlowResult
    duration_seconds: float = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class DrawingCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_code: str
    pdf_path: Path
    drawing_no: str
    part_name: str
    material_type: str
    parent_drawing_no: str | None = None
    parent_material_code: str | None = None
    parent_name: str | None = None
    parent_part_type: str | None = None
    source_batch: str | None = None


class WorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    case: DrawingCase
    drawing_sha256: str
    knowledge_snapshot: dict[str, object]
    dispatched_flows: list[str]
    skipped_flows: list[str]
    reader_executions: list[ReaderExecution]
    route: RouteResult
    elapsed_seconds: float
    render_seconds: float
    inference_seconds: float


@dataclass(frozen=True, slots=True)
class RenderedDrawing:
    drawing_sha256: str
    pages: tuple[Path, ...]
    duration_seconds: float
    cache_hit: bool


class FlowReader(Protocol):
    async def read(
        self,
        flow_id: str,
        pages: tuple[Path, ...],
        case: DrawingCase,
    ) -> ReaderExecution: ...
