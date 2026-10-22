import pytest

from southcity_assistant.security.roles import ROLE_TABLES, Role
from southcity_assistant.security.sql_policy import PolicyViolation, validate

SALES = ROLE_TABLES[Role.SALES_STAFF]
MGMT = ROLE_TABLES[Role.MANAGEMENT]


def ok(sql: str, tables=MGMT, max_rows: int = 200):
    return validate(sql, tables, max_rows)


def rejected(sql: str, tables=MGMT) -> str:
    with pytest.raises(PolicyViolation) as info:
        validate(sql, tables, 200)
    return info.value.code


# --- queries the assistant must be able to run -------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COALESCE(SUM(i.AmountBilled - i.AmountPaid), 0) AS TotalTunggakan "
        "FROM dbo.Invoices_Billing i JOIN dbo.Sales_Contracts c ON c.ContractID = i.ContractID "
        "WHERE c.CustomerName = N'Siti Rahmawati' AND i.PeriodMonth = 10 AND i.PeriodYear = 2026",
        "SELECT COUNT(*) AS Jumlah, SUM(SalesAmount) AS Total FROM Sales_Contracts WHERE CustomerName = 'Budi Santoso'",
        "SELECT u.UnitNumber, u.Status, c.ContractID FROM Units u LEFT JOIN Sales_Contracts c ON c.UnitID = u.UnitID "
        "WHERE u.UnitNumber = 'SM 1201'",
        "SELECT JournalID, Debit FROM GL_Journals WHERE AccountName = 'Kas & Bank' "
        "AND TransactionDate >= '2026-01-01' AND TransactionDate < '2026-02-01' ORDER BY TransactionDate",
        "WITH t AS (SELECT ContractID, SUM(AmountBilled) AS b FROM Invoices_Billing GROUP BY ContractID) "
        "SELECT c.CustomerName, t.b FROM t JOIN Sales_Contracts c ON c.ContractID = t.ContractID",
        "SELECT CustomerName FROM Sales_Contracts WHERE UnitID IN (SELECT UnitID FROM Units WHERE Status = 'Sold')",
        "SELECT [UnitNumber] FROM [dbo].[Units]",
        "SELECT YEAR(ContractDate) AS Tahun, MONTH(ContractDate) AS Bulan, COUNT(*) AS n FROM Sales_Contracts "
        "GROUP BY YEAR(ContractDate), MONTH(ContractDate) HAVING COUNT(*) > 0",
        "SELECT CASE WHEN AmountPaid >= AmountBilled THEN 'Lunas' ELSE 'Tunggak' END AS s FROM Invoices_Billing",
        "SELECT DISTINCT Tower FROM Units WHERE UnitNumber LIKE 'SM%'",
    ],
)
def test_allows_reporting_queries(sql):
    result = ok(sql)
    assert result.sql.upper().startswith(("SELECT", "WITH"))


def test_tables_reported_through_ctes_and_subqueries():
    vq = ok("WITH x AS (SELECT UnitID FROM Units) SELECT * FROM x WHERE x.UnitID IN "
            "(SELECT UnitID FROM Sales_Contracts)")
    assert vq.tables == {"Units", "Sales_Contracts"}


# --- writes and side effects -------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql, code",
    [
        ("DELETE FROM GL_Journals WHERE JournalID = 901", "not_select"),
        ("UPDATE Units SET Status = 'Sold'", "not_select"),
        ("INSERT INTO Units VALUES (11, 'X', 'Y', 'Z')", "not_select"),
        ("DROP TABLE Units", "not_select"),
        ("ALTER TABLE Units ADD x INT", "not_select"),
        ("TRUNCATE TABLE Units", "not_select"),
        ("MERGE INTO Units USING Units s ON 1 = 1 WHEN MATCHED THEN DELETE;", None),
        ("EXEC xp_cmdshell 'dir'", None),
        ("SELECT 1 FROM Units; DROP TABLE Units", "multiple_statements"),
        ("SELECT * FROM Units; DELETE FROM GL_Journals", "multiple_statements"),
        ("SELECT * INTO #t FROM Units", "disallowed_construct"),
        ("SELECT * INTO dbo.Copy FROM GL_Journals", "disallowed_construct"),
        ("WITH d AS (SELECT 1 AS a) DELETE FROM GL_Journals", None),
        ("GRANT SELECT ON GL_Journals TO public", None),
        ("CREATE TABLE x (a INT)", None),
        ("WAITFOR DELAY '00:00:10'", None),
    ],
)
def test_rejects_writes_and_side_effects(sql, code):
    got = rejected(sql)
    if code:
        assert got == code


