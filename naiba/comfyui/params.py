"""工作流参数扫描：导入时从 API 工作流自动派生可编辑槽位（不要求手写模板）。

扫描规则（绘影的 adapter 设计子集，按 Cat Chat 口径收敛）：
- 遍历每个节点的 ``inputs``；**值是数组的 = 节点连线**，跳过；
- 标量值（str/int/float/bool）是控件槽位，按原值类型分类：text / number / bool；
- ``seed`` / ``noise_seed`` 数值槽标为 seed 类：默认每次随机（上限 2^50，
  沿用 2026-09-20 教训），用户也可以固定；
- ``LoadImage.filename`` 标为图片槽位（只标记，本期不上传，P5 再接）；
- **主提示词识别置顶**（绘影函数 h 的规则子集）：从采样器节点的 ``positive`` /
  ``negative`` 连线回溯到来源节点的 text 槽，标为 positive_prompt / negative_prompt；
  连线追不到时按节点/标题名兜底；提示词槽位前端用多行输入框渲染并置顶；
- 节点 ``_meta.title`` 用于生成人类可读的槽位标签。
"""

from __future__ import annotations

from typing import Any

from naiba.comfyui import MAX_WORKFLOW_PARAMS

_SEED_FIELDS = ("seed", "noise_seed")
ROLE_POSITIVE = "positive_prompt"
ROLE_NEGATIVE = "negative_prompt"
# 采样器类节点：positive/negative 连线是主提示词的可靠锚点。
_SAMPLER_CLASS_MARKERS = ("ksampler", "samplercustom")
# 标题兜底关键词（连线追不到时用）。
_POSITIVE_TITLE_MARKERS = ("正向", "正面", "positive")
_NEGATIVE_TITLE_MARKERS = ("负向", "负面", "negative")
_TITLE_PROMPT_MARKERS = ("提示词", "prompt")


def _field_type(node_class: str, field_name: str, value: Any) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        if field_name in _SEED_FIELDS:
            return "seed"
        return "number"
    if isinstance(value, str):
        if node_class == "LoadImage" and field_name == "filename":
            return "image"
        return "text"
    return None


def _is_sampler(node: dict[str, Any]) -> bool:
    class_type = str(node.get("class_type") or "").lower()
    return any(marker in class_type for marker in _SAMPLER_CLASS_MARKERS)


def _prompt_roles(prompt: dict[str, Any]) -> dict[tuple[str, str], str]:
    """主提示词槽位识别：采样器 positive/negative 连线回溯 + 标题兜底。

    返回 ``{(node_id, field_name): role}``。规则（绘影函数 h 的子集）：
    1. 采样器节点的 ``positive`` / ``negative`` 连线指到哪个节点，那个节点如果有
       text 槽就是主提示词（正向 / 负向）——这是最可靠的锚点；
    2. 追不到连线时按节点标题/类名兜底（含「正向/positive」+提示词 → 正向）。
    """
    roles: dict[tuple[str, str], str] = {}
    for node_id, node in (prompt or {}).items():
        if not isinstance(node, dict) or not _is_sampler(node):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        for conn_field, role in (("positive", ROLE_POSITIVE), ("negative", ROLE_NEGATIVE)):
            conn = inputs.get(conn_field)
            if not (isinstance(conn, list) and conn):
                continue
            source = (prompt or {}).get(str(conn[0]))
            if not isinstance(source, dict):
                continue
            source_inputs = source.get("inputs") if isinstance(source.get("inputs"), dict) else {}
            if "text" in source_inputs and not isinstance(source_inputs.get("text"), list):
                roles[(str(conn[0]), "text")] = role
    # 标题兜底：只补连线法没覆盖到的文本槽。
    for node_id, node in (prompt or {}).items():
        if not isinstance(node, dict):
            continue
        if (str(node_id), "text") in roles:
            continue  # 连线法已识别，标题兜底不再覆盖。
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        text = inputs.get("text")
        if not isinstance(text, str):
            continue
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        title = str(meta.get("title") or "")
        haystack = f"{title} {node.get('class_type') or ''}".lower()
        if not any(marker in haystack for marker in _TITLE_PROMPT_MARKERS):
            continue
        if any(marker in haystack for marker in _NEGATIVE_TITLE_MARKERS):
            roles.setdefault((str(node_id), "text"), ROLE_NEGATIVE)
        elif any(marker in haystack for marker in _POSITIVE_TITLE_MARKERS):
            roles.setdefault((str(node_id), "text"), ROLE_POSITIVE)
    return roles


