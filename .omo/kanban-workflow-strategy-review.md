# Kanban 插件化策略调研报告

> **状态**：调研完成，待评审
> **作者**：Sisyphus（GLM 5.2 / OhMyOpenCode）
> **日期**：2026-06-26
> **仓库**：`/home/zml/workspace/hermes-agent`（fork：zmlgit/hermes-agent，upstream：NousResearch/hermes-agent）
> **目的**：决定如何把"看板工作流增强"做成纯插件，让 fork 升级 upstream 时零冲突

---

## 0. 读者须知

本报告**自包含**——外部审阅工具不需要访问对话历史或仓库即可理解全部论据。
所有引用的文件路径、行号、diff 行数都是在仓库 `/home/zml/workspace/hermes-agent` 的当前工作树上**实测**得到的。

仓库当前处于"未提交的进行中工作"状态：存在一个名为 `plugins/kanban-workflow/` 的插件目录（约 16000 行），以及 13 个被修改的核心文件（全部未 commit）。本报告评估这份进行中工作的方向是否正确，并给出替代方案。

---

## 1. 用户诉求（来自原话）

> "我希望我能用上看板工作流这个功能，但是官方不给合，我每次更新 Hermes 都要解决冲突很烦人，所以我要做成一个外挂的插件来满足。至于怎么做，你看着来。"

**翻译成可验收的目标：**

1. **能使用**"看板工作流"功能（task-loop 自动化：auto-verify、auto-unblock、auto-complete-parent、inherit-parent-notify）
2. **零冲突**：从 upstream（`origin/main`）`git pull` 时，与看板相关的改动不产生需要人工解决的合并冲突
3. **维护成本低**：upstream 改进 kanban 时，用户能自动受益，不需要手动同步代码

---

## 2. 仓库现状

### 2.1 fork 拓扑

```
origin  → https://github.com/NousResearch/hermes-agent.git   (upstream)
fork    → git@github.com:zmlgit/hermes-agent.git             (用户 fork)
HEAD    → 41b9b7e71 test(lazy-deps): make durable-target tests network-free
```

当前 HEAD 与 `origin/main` 同步。

### 2.2 未提交改动清单（`git status --short`）

**被修改的核心文件（M）：**
```
gateway/kanban_watchers.py        ← 从 1236 行缩减到 67 行 shim
gateway/run.py                    ← 9 行改动（仅注释）
hermes_cli/kanban.py              ← 从 2822 行缩减到 30 行 shim
hermes_cli/kanban_db.py           ← 从 8377 行缩减到 30 行 shim
hermes_cli/kanban_decompose.py    ← 缩减到 23 行 shim
hermes_cli/kanban_diagnostics.py  ← 缩减到 23 行 shim
hermes_cli/kanban_specify.py      ← 缩减到 23 行 shim
hermes_cli/kanban_swarm.py        ← 缩减到 23 行 shim
plugins/kanban/dashboard/plugin_api.py  ← 从 ~大型实现 缩减到 46 行 shim
tools/kanban_tools.py             ← 从 1589 行缩减到 65 行 shim
toolsets.py                       ← 27 行删除（移除 kanban toolset 和 _HERMES_CORE_TOOLS 条目）
```

**被删除的文件（D）：**
```
plugins/kanban/dashboard/dist/index.js
plugins/kanban/dashboard/dist/style.css
plugins/kanban/dashboard/manifest.json
plugins/kanban/systemd/hermes-kanban-dispatcher.service
```

**未跟踪（??）的新文件：**
```
plugins/kanban-workflow/                      ← 新插件目录，约 16000 行
tests/plugins/test_kanban_workflow_plugin.py  ← 229 行新测试
```

### 2.3 新插件 `plugins/kanban-workflow/` 结构

```
plugins/kanban-workflow/
├── __init__.py              258 行  ← register(ctx)，注册 2 个 hook
├── plugin.yaml               17 行  ← manifest
├── kanban_db.py            8320 行  ← DB 层
├── kanban.py               2814 行  ← CLI/逻辑
├── model_tools.py          1549 行  ← 模型工具注册
├── kanban_watchers.py      1184 行  ← gateway watcher
├── kanban_diagnostics.py   1107 行
├── kanban_decompose.py      477 行
├── kanban_specify.py        273 行
├── kanban_swarm.py          278 行
├── dashboard/
│   ├── plugin_api.py                  ← dashboard 后端
│   ├── dist/{index.js, style.css}     ← 前端 bundle
│   └── manifest.json
└── systemd/
    └── hermes-kanban-dispatcher.service
```

### 2.4 前一会话采用的策略（推断自代码）

把 `hermes_cli/kanban*.py`、`tools/kanban_tools.py`、`gateway/kanban_watchers.py`、`plugins/kanban/dashboard/plugin_api.py` **整个实现**搬到插件目录，然后把核心文件改成 30~65 行的 shim，shim 通过 `importlib.util.spec_from_file_location` 在原模块名下加载插件实现，保证 `from hermes_cli import kanban_db` 这类历史导入仍然能工作。

shim 示例（`hermes_cli/kanban.py` 全文，30 行）：

```python
"""Compatibility module for the kanban-workflow CLI/slash surface."""
from __future__ import annotations
import importlib.util, sys
from pathlib import Path

_PLUGIN_FILE = (
    Path(__file__).resolve().parents[1]
    / "plugins" / "kanban-workflow" / "kanban.py"
)
_spec = importlib.util.spec_from_file_location(__name__, _PLUGIN_FILE)
_module = importlib.util.module_from_spec(_spec)
sys.modules[__name__] = _module
_spec.loader.exec_module(_module)
```

