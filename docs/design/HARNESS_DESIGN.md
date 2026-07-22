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
failed
completed
aborted
```

Core 只负责跑 loop；Harness 负责把一次运行放进生命周期里。

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

第一版至少记录：

```text
用户输入
模型开始
模型 delta
assistant message
tool_call
policy decision
tool_result
final message
error
```

Trace 是后续 Evo 的原材料。

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
session tree
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

