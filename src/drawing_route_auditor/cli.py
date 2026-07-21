import asyncio
import json
from pathlib import Path
from shutil import which
from typing import Annotated, Any

import typer
from psycopg2 import Error as DatabaseError
from rich.console import Console
from rich.table import Table

from drawing_route_auditor.config import get_settings
from drawing_route_auditor.db.connection import connect, wait_for_database
from drawing_route_auditor.db.migrations import current_versions, load_migrations, migrate
from drawing_route_auditor.decision_tree.editor import apply_tree_patch
from drawing_route_auditor.decision_tree.importer import initialize_decision_tree
from drawing_route_auditor.decision_tree.repository import (
    evaluate_tree,
    list_trees,
    tree_details,
    validate_tree,
)
from drawing_route_auditor.workflow.models import DrawingInput, ProgressCallback, WorkflowProgress
from drawing_route_auditor.workflow.runner import (
    DEFAULT_TREE_KEY,
    run_and_evaluate,
    run_drawing,
)


app = typer.Typer(
    name="draw-route",
    no_args_is_help=True,
    add_completion=False,
    help="从制造图纸生成可审计工艺路线的工具。",
)
db_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="管理 PostgreSQL 基础设施。",
)
tree_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="维护当前决策树。",
)
app.add_typer(db_app, name="db")
app.add_typer(tree_app, name="tree")

console = Console()
error_console = Console(stderr=True, style="bold red")

_STATUS_CN = {
    "queued": "排队中",
    "running": "运行中",
    "succeeded": "成功",
    "complete": "完成",
    "complete_with_candidates": "含候选的完成",
    "partial": "部分完成",
    "error": "错误",
    "cancelled": "已取消",
    "candidate": "候选",
    "candidates": "候选",
    "resolved": "已确认",
    "draft": "草稿",
    "active": "已启用",
    "retired": "已退役",
    "ready": "就绪",
    "blocked": "阻塞",
    "conditional": "条件成立",
    "invalid": "无效",
    "pass": "通过",
    "fail": "未通过",
    "hit": "命中",
    "not_hit": "未命中",
    "unable_to_judge": "无法判断",
    "conflict": "冲突",
}
_KIND_CN = {
    "ERROR": "错误",
    "CANDIDATES": "候选",
    "error": "错误",
    "candidates": "候选",
}
_SOURCE_KIND_CN = {
    "observed_drawing": "图纸观察事实",
    "external": "外部事实",
    "derived": "派生事实",
}
_SUBJECT_SCOPE_CN = {
    "current_object": "当前对象",
    "bom_item": "BOM 项",
    "bom_link": "BOM 关联",
    "occurrence": "出现位置",
    "drawing_text": "图纸文字",
}
_VALUE_TYPE_CN = {
    "boolean": "布尔值",
    "text": "文本",
    "number": "数值",
    "text_array": "文本数组",
}
_NODE_KIND_CN = {
    "classification": "分类判断",
    "route_generation": "路线生成",
    "calculation": "字段计算",
}
_MAINTENANCE_CN = {
    "complete": "完整",
    "needs_review": "需要复核",
    "incomplete": "不完整",
    "executable": "可执行",
}
_CONFIDENCE_CN = {
    "certain": "确定",
    "candidate": "候选",
    "unknown": "未知",
}
_OPERATOR_CN = {
    "eq": "等于",
    "neq": "不等于",
    "starts_with": "开头为",
    "not_starts_with": "开头不是",
    "contains": "包含",
    "in": "属于",
    "lt": "小于",
    "lte": "小于等于",
    "gt": "大于",
    "gte": "大于等于",
}
_OUTCOME_CN = {
    "fact": "事实",
    "route_family": "路线族",
    "process": "工序",
    "stage": "阶段",
    "error": "错误",
}


def _cn_status(value: str) -> str:
    return _STATUS_CN.get(value, value)


def _json_default(value: Any) -> str:
    return str(value)


def _emit_json(payload: Any) -> None:
    typer.echo(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )


def _abort(message: str, *, code: int = 1) -> None:
    error_console.print(message)
    raise typer.Exit(code=code)


def _observation_cn(observation: Any) -> dict[str, object]:
    return {
        "事实键": observation.fact_key,
        "判断对象": observation.subject_ref,
        "状态": _cn_status(observation.status),
        "值": observation.value,
        "证据": [
            {
                "页码": item.page,
                "区域": item.region,
                "原文": item.text,
            }
            for item in observation.evidence
        ],
        "观察覆盖完整": observation.coverage_complete,
    }


