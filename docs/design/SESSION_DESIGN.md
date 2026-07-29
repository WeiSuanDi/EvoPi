# Session / Checkpoint v4 设计

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
`SessionHeader`，之后是带独立 `schema_version=4`、UUID 与 UTC 时间的 Entry：

```text
run_start → message* → run_end → checkpoint
                         ├→ branch
                         ├→ compact
                         ├→ leaf_selected
                         ├→ plugin_state
                         └→ merge
```

Entry 通过 `entry_id / parent_id` 构成树。`LeafSelectedEntry` 的 `parent_id` 指向
目标叶，`from_entry_id` 记录切换前位置；选择事件本身不进入模型消息，但成为新的
活动位置。`SessionManager.messages` 始终从当前活动叶沿父链重建，绝不返回全局累计
消息。

`message` Entry 只保存已经提交到 AgentContext 的 `UserMessage`、
`AssistantMessage` 和 `ToolResultMessage`。System Prompt、模型、工具、Policy 和
Context Provider 在恢复时由当前 Harness 重新装配。失败的 Model Attempt、
Policy/Confirmation 决策和工具执行细节只进入 Trace。

Message Codec 是严格、双向且可恢复的。它保留消息 ID、UTC 时间、ToolCall、
StopReason 与 JSON-safe metadata；遇到不支持的值会使事实日志写入失败，不使用
`repr` 产生不可恢复数据。

`PluginStateEntry` 是 v3 的通用插件状态事实，包含插件名/版本、键、`set/delete`
操作、严格 JSON-safe 值和可选 Run ID。状态沿当前活动叶投影，因此 branch、switch、
fork 和重启拥有一致语义。单值默认限制 64 KiB，单插件活动投影限制 1 MiB。

`MergeEntry` 是 v4 的证据绑定认知迁移事实。它仍然只有一个 `parent_id`，指向合并时的
目标活动叶；来源 Entry、共同祖先、来源分歧路径 SHA-256、Entry 数量、摘要来源和
operation ID 作为审计引用保存，不形成双父 DAG。加载日志时必须验证来源在该事实之前
存在、当时确为叶、共同祖先同时位于两条路径上，并重新计算分歧路径数量与摘要。

Merge 投影为 ID 稳定的上下文消息，只包含已提交摘要。来源分支的原始消息、ToolCall、
ToolResult 和 Plugin State 不复制、不重放、不合并；来源分支仍可独立切换和继续增长。

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
- 当前活动路径的 Plugin 状态投影；
- 快照校验信息。

快照中的消息只是缓存。加载时必须与活动路径的消息 ID、顺序、角色和工具关联一致；
不一致就丢弃并从日志重建。快照先原子写入临时文件、校验并替换到最终路径，再向 Session Log 追加
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
使用 Model Route 时，“模型”指纹保存稳定 route fingerprint，其中包含候选顺序、Provider、
模型名、哈希后的失败域、上下文预算、Failover 开关和 Circuit 配置。进程内 Circuit 状态与
Run affinity 不写入 Session 或 Checkpoint；恢复后由当前进程从 closed 状态重新建立健康
事实，避免把过期的端点故障当作持久会话事实。

显式打开另一个工作区创建的 Session 也允许继续。Session 保留原始工作区，工具绑定
当前 Harness 工作区，并通过 `SessionRecoveryInfo`、CLI stderr 与 `session_start`
Trace 数据暴露差异。

## 事件与 CLI

Session 与 Plugin State 使用治理可观测事件，Trace schema 继续保持 v2：

```text
session_start
session_checkpoint
session_error
session_merge_start
session_merge_end
session_merge_error
plugin_state_changed
```

普通 CLI Prompt 默认继续当前工作区最近的 Session：

```text
evopi "prompt"                 # 自动继续，没有记录则创建
evopi --new-session "prompt"   # 新建
evopi --session ID "prompt"    # 显式打开 ID 或路径
evopi --no-session "prompt"    # 仅内存
evopi session list             # 当前工作区只读列表
evopi session list --all --json
evopi session gc ID|PATH       # Checkpoint GC Dry Run
evopi session gc ID --apply    # 校验计划后删除

# REPL 内
/leaves
/switch <唯一 Entry 前缀>
/merge <来源叶前缀> [手工摘要]
```

