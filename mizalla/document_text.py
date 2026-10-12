"""Plain-text extraction for the document types the Chat service accepts.

Each extractor is deliberately best-effort: a document that yields little text
is reported as such rather than silently producing empty context, because an
empty context reads to the model as "this document says nothing" and invites a
confident wrong answer.

Legacy binary formats (``.doc``, ``.xls``) have no pure-Python reader. They are
handled by running LibreOffice when it is on PATH, and otherwise by scraping
printable runs out of the bytes - good enough for simple files, and reported as
approximate so the user knows to prefer PDF or DOCX.
"""

import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md", ".csv", ".rtf"}

#: Shown in the UI next to the upload control.
ACCEPT_ATTRIBUTE = ".pdf,.docx,.doc,.xlsx,.xls,.pptx,.ppt,.txt,.md,.csv,.rtf"

#: Runs of at least this many printable characters count as text in a binary file.
_MIN_RUN = 4
_PRINTABLE_RUN = re.compile(r"[^\x00-\x08\x0b\x0c\x0e-\x1f]{%d,}" % _MIN_RUN)

RTF_BODY = re.compile(r"\{\\rtf1.*?\\par(?![a-z])}", re.DOTALL)


class ExtractionError(Exception):
    """The document could not be read, or contained no usable text."""


def _blank_lines(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())


def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 - reported to the user
        raise ExtractionError(f"This PDF could not be opened ({exc}).") from exc

    pages = []
    for number, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page should not lose the rest
            text = ""
        text = text.strip()
        if text:
            pages.append(f"[Page {number}]\n{text}")
    if not pages:
        raise ExtractionError(
            "No text could be read from this PDF. It is probably a scan without a text layer."
        )
    return "\n\n".join(pages)


def _docx_text(content: bytes) -> str:
    from docx import Document

    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 - reported to the user
        raise ExtractionError(f"This DOCX could not be opened ({exc}).") from exc

    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return _blank_lines("\n".join(parts))


def _xlsx_text(content: bytes) -> str:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - reported to the user
        raise ExtractionError(f"This workbook could not be opened ({exc}).") from exc

    sheets = []
    try:
        for worksheet in workbook.worksheets:
            rows = []
            for row in worksheet.iter_rows(values_only=True):
                cells = ["" if cell is None else str(cell) for cell in row]
                # A trailing run of empty cells adds nothing but noise.
                while cells and not cells[-1].strip():
                    cells.pop()
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                sheets.append(f"[Sheet: {worksheet.title}]\n" + "\n".join(rows))
    finally:
        workbook.close()
    return _blank_lines("\n\n".join(sheets))


def _pptx_text(content: bytes) -> str:
    from pptx import Presentation

    try:
        presentation = Presentation(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 - reported to the user
        raise ExtractionError(f"This PPTX could not be opened ({exc}).") from exc

    slides = []
    for number, slide in enumerate(presentation.slides, 1):
        parts = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        parts.append(" | ".join(cells))
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes:
                parts.append(f"Notes: {notes}")
        if parts:
            slides.append(f"[Slide {number}]\n" + "\n".join(parts))
    return _blank_lines("\n\n".join(slides))


def _rtf_text(content: bytes) -> str:
    raw = content.decode("latin-1", errors="replace")
    body = RTF_BODY.search(raw)
    text = body.group(0) if body else raw
    # Strip control words, keep the brace-separated runs they wrap.
    text = re.sub(r"\\[a-z]+-?\d*\s?", " ", text)
    text = text.replace("{", " ").replace("}", "\n")
    text = re.sub(r"\\[a-z]+\d*", " ", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def _soffice_text(filename: str, suffix: str) -> str | None:
    """Convert a legacy binary file to text using LibreOffice, if it is installed."""
    if not shutil.which("soffice"):
        return None
    with tempfile.TemporaryDirectory() as workdir:
        source = Path(workdir) / f"source{suffix}"
        source.write_bytes(filename[1])
        try:
            subprocess.run(
                [
                    "soffice", "--headless", "--convert-to", "txt:Text",
                    "--outdir", workdir, str(source),
                ],
                check=True,
                capture_output=True,
                timeout=180,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        converted = Path(workdir) / "source.txt"
        if converted.exists():
            return converted.read_text(encoding="utf-8", errors="replace")
    return None


def _legacy_text(filename: str, suffix: str) -> tuple[str, bool]:
    """Extract text from a legacy binary document.

    Returns ``(text, approximate)`` so the caller can tell the user when the
    result is a scrape rather than a real parse.
    """
    converted = _soffice_text(filename, suffix)
    if converted:
        return converted, False

    content: bytes = filename[1]
    # Word and Excel write UTF-16LE, which is recognisable by the null byte in
    # the high half of most code units.
    if len(content) > 1 and content[1::2].count(0) > len(content) // 4:
        decoded = content.decode("utf-16-le", errors="replace")
    else:
        decoded = content.decode("latin-1", errors="replace")

    runs = [run.strip() for run in _PRINTABLE_RUN.findall(decoded)]
    text = "\n".join(run for run in runs if len(run) > _MIN_RUN)
    if not text.strip():
        raise ExtractionError(
            f"No text could be read from this {suffix} file. "
            "Save it as PDF or DOCX and upload that instead."
        )
    return text, True


def extract_text(filename: str, content: bytes) -> dict:
    """Extract text from ``content``.

    Returns ``{"text": str, "approximate": bool, "kind": str}``. Raises
    :class:`ExtractionError` for an unsupported type, an unreadable file, or a
    file that yields no text.
    """
    if not content:
        raise ExtractionError("The uploaded file was empty.")

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ExtractionError(
            f"'{suffix or filename}' is not a supported document type. "
            f"Upload one of: {', '.join(sorted(SUPPORTED_SUFFIXES))}."
        )

    approximate = False
    if suffix == ".pdf":
        kind, text = "PDF", _pdf_text(content)
    elif suffix == ".docx":
        kind, text = "Word", _docx_text(content)
    elif suffix == ".xlsx":
        kind, text = "Excel", _xlsx_text(content)
    elif suffix == ".pptx":
        kind, text = "PowerPoint", _pptx_text(content)
    elif suffix == ".rtf":
        kind, text = "RTF", _rtf_text(content)
    elif suffix in {".txt", ".md", ".csv"}:
        kind = "Text"
        text = content.decode("utf-8", errors="replace")
    else:
        kind = {"doc": "Word", "xls": "Excel", "ppt": "PowerPoint"}.get(suffix.lstrip("."), suffix)
        text, approximate = _legacy_text((filename, content), suffix)

    text = text.strip()
    if not text:
        raise ExtractionError(f"No text could be read from this {kind} file.")
    return {"text": text, "approximate": approximate, "kind": kind}
