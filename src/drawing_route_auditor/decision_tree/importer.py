from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from psycopg2.extras import Json

from drawing_route_auditor.db.connection import Connection

from drawing_route_auditor.decision_tree.catalog import EXECUTABLE_BRANCH_KEYS, FACTS, RULES
from drawing_route_auditor.decision_tree.source import DecisionTreeSource, SourceRow, load_decision_tree_source


_FUZZY_MARKERS = ("大概率", "一般", "绝大部分", "极少", "难以总结", "多维度")


@dataclass(frozen=True, slots=True)
class ImportSummary:
    tree_key: str
    version: int
    version_id: int
    source_sha256: str
    existing: bool
    source_row_count: int
    node_count: int
    branch_count: int
    rule_count: int


def _node_kind(node_key: str) -> str:
    number = int(node_key)
    if number in {1, 2, 5}:
        return "classification"
    if number in {3, 4}:
        return "route_generation"
    return "calculation"


def _branch_maintenance_status(row: SourceRow) -> str:
    if row.rule_text is None:
        return "incomplete"
    if row.branch_ref in EXECUTABLE_BRANCH_KEYS:
        return "executable"
    return "needs_review"


def _branch_confidence_mode(row: SourceRow) -> str:
    text = row.rule_text or ""
    if any(marker in text for marker in _FUZZY_MARKERS):
        return "candidate"
    if row.branch_ref in EXECUTABLE_BRANCH_KEYS:
        return "certain"
    return "unknown"


def _node_maintenance_status(rows: list[SourceRow]) -> str:
    branch_rows = [row for row in rows if row.branch_ref is not None]
    if not branch_rows or all(row.rule_text is None for row in branch_rows):
        return "incomplete"
    statuses = {_branch_maintenance_status(row) for row in branch_rows}
    if statuses == {"executable"}:
        return "complete"
    return "needs_review"


