"""Reports distinguish human/model checkpoints from observed tool activity."""

from datetime import datetime, timezone
import json

from .files import atomic_json, atomic_text, checkpoint_name


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat(timespec="seconds") if value is not None else "未记录"


def render_report(task):
    cp = json.loads(task["checkpoint"]) if task.get("checkpoint") else None
    pending = task["phase"] == "pausing"
    fresh = bool(cp and task.get("checkpoint_turn_id") == task.get("pause_turn_id")
                 and task.get("checkpoint_at", 0) >= (task.get("pause_requested_at") or 0))
    lines = [f"# {task.get('title') or task['thread_id']}", "",
             f"聊天：`{task['thread_id']}`", f"工作目录：`{task['cwd']}`", "",
             f"状态：{'等待回合结束确认；不能确认已经停下' if pending else task['phase']}",
             f"暂停原因：{task.get('pause_reason') or '未记录'}",
             f"回合结束时间：{stamp(task.get('closed_at'))}",
             f"最近断点时间：{stamp(task.get('checkpoint_at'))}", ""]
    if not fresh:
        lines.extend(["本次未确认完成新的收尾断点。以下仅为最近保存的记录，可能早于暂停。", ""])
    if cp:
        for field, label in (("completed", "已完成"), ("current", "当前状态"), ("remaining", "剩余工作"),
                             ("validation", "验证结果"), ("next_step", "下一步"), ("files", "相关文件"),
                             ("commands", "运行中的命令"), ("notes", "备注")):
            value = cp.get(field)
            if value:
                lines.extend([f"## {label}", ""])
                lines.extend([f"- {x}" for x in value] if isinstance(value, list) else [value])
                lines.append("")
    else:
        lines.extend(["尚无语义进度断点。不能从工具事件推断业务已完成。", ""])
    if task.get("last_tool"):
        lines.extend([f"最近工具事件：`{task['last_tool']}`（{stamp(task.get('last_tool_at'))}）。",
                      "工具事件不代表命令成功或业务完成。", ""])
    return "\n".join(lines)


def export_report(store, task):
    base = store.home / "checkpoints" / checkpoint_name(task["thread_id"])
    atomic_text(base.with_suffix(".md"), render_report(task))
    data = {key: task.get(key) for key in (
        "thread_id", "cwd", "title", "phase", "turn_id", "pause_turn_id", "pause_reason",
        "pause_requested_at", "closed_at", "closure_kind", "checkpoint_at", "checkpoint_turn_id",
        "last_tool", "last_tool_at",
    )}
    data["checkpoint"] = json.loads(task["checkpoint"]) if task.get("checkpoint") else None
    atomic_json(base.with_suffix(".json"), data)
    return base.with_suffix(".md")
