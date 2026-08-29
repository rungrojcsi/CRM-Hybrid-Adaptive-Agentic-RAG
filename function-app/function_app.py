"""
Azure Function: CDM → silver-md markdown transform.

Triggers:
  - HTTP POST /api/transform  (manual trigger / ADF Function Activity)
  - Timer: every hour at :05 (runs after ADF Tumbling Window 1h copy lands)
"""

import json
import logging
import os

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from transform.dax_extractor import extract_entities_dax
from transform.aggregator import REQUIRED_ENTITIES, aggregate_account_centric
from transform.renderer import render_and_upload_all, render_and_upload_lens
from transform.lenses import (
    aggregate_by_solution,
    aggregate_by_salesperson,
    aggregate_by_industry,
)
from transform.ai_searcher import search as vector_search
from transform.searcher import search as pg_search
from transform.asker import ask as rag_ask

# V5 hybrid (Quant + Qual) imports
from transform.intent_classifier import classify as classify_intent
from transform.dax_generator import (
    extract_account_citation,
    generate_dax,
    is_empty_or_null_result,
)
from transform.pbi_client import execute_queries as pbi_execute_queries
from transform.quant_pipeline import run_with_retry as run_quant_with_retry
from transform.quant_provenance import build_advisory, format_advisory
from transform.synthesizer import synthesize as synthesize_answer
from transform.orchestrator import plan_subquestions, unify as unify_answer

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

BRONZE_ACCOUNT = os.environ.get("BRONZE_STORAGE_ACCOUNT", "crmdev")
SILVER_ACCOUNT = os.environ.get("SILVER_STORAGE_ACCOUNT", "crmpocrs")
BRONZE_CONTAINER = "bronze"
SILVER_CONTAINER = "silver-md"

logger = logging.getLogger(__name__)


def _get_clients():
    cred = DefaultAzureCredential()
    bronze_svc = BlobServiceClient(
        f"https://{BRONZE_ACCOUNT}.blob.core.windows.net", credential=cred
    )
    silver_svc = BlobServiceClient(
        f"https://{SILVER_ACCOUNT}.blob.core.windows.net", credential=cred
    )
    return (
        bronze_svc.get_container_client(BRONZE_CONTAINER),
        silver_svc.get_container_client(SILVER_CONTAINER),
    )


def _run_transform() -> dict:
    # Source from the LIVE PBI semantic model via DAX (the crmdev → bronze
    # CDM path is dead since 2026-05-29). Downstream is unchanged — the DAX
    # extractor reproduces the cdm_parser entity contract.
    _, silver_client = _get_clients()
    entities = extract_entities_dax(entity_filter=REQUIRED_ENTITIES)
    records = aggregate_account_centric(entities)
    logger.info("Aggregated %d account records", len(records))
    summary = render_and_upload_all(records, silver_client)
    summary["total_accounts"] = len(records)

    # Cross-cutting qualitative lenses over the same records (solution /
    # salesperson / industry), written to their own silver-md prefixes.
    summary["lenses"] = {}
    for name, agg, prefix, tmpl in (
        ("solution", aggregate_by_solution, "solution", "solution.md.j2"),
        ("salesperson", aggregate_by_salesperson, "salesperson", "salesperson.md.j2"),
        ("industry", aggregate_by_industry, "industry", "industry.md.j2"),
    ):
        lens_records = agg(records)
        summary["lenses"][name] = render_and_upload_lens(
            lens_records, silver_client, tmpl, prefix
        )
        summary["lenses"][name]["total"] = len(lens_records)
    return summary


@app.route(route="transform", methods=["POST"])
def http_transform(req: func.HttpRequest) -> func.HttpResponse:
    """Manual / ADF Function Activity trigger."""
    logger.info("HTTP transform trigger received")
    try:
        result = _run_transform()
        return func.HttpResponse(
            json.dumps(result), status_code=200, mimetype="application/json"
        )
    except Exception as exc:
        logger.exception("Transform failed: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500, mimetype="application/json"
        )


