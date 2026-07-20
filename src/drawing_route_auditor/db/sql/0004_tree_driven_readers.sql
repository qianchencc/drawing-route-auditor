ALTER TABLE decision_tree_versions
    ADD COLUMN schema_version integer NOT NULL DEFAULT 1
        CHECK (schema_version >= 1);

CREATE TABLE decision_readers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id bigint NOT NULL
        REFERENCES decision_tree_versions(id) ON DELETE CASCADE,
    reader_key text NOT NULL,
    label text NOT NULL,
    capability_definition text NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (version_id, reader_key),
    UNIQUE (id, version_id)
);

CREATE TABLE fact_definitions_v2 (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id bigint NOT NULL
        REFERENCES decision_tree_versions(id) ON DELETE CASCADE,
    reader_id bigint,
    fact_key text NOT NULL,
    source_kind text NOT NULL
        CHECK (source_kind IN ('observed_drawing', 'external', 'derived')),
    subject_scope text NOT NULL
        CHECK (subject_scope IN (
            'current_object', 'bom_item', 'bom_link', 'occurrence', 'drawing_text'
        )),
    value_type text NOT NULL
        CHECK (value_type IN ('boolean', 'text', 'number', 'text_array')),
    allowed_values jsonb,
    label text NOT NULL,
    description text NOT NULL,
    judgement_definition text NOT NULL,
    hit_criteria text,
    not_hit_criteria text,
    coverage_requirement text,
    evidence_requirement text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (version_id, fact_key),
    UNIQUE (id, version_id),
    FOREIGN KEY (reader_id, version_id)
        REFERENCES decision_readers(id, version_id),
    CHECK (
        (source_kind = 'observed_drawing' AND reader_id IS NOT NULL)
        OR (source_kind IN ('external', 'derived') AND reader_id IS NULL)
    )
);

INSERT INTO fact_definitions_v2 (
    version_id,
    fact_key,
    source_kind,
    subject_scope,
    value_type,
    label,
    description,
    judgement_definition
)
SELECT DISTINCT
    rule.version_id,
    fact.fact_key,
    'derived',
    'current_object',
    fact.value_type,
    fact.fact_key,
    fact.description,
    fact.description
FROM fact_definitions AS fact
JOIN decision_rule_clauses AS clause
    ON clause.fact_definition_id = fact.id
JOIN decision_rules AS rule ON rule.id = clause.rule_id
ON CONFLICT (version_id, fact_key) DO NOTHING;

WITH base_version AS (
    SELECT min(id) AS id FROM decision_tree_versions
)
INSERT INTO fact_definitions_v2 (
    version_id,
    fact_key,
    source_kind,
    subject_scope,
    value_type,
    label,
    description,
    judgement_definition
)
SELECT
    base_version.id,
    fact.fact_key,
    'derived',
    'current_object',
    fact.value_type,
    fact.fact_key,
    fact.description,
    fact.description
FROM fact_definitions AS fact
CROSS JOIN base_version
WHERE base_version.id IS NOT NULL
ON CONFLICT (version_id, fact_key) DO NOTHING;

ALTER TABLE decision_rules
    ADD COLUMN decision_key text,
    ADD COLUMN question text,
    ADD COLUMN option_key text,
    ADD COLUMN option_label text,
    ADD CONSTRAINT decision_rules_id_version_unique UNIQUE (id, version_id);

UPDATE decision_rules
SET decision_key = rule_key,
    question = description,
    option_key = rule_key,
    option_label = description
WHERE decision_key IS NULL;

ALTER TABLE decision_rules
    ALTER COLUMN decision_key SET NOT NULL,
    ALTER COLUMN question SET NOT NULL,
    ALTER COLUMN option_key SET NOT NULL,
    ALTER COLUMN option_label SET NOT NULL;

ALTER TABLE decision_nodes
    ADD COLUMN route_required boolean NOT NULL DEFAULT false;

ALTER TABLE decision_rule_clauses
    ADD COLUMN version_id bigint,
    ADD COLUMN migrated_fact_definition_id bigint;

UPDATE decision_rule_clauses AS clause
SET version_id = rule.version_id
FROM decision_rules AS rule
WHERE rule.id = clause.rule_id;