def _reader_execution_cn(execution: Any) -> dict[str, object]:
    return {
        "读取器键": execution.reader_key,
        "读取器名称": execution.reader_label,
        "状态": _cn_status(execution.status),
        "耗时秒": execution.duration_seconds,
        "输入令牌": execution.prompt_tokens,
        "输出令牌": execution.completion_tokens,
        "错误码": execution.error_code,
        "错误信息": execution.error_message,
        "观察事实": (
            [_observation_cn(item) for item in execution.response.observations]
            if execution.response is not None
            else []
        ),
    }


def _rule_match_cn(match: Any) -> dict[str, object]:
    return {
        "节点": match.node_key,
        "分支": match.branch_key,
        "规则键": match.rule_key,
        "决策键": match.decision_key,
        "问题": match.question,
        "选项键": match.option_key,
        "选项": match.option_label,
        "优先级": match.priority,
        "状态": _cn_status(match.result_status),
        "结果类型": _OUTCOME_CN.get(match.outcome_type, match.outcome_type),
        "结果键": match.outcome_key,
        "结果值": match.outcome_value,
        "决定性事实": match.decisive_facts,
        "原因": match.reason,
        "缺失事实": match.missing_facts,
    }


def _decision_fact_cn(fact: Any) -> dict[str, object]:
    return {
        "事实键": fact.fact_key,
        "事实名称": fact.label,
        "状态": _cn_status(fact.status),
        "值": fact.value,
        "图纸证据": [
            {
                "页码": evidence.page,
                "区域": evidence.region,
                "原文": evidence.text,
            }
            for evidence in fact.evidence
        ],
    }


def _operation_decision_cn(decision: Any) -> dict[str, object]:
    return {
        "规则键": decision.rule_key,
        "决策键": decision.decision_key,
        "问题": decision.question,
        "已选选项": decision.selected_option,
        "其他选项": decision.alternative_options,
        "性质": _cn_status(decision.result_status),
        "规则说明": decision.reason,
        "缺失事实": decision.missing_facts,
        "事实依据": [_decision_fact_cn(item) for item in decision.decisive_facts],
    }


def _operation_cn(operation: Any) -> dict[str, object]:
    return {
        "工序号": operation.sequence,
        "工序实例键": operation.operation_key,
        "工序名称": operation.process_name,
        "来源规则": operation.source_rule_keys,
        "工序决策": [_operation_decision_cn(item) for item in operation.decisions],
    }


def _candidate_differences_cn(recommendation: Any) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    for candidate in recommendation.route_candidates:
        for operation in candidate.operations:
            for decision in operation.decisions:
                if decision.result_status != "candidate":
                    continue
                group = groups.setdefault(
                    decision.decision_key,
                    {
                        "决策键": decision.decision_key,
                        "问题": decision.question,
                        "候选选项": set(),
                        "影响工序": set(),
                        "仍需事实": set(),
                    },
                )
                group["候选选项"].add(decision.selected_option)
                group["影响工序"].add(operation.process_name)
                missing = {item.split(":", 1)[0] for item in decision.missing_facts}
                if not missing:
                    missing = {
                        fact.fact_key
                        for fact in decision.decisive_facts
                        if fact.status in {"unable_to_judge", "conflict"}
                    }
                group["仍需事实"].update(missing)
    return [
        {
            "决策键": group["决策键"],
            "问题": group["问题"],
            "候选选项": sorted(group["候选选项"]),
            "影响工序": sorted(group["影响工序"]),
            "仍需事实": sorted(group["仍需事实"]),
        }
        for group in groups.values()
    ]


def _issue_cn(issue: Any) -> dict[str, object]:
    return {
        "类型": _KIND_CN.get(issue.kind, issue.kind),
        "错误码": issue.code,
        "位置": issue.location,
        "说明": issue.message,
        "缺失事实": issue.missing_facts,
    }


