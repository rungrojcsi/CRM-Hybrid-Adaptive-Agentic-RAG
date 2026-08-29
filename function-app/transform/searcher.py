"""
searcher.py — vector similarity search against pgvector chunks table.

Usage:
    results = search(query_text, top_k=5)
    # returns list of dicts: {account_id, chunk_seq, content, similarity}
"""

from __future__ import annotations

import logging
import os
from typing import Any

import psycopg2
import psycopg2.extras

from transform.embedder import embed_texts

logger = logging.getLogger(__name__)

POSTGRES_CONN_STR = os.getenv("POSTGRES_CONN_STR", "")
DEFAULT_TOP_K = int(os.getenv("SEARCH_TOP_K", "5"))

_SEARCH_SQL = """
SELECT
    account_id,
    chunk_seq,
    content,
    1 - (embedding <=> %s::vector) AS similarity
FROM chunks
ORDER BY embedding <=> %s::vector
LIMIT %s
"""

_SEARCH_ACCOUNT_SQL = """
SELECT
    account_id,
    chunk_seq,
    content,
    1 - (embedding <=> %s::vector) AS similarity
FROM chunks
WHERE account_id = ANY(%s)
ORDER BY embedding <=> %s::vector
LIMIT %s
"""


def search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    account_ids: list[str] | None = None,
    conn_str: str | None = None,
) -> list[dict[str, Any]]:
    """
    Embed query and return top-K most similar chunks.

    Args:
        query:       Natural language question.
        top_k:       Number of results to return.
        account_ids: Optional list to restrict search to specific accounts.
        conn_str:    Override POSTGRES_CONN_STR env var.

    Returns:
        List of dicts sorted by similarity desc:
        [{"account_id": str, "chunk_seq": int, "content": str, "similarity": float}]
    """
    # Embed the query (single text → single vector)
    vectors = embed_texts([query])
    query_vec = vectors[0]

    conn_str = conn_str or POSTGRES_CONN_STR
    if not conn_str:
        raise ValueError("POSTGRES_CONN_STR not configured")

    conn = psycopg2.connect(conn_str)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if account_ids:
                cur.execute(
                    _SEARCH_ACCOUNT_SQL,
                    (str(query_vec), account_ids, str(query_vec), top_k),
                )
            else:
                cur.execute(_SEARCH_SQL, (str(query_vec), str(query_vec), top_k))

            rows = cur.fetchall()

        results = [
            {
                "account_id": row["account_id"],
                "chunk_seq": row["chunk_seq"],
                "content": row["content"],
                "similarity": float(row["similarity"]),
            }
            for row in rows
        ]
        logger.info("search query=%r top_k=%d returned %d results", query[:60], top_k, len(results))
        return results

    finally:
        conn.close()
