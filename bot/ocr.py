"""Free local OCR for the fallback path (when Gemini is unavailable).

Gemini reads PDFs and photos visually, so the main path never needs this.
The fallback text model can't see images, so we turn documents into text:
Tesseract OCR when it's installed (`apt install tesseract-ocr`), otherwise
the PDF's embedded text layer. OCR catches what text layers miss: scanned
pages and filled-in/flattened form values (rates, dates, names).
"""

from __future__ import annotations

import io
import logging
import re
import shutil

import pypdfium2 as pdfium
from PIL import Image

log = logging.getLogger(__name__)

try:
    import pytesseract
except ImportError:  # optional dependency
    pytesseract = None

# Words that make a page worth keeping when we have to cut a long document down.
_KEYWORDS = re.compile(
    r"(?i)\b(exhibit|schedule|compensation|rate|cpm|per mile|amount|total|due|balance|invoice|"
    r"signature|signed|date|effective|routing|account|deduction|bonus|escrow|weeks|days)\b"
)
_NUMBER = re.compile(r"\$?\d+[.,]\d+|\$\d+")


def tesseract_available() -> bool:
    return pytesseract is not None and shutil.which("tesseract") is not None


def ocr_image(image: Image.Image) -> str:
    return pytesseract.image_to_string(image.convert("L")).strip()


def image_text(data: bytes) -> str:
    """OCR a photo/screenshot. Empty string when OCR isn't available."""
    if not tesseract_available():
        return ""
    try:
        with Image.open(io.BytesIO(data)) as img:
            return ocr_image(img)
    except Exception as exc:
        log.warning("Image OCR failed: %s", exc)
        return ""


def pdf_pages(data: bytes, max_pages: int = 20, dpi: int = 300) -> list[str]:
    """Text of each page: OCR if available, else the embedded text layer."""
    pdf = pdfium.PdfDocument(data)
    use_ocr = tesseract_available()
    pages = []
    try:
        for i in range(min(len(pdf), max_pages)):
            page = pdf[i]
            text = ""
            if use_ocr:
                try:
                    text = ocr_image(page.render(scale=dpi / 72).to_pil())
                except Exception as exc:
                    log.warning("OCR failed on page %d: %s", i + 1, exc)
            if not text:
                text = page.get_textpage().get_text_range().strip()
            pages.append(text)
        if len(pdf) > max_pages:
            pages.append(f"[{len(pdf) - max_pages} more pages not read]")
    finally:
        pdf.close()
    return pages


def page_score(text: str) -> int:
    return 3 * len(_NUMBER.findall(text)) + len(_KEYWORDS.findall(text))


def select_pages(pages: list[str], budget: int) -> str:
    """Fit a long document into `budget` chars: keep page 1 plus the pages with the
    most numbers/keywords (rates, totals, signatures), in their original order."""
    pages = [re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", p)).strip() for p in pages]
    labeled = [f"--- page {i + 1} ---\n{p}" for i, p in enumerate(pages)]
    if sum(len(p) for p in labeled) <= budget:
        return "\n".join(labeled)

    chosen = {0}
    used = len(labeled[0])
    for i in sorted(range(1, len(labeled)), key=lambda i: page_score(pages[i]), reverse=True):
        if used + len(labeled[i]) <= budget:
            chosen.add(i)
            used += len(labeled[i])
    skipped = len(labeled) - len(chosen)
    out = "\n".join(labeled[i] for i in sorted(chosen))[:budget]
    return out + f"\n[{skipped} lower-priority page(s) left out to fit the backup AI's limit]"


def document_text(data: bytes | None, budget: int, max_pages: int = 20) -> str:
    if not data:
        return ""
    try:
        pages = pdf_pages(data, max_pages=max_pages)
    except Exception as exc:  # malformed PDFs are common; don't crash the batch
        log.warning("PDF read failed: %s", exc)
        return "[PDF could not be read]"
    if not any(p.strip() for p in pages):
        return "[No readable text in this PDF]"
    return select_pages(pages, budget)
