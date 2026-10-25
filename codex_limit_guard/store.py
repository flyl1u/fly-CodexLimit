"""Small transactional state machine shared by daemon, CLI and concurrent hooks."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

from .config import Config
from .policy import Decision, Snapshot, decide
from .control import disabled


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    thread_id TEXT PRIMARY KEY, cwd TEXT NOT NULL DEFAULT '', title TEXT,
    phase TEXT NOT NULL DEFAULT 'idle', turn_id TEXT,
    last_event_at REAL NOT NULL, pause_requested_at REAL, pause_reason TEXT,
    pause_turn_id TEXT, closed_at REAL, closure_kind TEXT,
    checkpoint TEXT, checkpoint_at REAL, checkpoint_turn_id TEXT,
    resume_requested_at REAL, resume_turn_id TEXT, resume_started_at REAL,
    marker_owned INTEGER NOT NULL DEFAULT 0, marker_title TEXT,
    marker_prefix TEXT,
    title_action TEXT, marker_done INTEGER NOT NULL DEFAULT 0,
    steer_turn_id TEXT, interrupt_turn_id TEXT,
    last_tool TEXT, last_tool_at REAL
);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY, observed_at REAL NOT NULL, snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, at REAL NOT NULL, thread_id TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS hook_receipts (
    thread_id TEXT NOT NULL, event TEXT NOT NULL, turn_id TEXT,
    received_at REAL NOT NULL, PRIMARY KEY(thread_id,event)
);
"""


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, home: Path):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "guard.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            version = self._meta(db, "schema_version", 2)
            if version not in (1, 2):
                raise ValueError(f"不支持的数据库版本: {version}")
            if "marker_prefix" not in {r[1] for r in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN marker_prefix TEXT")
            self._put(db, "schema_version", 2)

    def _open(self):
        db = sqlite3.connect(self.path, timeout=1.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=1000")
        return db

    @contextmanager
    def connect(self):
        db = self._open()
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _meta(db, key, default=None):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _put(db, key, value):
        db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, encode(value)))

    @staticmethod
    def _event(db, now, thread, kind, detail=None):
        db.execute("INSERT INTO events(at,thread_id,kind,detail) VALUES (?,?,?,?)",
                   (now, thread, kind, encode(detail) if detail is not None else None))
        db.execute("DELETE FROM events WHERE id < (SELECT MAX(id)-2000 FROM events)")

    def get_meta(self, key, default=None):
        with self.connect() as db:
            return self._meta(db, key, default)

    def put_meta(self, key, value):
        with self.transaction() as db:
            self._put(db, key, value)

    def task(self, thread_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
            return dict(row) if row else None

    def tasks(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY last_event_at DESC")]

    def record_hook(self, thread_id, event, turn_id=None, now=None):
        """Only the actual Hook entry point writes these receipts, never MCP calls."""
        now = time.time() if now is None else now
        with self.transaction() as db:
            db.execute("""INSERT INTO hook_receipts VALUES (?,?,?,?)
                ON CONFLICT(thread_id,event) DO UPDATE SET
                turn_id=excluded.turn_id,received_at=excluded.received_at
                WHERE excluded.received_at >= hook_receipts.received_at""",
                       (thread_id, event, turn_id, now))

    def hook_coverage(self, thread_id, now=None, stale_seconds=120):
        now = time.time() if now is None else now
        task = self.task(thread_id)
        with self.connect() as db:
            receipts = {r["event"]: dict(r) for r in db.execute(
                "SELECT * FROM hook_receipts WHERE thread_id=?", (thread_id,))}
        turn = task.get("turn_id") if task else None
        current = {name: r for name, r in receipts.items() if turn and r["turn_id"] == turn}
        prompt = current.get("UserPromptSubmit")
        tool = current.get("PreToolUse")
        verified = bool(prompt and tool and task["closed_at"] is None
                        and tool["received_at"] >= prompt["received_at"]
                        and 0 <= now - tool["received_at"] <= stale_seconds)
        return {"thread_id": thread_id, "turn_id": turn, "verified": verified,
                "state": "verified" if verified else "unverified",
                "reason": "本回合已收到提交和工具前置回调" if verified else
                          "缺少本回合真实回调；安装、信任和旧回调不能证明本回合受保护",
                "receipts": receipts}

    def protection_summary(self, config, now=None):
        active = [t for t in self.tasks() if t["phase"] in ("running", "resuming", "pausing")]
        unverified = [t["thread_id"] for t in active
                      if not self.hook_coverage(t["thread_id"], now, config.stale_seconds)["verified"]]
        return {"mode": "disabled" if disabled(self.home) else "cooperative", "registered_active": len(active),
                "unverified_threads": unverified,
                "callbacks_verified": bool(active) and not unverified,
                "runtime_control_requested": config.control_active_turns}

    def update_task(self, thread_id, **values):
        if not values:
            return
        # Column names are validated rather than accepting SQL from callers.
        with self.transaction() as db:
            allowed = {r[1] for r in db.execute("PRAGMA table_info(tasks)")} - {"thread_id"}
            if set(values) - allowed:
                raise ValueError("未知任务字段")
            db.execute(f"UPDATE tasks SET {','.join(k+'=?' for k in values)} WHERE thread_id=?",
                       (*values.values(), thread_id))

    def register(self, thread_id, cwd="", title=None, now=None):
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 200:
            raise ValueError("无效 thread_id")
        now = time.time() if now is None else now
        with self.transaction() as db:
            db.execute("""INSERT INTO tasks(thread_id,cwd,title,last_event_at) VALUES (?,?,?,?)
                ON CONFLICT(thread_id) DO UPDATE SET
                cwd=CASE WHEN excluded.cwd!='' THEN excluded.cwd ELSE tasks.cwd END,
                title=COALESCE(excluded.title,tasks.title),last_event_at=excluded.last_event_at""",
                       (thread_id, cwd, title, now))

    @staticmethod
    def _decision(db, config, now):
        raw = Store._meta(db, "decision")
        if raw and 0 <= now - raw["observed_at"] <= config.stale_seconds:
            return Decision(**raw)
        return decide(None, None, config, 0, True, now)

    def decision(self, config, now=None):
        now = time.time() if now is None else now
        if disabled(self.home):
            return Decision("disabled", "用户已停用保护", None, config.pause_remaining,
                            config.resume_remaining, now)
        with self.connect() as db:
            return self._decision(db, config, now)

    @staticmethod
    def _release_unverified_hold(db, thread_id, decision, now):
        if decision.protected:
            return
        row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
        if not row or row["phase"] not in ("pausing", "paused", "ready"):
            return
        reason = row["pause_reason"] or ""
        monitor_fault = any(s in reason for s in ("缓存", "数据不完整", "本地检查失败"))
        if decision.mode != "unknown" and not monitor_fault:
            return
        # Release the guard's hold, without claiming that the model or command stopped.
        phase = "running" if row["closed_at"] is None else "idle"
        db.execute("""UPDATE tasks SET phase=?,pause_reason=NULL,pause_requested_at=NULL,
            pause_turn_id=NULL,resume_requested_at=NULL,resume_turn_id=NULL,
            steer_turn_id=NULL,interrupt_turn_id=NULL WHERE thread_id=?""", (phase, thread_id))
        Store._event(db, now, thread_id, "unverified_pause_released", {"reason": reason})

    def release_unverified_holds(self, config, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            decision = self._decision(db, config, now)
            for row in db.execute("SELECT thread_id FROM tasks WHERE phase IN ('pausing','paused','ready')").fetchall():
                self._release_unverified_hold(db, row[0], decision, now)

    @staticmethod
    def _pause(db, thread_id, reason, now):
        row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
        if row and row["phase"] in ("running", "resuming"):
            db.execute("""UPDATE tasks SET phase='pausing',pause_requested_at=?,pause_reason=?,
                pause_turn_id=turn_id,closed_at=NULL,closure_kind=NULL,
                resume_requested_at=NULL,resume_started_at=NULL,
                steer_turn_id=NULL,interrupt_turn_id=NULL WHERE thread_id=?""", (now, reason, thread_id))
            Store._event(db, now, thread_id, "pause_requested", {"reason": reason})

    def observe_quota(self, payload, config, now=None):
        now = time.time() if now is None else now
        snapshot = Snapshot.parse(payload, now, config.limit_ids)
        with self.transaction() as db:
            old_row = db.execute("SELECT snapshot FROM samples ORDER BY id DESC LIMIT 1").fetchone()
            previous = Snapshot.from_dict(json.loads(old_row[0])) if old_row else None
            old_decision = self._meta(db, "decision", {})
            policy = {key: getattr(config, key) for key in (
                "pause_remaining", "five_hour_pause_remaining", "resume_remaining", "adaptive_reserve",
                "reserve_per_extra_task", "max_pause_remaining", "poll_seconds", "grace_seconds", "limit_ids")}
            policy["limit_ids"] = list(policy["limit_ids"])
            # Changing protection policy must not inherit a latch from an old threshold.
            same_policy = self._meta(db, "quota_policy") == policy
            latched = same_policy and self._meta(db, "quota_latched", old_decision.get("mode") == "protect")
            active = db.execute("SELECT COUNT(*) FROM tasks WHERE phase IN ('running','resuming','pausing')").fetchone()[0]
            decision = decide(snapshot, previous, config, active, latched, now)
            db.execute("INSERT INTO samples(observed_at,snapshot) VALUES (?,?)", (now, encode(snapshot.to_dict())))
            db.execute("DELETE FROM samples WHERE id < (SELECT MAX(id)-100 FROM samples)")
            self._put(db, "decision", decision.to_dict())
            self._put(db, "quota_policy", policy)
            # An outage never manufactures a quota-recovery latch or a business pause.
            self._put(db, "quota_latched", latched if decision.mode == "unknown" else decision.mode == "protect")
            if old_decision.get("mode") != decision.mode:
                self._event(db, now, None, "quota_mode", decision.to_dict())
            if decision.protected:
                for row in db.execute("SELECT thread_id FROM tasks WHERE phase IN ('running','resuming')").fetchall():
                    self._pause(db, row[0], decision.reason, now)
                db.execute("UPDATE tasks SET phase='paused' WHERE phase='ready'")
            else:
                for row in db.execute("SELECT thread_id FROM tasks WHERE phase IN ('pausing','paused','ready')").fetchall():
                    self._release_unverified_hold(db, row[0], decision, now)
                db.execute("UPDATE tasks SET phase='ready' WHERE phase='paused'")
            return decision

    def protect_if_stale(self, config, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            decision = self._decision(db, config, now)
            self._put(db, "decision", decision.to_dict())
            if decision.protected:
                for row in db.execute("SELECT thread_id FROM tasks WHERE phase IN ('running','resuming')").fetchall():
                    self._pause(db, row[0], decision.reason, now)
                db.execute("UPDATE tasks SET phase='paused' WHERE phase='ready'")
            else:
                for row in db.execute("SELECT thread_id FROM tasks WHERE phase IN ('pausing','paused','ready')").fetchall():
                    self._release_unverified_hold(db, row[0], decision, now)
            return decision

    def begin_work(self, thread_id, turn_id, config, now=None, tool=None, executed=False, user_submitted=False):
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None:
                raise ValueError("任务尚未注册")
            decision = self._decision(db, config, now)
            self._release_unverified_hold(db, thread_id, decision, now)
            row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
            if row["phase"] in ("paused", "ready", "pausing"):
                return False
            if (not user_submitted and row["phase"] in ("running", "resuming")
                    and turn_id and row["turn_id"] and turn_id != row["turn_id"]):
                return False
            if row["phase"] != "resuming":
                db.execute("UPDATE tasks SET phase='running',turn_id=?,closed_at=NULL WHERE thread_id=?",
                           (turn_id or row["turn_id"], thread_id))
            elif user_submitted and turn_id:
                db.execute("UPDATE tasks SET turn_id=?,resume_turn_id=?,closed_at=NULL WHERE thread_id=?",
                           (turn_id, turn_id, thread_id))
            if decision.protected:
                self._pause(db, thread_id, decision.reason, now)
                return False
            if tool and executed:
                db.execute("UPDATE tasks SET last_tool=?,last_tool_at=?,last_event_at=? WHERE thread_id=?",
                           (tool[:200], now, now, thread_id))
                if row["phase"] == "resuming" or row["marker_owned"]:
                    db.execute("""UPDATE tasks SET phase='running',turn_id=?,resume_started_at=?,
                        resume_requested_at=NULL WHERE thread_id=?""", (turn_id, now, thread_id))
                    self._event(db, now, thread_id, "resume_started")
            return True

    def request_resume(self, thread_id, turn_id, config, now=None):
        """A user submission grants one slot. Blocked submissions stay queued for retry."""
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
            decision = self._decision(db, config, now)
            self._release_unverified_hold(db, thread_id, decision, now)
            row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None:
                raise ValueError("任务尚未注册")
            if decision.protected:
                return False, decision.reason
            if row["phase"] == "pausing":
                if not turn_id or turn_id == row["turn_id"]:
                    return False, "上一回合仍在收尾，等待 Stop/Interrupt 确认后再继续"
                # A new user turn after recovery is lifecycle evidence that the old
                # turn ended, even when its closing Hook never reached this store.
                db.execute("""UPDATE tasks SET phase='ready',closed_at=?,closure_kind='new_user_turn'
                    WHERE thread_id=?""", (now, thread_id))
                self._event(db, now, thread_id, "turn_closed", {"kind": "new_user_turn", "phase": "ready"})
                row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
            if row["phase"] not in ("paused", "ready"):
                return True, ""
            # Expired queued requests don't reserve slots indefinitely.
            db.execute("UPDATE tasks SET resume_requested_at=NULL WHERE phase IN ('ready','paused') AND resume_requested_at<?",
                       (now - config.resume_slot_seconds,))
            db.execute("UPDATE tasks SET resume_requested_at=COALESCE(resume_requested_at,?) WHERE thread_id=?",
                       (now, thread_id))
            head = db.execute("""SELECT thread_id FROM tasks WHERE phase IN ('ready','paused')
                AND resume_requested_at IS NOT NULL ORDER BY resume_requested_at,thread_id LIMIT 1""").fetchone()
            last = self._meta(db, "last_resume_at")
            warming = db.execute("SELECT COUNT(*) FROM tasks WHERE phase='resuming' AND last_event_at>?",
                                 (now - config.resume_slot_seconds,)).fetchone()[0]
            if head[0] != thread_id or warming or (last is not None and now - last < config.resume_spacing_seconds):
                return False, "续做已排队；等待其他任务开始执行及恢复间隔后，再次发送继续"
            db.execute("""UPDATE tasks SET phase='resuming',resume_turn_id=?,turn_id=?,last_event_at=?,
                resume_started_at=NULL,closed_at=NULL,steer_turn_id=NULL,interrupt_turn_id=NULL
                WHERE thread_id=?""", (turn_id, turn_id, now, thread_id))
            self._put(db, "last_resume_at", now)
            self._event(db, now, thread_id, "resume_admitted")
            return True, ""

    def begin_checkpoint_turn(self, thread_id, turn_id, reason, now=None):
        """Accept a user message during protection without admitting business work."""
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None:
                raise ValueError("任务尚未注册")
            if row["phase"] == "pausing" and row["turn_id"] == turn_id:
                return
            db.execute("""UPDATE tasks SET phase='pausing',turn_id=?,pause_turn_id=?,
                pause_requested_at=?,pause_reason=?,closed_at=NULL,closure_kind=NULL,
                resume_requested_at=NULL,resume_started_at=NULL WHERE thread_id=?""",
                       (turn_id, turn_id, now, reason, thread_id))
            self._event(db, now, thread_id, "checkpoint_turn", {"turn_id": turn_id})

    def request_checkpoint_handoff(self, thread_id, turn_id, now=None):
        """At most one Stop continuation per turn, including duplicate hook calls."""
        now = time.time() if now is None else now
        with self.transaction() as db:
            key = f"checkpoint_handoff:{thread_id}"
            previous = self._meta(db, key)
            if previous and previous["turn_id"] == turn_id:
                return False
            self._put(db, key, {"turn_id": turn_id, "at": now})
            self._event(db, now, thread_id, "checkpoint_handoff", {"turn_id": turn_id})
            return True

    def checkpoint(self, thread_id, data, turn_id=None, now=None):
        if not isinstance(data, dict):
            raise ValueError("断点必须是 JSON 对象")
        allowed = {"completed", "current", "remaining", "next_step", "validation", "files", "commands", "notes"}
        if set(data) - allowed or not isinstance(data.get("current"), str) or not isinstance(data.get("next_step"), str):
            raise ValueError("断点需包含字符串 current/next_step，其他字段请使用约定的断点字段")
        for key in ("completed", "remaining", "validation", "files", "commands"):
            if key in data and (not isinstance(data[key], list) or any(not isinstance(v, str) for v in data[key])):
                raise ValueError(f"{key} 必须是字符串数组")
        if "notes" in data and not isinstance(data["notes"], str):
            raise ValueError("notes 必须是字符串")
        serialized = encode(data)
        if len(serialized.encode("utf-8")) > 32768:
            raise ValueError("断点最大 32 KiB；请保存简短摘要")
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT turn_id FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
            if not row:
                raise ValueError("任务尚未注册")
            if turn_id and row[0] and turn_id != row[0]:
                raise ValueError("断点来自过期回合，未覆盖最新记录")
            db.execute("UPDATE tasks SET checkpoint=?,checkpoint_at=?,checkpoint_turn_id=? WHERE thread_id=?",
                       (serialized, now, turn_id or row[0], thread_id))
            self._event(db, now, thread_id, "checkpoint_saved")

    def close_turn(self, thread_id, turn_id=None, kind="stopped", config=None, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None or (turn_id and row["turn_id"] and turn_id != row["turn_id"]):
                return False
            if row["closed_at"] is not None:
                return False
            protected = config is None or self._decision(db, config, now).protected
            phase = ("paused" if protected else "ready") if row["phase"] == "pausing" else "idle"
            # A resume that has not demonstrably executed work keeps its pause marker.
            if row["phase"] == "resuming":
                phase = "paused" if protected else "ready"
            db.execute("UPDATE tasks SET phase=?,closed_at=?,closure_kind=?,last_event_at=? WHERE thread_id=?",
                       (phase, now, kind, now, thread_id))
            self._event(db, now, thread_id, "turn_closed", {"kind": kind, "phase": phase})
            return True

    def acquire_lease(self, owner, ttl, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            lease = self._meta(db, "daemon_lease", {})
            if lease.get("owner") != owner and lease.get("expires_at", 0) > now:
                from .service import process_alive
                if not lease.get("pid") or process_alive(lease["pid"]):
                    return False
            self._put(db, "daemon_lease", {"owner": owner, "pid": os.getpid(), "expires_at": now + ttl})
            return True

    def release_lease(self, owner):
        with self.transaction() as db:
            if self._meta(db, "daemon_lease", {}).get("owner") == owner:
                db.execute("DELETE FROM meta WHERE key='daemon_lease'")

    def events(self, limit=30):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]
