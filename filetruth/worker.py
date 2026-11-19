"""The sandboxed worker: one document in, one JSON report on stdout.

This is the only process that opens the document with parser and rendering
libraries. It runs with no network and a read-only view of the input. See
sandbox.py for how it is started.

The steps for a document:

1. Get the parser's text (pypdf, PyMuPDF, python-docx, or a supplied capture).
2. Render each page to pixels with PDFium. DOCX goes through LibreOffice first.
3. Read the pixels with Tesseract.
4. Ask PyMuPDF where painted text sits, and which text is stored but not painted.
5. Hand all of it to alignment.compare and print the report.
"""

import base64
import hashlib
import io
import json
import math
import subprocess
import sys
import time
from collections import defaultdict
from importlib.metadata import version
from pathlib import Path
from zipfile import BadZipFile

import pymupdf
import pypdfium2 as pdfium
from lxml.etree import XMLSyntaxError
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from pypdfium2.version import PDFIUM_INFO

from . import capture, ocr, office
from .alignment import compare, key, outcome, overlaps, reconcile
from .sandbox import Rejected, Unavailable

DOCUMENT = Path("/input/document")
CAPTURE = Path("/input/capture.json")

# Budgets. Anything larger is rejected rather than processed slowly.
MAX_PAGES = 10
MIN_PAGE_POINTS, MAX_PAGE_POINTS = 10, 1600
MAX_PARSER_CHARACTERS = 150_000
MAX_SEGMENTS = 4000
MAX_HIDDEN_SPANS_PER_PAGE = 800
MAX_FINDINGS = 3000

# Conversion and OCR share one time budget that ends before the in-container
# (60 s) and parent (65 s) deadlines. A slow document then returns a report with
# unread pages marked unverifiable, instead of being killed with no evidence.
WORK_SECONDS = 48
OCR_SECONDS_PER_PAGE = 12

# Rendering: up to 2.5x, capped so the longest side stays near 2100 pixels.
RENDER_SCALE = 2.5
RENDER_LONGEST_SIDE = 2100
# Tesseract can drop a line at one scale and read it at another.
OCR_RETRY_SCALE = 0.8
PREVIEW_SIZE = (900, 1100)

# PyMuPDF `char_flags` bits that mark a span as drawn (filled or stroked).
FILLED_OR_STROKED = 8 | 16
# PDF text render mode 3 draws nothing. Parsers still read the text.
INVISIBLE_RENDER_MODE = 3


def require_isolated_network():
    """Refuse to run if any network interface other than loopback exists."""
    interfaces = Path("/proc/net/dev").read_text().splitlines()[2:]
    names = [line.split(":", 1)[0].strip() for line in interfaces if line.strip()]
    if any(name != "lo" for name in names):
        raise Unavailable("Worker requires an isolated network namespace.")


def docx_paragraph_segments(path):
    """Body paragraphs as python-docx returns them. They carry no page number."""
    return [
        {
            "text": text,
            "page": None,
            "source": f"word/document.xml body paragraph {number}",
        }
        for number, text in enumerate(office.paragraphs(path), 1)
    ]


def rounded_box(rect):
    return [round(value, 2) for value in rect]


def painted_lines(mupdf_page):
    """Lines of text that are actually drawn, with their place on the page.

    PyMuPDF reports coordinates for the unrotated page. Multiplying by the
    rotation matrix moves them into the displayed page that PDFium renders.
    """
    lines = []
    blocks = mupdf_page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
    for block in blocks:
        for line in block.get("lines", []):
            visible = [
                span
                for span in line["spans"]
                if span.get("char_flags", 0) & FILLED_OR_STROKED
                and span.get("alpha", 255) > 0
            ]
            if visible:
                box = pymupdf.Rect(line["bbox"]) * mupdf_page.rotation_matrix
                text = "".join(span["text"] for span in visible)
                lines.append({"text": text, "bbox": rounded_box(box)})
    return lines


def hidden_spans(mupdf_page):
    """Text the PDF stores but does not paint: invisible mode or zero opacity."""
    spans = []
    for span in mupdf_page.get_texttrace():
        invisible = span["type"] == INVISIBLE_RENDER_MODE
        if not invisible and span["opacity"] != 0:
            continue
        box = pymupdf.Rect(span["bbox"]) * mupdf_page.rotation_matrix
        if not all(math.isfinite(value) for value in box):
            continue
        spans.append(
            {
                "text": "".join(chr(char[0]) for char in span["chars"]),
                "bbox": rounded_box(box),
                "reason": "PDF non-painting text" if invisible else "PDF zero opacity",
            }
        )
    return spans


