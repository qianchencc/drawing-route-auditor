from __future__ import annotations

from importlib.resources import files
import json

from drawing_route_auditor.db.connection import Connection
from drawing_route_auditor.decision_tree.editor import (
    DecisionTreePatch,
    TreePatchOperation,
    apply_tree_patch_model,
)
from drawing_route_auditor.decision_tree.importer import TreeUpdateSummary


DEFAULT_TREE_KEY = "drawing-process-tree"
_CURRENT_REQUIRED_FACTS = {"surface_stage_owner"}
_CURRENT_REQUIRED_RULES = {
    "hopper_calibration_rolling",
    "hopper_seam_welding",
    "hopper_segment_prejoin",
    "rolled_child_outer_polish_owned_by_parent",
    "rolled_sheet_laser_blanking",
    "rolled_sheet_process_module_unavailable",
    "surface_stage_required",
}
_COVER_REQUIRED_FACTS = {"plm_part_name"}
_COVER_REQUIRED_RULES = {
    "bend_revolution_conflict_guard",
    "verified_cover_shell_laser_blanking",
}
_FIVE_SAMPLE_REQUIRED_FACTS = {"plm_drawing_number", "route_module"}
_FIVE_SAMPLE_REQUIRED_RULES = {
    "nt_crossbeam_final_polish",
    "nt_crossbeam_surface_integrated",
    "ntf_fork_drill",
    "ntf_fork_route_family",
    "ntf_fork_surface_integrated",
    "ntf_hopper_final_polish",
    "ntf_hopper_surface_integrated",
}
_PDF_ONLY_FORBIDDEN_FACTS = {
    "parent_part_type",
    "plm_drawing_number",
    "plm_part_name",
}
_PDF_ONLY_REMOVED_RULES = {
    "rolled_child_outer_polish_owned_by_parent",
    "transfer_to_assembly",
    "transfer_to_subassembly",
    "transfer_to_welding",
}
_ORIENTED_FACT_REQUIRED_RULES = {
    "bent_plate_hole_laser_blanking",
    "welded_component_by_verified_module",
}
_FEATURE_ROUTE_REQUIRED_FACTS = {
    "large_axisymmetric_bar_profile",
    "large_precision_internal_cylindrical_surface_present",
    "small_hole_relative_to_body_present",
    "weld_seam_finishing_required",
}
_FEATURE_ROUTE_REQUIRED_RULES = {
    "axisymmetric_bar_machined_family",
    "bar_saw_blanking",
    "small_hole_drilling",
    "welded_component_initial_weld",
}
_FEATURE_ROUTE_REMOVED_FACTS = {
    "independent_weld_joint_group_count",
    "axial_length_mm",
    "drawing_number_numeric_prefix",
    "global_axisymmetric_bar_profile",
    "max_outer_diameter_mm",
    "route_module",
    "tight_tolerance_internal_profile_present",
    "title_contains_assembly",
    "title_contains_welding",
}
_FEATURE_ROUTE_FORBIDDEN_CLAUSE_FACTS = {
    "drawing_number",
    "drawing_number_numeric_prefix",
    "part_name",
    "route_module",
    "title_contains_assembly",
    "title_contains_welding",
}


