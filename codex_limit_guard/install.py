"""Reversible user-level install, preserving unrelated hooks and TOML settings."""

import base64
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tomllib

from . import __version__
from .files import atomic_json, atomic_text


EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Interrupt", "SessionEnd")
BLOCK_START = "# BEGIN CODEX LIMIT GUARD (managed)"
BLOCK_END = "# END CODEX LIMIT GUARD (managed)"


def read_text(path):
    return path.read_text(encoding="utf-8-sig") if path.exists() else None


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path, text):
    if text is None:
        if path.exists():
            path.unlink()
    else:
        atomic_text(path, text)


def _allowed_paths(home, codex_home):
    return {p.resolve() for p in (
        codex_home / "hooks.json", codex_home / "config.toml",
        codex_home / "skills" / "codex-limit-checkpoint" / "SKILL.md",
        home / "installation.json",
    )}


def recover_install(home, codex_home):
    """Recover an interrupted file transaction without overwriting concurrent edits."""
    journal_path = home / "install-transaction.json"
    if not journal_path.exists():
        return
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    allowed = _allowed_paths(home, codex_home)
    for item in reversed(journal["writes"]):
        path = Path(item["path"]).resolve()
        if path not in allowed:
            raise ValueError("安装事务目录不匹配；请使用原 --codex-home 恢复")
        current = read_text(path)
        if current == item["after"]:
            _write(path, item["before"])
        elif current != item["before"]:
            raise ValueError(f"检测到安装中途的手动修改，保留文件与恢复日志: {path}")
    journal_path.unlink()


def apply_transaction(home, codex_home, changes):
    journal_path = home / "install-transaction.json"
    writes = [{"path": str(path.resolve()), "before": read_text(path), "after": value}
              for path, value in changes if read_text(path) != value]
    if not writes:
        return
    atomic_json(journal_path, {"writes": writes})
    try:
        for item in writes:
            path = Path(item["path"])
            if read_text(path) != item["before"]:
                raise ValueError(f"文件在安装期间发生变化，停止覆盖: {path}")
            _write(path, item["after"])
    except BaseException:
        recover_install(home, codex_home)
        raise
    journal_path.unlink()


def hook_groups(entrypoint, home, python=None):
    argv = [str(python or sys.executable), str(entrypoint), "--home", str(home), "hook"]
    ps_script = ("[Console]::InputEncoding = New-Object System.Text.UTF8Encoding; "
                 "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; "
                 "$OutputEncoding = [Console]::OutputEncoding; & "
                 + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv))
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    groups = {}
    for event in EVENTS:
        handler = {"type": "command", "command": shlex.join(argv),
                   "commandWindows": f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}",
                   "timeout": 3, "statusMessage": "Codex Limit Guard"}
        if event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
            handler["additionalContextLimit"] = 1000
        groups[event] = {"hooks": [handler]}
    return groups


def remove_groups(document, groups):
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("现有 hooks.json 的 hooks 不是对象")
    for event, managed in groups.items():
        values = hooks.get(event, [])
        if not isinstance(values, list):
            raise ValueError(f"现有 {event} 不是 Hook 数组")
        if managed not in values:
            raise ValueError(f"已安装的 {event} Hook 被手动修改或移除；保留现场，请先核对 hooks.json")
        values.remove(managed)
        if not values:
            hooks.pop(event, None)
    if not hooks:
        document.pop("hooks", None)