---

## 3. 关键发现：upstream 本身就有完整 kanban

这是整个调研中最重要的事实，**与前一会话的假设相反**。

### 3.1 upstream 包含全部 kanban 文件

通过 `git ls-tree -r origin/main --name-only | grep kanban` 确认，以下文件**都在 upstream**：

```
hermes_cli/kanban.py               ← IN UPSTREAM（完整实现，2820 行）
hermes_cli/kanban_db.py            ← IN UPSTREAM（完整实现，8377 行）
hermes_cli/kanban_decompose.py     ← IN UPSTREAM
hermes_cli/kanban_diagnostics.py   ← IN UPSTREAM
hermes_cli/kanban_specify.py       ← IN UPSTREAM
hermes_cli/kanban_swarm.py         ← IN UPSTREAM
tools/kanban_tools.py              ← IN UPSTREAM（完整实现，1560 行）
gateway/kanban_watchers.py         ← IN UPSTREAM（完整实现，1185 行）
plugins/kanban/dashboard/plugin_api.py  ← IN UPSTREAM
plugins/kanban/dashboard/dist/*    ← IN UPSTREAM
plugins/kanban/systemd/*           ← IN UPSTREAM
```

`toolsets.py` 和 `gateway/run.py` 也都在 upstream，且 upstream 版本的 `toolsets.py` 里**包含 kanban toolset 定义**，`gateway/run.py` 里**包含 kanban dispatcher 接线**。

### 3.2 upstream 已经支持工作流需要的 hook

**事实 1：upstream 的 `hermes_cli/kanban_db.py` 在 `complete_task()` 里 emit `kanban_task_completed` hook。**

`origin/main:hermes_cli/kanban_db.py` 第 3990~4010 行（实测）：

```python
_clear_failure_counter(conn, task_id)
recompute_ready(conn)
_cleanup_workspace(conn, task_id)
_done_task = get_task(conn, task_id)
_fire_kanban_lifecycle_hook(
    "kanban_task_completed",
    task_id,
    board=get_current_board(),
    assignee=_done_task.assignee if _done_task else None,
    run_id=run_id,
    summary=(summary if summary is not None else result),
)
```

**事实 2：upstream 的 `hermes_cli/plugins.py` 把 `kanban_task_completed` 列为受支持的插件 hook。**

`origin/main:hermes_cli/plugins.py` 第 170~196 行（实测）：

```python
# Kanban task lifecycle hooks. Fired by hermes_cli.kanban_db when a task
# transitions state, AFTER the change is committed to the board DB ...
#   - kanban_task_completed -> the WORKER process, when it calls
#                              kanban_complete (or a CLI/manual complete).
# Common kwargs: task_id: str, board: str | None, assignee: str | None,
#   run_id: int | None, profile_name: str.
# kanban_task_completed adds: summary: str | None.
"kanban_task_claimed",
"kanban_task_completed",
"kanban_task_blocked",
}
```

也就是说：**工作流插件需要的两个 hook（`post_tool_call` + `kanban_task_completed`）upstream 都原生支持**，不需要任何核心改动来"启用"它们。

### 3.3 upstream kanban_db 的公开 API 完全够用

插件 `__init__.py` 用到的 kanban_db API（实测全部在 `origin/main:hermes_cli/kanban_db.py` 中存在）：

| API | upstream 中存在 |
|---|---|
| `connect(board=...)` | ✓（2 处定义） |
| `get_current_board()` | ✓ |
| `parent_ids(conn, task_id)` | ✓ |
| `add_notify_sub(conn, ...)` | ✓ |
| `create_task(conn, ...)` | ✓ |
| `get_task(conn, task_id)` | ✓ |
| `unblock_task(conn, cid)` | ✓ |
| `complete_task(conn, pid, summary=...)` | ✓ |

---

## 4. 当前插件代码与 upstream 的差异（实测 diff 行数）

把 `origin/main` 的对应文件 dump 出来，与插件版本做 `diff`：

| 插件文件 | vs upstream 对应文件 | diff 行数 | 性质 |
|---|---|---|---|
| `kanban_decompose.py` | `hermes_cli/kanban_decompose.py` | **0** | 完全相同 |
| `kanban_specify.py` | `hermes_cli/kanban_specify.py` | **0** | 完全相同 |
| `kanban_swarm.py` | `hermes_cli/kanban_swarm.py` | **0** | 完全相同 |
| `kanban_diagnostics.py` | `hermes_cli/kanban_diagnostics.py` | **0** | 完全相同 |
| `kanban_watchers.py` | `gateway/kanban_watchers.py` | 17 | 仅 docstring 改动 |
| `kanban.py` | `hermes_cli/kanban.py` | 9 | 几乎相同 |
| `kanban_db.py` | `hermes_cli/kanban_db.py` | 117 | **插件删除了 upstream 的 `project_id` 字段**（见 §4.1） |
| `model_tools.py` | `tools/kanban_tools.py` | 189 | 工具注册方式重构 + **删除 upstream 的 `project` 参数** |

**结论：插件中约 15000 行是 upstream kanban 的逐字拷贝**，真正的"工作流增强"只有 `__init__.py` 那 258 行。

### 4.1 插件在开倒车：删除了 upstream 的 `project_id` 功能

