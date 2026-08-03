# Echo — AI Research Assistant

A real, working backend (FastAPI + RAG pipeline) and frontend for Echo. See `backend/DECISIONS.md` for every autonomous build choice and why.

## What's real vs. what to know before running

Everything here is genuine, working code — not mock data. Two things are worth knowing before you run it:

1. **You need an Anthropic API key.** Set it as an environment variable before starting the backend:
   ```
   export ANTHROPIC_API_KEY="your-key-here"
   ```
2. **The embedding model downloads automatically on first run.** `sentence-transformers` fetches `all-MiniLM-L6-v2` from Hugging Face the first time you index a paper — this needs normal internet access and happens once (it's cached after that). This project was built in a sandboxed environment that couldn't reach Hugging Face, so this specific step could not be tested live there — but the code is standard, well-established usage and will work on any machine with regular internet access.

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-key-here"
uvicorn app.main:app --reload --port 8000
```

The API is now running at `http://localhost:8000`. Visit `http://localhost:8000/health` to confirm.

## Running the frontend

The frontend is a single static HTML file — no build step needed.

```bash
cd frontend
python3 -m http.server 5500
```

Open `http://localhost:5500/echo_app.html` in your browser. If your backend runs somewhere other than `localhost:8000`, set it before the page loads:
```html
<script>window.ECHO_API_BASE = 'http://your-backend-host:8000';</script>
```
(add this line just before the closing `</head>` tag, or before the main `<script>` block).

## What's tested vs. untested

Tested inside the sandbox this was built in:
- PDF text extraction (PyMuPDF)
- Section detection and chunking logic
- SQLite schema and read/write
- ChromaDB storage and semantic retrieval (using a stand-in embedding, since the real model couldn't be downloaded in that sandbox)
- The RAG endpoint's retrieval path end to end
- Frontend JavaScript syntax

Not testable in that sandbox, but standard/expected to work normally:
- Live arXiv search (needs `export.arxiv.org` reachable — blocked in the build sandbox only)
- The real `sentence-transformers` embedding model (needs `huggingface.co` reachable — blocked in the build sandbox only)
- A real Anthropic API call (no API key was available in the build sandbox — you'll use your own)

**Recommended first real test once you have it running:** search for a topic, add one paper, wait for it to summarize, then open it in "Paper & Ask" and ask it a question. That single flow exercises the entire pipeline — ingestion, chunking, embedding, retrieval, and generation — in one pass.

## Project structure

```
echo-app/
  backend/
    DECISIONS.md          — every autonomous build decision, logged with reasons
    requirements.txt
    app/
      main.py              — FastAPI routes
      db.py                 — SQLite schema + connection
      ingestion.py          — arXiv search + PDF text extraction
      processing.py         — section detection + chunking
      embeddings.py         — sentence-transformers + ChromaDB
      rag.py                — the one retrieve-and-generate function everything else calls
      summarize.py          — 4-field structured summaries, built on rag.py
      contribute.py         — idea-to-paper matching via embedding similarity
  frontend/
    echo_app.html          — the full UI, wired to live API calls
```

## Known scope boundaries (by design, not oversights)

- **Auth**: skipped for v1 — single local user, per the build spec.
- **Retrieval sources**: arXiv only for now (Phase 1 spec explicitly allows one source to start; OpenAlex/Semantic Scholar/Crossref are a Phase 2 addition).
- **Credibility scoring**: not yet wired in — needs the Phase 2 retrieval sources above.
- **Citation-network graph & trend prediction**: intentionally stubbed as a disabled "Trends — coming soon" nav item, not built. This was explicitly out of scope for v1 in the build spec.
