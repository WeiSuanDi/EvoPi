# Policy 系统设计

## Policy 是什么

Policy 是挂在 Agent 执行链路关键节点上的策略插件。

它不是普通工具，也不是单纯提示词，而是 runtime 层面的执行约束。

在 EvoPi 中，Policy 不只是一个函数，而是一个可管理资产：

```text
可注册
可启用 / 禁用
可验证
可追踪
可升级
可回滚
```

第一版不要求完整实现所有高级能力，但核心对象必须预留可演进元数据。

设计原则：

```text
功能可以 MVP，架构要 Evolution-ready。
```

## Hook 和 Policy 的区别

```text
Hook 是位置。
Policy 是挂在这个位置上的策略。
```

例如：

```text
before_tool_call 是 Hook。
shell_safety 是 Policy。
```

所有 Policy Hook 都可通过 `PolicyContext.aborted` 观察当前运行是否已进入清理阶段。
Abort 是 Core/Harness 的运行级控制事实，不参与普通 Policy 冲突合并：Policy 可以记录、
脱敏或验证中止结果，但不能重新允许已经中止的模型调用或工具执行，也不能把中止结果
改写为成功的 `terminate=True`。

`PolicyContext.error_info` 为 `on_error` 提供 Provider-neutral 的结构化模型错误，原有
字符串 `error` 继续保留。Provider Reliability v1 不增加 Policy 重试 Hook，也不允许
Policy 直接改变 Core 已确定的重试预算或错误可重试性：Adapter 负责分类，Core 负责机械
重试，Harness 负责配置。每个 attempt 都重新运行 `before_model_call`，所以 Policy 仍可在
重试前根据最新上下文阻断调用；最终失败时 `on_error` 只执行一次。

`before_model_failover` 是跨候选数据发送前的独立治理 Hook。`PolicyContext` 提供安全的
source/target `ModelAttemptInfo`、结构化错误、剩余总 attempt 预算、Circuit 快照和
`selection_reason`。当主候选在首次请求前因开路或上下文不兼容被跳过时，source 与
error 可以为空，但 target 必须完整。该 Hook 只接受 `allow / block /
require_confirmation`；rewrite、terminate 和 validation 没有路由语义，必须 fail closed。

该 Hook 在目标候选的 Context Provider、动态 Plugin Prompt 与 `before_model_call` 完成
之后执行，读取即将发送的同一 Context 快照；授权完成后不会再次改写该快照。这样跨
Provider 的数据边界不会遗漏运行时注入内容。

Provider Failover v1 不增加 Policy Retry Hook，也不允许 Policy 扩大 Retry 预算、重开
Circuit 或绕过其他限制。现有离线 Trace Replay 第一版仍只重建 `before_tool_call`；
failover 事件和 Policy evaluation 已进入 Trace，但专用 Replay Case 留给后续协议扩展。

## 三类核心 Policy

### BeforeToolPolicy

工具执行前运行。

可能返回：

- allow
- block
- require_confirmation
- rewrite_args
- add_warning

### AfterToolPolicy

工具执行后运行。

可能返回：

- cleaned_result
- mark_error
- truncate
- sanitize
- terminate

### ValidationPolicy

一轮执行后运行。

可能返回：

- pass
- fail
- need_retry
- validation_report

这里的 `need_retry` 表达领域验证结论，不等同于 Provider 瞬态错误的自动重试。

## 内置工具确认 Policy

`ToolConfirmationPolicy` 是通用的 `before_tool_call` Policy。它接收需要人工确认的
工具名集合，命中后返回 `require_confirmation`，但不负责展示界面或收集用户输入。

```python
ToolConfirmationPolicy(tool_names={"shell_command"})
```

它与安全阻断 Policy 保持独立：

```text
普通 shell_command
  → tool_confirmation: require_confirmation

危险 shell_command
  → tool_confirmation: require_confirmation
  → shell_safety: block
  → PolicyEngine 最终选择 block
```

因此人工确认不能覆盖明确的安全阻断。具体启用哪些工具由 Domain Harness 的 Policy
Pack 决定，而不是硬编码在通用 Policy 中。当前 Coding Policy Pack 默认对
`shell_command` 启用确认，工作区内的 `write_file` 不要求交互确认。

## 第一版 Policy 边界

第一版 Policy 系统固定包含 6 项能力：

