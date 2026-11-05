# -*- coding: utf-8 -*-
"""OneBot v11（NapCatQQ）协议层：纯解析与文本切分，不做 IO、不碰存储。

边界（计划 §3.1）：本模块只回答「这个事件是什么、这条消息由哪些段组成、
这段长文本该按哪里切开」。运行提交、身份白名单、会话绑定和投递状态都在
``adapter.py`` / ``store.py`` 里；工具执行与权限一律留在 ``naiba/run/``。

安全性口径：
- 事件里的任何**本地路径**都不可信（NapCat 可能跑在另一台机器上），
  这里只把它当作「不支持的位置」，绝不交给上层当文件路径使用；
- 消息段类型是白名单：不认识的类型不静默丢弃，由调用方回告用户。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 事件体积上限：一条消息事件的 JSON 不应超过这个字节数（防畸形/恶意大包）。
MAX_EVENT_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_SEGMENTS = 64

# QQ / NapCat 单条消息的实用上限。超过就在段落边界切分（见 split_message）。
MESSAGE_CHARS_LIMIT = 3000
# 单条回复最多发多少段（防止模型输出把 QQ 刷屏），超出后追加省略说明。
MAX_MESSAGE_SEGMENTS = 12

# 白名单消息段类型 → 处理方式。
# text     直接拼正文；
# image    图片（下载 → 受管 uploads）；
# record   语音（同 image 管线，作为音频附件）；
# video    视频（同 image 管线）；
# file     文件（同 image 管线）；
# at/face/reply/markdown/forward/node 在私聊里没有可用语义，显式回告「不支持」。
TEXT_SEGMENTS = frozenset({"text"})
MEDIA_SEGMENTS: dict[str, str] = {
    "image": "image",
    "record": "audio",
    "video": "video",
    "file": "file",
}
# 私聊里可以安全忽略（不构成任何用户意图）的段。群聊的 ``at`` 例外：
# 它要先用来判断「这条消息是否 @ 了机器人」，再决定算不算触发。
IGNORED_SEGMENTS = frozenset({"reply", "face", "at", "markdown", "rps", "dice", "shake", "poke"})
# 明确不支持、必须回告的段（会丢内容，不能让用户以为发出去了）。
UNSUPPORTED_SEGMENTS = frozenset({"forward", "node", "json", "xml", "contact", "location", "music", "share"})

# 群聊触发前缀（@机器人之外的第二条入口）：用户可以在群里用「/cat 内容」叫它。
GROUP_TRIGGER_PREFIXES = ("/cat", "／cat")

MEDIA_EXTENSIONS = {
    "image": ".png",
    "audio": ".amr",
    "video": ".mp4",
    "file": ".bin",
}


class OneBotProtocolError(ValueError):
    """事件结构不合法（不进入任何业务分支）。"""


@dataclass
class ParsedMessage:
    """一条已归一化的入站消息。"""

    account_id: str
    message_id: str
    scope: str                      # private / group
    qq_user_id: str
    qq_group_id: str
    text: str
    media: list[dict[str, Any]] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    nickname: str = ""
    # 群聊触发事实：@ 了本 bot，或带群触发前缀。私聊恒为 True（私聊不需要触发词）。
    triggered: bool = True
    # 群聊里被 @ 的 QQ 号列表（用来判断触发；不参与身份判定）。
    mentioned_ids: list[str] = field(default_factory=list)


def _as_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_segment(raw: Any) -> tuple[str, dict[str, Any]] | None:
    """把一段消息归一化成 (type, data)。非法结构返回 None（由调用方记 unsupported）。"""
    if not isinstance(raw, dict):
        return None
    kind = _as_text(raw.get("type")).lower()
    if not kind:
        return None
    data = raw.get("data")
    return kind, dict(data) if isinstance(data, dict) else {}


# OneBot CQ 码（字符串形态）：[CQ:type,key=value,...]。只在收到字符串 message 时用。
_CQ_RE = re.compile(r"\[CQ:([A-Za-z_][A-Za-z0-9_]*)((?:,[^\]]*)?)\]")
_CQ_ESCAPE = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))


def _unescape_cq(value: str) -> str:
    for needle, replacement in _CQ_ESCAPE:
        value = value.replace(needle, replacement)
    return value


def parse_cq_string(message: str) -> list[dict[str, Any]]:
    """把 CQ 码字符串拆成标准消息段列表（尽力而为：不认识的码保留 type）。

    只在 NapCat 以 ``post_format=string`` 上报时使用；数组格式是首选路径。
    """
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in _CQ_RE.finditer(message):
        if match.start() > cursor:
            chunk = _unescape_cq(message[cursor:match.start()])
            if chunk:
                segments.append({"type": "text", "data": {"text": chunk}})
        kind = match.group(1).lower()
        data: dict[str, Any] = {}
        for pair in (match.group(2) or "").lstrip(",").split(","):
            if not pair or "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            data[key.strip()] = _unescape_cq(value)
        segments.append({"type": kind, "data": data})
        cursor = match.end()
    if cursor < len(message):
        chunk = _unescape_cq(message[cursor:])
        if chunk:
            segments.append({"type": "text", "data": {"text": chunk}})
    return segments


def parse_message_event(raw: Any) -> ParsedMessage:
    """校验并归一化一条 OneBot ``post_type=message`` 事件。

    只接受字段合法的事件：``post_type`` / ``message_type`` / ``message_id`` /
    ``user_id`` / ``self_id`` 缺一不可。群聊事件仍会被解析（便于上层按
    ``allow_groups`` 明确拒绝并回告），但不会绕过上层开关。
    """
    if not isinstance(raw, dict):
        raise OneBotProtocolError("事件不是对象")
    if _as_text(raw.get("post_type")).lower() != "message":
        raise OneBotProtocolError("不是消息事件")
    message_type = _as_text(raw.get("message_type")).lower()
    if message_type not in {"private", "group"}:
        raise OneBotProtocolError(f"不支持的消息类型：{message_type or '(空)'}")
    account_id = _as_text(raw.get("self_id"))
    if not account_id:
        raise OneBotProtocolError("缺少 self_id（bot 账号）")
    message_id = _as_text(raw.get("message_id"))
    if not message_id:
        raise OneBotProtocolError("缺少 message_id")
    user_id = _as_text(raw.get("user_id"))
    if not user_id:
        raise OneBotProtocolError("缺少 user_id")
    group_id = _as_text(raw.get("group_id")) if message_type == "group" else ""
    if message_type == "group" and not group_id:
        raise OneBotProtocolError("群消息缺少 group_id")

    raw_message = raw.get("message")
    if isinstance(raw_message, list):
        segments = [_normalize_segment(item) for item in raw_message[:MAX_SEGMENTS]]
    elif isinstance(raw_message, str):
        segments = [_normalize_segment(item) for item in parse_cq_string(raw_message)]
    elif raw_message is None:
        # 极少数实现只发 raw_message；退化成纯文本，至少不丢用户内容。
        segments = [("text", {"text": _as_text(raw.get("raw_message"))})] if _as_text(raw.get("raw_message")) else []
    else:
        raise OneBotProtocolError("message 必须是数组或字符串")

    texts: list[str] = []
    media: list[dict[str, Any]] = []
    ignored: list[str] = []
    unsupported: list[str] = []
    mentioned: list[str] = []
    for item in segments:
        if item is None:
            unsupported.append("unknown")
            continue
        kind, data = item
        if kind in TEXT_SEGMENTS:
            text = _as_text(data.get("text"))
            if text:
                texts.append(text)
        elif kind == "at":
            # 群聊触发判定要用它，但不能凭空当成「有人在跟 bot 说话」。
            mentioned.append(_as_text(data.get("qq")))
            ignored.append(kind)
        elif kind in MEDIA_SEGMENTS:
            media.append({
                "kind": MEDIA_SEGMENTS[kind],
                "segment": kind,
                "file": _as_text(data.get("file")),
                "file_id": _as_text(data.get("file_id")),
                "url": _as_text(data.get("url")),
                "name": _as_text(data.get("name")) or _as_text(data.get("file")),
            })
        elif kind in IGNORED_SEGMENTS:
            ignored.append(kind)
        else:
            unsupported.append(kind)

    text = "\n".join(texts).strip()
    # 群聊触发前缀（/cat 内容）：剥掉前缀，剩下的才是给模型的正文。
    prefixed = False
    lowered = text.lower()
    for prefix in GROUP_TRIGGER_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):].lstrip(":： \t")
            prefixed = True
            break
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    nickname = _as_text(sender.get("card")) or _as_text(sender.get("nickname"))
    if message_type == "private":
        triggered = True
    else:
        triggered = prefixed or account_id in mentioned
    return ParsedMessage(
        account_id=account_id,
        message_id=message_id,
        scope=message_type,
        qq_user_id=user_id,
        qq_group_id=group_id,
        text=text,
        media=media,
        ignored=ignored,
        unsupported=unsupported,
        nickname=nickname,
        triggered=triggered,
        mentioned_ids=mentioned,
    )


def _fence_marker(line: str) -> str:
    """该行是否是代码围栏行；是则返回围栏标记（```/~~~），否则空串。"""
    stripped = line.lstrip(" \t")
    for marker in ("```", "~~~"):
        if stripped.startswith(marker):
            return marker
    return ""


