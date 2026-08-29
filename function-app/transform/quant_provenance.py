"""quant_provenance.py — H3 guardrails for the Quant pipeline.

Three advisory guardrails computed deterministically from a generated DAX query
and its result rows. They NEVER change the numbers — they make the numbers
interpretable so a user can tell a 110M answer from a 1.17B answer:

  1. PROVENANCE      — which measure/column produced the headline number, and
                       the filters applied (status / year / month / person / …).
  2. DISAMBIGUATION  — when the question uses an ambiguous money term
                       (revenue / ยอด / sales) that maps to >=2 distinct
                       concepts, state which concept the DAX actually used and
                       name the alternatives.
  3. PLAUSIBILITY    — cheap sanity flags (ratio>1, percent>100, recognized
                       revenue attributed per-salesperson) surfaced as ⚠️.

Output is a markdown advisory block appended to the synthesized answer.
"""

from __future__ import annotations

import re
from typing import Any

# ── Metric concepts (ordered: most-specific token first) ─────────────────────
# Each entry: (regex token in DAX, human concept label). Labels mirror
# synthesizer.SYSTEM_PROMPT (SALES DATA MODEL — sales metrics, no finance P&L).
_CONCEPTS: list[tuple[str, str]] = [
    (r"Total SO Actual Amount \(P\) by Person", "ยอดขายจริง certified (realized actual) — [Total SO Actual Amount (P) by Person]"),
    (r"Total Target Amount",  "เป้า/target รายเดือน (Fact_GoalMonth) — [Total Target Amount]"),
    (r"Total SO Plan Amount", "ยอดแผน SO certified — [Total SO Plan Amount (P)]"),
    (r"Card_Achievement",     "% บรรลุเป้า (achievement)"),
    (r"Card_WinRate",         "อัตราชนะ (win rate)"),
    (r"Card_Forecast",        "พยากรณ์ (forecast)"),
    (r"Card_Pipeline",        "มูลค่า pipeline"),
    (r"SO Actual Amount",     "ยอดขายจริง (raw SO Actual Amount — prefer certified measure)"),
    (r"SO Plan Amount",       "ยอดแผน SO (SO Plan Amount, opportunity grain)"),
    (r"Total Activity",       "จำนวนกิจกรรม (activity count)"),
    (r"Possibility",          "ค่าความเป็นไปได้ของดีล (deal probability %)"),
]

# Ambiguous money terms in the *question* → distinct concepts they may mean.
_AMBIGUOUS_TERMS = ("revenue", "sales", "ยอด", "ยอดขาย", "turnover")
_MONEY_ALTERNATIVES = (
    "ยอดขายจริง (certified actual)",
    "ยอดแผน SO (SO Plan)",
    "เป้า/target (Fact_GoalMonth)",
)

# H2 — state-over-time: the model holds only a CURRENT snapshot (+transaction
# dates), no historical snapshots. Trending a STATE quantity (pipeline value /
# possibility / status as-of-each-period) is unanswerable and gets faked from
# transaction-date bucketing. Flow measures (revenue/sales per month) ARE
# trendable, so only flag when a STATE noun co-occurs with a temporal-change cue.
_STATE_NOUNS = (
    "pipeline", "possibility", "ความเป็นไปได้", "status", "สถานะ",
    "open deal", "open opportunit", "open opp", "headcount", "จำนวนพนักงาน",
    "aging", "prospect",
)
_TEMPORAL_CHANGE = (
    "over time", "week over week", "wow", "month over month", "mom",
    "quarter over quarter", "qoq", "trend", "แนวโน้ม", "changed", "change over",
    "dropped", "drop in", "increased", "decreased", "grew", "เปลี่ยนแปลง",
    "เทียบสัปดาห์", "เทียบเดือน", "ย้อนหลัง", "previous", "vs now",
    "transition", "เปลี่ยนจาก", "เปลี่ยนเป็น", "over the last",
)


