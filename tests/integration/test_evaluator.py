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
        "object_has_bom": {"status": "not_hit", "value": False},
        "raw_form": {"status": "hit", "value": "plate"},
        "continuous_revolved_surface": {"status": "not_hit", "value": False},
        "continuous_rolled_shell_surface_present": {
            "status": "not_hit",
            "value": False,
        },
        "has_bend_feature": {"status": "hit", "value": True},
        "outer_surface_polish_required": {"status": "not_hit", "value": False},
        "formal_cleaning_required": {"status": "not_hit", "value": False},
    }

    scenarios = evaluate_scenarios(connection, runtime, initial_facts)
    recommendation = assemble_recommendation(scenarios, tree_revision=runtime.revision)

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
        ("激光下料", "折弯"),
        ("剪板下料", "折弯"),
    }
    assert all(
        operation.decisions
        for candidate in recommendation.route_candidates
        for operation in candidate.operations
    )


@pytest.mark.integration
def test_identity_metadata_does_not_override_bent_geometry(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    routes = []
    for drawing_number, part_name in [
        ("ARBITRARY-001", "锥体"),
        ("SAME-FEATURES-999", "任意名称"),
    ]:
        scenarios = evaluate_scenarios(
            connection,
            runtime,
            {
                "object_has_bom": {"status": "not_hit", "value": False},
                "raw_form": {"status": "hit", "value": "plate"},
                "continuous_revolved_surface": {
                    "status": "not_hit",
                    "value": False,
                },
                "continuous_rolled_shell_surface_present": {
                    "status": "not_hit",
                    "value": False,
                },
                "has_bend_feature": {"status": "hit", "value": True},
                "has_hole_feature": {"status": "hit", "value": True},
                "outer_surface_polish_required": {
                    "status": "not_hit",
                    "value": False,
                },
                "formal_cleaning_required": {
                    "status": "not_hit",
                    "value": False,
                },
                "drawing_number": {"status": "hit", "value": drawing_number},
                "part_name": {"status": "hit", "value": part_name},
            },
        )
        recommendation = assemble_recommendation(
            scenarios, tree_revision=runtime.revision
        )
        assert recommendation.status == "complete"
        assert recommendation.route is not None
        routes.append(tuple(item.process_name for item in recommendation.route))

    assert routes == [("激光下料", "折弯"), ("激光下料", "折弯")]


@pytest.mark.integration
def test_continuous_rolled_shell_route_is_identity_invariant_and_feature_sensitive(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    shell_facts = {
        "object_has_bom": {"status": "not_hit", "value": False},
        "raw_form": {"status": "hit", "value": "plate"},
        "continuous_revolved_surface": {"status": "not_hit", "value": False},
        "continuous_rolled_shell_surface_present": {
            "status": "hit",
            "value": True,
        },
        "has_bend_feature": {"status": "not_hit", "value": False},
        "has_hole_feature": {"status": "not_hit", "value": False},
        "has_slot_feature": {"status": "not_hit", "value": False},
        "precision_tolerance_present": {"status": "not_hit", "value": False},
        "outer_surface_polish_required": {"status": "not_hit", "value": False},
        "formal_cleaning_required": {"status": "not_hit", "value": False},
    }
    for drawing_number, part_name in [
        ("IDENTITY-D", "弯曲管壳"),
        ("UNRELATED-SHELL", "任意连续板壳"),
    ]:
        scenarios = evaluate_scenarios(
            connection,
            runtime,
            {
                **shell_facts,
                "drawing_number": {"status": "hit", "value": drawing_number},
                "part_name": {"status": "hit", "value": part_name},
            },
        )
        recommendation = assemble_recommendation(
            scenarios,
            tree_revision=runtime.revision,
        )
        assert recommendation.status == "complete"
        assert recommendation.route is not None
        assert tuple(item.process_name for item in recommendation.route) == (
            "激光下料",
            "卷圆",
        )

    flat_scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            **shell_facts,
            "continuous_rolled_shell_surface_present": {
                "status": "not_hit",
                "value": False,
            },
        },
    )
    flat_recommendation = assemble_recommendation(
        flat_scenarios,
        tree_revision=runtime.revision,
    )
    routes = (
        [flat_recommendation.route]
        if flat_recommendation.route is not None
        else [
            candidate.operations for candidate in flat_recommendation.route_candidates
        ]
    )
    assert all(
        operation.process_name != "卷圆" for route in routes for operation in route
    )


