from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from psycopg2.extras import Json

from drawing_route_auditor.db.connection import Connection
from drawing_route_auditor.decision_tree.definition import (
    DecisionTreeDefinition,
    load_tree_definition,
)
from drawing_route_auditor.decision_tree.source import (
    DecisionTreeSource,
    load_decision_tree_source,
)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    tree_key: str
    version: int
    version_id: int
    source_sha256: str
    existing: bool
    source_row_count: int
    reader_count: int
    fact_count: int
    node_count: int
    branch_count: int
    rule_count: int


def _summary(
    connection: Connection,
    version_id: int,
    *,
    existing: bool,
) -> ImportSummary:
    row = connection.execute(
        """
        SELECT
            tree.tree_key,
            version.version,
            version.id AS version_id,
            version.source_sha256,
            (SELECT count(*) FROM decision_source_rows
                WHERE version_id = version.id) AS source_row_count,
            (SELECT count(*) FROM decision_readers
                WHERE version_id = version.id) AS reader_count,
            (SELECT count(*) FROM fact_definitions
                WHERE version_id = version.id) AS fact_count,
            (SELECT count(*) FROM decision_nodes
                WHERE version_id = version.id) AS node_count,
            (SELECT count(*) FROM decision_branches
                WHERE version_id = version.id) AS branch_count,
            (SELECT count(*) FROM decision_rules
                WHERE version_id = version.id) AS rule_count
        FROM decision_tree_versions AS version
        JOIN decision_trees AS tree ON tree.id = version.tree_id
        WHERE version.id = %s
        """,
        (version_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"决策树版本 {version_id} 已不存在")
    return ImportSummary(
        tree_key=row["tree_key"],
        version=row["version"],
        version_id=row["version_id"],
        source_sha256=row["source_sha256"],
        existing=existing,
        source_row_count=row["source_row_count"],
        reader_count=row["reader_count"],
        fact_count=row["fact_count"],
        node_count=row["node_count"],
        branch_count=row["branch_count"],
        rule_count=row["rule_count"],
    )


def _load_base_source(
    definition_path: Path,
    definition: DecisionTreeDefinition,
) -> DecisionTreeSource:
    base_path = Path(definition.base_source_path)
    if not base_path.is_absolute():
        base_path = definition_path.parent / base_path
    actual_sha256 = sha256(base_path.read_bytes()).hexdigest()
    if actual_sha256 != definition.base_source_sha256:
        raise ValueError("基础决策树来源校验和与定义不一致")
    return load_decision_tree_source(base_path)


def _insert_source_rows(
    connection: Connection,
    version_id: int,
    source: DecisionTreeSource,
) -> dict[int, int]:
    source_row_ids: dict[int, int] = {}
    for row in source.rows:
        inserted = connection.execute(
            """
            INSERT INTO decision_source_rows (
                version_id, row_number, serial_text, predecessor_ref,
                node_ref, node_title, branch_ref, thought, rule_text,
                raw_cells, formatting, source_row
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                version_id,
                row.row_number,
                row.serial_text,
                row.predecessor_ref,
                row.node_ref,
                row.node_title,
                row.branch_ref,
                row.thought,
                row.rule_text,
                Json(list(row.raw_cells)),
                Json(row.formatting),
                Json(row.source_row),
            ),
        ).fetchone()
        if inserted is None:
            raise RuntimeError("保存决策树来源行失败")
        source_row_ids[row.row_number] = inserted["id"]
    return source_row_ids


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
    version_id: int,
    definition: DecisionTreeDefinition,
) -> dict[str, int]:
    node_ids: dict[str, int] = {}
    for node in definition.nodes:
        row = connection.execute(
            """
            INSERT INTO decision_nodes (
                version_id, node_key, title, node_kind,
                maintenance_status, sequence, source_predecessor_ref,
                source_row_start, source_row_end, route_required
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                version_id,
                node.node_key,
                node.title,
                node.node_kind,
                node.maintenance_status,
                node.sequence,
                node.source_predecessor_ref,
                node.source_row_start,
                node.source_row_end,
                node.route_required,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("保存决策树节点失败")
        node_ids[node.node_key] = row["id"]
    return node_ids


def _insert_branches(
    connection: Connection,
    version_id: int,
    definition: DecisionTreeDefinition,
    node_ids: dict[str, int],
    source_row_ids: dict[int, int],
) -> dict[str, int]:
    branch_ids: dict[str, int] = {}
    for branch in definition.branches:
        row = connection.execute(
            """
            INSERT INTO decision_branches (
                version_id, node_id, source_row_id, branch_key,
                title, raw_rule_text, maintenance_status,
                confidence_mode, priority
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                version_id,
                node_ids[branch.node_key],
                source_row_ids[branch.source_row_number],
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


def import_decision_tree(
    connection: Connection,
    source_path: Path,
) -> ImportSummary:
    definition = load_tree_definition(source_path)
    source_bytes = source_path.read_bytes()
    source_hash = sha256(source_bytes).hexdigest()
    source_payload = json.loads(source_bytes.decode("utf-8-sig"))
    base_source = _load_base_source(source_path, definition)

    tree_row = connection.execute(
        """
        INSERT INTO decision_trees (tree_key, name, description)
        VALUES (%s, %s, %s)
        ON CONFLICT (tree_key) DO UPDATE SET
            name = EXCLUDED.name,
            description = EXCLUDED.description
        RETURNING id
        """,
        (definition.tree_key, definition.name, definition.description),
    ).fetchone()
    if tree_row is None:
        raise RuntimeError("保存决策树失败")
    tree_id = tree_row["id"]

    existing = connection.execute(
        """
        SELECT id, source_sha256
        FROM decision_tree_versions
        WHERE tree_id = %s AND version = %s
        """,
        (tree_id, definition.version),
    ).fetchone()
    if existing is not None:
        if existing["source_sha256"] != source_hash:
            raise ValueError(
                f"决策树 {definition.tree_key!r} 版本 {definition.version} "
                "已存在且内容不同"
            )
        return _summary(connection, existing["id"], existing=True)

    version_row = connection.execute(
        """
        INSERT INTO decision_tree_versions (
            tree_id, version, status, source_path,
            source_sha256, source_payload, schema_version
        )
        VALUES (%s, %s, 'draft', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            tree_id,
            definition.version,
            str(source_path),
            source_hash,
            Json(source_payload),
            definition.schema_version,
        ),
    ).fetchone()
    if version_row is None:
        raise RuntimeError("保存决策树版本失败")
    version_id = version_row["id"]

    source_row_ids = _insert_source_rows(connection, version_id, base_source)
    reader_ids = _insert_readers(connection, version_id, definition)
    fact_ids = _insert_facts(
        connection,
        version_id,
        definition,
        reader_ids,
    )
    node_ids = _insert_nodes(connection, version_id, definition)
    branch_ids = _insert_branches(
        connection,
        version_id,
        definition,
        node_ids,
        source_row_ids,
    )
    _insert_rules(
        connection,
        version_id,
        definition,
        branch_ids,
        fact_ids,
    )
    _insert_edges(
        connection,
        version_id,
        definition,
        node_ids,
        branch_ids,
    )
    return _summary(connection, version_id, existing=False)
