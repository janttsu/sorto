from __future__ import annotations

import json
import time
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

import httpx

from sorto.models import AnalysisPacket, Classification
from sorto.util import estimate_tokens, extract_json_object

REPAIR_USER = (
    "Your previous reply was not valid JSON matching the required schema. "
    "Reply with ONLY a JSON object with keys: label, confidence, dest_rel, "
    "rename, reason, needs_user. No markdown, no extra text."
)


class LLMError(RuntimeError):
    pass


class LLMParseError(LLMError):
    pass


class LLMClient(Protocol):
    def classify(self, packet: AnalysisPacket, system_prompt: str) -> Classification: ...

    def health(self) -> tuple[bool, str]: ...

    def list_models(self) -> list[str]: ...


def parse_classification(text: str) -> Classification:
    data = extract_json_object(text)
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    return Classification(
        label=str(data.get("label") or "unknown")[:80],
        confidence=confidence,
        dest_rel=str(data.get("dest_rel") or ""),
        rename=bool(data.get("rename", False)),
        reason=str(data.get("reason") or "")[:500],
        needs_user=bool(data.get("needs_user", False)),
        raw=text,
    )


def packet_user_message(packet: AnalysisPacket) -> str:
    body = json.dumps(packet.to_llm_dict(), ensure_ascii=False, indent=None)
    folders = "\n".join(f"- {f}" for f in packet.top_level_folders[:80]) or "- (none yet)"
    msg = (
        f"Classify this file. Destination scheme: {packet.dest_scheme}\n"
        f"Existing top-level folders:\n{folders}\n\n"
        f"FILE PACKET:\n{body}\n"
    )
    # Hard cap ~4k tokens of packet context.
    max_chars = 4 * 4000
    if len(msg) > max_chars:
        msg = msg[: max_chars - 20] + "\n…[truncated]"
    return msg


class OpenAICompatClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.1,
        max_tokens: int = 400,
        timeout_sec: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or "local"
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_sec = timeout_sec
        self.last_latency_s: float | None = None
        self.last_error: str | None = None
        self.last_tokens_est: int = 0

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, payload: dict[str, Any], *, use_json_format: bool) -> str:
        body = dict(payload)
        if use_json_format:
            body["response_format"] = {"type": "json_object"}
        url = f"{self.base_url}/chat/completions"
        timeout = httpx.Timeout(self.timeout_sec, connect=5.0)
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=self._headers(), json=body)
        if resp.status_code == 400 and use_json_format:
            return self._post(payload, use_json_format=False)
        if resp.status_code >= 400:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected LLM response shape: {data!r}"[:400]) from e

    def _complete(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                t0 = time.monotonic()
                text = self._post(payload, use_json_format=True)
                self.last_latency_s = time.monotonic() - t0
                self.last_error = None
                return text
            except (httpx.HTTPError, LLMError, ValueError) as e:
                last_err = e
                self.last_error = str(e)
                time.sleep(min(8.0, 2**attempt))
        raise LLMError(str(last_err) if last_err else "LLM request failed")

    def classify(self, packet: AnalysisPacket, system_prompt: str) -> Classification:
        user = packet_user_message(packet)
        self.last_tokens_est = estimate_tokens(system_prompt) + estimate_tokens(user)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
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

    def health(self) -> tuple[bool, str]:
        try:
            models = self.list_models()
            if models:
                if self.model and self.model not in models:
                    return True, f"reachable; model {self.model!r} not in listing ({len(models)} models)"
                return True, f"ok ({len(models)} models)"
            return True, "reachable (empty model list)"
        except Exception as e:
            # Try a trivial chat ping
            try:
                timeout = httpx.Timeout(5.0, connect=3.0)
                with httpx.Client(timeout=timeout) as client:
                    r = client.get(f"{self.base_url}/models", headers=self._headers())
                if r.status_code < 500:
                    return True, f"endpoint responded HTTP {r.status_code}"
                return False, f"HTTP {r.status_code}"
            except Exception:
                return False, str(e)

    def list_models(self) -> list[str]:
        timeout = httpx.Timeout(8.0, connect=3.0)
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{self.base_url}/models", headers=self._headers())
        if r.status_code >= 400:
            # Ollama native
            parsed = urlparse(self.base_url)
            native = urlunparse((parsed.scheme, parsed.netloc, "/api/tags", "", "", ""))
            try:
                with httpx.Client(timeout=timeout) as client:
                    r2 = client.get(native)
                if r2.status_code < 400:
                    data = r2.json()
                    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            except Exception:
                pass
            raise LLMError(f"models HTTP {r.status_code}")
        data = r.json()
        out = []
        for item in data.get("data") or []:
            mid = item.get("id")
            if mid:
                out.append(str(mid))
        return out


class FakeLLMClient:
    """Deterministic classifier for tests and --offline demos."""

    def __init__(self, handler=None, *, invalid: bool = False, needs_user: bool = False):
        self.handler = handler
        self.invalid = invalid
        self.needs_user = needs_user
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
        ext = (packet.extension or "").lower()
        folder = {
            ".pdf": "documents",
            ".txt": "documents",
            ".md": "documents",
            ".doc": "documents",
            ".docx": "documents",
            ".xls": "spreadsheets",
            ".xlsx": "spreadsheets",
            ".csv": "spreadsheets",
            ".ppt": "presentations",
            ".pptx": "presentations",
            ".jpg": "images/photos",
            ".jpeg": "images/photos",
            ".png": "images/screenshots" if "screen" in packet.filename.lower() else "images/photos",
            ".gif": "images/photos",
            ".mp4": "video",
            ".mkv": "video",
            ".mp3": "audio",
            ".wav": "audio",
            ".zip": "archives",
            ".tar": "archives",
            ".gz": "archives",
            ".7z": "archives",
            ".py": "code",
            ".rs": "code",
            ".js": "code",
            ".stl": "3d_and_cad",
            ".epub": "ebooks",
        }.get(ext, "_unsorted")
        dest = f"{folder}/{packet.filename}"
        if packet.duplicate_of:
            dest = f"_duplicates_candidates/{packet.filename}"
        return Classification(
            label=folder.split("/")[-1],
            confidence=0.0 if self.needs_user else 0.9,
            dest_rel=dest,
            rename=packet.meaningless_name,
            reason="fake classifier used extension mapping",
            needs_user=self.needs_user,
            raw="{}",
        )

    def health(self) -> tuple[bool, str]:
        return True, "fake"

    def list_models(self) -> list[str]:
        return ["fake"]
