"""包内日志出口：直接使用 AstrBot 核心 logger。

AstrBot 4.27+ 的 ``astrbot.api.logger`` 会按调用方模块把日志路由到插件专属
logger（``astrbot.plugin.emotion_favour``），日志等级可在 WebUI 插件详情页
独立调整（运行期生效）。因此不再需要本插件的 ``debug_to_info`` 提级开关，
各模块保持 ``from .log import logger`` 即可。
"""

from astrbot.api import logger

__all__ = ["logger"]
