"""
orchestrator.py — code-side orchestrator helpers for /api/orchestrator.

The Foundry bot (crm-hybrid-agent) is a thin relay; this module does the real work:
  1. plan_subquestions(q)  → (quant_q, qual_q) = verbatim + tool-specific narrative (LLM)
  2. (endpoint runs ask_quant ∥ ask_combined in parallel — reuses existing functions)
  3. unify(...)            → one Thai answer from quant structured + qual narrative (LLM)

Year correctness is guaranteed at the DAX layer (dax_generator._be_year_to_ce, −543);
the plan step only helps the fuzzy/spelled cases.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from .quant_provenance import build_advisory, format_advisory  # noqa: F401 (re-export convenience)
from .synthesizer import _format_quant_rows

logger = logging.getLogger(__name__)

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY  = os.getenv("AZURE_OPENAI_API_KEY", "")
PLAN_DEPLOYMENT       = os.getenv("PLAN_DEPLOYMENT",  os.getenv("SYNTH_DEPLOYMENT", "gpt-5.4"))
UNIFY_DEPLOYMENT      = os.getenv("UNIFY_DEPLOYMENT", os.getenv("SYNTH_DEPLOYMENT", "gpt-5.4"))
API_VERSION           = os.getenv("SYNTH_API_VERSION", "2024-12-01-preview")


def _chat(deployment: str, system: str, user: str, max_tokens: int, timeout: int) -> str:
    """One guarded Azure OpenAI chat call. Returns content or '' on any failure."""
    url = (f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
           f"{deployment}/chat/completions?api-version={API_VERSION}")
    body = {
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_completion_tokens": max_tokens,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "api-key": AZURE_OPENAI_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:
        logger.warning("orchestrator chat failed (%s): %s", deployment, exc)
        return ""


# ── 1. PLAN ───────────────────────────────────────────────────────────────────
_PLAN_SYSTEM = """You split a Thai CRM question into two tool sub-questions.
For EACH tool, output the user's question VERBATIM plus a short tool-specific narrative.

- quant_q = the user's question verbatim + "\\n\\nQuant narrative: <the NUMERIC ask only:
  which figure/aggregate/ranking, which period, which grain — ONE simple computable question>".
- qual_q  = the user's question verbatim + "\\n\\nQual narrative: <the NARRATIVE/context ask
  only: which accounts/deals/history/why>".

Normalize an ambiguous term in the narrative ONLY IF it appears (most need none):
- Buddhist-era year (เต็ม "2569" / ย่อ "69" / พูด "หกเก้า") → state it as an explicit
  Gregorian number in the narrative (2569 = ค.ศ. 2026). Give the converted number only.
- "รีไซเคิลบิน"/"recycle bin" → dormant/lost deals to revive (NOT the recycling industry).

Keep the VERBATIM part EXACTLY as the user typed. Reply ONLY with JSON:
{"quant_q": "...", "qual_q": "..."}"""


def plan_subquestions(q: str) -> tuple[str, str]:
    """LLM: user question → (quant_q, qual_q), each = verbatim + tool narrative.

    Falls back to (q, q) verbatim on any failure so the pipeline still runs.
    """
    raw = _chat(PLAN_DEPLOYMENT, _PLAN_SYSTEM, f"คำถาม: {q}", max_tokens=600, timeout=25)
    try:
        obj = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        quant_q = (obj.get("quant_q") or "").strip() or q
        qual_q  = (obj.get("qual_q") or "").strip() or q
        return quant_q, qual_q
    except Exception as exc:
        logger.warning("plan_subquestions parse failed: %s | raw=%r", exc, raw[:200])
        return q, q


# ── 3. UNIFY ──────────────────────────────────────────────────────────────────
# Ported from the Foundry agent instruction (tool-dispatch steps dropped; the
# answer-shaping rules kept). This is the single voice that composes the final answer.
_UNIFY_SYSTEM = """You are a senior CRM analyst reporting to a C-level executive. Speak with ONE voice.
Compose ONE Thai answer to the user's ORIGINAL question from the QUANT data and the QUAL narrative below.

