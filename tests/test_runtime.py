from pathlib import Path
import json
import sys
import tempfile
import unittest

from codex_limit_guard.config import Config
from codex_limit_guard.daemon import Coordinator
from codex_limit_guard.report import render_report
from codex_limit_guard.rpc import RpcClient, RpcError
from codex_limit_guard.store import Store
from tests.test_core import quota


class FakeRpc:
    def __init__(self):
        self.calls = []
        self.events = []
        self.loaded = set()
        self.title = "测试任务"
        self.turn_status = "inProgress"
        self.fail_write = False

    def drain(self):
        messages, self.events = self.events, []
        return messages

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/loaded/list":
            return {"data": list(self.loaded)}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "name": self.title,
                               "turns": [{"id": "turn", "status": self.turn_status}]}}
        if method == "thread/name/set":
            self.title = params["name"]
            if self.fail_write:
                self.fail_write = False
                raise RpcError("lost response after successful write")
        return {}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.cfg = Config(control_active_turns=True)
        self.rpc = FakeRpc()
        self.coordinator = Coordinator(self.store, self.cfg, self.rpc)
        self.store.observe_quota(quota(), self.cfg, now=100)
        self.store.register("task", title="测试任务", now=101)
        self.store.begin_work("task", "turn", self.cfg, now=101)
        self.store.observe_quota(quota(99), self.cfg, now=130)

    def tearDown(self):
        self.temp.cleanup()

    def count(self, method):
        return sum(call[0] == method for call in self.rpc.calls)

    def test_isolated_server_does_not_interrupt_or_rename_active_desktop_task(self):
        self.coordinator.reconcile_tasks(300)
        self.assertEqual(self.count("turn/interrupt"), 0)
        self.assertEqual(self.count("thread/name/set"), 0)
        self.assertEqual(self.store.task("task")["phase"], "pausing")
        self.assertTrue(self.store.get_meta("task_errors"))

    def test_verified_turn_steered_once_and_interrupt_waits_for_confirmation(self):
        self.rpc.loaded.add("task")
        self.coordinator.reconcile_tasks(131)
        self.coordinator.reconcile_tasks(132)
        self.assertEqual(self.count("turn/steer"), 1)
        self.coordinator.reconcile_tasks(230)
        self.assertEqual(self.count("turn/interrupt"), 1)
        self.assertEqual(self.store.task("task")["phase"], "pausing")
        self.rpc.turn_status = "interrupted"
        self.coordinator.reconcile_tasks(231)
        self.assertEqual(self.store.task("task")["phase"], "paused")
        self.assertEqual(self.rpc.title, "❗ 测试任务")

    def test_title_write_crash_recovers_ownership_and_preserves_user_rename(self):
        self.store.close_turn("task", "turn", config=self.cfg, now=131)
        self.rpc.fail_write = True
        self.coordinator.reconcile_tasks(132)
        self.assertIsNotNone(self.store.task("task")["title_action"])
        self.coordinator.reconcile_tasks(133)
        self.assertEqual(self.count("thread/name/set"), 1)
        self.assertTrue(self.store.task("task")["marker_owned"])
        self.rpc.title = "❗ 用户新标题"
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=160)
        self.store.request_resume("task", "new", self.cfg, now=161)
        self.coordinator.reconcile_tasks(161)
        self.assertEqual(self.rpc.title, "❗ 用户新标题")
        self.store.begin_work("task", "new", self.cfg, now=162, tool="Bash", executed=True)
        self.coordinator.reconcile_tasks(163)
        self.assertEqual(self.rpc.title, "用户新标题")

    def test_original_user_prefix_is_not_removed(self):
        self.rpc.title = "❗ 用户原标记"
        self.store.close_turn("task", "turn", config=self.cfg, now=131)
        self.coordinator.reconcile_tasks(132)
        self.store.update_task("task", phase="running", resume_started_at=150)
        self.coordinator.reconcile_tasks(151)
        self.assertEqual(self.rpc.title, "❗ 用户原标记")

    def test_report_distinguishes_old_checkpoint_and_pending_stop(self):
        self.store.checkpoint("task", {"current": "旧进度", "next_step": "继续检查"}, "turn", now=120)
        report = render_report(self.store.task("task"))
        self.assertIn("等待回合结束确认", report)
        self.assertIn("可能早于暂停", report)
        self.store.checkpoint("task", {"current": "新断点", "next_step": "核对测试结果"}, "turn", now=135)
        self.store.close_turn("task", "turn", config=self.cfg, now=136)
        report = render_report(self.store.task("task"))
        self.assertNotIn("可能早于暂停", report)

    def test_stale_completion_event_cannot_close_resumed_turn(self):
        self.store.close_turn("task", "turn", config=self.cfg, now=131)
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=160)
        self.store.request_resume("task", "new", self.cfg, now=161)
        self.rpc.events.append({"method": "turn/completed", "params": {
            "threadId": "task", "turn": {"id": "turn", "status": "completed"}}})
        self.coordinator.consume_events(162)
        self.assertEqual(self.store.task("task")["phase"], "resuming")
        self.rpc.events.append({"method": "item/completed", "params": {
            "threadId": "task", "turnId": "turn", "item": {"type": "commandExecution"}}})
        self.coordinator.consume_events(163)
        self.assertEqual(self.store.task("task")["phase"], "resuming")


class TransportTests(unittest.TestCase):
    def command(self):
        return [sys.executable, str(Path(__file__).with_name("fake_server.py"))]

    def test_roundtrip_notification_errors_and_clean_close(self):
        with RpcClient(self.command(), timeout=2) as rpc:
            self.assertEqual(rpc.request("echo", {"text": "中文"}), {"text": "中文"})
            self.assertEqual(rpc.drain()[0]["method"], "test/event")
            with self.assertRaises(RpcError) as error:
                rpc.request("bad")
            self.assertEqual(error.exception.code, -32601)
        self.assertIsNotNone(rpc.process.poll())

    def test_timeout_and_disconnect(self):
        with RpcClient(self.command(), timeout=0.1) as rpc:
            with self.assertRaisesRegex(RpcError, "超时"):
                rpc.request("hang")
            with self.assertRaises(RpcError):
                rpc.request("disconnect")


if __name__ == "__main__":
    unittest.main()
