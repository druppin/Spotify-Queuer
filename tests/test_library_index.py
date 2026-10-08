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
