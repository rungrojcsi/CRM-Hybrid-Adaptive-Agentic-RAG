"""
dax_generator.py — NL→DAX for prod SALES DATA MODEL (Title Case schema).

Targets the production Semantic Model `SALES DATA MODEL` in workspace SALES_DATA.
Prod has 21 pre-defined Sys_Measure DAX measures already in the model — NO DEFINE
injection needed; LLM just references measures by name.

Env override:
  PBI_DEFINE_MEASURES=true  → fall back to inline DEFINE pattern (for sm_crm_rs)
  PBI_DEFINE_MEASURES=false (default) → prod mode, no DEFINE injection
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import urllib.request

from .account_resolver import resolve_with_source as _resolve_account_with_source

logger = logging.getLogger(__name__)

AZURE_OPENAI_ENDPOINT  = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY   = os.getenv("AZURE_OPENAI_API_KEY", "")
DAX_GEN_DEPLOYMENT     = os.getenv("DAX_GEN_DEPLOYMENT", "gpt-5.4")
DAX_GEN_API_VERSION    = os.getenv("DAX_GEN_API_VERSION", "2024-12-01-preview")
PBI_DEFINE_MEASURES    = os.getenv("PBI_DEFINE_MEASURES", "false").lower() == "true"

_THIS_DIR = pathlib.Path(__file__).parent
_MEASURE_FILE = _THIS_DIR.parent / "dax" / "v5_crm_measures.dax"

# ============================================================
# Prod SALES DATA MODEL schema (Title Case)
# ============================================================
PROD_SCHEMA = """\
Semantic Model: SALES DATA MODEL (CSI CRM, sales pipeline focus).
Tables are Title Case; column names HAVE SPACES — wrap in [square brackets].

═══ CORE FACT TABLES ═══
Fact_Opportunity (deal grain, 1 row/opportunity) — the main pipeline table:
  [Opportunity ID] [Opportunity Name] [Opportunity No] [Account ID] [Sales Person ID]
  [Sale Person Name] (free text) [Solution ID] [Solution Name] [Status] (Open/Won/Lost)
  [IsWon] [IsPipeline] [IsImportant] [Possibility] (int %) [Progress]
  ⚠️ [IsWon]/[IsPipeline] are UNRELIABLE (IsWon=1 wrongly includes ~390 Open deals).
     For Won/Lost/Open ALWAYS filter [Status]="Won"/"Lost"/"Open", NOT the flags.
  [SO Plan Amount] [SO Actual Amount] (+ (H)(K)(P) split variants)
  [SO Plan Date] [SO Actual Date] [Closed Date] [Won Date] [Create Date] [Update Date]
  [Est. Close Date] [Actual Close Date] [Last Activity Date]
  [Sales Cycle Days] [Aging Days] [Aging Group] [Prospect Category Name]
  [Status Detail] [Final Reason] [Business Model] [Previous Progress]
  ⚠️ Account NAME is NOT here — join Dim_Account via [Account ID] (active rel).
  ⚠️ [SO Actual Date]→Dim_Date is INACTIVE. Do NOT slice SO Actual by month with a
     raw SUM over SO Actual Date — use the certified actual measure instead.

Fact_GoalMonth (monthly sales TARGET/quota per sales person):
  [Target Month] (Date) [Target Amount] [Sales Person ID]
  → the ONLY source of "เป้า/target/quota". Use [Total Target Amount]. Slices by Dim_Date.

Fact_SalesOrder: [Grand Total] [Invoiced Amount] [Remaining Amount] [Status Code] [Created On] [Opportunity ID]
Fact_Invoice:    [Grand Total] [Invoice Type] [Is Approved] [Created On] [Opportunity ID]
Fact_IncomePlan (per-opp income schedule): [SO Plan Date] [SO Plan Amount] [SO Actual Date] [SO Actual Amount] [Invoice Date] [Invoice Amount] [Opportunity ID]
Fact_OpportunityMovement (possibility/progress HISTORY per opp):
  [Opportunity Name] [Account ID] [Current Possibility] [Previous Possibility]
  [Possibility Updated Date] [Current Progress] [Previous Progress] → answers "possibility changed/dropped".
