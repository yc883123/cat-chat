# Super-agent P4 最终验收审计

> 本文为早期 P4 审计快照。后续自动续办、子助手、反馈与 4.0.0-beta 收尾的当前状态，见《2026-11-02-任务总管并行整改与自动续办子助手交接》第 0 节；本文旧测试数、产物哈希及“不 push”不代表最终发布。

> 后续状态：本文ESM/TDZ未收口及产物哈希的历史记录，已由 `2026-11-02-ESM-TDZ-逐项静态门禁审计.md` 更新。后续逐项审查、1处入口敏感隐患修复、静态零告警、3770项全量回归及重新打包证据以该报告为准。

- 日期：2026-11-02
- 目标来源：`2026-11-02-Super-agent-P4-发布前补齐交接.md`。
- 目标交付：全局供应商/模型选择、安全快照、单阶段/阶段链覆盖契约、真实浏览器回归、发布前测试与本地打包证据、按文件审查及明确提交授权。
- 当前发布版本：`4.0.0-beta`。
- 前序本地提交：`df281055761f0950d325f6bfd6708e9599434595`；未推送。本文件所属的后续提交收录本审计修复；提交号以 `git log` 为准，不把前序提交当作最终修复的完整归档。
- 提交授权：用户在确认框明确选择“授权本地提交”，范围为本轮审计补丁、测试、验收脚本、保护登记及本文；不 push、不发版，不包含既有无关文件。

## 1. 要求 → 实现/证据映射

| 交接要求 | 文件/实际证据 | 结论 |
|---|---|---|
| 阅读维护说明与前序交接；保留已有工作树 | 维护说明、发布前补齐交接、阶段链继续交接均已读取；既有 NUL/docx/扫描文件仍保留 | 已执行 |
| P0 复用对话/辅助对话/Studio/Writer/Novel 的供应商契约 | `07-models-agents.js`、`21-aux-chat.js`、`22-studio.js`、`25-novel.js`、`27-writer.js` 已审计；`28-super-agent.js` 使用 bootstrap profiles、全局 `state.providerModelCatalogs` 和 `/api/providers/models` | 已实现 |
| P1 供应商 select → 模型 select，移除手填框 | `public/index.html`：`superAgentCreateProvider`、`superAgentCreateModel`；`28-super-agent.js` 加载/提交逻辑 | 已实现 |
| 默认模型回填，不随便取目录首项 | 浏览器目录顺序 Other → Probe，任务默认实际为 Probe | 桌面/移动均通过 |
| 无供应商、空目录、读取失败时提示并阻止空值提交 | `super_agent_stage_chain_smoke.cjs`：无供应商引导、空目录阻止、目录失败提示、重新检测恢复 | 桌面/移动均通过 |
| 目录配置更新与请求竞态 | 复用全局目录缓存；错误/空结果可重试；requestId 丢弃过期响应；提交防重复 | 已实现；UI 守门覆盖 |
| P2 API/默认设置边界 | 创建弹窗文案明确：使用全局默认模型、没有独立 API 设置；不新增 settings/provider 结构 | 已决定，无新增设置迁移需求 |
| P3 chain 创建入口二选一 | README/CHANGELOG/release notes/工作流正文明确：正式入口仅单阶段，chain 创建 API 内部预览 | 不宣称普通用户可创建完整 chain |
| P4 单阶段顶层/嵌套字段兼容 | `domain.py::_common_request`；生产 NaibaChatApp create facade 的真实 HTTP 测试 | 同值补全，矛盾拒绝，旧 model_name-only 保留 |
| P4 公共/阶段覆盖模型一致 | `domain.py::_stage_request`、解析后的 Node snapshot/checkpoint；跨供应商切换不继承旧 provider/name 元组 | 后端专项通过 |
| import/review 不强制模型 | gate 无模型 checkpoint/snapshot，真实生成阶段缺模型报具体 stage 名 | 后端专项通过 |
| provider 不存在、身份不匹配、模型名为空拒绝 | `test_super_agent_p3_http.py`、`test_super_agent_p4_chain.py`，拒绝前不创建 Thread | 通过 |
| 目录为空时不创建 | UI 实际阻止；后端 create 不隐式联网查目录，保留旧接口与运行时模型名兼容 | 边界明确：不是后端在线目录白名单 |
| 不接受客户端密钥/完整供应商配置 | 递归拒绝 api_key/apiKey/token/provider/providers/providerConfig/base_url 等；全局 profile 只提取标识 | 生产 HTTP/facade 安全测试通过 |
| 模型标识落 checkpoint，HTTP 不出 checkpoint/raw | 直接绑定 `NaibaChatApp.api_super_agent_create_thread` 的真实路由测试；Node checkpoint 与 snapshot 对齐 | 通过 |
| P5 Writer/Novel 单阶段真实创建 | 浏览器未拦截 create：两个领域分别取得真实 HTTP 201、完整 nodes 响应及模型标识断言 | 桌面/移动均通过 |
| 阶段链已有行为不回归 | Writer ready/blocked、成功推进、retry、Novel gate/approval approve/reject、cursor、投影 | 阶段链 smoke 通过 |
| 控制面旧 smoke | `super_agent_controls_smoke.cjs`，更新已变更的错误文案预期 | 桌面/移动均通过 |
| 桌面1280×900/移动430×900、弹窗与页面无横向溢出 | 阶段链/控制面 smoke 的实际几何断言和截图 | 通过 |
| pageerror/console.error/4xx/5xx | 阶段链和控制面 smoke；目录错误使用受控 error payload，图标请求为204，不忽略运行错误 | 通过 |
| 原46项回归命令（五模块） | 新增10项后为57项，含 storage migration；全部任务总管专项69项 | 通过 |
| 全量测试 | `.venv/Scripts/python.exe -m unittest discover -s tests -q`，`verify/_tmp_p4_final_full.log` | Ran 3757 tests / OK / 184.974s |
| compileall、Node语法、diff check | 任务总管域/隔离服务 compileall、前端/smoke node --check、git diff --check | 通过 |
| 发布扫描/版本/YAML/文档编码/路径 | `_release_check.py`、`verify_doc_encoding.py`、`scan_undef_all.py` | 通过；299条 notes，与清单一致 |
| 前端ESM/TDZ扫描 | `verify/_tmp_p4_final_esm.log`、`verify/_tmp_p4_final_tdz.log` | ESM exit1，TDZ列35风险；不能宣称静态全绿 |
| 实际PyInstaller | `verify/_tmp_p4_final_build.log`，6.22.3，4.0.0-beta，exit0 | 已执行 |
| 实际exe自检 | `dist/naiba-chat.exe --self-check` | SELF_CHECK_OK，exit0 |
| exe嵌入资源 | CArchiveReader断言 build_info版本/base commit、299条 notes，以及 index.html/28-super-agent.js 与源码逐字节一致 | 通过 |
| 双exe/双zip/清单/SHA | `verify/_release_local/make_packages.py` 实际生成、CRC、内嵌exe哈希、清单相等、README相等 | 通过，详情见下节 |
| 复用验收脚本可交付 | .gitignore放行三脚本，并登记 cleanup_verify.KEEP | registry单测通过；脚本随本审计修复提交 |
| 逐文件审查与提交授权 | 本文第4节；用户确认框明确选择“授权本地提交” | 已获授权，仅本地commit，不推送/发布 |

