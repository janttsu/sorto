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


def classification_schema(ids: list[str], categories: list[str]) -> dict[str, Any]:
    """The answer's JSON schema, for a server that constrains decoding to it (Ollama's ``format``).

    ``jd_id`` can only be an ID of the outline or "new", and ``category`` only
    one of its categories: an ID the model makes up cannot even be written.
    The properties are in the order the model is meant to decide them in.
    """
    text = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "summary": text,
            "label": text,
            "rule": text,
            "own_media": {"type": "boolean"},
            "category": {"type": "string", "enum": [*categories, "none"]},
            "jd_id": {"type": "string", "enum": [*ids, "new"]},
            "new_id_category": text,
            "new_id_name": text,
            "subfolder": text,
            "new_filename": text,
            "confidence": {"type": "number"},
            "reason": text,
            "needs_user": {"type": "boolean"},
        },
        "required": ["summary", "label", "rule", "own_media", "category", "jd_id", "new_id_category",
                     "new_id_name", "subfolder", "new_filename", "confidence", "reason", "needs_user"],
    }


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
    "an event). Johnny.Decimal IDs are broad: name the kind of thing the ID will hold, either descriptively "
    "(\"Electricity, gas and water\") or simply (\"Moving house\"); a single trip, project or model is a "
    "subfolder of such an ID, so for one trip name the ID after trips, not after that one trip. "
    "It is never a file name, a number, a date, the name of a category or the kind of file (\"photo\", \"log\"). If the "
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


EXISTING_HOME_SYSTEM = (
    "A topic is about to get a new Johnny.Decimal ID of its own in the category shown. Before that, check "
    "whether one of the category's existing IDs already holds this kind of file, so the topic fits as a "
    "folder named after it inside that ID. A new ID is right only when the topic is a different kind of "
    "thing from what every listed ID holds; one more project, model, item or person of a kind an ID already "
    "holds belongs in that ID. Johnny.Decimal IDs are broad on purpose: one ID such as \"All short trips\" holds "
    "a lifetime of trips, each in its own dated subfolder. Judge by each ID's name, description and subfolders. If the user's rules say "
    "in so many words that this topic gets an ID of its own, answer \"none\". A rule that only asks for "
    "folders named after what the files are is met by a folder inside the existing ID. Reply with JSON only: "
    '{"id": "NN.NN" or "none", "confidence": 0.0-1.0, "reason": "at most 20 words, in English"}.'
)


def existing_home_question(topic: str, summary: str, category: str, ids: str, rules: str = "") -> str:
    return (
        f"CATEGORY: {category}\nITS IDS:\n{ids}\n\n"
        + (f"THE USER'S RULES:\n{rules}\n\n" if rules.strip() else "")
        + f"NEW TOPIC: {topic}\nFILE: {summary}\n\n"
        "Does one of these IDs already hold this kind of file, so the topic fits as a folder inside it?"
    )


def parse_existing_home_answer(text: str) -> tuple[str, float, str]:
    """(an existing ID "NN.NN" or "none", confidence, reason)."""
    data = focused_answer(text, "id", "confidence")
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    m = re.search(r"\d{2}\.\d{2}", str(data.get("id") or ""))
    return (m.group(0) if m else "none"), confidence, str(data.get("reason") or "")[:300]


FSCK_SYSTEM = (
    "You review one category of a Johnny.Decimal archive: its IDs (folders), what the notes say about them, "
    "their subfolders and samples of the files in them. Judge by the files, not only by the names. Answer only "
    "about IDs listed in the question, and only what the evidence clearly shows; empty lists are a fine answer.\n"
    "- descriptions: for each ID marked NO DESCRIPTION that holds files, one short sentence (at most 15 words) "
    "on what it holds, in the same language as the ID names. Not a list of file names.\n"
    "- names: for each NAME DIFFERENCE, which name fits the files: \"folder\" or \"note\".\n"
    "- renames: an ID whose folder name says nothing about what it holds (a number, a file name, a single "
    "date); suggest a short name in the language of the other IDs.\n"
    "- duplicates: IDs, or an ID and another ID's subfolder, that hold the same topic and should be one place. "
    "Johnny.Decimal IDs are broad: several IDs that are each one trip, one event or one model of the same kind "
    "are duplicates of one broad ID with a dated or named subfolder per item.\n"
    "- misplaced: an ID whose files clearly belong to the theme of another category listed (give its number).\n"
    "Reply with JSON only: {\"descriptions\": [{\"id\": \"NN.NN\", \"description\": \"...\"}], "
    "\"names\": [{\"id\": \"NN.NN\", \"right\": \"folder\" or \"note\", \"reason\": \"...\"}], "
    "\"renames\": [{\"id\": \"NN.NN\", \"name\": \"...\", \"reason\": \"...\"}], "
    "\"duplicates\": [{\"ids\": [\"NN.NN\", \"NN.NN or NN.NN/subfolder\"], \"reason\": \"...\"}], "
    "\"misplaced\": [{\"id\": \"NN.NN\", \"category\": \"NN\", \"reason\": \"...\"}]}. "
    "Every reason at most 20 words, in English."
)