Fact_Activity: [Activity Type] [Activity Date] [Subject] [Duration (Mins)] [Sales Person ID] [Opportunity ID]

═══ DIMENSIONS ═══
Dim_Account: [Account ID] [Account Name] [Industry] [Customer Type] [Province] [Country]
  [Customer Level] [Open Deals] [Is JOC] [Biz Sector] — JOIN for company names.
Dim_SalesPerson: [Sales Person ID] [Full Name] [Department ID] [Department Name] [User Status]
Dim_Date: [Date] [Year] [MonthNo] [Month Year] (e.g. "Jan 2026") [Quarter] (Q1-Q4)
  [MonthSort] (202601) [Fiscal Year] [ShortMonth]. NO [YearMonth] column.
Dim_Department: [Department ID] [Department Name] [Business Unit]
Dim_Solution: [Solution ID] [Solution Name] [Solution Code]
Dim_Contact: [Contact ID] [Account ID] [Contact Name] [Job Title] [Email]
Dim_Progress: [Progress Seq] [Progress]
Dim_ProspectType: [Prospect Category ID] [Prospect Category Name]
Others: Dim_Industry L1/L2, Dim_IndustrialEstate, Dim_OpportunityClose, Dim_Review.

═══ ACTIVE relationships (join directly) ═══
  Fact_Opportunity[Account ID]→Dim_Account ; [Sales Person ID]→Dim_SalesPerson
  Fact_Opportunity[SO Plan Date]→Dim_Date ; Fact_Opportunity→Fact_OpportunityMovement (Opportunity ID)
  Fact_GoalMonth[Target Month]→Dim_Date ; [Sales Person ID]→Dim_SalesPerson
  Fact_Activity[Activity Date]→Dim_Date ; [Sales Person ID]→Dim_SalesPerson
  Dim_SalesPerson[Department ID]→Dim_Department
INACTIVE (needs USERELATIONSHIP — avoid; prefer certified measures):
  Fact_Opportunity[SO Actual Date]→Dim_Date

NOT IN THIS MODEL (do NOT reference): recognized revenue / profit / margin / cost /
headcount / project expense. This is a SALES model — no finance P&L.
If user asks profit/margin/cost → reply it is not available in this model.
"""

PROD_MEASURES = """\
Certified measures (reference by [name] — they handle date/relationship context internally):
TARGET / ACTUAL / PLAN:
- [Total Target Amount]                  — monthly sales target (Fact_GoalMonth)
- [Total SO Actual Amount (P) by Person]  — realized actual sales (THE "ยอดขายจริง/actual closed")
- [Total SO Plan Amount (P)]             — committed SO plan
- [Card_Target_YTD] [Card_Actual_YTD] [Card_Plan_YTD] — YTD scalars
- [Card_Achievement_YTD]                 — % actual vs target
- [Accum Target Amount] [Accum SO Actual Amount (P)] — running totals
PIPELINE / FORECAST / WINRATE:
- [Card_Forecast] [Card_Pipeline_3Months] [Card_Pipeline_Coverage]
- [Card_WinRate_YTD] [Count of Status for Won]
ACTIVITY:
- [Total Activity] [Activity_Qty_Appointment] [Activity_Qty_Email] [Activity_Qty_Phone] [Activity_Qty_Task]
HISTORY:
- [Possibility Delta] (+ Fact_OpportunityMovement Current/Previous columns)

⚠️ For "ยอดขายจริง/actual sales closed" ALWAYS use [Total SO Actual Amount (P) by Person]
   — NOT a raw SUM(SO Actual Amount) (wrong basis + inactive date relationship).
