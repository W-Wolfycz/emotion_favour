"""`_render_custom_t2i` 的模板注入与临时文件清理。

出图模板要被聊天正文填进 `<script>` 串里，两个静默面：
① 正文里的 `{{ ... }}` 与 `</script>` 必须原样/转义后落进模板（顺序错了会把插件
   本地路径渲进图里，转义漏了会提前闭合脚本块、让正文当脚本执行）；
② 临时 HTML 含聊天正文，渲染失败也必须删掉（漏删不报错，只会把正文留在磁盘上）。
真实浏览器渲染仍由部署端验收，这里只覆盖这两个不变量。

运行：
    python3 -m unittest tests.test_render_custom_t2i -v
"""
import asyncio
import hashlib
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


def _load_shared():
    """按路径加载 tests/_shared.py（pytest 下不能按包名 import）。"""
    path = Path(__file__).resolve().parent / "_shared.py"
    spec = importlib.util.spec_from_file_location("ef_tests_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_shared = _load_shared()
NullCtx = _shared.NullCtx
TasksStub = _shared.TasksStub
load_methods = _shared.load_methods

FONT_URL = "file:///plugins/emotion_favour/fonts/"
TEMPLATE = (
    "<html><head><style>@font-face{src:url('{{ font_base }}')}</style>"
    "<script>const W = {{ max_width }};</script></head>"
    "<body>{{ text | safe }}</body></html>"
)


class _Page:
    """Playwright page 的最小替身：把 goto 加载到的 HTML 交给用例，不真的渲染。"""

    def __init__(self, capture, *, fail_screenshot=False, fail_close=False):
        self.capture = capture
        self.fail_screenshot = fail_screenshot
        self.fail_close = fail_close

    async def goto(self, uri, wait_until=None, timeout=None):
        self.capture["uri"] = uri
        local = url2pathname(urlparse(uri).path)
        self.capture["html"] = Path(local).read_text(encoding="utf-8")

    async def evaluate(self, _expression):
        return None

    async def screenshot(self, path=None, full_page=True):
        if self.fail_screenshot:
            raise RuntimeError("screenshot failed")
        Path(path).write_bytes(b"png")
        self.capture["png"] = path

    async def close(self):
        self.capture["closed"] = True
        if self.fail_close:
            raise RuntimeError("close failed")


class _Browser:
    def __init__(self, page):
        self._page = page

    async def new_page(self, viewport=None):
        return self._page


class _Stub:
    def __init__(self, output_dir, capture, *, fail_screenshot=False, fail_close=False):
        self._browser_lock = NullCtx()
        self._page = _Page(capture, fail_screenshot=fail_screenshot, fail_close=fail_close)
        self._html_template = TEMPLATE
        self._plugin_version = "3.5.0"
        self._font_base_url = lambda: FONT_URL
        self._CDN_TO_FILE = {}
        self._local_scripts = {}
        self._t2i_output_dir = Path(output_dir)
        self._tasks = TasksStub()

    async def _ensure_browser_locked(self, *, auto_install=True):
        return _Browser(self._page)


class RenderCustomT2ITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_methods(
            ("_render_custom_t2i",),
            namespace={"asyncio": asyncio, "hashlib": hashlib, "re": re},
            stub_cls=_Stub,
        )

    def test_body_text_is_injected_last_and_escaped(self):
        """安全边界：正文最后注入（模板占位符先替换完）且 `<` 被转义。
        顺序反了会把插件本地路径渲进图里；漏转义会让正文里的 `</script>` 提前闭合
        脚本块——两者都不会报错，出图照样成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            capture = {}
            stub = _Stub(tmp, capture)
            md_text = "正文 {{ font_base }} {{ max_width }} 结束</script>"
            output = asyncio.run(stub._render_custom_t2i(md_text, width=640))
            self.assertTrue(str(output).endswith(".png"))

            html = capture["html"]
            head, injected = html.split("<body>", 1)
            self.assertIn(FONT_URL, head, "模板占位符应替换成字体根路径")
            self.assertIn("const W = 640;", head, "模板占位符应替换成宽度")
            self.assertIn("{{ font_base }}", injected, "正文占位符不能被二次替换")
            self.assertIn("{{ max_width }}", injected, "正文占位符不能被二次替换")
            self.assertNotIn(FONT_URL, injected)
            self.assertNotIn("</script>", injected, "正文里的 </script> 必须转义")
            self.assertIn(r"\x3c/script>", injected)

    def test_temp_html_is_removed_when_render_fails(self):
        """守渲染失败路径的落盘清理：临时 HTML 含聊天正文，截图失败与关页失败两条
        路径都不能把它留下（先删文件再关页）。漏删不报错，只会把聊天正文留在磁盘上
        并随失败次数累积。"""
        cases = {"截图失败": {"fail_screenshot": True}, "关页失败": {"fail_close": True}}
        for label, kwargs in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                capture = {}
                stub = _Stub(tmp, capture, **kwargs)
                with self.assertRaises(RuntimeError):
                    asyncio.run(stub._render_custom_t2i("聊天正文", width=640))
                self.assertEqual(list(Path(tmp).glob("*.html")), [])

if __name__ == "__main__":
    unittest.main()
