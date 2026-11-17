# Super-agent P3 → P4 交接文档

- 日期：2026-11-01
- 分支：`feat/super-agent-p0-to-p1-handoff`
- 当前状态：P3 核心控制面与领域桥接已实现，P3 定向验收通过；前端任务总管截图仍有渲染/文案收口问题，尚未提交 commit。
- 下一阶段：P4 先收口前端视觉与真实浏览器验收，再扩展阶段链与完整 P4 能力。

## 1. P3 已完成内容

### 1.1 领域桥接

相关文件：

- `naiba/super_agent/domain.py`
- `naiba/super_agent/domain_adapters.py`
- `naiba/app.py`
- `naiba/http.py`

已实现：

- Writer / Novel 领域 Thread、Node 创建。
- Writer start / sync / cancel / retry。
- Novel start / sync / cancel / retry。
- Novel 结果进入 Approval 后再 apply。
- Approval approve / reject。
- domain_ref、attempt、executor_id、artifact 引用回链。
- 创建时校验 `start_request.model_name`，避免创建出必然无法启动的 Node。

### 1.2 Writer retry 状态一致性

Writer pipeline 的 `api_retry_pipeline()` 已经完成领域侧 attempt+1 和一次新任务提交。

Super-agent retry 不再继续调用 start，避免：

```text
领域任务已重提
→ Super-agent Node 再次 retry
→ 再次 start
→ 领域接口 409
→ 领域任务实际运行，但 Node 状态分叉
```

现在的逻辑是：

```text
api_retry_pipeline() 一次提交
→ 读取权威 pipeline
→ 读取 current_task_id
→ Super-agent attempt +1
→ 绑定 executor_id/current_task_id
→ 记录 run_started
```

### 1.3 interrupted / recovery

新增事件：

- `run_interrupted`

已修改：

- `naiba/super_agent/models.py`
- `naiba/super_agent/service.py`
- `naiba/super_agent/store.py`
- `naiba/super_agent/domain_adapters.py`

语义：

```text
running/stopping + 服务重启
→ Node interrupted
→ active Thread needs_recovery
→ 只产生一个 app_restarted 事件
→ 不自动重新提交领域任务
→ 用户显式 retry/resume 才重新提交
```

### 1.4 action 安全 DTO

`NaibaChatApp.api_super_agent_action()` 不再直接返回 `AdapterResult.as_dict()`。

公开结果只保留：

- `executor_type`
- `executor_id`
- `status`
- `attempt`
- `node_status`
- `output_refs`
- `artifact_refs`
- 摘要化 `domain_ref`
- `approval_id`

禁止返回：

- `checkpoint`
- `start_request`
- `_adapter_commands`
- `raw`
- 完整领域任务/流水线对象
- 完整 prompt 或内部请求快照

### 1.5 持久幂等

Writer / Novel start、cancel、retry 使用 Node checkpoint 中的 `_adapter_commands` 记录结果，不只依赖进程内 `FastPathRouter` 缓存。

重复相同 `request_id` 不应重复：

- 提交领域任务；
- 取消领域任务；
- 增加 Super-agent attempt；
- 写回版本；
- 创建重复 artifact。

## 2. 已通过的测试证据

### 2.1 P3 定向测试

最近一次定向门禁：

```text
Ran 93 tests
OK
```

覆盖组合：

```text
tests.test_super_agent_p3_http
tests.test_super_agent_p3_writer_novel
tests.test_super_agent_http
tests.test_super_agent_p0
tests.test_super_agent_p0_boundaries
tests.test_super_agent_p1_adapters
tests.test_super_agent_ui
tests.test_writer_pipeline
tests.test_novel_phase4
```

### 2.2 静态门禁

已通过：

```text
node --check public/js/25-novel.js
node --check public/js/27-writer.js
node --check public/js/28-super-agent.js
python -m compileall -q naiba/super_agent naiba/app.py
git diff --check
```

### 2.3 HTTP 验收

新增测试文件：

- `tests/test_super_agent_p3_http.py`

已覆盖：

- 创建 Writer / Novel 任务；
- 缺少 model_name 的创建拒绝；
- start；
- cancel；
- retry；
- retry request_id 幂等；
- approval approve；
- approval reject；
- reject 不写回领域版本；
- approve 重放不重复写回；
- action DTO 脱敏；
- recovery / interrupted / needs_recovery；
- recovery 幂等。

最近一次该测试文件结果：

```text
Ran 5 tests
OK
```

### 2.4 浏览器黑盒证据

使用独立源码实例：

```text
http://127.0.0.1:8798/
```

隔离目录：

```text
verify/_tmp_p3_browser
```

真实页面操作已验证：

