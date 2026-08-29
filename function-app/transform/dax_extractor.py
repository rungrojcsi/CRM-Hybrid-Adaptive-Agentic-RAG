"""
DAX extractor — sources account-centric entities from the LIVE Power BI
semantic model (SALES DATA MODEL) via executeQueries, reproducing the exact
output contract of ``cdm_parser.load_entities_blob``::

    dict[str, pd.DataFrame]   keyed by aggregator.REQUIRED_ENTITIES,
    columns = CDM friendly names (e.g. "Account ID"), every cell a str.

This replaces the dead crmdev → bronze CDM-CSV path (frozen 2026-05-29).
Everything downstream (aggregator → renderer → embedder → pgvector / AI Search)
is UNCHANGED — the reshaping to the old contract happens entirely in here.

The live model is shaped differently from the old CDM, so per entity we:
  * project + RENAME model columns to the friendly names the aggregator and
    ``templates/account.md.j2`` expect (``ENTITY_SPECS``), and
  * DERIVE the account linkage the model lacks — model ``Fact_Activity`` and
    ``Dim_Annotation`` attach to an Opportunity only, so we map
    ``Opportunity ID → Account ID`` (from ``Fact_Opportunity``) and synthesize
    the ``Regarding ID`` / ``Ref Object ID`` + ``Object Type Code`` columns the
    aggregator filters on (see ``_derive_account_links``).
"""

from __future__ import annotations

import logging

import pandas as pd

from transform import pbi_client

logger = logging.getLogger(__name__)

PAGE_SIZE = 30_000

# friendly (CDM / aggregator / template) name  ->  live-model column name.
# Column order is preserved into the SELECTCOLUMNS projection.
ENTITY_SPECS: dict[str, dict] = {
    "Dim_Account": {
        "table": "Dim_Account",
        "columns": {
            "Account ID": "Account ID",
            "Company Name": "Account Name",
            # Model "Industry" is 0% populated; "Industry L1" carries the real
            # industry taxonomy (~60% filled).
            "Industry": "Industry L1",
            "Account Status": "Status",
            "Customer Code": "Account Number",
            "Customer Type": "Customer Type",
            "City": "City",
            "Province": "Province",
            "Country": "Country",
        },
        # Not in the model (template renders "—"): Annual Revenue, No. of
        # Employees, Website.
    },
    "Dim_Contact": {
        "table": "Dim_Contact",
        "columns": {
            "Contact ID": "Contact ID",
            "Account ID": "Account ID",
            "Contact Name": "Contact Name",
            "Job Title": "Job Title",
            "Email": "Email",
            "Mobile Phone": "Mobile Phone",
        },
    },
    "Fact_Opportunity": {
        "table": "Fact_Opportunity",
        "columns": {
            "Opportunity ID": "Opportunity ID",
            "Account ID": "Account ID",
            "Opportunity Name": "Opportunity Name",
            "Status": "Status",
            "Possibility": "Possibility",
            "Est. Close Date": "Est. Close Date",
            "Total SO Plan Amount": "SO Plan Amount",
            "Solution Name": "Solution Name",
            "Sale Person Name": "Sale Person Name",
            "Description": "Description",
            "Detail Reasons": "Detail Reasons",
        },
    },
    "Fact_Activity": {
        "table": "Fact_Activity",
        "columns": {
            "Activity ID": "Activity ID",
            "Opportunity ID": "Opportunity ID",
            "Activity Date": "Activity Date",
            "Subject": "Subject",
            "Activity Type": "Activity Type",
            "State Name": "State",
        },
        # ~96k rows, near the executeQueries row cap → page it. Only activities
        # linked to an opportunity can be tied back to an account, so filter the
        # rest out at source (cuts volume, loses nothing under our derivation).
        "paged": True,
        "key": "Activity ID",
        "filter": "NOT ISBLANK(Fact_Activity[Opportunity ID])",
    },
    "Dim_Connection": {
        "table": "Dim_Connection",
        "columns": {
            "Opportunity ID": "Opportunity ID",
            "Stakeholder Role Name": "Stakeholder Role Name",
        },
    },
    "Dim_Review": {
        "table": "Dim_Review",
        "columns": {
            "Opportunity ID": "Opportunity ID",
        },
    },
    "Dim_OpportunityClose": {
        "table": "Dim_OpportunityClose",
        "columns": {
            "Opportunity ID": "Opportunity ID",
            "Close Detail Reason": "Close Detail Reason",
            "Actual Close Date": "Actual Close Date",
        },
    },
    "Dim_Annotation": {
        "table": "Dim_Annotation",
        "columns": {
            "Note ID": "Note ID",
            "Note Title": "Subject",
            "Note Body": "Note Text",
            "Created Date": "Created On",
            "Opportunity ID": "Opportunity ID",
        },
    },
}


