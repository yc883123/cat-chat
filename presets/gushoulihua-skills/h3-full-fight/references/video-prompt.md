# H3 逐段视频提示词格式

前提：逐段剧本和所有待用关键画面图均已获用户确认，所用版本在进度记录中仍然有效。每个视频段 P 对应一个独立的视频生成请求，`[Shot N]` 只表示该段内的镜头，并在各段重新编号。使用下方六字段、英文正文和明确列出的防文字尾部；不采用分镜图门槛、全图集重复上传或将参考图当逐格动作命令的规则。

## 每段先列生成设置与上传清单

设置与清单位于可复制的六字段正文之前，列出**视频段编号、单段时长、画幅、实际输入模式**，再按这一段**实际上传顺序**列出 `Picture 1...N` 和独立的 `Audio 1...M`。例如只有两个人物设定图、一张场景图和本段关键画面图时：

```text
P01 settings: duration = 10s; aspect ratio = 16:9; input mode = Ref2VA.
P01 uploads: Picture 1 = Character A appearance; Picture 2 = Character B appearance; Picture 3 = environment; Picture 4 = P01 approved key composition, occurring around 00:03, not necessarily the first frame.
Audio: none.
```

示例中的时长、画幅与模式都应替换为该段实际设置，与已确认剧本及用户配置一致；上限为 N 秒表示 1–N 秒可自选，单段同时遵守本 skill 的 15 秒上限，不增加“生成时长／保留时长”两套默认设置。Ref2VA 是本流程普通多图参考的默认输入模式；用户明确采用首帧、首尾帧等控制时，设置说明须匹配实际接口与素材职责，不把 summary 的任务关系名称当作输入模式。

上传部分只是单张关键画面、无音频时的编号示例；每段可有多张关键画面。若本段实际上传了声音文件，再在清单中按顺序列出 `Audio 1 = ...`。参考数量变化时按真实文件重排；单段内部所有字段必须同号同义。不同段可使用同一角色与场景图；相邻段需要锁定衔接状态时，也可共用同一张边界画面。除此之外，只上传当前段实际用到的关键画面，避免无关动作渗入。若用户实际接口必须采用另一种上传方式，清单应如实匹配接口，不假装已经上传。

## 正文六字段

```text
subject_definitions:
<Subject 1> is [character A] from <Picture 1>, with [identity, visible costume, weapon].
<Subject 2> is [character B] from <Picture 2>, with [identity, visible costume, weapon].
<Subject 3> is the [environment] from <Picture 3>, with [landmarks and ground].
<Picture 4> is the approved composition reference for [Shot 1] around [relative moment], showing [who is where, pose, distance, weapon/effect state]. It is not required to be the opening frame.

summary:
[reference generation] [One concise sentence giving the independent segment's action, camera intent and how the pictures are used.]

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - [identity and costume/weapon features retained].
<Subject 2> (appears in [Shot 1]): fully_preserved - [...].
<Subject 3> (appears in [Shot 1]): fully_preserved - [scene features retained].
<Picture 4> ([Shot 1] key composition): partially_preserved - [its framing, character relationship and action state inform the corresponding moment without freezing it or requiring an identical first frame].

detailed_description:
[One or two English sentences establishing the user-approved visual style and fast action.]
[Shot 1] [Start state and composition, then actions in playback order. Describe attack source and count, path, reaction, contact or near miss, force and landing; camera movement, chosen effects, synchronized sound and exact approved end state. Locate the referenced key composition at its intended moment.]

overall_soundscape:
[Continuous ambience and physical action sounds.]

non_diegetic_music:
N/A
```

画风开头置于 `[Shot 1]` 之前。2D 模式可用 `The target video is in full color, with fast, hand-drawn 2D anime action.`；用户取消 2D 后，将整句替换为用户要求或指定画风参考的英文表现描述，不沿用固定 2D 开头或 2D 材质限制，也不对最终图片或视频执行画风专项验收。仍从原手法库选择动作、作画与摄影设计，具体表现适配当前画风。

