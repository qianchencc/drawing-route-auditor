from __future__ import annotations

from pathlib import Path
from typing import Iterable

from psycopg2.extras import Json

from drawing_route_auditor.db.connection import Connection
from drawing_route_auditor.workflow.golden import GoldenEvaluation
from drawing_route_auditor.workflow.models import DrawingCase, ReaderExecution, WorkflowResult


def create_run(
    connection: Connection,
    *,
    case: DrawingCase,
    drawing_sha256: str,
    knowledge_snapshot: dict[str, object],
    model: str,
    prompt_version: str,
    task_keys: Iterable[tuple[str, str]],
) -> str:
    with connection.transaction():
        row = connection.execute(
            """
            INSERT INTO runs (
                status,
                knowledge_version,
                knowledge_snapshot,
                reader_model_version,
                reader_prompt_version,
                started_at
            )
            VALUES ('running', %s, %s, %s, %s, now())
            RETURNING id
            """,
            (
                str(knowledge_snapshot.get("knowledge_version", "prototype-v1")),
                Json(knowledge_snapshot),
                model,
                prompt_version,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create run")
        run_id = str(row["id"])
        connection.execute(
            """
            INSERT INTO run_inputs (
                run_id, input_kind, source_path, source_sha256, metadata
            )
            VALUES (%s, 'pdf', %s, %s, %s)
            """,
            (
                run_id,
                str(case.pdf_path),
                drawing_sha256,
                Json({"material_code": case.material_code}),
            ),
        )
        connection.execute(
            """
            INSERT INTO run_inputs (run_id, input_kind, source_path, metadata)
            VALUES (%s, 'context', 'plm:index.csv', %s)
            """,
            (run_id, Json(case.model_dump(mode="json", exclude={"pdf_path"}))),
        )
        for task_key, task_type in task_keys:
            connection.execute(
                """
                INSERT INTO tasks (run_id, task_key, task_type)
                VALUES (%s, %s, %s)
                """,
                (run_id, task_key, task_type),
            )
    return run_id


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
                raise RuntimeError(f"Task not queued: {task_key}")
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
    status: str,
    result: dict[str, object] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    task_status = "succeeded" if status == "succeeded" else "error"
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
                task_status,
                Json(result) if result is not None else None,
                error_code,
                error_message,
                run_id,
                task_key,
            ),
        ).fetchone()
        if task is None:
            raise RuntimeError(f"Task not running: {task_key}")
        connection.execute(
            """
            UPDATE task_attempts
            SET status = %s, finished_at = now(),
                error_code = %s, error_message = %s
            WHERE task_id = %s AND attempt_number = %s
            """,
            (
                task_status,
                error_code,
                error_message,
                task["id"],
                task["attempt_count"],
            ),
        )


