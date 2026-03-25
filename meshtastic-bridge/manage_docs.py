"""CLI document manager for the Meshtastic LLM bridge knowledge base.

Usage:
    python manage_docs.py ingest <file_or_directory>
    python manage_docs.py list
    python manage_docs.py delete <doc_id_or_filename>
    python manage_docs.py stats
    python manage_docs.py search <query>
"""

import argparse
import logging
import os
import sys
import time

from rag import RAGEngine
from rag.extractors import SUPPORTED_EXTENSIONS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("manage_docs")


def cmd_ingest(args, engine):
    """Ingest a file or all supported files in a directory."""
    path = os.path.abspath(args.path)

    if os.path.isdir(path):
        files = []
        for root, _, filenames in os.walk(path):
            for f in filenames:
                ext = os.path.splitext(f)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    files.append(os.path.join(root, f))

        if not files:
            print(f"No supported files found in: {path}")
            print(f"Supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
            return

        print(f"Found {len(files)} file(s) to ingest:")
        for f in files:
            print(f"  {os.path.basename(f)}")
        print()

        skip = getattr(args, "skip_existing", False)
        total_chunks = 0
        skipped = 0
        for file_path in files:
            try:
                start = time.time()
                result = engine.ingest_file(file_path, skip_existing=skip)
                elapsed = time.time() - start
                if result.get("skipped"):
                    skipped += 1
                    print(f"  SKIP {result['filename']} (already ingested)")
                else:
                    total_chunks += result["chunks"]
                    print(
                        f"  OK {result['filename']}: "
                        f"{result['chunks']} chunks ({elapsed:.1f}s)"
                    )
            except Exception as e:
                print(f"  FAIL {os.path.basename(file_path)}: {e}")

        print(f"\nDone. Ingested: {total_chunks} chunks. Skipped: {skipped} file(s).")

    elif os.path.isfile(path):
        try:
            start = time.time()
            result = engine.ingest_file(path)
            elapsed = time.time() - start
            print(
                f"Ingested: {result['filename']}\n"
                f"  Type: {result['file_type']}\n"
                f"  Chunks: {result['chunks']}\n"
                f"  Doc ID: {result['doc_id']}\n"
                f"  Time: {elapsed:.1f}s"
            )
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        print(f"Path not found: {path}")
        sys.exit(1)


def cmd_list(args, engine):
    """List all ingested documents."""
    docs = engine.list_documents()
    if not docs:
        print("No documents ingested yet.")
        print("Use: python manage_docs.py ingest <file>")
        return

    print(f"{'ID':<18} {'Type':<5} {'Chunks':<8} {'Filename'}")
    print("-" * 70)
    for doc in docs:
        print(
            f"{doc['doc_id']:<18} "
            f"{doc['file_type']:<5} "
            f"{doc['chunk_count']:<8} "
            f"{doc['filename']}"
        )
    print(f"\nTotal: {len(docs)} document(s)")


def cmd_delete(args, engine):
    """Delete a document by ID or filename."""
    if engine.delete_document(args.identifier):
        print(f"Deleted: {args.identifier}")
    else:
        print(f"Not found: {args.identifier}")
        sys.exit(1)


def cmd_stats(args, engine):
    """Show knowledge base statistics."""
    stats = engine.get_stats()
    size_mb = stats["db_size_bytes"] / (1024 * 1024)
    print(f"Documents: {stats['total_docs']}")
    print(f"Chunks:    {stats['total_chunks']}")
    print(f"DB size:   {size_mb:.2f} MB")
    print(f"Location:  {engine.db_path}")


def cmd_search(args, engine):
    """Test search against the knowledge base."""
    query = " ".join(args.query)
    if not query:
        print("Usage: python manage_docs.py search <query>")
        sys.exit(1)

    results = engine.search(query)
    if not results:
        print("No results found.")
        return

    print(f"Results for: \"{query}\"\n")
    for i, r in enumerate(results, 1):
        source = r["source"]
        title = r.get("article_title")
        label = f"{source}: {title}" if title else source
        print(f"[{i}] Score: {r['score']:.3f} | Source: {label}")
        # Show first 200 chars of the chunk
        preview = r["text"][:200].replace("\n", " ")
        print(f"    {preview}...")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Manage documents for the Meshtastic LLM bridge knowledge base"
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama API URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--rag-dir",
        default=None,
        help="RAG data directory (default: ~/.mesh-llm/rag)",
    )

    sub = parser.add_subparsers(dest="command")

    p_ingest = sub.add_parser("ingest", help="Ingest a file or directory")
    p_ingest.add_argument("path", help="File or directory to ingest")
    p_ingest.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that have already been ingested",
    )

    sub.add_parser("list", help="List ingested documents")

    p_delete = sub.add_parser("delete", help="Delete a document")
    p_delete.add_argument("identifier", help="Document ID or filename")

    sub.add_parser("stats", help="Show knowledge base statistics")

    p_search = sub.add_parser("search", help="Test search query")
    p_search.add_argument("query", nargs="+", help="Search query")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    engine = RAGEngine(
        ollama_base_url=args.ollama_url,
        data_dir=args.rag_dir,
    )

    commands = {
        "ingest": cmd_ingest,
        "list": cmd_list,
        "delete": cmd_delete,
        "stats": cmd_stats,
        "search": cmd_search,
    }

    commands[args.command](args, engine)


if __name__ == "__main__":
    main()
