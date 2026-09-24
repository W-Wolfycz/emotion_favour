"""tests/ 的共享测试基础设施。

集中「从 main.py 提取方法源码」的工具和几个通用桩对象，避免每个测试文件
各抄一套样板（AGENTS：测试基础设施要复用，不为每个测试文件重复搭环境）。

pytest 下 tests/ 不是包，跨测试文件不能按包名 import，统一由各测试文件
按路径加载本模块：

    _shared = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location(
            "ef_tests_shared", Path(__file__).resolve().parent / "_shared.py"))
    _shared.__spec__.loader.exec_module(_shared)

本地不安装 astrbot，所以凡是插件类方法一律走源码提取 + 桩执行，
不导入 `main.py`，也不导入真实 `astrbot.core`。
"""
import logging
import re
import sys
import textwrap
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
MAIN_PY = PLUGIN_DIR / "main.py"

if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def read_main_source() -> str:
    return MAIN_PY.read_text(encoding="utf-8")


def extract_method(src: str, name: str) -> str:
    """从 main.py 源码提取指定类方法的源（含 def 行），dedent 到顶层。

    扫描 `    def name(` / `    async def name(`（类内方法）起，到下一个同缩进
    def 或类外结构止；相邻方法之间的空行、注释、装饰器属于下一个方法，需要剔除，
    否则提取块会以装饰器结尾而语法错误。
    """
    lines = src.splitlines(keepends=True)
    start = None
    indent = ""
    for index, line in enumerate(lines):
        matched = re.match(r"^(\s*)(?:async\s+)?def " + re.escape(name) + r"\(", line)
        if matched and matched.group(1):  # 必须非零缩进（类内方法）
            start = index
            indent = matched.group(1)
            break
    if start is None:
        raise ValueError(f"未找到类方法 {name}")

    end = len(lines)
    pad_len = len(indent)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue  # 空行不算结构边界
        matched = re.match(r"^(\s*)(?:async\s+)?def ", line)
        if matched and len(matched.group(1)) == pad_len:
            end = index
            break
        if not (line.startswith(" ") or line.startswith("\t")):
            end = index
            break
    while end > start:
        tail = lines[end - 1].strip()
        if not tail or tail.startswith("#") or tail.startswith("@"):
            end -= 1
            continue
        break
    return textwrap.dedent("".join(lines[start:end]))


def load_methods(
    names,
    *,
    namespace=None,
    stub_cls=None,
    static_names=(),
    source=None,
):
    """批量提取 main.py 的方法到同一命名空间，可选绑回桩类。

    `static_names` 里的方法按 staticmethod 绑定（提取范围不含装饰器，
    否则方法之间互相调用时会把 self 当成第一个参数）。
    """
    namespace = dict(namespace or {})
    source = source if source is not None else read_main_source()
    prefix = "from __future__ import annotations\n"
    for name in names:
        exec(prefix + extract_method(source, name), namespace)  # noqa: S102
        if stub_cls is not None:
            func = namespace[name]
            setattr(stub_cls, name, staticmethod(func) if name in static_names else func)
    return namespace


def extract_top_function(src: str, name: str) -> str:
    """提取模块顶层函数（domain.py / utils.py 这类无类模块的方法）。"""
    lines = src.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if re.match(rf"^(?:async\s+)?def {re.escape(name)}\(", line):
            start = index
            break
    if start is None:
        raise ValueError(f"未找到顶层函数 {name}")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^(?:async\s+)?def |^class |^[A-Z][A-Z_]* = ", lines[index]):
            end = index
            break
    return "".join(lines[start:end])


def load_top_function(module_path, name: str, *, namespace=None):
    """从模块文件里提取顶层函数并 exec，返回函数对象，供测试直接调用。"""
    source = Path(module_path).read_text(encoding="utf-8")
    scope = dict(namespace or {})
    prefix = "from __future__ import annotations\n"
    exec(prefix + extract_top_function(source, name), scope)  # noqa: S102
    return scope[name]


def extract_class_attribute(src: str, name: str) -> str:
    """提取类属性赋值块（支持跨行，如 _CTX_CLEAN_PATTERN = re.compile(… )）。"""
    lines = src.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(name)} = ", line):
            start = index
            break
    if start is None:
        raise ValueError(f"未找到类属性 {name}")
    block = []
    depth = 0
    for index in range(start, len(lines)):
        block.append(lines[index])
        depth += lines[index].count("(") - lines[index].count(")")
        if depth <= 0:
            return textwrap.dedent("".join(block))
    raise ValueError(f"类属性 {name} 的赋值块不完整")


class EventStub:
    """AstrMessageEvent 的最小替身：只需要 result 构造器与 umo。"""

    unified_msg_origin = "platform_demo:GroupMessage:group_demo"

    def __init__(self):
        self.sent = []

    async def send(self, result):
        self.sent.append(result)

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, path):
        return ("image", path)


class TasksStub:
    """TaskSupervisor 的替身：记录 spawn 名称并关闭未消费的协程。"""

    def __init__(self):
        self.spawned = []

    def spawn(self, coro, name=None):
        self.spawned.append(name)
        coro.close()


class NullCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class LocksStub:
    """KeyedLockPool 的替身：记录 key，但不真正加锁。"""

    def __init__(self):
        self.keys = []

    def hold(self, key):
        self.keys.append(key)
        return NullCtx()


def make_logger(name: str = "emotion_favour_test") -> logging.Logger:
    return logging.getLogger(name)
