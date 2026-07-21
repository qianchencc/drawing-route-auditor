import json
from pathlib import Path

import pytest
from drawing_route_auditor.db.connection import Connection

from drawing_route_auditor.decision_tree.editor import apply_tree_patch
from drawing_route_auditor.decision_tree.importer import initialize_decision_tree
from drawing_route_auditor.decision_tree.repository import tree_details, validate_tree


SOURCE_PATH = Path("docs/decision_tree.json")


def _definition(tmp_path: Path, tree_key: str) -> Path:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    payload["tree_key"] = tree_key
    payload["name"] = f"Integration tree {tree_key}"
    path = tmp_path / f"{tree_key}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


@pytest.mark.integration
def test_init_is_idempotent_and_patch_updates_current_tree(
    db_connection: Connection,
    tmp_path: Path,
) -> None:
    tree_key = "integration-tree-load"
    source = _definition(tmp_path, tree_key)

    first = initialize_decision_tree(db_connection, source)
    second = initialize_decision_tree(db_connection, source)

    assert first.changed is True
    assert second.changed is False

    changed_definition = json.loads(source.read_text(encoding="utf-8"))
    changed_definition["description"] = "Forbidden full replacement"
    source.write_text(
        json.dumps(changed_definition, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="请使用增量补丁更新"):
        initialize_decision_tree(db_connection, source)
    assert second.revision_id == first.revision_id
    assert first.reader_count == 4
    assert first.fact_count == 31
    assert first.node_count == 4
    assert first.branch_count == 16
    assert first.rule_count == 38

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
        (first.revision_id,),
    ).fetchone()
    assert ownership == {
        "observed_owned": 24,
        "non_observed_unowned": 7,
        "total": 31,
    }

    payload = json.loads(source.read_text(encoding="utf-8"))
    rule = next(
        item for item in payload["rules"] if item["rule_key"] == "rolling_operation"
    )
    rule["description"] = "Incrementally updated rolling rule"
    patch = tmp_path / "patch.json"
    patch.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tree_key": tree_key,
                "operations": [
                    {
                        "op": "upsert",
                        "collection": "rules",
                        "key": "rolling_operation",
                        "value": rule,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    updated = apply_tree_patch(db_connection, patch)

    assert updated.changed is True
    assert updated.revision_id != first.revision_id
    updated_rule = next(
        item
        for item in tree_details(db_connection, tree_key)["rules"]
        if item["rule_key"] == "rolling_operation"
    )
    assert updated_rule["description"] == "Incrementally updated rolling rule"
    active_count = db_connection.execute(
        """
        SELECT count(*) AS count
        FROM decision_tree_versions AS revision
        JOIN decision_trees AS tree ON tree.id = revision.tree_id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()["count"]
    assert active_count == 1


@pytest.mark.integration
def test_invalid_patch_leaves_current_tree_unchanged(
    db_connection: Connection,
    tmp_path: Path,
) -> None:
    tree_key = "integration-tree-invalid-patch"
    source = _definition(tmp_path, tree_key)
    initialized = initialize_decision_tree(db_connection, source)
    patch = tmp_path / "invalid-patch.json"
    patch.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tree_key": tree_key,
                "operations": [
                    {
                        "op": "remove",
                        "collection": "facts",
                        "key": "route_family",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="未知事实"):
        apply_tree_patch(db_connection, patch)

    assert tree_details(db_connection, tree_key)["revision_id"] == (
        initialized.revision_id
    )


@pytest.mark.integration
def test_patch_rejects_invalid_runtime_reader_contract(
    db_connection: Connection,
    tmp_path: Path,
) -> None:
    tree_key = "integration-tree-reader-contract"
    source = _definition(tmp_path, tree_key)
    initialized = initialize_decision_tree(db_connection, source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    extra_reader = dict(payload["readers"][0])
    extra_reader["reader_key"] = "unexpected_fifth_reader"
    extra_reader["sequence"] = 5
    patch = tmp_path / "fifth-reader-patch.json"
    patch.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tree_key": tree_key,
                "operations": [
                    {
                        "op": "upsert",
                        "collection": "readers",
                        "key": "unexpected_fifth_reader",
                        "value": extra_reader,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="四个读取器"):
        apply_tree_patch(db_connection, patch)

    details = tree_details(db_connection, tree_key)
    assert details["revision_id"] == initialized.revision_id
    assert len(details["readers"]) == 4


@pytest.mark.integration
def test_validation_and_details_use_current_tree(
    db_connection: Connection,
    tmp_path: Path,
) -> None:
    tree_key = "integration-tree-validation"
    source = _definition(tmp_path, tree_key)
    initialize_decision_tree(db_connection, source)

    report = validate_tree(db_connection, tree_key)
    details = tree_details(db_connection, tree_key)

    assert report.error_count == 0
    assert report.counts == {
        "nodes": 4,
        "branches": 16,
        "edges": 5,
        "rules": 38,
        "clauses": 65,
    }
    assert len(details["readers"]) == 4
    assert len(details["facts"]) == 31
    assert len(details["rules"]) == 38
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
