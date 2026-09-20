# Naiba Chat 2.7.5 Beta

Naiba Chat 是运行在 Windows 本机的通用 AI 自动化工作台。它把在线或本地模型、内置工具、后台任务、Skill、MCP、视觉工具和文件产物统一到一个对话界面中。

## 2.7.5 Beta 主要能力

- **供应商预设与首启引导：新用户「选供应商 → 粘 Key → 开聊」（本次新能力）**：设置 → 模型 → 添加 API 与首次启动引导改为「预设卡片列表 + 表单」（参考 AI Gateway 布局：左边一列供应商卡片，点谁右边就是谁的表单）。内置 **14 条预设**——在线大厂 DeepSeek / Kimi / 智谱 GLM / 通义千问 / OpenAI / Claude / Gemini，中转 **TE 中转（teynex）** / **摆烂中转（bailan.store）**（参数照抄实测可用的卡片：URL、请求格式、推荐模型），本地 Ollama / LM Studio / llama.cpp / Unsloth，以及全手填的「自定义」入口。每条预填名称、API URL、请求格式、推荐模型与 Key 申请入口，**表单上只剩 API Key 一个空**。中转站用户不知道 `https://teynex.com` 这串 URL 通往哪，所以每条预设的引导文案按固定三段式写：**① 这是什么站 → ② 怎么注册 / 充值 → ③ Key 在哪创建、长什么样**，并配「打开注册页」按钮（原则：文案里出现的 URL，界面上必须有一个能点的按钮）；TE 中转的文案还写明两条实测经验——「该站按 max_tokens 预扣费，余额不足报 403 不是配置错，去充值就好」「用 GPT-5 / Codex 系模型请把请求格式改为 codex_responses、URL 末尾补 /v1」。**预设只是回填**，名称 / URL / 请求格式 / 模型全部照旧可改。
- **ComfyUI 批量生成三连修（本次重点修复）**：用户要 10 张图，`comfyui_batch` 当天 6 次提交全部「第 1 段提交失败」秒败，模型连盲试 6 次后只能自写 Python 脚本绕道。排查（只读 sqlite + 1:1 复刻提交路径 POST 实验）抓到两个根因 + 一个放大器，一起修：① **负 seed 不再爆节点上限**——工作流里 rgthree「Seed (rgthree)」节点 `seed=-1`（-1=随机，该节点声明范围 ±2^50，合法）此前被替换成 2^63 级巨数、必超节点 max，被 ComfyUI 整单 400 拒收（同一文件走 MCP 原样提交即成功，20 秒内一胜一败是定位关键）；现在随机值取各常见节点声明范围的**交集 2^50**（核心 KSampler 0~2^64-1 ∩ rgthree ±2^50）。② **shots 参数必须生效**——`shots` 此前只对单 `workflow` 分支生效、`workflow_paths` 分支被静默无视（`workflow_paths + shots=10` 返回 `total=1`，模型自己都发现参数没生效）；现在每个路径重复提交 shots 次，显式 `workflows` 数组 + shots>1 **明确报错**（数组已逐条列出，要重复请复制元素）而不是装没看见。③ **提交失败透传原因**——此前 400 响应里的 node_errors（节点号 / 类型 / 具体校验错误）全部被吞，任务上只剩一句「第 N 段提交失败」，模型无法自诊只能盲试；现在压缩成「HTTP 400 …；节点 20（Seed (rgthree)）：Value … bigger than max …」写进任务错误（上限 600 字符防灌库）。
- **修掉「上下文重置工具显示成功、实际没重置」（本次修复）**：模型调用 `reset_context` 后工具卡片显示「已执行」、返回 `ok:true`，但消息没有分割线、占用圆环照旧——上下文一点没变。根因是一次回归：per-call 上下文副本用浅拷贝派生（原注释写「零拷贝语义」），工具写进运行上下文的 `context_reset` 标记落在副本顶层就永久丢失，宿主收尾读原对象读到空。现在 per-call 视图**读=快照、写=同步回落共享对象**，工具写进运行上下文的状态不再「写完就丢」。
- **验证**：全量单测 **1472 例通过**（含 ComfyUI 新守门：seed 上限与连线引用不动、shots 三分支、数组+shots 报错、400 拒收透传到「Seed (rgthree)」「bigger than max」字样）。ComfyUI 修复经**端到端实证**——修复后的提交路径向真实 ComfyUI 提交 seed=-1 的工作流成功拿到 prompt_id；首启引导与供应商预设经**真实后端浏览器冒烟**（隔离实例 + 无头 Edge，覆盖向导弹出、预设卡片渲染与「不该弹」的负向场景）。

