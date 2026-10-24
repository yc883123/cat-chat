"""ComfyUI 节点输入声明（``/object_info``）的读取与解析：参数扫描的 schema 增强。

计划（docs/plans/2026-10-24-造梦机控件与媒体闭环计划.md §3.1）：
- 只在导入 / 参数 schema 刷新时访问，不进任务轮询和生成步骤；
- 查询按节点类缓存（单节点响应远小于全量 object_info）；
- ComfyUI 离线、插件节点未安装、字段声明缺失或响应格式变化时**一律降级**：
  返回 None，扫描器退回显式规则与 API prompt 类型判断，不阻断工作流读取；
- 对声明不明确的自定义节点不凭字段名推断媒体上传协议，不把连线端口
  伪装成可编辑槽位。
"""

from __future__ import annotations

import threading
from typing import Any

from naiba.comfyui import client


_AUTOGROW_TYPE_PREFIX = "COMFY_AUTOGROW"


def parse_autogrow_decl(spec: Any) -> dict[str, Any] | None:
    """解析 Autogrow 动态输入组声明；不是 Autogrow 返回 None。

    目标环境（ComfyUI 0.42.0 / MiniMax H3）的形态：
    ``["COMFY_AUTOGROW_V3", {"template": {"input": {"required": {...}},
    "prefix": "ref_image_", "min": 0, "max": 9}}]``。

    ``min`` 是模板展开后必须存在的槽位数（0 起编号 < min 的槽位为 required）；
    ``prefix``/``names`` 决定真实字段名，``max`` 是可选槽位总数。任何一项缺失或
    形状不对都返回 None——调用方按「无法确认」处理，不拿相似字符串猜可选性。
    """
    if not isinstance(spec, (list, tuple)) or len(spec) < 2:
        return None
    type_name = str(spec[0] or "").upper()
    if not type_name.startswith(_AUTOGROW_TYPE_PREFIX):
        return None
    config = spec[1] if isinstance(spec[1], dict) else {}
    template = config.get("template") if isinstance(config.get("template"), dict) else None
    if template is None:
        return None
    input_block = template.get("input") if isinstance(template.get("input"), dict) else {}
    required_block = input_block.get("required")
    optional_block = input_block.get("optional")
    if isinstance(required_block, dict) and required_block:
        template_required: bool | None = True
    elif isinstance(optional_block, dict) and optional_block:
        template_required = False
    else:
        template_required = None
    raw_names = template.get("names") if isinstance(template.get("names"), list) else []
    names = [str(name) for name in raw_names if isinstance(name, str) and name]
    prefix = template.get("prefix")
    if not isinstance(prefix, str):
        prefix = None
    min_count = template.get("min", 1)
    if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 0:
        min_count = None
    max_count = template.get("max")
    if isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < 1:
        max_count = None
    if names:
        max_count = len(names)
    if not names and prefix is None:
        return None
    return {
        "prefix": prefix,
        "names": names,
        "min": min_count,
        "max": max_count,
        "template_required": template_required,
    }


def _autogrow_index(template: dict[str, Any], suffix: str) -> int | None:
    """把 ``ref_image_3`` 映射回模板槽位索引；字段形状不符返回 None。"""
    names = template.get("names")
    if isinstance(names, list) and names:
        try:
            return names.index(suffix)
        except ValueError:
            return None
    prefix = template.get("prefix")
    max_count = template.get("max")
    if not isinstance(prefix, str) or not isinstance(max_count, int):
        return None
    if not suffix.startswith(prefix):
        return None
    tail = suffix[len(prefix):]
    if not tail.isdigit():
        return None
    index = int(tail)
    # 真实字段名是 prefix + 不带前导零的编号；ref_image_01 不是 ref_image_1。
    if str(index) != tail or not 0 <= index < max_count:
        return None
    return index


