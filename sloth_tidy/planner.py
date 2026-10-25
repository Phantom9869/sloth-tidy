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


def target_for(src: Path, root: Path, sug: Suggestion, taken: set[Path]) -> Path:
    category = sug.category if sug.category in CATEGORIES else "Misc"
    stem = slugify(sug.stem) if sug.stem else ""
    if not stem:
        stem = src.stem  # keep the original name rather than invent a bad one
    suffix = src.suffix.lower()
    folder = root / category
    candidate = folder / f"{stem}{suffix}"
    n = 2
    # never overwrite: neither something already on disk nor something planned earlier
    while candidate.exists() or candidate in taken:
        candidate = folder / f"{stem}-{n}{suffix}"
        n += 1
    # belt and braces: the result must live inside root/category
    assert candidate.resolve().parent == folder.resolve()
    return candidate