FSCK_STRUCTURE_SYSTEM = (
    "You are reviewing the whole structure of a Johnny.Decimal archive: areas (NN-NN), categories (NN) and "
    "IDs (NN.NN), with what each holds and how many files, and what a review of each category found. Think it "
    "through from every angle: overlapping or duplicated topics, categories that are too thin or too crowded, "
    "IDs in the wrong place, names that do not say what a folder holds, numbering that has drifted (gaps, IDs "
    "made one per item where one ID with subfolders would do), and whether a simpler structure, or a bigger "
    "change to the numbering, would serve the owner better. Johnny.Decimal rules: at most 10 areas, at most 10 "
    "categories per area, at most 100 IDs per category; NN.00 holds the category's notes and NN.01 is its inbox. "
    "Johnny.Decimal principles to weigh: prefer fewer, broader categories (overlapping ones such as investments, "
    "budget and savings belong in one, money); a category collects one kind of work; IDs are broad and hold "
    "every item of one kind, each item in a subfolder with a pattern (date first for trips and events, a name "
    "for people and suppliers, or 10 to 90); no ad-hoc, randomly named subfolders; creating an area or a "
    "category deserves friction, creating an ID is cheap; NN.00 to NN.09 are each category's system IDs (.00 "
    "notes, .01 inbox, .09 archive). The owner's own conventions, visible in the tree, win over these "
    "principles: do not propose renumbering just to follow them. "
    "Use the words exactly: an area is NN-NN, a category is NN, an ID is NN.NN; merging IDs is not merging "
    "categories. Never propose a number that is already in use for something else, and never invent one: "
    "for a new ID or category use the number given under FREE NUMBERS, and "
    "name the existing folders by the numbers and names shown. Only folders listed in the tree exist. "
    "Propose only changes the evidence supports, most valuable first; it is fine to propose few or none. "
    "Each proposal: a short title; what to change, concretely, naming the folders; why; the steps; and the "
    "effort (small, medium or large). Write in English. Reply with JSON only: {\"proposals\": [{\"title\": "
    "\"...\", \"change\": \"...\", \"why\": \"...\", \"steps\": [\"...\"], \"effort\": \"small|medium|large\"}]}."
)


