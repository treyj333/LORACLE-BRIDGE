"""Text extraction from various document formats.

Supports PDF, ZIM, and plain text files.
ZIM support is optional — requires libzim to be installed separately.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".zim", ".txt", ".md", ".text", ".rst", ".html", ".htm"}


def extract_file(file_path):
    """Extract text from a file based on its extension.

    Args:
        file_path: Path to the file.

    Returns:
        For PDFs/text/HTML: a single string.
        For ZIM files: a list of dicts [{"title": str, "text": str}].

    Raises:
        ValueError: If file type is unsupported.
        FileNotFoundError: If file doesn't exist.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    if os.path.getsize(file_path) == 0:
        raise ValueError(f"Cannot open empty file: filename={file_path!r}")

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        return extract_pdf(file_path)
    elif ext == ".zim":
        return extract_zim(file_path)
    elif ext in (".txt", ".md", ".text", ".rst"):
        return extract_text(file_path)
    elif ext in (".html", ".htm"):
        return extract_html(file_path)
    else:
        raise ValueError(
            f"Unsupported file type: {ext}. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )


def extract_html(file_path):
    """Extract visible text from an HTML file (strips script/style/nav/etc)."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    text = _strip_html(html)
    cleaned = _clean_text(text)
    if not cleaned.strip():
        logger.warning(f"No visible text extracted from HTML: {file_path}")
    return cleaned


def extract_pdf(file_path):
    """Extract text from a PDF using PyMuPDF.

    Args:
        file_path: Path to the PDF file.

    Returns:
        Extracted text as a single string.
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        raise ImportError(
            "PDF support requires PyMuPDF. Install it with: pip install pymupdf"
        )

    doc = fitz.open(file_path)
    pages = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text()
        if text and text.strip():
            pages.append(text.strip())

    doc.close()

    if not pages:
        logger.warning(f"No text extracted from PDF: {file_path}")
        return ""

    full_text = "\n\n".join(pages)
    return _clean_text(full_text)


def extract_zim(file_path):
    """Extract articles from a ZIM archive.

    Args:
        file_path: Path to the ZIM file.

    Returns:
        List of dicts: [{"title": str, "text": str}]
    """
    # Try libzim first (faster, C++ bindings)
    try:
        return _extract_zim_libzim(file_path)
    except ImportError:
        pass

    # Try zimply as fallback
    try:
        return _extract_zim_zimply(file_path)
    except ImportError:
        pass

    raise ImportError(
        "ZIM support requires one of:\n"
        "  pip install libzim     (recommended, faster)\n"
        "  pip install zimply     (pure Python fallback)"
    )


def _extract_zim_libzim(file_path):
    """Extract ZIM articles using libzim."""
    from libzim.reader import Archive

    archive = Archive(file_path)
    articles = []

    for i in range(archive.entry_count):
        try:
            entry = archive._get_entry_by_id(i)
            if not entry.is_redirect:
                item = entry.get_item()
                mimetype = item.mimetype
                if mimetype and "html" in mimetype:
                    html = bytes(item.content).decode("utf-8", errors="replace")
                    text = _strip_html(html)
                    if text and len(text) > 50:
                        title = entry.title or f"Article {i}"
                        articles.append({"title": title, "text": _clean_text(text)})
        except Exception as e:
            logger.debug(f"Skipping ZIM entry {i}: {e}")
            continue

    logger.info(f"Extracted {len(articles)} articles from ZIM: {file_path}")
    return articles


def _extract_zim_zimply(file_path):
    """Extract ZIM articles using zimply."""
    from zimply import ZIMFile

    zim = ZIMFile(file_path, "utf-8")
    articles = []

    # Iterate through all articles
    for idx, article in enumerate(zim):
        try:
            if hasattr(article, "data") and article.data:
                data = article.data
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                if "<html" in data.lower() or "<body" in data.lower():
                    text = _strip_html(data)
                else:
                    text = data
                if text and len(text) > 50:
                    title = getattr(article, "title", f"Article {idx}")
                    articles.append({"title": title, "text": _clean_text(text)})
        except Exception as e:
            logger.debug(f"Skipping ZIM article {idx}: {e}")
            continue

    logger.info(f"Extracted {len(articles)} articles from ZIM: {file_path}")
    return articles


def extract_text(file_path):
    """Extract text from a plain text or markdown file.

    Args:
        file_path: Path to the text file.

    Returns:
        File contents as a string.
    """
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return _clean_text(f.read())


def _strip_html(html):
    """Strip HTML tags and extract text content.

    Uses BeautifulSoup if available, falls back to regex.
    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Remove non-content elements
        for tag in soup.find_all(
            [
                "script", "style", "nav", "header", "footer",
                "noscript", "iframe", "svg",
            ]
        ):
            tag.decompose()

        # Remove common non-content classes/ids
        for selector in [
            ".navbox", ".sidebar", ".infobox", ".reference",
            ".toc", ".mw-editsection", ".noprint",
        ]:
            for el in soup.select(selector):
                el.decompose()

        text = soup.get_text(separator="\n")
    except ImportError:
        # Fallback: regex-based HTML stripping
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", " ", text)

    return text


def _clean_text(text):
    """Clean extracted text — remove control chars, excess whitespace, dot leaders."""
    if not text:
        return ""
    # Remove null bytes and control characters (keep newlines and tabs)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Strip broken UTF-8 sequences (common in old scanned PDFs)
    text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    text = text.replace("\ufffd", "")
    # Collapse dot/dash leaders from TOC pages (e.g. "Chapter 1 ........... 3")
    # These tokenize as one token per character and blow up embedding context windows
    text = re.sub(r"\.{4,}", "...", text)
    text = re.sub(r"-{4,}", "---", text)
    text = re.sub(r"_{4,}", "___", text)
    text = re.sub(r"\*{4,}", "***", text)
    # Collapse multiple blank lines to two newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse multiple spaces (but not newlines)
    text = re.sub(r"[^\S\n]+", " ", text)
    return text.strip()
