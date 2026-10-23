"""打标器（Labeler）：批量给图片打标签/描述，结果落本地标注库。

边界（与 Studio 同构、但不共用任务状态机）：
- 输入只收**服务端已校验的受管 uploads 引用**；前端文件夹拖入先经 `/api/uploads`
  分批上传，再提交引用。任务里不持久化用户原始目录的绝对路径。
- 逐图状态、标注结果、用量与任务计数在 ``labeler.db`` 的**同一个事务**里提交；
  ``data/labeler/tasks/<id>.events.jsonl`` 只是可重建的通知日志，不作为恢复依据。
- 引擎按 ``engine`` 标识扩展；本期只实现 ``provider``：模型来自设置里的供应商配置，
  云端与本地后端（Ollama / LM Studio / llama.cpp）都走同一条 ``complete()``。
- **不在应用内加载本地小模型**（没有 torch / WD14 / JoyCaption）：包体积与启动速度都不允许。
"""

# 任务状态机（store / jobs / service / 前端共用一份口径）。
LABELER_TASK_STATUSES: tuple[str, ...] = (
    "queued", "running", "done", "failed", "cancelled", "interrupted",
)
# 活跃（cache 引用保护、取消、恢复判定共用）。
LABELER_ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running")
# 可「继续未完成」的状态。
LABELER_RESUMABLE_STATUSES: tuple[str, ...] = ("interrupted", "failed", "cancelled")

# 逐图状态：uncertain = 模型请求已发出、但结果未落库就中断（可能已计费）。
LABELER_ITEM_STATES: tuple[str, ...] = (
    "queued", "running", "succeeded", "failed", "cancelled", "uncertain",
)

# 事件类型：只用于展示与增量轮询，恢复一律以 SQLite 为准。
LABELER_EVENT_TYPES: tuple[str, ...] = (
    "status", "image_done", "image_failed", "usage", "done", "cancelled", "error",
)
LABELER_TERMINAL_EVENT_TYPES: tuple[str, ...] = ("done", "cancelled", "error")

LABELER_MODULE = "labeler"
LABELER_ORIGIN_LABEL = "打标器"
