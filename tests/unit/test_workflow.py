from pathlib import Path

import pytest
from pydantic import ValidationError

from drawing_route_auditor.workflow.assembler import assemble_route
from drawing_route_auditor.workflow.golden import load_golden_routes
from drawing_route_auditor.workflow.models import (
    DrawingCase,
    EvidenceRef,
    FactObservation,
    FlowIssue,
    FlowResult,
    ReaderOperation,
    RouteConstraint,
)
from drawing_route_auditor.workflow.planning import plan_dispatch


def _case() -> DrawingCase:
    return DrawingCase(
        material_code="DEMO-PLATE-001",
        pdf_path=Path("drawing.pdf"),
        drawing_no="BG.T.8016#18",
        part_name="斗体",
        material_type="下料折弯件",
        parent_drawing_no="BG.T.5034#7",
        parent_material_code="DEMO-PARENT-001",
        parent_name="斗体部件",
        parent_part_type="焊接结构件",
        source_batch="第2批",
    )


def _operation(key: str, process: str) -> ReaderOperation:
    return ReaderOperation(
        operation_key=key,
        process=process,
        content="",
        targets=["part"],
        necessity_status="confirmed_required",
        execution_state="ready",
        blocked_by=[],
    )


def _flow(
    flow_id: str,
    *operations: ReaderOperation,
    status: str = "complete",
    issues: list[FlowIssue] | None = None,
    constraints: list[RouteConstraint] | None = None,
    observations: list[FactObservation] | None = None,
) -> FlowResult:
    return FlowResult(
        flow_id=flow_id,
        status=status,
        observations=observations or [],
        operations=list(operations),
        constraints=constraints or [],
        issues=issues or [],
    )


def test_not_hit_requires_complete_subject_coverage() -> None:
    with pytest.raises(ValidationError, match="complete observation coverage"):
        FactObservation(
            fact_key="weld_symbol_present",
            subject_ref="bom-link:3-4",
            status="not_hit",
            value=False,
            evidence=[EvidenceRef(page=1, region="主视图", text="未发现")],
            coverage_complete=False,
        )


def test_dispatch_skips_unneeded_machining_reader() -> None:
    dispatch = plan_dispatch(_case())

    assert dispatch.enabled_vision_flows == (
        "blanking",
        "surface_cleaning",
        "forming",
        "connection",
    )
    assert dispatch.skipped_vision_flows == ("machining",)


def test_partial_flow_blocks_transfer_but_preserves_safe_operations() -> None:
    issue = FlowIssue(
        kind="error",
        code="FORMING_STAGE_UNDETERMINED",
        message="缺少校圆阶段规则",
        affected_operation_keys=[],
        missing_facts=["forming_stage"],
        candidate_options=[],
    )
    route = assemble_route(
        (
            _flow("blanking", _operation("cut", "激光下料")),
            _flow(
                "forming",
                _operation("roll", "卷圆"),
                status="partial",
                issues=[issue],
            ),
            _flow("transfer", _operation("to-parent", "转焊接")),
        )
    )

    assert [item.process for item in route.operations] == [
        "激光下料",
        "卷圆",
        "转焊接",
    ]
    assert route.operations[0].execution_state == "ready"
    assert route.operations[1].execution_state == "ready"
    assert route.operations[2].execution_state == "blocked"
    assert route.committable_operation_keys == ["blanking.cut", "forming.roll"]
    assert route.status == "partial"


def test_conflicting_reader_constraint_is_not_silently_accepted() -> None:
    route = assemble_route(
        (
            _flow(
                "forming",
                _operation("roll", "卷圆"),
                _operation("calibrate", "校形"),
                constraints=[
                    RouteConstraint(
                        before_operation="calibrate",
                        after_operation="roll",
                        reason="错误方向",
                    )
                ],
            ),
        )
    )

    assert "CONSTRAINT_CONFLICT" in {issue.code for issue in route.issues}
    assert all(item.execution_state == "blocked" for item in route.operations)
    assert [item.process for item in route.operations] == ["卷圆", "校形"]


def test_conflicting_reader_facts_block_dependent_operations() -> None:
    blanking_observation = FactObservation(
        fact_key="sheet_thickness_mm",
        subject_ref="DEMO-PLATE-001",
        status="hit",
        value=2.5,
        evidence=[EvidenceRef(page=1, region="尺寸", text="2.5")],
        coverage_complete=True,
    )
    forming_observation = blanking_observation.model_copy(update={"value": 12.5})

    route = assemble_route(
        (
            _flow(
                "blanking",
                _operation("cut", "激光下料"),
                observations=[blanking_observation],
            ),
            _flow(
                "forming",
                _operation("roll", "卷圆"),
                observations=[forming_observation],
            ),
        )
    )

    assert "FACT_OBSERVATION_CONFLICT" in {
        issue.code for issue in route.issues
    }
    assert all(item.execution_state == "blocked" for item in route.operations)


def test_golden_loader_preserves_duplicate_route_versions_as_candidates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "routes.csv"
    source.write_text(
        "存货编码,工序号,工序名称,工艺内容,材料定额,开料尺寸\n"
        "X1,10,激光下料,工艺图,10,100*100\n"
        "X1,20,折弯,,0,\n"
        "X1,10,激光下料,工艺图,11,101*100\n"
        "X1,20,折弯,,0,\n",
        encoding="utf-8",
    )

    candidates = load_golden_routes("X1", route_sources=(source,))

    assert len(candidates) == 2
    assert all(
        candidate.expected_processes == ["激光下料", "折弯"]
        for candidate in candidates
    )
    assert candidates[0].candidate_id != candidates[1].candidate_id
