# Super-agent V1 用户反馈整改交接

- 日期：2026-11-02。
- 用途：在新对话继续用户逐条反馈的整改，不是竣工报告或发布证明。
- 当前结论：**渲染断点、F1–F3 功能与 F4 三栏结构/导航通过隔离验收；参考图视觉保真仍在整改，不能声称前端或 V1 整体完成。** 详见 [反馈整改验收](2026-11-02-Super-agent-反馈整改验收.md)。
- 下文第 2–6 节保留为接续前的问题快照，不再代表这些反馈项的当前实现状态。自动续办闭环是后续新增范围，按独立计划实施，不计入本次反馈通过结果。
- 没有创建 commit、没有 push、没有发布，没有重启或操作 8766；执行了本地模型替身验收，未调用外部付费模型。

## 1. 用户最新要求与边界

用户先要求完整 V1 后才发布稳定版 `4.0.0`、推 master，随后明确暂停发布：

> 4.0.0等下先不发版，等我看一遍界面

之后用户要求：

> 接下来我一一汇报问题，你整改

本次最后要求：

> 任务总管分页，还残留了对话入口的左侧栏
> 写一份交接文档，交代未完成的任务，在新对话进行

**执行口径：**

- 先处理下面四项用户反馈，不自动恢复整套 V1 发版目标。
- 不发版、不推 master、不擅自提交。旧文档中的发布建议或历史授权不能覆盖最新暂停要求。
- 用户正在使用 **8766 源码实例**；不要擅自终止、重启、取消它的任务或点击真实工具审批。
- 不用“默认完全权限”修复审批等待，不扩大工作区可信根、不自动批准、不自动重提可能计费的任务。
- 优先在独立数据目录与临时端口复现；改完后如需重启 8766，让用户确认时机。
- 当前已通知所有本轮子任务停止；没有由本轮反馈整改启动的持久服务。

## 2. 新对话第一件事：修复半成品 DOM 断点

**这是本次暂停时新产生、尚未验证的明显断点，优先级高于继续美化。**

`public/index.html` 的任务总管主体已改了一部分：

```html
<div id="superAgentStageChain">
  <div id="superAgentNodeList"></div>
</div>
```

但 `public/js/28-super-agent.js::renderDetail` 仍沿用旧逻辑：

```js
$("#superAgentStageChain").innerHTML = /* 节点列表 */;
$("#superAgentNodeList").innerHTML = $("#superAgentStageChain").innerHTML;
```

第一行会删掉嵌套的 NodeList，第二行再查该元素会得到 null。因此**不能把当前页面当成可用预览，也不能只刷新后宣布布局完成**。

推荐接续方式：把节点渲染直接写入唯一的 NodeList，空态也写同一容器；删除旧复制行，并只保留一套操作事件委托，避免嵌套冒泡导致一次点击发两次动作。若先恢复原 DOM 保证可用，只能定点恢复本次相应片段，不可整文件 checkout 覆盖已有工作。

相关入口（行号会随修改变化）：
- `public/index.html`：任务总管主体约 1670–1724 行；步骤容器约 1713 行。
- `public/js/28-super-agent.js`：`renderEmptyDetail`、`renderDetail`、`bindSuperAgentEvents`；旧复制约 182 行，两套委托约 673–681 行。

## 3. 用户反馈清单：全部尚未完成验收

### F1：8766 的 Chat 任务出现 NoneType 报错

截图显示：

> 控制结果需对账，未自动重放：'NoneType' object is not iterable

同时节点显示运行中。**根因已通过当前源码确认，但修复尚未落地。**

调用链：

1. `naiba/super_agent/store.py::_json(None, [])` 直接序列化成 JSON `null`，未使用默认列表。
2. `_load("null", [])` 又返回 `None`，使 `Thread.linked_run_ids` / `linked_job_ids` 不再是列表。
3. `naiba/super_agent/runtime.py::RuntimeAdapter._bind` 先保存 executor_id、发 `run_started`，随后执行 `list(getattr(thread, field))` 报错。
4. 此时真实 Chat 已经由 `runs.submit_chat` 启动。**修复不得重新 submit，否则可能重复执行和计费。**