def _summary(connection: Connection, version_id: int, *, existing: bool) -> ImportSummary:
    row = connection.execute(
        """
        SELECT
            tree.tree_key,
            version.version,
            version.id AS version_id,
            version.source_sha256,
            (SELECT count(*) FROM decision_source_rows WHERE version_id = version.id) AS source_row_count,
            (SELECT count(*) FROM decision_nodes WHERE version_id = version.id) AS node_count,
            (SELECT count(*) FROM decision_branches WHERE version_id = version.id) AS branch_count,
            (SELECT count(*) FROM decision_rules WHERE version_id = version.id) AS rule_count
        FROM decision_tree_versions AS version
        JOIN decision_trees AS tree ON tree.id = version.tree_id
        WHERE version.id = %s
        """,
        (version_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Decision-tree version {version_id} disappeared")
    return ImportSummary(
        tree_key=row["tree_key"],
        version=row["version"],
        version_id=row["version_id"],
        source_sha256=row["source_sha256"],
        existing=existing,
        source_row_count=row["source_row_count"],
        node_count=row["node_count"],
        branch_count=row["branch_count"],
        rule_count=row["rule_count"],
    )


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
                version_id,
                row_number,
                serial_text,
                predecessor_ref,
                node_ref,
                node_title,
                branch_ref,
                thought,
                rule_text,
                raw_cells,
                formatting,
                source_row
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
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
        source_row_ids[row.row_number] = inserted["id"]
    return source_row_ids


def _insert_nodes(
    connection: Connection,
    version_id: int,
    source: DecisionTreeSource,
) -> tuple[dict[str, int], dict[str, list[SourceRow]]]:
    rows_by_node: dict[str, list[SourceRow]] = defaultdict(list)
    for row in source.rows:
        rows_by_node[row.node_ref].append(row)

    node_ids: dict[str, int] = {}
    for node_key in sorted(rows_by_node, key=int):
        rows = rows_by_node[node_key]
        titles = {row.node_title for row in rows}
        predecessors = {row.predecessor_ref for row in rows}
        if len(titles) != 1 or len(predecessors) != 1:
            raise ValueError(
                f"Node {node_key} has inconsistent merged values: "
                f"titles={titles!r}, predecessors={predecessors!r}"
            )
        inserted = connection.execute(
            """
            INSERT INTO decision_nodes (
                version_id,
                node_key,
                title,
                node_kind,
                maintenance_status,
                sequence,
                source_predecessor_ref,
                source_row_start,
                source_row_end
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                version_id,
                node_key,
                rows[0].node_title,
                _node_kind(node_key),
                _node_maintenance_status(rows),
                int(node_key),
                rows[0].predecessor_ref,
                min(row.row_number for row in rows),
                max(row.row_number for row in rows),
            ),
        ).fetchone()
        node_ids[node_key] = inserted["id"]
    return node_ids, rows_by_node


def _insert_branches(
    connection: Connection,
    version_id: int,
    source: DecisionTreeSource,
    source_row_ids: dict[int, int],
    node_ids: dict[str, int],
) -> dict[str, int]:
    branch_ids: dict[str, int] = {}
    for row in source.rows:
        if row.branch_ref is None:
            continue
        if row.branch_ref in branch_ids:
            raise ValueError(f"Duplicate branch key {row.branch_ref!r}")
        inserted = connection.execute(
            """
            INSERT INTO decision_branches (
                version_id,
                node_id,
                source_row_id,
                branch_key,
                title,
                raw_rule_text,
                maintenance_status,
                confidence_mode,
                priority
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                version_id,
                node_ids[row.node_ref],
                source_row_ids[row.row_number],
                row.branch_ref,
                row.thought,
                row.rule_text,
                _branch_maintenance_status(row),
                _branch_confidence_mode(row),
                row.row_number,
            ),
        ).fetchone()
        branch_ids[row.branch_ref] = inserted["id"]
    return branch_ids


def _insert_edges(
    connection: Connection,
    version_id: int,
    rows_by_node: dict[str, list[SourceRow]],
    node_ids: dict[str, int],
    branch_ids: dict[str, int],
) -> None:
    predecessor_counts = Counter(
        rows[0].predecessor_ref for rows in rows_by_node.values()
    )

    for node_key in sorted(rows_by_node, key=int):
        predecessor_ref = rows_by_node[node_key][0].predecessor_ref or ""
        edge_kind: str
        from_node_id: int | None = None
        from_branch_id: int | None = None
        reason: str | None = None

        if predecessor_ref == "0":
            edge_kind = "root"
            resolution_status = "resolved"
        elif predecessor_ref in branch_ids:
            edge_kind = "branch"
            from_branch_id = branch_ids[predecessor_ref]
            resolution_status = (
                "ambiguous" if predecessor_counts[predecessor_ref] > 1 else "resolved"
            )
        elif predecessor_ref in node_ids:
            edge_kind = "node"
            from_node_id = node_ids[predecessor_ref]
            resolution_status = (
                "ambiguous" if predecessor_counts[predecessor_ref] > 1 else "resolved"
            )
        else:
            edge_kind = "root"
            resolution_status = "unresolved"
            reason = f"Predecessor {predecessor_ref!r} does not identify a node or branch"

        if resolution_status == "ambiguous":
            reason = (
                f"Predecessor {predecessor_ref!r} points to multiple nodes without "
                "an explicit edge condition"
            )

        connection.execute(
            """
            INSERT INTO decision_edges (
                version_id,
                edge_kind,
                from_node_id,
                from_branch_id,
                to_node_id,
                predecessor_ref,
                resolution_status,
                reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version_id,
                edge_kind,
                from_node_id,
                from_branch_id,
                node_ids[node_key],
                predecessor_ref,
                resolution_status,
                reason,
            ),
        )


def _seed_facts(connection: Connection) -> dict[str, int]:
    for fact in FACTS:
        connection.execute(
            """
            INSERT INTO fact_definitions (fact_key, value_type, description)
            VALUES (%s, %s, %s)
            ON CONFLICT (fact_key) DO UPDATE SET
                value_type = EXCLUDED.value_type,
                description = EXCLUDED.description
            """,
            (fact.key, fact.value_type, fact.description),
        )
    rows = connection.execute(
        "SELECT id, fact_key FROM fact_definitions"
    ).fetchall()
    return {row["fact_key"]: row["id"] for row in rows}


def _seed_rules(
    connection: Connection,
    version_id: int,
    branch_ids: dict[str, int],
    facts: dict[str, int],
    source: DecisionTreeSource,
) -> None:
    source_text_by_branch = {
        row.branch_ref: row.rule_text
        for row in source.rows
        if row.branch_ref is not None
    }

    for rule in RULES:
        branch_id = branch_ids.get(rule.branch_key)
        if branch_id is None:
            raise ValueError(
                f"Curated rule {rule.rule_key!r} references missing branch {rule.branch_key!r}"
            )
        inserted = connection.execute(
            """
            INSERT INTO decision_rules (
                version_id,
                branch_id,
                rule_key,
                description,
                evaluation_mode,
                result_kind,
                outcome_type,
                outcome_key,
                outcome_value,
                missing_behavior,
                source_text,
                priority
            )
            VALUES (%s, %s, %s, %s, 'all', %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                version_id,
                branch_id,
                rule.rule_key,
                rule.description,
                rule.result_kind,
                rule.outcome_type,
                rule.outcome_key,
                Json(rule.outcome_value) if rule.outcome_value is not None else None,
                rule.missing_behavior,
                source_text_by_branch[rule.branch_key],
                rule.priority,
            ),
        ).fetchone()
        rule_id = inserted["id"]

        for sequence, clause in enumerate(rule.clauses, start=1):
            connection.execute(
                """
                INSERT INTO decision_rule_clauses (
                    rule_id,
                    fact_definition_id,
                    operator,
                    expected_value,
                    sequence
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    rule_id,
                    facts[clause.fact_key],
                    clause.operator,
                    Json(clause.expected),
                    sequence,
                ),
            )


def import_decision_tree(
    connection: Connection,
    source_path: Path,
    *,
    tree_key: str,
    name: str,
    version: int,
    description: str | None = None,
) -> ImportSummary:
    source_bytes = source_path.read_bytes()
    source_hash = sha256(source_bytes).hexdigest()
    source = load_decision_tree_source(source_path)

    tree_row = connection.execute(
        """
        INSERT INTO decision_trees (tree_key, name, description)
        VALUES (%s, %s, %s)
        ON CONFLICT (tree_key) DO UPDATE SET
            name = EXCLUDED.name,
            description = coalesce(EXCLUDED.description, decision_trees.description)
        RETURNING id
        """,
        (tree_key, name, description),
    ).fetchone()
    tree_id = tree_row["id"]

    existing_hash = connection.execute(
        """
        SELECT id
        FROM decision_tree_versions
        WHERE tree_id = %s AND version = %s AND source_sha256 = %s
        """,
        (tree_id, version, source_hash),
    ).fetchone()
    if existing_hash is not None:
        return _summary(connection, existing_hash["id"], existing=True)

    conflicting_version = connection.execute(
        """
        SELECT source_sha256
        FROM decision_tree_versions
        WHERE tree_id = %s AND version = %s
        """,
        (tree_id, version),
    ).fetchone()
    if conflicting_version is not None:
        raise ValueError(
            f"Tree {tree_key!r} version {version} already exists with a different source"
        )

    version_row = connection.execute(
        """
        INSERT INTO decision_tree_versions (
            tree_id,
            version,
            source_path,
            source_sha256,
            source_payload
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            tree_id,
            version,
            str(source_path),
            source_hash,
            Json(source.payload),
        ),
    ).fetchone()
    version_id = version_row["id"]

    source_row_ids = _insert_source_rows(connection, version_id, source)
    node_ids, rows_by_node = _insert_nodes(connection, version_id, source)
    branch_ids = _insert_branches(
        connection,
        version_id,
        source,
        source_row_ids,
        node_ids,
    )
    _insert_edges(
        connection,
        version_id,
        rows_by_node,
        node_ids,
        branch_ids,
    )
    facts = _seed_facts(connection)
    _seed_rules(connection, version_id, branch_ids, facts, source)

    return _summary(connection, version_id, existing=False)
