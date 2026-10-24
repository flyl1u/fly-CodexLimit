"""Subprocess fixture, never imports or invokes Codex."""
import json
import sys

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if "id" not in msg:
        continue
    if method == "initialize":
        result = {"userAgent": "fake-codex", "platformOs": "windows"}
    elif method == "echo":
        print(json.dumps({"method": "test/event", "params": {"ok": True}}), flush=True)
        result = msg["params"]
    elif method == "hang":
        continue
    elif method == "disconnect":
        break
    elif method == "bad":
        print(json.dumps({"id": msg["id"], "error": {"code": -32601, "message": "missing method"}}), flush=True)
        continue
    else:
        result = {}
    print(json.dumps({"id": msg["id"], "result": result}), flush=True)
