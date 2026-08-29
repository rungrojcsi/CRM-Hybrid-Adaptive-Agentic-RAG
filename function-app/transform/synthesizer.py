"""
synthesizer.py — Unified answer composition for Qual + Quant hybrid responses.

Takes qualitative context (chunks) + quantitative result (rows) + user question,
produces a natural Thai/English answer that weaves both sources.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from .quant_provenance import build_advisory, format_advisory

logger = logging.getLogger(__name__)

AZURE_OPENAI_ENDPOINT      = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY       = os.getenv("AZURE_OPENAI_API_KEY", "")
SYNTH_DEPLOYMENT           = os.getenv("SYNTH_DEPLOYMENT", "gpt-5.4")
SYNTH_API_VERSION          = os.getenv("SYNTH_API_VERSION", "2024-12-01-preview")
SYNTH_MAX_CONTEXT_CHARS    = int(os.getenv("SYNTH_MAX_CONTEXT_CHARS", "8000"))

SYSTEM_PROMPT = """You are a CRM data assistant for CSI (a manufacturing company).
You answer questions about customer accounts and CRM data using the provided context.

You may receive two types of evidence:
1. QUAL_CONTEXT: chunks from account profiles in the CRM system (each labeled [N] Account: <id>)
2. QUANT_DATA: structured rows from DAX query against the Semantic Model
   - QUANT_DATA arrives as a markdown table with the DAX query above it for reference
   - The total row count is shown when output is truncated

Model = SALES DATA MODEL (sales pipeline). Monetary concepts (never conflate):

1. **เป้า / Target / quota** = `[Total Target Amount]` (Fact_GoalMonth).
   The official monthly sales target per sales person. The ONLY "เป้า".

2. **ยอดขายจริง / Actual sales closed** = `[Total SO Actual Amount (P) by Person]`
   (certified). THE "ยอดขาย" most users mean. Do NOT recompute from raw
   SUM(SO Actual Amount) — wrong basis.

3. **ยอดแผน SO / SO Plan (committed)** = `[Total SO Plan Amount (P)]` or
   SUM(Fact_Opportunity[SO Plan Amount]) (per-opportunity commit / pipeline when
   Status="Open"). NOT a quota.

⚠️ NO recognized-revenue / profit / margin / cost in this model. If a number
   labelled revenue/profit appears, it is NOT available — say so, don't invent.

Column-label mapping:
- `[Actual]` / `[Total SO Actual Amount (P) by Person]` → ยอดขายจริง (concept 2)
- `[Target]` / `[Total Target Amount]` → เป้า (concept 1)
- `[Plan]` / `[SO Plan Amount]` → ยอดแผน SO / pipeline (concept 3)
- `Dim_Account[Account Name]` → company name (use directly; rows carry real names now).

"Opportunity (ดีล)" = 1 pipeline row. Status: Open / Won / Lost (the reliable state field).

Rules:
- Answer only from the provided context. Do not fabricate data, numbers, or company names.
- If the context does not contain enough information, say so clearly.
- Be concise and factual. Use bullet points for lists.
- Cite sources at the Account level whenever possible:
  - Qual: cite by account name — e.g. "(จาก Account: บมจ. ABC)"
  - Quant with per-account breakdown: cite contributing accounts —
    e.g. "(จาก CRM Sales Model — top: บมจ. ABC, XYZ, DEF)"
  - Quant aggregate-only (no account dimension): cite as
    "(จาก CRM Sales Model — aggregate across all accounts)"