# --- role allowlist ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM GL_Journals",
        "SELECT * FROM dbo.GL_Journals",
        "SELECT * FROM [GL_Journals]",
        "SELECT * FROM gl_journals",
        "SELECT * FROM Units u JOIN GL_Journals g ON g.JournalID = u.UnitID",
        "SELECT * FROM Units WHERE UnitID IN (SELECT JournalID FROM GL_Journals)",
        "SELECT * FROM Units WHERE EXISTS (SELECT 1 FROM GL_Journals g WHERE g.JournalID = Units.UnitID)",
        "WITH x AS (SELECT * FROM GL_Journals) SELECT * FROM x",
        "SELECT (SELECT MAX(Debit) FROM GL_Journals) AS m FROM Units",
    ],
)
def test_sales_staff_cannot_reach_gl_journals(sql):
    assert rejected(sql, SALES) == "forbidden_table"
    ok(sql)  # Management may


def test_model_cannot_launder_tables_through_cte_names():
    assert rejected("WITH GL_Journals AS (SELECT * FROM Units) SELECT * FROM GL_Journals", MGMT) == "cte_shadowing"
    assert rejected("WITH Units AS (SELECT * FROM GL_Journals) SELECT * FROM Units", SALES) == "cte_shadowing"


@pytest.mark.parametrize(
    "sql, code",
    [
        ("SELECT * FROM sys.tables", "qualified_object"),
        ("SELECT * FROM INFORMATION_SCHEMA.TABLES", "qualified_object"),
        ("SELECT * FROM master.dbo.Units", "qualified_object"),
        ("SELECT * FROM SouthCity.dbo.GL_Journals", "qualified_object"),
        ("SELECT x FROM srv.db.dbo.Units", None),
        ("SELECT * FROM Employees", "unknown_table"),
        ("SELECT * FROM sysobjects", "unknown_table"),
        ("SELECT * FROM OPENQUERY(srv, 'select 1')", None),
        ("SELECT * FROM OPENROWSET(BULK 'c:\\x', SINGLE_CLOB) a", None),
        ("SELECT * FROM dbo.fn_x(1)", None),
        ("SELECT 1 AS a", "no_table"),
    ],
)
def test_rejects_system_external_and_unknown_objects(sql, code):
    got = rejected(sql)
    if code:
        assert got == code


# --- constructs outside the subset -------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Units WITH (NOLOCK)",
        "SELECT UnitID FROM Units UNION SELECT JournalID FROM GL_Journals",
        "SELECT UnitID FROM Units UNION ALL SELECT UnitID FROM Units",
        "SELECT ROW_NUMBER() OVER (ORDER BY UnitID) AS r FROM Units",
        "SELECT @@VERSION AS v FROM Units",
        "SELECT @x AS v FROM Units",
        "SELECT USER_NAME() AS u FROM Units",
        "SELECT DB_NAME() AS d FROM Units",
        "SELECT SYSTEM_USER AS s FROM Units",
        "SELECT NEXT VALUE FOR s AS n FROM Units",
        "SELECT * FROM Units FOR JSON PATH",
        "SELECT * FROM Units ORDER BY UnitID OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY",
        "SELECT * FROM (VALUES (1), (2)) v(a)",
        "SELECT * FROM Units u CROSS APPLY (SELECT 1 AS x) t",
        "SELECT * FROM Units -- trailing comment",
        "SELECT * FROM Units /* hidden */ WHERE 1 = 1",
        "SELECT TOP 10 PERCENT * FROM Units",
        "SELECT TOP 5 WITH TIES * FROM Units ORDER BY UnitID",
    ],
)
def test_rejects_constructs_outside_subset(sql):
    rejected(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Units, Sales_Contracts",
        "SELECT * FROM Units CROSS JOIN Sales_Contracts",
        "SELECT * FROM Units u JOIN Sales_Contracts c ON 1 = 1",
    ],
)
def test_rejects_cartesian_joins(sql):
    assert rejected(sql) == "cartesian_join"