def split_message(text: str, limit: int = MESSAGE_CHARS_LIMIT) -> list[str]:
    """按 QQ 单条上限切分长回复，尽量保留段落与代码块。

    - 优先在空行（段落边界）切，其次在换行切，最后才硬切字符；
    - 切在代码围栏内部时，本段末尾补闭合围栏、下一段开头重开同一围栏，
      避免 QQ 里出现「半段代码被当正文」；
    - 返回段都是非空字符串，且每段长度不超过 ``limit``（补的围栏行也算在内）；
    - 空输入返回空列表。

    实现是「先算围栏状态、再贪心装行」：每行的入/出围栏状态在切分前一次性算好，
    切点只按长度与段落边界选，因此补写的围栏行不会被误判成下一个围栏的开/闭。
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    if len(raw) <= limit:
        return [raw]

    lines = raw.splitlines(keepends=True)
    state_before: list[str] = []
    state_after: list[str] = []
    state = ""
    for line in lines:
        state_before.append(state)
        marker = _fence_marker(line)
        if marker:
            state = "" if state == marker else (state or marker)
        state_after.append(state)

    # 单行超过上限（长 URL / 不换行的模型输出）时先按可用长度硬切，
    # 硬切出来的一块沿用该行的围栏状态。
    items: list[tuple[str, str, str]] = []
    for index, line in enumerate(lines):
        before, after = state_before[index], state_after[index]
        overhead = _fence_overhead(before) + _fence_overhead(after)
        available = max(1, limit - overhead)
        if len(line) <= available or _fence_marker(line):
            items.append((line, before, after))
            continue
        for start in range(0, len(line), available):
            items.append((line[start:start + available], before, after))

    chunks: list[str] = []
    total = len(items)
    index = 0
    while index < total:
        start_state = items[index][1]
        prepend = f"{start_state}\n" if start_state else ""
        used = len(prepend)
        last_fit = index
        cursor = index
        while cursor < total:
            end_state = items[cursor][2]
            if used + len(items[cursor][0]) + _fence_overhead(end_state) > limit:
                break
            used += len(items[cursor][0])
            last_fit = cursor
            cursor += 1
        # 优先切在段落边界（本行是空行，或下一行是空行）；但只在「省下的余量超过半段」
        # 时才让步平均，否则为了一个空行把消息切成大量碎片，QQ 里反而更难读。
        cut = last_fit
        for candidate in range(last_fit, index, -1):
            if not items[candidate][0].strip() or (
                candidate + 1 < total and not items[candidate + 1][0].strip()
            ):
                if _items_len(items[index:candidate + 1]) >= limit // 2:
                    cut = candidate
                break
        body = prepend + "".join(item[0] for item in items[index:cut + 1])
        body = body.rstrip("\n")
        end_state = items[cut][2]
        if end_state:
            body = f"{body}\n{end_state}"
        if body.strip():
            chunks.append(body)
        index = cut + 1
    return chunks


def _fence_overhead(state: str) -> int:
    """补一行围栏（含换行）要占的长度。"""
    return len(state) + 1 if state else 0


def _items_len(items: list[tuple[str, str, str]]) -> int:
    return sum(len(item[0]) for item in items)

