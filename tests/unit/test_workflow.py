import asyncio
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from drawing_route_auditor.decision_tree.runtime import EvaluationScenario
from drawing_route_auditor.workflow.assembler import assemble_recommendation
from drawing_route_auditor.workflow.golden import evaluate_against_golden, load_golden_routes
from drawing_route_auditor.workflow.models import (
    EvidenceRef,
    FactObservation,
    ReaderExecution,
    ReaderPlan,
    ReaderResponse,
    RequestedFeature,
    RuleMatch,
    RouteCandidate,
    RouteOperation,
    RouteRecommendation,
)
from drawing_route_auditor.workflow.readers import build_reader_prompt, read_all
from drawing_route_auditor.workflow.render import prepare_reader_views


def _plan(number: int) -> ReaderPlan:
    return ReaderPlan(
        reader_id=number,
        reader_key=f"reader_{number}",
        label=f"读取器 {number}",
        capability_definition="读取指定图纸事实",
        sequence=number,
        requested_features=[
            RequestedFeature(
                fact_key=f"fact_{number}",
                label=f"事实 {number}",
                subject_scope="current_object",
                value_type="boolean",
                allowed_values=None,
                judgement_definition="按图纸判断",
                hit_criteria="发现明确证据",
                not_hit_criteria="完整检查后未发现",
                coverage_requirement="检查整页",
                evidence_requirement="提供区域和原文",
            )
        ],
    )


def _match(
    *,
    rule_key: str,
    decision_key: str,
    option_key: str,
    option_label: str,
    result_status: str,
    process_name: str,
    operation_key: str,
    order_rank: int,
) -> RuleMatch:
    return RuleMatch(
        node_key="3",
        branch_key="3.1",
        rule_key=rule_key,
        decision_key=decision_key,
        question="选择哪种工艺？",
        option_key=option_key,
        option_label=option_label,
        result_status=result_status,
        outcome_type="process",
        outcome_key=rule_key,
        outcome_value={
            "operation_key": operation_key,
            "process_name": process_name,
            "order_rank": order_rank,
        },
        decisive_facts=["route_family"],
        reason="规则命中",
        missing_facts=[],
    )


def test_not_hit_requires_complete_subject_coverage() -> None:
    with pytest.raises(ValidationError, match="观察范围已完整覆盖"):
        FactObservation(
            fact_key="weld_symbol_present",
            subject_ref="bom-link:3-4",
            status="not_hit",
            value=False,
            evidence=[EvidenceRef(page=1, region="主视图", text="未发现")],
            coverage_complete=False,
        )


def test_fixed_prompt_uses_tree_features_and_response_has_no_processes() -> None:
    prompt = build_reader_prompt(_plan(1))
    schema = ReaderResponse.model_json_schema()

    assert "fact_1" in prompt
    assert "按图纸判断" in prompt
    assert "requested_features" in prompt
    assert set(schema["properties"]) == {"reader_key", "observations"}


