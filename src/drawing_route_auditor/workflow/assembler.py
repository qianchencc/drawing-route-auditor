from __future__ import annotations

from collections import defaultdict
import heapq

from drawing_route_auditor.workflow.models import (
    AssembledOperation,
    FactObservation,
    FlowIssue,
    FlowResult,
    RouteConstraint,
    RouteResult,
)


_FLOW_RANK = {
    "blanking": 10,
    "forming": 20,
    "connection": 30,
    "machining": 40,
    "surface_cleaning": 50,
    "transfer": 90,
}


def _global_key(flow_id: str, local_key: str) -> str:
    prefix = f"{flow_id}."
    return local_key if local_key.startswith(prefix) else f"{prefix}{local_key}"


def _enterprise_constraints(
    operations: dict[str, AssembledOperation],
) -> list[RouteConstraint]:
    by_flow: dict[str, list[AssembledOperation]] = defaultdict(list)
    for operation in operations.values():
        by_flow[operation.flow_id].append(operation)

    constraints: list[RouteConstraint] = []

    def connect_flows(before_flow: str, after_flow: str, reason: str) -> None:
        for before in by_flow.get(before_flow, []):
            for after in by_flow.get(after_flow, []):
                constraints.append(
                    RouteConstraint(
                        before_operation=before.operation_key,
                        after_operation=after.operation_key,
                        reason=reason,
                    )
                )

    connect_flows("blanking", "forming", "毛坯下料先于成形")
    connect_flows("blanking", "connection", "毛坯下料先于本级连接")
    connect_flows("blanking", "machining", "毛坯下料先于本级精加工")
    connect_flows("blanking", "surface_cleaning", "毛坯下料先于最终表面处理")
    connect_flows("forming", "connection", "成形先于成形件连接")
    connect_flows("forming", "surface_cleaning", "成形先于最终表面处理")
    connect_flows("connection", "surface_cleaning", "本级连接先于最终表面恢复")
    connect_flows("machining", "surface_cleaning", "本级精加工先于最终表面清洁")

    transfer_operations = by_flow.get("transfer", [])
    for operation in operations.values():
        if operation.flow_id == "transfer":
            continue
        for transfer in transfer_operations:
            constraints.append(
                RouteConstraint(
                    before_operation=operation.operation_key,
                    after_operation=transfer.operation_key,
                    reason="转序必须等待本级制造完成",
                )
            )

    def connect_processes(
        flow_id: str,
        earlier: tuple[str, ...],
        later: tuple[str, ...],
        reason: str,
    ) -> None:
        first = [
            item
            for item in by_flow.get(flow_id, [])
            if any(marker in item.process for marker in earlier)
        ]
        second = [
            item
            for item in by_flow.get(flow_id, [])
            if any(marker in item.process for marker in later)
        ]
        for before in first:
            for after in second:
                if before.operation_key != after.operation_key:
                    constraints.append(
                        RouteConstraint(
                            before_operation=before.operation_key,
                            after_operation=after.operation_key,
                            reason=reason,
                        )
                    )

    connect_processes(
        "forming",
        ("卷圆", "折弯", "冲压", "翻边"),
        ("校形",),
        "主体成形先于校形",
    )
    connect_processes(
        "surface_cleaning",
        ("去角", "去毛刺", "倒钝"),
        ("抛光", "拉丝", "镜面"),
        "边缘处理先于表面精整",
    )
    connect_processes(
        "surface_cleaning",
        ("抛光", "拉丝", "镜面"),
        ("清洗",),
        "表面精整先于最终清洁",
    )
    return constraints


