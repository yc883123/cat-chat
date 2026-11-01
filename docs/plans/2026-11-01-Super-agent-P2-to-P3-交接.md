# Super-agent V1：P2 → P3 执行交接

- 日期：2026-11-01
- 当前阶段：**P2 第一切片已收口；下一阶段进入 P3 首个深度闭环**
- 当前分支：`feat/super-agent-p0-to-p1-handoff`
- P1 基线：`6fe64fff feat(super-agent): hand off P1 to P2`
- P2/KVMem 提交：`9e119d7 feat(super-agent): close P2 and add KVMem controls`
- 本文用途：新对话直接从 P3 开始，不重新设计 P0/P1/P2，不把 P2 只读页误当成完整执行闭环。

## 一、当前交付判定

### 1. P0/P1/P2 状态

- P0 Thread / Node / Event / Artifact / Approval 契约已完成并提交。
- P1 Adapter / Fast Path Router 已完成并提交。
- P2 第一切片已完成：任务总管入口、Thread 列表/详情只读展示、最小读取 API、owner 隔离和摘要化展示。
- P2 顶层视图状态已收口为统一 `setAppView("chat" | "studio" | "super-agent")`。
- `#tasksDialog` 仍保留为旧 Chat/Job 异步任务快捷面板；任务总管是显式跳转入口，不会自动接管旧任务。

### 2. KVMem 当前交付

本次提交同时完成 KVMem 第一版安全接入：

- 新增 KVMem 本地供应商预设。
- 普通 `llama_cpp` 仍然只是外部 OpenAI-compatible 服务连接，不获得进程控制。
- KVMem 控制台默认：`http://127.0.0.1:18199`。
- KVMem 推理 API 默认：`http://127.0.0.1:18200/v1`。
- 只允许回环控制台地址：`127.0.0.1`、`localhost`、`::1`。
- 新增 `naiba/kvmem.py`，通过 KVMem controller API 获取临时 token 并调用 start/stop。
- 不扫描端口、不反查 PID、不调用 `taskkill`、不按进程名杀进程。
- Cat Chat 不自动启动/关闭 KVMem 控制台，不在退出或切换供应商时自动停止服务。
- KVMem 启停控制放在对话顶栏 Agent 下拉框右侧。
- 只有当前选中的 provider 带 `service_manager=kvmem` 时显示；普通 llama.cpp、Ollama、LM Studio 和在线供应商隐藏。
- 停止前由后端检查仍使用该 provider 的活动任务；有活动任务时拒绝停止。

## 二、验证证据

### 已通过

- P2、KVMem、供应商、本地模型、Chat/Studio/移动端专项与回归：**169 项通过**。
- KVMem 配置专项：3 项通过。
- 最终 P2/KVMem 专项：18 项通过。
- Python `compileall`：通过。
- 前端 `node --check`：通过。
- `git diff --check`：通过。
- 真实 `NaibaChatApp` facade 装配测试已加入 `tests/test_super_agent_http.py`。
- KVMem fake controller 覆盖：回环 URL、临时 token、启停协议、状态读取、错误边界和禁止进程终止原语。

### 尚未完成的环境验证

- `verify/super_agent_smoke.cjs` 已提交，但当前环境缺少 Playwright Chromium executable，实际浏览器冒烟未运行。
- 未对用户当前正在使用的真实 KVMem 服务执行 start/stop；真实 KVMem 端到端验证必须在用户明确确认控制台可测试且允许启停后单独进行。

## 三、P2 已知边界，不要在 P3 误扩大

P2 任务总管仍然是只读第一切片，尚未实现：

- 创建 Thread；
- 暂停、继续、取消、重试、启动 Node；
- Approval 批准/拒绝；
- 普通 Chat / Studio 自动关联 Thread；
- Writer / Novel 深度阶段接入；
- Artifact 回链到五个工作台；
- Slow Path LLM Router；
- 自然语言任务拆解和自主循环。

这些不是 P2 第一切片的遗漏；P1→P2 交接明确将控制动作和创建任务放在后续切片，P3 的重点是首个垂直深度闭环。

## 四、P3 目标：织书台 / 拆书匠首个深度闭环

P3 不应先把五个工作台全部接入，也不应先做通用自然语言 Router。推荐先完成一个可恢复、可审批、可回写保护的垂直闭环：

```text
导入原文
  → 整理设定
  → 生成大纲
  → 大纲复核
  → 拆章批次
  → 章节生成
  → 一致性检查
```

### P3 第一切片推荐范围

1. 从任务总管显式创建一个“小说改编” Thread；
2. 以 `novel_stage` / `writer_pipeline` 等明确 `executor_type` 创建阶段 Node；
3. 织书台 / 拆书匠产生结构化阶段开始、完成、失败事件；
4. 阶段产物登记为 `super_artifacts`，只展示引用和摘要；
5. 需要人工确认的阶段使用 P0 Approval；
6. 阶段完成后确定性推进下一个 Node，继续走 P1 Fast Path；
7. 暂停、继续、取消、重试和重启恢复必须复用 P0/P1 契约；
8. 从任务总管跳回对应 Writer/Novel 详情页；
9. 原有独立织书台 / 拆书匠流程继续可用，不强制创建 Thread。

## 五、P3 明确不做

