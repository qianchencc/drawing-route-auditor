CREATE FUNCTION decision_fact_status(p_facts jsonb, p_fact_key text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN NOT (p_facts ? p_fact_key) THEN 'missing'
        WHEN jsonb_typeof(p_facts -> p_fact_key) = 'object'
             AND (p_facts -> p_fact_key) ? 'status'
            THEN p_facts -> p_fact_key ->> 'status'
        ELSE 'hit'
    END
$$;

CREATE FUNCTION decision_fact_value(p_facts jsonb, p_fact_key text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN NOT (p_facts ? p_fact_key) THEN NULL
        WHEN jsonb_typeof(p_facts -> p_fact_key) = 'object'
             AND (p_facts -> p_fact_key) ? 'value'
            THEN p_facts -> p_fact_key -> 'value'
        WHEN jsonb_typeof(p_facts -> p_fact_key) = 'object'
             AND p_facts -> p_fact_key ->> 'status' = 'not_hit'
            THEN 'false'::jsonb
        ELSE p_facts -> p_fact_key
    END
$$;

CREATE FUNCTION decision_clause_matches(
    p_actual jsonb,
    p_operator text,
    p_expected jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE p_operator
        WHEN 'eq' THEN p_actual = p_expected
        WHEN 'neq' THEN p_actual <> p_expected
        WHEN 'starts_with' THEN
            (p_actual #>> '{}') LIKE (p_expected #>> '{}') || '%'
        WHEN 'not_starts_with' THEN
            (p_actual #>> '{}') NOT LIKE (p_expected #>> '{}') || '%'
        WHEN 'contains' THEN
            position((p_expected #>> '{}') IN (p_actual #>> '{}')) > 0
        WHEN 'in' THEN
            p_expected @> jsonb_build_array(p_actual)
        WHEN 'lt' THEN
            CASE
                WHEN jsonb_typeof(p_actual) = 'number'
                     AND jsonb_typeof(p_expected) = 'number'
                    THEN (p_actual #>> '{}')::numeric < (p_expected #>> '{}')::numeric
                ELSE NULL
            END
        WHEN 'lte' THEN
            CASE
                WHEN jsonb_typeof(p_actual) = 'number'
                     AND jsonb_typeof(p_expected) = 'number'
                    THEN (p_actual #>> '{}')::numeric <= (p_expected #>> '{}')::numeric
                ELSE NULL
            END
        WHEN 'gt' THEN
            CASE
                WHEN jsonb_typeof(p_actual) = 'number'
                     AND jsonb_typeof(p_expected) = 'number'
                    THEN (p_actual #>> '{}')::numeric > (p_expected #>> '{}')::numeric
                ELSE NULL
            END
        WHEN 'gte' THEN
            CASE
                WHEN jsonb_typeof(p_actual) = 'number'
                     AND jsonb_typeof(p_expected) = 'number'
                    THEN (p_actual #>> '{}')::numeric >= (p_expected #>> '{}')::numeric
                ELSE NULL
            END
        ELSE NULL
    END
$$;

CREATE FUNCTION evaluate_decision_tree(
    p_tree_key text,
    p_version integer,
    p_facts jsonb
)
RETURNS TABLE (
    node_key text,
    branch_key text,
    rule_key text,
    result_status text,
    outcome_type text,
    outcome_key text,
    outcome_value jsonb,
    reason text,
    missing_facts text[]
)
LANGUAGE sql
STABLE
AS $$
    WITH selected_rules AS (
        SELECT
            node.node_key,
            branch.branch_key,
            rule.id AS rule_id,
            rule.rule_key,
            rule.description,
            rule.evaluation_mode,
            rule.result_kind,
            rule.outcome_type,
            rule.outcome_key,
            rule.outcome_value,
            rule.missing_behavior,
            rule.priority
        FROM decision_trees AS tree
        JOIN decision_tree_versions AS version ON version.tree_id = tree.id
        JOIN decision_rules AS rule ON rule.version_id = version.id
        JOIN decision_branches AS branch ON branch.id = rule.branch_id
        JOIN decision_nodes AS node ON node.id = branch.node_id
        WHERE tree.tree_key = p_tree_key
          AND version.version = p_version
    ),
    clause_results AS (
        SELECT
            selected.rule_id,
            fact.fact_key,
            p_facts ? fact.fact_key AS fact_provided,
            decision_fact_status(p_facts, fact.fact_key) AS fact_status,
            decision_clause_matches(
                decision_fact_value(p_facts, fact.fact_key),
                clause.operator,
                clause.expected_value
            ) AS matched
        FROM selected_rules AS selected
        JOIN decision_rule_clauses AS clause ON clause.rule_id = selected.rule_id
        JOIN fact_definitions AS fact ON fact.id = clause.fact_definition_id
    ),
    aggregates AS (
        SELECT
            selected.*,
            count(clause.fact_key) AS clause_count,
            count(*) FILTER (
                WHERE clause.fact_provided IS TRUE
            ) AS provided_count,
            count(*) FILTER (
                WHERE clause.fact_status IN ('hit', 'not_hit')
                  AND clause.matched IS TRUE
            ) AS true_count,
            count(*) FILTER (
                WHERE clause.fact_status IN ('hit', 'not_hit')
                  AND clause.matched IS FALSE
            ) AS false_count,
            count(*) FILTER (
                WHERE clause.fact_status NOT IN ('hit', 'not_hit')
                   OR clause.matched IS NULL
            ) AS unknown_count,
            coalesce(
                array_agg(
                    clause.fact_key || ':' || clause.fact_status
                    ORDER BY clause.fact_key
                ) FILTER (
                    WHERE clause.fact_status NOT IN ('hit', 'not_hit')
                       OR clause.matched IS NULL
                ),
                ARRAY[]::text[]
            ) AS unresolved_facts
        FROM selected_rules AS selected
        LEFT JOIN clause_results AS clause ON clause.rule_id = selected.rule_id
        GROUP BY
            selected.node_key,
            selected.branch_key,
            selected.rule_id,
            selected.rule_key,
            selected.description,
            selected.evaluation_mode,
            selected.result_kind,
            selected.outcome_type,
            selected.outcome_key,
            selected.outcome_value,
            selected.missing_behavior,
            selected.priority
    ),
    evaluated AS (
        SELECT
            aggregate.*,
            CASE
                WHEN aggregate.clause_count = 0 THEN aggregate.result_kind
                WHEN aggregate.provided_count = 0 THEN 'not_match'
                WHEN aggregate.evaluation_mode = 'all'
                     AND aggregate.false_count > 0 THEN 'not_match'
                WHEN aggregate.evaluation_mode = 'all'
                     AND aggregate.unknown_count > 0 THEN aggregate.missing_behavior
                WHEN aggregate.evaluation_mode = 'all' THEN aggregate.result_kind
                WHEN aggregate.evaluation_mode = 'any'
                     AND aggregate.true_count > 0 THEN aggregate.result_kind
                WHEN aggregate.evaluation_mode = 'any'
                     AND aggregate.unknown_count > 0 THEN aggregate.missing_behavior
                ELSE 'not_match'
            END AS final_status
        FROM aggregates AS aggregate
    )
    SELECT
        evaluated.node_key,
        evaluated.branch_key,
        evaluated.rule_key,
        evaluated.final_status AS result_status,
        evaluated.outcome_type,
        evaluated.outcome_key,
        evaluated.outcome_value,
        CASE
            WHEN cardinality(evaluated.unresolved_facts) > 0
                THEN evaluated.description
                     || '；缺少或无法确定：'
                     || array_to_string(evaluated.unresolved_facts, ', ')
            ELSE evaluated.description
        END AS reason,
        evaluated.unresolved_facts AS missing_facts
    FROM evaluated
    WHERE evaluated.final_status <> 'not_match'
      AND NOT (
          evaluated.final_status = 'candidate'
          AND EXISTS (
              SELECT 1
              FROM evaluated AS resolved
              WHERE resolved.final_status = 'resolved'
                AND resolved.outcome_type = evaluated.outcome_type
                AND resolved.outcome_key = evaluated.outcome_key
          )
      )
    ORDER BY
        evaluated.node_key::integer,
        evaluated.branch_key,
        evaluated.priority DESC,
        evaluated.rule_key
$$;