def test_rejects_recursive_cte():
    # sqlglot sees the UNION first; either way it must not pass.
    rejected("WITH r AS (SELECT UnitID FROM Units UNION ALL SELECT r.UnitID FROM r JOIN Units u "
             "ON u.UnitID = r.UnitID) SELECT * FROM r")
    assert rejected("WITH r AS (SELECT UnitID FROM r) SELECT * FROM r") == "recursive_cte"


def test_rejects_excessive_complexity():
    joins = " ".join(f"JOIN Units u{i} ON u{i}.UnitID = u0.UnitID" for i in range(1, 8))
    assert rejected(f"SELECT u0.UnitID FROM Units u0 {joins}") == "too_complex"
    nested = "SELECT UnitID FROM Units"
    for _ in range(7):
        nested = f"SELECT UnitID FROM Units WHERE UnitID IN ({nested})"
    assert rejected(nested) == "too_complex"


@pytest.mark.parametrize("sql", ["", "   ", "SELEC * FROM Units", "SELECT * FROM", "x" * 5000])
def test_rejects_empty_unparseable_or_oversized(sql):
    rejected(sql)


# --- row limit ---------------------------------------------------------------------------------

def test_adds_top_when_missing():
    vq = ok("SELECT UnitNumber FROM Units", max_rows=50)
    assert vq.sql.startswith("SELECT TOP 51 ")
    assert vq.row_limit == 51


def test_clamps_large_top_and_keeps_small_top():
    assert ok("SELECT TOP 100000 * FROM Units", max_rows=50).row_limit == 51
    small = ok("SELECT TOP 3 * FROM Units", max_rows=50)
    assert small.row_limit == 3 and "TOP 3" in small.sql


def test_limit_applies_to_outer_query_only():
    vq = ok("SELECT COUNT(*) AS n FROM (SELECT TOP 1000 UnitID FROM Units) x", max_rows=10)
    assert "TOP 1000" in vq.sql and vq.sql.startswith("SELECT TOP 11 ")


def test_rendered_sql_is_revalidated_and_stable():
    first = ok("select u.UnitNumber from units u where u.Status = N'Vacant'")
    assert ok(first.sql).sql == first.sql


# --- regressions from the security review -----------------------------------------------------

@pytest.mark.parametrize(
    "sql, tables, code",
    [
        # A CTE declared later must not hide a system object referenced earlier (leak verified on SQL Server).
        ("WITH leak AS (SELECT name FROM sysobjects), sysobjects AS (SELECT UnitID FROM dbo.Units) "
         "SELECT name FROM leak", SALES, "unknown_table"),
        ("WITH a AS (SELECT * FROM b), b AS (SELECT UnitID FROM Units) SELECT * FROM a", MGMT, "unknown_table"),
        # SQL Server ignores trailing spaces in names: [GL_Journals ] is GL_Journals.
        ("WITH x AS (SELECT * FROM [GL_Journals ]), [GL_Journals ] AS (SELECT UnitID FROM Units) SELECT * FROM x",
         SALES, None),
        ("SELECT * FROM [GL_Journals ]", SALES, None),
        ("SELECT * FROM [Units ]", MGMT, None),
    ],
)
def test_cte_scope_and_identifier_tricks(sql, tables, code):
    got = rejected(sql, tables)
    if code:
        assert got == code


