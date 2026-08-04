# Harness 设计笔记

Harness 是 EvoPi 的运行治理框架。

它包在 Core 外面，负责把一个“能跑的 Agent Loop”组织成一个“可治理、可扩展、可产品化”的 Agent Runtime。

## 一句话边界

```text
Core 负责执行；
Base Harness 负责组织执行；
Policy 负责约束执行；
Domain Harness 负责场景化执行。
```

## Base Harness 的定位

Base Harness 要广而浅。

它不内置具体领域深逻辑，而是提供通用治理骨架：

```text
生命周期
Hook 点
Policy 调度
Context 组装
Session / Trace 连接
Domain Harness 装配能力
```

领域深逻辑交给 Domain Harness。

例如：

```text
CodingHarness
FinanceHarness
ResearchHarness
```

## 第一版 Base Harness 六项能力

### 1. 生命周期治理

Harness 要知道 Agent 当前处在什么运行状态。

例如：

```text
idle
running
waiting_for_confirmation
aborting
failed
completed
aborted
```

Core 只负责跑 loop；Harness 负责把一次运行放进生命周期里。

Lifecycle v2 中，Harness 保存 Core 的结构化 `end_reason`：正常回答和主动早停映射为
`completed` 状态，异常和 turn limit 映射为 `failed`，主动取消先进入 `aborting`，
完成清理后映射为 `aborted`。

`BaseHarness.abort()` 委托给当前 Agent，并公开同一个只读 `signal`、`is_running` 和
`wait_for_idle()`。Abort 不属于可由 Policy 否决的普通治理决定；Policy 通过
`PolicyContext.aborted` 观察清理阶段，并继续记录 Trace。

`after_turn` Policy 通过独立的 Core `ShouldStopAfterTurn` 回调应用：Policy 仍在
`after_turn` Hook 上返回 `terminate` 并把原因写入 Trace，Harness 只把最终动作转换为
布尔停止决定。它与工具结果的 `terminate` 批次提示互不替代。

BaseHarness 在 Context Provider 与 Plugin Prompt Fragment 完成后、`before_model_call`
Policy 之前提供受保护的 Domain Context 装配点。默认实现不改变 Context；CodingHarness
用它注入临时 Turn 收尾提示，并在最后一个 Turn 收窄 Tool 视图。这样 Policy 始终审查
模型真正会看到的最终 Context，且临时提示不会写入 Session。

Provider Reliability v1 中，Harness 负责选择 Core 重试配置，但不重新实现重试循环。
`BaseHarness` 与 `CodingHarness` 默认传入启用状态的 `ModelRetryConfig`，裸 `Agent` 则默认
关闭，便于库调用方明确决定行为。每次重试仍重新进入 Harness 的 Context Provider 和
`before_model_call` Hook，因此重试不会绕过治理；Policy 阻断直接终止当前重试链。

Harness 将最终 `error` 事件中的 `ModelErrorInfo` 放入 `PolicyContext.error_info`，并保持
字符串 `error` 兼容。`on_error` 只在重试耗尽或错误不可重试时执行一次，不观察每个瞬态
attempt；每次尝试的细节由 `model_start`、`model_retry_*`、消息事件和 Trace 提供。

Provider Failover v1 由 Harness 组织，但继续复用同一个 Core `ModelCallExecutor`。
`ModelRoute` 声明有序候选、稳定 route fingerprint、失败域和进程内 Circuit；
`HarnessModelAttemptRouter` 负责候选选择、Run affinity、上下文窗口预检和
`before_model_failover` 治理。默认 Circuit 连续两次健康类失败后开启，30 秒后只允许一个
half-open probe；合法 `Retry-After` 会立即暂停共享失败域。明确的模型不存在和
`context_overflow` 只暂停对应候选，避免错误隔离同一端点上的其他模型。

任何从一个候选切到另一个候选的行为都先执行 Policy/Confirmation，包括新 Run 的主
候选已经开路或基础上下文已知不兼容时的初始 fallback。成功候选只在当前 Run 内保持
亲和性；Circuit 跨 Run 共享但不持久化、无后台探测，也不跨进程同步。动态 Context
Provider 与 Plugin Prompt 在实际 attempt 前仍会重新执行；跨候选授权发生在最终目标
Context 形成之后、网络请求之前，因此 `before_model_failover` 看到的快照与实际发送内容
一致。若动态注入导致基础预检后溢出，Adapter 的结构化 `context_overflow` 会把该候选
标记不可用并进入同一受治理切换链。关闭 Failover 时，初始选择也不会绕到备用候选。

### 2. Hook 点治理

Harness 要提供 Policy 可以插入的治理节点。

第一版关键 Hook：

```text
before_model_call
before_model_failover
after_model_call
before_tool_call
after_tool_call
after_turn
before_session_merge
on_error
```

Hook 是插槽，不是具体规则。

