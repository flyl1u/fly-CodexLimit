"""Validated configuration and paths; importing this module has no side effects."""

from dataclasses import asdict, dataclass, fields
from pathlib import Path
import json
import math
import os


def default_home() -> Path:
    explicit = os.environ.get("CODEX_LIMIT_GUARD_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "limit-guard"


def runtime_environment(home: Path) -> dict:
    manifest_path = Path(home) / "installation.json"
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"CODEX_HOME": str(Path(manifest["codex_home"]).resolve())}


@dataclass(frozen=True)
class Config:
    pause_remaining: float = 2
    five_hour_pause_remaining: float = 5
    resume_remaining: float = 25
    reserve_per_extra_task: float = 2
    adaptive_reserve: bool = False
    max_pause_remaining: float = 60
    poll_seconds: float = 30
    grace_seconds: float = 90
    stale_seconds: float = 120
    resume_spacing_seconds: float = 15
    resume_slot_seconds: float = 120
    rpc_timeout_seconds: float = 15
    title_prefix: str = "❗ "
    limit_ids: tuple[str, ...] = ()
    # Spawned stdio is an account/metadata connection, never an existing runtime.
    server_command: tuple[str, ...] = ("codex", "app-server")
    control_active_turns: bool = False

    def __post_init__(self):
        for name in (
            "pause_remaining", "five_hour_pause_remaining", "resume_remaining",
            "reserve_per_extra_task", "max_pause_remaining", "poll_seconds",
            "grace_seconds", "stale_seconds", "resume_spacing_seconds",
            "resume_slot_seconds", "rpc_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} 必须是有限数字")
        if not 0 <= self.pause_remaining < self.resume_remaining <= 100:
            raise ValueError("阈值必须满足 0 <= pause < resume <= 100")
        if not 0 <= self.five_hour_pause_remaining < self.resume_remaining:
            raise ValueError("5 小时阈值必须满足 0 <= five_hour_pause < resume")
        if not max(self.pause_remaining, self.five_hour_pause_remaining) <= self.max_pause_remaining < 95:
            raise ValueError("max_pause_remaining 必须不低于两种收尾线且小于 95")
        if self.reserve_per_extra_task < 0:
            raise ValueError("reserve_per_extra_task 不能为负")
        if min(self.poll_seconds, self.grace_seconds, self.resume_slot_seconds, self.rpc_timeout_seconds) <= 0:
            raise ValueError("轮询、收尾、恢复和 RPC 超时必须大于 0")
        if self.stale_seconds < self.poll_seconds * 2 or self.resume_spacing_seconds < 0:
            raise ValueError("缓存有效期至少覆盖两次轮询，恢复间隔不能为负")
        if not isinstance(self.title_prefix, str) or not self.title_prefix.strip() or len(self.title_prefix) > 20:
            raise ValueError("title_prefix 必须是 1–20 字符的非空标记")
        if not isinstance(self.control_active_turns, bool):
            raise ValueError("control_active_turns 必须是布尔值")
        if not isinstance(self.adaptive_reserve, bool):
            raise ValueError("adaptive_reserve 必须是布尔值")
        for name in ("limit_ids", "server_command"):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError(f"{name} 必须是字符串数组")
        if not self.server_command:
            raise ValueError("server_command 不能为空")

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        # Silently migrate old configuration; quota reminders no longer exist.
        data.pop("warn_remaining", None)
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"未知配置项: {', '.join(sorted(unknown))}")
        for key in ("limit_ids", "server_command"):
            if key in data and isinstance(data[key], list):
                data[key] = tuple(data[key])
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)
