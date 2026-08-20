import os
import time
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

from openai import OpenAI

from . import db, embeddings

client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

MAX_RETRIES = 4
BASE_DELAY_SECONDS = 15  # Gemini free tier resets per-minute; steps stay well clear of it.


def call_llm(prompt, max_tokens=2048):
    """Single hardened entry point for every Gemini call in the app (chat Q&A
    and paper summarization both go through this). Handles two failure modes
    that are easy to hit on Gemini's free tier + reasoning models:

    1. 429 rate limits — free tier caps requests per minute; retried with
       increasing backoff instead of failing immediately.
    2. Truncated output — gemini-3.5-flash spends part of max_tokens on internal
       "thinking" before writing the visible answer, and that can't be disabled
       for Gemini 3-series models. A low budget can cut the real answer off
       before it's finished. We detect this via finish_reason and retry with
       more room rather than silently returning a half response.
    """
    last_error = None
    tokens = max_tokens
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                max_tokens=tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            choice = resp.choices[0]
            content = choice.message.content
            if choice.finish_reason == "length" or not content:
                raise ValueError(f"Response truncated or empty (finish_reason={choice.finish_reason})")
            return content
        except Exception as e:
            last_error = e
            msg = str(e)
            is_rate_limit = "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()
            is_truncated = "truncated or empty" in msg
            if attempt < MAX_RETRIES - 1:
                if is_rate_limit:
                    time.sleep(BASE_DELAY_SECONDS * (attempt + 1))
                    continue
                if is_truncated:
                    tokens = int(tokens * 1.5)  # give it more room and try again
                    time.sleep(2)
                    continue
            raise last_error


def get_paper_titles(paper_ids):
    if not paper_ids:
        return {}
    conn = db.get_conn()
    placeholders = ",".join("?" for _ in paper_ids)
    rows = conn.execute(f"SELECT id, title FROM papers WHERE id IN ({placeholders})", paper_ids).fetchall()
    conn.close()
    return {r["id"]: r["title"] for r in rows}


def build_context(chunks, titles):
    parts = []
    for i, c in enumerate(chunks, start=1):
        title = titles.get(c["paper_id"], c["paper_id"])
        parts.append(f"[Source {i} — {title} ({c['section']})]\n{c['text']}")
    return "\n\n".join(parts)


def ask(question, paper_ids=None, user_id=None, k=6):
    """The one retrieve-and-generate function every AI feature in Echo calls.
    user_id scopes retrieval so a user can only ever retrieve their own chunks."""
    chunks = embeddings.query_chunks(question, k=k, paper_ids=paper_ids, user_id=user_id)
    if not chunks:
        return {"answer": "No indexed content found for this scope yet.", "sources": []}

    titles = get_paper_titles(list({c["paper_id"] for c in chunks}))
    context = build_context(chunks, titles)
    prompt = f"""Answer the question using ONLY the sources below. Cite sources like [Source 1].
If the sources don't contain the answer, say so plainly instead of guessing.

{context}

Question: {question}
Answer:"""

    try:
        answer = call_llm(prompt, max_tokens=1024)
    except Exception as e:
        answer = f"Gemini API error: {e}"

    return {"answer": answer, "sources": chunks}