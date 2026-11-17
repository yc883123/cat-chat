# Super-agent 工作台总计划

> 状态：**只写计划，未修改源码、未跑测试、未构建、未提交**。
> 日期：2026-11-01
> 规划基线：HEAD `8a53691`
> 内部标识：`super_agent`
> 首版建议显示名：**任务总管**（副标题或帮助说明保留「Super-agent」）
> 参考材料：`D:\naiba-chatexe\workspace\Super-agent设计探索_完整提取\Super-agent设计探索_完整文章.md`
> 关联现状：对话、工作台、异步任务、造梦机、拆书匠、织书台、上下文压缩和后台 Job 已存在；本计划不以重写这些系统为前提。

---

## 一、一页结论

### 1.1 Super-agent 在 Cat Chat 中是什么

Cat Chat 的 Super-agent 不是一个更强的普通 Agent，也不是把一个聊天 Session 无限延长，而是一个位于现有 Chat Run、后台 Job 和各个工作台模块之上的**持续任务协调层**。

它负责把一个长期目标拆成可以追踪的任务节点，并统一协调：

- 普通对话 Run；
- 后台 Job 和子 Agent；
- 织书台流水线；
- 拆书匠阶段任务；
- Studio 任务；
- 造梦机 / ComfyUI 任务；
- 后续的定时、文件和外部消息事件。

用户不再需要记住：

- 这件事在哪个 Session；
- 哪个子 Agent 正在工作；
- 结果属于哪个 Job；
- 工作台结果如何交接回对话；
- 重启后应该从哪里继续。

用户只需要看一个任务：

```text
目标是什么 → 做到哪一步 → 下一步是什么 → 是否需要我确认 → 产物在哪里
```

### 1.2 入口的最终决策

**Super-agent 做成与「对话」「工作台」同级的第三个应用级入口。**

推荐的用户可见导航：

```text
对话    工作台    任务总管
                  Super-agent
```

推荐显示方式：

- 主标签：`任务总管`；
- 帮助说明或空状态副标题：`Super-agent：统一推进跨对话、工作台和后台任务的长期目标`；
- 内部 key：`super_agent`。

它**不放在**以下位置：

1. **不放进对话顶栏的 Agent 下拉**：那里表达的是“当前这段对话由哪个 Agent 角色回答”，而 Super-agent 表达的是“跨会话持续推进一个任务”。
2. **不作为工作台普通分页**：工作台分页是提词库、打标器、造梦机、拆书匠、织书台等具体生产工具，Super-agent 是跨模块的协调中心。
3. **不只作为现有异步任务弹窗**：当前任务弹窗适合查看零散后台任务，不能承载目标、阶段、审批、产物和长期任务记忆。

### 1.3 三个版本的目标

| 版本 | 产品目标 | 关键能力 |
|---|---|---|
| V1：任务总管 | 把已有执行能力挂到一个持续任务上 | Thread、Task Node、统一事件、任务中心、人工确认、基础恢复 |
| V2：主动协作者 | 让任务可以被环境事件主动推动，并协调一条完整的小说改编生产链 | Trigger、订阅、任务图、项目资产库、长期记忆、Watchdog、有限自动推进 |
| V3：跨信道 Super-agent | 让同一任务跨入口、跨设备和执行环境持续运行 | QQ/GitHub/Webhook、远程执行、计算与环境分离、多用户、A2A |

### 1.4 首个垂直场景

首版优先落地：

> **任务总管 + 织书台 / 拆书匠 + Writer/Novel Worker + 任务级审批 + 统一任务详情页。**

原因：Writer 和 Novel 已有阶段化流水线、版本冲突保护、逐步写回和任务恢复边界，最适合验证 Super-agent 的核心价值。第二个接入造梦机现有 ComfyUI Job 与批量 API，由任务总管关联、协调和回链，不重做批量生成；第三个接入普通 Chat Run、后台 Job 和子 Agent。

---

## 二、现状与设计依据

### 2.1 当前已经有的执行基础

| 能力 | 当前实现 | 计划中的处理 |
|---|---|---|
| 普通 Chat Run | `naiba/run/manager.py`、`naiba/run/chat.py` | 作为一种 Worker 接入，不重写 |
| 后台 Job | `naiba/jobs.py`，支持父子关系、取消、checkpoint、恢复、重试 | 作为通用 Job Worker 接入 |
| 事件基础设施 | `naiba/events.py` 的 EventBus 和 `run_events` | 在其上增加任务层 Agent Event，不破坏 Run 事件 |
| 子 Agent | `naiba/subagent.py`，支持 fork/spawn 和深度限制 | V1 作为节点执行器；V2 再考虑结构化协作 |
| 织书台 | `naiba/writer/pipeline.py` | 首个领域 Worker |
| 拆书匠 | `naiba/novel/service.py` | 首个领域 Worker |
| Studio | `naiba/studio/service.py`、`naiba/studio/runner.py` | V1 先关联，后续统一适配 |
| 造梦机 | ComfyUI 工作台、现有 ComfyUI Job 和独立批量 API；批量执行、槽位映射、素材上传、逐条重试和产物预览由造梦机负责 | V1 预留 Job 关联；V2 通过 Adapter 调用或观察现有任务/批次 API，关联批次与逐条任务、收集状态和产物，不复制批量逻辑；真实 ComfyUI 端到端验收另行完成 |
| 上下文压缩 | `naiba/core/compaction.py` | 复用当前 Conversation 压缩；新增 Thread 摘要 |
| 任务恢复 | Job 和各工作台已有不同程度的恢复 / interrupted / retry | V1 先统一投影，不强行把所有任务改成可续跑 |
| 工作台交接 | `naiba/studio/handoff.py` | 改为可关联 Thread 的显式交接，不取消现有交接 |

### 2.2 当前前端结构

当前前端是单份 `public/index.html`，没有传统 SPA URL 路由，主要通过 DOM、状态和 `body` data 属性切换视图：

