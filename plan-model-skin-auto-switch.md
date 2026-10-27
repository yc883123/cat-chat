# 按模型自动切换外观皮肤计划

> **状态（2026-10-27）：已实施完成。** 18 张皮肤已转 WebP 入库 `public/skins/`，
> 自动/手动模式、映射规则、对话区与工作台双层渲染均已落地；全量 3379 项单测通过，
> 隔离实例冒烟 9/9 通过，浏览器实测对话区/工作台皮肤、18 张预设卡片、模式切换、
> 刷新保持、控件不被遮挡全部符合预期。下文保留设计与决策依据，供后续维护参考。

## 目标

在现有外观设置中增加“自动 / 手动”模式，让用户选择具体模型时可按模型家族与档位自动切换皮肤，同时允许用户随时切换模式并手动选择皮肤。

- **自动模式**：用户主动选择具体模型后，根据模型 ID 识别家族与档位并应用对应皮肤；供应商不参与判断。
- **手动模式**：模型变化不影响当前皮肤，用户通过现有皮肤选择器自行设置。
- 用户可以随时在现有外观设置中切换自动和手动模式。切回自动模式时，立即按当前已选模型同步一次皮肤，之后继续跟随模型主动切换。
- 未识别的模型不改变当前皮肤。

## 皮肤资源与映射清单

### 资源来源（已确认，2026-10-27）

- 皮肤原图以 `D:\naiba-chatexe\workspace\te-image-output\Model _final` 目录中的放大图为最终素材来源。
- **18 张已全部生成**，与 `Model_Scenes` 的序号和名称逐一对得上，内容一致（抽样像素对比差异 < 2/255，属正常放大插值）。
- 放大图文件名带 ComfyUI 序号后缀 `_00001_`（`07_GPT_Sol_Astra_00001_.png`），5120×2880 RGB PNG，合计约 270 MB。
- 该目录名中间含一个空格（`Model _final`），引用路径必须显式加引号。

### 分发格式：WebP（已拍板，2026-10-27）

原 270 MB PNG 直接随安装包分发不现实，已决定转 WebP。转换结果已产出到 `D:\naiba-chatexe\workspace\te-image-output\Model_Skins_webp`：

| 项目 | 数值 |
| --- | --- |
| 格式 | WebP，quality 90，method 6 |
| 分辨率 | 5120×2880 保持不缩放 |
| 单张体积 | 1.1–1.9 MB |
| 合计 | **25.2 MB**（原 269.7 MB，压缩约 10.7 倍） |
| 画质 | PSNR 约 40.5–43.7 dB，肉眼与原图不可分辨 |

**文件名去掉 `_00001_` 后缀**，与 `Model_Scenes` 的原名一致，扩展名为 `.webp`：`01_DeepSeek.webp` … `18_Qwen_Flash.webp`。这样皮肤标识（`01_DeepSeek` … `18_Qwen_Flash`）与文件主名完全相同，映射名称不变。

选 WebP 而非 AVIF 的理由：两者体积接近（AVIF q70 合计 15.9 MB，略小），但 WebP 在 WebView2、各主流浏览器与 Python/Pillow 的解码链路上兼容性更成熟，且本仓库的 `naiba/core/media_types.py` 已登记 `.webp` → `image/webp`（`_MEDIA_MIME_FALLBACK` 会兜底 `application/octet-stream` 导致图片拒渲染的系统）。AVIF 在部分工具链里解码失败（本次实测中图像查看器即无法解码 AVIF 输出），风险不值得为节省约 9 MB 承担。

**入库要求**：转换后必须校验每张能正常解码、尺寸为 5120×2880，并确认换肤后图片可显示（`Content-Type` 为 `image/webp`）。

### 18 个皮肤标识（与文件名一致）