⚠️ For "เป้า/target" ALWAYS use [Total Target Amount] — never substitute SO Plan.
"""

PROD_FEW_SHOT = """\
═══ EX 1: Actual vs Target per month (THE headline — "ปิดยอดเทียบเป้า") ═══
Q: "เดือนนี้/รายเดือนปีนี้ เซลล์ปิดยอดได้เท่าไหร่ จากเป้าเท่าไหร่"
EVALUATE
SUMMARIZECOLUMNS(
  Dim_Date[Month Year], Dim_Date[MonthSort],
  TREATAS({YEAR(TODAY())}, Dim_Date[Year]),
  "Actual", [Total SO Actual Amount (P) by Person],
  "Target", [Total Target Amount]
)
ORDER BY [MonthSort]

═══ EX 2: Company total actual vs target for a month ═══
EVALUATE
CALCULATETABLE(
  ROW("Actual", [Total SO Actual Amount (P) by Person], "Target", [Total Target Amount]),
  TREATAS({2026}, Dim_Date[Year]), TREATAS({5}, Dim_Date[MonthNo])
)

═══ EX 3: Per-sales-person actual vs target (Top N) ═══
EVALUATE
TOPN(10,
  SUMMARIZECOLUMNS(
    Dim_SalesPerson[Full Name],
    TREATAS({YEAR(TODAY())}, Dim_Date[Year]),
    "Actual", [Total SO Actual Amount (P) by Person],
    "Target", [Total Target Amount]
  ),
  [Actual], DESC)
ORDER BY [Actual] DESC

═══ EX 4: Top accounts by value — RETURN COMPANY NAME (no GUID) ═══
EVALUATE
TOPN(10,
  SUMMARIZECOLUMNS(Dim_Account[Account Name],
    "Actual", [Total SO Actual Amount (P) by Person]),
  [Actual], DESC)
ORDER BY [Actual] DESC

═══ EX 5: Top large OPEN pipeline opportunities ═══
EVALUATE
TOPN(5,
  SUMMARIZECOLUMNS(Dim_Account[Account Name], Fact_Opportunity[Opportunity Name],
    FILTER(Fact_Opportunity, Fact_Opportunity[Status]="Open"),
    "Pipeline", SUM(Fact_Opportunity[SO Plan Amount])),
  [Pipeline], DESC)
ORDER BY [Pipeline] DESC

═══ EX 6: Lost opportunities by account name + value ═══
EVALUATE
TOPN(10,
  SUMMARIZECOLUMNS(Dim_Account[Account Name],
    FILTER(Fact_Opportunity, Fact_Opportunity[Status]="Lost"),
    "LostValue", SUM(Fact_Opportunity[SO Plan Amount])),
  [LostValue], DESC)
ORDER BY [LostValue] DESC

═══ EX 7: Top lost deals by possibility (deal-level) ═══
EVALUATE
TOPN(10,
  SELECTCOLUMNS(FILTER(Fact_Opportunity, Fact_Opportunity[Status]="Lost"),
    "Opportunity", Fact_Opportunity[Opportunity Name],
    "Account", RELATED(Dim_Account[Account Name]),
    "Possibility", Fact_Opportunity[Possibility],
    "Plan Amount", Fact_Opportunity[SO Plan Amount]),
  [Possibility], DESC)
ORDER BY [Possibility] DESC

═══ EX 8: Win rate per account (guard zero denominator) ═══
EVALUATE
TOPN(10,
  FILTER(
    SUMMARIZECOLUMNS(Dim_Account[Account Name],
      "Won", COALESCE(CALCULATE(COUNTROWS(Fact_Opportunity), Fact_Opportunity[Status]="Won"), 0),
      "Closed", COALESCE(CALCULATE(COUNTROWS(Fact_Opportunity), Fact_Opportunity[Status] IN {"Won","Lost"}), 0),
      "Win Rate", DIVIDE(
        CALCULATE(COUNTROWS(Fact_Opportunity), Fact_Opportunity[Status]="Won"),
        CALCULATE(COUNTROWS(Fact_Opportunity), Fact_Opportunity[Status] IN {"Won","Lost"}), 0)),
    [Closed] >= 3),
  [Win Rate], DESC)
ORDER BY [Win Rate] DESC