`origin/main:hermes_cli/kanban_db.py` 比 `plugins/kanban-workflow/kanban_db.py` 多出的 117 行 diff 中，包含以下 upstream 新功能（插件没有）：

- `tasks` 表的 `project_id TEXT` 列（链接到 `hermes_cli/projects_db`）
- `create_task()` 的 `project_id` 参数
- 项目链接任务的 worktree 自动锚定逻辑（在项目主仓库下用确定性分支名 `<project-slug>-<task-id>`，而非随机的 `wt/<task-id>`）
- 把 `_add_column_if_missing` 抽到 `hermes_cli/sqlite_util`（插件保留本地副本）

`model_tools.py` 同样删除了 upstream 的 `project` 工具参数（让 agent 创建任务时可以指定 project）。

**这些不是工作流增强，是 upstream 已经合并、插件却丢失的能力。**

---

## 5. 为什么 shim 策略反而增加冲突

### 5.1 冲突面盘点

shim 化的核心文件**全都是 upstream 活跃维护的文件**。每次 upstream 修改它们，用户拉取时都会撞上"upstream 几千行实现 vs 用户 30 行 shim"的冲突，git 无法自动合并：

| 被改成 shim 的文件 | upstream 是否活跃 |
|---|---|
| `hermes_cli/kanban_db.py` | 是（最近加了 `project_id`） |
| `hermes_cli/kanban.py` | 是 |
| `tools/kanban_tools.py` | 是（最近加了 `project` 参数） |
| `gateway/kanban_watchers.py` | 是 |
| `gateway/run.py` | 是（高频改动） |
| `toolsets.py` | 是（高频改动） |
| `plugins/kanban/dashboard/*` | 是 |

### 5.2 双重维护负担

更糟的是，插件里有 8 个大文件是 upstream kanban 的拷贝。每次 upstream 改进 kanban（修 bug、加功能、重构），用户都要手动把改动 port 到 `plugins/kanban-workflow/` 对应文件里，否则插件的 kanban 实现会越来越落后 upstream——这正是 §4.1 已经发生的情况（`project_id` 已经丢失）。

**这与用户诉求"维护成本低"完全相反。**

### 5.3 测试已经出现回归

跑迁移后的测试，15 个用例失败（386 通过）：

```
tests/plugins/test_kanban_dashboard_plugin.py  ← 14 个失败
tests/plugins/test_kanban_worker_runs.py       ← 1 个失败（test_inspect_run_live_pid）
```

失败原因都是**测试文件还在引用旧路径** `plugins/kanban/dashboard/dist/index.js`，但迁移把这些文件移走了。这些测试是 upstream 维护的（在 `git status` 里没被修改），用户没义务改它们——但现在它们挂了，说明迁移破坏了 upstream 测试。

---

## 6. 推荐方案：极简插件 + 零核心改动

### 6.1 核心思路

**保留 upstream 的 kanban 代码原封不动。把真正属于用户的"工作流增强"（258 行 task-loop 自动化）做成纯插件，叠加在 upstream kanban 之上。**

### 6.2 目标插件结构

```
plugins/kanban-workflow/
├── plugin.yaml          # ~20 行 manifest，声明 2 个 hook
└── __init__.py          # ~250 行，保留现有 task-loop 逻辑
```

**删除：**
- `kanban_db.py` / `kanban.py` / `model_tools.py` / `kanban_watchers.py`（upstream 已有）
- `kanban_decompose.py` / `kanban_specify.py` / `kanban_swarm.py` / `kanban_diagnostics.py`（upstream 已有，0 diff）
- `dashboard/` / `systemd/`（upstream 已有）

### 6.3 改动清单

**步骤 1：撤销所有核心文件修改**

```bash
git checkout HEAD -- \
  hermes_cli/kanban.py hermes_cli/kanban_db.py \
  hermes_cli/kanban_decompose.py hermes_cli/kanban_diagnostics.py \
  hermes_cli/kanban_specify.py hermes_cli/kanban_swarm.py \
  tools/kanban_tools.py gateway/kanban_watchers.py \
  gateway/run.py toolsets.py \
  plugins/kanban/dashboard/plugin_api.py
git checkout HEAD -- \
  plugins/kanban/dashboard/dist/index.js \
  plugins/kanban/dashboard/dist/style.css \
  plugins/kanban/dashboard/manifest.json \
  plugins/kanban/systemd/hermes-kanban-dispatcher.service
```

执行后所有核心文件回到 upstream 状态，**零冲突面**。

**步骤 2：精简 `plugins/kanban-workflow/__init__.py`**

保留现有 258 行逻辑（task-loop hooks），但：
- 把 `_get_conn()` 里的 `from hermes_cli import kanban_db as _kb` **保留**（直接用 upstream 的，不再用插件副本）
- 删除任何对插件内 `model_tools` / `kanban_db` 等模块的引用

**步骤 3：精简 `plugins/kanban-workflow/plugin.yaml`**

```yaml
name: kanban-workflow
version: 0.3.0
description: Task-loop automation hooks for upstream kanban (auto-verify, auto-unblock, auto-complete-parent, inherit-parent-notify).
kind: backend
provides_hooks:
  - post_tool_call
  - kanban_task_completed
```

不声明 `provides_tools`——工具用 upstream 的 `kanban_show` / `kanban_complete` 等。

**步骤 4：删除插件的 8 个大文件 + dashboard + systemd**