`before_session_merge` 治理显式的跨分支认知迁移。它只接受 `allow`、`block` 或
`require_confirmation`；改写、验证和终止等没有 Merge 语义的动作会 fail closed。
自动摘要复用 `GovernedModelOperation`，因此继续经过 Provider Retry、Failover、Abort、
超时和普通 `before_model_call` Policy，但 Tool 集固定为空。手工摘要完全跳过模型。

### Human Confirmation 运行协议

当 Policy 返回 `require_confirmation` 时，由 Harness 负责把结构化请求交给外部
`ConfirmationHandler` 或 `ConfirmationBroker`，Core 不参与具体交互。Handler 保持
进程内兼容路径；Broker 是桌面 UI、IDE 和本地 RPC 的多调用者状态边界。

第一版运行语义：

```text
require_confirmation
  → lifecycle: waiting_for_confirmation
  → ConfirmationHandler(request)
  → approve: lifecycle 恢复 running，继续执行
  → deny: lifecycle 恢复 running，安全阻断并回填工具结果
  → cancelled: lifecycle 进入 aborting，完成清理后变为 aborted
```

约束：

- 没有配置 Handler 时默认拒绝。
- Handler 异常、返回类型错误或 request ID 不匹配时默认拒绝。
- 确认请求与响应必须进入 Trace，并与当前 `run_id` 关联。
- 等待确认期间的 Abort 会取消异步 Handler，并生成可追踪的 `cancelled` Response。
- Broker 使用 `pending → approved/denied/cancelled/expired/orphaned` 的单向状态机；重复、
  过期或 revision 漂移的响应明确失败，决不重放 Tool。
- `confirmation_state_changed` Event 只携带 request、Run、Session 与状态关联字段，不复制
  原始 Tool 参数；完整本地请求由 Broker Store 持有。
- 进程异常恢复时，未完成请求标记为 `orphaned`，不会自动重建 Run、Confirmation 等待者
  或 Tool 执行。优雅关闭则先持久化 `cancelled`，再唤醒等待者。
- CLI、Web UI 和 RPC 宿主只实现交互或 Broker 响应，不改变 Policy 或 Core。

### 3. Policy 调度

Harness 不负责具体规则判断，但负责调用 Policy，并把 PolicyDecision 应用到运行过程。

典型流程：

```text
before_tool_call 发生
  ↓
Harness 找到挂在该 hook 的 policies
  ↓
依次执行 policies
  ↓
合并 PolicyDecision
  ↓
决定 allow / block / rewrite / confirm
```

一句话：

```text
Policy 判断；
Harness 调度并执行判断结果。
```

### 4. Context 组装

Core 只接收最终 Context。

Harness 负责决定这次模型调用前应该装配哪些内容：

```text
system prompt
用户消息
历史消息
session summary
memory
skill
可用工具列表
领域上下文
```

第一版不必实现复杂 memory / skill 检索，但需要预留 Context Provider 机制。

### 5. Session / Trace 连接

Harness 要把 Core 的运行过程接到 Session / Trace。

Session 由 Harness 持有，裸 Agent 保持 Session-neutral。`BaseHarness` 默认使用内存
Session；调用方可注入持久 `SessionManager`。Harness 在 `agent_start`、
正式 `message_end` 和 `agent_end` 上追加 Run 边界与消息，并在 Run 结束后创建
Checkpoint。失败 Model Attempt 的 `committed=False` 消息只进入 Trace。

恢复时 Harness 先注入当前 SystemMessage，再装载 Session 中的 User、Assistant 与
ToolResult 消息；模型、工具、Policy 和 Context Provider 始终使用当前配置。运行时
指纹或工作区变化产生 warning，但不会自动切回历史可执行对象。

Lifecycle v2 至少记录：

```text
message_start / message_update / message_end
模型开始
tool_execution_start / tool_execution_end
turn_start / turn_end
agent_start / agent_end
policy decision
policy evaluation
confirmation request / response
session_start / session_checkpoint / session_error
error
```

新 Trace 顶层带 `schema_version=2`。无版本历史记录按 v1 读取，Replay 同时兼容
v1 的 `tool_call/tool_result` 和 v2 的工具执行事件。Trace 是后续 Evo 的原材料。

### 6. Domain Harness 装配能力

Base Harness 要支持领域 Harness 扩展。

例如：

```text
CodingHarness = BaseHarness + coding tools + coding prompts + coding policies
```

Base Harness 应提供注册入口：

```text
register_tool
register_policy
register_skill
set_system_prompt
add_context_provider
```

## Base Harness 的治理边界

Base Harness 可以组织下列能力，但不包含其领域规则或持久化实现：

```text
Session / Trace 接线
Context Provider
阈值式 Compaction 调度
Child Harness Factory
Plugin 能力快照与事务式重装配
```

