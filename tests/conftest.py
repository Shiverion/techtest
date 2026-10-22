from __future__ import annotations

import json

import pytest

from southcity_assistant.contracts import QueryPlan
from southcity_assistant.entities import EntityHints
from southcity_assistant.security.roles import Role


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="run tests that call the real LLM")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live LLM test; pass --live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


class ScriptedPlanner:
    """Stands in for the model only. It returns fixed plans; the SQL in them really runs on SQL Server."""

    def __init__(self, *plans: dict | QueryPlan):
        self.plans = [p if isinstance(p, QueryPlan) else QueryPlan.model_validate(p) for p in plans]
        self.calls: list[dict] = []

    def plan(self, question: str, role: Role, hints: EntityHints, repair: str | None = None,
             timeout: float | None = None) -> QueryPlan:
        self.calls.append({"question": question, "role": role, "hints": hints, "repair": repair, "timeout": timeout})
        if not self.plans:
            raise AssertionError("planner called more times than scripted")
        return self.plans.pop(0)


def query_plan(*queries: tuple[str, str], template: str = "") -> dict:
    return {"status": "query", "queries": [{"title": t, "sql": s} for t, s in queries],
            "answer_template": template, "reason": ""}


@pytest.fixture
def scripted():
    return ScriptedPlanner


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
