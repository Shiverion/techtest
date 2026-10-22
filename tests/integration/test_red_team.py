"""The red-team catalog (offline part) runs with the suite, so a regression in any layer fails CI."""

import pytest

from scripts.red_team import CATALOG, run

pytestmark = pytest.mark.integration


def test_offline_red_team_catalog(settings):
    results, integrity = run(settings, live=False)
    failures = [f"{r.attack['id']} {r.attack['category']}: {r.stage}; {r.detail}" for r in results if r.verdict == "FAIL"]
    assert not failures, failures
    assert integrity["unchanged"]
    gaps = {r.attack["id"] for r in results if r.verdict == "GAP"}
    assert gaps == {"24", "27"}  # documented gaps only; a new gap must be reviewed and documented
    assert len(results) == len(CATALOG)


def test_tautology_check_tells_literal_from_logic():
    from scripts.red_team import _tautology_injected

    literal = "SELECT DISTINCT TOP 6 c.CustomerName FROM dbo.Sales_Contracts AS c WHERE CustomerName = N''' OR 1=1 --'"
    logic = "SELECT DISTINCT TOP 6 c.CustomerName FROM dbo.Sales_Contracts AS c WHERE CustomerName = '' OR 1 = 1"
    assert _tautology_injected([(literal, False)]) == []
    assert _tautology_injected([(logic, False)])
    assert _tautology_injected([("SELECT x FROM t WHERE c IN (%s)", False)])


def test_sales_login_metadata_view_hides_the_ledger(settings):
    results, _ = run(settings, live=False)
    metadata = next(r for r in results if r.attack["id"] == "27")
    assert "GL_Journals" not in metadata.detail and "Units" in metadata.detail
