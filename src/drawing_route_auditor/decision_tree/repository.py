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
    version: int
    counts: dict[str, int]
    issues: tuple[ValidationIssue, ...]

    @property
    def error_count(self) -> int:
        return sum(issue.kind == "ERROR" for issue in self.issues)

    @property
    def candidate_count(self) -> int:
        return sum(issue.kind == "CANDIDATES" for issue in self.issues)


def list_tree_versions(connection: Connection) -> list[dict[str, Any]]:
    return connection.execute(
        """
        SELECT
            tree_key,
            name,
            version,
            status,
            source_path,
            source_sha256,
            node_count,
            branch_count,
            executable_rule_count,
            created_at
        FROM decision_tree_version_summary
        ORDER BY tree_key, version
        """
    ).fetchall()


def _version_row(
    connection: Connection,
    tree_key: str,
    version: int | None,
) -> dict[str, Any]:
    if version is None:
        row = connection.execute(
            """
            SELECT
                tree.name,
                tree.description,
                version.id AS version_id,
                version.version,
                version.status,
                version.source_path,
                version.source_sha256,
                version.created_at
            FROM decision_trees AS tree
            JOIN decision_tree_versions AS version ON version.tree_id = tree.id
            WHERE tree.tree_key = %s AND version.status = 'active'
            """,
            (tree_key,),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT
                tree.name,
                tree.description,
                version.id AS version_id,
                version.version,
                version.status,
                version.source_path,
                version.source_sha256,
                version.created_at
            FROM decision_trees AS tree
            JOIN decision_tree_versions AS version ON version.tree_id = tree.id
            WHERE tree.tree_key = %s AND version.version = %s
            """,
            (tree_key, version),
        ).fetchone()
    if row is None:
        requested = "启用版本" if version is None else f"版本 {version}"
        raise LookupError(f"未找到决策树 {tree_key!r} 的{requested}")
    return row


def tree_details(
    connection: Connection,
    tree_key: str,
    version: int | None,
) -> dict[str, Any]:
    version_row = _version_row(connection, tree_key, version)
    version_id = version_row["version_id"]
    nodes = connection.execute(
        """
        SELECT
            node.id,
            node.node_key,
            node.title,
            node.node_kind,
            node.maintenance_status,
            node.source_predecessor_ref,
            node.source_row_start,
            node.source_row_end
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
        **version_row,
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
    version: int | None,
) -> ValidationReport:
    details = tree_details(connection, tree_key, version)
    version_id = details["version_id"]
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
                    details={
                        "source_rows": [
                            node["source_row_start"],
                            node["source_row_end"],
                        ]
                    },
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
            (SELECT count(*) FROM decision_source_rows WHERE version_id = %s) AS source_rows,
            (SELECT count(*) FROM decision_nodes WHERE version_id = %s) AS nodes,
            (SELECT count(*) FROM decision_branches WHERE version_id = %s) AS branches,
            (SELECT count(*) FROM decision_edges WHERE version_id = %s) AS edges,
            (SELECT count(*) FROM decision_rules WHERE version_id = %s) AS rules,
            (SELECT count(*) FROM decision_rule_clauses AS clause
                JOIN decision_rules AS rule ON rule.id = clause.rule_id
                WHERE rule.version_id = %s) AS clauses
        """,
        (version_id,) * 6,
    ).fetchone()
    return ValidationReport(
        tree_key=tree_key,
        version=details["version"],
        counts=dict(counts_row),
        issues=tuple(issues),
    )


def evaluate_tree(
    connection: Connection,
    tree_key: str,
    version: int | None,
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(facts, dict):
        raise ValueError("事实输入必须是 JSON 对象")
    version_row = _version_row(connection, tree_key, version)
    resolved_version = version_row["version"]
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
        (tree_key, resolved_version, Json(facts), tree_key, resolved_version),
    ).fetchall()


def activate_tree(
    connection: Connection,
    tree_key: str,
    version: int,
    *,
    allow_incomplete: bool = False,
) -> None:
    report = validate_tree(connection, tree_key, version)
    if report.error_count and not allow_incomplete:
        raise ValueError(
            f"决策树存在 {report.error_count} 个验证错误；"
            "只有明确接受草稿时才能使用 allow_incomplete"
        )
    version_row = _version_row(connection, tree_key, version)
    connection.execute(
        """
        UPDATE decision_tree_versions
        SET status = 'retired', activated_at = NULL
        WHERE tree_id = (
            SELECT tree_id FROM decision_tree_versions WHERE id = %s
        ) AND status = 'active'
        """,
        (version_row["version_id"],),
    )
    connection.execute(
        """
        UPDATE decision_tree_versions
        SET status = 'active', activated_at = now()
        WHERE id = %s
        """,
        (version_row["version_id"],),
    )
