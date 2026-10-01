from __future__ import annotations

import base64
import ipaddress
import json
import re
import socket
import time
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

import httpx

from sorto.models import AnalysisPacket, Classification
from sorto.rules import SECTION_TITLE
from sorto.util import estimate_tokens, extract_json_object

REPAIR_USER = (
    "Your previous reply was not valid JSON matching the required schema. "
    "Reply with ONLY a JSON object with keys: summary, label, rule, own_media, jd_id, subfolder, "
    "new_filename, confidence, reason, needs_user (and new_id_category, new_id_name "
    'when jd_id is "new"). No markdown, no extra text.'
)


class LLMError(RuntimeError):
    pass


class LLMParseError(LLMError):
    pass


class NotLocalError(ValueError):
    """The configured LLM endpoint is not on this machine."""


def ensure_local_url(url: str) -> str:
    """Refuse any LLM endpoint that is not a loopback address on this machine.

    File previews and metadata are private; sorto never sends them off-host.
    Every address the host name resolves to must be loopback.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise NotLocalError(f"LLM URL must be http(s): {url!r}")
    host = parsed.hostname
    if not host:
        raise NotLocalError(f"LLM URL has no host: {url!r}")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise NotLocalError(f"cannot resolve LLM host {host!r}: {e}") from e
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise NotLocalError(f"LLM host {host!r} did not resolve")
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
        if not ip.is_loopback:
            raise NotLocalError(
                f"LLM host {host!r} resolves to {addr}, which is not this machine. "
                "sorto only talks to a local model (127.0.0.1 / ::1 / localhost)."
            )
    return url


class LLMClient(Protocol):
    model: str

    def classify(self, packet: AnalysisPacket, system_prompt: str) -> Classification: ...

    def health(self) -> tuple[bool, str]: ...

    def list_models(self) -> list[str]: ...


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def parse_classification(text: str) -> Classification:
    data = extract_json_object(text)
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    jd_id = str(data.get("jd_id") or data.get("id") or "").strip()
    if not jd_id and not data.get("summary"):
        raise ValueError("LLM JSON has neither jd_id nor summary")
    return Classification(
        label=str(data.get("label") or "unknown")[:80],
        confidence=confidence,
        jd_id=jd_id[:40],
        subfolder=str(data.get("subfolder") or "")[:200],
        new_filename=str(data.get("new_filename") or "")[:200],
        summary=str(data.get("summary") or "")[:1200],
        reason=str(data.get("reason") or "")[:600],
        rule=str(data.get("rule") or "")[:300],
        new_id_category=str(data.get("new_id_category") or "")[:10],
        new_id_name=str(data.get("new_id_name") or "")[:120],
        needs_user=_as_bool(data.get("needs_user", False)),
        own_media=_as_bool(data["own_media"]) if data.get("own_media") is not None else None,
        raw=text,
    )


def packet_user_message(packet: AnalysisPacket, *, max_chars: int = 16000, rules: bool = False) -> str:
    body = json.dumps(packet.to_llm_dict(), ensure_ascii=False, indent=None)
    msg = f"Analyze this file and choose its Johnny.Decimal ID.\n\nFILE PACKET:\n{body}\n"
    if len(msg) > max_chars:
        msg = msg[: max_chars - 20] + "\n…[truncated]"
    # Local models drift into the file's own language unless reminded last;
    # the rules reminder sits here too so it is the last thing the model reads.
    tail = f"{RULES_REMINDER} " if rules else ""
    return msg + f"\n{tail}{LANGUAGE_REMINDER}"


LANGUAGE_REMINDER = (
    "Write summary and reason in English only, even if the file and the folder names are "
    "in another language. Reply with the JSON object only."
)


RULES_REMINDER = (
    "Check the USER RULES first. A rule applies only if its condition is true for this file "
    "(the right kind of file, sender, dates or subject); if one applies, follow it and quote it "
    'briefly in "rule", otherwise leave "rule" empty. If the rule gives the topic an ID of its own '
    'and the outline has none for it yet, answer jd_id "new" with new_id_name instead of another ID.'
)

IMAGE_TOKENS_EST = 450


def user_content(text: str, images: list[bytes]) -> str | list[dict[str, Any]]:
    """Plain text, or OpenAI-style multimodal parts with inline JPEGs."""
    if not images:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for jpg in images:
        b64 = base64.b64encode(jpg).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return parts


def _has_images(payload: dict[str, Any]) -> bool:
    return any(isinstance(m.get("content"), list) for m in payload.get("messages", []))


def _strip_images(payload: dict[str, Any]) -> dict[str, Any]:
    messages = []
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            text = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
            m = {**m, "content": text}
        messages.append(m)
    return {**payload, "messages": messages}


CATEGORY_SYSTEM = (
    "You choose which existing Johnny.Decimal category a new topic belongs in. Judge by each "
    "category's own theme (its name and its IDs), not by its area alone. Reply with JSON only: "
    '{"category": "NN" or "none", "confidence": 0.0-1.0, "reason": "at most 20 words, in English"}.'
)


def category_question(topic: str, summary: str, categories: str) -> str:
    return (
        f"CATEGORIES:\n{categories}\n\nNEW TOPIC: {topic}\nFILE: {summary}\n\n"
        'Which category fits this topic by its own theme? Use "none" if no category fits.'
    )


# The focused questions need a few words, not an essay: a model that is unsure can write on in
# "reason" until the reply is cut off, and a cut-off reply is not valid JSON.
FOCUSED_MAX_TOKENS = 300


def focused_answer(text: str, *keys: str) -> dict[str, Any]:
    """The JSON object of a focused answer, or what can be read of one that was cut off.

    The fields that matter come before "reason" in every focused answer, so
    a reply cut off in the middle of its reason still holds them.
    """
    try:
        return extract_json_object(text)
    except (ValueError, json.JSONDecodeError):
        found: dict[str, Any] = {}
        for key in keys:
            m = re.search(rf'"{key}"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(-?\d+(?:\.\d+)?))', text or "")
            if m and m.group(1) is not None:
                try:
                    found[key] = json.loads(f'"{m.group(1)}"')
                except json.JSONDecodeError:
                    found[key] = m.group(1)
            elif m:
                found[key] = float(m.group(2))
        if not found:
            raise
        found.setdefault("reason", "(the reply was cut off)")
        return found


def parse_category_answer(text: str) -> tuple[str, float, str]:
    data = focused_answer(text, "category", "confidence")
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    raw = str(data.get("category") or "none").strip()
    m = re.match(r"\d{2}", raw)
    return (m.group(0) if m else "none"), confidence, str(data.get("reason") or "")[:300]


NEW_ID_SYSTEM = (
    "A file got an answer that names no existing Johnny.Decimal ID, so it gets a new ID of its own. You only "
    "name it: a short topic of one to four words, in the same language and style as the IDs listed. The name "
    "is the subject that more files like this one will share (a person, an organisation, a project, a device, "
    "an event). It is never a file name, a number, a date, the name of a category or the kind of file (\"photo\", \"log\"). If the "
    "user's rules say what such files are to be grouped under, name it after that: one rule, one ID, so "
    "use the rule's subject and not this file's own details. If an ID listed already is exactly this "
    "topic, answer its name as it is listed and the file joins it. Do not write a number. "
    'Reply with JSON only: {"name": "the name of the new ID", "reason": "at most 20 words, in English"}.'
)


def new_id_question(answer: str, label: str, summary: str, categories: str, rules: str = "") -> str:
    return (
        f"CATEGORIES AND THEIR IDS:\n{categories}\n\n"
        + (f"THE USER'S RULES:\n{rules}\n\n" if rules.strip() else "")
        + f"FILE ({label or 'file'}): {summary}\nEARLIER ANSWER, NOT AN EXISTING ID: {answer or '(nothing)'}\n\n"
        "What should the new ID for this file be called?"
    )


def parse_new_id_answer(text: str) -> tuple[str, str]:
    """(name for the new ID, reason)."""
    data = focused_answer(text, "name")
    return str(data.get("name") or "")[:120], str(data.get("reason") or "")[:300]


NEW_CATEGORY_SYSTEM = (
    "No existing Johnny.Decimal category fits a new topic, so a new category may be created for it. Choose "
    "the existing area it belongs in and name the category: the broader theme that this topic and similar "
    "later topics share, one to three words, in the same language and style as the categories listed. It "
    "must not repeat or overlap an existing category, and it is not the topic's own name unless nothing "
    "broader makes sense. The topic itself becomes an ID inside the new category, so a rule that asks for an "
    "ID of its own is already met: you only place and name the category. The area must be one of the areas "
    "listed, written as it is listed. Never write a number for the category: the next free number of the "
    "area is used. If no area fits, or the area is marked FULL, or you cannot name a sensible category, "
    "answer area \"none\". Reply "
    'with JSON only: {"area": "NN-NN" or "none", "category_name": "the name of the new category", '
    '"confidence": 0.0-1.0, "reason": "at most 20 words, in English"}.'
)


def new_category_question(topic: str, summary: str, areas: str, rules: str = "") -> str:
    return (
        f"AREAS AND THEIR CATEGORIES:\n{areas}\n\n"
        + (f"THE USER'S RULES:\n{rules}\n\n" if rules.strip() else "")
        + f"NEW TOPIC: {topic}\nFILE: {summary}\n\n"
        "Which area does a new category for this topic belong in, and what is the category called?"
    )


def parse_new_category_answer(text: str) -> tuple[str, str, float, str]:
    """(area "NN-NN" or "none", category name, confidence, reason)."""
    data = focused_answer(text, "area", "category_name", "confidence")
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    m = re.search(r"\d{2}-\d{2}", str(data.get("area") or ""))
    name = str(data.get("category_name") or "")[:120]
    return (m.group(0) if m else "none"), name, confidence, str(data.get("reason") or "")[:300]


class OpenAICompatClient:
    """Chat-completions client for a local server (Ollama, llama.cpp, LM Studio).

    Refuses non-loopback URLs, ignores proxy environment variables, and turns
    off model "thinking" (``reasoning_effort: none``) so a reasoning model such
    as Qwen 3.6 spends its budget on the JSON answer instead of hidden thoughts.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.6,
        top_p: float | None = 0.95,
        max_tokens: int = 800,
        timeout_sec: float = 1800.0,
        reasoning_effort: str = "none",
        max_retries: int = 1,
    ):
        self.base_url = ensure_local_url(base_url.rstrip("/"))
        self.model = model
        self.api_key = api_key or "local"
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.timeout_sec = timeout_sec
        self.reasoning_effort = reasoning_effort
        self.max_retries = max(0, int(max_retries))
        self.last_latency_s: float | None = None
        self.last_error: str | None = None
        self.last_tokens_est: int = 0
        self._send_reasoning = bool(reasoning_effort)
        self._send_images = True

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _client(self, timeout: httpx.Timeout) -> httpx.Client:
        # trust_env=False: never route through HTTP(S)_PROXY to another host.
        return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)

    def _post(self, payload: dict[str, Any], *, use_json_format: bool) -> str:
        body = dict(payload)
        if use_json_format:
            body["response_format"] = {"type": "json_object"}
        if self._send_reasoning:
            body["reasoning_effort"] = self.reasoning_effort
        url = f"{self.base_url}/chat/completions"
        timeout = httpx.Timeout(self.timeout_sec, connect=5.0)
        with self._client(timeout) as client:
            resp = client.post(url, headers=self._headers(), json=body)
        if resp.status_code == 400 and self._send_reasoning and "reasoning" in resp.text.lower():
            self._send_reasoning = False
            return self._post(payload, use_json_format=use_json_format)
        if resp.status_code in (400, 500) and _has_images(payload) and self._send_images:
            # Text-only model or server: keep going on EXIF and metadata alone.
            self._send_images = False
            self.last_error = f"images rejected by the server: {resp.text[:200]}"
            return self._post(_strip_images(payload), use_json_format=use_json_format)
        if resp.status_code == 400 and use_json_format:
            return self._post(payload, use_json_format=False)
        if resp.status_code >= 400:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            message = data["choices"][0]["message"]
            content = str(message.get("content") or "")
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(f"unexpected LLM response shape: {data!r}"[:400]) from e
        if not content.strip() and message.get("reasoning"):
            raise LLMError(
                "model spent its whole token budget thinking; raise max_tokens "
                "or keep reasoning_effort = \"none\""
            )
        return content

    def _complete(
        self, messages: list[dict[str, Any]], *, temperature: float | None = None, max_tokens: int | None = None
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": messages,
        }
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                t0 = time.monotonic()
                text = self._post(payload, use_json_format=True)
                self.last_latency_s = time.monotonic() - t0
                self.last_error = None
                return text
            except (httpx.HTTPError, LLMError, ValueError) as e:
                last_err = e
                self.last_error = str(e)
                if attempt < self.max_retries:
                    time.sleep(min(8.0, 2**attempt))
        raise LLMError(str(last_err) if last_err else "LLM request failed")

    def warm_up(self, system_prompt: str) -> tuple[bool, str]:
        """Load the model and put *system_prompt* in the server's prompt cache.

        One tiny request with the same system prompt as every real one: the
        model loads while sorto is still scanning and hashing, and the first
        file only pays for its own packet instead of the whole outline.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": 1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Reply with {}"},
            ],
        }
        t0 = time.monotonic()
        try:
            self._post(payload, use_json_format=False)
        except (httpx.HTTPError, LLMError, ValueError) as e:
            return False, f"not ready: {e}"[:300]
        return True, f"loaded and prompt cached in {time.monotonic() - t0:.1f}s"

    def classify(self, packet: AnalysisPacket, system_prompt: str) -> Classification:
        user = packet_user_message(packet, rules=SECTION_TITLE in system_prompt)
        images = packet.images if self._send_images else []
        self.last_tokens_est = (
            estimate_tokens(system_prompt) + estimate_tokens(user) + IMAGE_TOKENS_EST * len(images)
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content(user, images)},
        ]
        text = self._complete(messages)
        try:
            return parse_classification(text)
        except (ValueError, json.JSONDecodeError):
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": REPAIR_USER})
            text2 = self._complete(messages)
            try:
                return parse_classification(text2)
            except (ValueError, json.JSONDecodeError) as e:
                raise LLMParseError(f"invalid JSON after repair: {text2[:300]}") from e

    def classify_folder(self, packet: Any, system_prompt: str) -> Any:
        """One answer for a whole folder (same system prompt, so the cached outline is reused)."""
        from sorto.folders import FOLDER_QUESTION, parse_folder_answer

        body = json.dumps(packet.to_llm_dict(), ensure_ascii=False)[:14000]
        rules = f"\n{RULES_REMINDER}" if SECTION_TITLE in system_prompt else ""
        user = f"{FOLDER_QUESTION}{body}\n{rules}\n{LANGUAGE_REMINDER}"
        images = packet.images if self._send_images else []
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content(user, images)},
        ]
        # A folder decision moves many files at once: make it repeatable.
        text = self._complete(messages, temperature=0.0)
        try:
            return parse_folder_answer(text)
        except (ValueError, json.JSONDecodeError):
            messages += [{"role": "assistant", "content": text}, {"role": "user", "content": REPAIR_USER}]
            try:
                return parse_folder_answer(self._complete(messages, temperature=0.0))
            except (ValueError, json.JSONDecodeError) as e:
                raise LLMParseError(f"folder answer was not valid JSON: {e}") from e

    def propose_structure(self, survey: str, rules: str = "") -> dict[str, Any]:
        """A Johnny.Decimal structure for a tree that has none (sorto renumbers it)."""
        from sorto.bootstrap import STRUCTURE_SYSTEM

        user = f"THE FILES:\n{survey}\n"
        if rules:
            user += f"\nTHE USER'S RULES:\n{rules}\n"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": STRUCTURE_SYSTEM},
            {"role": "user", "content": user + "\nReply with the JSON object only."},
        ]
        text = self._complete(messages, temperature=0.0, max_tokens=max(self.max_tokens, 3000))
        try:
            return extract_json_object(text)
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMParseError(f"structure answer was not valid JSON: {e}") from e

    def choose_category(self, topic: str, summary: str, categories: str) -> tuple[str, float, str]:
        """Second, focused question before a new ID is created: which category, if any."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": CATEGORY_SYSTEM},
            {"role": "user", "content": category_question(topic, summary, categories)},
        ]
        try:
            # Deterministic: the same topic should always land in the same category.
            return parse_category_answer(self._complete(messages, temperature=0.0, max_tokens=FOCUSED_MAX_TOKENS))
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMParseError(f"category answer was not valid JSON: {e}") from e

    def name_new_id(
        self, answer: str, label: str, summary: str, categories: str, rules: str = ""
    ) -> tuple[str, str]:
        """Focused question when an answer named neither an existing ID nor a new one's name."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": NEW_ID_SYSTEM},
            {"role": "user", "content": new_id_question(answer, label, summary, categories, rules)},
        ]
        try:
            return parse_new_id_answer(self._complete(messages, temperature=0.0, max_tokens=FOCUSED_MAX_TOKENS))
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMParseError(f"new ID answer was not valid JSON: {e}") from e

    def invent_category(self, topic: str, summary: str, areas: str, rules: str = "") -> tuple[str, str, float, str]:
        """Last question before giving up on a new ID: a new category in an existing area, if any."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": NEW_CATEGORY_SYSTEM},
            {"role": "user", "content": new_category_question(topic, summary, areas, rules)},
        ]
        try:
            return parse_new_category_answer(self._complete(messages, temperature=0.0, max_tokens=FOCUSED_MAX_TOKENS))
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMParseError(f"new category answer was not valid JSON: {e}") from e

    def health(self) -> tuple[bool, str]:
        try:
            models = self.list_models()
        except Exception as e:
            return False, str(e)
        if models and self.model and self.model not in models:
            return False, f"reachable, but model {self.model!r} is not pulled ({len(models)} models)"
        return True, f"ok ({len(models)} models)"

    def _native_url(self, path: str) -> str:
        """Ollama's own API next to its OpenAI-compatible /v1."""
        parsed = urlparse(self.base_url)
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

    def server_context_window(self) -> int | None:
        """num_ctx baked into an Ollama model tag (e.g. a 16k tag of a small model)."""
        try:
            with self._client(httpx.Timeout(8.0, connect=3.0)) as client:
                r = client.post(self._native_url("/api/show"), json={"model": self.model})
            if r.status_code >= 400:
                return None
            for line in str(r.json().get("parameters") or "").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] == "num_ctx":
                    return int(parts[1])
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        return None

    def loaded_models(self) -> dict[str, dict[str, Any]]:
        """Models the Ollama server holds in memory right now (``/api/ps``)."""
        try:
            with self._client(httpx.Timeout(8.0, connect=3.0)) as client:
                r = client.get(self._native_url("/api/ps"))
            if r.status_code >= 400:
                return {}
            return {str(m.get("name")): m for m in r.json().get("models") or []}
        except (httpx.HTTPError, ValueError, TypeError):
            return {}

    def list_models(self) -> list[str]:
        timeout = httpx.Timeout(8.0, connect=3.0)
        with self._client(timeout) as client:
            r = client.get(f"{self.base_url}/models", headers=self._headers())
        if r.status_code >= 400:
            # Ollama native listing
            parsed = urlparse(self.base_url)
            native = urlunparse((parsed.scheme, parsed.netloc, "/api/tags", "", "", ""))
            with self._client(timeout) as client:
                r2 = client.get(native)
            if r2.status_code < 400:
                data = r2.json()
                return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            raise LLMError(f"models HTTP {r.status_code}")
        data = r.json()
        return [str(item["id"]) for item in data.get("data") or [] if item.get("id")]