def unread_lines(painted, ocr_lines):
    """Painted lines of two or more tokens that no OCR line covers.

    Single tokens such as page numbers are skipped. OCR drops them too often
    for the gap to mean anything.
    """
    return [
        line
        for line in painted
        if len(key(line["text"])) >= 2
        and not any(overlaps(line["bbox"], seen["bbox"]) for seen in ocr_lines)
    ]


def retry_ocr_for_gaps(image, size, language, installed, seconds, ocr_lines, notes):
    """Run OCR once more at a smaller scale and keep only lines in empty regions.

    Returns the possibly longer line list and the possibly shorter notes.
    """
    smaller = image.resize(
        (round(image.width * OCR_RETRY_SCALE), round(image.height * OCR_RETRY_SCALE))
    )
    second_pass, _ = ocr.recognize(smaller, size, language, installed, seconds)
    ocr_lines = ocr_lines + [
        line
        for line in second_pass
        if not any(overlaps(line["bbox"], seen["bbox"]) for seen in ocr_lines)
    ]
    if ocr_lines:
        notes = [note for note in notes if note != ocr.NO_WORDS]
    return ocr_lines, notes


def mark_painted_witnesses(ocr_lines, painted):
    """Note on each OCR line whether painted text says the same thing there.

    OCR and painted text agreeing in the same place is the strongest evidence
    this tool has that a line is really visible.
    """
    by_tokens = defaultdict(list)
    for line in painted:
        by_tokens[key(line["text"])].append(line)
    for line in ocr_lines:
        witness = next(
            (
                candidate
                for candidate in by_tokens[key(line["text"])]
                if overlaps(candidate["bbox"], line["bbox"])
            ),
            None,
        )
        line["painted_match"] = witness is not None
        if witness:
            line["painted_source"] = witness


def preview_jpeg(image):
    """A small base64 JPEG of the page for the report. OCR used the full raster."""
    image.thumbnail(PREVIEW_SIZE)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=78)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def inspect_page(
    mupdf_page, pdfium_page, number, size, language, installed, ocr_seconds, is_pdf
):
    """Render one page, read it, and collect its structural evidence.

    Returns the page entry for the report and the page's hidden spans.
    `ocr_seconds` is a function giving the time one OCR pass may take now.
    """
    width, height = size
    scale = min(RENDER_SCALE, RENDER_LONGEST_SIDE / max(width, height))
    bitmap = pdfium_page.render(scale=scale, draw_annots=True)
    image = bitmap.to_pil().convert("RGB")
    bitmap.close()
    pdfium_page.close()

    ocr_lines, notes = ocr.recognize(image, size, language, installed, ocr_seconds())

    # Structural coordinates are only trusted when both libraries agree on the
    # page size. Otherwise boxes could point at the wrong place.
    same_geometry = (
        abs(mupdf_page.rect.width - width) <= 1
        and abs(mupdf_page.rect.height - height) <= 1
    )
    painted = painted_lines(mupdf_page) if same_geometry else []

    can_ocr = bool(set(language.split("+")) & installed)
    if can_ocr and unread_lines(painted, ocr_lines):
        ocr_lines, notes = retry_ocr_for_gaps(
            image, size, language, installed, ocr_seconds(), ocr_lines, notes
        )
    mark_painted_witnesses(ocr_lines, painted)

    hidden = []
    if is_pdf:
        if same_geometry:
            hidden = hidden_spans(mupdf_page)
        else:
            notes.append(
                "Renderer geometry differs; structural coordinates unavailable."
            )
        if len(hidden) > MAX_HIDDEN_SPANS_PER_PAGE:
            raise Rejected("Too many structural spans.")

    page = {
        "number": number,
        "width": width,
        "height": height,
        "ocr": ocr_lines,
        "unread": unread_lines(painted, ocr_lines),
        "warnings": notes,
        "preview_jpeg": preview_jpeg(image),
    }
    return page, hidden


def tool_version(command):
    """First line of a tool's version output, or None if it cannot be run."""
    try:
        output = subprocess.check_output(
            command, stderr=subprocess.DEVNULL, timeout=3, text=True
        ).splitlines()
        return output[0][:140] if output else None
    except (OSError, subprocess.SubprocessError):
        return None


