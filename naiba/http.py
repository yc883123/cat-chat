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
import re
import secrets
import socket
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import naiba.net as net_io
from naiba.app import NaibaChatApp
from naiba.config import tool_catalog_entries, tool_group_entries, tool_preset_entries
from naiba.core.choices import backfill_turn_choice_groups
from naiba.core.conv_files import (
    ConvFilePreviewError,
    FolderConfirmRequired,
    _conv_file_allow,
    _conv_file_open,
    _conv_file_save,
)
from naiba.core.diagnostics import ensure_utf8_stdio
from naiba.core.exceptions import ActiveRunError
from naiba.core.http_range import content_range_header, parse_byte_range
from naiba.core.media_types import MIME_BY_EXT
from naiba.core.messages import strip_message_traces
from naiba.core.network import network_access_status, port_conflict_message
from naiba.core.paths import path_within
from naiba.paths import PathContext, default_path_context, static_asset_version
from naiba.storage.app_icon import APP_ICON_MAX_BYTES
from naiba.storage.avatars import AVATAR_MAX_BYTES
from naiba.storage.media import UPLOAD_MAX_BYTES
from naiba.storage.store import PRIMARY_RUN_KINDS as _PRIMARY_RUN_KINDS

logger = logging.getLogger("naiba.http")

# multipart 上传的传输层兜底上限（文件 80MB + 表单/边界开销）。
_UPLOAD_BODY_LIMIT = 100 * 1024 * 1024

# /api/health 的应用身份标记（launcher 用它排除「窗口开到别人的服务上」）：
# 端口被别的 HTTP 服务占用时，健康检查只看 200 会放行，用户会在别人的应用里打字。
# 该字段是**新增**的，老客户端/冒烟脚本只读 status 不受影响。
HEALTH_APP_MARKER = "cat-chat"

# Windows 专用套接字选项（其它平台没有这个名字；Windows 上取值是 **-5**，
# 所以判"可用性"必须用 `is None`，不能拿符号当哨兵——踩过）。
_SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)

# run 事件流关流前的宽限：run 状态置终态（`update_background_task`）与终态事件落库（`emit`）
# 不是一次原子写，中间窗口里"状态已终态 + 没有新事件"并不等于"终态事件不会来了"。只按状态
# 关流实测偶发让客户端整轮收不到 done，所以这里给它一小段时间等终态事件出现（见 _stream_run）。
# 取值只需覆盖那几毫秒的写库窗口；真有 run 崩死没落终态事件时，代价也只是这一小段等待。
TERMINAL_EVENT_GRACE_SECONDS = 2.0


# 部分系统 mimetypes 未注册 webp/avif 等，导致 <img> 接到 application/octet-stream
# 配合 nosniff 而拒绝渲染（缩略图破图）。兜底映射唯一定义在 core/media_types.py
# （顺带补齐此前漏登记的 .m4v——视频因此可能不播）。
_MEDIA_MIME_FALLBACK = dict(MIME_BY_EXT)


def skill_import_body_limit_mb(setting_mb: int) -> int:
    """Skill 导入端点的请求体上限（MB）：max(130, 设置值 × 1.5)。

    文件先 base64 编码再进 JSON（膨胀约 1/3）+ JSON 结构开销，按 ×1.5 留余量；
    设置 0（内置默认）= 全局默认 130MB，与引入该设置前逐字一致。只对
    /api/skills/install 与 /api/skills/install_folder 生效，聊天等其他请求不动。
    """
    return max(130, int(max(0, int(setting_mb or 0)) * 1.5))


