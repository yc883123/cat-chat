# Cat Chat 直连 NapCatQQ 实施计划

> 状态：**S0 / S1 / S2 已实施并通过自动化验证；S3 真实 NapCat 验收未做**。
> 日期：2026-11-05
> 目标：通过 NapCatQQ 的 OneBot 接口，让用户用 QQ 向 Cat Chat 发消息、收取 AI 回复，并使用 Cat Chat 会话已授权的工具。
>
> 实施结果见本文末尾「十、实施记录」：模块位置、已交付能力、自动化证据，以及**仍未验证**的部分。


---

## 一、目标和边界

Cat Chat 直接连接 NapCatQQ，不增加 AstrBot。NapCat 负责登录 QQ、收发 QQ 消息并提供 OneBot 接口；Cat Chat 负责把 QQ 消息交给现有会话和 Run 系统处理，再把结果发回 QQ。

```text
QQ 用户
  ↕
NapCatQQ
  ↕ OneBot 反向 WebSocket
Cat Chat OneBot Channel Adapter
  ↕
Conversation / Run / Tools
```

NapCat 是消息通道，不执行模型任务。Cat Chat 不重写模型调用或工具执行流程。

首期按以下边界设计：

- NapCat 主动连接 Cat Chat 的反向 WebSocket 服务。
- 私聊用户需在 Cat Chat 配置的允许名单中；未允许的账号不触发会话或模型调用。
- 默认不接收群聊控制消息。是否开放群聊，需另行确定群白名单、触发规则和多人会话隔离方式。
- 私聊支持 OneBot/NapCat 实际提供且 Cat Chat 能安全处理的文字、图片、语音、视频和文件消息段。协议或宿主能力不支持的类型必须明确告知用户，不得静默丢弃。
- QQ 消息沿用 Cat Chat 会话的模型、Agent、工具权限和审批规则，不因换成 QQ 通道而扩大权限。
- 首期采用配置文件配置，不要求新增设置 UI；密钥不得通过公开配置接口返回。

## 二、仓库现状和复用点

当前仓库没有 OneBot/NapCat 渠道模块、QQ 身份映射或 QQ 消息回传机制。

可复用的现有入口：

- `NaibaChatApp.api_create_conversation()`：创建普通会话，位于 `naiba/app.py`。
- `ConversationRunManager.submit_chat()`：提交聊天并复用会话模型、Agent、工具集和权限快照，位于 `naiba/run/chat.py`。
- `ConversationRunManager.wait_for_events()` / `events_after()`：读取 Run 持久化事件，位于 `naiba/run/manager.py`。
- `ChatStorage.get_conversation()`：读取完成后的会话消息，位于 `naiba/storage/store.py`。
- `NaibaChatApp.stop()`：应用后台服务统一关闭入口，位于 `naiba/app.py`。
- `ConfigStore`：默认配置、迁移和持久化，位于 `naiba/config.py`。
- 通用附件缓存和 `store_uploaded_file()`：附件写入 Cat Chat 受管 uploads，位于 `naiba/storage/media.py` 及 `naiba/app.py`。

Run 的 HTTP NDJSON 流是给浏览器用的，不应由渠道模块调用。OneBot 适配器应直接调用应用服务并读取 Run 事件或终态会话消息。

## 三、模块设计

### 3.1 OneBot Channel Adapter

新增 `naiba/channels/onebot.py`，职责包括：

- 接受 NapCat 反向 WebSocket 连接，验证配置的 OneBot access token。
- 解析 OneBot 事件和消息段，校验字段、大小和消息来源。
- 过滤未授权的 QQ 用户、群聊及不支持的消息类型。
- 对 OneBot `message_id` 做持久去重，避免重连或重复推送导致重复提交 Run。
- 将用户消息和媒体转换为 Cat Chat `submit_chat()` 可接受的正文与 `attachments`。
- 通过 NapCat OneBot API 发送回复、确认卡片文本和错误说明。
- 拆分超过 QQ 单条消息限制的回复，尽量保留段落和代码块。
- 维护重连、发送失败、待确认和运行中任务的可观察状态。

适配器不得把 OneBot 解析、QQ 身份判断或 NapCat API 调用塞入 `naiba/run/`。

### 3.2 应用装配和生命周期

在 `NaibaChatApp` 初始化时读取渠道配置并装配服务。服务只在明确启用后启动。应用关闭时停止监听、结束 WebSocket 会话并等待后台投递线程收尾。

