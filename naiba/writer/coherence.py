# -*- coding: utf-8 -*-
"""织书台跨章连贯性只读检查（跨章连贯性修复计划 §三.C）。

输入是已保存的章末状态与角色卡，**不调用模型、不改正文、不动版本**：

- 规则确定性可测：对结构化章末状态做跨章交叉比对，每条发现给出章节号、
  状态条目与正文原文片段（quote）作证据；
- 每条发现区分 ``confirmed``（状态条目直接冲突）与 ``tentative``（可能缺少
  交代），规则命中不冒充事实判断；
- 报告与检查时的正文版本绑定：任一章当前指针变化，旧报告整体显示为过期
  （``report_is_stale``）；重新运行即得新报告；
- 没有有效章末状态的章节不参与比对，只在 notes 里如实说明覆盖范围，
  并提示可用「重建状态」补建——检查本身绝不代用户补写状态或正文。

状态是模型提取的派生数据，规则只对状态做交叉比对；状态错则检查错，
因此每条发现都带可回溯的证据，由用户决定是否处理。
"""

from __future__ import annotations

import time
from typing import Any

from naiba.writer.store import WriterStoreError

# 报告 schema：发现结构或规则变化时递增，消费方按 schema 决定兼容方式。
REPORT_SCHEMA = 1
# 证据原文片段半径（正文中按关键字定位，前后各取这么多字）。
QUOTE_WIDTH = 40

FINDING_LABELS = {
    "character_unintroduced": "人物无铺垫出现",
    "character_vanished": "人物中后段消失",
    "knowledge_conflict": "已知/未知矛盾",
    "relation_conflict": "关系回退",
}

# 关系「建立/亲近」标记与「回到陌生」标记（确定性关键词，命中不等于事实）。
_BOND_MARKERS = ("结盟", "同盟", "结拜", "结为", "恋人", "挚友", "好友", "成亲", "同伴", "队友")
_STRANGER_MARKERS = ("互不相识", "素不相识", "形同陌路", "陌生的")
_MEET_MARKERS = ("初次见面", "初次相识", "第一次见面", "初识", "相识", "结识") + _STRANGER_MARKERS
# 知识冲突的关键字最短长度：避免单字误命中。
_KNOWLEDGE_MIN_LEN = 2


