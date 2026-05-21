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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class TurnstileTester:
    async def run_api_server(
        self,
        debug=False,
        headless=False,
        useragent=None,
        browser_type="chromium",
        thread=1,
        bind_host="127.0.0.1",
        bind_port=5000,
        proxy_support=False,
    ) -> None:
        """Run the API server with logging."""
        bind = f"{bind_host}:{bind_port}"
        logger.info(f"Starting API server on http://{bind}")
        logger.info(f"API documentation available at http://{bind}/")

        try:
            app = create_app(
                debug=debug,
                headless=headless,
                useragent=useragent,
                browser_type=browser_type,
                thread=thread,
                proxy_support=proxy_support,
            )
            import hypercorn.asyncio
            config = hypercorn.Config()
            config.bind = [bind]
            await hypercorn.asyncio.serve(app, config)
        except Exception as e:
            logger.error(f"API server failed to start: {str(e)}")

    async def main(self) -> None:
        logger.info("Cloudflare Turnstile: Welcome — starting API server")

        want_headed = os.environ.get("TURNSTILE_HEADED", "").lower() in ("1", "true", "yes", "on")
        # Unix/Linux: headed needs DISPLAY. Windows: no DISPLAY; use headed when TURNSTILE_HEADED is set.
        if os.name == "nt":
            can_headed = want_headed
        else:
            can_headed = want_headed and bool(os.environ.get("DISPLAY"))
        headless = not can_headed
        if want_headed and not can_headed:
            logger.warning(
                "TURNSTILE_HEADED is set but there is no GUI display (DISPLAY unset on Unix) — using headless mode."
            )

        # Default matches Archive start_server.bat / api_solver.py (camoufox).
        browser_type = (os.environ.get("TURNSTILE_BROWSER") or "camoufox").strip() or "camoufox"

        useragent = os.environ.get("TURNSTILE_USER_AGENT")
        if isinstance(useragent, str):
            useragent = useragent.strip() or None
        if headless and "camoufox" not in browser_type.lower():
            useragent = useragent or _DEFAULT_HEADLESS_UA

        bind_host = (os.environ.get("TURNSTILE_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        bind_port = _env_int("TURNSTILE_PORT", 5000)
        thread = max(1, _env_int("TURNSTILE_THREAD", 1))
        proxy_support = _env_bool("TURNSTILE_PROXY", False)

        logger.info(
            f"Browser: type={browser_type}, headless={headless}, thread={thread}, proxy_support={proxy_support}"
        )
        if proxy_support:
            proxies_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")
            if os.path.isfile(proxies_path):
                logger.info(f"Proxy pool: proxies.txt (random per solve when task has no proxy)")
            else:
                logger.warning(
                    "TURNSTILE_PROXY is on but proxies.txt is missing — only per-task proxy= in API requests will work."
                )

        try:
            await self.run_api_server(
                headless=headless,
                useragent=useragent,
                browser_type=browser_type,
                thread=thread,
                bind_host=bind_host,
                bind_port=bind_port,
                proxy_support=proxy_support,
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
