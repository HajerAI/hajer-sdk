"""A replay never reaches a database: importing a driver is refused, and the attempt is DB_REQUIRED.

Refusing the import (rather than the connection) is deliberate: a workflow whose code path loads a driver
depends on state the replay does not have, and a verdict computed without that state would be a guess.
`sqlite3` is not on the list — it is a file in the sandbox's own directory, not shared state.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from collections.abc import Sequence
from types import ModuleType

from hajer.replay._guard import Sandbox

DATABASE_DRIVERS = frozenset(
    {
        "psycopg",
        "psycopg2",
        "psycopg_pool",
        "asyncpg",
        "pymysql",
        "MySQLdb",
        "aiomysql",
        "mysql",
        "pymongo",
        "motor",
        "redis",
        "aioredis",
        "cx_Oracle",
        "oracledb",
        "pyodbc",
    }
)


class DatabaseImportRefusedError(ImportError):
    """A database driver was imported inside a replay."""


class _DriverFinder(importlib.abc.MetaPathFinder):
    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        _ = (path, target)  # the finder protocol's signature; only the name decides
        root = fullname.partition(".")[0]
        if root in DATABASE_DRIVERS:
            self._sandbox.refuse("DB_REQUIRED", f"import:{root}", 0)
            raise DatabaseImportRefusedError(f"HAJER_REPLAY: database driver {root!r} is refused in the sandbox")
        return None


def install_database_guard(sandbox: Sandbox) -> None:
    sys.meta_path.insert(0, _DriverFinder(sandbox))