@app.timer_trigger(schedule="0 5 * * * *", arg_name="timer", run_on_startup=False)
def timer_transform(timer: func.TimerRequest) -> None:
    """Runs at :05 every hour (5 min after ADF copy window closes)."""
    logger.info("Timer transform trigger (past_due=%s)", timer.past_due)
    result = _run_transform()
    logger.info("Transform result: %s", result)


# ──────────────────────────────────────────────────────────────────────────────
# Sprint 5 — Vector search via Azure AI Search (replaces pgvector + /api/embed)
# ──────────────────────────────────────────────────────────────────────────────

@app.route(route="search", methods=["POST"])
def http_search(req: func.HttpRequest) -> func.HttpResponse:
    """
    Vector similarity search over embedded chunks.

    Request body (JSON):
        q        : str   — natural language query (required)
        top_k    : int   — number of results to return (default: 5)
        account_ids: list[str] — optional filter by account IDs

    Response (JSON):
        {
          "query": str,
          "results": [
            {"account_id": str, "chunk_seq": int, "content": str, "similarity": float},
            ...
          ]
        }
    """
    logger.info("HTTP search trigger received")
    try:
        body = req.get_json()
        q = body.get("q", "").strip()
        if not q:
            return func.HttpResponse(
                json.dumps({"error": "Missing required field: q"}),
                status_code=400, mimetype="application/json"
            )
        top_k = int(body.get("top_k", 5))
        account_ids = body.get("account_ids") or None

        results = vector_search(q, top_k=top_k, account_ids=account_ids)
        return func.HttpResponse(
            json.dumps({"query": q, "results": results}),
            status_code=200, mimetype="application/json"
        )
    except Exception as exc:
        logger.exception("Search failed: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500, mimetype="application/json"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Sprint 3d — RAG answer (search + generative LLM)
# ──────────────────────────────────────────────────────────────────────────────

@app.route(route="ask", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def http_ask(req: func.HttpRequest) -> func.HttpResponse:
    """
    Full RAG pipeline: embed query → vector search → GPT-4o answer.

    Request body (JSON):
        q          : str        — natural language question (required)
        top_k      : int        — chunks to retrieve (default: 5)
        account_ids: list[str]  — optional filter by account IDs
        temperature: float      — LLM temperature (default: 0.1)

    Response (JSON):
        {
          "question": str,
          "answer":   str,
          "sources":  [{"account_id": str, "chunk_seq": int, "similarity": float}, ...]
        }
    """
    logger.info("HTTP ask trigger received")
    try:
        body = req.get_json()
        q = body.get("q", "").strip()
        if not q:
            return func.HttpResponse(
                json.dumps({"error": "Missing required field: q"}),
                status_code=400, mimetype="application/json"
            )
        top_k       = int(body.get("top_k", 5))
        account_ids = body.get("account_ids") or None
        temperature = float(body.get("temperature", 0.1))

        # Step 1: vector search
        chunks = vector_search(q, top_k=top_k, account_ids=account_ids)

        if not chunks:
            return func.HttpResponse(
                json.dumps({"question": q, "answer": "No relevant account data found.", "sources": []}),
                status_code=200, mimetype="application/json"
            )

        # Step 2: generative answer
        result = rag_ask(q, chunks, temperature=temperature)

        return func.HttpResponse(
            json.dumps({"question": q, **result}),
            status_code=200, mimetype="application/json"
        )
    except Exception as exc:
        logger.exception("Ask failed: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500, mimetype="application/json"
        )


# ──────────────────────────────────────────────────────────────────────────────
# pgvector backend — /api/ask-pg (uses psycopg2 + pgvector cosine search)
# ──────────────────────────────────────────────────────────────────────────────

@app.route(route="ask-pg", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def http_ask_pg(req: func.HttpRequest) -> func.HttpResponse:
    """
    RAG via pgvector backend (pg-crm-pocrs).

    Request body (JSON):
        q          : str        — natural language question (required)
        top_k      : int        — chunks to retrieve (default: 5)
        account_ids: list[str]  — optional filter by account IDs
        temperature: float      — LLM temperature (default: 0.1)

    Response (JSON):
        {"question": str, "answer": str, "sources": [...], "backend": "pgvector"}
    """
    logger.info("HTTP ask-pg trigger received")
    try:
        body = req.get_json()
        q = body.get("q", "").strip()
        if not q:
            return func.HttpResponse(
                json.dumps({"error": "Missing required field: q"}),
                status_code=400, mimetype="application/json"
            )
        top_k       = int(body.get("top_k", 5))
        account_ids = body.get("account_ids") or None
        temperature = float(body.get("temperature", 0.1))

        chunks = pg_search(q, top_k=top_k, account_ids=account_ids)

        if not chunks:
            return func.HttpResponse(
                json.dumps({"question": q, "answer": "No relevant account data found.", "sources": [], "backend": "pgvector"}),
                status_code=200, mimetype="application/json"
            )

        result = rag_ask(q, chunks, temperature=temperature)

        return func.HttpResponse(
            json.dumps({"question": q, **result, "backend": "pgvector"}),
            status_code=200, mimetype="application/json"
        )
    except Exception as exc:
        logger.exception("Ask-pg failed: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500, mimetype="application/json"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Combined backend — /api/ask-combined (pgvector + AI Search merged)
# ──────────────────────────────────────────────────────────────────────────────

def _merge_chunks(pg_chunks: list, search_chunks: list, combined_k: int = 8) -> list:
    """
    Merge pgvector + AI Search results, deduplicate, re-rank by similarity.
    Dedup key: first 120 chars of content (catches same chunk from both backends).
    """
    seen: set[str] = set()
    merged = []
    for chunk in pg_chunks + search_chunks:
        key = chunk.get("content", "")[:120].strip()
        if key and key not in seen:
            seen.add(key)
            merged.append(chunk)
    # Sort by similarity descending, take top combined_k
    merged.sort(key=lambda c: c.get("similarity", 0.0), reverse=True)
    return merged[:combined_k]


@app.route(route="ask-combined", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def http_ask_combined(req: func.HttpRequest) -> func.HttpResponse:
    """
    RAG via both pgvector + AI Search (merged results, single GPT-4o call).

    Request body (JSON):
        q          : str        — natural language question (required)
        top_k      : int        — chunks per backend (default: 5)
        combined_k : int        — max chunks after merge (default: 8)
        account_ids: list[str]  — optional filter by account IDs
        temperature: float      — LLM temperature (default: 0.1)

    Response (JSON):
        {"question": str, "answer": str, "sources": [...],
         "backend": "combined", "pg_count": int, "search_count": int}
    """
    logger.info("HTTP ask-combined trigger received")
    try:
        body = req.get_json()
        q = body.get("q", "").strip()
        if not q:
            return func.HttpResponse(
                json.dumps({"error": "Missing required field: q"}),
                status_code=400, mimetype="application/json"
            )
        top_k       = int(body.get("top_k", 5))
        combined_k  = int(body.get("combined_k", 8))
        account_ids = body.get("account_ids") or None
        temperature = float(body.get("temperature", 0.1))

        # Call both backends (sequential — Consumption plan)
        pg_chunks     = pg_search(q, top_k=top_k, account_ids=account_ids)
        search_chunks = vector_search(q, top_k=top_k, account_ids=account_ids)

        logger.info("Combined: pg=%d search=%d", len(pg_chunks), len(search_chunks))

        chunks = _merge_chunks(pg_chunks, search_chunks, combined_k=combined_k)

        if not chunks:
            return func.HttpResponse(
                json.dumps({"question": q, "answer": "No relevant account data found.",
                            "sources": [], "backend": "combined",
                            "pg_count": 0, "search_count": 0}),
                status_code=200, mimetype="application/json"
            )

        result = rag_ask(q, chunks, temperature=temperature)

        return func.HttpResponse(
            json.dumps({"question": q, **result, "backend": "combined",
                        "pg_count": len(pg_chunks), "search_count": len(search_chunks)}),
            status_code=200, mimetype="application/json"
        )
    except Exception as exc:
        logger.exception("Ask-combined failed: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500, mimetype="application/json"
        )


@app.route(route="ask-quant", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def http_ask_quant(req: func.HttpRequest) -> func.HttpResponse:
    """
    V5 Quant-only: NL → DAX → Power BI Semantic Model → tabular rows + brief Thai summary.

    Used by crm-quant-agent subagent (in multi-agent orchestrator pattern).
    Returns structured rows + DAX trace so orchestrator can synthesize with Qual peers.

    Body: {"q": str, "temperature"?: float, "narrate"?: bool}
      narrate=True (default) → also compose the full Thai narrative in "answer"
        (standalone /api/ask-quant stays usable on its own).
      narrate=False (tool mode) → skip the ~6s synthesize LLM call; return
        structured-only ("answer" empty) and let the orchestrator narrate from
        data + top_accounts + advisory.
    Response: {"question", "answer", "advisory", "dax", "data", "row_count", "error", ...}
    """
    logger.info("HTTP ask-quant trigger received")
    try:
        body = req.get_json()
        q = body.get("q", "").strip()
        if not q:
            return func.HttpResponse(
                json.dumps({"error": "Missing required field: q"}),
                status_code=400, mimetype="application/json"
            )
        temperature = float(body.get("temperature", 0.1))
        narrate = bool(body.get("narrate", True))

        result = run_quant_with_retry(q)
        rows = result["rows"]
        dax = result["dax"]
        citation = result["citation"]

        # Deterministic H3/H4 advisory (provenance / certified measure / plausibility)
        # as a structured field so the orchestrator can append it verbatim WITHOUT the
        # LLM narrative. build_advisory is deterministic — no network, no LLM.
        advisory = format_advisory(build_advisory(q, dax, rows))

        # Tool mode (narrate=False) skips the ~6s synthesize LLM call; the orchestrator
        # narrates from the structured fields. Standalone (default) keeps the narrative.
        answer = ""
        if narrate:
            answer = synthesize_answer(
                q,
                qual_chunks=None,
                quant_rows=rows,
                temperature=temperature,
                quant_dax=dax,
                quant_citation=citation,
            )

        return func.HttpResponse(
            json.dumps({
                "question":          q,
                "answer":            answer,
                "advisory":          advisory,
                "dax":               dax,
                "data":              rows[:50],
                "row_count":         len(rows),
                "value":             citation.get("value"),
                "measure":           citation.get("measure"),
                "top_accounts":      citation.get("top_accounts"),
                "is_aggregate_only": citation.get("is_aggregate_only"),
                "filter_subject":    citation.get("filter_subject"),
                "error":             result["error"],
                "retried":           result["retried"],
                "attempts":          result["attempts"],
                "backend":           "quant",
            }, ensure_ascii=False),
            status_code=200, mimetype="application/json"
        )
    except Exception as exc:
        logger.exception("Ask-quant failed: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500, mimetype="application/json"
        )


def _run_qual(q: str, top_k: int = 5, combined_k: int = 8, temperature: float = 0.1) -> dict:
    """ask_combined internals: pg + vector retrieval → merge → narrative. Reused by orchestrator."""
    pg_chunks     = pg_search(q, top_k=top_k)
    search_chunks = vector_search(q, top_k=top_k)
    chunks = _merge_chunks(pg_chunks, search_chunks, combined_k=combined_k)
    if not chunks:
        return {"answer": "", "sources": [], "pg_count": 0, "search_count": 0}
    result = rag_ask(q, chunks, temperature=temperature)
    return {**result, "pg_count": len(pg_chunks), "search_count": len(search_chunks)}


@app.route(route="orchestrator", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def http_orchestrator(req: func.HttpRequest) -> func.HttpResponse:
    """
    Code orchestrator: plan → (ask_quant ∥ ask_combined, PARALLEL) → unify → ONE Thai answer.

    The Foundry relay (crm-hybrid-agent) POSTs the user's question verbatim and returns
    `answer` unchanged. Parallel dispatch + deterministic year fix live here, not in the LLM agent.

    Body: {"q": str, "temperature"?: float}
    Response: {"question", "answer", "quant", "qual", "plan", "backend": "orchestrator"}
    """
    import concurrent.futures

    logger.info("HTTP orchestrator trigger received")
    try:
        body = req.get_json()
        q = body.get("q", "").strip()
        if not q:
            return func.HttpResponse(
                json.dumps({"error": "Missing required field: q"}),
                status_code=400, mimetype="application/json"
            )
        temperature = float(body.get("temperature", 0.1))

        # 1. Plan: verbatim + tool-specific narrative for each tool
        quant_q, qual_q = plan_subquestions(q)
        logger.info("orchestrator plan: quant_q_len=%d qual_q_len=%d", len(quant_q), len(qual_q))

        # 2. PARALLEL: full quant pipeline ∥ qual retrieval+narrative
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_quant = ex.submit(run_quant_with_retry, quant_q)
            f_qual  = ex.submit(_run_qual, qual_q, temperature=temperature)
            quant = f_quant.result()
            qual  = f_qual.result()

        # Safety net — ONE deterministic retry if quant came back empty: re-run with the
        # user's VERBATIM question (different phrasing than the plan's quant_q → different
        # DAX). Code-controlled and capped at one extra call, so it cannot cascade.
        if not quant["rows"] and quant_q != q:
            retry = run_quant_with_retry(q)
            if retry["rows"]:
                logger.info("orchestrator quant safety-net retry recovered %d rows", len(retry["rows"]))
                quant, quant_q = retry, q

        advisory = format_advisory(build_advisory(quant_q, quant["dax"], quant["rows"]))

        # 3. Unify into one Thai answer
        answer = unify_answer(
            q,
            quant_rows=quant["rows"],
            quant_dax=quant["dax"],
            quant_citation=quant["citation"],
            qual_answer=qual.get("answer"),
            advisory=advisory,
            temperature=temperature,
        )

        return func.HttpResponse(
            json.dumps({
                "question": q,
                "answer":   answer,
                "quant": {
                    "dax":               quant["dax"],
                    "data":              quant["rows"][:50],
                    "row_count":         len(quant["rows"]),
                    "value":             quant["citation"].get("value"),
                    "measure":           quant["citation"].get("measure"),
                    "top_accounts":      quant["citation"].get("top_accounts"),
                    "is_aggregate_only": quant["citation"].get("is_aggregate_only"),
                    "advisory":          advisory,
                    "error":             quant["error"],
                },
                "qual": {
                    "sources":      qual.get("sources", []),
                    "pg_count":     qual.get("pg_count", 0),
                    "search_count": qual.get("search_count", 0),
                },
                "plan":    {"quant_q": quant_q, "qual_q": qual_q},
                "backend": "orchestrator",
            }, ensure_ascii=False),
            status_code=200, mimetype="application/json"
        )
    except Exception as exc:
        logger.exception("Orchestrator failed: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": str(exc),
                        "answer": "ขออภัย ระบบไม่สามารถประมวลผลคำถามนี้ได้ในขณะนี้"}),
            status_code=200, mimetype="application/json"
        )


@app.route(route="ask-hybrid", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def http_ask_hybrid(req: func.HttpRequest) -> func.HttpResponse:
    """
    V5 Hybrid RAG: Qual (AI Search + pgvector) + Quant (PBI Semantic Model via DAX).

    Routes by LLM-classified intent (qual/quant/hybrid) then fans out to relevant
    backends in parallel and synthesizes a single unified Thai answer.

    Request body (JSON):
        q          : str        — natural language question (required)
        top_k      : int        — chunks per Qual backend (default: 5)
        combined_k : int        — max Qual chunks after merge (default: 8)
        account_ids: list[str]  — optional Qual filter
        force_intent: str       — override classifier ("qual"|"quant"|"hybrid")
        temperature: float      — synthesizer temperature (default: 0.1)

    Response (JSON):
        {"question", "answer", "intent", "qual_sources", "quant_dax",
         "quant_data", "backend": "hybrid"}
    """
    import concurrent.futures

    logger.info("HTTP ask-hybrid trigger received")
    try:
        body = req.get_json()
        q = body.get("q", "").strip()
        if not q:
            return func.HttpResponse(
                json.dumps({"error": "Missing required field: q"}),
                status_code=400, mimetype="application/json"
            )
        top_k        = int(body.get("top_k", 5))
        combined_k   = int(body.get("combined_k", 8))
        account_ids  = body.get("account_ids") or None
        force_intent = body.get("force_intent")
        temperature  = float(body.get("temperature", 0.1))

        # 1. Intent: default to "hybrid" (call all 3 backends always — let synthesizer decide).
        #    Set DEFAULT_INTENT env to "classify" to use LLM intent classifier instead.
        default_mode = os.getenv("DEFAULT_INTENT", "hybrid")
        if force_intent in ("qual", "quant", "hybrid"):
            intent = force_intent
        elif default_mode == "classify":
            intent = classify_intent(q)
        else:
            intent = default_mode  # "hybrid" by default
        logger.info("Hybrid intent=%s for q=%r", intent, q[:80])

        qual_chunks: list = []
        quant_result: dict = {}

        # 2. Fan-out in parallel: Qual searches + full Quant pipeline (gen+execute+retry)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            if intent in ("qual", "hybrid"):
                futures["pg"]     = executor.submit(pg_search, q, top_k=top_k, account_ids=account_ids)
                futures["search"] = executor.submit(vector_search, q, top_k=top_k, account_ids=account_ids)
            if intent in ("quant", "hybrid"):
                futures["quant"]  = executor.submit(run_quant_with_retry, q)

            pg_chunks     = futures["pg"].result()     if "pg" in futures     else []
            search_chunks = futures["search"].result() if "search" in futures else []
            quant_result  = futures["quant"].result()  if "quant" in futures  else {}

        if pg_chunks or search_chunks:
            qual_chunks = _merge_chunks(pg_chunks, search_chunks, combined_k=combined_k)

        quant_rows = quant_result.get("rows", [])
        quant_dax = quant_result.get("dax")
        quant_error = quant_result.get("error")
        quant_retried = quant_result.get("retried", False)
        quant_citation = quant_result.get("citation") or extract_account_citation([])

        # 3. Synthesize unified answer
        answer = synthesize_answer(
            q,
            qual_chunks,
            quant_rows,
            temperature=temperature,
            quant_dax=quant_dax,
            quant_citation=quant_citation,
        )

        return func.HttpResponse(
            json.dumps({
                "question":          q,
                "answer":            answer,
                "intent":            intent,
                "qual_sources":      [c.get("account_id", "") for c in qual_chunks],
                "quant_dax":         quant_dax,
                "quant_data":        quant_rows[:50],
                "value":             quant_citation.get("value"),
                "measure":           quant_citation.get("measure"),
                "top_accounts":      quant_citation.get("top_accounts"),
                "is_aggregate_only": quant_citation.get("is_aggregate_only"),
                "filter_subject":    quant_citation.get("filter_subject"),
                "backend":           "hybrid",
                "pg_count":          len(pg_chunks)     if intent != "quant" else 0,
                "search_count":      len(search_chunks) if intent != "quant" else 0,
                "quant_count":       len(quant_rows),
                "quant_error":       quant_error,
                "quant_retried":     quant_retried,
                "quant_attempts":    quant_result.get("attempts", 0),
            }, ensure_ascii=False),
            status_code=200, mimetype="application/json"
        )
    except Exception as exc:
        logger.exception("Ask-hybrid failed: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500, mimetype="application/json"
        )
