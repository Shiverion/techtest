"""Positive-subset T-SQL policy.

Model-generated SQL is never executed as written. It is parsed with sqlglot (T-SQL dialect), every AST node
is checked against an allowlist of node types, every table reference is resolved against the caller's role
(CTE names are visible only after their declaration, as in SQL Server), and the query that runs is the
policy's own re-rendering of the validated tree, re-validated once more.

Anything the parser does not understand, or that falls outside the subset below, is rejected: this module
fails closed. It is one layer of several; SQL Server permissions on the runtime login are the final one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ..schema import MONEY_COLUMNS, TABLES

DIALECT = "tsql"
MAX_SQL_CHARS = 4000
MAX_SELECTS = 6  # outer query + subqueries + CTE bodies
MAX_JOINS = 6
MAX_TABLE_REFS = 8  # physical table reads after expanding every CTE reference into its body
MAX_OUTPUT_COLUMNS = 40  # bounds the width of a fetched row (with cell widths bounded by the type rules)
MAX_CONCAT_ARGS = 6
MAX_STRING_TYPE_LENGTH = 400

# Table and CTE names: plain identifiers only. Rejects tricks such as [GL_Journals ] (SQL Server ignores
# trailing spaces when resolving names) or names that only differ by exotic characters.
_OBJECT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Column names and aliases: letters, digits, underscore, inner single spaces.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?: [A-Za-z0-9_]+)*$")

# Node types a read-only reporting query over this schema may contain. Everything else is rejected:
# DML/DDL, EXEC, INTO, UNION, hints, windows, VALUES, variables, OPENQUERY/OPENROWSET, unknown
# (Anonymous) functions, metadata functions, FOR JSON/XML, OFFSET/FETCH, sequences, REPLACE/REPLICATE
# (unbounded string growth).
ALLOWED_NODES: frozenset[type[exp.Expr]] = frozenset(
    {
        # query structure
        exp.Select, exp.From, exp.Join, exp.Table, exp.TableAlias, exp.Identifier, exp.Column,
        exp.Star, exp.Alias, exp.Where, exp.Group, exp.Having, exp.Order, exp.Ordered, exp.Limit,
        exp.Distinct, exp.Subquery, exp.With, exp.CTE, exp.Exists, exp.Paren,
        # literals
        exp.Literal, exp.National, exp.Boolean, exp.Null, exp.DataType, exp.DataTypeParam, exp.Var,
        # predicates and arithmetic
        exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like,
        exp.In, exp.Between, exp.Is, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Neg, exp.Case, exp.If,
        # scalar functions
        exp.Cast, exp.Convert, exp.Coalesce, exp.Round, exp.Abs, exp.Year, exp.Month, exp.Day,
        exp.Extract, exp.DateFromParts, exp.DateAdd, exp.DateDiff, exp.LastDay, exp.TsOrDsToDate,
        exp.CurrentDate, exp.CurrentTimestamp, exp.Upper, exp.Lower, exp.Trim, exp.Concat,
        exp.Length, exp.Substring,
        # aggregates
        exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max,
    }
)

_ALLOWED_TYPES = {
    exp.DataType.Type.INT, exp.DataType.Type.BIGINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.TINYINT,
    exp.DataType.Type.DECIMAL, exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, exp.DataType.Type.MONEY,
    exp.DataType.Type.DATE, exp.DataType.Type.DATETIME, exp.DataType.Type.DATETIME2,
    exp.DataType.Type.VARCHAR, exp.DataType.Type.NVARCHAR, exp.DataType.Type.CHAR, exp.DataType.Type.NCHAR,
    exp.DataType.Type.BIT,
}
_STRING_TYPES = {exp.DataType.Type.VARCHAR, exp.DataType.Type.NVARCHAR, exp.DataType.Type.CHAR,
                 exp.DataType.Type.NCHAR}


class PolicyViolation(Exception):
    """The SQL is outside the permitted subset. `code` is stable; `message` is safe to show."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str  # the policy's own rendering; the only text that may be executed
    tables: frozenset[str]  # canonical physical table names read by the query
    row_limit: int  # rows requested from the server (max_rows + 1 to detect truncation)
    money_columns: frozenset[str] = frozenset()  # casefolded output names holding IDR amounts
    plain_columns: frozenset[str] = frozenset()  # casefolded outputs that are bare column references


