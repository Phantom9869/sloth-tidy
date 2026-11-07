"""Turn suggestions into a concrete, collision-free move plan."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .brain import CATEGORIES, Suggestion

MAX_STEM = 60
MAX_WORDS = 6


@dataclass
class Move:
    src: Path
    dst: Path
    source: str  # "model" | "fallback"
    seconds: float = 0.0


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\.[a-z0-9]{1,5}$", "", text)  # models sometimes add an extension
    text = re.sub(r"[^a-z0-9]+", "-", text)
    words = [w for w in text.split("-") if w][:MAX_WORDS]
    text = "-".join(words)
    if len(text) > MAX_STEM:  # cut at a word boundary, not mid-word
        text = text[:MAX_STEM].rsplit("-", 1)[0]
    return text


def _real_dir_slot(root: Path, folder: Path) -> bool:
    """True if folder is inside root and is a real directory, or does not exist yet."""
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        return False
    try:
        folder.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def _category_dir(root: Path, category: str) -> Path:
    """Where files of this category go.

    rename follows a directory symlink, and mkdir cannot turn a file into a
    directory. Notes-2, Notes-3, ... keep the file inside the tidy folder,
    same idea as collision suffixes on filenames.
    """
    n = 1
    while True:
        folder = root / (category if n == 1 else f"{category}-{n}")
        if _real_dir_slot(root, folder):
            return folder
        n += 1


def target_for(src: Path, root: Path, sug: Suggestion, taken: set[Path]) -> Path:
    category = sug.category if sug.category in CATEGORIES else "Misc"
    stem = slugify(sug.stem) if sug.stem else ""
    if not stem:
        stem = src.stem  # keep the original name rather than invent a bad one
    suffix = src.suffix.lower()
    folder = _category_dir(root, category)
    candidate = folder / f"{stem}{suffix}"
    n = 2
    # never overwrite: neither something already on disk nor something planned earlier
    while candidate.exists() or candidate in taken:
        candidate = folder / f"{stem}-{n}{suffix}"
        n += 1
    # belt and braces: the result must live inside the chosen folder
    assert candidate.resolve().parent == folder.resolve()
    return candidate
