from __future__ import annotations

from textual.widgets import Static

from sorto.tui import _plain


def test_plain_static_accepts_markup_looking_filenames() -> None:
    w = _plain("", id="log")
    assert w._render_markup is False
    nasty = (
        "log: identify  Audio/2025/2025-02/[F4M] [Script Fill] Petplay.m4a  "
        "mime=audio/x-m4a 7.24s"
    )
    w.update(nasty)
    assert w._content == nasty


def test_static_default_markup_would_break() -> None:
    w = Static("")
    nasty = "identify  audio/x-m4a [F4M] file.m4a"
    try:
        w.update(nasty)
    except Exception as e:
        assert "markup" in type(e).__name__.lower() or "markup" in str(e).lower()
        return
    # Some Textual versions may not raise until render; markup still on.
    assert w._render_markup is True