def _concepts_in_dax(dax: str) -> list[str]:
    """Return human concept labels for every metric token present in the DAX
    (de-duplicated, most-specific token wins so 'SO Actual Amount' isn't also
    reported as the generic 'Amount')."""
    found: list[str] = []
    seen_spans: list[tuple[int, int]] = []
    for token, label in _CONCEPTS:
        for m in re.finditer(token, dax, re.IGNORECASE):
            span = m.span()
            # skip if this match sits inside an already-claimed (longer) token
            if any(s <= span[0] and span[1] <= e for s, e in seen_spans):
                continue
            seen_spans.append(span)
            if label not in found:
                found.append(label)
    return found


def _extract_filters(dax: str) -> list[str]:
    """Pull human-readable filter predicates from the DAX."""
    filters: list[str] = []

    # Status = "X"  /  Status IN {"A","B"}
    for op, val in re.findall(r"\[Status\]\s*(=|IN)\s*(\{[^}]*\}|\"[^\"]*\")", dax):
        clean = val.strip("{}").replace('"', "")
        filters.append(f"Status = {clean}")
    if re.search(r"\[IsWon\]\s*=\s*1", dax):
        filters.append("Status = Won (IsWon=1)")
    if re.search(r"\[IsPipeline\]\s*=\s*1", dax):
        filters.append("Status = Open (IsPipeline=1)")

    # Sale Person via SEARCH("name", Table[Sale Person Name])
    for name in re.findall(r"SEARCH\(\s*\"([^\"]+)\"\s*,\s*[A-Za-z_]+\[Sale Person Name\]", dax):
        filters.append(f"Sale Person ~ \"{name}\"")

    # YEAR(...) = N
    for y in re.findall(r"YEAR\([^)]*\)\s*=\s*(\d{4})", dax):
        filters.append(f"Year = {y}")
    # explicit year literals in TREATAS / Dim_Date[Year]
    for y in re.findall(r"Dim_Date\[Year\][^0-9]{0,12}(\d{4})", dax):
        if f"Year = {y}" not in filters:
            filters.append(f"Year = {y}")

    # MONTH(...) = N  (numeric literal only; variable M = per-month iteration)
    months = re.findall(r"MONTH\([^)]*\)\s*=\s*(\d{1,2})\b", dax)
    if months:
        filters.append("Month = " + ", ".join(months))

    # Possibility comparison
    for op, n in re.findall(r"\[Possibility\]\s*(=|>=|<=|>|<)\s*(\d+)", dax):
        filters.append(f"Possibility {op} {n}")

    # Solution Name = "X"
    for s in re.findall(r"\[Solution Name\]\s*=\s*\"([^\"]+)\"", dax):
        filters.append(f"Solution = {s}")

    # de-dup, preserve order
    out: list[str] = []
    for f in filters:
        if f not in out:
            out.append(f)
    return out


def _detect_disambiguation(question: str, concepts: list[str]) -> str | None:
    """If the question uses an ambiguous money term, state which concept was
    used and list the alternatives."""
    low = question.lower()
    # ASCII terms need word boundaries so "sales" doesn't match "salesperson";
    # Thai terms (no word separators) match as substrings.
    def _present(term: str) -> bool:
        if term.isascii():
            return re.search(rf"\b{re.escape(term)}\b", low) is not None
        return term in low
    if not any(_present(t) for t in _AMBIGUOUS_TERMS):
        return None
    if not concepts:
        return None
    used = concepts[0]
    others = [a for a in _MONEY_ALTERNATIVES if a.split(" (")[0] not in used]
    if not others:
        return None
    return (
        f"คำว่า \"revenue/ยอด/sales\" ตีความเป็น **{used}** "
        f"(นิยามอื่นที่เป็นไปได้: {'; '.join(others)})"
    )


