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

## Windows 启动与安装验证

- 生成的 commandWindows 在 Windows PowerShell 原生执行，含空格/中文的状态目录
  和中文 cwd 往返不丢失；不依赖当前工作目录。
- 启动后台脚本后，从真实账户取得完整额度快照，health.connected=true。
- `stop` 请求使该后台进程退出，health.connected=false，并释放租约。
- 登录启动脚本通过 PowerShell AST 解析，未向用户任务计划程序注册测试任务。
- 安装/重复安装/卸载、外部配置编辑、失败回滚、事务恢复均在临时 Codex 配置目录验证。
- 67 项自动化测试覆盖额度策略、生命周期及安装；收尾 Skill 通过 skill-creator 的 quick_validate.py。
- Windows 原生命令验证了剩余 17% 的任务放行、5% 的业务工具保护、Stop 注入 Skill、
  stdio MCP 保存断点及回合结束。该验证使用隔离状态与模拟额度，不启动模型。
- Wheel 打包成功；在独立虚拟环境中安装后，从工作区以外启动 MCP、列出 3 个工具、
  读取打包 Skill、临时目录安装和卸载均成功。项目运行时仅使用 Python 标准库。

## 用户级启用验证

2026-10-24 已按用户要求启用到本机：

- 安装用户级 Hooks、专用断点 MCP 与 codex-limit-checkpoint Skill。
- 守护程序在后台运行，真实账户查询正常。
- 当前用户的 CodexLimitGuard 登录启动任务已注册。
- 通过 Codex 原生启动审核界面信任本项目定义，没有更改私有信任记录或绕过审核。
- 系统 CLI 的 hooks/list 返回 6 个已启用且受信任的事件。
- 桌面内置 CLI 的 hooks/list 返回 7 个已启用且受信任的事件，包含 Interrupt。
- 已观察到真实 CLI 会话 SessionStart 的任务登记；该检查未提交模型请求。
- 桌面内置 CLI 的 /mcp 显示 codex_limit_guard 已连接，提供 3 个工具。
- 本轮修复后按用户要求重新启用，桌面 hooks/list 再次确认 7 个 Hook 均启用且受信任；
  没有警告或错误，其他用户配置和信任哈希未变。

桌面版已打开聊天需重新加载配置后才能使用新增集成。真实桌面 UI 对外部改名的
刷新时机尚未验证；独立 App Server 无法强制中断桌面活动回合的边界保持不变。

官方参考：[Hooks](https://learn.chatgpt.com/docs/hooks)、
[App Server](https://learn.chatgpt.com/docs/app-server)。

## Hook 失效修复

此节记录 2026-10-24 的修复；缓存故障行为已由下节的 2026-10-25 修复取代。

- 实际日志确认 17:55:35 在剩余 5% 时进入保护；部分旧聊天没有继续上报生命周期，
  个别聊天完全未登记，因此未送入收尾信号。账户连接成功不代表聊天受保护。
- 增加每聊天/事件/回合的真实回调收据，跨回合、已结束及过期记录不算当前覆盖。
- 增加全局 AGENTS.md 可卸载检查协议及 SessionStart 协议兜底；正常状态保持安静。
  MCP status 返回明确动作和保护 Skill 正文。漏登记时也可保存断点，且不伪造 Hook 证据。
- 本机独立 App Server 没有连接桌面活动运行时；不能对现有聊天远程热加载或强制中断。
  修复安装后需完整退出并重新打开 Codex，重新核对真实回调。未验证的旧聊天不能声称已修复。
- 完整回归覆盖 76 项，包含原生 Windows Hook → MCP 保存断点的隔离流程。
  隔离测试不等于真实账户耗尽验收，也没有为了验收启动额外模型回合。

## 2026-10-25 缓存误暂停修复

- 截图为五小时剩余 99%/周剩余 58%，实际暂停原因是缓存过期。状态机和全局协议
  错误地把 unknown 也当作保护，因此普通问候及停用命令被拦截。
- 最后缓存停留在前一晚；登录任务最后运行于 08:43，结果为 0xC000013A，未配置失败重启；
  健康记录为账户查询超时，现场无守护进程。终止的更具体外部原因未证实。
- unknown、Hook 检查故障及 MCP 不可用不再暂停；正常业务继续，状态不冒称监控正常。
  五小时 5%/周 2% 的有效快照仍触发保护；有效的低额度窗口不会被其他缺失窗口掩盖。
- 增加持久停用、保护期间可用的 disable_guard MCP、卸载后缓存 Hook 的退出检查。
  增加后台监控恢复、启动去重、失效进程租约回收、登录任务周期恢复与失败重试。
- 88 项回归覆盖原生 Windows 99% 缓存过期流程和真实模拟服务进程被终止后重启；
  收尾 Skill 另做独立行为检查，确认明确停用请求优先停用、unknown 问候正常回复。
- 隔离状态连接真实账户：MCP 初始化自动恢复监控、取得完整新快照，status 返回 continue；
  调用 disable_guard 后确认监控进程退出，再次查询仍为 disabled/continue，没有自动重启。
  此验证没有向真实聊天写入消息、标题或工具事件，也没有启动模型回合。