```text
1. Policy 协议
2. Hook 绑定
3. PolicyDecision
4. Policy Registry
5. Policy Engine
6. Policy Pack
```

### 1. Policy 协议

每个 Policy 至少要说明：

```text
name
version
description
hooks
priority
enabled
source
risk_level
metadata
```

并提供统一执行入口：

```text
run(input) -> PolicyDecision
```

第一版可以只实现最小运行逻辑，但元数据要先保留。

### 2. Hook 绑定

Policy 必须明确挂在哪些 Hook 上。

例如：

```text
shell_safety_policy
  hook = before_tool_call

output_truncation_policy
  hook = after_tool_call

test_after_edit_policy
  hook = after_turn

branch_merge_policy
  hook = before_session_merge
```

### 3. PolicyDecision

PolicyDecision 是结构化治理结果。

第一版支持：

```text
allow
block
rewrite_args
require_confirmation
trigger_validation
terminate
```

同时携带：

```text
reason
risk_level
metadata
```

### 4. Policy Registry

Registry 负责：

```text
注册 policy
查询某个 hook 下有哪些 policy
启用 / 禁用 policy
按 priority 排序
按 policy pack 批量加载
```

### 5. Policy Engine

Engine 负责执行策略链：

```text
Hook 触发
  ↓
Registry 找到 policies
  ↓
Engine 逐个执行
  ↓
收集 decisions
  ↓
合并 final decision
  ↓
交给 Harness 应用
```

第一版冲突合并采用保守策略。

### 6. Policy Pack

Policy Pack 是一组 Policy 的组合。

例如：

```text
coding_policy_pack
  shell_safety_policy
  file_write_guard_policy
  output_truncation_policy
  test_after_edit_policy
```

它用于表达治理风格：

```text
relaxed_policy_pack
strict_policy_pack
interview_demo_policy_pack
```

第一版可以先做静态 pack，不做自动识别和自动切换。

## Evolution-ready 接口预留

第一版可以暂不完整实现这些能力，但接口和元数据要预留：

```text
policy version
policy metadata
policy enable / disable
policy priority
policy source
policy validation
policy dry-run
policy conflict resolution
policy rollback
policy generated flag
supervisor review result
```

这些字段和接口的意义是：

```text
现在可以轻实现；
未来不用推倒重构。
```

第一版可采用：

```text
validate() 只做 schema check
rollback 只记录 version，不做真实回滚
supervisor_review 只在 metadata 中预留状态
policy_source 先支持 builtins / project，后续扩展 generated
```

## 冲突处理原则

第一版可以采用保守优先级：

```text
block > require_confirmation > rewrite_args > trigger_validation > allow
```

也就是说，只要有一个 Policy 判断危险，就不直接放行。

## 受控演进闭环

```mermaid
flowchart LR
    Trace["执行 Trace"] --> Pattern["发现重复失败 / 重复需求"]
    Pattern --> Opportunity["不可变 Opportunity Report"]
    Opportunity --> Draft["生成 Policy 草稿"]
    Draft --> Schema["Schema 校验"]
    Schema --> DryRun["Dry-run / 单元测试"]
    DryRun --> Replay["Trace Replay"]
    Replay --> Supervisor["Supervisor Report"]
    Supervisor --> Human["人工确认 / Activation Gate"]
    Human --> Registry["注册启用"]
```

这个闭环表达的是：Agent 可以辅助产生新策略，但策略必须经过验证才能进入 runtime。

## Policy Pattern Discovery v1

Pattern Discovery 是演进闭环的证据蒸馏层，不是第二个 Policy Engine。它只读取用户
显式提供的历史 JSONL Trace，将反复出现的真人 `before_tool_call` Confirmation 决策
聚合为不可变 `PolicyDiscoveryReport`：

```text
policy_evaluation → confirmation_request → confirmation_response
  ↓ 严格关联与结构校验
Tool + Policy + risk + argument shape
  ↓ 至少 3 次且跨 2 个 Run
repeated_denial / mixed_decisions / repeated_approval
```

语义签名不包含原始参数值。报告用聚合 `input_digest` 绑定规范化后的输入语料，只保存
Trace SHA-256、行号、Run ID、参数字段与结构摘要；自动拒绝、取消和其他 Hook 只形成
诊断统计。损坏 JSONL、不支持的版本、重复
Request ID、断裂关联或字段契约错误会使整次发现失败且不落盘。无匹配模式则生成零
Opportunity 的有效报告。

