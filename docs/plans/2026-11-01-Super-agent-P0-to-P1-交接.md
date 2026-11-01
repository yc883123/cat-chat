# Super-agent V1：P0 → P1 执行交接

- 日期：2026-11-01
- 当前阶段：**P0 已完成并提交；P1 尚未开始**
- 目标：新对话直接从 P1 Adapter / 确定性 Router 开始，不重新设计 P0，不先改前端。
- 相关文档：
  - [V1 执行交接](./2026-11-01-Super-agent-V1-执行交接.md)
  - [P0 验收记录](./2026-11-01-Super-agent-P0-验收记录.md)
  - [V1 总计划](./2026-11-01-Super-agent工作台总计划.md)

## 一、当前代码状态

P0 已落地在以下范围：

```text
naiba/super_agent/__init__.py
naiba/super_agent/models.py
naiba/super_agent/store.py
naiba/super_agent/service.py
```

已修改：

```text
naiba/app.py
naiba/storage/store.py
tests/test_storage_migration.py
```

新增测试：

```text
tests/test_super_agent_p0.py
tests/test_super_agent_p0_boundaries.py
```

P0 使用现有 `chat.db`，SQLite schema 为 v29，新增独立任务层表：

```text
super_threads
super_nodes
super_events
super_artifacts
super_approvals
```

任务总管 Thread 没有复用 `background_tasks`。现有 Chat Run、Job、Studio、Novel、Writer、ComfyUI 流程保持原有边界。

## 二、P0 已冻结的契约

### Thread

- owner 隔离：所有读取、控制和关联都必须通过 `owner_id`。
- 状态：`draft`、`active`、`waiting_user`、`paused`、`cancelled`、`completed`、`failed`、`needs_recovery`、`archived`。
- Thread 创建时冻结 `execution_snapshot`：
  - Agent
  - Provider
  - Model
  - workspace
  - permission mode
  - enabled tools
  - Skill policy
  - vision capabilities
  - cost policy
- Thread/Node 创建后不得修改 execution snapshot。

### Task Node

- 状态包含：`pending`、`ready`、`running`、`stopping`、`waiting_user`、`succeeded`、`failed`、`cancelled`、`blocked`、`interrupted`。
- Node 依赖只能引用同一 Thread 中已存在的 Node。
- Node 创建必须带稳定 `idempotency_key`。
- Worker 事件必须带稳定 `idempotency_key` 或 `source_event_id`。
- `attempt` 用于晚到结果 fencing；旧 attempt 不能写回新 attempt。

### Event

- 任务事件和现有 `run_events` 分开；P0 任务事件保存在 `super_events`。
- 有 Node 的事件入库、attempt 校验、节点推进、依赖解锁、Thread 收口在同一事务内完成。
- 事件重复到达不能重复推进或重复副作用。
- 暂停期间无法应用的 Node 事件保持未 handled，resume 时重放。
- Event、Artifact、Approval 的 Node/Thread 关系和 owner 必须校验。

### Approval

- Approval 创建同时写入 Approval、Node waiting_user、Thread waiting_user 和 approval_requested 事件。
- Approval 解决同时写入 Approval、Node、Thread 和 approval_received 事件。
- `pending → approved/rejected/cancelled` 只能解析一次；同状态重复请求幂等，不允许改成另一终态。
- 控制动作必须携带请求幂等键：pause/resume/cancel/Approval resolve 使用 `request_id`。

### Cancel / Pause / Recovery

- 取消：`running → stopping → cancelled`。
- 假 Worker 收到取消后确认停止；晚到完成事件只保留审计，不写回节点。
- 暂停期间完成事件不标记 handled；继续时重放并推进。
- App 启动会调用 Super-agent recovery：
  - `running/stopping` Node → `interrupted`
  - 活跃 Thread → `needs_recovery`
  - `waiting_user` 与 `paused` 保持
  - 已完成/已取消不重复执行
- recovery 幂等，不自动重复计费、执行或写回。

## 三、P0 验证结果

已通过：

```text
P0 主路径 + 边界测试：12/12
P0 + storage migration + Run/Job 核心回归：110/110
P0 + migration 最终验收子集：31/31
compileall：通过
git diff --check：通过
```

