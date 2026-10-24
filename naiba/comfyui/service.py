"""造梦机域门面：``app.comfyui.*`` 的调用面，返回 ``(payload, HTTPStatus)``。

与打标器门面同一形态：HTTP 层只解析请求与转发；API 格式校验、参数扫描、
取消语义、产物落点都在这里与下层模块。所有落盘路径由服务端决定。
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

from naiba.comfyui import client
from naiba.comfyui.jobs import ComfyUIJobManager, ComfyUITaskError
from naiba.comfyui.params import scan_params
from naiba.comfyui.runner import ComfyUIRunner
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
        """缓存清理的引用保护（与 Studio/打标器同一口径）。

        本期造梦机不消费 uploads（图片输入是 P5，接入后这里返回任务引用的
        上传图集合）；先接入保护入口，方向是「多留不误删」。
        """
        return set()

    # ---- 连接 ----

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

    # ---- 工作流库 ----

    def api_workflows(self) -> tuple[dict[str, Any], int]:
        rows = self.store.list_workflows()
        return {"workflows": rows, "count": len(rows)}, 200

    def api_workflow(self, workflow_id: str) -> tuple[dict[str, Any], int]:
        entry = self.store.get_workflow(workflow_id)
        if entry is None:
            return {"error": "工作流不存在或已被删除"}, 404
        # 懒升级：主提示词识别（role 字段）是后加的，旧导入的工作流详情时重扫一次
        # 并落盘；幂等，没有 role 字段才触发。
        params = entry.get("params") or []
        if params and not any("role" in slot for slot in params if isinstance(slot, dict)):
            prompt = entry.get("prompt") if isinstance(entry.get("prompt"), dict) else {}
            entry["params"] = scan_params(prompt)
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
            "params": scan_params(prompt),
            "created_at": stamp,
            "updated_at": stamp,
        }
        self.store.save_workflow(entry)
        return {"ok": True, "workflow": entry}, 201

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
        try:
            result = self.jobs.delete_tasks(body if isinstance(body, dict) else {})
        except (OSError, ComfyUIStoreError) as exc:
            return {"error": f"删除任务失败：{exc}"}, 500
        return {"ok": True, **result}, 200

    # ---- 产物文件 ----

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
