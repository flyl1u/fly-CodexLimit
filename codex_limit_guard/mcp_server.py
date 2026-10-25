"""Minimal stdio MCP tools for a shell-free checkpoint escape path."""

import json
import sqlite3

from . import __version__
from .report import export_report
from .control import disable


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
    {"name": "status", "description": "检查本地额度；仅protect时保存断点结束，unknown/disabled继续。缺少监控时后台恢复，不等待联网结果。",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"thread_id": {"type": "string"}}}},
    {"name": "disable_guard", "description": "仅在用户明确要求停用保护时调用。立即持久停用并停止监控，低额度时也允许，不执行shell。",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
]


def call_tool(name, arguments, store, config):
    if not isinstance(arguments, dict):
        raise ValueError("arguments 必须为对象")
    if name == "disable_guard":
        if arguments:
            raise ValueError("disable_guard 不接受参数")
        disable(store.home)
        return {"disabled": True, "action": "continue", "state_preserved": str(store.home)}
    if name == "status":
        if set(arguments) - {"thread_id"} or ("thread_id" in arguments and not isinstance(arguments["thread_id"], str)):
            raise ValueError("status 只接受可选的字符串 thread_id")
        from .service import ensure_monitor
        try:
            ensure_monitor(store, config)
        except (OSError, sqlite3.Error, ValueError):
            pass
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
                from .service import ensure_monitor
                try:
                    ensure_monitor(store, config)
                except (OSError, sqlite3.Error, ValueError):
                    pass
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
