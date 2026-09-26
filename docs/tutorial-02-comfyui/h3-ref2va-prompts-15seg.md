# H3 Ref2VA 提示词｜naiba-chat × ComfyUI 3 分钟口播教程（15 段 × 12s）

> 依据《口播文稿-3min.md》重切：原 8 个分镜 → **15 段 × 12 秒 = 180 秒**，每段口播约 40~52 字（中速 240 字/分，含停顿刚好 12 秒）。

## 每段统一生成参数

- 模式：**全能参考 / Ref2VA**（多参考生成）
- 时长：**12 秒**　分辨率档：**0.9**　画幅：**16:9**
- 生成后按 Seg01→Seg15 顺序拼接即得 3:00 成片

## 全局参考素材表（标签全片统一，各段只上传用到的）

| 标签 | 文件 | 作用 |
|---|---|---|
| `<Picture 1>` | `D:\海螺H3提示词工程\naiba-chat讲解\krea2_identity_edit_00022_.png` | 主持人形象（<Subject 1>） |
| `<Picture 2>` | `docs\tutorial-comfyui-3min\images\tut-00-api.png` | 添加 API 表单截图（真实） |
| `<Picture 3>` | `docs\tutorial-comfyui-3min\images\tut-01-main.png` | 开始页截图（真实） |
| `<Picture 4>` | `docs\tutorial-comfyui-3min\images\tut-02-starter-fill.png` | 指令已发出、AI 开始思考截图（真实） |
| `<Picture 5>` | `docs\tutorial-comfyui-3min\images\tut-03-result.png` | 出图结果对话截图（真实） |
| `<Picture 6>` | `docs\tutorial-comfyui-3min\images\tut-04-tasks.png` | 任务面板截图（真实） |
| `<Picture 7>` | `docs\tutorial-comfyui-3min\images\tut-05-agent-tools.png` | Agent 工具集截图（真实） |
| `<Picture 8>` | `docs\tutorial-comfyui-3min\images\tut-06-mcp.png` | 本地 Comfy MCP 卡截图（真实） |
| `<Audio 1>` | `D:\海螺H3提示词工程\素材4\音频\秦西西.mp3` | 主持人声线参考 |

**铁律（已写进每段提示词）**：截图在全息面板上必须 pixel-faithful 呈现，所有中文 UI 文字、布局、聊天气泡与参考图完全一致，禁止编造或改写任何界面元素；红圈/箭头只作为全息标注层叠加在面板上方，不改动截图本体。

> 若生成工具按实际上传顺序重新编号图片，请把提示词里的 `<Picture N>` 同步替换为本段上传顺序号。

## 分段口播文本（最终切分）

| 段 | 时间轴 | 口播（Chinese） | 用到的截图 |
|---|---|---|---|
| 01 | 0:00–0:12 | 不用写一行代码，让 AI 替你操作 ComfyUI 批量出图——就像这样，图片直接挂回聊天里。接下来三分钟，跟着点五下，你也能跑通。 | P5 |
| 02 | 0:12–0:24 | 第一步，配一个在线 API。naiba-chat 本身不带模型，AI 要干活，得先接个大脑。打开右上角设置，点左侧「API 供应商」。 | P2 |
| 03 | 0:24–0:36 | 再点最后那张「添加 API」卡片，按屏幕上的样子填：地址填平台给你的 Base URL，格式选 OpenAI 兼容，Key 到对应平台官网申请。 | P2 |
| 04 | 0:36–0:48 | DeepSeek、OpenAI、Gemini、Claude 都支持。保存之后回到主界面，一定记得在顶栏下拉里选中刚加的模型——不选，消息发不出去。 | P2→P3 |
| 05 | 0:48–1:00 | 第二步，把 ComfyUI 启动起来。naiba-chat 是指挥官，真正画图的是 ComfyUI。像平时一样启动它，保持开着就行。 | 无（全息终端） |
| 06 | 1:00–1:12 | 浏览器能打开本机的八一八八端口，就说明它在跑了。第三步，一键让 AI 接管。打开 naiba-chat，新建会话。 | P3 |
| 07 | 1:12–1:24 | 开始页上有一排现成卡片，点第一排那张「通过 HTTP 调用 ComfyUI」。预设指令立刻发出，不用你打字，也不用回车。 | P3→P4 |
| 08 | 1:24–1:36 | AI 会自己探测 ComfyUI 在不在线，接管后面的出图操作。第四步，用大白话出图。 | P4→P5 |
| 09 | 1:36–1:48 | 比如直接说：用 D 盘 workflows 里的 cover-api 点 json，出六张封面，seed 一到六。这个 json 是 ComfyUI 的 API 格式工作流。 | P5＋字幕条 |
| 10 | 1:48–2:00 | 在 ComfyUI 网页里搭好图，点菜单、工作流、导出 API，存成一个 json 就行。发出去之后，AI 自动把任务提交给 ComfyUI。 | P5＋全息流程图 |
| 11 | 2:00–2:12 | 图片直接挂回消息里，点开看大图；真实文件落盘在 data 下的 generated 目录。批量出图在后台跑，不耽误继续聊天。 | P5 |
| 12 | 2:12–2:24 | 想看进度，点顶栏的「任务」，能看到每张图的进度，也能随时停止。小贴士：如果 AI 说连不上 ComfyUI，九成是它没启动。 | P6 |
| 13 | 2:24–2:36 | 如果提示没有出图工具，到设置、Agent、工具集里，把「ComfyUI 联动」卡套用保存就行。 | P7 |
| 14 | 2:36–2:48 | 想玩进阶的，还可以点开始页「设置本地 Comfy MCP」，让 AI 自己装环境、走 MCP 通道——新手用 HTTP 直连就够了。 | P8 |
| 15 | 2:48–3:00 | 好了，打开你的 naiba-chat，点五下，出图去。 | P5＋结束字卡 |

