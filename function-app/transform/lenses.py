"""
Cross-cutting qualitative lenses over the account-centric records.

The account markdown (account/{id}.md) scatters solution / salesperson / industry
context across thousands of files. These lenses RE-GROUP the SAME aggregated data
into coherent per-solution / per-salesperson / per-industry documents so the RAG
retriever can answer "which customers use MES?", "what is Alex working on?",
"profile our manufacturing segment" from a single chunk.

Scope = QUALITATIVE context only (names, status, descriptions, recent activity).
Hard numbers / aggregations stay the quant agent's job — we do not compute money
totals here (avoids drifting from the certified measures).

All three functions consume the output of
``aggregator.aggregate_account_centric`` (no extra extraction).
"""

from __future__ import annotations

import re
from collections import defaultdict

from transform.aggregator import _strip_html

# Cap recent activities listed per salesperson — keeps the doc bounded.
_MAX_SP_ACTIVITIES = 50


def slugify(name: str) -> str:
    """File-safe blob key. Keeps unicode word chars (Thai/JP names stay distinct)."""
    s = re.sub(r"[^\w\-]+", "-", str(name).strip().lower(), flags=re.UNICODE)
    return re.sub(r"-+", "-", s).strip("-") or "unknown"


def aggregate_by_solution(records: list[dict]) -> list[dict]:
    """One doc per Solution Name → the deals (and their qualitative context)."""
    buckets: dict[str, list] = defaultdict(list)
    for rec in records:
        acct = rec["account"]
        for opp in rec["opportunities"]:
            sol = (opp.get("Solution Name") or "").strip()
            if not sol:
                continue
            buckets[sol].append({
                "account_name": acct.get("Company Name", ""),
                "opp_name": opp.get("Opportunity Name", ""),
                "status": opp.get("Status", ""),
                "possibility": opp.get("Possibility", ""),
                "est_close": opp.get("Est. Close Date", ""),
                "sale_person": opp.get("Sale Person Name", ""),
                "description": _strip_html(opp.get("Description", "")),
                "detail_reasons": _strip_html(opp.get("Detail Reasons", "")),
            })
    out = []
    for sol, deals in sorted(buckets.items()):
        accounts = {d["account_name"] for d in deals if d["account_name"]}
        out.append({
            "key": slugify(sol),
            "solution": sol,
            "deal_count": len(deals),
            "account_count": len(accounts),
            "deals": deals,
        })
    return out


def aggregate_by_salesperson(records: list[dict]) -> list[dict]:
    """One doc per Sale Person Name → their deals, accounts, recent activity."""
    opp_owner: dict[str, str] = {}
    deals: dict[str, list] = defaultdict(list)
    accounts: dict[str, set] = defaultdict(set)

    for rec in records:
        acct_name = rec["account"].get("Company Name", "")
        for opp in rec["opportunities"]:
            sp = (opp.get("Sale Person Name") or "").strip()
            if not sp:
                continue
            opp_owner[opp.get("Opportunity ID", "")] = sp
            accounts[sp].add(acct_name)
            deals[sp].append({
                "account_name": acct_name,
                "opp_name": opp.get("Opportunity Name", ""),
                "status": opp.get("Status", ""),
                "solution": opp.get("Solution Name", ""),
                "est_close": opp.get("Est. Close Date", ""),
                "description": _strip_html(opp.get("Description", "")),
            })

    activities: dict[str, list] = defaultdict(list)
    for rec in records:
        for a in rec["activities"]:
            sp = opp_owner.get(a.get("Opportunity ID", ""))
            if not sp:
                continue
            activities[sp].append({
                "date": a.get("Activity Date", ""),
                "subject": a.get("Subject", ""),
                "type": a.get("Activity Type", ""),
                "state": a.get("State Name", ""),
            })

    out = []
    for sp in sorted(deals):
        acts = sorted(activities.get(sp, []), key=lambda x: x["date"], reverse=True)
        out.append({
            "key": slugify(sp),
            "salesperson": sp,
            "account_count": len(accounts[sp]),
            "deal_count": len(deals[sp]),
            "deals": deals[sp],
            "activities": acts[:_MAX_SP_ACTIVITIES],
        })
    return out


def aggregate_by_industry(records: list[dict]) -> list[dict]:
    """One doc per Industry (L1) → the accounts in that segment + their solutions."""
    buckets: dict[str, list] = defaultdict(list)
    for rec in records:
        acct = rec["account"]
        ind = (acct.get("Industry") or "").strip()
        if not ind:
            continue
        solutions = sorted({
            o.get("Solution Name", "") for o in rec["opportunities"]
            if (o.get("Solution Name") or "").strip()
        })
        location = ", ".join(
            x for x in [acct.get("City", ""), acct.get("Province", "")] if x
        )
        buckets[ind].append({
            "account_name": acct.get("Company Name", ""),
            "customer_type": acct.get("Customer Type", ""),
            "status": acct.get("Account Status", ""),
            "location": location,
            "opp_count": len(rec["opportunities"]),
            "solutions": solutions,
        })
    out = []
    for ind, accts in sorted(buckets.items()):
        out.append({
            "key": slugify(ind),
            "industry": ind,
            "account_count": len(accts),
            "accounts": accts,
        })
    return out