| 序号 | 皮肤标识 | 对应模型家族 | 档位 | 家族词 | 档位词 |
| --- | --- | --- | --- | --- | --- |
| 01 | `01_DeepSeek` | DeepSeek | 标准 | deepseek | — |
| 02 | `02_DeepSeek_Mini` | DeepSeek | 轻量 | deepseek | flash / mini / lite |
| 03 | `03_Gemini_Pro` | Gemini | 高阶 | gemini | pro / ultra |
| 04 | `04_Gemini_Flash` | Gemini | 轻量 | gemini | flash / lite |
| 05 | `05_Claude_Opus_Fable_Mythos` | Claude | 高阶 | claude | opus / fable / mythos |
| 06 | `06_Claude_Haiku_Sonnet` | Claude | 轻量 | claude | haiku / sonnet |
| 07 | `07_GPT_Sol_Astra` | GPT | 高阶 | gpt / chatgpt | sol / astra |
| 08 | `08_GPT_Luna_Terra` | GPT | 轻量 | gpt / chatgpt | luna / terra / mini |
| 09 | `09_Grok_Heavy` | Grok | 高阶 | grok | heavy |
| 10 | `10_Grok_Mini_Fast` | Grok | 轻量 | grok | mini / fast |
| 11 | `11_GLM` | GLM | 标准 | glm / chatglm / zhipu | — |
| 12 | `12_GLM_Flash` | GLM | 轻量 | glm | flash / air |
| 13 | `13_Kimi_Pro` | Kimi | 高阶 | kimi / moonshot | pro |
| 14 | `14_Kimi` | Kimi | 标准 | kimi / moonshot | — |
| 15 | `15_Minimax_M` | MiniMax | 标准 | minimax / hailuo / abab | — |
| 16 | `16_Minimax_M_Flash` | MiniMax | 轻量 | minimax | flash |
| 17 | `17_Qwen` | Qwen | 标准 | qwen / tongyi | — |
| 18 | `18_Qwen_Flash` | Qwen | 轻量 | qwen | flash / lite |

同一角色的多个档位是**独立角色**，不是同一张图的换色。例如 Qwen 与 Qwen Flash 是大小两种造型的 AI 娘，必须分别匹配，不能用一个宽泛的 `qwen` 规则把 Flash 吞成标准版。

## 模型名匹配规则

### 版本号出现在中间不影响匹配

真实模型 ID 里的版本号常常夹在家族词与档位词之间，例如 `gpt-6.1-sol`、`gemini-3.1-pro-preview`、`deepseek-v4.1-flash`、`kimi-k2.7-code`。因此匹配**不做前缀或整串比对**，改为分词后按语义取词：

1. 统一转小写，并在字母与数字交界处插入分隔，使 `gemini31flash` 这类连写拆成 `gemini / 31 / flash`。
2. 按非字母数字字符切分，**丢弃纯数字 token**（版本号、参数量、日期都归入此类）。
3. 在剩余 token 中找家族词（必须作为独立 token 命中，不采用子串包含，避免 `Sakimichan` 这类无关名字误撞）。
4. 再找档位词：先判轻量档，再判高阶档（顺序不能反，否则 `MiniMax-M2-Flash` 会被系列名里的 `m` 抢先判成高阶）。
5. 命中家族但无档位词 → 该家族的标准档；该家族没有标准档图时，按下文“缺档回落”处理。
6. 家族词与档位词都没命中，或家族不在九个之内 → **不换肤**，保持当前皮肤，不猜测相近名称。

### 缺档回落（图片只有 18 张的必然结果）

Gemini、Claude、GPT、Grok 四个家族只有高阶与轻量两张图，没有标准档。DeepSeek、GLM、Kimi、MiniMax、Qwen 有标准档，但没有高阶图。因此：

- 模型带高阶词但该家族无高阶图（如 `deepseek-v4-pro`、`qwen-max`、`kimi-k3-pro`、`MiniMax-M2` 这类带 max/pro 词但家族只有标准档的）→ 回落到该家族**标准档**图。
- 模型带轻量词但该家族无轻量图 → 回落到该家族**标准档**图。
- 模型无档位词 → 有标准档图的家族用标准档，无标准档图的家族用**高阶档**（见下节拍板结果）。

回落必须显式记录在映射表里，并由测试守门，避免后续误以为“高阶词一定切到高阶图”。

### 非对话模型必须排除

`gpt-image-2`、`gpt-image-1.5`、`qwen-image-2.0-pro`、`qwen3-tts-flash`、`grok-imagine*`、`minimax` 视频/音频权重等虽然带有家族词，但属于画图、语音、向量或视频模型。这些模型被选中时**不换肤**，否则会出现“切到画图模型，头像突然变成无关角色”。判定规则：模型名含 image / video / tts / audio / speech / embed / rerank / asr / whisper / sora / imagine / ocr / music / voice 等词时不参与换肤。

