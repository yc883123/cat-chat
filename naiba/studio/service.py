"""Studio 业务编排（工作台的域门面；/api/studio/* 的业务方法从这里出）。

职责边界：
- 拥有 只读目录（catalog）、自定义库（custom）、任务管理（jobs=runner+store）、交接（handoff）。
- 生成输入在这里最终定稿：模板/自定义/自由输入 + 用户输入 + 变量字面替换，产出
  `content`（发给模型的 user 消息）；字面替换不做表达式/脚本求值。
- API 方法返回 (payload, HTTPStatus) 二元组，与 `app` 的 `api_*` 风格一致；HTTP 层只做转发。
"""

from __future__ import annotations

import re
from typing import Any

from naiba.llm.profiles import resolve_profile_with_override
from naiba.studio.catalog import CatalogError, StudioCatalog
from naiba.studio.custom import CustomStore
from naiba.studio.handoff import HandoffAdapter, compose_handoff_message
from naiba.studio.images import StudioImageError, validate_image_refs
from naiba.studio.jobs import StudioJobManager
from naiba.studio.runner import StudioRunner
from naiba.studio.store import StudioStore

VAR_PATTERN = re.compile(r"\{\{([^{}]*)\}\}")
VAR_PLACEHOLDER_DISPLAY = re.compile(r"\{\{([^{}]*)\}\}")


