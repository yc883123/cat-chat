# Super-agent P4 阶段链未完成交接

- 日期：2026-11-01
- 当前分支：`feat/super-agent-p0-to-p1-handoff`
- 上一份交接：`docs/plans/2026-11-01-Super-agent-P4-开始交接.md`
- 本文用途：记录本轮未完成的 P4 阶段链工作，供新对话继续；**不是完成证明**。
- 提交状态：本轮没有提交 commit。

> 重要：当前工作树包含本轮 P3/P4-0 以及阶段链半成品，还有用户原有的无关修改和未跟踪文件。新对话不得清理、回滚或整体提交工作树。

---

## 1. 目标与当前准确结论

P4 的目标是把任务总管从 P3 的“单 Thread + 单 Node 控制面”扩展为可持久化、可恢复、可控制的 Writer/Novel 阶段链：

```text
Writer：设定 → 大纲 → 拆章/章节 → 正文
Novel ：导入 → 设定/要素 → 大纲 → 剧本 → 复核/产物
```

当前准确状态：

- P3 单 Node 领域桥接、控制、Approval、attempt/request_id 幂等、interrupted/recovery 仍可用。
- P4-0 创建弹窗、控制提示和基础前端布局已收口，现有 P3 回归仍通过。
- 阶段链的字段、迁移、部分 store helper、前端投影已经出现，但**阶段链创建、原子推进、审批推进、真实浏览器专项验收仍未完成**。
- 不能把当前状态宣称为 P4 完成，也不能把现有 18 个定向测试当作 P4 阶段链证据。

---

## 2. 本轮实际完成的部分

### 2.1 已存在的后端阶段链半成品

涉及：

- `naiba/storage/store.py`
- `naiba/super_agent/models.py`
- `naiba/super_agent/store.py`
- `naiba/super_agent/service.py`
- `naiba/super_agent/domain.py`
- `naiba/app.py`

已存在：

- `CURRENT_SCHEMA_VERSION` 已从 29 改为 30。
- `v30` 迁移函数已增加：
  - Thread：`chain_version`
  - Node：`stage_key`、`sequence`、`promote_to`、`promotion_policy`、`chain_instance_id`
  - `idx_super_nodes_chain` 索引
- `Thread` / `TaskNode` dataclass 已增加对应字段。
- Store 已能读取/写入这些字段。
- Store 已出现 `create_chain()`、`list_chain()`、`promote_chain_node()` 的初步 helper。
- Service 已出现 `list_chain()` 和 `promote_chain_node()` facade。
- `domain.py` 已出现纯函数：
  - `build_writer_graph()`
  - `build_novel_graph()`
- `app.py` Node/Thread DTO 已开始返回阶段字段和 `chain_version`。
- `models.py` 已增加 `stage_promoted` 事件类型（后续仍需真正写入并测试）。

### 2.2 已存在的前端阶段链半成品

涉及：

- `public/js/28-super-agent.js`
- `public/index.html`
- `public/styles.css`
- `tests/test_super_agent_ui.py`

已存在：

- 阶段链容器 `#superAgentStageChain`。
- 使用 `stage_key`、`sequence`、`chain_version`、`current_node_id` 的渲染代码。
- 当前节点高亮和 `blocked/ready/running/waiting_user/failed` 状态 class。
- 节点关联 Approval/Artifact 的展示。
- 首次事件全量读取，后续尝试使用 `/events?after=<cursor>` 增量合并。
- 旧普通 Node 列表的兼容渲染保留在隐藏的 `#superAgentNodeList` 中。
- 现有 UI 静态测试从 5 个增加到 6 个，已覆盖上述字符串契约的一部分。

---

## 3. 明确未完成、必须继续的工作

### 3.1 P0：先修复当前半成品的确定性错误

#### A. `DomainCoordinator.create_thread()` 仍然只创建一个 Node

位置：`naiba/super_agent/domain.py`

当前仍按旧逻辑读取：

```text
body.domain
body.book_id
body.stage
body.start_request
```

然后：

```text
创建一个 Thread
创建一个 Node
```

虽然 `build_writer_graph()` / `build_novel_graph()` 存在，但没有真正接入 chain create。需要显式区分：

- 旧兼容模式：有 `stage`、无 chain body，继续创建一个 Node；
- 新 chain 模式：有明确的 `chain` / `chain_mode` / `stages` 字段，创建完整 graph。

不要让旧 `stage` 字段悄悄改变语义。

#### B. `create_chain()` 当前实现不是完整原子创建

位置：`naiba/super_agent/store.py`

当前实现逐个调用 `create_node()`，而 `create_node()` 会先验证依赖节点已经存在。若传入的链节点依赖逻辑使用前置 Node ID，必须确认：

