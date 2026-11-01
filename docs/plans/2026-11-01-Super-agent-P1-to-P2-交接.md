# Super-agent V1：P1 → P2 执行交接

- 日期：2026-11-01
- 当前阶段：**P1 第一切片已实施并提交；P2 前端与 HTTP 仍未开始**
- 当前分支：`feat/super-agent-p0-to-p1-handoff`
- P0 提交：`ba1d122` `feat(super-agent): complete P0 contracts and handoff`
- 本交接目的：新对话直接从 P2 任务总管前端开始，不重新设计 P0/P1，不扩展旧执行器。

## 一、当前提交与代码状态

本次 P1 交付应包含以下文件：

```text
naiba/super_agent/adapters.py
naiba/super_agent/router.py
naiba/super_agent/service.py
naiba/super_agent/store.py
tests/test_super_agent_p0.py
tests/test_super_agent_p1_adapters.py
docs/plans/2026-11-01-Super-agent-P1-to-P2-交接.md
```

P1 新增的核心对象：

- `AdapterCapabilities`
- `AdapterResult`
- `BaseAdapter`
- `FakeAdapter`
- `JobAdapter`
- `AdapterRegistry`
- `FastPathRequest`
- `FastPathRouter`

P1 没有修改：

- `public/`
- 五个工作台页面
- `naiba/jobs.py`
- `naiba/run/manager.py`
- `naiba/storage/store.py`
- HTTP 路由
- 真实供应商和付费 Worker
- Slow Path LLM Router

工作区中还可能存在其他用户原有的未跟踪文件；不要将它们带入 P2 或后续提交。

## 二、P0/P1 已冻结契约

### 2.1 任务层持久化

P0 继续使用现有 `chat.db` 的任务层表：

```text
super_threads
super_nodes
super_events
super_artifacts
super_approvals
```

P2 不得另建 Thread、Node、Event、Artifact 或 Approval 数据库。

所有任务查询和控制必须经过 `owner_id`。Thread 创建时冻结 execution snapshot：Agent、Provider、Model、workspace、permission mode、工具、Skill、视觉能力和费用策略。

### 2.2 Node / Event 状态

Node 状态仍由 P0 Service/Store 控制，不由前端自行推断或直接写库：

```text
pending / ready / running / stopping / waiting_user /
succeeded / failed / cancelled / blocked / interrupted
```

Thread 状态：

```text
draft / active / waiting_user / paused / cancelled /
completed / failed / needs_recovery / archived
```

`job_failed` 应用成功后：

```text
Node   -> failed
Thread -> failed
```

重试通过 P0 `retry_node()`：

```text
failed/cancelled -> ready
attempt + 1
Thread -> active
```

旧 attempt 的晚到事件只保留审计，不得写回新 attempt。

### 2.3 Adapter / Fast Path

- `FakeAdapter` 只委托 P0 `SuperAgentService` / `FakeWorker`。
- `JobAdapter` 只包装现有 `JobRegistry`，底层 Job ID 与 P0 Node attempt 不等价。
- `AdapterRegistry` 只按显式 `executor_type` 查找，未知类型不得 fallback 到 Chat 或模型。
- `FastPathRouter` 只做结构化校验、确定性路由、幂等和 attempt fencing，不调用 LLM。
- `status` 不使用 Router 的一次性动作缓存，必须读取最新执行器状态。
- start/cancel/retry/resume 的稳定请求键可在 Router 内存中短路，但不能把内存缓存当唯一幂等来源。
- Adapter 控制动作在 Node checkpoint 的 `_adapter_commands` 中保留持久化结果，重启后仍能识别已处理的 request。
- `output_refs` 是执行器输出；`artifact_refs` 是已登记在 `super_artifacts` 中、属于当前 Node 的 `path_or_ref`。

## 三、P1 验证结果

已通过：

```text
P1 Adapter/Router 专项：13/13
P0 + P0 边界 + storage migration：44/44
Job/Run 相关回归：97/97
compileall：通过
git diff --check：通过
```