class StudioService:
    """组装根唯一装配点；`app.studio.*` 调用面。"""

    def __init__(self, app: Any, create_conversation_callback: Any, submit_chat_callback: Any):
        self.app = app
        self.catalog = StudioCatalog(app.paths.public_dir / "studio")
        self.custom = CustomStore(app.paths.data_dir)
        store = StudioStore(app.paths.data_dir)
        runner = StudioRunner(app, store)
        self.jobs = StudioJobManager(app, store, runner)
        self.handoff = HandoffAdapter(create_conversation_callback, submit_chat_callback)
        self._runner = runner
        self._store = store

    # ---- 生命周期 ----

    def recover(self) -> int:
        return self.jobs.recover_interrupted()

    def shutdown(self) -> None:
        self.jobs.shutdown()

    def referenced_upload_paths(self) -> set[str]:
        """缓存清理用的引用保护：工作台任务仍引用的上传图片（主图 + 缩略图）。"""
        return self._store.referenced_upload_paths()

    # ---- 目录 ----

    def api_catalog_summary(self) -> tuple[dict[str, Any], int]:
        try:
            return {"catalog": self.catalog.summary()}, 200
        except CatalogError as exc:
            return {"error": str(exc)}, 500

    def api_catalog_items(self, module: str = "", item_type: str = "", query: str = "",
                          has_vars: Any = None) -> tuple[dict[str, Any], int]:
        try:
            parsed_vars: bool | None
            if has_vars is None or str(has_vars) == "":
                parsed_vars = None
            else:
                parsed_vars = str(has_vars) in ("1", "true", "True")
            rows = self.catalog.items(module=module, item_type=item_type, query=query, has_vars=parsed_vars)
            return {"items": rows, "count": len(rows)}, 200
        except CatalogError as exc:
            return {"error": str(exc)}, 400

    def api_catalog_item(self, item_id: str) -> tuple[dict[str, Any], int]:
        try:
            row = self.catalog.get_body(item_id)
            meta = self.catalog.item(item_id)
        except LookupError:
            return {"error": "工作台条目不存在"}, 404
        except CatalogError as exc:
            return {"error": str(exc)}, 404
        return {"item": meta, "body": row}, 200

    # ---- 自定义库 ----

    def api_custom_list(self) -> tuple[dict[str, Any], int]:
        return self.custom.list(), 200

    def api_custom_create(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            row = self.custom.create(
                str(body.get("body") or ""),
                title=str(body.get("title") or ""),
                module=str(body.get("module") or "global"),
                item_type=str(body.get("type") or "template"),
                tags=[str(t) for t in (body.get("tags") or []) if str(t).strip()],
                summary=str(body.get("summary") or ""),
                genre=str(body.get("genre") or ""),
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return {"item": row}, 201

    def api_custom_update(self, item_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            row = self.custom.update(item_id, body)
        except LookupError:
            return {"error": "自定义条目不存在"}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return {"item": row}, 200

    def api_custom_delete(self, item_id: str) -> tuple[dict[str, Any], int]:
        try:
            self.custom.delete(item_id)
        except LookupError:
            return {"error": "自定义条目不存在"}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return {"ok": True}, 200

    def api_custom_import(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        filename = str((body or {}).get("filename") or "")
        content = body.get("content")
        if not isinstance(content, str):
            return {"error": "content 必须是文本"}, 400
        try:
            row = self.custom.import_body(filename, content)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return {"item": row}, 201

    # ---- 生成任务 ----

    def api_generate(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = body if isinstance(body, dict) else {}
        model_key = str(body.get("model_key") or "").strip()
        model_name = str(body.get("model_name") or "").strip()[:256]
        if not model_key:
            model_key = str(self.app.config.data.get("default_model_key") or "")
        user_request = str(body.get("user_request") or "").strip()
        item_id = str(body.get("item_id") or "").strip()
        custom_id = str(body.get("custom_id") or "").strip()
        vars_raw = body.get("vars") or {}
        if not isinstance(vars_raw, dict):
            return {"error": "vars 必须是对象"}, 400
        try:
            # 图片引用是服务端权威校验：客户端只能给受管 uploads 里的真实图片。
            images = validate_image_refs(self.app.paths.data_dir, body.get("images"))
        except StudioImageError as exc:
            return {"error": str(exc)}, 400
        title = ""
        source_body = ""
        origin_label = ""
        if item_id:
            try:
                meta = self.catalog.item(item_id)
                source_body = self.catalog.get_body(item_id)
                title = str(meta.get("title") or "")
                module = str(meta.get("module") or "global")
                origin_label = "预设库"
            except LookupError:
                return {"error": "工作台条目不存在"}, 404
            except CatalogError as exc:
                return {"error": str(exc)}, 404

        elif custom_id:
            try:
                meta = self.custom.get(custom_id)
            except LookupError:
                return {"error": "自定义条目不存在"}, 404
            except ValueError as exc:
                return {"error": str(exc)}, 400
            title = str(meta.get("title") or "")
            module = str(meta.get("module") or "global")
            source_body = str(meta.get("body") or "")
            origin_label = "自定义"

        elif not user_request:
            return {"error": "请选择条目或填写输入"}, 400
        else:
            # 自由输入：用户输入即全文（未选条目/自定义条目）。
            module = "global"
            title = (user_request[:40] + "…") if len(user_request) > 40 else user_request
            origin_label = ""
        if source_body:
            content, unresolved = fill_vars(source_body, user_request, vars_raw)
        else:
            content, unresolved = user_request, []
        if not content.strip():
            return {"error": "生成内容为空：请填写输入或变量"}, 400
        input_data = {
            "content": content,
            "model_key": model_key,
            "model_name": model_name,
            "item_id": item_id,
            "custom_id": custom_id,
            "title": title,
            "module": module,
            "origin_label": origin_label,
            "user_request": user_request,
            "source_body": source_body,
            "custom_id": custom_id,
            "vars": {str(k): str(v) for k, v in (vars_raw or {}).items()},
            "unresolved_vars": unresolved,
            "images": images,
        }
        try:
            task = self.jobs.submit(input_data)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except LookupError as exc:
            return {"error": str(exc)}, 404
        return {"task": task, "unresolved_vars": unresolved}, 201

    def api_vision(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """图片反推：优先用当前参数里选中的模型，识别不了再用视觉配置兜底。

        逐张调用（而不是一次性把多图塞给模型）是为了让「图N → 描述」的对应关系
        由我们保证，而不是靠模型自己在一段混合输出里划线。单张失败只影响该张，
        其余结果照常返回；全部失败按 502 如实上报，绝不伪造已识图。
        """
        body = body if isinstance(body, dict) else {}
        try:
            images = validate_image_refs(self.app.paths.data_dir, body.get("images"))
        except StudioImageError as exc:
            return {"error": str(exc)}, 400
        if not images:
            return {"error": "请先上传图片"}, 400
        question = str(body.get("question") or "").strip()[:2000]
        model_key = str(body.get("model_key") or "").strip()
        model_name = str(body.get("model_name") or "").strip()[:256]
        selected_profile: dict[str, Any] | None = None
        selected_note = ""
        if model_key and model_name:
            try:
                candidate = resolve_profile_with_override(self.app.config, model_key, model_name)
                if candidate.get("supports_images_explicit") is False:
                    # 设置里明确标了纯文本：不浪费一次必然失败的请求，直接走视觉配置。
                    selected_note = "当前模型在设置里标为不支持图片，已改用视觉 API"
                else:
                    selected_profile = candidate
            except (ValueError, KeyError) as exc:
                selected_note = f"当前模型配置不可用（{exc}），已改用视觉 API"
        results: list[dict[str, Any]] = []
        failure_notes: list[str] = []
        fallback_used = 0
        for index, ref in enumerate(images, 1):
            prompt = (
                f"这是第 {index} 张图（图{index}）。请只描述这一张图片：主体、构图、风格、"
                "光线、画面中的文字（如有），以及可用于复现这张图的提示词要点。"
            )
            if question:
                prompt += f"\n补充要求：{question}"
            text = ""
            backend = ""
            direct_error = ""
            if selected_profile is not None:
                try:
                    text = str(
                        self.app.vision.describe_files_with_profile(
                            selected_profile, [ref["path"]], prompt
                        ) or ""
                    ).strip()
                    if text:
                        backend = "selected"
                except Exception as exc:  # noqa: BLE001 - 失败后仍可用视觉配置兜底
                    direct_error = str(exc) or "当前模型识别失败"
            if not text:
                # 当前参数模型没有识别成功（或明确不支持）→ 视觉配置兜底。
                try:
                    text = str(self.app.vision.describe_files([ref["path"]], prompt) or "").strip()
                    if text:
                        backend = "vision"
                        fallback_used += 1
                except Exception as exc:  # noqa: BLE001 - 单图失败不拖累其他图
                    if direct_error:
                        message = f"{direct_error}；视觉兜底也失败：{exc}"
                    else:
                        message = str(exc) or "视觉识别失败"
                    results.append({
                        "index": index, "name": ref["name"], "path": ref["path"],
                        "text": "", "error": message, "backend": "",
                    })
                    failure_notes.append(f"图{index}：{message}")
                    continue
            results.append({
                "index": index, "name": ref["name"], "path": ref["path"],
                "text": text, "error": "", "backend": backend,
                "fallback_reason": direct_error[:300] if backend == "vision" and direct_error else "",
            })
        analyzed = sum(1 for row in results if not row["error"])
        text = "\n\n".join(
            f"【图{row['index']}】\n{row['text']}" for row in results if not row["error"]
        )
        payload = {
            "ok": analyzed == len(results),
            "images": results,
            "text": text,
            "analyzed": analyzed,
            "failed": len(results) - analyzed,
            "fallback_used": fallback_used,
            "selected_note": selected_note,
        }
        if not analyzed:
            payload["error"] = "视觉识别全部失败：" + "；".join(failure_notes)
            return payload, 502
        return payload, 200

    def api_tasks(self, active_only: str = "", module: str = "",
                  exclude_module: str = "") -> tuple[dict[str, Any], int]:
        """任务列表（按模块分区）。

        `module` / `exclude_module` 是分区参数：模块库传 `exclude_module=novel`、
        创作场传 `module=novel`，各看各的、各 60 条。不传参保持原有全量行为。

        `module` / `origin_label` **从 `input` 提升到顶层**：这两个字段一直只写在任务
        input 里（`api_generate`），落库记录顶层从来没有；列表 slim 却按顶层取，于是
        历史列表「来源」恒为「来源未记录」。提升放这里而不是改存储结构，老任务
        （input 里已有值）也一并修好。
        """
        rows = self.jobs.tasks(
            active_only=active_only in ("1", "true"),
            module=module, exclude_module=exclude_module,
        )
        slim = []
        for row in rows:
            input_data = row.get("input") if isinstance(row.get("input"), dict) else {}
            item = {
                k: row.get(k)
                for k in ("id", "status", "detail", "title", "created_at", "updated_at",
                          "result", "result_partial", "error", "handoff", "usage")
            }
            # 顶层有值就用顶层（将来真落库时不用再改这里），否则从 input 取。
            item["module"] = row.get("module") or input_data.get("module") or ""
            item["origin_label"] = row.get("origin_label") or input_data.get("origin_label") or ""
            # title 同理：落库时只写 input.title（如《书名》· 阶段名），顶层从没有；
            # 不提升的话列表里所有创作场任务都退化成前端兜底的「生成任务」。
            item["title"] = row.get("title") or input_data.get("title") or ""
            novel = input_data.get("novel") if isinstance(input_data.get("novel"), dict) else None
            if novel:
                item["novel"] = {
                    "book_id": str(novel.get("book_id") or ""),
                    "stage": str(novel.get("stage") or ""),
                    "chapter_ids": list(novel.get("chapter_ids") or []),
                }
            slim.append(item)
        return {"tasks": slim}, 200

    def api_task(self, task_id: str) -> tuple[dict[str, Any], int]:
        task = self.jobs.task(task_id)
        if not task:
            return {"error": "任务不存在"}, 404
        return {"task": task}, 200

    def api_tasks_delete(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """批量删除历史产出（编辑模式多选删除）。

        只接受任务 ID 列表；不存在与进行中的任务分别以 `not_found` / `active`
        放进 `skipped`，调用方据此提示「跳过 N 条进行中的任务」。
        """
        raw = (body or {}).get("ids") if isinstance(body, dict) else None
        if not isinstance(raw, list) or not raw:
            return {"error": "ids 必须是非空数组"}, 400
        ids = [str(item).strip() for item in raw if str(item).strip()][:200]
        if not ids:
            return {"error": "ids 必须是非空数组"}, 400
        result = self.jobs.delete(ids)
        return {
            "ok": True,
            "deleted": len(result["deleted"]),
            "deleted_ids": result["deleted"],
            "skipped": result["skipped"],
        }, 200

    def api_task_events(self, task_id: str, after: Any) -> tuple[dict[str, Any], int]:
        task = self.jobs.task(task_id)
        if not task:
            return {"error": "任务不存在"}, 404
        try:
            parsed_after = max(0, int(str(after or "0")))
        except (TypeError, ValueError):
            return {"error": "after 必须是非负整数"}, 400
        rows = self.jobs.events(task_id, after=parsed_after)
        last_seq = int(rows[-1]["seq"]) if rows else parsed_after
        terminal = self._store.has_terminal_event(task_id)
        terminal_seq = self._store.terminal_event_seq(task_id)
        return {
            "events": rows,
            "count": len(rows),
            "after": parsed_after,
            "last_seq": max(last_seq, parsed_after),
            "terminal": terminal,
            "terminal_seq": terminal_seq,
            "status": task.get("status"),
        }, 200

    def api_task_cancel(self, task_id: str) -> tuple[dict[str, Any], int]:
        task = self.jobs.task(task_id)
        if not task:
            return {"error": "任务不存在"}, 404
        if task.get("status") in {"done", "failed"}:
            return {"error": "任务已结束，无需取消"}, 409
        if not self.jobs.cancel(task_id):
            return {"error": "任务无法取消（可能已结束）"}, 409
        return {"ok": True}, 200

    def api_task_retry(self, task_id: str) -> tuple[dict[str, Any], int]:
        try:
            task = self.jobs.retry(task_id)
        except LookupError:
            return {"error": "任务不存在"}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return {"task": task}, 201

    # ---- 交接 ----

    def api_handoff(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = body if isinstance(body, dict) else {}
        task_id = str(body.get("task_id") or "").strip()
        agent_id = str(body.get("agent_id") or "").strip()
        note = str(body.get("note") or "").strip()
        if not task_id:
            return {"error": "task_id 不能为空"}, 400
        if not agent_id:
            return {"error": "请选择 Agent（必选）"}, 400
        task = self.jobs.task(task_id)
        if not task:
            return {"error": "任务不存在"}, 404
        if task.get("status") != "done":
            return {"error": "生成任务未完成，无法交接"}, 409
        agent = self.app.config.get_agent(agent_id)
        if not agent:
            return {"error": "Agent 不存在或已被删除"}, 400
        input_data = task.get("input") or {}
        record = {
            "item_id": input_data.get("item_id") or "",
            "custom_id": input_data.get("custom_id") or "",
            "title": input_data.get("title") or "",
            "module": input_data.get("module") or "global",
            "origin_label": input_data.get("origin_label") or "",
            "vars": input_data.get("vars") or {},
            "user_request": input_data.get("user_request") or "",
            "source_body": input_data.get("source_body") or "",
            "result": str(task.get("result") or ""),
        }
        model_key = str(input_data.get("model_key") or "")
        model_name = str(input_data.get("model_name") or "")
        try:
            result = self.handoff.handoff(record, agent_id=agent_id, note=note,
                                          model_key=model_key, model_name=model_name)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except (LookupError, RuntimeError) as exc:
            return {"error": f"交接失败：{exc}".strip()}, 502
        updated = self._store.update_task(task_id, handoff={
            "conversation_id": result["conversation_id"],
            "run_id": str((result.get("run") or {}).get("id") or ""),
            "agent_id": agent_id,
            "note": note,
            "message": result.get("message") or "",
        })
        handoff_conversation_id = str(result["conversation_id"])
        _ = updated
        return {
            "conversation_id": handoff_conversation_id,
            "run_id": str((result.get("run") or {}).get("id") or ""),
            "task": updated or {},
        }, 201

    # ---- 结尾：组合根重启恢复调用 ----

    def preview_handoff_message(self, task_id: str, note: str = "") -> str:
        task = self.jobs.task(task_id)
        if not task:
            raise LookupError("任务不存在")
        input_data = task.get("input") or {}
        return compose_handoff_message({
            "item_id": input_data.get("item_id") or "",
            "title": input_data.get("title") or "",
            "module": input_data.get("module") or "global",
            "origin_label": input_data.get("origin_label") or "",
            "vars": input_data.get("vars") or {},
            "user_request": input_data.get("user_request") or "",
            "source_body": input_data.get("source_body") or "",
            "result": task.get("result") or task.get("result_partial") or "",
        }, note=note)


def fill_vars(body_text: str, user_request: str, vars_map: dict[str, Any]) -> tuple[str, list[str]]:
    """字面文本替换：`{{key}}` → 变量值；未识别/未未提供占位符原样保留并提示。"""
    text = str(body_text or "")
    unresolved: list[str] = []
    key_upper_map = {str(k).strip(): str(v) for k, v in (vars_map or {}).items()}
    resolved: dict[str, str] = {}
    for token in VAR_PATTERN.findall(text):
        key = token.strip()
        if key not in key_upper_map:
            if key not in unresolved:
                unresolved.append(key)
            continue
        resolved[token.strip()] = key_upper_map[token.strip()]
    for key, value in resolved.items():
        text = text.replace("{{" + key + "}}", value)
    user_request = str(user_request or "").strip()
    if user_request:
        block = f"\n\n【我的输入】\n{user_request}"
        text = (text + block) if not (text.endswith("\n\n") or not text.strip()) else (text.strip() + block)
    return text, unresolved
