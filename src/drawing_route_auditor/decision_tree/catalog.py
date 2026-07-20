from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FactSeed:
    key: str
    value_type: str
    description: str


@dataclass(frozen=True, slots=True)
class ClauseSeed:
    fact_key: str
    operator: str
    expected: Any


@dataclass(frozen=True, slots=True)
class RuleSeed:
    branch_key: str
    rule_key: str
    description: str
    result_kind: str
    outcome_type: str
    outcome_key: str
    outcome_value: Any
    clauses: tuple[ClauseSeed, ...]
    missing_behavior: str = "error"
    priority: int = 0


FACTS = (
    FactSeed("drawing_number", "text", "标题栏中的完整图号"),
    FactSeed("drawing_number_numeric_prefix", "text", "去除前置字母后的图号数字前缀"),
    FactSeed("object_has_bom", "boolean", "当前图纸是否存在明细栏或 BOM"),
    FactSeed("object_kind", "text", "当前对象为零件或部件"),
    FactSeed("component_kind", "text", "部件为焊接件、装配件或其他类型"),
    FactSeed("title_contains_welding", "boolean", "标题栏名称是否包含焊接"),
    FactSeed("weld_symbol_present", "boolean", "图纸是否存在焊接符号"),
    FactSeed("weld_annotation_present", "boolean", "图纸是否存在焊缝标注"),
    FactSeed("technical_requirement_mentions_welding", "boolean", "技术要求是否提及焊接"),
    FactSeed("technical_requirement_mentions_assembly", "boolean", "技术要求是否提及装配"),
    FactSeed("parent_part_type", "text", "上级对象类型"),
    FactSeed("is_plate_part", "boolean", "当前零件是否为板件"),
    FactSeed("has_bend_feature", "boolean", "当前零件是否存在折弯特征"),
    FactSeed("has_welding_feature", "boolean", "当前零件是否存在焊接特征"),
    FactSeed("requires_precision_machining", "boolean", "当前零件是否需要精加工"),
    FactSeed("raw_form", "text", "原始材料形态，如 tube、bar、casting、forging"),
)


def clause(fact_key: str, operator: str, expected: Any) -> ClauseSeed:
    return ClauseSeed(fact_key, operator, expected)


def rule(
    branch_key: str,
    rule_key: str,
    description: str,
    result_kind: str,
    outcome_type: str,
    outcome_key: str,
    outcome_value: Any,
    *clauses: ClauseSeed,
    missing_behavior: str = "error",
    priority: int = 0,
) -> RuleSeed:
    return RuleSeed(
        branch_key=branch_key,
        rule_key=rule_key,
        description=description,
        result_kind=result_kind,
        outcome_type=outcome_type,
        outcome_key=outcome_key,
        outcome_value=outcome_value,
        clauses=tuple(clauses),
        missing_behavior=missing_behavior,
        priority=priority,
    )


