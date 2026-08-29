"""
CDM (Common Data Model) parser — reads model.json + CSV files from Azure Blob Storage.

CDM path layout (Power BI dataflow output):
  bronze/CSI DATA PLATFORM/SALE DATA CLEANSING/model.json
  bronze/CSI DATA PLATFORM/SALE DATA CLEANSING/{Entity}/{Entity}/part-*.csv
"""

from __future__ import annotations

import json
import io
import logging
from pathlib import Path

import pandas as pd
from azure.storage.blob import ContainerClient

logger = logging.getLogger(__name__)

CDM_ROOT = "CSI DATA PLATFORM/SALE DATA CLEANSING"


def load_model(container_client: ContainerClient) -> dict:
    """Download and parse model.json from bronze container."""
    blob = container_client.get_blob_client(f"{CDM_ROOT}/model.json")
    return json.loads(blob.download_blob().readall())


def load_model_local(model_path: str | Path) -> dict:
    """Load model.json from local filesystem (for tests)."""
    with open(model_path, encoding="utf-8") as f:
        return json.load(f)


def entity_names(model: dict) -> list[str]:
    return [e["name"] for e in model.get("entities", [])]


def entity_attributes(model: dict, entity_name: str) -> list[str]:
    """Return ordered list of column names for an entity."""
    for e in model.get("entities", []):
        if e["name"] == entity_name:
            return [a["name"] for a in e.get("attributes", [])]
    raise ValueError(f"Entity '{entity_name}' not found in model")


def _read_csv_parts_blob(
    container_client: ContainerClient, entity_name: str, columns: list[str]
) -> pd.DataFrame:
    """Read all part-*.csv blobs for an entity and concat into one DataFrame.

    CDM CSV files have NO header row — column names come from model.json.

    Handles two naming patterns from Power BI dataflow CDM output:
      1. Direct:   {entity}/{entity}/part-*.csv
      2. Snapshot: {entity}/{entity}/part-*.csv.snapshots/part-*.csv@snapshot=<ts>
    """
    prefix = f"{CDM_ROOT}/{entity_name}/{entity_name}/"
    parts = []
    for blob_item in container_client.list_blobs(name_starts_with=prefix):
        name = blob_item.name
        if "/part-" not in name:
            continue
        if not (name.endswith(".csv") or ".csv@snapshot=" in name):
            continue
        try:
            data = container_client.get_blob_client(name).download_blob().readall()
            df = pd.read_csv(
                io.BytesIO(data), header=None, names=columns,
                dtype=str, keep_default_na=False,
            )
            parts.append(df)
        except Exception:
            logger.warning("Failed to read blob %s", name, exc_info=True)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def _read_csv_parts_local(
    fixtures_dir: str | Path, entity_name: str, columns: list[str] | None = None
) -> pd.DataFrame:
    """Read local fixture CSVs for an entity (tests).

    If columns provided: treat CSV as headerless (CDM style).
    If columns is None: use first row as header (synthetic fixtures with headers).
    """
    base = Path(fixtures_dir) / entity_name
    parts = sorted(base.glob("part-*.csv"))
    if not parts:
        return pd.DataFrame()
    kwargs: dict = {"dtype": str, "keep_default_na": False}
    if columns is not None:
        kwargs.update({"header": None, "names": columns})
    return pd.concat(
        [pd.read_csv(p, **kwargs) for p in parts],
        ignore_index=True,
    )


def load_entities_blob(
    container_client: ContainerClient,
    model: dict,
    entity_names_filter: list[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """Load all (or filtered) entities from blob storage."""
    names = entity_names(model)
    if entity_names_filter:
        names = [n for n in names if n in entity_names_filter]
    result = {}
    for name in names:
        cols = entity_attributes(model, name)
        logger.info("Loading entity: %s (%d columns)", name, len(cols))
        result[name] = _read_csv_parts_blob(container_client, name, cols)
    return result


def load_entities_local(
    fixtures_dir: str | Path,
    model: dict,
    entity_names_filter: list[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """Load all (or filtered) entities from local fixtures directory (tests)."""
    names = entity_names(model)
    if entity_names_filter:
        names = [n for n in names if n in entity_names_filter]
    result = {}
    for name in names:
        result[name] = _read_csv_parts_local(fixtures_dir, name)
    return result
