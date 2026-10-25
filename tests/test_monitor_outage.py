"""User-facing outage, persistent stop, and real subprocess recovery regressions."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from codex_limit_guard.config import Config
from codex_limit_guard.control import disable, disabled
from codex_limit_guard.files import atomic_json
from codex_limit_guard.hooks import handle_hook
from codex_limit_guard.install import install, uninstall
from codex_limit_guard.mcp_server import call_tool
from codex_limit_guard.service import ensure_monitor, process_alive
from codex_limit_guard.store import Store
from tests.test_core import quota

ENTRY = Path(__file__).resolve().parent.parent / "guard.py"


class OutageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "state"
        self.store = Store(self.home)
        self.cfg = Config(auto_start_monitor=False)

    def tearDown(self):
        self.temp.cleanup()

    def hook(self, name, now=100, **args):
        return handle_hook({"session_id": "chat", "turn_id": "new", "hook_event_name": name,
                            "tool_name": "Bash", "tool_input": {}, **args}, self.store, self.cfg, now)

    def test_99_percent_greeting_and_tools_continue_when_cache_is_fresh_or_expired(self):
        for now in (101, 400):
            with self.subTest(now=now):
                self.store.observe_quota(quota(1, 42), self.cfg, now=100)
                self.assertEqual(self.hook("UserPromptSubmit", now), {})
                self.assertEqual(self.hook("PreToolUse", now+1), {})
                self.assertEqual(self.hook("Stop", now+2), {})
                self.assertIsNone(self.store.task("chat")["checkpoint"])
                self.assertEqual(self.store.task("chat")["phase"], "idle")

    def test_missing_cache_never_injects_pause_or_stop_continuation(self):
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            self.assertEqual(self.hook(event), {})
        self.assertFalse(any(e["kind"] in ("pause_requested", "checkpoint_handoff") for e in self.store.events()))

    def test_partial_data_does_not_hide_a_confirmed_low_window(self):
        payload=quota(95)
        payload["rateLimitsByLimitId"]["unavailable"]=None
        self.assertEqual(self.store.observe_quota(payload,self.cfg,now=100).mode,"protect")
        payload=quota(1)
        payload["rateLimitsByLimitId"]["unavailable"]=None
        self.assertEqual(self.store.observe_quota(payload,self.cfg,now=101).mode,"unknown")
        self.assertEqual(self.hook("UserPromptSubmit",102),{})

    def test_old_monitor_fault_holds_cannot_lock_a_recovered_or_unknown_chat(self):
        for phase in ("pausing", "paused", "ready"):
            for used, now in ((1, 101), (1, 500)):
                with self.subTest(phase=phase,now=now):
                    self.store.observe_quota(quota(used, 42), self.cfg, now=100)
                    self.store.register("chat", now=100)
                    self.store.update_task("chat", phase=phase, turn_id="old", pause_reason="额度缓存缺失或已过期",
                                           pause_requested_at=99, closed_at=100 if phase != "pausing" else None)
                    self.assertEqual(self.hook("UserPromptSubmit", now), {})
                    self.assertEqual(self.hook("PreToolUse", now+1), {})
                    self.assertIsNone(self.store.task("chat")["pause_reason"])

    def test_confirmed_low_quota_still_pauses_and_control_mcp_is_exempt(self):
        self.store.observe_quota(quota(95), self.cfg, now=100)
        self.assertIn("[CODEX_LIMIT_GUARD]", self.hook("UserPromptSubmit",101)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("PreToolUse",102)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.hook("PreToolUse",103,tool_name="mcp__codex_limit_guard__disable_guard"), {})
        self.assertTrue(call_tool("disable_guard", {}, self.store, self.cfg)["disabled"])
        self.assertTrue(disabled(self.home))
        self.assertEqual(self.hook("PreToolUse",104), {})
        status=call_tool("status", {}, self.store, self.cfg)
        self.assertEqual((status["mode"],status["action"]),("disabled","continue"))
        self.assertNotIn("signal", status)

    def test_monitor_failure_does_not_create_recovery_latch_at_17_percent(self):
        self.hook("UserPromptSubmit",100)
        self.store.protect_if_stale(self.cfg, now=300)
        result=self.store.observe_quota(quota(83),self.cfg,now=301)
        self.assertEqual(result.mode,"normal")
        self.assertFalse(self.store.get_meta("quota_latched"))

    def cli(self,*args,data=None):
        return subprocess.run([sys.executable,str(ENTRY),"--home",str(self.home),*args],
                              input=data,capture_output=True,text=True,encoding="utf-8",timeout=12)

    def test_stop_works_with_invalid_config_and_corrupt_database(self):
        (self.home/"config.json").write_text("broken",encoding="utf-8")
        # Release all Store connections before replacing the isolated test database.
        (self.home/"guard.sqlite3").write_bytes(b"not a database")
        result=self.cli("stop")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)["disabled"])
        self.assertEqual(self.cli("daemon").returncode,0)

    def test_uninstalled_cached_hook_is_inert_even_with_valid_low_quota(self):
        codex=self.root/"codex"
        install(self.home,codex,ENTRY)
        self.store.observe_quota(quota(99),self.cfg)
        uninstall(self.home,codex)
        result=self.cli("hook",data=json.dumps({"session_id":"chat","hook_event_name":"PreToolUse"}))
        self.assertEqual(json.loads(result.stdout),{})
        self.assertNotEqual(self.cli("enable").returncode,0)

    def test_installed_native_hook_does_not_pause_99_percent_after_monitor_dies(self):
        install(self.home,self.root/"codex",ENTRY)
        atomic_json(self.home/"config.json",{"auto_start_monitor":False})
        self.store.observe_quota(quota(1,42),self.cfg,now=time.time()-3600)
        for name in ("UserPromptSubmit","PreToolUse","Stop"):
            result=self.cli("hook",data=json.dumps({"session_id":"chat","turn_id":"new",
                "hook_event_name":name,"tool_name":"Bash","tool_input":{}}))
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout or "{}"),{})

    def test_disabled_or_uninstalled_monitor_is_never_restarted(self):
        def forbidden(*args,**kwargs): raise AssertionError("unexpected monitor spawn")
        self.assertFalse(ensure_monitor(self.store,Config(),launcher=forbidden))
        atomic_json(self.home/"installation.json",{})
        disable(self.home)
        self.assertFalse(ensure_monitor(self.store,Config(),launcher=forbidden))

    def test_parallel_start_requests_launch_only_once(self):
        atomic_json(self.home/"installation.json",{})
        calls=[]
        def launch(args,**kwargs):
            calls.append(args)
            return SimpleNamespace(pid=123,wait=lambda:0)
        with ThreadPoolExecutor(8) as pool:
            results=list(pool.map(lambda _:ensure_monitor(self.store,Config(),launcher=launch),range(16)))
        self.assertEqual(sum(results),1)
        self.assertEqual(len(calls),1)

    def test_actual_monitor_crash_recovers_without_waiting_for_old_lease(self):
        cfg=Config(server_command=(sys.executable,str(Path(__file__).with_name("fake_server.py"))),poll_seconds=.2,stale_seconds=2)
        atomic_json(self.home/"config.json",cfg.to_dict())
        atomic_json(self.home/"installation.json",{"codex_home":str(self.root/"codex")})
        children=[]
        def launch(*args,**kwargs):
            child=subprocess.Popen(*args,**kwargs);children.append(child);return child
        def until(predicate):
            end=time.monotonic()+8
            while time.monotonic()<end:
                if predicate():return
                time.sleep(.05)
            self.fail((self.home/"daemon.stderr.log").read_text(encoding="utf-8"))
        try:
            self.assertTrue(ensure_monitor(self.store,cfg,launcher=launch))
            until(lambda:self.store.get_meta("health",{}).get("connected"))
            first=children[0]
            self.assertTrue(process_alive(first.pid))
            first.terminate();first.wait(timeout=4)
            self.assertFalse(process_alive(first.pid))
            self.assertTrue(ensure_monitor(self.store,cfg,launcher=launch))
            until(lambda:self.store.get_meta("daemon_lease",{}).get("pid")==children[-1].pid)
            until(lambda:self.store.get_meta("health",{}).get("connected"))
            self.assertEqual(self.store.decision(cfg).mode,"normal")
            disable(self.home)
            children[-1].wait(timeout=5)
            self.assertFalse(ensure_monitor(self.store,cfg,launcher=launch))
        finally:
            disable(self.home)
            for child in children:
                if child.poll() is None: child.terminate()
                child.wait(timeout=5)


if __name__ == "__main__":unittest.main()
