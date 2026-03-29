"""SITREP exporter — text and PDF output."""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


def export_text(sitrep_content: str, sitrep_id: str, output_dir: Optional[str] = None) -> str:
    """Export a SITREP as a plain text file.

    Returns:
        Path to the exported file.
    """
    if output_dir is None:
        output_dir = os.path.join(os.path.expanduser("~"), ".mesh-llm", "briefs")
    os.makedirs(output_dir, exist_ok=True)

    filename = f"{sitrep_id}.txt"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w") as f:
        f.write(f"LORACLE BRIEF — {sitrep_id}\n")
        f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 50 + "\n\n")
        f.write(sitrep_content)
        f.write("\n")

    logger.info(f"SITREP exported to {filepath}")
    return filepath


def export_pdf(sitrep_content: str, sitrep_id: str, output_dir: Optional[str] = None) -> str:
    """Export a SITREP as a PDF file using pymupdf.

    Returns:
        Path to the exported file.
    """
    if output_dir is None:
        output_dir = os.path.join(os.path.expanduser("~"), ".mesh-llm", "briefs")
    os.makedirs(output_dir, exist_ok=True)

    filename = f"{sitrep_id}.pdf"
    filepath = os.path.join(output_dir, filename)

    try:
        import fitz  # pymupdf

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)  # A4

        # Header
        header_rect = fitz.Rect(40, 30, 555, 60)
        page.insert_textbox(
            header_rect,
            f"LORACLE BRIEF — {sitrep_id}",
            fontsize=14,
            fontname="helv",
            color=(0.8, 0.1, 0.1),
        )

        # Timestamp
        ts_rect = fitz.Rect(40, 55, 555, 75)
        page.insert_textbox(
            ts_rect,
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            fontsize=9,
            fontname="helv",
            color=(0.4, 0.4, 0.4),
        )

        # Separator line
        page.draw_line(fitz.Point(40, 80), fitz.Point(555, 80), color=(0.7, 0.7, 0.7))

        # Content
        content_rect = fitz.Rect(40, 90, 555, 800)
        page.insert_textbox(
            content_rect,
            sitrep_content,
            fontsize=10,
            fontname="cour",  # Monospace for SITREP format
        )

        doc.save(filepath)
        doc.close()
        logger.info(f"SITREP PDF exported to {filepath}")

    except ImportError:
        logger.warning("pymupdf not available for PDF export, falling back to text")
        return export_text(sitrep_content, sitrep_id, output_dir)

    return filepath