def _recommendation_cn(recommendation: Any) -> dict[str, object]:
    return {
        "状态": _cn_status(recommendation.status),
        "确定路线": (
            [_operation_cn(item) for item in recommendation.route]
            if recommendation.route is not None
            else None
        ),
        "候选路线": [
            {
                "候选路线编号": candidate.route_candidate_id,
                "工序": [_operation_cn(item) for item in candidate.operations],
            }
            for candidate in recommendation.route_candidates
        ],
        "候选差异": _candidate_differences_cn(recommendation),
        "局部问题": [_issue_cn(item) for item in recommendation.local_issues],
    }


def _workflow_cn(workflow: Any) -> dict[str, object]:
    return {
        "运行编号": workflow.run_id,
        "输入": {
            "PDF路径": str(workflow.drawing_input.pdf_path),
            "物料编码": workflow.drawing_input.material_code,
            "图纸哈希": workflow.drawing_sha256,
        },
        "决策树键": workflow.tree_key,
        "模型版本": workflow.model_version,
        "提示词模板版本": workflow.prompt_template_version,
        "读取器结果": [
            _reader_execution_cn(item) for item in workflow.reader_executions
        ],
        "派生事实": [
            {"事实键": key, "事实值": value}
            for key, value in workflow.derived_facts.items()
        ],
        "规则命中": [_rule_match_cn(item) for item in workflow.rule_matches],
        "路线推荐": _recommendation_cn(workflow.recommendation),
        "总耗时秒": workflow.elapsed_seconds,
        "渲染耗时秒": workflow.render_seconds,
        "读取器耗时秒": workflow.reader_seconds,
    }


def _evaluation_cn(evaluation: Any) -> dict[str, object]:
    return {
        "物料编码": evaluation.material_code,
        "状态": _cn_status(evaluation.status),
        "工序序列一致": evaluation.operation_sequences_match,
        "推荐序列": evaluation.predicted_sequences,
        "标准序列": evaluation.expected_sequences,
        "缺失序列": evaluation.missing_sequences,
        "多出序列": evaluation.extra_sequences,
        "未解决路线问题": evaluation.unresolved_route_issues,
    }


@db_app.command("wait", help="等待 PostgreSQL 就绪。")
def db_wait(
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=1, help="最长等待秒数。"),
    ] = 30,
) -> None:
    try:
        elapsed = wait_for_database(timeout_seconds=timeout)
    except TimeoutError as error:
        _abort(str(error))
    console.print(f"PostgreSQL 已就绪（{elapsed:.2f} 秒）。")


@db_app.command("migrate", help="应用尚未执行的数据库迁移。")
def db_migrate() -> None:
    try:
        with connect(autocommit=True) as connection:
            result = migrate(connection)
    except (DatabaseError, RuntimeError, ValueError) as error:
        _abort(f"数据库迁移失败：{error}")
    if result.applied:
        console.print(f"本次应用迁移：{', '.join(result.applied)}")
    else:
        console.print("数据库结构已是最新。")
    console.print(f"当前迁移：{', '.join(result.current) or '-'}")


@db_app.command("current", help="显示当前数据库迁移版本。")
def db_current() -> None:
    try:
        with connect(autocommit=True) as connection:
            versions = current_versions(connection)
    except DatabaseError as error:
        _abort(f"读取迁移状态失败：{error}")
    console.print("\n".join(versions) if versions else "尚未应用迁移。")


@app.command("doctor", help="检查数据库、模型和 PDF 渲染器。")
def doctor() -> None:
    checks: list[tuple[str, str, str]] = []
    try:
        elapsed = wait_for_database(timeout_seconds=5)
        checks.append(("PostgreSQL", "正常", f"{elapsed:.2f} 秒就绪"))
        with connect(autocommit=True) as connection:
            applied = current_versions(connection)
        available = tuple(item.version for item in load_migrations())
        checks.append(
            (
                "数据库迁移",
                "正常" if applied == available else "错误",
                f"已应用={list(applied)}，可用={list(available)}",
            )
        )
    except (DatabaseError, RuntimeError, TimeoutError) as error:
        checks.append(("PostgreSQL", "错误", str(error)))

    settings = get_settings()
    model_ready = all(
        (
            settings.openai_base_url,
            settings.openai_api_key,
            settings.vision_model,
        )
    )
    checks.append(
        (
            "视觉模型",
            "正常" if model_ready else "错误",
            (
                f"模型={settings.vision_model}"
                if model_ready
                else "缺少 OPENAI_BASE_URL、OPENAI_API_KEY 或 MODEL"
            ),
        )
    )
    renderer = which("pdftoppm")
    checks.append(
        (
            "PDF 渲染器",
            "正常" if renderer else "错误",
            renderer or "未安装 pdftoppm",
        )
    )

    table = Table(title="环境检查")
    for heading in ("检查项", "状态", "详情"):
        table.add_column(heading)
    for name, status, detail in checks:
        style = "green" if status == "正常" else "red"
        table.add_row(name, f"[{style}]{status}[/{style}]", detail)
    console.print(table)
    if any(status == "错误" for _, status, _ in checks):
        raise typer.Exit(code=1)