反向 WebSocket 服务不得绕过自身鉴权。监听地址默认使用 loopback；需要局域网连接时，必须由用户显式设置监听地址，并提醒 OneBot 端口暴露风险。NapCat token 与 Cat Chat 全局 `access_token` 分开保存、分开校验。

HTTP handler 不负责持有长连接。若当前运行时不能直接承载 WebSocket，应由渠道模块使用独立 WebSocket 服务，并由应用生命周期管理，不能把长连接逻辑塞进 `ThreadingHTTPServer` 的普通请求处理函数。

### 3.3 配置

在 `default_config()` 和 `config.example.json` 增加独立 `onebot` 配置对象，至少包含：

```json
{
  "enabled": false,
  "listen_host": "127.0.0.1",
  "listen_port": 8766,
  "path": "/onebot/v11/ws",
  "access_token": "",
  "allowed_user_ids": [],
  "allowed_group_ids": [],
  "allow_groups": false,
  "default_model_key": "",
  "default_model_name": "",
  "default_agent_id": ""
}
```

配置加载需要兼容旧配置。任何面向 UI 或 API 的公开设置快照都必须脱敏 `access_token`，并仅返回是否已配置。渠道身份白名单的增删必须走校验，不接受任意消息自行完成高权限绑定。

第一版的 `allowed_group_ids` 和 `allow_groups` 默认不启用。若产品决定首发支持群聊，需增加 `@机器人` 或前缀触发规则、群/用户/话题到会话的映射，并限制群聊中的工具确认权限。

### 3.4 QQ 身份与 Cat Chat 会话

新增持久化映射，建议独立放在渠道存储模块，例如 `naiba/channels/store.py`，避免把渠道状态塞进通用模型配置：

```text
channel_account_id
conversation_id
scope                 # private / group
qq_user_id
qq_group_id
created_at
updated_at
```

私聊映射键至少包含 NapCat bot 账号和 QQ user ID。若后续开放群聊，映射键还要包含 group ID，防止不同群共用上下文。

会话首次创建使用显式配置的默认模型和 Agent；缺少有效模型时拒绝创建并向 QQ 返回设置提示。已映射会话后续消息继续提交到同一会话。提供安全的本机绑定流程和解绑方式；不把收到的第一条 QQ 消息自动视为可信绑定。

## 四、消息处理

### 4.1 入站消息

流程：

1. 验证 WebSocket token、事件类型、bot 身份和允许名单。
2. 校验 `message_id`；已处理事件直接忽略或重放已有处理结果，不重复创建 Run。
3. 按用户与会话范围查找已绑定会话；没有绑定时按绑定流程拒绝，不能静默创建可执行会话。
4. 把文本消息段整理为用户正文。
5. 对图片、语音、视频和文件按 OneBot 消息段处理：只从 NapCat 返回的受信位置或经过校验的下载接口获取文件；限制文件类型、体积、下载超时和总附件数；保存到 Cat Chat 受管 uploads 后作为附件提交。
6. 调用 `runs.submit_chat()`。若会话已有活动 Run，按现有 `ActiveRunError` 语义回复用户，不并发创建第二个主 Run。
7. 保存 message ID、Run ID 和投递状态，便于重复事件去重与失败对账。

禁止直接信任 OneBot 事件里任意本地路径。禁止让 NapCat 或远端事件指定 Cat Chat 任意文件路径。

### 4.2 AI 回复和长消息

适配器监听 Run 事件直到终态。成功时从存储读取该 Run 对应的完整 assistant 消息，再发送 QQ；不要只依赖可能被截断或分片的单个 delta。

长回复按 QQ/NapCat 实际消息限制切分：

- 优先在段落、换行和代码块边界切分。
- 单段仍超限时再按安全长度切分，并闭合/重开代码围栏。
- 每段记录发送状态；发送失败可重试未送达部分，不重复发送已确认送达部分。
- 对 Cat Chat 生成的图片、视频等产物，首期可发送可访问的 Cat Chat 媒体链接；链接必须采用现有媒体鉴权规则，并在外网不可达时说明无法访问。

### 4.3 工具调用和确认

工具由现有 Run 和 Executor 执行，OneBot 适配器不直接执行工具。

当 Run 产生待确认工具操作时：

- 通过现有 `confirmation_snapshot()` 获取仍有效的确认项。
- 向发起该会话的已绑定 QQ 私聊发送工具名称、必要的安全摘要、确认 ID 和有效期。
- 只接受绑定用户对该 Run 的确认/拒绝；转交现有 `confirm_tool_async()` / `reject_tool()`。
- 确认 ID 必须绑定 conversation、run 和 QQ 身份，并在超时、Run 终止或重启后失效。
- 不把任意 QQ 消息解释为授权；不提供绕过现有工具权限快照的命令。
- 群聊默认不允许确认工具操作。

