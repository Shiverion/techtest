"""Entity handling around SQL generation: hints before, existence checks after.

Before: word n-grams of the question are matched against known values (customers, units, GL accounts) with
parameterized queries over the role's own login, so the model gets exact spellings. A unit code that
matches nothing is reported straight away.

After validation, before execution: every literal the SQL filters an entity column on (CustomerName =,
IN (...), LIKE) is checked against the database. An unknown customer or unit becomes a "not found" answer
instead of a confident Rp 0; a LIKE that matches several customers becomes a clarification instead of an
aggregate over unrelated people. This check does not depend on the model reporting what it filtered on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import sqlglot
from sqlglot import exp

from .db import Database
from .schema import TABLES
from .security.roles import ROLE_TABLES, Role
from .security.sql_policy import ValidatedQuery

# (table, column, label). Identifiers are constants; only values are bound as parameters.
SOURCES: tuple[tuple[str, str, str], ...] = (
    ("Sales_Contracts", "CustomerName", "customer"),
    ("Units", "UnitNumber", "unit"),
    ("GL_Journals", "AccountName", "gl_account"),
)
FILTER_COLUMNS = {column.casefold(): (table, column, label) for table, column, label in SOURCES} | {
    "accountcode": ("GL_Journals", "AccountCode", "gl_account_code"),
}
MAX_NGRAM = 4
MAX_CANDIDATES = 200
# A unit code: upper-case anywhere ("SM 1201"), or any case right after the word "unit" ("unit sm 9999").
# Ordinary words before a year ("di 2026") are not unit codes.
UNIT_PATTERN = re.compile(
    r"\b(?:(?P<p1>[A-Z]{2})|(?i:unit)\s+(?P<p2>[A-Za-z]{2}))\s?-?\s?(?P<n>\d{3,4})\b"
)
UNIT_PREFIX_SQL = "SELECT DISTINCT LEFT(UnitNumber, CHARINDEX(' ', UnitNumber + ' ') - 1) FROM dbo.Units"
LABELS_ID = {"customer": "Customer", "unit": "Unit", "gl_account": "Akun", "gl_account_code": "Kode akun"}


@dataclass
class EntityHints:
    matches: dict[str, list[str]] = field(default_factory=dict)  # label -> exact DB values
    missing_units: list[str] = field(default_factory=list)  # unit-like mentions with no match

    def prompt_lines(self) -> list[str]:
        lines = [f"- {label}: " + ", ".join(repr(v) for v in values) for label, values in self.matches.items()]
        lines += [f"- unit {u!r} does NOT exist in Units" for u in self.missing_units]
        return lines


@dataclass(frozen=True)
class FilterProblem:
    kind: str  # "not_found" | "ambiguous"
    message: str


def unit_mentions(question: str, known_prefixes: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Unit codes in the question. Known tower prefixes (from the database) match in any case and
    position ("sm 9999"); other codes need upper case or the word "unit" in front."""
    patterns = [UNIT_PATTERN]
    if known_prefixes:
        alternatives = "|".join(re.escape(p) for p in known_prefixes)
        patterns.append(re.compile(rf"\b(?P<p1>(?i:{alternatives}))\s?-?\s?(?P<n>\d{{3,4}})\b"))
    units: list[str] = []
    for pattern in patterns:
        for m in pattern.finditer(question):
            prefix = m.group("p1") or m.groupdict().get("p2")
            unit = f"{prefix.upper()} {m.group('n')}"
            if unit not in units:
                units.append(unit)
    return units


def candidates(question: str) -> list[str]:
    tokens = re.findall(r"[\w&.'-]+", question)
    out: list[str] = []
    seen: set[str] = set()
    for n in range(1, MAX_NGRAM + 1):
        for i in range(len(tokens) - n + 1):
            phrase = " ".join(tokens[i : i + n]).strip(".'-")
            key = phrase.casefold()
            if len(phrase) >= 3 and key not in seen:
                seen.add(key)
                out.append(phrase)
    for unit in unit_mentions(question):  # "SM1201" / "sm-1201" -> "SM 1201"
        if unit.casefold() not in seen:
            seen.add(unit.casefold())
            out.append(unit)
    return out[:MAX_CANDIDATES]


def resolve(db: Database, role: Role, question: str, budget: Callable[[], object] = lambda: None) -> EntityHints:
    """`budget` is called before every lookup and raises once the request deadline has passed."""
    hints = EntityHints()
    terms = candidates(question)
    if not terms:
        return hints
    placeholders = ", ".join(["%s"] * len(terms))
    for table, column, label in SOURCES:
        if table not in ROLE_TABLES[role]:
            continue
        # Collation is case-insensitive, so "siti rahmawati" matches 'Siti Rahmawati'.
        sql = f"SELECT DISTINCT TOP 20 {column} FROM dbo.{table} WHERE {column} IN ({placeholders})"
        budget()
        found = sorted({row[0] for row in db.lookup(role, sql, tuple(terms)) if row[0]})
        if found:
            hints.matches[label] = found
    known_units = {u.casefold() for u in hints.matches.get("unit", [])}
    budget()
    prefixes = [r[0] for r in db.lookup(role, UNIT_PREFIX_SQL, None) if r[0]]
    hints.missing_units = [u for u in unit_mentions(question, prefixes) if u.casefold() not in known_units]
    return hints


