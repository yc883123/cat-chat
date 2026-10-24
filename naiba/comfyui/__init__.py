"""造梦机（ComfyUI 自动化工作台）：把用户跑通的 API 工作流收进库，填参反复出片。

独立工作台域（docs/plans/2026-10-23-ComfyUI自动化工作台模块计划.md）：
- 只依赖 config 的 ``comfyui_url``，不产生聊天 Run、不写回聊天消息；
- 执行代码与聊天域 ``naiba/jobs.py`` 的 ComfyUI worker **只共享口径**（seed 上限
  2^50、提交失败带服务端原因、历史+队列双查才判丢失），不共享代码、互不重构；
- 任务形态与打标器/Studio 同构：任务 JSON + 事件 JSONL + 游标轮询 + 启动恢复。
"""

from __future__ import annotations

COMFYUI_MODULE = "comfyui"
COMFYUI_ORIGIN_LABEL = "造梦机"
# 任务 id 前缀（工作台口径：studio st- / labeler tg- / comfyui cf-）。
COMFYUI_TASK_PREFIX = "cf"
COMFYUI_WORKFLOW_PREFIX = "cw"

COMFYUI_TASK_STATUSES = ("queued", "running", "completed", "failed", "cancelled", "interrupted")
COMFYUI_ACTIVE_STATUSES = ("queued", "running")
COMFYUI_RESUMABLE_STATUSES = ("interrupted", "failed")
# 事件类型：任务级 + 段级（WS 节点级进度留了后路，升级只改采集端）。
COMFYUI_EVENT_TYPES = (
    "status", "task_started", "shot_submitted", "shot_done", "shot_failed",
    "shot_lost", "task_done", "error", "log",
)

MAX_SHOTS = 200
# 单段轮询超时（对齐聊天域 jobs.py 的 30 分钟单段口径）。
SHOT_TIMEOUT_SECONDS = 1800
# 丢失判定的重提交预算（对齐 jobs.py max_resubmit 默认 1）。
MAX_RESUBMIT = 1
# 单段产物数量上限（防御异常工作流一次吐出海量文件拖垮落盘）。
MAX_FILES_PER_SHOT = 40
# 工作流条目参数槽位上限（防御超大工作流的扫描体积）。
MAX_WORKFLOW_PARAMS = 500
# 参数扫描的 schema 版本：升级识别规则（新增 role 等）后递增，工作流详情时
# 对旧版本条目重扫一次并落盘（幂等懒升级）。
PARAMS_SCHEMA_VERSION = 2
