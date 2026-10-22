import pytest

from southcity_assistant.schema import schema_prompt
from southcity_assistant.security.roles import (
    ROLE_TABLES,
    Role,
    RoleError,
    resolve_role,
    restricted_topic,
)


def demo(header=None, body=None):
    return resolve_role(header_role=header, body_role=body, api_key=None, auth_mode="demo", api_keys={})


def test_demo_mode_accepts_header_or_body():
    assert demo(header="Sales_Staff") is Role.SALES_STAFF
    assert demo(body="Management") is Role.MANAGEMENT
    assert demo(header="Management", body="Management") is Role.MANAGEMENT


@pytest.mark.parametrize(
    "header, body, status, code",
    [
        (None, None, 400, "missing_role"),
        ("Admin", None, 403, "unknown_role"),
        ("management", None, 403, "unknown_role"),  # exact role names only
        ("Sales_Staff", "Management", 400, "conflicting_role"),
        ("", None, 403, "unknown_role"),
    ],
)
def test_demo_mode_rejects_bad_roles(header, body, status, code):
    with pytest.raises(RoleError) as info:
        demo(header, body)
    assert (info.value.status, info.value.code) == (status, code)


KEYS = {"sales-key": "Sales_Staff", "mgmt-key": "Management"}


def keyed(api_key, header=None):
    return resolve_role(header_role=header, body_role=None, api_key=api_key, auth_mode="api_key", api_keys=KEYS)


def test_api_key_mode_binds_role_to_key():
    assert keyed("sales-key") is Role.SALES_STAFF
    assert keyed("mgmt-key", header="Management") is Role.MANAGEMENT


def test_api_key_mode_rejects_escalation_and_bad_keys():
    with pytest.raises(RoleError) as info:
        keyed("sales-key", header="Management")
    assert info.value.code == "role_not_permitted"
    for key, code in [(None, "missing_api_key"), ("nope", "invalid_api_key")]:
        with pytest.raises(RoleError) as info:
            keyed(key)
        assert info.value.code == code


def test_role_table_allowlists():
    assert "GL_Journals" not in ROLE_TABLES[Role.SALES_STAFF]
    assert ROLE_TABLES[Role.MANAGEMENT] == {"Units", "Sales_Contracts", "Invoices_Billing", "GL_Journals"}


def test_sales_prompt_never_mentions_gl_journals():
    sales = schema_prompt(ROLE_TABLES[Role.SALES_STAFF])
    assert "GL_Journals" not in sales and "Kas & Bank" not in sales
    assert "GL_Journals" in schema_prompt(ROLE_TABLES[Role.MANAGEMENT])


@pytest.mark.parametrize(
    "question",
    [
        "Tampilkan daftar catatan transaksi jurnal akun Kas & Bank pada tabel GL_Journals di bulan Januari 2026.",
        "berapa saldo kas dan bank?",
        "show the general ledger",
        "isi buku besar bulan ini",
    ],
)
def test_sales_staff_ledger_topics_are_restricted(question):
    assert restricted_topic(Role.SALES_STAFF, question)
    assert restricted_topic(Role.MANAGEMENT, question) is None


def test_ordinary_sales_questions_are_not_restricted():
    assert restricted_topic(Role.SALES_STAFF, "Berapa total nilai kontrak sales Budi Santoso?") is None
    assert restricted_topic(Role.SALES_STAFF, "kasus tunggakan Siti Rahmawati") is None
