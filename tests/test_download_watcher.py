import os
import time
from datetime import date, datetime
from pathlib import Path

from spotify_queuer.download_watcher import DownloadWatcher, SeenFiles, daily_playlist_name, rewrite_daily_playlist


def _age_file(path: Path, seconds_ago: float) -> None:
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


def test_daily_playlist_name_format():
    assert daily_playlist_name(datetime(2026, 9, 10, 14, 5)) == "recently downloaded 2026-09-10 14-05"


def test_seen_files_mark_if_new_dedupes_unchanged_files(tmp_path):
    seen = SeenFiles(tmp_path / "seen.sqlite3")
    p = tmp_path / "song.mp3"
    p.write_bytes(b"data")
    today = date(2026, 9, 10)

    assert seen.mark_if_new(p, 4, 100.0, today) is True
    assert seen.mark_if_new(p, 4, 100.0, today) is False  # unchanged -- not new
    assert seen.mark_if_new(p, 5, 100.0, today) is True  # size changed -- new again
    seen.close()


def test_rewrite_daily_playlist_writes_m3u_for_todays_files(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    song = downloads / "Title - Artist.mp3"
    song.write_bytes(b"data")

    seen = SeenFiles(tmp_path / "seen.sqlite3")
    today = date(2026, 9, 10)
    seen.mark_if_new(song, song.stat().st_size, song.stat().st_mtime, today)

    m3u_path = rewrite_daily_playlist(seen, tmp_path / "Playlists", today, datetime(2026, 9, 10, 14, 5))

    assert m3u_path.name == "recently downloaded 2026-09-10 14-05.m3u8"
    assert m3u_path.exists()
    assert "Title - Artist" in m3u_path.read_text(encoding="utf-8")
    seen.close()


def test_rewrite_daily_playlist_replaces_the_earlier_timestamped_file(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    song = downloads / "Title - Artist.mp3"
    song.write_bytes(b"data")

    seen = SeenFiles(tmp_path / "seen.sqlite3")
    today = date(2026, 9, 10)
    seen.mark_if_new(song, song.stat().st_size, song.stat().st_mtime, today)

    first_path = rewrite_daily_playlist(seen, tmp_path / "Playlists", today, datetime(2026, 9, 10, 14, 5))
    second_path = rewrite_daily_playlist(seen, tmp_path / "Playlists", today, datetime(2026, 9, 10, 16, 30))

    assert not first_path.exists()  # superseded -- shouldn't linger as a stale duplicate
    assert second_path.exists()
    playlists = sorted(p.name for p in (tmp_path / "Playlists").glob("*.m3u8"))
    assert playlists == ["recently downloaded 2026-09-10 16-30.m3u8"]
    seen.close()


def test_rewrite_daily_playlist_skips_files_that_vanished(tmp_path):
    seen = SeenFiles(tmp_path / "seen.sqlite3")
    today = date(2026, 9, 10)
    ghost = tmp_path / "Downloads" / "gone.mp3"
    seen.mark_if_new(ghost, 1, 1.0, today)  # never actually created on disk

    m3u_path = rewrite_daily_playlist(seen, tmp_path / "Playlists", today, datetime(2026, 9, 10, 14, 5))

    assert m3u_path.read_text(encoding="utf-8").strip() == "#EXTM3U"
    seen.close()


def test_rescan_files_a_stable_new_track(tmp_path):
    watch_dir = tmp_path / "Downloads"
    watch_dir.mkdir()
    song = watch_dir / "Title - Artist.mp3"
    song.write_bytes(b"data")
    _age_file(song, seconds_ago=30)  # older than the stability window -- safe to file

    new_files, errors = [], []
    watcher = DownloadWatcher(
        watch_dir=watch_dir,
        m3u_output_dir=tmp_path / "Playlists",
        state_db_path=tmp_path / "state.sqlite3",
        stable_seconds=5.0,
        on_new_file=new_files.append,
        on_error=errors.append,
    )

    watcher.rescan_now()

    assert new_files == [song]
    assert errors == []
    playlists = list((tmp_path / "Playlists").glob(f"recently downloaded {date.today().isoformat()}*.m3u8"))
    assert len(playlists) == 1
    watcher._seen.close()


def test_rescan_ignores_files_still_being_written(tmp_path):
    watch_dir = tmp_path / "Downloads"
    watch_dir.mkdir()
    song = watch_dir / "Title - Artist.mp3"
    song.write_bytes(b"data")  # fresh mtime -- looks like an in-progress download

    new_files = []
    watcher = DownloadWatcher(
        watch_dir=watch_dir,
        m3u_output_dir=tmp_path / "Playlists",
        state_db_path=tmp_path / "state.sqlite3",
        stable_seconds=5.0,
        on_new_file=new_files.append,
    )

    watcher.rescan_now()

    assert new_files == []
    watcher._seen.close()


def test_rescan_does_not_refile_an_already_seen_stable_file(tmp_path):
    watch_dir = tmp_path / "Downloads"
    watch_dir.mkdir()
    song = watch_dir / "Title - Artist.mp3"
    song.write_bytes(b"data")
    _age_file(song, seconds_ago=30)

    new_files = []
    watcher = DownloadWatcher(
        watch_dir=watch_dir,
        m3u_output_dir=tmp_path / "Playlists",
        state_db_path=tmp_path / "state.sqlite3",
        stable_seconds=5.0,
        on_new_file=new_files.append,
    )

    watcher.rescan_now()
    watcher.rescan_now()

    assert new_files == [song]  # only reported once
    watcher._seen.close()


def test_rescan_reports_error_when_folder_missing(tmp_path):
    errors = []
    watcher = DownloadWatcher(
        watch_dir=tmp_path / "does_not_exist",
        m3u_output_dir=tmp_path / "Playlists",
        state_db_path=tmp_path / "state.sqlite3",
        on_error=errors.append,
    )

    watcher.rescan_now()

    assert len(errors) == 1
    assert "not found" in errors[0]
    watcher._seen.close()