### 原型实测结果（2026-10-27，仅验证匹配规则，未改功能代码）

对当前 17 个供应商的实际模型名跑匹配规则，16 个命中，只有 `mimo-v2.5` 未命中（它不属于九个家族，属正确行为）。抽样结果：

| 真实模型 ID | 解析 | 目标皮肤 |
| --- | --- | --- |
| `gpt-5.6-terra` | GPT · 轻量 | `08_GPT_Luna_Terra` |
| `gpt-5.6-sol` / `gpt-6.1-sol` | GPT · 高阶 | `07_GPT_Sol_Astra` |
| `claude-opus-5.5` | Claude · 高阶 | `05_Claude_Opus_Fable_Mythos` |
| `gemini-3.8-flash` | Gemini · 轻量 | `04_Gemini_Flash` |
| `gemini31flash` | Gemini · 轻量 | `04_Gemini_Flash` |
| `deepseek-v4.1-flash` | DeepSeek · 轻量 | `02_DeepSeek_Mini` |
| `deepseek-v4-pro` | DeepSeek · 高阶（无图） | `01_DeepSeek`（回落） |
| `kimi-k3-pro` | Kimi · 高阶 | `13_Kimi_Pro` |
| `kimi-k3` | Kimi · 标准 | `14_Kimi` |
| `MiniMax-M2-Flash` | MiniMax · 轻量 | `16_Minimax_M_Flash` |
| `MiniMax-M2` | MiniMax · 标准 | `15_Minimax_M` |
| `qwen3-tts-flash` | 语音模型 | 不换肤 |
| `gpt-image-2` | 画图模型 | 不换肤 |

### 无档位词默认偏高阶（已拍板，2026-10-27）

`gpt-5`、`gpt-4o`、`grok-4.7`、`claude-latest`、`gemini-2.5` 这类只有家族词、没有档位词的模型，在只有高阶与轻量两档图的家族（GPT / Gemini / Claude / Grok）上**默认使用高阶图**：

| 模型 | 目标皮肤 |
| --- | --- |
| `gpt-5` / `gpt-4o` | `07_GPT_Sol_Astra` |
| `grok-4.7` / `grok-4.6` | `09_Grok_Heavy` |
| `claude-latest` | `05_Claude_Opus_Fable_Mythos` |
| `gemini-2.5`（无 pro/flash 词） | `03_Gemini_Pro` |

有标准档图的家族（DeepSeek / GLM / Kimi / MiniMax / Qwen）不受影响，仍落到各自标准档。用户自己在用的 `grok-4.7`、`kimi-k3`、`glm-4.6` 及三个本地 Qwen 均按此规则处理。

即：判定顺序为 **轻量词 → 高阶词 → 该家族标准档 → 家族高阶档**。最后一步“回落高阶”是本条拍板的结果，必须有测试守门。

## 交互设计

1. 在现有外观设置区域中增加“皮肤跟随模型”的自动 / 手动切换控件，与现有皮肤选项放在一起。
2. 自动模式下，皮肤由当前具体模型决定；用户通过皮肤选择器主动选择皮肤时，同时切换到手动模式，再应用所选皮肤，避免选择结果立即被模型规则覆盖。
3. 手动模式下，现有皮肤选择器行为保持不变；选择皮肤不会改变当前模型或供应商。
4. 从手动切回自动时，读取当前实际选中的模型并立即同步皮肤；若当前模型无法识别，保留现有皮肤并维持自动模式。
5. 自动切换只更改皮肤字段，不覆盖主题模式、字体等其他外观设置。

## 配置与持久化