@app.command("route", help="读取 PDF 图纸并生成工艺路线。")
def route(
    pdf: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="待分析的 PDF 图纸文件。",
            metavar="PDF文件",
        ),
    ],
    material_code: Annotated[
        str | None,
        typer.Option("--material-code", help="开发评估使用的物料编码。"),
    ] = None,
    external_facts: Annotated[
        Path | None,
        typer.Option(
            "--external-facts",
            exists=True,
            dir_okay=False,
            readable=True,
            help="当前决策树声明的外部事实 JSON。",
        ),
    ] = None,
    tree_key: Annotated[
        str,
        typer.Option("--tree-key", help="决策树稳定键。"),
    ] = DEFAULT_TREE_KEY,
    evaluate: Annotated[
        bool,
        typer.Option("--evaluate/--no-evaluate", help="是否后置加载开发答案。"),
    ] = False,
    output_format: Annotated[
        str,
        typer.Option("--format", help="输出格式：table 或 json。"),
    ] = "table",
    require_complete: Annotated[
        bool,
        typer.Option(
            "--require-complete",
            help="路线不完整或开发评估未通过时返回非零退出码。",
        ),
    ] = False,
) -> None:
    if output_format not in {"table", "json"}:
        _abort("--format 只能是 table 或 json", code=2)
    if evaluate and material_code is None:
        _abort("--evaluate 需要同时提供 --material-code", code=2)

    async def execute(
        progress_callback: ProgressCallback | None,
    ) -> tuple[Any, Any | None]:
        if evaluate:
            return await run_and_evaluate(
                drawing_input,
                settings,
                tree_key=tree_key,
                progress_callback=progress_callback,
            )
        workflow = await run_drawing(
            drawing_input,
            settings,
            tree_key=tree_key,
            progress_callback=progress_callback,
        )
        return workflow, None

    try:
        settings = get_settings()
        external_payload = (
            json.loads(external_facts.read_text(encoding="utf-8"))
            if external_facts is not None
            else {}
        )
        drawing_input = DrawingInput(
            pdf_path=pdf,
            material_code=material_code,
            external_facts=external_payload,
        )
        if output_format == "table":
            with console.status(
                "[cyan]正在初始化图纸工作流[/cyan]",
                spinner="dots",
            ) as status:

                def update_progress(event: WorkflowProgress) -> None:
                    status.update(f"[cyan]{event.message}[/cyan]")

                workflow, evaluation = asyncio.run(execute(update_progress))
        else:
            workflow, evaluation = asyncio.run(execute(None))
    except Exception as error:
        _abort(f"图纸路线运行失败：{error}")

    payload = {
        "运行结果": _workflow_cn(workflow),
        "开发评估": _evaluation_cn(evaluation) if evaluation else None,
    }
    if output_format == "json":
        _emit_json(payload)
    elif output_format == "table":
        console.print(
            f"运行编号 [bold]{workflow.run_id}[/bold]  "
            f"状态={_cn_status(workflow.recommendation.status)}  "
            f"总耗时={workflow.elapsed_seconds:.2f} 秒  "
            f"读取器耗时={workflow.reader_seconds:.2f} 秒"
        )
        reader_table = Table(title="读取器结果")
        for heading in ("读取器", "状态", "观察数", "耗时秒"):
            reader_table.add_column(heading)
        for execution in workflow.reader_executions:
            reader_table.add_row(
                execution.reader_label,
                _cn_status(execution.status),
                str(
                    len(execution.response.observations)
                    if execution.response is not None
                    else 0
                ),
                f"{execution.duration_seconds:.2f}",
            )
        console.print(reader_table)
        _print_recommendation(workflow.recommendation)
        if evaluation is not None:
            console.print(
                f"开发评估：{_cn_status(evaluation.status)}  "
                f"推荐={evaluation.predicted_sequences}  "
                f"标准={evaluation.expected_sequences}"
            )

    complete = workflow.recommendation.status in {
        "complete",
        "complete_with_candidates",
    }
    evaluation_ok = evaluation is None or evaluation.status in {
        "pass",
        "candidates",
    }
    if require_complete and (not complete or not evaluation_ok):
        raise typer.Exit(code=3)


