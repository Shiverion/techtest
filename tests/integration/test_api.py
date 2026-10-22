"""HTTP contract: role handling, status codes, response shape, no leaks."""

import pytest
from fastapi.testclient import TestClient

from southcity_assistant.app import create_app

from ..conftest import ScriptedPlanner, query_plan
from .test_questions import PLAN_Q1, PLAN_Q4, Q1, Q4, Q5

pytestmark = pytest.mark.integration


def client(settings, *plans, **overrides):
    planner = ScriptedPlanner(*plans)
    app = create_app(settings.model_copy(update=overrides), planner=planner)
    return TestClient(app), planner


def test_q1_over_http_with_header_role(settings):
    c, _ = client(settings, PLAN_Q1)
    r = c.post("/api/v1/ask", json={"question": Q1}, headers={"X-User-Role": "Sales_Staff"})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok" and body["role"] == "Sales_Staff"
    assert "Rp 10.500.000" in body["answer"]
    assert r.headers["X-Request-ID"] == body["request_id"]
    section = body["sections"][0]
    assert set(section) == {"title", "sql", "columns", "rows", "raw_rows", "row_count", "truncated"}


def test_q4_by_role_over_http(settings):
    c, _ = client(settings, PLAN_Q4)
    denied = c.post("/api/v1/ask", json={"question": Q4, "user_role": "Sales_Staff"})
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden_data"
    allowed = c.post("/api/v1/ask", json={"question": Q4, "user_role": "Management"})
    assert allowed.status_code == 200 and allowed.json()["sections"][0]["raw_rows"][0][0] == 901


@pytest.mark.parametrize("role", ["Sales_Staff", "Management"])
def test_q5_over_http(settings, role):
    c, planner = client(settings)
    r = c.post("/api/v1/ask", json={"question": Q5}, headers={"X-User-Role": role})
    assert r.status_code == 403 and r.json()["code"] == "write_request" and planner.calls == []


@pytest.mark.parametrize(
    "headers, body, status, code",
    [
        ({}, {"question": Q1}, 400, "missing_role"),
        ({"X-User-Role": "Admin"}, {"question": Q1}, 403, "unknown_role"),
        ({"X-User-Role": "Sales_Staff"}, {"question": Q1, "user_role": "Management"}, 400, "conflicting_role"),
    ],
)
def test_role_errors(settings, headers, body, status, code):
    c, planner = client(settings)
    r = c.post("/api/v1/ask", json=body, headers=headers)
    assert (r.status_code, r.json()["code"]) == (status, code) and planner.calls == []


def test_api_key_mode_prevents_role_escalation(settings):
    c, _ = client(settings, PLAN_Q1, auth_mode="api_key", api_keys={"k-sales": "Sales_Staff"})
    r = c.post("/api/v1/ask", json={"question": Q1, "user_role": "Management"}, headers={"X-API-Key": "k-sales"})
    assert r.status_code == 403 and r.json()["code"] == "role_not_permitted"
    assert c.post("/api/v1/ask", json={"question": Q1}).status_code == 401
    ok = c.post("/api/v1/ask", json={"question": Q1}, headers={"X-API-Key": "k-sales"})
    assert ok.status_code == 200 and ok.json()["role"] == "Sales_Staff"


def test_request_validation(settings):
    c, _ = client(settings)
    h = {"X-User-Role": "Management"}
    assert c.post("/api/v1/ask", json={}, headers=h).status_code == 422
    assert c.post("/api/v1/ask", json={"question": "x", "sql": "DROP TABLE Units"}, headers=h).status_code == 422
    assert c.post("/api/v1/ask", json={"question": "a" * 4001}, headers=h).status_code == 422


def test_rate_limit(settings):
    c, _ = client(settings, rate_limit_per_minute=2)
    h = {"X-User-Role": "Management"}
    codes = [c.post("/api/v1/ask", json={"question": Q5}, headers=h).status_code for _ in range(3)]
    assert codes == [403, 403, 429]


def test_demo_mode_rate_limit_ignores_unauthenticated_api_keys(settings):
    """Rotating an X-API-Key header must not reset the window in demo mode."""
    c, _ = client(settings, rate_limit_per_minute=1)
    h = {"X-User-Role": "Management"}
    first = c.post("/api/v1/ask", json={"question": Q5}, headers={**h, "X-API-Key": "a1"})
    second = c.post("/api/v1/ask", json={"question": Q5}, headers={**h, "X-API-Key": "a2"})
    assert (first.status_code, second.status_code) == (403, 429)


def test_llm_not_configured_is_a_clean_503(settings):
    from pydantic import SecretStr

    app = create_app(settings.model_copy(update={"llm_api_key": SecretStr("")}))
    r = TestClient(app).post("/api/v1/ask", json={"question": Q1}, headers={"X-User-Role": "Management"})
    assert r.status_code == 503 and r.json()["code"] == "llm_not_configured"


def test_internal_errors_do_not_leak(settings):
    class Exploding:
        def plan(self, *a, **k):
            raise RuntimeError("secret connection string Password=hunter2")

    app = create_app(settings, planner=Exploding())
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/ask", json={"question": Q1}, headers={"X-User-Role": "Management"})
    assert r.status_code == 500 and "hunter2" not in r.text and r.json()["code"] == "internal_error"


def test_responses_never_contain_credentials(settings):
    c, _ = client(settings, query_plan(("x", "SELECT CustName FROM dbo.Units")), query_plan(("x", "SELECT CustName FROM dbo.Units")))
    r = c.post("/api/v1/ask", json={"question": "daftar customer"}, headers={"X-User-Role": "Management"})
    text = r.text
    for secret in (settings.mgmt_db_password.get_secret_value(), settings.sales_db_password.get_secret_value()):
        assert secret not in text
    assert "DB-Lib" not in text and "Traceback" not in text
