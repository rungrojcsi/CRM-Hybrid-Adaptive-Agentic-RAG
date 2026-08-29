"""
pbi_client.py — Power BI REST executeQueries client.

Executes DAX queries against a Power BI Semantic Model via the REST API.

Auth: Delegated user OR service account (refresh_token grant)
  - MVP: cached refresh_token in Key Vault (env REFRESH_TOKEN)
  - Prod: service account user with PPU + refresh_token

Env vars:
  PBI_WORKSPACE_ID     — Fabric/PBI workspace GUID (default: SALES_DATA)
  PBI_DATASET_ID       — Semantic Model GUID (default: SALES DATA MODEL)
  PBI_TENANT_ID        — Entra tenant GUID
  PBI_CLIENT_ID        — Entra app registration client_id (public/delegated)
  PBI_REFRESH_TOKEN    — long-lived refresh token (delegated user, ~90 day TTL)
  PBI_API_VERSION      — default v1.0
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Defaults track production: SALES DATA MODEL in workspace SALES_DATA
PBI_WORKSPACE_ID  = os.getenv("PBI_WORKSPACE_ID",  "00000000-0000-0000-0000-000000000000")
PBI_DATASET_ID    = os.getenv("PBI_DATASET_ID",    "00000000-0000-0000-0000-000000000000")
PBI_TENANT_ID     = os.getenv("PBI_TENANT_ID",     "00000000-0000-0000-0000-000000000000")
PBI_CLIENT_ID     = os.getenv("PBI_CLIENT_ID",     "")
PBI_CLIENT_SECRET = os.getenv("PBI_CLIENT_SECRET", "")  # SP auth (preferred — no rotation)
PBI_REFRESH_TOKEN = os.getenv("PBI_REFRESH_TOKEN", "")
PBI_ACCESS_TOKEN  = os.getenv("PBI_ACCESS_TOKEN",  "")  # User delegated (1-hr TTL fallback)
PBI_API_VERSION   = os.getenv("PBI_API_VERSION",   "v1.0")
PBI_SCOPE_USER    = "https://analysis.windows.net/powerbi/api/.default offline_access"
PBI_SCOPE_APP     = "https://analysis.windows.net/powerbi/api/.default"


# In-process token cache: (token, exp_epoch). Refresh ~5min before expiry.
_TOKEN_CACHE: dict[str, Any] = {"token": None, "exp": 0.0}
_REFRESH_SKEW_SEC = 300


def _jwt_exp(token: str) -> float:
    """Extract `exp` claim from JWT without signature verification.

    Returns 0.0 if token is not a parseable JWT (lets caller treat as
    short-lived and refresh).
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload.get("exp", 0))
    except Exception:
        return 0.0


def _sp_client_credentials() -> tuple[str, float]:
    """Get app token via client_credentials (SP auth). No user, no refresh, no CA."""
    token_url = f"https://login.microsoftonline.com/{PBI_TENANT_ID}/oauth2/v2.0/token"
    body = urllib.parse.urlencode({
        "client_id":     PBI_CLIENT_ID,
        "client_secret": PBI_CLIENT_SECRET,
        "grant_type":    "client_credentials",
        "scope":         PBI_SCOPE_APP,
    }).encode()
    req = urllib.request.Request(
        token_url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    tok = data["access_token"]
    exp = _jwt_exp(tok) or (time.time() + float(data.get("expires_in", 3600)))
    return tok, exp


def _refresh_via_grant() -> tuple[str, float]:
    """Exchange refresh_token for a fresh access_token. Returns (token, exp_epoch)."""
    if not PBI_REFRESH_TOKEN or not PBI_CLIENT_ID:
        raise RuntimeError(
            "PBI_ACCESS_TOKEN expired and no PBI_REFRESH_TOKEN + PBI_CLIENT_ID configured for refresh"
        )
    token_url = f"https://login.microsoftonline.com/{PBI_TENANT_ID}/oauth2/v2.0/token"
    body = urllib.parse.urlencode({
        "client_id":     PBI_CLIENT_ID,
        "grant_type":    "refresh_token",
        "refresh_token": PBI_REFRESH_TOKEN,
        "scope":         PBI_SCOPE_USER,
    }).encode()
    req = urllib.request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    tok = data["access_token"]
    # Prefer JWT exp, fall back to expires_in
    exp = _jwt_exp(tok) or (time.time() + float(data.get("expires_in", 3600)))
    return tok, exp


def _get_access_token() -> str:
    """
    Get access token for PBI REST with caching + automatic refresh.

    Priority (preferred → fallback):
      1. Cached token still valid (refresh ~5min before exp)
      2. PBI_CLIENT_SECRET set → SP client_credentials (NO USER, NO ROTATION)
         — best path: app-to-app auth, secret valid 2yr, auto-refresh per call
      3. PBI_ACCESS_TOKEN env still valid by JWT exp (user delegated fallback)
      4. PBI_REFRESH_TOKEN grant (user delegated, needs refresh_token)
    """
    now = time.time()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["exp"] - _REFRESH_SKEW_SEC:
        return _TOKEN_CACHE["token"]

    # Preferred: Service Principal (no user, no CA, no rotation)
    if PBI_CLIENT_SECRET and PBI_CLIENT_ID:
        try:
            token, exp = _sp_client_credentials()
            _TOKEN_CACHE["token"] = token
            _TOKEN_CACHE["exp"] = exp
            logger.info("Got PBI token via SP client_credentials")
            return token
        except Exception as exc:
            logger.warning("SP client_credentials failed (%s) — falling back", exc)

    # Fallback: user delegated direct token
    if PBI_ACCESS_TOKEN:
        exp = _jwt_exp(PBI_ACCESS_TOKEN)
        if exp and now < exp - _REFRESH_SKEW_SEC:
            _TOKEN_CACHE["token"] = PBI_ACCESS_TOKEN
            _TOKEN_CACHE["exp"] = exp
            return PBI_ACCESS_TOKEN
        logger.info("PBI_ACCESS_TOKEN expired — attempting refresh grant")

    # Last resort: refresh_token grant
    token, exp = _refresh_via_grant()
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["exp"] = exp
    return token


def execute_queries(dax: str, dataset_id: str | None = None) -> dict[str, Any]:
    """
    Execute DAX query via PBI REST. Returns {"rows": [...], "raw": <api response>}.

    Raises on HTTP error (caller handles).
    """
    ds = dataset_id or PBI_DATASET_ID
    token = _get_access_token()
    url = (
        f"https://api.powerbi.com/{PBI_API_VERSION}/myorg/datasets/{ds}/executeQueries"
    )
    body = {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:1000]
        logger.error("PBI executeQueries %d: %s | DAX preview: %s",
                     exc.code, err_body, dax[:200])
        raise RuntimeError(f"PBI HTTP {exc.code}: {err_body}") from exc

    # API returns: {"results":[{"tables":[{"rows":[{...},{...}]}]}]}
    try:
        rows = data["results"][0]["tables"][0]["rows"]
    except (KeyError, IndexError):
        rows = []
    return {"rows": rows, "raw": data}
