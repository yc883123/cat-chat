"""句子库：从标注描述里规则分句 → 频次统计 → 可检索。

不做 NLP 依赖（spaCy 可选但首期不引入）：按中英标点与换行切分，结果标 ``degraded``
如实说明「规则分句，中文可能切得不够准」。分句只读标注库，不落额外表——
标注删改后句子库自动跟着变，不存在两处不一致。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# 句末标点后切开：中文句号/叹号/问号/分号、英文 !?;，以及「英文句点 + 空白 + 新句首」
# （不裸切英文句点，避免把 3.5、e.g. 这类写法切成两段）与换行。
_SPLIT_RE = re.compile(
    r"(?<=[。！？；!?;])\s*|(?<=\.)\s+(?=[A-Z\u4e00-\u9fff])|\r?\n+"
)
# 太短的片段（"图1"、"是"）没有检索价值，直接丢。
_MIN_SENTENCE_CHARS = 4
_MAX_SENTENCES_PER_DESCRIPTION = 40


def split_sentences(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    out: list[str] = []
    for piece in _SPLIT_RE.split(raw):
        sentence = (piece or "").strip().strip("　 ")
        if len(sentence) < _MIN_SENTENCE_CHARS:
            continue
        out.append(sentence)
        if len(out) >= _MAX_SENTENCES_PER_DESCRIPTION:
            break
    return out


def sentence_stats(records: list[dict[str, Any]], *, query: str = "",
                   limit: int = 300) -> dict[str, Any]:
    """统计描述里的句子频次；``query`` 按子串过滤（不区分大小写）。"""
    counter: Counter[str] = Counter()
    for row in records:
        for sentence in split_sentences(str(row.get("description") or "")):
            counter[sentence] += 1
    needle = str(query or "").strip().lower()
    items = [
        {"sentence": sentence, "count": count}
        for sentence, count in counter.items()
        if not needle or needle in sentence.lower()
    ]
    items.sort(key=lambda item: (-item["count"], item["sentence"]))
    return {
        "items": items[: max(1, min(int(limit), 2000))],
        "total_sentences": len(counter),
        "degraded": True,
        "note": "规则分句（按中英标点与换行），中文断句可能不够精确；描述为空时不产生句子。",
    }