def validate(sql: str, allowed_tables: frozenset[str], max_rows: int) -> ValidatedQuery:
    """Validate model SQL for a role and return the query that may run."""
    tree = _parse_single_select(sql)
    tables = _check_tree(tree, allowed_tables)
    row_limit = _apply_row_limit(tree, max_rows)
    rendered = tree.sql(dialect=DIALECT)

    # Defence against parser/generator differentials: what runs must pass the same checks again.
    second = _parse_single_select(rendered)
    if _check_tree(second, allowed_tables) != tables:
        raise PolicyViolation("policy_internal", "Query could not be re-validated.")
    return ValidatedQuery(sql=rendered, tables=tables, row_limit=row_limit,
                          money_columns=_money_outputs(second), plain_columns=_plain_outputs(second))


def _parse_single_select(sql: str) -> exp.Select:
    if not sql or not sql.strip():
        raise PolicyViolation("empty_sql", "No SQL was produced.")
    if len(sql) > MAX_SQL_CHARS:
        raise PolicyViolation("sql_too_long", "Generated SQL exceeds the length limit.")
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except SqlglotError:
        raise PolicyViolation("unparseable_sql", "Generated SQL could not be parsed safely.") from None
    if len(statements) != 1:
        raise PolicyViolation("multiple_statements", "Exactly one SQL statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise PolicyViolation(
            "not_select", f"Only SELECT queries are allowed (got {type(tree).__name__.upper()})."
        )
    return tree


def _check_tree(tree: exp.Select, allowed_tables: frozenset[str]) -> frozenset[str]:
    selects = joins = 0
    for node in tree.walk():
        if node.comments:
            raise PolicyViolation("comments", "SQL comments are not allowed.")
        if type(node) not in ALLOWED_NODES:
            raise PolicyViolation(
                "disallowed_construct", f"SQL construct not allowed: {type(node).__name__}."
            )
        if isinstance(node, exp.Select):
            selects += 1
        elif isinstance(node, exp.Join):
            joins += 1
            _check_join(node)
        elif isinstance(node, exp.Identifier):
            if not _IDENTIFIER.match(node.name):
                raise PolicyViolation("bad_identifier", "Identifiers must be plain names.")
        elif isinstance(node, exp.DataType):
            _check_type(node)
        elif isinstance(node, exp.Concat) and len(node.expressions) > MAX_CONCAT_ARGS:
            raise PolicyViolation("too_complex", f"CONCAT takes at most {MAX_CONCAT_ARGS} arguments.")
        elif isinstance(node, exp.With) and node.parent is not tree:
            raise PolicyViolation("disallowed_construct", "WITH is only allowed at the top level.")
        elif isinstance(node, exp.Literal) and not node.is_string and len(node.this) > 30:
            raise PolicyViolation("disallowed_construct", "Numeric literal too long.")
    if selects > MAX_SELECTS:
        raise PolicyViolation("too_complex", "Query has too many nested SELECTs.")
    if joins > MAX_JOINS:
        raise PolicyViolation("too_complex", "Query has too many joins.")
    for select in tree.find_all(exp.Select):
        if len(select.expressions) > MAX_OUTPUT_COLUMNS:
            raise PolicyViolation("too_complex", f"At most {MAX_OUTPUT_COLUMNS} output columns are allowed.")
    _check_limits(tree)
    return _resolve_tables(tree, allowed_tables)


def _check_type(node: exp.DataType) -> None:
    # sqlglot wraps LEN()'s argument in an implicit CAST(... AS TEXT) that is not emitted as T-SQL.
    cast = node.parent
    if node.this == exp.DataType.Type.TEXT and isinstance(cast, exp.Cast) and isinstance(cast.parent, exp.Length):
        return
    if node.this not in _ALLOWED_TYPES:
        raise PolicyViolation("disallowed_construct", f"Type not allowed: {node.this.value}.")
    if node.this in _STRING_TYPES:
        params = [p.name for p in node.expressions]
        if not params or not all(p.isdigit() and int(p) <= MAX_STRING_TYPE_LENGTH for p in params):
            raise PolicyViolation(
                "disallowed_construct", f"String casts need an explicit length <= {MAX_STRING_TYPE_LENGTH}."
            )


