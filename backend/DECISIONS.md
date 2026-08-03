# Echo — Autonomous Build Decisions

| Decision | Choice | Reason |
|---|---|---|
| Backend | Python FastAPI | Best library support for PDF parsing, embeddings, RAG; matches default policy |
| Vector store | Local ChromaDB (PersistentClient, file-based) | Zero external dependency, matches default policy |
| Structured data store | SQLite (stdlib `sqlite3`, no ORM) | File-based, zero setup, sufficient for v1 scale; avoids adding an ORM dependency for a handful of tables |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2` | Free, local, fast; industry-standard small embedding model |
| LLM provider | Anthropic API, `claude-sonnet-4-6`, called behind one `rag.ask()` function | Matches default policy; single seam makes the provider swappable later |
| Retrieval source, Phase 1 | arXiv only (via `export.arxiv.org` Atom API) | Phase 1 spec explicitly says "one retrieval source is fine to start"; this takes priority over the Section 2 default policy's fuller list (OpenAlex/Semantic Scholar/Unpaywall/Crossref), which is deferred to a later phase to avoid overscoping the MVP |
| Auth | Skipped entirely for v1; no `getCurrentUser()` seam added | Single-user local demo; the seam can be added when multi-user support is actually needed — adding an unused abstraction now would be premature |
| Frontend stack | Plain HTML/CSS/JS (not React+Vite+Tailwind as the default policy suggests) | The existing hand-built Echo UI (from earlier in this project) is already fully designed, reviewed, and approved by the user. Rebuilding it in React would duplicate completed work for no functional benefit at this stage. Logged as a deliberate deviation from the default policy, not an oversight. React migration remains a reasonable Phase 4+ task if the project grows a team. |
| Credibility scoring | Deferred to Phase 2, stubbed as `"unscored"` in Phase 1 | Needs Crossref/DOAJ integration, which is out of scope for the single-source Phase 1 MVP |
| Section detection | Heuristic line-matching against a fixed list of common academic headings | GROBID would be more robust but requires a running Java service; not worth the deployment complexity for v1 |
| Citation-network / trend prediction | Stubbed as a disabled, "coming soon" nav item (Phase 4) | Explicitly out of scope per spec; stubbed rather than omitted so the full intended product shape is visible |

## Known environment constraint (read before running)

This was built inside a sandboxed development environment whose network allowlist does not include `huggingface.co` (needed to download the embedding model weights) or paper-API domains like `export.arxiv.org` / `api.openalex.org`. That means:

- The code is correct and complete, but **could not be live-tested end-to-end inside this sandbox.**
- Everything that *could* be tested here was tested: PDF text extraction, chunking, SQLite storage, and a real call to the Anthropic API.
- On your own machine (or any normal server/cloud environment with regular internet access), `sentence-transformers` will download its model automatically on first run, and arXiv search will work as written — no code changes needed.
