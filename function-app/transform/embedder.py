"""
embedder.py — embed chunks and upsert to PostgreSQL (pgvector).

Embedding providers (controlled by env var EMBED_PROVIDER):
  azure_openai  — Azure OpenAI text-embedding-3-small  (default)
  voyage        — Voyage AI voyage-3-lite               (Claude-friendly, fallback)

PostgreSQL connection: env var POSTGRES_CONN_STR
  e.g. "postgresql://crm_admin:<password>@pg-crm-pocrs.postgres.database.azure.com/postgres?sslmode=require"

Upsert key: (account_id, chunk_seq)
  - If md_hash unchanged → skip  (idempotent)
  - If changed → update embedding + content + updated_at
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values

from transform.chunker import Chunk

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

EMBED_PROVIDER   = os.getenv("EMBED_PROVIDER", "azure_openai")
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "16"))

# Azure OpenAI
AZURE_OPENAI_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "text-embedding-3-large")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")

# Voyage AI
VOYAGE_API_KEY   = os.getenv("VOYAGE_API_KEY", "")
VOYAGE_MODEL     = os.getenv("VOYAGE_MODEL", "voyage-3-lite")

# Embedding dimensions (must match DB column vector(N))
EMBED_DIMENSIONS = int(os.getenv("EMBED_DIMENSIONS", "1536"))

# PostgreSQL
POSTGRES_CONN_STR = os.getenv("POSTGRES_CONN_STR", "")


# ──────────────────────────────────────────────────────────────────────────────
# Embedding clients
# ──────────────────────────────────────────────────────────────────────────────

def _embed_azure_openai(texts: list[str]) -> list[list[float]]:
    """Batch embed via Azure OpenAI REST (no openai SDK dependency)."""
    import urllib.request
    import json

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{AZURE_OPENAI_DEPLOYMENT}/embeddings?api-version={AZURE_OPENAI_API_VERSION}"
    )
    payload = json.dumps({"input": texts, "dimensions": EMBED_DIMENSIONS}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "api-key": AZURE_OPENAI_API_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())

    # Sort by index to preserve order
    sorted_data = sorted(body["data"], key=lambda x: x["index"])
    return [item["embedding"] for item in sorted_data]


def _embed_voyage(texts: list[str]) -> list[list[float]]:
    """Batch embed via Voyage AI REST."""
    import urllib.request
    import json

    url = "https://api.voyageai.com/v1/embeddings"
    payload = json.dumps({"model": VOYAGE_MODEL, "input": texts}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {VOYAGE_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())

    sorted_data = sorted(body["data"], key=lambda x: x["index"])
    return [item["embedding"] for item in sorted_data]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Dispatch to configured provider."""
    if EMBED_PROVIDER == "voyage":
        return _embed_voyage(texts)
    return _embed_azure_openai(texts)


# ──────────────────────────────────────────────────────────────────────────────
# PostgreSQL upsert
# ──────────────────────────────────────────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO chunks (account_id, chunk_seq, content, embedding, md_hash)
VALUES %s
ON CONFLICT (account_id, chunk_seq)
DO UPDATE SET
    content    = EXCLUDED.content,
    embedding  = EXCLUDED.embedding,
    md_hash    = EXCLUDED.md_hash,
    updated_at = now()
WHERE chunks.md_hash IS DISTINCT FROM EXCLUDED.md_hash
"""

_UNIQUE_CONSTRAINT_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chunks_account_id_chunk_seq_key'
    ) THEN
        ALTER TABLE chunks ADD CONSTRAINT chunks_account_id_chunk_seq_key
            UNIQUE (account_id, chunk_seq);
    END IF;
END $$;
"""


def upsert_chunks(chunks: Sequence[Chunk], embeddings: list[list[float]], conn_str: str | None = None) -> int:
    """
    Upsert chunks+embeddings into PostgreSQL.
    Returns count of rows actually written (skips unchanged md_hash).
    """
    if not chunks:
        return 0

    conn_str = conn_str or POSTGRES_CONN_STR
    if not conn_str:
        raise ValueError("POSTGRES_CONN_STR not configured")

    conn = psycopg2.connect(conn_str)
    try:
        with conn:
            with conn.cursor() as cur:
                # Ensure unique constraint exists
                cur.execute(_UNIQUE_CONSTRAINT_SQL)

                rows = [
                    (c.account_id, c.chunk_seq, c.content, emb, c.md_hash)
                    for c, emb in zip(chunks, embeddings)
                ]
                # pgvector expects embedding as string "[x,y,z,...]"
                template = "(%s, %s, %s, %s::vector, %s)"
                execute_values(cur, _UPSERT_SQL, rows, template=template, page_size=100)
                written = cur.rowcount if cur.rowcount >= 0 else len(rows)

        logger.info("upserted %d/%d chunks (account_id=%s)", written, len(rows), chunks[0].account_id)
        return written
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def embed_and_upsert(chunks: list[Chunk], conn_str: str | None = None) -> int:
    """
    Embed all chunks in batches and upsert to PostgreSQL.
    Returns total rows written.
    """
    if not chunks:
        return 0

    total = 0
    for i in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[i : i + EMBED_BATCH_SIZE]
        texts = [c.content for c in batch]

        try:
            embeddings = embed_texts(texts)
        except Exception as exc:
            logger.error("embedding batch %d-%d failed: %s", i, i + len(batch) - 1, exc)
            raise

        total += upsert_chunks(batch, embeddings, conn_str=conn_str)

    return total
