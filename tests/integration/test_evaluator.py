import json
from pathlib import Path

import pytest
from drawing_route_auditor.db.connection import Connection

from drawing_route_auditor.decision_tree.editor import apply_tree_patch
from drawing_route_auditor.decision_tree.importer import initialize_decision_tree
from drawing_route_auditor.decision_tree.repository import evaluate_tree
from drawing_route_auditor.decision_tree.runtime import (
    evaluate_closure,
    evaluate_scenarios,
    load_runtime_tree,
)
from drawing_route_auditor.workflow.assembler import assemble_recommendation


SOURCE_PATH = Path("docs/decision_tree.json")
TREE_KEY = "integration-tree-evaluator"


@pytest.fixture
def imported_tree(
    db_connection: Connection,
    tmp_path: Path,
) -> tuple[Connection, object]:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    payload["tree_key"] = TREE_KEY
    path = tmp_path / "tree.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    initialize_decision_tree(db_connection, path)
    runtime = load_runtime_tree(db_connection, TREE_KEY)
    return db_connection, runtime


@pytest.mark.integration
def test_evaluator_returns_decision_metadata(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, _ = imported_tree
    results = evaluate_tree(
        connection,
        TREE_KEY,
        {"route_family": {"status": "hit", "value": "rolled_sheet_part"}},
    )

    blanking = [item for item in results if item["decision_key"] == "blanking_method"]
    assert len(blanking) == 1
    assert blanking[0]["option_label"] == "激光下料"
    assert blanking[0]["result_status"] == "resolved"
    assert all(item["decisive_facts"] == ["route_family"] for item in blanking)
    assert {
        item["rule_key"] for item in results if item["result_status"] == "resolved"
    } == {"rolling_operation", "rolled_sheet_laser_blanking"}


@pytest.mark.integration
def test_closure_and_route_expansion_generate_complete_candidates(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    initial_facts = {
        "drawing_number_numeric_prefix": {"status": "hit", "value": "80"},
        "object_has_bom": {"status": "not_hit", "value": False},
        "is_plate_part": {"status": "hit", "value": True},
        "continuous_revolved_surface": {"status": "not_hit", "value": False},
        "has_bend_feature": {"status": "hit", "value": True},
        "outer_surface_polish_required": {"status": "not_hit", "value": False},
        "formal_cleaning_required": {"status": "not_hit", "value": False},
    }

    scenarios = evaluate_scenarios(connection, runtime, initial_facts)
    recommendation = assemble_recommendation(
        scenarios, tree_revision=runtime.revision
    )

    assert len(scenarios) == 1
    assert scenarios[0].facts["object_kind"] == {
        "status": "hit",
        "value": "part",
    }
    assert scenarios[0].facts["route_family"] == {
        "status": "hit",
        "value": "bent_sheet_part",
    }
    assert recommendation.status == "complete_with_candidates"
    assert {
        tuple(item.process_name for item in candidate.operations)
        for candidate in recommendation.route_candidates
    } == {
        ("激光下料", "折弯", "转装配"),
        ("激光下料", "折弯", "转部装"),
        ("激光下料", "折弯", "转焊接"),
        ("剪板下料", "折弯", "转装配"),
        ("剪板下料", "折弯", "转部装"),
        ("剪板下料", "折弯", "转焊接"),
    }
    assert all(
        operation.decisions
        for candidate in recommendation.route_candidates
        for operation in candidate.operations
    )


@pytest.mark.integration
def test_known_parent_suppresses_transfer_candidates(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "drawing_number_numeric_prefix": {"status": "hit", "value": "80"},
            "object_has_bom": {"status": "not_hit", "value": False},
            "is_plate_part": {"status": "hit", "value": True},
            "continuous_revolved_surface": {"status": "not_hit", "value": False},
            "has_bend_feature": {"status": "hit", "value": True},
            "outer_surface_polish_required": {"status": "not_hit", "value": False},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
            "parent_part_type": {"status": "hit", "value": "sub_assembly"},
        },
    )

    recommendation = assemble_recommendation(
        scenarios, tree_revision=runtime.revision
    )

    assert {
        tuple(item.process_name for item in candidate.operations)
        for candidate in recommendation.route_candidates
    } == {
        ("激光下料", "折弯", "转部装"),
        ("剪板下料", "折弯", "转部装"),
    }


@pytest.mark.integration
def test_rolled_sheet_without_validated_module_stays_partial(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "drawing_number_numeric_prefix": {"status": "hit", "value": "80"},
            "object_has_bom": {"status": "not_hit", "value": False},
            "is_plate_part": {"status": "hit", "value": True},
            "continuous_revolved_surface": {"status": "hit", "value": True},
            "has_bend_feature": {"status": "not_hit", "value": False},
            "outer_surface_polish_required": {"status": "not_hit", "value": False},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
            "parent_part_type": {"status": "hit", "value": "welded"},
        },
    )

    recommendation = assemble_recommendation(
        scenarios, tree_revision=runtime.revision
    )

    assert recommendation.status == "partial"
    assert recommendation.route is not None
    assert [item.process_name for item in recommendation.route] == [
        "激光下料",
        "卷圆",
        "转焊接",
    ]
    assert "rolled_sheet_process_module_unavailable" in {
        match.rule_key
        for scenario in scenarios
        for match in scenario.matches
        if match.result_status == "error"
    }


