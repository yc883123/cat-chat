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
from naiba.comfyui.jobs import ComfyUITaskError
from naiba.comfyui.store import _store_lock, now_ms

# 批次历史上限（跑批视图 + 历史下拉够用；更早的批次靠任务记录本身留存）。
MAX_BATCHES = 50
# 方案数与总条目数上限（总条目对齐单任务 shots ≤ 200 的口径）。
MAX_SCHEMES = 50


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
        # request_key 前缀就是批次 id（cfb-<batch_id>-<条目序号>），重放同一条
        # 目永远落到同一个 request_key 上；批次 id 先行生成不影响任何状态。
        batch_id = self.new_batch_id()
        for scheme_index, scheme in enumerate(schemes, 1):
            expanded = self._expand_scheme(
                scheme_index, scheme, entries, offset=len(payloads),
                batch_id=batch_id, title=title,
            )
            payloads.extend(expanded)
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
        with self._lock:
            _write_json(self.batch_path(batch_id), record)
        # 全部建好才统一入队：入队本身不失败（线程投递），不再回滚。
        for item in items:
            self.jobs.runner.submit(str(item["task_id"]))
        return record

    def _expand_scheme(self, scheme_index: int, scheme: Any, entries: dict[str, Any],
                       *, offset: int, batch_id: str, title: str) -> list[dict[str, Any]]:
        """把一个方案展开成逐条目的提交载荷；只套地址与校验，不做任何派生。

        ``offset`` 是前面方案已展开的条目数：条目编号与总数限制都按整批累计。
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
        scheme_overrides = scheme.get("overrides") if isinstance(scheme.get("overrides"), list) else []
        items = scheme.get("items")
        if not isinstance(items, list) or not items:
            raise ComfyUIBatchError(f"第 {scheme_index} 个方案至少要有一条条目")
        if len(items) + offset > MAX_SHOTS:
            raise ComfyUIBatchError(f"整批条目总数不能超过 {MAX_SHOTS} 条")

        out: list[dict[str, Any]] = []
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
        return out

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
        paths = sorted(self._dir.glob(f"{COMFYUI_BATCH_PREFIX}-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
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

    def get(self, batch_id: str) -> dict[str, Any] | None:
        batch_id = str(batch_id or "").strip()
        if not batch_id or "/" in batch_id or "\\" in batch_id or not batch_id.startswith(
                f"{COMFYUI_BATCH_PREFIX}-"):
            return None
        try:
            payload = json.loads(self.batch_path(batch_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        record = self._sanitize(payload)
        return self._with_task_state(record) if record else None

    def _with_task_state(self, record: dict[str, Any], *, slim: bool = False) -> dict[str, Any]:
        """逐条目 join 任务状态：跑批视图与历史下拉共用一份形状。"""
        items = []
        for item in record.get("items") or []:
            row = dict(item) if isinstance(item, dict) else {}
            task = self.store.get_task(str(row.get("task_id") or ""))
            if task is None:
                row.update({"status": "missing", "done": 0, "total": 0, "failed": 0,
                            "outputs": [], "error": "任务记录不存在"})
            else:
                checkpoint = task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}
                result = task.get("result") if isinstance(task.get("result"), dict) else {}
                outputs = [str(name) for name in (result.get("outputs") or []) if str(name or "")]
                row.update({
                    "status": task.get("status"),
                    "done": len(checkpoint.get("completed") or []),
                    "total": int((task.get("input") or {}).get("shots") or 0),
                    "failed": len(checkpoint.get("errors") or []),
                    "outputs": outputs,
                    "error": task.get("error") or "",
                })
            if slim:
                row.pop("outputs", None)
            items.append(row)
        slim_record = {**record, "items": items}
        if slim:
            done = sum(1 for row in items if row.get("status") == "completed")
            slim_record["done_count"] = done
            slim_record["item_count"] = len(items)
        return slim_record
