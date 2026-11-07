"""Turn an uploaded document into text, tables and numbers without a cloud service.

The extractor is deliberately conservative: it caps size and rows, reports what it
skipped, and never executes a formula. Numbers stay as text until the analysis engine
parses them with Decimal.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_TEXT_CHARS = 180_000
MAX_TABLE_ROWS = 400
MAX_TABLE_COLS = 40
MAX_TABLES = 10
MAX_PDF_PAGES = 40

SUPPORTED = {
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".csv": "csv",
    ".docx": "docx",
    ".pdf": "pdf",
    ".txt": "text",
    ".md": "text",
    ".json": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".tif": "image",
    ".tiff": "image",
    ".bmp": "image",
    ".heic": "image",
}
ACCEPTED = ", ".join(sorted(SUPPORTED))

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class AttachmentError(ValueError):
    """A user-facing extraction failure with a next step."""

    def __init__(self, code: str, message: str, remediation: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        try:
            text = format(Decimal(str(value)).normalize(), "f")
        except InvalidOperation:
            text = str(value)
        return text
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value).strip()


def _dedupe_headers(row: list[str]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(row):
        name = " ".join(str(raw or "").split()) or f"Column {index + 1}"
        count = seen.get(name.lower(), 0) + 1
        seen[name.lower()] = count
        headers.append(name if count == 1 else f"{name} ({count})")
    return headers


def _clean_rows(rows: list[list[str]]) -> list[list[str]]:
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    return rows


def _table(name: str, rows: list[list[str]]) -> dict[str, Any] | None:
    rows = [row for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        return None
    headers = _dedupe_headers([str(cell) for cell in rows[0]][:MAX_TABLE_COLS])
    width = len(headers)
    body = []
    for row in rows[1 : MAX_TABLE_ROWS + 1]:
        padded = [str(cell) for cell in row[:width]] + [""] * max(0, width - len(row))
        body.append(padded[:width])
    return {
        "name": name[:80],
        "headers": headers,
        "rows": body,
        "row_count": max(0, len(rows) - 1),
        "column_count": len(rows[0]),
        "truncated": len(rows) - 1 > MAX_TABLE_ROWS or len(rows[0]) > MAX_TABLE_COLS,
    }


def _tables_text(tables: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for table in tables:
        chunks.append(f"[Table: {table['name']}]")
        chunks.append(" | ".join(table["headers"]))
        for index, row in enumerate(table["rows"], start=1):
            chunks.append(f"{index}: " + " | ".join(row))
        chunks.append("")
    return "\n".join(chunks)


def _xlsx(raw: bytes) -> dict[str, Any]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several unrelated types
        raise AttachmentError(
            "E_ATTACHMENT_FORMAT",
            f"The workbook could not be opened: {exc}.",
            "Save it as .xlsx (not .xls), remove password protection, and upload again.",
        ) from exc
    tables: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        for sheet in workbook.worksheets:
            if len(tables) >= MAX_TABLES:
                warnings.append(f"Only the first {MAX_TABLES} sheets were read; the rest were skipped.")
                break
            rows = [
                [_cell(value) for value in row]
                for row in sheet.iter_rows(values_only=True)
            ]
            table = _table(sheet.title, _clean_rows(rows))
            if table:
                tables.append(table)
            else:
                warnings.append(f"Sheet {sheet.title!r} was empty and was skipped.")
    finally:
        workbook.close()
    if not tables:
        raise AttachmentError(
            "E_ATTACHMENT_EMPTY",
            "The workbook has no readable rows.",
            "Add at least one header row and one data row, then upload again.",
        )
    return {
        "kind": "xlsx",
        "tables": tables,
        "text": _tables_text(tables),
        "warnings": warnings,
        "pages": len(tables),
    }


def _csv(raw: bytes) -> dict[str, Any]:
    text = _decode(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    table = _table("CSV", _clean_rows(rows))
    if table is None:
        raise AttachmentError(
            "E_ATTACHMENT_EMPTY",
            "The CSV has no readable rows.",
            "Save the file with a header row and at least one data row, then upload again.",
        )
    return {
        "kind": "csv",
        "tables": [table],
        "text": _tables_text([table]),
        "warnings": [] if table["truncated"] is False else ["The CSV was truncated at the row limit."],
        "pages": 1,
    }


def _docx(raw: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            total = sum(info.file_size for info in archive.infolist())
            if total > MAX_ARCHIVE_BYTES:
                raise AttachmentError(
                    "E_ATTACHMENT_TOO_LARGE",
                    "The document expands beyond the extraction limit.",
                    "Remove embedded images or split the document, then upload again.",
                )
            xml = archive.read("word/document.xml")
    except KeyError as exc:
        raise AttachmentError(
            "E_ATTACHMENT_FORMAT",
            "The file is not a Word document with a readable body.",
            "Save it as .docx (not .doc) or export it to PDF, then upload again.",
        ) from exc
    except zipfile.BadZipFile as exc:
        raise AttachmentError(
            "E_ATTACHMENT_FORMAT",
            "The .docx file is not a valid archive.",
            "Re-save the document from Word or a compatible editor, then upload again.",
        ) from exc

    root = ElementTree.fromstring(xml)
    body = root.find(f"{{{W_NS}}}body")
    if body is None:
        raise AttachmentError(
            "E_ATTACHMENT_EMPTY",
            "The document has no body.",
            "Add text and upload again.",
        )

    def paragraph_text(element: ElementTree.Element) -> str:
        return "".join(node.text or "" for node in element.iter(f"{{{W_NS}}}t")).strip()

    lines: list[str] = []
    tables: list[dict[str, Any]] = []
    for child in body:
        if child.tag == f"{{{W_NS}}}p":
            text = paragraph_text(child)
            if text:
                lines.append(text)
        elif child.tag == f"{{{W_NS}}}tbl" and len(tables) < MAX_TABLES:
            rows: list[list[str]] = []
            for row in child.iter(f"{{{W_NS}}}tr"):
                cells = []
                for cell in row.iter(f"{{{W_NS}}}tc"):
                    cells.append(" ".join(paragraph_text(part) for part in cell.iter(f"{{{W_NS}}}p")).strip())
                rows.append(cells)
            table = _table(f"Table {len(tables) + 1}", rows)
            if table:
                tables.append(table)
    text = "\n".join(lines)
    if tables:
        text = (text + "\n\n" if text else "") + _tables_text(tables)
    if not text.strip():
        raise AttachmentError(
            "E_ATTACHMENT_EMPTY",
            "No text was found in the Word document.",
            "Check that the content is not only images; export it to PDF or attach a screenshot instead.",
        )
    return {
        "kind": "docx",
        "tables": tables,
        "text": text,
        "warnings": [],
        "pages": max(1, text.count("\n") // 45 + 1),
    }


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _text(raw: bytes, suffix: str) -> dict[str, Any]:
    text = _decode(raw)
    if not text.strip():
        raise AttachmentError(
            "E_ATTACHMENT_EMPTY",
            "The file is empty.",
            "Add content and upload again.",
        )
    return {
        "kind": "text",
        "tables": [],
        "text": text,
        "warnings": [],
        "pages": max(1, text.count("\n") // 45 + 1),
        "format": suffix,
    }


def _pdf(raw: bytes) -> dict[str, Any]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:
        raise AttachmentError(
            "E_ATTACHMENT_FORMAT",
            f"The PDF could not be opened: {exc}.",
            "Remove password protection or re-export the PDF, then upload again.",
        ) from exc
    pages: list[str] = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append(text)
    joined = "\n\n".join(text.strip() for text in pages if text.strip())
    warnings: list[str] = []
    ocr = False
    if len(joined.strip()) < max(40, 20 * len(pages)):
        try:
            joined = _ocr_pdf(raw)
            ocr = True
            warnings.append("The PDF had no text layer, so local OCR was used.")
        except AttachmentError:
            raise AttachmentError(
                "E_ATTACHMENT_OCR",
                "The PDF has no text layer and local OCR is not available.",
                "Export the pages as PNG/JPG and attach them, or install Tesseract for OCR.",
            ) from None
    if not joined.strip():
        raise AttachmentError(
            "E_ATTACHMENT_EMPTY",
            "No text was found in the PDF.",
            "Export the pages as PNG/JPG and attach them, or paste the text into a .txt file.",
        )
    if len(reader.pages) > MAX_PDF_PAGES:
        warnings.append(f"Only the first {MAX_PDF_PAGES} pages were read.")
    return {
        "kind": "pdf",
        "tables": [],
        "text": joined,
        "warnings": warnings,
        "pages": min(len(reader.pages), MAX_PDF_PAGES),
        "ocr": ocr,
    }


_OCR_SCRIPT = r"""
import Foundation
import Vision
import AppKit
import PDFKit

