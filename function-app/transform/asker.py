"""
asker.py — RAG answer generation via Azure OpenAI chat completions.

Flow:
  1. Receive retrieved chunks from searcher
  2. Build context prompt (inject chunk content)
  3. Call Azure OpenAI chat completions (gpt-4o or configured deployment)
  4. Return answer + source references

Env vars:
  AZURE_OPENAI_ENDPOINT        — same as embedder (shared Foundry endpoint)
  AZURE_OPENAI_API_KEY         — same as embedder
  AZURE_OPENAI_CHAT_DEPLOYMENT — chat model deployment name (default: gpt-4o)
  AZURE_OPENAI_CHAT_API_VERSION— API version for chat (default: 2024-02-01)
  RAG_MAX_CONTEXT_CHARS        — max chars from chunks injected into prompt (default: 8000)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from .account_resolver import resolve as _resolve_account

logger = logging.getLogger(__name__)

AZURE_OPENAI_ENDPOINT         = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY          = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_CHAT_DEPLOYMENT  = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5.4")
AZURE_OPENAI_CHAT_API_VERSION = os.getenv("AZURE_OPENAI_CHAT_API_VERSION", "2024-12-01-preview")
RAG_MAX_CONTEXT_CHARS         = int(os.getenv("RAG_MAX_CONTEXT_CHARS", "8000"))

_SYSTEM_PROMPT = """You are a CRM data assistant for CSI (a manufacturing company).
You answer questions about customer accounts using the provided context.
Context is extracted from account profiles in the CRM system.

Rules:
- Answer only from the provided context. Do not fabricate data.
- If the context does not contain enough information, say so clearly.
- Be concise and factual. Use bullet points for lists.
- ALWAYS refer to an account by its COMPANY NAME (shown in the header as
  "Account: <Company Name>"). NEVER cite the bare account_id GUID — if a header
  shows only a GUID (no resolved name), say "บัญชี (ไม่พบชื่อ)" rather than printing the GUID.
- Respond in the same language as the user's question (Thai or English)."""


def _build_context(chunks: list[dict[str, Any]], max_chars: int = RAG_MAX_CONTEXT_CHARS) -> str:
    """
    Assemble retrieved chunks into a single context block.
    Truncates to max_chars to stay within model context window.
    """
    lines = []
    total = 0
    for i, chunk in enumerate(chunks):
        acct = chunk["account_id"]
        name = _resolve_account(acct)  # GUID → company name (dim_account.json)
        label = f"{name} (account_id {acct})" if name else f"{acct} (ไม่พบชื่อ)"
        header = f"[{i + 1}] Account: {label} (similarity: {chunk['similarity']:.3f})"
        body = chunk["content"].strip()
        block = f"{header}\n{body}\n"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                lines.append(block[:remaining] + "\n[truncated]")
            break
        lines.append(block)
        total += len(block)
    return "\n".join(lines)


def ask(
    question: str,
    chunks: list[dict[str, Any]],
    temperature: float = 0.1,
) -> dict[str, Any]:
    """
    Generate a natural language answer from retrieved chunks.

    Args:
        question: User's question string.
        chunks:   Output from searcher.search() —
                  list of {account_id, chunk_seq, content, similarity}.
        temperature: LLM temperature (low = factual, default 0.1).

    Returns:
        {
          "answer": str,
          "sources": [{"account_id": str, "chunk_seq": int, "similarity": float}, ...]
        }
    """
    if not AZURE_OPENAI_ENDPOINT:
        raise ValueError("AZURE_OPENAI_ENDPOINT not configured")
    if not AZURE_OPENAI_API_KEY:
        raise ValueError("AZURE_OPENAI_API_KEY not configured")

    context_text = _build_context(chunks)

    user_message = f"""Context (retrieved CRM account data):
{context_text}

Question: {question}"""

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{AZURE_OPENAI_CHAT_DEPLOYMENT}/chat/completions"
        f"?api-version={AZURE_OPENAI_CHAT_API_VERSION}"
    )

    payload = json.dumps({
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        "temperature": temperature,
        "max_completion_tokens": 1024,
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "api-key": AZURE_OPENAI_API_KEY,
        },
        method="POST",
    )

    # Degrade gracefully: a raised urlopen (HTTPError 400/429, timeout) or a None
    # content (finish_reason=length / content filter) must NOT bubble up as an
    # unhandled 500 — that fails the Foundry agent run.
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read())
        answer = (body["choices"][0]["message"].get("content") or "").strip()
        usage  = body.get("usage", {})
    except Exception as exc:
        logger.warning("ask LLM call failed: %s", exc)
        answer, usage = "", {}

    if not answer:
        answer = "ขออภัย ระบบไม่สามารถเรียบเรียงคำตอบได้ในขณะนี้"

    logger.info(
        "ask question=%r chunks=%d tokens_used=%s",
        question[:60], len(chunks),
        usage.get("total_tokens", "?"),
    )

    sources = [
        {
            "account_id": c["account_id"],
            "chunk_seq":  c["chunk_seq"],
            "similarity": c["similarity"],
        }
        for c in chunks
    ]

    return {"answer": answer, "sources": sources}
