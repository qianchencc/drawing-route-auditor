import pytest
from psycopg2.errors import CheckViolation
from psycopg2.extras import Json
from psycopg2.extensions import connection as Connection


@pytest.mark.integration
def test_fact_observation_preserves_subject_coverage_and_evidence(
    db_connection: Connection,
) -> None:
    run_id = db_connection.execute(
        "INSERT INTO runs DEFAULT VALUES RETURNING id"
    ).fetchone()["id"]
    input_id = db_connection.execute(
        """
        INSERT INTO run_inputs (run_id, input_kind, source_path)
        VALUES (%s, 'pdf', 'drawing.pdf')
        RETURNING id
        """,
        (run_id,),
    ).fetchone()["id"]
    task_id = db_connection.execute(
        """
        INSERT INTO tasks (run_id, task_key, task_type)
        VALUES (%s, 'read-title', 'reader')
        RETURNING id
        """,
        (run_id,),
    ).fetchone()["id"]
    fact_definition_id = db_connection.execute(
        """
        INSERT INTO fact_definitions (fact_key, value_type, description)
        VALUES ('test_title_fact', 'boolean', 'Integration-test fact')
        RETURNING id
        """
    ).fetchone()["id"]
    fact_observation_id = db_connection.execute(
        """
        INSERT INTO fact_observations (
            run_id,
            fact_definition_id,
            reader_task_id,
            status,
            scope,
            subject_ref,
            confidence,
            observation_coverage,
            coverage_complete
        )
        VALUES (
            %s, %s, %s, 'unable_to_judge', 'bom_item', %s, 0.35, %s, false
        )
        RETURNING id
        """,
        (
            run_id,
            fact_definition_id,
            task_id,
            Json({"bom_no": "10", "bom_name": "筒体"}),
            Json({"inspected_pages": [1], "is_complete_for_subject": False}),
        ),
    ).fetchone()["id"]
    db_connection.execute(
        """
        INSERT INTO evidence (
            fact_observation_id,
            input_id,
            source_type,
            page_number,
            view_ref,
            region,
            original_text
        )
        VALUES (%s, %s, 'drawing', 1, 'title_block', %s, '无法辨认')
        """,
        (fact_observation_id, input_id, Json({"bbox": [10, 20, 30, 40]})),
    )

    stored = db_connection.execute(
        """
        SELECT
            hit.status,
            hit.scope,
            hit.subject_ref,
            hit.observation_coverage,
            hit.confidence,
            evidence.source_type,
            evidence.page_number,
            evidence.view_ref,
            evidence.region,
            evidence.original_text
        FROM fact_observations AS hit
        JOIN evidence ON evidence.fact_observation_id = hit.id
        WHERE hit.id = %s
        """,
        (fact_observation_id,),
    ).fetchone()

    assert stored["status"] == "unable_to_judge"
    assert stored["scope"] == "bom_item"
    assert stored["subject_ref"] == {"bom_no": "10", "bom_name": "筒体"}
    assert stored["observation_coverage"] == {
        "inspected_pages": [1],
        "is_complete_for_subject": False,
    }
    assert float(stored["confidence"]) == 0.35
    assert stored["source_type"] == "drawing"
    assert stored["page_number"] == 1
    assert stored["view_ref"] == "title_block"
    assert stored["region"] == {"bbox": [10, 20, 30, 40]}
    assert stored["original_text"] == "无法辨认"

    db_connection.execute("SAVEPOINT coverage_guard")
    with pytest.raises(CheckViolation):
        db_connection.execute(
            """
            INSERT INTO fact_observations (
                run_id, fact_definition_id, reader_task_id,
                status, scope, subject_ref, coverage_complete
            )
            VALUES (%s, %s, %s, 'not_hit', 'bom_item', %s, false)
            """,
            (
                run_id,
                fact_definition_id,
                task_id,
                Json({"bom_no": "11"}),
            ),
        )
    db_connection.execute("ROLLBACK TO SAVEPOINT coverage_guard")
