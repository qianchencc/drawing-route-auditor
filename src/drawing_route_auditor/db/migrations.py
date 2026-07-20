from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import re

from drawing_route_auditor.db.connection import Connection


_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
_LOCK_KEY = "drawing_route_auditor_schema_migrations"


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
                checksum=sha256(sql.encode("utf-8")).hexdigest(),
            )
        )

    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Migration versions must be unique")
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


def migrate(connection: Connection) -> MigrationResult:
    if not connection.autocommit:
        raise ValueError("Migration connection must use autocommit=True")

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
