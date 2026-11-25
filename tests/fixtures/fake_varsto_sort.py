"""A stand-in for `varsto mcp` on a plain folder, with invented data.

Usage: fake_varsto_sort.py ROOT FOLDER ACCESS [PLACEHOLDER_PATH ...]
Works on the real files under ROOT: moves never replace, rmdir only removes
an empty folder, and writes need an rw access level (as in Varsto).
"""

import json
import os
import sys

ROOT, FOLDER, ACCESS = sys.argv[1], sys.argv[2], sys.argv[3]
PLACEHOLDERS = set(sys.argv[4:])
ANALYSES = {}


def files():
    out = []
    for dirpath, _dirs, names in os.walk(ROOT):
        for n in names:
            if n == ".varsto-folder":
                continue
            out.append(os.path.relpath(os.path.join(dirpath, n), ROOT).replace(os.sep, "/"))
    return sorted(out)


def target(rel):
    if os.path.isabs(rel) or ".." in rel.split("/") or not rel:
        raise ValueError("path leaves the folder")
    return os.path.join(ROOT, rel)


def ok(text="ok"):
    return {"content": [{"type": "text", "text": text}], "isError": False}


def refused(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def call(name, args):
    writes = {"varsto_set_analysis", "varsto_mkdir", "varsto_move", "varsto_rmdir", "varsto_write"}
    if name in writes and ACCESS != "rw":
        return refused(f"folder {FOLDER} is read-only for the assistant")
    if name == "varsto_folders":
        return ok(json.dumps([{"name": FOLDER, "access": ACCESS, "local_path": ROOT}]))
    if name == "varsto_files":
        rows = [
            {
                "path": p,
                "state": "placeholder" if p in PLACEHOLDERS else "local",
                "local_path": None if p in PLACEHOLDERS else os.path.join(ROOT, p),
                "analysis": ANALYSES.get(p),
            }
            for p in files()
        ]
        return ok(json.dumps(rows))
    if name == "varsto_set_analysis":
        if args.get("analysis") == "refuse":
            return refused("analysis refused by the test")
        ANALYSES[args["path"]] = {"analysis": args["analysis"], "tags": args.get("tags", [])}
        return ok()
    try:
        if name == "varsto_mkdir":
            os.makedirs(target(args["path"]), exist_ok=True)
            return ok()
        if name == "varsto_move":
            src, dst = target(args["from"]), target(args["to"])
            if os.path.lexists(dst):
                return refused(f"{args['to']} already exists")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
            return ok()
        if name == "varsto_rmdir":
            d = target(args["path"])
            if os.path.abspath(d) == os.path.abspath(ROOT):
                return refused("the folder itself is not removed")
            os.rmdir(d)  # fails when the folder is not empty
            return ok()
        if name == "varsto_write":
            dst = target(args["path"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(args["text"])
            return ok()
    except (OSError, ValueError) as e:
        return refused(str(e))
    return refused(f"unknown tool {name}")


def main():
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method = msg.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "varsto", "version": "test"},
            }
        elif method == "tools/list":
            result = {"tools": []}
        elif method == "tools/call":
            result = call(msg["params"]["name"], msg["params"].get("arguments", {}))
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