Session 信息与 warning 写 stderr，模型文本继续写 stdout。`reset()` 创建并绑定新
Session；`close()` 释放锁，运行中禁止关闭。

## v1 / v2 / v3 迁移

打开 v1/v2/v3 日志时先在持锁状态下做完整结构验证，再生成对应的只读版本备份，将
Header 和 Entry 原子重写为 v4 并重新校验。原有 Entry ID、父子关系、消息和 Plugin
State 不变。旧 Checkpoint 不作为 v4 恢复来源；恢复从事实日志重建，并为最近已闭合
Run 生成新的 v4 Checkpoint。

## Compaction

Compaction 只处理当前活动路径，并保持 ToolCall / ToolResult 关系完整。Run 成功或主动
终止后先做轻量 token 阈值检查；低于阈值零模型调用。触发时同步执行
`before_session_compact` Policy、可选 Confirmation、Provider Retry、Abort 与 120 秒
墙钟上限，并记录 `session_compaction_start/end/error`。失败或阻断不会写
`CompactEntry`，也不改变已完成 Run。

## Branch Merge

`SessionManager.prepare_merge()` 固定以当前活动叶为目标，来源必须是另一个现存叶，
且两条路径都不能包含未闭合 Run。准备阶段计算共同祖先和来源分歧路径；提交阶段重新
计算并比较目标叶、共同祖先、Entry 数量和 SHA-256，防止自动摘要期间状态漂移。

用户提供摘要时不调用模型。省略摘要时，Harness 使用 `GovernedModelOperation`，只把
共同路径最后两条消息和来源分歧消息交给模型，Tool 集为空。该操作继续经过 Provider
Retry、Failover、Abort、120 秒墙钟上限及普通 `before_model_call` Policy。已知
Context Window 且超过预留输入预算时明确失败，要求用户改用手工摘要，不静默截断。

显式 Merge 先经过 `before_session_merge`。Policy 仅可 allow、block 或请求人工确认；
阻断、拒绝、模型错误、超时或事实日志写入失败都不会追加 `MergeEntry`。事件和 Trace
只记录定位字段、计数与摘要哈希，不重复记录完整摘要。

## Checkpoint GC

Checkpoint 是可由事实日志重建的派生缓存。`SessionManager.plan_checkpoint_gc()` 只读
扫描一个已持锁的持久 Session：对每个现存叶保留路径上最近三份校验有效快照，多个叶的
共享祖先只计一次；最近七天内的全部 Checkpoint 文件无条件保护。更老且不在保留集中的
有效快照，以及超过保护期的损坏快照、无日志引用的 UUID 快照和崩溃临时文件进入候选。
缺失的日志引用会进入计划的可观测项，但不存在文件无需删除。

计划是 `schema_version=1` 的稳定审计工件，绑定 Session ID、Session 路径、事实日志
SHA-256，以及每个候选的规范化相对路径、文件大小和内容摘要。`apply_checkpoint_gc()`
在删除第一份文件前先完成全量预检；日志或任一候选发生漂移就整体拒绝。进入删除阶段后，
单文件失败写入结构化 Report，其余候选可继续处理，且不会把 SessionManager 标记为
broken。

GC 不追加维护 Entry，也不修改 Session Tree。`session.jsonl`、版本备份、锁、Trace、
消息、分支和任何非 Checkpoint 文件永不进入候选。CLI 默认 Dry Run，只有 `--apply`
才永久删除；v1 每次只处理一个显式 Session，不提供后台或批量 GC。

## 隐私与 v4 边界

Session 和 Checkpoint 是本地明文，可能包含 Prompt、模型回复与工具输出。用户应像
保护 Trace 一样保护 Session 根目录。v4 不做自动脱敏、加密、远程存储、删除/重命名、
Session Log GC、跨 Session Merge、双父 DAG、运行中继续执行或工具幂等重放。

## 产品入口

交互工作台通过公共 Harness API 执行 `/new`、`/branch`、`/switch`、`/merge` 与
`/compact`，不直接修改 SessionManager 私有状态。`evopi run --json` 只输出 Session/
Run 标识与最终结果，不复制 Prompt 或 Provider State。`config show` 展示有效 Session
Root；`doctor` 仅做可清理的写入探测，不打开或修改既有 Session Log。
