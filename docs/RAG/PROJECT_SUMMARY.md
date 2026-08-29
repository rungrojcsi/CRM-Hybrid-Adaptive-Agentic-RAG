# RAG_Azure — Project Summary

**Project:** CSI CRM RAG on Azure
**Owner:** Boss (Rojios) · DX & AI Consultant Director, CSI Group
**Date:** 2026-05-23
**Status:** V5 Hybrid (Qual + Quant) — Phase 1 development, partial code in place
**Region lock:** `southeastasia`
**Subscription:** Azure Credit - <subscription> (`00000000-0000-0000-0000-000000000000`)

---

## 1. Goal

แปลง Dynamics 365 CE (CRM) → Account-centric markdown documents → ตอบคำถาม natural language ผ่าน 2 paths:

- **Qual track (RAG):** semantic search → markdown chunks → LLM synthesis
- **Quant track (NL2DAX):** intent classify → DAX generation → Power BI XMLA executeQueries → numeric answer
- **Hybrid endpoint (V5):** `/api/ask-hybrid` routes to one or both tracks then synthesizes a single answer

ผู้ใช้ปลายทาง: CSI sales analyst สอบถามผ่าน Teams bot (production: `crm-multiagent-bot`) หรือ webui prototype

---

## 2. Architecture (V5 Hybrid)

```
Dynamics 365 CE (Dataverse)
   ↓ Power BI Dataflow Gen1 (SALE DATA CLEANSING) — daily refresh
   ↓ ADLS Gen2 CDM (crmdev storage, upstream-managed)
   │
   ↓ ADF pipeline PL_crmdev_to_pocrs_bronze (Tumbling Window 1h)
   ↓
crmpocrs storage
   ├── bronze/        19 Dim_* CDM entities (mirrored)
   ├── silver-md/     Jinja2-rendered Account-centric .md files (one .md per AccountId)
   │                  Idempotent via md_hash blob-metadata
   ↓ (Qual track) Azure OpenAI text-embedding-3 → Azure AI Search index
   ↓ (Quant track) Power BI XMLA → CSI_DATA_MODEL (Layer 4 Semantic Model)
   ↓
Azure Function `function-app` (Python 3.11, Linux Consumption)
   /api/transform        CDM → silver-md render (HTTP + Timer hourly :05)
   /api/search           PG vector search
   /api/ask              RAG ask (PG-only)
   /api/ask-pg           Alias
   /api/ask-combined     PG + AI Search merge
   /api/ask-hybrid       V5 Qual+Quant route + synthesize
   ↓
webui/index.html (prototype) / Teams bot (production crm-multiagent-bot)
```

---

## 3. Code Layout

```
RAG_Azure/
├── CLAUDE.md                  Project context (stack, conventions, constraints)
├── HANDOFF_V35_2026-05-23.md  Active session handoff (V5 decision)
├── HANDOFF.md                 V3 production record (legacy)
├── pipeline-summary.md        7-layer architecture write-up
├── PROJECT_SUMMARY.md         this file
├── embed_all.sh               batch embedding script
├── function-app/         Azure Function workspace
│   ├── function_app.py        HTTP + Timer triggers (7 endpoints)
│   ├── host.json              Functions runtime config
│   ├── requirements.txt
│   ├── local.settings.json    dev env (Azure OpenAI, AI Search, PG, PBI)
│   ├── openapi_ask_hybrid.json
│   ├── transform/
│   │   ├── cdm_parser.py      model.json + CSV loader (load_model, load_entities_blob)
│   │   ├── aggregator.py      Account-centric merge (aggregate_account_centric)
│   │   ├── renderer.py        Jinja2 render + blob upload (md_hash idempotency)
│   │   ├── chunker.py         tiktoken cl100k_base (~800 tok, 100 overlap)
│   │   ├── embedder.py        Azure OpenAI text-embedding-3 + Voyage fallback → pgvector upsert
│   │   ├── searcher.py        PG pgvector search
│   │   ├── ai_searcher.py     Azure AI Search hybrid (vector + keyword)
│   │   ├── asker.py           RAG ask (build context + chat)
│   │   ├── intent_classifier.py  V5 — qual vs quant routing
│   │   ├── dax_generator.py   V5 — NL2DAX via LLM + schema/measures grounding
│   │   ├── pbi_client.py      V5 — Power BI REST executeQueries (XMLA)
│   │   └── synthesizer.py     V5 — merge qual+quant into single answer
│   └── tests/
│       ├── test_aggregator.py
│       └── fixtures/make_fixtures.py  synthetic CDM for offline tests
├── dax/
│   ├── sm_crm_rs_schema.json     Semantic Model schema
│   ├── measures.json                DAX measures registry
│   ├── prod_csi_data_model_measures.tsv
│   └── v5_crm_measures.dax
├── fabric/
│   └── notebook_ingest_bronze.py    Fabric Lakehouse ingestion (orphan POC)
├── Resulted/
│   ├── rag-Claude-test.md           validation test output
│   ├── rag-Copilot-test.md          baseline comparison
│   ├── V4_architecture_infographic.png
│   └── V5_architecture_infographic.png
└── webui/index.html                 Browser prototype client
```

