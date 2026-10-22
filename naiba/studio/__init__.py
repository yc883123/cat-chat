"""Cat Chat 工作台（Studio）——独立生成域（不依赖对话 Run/会话生命周期）。

模块分工（_studio_plan_v2.md「代码归属与依赖边界」）：
- catalog   只读预设目录（public/studio/ 的索引 + 按需正文）
- custom    用户自定义条目（每条一个 JSON，data_dir/studio/custom/）
- store     Studio 任务持久化（data_dir/studio/tasks/）
- runner    模型执行（串行队列，直调 ModelRuntime.complete()）
- jobs      生成任务管理（提交/查询/取消/重试/启动恢复）
- service   业务编排与 /api/studio/* 业务方法
- handoff   已选 Agent 的普通会话交接 adapter（由组装根注入回调）
"""

from naiba.studio.service import StudioService

__all__ = ["StudioService"]