def test_reader_views_include_cached_drawing_frame_focus(tmp_path: Path) -> None:
    page = tmp_path / "page-1.png"
    image = Image.new("RGB", (1000, 800), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((100, 200, 700, 650), outline="black", width=3)
    image.save(page)

    first = prepare_reader_views((page,))
    second = prepare_reader_views((page,))

    assert [item.name for item in first] == ["page-1.png", "page-1-focus.png"]
    assert second == first
    with Image.open(first[1]) as focus:
        assert focus.width < image.width
        assert focus.height < image.height


class BarrierReader:
    def __init__(self) -> None:
        self.started = 0
        self.release = asyncio.Event()

    async def read(
        self,
        plan: ReaderPlan,
        pages: tuple[Path, ...],
        subject_context: str,
    ) -> ReaderExecution:
        self.started += 1
        if self.started == 4:
            self.release.set()
        await asyncio.wait_for(self.release.wait(), timeout=1)
        return ReaderExecution(
            reader_key=plan.reader_key,
            reader_label=plan.label,
            status="succeeded",
            response=ReaderResponse(
                reader_key=plan.reader_key,
                observations=[],
            ),
            duration_seconds=0.01,
            prompt_tokens=1,
            completion_tokens=1,
        )


@pytest.mark.asyncio
async def test_four_readers_start_in_one_parallel_wave() -> None:
    adapter = BarrierReader()
    plans = tuple(_plan(number) for number in range(1, 5))

    completed: list[ReaderExecution] = []
    executions = await read_all(
        adapter,
        plans,
        (Path("page-1.png"),),
        "drawing",
        on_complete=completed.append,
    )

    assert adapter.started == 4
    assert len(executions) == 4
    assert all(item.status == "succeeded" for item in executions)
    assert {item.reader_key for item in completed} == {
        "reader_1",
        "reader_2",
        "reader_3",
        "reader_4",
    }


def test_process_candidates_expand_to_complete_routes() -> None:
    rolling = _match(
        rule_key="rolling",
        decision_key="forming",
        option_key="rolling",
        option_label="卷圆",
        result_status="resolved",
        process_name="卷圆",
        operation_key="forming",
        order_rank=30,
    )
    laser = _match(
        rule_key="laser",
        decision_key="blanking",
        option_key="laser",
        option_label="激光下料",
        result_status="candidate",
        process_name="激光下料",
        operation_key="blanking",
        order_rank=10,
    )
    shear = _match(
        rule_key="shear",
        decision_key="blanking",
        option_key="shear",
        option_label="剪板下料",
        result_status="candidate",
        process_name="剪板下料",
        operation_key="blanking",
        order_rank=10,
    )
    scenario = EvaluationScenario(
        facts={"route_family": {"status": "hit", "value": "rolled_sheet_part"}},
        matches=(rolling, laser, shear),
        selected_fact_options=(),
        issues=(),
    )

    result = assemble_recommendation(
        (scenario,),
        tree_version=3,
        evidence_by_fact={
            "route_family": [EvidenceRef(page=1, region="主视图", text="连续回转曲面")]
        },
    )

    assert result.status == "complete_with_candidates"
    assert {
        tuple(operation.process_name for operation in candidate.operations)
        for candidate in result.route_candidates
    } == {
        ("激光下料", "卷圆"),
        ("剪板下料", "卷圆"),
    }
    assert all(
        operation.decisions
        for candidate in result.route_candidates
        for operation in candidate.operations
    )
    laser_operation = next(
        operation
        for candidate in result.route_candidates
        for operation in candidate.operations
        if operation.process_name == "激光下料"
    )
    assert laser_operation.decisions[0].result_status == "candidate"
    assert laser_operation.decisions[0].decisive_facts[0].evidence[0].text == (
        "连续回转曲面"
    )


def test_golden_routes_dedupe_field_variants_but_keep_sequence_variants(
    tmp_path: Path,
) -> None:
    source = tmp_path / "routes.csv"
    source.write_text(
        "存货编码,工序号,工序名称,材料定额,开料尺寸\n"
        "X1,10,激光下料,10,100*100\n"
        "X1,20,折弯,0,\n"
        "X1,10,激光下料,11,101*100\n"
        "X1,20,折弯,0,\n"
        "X1,10,激光下料,12,102*100\n"
        "X1,20,钻孔,0,\n"
        "X1,30,折弯,0,\n",
        encoding="utf-8",
    )

    candidates = load_golden_routes("X1", route_sources=(source,))

    assert len(candidates) == 2
    assert {tuple(candidate.expected_processes) for candidate in candidates} == {
        ("激光下料", "折弯"),
        ("激光下料", "钻孔", "折弯"),
    }
    plain = next(
        item for item in candidates if item.expected_processes == ["激光下料", "折弯"]
    )
    assert len(plain.field_variants) == 2


def test_golden_evaluation_accepts_expected_route_inside_complete_candidates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "routes.csv"
    source.write_text(
        "存货编码,工序号,工序名称\nX1,10,激光下料\nX1,20,折弯\nX1,30,转部装\n",
        encoding="utf-8",
    )
    golden = load_golden_routes("X1", route_sources=(source,))

    def candidate(candidate_id: str, processes: list[str]) -> RouteCandidate:
        return RouteCandidate(
            route_candidate_id=candidate_id,
            operations=[
                RouteOperation(
                    sequence=index,
                    operation_key=f"operation-{index}",
                    process_name=process,
                    source_rule_keys=["test-rule"],
                    decisions=[],
                )
                for index, process in enumerate(processes, start=1)
            ],
        )

    recommendation = RouteRecommendation(
        status="complete_with_candidates",
        route=None,
        route_candidates=[
            candidate("expected", ["激光下料", "折弯", "转部装"]),
            candidate("alternative", ["剪板下料", "折弯", "转部装"]),
        ],
        local_issues=[],
    )

    evaluation = evaluate_against_golden("X1", recommendation, golden)

    assert evaluation.status == "candidates"
    assert evaluation.operation_sequences_match is False
    assert evaluation.missing_sequences == []
    assert evaluation.extra_sequences == [["剪板下料", "折弯", "转部装"]]
