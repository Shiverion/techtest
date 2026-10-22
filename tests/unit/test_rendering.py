import datetime as dt
from decimal import Decimal

import pytest

from southcity_assistant.db import QueryResult
from southcity_assistant.rendering import build_section, format_number, format_rupiah, render_answer


@pytest.mark.parametrize(
    "value, expected",
    [
        (Decimal("10500000.00"), "Rp 10.500.000"),
        (Decimal("2700000000"), "Rp 2.700.000.000"),
        (Decimal("0"), "Rp 0"),
        (Decimal("1234.5"), "Rp 1.234,50"),
        (Decimal("-5000"), "-Rp 5.000"),
        (Decimal("999.995"), "Rp 1.000"),
        (1500000000, "Rp 1.500.000.000"),
    ],
)
def test_format_rupiah(value, expected):
    assert format_rupiah(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [(Decimal("5.500000"), "5,5"), (Decimal("1234567.25"), "1.234.567,25"), (Decimal("-3"), "-3"), (0, "0")],
)
def test_format_number(value, expected):
    assert format_number(value) == expected


MONEY = frozenset({"debit", "credit", "salesamount", "amountbilled", "amountpaid", "totaltunggakan"})


def result(columns, rows, truncated=False):
    return QueryResult(columns=columns, rows=rows, truncated=truncated, money_columns=MONEY)


def test_section_formats_money_dates_and_keeps_exact_raw_values():
    r = result(["JournalID", "Debit", "TransactionDate", "Note"],
               [(901, Decimal("1500000000.00"), dt.date(2026, 1, 15), None)])
    section = build_section("Jurnal", "SELECT ...", r)
    assert section.rows == [["901", "Rp 1.500.000.000", "2026-01-15", None]]
    assert section.raw_rows == [[901, "1500000000.00", "2026-01-15", None]]
    assert section.row_count == 1 and not section.truncated


def test_only_money_columns_get_rupiah():
    """AVG over an ID is a Decimal but not an amount."""
    r = QueryResult(columns=["AvgID", "Debit"], rows=[(Decimal("5.500000"), Decimal("1500000000.00"))],
                    truncated=False, money_columns=frozenset({"debit"}))
    assert build_section("x", "SELECT", r).rows == [["5,5", "Rp 1.500.000.000"]]


Q1 = "Berapa sisa tunggakan billing Siti Rahmawati untuk periode bulan 10 tahun 2026?"
TUNGGAKAN = [("Tunggakan", result(["TotalTunggakan"], [(Decimal("10500000.00"),)]))]


def test_template_is_filled_from_data():
    answer = render_answer("Sisa tunggakan periode 10/2026 adalah {q1.TotalTunggakan}.", Q1, TUNGGAKAN)
    assert answer == "Sisa tunggakan periode 10/2026 adalah Rp 10.500.000."


def test_sum_and_row_count_placeholders():
    r = [("Kontrak", result(["ContractID", "SalesAmount"],
                            [(102, Decimal("1250000000")), (107, Decimal("1450000000"))]))]
    answer = render_answer("{q1.row_count} kontrak, total {q1.sum.SalesAmount}.", "kontrak Budi", r)
    assert answer == "2 kontrak, total Rp 2.700.000.000."


@pytest.mark.parametrize(
    "template",
    [
        "Sisa tunggakan adalah Rp 99.000.000.",  # invented amount
        "Total {q1.Missing}.",  # unknown column
        "Total {q4.TotalTunggakan}.",  # unknown query index
        "Total {TotalTunggakan}.",  # wrong placeholder syntax
        "Total {q1.TotalTunggakan!r}.",
        # numbers from the question must not become amounts
        "Sisa tunggakan Siti Rahmawati adalah Rp 10.",
        "Sisa tunggakan Siti Rahmawati adalah 10.",
        "Sisa tunggakan sekitar 10 juta, yaitu {q1.TotalTunggakan}.",
        "Tunggakan naik 10% menjadi {q1.TotalTunggakan}.",
    ],
)
def test_unusable_templates_fall_back_to_deterministic_summary(template):
    assert render_answer(template, Q1, TUNGGAKAN) == "Tunggakan: Rp 10.500.000."


@pytest.mark.parametrize(
    "text, ok",
    [
        ("Tunggakan Billing Siti Rahmawati Oktober 2026", True),
        ("Detail Invoice", True),
        ("Ada sepuluh kontrak", False),  # numbers written as words
        ("Total Rp 99 juta", False),
        ("Twelve invoices overdue", False),
        ("Unit SM 1201 tidak ditemukan", True),
        ("Jumlah 7 invoice", False),
    ],
)
def test_model_text_provenance(text, ok):
    from southcity_assistant.rendering import model_text_ok

    assert model_text_ok(text, "Berapa tunggakan Siti Rahmawati bulan 10 tahun 2026 unit SM 1201?") is ok


def test_question_numbers_allowed_as_periods_units_and_years():
    assert render_answer("Periode bulan 10 tahun 2026: {q1.TotalTunggakan}.", Q1, TUNGGAKAN) == \
        "Periode bulan 10 tahun 2026: Rp 10.500.000."
    unit = [("Unit", result(["Status"], [("Vacant",)]))]
    assert render_answer("Unit SM 1201 berstatus {q1.Status}.", "riwayat unit SM 1201", unit) == \
        "Unit SM 1201 berstatus Vacant."


def test_empty_results_are_explicit():
    r = [("Unit", result(["UnitNumber", "Status"], [("SM 1201", "Vacant")])),
         ("Kontrak", result(["ContractID"], [])),
         ("Tagihan", result(["InvoiceID"], [(1,)] * 3))]
    answer = render_answer("Kontrak terakhir {q2.ContractID}.", "unit SM 1201", r)  # q2 has no rows
    assert answer == "Unit: 1 baris ditemukan. Kontrak: tidak ada data. Tagihan: 3 baris ditemukan."


def test_question_month_cannot_become_a_count():
    """The question's month must not become a count in the answer."""
    question = "Berapa total kontrak Budi Santoso per bulan 10 tahun 2026?"
    r = [("Kontrak", result(["Jumlah", "SalesAmount"], [(2, Decimal("2700000000"))]))]
    answer = render_answer("Jumlah kontrak 10; nilai total {q1.SalesAmount}.", question, r)
    assert "10;" not in answer and answer == "Kontrak: 1 baris ditemukan."


def test_fully_truncated_section_is_not_called_empty():
    """Rows existed but none fit the byte budget."""
    r = [("Unit", result(["Label"], [], truncated=True))]
    assert render_answer("", "daftar unit", r) == \
        "Unit: ada data, tetapi melebihi batas ukuran output sehingga tidak ditampilkan."


def test_totals_over_truncated_rows_are_never_stated():
    """A sum/count over a cut-off list must not read as the full total."""
    r = [("Kontrak", result(["ContractID", "SalesAmount"], [(102, Decimal("1250000000"))], truncated=True))]
    answer = render_answer("Total {q1.sum.SalesAmount} dari {q1.row_count} kontrak.", "kontrak Budi", r)
    assert answer == "Kontrak: menampilkan 1 baris pertama (hasil dipotong sesuai batas)."
