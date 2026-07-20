CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'complete', 'partial', 'error', 'cancelled')),
    knowledge_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    error_code text,
    error_message text
);

CREATE TABLE run_inputs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    input_kind text NOT NULL CHECK (input_kind IN ('pdf', 'cad', 'context')),
    source_path text NOT NULL,
    source_sha256 text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, input_kind, source_path)
);

CREATE TABLE tasks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_key text NOT NULL,
    task_type text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'error', 'cancelled')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb,
    worker_id text,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, task_key)
);

CREATE INDEX tasks_claim_idx
    ON tasks (task_type, available_at, created_at)
    WHERE status = 'queued';

CREATE TABLE task_attempts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_number integer NOT NULL CHECK (attempt_number > 0),
    worker_id text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'error', 'cancelled')),
    error_code text,
    error_message text,
    UNIQUE (task_id, attempt_number)
);

CREATE TABLE decision_trees (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tree_key text NOT NULL UNIQUE,
    name text NOT NULL,
    description text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE decision_tree_versions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tree_id bigint NOT NULL REFERENCES decision_trees(id) ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'retired')),
    source_path text NOT NULL,
    source_sha256 text NOT NULL,
    source_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    UNIQUE (tree_id, version)
);

CREATE UNIQUE INDEX decision_tree_one_active_version_idx
    ON decision_tree_versions (tree_id)
    WHERE status = 'active';

CREATE TABLE decision_source_rows (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES decision_tree_versions(id) ON DELETE CASCADE,
    row_number integer NOT NULL CHECK (row_number > 0),
    serial_text text,
    predecessor_ref text,
    node_ref text,
    node_title text,
    branch_ref text,
    thought text,
    rule_text text,
    raw_cells jsonb NOT NULL,
    formatting jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_row jsonb NOT NULL,
    UNIQUE (version_id, row_number)
);

CREATE TABLE decision_nodes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES decision_tree_versions(id) ON DELETE CASCADE,
    node_key text NOT NULL,
    title text NOT NULL,
    node_kind text NOT NULL
        CHECK (node_kind IN ('classification', 'route_generation', 'calculation')),
    maintenance_status text NOT NULL
        CHECK (maintenance_status IN ('complete', 'needs_review', 'incomplete')),
    sequence integer NOT NULL CHECK (sequence > 0),
    source_predecessor_ref text,
    source_row_start integer NOT NULL,
    source_row_end integer NOT NULL,
    CHECK (source_row_end >= source_row_start),
    UNIQUE (version_id, node_key),
    UNIQUE (version_id, sequence)
);

CREATE TABLE decision_branches (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES decision_tree_versions(id) ON DELETE CASCADE,
    node_id bigint NOT NULL REFERENCES decision_nodes(id) ON DELETE CASCADE,
    source_row_id bigint NOT NULL REFERENCES decision_source_rows(id) ON DELETE CASCADE,
    branch_key text NOT NULL,
    title text,
    raw_rule_text text,
    maintenance_status text NOT NULL
        CHECK (maintenance_status IN ('executable', 'needs_review', 'incomplete')),
    confidence_mode text NOT NULL DEFAULT 'unknown'
        CHECK (confidence_mode IN ('certain', 'candidate', 'unknown')),
    priority integer NOT NULL DEFAULT 0,
    UNIQUE (version_id, branch_key)
);

CREATE TABLE decision_edges (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES decision_tree_versions(id) ON DELETE CASCADE,
    edge_kind text NOT NULL CHECK (edge_kind IN ('root', 'node', 'branch')),
    from_node_id bigint REFERENCES decision_nodes(id) ON DELETE CASCADE,
    from_branch_id bigint REFERENCES decision_branches(id) ON DELETE CASCADE,
    to_node_id bigint NOT NULL REFERENCES decision_nodes(id) ON DELETE CASCADE,
    predecessor_ref text NOT NULL,
    resolution_status text NOT NULL
        CHECK (resolution_status IN ('resolved', 'ambiguous', 'unresolved')),
    reason text,
    CHECK (
        (edge_kind = 'root' AND from_node_id IS NULL AND from_branch_id IS NULL)
        OR
        (edge_kind = 'node' AND from_node_id IS NOT NULL AND from_branch_id IS NULL)
        OR
        (edge_kind = 'branch' AND from_node_id IS NULL AND from_branch_id IS NOT NULL)
    ),
    UNIQUE (version_id, to_node_id, predecessor_ref)
);

