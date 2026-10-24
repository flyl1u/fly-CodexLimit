from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from codex_limit_guard.config import Config
from codex_limit_guard.policy import Snapshot, decide
from codex_limit_guard.store import Store
from codex_limit_guard.titles import mark_title, restore_title


def quota(used=10, weekly=None, reset=10000):
    return {"rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": used, "resetsAt": reset, "windowDurationMins": 300},
        "secondary": None if weekly is None else {
            "usedPercent": weekly, "resetsAt": 20000, "windowDurationMins": 10080},
    }}}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def evaluate(self, payload, latched=False, active=1, previous=None, now=100):
        return decide(Snapshot.parse(payload, now), previous, self.cfg, active, latched, now)

    def test_weekly_limit_and_multi_bucket_win(self):
        self.assertEqual(self.evaluate(quota(20, 98)).mode, "protect")
        value = quota()
        value["rateLimitsByLimitId"]["other"] = {"primary": {"usedPercent": 99}}
        self.assertEqual(self.evaluate(value).limiting_window, "other/primary")

    def test_five_hour_and_weekly_threshold_boundaries(self):
        for used, expected in ((94.99, "warning"), (95, "protect"), (95.01, "protect")):
            with self.subTest(window="five-hour", used=used):
                decision = self.evaluate(quota(used))
                self.assertEqual(decision.mode, expected)
                self.assertEqual(decision.pause_at, 5)
        for used, expected in ((97.99, "warning"), (98, "protect"), (98.01, "protect")):
            with self.subTest(window="weekly", used=used):
                decision = self.evaluate(quota(10, used))
                self.assertEqual(decision.mode, expected)
                self.assertEqual(decision.pause_at, 2)

    def test_weekly_only_uses_two_percent_even_in_primary(self):
        for name in ("primary", "secondary"):
            for duration in (10080, None):
                with self.subTest(name=name, duration=duration):
                    window = {"usedPercent": 97, "windowDurationMins": duration}
                    payload = {"rateLimits": {name: window}}
                    self.assertEqual(self.evaluate(payload).mode, "warning")
                    window["usedPercent"] = 98
                    decision = self.evaluate(payload)
                    self.assertEqual(decision.mode, "protect")
                    self.assertEqual(decision.pause_at, 2)

    def test_window_with_less_remaining_cannot_hide_another_breached_threshold(self):
        decision = self.evaluate(quota(96, 97))
        self.assertEqual(decision.mode, "protect")
        self.assertEqual(decision.limiting_window, "codex/primary")
        self.assertEqual(decision.pause_at, 5)
        payload = quota(10, 96)
        bucket = payload["rateLimitsByLimitId"]["codex"]
        bucket["primary"], bucket["secondary"] = bucket["secondary"], bucket["primary"]
        bucket["secondary"]["usedPercent"] = 95
        decision = self.evaluate(payload)
        self.assertEqual(decision.mode, "protect")
        self.assertEqual(decision.limiting_window, "codex/secondary")
        self.assertEqual(decision.pause_at, 5)

    def test_window_durations_survive_cache_and_old_samples_still_load(self):
        snapshot = Snapshot.parse(quota(20, 30), 100)
        self.assertEqual(Snapshot.from_dict(snapshot.to_dict()), snapshot)
        legacy = Snapshot.from_dict({"observed_at": 100, "windows": [
            {"bucket": "codex", "name": "primary", "used": 98, "resets_at": 10000}]})
        self.assertIsNone(legacy.windows[0].duration_minutes)
        self.assertEqual(decide(legacy, None, self.cfg, 1, False, 100).pause_at, 2)

    def test_invalid_window_duration_is_unknown(self):
        for duration in (True, 0, -300, 300.5, "300"):
            payload = quota()
            payload["rateLimitsByLimitId"]["codex"]["primary"]["windowDurationMins"] = duration
            with self.subTest(duration=duration):
                self.assertEqual(self.evaluate(payload).mode, "unknown")

    def test_five_hour_threshold_is_configurable_independently(self):
        self.cfg = Config(pause_remaining=1, five_hour_pause_remaining=3)
        self.assertEqual(self.evaluate(quota(97)).pause_at, 3)
        self.assertEqual(self.evaluate(quota(10, 99)).pause_at, 1)

    def test_missing_invalid_and_stale_data_never_means_zero_usage(self):
        for raw in ({}, {"rateLimits": None}, quota(None), quota(float("nan")), quota(True)):
            self.assertEqual(self.evaluate(raw).mode, "unknown")
        snap = Snapshot.parse(quota(), 100)
        self.assertEqual(decide(snap, None, self.cfg, 1, False, 221).mode, "unknown")
        self.assertTrue(Snapshot.parse(quota(), 100, ("absent",)).incomplete)

    def test_reset_time_is_not_evidence_of_recovery(self):
        self.assertEqual(self.evaluate(quota(100, reset=1), latched=True).mode, "protect")

    def test_hysteresis(self):
        self.assertEqual(self.evaluate(quota(82)).mode, "warning")
        self.assertEqual(self.evaluate(quota(82), latched=True).mode, "protect")
        self.assertEqual(self.evaluate(quota(70), latched=True).mode, "normal")

    def test_concurrency_and_burn_raise_reserve(self):
        self.assertEqual(self.evaluate(quota(91), active=3).mode, "protect")
        old = Snapshot.parse(quota(10), 70)
        value = self.evaluate(quota(25), previous=old)
        self.assertEqual(value.pause_at, 60)
        # Never calculate burn across a reset.
        reset = self.evaluate(quota(25, reset=20000), previous=old)
        self.assertEqual(reset.pause_at, 5)

    def test_burn_is_measured_per_window(self):
        old = Snapshot.parse(quota(10, 97), 70)
        decision = self.evaluate(quota(11, 97), previous=old)
        self.assertEqual(decision.mode, "warning")
        self.assertEqual(decision.limiting_window, "codex/secondary")
        self.assertEqual(decision.pause_at, 2)

    def test_recovery_requires_all_windows_to_clear_their_own_hysteresis(self):
        self.assertEqual(self.evaluate(quota(70, 76), latched=True).mode, "protect")
        self.assertEqual(self.evaluate(quota(76, 70), latched=True).mode, "protect")
        self.assertEqual(self.evaluate(quota(75, 75), latched=True).mode, "normal")

    def test_legacy_and_explicit_reached(self):
        self.assertEqual(self.evaluate({"rateLimits": {"primary": {"usedPercent": 5}}}).mode, "normal")
        raw = quota()
        raw["rateLimitsByLimitId"]["codex"]["rateLimitReachedType"] = "weekly"
        self.assertEqual(self.evaluate(raw).mode, "protect")

    def test_configuration_rejects_invalid_thresholds(self):
        for params in ({"poll_seconds": 0}, {"pause_remaining": 30}, {"stale_seconds": 1},
                       {"reserve_per_extra_task": -1}, {"resume_remaining": float("inf")},
                       {"five_hour_pause_remaining": -1}, {"five_hour_pause_remaining": 20},
                       {"five_hour_pause_remaining": True}, {"five_hour_pause_remaining": float("nan")},
                       {"max_pause_remaining": 4}):
            with self.assertRaises(ValueError):
                Config(**params)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home)
        self.cfg = Config()
        self.store.observe_quota(quota(), self.cfg, now=100)

    def tearDown(self):
        self.temp.cleanup()

    def start(self, thread="t", now=101):
        self.store.register(thread, "D:/work", "任务", now=now)
        self.assertTrue(self.store.begin_work(thread, "old-" + thread, self.cfg, now=now))

    def pause(self, thread="t"):
        self.store.observe_quota(quota(97), self.cfg, now=130)
        self.store.close_turn(thread, "old-" + thread, config=self.cfg, now=131)

    def test_full_lifecycle_with_recovery_and_actual_execution(self):
        self.start()
        self.store.checkpoint("t", {"current": "测试尚未完成", "next_step": "读取测试结果"}, "old-t", now=120)
        self.pause()
        self.assertEqual(self.store.task("t")["phase"], "paused")
        self.assertFalse(self.store.request_resume("t", "new", self.cfg, now=132)[0])
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=160)
        self.assertEqual(self.store.task("t")["phase"], "ready")
        self.assertTrue(self.store.request_resume("t", "new", self.cfg, now=161)[0])
        self.assertIsNone(self.store.task("t")["resume_started_at"])
        self.store.begin_work("t", "new", self.cfg, now=162, tool="Bash", executed=True)
        self.assertEqual(self.store.task("t")["resume_started_at"], 162)
        self.assertEqual(self.store.task("t")["phase"], "running")

    def test_old_stop_and_checkpoint_cannot_overwrite_resumed_turn(self):
        self.start()
        self.pause()
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=160)
        self.store.request_resume("t", "new", self.cfg, now=161)
        self.assertFalse(self.store.close_turn("t", "old-t", config=self.cfg, now=162))
        with self.assertRaises(ValueError):
            self.store.checkpoint("t", {"current": "旧", "next_step": "旧"}, "old-t")

    def test_normal_stop_does_not_claim_task_completion_or_quota_pause(self):
        self.start()
        self.store.close_turn("t", "old-t", config=self.cfg, now=102)
        self.assertEqual(self.store.task("t")["phase"], "idle")
        self.assertFalse(self.store.close_turn("t", "old-t", config=self.cfg, now=103))

    def test_resume_without_execution_retains_pause(self):
        self.start()
        self.pause()
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=160)
        self.store.request_resume("t", "new", self.cfg, now=161)
        self.store.close_turn("t", "new", config=self.cfg, now=162)
        self.assertEqual(self.store.task("t")["phase"], "ready")
        self.assertIsNone(self.store.task("t")["resume_started_at"])

    def test_multiple_resumes_are_serialized(self):
        for thread in ("a", "b"):
            self.start(thread)
        self.store.observe_quota(quota(99), self.cfg, now=130)
        for thread in ("a", "b"):
            self.store.close_turn(thread, "old-" + thread, config=self.cfg, now=131)
        self.store.observe_quota(quota(10, reset=30000), self.cfg, now=160)
        self.assertTrue(self.store.request_resume("a", "new-a", self.cfg, now=161)[0])
        self.assertFalse(self.store.request_resume("b", "new-b", self.cfg, now=162)[0])
        self.store.begin_work("a", "new-a", self.cfg, now=163, tool="Bash", executed=True)
        self.assertTrue(self.store.request_resume("b", "new-b", self.cfg, now=177)[0])

    def test_stale_cache_protects_active_task(self):
        self.start()
        self.store.protect_if_stale(self.cfg, now=300)
        self.assertEqual(self.store.task("t")["phase"], "pausing")

    def test_persistence_concurrent_hooks_and_single_daemon(self):
        def worker(index):
            local = Store(self.home)
            local.register(f"t-{index}", now=101)
            local.checkpoint(f"t-{index}", {"current": "已保存", "next_step": "检查"}, now=102)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(30)))
        self.assertEqual(len(Store(self.home).tasks()), 30)
        self.assertTrue(self.store.acquire_lease("one", 30, now=100))
        self.assertFalse(self.store.acquire_lease("two", 30, now=101))
        self.assertTrue(self.store.acquire_lease("two", 30, now=131))
        self.store.release_lease("one")
        self.assertEqual(self.store.get_meta("daemon_lease")["owner"], "two")

    def test_schema_upgrade_keeps_checkpoints_and_is_concurrency_safe(self):
        self.start()
        self.store.checkpoint("t", {"current": "升级前", "next_step": "核对"}, "old-t", now=102)
        with self.store.connect() as db:
            db.execute("ALTER TABLE tasks DROP COLUMN marker_prefix")
            db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: Store(self.home), range(8)))
        self.assertEqual(self.store.get_meta("schema_version"), 2)
        self.assertIn("升级前", self.store.task("t")["checkpoint"])


class TitleTests(unittest.TestCase):
    def test_no_duplicate_prefix_or_unowned_removal(self):
        self.assertEqual(mark_title("任务", "❗ "), ("❗ 任务", True))
        self.assertEqual(mark_title("❗ 用户标题", "❗ "), ("❗ 用户标题", False))
        self.assertEqual(restore_title("❗ 用户标题", "❗ ", False), "❗ 用户标题")

    def test_preserve_manual_rename(self):
        self.assertEqual(restore_title("❗ 新标题", "❗ ", True), "新标题")
        self.assertEqual(restore_title("用户已去掉标记", "❗ ", True), "用户已去掉标记")


if __name__ == "__main__":
    unittest.main()
