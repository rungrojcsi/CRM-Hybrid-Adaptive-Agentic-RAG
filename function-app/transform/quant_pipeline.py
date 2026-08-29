"""
quant_pipeline.py — Shared Quant pipeline: generate DAX → execute → retry-on-empty.

Used by both `/api/ask-quant` and `/api/ask-hybrid` to remove duplication and
centralise retry + telemetry.
"""

from __future__ import annotations

import logging
from typing import Any

from .dax_generator import (
    extract_account_citation,
    generate_dax,
)
from .pbi_client import execute_queries as pbi_execute_queries

logger = logging.getLogger(__name__)


def run_with_retry(question: str) -> dict[str, Any]:
    """Generate DAX, execute against PBI — single attempt, no synchronous retry.

    Retry was removed from the critical path: the second generate_dax + PBI round
    doubled the ask-quant latency and pushed the tool past the Foundry ~30s sync
    window, surfacing as "Sorry, something went wrong" even when the tool would
    eventually return 200. A clean empty/error result now returns as-is so the
    orchestrator can rephrase. Keys "retried"/"attempts" are kept for the response
    contract (function_app reads them).

    Returns:
        {
          "dax":       DAX,
          "rows":      list[dict],
          "error":     str | None,
          "retried":   bool,   # always False now
          "attempts":  int,    # always 1 now
          "citation":  {top_accounts, is_aggregate_only, measure, value},
        }
    """
    error: str | None = None
    rows: list[dict] = []

    dax = generate_dax(question)
    try:
        rows = pbi_execute_queries(dax)["rows"]
    except Exception as exc:
        logger.warning("PBI executeQueries failed: %s", exc)
        error = str(exc)[:500]

    citation = extract_account_citation(rows, question=question)
    return {
        "dax":      dax,
        "rows":     rows,
        "error":    error,
        "retried":  False,
        "attempts": 1,
        "citation": citation,
    }
