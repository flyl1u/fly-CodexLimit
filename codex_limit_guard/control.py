"""A persistent stop switch, independent of quota, Hook trust and MCP health."""

from pathlib import Path
import time

from .files import atomic_json


def disabled(home):
    return (Path(home) / "disabled.json").exists()


def disable(home, reason="用户停用"):
    atomic_json(Path(home) / "disabled.json", {"at": time.time(), "reason": reason})


def enable(home):
    if not (Path(home) / "installation.json").is_file():
        raise ValueError("尚未安装，不能启用保护")
    (Path(home) / "disabled.json").unlink(missing_ok=True)
