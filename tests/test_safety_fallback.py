"""Regressions for disabled/stale chat Hooks and an available checkpoint escape."""

import json
from pathlib import Path
import tempfile
import unittest

from codex_limit_guard.config import Config
from codex_limit_guard.hooks import handle_hook
from codex_limit_guard.install import install, uninstall
from codex_limit_guard.mcp_server import call_tool
from codex_limit_guard.protocol import AGENTS_BLOCK
from codex_limit_guard.store import Store
from tests.test_core import quota


ENTRY = Path(__file__).resolve().parent.parent / "guard.py"


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.cfg = Config()
        self.store.observe_quota(quota(), self.cfg, now=100)

    def tearDown(self):
        self.temp.cleanup()

    def hook(self, name, turn="turn", now=101):
        return handle_hook({"session_id": "chat", "turn_id": turn,
                            "hook_event_name": name, "tool_name": "Bash", "tool_input": {}},
                           self.store, self.cfg, now=now)

    def test_registration_and_old_callbacks_cannot_prove_current_turn(self):
        self.store.register("chat", now=100)
        self.assertFalse(self.store.hook_coverage("chat", now=101)["verified"])
        self.hook("UserPromptSubmit")
        self.hook("PreToolUse", now=102)
        self.assertTrue(self.store.hook_coverage("chat", now=103)["verified"])
        self.assertFalse(self.store.hook_coverage("chat", now=223)["verified"])
        self.hook("UserPromptSubmit", turn="new", now=104)
        self.assertFalse(self.store.hook_coverage("chat", now=105)["verified"])
        self.hook("PreToolUse", turn="new", now=106)
        self.assertTrue(self.store.hook_coverage("chat", now=107)["verified"])
        self.hook("Stop", turn="new", now=108)
        self.assertFalse(self.store.hook_coverage("chat", now=109)["verified"])

    def test_mcp_check_and_checkpoint_never_fabricate_hook_receipts(self):
        self.store.observe_quota(quota(), self.cfg)
        normal = call_tool("status", {"thread_id": "missing"}, self.store, self.cfg)
        self.assertEqual(normal["action"], "continue")
        self.assertFalse(normal["hook_coverage"]["verified"])
        self.assertIsNone(self.store.task("missing"))
        self.store.observe_quota(quota(95), self.cfg)
        protected = call_tool("status", {"thread_id": "missing"}, self.store, self.cfg)
        self.assertEqual(protected["action"], "save_checkpoint_and_stop")
        self.assertIn("[CODEX_LIMIT_GUARD]", protected["signal"])
        saved = call_tool("save_checkpoint", {"thread_id": "missing", "checkpoint": {
            "current": "Hook 未登记，但进度必须能保存", "next_step": "重新加载后核对现场"}}, self.store, self.cfg)
        self.assertTrue(saved["saved"])
        self.assertTrue(Path(saved["report_path"]).is_file())
        self.assertEqual(self.store.hook_coverage("missing")["receipts"], {})

    def test_unknown_quota_also_requires_checkpoint_and_stop(self):
        other = Store(self.root / "empty")
        output = call_tool("status", {}, other, self.cfg)
        self.assertEqual(output["mode"], "unknown")
        self.assertEqual(output["action"], "save_checkpoint_and_stop")

    def test_status_exemption_is_scoped_to_current_chat(self):
        self.store.observe_quota(quota(95), self.cfg, now=100)
        base = {"session_id": "chat", "turn_id": "turn", "hook_event_name": "PreToolUse",
                "tool_name": "mcp__codex_limit_guard__status"}
        self.assertEqual(handle_hook({**base, "tool_input": {"thread_id": "chat"}},
                                     self.store, self.cfg, now=101), {})
        denied = handle_hook({**base, "tool_input": {"thread_id": "other"}},
                             self.store, self.cfg, now=102)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_out_of_order_receipt_cannot_replace_newer_turn(self):
        self.store.record_hook("chat", "PreToolUse", "new", now=110)
        self.store.record_hook("chat", "PreToolUse", "old", now=109)
        self.assertEqual(self.store.hook_coverage("chat", now=111)["receipts"]
                         ["PreToolUse"]["turn_id"], "new")

    def test_start_protocol_contains_independent_preflight(self):
        output = self.hook("SessionStart")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("即使未收到 Hook 信号", output)
        self.assertIn("mode=unknown", output)

    def test_global_fallback_update_and_uninstall_preserve_user_instructions(self):
        codex = self.root / "codex"
        codex.mkdir()
        agents = codex / "AGENTS.md"
        agents.write_text("用户原有规范\n", encoding="utf-8")
        install(self.root / "install-state", codex, ENTRY)
        agents.write_text(agents.read_text(encoding="utf-8") + "用户后来加的规范\n", encoding="utf-8")
        install(self.root / "install-state", codex, ENTRY)
        self.assertEqual(agents.read_text(encoding="utf-8").count(AGENTS_BLOCK), 1)
        uninstall(self.root / "install-state", codex)
        self.assertEqual(agents.read_text(encoding="utf-8"), "用户原有规范\n用户后来加的规范\n")

    def test_upgrade_from_install_without_agents_block_and_edit_conflict(self):
        codex = self.root / "codex"
        codex.mkdir()
        state = self.root / "install-state"
        install(state, codex, ENTRY)
        manifest = json.loads((state / "installation.json").read_text(encoding="utf-8"))
        manifest.pop("agents_block")
        manifest.pop("original_agents")
        (codex / "AGENTS.md").unlink()
        (state / "installation.json").write_text(json.dumps(manifest), encoding="utf-8")
        install(state, codex, ENTRY)
        agents = codex / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8").replace("约 60 秒", "约 50 秒"), encoding="utf-8")
        with self.assertRaises(ValueError):
            install(state, codex, ENTRY)
        with self.assertRaises(ValueError):
            uninstall(state, codex)
        self.assertIn("约 50 秒", agents.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
