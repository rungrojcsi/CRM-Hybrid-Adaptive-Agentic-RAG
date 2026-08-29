"""
account_resolver.py — Map Account ID (GUID) → Company Name.

Lookup is built from silver-md container by scripts/build_account_lookup.py
and bundled at deploy time as transform/account_lookup.json.

Lookup is case-insensitive: keys are uppercased GUIDs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_DIM_PATH = _HERE / "dim_account.json"                     # bronze CDM (complete, structured)
_PRIMARY_PATH = _HERE / "account_lookup.json"              # silver-md (legacy, name-only)
_HEURISTIC_PATH = _HERE / "account_lookup_heuristic.json"  # opp-name prefix guess

_dim: dict[str, dict] | None = None
_primary: dict[str, str] | None = None
_heuristic: dict[str, str] | None = None


def _load_json(path: Path, default):
    if not path.exists():
        logger.warning("account lookup not found at %s — partial resolution", path)
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.exception("Failed to load %s: %s", path, exc)
        return default


def _dim_lookup() -> dict[str, dict]:
    global _dim
    if _dim is None:
        _dim = _load_json(_DIM_PATH, {})
        logger.info("Loaded dim_account: %d entries", len(_dim))
    return _dim


def _primary_lookup() -> dict[str, str]:
    global _primary
    if _primary is None:
        _primary = _load_json(_PRIMARY_PATH, {})
        logger.info("Loaded silver-md lookup: %d entries", len(_primary))
    return _primary


def _heuristic_lookup() -> dict[str, str]:
    global _heuristic
    if _heuristic is None:
        _heuristic = _load_json(_HEURISTIC_PATH, {})
        logger.info("Loaded heuristic lookup: %d entries", len(_heuristic))
    return _heuristic


def resolve(account_id: str | None) -> str | None:
    """GUID → company name via chain: dim_account (bronze) → silver-md → heuristic."""
    if not account_id:
        return None
    key = account_id.upper()
    rec = _dim_lookup().get(key)
    if rec and rec.get("name"):
        return rec["name"]
    hit = _primary_lookup().get(key)
    if hit:
        return hit
    return _heuristic_lookup().get(key)


def resolve_with_source(account_id: str | None) -> tuple[str | None, str | None]:
    """Return (name, source) where source ∈ {'dim', 'primary', 'heuristic', None}."""
    if not account_id:
        return None, None
    key = account_id.upper()
    rec = _dim_lookup().get(key)
    if rec and rec.get("name"):
        return rec["name"], "dim"
    hit = _primary_lookup().get(key)
    if hit:
        return hit, "primary"
    hit = _heuristic_lookup().get(key)
    if hit:
        return hit, "heuristic"
    return None, None


def resolve_full(account_id: str | None) -> dict | None:
    """Return full account record {name, industry, country, city, customer_type,
    owner_name, status, customer_code, ...} or None.
    """
    if not account_id:
        return None
    return _dim_lookup().get(account_id.upper())


def resolve_or_id(account_id: str | None) -> str:
    """Return company name if found, else the original GUID."""
    return resolve(account_id) or (account_id or "")


def stats() -> dict:
    return {
        "dim": len(_dim_lookup()),
        "primary": len(_primary_lookup()),
        "heuristic": len(_heuristic_lookup()),
    }
