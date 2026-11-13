# 输出与确认约定

## 确认方案

每组新素材先给一个简洁、具体的方案，不附六字段正式词。用户已确认的映射或选项直接保留；不要拿同一问题重复确认。用户明确要求跳过方案时按其授权执行，未解决的关键归属或剧情歧义仍需询问。

方案使用以下管理结构，按实际素材填充。无 speaker、Audio 或替换时写“无”，不要为齐格式发明人物或声音。最低需求是可核验的时间估算，不是模型质量保证。

```markdown
# 分段大纲

- 总单元数：1
- 生成总时长：8秒
- 阅读顺序：逐行左→右、从上到下；动作因果校验通过
- 成片形式：真人影像，含整合视效
- 色彩模式：彩色
- P间关系：单 P；若扩展为多 P，直接硬切，无重复素材
- 全局素材映射：Picture 1 = 人物参考；Picture 2 = 漫画分镜
- 全局视觉参考分工：Picture 1 = 人物身份与服装；Picture 2 = 剧情、动作、构图、镜头顺序及环境空间转实景
- 角色替换：源进攻方 → Character A → Picture 1；其余人物按实际记录
- 装备与道具：说明必要装备保留、来源和覆盖部位；非剧情物说明处理
- 环境转换：有实景图则明确来源；无实景图则按漫画空间事实转换
- 表演转换：说明源符号/残像等如何变为表演、连续动作或视效
- 全局音频映射：无
- 全局声音锚点：无
- 上传规则：所有 P 使用同一最终素材集、顺序与编号
- 待确认歧义：无

1. P01｜8秒｜段名
   - 内容：完整动作、对白及可见结果
   - 上传素材：Picture 1 = 人物参考；Picture 2 = 漫画分镜
   - 视觉参考分工：Picture 1 = 人物身份与服装；Picture 2 = 剧情、动作、构图、镜头顺序及环境空间转实景
   - 音频素材：无
   - 说话者：无
   - 有效台词：0字
   - 最低需求：7.4秒
   - 时长理由：按具体动作路径、接触、结果和反应简要说明
   - 分段必要性：首段；后续 P 必须说明无法自然合并的理由
   - 独立边界：首镜建立人物、空间与前提；结尾完成结果
   - 后期文字：无
```

每个 P 的上传素材、视觉参考分工、Audio 映射、全部 speaker 列表沿用全局文本。`全局声音锚点` 用 `S1 = Character A | female, young adult, medium pitch, clear voice`，有音频时用 `S1 = Character A | Audio 1`。非剧情道具处理也应进入方案，不在正式交付时临时决定。

方案末尾写“请确认以上分段、时长、阅读方向、角色替换、装备处理和参考映射；确认后我再输出正式 H3 真人提示词。”确认前停止输出正文，不反复请求已经取得的确认。

## 正式交付

先重述确认后的简洁大纲，包括改编记录和各 P 管理字段，再逐个输出全部 P。标题：`## P01｜8秒｜段名｜彩色真人`。正文用一个代码块容纳该 P 全部字段和固定尾部，便于复制。标题、大纲和检测结果不放进提示词。

六字段顺序固定，字段内容使用英文；只有 `<d>` 中语言标签和原台词使用对应语言：

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

### subject_definitions

- 定义所有上传 Picture 的职责与排除项，包含本 P 没有直接使用分镜的图；不能省略或重新编号。
- 定义可见 `<Subject N>` 的成片身份、客观外观、服装、颜色及参考来源。漫画旧人物被替换时明确排除旧身份与服装。
- 必要装备覆盖单列来源、部位、功能和状态；既能覆盖人物图又不模糊整个人物身份。
- 多视图表描述同一人，灰色摄影背景、排版、标签、非剧情物按方案排除。
- 定义环境的实景参考来源或漫画空间转实景方案。无实景图不声称有真实场景参考。
- 有对白时绑定稳定 `(Sx)` 与声音锚点。Audio 单列并绑定唯一 speaker；无 Audio 不虚构音频，无对白不强行分配 S 编号。

### summary

开头使用 `[reference generation]`；实际上传音频时才用 `[reference generation + audio reference]`。明确 `live-action` 成片与完整动作/对白内容，不使用首尾帧、I2VA、FL2VA、L2VA 或 `keyframe completion` 指令。

### retention_analysis

每个 Picture 单独写职责、对应 Shots 或连续性用途，并用 `fully_preserved`、`partially_preserved`、`attribute_transfer`、`weak_reference` 之一。图有标签、气泡、格框或文字要排除时，不用 `fully_preserved`；漫画改成真人、替换原人物时通常用 `partially_preserved`。`attribute_transfer` 可用于只转移装备等局部属性。

不只写“保留参考”，要说明保留什么、覆盖什么、排除什么。每个 Audio 单独用 `reference`，仅保留音色和表达，不复制参考音频的台词。

### detailed_description

第一句按方案选用：`The target video is black-and-white.` 或 `The target video is in full color.` 随后必须明确 `The target is live-action footage`，允许加 `with integrated visual effects`。

写清人物、装备、环境、分镜分别服从哪些 Picture。无实景图时写真实环境保留漫画空间关系，不笼统要求背景外观“匹配漫画”。不提取绘制线条和上色流派作为目标。明确排除格框、气泡、文字与 UI。

每镜：构图/人物状态 → 主要动作或发言 → 表演/声音 → 可见结果。镜头可合并相邻源格，但每个叙事格有可识别落点。接触近景和结果全景不能无依据变成两次攻击。保持方向、距离、武器归属和碰撞目标可读。

```text
[Shot 1] Establish the independent scene and first complete beat.
[Shot 2] At 00:02.500, the camera cuts to the visible consequence.
```

Shot 1 无时间戳；之后按同行 `[Shot N] At MM:SS.mmm, ...` 格式，严格递增且小于总时长。快速切镜不自动要求加长时长，但不能让动作缺少起势、接触或落点。P 内可用 `<scenetrans>`，P 间不用匹配姿态。

对白用 `<d>[Chinese] 原文台词</d>` 等实际语言标签。当前说话者动嘴，旁人闭嘴并反应；内心独白用 `says in an off-screen voiceover`，对应可见人物嘴保持闭合。不能为真人化擅自加喘息台词或战斗叫喊；非语言呼吸/用力声按实际表演需要克制使用。结尾必须完成本 P 动作、对白和关键反应，不凭烟尘推断伤亡。

### 声音和真人固定尾部

`overall_soundscape` 用 1–4 句英文概括环境、动作声和必要非语言人声，不重复对白、不写音乐。`non_diegetic_music` 固定为 `N/A`。

每个 P 在六字段之后逐字附加下面尾部。使用本 skill 的真人版本，不从动画 skill 复制 `animation footage`。该尾部属于本地交付约定，不是官方第七字段。

```text
[Prohibited items]

Text / subtitles / UI / watermarks / logos / corner badges / letters / numbers / symbols / glyphs / pseudo-text / text-like graphic shapes / comic speech bubbles / comic sound-effect marks / real-world UI

[Mandatory declaration]

Clean full-screen live-action footage, not a comic page or UI. No background music; keep only ambient sound, human voices, and sound effects. Do not render any subtitles, text, letters, numbers, symbols, glyphs, pseudo-text, watermarks, logos, speech bubbles, or sound-effect marks. Spoken dialogue inside <d> is audio only and must never appear visually.
```

## 检测输出

结构与人工语义复核完成后，按 [validation.md](validation.md) 给紧凑结果。没有保存文件时不声称运行过脚本；未实际生成视频时不声称真人呈现、人物身份或动作稳定性已被实测。
