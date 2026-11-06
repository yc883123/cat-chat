"""造梦机批量生成：批次记录 + 逐条目提交编排（2026-11-05 拍板方案 A）。

每条目 = 一次现有 ``jobs.submit``——校验、上传、轮询、续跑全是既有路径，
执行主循环零改动。本模块只做三件事：

- 批次记录落盘：``data/comfyui/batches/<id>.json``，原子写与任务存储同款
  （临时文件 + os.replace + WinError 5 短重试）；
- 提交编排：探活 → 全量干跑校验 → 逐条目创建（先全部建好、再统一入队，
  事务式：任一失败删掉已建任务，整单报错，不留半批）；
- 列表 / 详情：逐条目 join 任务状态与产物计数，供跑批视图轮询。

零猜测铁律：服务端不扫描、不派生、不猜时长。条目值（正文 / 时长 / 分辨率）
写进哪个槽位，由前端把用户在映射区指认的槽位地址随请求带来
（``mapping.prompt_slot`` / ``duration_slot`` / ``resolution_slot``）；
没有映射却填了值的条目整单拒绝——填了不生效比报错更伤用户。

「＋增加指认」（2026-11-06 拍板）：第二段提示词 / 第二时长 / 第二分辨率 /
备用槽是可选的扩展写入位（``mapping.extra_points``），条目里的补充值以
``item.extras = {"节点|字段": 值}`` 随条目带来；值只会写进前端指认出的槽位，
喂给没被指认的参数同样整单拒绝。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from naiba.comfyui import COMFYUI_BATCH_PREFIX, MAX_SHOTS
from naiba.comfyui import client
from naiba.comfyui.jobs import ComfyUITaskError, _running_step
from naiba.comfyui.store import _store_lock, now_ms

# 批次历史上限（跑批视图 + 历史下拉够用；更早的批次靠任务记录本身留存）。
MAX_BATCHES = 50
# 方案数与总条目数上限（总条目对齐单任务 shots ≤ 200 的口径）。
MAX_SCHEMES = 50
# 「＋增加指认」扩展写入位上限（第二段/第二时长/第二分辨率 + 备用槽）。
MAX_EXTRA_POINTS = 8


class ComfyUIBatchError(ValueError):
    """批量提交非法（参数 / 校验 / 探活）。"""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写批次记录（照 ComfyUIStore._write_json 同款，含 WinError 5 短重试）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _frozen(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _slot_of(raw: Any) -> dict[str, str] | None:
    """映射槽位地址：只收 {node_id, field_name}，形状不对按未映射处理。"""
    if not isinstance(raw, dict):
        return None
    node_id = str(raw.get("node_id") or "").strip()
    field_name = str(raw.get("field_name") or "").strip()
    if not node_id or not field_name:
        return None
    return {"node_id": node_id, "field_name": field_name}


def _override_value(overrides: list[dict[str, Any]], slot: dict[str, str] | None,
                    default: Any) -> Any:
    """从已校验的 overrides 里取一个槽位的值；没写这个槽位就返回默认值。"""
    if not slot:
        return default
    for row in overrides:
        if (str(row.get("node_id") or "") == slot["node_id"]
                and str(row.get("field_name") or "") == slot["field_name"]):
            return row.get("value")
    return default


def _extra_points(entry: dict[str, Any], raw: Any) -> list[dict[str, Any]]:
    """「＋增加指认」扩展写入位：第二段提示词 / 第二时长 / 第二分辨率 / 备用槽。

    返回形如 ``[{"id", "kind", "spec", "slot"}]``；形状不对、槽位对不上工作流、
    类型不匹配或同一槽位重复指认都整单拒绝——指认错位比悄悄丢弃更伤用户。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ComfyUIBatchError("「＋增加指认」的内容格式必须是列表")
    if len(raw) > MAX_EXTRA_POINTS:
        raise ComfyUIBatchError(f"指认行最多 {MAX_EXTRA_POINTS} 个")
    kinds = {"prompt", "duration", "resolution", "spare"}
    params = {(str(row.get("node_id") or ""), str(row.get("field_name") or "")): row
              for row in entry.get("params") or []}
    points: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    seen_kinds: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            raise ComfyUIBatchError("每一行指认都必须是对象")
        kind = str(row.get("kind") or "").strip()
        slot = _slot_of(row.get("slot"))
        if kind not in kinds:
            raise ComfyUIBatchError(f"未知的指认类型：{kind or '（空）'}")
        if slot is None:
            raise ComfyUIBatchError("指认行还没有选定写进哪个参数")
        key = (slot["node_id"], slot["field_name"])
        param = params.get(key)
        if param is None:
            raise ComfyUIBatchError(
                f"指认行指向的参数（节点 {slot['node_id']} · {slot['field_name']}）"
                f"不在这份工作流里，请重新指认")
        field_type = str(param.get("field_type") or "")
        control = str(param.get("control") or "")
        if control == "media" or field_type in ("image", "video", "audio"):
            raise ComfyUIBatchError("素材口是媒体槽：请在「素材口」区勾选启用，不能指认成写入位")
        if key in seen:
            raise ComfyUIBatchError(
                f"同一个参数（{slot['node_id']} · {slot['field_name']}）被指认了两行，"
                f"两行会互相覆盖；请删掉重复的那行")
        spec = "string"
        if kind == "prompt":
            if kind in seen_kinds:
                raise ComfyUIBatchError("第二段提示词的指认行只能有一行")
            if field_type != "text":
                raise ComfyUIBatchError("第二段提示词只能指认文本参数")
            spec = "text"
        elif kind == "duration":
            if kind in seen_kinds:
                raise ComfyUIBatchError("第二时长的指认行只能有一行")
            if field_type not in ("number", "seed"):
                raise ComfyUIBatchError("第二时长只能指认数字参数")
            spec = "number"
        elif kind == "resolution":
            if kind in seen_kinds:
                raise ComfyUIBatchError("第二分辨率的指认行只能有一行")
            if field_type != "number" and not (field_type == "text" and (param.get("candidates") or [])):
                raise ComfyUIBatchError("第二分辨率只能指认数字参数或带候选值的文本参数")
            spec = "text"
        else:  # spare：任何可改参数都行，值按该槽位的类型来。
            if field_type == "bool":
                spec = "bool"
            elif field_type in ("number", "seed"):
                spec = "number"
        seen.add(key)
        seen_kinds.add(kind)
        points.append({
            "id": str(row.get("id") or "").strip()[:64],
            "kind": kind,
            "spec": spec,
            "slot": dict(slot),
        })
    return points