RULES = (
    rule(
        "1.1", "object_component_by_number", "图号数字部分以 50 开头，判为部件",
        "resolved", "fact", "object_kind", "component",
        clause("drawing_number_numeric_prefix", "starts_with", "50"),
    ),
    rule(
        "1.1", "object_part_by_number", "图号数字部分以 80 开头，判为零件",
        "resolved", "fact", "object_kind", "part",
        clause("drawing_number_numeric_prefix", "starts_with", "80"),
    ),
    rule(
        "1.2", "object_component_by_bom", "存在明细栏或 BOM，判为部件",
        "resolved", "fact", "object_kind", "component",
        clause("object_has_bom", "eq", True),
    ),
    rule(
        "1.2", "object_part_without_bom", "不存在明细栏或 BOM，判为零件",
        "resolved", "fact", "object_kind", "part",
        clause("object_has_bom", "eq", False),
    ),
    rule(
        "2.1", "welded_component_by_title", "标题栏名称包含焊接，判为焊接件",
        "resolved", "fact", "component_kind", "welded",
        clause("title_contains_welding", "eq", True),
    ),
    rule(
        "2.1", "component_kind_unknown_without_title", "名称未出现焊接时无法单独确定部件类型",
        "error", "error", "component_kind_undetermined", None,
        clause("title_contains_welding", "eq", False),
    ),
    rule(
        "2.2", "welded_component_by_symbol_and_annotation", "焊接符号和焊缝标注同时存在，判为焊接件",
        "resolved", "fact", "component_kind", "welded",
        clause("weld_symbol_present", "eq", True),
        clause("weld_annotation_present", "eq", True),
    ),
    rule(
        "2.2", "welded_component_by_annotation_candidate", "仅有焊缝标注时，焊接件作为候选",
        "candidate", "fact", "component_kind", "welded",
        clause("weld_symbol_present", "eq", False),
        clause("weld_annotation_present", "eq", True),
    ),
    rule(
        "2.3", "welded_component_by_requirement_candidate", "技术要求提及焊接，焊接件作为候选",
        "candidate", "fact", "component_kind", "welded",
        clause("technical_requirement_mentions_welding", "eq", True),
    ),
    rule(
        "2.3", "assembly_component_by_requirement_candidate", "技术要求提及装配，装配件作为候选",
        "candidate", "fact", "component_kind", "assembly",
        clause("technical_requirement_mentions_assembly", "eq", True),
    ),
    rule(
        "3.1", "welded_component_first_operation", "焊接件首道工序为焊接(校正)",
        "resolved", "process", "weld_first_operation",
        {"process": "焊接(校正)", "position": "first"},
        clause("component_kind", "eq", "welded"),
    ),
    rule(
        "3.3", "transfer_welded_to_assembly", "上级为总装件时转装配",
        "resolved", "process", "transfer_to_assembly", {"process": "转装配"},
        clause("component_kind", "eq", "welded"),
        clause("parent_part_type", "eq", "final_assembly"),
    ),
    rule(
        "3.3", "transfer_welded_to_subassembly", "上级为部装件时转部装",
        "resolved", "process", "transfer_to_subassembly", {"process": "转部装"},
        clause("component_kind", "eq", "welded"),
        clause("parent_part_type", "eq", "sub_assembly"),
    ),
    rule(
        "3.3", "transfer_welded_to_welding", "上级为焊接件时转焊接",
        "resolved", "process", "transfer_to_welding", {"process": "转焊接"},
        clause("component_kind", "eq", "welded"),
        clause("parent_part_type", "eq", "welded"),
    ),
    rule(
        "4.1", "final_assembly_first_operation", "AZ_ 开头的装配件以装配为首工序",
        "resolved", "process", "assembly_first_operation",
        {"process": "装配", "position": "first"},
        clause("component_kind", "eq", "assembly"),
        clause("drawing_number", "starts_with", "AZ_"),
    ),
    rule(
        "4.1", "subassembly_first_operation", "非 AZ_ 开头的装配件以部装为首工序",
        "resolved", "process", "subassembly_first_operation",
        {"process": "部装", "position": "first"},
        clause("component_kind", "eq", "assembly"),
        clause("drawing_number", "not_starts_with", "AZ_"),
    ),
    rule(
        "4.2", "transfer_assembly_to_assembly", "上级为总装件时转装配",
        "resolved", "process", "transfer_to_assembly", {"process": "转装配"},
        clause("component_kind", "eq", "assembly"),
        clause("parent_part_type", "eq", "final_assembly"),
    ),
    rule(
        "4.2", "transfer_assembly_to_subassembly", "上级为部装件时转部装",
        "resolved", "process", "transfer_to_subassembly", {"process": "转部装"},
        clause("component_kind", "eq", "assembly"),
        clause("parent_part_type", "eq", "sub_assembly"),
    ),
    rule(
        "4.2", "transfer_assembly_to_welding", "上级为焊接件时转焊接",
        "resolved", "process", "transfer_to_welding", {"process": "转焊接"},
        clause("component_kind", "eq", "assembly"),
        clause("parent_part_type", "eq", "welded"),
    ),
    rule(
        "5.1", "flat_cut_plate_family", "板件无折弯且无精工特征时选择平板下料路线族",
        "candidate", "route_family", "flat_cut_plate_part", None,
        clause("object_kind", "eq", "part"),
        clause("is_plate_part", "eq", True),
        clause("has_bend_feature", "eq", False),
        clause("requires_precision_machining", "eq", False),
    ),
    rule(
        "5.2", "bent_sheet_family", "板件存在折弯特征时选择折弯板件路线族",
        "candidate", "route_family", "bent_sheet_part", None,
        clause("object_kind", "eq", "part"),
        clause("is_plate_part", "eq", True),
        clause("has_bend_feature", "eq", True),
    ),
    rule(
        "5.3", "welded_sheet_family", "板件存在焊接特征时选择焊接板件路线族",
        "candidate", "route_family", "welded_sheet_part", None,
        clause("object_kind", "eq", "part"),
        clause("is_plate_part", "eq", True),
        clause("has_welding_feature", "eq", True),
    ),
    rule(
        "5.4", "machined_plate_family", "板件需要精加工时选择精加工板件路线族",
        "candidate", "route_family", "machined_plate_part", None,
        clause("object_kind", "eq", "part"),
        clause("is_plate_part", "eq", True),
        clause("requires_precision_machining", "eq", True),
    ),
    rule(
        "5.5", "non_sheet_machined_family", "管、棒、铸件或锻件需要精加工时选择非板类精加工路线族",
        "candidate", "route_family", "non_sheet_machined_part", None,
        clause("object_kind", "eq", "part"),
        clause("raw_form", "in", ["tube", "bar", "casting", "forging"]),
        clause("requires_precision_machining", "eq", True),
    ),
)

EXECUTABLE_BRANCH_KEYS = frozenset(rule.branch_key for rule in RULES)