P0 专项文件：

- `tests/test_super_agent_p0.py`
- `tests/test_super_agent_p0_boundaries.py`

## 四、P1 只做什么

P1 进入 **Adapter 和确定性 Router**，不要先做 HTTP、UI 或真实供应商闭环。

### P1 最小范围

1. 定义统一 Adapter 协议/基类，至少包含：
   - `executor_type`
   - `create/start`
   - `status`
   - `cancel`
   - `retry/resume` 能力声明
   - `checkpoint`
   - `attempt`
   - `output_refs/artifacts`
   - owner/thread/node 校验边界
2. 先实现：
   - `FakeAdapter`：把现有 FakeWorker 适配到 Adapter 契约。
   - `JobAdapter`：只包装已有 `JobRegistry.start/get/cancel/resume/retry`。
3. 再预留/实现：
   - `ChatRunAdapter`：调用已有 `ConversationRunManager.submit_chat/cancel`，不复制 Chat 状态机。
   - Writer/Novel Adapter 的只读接口或明确的 deferred 能力；不要把领域服务强行改成通用 Job。
4. 实现 Fast Path Router：
   - 已知 `thread_id + node_id + executor_id + source_event_id` 时直接路由。
   - 事件推进、状态刷新、暂停、继续、取消、重试不调用 LLM。
   - 只做确定性映射、幂等判定、attempt fencing 和 Adapter 调用。
5. 增加 Adapter/Router 测试：
   - owner 隔离
   - 重复 start 不重复创建底层 Job
   - cancel 不再写回
   - retry 使用新 attempt
   - source event 重复不重复副作用
   - Fast Path 不调用模型
   - ComfyUI 确定性节点不绕 Agent（P1 只留接口/测试，不做造梦机深度接入）

## 五、P1 明确不要做

- 不修改五个工作台页面和 `public/`。
- 不新增任务总管前端入口。
- 不新增 Super-agent HTTP API，除非 P1 适配层稳定并另行确认。
- 不自动监听普通对话或所有工作台事件。
- 不把所有旧 Run/Job 自动导入 Thread。
- 不实现 Slow Path Router 或自然语言任务拆解。
- 不调用真实供应商，不做付费生成。
- 不重写 `JobRegistry`、`ConversationRunManager`、Writer、Novel、Studio 的既有状态机。
- 不用 Adapter 绕过 P0 owner、snapshot、request id、idempotency、attempt fencing 约束。

## 六、新对话开工步骤

新对话第一步：

1. 读取本文件、P0 验收记录和 V1 总计划。
2. `git status`、`git log -1`，确认当前已在 P0 提交之后。
3. 只读核对 `naiba/super_agent/service.py`、`store.py`、`models.py` 与 `naiba/jobs.py`、`naiba/run/manager.py` 的接口。
4. 先提交 P1 最小文件清单和测试清单，再开始写代码。
5. P1 第一切片只实现 `FakeAdapter + JobAdapter + AdapterRegistry/FastPathRouter` 及测试。
6. 每个切片先跑专项测试，再跑 P0 回归；不得让旧 Chat/Job/Studio 测试退化。

可直接粘贴的新对话开工指令：

> 读取 `docs/plans/2026-11-01-Super-agent-P0-to-P1-交接.md`、`docs/plans/2026-11-01-Super-agent-P0-验收记录.md` 和 `docs/plans/2026-11-01-Super-agent-V1-执行交接.md`，从当前 P0 提交之后开始 P1。先只读核对 P0 服务/存储契约和现有 JobRegistry、ConversationRunManager 接口，然后提出最小文件清单；第一切片只实现 FakeAdapter、JobAdapter、AdapterRegistry/FastPathRouter 与专项测试。不修改五个工作台页面、不新增 HTTP、不调用真实供应商、不进入 Slow Path LLM Router。

## 七、交付边界

本交接只代表 P0 已完成并交付到代码仓库。P1 仍需单独设计、实现、测试和提交。任何 Adapter 都必须复用 P0 的持久化契约，不得另建一套 Thread、Approval 或 Event 数据库。
