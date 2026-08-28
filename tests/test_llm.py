from __future__ import annotations

import pytest

from sorto.llm import FakeLLMClient, LLMParseError, parse_classification
from sorto.models import AnalysisPacket


def test_parse_json_object() -> None:
    cls = parse_classification(
        '{"label":"invoice","confidence":0.8,"dest_rel":"documents/a.pdf",'
        '"rename":false,"reason":"looks like an invoice","needs_user":false}'
    )
    assert cls.label == "invoice"
    assert cls.dest_rel == "documents/a.pdf"
    assert cls.confidence == 0.8


def test_parse_fenced_and_garbage() -> None:
    cls = parse_classification(
        "Sure.\n```json\n"
        '{"label":"x","confidence":1,"dest_rel":"a/b.txt","rename":false,'
        '"reason":"r","needs_user":false}\n```\n'
    )
    assert cls.dest_rel == "a/b.txt"


def test_parse_invalid() -> None:
    with pytest.raises(ValueError):
        parse_classification("not json at all")


def test_fake_invalid_raises() -> None:
    client = FakeLLMClient(invalid=True)
    pkt = AnalysisPacket(
        src_rel="a.txt",
        filename="a.txt",
        extension=".txt",
        size=1,
        mtime_iso="",
        mtime_ns=0,
        mime="text/plain",
        magic=None,
        type_guess="text",
        hex_preview="",
        text_preview="",
        extra_meta={},
        sha256=None,
        top_level_folders=[],
        dest_scheme="default",
        meaningless_name=False,
    )
    with pytest.raises(LLMParseError):
        client.classify(pkt, "prompt")
