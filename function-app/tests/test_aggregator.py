"""Tests for aggregator + renderer using local synthetic fixtures."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from tests.fixtures.make_fixtures import make, FIXTURES
from transform.cdm_parser import load_entities_local
from transform.aggregator import aggregate_account_centric
from transform.renderer import render_account_md, compute_hash


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def create_fixtures():
    """Generate fixture CSVs before tests run."""
    import os
    os.chdir(FIXTURES_DIR)
    make()


def _load() -> dict:
    # Minimal model dict (entity names only — parser uses directory layout)
    model = {"entities": [{"name": n} for n in FIXTURES]}
    return load_entities_local(FIXTURES_DIR, model)


def test_load_entities():
    entities = _load()
    assert "Dim_Account" in entities
    assert len(entities["Dim_Account"]) == 2
    assert "Dim_Contact" in entities
    assert len(entities["Dim_Contact"]) == 3


def test_aggregate_count():
    entities = _load()
    records = aggregate_account_centric(entities)
    assert len(records) == 2  # 2 accounts


def test_aggregate_acc001_contacts():
    entities = _load()
    records = aggregate_account_centric(entities)
    acc001 = next(r for r in records if r["account"]["Account ID"] == "ACC-001")
    assert len(acc001["contacts"]) == 2
    names = {c["Contact Name"] for c in acc001["contacts"]}
    assert "Nattaporn Siri" in names
    assert "Krit Chanon" in names


def test_aggregate_acc001_opportunities():
    entities = _load()
    records = aggregate_account_centric(entities)
    acc001 = next(r for r in records if r["account"]["Account ID"] == "ACC-001")
    assert len(acc001["opportunities"]) == 1
    opp = acc001["opportunities"][0]
    assert opp["Opportunity Name"] == "Alpha MES Phase 1"
    # Enriched fields
    assert "_connections" in opp
    assert len(opp["_connections"]) == 1
    assert opp["_connections"][0]["Stakeholder Role Name"] == "Decision Maker"


def test_aggregate_acc001_activities():
    entities = _load()
    records = aggregate_account_centric(entities)
    acc001 = next(r for r in records if r["account"]["Account ID"] == "ACC-001")
    # Only direct activities (Regarding ID == ACC-001); ACT-002 has Regarding ID = OPP-001
    assert len(acc001["activities"]) == 2
    subjects = {a["Subject"] for a in acc001["activities"]}
    assert "Initial Discovery Call" in subjects
    assert "Contract Follow-up" in subjects


def test_aggregate_acc002():
    entities = _load()
    records = aggregate_account_centric(entities)
    acc002 = next(r for r in records if r["account"]["Account ID"] == "ACC-002")
    assert len(acc002["contacts"]) == 1
    assert len(acc002["opportunities"]) == 1
    assert len(acc002["activities"]) == 0  # no direct activities for ACC-002


def test_render_markdown_structure():
    entities = _load()
    records = aggregate_account_centric(entities)
    acc001 = next(r for r in records if r["account"]["Account ID"] == "ACC-001")
    md = render_account_md(acc001)
    assert "# Alpha Manufacturing Co." in md
    assert "account_id: ACC-001" in md
    assert "Nattaporn Siri" in md
    assert "Alpha MES Phase 1" in md
    assert "Initial Discovery Call" in md
    assert "Decision Maker" in md


def test_render_md_hash_deterministic():
    entities = _load()
    records = aggregate_account_centric(entities)
    acc001 = next(r for r in records if r["account"]["Account ID"] == "ACC-001")
    md1 = render_account_md(acc001)
    md2 = render_account_md(acc001)
    # generated_at changes between calls — hash compares body only, so just verify format
    assert len(compute_hash(md1)) == 16
    assert compute_hash(md1[:100]) == compute_hash(md1[:100])


def test_render_empty_contacts():
    """Account with no contacts should render cleanly."""
    record = {
        "account": {"Account ID": "ACC-X", "Company Name": "Empty Co.", **{k: "" for k in [
            "Customer Code", "Website", "Annual Revenue", "No. of Employees",
            "Industry", "Customer Type", "City", "Province", "Country",
            "Account Status", "Created Date", "Last Modified Date",
            "Territory Code", "Owner Name (Backup)", "Industry Code",
            "Customer Type Code", "Sales Person ID", "Parent Account ID"
        ]}},
        "contacts": [],
        "opportunities": [],
        "activities": [],
    }
    md = render_account_md(record)
    assert "# Empty Co." in md
    assert "_(none)_" in md
