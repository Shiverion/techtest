"""Role-scoped SQL Server access.

Every request opens its own connection with the login for its role (never shared across roles or
requests), runs inside a transaction that is always rolled back, and is bounded by a query timeout.
Rollback is cleanup, not the read-only guarantee: the logins themselves cannot write.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import pymssql

from .security.roles import Role
from .security.sql_policy import ValidatedQuery
from .settings import Settings

# SQL Server errors caused by a malformed query the model can fix (syntax, unknown column/object,
# GROUP BY mistakes, conversions). Permission errors and timeouts are never "repairable".
REPAIRABLE_ERRORS = frozenset({102, 156, 170, 207, 208, 209, 245, 4104, 4145, 8117, 8118, 8120, 8127, 1013})
PERMISSION_ERRORS = frozenset({229, 230, 262, 297, 300, 916, 1088, 3701, 3704, 4701, 4902, 15151})
TIMEOUT_ERRORS = frozenset({20003, 20047})


class QueryError(Exception):
    def __init__(self, kind: str, number: int | None, safe_message: str):
        super().__init__(safe_message)
        self.kind = kind  # "repairable" | "permission" | "timeout" | "database"
        self.number = number
        self.safe_message = safe_message


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    money_columns: frozenset[str] = frozenset()  # casefolded names of IDR columns

    def is_money(self, column: str) -> bool:
        return column.casefold() in self.money_columns


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def connection(self, role: Role) -> Iterator[pymssql.Connection]:
        user, password = self.settings.db_credentials(role)
        # A serverless database that auto-paused refuses logins for up to a minute while it resumes;
        # with DB_CONNECT_RETRY_S > 0 the first request after idle waits for it instead of failing.
        give_up = time.monotonic() + self.settings.db_connect_retry_s
        while True:
            try:
                conn = pymssql.connect(
                    server=self.settings.db_host,
                    port=self.settings.db_port,
                    user=user,
                    password=password,
                    database=self.settings.db_name,
                    login_timeout=self.settings.db_login_timeout_s,
                    timeout=self.settings.db_query_timeout_s,
                    autocommit=False,
                    appname="southcity-assistant",
                )
                break
            except pymssql.Error:
                if time.monotonic() + 5 > give_up:
                    raise QueryError("database", None, "Database is unavailable.") from None
                time.sleep(5)
        try:
            yield conn
        finally:
            try:
                conn.rollback()
            except Exception:  # a timed-out connection is already dead (driver-level error)
                pass
            try:
                conn.close()
            except Exception:
                pass

    def run(self, role: Role, query: ValidatedQuery, max_rows: int, max_bytes: int) -> QueryResult:
        """Execute a policy-validated query. Only ValidatedQuery is accepted, never raw text."""
        if not isinstance(query, ValidatedQuery):
            raise TypeError("Database.run only accepts a ValidatedQuery")
        rows: list[tuple[Any, ...]] = []
        truncated = False
        size = 0
        with self.connection(role) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(query.sql)
                columns = _column_names(cursor.description or [])
                plain = [i for i, c in enumerate(columns) if c.casefold() in query.plain_columns]
                # Row by row, so the byte budget bounds memory while fetching (a row's width is bounded
                # by the policy: <= 40 columns, string casts <= 400, no MAX types, no REPLACE).
                while (row := cursor.fetchone()) is not None:
                    row = tuple(row)
                    # Outer-join placeholder rows (every plain column NULL) are not data: a vacant unit's
                    # contract section is empty, not "1 contract" made of NULLs.
                    if plain and all(row[i] is None for i in plain):
                        continue
                    if len(rows) == max_rows:
                        truncated = True
                        break
                    # JSON-encoded size, twice (formatted and raw), plus headroom for "Rp " and digit separators.
                    size += 2 * len(json.dumps([str(v) for v in row], ensure_ascii=False).encode("utf-8")) + 8 * len(row)
                    if size > max_bytes:
                        truncated = True
                        break
                    rows.append(row)
            except pymssql.Error as exc:
                raise _classify(exc, query.sql) from None
        return QueryResult(columns=columns, rows=rows, truncated=truncated, money_columns=query.money_columns)

    def lookup(self, role: Role, sql: str, params: tuple[Any, ...] | None) -> list[tuple[Any, ...]]:
        """Run code-authored lookups: values are bound parameters, or (params=None) the SQL is a fragment
        of an already policy-validated query."""
        with self.connection(role) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params)
                return [tuple(r) for r in cursor.fetchall()]
            except pymssql.Error as exc:
                raise _classify(exc, sql) from None


def _column_names(description: list[tuple[Any, ...]]) -> list[str]:
    names: list[str] = []
    for i, col in enumerate(description, start=1):
        name = col[0] or f"col{i}"
        while name in names:
            name = f"{name}_{i}"
        names.append(name)
    return names


def _classify(exc: pymssql.Error, sql: str) -> QueryError:
    number = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if number in PERMISSION_ERRORS:
        return QueryError("permission", number, "The database denied access for this role.")
    if number in TIMEOUT_ERRORS:
        return QueryError("timeout", number, "The query exceeded the time limit.")
    if number in REPAIRABLE_ERRORS:
        return QueryError("repairable", number, _repair_hint(exc, number, sql))
    return QueryError("database", number, "The database could not run the query.")


_GENERIC_HINTS = {
    245: "A value could not be converted to the target data type; check CAST/CONVERT and comparisons.",
    8114: "A value could not be converted to the target data type; check CAST/CONVERT and comparisons.",
}


def _repair_hint(exc: pymssql.Error, number: int | None, sql: str) -> str:
    """Feedback for the repair round. SQL Server's message is used only if every quoted token in it
    already appears in the model's own SQL (column/object names, syntax near X). Messages that carry
    stored data values (e.g. conversion errors quoting a customer name) become a fixed template."""
    raw = exc.args[1] if len(exc.args) > 1 else b""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    text = re.sub(r"\s+", " ", text.split("DB-Lib error message")[0]).strip()[:300]
    quoted = re.findall(r"'([^']*)'", text)
    if number in _GENERIC_HINTS or not text or any(q.casefold() not in sql.casefold() for q in quoted):
        return _GENERIC_HINTS.get(number or 0, f"SQL Server error {number}: the query is invalid.")
    return text
