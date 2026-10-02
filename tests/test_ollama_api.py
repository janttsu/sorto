"""An Ollama server is spoken to through its own /api/chat: keep_alive per request, num_ctx and num_gpu."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from sorto.config import load_config
from sorto.engine import make_llm
from sorto.llm import LLMError, OpenAICompatClient, keep_alive_value, user_content

ANSWER = '{"summary": "s", "jd_id": "13.13", "confidence": 0.9}'


class Server:
    """A stand-in for the model server: records every request it gets."""

    def __init__(self, *, ollama: bool = True, loaded: bool = True, chat_status: int = 200, chat_error: str = "",
                 expires: str = "2024-01-02T03:04:05.123456789+00:00"):
        self.ollama, self.loaded, self.chat_status, self.chat_error = ollama, loaded, chat_status, chat_error
        self.expires = expires
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.34.4"}) if self.ollama else httpx.Response(404, text="not found")
        if path == "/api/chat":
            if not self.ollama:
                return httpx.Response(404, text="404 page not found")
            if self.chat_status != 200:
                status, self.chat_status = self.chat_status, 200  # fail once, then work
                return httpx.Response(status, text=self.chat_error)
            return httpx.Response(200, json={"message": {"role": "assistant", "content": ANSWER},
                                             "prompt_eval_count": 9000, "eval_count": 200})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "m:1", "expires_at": self.expires}] if self.loaded else []})
        if path == "/api/generate":
            return httpx.Response(200, json={"done": True})
        if path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": ANSWER}}]})
        return httpx.Response(404, text="not found")

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    def last(self, path: str) -> dict:
        return next(b for p, b in reversed(self.calls) if p == path)


def _client(server: Server, monkeypatch, **kw) -> OpenAICompatClient:
    monkeypatch.setattr(
        OpenAICompatClient, "_client",
        lambda self, timeout: httpx.Client(transport=httpx.MockTransport(server), timeout=timeout),
    )
    return OpenAICompatClient(base_url="http://127.0.0.1:11434/v1", model="m:1", **kw)


def _ask(client: OpenAICompatClient, images: list[bytes] | None = None) -> str:
    return client._complete([{"role": "system", "content": "sys"},
                             {"role": "user", "content": user_content("look", images or [])}])


def test_ollama_gets_the_native_chat_call(monkeypatch) -> None:
    server = Server()
    client = _client(server, monkeypatch)
    assert _ask(client, [b"\xff\xd8jpeg"]) == ANSWER
    assert server.paths() == ["/api/version", "/api/ps", "/api/chat"]  # /api/ps: was it loaded for good already?
    body = server.last("/api/chat")
    assert body["model"] == "m:1" and body["stream"] is False and body["format"] == "json"
    assert body["think"] is False  # reasoning_effort "none"
    assert body["keep_alive"] == -1  # stays loaded while sorto runs
    assert body["options"] == {"temperature": 0.6, "top_p": 0.95, "num_predict": 800}  # no num_ctx: no reload
    user = body["messages"][1]
    assert user == {"role": "user", "content": "look", "images": [base64.b64encode(b"\xff\xd8jpeg").decode()]}
    assert client.last_tokens_est == 9200  # the server's real count
    _ask(client)
    assert server.paths().count("/api/version") == 1  # asked once


def test_another_server_gets_the_openai_call(monkeypatch) -> None:
    server = Server(ollama=False)
    client = _client(server, monkeypatch)
    assert _ask(client) == ANSWER
    assert server.paths() == ["/api/version", "/v1/chat/completions"]
    assert "keep_alive" not in server.last("/v1/chat/completions")
    forced = Server()  # an Ollama server, but the user wants its OpenAI-compatible endpoint
    assert _ask(_client(forced, monkeypatch, api="openai")) == ANSWER
    assert forced.paths() == ["/v1/chat/completions"]


@pytest.mark.parametrize(
    ("setting", "sent"),
    [("run", -1), ("45m", "45m"), ("-1", -1), ("0", 0), ("600", 600), ("", None), ("server", None)],
)
def test_keep_alive_setting(monkeypatch, setting, sent) -> None:
    assert keep_alive_value(setting) == sent
    server = Server()
    _ask(_client(server, monkeypatch, keep_alive=setting))
    assert server.last("/api/chat").get("keep_alive") == sent


def test_num_ctx_and_num_gpu_are_sent_only_when_set(monkeypatch) -> None:
    server = Server()
    _ask(_client(server, monkeypatch, num_ctx=16384, num_gpu=99))
    assert server.last("/api/chat")["options"]["num_ctx"] == 16384
    assert server.last("/api/chat")["options"]["num_gpu"] == 99


def test_release_sets_a_real_unload_time_when_the_run_ends(monkeypatch) -> None:
    """Ollama keeps the last keep_alive of a loaded model; leaving it out does not bring the default back."""
    server = Server()
    client = _client(server, monkeypatch)
    _ask(client)
    client.release()
    assert server.last("/api/generate") == {"model": "m:1", "keep_alive": "5m"}
    custom = Server()
    client = _client(custom, monkeypatch, keep_alive_after="30m")
    _ask(client)
    client.release()
    assert custom.last("/api/generate") == {"model": "m:1", "keep_alive": "30m"}


def test_release_leaves_alone_what_is_not_sortos_to_change(monkeypatch) -> None:
    gone = Server(loaded=False)
    client = _client(gone, monkeypatch)
    _ask(client)
    client.release()
    assert "/api/generate" not in gone.paths()  # not loaded any more: stopping sorto never loads a model
    fixed = Server()
    client = _client(fixed, monkeypatch, keep_alive="45m")
    _ask(client)
    client.release()
    assert "/api/generate" not in fixed.paths()  # a fixed time needs no handing back
    forever = Server(expires="2318-12-31T23:59:59+00:00")  # the server already kept it loaded for good
    client = _client(forever, monkeypatch)
    _ask(client)
    client.release()
    assert "/api/generate" not in forever.paths()
    stay = Server()
    client = _client(stay, monkeypatch, keep_alive_after="-1")
    _ask(client)
    client.release()
    assert "/api/generate" not in stay.paths()


def test_native_call_survives_a_model_without_thinking_or_vision(monkeypatch) -> None:
    server = Server(chat_status=400, chat_error='"m:1" does not support thinking')
    client = _client(server, monkeypatch)
    assert _ask(client) == ANSWER and "think" not in server.last("/api/chat")
    server = Server(chat_status=500, chat_error="this model cannot read images")
    client = _client(server, monkeypatch)
    assert _ask(client, [b"jpeg"]) == ANSWER and "images" not in server.last("/api/chat")["messages"][1]
    server = Server(chat_status=503, chat_error="overloaded")
    client = _client(server, monkeypatch, max_retries=0)
    with pytest.raises(LLMError, match="LLM HTTP 503"):
        _ask(client)


def test_settings_reach_the_client(inbox, target, tmp_path) -> None:
    user = tmp_path / "xdg-config" / "sorto" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('[llm]\napi = "ollama"\nkeep_alive = "2h"\nkeep_alive_after = "30m"\nnum_ctx = 16384\nnum_gpu = 99\n', encoding="utf-8")
    cfg = load_config(inbox, target)
    assert (cfg.llm_api, cfg.keep_alive, cfg.num_ctx, cfg.num_gpu) == ("ollama", "2h", 16384, 99)
    text = cfg.to_toml()
    assert 'keep_alive = "2h"' in text and "num_ctx = 16384" in text and 'api = "ollama"' in text
    llm = make_llm(cfg)
    assert llm._native is True and llm.keep_alive == "2h" and llm.num_ctx == 16384 and llm.num_gpu == 99
    assert llm.keep_alive_after == "30m" and load_config(inbox, target).keep_alive_after == "30m"
    user.unlink()
    default = load_config(inbox, target)
    assert (default.llm_api, default.keep_alive, default.num_ctx, default.num_gpu) == ("auto", "run", 0, -1)


def test_the_engine_releases_its_models_when_the_run_ends(inbox, target, cfg) -> None:
    from sorto.engine import Engine
    from sorto.llm import FakeLLMClient

    class Held(FakeLLMClient):
        released = 0

        def release(self) -> None:
            self.released += 1

    (inbox / "invoice.txt").write_text("Invoice", encoding="utf-8")
    reader, structure = Held(routes={"invoice": "13.13"}), Held()
    Engine(cfg, llm=reader, structure_llm=structure).run_until_idle(timeout=30)
    assert reader.released == 1 and structure.released == 1
