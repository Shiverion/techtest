"""Integration fixtures: a real SQL Server provisioned by `docker compose run --rm db-init`."""

from __future__ import annotations

from decimal import Decimal

import pymssql
import pytest

from southcity_assistant.db import Database
from southcity_assistant.security.roles import Role
from southcity_assistant.settings import Settings

# Golden fixture facts, computed by hand from schema.sql. Tests assert these AND independent
# reference SQL, so a broken seed cannot make the app and the reference agree on a wrong answer.
GOLDEN = {
    "q1_siti_arrears_2026_10": Decimal("10500000.00"),
    "q2_budi_total_sales": Decimal("2700000000.00"),
    "q2_budi_contracts": {102, 107},
    "q3_unit": ("SM 1201", "Vacant"),
    "q4_journal_ids": [901],
    "row_counts": {"Units": 10, "Sales_Contracts": 8, "Invoices_Billing": 9, "GL_Journals": 10},
}


@pytest.fixture(scope="session")
def settings() -> Settings:
    try:
        s = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # missing .env
        pytest.skip(f"integration settings unavailable: {exc}")
    try:
        user, password = s.db_credentials(Role.MANAGEMENT)
        pymssql.connect(server=s.db_host, port=s.db_port, user=user, password=password,
                        database=s.db_name, login_timeout=3).close()
    except pymssql.Error:
        pytest.skip("SQL Server not reachable: run `docker compose up -d db` and `docker compose run --rm db-init`")
    return s


@pytest.fixture(scope="session")
def db(settings) -> Database:
    return Database(settings)


def connect(settings: Settings, user: str, password: str) -> pymssql.Connection:
    return pymssql.connect(server=settings.db_host, port=settings.db_port, user=user, password=password,
                           database=settings.db_name, login_timeout=5, timeout=10, autocommit=False)


@pytest.fixture
def sales_conn(settings):
    conn = connect(settings, settings.sales_db_user, settings.sales_db_password.get_secret_value())
    yield conn
    conn.close()


@pytest.fixture
def mgmt_conn(settings):
    conn = connect(settings, settings.mgmt_db_user, settings.mgmt_db_password.get_secret_value())
    yield conn
    conn.close()


def table_checksums(conn: pymssql.Connection) -> dict[str, tuple[int, int]]:
    """Row count + CHECKSUM_AGG over every column, per table, to prove nothing changed."""
    cur = conn.cursor()
    out = {}
    for table in GOLDEN["row_counts"]:
        cur.execute(f"SELECT COUNT(*), CHECKSUM_AGG(CHECKSUM(*)) FROM dbo.{table}")
        out[table] = tuple(cur.fetchone())
    conn.rollback()
    return out


@pytest.fixture(scope="session")
def baseline_checksums(settings):
    conn = connect(settings, settings.mgmt_db_user, settings.mgmt_db_password.get_secret_value())
    try:
        sums = table_checksums(conn)
    finally:
        conn.close()
    assert {t: c for t, (c, _) in sums.items()} == GOLDEN["row_counts"], "fixture not freshly seeded"
    return sums
