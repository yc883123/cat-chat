# -*- coding: utf-8 -*-
"""HTTP 传输层：RequestHandler 与注入式 AppHTTPServer（层级 4）。

自 server.py 整类迁入（2026-09-06，3.4.2-③）；应用实例经 AppHTTPServer 注入
（self.server.app），业务分支下沉为 3.4.3 后续工作；本模块不 import server。
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import secrets
import socket
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import naiba.net as net_io
from naiba.app import NaibaChatApp
from naiba.config import tool_catalog_entries, tool_group_entries, tool_preset_entries
from naiba.core.choices import backfill_turn_choice_groups
from naiba.core.conv_files import _conv_file_allow, _conv_file_open, _conv_file_save
from naiba.core.diagnostics import ensure_utf8_stdio
from naiba.core.exceptions import ActiveRunError
from naiba.core.http_range import content_range_header, parse_byte_range
from naiba.core.media_types import MIME_BY_EXT
from naiba.core.network import network_access_status
from naiba.core.paths import path_within
from naiba.paths import PathContext, default_path_context, static_asset_version
from naiba.storage.avatars import AVATAR_MAX_BYTES
from naiba.storage.media import UPLOAD_MAX_BYTES, _uploads_total_bytes
from naiba.storage.store import PRIMARY_RUN_KINDS as _PRIMARY_RUN_KINDS

logger = logging.getLogger("naiba.http")

# multipart 上传的传输层兜底上限（文件 80MB + 表单/边界开销）。
_UPLOAD_BODY_LIMIT = 100 * 1024 * 1024


# 部分系统 mimetypes 未注册 webp/avif 等，导致 <img> 接到 application/octet-stream
# 配合 nosniff 而拒绝渲染（缩略图破图）。兜底映射唯一定义在 core/media_types.py
# （顺带补齐此前漏登记的 .m4v——视频因此可能不播）。
_MEDIA_MIME_FALLBACK = dict(MIME_BY_EXT)


class AppHTTPServer(ThreadingHTTPServer):
    """携带应用实例的 HTTP 服务器：RequestHandler 经 self.server.app 访问。"""

    def __init__(self, server_address, handler_cls, app: NaibaChatApp):
        super().__init__(server_address, handler_cls)
        self.app = app


class RequestHandler(BaseHTTPRequestHandler):
    @property
    def app(self) -> NaibaChatApp:
        """HTTP 服务器携带的应用实例（AppHTTPServer 注入）。"""
        return self.server.app  # type: ignore[attr-defined]

    server_version = "naiba-chat/1.0"

    def handle_one_request(self) -> None:
        """处理单个请求；任何未捕获异常都返回 500 JSON，而不是静默断连。

        默认 ``BaseHTTPRequestHandler.handle_one_request`` 在异常时只关闭连接、不写回包，
        浏览器会看到 ``net::ERR_EMPTY_RESPONSE``，难以定位。这里在异常时回一个 500 JSON。
        """
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            mname = "do_" + self.command
            if not hasattr(self, mname):
                self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method '%s'" % self.command)
                return
            method = getattr(self, mname)
            method()
            self.wfile.flush()
        except TimeoutError:
            self.log_error("Request timed out")
            self.close_connection = True
            return
        except socket.timeout:
            self.log_error("Request timed out")
            self.close_connection = True
            return
        except Exception as exc:  # noqa: BLE001
            self.log_error("Request handler error: %s", exc)
            self.close_connection = True
            try:
                # headers_sent 在首次 send_response 前不存在，用 getattr 兜底，否则会再抛
                # AttributeError 被吞掉、导致依然不回包（net::ERR_EMPTY_RESPONSE）。
                if not getattr(self.wfile, "closed", True) and not getattr(self, "headers_sent", False):
                    body = json.dumps({"error": f"服务器内部错误：{exc}"}, ensure_ascii=False).encode("utf-8")
                    self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {format_string % args}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            self._json({"status": "ok", "mcp": self.app.mcp.states()})
            return
        if path.startswith("/api/") and not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return
        if path == "/api/storage/stats":
            self._json(self.app.storage.storage_usage())
        elif path == "/api/imaging/stats":
            self._json({"image_cache_bytes": _uploads_total_bytes(self.app.paths.data_dir)})
        elif path == "/api/bootstrap":
            self._json(self.app.bootstrap())
        elif path == "/api/update":
            self._json(self.app.updater.status())
        elif path == "/api/agents":
            self._json({"agents": self.app.config.public_agents(), "default_agent_id": self.app.config.default_agent_id()})
        elif path == "/api/provider-presets":
            # 供应商预设表（「只填 API Key」的接入模板）：设置弹层与首启引导共用同一份。
            self._json(self.app.api_provider_presets())
        elif path == "/api/tasks":
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            active_only = query.get("active_only", ["0"])[0] == "1"
            # jobs_only=1：只回后台作业（shell/check/http_poll/comfyui/subagent）。
            # background_tasks 是 run 与 job 共用的一张表，chat/plan_execute 那些行是
            # "每一次回答的记录"而不是任务——平铺给任务面板会让用户以为"我发过的每句话
            # 都是一个任务"。过滤交给 SQL：默认 limit 是 50 且 chat 行占绝大多数。
            jobs_only = query.get("jobs_only", ["0"])[0] == "1"
            tasks = self.app.tasks.list(
                conversation_id,
                active_only,
                limit=200 if jobs_only else 50,
                exclude_kinds=_PRIMARY_RUN_KINDS if jobs_only else None,
            )
            self._json({"tasks": tasks})
        elif path == "/api/runs":
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            active_only = query.get("active_only", ["0"])[0] == "1"
            # 该接口回答的是「这条会话在不在回答」，因此只回**顶层对话 Run**
            # （后台子 Job 不算，见 runs.list_primary）。卡住的子任务曾把
            # 「分支 / 重新生成 / 新会话」三个救援入口全部挡住（2026-09-14）。
            # 需要看子任务请用 /api/tasks。
            self._json({"runs": self.app.runs.list_primary(conversation_id, active_only)})
        elif path.startswith("/api/runs/") and path.endswith("/events"):
            run_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after = max(0, int(query.get("after", ["0"])[0]))
            except ValueError:
                self._json({"error": "after 必须是整数"}, HTTPStatus.BAD_REQUEST)
                return
            self._stream_run(run_id, after)
        elif path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            run = self.app.runs.get(run_id)
            self._json(run or {"error": "运行不存在"}, HTTPStatus.OK if run else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            task = self.app.storage.get_background_task(task_id)
            self._json(task or {"error": "任务不存在"}, HTTPStatus.OK if task else HTTPStatus.NOT_FOUND)
        # ---- Harness Job Registry 接口 ----
        elif path == "/api/jobs":
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            active_only = query.get("active_only", ["0"])[0] == "1"
            jobs = self.app.jobs.list(owner=conversation_id or None, active_only=active_only)
            self._json({"jobs": jobs})
        elif path.startswith("/api/jobs/") and path.endswith("/events"):
            job_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            try:
                after = max(0, int(query.get("after", ["0"])[0]))
            except ValueError:
                self._json({"error": "after 必须是整数"}, HTTPStatus.BAD_REQUEST)
                return
            # 只读：Job 输出跨对话/跨会话可查询（配合 resume 记录的原 Job ID）。
            self._json(self.app.jobs.read(job_id, after))
        elif path.startswith("/api/jobs/") and path.endswith("/status"):
            job_id = path.split("/")[-2]
            job = self.app.jobs.get(job_id)
            self._json(job or {"error": "Job 不存在"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
        elif path == "/api/tools":
            self._json({"tools": self.app.tool_registry.schemas()})
        elif path == "/api/tool_catalog":
            catalog = tool_catalog_entries(self.app.tool_registry.schemas())
            self._json({
                "tools": catalog,
                "groups": tool_group_entries(catalog),
                "presets": tool_preset_entries(catalog),
            })
        elif path == "/api/mcp":
            self._json({"servers": self.app.mcp.states()})
        elif path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            # 只读：Job 详情跨对话/跨会话可查询。
            job = self.app.jobs.get(job_id)
            self._json(job or {"error": "Job 不存在"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
        elif path == "/api/conversations":
            query = urllib.parse.parse_qs(parsed.query)
            mode = query.get("mode", [None])[0]
            self._json({"conversations": self.app.storage.list_conversations(mode=mode)})
        elif path == "/api/workspaces":
            self._json({"workspaces": self.app.config.data.get("workspaces", [])})
        elif path == "/api/starter-prompts":
            # missing_presets：内置预设缺失数（前端据此决定是否显示「恢复默认预设」）
            self._json({
                "prompts": self.app.config.get_starter_prompts(),
                "missing_presets": self.app.config.count_missing_starter_presets(),
            })
        elif path == "/api/quick-messages":
            query = urllib.parse.parse_qs(parsed.query)
            self._json({"messages": self.app.config.get_quick_messages(query.get("sort", [""])[0])})
        elif path == "/api/conversation-prompt-presets":
            self._json({"presets": self.app.config.get_conversation_prompt_presets()})
        elif path == "/api/tool_sets":
            self._json({"tool_sets": self.app.config.get_tool_sets()})
        elif path.startswith("/api/conversations/") and path.endswith("/file/open"):
            conversation_id = path.split("/")[-3]
            query = urllib.parse.parse_qs(parsed.query)
            conversation = self.app.storage.get_conversation(conversation_id)
            if not conversation:
                self._json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
                return
            try:
                self._json(_conv_file_open(conversation, self.app.config, query.get("path", [""])[0]))
            except FileNotFoundError as exc:
                self._json({"error": f"文件不存在或已被移动：{exc}"}, HTTPStatus.NOT_FOUND)
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path.startswith("/api/conversations/") and path.endswith("/file/raw"):
            conversation_id = path.split("/")[-3]
            query = urllib.parse.parse_qs(parsed.query)
            conversation = self.app.storage.get_conversation(conversation_id)
            if not conversation:
                self._json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
                return
            try:
                raw_path = query.get("path", [""])[0]
                target, listed, within_root, _root = _conv_file_allow(conversation, self.app.config, raw_path)
                if target is None or not (listed or within_root):
                    raise ValueError("无权访问该文件：不在本会话改动记录中，也不在会话工作区内")
                if not target.is_file():
                    raise FileNotFoundError(str(target))
                data = target.read_bytes()
                if len(data) > 32 * 1024 * 1024:
                    raise ValueError("文件过大，无法预览")
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if content_type == "application/octet-stream":
                    content_type = _MEDIA_MIME_FALLBACK.get(target.suffix.lower(), content_type)
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type",
                    f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type,
                )
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError as exc:
                self._json({"error": f"文件不存在或已被移动：{exc}"}, HTTPStatus.NOT_FOUND)
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/search/messages":
            # 侧栏「全文」搜索：只读，复用 storage 的 instr(lower()) 子串匹配。
            # 校验（q 空 400 / limit 非法 400 / conversation_id 不存在 404）在 app 层，
            # 与 /api/conversations 一样属于「传输层只解析、业务层只判定」的分工。
            self._json(*self.app.api_search_messages(urllib.parse.parse_qs(parsed.query)))
        elif path.endswith("/branch_chain"):
            # 分支链（源 + 兄弟分支 / 自己的全部分支）：必须排在下面的
            # /api/conversations/<id> 兜底分支之前，否则会被当成会话 id 吃掉。
            conversation_id = path.split("/")[-2]
            self._json(*self.app.api_branch_chain(conversation_id))
        elif path.endswith("/first_turn"):
            conversation_id = path.split("/")[-2]
            info = self.app._first_turn_info(conversation_id)
            self._json(info or {}, HTTPStatus.OK)
        elif path.startswith("/api/conversations/"):
            conversation_id = path.rsplit("/", 1)[-1]
            conversation = self.app.storage.get_conversation(conversation_id)
            if conversation and conversation.get("messages"):
                backfill_turn_choice_groups(conversation["messages"])
            self._json(conversation or {"error": "对话不存在"}, HTTPStatus.OK if conversation else HTTPStatus.NOT_FOUND)
        elif path == "/api/plans":
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            if not conversation_id:
                self._json({"error": "conversation_id 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"plans": self.app.storage.list_plans(conversation_id)})
        elif path.startswith("/api/plans/"):
            plan_id = path.rsplit("/", 1)[-1]
            plan = self.app.storage.get_plan(plan_id)
            self._json(plan or {"error": "计划不存在"}, HTTPStatus.OK if plan else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/providers/") and path.endswith("/secret"):
            provider_id = path.split("/")[-2]
            api_key = self.app.config.provider_secret(provider_id)
            self._json(
                {"api_key": api_key} if api_key is not None else {"error": "供应商不存在"},
                HTTPStatus.OK if api_key is not None else HTTPStatus.NOT_FOUND,
            )
        elif path == "/api/model-profiles":
            query = urllib.parse.parse_qs(parsed.query)
            kind = query.get("kind", [None])[0]
            self._json({"profiles": self.app.config.model_profiles(kind)})
        elif path.startswith("/api/agents/avatar/"):
            self._serve_agent_avatar(path.rsplit("/", 1)[-1])
        elif path == "/api/file":
            query = urllib.parse.parse_qs(parsed.query)
            self._serve_local_file(query.get("path", [""])[0])
        elif path == "/api/workspace/browse":
            try:
                query = urllib.parse.parse_qs(parsed.query)
                self._json(self.app.browse_workspace(
                    query.get("path", [""])[0],
                    query.get("conversation_id", [""])[0],
                ))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/backgrounds":
            # 内置背景图清单（首次访问时幂等生成到 data/backgrounds/）。
            self._json(self.app.background_presets())
        elif path == "/api/install/dirs":
            self._json(self.app.list_skill_dirs())
        elif path == "/api/mcp/status/light":
            self._json({"servers": self.app.mcp.lightweight_status()})
        elif path == "/api/migration/health":
            self._json(self.app.migration_health())
        elif path.startswith("/api/"):
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        else:
            self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # 上传接口改 multipart 流式（不再走 JSON base64）：跳过 JSON 读取分流。
        if path == "/api/uploads" and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            self._upload_request()
            return
        # Agent 头像同样走 multipart（小体积、进内存），也必须在 JSON 读取前分流。
        if path == "/api/agents/avatar" and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            if not self._authorized(parsed):
                self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
                return
            self._agent_avatar_upload()
            return
        body = self._read_json(max_size=130 * 1024 * 1024)
        if body is None:
            return
        if path == "/api/auth":
            if self._is_local_request():
                self._json({"ok": True, "local": True})
                return
            valid = secrets.compare_digest(str(body.get("token") or ""), str(self.app.config.data["access_token"]))
            self._json({"ok": valid}, HTTPStatus.OK if valid else HTTPStatus.UNAUTHORIZED)
            return
        if not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return

        if path == "/api/storage/compact":
            try:
                self._json(self.app.storage.compact_database())
            except sqlite3.OperationalError as exc:
                self._json(
                    {"error": f"数据库压缩失败：{exc}"},
                    HTTPStatus.CONFLICT,
                )
        elif path == "/api/conversations":
            self._json(*self.app.api_create_conversation(body))
        elif path.startswith("/api/conversations/") and path.endswith("/branch"):
            conversation_id = path.split("/")[-2]
            message_id = str(body.get("message_id") or "")
            if not conversation_id or not message_id:
                self._json({"error": "conversation_id 和 message_id 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                result = self.app.storage.branch_conversation(conversation_id, message_id)
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(result, HTTPStatus.CREATED)
        elif path.startswith("/api/conversations/") and path.endswith("/session_start"):
            # 手动「新会话」分割线：标在指定消息上（不新增行、不删消息），
            # 重放侧遇到该标记即从此条之后重算上下文。
            conversation_id = path.split("/")[-2]
            conversation = self.app.storage.get_conversation(conversation_id, include_messages=False)
            if not conversation:
                self._json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
                return
            after_message_id = str(body.get("after_message_id") or "")
            if not after_message_id:
                self._json({"error": "after_message_id 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            marker = self.app.storage.set_session_start(
                conversation_id,
                after_message_id,
                source=str(body.get("source") or "manual"),
                handoff_path=str(body.get("handoff_path") or ""),
                note=str(body.get("note") or ""),
            )
            self._json({"message": marker} if marker else {"error": "消息不存在"},
                       HTTPStatus.CREATED if marker else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/conversations/") and path.endswith("/tools"):
            conversation_id = path.split("/")[-2]
            tools = body.get("tools") or []
            if not isinstance(tools, list) or not tools:
                self._json({"error": "tools 必须是非空数组"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                result = self.app.runs.enable_conversation_tools(
                    conversation_id, [str(item) for item in tools if str(item).strip()]
                )
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(result, HTTPStatus.OK)
        elif path.startswith("/api/conversations/") and path.endswith("/file/save"):
            conversation_id = path.split("/")[-3]
            raw_path = body.get("path")
            content = body.get("content")
            if not isinstance(raw_path, str) or not str(raw_path).strip():
                self._json({"error": "path 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            conversation = self.app.storage.get_conversation(conversation_id)
            if not conversation:
                self._json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
                return
            try:
                self._json(_conv_file_save(conversation, self.app.config, raw_path, content))
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path.startswith("/api/conversations/") and path.endswith("/settings"):
            conversation_id = path.split("/")[-2]
            self._json(*self.app.api_update_conversation_settings(conversation_id, body))
        elif path == "/api/workspaces":
            self._json(*self.app.api_upsert_workspace(body))
        elif path == "/api/workspaces/delete":
            self._json(*self.app.api_delete_workspace(body))
        elif path == "/api/agents":
            try:
                self._json(self.app.config.upsert_agent(body))
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/providers":
            try:
                self._json(self.app.api_upsert_model_profile(body))
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/provider-presets/open":
            # 「打开注册页」：preset_id 进、白名单地址出，不接受前端传 URL（见 app.py）。
            self._json(*self.app.api_open_provider_key_url(str(body.get("preset_id") or "")))
        elif path == "/api/providers/test":
            self._test_provider(body)
        elif path == "/api/providers/models":
            self._provider_models(body)
        elif path == "/api/providers/unload":
            self._unload_provider(body)
        elif path == "/api/models/unload":
            self._unload_provider(body)
        elif path == "/api/model-profiles/test":
            self._test_provider(body)
        elif path == "/api/model-profiles/models":
            self._provider_models(body)
        elif path == "/api/model-profiles/unload":
            self._unload_provider(body)
        elif path == "/api/model-profiles":
            try:
                self._json(self.app.api_upsert_model_profile(body))
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/imaging/clean":
            self._json(*self.app.api_clean_image_cache())
        elif path == "/api/settings":
            try:
                self._json(*self.app.api_update_runtime_settings(body))
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/settings/import-legacy":
            # 从用户指定的旧数据目录导入 config.json 与 data/（保留目标已存在数据）。
            try:
                source = Path(str(body.get("source") or "").strip()).expanduser().resolve()
                if not source.is_dir():
                    self._json({"error": "旧数据目录不存在"}, HTTPStatus.BAD_REQUEST)
                    return
                imported = self.app.import_legacy_data(source)
                self._json({"ok": True, **imported})
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/mcp/test":
            try:
                server_id = str(body.get("server_id") or "").strip()
                result = self.app.test_mcp_server(server_id)
                self._json(result)
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/workspace/pick":
            try:
                self._json(self.app.pick_workspace_directory(str(body.get("initial") or "")))
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/mcp/register":
            try:
                self._json(self.app.register_mcp_server(body))
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/mcp/remove":
            try:
                server_id = str(body.get("server_id") or "").strip()
                if not server_id:
                    raise ValueError("server_id 不能为空")
                self._json(self.app.remove_mcp_server(server_id))
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/mcp/reconnect":
            try:
                server_id = str(body.get("server_id") or "").strip()
                result = self.app.reconnect_mcp_server(server_id)
                self._json(result)
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/vision/test":
            try:
                selected = body.get("provider_model_key")
                selected_key = str(selected) if selected is not None else None
                probe = str(body.get("probe") or "vision").strip().lower()
                if probe == "text":
                    self._json(self.app.vision.probe_text(selected_key))
                elif probe == "vision":
                    self._json(self.app.vision.probe(selected_key))
                else:
                    self._json({"ok": False, "reason": "Unsupported vision probe"}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/search/test":
            try:
                self._json(self.app.web_search.probe(body))
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/update/check":
            self._json(self.app.updater.start_check(force=True))
        elif path == "/api/update/install":
            try:
                target_tag = None
                if isinstance(body, dict):
                    target_tag = body.get("tag")
                self._json(self.app.updater.start_install(target_tag=target_tag, on_ready=self.app.update_restart_callback))
            except RuntimeError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/character-card/parse":
            self._parse_character_card(body)
        elif path == "/api/conversation-prompt-presets":
            try:
                item = self.app.config.add_conversation_prompt_preset(
                    str(body.get("title") or ""),
                    str(body.get("system_prompt") or ""),
                    str(body.get("source") or "manual"),
                )
                self._json({"preset": item}, HTTPStatus.CREATED)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path.startswith("/api/conversation-prompt-presets/"):
            preset_id = path.rsplit("/", 1)[-1]
            try:
                item = self.app.config.update_conversation_prompt_preset(
                    preset_id, str(body.get("title") or ""), str(body.get("system_prompt") or "")
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"preset": item} if item else {"error": "快捷提示词不存在"}, HTTPStatus.OK if item else HTTPStatus.NOT_FOUND)
        elif path == "/api/tool_sets":
            try:
                item = self.app.config.upsert_tool_set(
                    str(body.get("name") or ""),
                    body.get("tools") or [],
                    str(body.get("id") or ""),
                )
                self._json({"tool_set": item}, HTTPStatus.OK)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/uploads":
            self._json({"error": "上传接口已升级为 multipart 流式"}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/uploads/delete":
            self._json(*self.app._delete_upload(body))
        elif path == "/api/uploads/rotate":
            self._json(*self.app.api_rotate_upload(body))
        elif path == "/api/uploads/check":
            self._json(*self.app.api_check_uploads(body))
        elif path == "/api/install/dir":
            self._install_dir(body)
        elif path == "/api/install/dir/remove":
            self._remove_install_dir(body)
        elif path == "/api/skills/install":
            self._install_skill(body)
        elif path == "/api/skills/install_folder":
            self._install_folder(body)
        elif path == "/api/skills/scan":
            self._json({
                "skills": self.app.catalog.scan(),
                "configured": self.app.config.get_skills_dirs(),
                "hidden_skills": self._hidden_skill_entries(),
                "recycled_skills": self.app._recycled_skill_entries(),
            })
        elif path == "/api/skills/delete":
            self._delete_skill(body)
        elif path == "/api/skills/recycle/clear":
            self._clear_skill_recycle(body)
        elif path == "/api/skills/recycle/restore":
            self._restore_skill_recycle(body)
        elif path == "/api/skills/unhide":
            self._unhide_skill(body)
        elif path == "/api/starter-prompts":
            title = str(body.get("title") or "").strip()
            text = str(body.get("text") or "").strip()
            if not text:
                self._json({"error": "指令内容不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                prompts = self.app.config.add_starter_prompt(title, text)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(
                {"prompts": prompts, "missing_presets": self.app.config.count_missing_starter_presets()},
                HTTPStatus.CREATED,
            )
        elif path == "/api/quick-messages":
            text = str(body.get("text") or "").strip()
            try:
                messages = self.app.config.add_quick_message(text)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"messages": messages}, HTTPStatus.CREATED)
        elif path.startswith("/api/quick-messages/") and path.endswith("/use"):
            index = path.split("/")[-2]
            try:
                idx = int(index)
                if idx < 0:
                    raise ValueError
            except ValueError:
                self._json({"error": "无效的快捷消息序号"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"messages": self.app.config.record_quick_message_use(idx)})
        elif path.startswith("/api/quick-messages/"):
            index = path.rsplit("/", 1)[-1]
            try:
                idx = int(index)
                if idx < 0:
                    raise ValueError
            except ValueError:
                self._json({"error": "无效的快捷消息序号"}, HTTPStatus.BAD_REQUEST)
                return
            text = str(body.get("text") or "").strip()
            try:
                messages = self.app.config.update_quick_message(idx, text)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"messages": messages})
        elif path == "/api/starter-prompts/restore":
            # 恢复默认预设：把缺失的内置预设补回列表头部（用户删除/改坏后的一键恢复）
            self._json({"prompts": self.app.config.restore_starter_presets()})
        elif path.startswith("/api/starter-prompts/"):
            index = path.rsplit("/", 1)[-1]
            try:
                idx = int(index)
                if idx < 0:
                    raise ValueError
            except ValueError:
                self._json({"error": "无效的指令序号"}, HTTPStatus.BAD_REQUEST)
                return
            title = str(body.get("title") or "").strip()
            text = str(body.get("text") or "").strip()
            if not text:
                self._json({"error": "指令内容不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                prompts = self.app.config.update_starter_prompt(idx, title, text)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"prompts": prompts, "missing_presets": self.app.config.count_missing_starter_presets()})
        elif path == "/api/migration/backup":
            self._json(self.app.migration_backup())
        elif path == "/api/migration/run":
            self._json(self.app.migration_run())
        elif path == "/api/migration/move-data":
            self._json(self.app.migration_move_data(body))
        elif path == "/api/migration/merge":
            self._json(self.app.migration_merge(body))
        elif path == "/api/chat/interject":
            # 插话（运行中入队）：202 表示「已排队，不代表已发送」——真正的发送时机
            # 由用户在队列面板点「引导」决定（见 run/manager.py 的插话段注释）。
            try:
                self._json(self.app.runs.interject(body), HTTPStatus.ACCEPTED)
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/chat/interject/guide":
            try:
                self._json(self.app.runs.guide_interjection(body))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/chat/interject/edit":
            try:
                self._json(self.app.runs.edit_interjection(body))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/chat/interject/delete":
            try:
                self._json(self.app.runs.delete_interjection(body))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/chat/cancel":
            run_id = str(body.get("run_id") or "").strip()
            conversation_id = str(body.get("conversation_id") or "").strip()
            if not run_id and not conversation_id:
                self._json({"error": "run_id 和 conversation_id 不能同时为空"}, HTTPStatus.BAD_REQUEST)
            else:
                if not run_id:
                    # list_primary 已限定为顶层对话 Run（kind + parent_job_id），
                    # 这里直接取最新的那条。
                    primary = self.app.runs.list_primary(conversation_id, active_only=True)
                    run_id = str((primary[0] if primary else {}).get("id") or "")
                run = self.app.runs.get(run_id) if run_id else None
                if run and conversation_id and str(run.get("conversation_id") or "") != conversation_id:
                    self._json({"error": "运行不属于当前对话"}, HTTPStatus.BAD_REQUEST)
                    return
                cancelled = self.app.runs.cancel(run_id) if run_id else None
                self._json(
                    {"cancelled": bool(cancelled), "run": cancelled},
                    HTTPStatus.OK if cancelled else HTTPStatus.NOT_FOUND,
                )
        elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path.split("/")[-2]
            if not job_id:
                self._json({"error": "job_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            else:
                reason = str(body.get("reason") or "") or None
                job = self.app.jobs.cancel(job_id, owner=body.get("conversation_id") or None, reason=reason)
                self._json(
                    {"cancelled": bool(job), "job": job},
                    HTTPStatus.OK if job else HTTPStatus.NOT_FOUND,
                )
        elif path.startswith("/api/jobs/") and path.endswith("/resume"):
            job_id = path.split("/")[-2]
            if not job_id:
                self._json({"error": "job_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            else:
                try:
                    new_id = self.app.jobs.resume(job_id, owner=body.get("conversation_id") or None)
                except ValueError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                else:
                    if new_id:
                        self._json({"resumed": True, "job_id": new_id}, HTTPStatus.OK)
                    else:
                        self._json({"error": "Job 不可恢复或不存在"}, HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/jobs/") and path.endswith("/retry"):
            job_id = path.split("/")[-2]
            if not job_id:
                self._json({"error": "job_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            else:
                try:
                    new_id = self.app.jobs.retry(job_id, owner=body.get("conversation_id") or None)
                except ValueError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                else:
                    if new_id:
                        self._json({"retried": True, "job_id": new_id}, HTTPStatus.OK)
                    else:
                        self._json({"error": "Job 不存在或无权访问"}, HTTPStatus.NOT_FOUND)
        elif path == "/api/chat":
            self._chat(body)
        elif path == "/api/tasks":
            try:
                self._json(self.app.tasks.submit(body), HTTPStatus.ACCEPTED)
            except ActiveRunError as exc:
                self._json(
                    {"error": str(exc), "active_run_id": exc.run_id},
                    HTTPStatus.CONFLICT,
                )
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/tool/confirm":
            self._confirm_tool(body)
        elif path == "/api/tool/reject":
            self._reject_tool(body)
        elif path.startswith("/api/plans/") and path.endswith("/execute"):
            plan_id = path.split("/")[-2]
            try:
                self._json(
                    self.app.runs.submit_plan(
                        plan_id,
                        web_search_enabled=bool(body.get("web_search_enabled", False)),
                    ),
                    HTTPStatus.ACCEPTED,
                )
            except ActiveRunError as exc:
                self._json(
                    {"error": str(exc), "active_run_id": exc.run_id},
                    HTTPStatus.CONFLICT,
                )
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path.startswith("/api/plans/") and path.endswith("/cancel"):
            plan_id = path.split("/")[-2]
            try:
                self._json(self.app.plans.cancel(plan_id))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path.startswith("/api/plans/") and path.endswith("/keep-planning"):
            plan_id = path.split("/")[-2]
            try:
                self._json(self.app.plans.keep_planning(plan_id))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/messages/edit":
            self._edit_message(body)
        elif path == "/api/messages/delete":
            self._json(*self.app.api_delete_message(body))
        elif path == "/api/messages/restore":
            self._json(*self.app.api_restore_messages(body))
        else:
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json()
        if body is None:
            return
        if not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return
        if path.startswith("/api/plans/"):
            plan_id = path.rsplit("/", 1)[-1]
            try:
                self._json(
                    self.app.plans.edit_plan(
                        plan_id,
                        title=body.get("title"),
                        content=body.get("content"),
                    )
                )
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return
        path = parsed.path
        if path == "/api/tasks/clear":
            self._json({"deleted": self.app.storage.clear_terminal_background_tasks()})
        elif path.startswith("/api/quick-messages/"):
            index = path.rsplit("/", 1)[-1]
            try:
                idx = int(index)
                if idx < 0:
                    raise ValueError
            except ValueError:
                self._json({"error": "无效的快捷消息序号"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"messages": self.app.config.remove_quick_message(idx)})
        elif path.startswith("/api/starter-prompts/"):
            index = path.rsplit("/", 1)[-1]
            try:
                idx = int(index)
                if idx < 0:
                    raise ValueError
            except ValueError:
                self._json({"error": "无效的指令序号"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({
                "prompts": self.app.config.remove_starter_prompt(idx),
                "missing_presets": self.app.config.count_missing_starter_presets(),
            })
        elif path.startswith("/api/conversation-prompt-presets/"):
            deleted = self.app.config.delete_conversation_prompt_preset(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/tool_sets/"):
            deleted = self.app.config.delete_tool_set(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/conversations/") and path.endswith("/messages"):
            conversation_id = path.split("/")[-2]
            if self.app.storage.list_background_tasks(conversation_id, active_only=True):
                self._json({"error": "当前对话仍有运行中的任务，无法清空"}, HTTPStatus.CONFLICT)
                return
            self._json({"deleted": self.app.storage.clear_conversation_messages(conversation_id)})
        elif path.startswith("/api/session_start/"):
            # 撤销分割线：先按遗留标记行删（role=session），否则清掉消息 metadata 上的标记。
            message_id = path.rsplit("/", 1)[-1]
            deleted = self.app.storage.delete_session_start(message_id)
            if not deleted:
                deleted = self.app.storage.clear_session_start(message_id)
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/conversations/"):
            deleted = self.app.storage.delete_conversation(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/tasks/") and path.endswith("/cancel"):
            task_id = path.split("/")[-2]
            task = self.app.tasks.cancel(task_id)
            self._json(task or {"error": "任务不存在"}, HTTPStatus.OK if task else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/agents/"):
            deleted = self.app.config.delete_agent(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/model-profiles/"):
            deleted = self.app.config.delete_model_profile(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/providers/"):
            deleted = self.app.config.delete_provider(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/skills/"):
            skill_id = path.rsplit("/", 1)[-1]
            self._delete_skill_by_id(skill_id)
        else:
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _authorized(self, parsed: urllib.parse.ParseResult) -> bool:
        # Desktop/localhost requests do not need the LAN access token.
        if self._is_local_request():
            return True
        expected = str(self.app.config.data["access_token"])
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.startswith("Bearer ") else ""
        if not provided:
            provided = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        return bool(provided) and secrets.compare_digest(provided, expected)

    def _is_local_request(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1", "localhost"}

    def _read_json(self, max_size: int = 2 * 1024 * 1024) -> dict[str, Any] | None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > max_size:
                self._json({"error": "请求内容过大"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return None
            payload = self.rfile.read(size) if size else b"{}"
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON 必须是对象")
            return value
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json({"error": f"无效 JSON：{exc}"}, HTTPStatus.BAD_REQUEST)
            return None

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, requested_path: str) -> None:
        relative = "index.html" if requested_path in {"", "/"} else requested_path.lstrip("/")
        path = (self.app.paths.public_dir / relative).resolve()
        try:
            path.relative_to(self.app.paths.public_dir.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            path = self.app.paths.public_dir / "index.html"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        if path.name == "index.html":
            data = data.replace(b"__ASSET_VERSION__", static_asset_version(self.app.paths.public_dir).encode("ascii"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _serve_local_file(self, source: str) -> None:
        if source.startswith("http://127.0.0.1:8188/") or source.startswith("http://localhost:8188/"):
            try:
                with net_io.open(source, timeout=60) as response:
                    data = response.read()
                    content_type = response.headers.get_content_type()
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type,
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        path = Path(source).expanduser().resolve()
        # Attachments created before a data-directory migration contain an
        # absolute path from the old install. Resolve those records by
        # filename inside the current uploads directory.
        current_data_root = self.app.paths.data_dir.resolve()
        current_uploads = (current_data_root / "uploads").resolve()
        if not path.is_file() and path.name:
            migrated_path = current_uploads / path.name
            if not migrated_path.is_file() and current_uploads.is_dir():
                # 分日目录（uploads/YYYY-MM-DD/）下的同命中值递归兜底。
                for candidate in current_uploads.rglob(path.name):
                    if candidate.is_file():
                        migrated_path = candidate
                        break
            if migrated_path.is_file():
                path = migrated_path
        allowed_roots = [
            self.app.config.resolve_workspace_dir(),
            current_data_root,
        ]
        if not any(path_within(path, root) for root in allowed_roots):
            self._json({"error": "文件不在允许访问的目录中"}, HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            self._json({"error": "文件不存在"}, HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        # 单区间 Range：视频/音频可 seek（浏览器拖动进度条、preload=metadata 只拉所需区间）。
        # 不解析成功（无头/非法/多区间）→ 整包 200；语法合法但越界 → 416 + bytes */size。
        try:
            byte_range = parse_byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = (0, size - 1) if byte_range is None else byte_range
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type == "application/octet-stream":
            content_type = _MEDIA_MIME_FALLBACK.get(path.suffix.lower(), content_type)
        self.send_response(HTTPStatus.OK if byte_range is None else HTTPStatus.PARTIAL_CONTENT)
        self.send_header(
            "Content-Type",
            f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type,
        )
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        if byte_range is not None:
            self.send_header("Content-Range", content_range_header(start, end, size))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        # 分块发送：几百 MB 的视频不再整包进内存。
        remaining = max(0, end - start + 1)
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    chunk = handle.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError as exc:
            # 响应头已发出，只能记录（客户端会看到截断的响应）。
            logger.warning("读取文件失败：path=%s error=%s", path, exc)

    def _parse_character_card(self, body: dict[str, Any]) -> None:
        self._json(*self.app._parse_character_card(body))

    def _read_multipart_small(self, max_bytes: int) -> tuple[dict[str, str], bytes] | None:
        """读取体积受限的 multipart 表单：文本字段进 dict、单个文件进内存。

        只用于小文件接口（Agent 头像）；大文件上传走 `_upload_request` 的 spool 流式路径。
        出错时已回包并返回 None。
        """
        from python_multipart import MultipartParser
        from python_multipart.multipart import parse_options_header

        content_type = str(self.headers.get("Content-Type", ""))
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if not length:
            self._json({"error": "缺少 Content-Length"}, HTTPStatus.BAD_REQUEST)
            return None
        if length > max_bytes + 64 * 1024:
            self._json({"error": "请求内容过大"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        _, params = parse_options_header(content_type)
        # parse_options_header 的键为 bytes（b'boundary'），兼容双形态。
        boundary = params.get("boundary") or params.get(b"boundary") or b""
        if not boundary:
            self._json({"error": "缺少 multipart boundary"}, HTTPStatus.BAD_REQUEST)
            return None

        fields: dict[str, str] = {}
        state: dict[str, Any] = {
            "name": "", "filename": "", "field": b"", "value": b"",
            "data": bytearray(), "too_large": False, "complete": False,
        }

        def on_part_begin() -> None:
            state["name"] = ""
            state["filename"] = ""
            state["field"] = b""
            state["value"] = b""
            state["data"] = bytearray()

        def on_header_field(data: bytes, start: int, end: int) -> None:
            state["field"] += data[start:end]

        def on_header_value(data: bytes, start: int, end: int) -> None:
            state["value"] += data[start:end]

        def on_header_end() -> None:
            field = state["field"].decode("latin-1").strip().lower()
            value = state["value"].decode("latin-1").strip()
            state["field"] = b""
            state["value"] = b""
            if field != "content-disposition":
                return
            _, disp_params = parse_options_header(value)
            raw_name = disp_params.get("name") or disp_params.get(b"name") or b""
            state["name"] = raw_name.decode("latin-1") if isinstance(raw_name, bytes) else str(raw_name)
            raw_filename = disp_params.get("filename") or disp_params.get(b"filename")
            if not raw_filename:
                return
            decoded = raw_filename.decode("latin-1") if isinstance(raw_filename, bytes) else str(raw_filename)
            try:
                # multipart header 的 filename 按 latin-1 解析，中文（UTF-8 字节）需还原。
                decoded = decoded.encode("latin-1").decode("utf-8")
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
            state["filename"] = Path(decoded.strip()).name

        def on_part_data(data: bytes, start: int, end: int) -> None:
            written = end - start
            if state["filename"] and len(state["data"]) + written > max_bytes:
                state["too_large"] = True
                return
            state["data"] += data[start:end]

        def on_part_end() -> None:
            if state["filename"]:
                state["file_data"] = bytes(state["data"])
            elif state["name"]:
                fields[state["name"]] = bytes(state["data"]).decode("utf-8", "replace")

        def on_end() -> None:
            state["complete"] = True

        callbacks = {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        }
        try:
            parser = MultipartParser(boundary, callbacks)
            remaining = length
            while remaining > 0 and not state["too_large"]:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                parser.write(chunk)
            if not state["too_large"]:
                parser.finalize()
            else:
                self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 解析失败/客户端中断统一走错误收尾
            try:
                self._json({"error": f"表单解析失败：{exc}"}, HTTPStatus.BAD_REQUEST)
            except OSError:
                pass
            return None
        if state["too_large"]:
            self._json({"error": "请求内容过大"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        file_data = state.get("file_data") or b""
        if not state["complete"] or not file_data:
            self._json({"error": "上传中断或未收到图片内容"}, HTTPStatus.BAD_REQUEST)
            return None
        return fields, file_data

    def _agent_avatar_upload(self) -> None:
        parsed = self._read_multipart_small(AVATAR_MAX_BYTES)
        if parsed is None:
            return
        fields, data = parsed
        self._json(*self.app.api_set_agent_avatar(fields.get("agent_id", ""), data))

    def _serve_agent_avatar(self, name: str) -> None:
        """Agent 头像：内容哈希命名 → 可长期强缓存；文件名非法/文件缺失走 404。"""
        payload, status = self.app.api_read_agent_avatar(name)
        if status != HTTPStatus.OK or not isinstance(payload, (bytes, bytearray)):
            self._json(payload if isinstance(payload, dict) else {"error": "头像不存在"}, status)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/webp")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _upload_request(self) -> None:
        """multipart/form-data 流式上传：解析到临时 spool 后交给 app 落盘。

        设计：python_multipart 流式回调（不整包进内存），文件 part 字节直接写
        spool（uploads 根下的 .part），完成后 _upload_spooled 做去重/落盘/图片处理。
        客户端中断（abort）时解析不完整 → spool 清理，不留垃圾。
        """
        from python_multipart import MultipartParser
        from python_multipart.multipart import parse_options_header

        content_type = str(self.headers.get("Content-Type", ""))
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length > _UPLOAD_BODY_LIMIT:
            self._json({"error": "请求内容过大"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        if not length:
            self._json({"error": "缺少 Content-Length"}, HTTPStatus.BAD_REQUEST)
            return
        _, params = parse_options_header(content_type)
        # 注意：parse_options_header 的返回键为 bytes（b'boundary'），兼容双形态。
        boundary = params.get("boundary") or params.get(b"boundary") or b""
        if not boundary:
            self._json({"error": "缺少 multipart boundary"}, HTTPStatus.BAD_REQUEST)
            return
        uploads_root = (self.app.paths.data_dir / "uploads").resolve()
        try:
            uploads_root.mkdir(parents=True, exist_ok=True)
            spool = tempfile.NamedTemporaryFile(
                mode="wb", delete=False, dir=uploads_root, suffix=".part"
            )
        except OSError as exc:
            self._json({"error": f"无法创建上传临时目录：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        state: dict[str, Any] = {
            "filename": "",
            "headers": {},
            "header_field": b"",
            "header_value": b"",
            "spool": spool,
            "written": 0,
            "too_large": False,
            "complete": False,
        }

        def on_part_begin() -> None:
            state["filename"] = ""
            state["headers"] = {}

        def on_header_field(data: bytes, start: int, end: int) -> None:
            state["header_field"] += data[start:end]

        def on_header_value(data: bytes, start: int, end: int) -> None:
            state["header_value"] += data[start:end]

        def on_header_end() -> None:
            field = state["header_field"].decode("latin-1").strip().lower()
            value = state["header_value"].decode("latin-1").strip()
            state["headers"][field] = value
            state["header_field"] = b""
            state["header_value"] = b""

        def _decode_multipart_filename(raw: Any) -> str:
            """multipart header 的 filename 按 latin-1 字节流解析；中文（UTF-8 字节）
            会被解码成乱码——还原原始字节后按 UTF-8 解码，失败回退原值。"""
            value = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw or "")
            try:
                return value.encode("latin-1").decode("utf-8")
            except (UnicodeDecodeError, UnicodeEncodeError):
                return value

        def on_headers_finished() -> None:
            disposition = state["headers"].get("content-disposition", "")
            _, disp_params = parse_options_header(disposition)
            # 键为 bytes（b'filename'），兼容双形态。
            raw_filename = disp_params.get("filename") or disp_params.get(b"filename")
            filename = _decode_multipart_filename(raw_filename) if raw_filename else ""
            if filename and filename.strip():
                state["filename"] = Path(filename.strip()).name
                state["headers"].clear()

        def on_part_data(data: bytes, start: int, end: int) -> None:
            if state["filename"] and not state["too_large"]:
                written = end - start
                if state["written"] + written > UPLOAD_MAX_BYTES:
                    state["too_large"] = True
                    return
                try:
                    state["spool"].write(data[start:end])
                    state["written"] += written
                except OSError:
                    state["too_large"] = True

        def on_part_end() -> None:
            state["spool"].flush()

        def on_end() -> None:
            state["complete"] = True

        callbacks = {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        }
        try:
            parser = MultipartParser(boundary, callbacks)
            remaining = length
            while remaining > 0 and not state["too_large"]:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                parser.write(chunk)
            if not state["too_large"]:
                parser.finalize()
            else:
                # 超限即停：不再消费请求体，关闭连接避免残留污染。
                self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 解析失败/客户端中断统一走错误收尾
            state["spool"].close()
            Path(state["spool"].name).unlink(missing_ok=True)
            try:
                self._json({"error": f"上传解析失败：{exc}"}, HTTPStatus.BAD_REQUEST)
            except OSError:
                pass
            return

        spool_path = state["spool"].name
        state["spool"].close()
        if state["too_large"] or state["written"] > UPLOAD_MAX_BYTES:
            Path(spool_path).unlink(missing_ok=True)
            self._json({"error": "单个文件不能超过 80 MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        if not state["complete"] or not state["filename"] or state["written"] == 0:
            # 客户端中断（abort）或空表单：清理 spool，不返回错误（连接可能已断）。
            Path(spool_path).unlink(missing_ok=True)
            self._json({"error": "上传中断或未收到文件内容"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            self._json(*self.app._upload_spooled(spool_path, state["filename"]))
        except OSError as exc:
            self._json({"error": f"上传保存失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _install_dir(self, body: dict[str, Any]) -> None:
        self._json(*self.app._install_dir(body))

    def _remove_install_dir(self, body: dict[str, Any]) -> None:
        self._json(*self.app._remove_install_dir(body))


    def _finish_install(self, dest_raw: str, dest: Path, extra: dict[str, Any] | None = None) -> None:
        self._json(*self.app._finish_install(dest_raw, dest, extra))

    def _install_skill(self, body: dict[str, Any]) -> None:
        self._json(*self.app._install_skill(body))

    def _install_folder(self, body: dict[str, Any]) -> None:
        self._json(*self.app._install_folder(body))

    def _hidden_skill_entries(self) -> list[dict[str, Any]]:
        return self.app._hidden_skill_entries()

    def _unhide_skill(self, body: dict[str, Any]) -> None:
        self._json(*self.app._unhide_skill(body))

    def _delete_skill(self, body: dict[str, Any]) -> None:
        self._json(*self.app._delete_skill(body))

    def _clear_skill_recycle(self, body: dict[str, Any]) -> None:
        self._json(*self.app._clear_skill_recycle(body))

    def _restore_skill_recycle(self, body: dict[str, Any]) -> None:
        self._json(*self.app._restore_skill_recycle(body))

    def _delete_skill_by_id(self, body: dict[str, Any]) -> None:
        self._json(*self.app._delete_skill_by_id(body))


    def _test_provider(self, body: dict[str, Any]) -> None:
        self._json(*self.app._test_provider(body))

    def _provider_models(self, body: dict[str, Any]) -> None:
        self._json(*self.app._provider_models(body))

    def _unload_provider(self, body: dict[str, Any]) -> None:
        self._json(*self.app._unload_provider(body))

    def _resolve_model_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.app._resolve_model_profile(body)

    def _provider_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.app._provider_profile(body)

    def _edit_message(self, body: dict[str, Any]) -> None:
        self._json(*self.app._edit_message(body))

    def _chat(self, body: dict[str, Any]) -> None:
        try:
            run = self.app.runs.submit_chat(body)
        except ActiveRunError as exc:
            self._json(
                {"error": str(exc), "active_run_id": exc.run_id},
                HTTPStatus.CONFLICT,
            )
            return
        except LookupError as exc:
            self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (ValueError, TypeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._stream_run(str(run["id"]), 0, known_run=run)

    def _stream_run(
        self,
        run_id: str,
        after: int = 0,
        known_run: dict[str, Any] | None = None,
    ) -> None:
        run = known_run or self.app.runs.get(run_id)
        if not run:
            self._json({"error": "运行不存在"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sequence = max(0, int(after))
        try:
            while True:
                events = self.app.runs.wait_for_events(run_id, sequence, timeout=15.0)
                for event in events:
                    # 回放（页面重载/重连 after=0 起）会把**已处理过**的 tool_confirm 事件
                    # 原样重发，前端据此又渲染出带「允许执行」按钮的卡——点了只会撞 409
                    # 「确认请求不属于该运行或已失效」。流出口统一标注当前仍否待确认：
                    # 实时事件此刻必在 pending（executor 先登记后发事件），不受影响。
                    if event.get("type") == "tool_confirm":
                        confirm_id = str(event.get("confirm_id") or "")
                        if confirm_id and not self.app.runs.owns_confirmation(run_id, confirm_id):
                            event = {**event, "confirm_resolved": True}
                    self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
                    sequence = max(sequence, int(event.get("sequence") or 0))
                current = self.app.runs.get(run_id)
                if not current or current.get("status") in self.app.runs.TERMINAL:
                    if not self.app.runs.events_after(run_id, sequence):
                        break
                if not events:
                    self.wfile.write(b'{"type":"heartbeat"}\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Detaching a stream never cancels its conversation-owned run.
            return

    def _confirm_tool(self, body: dict[str, Any]) -> None:
        self._json(*self.app._confirm_tool(body))

    def _reject_tool(self, body: dict[str, Any]) -> None:
        self._json(*self.app._reject_tool(body))


# ---- 服务生命周期：状态文件 / 实例锁 / 主入口（自 server.py 收口迁入） ----

def write_status(host: str, port: int, token: str, paths: PathContext) -> None:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    access = network_access_status(host, port)
    paths.status_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": host,
                "port": port,
                **access,
                "access_token": token,
                "started_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def acquire_instance_lock(paths: PathContext):
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        handle = paths.lock_path.open("a+b")
        if paths.lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Cat Chat 已经在运行，请勿重复启动") from exc
        return handle
    except OSError as exc:
        raise RuntimeError(
            f"无法创建实例锁文件（{exc}）：请检查数据目录 {paths.data_dir} 及锁文件 {paths.lock_path} 是否可写"
        ) from exc


def main_entry(paths: PathContext | None = None, on_app=None) -> None:
    """命令行主入口：构造应用、绑定 HTTP 服务并运行（自 server.py 收口迁入）。"""
    import argparse

    paths = paths or default_path_context()
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    ensure_utf8_stdio(line_buffering=True, write_through=True)
    parser = argparse.ArgumentParser(description="Cat Chat 局域网对话服务")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    try:
        instance_lock = acquire_instance_lock(paths)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    APP = NaibaChatApp(paths=paths)
    if on_app:
        on_app(APP, paths)
    host = args.host or str(APP.config.data.get("host", "0.0.0.0"))
    port = args.port or int(APP.config.data.get("port", 8765))
    APP.config.data["host"] = host
    APP.config.data["port"] = port
    APP.config.save()
    APP.listener_host = host
    server = AppHTTPServer((host, port), RequestHandler, APP)
    server.daemon_threads = True
    write_status(host, port, str(APP.config.data["access_token"]), paths)
    print("\nCat Chat 已启动")
    access = network_access_status(host, port)
    print(f"手机访问： {access['lan_url'] or access['lan_reason']}")
    print(f"本机访问： {access['local_url']}")
    print(f"访问口令： {APP.config.data['access_token']}")
    print("电脑端不需要打开网页。按 Ctrl+C 停止服务。\n")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        APP.stop()
        instance_lock.close()
        try:
            paths.status_path.unlink(missing_ok=True)
        except OSError:
            pass
