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
        "revision": 10,
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
    assert payload["内部修订"] == 10
    assert "版本" not in payload


def test_tree_export_returns_canonical_database_payload(monkeypatch: object) -> None:
    payload = {
        "schema_version": 2,
        "tree_key": "drawing-process-tree",
        "readers": [],
        "facts": [],
        "nodes": [],
        "branches": [],
        "rules": [],
        "edges": [],
    }
    monkeypatch.setattr(cli, "connect", _connection)
    monkeypatch.setattr(cli, "current_tree_payload", lambda connection, key: payload)

    result = runner.invoke(cli.app, ["tree", "export"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == payload


def test_route_json_keeps_tree_revision_but_hides_rule_revision() -> None:
    decision = SimpleNamespace(
        rule_key="rule",
        rule_revision=10,
        decision_key="decision",
        question="question",
        selected_option="option",
        alternative_options=[],
        result_status="resolved",
        reason="reason",
        missing_facts=[],
        decisive_facts=[],
    )
    workflow = SimpleNamespace(
        run_id="run",
        drawing_input=SimpleNamespace(pdf_path=Path("drawing.pdf")),
        drawing_sha256="sha",
        tree_key="drawing-process-tree",
        tree_revision=10,
        model_version="model",
        prompt_template_version="prompt",
        reader_executions=[],
        derived_facts={},
        rule_matches=[],
        recommendation=SimpleNamespace(
            status="error",
            route=None,
            route_candidates=[],
            local_issues=[],
        ),
        elapsed_seconds=1.0,
        render_seconds=0.1,
        reader_seconds=0.8,
    )

    assert "规则内部修订" not in cli._operation_decision_cn(decision)
    assert cli._workflow_cn(workflow)["决策树内部修订"] == 10
    operation = SimpleNamespace(decisions=[decision])
    assert cli._operation_decision_text(operation) == "question → option"


def test_route_table_shows_all_evidence_without_truncation(
    monkeypatch: object,
) -> None:
    evidence = [
        SimpleNamespace(
            source_type="drawing",
            page=1,
            region=f"区域 {index}",
            text=f"第 {index} 条证据完整原文 " + "证据内容" * 20,
        )
        for index in range(1, 4)
    ]
    fact = SimpleNamespace(
        label="原始形态",
        status="hit",
        value="bar",
        evidence=evidence,
    )
    decision = SimpleNamespace(
        question="棒料如何形成毛坯？",
        selected_option="锯床下料",
        result_status="resolved",
        decisive_facts=[fact],
    )
    operation = SimpleNamespace(
        sequence=1,
        process_name="锯床下料",
        decisions=[decision],
    )

    entries = cli._route_evidence_entries([operation])
    assert [entry[0].text for entry in entries] == [item.text for item in evidence]
    assert all(entry[1] == ["原始形态"] for entry in entries)
    assert all(entry[2] == ["1"] for entry in entries)

    stream = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=stream, width=240, color_system=None),
    )
    cli._print_route_table([operation], title="工艺路线")

    output = stream.getvalue()
    assert "工艺路线 · 事实依据" in output
    assert "工艺路线 · 完整证据" in output
    assert "规则修订" not in output
    assert "第 1 条证据完整原文" in output
    assert "第 2 条证据完整原文" in output
    assert "第 3 条证据完整原文" in output
    assert "另有" not in output
    assert "…" not in output


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
        evaluation=None,
    )
    cli._print_reference_routes(references)

    assert requested_codes == ["DEMO-PLATE-001"]
    assert references == [sequence]
    output = stream.getvalue()
    assert (
        "参考路线：激光下料 → 焊接(校正) → 卷圆 → 焊接(校正) → 卷圆 → 转焊接" in output
    )
    assert "开发评估" not in output


def test_route_cli_exposes_no_non_pdf_inference_inputs() -> None:
    result = runner.invoke(cli.app, ["route", "--help"])

    assert result.exit_code == 0
    assert "--external-facts" not in result.stdout
    assert "--material-code" not in result.stdout
