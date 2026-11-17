# Super-agent P4 阶段链继续交接

- 日期：2026-11-01
- 用途：新对话继续当前工作，不作为 P4 完成证明。
- 提交状态：没有提交 commit；工作树中已有的无关修改和未跟踪文件必须保留。
- 必读：修改前先阅读 `项目维护说明（修改代码前必读）.md`，并以当前源码和测试为准。

## 1. 当前结论

本轮在上一份“阶段链未完成交接”的基础上继续完成了 P4 后端最小可运行切片：

- v30 迁移测试已更新并检查阶段链字段。
- Writer/Novel chain body 已接入；创建请求能一次生成完整 graph。
- Thread + graph 使用同一 SQLite 事务创建，重复 `chain_instance_id` 返回同一 Thread/Node graph。
- 首节点为 `ready`，后续节点为 `blocked`；依赖和 `promote_to` 在落库后都是真实 Node ID。
- 成功终态在同一事务内推进下一节点、更新 `current_node_id`、写入 `stage_promoted` 事件。
- Novel 首阶段的 Approval → apply → 下一阶段 ready 已有定向测试。
- Node/事件 HTTP DTO 已补阶段推进字段和安全摘要。

仍然不能宣称 P4 完成：真实阶段链浏览器闭环、前端 chain 创建入口、完整 Writer/Novel 多阶段控制矩阵、recovery/取消/重试与外部领域提交崩溃窗口仍未收口。

## 2. 本轮改动

### 2.1 存储和阶段推进

涉及 `naiba/super_agent/store.py`、`naiba/super_agent/service.py`：

- 新增确定性 `_stable_id()`，让稳定的 `chain_instance_id` 可重放同一 Thread 和 Node。
- `create_thread_chain()` 在一个 `BEGIN IMMEDIATE` 事务中创建 Thread、所有 Node、真实依赖和首节点游标；异常会回滚整条链。
- `create_chain()` 改为单事务插入，不再循环调用独立事务的 `create_node()`。
- `_insert_chain_nodes_db()` 固定校验 stage 白名单输入的顺序、连续 sequence、首节点 ready、后续 blocked、依赖和 promote 引用。
- `_promote_chain_node_db()` 是阶段推进的事务内唯一基础入口：校验 owner/thread/current node/attempt/status，推进目标、更新 Thread、写 `stage_promoted`，并用持久事件幂等键防重复。
- `append_event_and_apply()` 对 chain Node 的成功事件调用原子 promote；旧单 Node 继续使用原有 `promote_ready_nodes()`。
- Service 不再对成功事件做第二套独立阶段决策。

### 2.2 Domain chain create

涉及 `naiba/super_agent/domain.py`：

- 保留旧 body（`domain + book_id + stage + start_request`）的单 Node 语义。
- 仅当 body 明确包含 `chain`、`chain_mode`、`stages` 或 `stage_requests` 时进入 chain 模式。
- 支持 `writer_v1`：`setup → outline → chapter_plan → chapter_content`。
- 支持 `novel_v1`：`import → elements → outline → screenplay → review`。
- Writer canonical stage 映射到实际领域阶段：`setup_all`、`outline_all`、`split`、`chapter`。
- Novel canonical stage 映射到实际领域阶段：`elements`、`outline`、`script`、`storyboard`；`import` 当前按 `elements` 适配，需新对话确认是否要单独的导入阶段。
- 每个阶段保留最小 `domain_ref`、`start_request`、`apply_request`；Novel apply request 会自动补 `book_id`、实际 stage 和 `chapter_ids`。

### 2.3 DTO 和迁移测试

涉及 `naiba/app.py`、`tests/test_storage_migration.py`：

- Node DTO 新增 `promote_to`、`promotion_policy`。
- Event summary 白名单新增 `from_node_id`、`from_stage`、`to_node_id`、`to_stage`、`chain_instance_id`。
- Thread DTO 包含 `current_stage_key`；创建响应额外返回完整 `nodes` 列表。
- v30 测试断言 `chain_version`、`stage_key`、`sequence`、`promote_to`、`promotion_policy`、`chain_instance_id` 存在。

### 2.4 新增专项测试

新增 `tests/test_super_agent_p4_chain.py`，当前 6 个用例覆盖：

- Writer/Novel graph 顺序、依赖和 promote 关系。
- chain create 多 Node、ready/blocked 初始状态和稳定幂等重放。
- 非法 graph 的 Thread + Node 整体回滚。
- Writer setup 成功只推进一次、`current_node_id` 更新、`stage_promoted` 公开摘要和重复事件不重复推进。
- 失败和旧 attempt 不推进。
- Novel Approval → apply → 第一节点 succeeded、第二节点 ready。

## 3. 实际验证结果

以下命令使用项目根 `.venv\Scripts\python.exe`：

```text
python -m unittest tests.test_super_agent_p4_chain tests.test_super_agent_p3_http tests.test_super_agent_p3_writer_novel tests.test_super_agent_ui tests.test_storage_migration -q
Ran 43 tests
OK
```