---
---

## Segment 01｜开场钩子（上传：P1 人设、P5 tut-03、A1 音频）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings with small clover drops, a pink off-shoulder ribbed-knit long-sleeve top with a front bow, and matching pink knit pants.
<Picture 5> is a real software screenshot showing a naiba-chat conversation filled with generated cover images; it is displayed on a holographic panel in [Shot 2] and must be reproduced pixel-faithfully.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] The target video opens a bright tech-tutorial studio scene: <Subject 1> speaks to camera, then a holographic panel rises behind her displaying the exact screenshot content of <Picture 5> under a bold title card. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink off-shoulder knit outfit are retained.
<Picture 5> ([Shot 2] holographic panel content): fully_preserved - the panel renders the screenshot pixel-faithfully with all Chinese UI text, chat bubbles, and embedded cover images identical to the reference, no invented UI.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video uses a bright, clean tech-infomercial style: a white-and-pink creator studio with soft key light and cyan holographic accents.
[Shot 1] A medium shot of <Subject 1> (S1), the young woman from <Picture 1> with her half-tied wavy hair, cat clip, and pink off-shoulder knit top, standing beside a white desk and speaking energetically to camera in the clear lively voice referenced from <Audio 1>: <d>[Chinese] 不用写一行代码,让 AI 替你操作 ComfyUI 批量出图——就像这样,图片直接挂回聊天里.</d> As she speaks, tiny cyan holographic particles gather at her side.
[Shot 2] At 00:06.000, the camera slowly pulls back as a large floating holographic panel unfolds behind her, rendering the exact content of <Picture 5> pixel-faithfully - a flat, undistorted, front-facing display with every Chinese UI label, chat bubble, and cover image identical to the reference screenshot. A glowing title card appears above the panel reading 「不写代码,让 AI 用 ComfyUI 出图」. <Subject 1> (S1) gestures toward the panel and continues, <d>[Chinese] 接下来三分钟,跟着点五下,你也能跑通.</d> She finishes with a confident smile.

overall_soundscape:
Quiet studio room tone throughout; a soft hologram shimmer whoosh when the panel unfolds.