def _strip_brackets(key: str) -> str:
    """executeQueries returns SELECTCOLUMNS aliases as ``[Friendly Name]``."""
    if key.startswith("[") and key.endswith("]"):
        return key[1:-1]
    return key


def _norm_cell(value) -> str:
    """Coerce a typed executeQueries value to the str the CDM parser produced."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def _rows_to_df(rows: list[dict], columns: dict) -> pd.DataFrame:
    """Map executeQueries rows → DataFrame with friendly names, all str.

    Guarantees every requested friendly column exists (missing → "") and that
    column order matches the spec, so downstream code sees the CDM contract.
    """
    friendly = list(columns)
    norm = [{_strip_brackets(k): _norm_cell(v) for k, v in row.items()} for row in rows]
    df = pd.DataFrame(norm)
    for col in friendly:
        if col not in df.columns:
            df[col] = ""
    return df.reindex(columns=friendly)


def _build_selectcolumns(table: str, columns: dict, filter_expr: str | None = None) -> str:
    source = f"FILTER({table}, {filter_expr})" if filter_expr else table
    pairs = ",\n  ".join(
        f'"{friendly}", {table}[{model}]' for friendly, model in columns.items()
    )
    return f"SELECTCOLUMNS(\n  {source},\n  {pairs}\n)"


def execute_dax(dax: str, dataset_id: str | None = None) -> list[dict]:
    """Run a DAX query against the semantic model, return raw rows."""
    return pbi_client.execute_queries(dax, dataset_id)["rows"]


def _eval_table(spec: dict, dataset_id: str | None) -> pd.DataFrame:
    dax = "EVALUATE " + _build_selectcolumns(
        spec["table"], spec["columns"], spec.get("filter")
    )
    return _rows_to_df(execute_dax(dax, dataset_id), spec["columns"])


def _eval_table_paged(
    spec: dict, dataset_id: str | None, page_size: int = PAGE_SIZE
) -> pd.DataFrame:
    """Page a large table via TOPNSKIP ordered by its key column."""
    sel = _build_selectcolumns(spec["table"], spec["columns"], spec.get("filter"))
    key = spec["key"]
    frames: list[pd.DataFrame] = []
    skip = 0
    while True:
        dax = f"EVALUATE TOPNSKIP({page_size}, {skip}, {sel}, [{key}], ASC)"
        df = _rows_to_df(execute_dax(dax, dataset_id), spec["columns"])
        if df.empty:
            break
        frames.append(df)
        if len(df) < page_size:
            break
        skip += page_size
    if not frames:
        return pd.DataFrame(columns=list(spec["columns"]))
    return pd.concat(frames, ignore_index=True)


def _derive_account_links(entities: dict[str, pd.DataFrame]) -> None:
    """Synthesize the account-level FK columns the live model omits.

    Model ``Fact_Activity`` / ``Dim_Annotation`` link to an Opportunity only;
    map ``Opportunity ID → Account ID`` so the aggregator (which filters on
    ``Regarding ID`` / ``Ref Object ID`` == Account ID) attaches them to the
    owning account. Rows with no resolvable opportunity get "" and fall away.
    """
    opp = entities.get("Fact_Opportunity")
    if opp is None or opp.empty:
        opp2acct: dict[str, str] = {}
    else:
        opp2acct = dict(zip(opp["Opportunity ID"], opp["Account ID"]))

    act = entities.get("Fact_Activity")
    if act is not None and not act.empty and "Opportunity ID" in act.columns:
        act["Regarding ID"] = act["Opportunity ID"].map(lambda o: opp2acct.get(o, ""))

    ann = entities.get("Dim_Annotation")
    if ann is not None and not ann.empty and "Opportunity ID" in ann.columns:
        ann["Ref Object ID"] = ann["Opportunity ID"].map(lambda o: opp2acct.get(o, ""))
        # Aggregator pre-filters account notes on Object Type Code == "1".
        ann["Object Type Code"] = "1"


def extract_entities_dax(
    dataset_id: str | None = None,
    entity_filter: list[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """Drop-in replacement for ``cdm_parser.load_entities_blob``.

    Returns the account-centric entity DataFrames sourced live from the PBI
    semantic model, in the exact shape ``aggregate_account_centric`` consumes.
    """
    names = list(ENTITY_SPECS)
    if entity_filter:
        names = [n for n in names if n in entity_filter]

    entities: dict[str, pd.DataFrame] = {}
    for name in names:
        spec = ENTITY_SPECS[name]
        if spec.get("paged"):
            entities[name] = _eval_table_paged(spec, dataset_id)
        else:
            entities[name] = _eval_table(spec, dataset_id)
        logger.info("DAX extracted %s: %d rows", name, len(entities[name]))

    _derive_account_links(entities)
    return entities