def _check_join(join: exp.Join) -> None:
    """Every join must link the source it adds to a different source, by equality of qualified columns,
    in a top-level AND conjunct.

    Rejects comma joins, CROSS JOIN, USING, ON 1=1, ON a.x = a.x, ON a.x = b.x OR 1=1, and a third join
    whose condition never mentions the table it adds (JOIN Units c ON a.x = b.x). The other side must be a
    source already joined in the same SELECT, not an outer query's table (a correlated join would leave
    this SELECT's own sources unlinked).
    """
    joined = _source_name(join.this)
    select = join.parent
    earlier: set[str] = set()
    if isinstance(select, exp.Select):
        from_ = select.args.get("from_")
        if from_ is not None:
            earlier.add(_source_name(from_.this))
        for other in select.args.get("joins") or []:
            if other is join:
                break
            earlier.add(_source_name(other.this))
    on = join.args.get("on")
    if on is None:
        raise PolicyViolation("cartesian_join", "Joins must have an ON condition.")
    conjuncts = list(on.flatten()) if isinstance(on, exp.And) else [on]
    for conjunct in conjuncts:
        while isinstance(conjunct, exp.Paren):
            conjunct = conjunct.this
        if (
            isinstance(conjunct, exp.EQ)
            and isinstance(conjunct.left, exp.Column)
            and isinstance(conjunct.right, exp.Column)
            and conjunct.left.table
            and conjunct.right.table
            and {conjunct.left.table.casefold(), conjunct.right.table.casefold()} - {joined} <= earlier
            and joined in (conjunct.left.table.casefold(), conjunct.right.table.casefold())
            and conjunct.left.table.casefold() != conjunct.right.table.casefold()
        ):
            return
    raise PolicyViolation(
        "cartesian_join",
        "Join condition must equate qualified columns of the two joined tables (e.g. ON c.UnitID = u.UnitID).",
    )


def _source_name(source: exp.Expr) -> str:
    return source.alias_or_name.casefold() if isinstance(source, (exp.Table, exp.Subquery)) else ""


def _check_limits(tree: exp.Select) -> None:
    for limit in tree.find_all(exp.Limit):
        options = limit.args.get("limit_options")
        if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
            raise PolicyViolation("disallowed_construct", "TOP PERCENT / WITH TIES are not allowed.")
        value = limit.expression
        if not (isinstance(value, exp.Literal) and not value.is_string and value.this.isdigit()):
            raise PolicyViolation("disallowed_construct", "TOP must be a whole-number literal.")


def _resolve_tables(tree: exp.Select, allowed_tables: frozenset[str]) -> frozenset[str]:
    """Resolve each table reference in SQL Server's scope order.

    A CTE body sees only the CTEs declared before it; the main query sees all of them. A name that is
    not a visible CTE must be an allowed application table (optionally dbo-qualified).
    """
    canonical = {name.casefold(): name for name in TABLES}
    used: set[str] = set()
    cost: dict[str, tuple[int, int]] = {}  # CTE name -> (table reads, joins) its body expands to

    def resolve(scope_root: exp.Expr, visible: set[str], skip: exp.Expr | None = None) -> tuple[int, int]:
        joins = sum(1 for j in scope_root.find_all(exp.Join) if not _inside(j, skip))
        reads = 0
        for table in scope_root.find_all(exp.Table):
            if _inside(table, skip):
                continue  # CTE bodies were resolved in their own scope
            if not isinstance(table.this, exp.Identifier) or not _OBJECT_NAME.match(table.name):
                raise PolicyViolation("table_function", "Only plain table names are allowed as sources.")
            if table.args.get("catalog") is not None:
                raise PolicyViolation("qualified_object", "Cross-database references are not allowed.")
            schema = table.args.get("db")
            name = table.name.casefold()
            if schema is None and name in visible:
                reads += cost[name][0]  # each reference re-runs the CTE body, joins included
                joins += cost[name][1]
                continue
            reads += 1
            if schema is not None and schema.name.casefold() != "dbo":
                raise PolicyViolation("qualified_object", "Only dbo tables are allowed.")
            if name not in canonical:
                raise PolicyViolation("unknown_table", f"Unknown table: {table.name}.")
            physical = canonical[name]
            if physical not in allowed_tables:
                raise PolicyViolation("forbidden_table", f"Your role may not read {physical}.")
            used.add(physical)
        return reads, joins

    with_ = tree.args.get("with_")
    declared: set[str] = set()
    if with_ is not None:
        for cte in with_.expressions:
            name = cte.alias_or_name
            if not _OBJECT_NAME.match(name):
                raise PolicyViolation("bad_identifier", "CTE names must be plain identifiers.")
            key = name.casefold()
            if key in canonical:
                raise PolicyViolation("cte_shadowing", "A CTE may not reuse a table name.")
            if key in declared:
                raise PolicyViolation("cte_shadowing", "Duplicate CTE name.")
            if any(t.name.casefold() == key for t in cte.this.find_all(exp.Table)):
                raise PolicyViolation("recursive_cte", "Recursive CTEs are not allowed.")
            cost[key] = resolve(cte.this, set(declared))
            declared.add(key)
    refs, joins = resolve(tree, declared, skip=with_)

    if refs > MAX_TABLE_REFS:
        raise PolicyViolation("too_complex", "Query references too many tables.")
    if joins > MAX_JOINS:
        raise PolicyViolation("too_complex", "Query has too many joins once CTEs are expanded.")
    if not used:
        raise PolicyViolation("no_table", "Query must read at least one application table.")
    return frozenset(used)


