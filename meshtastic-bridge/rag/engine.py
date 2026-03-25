"""RAG engine — SQLite + NumPy vector store with Ollama embeddings.

Provides document ingestion, storage, and semantic search for the
standalone Meshtastic LLM bridge. No external servers required.
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

import numpy as np
import requests

from .chunker import chunk_text
from .extractors import extract_file

logger = logging.getLogger(__name__)

# Constants aligned with N.O.M.A.D.'s RAG implementation
EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIM = 768
TARGET_CHARS_PER_CHUNK = 5100  # ~1700 tokens
OVERLAP_CHARS = 450  # ~150 tokens
SEARCH_DOCUMENT_PREFIX = "search_document: "
SEARCH_QUERY_PREFIX = "search_query: "
SCORE_THRESHOLD = 0.3
TOP_K = 5
EMBEDDING_BATCH_SIZE = 8
MAX_CONTEXT_CHARS = 2000  # Limit total injected context for LoRa brevity

RAG_SYSTEM_ADDENDUM = (
    "You have access to a knowledge base. Context documents are provided below. "
    "Use them to ground your answer when relevant. If the context doesn't help, "
    "say so briefly and answer from general knowledge. Keep your response concise."
)


class RAGEngine:
    """Lightweight RAG engine backed by SQLite and NumPy."""

    def __init__(
        self,
        ollama_base_url="http://localhost:11434",
        data_dir=None,
        embedding_model=EMBEDDING_MODEL,
        top_k=TOP_K,
        score_threshold=SCORE_THRESHOLD,
    ):
        self.ollama_url = ollama_base_url.rstrip("/")
        self.embedding_model = embedding_model
        self.top_k = top_k
        self.score_threshold = score_threshold
        self._session = requests.Session()
        self._db_lock = threading.Lock()

        # Data directory
        if data_dir is None:
            data_dir = os.path.join(os.path.expanduser("~"), ".mesh-llm", "rag")
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

        self.db_path = os.path.join(self.data_dir, "knowledge.db")
        self._init_db()

        # Embedding cache (loaded on first search)
        self._embeddings_cache = None
        self._cache_dirty = True

    def _init_db(self):
        """Create database tables if they don't exist."""
        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    file_path TEXT,
                    file_type TEXT,
                    chunk_count INTEGER DEFAULT 0,
                    ingested_at REAL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
                    chunk_index INTEGER,
                    text TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    char_count INTEGER,
                    metadata TEXT,
                    UNIQUE(doc_id, chunk_index)
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);
                """
            )
            conn.commit()
            conn.close()

    # ─── Document Management ─────────────────────────────────────────────

    def is_ingested(self, filename):
        """Check if a file has already been ingested by filename."""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT doc_id FROM documents WHERE filename = ?", (filename,)
        ).fetchone()
        conn.close()
        return row is not None

    def ingest_file(self, file_path, skip_existing=False):
        """Ingest a file into the knowledge base.

        Args:
            file_path: Path to PDF, ZIM, or text file.
            skip_existing: If True, skip files already ingested (by filename).

        Returns:
            Dict with ingestion results: {doc_id, filename, chunks, file_type, skipped}
        """
        file_path = os.path.abspath(file_path)
        filename = os.path.basename(file_path)
        ext = os.path.splitext(filename)[1].lower().lstrip(".")

        if skip_existing and self.is_ingested(filename):
            logger.info(f"Skipping (already ingested): {filename}")
            return {"doc_id": None, "filename": filename, "chunks": 0, "file_type": ext, "skipped": True}

        logger.info(f"Ingesting: {filename}")

        # Extract text
        content = extract_file(file_path)

        # Handle ZIM (returns list of articles) vs single-text files
        if isinstance(content, list):
            # ZIM: chunk each article separately
            all_chunks = []
            for article in content:
                title = article.get("title", "")
                text = article.get("text", "")
                if not text:
                    continue
                # Prepend title for context
                if title:
                    text = f"{title}\n\n{text}"
                article_chunks = chunk_text(text, TARGET_CHARS_PER_CHUNK, OVERLAP_CHARS)
                for chunk in article_chunks:
                    all_chunks.append({"text": chunk, "article_title": title})
        else:
            # Single document
            if not content or not content.strip():
                logger.warning(f"No text extracted from: {filename}")
                return {"doc_id": None, "filename": filename, "chunks": 0, "file_type": ext}
            text_chunks = chunk_text(content, TARGET_CHARS_PER_CHUNK, OVERLAP_CHARS)
            all_chunks = [{"text": c, "article_title": None} for c in text_chunks]

        if not all_chunks:
            logger.warning(f"No chunks generated from: {filename}")
            return {"doc_id": None, "filename": filename, "chunks": 0, "file_type": ext}

        logger.info(f"Generated {len(all_chunks)} chunks from {filename}")

        # Ensure embedding model is available
        self._ensure_embedding_model()

        # Embed chunks in batches
        texts = [SEARCH_DOCUMENT_PREFIX + c["text"] for c in all_chunks]
        embeddings = self._embed_batched(texts)

        # Store in database
        doc_id = hashlib.sha256(
            f"{filename}:{time.time()}".encode()
        ).hexdigest()[:16]

        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "INSERT INTO documents (doc_id, filename, file_path, file_type, chunk_count, ingested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, filename, file_path, ext, len(all_chunks), time.time()),
                )

                for i, (chunk, embedding) in enumerate(zip(all_chunks, embeddings)):
                    chunk_id = f"{doc_id}:{i}"
                    metadata = json.dumps({"article_title": chunk.get("article_title")})
                    conn.execute(
                        "INSERT INTO chunks (chunk_id, doc_id, chunk_index, text, embedding, char_count, metadata) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            chunk_id,
                            doc_id,
                            i,
                            chunk["text"],
                            np.array(embedding, dtype=np.float32).tobytes(),
                            len(chunk["text"]),
                            metadata,
                        ),
                    )

                conn.commit()
            finally:
                conn.close()

        self._cache_dirty = True
        logger.info(f"Ingested {filename}: {len(all_chunks)} chunks (doc_id: {doc_id})")

        return {
            "doc_id": doc_id,
            "filename": filename,
            "chunks": len(all_chunks),
            "file_type": ext,
        }

    def list_documents(self):
        """List all ingested documents.

        Returns:
            List of dicts: [{doc_id, filename, file_type, chunk_count, ingested_at}]
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT doc_id, filename, file_type, chunk_count, ingested_at FROM documents ORDER BY ingested_at DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def delete_document(self, doc_id_or_name):
        """Delete a document by ID or filename.

        Returns:
            True if deleted, False if not found.
        """
        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA foreign_keys=ON")

            # Try by doc_id first
            row = conn.execute(
                "SELECT doc_id FROM documents WHERE doc_id = ? OR filename = ?",
                (doc_id_or_name, doc_id_or_name),
            ).fetchone()

            if not row:
                conn.close()
                return False

            doc_id = row[0]
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            conn.commit()
            conn.close()

        self._cache_dirty = True
        logger.info(f"Deleted document: {doc_id_or_name}")
        return True

    def get_stats(self):
        """Get knowledge base statistics.

        Returns:
            Dict: {total_docs, total_chunks, db_size_bytes}
        """
        conn = sqlite3.connect(self.db_path)
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        conn.close()

        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "total_docs": docs,
            "total_chunks": chunks,
            "db_size_bytes": db_size,
        }

    # ─── Search ──────────────────────────────────────────────────────────

    def search(self, query, top_k=None, threshold=None):
        """Search for chunks relevant to a query.

        Args:
            query: Search query string.
            top_k: Number of results (default: self.top_k).
            threshold: Minimum similarity score (default: self.score_threshold).

        Returns:
            List of dicts: [{text, score, source, chunk_index, article_title}]
        """
        if top_k is None:
            top_k = self.top_k
        if threshold is None:
            threshold = self.score_threshold

        # Load embeddings into cache if needed
        self._load_cache()

        if not self._embeddings_cache or len(self._embeddings_cache["embeddings"]) == 0:
            return []

        # Embed query
        query_embedding = self._embed([SEARCH_QUERY_PREFIX + query])[0]
        query_vec = np.array(query_embedding, dtype=np.float32)

        # Cosine similarity against all chunks
        chunk_embeddings = self._embeddings_cache["embeddings"]
        similarities = _cosine_similarity(query_vec, chunk_embeddings)

        # Get top results above threshold
        indices = np.argsort(similarities)[::-1][:top_k * 2]  # Get extra for filtering

        results = []
        seen_sources = {}
        for idx in indices:
            score = float(similarities[idx])
            if score < threshold:
                break

            meta = self._embeddings_cache["metadata"][idx]
            source = meta["filename"]

            # Source diversity: penalize repeated sources
            source_count = seen_sources.get(source, 0)
            adjusted_score = score * (0.85 ** source_count)

            if adjusted_score < threshold:
                continue

            seen_sources[source] = source_count + 1
            results.append({
                "text": meta["text"],
                "score": round(score, 4),
                "adjusted_score": round(adjusted_score, 4),
                "source": source,
                "chunk_index": meta["chunk_index"],
                "article_title": meta.get("article_title"),
            })

            if len(results) >= top_k:
                break

        return results

    def build_context_messages(self, query):
        """Search and format results as context messages for Ollama.

        Args:
            query: The user's question.

        Returns:
            List of message dicts to inject into the chat, or empty list.
        """
        results = self.search(query)
        if not results:
            return []

        messages = []
        total_chars = 0

        for i, result in enumerate(results, 1):
            text = result["text"]
            # Truncate individual result if needed
            remaining = MAX_CONTEXT_CHARS - total_chars
            if remaining <= 100:
                break
            if len(text) > remaining:
                text = text[:remaining - 20] + " ...[truncated]"

            pct = int(result["score"] * 100)
            source = result["source"]
            title = result.get("article_title")
            source_label = f"{source}: {title}" if title else source

            content = f"[Context {i}] (Relevance: {pct}%, Source: {source_label}) {text}"
            messages.append({"role": "system", "content": content})
            total_chars += len(text)

        return messages

    # ─── Embedding ───────────────────────────────────────────────────────

    def _embed(self, texts):
        """Embed a list of texts via Ollama /api/embed.

        Args:
            texts: List of strings to embed (with prefix already applied).

        Returns:
            List of embedding vectors (list of floats).
        """
        resp = self._session.post(
            f"{self.ollama_url}/api/embed",
            json={"model": self.embedding_model, "input": texts},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]

    def _embed_batched(self, texts):
        """Embed texts in batches to avoid overwhelming Ollama.

        Args:
            texts: List of strings to embed.

        Returns:
            List of embedding vectors.
        """
        all_embeddings = []
        total = len(texts)

        for i in range(0, total, EMBEDDING_BATCH_SIZE):
            batch = texts[i : i + EMBEDDING_BATCH_SIZE]
            logger.info(
                f"Embedding batch {i // EMBEDDING_BATCH_SIZE + 1}/"
                f"{(total + EMBEDDING_BATCH_SIZE - 1) // EMBEDDING_BATCH_SIZE} "
                f"({len(batch)} chunks)"
            )
            embeddings = self._embed(batch)
            all_embeddings.extend(embeddings)

        return all_embeddings

    def _ensure_embedding_model(self):
        """Pull the embedding model if it's not installed."""
        try:
            resp = self._session.get(f"{self.ollama_url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            if any(self.embedding_model in m for m in models):
                return True
        except Exception:
            pass

        logger.info(f"Pulling embedding model: {self.embedding_model}")
        try:
            resp = self._session.post(
                f"{self.ollama_url}/api/pull",
                json={"name": self.embedding_model, "stream": False},
                timeout=600,
            )
            resp.raise_for_status()
            logger.info(f"Model {self.embedding_model} pulled successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to pull {self.embedding_model}: {e}")
            raise RuntimeError(
                f"Embedding model '{self.embedding_model}' is not installed and could not be pulled. "
                f"Install it manually: ollama pull {self.embedding_model}"
            )

    # ─── Cache ───────────────────────────────────────────────────────────

    def _load_cache(self):
        """Load all chunk embeddings into memory for fast search."""
        if not self._cache_dirty and self._embeddings_cache is not None:
            return

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT c.embedding, c.text, c.chunk_index, c.metadata, d.filename "
            "FROM chunks c JOIN documents d ON c.doc_id = d.doc_id"
        ).fetchall()
        conn.close()

        if not rows:
            self._embeddings_cache = {"embeddings": np.array([]), "metadata": []}
            self._cache_dirty = False
            return

        embeddings = []
        metadata = []
        for row in rows:
            emb = np.frombuffer(row[0], dtype=np.float32)
            embeddings.append(emb)
            meta_json = json.loads(row[3]) if row[3] else {}
            metadata.append({
                "text": row[1],
                "chunk_index": row[2],
                "filename": row[4],
                "article_title": meta_json.get("article_title"),
            })

        self._embeddings_cache = {
            "embeddings": np.vstack(embeddings),
            "metadata": metadata,
        }
        self._cache_dirty = False
        logger.debug(f"Loaded {len(metadata)} chunk embeddings into cache")


def _cosine_similarity(query_vec, matrix):
    """Compute cosine similarity between a query vector and a matrix of vectors.

    Args:
        query_vec: 1D numpy array.
        matrix: 2D numpy array (rows are vectors).

    Returns:
        1D numpy array of similarity scores.
    """
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return np.zeros(matrix.shape[0])

    matrix_norms = np.linalg.norm(matrix, axis=1)
    # Avoid division by zero
    matrix_norms = np.where(matrix_norms == 0, 1, matrix_norms)

    return np.dot(matrix, query_vec) / (matrix_norms * query_norm)