- 应用级「对话 / 工作台」入口：`public/index.html:30-46`；
- 工作台分页：`public/index.html:73-98`；
- 工作台切换与分页：`public/js/22-studio.js:261-397`；
- 对话顶栏 Agent 选择：`public/index.html:116-129`；
- 对话顶栏任务入口：`public/index.html:160-185`；
- 异步任务弹窗：`public/index.html:3137-3147`。

这套结构天然支持增加第三个应用级视图，但不适合把 Super-agent 塞入对话顶栏或工作台分页。

### 2.3 当前最重要的架构缺口

当前执行层已经比较丰富，缺少的是统一编排层：

```text
现状：Conversation + Run + Job + Studio/Novel/Writer 各自推进
目标：Thread + Task Graph + Router 统一关联已有执行器
```

主要缺口：

1. 没有一等的持续任务 Thread；
2. 没有统一的任务节点和依赖关系；
3. `EventBus` 主要负责写入事件和唤醒等待者，还不是订阅与路由系统；
4. 工作台与 Chat 的交接主要是一次性文本快照；
5. 没有统一的任务级审批、产物引用和长期摘要；
6. Watchdog 分散在 Run、Job、LLM、MCP 和前端流中；
7. 没有跨信道入口和任务映射。

---

## 三、产品边界与核心概念

### 3.1 概念关系

```text
Agent
  = 一个角色配置：提示词、Skill、工具范围和模型偏好

Conversation
  = 用户看到的对话入口

Thread
  = 一个持续任务目标，跨越多个 Conversation、Run、Job 和工作台

Run
  = 一次模型推理执行

Job
  = 一个可后台运行、可取消、可等待或可恢复的执行单元

Task Node
  = Thread 中的一个阶段或动作

Worker
  = 实际执行 Run、Job、Writer、Novel、Studio 或 ComfyUI 的适配器

Router
  = 只负责任务识别、路由、推进、汇总和请求确认的协调器

Artifact
  = 任务产生的文件、文本、媒体、版本或报告
```

### 3.2 Router 与 Worker 的职责边界

Router 不亲自完成具体工作，不通过提示词要求自己“尽量调用子 Agent”，而是由系统层固定职责：

Router 负责：

- 识别新消息属于哪个 Thread；
- 读取 Thread 摘要和当前节点；
- 根据事件推进任务图；
- 创建、暂停、取消、重试任务节点；
- 汇总 Worker 状态；
- 生成短通知；
- 提出人工确认。

Worker 负责：

- 调用模型；
- 使用工具；
- 执行脚本、HTTP、ComfyUI 和工作台流水线；
- 产生实际文件和媒体；
- 按权限快照执行有副作用的操作；
- 回报结构化结果。

Router 默认不拥有：

- Shell；
- 任意文件写入；
- 删除和覆盖；
- 对外发送；
- 付费生成；
- 动态扩大 Worker 权限。

### 3.3 路由的两条路径

#### Fast Path：确定性路由

以下信息足够确定归属时，不调用模型：

- `thread_id`；
- `node_id`；
- `executor_id`；
- `source_event_id`；
- 用户在任务页面点击的审批、继续、取消、重试动作。

例如：Writer 节点完成后，直接更新对应节点并检查下一节点，不再让 Router 重新猜测。

#### Slow Path：语义路由

只有以下情况才调用轻量 Router 模型：

- 新消息没有明确 Thread；
- 用户同时提到多个任务；
- 用户说“继续刚才那个”但有多个候选；
- 用户改变了原任务目标；
- 需要判断是补充要求、暂停、取消还是新任务。

---

## 四、Super-agent 前端入口与信息架构

### 4.1 入口方案对比

| 方案 | 结论 | 原因 |
|---|---|---|
| 放进对话顶栏 Agent 下拉 | 不采用 | 混淆“当前对话角色”和“跨模块任务总管”；顶栏已有文件、辅助、任务、Skill、MCP 等操作 |
| 放进工作台分页 | 不采用 | 会被理解成一个新的内容生产工具；任务总管实际上要统筹整个工作台和普通对话 |
| 继续放在异步任务弹窗 | 仅保留快捷入口 | 弹窗只能查看任务，无法承担目标、阶段、审批、产物和长期任务视图 |
| 与对话、工作台同级的应用入口 | **采用** | 符合现有应用级导航；能拥有独立主区、侧栏、状态和任务详情 |

### 4.2 推荐的顶层布局

```text
┌─────────────────────────────────────────────────────────────┐
│  对话             工作台             任务总管               │
├───────────────┬─────────────────────────────┬───────────────┤
│ 任务列表       │ 当前任务                    │ 任务详情       │
│               │                             │               │
│ + 新建任务     │ 《某某小说》改编任务        │ 当前阶段       │
│               │ 状态：等待确认              │ 待确认事项     │
│ 进行中         │ 总目标                      │ 最近事件       │
│ 等待确认       │ 阶段进度                    │ 关联对话       │
│ 已完成         │ 任务节点 / 时间线           │ 关联工作台     │
│ 失败           │                             │ 产物           │
└───────────────┴─────────────────────────────┴───────────────┘
```

### 4.3 左侧：任务列表

任务总管进入后，左侧不再显示对话树，而显示任务列表：

- 新建任务；
- 进行中；
- 等待确认；
- 已暂停；
- 已完成；
- 失败 / 需要处理；
- 已归档。

每个任务项只显示用户需要的摘要：

```text
任务名称
当前状态
当前阶段
最后更新时间
是否需要确认
```

不在列表中暴露：

- Worker 的内部 Session ID；
- 复杂工具调用日志；
- 大段推理过程；
- 子 Agent 的内部提示词。

### 4.4 中间：任务总览

任务总览不是另一个普通聊天窗口，而是面向目标的工作面板，包含：

1. 任务名称和总体目标；
2. 当前状态；
3. 进度概览；
4. 当前阶段；
5. 阶段时间线；
6. 已完成节点、运行节点和阻塞节点；
7. 最近一次系统决策；
8. 用户可输入的任务指令。

用户输入框的文案建议：

