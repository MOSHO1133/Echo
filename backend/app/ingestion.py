import uuid
import xml.etree.ElementTree as ET

import fitz  # PyMuPDF
import requests

ARXIV_API = "http://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom"}


def search_arxiv(query, max_results=6):
    """Search arXiv's public Atom API. Requires normal internet access at runtime."""
    params = {"search_query": f"all:{query}", "start": 0, "max_results": max_results}
    try:
        resp = requests.get(ARXIV_API, params=params, timeout=10)
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
        link = entry.find("atom:id", NS).text
        results.append(
            {
                "id": str(uuid.uuid4()),
                "title": title,
                "abstract": summary,
                "authors": authors,
                "year": published,
                "source": "arXiv",
                "venue": "arXiv preprint",
                "doi": link,
            }
        )
    return results


def extract_pdf_text(file_bytes: bytes) -> str:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return text
