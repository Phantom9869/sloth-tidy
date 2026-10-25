import json
import os
import time
from pathlib import Path

import pytest

from sloth_tidy import brain as brain_mod
from sloth_tidy.brain import (HeuristicBrain, LocalOnlyError, OllamaBrain, Suggestion,
                              ensure_local, parse_answer)
from sloth_tidy.cli import build_plan, main
from sloth_tidy.mover import apply, undo
from sloth_tidy.planner import slugify, target_for
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
