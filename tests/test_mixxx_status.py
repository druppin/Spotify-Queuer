import sqlite3

from spotify_queuer.db import connect, norm_path
from spotify_queuer.mixxx_status import needs_prep, sync_prep_status, write_needs_prep_playlist
from tests.test_mixxx_migrate import _CUES_SQL, _LIBRARY_SQL, _TRACK_LOCATIONS_SQL


def _mixxx_db(path, tracks):
    """tracks: [(file path, rating, bpm_lock, hotcue count)]"""
    conn = sqlite3.connect(str(path))
    for sql in (_LIBRARY_SQL, _TRACK_LOCATIONS_SQL, _CUES_SQL):
        conn.execute(sql)
    for i, (file_path, rating, bpm_lock, hotcues) in enumerate(tracks, start=1):
        conn.execute("INSERT INTO track_locations (id, location, filename) VALUES (?, ?, ?)",
                     (i, str(file_path).replace("\\", "/"), file_path.name))  # Mixxx stores forward slashes
        conn.execute("INSERT INTO library (id, location, rating, bpm_lock) VALUES (?, ?, ?, ?)",
                     (i, i, rating, bpm_lock))
        for n in range(hotcues):
            conn.execute("INSERT INTO cues (track_id, hotcue) VALUES (?, ?)", (i, n))
        conn.execute("INSERT INTO cues (track_id, hotcue) VALUES (?, -1)", (i,))  # main cue: not a hot cue
    conn.commit()
    conn.close()


def _app_db(tmp_path, files):
    """files: [(name, needs_prep)] -- created on disk and registered."""
    db = tmp_path / "app.sqlite3"
    conn = connect(db)
    paths = {}
    for i, (name, flag) in enumerate(files):
        path = tmp_path / "Music" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"x")
        conn.execute("INSERT INTO library_files (path, first_seen_at, needs_prep) VALUES (?, ?, ?)",
                     (norm_path(path), i, flag))
        paths[name] = path
    conn.commit()
    conn.close()
    return db, paths


def test_needs_prep_lists_new_downloads_missing_any_requirement(tmp_path):
    db, p = _app_db(tmp_path, [
        ("Ready - A.mp3", 1), ("No Cues - B.mp3", 1), ("Unrated - C.mp3", 1),
        ("Unlocked - D.mp3", 1), ("Not Imported - E.mp3", 1), ("Old Track - F.mp3", 0),
    ])
    _mixxx_db(tmp_path / "mixxxdb.sqlite", [
        (p["Ready - A.mp3"], 5, 1, 2),
        (p["No Cues - B.mp3"], 4, 1, 0),
        (p["Unrated - C.mp3"], 0, 1, 1),
        (p["Unlocked - D.mp3"], 3, 0, 1),
        (p["Old Track - F.mp3"], 0, 0, 0),  # unprepped, but predates tracking
    ])

    sync_prep_status(db, tmp_path / "mixxxdb.sqlite")

    assert [(path.name, missing) for path, missing in needs_prep(db)] == [
        ("No Cues - B.mp3", ["no hot cues"]),
        ("Unrated - C.mp3", ["unrated"]),
        ("Unlocked - D.mp3", ["BPM not locked"]),
        ("Not Imported - E.mp3", ["not in Mixxx yet"]),
    ]


def test_write_needs_prep_playlist_labels_what_is_missing(tmp_path):
    db, p = _app_db(tmp_path, [("Song - Artist.mp3", 1)])
    _mixxx_db(tmp_path / "mixxxdb.sqlite", [(p["Song - Artist.mp3"], 0, 0, 0)])
    sync_prep_status(db, tmp_path / "mixxxdb.sqlite")

    assert write_needs_prep_playlist(db, tmp_path / "Playlists") == 1
    text = (tmp_path / "Playlists" / "Needs prep.m3u8").read_text(encoding="utf-8")
    assert "Song [no hot cues, unrated, BPM not locked]" in text


def test_mixxx_paths_from_another_machine_match_by_path_below_the_music_folder(tmp_path):
    db, p = _app_db(tmp_path, [("Song - Artist.mp3", 1)])
    linux_path = tmp_path / "linux" / "Music" / "Song - Artist.mp3"  # stands in for /run/media/<user>/<stick>/Music/...
    _mixxx_db(tmp_path / "mixxxdb.sqlite", [(linux_path, 5, 1, 1)])

    sync_prep_status(db, tmp_path / "mixxxdb.sqlite", music_root=tmp_path / "Music")

    assert needs_prep(db) == []  # matched and set-ready
