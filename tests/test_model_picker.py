"""Choosing the local model: --model, or a list of every model the server has, asked at the start."""

from __future__ import annotations

import asyncio

from sorto.cli import build_parser
from sorto.model_picker import ModelPicker, pick_model

MODELS = [
    {"name": "big:35b", "size": 22_600_000_000, "parameters": "35.5B", "quantization": "Q4_K_M", "loaded": True},
    {"name": "small:9b", "size": 6_600_000_000, "parameters": "9.7B", "quantization": "Q4_K_M", "loaded": False},
    {"name": "tiny:2b", "size": 2_700_000_000, "parameters": "2.3B", "quantization": "Q8_0", "loaded": False},
]


def _pick(keys: list[str], default: str = "big:35b"):
    app = ModelPicker(MODELS, default=default, purpose="Which model?")

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            rows = [str(r.query_one("Label").render()) for r in app.query_one("#models").children]
            for key in keys:
                await pilot.press(key)
                await pilot.pause()
            return rows

    rows = asyncio.run(go())
    return app.return_value, rows


def test_every_model_is_listed_with_its_size_and_the_default_is_marked() -> None:
    chosen, rows = _pick(["enter"])
    assert chosen == "big:35b"
    assert len(rows) == 3
    assert "22.6 GB" in rows[0] and "35.5B" in rows[0] and "default" in rows[0] and "in memory" in rows[0]
    assert "default" not in rows[1]


def test_arrows_and_typing_choose_another_model() -> None:
    assert _pick(["down", "enter"])[0] == "small:9b"
    assert _pick(["t", "i", "n", "enter"])[0] == "tiny:2b"  # typing filters the list


def test_escape_runs_nothing() -> None:
    assert _pick(["escape"])[0] is None


def test_without_a_list_from_the_server_the_configured_model_is_used() -> None:
    class NoList:
        def model_catalog(self):
            raise OSError("server down")

    assert pick_model(NoList(), default="big:35b", purpose="") == "big:35b"


def test_model_and_llm_model_are_the_same_option() -> None:
    parser = build_parser()
    for flag in ("--model", "--llm-model"):
        assert parser.parse_args(["run", "in", "-t", "out", flag, "small:9b"]).llm_model == "small:9b"
        assert parser.parse_args(["fsck", "out", flag, "small:9b"]).llm_model == "small:9b"
    assert parser.parse_args(["run", "in", "-t", "out"]).llm_model is None
