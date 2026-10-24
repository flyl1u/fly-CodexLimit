"""Account polling and metadata coordination; only verified live turns are controlled."""

import json
from pathlib import Path
import threading
import time
import uuid

from .report import export_report
from .config import runtime_environment
from .rpc import RpcClient, RpcError
from .titles import mark_title, restore_title


PAUSE_SIGNAL = (
    "[CODEX_LIMIT_GUARD] 额度保护已触发。现在应用 codex-limit-checkpoint Skill，"
    "先简短说明正在保存断点，再按下列已注入的 Skill 正文执行；无需调用 shell 读取 Skill。"
    "停止新增业务工作，保存断点后汇报并结束本轮。用户消息已接收，"
    "尚未执行的请求写入 remaining，额度恢复后由用户续做。\n\n"
    + (Path(__file__).parent / "skill" / "SKILL.md").read_text(encoding="utf-8")
)


class Coordinator:
    def __init__(self, store, config, rpc):
        self.store = store
        self.config = config
        self.rpc = rpc

    def consume_events(self, now):
        for message in self.rpc.drain():
            method, params = message.get("method"), message.get("params", {})
            thread = params.get("threadId")
            task = self.store.task(thread) if thread else None
            # Only registered tasks are managed. Never enumerate and alter all user chats.
            if not task:
                continue
            if method == "turn/completed":
                turn = params.get("turn", {})
                self.store.close_turn(thread, turn.get("id"), kind=turn.get("status", "stopped"),
                                      config=self.config, now=now)
            elif method == "thread/name/updated" and isinstance(params.get("name"), str):
                self.store.update_task(thread, title=params["name"])
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") in ("commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"):
                    self.store.begin_work(thread, params.get("turnId"), self.config, now=now,
                                          tool=item["type"], executed=True)

    def read_thread(self, thread_id, include_turns=False):
        result = self.rpc.request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})
        return result["thread"]

    def loaded_ids(self):
        result = set()
        cursor = None
        seen = set()
        for _ in range(100):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.rpc.request("thread/loaded/list", params)
            result.update(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return result
            if cursor in seen:
                raise RpcError("loaded/list 返回重复分页游标")
            seen.add(cursor)
        raise RpcError("loaded/list 超过分页上限")

    def live_turn(self, task, loaded):
        if task["thread_id"] not in loaded or not task.get("turn_id"):
            return None
        # Reading is deliberately used instead of resuming a foreign runtime.
        thread = self.read_thread(task["thread_id"], include_turns=True)
        for turn in thread.get("turns", []):
            if turn.get("id") == task["turn_id"]:
                if turn.get("status") == "inProgress" and thread.get("status", {}).get("type") != "active":
                    return None
                return turn
        return None

    def reconcile_title(self, task):
        paused = task["phase"] in ("paused", "ready") and task.get("closed_at") is not None
        resumed = task.get("resume_started_at") is not None
        pending = json.loads(task["title_action"]) if task.get("title_action") else None
        if not pending and not ((paused and not task["marker_done"]) or (resumed and task["marker_done"])):
            return
        thread = self.read_thread(task["thread_id"])
        current = thread.get("name")
        if not isinstance(current, str) or not current.strip():
            # Don't replace an unknown auto-generated UI title with a guessed one.
            self.store.put_meta(f"title_error:{task['thread_id']}", "运行时未提供真实标题；等待标题元数据可读")
            raise RpcError("运行时未提供真实标题，未改名；等待标题元数据可读")
        prefix = task.get("marker_prefix") or self.config.title_prefix
        if pending:
            if current == pending["target"]:
                self._finish_title(task["thread_id"], pending)
                return
            if current != pending["source"]:
                # A user edited the title while a failed/pending write was outstanding.
                self.store.update_task(task["thread_id"], title_action=None, title=current,
                                       marker_done=1 if pending["kind"] == "mark" else 0,
                                       marker_owned=0)
                return
        elif paused:
            target, owned = mark_title(current, prefix)
            if not owned:
                self.store.update_task(task["thread_id"], marker_done=1, marker_owned=0, title=current)
                return
            pending = {"kind": "mark", "source": current, "target": target, "owned": True, "prefix": prefix}
        else:
            target = restore_title(current, prefix, bool(task["marker_owned"]))
            if target == current:
                self.store.update_task(task["thread_id"], marker_done=0, marker_owned=0, title=current)
                return
            pending = {"kind": "restore", "source": current, "target": target, "owned": False, "prefix": prefix}
        # Persist intent before I/O, so a crash after the write can recover ownership.
        self.store.update_task(task["thread_id"], title_action=json.dumps(pending, ensure_ascii=False))
        self.rpc.request("thread/name/set", {"threadId": task["thread_id"], "name": pending["target"]})
        self._finish_title(task["thread_id"], pending)

    def _finish_title(self, thread_id, pending):
        self.store.update_task(thread_id, title=pending["target"], title_action=None,
                               marker_done=1 if pending["kind"] == "mark" else 0,
                               marker_owned=int(pending["owned"]), marker_title=pending["target"],
                               marker_prefix=pending.get("prefix", self.config.title_prefix) if pending["kind"] == "mark" else None)
        self.store.put_meta(f"title_error:{thread_id}", None)

    def reconcile_tasks(self, now):
        loaded = self.loaded_ids() if self.config.control_active_turns else set()
        errors = []
        for task in self.store.tasks():
            try:
                if task["phase"] == "pausing":
                    turn = self.live_turn(task, loaded)
                    if turn and turn.get("status") in ("completed", "interrupted", "failed"):
                        self.store.close_turn(task["thread_id"], task["turn_id"], kind=turn["status"],
                                              config=self.config, now=now)
                    elif turn and turn.get("status") == "inProgress":
                        if task["steer_turn_id"] != task["turn_id"]:
                            self.rpc.request("turn/steer", {"threadId": task["thread_id"],
                                "expectedTurnId": task["turn_id"],
                                "input": [{"type": "text", "text": PAUSE_SIGNAL
                                           + f"\n当前 thread_id={task['thread_id']} turn_id={task['turn_id']}。"}]})
                            self.store.update_task(task["thread_id"], steer_turn_id=task["turn_id"])
                        if now - task["pause_requested_at"] >= self.config.grace_seconds and task["interrupt_turn_id"] != task["turn_id"]:
                            self.rpc.request("turn/interrupt", {"threadId": task["thread_id"], "turnId": task["turn_id"]})
                            # A successful request is not a completed turn acknowledgement.
                            self.store.update_task(task["thread_id"], interrupt_turn_id=task["turn_id"])
                    elif now - task["pause_requested_at"] >= self.config.grace_seconds:
                        errors.append({"thread_id": task["thread_id"], "error":
                            "未连接同一活动运行时；等待 Hook 收尾确认，无法强制中断"})
                self.reconcile_title(self.store.task(task["thread_id"]))
                latest = self.store.task(task["thread_id"])
                if latest.get("pause_reason"):
                    export_report(self.store, latest)
            except (RpcError, OSError, ValueError, KeyError) as exc:
                errors.append({"thread_id": task["thread_id"], "error": str(exc)[:300]})
        self.store.put_meta("task_errors", errors)
        return errors

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.consume_events(now)
        # A partial notification is not a complete account snapshot: poll all buckets.
        payload = self.rpc.request("account/rateLimits/read", {})
        decision = self.store.observe_quota(payload, self.config, now=now)
        errors = self.reconcile_tasks(now)
        self.store.put_meta("health", {"at": now, "connected": True, "error": None, "task_errors": len(errors),
                                      "protection": self.store.protection_summary(self.config, now)})
        return decision


def run_daemon(store, config, stop_event, once=False, client_factory=RpcClient):
    owner = str(uuid.uuid4())
    runtime_env = runtime_environment(store.home)
    lease_ttl = max(60, config.poll_seconds * 3, config.rpc_timeout_seconds * 5)
    if not store.acquire_lease(owner, lease_ttl):
        raise RuntimeError("此状态目录已有守护程序，拒绝重复启动")
    started_at = time.time()
    heartbeat_stop = threading.Event()
    lease_lost = threading.Event()

    def heartbeat():
        # Renew independently of RPC delays; a large task list must not lose its lease.
        while not heartbeat_stop.wait(1):
            try:
                if store.get_meta("stop_requested_at", 0) >= started_at:
                    stop_event.set()
                if not store.acquire_lease(owner, lease_ttl):
                    lease_lost.set()
                    stop_event.set()
                    return
            except Exception:
                # An unavailable state store cannot safely coordinate more mutations.
                lease_lost.set()
                stop_event.set()
                return

    watchdog = threading.Thread(target=heartbeat, daemon=True)
    watchdog.start()
    backoff = 1.0
    try:
        while not stop_event.is_set():
            try:
                with client_factory(config.server_command, timeout=config.rpc_timeout_seconds, env=runtime_env) as rpc:
                    coordinator = Coordinator(store, config, rpc)
                    while not stop_event.is_set():
                        if not store.acquire_lease(owner, lease_ttl):
                            raise RuntimeError("守护程序租约已丢失")
                        decision = coordinator.tick()
                        backoff = 1
                        if once:
                            return decision
                        stop_event.wait(config.poll_seconds)
            except (RpcError, OSError) as exc:
                store.put_meta("health", {"at": time.time(), "connected": False, "error": str(exc)[:300]})
                store.protect_if_stale(config)
                if once:
                    raise
                stop_event.wait(min(backoff, 30))
                backoff = min(30, backoff * 2)
                if not store.acquire_lease(owner, lease_ttl):
                    raise RuntimeError("守护程序租约已丢失")
        if lease_lost.is_set():
            raise RuntimeError("守护程序租约续期失败，已停止协调")
    finally:
        heartbeat_stop.set()
        watchdog.join(timeout=2)
        store.put_meta("health", {"at": time.time(), "connected": False, "error": "守护程序已退出"})
        store.release_lease(owner)
