DROP VIEW decision_tree_version_summary;

ALTER TABLE decision_branches
    DROP COLUMN source_row_id;

ALTER TABLE decision_nodes
    DROP COLUMN source_predecessor_ref,
    DROP COLUMN source_row_start,
    DROP COLUMN source_row_end;

DROP TABLE decision_source_rows;

CREATE VIEW decision_tree_version_summary AS
SELECT
    tree.tree_key,
    tree.name,
    version.id AS version_id,
    version.version,
    version.status,
    version.source_path,
    version.source_sha256,
    version.created_at,
    count(DISTINCT node.id) AS node_count,
    count(DISTINCT branch.id) AS branch_count,
    count(DISTINCT rule.id) AS executable_rule_count
FROM decision_trees AS tree
JOIN decision_tree_versions AS version ON version.tree_id = tree.id
LEFT JOIN decision_nodes AS node ON node.version_id = version.id
LEFT JOIN decision_branches AS branch ON branch.version_id = version.id
LEFT JOIN decision_rules AS rule ON rule.version_id = version.id
GROUP BY tree.id, version.id;
