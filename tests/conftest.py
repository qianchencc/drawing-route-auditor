from collections.abc import Iterator

import pytest

from drawing_route_auditor.db.connection import Connection, connect
from drawing_route_auditor.db.migrations import migrate


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    with connect(autocommit=True) as connection:
        migrate(connection)
    yield


class _RollbackFixtureTransaction(Exception):
    pass


@pytest.fixture
def db_connection() -> Iterator[Connection]:
    with connect() as connection:
        try:
            with connection.transaction():
                yield connection
                raise _RollbackFixtureTransaction
        except _RollbackFixtureTransaction:
            pass
