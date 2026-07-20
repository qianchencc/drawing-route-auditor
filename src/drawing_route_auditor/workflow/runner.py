from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from drawing_route_auditor.config import Settings
from drawing_route_auditor.db.connection import connect
from drawing_route_auditor.decision_tree.runtime import (
    evaluate_scenarios,
    EvaluationScenario,
    load_runtime_tree,
    observations_to_facts,
)
from drawing_route_auditor.workflow.assembler import assemble_recommendation, collect_fact_evidence
from drawing_route_auditor.workflow.golden import (
    GoldenEvaluation,
    evaluate_against_golden,
    load_golden_routes,
    write_case_answer,
)
from drawing_route_auditor.workflow.models import (
    DrawingInput,
    ReaderAdapter,
    ProgressCallback,
    ReaderExecution,
    RuleMatch,
    WorkflowResult,
    WorkflowProgress,
)
from drawing_route_auditor.workflow.readers import (
    PROMPT_TEMPLATE_VERSION,
    OpenAIReaderAdapter,
    read_all,
)
from drawing_route_auditor.workflow.render import prepare_reader_views, render_pdf
from drawing_route_auditor.workflow.repository import (
    create_run,
    fail_run,
    finish_task,
    persist_evaluation,
    persist_reader_executions,
    persist_workflow_result,
    start_tasks,
)


DEFAULT_TREE_KEY = "drawing-process-tree"
EVALUATOR_VERSION = "golden-process-sequence-v3"


class WorkflowConfigurationError(ValueError):
    pass


def _notify(
    callback: ProgressCallback | None,
    event: WorkflowProgress,
) -> None:
    if callback is not None:
        callback(event)


async def run_drawing(
    drawing_input: DrawingInput,
    settings: Settings,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
    tree_version: int | None = None,
    adapter: ReaderAdapter | None = None,
    runtime_root: Path = Path(".runtime"),
    progress_callback: ProgressCallback | None = None,
) -> WorkflowResult:
    started = perf_counter()
    _notify(
        progress_callback,
        WorkflowProgress("render", "started", "正在解析 PDF 并生成图纸视图"),
    )
    source = drawing_input.pdf_path.read_bytes()
    drawing_sha256 = sha256(source).hexdigest()
    subject_context = drawing_input.material_code or drawing_input.pdf_path.stem

    with connect(settings) as connection:
        runtime = load_runtime_tree(connection, tree_key, tree_version)
    active_adapter = adapter or _configured_adapter(settings)
    model_version = settings.vision_model or "未配置"
    with connect(settings) as connection:
        run_id, input_id = create_run(
            connection,
            drawing_input=drawing_input,
            drawing_sha256=drawing_sha256,
            runtime=runtime,
            model=model_version,
        )

    try:
        with connect(settings) as connection:
            start_tasks(connection, run_id, ["render"])
        rendered = await render_pdf(
            drawing_input.pdf_path,
            output_root=runtime_root / "rendered",
            dpi=settings.render_dpi,
        )
        reader_views = await asyncio.to_thread(
            prepare_reader_views,
            rendered.pages,
        )
        _notify(
            progress_callback,
            WorkflowProgress(
                "render",
                "completed",
                f"PDF 图纸视图准备完成，共 {len(rendered.pages)} 页",
                duration_seconds=rendered.duration_seconds,
            ),
        )
        with connect(settings) as connection:
            finish_task(
                connection,
                run_id,
                "render",
                succeeded=True,
                result={
                    "pages": [str(page) for page in rendered.pages],
                    "reader_views": [str(view) for view in reader_views],
                    "cache_hit": rendered.cache_hit,
                    "duration_seconds": rendered.duration_seconds,
                },
            )

        reader_task_keys = [f"reader:{plan.reader_key}" for plan in runtime.plans]
        with connect(settings) as connection:
            start_tasks(connection, run_id, reader_task_keys)
        completed_readers = 0

        def reader_completed(execution: ReaderExecution) -> None:
            nonlocal completed_readers
            completed_readers += 1
            _notify(
                progress_callback,
                WorkflowProgress(
                    "readers",
                    "updated",
                    (
                        f"{execution.reader_label}已完成 "
                        f"({completed_readers}/{len(runtime.plans)})"
                    ),
                    completed_readers=completed_readers,
                    total_readers=len(runtime.plans),
                    reader_label=execution.reader_label,
                    duration_seconds=execution.duration_seconds,
                ),
            )

        _notify(
            progress_callback,
            WorkflowProgress(
                "readers",
                "started",
                f"正在并发运行 {len(runtime.plans)} 个图纸读取器",
                total_readers=len(runtime.plans),
            ),
        )

        reader_started = perf_counter()
        executions = await read_all(
            active_adapter,
            runtime.plans,
            reader_views,
            subject_context,
            on_complete=reader_completed,
        )
        reader_seconds = perf_counter() - reader_started
        _notify(
            progress_callback,
            WorkflowProgress(
                "readers",
                "completed",
                f"全部 {len(executions)} 个图纸读取器已完成",
                completed_readers=len(executions),
                total_readers=len(runtime.plans),
                duration_seconds=reader_seconds,
            ),
        )

        with connect(settings) as connection:
            persist_reader_executions(
                connection,
                run_id=run_id,
                input_id=input_id,
                runtime=runtime,
                executions=executions,
                page_paths=[str(view) for view in reader_views],
            )
            for execution in executions:
                finish_task(
                    connection,
                    run_id,
                    f"reader:{execution.reader_key}",
                    succeeded=execution.status == "succeeded",
                    result=execution.model_dump(mode="json"),
                    error_code=execution.error_code,
                    error_message=execution.error_message,
                )
            start_tasks(connection, run_id, ["evaluate"])

        _notify(
            progress_callback,
            WorkflowProgress("evaluate", "started", "正在执行决策树并展开事实闭包"),
        )
        initial_facts, reader_issues = observations_to_facts(executions)
        with connect(settings) as connection:
            scenarios = evaluate_scenarios(
                connection,
                runtime,
                initial_facts,
                reader_issues,
            )
            finish_task(
                connection,
                run_id,
                "evaluate",
                succeeded=True,
                result={
                    "scenario_count": len(scenarios),
                    "facts": [scenario.facts for scenario in scenarios],
                    "matches": [
                        [match.model_dump(mode="json") for match in scenario.matches]
                        for scenario in scenarios
                    ],
                },
            )
            start_tasks(connection, run_id, ["assemble"])
        _notify(
            progress_callback,
            WorkflowProgress(
                "evaluate",
                "completed",
                f"决策树求值完成，共形成 {len(scenarios)} 个场景",
            ),
        )

        _notify(
            progress_callback,
            WorkflowProgress("assemble", "started", "正在组装并校验工艺路线"),
        )
        recommendation = assemble_recommendation(
            scenarios,
            tree_version=runtime.version,
            evidence_by_fact=collect_fact_evidence(executions),
            fact_labels=runtime.fact_labels,
        )
        derived_facts = _common_facts(scenarios)
        rule_matches = _all_matches(scenarios)
        elapsed_seconds = perf_counter() - started
        workflow = WorkflowResult(
            run_id=run_id,
            drawing_input=drawing_input,
            drawing_sha256=rendered.drawing_sha256,
            tree_key=runtime.tree_key,
            tree_version=runtime.version,
            model_version=model_version,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            reader_executions=list(executions),
            derived_facts=derived_facts,
            rule_matches=rule_matches,
            recommendation=recommendation,
            elapsed_seconds=elapsed_seconds,
            render_seconds=rendered.duration_seconds,
            reader_seconds=reader_seconds,
        )
        with connect(settings) as connection:
            finish_task(
                connection,
                run_id,
                "assemble",
                succeeded=True,
                result=recommendation.model_dump(mode="json"),
            )
            persist_workflow_result(
                connection,
                workflow,
                version_id=runtime.version_id,
            )
        _notify(
            progress_callback,
            WorkflowProgress(
                "complete",
                "completed",
                "工艺路线生成完成",
                duration_seconds=elapsed_seconds,
            ),
        )
        return workflow
    except Exception as error:
        _notify(
            progress_callback,
            WorkflowProgress(
                "complete",
                "failed",
                f"工艺路线运行失败：{error}",
            ),
        )
        with connect(settings) as connection:
            fail_run(
                connection,
                run_id,
                error_code=type(error).__name__,
                error_message=str(error),
            )
        raise