```bash
rm plugins/kanban-workflow/kanban_db.py
rm plugins/kanban-workflow/kanban.py
rm plugins/kanban-workflow/model_tools.py
rm plugins/kanban-workflow/kanban_watchers.py
rm plugins/kanban-workflow/kanban_decompose.py
rm plugins/kanban-workflow/kanban_specify.py
rm plugins/kanban-workflow/kanban_swarm.py
rm plugins/kanban-workflow/kanban_diagnostics.py
rm -r plugins/kanban-workflow/dashboard
rm -r plugins/kanban-workflow/systemd
```

**步骤 5：重写测试 `tests/plugins/test_kanban_workflow_plugin.py`**

删除以下用例（它们测的是 shim 路径，shim 不复存在）：
- `test_legacy_kanban_tools_import_exports_plugin_implementation`
- `test_legacy_kanban_db_import_exports_plugin_implementation`
- `test_legacy_kanban_cli_import_exports_plugin_implementation`
- `test_legacy_kanban_auxiliary_modules_export_plugin_implementations`
- `test_kanban_workflow_dashboard_manifest_owns_kanban_tab`
- `test_legacy_kanban_dashboard_import_exports_plugin_implementation`
- `test_register_kanban_tools_without_plugin_context_seeds_registry`
- `test_bundled_kanban_workflow_registers_tools_and_hooks`（改成只断言 hooks，不断言 tools）

保留以下行为测试（它们测的就是 task-loop 自动化）：
- `test_post_tool_call_inherits_parent_notify`
- `test_task_completed_hook_creates_verification_child`

**步骤 6：验证**

```bash
# 工作流插件自身测试通过
scripts/run_tests.sh tests/plugins/test_kanban_workflow_plugin.py

# upstream 的 kanban 测试全部通过（零回归）
scripts/run_tests.sh tests/tools/test_kanban_tools.py
scripts/run_tests.sh tests/plugins/test_kanban_dashboard_plugin.py
scripts/run_tests.sh tests/plugins/test_kanban_worker_runs.py
# ... 其他 kanban 测试

# 验证 plugin register 不报错
python -c "from hermes_cli.plugins import PluginManager; m = PluginManager(); m.discover_and_load(); print('OK')"
```

### 6.4 预期收益

| 维度 | 当前方案（16k 行 + shim） | 推荐方案（300 行） |
|---|---|---|
| 核心文件改动 | 13 个文件 | **0 个** |
| upstream pull 时的冲突 | 每次都有，且难解（shim vs 实现） | **零** |
| upstream kanban 改进 | 手动 port 到插件，已落后（缺 `project_id`） | **自动继承** |
| 插件体积 | ~16000 行 | **~300 行** |
| 工作流功能 | 完整 | **完整**（同样 4 个行为） |
| 测试维护 | 需要维护 shim 路径测试 + dashboard 测试 | **只维护 2 个行为测试** |

---

## 7. 风险与未验证的假设

报告必须诚实标注不确定性，请评审重点核查这些：

### 7.1 已验证（高置信度）

- ✅ upstream 包含全部 kanban 文件（`git ls-tree` 实测）
- ✅ upstream emit `kanban_task_completed` hook（读源码第 4005 行）
- ✅ upstream `plugins.py` 把该 hook 列为受支持（读源码第 193 行）
- ✅ upstream kanban_db 的 8 个 API 全部存在（grep 实测）
- ✅ 插件 4 个文件与 upstream 0 diff（`diff` 实测）
- ✅ 当前迁移测试有 15 个失败（pytest 实测）

### 7.2 推断（中置信度，建议复核）

- ⚠️ **推断**：插件 `__init__.py` 里那 258 行 task-loop 逻辑就是用户想要的"全部工作流增强"。
  - 依据：插件 `plugin.yaml` 声明的 `provides_hooks` 只有这两个；`__init__.py` 是唯一非拷贝的逻辑文件。
  - 风险：用户可能还有其他未在插件里体现的工作流需求（例如自定义 dispatcher 行为、自定义 dashboard 视图）。
  - 建议：**向用户确认 4 个行为（inherit-notify / auto-verify / auto-unblock / auto-complete-parent）是否完整覆盖需求**。

- ⚠️ **推断**：upstream 的 `_fire_kanban_lifecycle_hook` 在 worker 进程里触发，所以 hook 内调用 `kanban_db.connect()` 能读到一致的状态。
  - 依据：upstream `plugins.py` 注释说 hook 在 worker 进程触发，且 "AFTER the change is committed to the board DB"。
  - 风险：如果 worker 进程的 `HERMES_KANBAN_BOARD` 环境变量与 dispatcher 不一致，hook 可能连到错误的 board。
  - 建议：评审时验证插件 hook 里 `kanban_db.get_current_board()` 的返回值在 worker 上下文中正确。

### 7.3 未验证（低置信度，必须复核）

- ❓ **未查**：插件 `__init__.py` 里用到的 SQL 表结构（`kanban_notify_subs`、`task_links`、`tasks`）是否与 upstream 的 schema 完全一致。如果 upstream 改过表结构（例如 `kanban_notify_subs` 加了字段），插件里的 SQL 可能失败。
  - 建议：评审时 `git show origin/main:hermes_cli/kanban_db.py | grep -A5 "CREATE TABLE.*notify_subs\|CREATE TABLE.*task_links"` 对比插件 `__init__.py` 里的 SELECT 列名。

