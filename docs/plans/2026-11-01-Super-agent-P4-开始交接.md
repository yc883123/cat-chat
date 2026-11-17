# Super-agent P4 开始交接文档

- 日期：2026-11-01
- 当前分支：`feat/super-agent-p0-to-p1-handoff`
- 上一份交接：`docs/plans/2026-11-01-Super-agent-P3-to-P4-交接.md`
- 当前阶段：P4-0 前端渲染与浏览器验收已收口，准备在新对话进入 P4 阶段链设计与实施
- 提交状态：本轮未提交 commit

## 1. 本轮已完成：P4-0 渲染收口

### 1.1 任务总管能力文案

文件：

- `public/index.html`
- `public/styles.css`
- `public/js/28-super-agent.js`

已完成：

- 顶栏“只读查看”改为“查看与控制”。
- CSS class 从 `.super-agent-readonly` 改为 `.super-agent-capability`。
- 更新任务总管 P2/只读注释为控制面语义。
- 移动端继续隐藏能力 badge，避免顶栏横向溢出。

任务总管当前实际支持：

- 新建领域任务；
- 开始；
- 同步；
- 取消；
- 重试；
- 审批批准/拒绝；
- 回到 Writer / Novel 工作台。

### 1.2 新建领域任务弹窗

文件：

- `public/index.html`
- `public/styles.css`
- `public/js/28-super-agent.js`

已完成：

- `#superAgentCreateForm` 接入 `.writer-dialog-form`。
- 标题和关闭按钮改为 `.writer-manage-head` 横向布局。
- 使用 `h3#superAgentCreateTitle` 作为弹窗标题。
- 使用 `aria-labelledby="superAgentCreateTitle"`。
- 错误提示 `#superAgentCreateError` 增加 `role="alert"` 和 `aria-live="polite"`。
- 增加 `.super-agent-create-dialog`、`.super-agent-create-form` 专用样式。
- 表单内容支持纵向滚动，按钮区不被长内容推出弹窗。
- 移动端弹窗使用 `100vw × 100dvh` 全屏布局，取消边框和圆角。
- 使用安全区 padding，避免刘海/底部手势区遮挡。
- 打开弹窗、切换领域、修改字段、重新提交时清理过期错误。
- 保留原有创建 payload 和 `method="dialog"` 取消/关闭行为。
- 没有使用 `window.prompt`。

### 1.3 控制状态提示

文件：

- `public/js/28-super-agent.js`

已完成：

- `viewState` 增加 `actionMessage`，与读取错误 `error` 分开。
- 读取失败仍显示：`读取失败：...`。
- 控制操作期间显示：`正在处理…`。
- 普通控制失败显示：`操作失败：...`。
- 取消请求与领域任务自然结束竞争时，将 409 或“当前生成已结束，无需取消”显示为：

  ```text
  任务已结束，无需取消
  ```

- 取消竞态不再标记为红色读取错误，并会重新读取当前详情。
- 成功重新加载详情后清除旧动作提示。
- start/sync/cancel/retry/approval 开始前清理旧错误状态。

## 2. 新增浏览器验收脚本

文件：

- `verify/super_agent_controls_smoke.cjs`

脚本使用：

- 系统 Microsoft Edge；
- Playwright；
- 隔离源码实例；
- `NAIBA_SMOKE_BASE` 指定目标地址；
- `NAIBA_SMOKE_SHOTS` 指定截图输出目录。

覆盖内容：

- 桌面视口：`1280 × 900`；
- 移动视口：`430 × 900`；
- 任务总管视图可见；
- 创建弹窗打开；
- 标题与关闭按钮同一横向标题区；
- 标题和关闭按钮不重叠；
- 按钮区仍在弹窗内；
- 表单内容可滚动且不越界；
- 空表单错误提示可见且位于按钮区之前；
- 移动端全屏弹窗；
- 无横向溢出；
- 零 pageerror；
- 零 console.error；
- 无 4xx/5xx 响应；
- 保存桌面/移动截图。

推荐运行方式：

```bash
# 先准备独立隔离根目录和空闲端口
mkdir -p verify/_tmp_super_agent_controls
NAIBA_TMP_ROOT="$PWD/verify/_tmp_super_agent_controls" \\
NAIBA_TMP_PORT=8798 \\
python verify/_serve_tmp.py
```

另开终端运行：

```bash
NAIBA_SMOKE_BASE=http://127.0.0.1:8798 \\
NODE_PATH="$PWD/node_modules" \\
node verify/super_agent_controls_smoke.cjs
```

当前验收结果：

```text
桌面：全部通过
移动：全部通过
结果：全部通过
```

截图：

- `verify/super_agent_controls_shots/super-agent-create-desktop.png`
- `verify/super_agent_controls_shots/super-agent-create-mobile.png`

注意：源码实例启动时如果 8798/8799 已被其他实例占用，应先换空闲端口；不要杀掉不属于本次验收的服务。

## 3. 静态契约与测试结果

### 3.1 定向测试

已通过：

```bash
python -m unittest tests.test_super_agent_p3_http tests.test_super_agent_p3_writer_novel tests.test_super_agent_ui -q
```

结果：

```text
Ran 17 tests
OK
```

### 3.2 JavaScript 语法

已通过：

```bash
node --check public/js/28-super-agent.js
node --check public/js/25-novel.js
node --check public/js/27-writer.js
node --check verify/super_agent_controls_smoke.cjs
```

### 3.3 Python 与 whitespace

已通过：

```bash
python -m compileall -q naiba/super_agent naiba/app.py
git diff --check
```

### 3.4 静态 UI 契约

文件：

- `tests/test_super_agent_ui.py`

已增加断言：

