# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onefile build for Windows 10, per Build Spec §4.
# Build with: pyinstaller spotify_queuer.spec
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ['src/spotify_queuer/__main__.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    # watchdog's OS-specific Observer backend is loaded dynamically, which
    # PyInstaller's static import scan misses -- collect it explicitly.
    hiddenimports=collect_submodules('spotipy') + collect_submodules('watchdog'),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='SpotifyQueuer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
)