Memory 内容策略、Skill 选择、Coding 工具集合和安全 Policy Pack 仍属于 Domain
Harness 或独立模块。Base Harness 不扫描任意项目源码、不保存长期 Memory，也不把
Plugin、SubAgent 或 Session 语义塞回 Core。

`HarnessCapabilities` 是一次装配后的只读公开快照。装配顺序固定为：解析信任与来源，
收集内置和已授权扩展，校验冲突/依赖，生成最终 System Prompt，创建 Agent 并冻结
本次 Run 的能力。运行期间禁止 Reload；空闲 Reload 必须先在临时注册表验证，成功后
整体替换。

## 通用 Plugin Runtime

BaseHarness 是 `PluginAPI v1` 的宿主，但不把 Plugin 语义下沉到 Core。Harness 提供
公共异步 Command 分派、Context Provider、动态 Prompt Fragment、所有者隔离的 Tool
活动视图、Session-backed State 和宿主无关 UI。Plugin 不访问 BaseHarness 私有字段。

Plugin 的所有注册先进入暂存装配，完成依赖、重复命令/Tool、显式覆盖和 Handler
绑定校验后整体提交。运行中禁止 Reload。活动 Tool 集是基础集合与所有插件限制的
交集，只能收窄；session 作用域覆盖通过 Session schema v3+ 恢复。

活动演进 Policy 使用同一事务装配：Harness 先暂存内置能力、批准 Plugin 和用户显式
活动的 Policy Artifact，重新验证摘要、Manifest、实例契约、同名替换目标及预期摘要，
全部成功后才一次替换运行时。失败启动时 fail closed；空闲 `/reload` 失败时保留旧
Tool、Policy、Command、Prompt 和 Handler 快照。Run 开始时复制 Policy Registry，
本轮结束前禁止注册或 Reload，SubAgent 因而继承父 Run 的同一治理下界。

裸 BaseHarness 只有显式收到 `PolicyActivationService` 才读取活动工件；Coding CLI
是默认接入该用户级配置的产品宿主，并提供 `--no-evolved-policies`。

Plugin 候选创作仍属于 Coding Domain：CodingHarness 默认注册
`create_plugin_candidate`，固定工作区候选路径并复用 Plugin SDK/静态审查。BaseHarness
不认识候选目录，也不会隐式创建或授权工件。该 Tool 与其他 Tool 一样经过标准
before/after Tool Policy、Event 和 Trace 链，Run 的能力快照与 Tool ceiling 同样适用。

Event Handler 只观察订阅事件，返回值不能修改执行。`allow/block/confirmation/rewrite`
必须由注册到 Policy Engine 的 Policy 给出。UI 只提供交互，不替代 Tool Confirmation
Handler；非交互宿主的 Plugin 确认默认拒绝。

## Harness 和 Policy 的关系

```text
Harness = 场景工作台 / 插槽系统 / 调度框架
Policy = 工作规矩 / 具体代码规则
```

两者经常一起出现，但不是同一个东西。

```text
Harness 决定这个 Agent 在什么场景里工作；
Policy Pack 决定这个 Agent 在这个场景里按什么规矩工作。
```

例如：

```text
CodingHarness
  准备文件系统上下文、读写工具、shell 工具、测试入口、diff 状态等。

coding_policy_pack
  提供 shell_safety、file_write_guard、output_truncation、test_after_edit 等规则。
```

所以：

```text
CodingHarness 让 Agent 会写代码；
coding_policy_pack 让 Agent 安全、规范、可控地写代码。
```

## Harness 演进边界

Harness 可以演进，但要低频、强验证。

Harness 级演进包括：

```text
新增 Hook 点
调整 Policy 调度方式
调整 Policy 冲突解决方式
改变 session/subagent 治理流程
```

它比 Policy 演进风险更高，因此需要：

```text
schema check
dry-run
trace replay
supervisor review
human confirmation
```

## 活动能力与动态 Prompt

Harness 公开注册 Tool 与最终活动 Tool 的只读快照。CLI include/exclude 是不可扩张的
上限；Plugin 的 run/session 覆盖与其取交集，SubAgent 继承同一上限。Tool 注册、Reload、
Plan Mode 覆盖清理及 Session leaf 恢复后，Domain Harness 根据最终活动视图刷新
System Prompt。

CodingHarness 支持生成 Prompt、完整替换和追加三种组合语义。Plugin Prompt Fragment
与 Skill Context 仍在每次模型调用前装配，不被固化进基础 Prompt。裸 BaseHarness
不读取 CLI 环境、用户活动 Policy 或 Coding 资源。

Shell Environment 是 CodingHarness 的显式装配依赖，不属于 BaseHarness 或 Core。
CodingHarness 使用解析后的 `ShellEnvironment` 同时构造 Shell Tool、Tool metadata 与
动态 Prompt，避免执行器和模型各自猜测语法。CLI 必须在创建 Session 和模型前完成
解析；自定义 Harness 可直接注入同一只读值。
