# 输出格式

## 1. 确认阶段：只交付方案

每组新素材必须先给简洁方案并等待用户确认。本轮不得附带任何 `subject_definitions` 等六字段正式提示词：

```markdown
# 分段大纲

- 总单元数：2
- 生成总时长：27秒
- 阅读顺序：上→下
- 色彩模式：彩色
- P间关系：直接硬切，无重复素材
- 全局素材映射：Picture 1 = 原始第1张；Picture 2 = 原始第2张
- 全局视觉参考分工：Picture 1 = 分镜、构图与未被专门参考覆盖的人物/场景外观；Picture 2 = Character A 的人物外观
- 全局音频映射：无
- 全局声音锚点：S1 = Character A | male, early twenties, medium-low pitch, calm warm voice；S2 = Character B | female, young adult, medium-high pitch, bright clear voice
- 上传规则：所有 P 均完整上传以上素材并保持相同顺序
- 判读依据：中文漫画，右→左、上→下；语义链校验通过
- 待确认歧义：无

1. P01｜12秒｜段名
   - 内容：本段包含的完整对白、动作和视觉落点
   - 战斗动作链：起势 → 攻防路径 → 接触/落空 → 结果与反应
   - 高燃设计：主节奏；主冲击点；选用技巧及各自功能
   - 战斗可读性：人物方位、主运动方向、接触点和结尾状态如何被看清
   - 上传素材：Picture 1 = 原始第1张；Picture 2 = 原始第2张
   - 视觉参考分工：Picture 1 = 分镜、构图与未被专门参考覆盖的人物/场景外观；Picture 2 = Character A 的人物外观
   - 音频素材：无
   - 说话者：S1 = Character A；S2 = Character B
   - 有效台词：33字
   - 时长理由：正常口播约10.5秒，与反应并行，留1.5秒动作落点，12秒够用
   - 分段必要性：首段；若为 P02 起，说明与上一 P 合并会超时、产生无法自然容纳的时空跳转，或损害独立生成
   - 独立边界：首镜自行建立场景与人物；末镜完成台词和动作结果；可直接硬切
   - 后期文字：无
```

方案末尾只写“请确认以上分段、时长、阅读方向、参考映射和战斗设计；确认后我再输出正式 H3 提示词。”然后停止。用户确认前不得预先附词、示例词或第一段提示词。

每个 P 的“战斗动作链”必须覆盖源素材已有的起势、攻击或位移路径、防御/闪避/反制、接触或落空、结果与关键反应；素材未提供的环节不得虚构。“高燃设计”必须明确一个主节奏和一个主冲击点，只选少量服务动作的技巧。“战斗可读性”必须说明双方位置、主要方向、接触点与结果如何被看清，不能只写“保持清晰”。

高速／高燃方案的“高燃设计”还应简述主节拍的具体机位、透视和速度变化，并在正式 Shot 中落实；技巧名称不能替代这些关系。同一组素材的已授权返修沿用既有确认与用户修正，不把技能维护或表现力纠错当作新素材重新索取确认。

`全局视觉参考分工` 必须覆盖每个 Picture，并按人物外观、场景外观、颜色、分镜、构图或镜头顺序说明其职责；不得写画风名称或综合画风。只有漫画分镜图时，明确它同时负责人物与场景外观；有专门人物或场景参考时，对应内容优先服从专门参考，漫画图继续负责分镜叙事及未覆盖部分。存在职责冲突时必须列为待确认歧义。每个 P 的“视觉参考分工”必须逐字复用全局分工，不能在不同 P 中换绑职责。

每个 P 的“上传素材”和“音频素材”必须与全局映射一致；所有 P 均使用完整素材集并保持相同顺序。没有音频时，`全局音频映射` 和每个 P 的`音频素材`均写“无”。有音频时写成 `Audio 1 = 角色A音色；Audio 2 = 角色B音色`，并在所有 P 中逐字复用。每个 P 的“说话者”也重复列出全局全部角色与 S 编号，即使某角色本段不发言；实际发言者由“内容”和正式提示词标明。P02 起的“分段必要性”必须说明为何不能与上一 P 在 15 秒内自然合并；不能只写“内容不同”或“方便生成”。不要输出候选方案、ID账本、逐项算式或长篇检测表，除非用户明确索要详细审计。

## 2. 正式交付阶段：用户确认后输出

先重述确认后的分段大纲，再输出每个 P 的 H3 提示词。

管理标题：

```markdown
## P01｜12秒｜段名｜彩色
```

标题和大纲不是复制进 MiniMax 的提示词。正文严格按以下六个字段排序：

```text
subject_definitions:
...

summary:
...

retention_analysis:
...

detailed_description:
...

overall_soundscape:
...

non_diegetic_music:
N/A
```

除 `<d>` 内的语言标签和原文台词外，字段内容使用英文。`summary` 以 `[reference generation]` 开头；实际上传音频时才写 `[reference generation + audio reference]`。不得使用 `keyframe completion`。

### subject_definitions

