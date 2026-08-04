# EvoPi 文件夹架构

本文件记录 EvoPi 当前的目标目录结构。

它不是最终不可变设计，而是当前阶段的架构锚点。

## 总体结构

```text
EvoPi/
├── evopi/
│   ├── ai/
│   ├── core/
│   ├── harness/
│   ├── policy/
│   ├── plugins/
│   ├── tools/
│   ├── memory/
│   ├── skills/
│   ├── session/
│   ├── trace/
│   ├── subagents/
│   ├── validators/
│   ├── evolution/
│   ├── coding/
│   └── cli/
├── docs/
├── examples/
└── tests/
```

## evopi/ai

模型接入层，类似 Pi 的 `packages/ai`。

职责：

- 统一不同模型厂商 API
- 管理 provider 信息
- 管理认证和 credential
- 将厂商协议转成 EvoPi 内部 Model 接口

当前已实现 OpenAI-compatible Chat Completions、原生 OpenAI Responses 与 Anthropic
Messages 流式适配器，以及无 Policy 的 `ModelRoute` / Circuit 健康原语；测试中使用脚本化
Model 作为替身，真实模型调用仍是产品主路径。候选切换治理不在 AI Adapter 中执行，
由 Harness 的 `HarnessModelAttemptRouter` 接入 Policy、Confirmation 和 Trace。

## evopi/core

最小 Agent Runtime。

固定十项能力：

```text
基础类型协议
消息上下文
模型统一接口
工具统一接口
最小 Agent Loop
工具执行结果回填
基础事件输出
Agent 对象外壳
流式输出支持
Run 内 steering / follow-up 安全点调度
```

一句话：

```text
Core 负责 Agent 能跑。
```

## evopi/harness

运行治理框架。

职责：

- 定义 hook 点
- 调度 policy
- 管理 lifecycle
- 组织 session / context / tools
- 把 Core 包装成可长期运行的系统
- 组织 Model Route、Run affinity 与 `before_model_failover` 治理

一句话：

```text
Harness 负责 Agent 怎么被组织和治理。
```

## evopi/policy

可热插拔代码规则。

职责：

- 定义 Policy 协议
- 定义 PolicyDecision
- 注册、启用、禁用、回滚 Policy
- 提供内置策略，例如 shell_safety / file_write_guard

一句话：

```text
Policy 负责具体怎么治理。
```

## evopi/plugins

插件扩展层。

职责：

- 非执行式 Manifest / AST 候选发现
- SHA-256 摘要绑定 Approval 与内容寻址不可变快照
- Workspace Trust、依赖/冲突验证与事务式 Reload
- Plugin ABC + 通用 PluginAPI v1
- PluginLoader：发现、加载、依赖校验
- wire_plugins()：Harness 装配工具
- Policy 可感知工具插件来源（tool_plugin_source）
- Command / Context Provider / Prompt Fragment / Session State / Tool View / UI
- package data SDK、候选脚手架和 Plan Mode 普通样例
- Coding `create_plugin_candidate` Tool（固定候选目录、静态审查、无授权能力）

Plugin 是 Pi Extension 的 Python 对应物：同一 API 注册 Tool、Policy、Event Handler、
Command、Context、Prompt 和状态能力。Plugin 不按功能分类；Plan Mode 是普通样例。
Plugin 和 PolicyPack 平级，由 Harness 统一装配。

一句话：

```text
Plugin 提供可热插拔的第三方扩展能力。
```

`create_plugin_candidate` 位于 `evopi/coding/tools.py`，因为它是 Coding 产品的创作工作流，
而不是 Plugin Runtime 或 Core 的默认行为；底层模板与审查仍由 `evopi/plugins` 提供。

## evopi/tools

工具能力层。

职责：

- 工具注册
- 工具 schema
- 工具执行
- 内置工具

Tool 提供动作能力，Harness / Policy 决定动作如何被约束和验证。

## evopi/memory

