# -*- coding: utf-8 -*-
"""组装根：NaibaChatApp（层级 3/4，唯一允许组装全部子模块的对象）。

自 server.py 整类迁入（2026-09-06，3.4.2-②）；路径经 PathContext 注入，
本模块不 import server（哲学② DAG 红线）。
"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from http import HTTPStatus
from pathlib import Path
from typing import Any

import naiba.net as net_io
from naiba.capability import CapabilityRuntime
from naiba.config import (
    ConfigStore, VALID_MODEL_KINDS, _infer_kind_for_request_format,
    built_in_agent_ids, validate_skills_dir,
)
from naiba.core.cards import parse_sillytavern_card
from naiba.core.migration import (
    _copy_legacy_data, _database_has_conversations, _merge_data_tree,
    _sync_bundled_skills, migrate_legacy_data,
)
from naiba.core.media_types import media_exts_payload
from naiba.core.network import network_access_status
from naiba.core.conv_files import (
    FolderConfirmRequired,
    _conv_file_target,
    _conv_workspace_root,
    browse_workspace_tree,
    folder_index,
)
from naiba.core.paths import path_within
from naiba.jobs import JobRegistry
from naiba.llm.provider_presets import apply_preset_values, provider_preset_key_url, provider_presets_payload
from naiba.llm.runtime import ModelRuntime
from naiba.mcp import MCPRegistry
from naiba.paths import PathContext, default_path_context
from naiba.plans import PlanManager
from naiba.run.manager import ConversationRunManager
from naiba.search import WebSearchRuntime
from naiba.skills.catalog import SkillCatalog, _frontmatter_value
from naiba.skills.install import (
    _unique_dir, _zip_has_skill_md, clear_skill_recycle, delete_skill, list_skill_recycle,
    remove_skill_references, restore_skill_recycle_entry,
)
from naiba.storage.backgrounds import ensure_background_presets, is_backgrounds_path
from naiba.storage.media import (
    _clean_uploads_cache, _process_uploaded_image, _uploads_total_bytes, auto_clean_uploads,
    is_uploads_path, missing_cache_attachment, remove_uploaded_file, rotate_uploaded_image,
    store_uploaded_file,
)
from naiba.storage.app_icon import (
    clear_app_icon, has_custom_app_icon, read_app_icon_png, store_app_icon,
)
from naiba.storage.avatars import read_agent_avatar, store_agent_avatar
from naiba.storage.media_collect import MediaCollector
from naiba.storage.job_media import JobMediaWriter
from naiba.storage.store import ChatStorage
from naiba.subagent import run_subagent_agent
from naiba.tools.executor import ToolExecutor
from naiba.tools.registry import build_tool_registry
from naiba.updater import UpdateManager
from naiba.vision.runtime import VisionRouter

logger = logging.getLogger("naiba.app")


def _query_first(params: dict[str, Any], name: str) -> str:
    """取 query string 里的首个值（``parse_qs`` 的原生形态是 dict[str, list[str]]）。

    放模块级而非 app 方法：它是纯函数，且测试可以用轻量 stub app 直接跑路由层。
    """
    value = params.get(name)
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


class NaibaChatApp:
    def __init__(self, paths: PathContext | None = None):
        self._paths = paths or default_path_context()
        # 公开访问别名（同对象引用：rebind_data_dir 会就地更新字段）
        self.paths = self._paths
        # 冻结版首次启动：从 EXE 相邻旧目录迁移配置与数据到 %LOCALAPPDATA%\NaibaChat。
        initial_data_dir = self._paths.data_dir.resolve()
        self.data_migration = migrate_legacy_data(self._paths)
        self.config = ConfigStore(self._paths.config_path, paths=self._paths)
        # 统一网络代理策略：优先用 config 的 proxy 字段；旧配置未含该字段时
        # 保持历史兼容行为（跟随系统代理），直到用户在运行设置中显式保存。
        net_io.configure(self.config.data.get("proxy"))
        self.listener_host = str(self.config.data.get("host", "0.0.0.0"))
        # 会话级「允许读取工作区外文件夹」的确认记录（口径二，见 core.conv_files.FolderConfirmRequired）。
        # 只存内存：它表达的是"这一次对话里用户点过同意"，进程重启后重新问一次没有损失。
        self._confirmed_folders: dict[str, set[str]] = {}
        # ConfigStore may reveal a custom data directory after the legacy
        # bootstrap migration has already run. Rebind all runtime globals and
        # carry over the bootstrap directory so the same process reads the
        # directory the user configured.
        configured_data_dir = self.config.resolve_data_dir().resolve()
        if configured_data_dir != initial_data_dir:
            try:
                if (
                    initial_data_dir.is_dir()
                    and not path_within(configured_data_dir, initial_data_dir)
                    and not path_within(initial_data_dir, configured_data_dir)
                ):
                    self.data_migration["data"] = (
                        _merge_data_tree(initial_data_dir, configured_data_dir)
                        or bool(self.data_migration.get("data"))
                    )
            except OSError as exc:
                print(f"Data directory switch migration failed: {exc}")
            self._paths.rebind_data_dir(configured_data_dir)
        self._paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = ChatStorage(self._paths.data_dir / "chat.db")
        # 媒体采集器（工具产出点按声明提取 + 托管缓存）：data_dir/imaging 每次调用实时解析。
        self.media_collector = MediaCollector(self.config, self._paths)
        # 异步 Job 产物写回（终态时挂回发起它的助手消息；用同一采集器）。
        self.job_media_writer = JobMediaWriter(self.storage, self.config, self._paths, self.media_collector)
        # 统一事件总线：run/job 共用单点「写事件 + 唤醒」，装配根出口（阶段 2）。
        from naiba.events import EventBus

        self.event_bus = EventBus(self)
        repaired_bindings = self.storage.synchronize_workspace_bindings(
            self.config.workspace_bindings()
        )
        if repaired_bindings:
            print(f"Repaired {repaired_bindings} conversation workspace binding(s)")
        self.models = ModelRuntime()
        # Keep packaged Skills in the persistent managed directory as well.
        # A one-file executable extracts bundled assets to a temporary folder;
        # without this copy, replacing the executable can make a Skill that
        # existed in the previous build disappear from the user's catalog.
        bundled_skills = (self._paths.resource_dir / "skills").resolve()
        # 托管 Skills 目录位于数据目录内（self._paths.data_dir/skills），不再固定于 C 盘 self._paths.app_dir。
        managed_skills = self.config.resolve_managed_skills_dir()
        # 启动幂等合并：把旧版「数据目录同级 skills」与旧版「self._paths.app_dir/skills」中的
        # 用户自定义 Skill 合并进新托管目录（目标已有文件优先，不删除旧目录）。
        # 内置 Skills 由下方 _sync_bundled_skills 单独同步，这里跳过以免覆盖打包版本。
        for legacy_src in (
            (self._paths.data_dir.parent / "skills").resolve(),
            (self._paths.app_dir / "skills").resolve(),
        ):
            if legacy_src.is_dir() and legacy_src != managed_skills:
                try:
                    _merge_data_tree(legacy_src, managed_skills, self._hidden_relative_roots(legacy_src))
                except OSError as exc:
                    print(f"Merging legacy Skills failed: {exc}")
        if bundled_skills.is_dir() and bundled_skills != managed_skills:
            try:
                _sync_bundled_skills(
                    bundled_skills, managed_skills, self._hidden_relative_roots(bundled_skills)
                )
            except OSError as exc:
                print(f"Persisting bundled Skills failed: {exc}")
        # 兼容旧版单文件导入：它把 SKILL.md 直接写在托管目录根下，导致该 Skill 的
        # root 等于整个托管目录。启动时将定义迁入自己的目录，并同步稳定 ID 的配置引用。
        self._migrate_bare_managed_skill(managed_skills)

        skills_dirs: list[str] = []
        skills_sources: list[str] = []
        if bundled_skills.is_dir():
            skills_dirs.append(str(bundled_skills))
            skills_sources.append("builtin")
        if managed_skills.is_dir() and managed_skills != bundled_skills:
            skills_dirs.append(str(managed_skills))
            skills_sources.append("managed")
        # 旧配置默认 `skills_dirs: ["skills"]` 解析为 self._paths.app_dir/skills；重定向到新托管目录，
        # 保证旧配置/旧 Skill 不丢且不再写回 C 盘。
        legacy_managed = (self._paths.app_dir / "skills").resolve()
        for raw in self.config.data.get("skills_dirs", []):
            try:
                resolved = self.config._resolve_dir(str(raw))
                if resolved == legacy_managed:
                    resolved = managed_skills
                validate_skills_dir(resolved, app_dir=self._paths.app_dir, public_dir=self._paths.public_dir, data_dir=self._paths.data_dir)
                if str(resolved) not in skills_dirs:
                    skills_dirs.append(str(resolved))
                    skills_sources.append("external")
            except ValueError:
                logger.warning("已忽略不安全的 Skill 目录：%s", raw)
        self.catalog = SkillCatalog(
            [Path(path) for path in skills_dirs],
            base_dir=self._paths.app_dir,
            hidden_ids=self.config.get_hidden_skill_ids(),
            package_dir=bundled_skills,
            sources=skills_sources,
        )
        self.mcp = MCPRegistry(self.config.data.get("mcp_servers", []))
        self.executor = ToolExecutor(
            self.config.resolve_workspace_dir(),
            sys.executable,
            int(self.config.data.get("command_timeout", 120)),
            self.mcp,
            permission_mode=self.config.data.get("permission_mode", "confirm"),
            mcp_register=self.register_mcp_server,
        )
        self.plans = PlanManager(self)
        self.runs = ConversationRunManager(self)
        self.tasks = self.runs
        # Harness 级统一工具系统与 Job Registry
        from naiba.tools.registry import build_tool_registry
        from naiba.jobs import JobRegistry

        self.tool_registry = build_tool_registry()
        self.tool_registry.bind_executor(self.executor)
        self.tool_registry.bind_mcp(self.mcp)
        # 权限同源（Phase 2）：引擎从注册表解析 def 级 policy/元数据；别名经查询层归一
        self.executor.set_def_resolver(self.tool_registry.get)
        self.executor.set_alias_resolver(self.tool_registry.resolve)
        # core 域 Provider（Phase 3 双轨）：def 绑定新实现函数；引擎仍走旧 _tool_* 方法，行为不变
        from naiba.tools.providers.core import CoreToolProvider, ToolContext

        core_tool_context = ToolContext(
            workspace=self.config.resolve_workspace_dir(),
            python_executable=sys.executable,
            command_timeout=int(self.config.data.get("command_timeout", 120)),
            mcp_registry=self.mcp,
            mcp_register=self.register_mcp_server,
            # 宿主数据目录（动态）：uploads/generated 作为读取可信根（用户上传附件免确认）。
            data_dir_getter=lambda: self._paths.data_dir,
            # 本会话「用户已点过允许」的工作区外目录（动态）：读取类工具免确认的追加根。
            # 同源即 folder_index 的确认记录（用户级授权只有一个事实）；写策略不读它。
            confirmed_read_roots_getter=self.confirmed_read_roots,
            # reset_context 需要把「当前还在跑的后台任务」快照进分割线（种子消息里提醒模型）。
            extra={"active_background_tasks": self._active_background_tasks},
        )
        self.core_tools = CoreToolProvider(core_tool_context)
        self.tool_registry.register_provider(self.core_tools)
        # documents 域 Provider（PDF 工具）：read_pdf/pdf_render_pages/pdf_zoom_region
        # 单一定义；缓存目录按当前数据目录动态获取（防 rebind 漂移）。
        from naiba.tools.providers.documents import DocumentToolProvider

        self.tool_registry.register_provider(
            DocumentToolProvider(core_tool_context, lambda: self._paths.data_dir)
        )
        # video 域 Provider（视频抽帧）：probe_video/extract_frames 单一定义；
        # 帧图缓存走 uploads/video_frames（被 /api/imaging/clean 与统计自动覆盖）。
        from naiba.tools.providers.video import VideoToolProvider

        self.tool_registry.register_provider(
            VideoToolProvider(core_tool_context, lambda: self._paths.data_dir)
        )
        self.jobs = JobRegistry(self)
        # MCP 生命周期：工具发现后注册到统一工具表，断开/注销时清理
        self.mcp.on_tools_discovered = self.tool_registry.register_mcp_tools
        self.mcp.on_tools_deregistered = self.tool_registry.deregister_mcp_tools
        self.mcp.register_tools_into(self.tool_registry)
        # MCP uses demand-driven lifecycle.  Configured services stay stopped
        # until a run explicitly needs them or calls an MCP tool.
        from naiba.subagent import (
            run_subagent_agent,
        )
        self.jobs.agent_runner = lambda jid, spec, cancel, emit: run_subagent_agent(
            self, jid, spec, cancel, emit
        )
        # job/subagent 域 Provider（Phase 4）：8 个任务工具单一定义（def.execute + system）
        from naiba.tools.providers.jobs import JobToolProvider

        self.tool_registry.register_provider(JobToolProvider(self))
        # comfyui 域 Provider（Phase 4）：工作流检查/批量作业单一定义
        from naiba.tools.providers.comfyui import ComfyUIProvider

        self.tool_registry.register_provider(ComfyUIProvider(self))
        from naiba.capability import CapabilityRuntime

        self.capabilities = CapabilityRuntime(self)
        # capability 域 Provider（Phase 4）：Skill 安装/解压/定位工具单一定义
        from naiba.tools.providers.capability import CapabilityToolProvider

        self.tool_registry.register_provider(CapabilityToolProvider(self.capabilities))
        # 视觉运行时：注册 2 个视觉工具处理器。文本大脑发图只留路径占位，由模型按需调用 vision_analyze。
        from naiba.vision.runtime import VisionRouter

        self.vision = VisionRouter(self)
        # vision 域 Provider（Phase 4）：8 个视觉工具单一定义
        from naiba.tools.providers.vision import VisionToolProvider

        self.tool_registry.register_provider(VisionToolProvider(self.vision))
        # 联网搜索运行时（PLAN4 §联网搜索）：搜索开关开启且 provider 可用时才被加入 allowed_tools。
        from naiba.search import WebSearchRuntime

        self.web_search = WebSearchRuntime(self)
        # search/recall 域 Provider（Phase 4）：web_search/recall_history 单一定义
        from naiba.tools.providers.search import SearchRecallProvider

        self.tool_registry.register_provider(SearchRecallProvider(self.web_search, self.storage))
        # Storage marks in-flight runs as interrupted on startup. Deterministic
        # jobs that explicitly opted into resume are safely re-created from
        # their durable checkpoint and persisted parameters.
        self.jobs.resume_interrupted()
        # 重启清理只把 in-flight 的对话 Run 标成 interrupted；已经流式输出过的正文只存在
        # 事件流里，不重建就会表现为「那条回复凭空消失，刷新/重启都不回来」。这里把它
        # 落库成一条带「未完成」标记的 partial 消息（失败不影响启动）。
        try:
            self.runs.recover_interrupted_runs()
        except Exception:  # noqa: BLE001
            logger.exception("中断轮次恢复失败")
        self.updater = UpdateManager(self._paths.app_dir, self._paths.data_dir)
        self.update_restart_callback = None
        # 后台自动连接所有已启用 MCP 服务；对启动时未连上的做周期重试，
        # 保证 MCP 工具在会话固化工具集之前就绪（否则新会话烘焙不到它们）。
        self._start_mcp_background()

    def stop(self) -> None:
        self.runs.shutdown()
        self.plans.shutdown()
        self.jobs.shutdown()
        self.mcp.stop()

    def register_mcp_server(self, values: dict[str, Any]) -> dict[str, Any]:
        config = self.config.upsert_mcp_server(values)
        return {"saved": True, "server": self.mcp.upsert(config)}

    def remove_mcp_server(self, server_id: str) -> dict[str, Any]:
        """Persistently delete a registered MCP server and its live connection."""
        if self.config.delete_mcp_server(server_id):
            self.mcp.remove(server_id)
        else:
            self.mcp.remove(server_id)  # 配置里可能已缺，仍尝试清理运行态
        return {"removed": True, "server_id": server_id}

    def pick_workspace_directory(self, initial: str = "") -> dict[str, Any]:
        """Open a Windows native folder picker; return an empty path if cancelled."""
        initial_path = str(initial or self.config.resolve_workspace_dir())
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                parent=root,
                initialdir=initial_path if Path(initial_path).is_dir() else str(self._paths.exe_dir),
                title="选择 Cat Chat 工作区目录", mustexist=False,
            )
            root.destroy()
        except Exception as exc:
            raise RuntimeError(f"无法打开 Windows 原生目录选择器：{exc}") from exc
        selected = str(selected or "").strip()
        if not selected:
            return {"cancelled": True, "path": ""}
        resolved = self.config.resolve_workspace_dir(selected)
        self.config.ensure_workspace_writable(resolved)
        return {"cancelled": False, "path": selected, "resolved": str(resolved)}

    def browse_workspace(self, raw: str = "", conversation_id: str = "") -> dict[str, Any]:
        """Return a shallow, read-only project tree limited to a workspace root.

        传入 ``conversation_id`` 时以该会话的工作区为根（输入框 @ 引用文件/目录），
        并隐藏点号条目；不传时为全局默认工作区（设置页目录树，保持原行为）。
        """
        conversation: dict[str, Any] | None = None
        if str(conversation_id or "").strip():
            conversation = self.storage.get_conversation(str(conversation_id).strip())
            if not conversation:
                raise LookupError("对话不存在")
        root = _conv_workspace_root(conversation, self.config)
        if root is None:
            raise ValueError("工作区不可用")
        self.config.ensure_workspace_writable(root)
        return browse_workspace_tree(root, raw, hide_dotfiles=conversation is not None)

    # ---- 拖入文件夹的路径索引（"让模型知道这个目录里有什么"，不传内容） ----
    @staticmethod
    def _folder_key(path: Path) -> str:
        """确认记录的归一键：分隔符统一 + 去尾斜杠 + 忽略大小写（Windows 语义）。"""
        return str(path).replace("\\", "/").rstrip("/").lower()

    def _confirmed_folder_paths(self, conversation_id: str) -> set[str]:
        return self._confirmed_folders.get(str(conversation_id or ""), set())

    def confirmed_read_roots(self, conversation_id: str) -> list[Path]:
        """本会话用户已点过「允许」的工作区外目录（读取类工具的免确认根）。

        与 ``folder_index`` 的确认记录**同一个集合**：判定与执行同源，不为读取再存一份
        （否则同一事实两处记录必然漂移）。空会话 id 一律返回空——确认记录按会话隔离，
        没有会话就没有可继承的授权。

        仅供读取类权限策略消费（``core.ToolContext.confirmed_read_roots_getter``）；
        **写策略不读它**，写工作区外仍照旧逐次确认。
        """
        roots: list[Path] = []
        for key in self._confirmed_folder_paths(conversation_id):
            try:
                roots.append(Path(key))
            except (OSError, ValueError):
                continue
        return roots

    def folder_index(self, raw_path: Any, conversation_id: str = "", allow_outside: bool = False) -> dict[str, Any]:
        """扫一个文件夹的索引（只读、不读文件内容）。

        路径口径（计划 B.5 口径二）：会话工作区内的路径直接放行；工作区外的路径**必须**
        先在本会话里确认过一次（``allow_outside=True`` 表示"用户已在界面上点过同意"，
        确认按会话记住）。这样"拖个素材目录进来"不用拷进工作区，但也不会在用户没表态时
        静默扫描工作区外的任意目录。拒绝时抛 ``FolderConfirmRequired``（HTTP 层转成可操作的 403）。
        """
        conversation: dict[str, Any] | None = None
        if str(conversation_id or "").strip():
            conversation = self.storage.get_conversation(str(conversation_id).strip())
            if not conversation:
                raise LookupError("对话不存在")
        root = _conv_workspace_root(conversation, self.config)
        target = _conv_file_target(raw_path, root)
        if target is None:
            raise ValueError("文件夹路径无效")
        if not target.exists() or not target.is_dir():
            raise ValueError("目录不存在或已被移动")
        if not (root and path_within(target, root)):
            key = self._folder_key(target)
            if key not in self._confirmed_folder_paths(conversation_id):
                if not allow_outside:
                    raise FolderConfirmRequired(str(target))
                self._confirmed_folders.setdefault(str(conversation_id or ""), set()).add(key)
        return folder_index(target)

    def folder_indexes_for_send(self, conversation_id: str, folders: list[Any]) -> list[dict[str, Any]]:
        """**发送那一刻**生成索引快照（`submit_chat` 用）。

        刻意不在发送时复用前端手上的旧索引：用户在拖进来之后可能又往目录里放了图，
        快照以"真正送进模型的那一刻"为准，之后目录再变也不刷新（不做监听）。
        """
        result: list[dict[str, Any]] = []
        for item in folders or []:
            raw = str(item.get("path") if isinstance(item, dict) else item or "").strip()
            if not raw:
                continue
            try:
                result.append(self.folder_index(raw, conversation_id))
            except FolderConfirmRequired as exc:
                raise ValueError(
                    f"文件夹不在会话工作区内，且本会话尚未确认允许读取：{exc.path}"
                ) from exc
        return result

    def _start_mcp_background(self) -> None:
        """应用启动后在后台连接所有已启用 MCP 服务，并保持到退出。

        在独立线程里做（不阻塞启动），并对启动时未连上的服务周期重试，
        让"naiba-chat 先启动、MCP/ComfyUI 稍后才上线"的场景也能自动补连。
        """
        def _worker() -> None:
            try:
                self.mcp.start()  # 置 _persistent=True 并启动所有连接
            except Exception as exc:  # 单个服务启动失败不应中断其他服务
                logger.warning("MCP 后台启动部分失败：%s", exc)
            self.mcp.retry_unconnected_until_stopped()
        threading.Thread(target=_worker, name="naiba-mcp-background", daemon=True).start()

    def test_mcp_server(self, server_id: str) -> dict[str, Any]:
        """返回指定 MCP 的 stdio 状态，并对 ComfyUI 额外探测 HTTP 可达性。"""
        connection = self.mcp.connection(server_id)
        if not connection:
            raise ValueError(f"未注册的 MCP 服务：{server_id}")
        state = connection.state()
        if server_id == "comfy-mcp":
            address = (connection.env or {}).get("COMFYUI_SERVER_ADDRESS") or "http://127.0.0.1:8188"
            try:
                import urllib.request

                req = urllib.request.Request(f"{address.rstrip('/')}/", method="HEAD")
                with net_io.open(req, timeout=3) as resp:
                    state["comfyui_reachable"] = 200 <= resp.status < 500
            except Exception as exc:
                state["comfyui_reachable"] = False
                state["comfyui_error"] = str(exc)
        return state

    def reconnect_mcp_server(self, server_id: str) -> dict[str, Any]:
        """强制重连指定 MCP 服务：先停止再启动，返回最新状态。"""
        connection = self.mcp.connection(server_id)
        if not connection:
            raise ValueError(f"未注册的 MCP 服务：{server_id}")
        connection.stop()
        connection.start(timeout=20)
        return connection.state()

    def import_legacy_data(self, source: Path) -> dict[str, Any]:
        """从用户指定的旧数据目录导入 config.json、data/ 与 Skills（保留目标已存在数据）。"""
        source = Path(source)
        legacy_config = source / "config.json"
        legacy_data = source / "data"
        if not legacy_config.is_file() and not legacy_data.is_dir():
            raise ValueError("旧数据目录中没有 config.json 或 data/")
        report = _copy_legacy_data(source, self._paths)
        if report["config"]:
            # 重新加载配置，使导入的模型/MCP 立即生效。
            self.config = ConfigStore(self._paths.config_path, paths=self._paths)
        return report

    def bootstrap(self) -> dict[str, Any]:
        access = network_access_status(
            getattr(self, "listener_host", str(self.config.data.get("host", "0.0.0.0"))),
            int(self.config.data["port"]),
        )
        return {
            "settings": self.config.public(),
            "providers": self.config.public_providers(),
            "model_profiles": self.config.model_profiles(),
            "default_model_key": self.config.default_model_key(),
            "skills": self.catalog.scan(),
            "mcp_servers": self.mcp.states(),
            "agents": self.config.public_agents(),
            "default_agent_id": self.config.default_agent_id(),
            "tool_sets": self.config.get_tool_sets(),
            "workspaces": self.config.data.get("workspaces", []),
            "image_cache_bytes": _uploads_total_bytes(self._paths.data_dir),
            # 媒体扩展名与分桶上限的唯一来源（core/media_types.py）：前端不再各写
            # 一份正则，消除"前后端名单漂移"导致的产物静默消失/破图。
            "media_exts": media_exts_payload(),
            **access,
            "lan_restart_required": str(self.config.data.get("host", "0.0.0.0")) != self.listener_host,
            "update": self.updater.status(),
            "data_location": {
                "is_frozen": bool(getattr(sys, "frozen", False)),
                "data_dir": str(self._paths.data_dir),
                "config_path": str(self._paths.config_path),
                "exe_dir": str(self._paths.exe_dir),
                "migration": self.data_migration,
            },
            "data_migration": self.migration_health(),
            "resolved_workspace_dir": str(self.config.resolve_workspace_dir()),
            "proxy_state": net_io.proxy_state(),
        }

    def _active_background_tasks(self, conversation_id: str) -> list[dict[str, Any]]:
        """某会话仍在跑的后台任务（reset_context 写分割线时快照，种子消息里提醒模型）。"""
        try:
            return self.storage.list_background_tasks(conversation_id, active_only=True) or []
        except Exception:  # noqa: BLE001 - 快照失败不能阻断重置
            logger.exception("读取后台任务失败（reset_context 快照忽略）")
            return []

    def list_skill_dirs(self) -> dict[str, Any]:
        configured = self.config.get_skills_dirs()
        managed = self.config.resolve_managed_skills_dir()
        legacy_managed = (self._paths.app_dir / "skills").resolve()
        resolved = []
        for raw in configured:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = (self._paths.app_dir / path).resolve()
            path = path.resolve()
            # 旧默认 `skills` 解析为 self._paths.app_dir/skills，重定向到新的托管目录。
            if path == legacy_managed:
                path = managed
            resolved.append(str(path))
        # 托管目录始终作为唯一持久化入口，即便未显式配置也纳入返回。
        if str(managed) not in resolved:
            resolved.insert(0, str(managed))
        return {"configured": configured, "resolved": resolved}

    # ---- 数据与迁移（PLAN7 §数据与迁移） ----
    def migration_health(self) -> dict[str, Any]:
        """返回数据库版本、健康状态、已应用迁移与备份位置。"""
        integrity = self.storage.check_integrity()
        version = self.storage.get_user_version()
        return {
            "db_version": version,
            "data_dir": str(self.storage.data_dir),
            "configured_data_dir": str(self.config.resolve_data_dir()),
            "restart_required": self.config.resolve_data_dir() != self.storage.data_dir.resolve(),
            "healthy": bool(integrity.get("ok")),
            "integrity_details": integrity.get("details", []),
            "applied_versions": [version],
            "backup_location": str(self._paths.data_dir / "backups"),
            "resolved_skills_dirs": [str(path) for path in self.config.skills_dirs_resolved()],
        }

    def migration_backup(self) -> dict[str, Any]:
        """迁移前备份数据库及其 WAL/SHM  siblings。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = self._paths.data_dir / "backups" / f"migration-{stamp}"
        return self.storage.backup_for_migration(backup_dir)

    def migration_run(self) -> dict[str, Any]:
        """执行待应用的数据迁移；执行前禁止存在活动 Run。"""
        if self.storage.list_background_tasks(active_only=True):
            return {"ok": False, "error": "存在活动的 Run，请先等待其完成或取消后再执行迁移"}
        try:
            self.storage.apply_pending_migrations()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"迁移失败：{exc}", **self.migration_health()}
        return {"ok": True, **self.migration_health()}

    def migration_move_data(self, body: dict[str, Any]) -> dict[str, Any]:
        """Copy the current data directory to a new location and switch on restart."""
        if self.storage.list_background_tasks(active_only=True):
            return {"ok": False, "error": "存在活动任务，请先等待完成或取消后再迁移数据"}
        target = self.config.resolve_data_dir(str(body.get("data_dir") or "data"))
        self.config.ensure_data_dir_writable(target)
        source = self.storage.data_dir.resolve()
        if target == source:
            return {"ok": True, "message": "数据目录未改变", **self.migration_health()}
        if path_within(target, source) or path_within(source, target):
            return {"ok": False, "error": "目标数据目录不能是当前数据目录的父目录或子目录"}
        existing_db = target / "chat.db"
        if existing_db.exists() and _database_has_conversations(existing_db):
            return {"ok": False, "error": f"目标目录已有对话数据：{target}"}
        # Skill 托管目录跟随数据目录：源为当前实际运行数据目录（storage.data_dir）
        # 内的 skills（或旧版同级 skills、旧版 C 盘 self._paths.app_dir/skills），目标为目标 data_dir 内 skills。
        old_managed = (source / "skills").resolve()
        old_sibling_managed = (source.parent / "skills").resolve()
        new_managed = (target / "skills").resolve()
        legacy_managed = (self._paths.app_dir / "skills").resolve()
        try:
            with self.storage._connect() as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # 结构迁移：先确保当前库为最新 schema，复制后新目录即携带最新结构。
            self.storage.apply_pending_migrations()
            target.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                if item.name in {"server.lock", "backups"}:
                    continue
                if item.is_dir():
                    _merge_data_tree(item, target / item.name)
                elif not (target / item.name).exists():
                    shutil.copy2(item, target / item.name)
            # 搬迁托管 Skills 目录（旧 managed / 旧同级 managed / 旧 self._paths.app_dir/skills → 新 managed）。
            old_managed_sources = []
            for candidate in (old_managed, old_sibling_managed, legacy_managed):
                if candidate.is_dir() and candidate not in old_managed_sources:
                    old_managed_sources.append(candidate)
            if old_managed_sources and new_managed != old_managed:
                new_managed.mkdir(parents=True, exist_ok=True)
                for old_src in old_managed_sources:
                    if old_src.is_dir():
                        _merge_data_tree(old_src, new_managed)
            # 改写 skills_dirs：把指向旧 managed / 旧同级 managed / 旧 self._paths.app_dir/skills 的项改写为新绝对路径。
            old_refs = {str(old_managed), str(old_sibling_managed), str(legacy_managed)}
            with self.config.lock:
                dirs = self.config.data.setdefault("skills_dirs", [])
                rewritten = []
                for item in list(dirs):
                    resolved = str(self.config._resolve_dir(str(item)))
                    if resolved in old_refs:
                        if str(new_managed) not in rewritten:
                            rewritten.append(str(new_managed))
                    else:
                        rewritten.append(item)
                self.config.data["skills_dirs"] = rewritten
                # 若新托管目录不在 skills_dirs 中，追加以保证重启后被扫描/可安装。
                if str(new_managed) not in rewritten:
                    rewritten.append(str(new_managed))
                    self.config.data["skills_dirs"] = rewritten
                self.config.save()
            self.config.update_settings({"data_dir": str(target)})
        except (OSError, sqlite3.Error, ValueError) as exc:
            return {"ok": False, "error": f"迁移数据失败：{exc}"}
        return {
            "ok": True,
            "message": "数据与 Skills 已复制到新目录，请重启后生效",
            "target_data_dir": str(target),
            "target_skills_dir": str(new_managed),
            "restart_required": True,
            **self.migration_health(),
        }

    def migration_merge(self, body: dict[str, Any]) -> dict[str, Any]:
        """手动迁移：从用户指定的旧数据目录合并配置与对话（当前数据优先）。"""
        source = Path(str(body.get("source") or "").strip()).expanduser().resolve()
        if not source.is_dir():
            return {"ok": False, "error": "旧数据目录不存在"}
        try:
            report = self.import_legacy_data(source)
            self.storage.apply_pending_migrations()
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc), **self.migration_health()}
        return {"ok": True, "report": report, **self.migration_health()}

    # ---- 3.4.3 内联业务下沉：HTTP 分支体迁入（返回 (payload, status) 供传输层直发） ----

    def api_create_conversation(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        title = str(body.get("title") or "新对话")
        provider_id = str(body.get("provider_id") or self.config.data.get("provider_id") or "")
        model_key = str(body.get("model_key") or "")
        model_name = body.get("model_name", "")
        raw_agent_id = body.get("agent_id")
        agent_id = str(raw_agent_id or self.config.default_agent_id())
        if raw_agent_id is not None and not self.config.get_agent(agent_id):
            # 前端「新建会话」会沿用上一个会话的 agent_id；若该 Agent 已被删除，
            # 这里回退到默认 Agent 而不是 400 拒绝，避免新建会话整条链路报错。
            agent_id = self.config.default_agent_id()
        permission_mode = str(body.get("permission_mode") or "auto")
        web_search_enabled = body.get("web_search_enabled", False)
        deep_reasoning_enabled = body.get("deep_reasoning_enabled", False)
        reasoning_effort = body.get("reasoning_effort")
        workspace_dir = body.get("workspace_dir")
        workspace_group = body.get("workspace_group")
        if permission_mode not in ("confirm", "auto", "full"):
            return {"error": "permission_mode 必须是 confirm / auto / full"}, HTTPStatus.BAD_REQUEST
        if not isinstance(web_search_enabled, bool):
            return {"error": "web_search_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST
        if not isinstance(deep_reasoning_enabled, bool):
            return {"error": "deep_reasoning_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST
        if reasoning_effort is not None and str(reasoning_effort).lower() not in {"off", "low", "medium", "high", "auto"}:
            return {"error": "reasoning_effort 无效"}, HTTPStatus.BAD_REQUEST
        if workspace_dir is not None and not isinstance(workspace_dir, str):
            return {"error": "workspace_dir 必须是文本"}, HTTPStatus.BAD_REQUEST
        if workspace_group is not None and not isinstance(workspace_group, str):
            return {"error": "workspace_group 必须是文本"}, HTTPStatus.BAD_REQUEST
        if not isinstance(model_name, str):
            return {"error": "model_name 必须是文本"}, HTTPStatus.BAD_REQUEST
        if len(model_name.strip()) > 256:
            return {"error": "模型名称不能超过 256 个字符"}, HTTPStatus.BAD_REQUEST
        workspace_group = str(workspace_group or "").strip()
        if workspace_group:
            try:
                # Registered workspace bindings are authoritative.
                workspace_dir = self.config.workspace_dir_for_group(workspace_group)
                resolved_workspace = self.config.resolve_workspace_dir(workspace_dir)
                self.config.ensure_workspace_writable(resolved_workspace)
            except (OSError, ValueError) as exc:
                return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        if workspace_dir is not None and str(workspace_dir).strip():
            try:
                resolved_workspace = self.config.resolve_workspace_dir(str(workspace_dir).strip())
                self.config.ensure_workspace_writable(resolved_workspace)
            except (OSError, ValueError) as exc:
                return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        return (
            self.storage.create_conversation(
                title=title, provider_id=provider_id, agent_id=agent_id,
                interaction_mode="craft", model_key=model_key, model_name=model_name,
                permission_mode=permission_mode,
                web_search_enabled=web_search_enabled,
                deep_reasoning_enabled=deep_reasoning_enabled,
                reasoning_effort=str(reasoning_effort or ("medium" if deep_reasoning_enabled else "auto")),
                workspace_dir=str(workspace_dir or ""),
                workspace_group=workspace_group,
            ),
            HTTPStatus.CREATED,
        )

    def api_update_conversation_settings(self, conversation_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        title = body.get("title")
        system_prompt = body.get("system_prompt")
        stream_enabled = body.get("stream_enabled")
        provider_id = body.get("provider_id")
        agent_id = body.get("agent_id")
        model_key = body.get("model_key")
        model_name = body.get("model_name")
        if title is not None and not isinstance(title, str):
            return {"error": "title 必须是文本"}, HTTPStatus.BAD_REQUEST
        if title is not None and len(title.strip()) > 120:
            return {"error": "对话名称不能超过 120 个字符"}, HTTPStatus.BAD_REQUEST
        if system_prompt is not None and not isinstance(system_prompt, str):
            return {"error": "system_prompt 必须是文本"}, HTTPStatus.BAD_REQUEST
        if stream_enabled is not None and not isinstance(stream_enabled, bool):
            return {"error": "stream_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST
        if provider_id is not None and not isinstance(provider_id, str):
            return {"error": "provider_id 必须是文本"}, HTTPStatus.BAD_REQUEST
        if agent_id is not None and not isinstance(agent_id, str):
            return {"error": "agent_id 必须是文本"}, HTTPStatus.BAD_REQUEST
        if agent_id is not None and not self.config.get_agent(str(agent_id)):
            return {"error": "Agent 不存在"}, HTTPStatus.BAD_REQUEST
        # 会话已固化启用工具集后不允许会话内切换 Agent，否则工具集变化破坏前缀缓存。
        if agent_id is not None:
            try:
                conv_row = self.storage.get_conversation(conversation_id)
            except Exception:  # noqa: BLE001 - 读取失败不应中断整个请求
                conv_row = None
            current_agent_id = str((conv_row or {}).get("agent_id") or "")
            if agent_id != current_agent_id and (conv_row or {}).get("enabled_tool_ids"):
                return (
                    {"error": "该会话已固化启用工具集，暂不支持会话内切换 Agent；请新开对话后再切换"},
                    HTTPStatus.CONFLICT,
                )
        if model_key is not None and not isinstance(model_key, str):
            return {"error": "model_key 必须是文本"}, HTTPStatus.BAD_REQUEST
        if model_name is not None and not isinstance(model_name, str):
            return {"error": "model_name 必须是文本"}, HTTPStatus.BAD_REQUEST
        if isinstance(model_name, str) and len(model_name.strip()) > 256:
            return {"error": "模型名称不能超过 256 个字符"}, HTTPStatus.BAD_REQUEST
        interaction_mode = body.get("interaction_mode")
        if interaction_mode is not None:
            if not isinstance(interaction_mode, str):
                return {"error": "interaction_mode 必须是文本"}, HTTPStatus.BAD_REQUEST
            normalized_interaction_mode = interaction_mode.strip().lower()
            if normalized_interaction_mode not in {"plan", "craft", "ask"}:
                return {"error": "interaction_mode 必须是 plan 或普通模式"}, HTTPStatus.BAD_REQUEST
            interaction_mode = "craft"
        permission_mode = body.get("permission_mode")
        if permission_mode is not None:
            if not isinstance(permission_mode, str) or permission_mode not in ("confirm", "auto", "full"):
                return {"error": "permission_mode 必须是 confirm / auto / full"}, HTTPStatus.BAD_REQUEST
        web_search_enabled = body.get("web_search_enabled")
        if web_search_enabled is not None and not isinstance(web_search_enabled, bool):
            return {"error": "web_search_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST
        deep_reasoning_enabled = body.get("deep_reasoning_enabled")
        reasoning_effort = body.get("reasoning_effort")
        workspace_dir = body.get("workspace_dir")
        workspace_group = body.get("workspace_group")
        if deep_reasoning_enabled is not None and not isinstance(deep_reasoning_enabled, bool):
            return {"error": "deep_reasoning_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST
        if reasoning_effort is not None and str(reasoning_effort).lower() not in {"off", "low", "medium", "high", "auto"}:
            return {"error": "reasoning_effort 无效"}, HTTPStatus.BAD_REQUEST
        if workspace_dir is not None and not isinstance(workspace_dir, str):
            return {"error": "workspace_dir 必须是文本"}, HTTPStatus.BAD_REQUEST
        if workspace_group is not None and not isinstance(workspace_group, str):
            return {"error": "workspace_group 必须是文本"}, HTTPStatus.BAD_REQUEST
        favorite = body.get("favorite")
        if favorite is not None and not isinstance(favorite, bool):
            return {"error": "favorite 必须是布尔值"}, HTTPStatus.BAD_REQUEST
        if workspace_group is not None:
            workspace_group = str(workspace_group).strip()
            if workspace_group:
                try:
                    # Switching a group also switches its filesystem root.
                    workspace_dir = self.config.workspace_dir_for_group(workspace_group)
                    resolved_workspace = self.config.resolve_workspace_dir(workspace_dir)
                    self.config.ensure_workspace_writable(resolved_workspace)
                except (OSError, ValueError) as exc:
                    return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
            else:
                # "Ungrouped" changes only sidebar classification and
                # deliberately preserves the conversation's directory.
                workspace_dir = None
        elif workspace_dir is not None:
            # 兼容旧客户端只改 workspace_dir：若会话已在注册分组内，保持分组绑定。
            current = self.storage.get_conversation(conversation_id, include_messages=False)
            current_group = str((current or {}).get("workspace_group") or "").strip()
            if current_group:
                try:
                    workspace_dir = self.config.workspace_dir_for_group(current_group)
                    resolved_workspace = self.config.resolve_workspace_dir(workspace_dir)
                    self.config.ensure_workspace_writable(resolved_workspace)
                except (OSError, ValueError):
                    pass
        updated = self.storage.update_conversation_settings(
            conversation_id,
            title=title, system_prompt=system_prompt, stream_enabled=stream_enabled,
            provider_id=provider_id, agent_id=agent_id, interaction_mode=interaction_mode,
            model_key=model_key, model_name=model_name, permission_mode=permission_mode,
            web_search_enabled=web_search_enabled, deep_reasoning_enabled=deep_reasoning_enabled,
            reasoning_effort=reasoning_effort, workspace_dir=workspace_dir,
            workspace_group=workspace_group,
        )
        if favorite is not None:
            # 收藏单独落库：只改标记、不推进 updated_at（否则侧栏顺序被重排）。
            # 会话不存在时 update_conversation_settings 已返回 None，这里保持一致语义。
            favorited = self.storage.set_conversation_favorite(conversation_id, favorite)
            if favorited is None:
                return {"error": "对话不存在"}, HTTPStatus.NOT_FOUND
            updated = favorited if updated is None else {**updated, **favorited}
        return updated or {"error": "对话不存在"}, HTTPStatus.OK if updated else HTTPStatus.NOT_FOUND

    def api_provider_presets(self) -> dict[str, Any]:
        """供应商预设表（设置弹层与首启引导共用，名单唯一来源在 llm/provider_presets.py）。"""
        return {"presets": provider_presets_payload()}

    def api_open_provider_key_url(self, preset_id: str) -> tuple[dict[str, Any], int]:
        """用系统默认浏览器打开预设的 Key 申请入口（弹层与向导的「打开注册页」按钮）。

        地址只从预设表白名单里按 ``preset_id`` 取，**不接受调用方传入任意 URL**，
        因此这个端点不会变成「打开任意网址」的通道；自定义 / 本地预设没有入口，
        直接 400。引导文案里的远端网址必须能点，否则用户只能手抄（见 provider_presets）。
        """
        url = provider_preset_key_url(preset_id)
        if not url:
            return {"error": "该供应商模板没有注册页，请直接访问其官网"}, HTTPStatus.BAD_REQUEST
        try:
            opened = webbrowser.open(url)
        except Exception as exc:  # pragma: no cover - 取决于桌面环境
            return {"error": f"打开浏览器失败：{exc}"}, HTTPStatus.BAD_GATEWAY
        if not opened:
            return {"error": f"没能调起浏览器，请手动访问 {url}"}, HTTPStatus.BAD_GATEWAY
        return {"ok": True, "url": url}, HTTPStatus.OK

    def api_upsert_model_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """保存 API / 本地模型连接配置。

        ``provider.model`` 只属于供应商设置和连接测试；普通会话最终使用的模型始终来自
        会话模型下拉框，因此修改这里不会改写任何已有会话的模型选择。
        携带 ``preset_id`` 时先按预设表回填连接字段（用户显式提交的非空值优先），
        未知预设抛 ValueError（HTTP 层转 400）。
        """
        return self.config.upsert_provider(apply_preset_values(body))

    def api_upsert_workspace(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        name = str(body.get("name") or "").strip()
        raw_dir = str(body.get("dir") or "").strip()
        if not name:
            return {"error": "工作区名称不能为空"}, HTTPStatus.BAD_REQUEST
        if not raw_dir:
            return {"error": "工作区目录不能为空"}, HTTPStatus.BAD_REQUEST
        try:
            resolved_dir = self.config.resolve_workspace_dir(raw_dir)
            self.config.ensure_workspace_writable(resolved_dir)
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        workspaces = list(self.config.data.get("workspaces", []))
        if any(str(ws.get("name") or "").strip() == name for ws in workspaces):
            return {"error": "工作区名称已存在"}, HTTPStatus.BAD_REQUEST
        workspaces.append({"name": name, "dir": raw_dir})
        try:
            self.config.update_settings({"workspaces": workspaces})
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        return {"workspaces": self.config.data.get("workspaces", [])}, HTTPStatus.OK

    def api_delete_workspace(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        name = str(body.get("name") or "").strip()
        raw_dir = str(body.get("dir") or "").strip()
        workspaces = list(self.config.data.get("workspaces", []))
        new_list = [
            ws for ws in workspaces
            if not (
                str(ws.get("name") or "").strip() == name
                or (raw_dir and str(ws.get("dir") or "").strip() == raw_dir)
            )
        ]
        removed_names = [
            str(ws.get("name") or "").strip()
            for ws in workspaces
            if ws not in new_list
        ]
        if not removed_names:
            # 注册表中没有匹配项：若调用方仍给了名称，允许归档该名称下的对话（处理遗留分组）。
            if not name:
                return {"error": "工作区不存在"}, HTTPStatus.NOT_FOUND
            removed_names = [name]
        self.config.update_settings({"workspaces": new_list})
        # 已删除工作区下的对话归档到「未分组」，避免残留分组。
        for ws_name in removed_names:
            if ws_name:
                self.storage.clear_workspace_group(ws_name)
        return {"workspaces": self.config.data.get("workspaces", [])}, HTTPStatus.OK

    def api_update_runtime_settings(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if "data_dir" in body:
            requested = self.config.resolve_data_dir(str(body.get("data_dir") or "data"))
            if requested != self.paths.data_dir.resolve() and self.storage.list_background_tasks(active_only=True):
                return {"error": "存在活动任务，请先等待完成或取消后再切换数据目录"}, HTTPStatus.CONFLICT
        settings = self.config.update_settings(body)
        # 代理设置变更即时生效：重建统一网络入口的 opener，无需重启。
        net_io.configure(self.config.data.get("proxy"))
        model_key = str(body.get("model_key") or body.get("default_model_key") or "").strip()
        if model_key:
            self.config.set_default_model_key(model_key)
            settings = self.config.public()
        self.executor.command_timeout = int(self.config.data.get("command_timeout", 120))
        self.executor.set_permission_mode(str(self.config.data.get("permission_mode", "confirm")))
        # 工作区变更：仅影响新任务；已运行后台任务继续使用其启动时的快照路径。
        if "workspace_dir" in body:
            self.executor.workspace = self.config.resolve_workspace_dir()
        return (
            {
                "settings": settings,
                "default_model_key": self.config.default_model_key(),
                "resolved_workspace_dir": str(self.config.resolve_workspace_dir()),
                "resolved_data_dir": str(self.config.resolve_data_dir()),
                "image_cache_bytes": _uploads_total_bytes(self.paths.data_dir),
                "restart_required": (
                    ("data_dir" in body and self.config.resolve_data_dir() != self.paths.data_dir.resolve())
                    or ("host" in body and str(self.config.data.get("host")) != self.listener_host)
                ),
                "network_access": network_access_status(
                    self.listener_host,
                    int(self.config.data.get("port", 8765)),
                ),
                "proxy_state": net_io.proxy_state(),
            },
            HTTPStatus.OK,
        )

    def _reply(self, payload: Any, status: int = HTTPStatus.OK) -> tuple[dict[str, Any], int]:
        """传输层应答约定：返回 (payload, status) 供 http 层直发（3.4.3 下沉约定）。"""
        return payload, status

    def _confirm_tool(self, body: dict[str, Any]) -> None:
        confirm_id = str(body.get("confirm_id") or "").strip()
        run_id = str(body.get("run_id") or "").strip()
        if not confirm_id or not run_id:
            return self._reply({"error": "run_id 和 confirm_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        # Do not hold the browser's approval request open while a generation,
        # command or MCP action runs for minutes. The owning Run keeps waiting
        # for the real result through the confirmation condition.
        result_pair = self.runs.confirm_tool_async(run_id, confirm_id)
        if result_pair is None:
            return self._reply({"error": "确认请求不属于该运行或已失效"}, HTTPStatus.CONFLICT)
            return
        success, result = result_pair
        return self._reply({"success": success, "result": result})

    def _delete_skill(self, body: dict[str, Any]) -> None:
        skill_id = str(body.get("skill_id") or "").strip()
        if not skill_id:
            return self._reply({"error": "skill_id 不能为空"}, HTTPStatus.BAD_REQUEST)
        mode = str(body.get("mode") or "recycle").strip().lower()
        if mode not in {"recycle", "permanent"}:
            return self._reply({"error": "删除方式无效"}, HTTPStatus.BAD_REQUEST)
        return self._delete_skill_by_id(skill_id, permanently=mode == "permanent")

    def _skill_delete_must_hide(self, skill: dict[str, Any]) -> bool:
        """同一 Skill 是否由多个目录（或打包内置目录）同时提供。

        这种 Skill 不能只搬走其中一个副本：另一个副本会让它在列表里立刻"复活"，
        表现为「点了删除却没删掉」。此时统一按 id 隐藏（可在「已隐藏」里恢复），
        一次点击即从列表消失，且重启后不会回来。
        """
        if skill.get("duplicate_dirs"):
            return True
        return str(skill.get("id") or "") in self.catalog.bundled_skill_ids()

    def _remove_bundled_managed_copy(self, skill_id: str, permanently: bool) -> dict[str, Any] | None:
        """Remove the managed copy of a bundled Skill while leaving the package source intact."""
        managed_dir = self.config.resolve_managed_skills_dir()
        managed_catalog = SkillCatalog([managed_dir], sources=["managed"])
        managed_skill = managed_catalog.by_id().get(skill_id)
        if not managed_skill:
            return None
        return delete_skill(
            skill_id,
            str(self.paths.data_dir / "skills_recycle"),
            [],
            str(managed_dir),
            skills_by_id={skill_id: managed_skill},
            permanently=permanently,
        )

    def _delete_skill_by_id(self, skill_id: str, permanently: bool = False) -> None:
        """Delete a managed Skill, or persistently hide a bundled/external/shared Skill.

        Physical deletion is restricted to application-managed files. Bundled and external
        sources remain hidden by id; bundled Skills also discard their managed copy.
        """
        skills = self.catalog.by_id()
        skill = skills.get(skill_id)
        if not skill:
            return self._reply({"error": "Skill 不存在"}, HTTPStatus.NOT_FOUND)
        root = Path(str(skill.get("root") or skill.get("path") or "")).expanduser().resolve()
        if not root.exists():
            return self._reply({"error": "Skill 目录不存在"}, HTTPStatus.NOT_FOUND)
        agents = self.config.public_agents()
        must_hide = skill.get("source") != "managed" or self._skill_delete_must_hide(skill)
        if must_hide:
            managed_copy = None
            if skill_id in self.catalog.bundled_skill_ids():
                try:
                    managed_copy = self._remove_bundled_managed_copy(skill_id, permanently)
                except Exception as exc:  # noqa: BLE001
                    return self._reply({"error": f"处理内置 Skill 的托管副本失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                if managed_copy and not managed_copy.get("success"):
                    return self._reply({"error": managed_copy.get("error", "处理托管副本失败")}, HTTPStatus.BAD_REQUEST)
            self.config.hide_skill(skill_id)
            self.catalog.hidden_ids.add(skill_id)
            result = {
                "success": True,
                "skill_id": skill_id,
                "name": str(skill.get("name") or ""),
                "hidden": True,
                "recycled_to": managed_copy.get("recycled_to") if managed_copy else None,
                "permanently_deleted": bool(managed_copy and managed_copy.get("permanently_deleted")),
                "managed_copy_removed": bool(managed_copy),
                "cleaned_agent_refs": [
                    str(agent.get("id"))
                    for agent in agents
                    if skill_id in agent.get("skill_ids", [])
                ],
            }
        else:
            # 传真实托管目录（而非 root.parent）：托管 Skill 的 root 应位于其下；若 root 恰等于
            # 托管目录本身（历史遗留的散装 SKILL.md），底层会退化为只回收定义文件，绝不整目录搬走。
            managed_dir = self.config.resolve_managed_skills_dir()
            recycle_dir = self.paths.data_dir / "skills_recycle"
            try:
                result = delete_skill(
                    skill_id,
                    str(recycle_dir),
                    agents,
                    str(managed_dir),
                    skills_by_id=skills,
                    permanently=permanently,
                )
            except Exception as exc:  # noqa: BLE001
                return self._reply({"error": f"删除失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            if not result.get("success"):
                return self._reply({"error": result.get("error", "删除失败")}, HTTPStatus.BAD_REQUEST)
            if result.get("hidden"):
                self.config.hide_skill(skill_id)
                self.catalog.hidden_ids.add(skill_id)
        updated_agents = remove_skill_references(skill_id, agents)
        for agent in updated_agents:
            if agent.get("id") in built_in_agent_ids():
                continue
            try:
                self.config.upsert_agent(agent)
            except Exception:  # noqa: BLE001
                pass
        return self._reply(
            {
                "ok": True,
                "recycled_to": result.get("recycled_to"),
                "hidden": bool(result.get("hidden")),
                "permanently_deleted": bool(result.get("permanently_deleted")),
                "managed_copy_removed": bool(result.get("managed_copy_removed")),
                "cleaned_agent_refs": result.get("cleaned_agent_refs", []),
                "skills": self.catalog.scan(),
                "agents": self.config.public_agents(),
                "hidden_skills": self._hidden_skill_entries(),
                "recycled_skills": self._recycled_skill_entries(),
            }
        )

    def _clear_skill_recycle(self, _body: dict[str, Any]) -> None:
        result = clear_skill_recycle(self.paths.data_dir / "skills_recycle")
        if not result.get("success"):
            return self._reply({"error": result.get("error", "清空回收目录失败")}, HTTPStatus.BAD_REQUEST)
        return self._reply({"ok": True, "deleted": result.get("deleted", 0)})

    def _recycled_skill_entries(self) -> list[dict[str, str]]:
        return list_skill_recycle(self.paths.data_dir / "skills_recycle")

    def _restore_skill_recycle(self, body: dict[str, Any]) -> None:
        result = restore_skill_recycle_entry(
            str(body.get("entry") or ""),
            self.paths.data_dir / "skills_recycle",
            self.config.resolve_managed_skills_dir(),
        )
        if not result.get("success"):
            return self._reply({"error": result.get("error", "恢复 Skill 失败")}, HTTPStatus.BAD_REQUEST)
        restored = Path(str(result["restored_to"])).resolve()
        for skill in SkillCatalog([restored], sources=["managed"]).scan():
            skill_id = str(skill.get("id") or "")
            if skill_id:
                self.config.unhide_skill(skill_id)
                self.catalog.hidden_ids.discard(skill_id)
        return self._reply({
            "ok": True,
            "skills": self.catalog.scan(),
            "hidden_skills": self._hidden_skill_entries(),
            "recycled_skills": self._recycled_skill_entries(),
        })

    def _edit_message(self, body: dict[str, Any]) -> None:
        """删除指定消息及其之后所有消息，供"从该处重新编辑对话"使用。

        前端随后会携带新内容调用 /api/chat 重发一轮，因此这里只负责截断。
        """
        conversation_id = str(body.get("conversation_id") or "")
        message_id = str(body.get("message_id") or "")
        if not conversation_id or not message_id:
            return self._reply({"error": "conversation_id 和 message_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        conversation = self.storage.get_conversation(conversation_id)
        if not conversation:
            return self._reply({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
            return
        target = next((m for m in conversation.get("messages", []) if m.get("id") == message_id), None)
        if not target:
            return self._reply({"error": "消息不存在"}, HTTPStatus.NOT_FOUND)
            return
        if target.get("role") != "user":
            return self._reply({"error": "只能编辑用户消息"}, HTTPStatus.BAD_REQUEST)
            return
        removed = self.storage.truncate_from_message(conversation_id, message_id)
        metadata = target.get("metadata") or {}
        return self._reply({
            "ok": True,
            "removed": removed,
            "attachments": metadata.get("attachments") or [],
            # 文件夹索引与附件同源：编辑后重发要把它们一起还给输入区（见 04-messages.js）。
            "folder_indexes": metadata.get("folder_indexes") or [],
        })

    # ---- 侧栏「全文搜索」/「分支导航」/「删除单条消息」的 UI 直读接口 ----
    # 三个都返回 ``(payload, status)``，由 http.py 直接 ``self._json(*...)`` 透传：
    # 与 api_create_conversation / api_update_conversation_settings 同一形态。
    def api_search_messages(self, params: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """全文搜索（只读，不进模型上下文）。

        ``limit`` 默认 30、上限 50，越界即夹紧而不报错——它是 UI 的展示意图，不是契约；
        ``q`` 为空是 400（空查询会白扫一遍全库且结果毫无意义）；
        ``conversation_id`` 给了但不存在是 404，避免前端把「搜不到」误读成「这个会话没命中」。
        """
        query = _query_first(params, "q").strip()
        if not query:
            return {"error": "q 不能为空"}, HTTPStatus.BAD_REQUEST
        raw_limit = _query_first(params, "limit").strip()
        try:
            limit = int(raw_limit) if raw_limit else 30
        except ValueError:
            return {"error": "limit 必须是整数"}, HTTPStatus.BAD_REQUEST
        conversation_id = _query_first(params, "conversation_id").strip()
        try:
            return (
                self.storage.search_messages(query, conversation_id=conversation_id, limit=limit),
                HTTPStatus.OK,
            )
        except LookupError as exc:
            return {"error": str(exc)}, HTTPStatus.NOT_FOUND

    def api_branch_chain(self, conversation_id: str) -> tuple[dict[str, Any], int]:
        """该会话所属的分支链（源 + 兄弟分支，或自己的全部分支）。只读。"""
        if not conversation_id:
            return {"error": "conversation_id 不能为空"}, HTTPStatus.BAD_REQUEST
        try:
            return self.storage.branch_chain(conversation_id), HTTPStatus.OK
        except LookupError as exc:
            return {"error": str(exc)}, HTTPStatus.NOT_FOUND

    def api_delete_message(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """删除一条消息（``mode="single"``）或一整轮（``mode="turn"``），回传撤销快照。"""
        conversation_id = str(body.get("conversation_id") or "")
        message_id = str(body.get("message_id") or "")
        if not conversation_id or not message_id:
            return {"error": "conversation_id 和 message_id 不能为空"}, HTTPStatus.BAD_REQUEST
        mode = str(body.get("mode") or "single")
        try:
            return self.storage.delete_message(conversation_id, message_id, mode), HTTPStatus.OK
        except LookupError as exc:
            return {"error": str(exc)}, HTTPStatus.NOT_FOUND
        except ValueError as exc:
            return {"error": str(exc)}, HTTPStatus.BAD_REQUEST

    def api_restore_messages(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """撤销删除：按快照把消息原样插回（``messages`` 为 ``/api/messages/delete`` 的 ``removed``）。"""
        conversation_id = str(body.get("conversation_id") or "")
        if not conversation_id:
            return {"error": "conversation_id 不能为空"}, HTTPStatus.BAD_REQUEST
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return {"error": "messages 必须是非空数组"}, HTTPStatus.BAD_REQUEST
        try:
            return self.storage.restore_messages(conversation_id, messages), HTTPStatus.OK
        except LookupError as exc:
            return {"error": str(exc)}, HTTPStatus.NOT_FOUND
        except ValueError as exc:
            return {"error": str(exc)}, HTTPStatus.BAD_REQUEST

    def _hidden_skill_entries(self) -> list[dict[str, Any]]:
        """返回当前被隐藏（命中 hidden_skill_ids，但不带隐藏过滤扫描得到）的 Skill 条目。"""
        hidden_ids = set(self.config.get_hidden_skill_ids())
        if not hidden_ids:
            return []
        try:
            all_skills = SkillCatalog(
                list(self.catalog.directories),
                hidden_ids=[],
                package_dir=self.catalog.package_dir,
                sources=list(self.catalog.sources or []),
            ).scan()
        except Exception:  # noqa: BLE001 - 隐藏列表只是展示信息，不应让扫描失败
            return []
        return [
            {**item, "hidden": True}
            for item in all_skills
            if str(item.get("id") or "") in hidden_ids
        ]

    def _install_dir(self, body: dict[str, Any]) -> None:
        raw = str(body.get("dir") or "").strip()
        try:
            resolved = self.config.add_skills_dir(raw)
        except ValueError as exc:
            return self._reply({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.catalog.add_directory(raw)
        skills = self.catalog.scan()
        return self._reply({"dir": str(resolved), "configured": self.config.get_skills_dirs(), "skills": skills})

    def _parse_character_card(self, body: dict[str, Any]) -> None:
        """解析 SillyTavern 角色卡 PNG，返回归一化的人设 system_prompt 文本。"""
        encoded = str(body.get("data") or "")
        if "," in encoded and encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return self._reply({"error": "文件内容不是有效 Base64"}, HTTPStatus.BAD_REQUEST)
            return
        if len(data) > 80 * 1024 * 1024:
            return self._reply({"error": "角色卡文件不能超过 80 MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            result = parse_sillytavern_card(data)
        except ValueError as exc:
            return self._reply({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        # Every successful character-card parse is also preserved as an
        # independent reusable prompt.  This does not touch any conversation;
        # callers may still decide whether to copy the returned text into the
        # currently open settings draft.
        filename = Path(str(body.get("name") or "")).name
        fallback_title = Path(filename).stem.strip()
        title = str((result.get("meta") or {}).get("name") or "").strip() or fallback_title or "未命名角色"
        try:
            result["preset"] = self.config.add_conversation_prompt_preset(
                title, str(result.get("system_prompt") or ""), "character_card"
            )
        except ValueError:
            # A card without a usable normalized prompt remains a successful
            # parse response for old clients, but must not create an entry.
            pass
        return self._reply(result)

    def _provider_models(self, body: dict[str, Any]) -> None:
        try:
            provider = self._resolve_model_profile(body)
            return self._reply({"models": self.models.list_online_models(provider)})
        except Exception as exc:
            proxy_note = (net_io.proxy_state().get("note") or "").strip()
            suffix = f"（外部请求：{proxy_note}）" if proxy_note else ""
            return self._reply({"error": f"{exc}{suffix}"}, HTTPStatus.BAD_REQUEST)

    def _provider_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        provider = dict(body)
        if provider.get("id") and not provider.get("api_key"):
            stored = next(
                (item for item in self.config.data.get("providers", []) if item.get("id") == provider["id"]),
                None,
            )
            if stored:
                provider = {
                    **stored,
                    **{
                        key: value for key, value in provider.items()
                        if key != "api_key" or bool(value)
                    },
                }
        request_format = str(provider.get("request_format") or "openai_chat").strip().lower()
        provider["kind"] = (
            str(provider.get("kind") or "").strip().lower()
            if str(provider.get("kind") or "").strip().lower() in VALID_MODEL_KINDS
            else _infer_kind_for_request_format(request_format)
        )
        explicit_images = provider.get("supports_images")
        provider["supports_images_explicit"] = (
            explicit_images if isinstance(explicit_images, bool) else None
        )
        return provider

    def _first_turn_info(self, conversation_id: str) -> dict[str, Any] | None:
        """会话首轮「第一轮发送上下文」（系统提示词原文 + 工具集 + 模型/技能信息）。

        数据来源：会话级 `conversations.first_turn` 列（分支对话会继承、清空已结束任务不丢）；
        v16 之前的老会话该列为空，回退读最早 chat run 快照里的 first_turn 键。
        都没有时返回 None，前端不显示折叠卡；旧版落盘结构（prompt 字段而非 system）
        在此归一兼容——前端仅认 system。
        """
        info = self.storage.conversation_first_turn(conversation_id)
        if info is None:
            snapshot = self.storage.first_chat_run_snapshot(conversation_id)
            legacy = snapshot.get("first_turn") if isinstance(snapshot, dict) else None
            info = legacy if isinstance(legacy, dict) and legacy else None
        if not isinstance(info, dict) or not info:
            return None
        if not info.get("system") and info.get("prompt"):
            info = {**info, "system": str(info.get("prompt") or "")}
        return info

    def _reject_tool(self, body: dict[str, Any]) -> None:
        confirm_id = str(body.get("confirm_id") or "").strip()
        run_id = str(body.get("run_id") or "").strip()
        if not confirm_id or not run_id:
            return self._reply({"error": "run_id 和 confirm_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        result_pair = self.runs.reject_tool(run_id, confirm_id)
        if result_pair is None:
            return self._reply({"error": "确认请求不属于该运行或已失效"}, HTTPStatus.CONFLICT)
            return
        success, result = result_pair
        return self._reply({"success": success, "result": result})

    def _remove_install_dir(self, body: dict[str, Any]) -> None:
        raw = str(body.get("dir") or "").strip()
        if not raw:
            return self._reply({"error": "目录路径不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        configured = self.config.remove_skills_dir(raw)
        self.catalog.remove_directory(raw)
        return self._reply({"configured": configured, "skills": self.catalog.scan()})

    def _resolve_model_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """优先用 model_key 解析，缺失时回退到内联 provider（兼容旧调用）。"""
        model_key = str(body.get("model_key") or "").strip()
        if model_key:
            return self.config.profile(model_key)
        return self._provider_profile(body)

    def _test_provider(self, body: dict[str, Any]) -> None:
        try:
            provider = self._resolve_model_profile(body)
            result = self.models.complete(
                provider,
                [
                    {"role": "system", "content": "你是连接测试助手。直接回答，不要调用工具。"},
                    {"role": "user", "content": "只回复 OK"},
                ],
                {"temperature": 0, "max_tokens": 128, "stream": False, "connection_test": True, "lock_label": "连接测试"},
            )
            capability_resolver = getattr(self.vision, "brain_image_capability", None)
            capability = (
                capability_resolver(provider, probe_if_unknown=True)
                if callable(capability_resolver)
                else {
                    "supported": bool(self.vision.brain_supports_images(provider)),
                    "confirmed": False,
                    "source": "model_name",
                }
            )
            return self._reply({
                "ok": True,
                "response": result,
                "supports_images": bool(capability.get("supported")),
                "capability_confirmed": bool(capability.get("confirmed")),
                "capability_source": str(capability.get("source") or "model_name"),
                "proxy_state": net_io.proxy_state(),
            })
        except Exception as exc:
            proxy_note = (net_io.proxy_state().get("note") or "").strip()
            suffix = f"（外部请求：{proxy_note}）" if proxy_note else ""
            return self._reply({"ok": False, "error": f"{exc}{suffix}"}, HTTPStatus.BAD_REQUEST)

    def _unhide_skill(self, body: dict[str, Any]) -> None:
        skill_id = str(body.get("skill_id") or "").strip()
        if not skill_id:
            return self._reply({"error": "skill_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        self.config.unhide_skill(skill_id)
        self.catalog.hidden_ids.discard(skill_id)
        return self._reply({
            "ok": True,
            "skills": self.catalog.scan(),
            "hidden_skills": self._hidden_skill_entries(),
        })

    def _unload_provider(self, body: dict[str, Any]) -> None:
        try:
            model_key = str(body.get("model_key") or "").strip()
            provider = self.config.profile(model_key)
            result = self.models.unload_local_model(provider)
            return self._reply({"ok": True, **result})
        except Exception as exc:
            return self._reply({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _upload(self, body: dict[str, Any]) -> None:
        # 兼容回退：旧 JSON(base64) 格式不再接受，统一走 multipart 流式
        # （http.py _upload_request → _upload_spooled）。
        return self._reply({"error": "上传接口已升级为 multipart 流式，请刷新页面后重试"}, HTTPStatus.BAD_REQUEST)

    def api_set_agent_avatar(
        self, agent_id: str, raw: bytes
    ) -> tuple[dict[str, Any], int]:
        """设置 Agent 自定义头像：中心裁切正方形 → WebP 落盘 → 写回 agents[].avatar。

        头像是 Agent 级资源（用该 Agent 的会话统一显示），与 uploads 缓存分离存放，
        不受"未被引用即清理"的影响；换图会删掉上一份文件。
        """
        agent = self.config.get_agent(str(agent_id or "").strip())
        if not agent:
            return {"error": "Agent 不存在"}, HTTPStatus.NOT_FOUND
        try:
            name = store_agent_avatar(
                self._paths.data_dir,
                str(agent.get("id") or ""),
                raw,
                previous=str(agent.get("avatar") or ""),
            )
            saved = self.config.upsert_agent({**agent, "avatar": name})
        except ValueError as exc:
            return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        return {"ok": True, "agent": saved}, HTTPStatus.OK

    def api_read_agent_avatar(self, name: str) -> tuple[bytes, int] | tuple[dict[str, Any], int]:
        """读取头像文件字节（http.py 直接写响应体）。"""
        data = read_agent_avatar(self._paths.data_dir, name)
        if data is None:
            return {"error": "头像不存在"}, HTTPStatus.NOT_FOUND
        return data, HTTPStatus.OK

    # ---- 应用图标（托盘 + 桌面窗口）：落 app_dir，重启生效（见 storage/app_icon.py） ----
    def api_set_app_icon(self, raw: bytes) -> tuple[dict[str, Any], int]:
        """上传并启用自定义应用图标；图片不合法/落盘失败一律 400 + 中文文案。"""
        try:
            store_app_icon(self._paths.app_dir, raw)
        except ValueError as exc:
            return self._reply({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return self._reply({"ok": True, "custom": True})

    def api_clear_app_icon(self) -> tuple[dict[str, Any], int]:
        """恢复默认图标（只删 `custom-icon.*`，内置 `icon.ico` 不动）。"""
        try:
            clear_app_icon(self._paths.app_dir)
        except ValueError as exc:
            return self._reply({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return self._reply({"ok": True, "custom": False})

    def app_icon_status(self) -> dict[str, Any]:
        """图标状态：自定义是否生效（前端据此决定「恢复默认」是否可点）。"""
        return {"custom": has_custom_app_icon(self._paths.app_dir)}

    def api_read_app_icon(self) -> tuple[bytes, int] | tuple[dict[str, Any], int]:
        """当前生效图标的 PNG 字节（自定义优先，否则内置 icon.ico 转出），供弹窗预览。"""
        data = read_app_icon_png(self._paths.app_dir, self._paths.resource_dir)
        if not data:
            return {"error": "图标不可用"}, HTTPStatus.NOT_FOUND
        return data, HTTPStatus.OK

    def _upload_spooled(self, spool_path: str, original_name: str) -> tuple[dict[str, Any], int]:
        """multipart 流式上传的落库入口：http.py 完成协议解析后调用。

        spool_path 为传输层临时文件（.part），本方法负责读入 → 内容级去重 →
        分日目录落盘 → 图片压缩/缩略图 → 清理 spool。
        """
        spool = Path(spool_path)
        try:
            data = spool.read_bytes()
        except OSError as exc:
            spool.unlink(missing_ok=True)
            return {"error": f"上传临时文件读取失败：{exc}"}, HTTPStatus.BAD_REQUEST
        finally:
            spool.unlink(missing_ok=True)
        if len(data) > 80 * 1024 * 1024:
            return {"error": "单个文件不能超过 80 MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        imaging = dict(self.config.data.get("imaging") or {}) if getattr(self, "config", None) else {}
        result = store_uploaded_file(data, original_name, self._paths.data_dir, imaging)
        # 上传后超限自动清理（B1：只删未被消息/快照引用的缓存；阈值可在设置页调整，0=关闭）。
        try:
            auto_clean_mb = int(imaging.get("auto_clean_limit_mb", 256) or 256)
        except (TypeError, ValueError):
            auto_clean_mb = 256
        if auto_clean_mb > 0:
            auto_clean_uploads(
                self._paths.data_dir,
                limit=auto_clean_mb * 1024 * 1024,
                referenced_checker=self._upload_path_in_use,
                # 本次刚落盘的附件无条件保留：它此刻还没落库（引用保护对它无效），
                # 只有保护窗口 + 这条显式保护能拦住"上传即被清理"。
                protect_paths=[p for p in (result.get("path"), result.get("thumb_path")) if p],
            )
        return result, HTTPStatus.OK

    def _upload_path_in_use(self, target: Path) -> bool:
        """上传文件是否"在用"：消息/快照引用（storage 判定）之外，聊天背景图也算。

        背景图路径只写在 config.json 里（不是消息附件），不补这条判定的话，缓存自动
        清理会把它当无人引用的旧图删掉——用户什么都没做，背景图就"自己没了"
        （前端兜底只会清空设置并提示）。手动清理仍按"不区分引用"的既有语义，
        不改（那是用户显式点的按钮，UI 已明确告知）。
        """
        try:
            if self.storage.upload_path_referenced(target):
                return True
        except (OSError, ValueError):
            return True  # 判定不了时宁可保留（与清理侧同向）
        background = self.config.data.get("chat_background") or {}
        image = str(background.get("image") or "").strip()
        if not image:
            return False
        try:
            return Path(image).expanduser().resolve() == target
        except (OSError, ValueError):
            return False

    def api_clean_image_cache(self) -> tuple[dict[str, Any], int]:
        """手动清理缓存文件（设置页按钮）：与自动清理共用同一阈值口径
        （imaging.auto_clean_limit_mb，默认 256MB；0=关闭自动清理时手动回退默认值），
        按时间从旧到新保留最新文件、不区分引用（UI 已明确告知此语义）。
        """
        imaging = dict(self.config.data.get("imaging") or {}) if getattr(self, "config", None) else {}
        try:
            limit_mb = int(imaging.get("auto_clean_limit_mb", 256) or 256)
        except (TypeError, ValueError):
            limit_mb = 256
        if limit_mb <= 0:
            limit_mb = 256
        try:
            result = _clean_uploads_cache(limit=limit_mb * 1024 * 1024, data_dir=self._paths.data_dir)
        except OSError as exc:
            return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        return result, HTTPStatus.OK

    def _delete_upload(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """删除未被引用的上传文件（前端移除 chip 时调用；有引用则拒绝）。"""
        raw_path = str(body.get("path") or "")
        data_dir = self._paths.data_dir
        if not is_uploads_path(data_dir, raw_path):
            return {"error": "只允许删除宿主 uploads 缓存目录内的文件"}, HTTPStatus.FORBIDDEN
        if self._upload_path_in_use(Path(raw_path).expanduser().resolve()):
            return {"error": "该文件已被消息或聊天背景引用，不可删除"}, HTTPStatus.CONFLICT
        try:
            removed = remove_uploaded_file(data_dir, raw_path)
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        return {"ok": True, "removed": bool(removed)}, HTTPStatus.OK

    def api_rotate_upload(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """把一张（背景）图顺时针旋转 90° 并另存为新文件；前端随后把它写进背景设置。

        只接受 uploads / 内置 backgrounds 内的文件——这两个目录是"我们自己放图的地方"，
        别的路径（工作区任意文件）不该被这个接口读出来再另存一份。
        旋转结果走 ``store_uploaded_file``：分日目录 + 内容去重 + 缩略图，与普通上传同源。
        """
        raw = str(body.get("path") or "")
        data_dir = self._paths.data_dir
        try:
            turns = int(body.get("turns") or 1)
        except (TypeError, ValueError):
            return {"error": "turns 必须是整数（1 = 顺时针 90°）"}, HTTPStatus.BAD_REQUEST
        if not (is_uploads_path(data_dir, raw) or is_backgrounds_path(data_dir, raw)):
            return {"error": "只允许旋转宿主数据目录（uploads / backgrounds）内的图片"}, HTTPStatus.FORBIDDEN
        imaging = dict(self.config.data.get("imaging") or {}) if getattr(self, "config", None) else {}
        try:
            result = rotate_uploaded_image(Path(raw), data_dir, imaging, turns=turns)
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
        return {"ok": True, **result}, HTTPStatus.OK

    def background_presets(self) -> dict[str, Any]:
        """内置背景图清单（设置面板那一排缩略图用；首次访问时幂等生成）。

        生成失败/目录不可写时返回空清单——前端那排就整体隐藏，不影响自定义上传与其它设置。
        """
        return {"presets": ensure_background_presets(self._paths.data_dir)}

    def api_check_uploads(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """发送前附件存在性校验（前端在提交前调用，避免丢草稿才发现附件没了）。

        判定口径与 ``submit_chat`` 完全一致（``missing_cache_attachment``）：只查宿主
        缓存树（uploads/generated）内已丢失的文件，URL 与外部路径不判缺失。
        返回 {"missing": [丢失的原始路径, ...]}。
        """
        raw_paths = body.get("paths")
        if not isinstance(raw_paths, list):
            return {"error": "paths 必须是数组"}, HTTPStatus.BAD_REQUEST
        data_dir = self._paths.data_dir
        missing = [
            str(raw) for raw in raw_paths
            if missing_cache_attachment(data_dir, str(raw))
        ]
        return {"missing": missing}, HTTPStatus.OK


    def _finish_install(self, dest_raw: str, dest: Path, extra: dict[str, Any] | None = None) -> None:
        self.config.add_skills_dir(dest_raw)
        self.catalog.add_directory(dest_raw)
        # "导入即启用"：若本次安装目录里的 Skill 命中过 hidden_skill_ids（此前被隐藏/删除），
        # 自动取消隐藏，避免 scan() 静默过滤导致 UI 导入成功却不显示。
        installed_ids = {str(item.get("id") or "") for item in SkillCatalog([dest]).scan()}
        hidden_ids = set(self.config.get_hidden_skill_ids())
        unhidden: list[str] = [sid for sid in installed_ids if sid in hidden_ids]
        for sid in unhidden:
            self.config.unhide_skill(sid)
            self.catalog.hidden_ids.discard(sid)
        payload: dict[str, Any] = {
            "dir": str(dest),
            "configured": self.config.get_skills_dirs(),
            "skills": self.catalog.scan(),
            "hidden_skills": self._hidden_skill_entries(),
            "recycled_skills": self._recycled_skill_entries(),
            "unhidden": unhidden,
        }
        if extra:
            payload.update(extra)
        return self._reply(payload)

    def _install_folder(self, body: dict[str, Any]) -> None:
        files = body.get("files")
        if not isinstance(files, list) or not files:
            return self._reply({"error": "没有收到文件夹内容"}, HTTPStatus.BAD_REQUEST)
            return
        if len(files) > 2000:
            return self._reply({"error": "文件夹内文件数量过多（超过 2000）"}, HTTPStatus.BAD_REQUEST)
            return
        dest_resolved, dest_err = self._resolve_skill_dest(body)
        if dest_err is not None:
            return dest_err
        dest_raw, dest = dest_resolved
        entries: list[tuple[list[str], bytes]] = []
        total = 0
        for item in files:
            if not isinstance(item, dict):
                return self._reply({"error": "文件条目格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            parts = self._skill_relative_parts(item.get("path"))
            if not parts or any(part == ".." or ":" in part for part in parts):
                return self._reply(
                    {"error": f"文件夹包含非法路径：{item.get('path')}"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            encoded = str(item.get("data") or "")
            if "," in encoded and encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[1]
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError:
                return self._reply({"error": f"文件内容不是有效 Base64：{'/'.join(parts)}"}, HTTPStatus.BAD_REQUEST)
                return
            total += len(data)
            if total > 300 * 1024 * 1024:
                return self._reply(
                    {"error": "文件夹总大小不能超过 300 MB"},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            entries.append((parts, data))
        if not any(parts[-1] == "SKILL.md" and len(parts) <= 2 for parts, _ in entries):
            return self._reply(
                {"error": "文件夹必须包含 SKILL.md（需位于顶层或下一级目录）"},
                HTTPStatus.BAD_REQUEST,
            )
        # 单个裸 SKILL.md 也允许导入：补齐最小 frontmatter，避免它只能显示为笼统的“skill”。
        if len(entries) == 1 and entries[0][0] == ["SKILL.md"]:
            try:
                text = entries[0][1].decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and not (_frontmatter_value(text, "name") and _frontmatter_value(text, "description")):
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                heading_index = next((index for index, line in enumerate(lines) if line.startswith("#")), -1)
                heading = (
                    re.sub(r"^#{1,6}\s*", "", lines[heading_index]).strip()
                    if heading_index >= 0 else ""
                )
                derived_name = heading or str(body.get("name") or "skill")
                description_lines = lines[heading_index + 1:] if heading_index >= 0 else lines
                derived_desc = next(
                    (
                        line.lstrip("#>*- ").strip()
                        for line in description_lines
                        if line not in {"---", "..."}
                        and not re.match(r"^(name|description):\s*", line, re.I)
                        and not line.startswith("#")
                    ),
                    "未提供描述",
                )
                derived_name = derived_name.replace("\n", " ").strip()[:120] or "skill"
                derived_desc = derived_desc.replace("\n", " ").strip()[:240] or "未提供描述"
                text = f"---\nname: {derived_name}\ndescription: {derived_desc}\n---\n\n{text.lstrip()}"
                entries[0] = (entries[0][0], text.encode("utf-8"))
        prefix = self._skill_subdir_prefix(
            [parts for parts, _ in entries],
            dest,
            self._preferred_skill_dir_name(entries, str(body.get("name") or "").strip()),
        )
        dest.mkdir(parents=True, exist_ok=True)
        for parts, data in entries:
            target = (dest / Path(*prefix, *parts)).resolve()
            if not path_within(target, dest):
                return self._reply({"error": f"文件夹包含越界路径：{'/'.join(parts)}"}, HTTPStatus.BAD_REQUEST)
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return self._finish_install(dest_raw, dest, {"files": len(entries)})

    def _install_skill(self, body: dict[str, Any]) -> None:
        name = Path(str(body.get("name") or "skill.zip")).name
        encoded = str(body.get("data") or "")
        if "," in encoded and encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return self._reply({"error": "文件内容不是有效 Base64"}, HTTPStatus.BAD_REQUEST)
            return
        if len(data) > 80 * 1024 * 1024:
            return self._reply({"error": "Skill 压缩包不能超过 80 MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        dest_resolved, dest_err = self._resolve_skill_dest(body)
        if dest_err is not None:
            return dest_err
        dest_raw, dest = dest_resolved
        tmp_dir = Path(tempfile.mkdtemp(prefix="naiba_skill_"))
        try:
            zip_path = tmp_dir / name
            zip_path.write_bytes(data)
            with zipfile.ZipFile(zip_path) as archive:
                bad = archive.testzip()
                if bad is not None:
                    return self._reply({"error": f"压缩包损坏：{bad}"}, HTTPStatus.BAD_REQUEST)
                    return
                if not _zip_has_skill_md(archive):
                    return self._reply(
                        {"error": "压缩包必须包含 SKILL.md（位于压缩包顶层或其下一级目录）"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                members = archive.infolist()
                if len(members) > 5000:
                    return self._reply({"error": "压缩包内文件数量过多（超过 5000）"}, HTTPStatus.BAD_REQUEST)
                    return
                if sum(member.file_size for member in members) > 500 * 1024 * 1024:
                    return self._reply({"error": "压缩包解压后体积过大（超过 500 MB）"}, HTTPStatus.BAD_REQUEST)
                    return
                # 顶层直接散放 SKILL.md 的压缩包同样必须先收进独立子目录，否则该 Skill 的
                # root 就等于托管目录本身，删除时会连带移走整个目录下的所有 Skill。
                prefix = self._skill_subdir_prefix(
                    [Path(member.filename).parts for member in members if not member.is_dir()],
                    dest,
                    self._zip_skill_dir_name(archive, members, Path(name).stem),
                )
                extract_root = dest / Path(*prefix) if prefix else dest
                extract_root.mkdir(parents=True, exist_ok=True)
                for member in members:
                    target = (extract_root / member.filename).resolve()
                    if target != extract_root and not path_within(target, extract_root):
                        return self._reply(
                            {"error": f"压缩包包含越界路径：{member.filename}"},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
                archive.extractall(extract_root)
            return self._finish_install(dest_raw, dest)
        except zipfile.BadZipFile:
            return self._reply({"error": "不是有效的 zip 压缩包"}, HTTPStatus.BAD_REQUEST)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _resolve_skill_dest(self, body: dict[str, Any]) -> tuple[tuple[str, Path] | None, tuple[dict[str, Any], int] | None]:
        """解析 Skill 安装目标目录：未指定时默认安装到托管 Skills 目录。

        托管 Skills 目录位于数据目录内（self.paths.data_dir/skills）；旧 ``self.paths.app_dir/skills``
        与旧数据目录同级 skills 会重定向/合并到托管目录，保证旧配置不丢且默认落点离开 C 盘。
        """
        configured = self.config.get_skills_dirs()
        managed = self.config.resolve_managed_skills_dir()
        if str(body.get("dir") or "").strip():
            dest_raw = str(body.get("dir") or "").strip()
        elif configured and configured[0] != "skills":
            dest_raw = configured[0]
        else:
            dest_raw = str(managed)
        dest = self.config._resolve_dir(dest_raw)
        try:
            validate_skills_dir(dest, app_dir=self.paths.app_dir, public_dir=self.paths.public_dir, data_dir=self.paths.data_dir)
        except ValueError as exc:
            return None, ({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return None
        allowed = {self.config._resolve_dir(item) for item in configured}
        allowed.add(self.config._resolve_dir("skills"))
        allowed.add(managed)
        allowed.add((self.paths.app_dir / "skills").resolve())
        if dest not in allowed:
            return None, (
                {"error": "只能安装到已添加的 Skill 扫描目录，请先在上方添加该目录"},
                HTTPStatus.FORBIDDEN,
            )
            return None
        return (dest_raw, dest), None

    # ------------------------------------------------------------------
    # Skill 安装布局：散装上传必须先建立独立子目录
    # ------------------------------------------------------------------
    @staticmethod
    def _skill_relative_parts(raw: Any) -> list[str]:
        """把前端提交的相对路径规范化为安全的路径片段列表。"""
        relative = str(raw or "").replace("\\", "/").lstrip("/")
        return [part for part in relative.split("/") if part not in {"", "."}]

    @staticmethod
    def _clean_skill_dir_name(value: str) -> str:
        """清洗出可作为目录名的 Skill 标识，拒绝纯点号/空串等越界名字。"""
        cleaned = re.sub(r"[^\w.\-]+", "_", str(value or "").strip()).strip(" .-")
        return cleaned or "skill"

    def _hidden_relative_roots(self, directory: Path) -> set[str]:
        """返回 ``directory`` 中「已被用户删除（隐藏）」的 Skill 相对目录集合。

        启动时的内置同步与旧目录合并都会往托管目录补文件；如果不跳过这些目录，
        用户删掉的内置 Skill 会在下次启动被重新写回，表现为「删不掉」。
        """
        hidden = set(self.config.get_hidden_skill_ids())
        directory = Path(directory).expanduser().resolve()
        if not hidden or not directory.is_dir():
            return set()
        try:
            probe = SkillCatalog([directory], hidden_ids=[])
        except OSError:
            return set()
        roots: set[str] = set()
        for item in probe.scan():
            if str(item.get("id") or "") not in hidden:
                continue
            try:
                relative = Path(str(item.get("root") or "")).resolve().relative_to(directory)
            except (OSError, ValueError):
                continue
            posix = relative.as_posix().strip("/")
            if posix and posix != ".":
                roots.add(posix)
        return roots

    def _skill_subdir_prefix(self, layouts: list[list[str]], dest: Path, preferred_name: str) -> list[str]:
        """上传内容直接散放在导入根时，返回一个独立 Skill 子目录前缀，否则返回空列表。

        单文件 ``.md`` 导入会被提交为 ``path="SKILL.md"``。若直接写到扫描目录根下，该
        Skill 的 ``root`` 会等于扫描目录本身，删除时 ``delete_skill`` 会 ``shutil.move``
        整个目录，把该目录下的所有 Skill 一并搬走。因此凡是有文件直接落在导入根的上传，
        都先为它建立独立子目录。ZIP 顶层直接散放 ``SKILL.md`` 同理。
        """
        if not any(len(parts) == 1 for parts in layouts):
            return []
        return [_unique_dir(dest, self._clean_skill_dir_name(preferred_name)).name]

    def _migrate_bare_managed_skill(self, managed: Path) -> None:
        """将旧版托管目录根下的 SKILL.md 迁入独立目录，并保留其配置引用。"""
        skill_file = managed / "SKILL.md"
        if not skill_file.is_file():
            return
        try:
            before = next(
                item for item in SkillCatalog([managed]).scan()
                if Path(str(item.get("path") or "")).resolve() == skill_file.resolve()
            )
            text = skill_file.read_text(encoding="utf-8", errors="replace")
            directory = _unique_dir(
                managed,
                self._clean_skill_dir_name(_frontmatter_value(text, "name") or "skill"),
            )
            directory.mkdir(parents=True, exist_ok=False)
            shutil.move(str(skill_file), str(directory / skill_file.name))
            after_file = directory / skill_file.name
            after = next(
                item for item in SkillCatalog([managed]).scan()
                if Path(str(item.get("path") or "")).resolve() == after_file.resolve()
            )
        except (OSError, StopIteration) as exc:
            logger.warning("迁移裸 SKILL.md 失败：%s", exc)
            return

        old_id = str(before["id"])
        new_id = str(after["id"])
        if old_id == new_id:
            return
        changed = False
        with self.config.lock:
            for agent in self.config.data.get("agents", []):
                skill_ids = agent.get("skill_ids")
                if not isinstance(skill_ids, list) or old_id not in skill_ids:
                    continue
                agent["skill_ids"] = [new_id if item == old_id else item for item in skill_ids]
                changed = True
            hidden = self.config.data.get("hidden_skill_ids")
            if isinstance(hidden, list) and old_id in hidden:
                self.config.data["hidden_skill_ids"] = list(dict.fromkeys(
                    new_id if item == old_id else item for item in hidden
                ))
                changed = True
            if changed:
                self.config.save()
        logger.info("已迁移旧版裸 Skill：%s -> %s", skill_file, directory)

    @staticmethod
    def _preferred_skill_dir_name(entries: list[tuple[list[str], bytes]], fallback: str) -> str:
        """散装文件夹上传时推导子目录名：body.name → SKILL.md 的 frontmatter name → 文件名。"""
        for parts, data in entries:
            if parts[-1] == "SKILL.md" and len(parts) == 1:
                text = data.decode("utf-8", errors="replace")
                return _frontmatter_value(text, "name") or fallback or Path(parts[0]).stem
        return fallback or "skill"

    @staticmethod
    def _zip_skill_dir_name(archive: zipfile.ZipFile, members: list[Any], fallback: str) -> str:
        """散装压缩包上传时推导子目录名：顶层 SKILL.md 的 frontmatter name → 压缩包文件名。"""
        root_md = next(
            (
                member for member in members
                if not member.is_dir()
                and Path(member.filename).name == "SKILL.md"
                and len(Path(member.filename).parts) == 1
            ),
            None,
        )
        if root_md is None:
            return fallback
        try:
            text = archive.read(root_md).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 目录名推导失败不应阻断安装
            return fallback
        return _frontmatter_value(text, "name") or fallback



APP: NaibaChatApp


