from pathlib import Path

import pytest
from drawing_route_auditor.db.connection import Connection

from drawing_route_auditor.decision_tree.importer import import_decision_tree
from drawing_route_auditor.decision_tree.repository import evaluate_tree


SOURCE_PATH = Path("docs/1.json")
TREE_KEY = "integration-evaluator"


@pytest.fixture
def imported_tree(db_connection: Connection) -> Connection:
    import_decision_tree(
        db_connection,
        SOURCE_PATH,
        tree_key=TREE_KEY,
        name="Integration evaluator tree",
        version=1,
    )
    return db_connection


@pytest.mark.integration
def test_evaluator_resolves_clear_rules(imported_tree: Connection) -> None:
    results = evaluate_tree(
        imported_tree,
        TREE_KEY,
        1,
        {
            "drawing_number_numeric_prefix": {"status": "hit", "value": "50"},
            "object_has_bom": {"status": "hit", "value": True},
            "object_kind": {"status": "hit", "value": "component"},
            "title_contains_welding": {"status": "hit", "value": True},
            "component_kind": {"status": "hit", "value": "welded"},
            "parent_part_type": {"status": "hit", "value": "final_assembly"},
        },
    )
    resolved_keys = {
        row["rule_key"]
        for row in results
        if row["result_status"] == "resolved"
    }

    assert "object_component_by_number" in resolved_keys
    assert "object_component_by_bom" in resolved_keys
    assert "welded_component_by_title" in resolved_keys
    assert "welded_component_first_operation" in resolved_keys
    assert "transfer_assembly_to_assembly" not in resolved_keys
    assert "transfer_welded_to_assembly" in resolved_keys
    assert "welded_component_by_requirement_candidate" not in {
        row["rule_key"] for row in results
    }


@pytest.mark.integration
def test_evaluator_returns_multiple_explained_candidates(
    imported_tree: Connection,
) -> None:
    results = evaluate_tree(
        imported_tree,
        TREE_KEY,
        1,
        {
            "technical_requirement_mentions_welding": {
                "status": "hit",
                "value": True,
            },
            "technical_requirement_mentions_assembly": {
                "status": "hit",
                "value": True,
            },
        },
    )
    candidates = [
        row
        for row in results
        if row["result_status"] == "candidate"
        and row["branch_key"] == "2.3"
    ]

    assert len(candidates) == 2
    assert len(results) == 2
    assert {row["outcome_key"] for row in candidates} == {"component_kind"}
    assert {row["outcome_value"] for row in candidates} == {"welded", "assembly"}
    assert all(row["reason"] for row in candidates)


@pytest.mark.integration
def test_evaluator_returns_every_matching_route_family_candidate(
    imported_tree: Connection,
) -> None:
    results = evaluate_tree(
        imported_tree,
        TREE_KEY,
        1,
        {
            "object_kind": {"status": "hit", "value": "part"},
            "is_plate_part": {"status": "hit", "value": True},
            "has_bend_feature": {"status": "hit", "value": True},
            "has_welding_feature": {"status": "hit", "value": True},
            "requires_precision_machining": {"status": "hit", "value": True},
            "raw_form": {"status": "hit", "value": "sheet"},
        },
    )

    assert len(results) == 3
    assert all(row["result_status"] == "candidate" for row in results)
    assert {row["outcome_key"] for row in results} == {
        "bent_sheet_part",
        "machined_plate_part",
        "welded_sheet_part",
    }
    assert all(row["reason"] for row in results)


@pytest.mark.integration
def test_evaluator_marks_unavailable_weak_fact_as_error(
    imported_tree: Connection,
) -> None:
    results = evaluate_tree(
        imported_tree,
        TREE_KEY,
        1,
        {
            "technical_requirement_mentions_welding": {
                "status": "unable_to_judge"
            },
        },
    )

    assert len(results) == 1
    assert results[0]["rule_key"] == "welded_component_by_requirement_candidate"
    assert results[0]["result_status"] == "error"
    assert results[0]["missing_facts"] == [
        "technical_requirement_mentions_welding:unable_to_judge"
    ]


@pytest.mark.integration
def test_evaluator_marks_unavailable_fact_at_its_rule(
    imported_tree: Connection,
) -> None:
    results = evaluate_tree(
        imported_tree,
        TREE_KEY,
        1,
        {
            "title_contains_welding": {"status": "unable_to_judge"},
        },
    )
    title_results = [
        row for row in results if row["branch_key"] == "2.1"
    ]

    assert title_results
    assert results == title_results
    assert all(row["result_status"] == "error" for row in title_results)
    assert all(
        "title_contains_welding:unable_to_judge" in row["missing_facts"]
        for row in title_results
    )
