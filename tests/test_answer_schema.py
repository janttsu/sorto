"""The answer is constrained to the outline: a schema for Ollama, a lean packet, and fsck's free numbers."""

from __future__ import annotations

from pathlib import Path

from conftest import make_engine, packet
from sorto.fsck_model import (
    Proposal,
    _free_categories,
    _free_ids,
    _structure_question,
    _unknown_ids,
)
from sorto.jd import scan_jd
from sorto.llm import classification_schema
from test_ollama_api import Server, _client


def test_the_schema_lists_only_the_outlines_ids_in_the_order_of_deciding() -> None:
    schema = classification_schema(["13.01", "13.13"], ["13"])
    props = schema["properties"]
    assert props["jd_id"]["enum"] == ["13.01", "13.13", "new"]
    assert props["category"]["enum"] == ["13", "none"]
    order = list(props)
    assert order.index("rule") < order.index("category") < order.index("jd_id") < order.index("reason")
    assert set(schema["required"]) == set(props)


def test_the_engine_hands_the_outline_to_the_client(target: Path, cfg) -> None:
    engine = make_engine(cfg)
    try:
        ids = engine._answer_schema["properties"]["jd_id"]["enum"]
        assert "13.13" in ids and "11.12" in ids and "00.00" not in ids and ids[-1] == "new"
    finally:
        engine.db.close()


def test_ollama_gets_the_schema_as_format_other_servers_plain_json(monkeypatch) -> None:
    schema = classification_schema(["13.13"], ["13"])
    server = Server()
    llm = _client(server, monkeypatch)
    llm.answer_schema = schema
    llm.classify(packet("bill.pdf"), "system")
    assert server.last("/api/chat")["format"] == schema
    other = Server(ollama=False)
    llm = _client(other, monkeypatch)
    llm.answer_schema = schema
    llm.classify(packet("bill.pdf"), "system")
    body = other.last("/v1/chat/completions")
    assert body["response_format"] == {"type": "json_object"} and "schema" not in body


def test_the_packet_leaves_out_what_does_not_help_the_model() -> None:
    known = packet("bill.pdf", mime="application/pdf", magic="PDF document", hex_preview="25504446")
    d = known.to_llm_dict()
    assert not {"sha256", "type_guess", "magic", "hex_preview"} & set(d)
    blob = packet("blob.bin", mime="application/octet-stream", magic="data", hex_preview="00ff", text_preview="")
    d = blob.to_llm_dict()
    assert d["magic"] == "data" and d["hex_preview"] == "00ff" and "sha256" not in d


def test_fsck_gives_free_numbers_and_flags_made_up_ids(target: Path) -> None:
    index = scan_jd(target)
    free = _free_ids(index)
    assert free["13 Money"] == "13.14" and free["11 Health"] == "11.13"
    assert _free_categories(index)["10-19 Life"] == "14"
    question = _structure_question(index, {}, [])
    assert "FREE NUMBERS" in question and "- next free ID in 13 Money: 13.14" in question
    proposal = Proposal("Merge", "Move 13.13 into 13.14 and 90.11.", "why", ["Make 13.14"], "small")
    assert _unknown_ids(proposal, index, set(free.values())) == ["90.11"]
