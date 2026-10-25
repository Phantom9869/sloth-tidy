"""The thinking part: ask a local open-weight model (via Ollama) where a file belongs.

If the model is missing, slow, or says something odd, fall back to plain extension rules.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from .scanner import FileInfo

CATEGORIES = [
    "Documents", "Receipts", "Code", "Images", "Notes", "Data",
    "Archives", "Installers", "Audio", "Video", "Misc",
]

EXT_RULES = {
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg", ".bmp"},
    "Code": {".py", ".js", ".ts", ".go", ".rs", ".c", ".h", ".cpp", ".java", ".rb",
             ".sh", ".bash", ".ps1", ".sql", ".ipynb"},
    "Documents": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".html", ".htm"},
    "Notes": {".md", ".txt", ".rst"},
    "Data": {".csv", ".tsv", ".json", ".xlsx", ".xls", ".yaml", ".yml", ".xml"},
    "Archives": {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".xz"},
    "Installers": {".deb", ".rpm", ".exe", ".msi", ".dmg", ".appimage", ".apk"},
    "Audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a"},
    "Video": {".mp4", ".mkv", ".mov", ".webm", ".avi"},
}

SYSTEM_PROMPT = (
    "You tidy a messy Downloads folder. For each file you get its name, size and "
    "the first few hundred characters of content (if readable). Pick ONE category "
    "from this list: " + ", ".join(CATEGORIES) + ". "
    "Then suggest a short, descriptive filename stem: lowercase words separated by "
    "hyphens, 2 to 5 words, no extension. "
    "Describe what the file IS, using only words and facts that appear in its content. "
    "Do not invent dates, names or topics. "
    "Receipts means a single invoice, bill or order confirmation. "
    "A table of many transactions is Data. "
    "Code is only for source code files; a PDF or web page about a technical topic "
    "is Documents. Keep the extension's usual category unless the content clearly "
    "says otherwise. "
    "Reply with JSON only."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "name": {"type": "string"},
    },
    "required": ["category", "name"],
}


@dataclass
class Suggestion:
    category: str
    stem: str | None  # None means "keep the original name"
    source: str  # "model" or "fallback"


class LocalOnlyError(ValueError):
    pass


def ensure_local(host: str) -> str:
    """Refuse anything that is not loopback. Your files' contents never leave the machine."""
    parsed = urlparse(host)
    hostname = parsed.hostname
    if parsed.scheme not in ("http", "https") or not hostname:
        raise LocalOnlyError(f"not a valid Ollama URL: {host!r}")
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise LocalOnlyError(f"cannot resolve {hostname!r}") from exc
    for addr in addrs:
        if not ipaddress.ip_address(addr.split("%")[0]).is_loopback:
            raise LocalOnlyError(
                f"{hostname} resolves to {addr}, which is not localhost. "
                "sloth-tidy only talks to a model on this machine."
            )
    return host.rstrip("/")


def heuristic(info: FileInfo) -> Suggestion:
    for cat, exts in EXT_RULES.items():
        if info.suffix in exts:
            return Suggestion(cat, None, "fallback")
    return Suggestion("Misc", None, "fallback")


def build_prompt(info: FileInfo) -> str:
    lines = [
        f"filename: {info.path.name}",
        f"size_bytes: {info.size}",
        f"extension_usually_means: {heuristic(info).category}",
    ]
    lines.append(f"content_start: {info.snippet}")
    return "\n".join(lines)


class OllamaBrain:
    def __init__(self, model: str = "gemma3:4b", host: str = "http://127.0.0.1:11434",
                 timeout: float = 120.0):
        self.model = model
        self.host = ensure_local(host)
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.host + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def available(self) -> bool:
        try:
            req = urllib.request.Request(self.host + "/api/tags")
            with urllib.request.urlopen(req, timeout=3) as resp:
                tags = json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError):
            return False
        names = {m.get("name") for m in tags.get("models", [])}
        return self.model in names or f"{self.model}:latest" in names

    def suggest(self, info: FileInfo) -> Suggestion:
        if not info.snippet:
            # Lesson from the first real run: with nothing to read, a small model
            # happily invents "landscape-sunset-view-coastal-cliffs.jpg" for a blank
            # photo. Extension rules are honest about what they don't know.
            return heuristic(info)
        payload = {
            "model": self.model,
            "stream": False,
            "format": RESPONSE_SCHEMA,
            "options": {"temperature": 0.1, "num_predict": 128, "seed": 7},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(info)},
            ],
        }
        try:
            data = self._post("/api/chat", payload)
            answer = json.loads(data["message"]["content"])
        except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError):
            return heuristic(info)
        return parse_answer(answer, info)


def parse_answer(answer: object, info: FileInfo) -> Suggestion:
    """Trust, but verify. Small models occasionally improvise."""
    if not isinstance(answer, dict):
        return heuristic(info)
    category = str(answer.get("category", "")).strip().title()
    if category not in CATEGORIES:
        category = heuristic(info).category
    stem = answer.get("name")
    stem = stem if isinstance(stem, str) and stem.strip() else None
    return Suggestion(category, stem, "model")


class HeuristicBrain:
    """Used with --no-model, or when Ollama is not running."""

    model = "extension rules"

    def suggest(self, info: FileInfo) -> Suggestion:
        return heuristic(info)
