from spotify_queuer.library_index import LibraryIndex


def _touch(root, rel, content=b"x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_rescan_parses_layout_and_ignores_shallow_and_non_audio_files(tmp_path):
    _touch(tmp_path, "Artist/Album/Song A - Artist 1.mp3")
    _touch(tmp_path, "Artist/Album/cover.jpg")
    _touch(tmp_path, "loose.mp3")  # not in {Artist}/{Album}/

    tracks = LibraryIndex(tmp_path).rescan()

    assert [(t.title, t.artists, t.artist, t.album) for t in tracks] == [("Song A", "Artist 1", "Artist", "Album")]


def test_index_persists_across_instances(tmp_path):
    music, db = tmp_path / "music", tmp_path / "lib.sqlite3"
    _touch(music, "Artist/Album/Song A - Artist 1.mp3")
    first = LibraryIndex(music, db_path=db)
    first.rescan()
    first.close()

    second = LibraryIndex(music, db_path=db)
    assert [t.title for t in second.tracks()] == ["Song A"]  # loaded from the DB, no rescan yet
    second.close()


def test_rescan_picks_up_added_changed_and_removed_files(tmp_path):
    music, db = tmp_path / "music", tmp_path / "lib.sqlite3"
    a = _touch(music, "Artist/Album/Song A - Artist 1.mp3")
    _touch(music, "Artist/Album/Song B - Artist 1.mp3")
    index = LibraryIndex(music, db_path=db)
    index.rescan()

    a.unlink()
    _touch(music, "Artist/Album/Song C - Artist 1.mp3")
    assert sorted(t.title for t in index.rescan()) == ["Song B", "Song C"]
    index.close()

    reloaded = LibraryIndex(music, db_path=db)
    assert sorted(t.title for t in reloaded.tracks()) == ["Song B", "Song C"]
    reloaded.close()


def test_match_index_is_cached_until_library_changes(tmp_path):
    _touch(tmp_path, "Artist/Album/Song A - Artist 1.mp3")
    index = LibraryIndex(tmp_path)
    index.rescan()
    first = index.match_index()

    index.rescan()  # nothing changed
    assert index.match_index() is first

    _touch(tmp_path, "Artist/Album/Song B - Artist 1.mp3")
    index.rescan()
    rebuilt = index.match_index()
    assert rebuilt is not first
    assert len(rebuilt) == 2


def _tagged_mp3(root, rel, title, artists):
    from mutagen.easyid3 import EasyID3

    path = _touch(root, rel, (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 10)  # a few silent MPEG frames
    tags = EasyID3()
    tags["title"] = title
    tags["artist"] = artists
    tags.save(path)
    return path


def test_rescan_reads_title_and_artist_tags(tmp_path):
    _tagged_mp3(tmp_path, "Artist/Album/Song (Old Name).mp3", "Song - Extended Mix", ["Artist 1", "Artist 2"])

    [track] = LibraryIndex(tmp_path).rescan()

    assert (track.tag_title, track.tag_artists) == ("Song - Extended Mix", "Artist 1; Artist 2")


def test_match_index_matches_on_tags_when_filename_does_not(tmp_path):
    # Filename lost the " - Remix" suffix and names only the first artist; the tags are right.
    _tagged_mp3(
        tmp_path,
        "The Bloody Beetroots/Warp 1.9 [Remix]/Warp 1.9 (feat. Steve Aoki).mp3",
        "Warp 1.9 (feat. Steve Aoki) - longstoryshort Remix",
        ["The Bloody Beetroots", "Steve Aoki", "longstoryshort"],
    )
    index = LibraryIndex(tmp_path)
    index.rescan()

    candidate, _ = index.match_index().best(
        "Warp 1.9 (feat. Steve Aoki) - longstoryshort Remix", ["The Bloody Beetroots", "Steve Aoki", "longstoryshort"]
    )

    assert candidate is not None


def test_match_index_matches_untagged_title_containing_dash(tmp_path):
    _touch(tmp_path, "Artist 1/Song (2024)/Song - Extended Mix.mp3")
    index = LibraryIndex(tmp_path)
    index.rescan()

    candidate, _ = index.match_index().best("Song - Extended Mix", ["Artist 1"])

    assert candidate is not None


def test_rows_from_before_tags_were_indexed_are_reread(tmp_path):
    import sqlite3

    music, db = tmp_path / "music", tmp_path / "lib.sqlite3"
    path = _tagged_mp3(music, "Artist/Album/Song.mp3", "Song - Extended Mix", ["Artist 1"])
    stat = path.stat()
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE library_tracks (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL, "
        "artist TEXT NOT NULL, album TEXT NOT NULL, title TEXT NOT NULL, artists TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO library_tracks VALUES (?, ?, ?, 'Artist', 'Album', 'Song', 'Artist')",
        (str(path), stat.st_size, stat.st_mtime),
    )
    conn.commit()
    conn.close()

    index = LibraryIndex(music, db_path=db)
    [track] = index.rescan()
    index.close()

    assert track.tag_title == "Song - Extended Mix"
