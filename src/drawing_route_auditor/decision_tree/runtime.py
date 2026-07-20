from __future__ import annotations

from dataclasses import dataclass
import json

from drawing_route_auditor.db.connection import Connection
from drawing_route_auditor.decision_tree.repository import evaluate_tree
from drawing_route_auditor.workflow.models import (
    FactObservation,
    LocalIssue,
    ReaderExecution,
    ReaderPlan,
    RequestedFeature,
    RuleMatch,
)


@dataclass(frozen=True, slots=True)
class RuntimeTree:
    version_id: int
    tree_key: str
    version: int
    plans: tuple[ReaderPlan, ...]
    external_fact_keys: tuple[str, ...]
    fact_labels: dict[str, str]


@dataclass(frozen=True, slots=True)
class EvaluationScenario:
    facts: dict[str, object]
    matches: tuple[RuleMatch, ...]
    selected_fact_options: tuple[RuleMatch, ...]
    issues: tuple[LocalIssue, ...]


def load_runtime_tree(
    connection: Connection,
    tree_key: str,
    version: int | None = None,
) -> RuntimeTree:
    if version is None:
        version_row = connection.execute(
            """
            SELECT version.id AS version_id, version.version
            FROM decision_trees AS tree
            JOIN decision_tree_versions AS version ON version.tree_id = tree.id
            WHERE tree.tree_key = %s AND version.status = 'active'
            """,
            (tree_key,),
        ).fetchone()
    else:
        version_row = connection.execute(
            """
            SELECT version.id AS version_id, version.version
            FROM decision_trees AS tree
            JOIN decision_tree_versions AS version ON version.tree_id = tree.id
            WHERE tree.tree_key = %s AND version.version = %s
            """,
            (tree_key, version),
        ).fetchone()
    if version_row is None:
        requested = "启用版本" if version is None else f"版本 {version}"
        raise LookupError(f"决策树 {tree_key!r} 没有{requested}")

    rows = connection.execute(
        """
        SELECT
            reader.id AS reader_id,
            reader.reader_key,
            reader.label AS reader_label,
            reader.capability_definition,
            reader.sequence,
            fact.fact_key,
            fact.label AS fact_label,
            fact.subject_scope,
            fact.value_type,
            fact.allowed_values,
            fact.judgement_definition,
            fact.hit_criteria,
            fact.not_hit_criteria,
            fact.coverage_requirement,
            fact.evidence_requirement
        FROM decision_readers AS reader
        LEFT JOIN fact_definitions AS fact
            ON fact.reader_id = reader.id
           AND fact.source_kind = 'observed_drawing'
        WHERE reader.version_id = %s
        ORDER BY reader.sequence, fact.fact_key
        """,
        (version_row["version_id"],),
    ).fetchall()
    fact_rows = connection.execute(
        """
        SELECT fact_key, label, source_kind
        FROM fact_definitions
        WHERE version_id = %s
        ORDER BY fact_key
        """,
        (version_row["version_id"],),
    ).fetchall()
    external_fact_keys = tuple(
        row["fact_key"] for row in fact_rows if row["source_kind"] == "external"
    )
    fact_labels = {row["fact_key"]: row["label"] for row in fact_rows}

    plans_by_key: dict[str, dict[str, object]] = {}
    for row in rows:
        plan = plans_by_key.setdefault(
            row["reader_key"],
            {
                "reader_id": row["reader_id"],
                "reader_key": row["reader_key"],
                "label": row["reader_label"],
                "capability_definition": row["capability_definition"],
                "sequence": row["sequence"],
                "requested_features": [],
            },
        )
        if row["fact_key"] is None:
            continue
        plan["requested_features"].append(
            RequestedFeature(
                fact_key=row["fact_key"],
                label=row["fact_label"],
                subject_scope=row["subject_scope"],
                value_type=row["value_type"],
                allowed_values=row["allowed_values"],
                judgement_definition=row["judgement_definition"],
                hit_criteria=row["hit_criteria"],
                not_hit_criteria=row["not_hit_criteria"],
                coverage_requirement=row["coverage_requirement"],
                evidence_requirement=row["evidence_requirement"],
            )
        )
    plans = tuple(
        ReaderPlan.model_validate(item)
        for item in sorted(
            plans_by_key.values(),
            key=lambda item: int(item["sequence"]),
        )
    )
    if len(plans) != 4:
        raise ValueError(
            f"决策树版本必须定义且仅定义四个读取器；当前为 {len(plans)} 个"
        )
    if any(not plan.requested_features for plan in plans):
        raise ValueError("每个读取器必须负责至少一个图纸观察事实")
    return RuntimeTree(
        version_id=version_row["version_id"],
        tree_key=tree_key,
        version=version_row["version"],
        plans=plans,
        external_fact_keys=external_fact_keys,
        fact_labels=fact_labels,
    )


