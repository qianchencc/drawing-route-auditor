from pathlib import Path

from drawing_route_auditor.decision_tree.catalog import FACTS, RULES
from drawing_route_auditor.decision_tree.source import load_decision_tree_source


def test_curated_rules_reference_known_branches_and_facts() -> None:
    source = load_decision_tree_source(Path("docs/1.json"))
    branch_keys = {row.branch_ref for row in source.rows if row.branch_ref}
    fact_keys = {fact.key for fact in FACTS}

    assert {rule.branch_key for rule in RULES} <= branch_keys
    assert {
        clause.fact_key
        for rule in RULES
        for clause in rule.clauses
    } <= fact_keys
    assert all(rule.description for rule in RULES)
