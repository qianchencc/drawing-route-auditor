import json
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from rich.console import Console
from typer.testing import CliRunner

import drawing_route_auditor.cli as cli


runner = CliRunner()


@contextmanager
def _connection() -> Iterator[object]:
    yield object()


def _details() -> dict[str, object]:
    return {
        "tree_key": "drawing-process-tree",
        "name": "基础工艺决策树",
        "description": "测试决策树",
        "created_at": None,
        "nodes": [],
        "branches": [],
        "edges": [],
        "readers": [],
        "facts": [],
        "rules": [],
    }


def test_tree_show_reads_current_customer_tree(monkeypatch: object) -> None:
    requested: list[str] = []

    monkeypatch.setattr(cli, "connect", _connection)

    def tree_details(
        connection: object,
        tree_key: str,
    ) -> dict[str, object]:
        requested.append(tree_key)
        return _details()

    monkeypatch.setattr(cli, "tree_details", tree_details)

    result = runner.invoke(cli.app, ["tree", "show", "--format", "json"])

    assert result.exit_code == 0
    assert requested == ["drawing-process-tree"]
    payload = json.loads(result.stdout)
    assert payload["决策树键"] == "drawing-process-tree"
    assert "版本" not in payload


def test_tree_apply_rejects_format_before_connecting(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    patch = tmp_path / "patch.json"
    patch.write_text("{}", encoding="utf-8")

    def unexpected_connect() -> None:
        raise AssertionError("invalid format must not open a database connection")

    monkeypatch.setattr(cli, "connect", unexpected_connect)

    result = runner.invoke(
        cli.app,
        ["tree", "apply", str(patch), "--format", "yaml"],
    )

    assert result.exit_code == 2
    assert "--format 只能是 table 或 json" in result.stdout


def test_pdf_stem_loads_reference_route_without_evaluation(
    monkeypatch: object,
) -> None:
    sequence = [
        "激光下料",
        "焊接(校正)",
        "卷圆",
        "焊接(校正)",
        "卷圆",
        "转焊接",
    ]
    requested_codes: list[str] = []

    def load_golden_routes(
        material_code: str,
        *,
        route_sources: tuple[Path, ...],
    ) -> tuple[SimpleNamespace, ...]:
        requested_codes.append(material_code)
        assert route_sources == cli.DEFAULT_ROUTE_SOURCES
        return (SimpleNamespace(expected_processes=sequence),)

    monkeypatch.setattr(cli, "load_golden_routes", load_golden_routes)
    stream = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=stream, width=160, color_system=None),
    )

    references = cli._reference_sequences(
        Path("/drawings/DEMO-PLATE-001.pdf"),
        material_code=None,
        evaluation=None,
    )
    cli._print_reference_routes(references)

    assert requested_codes == ["DEMO-PLATE-001"]
    assert references == [sequence]
    output = stream.getvalue()
    assert "参考路线：激光下料 → 焊接(校正) → 卷圆 → 焊接(校正) → 卷圆 → 转焊接" in output
    assert "开发评估" not in output
