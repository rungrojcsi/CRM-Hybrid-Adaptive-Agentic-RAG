"""Tests for quant_provenance — H3 guardrails (provenance / disambiguation /
plausibility). DAX strings are real samples captured from the deployed pipeline.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from transform.quant_provenance import build_advisory, format_advisory

# Real DAX: "top 10 lost opportunities by highest possibility"
DAX_LOST = (
    'EVALUATE TOPN(10, SELECTCOLUMNS(FILTER(Fact_Opportunity, '
    'Fact_Opportunity[Status] = "Lost"), "Opportunity", Fact_Opportunity[Opportunity Name], '
    '"Highest Possibility", Fact_Opportunity[Possibility], '
    '"Amount", Fact_Opportunity[SO Plan Amount]), [Highest Possibility], DESC)'
)
# SALES DATA MODEL: top salesperson by certified actual (the headline metric)
DAX_REV_PERSON = (
    'EVALUATE TOPN(1, SUMMARIZECOLUMNS(Dim_SalesPerson[Full Name], '
    'TREATAS({YEAR(TODAY())}, Dim_Date[Year]), '
    '"Actual", [Total SO Actual Amount (P) by Person], "Target", [Total Target Amount]), '
    '[Actual], DESC)'
)
# Raw SO Actual (anti-pattern) — should trigger the prefer-certified warning
DAX_RAW_ACTUAL = (
    'EVALUATE ROW("v", CALCULATE(SUM(Fact_Opportunity[SO Actual Amount]), '
    'Fact_Opportunity[Status]="Won"))'
)
# Real DAX: win rate with a ratio column
DAX_WINRATE = (
    'EVALUATE TOPN(1, FILTER(SUMMARIZECOLUMNS(Fact_Opportunity[Account ID], '
    '"Won_Count", CALCULATE(COUNTROWS(Fact_Opportunity), Fact_Opportunity[IsWon]=1), '
    '"Win_Rate", 0.5), TRUE()), [Win_Rate], DESC)'
)


class TestProvenance:
    def test_lost_possibility_concepts_and_filter(self):
        adv = build_advisory("top 10 lost deals by possibility", DAX_LOST, [{"x": 1}])
        joined = " ".join(adv["provenance"])
        assert "ยอดแผน SO" in joined          # SO Plan Amount
        assert "ความเป็นไปได้" in joined       # Possibility
        assert "Status = Lost" in adv["filters"]

    def test_so_actual_not_double_reported_as_plan(self):
        dax = 'EVALUATE ROW("a", SUM(Fact_Opportunity[SO Actual Amount]))'
        adv = build_advisory("ยอดขาย", dax, [{"a": 1}])
        joined = " ".join(adv["provenance"])
        assert "ยอดขายจริง" in joined
        assert "ยอดแผน SO" not in joined       # must not also match "SO ... Amount"

    def test_filter_person_and_year(self):
        dax = ('EVALUATE ROW("r", CALCULATE(SUM(Fact_Opportunity[SO Actual Amount]), '
               'SEARCH("Alex", Fact_Opportunity[Sale Person Name],1,0)>0, '
               'YEAR(Fact_Opportunity[Closed Date])=2025))')
        adv = build_advisory("Alex sales 2025", dax, [{"r": 1}])
        assert any("Alex" in f for f in adv["filters"])
        assert "Year = 2025" in adv["filters"]


class TestDisambiguation:
    def test_ambiguous_revenue_term_flagged(self):
        adv = build_advisory("top salesperson by sales", DAX_REV_PERSON, [{"Actual": 1}])
        assert adv["disambiguation"] is not None
        assert "ยอดขายจริง" in adv["disambiguation"]

    def test_non_money_question_no_disambiguation(self):
        adv = build_advisory("how many lost deals", DAX_LOST, [{"x": 1}])
        assert adv["disambiguation"] is None  # "possibility/count", no money term

    def test_salesperson_word_does_not_trigger(self):
        # "salesperson" contains "sales" but is a role, not a money metric.
        adv = build_advisory("lost opportunities with salesperson", DAX_LOST, [{"x": 1}])
        assert adv["disambiguation"] is None


class TestPlausibility:
    def test_raw_so_actual_warns(self):
        adv = build_advisory("won sales total", DAX_RAW_ACTUAL, [{"v": 1}])
        assert any("certified" in w for w in adv["warnings"])

    def test_certified_actual_no_raw_warning(self):
        adv = build_advisory("sales by person", DAX_REV_PERSON, [{"Actual": 1}])
        assert not any("certified" in w for w in adv["warnings"])

    def test_ratio_over_one_flagged(self):
        adv = build_advisory("win rate", DAX_WINRATE, [{"Win_Rate": 1.8}])
        assert any("Win_Rate" in w for w in adv["warnings"])

    def test_ratio_within_range_ok(self):
        adv = build_advisory("win rate", DAX_WINRATE, [{"Win_Rate": 0.95}])
        assert not any("Win_Rate" in w for w in adv["warnings"])

    def test_so_plan_amount_not_flagged_as_recognized_revenue(self):
        # DAX_LOST uses "SO Plan Amount" + Sale Person — must NOT trigger the
        # recognized-revenue attribution warning (regression: "Plan Amount"
        # substring-matched "SO Plan Amount").
        dax = DAX_LOST.replace("Opportunity Name", "Sale Person Name")
        adv = build_advisory("lost deals by person", dax, [{"x": 1}])
        assert not any("attribute" in w for w in adv["warnings"])


class TestStateOverTime:
    def test_pipeline_wow_flagged(self):
        adv = build_advisory("how did pipeline value change week over week", DAX_LOST, [{"x": 1}])
        assert any("state-over-time" in w for w in adv["warnings"])

    def test_possibility_dropped_flagged(self):
        adv = build_advisory("which opportunities dropped in possibility over the last month",
                             DAX_LOST, [{"x": 1}])
        assert any("state-over-time" in w for w in adv["warnings"])

    def test_status_transition_flagged(self):
        adv = build_advisory("how many deals changed from Open to Lost this quarter",
                             DAX_LOST, [{"x": 1}])
        assert any("state-over-time" in w for w in adv["warnings"])

    def test_revenue_trend_not_flagged(self):
        # Flow measure trended over time IS answerable — must NOT flag.
        adv = build_advisory("monthly revenue trend for 2025", DAX_REV_PERSON, [{"Revenue": 1}])
        assert not any("state-over-time" in w for w in adv["warnings"])

    def test_static_pipeline_snapshot_not_flagged(self):
        # "current pipeline value" — state but no temporal-change cue → OK.
        adv = build_advisory("total open pipeline value now", DAX_LOST, [{"x": 1}])
        assert not any("state-over-time" in w for w in adv["warnings"])


class TestFormat:
    def test_format_includes_sections(self):
        # raw SO Actual + ambiguous "sales" term → all three sections present.
        adv = build_advisory("total sales", DAX_RAW_ACTUAL, [{"v": 1}])
        out = format_advisory(adv)
        assert "📊 ที่มา:" in out   # provenance
        assert "ℹ️" in out          # disambiguation
        assert "⚠️" in out          # raw-actual warning

    def test_no_dax_empty(self):
        assert format_advisory(build_advisory("hi", None, None)) == ""
