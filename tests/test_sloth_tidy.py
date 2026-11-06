import json
import os
import time
from pathlib import Path

import pytest

from sloth_tidy import brain as brain_mod
from sloth_tidy.brain import (HeuristicBrain, LocalOnlyError, OllamaBrain, Suggestion,
                              ensure_local, parse_answer)
from sloth_tidy.cli import build_plan, main
from sloth_tidy.mover import UndoError, apply, undo
from sloth_tidy.planner import Move, slugify, target_for
from sloth_tidy.scanner import FileInfo, scan


class FakeBrain:
    """Stands in for the model: returns canned answers keyed by filename."""
    model = "fake"

    def __init__(self, answers):
        self.answers = answers
        self.seen = []

    def suggest(self, info):
        self.seen.append(info)
        cat, stem = self.answers.get(info.path.name, ("Misc", None))
        return Suggestion(cat, stem, "model")


@pytest.fixture
def messy(tmp_path):
    (tmp_path / "IMG_2041.JPG").write_bytes(b"\xff\xd8fakejpeg")
    (tmp_path / "doc (3).txt").write_text("Invoice #4471 from Hetzner, total EUR 4.51")
    (tmp_path / "untitled.py").write_text("import socket\n# port scanner\n")
    (tmp_path / ".hidden").write_text("leave me")
    (tmp_path / "big.iso.crdownload").write_text("half a file")
    (tmp_path / "AlreadySorted").mkdir()
    return tmp_path


def test_scan_skips_hidden_partial_and_dirs(messy):
    names = [f.path.name for f in scan(messy)]
    assert names == ["IMG_2041.JPG", "doc (3).txt", "untitled.py"]


def test_scan_min_age_leaves_fresh_files(messy):
    assert scan(messy, min_age=3600) == []
    assert len(scan(messy, min_age=3600, now=time.time() + 7200)) == 3


def test_snippet_reads_text(messy):
    info = {f.path.name: f for f in scan(messy)}
    assert "Invoice #4471" in info["doc (3).txt"].snippet
    assert info["IMG_2041.JPG"].snippet == ""


def test_slugify_cleans_model_output():
    assert slugify("Hetzner Invoice: Oct 2026!.pdf") == "hetzner-invoice-oct-2026"
    assert slugify("../../etc/passwd") == "etc-passwd"
    assert slugify("x" * 200) == "x" * 60
    assert slugify("one two three four five six seven eight") == "one-two-three-four-five-six"
    assert slugify("aaaaaaaaaa " * 5 + "bbbbbbbbbbbb") == "-".join(["aaaaaaaaaa"] * 5)


def test_dry_run_moves_nothing(messy, capsys, monkeypatch):
    before = sorted(p.name for p in messy.iterdir())
    monkeypatch.setattr(brain_mod.OllamaBrain, "available", lambda self: False)
    main([str(messy)])
    after = sorted(p.name for p in messy.iterdir() if p.name != ".sloth-tidy")
    assert after == before  # only the hidden plan file is new
    assert "Dry run" in capsys.readouterr().out


def test_plan_apply_and_undo_roundtrip(messy):
    fake = FakeBrain({
        "doc (3).txt": ("Receipts", "Hetzner Invoice 4471"),
        "untitled.py": ("Code", "port-scanner"),
        "IMG_2041.JPG": ("Images", None),
    })
    moves = build_plan(messy, fake)
    targets = {m.src.name: str(m.dst.relative_to(messy)) for m in moves}
    assert targets == {
        "doc (3).txt": "Receipts/hetzner-invoice-4471.txt",
        "untitled.py": "Code/port-scanner.py",
        "IMG_2041.JPG": "Images/IMG_2041.jpg",
    }
    log = apply(messy, moves)
    assert (messy / "Receipts/hetzner-invoice-4471.txt").read_text().startswith("Invoice")
    assert not (messy / "doc (3).txt").exists()
    assert json.loads(log.read_text())["moves"]

    restored, skipped, _ = undo(messy)
    assert (restored, skipped) == (3, 0)
    assert (messy / "doc (3).txt").exists()
    assert not (messy / "Receipts").exists()  # empty category folders are cleaned up