待做：
- Store 的容器字段写入归一、旧 JSON null 读取兼容；只读兼容优先，不手工批量改用户数据库。
- `_update_node_db` 当前把 `checkpoint` 和 `output_refs` 都按 `[]` 默认序列化，需区分字典 `{}` 与列表 `[]`。
- `_bind` 加列表防御；对既有 executor 进行关联修复、清除已解决错误并结算原 claim。
- `command` 已绑定分支当前直接返回 status，`_tick_thread` 当前只把 interrupted 接回 running，均未完整修 linked/error/claim。
- 没有 executor_id 但已有持久 correlation 时，先查找原执行；不确定则明确待恢复，不盲目重放。
- 补合法状态转换：Thread `active → needs_recovery` 当前仍缺；Node `waiting_user → interrupted/stopping` 也缺，需结合重启和取消设计。

待测：新建/旧 null 数据、Chat/Job/子 Agent 绑定、仅启动一次、故障后接回同一执行、旧 attempt 不污染新执行。

### F2：聊天非完全权限时，等待审批像卡住

截图里的 `list_directory` 读取工作区外目录，**要求确认本身是正常权限行为**。截图能证明总管没有反映等待状态，不能单凭截图认定“已经点击允许仍然死锁”。

现有真实审批链：
- `naiba/tools/executor.py` 在 `_confirmation_lock` 下登记 `pending_confirmation`。
- `naiba/skills/agent.py` 发 `tool_confirm`，等待用户允许/拒绝。
- `naiba/events.py` 把 Chat Run 状态设为 `waiting`；工具结果后回到 `running`。
- `public/js/12-chat-input.js::approveTool/rejectTool` 使用 `/api/tool/confirm`、`/api/tool/reject`。
- `naiba/run/manager.py::owns_confirmation/confirm_tool_async` 使用该 Run 的隔离执行器。
- 总管 RuntimeAdapter 未投影该 waiting 状态；总管 Approval 列表也不是 Chat 工具审批记录。

推荐方案（**已约定、尚未实现**）：

```text
ConversationRunManager.confirmation_snapshot(run_id)
  → [{confirm_id, run_id, conversation_id, tool, reason, status}]

GET /api/super-agent/threads/{id}
  → thread.chat_confirmations: [
      {node_id, run_id, conversation_id, confirm_id, tool, reason, status}
    ]
```

- `status` 只用 `pending` / `processing`；无项目时返回 `[]`。
- 仅从当前活执行器的确认锁内取快照，核对 run、conversation、当前节点 attempt 的持久关联；禁止从历史事件重建授权。
- `reason` 使用固定安全短文案，`tool` 仅合法工具标识；不返回原始参数、绝对路径、权限或凭据。
- `pending`：总管显示“等待你确认”，提供“回到聊天确认”。
- `processing`：显示“已确认，工具正在执行”，不再出现重复批准按钮。
- 仍复用聊天原审批接口，总管不新增第二套批准机制。
- 全部解除后恢复运行；拒绝、超时、取消、重启需清掉可操作投影。
- 当前 `owns_confirmation` 直接无锁读取 pending，终态排除未完整含 interrupted/stopping，需一并审查。

本轮主 agent **只落了两个局部准备补丁，不能视为功能已修复**：
1. `naiba/http.py::_stream_run`：若存在 `confirmation_snapshot` 则标注 `confirm_processing` / `confirm_resolved`，否则走旧 owns 判断。**manager 方法仍不存在，故新分支尚不会生效。**
2. `public/js/12-chat-input.js::handleToolConfirmEvent`：重复确认卡收到 processing/resolved 时更新已有卡的文字。

仍缺：首次渲染 processing 卡时去掉按钮；`approveTool` 成功现在还写“已执行”，应改为已允许/正在执行，不能将异步受理当完成；确认状态契约登记、流回放与真实允许/拒绝测试。`tests/test_run_events.py` 本轮未修改。

后端子任务本轮仅给 `naiba/core/contracts.py` 的 typing import 加了 `Literal`，DTO TypedDict 尚未写，当前 import 未使用。

### F3：新建任务表单机械、重复、不人性化

用户截图中“目标 / 补充要求”与“任务指令”重复，内部术语多，模型设置占据过多首屏。

