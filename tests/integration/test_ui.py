"""The demo chat UI: served by the API itself, locked down with a strict CSP, no HTML injection paths."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from southcity_assistant.app import STATIC_DIR, create_app

from ..conftest import ScriptedPlanner

pytestmark = pytest.mark.integration


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings, planner=ScriptedPlanner()))


def test_root_redirects_to_the_ui(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"


def test_ui_page_and_assets_are_served_with_csp(client):
    page = client.get("/ui/")
    assert page.status_code == 200 and "SouthCity Data Assistant" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    for asset in ("/ui/app.js", "/ui/app.css"):
        assert client.get(asset).status_code == 200


def test_api_docs_are_not_affected_by_the_ui_csp(client):
    assert "content-security-policy" not in client.get("/docs").headers


def test_ui_never_inserts_api_data_as_html():
    script = Path(STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script, sink
    html = Path(STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "style=" not in html  # no inline code, so the CSP can forbid it


def test_ui_lists_all_five_test_questions():
    script = Path(STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for q in ("Siti Rahmawati", "Budi Santoso", "SM 1201", "Kas & Bank", "JournalID 901"):
        assert q in script
