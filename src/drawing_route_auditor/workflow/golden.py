from __future__ import annotations

import csv
from hashlib import sha256
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from drawing_route_auditor.workflow.models import RouteRecommendation

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROUTE_SOURCES: tuple[Path, ...] = (
    _PROJECT_ROOT / "docs/routes_1.csv",
    _PROJECT_ROOT / "docs/routes_2.csv",
)


class GoldenOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_number: str
    process_name: str
    source_file: str
    source_row: int
    raw: dict[str, str]


class GoldenRouteCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    expected_processes: list[str]
    field_variants: list[list[GoldenOperation]]
    source_ranges: list[str]


class GoldenEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_code: str
    status: str
    operation_sequences_match: bool
    predicted_sequences: list[list[str]]
    expected_sequences: list[list[str]]
    missing_sequences: list[list[str]]
    extra_sequences: list[list[str]]
    route_candidates: list[GoldenRouteCandidate]
    unresolved_route_issues: list[str]


def _clean(value: str | None) -> str:
    if value is None or value == "NULL":
        return ""
    return value.strip()


def _number(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return float("inf")


def _operation(
    row: dict[str, str],
    *,
    source: Path,
    row_number: int,
) -> GoldenOperation:
    return GoldenOperation(
        operation_number=_clean(row.get("工序号")),
        process_name=_clean(row.get("工序名称")),
        source_file=str(source),
        source_row=row_number,
        raw={str(key): _clean(value) for key, value in row.items()},
    )


def _split_source_versions(
    material_code: str,
    source: Path,
) -> list[list[GoldenOperation]]:
    versions: list[list[GoldenOperation]] = []
    current: list[GoldenOperation] = []
    previous_number: float | None = None
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            if _clean(row.get("存货编码")) != material_code:
                continue
            operation = _operation(
                row,
                source=source,
                row_number=row_number,
            )
            number = _number(operation.operation_number)
            if current and previous_number is not None and number <= previous_number:
                versions.append(current)
                current = []
            current.append(operation)
            previous_number = number
    if current:
        versions.append(current)
    return versions


def load_golden_routes(
    material_code: str,
    *,
    route_sources: tuple[Path, ...],
) -> tuple[GoldenRouteCandidate, ...]:
    versions: list[list[GoldenOperation]] = []
    for source in route_sources:
        versions.extend(_split_source_versions(material_code, source))
    if not versions:
        raise LookupError(f"未找到物料 {material_code} 的标准工艺路线")

    grouped: dict[tuple[str, ...], list[list[GoldenOperation]]] = {}
    for operations in versions:
        sequence = tuple(item.process_name for item in operations)
        grouped.setdefault(sequence, []).append(operations)

    candidates: list[GoldenRouteCandidate] = []
    for sequence, field_variants in sorted(grouped.items()):
        signature = json.dumps(sequence, ensure_ascii=False)
        candidate_id = sha256(signature.encode("utf-8")).hexdigest()[:16]
        source_ranges = [
            (
                f"{variant[0].source_file}:"
                f"{variant[0].source_row}-{variant[-1].source_row}"
            )
            for variant in field_variants
        ]
        candidates.append(
            GoldenRouteCandidate(
                candidate_id=candidate_id,
                expected_processes=list(sequence),
                field_variants=field_variants,
                source_ranges=source_ranges,
            )
        )
    return tuple(candidates)


def _predicted_sequences(
    recommendation: RouteRecommendation,
) -> list[list[str]]:
    if recommendation.route_candidates:
        return [
            [operation.process_name for operation in candidate.operations]
            for candidate in recommendation.route_candidates
        ]
    if recommendation.route is not None:
        return [[operation.process_name for operation in recommendation.route]]
    return []


def evaluate_against_golden(
    material_code: str,
    recommendation: RouteRecommendation,
    golden_candidates: tuple[GoldenRouteCandidate, ...],
) -> GoldenEvaluation:
    predicted = _predicted_sequences(recommendation)
    expected = [candidate.expected_processes for candidate in golden_candidates]
    predicted_set = {tuple(sequence) for sequence in predicted}
    expected_set = {tuple(sequence) for sequence in expected}
    missing = [list(sequence) for sequence in sorted(expected_set - predicted_set)]
    extra = [list(sequence) for sequence in sorted(predicted_set - expected_set)]
    exact_match = not missing and not extra
    covers_expected = not missing
    issue_codes = [issue.code for issue in recommendation.local_issues]
    if issue_codes or not covers_expected:
        status = "fail"
    elif exact_match and len(golden_candidates) == 1:
        status = "pass"
    else:
        status = "candidates"
    return GoldenEvaluation(
        material_code=material_code,
        status=status,
        operation_sequences_match=exact_match,
        predicted_sequences=predicted,
        expected_sequences=expected,
        missing_sequences=missing,
        extra_sequences=extra,
        route_candidates=list(golden_candidates),
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
            "comparison_scope": [
                "process_name",
                "process_order",
                "same_process_occurrence_count",
            ],
        },
        "material_code": evaluation.material_code,
        "status": (
            "candidates" if len(evaluation.route_candidates) > 1 else "confirmed"
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
