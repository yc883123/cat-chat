# 漫剧入口整改：Computer Use 真实鼠标验收交接

> **2026-11-03 续验更新**：已使用官方 Computer Use 跑通原生图／音视频／目录上传、跨入口工作流导入返回、安全参数另存、模拟生成与失败补收集、375×812 局部检查；发现并修复隐藏祖先中的导入弹窗、导入响应缺指纹、完整提示词状态文案三处问题。最新专项 232／全量 4086 项通过。**不是本清单全部通过**：准确矩阵、工具限制与剩余项见 `docs/plans/2026-11-03-漫剧制作入口整改验收.md` 的“Computer Use 实际续验与修复”节。cua02/cua03 隔离根的服务已请求受控关闭，不能复用旧 URL 当作活服务。后续从剩余项继续，不重新扩大范围，也不要沿用“Chrome/IDM 根因已锁定”的早段错误归因。

## 1. 新对话从这里开始

用户最新要求：**用真实鼠标验收，使用 Computer Use 工具**。本次对话已完成第一轮整改代码与离线测试，但未完成真实鼠标 GUI 验收。用户要求写交接文档，换新对话继续。

下一步不是重做整改方案，也不是继续用页面脚本证明可点击：

1. 先读本文件、《项目维护说明（修改代码前必读）》以及 `docs/plans/2026-11-03-漫剧制作入口整改验收.md`。
2. 查看新会话实际提供的 Computer Use 工具和官方技能，按官方说明初始化。
3. 使用真实鼠标、键盘、截图、原生文件对话框验收源码界面；发现问题则修复并回归。
4. 更新验收记录，清楚区分已通过、失败、环境阻断和未验证。

本会话尝试 `Skill("computer-use")` 返回 `Skill not found: computer-use`，没有加载到官方 Computer Use 能力。**这只是本会话的事实，不能推断新会话仍然没有。** 新会话应以其实际工具／技能列表为准，不要猜官方模块路径或凭空构造 SDK。

## 2. 用户已确认的范围与硬边界

- 依据 `docs/plans/2026-11-03-漫剧前端对照与制作入口整改建议.md` 实施第一轮完整往返，包含 H3 多模态纠错、安全参数另存和失败项复制。
- **不做织书台／拆书匠联动，不修改其入口和交接。** 用户先选择同时做制作主线，后明确撤回，最新决定优先。
- H3 R2V 支持最多 9 张图、3 段视频、3 段音频，按需添加，不必填满、不强制首图。I2V 仍需首帧。
- 音视频原件交给 H3；提词模型只接收用户填写的音视频内容／用途描述，不新增自动视频理解／音频转写。
- 参考视频只使用画面，不自动把视频自带音轨当作参考音频；需要声音参考时独立添加音频。
- 不做自动剪辑／成片合成，不新增通用自主制作流水线。
- **不调用真实付费模型、不提交真实 H3 推理，不改版本号、不提交、不推送、不发布。** 真实鼠标验收不等于付费生成授权；继续使用隔离假服务。
- 不操作正式配置、正式数据库和运行中的用户任务，不关闭正式版实例。

## 3. Git 和工作区状态

交接时分支 `master`，HEAD `9cb7271`（任务总管弹窗三段式骨架与视觉统一）。本轮整改全部未提交，开始前复核当前 Git 状态，避免覆盖其他会话的新改动。

### 本轮实现文件