- 将自动 / 手动模式作为外观设置的一部分持久化，并纳入后端字段校验、默认值、归一化和配置重载。
- **皮肤枚举必须扩容**：当前皮肤只有四个 CSS 配色（`violet` / `ocean` / `rose` / `forest`），后端 `naiba/config.py` 的 `APPEARANCE_SKINS`、前端 `public/js/01-core.js` 的 `SKINS`、`public/index.html` 首屏兜底脚本里的白名单都硬编码了这四个值。三处必须同步扩容并纳入同一份清单，漏一处会导致图片皮肤被静默回落成 `violet`。
- 现有 `violet` / `ocean` / `rose` / `forest` 四个配色皮肤保留，与 18 个图片皮肤并存，用户手动模式仍可选。
- 现有皮肤值继续保存为实际生效皮肤；模式字段只决定皮肤是否跟随模型。
- 自动换肤调用现有外观应用和保存路径，确保刷新页面后模式与当前皮肤一致。
- 当前外观配置保存在实例级配置中，因此同一实例上的客户端会共享模式和皮肤。自动模式下任一客户端主动选模并保存皮肤，可能改变其他客户端看到的外观；首期沿用现有实例级设置范围，不另做设备级存储。

## 渲染通道（已实现）

图片皮肤分两处铺放，共用 `html` 上的同一个 CSS 变量 `--model-skin-image`，层级口径一致：

| 位置 | 图层元素 | 层级处理 |
| --- | --- | --- |
| 对话区 | `.chat-backdrop .model-skin-layer` | 与聊天背景同格（grid-row 2），`cover` + 居中，超出部分由 `.chat-backdrop` 裁掉 |
| 工作台 | `.studio-view .studio-skin-layer` | 绝对定位铺满容器 `z-index: 0`；`.studio-view` 加 `isolation: isolate`，顶栏与各分页容器抬到 `z-index: 1` |

关键约束与取舍：

- **皮肤永远在控件下面**：工作台用 `isolation: isolate` 建立独立堆叠上下文，内容层固定 `z-index: 1`，皮肤层 `z-index: 0` 且 `pointer-events: none`。浏览器实测 `elementFromPoint` 命中顶栏、搜索框、分页按钮的都是真实控件，不是皮肤层。
- **配色皮肤不显示图片层**：`--model-skin-image` 只在模型皮肤时写入，配色皮肤下该变量为空、`data-model-skin` 属性被移除，两层都用 `display: none` 关掉。两类皮肤互斥，不会叠加。
- **与聊天背景互斥（3.9.1 补）**：两者都铺对话区整块背景，同时显示会互相糊住。模型皮肤生效时隐藏对话区的旧背景层（`.chat-bg-surface` / `.chat-bg-blur`），但设置卡缩略图与背景编辑器白板仍显示背景图（否则没法预览取景）。反向也成立：用户去设置聊天背景（选图或点内置背景）就说明他想用背景，此时模型皮肤自动让位、退回最近用过的配色皮肤（记录在 localStorage `naibaChatLastColorSkin`，不硬切紫藤以免改掉用户挑的色相）。只在「确实有背景图」时让位，清空背景或单改取景/强度不触发。
- **工作台容器底色要让位**：`.studio-view` 默认是不透明的 `var(--bg)`，会盖住皮肤层；仅在 `html[data-model-skin="on"]` 时清成透明，配色皮肤下保持原样（视觉零变化）。
- **压暗蒙层**：浅色主题叠 `rgba(255,255,255,.10→.34)`，暗色主题叠 `rgba(9,11,18,.52→.70)`，保证正文与气泡对比度。用伪元素而不是给图片叠 `filter`——后者会让整层参与合成，长会话滚动明显掉帧。
- **不用 `background-attachment: fixed`**：移动端失效且触发整屏重绘。

## 已实施清单（2026-10-27）

