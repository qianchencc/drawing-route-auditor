from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import re

from drawing_route_auditor.db.connection import Connection


_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
_LOCK_KEY = "drawing_route_auditor_schema_migrations"
_KNOWLEDGE_DATA = {
    "0008": "0008_current_tree_upgrade.json",
    "0009": "0009_cover_shell_route.json",
    "0010": "0010_five_sample_route_modules.json",
    "0011": "0011_pdf_only_inference.json",
    "0012": "0012_oriented_drawing_facts.json",
    "0013": "0013_robust_pdf_family_rules.json",
    "0014": "0014_strict_pdf_geometry.json",
    "0015": "0015_pdf_only_tree_metadata.json",
    "0016": "0016_cover_shell_pdf_family.json",
    "0017": "0017_local_flange_bend.json",
    "0018": "0018_hopper_geometry_precedence.json",
    "0019": "0019_remove_redundant_geometry_guard.json",
    "0020": "0020_feature_derived_routes.json",
    "0021": "0021_feature_metadata_cleanup.json",
    "0022": "0022_rolled_feature_completeness.json",
    "0023": "0023_derived_prerequisite_closure.json",
    "0024": "0024_reader_feature_guards.json",
    "0025": "0025_small_hole_drilling.json",
    "0026": "0026_large_precision_boring.json",
    "0027": "0027_directional_surface_finish.json",
    "0028": "0028_large_bore_reader_guard.json",
    "0029": "0029_multi_joint_access_guard.json",
    "0030": "0030_compact_geometry_features.json",
    "0031": "0031_welded_multi_joint_guard.json",
    "0032": "0032_precision_weldment_access.json",
    "0033": "0033_large_bore_reader_ownership.json",
    "0034": "0034_remove_unvalidated_weld_stages.json",
    "0035": "0035_external_mechanical_finish.json",
    "0036": "0036_weld_presence_routes.json",
    "0037": "0037_external_finish_reader_guard.json",
    "0038": "0038_large_bore_judgement_guard.json",
    "0039": "0039_surface_stage_ownership_guard.json",
    "0040": "0040_shaft_local_hole_geometry_guard.json",
    "0041": "0041_surface_branch_metadata.json",
    "0042": "0042_tube_stock_cut_route.json",
    "0043": "0043_compact_axisymmetric_turning.json",
    "0044": "0044_cylindrical_projection_guard.json",
    "0045": "0045_stepped_bar_evidence_guard.json",
    "0046": "0046_preserve_tube_stock_guard.json",
    "0047": "0047_surface_protection_and_order_guards.json",
    "0048": "0048_split_geometry_readers.json",
    "0049": "0049_precision_tolerance_judgement_guard.json",
    "0050": "0050_weld_local_surface_scope_guard.json",
    "0051": "0051_axis_stock_and_internal_surface_consistency.json",
    "0052": "0052_general_axis_stock_guard.json",
    "0053": "0053_external_cylindrical_precision_route.json",
    "0054": "0054_continuous_rolled_shell_route.json",
    "0055": "0055_exclude_rolled_shell_from_flat_bent.json",
    "0056": "0056_scope_rolled_shell_completion.json",
}


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[str, ...]
    current: tuple[str, ...]


def _migration_checksum(version: str, sql: str) -> str:
    payload = sql
    data_name = _KNOWLEDGE_DATA.get(version)
    if data_name is not None:
        resource = files("drawing_route_auditor.db.data").joinpath(data_name)
        payload = f"{payload}\n{resource.read_text(encoding='utf-8')}"
    return sha256(payload.encode("utf-8")).hexdigest()


def load_migrations() -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    migration_root = files("drawing_route_auditor.db.sql")

    for resource in migration_root.iterdir():
        match = _MIGRATION_NAME.match(resource.name)
        if match is None:
            continue
        sql = resource.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=match.group("version"),
                name=match.group("name"),
                sql=sql,
                checksum=_migration_checksum(match.group("version"), sql),
            )
        )

    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError("迁移版本号必须唯一")
    return tuple(migrations)


def _ensure_migration_table(connection: Connection) -> None:
    with connection.transaction():
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                name text NOT NULL,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )


