"""Agent fallback survives a missing lifecycle callback; normal checks stay quiet."""

INSTRUCTIONS = """## Codex Limit Guard

每轮用户任务开始、执行写文件/提交/发布等操作前，以及连续工作约 60 秒后，先调用
`codex_limit_guard.status`（可以传当前 `thread_id`）检查本地额度缓存。
`mode=normal` 时安静继续，不向用户发送额度提醒。
只有 `mode=protect` 且 `action=save_checkpoint_and_stop` 时才停止新增业务工作，应用
`codex-limit-checkpoint` Skill，调用专用 `save_checkpoint` 保存实际进度，简短汇报并结束本轮。
`mode=unknown`、`mode=disabled`、缓存过期或 MCP 不可用时继续正常任务，
不要因此暂停、保存收尾断点或发送额度提醒，也不要声称监控正常。
用户明确要求停用时调用 `codex_limit_guard.disable_guard`，该工具在低额度时也可用。
这是一项模型协作兜底，不代表已连接桌面运行时的强制中断接口。
"""

AGENTS_START = "<!-- BEGIN CODEX LIMIT GUARD (managed) -->"
AGENTS_END = "<!-- END CODEX LIMIT GUARD (managed) -->"
AGENTS_BLOCK = f"\n{AGENTS_START}\n{INSTRUCTIONS}{AGENTS_END}\n"
