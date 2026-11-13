"""造梦机任务管理：提交 / 查询 / 取消 / 重试续跑 / 启动恢复 / 批量删除。

提交契约（服务端权威）：
- 只接受库里的工作流 id（不接受内联 prompt）——工作流库是唯一入口，
  导入时已做过 API 格式校验与参数扫描；
- overrides 只接受扫描出的槽位，值按槽位类型保真转换（转不动=400）；
- media_inputs 只接受扫描出的媒体槽 + 宿主 uploads 受管文件引用（拒绝任意
  本机路径），类型/体积/存在性在此校验（计划 §5.2/§5.3）；
- media_skip 只接受扫描出的媒体槽地址：用户手动 ⊘ 强制跳过，执行端在提交副本
  移除对应下游连接，不以 optional 声明为门槛（2026-11-09 口径）；
- shots ≤ 200（对齐聊天域批量口径）；
- 任务 JSON 保存工作流快照（prompt 副本 + 名称），删除工作流不破坏历史任务。

重试契约：终态任务（failed / cancelled / interrupted）可直接重试——保留
checkpoint（已完成段不重复提交），清掉 errors 后把任务放回队列。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from naiba.comfyui import (
    COMFYUI_ACTIVE_STATUSES,
    MEDIA_INPUT_MAX_BYTES,
    MAX_SHOTS,
)
from naiba.comfyui import stepmeter
from naiba.comfyui.store import ComfyUIStoreError, now_ms
from naiba.core.contracts import COMFYUI_SUBMISSIONS_KEY
from naiba.core.media_types import media_kind_of
from naiba.storage.media import is_uploads_path


class ComfyUITaskError(ValueError):
    """任务操作非法（参数/状态）。"""


# 再次生成只接受这些当前表单字段；工作流基底永远从原任务冻结快照读取。
RERUN_EDITABLE_FIELDS = frozenset({
    "overrides", "media_inputs", "media_skip", "shots", "unload_after",
})


def _running_step(task: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any] | None:
    """采样步级进度：只在 running 且数据新鲜时带出；其余为 None（不渲染）。"""
    if str(task.get("status") or "") != "running":
        return None
    try:
        return stepmeter.snapshot_for(checkpoint.get("prompt_ids") or [])
    except Exception:  # noqa: BLE001 - 步级提取失败只是没有条
        return None


def _format_default(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "…"


class ComfyUIJobManager:
    def __init__(self, app: Any, store: Any, runner: Any):
        self.app = app
        self.store = store
        self.runner = runner

    # ---- 启动恢复 ----

    def recover_interrupted(self) -> int:
        """启动时遗留 queued/running → interrupted；任务详情里说明可重试续跑。"""
        recovered = 0
        for task in self.store.iter_tasks(active_only=True):
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            self.runner.cancel(task_id, reason="服务重启前任务中断")
            checkpoint = task.get("checkpoint") or {}
            completed = len(checkpoint.get("completed") or [])
            shots = int((task.get("input") or {}).get("shots") or 0)
            detail = (
                "服务重启前任务中断。已完成 "
                f"{completed}/{shots} 段的产物已保存在本地；"
                "点「重试」会从没完成的段继续，不会重复提交已完成的段。"
            )
            self.store.update_task(
                task_id, status="interrupted", detail=detail,
                finished_at=now_ms(),
                progress={
                    "done": completed,
                    "total": shots,
                    "failed": len(checkpoint.get("errors") or []),
                },
            )
            try:
                self.store.append_event(task_id, {
                    "type": "error", "message": "服务重启前任务中断",
                })
            except ComfyUIStoreError:
                pass
            recovered += 1
        return recovered

    # ---- 提交 ----

    def lookup_request(self, request_key: str, *, owner_id: str = "") -> dict[str, Any] | None:
        return self.store.lookup_request(request_key, owner_id=owner_id)

    def submit(self, body: dict[str, Any], *, request_key: str = "", owner_id: str = "",
               source_task_id: str = "", label: str = "", batch_id: str = "",
               hold: bool = False, entry_override: dict[str, Any] | None = None) -> dict[str, Any]:
        """提交一个造梦机任务。

        - ``label``：任务卡显示名（批量提交传「批次名 · 条目名」；单任务不传，
          卡片回落显示工作流名）；
        - ``batch_id``：来源批次 id（批量提交传；单任务留空）。随 input 冻结，
          重试续跑沿用原任务记录，字段不丢——前端结果页按它把批量条目和单次任务分档；
        - ``hold``：只创建不入队（批量编排的事务窗口——先全部建好再统一入队，
          回滚时任务还没进 worker，直接删记录即可）；单任务提交不要传。
        - ``entry_override``：用任务冻结快照作为工作流基底（再次生成用）；不再查
          工作流库，工作流已删除也能创建新任务。仍按快照 params 做全部校验。
        """
        if not isinstance(body, dict):
            raise ComfyUITaskError("请求体必须是对象")
        if entry_override is not None:
            entry = entry_override if isinstance(entry_override, dict) else {}
            workflow_id = str(entry.get("id") or "").strip()
            if not workflow_id:
                raise ComfyUITaskError("任务快照缺少工作流标识")
        else:
            workflow_id = str(body.get("workflow_id") or "").strip()
            if not workflow_id:
                raise ComfyUITaskError("请先选择要执行的工作流")
            entry = self.store.get_workflow(workflow_id)
            if entry is None:
                raise ComfyUITaskError("工作流不存在或已被删除")
        try:
            shots = max(1, min(int(body.get("shots", 1) or 1), MAX_SHOTS))
        except (TypeError, ValueError):
            raise ComfyUITaskError("shots 必须是正整数") from None
        overrides = self._validate_overrides(entry, body.get("overrides"))
        media_inputs = self._validate_media_inputs(entry, body.get("media_inputs"))
        media_skip = self._validate_media_skips(entry, body.get("media_skip"))
        input_data = {
            "workflow_id": workflow_id,
            "workflow_name": str(entry.get("name") or ""),
            # 快照：导入时校验过的 API prompt 原样保存，任务不随后续编辑漂移。
            "prompt": entry.get("prompt") or {},
            "params": entry.get("params") or [],
            "overrides": overrides,
            "media_inputs": media_inputs,
            # 手动 ⊘ 跳过的媒体槽地址：独立于 media_inputs，重试/续跑沿用快照。
            "media_skip": media_skip,
            "shots": shots,
            # 逐次卸载偏好随载荷冻结（母本 §5.5）；任务不随后续批次设置漂移。
            "unload_after": bool(body.get("unload_after")),
            "label": str(label or ""),
            "batch_id": str(batch_id or ""),
        }
        task = self.store.create_task(
            input_data, request_key=request_key, owner_id=owner_id,
            source_task_id=source_task_id,
        )
        if not hold:
            self.runner.submit(str(task.get("id") or ""))
        return task

    # ---- 再次生成（完成态单次任务 → 新任务）----

    def _rerun_source(self, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取完成态单次任务的冻结生成快照；结构不完整一律拒绝。"""
        task = self.store.get_task(str(task_id or ""))
        if task is None:
            raise LookupError("任务不存在")
        if str(task.get("status") or "") != "completed":
            raise ComfyUITaskError("只有已完成的单次任务可以再次生成")
        input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
        if str(input_data.get("batch_id") or "").strip():
            raise ComfyUITaskError("批量条目请使用批次里的重试或编辑，不支持再次生成")
        workflow_id = str(input_data.get("workflow_id") or "").strip()
        if not workflow_id:
            raise ComfyUITaskError("任务快照缺少工作流标识，无法再次生成")
        prompt = input_data.get("prompt")
        if not isinstance(prompt, dict) or not prompt:
            raise ComfyUITaskError("任务快照缺少工作流内容，无法再次生成")
        params = input_data.get("params")
        if not isinstance(params, list):
            raise ComfyUITaskError("任务快照缺少参数清单，无法再次生成")
        overrides = input_data.get("overrides")
        if not isinstance(overrides, list):
            raise ComfyUITaskError("任务快照缺少参数覆盖清单，无法再次生成")
        media_inputs = input_data.get("media_inputs")
        if not isinstance(media_inputs, list):
            raise ComfyUITaskError("任务快照缺少媒体输入清单，无法再次生成")
        media_skip = input_data.get("media_skip") or []
        if not isinstance(media_skip, list):
            raise ComfyUITaskError("任务快照的媒体跳过清单格式不对，无法再次生成")
        for label, rows in (("参数覆盖", overrides), ("媒体输入", media_inputs),
                            ("媒体跳过", media_skip)):
            for item in rows:
                if not isinstance(item, dict) or not str(item.get("node_id") or "").strip() \
                        or not str(item.get("field_name") or "").strip():
                    raise ComfyUITaskError(f"任务快照的{label}不完整，无法再次生成")
        for item in media_inputs:
            if not str(item.get("path") or "").strip():
                raise ComfyUITaskError("任务快照的媒体输入缺少文件路径，无法再次生成")
        try:
            shots = int(input_data.get("shots") or 1)
        except (TypeError, ValueError):
            raise ComfyUITaskError("任务快照的生成段数不合法，无法再次生成") from None
        if not 1 <= shots <= MAX_SHOTS:
            raise ComfyUITaskError("任务快照的生成段数超出允许范围，无法再次生成")
        return task, {
            "workflow_id": workflow_id,
            "workflow_name": str(input_data.get("workflow_name") or ""),
            "prompt": prompt,
            "params": params,
            "overrides": overrides,
            "media_inputs": media_inputs,
            "media_skip": media_skip,
            "shots": shots,
            "unload_after": bool(input_data.get("unload_after")),
        }

    def _media_input_snapshot(self, item: dict[str, Any]) -> dict[str, Any]:
        """回填用媒体行：保留受管路径，并标出文件当前是否仍可用。"""
        path = str(item.get("path") or "")
        target = Path(path).expanduser()
        available = False
        if path and is_uploads_path(Path(self.app.paths.data_dir), target):
            try:
                available = target.resolve().is_file()
            except OSError:
                available = False
        return {
            "node_id": str(item.get("node_id") or ""),
            "field_name": str(item.get("field_name") or ""),
            "path": path,
            "media": str(item.get("media") or ""),
            "name": str(item.get("name") or ""),
            "size": item.get("size"),
            "available": available,
        }

    def rerun_snapshot(self, task_id: str) -> dict[str, Any]:
        """再次生成只读快照：不含 checkpoint、prompt_ids、事件或旧产物目标。"""
        task, base = self._rerun_source(task_id)
        return {
            "source_task_id": str(task.get("id") or ""),
            "workflow_id": base["workflow_id"],
            "workflow_name": base["workflow_name"],
            "prompt": base["prompt"],
            "params": base["params"],
            "overrides": base["overrides"],
            "media_inputs": [self._media_input_snapshot(item) for item in base["media_inputs"]],
            "media_skip": base["media_skip"],
            "shots": base["shots"],
            "unload_after": base["unload_after"],
            "created_at": task.get("created_at"),
            "finished_at": task.get("finished_at"),
        }

    def submit_rerun(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """从完成态任务冻结快照创建新任务；不复制旧 checkpoint/产物。"""
        task, base = self._rerun_source(task_id)
        if not isinstance(body, dict):
            raise ComfyUITaskError("请求体必须是对象")
        extra = set(body) - RERUN_EDITABLE_FIELDS
        if extra:
            raise ComfyUITaskError(
                "再次生成只接受当前表单参数（参数、媒体、段数、释放模型），"
                "不接受工作流基底或来源标识"
            )
        entry = {
            "id": base["workflow_id"],
            "name": base["workflow_name"],
            "prompt": base["prompt"],
            "params": base["params"],
        }
        return self.submit(
            body,
            entry_override=entry,
            source_task_id=str(task.get("id") or ""),
            owner_id=str(task.get("owner_id") or ""),
        )

    def _validate_overrides(self, entry: dict[str, Any], raw: Any) -> list[dict[str, Any]]:
        """overrides 只认扫描出的槽位；值按槽位类型保真转换，转不动直接拒绝。"""
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ComfyUITaskError("overrides 必须是数组")
        params = entry.get("params") or []
        slot_map: dict[tuple[str, str], dict[str, Any]] = {
            (str(slot.get("node_id") or ""), str(slot.get("field_name") or "")): slot
            for slot in params if isinstance(slot, dict)
        }
        out: list[dict[str, Any]] = []
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                raise ComfyUITaskError(f"第 {index} 条参数格式非法")
            node_id = str(item.get("node_id") or "")
            field_name = str(item.get("field_name") or "")
            slot = slot_map.get((node_id, field_name))
            if slot is None:
                raise ComfyUITaskError(
                    f"参数 {node_id} · {field_name} 不是这份工作流的可编辑槽位"
                )
            if str(slot.get("control") or "") == "media":
                # 媒体槽必须走 media_inputs（受管引用 + 执行前上传回填），
                # 不接受裸字符串直传——绕过上传等于绕过校验。
                raise ComfyUITaskError(
                    f"参数 {slot.get('label')} 是媒体输入，请通过文件选择上传，"
                    "不接受文本值"
                )
            value = item.get("value")
            field_type = str(slot.get("field_type") or "text")
            if field_type == "seed":
                # seed：空值 = 每次随机（不留覆盖）；给了就按数字校验。
                if value is None or str(value).strip() == "":
                    continue
                try:
                    if isinstance(value, bool):
                        raise ValueError("seed cannot be boolean")
                    if isinstance(value, int):
                        number = value
                    elif isinstance(value, str):
                        number = int(value.strip())
                    elif isinstance(value, float) and value.is_integer() and abs(value) <= 2 ** 53:
                        number = int(value)
                    else:
                        raise ValueError("seed must be an exact integer")
                except (TypeError, ValueError):
                    raise ComfyUITaskError(f"参数 {slot.get('label')} 必须是数字") from None
                if number < 0:
                    continue  # 负数沿用「随机」语义，不覆盖
                out.append({"node_id": node_id, "field_name": field_name, "value": number})
                continue
            if field_type == "number":
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise ComfyUITaskError(f"参数 {slot.get('label')} 必须是数字") from None
                default = slot.get("default")
                out.append({
                    "node_id": node_id, "field_name": field_name,
                    "value": int(number) if isinstance(default, int) else number,
                })
                continue
            if field_type == "bool":
                if isinstance(value, bool):
                    out.append({"node_id": node_id, "field_name": field_name, "value": value})
                else:
                    text = str(value).strip().lower()
                    if text in ("true", "1", "是"):
                        out.append({"node_id": node_id, "field_name": field_name, "value": True})
                    elif text in ("false", "0", "否", ""):
                        out.append({"node_id": node_id, "field_name": field_name, "value": False})
                    else:
                        raise ComfyUITaskError(f"参数 {slot.get('label')} 必须是开/关")
                continue
            # text / image：原样字符串（image 槽本期不上传，用户填已存在的文件名）。
            out.append({
                "node_id": node_id, "field_name": field_name,
                "value": "" if value is None else str(value),
            })
        return out

    def _validate_media_inputs(self, entry: dict[str, Any], raw: Any) -> list[dict[str, Any]]:
        """媒体输入校验（计划 §5.2/§5.3）：受管引用 + 类型/体积/存在性。

        - 槽位来源不再限于扫描出的媒体槽：映射解释权在用户——payload 明确带
          ``media`` 类型、且该地址在快照图里是标量输入（连线不算）的自定义槽
          同样接受（任务总管的用户映射走这里）；造梦机直跑仍只在其媒体槽 UI
          上选文件，行为不变。
        - 路径必须位于宿主 uploads 受管树内且文件存在——**拒绝任意本机路径**，
          否则等于把客户端路径直接写进执行环境；
        - 文件扩展名要与槽位媒体类型一致，单文件 ≤ MEDIA_INPUT_MAX_BYTES；
        - 校验通过后把归一化的绝对路径存进任务快照；执行前上传 + 回填见 runner。
        """
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ComfyUITaskError("media_inputs 必须是数组")
        params = entry.get("params") or []
        slot_map: dict[tuple[str, str], dict[str, Any]] = {
            (str(slot.get("node_id") or ""), str(slot.get("field_name") or "")): slot
            for slot in params if isinstance(slot, dict)
        }
        graph_inputs: dict[tuple[str, str], Any] = {}
        for node_id, node in (entry.get("prompt") or {}).items():
            inputs = (node or {}).get("inputs") if isinstance(node, dict) else None
            if isinstance(inputs, dict):
                for field_name, value in inputs.items():
                    graph_inputs[(str(node_id), str(field_name))] = value
        data_dir = Path(self.app.paths.data_dir)
        out: list[dict[str, Any]] = []
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                raise ComfyUITaskError(f"第 {index} 条媒体输入格式非法")
            node_id = str(item.get("node_id") or "")
            field_name = str(item.get("field_name") or "")
            slot = slot_map.get((node_id, field_name))
            # control 由 schema 增强写入（在线导入）；离线导入只有 field_type。
            is_media_slot = str(slot.get("control") or "") == "media" if slot else False
            declared = str(item.get("media") or "")
            if slot is None or (
                not is_media_slot
                and str(slot.get("field_type") or "") not in ("image", "audio", "video")
            ):
                current = graph_inputs.get((node_id, field_name))
                if declared not in ("image", "audio", "video") or not isinstance(current, str):
                    raise ComfyUITaskError(
                        f"媒体输入 {node_id} · {field_name} 不是这份工作流的可上传槽位"
                    )
                kind = declared
            else:
                kind = str(slot.get("media") or slot.get("field_type") or "")
            path = str(item.get("path") or "").strip()
            label = str(slot.get("label") or f"{node_id} · {field_name}")
            if not path:
                raise ComfyUITaskError(f"媒体输入 {label} 还没有选择文件")
            target = Path(path).expanduser()
            if not is_uploads_path(data_dir, target):
                raise ComfyUITaskError(
                    f"媒体输入 {label} 必须是通过本应用上传的文件（受管引用），"
                    "不接受任意的本机路径"
                )
            target = target.resolve()
            actual_kind = media_kind_of(str(target))
            if actual_kind != kind:
                raise ComfyUITaskError(
                    f"媒体输入 {label} 的文件类型不对：槽位要 {kind}，文件是 "
                    f"{actual_kind or '非媒体类型'}"
                )
            size = target.stat().st_size
            if size > MEDIA_INPUT_MAX_BYTES:
                raise ComfyUITaskError(
                    f"媒体输入 {label} 超过单文件上限 "
                    f"{MEDIA_INPUT_MAX_BYTES // (1024 * 1024)} MB"
                )
            out.append({
                "node_id": node_id,
                "field_name": field_name,
                "path": str(target),
                "media": kind,
                "name": target.name,
                "size": size,
            })
        return out

    def _validate_media_skips(self, entry: dict[str, Any], raw: Any) -> list[dict[str, Any]]:
        """手动强制跳过：只认扫描出的媒体槽地址，不接受任意节点/字段。

        与 media_inputs 不同，这里要求槽位本身是媒体槽（control=media 或
        field_type 为 image/audio/video）。执行端只在该地址确实存在下游连接时
        移除连接；不以 ComfyUI optional 声明为门槛（用户手动 ⊘ 的明确意图）。
        """
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ComfyUITaskError("media_skip 必须是数组")
        slot_map: dict[tuple[str, str], dict[str, Any]] = {
            (str(slot.get("node_id") or ""), str(slot.get("field_name") or "")): slot
            for slot in (entry.get("params") or []) if isinstance(slot, dict)
        }
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                raise ComfyUITaskError(f"第 {index} 条媒体跳过格式非法")
            node_id = str(item.get("node_id") or "")
            field_name = str(item.get("field_name") or "")
            slot = slot_map.get((node_id, field_name))
            is_media_slot = bool(slot) and (
                str(slot.get("control") or "") == "media"
                or str(slot.get("field_type") or "") in ("image", "audio", "video")
            )
            if not is_media_slot:
                raise ComfyUITaskError(
                    f"媒体跳过 {node_id} · {field_name} 不是这份工作流的媒体槽"
                )
            key = (node_id, field_name)
            if key in seen:
                continue
            seen.add(key)
            out.append({"node_id": node_id, "field_name": field_name})
        return out

    # ---- 查询 ----

    def tasks(self, active_only: bool = False, limit: int = 60,
              scope: str = "all") -> list[dict[str, Any]]:
        if scope not in {"all", "single"}:
            raise ValueError("scope 必须是 single 或 all")
        if active_only:
            return [self._slim(task) for task in self.store.iter_tasks(active_only=True)
                    if scope != "single" or not (task.get("input") or {}).get("batch_id")]
        return [self._slim(task) for task in self.store.list_tasks(active_only=active_only,
                                                                 limit=limit, scope=scope)]

    def task_page(self, page_size: Any = None, cursor: Any = "", *,
                  scope: str = "all", active_only: bool = False) -> dict[str, Any]:
        page = self.store.task_page(page_size, cursor, scope=scope, active_only=active_only)
        page["rows"] = [self._slim(task) for task in page["rows"]]
        return page

    def task(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get_task(task_id)
        if task is None:
            return None
        # 与 tasks() 返回同一套精简字段：详情轮询会直接替换列表里的行，
        # 两边形状不一致会让卡片标题在「任务」和工作流名之间来回闪。
        row = self._slim(task)
        row["can_retry"] = (str(task.get("status") or "") in {"failed", "cancelled", "interrupted"}
                            and not (task.get("checkpoint") or {}).get(COMFYUI_SUBMISSIONS_KEY))
        row["can_cancel"] = str(task.get("status") or "") in COMFYUI_ACTIVE_STATUSES
        return row

    def events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.store.read_events(task_id, after=after)

    # ---- 取消 / 重试 / 删除 ----

    def cancel(self, task_id: str) -> bool:
        return self.runner.cancel(task_id, reason="用户取消")

    def retry(self, task_id: str, *, request_key: str = "", owner_id: str = "") -> dict[str, Any]:
        return self._retry_locked_with_lock(task_id, request_key=request_key, owner_id=owner_id)

    def _retry_locked_with_lock(self, task_id: str, *, request_key: str, owner_id: str) -> dict[str, Any]:
        # Receipt insertion and checkpoint mutation must be indivisible, including retries.
        with self.store._lock:
            return self._retry_locked(task_id, request_key=request_key, owner_id=owner_id)

    def _retry_locked(self, task_id: str, *, request_key: str, owner_id: str) -> dict[str, Any]:
        """重试 = 续跑：保留 checkpoint（已完成段不再提交），errors 清掉后重新排队。"""
        task = self.store.get_task(task_id)
        if task is None:
            raise LookupError("任务不存在")
        owner_id = owner_id or str(task.get("owner_id") or "")
        if task.get("owner_id") and owner_id != task.get("owner_id"):
            raise ComfyUITaskError("任务不属于当前 owner")
        digest = self.store.payload_hash({"task_id": task_id, "operation": "retry"})
        existing = self.lookup_request(request_key, owner_id=owner_id)
        if existing:
            receipts = [existing, *(existing.get("request_receipts") or [])]
            if existing.get("id") != task_id or not any(
                r.get("request_key") == request_key and r.get("owner_id", "") == owner_id
                and r.get("payload_hash") == digest for r in receipts):
                raise ComfyUITaskError("request_key 已绑定不同的操作")
            self.runner.submit(task_id)
            return existing
        if str(task.get("status") or "") in COMFYUI_ACTIVE_STATUSES:
            raise ComfyUITaskError("任务还在执行，请先取消或等待结束")
        checkpoint = dict(task.get("checkpoint") or {})
        if checkpoint.get(COMFYUI_SUBMISSIONS_KEY):
            raise ComfyUITaskError("上次生成请求是否已受理尚未确认，已保留记录；请先在 ComfyUI 核对，不能直接重试重复生成")
        checkpoint["errors"] = []
        # 取消不证明远端已经停止。所有旧凭证都保留，由 runner 核对历史与队列；
        # 只有确认失败或已不在远端的段才重提，成功段只补收集产物。
        shots = int((task.get("input") or {}).get("shots") or 1)
        done = len(checkpoint.get("completed") or [])
        if done >= shots:
            raise ComfyUITaskError("所有段都已完成，没有需要重试的内容")
        self.store.update_task(
            task_id, status="queued", checkpoint=checkpoint, error="", detail="",
            request_receipts=[*(task.get("request_receipts") or []), *([
                {"request_key": request_key, "owner_id": owner_id, "payload_hash": digest}
            ] if request_key else [])],
            # 重试是新的一轮运行：清掉上一轮的结束时刻（开始时刻由 runner 重写）。
            # retry_count 给卡片时间用：重试过的任务显示最近一轮开始而非创建时刻。
            finished_at=None,
            progress={"done": done, "total": shots, "failed": 0},
            retry_count=int(task.get("retry_count") or 0) + 1,
        )
        self.store.append_event(task_id, {
            "type": "log", "message": f"重试：从第 {done + 1} 段继续（已完成的段不会重复提交）",
        })
        self.runner.submit(task_id)
        return self.store.get_task(task_id) or task

    def delete_tasks(self, body: dict[str, Any], *, cancel_timeout: float = 15.0) -> dict[str, Any]:
        """删除任务记录、事件与产物目录。

        - 未指定 id = 清全部：只删已结束的任务，执行中的跳过并回报（「清除已结束」语义）；
        - 指定 id 且 ``cancel_active`` 为真：排队/运行中的任务先取消，等进入终态
          后再删——等待超时或取消失败时如实回报失败原因，任务保留不删。
        """
        ids = body.get("task_ids") if isinstance(body, dict) else None
        cancel_active = bool(isinstance(body, dict) and body.get("cancel_active"))
        # 结果页「单次」档的清除只清单次任务：批量条目有自己那一批的删除入口，
        # 不能因为单次页看不见它们就顺手把整批产物删了。
        exclude_batch = bool(isinstance(body, dict) and body.get("exclude_batch"))
        if isinstance(ids, list) and ids:
            targets = [str(item).strip() for item in ids[:200] if str(item).strip()]
        else:
            # 未指定 id = 清全部：执行中的在循环里跳过并回报。
            targets = []
            for task in self.store.iter_tasks():
                if exclude_batch and str((task.get("input") or {}).get("batch_id") or ""):
                    continue
                targets.append(str(task.get("id") or ""))
        deleted = 0
        skipped: list[str] = []
        failed: list[dict[str, str]] = []
        for task_id in targets:
            task = self.store.get_task(task_id)
            if task is None:
                continue
            if (task.get("checkpoint") or {}).get(COMFYUI_SUBMISSIONS_KEY):
                failed.append({"id": task_id, "reason": "生成请求是否已受理尚未确认，保留任务记录用于核对"})
                continue
            if ((task.get("checkpoint") or {}).get("cancellation") or {}).get("state") == "pending_remote":
                failed.append({"id": task_id, "reason": "远端停止尚未确认，保留任务记录用于对账"})
                continue
            if str(task.get("status") or "") in COMFYUI_ACTIVE_STATUSES:
                if not cancel_active:
                    skipped.append(task_id)
                    continue
                reason = ""
                try:
                    cancelled = self.runner.cancel(task_id, reason="删除任务前自动取消")
                except Exception as exc:  # noqa: BLE001 - 取消失败要如实回报
                    cancelled = False
                    reason = f"取消失败：{exc}"
                if not cancelled:
                    # 返回 False 多半是刚好已终态（竞态）：重查一次，真终态就继续删。
                    task = self.store.get_task(task_id)
                    if task is None:
                        continue
                    if str(task.get("status") or "") in COMFYUI_ACTIVE_STATUSES:
                        failed.append({
                            "id": task_id,
                            "reason": reason or "任务无法取消，已保留这条任务",
                        })
                        continue
                if not self._wait_terminal(task_id, timeout=cancel_timeout):
                    failed.append({
                        "id": task_id,
                        "reason": "已发出取消，但任务迟迟没有停下来，先保留这条任务，稍后再删",
                    })
                    continue
            if self.store.delete_task(task_id):
                deleted += 1
        return {"deleted": deleted, "skipped": skipped, "failed": failed}

    def _wait_terminal(self, task_id: str, *, timeout: float, interval: float = 0.15) -> bool:
        """等任务离开活动状态；超时返回 False（任务保留，由调用方回报）。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            task = self.store.get_task(task_id)
            if task is None or str(task.get("status") or "") not in COMFYUI_ACTIVE_STATUSES:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def delete_workflow(self, workflow_id: str) -> tuple[bool, str]:
        """删除工作流；被排队/执行中的任务引用时拒绝（历史任务用快照，不受影响）。"""
        for task in self.store.iter_tasks(active_only=True):
            input_data = task.get("input") or {}
            if str(input_data.get("workflow_id") or "") == str(workflow_id):
                return False, "有正在排队或执行中的任务还在用这份工作流，请先取消它们再删除"
        return self.store.delete_workflow(workflow_id), ""

    # ---- 内部 ----

    def _slim(self, task: dict[str, Any]) -> dict[str, Any]:
        input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
        checkpoint = task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {"completed": [], "outputs": []}
        return {
            "id": task.get("id"),
            "status": task.get("status"),
            "module": task.get("module"),
            "origin_label": task.get("origin_label"),
            "label": input_data.get("label") or "",
            "batch_id": input_data.get("batch_id") or "",
            "detail": task.get("detail") or "",
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "retry_count": int(task.get("retry_count") or 0),
            "progress": task.get("progress") or {},
            "result": result,
            "output_files": self._output_files(task.get("id"), result),
            "error": task.get("error") or "",
            "source_task_id": task.get("source_task_id") or "",
            "workflow_id": input_data.get("workflow_id") or "",
            "workflow_name": input_data.get("workflow_name") or "",
            "shots": input_data.get("shots") or 1,
            "done_segments": len(checkpoint.get("completed") or []),
            "failed_segments": len(checkpoint.get("errors") or []),
            "step": _running_step(task, checkpoint),
        }

    def _output_files(self, task_id: Any, result: dict[str, Any]) -> list[dict[str, str]]:
        """产物清单：文件名 + 本地绝对路径。

        只有「任务 id + 文件名」不够用：前端要拿这条路径去走 `/api/file`——手机/局域网
        靠 URL 上的口令才取得到字节，桌面端靠 `path` 参数才能弹原生「另存为」
        （`<a download>` 在 WebView2 里点了没反应，见 03-media 的 saveLocalFile）。
        这里只拼接、不校验存在性：文件被清理时交给请求侧 404，与图片 404 同一条链。
        """
        tid = str(task_id or "")
        if not tid:
            return []
        base = self.store.task_output_dir(tid)
        return [
            {"name": str(name), "path": str(base / str(name))}
            for name in (result.get("outputs") or [])
            if str(name or "")
        ]
