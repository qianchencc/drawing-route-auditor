from dataclasses import dataclass
from typing import Any

from psycopg2.extras import Json

from drawing_route_auditor.db.connection import Connection


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    kind: str
    code: str
    location: str
    message: str
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ValidationReport:
    tree_key: str
    counts: dict[str, int]
    issues: tuple[ValidationIssue, ...]

    @property
    def error_count(self) -> int:
        return sum(issue.kind == "ERROR" for issue in self.issues)

    @property
    def candidate_count(self) -> int:
        return sum(issue.kind == "CANDIDATES" for issue in self.issues)


def list_trees(connection: Connection) -> list[dict[str, Any]]:
    return connection.execute(
        """
        SELECT
            tree_key,
            name,
            node_count,
            branch_count,
            executable_rule_count,
            created_at
        FROM decision_tree_version_summary
        WHERE status = 'active'
        ORDER BY tree_key
        """
    ).fetchall()


def _current_tree_row(
    connection: Connection,
    tree_key: str,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            tree.name,
            tree.description,
            revision.id AS revision_id,
            revision.version AS revision,
            revision.created_at
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS revision ON revision.tree_id = tree.id
        WHERE tree.tree_key = %s AND revision.status = 'active'
        """,
        (tree_key,),
    ).fetchone()
    if row is None:
        raise LookupError(f"未找到当前决策树 {tree_key!r}")
    return row


def current_tree_payload(
    connection: Connection,
    tree_key: str,
) -> dict[str, object]:
    current = _current_tree_row(connection, tree_key)
    row = connection.execute(
        """
        SELECT source_payload
        FROM decision_tree_versions
        WHERE id = %s
        """,
        (current["revision_id"],),
    ).fetchone()
    if row is None or not isinstance(row["source_payload"], dict):
        raise RuntimeError("当前决策树缺少可导出的规范载荷")
    return row["source_payload"]


def tree_details(
    connection: Connection,
    tree_key: str,
) -> dict[str, Any]:
    current = _current_tree_row(connection, tree_key)
    version_id = current["revision_id"]
    nodes = connection.execute(
        """
        SELECT
            node.id,
            node.node_key,
            node.title,
            node.node_kind,
            node.maintenance_status
        FROM decision_nodes AS node
        WHERE node.version_id = %s
        ORDER BY node.sequence
        """,
        (version_id,),
    ).fetchall()
    branches = connection.execute(
        """
        SELECT
            node.node_key,
            branch.branch_key,
            branch.title,
            branch.maintenance_status,
            branch.confidence_mode,
            branch.raw_rule_text,
            count(rule.id) AS rule_count
        FROM decision_branches AS branch
        JOIN decision_nodes AS node ON node.id = branch.node_id
        LEFT JOIN decision_rules AS rule ON rule.branch_id = branch.id
        WHERE branch.version_id = %s
        GROUP BY node.node_key, branch.id
        ORDER BY node.node_key::integer, branch.priority
        """,
        (version_id,),
    ).fetchall()
    edges = connection.execute(
        """
        SELECT
            target.node_key AS to_node_key,
            edge.edge_kind,
            edge.predecessor_ref,
            edge.resolution_status,
            edge.reason
        FROM decision_edges AS edge
        JOIN decision_nodes AS target ON target.id = edge.to_node_id
        WHERE edge.version_id = %s
        ORDER BY target.sequence
        """,
        (version_id,),
    ).fetchall()
    readers = connection.execute(
        """
        SELECT reader_key, label, capability_definition, sequence
        FROM decision_readers
        WHERE version_id = %s
        ORDER BY sequence
        """,
        (version_id,),
    ).fetchall()
    facts = connection.execute(
        """
        SELECT
            fact.fact_key,
            fact.label,
            fact.source_kind,
            reader.reader_key,
            fact.subject_scope,
            fact.value_type,
            fact.allowed_values,
            fact.judgement_definition
        FROM fact_definitions AS fact
        LEFT JOIN decision_readers AS reader ON reader.id = fact.reader_id
        WHERE fact.version_id = %s
        ORDER BY fact.source_kind, reader.sequence, fact.fact_key
        """,
        (version_id,),
    ).fetchall()
    rules = connection.execute(
        """
        SELECT
            node.node_key,
            branch.branch_key,
            rule.rule_key,
            rule.description,
            rule.decision_key,
            rule.question,
            rule.option_key,
            rule.option_label,
            rule.result_kind,
            rule.outcome_type,
            rule.outcome_key,
            rule.outcome_value,
            coalesce(
                jsonb_agg(
                    jsonb_build_object(
                        'fact_key', fact.fact_key,
                        'operator', clause.operator,
                        'expected_value', clause.expected_value
                    ) ORDER BY clause.sequence
                ) FILTER (WHERE clause.id IS NOT NULL),
                '[]'::jsonb
            ) AS clauses
        FROM decision_rules AS rule
        JOIN decision_branches AS branch ON branch.id = rule.branch_id
        JOIN decision_nodes AS node ON node.id = branch.node_id
        LEFT JOIN decision_rule_clauses AS clause ON clause.rule_id = rule.id
        LEFT JOIN fact_definitions AS fact
            ON fact.id = clause.fact_definition_id
        WHERE rule.version_id = %s
        GROUP BY node.node_key, branch.branch_key, rule.id
        ORDER BY node.node_key::integer, branch.branch_key, rule.priority DESC
        """,
        (version_id,),
    ).fetchall()

    return {
        "tree_key": tree_key,
        **current,
        "nodes": nodes,
        "branches": branches,
        "edges": edges,
        "readers": readers,
        "facts": facts,
        "rules": rules,
    }


def validate_tree(
    connection: Connection,
    tree_key: str,
) -> ValidationReport:
    details = tree_details(connection, tree_key)
    version_id = details["revision_id"]
    issues: list[ValidationIssue] = []

    for edge in details["edges"]:
        if edge["resolution_status"] == "unresolved":
            issues.append(
                ValidationIssue(
                    kind="ERROR",
                    code="UNRESOLVED_PREDECESSOR",
                    location=f"node:{edge['to_node_key']}",
                    message=edge["reason"] or "无法解析前置节点",
                    details={"predecessor_ref": edge["predecessor_ref"]},
                )
            )
        elif edge["resolution_status"] == "ambiguous":
            issues.append(
                ValidationIssue(
                    kind="CANDIDATES",
                    code="AMBIGUOUS_EDGE",
                    location=f"node:{edge['to_node_key']}",
                    message=edge["reason"] or "前置节点允许多个目标",
                    details={"predecessor_ref": edge["predecessor_ref"]},
                )
            )

    for node in details["nodes"]:
        if node["maintenance_status"] == "incomplete":
            issues.append(
                ValidationIssue(
                    kind="ERROR",
                    code="INCOMPLETE_NODE",
                    location=f"node:{node['node_key']}",
                    message=f"节点“{node['title']}”没有可维护的判断规则",
                    details={},
                )
            )

    for branch in details["branches"]:
        if branch["maintenance_status"] == "needs_review":
            issues.append(
                ValidationIssue(
                    kind="CANDIDATES",
                    code="RULE_NEEDS_REVIEW",
                    location=f"branch:{branch['branch_key']}",
                    message="自然语言规则尚不能安全转换为唯一可执行条件",
                    details={
                        "rule_text": branch["raw_rule_text"],
                        "confidence_mode": branch["confidence_mode"],
                    },
                )
            )
        elif branch["maintenance_status"] == "incomplete":
            issues.append(
                ValidationIssue(
                    kind="ERROR",
                    code="INCOMPLETE_BRANCH",
                    location=f"branch:{branch['branch_key']}",
                    message="分支缺少判断规则",
                    details={},
                )
            )

    counts_row = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM decision_nodes WHERE version_id = %s) AS nodes,
            (SELECT count(*) FROM decision_branches WHERE version_id = %s) AS branches,
            (SELECT count(*) FROM decision_edges WHERE version_id = %s) AS edges,
            (SELECT count(*) FROM decision_rules WHERE version_id = %s) AS rules,
            (SELECT count(*) FROM decision_rule_clauses AS clause
                JOIN decision_rules AS rule ON rule.id = clause.rule_id
                WHERE rule.version_id = %s) AS clauses
        """,
        (version_id,) * 5,
    ).fetchone()
    return ValidationReport(
        tree_key=tree_key,
        counts=dict(counts_row),
        issues=tuple(issues),
    )


