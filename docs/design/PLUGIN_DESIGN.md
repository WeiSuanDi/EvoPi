# Plugin Runtime v1 设计

## 角色

Plugin 是 EvoPi 的通用运行时扩展单元。它不是一组预先枚举的
`PlanPlugin / MemoryPlugin / ToolPlugin` 类型，而是通过同一个 `PluginAPI v1`
向 Harness 声明能力：

```text
Tool / Policy / Command / Context Provider / Prompt Fragment
Session State / Active Tool View / UI / Event Observer
```

Plan Mode 只是这套通用骨架的第一个纵向样例，不是 Core 或 CodingHarness 特例。

## 信任与治理边界

批准后的 Plugin 是当前 EvoPi 进程中的受信任 Python 代码，拥有当前用户权限。
`requested_capabilities` 是审查说明，不是 OS 权限系统。PluginAPI 提供契约、装配验证
和可观测性，但不冒充沙箱。

生命周期固定为：

```text
candidate → non-executing review → digest-bound approval
→ immutable snapshot → transactional reload → active
```

- 发现和审查只读取 Manifest、计算摘要并做 AST/Schema 检查，不 import Python。
- Agent 可以生成候选，不能自行批准或激活。
- 项目候选还需要 Workspace Trust。
- 源码摘要变化不会替换已批准快照；新版本必须重新审查和批准。
- Reload 在暂存注册表中验证全部贡献，失败时旧 Runtime 保持完整。

## PluginAPI v1

`PLUGIN_API_VERSION = 1`。Manifest 可显式声明 `api_version`；旧 Manifest 默认按 v1
解释并产生迁移 warning。

公共能力：

- `register_tool(tool, replace=False)`：注册 Tool；未声明 effect 时为 `unknown`。
- `register_policy(policy)` / `load_policy_pack(pack)`：进入唯一 Policy 裁决链。
- `register_command()`：注册同步或异步 Slash Command。
- `register_context_provider()`：在模型调用前贡献结构化上下文。
- `register_prompt_fragment()`：每次模型调用前动态贡献附加 Prompt，不替换 Harness
  System Prompt。
- `api.tools`：查询全部/活动 Tool，并设置插件所有者隔离的 run/session 覆盖。
- `api.state`：读写 Session Tree 中该插件的命名空间。
- `api.ui`：通知、确认、选择、输入和状态展示。
- `api.on()`：订阅观察事件；返回值不能改变执行。

Plugin 不获得 BaseHarness 私有字段。所有注册先暂存、验证冲突和依赖，再原子提交。
宿主内置命令不可覆盖；重复 Tool 默认失败。显式 `replace=True` 是高风险审查信号。

## Policy 是唯一裁决链

Event Handler 只能观察。非空返回值产生契约 warning；异常产生
`plugin_handler_error`，不得隐式转换成 allow/block。

需要允许、阻断、确认、改写或触发验证的扩展必须注册 Policy，并服从全局冲突优先级：

```text
block > require_confirmation > rewrite_args > trigger_validation > allow
```

## Tool Effect 与活动视图

Tool 使用 `metadata["effects"]` 声明一个或多个 effect：

```text
read / write / execute / network / memory_write / delegate / unknown
```

所有者隔离的 Tool 覆盖只能收窄能力。最终活动集是 Harness 基础集合与所有插件限制的
交集，任何插件都不能重新启用被另一层关闭的 Tool。`run` 覆盖在 Run 后清除；
`session` 覆盖写入插件状态并随分支、切叶和恢复投影。

## Session State 与 UI

Session schema v3+ 的 `PluginStateEntry` 保存 `set/delete` 事实。状态沿活动叶投影，
Checkpoint 只缓存投影；不一致时从日志重建。单值上限 64 KiB，单插件活动投影上限
1 MiB，所有值必须严格 JSON-safe。

`PluginUI` 是宿主无关协议。非交互宿主：

- 允许通知和状态空操作；
- 确认默认拒绝；
- 选择和输入明确抛出 `PluginUIUnavailableError`。

REPL Adapter 在模态交互前暂停 Rich Live 区域，结束后恢复。UI Event/Trace 只记录
插件、操作和结果形态，不记录标题、消息、选项文本、输入或状态值。Plugin UI 不替代
Tool Confirmation Handler，也不能批准 Policy 决策。

## SDK 与 Plan Mode 样例

SDK 指南、`basic` 模板和 `plan-mode` 样例作为 package data 随 wheel 发布：

```text
evopi plugin examples
evopi plugin init my-plugin --template basic
evopi plugin init plan-mode --template plan-mode
```

默认候选目录是 `.evopi/plugin-candidates/<name>/`。生成内容包含 Manifest、入口文件、
README 和最小测试，不自动审查、批准或加载。

Plan Mode 样例只使用通用 API：

- `/plan on|off|status` 和 `/execute`；
- Session 状态与动态规划 Prompt；
- 活动 Tool 收窄到 `effects == ["read"]`；
- 防御性 `before_tool_call` Policy 阻断所有 effectful/unknown Tool；
- `/execute` 通过 PluginUI 确认后退出，但不自动执行计划。

## v1 不包含

- OS 沙箱或受限 Python Runtime；
- Provider 注册、远程 Plugin Registry；
- 快捷键、自定义编辑器或任意 Rich Renderer；
- 通用跨插件 Event Bus、任意消息注入；
- 自动审查、自动批准或自动激活。

## CLI 与 Prompt 集成

Plugin Command 与内置命令进入同一个只读命令注册表和帮助/补全系统，但不能覆盖保留
名称。Plugin Tool 的 `prompt_snippet`、`prompt_guidelines`、`effects` 和来源可以进入
动态 Coding Prompt；只有最终活动 Tool 会被描述。Plugin Tool 覆盖只能收窄 CLI/
Harness ceiling。

`doctor` 不扫描或 import 候选目录，只静态验证已有批准记录指向的内容寻址快照。
`config show` 也不导入 Plugin。`--plugin PATH` 仅保留为明确带 warning 的不受审开发
入口，正式流程仍是 review → approve → reload。
