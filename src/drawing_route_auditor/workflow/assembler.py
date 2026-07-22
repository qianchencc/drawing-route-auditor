from __future__ import annotations

from hashlib import sha256
from itertools import product
import json

from drawing_route_auditor.decision_tree.runtime import EvaluationScenario
from drawing_route_auditor.workflow.models import (
    DecisionFact,
    FactObservation,
    EvidenceRef,
    LocalIssue,
    OperationDecision,
    ReaderExecution,
    RouteCandidate,
    RouteOperation,
    RouteRecommendation,
    RuleMatch,
)


def collect_fact_observations(
    executions: tuple[ReaderExecution, ...],
    additional: tuple[FactObservation, ...] = (),
) -> dict[str, list[FactObservation]]:
    collected: dict[str, list[FactObservation]] = {}
    for execution in executions:
        if execution.response is None:
            continue
        for observation in execution.response.observations:
            collected.setdefault(observation.fact_key, []).append(observation)
    for observation in additional:
        collected.setdefault(observation.fact_key, []).append(observation)
    return collected


def collect_fact_evidence(
    executions: tuple[ReaderExecution, ...],
    additional: tuple[FactObservation, ...] = (),
) -> dict[str, list[EvidenceRef]]:
    collected: dict[str, list[EvidenceRef]] = {}
    observations = collect_fact_observations(executions, additional)
    for fact_key, fact_observations in observations.items():
        bucket = collected.setdefault(fact_key, [])
        known = {
            (item.source_type, item.page, item.region, item.text) for item in bucket
        }
        for observation in fact_observations:
            for evidence in observation.evidence:
                signature = (
                    evidence.source_type,
                    evidence.page,
                    evidence.region,
                    evidence.text,
                )
                if signature not in known:
                    bucket.append(evidence)
                    known.add(signature)
    return collected


def _process_spec(match: RuleMatch) -> dict[str, object] | None:
    if match.outcome_type != "process":
        return None
    if not isinstance(match.outcome_value, dict):
        return None
    process_name = match.outcome_value.get("process_name")
    operation_key = match.outcome_value.get("operation_key")
    order_rank = match.outcome_value.get("order_rank")
    if not isinstance(process_name, str) or not process_name:
        return None
    if not isinstance(operation_key, str) or not operation_key:
        return None
    if not isinstance(order_rank, int):
        return None
    return {
        "operation_key": operation_key,
        "process_name": process_name,
        "order_rank": order_rank,
    }


def _fact_value(facts: dict[str, object], fact_key: str) -> object | None:
    raw = facts.get(fact_key)
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


def _fact_status(facts: dict[str, object], fact_key: str) -> str:
    raw = facts.get(fact_key)
    if isinstance(raw, dict):
        status = raw.get("status")
        if status in {"hit", "not_hit", "unable_to_judge", "conflict"}:
            return str(status)
    return "hit" if fact_key in facts else "unable_to_judge"


def _leaf_fact_keys(
    fact_keys: list[str],
    *,
    facts: dict[str, object],
    matches: tuple[RuleMatch, ...],
    selected_fact_options: tuple[RuleMatch, ...],
    seen: frozenset[str] = frozenset(),
) -> list[str]:
    leaves: list[str] = []
    for fact_key in fact_keys:
        if fact_key in seen:
            continue
        value = _fact_value(facts, fact_key)
        selected_producers = [
            match
            for match in selected_fact_options
            if match.outcome_type == "fact"
            and match.outcome_key == fact_key
            and match.outcome_value == value
        ]
        resolved_producers = [
            match
            for match in matches
            if match.result_status == "resolved"
            and match.outcome_type == "fact"
            and match.outcome_key == fact_key
            and match.outcome_value == value
        ]
        producers = selected_producers
        if not producers and resolved_producers:
            priority = max(match.priority for match in resolved_producers)
            producers = [
                match for match in resolved_producers if match.priority == priority
            ]
        if not producers:
            leaves.append(fact_key)
            continue
        for producer in producers:
            leaves.extend(
                _leaf_fact_keys(
                    producer.decisive_facts,
                    facts=facts,
                    matches=matches,
                    selected_fact_options=selected_fact_options,
                    seen=seen | {fact_key},
                )
            )
    return list(dict.fromkeys(leaves))


