import uuid
import xml.etree.ElementTree as ET

import pymupdf as fitz  # PyMuPDF
import requests

ARXIV_API = "http://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom"}


def search_arxiv(query, max_results=15, year_from=None, year_to=None):
    search_query = f"all:{query}"
    if year_from or year_to:
        start_date = f"{year_from or '1990'}01010000"
        end_date = f"{year_to or '2100'}12312359"
        search_query += f" AND submittedDate:[{start_date} TO {end_date}]"

    params = {"search_query": search_query, "start": 0, "max_results": max_results}
    try:
        resp = requests.get(ARXIV_API, params=params, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return {"error": f"arXiv search failed: {e}"}

    root = ET.fromstring(resp.text)
    results = []
    for entry in root.findall("atom:entry", NS):
        title = entry.find("atom:title", NS).text.strip().replace("\n", " ")
        summary = entry.find("atom:summary", NS).text.strip()
        authors = [a.find("atom:name", NS).text for a in entry.findall("atom:author", NS)]
        published = entry.find("atom:published", NS).text[:4]
        abs_link = entry.find("atom:id", NS).text
        pdf_link = abs_link.replace("/abs/", "/pdf/")
        if not pdf_link.endswith(".pdf"):
            pdf_link += ".pdf"
        results.append(
            {
                "id": str(uuid.uuid4()),
                "title": title,
                "abstract": summary,
                "authors": authors,
                "year": published,
                "source": "arXiv",
                "venue": "arXiv preprint",
                "doi": abs_link,
                "pdf_url": pdf_link,
            }
        )
    return results


def extract_pdf_text(file_bytes: bytes) -> str:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return text


def fetch_arxiv_full_text(pdf_url: str) -> str:
    """Downloads the real arXiv PDF and extracts its full text.
    Returns '' on any failure so callers can fall back to the abstract."""
    try:
        resp = requests.get(pdf_url, timeout=20)
        resp.raise_for_status()
        return extract_pdf_text(resp.content)
    except Exception:
        return ""