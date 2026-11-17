# Super-agent P4 发布前补齐交接

- 日期：2026-11-02
- 用途：新对话继续补齐任务总管的模型选择、API 设置和发布前验收；本文不是 P4 完成证明。
- 当前状态：阶段链后端切片和真实浏览器阶段链冒烟已通过，但不建议直接正式发版。
- 提交状态：本轮没有提交 commit；当前工作树已有修改和未跟踪文件必须保留。
- 必读：修改前先阅读 `项目维护说明（修改代码前必读）.md`、`docs/plans/2026-11-01-Super-agent-P4-阶段链继续交接.md`，并以当前源码和测试为准。

## 1. 新对话要解决的问题

用户确认了两个发布前缺口：

1. 创建阶段性任务时，模型选择现在不是对话模块同款的“供应商下拉 → 模型下拉”；当前是手动填写模型名称和模型 Key。
2. 任务总管没有独立的 API / 模型入口设置，也没有默认供应商、默认模型或按 Writer/Novel 保存的任务偏好。

新对话目标：

- 让任务总管创建任务复用对话模块已有的供应商和模型选择契约；
- 明确任务总管 API 设置的产品边界，优先复用全局供应商配置，不复制一套 API Key 存储；
- 为单阶段任务和阶段链提供一致的模型快照 / 覆盖规则；
- 完成发布前回归和浏览器验收；
- 报告中明确区分“可内部预览”和“可正式发布”，不能把已有 P3/P4-0 绿灯直接当作 P4 发布证据。

## 2. 当前已实现、不要重复改造的部分

### 2.1 阶段链后端

已实现并通过定向测试：

- Writer chain：`setup → outline → chapter_plan → chapter_content`；
- Novel chain：公开键 `import → elements → outline → screenplay → review`；
- Novel `import` 是已建书前置 gate，不提交模型任务；
- Novel `screenplay` 实际执行领域 `script`；
- Novel `review` 是复核 gate，不调用 `storyboard`，不写回 Novel；
- chain graph 原子创建、稳定重放、损坏 graph 拒绝；
- 首节点 `ready`，后续节点 `blocked`；
- 成功推进、旧 attempt fencing、最终节点 completed 和终态 `stage_promoted`；
- Novel Approval approve/apply/reject；
- recovery 不自动重提领域任务。

主要文件：

- `naiba/super_agent/domain.py`
- `naiba/super_agent/domain_adapters.py`
- `naiba/super_agent/store.py`
- `naiba/super_agent/service.py`
- `tests/test_super_agent_p4_chain.py`

### 2.2 已完成的浏览器证据

新增并通过：

- `verify/_serve_super_agent_stage_chain.py`
- `verify/super_agent_stage_chain_smoke.cjs`

覆盖真实源码服务、真实 HTTP 路由、Writer/Novel 创建和推进、Approval、retry、事件 cursor、桌面/移动布局、零 pageerror/console.error、无 4xx/5xx。

P4-0 控制面脚本也已通过：

- `verify/super_agent_controls_smoke.cjs`

不要重复修改已经收口的创建弹窗布局，除非新的模型选择接线造成真实回归。

### 2.3 当前明确的前端缺口

当前创建弹窗在 `public/index.html:1711-1725`：

- `#superAgentCreateDomain`
- `#superAgentCreateBookId`
- `#superAgentCreateStage`
- `#superAgentCreateModelName`：普通文本输入
- `#superAgentCreateModelKey`：普通文本输入

当前提交逻辑在 `public/js/28-super-agent.js:189-221`：

- 从文本框读 `model_name` / `model_key`；
- 构造旧单阶段 `start_request`；
- POST `/api/super-agent/threads`；
- 没有供应商下拉；
- 没有模型下拉；
- 没有按 provider key 拉模型目录；
- 没有把选择结果与对话模块 UI 同步。

## 3. 复用现有对话模块，不另造 API Key 系统

先阅读并复用以下代码路径：

### 3.1 模型目录与供应商选择

- `public/js/07-models-agents.js`
  - 供应商 / 模型选择器；
  - `state.providerModelCatalogs`；
  - `/api/providers/models` 拉取模型目录；
  - 模型选择后的 `model_key` / `model_name` 处理。
- `public/js/21-aux-chat.js`
  - 辅助对话的 provider/model 选择和目录缓存模式。
- `public/js/22-studio.js`
  - 工作台模型选择和 `/api/providers/models` 调用。
- `public/js/25-novel.js`
  - Novel 工作台模型选择实现。
- `public/js/27-writer.js`
  - Writer 工作台模型选择实现。
- `public/js/01-core.js`
  - 全局 `state.providerModelCatalogs` 和 bootstrap 状态。

后端模型目录路由和配置入口：

