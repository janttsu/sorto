"""Client for the Varsto MCP server (`varsto mcp`).

Varsto keeps the files of your vault in folders on your devices and offers
them to an assistant through the Model Context Protocol: JSON-RPC 2.0 over the
standard input and output of `varsto mcp`. sorto uses it to record what each
file contains and to file the files into the Johnny.Decimal structure of a
Varsto folder. Nothing here reads or writes file contents itself; Varsto
applies every change, and it refuses what its access rules do not allow.

The key is read from the environment (`VARSTO_MCP_KEY`) by the `varsto`
process; sorto never stores it and never prints it.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any

from sorto import __version__

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_COMMAND = ("varsto", "mcp")


class VarstoError(RuntimeError):
    """Varsto could not do what was asked (not running, refused, or no answer)."""


class VarstoMcp:
    """One connection to `varsto mcp`. Use as a context manager."""

    def __init__(
        self,
        command: tuple[str, ...] | list[str] | None = None,
        *,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = list(command or DEFAULT_COMMAND)
        self.timeout = timeout
        self._env = {**os.environ, **(env or {})}
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._next_id = 0
        self.server_info: dict[str, Any] = {}

    # -- connection ---------------------------------------------------------

    def __enter__(self) -> VarstoMcp:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=self._env,
            )
        except FileNotFoundError as e:
            raise VarstoError(f"the command {self.command[0]!r} was not found") from e
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "sorto", "version": __version__},
            },
        )
        self.server_info = dict(result.get("serverInfo") or {})
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
            proc.wait()

    # -- JSON-RPC -----------------------------------------------------------

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._stderr.append(line.rstrip())

    def _write(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise VarstoError("not connected")
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise VarstoError("varsto mcp stopped: " + self._last_error()) from e

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise VarstoError(f"no answer to {method} within {self.timeout:.0f} s")
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                raise VarstoError(f"no answer to {method} within {self.timeout:.0f} s") from None
            if line is None:
                raise VarstoError("varsto mcp stopped: " + self._last_error())
            try:
                message = json.loads(line)
            except json.JSONDecodeError as e:
                raise VarstoError(f"{method}: the answer is not JSON") from e
            if message.get("id") != request_id:
                continue  # a notification, or an answer to something else
            if "error" in message:
                detail = (message["error"] or {}).get("message", "error")
                raise VarstoError(f"{method}: {detail}")
            return dict(message.get("result") or {})

    def _last_error(self) -> str:
        return self._stderr[-1] if self._stderr else "no message"

    # -- tools --------------------------------------------------------------

    def tool_names(self) -> list[str]:
        result = self._request("tools/list", {})
        return [t["name"] for t in result.get("tools", []) if "name" in t]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Run a tool. JSON answers are parsed; plain text is returned as it is."""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "".join(
            c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
        )
        if result.get("isError"):
            raise VarstoError(f"{name}: {text.strip() or 'refused'}")
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            return json.loads(stripped)
        return text

    def folders(self) -> list[dict[str, Any]]:
        """The folders this key may see: name, access (ro, analyse or rw), local_path."""
        return list(self.call("varsto_folders"))

    def files(self, folder: str, *, with_analysis: bool = True) -> list[dict[str, Any]]:
        return list(
            self.call("varsto_files", {"folder": folder, "with_analysis": with_analysis})
        )

    def set_analysis(
        self, folder: str, path: str, analysis: str, tags: list[str] | None = None, by: str | None = None
    ) -> Any:
        args: dict[str, Any] = {"folder": folder, "path": path, "analysis": analysis}
        if tags:
            args["tags"] = tags
        if by:
            args["by"] = by
        return self.call("varsto_set_analysis", args)

    def write(self, folder: str, path: str, text: str) -> Any:
        """Create or replace a text file; a replaced version stays in Varsto's history."""
        return self.call("varsto_write", {"folder": folder, "path": path, "text": text})

    def mkdir(self, folder: str, path: str) -> Any:
        return self.call("varsto_mkdir", {"folder": folder, "path": path})

    def move(self, folder: str, src: str, dst: str) -> Any:
        return self.call("varsto_move", {"folder": folder, "from": src, "to": dst})

    def rmdir(self, folder: str, path: str) -> Any:
        return self.call("varsto_rmdir", {"folder": folder, "path": path})


__all__ = ["VarstoError", "VarstoMcp", "DEFAULT_COMMAND", "PROTOCOL_VERSION"]