确认采用的整改方向（**尚未实施**）：
- 用一个“你想完成什么？”输入替代重复两栏；Chat 同一内容映射 `goal` 和 `message`，子 Agent 映射 `goal` 和 `instruction`。
- 任务名称可选并弱化，默认命名仍可用。
- 按任务类型渐进展示作品、会话、章节范围；不让普通用户手填内部 ID。
- 把供应商/模型收进可展开“使用的模型”，仍保留全局默认和手动切换，不复制 API Key。
- 跨类型步骤收进可展开“串联多个步骤”，保留现有真实 payload，不虚构未支持能力。
- 桌面更宽、更紧凑的分区；手机单列，提交按钮易找到。
- 提示语描述用户可感知行为，不说“执行器、兼容模式、固定链、attempt”等实现用语。

涉及 `public/index.html` 新建弹窗、`public/js/28-super-agent.js` 创建状态/payload、`public/styles.css`、`tests/test_super_agent_ui.py`。

### F4：按旧 HTML 预览做横向布局，清掉聊天左栏残留

参考原件：`docs/design/2026-11-01-任务总管-V1-预览.html`。用户又给了该 HTML 的截图；以真实应用能力填充，不复制演示数据和假进度。

桌面目标：
- 左：任务总管自己的任务列表、新建、筛选。
- 中：标题、总体进度、当前阶段、最近更新、步骤与时间线。
- 右：待确认、下一步、最近产物、相关入口。

**最新新增反馈：**任务总管外侧仍残留聊天页的工作区/搜索/手机访问等侧栏，导致“聊天侧栏 + 任务列表 + 详情 + 右栏”叠成四列，挤占内容。不要只改内部 `.super-agent-columns` 就算完成。

后续应在 `body[data-app-view="super-agent"]` 下处理聊天专属侧栏/布局轨道，并保留可用的应用级“对话 / 工作台 / 任务总管”切换；切回聊天必须恢复原布局、原折叠偏好。不能通过隐藏整块导航让用户无法返回。移动端导航也需独立验证。

当前 CSS 只隐藏 `.messages/.composer-wrap/.topbar/.studio-view` 等；工作台有自己的 `body[data-studio="on"]` 侧栏规则，任务总管没有对等完整处理。入口：
- `public/styles.css`：约 3500–3550 行工作台规则，约 5977 行起总管规则。
- `public/js/01-core.js::setAppView`、`public/js/14-file-panel.js` 的侧栏控制。
- `public/js/28-super-agent.js::openSuperAgentView/bindSuperAgentEvents` 仍接了全局聊天侧栏展开按钮。

重复节点的原根因：JS 把 StageChain HTML 复制到 NodeList，NodeList 的 `hidden` 又被作者 CSS `display:flex` 覆盖。必须单一渲染、单一事件委托，删除重复层时一起收掉高度。

**本轮 UI 只完成任务总管主体 HTML 的部分替换**：新建了筛选、概况、聊天确认、下一步容器；JS/CSS/表单/测试未跟上。详情见第2节断点。

## 4. 前一阶段 V1 扩展已写代码，但不是完整交付

以下改动来自本会话前半段；用户后来暂停整体目标。不要误以为都是本轮反馈修复成果。

- `naiba/super_agent/runtime.py`：新增 Chat/Job/子 Agent 桥、后台对账、claim、mixed 线性编排；文件当前未跟踪。
- `store.py/domain.py/models.py/app.py`：生产装配、创建/动作扩展、部分 stopping、列表安全回链字段。
- `run/chat.py`、`core/contracts.py`：持久执行关联键。
- `jobs.py/subagent.py`：子 Agent 异常误成功与结果覆盖修复草稿。
- 前端已有作品选择、单阶段/完整领域链、章节参数、可编辑部标题、Chat/已有 Job/子 Agent、mixed 步骤及线程控制草稿。

接口现状/约束：
- 顶层 `autostart`、`auto_advance` 为 bool，缺省 false；旧手动语义不自动改成运行。
- Writer/Novel 完整领域链复用已有 API；Writer 正文目前只针对指定一章，不宣传自动写完整本。
- `domain=mixed`、`nodes:[{key,domain,...单节点payload}]`；mixed 还需核查安全校验、上下文衔接和幂等。
- Job 仅关联已有 `job_id`，不新增任意 shell/kind/params 的绕权入口。
- `GET /api/super-agent/threads` 列表已加入 `linked_conversation_ids`、`linked_book_refs` 安全字段；**没有新增 query 过滤**，UI 一次列表后本地筛选，不做 N+1 详情请求。mixed 领域关联尚需补齐验证。