- `launcher.py`：冻结版受限媒体解码子进程早期入口。
- `naiba/app.py`、`naiba/http.py`：方案复制草稿与工作流另存接口接线。
- `naiba/core/contracts.py`：模式、媒体限额、编辑能力契约。
- `naiba/comfyui/{runner,schema,service,store}.py`：安全另存、schema 元数据、原子指纹复核、引用删除保护、可选媒体链裁剪。
- `naiba/studio/{images,service}.py`：recipe 专用九图内部校验，不放开公共任意限额。
- `naiba/super_agent/{recipes,recipe_catalog,recipe_store}.py`。
- `naiba/super_agent/recipe_assets/{convert_official.py,h3_r2v.api.json,h3_r2v.prompt.txt,provenance.json}`。
- 新增 `naiba/super_agent/recipe_assets/h3_r2v.v1.prompt.txt`，保留旧模板解析。
- `public/index.html`、`public/styles.css`。
- `public/js/{15-bind-events,22-studio,24-comfyui,28-super-agent}.js`。
- `tests/{test_super_agent_recipes,test_recipe_catalog,test_native_recipe_submission,test_recipe_frontend_assets,test_recipe_ui_fixture}.py`。
- 新增 `tests/test_comfyui_safe_save.py`、`tests/test_recipe_media_launcher.py`。
- `verify/_serve_recipe_ui.py`。
- 维护说明与本轮验收／交接文档。

### 必须保留的既有改动

`tests/test_comfyui_module.py` 在本轮开始前已有 **7 行未提交新增**，用于本地候选／连接探针 mock。本轮仅运行该测试，没有改这 7 行。其他既存未跟踪文件包括 `NUL`、设计预览 HTML、旧交接文档、Word 反馈、CSV／扫描脚本等，均未清理。**不要 git reset、git restore、git clean 或批量暂存。**

## 4. 已实现的行为

### 方案与草稿

- 方案选择旁“新建方案”“编辑方案（另存版本）”。
- 新建可基于已有方案或选择已有工作流，没有工作流时使用原造梦机导入并返回。
- 编辑采用不可变另存版本，旧任务继续使用冻结快照。
- 提词规则预览、复制正文、复制修改、新建副本并返回当前方案。
- 保存按钮“保存并继续当前任务”，选中新方案、恢复原批次，默认给出第一项内容输入。
- 草稿保存文本、素材、顺序、模式和选项；刷新只恢复安全元数据，不序列化 File，未完成上传提示重选。
- 更改素材引用时清除失效最终提示词，并保留旧文本副本供人工参考。

### 后端模式与执行

- 独立 `input_mode="prompt"|"script"`，不混淆生成方案 mode。
- 完整提示词不要求规则／提词模型，创建、确认、调度、重试、补收集都不解析无关模型。
- 纯文本代写无需图片和视觉能力；有实际图片输入才校验图像能力。
- 媒体带服务端核验的类型、hash、元数据、冻结路径及引用编号。
- AV 校验在 12 秒可终止子进程内执行，检查元数据、所需轨道、首个可解码帧，非整段完整解码保证。
- H3 视频正确连接：`LoadVideo` 输出 VIDEO → `GetVideoComponents` 输出 0 IMAGE → `MiniMaxH3ReferenceToVideo.ref_videos.*`。独立 `LoadAudio` → `ref_audios.*`。
- 从冻结工作流和规则版本派生，保留 v1 六图历史快照；可选 loader／组件裁剪保留共享／required 消费者。
- 字段级编辑能力；生成记录存在时锁定执行输入，提供真实“复制为新任务（不执行）”。
- 双确认、request_id 防重、revision/hash、未知提交对账、补收集不重新生成保持。

### 安全另存工作流

`POST /api/comfyui/workflows/{source_id}/save-as`

```json
{
  "name": "新工作流名称",
  "workflow_fingerprint": "当前源图指纹",
  "overrides": [
    {"node_id": "节点", "field_name": "字段", "value": 42}
  ]
}
```

返回 201 `{ok:true,workflow:新详情}`，包含新 ID／指纹。400 非法值，404 源缺失，409 指纹过期，503 修改字段缺权威 schema。

- 仅实时 schema 枚举成员、严格有限有界数字与布尔；拒绝自由字符串、代码、路径、媒体入口和任意目标／凭据。
- 前端把控件数值字符串转换成严格 JSON 数字／布尔后再提交。
- 不随机化 seed，不改源图，Store 锁内复核指纹并持久化副本。
- 空 overrides 可以安全复制未改动图，不需要为未修改字段额外授权。
- 原 `/params` 仍只保存显示项和顺序；方案／批次引用的工作流禁止删除。

## 5. 已跑过的测试，不要冒充 GUI 证据

