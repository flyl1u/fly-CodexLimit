"""Generate editable 1920x1080 architecture SVGs using the standard library.

PNG delivery files are rasterized from these SVGs. This script does not add a
dependency to the quota guard runtime.
"""

import argparse
from html import escape
from pathlib import Path


WIDTH, HEIGHT = 1920, 1080
FONT = "Segoe UI, Microsoft YaHei, Noto Sans CJK SC, Arial, sans-serif"

COPY = {
    "en": {
        "eyebrow": "FLY-CODEXLIMIT / SYSTEM ARCHITECTURE",
        "title": "Quota protection & resumable checkpoints",
        "subtitle": "One local daemon + lifecycle Hooks + a short checkpoint Skill",
        "account": ("ACCOUNT", "Account quota API", "App Server · short / long windows"),
        "daemon": ("COORDINATOR", "Local daemon", "One account view across all tasks"),
        "titles": ("METADATA", "Title metadata API", "Add / restore the pause marker"),
        "control": ("OPTIONAL", "Active-turn control", "steer / interrupt"),
        "state": ("Shared state", "Quota · tasks · checkpoints", "Title ownership · resume queue"),
        "reports": ("EXPORT", "Progress reports", "Markdown + JSON"),
        "session": ("SESSION", "Codex chat", "Current user task"),
        "hooks": ("LOCAL GUARDRAIL", "Lifecycle Hooks", "Read the cache · gate new tools"),
        "agent": ("WRAP-UP", "Current task + Skill", "Save progress · report · end the turn"),
        "mcp": ("CHECKPOINT TOOLS", "Checkpoint MCP", "save / read / status"),
        "optional": "Verified same runtime only",
        "rename": "Rename", "cache": "Quota cache", "checks": "Local checks",
        "export": "Export", "check": "Check", "wrap": "Wrap up", "save": "Save",
        "write": "Checkpoint writes",
        "footer": (
            ("NO EXTRA MODEL REQUESTS", "Polling · title updates · state sync"),
            ("CONFIRMED PAUSE", "Checkpoint → end confirmation → title marker"),
            ("USER-INITIATED RESUME", "Fresh quota → user request → actual execution"),
        ),
        "note": "Progress summaries still use the task's model context. Dashed connector: optional runtime control.",
    },
    "zh-CN": {
        "eyebrow": "FLY-CODEXLIMIT / 设计架构",
        "title": "额度保护与任务断点续作",
        "subtitle": "一个本地守护程序 + 生命周期 Hooks + 简短收尾 Skill",
        "account": ("账户", "账户额度接口", "Codex App Server · 短期与长期窗口"),
        "daemon": ("统一协调", "本地守护程序", "统一监控所有任务的账户额度"),
        "titles": ("元数据", "标题元数据接口", "添加 / 恢复暂停标记"),
        "control": ("可选能力", "活动回合控制", "steer / interrupt"),
        "state": ("本地共享状态", "额度 · 任务 · 断点", "标题所有权 · 恢复队列"),
        "reports": ("导出", "进度报告", "Markdown + JSON"),
        "session": ("会话", "Codex 聊天", "用户当前任务"),
        "hooks": ("本地检查", "生命周期 Hooks", "读取缓存 · 检查新的工具调用"),
        "agent": ("收尾", "当前任务 + Skill", "保存进度 · 简短汇报 · 结束本轮"),
        "mcp": ("断点工具", "专用断点 MCP", "保存 / 读取 / 查询状态"),
        "optional": "仅在核对同一活动运行时后",
        "rename": "改名", "cache": "额度缓存", "checks": "本地检查",
        "export": "导出", "check": "检查", "wrap": "收尾信号", "save": "保存断点",
        "write": "持久保存断点",
        "footer": (
            ("无需额外模型请求", "额度查询 · 标题更新 · 状态协调"),
            ("确认暂停后标记", "保存断点 → 确认结束 → 添加标题标记"),
            ("由用户发起续作", "真实额度恢复 → 用户继续 → 实际执行"),
        ),
        "note": "语义进度汇报仍使用当前任务的模型上下文。虚线表示可选的运行时控制能力。",
    },
}


def render(language):
    copy = COPY[language]
    parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title description">
<title id="title">fly-CodexLimit — {escape(copy['title'])}</title>
<desc id="description">{escape(copy['subtitle'])}. 16:9 architecture diagram.</desc>
<defs>
  <filter id="shadow" x="-15%" y="-30%" width="130%" height="170%"><feDropShadow dx="0" dy="5" stdDeviation="9" flood-color="#163b72" flood-opacity="0.055"/></filter>
  <marker id="arrow" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="12" markerHeight="12" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M2 1 L11 6 L2 11 Z" fill="#7c8da5"/></marker>
  <marker id="arrowOptional" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="12" markerHeight="12" markerUnits="userSpaceOnUse" orient="auto"><path d="M2 1 L11 6 L2 11 Z" fill="#b49663"/></marker>