记忆层。

职责：

- 长期记忆存储
- 记忆检索
- 记忆写入策略

Memory 属于软性经验沉淀，不替代 Policy 的硬约束。

## evopi/skills

技能层。

职责：

- 加载 skill
- 注册 skill
- 描述 skill 的适用场景、依赖工具、风险边界和验证方法

Skill 是任务经验包，不是底层工具。

## evopi/session

会话生命周期层。

职责：

- 严格的 Session Header / Entry / Message Codec
- 追加式 JSONL Session Log 和 Tree-ready 父子关系
- 跨平台独占锁与工作区分桶
- Run-end Checkpoint 原子写入、校验和日志回退
- 中断 Run 闭合与未知 ToolResult 恢复
- 运行时指纹比较和结构化恢复 warning
- 证据绑定的 Branch Merge 规划、提交和摘要上下文投影
- Checkpoint GC 只读规划、漂移预检和结构化执行报告

Session 负责一个任务或对话窗口如何跨 Run、跨 CLI 进程持续存在。当前已实现
Tree-ready Entry、branch/fork、证据绑定 merge、compact 上下文压缩、Checkpoint 快照
和派生快照 GC。
完整协议见 [`SESSION_DESIGN.md`](SESSION_DESIGN.md)。

## evopi/trace

执行轨迹层。

职责：

- 记录用户输入
- 记录模型输出
- 记录工具调用和工具结果
- 使用 Pi 风格的 message / turn / tool execution 生命周期事件
- 记录 `abort_requested`、中止后的完整 ToolResult 和最终 `agent_end(reason=aborted)`
- 记录 policy decision
- 记录包含输入、最终决策和逐条决策的 policy evaluation 快照
- 为新记录标记 schema v2，并保留 v1 读取兼容
- 支持后续分析和 replay

Trace 是演进的原材料。

## evopi/subagents

多 Agent 协作层。

职责：

- 创建 subagent
- 控制 subagent 上下文范围
- 控制 subagent 工具权限
- 验证 subagent 回传结果

SubAgent 也必须受 Harness / Policy 管理。

## evopi/validators

验证层。

职责：

- schema check
- dry-run
- `before_tool_call` trace replay（新 Trace 快照与旧 Trace 回退解析）
- failure case replay
- 单 Policy Supervisor 技术审查报告

当前验证层已提供 Policy schema check、隔离 dry-run 和离线工具级 Trace Replay。
`PolicyDryRunCase` 支持带期望 action / 期望重写参数的强类型用例；`dry_run_policy()`
同时接受传统 `PolicyContext` 和强类型用例。`build_policy_review_report()` 将既有验证
证据纯聚合为 `passed / review_required / failed`，并通过
`evopi policy review MODULE:ATTRIBUTE` 提供文本或 JSON 输出。Supervisor 不执行
Validator、不调用模型、不修改 Registry 或候选状态；报告也不是 ApprovalRecord。
候选 Policy 必须在独立的 Human / Activation Gate 授权后才能启用。

## evopi/evolution

受控演进与工件治理层。

职责：

- 从显式 Trace 发现不含原始参数值的 Policy Opportunity
- 保存带摘要校验的不可变 Opportunity / Review Evidence
- 管理正式 Policy 候选、内容寻址快照和隔离审查
- 管理人工 Approval、全局活动选择、运行时 Artifact Loader 与回滚
- 重建 Opportunity 引用的原始证据并生成非启用 Policy 候选（唯一模型步骤）
- 保存不可变 Generation Record（不含原始参数、完整 Prompt 或模型响应）

它不进入 Core，也不建立第二条运行时裁决链。`evopi policy discover` 只产生待审
Opportunity；`policy generate` 生成非启用候选；
`init/review/approve/deny/activate/deactivate/rollback/list/status`
构成后续人工治理链。Coding CLI 通过 Harness 显式接入活动 Policy。

新增生成模块：

