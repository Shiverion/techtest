"""Opt-in: `uv run pytest tests/live --live`. Real model + real database, all ten question/role cases."""

import pytest
from fastapi.testclient import TestClient

from southcity_assistant.app import create_app
from southcity_assistant.evaluation import Reference, run_suite
from southcity_assistant.security.roles import Role
from southcity_assistant.settings import Settings

pytestmark = pytest.mark.live


def test_all_questions_both_roles():
    settings = Settings()  # type: ignore[call-arg]
    if not settings.llm_api_key.get_secret_value():
        pytest.skip("LLM_API_KEY not set")
    client = TestClient(create_app(settings.model_copy(update={"rate_limit_per_minute": 10_000})))

    def post(question: str, role: Role):
        r = client.post("/api/v1/ask", json={"question": question}, headers={"X-User-Role": role.value})
        return r.status_code, r.json()

    results = run_suite(post, Reference(settings))
    failures = [f"{r.question}/{r.role}: {r.problems}" for r in results if not r.passed]
    assert not failures, failures