def _fact_conflict_issues(
    flow_results: tuple[FlowResult, ...],
    operations: dict[str, AssembledOperation],
) -> list[FlowIssue]:
    observations: dict[
        tuple[str, str], list[tuple[str, FactObservation]]
    ] = defaultdict(list)
    for result in flow_results:
        for observation in result.observations:
            observations[(observation.fact_key, observation.subject_ref)].append(
                (result.flow_id, observation)
            )

    conflicts: list[FlowIssue] = []
    for (fact_key, subject_ref), entries in observations.items():
        hit_values = {
            str(observation.value).strip().lower()
            for _, observation in entries
            if observation.status == "hit"
        }
        has_not_hit = any(
            observation.status == "not_hit" for _, observation in entries
        )
        if len(hit_values) <= 1 and not (hit_values and has_not_hit):
            continue
        source_flows = {flow_id for flow_id, _ in entries}
        affected = [
            operation.operation_key
            for operation in operations.values()
            if operation.flow_id in source_flows
        ]
        observed = [
            f"{flow_id}:{observation.status}={observation.value!r}"
            for flow_id, observation in entries
        ]
        conflicts.append(
            FlowIssue(
                kind="error",
                code="FACT_OBSERVATION_CONFLICT",
                message=(
                    f"事实 {fact_key!r} 在对象 {subject_ref!r} 上冲突："
                    + "; ".join(observed)
                ),
                affected_operation_keys=affected,
                missing_facts=[fact_key],
                candidate_options=[],
            )
        )
    return conflicts


