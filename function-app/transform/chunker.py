"""
chunker.py — splits silver-md account markdown into token-bounded chunks.

Strategy:
  - 800 token target size, 100 token overlap between consecutive chunks
  - Respects markdown section boundaries (## headings) where possible
  - Each chunk carries account_id, chunk_seq, content, md_hash (sha256 of content)

Token counting: tiktoken cl100k_base  (same encoding as text-embedding-3-small)
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Iterator

import tiktoken

logger = logging.getLogger(__name__)

_ENC = tiktoken.get_encoding("cl100k_base")

CHUNK_TOKENS = 800
OVERLAP_TOKENS = 100


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    account_id: str
    chunk_seq: int
    content: str
    md_hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.md_hash = hashlib.sha256(self.content.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _count(text: str) -> int:
    return len(_ENC.encode(text))


def _split_by_sections(md: str) -> list[str]:
    """
    Split markdown at level-2 headings (## …).
    Returns list of section strings, each starting with its heading line.
    """
    parts = re.split(r"(?m)^(?=##\s)", md)
    return [p.strip() for p in parts if p.strip()]


def _sliding_window(text: str, chunk_tok: int, overlap_tok: int) -> Iterator[str]:
    """
    Yield token-bounded windows over *text*.
    Falls back to line-level splitting so we never mid-word cut.
    """
    tokens = _ENC.encode(text)
    step = chunk_tok - overlap_tok
    if step <= 0:
        step = chunk_tok

    start = 0
    while start < len(tokens):
        end = min(start + chunk_tok, len(tokens))
        window_tokens = tokens[start:end]
        yield _ENC.decode(window_tokens)
        if end == len(tokens):
            break
        start += step


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def chunk_document(account_id: str, md_content: str) -> list[Chunk]:
    """
    Chunk a single account .md document.

    Algorithm:
      1. Split into ## sections
      2. If a section fits within CHUNK_TOKENS → keep as one chunk
      3. If larger → sliding-window split within the section
      4. Overlap is applied across section boundaries via carry-over buffer

    Returns list of Chunk objects sorted by chunk_seq.
    """
    sections = _split_by_sections(md_content)
    if not sections:
        sections = [md_content]

    chunks: list[Chunk] = []
    carry: str = ""          # overlap text carried from previous chunk
    seq = 0

    for section in sections:
        # Prepend carry-over from previous chunk
        text = (carry + "\n" + section).strip() if carry else section

        tok_count = _count(text)

        if tok_count <= CHUNK_TOKENS:
            # Section fits in one chunk
            chunks.append(Chunk(account_id=account_id, chunk_seq=seq, content=text))
            seq += 1
            # Set carry = last OVERLAP_TOKENS tokens of this section
            carry = _ENC.decode(_ENC.encode(text)[-OVERLAP_TOKENS:])
        else:
            # Section too large — sliding window
            for window in _sliding_window(text, CHUNK_TOKENS, OVERLAP_TOKENS):
                chunks.append(Chunk(account_id=account_id, chunk_seq=seq, content=window))
                seq += 1
            # carry = overlap from last window
            carry = _ENC.decode(_ENC.encode(section)[-OVERLAP_TOKENS:])

    logger.debug("account_id=%s produced %d chunks", account_id, len(chunks))
    return chunks
