import sqlite3
from pathlib import Path

from spotify_queuer.mixxx_migrate import TrackMatch, match_tracks, migrate

# Minimal subset of Mixxx's real res/schema.xml -- just the columns this
# module reads or writes -- so migrate() runs against something schema-shaped
# without needing a real Mixxx install to generate a fixture db.
_LIBRARY_SQL = """
CREATE TABLE library (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  duration FLOAT,
  location INTEGER,
  mixxx_deleted INTEGER DEFAULT 0,
  rating INTEGER DEFAULT 0,
  color INTEGER,
  comment varchar(256) DEFAULT '',
  key varchar(8) DEFAULT '',
  key_id INTEGER DEFAULT 0,
  keys BLOB,
  keys_version TEXT,
  keys_sub_version TEXT,
  bpm FLOAT DEFAULT 0,
  bpm_lock INTEGER DEFAULT 0,
  beats BLOB,
  beats_version TEXT,
  beats_sub_version TEXT DEFAULT '',
  timesplayed INTEGER DEFAULT 0,
  played INTEGER DEFAULT 0,
  last_played_at DATETIME,
  replaygain FLOAT DEFAULT 0,
  replaygain_peak REAL DEFAULT -1.0,
  grouping TEXT DEFAULT '',
  composer varchar(64) DEFAULT '',
  album_artist TEXT DEFAULT '',
  genre varchar(64) DEFAULT '',
  year varchar(16) DEFAULT '',
  tracknumber varchar(3) DEFAULT '',
  tuning_frequency_hz FLOAT DEFAULT 0.0
)
"""
_TRACK_LOCATIONS_SQL = """
CREATE TABLE track_locations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  location varchar(512) UNIQUE,
  filename varchar(512)
)
"""
_CUES_SQL = """
CREATE TABLE cues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id INTEGER NOT NULL,
  type INTEGER DEFAULT 0,
  position INTEGER DEFAULT -1,
  length INTEGER DEFAULT 0,
  hotcue INTEGER DEFAULT -1,
  label TEXT DEFAULT '',
  color INTEGER DEFAULT 0
)
"""
_CRATES_SQL = """
CREATE TABLE crates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name varchar(48) UNIQUE NOT NULL,
  count INTEGER DEFAULT 0,
  show INTEGER DEFAULT 1,
  locked INTEGER DEFAULT 0,
  autodj_source INTEGER DEFAULT 0
)
"""
_CRATE_TRACKS_SQL = """
CREATE TABLE crate_tracks (
  crate_id INTEGER NOT NULL,
  track_id INTEGER NOT NULL,
  UNIQUE (crate_id, track_id)
)
"""


def make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    for stmt in (_LIBRARY_SQL, _TRACK_LOCATIONS_SQL, _CUES_SQL, _CRATES_SQL, _CRATE_TRACKS_SQL):
        conn.execute(stmt)
    conn.commit()
    return conn


def add_track(conn: sqlite3.Connection, filename: str, duration: float, **fields) -> int:
    cur = conn.execute(
        "INSERT INTO track_locations (location, filename) VALUES (?, ?)", (f"/x/{filename}", filename)
    )
    loc_id = cur.lastrowid
    columns = ["location", "duration", *fields.keys()]
    values = [loc_id, duration, *fields.values()]
    cur = conn.execute(
        f"INSERT INTO library ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values
    )
    conn.commit()
    return cur.lastrowid


def test_match_tracks_single_candidate_matches_regardless_of_duration():
    matches, unmatched = match_tracks([(1, "song.mp3", 200.0)], [(10, "song.mp3", 999.0)])
    assert matches == [TrackMatch(1, 10, "song.mp3")]
    assert unmatched == []


def test_match_tracks_no_candidate_is_unmatched():
    matches, unmatched = match_tracks([(1, "song.mp3", 200.0)], [])
    assert matches == []
    assert unmatched == ["song.mp3"]


def test_match_tracks_disambiguates_duplicates_by_duration():
    source = [(1, "song.mp3", 200.0)]
    dest = [(10, "song.mp3", 200.3), (11, "song.mp3", 340.0)]
    matches, unmatched = match_tracks(source, dest)
    assert matches == [TrackMatch(1, 10, "song.mp3")]
    assert unmatched == []


