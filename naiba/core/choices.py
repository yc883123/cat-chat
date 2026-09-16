"""AI 回复中的交互选项识别（原 server.py _detect_choice_groups/_detect_choices）。

两条通道，**显式优先**：

1. 显式格式——回复里带 ``naiba-choices`` 标记的围栏 JSON 代码块，内容形如
   ``{"choice_groups": [{"prompt": "视觉", "choices": ["A", "B"], "mode": "single"}]}``；
2. 自然语言识别——明确的"选择"意图（或每题自带的「单选/多选」标识）+ 紧邻的编号/字母/圆点
   列表，兼容旧模型输出。

``normalize_choice_groups`` 是**组结构的唯一规范化入口**：实时事件（run/chat）、消息
metadata 与历史读取（http）三条路径共用同一套规则，组结构本身登记在
``naiba.core.contracts``（CHOICE_GROUP_KEYS / CHOICE_MODES / CHOICE_SOURCES）。
纯文本解析，无状态无依赖。
"""

from __future__ import annotations

import json
import re
from typing import Any

from naiba.core.contracts import (
    CHOICE_MODES,
    CHOICE_SOURCES,
    DEFAULT_CHOICE_MODE,
    EXPLICIT_CHOICE_FENCE,
    SOURCE_EXPLICIT,
    SOURCE_NATURAL,
)

# 自然语言识别的准入阈值。2026-09-15 由 40/40 放宽到 300/1000：40 字会把"题目带说明、
# 选项带补充"的正常提问整组漏掉（用户实测漏显示的主因之一）。长度只用于"这一行是不是
# 选项"的判定；命中的题目与选项**原文一律不截断**。
MAX_PROMPT_LEN = 300
MAX_CHOICE_LEN = 1000
MAX_CUE_DISTANCE = 2   # 题目行与选项组首行的最大行距（允许少量空行/单行间隔）
MAX_GROUP_CHOICES = 8  # 自然语言识别每组最多收 8 项（显式格式不截断）
MAX_SCAN_LEN = 20000   # 更长的回复视为长文（Skill 手册/文档），不做自然语言识别

