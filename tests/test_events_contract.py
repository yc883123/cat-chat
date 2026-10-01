# -*- coding: utf-8 -*-
"""事件契约守门（事件体系重构阶段 2）：静态扫描全部发射点。

历史守门（golden 反查）只能覆盖录制场景——发射超集（不在枚举里的 type）与
未登记负载键全部漏网。本守门改为 **AST 静态扫描**：

- 识别事件调用形状：``emit/_emit/append_run_event/status`` 的属性调用，或
  ``event/status`` 裸名回调调用；
- payload 参数必须是 dict 字面量，或函数内可解析的同名变量（含后续键赋值）；
- 校验：type 字面量 ∈ EventType；字面量键 ⊆ EVENT_PAYLOAD_KEYS[type] ∪ 公共键
  （宽松条目仅校验 type）；
- 无法静态解析（变量型 type/星号展开）→ 收集为 unresolved，断言为空（防逃逸）。

只匹配"事件调用形状"，多模态 content 块（``{"type":"image"}`` 等）不在
可调用参数位置，天然不受影响。
"""

import ast
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.contracts import EVENT_PAYLOAD_KEYS, EventType  # noqa: E402

# 事件调用形状（attr/裸名）。
CALL_ATTRS = {"emit", "_emit", "append_run_event", "status"}
CALL_NAMES = {"event", "status"}
NAIBA_DIR = ROOT / "naiba"


def _literal_dict(node: ast.AST) -> tuple[str | None, set[str]] | None:
    """dict 字面量 → (type 字面量 or None, 键集合)；非字面量 dict 返回 None。"""
    if not isinstance(node, ast.Dict):
        return None
    keys: set[str] = set()
    type_val: str | None = None
    for key, value in zip(node.keys, node.values):
        if key is None or not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        keys.add(key.value)
        if key.value == "type" and isinstance(value, ast.Constant) and isinstance(value.value, str):
            type_val = value.value
    return type_val, keys


