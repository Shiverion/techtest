"""Schema metadata and business definitions given to the model, filtered per role.

Mirrors schema.sql. A role's prompt only ever contains the tables that role may read, so the model is
never told GL_Journals exists when it is answering for Sales_Staff.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    note: str = ""


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    columns: tuple[Column, ...]


TABLES: dict[str, Table] = {
    t.name: t
    for t in (
        Table(
            "Units",
            "Property units. One row per unit.",
            (
                Column("UnitID", "INT", "primary key"),
                Column("UnitNumber", "VARCHAR(20)", "e.g. 'SM 1201' (tower prefix, space, number)"),
                Column("Tower", "VARCHAR(20)", "'Summer Tower' or 'Spring Tower'"),
                Column("Status", "VARCHAR(20)", "'Occupied', 'Vacant' or 'Sold'"),
            ),
        ),
        Table(
            "Sales_Contracts",
            "Unit sales contracts. One row per contract; a customer may own several units.",
            (
                Column("ContractID", "INT", "primary key"),
                Column("UnitID", "INT", "FK -> Units.UnitID"),
                Column("CustomerName", "VARCHAR(100)", "customer / buyer name"),
                Column("SalesAmount", "DECIMAL(18,2)", "contract value in IDR"),
                Column("ContractDate", "DATE"),
            ),
        ),
        Table(
            "Invoices_Billing",
            "Billing invoices per contract and period. Many invoices per contract.",
            (
                Column("InvoiceID", "INT", "primary key"),
                Column("ContractID", "INT", "FK -> Sales_Contracts.ContractID"),
                Column("PeriodMonth", "INT", "1-12"),
                Column("PeriodYear", "INT"),
                Column("AmountBilled", "DECIMAL(18,2)", "IDR"),
                Column("AmountPaid", "DECIMAL(18,2)", "IDR"),
                Column("DueDate", "DATE"),
            ),
        ),
        Table(
            "GL_Journals",
            "General ledger journal lines (financial accounting).",
            (
                Column("JournalID", "INT", "primary key"),
                Column("AccountCode", "VARCHAR(20)", "e.g. '1101-01'"),
                Column("AccountName", "VARCHAR(100)", "e.g. 'Kas & Bank', 'Pendapatan Sales Unit'"),
                Column("Debit", "DECIMAL(18,2)", "IDR"),
                Column("Credit", "DECIMAL(18,2)", "IDR"),
                Column("TransactionDate", "DATE"),
            ),
        ),
    )
}

MONEY_COLUMNS = frozenset(
    {"SalesAmount", "AmountBilled", "AmountPaid", "Debit", "Credit"}
)

# Business definitions the model must follow. They encode the semantics that valid-but-wrong SQL
# usually gets wrong (join fan-out, losing entities on inner joins, open-ended date ranges).
BUSINESS_RULES: dict[str, tuple[str, ...]] = {
    "Invoices_Billing": (
        "Outstanding billing / arrears ('tunggakan', 'sisa tagihan') for a period = "
        "SUM(AmountBilled - AmountPaid) over that customer's invoices filtered by PeriodMonth and "
        "PeriodYear. Use the period columns, not today's date.",
        "Invoices belong to a customer through Sales_Contracts (Invoices_Billing.ContractID).",
    ),
    "Sales_Contracts": (
        "Total contract value for a customer = SUM(SalesAmount) from Sales_Contracts only. Never join "
        "Invoices_Billing when summing SalesAmount: one contract has many invoices and the sum would "
        "be multiplied.",
        "Match customers with CustomerName = N'<exact name>' when the name is in RESOLVED ENTITIES.",
    ),
    "Units": (
        "Unit history ('riwayat unit') = three separate queries: (1) the unit row from Units filtered "
        "by UnitNumber; (2) its contracts: Units INNER JOIN Sales_Contracts; (3) its invoices: Units "
        "INNER JOIN Sales_Contracts INNER JOIN Invoices_Billing. Use INNER JOIN in (2) and (3) so a unit "
        "without history gives empty sections (never rows of NULLs); query (1) still shows the unit.",
        "Use COALESCE only around aggregates, never on row-level columns.",
    ),
    "GL_Journals": (
        "Filter accounts by AccountName or AccountCode. For a calendar month use a half-open range: "
        "TransactionDate >= 'YYYY-MM-01' AND TransactionDate < first day of the next month.",
    ),
}


def schema_prompt(allowed_tables: frozenset[str]) -> str:
    """Describe only the tables the role may read."""
    lines: list[str] = []
    for table in TABLES.values():
        if table.name not in allowed_tables:
            continue
        lines.append(f"TABLE dbo.{table.name} -- {table.description}")
        for col in table.columns:
            note = f" -- {col.note}" if col.note else ""
            lines.append(f"  {col.name} {col.sql_type}{note}")
        for rule in BUSINESS_RULES.get(table.name, ()):
            lines.append(f"  RULE: {rule}")
        lines.append("")
    return "\n".join(lines).strip()
