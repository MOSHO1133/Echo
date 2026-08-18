# Echo — AI Research Assistant

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-Backend-009688?logo=fastapi&logoColor=white)
![ChromaDB](https://img.shields.io/badge/ChromaDB-Vector%20Store-FF6F00)
![sentence--transformers](https://img.shields.io/badge/sentence--transformers-Embeddings-FCC624)
![Groq](https://img.shields.io/badge/LLM-Groq-F55036)
![SQLite](https://img.shields.io/badge/SQLite-Database-003B57?logo=sqlite&logoColor=white)
![Status](https://img.shields.io/badge/Status-Active-2EA44F)
![License](https://img.shields.io/badge/License-Unlicensed-lightgrey)

Echo is a local-first research assistant that helps researchers, students, and independent
learners move from **literature discovery** to **identifying where they could contribute
original work**. It combines live paper search, retrieval-augmented summarization, per-paper
Q&A, cross-paper comparison, and idea-to-literature matching in a single self-hosted tool.

Echo is not a wrapper around a single prompt — it is a full **RAG (Retrieval-Augmented
Generation) pipeline**: every paper you add is parsed, chunked, embedded into a vector store,
and then queried on demand, so that summaries and chat answers are grounded in the paper's
actual text rather than the model's general knowledge.

---

## Table of Contents

- [Core Features](#core-features)
- [Tech Stack](#tech-stack)
- [System Architecture](#system-architecture)
- [Data Flow](#data-flow)
- [Project Structure](#project-structure)
- [Setup & Installation](#setup--installation)
- [Environment Variables](#environment-variables)
- [API Reference](#api-reference)
- [Design Decisions](#design-decisions)
- [Known Limitations & Roadmap](#known-limitations--roadmap)
- [Security Notes](#security-notes)

---

## Core Features

| Feature | Description |
|---|---|
| **Live arXiv search** | Search arXiv's public API directly, with adjustable result count and year-range filtering. |
| **PDF upload** | Upload your own paper or in-progress draft — processed through the same pipeline as searched papers. |
| **Automatic structured summaries** | Every paper added gets four fields generated via RAG: **Methodology**, **Findings**, **Research Gap**, **Future Work**. |
| **Per-paper Q&A (chat)** | Ask free-form questions about any paper in your library; answers are grounded in retrieved source chunks and cite them (`[Source 1]`, etc.). |
| **Cross-paper comparison** | Select 2+ papers and view their summary fields side by side in a table. |
| **Contribute / idea matching** | Describe your own research idea; Echo finds the most similar paper in your library by embedding similarity and generates concrete guidance plus a novelty read (High / Medium / Low). |
| **Full-text reader** | View the extracted full text of any paper in a modal, without leaving the app. |
| **Background processing** | Adding a paper returns immediately; indexing and summarization run as a background task, with the UI auto-polling until complete. |

---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Backend framework | **FastAPI** (Python) | REST API, background tasks |
| Vector store | **ChromaDB** | Stores chunk embeddings for retrieval |
| Embedding model | **sentence-transformers** | Local, free, no API calls — runs on CPU |
| Structured data | **SQLite** | Paper metadata, summaries, feedback |
| LLM provider | **Groq** (OpenAI-compatible endpoint, `openai/gpt-oss-120b`) | Summarization + chat generation |
| PDF parsing | **PyMuPDF (fitz)** | Extracts full text from uploaded/downloaded PDFs |
| Paper source | **arXiv public API** | No key required |
| Frontend | Vanilla **HTML / CSS / JavaScript** | No framework — single `app.js`, single `echo_app.html` |

> **Why a swappable LLM provider?** `rag.py` uses the standard OpenAI Python client pointed at a
> provider's OpenAI-compatible endpoint. Swapping providers (the project has run on xAI Grok,
> Google Gemini, and now Groq) is a two-line change: `base_url` and `api_key`. This was a
> deliberate design choice so the project isn't locked to one vendor's pricing or rate limits.

---

## System Architecture

```mermaid
flowchart TB
    subgraph Client["Frontend — Browser"]
        UI["echo_app.html<br/>+ app.js<br/>(vanilla JS SPA)"]
    end

    subgraph API["Backend — FastAPI (app/main.py)"]
        Search["/search"]
        AddSearch["/library/add-from-search"]
        Upload["/library/upload"]
        Library["/library"]
        Ask["/ask"]
        Compare["/compare"]
        Contribute["/contribute"]
    end

    subgraph Pipeline["Processing Pipeline"]
        Ingestion["ingestion.py<br/>arXiv fetch + PDF extraction"]
        Processing["processing.py<br/>section detection + chunking"]
        Embeddings["embeddings.py<br/>sentence-transformers + ChromaDB"]
        Summarize["summarize.py<br/>4-field structured summary"]
        Rag["rag.py<br/>retrieve-and-generate (call_llm)"]
        ContributeLogic["contribute.py<br/>idea-to-paper similarity match"]
    end

    subgraph Storage["Storage"]
        SQLite[("SQLite<br/>papers, feedback")]
        Chroma[("ChromaDB<br/>chunk vectors")]
    end

    subgraph External["External Services"]
        ArxivAPI["arXiv Public API"]
        Groq["Groq API<br/>(OpenAI-compatible)"]
    end

    UI -->|HTTP fetch| Search
    UI --> AddSearch
    UI --> Upload
    UI --> Library
    UI --> Ask
    UI --> Compare
    UI --> Contribute

    Search --> Ingestion
    Ingestion -->|query| ArxivAPI

    AddSearch -->|background task| Processing
    Upload -->|background task| Processing
    Processing --> Embeddings
    Embeddings --> Chroma
    Processing --> Summarize
    Summarize --> Rag
    Rag -->|chat completion| Groq
    Rag --> Chroma

    Ask --> Rag
    Contribute --> ContributeLogic
    ContributeLogic --> Rag
    ContributeLogic --> Chroma

    AddSearch --> SQLite
    Upload --> SQLite
    Library --> SQLite
    Summarize --> SQLite
    Compare --> SQLite
```

---

## Data Flow

### Adding a paper (search result or upload)

```mermaid
sequenceDiagram
    participant User
    participant Frontend as app.js
    participant API as FastAPI
    participant Ingestion as ingestion.py
    participant Processing as processing.py
    participant Embed as embeddings.py
    participant Chroma as ChromaDB
    participant Summ as summarize.py
    participant Groq as Groq API
    participant DB as SQLite

    User->>Frontend: Click "+ Add to library"
    Frontend->>API: POST /library/add-from-search
    API->>Ingestion: fetch_arxiv_full_text(pdf_url)
    Ingestion-->>API: full paper text
    API->>DB: INSERT paper row (in_library=1)
    API-->>Frontend: 200 OK { id }
    Note over API: Background task starts — response already returned
    API->>Processing: detect_sections + chunk_sections
    Processing->>Embed: index_paper_chunks(chunks)
    Embed->>Chroma: store chunk vectors
    API->>Summ: summarize_paper(paper_id)
    Summ->>Embed: query_chunks(topic query)
    Embed->>Chroma: similarity search
    Chroma-->>Summ: top-k relevant chunks
    Summ->>Groq: single combined prompt (all 4 fields, JSON)
    Groq-->>Summ: structured JSON response
    Summ->>DB: UPDATE methodology, findings, research_gap, future_work
    Frontend->>API: GET /library (polls every 4s while processing)
    API-->>Frontend: updated paper row
    Frontend->>User: Summary fields render in UI
```

### Asking a question about a paper

```mermaid
sequenceDiagram
    participant User
    participant Frontend as app.js
    participant API as FastAPI
    participant Rag as rag.py
    participant Embed as embeddings.py
    participant Chroma as ChromaDB
    participant Groq as Groq API

    User->>Frontend: Types question, clicks Ask
    Frontend->>API: POST /ask { question, paper_ids }
    API->>Rag: ask(question, paper_ids)
    Rag->>Embed: query_chunks(question, k=6, paper_ids)
    Embed->>Chroma: similarity search scoped to paper(s)
    Chroma-->>Rag: top-k relevant chunks + metadata
    Rag->>Rag: build_context() — format chunks as [Source N]
    Rag->>Groq: call_llm(prompt) — with retry/backoff
    Groq-->>Rag: generated answer
    Rag-->>API: { answer, sources }
    API-->>Frontend: JSON response
    Frontend->>User: Renders answer + source chips
```

---

## Project Structure

```
echo-app/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI app, all route definitions
│   │   ├── rag.py           # LLM client + call_llm() (retry/backoff), ask()
│   │   ├── summarize.py     # 4-field structured summary generation
│   │   ├── contribute.py    # Idea-to-paper embedding similarity matching
│   │   ├── ingestion.py     # arXiv search + PDF text extraction
│   │   ├── processing.py    # Section detection + text chunking
│   │   ├── embeddings.py    # sentence-transformers + ChromaDB indexing/query
│   │   └── db.py            # SQLite connection + schema
│   ├── chroma_db/           # ChromaDB persistent vector store (gitignored)
│   ├── .env                 # API keys (gitignored — never commit)
│   ├── .gitignore
│   └── requirements.txt
└── frontend/
    ├── echo_app.html        # Single-page app shell (all screens, all styles)
    └── app.js                # All frontend logic (routing, rendering, API calls)
```

---

## Setup & Installation

### Prerequisites
- Python 3.11+ (developed against 3.13)
- A free [Groq API key](https://console.groq.com)

### Backend

```powershell
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Create `backend/.env`:
```
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-120b
```

Run the server:
```powershell
uvicorn app.main:app --reload --port 8000
```

Verify: open `http://localhost:8000/health` → expect `{"status":"ok"}`.

### Frontend

```powershell
cd frontend
python -m http.server 5500
```

Open `http://localhost:5500/echo_app.html`.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GROQ_API_KEY` | Yes | — | API key from console.groq.com |
| `GROQ_MODEL` | No | `openai/gpt-oss-120b` | Model used for summarization + chat |

---

## API Reference

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/search` | Search arXiv (`query`, `max_results`, `year_from`, `year_to`) |
| `POST` | `/library/add-from-search` | Add a search result to the library, triggers background processing |
| `POST` | `/library/upload` | Upload a PDF, triggers background processing |
| `GET` | `/library` | List all papers in the library |
| `DELETE` | `/library/{paper_id}` | Remove a paper (soft-delete + purge its vectors) |
| `GET` | `/paper/{paper_id}` | Get a single paper's full record |
| `POST` | `/ask` | Ask a question, scoped to one or more papers |
| `POST` | `/compare` | Fetch full records for 2+ papers to compare |
| `POST` | `/contribute` | Match a described idea against the library |
| `POST` | `/feedback` | Record a thumbs up/down vote on a result |

---

## Design Decisions

- **Single combined LLM call for summaries, not four.** Early versions made one API call per
  summary field. This was consolidated into one JSON-structured call, cutting API usage 4x —
  important on free-tier rate limits.
- **Retry with backoff, centralized in `rag.py`.** Every LLM call (chat and summarization)
  goes through one `call_llm()` function that retries on rate limits (429) and on truncated
  responses (reasoning models can run out of token budget mid-answer), rather than duplicating
  retry logic per feature.
- **Background processing, not blocking requests.** Adding a paper returns immediately with an
  ID; indexing and summarization happen as a FastAPI background task, and the frontend polls
  `/library` until every field is populated.
- **No frontend framework.** The UI is intentionally plain HTML/CSS/JS — no build step, no
  bundler, runs from a static file server.

---

## Known Limitations & Roadmap

- **Citation-network visualization and cross-corpus trend prediction** were scoped out of this
  version; they would require a citation graph data source beyond arXiv's API.
- **Single-user, local-first design.** SQLite and local ChromaDB storage are not intended for
  concurrent multi-user access; a hosted multi-user version would need to migrate to a hosted
  Postgres/pgvector or managed vector DB.
- **LLM provider is swappable but not multi-provider at runtime.** Currently one provider is
  configured via `.env` at a time.

---

## Security Notes

- `.env` is gitignored and must never be committed. If a key is ever accidentally committed,
  rotating the key immediately is the only real fix — removing it from a later commit does not
  invalidate a key that was already exposed, and git history must be scrubbed
  (`git filter-repo`) in addition to rotation.
- The app currently has no authentication layer — it's designed to run locally on `localhost`
  for a single user. Do not expose the backend directly to the public internet without adding
  an auth layer first.