def _inside(node: exp.Expr, ancestor: exp.Expr | None) -> bool:
    if ancestor is None:
        return False
    parent = node.parent
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def _money_outputs(tree: exp.Select) -> frozenset[str]:
    """Outer output names that carry IDR amounts.

    Money is a schema money column, or an output of a CTE / FROM-subquery that is money, carried through
    sums, differences, MIN/MAX/AVG, COALESCE, CASE and scaling by a number literal. Counts, ratios of two
    columns and date parts are not money. Only sources that feed a SELECT count: an alias inside a WHERE
    subquery does not leak outwards.
    """
    base = {c.casefold() for c in MONEY_COLUMNS}

    cte_money: dict[str, set[str]] = {}

    def outputs(select: exp.Select, known: set[str]) -> set[str]:
        known = set(known)
        for table in select.find_all(exp.Table):  # CTEs this SELECT reads contribute their money outputs
            if table.find_ancestor(exp.Select) is select:
                known |= cte_money.get(table.name.casefold(), set())
        for source in select.find_all(exp.Subquery):
            if isinstance(source.parent, (exp.From, exp.Join)) and isinstance(source.this, exp.Select):
                known |= outputs(source.this, known)
        found = set()
        for projection in select.expressions:
            expr = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(expr, exp.Star) and _is_money(expr, known):
                found.add(projection.alias_or_name.casefold())
        return found

    with_ = tree.args.get("with_")
    if with_ is not None:
        for cte in with_.expressions:
            if isinstance(cte.this, exp.Select):
                cte_money[cte.alias_or_name.casefold()] = outputs(cte.this, base)
    return frozenset(base | outputs(tree, base))


def _is_money(expr: exp.Expr, money: set[str]) -> bool:
    if expr.find(exp.Count, exp.Year, exp.Month, exp.Day, exp.Extract, exp.Length, exp.DateDiff, exp.Select):
        return False
    for op in expr.find_all(exp.Mul, exp.Div):
        if not any(isinstance(side, exp.Literal) and not side.is_string for side in (op.left, op.right)):
            return False  # column x column is not an IDR amount
    return any(c.name.casefold() in money for c in expr.find_all(exp.Column))


def _plain_outputs(tree: exp.Select) -> frozenset[str]:
    """Outer-query outputs that are bare column references (not aggregates or COALESCE/CASE results).
    In a non-aggregating query, a row in which all of them are NULL is the placeholder an outer join
    produces for 'no match'. Aggregating queries return none: a (NULL, 2) group is real data."""
    if tree.args.get("group") or any(p.find(exp.AggFunc) for p in tree.expressions):
        return frozenset()
    names = set()
    for projection in tree.expressions:
        expr = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(expr, exp.Column) and not isinstance(expr.this, exp.Star):
            names.add(projection.alias_or_name.casefold())
    return frozenset(names)


def _apply_row_limit(tree: exp.Select, max_rows: int) -> int:
    """Clamp the outer TOP so at most max_rows + 1 rows come back (the +1 reveals truncation)."""
    ceiling = max_rows + 1
    limit = tree.args.get("limit")
    if limit is not None and int(limit.expression.this) <= ceiling:
        return int(limit.expression.this)
    tree.set("limit", exp.Limit(expression=exp.Literal.number(ceiling)))
    return ceiling