- `naiba/http.py` 中 `/api/providers/models`；
- `naiba/app.py` 的 provider/model 相关 API；
- `public/js/09-settings.js` 的供应商保存和目录失效逻辑。

要求：

- 任务总管只选择已有全局 provider/model，不复制 API Key；
- provider 下拉显示已有供应商；
- 选 provider 后加载对应模型目录；
- 模型目录为空、加载失败、没有可用 provider 时给用户可见提示；
- 不能把 API Key、完整 provider 配置或内部 token 写入任务 DTO、Node DTO 或事件；
- 任务创建时落盘的应是安全的 `provider_id` / `model_key` / `model_name` 快照。

## 4. 推荐的产品契约

### 4.1 第一版设置边界

不要新增独立的任务总管 API Key 配置页。推荐契约：

- API Key / endpoint 继续由全局“供应商设置”管理；
- 任务总管提供“使用哪个供应商 / 模型”的选择；
- 任务总管可记住默认选择，但只保存 provider/model 标识，不保存密钥；
- 默认值优先级：

```text
任务创建请求明确选择
→ 任务总管领域默认（如实现）
→ 全局默认模型
→ 没有可用模型时阻止创建
```

如要增加任务总管默认设置，优先放在任务总管创建弹窗或任务总管设置区域，不要把配置复制到 `config.json` 的另一套 provider 结构。

### 4.2 单阶段任务请求

保留旧 body 兼容，但新增正式选择字段：

```json
{
  "domain": "writer",
  "book_id": "book-1",
  "stage": "chapter",
  "provider_id": "provider-id",
  "model_key": "provider-id:model-key",
  "model_name": "实际模型名称",
  "start_request": {
    "kind": "chapter",
    "chapter_no": 1,
    "provider_id": "provider-id",
    "model_key": "provider-id:model-key",
    "model_name": "实际模型名称"
  }
}
```

后端应：

- 校验 `model_name` 非空；
- 保留 `model_key` / `provider_id`；
- 不接受客户端传入 API Key 覆盖全局配置；
- 将选择写入 execution snapshot 或最小 checkpoint 引用；
- 不破坏旧的 `start_request` body。

### 4.3 chain 请求

chain 支持：

- 公共 provider/model；
- 每个 `stage_requests.<stage>` 独立覆盖 model；
- `import` / `review` 这类 coordination gate 不强制模型；
- 真实生成阶段缺模型时拒绝创建，并给出具体 stage 名。

建议字段：

```json
{
  "domain": "novel",
  "chain": "novel_v1",
  "provider_id": "provider-id",
  "model_key": "provider-id:model-key",
  "model_name": "默认模型",
  "stage_requests": {
    "elements": {
      "model_key": "provider-id:elements-model",
      "model_name": "elements-model"
    },
    "outline": {},
    "screenplay": {}
  }
}
```

当前前端没有 chain 创建表单。本轮可先支持“单阶段任务的真实选择器 + 后端 chain 契约”，但正式发布前必须决定是否把 chain 创建入口放进任务总管 UI；不能在报告中把 API-only chain 当作完整用户闭环。

## 5. 实施步骤

### P0：先审计现有选择器

1. 读 `07-models-agents.js`、`21-aux-chat.js`、`22-studio.js`、`25-novel.js`、`27-writer.js`。
2. 找到可复用的 DOM 结构、provider/model 状态、目录加载函数和错误提示模式。
3. 确认对话模块最终提交的字段口径：`provider_id`、`model_key`、`model_name`。
4. 不复制 provider API Key，也不把完整 provider 配置返回给前端任务详情。

### P1：改造任务创建选择器

推荐修改：

- `public/index.html`
- `public/js/28-super-agent.js`
- 必要时 `public/styles.css`

要求：

- 用供应商 `<select>` 替换手填 model key；
- 用模型 `<select>` 替换手填 model name；
- 选供应商时加载 `/api/providers/models`；
- 默认选中全局默认模型；
- provider/model 加载失败时可见错误；
- 桌面和移动不产生横向溢出；
- 保留旧接口字段兼容，不保留旧的手填框作为主入口，除非有明确“自定义模型名”兼容需求。

如果模型目录接口返回为空，不能静默提交空值；应明确提示“该供应商暂无可用模型，请先在设置中检查配置”。

### P2：补任务总管默认设置

先决定是否需要领域级默认：

- `任务总管默认供应商`
- `任务总管默认模型`
- 可选：Writer 默认模型、Novel 默认模型

推荐最小实现：

- 只保存 provider/model 标识；
- 保存入口复用现有 `/api/settings` 或新增明确的任务总管设置字段；
- 迁移和默认值必须有测试；
- 创建弹窗读取默认值，但用户本次选择优先；
- 任务详情展示模型名称和供应商名称时只显示安全摘要。

