import json
from pathlib import Path

import pytest
from drawing_route_auditor.db.connection import Connection

from drawing_route_auditor.decision_tree.editor import apply_tree_patch
from drawing_route_auditor.db.knowledge_migrations import (
    apply_compact_geometry_features,
    apply_compact_axisymmetric_turning,
    apply_cylindrical_projection_guard,
    apply_stepped_bar_evidence_guard,
    apply_preserve_tube_stock_guard,
    apply_cover_pdf_family_upgrade,
    apply_feature_derived_routes_upgrade,
    apply_external_mechanical_finish,
    apply_external_finish_reader_guard,
    apply_feature_metadata_cleanup,
    apply_derived_prerequisite_closure,
    apply_geometry_guard_cleanup,
    apply_hopper_geometry_upgrade,
    apply_local_flange_upgrade,
    apply_large_precision_boring,
    apply_large_bore_reader_ownership,
    apply_large_bore_reader_guard,
    apply_large_bore_judgement_guard,
    apply_directional_surface_finish,
    apply_reader_feature_guards,
    apply_small_hole_drilling,
    apply_multi_joint_access_guard,
    apply_oriented_facts_upgrade,
    apply_precision_weldment_access,
    apply_pdf_only_upgrade,
    apply_pdf_only_metadata_upgrade,
    apply_remove_unvalidated_weld_stages,
    apply_surface_stage_ownership_guard,
    apply_surface_branch_metadata,
    apply_shaft_local_hole_geometry_guard,
    apply_tube_stock_cut_route,
    apply_robust_family_upgrade,
    apply_weld_presence_routes,
    apply_rolled_feature_completeness,
    apply_welded_multi_joint_guard,
    apply_strict_geometry_upgrade,
)
from drawing_route_auditor.decision_tree.importer import initialize_decision_tree
from drawing_route_auditor.decision_tree.repository import tree_details, validate_tree
from drawing_route_auditor.decision_tree.repository import current_tree_payload


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


