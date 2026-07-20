from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


EXPECTED_COLUMNS = (
    ("c1", "序号"),
    ("c2", "紧前序"),
    ("c3", "序号"),
    ("c4", "思维链"),
    ("c5", "分支号"),
    ("c6", "思考过程"),
    ("c7", "判断规则"),
)


@dataclass(frozen=True, slots=True)
class SourceRow:
    row_number: int
    serial_text: str | None
    predecessor_ref: str | None
    node_ref: str
    node_title: str
    branch_ref: str | None
    thought: str | None
    rule_text: str | None
    raw_cells: tuple[dict[str, Any], ...]
    formatting: dict[str, Any]
    source_row: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DecisionTreeSource:
    path: Path
    payload: dict[str, Any]
    rows: tuple[SourceRow, ...]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_decision_tree_source(path: Path) -> DecisionTreeSource:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    actual_columns = tuple(
        (column.get("id"), column.get("header"))
        for column in payload.get("columns", [])
    )
    if actual_columns != EXPECTED_COLUMNS:
        raise ValueError(
            f"Unexpected decision-tree columns: {actual_columns!r}"
        )

    source_rows = payload.get("rows")
    if not isinstance(source_rows, list) or not source_rows:
        raise ValueError("Decision-tree source must contain non-empty rows")

    resolved_cells: dict[str, str | None] = {}
    parsed_rows: list[SourceRow] = []

    for expected_row_number, source_row in enumerate(source_rows, start=1):
        row_number = source_row.get("row")
        if row_number != expected_row_number:
            raise ValueError(
                f"Expected row {expected_row_number}, found {row_number!r}"
            )

        cells = source_row.get("cells")
        if not isinstance(cells, list) or len(cells) != len(EXPECTED_COLUMNS):
            raise ValueError(
                f"Row {row_number} must contain {len(EXPECTED_COLUMNS)} cells"
            )

        values: list[str | None] = []
        formatting_cells: list[dict[str, Any]] = []

        for column_number, cell in enumerate(cells, start=1):
            coordinate = f"R{row_number}C{column_number}"
            merged_into = cell.get("merged_into")
            if merged_into is not None:
                if merged_into not in resolved_cells:
                    raise ValueError(
                        f"{coordinate} references unknown merged cell {merged_into}"
                    )
                text = resolved_cells[merged_into]
            else:
                text = _optional_text(cell.get("text"))
                resolved_cells[coordinate] = text

            values.append(text)
            style = {
                key: cell[key]
                for key in ("fill", "text_runs", "visual_lines", "row_span")
                if key in cell
            }
            if style:
                formatting_cells.append(
                    {"column": column_number, **style}
                )

        node_ref = values[2]
        node_title = values[3]
        if node_ref is None or node_title is None:
            raise ValueError(f"Row {row_number} is missing its logical node")
        try:
            int(node_ref)
        except ValueError as error:
            raise ValueError(
                f"Row {row_number} has non-numeric node reference {node_ref!r}"
            ) from error

        parsed_rows.append(
            SourceRow(
                row_number=row_number,
                serial_text=values[0],
                predecessor_ref=values[1],
                node_ref=node_ref,
                node_title=node_title,
                branch_ref=values[4],
                thought=values[5],
                rule_text=values[6],
                raw_cells=tuple(cells),
                formatting={
                    "cells": formatting_cells,
                    "legend": payload.get("formatting_legend", {}),
                },
                source_row=source_row,
            )
        )

    return DecisionTreeSource(
        path=path,
        payload=payload,
        rows=tuple(parsed_rows),
    )