CREATE INDEX decision_edges_from_node_idx ON decision_edges (from_node_id);
CREATE INDEX decision_edges_from_branch_idx ON decision_edges (from_branch_id);

CREATE TABLE fact_definitions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fact_key text NOT NULL UNIQUE,
    value_type text NOT NULL
        CHECK (value_type IN ('boolean', 'text', 'number', 'text_array')),
    description text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE fact_hits (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    fact_definition_id bigint NOT NULL REFERENCES fact_definitions(id),
    reader_task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
    status text NOT NULL
        CHECK (status IN ('hit', 'not_hit', 'unable_to_judge', 'conflict')),
    scope text NOT NULL
        CHECK (scope IN (
            'current_object', 'bom_item', 'bom_link', 'occurrence', 'drawing_text'
        )),
    object_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
    value jsonb,
    confidence numeric(5, 4)
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX fact_hits_run_fact_idx
    ON fact_hits (run_id, fact_definition_id, status);

CREATE TABLE evidence (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fact_hit_id bigint NOT NULL REFERENCES fact_hits(id) ON DELETE CASCADE,
    input_id bigint REFERENCES run_inputs(id) ON DELETE SET NULL,
    source_type text NOT NULL
        CHECK (source_type IN ('drawing', 'cad', 'plm', 'rule')),
    page_number integer CHECK (page_number IS NULL OR page_number > 0),
    view_ref text,
    region jsonb,
    original_text text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX evidence_fact_hit_idx ON evidence (fact_hit_id);

CREATE TABLE decision_rules (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES decision_tree_versions(id) ON DELETE CASCADE,
    branch_id bigint NOT NULL REFERENCES decision_branches(id) ON DELETE CASCADE,
    rule_key text NOT NULL,
    description text NOT NULL,
    evaluation_mode text NOT NULL DEFAULT 'all'
        CHECK (evaluation_mode IN ('all', 'any')),
    result_kind text NOT NULL
        CHECK (result_kind IN ('resolved', 'candidate', 'error')),
    outcome_type text NOT NULL
        CHECK (outcome_type IN ('fact', 'route_family', 'process', 'stage', 'error')),
    outcome_key text NOT NULL,
    outcome_value jsonb,
    missing_behavior text NOT NULL DEFAULT 'error'
        CHECK (missing_behavior IN ('error', 'candidate', 'not_match')),
    source_text text,
    priority integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (version_id, rule_key)
);

CREATE TABLE decision_rule_clauses (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_id bigint NOT NULL REFERENCES decision_rules(id) ON DELETE CASCADE,
    fact_definition_id bigint NOT NULL REFERENCES fact_definitions(id),
    operator text NOT NULL
        CHECK (operator IN (
            'eq', 'neq', 'starts_with', 'not_starts_with',
            'contains', 'in', 'lt', 'lte', 'gt', 'gte'
        )),
    expected_value jsonb NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    UNIQUE (rule_id, sequence)
);

CREATE INDEX decision_rules_branch_idx ON decision_rules (branch_id, priority, rule_key);
CREATE INDEX decision_rule_clauses_rule_idx ON decision_rule_clauses (rule_id, sequence);

CREATE TABLE external_decisions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid REFERENCES runs(id) ON DELETE CASCADE,
    version_id bigint REFERENCES decision_tree_versions(id) ON DELETE CASCADE,
    element_key text NOT NULL,
    candidate_id text NOT NULL,
    actor text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (run_id IS NOT NULL OR version_id IS NOT NULL)
);

CREATE TABLE audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid REFERENCES runs(id) ON DELETE CASCADE,
    event_type text NOT NULL,
    actor text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

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