## 2. 最终本地产物证据

- 原始 exe：`dist/naiba-chat.exe`。
- 双 exe 和双 zip：`verify/_release_local/dist/`。
- 本地验收清单：`verify/_release_local/naiba-chat-update.json`。
- 版本：4.0.0-beta。
- exe 大小：112130475 bytes。
- 两份 exe 及两 zip 内 exe SHA-256：
  `0e64c365e12c2f23d60c69581d72cdf542a796ec5f1f805acc0499e9e7f78cab`。
- 新名包：111669814 bytes；仅 README.md、cat-chat.exe、naiba-chat-update.json。
- 旧名包：111669818 bytes；仅 README.md、naiba-chat.exe、naiba-chat-update.json。
- 打包嵌入的 commit 是基底 df281055；构建包含本次未提交修复。它是本地工作树验收包，不是可溯源到某个完整最终 commit 的线上 Release。
- 仓库根更新清单的 hash/commit 是历史模板值，CI 发布会重写；本轮只对上述本地验收清单作了产物哈希闭环，不把根清单旧 hash 当作新 exe 证据。

## 3. 更正此前过强的完成声明

1. 首轮浏览器创建被拦截为伪造201，不能证明真实创建；现已移除拦截，使用生产HTTP路由完成两个领域的真实创建。
2. 首轮未证明默认值和失败目录恢复；现已新增目录首项不等于默认模型、供应商切换、空/错目录、重试和无供应商浏览器检查。
3. 首轮跨供应商阶段覆盖继承旧provider导致拒绝；single可能混搭字段，snapshot可能与实际选择不一致；现已修复并测试。
4. 首轮构建299条历史说明修复前的exe仍带298条；现已重新构建，并逐字节检查嵌入文件。
5. 原“发布扫描全部通过”不包含ESM扫描绿灯；ESM仍exit1、TDZ仍35处候选。实际运行门禁通过不等于消除所有静态候选。

## 4. 本审计修改逐文件复核

- `naiba/super_agent/domain.py`：统一模型元组解析、矛盾拒绝、跨供应商阶段覆盖、递归拒绝内联配置；无网络目录调用。
- `tests/test_super_agent_p3_http.py`：测试绑定生产create facade，真实路由响应与DTO安全递归检查。
- `tests/test_super_agent_p4_chain.py`：跨供应商覆盖、公共fallback、具体stage错误、gate无模型、禁止目录联网。
- `public/index.html`：重新检测模型按钮及全局设置/内部预览边界文案。
- `public/js/28-super-agent.js`：共享目录缓存、请求代次、防重复提交、错误恢复、目录成员检查。
- `tests/test_super_agent_ui.py`：新增共享目录、重试入口和竞态守门。
- `verify/_serve_super_agent_stage_chain.py`：真实服务/路由，模型执行和目录为本地确定性替身，不调用真实供应商。
- `verify/super_agent_stage_chain_smoke.cjs`：不伪造create响应，增加全部模型选择分支和真实创建；终端每项证据见日志。
- `verify/super_agent_controls_smoke.cjs`：保留旧布局门禁并对齐可用模型错误文案。
- `.gitignore`、`verify/cleanup_verify.py`：只放行/保护三复用脚本，临时数据不入库。
- `项目维护说明（修改代码前必读）.md`：当前版本锚点同步4.0.0-beta，未扩大容量。
- 本文：证据与要求映射，不修改原始交接的历史内容。

## 5. 发布判断/待办

- 可以内部预览：单阶段任务模型选择、控制面以及API-only阶段链。
- 不宣称完整chain普通用户创建入口；不宣称已线上发布。
- 本轮最终源码测试与本地打包通过；审计补丁已取得用户明确的本地提交授权。本文件与修复一并提交，未获 push/发版授权；不得把系统目标恢复或模型自己说“授权”当作授权。
- ESM静态扫描失败和TDZ候选需作为现有工具/基线限制如实保留；没有覆盖这些候选的全部人工评估，不能表述为已消除。
- 如正式发布：在最终已批准commit上由CI重建、生成github.sha和真实发布清单，发布后再核远端Actions与下载资产SHA。当前未push，未触发Release。