def _apply_python_migration(connection: Connection, version: str) -> None:
    if version == "0008":
        from drawing_route_auditor.db.knowledge_migrations import apply_current_tree_upgrade

        apply_current_tree_upgrade(connection)
    elif version == "0009":
        from drawing_route_auditor.db.knowledge_migrations import apply_cover_shell_upgrade

        apply_cover_shell_upgrade(connection)
    elif version == "0010":
        from drawing_route_auditor.db.knowledge_migrations import apply_five_sample_upgrade

        apply_five_sample_upgrade(connection)
    elif version == "0011":
        from drawing_route_auditor.db.knowledge_migrations import apply_pdf_only_upgrade

        apply_pdf_only_upgrade(connection)
    elif version == "0012":
        from drawing_route_auditor.db.knowledge_migrations import apply_oriented_facts_upgrade

        apply_oriented_facts_upgrade(connection)
    elif version == "0013":
        from drawing_route_auditor.db.knowledge_migrations import apply_robust_family_upgrade

        apply_robust_family_upgrade(connection)
    elif version == "0014":
        from drawing_route_auditor.db.knowledge_migrations import apply_strict_geometry_upgrade

        apply_strict_geometry_upgrade(connection)
    elif version == "0015":
        from drawing_route_auditor.db.knowledge_migrations import apply_pdf_only_metadata_upgrade

        apply_pdf_only_metadata_upgrade(connection)
    elif version == "0016":
        from drawing_route_auditor.db.knowledge_migrations import apply_cover_pdf_family_upgrade

        apply_cover_pdf_family_upgrade(connection)
    elif version == "0017":
        from drawing_route_auditor.db.knowledge_migrations import apply_local_flange_upgrade

        apply_local_flange_upgrade(connection)
    elif version == "0018":
        from drawing_route_auditor.db.knowledge_migrations import apply_hopper_geometry_upgrade

        apply_hopper_geometry_upgrade(connection)
    elif version == "0019":
        from drawing_route_auditor.db.knowledge_migrations import apply_geometry_guard_cleanup

        apply_geometry_guard_cleanup(connection)
    elif version == "0020":
        from drawing_route_auditor.db.knowledge_migrations import apply_feature_derived_routes_upgrade

        apply_feature_derived_routes_upgrade(connection)
    elif version == "0021":
        from drawing_route_auditor.db.knowledge_migrations import apply_feature_metadata_cleanup

        apply_feature_metadata_cleanup(connection)
    elif version == "0022":
        from drawing_route_auditor.db.knowledge_migrations import apply_rolled_feature_completeness

        apply_rolled_feature_completeness(connection)
    elif version == "0023":
        from drawing_route_auditor.db.knowledge_migrations import apply_derived_prerequisite_closure

        apply_derived_prerequisite_closure(connection)
    elif version == "0024":
        from drawing_route_auditor.db.knowledge_migrations import apply_reader_feature_guards

        apply_reader_feature_guards(connection)
    elif version == "0025":
        from drawing_route_auditor.db.knowledge_migrations import apply_small_hole_drilling

        apply_small_hole_drilling(connection)
    elif version == "0026":
        from drawing_route_auditor.db.knowledge_migrations import apply_large_precision_boring

        apply_large_precision_boring(connection)
    elif version == "0027":
        from drawing_route_auditor.db.knowledge_migrations import apply_directional_surface_finish

        apply_directional_surface_finish(connection)
    elif version == "0028":
        from drawing_route_auditor.db.knowledge_migrations import apply_large_bore_reader_guard

        apply_large_bore_reader_guard(connection)
    elif version == "0029":
        from drawing_route_auditor.db.knowledge_migrations import apply_multi_joint_access_guard

        apply_multi_joint_access_guard(connection)
    elif version == "0030":
        from drawing_route_auditor.db.knowledge_migrations import apply_compact_geometry_features

        apply_compact_geometry_features(connection)
    elif version == "0031":
        from drawing_route_auditor.db.knowledge_migrations import apply_welded_multi_joint_guard

        apply_welded_multi_joint_guard(connection)
    elif version == "0032":
        from drawing_route_auditor.db.knowledge_migrations import apply_precision_weldment_access

        apply_precision_weldment_access(connection)
    elif version == "0033":
        from drawing_route_auditor.db.knowledge_migrations import apply_large_bore_reader_ownership

        apply_large_bore_reader_ownership(connection)
    elif version == "0034":
        from drawing_route_auditor.db.knowledge_migrations import apply_remove_unvalidated_weld_stages

        apply_remove_unvalidated_weld_stages(connection)
    elif version == "0035":
        from drawing_route_auditor.db.knowledge_migrations import apply_external_mechanical_finish

        apply_external_mechanical_finish(connection)
    elif version == "0036":
        from drawing_route_auditor.db.knowledge_migrations import apply_weld_presence_routes

        apply_weld_presence_routes(connection)
    elif version == "0037":
        from drawing_route_auditor.db.knowledge_migrations import apply_external_finish_reader_guard

        apply_external_finish_reader_guard(connection)
    elif version == "0038":
        from drawing_route_auditor.db.knowledge_migrations import apply_large_bore_judgement_guard

        apply_large_bore_judgement_guard(connection)
    elif version == "0039":
        from drawing_route_auditor.db.knowledge_migrations import apply_surface_stage_ownership_guard

        apply_surface_stage_ownership_guard(connection)
    elif version == "0040":
        from drawing_route_auditor.db.knowledge_migrations import (
            apply_shaft_local_hole_geometry_guard,
        )

        apply_shaft_local_hole_geometry_guard(connection)
    elif version == "0041":
        from drawing_route_auditor.db.knowledge_migrations import apply_surface_branch_metadata

        apply_surface_branch_metadata(connection)
    elif version == "0042":
        from drawing_route_auditor.db.knowledge_migrations import apply_tube_stock_cut_route

        apply_tube_stock_cut_route(connection)
    elif version == "0043":
        from drawing_route_auditor.db.knowledge_migrations import apply_compact_axisymmetric_turning

        apply_compact_axisymmetric_turning(connection)
    elif version == "0044":
        from drawing_route_auditor.db.knowledge_migrations import apply_cylindrical_projection_guard

        apply_cylindrical_projection_guard(connection)
    elif version == "0045":
        from drawing_route_auditor.db.knowledge_migrations import apply_stepped_bar_evidence_guard

        apply_stepped_bar_evidence_guard(connection)
    elif version == "0046":
        from drawing_route_auditor.db.knowledge_migrations import apply_preserve_tube_stock_guard

        apply_preserve_tube_stock_guard(connection)
    elif version == "0047":
        from drawing_route_auditor.db.knowledge_migrations import (
            apply_surface_protection_and_order_guards,
        )

        apply_surface_protection_and_order_guards(connection)
    elif version == "0048":
        from drawing_route_auditor.db.knowledge_migrations import apply_split_geometry_readers

        apply_split_geometry_readers(connection)
    elif version == "0049":
        from drawing_route_auditor.db.knowledge_migrations import (
            apply_precision_tolerance_judgement_guard,
        )

        apply_precision_tolerance_judgement_guard(connection)
    elif version == "0050":
        from drawing_route_auditor.db.knowledge_migrations import (
            apply_weld_local_surface_scope_guard,
        )

        apply_weld_local_surface_scope_guard(connection)
    elif version == "0051":
        from drawing_route_auditor.db.knowledge_migrations import (
            apply_axis_stock_and_internal_surface_consistency,
        )

        apply_axis_stock_and_internal_surface_consistency(connection)
    elif version == "0052":
        from drawing_route_auditor.db.knowledge_migrations import apply_general_axis_stock_guard

        apply_general_axis_stock_guard(connection)
    elif version == "0053":
        from drawing_route_auditor.db.knowledge_migrations import (
            apply_external_cylindrical_precision_route,
        )

        apply_external_cylindrical_precision_route(connection)
    elif version == "0054":
        from drawing_route_auditor.db.knowledge_migrations import apply_continuous_rolled_shell_route

        apply_continuous_rolled_shell_route(connection)
    elif version == "0055":
        from drawing_route_auditor.db.knowledge_migrations import (
            apply_exclude_rolled_shell_from_flat_bent,
        )

        apply_exclude_rolled_shell_from_flat_bent(connection)
    elif version == "0056":
        from drawing_route_auditor.db.knowledge_migrations import (
            apply_scope_rolled_shell_completion,
        )

        apply_scope_rolled_shell_completion(connection)


