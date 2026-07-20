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
from drawing_route_auditor.decision_tree.importer import import_decision_tree
from drawing_route_auditor.decision_tree.repository import (
    activate_tree,
    evaluate_tree,
    list_tree_versions,
    tree_details,
    validate_tree,
)
from drawing_route_auditor.workflow.runner import load_case, run_and_evaluate, run_drawing


app = typer.Typer(
    name="draw-route",
    no_args_is_help=True,
    help="Jinnan drawing-to-route infrastructure.",
)
db_app = typer.Typer(no_args_is_help=True, help="Manage PostgreSQL infrastructure.")
tree_app = typer.Typer(no_args_is_help=True, help="Maintain decision trees.")
app.add_typer(db_app, name="db")
app.add_typer(tree_app, name="tree")

console = Console()
error_console = Console(stderr=True, style="bold red")


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


@db_app.command("wait")
def db_wait(
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=1, help="Maximum seconds to wait."),
    ] = 30,
) -> None:
    try:
        elapsed = wait_for_database(timeout_seconds=timeout)
    except TimeoutError as error:
        _abort(str(error))
    console.print(f"PostgreSQL is ready ({elapsed:.2f}s).")


@db_app.command("migrate")
def db_migrate() -> None:
    try:
        with connect(autocommit=True) as connection:
            result = migrate(connection)
    except (DatabaseError, RuntimeError, ValueError) as error:
        _abort(f"Migration failed: {error}")
    if result.applied:
        console.print(f"Applied migrations: {', '.join(result.applied)}")
    else:
        console.print("Database schema is current.")
    console.print(f"Current migrations: {', '.join(result.current) or '-'}")


@db_app.command("current")
def db_current() -> None:
    try:
        with connect(autocommit=True) as connection:
            versions = current_versions(connection)
    except DatabaseError as error:
        _abort(f"Could not read migration state: {error}")
    console.print("\n".join(versions) if versions else "No migrations applied.")


@app.command("doctor")
def doctor() -> None:
    checks: list[tuple[str, str, str]] = []
    try:
        elapsed = wait_for_database(timeout_seconds=5)
        checks.append(("PostgreSQL", "OK", f"ready in {elapsed:.2f}s"))
        with connect(autocommit=True) as connection:
            applied = current_versions(connection)
        available = tuple(migration.version for migration in load_migrations())
        migration_status = "OK" if applied == available else "ERROR"
        checks.append(
            (
                "Migrations",
                migration_status,
                f"applied={list(applied)}, available={list(available)}",
            )
        )
    except (DatabaseError, RuntimeError, TimeoutError) as error:
        checks.append(("PostgreSQL", "ERROR", str(error)))
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
            "Vision model",
            "OK" if model_ready else "ERROR",
            (
                f"model={settings.vision_model}"
                if model_ready
                else "OPENAI_BASE_URL, OPENAI_API_KEY or MODEL is missing"
            ),
        )
    )
    renderer = which("pdftoppm")
    checks.append(
        (
            "PDF renderer",
            "OK" if renderer else "ERROR",
            renderer or "pdftoppm is not installed",
        )
    )


    table = Table(title="jn doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for name, status, detail in checks:
        style = "green" if status == "OK" else "red"
        table.add_row(name, f"[{style}]{status}[/{style}]", detail)
    console.print(table)
    if any(status == "ERROR" for _, status, _ in checks):
        raise typer.Exit(code=1)


@app.command("route")
def route(
    pdf: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True),
    ],
    context: Annotated[
        Path,
        typer.Option("--context", exists=True, dir_okay=False, readable=True),
    ],
    evaluate: Annotated[
        bool,
        typer.Option("--evaluate/--no-evaluate"),
    ] = False,
    output_format: Annotated[str, typer.Option("--format")] = "table",
    require_complete: Annotated[
        bool,
        typer.Option("--require-complete"),
    ] = False,
) -> None:
    try:
        settings = get_settings()
        case = load_case(context).model_copy(update={"pdf_path": pdf})
        if evaluate:
            workflow, evaluation = asyncio.run(
                run_and_evaluate(
                    case,
                    settings,
                    answer_path=(
                        Path("docs/cases")
                        / case.material_code
                        / "golden_route.json"
                    ),
                )
            )
        else:
            workflow = asyncio.run(run_drawing(case, settings))
            evaluation = None
    except (DatabaseError, OSError, RuntimeError, ValueError) as error:
        _abort(f"Drawing route failed: {error}")

    payload = {
        "workflow": workflow.model_dump(mode="json"),
        "evaluation": (
            evaluation.model_dump(mode="json")
            if evaluation is not None
            else None
        ),
    }
    if output_format == "json":
        _emit_json(payload)
    elif output_format == "table":
        console.print(
            f"Run [bold]{workflow.run_id}[/bold] "
            f"status={workflow.route.status.upper()} "
            f"elapsed={workflow.elapsed_seconds:.2f}s "
            f"inference={workflow.inference_seconds:.2f}s"
        )
        console.print(
            "Readers: "
            f"dispatched={workflow.dispatched_flows} "
            f"skipped={workflow.skipped_flows}"
        )
        operations = Table(title="Process route")
        for heading in ("Seq", "State", "Flow", "Process", "Content"):
            operations.add_column(heading)
        for operation in workflow.route.operations:
            state_style = (
                "green" if operation.execution_state == "ready" else "yellow"
            )
            operations.add_row(
                str(operation.sequence or "-"),
                f"[{state_style}]{operation.execution_state.upper()}[/{state_style}]",
                operation.flow_id,
                operation.process,
                operation.content,
            )
        console.print(operations)
        if workflow.route.issues:
            issues = Table(title="Issues")
            for heading in ("Kind", "Code", "Message"):
                issues.add_column(heading)
            for issue in workflow.route.issues:
                issues.add_row(issue.kind.upper(), issue.code, issue.message)
            console.print(issues)
        if evaluation is not None:
            console.print(
                f"Golden evaluation: {evaluation.status.upper()} "
                f"expected_candidates={[item.expected_processes for item in evaluation.route_candidates]} "
                f"predicted={evaluation.predicted_processes}"
            )
    else:
        _abort("--format must be table or json", code=2)

    if require_complete and (
        workflow.route.status != "complete"
        or (evaluation is not None and evaluation.status != "pass")
    ):
        raise typer.Exit(code=3)