前一阶段遗留：
1. Writer 按 `pipeline_id` 校验执行身份，不能只读同书当前任意流水线；Novel 取消需等待真实执行器确认。
2. 暂停/审批公共闸门、claim 确定失败与未知结果分类、恢复对账与显式重试。
3. 临时 DB 删除后的调度线程退出，避免单测 teardown 后刷后台异常。
4. mixed 跨执行器产物引用/上下文传递、权限快照复核。
5. 普通聊天页关联任务提示，织书台/拆书匠“安排任务”与关联入口：**尚未实施**。
6. 预定 `document` 事件 `naiba-open-super-agent`，detail 为 `{threadId?,domain?,bookId?,conversationId?,create?:true}`；28 模块预填准备函数已有，事件监听尚未接。
7. 不得以以上草稿或历史 P4 通过结果宣称 V1 竣工。

## 5. Git 与工作树：不要覆盖或混提

核查时：
- 分支：`feat/super-agent-p0-to-p1-handoff`。
- HEAD：`7872fb69bef7afdb6859f39217246d47c459501b`。
- 本会话无新 commit、无 push、无 Release。

暂存区已有上轮静态收尾 **12 个文件**，尚未 commit：

```text
.gitignore
public/js/11-run-stream.js
public/js/12-chat-input.js
public/js/skill-script-rules.js
tests/test_skill_prompts.py
tests/test_js_module_analysis.py
verify/cleanup_verify.py
verify/esm_graph_check.py
verify/tdz_check.py
verify/js_module_analysis.cjs
docs/plans/2026-11-02-ESM-TDZ-逐项静态门禁审计.md
docs/plans/2026-11-02-Super-agent-P4-最终验收审计.md
```

**特别注意 `public/js/12-chat-input.js` 是 MM：**暂存区是共享规则解环修复；工作树又有本次确认卡局部补丁。不能把 cached diff 当当前全部代码，也不要 reset/checkout 一把覆盖。

其余 V1 和 UI 改动未暂存。新 `verify/super_agent_v1_*`、`verify/_serve_super_agent_v1_ui.py` 仍被 verify 忽略规则覆盖，尚未加 `.gitignore` 放行和 `cleanup_verify.KEEP`。若将来纳入提交，两处必须同步，不能用 `git add -A`。

无关文件保持原样：`NUL`、docx 反馈、图片目录、扫描 CSV/PS1、read_doc 脚本和既有未跟踪计划/预览。不要清理或混入提交。

## 6. 真实测试与验证现状

### 已执行，但不能代表当前最新工作树

| 检查 | 结果与边界 |
|---|---|
| 上轮静态收尾 ESM/TDZ | 本会话前半段复跑通过：30 模块、0 TDZ/绑定问题；不是当前 HTML/JS 半成品的验收 |
| 静态专项 | `tests.test_js_module_analysis tests.test_skill_prompts tests.test_verify_assets`：20 项通过 |
| 首轮全量 | `verify/_tmp_v1_first_full.log`：3773 项，188.254 秒，FAILED(errors=1)；旧 P3 测试把 chat 当未知领域，现抛 LookupError；另有后台线程 SQLite 路径已删除异常 |
| 后端早期定向 | 子任务报告 75 项通过，但在 mixed/stopping/权限复核后续改动之前；最新 WIP 无对应通过证据 |
| stable/Beta 发布判据 | `tests/test_release_workflow.py` 和 `verify/_release_check.py` 改造，子任务报告 41 项通过；版本仍 `4.0.0-beta`，没有改发布材料 |
| V1 UI 资产单测 | 5 项：3通过、1失败、1错误。文本标记断言不匹配；公开 node DTO 无 executor_id；mixed 创建 HTTP 201 不等于执行通过 |
| 真实验收工具 | 仅 `--plan` 和较早 AST/compile 通过，最后小改未重测；从未运行真实闭环 |
| 本轮用户反馈整改 | **没有执行新单测、语法检查或浏览器验收** |

