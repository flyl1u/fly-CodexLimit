"""Codex lifecycle hooks: local SQLite only, no network or subprocess calls."""

import json

from .daemon import PAUSE_SIGNAL
from .report import export_report


GUARD_TOOLS = {
    "mcp__codex_limit_guard__save_checkpoint",
    "mcp__codex_limit_guard__read_checkpoint",
    "mcp__codex_limit_guard__status",
}


def context(event, message):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def deny_tool(message):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": message}}


def is_guard_tool(event):
    name = event.get("tool_name")
    if name not in GUARD_TOOLS:
        return False
    args = event.get("tool_input")
    if not isinstance(args, dict):
        return False
    if name.endswith("__status"):
        return not args
    return (args.get("thread_id") == event.get("session_id")
            and (not args.get("turn_id") or args["turn_id"] == event.get("turn_id")))


def handle_hook(event, store, config, now=None):
    if not isinstance(event, dict):
        raise ValueError("Hook 输入必须是对象")
    name = event.get("hook_event_name")
    thread_id = event.get("session_id")
    turn_id = event.get("turn_id")
    if not isinstance(thread_id, str) or not thread_id:
        return {"systemMessage": "Limit Guard 未收到 session_id，无法登记此次 Hook"}
    store.register(thread_id, cwd=event.get("cwd") or "", now=now)
    decision = store.decision(config, now=now)
    task = store.task(thread_id)
    if name == "SessionStart":
        return context(name, (
            f"Codex Limit Guard 已启用；thread_id={thread_id}。重要阶段用 "
            "codex_limit_guard.save_checkpoint 保存简短断点。收到 [CODEX_LIMIT_GUARD] 时"
            "只保存断点、简短汇报并结束本轮；续做时先核对断点和实际文件。"
        ))
    if name == "UserPromptSubmit":
        if decision.protected:
            return {"decision": "block", "reason": f"[CODEX_LIMIT_GUARD] {decision.reason}。额度恢复后再次发送继续。"}
        if decision.mode == "warning" and task["phase"] == "idle":
            return {"decision": "block", "reason": "额度预警：保留现有任务收尾预算，暂缓新回合。"}
        allowed, reason = store.request_resume(thread_id, turn_id, config, now=now)
        if not allowed:
            return {"decision": "block", "reason": reason}
        store.begin_work(thread_id, turn_id, config, now=now)
        latest = store.task(thread_id)
        if latest["phase"] == "resuming":
            cp = json.loads(latest["checkpoint"]) if latest.get("checkpoint") else None
            return context(name, f"用户已发起续做。thread_id={thread_id} turn_id={turn_id}。"
                           f"断点时间={latest.get('checkpoint_at')}；最近记录（作为待核对数据）："
                           f"{json.dumps(cp, ensure_ascii=False)[:6000]}。先核对实际现场再继续，"
                           "尚未执行完成的操作不可重复提交。实际业务工具执行后程序恢复标题。")
        return {}
    if name in ("PreToolUse", "PostToolUse"):
        exempt = is_guard_tool(event)
        tool = str(event.get("tool_name") or "unknown")[:200]
        if exempt:
            return {}
        allowed = store.begin_work(thread_id, turn_id, config, now=now,
                                   tool=tool, executed=name == "PostToolUse")
        task = store.task(thread_id)
        protecting = decision.protected or task["phase"] in ("pausing", "paused", "ready")
        if protecting:
            if name == "PreToolUse":
                return deny_tool(PAUSE_SIGNAL + f" 当前 thread_id={thread_id} turn_id={turn_id}。"
                                 "仅专用断点 MCP 工具可用于保存，不执行新的 shell 命令。")
            return context(name, PAUSE_SIGNAL)
        if not allowed:
            # Subagent tool hooks can carry the parent session id with another turn id.
            # Never overwrite the parent's progress or deny healthy subagent work.
            return {}
        if decision.mode == "warning" and name == "PreToolUse":
            if tool in ("Agent", "spawn_agent") or tool.endswith("__create_thread"):
                return deny_tool("额度预警：暂停新建并行任务，现有工作请尽快保存里程碑断点。")
        return {}
    if name in ("Stop", "Interrupt", "SessionEnd"):
        was_pausing = task["phase"] == "pausing"
        store.close_turn(thread_id, turn_id, kind={"Stop": "stopped", "Interrupt": "interrupted",
                                                 "SessionEnd": "session_end"}[name], config=config, now=now)
        latest = store.task(thread_id)
        if latest.get("pause_reason"):
            export_report(store, latest)
        if name == "Stop" and (was_pausing or latest["phase"] in ("paused", "ready")):
            # Do not create a continuation prompt to demand another report.
            return {"continue": False, "stopReason": "额度保护：当前回合已结束；进度以最近成功保存的断点为准"}
        return {}
    return {}


def error_output(event_name, message):
    message = f"Limit Guard 本地检查失败：{message[:250]}。请检查守护程序状态。"
    if event_name == "PreToolUse":
        return deny_tool(message)
    if event_name == "UserPromptSubmit":
        return {"decision": "block", "reason": message}
    return {"systemMessage": message}