- ❓ **未查**：`register(ctx)` 里的 `register_kanban_tools(ctx)` 调用——如果删除插件的 `model_tools.py`，这个调用会失败。推荐方案里要么删除这个调用（工具用 upstream 的），要么保留一个最小的 `model_tools.py` 仅暴露 `register_kanban_tools` 作为 no-op。
  - 建议：评审时确认 upstream 是否已经通过 `tools/kanban_tools.py` 注册了同名工具——如果是，插件**不能**再注册一次（会冲突或覆盖）。

- ❓ **未查**：`post_tool_call` hook 的 `args` 和 `result` 参数格式。插件 `_on_post_tool_call` 假设 `args` 是 dict、`result` 是 JSON 字符串。需要核对 upstream `plugins.py` 里 `post_tool_call` 的实际调用签名。

- ❓ **未查**：gateway dispatcher 是否需要工作流插件参与。AGENTS.md 提到 "kanban.dispatch_in_gateway: true"，dispatcher 跑在 gateway 里。如果工作流增强需要改变 dispatcher 行为（不只是响应 task 完成事件），纯 hook 方案可能不够。
  - 建议：评审时确认 dispatcher 的 tick 循环是否有 hook 点，或者用户的 4 个工作流行为是否都能通过响应 `kanban_task_completed` 事件实现（从代码看是的，但值得 double-check）。

---

## 8. 待用户确认的问题

1. **是否同意抛弃当前 16k 行复制式迁移、改走极简插件？** 这会丢掉前一会话绝大部分工作（但那些工作本来就在制造问题）。
2. task-loop 的 4 个行为（inherit-notify / auto-verify / auto-unblock / auto-complete-parent）**是否完整覆盖**你要的"看板工作流"？有没有要加/减的？
3. 是否接受把 `tests/plugins/test_kanban_dashboard_plugin.py` 和 `test_kanban_worker_runs.py` **交还给 upstream 维护**（你不再碰它们，它们随 upstream 走）？

---

## 9. 评审请求

请评审者重点核查：

1. **第 3 节的事实主张**：upstream 是否真的包含全部 kanban + 支持 `kanban_task_completed` hook？（这是整个方案的前提）
2. **第 5 节的冲突分析**：shim 策略是否真的会增加冲突？有没有办法让 shim 与 upstream 并存而不冲突？
3. **第 6 节的推荐方案**：极简插件是否能完整实现用户的 4 个工作流行为？有没有遗漏的依赖？
4. **第 7.3 节的未验证假设**：哪些必须在实施前补查？
5. **替代方案**：是否存在比"极简插件"更好的路线（例如：把工作流增强 PR 给 upstream、用 monkey-patch 而非 hook、用独立 pip 包而非内置插件）？

---

## 附录 A：关键代码引用

### A.1 upstream `kanban_task_completed` emit 点

文件：`origin/main:hermes_cli/kanban_db.py` 第 3990~4010 行
函数：`complete_task()`
触发时机：任务成功完成后、failure counter 清零后、`recompute_ready` 后、workspace 清理后

### A.2 upstream 受支持的 kanban hook 列表

文件：`origin/main:hermes_cli/plugins.py` 第 196~199 行

```python
"kanban_task_claimed",
"kanban_task_completed",
"kanban_task_blocked",
}
```

### A.3 工作流插件的 4 个行为（来自 `plugins/kanban-workflow/__init__.py`）

1. **`_on_post_tool_call`**（响应 `kanban_create` 工具调用）
   - 当 headless 子任务创建且未订阅通知时，从父任务继承 `kanban_notify_subs` 记录

2. **`_on_kanban_task_completed` → `_auto_verify`**
   - 当 `coder` / `dba` 完成非 trivial 任务且没有 tester 子任务时，自动创建 `Verify: <title>` 子任务分配给 `tester`

3. **`_on_kanban_task_completed` → `_auto_unblock_children`**
   - 当任务完成且其子任务处于 blocked 状态、且子任务的所有 parent 都已完成时，自动 unblock 子任务

4. **`_on_kanban_task_completed` → `_auto_complete_parent`**
   - 当任务完成且其父任务的所有子任务都已完成时，自动 complete 父任务

---

## 附录 B：调研方法

- `git ls-tree -r origin/main --name-only | grep kanban`：列出 upstream 全部 kanban 文件
- `git cat-file -e origin/main:<file>`：判断文件是否在 upstream
- `git show origin/main:<file> > /tmp/upstream_*.py`：dump upstream 版本
- `diff /tmp/upstream_*.py plugins/kanban-workflow/*.py | wc -l`：量化差异
- `git show origin/main:hermes_cli/kanban_db.py | grep -n ...`：定位 hook emit 点
- `python -m pytest tests/plugins/test_kanban_workflow_plugin.py`：跑插件测试（10/10 通过）
- `python -m pytest tests/tools/test_kanban_*.py tests/plugins/test_kanban_*.py`：跑全 kanban 测试套件（386 通过、15 失败）

所有命令的原始输出可应要求提供。

---

# 附录 C：Oracle 评审结果（2026-06-26）

> 评审者：Oracle（read-only 高质量推理模型，GLM 5.2）
> 评审方式：读完报告后独立查证仓库代码，核查每条事实主张

## 总判定

**AGREE WITH CAVEATS（同意，需修正两点）**

报告的核心推荐方向（抛弃 16k 行复制、改成 ~300 行纯 hook 插件、所有核心文件回滚到 upstream）是**正确且有据的**。修正点：

