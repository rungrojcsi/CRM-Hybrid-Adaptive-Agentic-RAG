"""Tests for the cross-cutting lenses (solution / salesperson / industry):
correct re-grouping of account records + markdown render via the new templates."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from transform.lenses import (
    slugify,
    aggregate_by_solution,
    aggregate_by_salesperson,
    aggregate_by_industry,
)
from transform.renderer import _build_env, render_lens_md


# Two accounts; shared solution "MES"; two salespersons; two industries.
RECORDS = [
    {
        "account": {"Account ID": "ACC-1", "Company Name": "Alpha Co.",
                    "Industry": "E. (Manufacturing)", "Customer Type": "Customer",
                    "Account Status": "Active", "City": "Bangkok", "Province": "BKK"},
        "contacts": [],
        "opportunities": [{
            "Opportunity ID": "OPP-1", "Opportunity Name": "Alpha MES",
            "Status": "Open", "Possibility": "80", "Est. Close Date": "2026-08-31",
            "Solution Name": "MES", "Sale Person Name": "Alex",
            "Description": "MES rollout", "Detail Reasons": "",
        }],
        "activities": [
            {"Opportunity ID": "OPP-1", "Activity Date": "2026-06-20",
             "Subject": "Call A", "Activity Type": "Phone Call", "State Name": "Completed"},
            {"Opportunity ID": "OPP-1", "Activity Date": "2026-06-22",
             "Subject": "Call B", "Activity Type": "Phone Call", "State Name": "Completed"},
        ],
        "notes": [],
    },
    {
        "account": {"Account ID": "ACC-2", "Company Name": "Beta Ltd.",
                    "Industry": "I. (Retail)", "Customer Type": "Prospect",
                    "Account Status": "Active", "City": "Chonburi", "Province": "CBI"},
        "contacts": [],
        "opportunities": [{
            "Opportunity ID": "OPP-2", "Opportunity Name": "Beta MES",
            "Status": "Won", "Possibility": "100", "Est. Close Date": "2026-05-01",
            "Solution Name": "MES", "Sale Person Name": "Beth",
            "Description": "", "Detail Reasons": "budget approved",
        }],
        "activities": [],
        "notes": [],
    },
]


def test_slugify():
    assert slugify("MES Solution") == "mes-solution"
    assert slugify("E. (Manufacturing)/製造業").startswith("e-manufacturing")
    assert slugify("") == "unknown"


def test_by_solution_groups_both_accounts():
    out = aggregate_by_solution(RECORDS)
    assert len(out) == 1
    mes = out[0]
    assert mes["solution"] == "MES"
    assert mes["deal_count"] == 2
    assert mes["account_count"] == 2
    names = {d["account_name"] for d in mes["deals"]}
    assert names == {"Alpha Co.", "Beta Ltd."}


def test_by_salesperson_splits_and_attaches_activities():
    out = {r["salesperson"]: r for r in aggregate_by_salesperson(RECORDS)}
    assert set(out) == {"Alex", "Beth"}
    assert out["Alex"]["deal_count"] == 1
    assert out["Alex"]["account_count"] == 1
    # activities on Alex's opportunity, most-recent first
    acts = out["Alex"]["activities"]
    assert [a["subject"] for a in acts] == ["Call B", "Call A"]
    assert out["Beth"]["activities"] == []  # no activities


def test_by_industry_groups_accounts():
    out = {r["industry"]: r for r in aggregate_by_industry(RECORDS)}
    assert set(out) == {"E. (Manufacturing)", "I. (Retail)"}
    mfg = out["E. (Manufacturing)"]
    assert mfg["account_count"] == 1
    assert mfg["accounts"][0]["account_name"] == "Alpha Co."
    assert mfg["accounts"][0]["solutions"] == ["MES"]


def _render(template_name, rec):
    tmpl = _build_env().get_template(template_name)
    return render_lens_md(rec, tmpl)


def test_render_solution_md():
    md = _render("solution.md.j2", aggregate_by_solution(RECORDS)[0])
    assert "# Solution: MES" in md
    assert "entity: solution" in md
    assert "Alpha Co." in md and "Beta Ltd." in md
    assert "MES rollout" in md


def test_render_salesperson_md():
    alex = next(r for r in aggregate_by_salesperson(RECORDS) if r["salesperson"] == "Alex")
    md = _render("salesperson.md.j2", alex)
    assert "# Salesperson: Alex" in md
    assert "Alpha MES" in md
    assert "Call A" in md and "Call B" in md


def test_render_industry_md():
    mfg = next(r for r in aggregate_by_industry(RECORDS) if r["industry"] == "E. (Manufacturing)")
    md = _render("industry.md.j2", mfg)
    assert "# Industry: E. (Manufacturing)" in md
    assert "Alpha Co." in md
    assert "Solutions: MES" in md


def test_render_industry_one_account_per_line():
    """Regression: trim_blocks must not collapse account bullets onto one line."""
    records = [
        {"account": {"Account ID": f"A{i}", "Company Name": f"Co {i}",
                     "Industry": "X", "Customer Type": "Customer",
                     "Account Status": "Active", "City": "", "Province": ""},
         "contacts": [], "opportunities": [], "activities": [], "notes": []}
        for i in range(3)
    ]
    md = _render("industry.md.j2", aggregate_by_industry(records)[0])
    bullets = [ln for ln in md.splitlines() if ln.startswith("- **Co ")]
    assert len(bullets) == 3            # each account on its own line
