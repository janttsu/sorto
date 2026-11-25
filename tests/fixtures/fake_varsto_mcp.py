"""A stand-in for `varsto mcp` with invented data, for the sorto client tests."""

import json
import sys

FOLDERS = [
    {"name": "archive", "access": "rw", "local_path": "/example/archive"},
    {"name": "inbox-demo", "access": "analyse", "local_path": None},
]


def answer(msg):
    method = msg.get("method")
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "serverInfo": {"name": "varsto", "version": "0.0.0-test"}}
    if method == "tools/list":
        return {"tools": [{"name": "varsto_folders"}, {"name": "varsto_write"}]}
    if method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments", {})
        if name == "varsto_folders":
            text = json.dumps(FOLDERS)
        elif name == "varsto_write" and args.get("text") == "refuse":
            return {"content": [{"type": "text", "text": "folder is read-only"}], "isError": True}
        elif name == "varsto_write":
            text = "ok"
        else:
            text = "unknown"
        return {"content": [{"type": "text", "text": text}], "isError": False}
    return {}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue  # notifications get no answer
        if mode == "silent" and msg.get("method") == "tools/list":
            continue  # never answers: the client must time out
        if mode == "error" and msg.get("method") == "tools/list":
            out = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "vault is locked"}}
        else:
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": answer(msg)}
        print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