def content_disposition_attachment(filename: str) -> str:
    """`Content-Disposition: attachment` 头（RFC 6266 + RFC 5987）。

    手机浏览器只能靠这个头把「内联预览」变成「保存到本地」——没有它，点开 .md / .py
    只会把文本显示在页面里，用户找不到任何保存入口（§九.135 第 3 项）。
    注意 HTTP 头只能是 latin-1：中文名必须走 `filename*=UTF-8''<百分号编码>`，同时给一个
    纯 ASCII 的 `filename=` 兜底，否则含中文的名字在只认旧写法的客户端上会被截断或丢字。
    """
    name = re.sub(r"[\r\n\t/\\]+", "_", str(filename or "")).strip() or "download"
    fallback = name.encode("ascii", "replace").decode("ascii").replace("?", "_") or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def _port_has_listener(host: str, port: int, timeout: float = 0.4) -> bool:
    """该端口上是否已有服务在**监听**（TIME_WAIT 残连不算）。

    只给 Windows 的重绑决策用：独占绑定失败后，要区分「端口真被别的进程听着」与
    「只是上次崩溃留下的 TIME_WAIT 残连」——前者必须如实报错（让用户看到中文提示），
    后者必须允许重绑（否则崩溃后几分钟内起不来）。判据就是「能不能连上」。
    """
    probe_host = "127.0.0.1" if str(host or "") in {"", "0.0.0.0", "::"} else str(host)
    try:
        with socket.create_connection((probe_host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class AppHTTPServer(ThreadingHTTPServer):
    """携带应用实例的 HTTP 服务器：RequestHandler 经 self.server.app 访问。

    **Windows 绑定语义**（这是 8765 端口加固的核心）：Python 的 ``HTTPServer`` 默认
    ``allow_reuse_address = 1``，而 Windows 的 ``SO_REUSEADDR`` 语义与 Unix 不同——
    只要先绑方没设 ``SO_EXCLUSIVEADDRUSE``，第二个进程也能绑上同一个端口且**不报错**，
    之后连接归谁不确定（表现为"刷新一下又通了"）。所以在 Windows 上改成
    「独占优先、必要时回落」：

    1. 先按 ``SO_EXCLUSIVEADDRUSE`` 独占绑定 —— 端口被占时如实抛 ``OSError``
       （由 launcher 弹中文提示 / CLI 打印指引，不再静默双绑）；
    2. 独占失败且**探不到监听者**（= 只剩 TIME_WAIT 残连）时，回落 ``SO_REUSEADDR``
       重绑 —— 保住"崩溃后立即重启仍能起来"这条既有能力。
       **禁止**把 ``allow_reuse_address`` 简单置 False 了事：那会让崩溃重启撞上
       TIME_WAIT 直接起不来（比端口冲突更难自查）。
    """

    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name != "nt" or _SO_EXCLUSIVEADDRUSE is None:
            super().server_bind()
            return
        self.socket.setsockopt(socket.SOL_SOCKET, _SO_EXCLUSIVEADDRUSE, 1)
        try:
            self.socket.bind(self.server_address)
        except OSError:
            host, port = self.server_address[0], int(self.server_address[1])
            if _port_has_listener(str(host), port):
                raise
            # 只剩 TIME_WAIT 残连：允许重绑，否则崩溃后几分钟内起不来。
            # **必须换一个新套接字**——Windows 上 SO_EXCLUSIVEADDRUSE 一旦设过就撤不干净
            # （实测：同一套接字撤掉独占再开复用，bind 直接 WinError 10013 权限拒绝）。
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = socket.socket(self.address_family, self.socket_type)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = socket.getfqdn(self.server_address[0])
        self.server_port = self.server_address[1]

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
            # app 标记见 HEALTH_APP_MARKER：launcher 靠它确认"这个 200 是我们自己的"。
            self._json({"status": "ok", "app": HEALTH_APP_MARKER, "mcp": self.app.mcp.states()})
            return
        if path.startswith("/api/") and not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return
        if path == "/api/storage/stats":
            self._json(self.app.storage.storage_usage())
        elif path == "/api/usage/stats":
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.api_usage_stats(query))
        elif path == "/api/imaging/stats":
            # 分目录字节数 + 每个 scope 最近一次清理的如实回报（含 unreachable/error）。
            self._json(self.app.api_imaging_stats())
        elif path == "/api/bootstrap":
            self._json(self.app.bootstrap())
        elif path == "/api/update":
            self._json(self.app.updater.status())
        elif path == "/api/update/notes":
            # 按需补某个版本的更新说明：静态发布页补进来的条目没有说明，点选那一版才去取。
            query = urllib.parse.parse_qs(parsed.query)
            tag = (query.get("tag") or [""])[0]
            try:
                self._json(self.app.updater.notes_for_tag(tag))
            except RuntimeError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
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
        elif path.startswith("/api/jobs/") and path.endswith("/manifest"):
            # 精确逐帧的帧清单分页读取（计划 2026-10-22 §4.3）。
            # 路径**只能**由 Job ID 解析：不接受客户端传任意文件路径（否则等于开放任意读）。
            job_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            try:
                offset = max(0, int(query.get("offset", ["0"])[0]))
                limit = max(1, min(500, int(query.get("limit", ["100"])[0])))
            except ValueError:
                self._json({"error": "offset/limit 必须是整数"}, HTTPStatus.BAD_REQUEST)
                return
            job = self.app.jobs.get(job_id)
            if not job:
                self._json({"error": "Job 不存在"}, HTTPStatus.NOT_FOUND)
                return
            owner = str(job.get("owner_session_id") or "")
            # 传了会话 id 就必须是发起该任务的会话；不传按只读详情口径放行
            # （与 /status、/events 一致：本机单用户应用，帧清单不含凭证）。
            if conversation_id and conversation_id != owner:
                self._json({"error": "无权读取该任务的帧清单"}, HTTPStatus.FORBIDDEN)
                return
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            checkpoint = job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {}
            exact_state = checkpoint.get("exact") if isinstance(checkpoint.get("exact"), dict) else {}
            manifest = str(result.get("manifest") or exact_state.get("manifest") or "")
            if not manifest:
                self._json({"error": "该任务没有可读取的帧清单"}, HTTPStatus.NOT_FOUND)
                return
            from naiba import video as video_svc

            try:
                payload = video_svc.read_exact_manifest(manifest, offset=offset, limit=limit)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            payload["job_id"] = job_id
            payload["job_status"] = str(job.get("status") or "")
            self._json(payload)
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
        elif path == "/api/aux-chats":
            # 辅助对话列表：页面重载后恢复标签（计划-辅助对话 §3.3）。
            self._json(*self.app.api_list_aux_chats())
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
            conversation = self.app.storage.get_conversation(conversation_id, include_trace=False)
            if not conversation:
                self._json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
                return
            try:
                self._json(_conv_file_open(
                    conversation, self.app.config, query.get("path", [""])[0],
                    encoding=query.get("encoding", [""])[0],
                ))
            except ConvFilePreviewError as exc:
                # 结构化预览错误（A.2）：前端按 code 渲染带操作按钮的错误卡片。
                status = {
                    "not_found": HTTPStatus.NOT_FOUND,
                    "encoding_required": HTTPStatus.UNPROCESSABLE_ENTITY,
                }.get(exc.code, HTTPStatus.BAD_REQUEST)
                self._json({"error": str(exc), "code": exc.code}, status)
            except FileNotFoundError as exc:
                self._json({"error": f"文件不存在或已被移动：{exc}"}, HTTPStatus.NOT_FOUND)
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path.startswith("/api/conversations/") and path.endswith("/file/raw"):
            conversation_id = path.split("/")[-3]
            query = urllib.parse.parse_qs(parsed.query)
            conversation = self.app.storage.get_conversation(conversation_id, include_trace=False)
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
                if query.get("download", ["0"])[0] == "1":
                    # 同上：加附件头 = 手机/桌面都能「另存为」，不加则维持内联预览。
                    self.send_header("Content-Disposition", content_disposition_attachment(target.name))
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
            conversation = self.app.storage.get_conversation(conversation_id, include_trace=False)
            if conversation and conversation.get("messages"):
                backfill_turn_choice_groups(conversation["messages"])
                # trace 是后端专用的线协议回放副本（`public/` 里零引用），却是最占体积的一份：
                # 读取侧已跳过 hydrate（include_trace=False），这里再剥一遍只是兜底
                # （存量行 metadata 里仍内联 trace 的极端形态）。
                strip_message_traces(conversation["messages"])
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
            self._serve_local_file(
                query.get("path", [""])[0],
                # ?download=1 = 让浏览器「存到本地」而不是内联预览（手机唯一的保存路径，
                # 见 §九.135 第 3 项）。默认不带 → 行为与历史逐字节一致。
                download=query.get("download", ["0"])[0] == "1",
            )
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
        elif path == "/api/files/folder-index":
            # 拖文件夹进输入区：只回一份路径清单（不上传内容）。工作区外需要一次确认
            # （口径二）——403 + needs_confirm 让前端弹框，而不是把用户堵在一句错误上。
            try:
                query = urllib.parse.parse_qs(parsed.query)
                self._json(
                    self.app.folder_index(
                        query.get("path", [""])[0],
                        query.get("conversation_id", [""])[0],
                        allow_outside=query.get("allow_outside", ["0"])[0] == "1",
                    )
                )
            except FolderConfirmRequired as exc:
                self._json(
                    {"error": str(exc), "needs_confirm": True, "path": exc.path},
                    HTTPStatus.FORBIDDEN,
                )
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/backgrounds":
            # 内置背景图清单（首次访问时幂等生成到 data/backgrounds/）。
            self._json(self.app.background_presets())
        elif path == "/api/app-icon":
            # 应用图标：状态（custom 是否生效）——重启后才会真的换掉托盘/窗口图标。
            self._json(self.app.app_icon_status())
        elif path == "/api/app-icon/image":
            self._serve_app_icon()
        elif path == "/api/install/dirs":
            self._json(self.app.list_skill_dirs())
        elif path == "/api/mcp/status/light":
            self._json({"servers": self.app.mcp.lightweight_status()})
        elif path == "/api/migration/health":
            self._json(self.app.migration_health())
        elif path == "/api/studio/catalog":
            # 工作台（Studio）预设目录：只读，返回索引摘要（条目明细走 /items）。
            self._json(*self.app.studio.api_catalog_summary())
        elif path == "/api/studio/catalog/items":
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.studio.api_catalog_items(
                module=str(query.get("module", [""])[0]),
                item_type=str(query.get("type", [""])[0]),
                query=str(query.get("q", [""])[0]),
                has_vars=str(query.get("has_vars", [""])[0]),
            ))
        elif path.startswith("/api/studio/catalog/"):
            self._json(*self.app.studio.api_catalog_item(path.rsplit("/", 1)[-1]))
        elif path == "/api/studio/custom":
            self._json(*self.app.studio.api_custom_list())
        elif path == "/api/studio/tasks":
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.studio.api_tasks(
                str(query.get("active_only", [""])[0]),
                module=str(query.get("module", [""])[0]),
                exclude_module=str(query.get("exclude_module", [""])[0]),
                exclude_modules=str(query.get("exclude_modules", [""])[0]),
            ))
        # ---- 织书台（原创写作） ----
        # 两段式硬契约与拆书匠一致：列表与详情不回整本正文；正文只有单章接口给。
        elif path == "/api/writer/books":
            self._json(*self.app.writer.api_list_books())
        elif path == "/api/writer/location":
            self._json(*self.app.writer.api_location())
        elif path.startswith("/api/writer/books/") and path.endswith("/export"):
            query = urllib.parse.parse_qs(parsed.query)
            book_id = path[len("/api/writer/books/"):-len("/export")]
            self._json(*self.app.writer.api_export_book(
                book_id,
                fmt=str(query.get("fmt", ["txt"])[0]),
                with_extras=str(query.get("extras", [""])[0]),
            ))
        elif path.startswith("/api/writer/books/") and path.endswith("/pipeline"):
            self._json(*self.app.writer.api_get_pipeline(path[len("/api/writer/books/"):-len("/pipeline")]))
        elif path.startswith("/api/writer/books/") and "/chapters/" in path:
            head, _sep, tail = path[len("/api/writer/books/"):].partition("/chapters/")
            self._json(*self.app.writer.api_get_chapter(head, tail))
        elif path.startswith("/api/writer/books/"):
            self._json(*self.app.writer.api_get_book(path[len("/api/writer/books/"):]))
        # ---- 创作场（小说改编） ----
        # 两段式硬契约：列表与详情只回索引；单章接口给基础稿件正文，
        # 其它稿件按 /manuscripts/<id> 单独取（一份原作可能有很多份改编稿）。
        elif path == "/api/novel/books":
            self._json(*self.app.novel.api_list_books())
        elif path == "/api/novel/location":
            # 数据目录与导出说明（路径由后端解析，前端不猜）。
            self._json(*self.app.novel.api_location())
        elif path.startswith("/api/novel/imports/"):
            # 导入暂存的单章正文（勾选前预览）：/api/novel/imports/<id>/chapters/<i>
            parts = path.split("/")
            if len(parts) == 7 and parts[3] == "imports" and parts[5] == "chapters":
                self._json(*self.app.novel.api_import_chapter(parts[4], parts[6]))
            else:
                self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/novel/books/") and path.endswith("/export"):
            # 整本导出：返回文本，前端落 Blob（不新增后端下载接口）。
            book_id = path[len("/api/novel/books/"):-len("/export")]
            query = urllib.parse.parse_qs(parsed.query)
            manuscripts_raw = None
            if query.get("manuscripts"):
                try:
                    manuscripts_raw = json.loads(str(query.get("manuscripts", ["{}"])[0]))
                except (ValueError, TypeError):
                    manuscripts_raw = None
            self._json(*self.app.novel.api_export_book(
                book_id,
                str(query.get("format", ["txt"])[0]),
                str(query.get("with_extras", [""])[0]),
                manuscripts_raw,
            ))
        elif path.startswith("/api/novel/books/") and path.endswith("/records"):
            # 管理记录：原文/改编稿/要素/大纲/剧本全量清单（不含正文）。
            book_id = path[len("/api/novel/books/"):-len("/records")]
            self._json(*self.app.novel.api_records(book_id))
        elif path.startswith("/api/novel/books/") and path.endswith("/script"):
            # 剧本场次列表（阶段三）。
            book_id = path[len("/api/novel/books/"):-len("/script")]
            self._json(*self.app.novel.api_list_script(book_id))
        elif path.startswith("/api/novel/books/") and "/manuscripts/" in path:
            # 单份稿件正文：/api/novel/books/<id>/chapters/<i>/manuscripts/<ms-id>
            head, _, manuscript_id = path.partition("/manuscripts/")
            book_id, _, chapter_no = head.rpartition("/chapters/")
            book_id = book_id[len("/api/novel/books/"):]
            self._json(*self.app.novel.api_get_manuscript(book_id, chapter_no, manuscript_id))
        elif path.startswith("/api/novel/books/") and "/chapters/" in path:
            head, _, chapter_no = path.partition("/chapters/")
            book_id = head[len("/api/novel/books/"):]
            self._json(*self.app.novel.api_get_chapter(book_id, chapter_no))
        elif path.startswith("/api/novel/books/"):
            self._json(*self.app.novel.api_get_book(path[len("/api/novel/books/"):]))
        elif path == "/api/labeler/presets":
            # 打标器：侧重预设 + 上限（前端只做提前提示，服务端仍逐项校验）。
            self._json(*self.app.labeler.api_presets())
        elif path == "/api/labeler/tasks/recent":
            # 必须排在通用 /api/labeler/tasks/<id> 之前，否则 recent 会被当成任务 id。
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.labeler.api_recent(str(query.get("limit", ["5"])[0])))
        elif path == "/api/labeler/tasks":
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.labeler.api_tasks(str(query.get("active_only", [""])[0])))
        elif path.startswith("/api/labeler/tasks/") and path.endswith("/events"):
            task_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.labeler.api_task_events(task_id, str(query.get("after", ["0"])[0])))
        elif path.startswith("/api/labeler/tasks/"):
            self._json(*self.app.labeler.api_task(path.rsplit("/", 1)[-1]))
        elif path == "/api/labeler/tags":
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.labeler.api_tags(str(query.get("limit", ["200"])[0])))
        elif path == "/api/labeler/annotations":
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.labeler.api_annotations(
                path_key=str(query.get("path_key", [""])[0]),
                q=str(query.get("q", [""])[0]),
                task_id=str(query.get("task_id", [""])[0]),
                limit=str(query.get("limit", ["500"])[0]),
            ))
        elif path == "/api/labeler/sentences/stats":
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.labeler.api_sentence_stats(
                q=str(query.get("q", [""])[0]),
                limit=str(query.get("limit", ["300"])[0]),
            ))
        elif path == "/api/comfyui/health":
            # 造梦机：连接测试（ComfyUI system_stats 摘要）。
            self._json(*self.app.comfyui.api_health())
        elif path == "/api/comfyui/models":
            # 造梦机：Checkpoint 模型候选（只读；不可达时空列表 + 说明）。
            self._json(*self.app.comfyui.api_models())
        elif path == "/api/comfyui/workflows":
            self._json(*self.app.comfyui.api_workflows())
        elif path.startswith("/api/comfyui/workflows/"):
            self._json(*self.app.comfyui.api_workflow(path.rsplit("/", 1)[-1]))
        elif path == "/api/comfyui/tasks":
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.comfyui.api_tasks(str(query.get("active_only", [""])[0])))
        elif path.startswith("/api/comfyui/tasks/") and path.endswith("/events"):
            # 固定后缀排在通用任务详情之前，events 不会被当成任务 id。
            task_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.comfyui.api_task_events(task_id, str(query.get("after", ["0"])[0])))
        elif path.startswith("/api/comfyui/tasks/") and "/files/" in path:
            # 产物文件：/api/comfyui/tasks/<id>/files/<name>（本地落盘字节）。
            middle = path[len("/api/comfyui/tasks/"):]
            task_id, _, name = middle.partition("/files/")
            self._serve_comfyui_task_file(task_id, name)
        elif path.startswith("/api/comfyui/tasks/"):
            self._json(*self.app.comfyui.api_task(path.rsplit("/", 1)[-1]))
        elif path.startswith("/api/studio/tasks/") and path.endswith("/events"):
            task_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed.query)
            self._json(*self.app.studio.api_task_events(task_id, str(query.get("after", ["0"])[0])))
        elif path.startswith("/api/studio/tasks/"):
            self._json(*self.app.studio.api_task(path.rsplit("/", 1)[-1]))
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
        # 打标器专用上传（原图留存 + 不去重缓存副本）：同样必须在 JSON 读取前分流。
        if path == "/api/labeler/uploads" and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            if not self._authorized(parsed):
                self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
                return
            self._upload_request(labeler=True)
            return
        # 造梦机输入媒体上传（字节保真进受管 uploads；执行任务前再转传 ComfyUI）：
        # 同样必须在 JSON 读取前分流。
        if path == "/api/comfyui/media" and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            if not self._authorized(parsed):
                self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
                return
            self._upload_request(comfyui=True)
            return
        # Agent 头像同样走 multipart（小体积、进内存），也必须在 JSON 读取前分流。
        if path == "/api/agents/avatar" and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            if not self._authorized(parsed):
                self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
                return
            self._agent_avatar_upload()
            return
        # 应用图标同样走 multipart 小体积进内存（≤5MB），也必须在 JSON 读取前分流。
        if path == "/api/app-icon" and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            if not self._authorized(parsed):
                self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
                return
            self._app_icon_upload()
            return
        # 创作场导入 TXT：文件进内存后交给小说域拆章（上限 60 MiB，与算法层同源）。
        if path == "/api/novel/import" and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            if not self._authorized(parsed):
                self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
                return
            self._novel_import_upload()
            return
        # 织书台导入启元存档：JSON 进内存后由 writer 域解析预览（上限 20 MiB）。
        if path == "/api/writer/import" and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            if not self._authorized(parsed):
                self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
                return
            self._writer_import_upload()
            return
        # Skill 导入两个端点的请求体上限跟随「Skill 大小上限」设置放宽：文件先
        # base64 编码再进 JSON（膨胀约 1/3）+ JSON 结构开销，按 ×1.5 留余量。
        # 设置 0 = 内置默认 130MB，与引入该设置前逐字一致；聊天等其他请求不动。
        if path in {"/api/skills/install", "/api/skills/install_folder"}:
            skill_mb = skill_import_body_limit_mb(self.app.config.get_skill_max_size_mb())
            skill_max_size = skill_mb * 1024 * 1024
            skill_oversize_error = (
                f"Skill 上传内容超过当前上限（{skill_mb} MB，含 base64 编码膨胀）。"
                "可在 设置 → Skills 管理 调大「Skill 大小上限」后重试"
            )
        elif path == "/api/labeler/tasks":
            # 打标批次含最多 500 条受管图片引用（每条带路径/相对路径/大小），
            # 远大于 2 MiB 通用上限；图片本体走 /api/uploads，不在这里传输。
            skill_max_size = 16 * 1024 * 1024
            skill_oversize_error = "打标批次内容过大（最多 500 张图片引用），请拆分批次后重试"
        elif path == "/api/comfyui/workflows":
            # 造梦机工作流导入：整个 API 工作流 JSON 在这里传输，上限对齐
            # 聊天域 _load_comfyui_workflow 的 20 MB 文件上限；任务提交走默认上限。
            skill_max_size = 20 * 1024 * 1024
            skill_oversize_error = "工作流内容过大（上限 20 MB），请精简后重新导出再导入"
        else:
            skill_max_size = 130 * 1024 * 1024
            skill_oversize_error = None
        body = self._read_json(max_size=skill_max_size, oversize_error=skill_oversize_error)
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
        elif path == "/api/studio/generate":
            # 工作台生成：Studio 自有异步任务（不创建普通会话，不产生 chat Run）。
            self._json(*self.app.studio.api_generate(body))
        elif path == "/api/studio/handoff":
            # 唯一跨进聊天域的路径：Agent 已显式确认后创建普通会话并提交首轮。
            self._json(*self.app.studio.api_handoff(body))
        elif path == "/api/studio/vision":
            # 图片反推：复用共享视觉能力逐图编号识别；结果只回前端，不建会话/任务。
            self._json(*self.app.studio.api_vision(body))
        elif path == "/api/studio/custom/import":
            self._json(*self.app.studio.api_custom_import(body))
        elif path == "/api/studio/custom":
            self._json(*self.app.studio.api_custom_create(body))
        # ---- 创作场（小说改编） ----
        elif path == "/api/novel/books":
            # 勾选章节入库（暂存 → 正式作品目录）；阶段一是文件处理，不建 Studio 任务。
            self._json(*self.app.novel.api_create_book(body))
        # ---- 创作场（阶段二、三）：生成走 Studio 任务队列 ----
        elif path == "/api/novel/stage":
            self._json(*self.app.novel.api_stage(body))
        elif path == "/api/novel/stage/preview":
            # 提交前预检：真实读稿、按滑条值算引用范围与 token 估算（不建任务）。
            self._json(*self.app.novel.api_stage_preview(body))
        elif path == "/api/novel/records/delete":
            # 批量删除管理记录（稿件 / 要素 / 大纲 / 剧本版本）。
            self._json(*self.app.novel.api_delete_records(str(body.get("book_id") or ""), body))
        elif path == "/api/novel/tasks/delete":
            # 删除生成记录：只清任务记录，不删从它保存的内容。
            self._json(*self.app.novel.api_delete_tasks(body))
        elif path == "/api/novel/reveal":
            # 打开作品保存目录（路径由服务端决定，不收客户端参数）。
            self._json(*self.app.novel.api_reveal())
        elif path.startswith("/api/novel/books/") and path.endswith("/apply"):
            # 把任务结果写回作品（要素 / 大纲 / 剧本 / 单章改编）
            book_id = path[len("/api/novel/books/"):-len("/apply")]
            self._json(*self.app.novel.api_apply(book_id, body))
        elif path.startswith("/api/novel/books/") and path.endswith("/manuscripts"):
            # 保存一份改编稿：POST /api/novel/books/<id>/chapters/<i>/manuscripts
            head = path[:-len("/manuscripts")]
            book_id, _, chapter_no = head.rpartition("/chapters/")
            book_id = book_id[len("/api/novel/books/"):]
            self._json(*self.app.novel.api_save_manuscript(book_id, chapter_no, body))
        elif path.startswith("/api/novel/books/") and path.endswith("/script/reorder"):
            # 场次重排：只改顺序，保留来源章节。
            book_id = path[len("/api/novel/books/"):-len("/script/reorder")]
            self._json(*self.app.novel.api_reorder_script(book_id, body))
        # ---- 织书台（原创写作） ----
        elif path == "/api/writer/books":
            self._json(*self.app.writer.api_create_book(body))
        elif path == "/api/writer/reveal":
            # 打开作品保存目录（路径由服务端决定，不收客户端参数）。
            if not self._is_local_request():
                self._json(
                    {"error": "作品文件夹在运行 Cat Chat 的电脑上，手机端打不开"},
                    HTTPStatus.FORBIDDEN,
                )
            else:
                self._json(*self.app.writer.api_reveal())
        elif path == "/api/writer/generate/preview":
            self._json(*self.app.writer.api_preview(body))
        elif path.startswith("/api/writer/import/") and path.endswith("/commit"):
            self._json(*self.app.writer.api_commit_import(
                {"import_id": path[len("/api/writer/import/"):-len("/commit")]}))
        elif path.startswith("/api/writer/books/") and path.endswith("/pipelines"):
            self._json(*self.app.writer.api_start_pipeline(
                path[len("/api/writer/books/"):-len("/pipelines")], body))
        elif path.startswith("/api/writer/books/") and path.endswith("/pipeline/cancel"):
            self._json(*self.app.writer.api_cancel_pipeline(
                path[len("/api/writer/books/"):-len("/pipeline/cancel")]))
        elif path.startswith("/api/writer/books/") and path.endswith("/pipeline/retry"):
            self._json(*self.app.writer.api_retry_pipeline(
                path[len("/api/writer/books/"):-len("/pipeline/retry")], body))
        elif path.startswith("/api/writer/books/") and path.endswith("/sync"):
            self._json(*self.app.writer.api_sync_chapters(
                path[len("/api/writer/books/"):-len("/sync")], body))
        elif path.startswith("/api/writer/books/") and path.endswith("/versions/select"):
            self._json(*self.app.writer.api_select_version(
                path[len("/api/writer/books/"):-len("/versions/select")], body))
        elif path.startswith("/api/studio/tasks/") and path.endswith("/cancel"):
            self._json(*self.app.studio.api_task_cancel(path.split("/")[-2]))
        elif path.startswith("/api/studio/tasks/") and path.endswith("/retry"):
            self._json(*self.app.studio.api_task_retry(path.split("/")[-2]))
        elif path == "/api/labeler/tasks":
            # 打标批次：受管 image_refs + 白名单 config（服务端会重新校验图片与模型）。
            self._json(*self.app.labeler.api_submit(body))
        elif path.startswith("/api/labeler/tasks/") and path.endswith("/cancel"):
            self._json(*self.app.labeler.api_cancel(path.split("/")[-2]))
        elif path.startswith("/api/labeler/tasks/") and path.endswith("/retry"):
            self._json(*self.app.labeler.api_retry(path.split("/")[-2], body))
        elif path == "/api/labeler/export":
            # 只收记录 ID / 查询条件 / 格式；落点由服务端决定（data/labeler/exports）。
            self._json(*self.app.labeler.api_export(body))
        elif path == "/api/labeler/exports/open":
            # 打开的是运行本应用的电脑上的文件夹：手机浏览器点了只会弹在看不见的 PC 上。
            if not self._is_local_request():
                self._json(
                    {"error": "导出文件夹在运行 Cat Chat 的电脑上，手机端打不开"},
                    HTTPStatus.FORBIDDEN,
                )
            else:
                self._json(*self.app.labeler.api_reveal_exports())
        elif path == "/api/labeler/uploads":
            # 打标上传走 multipart 流式（见 do_POST 开头的分流）；走到这里说明
            # 客户端发的是 JSON 等其它格式，一律拒绝。
            self._json({"error": "请以 multipart 表单上传图片"}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/comfyui/workflows":
            # 造梦机：导入工作流（API 格式校验 + 参数扫描 + 受管存储）。
            self._json(*self.app.comfyui.api_workflow_create(body))
        elif path.startswith("/api/comfyui/workflows/") and path.endswith("/params"):
            # 造梦机：保存这份工作流的主表单可见参数与显示顺序（按工作流粒度持久化）。
            self._json(*self.app.comfyui.api_workflow_params_set(path.split("/")[-2], body))
        elif path == "/api/comfyui/url":
            # 造梦机：保存 ComfyUI 地址（comfyui_url 键）并立即测试连接。
            self._json(*self.app.comfyui.api_set_url(body))
        elif path == "/api/comfyui/tasks":
            # 造梦机：提交任务（库里选工作流 + 覆盖参数 + 段数）。
            self._json(*self.app.comfyui.api_submit(body))
        elif path.startswith("/api/comfyui/tasks/") and path.endswith("/cancel"):
            self._json(*self.app.comfyui.api_cancel(path.split("/")[-2]))
        elif path.startswith("/api/comfyui/tasks/") and path.endswith("/retry"):
            self._json(*self.app.comfyui.api_retry(path.split("/")[-2]))
        elif path == "/api/comfyui/free":
            # 造梦机：请求 ComfyUI 卸载模型释放显存/内存（不打断执行中的任务）。
            self._json(*self.app.comfyui.api_free_models())
        elif path == "/api/comfyui/outputs/open":
            # 打开的是运行本应用的电脑上的文件夹：手机浏览器点了只会弹在看不见的 PC 上。
            if not self._is_local_request():
                self._json(
                    {"error": "产物文件夹在运行 Cat Chat 的电脑上，手机端打不开"},
                    HTTPStatus.FORBIDDEN,
                )
            else:
                self._json(*self.app.comfyui.api_reveal_outputs())
        elif path == "/api/aux-chats":
            # 辅助对话创建（计划-辅助对话 §3.3）：标题自动编号、权限档忽略入参强制只读。
            try:
                self._json(*self.app.api_create_aux_chat(body))
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path.startswith("/api/conversations/") and path.endswith("/branch"):
            conversation_id = path.split("/")[-2]
            message_id = str(body.get("message_id") or "")
            if not conversation_id or not message_id:
                self._json({"error": "conversation_id 和 message_id 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                # reset_agent=True = 用户选了「更换 Agent」：新会话不继承固化工具集，
                # 发送首轮前可在输入区重新选（详见 store.branch_conversation docstring）。
                result = self.app.storage.branch_conversation(
                    conversation_id, message_id, reset_agent=bool(body.get("reset_agent"))
                )
                # 与 GET /api/conversations/<id> 同口径：出网的消息不带 trace（前端只读
                # `branch_message` 预填输入框，随后自己重载新会话；这条响应曾把整份复制的
                # 历史连同 trace 一起发出去）。
                strip_message_traces((result.get("conversation") or {}).get("messages"))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(result, HTTPStatus.CREATED)
        elif path.startswith("/api/conversations/") and path.endswith("/compact"):
            # 手动整理上下文：与自动整理同一套逻辑（用该会话当前的模型发一次隐藏总结），
            # 标记落在「被整理区间最后一条消息」上，重放端据此以摘要开场。
            conversation_id = path.split("/")[-2]
            conversation = self.app.storage.get_conversation(conversation_id)
            if not conversation:
                self._json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
                return
            from naiba.core import compaction as compaction_mod
            from naiba.llm.profiles import resolve_profile_with_override

            settings = compaction_mod.resolve_settings(
                (getattr(self.app.config, "data", {}) or {}).get("auto_compaction"))
            model_key = str(conversation.get("model_key") or "")
            model_name = str(conversation.get("model_name") or "")
            if not model_key or not model_name:
                self._json({"error": "这个对话还没有记录模型，先发一条消息再整理"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                profile = resolve_profile_with_override(self.app.config, model_key, model_name)
            except (ValueError, KeyError) as exc:
                self._json({"error": f"模型配置不可用：{exc}"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                # PDF/视频处理指引按「本次实际可用工具名集合」收窄：手动整理不发请求、
                # 不建 run，没有本次 allowed_tools 快照，就用会话固化的 enabled_tool_ids
                # 走运行路径同一个 resolver（固化集为空时由 resolver 内部退回旧逻辑）。
                # 不再手拼全局 agent_tools 兜底——固化集就是该会话当时的授权事实。
                from naiba.run.session import resolve_allowed_tools
                agent = self.app.config.get_agent(str(conversation.get("agent_id") or ""))
                allowed_tools = set(resolve_allowed_tools(
                    self.app,
                    str(conversation.get("interaction_mode") or "craft"),
                    agent if isinstance(agent, dict) else {},
                    bool(conversation.get("web_search_enabled", False)),
                    model_key,
                    [str(item) for item in (conversation.get("enabled_tool_ids") or [])],
                    str(conversation.get("conv_kind") or "main"),
                ))
                result = compaction_mod.compact_conversation(
                    self.app,
                    conversation_id=conversation_id,
                    rows=list(conversation.get("messages") or []),
                    settings=settings,
                    profile=profile,
                    model_key=model_key,
                    model_name=model_name,
                    pdf_tools="read_pdf" in allowed_tools,
                    video_tools="extract_frames" in allowed_tools,
                    source="manual",
                    force=True,
                )
            except Exception as exc:  # noqa: BLE001 - 整理失败如实回报，不动对话
                self._json({"error": f"整理失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            # 不合格时把原因带回去，前端能说清「为什么没整理」。
            reason_text = {
                "region_too_small": "更早的消息太少，还没有可整理的内容",
                "region_empty": "更早的消息里没有可整理的内容",
                "freed_too_small": "摘要没有比原文更短，整理没有意义",
                "summary_too_short": "模型返回的交接总结太短，没有采用",
                "summary_too_long": "模型返回的交接总结过长，没有采用",
                "summary_refusal": "模型没有按要求输出交接总结，没有采用",
                "marker_write_failed": "保存整理结果失败",
            }.get(str(result.get("reason") or ""), "")
            if not result.get("compacted") and str(result.get("reason") or "").startswith("summary_failed"):
                reason_text = "总结请求失败，本轮没有整理"
            payload = {"ok": bool(result.get("compacted")), **result}
            if reason_text:
                payload["message"] = reason_text
            self._json(
                payload,
                HTTPStatus.CREATED if result.get("compacted") else HTTPStatus.CONFLICT,
            )
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
            conversation = self.app.storage.get_conversation(conversation_id, include_trace=False)
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
        elif path == "/api/conversations/reorder":
            # 手动排序落库：精确匹配必须放在上面的 endswith 链之后也安全——
            # "reorder" 不与任何 endswith 后缀冲突；但为了可读性仍紧邻 settings 放置。
            self._json(*self.app.api_reorder_conversations(body))
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
        elif path == "/api/providers/pricing":
            # 定价弹层专用入口：只提交 {id, kind, pricing}，不碰连接配置。
            self._json(*self.app.api_update_provider_pricing(body))
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
            # 按 scope 清理（"uploads" / "generated"）；不带 scope 时两者都清（兼容旧调用）。
            self._json(*self.app.api_clean_image_cache(str(body.get("scope") or "")))
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
                model_override = str(body.get("model") or "").strip()
                probe = str(body.get("probe") or "vision").strip().lower()
                if probe == "text":
                    self._json(self.app.vision.probe_text(selected_key, model_override))
                elif probe == "vision":
                    self._json(self.app.vision.probe(selected_key, model_override))
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
        elif path == "/api/update/cancel":
            try:
                self._json(self.app.updater.cancel_install())
            except RuntimeError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/update/apply":
            # 用户点「立即重启」：把已下载并校验过的安装包交给替换脚本。
            try:
                self._json(self.app.updater.apply_ready(on_ready=self.app.update_restart_callback))
            except RuntimeError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/update/discard":
            try:
                self._json(self.app.updater.discard_ready())
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
        elif path == "/api/app-icon":
            # 图标走 multipart 小体积进内存（见 do_POST 的分流）；走到这里说明客户端发了
            # JSON —— 明确回一句，免得前端把它当成"接口不存在"去猜。
            self._json({"error": "应用图标请以 multipart 表单上传图片"}, HTTPStatus.BAD_REQUEST)
        else:
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json()
        if body is None:
            return
        if not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return
        if path.startswith("/api/skills/") and path.endswith("/override"):
            # PATCH /api/skills/{skill_id}/override：别名/备注覆盖层（Skills 管理页改版）。
            skill_id = path[len("/api/skills/"):-len("/override")]
            self._json(*self.app.api_skill_override(skill_id, body))
            return
        if path.startswith("/api/studio/custom/"):
            self._json(*self.app.studio.api_custom_update(path.rsplit("/", 1)[-1], body))
            return
        if path.startswith("/api/novel/books/") and "/chapters/" in path:
            head, _, chapter_no = path.partition("/chapters/")
            book_id = head[len("/api/novel/books/"):]
            self._json(*self.app.novel.api_update_chapter(book_id, chapter_no, body))
            return
        # ---- 织书台（原创写作） ----
        if path.startswith("/api/writer/books/") and "/chapters/" in path:
            head, _, chapter_no = path.partition("/chapters/")
            book_id = head[len("/api/writer/books/"):]
            self._json(*self.app.writer.api_save_chapter(book_id, chapter_no, body))
            return
        if path.startswith("/api/writer/books/"):
            self._json(*self.app.writer.api_patch_book(path[len("/api/writer/books/"):], body))
            return
        if path.startswith("/api/novel/books/") and "/script/" in path:
            # 改单条场次：PATCH /api/novel/books/<id>/script/<i>
            head, _, scene_no = path.partition("/script/")
            book_id = head[len("/api/novel/books/"):]
            self._json(*self.app.novel.api_update_scene(book_id, scene_no, body))
            return
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
        if path == "/api/studio/tasks":
            # 批量删除历史产出（编辑模式多选删除）：只删记录，不动已生成文件。
            body = self._read_json()
            if body is None:
                return
            self._json(*self.app.studio.api_tasks_delete(body))
        elif path == "/api/comfyui/tasks":
            # 造梦机：批量删除历史任务（执行中跳过）；产物目录随任务一起删。
            body = self._read_json()
            if body is None:
                return
            self._json(*self.app.comfyui.api_tasks_delete(body))
        elif path == "/api/labeler/tasks":
            # 打标器：批量删除已结束任务（执行中跳过）；标注库与原图留存不受影响。
            # 不带 task_ids = 清理全部已结束（与造梦机同口径）。
            body = self._read_json()
            if body is None:
                return
            self._json(*self.app.labeler.api_tasks_delete(body))
        elif path.startswith("/api/comfyui/workflows/"):
            self._json(*self.app.comfyui.api_workflow_delete(path.rsplit("/", 1)[-1]))
        # ---- 创作场（小说改编） ----
        elif path.startswith("/api/novel/books/") and "/manuscripts/" in path:
            # 删一份稿件：DELETE /api/novel/books/<id>/chapters/<i>/manuscripts/<ms-id>
            head, _, manuscript_id = path.partition("/manuscripts/")
            book_id, _, chapter_no = head.rpartition("/chapters/")
            book_id = book_id[len("/api/novel/books/"):]
            self._json(*self.app.novel.api_delete_manuscript(book_id, chapter_no, manuscript_id))
        elif path.startswith("/api/novel/books/") and "/script/" in path:
            # 删单条场次：DELETE /api/novel/books/<id>/script/<i>
            head, _, scene_no = path.partition("/script/")
            book_id = head[len("/api/novel/books/"):]
            self._json(*self.app.novel.api_delete_scene(book_id, scene_no))
        elif path.startswith("/api/novel/books/") and "/chapters/" in path:
            head, _, chapter_no = path.partition("/chapters/")
            book_id = head[len("/api/novel/books/"):]
            self._json(*self.app.novel.api_delete_chapter(book_id, chapter_no))
        elif path.startswith("/api/novel/books/"):
            # 删作品：只作用于该作品目录，不动 Studio 历史任务记录。
            self._json(*self.app.novel.api_delete_book(path[len("/api/novel/books/"):]))
        # ---- 织书台（原创写作） ----
        elif path.startswith("/api/writer/books/") and "/chapters/" in path:
            head, _, chapter_no = path.partition("/chapters/")
            book_id = head[len("/api/writer/books/"):]
            self._json(*self.app.writer.api_delete_chapter(book_id, chapter_no))
        elif path.startswith("/api/writer/books/"):
            self._json(*self.app.writer.api_delete_book(path[len("/api/writer/books/"):]))
        elif path == "/api/app-icon":
            # 恢复默认图标（只删 custom-icon.*，内置 icon.ico 不动）。
            self._json(*self.app.api_clear_app_icon())
        elif path == "/api/tasks/clear":
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
        elif path.startswith("/api/studio/custom/"):
            self._json(*self.app.studio.api_custom_delete(path.rsplit("/", 1)[-1]))
        elif path.startswith("/api/conversations/") and path.endswith("/messages"):
            conversation_id = path.split("/")[-2]
            if self.app.storage.list_background_tasks(conversation_id, active_only=True):
                self._json({"error": "当前对话仍有运行中的任务，无法清空"}, HTTPStatus.CONFLICT)
                return
            self._json({"deleted": self.app.storage.clear_conversation_messages(conversation_id)})
        elif path.startswith("/api/compaction/"):
            # 撤销自动整理标记：旧消息全部回到模型上下文（下轮前缀缓存重预热一次）。
            message_id = path.rsplit("/", 1)[-1]
            deleted = self.app.storage.clear_compaction_marker(message_id)
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
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

    def _read_json(
        self,
        max_size: int = 2 * 1024 * 1024,
        oversize_error: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > max_size:
                self._json(
                    {"error": oversize_error or "请求内容过大"},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
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
            # 带扩展名的**资源**请求（图片 / 脚本 / 样式）不能回落成 index.html：
            # 浏览器会拿到一张 HTML 当图片用（或被 nosniff 直接拒掉），表现为静默破图，
            # 排查时很难看出是「文件不存在」而不是「路径写错」。这里明确回 404。
            # 两个例外保持原有语义：
            # - 根路径（relative == index.html）本身就是要首页；
            # - public 目录整体不存在（onefile 的 _MEI 被清）时，仍走下面的降级兜底页，
            #   否则用户在资源目录失效时只会看到一屏 404，不知道该怎么办。
            wants_asset = bool(Path(relative).suffix) and relative != "index.html"
            if wants_asset and self.app.paths.public_dir.is_dir():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            path = self.app.paths.public_dir / "index.html"
        # onefile 资源目录失效降级：exe 打包（无 COLLECT，运行目录是 %TEMP%\_MEIxxxx）
        # 在「更新重启窗口 / Temp 清理」后 _MEI 目录会被删除，进程却还活着占着端口——
        # 桌面壳资源已在内存里照常可用，而手机端此时请求 / 会 FileNotFoundError→500 JSON
        # 白屏（2026-10-17 用户手机截图实测）。这里返回内嵌的最小兜底页（不依赖任何文件），
        # 告知原因与恢复方式，而不是一屏报错。
        try:
            data = path.read_bytes()
        except OSError:
            self._serve_static_degraded(requested_path)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
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

    def _serve_static_degraded(self, requested_path: str) -> None:
        """资源目录失效时的内嵌兜底页（零文件依赖）。

        典型成因：onefile 打包的 ``%TEMP%\\_MEIxxxx`` 资源目录在自动更新重启窗口或
        Temp 清理后被删除，而服务进程还活着占着端口——桌面端不受影响（资源已在内存），
        手机/局域网页面此时全部 500。返回 503 + 一个纯内嵌 HTML：不引任何外部资源
        （此刻 js/css 都拿不到），自动重试加载，并给出明确的恢复指引。
        """
        page = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Cat Chat · 正在恢复</title>"
            "<style>body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;display:flex;"
            "align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f6f5f8;"
            "color:#3a3a44}.box{max-width:420px;padding:28px 30px;background:#fff;border-radius:14px;"
            "box-shadow:0 6px 24px rgba(0,0,0,.08);text-align:center}h1{font-size:17px;margin:0 0 10px}"
            "p{font-size:13px;line-height:1.7;margin:6px 0;color:#6b6b76}b{color:#3a3a44}</style></head>"
            "<body><div class=\"box\"><h1>Cat Chat 正在恢复</h1>"
            "<p>页面资源暂时不可用——通常是应用<b>刚完成更新</b>或正在重启。</p>"
            "<p>稍等几秒后本页会<b>自动重试</b>；若超过一分钟仍停留在此，"
            "请在<b>电脑端完全退出 Cat Chat（含托盘）再重新打开</b>，然后刷新本页。</p>"
            "</div><script>setTimeout(function(){location.reload()},5000);</script></body></html>"
        )
        body = page.encode("utf-8")
        self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Retry-After", "5")
        self.end_headers()
        self.wfile.write(body)

    def _serve_comfyui_task_file(self, task_id: str, name: str) -> None:
        """造梦机产物文件：只服务 data/comfyui/outputs/<task_id>/ 下的受管字节。"""
        path = self.app.comfyui.task_file(task_id, urllib.parse.unquote(str(name or "")))
        if path is None:
            self._json({"error": "产物文件不存在"}, HTTPStatus.NOT_FOUND)
            return
        self._serve_local_file(str(path))

    def _serve_local_file(self, source: str, download: bool = False) -> None:
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
            if download:
                self.send_header("Content-Disposition", content_disposition_attachment(Path(urllib.parse.urlparse(source).path).name))
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
        if download:
            # 只加这一个头：`Cache-Control` / `Accept-Ranges` / Range 语义一字不动
            # （视频 seek 走同一个函数，动它们就会波及播放）。
            self.send_header("Content-Disposition", content_disposition_attachment(path.name))
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

    def _novel_import_upload(self) -> None:
        """创作场导入 TXT：multipart 小体积进内存 → 小说域拆章并落导入暂存。

        上限用算法层的 `MAX_FILE_BYTES`（60 MiB）：小说 TXT 体积不大，进内存比流式
        spool 简单，也不会在 uploads 里留下需要清理的副本。
        """
        from naiba.core.novel_split import MAX_FILE_BYTES as NOVEL_MAX_BYTES

        parsed = self._read_multipart_small(NOVEL_MAX_BYTES)
        if parsed is None:
            return
        fields, data = parsed
        # 文件名在 multipart 头里是 latin-1 字节流；`_read_multipart_small` 只回字段
        # 字典，文件名由前端额外放在 filename 字段里（避免二次编码猜测）。
        filename = str(fields.get("filename") or fields.get("name") or "未命名.txt")
        self._json(*self.app.novel.api_import(
            filename=filename, data=data, max_len=fields.get("max_len"),
        ))

    def _writer_import_upload(self) -> None:
        """织书台导入启元存档：multipart 小体积进内存 → writer 域解析预览。"""
        from naiba.writer.store import MAX_IMPORT_BYTES

        parsed = self._read_multipart_small(MAX_IMPORT_BYTES)
        if parsed is None:
            return
        fields, data = parsed
        filename = str(fields.get("filename") or fields.get("name") or "作品存档.json")
        self._json(*self.app.writer.api_import(filename=filename, data=data))

    def _app_icon_upload(self) -> None:
        """应用图标上传（multipart 小体积进内存 → storage/app_icon 归一化落盘）。"""
        parsed = self._read_multipart_small(APP_ICON_MAX_BYTES)
        if parsed is None:
            return
        _fields, data = parsed
        self._json(*self.app.api_set_app_icon(data))

    def _serve_app_icon(self) -> None:
        """当前生效图标的 PNG（弹窗预览用）：no-store —— 换/换回图标后刷新即见新图。"""
        payload, status = self.app.api_read_app_icon()
        if status != HTTPStatus.OK or not isinstance(payload, (bytes, bytearray)):
            self._json(payload if isinstance(payload, dict) else {"error": "图标不可用"}, status)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

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

    def _upload_request(self, labeler: bool = False, comfyui: bool = False) -> None:
        """multipart/form-data 流式上传：解析到临时 spool 后交给 app 落盘。

        设计：python_multipart 流式回调（不整包进内存），文件 part 字节直接写
        spool（uploads 根下的 .part），完成后按入口分流：通用上传走
        ``_upload_spooled``（去重 + 按设置压缩）；打标器上传走
        ``_upload_labeler_spooled``（原图留存 + 不去重）。
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
            if comfyui:
                self._json(*self.app._upload_comfyui_spooled(spool_path, state["filename"]))
            elif labeler:
                self._json(*self.app._upload_labeler_spooled(spool_path, state["filename"]))
            else:
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
        # 关流判据（曾经只看"状态已终态"，于是偶发整轮收不到 done）：
        # 状态置终态与终态事件落库**不是一次原子写**——收尾路径先 `update_background_task`
        # 再 emit done/cancelled/error（见 run/chat.py）。若流线程恰好在这个窗口里检查，
        # 就会"状态已终态 + 还没有新事件"→ break，客户端从头到尾没收到终态事件（前端还能
        # 靠收尾重载会话兜住，但恢复/重放路径就丢了收尾信号）。判据改成三条：
        #   ① 本连接已送达终态事件 → 立即关流（终态之后不再有新事件，前端收到即停止轮询）；
        #   ② 状态已终态、且该 run 的终态事件**早已在游标之下**（客户端此前已拿到）→ 关流；
        #   ③ 状态已终态但终态事件还没落库 → 给一小段 grace 等它出现，超时才按原行为关流
        #      （进程崩溃/被中断清理的 run 可能真的没有终态事件）。
        terminal_sent = False
        grace_deadline = 0.0
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
                    if event.get("type") in self.app.runs.TERMINAL_EVENT_TYPES:
                        terminal_sent = True
                if terminal_sent:
                    break
                current = self.app.runs.get(run_id)
                if not current:
                    break
                if current.get("status") in self.app.runs.TERMINAL:
                    if self.app.runs.events_after(run_id, sequence):
                        continue
                    if self.app.runs.terminal_event_sequence(run_id):
                        break
                    if grace_deadline == 0.0:
                        grace_deadline = time.monotonic() + TERMINAL_EVENT_GRACE_SECONDS
                    if time.monotonic() < grace_deadline:
                        time.sleep(0.05)
                        continue
                    break
                grace_deadline = 0.0
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
    try:
        server = AppHTTPServer((host, port), RequestHandler, APP)
    except OSError as exc:
        # 端口被占/无权限绑定：CLI 下原本甩一条英文 traceback（普通用户看不懂）。
        # 改成与桌面版同款中文指引 + 非零退出码（见 core.network.port_conflict_message）。
        print(port_conflict_message(port, host=host, detail=str(exc)), file=sys.stderr)
        raise SystemExit(2) from exc
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
