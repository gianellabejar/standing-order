"""Text extraction for text PDFs (pypdf). Scanned or image-only PDFs are reported, never guessed at."""
import io

from pypdf import PdfReader

MIN_TEXT_CHARS = 20  # fewer non-space characters than this in the whole PDF = no usable text


def page_texts(pdf_bytes: bytes) -> list[str]:
    return [(p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf_bytes)).pages]


def has_text(pages: list[str]) -> bool:
    return sum(len("".join(t.split())) for t in pages) >= MIN_TEXT_CHARS


def with_page_markers(pages: list[str]) -> str:
    """The model reports provenance from these markers; code then checks the page number is real."""
    return "\n".join(f"=== page {i} ===\n{t.strip()}" for i, t in enumerate(pages, start=1))