- 按全局固定编号定义本 P 上传的每个 Picture 及其用途；没有直接提供本段分镜的图片也要说明其身份、服装或场景连续性用途。
- 定义每个可见角色 `<Subject N>` 的身份、客观外观锚点和参考来源。
- 为说话者绑定稳定 `(Sx)` 和全局声音锚点；有音频时定义每个 `<Audio N>`，并将它绑定到唯一 `(Sx)`。无音频时，在每个该角色实际发声的 P 中逐字复用同一英文声音锚点。
- 定义当前场景。Subject 与 speaker 编号不要求相同。
- Picture 和 Subject 定义只描述人物外观、场景外观、颜色、分镜、构图及镜头职责，不得出现画风、媒介、渲染或审美标签。不得写 `chibi`、`anime style`、`manga style`、`comic style`、`cinematic style`、`realistic style`、`2D style`、`cel-shaded`、`line art` 或同类措辞。

### retention_analysis

可见内容只用 `fully_preserved`、`partially_preserved`、`attribute_transfer`、`weak_reference`。每个已上传 Picture 都必须出现：说明它用于哪些 Shot，或明确以 `weak_reference` 仅维持身份、服装或场景连续性。每个已上传 Audio 也必须单独出现并使用 `reference`，说明它只提供哪个 `(Sx)` 的音色与表达方式，不复制源台词。

只要漫画参考图包含气泡或任何可读文字、伪文字、文字状图形，该 Picture 在 `retention_analysis` 中就必须使用 `partially_preserved`，不得使用 `fully_preserved`；同时明确列出要保留的剧情、动作、构图、镜头或环境职责，以及要排除的气泡和文字元素。

### detailed_description

开头第一句只能根据确认结果二选一：`The target video is black-and-white.` 或 `The target video is in full color.`。随后用客观引用关系说明人物外观、场景外观、分镜和构图分别服从哪些 Picture，并禁止格框、气泡、字幕、水印、Logo 和可读文字。示例：`Character appearance follows <Picture 2>; environment appearance follows <Picture 3>; shot order and composition follow <Picture 1>.` 只有漫画分镜图时可写：`Character and environment appearance follow <Picture 1>; shot order and composition follow its storyboard content.`

六字段 H3 正文中不得输出任何画风、媒介、渲染或审美标签，不得描述或概括参考图的线条、上色、质感、人物比例或造型流派。不得写 `style consistent with <Picture N>`、`match the art style` 或任何同义表达。黑白/彩色之外的视觉呈现完全交给已上传且职责明确的参考图；动作、机位、光线和客观可见事实仍应正常描述。

每个镜头围绕一个主要观看关系组织人物动作、摄影机运动、表演／声音与可见结果；这些信息可同步发生，不写成必须依次完成并停顿的四步。保持漫画事件顺序，每格的关键信息都应有落点，但格数不决定 Shot 数或停留时长。同一动作的相邻姿态可合为连续镜头。

动漫打戏必须把已确认的动作链和高燃设计写进具体 Shot。开场的空间建立可与首次行动同步；高燃段写清机位与主体的相对运动、近远尺度如何改变、爆发与短停怎样衔接，见 [performance-execution.md](performance-execution.md)。不写脱离动作的技巧清单。高速经过可短暂出画、遮挡或抽象化，但关键接触／落空和结果必须可辨。主冲击点只有一个，其他碰撞保持次级层次。具体运动与透视描述允许使用，不把画风标签禁令误解为禁止动画表现手法。

```text
[Shot 1] Independent scene establishment and the first complete beat.

[Shot 2] At 00:04.000, the camera cuts to the next beat.
```

Shot 1 无时间戳；Shot 2 起必须在 `[Shot N]` 后同一行写严格递增且小于总时长的时间戳。P 内可用 `<scenetrans>`，不能跨 P 使用。

台词格式遵循 `dialogue-voice.md`，统一写成 `<d>[Chinese] 原文台词</d>` 等实际语言标签。当前说话者动嘴，其他可见角色闭嘴并对该句反应。内心独白使用 off-screen voiceover，人物嘴始终闭合。最后一个 Shot 必须说完本段对白，并显示动作、揭示或笑点的完成状态。

### 声音

`overall_soundscape` 用 1–4 句英文概括环境声、动作声和非语言人声，不重复台词、不写音乐。`non_diegetic_music` 固定为 `N/A`。

每个 P 都必须在六字段后逐字附加以下固定尾部；它不是官方 H3 字段，但不得改写、增删、翻译或省略：

```text
[Prohibited items]

Text / subtitles / UI / watermarks / logos / corner badges / letters / numbers / symbols / glyphs / pseudo-text / text-like graphic shapes / comic speech bubbles / comic sound-effect marks / real-world UI

[Mandatory declaration]

Clean full-screen animation footage, not a comic page or UI. No background music; keep only ambient sound, human voices, and sound effects. Do not render any subtitles, text, letters, numbers, symbols, glyphs, pseudo-text, watermarks, logos, speech bubbles, or sound-effect marks. Spoken dialogue inside <d> is audio only and must never appear visually.
```

## 3. 交付检查

确认已取得用户对方案的明确确认；全部源格有可识别落点；台词、语言标签和说话者正确；颜色依据正确；战斗动作链完整、空间方向清楚、唯一主冲击点明确且高燃技巧服务源动作；全局视觉参考分工覆盖每个 Picture 且没有职责冲突；六字段正文只声明黑白/彩色而没有任何画风词；所有 P 的 Picture/Audio 映射完全相同；全局 speaker、Audio 绑定和声音锚点稳定；Shot 时间戳使用官方同行格式；固定尾部存在且逐字一致；六字段完整；全部 P 均已输出。随后附 `segmentation-validation.md` 规定的紧凑检测结果。
