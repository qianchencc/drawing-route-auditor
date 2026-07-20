import json
from pathlib import Path

import pytest
from drawing_route_auditor.db.connection import Connection

from drawing_route_auditor.decision_tree.importer import import_decision_tree
from drawing_route_auditor.decision_tree.repository import tree_details, validate_tree


SOURCE_PATH = Path("docs/decision_tree_v3.json")


def _definition(tmp_path: Path, tree_key: str) -> Path:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    payload["tree_key"] = tree_key
    payload["name"] = f"Integration tree {tree_key}"
    payload["base_source_path"] = str(Path("docs/1.json").resolve())
    path = tmp_path / f"{tree_key}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


@pytest.mark.integration
def test_import_is_lossless_tree_driven_and_idempotent(
    db_connection: Connection,
    tmp_path: Path,
) -> None:
    tree_key = "integration-tree-driven-import"
    source = _definition(tmp_path, tree_key)

    first = import_decision_tree(db_connection, source)
    second = import_decision_tree(db_connection, source)

    assert first.existing is False
    assert second.existing is True
    assert second.version_id == first.version_id
    assert first.source_row_count == 21
    assert first.reader_count == 4
    assert first.fact_count == 30
    assert first.node_count == 4
    assert first.branch_count == 16
    assert first.rule_count == 31

    formatting = db_connection.execute(
        """
        SELECT formatting
        FROM decision_source_rows
        WHERE version_id = %s AND row_number = 7
        """,
        (first.version_id,),
    ).fetchone()["formatting"]
    assert any(cell.get("fill") == "#FFFF00" for cell in formatting["cells"])

    ownership = db_connection.execute(
        """
        SELECT
            count(*) FILTER (
                WHERE fact.source_kind = 'observed_drawing'
                  AND fact.reader_id IS NOT NULL
            ) AS observed_owned,
            count(*) FILTER (
                WHERE fact.source_kind <> 'observed_drawing'
                  AND fact.reader_id IS NULL
            ) AS non_observed_unowned,
            count(*) AS total
        FROM fact_definitions AS fact
        WHERE fact.version_id = %s
        """,
        (first.version_id,),
    ).fetchone()
    assert ownership == {
        "observed_owned": 24,
        "non_observed_unowned": 6,
        "total": 30,
    }


@pytest.mark.integration
def test_validation_and_details_come_from_imported_definition(
    db_connection: Connection,
    tmp_path: Path,
) -> None:
    tree_key = "integration-tree-driven-validation"
    source = _definition(tmp_path, tree_key)
    import_decision_tree(db_connection, source)

    report = validate_tree(db_connection, tree_key, 3)
    details = tree_details(db_connection, tree_key, 3)

    assert report.error_count == 0
    assert report.counts == {
        "source_rows": 21,
        "nodes": 4,
        "branches": 16,
        "edges": 5,
        "rules": 31,
        "clauses": 49,
    }
    assert len(details["readers"]) == 4
    assert len(details["facts"]) == 30
    assert len(details["rules"]) == 31
    assert {
        item["reader_key"]
        for item in details["facts"]
        if item["source_kind"] == "observed_drawing"
    } == {
        "document_structure_reader",
        "geometry_dimension_reader",
        "symbol_relation_reader",
        "requirement_annotation_reader",
    }