def _quote(content: str, keyword: str, *, width: int = QUOTE_WIDTH) -> str:
    """在正文里定位关键字并截取前后片段；找不到返回空串（证据只留状态条目）。"""
    text = str(content or "")
    keyword = str(keyword or "").strip()
    if not keyword:
        return ""
    index = text.find(keyword)
    if index < 0:
        return ""
    start = max(0, index - width)
    end = min(len(text), index + len(keyword) + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def _chapter_contents(store: dict[str, Any], book_id: str, nos: list[int]) -> dict[int, str]:
    """按需读取正文（只为证据引文；读不到就用空串，不中断检查）。"""
    contents: dict[int, str] = {}
    for no in nos:
        try:
            contents[no] = str(store.get_chapter(book_id, no).get("content") or "")
        except (LookupError, WriterStoreError):
            contents[no] = ""
    return contents


def _card_names(setup: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """角色卡名字 → 角色行（含别名，别名按分隔符拆开）。"""
    from naiba.writer.prompts import _character_aliases

    card: dict[str, dict[str, Any]] = {}
    for row in setup.get("characters") or []:
        if not isinstance(row, dict):
            continue
        for name in _character_aliases(row):
            if name:
                card[name] = row
    return card


def _state_characters(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = state.get("characters") if isinstance(state.get("characters"), list) else []
    return [row for row in rows if isinstance(row, dict) and str(row.get("name") or "").strip()]


def _presence(state: dict[str, Any], name: str) -> str:
    """某名字在某章状态的在场取值：出场 / 被提及 / 缺席 / 未提及（未列入）。"""
    for row in _state_characters(state):
        if str(row.get("name") or "") == name:
            return str(row.get("presence") or "出场").strip() or "出场"
    return "未提及"


def _pair_key(between: list[str]) -> tuple[str, ...]:
    return tuple(sorted(str(b) for b in between if str(b).strip()))


def run_continuity_check(store: Any, book_id: str) -> dict[str, Any]:
    """全书只读检查：返回报告 dict（调用方负责落盘与展示）。"""
    overview = store.chapter_state_overview(book_id)
    setup = store.load_setup(book_id)
    card = _card_names(setup)
    lead_names = {str(row.get("name") or "").strip()
                  for row in (setup.get("characters") or [])
                  if isinstance(row, dict) and str(row.get("role") or "") == "lead"
                  and str(row.get("name") or "").strip()}

    chapters: list[dict[str, Any]] = []
    for row in overview.get("states") or []:
        record = row.get("record")
        state = dict(record.get("state") or {}) if isinstance(record, dict) and record.get("state") else {}
        chapters.append({
            "no": int(row.get("no") or 0),
            "current_version_id": str(row.get("current_version_id") or ""),
            "state_status": str(record.get("effective_status") or "missing") if record else "missing",
            "state": state,
        })
    ready = [row for row in chapters if row["state_status"] == "ready" and row["state"]]
    findings: list[dict[str, Any]] = []

    if ready:
        involved = {row["no"] for row in ready}
        contents = _chapter_contents(store, book_id, sorted(involved))
        findings.extend(_check_unintroduced(ready, card, contents))
        findings.extend(_check_vanished(ready, lead_names, contents))
        findings.extend(_check_knowledge(ready, contents))
        findings.extend(_check_relations(ready, contents))

    notes: list[str] = []
    missing = [row["no"] for row in chapters if row["state_status"] != "ready"]
    if missing:
        notes.append(
            f"第 {'、'.join(str(no) for no in missing[:20])} 章"
            + ("等" if len(missing) > 20 else "")
            + "没有有效章末状态，这些章不参与比对；可用「重建状态」按当前正文补建。")
    if not chapters:
        notes.append("这本书还没有任何章末状态记录；先写正文并提取状态，再做连贯性检查。")

    return {
        "schema": REPORT_SCHEMA,
        "book_id": str(book_id or ""),
        "generated_at": int(time.time() * 1000),
        "checked_chapters": [row["no"] for row in ready],
        # 版本绑定：任一章当前指针与此处不一致，报告整体过期。
        "chapter_versions": {str(row["no"]): row["current_version_id"] for row in chapters},
        "findings": findings,
        "notes": notes,
    }


def _finding(*, type: str, severity: str, chapters: list[int], summary: str,
             evidence: list[dict[str, Any]], names: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": type,
        "type_label": FINDING_LABELS.get(type, type),
        "severity": severity,
        "chapters": chapters,
        "names": [str(n) for n in (names or [])],
        "summary": summary,
        "evidence": evidence,
    }


def _evidence(chapter_no: int, quote: str, entry: str) -> dict[str, Any]:
    row: dict[str, Any] = {"chapter_no": int(chapter_no), "source": "state", "entry": str(entry)}
    if quote:
        row["quote"] = quote
        row["source"] = "state+content"
    return row


def _check_unintroduced(ready: list[dict[str, Any]], card: dict[str, str],
                        contents: dict[int, str]) -> list[dict[str, Any]]:
    """无铺垫出现：第 N 章新「出场」的名字，既不在角色卡、之前的章也没出现过。"""
    findings: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for row in ready:
        no = row["no"]
        state = row["state"]
        for character in _state_characters(state):
            name = str(character.get("name") or "")
            presence = str(character.get("presence") or "出场")
            if name in seen or name in card:
                if name not in seen:
                    seen[name] = no
                continue
            if presence not in ("出场", "被提及"):
                continue
            # 角色卡没有、此前状态也没出现 → 无铺垫（可能确实是新配角，故为 tentative）。
            entry = f"{name}（{presence}）"
            findings.append(_finding(
                type="character_unintroduced", severity="tentative", chapters=[no],
                names=[name],
                summary=f"第 {no} 章出现{name}，但角色卡里没有这个名字，之前的章末状态也没出现过。",
                evidence=[_evidence(no, _quote(contents.get(no, ""), name), entry)],
            ))
            seen[name] = no
        # 关系里提到的新名字也算「出现过」。
        for relation in state.get("relations") or []:
            if isinstance(relation, dict):
                for name in relation.get("between") or []:
                    seen.setdefault(str(name), no)
    return findings


def _check_vanished(ready: list[dict[str, Any]], lead_names: set[str],
                    contents: dict[int, str]) -> list[dict[str, Any]]:
    """主角消失：某章「出场」后，直到全书最后一条有效状态都没有再出场/被提及。"""
    findings: list[dict[str, Any]] = []
    if len(ready) < 2:
        return findings
    for name in sorted(lead_names):
        last_seen_index = -1
        for index, row in enumerate(ready):
            if _presence(row["state"], name) in ("出场", "被提及"):
                last_seen_index = index
        if last_seen_index < 0 or last_seen_index >= len(ready) - 1:
            continue
        after = ready[last_seen_index + 1:]
        if any(_presence(row["state"], name) in ("出场", "被提及") for row in after):
            continue
        gap = after[0]
        seen_row = ready[last_seen_index]
        findings.append(_finding(
            type="character_vanished", severity="tentative",
            chapters=[seen_row["no"], gap["no"]], names=[name],
            summary=(f"主角{name}最后一次出场在第 {seen_row['no']} 章，"
                     f"之后直到第 {ready[-1]['no']} 章的章末状态都没有再出现。"),
            evidence=[
                _evidence(seen_row["no"], _quote(contents.get(seen_row["no"], ""), name),
                          f"{name}（出场）"),
                _evidence(gap["no"], "", f"{name}（{_presence(gap['state'], name)}）"),
            ],
        ))
    return findings


def _check_knowledge(ready: list[dict[str, Any]],
                     contents: dict[int, str]) -> list[dict[str, Any]]:
    """已知/未知矛盾：第 k 章「X 不知道 S」，之后 X「知道 S」而中间没有交代。"""
    findings: list[dict[str, Any]] = []
    for index, row in enumerate(ready):
        state = row["state"]
        knowledges = state.get("knowledge") if isinstance(state.get("knowledge"), list) else []
        for item in knowledges:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            for secret in item.get("unknown") or []:
                secret = str(secret or "").strip()
                if len(secret) < _KNOWLEDGE_MIN_LEN:
                    continue
                for later_index in range(index + 1, len(ready)):
                    later = ready[later_index]
                    later_items = later["state"].get("knowledge") \
                        if isinstance(later["state"].get("knowledge"), list) else []
                    known_hit = any(
                        isinstance(later_item, dict) and str(later_item.get("name") or "") == name
                        and any(secret in str(k) or str(k) in secret
                                for k in (later_item.get("known") or []))
                        for later_item in later_items)
                    if not known_hit:
                        continue
                    # 中间章的事件/未决提到过这个秘密 → 算有交代，不算矛盾；
                    # 注意「X 在本章已知道」本身不是交代，交代必须有事件依据。
                    if _reveal_between(ready[index + 1:later_index + 1], name, secret):
                        break
                    findings.append(_finding(
                        type="knowledge_conflict", severity="tentative",
                        chapters=[row["no"], later["no"]], names=[name],
                        summary=(f"第 {row['no']} 章末{name}还不知道「{secret}」，"
                                 f"第 {later['no']} 章末已知道，中间的章末状态没有记录知情经过；"
                                 "可能缺少交代。"),
                        evidence=[
                            _evidence(row["no"], _quote(contents.get(row["no"], ""), secret),
                                      f"{name}不知道：{secret}"),
                            _evidence(later["no"], _quote(contents.get(later["no"], ""), secret),
                                      f"{name}已知：{secret}"),
                        ],
                    ))
                    break
    return findings


def _reveal_between(rows: list[dict[str, Any]], name: str, secret: str) -> bool:
    """中间章是否已交代知情经过：事件/未决线索里提到这个秘密。

    只认事件类条目——「X 的 known 里出现了秘密」是待核对的结果，不是交代本身。
    """
    for row in rows:
        state = row["state"]
        for key in ("events", "open_threads"):
            for text in state.get(key) or []:
                if secret in str(text):
                    return True
    return False


def _check_relations(ready: list[dict[str, Any]],
                     contents: dict[int, str]) -> list[dict[str, Any]]:
    """关系回退：同一对人物先记录「建立关系」，之后又记录「回到陌生/初次见面」。"""
    findings: list[dict[str, Any]] = []
    bonds: dict[tuple[str, ...], tuple[int, str]] = {}
    for row in ready:
        no = row["no"]
        relations = row["state"].get("relations") if isinstance(row["state"].get("relations"), list) else []
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            pair = _pair_key(relation.get("between") or [])
            change = str(relation.get("change") or "").strip()
            if not pair or not change:
                continue
            names = list(pair)
            if pair not in bonds and any(marker in change for marker in _BOND_MARKERS):
                bonds[pair] = (no, change)
                continue
            if pair in bonds:
                bond_no, bond_change = bonds[pair]
                if any(marker in change for marker in _STRANGER_MARKERS):
                    severity = "confirmed"
                    summary = (f"第 {bond_no} 章记录{names[0]}与{names[1]}「{bond_change}」，"
                               f"第 {no} 章又写成「{change}」；正文明确冲突。")
                elif any(marker in change for marker in _MEET_MARKERS):
                    severity = "tentative"
                    summary = (f"第 {bond_no} 章记录{names[0]}与{names[1]}「{bond_change}」，"
                               f"第 {no} 章又写成「{change}」；可能缺少交代。")
                else:
                    continue
                findings.append(_finding(
                    type="relation_conflict", severity=severity,
                    chapters=[bond_no, no], names=names, summary=summary,
                    evidence=[
                        _evidence(bond_no, _quote(contents.get(bond_no, ""), names[0]),
                                  f"{names[0]}、{names[1]}：{bond_change}"),
                        _evidence(no, _quote(contents.get(no, ""), names[0]),
                                  f"{names[0]}、{names[1]}：{change}"),
                    ],
                ))
    return findings


def report_is_stale(store: Any, book_id: str, report: dict[str, Any]) -> bool:
    """旧报告是否过期：任一章当前指针与检查时不一致（含章被增删）。"""
    versions = report.get("chapter_versions") if isinstance(report.get("chapter_versions"), dict) else {}
    try:
        overview = store.chapter_state_overview(book_id)
    except (LookupError, WriterStoreError):
        return True
    current = {str(row.get("no") or ""): str(row.get("current_version_id") or "")
               for row in overview.get("states") or []}
    return current != {str(k): str(v) for k, v in versions.items()}