def test_never_overwrites_existing_or_planned(tmp_path):
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "todo.md").write_text("old")
    (tmp_path / "a.md").write_text("one")
    (tmp_path / "b.md").write_text("two")
    fake = FakeBrain({"a.md": ("Notes", "todo"), "b.md": ("Notes", "todo")})
    moves = build_plan(tmp_path, fake)
    assert [m.dst.name for m in moves] == ["todo-2.md", "todo-3.md"]
    apply(tmp_path, moves)
    assert (tmp_path / "Notes" / "todo.md").read_text() == "old"


def test_apply_skips_if_destination_appears_after_planning(tmp_path):
    (tmp_path / "a.txt").write_text("mine")
    moves = build_plan(tmp_path, FakeBrain({"a.txt": ("Notes", "x")}))
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "x.txt").write_text("someone else's")
    assert apply(tmp_path, moves) is None
    assert (tmp_path / "a.txt").read_text() == "mine"
    assert (tmp_path / "Notes" / "x.txt").read_text() == "someone else's"


def test_apply_keeps_undo_log_when_category_dir_is_blocked(tmp_path):
    # "Images" is a file, so creating Images/ fails. That must not discard the
    # undo log for a.txt, which was already renamed, or skip c.txt afterwards.
    (tmp_path / "a.txt").write_text("aaa")
    (tmp_path / "b.txt").write_text("bbb")
    (tmp_path / "c.txt").write_text("ccc")
    (tmp_path / "Images").write_text("not a directory")
    moves = [
        Move(tmp_path / "a.txt", tmp_path / "Notes" / "a.txt", "fallback"),
        Move(tmp_path / "b.txt", tmp_path / "Images" / "b.txt", "fallback"),
        Move(tmp_path / "c.txt", tmp_path / "Code" / "c.txt", "fallback"),
    ]
    log = apply(tmp_path, moves)
    assert log is not None
    assert [m["from"] for m in json.loads(log.read_text())["moves"]] == ["a.txt", "c.txt"]
    assert (tmp_path / "Notes" / "a.txt").read_text() == "aaa"
    assert (tmp_path / "Code" / "c.txt").read_text() == "ccc"
    assert (tmp_path / "b.txt").read_text() == "bbb"
    assert (tmp_path / "Images").is_file() and (tmp_path / "Images").read_text() == "not a directory"
    restored, skipped, _ = undo(tmp_path)
    assert (restored, skipped) == (2, 0)
    assert (tmp_path / "a.txt").read_text() == "aaa"
    assert (tmp_path / "c.txt").read_text() == "ccc"


def test_apply_skips_files_deleted_after_planning_and_keeps_undo_log(tmp_path):
    for n in ("a", "b", "c"):
        (tmp_path / f"{n}.txt").write_text(n)
    moves = build_plan(tmp_path, FakeBrain({"a.txt": ("Notes", "a"), "b.txt": ("Notes", "b"),
                                            "c.txt": ("Notes", "c")}))
    (tmp_path / "b.txt").unlink()  # user deletes a file while the model is still thinking
    log = apply(tmp_path, moves)
    assert log is not None
    assert [m["from"] for m in json.loads(log.read_text())["moves"]] == ["a.txt", "c.txt"]
    restored, skipped, _ = undo(tmp_path)
    assert (restored, skipped) == (2, 0)
    assert (tmp_path / "a.txt").exists() and (tmp_path / "c.txt").exists()


def test_undo_skips_files_moved_again(messy):
    moves = build_plan(messy, FakeBrain({"untitled.py": ("Code", "scanner")}))
    apply(messy, moves)
    (messy / "Code" / "scanner.py").rename(messy / "elsewhere.py")
    restored, skipped, _ = undo(messy)
    assert skipped == 1 and restored == 2


def test_parse_answer_rejects_invented_category():
    info = FileInfo(Path("x.png"), 1, 0, "")
    s = parse_answer({"category": "Memes", "name": "cat"}, info)
    assert s.category == "Images"  # falls back to the extension's category
    assert parse_answer("garbage", info).source == "fallback"


def test_heuristic_brain():
    assert HeuristicBrain().suggest(FileInfo(Path("a.zip"), 1, 0, "")).category == "Archives"
    assert HeuristicBrain().suggest(FileInfo(Path("a.weird"), 1, 0, "")).category == "Misc"


