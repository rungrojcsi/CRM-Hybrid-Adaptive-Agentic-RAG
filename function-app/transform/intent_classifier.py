"""
intent_classifier.py — Classify user question intent for hybrid routing.

Returns one of: "qual" | "quant" | "hybrid"
  qual   = narrative, history, "why", customer story, notes
  quant  = numbers, aggregations, "top N", KPIs, time periods
  hybrid = mixed (e.g., "Top 5 deals + explain risk")

Uses gpt-4o-mini for speed/cost balance (~500ms, ~$0.001/call).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Literal

logger = logging.getLogger(__name__)

AZURE_OPENAI_ENDPOINT     = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY      = os.getenv("AZURE_OPENAI_API_KEY", "")
CLASSIFIER_DEPLOYMENT     = os.getenv("INTENT_CLASSIFIER_DEPLOYMENT", "gpt-5.4")
CLASSIFIER_API_VERSION    = os.getenv("INTENT_CLASSIFIER_API_VERSION", "2024-12-01-preview")

Intent = Literal["qual", "quant", "hybrid"]

SYSTEM_PROMPT = """You are a question intent classifier for a CRM AI bot.

Classify the user question into exactly one of these labels:
- "quant": numbers, aggregations, counts, top-N, win rates, totals, time periods, KPIs
- "qual": narrative, customer history, notes, why-questions, account context, descriptions
- "hybrid": needs both numbers AND narrative (e.g., "Top 5 deals at risk and explain why")

Respond with ONLY the label string. No explanation. No quotes."""


def classify(question: str) -> Intent:
    """Classify question intent. Returns 'qual', 'quant', or 'hybrid'."""
    if not question.strip():
        return "qual"

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{CLASSIFIER_DEPLOYMENT}/chat/completions"
        f"?api-version={CLASSIFIER_API_VERSION}"
    )
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": question},
        ],
        "temperature": 0,
        "max_completion_tokens": 16,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "api-key": AZURE_OPENAI_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    label = data["choices"][0]["message"]["content"].strip().lower()
    if label in ("qual", "quant", "hybrid"):
        return label  # type: ignore[return-value]
    logger.warning("Classifier returned unexpected label %r — defaulting to hybrid", label)
    return "hybrid"
