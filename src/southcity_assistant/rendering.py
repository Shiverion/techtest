"""Turn query results into the response: Rupiah formatting, tables, and the answer sentence.

Every number in the answer comes from the database. The model only supplies a sentence template with
placeholders; if the template is unusable (unknown placeholder, an amount or number not taken from the
question, a missing first row, a total over truncated rows) a deterministic summary is used instead.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .contracts import Section
from .db import QueryResult

_PLACEHOLDER = re.compile(r"\{q([1-3])\.(?:(sum)\.)?([A-Za-z_]\w*)\}")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# Literal template text may not state amounts itself: only placeholders may.
_NUMBER_CONTEXT = re.compile(
    # Only calendar words, a unit prefix (SM 1201) or a period separator (10/2026) may precede an echoed
    # number. Nouns such as "kontrak" are excluded: "Jumlah kontrak 10" would state a count.
    r"(?:\b(?i:bulan|periode|tahun|year|month|"
    r"januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember|"
    r"january|february|march|may|june|july|august|october|december)|\b[A-Z]{2}|/)\s*$"
)
_NUMBER_WORDS = re.compile(
    r"\b(?:nol|satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|sebelas|\w+belas|\w*puluh|"
    r"seratus|\w*ratus|seribu|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"\w+teen|twenty|thirty|forty|fifty|hundred|dozen)\b",
    re.IGNORECASE,
)
_AMOUNT_WORDS = re.compile(r"\b(?:rp|idr|rupiah|ribu|juta|miliar|milyar|triliun|thousand|million|billion)\b|%",
                           re.IGNORECASE)


def format_rupiah(value: Decimal | int) -> str:
    """Indonesian format: Rp 10.500.000 or Rp 1.234,50 (cents only when non-zero)."""
    amount = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    whole, _, cents = f"{abs(amount):.2f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    return f"{sign}Rp {grouped}" + (f",{cents}" if cents != "00" else "")


def format_number(value: Decimal | int) -> str:
    """Non-monetary numbers, Indonesian separators: 1.234,5."""
    text = f"{Decimal(value).normalize():f}"
    whole, _, frac = text.partition(".")
    sign = "-" if whole.startswith("-") else ""
    grouped = f"{int(whole.lstrip('-') or 0):,}".replace(",", ".")
    return f"{sign}{grouped}" + (f",{frac}" if frac else "")


def display(value: Any, money: bool = False) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format_rupiah(value) if money else format_number(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def raw(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)  # exact; never through float
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def build_section(title: str, sql: str, result: QueryResult) -> Section:
    money = [result.is_money(c) for c in result.columns]
    return Section(
        title=title,
        sql=sql,
        columns=result.columns,
        rows=[[display(v, m) for v, m in zip(row, money)] for row in result.rows],
        raw_rows=[[raw(v) for v in row] for row in result.rows],
        row_count=len(result.rows),
        truncated=result.truncated,
    )


def render_answer(template: str, question: str, results: list[tuple[str, QueryResult]]) -> str:
    filled = _fill_template(template, question, results) if template.strip() else None
    return filled or _default_summary(results)


def model_text_ok(text: str, question: str) -> bool:
    """Whether model-written text (answer template prose, section title, refusal reason) states no figures
    of its own: no amounts or amount units, no numbers written as words, and digits only when echoed from
    the question as a period, year or unit code."""
    if _AMOUNT_WORDS.search(text) or _NUMBER_WORDS.search(text):
        return False
    question_numbers = set(_NUMBER.findall(question))
    for match in _NUMBER.finditer(text):
        # "adalah 10." would turn the question's month number into a fabricated fact.
        context = text[max(0, match.start() - 16) : match.start()]
        if match.group() not in question_numbers or not _NUMBER_CONTEXT.search(context):
            return False
    return True


def _fill_template(template: str, question: str, results: list[tuple[str, QueryResult]]) -> str | None:
    literal_text = _PLACEHOLDER.sub(" ", template)
    if "{" in literal_text or "}" in literal_text:
        return None  # unknown placeholder syntax
    if not model_text_ok(literal_text, question):
        return None  # the model stated a figure instead of using a placeholder
    if any(r.truncated for _, r in results):
        return None  # first-row values could be fine, but totals/counts over a cut-off list are not

    def substitute(match: re.Match[str]) -> str:
        index, agg, column = int(match.group(1)) - 1, match.group(2), match.group(3)
        if index >= len(results):
            raise LookupError
        result = results[index][1]
        if column == "row_count" and not agg:
            return str(len(result.rows))
        if column not in result.columns:
            raise LookupError
        col = result.columns.index(column)
        money = result.is_money(column)
        if agg == "sum":
            values = [row[col] for row in result.rows if isinstance(row[col], (int, Decimal))]
            return display(sum(values, Decimal(0)), money) or "0"
        if not result.rows:
            raise LookupError
        shown = display(result.rows[0][col], money)
        return shown if shown is not None else "tidak tersedia"

    try:
        return _PLACEHOLDER.sub(substitute, template).strip()
    except LookupError:
        return None


def _default_summary(results: list[tuple[str, QueryResult]]) -> str:
    parts: list[str] = []
    for title, result in results:
        if result.truncated and not result.rows:
            parts.append(f"{title}: ada data, tetapi melebihi batas ukuran output sehingga tidak ditampilkan.")
        elif result.truncated:
            parts.append(f"{title}: menampilkan {len(result.rows)} baris pertama (hasil dipotong sesuai batas).")
        elif not result.rows:
            parts.append(f"{title}: tidak ada data.")
        elif len(result.rows) == 1 and len(result.columns) == 1:
            parts.append(f"{title}: {display(result.rows[0][0], result.is_money(result.columns[0]))}.")
        else:
            parts.append(f"{title}: {len(result.rows)} baris ditemukan.")
    return " ".join(parts)
