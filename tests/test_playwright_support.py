import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from playwright_support import ChromiumInstallError, install_chromium, is_missing_chromium_error


class _FakeProcess:
    def __init__(self, *, returncode=0, output=b""):
        self.returncode = returncode
        self.output = output
        self.killed = False

    async def communicate(self):
        return self.output, None

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


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


class TestChromiumInstaller(unittest.IsolatedAsyncioTestCase):
    async def test_uses_current_python_module_entrypoint(self):
        process = _FakeProcess(output=b"Chromium installed")
        create = AsyncMock(return_value=process)
        with patch("playwright_support.asyncio.create_subprocess_exec", create):
            output = await install_chromium(
                python_executable="python-demo",
                timeout=5,
            )

        self.assertEqual(output, "Chromium installed")
        create.assert_awaited_once_with(
            "python-demo",
            "-m",
            "playwright",
            "install",
            "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def test_nonzero_exit_includes_installer_output(self):
        process = _FakeProcess(returncode=1, output=b"download failed")
        with patch(
            "playwright_support.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            with self.assertRaisesRegex(ChromiumInstallError, "download failed"):
                await install_chromium(python_executable="python-demo", timeout=5)


if __name__ == "__main__":
    unittest.main()