def _short_text(value: str, limit: int = 48) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _display_value(value: object | None) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    if value is None:
        return "未知"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _operation_nature(operation: Any) -> str:
    return (
        "候选"
        if any(item.result_status == "candidate" for item in operation.decisions)
        else "确定"
    )


def _operation_decision_text(operation: Any) -> str:
    if not operation.decisions:
        return "—"
    return "\n".join(
        f"{item.question} → {item.selected_option}" for item in operation.decisions
    )


def _operation_fact_text(operation: Any) -> str:
    facts: dict[str, str] = {}
    for decision in operation.decisions:
        for fact in decision.decisive_facts:
            if fact.status in {"unable_to_judge", "conflict"}:
                value = _cn_status(fact.status)
            else:
                value = _display_value(fact.value)
            facts.setdefault(fact.label, value)
    return "；".join(f"{key}={value}" for key, value in facts.items()) or "—"


def _operation_evidence_text(operation: Any) -> str:
    evidence_rows: list[str] = []
    known: set[tuple[int, str, str]] = set()
    for decision in operation.decisions:
        for fact in decision.decisive_facts:
            for evidence in fact.evidence:
                signature = (evidence.page, evidence.region, evidence.text)
                if signature in known:
                    continue
                known.add(signature)
                evidence_rows.append(
                    f"第{evidence.page}页·{evidence.region}："
                    f"{_short_text(evidence.text)}"
                )
    if not evidence_rows:
        return "—"
    visible = evidence_rows[:2]
    if len(evidence_rows) > 2:
        visible.append(f"另有 {len(evidence_rows) - 2} 条证据")
    return "\n".join(visible)


def _print_route_table(
    operations: list[Any],
    *,
    title: str,
    caption: str | None = None,
) -> None:
    table = Table(title=title, caption=caption, expand=True, show_lines=True)
    table.add_column("序号", justify="right", no_wrap=True)
    table.add_column("工序", no_wrap=True)
    table.add_column("性质", no_wrap=True)
    table.add_column("决策")
    table.add_column("事实依据")
    table.add_column("图纸证据")
    for operation in operations:
        table.add_row(
            str(operation.sequence),
            operation.process_name,
            _operation_nature(operation),
            _operation_decision_text(operation),
            _operation_fact_text(operation),
            _operation_evidence_text(operation),
        )
    console.print(table)


def _print_recommendation(recommendation: Any) -> None:
    if recommendation.route is not None:
        title = "已确定的局部工序" if recommendation.status == "partial" else "工艺路线"
        _print_route_table(recommendation.route, title=title)

    for index, candidate in enumerate(recommendation.route_candidates, start=1):
        _print_route_table(
            candidate.operations,
            title=f"工艺路线 {index}",
            caption=f"候选编号：{candidate.route_candidate_id}",
        )

    differences = _candidate_differences_cn(recommendation)
    if differences:
        table = Table(title="候选路线差异", expand=True)
        for heading in ("决策点", "候选选项", "影响工序", "仍需事实"):
            table.add_column(heading)
        for item in differences:
            table.add_row(
                str(item["问题"]),
                ", ".join(item["候选选项"]),
                ", ".join(item["影响工序"]),
                ", ".join(item["仍需事实"]) or "规则保留多个选项",
            )
        console.print(table)

    if recommendation.local_issues:
        table = Table(title="局部问题")
        for heading in ("类型", "错误码", "位置", "说明", "缺失事实"):
            table.add_column(heading)
        for issue in recommendation.local_issues:
            table.add_row(
                _KIND_CN.get(issue.kind, issue.kind),
                issue.code,
                issue.location,
                issue.message,
                ", ".join(issue.missing_facts),
            )
        console.print(table)