@tree_app.command("import")
def tree_import(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    tree_key: Annotated[str, typer.Option("--key", help="Stable decision-tree key.")],
    version: Annotated[int, typer.Option("--version", min=1)],
    name: Annotated[
        str,
        typer.Option("--name", help="Human-readable tree name."),
    ] = "基础工艺决策树",
    description: Annotated[
        str | None,
        typer.Option("--description"),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="table or json"),
    ] = "table",
) -> None:
    try:
        with connect() as connection:
            with connection.transaction():
                summary = import_decision_tree(
                    connection,
                    source,
                    tree_key=tree_key,
                    name=name,
                    version=version,
                    description=description,
                )
    except (DatabaseError, OSError, ValueError) as error:
        _abort(f"Decision-tree import failed: {error}")

    payload = {
        "tree_key": summary.tree_key,
        "version": summary.version,
        "version_id": summary.version_id,
        "source_sha256": summary.source_sha256,
        "existing": summary.existing,
        "source_rows": summary.source_row_count,
        "nodes": summary.node_count,
        "branches": summary.branch_count,
        "rules": summary.rule_count,
    }
    if output_format == "json":
        _emit_json(payload)
        return
    if output_format != "table":
        _abort("--format must be table or json", code=2)

    table = Table(title="Decision-tree import")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in payload.items():
        table.add_row(key, str(value))
    console.print(table)


@tree_app.command("list")
def tree_list(
    output_format: Annotated[str, typer.Option("--format")] = "table",
) -> None:
    try:
        with connect() as connection:
            rows = list_tree_versions(connection)
    except DatabaseError as error:
        _abort(f"Could not list decision trees: {error}")

    if output_format == "json":
        _emit_json(rows)
        return
    if output_format != "table":
        _abort("--format must be table or json", code=2)

    table = Table(title="Decision trees")
    for heading in ("Key", "Version", "Status", "Nodes", "Branches", "Rules", "Source"):
        table.add_column(heading)
    for row in rows:
        table.add_row(
            row["tree_key"],
            str(row["version"]),
            row["status"],
            str(row["node_count"]),
            str(row["branch_count"]),
            str(row["executable_rule_count"]),
            row["source_path"],
        )
    console.print(table)


