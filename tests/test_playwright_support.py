import unittest
from unittest.mock import AsyncMock, patch

from playwright_support import ChromiumInstallError, install_chromium, is_missing_chromium_error

class TestMissingChromiumDetection(unittest.TestCase):
    def test_accepts_playwright_missing_executable_message(self):
        error = RuntimeError(
            "BrowserType.launch: Executable doesn't exist at /cache/chromium/headless_shell\n"
            "Please run the following command to download new browsers:\n"
            "playwright install"
        )
        self.assertTrue(is_missing_chromium_error(error))

    def test_rejects_unrelated_launch_failure(self):
        self.assertFalse(is_missing_chromium_error(RuntimeError("Target page has been closed")))
        self.assertFalse(is_missing_chromium_error(RuntimeError("Host system is missing dependencies")))

if __name__ == "__main__":
    unittest.main()
