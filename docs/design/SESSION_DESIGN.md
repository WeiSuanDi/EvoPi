# Session / Checkpoint v1 设计

## 角色与层级

Session 是一个持久存在的任务容器，通常对应一个对话窗口。它不替代 Core
生命周期，而是在 Harness 边界保存多个 Run 的正式结果：

```text
Session → Run → Turn → Model Attempt
```

- Session：跨进程存在的对话与任务容器。
- Run：一次 `Agent.prompt()` 的完整执行。
- Turn：一次模型响应及其工具批次。
- Model Attempt：一次具体 Provider 请求；自动重试仍属于同一个 Turn。

裸 `Agent` 不依赖 Session。`BaseHarness` 持有 `SessionManager`，负责把 Core 事件转换为
持久化事实。库调用默认使用内存 Session；CLI 默认在工作区维度继续最近的磁盘 Session。

## 事实来源与 Tree-ready Entry

每个持久 Session 使用追加式 `session.jsonl` 作为事实来源。第一条是
`SessionHeader`，之后是带独立 `schema_version=1`、UUID 与 UTC 时间的 Entry：

```text
run_start → message* → run_end → checkpoint
```

Entry 通过 `entry_id / parent_id` 构成树。v1 只追加到单一活动叶子，不提供 branch、
fork 或 active-leaf 切换，但数据协议不需要为这些能力重新定义父子关系。

`message` Entry 只保存已经提交到 AgentContext 的 `UserMessage`、
`AssistantMessage` 和 `ToolResultMessage`。System Prompt、模型、工具、Policy 和
Context Provider 在恢复时由当前 Harness 重新装配。失败的 Model Attempt、
Policy/Confirmation 决策和工具执行细节只进入 Trace。

Message Codec 是严格、双向且可恢复的。它保留消息 ID、UTC 时间、ToolCall、
StopReason 与 JSON-safe metadata；遇到不支持的值会使事实日志写入失败，不使用
`repr` 产生不可恢复数据。

## 磁盘布局与锁

默认根目录为 `~/.evopi/sessions/`，优先级为：

```text
Python root 参数 / CLI --session-root
→ EVOPI_SESSION_DIR
→ ~/.evopi/sessions/
```

工作区使用“可读 slug + 规范化路径 SHA-256 摘要”分桶：

```text
<root>/<workspace-bucket>/<session-id>/
├── session.jsonl
├── checkpoints/
│   └── <checkpoint-id>.json
└── session.lock
```

`SessionManager` 在整个打开生命周期内持有跨平台进程锁。并发打开同一 Session
明确失败，不自动分叉。创建文件时尽量设置为仅当前用户可访问；这是尽力而为的权限
收紧，不是加密。

## Checkpoint

Checkpoint 是每个 Run 结束后的不可变恢复投影，不是第二份事实日志。它保存：

- 活动 Entry 与日志偏移；
- 当前活动路径上已经提交的对话消息；
- 最近 Run 的状态与结构化模型错误；
- Harness、模型、System Prompt、工具定义和 Policy 描述的运行时指纹；
- 快照校验信息。

快照先原子写入临时文件、校验并替换到最终路径，再向 Session Log 追加
`checkpoint` Entry。快照缺失或校验失败时，加载器依次尝试更旧的 Checkpoint，
最终从 Session Log 全量重建。

Checkpoint 写入失败只产生结构化 warning 和 `session_error(recoverable=true)`，
因为事实日志已经包含完整 Run。Session Log 写入失败会把 Manager 标记为 broken，
阻止模型或工具继续执行。

## 中断恢复

v1 不创建运行中 Checkpoint，也不自动继续未完成 Run。打开 Session 时若活动路径
存在没有 `run_end` 的 Run：

1. 保留已经提交的正式消息；
2. 查找 AssistantMessage 中没有对应 ToolResult 的 ToolCall；
3. 为每个缺失调用追加“执行结果未知、未自动重跑”的错误 ToolResult；
4. 追加 `run_end(reason=interrupted, recovered=true)`；
5. 创建恢复 Checkpoint。

这样可以明确区分“工具确定失败”和“进程退出后执行结果未知”，并避免对非幂等工具
做隐式重放。

日志只允许修复最后一条未完成 JSONL 残行，而且必须在持锁后截断。中间损坏、重复
Entry ID、断裂 parent、非法消息和不支持的新 schema version 都拒绝打开。

## 运行时漂移与工作区

恢复始终使用当前 Harness 的可执行配置。每个新 Run 前比较上次 Checkpoint 的
Harness、模型、System Prompt、工具和 Policy 指纹；变化产生 warning，但不阻止运行。

显式打开另一个工作区创建的 Session 也允许继续。Session 保留原始工作区，工具绑定
当前 Harness 工作区，并通过 `SessionRecoveryInfo`、CLI stderr 与 `session_start`
Trace 数据暴露差异。

## 事件与 CLI

Session 使用三个治理可观测事件，Trace schema 继续保持 v2：

```text
session_start
session_checkpoint
session_error
```

普通 CLI Prompt 默认继续当前工作区最近的 Session：

```text
evopi "prompt"                 # 自动继续，没有记录则创建
evopi --new-session "prompt"   # 新建
evopi --session ID "prompt"    # 显式打开 ID 或路径
evopi --no-session "prompt"    # 仅内存
evopi session list             # 当前工作区只读列表
evopi session list --all --json
```

Session 信息与 warning 写 stderr，模型文本继续写 stdout。`reset()` 创建并绑定新
Session；`close()` 释放锁，运行中禁止关闭。

## 隐私与 v1 边界

Session 和 Checkpoint 是本地明文，可能包含 Prompt、模型回复与工具输出。用户应像
保护 Trace 一样保护 Session 根目录。v1 不做自动脱敏、加密、远程存储、删除/重命名、
Checkpoint GC、compact、branch/fork、运行中恢复或工具幂等重放。