@tree_app.command("init", help="从完整定义初始化空决策树。")
def tree_init(
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="完整决策树 JSON 定义文件。",
            metavar="定义文件",
        ),
    ],
    output_format: Annotated[
        str,
        typer.Option("--format", help="输出格式：table 或 json。"),
    ] = "table",
) -> None:
    if output_format not in {"table", "json"}:
        _abort("--format 只能是 table 或 json", code=2)
    try:
        with connect() as connection:
            summary = initialize_decision_tree(connection, source)
    except (DatabaseError, OSError, ValueError, RuntimeError) as error:
        _abort(f"决策树初始化失败：{error}")

    payload = {
        "决策树键": summary.tree_key,
        "已更新": summary.changed,
        "读取器数": summary.reader_count,
        "事实数": summary.fact_count,
        "节点数": summary.node_count,
        "分支数": summary.branch_count,
        "规则数": summary.rule_count,
    }
    if output_format == "json":
        _emit_json(payload)
        return
    table = Table(title="当前决策树")
    table.add_column("字段")
    table.add_column("值")
    for key, value in payload.items():
        table.add_row(key, str(value))
    console.print(table)


@tree_app.command("apply", help="原子应用决策树增量补丁。")
def tree_apply(
    patch: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="增量补丁 JSON 文件。",
            metavar="补丁文件",
        ),
    ],
    output_format: Annotated[
        str,
        typer.Option("--format", help="输出格式：table 或 json。"),
    ] = "table",
) -> None:
    if output_format not in {"table", "json"}:
        _abort("--format 只能是 table 或 json", code=2)
    try:
        with connect() as connection:
            summary = apply_tree_patch(connection, patch)
    except (DatabaseError, LookupError, OSError, ValueError, RuntimeError) as error:
        _abort(f"决策树增量更新失败：{error}")

    payload = {
        "决策树键": summary.tree_key,
        "已更新": summary.changed,
        "读取器数": summary.reader_count,
        "事实数": summary.fact_count,
        "节点数": summary.node_count,
        "分支数": summary.branch_count,
        "规则数": summary.rule_count,
    }
    if output_format == "json":
        _emit_json(payload)
        return
    table = Table(title="决策树增量更新")
    table.add_column("字段")
    table.add_column("值")
    for key, value in payload.items():
        table.add_row(key, str(value))
    console.print(table)


@tree_app.command("list", help="列出当前维护的决策树。")
def tree_list(
    output_format: Annotated[
        str,
        typer.Option("--format", help="输出格式：table 或 json。"),
    ] = "table",
) -> None:
    try:
        with connect() as connection:
            rows = list_trees(connection)
    except DatabaseError as error:
        _abort(f"读取决策树列表失败：{error}")
    payload = [
        {
            "决策树键": row["tree_key"],
            "名称": row["name"],
            "节点数": row["node_count"],
            "分支数": row["branch_count"],
            "规则数": row["executable_rule_count"],
        }
        for row in rows
    ]
    if output_format == "json":
        _emit_json(payload)
        return
    if output_format != "table":
        _abort("--format 只能是 table 或 json", code=2)
    table = Table(title="当前决策树")
    for heading in ("决策树键", "名称", "节点", "分支", "规则"):
        table.add_column(heading)
    for item in payload:
        table.add_row(
            str(item["决策树键"]),
            str(item["名称"]),
            str(item["节点数"]),
            str(item["分支数"]),
            str(item["规则数"]),
        )
    console.print(table)


