"""
ai_searcher.py — Hybrid vector + keyword search via Azure AI Search.

Replaces: searcher.py (pgvector) + embedder.py (manual embedding)

Search flow:
  1. Embed query text via Azure OpenAI (same deployment as indexer skillset)
  2. POST to AI Search /search with hybrid: vector + full-text + semantic ranking
  3. Return top-k chunks in same format as old searcher.search()

Env vars:
  AZURE_SEARCH_ENDPOINT      — e.g. https://<resource>.search.windows.net
  AZURE_SEARCH_KEY           — AI Search admin or query key
  AZURE_SEARCH_INDEX_NAME    — index name (default: crm-accounts)
  AZURE_OPENAI_ENDPOINT      — same as embedder (Azure AI Foundry or OpenAI)
  AZURE_OPENAI_API_KEY       — same as embedder
  AZURE_OPENAI_DEPLOYMENT    — embedding deployment (default: text-embedding-3-large)
  AZURE_OPENAI_API_VERSION   — API version (default: 2024-02-01)
  EMBED_DIMENSIONS           — embedding dimensions (default: 1536)
  SEARCH_TOP_K               — default top-k results (default: 5)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

SEARCH_ENDPOINT   = os.getenv("AZURE_SEARCH_ENDPOINT", "https://<resource>.search.windows.net")
SEARCH_KEY        = os.getenv("AZURE_SEARCH_KEY", "")
SEARCH_INDEX      = os.getenv("AZURE_SEARCH_INDEX_NAME", "crm-accounts")
SEARCH_API_VER    = "2024-07-01"
DEFAULT_TOP_K     = int(os.getenv("SEARCH_TOP_K", "5"))

AOAI_ENDPOINT     = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AOAI_API_KEY      = os.getenv("AZURE_OPENAI_API_KEY", "")
AOAI_DEPLOYMENT   = os.getenv("AZURE_OPENAI_DEPLOYMENT", "text-embedding-3-large")
AOAI_API_VERSION  = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
EMBED_DIMENSIONS  = int(os.getenv("EMBED_DIMENSIONS", "1536"))


def _embed_query(text: str) -> list[float]:
    """Embed a single query string via Azure OpenAI."""
    url = (
        f"{AOAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{AOAI_DEPLOYMENT}/embeddings?api-version={AOAI_API_VERSION}"
    )
    payload = json.dumps({"input": [text], "dimensions": EMBED_DIMENSIONS}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "api-key": AOAI_API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    return body["data"][0]["embedding"]


def search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    account_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Hybrid vector + full-text + semantic search over AI Search index.

    Args:
        query:       Natural language question.
        top_k:       Number of results to return.
        account_ids: Optional list of account IDs to filter results.
                     Matches against account_id field (blob filename without .md).

    Returns:
        List of dicts sorted by relevance desc:
        [{"account_id": str, "chunk_seq": int, "content": str, "similarity": float}]
        chunk_seq is approximated from the AI Search chunk key suffix.
    """
    if not SEARCH_KEY:
        raise ValueError("AZURE_SEARCH_KEY not configured")

    query_vector = _embed_query(query)

    search_body: dict[str, Any] = {
        "search": query,
        "queryType": "semantic",
        "semanticConfiguration": "default",
        "vectorQueries": [{
            "kind": "vector",
            "vector": query_vector,
            "fields": "embedding",
            "k": top_k,
            "exhaustive": False,
        }],
        "select": "id,account_id,content,parent_id",
        "top": top_k,
    }

    if account_ids:
        # Filter: account_id field stores the blob filename e.g. "ACC-001.md"
        # Strip .md when comparing — account_ids may or may not include .md
        normalized = [f"{a}.md" if not a.endswith(".md") else a for a in account_ids]
        filter_expr = " or ".join(f"account_id eq '{v}'" for v in normalized)
        search_body["filter"] = filter_expr

    url = (
        f"{SEARCH_ENDPOINT.rstrip('/')}/indexes/{SEARCH_INDEX}"
        f"/docs/search?api-version={SEARCH_API_VER}"
    )
    payload = json.dumps(search_body).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "api-key": SEARCH_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())

    results = []
    for i, doc in enumerate(body.get("value", [])):
        # account_id stored as "AccountId.md" — strip extension for consistency
        raw_account_id = doc.get("account_id", "")
        account_id = raw_account_id.replace(".md", "") if raw_account_id.endswith(".md") else raw_account_id

        # Use @search.rerankerScore (semantic) if available, else @search.score
        similarity = doc.get("@search.rerankerScore") or doc.get("@search.score", 0.0)

        results.append({
            "account_id": account_id,
            "chunk_seq": i,           # positional rank (AI Search doesn't expose chunk_seq directly)
            "content": doc.get("content", ""),
            "similarity": float(similarity),
        })

    logger.info("AI Search: query=%r top_k=%d returned %d results", query[:60], top_k, len(results))
    return results