@pytest.mark.integration
def test_feature_routes_are_identity_invariant_and_feature_sensitive(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree

    def route_for(facts: dict[str, object]) -> tuple[str, ...]:
        scenarios = evaluate_scenarios(connection, runtime, facts)
        recommendation = assemble_recommendation(
            scenarios, tree_revision=runtime.revision
        )
        assert recommendation.status == "complete"
        assert recommendation.route is not None
        return tuple(item.process_name for item in recommendation.route)

    shaft_features = {
        "object_has_bom": {"status": "not_hit", "value": False},
        "raw_form": {"status": "hit", "value": "bar"},
        "single_axis_external_cylindrical_profile": {
            "status": "hit",
            "value": True,
        },
        "large_axisymmetric_bar_profile": {"status": "hit", "value": True},
        "material_grade": {"status": "hit", "value": "2Cr13"},
        "has_hole_feature": {"status": "hit", "value": True},
        "precision_tolerance_present": {"status": "hit", "value": True},
        "small_hole_relative_to_body_present": {"status": "hit", "value": True},
        "outer_surface_polish_required": {"status": "not_hit", "value": False},
        "formal_cleaning_required": {"status": "not_hit", "value": False},
    }
    expected_shaft = ("锯床下料", "粗车", "调质", "精车", "钻孔")
    for drawing_number, part_name in [
        ("IDENTITY-A", "叉杆"),
        ("COMPLETELY-DIFFERENT", "任意轴类名称"),
    ]:
        assert (
            route_for(
                {
                    **shaft_features,
                    "drawing_number": {"status": "hit", "value": drawing_number},
                    "part_name": {"status": "hit", "value": part_name},
                }
            )
            == expected_shaft
        )

    component_common = {
        "object_has_bom": {"status": "hit", "value": True},
        "weld_symbol_present": {"status": "hit", "value": True},
        "weld_annotation_present": {"status": "hit", "value": True},
        "technical_requirement_mentions_welding": {"status": "hit", "value": True},
        "weld_seam_finishing_required": {"status": "hit", "value": True},
        "large_precision_internal_cylindrical_surface_present": {
            "status": "hit",
            "value": True,
        },
        "external_mechanical_surface_finish_required": {
            "status": "not_hit",
            "value": False,
        },
        "surface_corrosion_protection_required": {
            "status": "not_hit",
            "value": False,
        },
        "formal_cleaning_required": {"status": "not_hit", "value": False},
    }
    expected_crossbeam = ("焊接(校正)", "抛光", "镗")
    for drawing_number, part_name in [
        ("IDENTITY-B", "横梁部件"),
        ("UNRELATED-NUMBER", "完全不同名称"),
    ]:
        assert (
            route_for(
                {
                    **component_common,
                    "drawing_number": {"status": "hit", "value": drawing_number},
                    "part_name": {"status": "hit", "value": part_name},
                }
            )
            == expected_crossbeam
        )

    without_large_bore = {
        **component_common,
        "drawing_number": {"status": "hit", "value": "IDENTITY-B"},
        "part_name": {"status": "hit", "value": "横梁部件"},
        "large_precision_internal_cylindrical_surface_present": {
            "status": "not_hit",
            "value": False,
        },
    }
    assert route_for(without_large_bore) == (
        "焊接(校正)",
        "抛光",
    )


@pytest.mark.integration
def test_single_axis_external_tolerance_drives_turning_without_identity(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    geometry_facts = {
        "object_has_bom": {"status": "not_hit", "value": False},
        "raw_form": {"status": "hit", "value": "bar"},
        "single_axis_external_cylindrical_profile": {
            "status": "hit",
            "value": True,
        },
        "large_axisymmetric_bar_profile": {"status": "not_hit", "value": False},
        "has_hole_feature": {"status": "not_hit", "value": False},
        "has_slot_feature": {"status": "not_hit", "value": False},
        "large_precision_internal_cylindrical_surface_present": {
            "status": "not_hit",
            "value": False,
        },
        "outer_surface_polish_required": {"status": "not_hit", "value": False},
        "external_mechanical_surface_finish_required": {
            "status": "not_hit",
            "value": False,
        },
        "formal_cleaning_required": {"status": "not_hit", "value": False},
    }
    for drawing_number, part_name in [
        ("IDENTITY-C", "销轴"),
        ("UNRELATED-IDENTITY", "任意单轴零件"),
    ]:
        scenarios = evaluate_scenarios(
            connection,
            runtime,
            {
                **geometry_facts,
                "precision_tolerance_present": {"status": "hit", "value": True},
                "drawing_number": {"status": "hit", "value": drawing_number},
                "part_name": {"status": "hit", "value": part_name},
            },
        )
        recommendation = assemble_recommendation(
            scenarios,
            tree_revision=runtime.revision,
        )
        assert recommendation.status == "complete"
        assert recommendation.route is not None
        assert tuple(item.process_name for item in recommendation.route) == (
            "锯床下料",
            "车",
        )

    without_tolerance = evaluate_scenarios(
        connection,
        runtime,
        {
            **geometry_facts,
            "precision_tolerance_present": {"status": "not_hit", "value": False},
        },
    )
    incomplete = assemble_recommendation(
        without_tolerance,
        tree_revision=runtime.revision,
    )
    assert incomplete.status != "complete"
    assert incomplete.route is None or all(
        item.process_name != "车" for item in incomplete.route
    )


@pytest.mark.integration
def test_corrosion_protection_without_method_and_finish_order_is_partial(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "object_has_bom": {"status": "hit", "value": True},
            "weld_symbol_present": {"status": "hit", "value": True},
            "weld_annotation_present": {"status": "hit", "value": True},
            "weld_seam_finishing_required": {"status": "hit", "value": True},
            "large_precision_internal_cylindrical_surface_present": {
                "status": "hit",
                "value": True,
            },
            "external_mechanical_surface_finish_required": {
                "status": "not_hit",
                "value": False,
            },
            "surface_corrosion_protection_required": {
                "status": "hit",
                "value": True,
            },
            "surface_protection_method": {"status": "unable_to_judge"},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
        },
    )

    recommendation = assemble_recommendation(scenarios, tree_revision=runtime.revision)

    assert recommendation.status == "partial"
    assert recommendation.route is not None
    assert tuple(item.process_name for item in recommendation.route) == ("焊接(校正)",)
    assert {
        tuple(item.process_name for item in candidate.operations)
        for candidate in recommendation.route_candidates
    } == {
        ("焊接(校正)", "抛光", "镗"),
        ("焊接(校正)", "镗", "抛光"),
    }
    missing = {
        fact for issue in recommendation.local_issues for fact in issue.missing_facts
    }
    assert any(fact.startswith("surface_protection_method:") for fact in missing)
    assert any(
        fact.startswith("weld_finish_precision_order_supported:") for fact in missing
    )

    explicit = evaluate_tree(
        connection,
        TREE_KEY,
        {
            "surface_corrosion_protection_required": {
                "status": "hit",
                "value": True,
            },
            "surface_protection_method": {
                "status": "hit",
                "value": "powder_coating",
            },
        },
    )
    assert any(
        item["result_status"] == "resolved"
        and item["outcome_type"] == "process"
        and item["outcome_value"]["process_name"] == "喷塑"
        for item in explicit
    )


@pytest.mark.integration
def test_plain_tube_stock_uses_saw_cut_without_plate_forming(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "object_has_bom": {"status": "not_hit", "value": False},
            "raw_form": {"status": "hit", "value": "tube"},
            "continuous_revolved_surface": {"status": "not_hit", "value": False},
            "has_bend_feature": {"status": "not_hit", "value": False},
            "has_hole_feature": {"status": "not_hit", "value": False},
            "has_slot_feature": {"status": "not_hit", "value": False},
            "precision_tolerance_present": {"status": "not_hit", "value": False},
            "outer_surface_polish_required": {"status": "not_hit", "value": False},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
            "drawing_number": {"status": "hit", "value": "ARBITRARY-TUBE"},
            "part_name": {"status": "hit", "value": "任意名称"},
        },
    )

    recommendation = assemble_recommendation(scenarios, tree_revision=runtime.revision)

    assert recommendation.status == "complete"
    assert recommendation.route is not None
    assert tuple(item.process_name for item in recommendation.route) == ("锯床下料",)


