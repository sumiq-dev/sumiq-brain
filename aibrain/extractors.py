"""Turn source files into plain text (markdown-ish, keeping headings, slides, pages and tables).

Office formats (docx, pptx, xlsx) are zip archives of XML and are parsed with the standard
library. PDFs use pypdf. OCR and audio transcription plug in here later.
"""

from __future__ import annotations

import mimetypes
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".org", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".xml", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".swift", ".lua", ".sh", ".sql", ".gd", ".shader",
}
MAX_SHEET_ROWS = 2000

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def guess_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def extract_text(path: Path) -> str | None:
    """Return the file's text, or None if it has no extractable text (unsupported type,
    scanned PDF without OCR, corrupt file)."""
    ext = path.suffix.lower()
    handler = {".pdf": _pdf, ".docx": _docx, ".pptx": _pptx, ".xlsx": _xlsx,
               ".html": _html, ".htm": _html}.get(ext)
    try:
        if handler:
            text = handler(path)
        elif ext in TEXT_EXTENSIONS or guess_mime(path).startswith("text/"):
            text = _read_text(path)
        else:
            return None
    except Exception:  # corrupt or encrypted files: treat as "no text", never crash ingestion
        return None
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    return text + "\n" if text else None


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# --------------------------------------------------------------------------- PDF

def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        return ""
    pages = []
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"## Page {i}\n\n{text}")
    return "\n\n".join(pages)


# -------------------------------------------------------------------------- DOCX

def _docx_paragraph(p: ET.Element) -> str:
    parts = []
    for node in p.iter():
        if node.tag == f"{W}t":
            parts.append(node.text or "")
        elif node.tag == f"{W}tab":
            parts.append("\t")
        elif node.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    text = "".join(parts).strip()
    style = p.find(f"{W}pPr/{W}pStyle")
    if text and style is not None:
        m = re.match(r"(?i)heading\s*(\d)", style.get(f"{W}val", ""))
        if m:
            return f"{'#' * min(int(m.group(1)), 6)} {text}"
        if style.get(f"{W}val", "").lower() == "title":
            return f"# {text}"
    if text and p.find(f"{W}pPr/{W}numPr") is not None:
        return f"- {text}"
    return text


def _docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    blocks = []
    for child in body if body is not None else []:
        if child.tag == f"{W}p":
            blocks.append(_docx_paragraph(child))
        elif child.tag == f"{W}tbl":
            for row in child.iter(f"{W}tr"):
                cells = [" ".join(filter(None, (_docx_paragraph(p) for p in tc.iter(f"{W}p"))))
                         for tc in row.iter(f"{W}tc")]
                blocks.append("| " + " | ".join(cells) + " |")
    return "\n\n".join(b for b in blocks if b)


# -------------------------------------------------------------------------- PPTX

def _numbered(names: list[str], pattern: str) -> list[str]:
    found = [(int(m.group(1)), n) for n in names if (m := re.fullmatch(pattern, n))]
    return [n for _, n in sorted(found)]


def _drawing_paragraphs(xml: bytes) -> list[str]:
    root = ET.fromstring(xml)
    out = []
    for p in root.iter(f"{A}p"):
        text = "".join(t.text or "" for t in p.iter(f"{A}t")).strip()
        if text:
            out.append(text)
    return out


def _pptx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        slides = []
        for i, name in enumerate(_numbered(names, r"ppt/slides/slide(\d+)\.xml"), 1):
            parts = [f"## Slide {i}", *_drawing_paragraphs(z.read(name))]
            notes = f"ppt/notesSlides/notesSlide{name.rsplit('slide', 1)[1]}"
            if notes in names:
                text = [t for t in _drawing_paragraphs(z.read(notes)) if not t.isdigit()]
                if text:
                    parts.append("Notes: " + " ".join(text))
            slides.append("\n\n".join(parts))
    return "\n\n".join(slides)


# -------------------------------------------------------------------------- XLSX

def _xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(f"{PR}Relationship"):
                rels[rel.get("Id")] = "xl/" + rel.get("Target", "").lstrip("/").removeprefix("xl/")
        sheets = []
        for sheet in ET.fromstring(z.read("xl/workbook.xml")).iter(f"{S}sheet"):
            target = rels.get(sheet.get(f"{R}id"))
            if target in names:
                sheets.append((sheet.get("name", "Sheet"), target))

        out = []
        for name, target in sheets:
            rows = []
            for row in ET.fromstring(z.read(target)).iter(f"{S}row"):
                cells = []
                for c in row.iter(f"{S}c"):
                    kind, v = c.get("t"), c.find(f"{S}v")
                    if kind == "s" and v is not None and v.text and v.text.isdigit() and int(v.text) < len(shared):
                        cells.append(shared[int(v.text)])
                    elif kind == "inlineStr":
                        cells.append("".join(t.text or "" for t in c.iter(f"{S}t")))
                    else:
                        cells.append(v.text if v is not None and v.text else "")
                if any(cells):
                    rows.append("| " + " | ".join(cells) + " |")
                if len(rows) >= MAX_SHEET_ROWS:
                    rows.append(f"(truncated after {MAX_SHEET_ROWS} rows)")
                    break
            out.append(f"## Sheet: {name}\n\n" + "\n".join(rows))
    return "\n\n".join(out)


# -------------------------------------------------------------------------- HTML

class _HtmlText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "pre"}
    SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
            if tag[0] == "h" and tag[1:].isdigit():
                self.parts.append("#" * int(tag[1:]) + " ")
            elif tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def _html(path: Path) -> str:
    parser = _HtmlText()
    parser.feed(_read_text(path))
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(lines)