def test_cte_may_use_earlier_cte():
    vq = ok("WITH a AS (SELECT UnitID FROM Units), b AS (SELECT UnitID FROM a) SELECT * FROM b")
    assert vq.tables == {"Units"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) AS n FROM Units a JOIN Units b ON a.UnitID = b.UnitID OR 1 = 1",
        "SELECT COUNT(*) AS n FROM Units a JOIN Units b ON a.UnitID = a.UnitID",
        "SELECT COUNT(*) AS n FROM Units a JOIN Sales_Contracts c ON UnitID = UnitID",
        "SELECT COUNT(*) AS n FROM Units a JOIN Sales_Contracts c ON (a.UnitID = c.UnitID OR c.ContractID > 0)",
    ],
)
def test_join_must_really_link_both_sides(sql):
    assert rejected(sql) == "cartesian_join"


def test_join_link_may_be_one_of_several_conditions():
    ok("SELECT * FROM Units u JOIN Sales_Contracts c ON c.UnitID = u.UnitID AND c.SalesAmount > 0")


def test_cte_reuse_counts_toward_complexity():
    refs = " ".join(f"JOIN t t{i} ON t{i}.UnitID = t0.UnitID" for i in range(1, 7))
    sql = (f"WITH t AS (SELECT u.UnitID FROM Units u JOIN Sales_Contracts c ON c.UnitID = u.UnitID) "
           f"SELECT t0.UnitID FROM t t0 {refs}")
    assert rejected(sql) == "too_complex"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT REPLACE(REPLACE(CAST('a' AS VARCHAR(MAX)), 'a', 'aaa'), 'a', 'aaa') AS Huge FROM Units",
        "SELECT CAST(UnitNumber AS VARCHAR(MAX)) AS x FROM Units",
        "SELECT CAST(UnitNumber AS NVARCHAR(4000)) AS x FROM Units",
        "SELECT CAST(UnitNumber AS VARCHAR) AS x FROM Units",
        "SELECT CAST(UnitNumber AS XML) AS x FROM Units",
        "SELECT CAST(UnitNumber AS VARBINARY(10)) AS x FROM Units",
    ],
)
def test_unbounded_values_are_rejected(sql):
    assert rejected(sql) == "disallowed_construct"


def test_bounded_casts_are_allowed():
    ok("SELECT CAST(u.UnitID AS DECIMAL(18,2)) AS a, CAST(u.UnitNumber AS NVARCHAR(50)) AS b, "
       "CAST(s.ContractDate AS DATE) AS c FROM Units u JOIN Sales_Contracts s ON s.UnitID = u.UnitID")


def test_each_join_must_link_the_table_it_adds():
    """The third join's condition never mentions c, so c multiplies the rows."""
    assert rejected("SELECT COUNT(*) AS n FROM Units a JOIN Units b ON a.UnitID = b.UnitID "
                    "JOIN Units c ON a.UnitID = b.UnitID") == "cartesian_join"
    ok("SELECT COUNT(*) AS n FROM Units a JOIN Sales_Contracts b ON b.UnitID = a.UnitID "
       "JOIN Invoices_Billing c ON c.ContractID = b.ContractID")


def test_cte_expansion_is_charged_per_reference():
    """8 syntactic references expanding to 16 physical reads."""
    sql = ("WITH a AS (SELECT x.UnitID, 1 AS k FROM Units x JOIN Units y ON x.UnitID = y.UnitID), "
           "b AS (SELECT x.k FROM a x JOIN a y ON x.k = y.k), "
           "c AS (SELECT x.k FROM b x JOIN b y ON x.k = y.k) "
           "SELECT COUNT(*) AS n FROM c x JOIN c y ON x.k = y.k")
    assert rejected(sql) == "too_complex"


def test_row_width_is_bounded():
    """Wide projections of CONCAT chains could pull hundreds of MB."""
    cols = ", ".join(["UnitNumber"] * 41)
    assert rejected(f"SELECT {cols} FROM Units") == "too_complex"
    assert rejected("SELECT CONCAT(UnitNumber, UnitNumber, UnitNumber, UnitNumber, UnitNumber, UnitNumber, "
                    "UnitNumber) AS s FROM Units") == "too_complex"