The example shows roles, not mandatory wording. Define only subjects and references that really appear. Character and scene images may be cited within their `<Subject N>` definitions; each approved key composition gets its own `<Picture N>` with a distinct relative time and purpose. When several key pictures guide one segment, list each in `subject_definitions` and `retention_analysis`, then describe their order and the motion between them in `detailed_description`. A shared boundary picture can anchor the preceding segment's end and the following segment's start, with numbering recalculated separately for each request. Do not imply that different reference moments happen simultaneously or that the video must reproduce every picture as an exact frame. Keep the body in English, while dialogue retains its original language in `<d>[Chinese] 原文</d>` or the matching language tag. Use stable `(S1)`, `(S2)` for actual speakers. A user-approved clip may contain an internal hard cut only if that cut was in the approved script; then add `[Shot 2] At MM:SS.mmm, ...` with a time relative to this segment, and maintain the same upload mapping.

## Reference task and audio

- A key composition used loosely as image guidance stays `[reference generation]`, even if it depicts a midpoint or end pose. Describe where it belongs; never say `the shot begins from <Picture N>` unless the user explicitly approved it as the actual opening frame.
- If the user requires an image as an exact first/key/last frame, use `keyframe completion` as appropriate, combined with `reference generation` when character and scene references also apply. Do not silently elevate ordinary composition guidance to exact-frame control.
- For an uploaded audio clip used only for timbre, cadence, rhythm, music style or sound texture, use `[reference generation + audio reference]`, define `<Audio N>`, and give it `reference` in `retention_analysis`. Direct reuse uses `audio reuse` and `fully_copy` or `partially_copy` as actually requested. Audio numbering is separate from Pictures; state which sound layer and which span of this video it governs. Never invent audio labels when there is no uploaded file.
- By default `non_diegetic_music: N/A`. If the user expressly wants music or a music reference, describe its actual role in `non_diegetic_music`; do not hide it under effects. Environmental sound and physical effects belong in `overall_soundscape`, with timed cues in `detailed_description`.

## Fight direction and guardrails

Each prompt independently names the place and current character states. The first visible movement comes from the approved start state, passes the key compositions in their intended order if applicable, then continues to the approved end state. Write concrete action and camera movement between those moments, not only technique labels. Give one main visual/physical peak with supporting beats, unless the script requires another hierarchy. Normal combat remains fast; a brief impact frame, smear or hit-stop should not become seconds of slow motion. Do not reset both fighters to neutral after each exchange. Preserve the exact attack count, who initiated it, its direction and outcome; keep effects attached to their source.

For a segment boundary, end on a readable result with stable character, weapon, and effect states. Open the next segment with the independent establishing/reaction shot chosen in the approved script, while preserving world geography and causality. Use exact first/last-frame matching only when explicitly required or when the action truly cannot be bridged by an independent shot. Every selected animation or camera technique must have an on-screen trigger, visible action, and exit, with its visual execution adapted to the approved style; write what the camera reveals rather than naming a technique. A whole fight shown as an unmoving two-character wide shot with repetitive exchanges fails direction review; an intentional brief locked shot for impact, contrast, or aftermath is allowed.

## 六字段后的固定尾部

在六字段后追加以下两段英文尾部。台词只发声，不呈现在画面。尾部禁止可读文字，不禁止剧情必需的非文字图形，如魔法阵、晶石纹样、徽章或能量轨迹。

```text
[Prohibited items]
No subtitles, captions, speech bubbles, UI, logos, watermarks, or other readable text appear on screen. Spoken dialogue is heard only, never displayed as text.

[Mandatory declaration]
This video uses only environmental ambience, voices, and action sound effects; no background music.
```

若用户要求配乐，将 `non_diegetic_music` 写成实际的配乐方案，并把 `[Mandatory declaration]` 的整句替换为 `This video uses environmental ambience, voices, action sound effects, and the background music described in non_diegetic_music.`；若直接复用音频，还须在六字段内按其实际用途标注 `<Audio N>` 和复用范围。不要复制无配乐声明后又描述配乐。

After output, check segment settings, upload labels, audio role, time, approved actions, continuity record, valid asset versions, and chosen techniques against the script. This is a text check; video performance requires viewing the generated clip. On viewing, check camera movement, spatial and force continuity, attack count, timing, impact, sound, and segment transitions. Check 2D style and animation presentation only when 2D mode is active; if the user cancelled 2D, skip the style-specific acceptance check while retaining the other checks. Route a failure to its cause: wrong image or reference mapping → repair the affected key image or upload list; impossible action density or weak camera design → revise the approved script; prompt ambiguity → revise only the prompt. Update the progress and version record, marking only genuinely affected artifacts and adjacent segments for revision or confirmation according to the main skill. Do not treat a visibly failed fixed-shot fight as successful merely because techniques were named in text.