def _operation_decision(
    match: RuleMatch,
    options: list[RuleMatch],
    *,
    scenario: EvaluationScenario,
    evidence_by_fact: dict[str, list[EvidenceRef]],
    observations_by_fact: dict[str, list[FactObservation]],
    fact_labels: dict[str, str],
    tree_revision: int,
) -> OperationDecision:
    leaf_keys = _leaf_fact_keys(
        match.decisive_facts,
        facts=scenario.facts,
        matches=scenario.matches,
        selected_fact_options=scenario.selected_fact_options,
    )
    return OperationDecision(
        rule_key=match.rule_key,
        decision_key=match.decision_key,
        question=match.question,
        selected_option=match.option_label,
        alternative_options=sorted(
            {
                option.option_label
                for option in options
                if option.option_key != match.option_key
            }
        ),
        result_status=match.result_status,
        reason=match.reason,
        missing_facts=match.missing_facts,
        decisive_facts=[
            DecisionFact(
                fact_key=fact_key,
                label=fact_labels.get(fact_key, fact_key),
                status=_fact_status(scenario.facts, fact_key),
                value=_fact_value(scenario.facts, fact_key),
                evidence=evidence_by_fact.get(fact_key, []),
                subject_observations=observations_by_fact.get(fact_key, []),
            )
            for fact_key in leaf_keys
        ],
        rule_revision=tree_revision,
    )


def _ordered_operations(
    matches: list[RuleMatch],
    *,
    scenario: EvaluationScenario,
    candidate_groups: dict[str, list[RuleMatch]],
    evidence_by_fact: dict[str, list[EvidenceRef]],
    observations_by_fact: dict[str, list[FactObservation]],
    fact_labels: dict[str, str],
    tree_revision: int,
) -> list[RouteOperation]:
    specs: dict[tuple[str, str], dict[str, object]] = {}
    for match in matches:
        spec = _process_spec(match)
        if spec is None:
            continue
        key = (str(spec["operation_key"]), str(spec["process_name"]))
        existing = specs.setdefault(
            key,
            {
                **spec,
                "matches": [],
            },
        )
        matched_rules = existing["matches"]
        if isinstance(matched_rules, list):
            matched_rules.append(match)
    ordered = sorted(
        specs.values(),
        key=lambda item: (
            int(item["order_rank"]),
            str(item["operation_key"]),
            str(item["process_name"]),
        ),
    )
    operations: list[RouteOperation] = []
    for sequence, item in enumerate(ordered, start=1):
        matched_rules = item["matches"]
        if not isinstance(matched_rules, list):
            continue
        decisions = [
            _operation_decision(
                match,
                candidate_groups.get(match.decision_key, [match]),
                scenario=scenario,
                evidence_by_fact=evidence_by_fact,
                observations_by_fact=observations_by_fact,
                fact_labels=fact_labels,
                tree_revision=tree_revision,
            )
            for match in matched_rules
        ]
        operations.append(
            RouteOperation(
                sequence=sequence,
                operation_key=str(item["operation_key"]),
                process_name=str(item["process_name"]),
                source_rule_keys=sorted({match.rule_key for match in matched_rules}),
                decisions=decisions,
            )
        )
    return operations


def _deduplicate_issues(issues: list[LocalIssue]) -> list[LocalIssue]:
    unique: dict[tuple[object, ...], LocalIssue] = {}
    for issue in issues:
        key = (
            issue.kind,
            issue.code,
            issue.location,
            issue.message,
            tuple(issue.missing_facts),
        )
        unique.setdefault(key, issue)
    return list(unique.values())


