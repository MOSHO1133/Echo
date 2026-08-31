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
BASE_DELAY_SECONDS = 15  # Groq free tier resets per-minute; steps stay well clear of it.


def call_llm(prompt, max_tokens=2048):
    """Single hardened entry point for every LLM call in the app (chat Q&A
    and paper summarization both go through this). Handles three failure
    modes:

    1. Per-minute rate limits (429) — retried with increasing backoff.
    2. Truncated output (finish_reason=length) — the model ran out of budget
       mid-answer, so we retry with MORE tokens.
    3. Per-request size limit exceeded (413 'Request too large') — the
       combined prompt + max_tokens exceeded the provider's per-request
       budget, so we retry with FEWER tokens (the prompt itself is already
       capped in build_context, so shrinking the response budget is what
       brings the total back under the limit).
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
            is_rate_limit = ("429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()) and "too large" not in msg.lower()
            is_truncated = "truncated or empty" in msg
            is_too_large = "413" in msg or "too large" in msg.lower() or "reduce your message size" in msg.lower()

            if attempt < MAX_RETRIES - 1:
                if is_too_large:
                    tokens = max(300, tokens // 2)
                    time.sleep(1)
                    continue
                if is_rate_limit:
                    time.sleep(BASE_DELAY_SECONDS * (attempt + 1))
                    continue
                if is_truncated:
                    tokens = int(tokens * 1.5)
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


def build_context(chunks, titles, max_chars_per_chunk=900):
    """max_chars_per_chunk keeps total prompt size predictable regardless of
    how many chunks are retrieved — without this, a handful of long chunks
    can push a request over the provider's per-request token budget (seen as
    a 413 'Request too large' error), independent of the per-minute limit."""
    parts = []
    for i, c in enumerate(chunks, start=1):
        title = titles.get(c["paper_id"], c["paper_id"])
        text = c["text"]
        if len(text) > max_chars_per_chunk:
            text = text[:max_chars_per_chunk] + "..."
        parts.append(f"[Source {i} — {title} ({c['section']})]\n{text}")
    return "\n\n".join(parts)


def ask(question, paper_ids=None, user_id=None, k=6, whole_library=False):
    """The one retrieve-and-generate function every AI feature in Echo calls.
    user_id scopes retrieval so a user can only ever retrieve their own chunks.

    Two distinct modes, both driven by explicit paper_ids from the caller
    (main.py) — this function never guesses which papers a user owns:

    - whole_library=False: paper_ids is the specific subset the user is
      asking about (usually one paper, "This paper" mode). A single top-k
      query across just those papers.

    - whole_library=True: paper_ids is the user's ENTIRE library. Retrieval
      is done PER PAPER (a separate query for each paper_id, each guaranteed
      a minimum number of results) rather than one global top-k query. This
      matters: a single strongly-matching paper can otherwise consume most
      or all of a small global k, starving the other papers in the library
      of any representation at all — even when they contain content the
      question genuinely needs. Per-paper retrieval guarantees every paper
      gets considered before the best chunks overall are kept for the
      answer context.

    Response includes ranked_papers only in whole_library mode: which
    papers were most relevant, sorted closest-first.
    """
    if not paper_ids:
        return {"answer": "No indexed content found for this scope yet.", "sources": [], "ranked_papers": None}

    if whole_library:
        per_paper_k = 4  # guaranteed minimum chunks considered per paper
        q_emb = embeddings.encode_query(question)
        chunks = []
        for pid in paper_ids:
            chunks.extend(
                embeddings.query_chunks_by_vector(q_emb, k=per_paper_k, paper_ids=[pid], user_id=user_id)
            )
        # Keep the globally best chunks across all papers for the actual
        # answer context, but only after every paper had a fair chance to
        # contribute — a paper that's genuinely irrelevant will naturally
        # fall out here, rather than never being queried at all.
        chunks.sort(key=lambda c: c["distance"])
        chunks = chunks[: max(k * 3, per_paper_k * len(paper_ids))]
    else:
        chunks = embeddings.query_chunks(question, k=k, paper_ids=paper_ids, user_id=user_id)

    if not chunks:
        return {"answer": "No indexed content found for this scope yet.", "sources": [], "ranked_papers": None}

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
        answer = f"LLM API error: {e}"

    ranked_papers = None
    if whole_library:
        scores = {}
        for c in chunks:
            scores.setdefault(c["paper_id"], []).append(c["distance"])
        ranked = sorted(scores.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        ranked_papers = [
            {"paper_id": pid, "title": titles.get(pid, pid), "avg_distance": sum(dists) / len(dists)}
            for pid, dists in ranked
        ]

    return {"answer": answer, "sources": chunks, "ranked_papers": ranked_papers}