"""Streaming endpoint: same pipeline as /api/v1/ask, progress as Server-Sent Events."""

import json

import pytest
from fastapi.testclient import TestClient

from southcity_assistant.app import create_app

from ..conftest import ScriptedPlanner
from .test_questions import PLAN_Q1, Q1, Q5

pytestmark = pytest.mark.integration


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def stream(settings, question, role="Sales_Staff", *plans, **overrides):
    client = TestClient(create_app(settings.model_copy(update=overrides), planner=ScriptedPlanner(*plans)))
    return client.post("/api/v1/ask/stream", json={"question": question}, headers={"X-User-Role": role})


def test_q1_streams_stages_then_sql_then_result(settings):
    r = stream(settings, Q1, "Sales_Staff", PLAN_Q1)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = events(r.text)
    stages = [d["id"] for e, d in evs if e == "stage"]
    assert stages == ["guard", "scope", "entities", "planning", "policy", "checking", "executing"]
    kinds = [e for e, _ in evs]
    assert kinds.index("sql") < kinds.index("result") and kinds[-1] == "result"
    sql = [d for e, d in evs if e == "sql"]
    assert [d["index"] for d in sql] == [0, 1] and all(d["sql"].startswith("SELECT TOP") for d in sql)
    result = evs[-1][1]
    assert result["status"] == 200 and "Rp 10.500.000" in result["response"]["answer"]


def test_refusal_stops_at_the_guard(settings):
    evs = events(stream(settings, Q5).text)
    assert [d["id"] for e, d in evs if e == "stage"] == ["guard"]
    assert evs[-1][1]["status"] == 403 and evs[-1][1]["response"]["code"] == "write_request"


def test_admission_errors_are_plain_json(settings):
    client = TestClient(create_app(settings, planner=ScriptedPlanner()))
    r = client.post("/api/v1/ask/stream", json={"question": Q1}, headers={"X-User-Role": "Admin"})
    assert r.status_code == 403 and r.json()["code"] == "unknown_role"


def test_stream_releases_its_concurrency_slot(settings):
    client = TestClient(create_app(settings.model_copy(update={"max_concurrent_requests": 1}),
                                   planner=ScriptedPlanner()))
    for _ in range(3):  # would get 503 'busy' if a finished stream kept its slot
        r = client.post("/api/v1/ask/stream", json={"question": Q5}, headers={"X-User-Role": "Management"})
        assert r.status_code == 200 and events(r.text)[-1][1]["status"] == 403


def test_trusted_proxy_header_identifies_the_client(settings):
    client = TestClient(create_app(settings.model_copy(update={"trust_proxy": True, "rate_limit_per_minute": 1}),
                                   planner=ScriptedPlanner()))
    h = {"X-User-Role": "Management"}
    first = client.post("/api/v1/ask", json={"question": Q5}, headers={**h, "X-Real-IP": "203.0.113.1"})
    other = client.post("/api/v1/ask", json={"question": Q5}, headers={**h, "X-Real-IP": "203.0.113.2"})
    again = client.post("/api/v1/ask", json={"question": Q5}, headers={**h, "X-Real-IP": "203.0.113.1"})
    assert (first.status_code, other.status_code, again.status_code) == (403, 403, 429)
