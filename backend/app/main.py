import datetime
import os
import time
import uuid
from collections import defaultdict
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel, Field

from . import analysis, contribute, db, embeddings, ingestion, processing, rag, relevance, summarize

app = FastAPI(title="Echo API")

allowed_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5500,http://127.0.0.1:5500"
).split(",")
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["*"], allow_headers=["*"])

db.init_db()

# --- Google authentication -------------------------------------------------
# Every route (except /health and /config/relevance) requires a valid Google
# ID token in the Authorization header: "Authorization: Bearer <token>". The
# token is verified against Google's public keys (no secret needed on our
# side). The verified 'sub' claim becomes the user's permanent user_id, used
# to scope every database and vector-store query so users can never see or
# retrieve each other's data.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")


def get_current_user(authorization: str = Header(default=None)):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Server misconfigured: GOOGLE_CLIENT_ID not set.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired Google token.")

    user_id = idinfo["sub"]
    db.upsert_user(user_id, idinfo.get("email"), idinfo.get("name"), idinfo.get("picture"))
    return {"id": user_id, "email": idinfo.get("email"), "name": idinfo.get("name"), "picture": idinfo.get("picture")}


class SearchReq(BaseModel):
    query: str
    max_results: int = 15
    year_from: Optional[str] = None
    year_to: Optional[str] = None


class AskReq(BaseModel):
    question: str = Field(..., max_length=2000)
    paper_ids: Optional[List[str]] = None


class CompareReq(BaseModel):
    paper_ids: List[str]


class ContribReq(BaseModel):
    idea: str = Field(..., max_length=2000)
    paper_ids: List[str]


class FeedbackReq(BaseModel):
    target_type: str
    target_id: str
    vote: str


class AddFromSearchReq(BaseModel):
    id: Optional[str] = None
    title: str
    abstract: str = ""
    authors: List[str] = []
    year: str = ""
    venue: str = ""
    source: str = "arXiv"
    doi: str = ""
    pdf_url: str = ""


def _store_paper(paper_id, user_id, title, authors, year, venue, source, doi, pdf_url, full_text):
    conn = db.get_conn()
    # NOTE: "INSERT OR REPLACE" is SQLite-only syntax; Postgres uses
    # "INSERT ... ON CONFLICT (id) DO UPDATE SET ..." with the same
    # "excluded" pseudo-table for referencing the incoming values.
    conn.execute(
        """INSERT INTO papers
           (id, user_id, title, authors, year, venue, source, doi, pdf_url, tags, full_text, in_library, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT (id) DO UPDATE SET
             user_id=excluded.user_id, title=excluded.title, authors=excluded.authors,
             year=excluded.year, venue=excluded.venue, source=excluded.source, doi=excluded.doi,
             pdf_url=excluded.pdf_url, tags=excluded.tags, full_text=excluded.full_text,
             in_library=excluded.in_library, created_at=excluded.created_at""",
        (paper_id, user_id, title, authors, year, venue, source, doi, pdf_url, "", full_text,
         datetime.datetime.now(datetime.UTC).isoformat()),
    )
    conn.commit()
    conn.close()


def _index_and_summarize(paper_id, user_id, full_text):
    """Runs in the background after the paper row already exists, so a
    summarization failure never hides the paper from the library."""
    try:
        sections = processing.detect_sections(full_text)
        chunks = processing.chunk_sections(sections)
        embeddings.index_paper_chunks(paper_id, chunks, user_id)
        if chunks:
            summarize.summarize_paper(paper_id, user_id)
    except Exception as e:
        conn = db.get_conn()
        conn.execute("UPDATE papers SET research_gap=? WHERE id=?", (f"Processing failed: {e}", paper_id))
        conn.commit()
        conn.close()


def _fetch_index_and_summarize(paper_id, user_id, pdf_url, abstract):
    """Background-only entry point for search-added papers: downloading and
    parsing the actual PDF happens here instead of inside the request
    handler. A slow or large PDF can take 20-30+ seconds — long enough to
    exceed Render's own reverse-proxy timeout, which then returns its own
    timeout page with no CORS headers at all. The browser reports that as a
    'blocked by CORS policy' error, which is misleading — the real cause is
    the request simply taking too long, not a CORS misconfiguration. Moving
    this work to the background means the initial POST response is nearly
    instant regardless of how slow any individual PDF is."""
    full_text = ingestion.fetch_arxiv_full_text(pdf_url) if pdf_url else ""
    if not full_text.strip():
        full_text = abstract
    conn = db.get_conn()
    conn.execute("UPDATE papers SET full_text=? WHERE id=?", (full_text, paper_id))
    conn.commit()
    conn.close()
    _index_and_summarize(paper_id, user_id, full_text)


