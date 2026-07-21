from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any

from psycopg2 import OperationalError, connect as postgres_connect
from psycopg2.extensions import (
    TRANSACTION_STATUS_IDLE,
    TRANSACTION_STATUS_INTRANS,
    connection as RawConnection,
)
from psycopg2.extras import RealDictCursor

from drawing_route_auditor.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class QueryResult:
    rows: tuple[dict[str, Any], ...]

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)


class Connection:
    def __init__(self, raw: RawConnection) -> None:
        self._raw = raw
        self._transaction_depth = 0
        self._savepoint_sequence = 0

    @property
    def autocommit(self) -> bool:
        return self._raw.autocommit

    def execute(
        self,
        query: str,
        params: Sequence[Any] | None = None,
    ) -> QueryResult:
        cursor = self._raw.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute(query, params)
            if cursor.description is None:
                return QueryResult(())
            return QueryResult(tuple(dict(row) for row in cursor.fetchall()))
        finally:
            cursor.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        was_autocommit = self._raw.autocommit
        if was_autocommit:
            self._raw.autocommit = False

        status = self._raw.get_transaction_status()
        owns_transaction = self._transaction_depth == 0
        savepoint: str | None = None
        if owns_transaction:
            if status == TRANSACTION_STATUS_IDLE:
                self.execute("BEGIN")
            elif status != TRANSACTION_STATUS_INTRANS:
                if was_autocommit:
                    self._raw.autocommit = True
                raise RuntimeError("数据库连接当前无法开始事务")
        else:
            if status != TRANSACTION_STATUS_INTRANS:
                raise RuntimeError("数据库连接当前无法创建事务保存点")
            self._savepoint_sequence += 1
            savepoint = f"drawing_route_auditor_sp_{self._savepoint_sequence}"
            self.execute(f"SAVEPOINT {savepoint}")

        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            if owns_transaction:
                self._raw.rollback()
            else:
                self.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            if owns_transaction:
                self._raw.commit()
            else:
                self.execute(f"RELEASE SAVEPOINT {savepoint}")
        finally:
            self._transaction_depth -= 1
            if was_autocommit:
                self._raw.autocommit = True


    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


@contextmanager
def connect(
    settings: Settings | None = None,
    *,
    autocommit: bool = False,
) -> Iterator[Connection]:
    active_settings = settings or get_settings()
    raw_connection = postgres_connect(
        active_settings.database_url,
        connect_timeout=active_settings.database_connect_timeout_seconds,
    )
    raw_connection.autocommit = autocommit
    connection = Connection(raw_connection)
    try:
        yield connection
    finally:
        connection.close()


def wait_for_database(
    settings: Settings | None = None,
    *,
    timeout_seconds: float = 30,
    interval_seconds: float = 0.5,
) -> float:
    active_settings = settings or get_settings()
    started_at = monotonic()
    last_error: OperationalError | None = None

    while monotonic() - started_at < timeout_seconds:
        try:
            with connect(active_settings, autocommit=True) as connection:
                connection.execute("SELECT 1")
            return monotonic() - started_at
        except OperationalError as error:
            last_error = error
            sleep(interval_seconds)

    message = f"PostgreSQL was not ready after {timeout_seconds:.1f}s"
    if last_error is not None:
        message = f"{message}: {last_error}"
    raise TimeoutError(message)