class FakeLLMClient:
    """Deterministic classifier for tests: picks an ID by keyword in the filename."""

    def __init__(
        self,
        handler=None,
        *,
        invalid: bool = False,
        needs_user: bool = False,
        routes: dict[str, str] | None = None,
        default_id: str = "",
        folder_handler=None,
        structure: dict[str, Any] | None = None,
    ):
        self.handler = handler
        self.folder_handler = folder_handler
        self.structure = structure
        self.surveys: list[str] = []
        self.folder_calls: list[Any] = []
        self.invalid = invalid
        self.needs_user = needs_user
        self.routes = routes or {}
        self.default_id = default_id
        self.calls: list[AnalysisPacket] = []
        self.last_latency_s: float = 0.0
        self.last_error: str | None = None
        self.last_tokens_est: int = 32
        self.model = "fake"

    def classify(self, packet: AnalysisPacket, system_prompt: str) -> Classification:
        self.calls.append(packet)
        if self.invalid:
            raise LLMParseError("invalid JSON after repair: not-json")
        if self.handler:
            return self.handler(packet, system_prompt)
        name = packet.filename.lower()
        jd_id = next((v for k, v in self.routes.items() if k in name), self.default_id)
        return Classification(
            label=packet.extension.lstrip(".") or "file",
            confidence=0.0 if (self.needs_user or not jd_id) else 0.9,
            jd_id=jd_id,
            subfolder="",
            new_filename="",
            summary=f"A {packet.extension or 'extension-less'} file named {packet.filename}.",
            reason="fake classifier matched a filename keyword" if jd_id else "no match",
            needs_user=self.needs_user,
            raw="{}",
        )

    def classify_folder(self, packet: Any, system_prompt: str) -> Any:
        """Tests decide folders with folder_handler; by default every folder is mixed."""
        from sorto.folders import FolderAnswer

        self.folder_calls.append(packet)
        if self.folder_handler:
            return self.folder_handler(packet, system_prompt)
        return FolderAnswer(summary="", coherent=False, jd_id="", subfolder="", confidence=0.0, reason="fake")

    def propose_structure(self, survey: str, rules: str = "") -> dict[str, Any]:
        self.surveys.append(survey)
        return self.structure or {"areas": []}

    def health(self) -> tuple[bool, str]:
        return True, "fake"

    def list_models(self) -> list[str]:
        return ["fake"]