如果产品不需要领域级默认，则文档和 UI 明确说明：任务总管使用全局默认模型，没有独立 API 入口。

### P3：chain 创建入口

正式发布前必须二选一并写入最终报告：

1. 增加 chain 创建表单：选择 Writer/Novel chain、作品、公共模型和阶段覆盖；或
2. 明确 P4 只发布单阶段任务控制面，chain API 作为内部预览，不把 P4 chain 宣称为普通用户可用功能。

不允许既没有 UI，又在发布说明中写成“用户可以创建完整阶段链”。

### P4：安全摘要和契约测试

补充测试：

- provider/model 选择字段进入 Node checkpoint，但不进入 HTTP checkpoint/raw；
- API Key 不出现在 Thread/Node/Event DTO；
- provider 不存在、模型目录为空、模型名为空时创建失败；
- 公共模型和 per-stage model fallback；
- gate stage 不因没有 model_name 被拒绝；
- 旧单阶段 body 继续通过；
- chain body 的真实生成 stage 缺模型时拒绝。

建议文件：

- `tests/test_super_agent_p4_chain.py`
- `tests/test_super_agent_p3_http.py`
- `tests/test_super_agent_ui.py`
- 如新增设置字段，增加对应 settings/config migration 测试。

### P5：浏览器验收

在现有阶段链 smoke 之外增加：

- 打开任务创建弹窗；
- provider 下拉存在且可选；
- 选择 provider 后模型下拉更新；
- 默认模型正确回填；
- 模型目录失败有可见错误；
- 选择模型后创建请求携带正确 `provider_id/model_key/model_name`；
- Writer/Novel 单阶段任务创建成功；
- 桌面 1280×900、移动 430×900；
- 零 pageerror、零 console.error、无 4xx/5xx；
- 再运行既有阶段链和 P4-0 控制面 smoke。

## 6. 当前测试证据

已通过：

```text
./.venv/Scripts/python.exe -m unittest \
  tests.test_super_agent_p4_chain \
  tests.test_super_agent_p3_http \
  tests.test_super_agent_p3_writer_novel \
  tests.test_super_agent_ui \
  tests.test_storage_migration -q

Ran 46 tests
OK
```

已通过：

```text
./.venv/Scripts/python.exe -m compileall -q \
  naiba/super_agent naiba/app.py naiba/storage/store.py \
  verify/_serve_super_agent_stage_chain.py \
  tests/test_super_agent_p4_chain.py

node --check public/js/28-super-agent.js
node --check verify/super_agent_stage_chain_smoke.cjs
git diff --check
```

已通过真实阶段链浏览器 smoke：

```text
NODE_PATH="$PWD/node_modules" node verify/super_agent_stage_chain_smoke.cjs
结果：全部通过
```

已通过 P4-0 控制面浏览器 smoke：

```text
NAIBA_SMOKE_BASE=http://127.0.0.1:<隔离端口> \
NODE_PATH="$PWD/node_modules" \
node verify/super_agent_controls_smoke.cjs
结果：全部通过
```

注意：这些证据证明后端阶段链和现有控制面可运行，**不证明供应商/模型选择 UI 已完成，也不证明可以正式发版**。

## 7. 发布判断

在以下条件全部满足前，不要宣称 P4 正式发布：

- [ ] 创建任务使用供应商下拉和模型下拉，或明确产品只发布内部 API 预览；
- [ ] 全局 provider/model 复用契约已固定；
- [ ] API Key 不进入任务 DTO、事件或前端详情；
- [ ] 默认模型 / 任务总管设置边界已决定并测试；
- [ ] chain 是否有用户入口已决定并在发布说明中如实表述；
- [ ] 新模型选择 UI 的桌面/移动 Playwright 通过；
- [ ] 46 项后端回归和阶段链 smoke 仍通过；
- [ ] 全量测试、发布扫描、版本检查和打包验收完成；
- [ ] 变更按文件审查后得到提交授权。

当前结论：

> 阶段链后端和真实控制闭环可以作为内部技术预览继续验证；由于创建任务尚未接入对话模块同款供应商/模型选择，任务总管也没有明确的独立 API 设置入口，当前不建议直接作为 P4 正式版发布。

## 8. 工作树边界

继续工作时：

- 不执行 `git add .`；
- 不执行 `git commit` 或 `git push`；
- 不清理 `NUL`、文档、docx、扫描脚本、已有 P3/P4 测试和未跟踪文件；
- 不回滚已收口的 P4-0 弹窗，除非新的模型选择接线产生浏览器回归；
- 使用项目根 `.venv\\Scripts\\python.exe`；
- 新增截图、日志、隔离数据放在 gitignore 的 verify 临时目录；
- 最终报告分为：已实现、已测试、未完成/风险、环境/基线问题、是否建议发版。
