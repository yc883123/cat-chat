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
                parsed = parse_input_decl(spec)
                if parsed is not None:
                    fields[str(field_name)] = parsed
        return fields
