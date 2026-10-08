import os
from pathlib import Path

from spotify_queuer.usb_sync import apply_sync, plan_sync


def test_plan_sync_detects_new_file(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    (source / "Artist").mkdir(parents=True)
    (source / "Artist" / "song.mp3").write_bytes(b"hello")
    dest.mkdir()

    actions = plan_sync(source, dest)

    assert len(actions) == 1
    assert actions[0].reason == "new"
    assert actions[0].relative_path == Path("Artist") / "song.mp3"


def test_plan_sync_skips_identical_files(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    (source / "song.mp3").write_bytes(b"hello")
    (dest / "song.mp3").write_bytes(b"hello")
    st = (source / "song.mp3").stat()
    os.utime(dest / "song.mp3", (st.st_atime, st.st_mtime))

    assert plan_sync(source, dest) == []


def test_plan_sync_detects_changed_size(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    (source / "song.mp3").write_bytes(b"hello world")
    (dest / "song.mp3").write_bytes(b"hi")

    actions = plan_sync(source, dest)
    assert len(actions) == 1
    assert actions[0].reason == "changed"


def test_apply_sync_copies_files_and_never_deletes(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    (source / "new.mp3").write_bytes(b"new")
    (dest / "old.mp3").write_bytes(b"old")  # absent from source -- must survive

    copied = apply_sync(plan_sync(source, dest))

    assert copied == 1
    assert (dest / "new.mp3").read_bytes() == b"new"
    assert (dest / "old.mp3").exists()


def test_apply_sync_creates_nested_directories(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    (source / "A" / "B").mkdir(parents=True)
    (source / "A" / "B" / "song.mp3").write_bytes(b"x")
    dest.mkdir()

    apply_sync(plan_sync(source, dest))

    assert (dest / "A" / "B" / "song.mp3").exists()


def test_apply_sync_calls_progress_callback(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    (source / "a.mp3").write_bytes(b"a")
    (source / "b.mp3").write_bytes(b"b")

    seen = []
    apply_sync(plan_sync(source, dest), progress_cb=seen.append)

    assert len(seen) == 2


def test_apply_sync_calls_chunk_callback_with_running_byte_count(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    content = b"y" * (5 * 1024 * 1024 + 37)  # spans more than one copy chunk
    (source / "big.bin").write_bytes(content)

    seen = []
    apply_sync(plan_sync(source, dest), chunk_cb=lambda action, copied: seen.append(copied))

    assert seen[0] == 0  # reported before any bytes are copied
    assert seen[-1] == len(content)
    assert seen == sorted(seen)
    assert (dest / "big.bin").read_bytes() == content