若某类确认无法安全映射到 QQ，必须提示用户回 Cat Chat 网页处理，不得自动放行。

## 五、错误和恢复

- WebSocket 断开后按带上限的退避策略重连，并报告连接状态。
- 收到消息后先持久记录，再提交 Run；处理过程中崩溃后能够识别未完成记录，不重复生成或计费。
- Run 失败、模型无回复、附件下载失败、NapCat 发送失败都要留下可查状态，并在 QQ 可达时返回简短原因。
- QQ 投递失败不得把 Cat Chat Run 标成失败；两者状态分开保存。
- 进程重启后恢复未完成的回复投递和有效确认状态；失效确认不恢复为可批准状态。
- 日志不记录 OneBot token、完整私人消息附件 URL 或未经脱敏的凭据。

## 六、实施切片

### S0：协议与本地样例

- 固定 OneBot 版本和 NapCat 实际提供的反向 WebSocket、鉴权及媒体消息段行为。
- 建立本地假 NapCat 服务，用固定事件样例验证连接、鉴权和消息解析。
- 核实 WebSocket 依赖与 PyInstaller 打包方式；增加锁定的构建依赖及必要 hidden import。

### S1：私聊文字最小版

- 配置、反向 WebSocket 生命周期和访问名单。
- 安全绑定一个 QQ 用户与一个 Cat Chat 会话。
- 接收文字、提交 Run、读取完整回复、长文本分段回传。
- 验证重复消息去重、活动 Run 冲突和发送失败恢复。

### S2：工具确认和媒体

- 将有效工具确认映射到 QQ 私聊，完成确认 ID 绑定、超时和拒绝路径。
- 支持 OneBot/NapCat 能力范围内的图片、语音、视频和文件入站；文件先进入受管 uploads，再作为会话附件提交。
- 验证格式、体积、数量、下载超时和失败提示。
- 支持可发送的生成产物回传；不支持的媒体发送能力明确列为限制。

### S3：真实 NapCat 验收

- 用隔离 QQ 账号和本机 NapCat 做私聊收发验收。
- 验证工具确认、长回复分段、至少一种图片附件和一种非图片附件。
- 验证 NapCat 断线重连、Cat Chat 重启、重复事件和 QQ 发送失败恢复。
- 未完成真实 NapCat 验收前，不宣称 QQ 控制功能可用。

## 七、测试

新增单元测试覆盖：

- OneBot 鉴权、事件字段校验、私聊白名单和群聊默认拒绝；
- 会话绑定、模型缺失、conversation 隔离；
- 重复 `message_id` 不重复提交 Run；
- 文本、混合消息段和附件安全落库；
- 完整 assistant 回复读取与长文本分段；
- 工具确认只接受原绑定 QQ 用户，且过期/终态确认不可批准；
- NapCat 发送失败、WebSocket 断线、重连和应用关闭；
- 配置迁移和 token 脱敏；
- PyInstaller `--self-check` 包含 WebSocket 依赖。

自动化测试全部使用本地假 NapCat 和假模型，不访问真实 QQ 或模型供应商。真实 NapCat 验收单独执行。

## 八、待确认项

- 是否首发支持群聊。当前计划默认只开放白名单私聊。
- 是否允许 QQ 用户在 QQ 内确认工具操作。当前计划只允许绑定用户确认，并要求确认 ID 有时效；无法安全确认的操作回网页处理。
- NapCat 与 Cat Chat 是否同机部署。若跨机器，需要确定反向 WebSocket 监听地址、Windows 防火墙和网络访问控制。
- NapCat 对语音、视频和文件消息段的实际下载/回传接口，以固定 OneBot 版本和真实测试为准；计划不假定所有媒体类型都能双向传输。

---

## 九、完成标准

- Cat Chat 能直接接 NapCatQQ，不依赖 AstrBot。
- 已绑定 QQ 用户的私聊消息进入正确的 Cat Chat 会话，AI 完整回复能返回 QQ。
- 工具仍由 Cat Chat 执行，确认和权限沿用现有安全规则。
- 消息去重、附件受管存储、长回复分段、断线重连和投递失败状态均有测试覆盖。
- 真实 NapCat 私聊验收通过，并记录支持的 OneBot 版本、媒体类型和部署限制。

---

## 十、实施记录（2026-11-05）

### 10.1 已交付