def _owned_paper_ids(user_id, requested_ids):
    """Filters a list of paper_ids down to only those the requesting user
    actually owns — prevents one user from reading/comparing another user's
    papers just by guessing/reusing an ID."""
    if not requested_ids:
        return []
    conn = db.get_conn()
    placeholders = ",".join("?" for _ in requested_ids)
    rows = conn.execute(
        f"SELECT id FROM papers WHERE id IN ({placeholders}) AND user_id=?",
        (*requested_ids, user_id),
    ).fetchall()
    conn.close()
    return [r["id"] for r in rows]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config/relevance")
def get_relevance_config():
    """Exposes the same thresholds/labels analysis.py uses server-side, so
    the frontend never hardcodes a threshold number that could drift out of
    sync with relevance.py (the single source of truth). No auth required —
    this is static config, not user data, and the frontend fetches it at
    boot before sign-in completes."""
    return relevance.config_payload()


@app.post("/search", dependencies=[Depends(get_current_user)])
def search(req: SearchReq):
    results = ingestion.search_arxiv(req.query, max_results=req.max_results, year_from=req.year_from, year_to=req.year_to)
    return {"results": results}


MAX_LIBRARY_PAPERS = 5


def _library_count(user_id):
    conn = db.get_conn()
    count = conn.execute(
        "SELECT COUNT(*) as c FROM papers WHERE in_library=1 AND user_id=?", (user_id,)
    ).fetchone()["c"]
    conn.close()
    return count


