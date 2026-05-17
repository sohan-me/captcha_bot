import sys
import time
import logging
import asyncio
from api_solver import create_app


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

        try:
            await self.run_api_server()
        except KeyboardInterrupt:
            logger.warning("\nOperation cancelled by user")
        except Exception as e:
            logger.error(f"An error occurred: {str(e)}")
        finally:
            logger.info("Cloudflare Turnstile: Server stopped")


if __name__ == "__main__":
    tester = TurnstileTester()
    asyncio.run(tester.main())