当前环境没有 `pytest`，验证使用仓库已有 `unittest` 入口。

## 四、P2 目标：让任务总管可见、可用

P2 进入任务总管的最小前端和 HTTP 薄接口。第一目标不是接入五个工作台的深度流程，而是让用户能够看到并控制 P0/P1 已有任务。

### 4.1 顶层应用入口

在现有应用视图切换中增加：

```text
appView = chat | studio | super-agent
```

任务总管是与「对话」「工作台」同级的可选应用入口，不是 Agent 下拉项、普通聊天窗口或工作台分页。

不使用任务总管的用户必须继续按原有对话和工作台流程使用，打开旧页面不得自动创建 Thread。

### 4.2 P2 最小可见能力

任务总管页面至少提供：

- Thread 列表；
- Thread 状态、标题、目标和当前节点；
- Node 列表、依赖、状态、attempt、executor type；
- 事件时间线摘要；
- Approval 待确认项；
- Artifact 产物引用；
- 任务详情中的暂停、继续、取消、重试操作；
- 创建任务；
- 从对话/工作台显式关联任务的最小入口可以后置，但不得自动监听所有操作。

任务总管展示摘要和引用，不复制 Writer 正文、Novel 全文、ComfyUI 工作流编辑器、完整提示词库或内部推理日志。

### 4.3 P2 API 最小范围

P2 可以新增 Super-agent 专用 HTTP API，但只做薄控制层，统一委托 `SuperAgentService` / `FastPathRouter`：

```text
GET  /api/super-agent/threads
POST /api/super-agent/threads
GET  /api/super-agent/threads/<thread_id>
GET  /api/super-agent/threads/<thread_id>/nodes
GET  /api/super-agent/threads/<thread_id>/events
GET  /api/super-agent/threads/<thread_id>/artifacts
GET  /api/super-agent/threads/<thread_id>/approvals

POST /api/super-agent/threads/<thread_id>/pause
POST /api/super-agent/threads/<thread_id>/resume
POST /api/super-agent/threads/<thread_id>/cancel
POST /api/super-agent/nodes/<node_id>/retry
POST /api/super-agent/nodes/<node_id>/start
POST /api/super-agent/nodes/<node_id>/sync
```

实际路径应先搜索仓库既有 API 命名、鉴权和错误响应约定后再决定；上面是能力清单，不是要求照抄的最终 URL。

HTTP 层必须：

- 从现有会话/认证上下文取得 owner，不信任客户端任意传入 owner；
- 对 Thread/Node 统一做 owner 隔离；
- 控制请求要求稳定 `request_id` / 幂等键；
- 不直接操作 SQLite；
- 不把 `JobRegistry` 或 `ConversationRunManager` 暴露成任务总管 API；
- 不因刷新/轮询调用 LLM；
- 返回可供前端展示的结构化错误，但不泄漏 API Key、内部推理或完整长日志。

### 4.4 前端结构边界

开始实现前先只读核对：

- 现有 `appView` / chat-studio 切换方式；
- `public/index.html` 的顶层布局和入口；
- 现有公共状态、fetch 封装、错误提示、移动端布局；
- `#tasksDialog` 和现有后台任务展示，避免重复建设同一信息层；
- 现有五个工作台的入口边界。

建议 P2 新增独立模块文件和独立样式，尽量不要把任务总管逻辑散落进五个工作台。任务总管不应复用普通聊天区作为主布局。

桌面端目标：

```text
左：Thread 列表
中：当前 Thread / Node 阶段与时间线
右：Approval、Artifact、操作区
```

手机端目标：

```text
单列：Thread → 详情 → 时间线 → Approval / Artifact / 操作
```

### 4.5 轮询口径

P2 第一版可使用确定性轮询或显式刷新：

- 只读取任务状态、事件、Approval、Artifact；
- 不因轮询调用 Router 模型；
- 不自动把普通 Chat Run、独立 Job 或工作台操作导入 Thread；
- 轮询失败必须显示可恢复错误，不清空当前已显示内容；
- 任务控制成功后重新拉取真实状态，不靠前端乐观状态冒充后端结果。

