from __future__ import annotations

import pytest

from conftest import packet
from sorto.llm import (
    FakeLLMClient,
    LLMParseError,
    NotLocalError,
    OpenAICompatClient,
    ensure_local_url,
    parse_classification,
)


def test_parse_json_object() -> None:
    cls = parse_classification(
        '{"summary":"An invoice from ACME for March 2024.","label":"invoice","jd_id":"13.13",'
        '"subfolder":"2024","new_filename":"","confidence":0.8,"reason":"bill","needs_user":false}'
    )
    assert cls.jd_id == "13.13" and cls.subfolder == "2024"
    assert cls.summary.startswith("An invoice")
    assert cls.confidence == 0.8 and cls.needs_user is False


def test_parse_fenced_and_string_bools() -> None:
    cls = parse_classification(
        'Sure.\n```json\n{"summary":"s","jd_id":"11.12","confidence":"2","needs_user":"false"}\n```\n'
    )
    assert cls.jd_id == "11.12" and cls.confidence == 1.0 and cls.needs_user is False


def test_parse_invalid() -> None:
    with pytest.raises(ValueError):
        parse_classification("not json at all")
    with pytest.raises(ValueError):
        parse_classification('{"label": "x"}')


def test_fake_invalid_raises() -> None:
    with pytest.raises(LLMParseError):
        FakeLLMClient(invalid=True).classify(packet("a.txt"), "prompt")


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:11435/v1", "http://localhost:11434/v1", "http://[::1]:8080/v1"]
)
def test_local_urls_accepted(url: str) -> None:
    assert ensure_local_url(url) == url


@pytest.mark.parametrize(
    "url",
    ["http://192.168.1.10:11434/v1", "http://8.8.8.8/v1", "http://10.0.0.1/v1", "ftp://127.0.0.1/"],
)
def test_remote_urls_refused(url: str) -> None:
    with pytest.raises(NotLocalError):
        ensure_local_url(url)
    with pytest.raises(NotLocalError):
        OpenAICompatClient(base_url=url, model="m")


def test_focused_answers_survive_a_reply_cut_off_in_its_reason() -> None:
    """A model unsure of itself wrote on in "reason" until max_tokens: the name was there, the JSON was not."""
    import pytest

    from sorto.llm import parse_category_answer, parse_new_category_answer, parse_new_id_answer

    cut = '{\n"name": "Travel",\n"reason": "The file is a train ticket, which falls under travel'
    assert parse_new_id_answer(cut) == ("Travel", "(the reply was cut off)")
    assert parse_category_answer('{"category": "14", "confidence": 0.9, "reason": "Travel docu') == (
        "14", 0.9, "(the reply was cut off)"
    )
    assert parse_new_category_answer(
        '{"area": "10-19", "category_name": "Pets and \\"more\\"", "confidence": 0.85, "reason": "Pets'
    ) == ("10-19", 'Pets and "more"', 0.85, "(the reply was cut off)")
    assert parse_new_id_answer('{"name": "Ok", "reason": "fine"}') == ("Ok", "fine")
    for unusable in ("no json here", '{"reason": "only a reason, cut o'):
        with pytest.raises(ValueError):
            parse_new_id_answer(unusable)
