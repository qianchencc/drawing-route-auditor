from __future__ import annotations

import csv
from difflib import SequenceMatcher
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from drawing_route_auditor.workflow.models import RouteResult


class GoldenOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_number: str
    process: str
    content: str
    production_center: str
    work_center: str
    team_name: str
    source_file: str
    source_row: int
    raw: dict[str, str]


class GoldenRouteCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    reason: str
    expected_processes: list[str]
    operations: list[GoldenOperation]


class EvaluationDifference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    predicted: list[str]
    expected: list[str]
    predicted_range: list[int]
    expected_range: list[int]


class CandidateComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    operation_sequence_match: bool
    expected_processes: list[str]
    differences: list[EvaluationDifference]


class GoldenEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_code: str
    status: str
    operation_sequence_match: bool
    predicted_processes: list[str]
    route_candidates: list[GoldenRouteCandidate]
    comparisons: list[CandidateComparison]
    unresolved_route_issues: list[str]


def _clean(value: str | None) -> str:
    if value is None or value == "NULL":
        return ""
    return value.strip()


def _operation_number(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return float("inf")


def _operation_from_row(
    row: dict[str, str],
    *,
    source: Path,
    row_number: int,
) -> GoldenOperation:
    return GoldenOperation(
        operation_number=_clean(row.get("工序号")),
        process=_clean(row.get("工序名称")),
        content=_clean(row.get("工艺内容")),
        production_center=_clean(row.get("生产中心")),
        work_center=_clean(row.get("机器工作中心")),
        team_name=_clean(row.get("班组名称")),
        source_file=str(source),
        source_row=row_number,
        raw={str(key): _clean(value) for key, value in row.items()},
    )


def _candidate_signature(
    operations: list[GoldenOperation],
) -> tuple[tuple[str, ...], ...]:
    fields = (
        "工序号",
        "工序名称",
        "工艺内容",
        "材料编码",
        "材料规格",
        "材料定额",
        "开料尺寸",
        "材质",
        "准备工时",
        "工序工时",
        "等待工时",
        "机器工作中心",
        "生产中心",
        "班组名称",
    )
    return tuple(tuple(item.raw.get(field, "") for field in fields) for item in operations)


def load_golden_routes(
    material_code: str,
    *,
    route_sources: tuple[Path, ...],
) -> tuple[GoldenRouteCandidate, ...]:
    raw_candidates: list[tuple[Path, list[GoldenOperation]]] = []
    for source in route_sources:
        current: list[GoldenOperation] = []
        previous_number: float | None = None
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                if _clean(row.get("存货编码")) != material_code:
                    continue
                operation = _operation_from_row(
                    row,
                    source=source,
                    row_number=row_number,
                )
                number = _operation_number(operation.operation_number)
                if (
                    current
                    and previous_number is not None
                    and number <= previous_number
                ):
                    raw_candidates.append((source, current))
                    current = []
                current.append(operation)
                previous_number = number
        if current:
            raw_candidates.append((source, current))

    if not raw_candidates:
        raise LookupError(f"No golden route found for material {material_code}")

    seen_signatures: set[tuple[tuple[str, ...], ...]] = set()
    candidates: list[GoldenRouteCandidate] = []
    for source, operations in raw_candidates:
        signature = _candidate_signature(operations)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        start_row = operations[0].source_row
        end_row = operations[-1].source_row
        candidate_id = f"{source.stem}:rows-{start_row}-{end_row}"
        candidates.append(
            GoldenRouteCandidate(
                candidate_id=candidate_id,
                reason=(
                    "同一物料在历史路线数据中存在独立工序号序列；"
                    "现有字段不能确认唯一有效版本"
                ),
                expected_processes=[item.process for item in operations],
                operations=operations,
            )
        )
    return tuple(candidates)


def _compare_candidate(
    predicted: list[str],
    candidate: GoldenRouteCandidate,
) -> CandidateComparison:
    expected = candidate.expected_processes
    matcher = SequenceMatcher(a=predicted, b=expected, autojunk=False)
    differences: list[EvaluationDifference] = []
    for tag, predicted_start, predicted_end, expected_start, expected_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        differences.append(
            EvaluationDifference(
                kind=tag,
                predicted=predicted[predicted_start:predicted_end],
                expected=expected[expected_start:expected_end],
                predicted_range=[predicted_start + 1, predicted_end],
                expected_range=[expected_start + 1, expected_end],
            )
        )
    return CandidateComparison(
        candidate_id=candidate.candidate_id,
        operation_sequence_match=not differences,
        expected_processes=expected,
        differences=differences,
    )


def evaluate_against_golden(
    material_code: str,
    route: RouteResult,
    golden_candidates: tuple[GoldenRouteCandidate, ...],
) -> GoldenEvaluation:
    predicted = [item.process.strip() for item in route.operations]
    comparisons = [
        _compare_candidate(predicted, candidate)
        for candidate in golden_candidates
    ]
    matched = any(item.operation_sequence_match for item in comparisons)
    issue_codes = [issue.code for issue in route.issues]
    if len(golden_candidates) > 1:
        status = "candidates"
    elif matched and not issue_codes:
        status = "pass"
    else:
        status = "fail"
    return GoldenEvaluation(
        material_code=material_code,
        status=status,
        operation_sequence_match=matched,
        predicted_processes=predicted,
        route_candidates=list(golden_candidates),
        comparisons=comparisons,
        unresolved_route_issues=issue_codes,
    )


def write_case_answer(
    destination: Path,
    evaluation: GoldenEvaluation,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "isolation_contract": {
            "inference_must_not_read_this_file": True,
            "loaded_after_recommendation_persisted": True,
            "purpose": "development evaluation and regression only",
        },
        "material_code": evaluation.material_code,
        "status": (
            "candidates"
            if len(evaluation.route_candidates) > 1
            else "confirmed"
        ),
        "route_candidates": [
            candidate.model_dump(mode="json")
            for candidate in evaluation.route_candidates
        ],
    }
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