> 本版详细说明与历史版本更新日志见 [CHANGELOG.md](CHANGELOG.md)。

## 开始使用

### 使用 Windows 版本

1. 下载 `naiba-chat-2.7.5-beta-windows-x64.zip`。
2. 解压到一个可写目录。
3. 运行 `naiba-chat.exe`。
4. 在设置中添加在线 API 或本地模型服务。

首次运行会创建本地数据目录。升级时请直接替换程序文件，不要删除原有 `data` 目录和配置文件。

### 从源码运行

需要 Windows、Python 3.11 或更高版本。

```powershell
git clone https://github.com/yc883123/naiba-chat.git
Set-Location naiba-chat
python -m pip install pywebview pystray pillow "mcp==2.1.1" pymupdf "av==18.1.0" python-multipart
python server.py
```

服务默认地址为 `http://127.0.0.1:8765`。

## 模型配置

在“设置 → 模型”中添加供应商，填写：

- API 地址
- API Key（本地服务通常可以留空）
- 模型名称
- 请求格式
- 是否支持图片输入
- 上下文长度和生成参数

本地模型建议先使用界面中的连接测试确认模型目录、文本推理和图片能力。模型是否能稳定执行工具，仍取决于模型自身的工具调用能力和上下文能力。

## ComfyUI 与 MCP

Naiba Chat 默认连接：

```text
ComfyUI HTTP API:  http://127.0.0.1:8188
```

使用前请确保 ComfyUI 已启动，并且目标节点、模型和素材在 ComfyUI 中可用。

当前内置流程接收 ComfyUI **API 工作流**。前端保存的 UI JSON（通常包含 `nodes` 和 `links`）不能直接提交，需要先在 ComfyUI 中导出 API 格式。

任务提交后由宿主完成以下工作：

1. 校验并规范化工作流参数。
2. 向 `/prompt` 提交任务。
3. 轮询任务历史和执行状态。
4. 自动收集图片、视频或音频产物，缓存到受管理数据目录并生成缩略图。
5. 附加到最终消息（中止/取消的轮次也会展示已生成的产物）。
6. 在聊天界面提供预览或下载。

推荐的工作流修改方式：对已有工作流文件，用 `read_file`/`comfyui_prepare_workflow` 读取结构，用 `edit_file` 做局部精确替换（改提示词/seed/尺寸等），再用 `comfyui_batch` 的 `workflow_paths` 提交文件路径；不要整段内联大工作流 JSON。

这条链路不要求模型读取产物本身。需要评价画面、OCR、定位对象或比较图片时，用户应明确提出分析要求；用户明确要求「保存/复制到指定目录」时模型会实际执行。

### MCP 说明

- 应用启动时自动连接所有已启用的 MCP 服务，并对启动时未连上的服务做周期重试。
- MCP 工具以 `mcp__<server>__<tool>` 形式注册进当前会话的可用工具集；会话工具集在首条消息时固化。
- `call_mcp(server, tool, arguments)` 只能调用**当前会话可用集内**的 MCP 工具；若目标工具不在可用集内，会提示用户重开会话、在 Agent 工具勾选里加上该 MCP 服务后再用。
- `register_mcp` 只是把服务登记进配置，其工具会在**重开会话后**进入新会话的可用集，本会话内不会因注册而新增可用工具。