# 显式结构化选项块：```naiba-choices … ```（info string 允许带 json 等后缀）。
_EXPLICIT_BLOCK_RE = re.compile(
    r"^[ \t]*```[ \t]*" + EXPLICIT_CHOICE_FENCE + r"\b[^\n]*\n(?P<body>[\s\S]*?)^[ \t]*```[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)

# 只接受明确的"选择"意图。英文只保留完整词组，避免正文/代码里的裸 select / choose / pick
# 触发误判（例如 SQL 的 "SELECT * FROM …"）。
# 2026-09-16 扩充：AI 常写「以下是当前的选项 / 选项如下 / 你可以选择 / 请从以下中选」这类不含
# "请选择"字样的措辞——实测本机 31 条"问句 + 编号列表"回复里只有 5 条命中旧词表，是"该弹
# 没弹"的主因。新增词一律是「选项类名词 + 结构词」组合，仍不接受裸 select / choose / pick。
CUE_RE = re.compile(
    r"请(?:先|再)?选择|请(?:你|您)?选|再选(?:一下|一个|个)?|供(?:你|您)?选择|可供选择|"
    r"选哪个|选一个|pick one|choose one|select one|which one|which of|choose from|select from|"
    r"select\s+(?:multiple|more\s+than\s+one)|choose\s+(?:multiple|more\s+than\s+one)|"
    r"以下是(?:当前|本次|这轮|可)?(?:的)?(?:选项|选择项|可选方案|可选项|可选项目)|"
    r"下面(?:是|有)(?:当前|本次)?(?:的)?(?:选项|选择项|可选方案|可选项)|"
    r"(?:选项|选择项|可选方案|可选项|可选项目)(?:如下|如下所示|有以下几个|有以下这些)|"
    r"你可以(?:选择|从以下|从下面|从下列|从中选)|"
    r"请从(?:以下|下面|下列|上述)|"
    r"从(?:以下|下面|下列)(?:选项|方案|中)?选|"
    r"here (?:are|is) (?:the|some|your|a few) (?:options|choices)|"
    r"(?:the )?(?:options|choices) (?:are|below)|"
    r"you can (?:choose|pick|select)",
    re.IGNORECASE,
)
# 每题自带的单选/多选标识：必须是"独立词"形态（括号包裹、后随标点、或行尾），
# 以免把「多选组件实现」这类正文词当成题目。
MODE_MARKER_RE = re.compile(
    r"[（(【\[「『][ \t]*(?:单选|多选|复选)[ \t]*[）)】\]」』]"
    r"|(?:单选|多选|复选)[ \t]*[：:，,。.、]"
    r"|(?:单选|多选|复选)[ \t]*$"
    r"|single[\s\-]*choice|multiple[\s\-]*choice",
    re.IGNORECASE,
)
# 光秃秃的一个模式词（如小标题「## 多选」）不是题目行。
_MODE_ONLY_RE = re.compile(r"^(?:单选|多选|复选)[ \t]*[：:]?$", re.IGNORECASE)
MULTI_HINT_RE = re.compile(
    r"多选|复选|选择多个|可多选多项|多项选择|勾选多个|选出多个|"
    r"choose\s+(?:multiple|more\s+than\s+one)|select\s+(?:multiple|more\s+than\s+one)|"
    r"select\s+all\s+that\s+apply|multiple\s+(?:choice|answer|option|select)",
    re.IGNORECASE,
)


def _clean_choice_text(value: str) -> str:
    """剥离行首任务框/加粗/编号残留（如 bullet 行 "- 1. 安装依赖" → "安装依赖"）。"""
    value = re.sub(r"^(?:\[[ xX]\]\s*)", "", str(value or "").strip())
    value = re.sub(r"^(?:\*\*|__)", "", value)
    value = re.sub(r"\s*(?:\*\*|__)$", "", value)
    # 行内加粗闭合标记（"1. **甲** — 说明"）也一并剥掉：选项按钮只显示纯文本、不解析 Markdown。
    value = value.replace("**", "")
    value = re.sub(r"^\d{1,2}\s*[.、:：)）\]】]\s*", "", value.strip())
    return value.strip()


def _is_prompt_line(text: str) -> bool:
    """该行能否当"题目行"：带明确选择意图，或带每题自带的单选/多选标识。"""
    text = str(text or "").strip()
    if not text:
        return False
    if CUE_RE.search(text):
        return True
    if _MODE_ONLY_RE.match(text):
        return False
    return bool(MODE_MARKER_RE.search(text))


def _mode_of(prompt: str) -> str:
    """题目行里的多选提示（"可多选 / 选择多个 …"）→ multi，其余为 single。"""
    return "multi" if MULTI_HINT_RE.search(str(prompt or "")) else DEFAULT_CHOICE_MODE


def _choice_marker_continues(kind: str, previous: Any, current: Any) -> bool:
    """跨空行的两个选项标记是否属于同一序列（只对可判定的编号/字母序列生效）。

    用于「1. …（空行）2. …」这类 Markdown 列表：空行后编号仍连续 → 同一组；
    编号重新从 1 开始（新列表）→ 交给调用方 finish_group 切开，避免两组被合并后
    因编号不连续而整组丢弃。bullet/named 的 marker 是递增计数、无法判定，一律视为延续。
    """
    if kind == "numbered":
        return isinstance(previous, int) and isinstance(current, int) and current == previous + 1
    if kind == "lettered":
        return (isinstance(previous, str) and isinstance(current, str)
                and len(previous) == 1 and len(current) == 1
                and current == chr(ord(previous) + 1))
    return True


def normalize_choice_groups(raw: Any, source: str = "") -> list[dict[str, Any]]:
    """把任意来源的选项数据规范化为契约形态（见 contracts.CHOICE_GROUP_KEYS）。

    接受 ``{"choice_groups": [...]}`` / ``[{"prompt","choices","mode"}, …]`` /
    ``["A", "B"]``（旧版 ``message.metadata.choices`` 的纯字符串数组 → 一个无题目的组）。

    规则：题目标题与选项去首尾空白、丢弃空选项；``mode`` 只认 single|multi，缺省 single；
    ``source`` 缺省继承入参，非法值回落到 natural。**题目与选项原文一律不截断**
    （长度上限只作用于自然语言识别的准入判定，不作用于本函数）。
    """
    if isinstance(raw, dict):
        raw = raw.get("choice_groups")
    if raw is None or isinstance(raw, str):
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    entries = list(raw)
    if not entries:
        return []
    # 旧版扁平字符串数组是"一组多选"而不是"每组一项"。
    raw_entries: list[Any] = [{"choices": entries}] if all(isinstance(x, str) for x in entries) else entries
    default_source = source if source in CHOICE_SOURCES else SOURCE_NATURAL
    groups: list[dict[str, Any]] = []
    for item in raw_entries:
        if isinstance(item, str):
            item = {"choices": [item]}
        if not isinstance(item, dict):
            continue
        choices_raw = item.get("choices")
        if isinstance(choices_raw, str):
            choices_raw = [choices_raw]
        if not isinstance(choices_raw, (list, tuple)):
            choices_raw = []
        choices = [str(choice).strip() for choice in choices_raw if str(choice or "").strip()]
        if not choices:
            continue
        mode = str(item.get("mode") or "").strip().lower()
        if mode not in CHOICE_MODES:
            mode = DEFAULT_CHOICE_MODE
        item_source = str(item.get("source") or "").strip().lower()
        if item_source not in CHOICE_SOURCES:
            item_source = default_source
        groups.append(
            {
                "prompt": str(item.get("prompt") or "").strip(),
                "choices": choices,
                "mode": mode,
                "source": item_source,
            }
        )
    return groups


def explicit_choice_groups(text: str) -> list[dict[str, Any]]:
    """解析回复里的 ``naiba-choices`` 结构化块；有效块返回规范化组，否则空列表。

    多个块时取**第一个有效块**。解析失败或结构无效（无选项）的块一律忽略，交给自然
    语言识别兜底——无效块既不隐藏也不生成可点击控件（前端按 ``source`` 判定）。
    """
    raw_text = str(text or "")
    if EXPLICIT_CHOICE_FENCE not in raw_text:
        return []
    for match in _EXPLICIT_BLOCK_RE.finditer(raw_text):
        body = match.group("body").strip()
        if not body:
            continue
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            continue
        groups = normalize_choice_groups(payload, source=SOURCE_EXPLICIT)
        if groups:
            return groups
    return []


def _detect_choice_groups(text: str) -> list[dict[str, Any]]:
    """自然语言识别：检测回复中的交互选项组，并保留每组前面的题目行。"""
    raw_text = str(text or "")
    # Long Skill manuals and MCP instructions are not interactive choices.
    if len(raw_text) > MAX_SCAN_LEN or re.search(r"<skill\b|mcp_servers\s*:|official-comfy-mcp", raw_text, re.I):
        return []
    # 围栏代码块默认不参与识别（多是代码示例，剔除以免误判）；但 AI 也常用代码块包裹
    # 「选择题」的选项清单——这类块紧跟在带选择意图/单选多选标识的题目行之后，块内列表
    # 需要并入候选（2026-09-16，实测"给我几个选择题"这类请求会触发）。
    # 规则：块内行仅在「块前一行是题目行」时并入；显式 naiba-choices 块不参与自然语言识别。
    lines: list[str] = []
    fence_body: list[str] | None = None
    fence_is_explicit = False
    for raw_line in raw_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if fence_body is None:
                fence_body = []
                fence_info = stripped[3:].strip().lower().split()
                fence_is_explicit = bool(fence_info) and fence_info[0] == EXPLICIT_CHOICE_FENCE
            else:
                if fence_body and not fence_is_explicit and lines and _is_prompt_line(lines[-1]):
                    lines.extend(fence_body)
                fence_body = None
                fence_is_explicit = False
            continue
        if fence_body is None:
            lines.append(raw_line)
        else:
            fence_body.append(raw_line)

    numbered_pattern = re.compile(
        r"^\s*(?:\*\*|__)?(?:[（(\[【]?\s*(\d{1,2})\s*[.、):：）\]】])"
        r"\s*(?:\*\*|__)?\s*(.+?)\s*$"
    )
    lettered_pattern = re.compile(
        r"^\s*(?:\*\*|__)?(?:[（(\[【]?\s*([A-Ha-h])\s*[.、):：）\]】])"
        r"\s*(?:\*\*|__)?\s*(.+?)\s*$"
    )
    named_pattern = re.compile(
        r"^\s*(?:\*\*|__)?(?:选项|方案)\s*[一二三四五六七八\dA-Ha-h]+\s*[.、:：)）]"
        r"\s*(?:\*\*|__)?\s*(.+?)\s*$",
        re.IGNORECASE,
    )
    bullet_pattern = re.compile(r"^\s*[-+*•]\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")

    circled_numbers = {char: index for index, char in enumerate("①②③④⑤⑥⑦⑧", start=1)}
    groups: list[dict[str, Any]] = []
    current_kind = ""
    current_items: list[list[Any]] = []
    current_prompt = ""
    current_start_line = -1
    current_prompt_line = -1  # 组首行时最近一个题目行的快照（避免被后续题目行覆盖）
    blank_gap = False         # 组内是否出现过空行（跨空行的同一序列仍算一组）
    preceding_prompt = ""
    preceding_prompt_line = -1
    recent_prompt = ""
    recent_prompt_line = -1

    def finish_group() -> None:
        nonlocal current_kind, current_items, current_prompt, current_start_line, current_prompt_line, blank_gap
        if current_items:
            groups.append(
                {
                    "kind": current_kind,
                    "items": current_items,
                    "prompt": current_prompt,
                    "start_line": current_start_line,
                    "prompt_line": current_prompt_line,
                }
            )
        current_kind = ""
        current_items = []
        current_prompt = ""
        current_start_line = -1
        current_prompt_line = -1
        blank_gap = False

    for line_index, raw_line in enumerate(lines):
        # Models frequently put compact choices on one line, for example
        # "1. 文生视频 2. 图生视频" or "请选择语言：1. 中文 2. 英文".
        # Split at a choice marker preceded by whitespace or a CJK/ASCII
        # punctuation so such compact prompts yield separate lines, while
        # decimal numbers in prose are left untouched.
        expanded_lines = re.sub(
            r"(?<=[\s：:、。；;，,])(?=\d{1,2}\s*[.、):：）\]】])",
            "\n",
            raw_line,
        ).splitlines() or [""]
        for line in expanded_lines:
            stripped = line.strip()
            # 空行不打断选项组：AI 常用「1. …（空行）2. …」的 Markdown 写法。空行若按普通行
            # 处理会 finish_group，把一组选项切成两个单项组（<2 项全部丢弃）——真实会话里这是
            # "该弹没弹"的另一个成因。真正的新列表由下面的延续性校验切开，不合并。
            if not stripped:
                if current_items:
                    blank_gap = True
                continue
            parsed: tuple[str, Any, str] | None = None
            match = numbered_pattern.match(line)
            if match:
                parsed = ("numbered", int(match.group(1)), _clean_choice_text(match.group(2)))
            if not parsed:
                match = lettered_pattern.match(line)
                if match:
                    parsed = ("lettered", match.group(1).upper(), _clean_choice_text(match.group(2)))
            if not parsed:
                match = named_pattern.match(line)
                if match:
                    parsed = ("named", len(current_items), _clean_choice_text(match.group(1)))
            if not parsed and stripped and stripped[0] in circled_numbers:
                value = _clean_choice_text(stripped[1:].lstrip(".、):：） "))
                if value:
                    parsed = ("numbered", circled_numbers[stripped[0]], value)
            if not parsed:
                match = bullet_pattern.match(line)
                if match:
                    parsed = ("bullet", len(current_items), _clean_choice_text(match.group(1)))
            # 选项文本非空且不能是整段话
            if parsed and (not parsed[2] or len(parsed[2]) > MAX_CHOICE_LEN):
                parsed = None
            # 断行展开出的裸 bullet 符号（"- " / "• "）不是内容行，
            # 跳过以免打断正在收集的选项组。
            if not parsed and re.fullmatch(r"[-+*•]\s*", stripped):
                continue
            # 缩进续行：长选项允许换行书写（续行缩进于编号），并进上一项。
            # 只把「上一项 + 续行」仍在长度上限内的续行并进去，避免把整段正文吞成选项；
            # 超限的续行**丢弃该行但不打断当前组**——一个超长续行若按普通行处理会结束
            # 选项组，把前后两半分切成两个各不足两项的组，整块面板连同选项一起消失
            # （这正是"漏显示"的成因）。丢的是溢出文本，组与已并入部分保持可用。
            if (not parsed and current_items and stripped
                    and line[:1] in (" ", "\t")):
                merged = f"{current_items[-1][1]} {stripped}"
                if len(merged) <= MAX_CHOICE_LEN:
                    current_items[-1][1] = merged
                continue

            # A numbered heading such as "**1. 请选择时长：**" introduces the
            # following choices; it is not itself an option. Treat it as the
            # prompt so the option markers remain consecutive.
            if parsed and parsed[0] in {"numbered", "lettered"} and _is_prompt_line(parsed[2]):
                finish_group()
                preceding_prompt = parsed[2]
                preceding_prompt_line = line_index
                if len(preceding_prompt) <= MAX_PROMPT_LEN:
                    recent_prompt = preceding_prompt
                    recent_prompt_line = line_index
                continue

            if parsed:
                kind, marker, value = parsed
                if current_items and (kind != current_kind
                        or (blank_gap and not _choice_marker_continues(current_kind, current_items[-1][0], marker))):
                    finish_group()
                if not current_items:
                    current_kind = kind
                    current_start_line = line_index
                    if (preceding_prompt_line >= 0 and _is_prompt_line(preceding_prompt)
                            and len(preceding_prompt) <= MAX_PROMPT_LEN):
                        current_prompt = preceding_prompt
                        current_prompt_line = preceding_prompt_line
                    else:
                        current_prompt = recent_prompt
                        current_prompt_line = recent_prompt_line
                current_items.append([marker, value])
                continue

            finish_group()
            if stripped:
                preceding_prompt = _clean_choice_text(re.sub(r"^(?:#{1,6}\s*)", "", stripped))
                preceding_prompt_line = line_index
                if _is_prompt_line(preceding_prompt) and len(preceding_prompt) <= MAX_PROMPT_LEN:
                    recent_prompt = preceding_prompt
                    recent_prompt_line = line_index

    finish_group()

    candidates: list[dict[str, Any]] = []
    for group in groups:
        items = group["items"]
        if len(items) < 2:
            continue
        markers = [marker for marker, _ in items]
        if group["kind"] == "numbered" and markers != list(range(markers[0], markers[0] + len(items))):
            continue
        if group["kind"] == "lettered":
            expected = [chr(ord(markers[0]) + offset) for offset in range(len(items))]
            if markers != expected:
                continue
        prompt = str(group["prompt"] or "").strip()
        if not _is_prompt_line(prompt) or len(prompt) > MAX_PROMPT_LEN:
            continue
        # 题目行必须紧跟选项组（允许少量空行/单行间隔），否则视为普通列表。
        if not (0 <= group["start_line"] - group["prompt_line"] <= MAX_CUE_DISTANCE):
            continue
        candidates.append(
            {
                "prompt": prompt,
                "choices": [value for _, value in items][:MAX_GROUP_CHOICES],
                "mode": _mode_of(prompt),
            }
        )
    return candidates


def detect_choice_groups(text: str) -> list[dict[str, Any]]:
    """交互选项识别的公共入口：有效显式块优先，否则自然语言识别。"""
    explicit = explicit_choice_groups(text)
    if explicit:
        return explicit
    return normalize_choice_groups(_detect_choice_groups(text), source=SOURCE_NATURAL)


def resolve_message_choice_groups(metadata: Any, content: str) -> list[dict[str, Any]]:
    """历史读取口径：**优先保留有效 metadata**，缺失或无效才解析正文补齐。

    落库的 choice_groups/choices 是写入时的权威数据（含显式块来源标记），历史接口不得
    用重新解析正文的结果覆盖它——正文里可能根本没有可识别的选择文本（旧版落库形态、
    被截断的正文、用别的措辞写的题目），覆盖一次就把面板整块弄没了。
    """
    data = metadata if isinstance(metadata, dict) else {}
    stored = normalize_choice_groups(data.get("choice_groups") or data.get("choices"))
    if stored:
        return stored
    return detect_choice_groups(content)


def backfill_turn_choice_groups(messages: list[dict[str, Any]]) -> None:
    """历史读取：给「最后一条 user 消息之后」的 assistant 消息就地补齐选项数据。

    前端选择面板的保活口径是「未回答的那组选项，即使后面又追加了 assistant 消息也要留着」，
    因此不能只补最后一条（followup 轮次 / 后台任务回执 / 错误重试都会把面板顶掉）。
    补齐只走 resolve_message_choice_groups：有效 metadata 优先，绝不用空的解析结果覆盖
    已落库数据；无选项时写空列表，保持与既有 metadata 形态一致，重复调用幂等。
    入参为会话的 messages 列表（storage.get_conversation 的返回值），就地修改。
    """
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "user":
            break
        if role != "assistant":
            continue
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            message["metadata"] = metadata
        choice_groups = resolve_message_choice_groups(metadata, str(message.get("content") or ""))
        metadata["choice_groups"] = choice_groups
        metadata["choices"] = choice_groups[0]["choices"] if choice_groups else []


def _detect_choices(text: str) -> list[str]:
    """兼容旧调用方：返回检测到的第一组选项。"""
    groups = detect_choice_groups(text)
    return groups[0]["choices"] if groups else []
