# 兼容性与验证边界

检查日期：2026-10-24（Asia/Shanghai）。

本机安装的 CLI 是 `codex-cli 0.147.0`；Codex 桌面版内置 CLI 是
`0.163.0-alpha.10.2`。两者版本不同，不能仅凭系统 CLI 的帮助推断桌面能力。
开发时用 `codex app-server generate-json-schema` 生成本机版本协议到被 Git 忽略的
`.guard-local/schema`，用于核对参数；不提交个人会话或认证数据。

检查发现桌面版 App Server 以独立进程运行，没有 `--listen` 参数。
系统 CLI 的 `app-server daemon version` 明确报错：daemon 生命周期仅支持 Unix。
因此 Windows 的 `app-server proxy` 命令存在，不代表已有可连接的桌面控制 socket。
两个本机版本的 `features list` 均将 `hooks` 列为 `stable / true`。

## 实现约束

- 默认新建 stdio App Server，仅用于账户读取及受支持的持久化标题元数据操作。
- Hooks 的输入是当前会话的真实生命周期事件，状态写入自己的 SQLite。
- 活动回合的 steer/interrupt 必须显式启用，且每次确认同一连接中已加载该任务、
  活动回合 ID 一致；未通过验证时记录缺失能力，不 resume 或接管陌生运行时。
- `thread/read` 读取历史，不等于订阅当前桌面回合。
- 不直接读写 Codex 私有 SQLite，不调用模型来轮询额度、起标题或做测试。

## 本机只读集成验证

使用系统 CLI 启动独立 stdio App Server，完成 initialize/initialized 后：

- `account/read(refreshToken=false)`：返回 ChatGPT 登录类型。
- `account/rateLimits/read`：读取到 `codex` 额度桶，数据完整。
- `thread/read(includeTurns=false)`：可读取引用聊天的 ID 和真实标题。
- `thread/loaded/list`：该引用聊天不在此连接的活动任务列表。
- 引用聊天在独立服务器中显示 `notLoaded`，与桌面承载的运行时不同。

上述验证不启动模型回合、不更改真实聊天标题、不执行中断。
持久化标题读接口已验证；真实桌面 UI 对外部元数据改名的刷新时机仍需实际启用时验证。
保护与恢复状态机、改名写入及失败重试用隔离的模拟运行时做集成测试。

官方参考：[Hooks](https://learn.chatgpt.com/docs/hooks)、
[App Server](https://learn.chatgpt.com/docs/app-server)。