@pytest.mark.parametrize("url", ["http://example.com:11434", "http://8.8.8.8:11434", "ftp://localhost"])
def test_refuses_non_local_hosts(url):
    with pytest.raises(LocalOnlyError):
        ensure_local(url)


def test_accepts_localhost():
    assert ensure_local("http://localhost:11434/") == "http://localhost:11434"


def test_ollama_brain_uses_chat_api_and_parses(monkeypatch):
    calls = {}

    def fake_post(self, path, payload):
        calls["path"], calls["payload"] = path, payload
        return {"message": {"content": json.dumps({"category": "receipts", "name": "aws bill"})}}

    monkeypatch.setattr(OllamaBrain, "_post", fake_post)
    b = OllamaBrain()
    s = b.suggest(FileInfo(Path("x.pdf"), 10, 0, "Amazon Web Services invoice"))
    assert (s.category, s.stem, s.source) == ("Receipts", "aws bill", "model")
    assert calls["path"] == "/api/chat"
    assert calls["payload"]["format"]["properties"]["category"]["enum"]
    assert "Amazon Web Services" in calls["payload"]["messages"][1]["content"]


def test_ollama_brain_falls_back_when_server_errors(monkeypatch):
    def boom(self, path, payload):
        raise OSError("connection refused")

    monkeypatch.setattr(OllamaBrain, "_post", boom)
    s = OllamaBrain().suggest(FileInfo(Path("x.mp3"), 10, 0, ""))
    assert (s.category, s.source) == ("Audio", "fallback")


def test_ollama_brain_does_not_guess_names_for_unreadable_files(monkeypatch):
    def must_not_call(self, path, payload):
        raise AssertionError("model should not be asked about a file it cannot read")

    monkeypatch.setattr(OllamaBrain, "_post", must_not_call)
    s = OllamaBrain().suggest(FileInfo(Path("IMG_0001.jpg"), 10, 0, ""))
    assert (s.category, s.stem, s.source) == ("Images", None, "fallback")


class CountingBrain(FakeBrain):
    calls = 0

    def suggest(self, info):
        CountingBrain.calls += 1
        return Suggestion("Notes", f"take {CountingBrain.calls}", "model")


def test_apply_reuses_dry_run_plan(tmp_path, monkeypatch):
    from sloth_tidy import cli
    (tmp_path / "a.txt").write_text("hello")
    CountingBrain.calls = 0
    monkeypatch.setattr(cli, "pick_brain", lambda args: CountingBrain({}))
    cli.main([str(tmp_path)])                 # dry run asks the model once
    cli.main([str(tmp_path), "--apply"])      # apply must not ask again
    assert CountingBrain.calls == 1
    assert (tmp_path / "Notes" / "take-1.txt").exists()


def test_stale_plan_is_ignored_when_file_changes(tmp_path, monkeypatch):
    from sloth_tidy import cli
    f = tmp_path / "a.txt"
    f.write_text("hello")
    CountingBrain.calls = 0
    monkeypatch.setattr(cli, "pick_brain", lambda args: CountingBrain({}))
    cli.main([str(tmp_path)])
    f.write_text("hello, but edited and longer")
    cli.main([str(tmp_path), "--apply"])
    assert CountingBrain.calls == 2
    assert (tmp_path / "Notes" / "take-2.txt").exists()


class VanishingBrain(CountingBrain):
    """Deletes a.txt while 'thinking' about b.txt, like a user cleaning up mid-run."""

    def suggest(self, info):
        if info.path.name == "b.txt":
            (info.path.parent / "a.txt").unlink()
        return super().suggest(info)


def test_dry_run_survives_file_deleted_while_planning(tmp_path, monkeypatch, capsys):
    from sloth_tidy import cli
    (tmp_path / "a.txt").write_text("first")
    (tmp_path / "b.txt").write_text("second")
    CountingBrain.calls = 0
    monkeypatch.setattr(cli, "pick_brain", lambda args: VanishingBrain({}))
    cli.main([str(tmp_path)])                 # used to crash with FileNotFoundError
    assert "Dry run: nothing moved" in capsys.readouterr().out
    monkeypatch.setattr(cli, "pick_brain", lambda args: CountingBrain({}))
    cli.main([str(tmp_path), "--apply"])      # the saved plan still matches the folder
    assert CountingBrain.calls == 2           # so apply reused it instead of re-asking
    assert (tmp_path / "Notes" / "take-2.txt").read_text() == "second"