1. 极简插件的 `plugin.yaml` **必须不含 `provides_tools`**，且 `register()` **必须不调用 `register_kanban_tools`** ——否则 9 个 kanban 工具会和 upstream 自动发现的 `tools/kanban_tools.py` **双重注册**。
2. `kind: backend` 对纯 hook 插件**语义错误**，应改为 `kind: standalone` 或确认 upstream 对 hook 插件的自动加载意图。

## 事实核验（报告 §3 各主张）

| 主张 | 状态 | 证据 |
|---|---|---|
| §3.1 upstream 包含全部 kanban 文件 | **VERIFIED** | `git ls-tree -r origin/main` 列出所有文件 |
| §3.2 事实1：`kanban_task_completed` 在 upstream `kanban_db.py` ~L4005 emit | **VERIFIED** | `_fire_kanban_lifecycle_hook("kanban_task_completed", ...)` 在 `origin/main:hermes_cli/kanban_db.py:4004-4014`，位于 `complete_task()` 内 |
| §3.2 事实2：`plugins.py` ~L193 列出该 hook | **VERIFIED** | `origin/main:hermes_cli/plugins.py:192-194` 列出 `kanban_task_claimed/completed/blocked`；`post_tool_call` 在 L130 |
| §3.3 8 个 kanban_db API 全部存在 | **VERIFIED** | connect (L1584)、get_current_board (L345)、parent_ids (L2733)、add_notify_sub (L7890)、create_task (L2232)、get_task (L2551)、unblock_task (L4518)、complete_task (L3824) |
| §2.1 "HEAD 与 origin/main 同步" | **REFUTED** | HEAD=41b9b7e7 vs origin/main=6dfb8326，HEAD 落后 20+ 提交。**不影响结论**：所有 §3 核验都正确使用 `origin/main:`，所以判定成立。但意味着 `git pull` 会把 `project_id` 工作（插件副本里缺失的）拉进来——反而**加强**了 §4.1 的论据 |
| §4 diff 行数（0/0/0/0、117、189、9、17） | **VERIFIED** | 8 个 `diff` 计数全部复现；`project_id` 在插件中为 0 vs upstream 14 处；`project` 参数确认从插件 `model_tools.py` 移除 |
| §5.3 15 个测试失败 | **PLAUSIBLE（未重跑）** | `git status` 确认 `plugins/kanban/dashboard/dist/*` 被删（D）且对应测试未被改动——与报告的失败模式一致。未重跑 400 个测试 |

## 报告**遗漏**的重要风险

1. **确认的 9 个工具双重注册风险（报告 §7.3 已 flag，现已证实）**
   - Upstream `tools/kanban_tools.py` 通过自动发现 + `registry.register(toolset="kanban", name="kanban_show"…)` 注册全部 9 个工具（`origin/main:tools/kanban_tools.py:1481-1554`）。
   - 插件 `__init__.py:253-255` 调用 `register_kanban_tools(ctx)`，注册**相同的 9 个名字**（`plugins/kanban-workflow/model_tools.py:1532-1549`）。
   - 当前 `plugin.yaml` 还把这 9 个列在 `provides_tools` 下。
   - 报告 §6.3 步骤 2 说"删除对插件 model_tools 的引用"，但**没有显式指出** `plugin.yaml` 里的 `provides_tools` 也必须移除——步骤 3 的示例 YAML 隐式省略了它，但如果照字面执行可能漏改。
   - **必须修正**：极简插件的 `plugin.yaml` 不能有 `provides_tools`，`register()` 不能调用 `register_kanban_tools`。

2. **`kind: backend` 对纯 hook 插件语义错误**
   - Upstream `_VALID_PLUGIN_KINDS` 定义 `backend` 为"现有核心工具的可插拔后端（如 image_gen）"（`plugins.py:276-277`）。
   - 捆绑的 `backend` 插件会自动加载（`plugins.py:1325`）。
   - 报告 §6.3 步骤 3 保留 `kind: backend`——可能是为了自动加载，但纯 hook 插件的正确 kind 应是 `standalone`（需显式 `hermes tools`/config 启用），或需要确认 upstream 对 hook 插件的自动加载是否真的需要 `backend`。
   - 这是潜在的配置语义 bug，不是硬阻塞。

3. **§5 冲突面分析不完整**
   - 报告列了 7 个被 shim 化的源文件作为冲突源，但**遗漏了 5 个被改动的测试文件**：`tests/test_toolsets.py`（+22 行）、`tests/tools/test_kanban_tools.py`、`tests/hermes_cli/test_kanban_core_functionality.py`、`tests/gateway/test_kanban_watchers_mixin.py`、`tests/gateway/test_kanban_auto_decompose_live.py`。
   - Upstream 都在活跃维护这些测试，每次 pull 都会冲突。
   - 极简插件方案会通过 `git checkout HEAD --` 把这些也回滚——**反而加强了**推荐，但报告应该枚举清楚。

4. **`post_tool_call` 签名假设正确（解决 §7.3）**
   - Upstream 触发 `invoke_hook("post_tool_call", tool_name=…, args=…, result=…, task_id=…, session_id=…, …)`（`model_tools.py:880-895`）。
   - 插件 `_on_post_tool_call(tool_name, args, result, **_unused)` 匹配；多余参数被 `**_unused` 吞掉。无风险。

5. **SQL schema 假设正确（解决 §7.3）**
   - 插件查询引用的 `task_links(parent_id, child_id)`、`tasks(id, assignee, status, title)`、`kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile)`——所有列在 `origin/main:hermes_cli/kanban_db.py:1012-1167` 都存在。无风险。

## 替代方案评估（报告未提到的）

