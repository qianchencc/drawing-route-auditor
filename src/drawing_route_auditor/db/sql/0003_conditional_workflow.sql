ALTER TABLE runs
    ADD COLUMN knowledge_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN reader_model_version text,
    ADD COLUMN reader_prompt_version text,
    ADD COLUMN result jsonb,
    ADD COLUMN elapsed_milliseconds integer
        CHECK (elapsed_milliseconds IS NULL OR elapsed_milliseconds >= 0);

ALTER TABLE fact_hits RENAME TO fact_observations;
ALTER TABLE fact_observations RENAME COLUMN object_ref TO subject_ref;
ALTER INDEX fact_hits_run_fact_idx RENAME TO fact_observations_run_fact_idx;

ALTER TABLE fact_observations
    ADD COLUMN observation_coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN coverage_complete boolean NOT NULL DEFAULT false;

ALTER TABLE fact_observations
    DROP CONSTRAINT fact_hits_status_check;

ALTER TABLE fact_observations
    ADD CONSTRAINT fact_observations_status_check
        CHECK (status IN (
            'hit',
            'not_hit',
            'unable_to_judge',
            'conflict',
            'missing_due_to_reader_failure'
        )),
    ADD CONSTRAINT fact_observations_not_hit_coverage_check
        CHECK (status <> 'not_hit' OR coverage_complete);

ALTER TABLE evidence RENAME COLUMN fact_hit_id TO fact_observation_id;
ALTER INDEX evidence_fact_hit_idx RENAME TO evidence_fact_observation_idx;

CREATE TABLE run_flow_results (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    flow_id text NOT NULL,
    status text NOT NULL
        CHECK (status IN ('complete', 'partial', 'error', 'skipped')),
    duration_milliseconds integer NOT NULL CHECK (duration_milliseconds >= 0),
    prompt_tokens integer NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens integer NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, flow_id)
);

CREATE TABLE route_operations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    operation_key text NOT NULL,
    flow_id text NOT NULL,
    process text NOT NULL,
    content text NOT NULL,
    targets jsonb NOT NULL DEFAULT '[]'::jsonb,
    necessity_status text NOT NULL
        CHECK (necessity_status IN ('confirmed_required', 'conditional')),
    execution_state text NOT NULL
        CHECK (execution_state IN ('ready', 'blocked', 'conditional', 'invalid')),
    blocked_by jsonb NOT NULL DEFAULT '[]'::jsonb,
    sequence integer CHECK (sequence IS NULL OR sequence > 0),
    lineage jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, operation_key)
);

CREATE INDEX route_operations_run_sequence_idx
    ON route_operations (run_id, sequence);

CREATE TABLE route_constraints (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    before_operation_key text NOT NULL,
    after_operation_key text NOT NULL,
    reason text NOT NULL,
    guard jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (before_operation_key <> after_operation_key),
    UNIQUE (run_id, before_operation_key, after_operation_key),
    FOREIGN KEY (run_id, before_operation_key)
        REFERENCES route_operations (run_id, operation_key) ON DELETE CASCADE,
    FOREIGN KEY (run_id, after_operation_key)
        REFERENCES route_operations (run_id, operation_key) ON DELETE CASCADE
);

CREATE TABLE route_issues (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('error', 'candidates')),
    code text NOT NULL,
    message text NOT NULL,
    affected_operation_keys jsonb NOT NULL DEFAULT '[]'::jsonb,
    missing_facts jsonb NOT NULL DEFAULT '[]'::jsonb,
    candidate_options jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX route_issues_run_idx ON route_issues (run_id, kind, code);

CREATE TABLE route_evaluations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    evaluator_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('pass', 'fail', 'candidates', 'error')),
    report jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, evaluator_version)
);
