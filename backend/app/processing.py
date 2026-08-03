SECTION_HEADS = [
    "abstract", "introduction", "related work", "background", "methodology",
    "method", "methods", "dataset", "data", "results", "experiments",
    "evaluation", "discussion", "limitations", "conclusion", "conclusions",
    "future work", "references",
]


def detect_sections(text: str):
    """Heuristic section splitter: treats short standalone lines matching a
    known academic heading as a new section boundary."""
    lines = text.split("\n")
    sections = []
    current = {"label": "body", "text": ""}
    for line in lines:
        stripped = line.strip().lower().strip(":")
        matched = next((h for h in SECTION_HEADS if stripped == h), None)
        if matched and len(line.strip()) < 40:
            if current["text"].strip():
                sections.append(current)
            current = {"label": matched, "text": ""}
        else:
            current["text"] += line + "\n"
    if current["text"].strip():
        sections.append(current)
    return sections


def chunk_text(text: str, chunk_size: int = 400, overlap: int = 50):
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        piece = " ".join(words[start:end])
        if piece.strip():
            chunks.append(piece)
        start += chunk_size - overlap
    return chunks


def chunk_sections(sections):
    chunks = []
    for sec in sections:
        for c in chunk_text(sec["text"]):
            chunks.append({"section": sec["label"], "text": c})
    return chunks