def _active_source_payload(
    connection: Connection,
    tree_key: str,
) -> dict[str, object] | None:
    current = connection.execute(
        """
        SELECT revision.source_payload
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS revision ON revision.tree_id = tree.id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()
    if current is None:
        return None
    payload = current["source_payload"]
    if not isinstance(payload, dict):
        raise RuntimeError("当前决策树存储载荷无效")
    return payload


def _unvalidated_weld_stages_removed(payload: dict[str, object]) -> bool:
    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    removed_facts = {
        "machining_access_obstructing_member_present",
        "separated_weld_access_regions_present",
    }
    removed_rules = {
        "deferred_member_weld_finish_polish",
        "deferred_obstructing_member_weld",
        "multi_joint_access_classification_required",
        "precision_weldment_access_classification_required",
        "separated_access_secondary_weld",
    }
    return removed_facts.isdisjoint(facts) and removed_rules.isdisjoint(rules)


def _upgrade_patch(tree_key: str, data_name: str) -> DecisionTreePatch:
    resource = files("drawing_route_auditor.db.data").joinpath(data_name)
    payload = json.loads(resource.read_text(encoding="utf-8"))
    payload["tree_key"] = tree_key
    return DecisionTreePatch.model_validate(payload)


def _apply_upgrade(
    connection: Connection,
    *,
    tree_key: str,
    data_name: str,
    required_facts: set[str],
    required_rules: set[str],
    source_label: str,
) -> TreeUpdateSummary | None:
    current = connection.execute(
        """
        SELECT revision.id AS revision_id
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS revision ON revision.tree_id = tree.id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()
    if current is None:
        return None

    facts = {
        row["fact_key"]
        for row in connection.execute(
            "SELECT fact_key FROM fact_definitions WHERE version_id = %s",
            (current["revision_id"],),
        ).fetchall()
    }
    rules = {
        row["rule_key"]
        for row in connection.execute(
            "SELECT rule_key FROM decision_rules WHERE version_id = %s",
            (current["revision_id"],),
        ).fetchall()
    }
    if (
        int(data_name[:4]) < 20
        and _FEATURE_ROUTE_REQUIRED_FACTS <= facts
        and _FEATURE_ROUTE_REQUIRED_RULES <= rules
        and _FEATURE_ROUTE_REMOVED_FACTS.isdisjoint(facts)
    ):
        return None
    if required_facts <= facts and required_rules <= rules:
        return None

    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, data_name),
        source_label=source_label,
    )


def apply_current_tree_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0008_current_tree_upgrade.json",
        required_facts=_CURRENT_REQUIRED_FACTS,
        required_rules=_CURRENT_REQUIRED_RULES,
        source_label="migration:0008_current_tree_knowledge",
    )


def apply_cover_shell_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0009_cover_shell_route.json",
        required_facts=_COVER_REQUIRED_FACTS,
        required_rules=_COVER_REQUIRED_RULES,
        source_label="migration:0009_cover_shell_blanking_knowledge",
    )


def apply_five_sample_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0010_five_sample_route_modules.json",
        required_facts=_FIVE_SAMPLE_REQUIRED_FACTS,
        required_rules=_FIVE_SAMPLE_REQUIRED_RULES,
        source_label="migration:0010_five_sample_route_modules",
    )


