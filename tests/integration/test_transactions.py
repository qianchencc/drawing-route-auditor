from uuid import uuid4

import pytest

from drawing_route_auditor.db.connection import connect
from drawing_route_auditor.workflow.repository import finish_task, start_tasks


@pytest.mark.integration
def test_task_transition_commits_after_prior_read() -> None:
    run_id = str(uuid4())
    with connect() as connection:
        with connection.transaction():
            connection.execute(
                "INSERT INTO runs (id, status) VALUES (%s, 'running')",
                (run_id,),
            )
            connection.execute(
                """
                INSERT INTO tasks (run_id, task_key, task_type)
                VALUES (%s, 'evaluate', 'evaluate')
                """,
                (run_id,),
            )

    try:
        with connect() as connection:
            connection.execute(
                "SELECT status FROM runs WHERE id = %s",
                (run_id,),
            )
            start_tasks(connection, run_id, ["evaluate"])

        with connect() as connection:
            finish_task(
                connection,
                run_id,
                "evaluate",
                succeeded=True,
                result={"facts": {}},
            )
            status = connection.execute(
                """
                SELECT status
                FROM tasks
                WHERE run_id = %s AND task_key = 'evaluate'
                """,
                (run_id,),
            ).fetchone()
        assert status == {"status": "succeeded"}
    finally:
        with connect() as connection:
            with connection.transaction():
                connection.execute("DELETE FROM runs WHERE id = %s", (run_id,))
