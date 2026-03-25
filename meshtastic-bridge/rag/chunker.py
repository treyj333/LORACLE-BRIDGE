"""Text chunking for RAG document ingestion.

Splits text into overlapping chunks suitable for embedding.
Pure Python — no external dependencies.
"""

import re


def chunk_text(text, target_size=5100, overlap=450):
    """Split text into overlapping chunks.

    Splits on paragraph boundaries first, then sentence boundaries
    for paragraphs that exceed target_size.

    Args:
        text: Input text to chunk.
        target_size: Target chunk size in characters (~1700 tokens).
        overlap: Overlap between consecutive chunks in characters (~150 tokens).

    Returns:
        List of text chunks.
    """
    if not text or not text.strip():
        return []

    text = text.strip()

    # If text fits in one chunk, return it directly
    if len(text) <= target_size:
        return [text]

    # Split into paragraphs
    paragraphs = re.split(r"\n\s*\n", text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    # Break oversized paragraphs into sentences
    segments = []
    for para in paragraphs:
        if len(para) <= target_size:
            segments.append(para)
        else:
            sentences = _split_sentences(para)
            segments.extend(sentences)

    # Accumulate segments into chunks with overlap
    chunks = []
    current = ""

    for segment in segments:
        # Hard-split any segment that still exceeds target_size
        if len(segment) > target_size:
            # Flush what we have so far
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(segment, target_size))
            continue

        candidate = (current + "\n\n" + segment).strip() if current else segment

        if len(candidate) <= target_size:
            current = candidate
        else:
            if current:
                chunks.append(current)
                # Start next chunk with overlap from end of previous
                if overlap > 0 and len(current) > overlap:
                    current = current[-overlap:].lstrip()
                    current = (current + "\n\n" + segment).strip()
                else:
                    current = segment
            else:
                # Single segment exceeds target — hard-split at word boundaries
                chunks.extend(_hard_split(segment, target_size))
                current = ""

    if current:
        chunks.append(current)

    return chunks


def _hard_split(text, max_size):
    """Split text into pieces of at most max_size chars at word boundaries.

    Falls back to character slicing if a single word exceeds max_size
    (e.g. binary junk or base64-encoded data from bad PDF extraction).
    """
    pieces = []
    remaining = text

    while remaining:
        if len(remaining) <= max_size:
            pieces.append(remaining)
            break

        # Find the last space within the budget
        split_at = remaining.rfind(" ", 0, max_size)
        if split_at <= 0:
            # No space found — force split at max_size (binary/junk data)
            split_at = max_size

        pieces.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return [p for p in pieces if p]


def _split_sentences(text):
    """Split text into sentences."""
    # Split on sentence-ending punctuation followed by space or newline
    parts = re.split(r"(?<=[.!?])\s+", text)
    # Recombine very short fragments
    sentences = []
    current = ""
    for part in parts:
        if not part.strip():
            continue
        if len(current) + len(part) < 200:
            current = (current + " " + part).strip() if current else part
        else:
            if current:
                sentences.append(current)
            current = part
    if current:
        sentences.append(current)
    return sentences if sentences else [text]