- 新建任务时：`告诉我你想完成什么`；
- 已有任务时：`补充要求、修改目标，或告诉我下一步怎么做`。

它不是用来展示所有模型过程的聊天框。需要详细对话时，通过关联 Conversation 进入普通对话。

### 4.5 右侧：确认、产物和关联关系

右侧按优先级显示：

1. **待确认事项**：必须先处理的问题；
2. **下一步**：系统准备执行的动作；
3. **最近产物**：文本、图片、视频、版本和报告；
4. **关联对话**：打开普通聊天继续交流；
5. **关联工作台**：跳转到织书台、拆书匠、造梦机等具体模块。

同一信息只保留一层：任务中心展示摘要，完整内容进入对应详情或工作台，不在任务页面重复铺开整篇正文。

### 4.6 与现有「对话」的关系

对话仍然是自由交流和直接使用 Agent 的地方，不被 Super-agent 取代。

在对话中保留以下轻量入口：

- 新建任务；
- 将当前对话加入已有任务；
- 从当前消息创建任务；
- 查看关联任务；
- 跳转到任务总管。

现有 `#openTasks` 异步任务按钮和 `#activeTaskBar` 继续保留，但调整为快捷查看：

```text
当前对话任务 → 查看任务总管
```

不能把普通 Chat 的所有消息自动变成 Super-agent 任务，否则会污染任务列表。V1 采用显式创建或明确识别：

- 用户点击“创建任务”；或
- 用户明确提出跨阶段、长时间、多步骤目标；或
- 工作台操作明确选择“纳入任务总管”。

### 4.7 与现有「工作台」的关系

工作台继续承载领域工具：

```text
工作台
├─ 提词库
├─ 打标器
├─ 造梦机
├─ 拆书匠
└─ 织书台
```

每个领域页面增加轻量关联能力：

- `创建任务`：以当前作品 / 工作流 / 输入创建 Thread；
- `关联任务`：将当前操作挂到已有 Thread；
- `查看任务`：跳转任务总管；
- `返回产物`：从任务总管回到具体领域页面。

Super-agent 不复制工作台的编辑器和生成器。工作台负责“怎么做”，任务总管负责“为了什么、做到哪一步、下一步是什么”。

### 4.8 移动端设计

桌面端采用三栏，移动端不强行保留三栏：

```text
任务列表 → 任务详情 → 事件 / 产物 / 待确认
```

移动端优先显示：

- 当前状态；
- 是否需要确认；
- 下一步；
- 最近产物；
- 暂停、继续、取消按钮。

模型推理过程、完整日志和复杂任务图默认折叠。

### 4.9 前端实现边界

V1 沿用当前单页 DOM 视图切换，不引入新的 SPA 路由框架。

建议新增：

```text
#openSuperAgent       顶层入口
#superAgentView       主视图
#sidebarSuperAgent    任务列表侧栏
body[data-app-view]   chat / studio / super-agent
state.appView         当前应用级视图
state.superAgent      当前任务总管状态
```

不要同时维护三个互相独立的布尔值：

```text
不要让 data-studio、data-super-agent、data-chat 同时自由切换
```

统一使用一个应用视图状态：

```text
appView = "chat" | "studio" | "super-agent"
```

现有 `body[data-studio="on"]` 在迁移期保留兼容，但新的切换逻辑以 `data-app-view` 为准。现有 `settings.studio.view` 继续保存工作台分页；应用级最后视图新增 `settings.app.view`，从旧偏好平滑迁移。

---

## 五、V1：任务总管

### 5.1 V1 目标

V1 只做本地单用户、网页内的持续任务编排：

- 一个任务跨多个 Conversation、Run、Job 和工作台阶段；
- Worker 执行期间用户可以继续发消息；
- Worker 完成后能自动推进安全的下一节点；
- 需要判断时暂停并明确请求用户确认；
- 应用重启后状态不丢失；
- 用户从任务总管查看进度、产物和关联入口。

### 5.2 V1 必须支持

- Chat Run；
- 通用后台 Job；
- 子 Agent；
- Writer Pipeline；
- Novel 阶段任务；
- Thread 创建、查看、暂停、继续、取消、重试；
- Task Node 状态和依赖；
- 任务级事件时间线；
- 任务级人工确认；
- Artifact 引用；
- 基础重启恢复。

### 5.3 V1 暂不支持

- QQ、微信、GitHub、邮件和 Webhook；
- 云端常驻；
- 多用户 / 多租户；
- 向量数据库和复杂语义记忆；
- Agent 之间自由通信；
- 任意任务无限自主运行；
- 远程设备执行；
- 自动对外发送或发布。

### 5.4 V1 数据模型

#### Thread

```text
thread_id
title
goal
status
workspace_dir
router_agent_id
autonomy_level
summary
current_node_id
linked_conversation_ids
linked_run_ids
linked_job_ids
linked_domain_task_ids
last_event_at
created_at
updated_at
```

Thread 状态：

```text
draft
active
waiting_user
paused
completed
failed
archived
```

#### Task Node

```text
node_id
thread_id
parent_id
kind
status
dependencies
executor_type
executor_id
input_refs
output_refs
approval_required
retry_policy
checkpoint
attempt
created_at
updated_at
```

节点状态：

```text
pending
ready
running
waiting_user
succeeded
failed
cancelled
blocked
```

`executor_type` 首批支持：

```text
chat_run
job
subagent
writer_pipeline
novel_stage
studio_task
comfyui_task
```

#### Agent Event

`run_events` 继续保存 Run 内部事件；新增任务层事件用于推进 Thread：

```text
event_id
thread_id
node_id
source
event_type
payload
source_event_id
idempotency_key
created_at
handled_at
```

首批事件：

```text
user_message
run_started
run_completed
job_completed
job_failed
writer_stage_completed
novel_stage_completed
artifact_created
approval_requested
approval_received
task_cancelled
app_restarted
```

#### Artifact

```text
artifact_id
thread_id
node_id
kind
path_or_ref
title
summary
version
created_at
```