## 五、P2 明确不做

- 不接 Writer / Novel 深度闭环；
- 不修改五个工作台页面以接入完整任务总管流程；如确需入口，先做单一轻量入口并另行确认；
- 不实现 ChatRunAdapter、WriterAdapter、NovelAdapter；
- 不实现 Slow Path LLM Router、自然语言任务拆解或自主循环；
- 不接真实供应商、付费生成、ComfyUI 深度执行；
- 不自动迁移旧 Run/Job；
- 不监听所有对话和工作台事件；
- 不复制模块全文、完整日志或内部推理；
- 不新增第二套权限、owner 或成本策略；
- 不直接写任务数据库；
- 不提交或推送未核验的 UI 试验文件。

## 六、新对话开工指令

新对话第一步：

1. 读取本文、P0 验收记录、P0→P1 交接和 V1 执行交接；
2. 执行 `git status`、`git log -1`，确认 P1 提交已存在且工作区原有未跟踪文件未被带入；
3. 只读核对现有 HTTP 路由、认证/owner 获取、`appView` 视图切换、`public/index.html`、公共 fetch/错误处理和 `#tasksDialog`；
4. 提出 P2 最小文件清单和 API/UI 测试清单，先不要写代码；
5. P2 第一切片只实现：任务总管入口、Thread 列表/详情只读展示、最小 HTTP 读取接口和专项测试；
6. 第二切片再加入控制动作和创建任务；
7. 不修改五个工作台页面，不接真实供应商，不进入 Slow Path；
8. 每个切片先跑专项测试，再跑 P0/P1 和旧 Chat/Job/Run 回归。

可直接粘贴的新对话开工指令：

> 读取 `docs/plans/2026-11-01-Super-agent-P1-to-P2-交接.md`、`docs/plans/2026-11-01-Super-agent-P0-to-P1-交接.md`、`docs/plans/2026-11-01-Super-agent-P0-验收记录.md` 和 `docs/plans/2026-11-01-Super-agent-V1-执行交接.md`，从已提交的 P1 开始执行 P2。先只读核对现有 HTTP 路由/owner 认证、appView、public/index.html、公共 fetch 封装和 #tasksDialog，提出 P2 最小文件清单；第一切片只做任务总管入口、Thread 列表/详情只读展示、最小读取 API 与专项测试。不修改五个工作台页面、不接真实供应商、不进入 Slow Path LLM Router。

## 七、P2 验收门槛

第一切片至少通过：

- 不同 owner 不能读取或控制彼此 Thread/Node；
- Thread/Node/Event/Artifact/Approval 只读页面数据与 P0 Service 一致；
- 页面刷新不会创建 Thread、Node 或调用 LLM；
- 任务总管入口不影响 Chat/Studio 原流程；
- 移动端不出现横向溢出；
- 事件、Approval、Artifact 展示只显示摘要和引用；
- HTTP 错误不泄漏密钥或内部推理；
- P0/P1 专项和既有 Chat/Job/Run 回归不退化。

## 八、停止条件

遇到以下情况先停，不要扩大 P2：

1. 为显示任务状态必须复制旧模块数据库；
2. owner 隔离无法从现有认证上下文可靠取得；
3. 页面刷新需要调用 LLM；
4. 普通 Chat/Studio 流程被迫创建 Thread；
5. 控制动作没有稳定 request_id 或无法证明幂等；
6. 为接入 UI 必须重写 JobRegistry、ConversationRunManager 或 P0 状态机；
7. 任务总管开始展示完整正文、内部推理或全部旧日志；
8. 需要接入真实供应商才能完成 P2 只读验收。

## 九、当前交付判定

P1 第一切片的反馈边界已补齐，P2 可以从本交接开始设计和实施。当前 P2 仍未开始；本交接不代表 P2 已有 HTTP 或页面实现。
