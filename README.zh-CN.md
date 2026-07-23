# EvoPi

[English](README.md) | [简体中文](README.zh-CN.md)

**一个以 Policy 治理执行过程、面向持续演进的 Python Agent Runtime。**

EvoPi 为构建可调用模型、使用工具并接受明确运行时治理的 Agent 提供紧凑基础。稳定的 Core 负责 Agent 主循环，Harness 组织领域行为，Policy 则在清晰定义的生命周期 Hook 上检查和控制具体动作。

## 为什么选择 EvoPi

- **稳定的 Agent Core** — 提供类型化消息、流式事件、工具调用、工具结果和有界多轮执行。
- **可插拔 Harness** — 为具体领域组合 Prompt、工具、上下文、生命周期行为和 Policy。
- **Policy 运行时治理** — 可在 Hook 上允许、阻止、改写、验证或终止操作。
- **可靠的 Provider 边界** — 内置 Anthropic Messages 与 OpenAI-compatible 流式适配器，统一错误分类、流式 I/O 超时和可观测重试。
- **Trace 优先的可观测性** — 以 JSONL 记录模型、工具、Policy 与生命周期事件，便于检查和面向回放的工作流。
- **内置编码运行时** — 提供感知工作区的文件与 Shell 工具，以及保守的默认安全策略。

## 架构

```mermaid
flowchart LR
    U["应用 / 用户"] --> H["Harness"]
    H --> C["Core Agent Loop"]
    C --> M["模型适配器"]
    C --> T["工具注册表"]
    H --> P["Policy Engine"]
    H --> X["Trace"]
    P -. "治理 Hook" .-> C
```

各层职责保持明确分离：

- **Core** 执行模型 → 工具 → 结果 → 下一轮的主循环。
- **Harness** 组装运行时行为并提供治理 Hook。
- **Policy** 在 Hook 上作出结构化决策。
- **Tools** 提供能力，但不自行决定何时适合使用。
- **Trace** 保存执行记录，为调试、评估和受控演进提供依据。

## 环境要求

- Python 3.11 或更高版本
- 开发环境推荐 Python 3.12
- 一个兼容 Anthropic 或 OpenAI API 的模型端点

## 安装

### Conda

```powershell
git clone <repository-url>
cd EvoPi
conda env create -f environment.yml
conda activate EvoPi
```

### pip

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

## 配置

复制环境变量示例，并填写所使用模型服务的凭据：

```powershell
Copy-Item .env.example .env
```

Anthropic-compatible 端点使用：

```dotenv
EVOPI_PROVIDER=anthropic
ANTHROPIC_BASE_URL=https://api.anthropic.com
ANTHROPIC_AUTH_TOKEN=your-api-key
ANTHROPIC_MODEL=your-model-name
```

OpenAI-compatible 端点使用：

```dotenv
EVOPI_PROVIDER=openai-compatible
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-name
```

凭据从系统环境变量或本地 `.env` 文件加载。EvoPi 不会持久化或打印解析后的 API 密钥。

## 快速开始

在当前目录运行编码 Agent：

```powershell
evopi '检查这个项目并总结它的架构。'
```

也可以显式选择模型提供方或工作区：

```powershell
evopi --provider anthropic --workspace C:\path\to\project '运行测试并解释所有失败。'
```

基于 Harness 的 CLI 默认会对瞬态模型错误额外重试最多三次，也可以显式调整：

```powershell
evopi --max-retries 5 --model-timeout 90 '检查这个仓库。'
evopi --no-retry '执行任务，但不要自动重试模型。'
```

在 PowerShell 中，当 Prompt 含空格或引号时，建议使用单引号包裹整个 Prompt。

## Python API