NEW_CATEGORY_SYSTEM = (
    "No existing Johnny.Decimal category fits a new topic, so a new category may be created for it. Choose "
    "the existing area it belongs in and name the category: the broader theme that this topic and similar "
    "later topics share. Johnny.Decimal prefers fewer, broader categories, and a new one deserves a moment's "
    "thought: if the topic would fit an existing category after all, answer area \"none\" rather than make an "
    "overlapping one. Name it in one to three words, in the same language and style as the categories listed. It "
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


def _native_message(message: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI-style message in Ollama's own shape: text in ``content``, pictures in ``images``."""
    content = message.get("content")
    if not isinstance(content, list):
        return {"role": message.get("role", "user"), "content": content or ""}
    text = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
    images = []
    for part in content:
        url = (part.get("image_url") or {}).get("url", "") if part.get("type") == "image_url" else ""
        if url.startswith("data:") and "," in url:
            images.append(url.split(",", 1)[1])
    out: dict[str, Any] = {"role": message.get("role", "user"), "content": text}
    if images:
        out["images"] = images
    return out


def keep_alive_value(setting: str) -> int | str | None:
    """What to send as ``keep_alive``: None leaves it to the server.

    "run" keeps the model loaded for as long as sorto runs (sent as -1; when
    sorto stops it sets the timer to ``keep_alive_after``). A bare number is
    seconds, anything else an Ollama duration such as "45m".
    """
    setting = (setting or "").strip().lower()
    if setting in ("", "server", "default"):
        return None
    if setting == "run":
        return -1
    return int(setting) if re.fullmatch(r"-?\d+", setting) else setting


class OpenAICompatClient:
    """Chat client for a local server (Ollama, llama.cpp, LM Studio).

    Refuses non-loopback URLs, ignores proxy environment variables, and turns
    off model "thinking" (``reasoning_effort: none``) so a reasoning model such
    as Qwen 3.6 spends its budget on the JSON answer instead of hidden thoughts.

    An Ollama server is spoken to through its own ``/api/chat``: unlike its
    OpenAI-compatible endpoint, that one honours ``keep_alive`` per request
    and takes ``num_ctx`` / ``num_gpu``. Any other server gets the
    OpenAI-compatible ``/v1/chat/completions``.
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
        api: str = "auto",
        keep_alive: str = "run",
        keep_alive_after: str = "5m",
        num_ctx: int = 0,
        num_gpu: int = -1,
    ):
        self.base_url = ensure_local_url(base_url.rstrip("/"))
        self.model = model
        self.api_key = api_key or "local"
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.timeout_sec = timeout_sec
        self.reasoning_effort = reasoning_effort
        # Set by the engine from the outline (classification_schema): the IDs an answer may name.
        self.answer_schema: dict[str, Any] | None = None
        self.max_retries = max(0, int(max_retries))
        self.last_latency_s: float | None = None
        self.last_error: str | None = None
        self.last_tokens_est: int = 0
        self._send_reasoning = bool(reasoning_effort)
        self._send_images = True
        self.keep_alive = keep_alive
        self.keep_alive_after = keep_alive_after
        self._held_before: bool | None = None  # the model was already loaded for good before sorto asked
        self.num_ctx = max(0, int(num_ctx or 0))  # 0: the model's own context length
        self.num_gpu = int(num_gpu if num_gpu is not None else -1)  # -1: the server decides
        api = (api or "auto").strip().lower()
        # None: not known yet, asked on the first request.
        self._native: bool | None = {"ollama": True, "openai": False}.get(api)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _client(self, timeout: httpx.Timeout) -> httpx.Client:
        # trust_env=False: never route through HTTP(S)_PROXY to another host.
        return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)

    def is_ollama(self) -> bool:
        """The server is Ollama (it answers ``/api/version``). Asked once; a server that is down is asked again."""
        if self._native is None:
            try:
                with self._client(httpx.Timeout(5.0, connect=3.0)) as client:
                    r = client.get(self._native_url("/api/version"))
                self._native = r.status_code == 200 and "version" in r.json()
            except (httpx.HTTPError, ValueError, TypeError):
                return False
        return self._native

    def _post_native(self, payload: dict[str, Any], *, use_json_format: bool) -> str:
        options: dict[str, Any] = {"temperature": payload.get("temperature", self.temperature)}
        if payload.get("top_p") is not None:
            options["top_p"] = payload["top_p"]
        if payload.get("max_tokens"):
            options["num_predict"] = payload["max_tokens"]
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        if self.num_gpu >= 0:
            options["num_gpu"] = self.num_gpu
        body: dict[str, Any] = {
            "model": payload["model"],
            "messages": [_native_message(m) for m in payload.get("messages", [])],
            "stream": False,
            "options": options,
        }
        if use_json_format:
            body["format"] = payload.get("schema") or "json"
        if self._send_reasoning:
            body["think"] = self.reasoning_effort != "none"
        keep = keep_alive_value(self.keep_alive)
        if keep is not None:
            body["keep_alive"] = keep
        if keep == -1 and self._held_before is None:
            self._held_before = self._loaded_for_good()
        timeout = httpx.Timeout(self.timeout_sec, connect=5.0)
        with self._client(timeout) as client:
            resp = client.post(self._native_url("/api/chat"), json=body)
        if resp.status_code == 404 and "model" not in resp.text.lower():
            self._native = False  # something else answers on this port: use the OpenAI-compatible endpoint
            return self._post(payload, use_json_format=use_json_format)
        if resp.status_code == 400 and self._send_reasoning and "think" in resp.text.lower():
            self._send_reasoning = False
            return self._post_native(payload, use_json_format=use_json_format)
        if resp.status_code in (400, 500) and _has_images(payload) and self._send_images:
            self._send_images = False
            self.last_error = f"images rejected by the server: {resp.text[:200]}"
            return self._post_native(_strip_images(payload), use_json_format=use_json_format)
        if resp.status_code >= 400:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            message = data["message"]
            content = str(message.get("content") or "")
        except (KeyError, TypeError, AttributeError) as e:
            raise LLMError(f"unexpected LLM response shape: {data!r}"[:400]) from e
        if not content.strip() and message.get("thinking"):
            raise LLMError(
                "model spent its whole token budget thinking; raise max_tokens "
                "or keep reasoning_effort = \"none\""
            )
        try:
            self.last_tokens_est = int(data.get("prompt_eval_count") or 0) + int(data.get("eval_count") or 0)
        except (TypeError, ValueError):
            pass
        return content

    def _loaded_for_good(self) -> bool:
        """The server already holds this model with no unload time (someone set it to stay loaded)."""
        expires = str(self.loaded_models().get(self.model, {}).get("expires_at") or "")
        try:
            return int(expires[:4]) > time.gmtime().tm_year + 1
        except ValueError:
            return False

    def release(self) -> None:
        """A run is over: stop holding the model. It unloads ``keep_alive_after`` from now.

        Only for ``keep_alive = "run"``. Ollama keeps the last keep_alive it
        was given for a loaded model (a request without one does not bring
        the server's default back; tested), so the timer has to be set to a
        real value. A model that was loaded for good before sorto used it is
        left that way, and one that is not loaded any more is not touched, so
        stopping sorto never loads a model.
        """
        if keep_alive_value(self.keep_alive) != -1 or not self._native or self._held_before:
            return
        after = keep_alive_value(self.keep_alive_after)
        if after is None:
            after = "5m"  # Ollama's own default
        if after == -1:
            return
        try:
            if self.model not in self.loaded_models():
                return
            with self._client(httpx.Timeout(15.0, connect=3.0)) as client:
                client.post(self._native_url("/api/generate"), json={"model": self.model, "keep_alive": after})
        except (httpx.HTTPError, ValueError, TypeError):
            pass

    def _post(self, payload: dict[str, Any], *, use_json_format: bool) -> str:
        if self.is_ollama():
            return self._post_native(payload, use_json_format=use_json_format)
        body = {k: v for k, v in payload.items() if k != "schema"}  # the schema is for Ollama's own API
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
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        schema: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": messages,
        }
        if schema:
            payload["schema"] = schema
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
        text = self._complete(messages, schema=self.answer_schema)
        try:
            return parse_classification(text)
        except (ValueError, json.JSONDecodeError):
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": REPAIR_USER})
            text2 = self._complete(messages, schema=self.answer_schema)
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

    def existing_home(
        self, topic: str, summary: str, category: str, ids: str, rules: str = ""
    ) -> tuple[str, float, str]:
        """Last check before a new ID in a category: would a folder in one of its IDs do?"""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": EXISTING_HOME_SYSTEM},
            {"role": "user", "content": existing_home_question(topic, summary, category, ids, rules)},
        ]
        try:
            return parse_existing_home_answer(
                self._complete(messages, temperature=0.0, max_tokens=FOCUSED_MAX_TOKENS)
            )
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMParseError(f"existing ID answer was not valid JSON: {e}") from e

    def review_category(self, question: str) -> dict[str, Any]:
        """``sorto fsck``: one category's IDs, notes and file samples reviewed; see FSCK_SYSTEM."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": FSCK_SYSTEM},
            {"role": "user", "content": question + "\n\nReply with the JSON object only."},
        ]
        text = self._complete(messages, temperature=0.0, max_tokens=max(self.max_tokens, 2000))
        try:
            return extract_json_object(text)
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMParseError(f"review answer was not valid JSON: {e}") from e

    def review_structure(self, question: str) -> dict[str, Any]:
        """``sorto fsck``: the whole tree reviewed once, with the model thinking it through; see FSCK_STRUCTURE_SYSTEM."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": FSCK_STRUCTURE_SYSTEM},
            {"role": "user", "content": question + "\n\nReply with the JSON object only."},
        ]
        saved = (self.reasoning_effort, self._send_reasoning)
        self.reasoning_effort, self._send_reasoning = "high", True  # this one question is worth the thinking
        try:
            text = self._complete(messages, temperature=0.3, max_tokens=12000)
        finally:
            self.reasoning_effort, self._send_reasoning = saved
        try:
            return extract_json_object(text)
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMParseError(f"structure review was not valid JSON: {e}") from e

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

    def model_catalog(self) -> list[dict[str, Any]]:
        """Every model the server offers: name, and from Ollama also size, parameters, quantization, loaded."""
        if self.is_ollama():
            timeout = httpx.Timeout(8.0, connect=3.0)
            with self._client(timeout) as client:
                tags = client.get(self._native_url("/api/tags"))
                ps = client.get(self._native_url("/api/ps"))
            if tags.status_code < 400:
                loaded = {m.get("name") for m in (ps.json().get("models") or [])} if ps.status_code < 400 else set()
                out = []
                for m in tags.json().get("models") or []:
                    details = m.get("details") or {}
                    out.append({
                        "name": m.get("name", ""), "size": int(m.get("size") or 0),
                        "parameters": str(details.get("parameter_size") or ""),
                        "quantization": str(details.get("quantization_level") or ""),
                        "loaded": m.get("name") in loaded,
                    })
                return sorted((m for m in out if m["name"]), key=lambda m: m["name"])
        return [{"name": n, "size": 0, "parameters": "", "quantization": "", "loaded": False}
                for n in sorted(self.list_models())]

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

    def model_catalog(self) -> list[dict[str, Any]]:
        return [{"name": "fake", "size": 0, "parameters": "", "quantization": "", "loaded": False}]