正文、长日志和媒体文件不直接塞进 Router 上下文，只在任务页展示摘要和引用。

#### Approval

```text
approval_id
thread_id
node_id
action
description
risk_level
status
requested_at
resolved_at
resolved_by
```

适用动作：覆盖、删除、付费生成、对外发送、发布、修改关键目标和扩大工具权限。

### 5.5 V1 状态和恢复契约

任务节点创建时必须携带：

```text
thread_id
node_id
attempt
idempotency_key
```

相同完成事件重复到达时：

- 只能推进一次；
- 不能重复写回；
- 不能重复提交付费任务；
- 不能重复生成章节或覆盖版本。

应用重启后：

1. 活跃 Thread 保留；
2. 可安全恢复的现有 Job 复用已有恢复逻辑；
3. 无法安全续跑的 Chat Run 标记为 `interrupted`，保留 partial；
4. Writer、Novel 和 Studio 按各自现有语义恢复或标记中断；
5. Router 重新汇总节点状态，不根据旧事件盲目重复执行；
6. 用户可以继续、重试或查看部分结果。

### 5.6 V1 首个任务例子：织书台

用户提出：

> 把这本小说导入，整理设定和大纲，按每批 20 章拆章，发现人物冲突先问我，确认后生成前三章。

Thread 创建：

```text
《某某小说》改编任务
```

Task Node：

```text
导入原文
  ↓
整理设定
  ↓
生成大纲
  ↓
大纲复核
  ↓
拆章批次 1
拆章批次 2
拆章批次 3
  ↓
生成前三章
  ↓
一致性检查
```

执行规则：

- 导入和素材整理使用现有 Novel / Writer 能力；
- 大纲完成后自动生成复核节点；
- 发现冲突时进入 `waiting_user`；
- 用户确认后恢复对应节点；
- 拆章批次可以并行，但写回继续遵守现有版本冲突和幂等保护；
- 产物以 Artifact 关联，不把全文复制到每个 Worker；
- 最终结果从任务总管跳回织书台或关联对话。

### 5.7 V1 实施分期

#### P0：领域骨架与契约

- 新增 `naiba/super_agent/`；
- 新增 Thread、Task Node、Event、Artifact、Approval 存储；
- 定义状态机和事件类型；
- 增加 schema version、原子写和迁移；
- 使用假 Worker 验证创建、推进、取消和恢复。

#### P1：Worker Adapter 与 Router

- `ChatRunAdapter`；
- `JobAdapter`；
- `WriterAdapter`；
- `NovelAdapter`；
- 统一创建、状态、取消、重试、恢复和产物读取；
- Fast Path 确定性事件路由；
- Slow Path 语义归属预留；
- 事件去重和幂等推进。

#### P2：任务总管前端

- 新增第三个应用级入口；
- 新增任务列表、详情、时间线、待确认和产物区域；
- 对话任务条增加“查看任务总管”；
- 工作台增加“创建任务 / 关联任务 / 查看任务”；
- 桌面三栏、移动端单列详情；
- 当前任务弹窗继续作为快捷入口。

#### P3：首个垂直闭环

- 接入织书台和拆书匠；
- 完成“导入 → 设定 → 大纲 → 复核 → 拆章 → 章节”的任务链；
- 验证审批、取消、重试、重启和产物回链；
- 不影响旧的独立工作台流程。

#### P4：回归与灰度

- 假模型全链路；
- 隔离实例 UI 冒烟；
- 真实小样本任务；
- 失败、取消、重启和重复事件测试；
- feature flag 灰度；
- 明确旧流程回退方式。

---

## 六、V2：主动协作者

### 6.1 V2 目标

V2 从“用户创建任务，系统执行”升级为：

> 系统持续观察已经授权的任务环境，在安全范围内主动推进、提醒和恢复。

### 6.2 Trigger Registry

统一登记以下触发来源：

```text
manual
event
job_finished
job_failed
cron
file_changed
artifact_created
writer_stage_completed
novel_stage_completed
webhook
```

每个 Trigger 保存：

```text
trigger_id
source
condition
target_thread
action
enabled
priority
dedupe_policy
retry_policy
```

示例：

- Writer 章节生成后自动做一致性检查；
- ComfyUI 视频完成后自动收集结果；
- 文件变化后重新运行分析；
- Job 失败后通知用户并提供重试；
- 长时间无进展时提醒用户；
- 定时检查正在推进的作品。

### 6.3 Thread 订阅与事件直通

V2 为 Thread 建立订阅关系：

```text
《某某小说》改编任务
  订阅：Writer Pipeline
  订阅：Novel Project
  订阅：章节目录
  订阅：相关 ComfyUI Job
```

已绑定事件直接进入 Thread；无法判断归属的新事件才进入 Router 慢路径。避免每一条轮询事件都调用模型。

### 6.4 任务图升级

V1 先支持链式阶段，V2 支持完整任务图：

```text
      设定生成
       /    \
人物检查   世界观检查
       \    /
      统一复核
         ↓
      拆章批次
```

需要支持：

- 串行依赖；
- 并行节点；
- 汇聚节点；
- 条件分支；
- 批量节点；
- 人工审批节点；
- 失败重试；
- 降级路径；
- 部分成功；
- 超时和手动接管。

### 6.5 小说自动分镜生成短剧：V2 首个跨工作台任务

V2 的首个完整垂直任务不是把造梦机批量功能搬进任务总管，而是由任务总管协调拆书匠、提词库和造梦机，完成从小说到短剧片段的制作。各工作台继续执行自己的专业操作，任务总管负责依赖、产物引用、进度、确认和失败恢复。

```text
小说原文
  → 拆书匠：拆出剧本、分集分场和制作要素
  → 提词库：结合角色/场景/道具要素，生成资产图提示词
  → 资产制作：生成、审核并确认角色/场景/道具资产图
  → 提词库：按确认后的剧本和资产约束，生成逐场视频提示词
  → 造梦机：引用资产图和逐场提示词，批量生成短剧片段
  → 任务总管：汇总成片状态、失败项和可追溯产物
```

