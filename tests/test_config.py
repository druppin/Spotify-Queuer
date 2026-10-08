from spotify_queuer.config import AppConfig


def test_config_roundtrip(tmp_path):
    cfg = AppConfig()
    cfg.spotify.client_id = "abc"
    cfg.queue.last_selected_playlist_ids = ["1", "2"]
    cfg.matching.fuzzy_threshold = 90
    cfg.download_watch.watch_dir = "D:\\Downloads"

    path = tmp_path / "config.json"
    cfg.save(path)
    loaded = AppConfig.load(path)

    assert loaded.spotify.client_id == "abc"
    assert loaded.queue.last_selected_playlist_ids == ["1", "2"]
    assert loaded.matching.fuzzy_threshold == 90
    assert loaded.download_watch.watch_dir == "D:\\Downloads"
    assert not path.with_name(path.name + ".tmp").exists()


def test_load_or_default_returns_defaults_when_missing(tmp_path):
    cfg = AppConfig.load_or_default(tmp_path / "nope.json")
    assert cfg.matching.fuzzy_threshold == 87
    assert cfg.queue.last_selected_playlist_ids == []
    assert cfg.queue.skip_local_tracks is True
    assert cfg.queue.interrupt_playback is True


def test_load_ignores_unknown_keys_from_older_config_files(tmp_path):
    """A field removed from the schema (e.g. the old playlists.tracked_ids)
    shouldn't break loading a config file that still has it on disk."""
    path = tmp_path / "config.json"
    path.write_text(
        '{"queue": {"skip_local_tracks": false, "made_up_field": 1}, '
        '"playlists": {"tracked_ids": ["1"]}}',
        encoding="utf-8",
    )
    cfg = AppConfig.load(path)
    assert cfg.queue.skip_local_tracks is False
    assert not hasattr(cfg.queue, "made_up_field")
