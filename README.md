# Spotify Queuer

Four independent tools around a DJ music workflow:

1. **Playlist queue picker** — every Spotify playlist listed with cover art;
   check the ones you want, hit "Queue Selected", and they're sent to
   Spotify playback in list order (optionally skipping tracks you already
   have locally).
2. **Download watcher** — watches a folder for newly downloaded audio files
   and files them into a dated `recently downloaded <date>.m3u8` so you can
   find and sort the day's batch into Mixxx crates. Uses a filesystem-event
   watcher *and* a periodic full-directory rescan as a backstop, since a
   missed file here is real data loss and Mixxx has no safety net of its own
   (it only shows a "date added" view of what it already knows about).
3. **USB sync** (`Sync USB Sticks...` in the main window) — additive/update
   only: copies new or changed files from a source stick to a destination
   stick, and never deletes anything already on the destination.
4. **Mixxx metadata migration** (`tools/mixxx_metadata_migrate.py`) — a
   standalone script for the rare case you need to bring a second USB
   stick's Mixxx library (ratings, hot cues, play counts, beatgrid, crate
   memberships, ...) up to date with the one you've actually been curating.
   Matches tracks across the two `mixxxdb.sqlite` files by filename +
   duration (their absolute paths differ), always backs up the destination
   database first, and applies everything in one all-or-nothing transaction.

Note: [`Spotify Song Queuer — Build Spec.docx`](Spotify%20Song%20Queuer%20%E2%80%94%20Build%20Spec.docx)
describes the *original* design (continuous polling that auto-queued missing
tracks and mirrored playlists to `.m3u8`). That approach didn't work out in
practice and has been fully replaced by the four tools above.

## Setup

1. Python 3.11+, then:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements-dev.txt
   pip install -e .
   ```
   The editable install (`pip install -e .`) registers the `spotify_queuer`
   package (it lives under `src/`) so `python -m spotify_queuer` resolves;
   `requirements-dev.txt` alone gets you the dependencies but not the package
   itself.
2. Create a Spotify Developer app at https://developer.spotify.com/dashboard
   and add `http://127.0.0.1:8888/callback` as a redirect URI.
3. Copy `config.example.json` to `config.json` and fill in `spotify.client_id`,
   `spotify.client_secret`, `spotify.device_name` (must match the name shown
   in Spotify Connect for the PC running this app), and `paths.*` /
   `download_watch.watch_dir`.
4. Have the Spotify desktop client (or a librespot-based agent) running on
   that PC before starting this app — it can't create a Connect device itself.

## Run

```bash
python -m spotify_queuer            # uses ./config.json
python -m spotify_queuer path\to\config.json
```

First run opens a browser for the Spotify OAuth consent flow; the token is
cached alongside the config file (`.spotify_token_cache`) and silently
refreshed after that.

The download watcher runs only while the window is open — closing it stops
everything (no system tray / background behavior).

### Mixxx metadata migration (separate, occasional-use tool)

```bash
python tools/mixxx_metadata_migrate.py --source D:\...\mixxxdb.sqlite --dest E:\...\mixxxdb.sqlite --dry-run
python tools/mixxx_metadata_migrate.py --source D:\...\mixxxdb.sqlite --dest E:\...\mixxxdb.sqlite
```

Close Mixxx on both sides first — it holds its database open while running.
Always test with `--dry-run` before a real run.

## Tests

```bash
pytest
```

Covers the pure logic: fuzzy matching, `.m3u8` writing, the rate limiter,
config load/save, the queue engine (fake Spotify client, no network/GUI),
the download watcher's file-stability/dedup logic, USB sync's diff/copy
logic, and the Mixxx migration's track-matching and transactional
copy/rollback behavior (against synthetic sqlite fixtures, no real Mixxx
install needed). GUI and live-Spotify behavior still need manual
verification.

## Packaging

```bash
pip install pyinstaller
pyinstaller spotify_queuer.spec
```

Produces `dist/SpotifyQueuer.exe` (onefile, windowed).

## Layout

- `src/spotify_queuer/matching.py` — shared fuzzy title/artist matcher
- `src/spotify_queuer/spotify_client.py` — spotipy wrapper (rate limiting, queue/playback calls)
- `src/spotify_queuer/queue_engine.py` — playlist-picker queuing logic (skip-local filter, queue vs. start_playback policy)
- `src/spotify_queuer/library_index.py` — local library scan (on demand)
- `src/spotify_queuer/download_watcher.py` — downloads-folder watcher + dated `.m3u8` filing
- `src/spotify_queuer/m3u_writer.py` — atomic `.m3u8` output
- `src/spotify_queuer/usb_sync.py` — additive/update-only directory diff + copy
- `src/spotify_queuer/mixxx_migrate.py` — Mixxx `mixxxdb.sqlite` metadata migration logic
- `src/spotify_queuer/logging_setup.py` — activity/error logs
- `src/spotify_queuer/app.py` — wires the above together, GUI-agnostic
- `src/spotify_queuer/gui/` — PySide6 UI
- `tools/mixxx_metadata_migrate.py` — CLI wrapper around `mixxx_migrate.py`