- 不能引用尚未插入的节点；
- 不能在中途失败后留下半条 Thread/Node 链；
- 整个 Thread + graph 最好在一个 `BEGIN IMMEDIATE` 事务中完成；
- 重复整体幂等请求只能得到同一 Thread/Node graph。

建议新建专用 `_create_chain_db()`，不要用多个独立事务拼接完整链。

#### C. 阶段依赖/状态契约尚未统一

计划要求：

- 第一个节点 `ready`；
- 后续节点 `blocked`；
- `dependencies` 指向真实 Node ID；
- `promote_to` 指向真实后继 Node ID；
- `current_node_id` 指向当前阶段。

当前 builder 只输出 stage 名称，Store 还需要在插入后完成真实 ID 映射，并验证顺序、依赖、promotion policy。

#### D. `promote_chain_node()` 尚未达到 P4 原子 promote 契约

当前 helper 需要继续补齐：

- source Node 必须匹配 `owner/thread/attempt`；
- source 必须是本链当前成功节点；
- 重复 completion / 重复 sync / 重启重放只能推进一次；
- target ready、Thread.current_node_id、Thread 状态和推进事件应在同一事务中更新；
- 最终节点成功后才把 Thread 置为 completed；
- 失败、取消、旧 attempt、暂停、needs_recovery 不得推进；
- `request_id`/promotion key 必须持久化，不能只依赖进程内缓存。

当前代码中 `promote_chain_node()` 没有完整写出 `stage_promoted` 事件，且 `request_id` 尚未形成可靠的持久幂等 ledger；这必须修正。

#### E. Service/Store 有两套事件应用逻辑

位置：

- `naiba/super_agent/service.py::_apply_event()`
- `naiba/super_agent/store.py::append_event_and_apply()`

需要明确唯一的阶段推进入口，避免一边调用 `_promote_ready_nodes_db()`，另一边调用 `promote_chain_node()` 导致双重决策。建议：

```text
accepted terminal event
→ attempt fence
→ current node finalize
→ chain coordinator atomic promote
→ stage_promoted event
```

通用依赖 ready helper 只能作为内部基础函数，不能继续承载完整领域编排。

### 3.2 P1：真正接入 Writer/Novel chain create

建议新增明确的请求契约，例如：

```json
{
  "domain": "writer",
  "book_id": "book-1",
  "chain": "writer_v1",
  "chain_version": 1,
  "chain_instance_id": "stable-request-chain-id",
  "title": "写作任务",
  "goal": "完成整条写作流程",
  "stage_requests": {
    "setup": {},
    "outline": {},
    "chapter_plan": {},
    "chapter_content": {}
  },
  "model_name": "probe",
  "model_key": "probe",
  "idempotency_key": "chain-create-1"
}
```

Novel 对应：

```json
{
  "domain": "novel",
  "chain": "novel_v1",
  "chain_version": 1,
  "stage_requests": {
    "import": {},
    "elements": {},
    "outline": {},
    "screenplay": {},
    "review": {}
  }
}
```

实际字段名称可调整，但必须：

- chain 模式明确可识别；
- 旧单 Node body 完全兼容；
- 阶段白名单固定且不允许任意客户端注入；
- 所有 Node 都有正确 executor、domain_ref、checkpoint 最小引用；
- 仅首节点 ready，后续 blocked；
- 返回 Thread + 全部 nodes 或可查询的稳定投影；
- chain create 失败不残留半条链。

注意：当前 `DomainCoordinator.NOVEL_STAGES` 仍未纳入真实 Novel 服务的 `storyboard` 阶段，需先决定 P4 五阶段 canonical key 是否使用 `screenplay`，并做好领域服务映射，不能让白名单与 `naiba/novel/prompts.py` 漂移。

### 3.3 P2：领域 Adapter 与阶段推进连接

涉及：`naiba/super_agent/domain_adapters.py`

必须验证并实现：

- Writer 当前阶段成功后只推进一次；
- Novel 当前阶段需要 Approval 时，approve → apply → 当前节点 succeeded → 下一节点 ready；
- reject 不推进，且停留/失败语义固定；
- apply 失败不能留下 `approved + ready` 的不可恢复死状态；
- 旧 attempt 的迟到成功/失败事件不能推进新 attempt；
- recovery 后不能隐式重新提交领域任务或误推进下一阶段；
- `resume` 与 `retry` 的语义保持现有 P3 契约，不要重复提交 Writer 任务。

外部领域任务提交与 Super-agent Node 落库之间存在崩溃窗口，P4 完整自动链前必须至少有明确方案：

- 领域侧幂等提交键；或
- 先持久化 intent，再提交并 reconcile；或
- 启动恢复时反查并绑定孤儿领域任务。

