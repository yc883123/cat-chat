"""消息附件汇总与用户轮次文本拼装（纯函数层）。

职责边界（P2 收口后）：
- **媒体采集**（扫工具结果、托管缓存、缩略图、去重、落盘前分桶预截断）归
  ``storage/media_collect.py``——它只在工具产出的那一刻运行，结果写进该次
  ``tool_run["media"]``（原始 result 只有那里可见）。
- 本模块只做**纯计算**：用户轮次模型可见文本（``compose_user_content``）、
  图像意图判定（``_image_intent``）、把各次工具调用的媒体记录汇总成消息级
  ``metadata.attachments``（``union_run_media``）。
- 名单唯一定义在 ``core/media_types.py``；这里只做 re-export 兼容。
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

from naiba.core.media_types import (
    MEDIA_EXTS,
    MEDIA_SHOW_MAX_BYTES,
    is_media_path,
    media_kind_of,
    truncate_by_kind,
)
from naiba.core.paths import normalized_path_key, path_within  # noqa: F401  (re-export for callers)

# 多媒体产物（图片/视频/音频）走消息内产物卡片预览
# （tool_run.media → metadata.attachments → mediaMarkup），不列入"修改文件"总结，
# 避免同一产物出现两套入口。名单唯一定义在 core/media_types.py
# （前端经 /api/bootstrap.media_exts 取同一份）。
MEDIA_PRODUCT_EXTS = MEDIA_EXTS


def _is_media_product_path(raw: str) -> bool:
    """按扩展名判断文件是否属于多媒体产物（图片/视频/音频）。"""
    return is_media_path(raw)


# 仅附件、无文字的用户轮次：模型侧显式说明"用户没写指令"，避免模型自行编造用户诉求。
ATTACHMENT_ONLY_NOTICE = "[用户未输入文字，只发送了以下附件]"
# 同理，只有文件夹没有文字时也用固定提示行（与附件口径一致，不新增"另一种沉默"）。
FOLDER_ONLY_NOTICE = "[用户未输入文字，只发送了以下文件夹]"


def folder_index_lines(indexes: list[dict[str, Any]] | None) -> list[str]:
    """文件夹索引的模型侧清单（与气泡展示层同一份数据，但**完整**）。

    形态（每个文件夹一段）：

        [文件夹] D:\\素材4（共 128 项，图片 96；下列路径均相对该文件夹）
        - a/图1.png
        - b/图2.png
        - （仅列出前 300 项，另有 421 项未列出；需要更多内容可用工具按目录路径读取）

    为什么要给"相对路径 + 绝对基路径"而不是一次性列绝对路径：模型要哪张图时把基路径与
    相对路径一拼就能用现有工具链（读图 / 媒体工具 / 目录工具）打开，
    这也是本需求的原意——用户不必逐个复制文件路径。

    截断口径与 ``storage/media_collect`` 的"分桶预截断"同源：宁可让模型知道自己拿到的是
    不全的清单（末尾自述），也不要它以为"这个目录就这几张图"。
    """
    lines: list[str] = []
    for item in indexes or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        total = int(item.get("total") or 0)
        images = int(item.get("image_count") or 0)
        entries = [entry for entry in (item.get("entries") or []) if isinstance(entry, dict)]
        lines.append(f"[文件夹] {path}（共 {total} 项，图片 {images}；下列路径均相对该文件夹）")
        for entry in entries:
            rel = str(entry.get("rel") or "").strip()
            if rel:
                lines.append(f"- {rel}")
        missing = max(0, total - len(entries))
        if item.get("truncated") and missing:
            lines.append(f"- （仅列出前 {len(entries)} 项，另有 {missing} 项未列出；需要更多内容可用工具按目录路径读取）")
    return lines


def upload_reference_lines(
    uploads: list[dict[str, Any]], *, pdf_tools: bool = True, video_tools: bool = True,
) -> list[str]:
    """用户上传附件的模型侧引用行（_run_chat 与历史重放共用，保证逐字节一致）。

    PDF 附件在 ``pdf_tools=True`` 时追加固定处理指引：提取文本用 read_pdf；扫描版/看图用
    pdf_render_pages 渲染页图后 vision_analyze；细节不清时 pdf_zoom_region 局部放大。
    视频附件在 ``video_tools=True`` 时追加内容分析指引：需要理解视频内容时，probe_video
    读取元信息 → extract_frames 抽帧/生成联系表 → vision_analyze 识别内容；仅要求展示文件时不抽帧。
    该文本条件出现、每轮稳定，不改变非 PDF/非视频会话的前缀。
    会话工具集不含对应工具时传 ``pdf_tools=False`` / ``video_tools=False``：不再指引模型
    调用不存在的工具（系统提示的「PDF / 视频处理策略」段同口径，见 ``run/chat.py``）。

    视频类型判定读 ``core/media_types.py::media_kind_of``（不在此二次维护扩展名名单，
    教训 §九.30）；PDF 分支沿用扩展名判断以保持既有输出逐字节不变。
    """
    lines: list[str] = []
    for item in uploads or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        if path.lower().endswith(".pdf"):
            line = f"[用户上传文件：{path}]"
            if pdf_tools:
                line += (
                    "（PDF 文档：提取文本用 read_pdf；扫描版或需要看图时用 "
                    "pdf_render_pages 渲染页图后调用 vision_analyze；细节不清时用 pdf_zoom_region 局部放大）"
                )
            lines.append(line)
        elif video_tools and media_kind_of(path) == "video":
            lines.append(
                f"[用户上传文件：{path}]"
                "（询问视频内容时：用 probe_video 读取时长与帧率，再用 extract_frames 抽帧"
                "（模式可选 times/interval/keyframes）并生成联系表，然后把联系表或帧图路径"
                "交给 vision_analyze 识别内容；仅要求展示/播放这个文件时不要抽帧）"
            )
        else:
            lines.append(f"[用户上传文件：{path}]")
    return lines


def compose_user_content(
    message: str, uploads: list[dict[str, Any]], *,
    folder_indexes: list[dict[str, Any]] | None = None,
    pdf_tools: bool = True, video_tools: bool = True,
) -> str:
    """用户轮次的模型可见文本（_run_chat 与历史重放共用的唯一拼接口径）。

    非空文字：保持历史口径逐字节不变（``文字 + "\\n" + 引用行``），前缀缓存不受影响；
    纯附件（用户未输入文字）：以固定提示行替代空文字，再接附件引用行，使模型明确
    "本轮只有附件、没有指令"，而不是自行脑补诉求。
    ``pdf_tools`` / ``video_tools`` 透传给 ``upload_reference_lines``
    （会话工具集是否含 read_pdf / extract_frames）。

    ``folder_indexes``：拖入文件夹的路径索引（见 ``folder_index_lines``）。它**追加在附件
    引用行之后**——含附件的旧会话前缀字节因此逐字不变，只有真的带了文件夹的轮次才会多出这一段。
    """
    text = str(message or "")
    lines = upload_reference_lines(uploads, pdf_tools=pdf_tools, video_tools=video_tools)
    folder_lines = folder_index_lines(folder_indexes)
    if not lines and not folder_lines:
        return text
    body = "\n".join([*lines, *folder_lines])
    if text.strip():
        return f"{text}\n{body}"
    notice = ATTACHMENT_ONLY_NOTICE if lines else FOLDER_ONLY_NOTICE
    return f"{notice}\n{body}"


_IMAGE_MEDIA_TERM_RE = re.compile(
    r"(图片|图像|照片|缩略图|位图|图标|png|jpe?g|webp|gif|image|picture|photo|imag"
    r"|(?<![地纸表网草截导流框])图"
    # 视频/音频词（2026-10-06 出卡修复）：媒体词不再只认图片，否则「看看那个视频」
    # 判无意图，枚举类工具（intent_gated）返回的视频/音频一律不出卡。
    r"|视频|影片|音频|音乐|语音"
    # 拉丁扩展名带词边界：防止 "mov" 命中 "remove/movie"、"wav" 命中 "wave" 之类子串误伤。
    r"|\b(?:mp4|webm|mov|m4v|ogv|mp3|wav|flac|ogg|m4a|video|audio)\b)",
    re.IGNORECASE,
)
# 用户"要看到/列出/查找/确认图片"的动作词：与图片词同时命中才判定为图像意图，
# 避免"这张图片是谁画的"这类只是提及图片、并不是要显示的请求被误判。
# 双词同时命中才放行的约束不变——本修复只扩媒体词表，不放松动作词。
# 注意动作词不加单字"听"：「我听说」会误命中；「试听/听听」已覆盖常见诉求。
_IMAGE_VIEW_ACTION_RE = re.compile(
    r"(列出|查看|找找|查找|找到|看看|看一下|看一看|看|显示|展示|预览|确认|查询|打开|发给|给我|浏览|翻看|看图|识图|贴出|放出|播放|试听|听听)",
    re.IGNORECASE,
)


def _image_intent(text: str) -> bool:
    """用户是否明确要求查看/列出/查找媒体（图片/视频/音频；据此决定枚举类工具返回的产物是否显示）。

    必须同时命中"媒体词"与"查看/列出/查找/确认类动作词"，才算意图，减少误伤。
    函数名保留 _image_intent（server.py 在再导出；语义已从"图片"扩展为"多媒体"）。
    结果经 run_context["media_intent"] 传给采集器，由工具声明 ``intent_gated`` 消费
    （不再按工具名硬编码枚举集合）。
    """
    t = str(text or "")
    return bool(_IMAGE_MEDIA_TERM_RE.search(t) and _IMAGE_VIEW_ACTION_RE.search(t))


def union_run_media(
    runs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """把本轮各次工具调用的媒体记录汇总为消息级附件列表（含分桶截断）。

    数据来源是各 run 的 ``media``（由 ``storage/media_collect`` 在工具产出时按声明
    提取并托管缓存）；**不再扫描结果文本**——取消/失败路径的 runs 由事件重建，
    同样带 media，三条收尾路径口径一致。

    去重口径：同来源只留一份；同名（不同来源）优先保留带缩略图的版本。
    截断口径：按类型分桶（图 20 / 视频 8 / 音频 8），返回可自述的 truncated 信息
    （调用方写进 ``metadata.attachments_truncated``，前端渲染提示块）。
    """
    ordered: list[dict[str, Any]] = []
    seen_source: set[str] = set()
    index_by_name: dict[str, int] = {}
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        for record in run.get("media") or []:
            if not isinstance(record, dict):
                continue
            source = str(record.get("source") or "")
            if not source or source in seen_source:
                continue
            name_key = str(record.get("name") or "").strip().lower()
            if name_key and name_key in index_by_name:
                index = index_by_name[name_key]
                if record.get("thumb_path") and not ordered[index].get("thumb_path"):
                    ordered[index] = record
                continue
            seen_source.add(source)
            if name_key:
                index_by_name[name_key] = len(ordered)
            ordered.append(record)
    return truncate_by_kind(ordered)


# ============================================================
# 「指名出卡」（show_media）：意图判定与目标解析（纯函数层）
# ============================================================
# 两道**独立**门槛（计划 2026-10-20 §2.1，任一失败都不产生媒体记录）：
#   ① 用户意图门 `show_media_intent()`：本轮用户原文里确实有「明确要求展示某个媒体」；
#   ② 目标授权门 `media_candidates_from_messages()` + `extract_media_targets_from_text()`：
#      目标只能是会话已记录的媒体，或用户原文里明确给出的工作区内路径/文件名。
# 模型自然语言推理不是安全边界：即使模型误调工具，执行层仍按这两道门 fail closed
# （`tools/providers/core.py::_tool_show_media`），绝不产出半张卡。

# 媒体词（与 `_IMAGE_MEDIA_TERM_RE` 分开维护：那个服务"目录枚举是否附图"的旧语义，
# 语义一旦漂移会顺带改变 list_directory 的行为，计划 §4.1 明确要求不要动它）。
# 「文件/附件/产物/素材」也算媒体词：用户常说「显示这个文件」，目标门会再确认它确实是媒体。
_SHOW_MEDIA_TERM_RE = re.compile(
    r"(图片|图像|照片|缩略图|位图|截图"
    # 裸「图」：排除「地图/图纸/图表/网址/草图/截图/导图/流程图/框图」等非媒体词
    # （与 _IMAGE_MEDIA_TERM_RE 的负向前瞻同口径，避免「这张图是谁画的」之类误判）。
    r"|(?<![地纸表网草截导流框])图"
    r"|视频|影片|录像"
    r"|音频|音乐|语音|录音"
    r"|媒体|多媒体|产物|素材|附件|文件"
    r"|\b(?:image|picture|photo|video|audio|media|file|clip)\b"
    r"|\b(?:mp4|webm|mov|m4v|ogv|png|jpe?g|webp|gif|mp3|wav|flac|ogg|m4a)\b)",
    re.IGNORECASE,
)

# 正向展示动作：把某个**已经存在**的媒体呈现出来。
# 刻意不收「查找/列出/浏览/翻看/确认/查询」——那些是枚举请求，走 list_directory；
# 裸「看看/看一下」只在用户明确给出媒体文件名时收取；没有文件名时仍可能是在问内容。
_SHOW_MEDIA_ACTION_RE = re.compile(
    r"(展示|显示|播放|放出|放出来|放一下|打开|预览|贴出|贴一下|亮出|亮出来|秀一下|调出来"
    r"|给我看看|给我看一下|给我看|让我看看|让我看一下|让我看|发给我|发出来|发我看)"
)
_SHOW_MEDIA_BARE_LOOK_RE = re.compile(r"(?:看看|看一下|看下)")
_SHOW_MEDIA_CONTENT_QUERY_RE = re.compile(r"(?:讲了什么|内容|画面|发生了什么|总结|概括|分析|是什么)")

# 媒体词后紧跟这些「元信息名词」时不算展示请求（要的是路径/大小/内容，不是那张卡）。
_SHOW_MEDIA_NON_TARGET_TAILS: tuple[str, ...] = (
    "路径", "位置", "地址", "目录", "列表", "内容", "代码", "文本", "字节",
    "大小", "时长", "名字", "文件名", "后缀", "格式", "数量", "总大小",
)

# 否定：出现在动作词**之前**（同一句读段内）即视为「不要展示」。
_SHOW_MEDIA_NEGATIONS: tuple[str, ...] = (
    "不要", "不用", "不想", "不需要", "无需", "无须", "不必", "没必要",
    "禁止", "先别", "暂时别", "别", "勿", "甭", "不许", "不准", "不能", "不可",
    "不把", "不将",
)

# 逗号前已完成的其他动作，不能把其否定错误地套到逗号后的展示请求上。
# 「不要删掉这张图，把它展示出来」允许；「不要把那张图，展示出来」仍然拒绝。
_SHOW_MEDIA_OTHER_ACTION_RE = re.compile(
    r"删除|删掉|修改|改动|解释|分析|描述|询问|确认|下载|上传|移动|复制|重命名"
    r"|转码|压缩|裁剪|生成|搜索|查找|执行|运行|问"
)

# 假设/条件：在**同一句**里出现即整句不放行（「如果把视频放出来会怎样」）。
_SHOW_MEDIA_CONDITIONALS: tuple[str, ...] = (
    "如果", "假如", "要是", "万一", "假设", "倘若", "若是", "若", "的话",
    "会怎样", "会怎么样", "会不会", "能否", "是否能",
)

# 引用/转述：说明这句是别人说的，不是本轮用户自己的请求。
_SHOW_MEDIA_REPORTING: tuple[str, ...] = (
    "他说", "她说", "它说", "有人说", "听说", "据说", "提到", "写道", "写着",
    "转述", "复述", "引用", "说过",
)

# 引号对：引号内的动作词不算本轮请求（但引号内的**路径**仍可作为目标，
# 所以只在动作/否定/条件判定上掩码，媒体词判定用原文）。
_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"),
    ('"', '"'), ("'", "'"), ("`", "`"), ("《", "》"),
)

# 句子边界：只按强句读切分。**不按逗号切**——「请把刚才那个视频，展示出来」
# 逗号前后同属一个请求，切开会把合法请求判成无意图。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；;!?\n]+")

# 路径字符（用于把散文中粘连的文件名切出来）：ASCII 路径字符 + CJK。
_MEDIA_EXT_TAIL_RE = re.compile(
    r"\.(?:" + "|".join(ext.lstrip(".") for ext in sorted(MEDIA_EXTS, key=len, reverse=True))
    + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_DRIVE_PREFIX_RE = re.compile(r"[A-Za-z]:[\\/]")

# show_media 的拒绝原因（稳定枚举；模型据它决定「先问用户」还是「换目标」）。
SHOW_MEDIA_REASONS: tuple[str, ...] = (
    "intent_required", "ambiguous", "not_found", "not_media",
    "outside_root", "over_limit", "too_large", "unreadable",
)

# 候选快照上限：超长会话（几百轮、每轮多条媒体）不该把整个历史附件全塞进 RunContext。
SHOW_MEDIA_CANDIDATE_LIMIT = 300


def _mask_quoted(text: str) -> str:
    """把成对引号内的字符替换成等长空格（保持下标对齐，便于逐句对照原文）。"""
    chars = list(text)
    for opener, closer in _QUOTE_PAIRS:
        start = 0
        while True:
            begin = text.find(opener, start)
            if begin < 0:
                break
            end = text.find(closer, begin + 1)
            if end < 0:
                start = begin + 1
                continue
            for index in range(begin, min(end + 1, len(chars))):
                chars[index] = " "
            start = end + 1
    return "".join(chars)


def _showable_media_terms(text: str) -> bool:
    """本句是否有「可被展示的媒体词」（排除「XX的路径/大小/内容」这类元信息请求）。"""
    metadata_end = 0
    for match in _SHOW_MEDIA_TERM_RE.finditer(text):
        if match.start() < metadata_end:
            continue  # 「图片的文件名」里的「文件」属于同一元信息目标，不能再次命中。
        tail = text[match.end():]
        prefix = re.match(r"\s*(?:的\s*)?", tail)
        offset = prefix.end() if prefix is not None else 0
        noun = next((word for word in _SHOW_MEDIA_NON_TARGET_TAILS if tail[offset:].startswith(word)), "")
        if noun:
            metadata_end = match.end() + offset + len(noun)
            continue
        return True
    return False


def _quoted_media_target(text: str) -> bool:
    """引号内只允许作为紧邻展示动作的目标，不把被引话题借给其他请求。"""
    for opener, closer in _QUOTE_PAIRS:
        start = 0
        while True:
            begin = text.find(opener, start)
            if begin < 0:
                break
            end = text.find(closer, begin + 1)
            if end < 0:
                break
            value = text[begin + 1:end].strip()
            tail = text[end + 1:end + 1 + 20].lstrip(" ，,、:：").removeprefix("的")
            head = text[max(0, begin - 20):begin].rstrip(" ，,、:：")
            metadata_tail = any(tail.startswith(word) for word in _SHOW_MEDIA_NON_TARGET_TAILS)
            nearby_action = bool(
                re.search(r"(?:把|将)?\s*$", head)
                and _SHOW_MEDIA_ACTION_RE.match(tail)
            ) or bool(_SHOW_MEDIA_ACTION_RE.search(head[-12:]))
            has_media_target = media_kind_of(value) is not None or _showable_media_terms(value)
            if has_media_target and nearby_action and not metadata_tail:
                return True
            start = end + 1
    return False


def _negation_before(text: str, position: int) -> bool:
    """检查整句否定范围，不因长目标名或目标后的逗号丢掉否定。"""
    clauses = re.split(r"[，,、：:]", text[:position])
    for index in range(len(clauses) - 1, -1, -1):
        clause = clauses[index]
        ends = [
            match.end()
            for token in _SHOW_MEDIA_NEGATIONS
            for match in re.finditer(re.escape(token), clause)
        ]
        if re.search(r"不\s*(?:再|继续|直接|自动)?\s*$", clause):
            ends.append(len(clause))
        if not ends:
            continue
        if index == len(clauses) - 1:
            return True
        scope = clause[max(ends):]
        if _SHOW_MEDIA_ACTION_RE.search(scope) or _SHOW_MEDIA_OTHER_ACTION_RE.search(scope):
            continue  # 前一分句已否定另一个动作；本分句是新的展示请求。
        return True  # 「不要把那张图，展示出来」的否定仍覆盖当前动作。
    return False


def show_media_intent(text: str) -> bool:
    """本轮用户原文是否**明确要求展示**某个已有媒体（show_media 的用户意图门）。

    规则表驱动（计划 §4.1：不用单一包含词正则当承诺）。必须同时成立：

    - 同一句里既有可展示的媒体词，又有**正向**展示动作词；
    - 动作词不在引号内（引用/转述不是请求）；
    - 动作词前没有否定词（不要/别/不用/无需/禁止…），且否定词与动作词之间没有句读；
    - 同一句里没有假设/条件词（如果/假如/要是/若/的话/会怎样…）；
    - 同一句里没有转述标记（他说/听说/提到/写着…）。

    只在**承载用户原始请求的普通主对话 Run** 上调用（`run/chat.py` 装配处），
    不得用路由增强文本、Agent 提示或历史助手文本作为授权来源。
    """
    raw = str(text or "")
    if not raw.strip():
        return False
    masked = _mask_quoted(raw)
    # 分隔符只从 masked 里取，原文按同一组索引切片。引号内的句号/换行已变为空格，
    # 不会再让原文与掩码后的句子数组错位。
    boundaries = [match for match in _SENTENCE_SPLIT_RE.finditer(masked)]
    starts = [0, *(match.end() for match in boundaries)]
    ends = [*(match.start() for match in boundaries), len(masked)]
    for start, end in zip(starts, ends):
        raw_sentence = raw[start:end]
        masked_sentence = masked[start:end]
        if not (_showable_media_terms(masked_sentence) or _quoted_media_target(raw_sentence)):
            continue
        if any(token in masked_sentence for token in _SHOW_MEDIA_CONDITIONALS):
            continue
        if any(token in masked_sentence for token in _SHOW_MEDIA_REPORTING):
            continue
        for match in _SHOW_MEDIA_ACTION_RE.finditer(masked_sentence):
            if _negation_before(masked_sentence, match.start()):
                continue
            return True
        # 文件名明确且只说「看看」时，按展示已有文件处理；显式问内容仍走读取/分析工具。
        bare_look = _SHOW_MEDIA_BARE_LOOK_RE.search(masked_sentence)
        if (
            _MEDIA_EXT_TAIL_RE.search(masked_sentence)
            and bare_look is not None
            and not _negation_before(masked_sentence, bare_look.start())
            and not _SHOW_MEDIA_CONTENT_QUERY_RE.search(masked_sentence)
        ):
            return True
    return False


# ============================================================
# 「精确逐帧」（extract_video_frames_exact）：用户意图判定（纯函数层）
# ============================================================
# 这是一道**授权门**，不是"语义建议"：模型传什么参数都不能自证意图，只有承载本轮
# 用户原文的普通主对话 Run 能置位（计划 2026-10-22 §4.2）。判定必须 fail closed：
# 无法判断、含否定、只在附件/引用里出现「逐帧」字样 → 一律不放行。
#
# 设计取舍（与 show_media_intent 同构，但规则方向相反）：
# show_media 是"有正向动作词才放行"；逐帧是"必须明确说出连续/每帧语义才放行"，
# 因为它的代价是整段视频的每一帧落盘（几百到几千张 PNG），误判成本远高于一次出卡。
_FRAME_INTENT_STRONG_TERMS: tuple[str, ...] = (
    "逐帧", "每一帧", "每个帧", "每帧", "逐格", "每一格", "每个格",
    "连续帧", "全部帧", "所有帧", "整段帧", "一帧不漏", "一帧不落",
    "不要漏帧", "不漏帧", "帧序列", "帧号",
    "frame by frame", "every frame", "all frames",
)
# 排除「逐帧」被当作"不要"对象的表述：这些词出现在**同一句**时整句不放行
# （「不要逐帧」「不需要逐帧」是明确否定）。
_FRAME_INTENT_NEGATIONS: tuple[str, ...] = (
    "不要", "不用", "不想", "不需要", "无需", "无须", "不必", "没必要",
    "禁止", "别", "勿", "甭", "不许", "不准", "不能", "不可", "算了",
)
# 概览类请求：即使句子里出现「帧」也只走抽样（extract_frames），不进精确工具。
_FRAME_INTENT_SAMPLING_TERMS: tuple[str, ...] = (
    "抽几帧", "抽一些帧", "抽几格", "关键帧", "概览", "快速看", "大致看",
    "切镜点", "切镜", "转场", "代表帧",
)
# 引用/转述：别人说的不算本轮用户请求（同 show_media 口径）。
_FRAME_INTENT_REPORTING: tuple[str, ...] = (
    "他说", "她说", "它说", "有人说", "听说", "据说", "提到", "写道", "写着",
    "转述", "复述", "引用", "说过", "原文里",
)


def video_frames_intent(text: str) -> bool:
    """本轮用户原文是否**明确要求**逐帧/连续帧抽取（extract_video_frames_exact 的门）。

    只在该工具已 opt-in 的普通主对话 Run 上调用（`run/chat.py` 装配处）。规则：

    - 同一句里要有强逐帧词（逐帧/每一帧/连续帧/逐格/一帧不漏…）；
    - 该句不含否定词（不要逐帧/不需要逐帧 → 拒绝）；
    - 该句不含抽样/概览词（抽几帧/关键帧/概览 → 走 extract_frames）；
    - 该句不含转述标记（他说/原文里/提到…）；
    - 动作词与词本身都不在引号内（`_mask_quoted` 已把引号内容压成空格）。

    无法判断时返回 False（fail closed）。附件里的指令、工具输出、模型自述都不作为来源。
    """
    raw = str(text or "")
    if not raw.strip():
        return False
    masked = _mask_quoted(raw)
    boundaries = [match for match in _SENTENCE_SPLIT_RE.finditer(masked)]
    starts = [0, *(match.end() for match in boundaries)]
    ends = [*(match.start() for match in boundaries), len(masked)]
    for start, end in zip(starts, ends):
        sentence = masked[start:end]
        if not any(term in sentence for term in _FRAME_INTENT_STRONG_TERMS):
            continue
        if any(term in sentence for term in _FRAME_INTENT_NEGATIONS):
            continue
        if any(term in sentence for term in _FRAME_INTENT_SAMPLING_TERMS):
            continue
        if any(term in sentence for term in _FRAME_INTENT_REPORTING):
            continue
        return True
    return False


def _is_http_url(value: str) -> bool:
    return str(value or "").strip().lower().startswith(("http://", "https://"))


def _source_name(source: str) -> str:
    """来源的展示名（本地路径取文件名；URL 优先取 ?filename=，其次取路径尾段）。"""
    text = str(source or "").strip()
    if not text:
        return ""
    if _is_http_url(text):
        try:
            parsed = urllib.parse.urlsplit(text)
        except ValueError:
            return ""
        filename = (urllib.parse.parse_qs(parsed.query).get("filename") or [""])[0]
        return Path(filename).name if filename else Path(parsed.path or "").name
    return Path(text).name


def _candidate_key(source: str) -> str:
    """候选去重键：本地路径按真实目标归一；URL 按原串（计划 §2.3.1）。"""
    text = str(source or "").strip()
    if not text:
        return ""
    if _is_http_url(text):
        return "url:" + text
    try:
        # 请求目标同样经过 resolve；8.3 短名、目录联接等别名必须按同一真实来源比较。
        # normalized_path_key 的通用契约只做词法归一，因此仅在媒体授权处解析。
        return normalized_path_key(Path(text).expanduser().resolve())
    except (OSError, ValueError):
        return ""


def _media_record(source: str, name: str = "", thumb_path: str = "") -> dict[str, Any] | None:
    """构造一条候选记录（非媒体来源返回 None）。"""
    text = str(source or "").strip()
    if not text:
        return None
    kind = media_kind_of(text) or media_kind_of(name)
    if not kind:
        return None
    return {
        "kind": kind,
        "name": str(name or "").strip() or _source_name(text),
        "source": text,
        "thumb_path": str(thumb_path or ""),
    }


def media_candidates_from_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """会话已记录的媒体候选（唯一来源：消息 ``metadata.attachments`` + ``tool_runs[].media``）。

    **不扫工作区、不查其他会话、不搜宿主全局 generated、不凭 basename 到磁盘猜路径**。
    返回 ``[{kind, name, source, thumb_path}]``，按规范化来源去重、保持出现顺序。
    """
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            continue
        records: list[Any] = []
        attachments = metadata.get("attachments")
        if isinstance(attachments, list):
            records.extend(attachments)
        for run in metadata.get("tool_runs") or []:
            if not isinstance(run, dict):
                continue
            media = run.get("media")
            if isinstance(media, list):
                records.extend(media)
        for record in records:
            if not isinstance(record, dict):
                continue
            entry = _media_record(
                str(record.get("source") or record.get("path") or ""),
                str(record.get("name") or ""),
                str(record.get("thumb_path") or ""),
            )
            if entry is None:
                continue
            key = _candidate_key(entry["source"])
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(entry)
    return ordered


def _is_path_char(ch: str) -> bool:
    if ch in "\\/:._-~":
        return True
    if ch.isascii() and ch.isalnum():
        return True
    code = ord(ch)
    return 0x3400 <= code <= 0x4dbf or 0x4e00 <= code <= 0x9fff or 0xf900 <= code <= 0xfaff


def _media_path_tokens(text: str) -> list[str]:
    """从散文里切出「以受支持媒体扩展名结尾」的路径串（保持出现顺序，最多 20 条）。"""
    tokens: list[str] = []
    for match in _MEDIA_EXT_TAIL_RE.finditer(str(text or "")):
        start = match.start()
        while start > 0 and _is_path_char(text[start - 1]):
            start -= 1
        tokens.append(text[start:match.end()])
        if len(tokens) >= 20:
            break
    return tokens


def _is_cjk(ch: str) -> bool:
    code = ord(ch) if ch else 0
    return 0x3400 <= code <= 0x4dbf or 0x4e00 <= code <= 0x9fff or 0xf900 <= code <= 0xfaff


def _token_forms(token: str) -> list[str]:
    """一个粘连路径串的候选写法（按「越具体越优先」排序，去重保序）。

    散文里的路径常与中文粘在一起（``把D:\\out\\a.png展示出来``、``把根图.png放出来``）：
    先按盘符/UNC 锚点截，再试原串，最后**逐字**剥掉开头的 CJK
    （只剥一个字、剥两个字……最多 8 次）——一次剥掉整段 CJK 会把文件名里的中文
    （``根图.png``）也一起剥掉，于是永远解析不到。

    带目录的路径绝不退成尾段或裸文件名：原路径不存在或越界时，不能授权另一份同名文件。
    """
    forms: list[str] = []
    drive = _DRIVE_PREFIX_RE.search(token)
    if drive:
        value = token[drive.start():].strip().strip("，。；：、！？（）()【】「」『』“”‘’")
        return [value] if value else []
    unc = token.find("\\\\")
    if unc >= 0:
        value = token[unc:].strip().strip("，。；：、！？（）()【】「」『』“”‘’")
        return [value] if value else []
    forms.append(token)
    head = token
    for _ in range(8):
        if not head or not _is_cjk(head[0]):
            break
        head = head[1:]
        if head:
            forms.append(head)
    ordered: list[str] = []
    for form in forms:
        value = form.strip().strip("，。；：、！？（）()【】「」『』“”‘’")
        if value and value not in ordered:
            ordered.append(value)
    return ordered[:16]


def _resolve_declared_path(
    token: str, workspace: str | Path | None
) -> Path | None:
    """把一个散文路径串解析成**工作区内真实存在的普通文件**；解析不到返回 None。"""
    for form in _token_forms(token):
        candidate = Path(form).expanduser()
        if not candidate.is_absolute():
            if not str(workspace or "").strip():
                continue
            candidate = Path(str(workspace)) / candidate
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if not media_kind_of(resolved.name):
            continue
        if str(workspace or "").strip():
            try:
                root = Path(str(workspace)).expanduser().resolve()
            except (OSError, ValueError):
                continue
            if not path_within(resolved, root):
                continue
        try:
            if not resolved.is_file():
                continue
        except OSError:
            continue
        return resolved
    return None


def extract_media_targets_from_text(
    text: str, workspace: str | Path | None = None
) -> list[dict[str, Any]]:
    """本轮用户原文里**明确给出**的工作区媒体路径（目标授权门的第二个来源）。

    只认「原文里真的写出来的路径/文件名」：不做目录扫描、不按 basename 到磁盘猜路径。
    相对路径按本轮冻结工作区 ``workspace`` 解析；解析后必须仍在工作区内（符号链接按
    真实目标判定）；必须是存在的普通文件且扩展名属于受支持媒体类型。
    """
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in _media_path_tokens(text):
        resolved = _resolve_declared_path(token, workspace)
        if resolved is None:
            continue
        key = _candidate_key(str(resolved))
        if not key or key in seen:
            continue
        seen.add(key)
        entry = _media_record(str(resolved), resolved.name)
        if entry is not None:
            targets.append(entry)
    return targets


def build_show_media_candidates(
    messages: list[dict[str, Any]] | None,
    message: str,
    workspace: str | Path | None = None,
) -> list[dict[str, Any]]:
    """一轮 Run 的 ``run_context["show_media_candidates"]`` 快照（run/chat 装配处唯一入口）。

    会话已记录媒体（``metadata.attachments`` / ``tool_runs[].media``）
    ＋ 本轮用户原文里明确给出的工作区内媒体路径，按规范化来源去重；
    只保留媒体类型，最多 ``SHOW_MEDIA_CANDIDATE_LIMIT`` 条（保留最新的那一批）。
    """
    pool = media_candidates_from_messages(messages)
    seen = {_candidate_key(item["source"]) for item in pool}
    for target in extract_media_targets_from_text(message, workspace):
        key = _candidate_key(target["source"])
        if not key or key in seen:
            continue
        seen.add(key)
        pool.append(target)
    if len(pool) > SHOW_MEDIA_CANDIDATE_LIMIT:
        pool = pool[-SHOW_MEDIA_CANDIDATE_LIMIT:]
    return pool


def _show_shown(record: dict[str, Any]) -> dict[str, Any]:
    """成功结果（原始形态，供采集器按 ``structured`` 声明读出 source/thumb_path）。

    本地来源在这里再走一次 ``_local_file_reason``：候选可能是**早就被缓存清理掉**的
    老附件，直接回 ``shown`` 会让模型对用户说"已经展示了"，而采集器落地校验把这条丢掉、
    用户什么也看不到（静默失败）。宁可回 ``not_found``/``unreadable`` 让模型如实说明。
    """
    source = str(record.get("source") or "")
    if source and not _is_http_url(source):
        reason = _local_file_reason(Path(source))
        if reason:
            return _show_skipped(reason, name=str(record.get("name") or ""))
    return {
        "ok": True,
        "status": "shown",
        "kind": str(record.get("kind") or media_kind_of(source) or ""),
        "name": str(record.get("name") or "").strip() or _source_name(source),
        "source": source,
        "thumb_path": str(record.get("thumb_path") or ""),
    }


def _show_skipped(reason: str, choices: list[str] | None = None, name: str = "") -> dict[str, Any]:
    """拒绝结果（稳定原因枚举 + 安全的候选展示名，供模型追问）。"""
    value = reason if reason in SHOW_MEDIA_REASONS else "not_found"
    payload: dict[str, Any] = {"ok": False, "status": "skipped", "reason": value, "name": name}
    safe = []
    for item in choices or []:
        text = str(item or "").strip()
        if text and text not in safe:
            safe.append(text)
    if safe:
        payload["choices"] = safe[:8]
    return payload


def _local_file_reason(path: Path) -> str:
    """本地目标的存在性/类型/体积预检（**只读 stat**，不读全文、不复制、不建缩略图）。

    返回空串表示通过；否则返回 ``not_found`` / ``not_media`` / ``too_large`` / ``unreadable``。
    """
    try:
        if not path.exists():
            return "not_found"
        if not path.is_file():
            return "not_media"
        if not media_kind_of(path.name):
            return "not_media"
        size = path.stat().st_size
    except OSError:
        return "unreadable"
    if size > MEDIA_SHOW_MAX_BYTES:
        return "too_large"
    return ""


def _find_candidate_by_source(
    pool: list[dict[str, Any]], source: str
) -> dict[str, Any] | None:
    key = _candidate_key(source)
    if not key:
        return None
    for record in pool:
        if _candidate_key(str(record.get("source") or "")) == key:
            return record
    return None


def _local_target(
    raw: str, workspace: str | Path | None
) -> tuple[Path | None, str]:
    """把工具参数解析成本地路径；返回 ``(path, reason)``，二者必有其一为空。"""
    text = str(raw or "").strip()
    if not text:
        return None, "not_found"
    if _is_http_url(text):
        return None, "not_found"
    try:
        candidate = Path(text).expanduser()
    except (OSError, ValueError):
        return None, "unreadable"
    if not candidate.is_absolute():
        if not str(workspace or "").strip():
            return None, "not_found"
        candidate = Path(str(workspace)) / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None, "unreadable"
    # 越界判定先于存在性：符号链接按真实目标判定，只要解析后不在工作区内就一律拒绝
    # （否则「工作区里放个指向 C:\ 的链接」就成了读任意路径的后门）。
    if str(workspace or "").strip():
        try:
            root = Path(str(workspace)).expanduser().resolve()
        except (OSError, ValueError):
            root = None
        if root is not None and not path_within(resolved, root):
            return None, "outside_root"
    reason = _local_file_reason(resolved)
    if reason:
        return None, reason
    return resolved, ""


def _looks_like_path(raw: str) -> bool:
    text = str(raw or "").strip()
    if not text:
        return False
    if _DRIVE_PREFIX_RE.match(text):
        return True
    if text.startswith(("\\\\", "~/", "~\\")):
        return True
    return "/" in text or "\\" in text


_SENTENCE_MARKERS = "，,。；;！!？?、\n\t"

# 一句「请求」比一条路径长得多；超过这个长度一律按句子处理（真实文件名极少这么长）。
_SENTENCE_MAX_CHARS = 60


def _is_sentence_like(raw: str) -> bool:
    """输入像「一句话」而不是路径/文件名（含空白、句读，或明显过长）。

    存在的理由（实测）：`paths` 是字符串数组、而模型又不知道「刚才那个视频」该写成什么，
    于是会把用户整句话原样传进来（``paths: ["把刚才那张图展示出来"]``）。那种输入里常带
    空格或中文句读，按路径解析必然 ``not_found``——本函数只用来**改走文本解析**，
    不改变真路径的判定（真路径仍由 `_local_target` 逐条校验）。
    """
    text = str(raw or "").strip()
    if not text:
        return False
    if len(text) > _SENTENCE_MAX_CHARS:
        return True
    if any(ch.isspace() for ch in text):
        return True
    return any(mark in text for mark in _SENTENCE_MARKERS)


def _kinds_mentioned(text: str) -> set[str]:
    """文本里提到的媒体类型（用于「刚才那个视频」这类代词的按类型过滤）。"""
    value = str(text or "").lower()
    kinds: set[str] = set()
    if re.search(r"(视频|影片|录像|video|clip|mp4|webm|mov|m4v|ogv)", value):
        kinds.add("video")
    if re.search(r"(音频|音乐|语音|录音|audio|music|mp3|wav|flac|ogg|m4a)", value):
        kinds.add("audio")
    # 裸「图」也要认（「把刚才那张图展示出来」是最常见的说法），负向前瞻与
    # `_SHOW_MEDIA_TERM_RE` 同口径，避免「地图/图纸/图表」被当成图片。
    if re.search(
        r"(图片|图像|照片|缩略图|位图|截图|(?<![地纸表网草截导流框])图"
        r"|image|picture|photo|png|jpe?g|webp|gif)",
        value,
    ):
        kinds.add("image")
    return kinds


def _authorized_local(
    path: Path,
    pool: list[dict[str, Any]],
    message_text: str,
    workspace: str | Path | None,
) -> bool:
    """本地目标是否通过目标授权门（会话已记录媒体 / 用户原文明确给出）。"""
    if _find_candidate_by_source(pool, str(path)) is not None:
        return True
    key = _candidate_key(str(path))
    if not key:
        return False
    for target in extract_media_targets_from_text(message_text, workspace):
        if _candidate_key(str(target.get("source") or "")) == key:
            return True
    return False


def _resolve_local_request(
    raw: str,
    workspace: str | Path | None,
    pool: list[dict[str, Any]],
    message_text: str,
) -> dict[str, Any] | None:
    """显式本地路径/文件名 → 结果；返回 ``None`` 表示「它只是裸文件名，交给描述性指代」。

    两种「找不到」要分开处理：

    - 带分隔符/盘符的真路径：找不到就是 ``not_found``，**绝不**退到按名字猜候选
      （否则「D:\\a\\b.png 不存在」会神奇地显示成工作区里另一张同名图）；
    - 裸文件名（``a.png``）：先按工作区相对路径解析，解析不到再走候选展示名精确匹配
      （同名多份 → ``ambiguous``），这也是计划 §2.3 要求的「仅 basename 冲突 → 歧义」。
    """
    resolved, reason = _local_target(raw, workspace)
    if resolved is None:
        if reason == "not_found" and not _looks_like_path(raw):
            return None
        return _show_skipped(reason)
    if not _authorized_local(resolved, pool, message_text, workspace):
        return _show_skipped("not_found")
    record = _find_candidate_by_source(pool, str(resolved)) or {}
    return _show_shown({**record, "source": str(resolved), "name": resolved.name})


def _trusted_local_record(raw: str, pool: list[dict[str, Any]]) -> dict[str, Any] | None:
    """精确解析本地参数并查找会话可信记录（可位于 workspace 外，如 uploads）。"""
    try:
        path = Path(str(raw or "").strip()).expanduser()
        # 相对参数必须交给 _local_target 按本轮工作区解析、检查边界，不能借进程 cwd 绕过。
        if not path.is_absolute():
            return None
        path = path.resolve()
    except (OSError, ValueError):
        return None
    record = _find_candidate_by_source(pool, str(path))
    return {**record, "source": str(path)} if record is not None else None


def _resolve_descriptive(
    raw: str, message_text: str, pool: list[dict[str, Any]]
) -> dict[str, Any]:
    """代词/模糊指代（「刚才那个视频」）解析：**恰好一个**才允许展示。

    多个候选一律 ``ambiguous``（返回安全的候选展示名，不挑最新、不挑第一个）；
    零个 ``not_found``。基名精确匹配优先于「包含」匹配（同名不同路径仍是 ambiguous）。
    """
    hint = str(raw or "").strip()
    kinds = _kinds_mentioned(hint) or _kinds_mentioned(message_text)
    filtered = [item for item in pool if not kinds or str(item.get("kind") or "") in kinds]
    base = hint.lower()
    if base and not _looks_like_path(hint):
        by_name = [
            item for item in filtered
            if str(item.get("name") or "").strip().lower() == base
        ]
        if len(by_name) == 1:
            return _show_shown(by_name[0])
        if len(by_name) > 1:
            return _show_skipped("ambiguous", [str(item.get("name") or "") for item in by_name])
        by_contains = [
            item for item in filtered
            if base and base in str(item.get("name") or "").strip().lower()
        ]
        if len(by_contains) == 1:
            return _show_shown(by_contains[0])
        if len(by_contains) > 1:
            return _show_skipped("ambiguous", [str(item.get("name") or "") for item in by_contains])
    if not filtered:
        return _show_skipped("not_found")
    if len(filtered) == 1:
        return _show_shown(filtered[0])
    return _show_skipped("ambiguous", [str(item.get("name") or "") for item in filtered])


def resolve_show_media_target(
    paths: Any,
    *,
    workspace: str | Path | None = None,
    message_text: str = "",
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """show_media 的目标授权门：把 ``paths`` 解析成**唯一**可展示目标。

    返回 ``{"ok": True, "status": "shown", kind, name, source, thumb_path}``
    或 ``{"ok": False, "status": "skipped", "reason": <枚举>, "name", "choices"?}``。

    契约（计划 §2.2/§2.3）：
    - ``paths`` 必须是字符串数组、**单次最多 1 项**；为空/非法/两项以上/重复项一律拒绝，
      且**先完成全部校验再采集**（任一失败都不产生半张卡）；
    - 空数组 = 「按用户原文里的代词指代解析」；
    - 本地目标必须是工作区内真实存在的普通媒体文件（符号链接按真实目标判定）；
    - 超过 ``MEDIA_SHOW_MAX_BYTES`` 返回 ``too_large``，只读 stat，不读全文、不复制。
    """
    pool = [item for item in (candidates or []) if isinstance(item, dict)]
    raw_items: list[Any]
    if paths is None:
        raw_items = []
    elif isinstance(paths, str):
        raw_items = [paths]
    elif isinstance(paths, (list, tuple)):
        raw_items = list(paths)
    else:
        return _show_skipped("not_found")
    if any(not isinstance(item, str) for item in raw_items):
        return _show_skipped("not_found")
    if len(raw_items) > 1:
        return _show_skipped("over_limit")
    if not raw_items:
        return _resolve_descriptive("", message_text, pool)
    raw = raw_items[0].strip()
    if not raw:
        return _show_skipped("not_found")

    # ① URL 形态（ComfyUI /view 等）：只认会话里已记录的同串来源，不联网、不猜地址。
    if _is_http_url(raw):
        record = _find_candidate_by_source(pool, raw)
        if record is None:
            return _show_skipped("not_found")
        return _show_shown(record)

    # 会话已记录媒体（尤其是宿主 uploads/generated 缓存）可按精确来源复用，
    # 即使其缓存目录位于工作区外。新声明路径仍由后续 _local_target 严格限制在 workspace。
    trusted = _trusted_local_record(raw, pool)
    if trusted is not None:
        return _show_shown(trusted)

    # ② 模型把用户原话整句塞进了 paths（实测常见）：那不是路径。
    #    只从这句话里捡**已进候选**的路径（不引入新的授权来源），其余交给下文按候选判定。
    #    例外：**确实指向文件（或超限/越界）的串按真路径判定**——目录一深，绝对路径很容易
    #    超过 60 字符，不能因为「长」就被当成句子（短路径 TEMP 实测：78 字符的合法 mp4
    #    被判 not_found）。只有「像路径但根本指不到文件」才回退到文本解析。
    if _is_sentence_like(raw):
        treat_as_path = False
        if _looks_like_path(raw):
            probe, reason = _local_target(raw, workspace)
            treat_as_path = probe is not None or reason in {"too_large", "outside_root", "unreadable"}
        if not treat_as_path:
            for target in extract_media_targets_from_text(raw, workspace):
                hit = _find_candidate_by_source(pool, str(target.get("source") or ""))
                if hit is not None:
                    return _show_shown(hit)
            return _resolve_descriptive("", f"{raw} {message_text}".strip(), pool)

    # ③ 本地路径/文件名形态：按本轮冻结工作区解析，且必须通过授权（会话候选 / 原文明确给出）。
    #    带分隔符/盘符的真路径只走这一条；裸文件名（a.png）解析不到时退回 ④ 按候选展示名匹配
    #    ——「仅 basename 冲突 → ambiguous」正是从那里出来的。
    if _looks_like_path(raw) or Path(raw).suffix:
        resolved_result = _resolve_local_request(raw, workspace, pool, message_text)
        if resolved_result is not None:
            return resolved_result

    # ④ 描述性指代（「刚才那个视频」/「a.png」）：候选集合里恰好一个才放行。
    return _resolve_descriptive(raw, message_text, pool)