═══ EX 9: Possibility dropped (HISTORY — Fact_OpportunityMovement) ═══
Q: "ดีลไหน possibility ลดลง / เปลี่ยนแปลง"
EVALUATE
SELECTCOLUMNS(
  FILTER(Fact_OpportunityMovement,
    Fact_OpportunityMovement[Current Possibility] < Fact_OpportunityMovement[Previous Possibility]),
  "Opportunity", Fact_OpportunityMovement[Opportunity Name],
  "Previous", Fact_OpportunityMovement[Previous Possibility],
  "Current", Fact_OpportunityMovement[Current Possibility],
  "Updated", Fact_OpportunityMovement[Possibility Updated Date])

═══ EX 10: Activity count per sales person ═══
EVALUATE
SUMMARIZECOLUMNS(Dim_SalesPerson[Full Name],
  TREATAS({YEAR(TODAY())}, Dim_Date[Year]),
  "Activities", [Total Activity],
  "Appointments", [Activity_Qty_Appointment])

═══ EX 11: Specific sales person partial-name match ═══
EVALUATE
CALCULATETABLE(
  ROW("Actual", [Total SO Actual Amount (P) by Person], "Target", [Total Target Amount]),
  FILTER(ALL(Dim_SalesPerson), SEARCH("Salesperson1", Dim_SalesPerson[Full Name], 1, 0) > 0),
  TREATAS({YEAR(TODAY())}, Dim_Date[Year]))

═══ EX 12: Pipeline by solution (filter blank group key) ═══
EVALUATE
TOPN(10,
  FILTER(
    SUMMARIZECOLUMNS(Dim_Solution[Solution Name],
      "Pipeline", CALCULATE(SUM(Fact_Opportunity[SO Plan Amount]), Fact_Opportunity[Status]="Open")),
    NOT(ISBLANK(Dim_Solution[Solution Name]))),
  [Pipeline], DESC)
ORDER BY [Pipeline] DESC
"""

ANTI_PATTERNS = """\
AVOID (SALES DATA MODEL specific):
- Title-case tables; column names have spaces in [brackets].
- [IsWon]/[IsPipeline] are INTEGER 0/1 — never = TRUE/FALSE.
- Account names: ALWAYS join Dim_Account[Account Name] — NEVER return a bare [Account ID] GUID.
- For "ยอดขายจริง/actual sales": [Total SO Actual Amount (P) by Person] — NEVER raw SUM(SO Actual) (inactive date rel + wrong basis).
- For "เป้า/target": [Total Target Amount] — NEVER substitute SUM(SO Plan Amount).
- Certified measures slice Dim_Date correctly — use SUMMARIZECOLUMNS(Dim_Date[...], measure) for trends. No GENERATE+SUMX hacks needed.
- NO finance: do not invent [Total Revenue]/[Total Profit]/cost/margin — not in this model.
- TOPN must be followed by ORDER BY same column DESC.
- Guard COUNTROWS with COALESCE(...,0); guard ratios with denominator > 0; filter blank group keys with NOT(ISBLANK(...)).
- Implicit year = YEAR(TODAY()); only use a literal year when user states it.
"""

SYSTEM_PROMPT = """You are a DAX query generator for the production SALES DATA MODEL Semantic Model (CSI CRM sales pipeline).

{schema}

{measures}

{few_shot}

{anti_patterns}