| 位置 | 改动 |
| --- | --- |
| `public/skins/*.webp` | 18 张皮肤入库，25.2 MB，文件名与皮肤键一一对应 |
| `naiba/config.py` | `APPEARANCE_MODEL_SKIN_FILES`（18 项）、`APPEARANCE_COLOR_SKINS`、`APPEARANCE_SKINS`（22 项并集）、`APPEARANCE_SKIN_MODES`、`APPEARANCE_SKIN_MODE_DEFAULT`；`normalize_appearance` 与 `update_settings` 支持 `skin_mode`；错误文案改为集合动态生成 |
| `public/js/26-model-skin.js`（新增） | 家族/档位识别与映射唯一真源，纯函数、可直接单测 |
| `public/js/01-core.js` | `MODEL_SKINS`（键 → 文件名 + 中文标签）、`SKINS` 扩到 22 项、`isModelSkin` / `modelSkinImageUrl` / `skinLabel`；`applyAppearance` 写 `--model-skin-image` 与 `data-model-skin`；新增 `applyModelSkinFor`（面板外，即时落库）与 `previewModelSkinFor`（面板内，只预览不落库） |
| `public/js/09-settings.js` | `buildModelSkinCards` 现建 18 张卡片；`activePickerSkin` 统一读取两处选中态；`syncAppearanceControls` 回显模式与卡片 |
| `public/js/15-bind-events.js` | 模式切换（切回自动立即按当前模型同步一次，只预览）、卡片点击（顺手切手动）、配色 radio 点击（清卡片并切手动）、切页回显改走 `syncAppearanceControls`、恢复默认连 `skin_mode` 一起重置 |
| `public/js/07-models-agents.js` | `saveComposerModelSelection`（用户主动选模的唯一入口）末尾调用 `applyModelSkinFor` |
| `public/index.html` | 模式控件 + 配色皮肤区 + 模型皮肤卡片容器；对话区与工作台各加一个皮肤图层；首屏白名单扩到 22 项 |
| `public/styles.css` | 两处皮肤层规则、压暗蒙层、工作台堆叠上下文、卡片网格样式 |
| `naiba/http.py` | 带扩展名的资源缺失时回 404，不再回落成 `index.html`（否则皮肤键写错只会静默破图） |
| `tests/test_appearance.py` | 新增 8 项：18 个皮肤键往返、皮肤文件真实存在、模式默认/往返/拒绝非法值、加载期归一化、部分更新不抹平其它键、错误文案含全部选项 |
| `tests/test_model_skin_mapping.py`（新增） | 10 项，用 node 直接跑前端映射模块，覆盖版本号夹中间、档位不吞并、缺档回落、偏高阶、非对话排除、未知家族、供应商无关、18 个皮肤全可达 |
| `tests/test_static_degraded.py` | 新增 2 项：资源缺失回 404、无扩展名路径仍走 SPA 兜底 |
| `verify/_model_skin_smoke.py`（新增，不入库） | 隔离实例冒烟 9 项，含 18 张 WebP 的 MIME 校验 |

## 当前实现与拟议挂钩点

- `public/js/07-models-agents.js` 的 `saveComposerModelSelection()` 保存输入区选中的具体模型，是响应用户主动选模的主要入口。只在有效模型值发生主动变更后触发自动换肤。
- `public/js/15-bind-events.js` 绑定顶部模型配置档案变化和输入区具体模型变化。实现时应区分两者，避免把供应商或配置档案选择误当成模型家族选择。
- `public/js/07-models-agents.js` 的 `populateModels()` 和 `applyConversationModel()` 会在初始化、刷新目录或切换会话时恢复选择。这些恢复流程不应触发自动换肤；只有用户主动变更，或用户明确从手动切回自动时才同步。
- `public/js/01-core.js` 的 `applyAppearance()` 负责应用皮肤并处理现有缓存；外观保存流程负责持久化。
- `naiba/config.py` 的外观字段校验与 `update_settings()` 需要支持新增的自动 / 手动模式字段及默认值。
- `naiba/app.py` 的 bootstrap 需返回模式设置，供前端初始化。
- `public/index.html` 和 `public/styles.css` 负责在现有外观设置区域增加模式控件及其样式，并遵循现有布局。

## 实施结果（原拟议步骤，8 步全部完成）