- 基线全量：4054 项通过。
- 首轮集成：4078 项通过。
- 最终全量：**4082 项通过，297.485 秒**，`verify/_tmp_remediation_final_full_tests.log`。
- 最后转换脚本／前端类型收尾后专项：**36 项通过**，覆盖 catalog、frontend assets、安全另存和 launcher 子进程入口。
- 后端扩展组合曾跑 295 项通过；前端相关 136 项通过；夹具含零参考、仅视频、仅音频、混合、9＋3＋3 满容量真实 HTTP 替身执行与输出解码。
- ESM、TDZ、JS 语法、未定义名称扫描（145 文件、0 候选）、文档编码和 `git diff --check` 通过。
- 全量回归与最后 36 项专项并非完全同一时刻的树；如新会话改代码，仍须按维护说明重跑。

## 6. 旧浏览器验证的真实边界

使用官方 Browser Use 的 IAB 检查过桌面 1280×720、手机 375×812：

- 填写剧本 → 编辑内置方案 → 保存继续 → 原文本逐字保留并选中新版本。
- 预览专用规则 → 复制修改 → 保存返回 → 当前方案选中新规则。
- 切完整提示词，模型依赖控件隐藏；复制混合素材草稿并填写完整提示词，仅建单成功。
- 混合素材图片可用，video/audio `duration=1`、`readyState=4`、无媒体错误。
- 两个弹窗及混合复制草稿 clientWidth/scrollWidth 均 375，根页面无横向溢出。
- 观察到的步骤运行时错误列表为空，配置／复制／仅建单结束后假服务 model/prompt/upload 计数均为 0。

**但不能称真实鼠标通过：**高层 locator 点击反复超时，DOM/CUA 点击未达预期，确认命中无遮挡后改用已观察控件的页面 `click()`／input 事件检查。仅证明事件接线和数据往返，不能证明真实鼠标／焦点。截图内容当时模型无法读取。用户因此明确要求 Computer Use 重新验收。

下一轮**禁止**用 `evaluate().click()`、脚本填表、直接调用应用函数、API 代办等替代用户要求的真实鼠标路径；HTTP API 只可用于隔离夹具设置与独立证据核验，不可冒充 GUI 操作。

## 7. 重新启动隔离验收环境

本轮专用夹具服务已通过受控 shutdown 停止，后台进程正常退出。原来浏览器标签可能仍显示旧地址，但服务已停止，**不要复用旧端口或视为仍在运行**。

旧临时夹具位于 `verify/_tmp_remediation_browser_20261103/`，含临时配置、假服务控制 token 和测试数据。不打印整个 manifest，不将 token 写进报告。不要在旧非空 root 重新启动或删除它。