def _plausibility_flags(rows: list[dict[str, Any]], dax: str) -> list[str]:
    """Cheap deterministic sanity flags."""
    flags: list[str] = []
    if not rows:
        return flags

    cols = list(rows[0].keys())
    for c in cols:
        low = c.lower()
        is_pct = "%" in c or "percent" in low or "margin" in low
        is_ratio = "rate" in low or "ratio" in low
        for r in rows:
            v = r.get(c)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            if is_pct and v > 100:
                flags.append(f"`{c}` = {v} เกิน 100% — ตรวจสูตร")
                break
            if is_ratio and not is_pct and v > 1:
                flags.append(f"`{c}` = {v} เกิน 1.0 (ควรอยู่ 0–1) — ตรวจสูตร")
                break

    # Raw SO Actual Amount (not the certified measure) → wrong basis + inactive
    # SO-Actual-Date relationship. Warn to prefer the certified measure.
    raw_actual = re.search(r"SUM\s*\(\s*Fact_Opportunity\[SO Actual Amount\]", dax)
    if raw_actual:
        flags.append(
            "ใช้ SUM(SO Actual Amount) ดิบ — ควรใช้ certified [Total SO Actual Amount (P) by Person] "
            "(SO Actual Date relationship เป็น inactive, ตัวเลขอาจคลาดเคลื่อน)"
        )
    return flags


# Status transition "from X to Y" needs status history (snapshot can't tell
# how a deal's status changed) — precise pattern so flow counts (e.g. "won
# deals trend") are not flagged.
_TRANSITION_RE = re.compile(
    r"(from|จาก)\s+(open|won|lost|เปิด|ชนะ|แพ้)\s+(to|into|เป็น|ไป)\s+(open|won|lost|เปิด|ชนะ|แพ้)",
    re.IGNORECASE,
)


def _state_over_time_flag(question: str) -> str | None:
    """Flag when the question asks for a STATE quantity trended over time, or a
    status transition — both unanswerable from a snapshot-only model (H2)."""
    low = question.lower()
    has_state_trend = any(n in low for n in _STATE_NOUNS) and any(t in low for t in _TEMPORAL_CHANGE)
    if not has_state_trend and not _TRANSITION_RE.search(question):
        return None
    return (
        "คำถามอิงสถานะย้อนเวลา (state-over-time: pipeline/possibility/status ณ แต่ละช่วง) "
        "แต่ model มีแค่ snapshot ปัจจุบัน ไม่มีประวัติย้อนหลัง — ตัวเลข trend คำนวณจากวันที่ "
        "transaction ไม่ใช่ค่าจริง ณ แต่ละช่วงเวลา จึงอาจคลาดเคลื่อน"
    )


def build_advisory(question: str, dax: str | None, rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Compute the three guardrails. Returns dict with keys:
    provenance (list[str]), filters (list[str]), disambiguation (str|None),
    warnings (list[str]). Empty/no-op when no DAX."""
    if not dax:
        return {"provenance": [], "filters": [], "disambiguation": None, "warnings": []}
    concepts = _concepts_in_dax(dax)
    warnings = _plausibility_flags(rows or [], dax)
    state_flag = _state_over_time_flag(question)
    if state_flag:
        warnings.append(state_flag)
    return {
        "provenance":     concepts,
        "filters":        _extract_filters(dax),
        "disambiguation": _detect_disambiguation(question, concepts),
        "warnings":       warnings,
    }


def format_advisory(adv: dict[str, Any]) -> str:
    """Render the advisory dict as a markdown block to append to the answer.
    Returns '' when there is nothing to add."""
    lines: list[str] = []
    if adv.get("warnings"):
        for w in adv["warnings"]:
            lines.append(f"⚠️ {w}")
    prov = adv.get("provenance") or []
    filt = adv.get("filters") or []
    if prov or filt:
        bits = []
        if prov:
            bits.append("ตัวเลขจาก " + "; ".join(prov))
        if filt:
            bits.append("เงื่อนไข: " + ", ".join(filt))
        lines.append("📊 ที่มา: " + " | ".join(bits))
    if adv.get("disambiguation"):
        lines.append("ℹ️ " + adv["disambiguation"])
    return "\n\n".join(lines)
