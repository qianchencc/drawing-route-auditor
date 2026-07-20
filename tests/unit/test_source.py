from collections import Counter
from pathlib import Path

from drawing_route_auditor.decision_tree.source import load_decision_tree_source


SOURCE_PATH = Path("docs/1.json")


def test_parses_all_source_rows_and_merged_cells() -> None:
    source = load_decision_tree_source(SOURCE_PATH)

    assert len(source.rows) == 21
    assert Counter(row.node_ref for row in source.rows) == {
        "1": 2,
        "2": 3,
        "3": 3,
        "4": 2,
        "5": 6,
        "6": 1,
        "7": 1,
        "8": 1,
        "9": 1,
        "10": 1,
    }

    merged_row = source.rows[1]
    assert merged_row.predecessor_ref == "0"
    assert merged_row.node_ref == "1"
    assert merged_row.node_title == "查看图纸判断图纸类型"
    assert merged_row.branch_ref == "1.2"


def test_preserves_visual_formatting_metadata() -> None:
    source = load_decision_tree_source(SOURCE_PATH)

    yellow_row = source.rows[6]
    red_text_row = source.rows[9]

    yellow_cells = yellow_row.formatting["cells"]
    assert any(cell.get("fill") == "#FFFF00" for cell in yellow_cells)

    red_runs = [
        run
        for cell in red_text_row.formatting["cells"]
        for run in cell.get("text_runs", [])
    ]
    assert any(run.get("color") == "#FF0000" for run in red_runs)