def apply_pdf_only_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    current = connection.execute(
        """
        SELECT revision.source_payload
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS revision ON revision.tree_id = tree.id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()
    if current is None:
        return None

    payload = current["source_payload"]
    if not isinstance(payload, dict):
        raise RuntimeError("当前决策树存储载荷无效")
    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key"): item
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    forbidden_present = _PDF_ONLY_FORBIDDEN_FACTS & facts
    removed_rules_present = _PDF_ONLY_REMOVED_RULES & set(rules)
    forbidden_references = {
        rule_key
        for rule_key, rule in rules.items()
        if any(
            clause.get("fact_key") in _PDF_ONLY_FORBIDDEN_FACTS
            for clause in rule.get("clauses", [])
            if isinstance(clause, dict)
        )
    }
    if not (forbidden_present or removed_rules_present or forbidden_references):
        return None
    if forbidden_present != _PDF_ONLY_FORBIDDEN_FACTS:
        raise RuntimeError(
            f"PDF-only 迁移遇到部分外部事实状态：{sorted(forbidden_present)}"
        )
    if removed_rules_present != _PDF_ONLY_REMOVED_RULES:
        raise RuntimeError(
            f"PDF-only 迁移遇到部分待删除规则状态：{sorted(removed_rules_present)}"
        )

    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0011_pdf_only_inference.json"),
        source_label="migration:0011_pdf_only_inference",
    )


def apply_oriented_facts_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0012_oriented_drawing_facts.json",
        required_facts=set(),
        required_rules=_ORIENTED_FACT_REQUIRED_RULES,
        source_label="migration:0012_oriented_drawing_facts",
    )


def apply_robust_family_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    current = connection.execute(
        """
        SELECT revision.source_payload
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS revision ON revision.tree_id = tree.id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()
    if current is None:
        return None
    payload = current["source_payload"]
    if not isinstance(payload, dict):
        raise RuntimeError("当前决策树存储载荷无效")
    fork_rule = next(
        (
            item
            for item in payload.get("rules", [])
            if isinstance(item, dict) and item.get("rule_key") == "ntf_fork_rod_module"
        ),
        None,
    )
    if fork_rule is None:
        return None
    if not any(
        isinstance(clause, dict) and clause.get("fact_key") == "part_name"
        for clause in fork_rule.get("clauses", [])
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0013_robust_pdf_family_rules.json"),
        source_label="migration:0013_robust_pdf_family_rules",
    )


def apply_strict_geometry_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    current = connection.execute(
        """
        SELECT revision.source_payload
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS revision ON revision.tree_id = tree.id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()
    if current is None:
        return None
    payload = current["source_payload"]
    if not isinstance(payload, dict):
        raise RuntimeError("当前决策树存储载荷无效")
    bom_fact = next(
        (
            item
            for item in payload.get("facts", [])
            if isinstance(item, dict) and item.get("fact_key") == "object_has_bom"
        ),
        None,
    )
    if bom_fact is None:
        return None
    definition = bom_fact.get("judgement_definition")
    if isinstance(definition, str) and "至少一条已填写" in definition:
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0014_strict_pdf_geometry.json"),
        source_label="migration:0014_strict_pdf_geometry",
    )


def apply_pdf_only_metadata_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    current = connection.execute(
        """
        SELECT revision.source_payload
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS revision ON revision.tree_id = tree.id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()
    if current is None:
        return None
    payload = current["source_payload"]
    if not isinstance(payload, dict):
        raise RuntimeError("当前决策树存储载荷无效")
    module_branch = next(
        (
            item
            for item in payload.get("branches", [])
            if isinstance(item, dict) and item.get("branch_key") == "3.6"
        ),
        None,
    )
    if module_branch is None:
        return None
    rule_text = module_branch.get("rule_text")
    if isinstance(rule_text, str) and "PLM" not in rule_text:
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0015_pdf_only_tree_metadata.json"),
        source_label="migration:0015_pdf_only_tree_metadata",
    )


def apply_cover_pdf_family_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0016_cover_shell_pdf_family.json",
        required_facts=set(),
        required_rules={"cover_shell_bent_family"},
        source_label="migration:0016_cover_shell_pdf_family",
    )


