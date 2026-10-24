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
        self.assertEqual(blocked["decision"], "block")
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=self.now + 60)
        result = self.invoke("UserPromptSubmit", offset=61, turn_id="new")
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        self.assertEqual(self.store.task("thread")["phase"], "resuming")
        self.invoke("PostToolUse", offset=62, turn_id="new", tool_name="Bash", tool_input={})
        self.assertEqual(self.store.task("thread")["phase"], "running")

    def test_warning_denies_spawn_and_new_round(self):
        self.invoke("UserPromptSubmit")
        self.store.observe_quota(quota(82, reset=30000), self.cfg, now=self.now + 30)
        blocked = self.invoke("PreToolUse", offset=31, tool_name="spawn_agent", tool_input={})
        self.assertEqual(blocked["hookSpecificOutput"]["permissionDecision"], "deny")
        self.invoke("Stop", offset=32)
        self.assertEqual(self.invoke("UserPromptSubmit", offset=33, turn_id="new")["decision"], "block")

    def test_check_failure_returns_valid_deny_shape(self):
        self.assertEqual(error_output("PreToolUse", "locked")["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(error_output("UserPromptSubmit", "locked")["decision"], "block")

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
        self.assertEqual(len(replies[1]["result"]["tools"]), 3)
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertTrue(replies[3]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
