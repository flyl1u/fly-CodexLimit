import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

from codex_limit_guard.config import Config
from codex_limit_guard.config import runtime_environment
from codex_limit_guard.daemon import run_daemon
from codex_limit_guard.files import atomic_json
from codex_limit_guard.install import install, uninstall, apply_transaction, recover_install
from codex_limit_guard.store import Store
from tests.test_core import quota


ENTRY = Path(__file__).resolve().parent.parent / "guard.py"


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guard install 空格 ")
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex home"
        self.home = self.root / "state"
        self.codex.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_install_update_uninstall_preserves_unrelated_changes(self):
        original_hook = {"hooks": [{"type": "command", "command": "existing-policy"}]}
        hooks = {"description": "保留原有策略", "hooks": {"PreToolUse": [original_hook]}}
        atomic_json(self.codex / "hooks.json", hooks)
        original_config = 'model = "existing-model"\n[features]\nhooks = true\n'
        (self.codex / "config.toml").write_text(original_config, encoding="utf-8")
        install(self.home, self.codex, ENTRY)
        self.assertEqual(runtime_environment(self.home)["CODEX_HOME"], str(self.codex.resolve()))
        install(self.home, self.codex, ENTRY)
        data = json.loads((self.codex / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["hooks"]["PreToolUse"]), 2)
        data["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": "later-policy"}]})
        atomic_json(self.codex / "hooks.json", data)
        with (self.codex / "config.toml").open("a", encoding="utf-8") as out:
            out.write('\n[mcp_servers.unrelated]\ncommand="keep-me"\n')
        uninstall(self.home, self.codex)
        restored = json.loads((self.codex / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(restored["hooks"]["PreToolUse"]), 2)
        self.assertEqual(restored["hooks"]["PreToolUse"][0], original_hook)
        config = tomllib.loads((self.codex / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "existing-model")
        self.assertIn("unrelated", config["mcp_servers"])
        self.assertNotIn("codex_limit_guard", config["mcp_servers"])

    def test_clean_uninstall_removes_only_generated_files(self):
        install(self.home, self.codex, ENTRY)
        uninstall(self.home, self.codex)
        self.assertFalse((self.codex / "hooks.json").exists())
        self.assertFalse((self.codex / "config.toml").exists())
        self.assertFalse((self.codex / "skills/codex-limit-checkpoint/SKILL.md").exists())
        self.assertTrue((self.home / "install-backup.json").exists())

    def test_dry_run_does_not_write_target_or_state(self):
        result = install(self.home, self.codex, ENTRY, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(list(self.codex.iterdir()), [])
        self.assertFalse(self.home.exists())

    def test_conflicting_mcp_and_changed_skill_are_not_overwritten(self):
        (self.codex / "config.toml").write_text('[mcp_servers.codex_limit_guard]\ncommand="other"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            install(self.home, self.codex, ENTRY)
        self.assertFalse((self.codex / "hooks.json").exists())
        (self.codex / "config.toml").unlink()
        install(self.home, self.codex, ENTRY)
        skill = self.codex / "skills/codex-limit-checkpoint/SKILL.md"
        skill.write_text("用户修改", encoding="utf-8")
        result = uninstall(self.home, self.codex)
        self.assertTrue(result["kept_edited_skill"])
        self.assertEqual(skill.read_text(encoding="utf-8"), "用户修改")

    def test_install_rollback_and_crash_recovery(self):
        self.home.mkdir()
        hooks_path, config_path = self.codex / "hooks.json", self.codex / "config.toml"
        real_write = __import__("codex_limit_guard.install", fromlist=["_write"])._write
        failed = False
        def fail_once(path, text):
            nonlocal failed
            if path == config_path and not failed:
                failed = True
                raise OSError("synthetic disk error")
            return real_write(path, text)
        with patch("codex_limit_guard.install._write", side_effect=fail_once):
            with self.assertRaises(OSError):
                apply_transaction(self.home, self.codex, [(hooks_path, "{}"), (config_path, "x=1")])
        self.assertFalse(hooks_path.exists())
        # Simulate a crash after the first atomic replacement, before the second.
        atomic_json(self.home / "install-transaction.json", {"writes": [
            {"path": str(hooks_path), "before": None, "after": "{}"},
            {"path": str(config_path), "before": None, "after": "x=1"}]})
        hooks_path.write_text("{}", encoding="utf-8")
        recover_install(self.home, self.codex)
        self.assertFalse(hooks_path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Hook launcher")
    def test_generated_windows_hook_runs_with_spaces_and_unicode_paths(self):
        install(self.home, self.codex, ENTRY)
        hooks = json.loads((self.codex / "hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"].split()
        event = {"session_id": "real-hook-test", "hook_event_name": "SessionStart", "cwd": "D:/中文目录"}
        result = subprocess.run(command, input=json.dumps(event, ensure_ascii=False), encoding="utf-8",
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertIn("real-hook-test", output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(Store(self.home).task("real-hook-test")["cwd"], "D:/中文目录")

    @unittest.skipUnless(os.name == "nt", "Windows Hook launcher")
    def test_installed_hooks_deliver_warning_and_checkpoint_handoff_over_stdio(self):
        install(self.home, self.codex, ENTRY)
        hooks = json.loads((self.codex / "hooks.json").read_text(encoding="utf-8"))
        store, config = Store(self.home), Config()

        def invoke(name, **extra):
            command = hooks["hooks"][name][0]["hooks"][0]["commandWindows"].split()
            event = {"session_id": "native-flow", "turn_id": "turn", "hook_event_name": name, **extra}
            result = subprocess.run(command, input=json.dumps(event), encoding="utf-8",
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout) if result.stdout.strip() else {}

        store.observe_quota(quota(83), config)
        self.assertNotIn("decision", invoke("UserPromptSubmit"))
        self.assertEqual(invoke("PreToolUse", tool_name="Bash", tool_input={}), {})
        store.observe_quota(quota(95), config)
        self.assertEqual(invoke("PreToolUse", tool_name="Bash", tool_input={})
                         ["hookSpecificOutput"]["permissionDecision"], "deny")
        handoff = invoke("Stop")
        self.assertIn("codex-limit-checkpoint", handoff["reason"])
        args = {"thread_id": "native-flow", "turn_id": "turn",
                "checkpoint": {"current": "原生 Hook 验证现场", "next_step": "额度恢复后核对"}}
        self.assertEqual(invoke("PreToolUse", tool_name="mcp__codex_limit_guard__save_checkpoint", tool_input=args), {})
        wire = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "save_checkpoint", "arguments": args}}
        saved = subprocess.run([sys.executable, str(ENTRY), "--home", str(self.home), "mcp"],
                               input=json.dumps(wire) + "\n", encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertFalse(json.loads(saved.stdout)["result"]["isError"])
        self.assertFalse(invoke("Stop", stop_hook_active=True)["continue"])
        self.assertEqual(store.task("native-flow")["phase"], "paused")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "state"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, data=None):
        return subprocess.run([sys.executable, str(ENTRY), "--home", str(self.home), *args],
                              input=data, text=True, encoding="utf-8", capture_output=True, timeout=15)

    def test_init_status_and_invalid_config(self):
        self.assertEqual(self.run_cli("init").returncode, 0)
        status = self.run_cli("status", "--json")
        self.assertEqual(json.loads(status.stdout)["decision"]["mode"], "unknown")
        (self.home / "config.json").write_text('{"pause_remaining": 50}', encoding="utf-8")
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 1)

    def test_hook_error_still_returns_valid_protocol_block(self):
        self.home.mkdir()
        (self.home / "config.json").write_text("invalid", encoding="utf-8")
        result = self.run_cli("hook", data=json.dumps({"session_id": "test", "hook_event_name": "PreToolUse"}))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_demo_never_connects_to_codex(self):
        output = Path(self.temp.name) / "demo"
        result = self.run_cli("demo", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = json.loads((output / "demo-run.json").read_text(encoding="utf-8"))
        self.assertEqual(trace["model_requests"], 0)
        self.assertEqual(trace["codex_connections"], 0)
        self.assertTrue(all(t["title"].startswith("❗ ") for t in trace["trace"][2]["tasks"]))
        self.assertTrue(all(not t["title"].startswith("❗ ") for t in trace["trace"][-1]["tasks"]))

    def test_daemon_stops_via_local_request_and_releases_lease(self):
        store = Store(self.home)
        stop = threading.Event()
        errors = []
        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def request(self, *_):
                return quota()
            def drain(self):
                return []
        def run():
            try:
                run_daemon(store, Config(), stop, client_factory=FakeClient)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 5
        while store.get_meta("health") is None and time.monotonic() < deadline:
            time.sleep(0.01)
        store.put_meta("stop_requested_at", time.time())
        worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(store.get_meta("daemon_lease"))


if __name__ == "__main__":
    unittest.main()