任务图至少需要表达：

- 小说导入、剧本拆分、分集分场和角色/场景/道具要素整理；
- 资产提示词生成、资产图生成、人工审核，以及审核后的版本确认；
- 逐场视频提示词生成，并能关联到对应剧集、场次和所需资产；
- 造梦机批量任务的提交、逐项状态、产物回收和失败重试；
- 某个角色或场景资产变更后，标出受影响的提示词和视频任务，不静默替换已确认版本。

造梦机批量能力是独立的领域执行器，批量编排不归任务总管重复实现。Super-agent 只通过现有 ComfyUI Job / 批次 API 建立关联、观察状态并回链产物；不复制批次校验、工作流槽位映射、媒体上传、提交队列或逐条重试逻辑。不得重新引入 `recipe_batch`、RecipeCoordinator 或任务总管配方线。

造梦机批次的输入必须已经解析成具体工作流槽位地址。槽位映射由用户在造梦机界面明确设置；映射缺失或失效时必须停止并要求补映射，不能由任务总管扫描工作流、根据名称猜测或静默提交。素材须先经造梦机受管媒体 API 上传，再引用其受管路径。

批次提交后，任务总管关联批次 ID、逐条任务 ID、所属剧集/场次、固定的资产版本和输出产物引用。失败恢复以现有逐条任务重试为准，只重试失败项，不重新提交已成功条目；是否整批重提必须由用户明确发起。

#### 项目资产库

V2 增加项目级资产库，供同一小说改编任务中的拆书匠、提词库和造梦机共同引用。它管理的是生产资产及其关系，不替代提词库的提示词管理，也不替代造梦机的工作流和批次执行。

首期资产类型：

```text
character
scene
prop
```

每项资产至少记录：

```text
asset_id
thread_id / project_id
asset_type
name
source_refs              # 拆书要素、原文或用户提供素材
prompt_ref               # 提词库中的生成提示词及版本
artifact_refs            # 生成的图片等文件
version
status                   # draft / review / approved / rejected / superseded
approved_version
used_by                  # 剧集、场次、提示词和造梦机任务引用
created_at / updated_at
```

约束：

- 资产按项目隔离；跨项目复用必须显式复制或授权引用，不能因名称相同自动合并。
- 生成新版本不覆盖已确认版本；下游节点固定引用具体 `asset_id + version`。
- 资产图进入造梦机前，任务总管能显示其名称、类型、确认状态和对应场次；用户能看出每张图会用于哪里。
- 拒绝或替换资产时，只重开受影响的下游节点；其他已完成场次和资产保持不变。
- 写入、替换、删除资产及付费生成等高风险动作遵守任务审批和权限快照规则。

提词库负责提示词的生成、编辑和版本；项目资产库保存提示词引用、生成资产及用途关系。资产图生成与逐场视频生成是不同的执行节点，可由造梦机不同工作流完成，各自都需要在造梦机明确配置并确认槽位映射。资产库保存资产引用及版本，不替代造梦机媒体上传与批次记录；进入视频批次前，素材仍须走造梦机受管媒体 API。造梦机批量流程仍留在造梦机，由任务图把已确认资产版本和对应场次提示词作为输入，并接回每条生成任务的状态和产物引用。每条批次任务需关联批次 ID、任务 ID、场次 ID、输入资产版本和结果引用；现有造梦机批次并不自动提供这些小说项目语义，属于 V2 需要新增的关联能力。

#### V2 垂直闭环验收

- [ ] 从一份小说样本生成可审核的剧本、分集分场和制作要素。
- [ ] 从要素生成角色、场景或道具提示词，并在项目资产库形成可追踪版本。
- [ ] 生成至少一组资产图；审核、拒绝、修改和重新确认后，版本及引用关系正确。
- [ ] 资产图和逐场视频使用各自明确配置的造梦机工作流；缺少槽位映射时拒绝提交，不猜测地址或用途。
- [ ] 素材通过造梦机受管媒体接口上传；任务总管记录批次、逐条任务、场次、输入资产版本和结果引用。
- [ ] 逐场视频提示词能关联正确的剧本场次和已确认资产版本。
- [ ] 造梦机收到的批次能逐项显示场次、提示词和资产引用；生成结果能回链到任务节点和对应场次。
- [ ] 对单项失败执行重试时不重复提交已成功项；资产版本变化不会静默改写已提交批次。
- [ ] 用真实 ComfyUI 先完成一条最小样本，再完成 2-3 条批次，验证逐条执行、失败隔离、失败项重试、卸载、产物预览和历史批次；仅通过假 Worker 或 API 单测不能标记此闭环完成。
- [ ] 中断并重启后，任务总管能恢复阶段状态、待确认项、资产版本和造梦机任务引用。

此闭环验证任务总管的价值在于跨工作台协调与产物关系管理。单独在造梦机完成批量生成仍由造梦机直接负责，不要求经过任务总管。

### 6.6 长期记忆

V2 先做结构化记忆，不急于引入向量数据库。

记忆类型：

```text
用户偏好
项目目标
已确认决策
角色设定
世界观约束
写作风格
失败经验
产物关系
版本选择
```

记忆字段：

```text
memory_id
scope
content
source
confidence
created_at
updated_at
expires_at
editable
revoked
```

记忆范围：

```text
user
project
thread
agent
artifact
```

用户必须可以查看、修改、删除、禁用记忆，并限制记忆只对某个项目或 Thread 生效。

上下文策略：

- Router 直接携带目标、当前阶段、近期决策、阻塞点和摘要；
- 完整正文、长日志、旧版本、视频分析和大型工作流留在上下文外；
- 通过 `thread_id`、`node_id`、`artifact_id` 按需召回；
- 统一使用 Inline / Spill / Chain of Recall 思路。

### 6.7 Watchdog Supervisor

V2 新增统一监督器，登记：

```text
task_id
worker_id
thread_id
started_at
last_heartbeat_at
last_progress_at
deadline
cancel_requested_at
lease_expiry
```

