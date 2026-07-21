UPDATE decision_tree_versions AS revision
SET source_payload = jsonb_set(
    jsonb_set(
        revision.source_payload
            - 'version'
            - 'base_source_path'
            - 'base_source_sha256',
        '{nodes}',
        coalesce(
            (
                SELECT jsonb_agg(
                    item.value
                        - 'source_predecessor_ref'
                        - 'source_row_start'
                        - 'source_row_end'
                    ORDER BY item.ordinality
                )
                FROM jsonb_array_elements(revision.source_payload -> 'nodes')
                    WITH ORDINALITY AS item(value, ordinality)
            ),
            '[]'::jsonb
        )
    ),
    '{branches}',
    coalesce(
        (
            SELECT jsonb_agg(
                item.value - 'thought' - 'source_row_number'
                ORDER BY item.ordinality
            )
            FROM jsonb_array_elements(revision.source_payload -> 'branches')
                WITH ORDINALITY AS item(value, ordinality)
        ),
        '[]'::jsonb
    )
)
WHERE revision.status = 'active';
