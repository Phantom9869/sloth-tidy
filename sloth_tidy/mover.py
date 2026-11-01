"""Apply a plan and remember how to take it back."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .planner import Move
from .scanner import STATE_DIR


def log_dir(root: Path) -> Path:
    return root / STATE_DIR


def apply(root: Path, moves: list[Move]) -> Path | None:
    if not moves:
        return None
    done: list[dict] = []
    for mv in moves:
        if not mv.src.is_file():  # deleted or moved away since planning; nothing to do
            continue
        if mv.dst.exists():  # something appeared since planning; skip instead of clobbering
            continue
        mv.dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(mv.src, mv.dst)  # same filesystem, so this is atomic
        except OSError:
            continue  # keep going, so the moves already made still get an undo log
        done.append({"from": str(mv.src.relative_to(root)), "to": str(mv.dst.relative_to(root))})
    if not done:
        return None
    ld = log_dir(root)
    ld.mkdir(exist_ok=True)
    path = ld / f"undo-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**6:06d}.json"
    path.write_text(json.dumps({"created": time.time(), "moves": done}, indent=2))
    return path


def latest_log(root: Path) -> Path | None:
    ld = log_dir(root)
    if not ld.is_dir():
        return None
    logs = sorted(ld.glob("undo-*.json"))
    return logs[-1] if logs else None


def undo(root: Path) -> tuple[int, int, Path | None]:
    """Reverse the most recent run. Returns (restored, skipped, log_path)."""
    log = latest_log(root)
    if log is None:
        return 0, 0, None
    data = json.loads(log.read_text())
    restored = skipped = 0
    for entry in reversed(data["moves"]):
        src = root / entry["from"]
        dst = root / entry["to"]
        if dst.exists() and not src.exists():
            os.rename(dst, src)
            restored += 1
            try:
                dst.parent.rmdir()  # only succeeds if the category folder is now empty
            except OSError:
                pass
        else:
            skipped += 1
    log.rename(log.with_suffix(".undone"))
    return restored, skipped, log


# --- remembered plans -------------------------------------------------------
# A dry run saves its plan. A later --apply reuses it if none of the files changed,
# so the plan you read is exactly the plan that runs (and the model isn't asked twice).

def _fingerprint(path: Path) -> list:
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def save_plan(root: Path, moves: list[Move], model: str) -> Path:
    ld = log_dir(root)
    ld.mkdir(exist_ok=True)
    data = {
        "model": model,
        "moves": [
            {"from": str(m.src.relative_to(root)), "to": str(m.dst.relative_to(root)),
             "source": m.source, "seconds": m.seconds, "fp": _fingerprint(m.src)}
            for m in moves
        ],
    }
    path = ld / "plan.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def load_plan(root: Path, current: list[Path], model: str) -> list[Move] | None:
    path = log_dir(root) / "plan.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return None
    if data.get("model") != model:
        return None
    entries = data.get("moves", [])
    if sorted(e["from"] for e in entries) != sorted(str(p.relative_to(root)) for p in current):
        return None
    moves = []
    for e in entries:
        src, dst = root / e["from"], root / e["to"]
        if not src.is_file() or _fingerprint(src) != e["fp"] or dst.exists():
            return None
        moves.append(Move(src, dst, e["source"], 0.0))
    return moves


def forget_plan(root: Path) -> None:
    try:
        (log_dir(root) / "plan.json").unlink()
    except FileNotFoundError:
        pass