def _module_top_assigns(tree: ast.Module) -> dict[str, tuple[str | None, set[str]]]:
    """仅模块直接子语句（不进入函数/类体）的 Name → dict 字面量映射。"""
    assigns: dict[str, tuple[str | None, set[str]]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            literal = _literal_dict(node.value)
            if literal is not None:
                assigns[node.targets[0].id] = literal
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            literal = _literal_dict(node.value)
            if literal is not None:
                assigns[node.target.id] = literal
    return assigns


def _collect_assigns(tree: ast.AST, *, allow_nested_functions: bool = True) -> dict[str, tuple[str | None, set[str]]]:
    """收集树内「Name → dict 字面量（含后续下标键赋值）」映射，供 payload 变量解析。

    ``allow_nested_functions=False`` 时不进入嵌套函数体（函数作用域归属各自条目）。
    """
    assigns: dict[str, tuple[str | None, set[str]]] = {}
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else (
                node.target if isinstance(node, ast.AnnAssign) else None
            )
            if isinstance(target, ast.Name) and value is not None:
                literal = _literal_dict(value)
                if literal is not None:
                    type_val, keys = literal
                    existing = assigns.get(target.id)
                    if existing is None:
                        assigns[target.id] = (type_val, set(keys))
                    else:
                        assigns[target.id] = (existing[0] if existing[0] is not None else type_val, existing[1] | keys)
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            target = node.value.id
            key = node.slice
            if target in assigns and isinstance(key, ast.Constant) and isinstance(key.value, str):
                type_val, keys = assigns[target]
                assigns[target] = (type_val, keys | {key.value})
        for child in ast.iter_child_nodes(node):
            if not allow_nested_functions and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            pending.append(child)
    return assigns


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """node → 父节点映射（作用域解析用）。"""
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _function_scopes(tree: ast.AST) -> dict[ast.AST, dict[str, tuple[str | None, set[str]]]]:
    """每个 FunctionDef 节点 → 其函数体的 assigns（嵌套下级函数各自独立收集）。"""
    scopes: dict[ast.AST, dict[str, tuple[str | None, set[str]]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        scopes[node] = _collect_assigns(node, allow_nested_functions=False)
    return scopes


def _resolve_payload_name(
    name: str,
    call_node: ast.Call,
    parent: dict[ast.AST, ast.AST],
    function_scopes: dict[ast.AST, dict[str, tuple[str | None, set[str]]]],
    module_assigns: dict[str, tuple[str | None, set[str]]],
) -> tuple[str | None, set[str]] | None:
    """按词法作用域链（最内层函数 → 模块）解析 Name → dict 字面量信息。"""
    node: ast.AST | None = call_node
    while node is not None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            scope_assigns = function_scopes.get(node)
            if scope_assigns is not None and name in scope_assigns:
                return scope_assigns[name]
        node = parent.get(node)
    return module_assigns.get(name)


def _payload_arg(call_node: ast.Call) -> ast.AST | None:
    """事件调用的 payload 参数（emit 系第二参、回调系第一参）；非事件调用返回 None。"""
    func = call_node.func
    if isinstance(func, ast.Attribute) and func.attr in CALL_ATTRS:
        return call_node.args[1] if len(call_node.args) >= 2 else None
    if isinstance(func, ast.Name) and func.id in CALL_NAMES:
        return call_node.args[0] if call_node.args else None
    return None


def _scan_emissions() -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """返回 (违规清单, 无法静态解析清单)。

    豁免：行尾 ``# noqa: event-internal`` 标记者跳过——sink 内部输入形态
    （tool_requested/tool_started 经 _RunEventSink 归一为 tool_start 后才入表），
    属显式声明过的内部协议，不参与 wire 契约校验。
    """
    violations: list[tuple[str, int, str]] = []
    unresolved: list[tuple[str, int, str]] = []
    for path in sorted(NAIBA_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
        module_assigns = _module_top_assigns(tree)
        function_scopes = _function_scopes(tree)
        parent = _parent_map(tree)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            payload = _payload_arg(node)
            if payload is None:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if node.end_lineno and node.end_lineno <= len(lines) and "noqa: event-internal" in lines[node.end_lineno - 1]:
                continue
            if isinstance(payload, ast.Dict):
                literal = _literal_dict(payload)
            elif isinstance(payload, ast.Name):
                literal = _resolve_payload_name(payload.id, node, parent, function_scopes, module_assigns)
                if literal is None:
                    continue  # 转发调用（payload 为参数/外部入参）：真实发射点在上游已扫描
            else:
                literal = None
            if literal is None:
                unresolved.append((rel, node.lineno, "payload 无法静态解析（变量/星号展开需人工确认）"))
                continue
            type_val, keys = literal
            if type_val is None:
                unresolved.append((rel, node.lineno, "type 不是字面量（需人工确认其登记进 EventType）"))
                continue
            if type_val not in EventType.__members__.values():
                violations.append((rel, node.lineno, f"未知事件 type：{type_val!r}（未登记进 EventType）"))
                continue
            allowed = EVENT_PAYLOAD_KEYS.get(type_val, frozenset())
            if allowed is None:
                continue  # 宽松条目（job 域动态字段）：仅校验 type 存在性
            extra = sorted(keys - allowed - {"type", "run_id", "sequence", "created_at"})
            if extra:
                violations.append((rel, node.lineno, f"事件 {type_val} 负载键未登记进契约图：{extra}"))
    return violations, unresolved


class EventEmissionContractTests(unittest.TestCase):
    def test_all_emission_points_validate(self):
        violations, unresolved = _scan_emissions()
        self.assertEqual(violations, [], "事件发射点存在契约违规:\n" + "\n".join(
            f"  {rel}:{line} {note}" for rel, line, note in violations
        ))
        self.assertEqual(unresolved, [], "事件发射点存在无法静态解析的 payload:\n" + "\n".join(
            f"  {rel}:{line} {note}" for rel, line, note in unresolved
        ))

    def test_event_payload_keys_cover_all_enum_members(self):
        missing = sorted(set(EventType.__members__.values()) - set(EVENT_PAYLOAD_KEYS))
        self.assertEqual(missing, [], f"EventType 成员缺少 EVENT_PAYLOAD_KEYS 条目：{missing}")
        extra = sorted(set(EVENT_PAYLOAD_KEYS) - set(EventType.__members__.values()))
        self.assertEqual(extra, [], f"EVENT_PAYLOAD_KEYS 存在非枚举条目：{extra}")

    def test_spread_keys_within_event_payload_union(self):
        # 契约图键必须 ⊆ EventPayload 并集（并集是 wire 类型标注的底）。
        from naiba.core.contracts import EventPayload

        union = set(EventPayload.__annotations__)
        for kind, allowed in EVENT_PAYLOAD_KEYS.items():
            if allowed is None:
                continue
            missing = sorted(allowed - union)
            self.assertEqual(missing, [], f"事件 {kind} 契约图键未在 EventPayload 并集登记：{missing}")

    def test_done_payload_carries_no_full_message(self):
        """终态 `done` 不带完整消息对象（2026-10 起）。

        带一份就等于把整条消息（含最占体积的 `metadata.trace`，实测单轮最长 318KB）写进
        `run_events`、终态再被 `slim_terminal_run` 删掉——纯空转。完整消息由前端收尾重载
        会话（`GET /api/conversations/<id>`）提供；事件只留"只有事件能给"的即时动作字段。
        """
        allowed = EVENT_PAYLOAD_KEYS["done"]
        self.assertNotIn("message", allowed, "done 不得再携带完整消息对象")
        self.assertIn("session_start", allowed, "模型重置上下文后的种子预填只走事件")


class ValidateEventPayloadTests(unittest.TestCase):
    """validate_event_payload 行为单测（严格模式核心）。"""

    def setUp(self):
        os.environ["NAIBA_STRICT_EVENTS"] = "0"  # 测试不依赖环境开关

    def test_known_event_passes(self):
        from naiba.events import validate_event_payload

        self.assertEqual(validate_event_payload({"type": "status", "message": "ok"}), [])
        self.assertEqual(validate_event_payload({"type": "delta", "content": "hi"}), [])
        self.assertEqual(validate_event_payload({"type": "job_status", "status": "running", "kind": "shell"}), [])

    def test_unknown_type_rejected(self):
        from naiba.events import validate_event_payload

        issues = validate_event_payload({"type": "not_a_real_event", "x": 1})
        self.assertTrue(any("未知事件 type" in item for item in issues))

    def test_unregistered_keys_rejected(self):
        from naiba.events import validate_event_payload

        issues = validate_event_payload({"type": "status", "message": "ok", "nope": 1})
        self.assertTrue(any("负载键未登记" in item for item in issues))

    def test_heartbeat_only_type(self):
        from naiba.events import validate_event_payload

        self.assertEqual(validate_event_payload({"type": "heartbeat"}), [])


if __name__ == "__main__":
    unittest.main()