不能靠内存缓存解决。

### 3.4 P3：HTTP DTO 与事件投影收口

涉及：`naiba/app.py`、必要时 `naiba/http.py`

需要补齐：

- Thread DTO：`chain_version`、当前阶段摘要/当前 stage key（可由 nodes 派生，但要固定契约）；
- Node DTO：`stage_key`、`sequence`、`promote_to`、`promotion_policy`、`chain_instance_id`；
- Event DTO：安全的 `stage_promoted` 摘要，至少包含 from/to stage/node 的公开信息；
- 不泄漏 checkpoint、start_request、apply_request、`_adapter_commands`、raw、完整领域任务对象；
- `GET /events?after=` 在 promote 后只返回新增事件且 cursor 单调；
- 如不增加独立 promote HTTP action，则自动推进应完全由后端事件/协调器完成。

当前 Node DTO 只返回部分阶段字段，尚未返回 `promote_to` 和 `promotion_policy`；当前事件 summary 白名单也没有阶段推进字段。

### 3.5 P4：前端阶段链展示仍需真实验证

涉及：

- `public/js/28-super-agent.js`
- `public/index.html`
- `public/styles.css`
- `tests/test_super_agent_ui.py`

已经有静态实现，但还需：

- 确认后端真实 chain projection 能喂给它，而不是只靠字符串测试；
- 当前节点高亮与 `current_node_id` 一致；
- blocked/ready/running/waiting_user/failed/succeeded 在真实状态变化中正确更新；
- 阶段推进事件能进入时间线；
- 节点级 action 不会把 blocked 节点误显示成可开始；
- Approval/Artifact 关联到正确阶段；
- 事件增量合并不重复、不丢失、不因切换 Thread 串线；
- 第一次切换 Thread 时重置 cursor；
- 页面刷新后从服务端重新建立 cursor，不依赖旧内存状态；
- 桌面 1280×900、移动 430×900 无横向溢出。

不要再修改已收口的创建弹窗结构，除非真实浏览器发现回归。

### 3.6 P5：专项浏览器冒烟尚未完成

交接文档要求的 `verify/super_agent_stage_chain_smoke.cjs` 或等价脚本尚未新增。现有：

- `verify/super_agent_controls_smoke.cjs`：主要验证 P4-0 弹窗和控制面布局；
- `verify/super_agent_smoke.cjs`：普通视图/溢出检查；
- `verify/p4_display_smoke.*`、`verify/p5_writeback_*`：其他媒体功能，不是阶段链验收。

必须新增或明确等价的浏览器门禁，至少覆盖：

- Writer chain 创建；
- Novel chain 创建；
- 首阶段 ready，其余 blocked；
- start 后 running；
- 成功后下一阶段 ready；
- 失败后 retry；
- Novel approval waiting；
- approve 后推进；
- reject 不推进；
- 回到 Writer/Novel 工作台；
- 桌面/移动无溢出；
- 零 pageerror；
- 零 console.error；
- 无 4xx/5xx；
- 保存截图。

如果 Chromium/Playwright 环境不可用，必须如实记录为环境阻断；静态测试不能替代浏览器门禁。

---

## 4. 当前测试与验证证据

### 4.1 本轮实际运行且通过

```bash
python -m unittest tests.test_super_agent_p3_http tests.test_super_agent_p3_writer_novel tests.test_super_agent_ui -q
```

结果：

```text
Ran 18 tests
OK
```

```bash
python -m compileall -q naiba/super_agent naiba/app.py naiba/storage/store.py
node --check public/js/28-super-agent.js
git diff --check
```

上述检查本轮通过（其中 `models.py` 最后一次小修正后，新对话应再次重跑）。

### 4.2 这些证据没有覆盖的内容

当前没有 P4 阶段链专用测试文件。检索未发现 `tests/test_super_agent_p4_chain.py`。

未覆盖：

- Writer/Novel graph builder 的顺序/依赖/初始状态；
- v30 migration 默认值/旧库升级；
- chain create 多 Node；
- promotion 幂等和 `current_node_id`；
- `stage_promoted` 事件；
- approval promote；
- reject 不推进；
- 旧 attempt fencing 与 chain 组合；
- chain HTTP DTO；
- 真实浏览器阶段链闭环。

### 4.3 迁移测试的已知风险

`tests/test_storage_migration.py` 中仍有硬编码断言：

```python
self.assertEqual(CURRENT_SCHEMA_VERSION, 29)
```

因为当前代码已升到 v30，这个测试需要更新为与 v30 契约一致，并新增：

- v29 → v30 升级字段存在；
- 默认值不破坏旧 Thread/Node；
- 重复迁移幂等；
- 新库直接达到 v30。

