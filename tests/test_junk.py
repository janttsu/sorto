from __future__ import annotations

from sorto.junk import classify_junk


def test_known_filenames_are_junk() -> None:
    assert classify_junk(".DS_Store")
    assert classify_junk("inbox/Thumbs.db")
    assert classify_junk("foo/.nomedia")
    assert classify_junk("desktop.ini")


def test_temp_extensions_are_junk() -> None:
    assert classify_junk("downloads/chrome.crdownload")
    assert classify_junk("a/file.tmp")
    assert classify_junk("a/file.part")
    assert classify_junk("notes.txt.swp")


def test_android_and_cache_paths() -> None:
    assert classify_junk("phone/DCIM/.thumbnails/tiny.jpg")
    assert classify_junk("backup/Android/data/com.whatsapp/cache/x")
    assert classify_junk("backup/Android/data/com.foo/code_cache/y")
    assert classify_junk("LOST.DIR/file0001")
    assert classify_junk("DCIM/.trashed-1700000000-IMG_0001.jpg")
    assert classify_junk(".thumbdata3--1967297232")


def test_real_user_files_are_not_junk() -> None:
    assert classify_junk("DCIM/Camera/IMG_0001.jpg") is None
    assert classify_junk("WhatsApp/Media/WhatsApp Images/IMG-2023.jpg") is None
    assert classify_junk("documents/invoice.pdf") is None
    assert classify_junk("code/app.py") is None
    assert classify_junk("Android/data/com.foo/files/notes.txt") is None
