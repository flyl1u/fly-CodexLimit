"""Codex JSONL transport. No shell, model turns, login or approval side effects."""

from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading

from . import __version__


class RpcError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def resolve_command(command) -> list[str]:
    """Resolve the npm Windows shim without passing user strings through cmd.exe."""
    argv = list(command)
    executable = shutil.which(argv[0])
    if executable is None:
        raise RpcError(f"找不到程序: {argv[0]}，请配置 server_command")
    path = Path(executable)
    if os.name == "nt" and path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        entry = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if path.stem.lower() == "codex" and entry.is_file() and node:
            return [node, str(entry), *argv[1:]]
        raise RpcError("server_command 请使用实际 .exe 或 [python, script.py]，不执行 shell 包装器")
    return [str(path), *argv[1:]]


class RpcClient:
    def __init__(self, command, timeout=15, env=None):
        self.command = resolve_command(command)
        self.timeout = timeout
        self.env = dict(os.environ, **(env or {}), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        self.process = None
        self.notifications = queue.Queue(maxsize=1000)
        self.diagnostics = deque(maxlen=10)
        self._pending = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._closed = False
        self._threads = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def start(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                        text=True, bufsize=1, env=self.env, creationflags=flags)
        for target in (self._read, self._read_stderr):
            worker = threading.Thread(target=target, daemon=True)
            worker.start()
            self._threads.append(worker)
        try:
            self.info = self.request("initialize", {"clientInfo": {
                "name": "codex_limit_guard", "title": "Codex Limit Guard", "version": __version__,
            }})
            self.notify("initialized", {})
        except BaseException:
            self.close()
            raise
        return self

    def _send(self, message):
        with self._write_lock:
            if self._closed or self.process is None or self.process.poll() is not None:
                raise RpcError("App Server 连接已断开")
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise RpcError("App Server 写入失败") from exc

    def request(self, method, params=None):
        future = Future()
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = future
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
            return future.result(timeout=self.timeout)
        except FutureTimeout as exc:
            raise RpcError(f"App Server 超时: {method}") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def notify(self, method, params=None):
        self._send({"method": method, "params": params or {}})

    def _fail_pending(self, message):
        with self._lock:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RpcError(message))

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        continue
                except (ValueError, TypeError):
                    self.diagnostics.append("App Server 返回了非 JSON 行")
                    continue
                if "id" in msg and "method" in msg:
                    # This observer never approves an action or fulfils dynamic tools.
                    self._send({"id": msg["id"], "error": {
                        "code": -32601, "message": "Guard is an observer; use the primary client for interactive requests",
                    }})
                elif "id" in msg:
                    with self._lock:
                        future = self._pending.get(msg["id"])
                        if future and not future.done():
                            if "error" in msg:
                                err = msg["error"]
                                future.set_exception(RpcError(str(err.get("message", "RPC error")), err.get("code")))
                            else:
                                future.set_result(msg.get("result", {}))
                elif "method" in msg:
                    try:
                        self.notifications.put_nowait(msg)
                    except queue.Full:
                        self.notifications.get_nowait()
                        self.notifications.put_nowait(msg)
        except (OSError, ValueError, RpcError):
            pass
        finally:
            self._fail_pending("App Server 输出已关闭")

    def _read_stderr(self):
        try:
            # Drain logs without persisting raw upstream data, tokens or prompts.
            for _ in self.process.stderr:
                pass
        except (OSError, ValueError):
            pass

    def drain(self):
        result = []
        while True:
            try:
                result.append(self.notifications.get_nowait())
            except queue.Empty:
                return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for worker in self._threads:
            worker.join(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()
        self._fail_pending("App Server 连接已关闭")
