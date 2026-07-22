import asyncio
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from drawing_route_auditor.decision_tree.runtime import (
    EvaluationScenario,
    RuntimeTree,
    observations_to_facts,
)
from drawing_route_auditor.workflow.assembler import assemble_recommendation
from drawing_route_auditor.workflow.golden import evaluate_against_golden, load_golden_routes
from drawing_route_auditor.workflow.models import (
    EvidenceRef,
    DrawingInput,
    FactContract,
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
from drawing_route_auditor.workflow.readers import (
    build_reader_prompt,
    read_all,
    select_reader_views,
    validate_reader_response,
)
from drawing_route_auditor.workflow.render import READER_VIEW_VERSION, prepare_reader_views
from drawing_route_auditor.workflow.runner import (
    _require_pdf_only_runtime,
    WorkflowConfigurationError,
)


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


def test_not_hit_normalizes_numeric_zero_from_reader() -> None:
    observation = FactObservation(
        fact_key="independent_weld_joint_group_count",
        subject_ref="current_object",
        status="not_hit",
        value=0,
        evidence=[EvidenceRef(page=1, region="整页", text="未发现焊接接头")],
        coverage_complete=True,
    )

    assert observation.value is False


def test_not_hit_requires_false_value() -> None:
    with pytest.raises(ValidationError, match="value 必须为 false"):
        FactObservation(
            fact_key="weld_symbol_present",
            subject_ref="bom-link:3-4",
            status="not_hit",
            value=True,
            evidence=[EvidenceRef(page=1, region="主视图", text="未发现")],
            coverage_complete=True,
        )


@pytest.mark.parametrize("field", ["external_facts", "material_code"])
def test_drawing_input_rejects_non_pdf_context(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DrawingInput.model_validate(
            {
                "pdf_path": "drawing.pdf",
                field: {},
            }
        )


def test_route_runtime_rejects_external_fact_contracts() -> None:
    runtime = RuntimeTree(
        revision_id=1,
        tree_key="legacy-tree",
        revision=1,
        plans=(),
        fact_contracts={
            "plm_part_name": FactContract(
                fact_key="plm_part_name",
                label="PLM 物料名称",
                source_kind="external",
                subject_scope="current_object",
                value_type="text",
                allowed_values=None,
            )
        },
    )

    with pytest.raises(WorkflowConfigurationError, match="禁止外部事实"):
        _require_pdf_only_runtime(runtime)


def test_title_flags_are_derived_only_from_main_title_name() -> None:
    evidence = [EvidenceRef(page=1, region="主标题栏名称", text="横梁部件")]
    plan = ReaderPlan(
        reader_id=1,
        reader_key="document_structure_reader",
        label="文档结构读取器",
        capability_definition="读取主标题栏",
        sequence=1,
        requested_features=[
            RequestedFeature(
                fact_key="part_name",
                label="名称",
                subject_scope="current_object",
                value_type="text",
                allowed_values=None,
                judgement_definition="逐字读取",
                hit_criteria="名称可辨",
                not_hit_criteria=None,
                coverage_requirement="检查主标题栏",
                evidence_requirement="提供原文",
            ),
            RequestedFeature(
                fact_key="title_contains_welding",
                label="名称包含焊接",
                subject_scope="current_object",
                value_type="boolean",
                allowed_values=None,
                judgement_definition="检查名称",
                hit_criteria="包含焊接",
                not_hit_criteria="不包含焊接",
                coverage_requirement="检查名称",
                evidence_requirement="提供名称原文",
            ),
            RequestedFeature(
                fact_key="title_contains_assembly",
                label="名称包含装配",
                subject_scope="current_object",
                value_type="boolean",
                allowed_values=None,
                judgement_definition="检查名称",
                hit_criteria="包含装配动作",
                not_hit_criteria="不包含装配动作",
                coverage_requirement="检查名称",
                evidence_requirement="提供名称原文",
            ),
        ],
    )
    response = ReaderResponse(
        reader_key=plan.reader_key,
        observations=[
            FactObservation(
                fact_key="part_name",
                subject_ref="current_object",
                status="hit",
                value="横梁部件",
                evidence=evidence,
                coverage_complete=True,
            ),
            FactObservation(
                fact_key="title_contains_welding",
                subject_ref="current_object",
                status="hit",
                value=True,
                evidence=evidence,
                coverage_complete=True,
            ),
            FactObservation(
                fact_key="title_contains_assembly",
                subject_ref="current_object",
                status="hit",
                value=True,
                evidence=evidence,
                coverage_complete=True,
            ),
        ],
    )

    validated = validate_reader_response(plan, response, "current_object")
    observations = {item.fact_key: item for item in validated.observations}

    assert observations["title_contains_welding"].status == "not_hit"
    assert observations["title_contains_welding"].value is False
    assert observations["title_contains_assembly"].status == "not_hit"
    assert observations["title_contains_assembly"].value is False
    assert (
        observations["title_contains_assembly"].evidence
        == observations["part_name"].evidence
    )


def test_fixed_prompt_uses_tree_features_and_response_has_no_processes() -> None:
    prompt = build_reader_prompt(_plan(1))
    schema = ReaderResponse.model_json_schema()

    assert "fact_1" in prompt
    assert "按图纸判断" in prompt
    assert "requested_features" in prompt
    assert set(schema["properties"]) == {"reader_key", "observations"}
    source_type_schema = schema["$defs"]["DrawingEvidenceRef"]["properties"][
        "source_type"
    ]
    assert source_type_schema["const"] == "drawing"


def test_evidence_removes_postgresql_nul_characters() -> None:
    evidence = EvidenceRef(
        page=1,
        region="主\x00视图",
        text="焊缝\x00证据",
    )

    assert evidence.region == "主视图"
    assert evidence.text == "焊缝证据"


def test_reader_response_rejects_non_drawing_evidence() -> None:
    observation = FactObservation(
        fact_key="fact_1",
        subject_ref="drawing",
        status="hit",
        value=True,
        evidence=[EvidenceRef(source_type="plm", text="PLM 不是图纸证据")],
        coverage_complete=True,
    )

    with pytest.raises(ValidationError, match="drawing"):
        ReaderResponse(reader_key="reader_1", observations=[observation])


def test_reader_views_include_cached_responsibility_regions(tmp_path: Path) -> None:
    page = tmp_path / "page-1.png"
    image = Image.new("RGB", (1000, 800), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((100, 200, 700, 650), outline="black", width=3)
    image.save(page)

    first = prepare_reader_views((page,))
    second = prepare_reader_views((page,))

    assert [item.name for item in first] == [
        "page-1.png",
        f"page-1-title-{READER_VIEW_VERSION}.png",
        f"page-1-geometry-{READER_VIEW_VERSION}.png",
        f"page-1-requirements-{READER_VIEW_VERSION}.png",
    ]
    assert second == first
    document_plan = _plan(1).model_copy(
        update={"reader_key": "document_structure_reader"}
    )
    assert [item.name for item in select_reader_views(document_plan, first)] == [
        "page-1.png",
        f"page-1-title-{READER_VIEW_VERSION}.png",
    ]
    geometry_plan = _plan(2).model_copy(
        update={"reader_key": "geometry_dimension_reader"}
    )
    assert [item.name for item in select_reader_views(geometry_plan, first)] == [
        f"page-1-geometry-{READER_VIEW_VERSION}.png",
    ]


def test_portrait_reader_views_include_clockwise_corrections(tmp_path: Path) -> None:
    page = tmp_path / "page-1.png"
    image = Image.new("RGB", (800, 1000), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((100, 100, 700, 900), outline="black", width=3)
    image.save(page)

    views = prepare_reader_views((page,))

    assert [item.name for item in views] == [
        "page-1.png",
        f"page-1-title-{READER_VIEW_VERSION}.png",
        f"page-1-geometry-{READER_VIEW_VERSION}.png",
        f"page-1-requirements-{READER_VIEW_VERSION}.png",
        f"page-1-title-{READER_VIEW_VERSION}-rotated.png",
        f"page-1-geometry-{READER_VIEW_VERSION}-rotated.png",
        f"page-1-requirements-{READER_VIEW_VERSION}-rotated.png",
    ]
    document_plan = _plan(1).model_copy(
        update={"reader_key": "document_structure_reader"}
    )
    assert [item.name for item in select_reader_views(document_plan, views)] == [
        "page-1.png",
        f"page-1-title-{READER_VIEW_VERSION}.png",
        f"page-1-title-{READER_VIEW_VERSION}-rotated.png",
    ]


def test_reader_response_is_checked_against_dynamic_plan() -> None:
    plan = _plan(1)
    valid = ReaderResponse(
        reader_key=plan.reader_key,
        observations=[
            FactObservation(
                fact_key="fact_1",
                subject_ref="drawing",
                status="hit",
                value=True,
                evidence=[EvidenceRef(page=1, region="主视图", text="明确证据")],
                coverage_complete=True,
            )
        ],
    )

    assert validate_reader_response(plan, valid, "drawing") == valid
    invalid = valid.model_copy(
        update={
            "observations": [valid.observations[0].model_copy(update={"evidence": []})]
        }
    )
    with pytest.raises(ValueError, match="缺少要求的证据"):
        validate_reader_response(plan, invalid, "drawing")


def test_fact_merge_keeps_subjects_before_presence_aggregation() -> None:
    def execution(observations: list[FactObservation]) -> ReaderExecution:
        return ReaderExecution(
            reader_key="symbol_relation_reader",
            reader_label="符号关系读取器",
            status="succeeded",
            response=ReaderResponse(
                reader_key="symbol_relation_reader",
                observations=observations,
            ),
            duration_seconds=0.1,
            prompt_tokens=1,
            completion_tokens=1,
        )

    hit = FactObservation(
        fact_key="weld_symbol_present",
        subject_ref="occurrence:A",
        status="hit",
        value=True,
        evidence=[EvidenceRef(page=1, region="A", text="焊接符号")],
        coverage_complete=True,
    )
    not_hit = hit.model_copy(
        update={
            "subject_ref": "occurrence:B",
            "status": "not_hit",
            "value": False,
            "evidence": [EvidenceRef(page=1, region="B", text="完整检查未发现")],
        }
    )
    facts, issues = observations_to_facts((execution([hit, not_hit]),))

    assert facts["weld_symbol_present"] == {"status": "hit", "value": True}
    assert issues == []

    conflicting = not_hit.model_copy(update={"subject_ref": "occurrence:A"})
    facts, issues = observations_to_facts((execution([hit, conflicting]),))
    assert facts["weld_symbol_present"] == {"status": "conflict"}
    assert {issue.code for issue in issues} == {"SUBJECT_OBSERVATION_CONFLICT"}


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
        tree_revision=3,
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
