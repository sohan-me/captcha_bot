import os
import sys
import time
import logging
import asyncio
from api_solver import create_app

# Headless Chromium on Linux needs a real UA string (Playwright + this project’s api_solver rules).
_DEFAULT_HEADLESS_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class CustomLogger(logging.Logger):
    COLORS = {
        'DEBUG': '\033[35m',    # Magenta
        'INFO': '\033[34m',     # Blue
        'SUCCESS': '\033[32m',  # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
    }
    RESET = '\033[0m'  # Reset color

    def format_message(self, level, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{self.COLORS.get(level, '')}{level}{self.RESET}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger = logging.getLogger("TurnstileTester")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


class TurnstileTester:
    async def run_api_server(self, debug=False, headless=False, useragent=None, browser_type="chromium", thread=1) -> None:
        """Run the API server with logging."""
        logger.info("Starting API server on http://localhost:5000")
        logger.info("API documentation available at http://localhost:5000/")

        try:
            app = create_app(
                debug=debug,
                headless=headless,
                useragent=useragent,
                browser_type=browser_type,
                thread=thread,
                proxy_support=False,
            )
            import hypercorn.asyncio
            config = hypercorn.Config()
            config.bind = ["127.0.0.1:5000"]
            await hypercorn.asyncio.serve(app, config)
        except Exception as e:
            logger.error(f"API server failed to start: {str(e)}")

    async def main(self) -> None:
        logger.info("Cloudflare Turnstile: Welcome — starting API server")

        want_headed = os.environ.get("TURNSTILE_HEADED", "").lower() in ("1", "true", "yes", "on")
        has_display = bool(os.environ.get("DISPLAY"))
        # VPS / systemd: no X11 → must use headless or xvfb-run.
        headless = not (want_headed and has_display)
        if want_headed and not has_display:
            logger.warning(
                "TURNSTILE_HEADED is set but DISPLAY is missing — using headless mode (required on a headless server)"
            )

        browser_type = (os.environ.get("TURNSTILE_BROWSER") or "chromium").strip() or "chromium"

        useragent = os.environ.get("TURNSTILE_USER_AGENT")
        if isinstance(useragent, str):
            useragent = useragent.strip() or None
        if headless and "camoufox" not in browser_type.lower():
            useragent = useragent or _DEFAULT_HEADLESS_UA

        logger.info(f"Browser: type={browser_type}, headless={headless}")

        try:
            await self.run_api_server(
                headless=headless,
                useragent=useragent,
                browser_type=browser_type,
            )
        except KeyboardInterrupt:
            logger.warning("\nOperation cancelled by user")
        except Exception as e:
            logger.error(f"An error occurred: {str(e)}")
        finally:
            logger.info("Cloudflare Turnstile: Server stopped")


if __name__ == "__main__":
    # Under PM2 / systemd, stdout is often not a TTY and may be fully block-buffered, so log
    # files stay at 0 bytes until exit unless we line-buffer or use PYTHONUNBUFFERED=1 / python -u.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass
    print("[captcha_bot] starting", flush=True)

    tester = TurnstileTester()
    asyncio.run(tester.main())