排序固定为安全主题优先：重复拒绝、决策分歧、重复批准；同类再按风险、跨 Run 数、
频次和最近时间排序。这个排序只决定审查顺序，不建议 `allow/block` 动作。

公共 API 位于 `evopi.evolution`。CLI 使用
`evopi policy discover TRACE...`，结果原子写入
`EVOPI_HOME/opportunities/policies/reports/`。Discovery 不运行模型、工具、
Confirmation Handler、Validator 或候选源码，也不会创建、批准、启用或替换 Policy。

## `before_tool_call` Trace Replay

第一版 Trace Replay 是候选 Policy 的离线回归验证层，不是完整 Agent 回放。

Harness 在每次 Policy Engine 求值后继续记录逐条 `policy_decision`，并额外记录一个
`policy_evaluation` 快照。快照包含 Hook、原始工具调用、参数、最终决策和各 Policy
决策，保证回放不依赖模型消息历史或运行中的工具注册表。

```text
历史 JSONL Trace
  ↓ 提取 before_tool_call 案例
空 AgentContext + 工具名 + 原始参数
  ↓ 只运行候选 Policy
候选决策与同名历史决策比较
  ↓
unchanged / changed / new / error
```

状态语义固定为：

- 历史中没有同名 Policy：`new`。
- `action` 或 `rewritten_args` 不同：`changed`。
- 两者相同：`unchanged`。
- Policy Engine 捕获到候选执行异常：`error`。

`changed` 只表示行为发生变化，需要 Supervisor / Human Review，不自动使报告失败。
Trace 解析错误、候选执行错误或没有可回放案例时，验证不通过。新 Trace 优先使用
`policy_evaluation`；没有快照时，v1 Trace 回退解析 `tool_call + policy_decision`，
v2 Trace 回退解析 `tool_execution_start + policy_decision`，避免已有执行记录失效。

回放过程不得调用模型、执行工具或触发 Confirmation Handler。第一版也不重建完整
消息历史，不支持依赖工具注册表、`after_tool_call` 结果或非空 `AgentContext` 的 Policy。
公共回放入口由 `evopi.validators` 导出；Supervisor CLI 可以加载案例并编排这些既有
Validator，但回放器本身仍保持独立。

## Supervisor 离线审查报告

Supervisor Report v1 面向单个 Policy 候选，将 Schema、Dry Run 和 Trace Replay 的
既有结果聚合为统一技术结论：

```text
passed / review_required / failed
```

`build_policy_review_report()` 是同步、确定性、无副作用的纯聚合入口。它不运行
Validator、不执行候选 Policy、不调用模型或工具，也不注册、启用或替换 Policy。CLI
`evopi policy review MODULE:ATTRIBUTE` 负责在外层依次运行 Schema、可选 Dry Run 和
可选 Replay，再将结果交给聚合器。

证据规则固定为：

- Schema 必需；任何已提供检查失败、Replay 执行错误、空 Replay、Policy 名称不匹配
  或不适用于当前 Hook 的 Replay 输入都会产生 `failed`。
- 缺失 Dry Run 会产生 `review_required`。
- `before_tool_call` Policy 缺失 Replay 会产生 `review_required`；其他 Hook 将 Replay
  检查标记为 `not_applicable`。
- Validator warning 与 Replay `changed/new` 会产生可定位 Finding，并使报告进入
  `review_required`；`unchanged` 不产生待审 Finding。
- 状态优先级始终是 `failed > review_required > passed`。

Finding 只保存案例 ID、工具名、Trace 行号、历史/候选 action 和参数是否发生改写，
不复制原始工具参数或潜在敏感内容。`risk_level` 进入候选快照，但不会单独触发人工
审查；生成式候选仍由 Schema warning 进入审查。

Supervisor 的 `passed` 仅表示当前技术证据没有待处理项，不等于人工批准或已经启用。
Human Approval、ApprovalRecord 和 Active Selection 已由后续独立生命周期实现，仍不
属于 Supervisor 聚合器。

## Policy Evolution Pipeline v1

正式候选是带 `evopi-policy.json` 的目录工件。生命周期固定为：

```text
candidate → static inspection → immutable review snapshot
→ isolated Schema / Dry Run / Replay → Supervisor Evidence
→ human approve or deny → explicit active selection
→ Harness transactional reload → deactivate or rollback
```

