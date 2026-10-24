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

    @property
    def remaining(self) -> float:
        return max(0.0, 100.0 - self.used)

    @property
    def key(self) -> tuple:
        return self.bucket, self.name, self.resets_at


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
                windows.append(Window(bucket, name, float(used), reset))
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
    extra = max(0, active_tasks - 1) * config.reserve_per_extra_task
    burn = 0.0
    if snapshot and previous and 0 < snapshot.observed_at - previous.observed_at <= config.stale_seconds:
        elapsed = snapshot.observed_at - previous.observed_at
        old = {w.key: w.used for w in previous.windows}
        burn = max((max(0, w.used - old.get(w.key, w.used)) / elapsed for w in snapshot.windows), default=0)
    pause_at = min(config.max_pause_remaining,
                   config.pause_remaining + extra + burn * (config.poll_seconds + config.grace_seconds))
    resume_at = min(99.0, max(config.resume_remaining, pause_at + 5))
    observed = snapshot.observed_at if snapshot else 0
    if snapshot is None or now - observed > config.stale_seconds or observed > now + 5:
        return Decision("unknown", "额度缓存缺失或已过期，等待账户查询", None, pause_at, resume_at, observed)
    if snapshot.incomplete or not snapshot.windows:
        return Decision("unknown", "额度数据不完整，不能确认可以继续", None, pause_at, resume_at, observed)
    limiting = min(snapshot.windows, key=lambda w: w.remaining)
    remaining = limiting.remaining
    label = f"{limiting.bucket}/{limiting.name}"
    if snapshot.reached or remaining <= pause_at or (latched and remaining < resume_at):
        return Decision("protect", f"{label} 剩余 {remaining:g}%，保留收尾额度", remaining,
                        pause_at, resume_at, observed, label)
    if remaining <= max(config.warn_remaining, pause_at + 5):
        return Decision("warning", f"{label} 剩余 {remaining:g}%，暂停新增并行任务", remaining,
                        pause_at, resume_at, observed, label)
    return Decision("normal", "额度充足", remaining, pause_at, resume_at, observed, label)