@tree_app.command("show", help="打印当前决策树。")
def tree_show(
    tree_key: Annotated[
        str,
        typer.Argument(help="决策树稳定键。", metavar="决策树键"),
    ] = DEFAULT_TREE_KEY,
    output_format: Annotated[
        str,
        typer.Option("--format", help="输出格式：table 或 json。"),
    ] = "table",
) -> None:
    try:
        with connect() as connection:
            details = tree_details(connection, tree_key)
    except (DatabaseError, LookupError) as error:
        _abort(f"读取决策树详情失败：{error}")
    payload = _tree_details_cn(details)
    if output_format == "json":
        _emit_json(payload)
        return
    if output_format != "table":
        _abort("--format 只能是 table 或 json", code=2)
    console.print(f"决策树 [bold]{tree_key}[/bold]")
    reader_table = Table(title="读取器")
    for heading in ("顺序", "读取器键", "名称", "能力"):
        reader_table.add_column(heading)
    for reader in details["readers"]:
        reader_table.add_row(
            str(reader["sequence"]),
            reader["reader_key"],
            reader["label"],
            reader["capability_definition"],
        )
    console.print(reader_table)
    fact_table = Table(title="事实定义")
    for heading in ("事实键", "中文名称", "来源", "读取器", "对象范围", "值类型"):
        fact_table.add_column(heading)
    for fact in details["facts"]:
        fact_table.add_row(
            fact["fact_key"],
            fact["label"],
            _SOURCE_KIND_CN.get(fact["source_kind"], fact["source_kind"]),
            fact["reader_key"] or "-",
            _SUBJECT_SCOPE_CN.get(fact["subject_scope"], fact["subject_scope"]),
            _VALUE_TYPE_CN.get(fact["value_type"], fact["value_type"]),
        )
    console.print(fact_table)
    node_table = Table(title="节点")
    for heading in ("节点", "名称", "类型", "维护状态"):
        node_table.add_column(heading)
    for node in details["nodes"]:
        node_table.add_row(
            node["node_key"],
            node["title"],
            _NODE_KIND_CN.get(node["node_kind"], node["node_kind"]),
            _MAINTENANCE_CN.get(node["maintenance_status"], node["maintenance_status"]),
        )
    console.print(node_table)
    rule_table = Table(title="规则条件与结果")
    for heading in ("节点/分支", "问题", "选项", "条件", "结果", "状态"):
        rule_table.add_column(heading)
    for rule in details["rules"]:
        conditions = "；".join(
            f"{item['fact_key']} "
            f"{_OPERATOR_CN.get(item['operator'], item['operator'])} "
            f"{item['expected_value']}"
            for item in rule["clauses"]
        )
        result = (
            f"{_OUTCOME_CN.get(rule['outcome_type'], rule['outcome_type'])}:"
            f"{rule['outcome_key']}={rule['outcome_value']}"
        )
        rule_table.add_row(
            f"{rule['node_key']}/{rule['branch_key']}",
            rule["question"],
            rule["option_label"],
            conditions,
            result,
            _cn_status(rule["result_kind"]),
        )
    console.print(rule_table)


def _tree_details_cn(details: dict[str, Any]) -> dict[str, object]:
    return {
        "决策树键": details["tree_key"],
        "名称": details["name"],
        "说明": details["description"],
        "读取器": [
            {
                "读取器键": item["reader_key"],
                "名称": item["label"],
                "能力": item["capability_definition"],
                "顺序": item["sequence"],
            }
            for item in details["readers"]
        ],
        "事实定义": [
            {
                "事实键": item["fact_key"],
                "名称": item["label"],
                "来源类型": _SOURCE_KIND_CN.get(
                    item["source_kind"], item["source_kind"]
                ),
                "读取器键": item["reader_key"],
                "对象范围": _SUBJECT_SCOPE_CN.get(
                    item["subject_scope"], item["subject_scope"]
                ),
                "值类型": _VALUE_TYPE_CN.get(item["value_type"], item["value_type"]),
                "允许值": item["allowed_values"],
                "判断定义": item["judgement_definition"],
            }
            for item in details["facts"]
        ],
        "节点": [
            {
                "节点键": item["node_key"],
                "名称": item["title"],
                "类型": _NODE_KIND_CN.get(item["node_kind"], item["node_kind"]),
                "维护状态": _MAINTENANCE_CN.get(
                    item["maintenance_status"], item["maintenance_status"]
                ),
            }
            for item in details["nodes"]
        ],
        "分支": [
            {
                "节点键": item["node_key"],
                "分支键": item["branch_key"],
                "名称": item["title"],
                "维护状态": _MAINTENANCE_CN.get(
                    item["maintenance_status"], item["maintenance_status"]
                ),
                "置信方式": _CONFIDENCE_CN.get(
                    item["confidence_mode"], item["confidence_mode"]
                ),
                "规则数": item["rule_count"],
            }
            for item in details["branches"]
        ],
        "规则": [
            {
                "节点键": item["node_key"],
                "分支键": item["branch_key"],
                "规则键": item["rule_key"],
                "说明": item["description"],
                "决策键": item["decision_key"],
                "问题": item["question"],
                "选项键": item["option_key"],
                "选项": item["option_label"],
                "状态": _cn_status(item["result_kind"]),
                "结果类型": _OUTCOME_CN.get(item["outcome_type"], item["outcome_type"]),
                "结果键": item["outcome_key"],
                "结果值": item["outcome_value"],
                "条件": [
                    {
                        "事实键": clause["fact_key"],
                        "运算": _OPERATOR_CN.get(
                            clause["operator"], clause["operator"]
                        ),
                        "期望值": clause["expected_value"],
                    }
                    for clause in item["clauses"]
                ],
            }
            for item in details["rules"]
        ],
    }