---

## 4. Knowledge Graph Summary (from `/graphify`)

- **250 nodes, 304 edges, 23 communities**
- **Top God Nodes** (most connected):
  1. `aggregate_account_centric()` — 16 edges (silver-md aggregation core)
  2. `_load()` — 10 edges (CDM CSV loader)
  3. `_run_transform()` — 8 edges (transform orchestration)
  4. `chunk_document()` — 8 edges (tokenization seam)
  5. `search()` — 8 edges (PG vector retrieval)
  6. `ask()` — 8 edges (RAG entry)
  7. `http_ask_hybrid` (`/api/ask-hybrid` V5) — 8 edges
- **Cross-community bridges (betweenness):**
  - `chunk_document()` (0.247) — AI Search ↔ Renderer
  - `render_and_upload_all()` (0.239) — Renderer ↔ HTTP API ↔ Transform Trigger ↔ AI Search
  - `_embed_query()` (0.219) — AI Search ↔ V5 Hybrid RAG
- **127 weakly-connected nodes** = documentation gaps / fixture isolation / config islands

Artifacts: `graphify-out/graph.html`, `graphify-out/GRAPH_REPORT.md`, `graphify-out/graph.json`

---

## 5. Key Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/transform` | POST | Render CDM → silver-md (manual) |
| (timer) | every 1h :05 | Auto-render after ADF copy |
| `/api/search` | POST | Vector search on chunks (PG) |
| `/api/ask` | POST | RAG ask (PG-only) |
| `/api/ask-pg` | POST | Alias of `/ask` |
| `/api/ask-combined` | POST | PG + Azure AI Search merge (top-K each, merge_chunks) |
| `/api/ask-hybrid` | POST | **V5** — intent classify → qual (RAG) and/or quant (DAX via PBI) → synthesize |

---

## 6. Key Decisions (locked)

| # | Decision | Rationale |
|---|---|---|
| 1 | Plug at **CSI_DATA_MODEL (Layer 4)** via XMLA | Boss has no Copilot Studio admin; XMLA = loose contract, same data as production bot, 0.5-1 day vs 3-5 days |
| 2 | Linux **Consumption** plan (classic) | Flex Consumption blocked in `southeastasia` (corr ID `b2a607ca-...`) as of 2026-05-20 |
| 3 | Azure OpenAI in **East US / Sweden Central** | `text-embedding-3` not available in Southeast Asia |
| 4 | **Neon Postgres pgvector** as canonical store | Only managed Azure Postgres with official remote MCP (Supabase analog) |
| 5 | **md_hash idempotency** in blob metadata | sha256 of body — skip upload if unchanged |
| 6 | **One Account → one .md** keyed by AccountId | Filename: `account/{AccountId}.md` |
| 7 | **Trust `model.json`** for CDM schema | Do not hardcode column names (D365 FK = `_<entity>_value` or `<Entity>Id`) |
| 8 | **Test-first transform** with synthetic fixtures | `tests/fixtures/make_fixtures.py` — no real PII in repo |

---

## 7. Production Context (reverse-engineered)

The bot Boss is extending (`crm-multiagent-bot`) lives in Microsoft Copilot Studio env `Default-00000000-0000-0000-0000-000000000000` (Boss has **no access** — admin gate). Production stack:

```
D365 CE → Dataflow Gen1 (SALES_DATA ws 2de94070-...)
       → CSI_DATA_MODEL (dataset 00000000-0000-0000-0000-000000000000)
         Tables: Dim_{Account,Date,Department,Employee,Project,Solution,Allocation,…}
                 Fact_{Income_Plan, Opportunity, Project_Expenses}
         DAX: Profit Margin %, Departments at Risk, Best Month Profit, …
         RLS: Sales Person ID
       → TextToQuery (hidden Azure Function — admin-gated)
       → Foundry <resource> / projects: proj-00, proj-crmmodel, proj-crm-pocrs
       → crm-multiagent-bot (Copilot Studio)
       → Teams
```