```python
import asyncio
from pathlib import Path

from evopi.ai import model_from_environment
from evopi.coding import CodingHarness
from evopi.cli.confirmation import async_terminal_confirmation_handler


async def main() -> None:
    harness = CodingHarness(
        model=model_from_environment(),
        workspace=Path.cwd(),
        trace_path=Path(".evopi/trace.jsonl"),
        confirmation_handler=async_terminal_confirmation_handler,
    )
    response = await harness.prompt("检查项目结构。")
    print(response.content)


asyncio.run(main())
```

不使用编码 Harness 的最小 Agent 示例位于 [`examples/basic_agent.py`](examples/basic_agent.py)，可直接运行的 CLI 入口示例位于 [`examples/coding_agent.py`](examples/coding_agent.py)。

## 生命周期与终止

EvoPi 使用 Pi 风格的消息、Turn 和工具执行生命周期事件。客户端可以通过 `tool_call_id` 关联 `tool_execution_start` 与 `tool_execution_end`，直接读取 `is_error`，并消费 `turn_end` 或 `agent_end`，无需从自然语言回答中反推运行状态。

`ToolResult.terminate` 是工具批次级的早停提示。EvoPi 会完成当前 AssistantMessage 请求的所有工具；只有非空批次中的每个最终结果都设置 `terminate=True`，才跳过下一次模型调用。Policy 阻断、确认拒绝、工具缺失或执行失败通常会返回错误结果，并允许模型在下一轮作出解释。

`Agent.prompt()` 继续返回 `AssistantMessage`。结构化结束信息通过 `Agent.last_run` 和 `agent_end` 暴露，结束原因包括 `completed`、`terminated`、`aborted`、`error` 和 `turn_limit`。

运行中的任务可以通过 `Agent.abort()` 或 `BaseHarness.abort()` 协作式中止。该调用是同步、线程安全、幂等的，空闲时调用不会产生影响。模型流与异步工具会被取消，运行中的 Shell 进程树会被终止；当前批次中每个已请求的兄弟工具仍会获得可关联的错误结果。已经产生的模型文本会保留，未完成的工具调用不会写入正式消息，而是保存在诊断元数据中。应用可以通过 `signal`、`is_running` 和 `wait_for_idle()` 接入自己的生命周期。

如果外部直接取消正在等待 `prompt()` 的 Task，EvoPi 会先完成同样的清理，再重新抛出 `asyncio.CancelledError`。CLI 第一次 `Ctrl+C` 会触发优雅清理并以状态码 130 退出；第二次中断保留宿主运行时的强制中断行为。

## Provider 可靠性

模型 Adapter 会把 Provider HTTP 响应、流内错误、超时、连接失败、提前 EOF 和协议错误统一转换为 `ModelErrorInfo`。标准分类覆盖认证、权限、无效请求、资源不存在、上下文溢出、配额耗尽、限流、过载、超时、连接、服务端、协议和未知错误。结构化信息可通过 `ModelError.info`、`Agent.last_run.error_info`、生命周期事件、Policy 错误上下文和 JSONL Trace 获取。

裸 `Agent` 只有显式传入 `ModelRetryConfig` 才会自动重试；`BaseHarness` 与 `CodingHarness` 默认启用确定性重试：初次调用之外最多三次，退避为 2/4/8 秒。只有 `rate_limited`、`overloaded`、`timeout`、`connection` 和 `server` 会重试。合法且更长的 `Retry-After` 优先；若超过 60 秒等待上限则立即失败。

所有 attempt 都属于同一个 Run 和 Turn。Context Provider 与 `before_model_call` Policy 每次都会重新运行，`after_model_call` 只处理成功响应。失败 attempt 会以 `stop_reason=error` 完整保存在 Event 与 Trace 中，包括部分文本和 ToolCall 诊断信息，但不会写入模型上下文。`model_retry_start` / `model_retry_end` 暴露重试等待和最终结果；Abort 可以打断正在进行的模型流或退避等待。

`--model-timeout` 表示单次请求的连接及流式 I/O 空闲超时，不是整个 Run 的墙钟总时限；只要数据持续到达，健康的长流可以继续运行。

