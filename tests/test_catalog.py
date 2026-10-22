from spotify_queuer.catalog import Catalog, rewrite_playlist_files
from spotify_queuer.db import connect
from spotify_queuer.queue_engine import QueuedTrack


def test_playlist_file_lists_local_tracks_in_spotify_order(tmp_path):
    db = tmp_path / "app.sqlite3"
    catalog = Catalog(db)
    catalog.sync_playlists([{"id": "p1", "name": "Friday Set"}])
    tracks = [QueuedTrack(f"spotify:track:{t}", f"Song {t}", "Artist", playlist_id="p1") for t in "abc"]
    catalog.store_playlist("p1", "s1", tracks)
    conn = connect(db)
    for name in ("c.mp3", "a.mp3"):
        conn.execute("INSERT INTO library_files (path, first_seen_at) VALUES (?, 0)", (str(tmp_path / name),))
    conn.commit()
    assert catalog.link("spotify:track:c", tmp_path / "c.mp3", "match", 95)
    assert catalog.link("spotify:track:a", tmp_path / "a.mp3", "download")
    assert not catalog.link("spotify:track:b", tmp_path / "unknown.mp3", "match")  # not a known file

    rewrite_playlist_files(catalog, ["p1"], tmp_path / "Playlists")

    lines = (tmp_path / "Playlists" / "Friday Set.m3u8").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.endswith(".mp3")] == ["..\\a.mp3", "..\\c.mp3"]  # b has no file
