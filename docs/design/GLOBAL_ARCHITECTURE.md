# EvoPi 全局架构图景

本文件用于固定当前阶段对 EvoPi 最终形态的理解。

## 一句话定位

EvoPi 是一个支持代码级执行策略演进的 Python Agent Runtime。

它不是“又一个代码助手”，而是尝试把 Agent 的执行链路治理能力做成可插拔、可验证、可沉淀、可演进的系统。

## 核心判断

```text
Prompt / Memory / Skill 是软性能力沉淀；
Policy / Harness 是运行时行为治理的代码级沉淀。
```

EvoPi 的重点是后者。

## 分层架构

```mermaid
flowchart TD
    User["用户 / 产品入口"] --> Harness["Harness<br/>运行治理框架"]
    Harness --> Core["Core<br/>稳定 Agent Loop"]
    Core --> Model["Model / API<br/>统一模型接入"]
    Harness --> Routing["Model Route / Circuit<br/>候选与健康状态"]
    Routing --> Model
    Core --> Tools["Tools<br/>具体动作能力"]
    Harness --> Policy["Policy<br/>可热插拔代码规则"]
    Harness --> Memory["Memory / Skills<br/>软性经验沉淀"]
    Harness --> Session["Session / Trace<br/>状态与执行轨迹"]
    Harness --> SubAgents["SubAgents<br/>多 Agent 协作"]
    Policy --> Validators["Validators<br/>schema / dry-run / replay"]
    Session --> Evolution["Evolution<br/>受控演进闭环"]
    Evolution --> Policy
    Evolution --> Harness
```

## 各层职责

### Core

Core 是稳定主线。

它负责最小 Agent Loop：

```text
model_call → tool_call → tool_result → next_turn → final_response
```

Core 不负责具体场景治理。

### Harness

Harness 是运行治理框架。

它负责定义 Agent 运行中哪些地方可以被治理：

```text
before_model_call
after_model_call
before_tool_call
after_tool_call
after_turn
before_memory_write
before_subagent_spawn
before_session_compact
```

Harness 还负责组织：

- session
- context
- events
- trace
- tools
- policy registry
- subagents
- lifecycle

一句话：

```text
Harness 定义哪里可以被治理，以及治理结果如何影响主循环。
```

### Policy

Policy 是挂在 Harness 节点上的代码级治理规则。

它负责具体判断：

```text
allow
block
rewrite_args
require_confirmation
trigger_validation
terminate
```

一句话：

```text
Policy 定义在这些位置上具体怎么治理。
```

### Tools

Tools 是 Agent 能执行的具体动作。

例如：

```text
read_file
write_file
shell_command
web_search
memory_write
subagent_spawn
```

Tool 提供能力，Policy / Harness 决定这些能力如何被允许、约束、验证。

### Memory / Skills

Memory 和 Skills 属于软性经验沉淀。

它们影响模型看到什么、知道什么、采用什么任务经验。

但它们不应该替代 runtime 层的硬约束。

### Session / Trace

Session 是跨 Run、跨进程存在的任务容器，以追加式 JSONL Entry Log 保存已经提交的
对话事实，并在每个 Run 结束后生成不可变 Checkpoint 恢复投影。层级固定为：

```text
Session → Run → Turn → Model Attempt
```

Session 的 Entry 使用 `entry_id / parent_id`，v1 保持单一活动路径，但协议为后续
branch/fork 预留 Tree 结构。裸 Core 不依赖存储；Harness 负责把正式消息和 Run 边界
接入 Session。失败 attempt 和运行治理细节不进入 Session，仍由 Trace 保存。

Trace 记录执行过程：

```text
用户输入
模型输出
工具调用
工具结果
policy decision
验证结果
错误和重试
候选切换和 circuit 状态
```

多 Provider 可靠性仍遵循同一分层：AI 层提供无 Policy 的候选与健康原语，Core 只执行
provider-neutral Attempt，Harness 在任何跨候选请求前运行 Policy / Confirmation。原始
failure domain 不进入 Trace；Session 只保存 route fingerprint，不持久化短期 Circuit 状态。

Trace 是后续演进的原材料。