- 不修改或重写 Writer / Novel 的既有正文、章节、版本状态机；
- 不复制 Writer / Novel 数据库；
- 不把完整正文、完整章节、完整提示词库或内部推理放进任务总管；
- 不自动监听所有工作台操作；
- 不把每个历史任务自动导入 Thread；
- 不接入真实付费供应商作为 P3 前置条件；
- 不进入 Slow Path LLM Router；
- 不实现无限自主循环；
- 不把 ComfyUI、提词库、打标器一起扩成完整可恢复执行器。

## 六、P3 实施前必须先核对的代码

### Writer / Novel 领域服务

- `naiba/writer/` 与 `naiba/novel/` 的现有 API facade、任务状态和版本写回接口；
- `public/js/25-novel.js`、`public/js/27-writer.js` 的现有页面状态和轮询边界；
- `tests/` 中 Writer / Novel 的保存、版本、批次、恢复和回归测试；
- 现有 Studio/Novel/Writer 任务记录与 `conversation_id` / `model_key` 关联方式。

### Super-agent 契约

复用，不重写：

- `SuperAgentService.create_thread()` / `create_node()`；
- `emit_event()` 与 P0 event apply；
- `create_approval()` / `resolve_approval()`；
- Artifact 登记和 owner/thread/node 关系校验；
- `FastPathRouter` 的显式 executor_type 路由；
- attempt fencing、request_id、幂等和 recovery。

P3 新增 Adapter 时必须：

- 明确 `executor_type`；
- 不对未知 executor_type fallback 到 Chat 或模型；
- 不绕过 P0 owner、attempt、snapshot 和幂等契约；
- 不让刷新/轮询调用 LLM；
- 不把一次性内存动作缓存当成状态真源。

## 七、P3 推荐文件范围

实际文件必须先只读核对后确定，推荐从以下范围开始：

### 后端

- `naiba/super_agent/adapters.py`
- `naiba/super_agent/router.py`
- `naiba/super_agent/service.py`
- `naiba/super_agent/store.py`
- `naiba/app.py`
- Writer / Novel 对应领域服务文件

### 前端

- `public/js/25-novel.js`
- `public/js/27-writer.js`
- `public/js/28-super-agent.js`
- `public/index.html`（只加最小显式创建/关联/回链入口）
- `public/styles.css`

不要直接扩大到五个工作台所有页面；P3 首切片只围绕 Writer/Novel 垂直闭环。

### 测试

建议新增：

- `tests/test_super_agent_p3_writer_novel.py`
- Writer/Novel 与 Thread 事件映射测试；
- Artifact/Approval/版本写回保护测试；
- pause/resume/cancel/retry/recovery 回归；
- 旧 Writer/Novel 独立流程回归；
- 真实 HTTP 只读/控制集成测试；
- 浏览器级最小 P3 冒烟。

## 八、P3 验收门槛

至少证明：

1. 用户不使用任务总管时，Writer/Novel 原流程仍可独立完成；
2. 只有用户显式创建或关联的任务进入 Thread；
3. 阶段状态由领域服务和 Super-agent Service 的真实状态产生，前端不自行推断；
4. 事件重复、晚到事件和旧 attempt 不会重复写回；
5. Approval 只能解析一次，重复请求幂等；
6. Artifact 只展示引用/摘要，不复制正文；
7. 暂停、继续、取消、重试、重启恢复不会重复生成、重复计费或覆盖错误版本；
8. 关键版本写回前有明确审批/保护；
9. 任务总管与 Writer/Novel 能互相回链；
10. Fast Path 确定性推进不调用 Slow Path LLM Router；
11. P0/P1、Chat/Job/Run、Writer/Novel 既有回归不退化。

## 九、新对话开工指令

新对话第一步：

1. 读取本文；
2. 执行 `git status`、`git log -2 --oneline --decorate`，确认 P2/KVMem 提交 `9e119d7` 已存在；
3. 不处理工作区原有无关未跟踪文件；
4. 只读核对 Writer/Novel 领域服务、现有任务状态、版本写回、P1 Adapter/Router 和现有测试；
5. 提出 P3 第一切片最小文件清单与测试清单；
6. 先不要接真实付费供应商，不进入 Slow Path；
7. 第一切片只实现 Writer/Novel 显式关联、阶段 Node/事件/Artifact/Approval 和可恢复闭环。

可直接粘贴的新对话开工指令：

> 读取 `docs/plans/2026-11-01-Super-agent-P2-to-P3-交接.md`，确认 HEAD 已包含 `9e119d7 feat(super-agent): close P2 and add KVMem controls`，从 P3 首个深度闭环开始。先只读核对 Writer/Novel 领域服务、版本写回、现有任务状态和 P1 Adapter/Fast Path 契约，提出 P3 第一切片最小文件清单；只做织书台/拆书匠显式关联、阶段 Node/Event/Artifact/Approval 和可恢复闭环，不改旧独立流程、不接真实供应商、不进入 Slow Path。

## 十、工作区纪律

以下是本次 P2/KVMem 提交之外的原有工作区文件，不属于交接：

- `NUL`
- `docs/design/2026-11-01-任务总管-V1-预览.html`
- `docs/plans/2026-11-01-Super-agent工作台总计划.md`
- `docs/调研报告-2026-11-01-前端轮询与日志排查.md`
- `docx_feedback_images/`
- `hf_ninfer_13gb.csv`
- `hf_ninfer_scan.csv`
- `hf_ninfer_scan.ps1`
- `hf_ninfer_scan2.ps1`
- `read_doc.py`
- `read_doc_images.py`
- `第二次反馈修改.docx`
- `织书台第一次反馈.docx`
- `织书台第三次反馈.docx`

新对话不得将这些文件带入 P3 提交。