1. ✅ 18 张 WebP 已入库 `public/skins/`；`.webp` 走 `image/webp`（已在 `naiba/core/media_types.py` 的 `MIME_BY_EXT`），冒烟实测 MIME 正确、不破图。
2. ✅ 后端 `APPEARANCE_SKINS`（22 项）、前端 `SKINS`、`index.html` 首屏白名单三处同源，并有测试守门文件真实存在。
3. ✅ 外观设置加入自动 / 手动控件，两种模式即时应用并持久化到服务端 `appearance.skin_mode`。
4. ✅ 映射逻辑集中在新模块 `public/js/26-model-skin.js`，含分词、丢数字、家族词独立命中、先轻量后高阶、无档位词升高阶、缺档回落、非对话模型排除。
5. ✅ 点皮肤（配色 radio 或模型卡片）都会切到手动；切回自动时按当前模型立即同步一次（面板内只预览，不提前落库）。
6. ✅ 换肤只在 `saveComposerModelSelection`（用户主动选模的唯一入口）触发；初始化、目录刷新、会话恢复、供应商切换均不经过该入口。
7. ✅ 图片皮肤渲染通道已实现，对话区与工作台双层渲染，与四个配色皮肤互斥共存。
8. ✅ 前后端测试补齐：`tests/test_appearance.py` +8 项、`tests/test_model_skin_mapping.py` +10 项、`tests/test_static_degraded.py` +2 项；全量 3379 项通过。

## 验证

- 自动模式下，用户主动选择已映射模型会切换到对应皮肤；相同模型家族通过不同供应商选择时结果一致。
- 带版本号的模型名（如 `gpt-6.1-sol`）能正确落到家族与档位，版本号变化不需要改规则。
- 同一家族不同档位不会被吞并：`kimi-k3` 与 `kimi-k3-pro`、`MiniMax-M2` 与 `MiniMax-M2-Flash`、`qwen` 与 `qwen-flash` 分别落到不同皮肤。
- 缺档回落生效：`deepseek-v4-pro`、`qwen-max`、`MiniMax-M2` 回落到对应标准档图。
- 无档位词默认偏高阶：`gpt-5`、`gpt-4o`、`grok-4.7`、`claude-latest` 落到各自高阶皮肤。
- 非对话模型（`gpt-image-2`、`qwen3-tts-flash`、`grok-imagine*`）不触发换肤。
- 自动模式下，选择未知模型（如 `mimo-v2.5`）时皮肤保持不变。
- 手动模式下，切换模型不改变皮肤；手动选择皮肤后模式仍为手动。
- 从手动切回自动后，立即按当前模型同步皮肤；未知模型不改变皮肤。
- 页面初始化、刷新模型目录、切换会话及恢复会话模型不会意外触发自动换肤。
- 自动换肤不改变主题模式、字体等其他外观字段；保存后重载能恢复模式和皮肤。
- 图片皮肤与四个配色皮肤可互相切换，刷新后不回落到 `violet`。
- WebP 皮肤加载后 `Content-Type` 为 `image/webp`，不出现破图或 `application/octet-stream` 拒渲染。
- 前端补充相关交互验证；后端补充外观设置字段校验、默认值及持久化测试。运行相关外观与模型选择测试。

## 范围外

- 不按供应商或模型配置档案选择外观。
- 不重命名皮肤标识：WebP 文件主名与 `01_DeepSeek` … `18_Qwen_Flash` 完全一致。
- 不为缺失的档位新增素材（如为 GPT / Gemini / Claude / Grok 补标准档图）。
- 不改变会话模型绑定、全局默认模型或模型目录管理行为。
- 不把自动换肤设置改为每台设备独立；如需设备间隔离，应另行设计存储范围。
- 皮肤不铺到侧栏与设置弹窗：只在对话区与工作台两处主内容区。侧栏是深色导航区，
  铺图会压垮可读性；设置弹窗是操作界面，铺图反而干扰。

## 体积与打包（已拍板）

| 项目 | 数值 |
| --- | --- |
| 原放大图 PNG | 5120×2880，约 13.2–17.4 MB/张，合计 269.7 MB |
| 分发用 WebP | 5120×2880，约 1.1–1.9 MB/张，**合计 25.2 MB** |
| 压缩比 | 约 10.7 倍 |
| 输出目录 | `D:\naiba-chatexe\workspace\te-image-output\Model_Skins_webp` |
| 生成参数 | WebP quality 90，method 6，不缩放分辨率 |

25.2 MB 属于可接受的安装包增量。若后续仍嫌大，可再降到 quality 80（合计约 15.7 MB，PSNR 约 38.5 dB）或缩到 2560 宽；这两项本轮不做，需要时另行确认。

实施时请用同一套转换参数重跑一遍，或在入库前校验 18 个文件的尺寸与格式，避免手工处理出现漏转或尺寸不一致。