@pytest.mark.integration
def test_compact_axisymmetric_bar_uses_general_turning_without_identity(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree

    def recommendation_for(profile_status: str, profile_value: bool):
        scenarios = evaluate_scenarios(
            connection,
            runtime,
            {
                "object_has_bom": {"status": "not_hit", "value": False},
                "raw_form": {"status": "hit", "value": "bar"},
                "single_axis_external_cylindrical_profile": {
                    "status": profile_status,
                    "value": profile_value,
                },
                "large_axisymmetric_bar_profile": {
                    "status": "not_hit",
                    "value": False,
                },
                "has_hole_feature": {"status": "hit", "value": True},
                "precision_tolerance_present": {"status": "hit", "value": True},
                "small_hole_relative_to_body_present": {
                    "status": "hit",
                    "value": True,
                },
                "outer_surface_polish_required": {
                    "status": "not_hit",
                    "value": False,
                },
                "formal_cleaning_required": {"status": "not_hit", "value": False},
                "drawing_number": {"status": "hit", "value": "ARBITRARY-ID"},
                "part_name": {"status": "hit", "value": "任意名称"},
            },
        )
        return assemble_recommendation(scenarios, tree_revision=runtime.revision)

    recommendation = recommendation_for("hit", True)
    assert recommendation.status == "complete"
    assert recommendation.route is not None
    assert tuple(item.process_name for item in recommendation.route) == (
        "锯床下料",
        "车",
    )

    negative = recommendation_for("not_hit", False)
    assert negative.status == "partial"
    assert negative.route is not None
    assert tuple(item.process_name for item in negative.route) == ("锯床下料",)


@pytest.mark.integration
def test_part_surface_requirement_without_stage_evidence_is_partial(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "object_has_bom": {"status": "not_hit", "value": False},
            "raw_form": {"status": "hit", "value": "bar"},
            "single_axis_external_cylindrical_profile": {
                "status": "hit",
                "value": True,
            },
            "large_axisymmetric_bar_profile": {"status": "hit", "value": True},
            "material_grade": {"status": "hit", "value": "2Cr13"},
            "has_hole_feature": {"status": "hit", "value": True},
            "precision_tolerance_present": {"status": "hit", "value": True},
            "small_hole_relative_to_body_present": {
                "status": "hit",
                "value": True,
            },
            "outer_surface_polish_required": {"status": "hit", "value": True},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
        },
    )

    recommendation = assemble_recommendation(scenarios, tree_revision=runtime.revision)

    assert recommendation.status == "partial"
    assert recommendation.route is not None
    assert tuple(item.process_name for item in recommendation.route) == (
        "锯床下料",
        "粗车",
        "调质",
        "精车",
        "钻孔",
    )
    assert any(
        missing.startswith("surface_stage_owner:")
        for issue in recommendation.local_issues
        for missing in issue.missing_facts
    )