func recognise(_ cg: CGImage) -> [String] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([request]) } catch { return [] }
    return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
}

func imageCG(_ url: URL) -> CGImage? {
    guard let image = NSImage(contentsOf: url), let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff) else { return nil }
    return rep.cgImage
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
if url.pathExtension.lowercased() == "pdf", let doc = PDFDocument(url: url) {
    for index in 0..<min(doc.pageCount, 40) {
        guard let page = doc.page(at: index) else { continue }
        let bounds = page.bounds(for: .mediaBox)
        let scale: CGFloat = min(2.0, 2200.0 / max(bounds.width, 1))
        let size = NSSize(width: bounds.width * scale, height: bounds.height * scale)
        let thumb = page.thumbnail(of: size, for: .mediaBox)
        guard let tiff = thumb.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let cg = rep.cgImage else { continue }
        print("--- page \(index + 1) ---")
        print(recognise(cg).joined(separator: "\n"))
    }
} else if let cg = imageCG(url) {
    print(recognise(cg).joined(separator: "\n"))
} else {
    fputs("cannot read file\n", stderr)
    exit(3)
}
"""


def _vision_binary() -> Path:
    cache = Path(tempfile.gettempdir()) / "costpilot-vision"
    cache.mkdir(parents=True, exist_ok=True)
    script = cache / "extract.swift"
    binary = cache / "extract"
    if not script.exists():
        script.write_text(textwrap.dedent(_OCR_SCRIPT), encoding="utf-8")
    stale = not binary.exists() or binary.stat().st_mtime < script.stat().st_mtime
    if stale:
        subprocess.run(
            [shutil.which("swiftc") or "swiftc", "-O", str(script), "-o", str(binary)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    return binary


def _run_ocr(path: Path) -> str:
    if sys.platform == "darwin" and shutil.which("swiftc"):
        try:
            binary = _vision_binary()
            result = subprocess.run(
                [str(binary), str(path)], capture_output=True, text=True, timeout=90
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (OSError, subprocess.SubprocessError):
            pass
    tesseract = shutil.which("tesseract")
    if tesseract and path.suffix.lower() != ".pdf":
        try:
            result = subprocess.run(
                [tesseract, str(path), "stdout", "-l", "eng"],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (OSError, subprocess.SubprocessError):
            pass
    raise AttachmentError(
        "E_ATTACHMENT_OCR",
        "No local OCR engine is available for this image.",
        "Install Tesseract (brew install tesseract), or attach a PDF with a text layer, "
        "a .txt file, or a spreadsheet.",
    )


def _ocr_pdf(raw: bytes) -> str:
    with tempfile.TemporaryDirectory(prefix="costpilot-pdf-") as folder:
        path = Path(folder) / "document.pdf"
        path.write_bytes(raw)
        return _run_ocr(path)


def _image(raw: bytes, suffix: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="costpilot-image-") as folder:
        path = Path(folder) / f"image{suffix}"
        path.write_bytes(raw)
        text = _run_ocr(path)
    if not text.strip():
        raise AttachmentError(
            "E_ATTACHMENT_EMPTY",
            "The OCR pass found no readable text in the image.",
            "Use a sharper screenshot with larger text, or attach the source document instead.",
        )
    return {
        "kind": "image",
        "tables": [],
        "text": text,
        "warnings": ["Text was read from an image with local OCR; check the quoted figures."],
        "pages": max(1, text.count("\n") // 45 + 1),
        "ocr": True,
    }


def _looks_like_dataset(tables: list[dict[str, Any]]) -> bool:
    from costpilot.data.loader import REQUIRED_FILES

    required = {name[:-4].lower() for name in REQUIRED_FILES}
    sheets = {table["name"].strip().lower() for table in tables}
    return len(required & sheets) >= max(3, len(required) // 2)


def extract(raw: bytes, filename: str) -> dict[str, Any]:
    """Extract one attachment. Raises AttachmentError with a next step."""
    name = Path(str(filename or "attachment")).name.strip() or "attachment"
    suffix = Path(name).suffix.lower()
    if not raw:
        raise AttachmentError(
            "E_ATTACHMENT_EMPTY",
            "The attached file is empty.",
            "Choose a non-empty file and upload it again.",
        )
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(
            "E_ATTACHMENT_TOO_LARGE",
            f"The file is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.",
            "Compress the file, remove embedded images, or split it into two attachments.",
        )
    kind = SUPPORTED.get(suffix)
    if kind is None:
        raise AttachmentError(
            "E_ATTACHMENT_FORMAT",
            f"{suffix or 'This file type'} is not supported.",
            f"Attach one of: {ACCEPTED}.",
        )
    if kind == "xlsx":
        payload = _xlsx(raw)
    elif kind == "csv":
        payload = _csv(raw)
    elif kind == "docx":
        payload = _docx(raw)
    elif kind == "pdf":
        payload = _pdf(raw)
    elif kind == "image":
        payload = _image(raw, suffix)
    else:
        payload = _text(raw, suffix)

    text = str(payload.get("text") or "")
    warnings = list(payload.pop("warnings", []))
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        warnings.append(
            f"The extract was cut at {MAX_TEXT_CHARS:,} characters; questions beyond that point need a smaller file."
        )
    for table in payload.get("tables", []):
        if table.get("truncated"):
            warnings.append(
                f"Sheet {table['name']!r} was truncated at {MAX_TABLE_ROWS} rows and "
                f"{MAX_TABLE_COLS} columns."
            )
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "name": name,
        "kind": payload["kind"],
        "sha256": digest,
        "size": len(raw),
        "text": text,
        "tables": payload.get("tables", []),
        "pages": int(payload.get("pages") or 1),
        "ocr": bool(payload.get("ocr", False)),
        "looks_like_dataset": _looks_like_dataset(payload.get("tables", [])),
        "warnings": warnings,
    }


def public_view(attachment: dict[str, Any], *, rows: int = 6) -> dict[str, Any]:
    """A summary safe to return to the chat; full text stays on the server."""
    preview_tables = []
    for table in attachment.get("tables", []):
        preview_tables.append(
            {
                "name": table["name"],
                "headers": table["headers"],
                "rows": table["rows"][:rows],
                "row_count": table["row_count"],
                "column_count": table["column_count"],
                "truncated": table["truncated"],
            }
        )
    text = str(attachment.get("text") or "")
    return {
        "attachment_id": attachment.get("attachment_id"),
        "name": attachment.get("name"),
        "kind": attachment.get("kind"),
        "size": attachment.get("size"),
        "sha256": attachment.get("sha256"),
        "pages": attachment.get("pages"),
        "ocr": attachment.get("ocr", False),
        "looks_like_dataset": attachment.get("looks_like_dataset", False),
        "tables": preview_tables,
        "text_preview": text[:1200],
        "characters": len(text),
        "warnings": attachment.get("warnings", []),
        "created_at": attachment.get("created_at"),
    }


def numeric_headers(table: dict[str, Any]) -> list[str]:
    """Headers whose cells are mostly numbers."""
    out = []
    for index, header in enumerate(table["headers"]):
        values = [row[index] for row in table["rows"] if index < len(row)]
        if _numeric_ratio(values) >= Decimal("0.5"):
            out.append(header)
    return out


def numeric_columns(attachment: dict[str, Any], table_name: str | None = None) -> list[str]:
    table = choose_table(attachment, table_name)
    if table is None:
        return []
    return numeric_headers(table)


def choose_table(attachment: dict[str, Any], name: str | None = None) -> dict[str, Any] | None:
    tables = attachment.get("tables") or []
    if not tables:
        return None
    if not name:
        return tables[0]
    wanted = str(name).strip().lower()
    for table in tables:
        if table["name"].strip().lower() == wanted:
            return table
    return None


_NUMBER_TEXT = re.compile(r"^[+-]?(?:[0-9][0-9,]*)(?:\.[0-9]+)?%?$")
_CURRENCY = re.compile(r"[₹$€£]")
_PAREN = re.compile(r"^\((.*)\)$")


def parse_number(text: str) -> Decimal | None:
    """Parse a displayed figure. Percent signs are kept as the number shown."""
    raw = str(text or "").strip()
    if not raw or raw in {"-", "—", "–", "n/a", "N/A", "NA"}:
        return None
    raw = raw.replace("\u00a0", " ").replace(" ", "")
    raw = raw.replace("₹", "").replace("$", "").replace("€", "").replace("£", "")
    raw = raw.replace("INR", "").replace("Rs.", "").replace("Rs", "")
    negative = False
    match = _PAREN.match(raw)
    if match:
        negative = True
        raw = match.group(1)
    if raw.endswith("%"):
        raw = raw[:-1]
    if not _NUMBER_TEXT.match(raw):
        return None
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    return -value if negative else value


def _numeric_ratio(values: list[str]) -> Decimal:
    if not values:
        return Decimal("0")
    numeric = sum(1 for value in values if parse_number(value) is not None)
    return Decimal(numeric) / Decimal(len(values))


def header_unit(header: str, values: list[str]) -> str:
    """Unit hint from the header and the printed cells, never assumed silently."""
    text = " ".join(values[:50])
    haystack = header + " " + text
    if "₹" in haystack or re.search(r"\bINR\b|Rs\.?", haystack):
        return "INR"
    if "$" in haystack or re.search(r"\bUSD\b", haystack):
        return "USD"
    if "€" in haystack or re.search(r"\bEUR\b", haystack):
        return "EUR"
    if "£" in haystack or re.search(r"\bGBP\b", haystack):
        return "GBP"
    if "%" in header or (values and sum(1 for v in values if v.strip().endswith("%")) > len(values) / 2):
        return "%"
    return "number"


def find_column(table: dict[str, Any], name: str | None) -> str | None:
    if not name:
        return None
    wanted = str(name).strip().lower()
    headers = [header.lower() for header in table["headers"]]
    if wanted in headers:
        return table["headers"][headers.index(wanted)]
    for header in table["headers"]:
        if wanted in header.lower() or header.lower() in wanted:
            return header
    compact = re.sub(r"[^a-z0-9]", "", wanted)
    for header in table["headers"]:
        if compact and compact in re.sub(r"[^a-z0-9]", "", header.lower()):
            return header
    return None


def column_values(table: dict[str, Any], header: str) -> list[str]:
    index = table["headers"].index(header)
    return [row[index] if index < len(row) else "" for row in table["rows"]]


def snippets(text: str, query: str, limit: int = 4) -> list[str]:
    """Return passages around a literal or word-level match."""
    clean = " ".join(str(text or "").split())
    if not clean:
        return []
    needle = str(query or "").strip()
    if not needle:
        return []
    lowered = clean.lower()
    positions = [match.start() for match in re.finditer(re.escape(needle.lower()), lowered)]
    if not positions:
        words = [word for word in re.findall(r"[a-zA-Z0-9]{3,}", needle.lower())]
        positions = [lowered.find(word) for word in words]
        positions = [position for position in positions if position >= 0]
    out: list[str] = []
    seen: set[str] = set()
    for position in positions[:limit]:
        start = max(0, position - 160)
        end = min(len(clean), position + len(needle) + 220)
        passage = clean[start:end].strip()
        if passage and passage not in seen:
            seen.add(passage)
            out.append(passage)
    return out


def accepted_formats() -> str:
    return ACCEPTED


def cache_dir() -> Path:
    root = Path(os.environ.get("COSTPILOT_CACHE", Path(tempfile.gettempdir()) / "costpilot"))
    root.mkdir(parents=True, exist_ok=True)
    return root