**(a) 把 4 个工作流 hook PR 给 upstream —— 长期最优，接受度不确定**
- 4 个行为（auto-verify / unblock / complete-parent / inherit-notify）是通用的 task-loop 自动化，坐在 upstream 自己的 hook 上。
- 如果泛化（硬编码的 `_IMPLEMENTATION_ASSIGNEES={"coder","dba"}` 和 `_TRIVIAL_KEYWORDS` 改成 config），这是一个合理的 upstream 贡献，能**彻底消除插件**。
- **权衡**：高延迟（PR review）、接受度不确定（upstream 可能认为太 opinionated）、用户失去迭代速度的控制。
- **判定**：值得作为**并行**路线尝试，但短期内不能替代插件。

**(b) 用 pip 包代替捆绑插件 —— 略干净，但没更好**
- 同样的 hook 代码，通过 `pip install hermes-kanban-workflow` + entry point 分发。
- 解决不了任何捆绑插件解决不了的问题（hook 代码相同，只是分发方式不同）。
- 给用户多加一个安装步骤。对单用户 fork 更差。

**(c) 在 import 时 monkey-patch `kanban_db` 函数 —— 严格更差**
- 插件需要的 hook（`post_tool_call`、`kanban_task_completed`）已经存在且在正确的点触发。
- Monkey-patch `complete_task` / `create_task` 会用更脆弱的方式复制 hook 机制（upstream 改签名时静默失效、更难调试、绕过 `has_hook` 快路径）。**否决**。

**(d) 只对低频改动的文件做 shim —— 不存在**
- 报告 §5.1 表格显示每个被 shim 化的文件都在活跃维护（`kanban_db.py` 刚加 `project_id`；`gateway/run.py` 和 `toolsets.py` 是高频改动）。
- 不存在"低频改动"的 kanban 文件可以保留 shim。这个替代方案是空的。

## 最终建议

**按报告 §6 执行**：把 13 个被修改的核心文件 + 5 个被修改的测试文件全部回滚到 upstream，删除插件的 8 个大型拷贝模块 + dashboard + systemd，只发布 ~250 行的 `__init__.py` + 精简的 `plugin.yaml`。

**执行前对 §6.3 的两点强制修正**：
1. 精简的 `plugin.yaml` **不能含 `provides_tools`**，`register()` **不能调用 `register_kanban_tools`**——否则 9 个 kanban 工具会和 upstream 自动发现的 `tools/kanban_tools.py` 双重注册。
2. 把 `kind: backend` 改成 `kind: standalone`（或确认 upstream 对 hook 插件的自动加载意图）。

§7.3 的风险现已全部解决（SQL ✓、post_tool_call 签名 ✓、工具冲突确认但可修）；唯一开放的产品问题是 §7.2 的"4 个行为是否是完整的工作流需求？"——删除拷贝之前先和用户确认。

---

## 附录 D：报告作者对 Oracle 评审的回应

Oracle 的评审**接受**，所有修正点已纳入实施计划：

1. **双重注册风险**：实施时 `plugin.yaml` 不写 `provides_tools`，`register()` 不调用 `register_kanban_tools`。工具完全交给 upstream 的 `tools/kanban_tools.py`。
2. **`kind: backend` 语义**：实施时改为 `kind: standalone`（若自动加载是必需的，再单独评估）。
3. **§5 冲突面补充**：5 个被改动的测试文件也会一并 `git checkout HEAD --` 回滚。
4. **并行 PR upstream**：作为后续可选路线，不在本次实施范围内。
5. **§7.2 开放问题**：实施前必须由用户确认 4 个 task-loop 行为是否完整覆盖需求。

---

# 附录 E：3 项待验证疑点的补查结果（2026-06-26）

> 作者对 Oracle 评审的回应里列了 3 个"动手前必须验证"的疑点。以下为实际查证结果。

## E.1 疑点 ①：`registry.register()` 对重名的实际行为

**结论：Oracle 严重性判断不准确，但修正建议仍然正确。**

实测 `tools/registry.py` 第 234~290 行的 `register()` 方法逻辑：

```python
with self._lock:
    existing = self._tools.get(name)
    if existing and existing.toolset != toolset:      # ← 关键判断
        # ... MCP / override / reject 分支 ...
        return                                       # ← 仅"不同 toolset"才 reject
    self._tools[name] = ToolEntry(...)               # ← 同名+同 toolset 直接到这里
```

**实际行为分两种情况：**

| 情况 | 行为 |
|---|---|
| 同名 + **同 toolset** | **静默覆盖**（不抛错、不警告、不打 log） |
| 同名 + **不同 toolset** | **REJECTED**（log error，return 不注册），除非 `override=True` 或双方都是 MCP |

**对本场景的影响**：
- upstream `tools/kanban_tools.py` 用 `toolset="kanban"` 注册 9 个工具
- 插件 `register_kanban_tools(ctx)` 也用 `toolset="kanban"` 注册同名 9 个工具
- → **会静默覆盖**，取决于加载顺序：插件版可能赢（用户失去 `project_id`），也可能输（插件代码白跑）
- **不会崩溃**，但会有**隐蔽的行为差异**

**Oracle 的"CONFIRMED RISK"措辞过重**——不是崩溃级风险，是"静默行为不确定"。但修正建议（不写 `provides_tools`、不调用 `register_kanban_tools`）**无论 registry 怎么实现都是对的**，所以方案不受影响。

## E.2 疑点 ②：upstream 纯 hook 插件的 `kind` 先例 —— Oracle 错了

