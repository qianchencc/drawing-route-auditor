from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from psycopg2.extras import Json

from drawing_route_auditor.db.connection import Connection
from drawing_route_auditor.decision_tree.definition import (
    DecisionTreeDefinition,
    load_tree_definition,
)


@dataclass(frozen=True, slots=True)
class TreeUpdateSummary:
    tree_key: str
    revision_id: int
    changed: bool
    reader_count: int
    fact_count: int
    node_count: int
    branch_count: int
    rule_count: int


def _summary(
    connection: Connection,
    revision_id: int,
    *,
    changed: bool,
) -> TreeUpdateSummary:
    row = connection.execute(
        """
        SELECT
            tree.tree_key,
            revision.id AS revision_id,
            (SELECT count(*) FROM decision_readers
                WHERE version_id = revision.id) AS reader_count,
            (SELECT count(*) FROM fact_definitions
                WHERE version_id = revision.id) AS fact_count,
            (SELECT count(*) FROM decision_nodes
                WHERE version_id = revision.id) AS node_count,
            (SELECT count(*) FROM decision_branches
                WHERE version_id = revision.id) AS branch_count,
            (SELECT count(*) FROM decision_rules
                WHERE version_id = revision.id) AS rule_count
        FROM decision_tree_versions AS revision
        JOIN decision_trees AS tree ON tree.id = revision.tree_id
        WHERE revision.id = %s
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"决策树存储修订 {revision_id} 已不存在")
    return TreeUpdateSummary(
        tree_key=row["tree_key"],
        revision_id=row["revision_id"],
        changed=changed,
        reader_count=row["reader_count"],
        fact_count=row["fact_count"],
        node_count=row["node_count"],
        branch_count=row["branch_count"],
        rule_count=row["rule_count"],
    )


def _insert_readers(
    connection: Connection,
    version_id: int,
    definition: DecisionTreeDefinition,
) -> dict[str, int]:
    reader_ids: dict[str, int] = {}
    for reader in definition.readers:
        row = connection.execute(
            """
            INSERT INTO decision_readers (
                version_id, reader_key, label,
                capability_definition, sequence
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                version_id,
                reader.reader_key,
                reader.label,
                reader.capability_definition,
                reader.sequence,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("保存决策树读取器失败")
        reader_ids[reader.reader_key] = row["id"]
    return reader_ids


def _insert_facts(
    connection: Connection,
    version_id: int,
    definition: DecisionTreeDefinition,
    reader_ids: dict[str, int],
) -> dict[str, int]:
    fact_ids: dict[str, int] = {}
    for fact in definition.facts:
        row = connection.execute(
            """
            INSERT INTO fact_definitions (
                version_id, reader_id, fact_key, source_kind,
                subject_scope, value_type, allowed_values, label,
                description, judgement_definition, hit_criteria,
                not_hit_criteria, coverage_requirement,
                evidence_requirement
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                version_id,
                reader_ids.get(fact.reader_key or ""),
                fact.fact_key,
                fact.source_kind,
                fact.subject_scope,
                fact.value_type,
                Json(fact.allowed_values) if fact.allowed_values is not None else None,
                fact.label,
                fact.description,
                fact.judgement_definition,
                fact.hit_criteria,
                fact.not_hit_criteria,
                fact.coverage_requirement,
                fact.evidence_requirement,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("保存事实定义失败")
        fact_ids[fact.fact_key] = row["id"]
    return fact_ids


def _insert_nodes(
    connection: Connection,
    revision_id: int,
    definition: DecisionTreeDefinition,
) -> dict[str, int]:
    node_ids: dict[str, int] = {}
    for node in definition.nodes:
        row = connection.execute(
            """
            INSERT INTO decision_nodes (
                version_id, node_key, title, node_kind,
                maintenance_status, sequence, route_required
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                revision_id,
                node.node_key,
                node.title,
                node.node_kind,
                node.maintenance_status,
                node.sequence,
                node.route_required,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("保存决策树节点失败")
        node_ids[node.node_key] = row["id"]
    return node_ids


def _insert_branches(
    connection: Connection,
    revision_id: int,
    definition: DecisionTreeDefinition,
    node_ids: dict[str, int],
) -> dict[str, int]:
    branch_ids: dict[str, int] = {}
    for branch in definition.branches:
        row = connection.execute(
            """
            INSERT INTO decision_branches (
                version_id, node_id, branch_key, title, raw_rule_text,
                maintenance_status, confidence_mode, priority
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                revision_id,
                node_ids[branch.node_key],
                branch.branch_key,
                branch.title,
                branch.rule_text,
                branch.maintenance_status,
                branch.confidence_mode,
                branch.priority,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("保存决策树分支失败")
        branch_ids[branch.branch_key] = row["id"]
    return branch_ids


def _insert_rules(
    connection: Connection,
    version_id: int,
    definition: DecisionTreeDefinition,
    branch_ids: dict[str, int],
    fact_ids: dict[str, int],
) -> None:
    for rule in definition.rules:
        row = connection.execute(
            """
            INSERT INTO decision_rules (
                version_id, branch_id, rule_key, description,
                evaluation_mode, result_kind, outcome_type,
                outcome_key, outcome_value, missing_behavior,
                source_text, priority, decision_key, question,
                option_key, option_label
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                version_id,
                branch_ids[rule.branch_key],
                rule.rule_key,
                rule.description,
                rule.evaluation_mode,
                rule.result_kind,
                rule.outcome_type,
                rule.outcome_key,
                Json(rule.outcome_value) if rule.outcome_value is not None else None,
                rule.missing_behavior,
                rule.description,
                rule.priority,
                rule.decision_key,
                rule.question,
                rule.option_key,
                rule.option_label,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("保存决策树规则失败")
        rule_id = row["id"]
        for sequence, clause in enumerate(rule.clauses, start=1):
            connection.execute(
                """
                INSERT INTO decision_rule_clauses (
                    rule_id, version_id, fact_definition_id,
                    operator, expected_value, sequence
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    rule_id,
                    version_id,
                    fact_ids[clause.fact_key],
                    clause.operator,
                    Json(clause.expected_value),
                    sequence,
                ),
            )


def _insert_edges(
    connection: Connection,
    version_id: int,
    definition: DecisionTreeDefinition,
    node_ids: dict[str, int],
    branch_ids: dict[str, int],
) -> None:
    for edge in definition.edges:
        connection.execute(
            """
            INSERT INTO decision_edges (
                version_id, edge_kind, from_node_id, from_branch_id,
                to_node_id, predecessor_ref, resolution_status, reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version_id,
                edge.edge_kind,
                node_ids.get(edge.from_node_key or ""),
                branch_ids.get(edge.from_branch_key or ""),
                node_ids[edge.to_node_key],
                edge.predecessor_ref,
                edge.resolution_status,
                edge.reason,
            ),
        )


def _source_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def persist_tree_revision(
    connection: Connection,
    *,
    definition: DecisionTreeDefinition,
    source_payload: dict[str, object],
    source_label: str,
    allow_update: bool = True,
) -> TreeUpdateSummary:
    source_hash = _source_hash(source_payload)
    tree_row = connection.execute(
        """
        INSERT INTO decision_trees (tree_key, name, description)
        VALUES (%s, %s, %s)
        ON CONFLICT (tree_key) DO NOTHING
        RETURNING id
        """,
        (definition.tree_key, definition.name, definition.description),
    ).fetchone()
    if tree_row is None:
        tree_row = connection.execute(
            "SELECT id FROM decision_trees WHERE tree_key = %s FOR UPDATE",
            (definition.tree_key,),
        ).fetchone()
    if tree_row is None:
        raise RuntimeError("保存决策树失败")
    tree_id = tree_row["id"]

    current = connection.execute(
        """
        SELECT id, source_sha256
        FROM decision_tree_versions
        WHERE tree_id = %s AND status = 'active'
        """,
        (tree_id,),
    ).fetchone()
    if current is not None and current["source_sha256"] == source_hash:
        return _summary(connection, current["id"], changed=False)
    if current is not None and not allow_update:
        raise ValueError(f"决策树 {definition.tree_key!r} 已初始化；请使用增量补丁更新")
    connection.execute(
        """
        UPDATE decision_trees
        SET name = %s, description = %s
        WHERE id = %s
        """,
        (definition.name, definition.description, tree_id),
    )

    revision_row = connection.execute(
        """
        INSERT INTO decision_tree_versions (
            tree_id, version, status, source_path,
            source_sha256, source_payload, schema_version
        )
        SELECT
            %s, coalesce(max(version), 0) + 1, 'draft',
            %s, %s, %s, %s
        FROM decision_tree_versions
        WHERE tree_id = %s
        RETURNING id
        """,
        (
            tree_id,
            source_label,
            source_hash,
            Json(source_payload),
            definition.schema_version,
            tree_id,
        ),
    ).fetchone()
    if revision_row is None:
        raise RuntimeError("保存决策树存储修订失败")
    revision_id = revision_row["id"]

    reader_ids = _insert_readers(connection, revision_id, definition)
    fact_ids = _insert_facts(
        connection,
        revision_id,
        definition,
        reader_ids,
    )
    node_ids = _insert_nodes(connection, revision_id, definition)
    branch_ids = _insert_branches(
        connection,
        revision_id,
        definition,
        node_ids,
    )
    _insert_rules(
        connection,
        revision_id,
        definition,
        branch_ids,
        fact_ids,
    )
    _insert_edges(
        connection,
        revision_id,
        definition,
        node_ids,
        branch_ids,
    )

    connection.execute(
        """
        UPDATE decision_tree_versions
        SET status = 'retired', activated_at = NULL
        WHERE tree_id = %s AND status = 'active'
        """,
        (tree_id,),
    )
    connection.execute(
        """
        UPDATE decision_tree_versions
        SET status = 'active', activated_at = now()
        WHERE id = %s
        """,
        (revision_id,),
    )

    from drawing_route_auditor.decision_tree.repository import validate_tree

    report = validate_tree(connection, definition.tree_key)
    if report.error_count:
        raise ValueError(f"决策树存在 {report.error_count} 个验证错误，拒绝更新")
    from drawing_route_auditor.decision_tree.runtime import load_runtime_tree

    load_runtime_tree(connection, definition.tree_key)
    return _summary(connection, revision_id, changed=True)


def initialize_decision_tree(
    connection: Connection,
    source_path: Path,
) -> TreeUpdateSummary:
    source_payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    definition = load_tree_definition(source_path)
    with connection.transaction():
        return persist_tree_revision(
            connection,
            definition=definition,
            source_payload=source_payload,
            source_label=f"init:{source_path}",
            allow_update=False,
        )