@tree_app.command("validate", help="校验当前决策树结构。")
def tree_validate(
    tree_key: Annotated[
        str,
        typer.Argument(help="决策树稳定键。", metavar="决策树键"),
    ] = DEFAULT_TREE_KEY,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="存在任何问题时返回非零退出码。"),
    ] = False,
    output_format: Annotated[
        str,
        typer.Option("--format", help="输出格式：table 或 json。"),
    ] = "table",
) -> None:
    try:
        with connect() as connection:
            report = validate_tree(connection, tree_key)
    except (DatabaseError, LookupError) as error:
        _abort(f"验证决策树失败：{error}")
    count_names = {
        "nodes": "节点",
        "branches": "分支",
        "edges": "边",
        "rules": "规则",
        "clauses": "条件",
    }
    payload = {
        "决策树键": report.tree_key,
        "数量": {
            count_names.get(key, key): value for key, value in report.counts.items()
        },
        "问题": [
            {
                "类型": _KIND_CN.get(item.kind, item.kind),
                "错误码": item.code,
                "位置": item.location,
                "说明": item.message,
            }
            for item in report.issues
        ],
    }
    if output_format == "json":
        _emit_json(payload)
    elif output_format == "table":
        console.print(
            f"{tree_key} "
            + " ".join(
                f"{count_names.get(key, key)}={value}"
                for key, value in report.counts.items()
            )
        )
        table = Table(title="验证问题")
        for heading in ("类型", "错误码", "位置", "说明"):
            table.add_column(heading)
        for issue in report.issues:
            style = "red" if issue.kind == "ERROR" else "yellow"
            table.add_row(
                f"[{style}]{_KIND_CN.get(issue.kind, issue.kind)}[/{style}]",
                issue.code,
                issue.location,
                issue.message,
            )
        console.print(table)
    else:
        _abort("--format 只能是 table 或 json", code=2)
    if strict and report.issues:
        raise typer.Exit(code=3)


@tree_app.command("evaluate", help="使用事实 JSON 对决策树进行离线求值。")
def tree_evaluate(
    facts_path: Annotated[
        Path,
        typer.Option(
            "--facts",
            exists=True,
            dir_okay=False,
            readable=True,
            help="事实 JSON 文件。",
        ),
    ],
    tree_key: Annotated[
        str,
        typer.Argument(help="决策树稳定键。", metavar="决策树键"),
    ] = DEFAULT_TREE_KEY,
    output_format: Annotated[
        str,
        typer.Option("--format", help="输出格式：table 或 json。"),
    ] = "table",
) -> None:
    try:
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
        with connect() as connection:
            rows = evaluate_tree(connection, tree_key, facts)
    except (DatabaseError, LookupError, OSError, ValueError) as error:
        _abort(f"决策树求值失败：{error}")
    payload = [
        {
            "节点": row["node_key"],
            "分支": row["branch_key"],
            "规则键": row["rule_key"],
            "决策键": row["decision_key"],
            "问题": row["question"],
            "选项": row["option_label"],
            "状态": _cn_status(row["result_status"]),
            "结果类型": _OUTCOME_CN.get(row["outcome_type"], row["outcome_type"]),
            "结果键": row["outcome_key"],
            "结果值": row["outcome_value"],
            "原因": row["reason"],
            "缺失事实": list(row["missing_facts"]),
        }
        for row in rows
    ]
    if output_format == "json":
        _emit_json(payload)
        return
    if output_format != "table":
        _abort("--format 只能是 table 或 json", code=2)
    table = Table(title="决策树求值")
    for heading in ("状态", "节点", "分支", "问题", "选项", "结果", "原因"):
        table.add_column(heading)
    for item in payload:
        table.add_row(
            str(item["状态"]),
            str(item["节点"]),
            str(item["分支"]),
            str(item["问题"]),
            str(item["选项"]),
            f"{item['结果类型']}:{item['结果键']}={item['结果值']}",
            str(item["原因"]),
        )
    console.print(table)




if __name__ == "__main__":
    app()