UPDATE decision_rule_clauses AS clause
SET migrated_fact_definition_id = migrated.id
FROM decision_rules AS rule,
     fact_definitions AS old_fact,
     fact_definitions_v2 AS migrated
WHERE rule.id = clause.rule_id
  AND old_fact.id = clause.fact_definition_id
  AND migrated.version_id = rule.version_id
  AND migrated.fact_key = old_fact.fact_key;

ALTER TABLE fact_observations
    ADD COLUMN migrated_fact_definition_id bigint;

WITH base_version AS (
    SELECT min(id) AS id FROM decision_tree_versions
)
UPDATE fact_observations AS observation
SET migrated_fact_definition_id = migrated.id
FROM fact_definitions AS old_fact,
     fact_definitions_v2 AS migrated,
     base_version
WHERE old_fact.id = observation.fact_definition_id
  AND migrated.version_id = base_version.id
  AND migrated.fact_key = old_fact.fact_key;

ALTER TABLE decision_rule_clauses
    DROP CONSTRAINT decision_rule_clauses_fact_definition_id_fkey,
    DROP CONSTRAINT decision_rule_clauses_rule_id_fkey,
    DROP COLUMN fact_definition_id;

ALTER TABLE fact_observations
    DROP CONSTRAINT fact_hits_fact_definition_id_fkey,
    DROP COLUMN fact_definition_id;

DROP TABLE fact_definitions;
ALTER TABLE fact_definitions_v2 RENAME TO fact_definitions;

ALTER TABLE decision_rule_clauses
    RENAME COLUMN migrated_fact_definition_id TO fact_definition_id;
ALTER TABLE fact_observations
    RENAME COLUMN migrated_fact_definition_id TO fact_definition_id;

ALTER TABLE decision_rule_clauses
    ALTER COLUMN version_id SET NOT NULL,
    ALTER COLUMN fact_definition_id SET NOT NULL,
    ADD CONSTRAINT decision_rule_clauses_rule_version_fkey
        FOREIGN KEY (rule_id, version_id)
        REFERENCES decision_rules(id, version_id) ON DELETE CASCADE,
    ADD CONSTRAINT decision_rule_clauses_fact_version_fkey
        FOREIGN KEY (fact_definition_id, version_id)
        REFERENCES fact_definitions(id, version_id);

ALTER TABLE fact_observations
    ALTER COLUMN fact_definition_id SET NOT NULL,
    ADD CONSTRAINT fact_observations_fact_definition_fkey
        FOREIGN KEY (fact_definition_id)
        REFERENCES fact_definitions(id);

CREATE INDEX fact_definitions_version_reader_idx
    ON fact_definitions (version_id, reader_id, fact_key);
CREATE INDEX decision_rule_clauses_version_fact_idx
    ON decision_rule_clauses (version_id, fact_definition_id);

CREATE TABLE reader_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    version_id bigint NOT NULL
        REFERENCES decision_tree_versions(id) ON DELETE RESTRICT,
    reader_id bigint NOT NULL,
    task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'error', 'cancelled')),
    requested_features jsonb NOT NULL,
    page_inputs jsonb NOT NULL DEFAULT '[]'::jsonb,
    prompt_template_version text NOT NULL,
    output_schema_version text NOT NULL,
    model_version text,
    duration_milliseconds integer
        CHECK (duration_milliseconds IS NULL OR duration_milliseconds >= 0),
    prompt_tokens integer NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens integer NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (run_id, reader_id),
    FOREIGN KEY (reader_id, version_id)
        REFERENCES decision_readers(id, version_id)
);

CREATE TABLE route_recommendations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    version_id bigint NOT NULL
        REFERENCES decision_tree_versions(id) ON DELETE RESTRICT,
    status text NOT NULL
        CHECK (status IN ('complete', 'complete_with_candidates', 'partial', 'error')),
    confirmed_route jsonb,
    local_issues jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE route_candidates (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recommendation_id bigint NOT NULL
        REFERENCES route_recommendations(id) ON DELETE CASCADE,
    candidate_key text NOT NULL,
    operations jsonb NOT NULL,
    key_decisions jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (recommendation_id, candidate_key)
);
