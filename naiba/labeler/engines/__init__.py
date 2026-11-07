"""打标引擎注册表。

``provider``：走 ``app.models.complete()``，模型来自设置里的供应商配置，
云端与本地后端一视同仁。``chat``：自定义打标选中 Skill 后走对话模块同款
SkillAgent 工具循环（模型可按需 read_file 读 Skill 参考文件），产出交给同一条
解析管线。后续若要接「应用内本地标注模型」（WD14 / JoyCaption），
按新的 ``engine`` 标识加适配层即可，不改动前端页面结构与任务契约。
"""

from __future__ import annotations

from typing import Any

from naiba.labeler.engines.chat import ChatSkillEngine
from naiba.labeler.engines.provider import ProviderLabelerEngine

ENGINES: dict[str, Any] = {"provider": ProviderLabelerEngine, "chat": ChatSkillEngine}
ENGINE_IDS = tuple(ENGINES)
DEFAULT_ENGINE = "provider"


def get_engine(engine_id: str) -> Any:
    key = str(engine_id or DEFAULT_ENGINE).strip() or DEFAULT_ENGINE
    engine = ENGINES.get(key)
    if engine is None:
        raise ValueError(f"未知的打标引擎：{key}")
    return engine