def analyze(path, parser, language, soffice):
    """Build the full report for the document at `path`."""
    deadline = time.monotonic() + WORK_SECONDS
    require_isolated_network()

    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    is_pdf = data.startswith(b"%PDF-")
    captured = CAPTURE.is_file()
    if parser == "auto":
        parser = "pypdf" if is_pdf else "python-docx"
    if (parser == "python-docx") == is_pdf:
        raise Rejected("Parser does not support this format.")

    # The parser always reads the original bytes. For DOCX, LibreOffice's PDF
    # is only the picture to compare against, never a source of parser text.
    segments = []
    if is_pdf:
        render_path = path
    else:
        office.validate(path)
        if not captured:
            segments = docx_paragraph_segments(path)
        render_path = office.render_docx(path, soffice)

    installed = ocr.languages()
    pages, hidden_by_page = [], {}
    with (
        pymupdf.open(render_path) as mupdf_doc,
        pdfium.PdfDocument(str(render_path)) as pdfium_doc,
    ):
        page_count = len(mupdf_doc)
        if (
            mupdf_doc.needs_pass
            or not 0 < page_count <= MAX_PAGES
            or page_count != len(pdfium_doc)
        ):
            raise Rejected("Encrypted, oversized or inconsistent document.")

        if captured:
            identity, segments = capture.load(CAPTURE, digest, page_count)
        else:
            identity = {"name": parser, "version": version(parser)}
        pypdf_reader = PdfReader(path) if not captured and parser == "pypdf" else None
        if pypdf_reader and len(pypdf_reader.pages) != page_count:
            raise Rejected("Parser and renderer disagree on page count.")

        for index, mupdf_page in enumerate(mupdf_doc):
            number = index + 1
            pdfium_page = pdfium_doc[index]
            size = pdfium_page.get_size()
            if not all(
                math.isfinite(side) and MIN_PAGE_POINTS <= side <= MAX_PAGE_POINTS
                for side in size
            ):
                raise Rejected("Unsupported page geometry.")

            if is_pdf and not captured:
                if pypdf_reader is not None:
                    text = pypdf_reader.pages[index].extract_text()
                else:
                    text = mupdf_page.get_text("text", sort=False)
                segments.append(
                    {"text": text or "", "page": number, "source": f"PDF page {number}"}
                )
            if (
                sum(len(segment["text"]) for segment in segments)
                > MAX_PARSER_CHARACTERS
                or len(segments) > MAX_SEGMENTS
            ):
                raise Rejected("Parser output exceeds the alignment budget.")

            def ocr_seconds(pages_left=page_count - index):
                # Each remaining page gets an equal share of the time left.
                share = (deadline - time.monotonic()) / pages_left
                return min(OCR_SECONDS_PER_PAGE, share)

            page, hidden = inspect_page(
                mupdf_page,
                pdfium_page,
                number,
                size,
                language,
                installed,
                ocr_seconds,
                is_pdf,
            )
            pages.append(page)
            if is_pdf:
                hidden_by_page[number] = hidden

    findings = compare(segments, pages, hidden_by_page)
    if len(findings) > MAX_FINDINGS:
        raise Rejected("Evidence exceeds report budget.")

    tools = {
        "pymupdf": version("pymupdf"),
        "pdfium": str(PDFIUM_INFO),
        "tesseract": tool_version(["tesseract", "--version"]),
        "ocr_models_available": sorted(installed & {"eng", "ara"}),
    }
    if not is_pdf:
        tools["libreoffice"] = tool_version([soffice, "--version"])
    return {
        "schema_version": "1.1",
        "sha256": digest,
        "format": "PDF" if is_pdf else "DOCX",
        "parser": identity,
        "parser_mode": "captured" if captured else "executed",
        "renderer": "PDFium" if is_pdf else "LibreOffice Writer → PDFium",
        "pdfium_adapter_version": version("pypdfium2"),
        "ocr_language": language,
        "tools": tools,
        "coordinate_space": "display page points, top-left origin, [x0,y0,x1,y1]",
        "segments": segments,
        "pages": pages,
        "findings": findings,
        "policy": outcome(findings),
        "reconciled": reconcile(segments, findings),
    }


def main():
    """Run one job and always print a JSON object, even on failure.

    Error text is fixed wording on purpose: messages from native libraries can
    contain document content.
    """
    try:
        result = analyze(DOCUMENT, *sys.argv[1:])
    except Rejected as error:
        result = {"kind": "rejected", "error": str(error)}
    except (
        BadZipFile,
        XMLSyntaxError,
        pymupdf.FileDataError,
        pdfium.PdfiumError,
        PyPdfError,
    ):
        result = {
            "kind": "rejected",
            "error": "Invalid, unsupported or over-budget document.",
        }
    except Unavailable as error:
        result = {"kind": "unavailable", "error": str(error)}
    except Exception:  # noqa: BLE001
        result = {
            "kind": "unavailable",
            "error": "Worker could not complete verification.",
        }
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
