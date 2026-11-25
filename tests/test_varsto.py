from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sorto.varsto import VarstoError, VarstoMcp

FAKE = Path(__file__).parent / "fixtures" / "fake_varsto_mcp.py"


def fake(mode: str = "normal", timeout: float = 5.0) -> VarstoMcp:
    return VarstoMcp([sys.executable, str(FAKE), mode], timeout=timeout)


def test_handshake_and_folders() -> None:
    with fake() as v:
        assert v.server_info["name"] == "varsto"
        assert v.tool_names() == ["varsto_folders", "varsto_write"]
        folders = v.folders()
    assert [f["name"] for f in folders] == ["archive", "inbox-demo"]
    assert folders[0]["access"] == "rw"


def test_refused_call_raises_with_varstos_reason() -> None:
    with fake() as v:
        with pytest.raises(VarstoError, match="read-only"):
            v.write("archive", "INDEX.md", "refuse")


def test_write_returns_text_when_not_json() -> None:
    with fake() as v:
        assert v.write("archive", "INDEX.md", "hello") == "ok"


def test_error_answer_is_reported() -> None:
    with fake("error") as v, pytest.raises(VarstoError, match="vault is locked"):
        v.tool_names()


def test_no_answer_times_out() -> None:
    with pytest.raises(VarstoError, match="no answer"):
        with fake("silent", timeout=0.5) as v:
            v.tool_names()


def test_missing_binary_is_a_clear_error() -> None:
    with pytest.raises(VarstoError, match="was not found"):
        VarstoMcp(["no-such-varsto-binary-xyz"]).start()


def test_check_command_lists_folders_without_secrets(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from sorto import varsto as module
    from sorto.cli import main

    class Fake(module.VarstoMcp):
        def __init__(self, command, **kw):  # run the fixture instead of the real varsto
            super().__init__([sys.executable, str(FAKE), "normal"], **kw)

    monkeypatch.setattr("sorto.varsto.VarstoMcp", Fake)
    assert main(["varsto", "check"]) == 0
    out = capsys.readouterr().out
    assert "archive: rw" in out and "inbox-demo: analyse" in out
    assert "vmk_" not in out