def observations_to_facts(
    executions: tuple[ReaderExecution, ...],
) -> tuple[dict[str, object], list[LocalIssue]]:
    observations: dict[str, list[FactObservation]] = {}
    issues: list[LocalIssue] = []
    for execution in executions:
        if execution.status == "error" or execution.response is None:
            issues.append(
                LocalIssue(
                    kind="error",
                    code="READER_FAILURE",
                    location=execution.reader_key,
                    message=execution.error_message or "读取器执行失败",
                    missing_facts=[],
                )
            )
            continue
        for observation in execution.response.observations:
            observations.setdefault(observation.fact_key, []).append(observation)

    facts: dict[str, object] = {}
    for fact_key, values in observations.items():
        if len(values) == 1:
            observation = values[0]
            facts[fact_key] = {
                "status": observation.status,
                "value": observation.value,
            }
            continue
        signatures = {
            (
                observation.status,
                json.dumps(
                    observation.value,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            for observation in values
        }
        if len(signatures) == 1:
            observation = values[0]
            facts[fact_key] = {
                "status": observation.status,
                "value": observation.value,
            }
        else:
            facts[fact_key] = {"status": "conflict"}
            issues.append(
                LocalIssue(
                    kind="error",
                    code="SUBJECT_OBSERVATION_CONFLICT",
                    location=fact_key,
                    message="同一 Reader 对该事实返回了冲突对象观察",
                    missing_facts=[fact_key],
                )
            )
    return facts, issues


def evaluate_closure(
    connection: Connection,
    runtime: RuntimeTree,
    initial_facts: dict[str, object],
) -> tuple[dict[str, object], list[RuleMatch], list[LocalIssue]]:
    facts = dict(initial_facts)
    matches_by_rule: dict[str, RuleMatch] = {}
    issues: list[LocalIssue] = []

    while True:
        rows = evaluate_tree(
            connection,
            runtime.tree_key,
            runtime.version,
            facts,
        )
        current_matches: dict[str, RuleMatch] = {}
        for row in rows:
            current_matches[row["rule_key"]] = RuleMatch(
                node_key=row["node_key"],
                branch_key=row["branch_key"],
                rule_key=row["rule_key"],
                decision_key=row["decision_key"],
                question=row["question"],
                option_key=row["option_key"],
                option_label=row["option_label"],
                priority=row["priority"],
                result_status=row["result_status"],
                outcome_type=row["outcome_type"],
                outcome_key=row["outcome_key"],
                outcome_value=row["outcome_value"],
                decisive_facts=list(row["decisive_facts"]),
                reason=row["reason"],
                missing_facts=list(row["missing_facts"]),
            )
        resolved_decisions = {
            match.decision_key
            for match in current_matches.values()
            if match.result_status == "resolved"
        }
        current_matches = {
            rule_key: match
            for rule_key, match in current_matches.items()
            if not (
                match.result_status == "candidate"
                and match.decision_key in resolved_decisions
            )
        }
        matches_by_rule = current_matches

        derived: dict[str, list[RuleMatch]] = {}
        for match in matches_by_rule.values():
            if match.result_status != "resolved" or match.outcome_type != "fact":
                continue
            derived.setdefault(match.outcome_key, []).append(match)

        changed = False
        for fact_key, fact_matches in derived.items():
            highest_priority = max(match.priority for match in fact_matches)
            fact_matches = [
                match for match in fact_matches if match.priority == highest_priority
            ]
            values = {
                json.dumps(
                    match.outcome_value,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                for match in fact_matches
            }
            if len(values) > 1:
                if facts.get(fact_key) != {"status": "conflict"}:
                    facts[fact_key] = {"status": "conflict"}
                    issues.append(
                        LocalIssue(
                            kind="error",
                            code="DERIVED_FACT_CONFLICT",
                            location=fact_key,
                            message="多个已解析规则派生出不同事实值",
                            missing_facts=[fact_key],
                        )
                    )
                    changed = True
                continue
            value = fact_matches[0].outcome_value
            next_fact = {"status": "hit", "value": value}
            if facts.get(fact_key) != next_fact:
                facts[fact_key] = next_fact
                changed = True
        if not changed:
            break

    for match in matches_by_rule.values():
        if match.result_status != "error":
            continue
        issues.append(
            LocalIssue(
                kind="error",
                code="DECISION_FACT_UNRESOLVED",
                location=f"{match.node_key}/{match.branch_key}",
                message=match.reason,
                missing_facts=match.missing_facts,
            )
        )
    return facts, list(matches_by_rule.values()), issues


def evaluate_scenarios(
    connection: Connection,
    runtime: RuntimeTree,
    initial_facts: dict[str, object],
    initial_issues: list[LocalIssue] | None = None,
    *,
    max_scenarios: int = 64,
) -> tuple[EvaluationScenario, ...]:
    prepared_facts = dict(initial_facts)
    for fact_key in runtime.external_fact_keys:
        prepared_facts.setdefault(fact_key, {"status": "unable_to_judge"})

    pending: list[
        tuple[dict[str, object], tuple[RuleMatch, ...], tuple[LocalIssue, ...]]
    ] = [
        (
            prepared_facts,
            (),
            tuple(initial_issues or []),
        )
    ]
    completed: list[EvaluationScenario] = []

    while pending:
        facts, selected, inherited_issues = pending.pop(0)
        final_facts, matches, closure_issues = evaluate_closure(
            connection,
            runtime,
            facts,
        )
        selected_decisions = {item.decision_key for item in selected}
        groups: dict[str, list[RuleMatch]] = {}
        for match in matches:
            if (
                match.result_status == "candidate"
                and match.outcome_type == "fact"
                and match.decision_key not in selected_decisions
            ):
                groups.setdefault(match.decision_key, []).append(match)

        if not groups:
            completed.append(
                EvaluationScenario(
                    facts=final_facts,
                    matches=tuple(matches),
                    selected_fact_options=selected,
                    issues=(*inherited_issues, *closure_issues),
                )
            )
            continue

        decision_key = sorted(groups)[0]
        options_by_value: dict[str, RuleMatch] = {}
        for option in groups[decision_key]:
            signature = json.dumps(
                [option.outcome_key, option.outcome_value],
                ensure_ascii=False,
                sort_keys=True,
            )
            options_by_value.setdefault(signature, option)
        options = list(options_by_value.values())
        if len(options) < 2:
            completed.append(
                EvaluationScenario(
                    facts=final_facts,
                    matches=tuple(matches),
                    selected_fact_options=selected,
                    issues=(
                        *inherited_issues,
                        *closure_issues,
                        LocalIssue(
                            kind="error",
                            code="INCOMPLETE_FACT_CANDIDATE_SET",
                            location=decision_key,
                            message="候选事实没有形成有限且完整的选项集合",
                            missing_facts=options[0].decisive_facts if options else [],
                        ),
                    ),
                )
            )
            continue

        if len(pending) + len(completed) + len(options) > max_scenarios:
            raise ValueError(f"候选场景数量超过上限 {max_scenarios}")
        for option in options:
            branch_facts = dict(final_facts)
            branch_facts[option.outcome_key] = {
                "status": "hit",
                "value": option.outcome_value,
            }
            pending.append(
                (
                    branch_facts,
                    (*selected, option),
                    (*inherited_issues, *closure_issues),
                )
            )

    return tuple(completed)