@app.post("/library/add-from-search")
def add_from_search(req: AddFromSearchReq, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    if _library_count(user["id"]) >= MAX_LIBRARY_PAPERS:
        return {"error": f"Library limit reached ({MAX_LIBRARY_PAPERS} papers). Remove a paper before adding another."}
    paper_id = req.id or str(uuid.uuid4())
    # Store immediately with the abstract as a safe placeholder — the real
    # full text (if the PDF download+extraction succeeds) is filled in by
    # the background task. This keeps the request itself fast no matter how
    # long that specific PDF takes to fetch and parse.
    _store_paper(paper_id, user["id"], req.title, ",".join(req.authors), req.year, req.venue, req.source, req.doi, req.pdf_url, req.abstract)
    background_tasks.add_task(_fetch_index_and_summarize, paper_id, user["id"], req.pdf_url, req.abstract)
    return {"id": paper_id}


MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


@app.post("/library/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...), user=Depends(get_current_user)):
    if _library_count(user["id"]) >= MAX_LIBRARY_PAPERS:
        return {"error": f"Library limit reached ({MAX_LIBRARY_PAPERS} papers). Remove a paper before adding another."}
    if file.content_type != "application/pdf":
        return {"error": "Only PDF files are accepted."}
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        return {"error": "File too large (25 MB limit)."}
    if not content.startswith(b"%PDF-"):
        return {"error": "File does not appear to be a valid PDF."}
    text = ingestion.extract_pdf_text(content)
    paper_id = str(uuid.uuid4())
    _store_paper(paper_id, user["id"], file.filename, "Uploaded by you", "", "", "upload", "", "", text)
    background_tasks.add_task(_index_and_summarize, paper_id, user["id"], text)
    return {"id": paper_id}


@app.get("/library")
def get_library(user=Depends(get_current_user)):
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM papers WHERE in_library=1 AND user_id=? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()
    conn.close()
    return {"papers": [dict(r) for r in rows]}


@app.delete("/library/{paper_id}")
def remove(paper_id: str, user=Depends(get_current_user)):
    conn = db.get_conn()
    conn.execute("UPDATE papers SET in_library=0 WHERE id=? AND user_id=?", (paper_id, user["id"]))
    conn.commit()
    conn.close()
    embeddings.delete_paper_chunks(paper_id)
    return {"ok": True}


@app.get("/paper/{paper_id}")
def get_paper(paper_id: str, user=Depends(get_current_user)):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM papers WHERE id=? AND user_id=?", (paper_id, user["id"])).fetchone()
    conn.close()
    return dict(row) if row else {"error": "not found"}


# --- Rate limiting ----------------------------------------------------------
_rate_limit_hits = defaultdict(list)
_global_hits = []
RATE_LIMIT_MAX = 20
RATE_LIMIT_WINDOW = 60
GLOBAL_LIMIT_MAX = 100


def _check_rate_limit(user_id, bucket):
    now = time.time()
    global _global_hits
    _global_hits = [t for t in _global_hits if now - t < RATE_LIMIT_WINDOW]
    if len(_global_hits) >= GLOBAL_LIMIT_MAX:
        return False
    _global_hits.append(now)

    key = f"{bucket}:{user_id}"
    _rate_limit_hits[key] = [t for t in _rate_limit_hits[key] if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limit_hits[key]) >= RATE_LIMIT_MAX:
        return False
    _rate_limit_hits[key].append(now)
    return True


@app.post("/ask")
def ask(req: AskReq, user=Depends(get_current_user)):
    if not _check_rate_limit(user["id"], "ask"):
        return {"answer": "Rate limit reached — please wait a minute and try again.", "sources": []}
    if req.paper_ids:
        owned_ids = _owned_paper_ids(user["id"], req.paper_ids)
        return rag.ask(req.question, paper_ids=owned_ids, user_id=user["id"], whole_library=False)
    else:
        owned_ids = [p["id"] for p in _owned_library_rows(user["id"])]
        return rag.ask(req.question, paper_ids=owned_ids, user_id=user["id"], whole_library=True)


@app.post("/compare")
def compare(req: CompareReq, user=Depends(get_current_user)):
    owned_ids = _owned_paper_ids(user["id"], req.paper_ids)
    if len(owned_ids) < 2:
        return {"error": "Select at least 2 papers to compare."}
    conn = db.get_conn()
    placeholders = ",".join("?" for _ in owned_ids)
    rows = conn.execute(f"SELECT * FROM papers WHERE id IN ({placeholders}) AND user_id=?", (*owned_ids, user["id"])).fetchall()
    conn.close()
    return {"papers": [dict(r) for r in rows]}


@app.post("/contribute")
def contribute_ep(req: ContribReq, user=Depends(get_current_user)):
    if not _check_rate_limit(user["id"], "contribute"):
        return {"error": "Rate limit reached — please wait a minute and try again."}
    owned_ids = _owned_paper_ids(user["id"], req.paper_ids)
    result = contribute.match_idea(req.idea, owned_ids, user["id"])
    return result or {"error": "No matches found — add papers to your library first."}


@app.post("/feedback")
def feedback(req: FeedbackReq, user=Depends(get_current_user)):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO feedback (user_id, target_type, target_id, vote, created_at) VALUES (?, ?, ?, ?, ?)",
        (user["id"], req.target_type, req.target_id, req.vote, datetime.datetime.now(datetime.UTC).isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


def _owned_library_rows(user_id):
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, title, methodology, findings FROM papers WHERE in_library=1 AND user_id=?", (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/analyze")
def analyze(req: AskReq, user=Depends(get_current_user)):
    """Structural analysis of the user's library against a question: which
    papers (and which SECTIONS of them) are most relevant, which sub-topics
    of the question are covered vs. missing, all computed locally via
    embeddings — costs exactly one LLM call (question decomposition), not
    one per paper, so it stays cheap regardless of library size."""
    if not _check_rate_limit(user["id"], "analyze"):
        return {"error": "Rate limit reached — please wait a minute and try again."}

    owned_papers = _owned_library_rows(user["id"])
    if not owned_papers:
        return {"error": "Add papers to your library first."}

    result = analysis.analyze_library(req.question, owned_papers, user["id"])
    result["titles"] = {p["id"]: p["title"] for p in owned_papers}
    return result


@app.get("/library/health")
def library_health(user=Depends(get_current_user)):
    """Static diversity score for the user's current library — no question
    needed, no LLM call, purely local embedding comparison."""
    owned_papers = _owned_library_rows(user["id"])
    return analysis.library_diversity(owned_papers)