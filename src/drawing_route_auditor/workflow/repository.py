from __future__ import annotations

from typing import Iterable

from psycopg2.extras import Json

from drawing_route_auditor.db.connection import Connection
from drawing_route_auditor.decision_tree.runtime import RuntimeTree
from drawing_route_auditor.workflow.golden import GoldenEvaluation
from drawing_route_auditor.workflow.models import (
    DrawingInput,
    ReaderExecution,
    WorkflowResult,
)
from drawing_route_auditor.workflow.readers import (
    OUTPUT_SCHEMA_VERSION,
    PROMPT_TEMPLATE_VERSION,
)


def create_run(
    connection: Connection,
    *,
    drawing_input: DrawingInput,
    drawing_sha256: str,
    runtime: RuntimeTree,
    model: str,
) -> tuple[str, int, int | None]:
    task_keys = [
        ("render", "pdf_render"),
        *((f"reader:{plan.reader_key}", "vision_reader") for plan in runtime.plans),
        ("evaluate", "decision_tree_evaluation"),
        ("assemble", "route_assembly"),
    ]
    with connection.transaction():
        row = connection.execute(
            """
            INSERT INTO runs (
                status, knowledge_version, knowledge_snapshot,
                reader_model_version, reader_prompt_version, started_at
            )
            VALUES ('running', %s, %s, %s, %s, now())
            RETURNING id
            """,
            (
                runtime.tree_key,
                Json(
                    {
                        "tree_key": runtime.tree_key,
                        "tree_revision": runtime.revision,
                        "reader_keys": [plan.reader_key for plan in runtime.plans],
                        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                        "output_schema_version": OUTPUT_SCHEMA_VERSION,
                    }
                ),
                model,
                PROMPT_TEMPLATE_VERSION,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("创建运行记录失败")
        run_id = str(row["id"])
        input_row = connection.execute(
            """
            INSERT INTO run_inputs (
                run_id, input_kind, source_path, source_sha256, metadata
            )
            VALUES (%s, 'pdf', %s, %s, %s)
            RETURNING id
            """,
            (
                run_id,
                str(drawing_input.pdf_path),
                drawing_sha256,
                Json({"material_code": drawing_input.material_code}),
            ),
        ).fetchone()
        if input_row is None:
            raise RuntimeError("创建运行输入记录失败")
        context_input_id: int | None = None
        if drawing_input.external_facts:
            context_row = connection.execute(
                """
                INSERT INTO run_inputs (
                    run_id, input_kind, source_path, metadata
                )
                VALUES (%s, 'context', 'cli:external-facts', %s)
                RETURNING id
                """,
                (
                    run_id,
                    Json(
                        {
                            key: value.model_dump(mode="json")
                            for key, value in drawing_input.external_facts.items()
                        }
                    ),
                ),
            ).fetchone()
            if context_row is None:
                raise RuntimeError("创建外部事实输入记录失败")
            context_input_id = context_row["id"]
        for task_key, task_type in task_keys:
            connection.execute(
                """
                INSERT INTO tasks (run_id, task_key, task_type)
                VALUES (%s, %s, %s)
                """,
                (run_id, task_key, task_type),
            )
        tasks = connection.execute(
            "SELECT id, task_key FROM tasks WHERE run_id = %s",
            (run_id,),
        ).fetchall()
        task_ids = {item["task_key"]: item["id"] for item in tasks}
        for plan in runtime.plans:
            connection.execute(
                """
                INSERT INTO reader_requests (
                    run_id, version_id, reader_id, task_id,
                    requested_features, prompt_template_version,
                    output_schema_version, model_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    runtime.revision_id,
                    plan.reader_id,
                    task_ids[f"reader:{plan.reader_key}"],
                    Json(
                        [
                            feature.model_dump(mode="json")
                            for feature in plan.requested_features
                        ]
                    ),
                    PROMPT_TEMPLATE_VERSION,
                    OUTPUT_SCHEMA_VERSION,
                    model,
                ),
            )
    return run_id, input_row["id"], context_input_id


def persist_external_facts(
    connection: Connection,
    *,
    run_id: str,
    input_id: int | None,
    runtime: RuntimeTree,
    drawing_input: DrawingInput,
) -> None:
    if not drawing_input.external_facts:
        return
    fact_rows = connection.execute(
        """
        SELECT id, fact_key, subject_scope
        FROM fact_definitions
        WHERE version_id = %s AND source_kind = 'external'
        """,
        (runtime.revision_id,),
    ).fetchall()
    definitions = {row["fact_key"]: row for row in fact_rows}
    unknown = set(drawing_input.external_facts) - set(definitions)
    if unknown:
        raise ValueError(f"当前决策树未声明外部事实：{sorted(unknown)}")
    default_subject = drawing_input.material_code or drawing_input.pdf_path.stem
    with connection.transaction():
        for fact_key, external in drawing_input.external_facts.items():
            definition = definitions[fact_key]
            row = connection.execute(
                """
                INSERT INTO fact_observations (
                    run_id, fact_definition_id, status, scope,
                    subject_ref, value, observation_coverage, coverage_complete
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, true)
                RETURNING id
                """,
                (
                    run_id,
                    definition["id"],
                    external.status,
                    definition["subject_scope"],
                    Json({"ref": external.subject_ref or default_subject}),
                    Json(external.value) if external.value is not None else None,
                    Json(
                        {
                            "source_ref": external.source_ref,
                            "is_complete_for_subject": True,
                        }
                    ),
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("保存外部事实失败")
            connection.execute(
                """
                INSERT INTO evidence (
                    fact_observation_id, input_id, source_type, original_text
                )
                VALUES (%s, %s, 'plm', %s)
                """,
                (row["id"], input_id, external.source_ref),
            )


def start_tasks(
    connection: Connection,
    run_id: str,
    task_keys: Iterable[str],
) -> None:
    with connection.transaction():
        for task_key in task_keys:
            task = connection.execute(
                """
                UPDATE tasks
                SET status = 'running', started_at = now(), worker_id = 'local',
                    attempt_count = attempt_count + 1
                WHERE run_id = %s AND task_key = %s AND status = 'queued'
                RETURNING id, attempt_count
                """,
                (run_id, task_key),
            ).fetchone()
            if task is None:
                raise RuntimeError(f"任务不在排队状态：{task_key}")
            connection.execute(
                """
                INSERT INTO task_attempts (
                    task_id, attempt_number, worker_id, status
                )
                VALUES (%s, %s, 'local', 'running')
                """,
                (task["id"], task["attempt_count"]),
            )


def finish_task(
    connection: Connection,
    run_id: str,
    task_key: str,
    *,
    succeeded: bool,
    result: dict[str, object] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    status = "succeeded" if succeeded else "error"
    with connection.transaction():
        task = connection.execute(
            """
            UPDATE tasks
            SET status = %s, result = %s, finished_at = now(),
                error_code = %s, error_message = %s
            WHERE run_id = %s AND task_key = %s AND status = 'running'
            RETURNING id, attempt_count
            """,
            (
                status,
                Json(result) if result is not None else None,
                error_code,
                error_message,
                run_id,
                task_key,
            ),
        ).fetchone()
        if task is None:
            raise RuntimeError(f"任务不在运行状态：{task_key}")
        connection.execute(
            """
            UPDATE task_attempts
            SET status = %s, finished_at = now(),
                error_code = %s, error_message = %s
            WHERE task_id = %s AND attempt_number = %s
            """,
            (
                status,
                error_code,
                error_message,
                task["id"],
                task["attempt_count"],
            ),
        )


def persist_reader_executions(
    connection: Connection,
    *,
    run_id: str,
    input_id: int,
    runtime: RuntimeTree,
    executions: tuple[ReaderExecution, ...],
    page_paths: list[str],
) -> None:
    fact_rows = connection.execute(
        """
        SELECT fact.id, fact.fact_key, reader.reader_key
        FROM fact_definitions AS fact
        JOIN decision_readers AS reader ON reader.id = fact.reader_id
        WHERE fact.version_id = %s
        """,
        (runtime.revision_id,),
    ).fetchall()
    fact_ids = {(row["reader_key"], row["fact_key"]): row["id"] for row in fact_rows}
    plan_ids = {plan.reader_key: plan.reader_id for plan in runtime.plans}
    task_rows = connection.execute(
        "SELECT id, task_key FROM tasks WHERE run_id = %s",
        (run_id,),
    ).fetchall()
    task_ids = {row["task_key"]: row["id"] for row in task_rows}

    with connection.transaction():
        for execution in executions:
            reader_id = plan_ids[execution.reader_key]
            connection.execute(
                """
                UPDATE reader_requests
                SET status = %s, page_inputs = %s,
                    duration_milliseconds = %s,
                    prompt_tokens = %s, completion_tokens = %s,
                    error_code = %s, error_message = %s,
                    finished_at = now()
                WHERE run_id = %s AND reader_id = %s
                """,
                (
                    execution.status,
                    Json(execution.page_inputs or page_paths),
                    round(execution.duration_seconds * 1000),
                    execution.prompt_tokens,
                    execution.completion_tokens,
                    execution.error_code,
                    execution.error_message,
                    run_id,
                    reader_id,
                ),
            )
            if execution.response is None:
                continue
            for observation in execution.response.observations:
                fact_id = fact_ids[(execution.reader_key, observation.fact_key)]
                coverage = {
                    "inspected_pages": sorted(
                        {item.page for item in observation.evidence}
                    ),
                    "inspected_regions": [item.region for item in observation.evidence],
                    "is_complete_for_subject": observation.coverage_complete,
                }
                row = connection.execute(
                    """
                    INSERT INTO fact_observations (
                        run_id, fact_definition_id, reader_task_id,
                        status, scope, subject_ref, value,
                        observation_coverage, coverage_complete
                    )
                    SELECT
                        %s, fact.id, %s, %s, fact.subject_scope,
                        %s, %s, %s, %s
                    FROM fact_definitions AS fact
                    WHERE fact.id = %s
                    RETURNING id
                    """,
                    (
                        run_id,
                        task_ids[f"reader:{execution.reader_key}"],
                        observation.status,
                        Json({"ref": observation.subject_ref}),
                        Json(observation.value)
                        if observation.value is not None
                        else None,
                        Json(coverage),
                        observation.coverage_complete,
                        fact_id,
                    ),
                ).fetchone()
                if row is None:
                    raise RuntimeError("保存事实观察失败")
                for evidence in observation.evidence:
                    connection.execute(
                        """
                        INSERT INTO evidence (
                            fact_observation_id, input_id, source_type,
                            page_number, view_ref, original_text
                        )
                        VALUES (%s, %s, 'drawing', %s, %s, %s)
                        """,
                        (
                            row["id"],
                            input_id,
                            evidence.page,
                            evidence.region,
                            evidence.text,
                        ),
                    )


def persist_workflow_result(
    connection: Connection,
    workflow: WorkflowResult,
    *,
    revision_id: int,
) -> None:
    recommendation = workflow.recommendation
    with connection.transaction():
        row = connection.execute(
            """
            INSERT INTO route_recommendations (
                run_id, version_id, status, confirmed_route, local_issues
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                workflow.run_id,
                revision_id,
                recommendation.status,
                Json([item.model_dump(mode="json") for item in recommendation.route])
                if recommendation.route is not None
                else None,
                Json(
                    [
                        item.model_dump(mode="json")
                        for item in recommendation.local_issues
                    ]
                ),
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("保存路线推荐失败")
        recommendation_id = row["id"]
        for candidate in recommendation.route_candidates:
            connection.execute(
                """
                INSERT INTO route_candidates (
                    recommendation_id, candidate_key, operations
                )
                VALUES (%s, %s, %s)
                """,
                (
                    recommendation_id,
                    candidate.route_candidate_id,
                    Json(
                        [item.model_dump(mode="json") for item in candidate.operations]
                    ),
                ),
            )
        if recommendation.route is not None:
            for operation in recommendation.route:
                connection.execute(
                    """
                    INSERT INTO route_operations (
                        run_id, operation_key, flow_id, process, content,
                        targets, necessity_status, execution_state,
                        blocked_by, sequence, lineage
                    )
                    VALUES (
                        %s, %s, 'decision_tree', %s, '', '[]'::jsonb,
                        'confirmed_required', 'ready', '[]'::jsonb,
                        %s, %s
                    )
                    """,
                    (
                        workflow.run_id,
                        operation.operation_key,
                        operation.process_name,
                        operation.sequence,
                        Json(
                            {
                                "source_rules": operation.source_rule_keys,
                                "decisions": [
                                    item.model_dump(mode="json")
                                    for item in operation.decisions
                                ],
                            }
                        ),
                    ),
                )
        for issue in recommendation.local_issues:
            connection.execute(
                """
                INSERT INTO route_issues (
                    run_id, kind, code, message,
                    affected_operation_keys, missing_facts, candidate_options
                )
                VALUES (%s, %s, %s, %s, '[]'::jsonb, %s, '[]'::jsonb)
                """,
                (
                    workflow.run_id,
                    issue.kind,
                    issue.code,
                    issue.message,
                    Json(issue.missing_facts),
                ),
            )
        run_status = (
            "complete"
            if recommendation.status in {"complete", "complete_with_candidates"}
            else recommendation.status
        )
        connection.execute(
            """
            UPDATE runs
            SET status = %s, result = %s,
                elapsed_milliseconds = %s, finished_at = now()
            WHERE id = %s
            """,
            (
                run_status,
                Json(workflow.model_dump(mode="json")),
                round(workflow.elapsed_seconds * 1000),
                workflow.run_id,
            ),
        )


def persist_evaluation(
    connection: Connection,
    run_id: str,
    evaluation: GoldenEvaluation,
    *,
    evaluator_version: str,
) -> None:
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO route_evaluations (
                run_id, evaluator_version, status, report
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, evaluator_version) DO UPDATE SET
                status = EXCLUDED.status,
                report = EXCLUDED.report,
                created_at = now()
            """,
            (
                run_id,
                evaluator_version,
                evaluation.status,
                Json(evaluation.model_dump(mode="json")),
            ),
        )


def fail_run(
    connection: Connection,
    run_id: str,
    *,
    error_code: str,
    error_message: str,
) -> None:
    with connection.transaction():
        connection.execute(
            """
            UPDATE runs
            SET status = 'error', error_code = %s,
                error_message = %s, finished_at = now()
            WHERE id = %s
            """,
            (error_code, error_message, run_id),
        )