def test_scan_skips_file_that_vanishes_mid_scan(tmp_path, monkeypatch):
    # A browser finishing a download renames its partial file between listing and stat.
    from sloth_tidy import scanner
    (tmp_path / "keep.txt").write_text("hello")
    gone = tmp_path / "gone.txt"
    gone.write_text("bye")
    real = scanner.is_candidate

    def vanish_after_check(p):
        ok = real(p)
        if p == gone:
            p.unlink()
        return ok

    monkeypatch.setattr(scanner, "is_candidate", vanish_after_check)
    found = scanner.scan(tmp_path)
    assert [f.path.name for f in found] == ["keep.txt"]


def test_undo_refuses_corrupt_log(tmp_path):
    # A half-written or hand-edited undo log used to raise JSONDecodeError.
    state = tmp_path / ".sloth-tidy"
    state.mkdir()
    bad = state / "undo-20260101-000000-000001.json"
    bad.write_text("not json at all{{{")
    with pytest.raises(UndoError, match="unreadable"):
        undo(tmp_path)
    assert bad.is_file()  # left in place so it can be inspected


def test_undo_refuses_log_missing_moves(tmp_path):
    state = tmp_path / ".sloth-tidy"
    state.mkdir()
    bad = state / "undo-20260101-000000-000002.json"
    bad.write_text(json.dumps({"created": 1}))  # no "moves" key
    with pytest.raises(UndoError, match="unreadable"):
        undo(tmp_path)
    assert bad.is_file()


def test_undo_refuses_path_escaping_log(tmp_path):
    # A tampered log with ../ must not rename files onto the parent directory.
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "x.txt").write_text("inside")
    state = tmp_path / ".sloth-tidy"
    state.mkdir()
    bad = state / "undo-20260101-000000-000003.json"
    bad.write_text(json.dumps({
        "created": 1,
        "moves": [{"from": "../escape-out.txt", "to": "Notes/x.txt"}],
    }))
    with pytest.raises(UndoError, match="outside"):
        undo(tmp_path)
    assert (tmp_path / "Notes" / "x.txt").read_text() == "inside"
    assert not (tmp_path.parent / "escape-out.txt").exists()
    assert bad.is_file()


def test_undo_cli_reports_corrupt_log(tmp_path, capsys):
    state = tmp_path / ".sloth-tidy"
    state.mkdir()
    (state / "undo-20260101-000000-000004.json").write_text("{")
    assert main(["undo", str(tmp_path)]) == 1
    assert "Can't undo" in capsys.readouterr().out


def test_cli_rejects_non_positive_every(tmp_path):
    # --every 0 would busy-loop; a negative value makes time.sleep raise.
    with pytest.raises(SystemExit):
        main([str(tmp_path), "--watch", "--every", "0", "--no-model"])
    with pytest.raises(SystemExit):
        main([str(tmp_path), "--watch", "--every", "-1", "--no-model"])


def test_cli_rejects_negative_min_age(tmp_path):
    with pytest.raises(SystemExit):
        main([str(tmp_path), "--min-age", "-5", "--no-model"])


def test_symlinked_category_is_not_followed(tmp_path):
    # rename follows a directory symlink. Filing into Notes here used to drop
    # the file outside the tidy folder, after which undo refused to bring it back.
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    try:
        (tmp_path / "a.txt").write_text("secret")
        (tmp_path / "Notes").symlink_to(outside)
        moves = build_plan(tmp_path, FakeBrain({"a.txt": ("Notes", "secret note")}))
        assert moves[0].dst.relative_to(tmp_path) == Path("Notes-2/secret-note.txt")
        log = apply(tmp_path, moves)
        assert log is not None
        assert (tmp_path / "Notes-2" / "secret-note.txt").read_text() == "secret"
        assert list(outside.iterdir()) == []
        assert (tmp_path / "Notes").is_symlink()
        restored, skipped, _ = undo(tmp_path)
        assert (restored, skipped) == (1, 0)
        assert (tmp_path / "a.txt").read_text() == "secret"
        assert (tmp_path / "Notes").is_symlink()
        assert list(outside.iterdir()) == []
    finally:
        for child in outside.iterdir():
            child.unlink()
        outside.rmdir()