- 不再出现“只读查看”；
- 创建表单使用 `.writer-dialog-form`；
- 使用 `.writer-manage-head`；
- 存在 `aria-live="polite"`；
- 存在取消竞态用户提示；
- 存在普通操作失败提示；
- 存在移动端全屏弹窗规则。

## 4. 当前工作树边界

本轮没有清理、回滚或提交无关文件。

当前工作树中已有的其他修改/未跟踪文件必须保留，不能擅自清理：

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
?? NUL
?? docs/design/2026-11-01-任务总管-V1-预览.html
?? docs/plans/2026-11-01-Super-agent-P3-to-P4-交接.md
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

不要直接提交整个工作树。后续如需提交，先按文件审查 diff，再由用户明确授权提交。

## 5. 新对话开始 P4 的第一步

新对话应先读取本文件和上一份 P3→P4 交接：

```text
读取：
- docs/plans/2026-11-01-Super-agent-P4-开始交接.md
- docs/plans/2026-11-01-Super-agent-P3-to-P4-交接.md
```

然后执行：

1. 读取 `git status --short` 和以下相关 diff：
   - `naiba/super_agent/domain.py`
   - `naiba/super_agent/domain_adapters.py`
   - `naiba/super_agent/models.py`
   - `naiba/super_agent/service.py`
   - `naiba/super_agent/store.py`
   - `naiba/app.py`
   - `naiba/http.py`
   - `public/js/28-super-agent.js`
2. 确认 P4-0 已有测试证据，不重复修复已经收口的弹窗渲染问题。
3. 先设计并评审阶段链 graph/coordinator contract，不直接把完整阶段链塞入现有单 Node 创建逻辑。
4. 设计通过后，再从只读 graph 投影和 graph builder 开始实现。
5. 每一步都保留现有 P3 的幂等、attempt、executor、request_id、approval、interrupted/recovery 契约。

## 6. P4 阶段链目标

### 6.1 Writer

```text
设定 → 大纲 → 拆章/章节 → 正文
```

建议节点：

- `setup`
- `outline`
- `chapter_plan`
- `chapter_content`

### 6.2 Novel

```text
导入 → 设定/要素 → 大纲 → 剧本 → 复核/产物
```

建议节点：

- `import`
- `elements`
- `outline`
- `screenplay`
- `review`

### 6.3 统一 Thread/Node 扩展方向

Thread 侧建议增加：

- `chain_version`
- `current_node_id`
- `needs_recovery`（沿用现有语义）

Node 侧建议增加：

- `stage_key`
- `sequence`
- `promote_to`
- `promotion_policy`
- `chain_instance_id`

不要破坏现有：

- `attempt`
- `executor_id`
- `request_id` 持久幂等；
- approval；
- cancel；
- interrupted/recovery；
- 安全 action DTO。

## 7. P4 推荐实施拆分

### P4-1：graph model + 只读投影

- 增加 `chain_version` 和阶段字段；
- 实现 Writer/Novel graph builder；
- 第一个节点 `ready`，其余节点 `blocked`；
- 暂不自动推进；
- 增加 graph 顺序、依赖和初始状态测试。

### P4-2：单次 promote

优先从 Writer 的短链开始：

```text
setup → outline
```

要求：

- 前一节点成功后只 promote 一次；
- 重复成功事件不重复创建/激活节点；
- `current_node_id` 持久一致；
- 失败不自动推进；
- 旧单 Node Thread 不受影响。

### P4-3：审批与多阶段控制

- 接入 Novel approval 节点；
- approve 后 promote；
- reject 后停留在当前阶段或进入可重试状态；
- 验证 cancel/retry/attempt fencing；
- 验证 interrupted/recovery 不自动重提。

### P4-4：完整阶段链

- Writer 四阶段；
- Novel 五阶段；
- 统一 output/artifact refs；
- 阶段错误、重试和恢复矩阵。

### P4-5：前端阶段链展示

- 当前节点高亮；
- blocked/ready/running/waiting/failed 状态；
- 阶段推进事件；
- 节点级控制；
- 当前节点审批和产物关联；
- 桌面/移动浏览器回归。

## 8. P4 必须保留的测试门禁

```text
graph builder：
- Writer 节点顺序正确
- Novel 节点顺序正确
- dependencies 正确
- 初始 blocked/ready 正确

promotion：
- 成功只推进一次
- 重复事件不重复创建 Node
- promote 后 current_node_id 正确
- 失败不自动推进
- reject 不推进

control：
- cancel 只影响当前节点
- retry attempt 递增
- 旧 attempt 结果被 fencing
- request_id 幂等
- interrupted 不自动重提

compatibility：
- 旧单 Node Thread 继续可用
- 新旧 Thread 投影格式稳定

browser：
- Writer/Novel 创建
- 第一阶段 ready
- start 后 running
- 失败后 retry
- approval approve/reject
- 阶段推进
- 回到 Writer/Novel 工作台
- 桌面/移动无溢出
- 零 pageerror/console.error
```

## 9. 新对话启动话术

```text
读取 docs/plans/2026-11-01-Super-agent-P4-开始交接.md，开始 P4。
先不要修改已经收口的任务总管弹窗渲染，先基于现有 P3 单 Node 契约设计并评审阶段链 graph/coordinator contract。
优先完成 P4-1：Writer/Novel graph builder、chain_version、阶段依赖与初始 blocked/ready 投影；保留旧单 Node Thread 兼容，以及现有 request_id、attempt、executor、approval、cancel、interrupted/recovery 幂等契约。
设计和测试通过后，再实施最小 promote 切片，不要一次性扩展完整阶段链。
不要清理或提交工作树中无关文件。
```