不要为了测试通过把生产版本回退到 29。

---

## 5. 当前工作树边界

本轮没有清理、回滚或提交无关文件。新对话必须保留这些已有改动：

```text
M naiba/app.py
M naiba/http.py
M naiba/storage/store.py
M naiba/super_agent/models.py
M naiba/super_agent/service.py
M naiba/super_agent/store.py
M public/index.html
M public/js/25-novel.js
M public/js/27-writer.js
M public/js/28-super-agent.js
M public/styles.css
M tests/test_super_agent_ui.py
?? NUL
?? docs/design/2026-11-01-任务总管-V1-预览.html
?? docs/plans/2026-11-01-Super-agent-P3-to-P4-交接.md
?? docs/plans/2026-11-01-Super-agent-P4-开始交接.md
?? docs/plans/2026-11-01-Super-agent工作台总计划.md
?? docs/plans/2026-11-01-打标器通用描述与自定义档位交接.md
?? docs/调研报告-2026-11-01-前端轮询与日志排查.md
?? docx_feedback_images/
?? hf_ninfer_13gb.csv
?? hf_ninfer_scan.csv
?? hf_ninfer_scan.ps1
?? hf_ninfer_scan2.ps1
?? naiba/super_agent/domain.py
?? naiba/super_agent/domain_adapters.py
?? read_doc.py
?? read_doc_images.py
?? tests/test_super_agent_p3_http.py
?? tests/test_super_agent_p3_writer_novel.py
?? 第二次反馈修改.docx
?? 织书台第一次反馈.docx
?? 织书台第三次反馈.docx
```

以新对话实际 `git status --short` 为准；上面只是本轮快照。

不要直接执行：

```bash
git add .
git commit -am ...
```

如需提交，必须先按文件审查 diff，并由用户明确授权。

---

## 6. 新对话推荐执行顺序

### 第一步：重新审计，不重复 P4-0

读取：

```text
docs/plans/2026-11-01-Super-agent-P4-开始交接.md
docs/plans/2026-11-01-Super-agent-P4-阶段链未完成交接.md
docs/plans/2026-11-01-Super-agent-P3-to-P4-交接.md
```

然后运行：

```bash
git status --short
git diff -- naiba/storage/store.py naiba/super_agent/models.py naiba/super_agent/store.py naiba/super_agent/service.py naiba/super_agent/domain.py naiba/app.py public/js/28-super-agent.js
python -m unittest tests.test_super_agent_p3_http tests.test_super_agent_p3_writer_novel tests.test_super_agent_ui -q
```

### 第二步：先修后端 P4-1 contract

按顺序：

1. 更新 migration 测试到 v30；
2. 新增 `tests/test_super_agent_p4_chain.py`；
3. 修复原子 graph create；
4. 接入明确 chain body；
5. 证明 Writer/Novel 顺序、依赖、首节点 ready、其余 blocked；
6. 证明旧单 Node Thread 不变。

### 第三步：完成最小 promotion slice

先只保证 Writer：

```text
setup → outline
```

必须先通过：

- 成功只推进一次；
- 重复成功事件不重复创建/激活；
- promote 后 current_node_id 正确；
- 失败不推进；
- 旧 attempt 不推进；
- `stage_promoted` 事件可通过 after cursor 读取。

然后再扩展剩余 Writer/Novel 链。

### 第四步：接 Approval / retry / recovery

固定并测试：

- Novel approve → apply → promote；
- reject 不推进；
- apply 失败可恢复；
- cancel 只影响当前节点/按明确 Thread 语义；
- retry attempt 递增；
- interrupted 不自动重提；
- recovery 后重复调用不重复生成 executor/promote。

### 第五步：再做前端真实闭环

前端只消费后端 projection，不自行猜测阶段关系。补专项浏览器脚本并运行桌面/移动门禁。

---

## 7. 新对话启动话术

```text
读取 docs/plans/2026-11-01-Super-agent-P4-阶段链未完成交接.md，继续完成 P4 阶段链。
先按文档审计当前工作树，不要清理或提交无关文件，也不要重复修改已经收口的 P4-0 创建弹窗。
优先修复 v30 migration 测试和原子 graph create，真正接入 Writer/Novel chain body；新增 tests/test_super_agent_p4_chain.py，先完成 Writer setup→outline 的一次性 promote、current_node_id、stage_promoted 事件和幂等，再扩展完整链。
保留旧单 Node 的 request_id、attempt、executor_id、approval、cancel、interrupted/recovery 契约；最后补真实浏览器阶段链冒烟和桌面/移动验收。
所有测试结果必须按实际输出报告，不能把 P3 定向绿灯宣称为 P4 完成。
```