```text
evopi/evolution/policy_generation_protocol.py    协议、严格 codec、摘要校验
policy_generation_evidence.py                    证据重建与确定性均衡选择
policy_generation.py                            两阶段模型生成服务与候选物化
policy_generation_store.py                      不可变 Generation Record 存储
```

## evopi/coding

Coding 场景示范层。

职责：

- coding harness

## CLI 产品结构

`evopi/cli/product.py` 定义顶层产品帮助与一次性结果协议；
`evopi/cli/repl.py` 维护唯一的交互命令注册表；
`evopi/cli/runtime.py` 解析 Model Route、Failover 与 Tool ceiling；
`evopi/cli/diagnostics.py` 提供安全配置快照和离线 Doctor；
`evopi/cli/main.py` 只负责编排两层入口。

公开产品契约见 `docs/design/CLI_PRODUCT.md`。CLI 是 CodingHarness 的宿主，不代表
BaseHarness 会隐式启用 Memory、Skills、活动 Policy、磁盘 Session 或用户配置。

CLI 将 `--max-turns / EVOPI_MAX_TURNS` 解析为严格模型 Turn 总预算。Core 只记录计量；
CodingHarness 负责剩余两轮提示、最后一轮空 Tool 视图和防御 Policy。该分层避免把
Coding 产品的收尾行为固化到通用 Agent Loop。

`evopi/tools/shell_environment.py` 解析有限的 Shell 模式并产生只读实际环境；
`shell_command` 通过显式可执行程序与参数启动进程。Windows cmd 的原始命令通过
子进程私有环境变量传输，以避开 CPython argv 转义与 `cmd.exe` 引号规则不兼容，
Policy 始终先审查原始命令。Shell 选择、语法提示和诊断属于 Coding/CLI 装配，
不会下沉到 Core 或 BaseHarness。
- coding tools
- coding policies
- coding prompts

Coding 是 EvoPi 的第一个验证场景，不是项目本体。

## evopi/cli

命令行入口。

负责加载模型与工作区配置、运行 CodingHarness、流式展示模型输出，并通过异步终端
Confirmation Handler 收集 Shell 工具的 `y/N` 人工授权。第一次 `Ctrl+C` 请求优雅
Abort 并返回状态码 130；确认界面的中断映射为显式 `cancelled` 决策。普通 Prompt
默认继续当前工作区最近 Session，并支持 `--new-session / --session / --no-session /
--session-root`；`evopi session list` 提供只读列表，`evopi session gc` 对一个显式
Session 预览或执行 Checkpoint 缓存回收。
Coding CLI 默认装配用户全局活动 Policy，可用 `--no-evolved-policies` 临时关闭；
REPL `/policies` 展示装配快照，`/reload` 联合刷新 Plugin 与 Policy。
`evopi policy discover TRACE...` 离线分析显式 Trace，并保存不可变 Opportunity
Report；它不会构建 CodingHarness、调用模型或改变活动 Policy。

`evopi rpc` 通过 stdio JSONL 暴露固定的本地宿主协议。`evopi/rpc/` 包含严格 Codec、
有界 Event Stream、方法分派和只依赖 BaseHarness 公共接口的 `HarnessRpcHost`；
`evopi/cli/rpc.py` 只负责标准输入输出适配。RPC 不提供独立 Tool 执行入口，确认响应也
只能提交到 Harness 已绑定的 Confirmation Broker。

## docs

项目设计文档。内部状态、路线和面试资料由本地协作文档维护，不进入公开仓库。

当前重点：

- 全局架构
- Core 设计
- Harness 设计
- Policy 设计
- Session / Checkpoint 设计
- Plugin Runtime 设计
- 项目结构

## examples

示例任务。

用于展示：

- basic agent
- coding agent
- policy 拦截
- trace 记录

## tests

测试目录。

第一版优先覆盖：

- core loop
- policy engine
- harness 调度
- coding harness
