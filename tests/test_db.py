import sqlite3

from spotify_queuer.db import connect, ensure_migrated, norm_path


def test_connect_creates_schema_idempotently(tmp_path):
    connect(tmp_path / "app.sqlite3").close()
    conn = connect(tmp_path / "app.sqlite3")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"playlists", "tracks", "playlist_tracks", "library_files", "track_files", "queue_events",
            "mixxx_status"} <= tables


def test_deleting_a_file_cascades_its_track_link(tmp_path):
    conn = connect(tmp_path / "app.sqlite3")
    conn.execute("INSERT INTO tracks VALUES ('a', 'spotify:track:a', 'Song', 'Artist')")
    conn.execute("INSERT INTO library_files (path, first_seen_at) VALUES ('x.mp3', 0)")
    conn.execute("INSERT INTO track_files VALUES ('a', 'x.mp3', 'match', 90, 0)")
    conn.execute("DELETE FROM library_files WHERE path = 'x.mp3'")
    assert conn.execute("SELECT COUNT(*) FROM track_files").fetchone()[0] == 0


def _legacy_db(path, sql, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(sql)
    table = sql.split()[2]
    conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)
    conn.commit()
    conn.close()


def test_ensure_migrated_imports_legacy_databases_once(tmp_path):
    lib_path = norm_path(tmp_path / "Music" / "A" / "B" / "Song - Artist.mp3")
    dl_path = norm_path(tmp_path / "Music" / "Genres" / "New - Someone.mp3")
    _legacy_db(
        tmp_path / "library_index.sqlite3",
        "CREATE TABLE library_tracks (path TEXT, size INT, mtime REAL, artist TEXT, album TEXT, title TEXT,"
        " artists TEXT, tag_title TEXT, tag_artists TEXT)",
        [(lib_path.replace("\\", "/"), 10, 1.0, "A", "B", "Song", "Artist", None, None)],
    )
    _legacy_db(
        tmp_path / "download_watcher_state.sqlite3",
        "CREATE TABLE seen_files (path TEXT, size INT, mtime REAL, first_seen_date TEXT)",
        [(lib_path, 10, 1.0, "2026-10-20"), (dl_path, 20, 2.0, "2026-10-21")],
    )
    _legacy_db(
        tmp_path / "queue_history.sqlite3",
        "CREATE TABLE queued_tracks (uri TEXT, title TEXT, artists TEXT, playlist_id TEXT, playlist_name TEXT,"
        " queued_at REAL, downloaded_at REAL, download_path TEXT)",
        [("spotify:track:x", "New", "Someone", "p1", "Friday Set", 5.0, 6.0, dl_path),
         ("spotify:track:y", "Gone", "Nobody", "p1", "Friday Set", 5.0, None, None)],
    )

    db = tmp_path / "app.sqlite3"
    ensure_migrated(db, tmp_path)
    ensure_migrated(db, tmp_path)  # second call is a no-op

    conn = connect(db)
    files = dict(conn.execute("SELECT path, in_library FROM library_files").fetchall())
    assert files == {lib_path: 1, dl_path: 0}  # the library row and the seen row for one file merged
    assert conn.execute("SELECT COUNT(*) FROM queue_events").fetchone()[0] == 2
    assert conn.execute("SELECT track_id, path, source FROM track_files").fetchall() == [("x", dl_path, "download")]
    assert conn.execute("SELECT name, snapshot_id FROM playlists").fetchall() == [("Friday Set", None)]