</defs>
<rect width="1920" height="1080" fill="#f6f8fc"/>
<g font-family="{FONT}">''']

    def text(x, y, value, size=20, fill="#556982", weight=400, anchor="start", spacing=None):
        tracking = f' letter-spacing="{spacing}"' if spacing is not None else ""
        parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{tracking}>{escape(value)}</text>')

    def rect(x, y, w, h, fill, stroke="none", radius=20, shadow=False):
        effect = ' filter="url(#shadow)"' if shadow else ""
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{effect}/>')

    def path(data, double=False, optional=False):
        marker = "arrowOptional" if optional else "arrow"
        attrs = ' marker-start="url(#arrow)"' if double else ""
        dash = ' stroke-dasharray="9 8"' if optional else ""
        color = "#b49663" if optional else "#7c8da5"
        parts.append(f'<path d="{data}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" marker-end="url(#{marker})"{attrs}{dash}/>')

    def label(x, y, key, anchor="middle", width=None, color="#60738f"):
        value = copy[key]
        if width:
            rect(x - width / 2, y - 20, width, 30, "#f6f8fc", radius=8)
        text(x, y, value, 18, color, 500, anchor)

    # Header.
    text(80, 59, copy["eyebrow"], 18, "#3b6fa4", 650, spacing=1.1)
    text(80, 118, copy["title"], 46, "#172c4b", 650)
    text(80, 158, copy["subtitle"], 22)
    rect(1598, 54, 242, 44, "#eaf0f8", radius=22)
    text(1719, 83, "16:9 · 1920 × 1080", 19, "#42618a", 550, "middle")
    parts.append('<path d="M80 185 H1840" stroke="#dce5f0" stroke-width="1.5"/>')

    # Draw edges before cards so endpoints remain clean.
    path("M410 322 H520")                          # Account -> daemon.
    path("M900 322 H1020")                         # Daemon -> title API.
    path("M710 384 V470", double=True)             # Daemon <-> shared state.
    path("M710 606 V700", double=True)             # Hooks <-> shared state.
    path("M520 538 H410")                          # Shared state -> reports.
    path("M410 764 H520")                          # Chat -> Hooks.
    path("M900 764 H1020")                         # Hooks -> task.
    path("M1400 764 H1510")                        # Task -> MCP.
    path("M1675 700 V538 H900")                    # MCP -> shared state.
    path("M710 260 V222 H1675 V260", optional=True)  # Daemon -> optional control.

    def card(key, x, y, w, h=124, theme="normal"):
        dark = theme == "dark"
        optional = theme == "optional"
        accent = theme == "accent"
        fill = "#173e74" if dark else "#edf4fe" if accent else "#fffcf6" if optional else "#ffffff"
        stroke = "#173e74" if dark else "#cbdcf2" if accent else "#e5d8c0" if optional else "#dce4ef"
        rect(x, y, w, h, fill, stroke, shadow=True)
        kicker, heading, body = copy[key]
        text(x+24, y+30, kicker, 14, "#b9d0f3" if dark else "#a08655" if optional else "#5c7ca9", 650, spacing=0.8)
        if key == "titles":
            rect(x+w-54, y+16, 30, 30, "#fff0ed", radius=9)
            text(x+w-39, y+39, "!", 23, "#d45945", 750, "middle")
        title_size = 28 if key not in ("control", "agent", "mcp") else 26
        text(x+24, y+68, heading, title_size, "#ffffff" if dark else "#213b5d", 650)
        body_size = 17 if key == "account" else 19
        text(x+24, y+102, body, body_size, "#d7e5f8" if dark else "#657990")

    card("account", 80, 260, 330)
    card("daemon", 520, 260, 380, theme="dark")
    card("titles", 1020, 260, 380)
    card("control", 1510, 260, 330, theme="optional")
    card("reports", 80, 476, 330)
    card("session", 80, 700, 330, h=128)
    card("hooks", 520, 700, 380, h=128, theme="accent")
    card("agent", 1020, 700, 380, h=128)
    card("mcp", 1510, 700, 330, h=128, theme="accent")

    # Cylinder for the durable shared state.
    parts.append('<path d="M520 490 C520 463 900 463 900 490 V586 C900 613 520 613 520 586 Z" fill="#edf8f2" stroke="#b8d8c8" stroke-width="1.5" filter="url(#shadow)"/>')
    parts.append('<ellipse cx="710" cy="490" rx="190" ry="20" fill="#e4f3eb" stroke="#b8d8c8" stroke-width="1.5"/>')
    text(710, 538, copy["state"][0], 29, "#215a44", 650, "middle")
    text(710, 569, copy["state"][1], 19, "#527b69", anchor="middle")
    text(710, 593, copy["state"][2], 17, "#527b69", anchor="middle")

    label(1180, 211, "optional", width=328 if language == "en" else 278, color="#9b7e50")
    label(960, 302, "rename")
    label(739, 433, "cache", anchor="start")
    label(739, 660, "checks", anchor="start")
    label(465, 518, "export")
    label(465, 744, "check")
    label(960, 744, "wrap")
    label(1455, 744, "save")
    label(1320, 518, "write")

    # Short, factual lifecycle notes.
    rect(80, 900, 1760, 112, "#eaf0f8", radius=22)
    parts.append('<path d="M610 925 V987 M1220 925 V987" stroke="#d4dfed" stroke-width="1.5"/>')
    for x, (heading, body) in zip((106, 638, 1248), copy["footer"]):
        text(x, 942, heading, 18, "#315682", 650)
        text(x, 978, body, 18, "#5c718c")
    text(80, 1046, copy["note"], 16, "#73849b")
    text(1840, 1046, "flyl1u/fly-CodexLimit", 16, "#73849b", anchor="end")
    parts.append("</g></svg>\n")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Generate 16:9 bilingual architecture SVGs")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent.parent / "docs" / "images")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for language in COPY:
        target = args.output / f"architecture.{language}.svg"
        target.write_text(render(language), encoding="utf-8")
        print(target)


if __name__ == "__main__":
    main()