@pytest.mark.integration
def test_component_surface_requirement_without_stage_evidence_is_partial(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "object_has_bom": {"status": "hit", "value": True},
            "weld_symbol_present": {"status": "hit", "value": True},
            "weld_annotation_present": {"status": "hit", "value": True},
            "weld_seam_finishing_required": {"status": "hit", "value": True},
            "large_precision_internal_cylindrical_surface_present": {
                "status": "not_hit",
                "value": False,
            },
            "external_mechanical_surface_finish_required": {
                "status": "hit",
                "value": True,
            },
            "formal_cleaning_required": {"status": "not_hit", "value": False},
        },
    )

    recommendation = assemble_recommendation(scenarios, tree_revision=runtime.revision)

    assert recommendation.status == "partial"
    assert recommendation.route is not None
    assert tuple(item.process_name for item in recommendation.route) == (
        "焊接(校正)",
        "抛光",
    )
    assert any(
        missing.startswith("surface_stage_owner:")
        for issue in recommendation.local_issues
        for missing in issue.missing_facts
    )


@pytest.mark.integration
def test_unclassified_geometry_conflict_leaves_route_family_unresolved(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "drawing_number_numeric_prefix": {"status": "hit", "value": "80"},
            "object_has_bom": {"status": "not_hit", "value": False},
            "raw_form": {"status": "hit", "value": "plate"},
            "continuous_revolved_surface": {"status": "hit", "value": True},
            "has_bend_feature": {"status": "hit", "value": True},
        },
    )

    assert all("route_family" not in scenario.facts for scenario in scenarios)
    assert "part_base_route_required" in {
        match.rule_key
        for scenario in scenarios
        for match in scenario.matches
        if match.result_status == "error"
    }