async def run_and_evaluate(
    drawing_input: DrawingInput,
    settings: Settings,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
    tree_version: int | None = None,
    adapter: ReaderAdapter | None = None,
    runtime_root: Path = Path(".runtime"),
    progress_callback: ProgressCallback | None = None,
    route_sources: tuple[Path, ...] = (
        Path("docs/routes_1.csv"),
        Path("docs/routes_2.csv"),
    ),
    answer_path: Path | None = None,
) -> tuple[WorkflowResult, GoldenEvaluation]:
    if drawing_input.material_code is None:
        raise ValueError("开发评估必须提供物料编码")
    workflow = await run_drawing(
        drawing_input,
        settings,
        tree_key=tree_key,
        tree_version=tree_version,
        adapter=adapter,
        runtime_root=runtime_root,
        progress_callback=progress_callback,
    )
    golden = load_golden_routes(
        drawing_input.material_code,
        route_sources=route_sources,
    )
    evaluation = evaluate_against_golden(
        drawing_input.material_code,
        workflow.recommendation,
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


def _configured_adapter(settings: Settings) -> OpenAIReaderAdapter:
    if settings.openai_base_url is None:
        raise WorkflowConfigurationError("未配置 OPENAI_BASE_URL")
    if settings.openai_api_key is None:
        raise WorkflowConfigurationError("未配置 OPENAI_API_KEY")
    if settings.vision_model is None:
        raise WorkflowConfigurationError("未配置 MODEL")
    return OpenAIReaderAdapter(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.vision_model,
        timeout_seconds=settings.vision_timeout_seconds,
    )


def _common_facts(
    scenarios: tuple[EvaluationScenario, ...],
) -> dict[str, object]:
    if not scenarios:
        return {}
    first = scenarios[0].facts
    return {
        key: value
        for key, value in first.items()
        if all(scenario.facts.get(key) == value for scenario in scenarios[1:])
    }


def _all_matches(
    scenarios: tuple[EvaluationScenario, ...],
) -> list[RuleMatch]:
    matches: dict[str, RuleMatch] = {}
    for scenario in scenarios:
        for match in scenario.matches:
            matches.setdefault(match.rule_key, match)
    return list(matches.values())
