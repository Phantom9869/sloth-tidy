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
        # A symlinked category, a "../" destination, or an absolute path would
        # rename the file out of the folder. Skip it; do not create parents there.
        if not _under(root, mv.src) or not _under(root, mv.dst):
            continue
        # is_file() follows a symlink, so a file replaced by a link after
        # planning would otherwise be renamed. Scanner leaves symlinks alone;
        # apply should too. A broken symlink at the destination also counts
        # as something already there (exists() is False for those).
        if mv.src.is_symlink() or not mv.src.is_file():
            continue
        if mv.dst.exists() or mv.dst.is_symlink():
            continue
        try:
            # mkdir is in here too: if the category name is already a file,
            # FileExistsError must not abandon moves that already succeeded.
            mv.dst.parent.mkdir(parents=True, exist_ok=True)
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


class UndoError(ValueError):
    """The undo log is damaged or tries to touch files outside the folder."""


def _under(root: Path, path: Path) -> bool:
    """True if path stays inside root and does not pass through a directory symlink.

    rename follows a directory symlink, so a category named Notes that points
    elsewhere would move the file out of the folder. The last component is
    allowed to be a symlink: rename moves that link and does not follow it.
    Resolving the whole path would follow the link and refuse undo of every
    other file in the log. A symlink loop used to raise RuntimeError here.
    """
    try:
        lexical = path.relative_to(root)
    except ValueError:
        return False
    # Empty parts means the path is root itself ("" or "."). Renaming the
    # tidy folder is not an undo. ".." is an escape even if a later resolve
    # would land back inside.
    if not lexical.parts or ".." in lexical.parts:
        return False
    current = root
    for part in lexical.parts[:-1]:
        current = current / part
        try:
            if current.is_symlink():
                return False
            current.resolve().relative_to(root.resolve())
        except (ValueError, OSError, RuntimeError):
            return False
    return True


def undo(root: Path) -> tuple[int, int, Path | None]:
    """Reverse the most recent run. Returns (restored, skipped, log_path).

    Raises UndoError if the log is unreadable or any entry points outside root;
    the log file is left in place so it can be inspected.
    """
    log = latest_log(root)
    if log is None:
        return 0, 0, None
    try:
        data = json.loads(log.read_text())
        entries = data["moves"]
        if not isinstance(entries, list):
            raise TypeError("moves must be a list")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise UndoError(f"undo log is unreadable: {log.name}") from exc

    # Refuse the whole log if any entry would escape the folder (a tampered log
    # must not rename files onto the parent directory, /etc, ...).
    planned: list[tuple[Path, Path]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise UndoError(f"undo log is malformed: {log.name}")
        # JSON null, a number, or a list is still corrupt. Joining those onto
        # root raises TypeError, which the CLI does not turn into "Can't undo".
        src_rel, dst_rel = entry.get("from"), entry.get("to")
        if not isinstance(src_rel, str) or not isinstance(dst_rel, str):
            raise UndoError(f"undo log is malformed: {log.name}")
        try:
            src, dst = root / src_rel, root / dst_rel
        except (TypeError, ValueError) as exc:
            raise UndoError(f"undo log is malformed: {log.name}") from exc
        if not _under(root, src) or not _under(root, dst):
            raise UndoError(f"undo log points outside the folder: {log.name}")
        planned.append((src, dst))

    restored = skipped = 0
    for src, dst in reversed(planned):
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
    entries = []
    for m in moves:
        try:
            fp = _fingerprint(m.src)
        except FileNotFoundError:
            continue  # deleted or moved away while the model was still thinking
        entries.append({"from": str(m.src.relative_to(root)), "to": str(m.dst.relative_to(root)),
                        "source": m.source, "seconds": m.seconds, "fp": fp})
    data = {"model": model, "moves": entries}
    path = ld / "plan.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def load_plan(root: Path, current: list[Path], model: str) -> list[Move] | None:
    path = log_dir(root) / "plan.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    # Anything we cannot prove is a safe in-folder plan of these exact files
    # is ignored, so --apply builds a fresh plan instead of crashing or
    # replaying a destination outside the folder.
    if not isinstance(data, dict) or data.get("model") != model:
        return None
    entries = data.get("moves", [])
    if not isinstance(entries, list):
        return None
    try:
        froms = [e["from"] for e in entries]
    except (TypeError, KeyError, AttributeError):
        return None
    if any(not isinstance(item, str) for item in froms):
        return None
    if sorted(froms) != sorted(str(p.relative_to(root)) for p in current):
        return None
    moves = []
    for e in entries:
        if not isinstance(e, dict):
            return None
        dst_rel, source = e.get("to"), e.get("source")
        if not isinstance(dst_rel, str) or not isinstance(source, str):
            return None
        try:
            src, dst = root / e["from"], root / dst_rel
            if not _under(root, src) or not _under(root, dst):
                return None
            unchanged = src.is_file() and _fingerprint(src) == e.get("fp") and not dst.exists()
        except (ValueError, OSError):
            return None
        if not unchanged:
            return None
        moves.append(Move(src, dst, source, 0.0))
    return moves


def forget_plan(root: Path) -> None:
    try:
        (log_dir(root) / "plan.json").unlink()
    except FileNotFoundError:
        pass