1. 页面内创建 Writer 任务；
2. 页面内创建 Novel 任务；
3. Writer Node 从“就绪”变为“运行中”；
4. 事件时间线出现 `run_started`；
5. 任务失败后页面显示“失败”和安全错误摘要；
6. 页面点击“重试”后 attempt 从 0 变为 1，再次进入“运行中”；
7. Writer “回到工作台”切到 Studio → Writer，并打开正确作品；
8. Novel “回到工作台”切到 Studio → Novel，并打开正确作品；
9. Novel / Writer 页面都未依赖浏览器原生 prompt 创建任务。

浏览器截图/证据路径（由浏览器工具产生）：

- `C:\Users\admin\.zcode\cli\artifacts\sess_f029a561-fa1c-4e38-aab0-7bc5de559368\call_05db184a154a4192b4245c2ef30c1ca0-tool-result-775707c6-5f89-4a95-8f6a-d8a8b0b5e001.png`
- `C:\Users\admin\.zcode\cli\artifacts\sess_f029a561-fa1c-4e38-aab0-7bc5de559368\call_8992ff8d41064b91b95fb77cd9c972b9-tool-result-8d7395c9-a97f-43ed-af9c-8394cf438c1d.png`

注意：浏览器运行时对首次欢迎弹窗和视图按钮曾出现 IAB DOM-CUA 状态不同步，但任务创建、控制、回链的关键页面状态已通过 DOM 快照验证；P4 应改成可重复的自动化浏览器冒烟脚本，减少手动 IAB 依赖。

## 3. 当前明确未收口问题

### 3.1 任务总管顶部“只读查看”文案错误

位置：

- `public/index.html:1678-1680`
- `public/styles.css:5957-5970`

当前仍有：

```html
<span class="super-agent-readonly">只读查看</span>
```

但页面已经支持：

- 新建领域任务；
- 开始；
- 同步；
- 取消；
- 重试；
- 审批批准/拒绝。

这会造成截图与真实功能不一致。P4 应改成用户视角文案，例如：

```text
任务控制
```

或：

```text
查看与控制
```

不要继续使用“只读查看”。

### 3.2 创建对话框复用了不完整的 Writer dialog class

位置：

- `public/index.html:1711-1722`
- `public/styles.css:5738-5747`

当前 HTML 使用：

```html
<dialog id="superAgentCreateDialog" class="writer-dialog">
  <form method="dialog" id="superAgentCreateForm">
    <div class="writer-dialog-head">...</div>
    <label>...</label>
    ...
  </form>
</dialog>
```

但现有 CSS 主要定义的是：

```css
.writer-dialog-form { ... }
.writer-dialog-form h3 { ... }
.writer-dialog-actions { ... }
```

创建表单没有使用 `.writer-dialog-form`，且 `.writer-dialog-head` 没有对应样式定义。结果可能导致截图中：

- 标题/关闭按钮未正确横向排布；
- label/input 未形成稳定的纵向表单布局；
- 表单内间距、按钮区和错误提示显示不一致；
- 移动端宽度和滚动行为异常。

P4 第一修复应二选一：

1. 给创建表单补上已有骨架 class：

```html
<form method="dialog" class="writer-dialog-form" id="superAgentCreateForm">
```

并将 head 改用已有的 `.writer-manage-head` 或补齐 `.writer-dialog-head` 样式；

2. 为 `.super-agent-create-dialog` 单独定义完整布局样式，避免继续借用 Writer 对话框样式。

推荐方案 1，改动更小。

### 3.3 页面注释与实际能力过时

位置：

- `public/js/28-super-agent.js:1-3`
- `public/styles.css:5957`
- 相关 P2 只读描述。

当前仍有“P2 第一切片（只读）”的注释和 CSS 注释，但功能已经进入 P3 控制面。P4 应同步更新注释，避免后续维护者误判边界。

### 3.4 取消存在真实竞态，需要 UX 处理

浏览器实测：点击 Writer “取消”时，后台任务已在同一时间窗口结束，接口返回：

```text
当前生成已结束，无需取消
```

后端行为是诚实的，但前端当前把它显示为“读取失败”，视觉上像系统错误。

P4 应区分：

- 真实失败；
- 任务已自然结束，取消已无必要；
- 用户取消成功；
- 取消请求与终态竞争。

推荐把 409/已结束类结果显示为非错误状态提示，并自动刷新详情。

## 4. 完整测试集现状

最近一次完整运行：

```text
python -m unittest discover -s tests -q
```

结果：

```text
Ran 3731 tests
FAILED (failures=9, errors=31, skipped=9)
```

剩余失败不是 P3 代码引起，主要分为：

### 4.1 PyAV 未安装

错误集中在：

- `tests/test_video.py`
- `tests/test_video_exact.py`
- `naiba/video.py:83`

原因：

```python
import av
```