def evaluate_tree(
    connection: Connection,
    tree_key: str,
    facts: dict[str, Any],
    *,
    revision: int | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(facts, dict):
        raise ValueError("事实输入必须是 JSON 对象")
    resolved_revision = revision
    if resolved_revision is None:
        current = _current_tree_row(connection, tree_key)
        resolved_revision = current["revision"]
    return connection.execute(
        """
        WITH evaluated AS (
            SELECT *
            FROM evaluate_decision_tree(%s, %s, %s)
        )
        SELECT
            evaluated.*,
            rule.decision_key,
            rule.question,
            rule.option_key,
            rule.option_label,
            rule.priority,
            ARRAY(
                SELECT fact.fact_key
                FROM decision_rule_clauses AS clause
                JOIN fact_definitions AS fact
                    ON fact.id = clause.fact_definition_id
                WHERE clause.rule_id = rule.id
                ORDER BY clause.sequence
            ) AS decisive_facts
        FROM evaluated
        JOIN decision_trees AS tree ON tree.tree_key = %s
        JOIN decision_tree_versions AS version
            ON version.tree_id = tree.id AND version.version = %s
        JOIN decision_rules AS rule
            ON rule.version_id = version.id
           AND rule.rule_key = evaluated.rule_key
        ORDER BY
            evaluated.node_key::integer,
            evaluated.branch_key,
            rule.priority DESC,
            evaluated.rule_key
        """,
        (tree_key, resolved_revision, Json(facts), tree_key, resolved_revision),
    ).fetchall()