non_diegetic_music:
Light upbeat tech-pop with plucky synths at low volume under the narration, starting bright from the first frame.
```

## Segment 02｜第一步·配 API（上）（上传：P1、P2 tut-00、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 2> is a real software screenshot of the naiba-chat "add API provider" form; it is displayed on a holographic panel in [Shot 2] and must be reproduced pixel-faithfully.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> explains the first setup step in the studio; a holographic panel displaying <Picture 2> pixel-faithfully appears beside her as she points at the settings area. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 2> ([Shot 2] holographic panel content): fully_preserved - the panel renders the screenshot pixel-faithfully, all Chinese form labels and fields identical to the reference, no invented UI.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style with cyan holographic accents.
[Shot 1] A medium shot of <Subject 1> (S1), the young woman from <Picture 1> in the pink off-shoulder knit top, standing in the white-and-pink studio and speaking to camera in the clear lively voice referenced from <Audio 1>: <d>[Chinese] 第一步,配一个在线 API.naiba-chat 本身不带模型,AI 要干活,得先接个大脑.</d> A small glowing brain-shaped hologram icon blinks above her open palm, then dissolves.
[Shot 2] At 00:06.000, a floating holographic panel slides in at her left, rendering the exact content of <Picture 2> pixel-faithfully - a flat, undistorted display with every Chinese label, input field, and button identical to the reference screenshot. <Subject 1> (S1) turns and points at the top-right settings region of the panel while continuing, <d>[Chinese] 打开右上角设置,点左侧「API 供应商」.</d> A thin holographic arrow follows her fingertip to the left sidebar of the displayed interface.

overall_soundscape:
Quiet studio room tone; soft hologram slide-in whoosh and a subtle digital blip on the icon.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume under the narration.
```

## Segment 03｜第一步·填表单三项（上传：P1、P2 tut-00、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 2> is a real software screenshot of the naiba-chat "add API provider" form; it fills the holographic panel in both shots and must be reproduced pixel-faithfully.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> walks through the three key form fields while the holographic panel shows <Picture 2> pixel-faithfully and glowing holographic red circles highlight the URL, format, and key fields as an annotation layer. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 2> ([Shot 1], [Shot 2] holographic panel content): fully_preserved - the screenshot is rendered pixel-faithfully; the red circles are a separate holographic annotation layer floating above the panel and never alter the screenshot content itself.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: <Subject 1> (S1), the young woman from <Picture 1>, stands right of frame while a large floating holographic panel at left renders the exact content of <Picture 2> pixel-faithfully, every Chinese form label and dropdown identical to the reference. She says in the clear lively voice referenced from <Audio 1>, <d>[Chinese] 再点最后那张「添加 API」卡片,按屏幕上的样子填.</d> A glowing red holographic circle draws itself around the address field on the panel.
[Shot 2] At 00:06.000, the camera pushes in slowly toward the panel as two more glowing red holographic circles appear around the format dropdown and the key field, all as an annotation layer above the untouched screenshot. <Subject 1> (S1) points at each circled field in turn and continues, <d>[Chinese] 地址填平台给你的 Base URL,格式选 OpenAI 兼容,Key 到对应平台官网申请.</d> The three circles pulse gently in sync with her pointing.

