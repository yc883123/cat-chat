from __future__ import annotations

from naiba.core.contracts import RunContext

import argparse
import base64
import hashlib
import gzip
import json
import mimetypes
import os
import ipaddress
import io
import re
import secrets
import shutil
import sqlite3
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
import zipfile
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 路径语义与目录分类见 naiba/paths.py：此处构建进程级 PathContext 并暴露同名模块级
# 常量（与既有引用面保持同名，语义不变；运行时二次重绑定见 NaibaChatApp.__init__）。
from naiba.paths import PathContext, default_path_context, static_asset_version

PC: PathContext = default_path_context()
EXE_DIR = PC.exe_dir
RESOURCE_DIR = PC.resource_dir
APP_DIR = PC.app_dir
PUBLIC_DIR = PC.public_dir
CONFIG_PATH = PC.config_path
DATA_DIR = PC.data_dir
STATUS_PATH = PC.status_path
LOCK_PATH = PC.lock_path

from naiba import net as net_io
from naiba.mcp import MCPRegistry
from naiba.llm.runtime import ModelRuntime
from naiba.plans import CraftToolExecutor, PlanManager, ReadOnlyToolExecutor, resolve_mode_tools
from naiba.skills.agent import SkillAgent
from naiba.skills.catalog import SkillCatalog
from naiba.skills.install import _zip_has_skill_md, delete_skill, remove_skill_references
from naiba.core.exceptions import ActiveRunError, TaskCancelled
from naiba.tools.executor import ToolExecutor
from naiba.run.manager import ConversationRunManager
from naiba.storage.store import ChatStorage
from naiba.updater import UpdateManager
from naiba.core.attachments import (
    MEDIA_PRODUCT_EXTS, _IMAGE_MEDIA_TERM_RE, _IMAGE_VIEW_ACTION_RE,
    _image_intent, _is_media_product_path, union_run_media,
)
from naiba.core.file_changes import FILE_MODIFY_TOOLS, file_changes_from_runs
from naiba.core.conv_files import (
    _CONV_FILE_READ_CAP, _CONV_FILE_SAVE_CAP, _CONV_FILE_SNIFF_BYTES, _CONV_IMAGE_EXTS,
    _conv_file_allow, _conv_file_open, _conv_file_save, _conv_file_target,
    _conv_touched_files, _conv_workspace_root,
)
from naiba.core.history import (
    CONTENT_READ_TOOLS, IMAGE_MEDIA_TYPES, MODEL_IMAGE_HISTORY_LIMIT,
    MODEL_IMAGE_MAX_EDGE, MODEL_IMAGE_TARGET_BYTES, _content_read_tool_outputs,
    _copy_model_trace_message, _debug_replay_digest, build_model_history, encode_image_for_model,
)
from naiba.core.choices import _detect_choice_groups, _detect_choices
from naiba.core.paths import path_within
from naiba.storage.media import (
    _clean_uploads_cache, _ensure_webp_thumb, _fit_image_pixels, _image_cache_dirs,
    _process_uploaded_image, _thumb_webp_path, _uploads_total_bytes,
    IMAGE_CACHE_CLEAN_LIMIT, IMAGE_SUFFIXES,
)
from naiba.core.diagnostics import CACHE_DEBUG_ON, _cache_debug_enabled


from naiba.core.cards import _decode_card_payload, parse_sillytavern_card
from naiba.core.migration import (
    _config_has_providers, _copy_legacy_data, _database_has_conversations,
    _merge_data_tree, _sync_bundled_skills, migrate_legacy_data,
)
from naiba.core.network import _is_usable_lan_ipv4, get_lan_ip, network_access_status
from naiba.app import NaibaChatApp
from naiba.http import AppHTTPServer, HEALTH_APP_MARKER, RequestHandler
from naiba.config import (
    ConfigStore,
    VALID_MODEL_KINDS,
    _context_window_source,
    _infer_context_window,
    _infer_kind_for_request_format,
    _infer_supports_images,
    built_in_agent_ids,
    built_in_agents,
    default_config,
    resolve_tool_preset,
    tool_catalog_entries,
    tool_group_entries,
    tool_preset_entries,
    validate_skills_dir,
)


def write_status(host: str, port: int, token: str) -> None:
    """兼容门面委托（3.4.3 收口）：实现见 naiba.http.write_status。"""
    from naiba.http import write_status as _impl

    _impl(host, port, token, PC)


def acquire_instance_lock():
    """兼容门面委托（3.4.3 收口）：实现见 naiba.http.acquire_instance_lock。"""
    from naiba.http import acquire_instance_lock as _impl

    return _impl(PC)


def main() -> None:
    """兼容门面委托（3.4.3 收口）：实现见 naiba.http.main_entry。"""
    global APP, DATA_DIR, STATUS_PATH, LOCK_PATH

    def _bind(instance, paths) -> None:
        global APP, DATA_DIR, STATUS_PATH, LOCK_PATH
        APP = instance
        DATA_DIR = paths.data_dir
        STATUS_PATH = paths.status_path
        LOCK_PATH = paths.lock_path

    from naiba.http import main_entry

    main_entry(PC, on_app=_bind)


if __name__ == "__main__":
    main()
