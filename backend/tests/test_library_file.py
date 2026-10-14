"""test_library_file.py — the exported library file is library.csv, for Plex and Jellyfin alike.

Before Jellyfin it was plex_library.csv. An install that hasn't run Export since the rename still
reads its old file, until the next Export writes library.csv. The old file is never deleted, so
going back to an older version still finds it.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

import channel_engine
import export

HEADER = "Type,Title,Year,Genres\n"


def test_export_writes_library_csv():
    assert export.OUTPUT_FILE == "library.csv"


def test_a_new_install_uses_library_csv(tmp_path):
    assert channel_engine.library_csv(tmp_path) == tmp_path / "library.csv"


def test_an_older_install_still_reads_plex_library_csv(tmp_path):
    (tmp_path / "plex_library.csv").write_text(HEADER)
    assert channel_engine.library_csv(tmp_path) == tmp_path / "plex_library.csv"


def test_once_exported_again_the_new_file_wins(tmp_path):
    (tmp_path / "plex_library.csv").write_text(HEADER)
    (tmp_path / "library.csv").write_text(HEADER)
    assert channel_engine.library_csv(tmp_path) == tmp_path / "library.csv"


def test_the_screens_read_an_older_install_s_file(pr):
    (pr._test_data_dir / "plex_library.csv").write_text(HEADER + "Movie,Heat,1995,Crime\n", encoding="utf-8")
    info = pr.csv_info()
    assert info["exists"] is True and info["movies"] == 1


def test_the_download_is_named_library_csv(pr):
    (pr._test_data_dir / "library.csv").write_text(HEADER, encoding="utf-8")
    assert pr.download_csv().filename == "library.csv"


def test_plex_libraries_are_labelled_plex(pr, monkeypatch):
    """The Export/Scan library picker names the server: "Plex" and "Jellyfin", not "Your Library"."""
    monkeypatch.setattr(pr, "_load_config", lambda: {"plex_url": "http://p", "plex_token": "x"})
    monkeypatch.setattr(pr, "_plex_get", lambda *a: {"MediaContainer": {"Directory": [
        {"key": "1", "title": "Movies", "type": "movie"}]}})
    assert pr.list_libraries() == [{"key": "plex:1", "title": "Movies", "type": "movie", "server": "Plex"}]