监督内容：

- 是否仍有心跳；
- 是否有实质进展；
- 是否超过 deadline；
- 数据库显示运行但底层线程是否消失；
- 是否产生孤儿任务；
- 是否重复恢复；
- 是否出现迟到结果。

代码负责状态收敛、超时和幂等；模型只负责解释、诊断和提出修复建议。

### 6.8 V2 自动化等级

每个 Thread 设置自动化等级：

| 等级 | 行为 |
|---|---|
| 观察 | 只收集事件，不自动执行 |
| 建议 | 提出下一步，等待用户确认 |
| 安全自动 | 自动执行读取、检查、摘要、整理和状态同步 |
| 高风险需确认 | 删除、覆盖、发布、付费生成、外发、扩权等必须确认 |

### 6.9 V2 验收目标

- Job 或工作台阶段完成后可以自动触发后续节点；
- 任务可以跨天、跨多次打开应用继续；
- 用户可以查看并修改任务记忆；
- 任务页能够显示卡住原因；
- Watchdog 能发现无进展任务；
- 安全节点自动推进，高风险节点暂停；
- 一个 Thread 可以关联多个 Conversation；
- Router 不需要加载整个项目历史；
- 失败和恢复过程可追踪。
- 小说改编项目中的资产可按版本审核、复用，并精确传递到对应场次和造梦机批次。
- 小说 → 拆书匠 → 提词库 → 项目资产库 → 造梦机的真实小样本流程通过端到端验收。

---

## 七、V3：跨信道、跨设备的 Super-agent

### 7.1 V3 目标

V3 才进入文章中更完整的 Super-agent 形态：同一任务可以从不同入口进入，在不同设备和执行环境上持续运行。

### 7.2 多信道入口

接入 Channel Adapter：

```text
Web
QQ / NapCat / AstrBot
GitHub Issue / Pull Request
Webhook
邮件
本地 API
移动端
```

统一流程：

```text
外部消息
  → Channel Adapter
  → thread_id 映射
  → Router
  → 现有 Worker
```

示例：

```text
网页创建任务
  ↓
QQ 群补充要求
  ↓
GitHub 评论提供反馈
  ↓
任务页确认方案
  ↓
Worker 修改并回报结果
```

每个渠道需要处理：

- 外部消息 ID；
- 去重；
- Thread 映射；
- 发送状态；
- 失败重试；
- 消息长度和格式；
- Webhook 验签；
- 渠道级权限和审批。

输出风格按渠道调整：

- IM：短结论、低打扰；
- Web：完整进度和产物；
- GitHub：结构化评论；
- 邮件：正式摘要；
- 手机：只通知需要决策的事项。

### 7.3 计算、状态和环境分离

V3 可拆成三层：

```text
Control Plane
  Router、Thread、记忆、事件、审批、任务图

Execution Plane
  Chat Run、Job、Writer、Novel、ComfyUI、Shell

Environment Plane
  本地电脑、代码仓库、ComfyUI 机器、远程服务器
```

远程 Router 不直接拿本地凭据，而通过轻量执行端访问用户环境：

```text
远程 Router
    ↓
本地执行端 Daemon
    ↓
用户工作区、凭据、ComfyUI、私有文件
```

执行端只接受经过授权的请求、执行并返回结果，不保存完整 Agent 会话，也不把凭据放进模型上下文。

### 7.4 多用户和 A2A

V3 才引入：

- 用户身份；
- 团队和项目权限；
- 任务共享；
- 审批人；
- 设备能力清单；
- 审计日志；
- 个人 Super-agent 与团队 Super-agent 的协作。

跨 Agent 协作必须使用结构化协议：

```text
请求目标
权限范围
输入引用
输出格式
截止时间
费用上限
可见上下文
审批要求
```

不能让不同 Agent 直接共享所有历史和凭据。

### 7.5 V3 安全要求

- 渠道白名单；
- Worker 能力声明；
- 设备执行权限；
- 外部事件可信度分级；
- Prompt Injection 防护；
- 凭据不进入模型上下文；
- 高风险动作二次确认；
- 完整审计；
- 任务级速率限制；
- 失败隔离；
- 远程执行端心跳和租约；
- 迟到结果版本校验。

---

## 八、三版边界总表

| 能力 | V1 任务总管 | V2 主动协作者 | V3 跨信道 Super-agent |
|---|---:|---:|---:|
| 一个任务跨多个 Run | 必须 | 必须 | 必须 |
| 统一任务节点 | 基础链式 | 完整任务图 | 分布式任务图 |
| Chat / Job / Writer / Novel 关联 | 首批接入 | 全面统一 | 跨设备统一 |
| 人工确认 | 必须 | 分级自动化 | 多用户审批 |
| 任务恢复 | 基础恢复 | Watchdog 自动恢复 | 分布式租约恢复 |
| 长期记忆 | Thread 摘要 | 项目 / 用户记忆 | 多用户共享记忆 |
| 定时触发 | 不做 | 支持 | 跨设备调度 |
| 文件 / Job 事件触发 | 内部事件 | Trigger Registry | 外部事件总线 |
| 外部渠道 | 不做 | 预留接口 | 正式接入 |
| 云端运行 | 不做 | 不强制 | 可选 |
| 多用户 | 不做 | 不做 | 支持 |
| A2A | 不做 | 不做 | 支持 |
| 任务中心 | 必须 | 主入口 | 多渠道统一视图 |

---

## 九、后端模块与接口规划

### 9.1 建议的领域目录

```text
naiba/super_agent/
├── __init__.py
├── models.py       # Thread / Node / Event / Artifact / Approval
├── store.py        # 持久化、迁移、原子写、幂等
├── service.py      # 创建、查询、控制和汇总
├── router.py       # Fast Path / Slow Path 路由和推进
├── adapters.py     # Chat / Job / Writer / Novel 等 Worker Adapter
├── events.py       # Agent Event 规范化和去重
├── recovery.py     # Thread 与节点恢复
└── watchdog.py     # V2 统一监督接口
```

