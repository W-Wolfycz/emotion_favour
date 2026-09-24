"""Playwright「浏览器缺失」错误识别。

`is_missing_chromium_error` 认的是上游 Playwright 的报错文案：这是被下游依赖的
协议字符串，文案对不上不会报错，只会让 Chromium 自愈悄悄不再触发（用户只看到
出图回退，没有任何提示），所以留一条盯住。

运行：
    python3 -m unittest tests.test_playwright_support -v
"""
import unittest

from playwright_support import is_missing_chromium_error


class TestMissingChromiumDetection(unittest.TestCase):
    def test_accepts_playwright_missing_executable_message(self):
        """守「自愈静默失灵」：识别规则与上游文案脱节后，浏览器缺失不再自动安装，
        每次出图都回退纯文本，日志里只有一句普通 T2I 失败，不会指向真因。"""
        error = RuntimeError(
            "BrowserType.launch: Executable doesn't exist at /cache/chromium/headless_shell\n"
            "Please run the following command to download new browsers:\n"
            "playwright install"
        )
        self.assertTrue(is_missing_chromium_error(error))

if __name__ == "__main__":
    unittest.main()