旧全量错误定位：`tests/test_super_agent_p3_writer_novel.py::test_unknown_domain_and_owner_boundary_are_rejected`。最终应使用真正未知 domain 测拒绝，并补合法 chat 缺参/不存在会话的契约测试，不能只删失败断言。

### 保留的未验收工具

- `verify/_serve_super_agent_v1_ui.py`
- `verify/super_agent_v1_ui_smoke.cjs`
- `tests/test_super_agent_v1_ui_assets.py`
- `verify/super_agent_v1_acceptance.py`
- `verify/super_agent_v1_fixture.py`

UI 工具用真实 App/HTTP 和本地替身；Chat/子 Agent 原模式只创建、不开跑。已知待修：公开 DTO 断言、两视口 retry fixture 隔离、回链门禁、新表单字段更新。**不曾执行浏览器脚本，不能说截图/手机验收通过。**

本轮仅加载过浏览器能力说明，未打开8766进行操作。后续浏览器测试先加载 `browser-use:control-browser`，用当前官方工具；项目维护另要求对应 Playwright 冒烟。截图/日志放忽略的测试目录。

### 真实供应商

用户此前确认过 `tedschat / deepseek-v4.1-flash` 小样本（最多20次推理、串行、含内部重试），但整体目标后来暂停。只完成生产模型目录查询，返回2个模型且目标精确匹配；**本会话没有真实推理调用，不能将用户自行在8766使用模型的调用算成我们的验收。**

先完成本次反馈整改；要继续付费闭环时确认目标已恢复，使用独立原创测试文本和数据目录，不动现有作品。真实脚本离线预算估算18次、硬上限20次；网络拦截、流式锁、重启及产物唯一性还待代码审查，不能直接开跑。

## 7. 建议接续顺序

1. 读根 `项目维护说明（修改代码前必读）.md`、本文，核对 status/cached diff，确认没有其他会话继续写文件。
2. **先修第2节 DOM/JS 半接线**，避免用户再打开详情即空引用。不要继续增加未要求的能力。
3. 修 F1 容器默认值及同一 executor 的幂等恢复；用隔离数据复现，不重提8766真实任务。
4. 修 F2 快照、等待状态、出网 DTO、首次/重复确认卡 processing 文案；用本地假模型加真实 ToolExecutor 走允许/拒绝闭环。
5. 完成 F3 自然表单，完成 F4 三栏及聊天侧栏隐藏/布局释放/切回恢复；新容器只展示真实数据。
6. 补专项：null新旧兼容、一次submit、恢复不重放、多确认/processing/取消/超时/重启；更新 UI 行为测试，不仅 grep 字符串。
7. 用项目 `.venv/Scripts/python.exe` 跑专项，再按公共 API/状态改动等级跑全量、ESM/TDZ、Node语法。失败如实分本次/旧WIP，不拿历史绿灯顶替。
8. 在隔离实例做桌面1280×900和手机375×900浏览器验收：三栏、无聊天残留侧栏、单份节点、自然表单、待确认跳转、允许/拒绝后恢复。每个关键步骤查页面错误与横向溢出。
9. 给用户看修后界面、报告实际证据，继续等下一条反馈；**不发版、不推 master。**

## 8. 新对话可复制提示

```text
读取 docs/plans/2026-11-02-Super-agent-V1-用户反馈整改交接.md，继续用户逐条反馈整改，不恢复整体发版目标。
当前用户正在用8766源码实例。先处理：详情DOM/JS半接线；NoneType关联绑定错误且绝不能重提真实任务；聊天审批等待/处理中真实显示且不放宽权限；新建表单自然文案；按旧HTML预览做横向三栏，并移除残留聊天专属侧栏、保留应用导航。
先读维护说明、检查git工作树和暂存区。public/js/12-chat-input.js为MM，暂存的是旧解环修复，未暂存是确认卡准备补丁，不能覆盖。
所有修复在隔离实例验证，尚未实现的confirmation_snapshot/chat_confirmations接口先补契约。不要擅自重启8766、批准它的工具、重试真实模型、清理用户文件。
完成针对性测试和桌面/手机浏览器验收后给用户看界面。4.0.0不发布、不push、不擅自commit；未通过的事项如实保留。
```