目录名是规划建议，正式开工前再次核对现有模块边界，不在本计划阶段提前创建。

### 9.2 V1 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/super-agent/threads` | 创建任务 Thread |
| `GET` | `/api/super-agent/threads` | 查询任务列表 |
| `GET` | `/api/super-agent/threads/{id}` | 查询任务详情 |
| `GET` | `/api/super-agent/threads/{id}/events?after=` | 增量读取任务事件 |
| `POST` | `/api/super-agent/threads/{id}/message` | 向任务补充要求 |
| `POST` | `/api/super-agent/threads/{id}/pause` | 暂停任务 |
| `POST` | `/api/super-agent/threads/{id}/resume` | 继续任务 |
| `POST` | `/api/super-agent/threads/{id}/cancel` | 取消任务 |
| `POST` | `/api/super-agent/threads/{id}/retry` | 重试失败节点 |
| `POST` | `/api/super-agent/threads/{id}/approvals/{approval_id}` | 处理审批 |
| `GET` | `/api/super-agent/threads/{id}/artifacts` | 查询任务产物 |
| `POST` | `/api/super-agent/threads/{id}/links` | 关联 Conversation / 工作台任务 |

接口需要遵守：

- 所有读写都校验 Thread 所有权；
- 事件使用 `after` 游标增量读取；
- 控制动作携带幂等键；
- 长操作返回已受理状态，不阻塞 HTTP 请求；
- 不把完整正文和大型日志放入列表响应。

### 9.3 与现有接口的关系

V1 不删除和不改语义以下现有接口：

- `/api/chat`；
- `/api/runs/*`；
- `/api/jobs/*`；
- `/api/studio/*`；
- `/api/novel/*`；
- `/api/writer/*`；
- `/api/comfyui/*`。

Super-agent 通过 Adapter 和关联 ID 调用或观察它们，不把所有领域任务强行搬进同一个存储实现。造梦机批量任务优先使用现有 `/api/comfyui/batches` 和 `/api/comfyui/tasks/*` 接口；不新增或恢复任务总管配方 API、`RecipeCoordinator` 或 `recipe_batch`。

---

## 十、前端文件与状态规划

### 10.1 建议新增文件

```text
public/js/28-super-agent.js
```

职责：

- 任务总管状态；
- Thread 列表和详情；
- 事件增量轮询；
- 创建、暂停、继续、取消、重试；
- 审批卡片；
- 产物和关联入口；
- 与现有对话、工作台的跳转。

如果现有文件编号已经有新增占用，开工前按当前实际顺序选择下一个编号，不强行覆盖已有文件。

### 10.2 `index.html` 规划位置

- 侧栏模式行：在 `#openChatMode`、`#openStudio` 同级增加 `#openSuperAgent`；
- 对话侧栏：保持只在 Chat 模式显示；
- 工作台分页：保持只在 Studio 模式显示；
- 新增 `#superAgentView` 主容器；
- 新增任务列表侧栏和任务详情区域；
- 现有 `#tasksDialog` 保留，增加跳转任务总管的入口。

### 10.3 启动与恢复

`05-bootstrap.js` 启动时：

1. 读取 `/api/bootstrap`；
2. 读取 `settings.app.view`；
3. 如果是 `super-agent`，恢复任务总管页面；
4. 加载 Thread 列表；
5. 若有活动 Thread，启动事件轮询；
6. 不因为任务总管打开而停止普通 Chat 或工作台后台任务同步。

### 10.4 视图切换约束

只维护一个应用级视图：

```text
chat | studio | super-agent
```

切换 Super-agent 时：

- 不清空当前 Conversation；
- 不取消任何后台 Job；
- 不结束工作台任务；
- 只切换主视图和左侧导航；
- 返回对话或工作台后恢复原选中状态。

---

## 十一、权限、自动化和安全边界

### 11.1 权限继承

Super-agent 创建节点时冻结：

- Agent；
- 模型；
- 工作区；
- permission mode；
- enabled tools；
- Skill policy；
- 视觉能力；
- 费用 / 外部服务策略。

不能因为 Router 判断“任务需要”就动态扩大 Worker 权限。需要扩权时必须生成 Approval，让用户明确确认并重新创建带新权限的节点。

### 11.2 自动化等级

V1 默认使用：

- 读取、分析、摘要、状态同步可以自动；
- 写回关键版本需要根据领域规则确认；
- 删除、覆盖、外发、发布、付费生成必须确认。

外部事件中的文本只能作为数据，不能改变工具白名单、审批规则或费用开关。

### 11.3 取消语义

取消 Thread 时：

1. 尚未开始的节点变为 `cancelled`；
2. 可取消的 Job 发送取消请求；
3. 正在停止的 Worker 进入 `stopping`，等待原有 Watchdog 收尾；
4. 不假装底层线程已经立即消失；
5. 晚到结果必须通过 `thread_id + node_id + attempt` 校验，不能写回已取消的新任务。

---

## 十二、测试与验收总纲

### 12.1 数据和状态

- [ ] Thread 创建、读取、更新、归档；
- [ ] Task Node 依赖推进正确；
- [ ] 重复事件只推进一次；
- [ ] schema migration 可重复执行；
- [ ] 原子写失败不会损坏旧数据；
- [ ] Artifact 引用不丢失；
- [ ] Approval 状态只能按合法状态转换。

### 12.2 执行与恢复

- [ ] Chat Run、Job、Writer、Novel 能关联到同一 Thread；
- [ ] Worker 运行期间可以继续对话；
- [ ] 暂停、继续、取消和重试语义明确；
- [ ] 取消后不会产生新的副作用；
- [ ] 重启后任务不会静默消失；
- [ ] 重试不会重复写回或重复付费；
- [ ] 底层 Worker 晚到事件不会污染新任务。

### 12.3 前端