def apply_local_flange_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    current = connection.execute(
        """
        SELECT revision.source_payload
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS revision ON revision.tree_id = tree.id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()
    if current is None:
        return None
    payload = current["source_payload"]
    if not isinstance(payload, dict):
        raise RuntimeError("当前决策树存储载荷无效")
    bend_fact = next(
        (
            item
            for item in payload.get("facts", [])
            if isinstance(item, dict) and item.get("fact_key") == "has_bend_feature"
        ),
        None,
    )
    if bend_fact is None:
        return None
    definition = bend_fact.get("judgement_definition")
    if isinstance(definition, str) and "小角度短翻边" in definition:
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0017_local_flange_bend.json"),
        source_label="migration:0017_local_flange_bend",
    )


def apply_hopper_geometry_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0018_hopper_geometry_precedence.json",
        required_facts=set(),
        required_rules={"hopper_rolled_family_override"},
        source_label="migration:0018_hopper_geometry_precedence",
    )


def apply_geometry_guard_cleanup(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    current = connection.execute(
        """
        SELECT revision.source_payload
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS revision ON revision.tree_id = tree.id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()
    if current is None:
        return None
    payload = current["source_payload"]
    if not isinstance(payload, dict):
        raise RuntimeError("当前决策树存储载荷无效")
    if not any(
        isinstance(item, dict)
        and item.get("rule_key") == "bend_revolution_conflict_guard"
        for item in payload.get("rules", [])
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0019_remove_redundant_geometry_guard.json"),
        source_label="migration:0019_remove_redundant_geometry_guard",
    )


def apply_feature_derived_routes_upgrade(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None

    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key"): item
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    forbidden_references = {
        rule_key
        for rule_key, rule in rules.items()
        if isinstance(rule_key, str)
        and any(
            isinstance(clause, dict)
            and clause.get("fact_key") in _FEATURE_ROUTE_FORBIDDEN_CLAUSE_FACTS
            for clause in rule.get("clauses", [])
        )
    }
    compact_contract = (
        _FEATURE_ROUTE_REQUIRED_FACTS <= facts
        and _FEATURE_ROUTE_REQUIRED_RULES <= set(rules)
        and _FEATURE_ROUTE_REMOVED_FACTS.isdisjoint(facts)
    )
    legacy_feature_contract = {
        "global_axisymmetric_bar_profile",
        "independent_weld_joint_group_count",
        "tight_tolerance_internal_profile_present",
        "weld_seam_finishing_required",
    } <= facts and {
        "axisymmetric_bar_machined_family",
        "bar_saw_blanking",
        "internal_profile_wire_cut",
        "welded_component_initial_weld",
    } <= set(rules)
    identity_facts_absent = {
        "drawing_number_numeric_prefix",
        "route_module",
        "title_contains_assembly",
        "title_contains_welding",
    }.isdisjoint(facts)
    already_current = (
        (compact_contract or legacy_feature_contract)
        and identity_facts_absent
        and not forbidden_references
    )
    if already_current:
        return None

    patch = _upgrade_patch(tree_key, "0020_feature_derived_routes.json")
    collections = {
        "facts": facts,
        "rules": set(rules),
        "branches": {
            item.get("branch_key")
            for item in payload.get("branches", [])
            if isinstance(item, dict)
        },
    }
    operations = [
        operation
        for operation in patch.operations
        if operation.op != "remove"
        or operation.key in collections.get(operation.collection, set())
    ]
    scheduled_rule_removals = {
        operation.key
        for operation in operations
        if operation.op == "remove" and operation.collection == "rules"
    }
    operations.extend(
        TreePatchOperation(op="remove", collection="rules", key=rule_key)
        for rule_key in sorted(forbidden_references - scheduled_rule_removals)
    )
    return apply_tree_patch_model(
        connection,
        patch.model_copy(update={"operations": operations}),
        source_label="migration:0020_feature_derived_routes",
    )


def apply_feature_metadata_cleanup(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key"): item
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    object_kind = facts.get("object_kind")
    component_kind = facts.get("component_kind")
    if (
        isinstance(object_kind, dict)
        and isinstance(component_kind, dict)
        and "图号数字前缀" not in str(object_kind.get("judgement_definition", ""))
        and "由名称" not in str(component_kind.get("judgement_definition", ""))
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0021_feature_metadata_cleanup.json"),
        source_label="migration:0021_feature_metadata_cleanup",
    )


def apply_rolled_feature_completeness(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0022_rolled_feature_completeness.json",
        required_facts=set(),
        required_rules={"rolled_sheet_feature_sequence_incomplete"},
        source_label="migration:0022_rolled_feature_completeness",
    )


def apply_derived_prerequisite_closure(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    required = {
        "assembly_component_requirement_candidate",
        "welded_component_by_symbol",
        "welded_component_feature_route_required",
        "welded_component_requirement_candidate",
    }
    rules = {
        item.get("rule_key"): item
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    initial_weld = rules.get("welded_component_initial_weld")
    if (
        "independent_weld_joint_group_count" not in facts
        and "welded_component_feature_route_required" not in rules
        and isinstance(initial_weld, dict)
        and any(
            isinstance(clause, dict) and clause.get("fact_key") == "weld_symbol_present"
            for clause in initial_weld.get("clauses", [])
        )
    ):
        return None
    if required <= set(rules) and all(
        rules[key].get("missing_behavior") == "not_match" for key in required
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0023_derived_prerequisite_closure.json"),
        source_label="migration:0023_derived_prerequisite_closure",
    )


def apply_reader_feature_guards(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key"): item
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    weld_groups = facts.get("independent_weld_joint_group_count")
    if isinstance(facts.get("small_hole_relative_to_body_present"), dict):
        return None
    transverse_hole = facts.get("transverse_hole_feature_present")
    if (
        isinstance(weld_groups, dict)
        and weld_groups.get("not_hit_criteria")
        and isinstance(transverse_hole, dict)
        and "正交视图" in str(transverse_hole.get("judgement_definition", ""))
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0024_reader_feature_guards.json"),
        source_label="migration:0024_reader_feature_guards",
    )


def apply_small_hole_drilling(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    if (
        "small_hole_relative_to_body_present" in facts
        and "small_hole_drilling" in rules
        and "transverse_hole_feature_present" not in facts
        and "transverse_hole_drilling" not in rules
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0025_small_hole_drilling.json"),
        source_label="migration:0025_small_hole_drilling",
    )


def apply_large_precision_boring(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    if (
        "large_precision_internal_cylindrical_surface_present" in facts
        and "weldment_large_precision_boring" in rules
        and "coaxial_precision_bore_across_members" not in facts
        and "weldment_coaxial_boring" not in rules
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0026_large_precision_boring.json"),
        source_label="migration:0026_large_precision_boring",
    )


def apply_directional_surface_finish(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    if any(
        isinstance(item, dict)
        and item.get("fact_key") == "external_mechanical_surface_finish_required"
        for item in payload.get("facts", [])
    ):
        return None
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    if (
        "directional_surface_finish" in rules
        and "intermediate_directional_surface_finish" not in rules
        and "final_directional_surface_finish" not in rules
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0027_directional_surface_finish.json"),
        source_label="migration:0027_directional_surface_finish",
    )


def apply_large_bore_reader_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    fact = next(
        (
            item
            for item in payload.get("facts", [])
            if isinstance(item, dict)
            and item.get("fact_key")
            == "large_precision_internal_cylindrical_surface_present"
        ),
        None,
    )
    if isinstance(fact, dict) and "任意一项" in str(fact.get("hit_criteria", "")):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0028_large_bore_reader_guard.json"),
        source_label="migration:0028_large_bore_reader_guard",
    )


def apply_multi_joint_access_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None or _unvalidated_weld_stages_removed(payload):
        return None
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0029_multi_joint_access_guard.json",
        required_facts=set(),
        required_rules={"multi_joint_access_classification_required"},
        source_label="migration:0029_multi_joint_access_guard",
    )


def apply_compact_geometry_features(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    removed_facts = {
        "axial_length_mm",
        "global_axisymmetric_bar_profile",
        "max_outer_diameter_mm",
        "tight_tolerance_internal_profile_present",
    }
    if (
        "large_axisymmetric_bar_profile" in facts
        and "internal_profile_wire_cut" not in rules
        and removed_facts.isdisjoint(facts)
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0030_compact_geometry_features.json"),
        source_label="migration:0030_compact_geometry_features",
    )


def apply_welded_multi_joint_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    if _unvalidated_weld_stages_removed(payload):
        return None
    rule = next(
        (
            item
            for item in payload.get("rules", [])
            if isinstance(item, dict)
            and item.get("rule_key") == "multi_joint_access_classification_required"
        ),
        None,
    )
    if isinstance(rule, dict) and any(
        isinstance(clause, dict)
        and clause.get("fact_key") == "weld_symbol_present"
        and clause.get("expected_value") is True
        for clause in rule.get("clauses", [])
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0031_welded_multi_joint_guard.json"),
        source_label="migration:0031_welded_multi_joint_guard",
    )


def apply_precision_weldment_access(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    if _unvalidated_weld_stages_removed(payload):
        return None
    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    if (
        "precision_weldment_access_classification_required" in rules
        and "deferred_obstructing_member_weld" in rules
        and "deferred_member_weld_finish_polish" in rules
        and "deferred_continuous_weld" not in rules
        and "deferred_weld_finish_polish" not in rules
        and "long_continuous_weld_joint_present" not in facts
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0032_precision_weldment_access.json"),
        source_label="migration:0032_precision_weldment_access",
    )


def apply_large_bore_reader_ownership(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    fact = next(
        (
            item
            for item in payload.get("facts", [])
            if isinstance(item, dict)
            and item.get("fact_key")
            == "large_precision_internal_cylindrical_surface_present"
        ),
        None,
    )
    if isinstance(fact, dict) and fact.get("reader_key") == "symbol_relation_reader":
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0033_large_bore_reader_ownership.json"),
        source_label="migration:0033_large_bore_reader_ownership",
    )


def apply_remove_unvalidated_weld_stages(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None or _unvalidated_weld_stages_removed(payload):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0034_remove_unvalidated_weld_stages.json"),
        source_label="migration:0034_remove_unvalidated_weld_stages",
    )


def apply_external_mechanical_finish(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    if (
        "external_mechanical_surface_finish_required" in facts
        and (
            "external_mechanical_surface_finish" in rules
            or "component_surface_stage_required" in rules
        )
        and "directional_surface_finish_required" not in facts
        and "directional_surface_finish" not in rules
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0035_external_mechanical_finish.json"),
        source_label="migration:0035_external_mechanical_finish",
    )


def apply_weld_presence_routes(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key")
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key"): item
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    initial_weld = rules.get("welded_component_initial_weld")
    if (
        "independent_weld_joint_group_count" not in facts
        and "welded_component_feature_route_required" not in rules
        and isinstance(initial_weld, dict)
        and any(
            isinstance(clause, dict) and clause.get("fact_key") == "weld_symbol_present"
            for clause in initial_weld.get("clauses", [])
        )
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0036_weld_presence_routes.json"),
        source_label="migration:0036_weld_presence_routes",
    )


def apply_external_finish_reader_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key"): item
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    fact = facts.get("external_mechanical_surface_finish_required")
    if (
        isinstance(fact, dict)
        and "无工艺意义" in str(fact.get("not_hit_criteria", ""))
        and "component_external_finish_judgement_required" in rules
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0037_external_finish_reader_guard.json"),
        source_label="migration:0037_external_finish_reader_guard",
    )


def apply_large_bore_judgement_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0038_large_bore_judgement_guard.json",
        required_facts=set(),
        required_rules={"component_large_bore_judgement_required"},
        source_label="migration:0038_large_bore_judgement_guard",
    )


def apply_surface_stage_ownership_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    if (
        "component_surface_stage_required" in rules
        and "explicit_part_surface_current_level" not in rules
        and "external_mechanical_surface_finish" not in rules
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0039_surface_stage_ownership_guard.json"),
        source_label="migration:0039_surface_stage_ownership_guard",
    )


def apply_shaft_local_hole_geometry_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key"): item
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    raw_form_definition = str(facts.get("raw_form", {}).get("judgement_definition", ""))
    large_profile_definition = str(
        facts.get("large_axisymmetric_bar_profile", {}).get("judgement_definition", "")
    )
    if (
        "M10等局部螺纹孔" in raw_form_definition
        or "局部后加工孔不改变该判断" in raw_form_definition
    ) and "M10等局部后加工孔" in large_profile_definition:
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0040_shaft_local_hole_geometry_guard.json"),
        source_label="migration:0040_shaft_local_hole_geometry_guard",
    )


def apply_surface_branch_metadata(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    branch = next(
        (
            item
            for item in payload.get("branches", [])
            if isinstance(item, dict) and item.get("branch_key") == "3.4"
        ),
        None,
    )
    if isinstance(branch, dict) and "承担不明时保持部分结果" in str(
        branch.get("rule_text", "")
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0041_surface_branch_metadata.json"),
        source_label="migration:0041_surface_branch_metadata",
    )


def apply_tube_stock_cut_route(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key"): item
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    rules = {
        item.get("rule_key")
        for item in payload.get("rules", [])
        if isinstance(item, dict)
    }
    route_family = facts.get("route_family", {})
    if (
        "304方管80×50×5"
        in str(facts.get("raw_form", {}).get("judgement_definition", ""))
        and "矩形方管"
        in str(
            facts.get("continuous_revolved_surface", {}).get("judgement_definition", "")
        )
        and "tube_cut_part" in route_family.get("allowed_values", [])
        and {"plain_tube_cut_family", "tube_saw_blanking"} <= rules
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0042_tube_stock_cut_route.json"),
        source_label="migration:0042_tube_stock_cut_route",
    )


def apply_compact_axisymmetric_turning(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    return _apply_upgrade(
        connection,
        tree_key=tree_key,
        data_name="0043_compact_axisymmetric_turning.json",
        required_facts={"single_axis_external_cylindrical_profile"},
        required_rules={"compact_axisymmetric_bar_turning"},
        source_label="migration:0043_compact_axisymmetric_turning",
    )


def apply_cylindrical_projection_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    facts = {
        item.get("fact_key"): item
        for item in payload.get("facts", [])
        if isinstance(item, dict)
    }
    raw_form = facts.get("raw_form", {})
    if (
        "轴向投影不是圆形" in str(raw_form.get("judgement_definition", ""))
        or "不得只检查最大横向尺寸" in str(raw_form.get("coverage_requirement", ""))
    ) and "简单圆柱在轴向主视图" in str(
        facts.get("single_axis_external_cylindrical_profile", {}).get(
            "judgement_definition", ""
        )
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0044_cylindrical_projection_guard.json"),
        source_label="migration:0044_cylindrical_projection_guard",
    )


def apply_stepped_bar_evidence_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    raw_form = next(
        (
            item
            for item in payload.get("facts", [])
            if isinstance(item, dict) and item.get("fact_key") == "raw_form"
        ),
        None,
    )
    if isinstance(raw_form, dict) and "不得只检查最大横向尺寸" in str(
        raw_form.get("coverage_requirement", "")
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0045_stepped_bar_evidence_guard.json"),
        source_label="migration:0045_stepped_bar_evidence_guard",
    )


def apply_preserve_tube_stock_guard(
    connection: Connection,
    *,
    tree_key: str = DEFAULT_TREE_KEY,
) -> TreeUpdateSummary | None:
    payload = _active_source_payload(connection, tree_key)
    if payload is None:
        return None
    raw_form = next(
        (
            item
            for item in payload.get("facts", [])
            if isinstance(item, dict) and item.get("fact_key") == "raw_form"
        ),
        None,
    )
    if (
        isinstance(raw_form, dict)
        and "304方管80×50×5" in str(raw_form.get("judgement_definition", ""))
        and "不得只检查最大横向尺寸" in str(raw_form.get("coverage_requirement", ""))
    ):
        return None
    return apply_tree_patch_model(
        connection,
        _upgrade_patch(tree_key, "0046_preserve_tube_stock_guard.json"),
        source_label="migration:0046_preserve_tube_stock_guard",
    )