def install(home, codex_home, entrypoint, dry_run=False):
    home, codex_home, entrypoint = Path(home).resolve(), Path(codex_home).resolve(), Path(entrypoint).resolve()
    if not entrypoint.is_file():
        raise ValueError("guard.py 不存在，不能安装")
    if not dry_run:
        home.mkdir(parents=True, exist_ok=True)
        recover_install(home, codex_home)
    manifest_path = home / "installation.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    if previous and Path(previous["codex_home"]).resolve() != codex_home:
        raise ValueError("此状态目录已经绑定另一个 Codex 配置目录，请使用不同 --home")
    hooks_path, config_path = codex_home / "hooks.json", codex_home / "config.toml"
    hooks_raw, config_raw = read_text(hooks_path), read_text(config_path)
    hooks_doc = json.loads(hooks_raw) if hooks_raw is not None else {}
    if not isinstance(hooks_doc, dict):
        raise ValueError("hooks.json 必须是对象")
    config = config_raw or ""
    if previous:
        remove_groups(hooks_doc, previous["groups"])
        block = previous["mcp_block"]
        if block not in config:
            raise ValueError("已安装的 MCP 配置块被修改；保留手动修改，未执行覆盖")
        config = config.replace(block, "", 1)
    if "codex_limit_guard" in tomllib.loads(config).get("mcp_servers", {}):
        raise ValueError("已存在非本程序管理的 codex_limit_guard MCP 配置，未覆盖")
    if BLOCK_START in config:
        raise ValueError("存在未知的 Limit Guard 配置块，请先核对")
    if tomllib.loads(config).get("features", {}).get("hooks") is False:
        raise ValueError("当前 features.hooks=false；请在 Codex 配置中启用 Hooks 后再安装")
    groups = hook_groups(entrypoint, home)
    active = hooks_doc.setdefault("hooks", {})
    if not isinstance(active, dict):
        raise ValueError("hooks 字段不是对象")
    for event, group in groups.items():
        active.setdefault(event, []).append(group)
    args = [str(entrypoint), "--home", str(home), "mcp"]
    block = (f"\n{BLOCK_START}\n[mcp_servers.codex_limit_guard]\n"
             f"command = {json.dumps(sys.executable, ensure_ascii=False)}\n"
             f"args = {json.dumps(args, ensure_ascii=False)}\n"
             f"startup_timeout_sec = 10\n{BLOCK_END}\n")
    config += block
    tomllib.loads(config)
    skill_source = Path(__file__).parent / "skill" / "SKILL.md"
    skill = skill_source.read_text(encoding="utf-8")
    skill_path = codex_home / "skills" / "codex-limit-checkpoint" / "SKILL.md"
    existing_skill = read_text(skill_path)
    if existing_skill is not None and (not previous or fingerprint(existing_skill) != previous["skill_hash"]):
        raise ValueError("同名 Skill 已存在或被手动修改，未覆盖")
    manifest = {"version": __version__, "codex_home": str(codex_home), "entrypoint": str(entrypoint),
                "groups": groups, "mcp_block": block, "skill_hash": fingerprint(skill),
                "original_hooks": previous["original_hooks"] if previous else hooks_raw,
                "original_config": previous["original_config"] if previous else config_raw}
    result = {"codex_home": str(codex_home), "home": str(home), "hook_events": list(EVENTS),
              "skill": str(skill_path), "trust_required": True, "dry_run": dry_run}
    if not dry_run:
        # Keep the original inputs in a durable, local backup before editing.
        if not (home / "install-backup.json").exists():
            atomic_json(home / "install-backup.json", {"hooks": hooks_raw, "config": config_raw})
        apply_transaction(home, codex_home, [
            (hooks_path, json.dumps(hooks_doc, ensure_ascii=False, indent=2) + "\n"),
            (config_path, config), (skill_path, skill),
            (manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"),
        ])
    return result


def uninstall(home, codex_home):
    home, codex_home = Path(home).resolve(), Path(codex_home).resolve()
    recover_install(home, codex_home)
    manifest_path = home / "installation.json"
    if not manifest_path.exists():
        return {"removed": False, "reason": "没有本程序的安装记录"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if Path(manifest["codex_home"]).resolve() != codex_home:
        raise ValueError("--codex-home 与安装记录不同，未更改文件")
    hooks_path, config_path = codex_home / "hooks.json", codex_home / "config.toml"
    hooks = json.loads(read_text(hooks_path) or "{}")
    remove_groups(hooks, manifest["groups"])
    original_raw = manifest["original_hooks"]
    original = json.loads(original_raw) if original_raw is not None else {}
    new_hooks = original_raw if hooks == original else json.dumps(hooks, ensure_ascii=False, indent=2) + "\n"
    config = read_text(config_path) or ""
    if manifest["mcp_block"] not in config:
        raise ValueError("MCP 配置块被修改，未覆盖手动编辑")
    config = config.replace(manifest["mcp_block"], "", 1)
    new_config = None if not config and manifest["original_config"] is None else config
    skill_path = codex_home / "skills" / "codex-limit-checkpoint" / "SKILL.md"
    skill = read_text(skill_path)
    keep_skill = skill is not None and fingerprint(skill) != manifest["skill_hash"]
    changes = [(hooks_path, new_hooks), (config_path, new_config), (manifest_path, None)]
    if not keep_skill:
        changes.append((skill_path, None))
    apply_transaction(home, codex_home, changes)
    return {"removed": True, "kept_edited_skill": keep_skill,
            "state_preserved": str(home), "note": "断点和安装备份保留；运行中的守护程序请执行 stop"}
