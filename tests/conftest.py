from collections.abc import Iterator

import pytest
from drawing_route_auditor.db.connection import Connection

from drawing_route_auditor.db.connection import connect
from drawing_route_auditor.db.migrations import migrate


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    with connect(autocommit=True) as connection:
        migrate(connection)
    yield


@pytest.fixture
def db_connection() -> Iterator[Connection]:
    with connect() as connection:
        try:
            yield connection
        finally:
            connection.rollback()