**结论：Oracle 的"`kind: backend` → `kind: standalone`"建议是错的。正确做法是直接删掉 `kind` 字段。**

实测 upstream 的纯 hook 插件先例（grep `plugins/*/plugin.yaml` + 检查 `__init__.py` 是否调用 `register_tool`）：

| 插件 | 注册的工具 | 注册的 hook | `kind` 字段 |
|---|---|---|---|
| `plugins/security-guidance/` | **0** | `transform_tool_result`, `pre_tool_call` | **不写 kind** |
| `plugins/disk-cleanup/` | **0** | `post_tool_call`, `on_session_end` | **不写 kind** |
| `plugins/kanban-workflow/`（当前） | 9（错误地） | `post_tool_call`, `kanban_task_completed` | `backend`（错） |

**upstream 自己的纯 hook 插件两个先例都不写 `kind` 字段**，让 PluginManager 走默认路径。Oracle 的"`kind: standalone`"建议**没有先例支撑**——`standalone` 在 upstream 里被 `teams_pipeline` 用，但 `teams_pipeline` 是另一种形态（无工具也无 hook）。

**正确修正**：精简的 `plugin.yaml` **直接删掉 `kind: backend` 这一行**，不替换成别的 kind。

修正后的 `plugin.yaml` 示例：

```yaml
name: kanban-workflow
version: 1.0.0
description: Task-loop automation hooks for upstream kanban.
provides_hooks:
  - post_tool_call
  - kanban_task_completed
# 注意：不写 kind、不写 provides_tools
```

## E.3 疑点 ③：worker 进程里插件加载与 hook 触发的时序

**结论：时序安全，由 import 链天然保证。Oracle 和报告都没充分说明，但实际无风险。**

实测 worker 启动路径（`origin/main:hermes_cli/kanban_db.py` 第 7349、7492 行）：

```python
"""Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess."""
proc = subprocess.Popen([...])  # 第 7492 行
```

worker 是 `hermes -p <profile> chat -q` 起的子进程。子进程内的加载链：

```
hermes chat -q
  └─ main.py 解析参数
     ├─ main.py L11233/11408/11677: 显式调用 discover_plugins()  ← 第一道保险
     └─ 创建 AIAgent，进入 run_conversation()
        └─ 任何工具调用都经过 model_tools.handle_function_call()
           └─ model_tools.py L201-202: import 时调用 discover_plugins()  ← 第二道保险
              └─ PluginManager.discover_and_load()
                 └─ 插件 register() 运行，注册 kanban_task_completed hook
                    └─ 之后 agent 才可能调用 kanban_complete
                       └─ complete_task() 触发 _fire_kanban_lifecycle_hook()
                          └─ invoke_hook() 派发到已注册的回调
```

**两层保证**：
1. `main.py` 在 CLI 启动时显式调用 `discover_plugins()`（3 个调用点）
2. `model_tools.py` 第 201-202 行作为 import 副作用再次调用（幂等）

任何工具调用（包括 `kanban_complete`）**必须**经过 `model_tools.py`，所以插件**必然**在 hook 可能触发前完成注册。**时序风险不存在**。

**额外安全网**：`_fire_kanban_lifecycle_hook` 的实现（`kanban_db.py` 第 108~128 行）把所有异常吞掉（"a misbehaving observer must never break a board state transition"）——即便插件加载真出问题，kanban 状态转换本身也不会崩。

## E.4 综合修正后的实施清单（替换报告 §6.3）

基于 3 项验证，原 §6.3 的步骤需更新：

| 步骤 | 原方案 | 修正后 |
|---|---|---|
| 步骤 1（撤销核心改动） | 不变 | 13 个源文件 + **5 个测试文件** 全部 `git checkout HEAD --` + 恢复 4 个删除文件 |
| 步骤 2（精简 `__init__.py`） | 保留 258 行 task-loop 逻辑 | 不变，但**删除 `register()` 里的 `register_kanban_tools(ctx)` 调用** |
| 步骤 3（精简 `plugin.yaml`） | `kind: backend` | **直接删掉 `kind` 字段**（不替换成 standalone）；删除 `provides_tools` |
| 步骤 4（删除 8 个大文件 + dashboard + systemd） | 不变 | 不变 |
| 步骤 5（重写测试） | 删 8 个 shim 路径测试，保留 2 个行为测试 | 不变，**但应补 2 个新测试**覆盖 `_auto_unblock_children` 和 `_auto_complete_parent`（当前 4 个行为只有 2 个有测试） |
| 步骤 6（验证） | 跑插件测试 + kanban 测试套件 | 不变 |

## E.5 评审者之间的分歧记录

| 议题 | Oracle 立场 | 作者立场（基于实测） | 谁对 |
|---|---|---|---|
| 双重注册严重性 | CONFIRMED RISK | 静默覆盖（非崩溃），但建议仍对 | **作者**（Oracle 没查 registry 实现） |
| `kind` 字段 | 改成 `standalone` | **删掉** | **作者**（Oracle 没看 upstream 先例） |
| worker 加载时序 | 未提及 | 安全（import 链保证） | **作者补充** |
| 测试覆盖 | 未提及 | 4 个行为只有 2 个有测试 | **作者补充** |
| 核心方案（极简插件） | AGREE | AGREE | **双方一致** |

**结论：Oracle 的总体方向正确，但 2 个具体修正建议需要按实测结果调整。最终方案以附录 E.4 为准。**