def check_filters(
    db: Database, role: Role, queries: list[ValidatedQuery], budget: Callable[[], object] = lambda: None
) -> FilterProblem | None:
    """Verify that the entities the SQL filters on exist, using SQL Server's own semantics.

    Each predicate (=, IN, LIKE) that constrains an entity column (CustomerName, UnitNumber, AccountName,
    AccountCode; possibly wrapped in functions such as UPPER) with literals is re-run on its own against
    the base table: `SELECT DISTINCT TOP 6 col FROM dbo.T AS alias WHERE <predicate>`. No match means the
    entity does not exist; several customers matching a non-exact filter means the name is ambiguous.
    Collation, trailing spaces and function wrappers behave exactly as in the real query.

    Not verified (left to the model and documented): filters on columns renamed through a CTE or
    derived table, and predicates that mix entity columns with other columns or subqueries.
    """
    for query in queries:
        tree = sqlglot.parse_one(query.sql, read="tsql")
        for predicate in tree.find_all(exp.EQ, exp.In, exp.Like):
            target = _entity_predicate(predicate)
            if target is None:
                continue
            column, table, label = target
            if table not in ROLE_TABLES[role]:
                continue  # the SQL policy has already rejected such a query
            alias = column.table or table
            sql = (f"SELECT DISTINCT TOP 6 {alias}.{column.name} FROM dbo.{table} AS {alias} "
                   f"WHERE {predicate.sql(dialect='tsql')}")
            budget()
            found = [r[0] for r in db.lookup(role, sql, None)]
            shown = ", ".join(f"'{v}'" for v in _literals(predicate)) or "filter"
            if not found:
                return FilterProblem("not_found", f"{LABELS_ID[label]} {shown} tidak ditemukan di database.")
            exact = isinstance(predicate, (exp.EQ, exp.In)) and isinstance(_column_side(predicate), exp.Column)
            if label == "customer" and len(found) > 1 and not exact:
                names = ", ".join(found[:5])
                return FilterProblem(
                    "ambiguous", f"Filter {shown} cocok dengan beberapa customer ({names}). Sebutkan nama lengkapnya."
                )
    return None


def _column_side(predicate: exp.Expr) -> exp.Expr:
    return predicate.this if isinstance(predicate, exp.In) else predicate.left


def _entity_predicate(predicate: exp.Expr) -> tuple[exp.Column, str, str] | None:
    """(column, physical table, label) if the predicate filters one entity column of a physical table
    by literals only, and is in force (an even number of NOTs above it)."""
    if sum(1 for a in _ancestors(predicate) if isinstance(a, exp.Not)) % 2:
        return None
    if predicate.find(exp.Select):
        return None
    columns = list(predicate.find_all(exp.Column))
    if len(columns) != 1 or columns[0].name.casefold() not in FILTER_COLUMNS:
        return None
    column = columns[0]
    if isinstance(predicate, exp.Like) and not _inside_side(column, predicate.this):
        return None  # 'x' LIKE col is a pattern supplied by the data, not a filter on it
    table, _, label = FILTER_COLUMNS[column.name.casefold()]
    if _source_table(predicate, column) != table:
        return None
    return column, table, label


def _source_table(predicate: exp.Expr, column: exp.Column) -> str | None:
    """Physical table the column comes from in the predicate's own SELECT, or None (CTE/derived/unknown)."""
    select = predicate.find_ancestor(exp.Select)
    if select is None:
        return None
    sources = [t for t in select.find_all(exp.Table) if t.find_ancestor(exp.Select) is select]
    if column.table:
        candidates = [t for t in sources if t.alias_or_name.casefold() == column.table.casefold()]
    else:
        # Unqualified: the one source whose schema has this column (CTEs and unknown names never match).
        candidates = [t for t in sources if _has_column(t.name, column.name)]
    if len(candidates) != 1:
        return None
    return next((name for name in TABLES if name.casefold() == candidates[0].name.casefold()), None)


def _has_column(table: str, column: str) -> bool:
    for name, meta in TABLES.items():
        if name.casefold() == table.casefold():
            return any(c.name.casefold() == column.casefold() for c in meta.columns)
    return False


def _inside_side(node: exp.Expr, side: exp.Expr) -> bool:
    return node is side or any(a is side for a in _ancestors(node))


def _ancestors(node: exp.Expr):
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def _literals(predicate: exp.Expr) -> list[str]:
    return [node.name for node in predicate.find_all(exp.Literal, exp.National)
            if isinstance(node, exp.National) or node.is_string]


