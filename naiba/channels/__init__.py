"""QQ 渠道（NapCatQQ / OneBot v11 反向 WebSocket）。

模块边界：NapCat 只负责登录 QQ 与收发消息；这里只做协议解析、身份过滤、
会话绑定与投递状态，模型调用、工具执行与权限仍然走 ``naiba/run/`` 的既有链路。
``naiba/run/`` 不得反向依赖本包（见项目维护说明 §3 依赖方向）。
"""

from __future__ import annotations

from .service import OneBotChannelService

__all__ = ["OneBotChannelService"]