overall_soundscape:
Quiet studio room tone; three soft digital chimes as each red circle appears.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 04｜保存并选中模型（上传：P1、P2 tut-00、P3 tut-01、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 2> is a real software screenshot of the naiba-chat "add API provider" form, displayed pixel-faithfully on a holographic panel in [Shot 1].
<Picture 3> is a real software screenshot of the naiba-chat start page with its top bar, displayed pixel-faithfully on the holographic panel in [Shot 2].
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> names the supported platforms while the panel shows <Picture 2>, then the panel cross-dissolves to <Picture 3> as she stresses selecting the model in the top bar. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 2> ([Shot 1] holographic panel content): fully_preserved - rendered pixel-faithfully with all Chinese UI text identical to the reference.
<Picture 3> ([Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with all Chinese UI text and cards identical to the reference.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: <Subject 1> (S1), the young woman from <Picture 1>, stands beside the floating holographic panel rendering <Picture 2> pixel-faithfully. Four small glowing holographic badges orbit her hand as she counts them in the clear lively voice referenced from <Audio 1>: <d>[Chinese] DeepSeek、OpenAI、Gemini、Claude 都支持.</d> She then presses a glowing save point on the panel's lower area and continues, <d>[Chinese] 保存之后回到主界面.</d>
[Shot 2] At 00:06.000, the panel cross-dissolves to render <Picture 3> pixel-faithfully - the start page with its top bar, every Chinese label and card identical to the reference screenshot. A pulsing holographic arrow points at the model dropdown in the top bar. <Subject 1> (S1) raises a warning finger and says emphatically, <d>[Chinese] 一定记得在顶栏下拉里选中刚加的模型——不选,消息发不出去.</d> The arrow flashes red once on the final phrase.

overall_soundscape:
Quiet studio room tone; a soft confirm chime on save and a gentle cross-dissolve shimmer.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 05｜第二步·启动 ComfyUI（上传：P1、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> explains starting ComfyUI; an abstract holographic terminal with scrolling green log lines and a glowing commander-versus-painter diagram illustrates her words. No software screenshot appears in this segment. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium shot of <Subject 1> (S1), the young woman from <Picture 1>, speaking to camera in the clear lively voice referenced from <Audio 1>: <d>[Chinese] 第二步,把 ComfyUI 启动起来.</d> Behind her, an abstract holographic terminal window materializes with generic green log lines scrolling upward, clearly a stylized hologram rather than any real interface. A small holographic power icon flips from grey to green.
[Shot 2] At 00:05.000, a simple glowing holographic diagram appears beside her: a tiny conductor figure labeled as the commander connected by a light beam to a paintbrush figure, illustrating command versus painting. <Subject 1> (S1) gestures from the conductor to the paintbrush while continuing, <d>[Chinese] naiba-chat 是指挥官,真正画图的是 ComfyUI.像平时一样启动它,保持开着就行.</d> The paintbrush figure paints a small glowing stroke that lingers as she smiles.

overall_soundscape:
Quiet studio room tone; a low electronic hum from the holographic terminal and a soft power-up blip.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 06｜8188 确认 + 第三步开场（上传：P1、P3 tut-01、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 3> is a real software screenshot of the naiba-chat start page; it is displayed pixel-faithfully on a holographic panel in [Shot 2].
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> confirms ComfyUI is running via a glowing holographic 8188 badge, then introduces the one-click takeover as the panel unfolds showing <Picture 3> pixel-faithfully. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 3> ([Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with every Chinese label, card, and top-bar element identical to the reference screenshot, no invented UI.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium shot of <Subject 1> (S1), the young woman from <Picture 1>, beside an abstract holographic browser window showing only a glowing green badge reading 「127.0.0.1:8188」 with a check mark. She says in the clear lively voice referenced from <Audio 1>, <d>[Chinese] 浏览器能打开本机的八一八八端口,就说明它在跑了.</d> The badge pulses green.
[Shot 2] At 00:06.000, the holographic browser folds away and a large floating panel unfolds rendering the exact content of <Picture 3> pixel-faithfully - the naiba-chat start page with all its Chinese cards identical to the reference. <Subject 1> (S1) brightens and continues, <d>[Chinese] 第三步,一键让 AI 接管.打开 naiba-chat,新建会话.</d> A sparkling holographic plus icon pops over the panel as she says the final words.

overall_soundscape:
Quiet studio room tone; a cheerful check chime on the badge and a hologram unfold whoosh.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 07｜点卡片·指令即发（上传：P1、P3 tut-01、P4 tut-02、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 3> is a real software screenshot of the naiba-chat start page with its starter cards, displayed pixel-faithfully in [Shot 1].
<Picture 4> is a real software screenshot of the naiba-chat conversation right after the preset instruction was auto-sent, showing the sent user bubble and the AI thinking indicator, displayed pixel-faithfully in [Shot 2].
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> points at the first starter card on the panel showing <Picture 3>, then the panel transitions to <Picture 4> showing the instruction instantly sent and the AI starting to think. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 3> ([Shot 1] holographic panel content): fully_preserved - rendered pixel-faithfully with all Chinese cards identical to the reference.
<Picture 4> ([Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with the sent message bubble, the thinking indicator, and all Chinese text identical to the reference.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: the floating holographic panel renders <Picture 3> pixel-faithfully. <Subject 1> (S1), the young woman from <Picture 1>, says in the clear lively voice referenced from <Audio 1>, <d>[Chinese] 开始页上有一排现成卡片,点第一排那张「通过 HTTP 调用 ComfyUI」.</d> Her fingertip taps the corresponding card on the panel and a ring of holographic light ripples out from the exact tap point, leaving the screenshot itself untouched.
[Shot 2] At 00:06.000, the panel cross-dissolves to render <Picture 4> pixel-faithfully - the preset instruction already sent as a chat bubble with the AI thinking indicator below it, all Chinese text identical to the reference. A glowing holographic paper-plane streak flashes from the card area into the sent bubble, visualizing the instant auto-send. <Subject 1> (S1) spreads her hands and continues, <d>[Chinese] 预设指令立刻发出,不用你打字,也不用回车.</d>

overall_soundscape:
Quiet studio room tone; a tap ripple and a quick send-off whoosh on the transition.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 08｜AI 探测接管（上传：P1、P4 tut-02、P5 tut-03、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 4> is a real software screenshot of the naiba-chat conversation with the preset instruction sent and the AI thinking, displayed pixel-faithfully in [Shot 1].
<Picture 5> is a real software screenshot of the naiba-chat conversation with generated cover images, displayed pixel-faithfully at the end of [Shot 2].
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] With the panel showing <Picture 4> pixel-faithfully, a holographic probe animation visualizes the AI checking ComfyUI and taking over; the panel then begins revealing <Picture 5>. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 4> ([Shot 1] holographic panel content): fully_preserved - rendered pixel-faithfully with the sent bubble, thinking indicator, and all Chinese text identical to the reference.
<Picture 5> ([Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with all chat bubbles and cover images identical to the reference.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: the holographic panel renders <Picture 4> pixel-faithfully - the sent instruction bubble with the AI thinking indicator. A holographic probe animation rises above the panel: a small radar ring sweeps and locks onto a green node labeled as the ComfyUI side, visualizing the online check. <Subject 1> (S1), the young woman from <Picture 1>, says in the clear lively voice referenced from <Audio 1>, <d>[Chinese] AI 会自己探测 ComfyUI 在不在线,接管后面的出图操作.</d>
[Shot 2] At 00:07.000, the panel begins sliding to reveal <Picture 5> pixel-faithfully from the top - the conversation with generated covers, identical to the reference - as <Subject 1> (S1) adds brightly, <d>[Chinese] 第四步,用大白话出图.</d> A tiny holographic spark leaps from the radar node toward the panel as the slide completes.

overall_soundscape:
Quiet studio room tone; a radar sweep ping and a panel slide whoosh.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume with a slight lift.
```

## Segment 09｜大白话指令示例（上传：P1、P5 tut-03、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 5> is a real software screenshot of the naiba-chat conversation filled with generated cover images, displayed pixel-faithfully on the holographic panel.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> recites the plain-language command while the panel shows <Picture 5> pixel-faithfully and a holographic subtitle strip displays the command text verbatim. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 5> ([Shot 1], [Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with all Chinese chat text and cover images identical to the reference.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: <Subject 1> (S1), the young woman from <Picture 1>, stands beside the holographic panel rendering <Picture 5> pixel-faithfully. She speaks conversationally in the clear lively voice referenced from <Audio 1>: <d>[Chinese] 比如直接说:用 D 盘 workflows 里的 cover-api 点 json,出六张封面,seed 一到六.</d> As she recites it, a holographic subtitle strip materializes at the bottom of the frame displaying the command text 「用 D 盘 workflows 里的 cover_api.json,出六张封面,seed 一到六」 in clean white type.
[Shot 2] At 00:07.000, the camera pushes in slightly as a small holographic json-file icon unfolds above the panel, showing curly-brace brackets made of light. <Subject 1> (S1) taps the icon and continues, <d>[Chinese] 这个 json 是 ComfyUI 的 API 格式工作流.</d> The icon rotates gently and settles.

overall_soundscape:
Quiet studio room tone; a soft type-on tick as the subtitle strip appears and a gentle hologram unfold tone.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 10｜导出 API 工作流（上传：P1、P5 tut-03、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 5> is a real software screenshot of the naiba-chat conversation with generated covers, displayed pixel-faithfully on the holographic panel in [Shot 2].
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> explains exporting an API-format workflow using an abstract four-step holographic flow diagram, then the panel reveals <Picture 5> pixel-faithfully as the submission result. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 5> ([Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with all Chinese text and cover images identical to the reference.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium shot: <Subject 1> (S1), the young woman from <Picture 1>, stands before an abstract holographic flow diagram of four glowing nodes connected by light lines - a node-graph icon, a menu icon, an export arrow icon, and a json-file icon. She traces the flow with her finger while saying in the clear lively voice referenced from <Audio 1>, <d>[Chinese] 在 ComfyUI 网页里搭好图,点菜单、工作流、导出 API,存成一个 json 就行.</d> Each node lights up as her finger passes.
[Shot 2] At 00:07.000, the diagram folds away and the floating panel rises rendering <Picture 5> pixel-faithfully - the conversation with generated covers, identical to the reference. A small holographic rocket streaks from her palm toward the panel as she continues, <d>[Chinese] 发出去之后,AI 自动把任务提交给 ComfyUI.</d>

overall_soundscape:
Quiet studio room tone; node chimes in sequence and a quick rocket whoosh.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 11｜图片挂回消息（上传：P1、P5 tut-03、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 5> is a real software screenshot of the naiba-chat conversation with generated cover images hanging in the chat, displayed pixel-faithfully on the holographic panel.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> shows the generated images hanging in the chat on the panel displaying <Picture 5> pixel-faithfully, with a holographic folder badge marking the on-disk location. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 5> ([Shot 1], [Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with all Chinese chat text and cover images identical to the reference, no invented UI.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: the holographic panel renders <Picture 5> pixel-faithfully, the conversation with cover images hanging in the message bubbles. <Subject 1> (S1), the young woman from <Picture 1>, points at one embedded image on the panel and a holographic magnifier briefly enlarges it, then she says in the clear lively voice referenced from <Audio 1>, <d>[Chinese] 图片直接挂回消息里,点开看大图.</d>
[Shot 2] At 00:06.000, a small glowing holographic folder badge reading 「data/generated」 docks at the lower corner of the panel, and tiny holographic image thumbnails stream from the panel into it. <Subject 1> (S1) continues, <d>[Chinese] 真实文件落盘在 data 下的 generated 目录.批量出图在后台跑,不耽误继续聊天.</d> She mimes casually continuing to type in the air with a relaxed smile.

overall_soundscape:
Quiet studio room tone; a camera-like zoom click on the magnifier and a soft filing whoosh for the folder badge.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 12｜任务面板 + 贴士一（上传：P1、P6 tut-04、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 6> is a real software screenshot of the naiba-chat task panel showing per-image progress and stop controls, displayed pixel-faithfully on the holographic panel.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> presents the task panel on the holographic display showing <Picture 6> pixel-faithfully, then gives the first troubleshooting tip with a small warning hologram. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 6> ([Shot 1], [Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with all Chinese UI text, progress indicators, and buttons identical to the reference, no invented UI.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: the holographic panel renders <Picture 6> pixel-faithfully - the task panel with per-image progress bars and stop buttons, every Chinese label identical to the reference. <Subject 1> (S1), the young woman from <Picture 1>, traces along one progress bar with her finger and says in the clear lively voice referenced from <Audio 1>, <d>[Chinese] 想看进度,点顶栏的「任务」,能看到每张图的进度,也能随时停止.</d> Her finger hovers over a stop button and a gentle holographic highlight pulses on it.
[Shot 2] At 00:07.000, the panel shrinks to the background and a small holographic warning triangle blinks beside her. <Subject 1> (S1) raises one finger like sharing a secret and continues, <d>[Chinese] 小贴士:如果 AI 说连不上 ComfyUI,九成是它没启动.</d> The warning triangle flips into a green power icon as she finishes.

overall_soundscape:
Quiet studio room tone; a soft progress tick, a warning blip, and a resolve chime.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 13｜贴士二·工具集联动卡（上传：P1、P7 tut-05、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 7> is a real software screenshot of the naiba-chat Agent toolset page with the ComfyUI linkage card, displayed pixel-faithfully on the holographic panel.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> gives the second troubleshooting tip while the holographic panel shows <Picture 7> pixel-faithfully with a red frame annotation around the ComfyUI linkage card. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 7> ([Shot 1], [Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with all Chinese UI text and cards identical to the reference; the red frame is a holographic annotation layer that never alters the screenshot.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: the holographic panel renders <Picture 7> pixel-faithfully - the Agent toolset page, every Chinese label and card identical to the reference. <Subject 1> (S1), the young woman from <Picture 1>, says in the clear lively voice referenced from <Audio 1>, <d>[Chinese] 如果提示没有出图工具,到设置、Agent、工具集里.</d> A thin holographic breadcrumb trail of three glowing crumbs lights up above the panel, echoing the settings path.
[Shot 2] At 00:06.000, a glowing red holographic frame draws itself around the ComfyUI linkage card on the panel as an annotation layer above the untouched screenshot. <Subject 1> (S1) taps the framed card and continues, <d>[Chinese] 把「ComfyUI 联动」卡套用保存就行.</d> A small holographic check mark stamps onto the frame as she smiles.

overall_soundscape:
Quiet studio room tone; breadcrumb blips in sequence and a stamp-like confirm thunk.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 14｜进阶·本地 Comfy MCP（上传：P1、P8 tut-06、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 8> is a real software screenshot of the naiba-chat local Comfy MCP card, displayed pixel-faithfully on the holographic panel.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> introduces the advanced MCP option while the holographic panel shows <Picture 8> pixel-faithfully, closing with a friendly beginner recommendation. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 8> ([Shot 1], [Shot 2] holographic panel content): fully_preserved - rendered pixel-faithfully with all Chinese UI text identical to the reference, no invented UI.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: the holographic panel renders <Picture 8> pixel-faithfully - the local Comfy MCP card with all its Chinese text identical to the reference. A small holographic rocket badge hovers over the panel marking it as advanced. <Subject 1> (S1), the young woman from <Picture 1>, says in the clear lively voice referenced from <Audio 1>, <d>[Chinese] 想玩进阶的,还可以点开始页「设置本地 Comfy MCP」,让 AI 自己装环境、走 MCP 通道.</d> A glowing holographic tunnel of light briefly draws itself from the panel into the distance, visualizing the MCP channel.
[Shot 2] At 00:07.000, the tunnel folds away and the panel settles calmly. <Subject 1> (S1) waves a hand gently and concludes with a warm smile, <d>[Chinese] 新手用 HTTP 直连就够了.</d> A small green holographic badge reading 「HTTP 直连」 blinks once beside her.

overall_soundscape:
Quiet studio room tone; a deep sci-fi tunnel hum that fades into a soft friendly chime.

non_diegetic_music:
The same light upbeat tech-pop bed continues at low volume.
```

## Segment 15｜收尾（上传：P1、P5 tut-03、A1）

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long wavy light-brown hair half-tied with a pink fluffy pom-pom and a white cartoon-cat hair clip, layered silver chain earrings, and a pink off-shoulder ribbed-knit top with matching pink knit pants.
<Picture 5> is a real software screenshot of the naiba-chat conversation filled with generated cover images, displayed pixel-faithfully as the backdrop holographic wall.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1), a clear lively young female Mandarin voice.

summary:
[reference generation + audio reference] <Subject 1> delivers the short closing call-to-action in front of a holographic wall showing <Picture 5> pixel-faithfully, then waves goodbye as an end card appears. <Audio 1> guides her voice timbre.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hairstyle, cat clip, earrings, and pink knit outfit are retained.
<Picture 5> ([Shot 1], [Shot 2] holographic wall content): fully_preserved - rendered pixel-faithfully with all Chinese chat text and cover images identical to the reference, no invented UI.
<Audio 1>: reference - its timbre guides <Subject 1>'s narration without copying the original signal.

detailed_description:
The target video continues the bright tech-infomercial studio style.
[Shot 1] A medium-wide shot: behind <Subject 1> (S1), the young woman from <Picture 1>, a wide holographic wall renders <Picture 5> pixel-faithfully - the conversation full of generated covers, every Chinese label identical to the reference. She faces the camera, counts five fingers quickly with a playful rhythm, and says in the clear lively voice referenced from <Audio 1>, <d>[Chinese] 好了,打开你的 naiba-chat,点五下,出图去.</d>
[Shot 2] At 00:05.000, she waves goodbye with a bright smile as glowing holographic particles gather into an end card beside her reading 「3 分钟,点五下,出图去」. The camera slowly pulls back, the holographic wall and the end card hold in frame, and the scene settles into a clean final composition while the music resolves.

overall_soundscape:
Quiet studio room tone; a cheerful particle shimmer as the end card forms, then a gentle fade.

non_diegetic_music:
The light upbeat tech-pop bed rises slightly for the finale and resolves on a bright final chord.
```

---

## 使用提示

1. 每段独立生成：按段首「上传」清单传素材（人设图 + 该段截图 + 音频），把对应代码块整段粘入提示词框。
2. 若 H3 按上传顺序重排 `<Picture N>` 编号，同步替换段内编号即可（人设图建议始终第一个上传）。
3. 口播语速若偏快导致对白超出 12 秒，可将该段对白末尾的过渡句（如「第四步，用大白话出图。」）挪到下一段开头。
4. 拼接后检查段 4→5、8→9、12→13 三处跨段衔接的动作连续性（手势方向与面板位置已在提示词中保持一致）。
