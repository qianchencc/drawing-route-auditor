import json
from contextlib import contextmanager
from typing import Iterator

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
        "version_id": 1,
        "version": 3,
        "status": "active",
        "source_path": "docs/decision_tree_v3.json",
        "source_sha256": "abc",
        "created_at": None,
        "nodes": [],
        "branches": [],
        "edges": [],
        "readers": [],
        "facts": [],
        "rules": [],
    }


def test_tree_show_defaults_to_active_customer_tree(monkeypatch: object) -> None:
    requested: list[tuple[str, int | None]] = []

    monkeypatch.setattr(cli, "connect", _connection)

    def tree_details(
        connection: object,
        tree_key: str,
        version: int | None,
    ) -> dict[str, object]:
        requested.append((tree_key, version))
        return _details()

    monkeypatch.setattr(cli, "tree_details", tree_details)

    result = runner.invoke(cli.app, ["tree", "show", "--format", "json"])

    assert result.exit_code == 0
    assert requested == [("drawing-process-tree", None)]
    payload = json.loads(result.stdout)
    assert payload["决策树键"] == "drawing-process-tree"
    assert payload["版本"] == 3