使用**全新空目录**启动（以下路径相对项目根）：

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py verify\_serve_recipe_ui.py --root verify\_tmp_recipe_computeruse_<本次唯一后缀> --port 0 --media-mode video
```

必须用项目 venv，不用系统 Python。夹具：

- 安装禁止非 loopback 网络的 guard，独立 ConfigStore／PathContext，不读正式配置、不迁移正式数据。
- 真实 NaibaChatApp、Studio 与 ComfyUI 执行器；只把外部模型和 ComfyUI HTTP 服务替换为本地假服务。
- 启动输出给 `fixture.json` 路径和本轮 `app_url`，用该 URL 打开源码网页。
- 默认有 I2V、R2V 两份草稿，额外 `LOCAL 多媒体参考 · 可复制草稿`；manifest 的 `mixed_draft` 与 `mixed_payload` 可用于核验来源。
- 九张 PNG、三段 PCM WAV、三段本地合成且可解码 MP4；manifest 给 `images/audios/videos` 路径。真实鼠标文件对话框可直接选择这些受控文件。
- 用户可编辑工作流 `LOCAL · 可编辑 H3 配方工作流`；替代 LoRA 枚举 `fixture_alternative.safetensors` 可用于安全另存路径。
- 控制端点使用 `X-Fixture-Token`，可读计数及提交图；不要将授权 URL／token 展示给用户。
- 退出用控制端点 POST `/shutdown`，只停止本轮自建服务，不触碰正式服务。

**不要直接执行普通 `server.py` 作为本次验收环境**：它没有通用 `--config` 隔离参数，可能读写正式配置、状态和实例锁。沿用现有夹具。

## 8. 真实鼠标验收清单（按顺序）

每组保存截图证据和观察结果；截图／日志放 gitignore 临时目录，报告使用相对路径，不存凭据。

1. **首次进入与默认第一项**：真实鼠标关闭欢迎引导 → 任务总管 → 批量制作视频。检查新建／编辑入口、默认内容输入、滚动与按钮可点。
2. **方案另存往返**：填任务名、正文、数量与参数，上传素材，编辑内置方案另存，检查原文本／顺序／素材／模式／数值均保留，选择新方案，不新增空任务、不执行。
3. **规则往返**：查看确切正文和来源，复制修改、新建规则，保存后回同一方案；取消也不丢选择。再保存方案回原批次。
4. **工作流选择／导入返回**：新建方案 → 使用我的工作流 → 已有工作流或原生文件对话框导入受控 JSON，确认返回原方案／原批次，不丢原稿。
5. **参数保存断路**：造梦机选择夹具工作流，改 LoRA 为合法替代枚举，数字／布尔参数；点击独立另存，不点原显示编辑充当值保存；验证新 ID、原值不变，返回原制作草稿。非法枚举／任意文本明确失败。
6. **模式依赖**：完整提示词不强制模型；纯文本代写无图也能准备。模型设置打开／关闭后回原稿。不配置真实供应商。
7. **真实上传**：用原生文件对话框添加图、视频、音频，目录导入、失败重选；验证未选媒体不铺满必填框，H3 R2V 不强制首图，I2V 首帧依旧必需。
8. **引用与素材交互**：仅视频、仅音频、混合；添加／删除／重排时编号一致且旧提示词失效提醒。图视频音频分别预览、真实点击播放／暂停。
9. **满容量与超限**：9图／3视频／3音频可准备，额外素材明确拒绝，不静默截断；重复文件去重上传不代表槽位丢失。
10. **实际模拟执行闭环**：使用隔离本地替身，真实鼠标逐次确认提词和生成，审核编辑、保存、失效确认、重复点击防重、产物播放／下载。明确写“模拟生成”，不是 H3 实测。
11. **失败恢复与复制**：用夹具控制失败场景，检查字段锁定准确、复制新任务不执行、补收集不生成、未知提交不重提。
12. **桌面＋375px**：真实鼠标／键盘焦点、Tab／Esc、嵌套弹窗返回、底部按钮和滚动、横向溢出、媒体播放。优先 Computer Use 实际窗口缩放；若用浏览器设备模式须说明条件。
13. **无意外调用**：导航、规则／方案／工作流保存和仅建单不增加 model/prompt 计数；只有明确的模拟确认才增加正确计数。不得出现外部网络。

若 Computer Use 未加载或鼠标控制失败，立即记录具体错误，不绕回脚本点击后宣布通过。若只能完成部分项目，按项目标注，不笼统写“验收通过”。

## 9. 回归和收口

如修复代码，按维护说明执行相关测试和最终全量：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_super_agent_recipes tests.test_recipe_catalog tests.test_native_recipe_submission tests.test_recipe_frontend_assets tests.test_recipe_ui_fixture tests.test_comfyui_safe_save tests.test_recipe_media_launcher -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe verify\scan_undef_all.py
.\.venv\Scripts\python.exe verify\esm_graph_check.py
.\.venv\Scripts\python.exe verify\tdz_check.py
.\.venv\Scripts\python.exe verify\verify_doc_encoding.py
git diff --check
```

更新 `docs/plans/2026-11-03-漫剧制作入口整改验收.md` 中未完成项，记录 Computer Use 实际工具、真实操作、截图、错误、修复及结果。仍不得把替身通过称为真实 H3 推理通过，也不得在没有用户新授权时打包／提交／推送／发布。

本轮唯一核心待办：**补足真实鼠标 GUI 验收，发现问题就修，不重新扩大业务范围。**
