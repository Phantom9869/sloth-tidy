"""Command line entry point.

    sloth-tidy ~/Downloads            # dry run: show the plan, touch nothing
    sloth-tidy ~/Downloads --apply    # actually move files, write an undo log
    sloth-tidy undo ~/Downloads       # put everything back
    sloth-tidy ~/Downloads --watch    # nap, wake up, tidy files that have settled
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .brain import HeuristicBrain, LocalOnlyError, OllamaBrain
from .mover import apply, forget_plan, load_plan, save_plan, undo
from .planner import Move, target_for
from .scanner import scan


def build_plan(root: Path, brain, min_age: float = 0.0) -> list[Move]:
    moves: list[Move] = []
    taken: set[Path] = set()
    for info in scan(root, min_age=min_age):
        t0 = time.perf_counter()
        sug = brain.suggest(info)
        elapsed = time.perf_counter() - t0
        dst = target_for(info.path, root, sug, taken)
        taken.add(dst)
        moves.append(Move(info.path, dst, sug.source, elapsed))
    return moves


def show(root: Path, moves: list[Move], out=sys.stdout) -> None:
    if not moves:
        print("Nothing to tidy. The sloth approves.", file=out)
        return
    width = max(len(m.src.name) for m in moves)
    for m in moves:
        tag = "ai " if m.source == "model" else "ext"
        print(f"  [{tag}] {m.src.name:<{width}}  ->  {m.dst.relative_to(root)}"
              f"   ({m.seconds:.1f}s)", file=out)


def pick_brain(args):
    if args.no_model:
        return HeuristicBrain()
    brain = OllamaBrain(model=args.model, host=args.host)
    if not brain.available():
        print(f"! model {args.model!r} not reachable at {args.host}; "
              "falling back to extension rules", file=sys.stderr)
        return HeuristicBrain()
    return brain


def run_once(root: Path, brain, do_apply: bool, min_age: float, quiet: bool = False) -> int:
    if quiet and not scan(root, min_age=min_age):
        return 0  # watch mode: say nothing when there is nothing to do
    if quiet:
        print(f"[{time.strftime('%H:%M:%S')}] woke up, found something")
    t0 = time.perf_counter()
    moves = None
    if do_apply:
        current = [f.path for f in scan(root, min_age=min_age)]
        moves = load_plan(root, current, brain.model)
        if moves is not None:
            print("Using the plan from your last dry run (no files changed since).")
    if moves is None:
        moves = build_plan(root, brain, min_age=min_age)
    show(root, moves)
    total = time.perf_counter() - t0
    if moves:
        print(f"\n{len(moves)} file(s) planned with {brain.model} in {total:.1f}s.")
    if moves and do_apply:
        log = apply(root, moves)
        forget_plan(root)
        print(f"Moved. Undo log: {log.relative_to(root) if log else '(nothing moved)'}")
    elif moves:
        save_plan(root, moves, brain.model)
        print("Dry run: nothing moved. Add --apply when you like the plan.")
    return len(moves)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "undo":
        p = argparse.ArgumentParser(prog="sloth-tidy undo")
        p.add_argument("folder", type=Path)
        a = p.parse_args(argv[1:])
        restored, skipped, log = undo(a.folder.resolve())
        if log is None:
            print("No undo log found. Nothing to put back.")
            return 1
        print(f"Restored {restored} file(s) from {log.name}" +
              (f", skipped {skipped} that had moved again" if skipped else ""))
        return 0

    p = argparse.ArgumentParser(prog="sloth-tidy", description="Lazy, local folder tidying.")
    p.add_argument("folder", type=Path)
    p.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    p.add_argument("--model", default="gemma3:4b", help="Ollama model tag (default: gemma3:4b; try gemma3:1b on low RAM)")
    p.add_argument("--host", default="http://127.0.0.1:11434", help="Ollama URL, must be localhost")
    p.add_argument("--no-model", action="store_true", help="skip the model, use extension rules")
    p.add_argument("--watch", action="store_true", help="keep running and tidy in the background")
    p.add_argument("--every", type=float, default=300, help="watch: seconds between naps (300)")
    p.add_argument("--min-age", type=float, default=None,
                   help="only touch files untouched for this many seconds "
                        "(default 0, or 600 in watch mode)")
    p.add_argument("--version", action="version", version=f"sloth-tidy {__version__}")
    a = p.parse_args(argv)

    root = a.folder.expanduser().resolve()
    if not root.is_dir():
        p.error(f"{root} is not a folder")
    try:
        brain = pick_brain(a)
    except LocalOnlyError as exc:
        p.error(str(exc))

    min_age = a.min_age if a.min_age is not None else (600 if a.watch else 0)
    if not a.watch:
        run_once(root, brain, a.apply, min_age)
        return 0

    print(f"Watching {root} every {a.every:.0f}s; files must sit still for {min_age:.0f}s. "
          "Ctrl+C to stop.")
    try:
        while True:
            run_once(root, brain, a.apply, min_age, quiet=True)
            time.sleep(a.every)
    except KeyboardInterrupt:
        print("\nBack to sleep.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
