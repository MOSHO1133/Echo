import contextlib
import datetime
import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel, Field

from . import analysis, contribute, db, embeddings, ingestion, processing, rag, relevance, summarize

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


# --- Global safety net -------------------------------------------------
# Nothing should ever bare-500 without being logged and without the
# frontend getting a clean, parseable error. This catches anything that
# slips past every other try/except in the file (e.g. a bug in a library
# we call into).
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception on {request.method} {request.url.path}")
    return JSONResponse(status_code=500, content={"error": "Internal server error. Please try again."})


# --- DB connection helper ------------------------------------------------
# The original code did `conn = db.get_conn()` ... `conn.close()` with no
# try/finally in most places. If anything raised between those two lines,
# the connection never closed — a slow leak that, after enough failures,
# exhausts the pool and causes unrelated requests to start failing too.
# This guarantees closure no matter what.
@contextlib.contextmanager
def db_conn():
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


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
    try:
        db.upsert_user(user_id, idinfo.get("email"), idinfo.get("name"), idinfo.get("picture"))
    except Exception:
        logger.exception(f"upsert_user failed for {user_id}")
        raise HTTPException(status_code=500, detail="Failed to sync user record.")
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
    full_text = (full_text or "").replace("\x00", "")
    with db_conn() as conn:
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


def _set_paper_error(paper_id, message):
    try:
        with db_conn() as conn:
            conn.execute("UPDATE papers SET research_gap=? WHERE id=?", (message, paper_id))
            conn.commit()
    except Exception:
        # If even the error-write fails, log it but don't raise from a
        # background thread — there's nothing left to report to.
        logger.exception(f"[{paper_id}] failed to write error status")


# Serializes the ENTIRE per-paper pipeline (download, chunk, embed,
# summarize) — not just embed/summarize — so only one paper is ever doing
# any work at all, regardless of batch size. Render's free-tier 512MB limit
# was exceeded even with only the embed/summarize stage serialized, because
# concurrent PDF downloads for a batch add still overlapped and spiked
# memory independently. This is the single biggest lever on this tier.
_processing_lock = threading.Lock()

# Separate, short-lived lock just for the count-check-then-insert sequence
# in add-from-search / upload, so two near-simultaneous "Add" clicks can't
# both pass the library-limit check before either row is inserted.
_library_limit_lock = threading.Lock()

MAX_LIBRARY_PAPERS = 5
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


def _library_count(user_id):
    with db_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) as c FROM papers WHERE in_library=1 AND user_id=?", (user_id,)
        ).fetchone()["c"]


def _run_pipeline(paper_id, user_id, pdf_url, abstract):
    """Full per-paper pipeline, run under the global processing lock so
    only one paper is ever downloading/chunking/embedding/summarizing at
    a time across the whole process."""
    logger.info(f"[{paper_id}] queued, waiting for processing lock")
    with _processing_lock:
        logger.info(f"[{paper_id}] acquired lock, fetching PDF: {pdf_url!r}")
        try:
            full_text = ingestion.fetch_arxiv_full_text(pdf_url) if pdf_url else ""
            logger.info(f"[{paper_id}] fetched {len(full_text)} chars from PDF")
        except Exception as e:
            logger.exception(f"[{paper_id}] PDF fetch failed, falling back to abstract")
            full_text = ""

        if not full_text.strip():
            logger.warning(f"[{paper_id}] no usable PDF text, using abstract instead")
            full_text = abstract or ""

        full_text = full_text.replace("\x00", "")

        try:
            with db_conn() as conn:
                conn.execute("UPDATE papers SET full_text=? WHERE id=?", (full_text, paper_id))
                conn.commit()
            logger.info(f"[{paper_id}] full_text saved ({len(full_text)} chars)")
        except Exception as e:
            logger.exception(f"[{paper_id}] failed to save full_text")
            _set_paper_error(paper_id, f"Processing failed: {e}")
            return

        try:
            sections = processing.detect_sections(full_text)
            logger.info(f"[{paper_id}] detected {len(sections)} sections")

            chunks = processing.chunk_sections(sections)
            logger.info(f"[{paper_id}] produced {len(chunks)} chunks")

            if not chunks:
                logger.warning(f"[{paper_id}] zero chunks, marking no extractable content")
                _set_paper_error(paper_id, "No extractable text found.")
                return

            embeddings.index_paper_chunks(paper_id, chunks, user_id)
            logger.info(f"[{paper_id}] embeddings indexed")

            summarize.summarize_paper(paper_id, user_id)
            logger.info(f"[{paper_id}] summary complete")
        except Exception as e:
            logger.exception(f"[{paper_id}] chunk/embed/summarize stage failed")
            _set_paper_error(paper_id, f"Processing failed: {e}")