def input_is_optional(declarations: dict[str, dict[str, Any]] | None,
                      field_name: str) -> bool | None:
    """判断一个连线目标字段能否安全省略：True / False / None（无法确认）。

    规则：
    - 精确字段声明存在：直接采用 required/optional 归属；
    - 动态输入（``ref_images.ref_image_3``）回查 ``ref_images`` 的 Autogrow 声明，
      校验前缀/编号范围与 ``min``；前导零、越界、前缀不符都不算匹配；
    - 没有精确声明、也不是可解析的 Autogrow：返回 None，绝不凭相似前缀猜。
    """
    if not isinstance(declarations, dict) or not declarations:
        return None
    field_name = str(field_name or "")
    if not field_name:
        return None
    exact = declarations.get(field_name)
    if isinstance(exact, dict) and exact.get("kind") != "autogrow":
        return bool(exact.get("optional"))
    group_key = ""
    group: dict[str, Any] | None = None
    for key, value in declarations.items():
        if not isinstance(value, dict) or value.get("kind") != "autogrow":
            continue
        key = str(key)
        if field_name.startswith(f"{key}.") and len(key) > len(group_key):
            group_key, group = key, value
    if group is None:
        return None
    template = group.get("autogrow") if isinstance(group.get("autogrow"), dict) else {}
    index = _autogrow_index(template, field_name[len(group_key) + 1:])
    if index is None:
        return None
    if not bool(group.get("optional")):
        return False
    template_required = template.get("template_required")
    min_count = template.get("min")
    if template_required is None or not isinstance(min_count, int):
        return None
    if template_required and index < min_count:
        return False
    return True


def parse_input_decl(spec: Any) -> dict[str, Any] | None:
    """解析一个输入声明为可编辑控件语义；连线端口 / 未知类型返回 None。

    兼容两种声明形态（目标环境 ComfyUI 0.42.0 实测两种并存）：
    - 旧式枚举：``[["a", "b"], {...}]``；
    - 新式：``["COMBO", {"options": [...]}]``、``["STRING", {"multiline": True}]``、
      ``["INT", {...}]``、``["BOOLEAN", {...}]`` 等。
    ``STRING``/``COMBO`` 之外的自定义类型名（第三方自定义连线类型）没有可靠
    语义，一律不当作可编辑控件——宁缺勿错。
    """
    if not isinstance(spec, (list, tuple)) or not spec:
        return None
    first = spec[0]
    if isinstance(first, (list, tuple)):
        # 旧式枚举：整表都是字符串才算枚举；混入非字符串的不可靠声明不当作控件。
        if not all(isinstance(item, str) and str(item).strip() for item in first):
            return None
        return {"kind": "combo",
                "candidates": [str(item) for item in first],
                "multiline": False}
    config = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    options = config.get("options")
    if isinstance(options, (list, tuple)):
        candidates = [str(item) for item in options
                      if isinstance(item, str) and str(item).strip()]
        if candidates:
            return {"kind": "combo", "candidates": candidates, "multiline": False}
    upper = str(first).upper()
    if upper == "STRING":
        return {"kind": "text", "candidates": None,
                "multiline": bool(config.get("multiline"))}
    if upper in ("INT", "FLOAT", "NUMBER"):
        return {"kind": "number", "candidates": None, "multiline": False}
    if upper in ("BOOLEAN", "BOOL"):
        return {"kind": "bool", "candidates": None, "multiline": False}
    return None


class NodeSchemaProvider:
    """按节点类缓存 ``/object_info/<类名>`` 的输入声明解析结果。

    缓存含「拿不到 = None」的结果：同一轮增强里反复失败不再重复请求。
    """

    def __init__(self, base: str, timeout: int = 8):
        self._base = base
        self._timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, dict[str, Any]] | None] = {}

    def node_inputs(self, class_type: str) -> dict[str, dict[str, Any]] | None:
        """返回 ``{字段名: 声明}``；节点不存在 / 离线 / 解析失败返回 None（降级）。"""
        class_type = str(class_type or "").strip()
        if not class_type:
            return None
        with self._lock:
            if class_type in self._cache:
                return self._cache[class_type]
        fields = self._fetch(class_type)
        with self._lock:
            self._cache[class_type] = fields
        return fields

    def _fetch(self, class_type: str) -> dict[str, dict[str, Any]] | None:
        node = client.object_info_node(self._base, class_type, timeout=self._timeout)
        if node is None:
            return None
        inputs = node.get("input") if isinstance(node.get("input"), dict) else {}
        fields: dict[str, dict[str, Any]] = {}
        for scope in ("required", "optional"):
            block = inputs.get(scope) if isinstance(inputs.get(scope), dict) else {}
            for field_name, spec in block.items():
                autogrow = parse_autogrow_decl(spec)
                if autogrow is not None:
                    fields[str(field_name)] = {
                        "kind": "autogrow",
                        "optional": scope == "optional",
                        "autogrow": autogrow,
                    }
                    continue
                parsed = parse_input_decl(spec)
                fields[str(field_name)] = {
                    **(parsed or {"kind": "unknown"}),
                    "optional": scope == "optional",
                }
        return fields
