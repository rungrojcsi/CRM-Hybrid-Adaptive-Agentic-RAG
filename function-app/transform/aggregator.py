"""
Account-centric aggregator.

FK column names verified from model.json (Power BI dataflow CDM):
  Dim_Contact["Account ID"]        → Dim_Account["Account ID"]
  Fact_Opportunity["Account ID"]   → Dim_Account["Account ID"]
  Fact_Activity["Regarding ID"]    → Dim_Account["Account ID"]  (polymorph: also links to Opportunity)
  Dim_Connection["Opportunity ID"] → Fact_Opportunity["Opportunity ID"]
  Dim_Review["Opportunity ID"]     → Fact_Opportunity["Opportunity ID"]
  Dim_OpportunityClose["Opportunity ID"] → Fact_Opportunity["Opportunity ID"]
  Dim_Annotation["Ref Object ID"]  (Object Type Code="1") → Dim_Account["Account ID"]
  Dim_Annotation["Ref Object ID"]  (Object Type Code="3") → Fact_Opportunity["Opportunity ID"]
"""

import html as _html_mod
import re

import pandas as pd


def _strip_html(text: str) -> str:
    """Strip HTML tags and unescape entities from CKEditor note bodies."""
    text = re.sub(r'<[^>]+>', ' ', str(text))
    text = _html_mod.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()

# Entities used in aggregation (load only these from CDM)
REQUIRED_ENTITIES = [
    "Dim_Account",
    "Dim_Contact",
    "Fact_Opportunity",
    "Fact_Activity",
    "Dim_Connection",
    "Dim_Review",
    "Dim_OpportunityClose",
    "Dim_Annotation",
]


def _safe(entities: dict[str, pd.DataFrame], name: str) -> pd.DataFrame:
    return entities.get(name, pd.DataFrame())


def _filter(df: pd.DataFrame, col: str, value: str) -> list[dict]:
    if df.empty or col not in df.columns:
        return []
    return df[df[col] == value].to_dict(orient="records")


def _enrich_opportunity(
    opp_row: dict,
    connections: pd.DataFrame,
    reviews: pd.DataFrame,
    closes: pd.DataFrame,
) -> dict:
    """Attach per-opportunity related rows."""
    opp_id = opp_row.get("Opportunity ID", "")
    return {
        **opp_row,
        "_connections": _filter(connections, "Opportunity ID", opp_id),
        "_reviews": _filter(reviews, "Opportunity ID", opp_id),
        "_close": _filter(closes, "Opportunity ID", opp_id),  # 0 or 1 rows
    }


def aggregate_account_centric(entities: dict[str, pd.DataFrame]) -> list[dict]:
    """
    Returns one record per Account with nested related rows.

    Output shape per record:
      {
        "account":      dict,          # Dim_Account row
        "contacts":     [dict, ...],   # Dim_Contact rows
        "opportunities":[dict, ...],   # Fact_Opportunity rows (with _connections/_reviews/_close)
        "activities":   [dict, ...],   # Fact_Activity rows (Regarding ID == Account ID)
      }
    """
    accounts = _safe(entities, "Dim_Account")
    contacts = _safe(entities, "Dim_Contact")
    opportunities = _safe(entities, "Fact_Opportunity")
    activities = _safe(entities, "Fact_Activity")
    connections = _safe(entities, "Dim_Connection")
    reviews = _safe(entities, "Dim_Review")
    closes = _safe(entities, "Dim_OpportunityClose")
    annotations = _safe(entities, "Dim_Annotation")

    # CDM snapshot partitions (2023/2024/2025/...) are all concat — dedupe by PK.
    # keep="last" preserves the most-recent partition's state for each record.
    if not contacts.empty and "Contact ID" in contacts.columns:
        contacts = contacts.drop_duplicates(subset=["Contact ID"], keep="last")
    if not opportunities.empty and "Opportunity ID" in opportunities.columns:
        opportunities = opportunities.drop_duplicates(subset=["Opportunity ID"], keep="last")
    if not activities.empty and "Activity ID" in activities.columns:
        activities = activities.drop_duplicates(subset=["Activity ID"], keep="last")
    if not annotations.empty and "Note ID" in annotations.columns:
        annotations = annotations.drop_duplicates(subset=["Note ID"], keep="last")

    # Pre-filter: only Account-linked notes (Object Type Code = "1")
    acct_annotations = pd.DataFrame()
    if not annotations.empty and "Object Type Code" in annotations.columns:
        acct_annotations = annotations[annotations["Object Type Code"] == "1"]

    if accounts.empty:
        return []

    records = []
    for _, account_row in accounts.iterrows():
        acct_id = account_row.get("Account ID", "")
        if not acct_id:
            continue

        acct_contacts = _filter(contacts, "Account ID", acct_id)
        acct_opps_raw = _filter(opportunities, "Account ID", acct_id)
        acct_opps = [
            _enrich_opportunity(o, connections, reviews, closes)
            for o in acct_opps_raw
        ]
        # Direct activities (Regarding = Account)
        acct_activities = _filter(activities, "Regarding ID", acct_id)

        # Notes attached to this Account (HTML stripped)
        raw_notes = _filter(acct_annotations, "Ref Object ID", acct_id)
        acct_notes = [
            {
                "title": n.get("Note Title", ""),
                "body": _strip_html(n.get("Note Body", "")),
                "date": n.get("Created Date", ""),
            }
            for n in raw_notes
            if n.get("Note Body", "").strip()  # skip empty bodies
        ]

        records.append({
            "account": account_row.to_dict(),
            "contacts": acct_contacts,
            "opportunities": acct_opps,
            "activities": acct_activities,
            "notes": acct_notes,
        })

    return records
