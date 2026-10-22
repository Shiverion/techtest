"""The database itself enforces RBAC and read-only access, independent of every application check.

These tests connect with the runtime logins directly, bypassing the guard, the model and the SQL policy.
"""

import pymssql
import pytest

from southcity_assistant.db import QueryError
from southcity_assistant.security.roles import Role
from southcity_assistant.security.sql_policy import ValidatedQuery

from .conftest import GOLDEN, table_checksums

pytestmark = pytest.mark.integration

WRITES = [
    "DELETE FROM dbo.GL_Journals WHERE JournalID = 901",
    "UPDATE dbo.Units SET Status = 'Sold' WHERE UnitID = 4",
    "INSERT INTO dbo.Units VALUES (99, 'X 9999', 'X', 'Vacant')",
    "DROP TABLE dbo.Invoices_Billing",
    "ALTER TABLE dbo.Units ADD Hacked INT",
    "TRUNCATE TABLE dbo.GL_Journals",
    "CREATE TABLE dbo.Hacked (a INT)",
    "CREATE VIEW dbo.v AS SELECT 1 AS a",
    "GRANT SELECT ON dbo.GL_Journals TO public",
]


def error_number(conn, sql) -> int:
    cur = conn.cursor()
    with pytest.raises(pymssql.Error) as info:
        cur.execute(sql)
        cur.fetchall()
    conn.rollback()
    return info.value.args[0]


def test_sales_login_is_denied_gl_journals(sales_conn):
    assert error_number(sales_conn, "SELECT * FROM dbo.GL_Journals") == 229
    assert error_number(sales_conn, "SELECT u.UnitID FROM dbo.Units u WHERE EXISTS "
                                    "(SELECT 1 FROM dbo.GL_Journals)") == 229


def test_sales_login_reads_its_tables(sales_conn):
    cur = sales_conn.cursor()
    for table in ("Units", "Sales_Contracts", "Invoices_Billing"):
        cur.execute(f"SELECT COUNT(*) FROM dbo.{table}")
        assert cur.fetchone()[0] == GOLDEN["row_counts"][table]


def test_management_login_reads_gl_journals(mgmt_conn):
    cur = mgmt_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM dbo.GL_Journals")
    assert cur.fetchone()[0] == 10


@pytest.mark.parametrize("sql", WRITES)
def test_no_runtime_login_can_write(sql, sales_conn, mgmt_conn, baseline_checksums):
    for conn in (sales_conn, mgmt_conn):
        assert error_number(conn, sql) in {229, 230, 262, 297, 300, 1088, 3701, 3704, 4701, 15151, 4902}
    assert table_checksums(mgmt_conn) == baseline_checksums


@pytest.mark.parametrize("conn_name", ["sales_conn", "mgmt_conn"])
def test_effective_permissions_are_minimal(conn_name, request):
    conn = request.getfixturevalue(conn_name)
    cur = conn.cursor()
    cur.execute("SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')")
    db_perms = {row[0] for row in cur.fetchall()}
    # public's default metadata permissions (VIEW ANY COLUMN ... KEY DEFINITION) are harmless;
    # nothing that writes, executes, creates, alters, controls or impersonates may appear.
    risky = {p for p in db_perms
             if any(w in p for w in ("INSERT", "UPDATE", "DELETE", "ALTER", "EXECUTE", "CREATE",
                                     "CONTROL", "OWNERSHIP", "IMPERSONATE", "BACKUP", "REFERENCES"))}
    assert "CONNECT" in db_perms and not risky, risky
    cur.execute(
        "SELECT IS_ROLEMEMBER('db_owner'), IS_ROLEMEMBER('db_datawriter'), IS_ROLEMEMBER('db_ddladmin'), "
        "IS_ROLEMEMBER('db_datareader'), IS_SRVROLEMEMBER('sysadmin')"
    )
    assert tuple(cur.fetchone()) == (0, 0, 0, 0, 0)
    cur.execute("SELECT DISTINCT permission_name FROM fn_my_permissions('dbo.Units', 'OBJECT')")
    assert {row[0] for row in cur.fetchall()} == {"SELECT"}
    conn.rollback()


def test_executor_denies_even_if_policy_were_bypassed(db, baseline_checksums):
    """A hand-built ValidatedQuery skips the policy entirely; SQL Server still refuses."""
    forged_read = ValidatedQuery(sql="SELECT * FROM dbo.GL_Journals", tables=frozenset({"Units"}), row_limit=10)
    with pytest.raises(QueryError) as info:
        db.run(Role.SALES_STAFF, forged_read, 10, 10_000)
    assert info.value.kind == "permission"

    forged_delete = ValidatedQuery(sql="DELETE FROM dbo.GL_Journals WHERE JournalID = 901",
                                   tables=frozenset({"GL_Journals"}), row_limit=10)
    for role in Role:
        with pytest.raises(QueryError) as info:
            db.run(role, forged_delete, 10, 10_000)
        assert info.value.kind == "permission"


def test_database_errors_are_sanitized(db):
    bad = ValidatedQuery(sql="SELECT NoSuchColumn FROM dbo.Units", tables=frozenset({"Units"}), row_limit=10)
    with pytest.raises(QueryError) as info:
        db.run(Role.MANAGEMENT, bad, 10, 10_000)
    assert info.value.kind == "repairable" and info.value.number == 207
    assert "DB-Lib" not in info.value.safe_message and "NoSuchColumn" in info.value.safe_message


def test_query_timeout_is_enforced_and_connection_discarded(settings):
    from southcity_assistant.db import Database

    fast = Database(settings.model_copy(update={"db_query_timeout_s": 1}))
    slow = ValidatedQuery(sql="WAITFOR DELAY '00:00:05'", tables=frozenset({"Units"}), row_limit=1)
    with pytest.raises(QueryError) as info:
        fast.run(Role.MANAGEMENT, slow, 10, 10_000)
    assert info.value.kind == "timeout"
    ok = ValidatedQuery(sql="SELECT TOP 1 UnitID FROM dbo.Units", tables=frozenset({"Units"}), row_limit=1)
    assert fast.run(Role.MANAGEMENT, ok, 10, 10_000).rows  # next request gets a fresh connection
