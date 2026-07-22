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
    assert any(
        "工艺路线 · 事实依据" in line and "工艺路线 · 完整证据" in line
        for line in output.splitlines()
    )
    assert "规则修订" not in output
    assert "第 1 条证据完整原文" in output
    assert "第 2 条证据完整原文" in output
    assert "第 3 条证据完整原文" in output
    assert "另有" not in output
    assert "…" not in output


def test_route_table_stacks_evidence_at_narrow_width(monkeypatch: object) -> None:
    evidence = SimpleNamespace(
        source_type="drawing",
        page=1,
        region="技术要求第3条",
        text="外露焊缝磨平抛光。",
    )
    fact = SimpleNamespace(
        label="焊缝修平要求",
        status="hit",
        value=True,
        evidence=[evidence],
    )
    decision = SimpleNamespace(
        question="明确要求修平的首道焊缝如何整饰？",
        selected_option="抛光",
        result_status="resolved",
        decisive_facts=[fact],
    )
    operation = SimpleNamespace(
        sequence=1,
        process_name="抛光",
        decisions=[decision],
    )
    stream = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=stream, width=88, color_system=None),
    )

    cli._print_route_table([operation], title="已确定的局部工序")

    output = stream.getvalue()
    assert "已确定的局部工序 · 事实依据" in output
    assert "已确定的局部工序 · 完整证据" in output
    assert not any(
        "事实依据" in line and "完整证据" in line for line in output.splitlines()
    )


def test_partial_route_is_grouped_in_rounded_panel(monkeypatch: object) -> None:
    operation = SimpleNamespace(
        sequence=1,
        process_name="焊接(校正)",
        decisions=[],
    )
    recommendation = SimpleNamespace(
        status="partial",
        route=[operation],
        route_candidates=[],
        local_issues=[],
    )
    stream = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=stream, width=160, color_system=None),
    )

    cli._print_recommendation(recommendation)

    output = stream.getvalue()
    assert output.count("╭") == 1
    assert output.count("╰") == 1
    assert "所属：已确定的局部工序" in output
    assert "已确定的局部工序 · 事实依据" not in output


def test_complete_route_is_grouped_in_rounded_panel(monkeypatch: object) -> None:
    operation = SimpleNamespace(
        sequence=1,
        process_name="锯床下料",
        decisions=[],
    )
    recommendation = SimpleNamespace(
        status="complete",
        route=[operation],
        route_candidates=[],
        local_issues=[],
    )
    stream = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=stream, width=160, color_system=None),
    )

    cli._print_recommendation(recommendation)

    output = stream.getvalue()
    assert output.count("╭") == 1
    assert output.count("╰") == 1
    assert "所属：工艺路线" in output


def test_partial_order_candidates_are_visibly_distinct(
    monkeypatch: object,
) -> None:
    def operation(sequence: int, name: str) -> SimpleNamespace:
        return SimpleNamespace(sequence=sequence, process_name=name, decisions=[])

    recommendation = SimpleNamespace(
        status="partial",
        route=[operation(1, "焊接(校正)")],
        route_candidates=[
            SimpleNamespace(
                route_candidate_id="finish-first",
                operations=[
                    operation(1, "焊接(校正)"),
                    operation(2, "抛光"),
                    operation(3, "镗"),
                ],
            ),
            SimpleNamespace(
                route_candidate_id="precision-first",
                operations=[
                    operation(1, "焊接(校正)"),
                    operation(2, "镗"),
                    operation(3, "抛光"),
                ],
            ),
        ],
        local_issues=[],
    )
    stream = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=stream, width=240, color_system=None),
    )

    cli._print_recommendation(recommendation)

    output = stream.getvalue()
    assert "已确定的顺序前缀" in output
    assert "局部顺序候选 1" in output
    assert "局部顺序候选 2" in output
    assert "已确定的局部工序" not in output
    assert output.count("╭") == 2
    assert output.count("╰") == 2
    assert "所属：局部顺序候选 1 · 候选编号：finish-first" in output
    assert "所属：局部顺序候选 2 · 候选编号：precision-first" in output


def test_local_issue_table_wraps_without_truncation(monkeypatch: object) -> None:
    message = (
        "PDF明确防锈/防腐义务时必须同时给出可验证的具体方法；"
        "只有防锈处理时保持部分结果，禁止按历史路线补成喷塑。"
    )
    missing_fact = "surface_protection_method:unable_to_judge"
    recommendation = SimpleNamespace(
        status="partial",
        route=None,
        route_candidates=[],
        local_issues=[
            SimpleNamespace(
                kind="error",
                code="DECISION_FACT_UNRESOLVED",
                location="3/3.4",
                message=message,
                missing_facts=[missing_fact],
            )
        ],
    )
    stream = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=stream, width=88, color_system=None),
    )

    cli._print_recommendation(recommendation)

    output = stream.getvalue()
    assert "PDF明确防锈/防腐义务" in output
    assert "禁止按历史路线补成喷塑。" in output
    assert "surface_protection_method" in output
    assert "unable_to_judge" in output
    assert "DECISION_FACT_UNRESOLVED" in output
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


def test_missing_reference_route_is_printed_explicitly(monkeypatch: object) -> None:
    stream = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=stream, width=88, color_system=None),
    )

    cli._print_reference_routes([])

    assert stream.getvalue().strip() == "参考路线：未提供"


def test_pdf_stem_loads_reference_route_outside_project_directory(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    references = cli._reference_sequences(
        Path("/drawings/DEMO-WELD-001.pdf"),
        evaluation=None,
    )

    assert references == [["焊接(校正)", "镗", "抛光", "喷塑", "转部装"]]


def test_route_prints_reference_answer_last_outside_project_directory(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "DEMO-WELD-001.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    workflow = SimpleNamespace(
        run_id="run",
        recommendation=SimpleNamespace(
            status="partial",
            route=None,
            route_candidates=[],
            local_issues=[],
        ),
        elapsed_seconds=1.0,
        reader_seconds=0.8,
        reader_executions=[],
    )

    async def run_drawing(*args: object, **kwargs: object) -> SimpleNamespace:
        return workflow

    monkeypatch.setattr(cli, "get_settings", lambda: object())
    monkeypatch.setattr(cli, "run_drawing", run_drawing)
    monkeypatch.setattr(cli, "_workflow_cn", lambda workflow: {})
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["route", str(pdf)])

    assert result.exit_code == 0
    assert result.stdout.rstrip().endswith(
        "参考路线：焊接(校正) → 镗 → 抛光 → 喷塑 → 转部装"
    )


def test_route_cli_exposes_no_non_pdf_inference_inputs() -> None:
    result = runner.invoke(cli.app, ["route", "--help"])

    assert result.exit_code == 0
    assert "--external-facts" not in result.stdout
    assert "--material-code" not in result.stdout