## Answer policy
- The QUANT numbers are AUTHORITATIVE for any figure (actual/target/gap/pipeline/count/dates).
- The QUAL narrative is AUTHORITATIVE for account names, history, intent.
- Preserve every QUANT figure EXACTLY — never recompute or re-round.
- Present QUANT numbers FIRST, then the narrative. Multi-metric data → markdown table at the top;
  single-value ranking → numbered list.
- If QUANT is empty but QUAL has data → answer from QUAL (do NOT say "no data"). If BOTH empty →
  "ยังไม่พบข้อมูลในระบบ CRM สำหรับคำถามนี้ — กรุณาระบุช่วงเวลาหรือเงื่อนไขเพิ่มเติม".
- Append the QUANT_ADVISORY block VERBATIM at the very end — never paraphrase, drop, or duplicate it.
- Finance not in scope: profit/margin/cost/recognized-revenue are NOT available — if asked, say
  "ระบบนี้ไม่มีข้อมูลกำไร/ต้นทุน"; never fabricate.

## Column rendering / Terminology
- NEVER show raw [bracketed] column names. Render clean Thai labels: [Actual]→"ทำได้"/"ยอดขายจริง",
  [Target]→"เป้า", [Gap]→"ส่วนต่าง" (ติดลบ=ต่ำกว่าเป้า), [Pipeline Coverage]→"pipeline coverage (เท่า)",
  [Open Pipeline]/[SO Plan]→"pipeline เปิด / SO Plan". Strip every [ ] from figures/labels.
- ทำได้/ยอดขายจริง = realized SO actual (certified). เป้า = monthly target per person. ส่วนต่าง = actual − target.
- Cite accounts by COMPANY NAME, never a bare account_id GUID. If a top account has name_source
  "heuristic", prefix it with ~ and footnote "ชื่อที่นำหน้าด้วย ~ คือชื่อโดยประมาณ".

## Style
- ภาษาไทยหลัก; English เฉพาะชื่อคน/บริษัท/business terms. Numbers as `1,234,567.89 บาท`.
- Headline first (2–3 บรรทัด), then the table/data, then a brief insight if warranted.
- No filler. Never reveal that tools/agents/DAX/Power BI/models were used. Never split into
  "เชิงปริมาณ/เชิงคุณภาพ" sections."""


def unify(
    q: str,
    quant_rows: list[dict[str, Any]] | None,
    quant_dax: str | None,
    quant_citation: dict[str, Any] | None,
    qual_answer: str | None,
    advisory: str | None,
    temperature: float = 0.1,
) -> str:
    """LLM: compose the final Thai answer from quant structured + qual narrative."""
    parts = [f"คำถามเดิมของผู้ใช้: {q}\n"]

    if quant_rows:
        parts.append("QUANT_DATA:\n" + _format_quant_rows(quant_rows, quant_dax) + "\n")
        cit = quant_citation or {}
        if cit.get("top_accounts"):
            names = ", ".join(
                (f"~{a['name']}" if a.get("name_source") == "heuristic" else str(a.get("name")))
                for a in cit["top_accounts"] if a.get("name")
            )
            if names:
                parts.append(f"QUANT_TOP_ACCOUNTS: {names}\n")
        if cit.get("value") is not None:
            parts.append(f"QUANT_VALUE: {cit['value']} (measure: {cit.get('measure')})\n")
    else:
        parts.append("QUANT_DATA: (empty)\n")

    parts.append("QUAL_NARRATIVE:\n" + (qual_answer or "(empty)") + "\n")
    parts.append("QUANT_ADVISORY (append verbatim at the very end):\n" + (advisory or "(none)") + "\n")
    parts.append("ตอบเป็นภาษาไทย กระชับ ตามกฎด้านบน:")

    answer = _chat(UNIFY_DEPLOYMENT, _UNIFY_SYSTEM, "\n".join(parts),
                   max_tokens=2000, timeout=60)
    if not answer:
        answer = "ขออภัย ระบบไม่สามารถเรียบเรียงคำตอบได้ในขณะนี้"
    # Belt-and-suspenders: ensure the advisory is present at the end.
    if advisory and advisory not in answer:
        answer = f"{answer}\n\n{advisory}"
    return answer
