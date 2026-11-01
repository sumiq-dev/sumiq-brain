"""Turn source files into plain text. Stage 1 handles text-like files only;
PDF/Office/OCR/audio extractors plug in here later."""

from __future__ import annotations

import mimetypes
from pathlib import Path

TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".org", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".xml", ".html", ".htm", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".swift", ".lua", ".sh", ".sql", ".gd", ".shader",
}


def guess_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def extract_text(path: Path) -> str | None:
    """Return the file's text, or None if no extractor supports it (yet)."""
    if path.suffix.lower() in TEXT_EXTENSIONS or guess_mime(path).startswith("text/"):
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="latin-1")
    return None