## 工具、Skill 与 MCP 的关系

- **内置工具**：由当前任务意图直接触发，例如文件操作、命令执行、后台任务、ComfyUI 和视觉处理。
- **Skill**：提供特定领域的说明、模板、脚本和检查清单，不决定工具是否可用。
- **MCP**：接入外部工具服务；只有任务明确需要对应 MCP 服务时才会连接或调用。

因此，生成图片不需要先启动短剧 Skill，普通编程、资料整理、自动化处理和媒体任务也使用同一套运行机制。

## 数据与安全

- 对话、设置、附件和任务状态保存在本地数据目录。
- 缓存文件（用户上传与宿主产物的图片/PDF/视频/音频等）统一存于受管数据目录（用户上传 `data/uploads`、生成产物 `data/generated`），设置页可查看合计大小、按时间清理旧缓存（清理为手动操作，会按时间从旧到新删除最旧文件、不区分是否被引用）并配置自动清理阈值（默认 256MB，自动清理只删未被消息引用的最旧文件）。
- API Key 不应提交到 Git；示例配置不包含真实凭据。
- 局域网访问需要访问口令，默认仅本机访问时不要求口令。
- 文件预览只允许应用工作区和受管理数据目录（`data/uploads`、`data/generated`）中的文件。
- 高风险写入、删除、外部发布和凭据操作仍受权限策略保护。

## 自动更新校验

发布资产包含：

- `naiba-chat.exe`
- `naiba-chat-update.json`
- `naiba-chat-2.7.5-beta-windows-x64.zip`

更新器会验证清单中的仓库、提交、文件名和 SHA-256。下载文件还必须是有效的 Windows 可执行文件；任何一项不一致都会终止安装。

## Beta 说明

这是 2.7.5 Beta，适合实际使用和反馈，但仍有以下边界：

- 不内置 ComfyUI、模型权重或第三方生成服务，需用户自行安装和配置。
- 不同模型的工具调用质量差异较大，小型模型可能无法稳定完成长链任务。
- ComfyUI 自定义节点和工作流输入差异很大，复杂模板仍可能需要一次参数适配。
- 自动化任务应在交付前检查最终文件；涉及发布、删除或覆盖的重要操作应保留备份。
- MCP 工具仅在重开会话后才会进入会话可用集；若模型发现 MCP 服务存在但其工具不在当前会话可用范围，会提示用户重开会话。
- 视频抽帧依赖 PyAV（打包版已内置随附的 FFmpeg 运行库）；从源码运行时需要 `pip install av`，缺失时工具会明确提示安装而不是直接崩溃。

## 测试与构建

```powershell
Get-ChildItem public\js\*.js | ForEach-Object { node --check $_.FullName }
python -m unittest discover -s tests -q
$env:NAIBA_BUILD_VERSION = "2.7.5-beta"
python -m PyInstaller --noconfirm --clean naiba-chat.spec
```

静态检查器、浏览器冒烟与数据播种脚本**随仓库发布**在 `verify/`（完整清单与用法见 `项目维护说明（修改代码前必读）.md` §六）：

```powershell
python verify\scan_undef_all.py        # 未定义名扫描（Python 全包）
python verify\esm_graph_check.py       # 前端 ESM 导入/导出图一致性
python verify\tdz_check.py             # 前端顶层求值序（TDZ）候选
node verify\media_markup_check.mjs     # 媒体渲染（无浏览器）
$env:NODE_PATH="%USERPROFILE%\node_modules"
node verify\browser_smoke.cjs          # 浏览器冒烟（需 playwright + Edge）
python verify\ring_usage_smoke.py      # 上下文圆环/提醒端到端冒烟（自编排）
```

浏览器冒烟需要本机 `npm install playwright` 并已安装 Edge；脚本一律用相对项目根的路径，换机器无需改代码。

## 项目地址

- GitHub：<https://github.com/yc883123/naiba-chat>
- Issues：<https://github.com/yc883123/naiba-chat/issues>

