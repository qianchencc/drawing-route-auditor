import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from drawing_route_auditor.decision_tree.definition import load_tree_definition


SOURCE = Path("docs/decision_tree.json")


def test_tree_definition_is_the_single_reader_fact_rule_source() -> None:
    definition = load_tree_definition(SOURCE)

    assert definition.tree_key == "drawing-process-tree"
    assert [
        item.reader_key
        for item in sorted(definition.readers, key=lambda item: item.sequence)
    ] == [
        "document_structure_reader",
        "geometry_dimension_reader",
        "geometry_feature_reader",
        "symbol_relation_reader",
        "requirement_annotation_reader",
        "surface_texture_reader",
    ]
    fact_keys = {item.fact_key for item in definition.facts}
    assert {
        clause.fact_key for rule in definition.rules for clause in rule.clauses
    } <= fact_keys
    observed = [
        item for item in definition.facts if item.source_kind == "observed_drawing"
    ]
    assert observed
    assert all(item.reader_key is not None for item in observed)
    assert all(
        item.reader_key is None
        for item in definition.facts
        if item.source_kind != "observed_drawing"
    )


def test_observed_fact_requires_registered_reader(tmp_path: Path) -> None:
    payload = SOURCE.read_text(encoding="utf-8")
    invalid = payload.replace(
        '"reader_key": "document_structure_reader"',
        '"reader_key": "missing_reader"',
        1,
    )
    path = tmp_path / "invalid.json"
    path.write_text(invalid, encoding="utf-8")

    with pytest.raises(ValidationError, match="已注册读取器"):
        load_tree_definition(path)


def test_rule_output_must_match_derived_fact_contract(tmp_path: Path) -> None:
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    rule = next(
        item
        for item in payload["rules"]
        if item["rule_key"] == "object_component_by_bom"
    )
    rule["outcome_value"] = "unknown-kind"
    path = tmp_path / "invalid-outcome.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="不在允许范围内"):
        load_tree_definition(path)
