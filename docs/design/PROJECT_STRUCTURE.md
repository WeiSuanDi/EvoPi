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
│   ├── tools/
│   ├── memory/
│   ├── skills/
│   ├── session/
│   ├── trace/
│   ├── subagents/
│   ├── validators/
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

第一版可以先做得很薄，甚至先用 FakeModel。

## evopi/core

最小 Agent Runtime。

固定八项能力：

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

- session 状态
- session tree
- context compact
- checkpoint

Session 负责一个任务或对话窗口如何持续存在。

## evopi/trace

执行轨迹层。

职责：

- 记录用户输入
- 记录模型输出
- 记录工具调用和工具结果
- 记录 policy decision
- 记录包含输入、最终决策和逐条决策的 policy evaluation 快照
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
- supervisor review 辅助

当前验证层已提供 Policy schema check、隔离 dry-run 和离线工具级 Trace Replay。
候选 Policy / Harness 改动必须经过验证后才能启用。

## evopi/coding

Coding 场景示范层。

职责：

- coding harness
- coding tools
- coding policies
- coding prompts

Coding 是 EvoPi 的第一个验证场景，不是项目本体。

## evopi/cli

命令行入口。

负责加载模型与工作区配置、运行 CodingHarness、流式展示模型输出，并通过终端
Confirmation Handler 收集 Shell 工具的 `y/N` 人工授权。

## docs

项目设计文档。内部状态、路线和面试资料由本地协作文档维护，不进入公开仓库。

当前重点：

- 全局架构
- Core 设计
- Harness 设计
- Policy 设计
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