def test_match_tracks_leaves_ambiguous_duplicates_unmatched():
    source = [(1, "song.mp3", 200.0)]
    dest = [(10, "song.mp3", 200.1), (11, "song.mp3", 200.2)]  # both within tolerance
    matches, unmatched = match_tracks(source, dest)
    assert matches == []
    assert unmatched == ["song.mp3"]


def test_migrate_copies_metadata_cues_and_crates(tmp_path):
    source_path, dest_path = tmp_path / "source.sqlite", tmp_path / "dest.sqlite"

    src_conn = make_db(source_path)
    src_track_id = add_track(src_conn, "song.mp3", 200.0, rating=5, comment="great track", bpm=128.0)
    src_conn.execute(
        "INSERT INTO cues (track_id, type, position, hotcue, label) VALUES (?, 1, 5000, 0, 'Drop')",
        (src_track_id,),
    )
    src_conn.execute("INSERT INTO crates (name) VALUES ('House')")
    src_conn.execute("INSERT INTO crate_tracks (crate_id, track_id) VALUES (1, ?)", (src_track_id,))
    src_conn.commit()
    src_conn.close()

    dest_conn = make_db(dest_path)
    dest_track_id = add_track(dest_conn, "song.mp3", 200.2, rating=0, comment="")
    dest_conn.close()

    report = migrate(source_path, dest_path)

    assert report.matched_count == 1
    assert report.unmatched_source_filenames == []
    assert report.cues_copied == 1
    assert report.crate_memberships_copied == 1
    assert report.crates_created == ["House"]

    result_conn = sqlite3.connect(str(dest_path))
    row = result_conn.execute(
        "SELECT rating, comment, bpm FROM library WHERE id = ?", (dest_track_id,)
    ).fetchone()
    assert row == (5, "great track", 128.0)

    cue = result_conn.execute("SELECT hotcue, label FROM cues WHERE track_id = ?", (dest_track_id,)).fetchone()
    assert cue == (0, "Drop")

    crate = result_conn.execute(
        "SELECT crates.name, crates.count FROM crate_tracks "
        "JOIN crates ON crate_tracks.crate_id = crates.id WHERE crate_tracks.track_id = ?",
        (dest_track_id,),
    ).fetchone()
    assert crate == ("House", 1)
    result_conn.close()

    backups = list(tmp_path.glob("dest.sqlite.bak-*"))
    assert len(backups) == 1


def test_migrate_dry_run_changes_nothing(tmp_path):
    source_path, dest_path = tmp_path / "source.sqlite", tmp_path / "dest.sqlite"

    src_conn = make_db(source_path)
    add_track(src_conn, "song.mp3", 200.0, rating=5)
    src_conn.close()

    dest_conn = make_db(dest_path)
    dest_track_id = add_track(dest_conn, "song.mp3", 200.0, rating=0)
    dest_conn.close()

    report = migrate(source_path, dest_path, dry_run=True)

    assert report.matched_count == 1
    result_conn = sqlite3.connect(str(dest_path))
    rating = result_conn.execute("SELECT rating FROM library WHERE id = ?", (dest_track_id,)).fetchone()[0]
    assert rating == 0  # untouched
    result_conn.close()

    assert list(tmp_path.glob("dest.sqlite.bak-*")) == []  # no backup made for a dry run


def test_migrate_rolls_back_on_failure_leaving_dest_unchanged(tmp_path, monkeypatch):
    source_path, dest_path = tmp_path / "source.sqlite", tmp_path / "dest.sqlite"

    src_conn = make_db(source_path)
    add_track(src_conn, "song.mp3", 200.0, rating=5)
    src_conn.close()

    dest_conn = make_db(dest_path)
    dest_track_id = add_track(dest_conn, "song.mp3", 200.0, rating=0)
    dest_conn.close()

    import spotify_queuer.mixxx_migrate as mixxx_migrate

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(mixxx_migrate, "_copy_cues", boom)

    try:
        mixxx_migrate.migrate(source_path, dest_path)
    except RuntimeError:
        pass

    result_conn = sqlite3.connect(str(dest_path))
    rating = result_conn.execute("SELECT rating FROM library WHERE id = ?", (dest_track_id,)).fetchone()[0]
    assert rating == 0  # rolled back, not partially applied
    result_conn.close()
