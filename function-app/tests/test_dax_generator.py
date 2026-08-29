"""Tests for dax_generator — validator + account citation post-processor."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from transform.dax_generator import (
    extract_account_citation,
    is_empty_or_null_result,
    validate_dax,
)


class TestValidator:
    def test_forbidden_finance_table(self):
        # SALES DATA MODEL has no finance tables — Fact_Project_Expenses must flag.
        # (Dim_Account is now a VALID table in this model and must NOT flag.)
        dax = "EVALUATE SUMMARIZECOLUMNS(Fact_Project_Expenses[Cost], \"X\", [Total Activity])"
        issues = validate_dax(dax)
        assert any("Fact_Project_Expenses" in i for i in issues)

    def test_dim_account_now_valid(self):
        dax = "EVALUATE TOPN(5, SUMMARIZECOLUMNS(Dim_Account[Account Name], \"A\", [Total Target Amount]), [A], DESC)"
        assert not any("Dim_Account" in i for i in validate_dax(dax))

    def test_forbidden_yearmonth_column(self):
        dax = "EVALUATE SUMMARIZECOLUMNS(Dim_Date[YearMonth], \"X\", [Total Revenue])"
        issues = validate_dax(dax)
        assert any("YearMonth" in i for i in issues)

    def test_iswon_boolean_bug(self):
        dax = "EVALUATE FILTER(Fact_Opportunity, Fact_Opportunity[IsWon] = TRUE)"
        issues = validate_dax(dax)
        assert any("Integer" in i for i in issues)

    def test_iswon_integer_ok(self):
        dax = "EVALUATE FILTER(Fact_Opportunity, Fact_Opportunity[IsWon] = 1)"
        assert validate_dax(dax) == []

    def test_unknown_table(self):
        dax = "EVALUATE SUMMARIZECOLUMNS(Fact_Mystery_Table[X], \"V\", [Total Revenue])"
        issues = validate_dax(dax)
        assert any("Fact_Mystery_Table" in i for i in issues)

    def test_clean_dax_no_issues(self):
        dax = """EVALUATE
        SUMMARIZECOLUMNS(
          Dim_Department[Department_Group],
          "Margin", [Profit Margin %]
        )"""
        assert validate_dax(dax) == []


class TestEmptyResultDetection:
    def test_empty_list(self):
        assert is_empty_or_null_result([]) is True

    def test_all_nulls(self):
        assert is_empty_or_null_result([{"a": None, "b": None}]) is True

    def test_empty_strings(self):
        assert is_empty_or_null_result([{"a": "", "b": ""}]) is True

    def test_has_value(self):
        assert is_empty_or_null_result([{"a": 0, "b": None}]) is False

    def test_has_string(self):
        assert is_empty_or_null_result([{"a": None, "b": "hi"}]) is False


class TestAccountCitation:
    def test_aggregate_only_no_account_column(self):
        rows = [{"Margin": 0.234}]
        c = extract_account_citation(rows)
        assert c["is_aggregate_only"] is True
        assert c["value"] == 0.234
        assert c["measure"] == "Margin"
        assert c["top_accounts"] == []

    def test_per_account_breakdown(self):
        rows = [
            {"Account ID": "A001", "Won_Amount": 500_000},
            {"Account ID": "A002", "Won_Amount": 300_000},
            {"Account ID": "A003", "Won_Amount": 100_000},
        ]
        c = extract_account_citation(rows)
        assert c["is_aggregate_only"] is False
        assert c["measure"] == "Won_Amount"
        assert len(c["top_accounts"]) == 3
        # Short non-GUID IDs are not resolved → name = raw, account_id = None
        assert c["top_accounts"][0]["name"] == "A001"
        assert c["top_accounts"][0]["account_id"] is None
        assert c["top_accounts"][0]["value"] == 500_000

    def test_guid_unresolved_falls_back_to_id(self):
        # Looks like a GUID but not in lookup → name = GUID, account_id = None
        rows = [{"Account ID": "DEADBEEF-DEAD-BEEF-DEAD-BEEFDEADBEEF", "Revenue": 1_000_000}]
        c = extract_account_citation(rows)
        assert c["top_accounts"][0]["name"] == "DEADBEEF-DEAD-BEEF-DEAD-BEEFDEADBEEF"
        assert c["top_accounts"][0]["account_id"] is None

    def test_top_n_caps_to_5(self):
        rows = [{"Account ID": f"A{i}", "Sales": 100 - i} for i in range(10)]
        c = extract_account_citation(rows, top_n=5)
        assert len(c["top_accounts"]) == 5
        assert c["top_accounts"][0]["name"] == "A0"

    def test_sale_person_treated_as_account(self):
        rows = [
            {"Sale Person Name": "Salesperson1", "Won_Amount": 1_000},
            {"Sale Person Name": "Salesperson2",   "Won_Amount":   500},
        ]
        c = extract_account_citation(rows)
        assert c["is_aggregate_only"] is False
        assert c["top_accounts"][0]["name"] == "Salesperson1"

    def test_empty_rows(self):
        c = extract_account_citation([])
        assert c["is_aggregate_only"] is True
        assert c["top_accounts"] == []
        assert c["value"] is None

    def test_filter_subject_detected_per_sales_person(self):
        rows = [{"Dim_Date[Month Year]": "Jan 2026", "[Revenue]": 1000}]
        c = extract_account_citation(rows, question="Salesperson1 ยอดขายเดือนต่อเดือนปีนี้")
        assert c["is_aggregate_only"] is True
        assert c["filter_subject"] is not None
        assert "salesperson1" in c["filter_subject"].lower()

    def test_no_filter_subject_for_pure_aggregate(self):
        rows = [{"[Total]": 1234567}]
        c = extract_account_citation(rows, question="ยอดขายรวมทั้งบริษัท")
        assert c["filter_subject"] is None


class TestSystemPromptTemplate:
    """Guard against `.format()` KeyError from un-escaped {…} in SYSTEM_PROMPT rules."""

    def test_system_prompt_renders(self):
        from transform.dax_generator import _build_system_prompt
        p = _build_system_prompt()
        assert len(p) > 1000
        # YEAR(TODAY()) braces must round-trip into the final text, not be eaten by format()
        assert "TREATAS({YEAR(TODAY())}" in p