Supporting resources (RG `RESOURCE_GROUP`): `<resource>`, `<resource>` (Purview), `<resource>` (Cosmos), `<resource>` (KV), `<resource>`, `<resource>`.

RG `RESOURCE_GROUP`: `crm-aixpg-agent73268` (Azure Bot), `foundry-crm-pocrs`, `proj-crm-pocrs` (5 agents inc. `crm-aixpg-agent v10`, `crm-search-agent`, `crm-pg-agent`, `rsboardroom`, `proposal-master-rs`).

---

## 8. Pitfalls (do not repeat)

1. **Fabric Data Agent needs F64+** — Trial FTL64 ไม่รวม feature, ต้อง tenant admin
2. **Fabric Web Modeling UI เปราะ** — measure dev ใน Power BI Desktop
3. **Bronze CSVs are headerless** — column names live ใน `model.json` CDM manifest
4. **Fact_Opportunity strict-type fails** — US date + decimals as int → all-string + cast in DAX
5. **Monaco editor auto-indent** — paste via `cmd+v` not programmatic
6. **Flex Consumption blocked** in southeastasia — use classic Linux Consumption
7. **No `az` CLI locally** as of 2026-05-20 — use synthetic fixtures until SP env vars configured

---

## 9. Boss's Microsoft License Inventory (verified 2026-05-23)

- ✅ Power BI **PPU** (paid)
- ✅ **Fabric Trial** FTL64 SE Asia (60d, expires ~2026-07-22)
- ✅ **M365 E3**
- ✅ **Copilot Studio Viral**
- ❌ Not tenant admin · ❌ No Copilot Studio env admin · ❌ Cannot see TextToQuery function code

---

## 10. Orphan Fabric POC (decision pending)

Created before Layer 4 pivot — needs disposition:

| Item | ID | Status |
|---|---|---|
| Fabric Trial | FTL64 SE Asia | active, expires ~2026-07-22 |
| Workspace `CSI_CRM_QUANT_POC_rs` | `00000000-0000-0000-0000-000000000000` | created |
| Lakehouse `lh_crm_rs` | `00000000-0000-0000-0000-000000000000` | 17 Delta tables, ~297K rows |
| Notebook | `00000000-0000-0000-0000-000000000000` | code in `fabric/notebook_ingest_bronze.py` |
| Semantic Model `sm_crm_rs` | `00000000-0000-0000-0000-000000000000` | 16 tables Direct Lake |
| Dataflow `df_crm_dim_load` | `00000000-0000-0000-0000-000000000000` | unused |

**Options:** (A) Delete to free Trial credits · (B) Keep as reference · (C) Repurpose as 2nd-track Direct Lake bot

---

## 11. Open Todos (Airtable Boss Memory)

| Priority | Title | Record |
|---|---|---|
| P1 | Inspect Foundry proj-00 | `recMgbu1qt4jwLjA4` |
| P1 | Request Copilot Studio admin access | `reczMQMLo8Httx4WJ` |
| P2 | Identify TextToQuery Function App owner | `recCHDEQ5tdeC3ael` |
| P2 | Decide fate of orphan Fabric POC | `recsh4PaBuZ2ynRQK` |

---

## 12. Quick URLs

- **Foundry:** https://ai.azure.com/
- **PBI Workspace SALES_DATA:** https://app.powerbi.com/groups/00000000-0000-0000-0000-000000000000
- **CSI_DATA_MODEL:** https://app.powerbi.com/onelake/details/00000000-0000-0000-0000-000000000000/dataset/00000000-0000-0000-0000-000000000000/overview
- **Azure RG KT:** Portal → RESOURCE_GROUP (sub `00000000-…
- **Azure RG RS:** Portal → RESOURCE_GROUP (sub `00000000-…

---

## 13. Next Action

Per `HANDOFF_V35_2026-05-23.md`:

1. เปิด Foundry portal → proj-00 (ยังไม่เคยตรวจ)
2. ตรวจ Agents / Workflows / Connections
3. Configure new agent with Power BI tool → CSI_DATA_MODEL (XMLA)
4. Test in Playground: "Profit Margin เดือนนี้?", "แผนกไหนเสี่ยงสุด?"
5. กลับมา deploy Function App แล้ว wire `/api/ask-hybrid` กับ Foundry agent

**Session stats:** ~6h spent, 4 pivots (Fabric Trial → Lakehouse → Custom NL2SQL → Direct Semantic Model). Final landing = Layer 4 XMLA via Foundry proj-00.