def _pre_feature_definition(tmp_path: Path, tree_key: str) -> Path:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    payload["tree_key"] = tree_key
    object_kind = next(
        item for item in payload["facts"] if item["fact_key"] == "object_kind"
    )
    object_kind["judgement_definition"] = "旧版按图号数字前缀和BOM派生。"
    payload["rules"].append(
        {
            "branch_key": "3.6",
            "rule_key": "synthetic_identity_route_leak",
            "description": "旧版按图号直接产生工序。",
            "decision_key": "synthetic_identity_route",
            "question": "按图号选择什么工序？",
            "option_key": "synthetic_identity_process",
            "option_label": "身份工序",
            "result_kind": "resolved",
            "outcome_type": "process",
            "outcome_key": "synthetic_identity_process",
            "outcome_value": {
                "order_rank": 1,
                "process_name": "身份工序",
                "operation_key": "synthetic_identity_process",
            },
            "clauses": [
                {
                    "fact_key": "drawing_number",
                    "operator": "starts_with",
                    "expected_value": "KNOWN-ANSWER",
                }
            ],
            "missing_behavior": "not_match",
            "priority": 999,
        }
    )
    path = tmp_path / f"pre-feature-{tree_key}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
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
    assert first.fact_count == 33
    assert first.node_count == 4
    assert first.branch_count == 15
    assert first.rule_count == 42

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
        "observed_owned": 27,
        "non_observed_unowned": 6,
        "total": 33,
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
    exported = current_tree_payload(db_connection, tree_key)
    exported_rule = next(
        item for item in exported["rules"] if item["rule_key"] == "rolling_operation"
    )
    assert exported_rule["description"] == "Incrementally updated rolling rule"
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
def test_current_tree_is_pdf_only_and_all_cleanup_migrations_are_idempotent(
    db_connection: Connection,
    tmp_path: Path,
) -> None:
    tree_key = "integration-tree-pdf-only"
    initialize_decision_tree(db_connection, _definition(tmp_path, tree_key))
    payload = current_tree_payload(db_connection, tree_key)

    assert all(item["source_kind"] != "external" for item in payload["facts"])
    assert {
        "parent_part_type",
        "plm_part_name",
        "plm_drawing_number",
    }.isdisjoint(item["fact_key"] for item in payload["facts"])
    facts_by_key = {item["fact_key"]: item for item in payload["facts"]}
    assert "304方管80×50×5" in facts_by_key["raw_form"]["judgement_definition"]
    assert (
        "M10等局部后加工孔"
        in facts_by_key["large_axisymmetric_bar_profile"]["judgement_definition"]
    )
    assert "标准管材的壁厚不是板厚" in facts_by_key["sheet_thickness_mm"]["description"]
    assert "304方管80×50×5" in facts_by_key["is_plate_part"]["judgement_definition"]
    assert (
        "矩形方管"
        in facts_by_key["continuous_revolved_surface"]["judgement_definition"]
    )
    assert "single_axis_external_cylindrical_profile" in facts_by_key
    assert (
        "实体外圆"
        in facts_by_key["continuous_revolved_surface"]["judgement_definition"]
    )
    assert (
        "简单圆柱在轴向主视图"
        in facts_by_key["single_axis_external_cylindrical_profile"][
            "judgement_definition"
        ]
    )
    assert "不得只检查最大横向尺寸" in facts_by_key["raw_form"]["coverage_requirement"]
    assert "tube_cut_part" in facts_by_key["route_family"]["allowed_values"]
    branches_by_key = {item["branch_key"]: item for item in payload["branches"]}
    assert "承担不明时保持部分结果" in branches_by_key["3.4"]["rule_text"]
    assert apply_pdf_only_upgrade(db_connection, tree_key=tree_key) is None
    assert apply_oriented_facts_upgrade(db_connection, tree_key=tree_key) is None
    assert apply_robust_family_upgrade(db_connection, tree_key=tree_key) is None
    assert apply_strict_geometry_upgrade(db_connection, tree_key=tree_key) is None
    assert apply_pdf_only_metadata_upgrade(db_connection, tree_key=tree_key) is None
    assert apply_cover_pdf_family_upgrade(db_connection, tree_key=tree_key) is None
    assert apply_local_flange_upgrade(db_connection, tree_key=tree_key) is None
    assert apply_hopper_geometry_upgrade(db_connection, tree_key=tree_key) is None
    assert apply_geometry_guard_cleanup(db_connection, tree_key=tree_key) is None
    assert (
        apply_feature_derived_routes_upgrade(db_connection, tree_key=tree_key) is None
    )
    assert apply_feature_metadata_cleanup(db_connection, tree_key=tree_key) is None
    assert apply_rolled_feature_completeness(db_connection, tree_key=tree_key) is None
    assert apply_derived_prerequisite_closure(db_connection, tree_key=tree_key) is None
    assert apply_reader_feature_guards(db_connection, tree_key=tree_key) is None
    assert apply_small_hole_drilling(db_connection, tree_key=tree_key) is None
    assert apply_large_precision_boring(db_connection, tree_key=tree_key) is None
    assert apply_directional_surface_finish(db_connection, tree_key=tree_key) is None
    assert apply_large_bore_reader_guard(db_connection, tree_key=tree_key) is None
    assert apply_multi_joint_access_guard(db_connection, tree_key=tree_key) is None
    assert apply_compact_geometry_features(db_connection, tree_key=tree_key) is None
    assert apply_welded_multi_joint_guard(db_connection, tree_key=tree_key) is None
    assert apply_precision_weldment_access(db_connection, tree_key=tree_key) is None
    assert apply_large_bore_reader_ownership(db_connection, tree_key=tree_key) is None
    assert (
        apply_remove_unvalidated_weld_stages(db_connection, tree_key=tree_key) is None
    )
    assert apply_external_mechanical_finish(db_connection, tree_key=tree_key) is None
    assert apply_weld_presence_routes(db_connection, tree_key=tree_key) is None
    assert apply_external_finish_reader_guard(db_connection, tree_key=tree_key) is None
    assert apply_large_bore_judgement_guard(db_connection, tree_key=tree_key) is None
    assert apply_surface_stage_ownership_guard(db_connection, tree_key=tree_key) is None
    assert (
        apply_shaft_local_hole_geometry_guard(db_connection, tree_key=tree_key) is None
    )
    assert apply_surface_branch_metadata(db_connection, tree_key=tree_key) is None
    assert apply_tube_stock_cut_route(db_connection, tree_key=tree_key) is None
    assert apply_compact_axisymmetric_turning(db_connection, tree_key=tree_key) is None
    assert apply_cylindrical_projection_guard(db_connection, tree_key=tree_key) is None
    assert apply_stepped_bar_evidence_guard(db_connection, tree_key=tree_key) is None
    assert apply_preserve_tube_stock_guard(db_connection, tree_key=tree_key) is None


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
def test_patch_rejects_removing_a_rule_output_fact(
    db_connection: Connection,
    tmp_path: Path,
) -> None:
    tree_key = "integration-tree-output-fact"
    source = _definition(tmp_path, tree_key)
    initialized = initialize_decision_tree(db_connection, source)
    patch = tmp_path / "remove-output-fact.json"
    patch.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tree_key": tree_key,
                "operations": [
                    {
                        "op": "remove",
                        "collection": "facts",
                        "key": "component_kind",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="产出未知事实"):
        apply_tree_patch(db_connection, patch)

    assert tree_details(db_connection, tree_key)["revision_id"] == (
        initialized.revision_id
    )


@pytest.mark.integration
def test_feature_cleanup_is_atomic_idempotent_and_removes_unknown_identity_rules(
    db_connection: Connection,
    tmp_path: Path,
) -> None:
    tree_key = "integration-tree-pre-feature-cleanup"
    source = _pre_feature_definition(tmp_path, tree_key)
    legacy = initialize_decision_tree(db_connection, source)
    before_payload = current_tree_payload(db_connection, tree_key)

    upgraded = apply_feature_derived_routes_upgrade(db_connection, tree_key=tree_key)

    assert upgraded is not None
    assert upgraded.changed is True
    assert upgraded.revision_id != legacy.revision_id
    current = current_tree_payload(db_connection, tree_key)
    forbidden = {
        "drawing_number",
        "drawing_number_numeric_prefix",
        "part_name",
        "route_module",
        "title_contains_assembly",
        "title_contains_welding",
    }
    assert all(
        clause["fact_key"] not in forbidden
        for rule in current["rules"]
        for clause in rule.get("clauses", [])
    )
    assert "synthetic_identity_route_leak" not in {
        rule["rule_key"] for rule in current["rules"]
    }
    old_revision = db_connection.execute(
        "SELECT status, source_payload FROM decision_tree_versions WHERE id = %s",
        (legacy.revision_id,),
    ).fetchone()
    assert old_revision["status"] == "retired"
    assert old_revision["source_payload"] == before_payload

    assert (
        apply_feature_derived_routes_upgrade(db_connection, tree_key=tree_key) is None
    )
    metadata = apply_feature_metadata_cleanup(db_connection, tree_key=tree_key)
    assert metadata is not None
    assert apply_feature_metadata_cleanup(db_connection, tree_key=tree_key) is None


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
        "branches": 15,
        "edges": 5,
        "rules": 42,
        "clauses": 107,
    }
    assert len(details["readers"]) == 4
    assert len(details["facts"]) == 33
    assert len(details["rules"]) == 42
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
