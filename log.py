"""包内日志 wrapper：支持 ``debug_to_info`` 把 debug 日志提级为 info 输出。

各模块仍按 ``logger.debug/info/warning/error`` 调用，本文件包装后根据配置
决定 debug 实际走 info 还是 debug 通道，让用户无需改 AstrBot 后端日志级别
即可看到详细运行信息（与 chat_memory 插件的 ``debug_to_info`` 一致）。
"""

from astrbot.api import logger as _astrbot_logger


class _LoggerProxy:
    """转发到 astrbot logger，但 ``debug`` 受 ``debug_to_info`` 控制。"""

    def __init__(self):
        self.debug_to_info = False

    def debug(self, msg, *args, **kwargs):
        if self.debug_to_info:
            _astrbot_logger.info(msg, *args, **kwargs)
        else:
            _astrbot_logger.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        _astrbot_logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        _astrbot_logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        _astrbot_logger.error(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        _astrbot_logger.critical(msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        _astrbot_logger.exception(msg, *args, **kwargs)


logger = _LoggerProxy()


def configure(debug_to_info: bool) -> None:
    """启动时由 main.py 调用，根据配置开关提级。"""
    logger.debug_to_info = bool(debug_to_info)