Rules:
- Output ONLY the EVALUATE clause. No DEFINE, no markdown fences, no comments.
- Reference certified measures by [name]; Title-case table names.
- "ยอดขาย/sales/ยอดปิด" → [Total SO Actual Amount (P) by Person]. "เป้า/target/quota" → [Total Target Amount]. "แผน/plan" → [Total SO Plan Amount (P)] or SUM([SO Plan Amount]).
- Account/customer questions → return Dim_Account[Account Name], never a GUID.
- Trends/per-month/per-quarter → SUMMARIZECOLUMNS(Dim_Date[Month Year]/[Quarter], <measure>) with an explicit Year TREATAS; certified measures respect the date context.
- Possibility/progress CHANGE over time → Fact_OpportunityMovement (Current vs Previous).
- Activity questions → [Total Activity] / [Activity_Qty_*] (Fact_Activity).
- Finance (profit/margin/cost/recognized revenue) is NOT in this model → return EVALUATE ROW("note", "Finance metrics are not available in the sales model.").
- TOPN → always ORDER BY same column DESC. COUNTROWS → COALESCE(...,0). Ratios → guard denominator > 0. Blank group keys → FILTER NOT(ISBLANK(...)).
- Implicit month/quarter without year → YEAR(TODAY()); explicit literal year only when user states it."""


def _build_system_prompt() -> str:
    return SYSTEM_PROMPT.format(
        schema=PROD_SCHEMA,
        measures=PROD_MEASURES,
        few_shot=PROD_FEW_SHOT,
        anti_patterns=ANTI_PATTERNS,
    )


def _load_measures_for_define() -> str:
    """Load measure library for inline DEFINE (only when PBI_DEFINE_MEASURES=true)."""
    if not _MEASURE_FILE.exists():
        return ""
    raw = _MEASURE_FILE.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("//")]
    return "\n".join(lines)


# Dim_Date[Year] stores Christian Era (e.g. 2026). Thai users say Buddhist Era
# (2569). The DAX-gen LLM sometimes emits the BE year literally as the filter value,
# which matches nothing → empty result. This deterministic post-process converts any
# bare Buddhist-era year (2500–2599) in the generated DAX to CE (subtract 543). Months
# are 1–12 and amounts are large/decimal, so a bare 25xx integer is always a BE year.
_BE_YEAR_RE = re.compile(r"\b(25\d\d)\b")


def _be_year_to_ce(dax: str) -> str:
    def _sub(m: "re.Match[str]") -> str:
        y = int(m.group(1))
        return str(y - 543) if 2500 <= y <= 2599 else m.group(0)
    return _BE_YEAR_RE.sub(_sub, dax)


def generate_dax(question: str, retry_error: str | None = None, previous_dax: str | None = None) -> str:
    """Generate DAX query. For prod model, output is just EVALUATE clause."""
    system = _build_system_prompt()

    user_msg = question
    if retry_error and previous_dax:
        user_msg = (
            f"Previous attempt FAILED with error or empty result:\n{retry_error[:500]}\n\n"
            f"Previous DAX:\n{previous_dax[-600:]}\n\n"
            f"Generate a CORRECTED EVALUATE for the original question:\n{question}"
        )

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{DAX_GEN_DEPLOYMENT}/chat/completions"
        f"?api-version={DAX_GEN_API_VERSION}"
    )
    body = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user_msg},
        ],
        "temperature": 0,
        "max_completion_tokens": 1200,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "api-key": AZURE_OPENAI_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    user_query = data["choices"][0]["message"]["content"].strip()

    if user_query.startswith("```"):
        user_query = "\n".join(
            line for line in user_query.splitlines() if not line.startswith("```")
        )

    # Deterministic year floor: BE → CE (2569 → 2026) regardless of what the LLM emitted.
    fixed = _be_year_to_ce(user_query)
    if fixed != user_query:
        logger.info("DAX BE→CE year fix applied")
        user_query = fixed

    issues = validate_dax(user_query)
    if issues:
        # Hard-block on safety-critical issues (DML / DEFINE / forbidden tables)
        critical = [i for i in issues if any(k in i for k in [
            "DML keyword", "DEFINE block not allowed", "NOT in CSI_DATA_MODEL",
            "Bare 'EVALUATE",
        ])]
        if critical:
            logger.error("DAX BLOCKED by pre-flight: %s | DAX: %s", "; ".join(critical), user_query[:200])
            # Replace with safe deflection so caller doesn't ship bad DAX to PBI
            user_query = (
                'EVALUATE ROW("note", "DAX rejected by pre-flight: ' +
                "; ".join(critical).replace('"', "'") + '")'
            )
        else:
            logger.warning("DAX pre-flight warnings: %s | DAX: %s", "; ".join(issues), user_query[:200])

    if PBI_DEFINE_MEASURES:
        measures = _load_measures_for_define()
        if measures:
            return f"DEFINE\n{measures}\n\n{user_query}"

    return user_query


def is_empty_or_null_result(rows: list[dict]) -> bool:
    """Detect when PBI returned 0 rows OR all values null/empty."""
    if not rows:
        return True
    for row in rows:
        for v in row.values():
            if v is not None and v != "":
                return False
    return True


# ============================================================
# DAX pre-flight validator (cheap, regex-only)
# ============================================================
# Finance tables not present in SALES DATA MODEL — flag if the LLM reverts to them.
_FORBIDDEN_TABLES = {"Fact_Project_Expenses", "Dim_Employee_Cost", "Dim_Allocation", "Dim_Allocate_Report"}
_FORBIDDEN_COLS = {"[YearMonth]"}
_KNOWN_TABLES = {
    "Dim_Date", "Dim_Department", "Dim_SalesPerson", "Dim_Account", "Dim_Contact",
    "Dim_Solution", "Dim_Progress", "Dim_ProspectType", "Dim_OpportunityClose",
    "Dim_Review", "Dim_IndustrialEstate",
    "Fact_Opportunity", "Fact_GoalMonth", "Fact_SalesOrder", "Fact_Invoice",
    "Fact_IncomePlan", "Fact_OpportunityMovement", "Fact_Activity",
}

_TABLE_REF_RE = re.compile(r"\b([A-Z][A-Za-z_0-9]*)\[")
_BOOLEAN_BUG_RE = re.compile(r"\[Is\w+\]\s*=\s*(TRUE|FALSE)\b", re.IGNORECASE)
_DEFINE_RE = re.compile(r"^\s*DEFINE\b", re.IGNORECASE | re.MULTILINE)
_DML_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|MERGE)\s+(INTO|TABLE|FROM|VIEW)\b", re.IGNORECASE)
_BARE_TABLE_DUMP_RE = re.compile(r"^\s*EVALUATE\s+([A-Z][A-Za-z_]+)\s*$", re.IGNORECASE | re.MULTILINE)


def validate_dax(dax: str) -> list[str]:
    """Cheap pre-flight checks. Returns list of issues (empty == OK).

    Controls:
      - Forbidden tables (Dim_Account/Fact_Activity/Goal — not deployed)
      - Forbidden columns ([YearMonth])
      - Boolean bug ([IsWon] = TRUE)
      - Unknown tables (catches hallucinated table names)
      - DEFINE block injection (we use pre-defined measures only — prod safety)
      - DML keywords (executeQueries is read-only but belt-and-braces)
      - Bare table EVALUATE (e.g. "EVALUATE Fact_Opportunity" returns ALL 2,536 rows)
    """
    issues: list[str] = []
    for forbidden in _FORBIDDEN_TABLES:
        if forbidden in dax:
            issues.append(f"References table {forbidden} which is NOT in SALES DATA MODEL")
    for col in _FORBIDDEN_COLS:
        if col in dax:
            issues.append(f"Column {col} does not exist — use [Month Year] instead")
    if _BOOLEAN_BUG_RE.search(dax):
        issues.append("IsWon/IsPipeline compared to TRUE/FALSE — use = 1 or = 0 (Integer column)")
    if _DEFINE_RE.search(dax) and not os.getenv("PBI_DEFINE_MEASURES", "").lower() == "true":
        issues.append("DEFINE block not allowed in prod mode — measures are pre-defined in SALES DATA MODEL")
    if _DML_RE.search(dax):
        issues.append("Destructive DML keyword detected — DAX is read-only via executeQueries")
    bare = _BARE_TABLE_DUMP_RE.search(dax)
    if bare:
        issues.append(
            f"Bare 'EVALUATE {bare.group(1)}' returns ALL rows — use SUMMARIZECOLUMNS or TOPN"
        )
    referenced = set(_TABLE_REF_RE.findall(dax))
    unknown = referenced - _KNOWN_TABLES - _FORBIDDEN_TABLES
    unknown = {t for t in unknown if "_" in t}
    if unknown:
        issues.append(f"Unknown table(s) referenced: {', '.join(sorted(unknown))}")
    return issues


# ============================================================
# Account-citation post-processor (Task #9)
# ============================================================
_ACCOUNT_KEY_HINTS = ("account", "customer", "sale person", "saleperson", "sales person")


def _is_account_column(key: str) -> bool:
    low = key.lower()
    return any(hint in low for hint in _ACCOUNT_KEY_HINTS)


def _pick_measure_column(rows: list[dict]) -> str | None:
    """First numeric column wins (DAX EVALUATE preserves column order)."""
    if not rows:
        return None
    for k, v in rows[0].items():
        if isinstance(v, (int, float)) and not _is_account_column(k):
            return k
    return None


_FILTER_SUBJECT_HINTS = (
    # role labels
    "sale person", "sales person", "saleperson",
    # sales person names — the real roster is loaded from config/CRM at runtime;
    # placeholders here for the public version (real employee names removed)
    "salesperson1", "salesperson2", "salesperson3",
    # departments
    "swd", "wms", "iiots", "imd", "ggs", "icsd", "erp", "sbs", "joc", "iotd",
    # solutions
    "mpos", "itsystem", "rubix", "f1 gl", "xells",
    # NOTE: very short solution codes (e.g. "ax", "tm") omitted to avoid
    # false positives ("tax", "team"); detect those via Solution Name in DAX.
)


def _detect_filter_subject(question: str | None) -> str | None:
    """Return a short label naming the filter subject the question imposes
    (e.g. 'Salesperson1', 'Sale Person Name'). None if no narrow filter detected.
    """
    if not question:
        return None
    low = question.lower()
    for hint in _FILTER_SUBJECT_HINTS:
        if hint in low:
            # Return the original-cased token from the question for cleanest cite
            for tok in question.split():
                if hint in tok.lower():
                    return tok.strip(".,?!:;'\"")
            return hint
    return None


def extract_account_citation(rows: list[dict], top_n: int = 5, question: str | None = None) -> dict:
    """Post-process PBI rows into Account-level citation envelope.

    Returns: {
        value: scalar (if single-row aggregate) or None,
        measure: column name used for ranking,
        top_accounts: [{name, value}, ...],
        is_aggregate_only: bool,
        filter_subject: str | None,  # set when a narrow filter exists but rows lack Account dim
    }
    """
    if not rows:
        return {"value": None, "measure": None, "top_accounts": [],
                "is_aggregate_only": True, "filter_subject": _detect_filter_subject(question)}

    measure = _pick_measure_column(rows)
    account_cols = [k for k in rows[0].keys() if _is_account_column(k)]

    if not account_cols or (len(rows) == 1 and not account_cols):
        value = rows[0].get(measure) if measure and len(rows) == 1 else None
        return {
            "value": value,
            "measure": measure,
            "top_accounts": [],
            "is_aggregate_only": True,
            "filter_subject": _detect_filter_subject(question),
        }

    name_col = account_cols[0]
    if measure:
        ranked = sorted(
            (r for r in rows if r.get(measure) is not None),
            key=lambda r: r[measure],
            reverse=True,
        )[:top_n]
    else:
        ranked = rows[:top_n]
    top = []
    for r in ranked:
        raw = r.get(name_col)
        resolved, source = (None, None)
        if isinstance(raw, str) and len(raw) == 36 and raw.count("-") == 4:
            resolved, source = _resolve_account_with_source(raw)
        top.append({
            "name": resolved or raw,            # company name preferred; GUID fallback
            "account_id": raw if resolved else None,  # GUID kept when resolved
            "name_source": source,              # 'primary' | 'heuristic' | None
            "value": r.get(measure),
        })
    return {
        "value": None,
        "measure": measure,
        "top_accounts": top,
        "is_aggregate_only": False,
        "filter_subject": None,
    }
