"""造梦机域门面：``app.comfyui.*`` 的调用面，返回 ``(payload, HTTPStatus)``。

与打标器门面同一形态：HTTP 层只解析请求与转发；API 格式校验、参数扫描、
取消语义、产物落点都在这里与下层模块。所有落盘路径由服务端决定。
"""

from __future__ import annotations

import json
import os
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
        """Checkpoint / LoRA 模型候选（只读辅助选择）。

        ComfyUI 不可达或解析失败时返回空候选加说明——候选只是辅助，
        不阻断工作流编辑和提交（用户仍可手动输入模型名）。
        """
        base = client.normalize_base_url(self.app.config.data.get("comfyui_url"))
        checkpoints = client.checkpoint_names(base)
        loras = client.lora_names(base)
        if checkpoints or loras:
            return {
                "ok": True,
                "models": checkpoints,
                "loras": loras,
                "count": len(checkpoints) + len(loras),
            }, 200
        return {
            "ok": False,
            "models": [],
            "loras": [],
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
            "params": self._scan_params_current(prompt),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": stamp,
            "updated_at": stamp,
        }
        self.store.save_workflow(entry)
        return {"ok": True, "workflow": entry}, 201

    def api_workflow_params_set(self, workflow_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """保存这份工作流在主表单显示哪些参数（按工作流粒度持久化）。

        ``visible`` 是 ``{node_id, field_name}`` 数组，只认扫描出的槽位；
        没列出的参数不显示也不提交覆盖值，生成时按工作流原本的值执行。
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
        visible: list[dict[str, str]] = []
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                return {"error": f"第 {index} 条参数格式必须是对象"}, 400
            key = (str(item.get("node_id") or ""), str(item.get("field_name") or ""))
            if key not in slot_keys:
                return {"error": f"参数 {key[0]} · {key[1]} 不是这份工作流的可编辑槽位"}, 400
            visible.append({"node_id": key[0], "field_name": key[1]})
        entry["visible_params"] = visible
        entry["updated_at"] = int(time.time() * 1000)
        self.store.save_workflow(entry)
        return {"ok": True, "visible_params": visible}, 200

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