def persist_workflow_result(
    connection: Connection,
    workflow: WorkflowResult,
) -> None:
    run_id = workflow.run_id
    input_rows = connection.execute(
        """
        SELECT id, input_kind
        FROM run_inputs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchall()
    input_ids = {row["input_kind"]: row["id"] for row in input_rows}

    with connection.transaction():
        task_rows = connection.execute(
            "SELECT id, task_key FROM tasks WHERE run_id = %s",
            (run_id,),
        ).fetchall()
        task_ids = {row["task_key"]: row["id"] for row in task_rows}

        for execution in workflow.reader_executions:
            result = execution.flow_result
            task_key = (
                "infer:transfer"
                if result.flow_id == "transfer"
                else f"reader:{result.flow_id}"
            )
            connection.execute(
                """
                INSERT INTO run_flow_results (
                    run_id, flow_id, status, duration_milliseconds,
                    prompt_tokens, completion_tokens, result
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    result.flow_id,
                    result.status,
                    round(execution.duration_seconds * 1000),
                    execution.prompt_tokens,
                    execution.completion_tokens,
                    Json(result.model_dump(mode="json")),
                ),
            )
            for observation in result.observations:
                value_type = "text"
                if isinstance(observation.value, bool):
                    value_type = "boolean"
                elif isinstance(observation.value, (int, float)):
                    value_type = "number"
                fact_row = connection.execute(
                    """
                    INSERT INTO fact_definitions (
                        fact_key, value_type, description
                    )
                    VALUES (%s, %s, %s)
                    ON CONFLICT (fact_key) DO UPDATE SET
                        description = fact_definitions.description
                    RETURNING id
                    """,
                    (
                        observation.fact_key,
                        value_type,
                        f"Observed by {result.flow_id} flow",
                    ),
                ).fetchone()
                if fact_row is None:
                    raise RuntimeError("Failed to store fact definition")
                coverage = {
                    "inspected_pages": sorted(
                        {item.page for item in observation.evidence}
                    ),
                    "inspected_regions": [
                        item.region for item in observation.evidence
                    ],
                    "is_complete_for_subject": observation.coverage_complete,
                }
                observation_row = connection.execute(
                    """
                    INSERT INTO fact_observations (
                        run_id, fact_definition_id, reader_task_id,
                        status, scope, subject_ref, value,
                        observation_coverage, coverage_complete
                    )
                    VALUES (
                        %s, %s, %s, %s, 'current_object', %s, %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        run_id,
                        fact_row["id"],
                        task_ids.get(task_key),
                        observation.status,
                        Json({"ref": observation.subject_ref}),
                        Json(observation.value)
                        if observation.value is not None
                        else None,
                        Json(coverage),
                        observation.coverage_complete,
                    ),
                ).fetchone()
                if observation_row is None:
                    raise RuntimeError("Failed to store fact observation")
                source_type = "plm" if result.flow_id == "transfer" else "drawing"
                input_id = input_ids.get(
                    "context" if source_type == "plm" else "pdf"
                )
                for item in observation.evidence:
                    connection.execute(
                        """
                        INSERT INTO evidence (
                            fact_observation_id, input_id, source_type,
                            page_number, view_ref, original_text
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            observation_row["id"],
                            input_id,
                            source_type,
                            item.page,
                            item.region,
                            item.text,
                        ),
                    )

        for operation in workflow.route.operations:
            connection.execute(
                """
                INSERT INTO route_operations (
                    run_id, operation_key, flow_id, process, content,
                    targets, necessity_status, execution_state,
                    blocked_by, sequence, lineage
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    operation.operation_key,
                    operation.flow_id,
                    operation.process,
                    operation.content,
                    Json(operation.targets),
                    operation.necessity_status,
                    operation.execution_state,
                    Json(operation.blocked_by),
                    operation.sequence,
                    Json(operation.lineage),
                ),
            )
        for constraint in workflow.route.constraints:
            connection.execute(
                """
                INSERT INTO route_constraints (
                    run_id, before_operation_key, after_operation_key, reason
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, before_operation_key, after_operation_key)
                    DO NOTHING
                """,
                (
                    run_id,
                    constraint.before_operation,
                    constraint.after_operation,
                    constraint.reason,
                ),
            )
        for issue in workflow.route.issues:
            connection.execute(
                """
                INSERT INTO route_issues (
                    run_id, kind, code, message,
                    affected_operation_keys, missing_facts, candidate_options
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    issue.kind,
                    issue.code,
                    issue.message,
                    Json(issue.affected_operation_keys),
                    Json(issue.missing_facts),
                    Json(issue.candidate_options),
                ),
            )
        connection.execute(
            """
            UPDATE runs
            SET status = %s, result = %s,
                elapsed_milliseconds = %s, finished_at = now()
            WHERE id = %s
            """,
            (
                workflow.route.status,
                Json(workflow.model_dump(mode="json")),
                round(workflow.elapsed_seconds * 1000),
                run_id,
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
            SET status = 'error', error_code = %s, error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
            (error_code, error_message, run_id),
        )
