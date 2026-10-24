"""Command line entry points. Hooks and MCP reserve stdout for their protocols."""

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading

from . import __version__
from .config import Config, default_home, runtime_environment
from .daemon import run_daemon
from .files import atomic_json
from .hooks import error_output, handle_hook
from .report import export_report
from .store import Store


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser():
    result = argparse.ArgumentParser(description="Codex 本地额度保护、断点和恢复协调")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--home", type=Path, default=None, help="状态目录（也可设 CODEX_LIMIT_GUARD_HOME）")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="创建默认配置，不修改 Codex 配置")
    daemon = commands.add_parser("daemon", help="运行后台监控；Ctrl+C 或 stop 结束")
    daemon.add_argument("--once", action="store_true", help="只查询并协调一次")
    commands.add_parser("stop", help="通知本状态目录的守护程序退出")
    status = commands.add_parser("status", help="查看缓存额度、任务和最近错误")
    status.add_argument("--json", action="store_true")
    doctor = commands.add_parser("doctor", help="检查配置与能力；--live 只读连接真实 Codex")
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--thread", help="只读检查一个已知聊天的元数据及运行时归属")
    commands.add_parser("hook", help=argparse.SUPPRESS)
    commands.add_parser("mcp", help="以 stdio 提供断点 MCP 工具")
    for verb in ("install", "uninstall"):
        command = commands.add_parser(verb, help="安装用户级 Hooks/MCP/Skill" if verb == "install" else "移除本程序管理的配置，保留断点")
        command.add_argument("--codex-home", type=Path, default=None)
        if verb == "install":
            command.add_argument("--dry-run", action="store_true")
    checkpoint = commands.add_parser("checkpoint", help="人工读取或写入断点；模型使用专用 MCP 工具")
    checkpoint.add_argument("--thread", required=True)
    checkpoint.add_argument("--turn")
    checkpoint.add_argument("--input", help="JSON 文件；- 表示 stdin；省略时读取最近断点")
    demo = commands.add_parser("demo", help="演示三任务暂停和恢复，全程不连接 Codex")
    demo.add_argument("--output", type=Path, default=Path(".guard-local/demo"))
    return result


def doctor(home, config, live=False, thread_id=None):
    from .policy import Snapshot
    from .rpc import RpcClient, resolve_command
    import time

    result = {"python": sys.version.split()[0], "version": __version__, "home": str(home),
              "config_valid": True, "resolved_server_command": resolve_command(config.server_command),
              "control_active_turns_enabled": config.control_active_turns,
              "hooks_loaded": "需在目标 Codex 聊天中触发生命周期事件验证；本命令不绕过信任审核",
              "default_mode": "Hooks 协作收尾，独立连接读取账户和持久化标题元数据"}
    if live:
        with RpcClient(config.server_command, timeout=config.rpc_timeout_seconds, env=runtime_environment(home)) as rpc:
            account = rpc.request("account/read", {"refreshToken": False}).get("account")
            result["auth_type"] = account.get("type") if account else None
            snap = Snapshot.parse(rpc.request("account/rateLimits/read", {}), time.time(), config.limit_ids)
            result["quota_readable"] = bool(snap.windows) and not snap.incomplete
            result["quota_buckets"] = sorted({w.bucket for w in snap.windows})
            if thread_id:
                thread = rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
                # Same paginated logic used by the coordinator; no thread/resume call.
                from .daemon import Coordinator
                loaded = Coordinator(None, config, rpc).loaded_ids()
                result["thread"] = {"id": thread_id, "title_available": bool(thread.get("name")),
                                    "runtime_status": thread.get("status"), "same_live_runtime": thread_id in loaded}
    if (home / "installation.json").exists():
        manifest = json.loads((home / "installation.json").read_text(encoding="utf-8"))
        result["installed"] = True
        result["entrypoint_exists"] = Path(manifest["entrypoint"]).is_file()
    else:
        result["installed"] = False
    return result


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    home = (args.home or default_home()).expanduser().resolve()
    event = {}
    try:
        if args.command == "hook":
            event = json.loads(sys.stdin.read(131073))
        config = Config.load(home / "config.json")
        if args.command == "demo":
            from .demo import run_demo
            result = run_demo(args.output)
            for step in result["trace"]:
                print(step["step"] + "：" + ", ".join(f"{t['title']} [{t['phase']}]" for t in step["tasks"]))
            print(f"模型请求 0；完整记录：{args.output.resolve() / 'demo-run.json'}")
            return 0
        if args.command == "doctor":
            result = doctor(home, config, args.live, args.thread)
            emit(result)
            return 1 if args.live and not result.get("quota_readable") else 0
        if args.command in ("install", "uninstall"):
            from .install import install, uninstall
            codex_home = (args.codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))).expanduser().resolve()
            if args.command == "install":
                entrypoint = Path(__file__).resolve().parent.parent / "guard.py"
                if not entrypoint.exists():
                    entrypoint = Path(__file__).with_name("_launcher.py")
                emit(install(home, codex_home, entrypoint, args.dry_run))
            else:
                emit(uninstall(home, codex_home))
            return 0
        store = Store(home)
        if args.command == "init":
            path = home / "config.json"
            if not path.exists():
                atomic_json(path, config.to_dict())
            emit({"home": str(home), "config": str(path), "created": True})
        elif args.command == "hook":
            result = handle_hook(event, store, config)
            if result or event.get("hook_event_name") in ("Stop", "Interrupt"):
                print(json.dumps(result, ensure_ascii=False))
        elif args.command == "mcp":
            from .mcp_server import serve
            serve(store, config, sys.stdin, sys.stdout)
        elif args.command == "daemon":
            stopped = threading.Event()
            signal.signal(signal.SIGINT, lambda *_: stopped.set())
            signal.signal(signal.SIGTERM, lambda *_: stopped.set())
            decision = run_daemon(store, config, stopped, once=args.once)
            if args.once and decision:
                emit(decision.to_dict())
        elif args.command == "stop":
            import time
            store.put_meta("stop_requested_at", time.time())
            emit({"stop_requested": True, "home": str(home)})
        elif args.command == "status":
            decision = store.decision(config)
            tasks = store.tasks()
            health = store.get_meta("health")
            if args.json:
                emit({"decision": decision.to_dict(), "health": health, "task_errors": store.get_meta("task_errors", []),
                      "tasks": [{k: t[k] for k in ("thread_id", "title", "phase", "checkpoint_at", "pause_reason")} for t in tasks]})
            else:
                print(f"额度状态：{decision.mode}；{decision.reason}")
                print(f"守护程序：{json.dumps(health, ensure_ascii=False) if health else '尚未启动'}")
                for task in tasks:
                    print(f"{task['phase']:10} {task['thread_id']}  {task.get('title') or ''}")
        elif args.command == "checkpoint":
            if args.input:
                payload = json.loads(sys.stdin.read(32769) if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig"))
                store.checkpoint(args.thread, payload, args.turn)
                emit({"saved": True, "report": str(export_report(store, store.task(args.thread)))})
            else:
                from .mcp_server import call_tool
                emit(call_tool("read_checkpoint", {"thread_id": args.thread}, store, config))
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error, KeyError) as exc:
        if args.command == "hook":
            print(json.dumps(error_output(event.get("hook_event_name"), str(exc)), ensure_ascii=False))
            return 0
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
