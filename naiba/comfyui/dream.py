# -*- coding: utf-8 -*-
"""造梦机对话域（计划 docs/plans/2026-11-17-造梦机对话预设Agent与工具集设计.md）。

三块职责，全部只经由造梦机既有服务/存储层动作，不旁路执行：

- **对话参数配置**（ChatConfigStore）：按工作流 ID + 指纹绑定，保存用户在
  「对话参数」弹窗里逐条指认的用途绑定（提示词/尺寸/媒体/自定义参数）。
  指纹不一致即失效，绑定不静默漂移；这里只存配置，不改工作流本体。
- **确定性尺寸计算**（resolve_size）：比例 + MP → 实际宽高。W≈sqrt(P×r)、
  H≈sqrt(P/r) 取整，节点声明提供上下限时夹取；实际比例/MP 与取整说明如实
  返回，差异明显时列为待确认项，不让模型自由填写。
- **准备快照**（DreamSnapshotStore / DreamService.prepare）：有期限的不可变
  快照，在预检时就把单次/批量的**最终提交体**全部展开并冻结（工作流指纹、
  参数修改、素材引用、单次/批量模式与实际执行次数）；``dream_submit`` 只接受
  快照 ID，提交层零解释。request_key 在 prepare 时由宿主生成并冻结，
  超时重试落回同一条任务，不会重复生成。

执行归属（ownership）：快照与提交关联记录发起会话（conversation 形态）；
总管受管 Run 形态（thread/node/attempt）在记录结构里预留字段，当前版本
只有普通主对话入口会创建快照（计划 §7.7：首版只交付主对话入口）。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from naiba.comfyui import PARAMS_SCHEMA_VERSION
from naiba.comfyui.store import ComfyUIStore, ComfyUIStoreError, now_ms

logger = logging.getLogger("naiba.comfyui.dream")

# 快照回执的读-改-写在同进程内串行化；唯一的 .tmp 名避免多进程/多线程写同一
# 临时文件（Windows 上会 PermissionError，静默丢一次回执写入）。
_SNAPSHOT_WRITE_LOCK = threading.RLock()

# 准备快照的有效期：对话一轮的确认窗口足够；过期未提交必须重新准备（工作流
# 与素材都可能已变化）。已提交的快照永久保留提交回执，供对账与幂等重放。
DREAM_SNAPSHOT_TTL_MS = 30 * 60 * 1000
# 快照文件自身保留期：过期未提交的记录在这个期限后被创建动作顺手清理。
DREAM_SNAPSHOT_KEEP_MS = 24 * 60 * 60 * 1000

# 单份配置的绑定与槽位上限（弹窗逐条添加，防御手攒请求把配置撑爆）。
DREAM_MAX_BINDINGS = 32
DREAM_MAX_SLOTS_PER_BINDING = 8
# 批量条目上限（与既有批次 MAX_SCHEMES 内单方案条目口径一致）。
DREAM_MAX_BATCH_ITEMS = 50

# 用途枚举：与计划 §5.4 指认表一致。media 单列（媒体槽走 media_inputs 通道）。
DREAM_PURPOSES = (
    "positive_prompt", "negative_prompt", "size", "seed",
    "number", "enum", "text", "bool", "media",
)
# ask 策略：每次询问 / 用户提到再改 / 保持默认。
DREAM_ASK_MODES = ("always", "on_mention", "keep_default")
# 预设默认收集项：提示词、尺寸、必填素材（计划 §5.4）。
DREAM_COLLECT_PURPOSES = frozenset({"positive_prompt", "size", "media"})

_MEDIA_TYPES = ("image", "audio", "video")
_PURPOSE_SLOT_TYPES: dict[str, frozenset[str]] = {
    "positive_prompt": frozenset({"text"}),
    "negative_prompt": frozenset({"text"}),
    "size": frozenset({"number", "text"}),
    "seed": frozenset({"seed"}),
    "number": frozenset({"number"}),
    "enum": frozenset({"text", "number", "bool"}),
    "text": frozenset({"text"}),
    "bool": frozenset({"bool"}),
    "media": frozenset({"image", "audio", "video"}),
}


class DreamConfigError(ValueError):
    """对话参数配置非法（弹窗保存与工具读取共用）。"""


class DreamPrepareError(ValueError):
    """预检未通过（缺绑定、缺素材、尺寸无法满足等）。"""


class DreamSubmitError(RuntimeError):
    """提交被拒绝或无法核实（快照过期、指纹不符等）。"""


def _clean_text(value: Any, limit: int, label: str, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise DreamConfigError(f"{label}不能为空")
    if len(text) > limit:
        raise DreamConfigError(f"{label}最长 {limit} 字")
    if any(ord(ch) < 32 for ch in text):
        raise DreamConfigError(f"{label}含控制字符")
    return text


# ---------------------------------------------------------------------------
# 对话参数配置（按工作流 ID + 指纹绑定）
# ---------------------------------------------------------------------------


class ChatConfigStore:
    """「对话参数」弹窗的服务端配置存储。

    一份配置 = 一个工作流的全部用途绑定；文件名即工作流 ID（重命名工作流
    不换 ID，指纹变化时配置整条失效，需要用户在弹窗里重新保存）。
    """

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir) / "comfyui" / "chat-configs"

    def _path(self, workflow_id: str) -> Path:
        if not re.fullmatch(r"cw-[0-9a-zA-Z_-]{4,64}", str(workflow_id or "")):
            raise DreamConfigError("工作流 ID 不合法")
        return self._dir / f"{workflow_id}.json"

    def load(self, workflow_id: str) -> dict[str, Any] | None:
        try:
            path = self._path(workflow_id)
        except DreamConfigError:
            return None
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("对话参数配置读取失败：%s（%s）", path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def save(
        self, workflow_id: str, fingerprint: str, bindings: Any,
        *, workflow_name: str = "",
    ) -> dict[str, Any]:
        """校验并落盘一份配置；返回保存后的完整记录。"""
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise DreamConfigError("工作流指纹缺失或不合法，请重新打开弹窗后保存")
        cleaned = validate_bindings(bindings)
        record = {
            "workflow_id": str(workflow_id),
            "workflow_fingerprint": fingerprint,
            "updated_at": now_ms(),
            "bindings": cleaned,
        }
        if workflow_name:
            record["workflow_name"] = _clean_text(workflow_name, 120, "工作流名")
        path = self._path(workflow_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise DreamConfigError(f"对话参数配置保存失败：{exc}") from None
        return record

    def delete(self, workflow_id: str) -> bool:
        """删除配置；返回是否真的删掉了文件（不存在返回 False，供界面如实回报）。"""
        try:
            path = self._path(workflow_id)
        except DreamConfigError:
            return False
        try:
            existed = path.is_file()
            path.unlink(missing_ok=True)
            return existed
        except OSError:
            return False


def validate_bindings(raw: Any) -> list[dict[str, Any]]:
    """校验用户提交的绑定数组（弹窗保存的唯一入口）。"""
    if not isinstance(raw, list):
        raise DreamConfigError("bindings 必须是数组")
    if len(raw) > DREAM_MAX_BINDINGS:
        raise DreamConfigError(f"绑定最多 {DREAM_MAX_BINDINGS} 条")
    cleaned: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise DreamConfigError(f"第 {index} 条绑定格式必须是对象")
        purpose = str(item.get("purpose") or "").strip()
        if purpose not in DREAM_PURPOSES:
            raise DreamConfigError(f"第 {index} 条绑定的用途不合法：{purpose or '（空）'}")
        display_name = _clean_text(item.get("display_name"), 60, f"第 {index} 条显示名", required=True)
        description = _clean_text(item.get("description"), 200, f"第 {index} 条用途说明")
        ask = str(item.get("ask") or "on_mention").strip()
        if ask not in DREAM_ASK_MODES:
            raise DreamConfigError(f"第 {index} 条的询问策略不合法：{ask}")
        slots_raw = item.get("slots")
        if not isinstance(slots_raw, list) or not slots_raw:
            raise DreamConfigError(f"「{display_name}」还没有指认任何参数槽位")
        if len(slots_raw) > DREAM_MAX_SLOTS_PER_BINDING:
            raise DreamConfigError(
                f"「{display_name}」最多指认 {DREAM_MAX_SLOTS_PER_BINDING} 个槽位"
            )
        slots: list[dict[str, str]] = []
        seen_slots: set[tuple[str, str]] = set()
        for slot_index, slot in enumerate(slots_raw, 1):
            if not isinstance(slot, dict):
                raise DreamConfigError(f"「{display_name}」第 {slot_index} 个槽位格式必须是对象")
            node_id = str(slot.get("node_id") or "").strip()
            field_name = str(slot.get("field_name") or "").strip()
            if not node_id or not field_name:
                raise DreamConfigError(f"「{display_name}」第 {slot_index} 个槽位缺少参数地址")
            if (node_id, field_name) in seen_slots:
                continue
            seen_slots.add((node_id, field_name))
            slot_name = _clean_text(slot.get("name"), 60, f"「{display_name}」槽位名")
            slots.append({"node_id": node_id, "field_name": field_name, "name": slot_name})
        if purpose == "size" and len(slots) > 2:
            raise DreamConfigError(f"「{display_name}」是尺寸绑定：最多指认宽高两个槽位")
        per_item = bool(item.get("per_item"))
        if purpose in ("positive_prompt", "negative_prompt") and per_item:
            # 提示词逐条变化走批量条目的 prompt 字段，不靠 per_item 逐值覆盖。
            per_item = False
        binding_id = str(item.get("id") or "").strip() or f"b{index}"
        if not re.fullmatch(r"b[a-zA-Z0-9_-]{1,32}", binding_id):
            raise DreamConfigError(f"第 {index} 条绑定 ID 不合法")
        if binding_id in seen_ids:
            raise DreamConfigError(f"绑定 ID 重复：{binding_id}")
        seen_ids.add(binding_id)
        cleaned.append({
            "id": binding_id,
            "purpose": purpose,
            "display_name": display_name,
            "description": description,
            "slots": slots,
            "ask": ask,
            "per_item": per_item,
        })
    return cleaned


def check_bindings_against_params(
    bindings: list[dict[str, Any]], params: list[Any],
) -> list[str]:
    """把配置绑定与当前扫描参数对账，返回失效项说明（不抛错，供详情/预检呈现）。"""
    problems: list[str] = []
    slot_map: dict[tuple[str, str], dict[str, Any]] = {}
    for slot in params or []:
        if isinstance(slot, dict):
            slot_map[(str(slot.get("node_id") or ""), str(slot.get("field_name") or ""))] = slot
    for binding in bindings or []:
        if not isinstance(binding, dict):
            continue
        name = str(binding.get("display_name") or binding.get("id") or "?")
        purpose = str(binding.get("purpose") or "")
        allowed = _PURPOSE_SLOT_TYPES.get(purpose, frozenset())
        for slot in binding.get("slots") or []:
            key = (str(slot.get("node_id") or ""), str(slot.get("field_name") or ""))
            found = slot_map.get(key)
            if found is None:
                problems.append(f"「{name}」指认的 {key[0]} · {key[1]} 不在当前参数清单里（工作流可能已更新）")
                continue
            field_type = str(found.get("field_type") or "")
            if allowed and field_type not in allowed:
                problems.append(
                    f"「{name}」的用途（{purpose}）与槽位类型（{field_type}）不匹配：{key[0]} · {key[1]}"
                )
    return problems


# ---------------------------------------------------------------------------
# 确定性尺寸计算（计划 §5.5：程序实现，不让模型自由填写）
# ---------------------------------------------------------------------------

_RATIO_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[:：/]\s*(\d+(?:\.\d+)?)\s*$")


def parse_ratio(text: Any) -> float | None:
    """解析比例字符串（9:16 / 16:9 / 1.5）；非法返回 None，不猜。"""
    raw = str(text or "").strip()
    match = _RATIO_RE.match(raw)
    if match:
        width = float(match.group(1))
        height = float(match.group(2))
        if width <= 0 or height <= 0:
            return None
        return width / height
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _fmt_step(step: float) -> str:
    """步长显示成干净的字符串（8.0 → '8'，0.5 → '0.5'）。"""
    return str(int(step)) if float(step).is_integer() else str(step)


def resolve_size(
    request: dict[str, Any],
    width_slot: dict[str, Any] | None,
    height_slot: dict[str, Any] | None,
    limits: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """把用户的比例/MP 请求换算成实际宽高（确定性，无副作用）。

    ``request`` 支持三种写法：
    - ``{"ratio": "9:16", "mp": 1.0}``：比例 + 目标总像素（百万像素）；
    - ``{"ratio": "9:16"}``：沿用工作流当前宽高的总像素，只改比例；
    - ``{"width": 832, "height": 1216}``：直接指定宽高（仍按槽位上下限夹取）。

    ``limits``：``{"width": {"min","max"}, "height": {...}}``，来自节点实时
    声明；拿不到时不夹取并在结果里标注 ``limits_checked=False``。
    """
    result: dict[str, Any] = {
        "width": None, "height": None,
        "ratio_actual": None, "mp_actual": None,
        "limits_checked": bool(limits),
        "notes": [],
    }
    if not (isinstance(width_slot, dict) and isinstance(height_slot, dict)):
        result["error"] = "这份工作流没有指认宽高两个槽位，无法按比例/MP 计算尺寸"
        return result
    if not isinstance(request, dict):
        result["error"] = "尺寸请求格式必须是对象"
        return result
    notes = result["notes"]
    if request.get("width") is not None or request.get("height") is not None:
        try:
            width = int(round(float(request.get("width"))))
            height = int(round(float(request.get("height"))))
        except (TypeError, ValueError):
            result["error"] = "宽高必须是数字"
            return result
        if width <= 0 or height <= 0:
            result["error"] = "宽高必须是正数"
            return result
        base_ratio = width / height
    else:
        ratio = parse_ratio(request.get("ratio"))
        if ratio is None:
            result["error"] = "比例写法不合法：要像 9:16、16:9 这样的宽:高，或一个正数"
            return result
        try:
            base_mp = float(request.get("mp")) if request.get("mp") is not None else None
        except (TypeError, ValueError):
            result["error"] = "MP（百万像素）必须是数字，例如 1.0"
            return result
        if base_mp is not None and not 0 < base_mp <= 64:
            result["error"] = "MP 超出合理范围（0–64）"
            return result
        try:
            current_w = float(width_slot.get("default"))
            current_h = float(height_slot.get("default"))
        except (TypeError, ValueError):
            current_w = current_h = 0.0
        if base_mp is None:
            if current_w > 0 and current_h > 0:
                base_mp = (current_w * current_h) / 1_000_000.0
                notes.append(
                    f"未给 MP：沿用工作流当前尺寸 {int(current_w)}×{int(current_h)} "
                    f"的总像素（约 {base_mp:.2f}MP）"
                )
            else:
                result["error"] = "工作流当前宽高拿不到，请给出 MP（如 1.0）或直接给宽高"
                return result
        pixels = base_mp * 1_000_000.0
        width = int(round(math.sqrt(pixels * ratio)))
        height = int(round(math.sqrt(pixels / ratio)))
        if width <= 0 or height <= 0:
            result["error"] = "换算出的宽高不合法（MP 或比例过小）"
            return result
        base_ratio = ratio
    # 节点声明上下限夹取（schema 实时声明，不是猜测）。
    if isinstance(limits, dict):
        for name in ("width", "height"):
            bound = limits.get(name)
            if not isinstance(bound, dict):
                continue
            low, high = bound.get("min"), bound.get("max")
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
                continue
            value = width if name == "width" else height
            if value < low:
                notes.append(f"{name} {value} 低于节点下限 {int(low)}，已夹取")
                value = int(low)
            elif value > high:
                notes.append(f"{name} {value} 高于节点上限 {int(high)}，已夹取")
                value = int(high)
            if name == "width":
                width = value
            else:
                height = value
    # 节点声明 step（步长）对齐：仅在 schema 明确声明时做，不硬编码任何倍数。
    # 对齐后仍落在上下限内，否则退回对齐前的值并如实说明（不擅自改未知参数）。
    if isinstance(limits, dict):
        for name in ("width", "height"):
            bound = limits.get(name)
            if not isinstance(bound, dict):
                continue
            step = bound.get("step")
            if not isinstance(step, (int, float)) or step <= 0:
                continue
            value = width if name == "width" else height
            aligned = int(round(value / step) * step)
            low, high = bound.get("min"), bound.get("max")
            in_range = True
            if isinstance(low, (int, float)) and aligned < low:
                in_range = False
            if isinstance(high, (int, float)) and aligned > high:
                in_range = False
            if in_range and aligned > 0 and aligned != value:
                notes.append(f"{name} {value} 未按节点步长 {_fmt_step(step)} 对齐，已对齐到 {aligned}")
                if name == "width":
                    width = aligned
                else:
                    height = aligned
    actual_ratio = width / height
    actual_mp = (width * height) / 1_000_000.0
    result["width"] = width
    result["height"] = height
    result["ratio_actual"] = round(actual_ratio, 4)
    result["mp_actual"] = round(actual_mp, 4)
    drift = abs(actual_ratio - base_ratio) / base_ratio if base_ratio else 0.0
    if drift > 0.05:
        result["needs_user_choice"] = (
            f"按节点上下限夹取后实际比例 {width}:{height} 与请求比例偏差超过 5%，"
            "请确认是否接受，或改用「沿用工作流默认尺寸」"
        )
    return result


# ---------------------------------------------------------------------------
# 准备快照（有期限、不可变；提交回执冻结在记录里）
# ---------------------------------------------------------------------------


class DreamSnapshotStore:
    """``data/comfyui/dream-snapshots/<id>.json``：预检快照与提交回执。"""

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir) / "comfyui" / "dream-snapshots"

    def _path(self, snapshot_id: str) -> Path:
        if not re.fullmatch(r"ds-[0-9a-f]{8,32}", str(snapshot_id or "")):
            raise DreamSubmitError("快照 ID 不合法")
        return self._dir / f"{snapshot_id}.json"

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        snapshot_id = f"ds-{uuid.uuid4().hex[:12]}"
        record = dict(record)
        record["id"] = snapshot_id
        stamp = now_ms()
        record["created_at"] = stamp
        record["expires_at"] = stamp + DREAM_SNAPSHOT_TTL_MS
        record.setdefault("submitted", {})
        record.setdefault("notify_registered", False)
        self._sweep()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{snapshot_id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise DreamPrepareError(f"预检快照保存失败：{exc}") from None
        return record

    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        try:
            path = self._path(snapshot_id)
        except DreamSubmitError:
            return None
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("预检快照读取失败：%s（%s）", path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def update(self, snapshot_id: str, **fields: Any) -> dict[str, Any] | None:
        # 并发重放会同时写同一快照的提交回执：固定 .tmp 名在 Windows 上互相
        # PermissionError，且后写会覆盖先写的 notify_* 字段。锁 + 唯一临时名
        # 保证「读-改-写」原子、回执不丢（§7.2/§8 不重复生成的凭据）。
        with _SNAPSHOT_WRITE_LOCK:
            record = self.get(snapshot_id)
            if record is None:
                return None
            allowed = {"submitted", "notify_registered", "notify_wait_id"}
            for key, value in fields.items():
                if key in allowed:
                    record[key] = value
            path = self._dir / f"{record['id']}.json"
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
            try:
                tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                logger.warning("预检快照更新失败：%s（%s）", path, exc)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            return record

    def _sweep(self) -> None:
        """创建新快照时顺手清理「过期且未提交」的记录；已提交回执永久保留。

        提交回执是同一快照重放不重复生成的唯一凭据（计划 §7.2）：被清掉后
        ``submit()`` 会报「快照不存在」要求重新预检，而新预检生成新的
        ``request_key`` → 可能重复生成。因此按文件内容判断，``submitted``
        非空的记录绝不清理；读不出来的文件也一律保留，避免误删回执。
        """
        try:
            cutoff = now_ms() - DREAM_SNAPSHOT_KEEP_MS
            for path in self._dir.glob("ds-*.json"):
                try:
                    if path.stat().st_mtime * 1000 >= cutoff:
                        continue
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict) and payload.get("submitted"):
                        continue
                    path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    continue
        except OSError:
            pass


def snapshot_expired(record: dict[str, Any], *, now: int | None = None) -> bool:
    now = now_ms() if now is None else int(now)
    return now > int(record.get("expires_at") or 0)


def _parse_target_id(target_id: str) -> tuple[str, str]:
    """解析对话域目标 ID：带类型前缀（等待登记用）或原生 ID 形态。"""
    raw = str(target_id or "").strip()
    if raw.startswith("comfy-task:"):
        return "comfy-task", raw[len("comfy-task:"):]
    if raw.startswith("comfy-batch:"):
        return "comfy-batch", raw[len("comfy-batch:"):]
    if re.fullmatch(r"cf-[0-9]{8}-[0-9a-f]{8,32}", raw):
        return "comfy-task", raw
    if re.fullmatch(r"cfb-[0-9a-f]{8,32}", raw):
        return "comfy-batch", raw
    raise DreamPrepareError(
        f"目标 ID 不合法：{raw or '（空）'}。应使用提交回执返回的任务/批次 ID"
    )


def encode_wait_target(kind: str, target_id: str) -> str:
    """等待目标带类型前缀（计划 §7.5：明确区分普通 Job、原生任务与批次）。"""
    raw = str(target_id or "").strip()
    return raw if raw.startswith(f"{kind}:") else f"{kind}:{raw}"


# ---------------------------------------------------------------------------
# DreamService：工具层（与未来的总管 Adapter）共用的服务面
# ---------------------------------------------------------------------------


class DreamService:
    """对话生成域服务面：目录 / 详情 / 预检 / 提交 / 状态 / 取消。

    只读路径（目录/详情）不触发联网重扫、不落盘元数据；预检在 prepare 时把
    提交体完整展开并冻结；提交走 ``jobs.submit`` / ``batches.create`` 既有
    校验，request_key 与 owner 由这里（宿主）生成，模型传不了。
    """

    def __init__(self, app: Any, *, store: Any = None, jobs: Any = None, batches: Any = None):
        """``store/jobs/batches`` 缺省时从 ``app.comfyui`` 取。

        ComfyUIService 在自身构造过程中创建 DreamService（``app.comfyui`` 还没挂上
        组装根），所以组装路径必须显式传组件；工具层在调用时构建（app.comfyui 已
        就绪），可只传 app。
        """
        self.app = app
        data_dir = Path(app.paths.data_dir)
        comfy = getattr(app, "comfyui", None)
        self.store = store if store is not None else comfy.store
        self.jobs = jobs if jobs is not None else comfy.jobs
        self.batches = batches if batches is not None else comfy.batches
        self.chat_configs = ChatConfigStore(data_dir)
        self.snapshots = DreamSnapshotStore(data_dir)

    # ---- 目录与详情（只读） ----

    def _fingerprint(self, workflow_id: str) -> str | None:
        entry = self.store.get_workflow(workflow_id)
        if entry is None:
            return None
        return ComfyUIStore.payload_hash(entry.get("prompt") or {})

    def list_workflows(self, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        rows = self.store.list_workflows()
        rows = sorted(rows, key=lambda item: str(item.get("name") or ""))
        total = len(rows)
        try:
            page = max(1, int(page or 1))
        except (TypeError, ValueError):
            page = 1
        page_size = 10
        start = (page - 1) * page_size
        items: list[dict[str, Any]] = []
        for row in rows[start:start + page_size]:
            workflow_id = str(row.get("id") or "")
            config = self.chat_configs.load(workflow_id)
            items.append({
                "id": workflow_id,
                "name": str(row.get("name") or ""),
                "node_count": row.get("node_count"),
                "param_count": row.get("param_count"),
                "updated_at": row.get("updated_at"),
                "workflow_fingerprint": self._fingerprint(workflow_id) or "",
                "chat_config_ready": bool(config),
            })
        return {
            "workflows": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": start + page_size < total,
        }

    def workflow_details(self, workflow_id: str, param_page: int = 1) -> dict[str, Any]:
        """只读详情：不触发懒升级重扫、不写元数据（与页面 api_workflow 的差别）。"""
        entry = self.store.get_workflow(workflow_id)
        if entry is None:
            raise LookupError("工作流不存在或已被删除")
        params = entry.get("params") if isinstance(entry.get("params"), list) else []
        fingerprint = ComfyUIStore.payload_hash(entry.get("prompt") or {})
        compact: list[dict[str, Any]] = []
        for slot in params:
            if not isinstance(slot, dict):
                continue
            links = [item for item in (slot.get("connections") or []) if isinstance(item, dict)]
            compact.append({
                "node_id": slot.get("node_id"),
                "field_name": slot.get("field_name"),
                "field_type": slot.get("field_type"),
                # 计划 §5.3：参数来源要可追溯。只给槽位级摘要，不把整张工作流图给模型。
                "class_type": slot.get("class_type") or "",
                "node_title": slot.get("node_title") or "",
                "role": slot.get("role") or "",
                "label": slot.get("label") or "",
                "default": None if slot.get("field_type") == "seed" else slot.get("default"),
                "control": slot.get("control") or "",
                "candidates": list(slot.get("candidates") or [])[:20],
                "media_optional": slot.get("media_optional"),
                "connections": [
                    {
                        "target_node_id": str(link.get("target_node_id") or ""),
                        "target_class_type": str(link.get("target_class_type") or ""),
                        "target_field": str(link.get("target_field") or ""),
                    }
                    for link in links[:6]
                ],
                "connection_total": len(links),
            })
        try:
            page = max(1, int(param_page or 1))
        except (TypeError, ValueError):
            page = 1
        page_size = 60
        start = (page - 1) * page_size
        config = self.chat_configs.load(workflow_id)
        stale_problems = check_bindings_against_params(
            (config or {}).get("bindings") or [], params,
        ) if config else []
        stale = int(entry.get("params_version") or 1) != PARAMS_SCHEMA_VERSION
        return {
            "id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or ""),
            "workflow_fingerprint": fingerprint,
            "params_version": entry.get("params_version"),
            "params_version_current": PARAMS_SCHEMA_VERSION,
            "params_stale": stale,
            "param_total": len(compact),
            "param_page": page,
            "param_page_size": page_size,
            "params": compact[start:start + page_size],
            "chat_config": config,
            "chat_config_problems": stale_problems,
            "note": (
                "参数清单是导入时扫描的快照；这份工作流的参数识别规则已升级，"
                "请在造梦机页面打开一次工作流详情以刷新参数清单，再重新预检"
            ) if stale else "",
        }

    # ---- 预检 ----

    @staticmethod
    def _binding_map(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        return {
            str(binding.get("id") or ""): binding
            for binding in (config or {}).get("bindings") or [] if isinstance(binding, dict)
        }

    @staticmethod
    def _slot_map(params: list[Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (str(slot.get("node_id") or ""), str(slot.get("field_name") or "")): slot
            for slot in params or [] if isinstance(slot, dict)
        }

    def _schema_limits(
        self, width_slot: dict[str, Any], height_slot: dict[str, Any],
    ) -> dict[str, dict[str, float]] | None:
        """宽高槽位的实时声明上下限；ComfyUI 离线或声明缺失时返回 None（标注未核）。"""
        from naiba.comfyui import client
        from naiba.comfyui.schema import NodeSchemaProvider

        base = client.normalize_base_url(self.app.config.data.get("comfyui_url"))
        if client.reachable(base, timeout=3) is None:
            return None
        provider = NodeSchemaProvider(base)
        limits: dict[str, dict[str, float]] = {}
        for name, slot in (("width", width_slot), ("height", height_slot)):
            try:
                declarations = provider.node_inputs(str(slot.get("class_type") or ""))
            except Exception:  # noqa: BLE001 - 拿不到声明按未核处理
                return None
            decl = (declarations or {}).get(str(slot.get("field_name") or ""))
            if not isinstance(decl, dict) or decl.get("kind") != "number":
                return None
            low, high = decl.get("min"), decl.get("max")
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
                return None
            bound = {"min": float(low), "max": float(high)}
            # step 来自节点实时声明，缺声明时留空——绝不在代码里硬编码 8 的倍数。
            step = decl.get("step")
            if isinstance(step, (int, float)) and step > 0:
                bound["step"] = float(step)
            limits[name] = bound
        return limits

    def prepare(
        self,
        *,
        ownership: dict[str, Any],
        workflow_id: str,
        mode: str,
        values: dict[str, Any] | None = None,
        items: list[dict[str, Any]] | None = None,
        shots: int = 1,
        title: str = "",
        unload_after: bool = False,
        notify: bool = True,
    ) -> dict[str, Any]:
        """无副作用预检：解析绑定 → 计算尺寸/展开提交体 → 冻结快照。

        ``values`` 是「绑定 ID → 用户值」的映射（模型只传绑定 ID 与值，不传
        节点地址）；``items`` 仅批量模式用（逐条 ``values`` 与 shots）。
        """
        entry = self.store.get_workflow(str(workflow_id or ""))
        if entry is None:
            raise LookupError("工作流不存在或已被删除")
        fingerprint = ComfyUIStore.payload_hash(entry.get("prompt") or {})
        params = entry.get("params") if isinstance(entry.get("params"), list) else []
        slot_map = self._slot_map(params)
        config = self.chat_configs.load(str(entry.get("id") or ""))
        bindings = self._binding_map(config)
        mode = str(mode or "single").strip()
        if mode not in ("single", "batch"):
            raise DreamPrepareError("mode 只能是 single 或 batch")
        if mode == "batch" and not isinstance(items, list):
            raise DreamPrepareError("批量模式必须提供 items 数组（每个条目带自己的 values）")
        if mode == "single" and items:
            raise DreamPrepareError("单次模式不接受 items；同一提示词多张请用 shots")
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise DreamPrepareError("values 必须是「绑定 ID → 值」的对象")
        problems = check_bindings_against_params((config or {}).get("bindings") or [], params)
        missing = list(problems)

        # 配置失效检查：指纹对不上时绑定全部失效，必须重新指认。
        if config and str(config.get("workflow_fingerprint") or "") != fingerprint:
            raise DreamPrepareError(
                "对话参数配置与当前工作流指纹不一致（工作流已被修改）："
                "请在造梦机工作流页重新保存「对话参数」后再预检"
            )

        try:
            shots_value = max(1, min(int(shots if shots else 1), 200))
        except (TypeError, ValueError):
            raise DreamPrepareError("shots 必须是正整数") from None

        used_bindings: list[str] = []
        collect_missing: list[str] = []
        for binding in (config or {}).get("bindings") or []:
            if not isinstance(binding, dict):
                continue
            if str(binding.get("ask") or "") != "always":
                continue
            if str(binding.get("id")) in {str(key) for key in values}:
                continue
            purpose = str(binding.get("purpose") or "")
            if purpose not in DREAM_COLLECT_PURPOSES:
                continue
            if mode == "batch" and (purpose == "positive_prompt" or bool(binding.get("per_item"))):
                # 批量里逐条变化的绑定由条目 values 回答，共享层不算缺失。
                continue
            collect_missing.append(str(binding.get("display_name") or binding.get("id")))
        if collect_missing:
            missing.append(
                "以下必答项还没有给值：" + "、".join(collect_missing)
                + "；用户明确说「沿用默认」时，把这些绑定的值填 {\"keep_default\": true} 再预检"
            )
        if not bindings:
            missing.append(
                "这份工作流还没有「对话参数」配置：提示词、尺寸等参数需要先在"
                "造梦机工作流页的「对话参数」弹窗里指认，对话才能填写"
            )

        shared_media_inputs: list[dict[str, Any]] = []
        shared_media_skip: list[dict[str, Any]] = []
        size_summary: dict[str, Any] | None = None
        single_overrides: list[dict[str, Any]] = []
        for binding_id, raw_value in values.items():
            binding = bindings.get(str(binding_id))
            if binding is None:
                available = "、".join(sorted(bindings)) or "（还没有任何绑定）"
                raise DreamPrepareError(
                    f"绑定 {binding_id} 不存在。可用绑定：{available}；"
                    "请在造梦机工作流页打开「对话参数」完成指认"
                )
            used_bindings.append(str(binding_id))
            purpose = str(binding.get("purpose") or "")
            slots = binding.get("slots") or []
            if isinstance(raw_value, dict) and raw_value.get("keep_default"):
                # 用户明确选择「沿用工作流默认」：记录进 used_bindings，不产生任何覆盖。
                continue
            if purpose == "size":
                size_summary = self._apply_size_binding(slots, raw_value, slot_map, missing)
                continue
            resolved, resolved_skip = self._apply_value_binding(
                purpose, slots, raw_value, slot_map, missing, ownership,
            )
            single_overrides.extend(resolved)
            shared_media_inputs.extend(resolved_skip[0])
            shared_media_skip.extend(resolved_skip[1])

        total_executions = 0
        batch_body: dict[str, Any] = {}
        single_body: dict[str, Any] = {}
        items_summary: list[dict[str, Any]] = []
        if mode == "single":
            total_executions = shots_value
            # 尺寸绑定在单次模式同样落成 overrides（换算结果冻结进提交体）。
            if size_summary:
                if size_summary.get("native_slot"):
                    single_overrides.append(size_summary["native_slot"])
                elif size_summary.get("width") is not None:
                    size_binding = next(
                        (b for b in bindings.values() if str(b.get("purpose")) == "size"), None,
                    )
                    slots = (size_binding or {}).get("slots") or []
                    if len(slots) == 2:
                        single_overrides.append({
                            "node_id": slots[0]["node_id"], "field_name": slots[0]["field_name"],
                            "value": size_summary["width"],
                        })
                        single_overrides.append({
                            "node_id": slots[1]["node_id"], "field_name": slots[1]["field_name"],
                            "value": size_summary["height"],
                        })
            single_body = {
                "workflow_id": str(entry.get("id") or ""),
                "overrides": single_overrides,
                "media_inputs": shared_media_inputs,
                "media_skip": shared_media_skip,
                "shots": shots_value,
                "unload_after": bool(unload_after),
                "label": f"对话生成 · {str(entry.get('name') or '')}"[:80],
            }
        else:
            clean_items = self._clean_batch_items(items)
            total_executions = sum(int(item["shots"]) for item in clean_items)
            batch_body = self._expand_batch_body(
                entry, bindings, values, clean_items,
                slot_map, missing, ownership,
                title=title, unload_after=unload_after,
            )
            items_summary = [
                {"name": item["name"], "shots": item["shots"]} for item in clean_items
            ]

        record = {
            "ownership": dict(ownership or {}),
            "workflow_id": str(entry.get("id") or ""),
            "workflow_name": str(entry.get("name") or ""),
            "workflow_fingerprint": fingerprint,
            "mode": mode,
            "single": single_body,
            "batch": batch_body,
            "size_summary": size_summary,
            "used_bindings": used_bindings,
            "missing": missing,
            "notify": bool(notify),
            # request_key 在预检时由宿主生成并冻结：超时重试落回同一条任务。
            "request_key": f"dream-{uuid.uuid4().hex[:24]}",
            "summary": {
                "mode": mode,
                "workflow": str(entry.get("name") or ""),
                "shots": shots_value if mode == "single" else None,
                "items": items_summary,
                "total_executions": total_executions,
                "size": size_summary,
                "overrides": len(single_overrides),
                "media_inputs": len(shared_media_inputs) if mode == "single" else None,
                "missing": missing,
            },
        }
        return self.snapshots.create(record)

    @staticmethod
    def _clean_batch_items(items: list[Any]) -> list[dict[str, Any]]:
        if not items:
            raise DreamPrepareError("批量模式至少要有一个条目")
        if len(items) > DREAM_MAX_BATCH_ITEMS:
            raise DreamPrepareError(f"批量条目最多 {DREAM_MAX_BATCH_ITEMS} 个，请拆成多批")
        cleaned: list[dict[str, Any]] = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise DreamPrepareError(f"第 {index} 个条目格式必须是对象")
            values = item.get("values") if isinstance(item.get("values"), dict) else {}
            try:
                item_shots = max(1, min(int(item.get("shots") or 1), 200))
            except (TypeError, ValueError):
                raise DreamPrepareError(f"第 {index} 个条目的 shots 必须是正整数") from None
            cleaned.append({
                "name": str(item.get("name") or f"条目 {index}")[:60],
                "shots": item_shots,
                "values": dict(values),
            })
        return cleaned

    def _apply_size_binding(
        self,
        slots: list[dict[str, Any]],
        raw_value: Any,
        slot_map: dict[tuple[str, str], dict[str, Any]],
        missing: list[str],
    ) -> dict[str, Any] | None:
        """尺寸绑定：双数字槽按比例/MP 换算；单槽是原生比例/MP 控件，值原样透传。"""
        if not slots:
            missing.append("尺寸绑定没有指认槽位")
            return None
        if len(slots) == 1:
            slot = slots[0]
            meta = slot_map.get((str(slot.get("node_id")), str(slot.get("field_name"))))
            if meta is None:
                missing.append(f"尺寸槽位 {slot.get('node_id')} · {slot.get('field_name')} 不在当前参数清单里")
                return None
            return {
                "native_slot": {"node_id": slot["node_id"], "field_name": slot["field_name"], "value": raw_value},
                "note": "原生比例/MP 槽：值原样写入，实际尺寸以工作流执行为准",
            }
        width_meta = slot_map.get((str(slots[0].get("node_id")), str(slots[0].get("field_name"))))
        height_meta = slot_map.get((str(slots[1].get("node_id")), str(slots[1].get("field_name"))))
        if width_meta is None or height_meta is None:
            missing.append("宽高槽位不在当前参数清单里（工作流可能已更新），请重新保存对话参数")
            return None
        limits = self._schema_limits(width_meta, height_meta)
        resolved = resolve_size(raw_value if isinstance(raw_value, dict) else {}, width_meta, height_meta, limits)
        if resolved.get("error"):
            missing.append(f"尺寸无法确定：{resolved['error']}")
            return None
        if resolved.get("needs_user_choice"):
            missing.append(resolved["needs_user_choice"])
        return resolved

    def _apply_value_binding(
        self,
        purpose: str,
        slots: list[dict[str, Any]],
        raw_value: Any,
        slot_map: dict[tuple[str, str], dict[str, Any]],
        missing: list[str],
        ownership: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        """非尺寸绑定 → (overrides, (media_inputs, media_skip))。"""
        overrides: list[dict[str, Any]] = []
        media_inputs: list[dict[str, Any]] = []
        media_skips: list[dict[str, Any]] = []
        if purpose == "media":
            per_slot = raw_value if isinstance(raw_value, list) else [raw_value] * len(slots)
            if len(per_slot) != len(slots):
                missing.append(
                    f"媒体绑定给了 {len(per_slot)} 个素材值，但指认了 {len(slots)} 个槽位；"
                    "请逐槽给值或只给一个共享值"
                )
                return [], (media_inputs, media_skips)
            for slot, value in zip(slots, per_slot):
                meta = slot_map.get((str(slot.get("node_id")), str(slot.get("field_name"))))
                if meta is None:
                    missing.append(f"媒体槽 {slot.get('node_id')} · {slot.get('field_name')} 不在当前参数清单里")
                    continue
                kind = str(meta.get("media") or meta.get("field_type") or "")
                if isinstance(value, dict) and value.get("skip"):
                    media_skips.append({"node_id": slot["node_id"], "field_name": slot["field_name"]})
                    continue
                name = str((value or {}).get("attachment") if isinstance(value, dict) else value or "").strip()
                if not name:
                    missing.append(
                        f"媒体槽「{slot.get('name') or slot.get('field_name')}」还没有给参考素材"
                        "（值传 {\"attachment\": \"<本会话附件文件名>\"}，明确不要参考图时传 {\"skip\": true}）"
                    )
                    continue
                path = self._find_session_attachment(name, kind, ownership)
                if path is None:
                    missing.append(
                        f"附件「{name}」不在本会话已上传的{kind}素材里；请先把文件发给对话再引用"
                    )
                    continue
                media_inputs.append({
                    "node_id": slot["node_id"], "field_name": slot["field_name"],
                    "path": path, "media": kind,
                })
            return [], (media_inputs, media_skips)
        for slot in slots:
            meta = slot_map.get((str(slot.get("node_id")), str(slot.get("field_name"))))
            if meta is None:
                missing.append(f"槽位 {slot.get('node_id')} · {slot.get('field_name')} 不在当前参数清单里")
                continue
            if purpose == "seed" and (raw_value is None or str(raw_value).strip() == ""):
                continue  # 空 seed = 随机（沿用造梦机语义），不留覆盖
            overrides.append({"node_id": slot["node_id"], "field_name": slot["field_name"], "value": raw_value})
        return overrides, (media_inputs, media_skips)

    def _find_session_attachment(
        self, name: str, kind: str, ownership: dict[str, Any] | None = None,
    ) -> str | None:
        """在本会话消息附件里按文件名查找受管上传文件。

        只认 ``data/uploads`` 受管树内的路径（附件记录由上传管线落库），
        类型按扩展名与槽位一致才返回；模型传不了任意本机路径。
        """
        from naiba.core.media_types import media_kind_of
        from naiba.storage.media import is_uploads_path

        conversation_id = str((ownership or {}).get("conversation_id") or "")
        if not conversation_id:
            return None
        try:
            conversation = self.app.storage.get_conversation(
                conversation_id, include_trace=False, include_messages=True,
            )
        except TypeError:
            conversation = self.app.storage.get_conversation(conversation_id)
        messages = (conversation or {}).get("messages") if isinstance(conversation, dict) else None
        data_dir = Path(self.app.paths.data_dir)
        wanted = name.lower()
        for message in reversed(messages or []):
            for attachment in (message or {}).get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                candidate = str(attachment.get("path") or attachment.get("name") or "")
                if not candidate or Path(candidate).name.lower() != wanted:
                    continue
                target = Path(candidate).expanduser()
                if not is_uploads_path(data_dir, target) or not target.is_file():
                    continue
                if media_kind_of(str(target)) != kind:
                    continue
                try:
                    return str(target.resolve())
                except OSError:
                    continue
        return None

    def _expand_batch_body(
        self,
        entry: dict[str, Any],
        bindings: dict[str, dict[str, Any]],
        shared_values: dict[str, Any],
        items: list[dict[str, Any]],
        slot_map: dict[tuple[str, str], dict[str, Any]],
        missing: list[str],
        ownership: dict[str, Any] | None = None,
        *,
        title: str = "",
        unload_after: bool = False,
    ) -> dict[str, Any]:
        """把批量预检展开成既有 ``batches.create`` 契约的提交体。

        提示词绑定作为 ``mapping.prompt_slot`` 逐条变化；其他绑定分两层——
        共享值进 ``overrides``，逐条值进 ``extras``（键 = "node_id|field_name"，
        与批量契约一致）。媒体引用进每条的 ``media_inputs``。
        """
        errors: list[str] = []
        prompt_binding = next(
            (b for b in bindings.values() if str(b.get("purpose")) == "positive_prompt"), None,
        )
        if prompt_binding is None:
            errors.append(
                "批量模式需要「正向提示词」绑定（逐条提示词写入指认的提示词槽位）；"
                "请在对话参数配置里指认正向提示词"
            )
            prompt_slots = []
        else:
            prompt_slots = prompt_binding.get("slots") or []
        mapping: dict[str, Any] = {
            "prompt_slot": (
                {"node_id": prompt_slots[0]["node_id"], "field_name": prompt_slots[0]["field_name"]}
                if prompt_slots else None
            ),
            "duration_slot": None,
            "resolution_slot": None,
            "extra_points": [],
            "media_ports": [
                {"node_id": slot["node_id"], "field_name": slot["field_name"], "checked": True}
                for slot in self._media_slots(entry)
            ],
        }
        shared_overrides: list[dict[str, Any]] = []
        shared_media: list[dict[str, Any]] = []
        shared_skip: list[dict[str, Any]] = []
        for binding_id, raw_value in shared_values.items():
            binding = bindings.get(str(binding_id))
            if binding is None:
                continue  # 未知绑定已在上方统一报错（missing 由 prepare 汇总）
            purpose = str(binding.get("purpose") or "")
            if purpose == "size":
                continue  # 批量共享尺寸进 overrides（单槽尺寸除外，见下）
            resolved, (media_rows, skip_rows) = self._apply_value_binding(
                purpose, binding.get("slots") or [], raw_value, slot_map, missing, ownership,
            )
            shared_overrides.extend(resolved)
            shared_media.extend(media_rows)
            shared_skip.extend(skip_rows)
        # 尺寸共享值：双槽尺寸 → overrides；单槽原生尺寸 → overrides（值原样）。
        size_binding = next(
            (b for b in bindings.values() if str(b.get("purpose")) == "size"), None,
        )
        if size_binding is not None and str(size_binding.get("id")) in shared_values:
            raw_value = shared_values[str(size_binding.get("id"))]
            size_summary = self._apply_size_binding(
                size_binding.get("slots") or [], raw_value, slot_map, missing,
            )
            if size_summary:
                if size_summary.get("native_slot"):
                    shared_overrides.append(size_summary["native_slot"])
                elif size_summary.get("width") is not None:
                    slots = size_binding.get("slots") or []
                    if len(slots) == 2:
                        shared_overrides.append({
                            "node_id": slots[0]["node_id"], "field_name": slots[0]["field_name"],
                            "value": size_summary["width"],
                        })
                        shared_overrides.append({
                            "node_id": slots[1]["node_id"], "field_name": slots[1]["field_name"],
                            "value": size_summary["height"],
                        })
        batch_items: list[dict[str, Any]] = []
        for item in items:
            extras: dict[str, Any] = {}
            item_prompt = ""
            item_media: list[dict[str, Any]] = []
            item_skip: list[dict[str, Any]] = []
            for binding_id, raw_value in (item.get("values") or {}).items():
                binding = bindings.get(str(binding_id))
                if binding is None:
                    errors.append(f"条目「{item.get('name')}」引用了不存在的绑定 {binding_id}")
                    continue
                purpose = str(binding.get("purpose") or "")
                slots = binding.get("slots") or []
                if purpose == "positive_prompt":
                    if not prompt_slots:
                        continue
                    item_prompt = str(raw_value or "")
                elif purpose == "size":
                    errors.append(
                        f"条目「{item.get('name')}」不能逐条改尺寸：尺寸是整批固定参数，"
                        "逐条尺寸请拆成多批或在造梦机批量页编辑"
                    )
                    continue
                else:
                    resolved, (media_rows, skip_rows) = self._apply_value_binding(
                        purpose, slots, raw_value, slot_map, missing, ownership,
                    )
                    for row in resolved:
                        extras[f"{row['node_id']}|{row['field_name']}"] = row["value"]
                    item_media.extend(media_rows)
                    item_skip.extend(skip_rows)
            batch_items.append({
                "name": item["name"],
                "prompt": item_prompt,
                "extras": extras,
                "shots": int(item["shots"]),
                "media_inputs": [*item_media, *shared_media],
                "media_skip": [*item_skip, *shared_skip],
            })
        # 结构性错误整体失败（不产生快照）；待确认项（缺素材、尺寸待选）随快照
        # 返回，让模型看到条目数与缺失内容再追问用户。
        if errors:
            raise DreamPrepareError("；".join(errors[:5]))
        return {
            "title": (str(title or "").strip() or f"对话批量 · {entry.get('name') or ''}")[:120],
            "unload_after": bool(unload_after),
            "schemes": [{
                "workflow_id": str(entry.get("id") or ""),
                "mapping": mapping,
                "overrides": shared_overrides,
                "items": batch_items,
            }],
        }

    @staticmethod
    def _media_slots(entry: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for slot in entry.get("params") or []:
            if isinstance(slot, dict) and (
                str(slot.get("control") or "") == "media"
                or str(slot.get("field_type") or "") in _MEDIA_TYPES
            ):
                rows.append({"node_id": slot.get("node_id"), "field_name": slot.get("field_name")})
        return rows

    # ---- 提交 ----

    def submit(self, snapshot_id: str) -> dict[str, Any]:
        """按预检快照创建原生任务/批次；幂等，返回提交回执。"""
        record = self.snapshots.get(str(snapshot_id or ""))
        if record is None:
            raise DreamSubmitError("快照不存在：请重新调用预检（dream_prepare）")
        submitted = record.get("submitted") if isinstance(record.get("submitted"), dict) else {}
        if submitted:
            # 已提交：幂等重放返回原回执，不重复创建。
            return {"snapshot_id": record["id"], "already_submitted": True, **submitted}
        if snapshot_expired(record):
            raise DreamSubmitError(
                "预检快照已过期（30 分钟）：工作流或素材可能已变化，请重新预检后再提交"
            )
        missing = record.get("missing") if isinstance(record.get("missing"), list) else []
        if missing:
            raise DreamSubmitError(
                "预检还有未解决的缺失项：" + "；".join(str(item) for item in missing[:3])
                + "。请先向用户确认/补齐后重新预检，不要带着缺失项提交"
            )
        entry = self.store.get_workflow(str(record.get("workflow_id") or ""))
        if entry is None:
            raise DreamSubmitError("工作流已被删除：请重新预检")
        fingerprint = ComfyUIStore.payload_hash(entry.get("prompt") or {})
        if fingerprint != str(record.get("workflow_fingerprint") or ""):
            raise DreamSubmitError("工作流内容已变化（指纹不一致）：请重新预检后再提交")
        ownership = record.get("ownership") if isinstance(record.get("ownership"), dict) else {}
        owner_id = self.ownership_owner_id(ownership)
        mode = str(record.get("mode") or "single")
        request_key = str(record.get("request_key") or "")
        try:
            if mode == "batch":
                batch = self.batches.create(
                    dict(record.get("batch") or {}),
                    request_key=request_key, owner_id=owner_id,
                )
                receipt = {"kind": "comfy-batch", "batch_id": str(batch.get("id") or "")}
            else:
                task = self.jobs.submit(
                    dict(record.get("single") or {}),
                    request_key=request_key, owner_id=owner_id,
                    label=str((record.get("single") or {}).get("label") or ""),
                )
                receipt = {
                    "kind": "comfy-task", "task_id": str(task.get("id") or ""),
                    "task_status": str(task.get("status") or ""),
                }
        except (ValueError, ComfyUIStoreError, OSError) as exc:
            raise DreamSubmitError(f"提交失败：{exc}") from None
        self.snapshots.update(record["id"], submitted=receipt)
        return {"snapshot_id": record["id"], "already_submitted": False, **receipt}

    # ---- 状态 / 取消 ----

    @staticmethod
    def ownership_owner_id(ownership: dict[str, Any]) -> str:
        return str((ownership or {}).get("owner_id") or (ownership or {}).get("conversation_id") or "")

    def _check_task_ownership(self, task_id: str, ownership: dict[str, Any]) -> dict[str, Any]:
        task = self.jobs.task(task_id)
        if task is None:
            raise LookupError("任务不存在或已被删除")
        record = self.store.get_task(task_id) or {}
        owner_id = self.ownership_owner_id(ownership)
        # 只有经由对话提交（owner_id 已记录）且归属本会话的任务才可读/取消；
        # 页面提交的任务没有归属记录，不扩大到造梦机全库。
        task_owner = str(record.get("owner_id") or "")
        if not task_owner or (owner_id and task_owner != owner_id):
            raise PermissionError("这条任务不是当前对话发起的，不能通过对话工具查看或取消")
        return task

    def _check_batch_ownership(self, batch_id: str, ownership: dict[str, Any]) -> dict[str, Any]:
        record = self.batches.get(batch_id)
        if record is None:
            raise LookupError("批次不存在或已被删除")
        owner_id = self.ownership_owner_id(ownership)
        batch_owner = str(record.get("owner_id") or "")
        if not batch_owner or (owner_id and batch_owner != owner_id):
            raise PermissionError("这个批次不是当前对话发起的，不能通过对话工具查看或取消")
        return record

    def task_status(self, target_ids: Any, ownership: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(target_ids, list) or not target_ids:
            raise DreamPrepareError("target_ids 必须是非空数组")
        if len(target_ids) > 20:
            raise DreamPrepareError("一次最多查询 20 个目标")
        rows: list[dict[str, Any]] = []
        for target_id in target_ids:
            kind, real_id = _parse_target_id(str(target_id))
            try:
                if kind == "comfy-batch":
                    rows.append({
                        "id": str(target_id), "kind": kind,
                        "batch": self._check_batch_ownership(real_id, ownership),
                    })
                else:
                    rows.append({
                        "id": str(target_id), "kind": kind,
                        "task": self._check_task_ownership(real_id, ownership),
                    })
            except (LookupError, PermissionError) as exc:
                rows.append({"id": str(target_id), "kind": kind, "error": str(exc)})
        return {"targets": rows}

    def cancel(self, target_id: str, ownership: dict[str, Any]) -> dict[str, Any]:
        kind, real_id = _parse_target_id(str(target_id or ""))
        if kind == "comfy-batch":
            batch = self._check_batch_ownership(real_id, ownership)
            cancelled = 0
            skipped: list[str] = []
            for item in batch.get("items") or []:
                task_id = str((item or {}).get("task_id") or "")
                if not task_id:
                    continue
                try:
                    task = self._check_task_ownership(task_id, ownership)
                except (LookupError, PermissionError) as exc:
                    skipped.append(f"{task_id}：{exc}")
                    continue
                if str(task.get("status") or "") not in ("queued", "running"):
                    skipped.append(f"{task_id}：已结束（{task.get('status')}）")
                    continue
                if self.jobs.cancel(task_id):
                    cancelled += 1
                else:
                    skipped.append(f"{task_id}：无法取消（可能刚结束）")
            return {"id": str(target_id), "kind": kind, "cancelled": cancelled, "skipped": skipped}
        task = self._check_task_ownership(real_id, ownership)
        if str(task.get("status") or "") not in ("queued", "running"):
            raise DreamSubmitError(f"任务已结束（{task.get('status')}），无需取消")
        if not self.jobs.cancel(real_id):
            raise DreamSubmitError("任务无法取消（可能刚好已结束），请稍后查询状态")
        return {"id": str(target_id), "kind": kind, "cancelled": 1}
