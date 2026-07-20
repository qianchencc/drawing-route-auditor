from pathlib import Path

import pytest
from drawing_route_auditor.db.connection import Connection

from drawing_route_auditor.decision_tree.catalog import RULES
from drawing_route_auditor.decision_tree.importer import import_decision_tree
from drawing_route_auditor.decision_tree.repository import tree_details, validate_tree


SOURCE_PATH = Path("docs/1.json")
TREE_KEY = "integration-customer-base"


@pytest.mark.integration
def test_import_is_lossless_and_idempotent(db_connection: Connection) -> None:
    first = import_decision_tree(
        db_connection,
        SOURCE_PATH,
        tree_key=TREE_KEY,
        name="Integration customer base tree",
        version=1,
    )
    second = import_decision_tree(
        db_connection,
        SOURCE_PATH,
        tree_key=TREE_KEY,
        name="Integration customer base tree",
        version=1,
    )

    assert first.existing is False
    assert second.existing is True
    assert second.version_id == first.version_id
    assert first.source_row_count == 21
    assert first.node_count == 10
    assert first.branch_count == 18
    assert first.rule_count == len(RULES)

    formatting = db_connection.execute(
        """
        SELECT formatting
        FROM decision_source_rows
        WHERE version_id = %s AND row_number = 7
        """,
        (first.version_id,),
    ).fetchone()["formatting"]
    assert any(cell.get("fill") == "#FFFF00" for cell in formatting["cells"])


@pytest.mark.integration
def test_validation_localizes_incomplete_and_ambiguous_sections(
    db_connection: Connection,
) -> None:
    import_decision_tree(
        db_connection,
        SOURCE_PATH,
        tree_key=TREE_KEY,
        name="Integration customer base tree",
        version=1,
    )

    report = validate_tree(db_connection, TREE_KEY, 1)
    details = tree_details(db_connection, TREE_KEY, 1)

    assert report.counts == {
        "source_rows": 21,
        "nodes": 10,
        "branches": 18,
        "edges": 10,
        "rules": len(RULES),
        "clauses": sum(len(rule.clauses) for rule in RULES),
    }
    assert {
        issue.location
        for issue in report.issues
        if issue.code == "INCOMPLETE_NODE"
    } == {"node:8", "node:9", "node:10"}
    assert {
        issue.location
        for issue in report.issues
        if issue.code == "AMBIGUOUS_EDGE"
    } == {"node:2", "node:3", "node:4", "node:5"}
    assert {
        branch["branch_key"]
        for branch in details["branches"]
        if branch["maintenance_status"] == "needs_review"
    } == {"3.2", "5.6", "6.1", "7.1"}
