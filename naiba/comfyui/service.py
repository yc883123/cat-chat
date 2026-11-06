"""造梦机域门面：``app.comfyui.*`` 的调用面，返回 ``(payload, HTTPStatus)``。

与打标器门面同一形态：HTTP 层只解析请求与转发；API 格式校验、参数扫描、
取消语义、产物落点都在这里与下层模块。所有落盘路径由服务端决定。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

from naiba.comfyui import (
    COMFYUI_ACTIVE_STATUSES,
    COMFYUI_RESUMABLE_STATUSES,
    PARAMS_SCHEMA_VERSION,
)
from naiba.comfyui import client
from naiba.comfyui.batches import ComfyUIBatchError, ComfyUIBatchManager
from naiba.comfyui.jobs import ComfyUIJobManager, ComfyUITaskError
from naiba.comfyui.params import enrich_slots, scan_params
from naiba.comfyui.runner import ComfyUIRunner
from naiba.comfyui.schema import NodeSchemaProvider
from naiba.comfyui.store import ComfyUIStore, ComfyUIStoreError

# 工作流 JSON 上限（对齐聊天域 _load_comfyui_workflow 的 20 MB 文件上限）。
WORKFLOW_MAX_BYTES = 20 * 1024 * 1024

# UI 格式报错文案（用户拍板，白话完整句）：告诉用户怎么导出 API 格式。
UI_FORMAT_ERROR = (
    "这是 ComfyUI 的界面格式（编辑器文件），这里用不了。请在 ComfyUI 里打开这份工作流，"
    "用「导出（API）」另存一份 JSON 再导入：老版本界面先在设置里打开「开发者模式」，"
    "顶栏会出现「导出（API）」按钮；新版本界面在工作流菜单里直接有「导出（API）」。"
)


def normalize_api_workflow(value: Any) -> dict[str, Any]:
    """API 格式校验（与聊天域工具同规则）：UI 格式带导出引导报错。"""
    if not isinstance(value, dict):
        raise ValueError("工作流 JSON 必须是对象")
    candidate = value.get("prompt") if isinstance(value.get("prompt"), dict) else value
    if isinstance(candidate.get("nodes"), list) or isinstance(candidate.get("links"), list):
        raise ValueError(UI_FORMAT_ERROR)
    if not candidate:
        raise ValueError("工作流为空")
    invalid = [key for key, node in candidate.items() if not isinstance(node, dict)]
    if invalid:
        raise ValueError(f"API 工作流节点值必须是对象：{', '.join(map(str, invalid[:5]))}")
    return candidate


def _open_folder(target: Path) -> None:
    """用系统文件管理器打开目录（单拆出来便于测试打桩，与打标器同实现）。"""
    if sys.platform.startswith("win"):
        os.startfile(str(target))  # type: ignore[attr-defined]  # 仅 Windows 有
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


class ComfyUIService:
    def __init__(self, app: Any):
        self.app = app
        data_dir = Path(app.paths.data_dir)
        self.store = ComfyUIStore(data_dir)
        self.runner = ComfyUIRunner(app, self.store)
        self.jobs = ComfyUIJobManager(app, self.store, self.runner)
        self.batches = ComfyUIBatchManager(app, self.store, self.jobs)

    # ---- 生命周期 ----

    def recover(self) -> int:
        return self.jobs.recover_interrupted()

    def shutdown(self) -> None:
        self.runner.shutdown()

    def referenced_upload_paths(self) -> set[str]:
        """缓存清理的引用保护（计划 §5.3）：任务仍引用的输入媒体文件。

        覆盖排队/执行中（活动）与可重试（interrupted/failed）任务——重试要从
        快照引用重新上传，文件被清掉重试就断了。completed/cancelled 不保护：
        资产回收沿用通用 uploads 清理，不另造生命周期。
        """
        from naiba.core.paths import normalized_path_key

        protected = (*COMFYUI_ACTIVE_STATUSES, *COMFYUI_RESUMABLE_STATUSES)
        keys: set[str] = set()
        for task in self.store.list_tasks(limit=5000):
            if str(task.get("status") or "") not in protected:
                continue
            input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
            for ref in input_data.get("media_inputs") or []:
                if not isinstance(ref, dict):
                    continue
                key = normalized_path_key(Path(str(ref.get("path") or "")).expanduser())
                if key:
                    keys.add(key)
        return keys

    # ---- 连接 ----

    def _scan_params_current(self, prompt: dict[str, Any]) -> list[dict[str, Any]]:
        """按当前识别规则扫描 + 节点声明增强（计划 §3.1）。

        增强只在导入 / schema 刷新时访问 /object_info（按节点类缓存）；ComfyUI
        离线时退回纯规则扫描，不阻断工作流读取。
        """
        slots = scan_params(prompt)
        base = client.normalize_base_url(self.app.config.data.get("comfyui_url"))
        if client.reachable(base, timeout=3) is None:
            return slots
        try:
            return enrich_slots(slots, NodeSchemaProvider(base))
        except Exception:  # noqa: BLE001 - 增强失败不阻断导入/详情
            return slots

    @staticmethod
    def _unconfirmed_media_slots(params: list[Any]) -> list[dict[str, Any]]:
        """还没查清「未选能否跳过」的媒体槽（升级时 ComfyUI 离线的那批）。"""
        pending: list[dict[str, Any]] = []
        for slot in params:
            if not isinstance(slot, dict):
                continue
            if str(slot.get("field_type") or "") not in ("image", "audio", "video"):
                continue
            if slot.get("media_optional") is None:
                pending.append(slot)
        return pending

    def _refresh_unconfirmed_media(self, entry: dict[str, Any]) -> bool:
        """只补媒体槽的可选性元数据；不重扫参数、不动用户配置。

        返回是否至少有一个槽从「无法确认」变成了明确结论。若 ComfyUI 仍离线或
        目标节点声明缺失，返回 False 且不落盘，下次打开详情再试。
        """
        params = entry.get("params")
        if not isinstance(params, list):
            return False
        pending = self._unconfirmed_media_slots(params)
        if not pending:
            return False
        base = client.normalize_base_url(self.app.config.data.get("comfyui_url"))
        if client.reachable(base, timeout=3) is None:
            return False
        try:
            enrich_slots(pending, NodeSchemaProvider(base))
        except Exception:  # noqa: BLE001 - 补元数据失败不阻断详情
            return False
        return any(slot.get("media_optional") is not None for slot in pending)

    def api_health(self) -> tuple[dict[str, Any], int]:
        base = client.normalize_base_url(self.app.config.data.get("comfyui_url"))
        # 交互探测用短超时：可达时 /system_stats 毫秒级返回；连不上（尤其被
        # 防火墙/代理静默丢包的地址）最多等 3 秒，不让按钮干等 10 秒。
        stats = client.reachable(base, timeout=3)
        if stats is None:
            return {
                "ok": False,
                "url": base,
                "error": "连不上 ComfyUI：请确认它已启动，地址是否是设置里的 ComfyUI 地址",
            }, 200
        return {"ok": True, "url": base, **stats}, 200

    def api_set_url(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """保存 ComfyUI 地址（造梦机页的「改地址」入口）并立即测试连接。

        只接受 http/https 地址；格式非法原样拒绝。写进 config 的
        ``comfyui_url`` 键（聊天域 ComfyUI 工具读同一个键）。正在执行的任务
        不受影响——runner 在任务开始时已取好当时的地址。
        """
        raw = str((body or {}).get("url") or "").strip()
        if not raw:
            return {"error": "请填写 ComfyUI 地址，例如 http://127.0.0.1:8188"}, 400
        if "://" not in raw:
            raw = f"http://{raw}"
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return {"error": "地址格式不对：要像 http://127.0.0.1:8188 这样"}, 400
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        try:
            self.app.config.data["comfyui_url"] = normalized
            self.app.config.save()
        except (OSError, ValueError) as exc:
            return {"error": f"地址保存失败：{exc}"}, 500
        stats = client.reachable(normalized, timeout=3)
        if stats is None:
            return {
                "ok": True,
                "url": normalized,
                "reachable": False,
                "error": "地址已保存，但现在连不上 ComfyUI：请确认它已启动",
            }, 200
        return {"ok": True, "url": normalized, "reachable": True, **stats}, 200

    def api_models(self) -> tuple[dict[str, Any], int]:
        """模型候选（只读辅助选择）：checkpoint / LoRA 之外再带 unet / clip /
        vae / controlnet 各自的目录清单。

        各类候选来自对应标准加载器在 /object_info 里的枚举；ComfyUI 不可达或
        解析失败时返回空候选加说明——候选只是辅助，不阻断工作流编辑和提交
        （用户仍可手动输入模型名）。任一类非空即算 ok。
        """
        base = client.normalize_base_url(self.app.config.data.get("comfyui_url"))
        checkpoints = client.checkpoint_names(base)
        loras = client.lora_names(base)
        unets = client.unet_names(base)
        clips = client.clip_names(base)
        vaes = client.vae_names(base)
        controlnets = client.controlnet_names(base)
        total = sum(len(group) for group in (checkpoints, loras, unets, clips, vaes, controlnets))
        if total:
            return {
                "ok": True,
                "models": checkpoints,
                "loras": loras,
                "unets": unets,
                "clips": clips,
                "vaes": vaes,
                "controlnets": controlnets,
                "count": total,
            }, 200
        return {
            "ok": False,
            "models": [],
            "loras": [],
            "unets": [],
            "clips": [],
            "vaes": [],
            "controlnets": [],
            "count": 0,
            "error": "暂时拿不到 ComfyUI 的模型列表：请确认 ComfyUI 已启动、地址正确。仍可以手动输入模型名。",
        }, 200

    # ---- 工作流库 ----

    def api_workflows(self) -> tuple[dict[str, Any], int]:
        rows = self.store.list_workflows()
        return {"workflows": rows, "count": len(rows)}, 200

    def api_workflow(self, workflow_id: str) -> tuple[dict[str, Any], int]:
        entry = self.store.get_workflow(workflow_id)
        if entry is None:
            return {"error": "工作流不存在或已被删除"}, 404
        # 懒升级：参数识别规则升级后（PARAMS_SCHEMA_VERSION 递增），旧导入的
        # 工作流详情时重扫一次并落盘；幂等，版本一致才跳过。
        # v6 另外补一个只针对媒体槽可选性的重试：升级时 ComfyUI 离线的工作流，
        # 下次打开详情时只补 media_optional，不重扫、不动用户参数配置。
        if int(entry.get("params_version") or 1) != PARAMS_SCHEMA_VERSION:
            prompt = entry.get("prompt") if isinstance(entry.get("prompt"), dict) else {}
            entry["params"] = self._scan_params_current(prompt)
            entry["params_version"] = PARAMS_SCHEMA_VERSION
            self.store.save_workflow(entry)
        elif self._refresh_unconfirmed_media(entry):
            self.store.save_workflow(entry)
        entry["workflow_fingerprint"] = hashlib.sha256(json.dumps(
            entry.get("prompt") or {}, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {"workflow": entry}, 200

    def api_workflow_create(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = body if isinstance(body, dict) else {}
        name = str(body.get("name") or "").strip()[:120]
        raw = body.get("workflow")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return {"error": "工作流内容不是合法 JSON，请检查后再导入"}, 400
        try:
            prompt = normalize_api_workflow(raw)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        if not name:
            name = "未命名工作流"
        stamp = int(time.time() * 1000)
        entry = {
            "id": self.store.new_workflow_id(),
            "name": name,
            "prompt": prompt,
            "workflow_fingerprint": ComfyUIStore.payload_hash(prompt),
            "params": self._scan_params_current(prompt),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": stamp,
            "updated_at": stamp,
        }
        self.store.save_workflow(entry)
        return {"ok": True, "workflow": entry}, 201

    def api_workflow_save_as(self, workflow_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """另存当前安全配置；不执行、不接受图/目标/凭据，不复用生成时的 seed 转换。"""
        if not isinstance(body, dict) or set(body) != {"name", "workflow_fingerprint", "overrides"}:
            return {"error": "另存只接受 name、workflow_fingerprint、overrides"}, 400
        name = body["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
            return {"error": "名称须为 1–120 字的普通文本"}, 400
        fingerprint = body["workflow_fingerprint"]
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            return {"error": "workflow_fingerprint 格式不正确，请重新读取工作流"}, 400
        overrides = body["overrides"]
        if not isinstance(overrides, list) or len(overrides) > 2000:
            return {"error": "overrides 必须是有界参数数组"}, 400
        entry = self.store.get_workflow(workflow_id)
        if entry is None:
            return {"error": "工作流不存在或已被删除"}, 404
        source = entry.get("prompt")
        if not isinstance(source, dict) or not source:
            return {"error": "源工作流无有效节点图"}, 400
        if ComfyUIStore.payload_hash(source) != fingerprint:
            return {"error": "工作流版本已变化，请重新载入后另存"}, 409
        prompt = copy.deepcopy(source)
        slots = {(slot["node_id"], slot["field_name"]): slot for slot in scan_params(source)}
        provider = NodeSchemaProvider(client.normalize_base_url(self.app.config.data.get("comfyui_url")))
        seen = set()
        for item in overrides:
            if not isinstance(item, dict) or set(item) != {"node_id", "field_name", "value"}:
                return {"error": "覆盖项只接受 node_id、field_name、value"}, 400
            node_id, field = item["node_id"], item["field_name"]
            if not isinstance(node_id, str) or not isinstance(field, str):
                return {"error": "node_id 和 field_name 必须是字符串"}, 400
            key = (node_id, field)
            slot = slots.get(key)
            if key in seen or slot is None or slot["field_type"] in {"image", "video", "audio"}:
                return {"error": "覆盖参数重复、不存在或属于受管媒体入口"}, 400
            seen.add(key)
            try:
                declarations = provider.node_inputs(slot["class_type"])
            except Exception:  # noqa: BLE001 - authoritative schema must fail closed
                declarations = None
            decl = (declarations or {}).get(field)
            if not isinstance(decl, dict) or decl.get("kind") == "unknown":
                return {"error": f"无法取得 {node_id} · {field} 的实时权威声明，未保存"}, 503
            value = item["value"]
            kind = decl.get("kind")
            valid = False
            if kind == "combo":
                valid = isinstance(value, str) and value in (decl.get("candidates") or [])
            elif kind == "bool":
                valid = type(value) is bool
            elif kind == "number":
                low, high = decl.get("min"), decl.get("max")
                def finite(number: Any) -> bool:
                    return type(number) in (int, float) and (type(number) is int or math.isfinite(number))
                if not finite(low) or not finite(high) or low > high:
                    return {"error": f"{node_id} · {field} 缺少可信数值范围，未保存"}, 503
                valid = finite(value) and low <= value <= high
                if decl.get("number_type") == "INT" or slot["field_type"] == "seed":
                    valid = valid and type(value) is int
                if slot["field_type"] == "seed":
                    valid = valid and value >= 0
            # STRING (even multiline), code, URLs and paths are never configuration overrides.
            if not valid:
                return {"error": f"{node_id} · {field} 不是允许保存的枚举成员或严格类型范围值"}, 400
            prompt[node_id]["inputs"][field] = value
        # Re-read before writing so a concurrent source edit/deletion cannot silently fork stale data.
        current = self.store.get_workflow(workflow_id)
        if current is None or ComfyUIStore.payload_hash(current.get("prompt") or {}) != fingerprint:
            return {"error": "工作流版本已变化，请重新载入后另存"}, 409
        stamp = int(time.time() * 1000)
        params = scan_params(prompt)
        try:
            enrich_slots(params, provider)
        except Exception:  # noqa: BLE001 - metadata only; changed values already checked above
            pass
        saved = {"id": self.store.new_workflow_id(), "name": name.strip(), "prompt": prompt,
                 "params": params, "params_version": PARAMS_SCHEMA_VERSION,
                 "created_at": stamp, "updated_at": stamp}
        for key in ("visible_params", "param_order"):
            if key in entry:
                saved[key] = copy.deepcopy(entry[key])
        saved["workflow_fingerprint"] = ComfyUIStore.payload_hash(prompt)
        try:
            if not self.store.save_workflow_as(workflow_id, fingerprint, saved):
                return {"error": "工作流版本已变化，请重新载入后另存"}, 409
        except (OSError, ComfyUIStoreError) as exc:
            return {"error": f"另存工作流失败：{exc}"}, 500
        return {"ok": True, "workflow": saved}, 201

    def api_workflow_params_set(self, workflow_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """保存这份工作流在主表单显示哪些参数、按什么顺序显示（按工作流粒度持久化）。

        ``visible`` 是 ``{node_id, field_name}`` 数组，只认扫描出的槽位；
        没列出的参数不显示也不提交覆盖值，生成时按工作流原本的值执行。
        ``order`` 可选，是「编辑参数」清单的完整顺序（含没勾选的行），用户拖出来的
        先后顺序据此保存；不传时保留原有顺序，避免旧前端把排序清掉。
        """
        entry = self.store.get_workflow(workflow_id)
        if entry is None:
            return {"error": "工作流不存在或已被删除"}, 404
        body = body if isinstance(body, dict) else {}
        raw = body.get("visible")
        if not isinstance(raw, list):
            return {"error": "visible 必须是参数数组"}, 400
        slot_keys = {(str(slot.get("node_id") or ""), str(slot.get("field_name") or ""))
                     for slot in entry.get("params") or [] if isinstance(slot, dict)}

        def _clean(items: Any, label: str) -> tuple[list[dict[str, str]], str]:
            out: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for index, item in enumerate(items, 1):
                if not isinstance(item, dict):
                    return [], f"第 {index} 条{label}格式必须是对象"
                key = (str(item.get("node_id") or ""), str(item.get("field_name") or ""))
                if key not in slot_keys:
                    return [], f"{label} {key[0]} · {key[1]} 不是这份工作流的可编辑槽位"
                if key in seen:
                    continue
                seen.add(key)
                out.append({"node_id": key[0], "field_name": key[1]})
            return out, ""

        visible, error = _clean(raw, "参数")
        if error:
            return {"error": error}, 400
        order = entry.get("param_order")
        raw_order = body.get("order")
        if raw_order is not None:
            if not isinstance(raw_order, list):
                return {"error": "order 必须是参数数组"}, 400
            order, error = _clean(raw_order, "排序")
            if error:
                return {"error": error}, 400
        entry["visible_params"] = visible
        if order is not None:
            entry["param_order"] = order
        entry["updated_at"] = int(time.time() * 1000)
        self.store.save_workflow(entry)
        return {"ok": True, "visible_params": visible, "param_order": entry.get("param_order") or []}, 200

    def api_workflow_delete(self, workflow_id: str) -> tuple[dict[str, Any], int]:
        entry = self.store.get_workflow(workflow_id)
        if entry is None:
            return {"error": "工作流不存在或已被删除"}, 404
        try:
            ok, reason = self.jobs.delete_workflow(workflow_id)
        except (OSError, ComfyUIStoreError) as exc:
            return {"error": f"删除工作流失败：{exc}"}, 500
        if not ok:
            return {"error": reason or "工作流删除被拒绝"}, 409
        return {"ok": True}, 200

    # ---- 任务 ----

    def api_tasks(self, active_only: str = "") -> tuple[dict[str, Any], int]:
        rows = self.jobs.tasks(active_only=str(active_only or "") in ("1", "true"))
        return {"tasks": rows}, 200

    def api_task(self, task_id: str) -> tuple[dict[str, Any], int]:
        task = self.jobs.task(task_id)
        if task is None:
            return {"error": "任务不存在"}, 404
        return {"task": task}, 200

    def api_task_events(self, task_id: str, after: Any = 0) -> tuple[dict[str, Any], int]:
        task = self.store.get_task(task_id)
        if task is None:
            return {"error": "任务不存在"}, 404
        try:
            parsed_after = max(0, int(str(after or "0")))
        except (TypeError, ValueError):
            return {"error": "after 必须是非负整数"}, 400
        rows = self.jobs.events(task_id, after=parsed_after)
        last_seq = int(rows[-1]["seq"]) if rows else parsed_after
        return {
            "events": rows,
            "count": len(rows),
            "after": parsed_after,
            "last_seq": max(last_seq, parsed_after),
            "status": task.get("status"),
        }, 200

    def api_submit(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        try:
            task = self.jobs.submit(body)
        except ComfyUITaskError as exc:
            return {"error": str(exc)}, 400
        except (OSError, ComfyUIStoreError) as exc:
            return {"error": f"任务创建失败：{exc}"}, 500
        detail = self.jobs.task(str(task.get("id") or "")) or task
        return {"task": detail}, 201

    def api_cancel(self, task_id: str) -> tuple[dict[str, Any], int]:
        task = self.store.get_task(task_id)
        if task is None:
            return {"error": "任务不存在"}, 404
        if str(task.get("status") or "") in {"completed", "failed", "cancelled", "interrupted"}:
            return {"error": "任务已结束，无需取消"}, 409
        if not self.jobs.cancel(task_id):
            return {"error": "任务无法取消（可能已结束）"}, 409
        return {"ok": True}, 200

    def api_retry(self, task_id: str) -> tuple[dict[str, Any], int]:
        try:
            task = self.jobs.retry(task_id)
        except LookupError:
            return {"error": "任务不存在"}, 404
        except ComfyUITaskError as exc:
            return {"error": str(exc)}, 400
        except (OSError, ComfyUIStoreError) as exc:
            return {"error": f"重试失败：{exc}"}, 500
        return {"task": self.jobs.task(str(task.get("id") or "")) or task}, 201

    def api_tasks_delete(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = body if isinstance(body, dict) else {}
        try:
            cancel_timeout = min(60.0, max(1.0, float(body.get("cancel_timeout") or 15.0)))
        except (TypeError, ValueError):
            cancel_timeout = 15.0
        try:
            result = self.jobs.delete_tasks(body, cancel_timeout=cancel_timeout)
        except (OSError, ComfyUIStoreError) as exc:
            return {"error": f"删除任务失败：{exc}"}, 500
        return {"ok": True, **result}, 200

    # ---- 批量生成 ----

    def api_batch_create(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        try:
            batch = self.batches.create(body)
        except ComfyUIBatchError as exc:
            return {"error": str(exc)}, 400
        except ComfyUITaskError as exc:
            return {"error": str(exc)}, 400
        except (OSError, ComfyUIStoreError) as exc:
            return {"error": f"批量提交失败：{exc}"}, 500
        return {"ok": True, "batch": self.batches.get(str(batch.get("id") or "")) or batch}, 201

    def api_batches(self) -> tuple[dict[str, Any], int]:
        rows = self.batches.list()
        return {"batches": rows, "count": len(rows)}, 200

    def api_batch(self, batch_id: str, *, with_schemes: bool = False) -> tuple[dict[str, Any], int]:
        batch = self.batches.get(batch_id, with_schemes=with_schemes)
        if batch is None:
            return {"error": "批次不存在或已被删除"}, 404
        return {"batch": batch}, 200

    def api_batch_delete(self, batch_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = body if isinstance(body, dict) else {}
        delete_outputs = body.get("delete_outputs")
        try:
            result = self.batches.delete(
                batch_id, delete_outputs=True if delete_outputs is None else bool(delete_outputs)
            )
        except ComfyUIBatchError as exc:
            return {"error": str(exc)}, 409
        except (OSError, ComfyUIStoreError) as exc:
            return {"error": f"删除批次失败：{exc}"}, 500
        if result is None:
            return {"error": "批次不存在或已被删除"}, 404
        return {"ok": True, **result}, 200

    def api_batch_clear_outputs(self, batch_id: str) -> tuple[dict[str, Any], int]:
        try:
            result = self.batches.clear_outputs(batch_id)
        except ComfyUIBatchError as exc:
            return {"error": str(exc)}, 409
        except (OSError, ComfyUIStoreError) as exc:
            return {"error": f"删除产物失败：{exc}"}, 500
        if result is None:
            return {"error": "批次不存在或已被删除"}, 404
        return {"ok": True, **result}, 200

    # ---- 产物文件 ----

    def api_free_models(self) -> tuple[dict[str, Any], int]:
        """请求 ComfyUI 卸载已加载模型，释放显存与内存（``POST /free``）。

        ComfyUI 不会因此打断正在执行的任务——队列空下来才真正卸载，所以
        还有任务在跑时给前端 pending 提示，文案讲清楚「稍后生效」。
        """
        base = client.normalize_base_url(self.app.config.data.get("comfyui_url"))
        if client.reachable(base, timeout=3) is None:
            return {
                "ok": False,
                "error": "连不上 ComfyUI：请确认它已启动，地址是否是设置里的 ComfyUI 地址",
            }, 200
        ok, reason = client.free_models(base)
        if not ok:
            return {"ok": False, "error": f"ComfyUI 没有响应模型释放请求：{reason}"}, 500
        pending = bool(self.store.list_tasks(active_only=True, limit=1))
        return {"ok": True, "pending": pending}, 200

    def api_reveal_outputs(self) -> tuple[dict[str, Any], int]:
        """在运行本应用的电脑上打开产物根目录（路径固定，不收客户端参数）。"""
        try:
            target = self.store.outputs_dir.resolve()
            target.mkdir(parents=True, exist_ok=True)
            _open_folder(target)
        except OSError as exc:
            return {"error": f"无法打开产物文件夹：{exc}"}, 500
        return {"ok": True, "path": str(target)}, 200

    def task_file(self, task_id: str, name: str) -> Path | None:
        """解析任务产物文件的本地路径；名字含路径分隔符或文件不存在一律 None。"""
        task = self.store.get_task(task_id)
        if task is None:
            return None
        safe = str(name or "").strip()
        if not safe or "/" in safe or "\\" in safe or safe.startswith("."):
            return None
        path = self.store.task_output_dir(task_id) / safe
        return path if path.is_file() else None
