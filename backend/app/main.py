import datetime
import logging
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

# --- Logging ---------------------------------------------------------------
# Render captures stdout as logs automatically. Every stage of the
# add/process pipeline logs here so a stuck or crashed paper is always
# traceable by its paper_id, instead of silently vanishing.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("echo")

app = FastAPI(title="Echo API")

allowed_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5500,http://127.0.0.1:5500"
).split(",")
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["*"], allow_headers=["*"])

db.init_db()

# --- Google authentication -------------------------------------------------
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
    try:
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
    except Exception:
        logger.exception(f"[{paper_id}] _store_paper failed (title={title!r})")
        raise
    finally:
        conn.close()


import threading

_processing_lock = threading.Lock()


def _index_and_summarize(paper_id, user_id, full_text):
    logger.info(f"[{paper_id}] waiting for processing lock (queue depth may cause delay)")
    with _processing_lock:
        logger.info(f"[{paper_id}] acquired lock, starting chunk/embed/summarize")
        try:
            sections = processing.detect_sections(full_text)
            logger.info(f"[{paper_id}] detected {len(sections)} sections")

            chunks = processing.chunk_sections(sections)
            logger.info(f"[{paper_id}] produced {len(chunks)} chunks")

            embeddings.index_paper_chunks(paper_id, chunks, user_id)
            logger.info(f"[{paper_id}] embeddings indexed")

            if chunks:
                summarize.summarize_paper(paper_id, user_id)
                logger.info(f"[{paper_id}] summary complete")
            else:
                logger.warning(f"[{paper_id}] zero chunks produced, skipping summarization")
                conn = db.get_conn()
                conn.execute("UPDATE papers SET research_gap=? WHERE id=?", ("No extractable text found.", paper_id))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.exception(f"[{paper_id}] processing failed during chunk/embed/summarize")
            conn = db.get_conn()
            conn.execute("UPDATE papers SET research_gap=? WHERE id=?", (f"Processing failed: {e}", paper_id))
            conn.commit()
            conn.close()


def _fetch_index_and_summarize(paper_id, user_id, pdf_url, abstract):
    logger.info(f"[{paper_id}] background task started, pdf_url={pdf_url!r}")
    try:
        full_text = ingestion.fetch_arxiv_full_text(pdf_url) if pdf_url else ""
        logger.info(f"[{paper_id}] fetched {len(full_text)} chars from PDF")
        if not full_text.strip():
            logger.warning(f"[{paper_id}] PDF text empty, falling back to abstract")
            full_text = abstract
        full_text = full_text.replace("\x00", "")
        conn = db.get_conn()
        conn.execute("UPDATE papers SET full_text=? WHERE id=?", (full_text, paper_id))
        conn.commit()
        conn.close()
        logger.info(f"[{paper_id}] full_text saved ({len(full_text)} chars), handing off to indexer")
    except Exception as e:
        logger.exception(f"[{paper_id}] fetch/store stage failed")
        conn = db.get_conn()
        conn.execute("UPDATE papers SET research_gap=? WHERE id=?", (f"Processing failed: {e}", paper_id))
        conn.commit()
        conn.close()
        return
    _index_and_summarize(paper_id, user_id, full_text)


def _owned_paper_ids(user_id, requested_ids):
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
    logger.info(f"[{paper_id}] add-from-search request received: title={req.title!r}, pdf_url={req.pdf_url!r}")
    try:
        _store_paper(paper_id, user["id"], req.title, ",".join(req.authors), req.year, req.venue, req.source, req.doi, req.pdf_url, req.abstract)
    except Exception as e:
        # Previously this exception propagated as a bare 500 with no way for
        # the frontend (or us) to know what happened. Now it's logged in
        # full AND reported back so the "Add" button can show a real error
        # instead of a silent/generic failure.
        logger.exception(f"[{paper_id}] add_from_search failed at store stage")
        return {"error": f"Failed to save paper: {e}"}
    background_tasks.add_task(_fetch_index_and_summarize, paper_id, user["id"], req.pdf_url, req.abstract)
    logger.info(f"[{paper_id}] stored successfully, background task queued")
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
    try:
        _store_paper(paper_id, user["id"], file.filename, "Uploaded by you", "", "", "upload", "", "", text)
    except Exception as e:
        logger.exception(f"[{paper_id}] upload failed at store stage")
        return {"error": f"Failed to save paper: {e}"}
    background_tasks.add_task(_index_and_summarize, paper_id, user["id"], text)
    logger.info(f"[{paper_id}] upload stored, background task queued")
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
    logger.info(f"[{paper_id}] removed from library")
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
    try:
        if req.paper_ids:
            owned_ids = _owned_paper_ids(user["id"], req.paper_ids)
            return rag.ask(req.question, paper_ids=owned_ids, user_id=user["id"], whole_library=False)
        else:
            owned_ids = [p["id"] for p in _owned_library_rows(user["id"])]
            return rag.ask(req.question, paper_ids=owned_ids, user_id=user["id"], whole_library=True)
    except Exception as e:
        logger.exception(f"/ask failed for user={user['id']}")
        return {"answer": f"Something went wrong reaching Echo: {e}", "sources": []}


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
    owned_papers = _owned_library_rows(user["id"])
    return analysis.library_diversity(owned_papers)