from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter

from drawing_route_auditor.config import Settings
from drawing_route_auditor.db.connection import connect
from drawing_route_auditor.workflow.assembler import assemble_route
from drawing_route_auditor.workflow.golden import (
    GoldenEvaluation,
    evaluate_against_golden,
    load_golden_routes,
    write_case_answer,
)
from drawing_route_auditor.workflow.models import (
    DrawingCase,
    FlowReader,
    ReaderExecution,
    WorkflowResult,
)
from drawing_route_auditor.workflow.planning import infer_transfer, plan_dispatch
from drawing_route_auditor.workflow.readers import OpenAIFlowReader, read_flows
from drawing_route_auditor.workflow.render import render_pdf
from drawing_route_auditor.workflow.repository import (
    create_run,
    fail_run,
    finish_task,
    persist_evaluation,
    persist_workflow_result,
    start_tasks,
)


PROMPT_VERSION = "flow-readers-v3"
KNOWLEDGE_VERSION = "conditional-workflow-v3"
EVALUATOR_VERSION = "golden-candidates-v2"


class WorkflowConfigurationError(ValueError):
    pass


async def run_drawing(
    case: DrawingCase,
    settings: Settings,
    *,
    reader: FlowReader | None = None,
    runtime_root: Path = Path(".runtime"),
) -> WorkflowResult:
    started = perf_counter()
    drawing_sha256 = sha256(case.pdf_path.read_bytes()).hexdigest()
    dispatch = plan_dispatch(case)
    knowledge_snapshot: dict[str, object] = {
        "knowledge_version": KNOWLEDGE_VERSION,
        "reader_prompt_version": PROMPT_VERSION,
        "reader_model_version": settings.vision_model,
        "decision_tree": {
            "tree_key": "drawing-process-tree",
            "version": 1,
            "status": "draft",
        },
        "validated_product_family_experience": {
            "thin_nonrectangular_sheet_blanking": {
                "source": "development-only aggregate of docs/routes_1.csv and docs/routes_2.csv",
                "population": "PLM material_type=下料折弯件",
                "laser_first_operation": 169,
                "total_with_known_first_operation": 186,
                "use": "flow rule, never per-material lookup during inference",
            }
        },
        "dispatch": {
            "object_kind": dispatch.object_kind,
            "ownership_mode": dispatch.ownership_mode,
            "enabled_vision_flows": list(dispatch.enabled_vision_flows),
            "skipped_vision_flows": list(dispatch.skipped_vision_flows),
            "reasons": dispatch.reasons,
        },
        "external_context_snapshot": case.model_dump(
            mode="json", exclude={"pdf_path"}
        ),
    }
    task_keys = [
        ("render", "pdf_render"),
        *(
            (f"reader:{flow_id}", "vision_reader")
            for flow_id in dispatch.enabled_vision_flows
        ),
        ("infer:transfer", "deterministic_inference"),
        ("assemble", "route_assembly"),
    ]
    with connect(settings) as connection:
        run_id = create_run(
            connection,
            case=case,
            drawing_sha256=drawing_sha256,
            knowledge_snapshot=knowledge_snapshot,
            model=settings.vision_model or "unconfigured",
            prompt_version=PROMPT_VERSION,
            task_keys=task_keys,
        )

    try:
        with connect(settings) as connection:
            start_tasks(connection, run_id, ["render"])
        rendered = await render_pdf(
            case.pdf_path,
            output_root=runtime_root / "rendered",
            dpi=settings.render_dpi,
        )
        with connect(settings) as connection:
            finish_task(
                connection,
                run_id,
                "render",
                status="succeeded",
                result={
                    "pages": [str(page) for page in rendered.pages],
                    "cache_hit": rendered.cache_hit,
                    "duration_seconds": rendered.duration_seconds,
                },
            )

        active_reader = reader or _configured_reader(settings)
        reader_task_keys = [
            *(f"reader:{flow_id}" for flow_id in dispatch.enabled_vision_flows),
            "infer:transfer",
        ]
        with connect(settings) as connection:
            start_tasks(connection, run_id, reader_task_keys)

        inference_started = perf_counter()
        vision_future = read_flows(
            active_reader,
            dispatch.enabled_vision_flows,
            rendered.pages,
            case,
        )
        transfer_future = asyncio.to_thread(infer_transfer, case)
        vision_executions, transfer_result = await asyncio.gather(
            vision_future,
            transfer_future,
        )
        inference_seconds = perf_counter() - inference_started
        transfer_execution = ReaderExecution(
            flow_result=transfer_result,
            duration_seconds=0,
            prompt_tokens=0,
            completion_tokens=0,
        )
        reader_executions = (*vision_executions, transfer_execution)

        with connect(settings) as connection:
            for execution in reader_executions:
                flow_id = execution.flow_result.flow_id
                task_key = (
                    "infer:transfer"
                    if flow_id == "transfer"
                    else f"reader:{flow_id}"
                )
                finish_task(
                    connection,
                    run_id,
                    task_key,
                    status="succeeded",
                    result=execution.model_dump(mode="json"),
                )
            start_tasks(connection, run_id, ["assemble"])

        flow_results = tuple(
            execution.flow_result for execution in reader_executions
        )
        route = assemble_route(flow_results)
        elapsed_seconds = perf_counter() - started
        workflow = WorkflowResult(
            run_id=run_id,
            case=case,
            drawing_sha256=rendered.drawing_sha256,
            knowledge_snapshot=knowledge_snapshot,
            dispatched_flows=[
                *dispatch.enabled_vision_flows,
                "transfer",
            ],
            skipped_flows=list(dispatch.skipped_vision_flows),
            reader_executions=list(reader_executions),
            route=route,
            elapsed_seconds=elapsed_seconds,
            render_seconds=rendered.duration_seconds,
            inference_seconds=inference_seconds,
        )
        with connect(settings) as connection:
            finish_task(
                connection,
                run_id,
                "assemble",
                status="succeeded",
                result=route.model_dump(mode="json"),
            )
            persist_workflow_result(connection, workflow)
        return workflow
    except Exception as error:
        with connect(settings) as connection:
            fail_run(
                connection,
                run_id,
                error_code=type(error).__name__,
                error_message=str(error),
            )
        raise