def assemble_route(flow_results: tuple[FlowResult, ...]) -> RouteResult:
    operations: dict[str, AssembledOperation] = {}
    insertion_index: dict[str, int] = {}
    issues: list[FlowIssue] = []

    for result in flow_results:
        issues.extend(
            issue.model_copy(
                update={
                    "affected_operation_keys": [
                        _global_key(result.flow_id, key)
                        for key in issue.affected_operation_keys
                    ]
                }
            )
            for issue in result.issues
        )
        observation_keys = [item.fact_key for item in result.observations]
        for reader_operation in result.operations:
            operation_key = _global_key(result.flow_id, reader_operation.operation_key)
            if operation_key in operations:
                issues.append(
                    FlowIssue(
                        kind="error",
                        code="DUPLICATE_OPERATION_INSTANCE",
                        message=f"重复工序实例：{operation_key}",
                        affected_operation_keys=[operation_key],
                        missing_facts=[],
                        candidate_options=[],
                    )
                )
                continue
            insertion_index[operation_key] = len(insertion_index)
            operations[operation_key] = AssembledOperation(
                operation_key=operation_key,
                flow_id=result.flow_id,
                process=reader_operation.process,
                content=reader_operation.content,
                targets=reader_operation.targets,
                necessity_status=reader_operation.necessity_status,
                execution_state=reader_operation.execution_state,
                blocked_by=list(reader_operation.blocked_by),
                lineage={
                    "source_flow": result.flow_id,
                    "source_fact_observations": observation_keys,
                },
            )

    issues.extend(_fact_conflict_issues(flow_results, operations))

    constraints = _enterprise_constraints(operations)
    enterprise_edges = {
        (item.before_operation, item.after_operation) for item in constraints
    }
    for result in flow_results:
        for source in result.constraints:
            constraint = RouteConstraint(
                before_operation=_global_key(result.flow_id, source.before_operation),
                after_operation=_global_key(result.flow_id, source.after_operation),
                reason=source.reason,
            )
            edge = (constraint.before_operation, constraint.after_operation)
            reverse = (constraint.after_operation, constraint.before_operation)
            if edge[0] not in operations or edge[1] not in operations:
                issues.append(
                    FlowIssue(
                        kind="error",
                        code="CONSTRAINT_TARGET_MISSING",
                        message=f"约束引用未知工序：{edge[0]} -> {edge[1]}",
                        affected_operation_keys=[key for key in edge if key in operations],
                        missing_facts=[],
                        candidate_options=[],
                    )
                )
                continue
            if reverse in enterprise_edges:
                issues.append(
                    FlowIssue(
                        kind="error",
                        code="CONSTRAINT_CONFLICT",
                        message=f"Reader 约束与企业约束冲突：{edge[0]} -> {edge[1]}",
                        affected_operation_keys=list(edge),
                        missing_facts=[],
                        candidate_options=[],
                    )
                )
                continue
            constraints.append(constraint)

    deduplicated: dict[tuple[str, str], RouteConstraint] = {}
    for constraint in constraints:
        deduplicated.setdefault(
            (constraint.before_operation, constraint.after_operation),
            constraint,
        )
    constraints = list(deduplicated.values())

    descendants: dict[str, set[str]] = defaultdict(set)
    indegree = {key: 0 for key in operations}
    for constraint in constraints:
        before = constraint.before_operation
        after = constraint.after_operation
        if before == after:
            continue
        if after not in descendants[before]:
            descendants[before].add(after)
            indegree[after] += 1

    ready: list[tuple[int, int, str]] = []
    for key, degree in indegree.items():
        if degree == 0:
            operation = operations[key]
            heapq.heappush(
                ready,
                (_FLOW_RANK.get(operation.flow_id, 80), insertion_index[key], key),
            )

    ordered_keys: list[str] = []
    while ready:
        _, _, key = heapq.heappop(ready)
        ordered_keys.append(key)
        for child in sorted(descendants[key], key=insertion_index.get):
            indegree[child] -= 1
            if indegree[child] == 0:
                operation = operations[child]
                heapq.heappush(
                    ready,
                    (
                        _FLOW_RANK.get(operation.flow_id, 80),
                        insertion_index[child],
                        child,
                    ),
                )

    cycle_keys = [key for key, degree in indegree.items() if degree > 0]
    if cycle_keys:
        issues.append(
            FlowIssue(
                kind="error",
                code="HARD_CONSTRAINT_CYCLE",
                message="硬约束图存在环，环内及依赖后继不可提交",
                affected_operation_keys=cycle_keys,
                missing_facts=[],
                candidate_options=[],
            )
        )
        ordered_keys.extend(sorted(cycle_keys, key=insertion_index.get))
        for key in cycle_keys:
            operations[key] = operations[key].model_copy(
                update={
                    "execution_state": "invalid",
                    "blocked_by": [*operations[key].blocked_by, "HARD_CONSTRAINT_CYCLE"],
                }
            )

    affected_by_issue = {
        key
        for issue in issues
        for key in issue.affected_operation_keys
        if key in operations
    }
    for key in affected_by_issue:
        operation = operations[key]
        if operation.execution_state == "ready":
            operations[key] = operation.model_copy(
                update={
                    "execution_state": "blocked",
                    "blocked_by": [*operation.blocked_by, "UNRESOLVED_ROUTE_ISSUE"],
                }
            )

    changed = True
    while changed:
        changed = False
        for constraint in constraints:
            before = operations[constraint.before_operation]
            after = operations[constraint.after_operation]
            if before.execution_state != "ready" and after.execution_state == "ready":
                operations[after.operation_key] = after.model_copy(
                    update={
                        "execution_state": "blocked",
                        "blocked_by": [
                            *after.blocked_by,
                            f"blocked predecessor:{before.operation_key}",
                        ],
                    }
                )
                changed = True

    unresolved_flow = any(result.status != "complete" for result in flow_results)
    if unresolved_flow:
        for operation in operations.values():
            if operation.flow_id != "transfer" or operation.execution_state != "ready":
                continue
            operations[operation.operation_key] = operation.model_copy(
                update={
                    "execution_state": "blocked",
                    "blocked_by": [
                        *operation.blocked_by,
                        "unresolved manufacturing flow may insert a predecessor",
                    ],
                }
            )

    ordered_operations: list[AssembledOperation] = []
    for sequence, key in enumerate(ordered_keys, start=1):
        ordered_operations.append(
            operations[key].model_copy(update={"sequence": sequence})
        )

    committable = [
        item.operation_key
        for item in ordered_operations
        if item.necessity_status == "confirmed_required"
        and item.execution_state == "ready"
    ]
    blocked = [
        item.operation_key
        for item in ordered_operations
        if item.execution_state != "ready"
    ]
    if not ordered_operations and any(issue.kind == "error" for issue in issues):
        status = "error"
    elif issues or blocked or unresolved_flow:
        status = "partial"
    else:
        status = "complete"

    return RouteResult(
        status=status,
        operations=ordered_operations,
        constraints=constraints,
        issues=issues,
        committable_operation_keys=committable,
        blocked_operation_keys=blocked,
    )