def _run_upload_pipeline(paper_id, user_id, full_text):
    """Same lock, same guarantees, for directly-uploaded PDFs (text is
    already extracted by the time this runs, so no download stage)."""
    logger.info(f"[{paper_id}] queued (upload), waiting for processing lock")
    with _processing_lock:
        logger.info(f"[{paper_id}] acquired lock, starting chunk/embed/summarize")
        try:
            sections = processing.detect_sections(full_text)
            logger.info(f"[{paper_id}] detected {len(sections)} sections")

            chunks = processing.chunk_sections(sections)
            logger.info(f"[{paper_id}] produced {len(chunks)} chunks")

            if not chunks:
                logger.warning(f"[{paper_id}] zero chunks, marking no extractable content")
                _set_paper_error(paper_id, "No extractable text found.")
                return

            embeddings.index_paper_chunks(paper_id, chunks, user_id)
            logger.info(f"[{paper_id}] embeddings indexed")

            summarize.summarize_paper(paper_id, user_id)
            logger.info(f"[{paper_id}] summary complete")
        except Exception as e:
            logger.exception(f"[{paper_id}] chunk/embed/summarize stage failed")
            _set_paper_error(paper_id, f"Processing failed: {e}")


def _owned_paper_ids(user_id, requested_ids):
    if not requested_ids:
        return []
    with db_conn() as conn:
        placeholders = ",".join("?" for _ in requested_ids)
        rows = conn.execute(
            f"SELECT id FROM papers WHERE id IN ({placeholders}) AND user_id=?",
            (*requested_ids, user_id),
        ).fetchall()
        return [r["id"] for r in rows]


def _owned_library_rows(user_id):
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, methodology, findings FROM papers WHERE in_library=1 AND user_id=?", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config/relevance")
def get_relevance_config():
    return relevance.config_payload()


@app.post("/search", dependencies=[Depends(get_current_user)])
def search(req: SearchReq):
    try:
        results = ingestion.search_arxiv(req.query, max_results=req.max_results, year_from=req.year_from, year_to=req.year_to)
        return {"results": results}
    except Exception as e:
        logger.exception("search failed")
        return {"error": f"Search failed: {e}", "results": []}