Evidence、Approval 和 Active Selection 是三个独立事实。`review_required` 只有在人工
接受 Findings 并填写原因后才能批准，`failed` 永远不能批准。批准绑定候选摘要与报告
摘要，并复制审查快照；不会自动启用。每个名称最多一个活动摘要，回滚只能选择仍然
获批且快照完整的历史版本。

运行时 Loader 会在 import 前重新校验目录摘要和 Manifest，import 后校验实例契约，
再标注 Artifact digest、Activation ID 与 Selection ID。同名覆盖必须显式绑定替换
名称和被替换 Policy 的当前摘要；缺失或漂移均 fail closed。Coding CLI 默认装配活动
集，裸 BaseHarness 保持中立。Pattern Discovery 已提供只读机会发现；模型自动生成
候选与自动 Promotion 仍不属于 v1。

## 通用 Artifact Activation

`evopi.evolution` 为 Policy 与 Plugin 提供统一的摘要绑定激活协议：

```text
ArtifactCandidate(kind, name, version, source, risk, sha256)
→ ActivationRecord(approved|denied, operator, evidence, reason)
→ ActivationGate
```

旧 `ApprovalRecord` 仍可读取。warn 模式允许迁移期加载并记录 warning；strict 模式下
Harness 注册 Policy 必须匹配当前实现摘要，同名同版本但源码或声明契约变化会失效。
Plugin 额外复制到内容寻址不可变快照，批准不等于 OS 沙箱。

PluginAPI 不建立第二条裁决链。Plugin Event Handler、Command、Prompt Fragment、
Session State、Tool 活动覆盖和 UI 都不能直接产生 Policy 动作；需要治理 ToolCall
的 Plugin 必须注册正常 Policy，并服从同一冲突优先级。Tool
`metadata["effects"]` 提供 `read/write/execute/network/memory_write/delegate/unknown`
分类。Plan Mode 样例通过 Tool 覆盖加防御性 `before_tool_call` Policy 实现，而不是
Core 特例。

## Evo 边界

EvoPi 的演进对象分层：

```text
Policy 高频演进
Harness 低频演进
Core 默认稳定
```

其中 Policy 是第一阶段主要演进对象。因为 Policy 是明确挂在 Hook 点上的规则插件，输入输出协议稳定，新增、禁用、升级、回滚都相对可控。

Harness 也可以演进，但它改变的是运行治理框架本身，例如新增 Hook 点、调整 Policy 冲突处理方式、改变 session/subagent 治理方式。这类演进影响更大，应该低频、强验证。

Core 负责最小 Agent Loop，默认不作为自演进对象。Core 的稳定性优先级高于灵活性。

## 演进形态

当前存在两种候选形态，后续需要继续讨论取舍。

### 1. 持续挂载式演进

系统在运行中不断沉淀新的 Policy / Skill / Tool，并注册为可用扩展。

优点：

- 灵活
- 增长感强
- 适合处理长尾问题

风险：

- 扩展数量可能膨胀
- 策略之间可能冲突
- 需要良好的检索、禁用和回滚机制

### 2. Policy 组合包式演进

系统把多条 Policy 总结成可复用的策略组合。

例如：

```text
strict_coding_policy_pack
finance_audit_policy_pack
safe_shell_policy_pack
```

当识别到常见场景时，自动切换或推荐启用对应 Policy Pack。

优点：

- 更容易管理
- 更适合产品化
- 面试表达更清晰

风险：

- 需要场景识别能力
- Pack 内部策略仍需处理冲突
- 可能不如单条 Policy 灵活

## 监督者 Agent

EvoPi 的演进不能由执行 Agent 单独决定。

当系统尝试生成、修改或启用新的 Policy / Harness 规则时，需要一个隔离的监督者角色参与验证。

监督者 Agent 的职责：

- 审查新 Policy / Harness 改动是否符合协议
- 回放历史 trace 或 failure cases
- 对比演进前后的效果
- 检查是否引入新的安全风险
- 给出 pass / fail / need_human_review

监督者 Agent 不直接参与当前用户任务执行，避免“自己做题自己判卷”的问题。

第一版可以先把监督者 Agent 做成离线验证流程：

```text
candidate policy
  ↓
schema check
  ↓
dry-run replay
  ↓
supervisor review
  ↓
human confirmation
  ↓
enable / reject
```
