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

Provider Reliability v1 中，Harness 负责选择 Core 重试配置，但不重新实现重试循环。
`BaseHarness` 与 `CodingHarness` 默认传入启用状态的 `ModelRetryConfig`，裸 `Agent` 则默认
关闭，便于库调用方明确决定行为。每次重试仍重新进入 Harness 的 Context Provider 和
`before_model_call` Hook，因此重试不会绕过治理；Policy 阻断直接终止当前重试链。

Harness 将最终 `error` 事件中的 `ModelErrorInfo` 放入 `PolicyContext.error_info`，并保持
字符串 `error` 兼容。`on_error` 只在重试耗尽或错误不可重试时执行一次，不观察每个瞬态
attempt；每次尝试的细节由 `model_start`、`model_retry_*`、消息事件和 Trace 提供。

### 2. Hook 点治理

Harness 要提供 Policy 可以插入的治理节点。

第一版关键 Hook：

```text
before_model_call
after_model_call
before_tool_call
after_tool_call
after_turn
on_error
```

Hook 是插槽，不是具体规则。

### Human Confirmation 最小运行协议

当 Policy 返回 `require_confirmation` 时，由 Harness 负责把结构化请求交给外部
`ConfirmationHandler`，Core 不参与具体交互。

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
- 当前只支持进程内等待；跨进程恢复属于后续 Session / Checkpoint 能力。
- CLI、Web UI 和远程审批只实现 Handler，不改变 Policy 或 Core。

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

## Base Harness 不负责什么

第一版暂不放进 Base Harness 的能力：

```text
复杂记忆系统
复杂 skill 检索
session branch / fork / compact
subagent tree
自动 compact
自动 evolution
supervisor agent
领域深规则
```

这些能力可以后续作为模块或 Domain Harness 扩展。

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
