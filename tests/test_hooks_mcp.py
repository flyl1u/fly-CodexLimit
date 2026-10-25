import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from codex_limit_guard.config import Config
from codex_limit_guard.hooks import handle_hook, error_output
from codex_limit_guard.mcp_server import call_tool, serve
from codex_limit_guard.store import Store
from tests.test_core import quota


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.cfg = Config()
        self.now = time.time()
        self.store.observe_quota(quota(), self.cfg, now=self.now)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, name, **extra):
        return {"hook_event_name": name, "session_id": "thread", "turn_id": "turn",
                "cwd": "D:/test", **extra}

    def invoke(self, name, offset=1, **extra):
        return handle_hook(self.event(name, **extra), self.store, self.cfg, now=self.now + offset)

    def pause(self):
        self.invoke("UserPromptSubmit")
        self.store.observe_quota(quota(99), self.cfg, now=self.now + 30)

    def test_normal_hooks_are_quiet_and_only_start_injects_protocol(self):
        self.assertIn("additionalContext", self.invoke("SessionStart")["hookSpecificOutput"])
        self.assertEqual(self.invoke("UserPromptSubmit"), {})
        self.assertEqual(self.invoke("PreToolUse", tool_name="Bash", tool_input={"command": "anything"}), {})
        self.invoke("PostToolUse", tool_name="Bash", tool_input={})
        self.assertEqual(self.store.task("thread")["last_tool"], "Bash")

    def test_protected_tools_denied_but_checkpoint_does_not_need_shell(self):
        self.pause()
        output = self.invoke("PreToolUse", offset=31, tool_name="Bash", tool_input={"command": "save then delete"})
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("continue", output)
        post = self.invoke("PostToolUse", offset=31, tool_name="Bash", tool_input={})
        self.assertIn("thread_id=thread turn_id=turn", post["hookSpecificOutput"]["additionalContext"])
        args = {"thread_id": "thread", "turn_id": "turn", "checkpoint": {"current": "现场", "next_step": "核对"}}
        self.assertEqual(self.invoke("PreToolUse", offset=31, tool_name="mcp__codex_limit_guard__save_checkpoint", tool_input=args), {})
        saved = call_tool("save_checkpoint", args, self.store, self.cfg)
        self.assertTrue(saved["saved"])
        self.assertTrue(Path(saved["report_path"]).is_file())
        args["thread_id"] = "another-thread"
        denied = self.invoke("PreToolUse", offset=31, tool_name="mcp__codex_limit_guard__save_checkpoint", tool_input=args)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_stop_closes_without_expensive_continuation(self):
        self.pause()
        result = self.invoke("Stop", offset=31, stop_hook_active=True)
        self.assertFalse(result["continue"])
        self.assertNotIn("decision", result)
        self.assertEqual(self.store.task("thread")["phase"], "paused")

    def test_resume_injects_checkpoint_only_after_user_request(self):
        self.pause()
        self.invoke("Stop", offset=31)
        blocked = self.invoke("UserPromptSubmit", offset=32, turn_id="new")
        self.assertNotIn("decision", blocked)
        self.assertIn("codex-limit-checkpoint", blocked["hookSpecificOutput"]["additionalContext"])
        self.invoke("Stop", offset=33, turn_id="new", stop_hook_active=True)
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=self.now + 60)
        result = self.invoke("UserPromptSubmit", offset=61, turn_id="new")
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        self.assertEqual(self.store.task("thread")["phase"], "resuming")
        self.invoke("PostToolUse", offset=62, turn_id="new", tool_name="Bash", tool_input={})
        self.assertEqual(self.store.task("thread")["phase"], "running")

    def test_seventeen_percent_is_silent_for_new_tasks_and_parallel_work(self):
        self.invoke("UserPromptSubmit")
        self.store.observe_quota(quota(83, reset=30000), self.cfg, now=self.now + 30)
        self.assertEqual(self.invoke("PreToolUse", offset=31, tool_name="spawn_agent", tool_input={}), {})
        self.invoke("Stop", offset=32)
        result = self.invoke("UserPromptSubmit", offset=33, turn_id="new")
        self.assertEqual(result, {})
        self.assertEqual(self.store.task("thread")["turn_id"], "new")
        self.assertEqual(self.store.task("thread")["phase"], "running")
        self.assertEqual(self.invoke("PreToolUse", offset=34, turn_id="new", tool_name="Bash", tool_input={}), {})

    def test_stop_hands_off_skill_once_before_closing_without_a_checkpoint(self):
        self.pause()
        result = self.invoke("Stop", offset=31)
        self.assertEqual(result["decision"], "block")
        self.assertIn("codex-limit-checkpoint", result["reason"])
        self.assertIn("checkpoint` 至少包含", result["reason"])
        self.assertEqual(self.store.task("thread")["phase"], "pausing")
        self.assertFalse(self.invoke("Stop", offset=32)["continue"])
        self.assertEqual(self.store.task("thread")["phase"], "paused")

    def test_successful_skill_checkpoint_closes_without_extra_continuation(self):
        self.pause()
        self.assertEqual(self.invoke("Stop", offset=31)["decision"], "block")
        self.store.checkpoint("thread", {"current": "现场已保存", "next_step": "核对现场"}, "turn", now=self.now + 32)
        result = self.invoke("Stop", offset=33, stop_hook_active=True)
        self.assertFalse(result["continue"])
        self.assertNotIn("decision", result)
        self.assertEqual(self.store.task("thread")["phase"], "paused")

    def test_prompt_during_protection_is_delivered_to_skill_and_can_save_checkpoint(self):
        self.pause()
        self.invoke("Stop", offset=31, stop_hook_active=True)
        result = self.invoke("UserPromptSubmit", offset=32, turn_id="new")
        self.assertNotIn("decision", result)
        self.assertIn("用户消息已接收", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.store.task("thread")["turn_id"], "new")
        args = {"thread_id": "thread", "turn_id": "new", "checkpoint": {"current": "新请求待续做", "next_step": "核对"}}
        self.assertEqual(self.invoke("PreToolUse", offset=33, turn_id="new",
                                     tool_name="mcp__codex_limit_guard__save_checkpoint", tool_input=args), {})
        self.assertTrue(call_tool("save_checkpoint", args, self.store, self.cfg)["saved"])
        self.assertEqual(self.invoke("PreToolUse", offset=34, turn_id="new", tool_name="Bash", tool_input={})
                         ["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_stale_quota_does_not_reject_user_message(self):
        result = self.invoke("UserPromptSubmit", offset=200)
        self.assertNotIn("decision", result)
        self.assertEqual(result, {})
        self.assertEqual(self.store.task("thread")["phase"], "running")

    def test_new_user_turn_recovers_a_task_with_a_missing_stop_hook(self):
        self.pause()
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=self.now + 60)
        self.assertEqual(self.store.task("thread")["phase"], "pausing")
        result = self.invoke("UserPromptSubmit", offset=61, turn_id="new")
        self.assertNotIn("decision", result)
        self.assertEqual(self.store.task("thread")["phase"], "resuming")
        self.assertEqual(self.invoke("PreToolUse", offset=62, turn_id="new", tool_name="Bash", tool_input={}), {})

    def test_healthy_user_submission_updates_turn_but_child_tools_cannot(self):
        self.invoke("UserPromptSubmit")
        self.invoke("UserPromptSubmit", turn_id="new")
        self.assertEqual(self.store.task("thread")["turn_id"], "new")
        self.invoke("PreToolUse", turn_id="child", tool_name="Bash", tool_input={})
        self.assertEqual(self.store.task("thread")["turn_id"], "new")

    def test_resume_queue_does_not_reject_user_message(self):
        self.pause()
        self.invoke("Stop", offset=31, stop_hook_active=True)
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=self.now + 60)
        self.store.put_meta("last_resume_at", self.now + 60)
        result = self.invoke("UserPromptSubmit", offset=61, turn_id="new")
        self.assertNotIn("decision", result)
        self.assertIn("续做已排队", result["hookSpecificOutput"]["additionalContext"])

    def test_new_user_turn_can_execute_when_resuming_turn_lost_its_stop_hook(self):
        self.pause()
        self.invoke("Stop", offset=31, stop_hook_active=True)
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=self.now + 60)
        self.invoke("UserPromptSubmit", offset=61, turn_id="resume-one")
        self.invoke("UserPromptSubmit", offset=62, turn_id="resume-two")
        self.invoke("PostToolUse", offset=63, turn_id="resume-two", tool_name="Bash", tool_input={})
        self.assertEqual(self.store.task("thread")["turn_id"], "resume-two")
        self.assertEqual(self.store.task("thread")["phase"], "running")
        self.assertIsNotNone(self.store.task("thread")["resume_started_at"])

    def test_subagent_turn_does_not_overwrite_parent_or_block_healthy_work(self):
        self.invoke("UserPromptSubmit")
        result = self.invoke("PreToolUse", turn_id="child-turn", tool_name="Bash", tool_input={})
        self.assertEqual(result, {})
        self.assertEqual(self.store.task("thread")["turn_id"], "turn")
        self.invoke("PostToolUse", turn_id="child-turn", tool_name="Bash", tool_input={})
        self.assertEqual(self.store.task("thread")["turn_id"], "turn")

    def test_monitor_failure_does_not_deny_work(self):
        self.assertEqual(error_output("PreToolUse", "locked"), {})
        self.assertEqual(error_output("UserPromptSubmit", "locked"), {})

    def test_mcp_wire_roundtrip_and_tool_error(self):
        self.invoke("UserPromptSubmit")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "save_checkpoint", "arguments": {
                "thread_id": "thread", "checkpoint": {"current": "保存现场", "next_step": "继续"}}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "save_checkpoint", "arguments": {
                "thread_id": "thread", "checkpoint": {"current": "缺少下一步"}}}},
        ]
        source = io.StringIO("\n".join(json.dumps(x) for x in requests) + "\n")
        target = io.StringIO()
        serve(self.store, self.cfg, source, target)
        replies = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertEqual(len(replies[1]["result"]["tools"]), 4)
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertTrue(replies[3]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
