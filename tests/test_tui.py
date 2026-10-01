from __future__ import annotations

from textual.widgets import Static

from sorto.tui import _plain


def test_plain_static_accepts_markup_looking_filenames() -> None:
    w = _plain("", id="log")
    assert w._render_markup is False
    nasty = (
        "log: identify  Audio/2024/2024-02/[Live] [Take 2] Song.m4a  "
        "mime=audio/x-m4a 7.24s"
    )
    w.update(nasty)
    assert w._content == nasty


def test_static_default_markup_would_break() -> None:
    w = Static("")
    nasty = "identify  audio/x-m4a [Live] file.m4a"
    try:
        w.update(nasty)
    except Exception as e:
        assert "markup" in type(e).__name__.lower() or "markup" in str(e).lower()
        return
    # Some Textual versions may not raise until render; markup still on.
    assert w._render_markup is True


def _run_app(cfg, script) -> None:
    import asyncio

    from conftest import make_engine
    from sorto.tui import SortoApp

    app = SortoApp(make_engine(cfg))

    async def go() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await script(app, pilot)

    asyncio.run(go())


def test_tui_shows_analysis_and_destination(inbox, target, cfg) -> None:
    (inbox / "invoice-june.txt").write_text("Invoice 12 EUR", encoding="utf-8")
    cfg.follow = True
    seen: dict[str, str] = {}

    async def script(app, pilot) -> None:
        for _ in range(60):
            await pilot.pause(0.1)
            last = str(app.query_one("#now", Static)._content)
            if "Result:" in last:
                seen["now"] = last
                break
        app.engine.request_stop()

    _run_app(cfg, script)
    text = seen["now"]
    assert "invoice-june.txt" in text and "Analysis:" in text
    assert "13.13 Invoices" in text
    assert "13.13 Invoices/invoice-june.txt" in text.replace("\n  ", "")
    assert "MOVED" in text
    assert (target / "10-19 Life/13 Money/13.13 Invoices/invoice-june.txt").exists()


def test_tui_confirm_keep_leaves_file(inbox, target, cfg) -> None:
    (inbox / "invoice-june.txt").write_text("Invoice 12 EUR", encoding="utf-8")
    cfg.follow = True
    cfg.confirm = True

    async def script(app, pilot) -> None:
        for _ in range(60):
            await pilot.pause(0.1)
            if "Move it there?" in str(app.query_one("#now", Static)._content):
                assert app.engine.snapshot().awaiting_confirm
                await pilot.press("n")
                break
        for _ in range(30):
            await pilot.pause(0.1)
            if app.engine.snapshot().counts.skipped:
                break
        app.engine.request_stop()

    _run_app(cfg, script)
    assert (inbox / "invoice-june.txt").exists()


def test_tui_shows_time_left_and_switches_model(inbox, target, cfg, monkeypatch) -> None:
    from sorto.llm import FakeLLMClient

    for i in range(4):
        (inbox / f"invoice-{i}.txt").write_text(f"Invoice {i}", encoding="utf-8")
    cfg.follow = True
    cfg.models = ["fake", "fake-small"]

    def fake_make_llm(c, *, fake=False):
        llm = FakeLLMClient(routes={"invoice": "13.13"})
        llm.model = c.llm_model
        return llm

    monkeypatch.setattr("sorto.engine.make_llm", fake_make_llm)
    seen: dict[str, str] = {}

    async def script(app, pilot) -> None:
        seen["eta0"] = str(app.query_one("#eta", Static)._content)
        await pilot.press("m")
        for _ in range(100):
            await pilot.pause(0.1)
            eta = str(app.query_one("#eta", Static)._content)  # redrawn every 0.4 s
            if app.engine.model_name == "fake-small" and "nothing left" in eta:
                break
        seen["eta1"] = str(app.query_one("#eta", Static)._content)
        seen["keys"] = str(app.query_one("#keys", Static)._content)
        app.engine.request_stop()

    _run_app(cfg, script)
    assert seen["eta0"].startswith("time left:")
    assert "nothing left" in seen["eta1"] or "scanning" in seen["eta1"]
    assert "fake-small" in seen["keys"]


def test_tui_browses_older_files_in_the_history(inbox, target, cfg) -> None:
    names = [f"invoice-{i}.txt" for i in range(6)]
    for name in names:
        (inbox / name).write_text(f"Invoice {name}", encoding="utf-8")
    cfg.follow = True
    seen: dict[str, str] = {}

    def text(app, wid: str) -> str:
        return str(app.query_one(wid, Static)._content)

    async def script(app, pilot) -> None:
        for _ in range(100):
            await pilot.pause(0.1)
            if app.engine.snapshot().counts.done == len(names) and "HISTORY (6)" in text(app, "#history-title"):
                break
        order = [h.filename for h in app.engine.snapshot().history]  # newest first
        seen["follow_title"] = text(app, "#last-title")
        assert f"File:     {order[1]}" in text(app, "#last")  # NOW holds the newest, the panel the one before
        await pilot.press("down")
        seen["one_title"] = text(app, "#last-title")
        assert f"File:     {order[2]}" in text(app, "#last")
        marked = [line for line in text(app, "#history").splitlines() if line.startswith("▶")]
        assert len(marked) == 1 and order[2] in marked[0]
        await pilot.press("end")
        assert f"File:     {order[5]}" in text(app, "#last") and "HISTORY 6 of 6" in text(app, "#last-title")
        await pilot.press("down")  # already the oldest
        assert f"File:     {order[5]}" in text(app, "#last")
        await pilot.press("k")
        assert f"File:     {order[4]}" in text(app, "#last")
        await pilot.press("pageup")  # past the newest: follow the latest again
        seen["back_title"] = text(app, "#last-title")
        assert f"File:     {order[1]}" in text(app, "#last")
        await pilot.press("j", "j")
        await pilot.press("escape")
        assert text(app, "#last-title").startswith("LAST FILED")
        # a file that arrives while browsing does not move the selection
        await pilot.press("down")
        (inbox / "invoice-late.txt").write_text("Invoice late", encoding="utf-8")
        for _ in range(100):
            await pilot.pause(0.1)
            if "HISTORY (7)" in text(app, "#history-title"):
                break
        assert f"File:     {order[2]}" in text(app, "#last") and "HISTORY 4 of 7" in text(app, "#last-title")
        app.engine.request_stop()

    _run_app(cfg, script)
    assert seen["follow_title"].startswith("LAST FILED") and "browse" in seen["follow_title"]
    assert seen["one_title"].startswith("HISTORY 3 of 6")
    assert seen["back_title"].startswith("LAST FILED")