- Respond in the same language as the user's question (Thai or English).
- Format numbers with commas (1,234,567.89)."""


_CURRENCY_HINTS = ("amount", "revenue", "profit", "cost", "expense", "plan", "actual", "sales")


def _looks_currency(key: str) -> bool:
    low = key.lower()
    return any(h in low for h in _CURRENCY_HINTS)


def _fmt_value(key: str, v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        if _looks_currency(key):
            return f"{v:,.2f}"
        if isinstance(v, float):
            return f"{v:,.4f}".rstrip("0").rstrip(".")
        return f"{v:,}"
    return str(v)


def _format_quant_rows(rows: list[dict[str, Any]], dax: str | None, max_rows: int = 50) -> str:
    """Render rows as markdown table with DAX header + truncation note."""
    if not rows:
        return ""
    cols = list(rows[0].keys())
    head = "| " + " | ".join(cols) + " |"
    sep  = "| " + " | ".join("---" for _ in cols) + " |"
    body_rows = []
    for r in rows[:max_rows]:
        body_rows.append("| " + " | ".join(_fmt_value(c, r.get(c)) for c in cols) + " |")
    parts = []
    if dax:
        parts.append(f"```dax\n{dax.strip()}\n```")
    parts.append(head)
    parts.append(sep)
    parts.extend(body_rows)
    if len(rows) > max_rows:
        parts.append(f"\n_(showing {max_rows} of {len(rows)} rows)_")
    return "\n".join(parts)


def _build_qual_context(
    chunks: list[dict[str, Any]],
    max_chars: int = SYNTH_MAX_CONTEXT_CHARS,
) -> str:
    """Mirror V3 asker._build_context — total cap (not per-chunk) + account_id headers."""
    lines = []
    total = 0
    for i, chunk in enumerate(chunks):
        acct = chunk.get("account_id", "?")
        sim  = chunk.get("similarity", 0.0)
        header = f"[{i + 1}] Account: {acct} (similarity: {sim:.3f})"
        body   = (chunk.get("content") or "").strip()
        block  = f"{header}\n{body}\n"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                lines.append(block[:remaining] + "\n[truncated]")
            break
        lines.append(block)
        total += len(block)
    return "\n".join(lines)


def synthesize(
    question: str,
    qual_chunks: list[dict[str, Any]] | None,
    quant_rows: list[dict[str, Any]] | None,
    temperature: float = 0.1,
    quant_dax: str | None = None,
    quant_citation: dict[str, Any] | None = None,
) -> str:
    """Compose unified Thai answer from Qual + Quant evidence.

    quant_citation (optional): {top_accounts, is_aggregate_only, measure} from
    dax_generator.extract_account_citation — passed as a CITATION_HINT block so the
    LLM cites at Account level per the Rules block.
    """
    qual_text = _build_qual_context(qual_chunks) if qual_chunks else ""
    quant_text = _format_quant_rows(quant_rows, quant_dax) if quant_rows else ""

    user_msg = f"คำถาม: {question}\n\n"
    if qual_text:
        user_msg += f"QUAL_CONTEXT:\n{qual_text}\n\n"
    if quant_text:
        user_msg += f"QUANT_DATA:\n{quant_text}\n\n"
    if quant_citation:
        subj = quant_citation.get("filter_subject")
        if quant_citation.get("top_accounts"):
            parts = []
            for a in quant_citation["top_accounts"]:
                nm = a.get("name")
                if not nm:
                    continue
                src = a.get("name_source")
                # Heuristic guesses get a ~ prefix so user knows it's a best-guess
                # (extracted from opportunity-name prefix, not a verified company name).
                label = f"~{nm}" if src == "heuristic" else str(nm)
                parts.append(label)
            names = ", ".join(parts)
            user_msg += (
                f"CITATION_HINT: Quant top contributing accounts → cite "
                f"'(จาก CRM Sales Model — top: {names})'. Names prefixed with ~ are "
                f"best-guess from opportunity-name prefix (account is Inactive in D365); "
                f"add a brief footnote 'ชื่อที่นำหน้าด้วย ~ คือชื่อโดยประมาณ' if any ~ appears. "
                f"If a name still looks like a GUID, lookup failed entirely — keep as-is.\n\n"
            )
        elif quant_citation.get("is_aggregate_only") and subj:
            user_msg += f"CITATION_HINT: Quant result is filtered by {subj} — cite '(จาก CRM Sales Model — filtered by {subj})'. DO NOT say 'aggregate across all accounts'.\n\n"
        elif quant_citation.get("is_aggregate_only"):
            user_msg += "CITATION_HINT: Quant is aggregate-only — cite '(จาก CRM Sales Model — aggregate across all accounts)'.\n\n"
    user_msg += "ตอบเป็นภาษาไทย กระชับ:"

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{SYNTH_DEPLOYMENT}/chat/completions"
        f"?api-version={SYNTH_API_VERSION}"
    )
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        "temperature": temperature,
        "max_completion_tokens": 1500,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "api-key": AZURE_OPENAI_API_KEY},
    )
    # Degrade gracefully: a raised HTTPError (400/429), transport timeout, or a
    # None content (finish_reason=length / content filter) must NOT bubble up as
    # an unhandled 500 — that fails the whole Foundry agent run. Return a usable
    # answer (with the quant advisory still appended below) instead.
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        answer = (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:
        logger.warning("Synthesizer LLM call failed: %s", exc)
        answer = ""

    if not answer:
        answer = (
            "ขออภัย ระบบไม่สามารถเรียบเรียงคำตอบได้ในขณะนี้"
            + ("" if quant_rows else " (ไม่พบข้อมูลที่เกี่ยวข้อง)")
        )

    # H3 guardrails: append deterministic provenance / disambiguation /
    # plausibility advisory so the number is interpretable (quant only).
    if quant_rows:
        advisory = format_advisory(build_advisory(question, quant_dax, quant_rows))
        if advisory:
            answer = f"{answer}\n\n{advisory}"
    return answer