def migrate(connection: Connection) -> MigrationResult:
    if not connection.autocommit:
        raise ValueError("执行迁移的连接必须设置 autocommit=True")

    migrations = load_migrations()
    connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (_LOCK_KEY,))
    applied_now: list[str] = []

    try:
        _ensure_migration_table(connection)
        existing_rows = connection.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        existing = {row["version"]: row["checksum"] for row in existing_rows}

        for migration in migrations:
            recorded_checksum = existing.get(migration.version)
            if recorded_checksum is not None:
                if recorded_checksum != migration.checksum:
                    raise RuntimeError(
                        f"Migration {migration.version} checksum changed after application"
                    )
                continue

            with connection.transaction():
                connection.execute(migration.sql)
                _apply_python_migration(connection, migration.version)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.name, migration.checksum),
                )
            applied_now.append(migration.version)
    finally:
        connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_KEY,))

    current_rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return MigrationResult(
        applied=tuple(applied_now),
        current=tuple(row["version"] for row in current_rows),
    )


def current_versions(connection: Connection) -> tuple[str, ...]:
    row = connection.execute(
        "SELECT to_regclass('public.schema_migrations') AS table_name"
    ).fetchone()
    if row is None or row["table_name"] is None:
        return ()
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return tuple(item["version"] for item in rows)