def scan_params(prompt: dict[str, Any]) -> list[dict[str, Any]]:
    """从 API 工作流扫描全部可编辑槽位；顺序 = 节点在 JSON 里的原始顺序。"""
    prompt = prompt or {}
    roles = _prompt_roles(prompt)
    slots: list[dict[str, Any]] = []
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        class_type = str(node.get("class_type") or "")
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        node_title = str(meta.get("title") or "")
        for field_name, value in inputs.items():
            # 数组 = 来自上游节点的连线，不是控件值。
            if isinstance(value, list):
                continue
            field_type = _field_type(class_type, str(field_name), value)
            if field_type is None:
                continue
            slots.append({
                "node_id": str(node_id),
                "field_name": str(field_name),
                "field_type": field_type,
                # seed 槽 default 置空：语义是「不填 = 每次随机」，不展示旧随机值。
                "default": None if field_type == "seed" else value,
                "node_title": node_title,
                "class_type": class_type,
                "role": roles.get((str(node_id), str(field_name)), ""),
                "label": _slot_label(str(node_id), node_title, class_type, str(field_name)),
            })
            if len(slots) >= MAX_WORKFLOW_PARAMS:
                return slots
    return slots


def _slot_label(node_id: str, node_title: str, class_type: str, field_name: str) -> str:
    head = node_title or class_type or node_id
    return f"{node_id} · {head} · {field_name}"


def apply_overrides(
    prompt: dict[str, Any],
    overrides: list[dict[str, Any]],
    *,
    randomize_seeds: bool = True,
) -> dict[str, Any]:
    """把用户填值按原值类型保真地写进工作流副本，返回新对象（不改原 JSON）。

    - 覆盖项只写「槽位存在且类型可保真转换」的值；未列出的槽位保持原样；
    - seed 槽：用户给了 ≥0 的数就固定；给了负数或不给且 ``randomize_seeds``
      为真时随机（上限 2^50）。
    """
    from naiba.comfyui.client import random_seed

    import copy

    workflow = copy.deepcopy(prompt)
    override_map: dict[tuple[str, str], Any] = {}
    for item in overrides or []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("node_id") or ""), str(item.get("field_name") or ""))
        if key[0] and key[1]:
            override_map[key] = item.get("value")

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field_name, current in list(inputs.items()):
            key = (str(node_id), str(field_name))
            is_seed = str(field_name) in _SEED_FIELDS and isinstance(current, (int, float)) \
                and not isinstance(current, bool)
            if key in override_map:
                value = override_map.pop(key)
                converted = _convert(current, value)
                if converted is not _MISSING:
                    # seed 槽的负数是「随机」哨兵（rgthree 等节点用 -1 表示随机），
                    # 不把负数写进提交，一律按随机处理。
                    if is_seed and isinstance(converted, (int, float)) and converted < 0:
                        inputs[field_name] = random_seed()
                    else:
                        inputs[field_name] = converted
                    continue
            if is_seed:
                # 未覆盖的 seed：负数（随机哨兵）必随机；randomize_seeds 开着时
                # 未固定值也随机（同一段工作流多次提交产出不同图）。
                if (isinstance(current, (int, float)) and current < 0) or randomize_seeds:
                    inputs[field_name] = random_seed()
    return workflow


_MISSING = object()


def _convert(current: Any, value: Any) -> Any:
    """按槽位当前值的类型做保真转换；转不动返回哨兵（保持原值）。"""
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "是"):
            return True
        if text in ("false", "0", "否"):
            return False
        return _MISSING
    if isinstance(current, (int, float)):
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _MISSING
        if isinstance(current, int) and number.is_integer():
            return int(number)
        return number
    if isinstance(current, str):
        return str(value)
    return _MISSING