@pytest.mark.integration
def test_runtime_tree_declares_only_pdf_observed_or_derived_facts(
    imported_tree: tuple[Connection, object],
) -> None:
    _, runtime = imported_tree

    assert all(
        contract.source_kind != "external"
        for contract in runtime.fact_contracts.values()
    )
    assert {
        "parent_part_type",
        "plm_part_name",
        "plm_drawing_number",
        "drawing_number_numeric_prefix",
        "route_module",
        "title_contains_assembly",
        "title_contains_welding",
    }.isdisjoint(runtime.fact_contracts)

    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    forbidden_decision_facts = {
        "drawing_number",
        "drawing_number_numeric_prefix",
        "part_name",
        "route_module",
        "title_contains_assembly",
        "title_contains_welding",
    }
    assert all(
        clause["fact_key"] not in forbidden_decision_facts
        for rule in payload["rules"]
        for clause in rule.get("clauses", [])
    )


@pytest.mark.integration
def test_rolled_sheet_without_feature_complete_sequence_stays_partial(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "object_has_bom": {"status": "not_hit", "value": False},
            "raw_form": {"status": "hit", "value": "plate"},
            "continuous_revolved_surface": {"status": "hit", "value": True},
            "continuous_rolled_shell_surface_present": {
                "status": "not_hit",
                "value": False,
            },
            "has_bend_feature": {"status": "not_hit", "value": False},
            "outer_surface_polish_required": {"status": "not_hit", "value": False},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
        },
    )

    recommendation = assemble_recommendation(scenarios, tree_revision=runtime.revision)

    assert recommendation.status == "partial"
    assert recommendation.route is not None
    assert [item.process_name for item in recommendation.route] == [
        "激光下料",
        "卷圆",
    ]
    assert "rolled_sheet_feature_sequence_incomplete" in {
        match.rule_key
        for scenario in scenarios
        for match in scenario.matches
        if match.result_status == "error"
    }


