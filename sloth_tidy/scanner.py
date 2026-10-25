"""Find files worth tidying and pull a short, cheap snippet out of each one."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

STATE_DIR = ".sloth-tidy"

# Things a browser or editor is still writing. Touching these mid-download is rude.
PARTIAL_SUFFIXES = {".crdownload", ".part", ".partial", ".tmp", ".download", ".swp"}

TEXT_SUFFIXES = {
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".log", ".html", ".htm", ".xml",
    ".py", ".js", ".ts", ".go", ".rs", ".c", ".h", ".cpp", ".java", ".rb",
    ".sh", ".bash", ".ps1", ".sql", ".ipynb",
}

SNIPPET_CHARS = 600


@dataclass
class FileInfo:
    path: Path
    size: int
    mtime: float
    snippet: str  # may be empty; the model then works from name + metadata only

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()


def _read_text(path: Path, limit: int = SNIPPET_CHARS) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _read_pdf(path: Path, limit: int = SNIPPET_CHARS) -> str:
    """Use poppler's pdftotext if it is installed; otherwise give up quietly."""
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    try:
        out = subprocess.run(
            [exe, "-l", "2", "-q", str(path), "-"],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.decode("utf-8", errors="replace")[:limit]


def snippet_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        raw = _read_text(path)
    elif suffix == ".pdf":
        raw = _read_pdf(path)
    else:
        raw = ""  # images, archives, binaries: name + size is all we look at
    if suffix in (".html", ".htm", ".xml"):
        raw = re.sub(r"<[^>]+>", " ", raw)  # the words matter, the tags don't
    # collapse whitespace so the prompt stays small
    return " ".join(raw.split())[:SNIPPET_CHARS]


def is_candidate(path: Path) -> bool:
    name = path.name
    if not path.is_file() or path.is_symlink():
        return False
    if name.startswith("."):
        return False
    if path.suffix.lower() in PARTIAL_SUFFIXES:
        return False
    return True


def scan(folder: Path, min_age: float = 0.0, now: float | None = None) -> list[FileInfo]:
    """Top-level files only. Folders the user already made are left alone on purpose."""
    now = time.time() if now is None else now
    found: list[FileInfo] = []
    for entry in sorted(folder.iterdir()):
        if entry.name == STATE_DIR or not is_candidate(entry):
            continue
        st = entry.stat()
        if now - st.st_mtime < min_age:
            continue  # still "fresh"; the sloth waits
        found.append(FileInfo(entry, st.st_size, st.st_mtime, snippet_for(entry)))
    return found
