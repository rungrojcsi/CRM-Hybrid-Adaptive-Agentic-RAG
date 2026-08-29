"""Tests for transform.dax_extractor — the DAX source layer that replaces the
dead CDM-CSV path. Mocks executeQueries; verifies it reproduces the cdm_parser
contract and feeds aggregator + renderer unchanged."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from transform import dax_extractor as dx
from transform.aggregator import aggregate_account_centric
from transform.renderer import render_account_md


# ── unit: header strip + cell normalization ────────────────────────────────
def test_strip_brackets():
    assert dx._strip_brackets("[Account ID]") == "Account ID"
    assert dx._strip_brackets("Account ID") == "Account ID"


def test_norm_cell_types():
    assert dx._norm_cell(None) == ""
    assert dx._norm_cell(True) == "true"
    assert dx._norm_cell(80.0) == "80"        # integral float → no ".0"
    assert dx._norm_cell(80.5) == "80.5"
    assert dx._norm_cell(1234567.0) == "1234567"
    assert dx._norm_cell("x") == "x"


def test_rows_to_df_strips_casts_and_fills_missing():
    cols = {"Account ID": "Account ID", "Company Name": "Account Name", "City": "City"}
    rows = [{"[Account ID]": "ACC-1", "[Company Name]": "Alpha"}]  # City absent
    df = dx._rows_to_df(rows, cols)
    assert list(df.columns) == ["Account ID", "Company Name", "City"]
    assert df.iloc[0]["Account ID"] == "ACC-1"
    assert df.iloc[0]["City"] == ""           # missing key → ""
    assert (df.dtypes == object).all()


def test_rows_to_df_empty():
    cols = {"Account ID": "Account ID", "Company Name": "Account Name"}
    df = dx._rows_to_df([], cols)
    assert df.empty
    assert list(df.columns) == ["Account ID", "Company Name"]


# ── unit: DAX string construction ──────────────────────────────────────────
def test_build_selectcolumns_aliases_to_friendly():
    dax = dx._build_selectcolumns("Dim_Account", {"Company Name": "Account Name"})
    assert '"Company Name", Dim_Account[Account Name]' in dax
    assert dax.startswith("SELECTCOLUMNS(")


def test_build_selectcolumns_with_filter():
    dax = dx._build_selectcolumns(
        "Fact_Activity", {"Activity ID": "Activity ID"},
        filter_expr="NOT ISBLANK(Fact_Activity[Opportunity ID])",
    )
    assert "FILTER(Fact_Activity, NOT ISBLANK(Fact_Activity[Opportunity ID]))" in dax


# ── unit: paging loop ──────────────────────────────────────────────────────
def test_eval_table_paged_concats_pages_and_stops(monkeypatch):
    spec = {
        "table": "Fact_Activity",
        "columns": {"Activity ID": "Activity ID"},
        "key": "Activity ID",
    }
    page = [{"[Activity ID]": f"A{i}"} for i in range(2)]   # page_size=2 → full page

    calls = {"n": 0}

    def fake_exec(dax, dataset_id=None):
        # skip=0 → full page, skip>=2 → empty (stop)
        calls["n"] += 1
        return page if "TOPNSKIP(2, 0," in dax else []

    monkeypatch.setattr(dx, "execute_dax", fake_exec)
    df = dx._eval_table_paged(spec, None, page_size=2)
    assert len(df) == 2
    assert calls["n"] == 2                       # one full page + one empty


# ── integration: full extract → aggregate → render, incl. derived links ─────
def _mock_model():
    """Return a dispatcher mimicking executeQueries per table.

    One account ACC-001 with: 1 contact, 1 opportunity OPP-001, 1 activity and
    1 note both linked to OPP-001 only (no direct account link) — exercises the
    derivation that re-attaches them to the account.
    """
    by_table = {
        "Dim_Account": [{
            "[Account ID]": "ACC-001", "[Company Name]": "Alpha Manufacturing Co.",
            "[Industry]": "Automotive", "[Account Status]": "Active",
            "[Customer Code]": "CUST-01", "[Customer Type]": "Customer",
            "[City]": "Bangkok", "[Province]": "BKK", "[Country]": "Thailand",
        }],
        "Dim_Contact": [{
            "[Contact ID]": "CON-1", "[Account ID]": "ACC-001",
            "[Contact Name]": "Nattaporn Siri", "[Job Title]": "CTO",
            "[Email]": "n@alpha.co", "[Mobile Phone]": "0810000000",
        }],
        "Fact_Opportunity": [{
            "[Opportunity ID]": "OPP-001", "[Account ID]": "ACC-001",
            "[Opportunity Name]": "Alpha MES Phase 1", "[Status]": "Open",
            "[Possibility]": 80.0, "[Est. Close Date]": "2026-08-31",
            "[Total SO Plan Amount]": 5000000.0, "[Solution Name]": "MES",
            "[Sale Person Name]": "Alex", "[Description]": "MES rollout",
            "[Detail Reasons]": "",
        }],
        "Fact_Activity": [{
            "[Activity ID]": "ACT-1", "[Opportunity ID]": "OPP-001",
            "[Activity Date]": "2026-06-20", "[Subject]": "Discovery Call",
            "[Activity Type]": "Phone Call", "[State Name]": "Completed",
        }],
        "Dim_Connection": [{
            "[Opportunity ID]": "OPP-001", "[Stakeholder Role Name]": "Decision Maker",
        }],
        "Dim_Review": [],
        "Dim_OpportunityClose": [],
        "Dim_Annotation": [{
            "[Note ID]": "NOTE-1", "[Note Title]": "Meeting note",
            "[Note Body]": "Customer keen on phase 2", "[Created Date]": "2026-06-21",
            "[Opportunity ID]": "OPP-001",
        }],
    }

    def dispatch(dax, dataset_id=None):
        for table, rows in by_table.items():
            # match the table named right after SELECTCOLUMNS( / FILTER(
            if f"SELECTCOLUMNS(\n  {table}," in dax or f"FILTER({table}," in dax:
                return rows
        raise AssertionError(f"unexpected table in DAX: {dax[:120]}")

    return dispatch


def test_extract_entities_returns_full_contract(monkeypatch):
    monkeypatch.setattr(dx, "execute_dax", _mock_model())
    entities = dx.extract_entities_dax()
    # all 8 aggregator entities present
    assert set(entities) == set(dx.ENTITY_SPECS)
    acc = entities["Dim_Account"]
    assert acc.iloc[0]["Company Name"] == "Alpha Manufacturing Co."
    # integral floats normalized
    assert entities["Fact_Opportunity"].iloc[0]["Possibility"] == "80"
    assert entities["Fact_Opportunity"].iloc[0]["Total SO Plan Amount"] == "5000000"


def test_derived_account_links(monkeypatch):
    monkeypatch.setattr(dx, "execute_dax", _mock_model())
    entities = dx.extract_entities_dax()
    # activity got Regarding ID derived from its opportunity's account
    assert entities["Fact_Activity"].iloc[0]["Regarding ID"] == "ACC-001"
    # annotation got Ref Object ID + Object Type Code for account attachment
    ann = entities["Dim_Annotation"].iloc[0]
    assert ann["Ref Object ID"] == "ACC-001"
    assert ann["Object Type Code"] == "1"


def test_extract_feeds_aggregator_and_renderer(monkeypatch):
    monkeypatch.setattr(dx, "execute_dax", _mock_model())
    entities = dx.extract_entities_dax()
    records = aggregate_account_centric(entities)
    assert len(records) == 1
    rec = records[0]
    assert rec["account"]["Account ID"] == "ACC-001"
    assert len(rec["contacts"]) == 1
    assert len(rec["opportunities"]) == 1
    assert rec["opportunities"][0]["_connections"][0]["Stakeholder Role Name"] == "Decision Maker"
    # derived: activity + note re-attached to the account via opportunity
    assert len(rec["activities"]) == 1
    assert len(rec["notes"]) == 1

    md = render_account_md(rec)
    assert "# Alpha Manufacturing Co." in md
    assert "Nattaporn Siri" in md
    assert "Alpha MES Phase 1" in md
    assert "Discovery Call" in md
    assert "Decision Maker" in md
    assert "Customer keen on phase 2" in md