@app.post("/library/add-from-search")
def add_from_search(req: AddFromSearchReq, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    paper_id = req.id or str(uuid.uuid4())
    logger.info(f"[{paper_id}] add-from-search request received: title={req.title!r}")

    with _library_limit_lock:
        try:
            count = _library_count(user["id"])
        except Exception as e:
            logger.exception(f"[{paper_id}] failed to check library count")
            return {"error": "Could not verify library status. Please try again."}

        if count >= MAX_LIBRARY_PAPERS:
            logger.info(f"[{paper_id}] rejected: library limit reached ({count})")
            return {"error": f"Library limit reached ({MAX_LIBRARY_PAPERS} papers). Remove a paper before adding another."}

        try:
            _store_paper(paper_id, user["id"], req.title, ",".join(req.authors), req.year, req.venue, req.source, req.doi, req.pdf_url, req.abstract)
        except Exception as e:
            logger.exception(f"[{paper_id}] add_from_search failed at store stage")
            return {"error": f"Failed to save paper: {e}"}

    background_tasks.add_task(_run_pipeline, paper_id, user["id"], req.pdf_url, req.abstract)
    logger.info(f"[{paper_id}] stored successfully, background task queued")
    return {"id": paper_id}


@app.post("/library/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...), user=Depends(get_current_user)):
    if file.content_type != "application/pdf":
        return {"error": "Only PDF files are accepted."}

    try:
        content = await file.read()
    except Exception as e:
        logger.exception("upload file read failed")
        return {"error": "Failed to read uploaded file."}

    if len(content) > MAX_UPLOAD_BYTES:
        return {"error": "File too large (25 MB limit)."}
    if not content.startswith(b"%PDF-"):
        return {"error": "File does not appear to be a valid PDF."}

    try:
        text = ingestion.extract_pdf_text(content)
    except Exception as e:
        logger.exception("PDF text extraction failed on upload")
        return {"error": f"Could not read this PDF: {e}"}

    paper_id = str(uuid.uuid4())

    with _library_limit_lock:
        try:
            count = _library_count(user["id"])
        except Exception:
            logger.exception(f"[{paper_id}] failed to check library count")
            return {"error": "Could not verify library status. Please try again."}

        if count >= MAX_LIBRARY_PAPERS:
            return {"error": f"Library limit reached ({MAX_LIBRARY_PAPERS} papers). Remove a paper before adding another."}

        try:
            _store_paper(paper_id, user["id"], file.filename, "Uploaded by you", "", "", "upload", "", "", text)
        except Exception as e:
            logger.exception(f"[{paper_id}] upload failed at store stage")
            return {"error": f"Failed to save paper: {e}"}

    background_tasks.add_task(_run_upload_pipeline, paper_id, user["id"], text)
    logger.info(f"[{paper_id}] upload stored, background task queued")
    return {"id": paper_id}


@app.get("/library")
def get_library(user=Depends(get_current_user)):
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM papers WHERE in_library=1 AND user_id=? ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
        return {"papers": [dict(r) for r in rows]}


@app.delete("/library/{paper_id}")
def remove(paper_id: str, user=Depends(get_current_user)):
    with db_conn() as conn:
        conn.execute("UPDATE papers SET in_library=0 WHERE id=? AND user_id=?", (paper_id, user["id"]))
        conn.commit()
    try:
        embeddings.delete_paper_chunks(paper_id)
    except Exception:
        logger.exception(f"[{paper_id}] failed to delete embedding chunks (paper still removed from library)")
    logger.info(f"[{paper_id}] removed from library")
    return {"ok": True}


@app.get("/paper/{paper_id}")
def get_paper(paper_id: str, user=Depends(get_current_user)):
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM papers WHERE id=? AND user_id=?", (paper_id, user["id"])).fetchone()
        return dict(row) if row else {"error": "not found"}


# --- Rate limiting ----------------------------------------------------------
_rate_limit_hits = defaultdict(list)
_global_hits = []
_rate_lock = threading.Lock()
RATE_LIMIT_MAX = 20
RATE_LIMIT_WINDOW = 60
GLOBAL_LIMIT_MAX = 100
_last_cleanup = 0
CLEANUP_INTERVAL = 300  # sweep stale per-user keys every 5 minutes


def _check_rate_limit(user_id, bucket):
    global _last_cleanup
    now = time.time()
    with _rate_lock:
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

        # Prevents _rate_limit_hits from growing forever as new users hit
        # the API over the app's lifetime — without this, every distinct
        # user_id+bucket combination that ever made one request stays in
        # memory permanently, even with an empty list.
        if now - _last_cleanup > CLEANUP_INTERVAL:
            for k in [k for k, v in _rate_limit_hits.items() if not v]:
                del _rate_limit_hits[k]
            _last_cleanup = now

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
    try:
        owned_ids = _owned_paper_ids(user["id"], req.paper_ids)
        if len(owned_ids) < 2:
            return {"error": "Select at least 2 papers to compare."}
        with db_conn() as conn:
            placeholders = ",".join("?" for _ in owned_ids)
            rows = conn.execute(f"SELECT * FROM papers WHERE id IN ({placeholders}) AND user_id=?", (*owned_ids, user["id"])).fetchall()
            return {"papers": [dict(r) for r in rows]}
    except Exception as e:
        logger.exception(f"/compare failed for user={user['id']}")
        return {"error": f"Comparison failed: {e}"}


@app.post("/contribute")
def contribute_ep(req: ContribReq, user=Depends(get_current_user)):
    if not _check_rate_limit(user["id"], "contribute"):
        return {"error": "Rate limit reached — please wait a minute and try again."}
    try:
        owned_ids = _owned_paper_ids(user["id"], req.paper_ids)
        result = contribute.match_idea(req.idea, owned_ids, user["id"])
        return result or {"error": "No matches found — add papers to your library first."}
    except Exception as e:
        logger.exception(f"/contribute failed for user={user['id']}")
        return {"error": f"Contribute failed: {e}"}


@app.post("/feedback")
def feedback(req: FeedbackReq, user=Depends(get_current_user)):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO feedback (user_id, target_type, target_id, vote, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], req.target_type, req.target_id, req.vote, datetime.datetime.now(datetime.UTC).isoformat()),
        )
        conn.commit()
    return {"ok": True}


@app.post("/analyze")
def analyze(req: AskReq, user=Depends(get_current_user)):
    if not _check_rate_limit(user["id"], "analyze"):
        return {"error": "Rate limit reached — please wait a minute and try again."}
    try:
        owned_papers = _owned_library_rows(user["id"])
        if not owned_papers:
            return {"error": "Add papers to your library first."}
        result = analysis.analyze_library(req.question, owned_papers, user["id"])
        result["titles"] = {p["id"]: p["title"] for p in owned_papers}
        return result
    except Exception as e:
        logger.exception(f"/analyze failed for user={user['id']}")
        return {"error": f"Analysis failed: {e}"}


@app.get("/library/health")
def library_health(user=Depends(get_current_user)):
    try:
        owned_papers = _owned_library_rows(user["id"])
        return analysis.library_diversity(owned_papers)
    except Exception as e:
        logger.exception(f"/library/health failed for user={user['id']}")
        return {"error": f"Diversity check failed: {e}"}