@pytest.mark.integration
def test_identity_name_does_not_resolve_conflicting_geometry(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    for part_name in ["斗体", "完全无关的名称"]:
        scenarios = evaluate_scenarios(
            connection,
            runtime,
            {
                "object_has_bom": {"status": "not_hit", "value": False},
                "part_name": {"status": "hit", "value": part_name},
                "raw_form": {"status": "hit", "value": "plate"},
                "continuous_revolved_surface": {"status": "hit", "value": True},
                "has_bend_feature": {"status": "hit", "value": True},
                "outer_surface_polish_required": {"status": "not_hit", "value": False},
                "formal_cleaning_required": {"status": "not_hit", "value": False},
            },
        )
        recommendation = assemble_recommendation(
            scenarios, tree_revision=runtime.revision
        )
        assert recommendation.status == "error"
        assert all("route_family" not in scenario.facts for scenario in scenarios)
        assert recommendation.route is None


@pytest.mark.integration
def test_missing_base_route_is_not_reported_as_complete(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "object_has_bom": {"status": "not_hit", "value": False},
            "continuous_revolved_surface": {"status": "hit", "value": True},
            "has_bend_feature": {"status": "not_hit", "value": False},
            "raw_form": {"status": "hit", "value": "other"},
            "outer_surface_polish_required": {"status": "not_hit", "value": False},
            "formal_cleaning_required": {"status": "not_hit", "value": False},
        },
    )

    recommendation = assemble_recommendation(scenarios, tree_revision=runtime.revision)
    assert recommendation.status == "error"
    assert recommendation.route is None
    assert any(
        missing.startswith("route_family:")
        for issue in recommendation.local_issues
        for missing in issue.missing_facts
    )


@pytest.mark.integration
def test_welded_component_without_weld_symbol_is_not_complete(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree
    scenarios = evaluate_scenarios(
        connection,
        runtime,
        {
            "object_has_bom": {"status": "hit", "value": True},
            "weld_symbol_present": {"status": "unable_to_judge"},
            "weld_annotation_present": {"status": "hit", "value": True},
            "outer_surface_polish_required": {"status": "not_hit", "value": False},
            "external_mechanical_surface_finish_required": {
                "status": "not_hit",
                "value": False,
            },
            "large_precision_internal_cylindrical_surface_present": {
                "status": "not_hit",
                "value": False,
            },
            "formal_cleaning_required": {"status": "not_hit", "value": False},
        },
    )

    recommendation = assemble_recommendation(scenarios, tree_revision=runtime.revision)
    assert recommendation.status == "error"
    assert recommendation.route is None
    assert {issue.code for issue in recommendation.local_issues} == {"NO_ROUTE_RESULT"}


@pytest.mark.integration
def test_structural_object_fact_ignores_identity_metadata(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, runtime = imported_tree

    facts, _, issues = evaluate_closure(
        connection,
        runtime,
        {
            "drawing_number": {"status": "hit", "value": "50-LOOKALIKE"},
            "part_name": {"status": "hit", "value": "焊接部件"},
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
            "object_kind": {"status": "hit", "value": "component"},
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
def test_unavailable_feature_does_not_trigger_identity_fallback(
    imported_tree: tuple[Connection, object],
) -> None:
    connection, _ = imported_tree
    results = evaluate_tree(
        connection,
        TREE_KEY,
        {
            "object_has_bom": {"status": "hit", "value": True},
            "weld_symbol_present": {"status": "unable_to_judge"},
            "object_kind": {"status": "hit", "value": "component"},
            "component_kind": {"status": "hit", "value": "welded"},
            "drawing_number": {"status": "hit", "value": "KNOWN-ANSWER"},
            "part_name": {"status": "hit", "value": "横梁部件"},
            "external_mechanical_surface_finish_required": {
                "status": "not_hit",
                "value": False,
            },
            "large_precision_internal_cylindrical_surface_present": {
                "status": "not_hit",
                "value": False,
            },
        },
    )

    assert not any(item["result_status"] == "error" for item in results)
    assert not any(
        item["result_status"] == "resolved" and item["outcome_type"] == "process"
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

    snapshot = next(
        item for item in snapshot_matches if item.rule_key == "rolling_operation"
    )
    active = next(
        item for item in active_matches if item["rule_key"] == "rolling_operation"
    )
    assert snapshot.option_label == "卷圆"
    assert active["option_label"] == "更新后的卷圆"