当前环境没有安装 PyAV，因此视频抽帧、精确范围、缓存和媒体采集测试无法执行。

### 4.2 pywebview 运行时不可用

错误集中在：

- `tests/test_file_action_bridge.py`

这些测试需要真实 pywebview 窗口对象，当前测试环境不提供。

### 4.3 不能对外宣称全仓库全绿

P3 定向门禁是绿的，但全仓库测试仍是：

```text
3731 tests: 9 failures, 31 errors, 9 skipped
```

P4 继续工作时必须保留这一区分。

## 5. 当前工作树与提交状态

本次 P3 修复尚未提交 commit。当前关键变更包括：

```text
M naiba/app.py
M naiba/http.py
M naiba/super_agent/models.py
M naiba/super_agent/service.py
M naiba/super_agent/store.py
M public/index.html
M public/js/25-novel.js
M public/js/27-writer.js
M public/js/28-super-agent.js
M public/styles.css
M tests/test_super_agent_ui.py
?? naiba/super_agent/domain.py
?? naiba/super_agent/domain_adapters.py
?? tests/test_super_agent_p3_http.py
?? tests/test_super_agent_p3_writer_novel.py
```

工作树里还有用户原有的其他未跟踪/未提交文件，不要在 P4 中擅自清理或提交无关文件。

## 6. P4 建议目标

### P4-0：先收口截图渲染

优先级最高：

1. 修正“只读查看”文案；
2. 修正创建对话框布局；
3. 增加桌面/移动端截图验收；
4. 检查按钮、状态 badge、错误提示、Thread 三栏在不同宽度下是否溢出；
5. 把“读取失败”与“取消竞态”提示分开。

### P4-1：控制面 UX 完整化

- start / sync / cancel / retry 按钮按状态显示；
- 控制按钮 loading/disabled 防重复点击；
- action 失败后保留当前详情，不清空页面；
- 失败后自动刷新一次服务端状态；
- approval 操作显示处理中并防重复提交；
- 错误提示使用用户视角文案。

### P4-2：阶段链

当前 `DomainCoordinator.create_thread()` 只创建一个 Node，不是完整阶段链。

P4 需要设计并实现：

- Writer：设定 → 大纲 → 拆章/章节 → 正文；
- Novel：导入 → 设定/要素 → 大纲 → 剧本 → 复核/产物；
- 阶段完成后自动 promote 下一 Node；
- 每个阶段仍保留 approval、cancel、retry、attempt fencing。

不要把完整阶段链偷偷塞回 P3 修复；应单独设计 P4 的 Node graph / coordinator contract。

### P4-3：真实浏览器自动化

将当前手动 IAB 验收固化为：

- `verify/super_agent_controls_smoke.cjs` 或等价脚本；
- 使用隔离 data/config；
- 覆盖创建、start、sync、cancel、retry、approval、回链；
- 桌面和移动宽度；
- 保存截图和页面错误；
- 如果 Chromium/Playwright 环境不可用，要明确记录阻断，不用静态 source test 代替。

## 7. P4 开始前不要做的事

- 不要重新实现已经通过的 Writer retry 单提交逻辑；
- 不要把 action 响应改回返回 checkpoint/raw；
- 不要删除 P3 POST 控制接口以恢复旧 P2 只读测试；
- 不要把 interrupted 映射回 cancelled；
- 不要为了“全绿”删除视频/pywebview 测试；
- 不要清理用户原有未跟踪文件；
- 不要直接提交整个工作树，先按文件审查 diff。

## 8. 新对话第一步验收清单

新对话开始后应先执行：

```text
1. 阅读本交接文档。
2. 读取当前 git status 和相关 diff。
3. 重现/读取任务总管截图或启动隔离源码实例。
4. 先修复任务总管前端渲染问题，不先扩展阶段链。
5. 运行：
   - python -m unittest tests.test_super_agent_p3_http tests.test_super_agent_p3_writer_novel tests.test_super_agent_ui -q
   - node --check public/js/28-super-agent.js
6. 通过浏览器截图确认桌面与移动布局。
7. 再提出 P4 阶段链方案。
```

## 9. 推荐的新对话启动话术

```text
读取 docs/plans/2026-11-01-Super-agent-P3-to-P4-交接.md，开始 P4。
先不要扩展阶段链，先处理交接文档中记录的任务总管前端渲染问题：
1. 把“只读查看”改成与实际控制能力一致的用户文案；
2. 修正新建领域任务对话框的表单布局、标题/关闭按钮、按钮区、错误提示和移动端显示；
3. 运行 P3 定向测试、node --check 和真实浏览器桌面/移动截图验收；
4. 不要清理或提交工作树中无关文件；
5. 渲染问题收口并验收后，再给出 P4 阶段链设计和实施计划。
```
