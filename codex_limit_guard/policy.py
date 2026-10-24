"""Pure quota parsing and hysteretic, concurrency-aware protection decisions."""

from dataclasses import asdict, dataclass
import math

from .config import Config


@dataclass(frozen=True)
class Window:
    bucket: str
    name: str
    used: float
    resets_at: int | None
    duration_minutes: int | None = None

    @property
    def remaining(self) -> float:
        return max(0.0, 100.0 - self.used)

    @property
    def key(self) -> tuple:
        return self.bucket, self.name, self.resets_at, self.duration_minutes


@dataclass(frozen=True)
class Snapshot:
    observed_at: float
    windows: tuple[Window, ...]
    incomplete: bool = False
    reached: bool = False

    @classmethod
    def parse(cls, payload: dict, now: float, limit_ids=()) -> "Snapshot":
        if not isinstance(payload, dict):
            return cls(now, (), True)
        buckets = payload.get("rateLimitsByLimitId")
        if not isinstance(buckets, dict) or not buckets:
            legacy = payload.get("rateLimits")
            buckets = {legacy.get("limitId") or "codex": legacy} if isinstance(legacy, dict) else {}
        wanted = set(limit_ids) if limit_ids else set(buckets)
        incomplete = not wanted or bool(wanted - set(buckets))
        windows = []
        reached = False
        for bucket in sorted(wanted):
            raw = buckets.get(bucket)
            if not isinstance(raw, dict):
                incomplete = True
                continue
            reached = reached or bool(raw.get("rateLimitReachedType"))
            found = False
            for name in ("primary", "secondary"):
                value = raw.get(name)
                if value is None:
                    continue
                used = value.get("usedPercent") if isinstance(value, dict) else None
                if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
                    incomplete = True
                    continue
                reset = value.get("resetsAt")
                reset = reset if isinstance(reset, int) and not isinstance(reset, bool) else None
                duration = value.get("windowDurationMins")
                if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0):
                    incomplete = True
                    continue
                windows.append(Window(bucket, name, float(used), reset, duration))
                found = True
            incomplete = incomplete or not found
        return cls(now, tuple(windows), incomplete, reached)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        return cls(data["observed_at"], tuple(Window(**w) for w in data["windows"]),
                   data.get("incomplete", False), data.get("reached", False))


@dataclass(frozen=True)
class Decision:
    mode: str
    reason: str
    remaining: float | None
    pause_at: float
    resume_at: float
    observed_at: float
    limiting_window: str | None = None

    @property
    def protected(self) -> bool:
        return self.mode in ("protect", "unknown")

    def to_dict(self) -> dict:
        return asdict(self)


def decide(snapshot: Snapshot | None, previous: Snapshot | None, config: Config,
           active_tasks: int, latched: bool, now: float) -> Decision:
    extra = max(0, active_tasks - 1) * config.reserve_per_extra_task if config.adaptive_reserve else 0
    pause_at = min(config.max_pause_remaining, config.pause_remaining + extra)
    resume_at = min(99.0, max(config.resume_remaining, pause_at + 5))
    observed = snapshot.observed_at if snapshot else 0
    if snapshot is None or now - observed > config.stale_seconds or observed > now + 5:
        return Decision("unknown", "额度缓存缺失或已过期，等待账户查询", None, pause_at, resume_at, observed)
    if snapshot.incomplete or not snapshot.windows:
        return Decision("unknown", "额度数据不完整，不能确认可以继续", None, pause_at, resume_at, observed)
    elapsed = snapshot.observed_at - previous.observed_at if previous else 0
    old = {w.key: w.used for w in previous.windows} if previous and 0 < elapsed <= config.stale_seconds else {}
    decisions = []
    for window in snapshot.windows:
        # Duration identifies a 5-hour window even if primary/secondary are swapped.
        # Missing duration, including old cached samples, uses the weekly fallback.
        base = config.five_hour_pause_remaining if window.duration_minutes == 300 else config.pause_remaining
        burn = (max(0, window.used - old[window.key]) / elapsed
                if config.adaptive_reserve and window.key in old else 0)
        pause_at = min(config.max_pause_remaining, base + extra + burn * (config.poll_seconds + config.grace_seconds))
        resume_at = min(99.0, max(config.resume_remaining, pause_at + 5))
        remaining = window.remaining
        label = f"{window.bucket}/{window.name}"
        if snapshot.reached or remaining <= pause_at or (latched and remaining < resume_at):
            mode, reason = "protect", f"{label} 剩余 {remaining:g}%，保留收尾额度"
        elif remaining <= max(config.warn_remaining, pause_at + 5):
            mode, reason = "warning", f"{label} 剩余 {remaining:g}%，建议保存进度；仍可继续任务"
        else:
            mode, reason = "normal", "额度充足"
        decisions.append(Decision(mode, reason, remaining, pause_at, resume_at, observed, label))
    # Compare every window against its own threshold before choosing the cause.
    priority = {"protect": 0, "warning": 1, "normal": 2}
    return min(decisions, key=lambda d: (priority[d.mode], d.remaining - (d.resume_at if latched else d.pause_at)))