附带输出中有既有迁移测试的 `disk full (simulated)` 日志，但测试退出码为 0。

```text
python -m compileall -q naiba/super_agent naiba/app.py naiba/storage/store.py
通过

node --check public/js/28-super-agent.js
通过

git diff --check
通过
```

Playwright 已用源码临时服务和 Microsoft Edge 跑 `verify/super_agent_controls_smoke.cjs`：

- 桌面 1280×900：全部通过。
- 移动 430×900：全部通过。
- 页面、创建弹窗、无横向溢出、零 pageerror、零 console.error、无 4xx/5xx 均通过。
- 该脚本验证的是 P4-0 控制面，不是完整阶段链闭环；阶段链专项浏览器门禁仍未新增。

## 4. 当前未完成项和风险

按优先级继续：

### P0：先审查当前实现

- 检查 `create_thread_chain()` 的部分重放场景：同一稳定 Thread 已存在但 graph 被外部破坏时，目前会报“阶段链缺失”；需要决定是否补偿创建还是明确失败。
- 确认 `chain_version` 的语义。当前它表示 chain schema/version，阶段推进不递增；Thread 的普通 `version` 才记录状态变化。
- 确认 Novel `import → elements` 的实际领域映射，避免 canonical key 与 `naiba/novel/prompts.py` 漂移。
- 检查 `_apply_event()`、`append_event_and_apply()` 和 direct `promote_chain_node()` 的恢复重放，确保不会产生重复 `stage_promoted`。

### P1：HTTP 和前端真实闭环

- 新增 `verify/super_agent_stage_chain_smoke.cjs` 或等价脚本；至少覆盖 Writer/Novel chain create、首阶段 ready、start running、成功推进、失败 retry、Novel approval approve/reject、事件增量 cursor、桌面/移动无溢出、零 pageerror/console.error、无 4xx/5xx、截图。
- 当前 `public/js/28-super-agent.js` 创建弹窗仍发送旧单阶段 body；它能展示后端返回的阶段链，但没有 chain 创建表单。需要决定 P4 是否新增 chain 选择/阶段链创建 UI，且保留旧单 Node 创建兼容。
- 真实浏览器要确认切换 Thread 会重置事件 cursor，刷新后从服务端重新建立 cursor，`stage_promoted` 不重复、不丢失。

### P2：控制和领域边界

- 完成 Writer 四阶段逐段推进、失败后 retry、取消只影响当前阶段。
- 完成 Novel reject 不推进、apply 失败可恢复、approve 重放不重复写回。
- 验证旧 attempt 的迟到领域任务结果不能推进新 attempt。
- 验证 interrupted/recovery 不自动重提领域任务；确认外部任务提交与 Node 落库崩溃窗口的幂等方案。
- 评估最终节点成功、Thread completed、artifact 引用和 `stage_promoted` 是否需要统一终态事件摘要。

## 5. 当前工作树边界

本轮没有清理、回滚或提交无关文件。继续时先执行：

```powershell
git status --short
```

必须保留之前交接列出的既有修改和未跟踪文件；尤其不要删除 `NUL`、文档、扫描脚本、docx 反馈文件、`naiba/super_agent/domain.py`、`naiba/super_agent/domain_adapters.py`、P3 测试，以及本轮新增的 `tests/test_super_agent_p4_chain.py` 和本交接文档。不要执行 `git add .`、`git commit -am` 或整体清理。

## 6. 新对话推荐顺序

1. 阅读项目维护说明、本文件和 `docs/plans/2026-11-01-Super-agent-P4-阶段链未完成交接.md`。
2. 查看当前 diff，先复跑 43 个定向测试、compileall、node check、diff check。
3. 先补 direct promotion、部分重放、最终节点和 recovery 的确定性测试，再动前端。
4. 决定 Novel `import` 的 canonical/实际映射，并固定契约。
5. 新增阶段链浏览器冒烟，使用隔离源码服务；不要把 P4-0 控制面冒烟当作阶段链证据。
6. 若新增前端行为，最后必须再次运行 Playwright，桌面和移动都要覆盖。

## 7. 可直接复制的新对话启动话术

```text
读取 docs/plans/2026-11-01-Super-agent-P4-阶段链继续交接.md，继续当前 P4 阶段链工作。
先阅读项目维护说明和上一份阶段链未完成交接，审计当前工作树，不要清理、回滚或提交无关文件，也不要重复修改已经收口的 P4-0 弹窗。
先复跑交接文档中的 43 个定向测试和静态检查；然后审查 create_thread_chain 的重放、最终节点、recovery、旧 attempt fencing 与 direct promote 事务。
确认 Novel import/elements 的 canonical 映射后，新增真实阶段链浏览器冒烟，覆盖 Writer/Novel 创建、ready/blocked、start、成功推进、失败 retry、Approval approve/reject、事件增量 cursor、桌面/移动和零 pageerror/console.error。
前端改动最后必须用 Playwright 验收；报告必须区分已实现、已测试、未完成和环境/基线问题，不能把 P3 或 P4-0 绿灯宣称为 P4 完成。
```