@tree_app.command("show")
def tree_show(
    tree_key: Annotated[str, typer.Argument()],
    version: Annotated[int, typer.Option("--version", min=1)],
    output_format: Annotated[str, typer.Option("--format")] = "table",
) -> None:
    try:
        with connect() as connection:
            details = tree_details(connection, tree_key, version)
    except (DatabaseError, LookupError) as error:
        _abort(str(error))

    if output_format == "json":
        _emit_json(details)
        return
    if output_format != "table":
        _abort("--format must be table or json", code=2)

    console.print(
        f"[bold]{tree_key}[/bold] version={version} status={details['status']} "
        f"source={details['source_path']}"
    )
    node_table = Table(title="Nodes")
    for heading in ("Node", "Title", "Kind", "State", "Predecessor", "Rows"):
        node_table.add_column(heading)
    for node in details["nodes"]:
        node_table.add_row(
            node["node_key"],
            node["title"],
            node["node_kind"],
            node["maintenance_status"],
            node["source_predecessor_ref"] or "-",
            f"{node['source_row_start']}-{node['source_row_end']}",
        )
    console.print(node_table)

    branch_table = Table(title="Branches")
    for heading in ("Node", "Branch", "Thought", "State", "Confidence", "Rules"):
        branch_table.add_column(heading)
    for branch in details["branches"]:
        branch_table.add_row(
            branch["node_key"],
            branch["branch_key"],
            branch["title"] or "-",
            branch["maintenance_status"],
            branch["confidence_mode"],
            str(branch["rule_count"]),
        )
    console.print(branch_table)


@tree_app.command("validate")
def tree_validate(
    tree_key: Annotated[str, typer.Argument()],
    version: Annotated[int, typer.Option("--version", min=1)],
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Return exit code 3 when any issue exists."),
    ] = False,
    output_format: Annotated[str, typer.Option("--format")] = "table",
) -> None:
    try:
        with connect() as connection:
            report = validate_tree(connection, tree_key, version)
    except (DatabaseError, LookupError) as error:
        _abort(str(error))

    payload = {
        "tree_key": report.tree_key,
        "version": report.version,
        "counts": report.counts,
        "error_count": report.error_count,
        "candidate_count": report.candidate_count,
        "issues": [
            {
                "kind": issue.kind,
                "code": issue.code,
                "location": issue.location,
                "message": issue.message,
                "details": issue.details,
            }
            for issue in report.issues
        ],
    }
    if output_format == "json":
        _emit_json(payload)
    elif output_format == "table":
        console.print(
            f"{tree_key} version={version} rows={report.counts['source_rows']} "
            f"nodes={report.counts['nodes']} branches={report.counts['branches']} "
            f"rules={report.counts['rules']}"
        )
        table = Table(title="Validation issues")
        for heading in ("Kind", "Code", "Location", "Message"):
            table.add_column(heading)
        for issue in report.issues:
            style = "red" if issue.kind == "ERROR" else "yellow"
            table.add_row(
                f"[{style}]{issue.kind}[/{style}]",
                issue.code,
                issue.location,
                issue.message,
            )
        console.print(table)
    else:
        _abort("--format must be table or json", code=2)

    if strict and report.issues:
        raise typer.Exit(code=3)


@tree_app.command("evaluate")
def tree_evaluate(
    tree_key: Annotated[str, typer.Argument()],
    facts_path: Annotated[
        Path,
        typer.Option("--facts", exists=True, dir_okay=False, readable=True),
    ],
    version: Annotated[int, typer.Option("--version", min=1)],
    output_format: Annotated[str, typer.Option("--format")] = "table",
) -> None:
    try:
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
        with connect() as connection:
            rows = evaluate_tree(connection, tree_key, version, facts)
    except (DatabaseError, LookupError, OSError, ValueError, json.JSONDecodeError) as error:
        _abort(f"Decision-tree evaluation failed: {error}")

    if output_format == "json":
        _emit_json(rows)
        return
    if output_format != "table":
        _abort("--format must be table or json", code=2)

    table = Table(title="Decision-tree evaluation")
    for heading in ("Status", "Node", "Branch", "Rule", "Outcome", "Reason"):
        table.add_column(heading)
    for row in rows:
        status = row["result_status"].upper()
        style = {"RESOLVED": "green", "CANDIDATE": "yellow", "ERROR": "red"}.get(
            status,
            "white",
        )
        table.add_row(
            f"[{style}]{status}[/{style}]",
            row["node_key"],
            row["branch_key"],
            row["rule_key"],
            f"{row['outcome_type']}:{row['outcome_key']}",
            row["reason"],
        )
    console.print(table)


@tree_app.command("activate")
def tree_activate(
    tree_key: Annotated[str, typer.Argument()],
    version: Annotated[int, typer.Option("--version", min=1)],
    allow_incomplete: Annotated[
        bool,
        typer.Option("--allow-incomplete", help="Explicitly activate a draft with errors."),
    ] = False,
) -> None:
    try:
        with connect() as connection:
            with connection.transaction():
                activate_tree(
                    connection,
                    tree_key,
                    version,
                    allow_incomplete=allow_incomplete,
                )
    except (DatabaseError, LookupError, ValueError) as error:
        _abort(f"Activation failed: {error}")
    console.print(f"Activated {tree_key} version {version}.")


if __name__ == "__main__":
    app()