@pytest.mark.integration
@pytest.mark.parametrize("part_name", ["斗体", "锥体"])
def test_validated_hopper_family_expands_complete_rolled_module(
    imported_tree: tuple[Connection, object],
    part_name: str,
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "drawing_number_numeric_prefix": {"status": "hit", "value": "80"},
            "object_has_bom": {"status": "not_hit", "value": False},
            "part_name": {"status": "hit", "value": part_name},
            "is_plate_part": {"status": "hit", "value": True},
            "continuous_revolved_surface": {"status": "hit", "value": True},
            "has_bend_feature": {"status": "not_hit", "value": False},
            "outer_surface_polish_required": {"status": "hit", "value": True},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
            "parent_part_type": {"status": "hit", "value": "welded"},
        },
    )

    recommendation = assemble_recommendation(
        scenarios, tree_revision=runtime.revision
    )

    assert recommendation.status == "complete"
    assert recommendation.route is not None
    assert [item.process_name for item in recommendation.route] == [
        "激光下料",
        "焊接(校正)",
        "卷圆",
        "焊接(校正)",
        "卷圆",
        "转焊接",
    ]


@pytest.mark.integration
def test_missing_base_route_is_not_reported_as_complete(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "drawing_number_numeric_prefix": {"status": "hit", "value": "80"},
            "object_has_bom": {"status": "not_hit", "value": False},
            "is_plate_part": {"status": "not_hit", "value": False},
            "continuous_revolved_surface": {"status": "hit", "value": True},
            "has_bend_feature": {"status": "not_hit", "value": False},
            "raw_form": {"status": "hit", "value": "other"},
            "outer_surface_polish_required": {"status": "hit", "value": True},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
        },
    )

    recommendation = assemble_recommendation(
        scenarios, tree_revision=runtime.revision
    )
    assert recommendation.status == "error"
    assert recommendation.route is None
    assert any(
        missing.startswith("route_family:")
        for issue in recommendation.local_issues
        for missing in issue.missing_facts
    )


@pytest.mark.integration
def test_component_without_route_template_is_not_reported_as_complete(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "drawing_number_numeric_prefix": {"status": "hit", "value": "50"},
            "object_has_bom": {"status": "hit", "value": True},
            "title_contains_welding": {"status": "hit", "value": True},
            "outer_surface_polish_required": {"status": "not_hit", "value": False},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
        },
    )

    recommendation = assemble_recommendation(
        scenarios, tree_revision=runtime.revision
    )
    assert recommendation.status == "error"
    assert any(
        "尚未定义部件的完整基础工艺路线" in issue.message
        for issue in recommendation.local_issues
    )


@pytest.mark.integration
def test_higher_priority_structural_fact_resolves_conflicting_prefix(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree

    facts, _, issues = evaluate_closure(
        connection,
        runtime,
        {
            "drawing_number_numeric_prefix": {"status": "hit", "value": "50"},
            "object_has_bom": {"status": "not_hit", "value": False},
        },
    )

    assert facts["object_kind"] == {"status": "hit", "value": "part"}
    assert all(issue.code != "DERIVED_FACT_CONFLICT" for issue in issues)


@pytest.mark.integration
def test_candidate_facts_branch_without_stopping_evaluation(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
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

    assert len(scenarios) == 2
    assert {
        option.outcome_value
        for scenario in scenarios
        for option in scenario.selected_fact_options
    } == {"welded", "assembly"}


@pytest.mark.integration
def test_unavailable_fact_is_localized_to_referencing_rules(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, _ = imported_tree
    results = evaluate_tree(
        connection,
        TREE_KEY,
        {"drawing_number_numeric_prefix": {"status": "unable_to_judge"}},
    )

    assert results
    assert all(item["branch_key"] == "1.1" for item in results)
    assert all(item["result_status"] == "error" for item in results)
    assert all(
        "drawing_number_numeric_prefix:unable_to_judge" in item["missing_facts"]
        for item in results
    )


@pytest.mark.integration
def test_closure_stays_on_runtime_revision_after_tree_update(
    imported_tree: tuple[Connection, object],
    tmp_path: Path,
) -> None:
    connection, runtime = imported_tree
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    rule = next(
        item for item in payload["rules"] if item["rule_key"] == "rolling_operation"
    )
    rule["option_label"] = "更新后的卷圆"
    patch = tmp_path / "revision-snapshot-patch.json"
    patch.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tree_key": TREE_KEY,
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
    apply_tree_patch(connection, patch)

    _, snapshot_matches, _ = evaluate_closure(
        connection,
        runtime,
        {"route_family": {"status": "hit", "value": "rolled_sheet_part"}},
    )
    active_matches = evaluate_tree(
        connection,
        TREE_KEY,
        {"route_family": {"status": "hit", "value": "rolled_sheet_part"}},
    )

    snapshot = next(item for item in snapshot_matches if item.rule_key == "rolling_operation")
    active = next(item for item in active_matches if item["rule_key"] == "rolling_operation")
    assert snapshot.option_label == "卷圆"
    assert active["option_label"] == "更新后的卷圆"
