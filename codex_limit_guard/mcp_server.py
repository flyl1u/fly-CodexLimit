"""Minimal stdio MCP tools for a shell-free checkpoint escape path."""

import json
import sqlite3

from . import __version__
from .report import export_report


CHECKPOINT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["current", "next_step"],
    "properties": {
        "current": {"type": "string", "description": "当前现场及尚未确认的操作"},
        "next_step": {"type": "string", "description": "续做时第一步"},
        **{key: {"type": "array", "items": {"type": "string"}} for key in (
            "completed", "remaining", "validation", "files", "commands")},
        "notes": {"type": "string"},
    },
}

IDENTITY = {"thread_id": {"type": "string"}, "turn_id": {"type": "string"}}
TOOLS = [
    {"name": "save_checkpoint", "description": "保存当前 Codex 聊天断点，不执行 shell，不启动模型。额度保护时也可调用。",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["thread_id", "checkpoint"],
                     "properties": {**IDENTITY, "checkpoint": CHECKPOINT_SCHEMA}}},
    {"name": "read_checkpoint", "description": "读取最近保存的断点、时间和任务阶段；恢复前核对实际现场。",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["thread_id"],
                     "properties": IDENTITY}},
    {"name": "status", "description": "每轮开始、写入前及连续工作60秒后检查额度；protect/unknown时保存断点并结束。只读本地缓存，不联网。",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"thread_id": {"type": "string"}}}},
]


def call_tool(name, arguments, store, config):
    if not isinstance(arguments, dict):
        raise ValueError("arguments 必须为对象")
    if name == "status":
        if set(arguments) - {"thread_id"} or ("thread_id" in arguments and not isinstance(arguments["thread_id"], str)):
            raise ValueError("status 只接受可选的字符串 thread_id")
        decision = store.decision(config)
        result = decision.to_dict()
        result["action"] = "save_checkpoint_and_stop" if decision.protected else "continue"
        if decision.protected:
            from .daemon import PAUSE_SIGNAL
            result["signal"] = PAUSE_SIGNAL
        if arguments.get("thread_id"):
            result["hook_coverage"] = store.hook_coverage(arguments["thread_id"], stale_seconds=config.stale_seconds)
        return result
    if name not in ("save_checkpoint", "read_checkpoint"):
        raise ValueError("未知工具")
    allowed = {"thread_id", "turn_id", "checkpoint"} if name == "save_checkpoint" else {"thread_id", "turn_id"}
    if set(arguments) - allowed:
        raise ValueError("未知工具参数")
    thread = arguments.get("thread_id")
    task = store.task(thread) if isinstance(thread, str) else None
    if task is None:
        if name != "save_checkpoint":
            raise ValueError("聊天尚未登记；先确认 Hook 已加载并受信任")
        # A missing Hook must not prevent the fallback from saving progress.
        # Registering via MCP deliberately creates no Hook receipt or pause proof.
        store.register(thread)
    if name == "save_checkpoint":
        store.checkpoint(thread, arguments.get("checkpoint"), arguments.get("turn_id"))
        task = store.task(thread)
        path = export_report(store, task)
        return {"saved": True, "checkpoint_at": task["checkpoint_at"], "report_path": str(path), "phase": task["phase"]}
    return {"thread_id": thread, "phase": task["phase"], "checkpoint_at": task["checkpoint_at"],
            "checkpoint_turn_id": task["checkpoint_turn_id"],
            "checkpoint": json.loads(task["checkpoint"]) if task.get("checkpoint") else None}


def serve(store, config, input_stream, output_stream):
    def send(message):
        output_stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        output_stream.flush()

    for line in input_stream:
        request_id = None
        try:
            if len(line) > 131072:
                raise ValueError("MCP 请求过大")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("请求必须为对象")
            if "id" not in request:
                continue
            request_id = request["id"]
            method = request.get("method")
            params = request.get("params") or {}
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "codex_limit_guard", "version": __version__}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    value = call_tool(params.get("name"), params.get("arguments", {}), store, config)
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
                except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            else:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
                continue
            send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except (ValueError, TypeError, AttributeError) as exc:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)[:250]}})
