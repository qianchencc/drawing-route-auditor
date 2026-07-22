from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ReaderSourceKind = Literal["observed_drawing", "external", "derived"]
ValueType = Literal["boolean", "text", "number", "text_array"]
SubjectScope = Literal[
    "current_object",
    "bom_item",
    "bom_link",
    "occurrence",
    "drawing_text",
]


def fact_value_matches(value_type: ValueType, value: object) -> bool:
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "text":
        return isinstance(value, str)
    if value_type == "text_array":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


def validate_fact_value(
    *,
    value_type: ValueType,
    allowed_values: list[str] | None,
    value: object,
    context: str,
) -> None:
    if not fact_value_matches(value_type, value):
        raise ValueError(f"{context} 的值不符合 {value_type} 合同")
    if allowed_values is not None and value not in allowed_values:
        raise ValueError(f"{context} 的值 {value!r} 不在允许范围内")


class ReaderDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reader_key: str
    label: str
    capability_definition: str
    sequence: int = Field(gt=0)


class FactDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_key: str
    source_kind: ReaderSourceKind
    reader_key: str | None = None
    subject_scope: SubjectScope
    value_type: ValueType
    allowed_values: list[str] | None = None
    label: str
    description: str
    judgement_definition: str
    hit_criteria: str | None = None
    not_hit_criteria: str | None = None
    coverage_requirement: str | None = None
    evidence_requirement: str | None = None


class NodeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_key: str
    title: str
    node_kind: Literal["classification", "route_generation", "calculation"]
    maintenance_status: Literal["complete", "needs_review", "incomplete"]
    sequence: int = Field(gt=0)
    route_required: bool = False


class BranchDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_key: str
    node_key: str
    title: str
    rule_text: str
    maintenance_status: Literal["executable", "needs_review", "incomplete"]
    confidence_mode: Literal["certain", "candidate", "unknown"]
    priority: int = 0


class ClauseDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_key: str
    operator: Literal[
        "eq",
        "neq",
        "starts_with",
        "not_starts_with",
        "contains",
        "in",
        "lt",
        "lte",
        "gt",
        "gte",
    ]
    expected_value: object


class RuleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_key: str
    rule_key: str
    description: str
    decision_key: str
    question: str
    option_key: str
    option_label: str
    evaluation_mode: Literal["all", "any"] = "all"
    result_kind: Literal["resolved", "candidate", "error"]
    outcome_type: Literal["fact", "route_family", "process", "stage", "error"]
    outcome_key: str
    outcome_value: object | None = None
    missing_behavior: Literal["error", "candidate", "not_match"] = "error"
    priority: int = 0
    clauses: list[ClauseDefinition]


class EdgeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_node_key: str | None = None
    from_branch_key: str | None = None
    to_node_key: str
    edge_kind: Literal["root", "node", "branch"]
    predecessor_ref: str
    resolution_status: Literal["resolved", "ambiguous", "unresolved"] = "resolved"
    reason: str | None = None


def _validate_fact_sources(
    facts: list[FactDefinition],
    readers: set[str],
) -> None:
    for fact in facts:
        if fact.source_kind == "observed_drawing":
            if fact.reader_key not in readers:
                raise ValueError(
                    f"图纸观察事实 {fact.fact_key!r} 必须指定一个已注册读取器"
                )
        elif fact.reader_key is not None:
            raise ValueError(f"非图纸观察事实 {fact.fact_key!r} 不能指定读取器")


def _validate_rule_references(
    rule: RuleDefinition,
    branches: set[str],
    facts: dict[str, FactDefinition],
) -> None:
    if rule.branch_key not in branches:
        raise ValueError(f"规则 {rule.rule_key!r} 引用了未知分支")
    missing = {item.fact_key for item in rule.clauses} - set(facts)
    if missing:
        raise ValueError(f"规则 {rule.rule_key!r} 引用了未知事实：{sorted(missing)}")
    if rule.outcome_type != "fact":
        return
    target = facts.get(rule.outcome_key)
    if target is None:
        raise ValueError(f"规则 {rule.rule_key!r} 产出未知事实 {rule.outcome_key!r}")
    if target.source_kind != "derived":
        raise ValueError(
            f"规则 {rule.rule_key!r} 只能产出派生事实 {rule.outcome_key!r}"
        )
    validate_fact_value(
        value_type=target.value_type,
        allowed_values=target.allowed_values,
        value=rule.outcome_value,
        context=f"规则 {rule.rule_key!r} 产出事实 {rule.outcome_key!r}",
    )


def _validate_edge_references(
    edge: EdgeDefinition,
    nodes: set[str],
    branches: set[str],
) -> None:
    if edge.to_node_key not in nodes:
        raise ValueError(f"边指向未知节点 {edge.to_node_key!r}")
    if edge.from_node_key is not None and edge.from_node_key not in nodes:
        raise ValueError(f"边引用了未知来源节点 {edge.from_node_key!r}")
    if edge.from_branch_key is not None and edge.from_branch_key not in branches:
        raise ValueError(f"边引用了未知来源分支 {edge.from_branch_key!r}")


class DecisionTreeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=2)
    tree_key: str
    name: str
    description: str | None = None
    readers: list[ReaderDefinition]
    facts: list[FactDefinition]
    nodes: list[NodeDefinition]
    branches: list[BranchDefinition]
    rules: list[RuleDefinition]
    edges: list[EdgeDefinition]

    @model_validator(mode="after")
    def validate_references(self) -> DecisionTreeDefinition:
        reader_keys = [item.reader_key for item in self.readers]
        if len(reader_keys) != len(set(reader_keys)):
            raise ValueError("reader_key 必须唯一")
        fact_keys = [item.fact_key for item in self.facts]
        if len(fact_keys) != len(set(fact_keys)):
            raise ValueError("fact_key 必须唯一")
        node_keys = [item.node_key for item in self.nodes]
        if len(node_keys) != len(set(node_keys)):
            raise ValueError("node_key 必须唯一")
        branch_keys = [item.branch_key for item in self.branches]
        if len(branch_keys) != len(set(branch_keys)):
            raise ValueError("branch_key 必须唯一")
        rule_keys = [item.rule_key for item in self.rules]
        if len(rule_keys) != len(set(rule_keys)):
            raise ValueError("rule_key 必须唯一")

        readers = set(reader_keys)
        fact_definitions = {item.fact_key: item for item in self.facts}
        nodes = set(node_keys)
        branches = set(branch_keys)
        _validate_fact_sources(self.facts, readers)
        for branch in self.branches:
            if branch.node_key not in nodes:
                raise ValueError(f"分支 {branch.branch_key!r} 引用了未知节点")
        for rule in self.rules:
            _validate_rule_references(rule, branches, fact_definitions)
        for edge in self.edges:
            _validate_edge_references(edge, nodes, branches)
        return self


def load_tree_definition(path: Path) -> DecisionTreeDefinition:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return DecisionTreeDefinition.model_validate(payload)
