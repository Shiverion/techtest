import json

import pytest

from southcity_assistant.entities import EntityHints, candidates
from southcity_assistant.llm import LLMError, build_system_prompt, parse_plan
from southcity_assistant.security.roles import Role


def plan_json(**overrides):
    base = {"status": "query", "queries": [{"title": "T", "sql": "SELECT 1 FROM Units"}],
            "answer_template": "", "reason": ""}
    base.update(overrides)
    return json.dumps(base)


def test_valid_plan_parses_including_code_fences():
    assert parse_plan(plan_json()).status == "query"
    assert parse_plan("```json\n" + plan_json() + "\n```").queries[0].title == "T"


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        plan_json(status="execute"),
        plan_json(queries=[]),  # query without SQL
        plan_json(status="refuse"),  # refusal carrying queries
        plan_json(extra_field="x"),  # fields outside the contract
        plan_json(queries=[{"title": "T", "sql": "SELECT 1", "tables": ["Units"]}]),  # model-declared tables
        plan_json(queries=[{"title": "T", "sql": "S" * 5000}]),
        plan_json(queries=[{"title": "T", "sql": "SELECT 1 FROM Units"}] * 4),
    ],
)
def test_invalid_model_output_fails_closed(content):
    with pytest.raises(LLMError) as info:
        parse_plan(content)
    assert info.value.code == "llm_invalid_output"


def test_refusal_plan_parses():
    plan = parse_plan(json.dumps({"status": "refuse", "reason": "write request"}))
    assert plan.status == "refuse" and plan.queries == []


def test_prompt_is_role_scoped_and_carries_entities():
    hints = EntityHints(matches={"customer": ["Siti Rahmawati"]}, missing_units=["SM 9999"])
    sales = build_system_prompt(Role.SALES_STAFF, hints)
    assert "GL_Journals" not in sales
    assert "'Siti Rahmawati'" in sales and "'SM 9999' does NOT exist" in sales
    assert "Sales_Staff" in sales
    assert "GL_Journals" in build_system_prompt(Role.MANAGEMENT, EntityHints())


def test_entity_candidates_cover_names_units_and_accounts():
    c = candidates("Tampilkan jurnal akun Kas & Bank dan unit SM1201 milik siti rahmawati")
    folded = {x.casefold() for x in c}
    assert {"kas & bank", "sm 1201", "siti rahmawati"} <= folded