- [ ] 顶层入口与对话、工作台同级；
- [ ] 切换视图不取消后台任务；
- [ ] 普通对话仍能看到关联任务；
- [ ] 工作台能创建或关联任务；
- [ ] 任务总管能跳回工作台和对话；
- [ ] 活动任务事件可以增量刷新；
- [ ] 桌面三栏无横向溢出；
- [ ] 手机端列表、详情、审批可用；
- [ ] 任务页面不重复铺开完整正文和内部推理。

### 12.4 首个真实闭环

至少完成一次真实小样本：

```text
导入小说 → 生成设定 → 生成大纲 → 人工确认 → 拆章 → 生成章节 → 返回产物
```

并验证：

- 中途继续对话；
- 中途暂停；
- 审批后继续；
- 任务失败后重试；
- 应用重启后查看状态；
- 从任务总管跳回织书台；
- 旧的独立织书台流程仍能运行。

### 12.5 V2 / V3 专项

V2：

- [ ] Trigger 去重和失败重试；
- [ ] Thread 订阅直通；
- [ ] 项目资产库的数据契约、版本、审核状态和引用关系；
- [ ] 拆书匠、提词库、项目资产库、造梦机之间的产物交接；
- [ ] 缺少或失效的工作流槽位映射会拒绝提交，不由任务总管猜测；
- [ ] 素材经造梦机受管媒体接口上传，批次、逐条任务、场次和资产版本可关联；
- [ ] 重试只处理失败项，已成功项不会因批次重提而重复生成；
- [ ] 小说自动分镜生成短剧真实小样本通过 ComfyUI 验收；
- [ ] 长期记忆查看、编辑和撤销；
- [ ] Watchdog 检测无进展任务；
- [ ] 自动化等级不越权。

V3：

- [ ] 外部渠道消息验签和去重；
- [ ] 同一 Thread 跨渠道连续；
- [ ] 远程执行端断线与恢复；
- [ ] 多用户权限隔离；
- [ ] A2A 结构化协议；
- [ ] 凭据不进入模型上下文；
- [ ] 高风险外部动作有审计和审批。

---

## 十三、风险与停止条件

| 风险 | 影响 | 缓解 |
|---|---|---|
| 把 Super-agent 做成普通 Agent 预设 | 仍受单 Session 和人工注意力限制 | 以 Thread 和 Task Node 为系统一等对象 |
| 把 Super-agent 放进工作台分页 | 用户误解为又一个生产工具 | 采用第三个应用级入口 |
| 多套状态重复维护 | 状态漂移、任务假完成 | Adapter + 统一任务层状态投影 |
| Router 拥有过多工具 | 提示词注入和误操作风险 | Router 无高副作用工具，Worker 权限快照执行 |
| 自动恢复重复副作用 | 重复生成、重复计费、重复写回 | attempt、idempotency_key、晚到结果 fencing |
| 任务列表变成日志列表 | 信息过载 | 任务摘要、阶段、待确认和产物分层展示 |
| 过早接入外部渠道 | 安全和权限边界尚未稳定 | V1/V2 先做本地单用户，V3 再做跨信道 |
| 过早引入向量记忆 | 调试和撤销困难 | V2 先做结构化记忆和明确来源 |
| 为 Super-agent 重写现有 Worker | 回归面过大 | 只新增 Adapter 和任务层关联 |

必须暂停推进的条件：

1. 取消语义无法证明不会继续产生副作用；
2. 重试可能重复计费、重复生成或覆盖版本；
3. 普通对话或工作台旧流程受到影响；
4. Thread 所有权和工具权限无法明确校验；
5. 真实小样本尚未通过却准备宣称闭环完成；
6. 外部渠道尚未完成验签、去重和审计却准备开放自动执行。

---

## 十四、明确不做

本总计划不包含以下内容：

- 不把 Super-agent 伪装成当前 Agent 下拉中的一个角色；
- 不把所有普通聊天自动转成长期任务；
- 不把所有 Worker 的内部推理和工具日志直接展示给用户；
- 不删除或替换现有 Chat、Job、Studio、Novel、Writer、ComfyUI 接口；
- 不在 V1 引入 QQ、GitHub、邮件和云端常驻；
- 不在 V1 引入多用户和跨租户共享；
- 不在 V1 允许无限自主循环；
- 不在未确认前执行删除、覆盖、付费、发布和对外发送；
- 不用“自动恢复”掩盖底层线程实际未停止的问题；
- 不在计划阶段修改源码、构建、发版或推送。

---

## 十五、开工顺序

建议按以下顺序开工：

1. **P0：Thread / Task Node / Event / Artifact / Approval 数据契约**；
2. **P1：Chat、Job、Writer、Novel Adapter 和确定性 Router**；
3. **P2：第三个应用级入口“任务总管”与任务详情页**；
4. **P3：织书台 / 拆书匠首个真实闭环**；
5. **P4：取消、重启、重试、幂等和全量回归**；
6. **V2：Trigger、订阅、任务图、结构化记忆和统一 Watchdog**；
7. **V3：多信道、远程执行、多用户和 A2A**。

每个阶段开始前重新核对：

- HEAD 和工作区状态；
- 当前导航结构；
- Writer / Novel / Job 的真实状态机；
- 现有发布和测试约束；
- 是否需要用户另行授权真实供应商、付费生成、发版或推送。

---

## 十六、最终产品形态

三版完成后，用户看到的是：

```text
任务总管
 ├── 总目标
 ├── 当前阶段
 ├── 执行进度
 ├── 工作台产物
 ├── 后台任务
 ├── 历史决策
 ├── 待确认事项
 ├── 自动化规则
 └── 可接入的消息渠道
```

内部形成：

```text
Router
  → Thread
    → Task Graph
      → Chat Run / Job / Writer / Novel / Studio / ComfyUI
        → EventBus
          → Watchdog / Memory / Artifact / Approval
```

最终口径：

> **V1 先把多个已有执行系统挂到一个任务线程；V2 再做主动触发和长期记忆；V3 最后做跨信道、跨设备和多用户。**
>
> **前端入口固定为与「对话」「工作台」同级的第三个应用级入口，用户看到的名称建议是「任务总管」，Super-agent 作为解释性副标题和内部架构名。**
