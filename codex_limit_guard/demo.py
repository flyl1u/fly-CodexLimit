"""Deterministic three-task demo that never connects to Codex or a model."""

import json
from pathlib import Path
import time

from .config import Config
from .daemon import Coordinator
from .files import atomic_json
from .hooks import handle_hook
from .store import Store


def quota(used, reset):
    return {"rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": used, "resetsAt": reset, "windowDurationMins": 300},
        "secondary": {"usedPercent": used, "resetsAt": reset + 10000, "windowDurationMins": 10080}}}}


class MemoryRuntime:
    def __init__(self):
        self.titles = {f"demo-{i}": f"示例任务 {i}" for i in range(1, 4)}

    def drain(self):
        return []

    def request(self, method, params):
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "name": self.titles[params["threadId"]]}}
        if method == "thread/name/set":
            self.titles[params["threadId"]] = params["name"]
            return {}
        raise ValueError(f"演示不允许调用接口: {method}")


def run_demo(output):
    output = Path(output).resolve()
    marker = output / "demo-state.json"
    if (output / "guard.sqlite3").exists() and not marker.exists():
        raise ValueError("目标目录包含非演示状态，拒绝覆盖；请选择新的 --output")
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(marker, {"kind": "codex-limit-guard-demo"})
    # Each run has its own state, so demonstrations never replay stale task records.
    import uuid
    state = output / ("run-" + uuid.uuid4().hex[:8])
    store = Store(state)
    config = Config(resume_spacing_seconds=1)
    runtime = MemoryRuntime()
    coordinator = Coordinator(store, config, runtime)
    now = time.time()
    trace = []

    def record(label):
        trace.append({"step": label, "mode": store.decision(config, now=now).mode,
                      "tasks": [{"id": task["thread_id"], "phase": task["phase"],
                                 "title": runtime.titles[task["thread_id"]]} for task in store.tasks()]})

    store.observe_quota(quota(40, 10000), config, now=now)
    for key, title in runtime.titles.items():
        store.register(key, "D:/demo-project", title, now=now)
        handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": key, "turn_id": "old-" + key},
                    store, config, now=now)
    record("三个任务正常运行")
    now += 30
    store.observe_quota(quota(99, 10000), config, now=now)
    record("统一触发额度保护")
    for key in runtime.titles:
        store.checkpoint(key, {"completed": ["示例页面实现"], "current": "第二组测试结果尚待确认",
                              "remaining": ["修复失败项", "完整验证"], "next_step": "先读取第二组测试结果",
                              "validation": ["只确认第一组通过"]}, "old-" + key, now=now + 1)
        handle_hook({"hook_event_name": "Stop", "session_id": key, "turn_id": "old-" + key},
                    store, config, now=now + 2)
    coordinator.reconcile_tasks(now + 3)
    record("保存断点、确认停止并标记标题")
    now += 60
    store.observe_quota(quota(10, 30000), config, now=now)
    record("真实额度恢复，等待用户继续，保留感叹号")
    for key in runtime.titles:
        now += 2
        result = handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": key, "turn_id": "new-" + key},
                             store, config, now=now)
        if "decision" in result:
            raise RuntimeError("演示恢复队列未放行")
        handle_hook({"hook_event_name": "PostToolUse", "session_id": key, "turn_id": "new-" + key,
                     "tool_name": "Bash", "tool_input": {}}, store, config, now=now + 1)
        coordinator.reconcile_tasks(now + 1)
    record("用户续做并实际执行后恢复标题")
    result = {"model_requests": 0, "codex_connections": 0, "state_directory": str(state), "trace": trace}
    atomic_json(output / "demo-run.json", result)
    return result
