# EvoPi — Python Agent Runtime

EvoPi 是一个 Policy-governed、Evolution-ready 的 Python Agent Runtime。CodingHarness 是第一个验证场景，不是项目本体。

## 架构边界（不可违反）

- **Core** (`evopi/core/`) — 负责执行，默认稳定，不做场景治理。只做 `model_call → tool_call → tool_result → next_turn`
- **Base Harness** (`evopi/harness/`) — 负责组织执行（Hook、Policy 调度、Context、Session、Trace）
- **Policy** (`evopi/policy/`) — 负责约束执行，高频演进对象
- **Domain Harness** (`evopi/coding/`) — 场景化装配
- 禁止将 Session 持久化、Memory、Skill、SubAgent、Shell 安全判断、文件确认逻辑塞进 Core

## 质量门禁

每次代码修改后必须：

```bash
conda activate EvoPi
python -m ruff check .        # 1. Lint
python -m mypy                # 2. 类型检查
python -m pytest -q           # 3. 测试（当前 154 项）
git diff --check              # 4. 空白
```

## 代码约定

- Python 3.11+，开发用 3.12
- 消息类型：`dataclass(slots=True, kw_only=True)`，`role` 用 `Literal` + `init=False`
- 测试：pytest 纯函数，命名 `tests/<layer>/test_<module>.py`
- 所有模块顶部 `from __future__ import annotations`
- 新增 Public API：`__all__` + `__init__.py` 导出 + 对应测试
- Line length 100（ruff）

## Policy 开发协议

新 Policy 必须声明：`name`、`version`、`description`、`hooks`、`priority`、`enabled`、`source`、`risk_level`、`metadata`。

冲突优先级（不可违反）：`block > require_confirmation > rewrite_args > trigger_validation > allow`

## 当前状态

- 测试：154 passed，Mypy 97 文件通过
- 分支：`agent/session-checkpoint-v1`，PR #6 Draft
- 下一步：Tool timeout / Run deadline → ApprovalRecord / Activation Gate → Session branch/fork/compact

## 密钥

`.env` 被 Git 忽略。禁止在任何输出中回显真实密钥。可提交模板：`.env.example`。

## 设计文档

`docs/design/` 下有 GLOBAL_ARCHITECTURE、CORE_DESIGN、HARNESS_DESIGN、POLICY_SYSTEM、SESSION_DESIGN、PROJECT_STRUCTURE。完整项目状态见 `PROJECT_STATE.md` 和 `THREAD_HANDOFF.md`。