def _extra_value(point: dict[str, Any], raw: Any, *, where: str) -> Any:
    """按指认类型换算补充值；空值返回 None（不写入）。只做直译，不再猜测。"""
    label = f"{point['slot']['node_id']} · {point['slot']['field_name']}"
    spec = point["spec"]
    if isinstance(raw, str):
        raw = raw.strip()
    if raw is None or raw == "" or (isinstance(raw, str) and not raw):
        return None
    if spec == "number":
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ComfyUIBatchError(f"{where}「{label}」要填数字，现在填的是「{raw}」") from None
        return int(number) if number.is_integer() else number
    if spec == "bool":
        text = str(raw).strip().lower()
        if text in ("true", "1", "on", "开"):
            return True
        if text in ("false", "0", "off", "关"):
            return False
        raise ComfyUIBatchError(f"{where}「{label}」只能填开/关，现在填的是「{raw}」")
    text = str(raw)
    if spec == "text" and len(text) > 20000:
        raise ComfyUIBatchError(f"{where}「{label}」的正文太长（超过 20000 字）")
    return text


class ComfyUIBatchManager:
    """线程安全（锁内写 JSON）；提交编排复用 jobs 的既有校验与幂等。"""

    def __init__(self, app: Any, store: Any, jobs: Any):
        self.app = app
        self.store = store
        self.jobs = jobs
        self._dir = Path(app.paths.data_dir) / "comfyui" / "batches"
        self._lock = _store_lock(self._dir)

    # ---- 路径 ----

    def batch_path(self, batch_id: str) -> Path:
        return self._dir / f"{batch_id}.json"

    def schemes_path(self, batch_id: str) -> Path:
        # 可编辑快照单独落盘：批次列表每 5 秒读一遍主记录，不能把整批正文
        # 跟着一起解析；只有「编辑这一批」才会读这个文件。
        return self._dir / f"{batch_id}.schemes.json"

    @staticmethod
    def new_batch_id() -> str:
        return f"{COMFYUI_BATCH_PREFIX}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _sanitize(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict) or not str(payload.get("id") or "").startswith(
                f"{COMFYUI_BATCH_PREFIX}-"):
            return None
        payload.setdefault("title", "")
        payload.setdefault("unload_after", False)
        payload.setdefault("items", [])
        return payload

    # ---- 创建 ----

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        title = str(body.get("title") or "").strip()[:120] or "未命名批次"
        unload_after = bool(body.get("unload_after"))
        schemes = body.get("schemes")
        if not isinstance(schemes, list) or not schemes:
            raise ComfyUIBatchError("批次至少要有一个方案")
        if len(schemes) > MAX_SCHEMES:
            raise ComfyUIBatchError(f"方案最多 {MAX_SCHEMES} 个，请拆成多批")

        base = client.normalize_base_url(self.app.config.data.get("comfyui_url"))
        if client.reachable(base, timeout=5) is None:
            raise ComfyUIBatchError(
                "ComfyUI 服务不可达：请确认它已启动、地址正确（设置里的 ComfyUI 地址）"
            )

        entries: dict[str, dict[str, Any]] = {}
        payloads: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        # request_key 前缀就是批次 id（cfb-<batch_id>-<条目序号>），重放同一条
        # 目永远落到同一个 request_key 上；批次 id 先行生成不影响任何状态。
        batch_id = self.new_batch_id()
        for scheme_index, scheme in enumerate(schemes, 1):
            expanded, snapshot = self._expand_scheme(
                scheme_index, scheme, entries, offset=len(payloads),
                batch_id=batch_id, title=title,
            )
            payloads.extend(expanded)
            snapshots.append(snapshot)
        if not payloads:
            raise ComfyUIBatchError("批次里没有任何条目")

        items: list[dict[str, Any]] = []
        try:
            for payload in payloads:
                # hold：先全部建好再统一入队——回滚时任务还没进 worker，
                # 直接删记录即可，不会出现「边跑边删」的竞态。
                task = self.jobs.submit(
                    {
                        "workflow_id": payload["workflow_id"],
                        "overrides": payload["overrides"],
                        "media_inputs": payload["media_inputs"],
                        "shots": payload["shots"],
                        "unload_after": unload_after,
                    },
                    request_key=payload["request_key"],
                    label=payload["label"],
                    batch_id=batch_id,
                    hold=True,
                )
                task_id = str(task.get("id") or "")
                items.append({
                    "index": payload["index"],
                    "name": payload["name"],
                    "workflow_id": payload["workflow_id"],
                    "workflow_name": payload["workflow_name"],
                    "task_id": task_id,
                    "label": payload["label"],
                    "request_key": payload["request_key"],
                })
        except Exception as exc:  # noqa: BLE001 - 回滚后把原始原因报给用户
            # 回滚按批次 id 扫任务（request_key 前缀），不依赖内存列表——
            # 「任务已落盘但调用方没拿到返回值」的窗口同样要清干净。
            self._rollback(batch_id)
            raise ComfyUIBatchError(f"批量提交失败，整批已撤销：{exc}") from None

        record = {
            "id": batch_id,
            "title": title,
            "created_at": now_ms(),
            "unload_after": unload_after,
            "items": items,
        }
        try:
            with self._lock:
                # 先写主记录再写快照：万一只落了主记录，批次仍可见、可删；
                # 快照缺失时详情会退回任务输入兜底。
                _write_json(self.batch_path(batch_id), record)
                if snapshots:
                    _write_json(self.schemes_path(batch_id),
                                {"id": batch_id, "schemes": snapshots})
        except Exception:
            # 记录没落盘就不能留下「有任务、没批次」的半批：任务仍是 hold 状态，直接清掉。
            self._rollback(batch_id)
            for path in (self.batch_path(batch_id), self.schemes_path(batch_id)):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        # 全部建好才统一入队：入队本身不失败（线程投递），不再回滚。
        for item in items:
            self.jobs.runner.submit(str(item["task_id"]))
        return record

    def _expand_scheme(self, scheme_index: int, scheme: Any, entries: dict[str, Any],
                       *, offset: int, batch_id: str, title: str,
                       ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """把一个方案展开成逐条目的提交载荷；只套地址与校验，不做任何派生。

        ``offset`` 是前面方案已展开的条目数：条目编号与总数限制都按整批累计。
        同时返回一份可编辑快照（映射 / 固定参数 / 条目字段），供「编辑这一批」
        原样回填；快照只服务回填，不参与执行。
        """
        if not isinstance(scheme, dict):
            raise ComfyUIBatchError(f"第 {scheme_index} 个方案格式必须是对象")
        workflow_id = str(scheme.get("workflow_id") or "").strip()
        if not workflow_id:
            raise ComfyUIBatchError(f"第 {scheme_index} 个方案还没有选择工作流")
        entry = entries.get(workflow_id)
        if entry is None:
            entry = self.store.get_workflow(workflow_id)
            if entry is None:
                raise ComfyUIBatchError(f"第 {scheme_index} 个方案的工作流不存在或已被删除")
            entries[workflow_id] = entry
        workflow_name = str(entry.get("name") or "")
        mapping = scheme.get("mapping") if isinstance(scheme.get("mapping"), dict) else {}
        prompt_slot = _slot_of(mapping.get("prompt_slot"))
        duration_slot = _slot_of(mapping.get("duration_slot"))
        resolution_slot = _slot_of(mapping.get("resolution_slot"))
        extra_points = _extra_points(entry, mapping.get("extra_points"))
        primary_keys = {f"{slot['node_id']}|{slot['field_name']}"
                        for slot in (prompt_slot, duration_slot, resolution_slot) if slot}
        for point in extra_points:
            key = f"{point['slot']['node_id']}|{point['slot']['field_name']}"
            if key in primary_keys:
                raise ComfyUIBatchError(
                    f"指认行「{point['kind']}」和主映射选中了同一个参数（{key}），"
                    f"会互相覆盖；两个写入位别指向同一段")
        scheme_overrides = scheme.get("overrides") if isinstance(scheme.get("overrides"), list) else []
        items = scheme.get("items")
        if not isinstance(items, list) or not items:
            raise ComfyUIBatchError(f"第 {scheme_index} 个方案至少要有一条条目")
        if len(items) + offset > MAX_SHOTS:
            raise ComfyUIBatchError(f"整批条目总数不能超过 {MAX_SHOTS} 条")

        out: list[dict[str, Any]] = []
        snapshot_items: list[dict[str, Any]] = []
        for item_index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise ComfyUIBatchError(f"方案 {scheme_index} 的第 {item_index} 条条目格式必须是对象")
            name = str(item.get("name") or "").strip()[:120] or f"条目 {item_index}"
            index = offset + item_index
            overrides = _frozen(scheme_overrides)
            prompt = str(item.get("prompt") or "").strip()
            if prompt and not prompt_slot:
                raise ComfyUIBatchError(
                    f"方案 {scheme_index} 的映射区没有指认正文写进哪个参数，"
                    f"但条目「{name}」填了正文；请指认正文写入位或清空正文"
                )
            if prompt and prompt_slot:
                overrides.append({**prompt_slot, "value": prompt})
            duration = item.get("duration_seconds")
            if duration is not None and str(duration).strip() != "":
                if not duration_slot:
                    raise ComfyUIBatchError(
                        f"方案 {scheme_index} 的映射区没有指认时长写进哪个参数，"
                        f"但条目「{name}」填了时长；请指认时长写入位或清空时长"
                    )
                overrides.append({**duration_slot, "value": duration})
            resolution = str(item.get("resolution") or "").strip()
            if resolution and not resolution_slot:
                raise ComfyUIBatchError(
                    f"方案 {scheme_index} 的映射区没有指认分辨率写进哪个参数，"
                    f"但条目「{name}」填了分辨率；请指认分辨率写入位或清空分辨率"
                )
            if resolution and resolution_slot:
                overrides.append({**resolution_slot, "value": resolution})
            # 「＋增加指认」的补充值：只写进前端指认出的槽位，喂给没被指认的
            # 参数整单拒绝（与主映射同一口径）。
            extras_raw = item.get("extras")
            extras_map = extras_raw if isinstance(extras_raw, dict) else {}
            known_keys = {f"{point['slot']['node_id']}|{point['slot']['field_name']}"
                          for point in extra_points}
            for key in extras_map:
                if key not in known_keys:
                    raise ComfyUIBatchError(
                        f"条目「{name}」给未指认的参数（{key}）填了补充值；"
                        f"请在映射区点「＋增加指认」把这一参数指认出来，或清掉这个值"
                    )
            for point in extra_points:
                key = f"{point['slot']['node_id']}|{point['slot']['field_name']}"
                value = _extra_value(point, extras_map.get(key), where=f"条目「{name}」")
                if value is None:
                    continue
                overrides.append({**point["slot"], "value": value})
            media_inputs = item.get("media_inputs")
            if media_inputs is not None and not isinstance(media_inputs, list):
                raise ComfyUIBatchError(f"条目「{name}」的素材列表格式不对")
            try:
                shots = max(1, min(int(item.get("shots", 1) or 1), MAX_SHOTS))
            except (TypeError, ValueError):
                raise ComfyUIBatchError(f"条目「{name}」的条数必须是正整数") from None
            # 干跑：与单任务提交同一套校验（槽位白名单 + 类型保真 + 受管媒体引用）。
            try:
                overrides = self.jobs._validate_overrides(entry, overrides)
                media_inputs = self.jobs._validate_media_inputs(entry, media_inputs)
            except ComfyUITaskError as exc:
                raise ComfyUIBatchError(str(exc)) from None
            out.append({
                "index": index,
                "name": name,
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "overrides": overrides,
                "media_inputs": media_inputs,
                "shots": shots,
                "request_key": f"{batch_id}-{index}",
                "label": f"{title} · {name}",
            })
            snapshot_items.append({
                "name": name,
                # 剧本只服务回填（下次还能接着「转换」），不参与执行。
                "script": str(item.get("script") or "").strip()[:20000],
                "prompt": _override_value(overrides, prompt_slot, ""),
                "duration_seconds": _override_value(overrides, duration_slot, None),
                "resolution": _override_value(overrides, resolution_slot, ""),
                # 指认位的补充值原样记下来，「编辑这一批」回填同一个表的对应列。
                "extras": {
                    f"{point['slot']['node_id']}|{point['slot']['field_name']}":
                        _override_value(overrides, point["slot"], None)
                    for point in extra_points
                    if _override_value(overrides, point["slot"], None) is not None
                },
                "shots": shots,
                "media_inputs": _frozen(media_inputs or []),
            })
        snapshot = {
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "overrides": _frozen(scheme_overrides),
            "mapping": {
                "prompt_slot": self._valid_slot(entry, prompt_slot),
                "duration_slot": self._valid_slot(entry, duration_slot),
                "resolution_slot": self._valid_slot(entry, resolution_slot),
                "extra_points": [
                    {key: point[key] for key in ("id", "kind", "spec", "slot")}
                    for point in extra_points
                ],
                "media_ports": self._media_ports(entry, mapping.get("media_ports")),
            },
            "items": snapshot_items,
        }
        return out, snapshot

    @staticmethod
    def _valid_slot(entry: dict[str, Any], slot: dict[str, str] | None) -> dict[str, str] | None:
        """快照里的映射只保留这份工作流真有的槽位；失效地址不复活。"""
        if not slot:
            return None
        for row in entry.get("params") or []:
            if (str(row.get("node_id") or "") == slot["node_id"]
                    and str(row.get("field_name") or "") == slot["field_name"]):
                return dict(slot)
        return None

    @staticmethod
    def _media_ports(entry: dict[str, Any], raw: Any) -> list[dict[str, Any]]:
        """素材口快照：只收这份工作流里的媒体槽，保留勾选与顺序。"""
        if not isinstance(raw, list):
            return []
        allowed = {
            (str(row.get("node_id") or ""), str(row.get("field_name") or ""))
            for row in entry.get("params") or []
            if str(row.get("control") or "") == "media"
            or str(row.get("field_type") or "") in ("image", "video", "audio")
        }
        ports: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for port in raw[:64]:
            slot = _slot_of(port)
            if slot is None:
                continue
            key = (slot["node_id"], slot["field_name"])
            if key not in allowed or key in seen:
                continue
            seen.add(key)
            ports.append({**slot, "checked": bool(port.get("checked"))})
        return ports

    def _rollback(self, batch_id: str) -> None:
        """事务回滚：删掉本批次已建的全部任务记录（hold 创建的任务未入队）。"""
        prefix = f"{batch_id}-"
        for task in self.store.list_tasks(limit=500):
            if not str(task.get("request_key") or "").startswith(prefix):
                continue
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            if str(task.get("status") or "") in {"queued", "running"}:
                self.store.update_task(task_id, status="cancelled",
                                       detail="批量提交失败，整批已撤销", finished_at=now_ms())
            self.store.delete_task(task_id)

    # ---- 查询 ----

    def list(self, limit: int = MAX_BATCHES) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self._dir.exists():
            return rows
        paths = sorted(
            (path for path in self._dir.glob(f"{COMFYUI_BATCH_PREFIX}-*.json")
             if not path.name.endswith(".schemes.json")),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            record = self._sanitize(payload)
            if record is None:
                continue
            rows.append(self._with_task_state(record, slim=True))
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def _read_record(self, batch_id: str) -> dict[str, Any] | None:
        batch_id = str(batch_id or "").strip()
        if not batch_id or "/" in batch_id or "\\" in batch_id or not batch_id.startswith(
                f"{COMFYUI_BATCH_PREFIX}-"):
            return None
        try:
            payload = json.loads(self.batch_path(batch_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return self._sanitize(payload)

    def _read_schemes(self, batch_id: str, record: dict[str, Any]) -> list[dict[str, Any]]:
        # 过渡兼容：早期实现把快照写在主记录里；新记录一律走独立的 schemes 文件。
        inline = record.get("schemes")
        if isinstance(inline, list) and inline:
            return inline
        try:
            payload = json.loads(self.schemes_path(batch_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        schemes = payload.get("schemes") if isinstance(payload, dict) else None
        return schemes if isinstance(schemes, list) else []

    def get(self, batch_id: str, *, with_schemes: bool = False) -> dict[str, Any] | None:
        record = self._read_record(batch_id)
        if record is None:
            return None
        if not with_schemes:
            # 快照只在「编辑这一批」时取；跑批轮询不反复传正文。
            return self._with_task_state(record)
        schemes = self._read_schemes(batch_id, record)
        detail = self._with_task_state(record, with_restore=not schemes)
        detail["schemes"] = schemes
        return detail

    def _task_ids(self, record: dict[str, Any]) -> list[str]:
        return [str(item.get("task_id") or "") for item in record.get("items") or []
                if isinstance(item, dict) and str(item.get("task_id") or "")]

    def clear_outputs(self, batch_id: str) -> dict[str, Any] | None:
        """删掉这一批的任务记录与产物文件，批次记录保留（还能回填再跑）。"""
        record = self._read_record(batch_id)
        if record is None:
            return None
        ids = self._task_ids(record)
        if not ids:
            return {"deleted": 0, "skipped": [], "failed": []}
        result = self.jobs.delete_tasks({"task_ids": ids, "cancel_active": True})
        failed = result.get("failed") or []
        if failed:
            first = failed[0]
            raise ComfyUIBatchError(
                f"有任务没能删掉，批次先保留：{first.get('reason') or first.get('id') or ''}"
            )
        return result

    def delete(self, batch_id: str, *, delete_outputs: bool = True) -> dict[str, Any] | None:
        """删掉整个批次；默认连任务记录与产物文件一起删。"""
        record = self._read_record(batch_id)
        if record is None:
            return None
        result = {"deleted": 0, "skipped": [], "failed": []}
        ids = self._task_ids(record)
        if delete_outputs and ids:
            result = self.jobs.delete_tasks({"task_ids": ids, "cancel_active": True})
            failed = result.get("failed") or []
            if failed:
                first = failed[0]
                raise ComfyUIBatchError(
                    f"有任务没能删掉，批次先保留：{first.get('reason') or first.get('id') or ''}"
                )
        with self._lock:
            self.batch_path(batch_id).unlink(missing_ok=True)
            try:
                self.schemes_path(batch_id).unlink(missing_ok=True)
            except OSError:
                # 主记录已删：快照文件清不掉也不影响列表（list 会跳过它）。
                pass
        return result

    def _with_task_state(self, record: dict[str, Any], *, slim: bool = False,
                         with_restore: bool = False) -> dict[str, Any]:
        """逐条目 join 任务状态：跑批视图与历史下拉共用一份形状。

        ``with_restore`` 只给「编辑这一批」用：老批次没有快照时，把任务输入
        （overrides / media_inputs / shots）作为 ``restore`` 兜底。
        """
        items = []
        for item in record.get("items") or []:
            row = dict(item) if isinstance(item, dict) else {}
            task = self.store.get_task(str(row.get("task_id") or ""))
            if task is None:
                # 用户删过产物（或任务记录损坏）：卡片显示「产物已删除」，
                # 不再给一个必然 404 的「重试」按钮。
                row.update({"status": "missing", "done": 0, "total": 0, "failed": 0,
                            "outputs": [], "error": "任务记录和产物已删除"})
            else:
                checkpoint = task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}
                result = task.get("result") if isinstance(task.get("result"), dict) else {}
                outputs = [str(name) for name in (result.get("outputs") or []) if str(name or "")]
                input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
                row.update({
                    "status": task.get("status"),
                    "done": len(checkpoint.get("completed") or []),
                    "total": int(input_data.get("shots") or 0),
                    "failed": len(checkpoint.get("errors") or []),
                    "outputs": outputs,
                    "error": task.get("error") or "",
                    # 当前正在生成的条目带采样步级进度（其余条目 None，不渲染）。
                    "step": _running_step(task, checkpoint),
                })
                if with_restore and not slim:
                    row["restore"] = {
                        "workflow_id": str(input_data.get("workflow_id")
                                           or row.get("workflow_id") or ""),
                        "workflow_name": str(input_data.get("workflow_name")
                                             or row.get("workflow_name") or ""),
                        "overrides": input_data.get("overrides") or [],
                        "media_inputs": input_data.get("media_inputs") or [],
                        "shots": int(input_data.get("shots") or 1),
                    }
            if slim:
                row.pop("outputs", None)
                row.pop("restore", None)
            items.append(row)
        detail = {**record, "items": items}
        detail.pop("schemes", None)
        if slim:
            done = sum(1 for row in items if row.get("status") == "completed")
            detail["done_count"] = done
            detail["item_count"] = len(items)
        return detail