def assemble_recommendation(
    scenarios: tuple[EvaluationScenario, ...],
    *,
    tree_revision: int,
    evidence_by_fact: dict[str, list[EvidenceRef]] | None = None,
    observations_by_fact: dict[str, list[FactObservation]] | None = None,
    fact_labels: dict[str, str] | None = None,
) -> RouteRecommendation:
    evidence = evidence_by_fact or {}
    observations = observations_by_fact or {}
    labels = fact_labels or {}
    all_issues: list[LocalIssue] = []
    clean_candidates: list[RouteCandidate] = []
    partial_routes: list[list[RouteOperation]] = []

    for scenario in scenarios:
        scenario_issues = list(scenario.issues)
        resolved_processes = [
            match
            for match in scenario.matches
            if match.result_status == "resolved" and match.outcome_type == "process"
        ]
        candidate_groups: dict[str, list[RuleMatch]] = {}
        for match in scenario.matches:
            if match.result_status == "candidate" and match.outcome_type == "process":
                candidate_groups.setdefault(
                    match.decision_key,
                    [],
                ).append(match)

        complete_groups: list[list[RuleMatch]] = []
        for decision_key, options in sorted(candidate_groups.items()):
            distinct = {
                (
                    option.option_key,
                    json.dumps(
                        option.outcome_value,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ): option
                for option in options
            }
            option_list = list(distinct.values())
            if len(option_list) < 2:
                scenario_issues.append(
                    LocalIssue(
                        kind="error",
                        code="INCOMPLETE_PROCESS_CANDIDATE_SET",
                        location=decision_key,
                        message="工艺候选没有形成有限且完整的选项集合",
                        missing_facts=(
                            option_list[0].decisive_facts if option_list else []
                        ),
                    )
                )
                continue
            complete_groups.append(option_list)

        partial_routes.append(
            _ordered_operations(
                resolved_processes,
                scenario=scenario,
                candidate_groups=candidate_groups,
                evidence_by_fact=evidence,
                observations_by_fact=observations,
                fact_labels=labels,
                tree_revision=tree_revision,
            )
        )
        all_issues.extend(scenario_issues)
        if scenario_issues:
            continue

        combinations = product(*complete_groups) if complete_groups else [()]
        for combination in combinations:
            selected_processes = list(combination)
            operations = _ordered_operations(
                [*resolved_processes, *selected_processes],
                scenario=scenario,
                candidate_groups=candidate_groups,
                evidence_by_fact=evidence,
                observations_by_fact=observations,
                fact_labels=labels,
                tree_revision=tree_revision,
            )
            if not operations:
                continue
            signature = json.dumps(
                [item.process_name for item in operations],
                ensure_ascii=False,
            )
            candidate_id = sha256(signature.encode("utf-8")).hexdigest()[:16]
            clean_candidates.append(
                RouteCandidate(
                    route_candidate_id=candidate_id,
                    operations=operations,
                )
            )

    candidates_by_sequence: dict[tuple[str, ...], RouteCandidate] = {}
    for candidate in clean_candidates:
        sequence = tuple(operation.process_name for operation in candidate.operations)
        candidates_by_sequence.setdefault(sequence, candidate)
    candidates = list(candidates_by_sequence.values())
    issues = _deduplicate_issues(all_issues)

    if issues:
        partial = max(partial_routes, key=len, default=[])
        status = "partial" if partial else "error"
        return RouteRecommendation(
            status=status,
            route=partial or None,
            route_candidates=[],
            local_issues=issues,
        )
    if len(candidates) > 1:
        return RouteRecommendation(
            status="complete_with_candidates",
            route=None,
            route_candidates=candidates,
            local_issues=[],
        )
    if len(candidates) == 1:
        return RouteRecommendation(
            status="complete",
            route=candidates[0].operations,
            route_candidates=[],
            local_issues=[],
        )
    return RouteRecommendation(
        status="error",
        route=None,
        route_candidates=[],
        local_issues=[
            LocalIssue(
                kind="error",
                code="NO_ROUTE_RESULT",
                location="route",
                message="决策树没有生成可用工艺路线",
                missing_facts=[],
            )
        ],
    )
