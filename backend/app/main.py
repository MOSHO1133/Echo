import datetime
import uuid
from typing import List, Optional

from fastapi import BackgroundTasks, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import contribute, db, embeddings, ingestion, processing, rag, summarize

app = FastAPI(title="Echo API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db.init_db()


class SearchReq(BaseModel):
    query: str
    max_results: int = 15
    year_from: Optional[str] = None
    year_to: Optional[str] = None


class AskReq(BaseModel):
    question: str
    paper_ids: Optional[List[str]] = None


class CompareReq(BaseModel):
    paper_ids: List[str]


class ContribReq(BaseModel):
    idea: str
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


def _store_paper(paper_id, title, authors, year, venue, source, doi, pdf_url, full_text):
    conn = db.get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO papers
           (id, title, authors, year, venue, source, doi, pdf_url, tags, full_text, in_library, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (paper_id, title, authors, year, venue, source, doi, pdf_url, "", full_text, datetime.datetime.now(datetime.UTC).isoformat()),
    )
    conn.commit()
    conn.close()


def _index_and_summarize(paper_id, full_text):
    """Runs in the background after the paper row already exists, so a
    summarization failure never hides the paper from the library."""
    try:
        sections = processing.detect_sections(full_text)
        chunks = processing.chunk_sections(sections)
        embeddings.index_paper_chunks(paper_id, chunks)
        if chunks:
            summarize.summarize_paper(paper_id)
    except Exception as e:
        conn = db.get_conn()
        conn.execute("UPDATE papers SET research_gap=? WHERE id=?", (f"Processing failed: {e}", paper_id))
        conn.commit()
        conn.close()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search")
def search(req: SearchReq):
    results = ingestion.search_arxiv(req.query, max_results=req.max_results, year_from=req.year_from, year_to=req.year_to)
    return {"results": results}


@app.post("/library/add-from-search")
def add_from_search(req: AddFromSearchReq, background_tasks: BackgroundTasks):
    paper_id = req.id or str(uuid.uuid4())
    full_text = ingestion.fetch_arxiv_full_text(req.pdf_url) if req.pdf_url else ""
    if not full_text:
        full_text = req.abstract
    _store_paper(paper_id, req.title, ",".join(req.authors), req.year, req.venue, req.source, req.doi, req.pdf_url, full_text)
    background_tasks.add_task(_index_and_summarize, paper_id, full_text)
    return {"id": paper_id}


@app.post("/library/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    content = await file.read()
    text = ingestion.extract_pdf_text(content)
    paper_id = str(uuid.uuid4())
    _store_paper(paper_id, file.filename, "Uploaded by you", "", "", "upload", "", "", text)
    background_tasks.add_task(_index_and_summarize, paper_id, text)
    return {"id": paper_id}


@app.get("/library")
def get_library():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM papers WHERE in_library=1 ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"papers": [dict(r) for r in rows]}


@app.delete("/library/{paper_id}")
def remove(paper_id: str):
    conn = db.get_conn()
    conn.execute("UPDATE papers SET in_library=0 WHERE id=?", (paper_id,))
    conn.commit()
    conn.close()
    embeddings.delete_paper_chunks(paper_id)
    return {"ok": True}


@app.get("/paper/{paper_id}")
def get_paper(paper_id: str):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    conn.close()
    return dict(row) if row else {"error": "not found"}


@app.post("/ask")
def ask(req: AskReq):
    return rag.ask(req.question, paper_ids=req.paper_ids)


@app.post("/compare")
def compare(req: CompareReq):
    if len(req.paper_ids) < 2:
        return {"error": "Select at least 2 papers to compare."}
    conn = db.get_conn()
    placeholders = ",".join("?" for _ in req.paper_ids)
    rows = conn.execute(f"SELECT * FROM papers WHERE id IN ({placeholders})", req.paper_ids).fetchall()
    conn.close()
    return {"papers": [dict(r) for r in rows]}


@app.post("/contribute")
def contribute_ep(req: ContribReq):
    result = contribute.match_idea(req.idea, req.paper_ids)
    return result or {"error": "No matches found — add papers to your library first."}


@app.post("/feedback")
def feedback(req: FeedbackReq):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO feedback (target_type, target_id, vote, created_at) VALUES (?, ?, ?, ?)",
        (req.target_type, req.target_id, req.vote, datetime.datetime.now(datetime.UTC).isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}