async def run_and_evaluate(
    case: DrawingCase,
    settings: Settings,
    *,
    reader: FlowReader | None = None,
    runtime_root: Path = Path(".runtime"),
    route_sources: tuple[Path, ...] = (
        Path("docs/routes_1.csv"),
        Path("docs/routes_2.csv"),
    ),
    answer_path: Path | None = None,
) -> tuple[WorkflowResult, GoldenEvaluation]:
    workflow = await run_drawing(
        case,
        settings,
        reader=reader,
        runtime_root=runtime_root,
    )

    # Isolation seam: the recommendation is persisted before any golden data is read.
    golden = load_golden_routes(
        case.material_code,
        route_sources=route_sources,
    )
    evaluation = evaluate_against_golden(
        case.material_code,
        workflow.route,
        golden,
    )
    with connect(settings) as connection:
        persist_evaluation(
            connection,
            workflow.run_id,
            evaluation,
            evaluator_version=EVALUATOR_VERSION,
        )
    if answer_path is not None:
        write_case_answer(answer_path, evaluation)
    return workflow, evaluation


def load_case(path: Path) -> DrawingCase:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return DrawingCase.model_validate(payload)


def _configured_reader(settings: Settings) -> OpenAIFlowReader:
    if settings.openai_base_url is None:
        raise WorkflowConfigurationError("OPENAI_BASE_URL is not configured")
    if settings.openai_api_key is None:
        raise WorkflowConfigurationError("OPENAI_API_KEY is not configured")
    if settings.vision_model is None:
        raise WorkflowConfigurationError("MODEL is not configured")
    return OpenAIFlowReader(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.vision_model,
        timeout_seconds=settings.vision_timeout_seconds,
    )