可以直接运行以下命令验证批次契约：

```powershell
python -m pytest tests/core/test_agent_loop.py::test_tool_batch_terminates_only_when_every_final_result_agrees -vv
python -m pytest tests/core/test_agent_loop.py::test_mixed_tool_batch_continues_to_summary -vv
```

第一个案例证明所有兄弟工具都会执行，随后全 `terminate` 批次才停止运行；第二个案例证明只要有一个结果不终止，循环就会继续调用模型生成总结。

## 运行时治理

内置 `CodingHarness` 会注册限定在工作区内的目录查看、文件读取、文件写入和 Shell 命令工具。默认 Policy Pack 提供：

- 危险 Shell 模式拦截；
- 未被阻断的 Shell 命令执行前人工确认；
- 写入目标工作区边界检查；
- 工具输出截断；
- 编辑后的测试提示。

Policy 是普通的类型化 Python 组件，既可以单独注册，也可以组合成可复用的 Policy Pack。Policy 决策会与模型和工具事件一起写入运行时 Trace。

`evopi` CLI 会自动安装异步交互式 `y/N` Confirmation Handler。在确认界面按下 `Ctrl+C` 会产生明确的 `cancelled` 决策并中止当前运行。Python API 用户可以注入自己的同步或异步 Handler；未配置 Handler 时，确认请求默认被拒绝。

### 离线 Policy 回放

EvoPi 可以用历史 JSONL Trace 回放候选 `before_tool_call` Policy，整个过程不会调用模型、执行工具或请求人工确认：

```python
import asyncio

from evopi.policy.builtins import ShellSafetyPolicy
from evopi.validators import load_before_tool_replay_cases, replay_policy

policy = ShellSafetyPolicy()
cases = load_before_tool_replay_cases(
    ".evopi/trace.jsonl",
    policy_name=policy.name,
)
report = asyncio.run(replay_policy(policy, cases))

print(report.unchanged_count, report.changed_count, report.passed)
```

回放会将候选决策与历史中同名 Policy 的决策进行比较。`action` 或改写参数发生变化时，结果标记为 `changed`，交给 Supervisor 或人工审查；Trace 结构损坏、候选 Policy 执行异常和空案例集会使报告不通过。新 Trace 使用生命周期 schema v2；无版本的 v1 记录和尚未包含 Policy Evaluation 快照的旧 Trace 都能直接回放，无需改写历史文件。

### Supervisor Policy 审查

EvoPi 可以将 Schema 校验、隔离 Dry Run 与 Trace Replay 聚合成确定性的技术审查报告：

```bash
evopi policy review my_project.policies:candidate \
  --dry-run-cases my_project.review_cases:shell_cases \
  --trace .evopi/trace.jsonl
```

添加 `--json` 可输出稳定的 JSON-ready 报告。命令退出码为：`0` 表示 `passed`，`2` 表示 `review_required`，`1` 表示 `failed` 或加载错误。缺失 Dry Run、缺失适用的 Replay、Validator warning 或 `changed/new` 回放案例会要求审查；无效或相互矛盾的证据会使报告失败。

Supervisor Report 是离线技术证据工件。它不会调用模型、执行工具、注册候选 Policy 或授权启用；Human Approval 与 Activation Gate 仍是独立控制层。

> [!IMPORTANT]
> Policy 检查能够降低意外操作风险，但不能替代操作系统级沙箱。在处理不可信 Prompt、仓库或命令之前，请审查并强化相应策略。

## 开发

安装开发依赖后运行：

```bash
python -m pytest -q
python -m ruff check .
python -m mypy evopi
```

[`docs/design`](docs/design) 中的架构文档进一步介绍了 Core、Harness、Policy 与项目结构。

## 参与贡献

欢迎提交 Issue 和 Pull Request。请保持改动聚焦，为行为变化补充测试，并维护稳定 Core 机制与领域 Harness、Policy 行为之间的职责边界。
