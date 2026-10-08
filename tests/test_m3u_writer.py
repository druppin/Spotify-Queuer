from spotify_queuer.m3u_writer import M3UEntry, playlist_m3u_path, sanitize_filename, write_playlist_m3u


def test_sanitize_filename_strips_invalid_chars():
    assert sanitize_filename('House: Deep/Tech?') == 'House_ Deep_Tech_'


def test_sanitize_filename_falls_back_when_empty():
    assert sanitize_filename('   ') == 'playlist'


def test_sanitize_filename_replaces_each_invalid_char():
    assert sanitize_filename('???') == '___'


def test_write_playlist_m3u_atomic_and_relative_paths(tmp_path):
    music_root = tmp_path / "Artist" / "Album"
    music_root.mkdir(parents=True)
    track_file = music_root / "Title - Artist.flac"
    track_file.write_bytes(b"fake")

    out_dir = tmp_path / "_Playlists"
    entries = [M3UEntry(file_path=track_file, title="Title", artists="Artist", duration_seconds=200)]
    out_path = write_playlist_m3u("My Playlist", entries, out_dir)

    assert out_path == playlist_m3u_path("My Playlist", out_dir)
    content = out_path.read_text(encoding="utf-8")
    assert content.startswith("#EXTM3U")
    assert "#EXTINF:200,Artist - Title" in content
    assert str(track_file) not in content  # path must be relative, not absolute
    assert not out_path.with_name(out_path.name + ".tmp").exists()


def test_write_playlist_m3u_rewrites_whole_file(tmp_path):
    music_root = tmp_path / "Artist" / "Album"
    music_root.mkdir(parents=True)
    track_a = music_root / "A.flac"
    track_a.write_bytes(b"fake")
    track_b = music_root / "B.flac"
    track_b.write_bytes(b"fake")

    out_dir = tmp_path / "_Playlists"
    write_playlist_m3u("P", [M3UEntry(file_path=track_a, title="A", artists="X")], out_dir)
    out_path = write_playlist_m3u("P", [M3UEntry(file_path=track_b, title="B", artists="X")], out_dir)

    content = out_path.read_text(encoding="utf-8")
    assert "A.flac" not in content
    assert "B.flac" in content