Session 与 Trace 互补：Session 回答“下一次从什么正式上下文继续”，Trace 回答“此前
具体发生了什么以及为何这样治理”。详细协议见
[`SESSION_DESIGN.md`](SESSION_DESIGN.md)。

### Validators

Validators 用于验证候选升级是否可靠。

包括：

- schema check
- dry-run
- trace replay
- failure case replay
- supervisor review

## Harness 和 Policy 的关系

```text
Harness = 插槽系统 / 调度框架 / 运行治理结构
Policy = 插入插槽的具体代码规则
```

两者不是割裂关系。

```text
Harness 使用 Policy；
Policy 依附 Harness 生效。
```

## Evo 边界

当前固定为三级：

```text
Policy 高频演进
Harness 低频演进
Core 默认稳定
```

### Policy Evolution

新增、升级、禁用、回滚某个 Policy。

例如：

```text
shell_safety_policy v1 → shell_safety_policy v2
```

这是 EvoPi 的主要演进对象。

### Harness Evolution

新增 Hook 点、调整 Policy 冲突处理方式、改变 session/subagent 治理流程。

这是高级演进对象，必须强验证。

### Core Stability

Core 是稳定内核，不作为常规自演进对象。

## 演进闭环

```mermaid
flowchart LR
    Trace["Trace / Failure Cases"] --> Pattern["模式发现"]
    Pattern --> Opportunity["不可变 Opportunity Evidence"]
    Opportunity --> Candidate["候选 Policy / Harness 改动"]
    Candidate --> Schema["Schema Check"]
    Schema --> DryRun["Dry-run / Replay"]
    DryRun --> Supervisor["Supervisor Agent Review"]
    Supervisor --> Human["Human Confirmation"]
    Human --> Approval["Digest-bound Approval"]
    Approval --> Active["Explicit Active Selection"]
    Active --> Registry["Transactional Harness Reload / Rollback"]
```

原则：

```text
执行 Agent 不能自己给自己的演进打分。
```

因此需要隔离的 Supervisor Agent。

Policy Evolution v1 已把这条闭环落实为目录候选、非执行式静态检查、隔离 Worker、
不可变 Evidence、人工批准/拒绝、独立活动指针、Harness 事务装配和回滚。批准不等于
启用，技术 `passed` 也不等于人工授权。Coding CLI 默认读取当前用户活动集；裸
BaseHarness 不隐式读取用户目录。

Pattern Discovery v1 进一步补齐 Trace 到候选之前的只读入口：它只分析显式提供的
`before_tool_call` Policy/Confirmation 证据，以不含原始参数值的语义签名产生不可变
Opportunity Report。报告标记重复拒绝、决策分歧和重复批准，但不生成候选、不建议
具体 Policy 动作，也不改变运行时。

## 两种演进形态

### 持续挂载式演进

不断生成新的 Policy / Skill / Tool 并注册。

适合处理长尾问题，但需要治理扩展膨胀和冲突。

### Policy Pack 式演进

把一组 Policy 整理成可复用策略包。

例如：

```text
strict_coding_policy_pack
finance_audit_policy_pack
safe_shell_policy_pack
```

系统识别到常见场景时，推荐或切换 Policy Pack。

当前倾向：

```text
持续挂载产生经验；
Policy Pack 整理经验。
```

## 最终图景

EvoPi 的最终图景不是一个单一 Agent 产品，而是一套 Agent Runtime：

```text
稳定 Core
可扩展 Harness
可热插拔 Policy
Trace 驱动经验沉淀
Supervisor Agent 隔离验证
Human Confirmation 最终上车
```

## 运行时资源治理

可执行扩展与软性资源采用不同信任链：

```text
Policy / Plugin → Candidate → Review → digest-bound Activation → immutable snapshot
Skill / Prompt  → Workspace Trust → protected write → Trace
```

Plugin 发现阶段不得 import 候选 Python。项目 Plugin 同时需要摘要批准和 Workspace
Trust；源码变化只产生 stale 状态，运行时继续使用已批准快照。SubAgent 是父 Harness
的受治理子运行，安全 Policy、Confirmation、Abort、Deadline 与 Tool capability
ceiling 均不可弱化。