def test_category_name_occupied_by_a_file_uses_a_real_directory(tmp_path):
    (tmp_path / "a.txt").write_text("aaa")
    (tmp_path / "Notes").write_text("leave me")
    moves = build_plan(tmp_path, FakeBrain({"a.txt": ("Notes", "a"), "Notes": ("Misc", None)}))
    by_src = {m.src.name: m.dst.relative_to(tmp_path) for m in moves}
    assert by_src == {
        "a.txt": Path("Notes-2/a.txt"),
        "Notes": Path("Misc/Notes"),
    }
    apply(tmp_path, moves)
    assert (tmp_path / "Notes-2" / "a.txt").read_text() == "aaa"
    assert (tmp_path / "Misc" / "Notes").read_text() == "leave me"


def test_apply_skips_moves_that_leave_the_folder(tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    escaped = tmp_path.parent / f"escaped-{tmp_path.name}.txt"
    try:
        (tmp_path / "a.txt").write_text("secret")
        (tmp_path / "b.txt").write_text("dotdot")
        (tmp_path / "c.txt").write_text("ok")
        (tmp_path / "Notes").symlink_to(outside)
        moves = [
            Move(tmp_path / "a.txt", tmp_path / "Notes" / "a.txt", "fallback"),
            Move(tmp_path / "b.txt", tmp_path / ".." / escaped.name, "fallback"),
            Move(tmp_path / "c.txt", tmp_path / "Code" / "c.txt", "fallback"),
        ]
        log = apply(tmp_path, moves)
        assert log is not None
        assert [m["from"] for m in json.loads(log.read_text())["moves"]] == ["c.txt"]
        assert (tmp_path / "a.txt").read_text() == "secret"
        assert (tmp_path / "b.txt").read_text() == "dotdot"
        assert (tmp_path / "Code" / "c.txt").read_text() == "ok"
        assert list(outside.iterdir()) == []
        assert not escaped.exists()
    finally:
        for child in outside.iterdir():
            child.unlink()
        outside.rmdir()
        escaped.unlink(missing_ok=True)


def test_plan_that_points_outside_is_rebuilt(tmp_path, capsys):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    st = f.stat()
    outside = tmp_path.parent / f"escaped-{tmp_path.name}.txt"
    state = tmp_path / ".sloth-tidy"
    state.mkdir()
    (state / "plan.json").write_text(json.dumps({
        "model": "extension rules",
        "moves": [{
            "from": "a.txt",
            "to": f"../{outside.name}",
            "source": "fallback",
            "seconds": 0.0,
            "fp": [st.st_size, st.st_mtime_ns],
        }],
    }))
    assert main([str(tmp_path), "--apply", "--no-model"]) == 0
    out = capsys.readouterr().out
    assert "Using the plan from your last dry run" not in out
    assert not outside.exists()
    assert (tmp_path / "Notes" / "a.txt").read_text() == "hello"


@pytest.mark.parametrize("payload", [
    "[]",
    "null",
    '{"model": "extension rules", "moves": null}',
    '{"model": "extension rules", "moves": ["a.txt"]}',
])
def test_malformed_plan_is_rebuilt(tmp_path, payload):
    # A plan that is still valid JSON but not the shape we write used to raise
    # AttributeError / TypeError on --apply instead of being planned again.
    (tmp_path / "a.txt").write_text("hello")
    state = tmp_path / ".sloth-tidy"
    state.mkdir()
    (state / "plan.json").write_text(payload)
    assert main([str(tmp_path), "--apply", "--no-model"]) == 0
    assert (tmp_path / "Notes" / "a.txt").read_text() == "hello"


def test_undo_refuses_symlink_loop_without_crashing(tmp_path):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    state = tmp_path / ".sloth-tidy"
    state.mkdir()
    bad = state / "undo-20260101-000000-000099.json"
    bad.write_text(json.dumps({"moves": [{"from": "a.txt", "to": "loop/a.txt"}]}))
    with pytest.raises(UndoError, match="outside"):
        undo(tmp_path)
    assert bad.is_file()
