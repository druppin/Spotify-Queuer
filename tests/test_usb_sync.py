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


def test_compare_trees_reports_all_three_kinds_of_difference(tmp_path):
    from spotify_queuer.usb_sync import compare_trees

    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    (source / "new.mp3").write_bytes(b"new")
    (source / "truncated.mp3").write_bytes(b"x" * 1000)
    (dest / "truncated.mp3").write_bytes(b"x" * 10)
    (dest / "only_dest.mp3").write_bytes(b"old")
    (source / "same.mp3").write_bytes(b"same")
    (dest / "same.mp3").write_bytes(b"same")
    st = (source / "same.mp3").stat()
    os.utime(dest / "same.mp3", (st.st_atime, st.st_mtime))

    diffs = {str(d.relative_path): d for d in compare_trees(source, dest)}

    assert set(diffs) == {"new.mp3", "truncated.mp3", "only_dest.mp3"}
    assert diffs["new.mp3"].status == "new"
    assert diffs["only_dest.mp3"].status == "dest_only"
    truncated = diffs["truncated.mp3"]
    assert truncated.status == "changed"
    assert truncated.size_delta == -990
    assert "Source is larger" in truncated.note


def test_plan_sync_never_plans_dest_only_files(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    (dest / "only_dest.mp3").write_bytes(b"old")

    assert plan_sync(source, dest) == []


def test_write_diff_csv(tmp_path):
    import csv

    from spotify_queuer.usb_sync import compare_trees, write_diff_csv

    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    (source / "a.mp3").write_bytes(b"abc")

    out = tmp_path / "diff.csv"
    write_diff_csv(compare_trees(source, dest), out)

    with open(out, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["status"] == "new"
    assert rows[0]["path"] == "a.mp3"
    assert rows[0]["source_size"] == "3"


def test_plan_sync_treats_whole_hour_mtime_shift_as_unchanged(tmp_path):
    # FAT/exFAT sticks written under another time zone or DST offset.
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    (source / "song.mp3").write_bytes(b"hello")
    (dest / "song.mp3").write_bytes(b"hello")
    st = (source / "song.mp3").stat()
    os.utime(dest / "song.mp3", (st.st_atime, st.st_mtime + 3600))
    assert plan_sync(source, dest) == []

    os.utime(dest / "song.mp3", (st.st_atime, st.st_mtime + 1800))  # not a whole-hour shift
    assert len(plan_sync(source, dest)) == 1