| 计划条目 | 实现位置 |
|---|---|
| 3.1 OneBot Channel Adapter | `naiba/channels/onebot.py`（纯解析/分段）、`adapter.py`（入站→Run→投递）、`store.py`（账本）、`service.py`（反向 WS 服务） |
| 3.2 应用装配和生命周期 | `NaibaChatApp.__init__` 末尾装配 `self.onebot`，`stop()` 首项停渠道；独立 `websockets.sync` 线程承载长连接，不占用 `ThreadingHTTPServer` 请求处理函数 |
| 3.3 配置 | `default_config()["onebot"]`、`normalize_onebot()`、`onebot_public()`、`ConfigStore.update_onebot()`；`config.example.json` 同步；旧配置缺键自动补齐 |
| 3.4 QQ 身份与会话 | `ChannelStore`（独立 `data/channels/qq.db`），绑定键含 NapCat bot 账号 + scope + 群号；`bind_conversation` / `bind_new_conversation` 只开放本机 HTTP 入口 |
| 4.1 入站消息 | 先 `record_message`（`message_id` 持久去重）再 `submit_chat`；媒体只走 http(s) 直链或 OneBot 下载接口，魔数校验 + 80MB 上限 + 受管 uploads |
| 4.2 AI 回复和长消息 | 等 Run 终态后从存储读完整 assistant 消息；`split_message()` 按段落/围栏切分并闭合重开；投递逐段落库 |
| 4.3 工具调用和确认 | `confirmation_snapshot()` 同步到 QQ 私聊；凭据绑定 conversation+run+QQ，一次性、有时效、Run 终结即作废；群聊与换人一律拒绝 |
| 五、错误和恢复 | `recover_pending()` 补投递；`recover_incomplete()` 标记「已记录未提交」残留；`void_all_confirmations()` 重启作废旧凭据；QQ 投递状态与 Run 状态分开存 |
| S0 构建依赖 | `naiba-chat.spec` hidden imports、`launcher._run_self_check()`、`release.yml` 锁 `websockets==15.0.1` |

### 10.2 自动化证据

- `tests/test_onebot_protocol.py`（17 项）：事件校验、段白名单、CQ 码、群触发（@ / `/cat` 前缀）、长文本切分与围栏闭合。
- `tests/test_onebot_channel.py`（52 项）：配置迁移与口令脱敏、启用前校验、账本幂等、白名单/群聊默认拒绝、去重、活动 Run 冲突、投递失败与补发、确认时效与一次性、媒体类型/体积校验、反向 WS 路径与口令鉴权、`restart()` 重新武装。
- `tests/test_onebot_e2e.py`（4 项）：真实 `NaibaChatApp` + `ConversationRunManager` + 假 OpenAI 流式服务跑完整私聊闭环（含图片附件落 uploads）。
- `verify/onebot_fake_napcat_smoke.py`：真实反向 WebSocket 传输冒烟，假 NapCat 作为客户端连入、发事件、回动作响应。
- 全量回归 `python -m unittest discover -s tests -q`：**4100 项通过**；`scan_undef_all` / `esm_graph_check` / `tdz_check` / `verify_doc_encoding` 全通过；Playwright 桌面 + 375px 无 pageerror。

### 10.3 未验证（不得宣称可用）

- **S3 真实 NapCat 验收完全未做**：没有用真实 QQ 账号登录过 NapCat，没有验证真实 NapCat 的 `get_image`/`get_file`/`get_record` 返回形态、语音/视频/文件的实际可用性、真实断线重连行为。
- **真实供应商未测**：全链路只跑了本地假 OpenAI 服务与假模型。
- **工具确认只做了账本与转发层验证**：没有在真实 Run 里触发一次待确认工具再经 QQ 批准。
- **PyInstaller 未打包**：`websockets` 只做了 spec/self-check/CI 依赖登记，未实际构建 exe 验证。
- **群聊**：协议层支持 @ 与 `/cat` 前缀触发，但按计划默认关闭，且没有真实群验证。
- **生成产物回传**：仅在服务可被局域网访问时给出带口令的 `/api/file` 链接；仅本机监听时明确回告无法查看，未做真实媒体外发验证。

### 10.4 部署限制

- OneBot 端口默认只监听 `127.0.0.1`；NapCat 与 Cat Chat 不同机时须显式改 `listen_host` 并自行承担端口暴露风险（服务会打警告日志）。
- `access_token` 是 OneBot 专用口令，与 Cat Chat 全局 `access_token` 分开；启用渠道时两者都必须有效。
- 工具确认仅在绑定用户的私聊内有效，群聊不允许确认。

