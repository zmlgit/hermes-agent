# PRD: kanban-workflow 插件

> **版本**：3.0 | **架构师/PM**：Sisyphus | **日期**：2026-06-26
> **状态**：定稿 (Added Configurability & Subagent Propagation)

## 1. 背景与问题
用户 fork 了 NousResearch/hermes-agent，增加了一套"看板工作流"增强。上游未接受。每次同步 upstream 都产生合并冲突。需要通过纯插件化彻底解决冲突问题。

## 2. 产品目标
把工作流增强做成**纯插件**：零核心改动、自动继承 upstream kanban 改进、功能完整、高可配置性、支持多级 Agent 协作。

## 3. Kanban 状态机与转换处理策略

### 3.1 状态枚举
todo / ready / running / blocked / done / archived

### 3.2 完整状态转换矩阵
| # | 转换 | 代码动作 | LLM 可见性 | 说明 |
|---|---|---|---|---|
| T1 | `[new] → todo` | 继承 parent notify | 📋 工具结果 |
| T2 | `todo → ready` | 自动依赖解析 | 🔇 程序处理 |
| T3 | `ready → running` | fire `kanban_task_claimed` | 🔇 程序处理 |
| **T4** | **running → done** | **auto-verify + auto-unblock + auto-complete-parent** | **📋 工具结果增强 (US-6)** | 核心工作流自动化 |
| T5 | `running/ready → blocked` | fire `kanban_task_blocked` | 📋 工具结果 |
| T6 | `blocked → ready/todo` | auto-unblock（T4 子动作） | 📋 注入 T4 结果 |
| T7 | `* → archived` | 清理 | 🔇 程序处理 |

## 4. 用户故事与验收标准

### US-1：自动创建验证任务 (Configurable)
- `_on_kanban_task_completed` 触发时，检查任务 assignee 是否在配置的 `verify_assignees` 中（默认 `["coder", "dba"]`）。
- 检查任务标题是否不包含 `trivial_keywords`（默认 `["typo", "trivial", "rename", "cosmetic", "docs", "comment"]`）。
- 满足条件则创建 `Verify: <title>` 子任务。

### US-2/3：自动解锁与自动完成
- 父任务完成时解锁所有已准备好的阻塞子任务。
- 所有子任务完成时自动完成父任务。

### US-4：继承父任务通知订阅
- `kanban_create` 触发后，将父任务的订阅关系拷贝给新子任务。

### US-5：Session 级 Board 聚焦与子代理传播 (Subagent Propagation)
- Orchestrator 指定 `board=X` 时，锁定当前 session 的 ContextVar。
- **新增**：触发 `subagent_start` 时，拦截并将父 Agent 的锁定 Board 隐式传递给子 Agent 的 session，保证跨线程协作的看板上下文一致。

### US-6：`kanban_complete` 返回值增强
- 注入 `workflow_actions`，让大模型看到自动化的连带效果。

### US-7：人类可见性与容错兜底 (Human UX & Resilience)
- 所有的自动化动作必须打印带有 `[Kanban Auto]` 前缀的 `log.info`，让人类在日志/TUI 后台可感知。
- 自动化过程中的任何异常只打 Warn 日志，**绝对不允许**抛出中断主业务流程（Fail-Open）。

## 5. 配置规范
用户可在 `~/.hermes/config.yaml` 中配置：
```yaml
plugins:
  kanban_workflow:
    verify_assignees: ["coder", "dba", "frontend"]
    trivial_keywords: ["typo", "docs"]
```