def test_len_is_allowed_despite_parser_internal_cast():
    """LEN() is allowed despite sqlglot's internal TEXT cast."""
    ok("SELECT LEN(UnitNumber) AS n FROM Units")
    assert rejected("SELECT CAST(UnitNumber AS TEXT) AS t FROM Units") == "disallowed_construct"


def test_money_inference_is_scoped():
    """An alias inside a WHERE subquery must not make the outer column money;
    scaling by a literal keeps money."""
    vq = ok("SELECT AVG(CAST(c.UnitID AS DECIMAL(18,2))) AS n FROM Sales_Contracts c "
            "WHERE EXISTS (SELECT SUM(SalesAmount) AS n FROM Sales_Contracts)")
    assert "n" not in vq.money_columns
    assert "total" in ok("SELECT SUM(SalesAmount * 1) AS Total FROM Sales_Contracts").money_columns
    assert "rasio" not in ok("SELECT SUM(AmountPaid) / SUM(AmountBilled) AS Rasio FROM Invoices_Billing").money_columns


def test_join_must_link_to_a_source_of_the_same_select():
    """Correlating the join to an outer table leaves b and c unlinked."""
    sql = ("SELECT a.UnitID FROM Units a WHERE EXISTS (SELECT 1 AS x FROM Units b "
           "JOIN Units c ON c.UnitID = a.UnitID)")
    assert rejected(sql) == "cartesian_join"


def test_joins_are_counted_after_cte_expansion():
    """7 joins hidden behind CTE references."""
    sql = ("WITH a AS (SELECT x.UnitID FROM Units x JOIN Units y ON y.UnitID = x.UnitID "
           "JOIN Units z ON z.UnitID = x.UnitID JOIN Units w ON w.UnitID = x.UnitID) "
           "SELECT p.UnitID FROM a p JOIN a q ON q.UnitID = p.UnitID")
    assert rejected(sql) == "too_complex"


def test_money_from_one_cte_does_not_leak_into_a_sibling():
    """Alias n is money in CTE m, but AVG(n) in CTE k is over UnitIDs."""
    vq = ok("WITH m AS (SELECT SUM(SalesAmount) AS n FROM Sales_Contracts), "
            "k AS (SELECT CAST(UnitID AS DECIMAL(18,2)) AS n FROM Units) SELECT AVG(n) AS r FROM k")
    assert "r" not in vq.money_columns
    vq = ok("WITH m AS (SELECT SUM(SalesAmount) AS n FROM Sales_Contracts) SELECT n AS r FROM m")
    assert "r" in vq.money_columns


def test_aggregating_queries_have_no_placeholder_columns():
    """A (NULL, 2) group is real data, not an outer-join placeholder."""
    vq = ok("SELECT c.CustomerName, COUNT(u.UnitID) AS JumlahUnit FROM Units u "
            "LEFT JOIN Sales_Contracts c ON c.UnitID = u.UnitID GROUP BY c.CustomerName")
    assert vq.plain_columns == frozenset()
    assert ok("SELECT c.ContractID FROM Units u LEFT JOIN Sales_Contracts c ON c.UnitID = u.UnitID").plain_columns \
        == {"contractid"}


def test_money_columns_are_derived_from_the_sql():
    vq = ok("WITH t AS (SELECT ContractID, SUM(AmountBilled - AmountPaid) AS Sisa FROM Invoices_Billing "
            "GROUP BY ContractID) SELECT t.Sisa, COUNT(*) AS Jumlah, AVG(CAST(t.ContractID AS DECIMAL(18,2))) AS AvgID, "
            "MAX(t.Sisa) AS Terbesar FROM t GROUP BY t.Sisa")
    assert {"sisa", "terbesar"} <= vq.money_columns
    assert not {"jumlah", "avgid"} & vq.money_columns
