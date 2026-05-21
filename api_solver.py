import os
import sys
import html
import time
import uuid
import json
import random
import secrets
import logging
import asyncio
import argparse
import atexit
from collections import deque
from contextlib import suppress
from quart import Quart, request, jsonify, session, redirect
from werkzeug.security import check_password_hash
from camoufox.async_api import AsyncCamoufox
from patchright.async_api import async_playwright


COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger = logging.getLogger("TurnstileAPIServer")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)

# Default "Live test" form on `/` (index page)
DEFAULT_LIVE_TEST_URL = "https://appointment.ivacbd.com/signin"
DEFAULT_LIVE_TEST_SITEKEY = "0x4AAAAAACghKkJHL1t7UkuZ"

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_JSON_PATH = os.path.join(_APP_DIR, "admin.json")

# Extra Chromium flags to trim background work (Playwright chromium/chrome/msedge only).
_CHROMIUM_LOW_RESOURCE_ARGS = (
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
)

RESULTS_SAVE_DEBOUNCE_SEC = 15.0


def playwright_proxy_from_string(proxy_str: str) -> dict:
    """Build a Playwright proxy dict from common client formats.

    Supported:
      - http:host:port
      - http:host:port:user:pass   (IVAC / VFS captcha bot format)
      - host:port                  (http assumed)
      - http://host:port
      - http://user:pass@host:port
    """
    s = (proxy_str or "").strip()
    if not s:
        raise ValueError("Empty proxy string")

    if "://" in s:
        from urllib.parse import urlparse

        parsed = urlparse(s)
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"Invalid proxy URL: {proxy_str!r}")
        scheme = parsed.scheme or "http"
        server = f"{scheme}://{parsed.hostname}:{parsed.port}"
        if parsed.username:
            return {
                "server": server,
                "username": parsed.username,
                "password": parsed.password or "",
            }
        return {"server": server}

    parts = s.split(":")
    if len(parts) == 2:
        host, port = parts
        return {"server": f"http://{host}:{port}"}
    if len(parts) == 3:
        scheme, host, port = parts
        return {"server": f"{scheme}://{host}:{port}"}
    if len(parts) == 5:
        scheme, host, port, user, passwd = parts
        return {
            "server": f"{scheme}://{host}:{port}",
            "username": user,
            "password": passwd,
        }
    raise ValueError(
        f"Invalid proxy format: {proxy_str!r}. "
        "Use http:host:port or http:host:port:user:pass (or a full http:// URL)."
    )


class TurnstileAPIServer:
    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Cloudflare Turnstile Solver</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async></script>
        <script>
            async function fetchIP() {
                try {
                    const response = await fetch('https://api64.ipify.org?format=json');
                    const data = await response.json();
                    document.getElementById('ip-display').innerText = `Your IP: ${data.ip}`;
                } catch (error) {
                    console.error('Error fetching IP:', error);
                    document.getElementById('ip-display').innerText = 'Failed to fetch IP';
                }
            }
            window.onload = fetchIP;
        </script>
    </head>
    <body class="flex min-h-screen flex-col items-center justify-center gap-8 bg-zinc-950 p-6 font-sans text-zinc-300 antialiased">
        <div class="w-full max-w-md space-y-6 text-center">
            <div class="flex justify-center rounded-xl border border-zinc-800/80 bg-zinc-900/50 p-6 shadow-xl shadow-black/30">
                <!-- cf turnstile -->
            </div>
            <p id="ip-display" class="text-sm text-zinc-500">Fetching your IP...</p>
        </div>
    </body>
    </html>
    """

    def __init__(self, headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool):
        self.app = Quart(__name__)
        secret_key = os.environ.get("SECRET_KEY")
        if not secret_key:
            secret_key = secrets.token_hex(32)
            logger.warning(
                "SECRET_KEY is not set; using a random key. "
                "Admin sessions reset on every restart. Set SECRET_KEY for stable cookies."
            )
        self.app.secret_key = secret_key
        self.debug = debug
        self.results = self._load_results()
        self.api_keys, self.api_key_records = self._load_api_keys()
        self.usage = self._load_usage()
        self.browser_type = browser_type
        self.headless = headless
        self.useragent = useragent
        self.max_thread_count = max(1, thread)
        self.thread_count = self.max_thread_count
        self.current_browser_count = 0
        self.multi_thread = False  # Set to OFF by default to save resources
        self.pool_adjustment_lock = asyncio.Lock()
        self.shelf = {}
        self.shelf_lock = asyncio.Lock()
        self.shelf_max_age_seconds = 50.0
        self.shelf_prefill_url = DEFAULT_LIVE_TEST_URL
        self.shelf_prefill_sitekey = DEFAULT_LIVE_TEST_SITEKEY
        self.shelf_prefill_action = ""
        self.shelf_prefill_cdata = ""
        self.shelf_running = False
        self._shelf_loop_task = None
        self.proxy_support = proxy_support
        self._proxies_cache_path = None
        self._proxies_cache_mtime = None
        self._proxies_cache_list = []
        self._results_save_task = None
        self.browser_pool = asyncio.Queue()
        self.browser_args = []
        self.playwright = None
        self.camoufox = None
        if useragent:
            self.browser_args.append(f"--user-agent={useragent}")

        self._setup_routes()
        atexit.register(self._flush_results_to_disk_sync_at_exit)

    def _get_proxies_list_cached(self):
        """Load proxies.txt with mtime cache to avoid re-reading on every solve."""
        path = os.path.join(os.getcwd(), "proxies.txt")
        if not os.path.isfile(path):
            self._proxies_cache_path = path
            self._proxies_cache_mtime = None
            self._proxies_cache_list = []
            return []
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return self._proxies_cache_list if self._proxies_cache_path == path else []
        if self._proxies_cache_path == path and self._proxies_cache_mtime == mtime:
            return self._proxies_cache_list
        try:
            with open(path, encoding="utf-8") as proxy_file:
                lines = [
                    line.strip()
                    for line in proxy_file
                    if line.strip() and not line.strip().startswith("#")
                ]
        except OSError as e:
            if self.debug:
                logger.warning(f"Could not read proxies.txt: {e}")
            return self._proxies_cache_list if self._proxies_cache_path == path else []
        self._proxies_cache_path = path
        self._proxies_cache_mtime = mtime
        self._proxies_cache_list = lines
        return lines

    async def _request_debounced_results_save(self):
        """Coalesce results.json writes; flushes RESULTS_SAVE_DEBOUNCE_SEC after the last success."""
        if self._results_save_task is not None and not self._results_save_task.done():
            self._results_save_task.cancel()
        self._results_save_task = asyncio.create_task(self._debounced_flush_results_delay())

    async def _debounced_flush_results_delay(self):
        task = asyncio.current_task()
        try:
            await asyncio.sleep(RESULTS_SAVE_DEBOUNCE_SEC)
            self._flush_results_to_disk_sync()
        except asyncio.CancelledError:
            raise
        finally:
            if self._results_save_task is task:
                self._results_save_task = None

    def _flush_results_to_disk_sync(self):
        """Write self.results to results.json (synchronous)."""
        try:
            with open("results.json", "w") as result_file:
                json.dump(self.results, result_file, indent=4)
        except IOError as e:
            logger.error(f"Error saving results to file: {str(e)}")

    def _flush_results_to_disk_sync_at_exit(self):
        """Best-effort flush on process exit (sync; event loop may be gone)."""
        t = self._results_save_task
        if t is not None and not t.done():
            t.cancel()
        self._flush_results_to_disk_sync()

    @staticmethod
    def _load_results():
        """Load previous results from results.json."""
        try:
            if os.path.exists("results.json"):
                with open("results.json", "r") as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error loading results: {str(e)}. Starting with an empty results dictionary.")
        return {}

    @staticmethod
    def _load_api_keys():
        """Load API keys from keys.json."""
        try:
            if os.path.exists("keys.json"):
                with open("keys.json", "r") as f:
                    raw_keys = json.load(f)
                    api_keys = []
                    records = {}
                    if isinstance(raw_keys, list):
                        for item in raw_keys:
                            if isinstance(item, str):
                                key = item
                                record = {"key": key, "userName": "", "expiryDate": "", "enabled": True}
                            elif isinstance(item, dict):
                                key = item.get("key", "")
                                if not key:
                                    continue
                                record = {
                                    "key": key,
                                    "userName": item.get("userName", ""),
                                    "expiryDate": item.get("expiryDate", ""),
                                    "enabled": item.get("enabled", True),
                                }
                            else:
                                continue
                            api_keys.append(key)
                            records[key] = record
                    return api_keys, records
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error loading keys: {str(e)}. Starting with empty keys list.")
        return [], {}

    def _save_api_keys(self):
        """Save API keys to keys.json."""
        try:
            with open("keys.json", "w") as f:
                json.dump([self.api_key_records.get(k, {"key": k, "userName": "", "expiryDate": "", "enabled": True}) for k in self.api_keys], f, indent=4)
        except IOError as e:
            logger.error(f"Error saving keys to file: {str(e)}")

    @staticmethod
    def _load_usage():
        """Load API key usage from usage.json."""
        try:
            if os.path.exists("usage.json"):
                with open("usage.json", "r") as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error loading usage: {str(e)}. Starting with empty usage dict.")
        return {}

    def _save_usage(self):
        """Save API key usage to usage.json."""
        try:
            with open("usage.json", "w") as f:
                json.dump(self.usage, f, indent=4)
        except IOError as e:
            logger.error(f"Error saving usage to file: {str(e)}")

    def _increment_usage(self, client_key: str):
        if client_key not in self.usage:
            self.usage[client_key] = 0
        self.usage[client_key] += 1
        self._save_usage()

    def _is_key_expired(self, key: str) -> bool:
        record = self.api_key_records.get(key, {})
        expiry = (record.get("expiryDate") or "").strip()
        if not expiry:
            return False
        try:
            return time.strftime("%Y-%m-%d") > expiry
        except Exception:
            return False

    def _ensure_key_status(self, key: str) -> None:
        if key not in self.api_key_records:
            self.api_key_records[key] = {"key": key, "userName": "", "expiryDate": "", "enabled": True}
        if self._is_key_expired(key) and self.api_key_records[key].get("enabled", True):
            self.api_key_records[key]["enabled"] = False
            self._save_api_keys()

    def _validate_api_key(self, key: str):
        if not key or key not in self.api_keys:
            return False, "ERROR_KEY_DOES_NOT_EXIST", "Invalid or missing API key"
        self._ensure_key_status(key)
        record = self.api_key_records.get(key, {})
        if not record.get("enabled", True):
            if self._is_key_expired(key):
                return False, "ERROR_KEY_EXPIRED", "API key has expired"
            return False, "ERROR_KEY_DISABLED", "API key is disabled"
        return True, "", ""

    def _api_key_rows(self):
        rows = []
        for key in self.api_keys:
            self._ensure_key_status(key)
            record = self.api_key_records.get(key, {})
            rows.append({
                "key": key,
                "userName": record.get("userName", ""),
                "expiryDate": record.get("expiryDate", ""),
                "enabled": record.get("enabled", True),
                "expired": self._is_key_expired(key),
                "usage": self.usage.get(key, 0),
            })
        return rows

    @staticmethod
    def _shelf_key(url, sitekey, action, cdata):
        u = (url or "").strip().rstrip("/")
        return (u, (sitekey or "").strip(), action or "", cdata or "")

    def _prune_shelf_key(self, key):
        dq = self.shelf.get(key)
        if not dq:
            return
        now = time.time()
        max_age = float(self.shelf_max_age_seconds)
        while dq and (now - dq[0]["created"]) > max_age:
            dq.popleft()
        if not dq:
            del self.shelf[key]

    @staticmethod
    def _load_admin_record():
        try:
            if os.path.exists(ADMIN_JSON_PATH):
                with open(ADMIN_JSON_PATH, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("username") and data.get("password_hash"):
                    return data
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load admin.json: {e}")
        return None

    def _require_admin_session(self):
        if not session.get("admin"):
            return jsonify({"success": False, "error": "Unauthorized", "loginRequired": True}), 401
        return None

    @staticmethod
    def _render_login_page(error=None, notice=None):
        err_html = (
            f'<p class="mb-4 rounded-lg border border-rose-500/30 bg-rose-950/40 px-3 py-2 text-sm text-rose-200">{html.escape(error)}</p>'
            if error
            else ""
        )
        note_html = (
            f'<p class="mb-4 rounded-lg border border-cyan-500/25 bg-cyan-950/30 px-3 py-2 text-sm text-cyan-100/90">{html.escape(notice)}</p>'
            if notice
            else ""
        )
        tailwind_inline = (
            "        tailwind.config = { theme: { extend: { fontFamily: "
            "{ sans: ['DM Sans', 'system-ui', 'sans-serif'] } } } } };"
        )
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin login — Cloudflare Turnstile Solver</title>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700&display=swap" rel="stylesheet">
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
{tailwind_inline}
    </script>
</head>
<body class="min-h-screen bg-zinc-950 font-sans text-zinc-300 antialiased">
    <div class="pointer-events-none fixed inset-0 bg-[radial-gradient(ellipse_100%_60%_at_50%_-20%,rgba(34,211,238,0.07),transparent_55%)]"></div>
    <div class="relative mx-auto flex min-h-screen max-w-md flex-col justify-center px-4 py-12">
        <p class="mb-1 text-center text-[10px] font-semibold uppercase tracking-[0.22em] text-zinc-500">Cloudflare Turnstile solver</p>
        <h1 class="mb-8 text-center text-xl font-semibold tracking-tight text-white">Admin sign in</h1>
        <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-6 shadow-xl shadow-black/30">
            {note_html}
            {err_html}
            <form method="post" action="/login" class="space-y-4">
                <div>
                    <label for="username" class="mb-1 block text-sm font-medium text-zinc-400">Username</label>
                    <input id="username" name="username" type="text" autocomplete="username" required
                        class="w-full rounded-lg border border-zinc-800 bg-zinc-950/80 px-3 py-2 text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-2 focus:ring-cyan-500/40" />
                </div>
                <div>
                    <label for="password" class="mb-1 block text-sm font-medium text-zinc-400">Password</label>
                    <input id="password" name="password" type="password" autocomplete="current-password" required
                        class="w-full rounded-lg border border-zinc-800 bg-zinc-950/80 px-3 py-2 text-zinc-100 focus:outline-none focus:ring-2 focus:ring-cyan-500/40" />
                </div>
                <button type="submit" class="w-full rounded-lg bg-cyan-600 py-2.5 text-sm font-semibold text-white shadow-lg shadow-cyan-950/25 transition hover:bg-cyan-500">Sign in</button>
            </form>
        </div>
    </div>
</body>
</html>"""

    async def login_page(self):
        if request.method == "GET":
            if session.get("admin"):
                return redirect("/")
            rec = self._load_admin_record()
            if not rec:
                return self._render_login_page(
                    notice="No admin account yet. From this folder run: python admin.py"
                )
            return self._render_login_page()
        form = await request.form
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        rec = self._load_admin_record()
        if not rec:
            return self._render_login_page(error="Admin is not configured. Run python admin.py"), 200
        if username != rec.get("username") or not check_password_hash(rec["password_hash"], password):
            return self._render_login_page(error="Invalid username or password"), 200
        session.clear()
        session["admin"] = True
        return redirect("/")

    async def logout(self):
        session.pop("admin", None)
        return redirect("/login")

    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.route("/login", methods=["GET", "POST"])(self.login_page)
        self.app.route("/logout", methods=["POST"])(self.logout)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/createTask', methods=['POST'])(self.create_task_api)
        self.app.route('/getTaskResult', methods=['POST'])(self.get_task_result_api)
        self.app.route('/generateKey', methods=['POST'])(self.generate_key_api)
        self.app.route('/removeKey', methods=['POST'])(self.remove_key_api)
        self.app.route('/toggleKey', methods=['POST'])(self.toggle_key_api)
        self.app.route('/updateKeyExpiry', methods=['POST'])(self.update_key_expiry_api)
        self.app.route('/getUsage', methods=['GET'])(self.get_usage_api)
        self.app.route('/toggleMultiThread', methods=['POST'])(self.toggle_multi_thread_api)
        self.app.route('/getMultiThreadStatus', methods=['GET'])(self.get_multi_thread_status_api)
        self.app.route('/setThreadCount', methods=['POST'])(self.set_thread_count_api)
        self.app.route('/getShelfStatus', methods=['GET'])(self.get_shelf_status_api)
        self.app.route('/setShelfSettings', methods=['POST'])(self.set_shelf_settings_api)
        self.app.route('/shelfControl', methods=['POST'])(self.shelf_control_api)
        self.app.route('/')(self.index)

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        logger.info("Starting browser initialization in the background")
        asyncio.create_task(self._initialize_browser_safe())

    async def _initialize_browser_safe(self) -> None:
        try:
            await self._initialize_browser()
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""

        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            self.playwright = await async_playwright().start()
        elif self.browser_type == "camoufox":
            self.camoufox = AsyncCamoufox(headless=self.headless, window=(800, 600))

        target = self.thread_count if self.multi_thread else 1
        for _ in range(target):
            self.current_browser_count += 1
            browser = await self._launch_browser_instance()

            await self.browser_pool.put((self.current_browser_count, browser))

            if self.debug:
                logger.success(f"Browser {self.current_browser_count} initialized successfully")

        logger.success(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")

    async def _launch_browser_instance(self):
        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            launch_args = list(self.browser_args) + list(_CHROMIUM_LOW_RESOURCE_ARGS)
            return await self.playwright.chromium.launch(
                channel=self.browser_type,
                headless=self.headless,
                args=launch_args
            )
        if self.browser_type == "camoufox":
            return await self.camoufox.start()
        raise ValueError(f"Unsupported browser type: {self.browser_type}")


    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: str = None, cdata: str = None, proxy: str = None, useragent: str = None, client_key: str = None):
        """Solve the Turnstile challenge."""
        selected_proxy = proxy

        index, browser = await self.browser_pool.get()
        context = None
        recreate_browser = False

        start_time = time.time()

        async def _solve_core():
            nonlocal context, selected_proxy

            async def _new_context_with_proxy(proxy_line: str):
                pw_proxy = playwright_proxy_from_string(proxy_line)
                return await browser.new_context(proxy=pw_proxy, user_agent=useragent)

            if selected_proxy:
                context = await _new_context_with_proxy(selected_proxy)
            elif self.proxy_support:
                proxies = self._get_proxies_list_cached()
                selected_proxy = random.choice(proxies) if proxies else None
                if selected_proxy:
                    context = await _new_context_with_proxy(selected_proxy)
                else:
                    context = await browser.new_context(user_agent=useragent)
            else:
                context = await browser.new_context(user_agent=useragent)

            page = await context.new_page()

            if self.debug:
                logger.debug(f"Browser {index}: Starting Turnstile solve for URL: {url} with Sitekey: {sitekey} | Proxy: {selected_proxy}")
                logger.debug(f"Browser {index}: Setting up page data and route")

            url_with_slash = url + "/" if not url.endswith("/") else url
            turnstile_div = f'<div class="cf-turnstile" style="background: white;" data-sitekey="{sitekey}"' + (f' data-action="{action}"' if action else '') + (f' data-cdata="{cdata}"' if cdata else '') + '></div>'
            page_data = self.HTML_TEMPLATE.replace("<!-- cf turnstile -->", turnstile_div)

            # Serve our own HTML for the navigation so we never hit any page-level
            # Cloudflare challenge on the target hostname (e.g. visa.vfsglobal.com).
            # The page's window.location.origin will still be the real hostname,
            # which is what Turnstile needs for sitekey/origin validation.
            async def _fulfill_with_template(route):
                try:
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=page_data)
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: route.fulfill error (falling back to continue): {e}")
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            # Intercept all navigation requests to the target hostname so we
            # always serve our template regardless of Cloudflare redirects or
            # interstitial pages (/cdn-cgi/challenge, /cdn-cgi/challenge-platform/…).
            from urllib.parse import urlparse as _urlparse
            _parsed = _urlparse(url)
            _origin = f"{_parsed.scheme}://{_parsed.netloc}"
            await page.route(f"{_origin}/**", _fulfill_with_template)
            # Also intercept the exact root URL as a fallback
            await page.route(url_with_slash, _fulfill_with_template)

            try:
                # Navigate to the real URL. The route handler intercepts and serves
                # our template, establishing window.location.origin = target hostname
                # which Turnstile needs for sitekey/origin validation.
                await page.goto(url_with_slash, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Initial navigation error (ignored, will inject widget anyway): {e}")
                # If goto failed (e.g. blocked entirely), force-navigate to our template
                # via data: URI won't work (origin mismatch), so try a second goto
                try:
                    await page.goto(url_with_slash, wait_until="commit", timeout=10000)
                except Exception:
                    pass
            # Small breath so any JS settles before we check for the widget
            await asyncio.sleep(0.3)

            # Ensure the Turnstile widget is present and the API script is loaded.
            # Idempotent: if our route already served the template, this is a no-op
            # for the widget; we only force-render if the script was already cached.
            await page.evaluate(f'''() => {{
                if (!document.querySelector('.cf-turnstile')) {{
                    document.body.innerHTML = '';
                    document.body.style.backgroundColor = '#ffffff';
                    document.body.style.display = 'flex';
                    document.body.style.justifyContent = 'center';
                    document.body.style.alignItems = 'center';
                    document.body.style.height = '100vh';
                    document.body.style.margin = '0';

                    const div = document.createElement('div');
                    div.className = 'cf-turnstile';
                    div.id = 'solver-widget';
                    div.setAttribute('data-sitekey', '{sitekey}');
                    {f"div.setAttribute('data-action', '{action}');" if action else ""}
                    {f"div.setAttribute('data-cdata', '{cdata}');" if cdata else ""}
                    document.body.appendChild(div);
                }}

                if (!document.querySelector('script[src*="turnstile/v0/api.js"]')) {{
                    const script = document.createElement('script');
                    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
                    script.async = true;
                    script.defer = true;
                    document.head.appendChild(script);
                }} else if (window.turnstile) {{
                    try {{ window.turnstile.render('.cf-turnstile'); }} catch (e) {{}}
                }}
            }}''')

            # Wait for the Turnstile widget to appear before polling.
            # The script is async so it needs a few seconds to load and render.
            try:
                await page.wait_for_selector(".cf-turnstile iframe, [name=cf-turnstile-response]", timeout=12000)
            except Exception:
                if self.debug:
                    logger.debug(f"Browser {index}: Turnstile widget not found via wait_for_selector after 12s, continuing anyway")

            if self.debug:
                logger.debug(f"Browser {index}: Starting Turnstile response retrieval loop")

            SOLVE_DEADLINE_SEC = 55
            solve_deadline = time.time() + SOLVE_DEADLINE_SEC
            attempt = 0
            while time.time() < solve_deadline:
                attempt += 1
                try:
                    turnstile_check = ""
                    response_inputs = page.locator("[name=cf-turnstile-response]")

                    if await response_inputs.count() > 0:
                        turnstile_check = await response_inputs.first.input_value(timeout=1000)

                    if turnstile_check == "":
                        if self.debug:
                            remaining = round(solve_deadline - time.time(), 1)
                            logger.debug(f"Browser {index}: Attempt {attempt} - no token yet ({remaining}s left)")

                        widget = page.locator(".cf-turnstile").first
                        if await widget.count() > 0:
                            try:
                                await widget.click(timeout=1000)
                            except Exception:
                                pass
                        await asyncio.sleep(0.5)
                    else:
                        elapsed_time = round(time.time() - start_time, 3)

                        logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{turnstile_check[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds (attempt {attempt})")

                        self.results[task_id] = {"value": turnstile_check, "elapsed_time": elapsed_time}
                        await self._request_debounced_results_save()
                        if client_key:
                            self._increment_usage(client_key)
                        break
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: poll iter {attempt} exception: {e}")
                    await asyncio.sleep(0.5)

            if self.results.get(task_id) == "CAPTCHA_NOT_READY":
                elapsed_time = round(time.time() - start_time, 3)
                self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
                if self.debug:
                    logger.error(f"Browser {index}: Error solving Turnstile in {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds")

        try:
            # asyncio.timeout() exists only in Python 3.11+; wait_for works on 3.10 (common on Ubuntu LTS VPS).
            await asyncio.wait_for(_solve_core(), timeout=75.0)
        except asyncio.TimeoutError:
            elapsed_time = round(time.time() - start_time, 3)
            self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
            logger.error(f"Browser {index}: Timeout solving Turnstile in {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds")
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
            if "Connection closed while reading from the driver" in str(e):
                recreate_browser = True
            logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
            if self.debug:
                logger.debug(f"Browser {index}: Turnstile solve traceback", exc_info=True)
        finally:
            if self.debug:
                logger.debug(f"Browser {index}: Clearing page state")

            if context is not None:
                with suppress(Exception):
                    await context.close()
            if recreate_browser:
                with suppress(Exception):
                    await browser.close()
                try:
                    browser = await self._launch_browser_instance()
                    logger.warning(f"Browser {index}: Recreated browser instance after driver disconnect")
                except Exception as recreate_error:
                    logger.error(f"Browser {index}: Failed to recreate browser instance: {str(recreate_error)}")
            await self.browser_pool.put((index, browser))

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')
        proxy = request.args.get('proxy')
        useragent = request.args.get('useragent')

        if not url or not sitekey:
            return jsonify({
                "status": "error",
                "error": "Both 'url' and 'sitekey' are required"
            }), 400

        task_id = str(uuid.uuid4())
        self.results[task_id] = "CAPTCHA_NOT_READY"

        try:
            asyncio.create_task(self._solve_turnstile(task_id=task_id, url=url, sitekey=sitekey, action=action, cdata=cdata, proxy=proxy, useragent=useragent))

            if self.debug:
                logger.debug(f"Request completed with taskid {task_id}.")
            return jsonify({"task_id": task_id}), 202
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return jsonify({
                "status": "error",
                "error": str(e)
            }), 500

    async def get_result(self):
        """Return solved data"""
        task_id = request.args.get('id')

        if not task_id or task_id not in self.results:
            return jsonify({"status": "error", "error": "Invalid task ID/Request parameter"}), 400

        result = self.results[task_id]
        status_code = 200

        if "CAPTCHA_FAIL" in result:
            status_code = 422

        return result, status_code

    async def generate_key_api(self):
        """Generate a new API key and save it."""
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        data = await request.get_json(silent=True) or {}
        user_name = (data.get("userName") or "").strip()
        expiry_date = (data.get("expiryDate") or "").strip()
        new_key = "MT-" + str(uuid.uuid4()).replace("-", "")
        self.api_keys.append(new_key)
        self.api_key_records[new_key] = {
            "key": new_key,
            "userName": user_name,
            "expiryDate": expiry_date,
            "enabled": True,
        }
        self._save_api_keys()
        if self.debug:
            logger.info(f"Generated new API key: {new_key}")
        return jsonify({"success": True, "key": new_key, "record": self.api_key_records[new_key]})

    async def remove_key_api(self):
        """Remove an API key and its usage record."""
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        data = await request.get_json(silent=True) or {}
        key = (data.get("key") or "").strip()

        if not key:
            return jsonify({"success": False, "error": "API key is required"}), 400

        if key not in self.api_keys:
            return jsonify({"success": False, "error": "API key not found"}), 404

        self.api_keys.remove(key)
        self.api_key_records.pop(key, None)
        self.usage.pop(key, None)
        self._save_api_keys()
        self._save_usage()

        if self.debug:
            logger.info(f"Removed API key: {key}")

        return jsonify({"success": True})

    async def toggle_key_api(self):
        """Enable or disable an API key."""
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        data = await request.get_json(silent=True) or {}
        key = (data.get("key") or "").strip()
        enabled = data.get("enabled")

        if not key:
            return jsonify({"success": False, "error": "API key is required"}), 400

        if key not in self.api_keys:
            return jsonify({"success": False, "error": "API key not found"}), 404

        if self._is_key_expired(key) and enabled is True:
            return jsonify({"success": False, "error": "Cannot enable an expired API key"}), 400

        self._ensure_key_status(key)
        self.api_key_records[key]["enabled"] = bool(enabled)
        self._save_api_keys()

        return jsonify({"success": True, "record": self.api_key_records[key]})

    async def update_key_expiry_api(self):
        """Update expiry date for an API key."""
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        data = await request.get_json(silent=True) or {}
        key = (data.get("key") or "").strip()
        expiry_date = (data.get("expiryDate") or "").strip()

        if not key:
            return jsonify({"success": False, "error": "API key is required"}), 400

        if key not in self.api_keys:
            return jsonify({"success": False, "error": "API key not found"}), 404

        self._ensure_key_status(key)
        self.api_key_records[key]["expiryDate"] = expiry_date

        if not self._is_key_expired(key):
            self.api_key_records[key]["enabled"] = True

        self._save_api_keys()

        return jsonify({"success": True, "record": self.api_key_records[key]})

    async def get_usage_api(self):
        """Return usage statistics for all API keys."""
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        return jsonify({"success": True, "usage": self.usage, "keys": self.api_keys, "records": self._api_key_rows()})

    async def create_task_api(self):
        """Standard Captcha Provider API format: createTask"""
        data = await request.get_json()
        
        # Check API Key
        client_key = data.get('clientKey')
        valid_key, key_error_code, key_error_description = self._validate_api_key(client_key)
        if not valid_key:
            return jsonify({"errorId": 1, "errorCode": key_error_code, "errorDescription": key_error_description}), 401

        if not data or 'task' not in data:
            return jsonify({"errorId": 1, "errorCode": "ERROR_ZERO_CAPTCHA_FILE", "errorDescription": "Task data missing"}), 400
        
        task = data['task']
        url = task.get('websiteURL')
        sitekey = task.get('websiteKey')
        action = task.get('action')
        cdata = task.get('cdata')
        proxy = task.get('proxy')
        useragent = task.get('userAgent')
        
        if not url or not sitekey:
            return jsonify({"errorId": 1, "errorCode": "ERROR_ZERO_CAPTCHA_FILE", "errorDescription": "websiteURL and websiteKey are required"}), 400

        shelf_key = self._shelf_key(url, sitekey, action, cdata)
        async with self.shelf_lock:
            self._prune_shelf_key(shelf_key)
            dq = self.shelf.get(shelf_key)
            shelved_token = None
            if dq:
                shelved_token = dq.popleft()["token"]
                if not dq:
                    del self.shelf[shelf_key]
        if shelved_token:
            task_id = str(uuid.uuid4())
            self.results[task_id] = {"value": shelved_token, "elapsed_time": 0.0}
            self._increment_usage(client_key)
            if self.debug:
                logger.debug(f"createTask served from shelf with taskid {task_id}.")
            return jsonify({
                "errorId": 0,
                "taskId": task_id
            })

        task_id = str(uuid.uuid4())
        self.results[task_id] = "CAPTCHA_NOT_READY"
        
        # This runs in the background. Because we use asyncio.create_task,
        # multiple team members can send requests at the exact same time 
        # and the server will process them concurrently (up to the limit of threads/browsers).
        asyncio.create_task(self._solve_turnstile(task_id=task_id, url=url, sitekey=sitekey, action=action, cdata=cdata, proxy=proxy, useragent=useragent, client_key=client_key))
        
        if self.debug:
            logger.debug(f"createTask request completed with taskid {task_id}.")
            
        return jsonify({
            "errorId": 0,
            "taskId": task_id
        })

    async def get_task_result_api(self):
        """Standard Captcha Provider API format: getTaskResult"""
        data = await request.get_json()
        
        # Check API Key
        client_key = data.get('clientKey')
        valid_key, key_error_code, key_error_description = self._validate_api_key(client_key)
        if not valid_key:
            return jsonify({"errorId": 1, "errorCode": key_error_code, "errorDescription": key_error_description}), 401

        if not data or 'taskId' not in data:
            return jsonify({"errorId": 1, "errorCode": "ERROR_TASK_ABSENT", "errorDescription": "taskId missing"}), 400
            
        task_id = data['taskId']
        if task_id not in self.results:
            return jsonify({"errorId": 1, "errorCode": "ERROR_NO_SUCH_CAPCHA_ID", "errorDescription": "Invalid task ID"}), 400
            
        result = self.results[task_id]
        
        if result == "CAPTCHA_NOT_READY":
            return jsonify({
                "errorId": 0,
                "status": "processing"
            })
            
        if isinstance(result, dict) and "CAPTCHA_FAIL" in result.get("value", ""):
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Failed to solve captcha"
            })
            
        if isinstance(result, dict) and "value" in result:
            return jsonify({
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "token": result["value"]
                }
            })
            
        return jsonify({"errorId": 1, "errorCode": "ERROR_UNKNOWN", "errorDescription": "Unknown state"}), 500

    async def get_shelf_status_api(self):
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        async with self.shelf_lock:
            for k in list(self.shelf.keys()):
                self._prune_shelf_key(k)
            shelf_counts = {str(k): len(v) for k, v in self.shelf.items()}
        return jsonify({
            "success": True,
            "running": self.shelf_running,
            "maxAgeSeconds": self.shelf_max_age_seconds,
            "websiteURL": self.shelf_prefill_url,
            "websiteKey": self.shelf_prefill_sitekey,
            "action": self.shelf_prefill_action,
            "cdata": self.shelf_prefill_cdata,
            "shelfCounts": shelf_counts,
            "totalShelved": sum(shelf_counts.values()),
        })

    async def set_shelf_settings_api(self):
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        data = await request.get_json(silent=True) or {}
        if "maxAgeSeconds" in data and data["maxAgeSeconds"] is not None:
            try:
                ma = float(data["maxAgeSeconds"])
                if ma < 1.0:
                    ma = 1.0
                self.shelf_max_age_seconds = ma
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "maxAgeSeconds must be a number"}), 400
        if "websiteURL" in data and data["websiteURL"] is not None:
            self.shelf_prefill_url = (data["websiteURL"] or "").strip()
        if "websiteKey" in data and data["websiteKey"] is not None:
            self.shelf_prefill_sitekey = (data["websiteKey"] or "").strip()
        if "action" in data and data["action"] is not None:
            self.shelf_prefill_action = (data["action"] or "").strip()
        if "cdata" in data and data["cdata"] is not None:
            self.shelf_prefill_cdata = (data["cdata"] or "").strip()
        async with self.shelf_lock:
            for k in list(self.shelf.keys()):
                self._prune_shelf_key(k)
        return jsonify({"success": True})

    async def shelf_control_api(self):
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        data = await request.get_json(silent=True) or {}
        run = data.get("running")
        if run is True:
            self.shelf_running = True
            if self._shelf_loop_task is None or self._shelf_loop_task.done():
                self._shelf_loop_task = asyncio.create_task(self._shelf_background_loop())
        elif run is False:
            self.shelf_running = False
            if self._shelf_loop_task and not self._shelf_loop_task.done():
                self._shelf_loop_task.cancel()
                try:
                    await self._shelf_loop_task
                except asyncio.CancelledError:
                    pass
            self._shelf_loop_task = None
        return jsonify({"success": True, "running": self.shelf_running})

    async def _shelf_background_loop(self):
        while self.shelf_running:
            try:
                url = self.shelf_prefill_url
                sk = self.shelf_prefill_sitekey
                if not url or not sk:
                    await asyncio.sleep(1)
                    continue
                key = self._shelf_key(url, sk, self.shelf_prefill_action, self.shelf_prefill_cdata)
                async with self.shelf_lock:
                    self._prune_shelf_key(key)
                    dq = self.shelf.get(key)
                    n = len(dq) if dq else 0
                max_cap = max(5, int(self.thread_count) * 3)
                if n >= max_cap:
                    await asyncio.sleep(0.5)
                    continue
                if not self.shelf_running:
                    break
                tid = str(uuid.uuid4())
                self.results[tid] = "CAPTCHA_NOT_READY"
                await self._solve_turnstile(
                    task_id=tid,
                    url=url,
                    sitekey=sk,
                    action=self.shelf_prefill_action or None,
                    cdata=self.shelf_prefill_cdata or None,
                    proxy=None,
                    useragent=None,
                    client_key=None,
                )
                res = self.results.pop(tid, None)
                if self.shelf_running and isinstance(res, dict):
                    val = res.get("value")
                    if val and val != "CAPTCHA_FAIL":
                        async with self.shelf_lock:
                            if key not in self.shelf:
                                self.shelf[key] = deque()
                            self.shelf[key].append({"token": val, "created": time.time()})
                            self._prune_shelf_key(key)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.debug:
                    logger.error(f"Shelf prefill error: {str(e)}")
                await asyncio.sleep(1)

    async def toggle_multi_thread_api(self):
        """Toggle multi-threading on or off."""
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        data = await request.get_json()
        enable = data.get('enable', False)
        
        if enable:
            self.multi_thread = True
            target = self.thread_count
        else:
            self.multi_thread = False
            target = 1
            
        asyncio.create_task(self._adjust_browser_pool(target))
        return jsonify({"success": True, "multi_thread": self.multi_thread, "target": target})

    async def get_multi_thread_status_api(self):
        """Get current multi-thread status."""
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        return jsonify({
            "success": True,
            "multi_thread": self.multi_thread,
            "current_browsers": self.current_browser_count,
            "max_threads": self.thread_count,
            "max_thread_limit": self.max_thread_count
        })

    async def set_thread_count_api(self):
        """Update the browser quantity used when multi-thread mode is ON."""
        auth = self._require_admin_session()
        if auth is not None:
            return auth
        data = await request.get_json(silent=True) or {}
        try:
            requested = int(data.get("threadCount", self.thread_count))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "threadCount must be a number"}), 400

        requested = max(1, min(requested, self.max_thread_count))
        self.thread_count = requested

        if self.multi_thread:
            asyncio.create_task(self._adjust_browser_pool(self.thread_count))

        return jsonify({
            "success": True,
            "threadCount": self.thread_count,
            "maxThreadLimit": self.max_thread_count,
            "multi_thread": self.multi_thread
        })

    async def _adjust_browser_pool(self, target: int):
        async with self.pool_adjustment_lock:
            logger.info(f"Adjusting browser pool to {target} browsers...")
            while self.current_browser_count < target:
                self.current_browser_count += 1
                try:
                    browser = await self._launch_browser_instance()
                    await self.browser_pool.put((self.current_browser_count, browser))
                    if self.debug:
                        logger.success(f"Browser {self.current_browser_count} initialized successfully")
                except Exception as e:
                    logger.error(f"Failed to launch browser: {e}")
                    self.current_browser_count -= 1
                    break
                    
            while self.current_browser_count > target:
                try:
                    index, browser = await self.browser_pool.get()
                    with suppress(Exception):
                        await browser.close()
                    self.current_browser_count -= 1
                    if self.debug:
                        logger.success(f"Browser {index} closed successfully")
                except Exception as e:
                    logger.error(f"Failed to close browser: {e}")
                    break
                    
            logger.success(f"Browser pool adjusted. Current browsers: {self.current_browser_count}")

    async def index(self):
        """Serve the API documentation page (requires admin login)."""
        if not session.get("admin"):
            return redirect("/login")
        html = (
            """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Cloudflare Turnstile Solver API</title>
                <link rel="preconnect" href="https://fonts.googleapis.com">
                <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
                <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&display=swap" rel="stylesheet">
                <script src="https://cdn.tailwindcss.com"></script>
                <script>
                    tailwind.config = {
                        theme: {
                            extend: {
                                fontFamily: { sans: ['DM Sans', 'system-ui', 'sans-serif'] },
                            },
                        },
                    };
                </script>
            </head>
            <body class="min-h-screen bg-zinc-950 text-zinc-300 antialiased font-sans selection:bg-cyan-500/25 selection:text-white">
                <div class="pointer-events-none fixed inset-0 bg-[radial-gradient(ellipse_100%_60%_at_50%_-20%,rgba(34,211,238,0.07),transparent_55%)]"></div>
                <div class="relative">
                    <header class="sticky top-0 z-30 border-b border-zinc-800/80 bg-zinc-950/85 backdrop-blur-md">
                        <div class="mx-auto flex max-w-5xl items-center justify-between gap-4 px-4 py-4 sm:px-6">
                            <div>
                                <p class="text-[10px] font-semibold uppercase tracking-[0.22em] text-zinc-500">Cloudflare Turnstile solver</p>
                                <h1 class="text-lg font-semibold tracking-tight text-white sm:text-xl">Control panel</h1>
                            </div>
                            <div class="flex shrink-0 items-center gap-2">
                                <span class="hidden rounded-full border border-zinc-800 bg-zinc-900/80 px-2.5 py-1 text-[10px] font-medium uppercase tracking-wider text-zinc-500 sm:inline">Local</span>
                                <button type="button" id="logout-btn" class="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-xs font-medium text-zinc-200 transition hover:border-zinc-600 hover:bg-zinc-700">Log out</button>
                            </div>
                        </div>
                    </header>
                    <main class="mx-auto max-w-5xl space-y-6 px-4 py-8 sm:px-6 sm:py-10">

                    <section class="rounded-2xl border border-zinc-800/70 bg-zinc-900/40 p-5 shadow-xl shadow-black/20 sm:p-6">
                        <div class="mb-4 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                            <div>
                                <h2 class="text-lg font-semibold text-white tracking-tight mb-1">API Key Management</h2>
                                <p class="text-sm text-zinc-500">Generate API keys for users with optional expiry date.</p>
                            </div>
                            <button id="generate-key-btn" type="button" class="rounded-lg bg-cyan-600 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-cyan-950/25 transition hover:bg-cyan-500">
                                Generate New Key
                            </button>
                        </div>
                        <div id="api-key-result" class="mt-3 hidden rounded-xl border border-emerald-500/25 bg-emerald-950/20 p-3">
                            <p class="text-sm text-zinc-500 mb-1">New API Key (Save this!):</p>
                            <div class="flex items-center gap-2">
                                <input type="text" id="new-api-key" readonly class="flex-1 rounded-lg border border-zinc-800 bg-zinc-950/80 px-3 py-2 font-mono text-sm text-emerald-400" />
                                <button type="button" id="copy-key-btn" class="px-3 py-2 rounded bg-zinc-800 hover:bg-zinc-700 text-white text-sm">Copy</button>
                            </div>
                        </div>
                    </section>

                    <div id="generate-key-modal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/60 px-4 backdrop-blur-sm" style="display:none;">
                        <div class="w-full max-w-lg rounded-2xl border border-zinc-800/90 bg-zinc-900 p-6 shadow-2xl shadow-black/40">
                            <div class="flex items-start justify-between gap-4 mb-4">
                                <div>
                                    <h2 class="text-lg font-semibold text-white tracking-tight mb-1">Generate New API Key</h2>
                                    <p class="text-sm text-zinc-500">Enter user details before creating the key.</p>
                                </div>
                                <button id="close-generate-key-modal" type="button" class="text-zinc-500 hover:text-white text-2xl leading-none">&times;</button>
                            </div>
                            <div class="space-y-4">
                                <div>
                                    <label for="api-user-name" class="block text-sm font-medium text-zinc-400 mb-1">User Name</label>
                                    <input id="api-user-name" type="text" placeholder="Customer / user name" autocomplete="off"
                                        class="w-full px-3 py-2 rounded bg-zinc-950/80 border border-zinc-800 text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-2 focus:ring-cyan-500/40" />
                                </div>
                                <div>
                                    <label for="api-expiry-date" class="block text-sm font-medium text-zinc-400 mb-1">Expiry Date <span class="text-zinc-600 font-normal">(optional)</span></label>
                                    <input id="api-expiry-date" type="date"
                                        class="w-full px-3 py-2 rounded bg-zinc-950/80 border border-zinc-800 text-zinc-100 focus:outline-none focus:ring-2 focus:ring-cyan-500/40" />
                                </div>
                            </div>
                            <div class="mt-6 flex justify-end gap-3">
                                <button id="cancel-generate-key-btn" type="button" class="px-4 py-2 rounded bg-zinc-800 hover:bg-zinc-700 text-white font-semibold text-sm">Cancel</button>
                                <button id="confirm-generate-key-btn" type="button" class="rounded-lg bg-cyan-600 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-cyan-950/25 transition hover:bg-cyan-500">Generate Key</button>
                            </div>
                        </div>
                    </div>

                    <section class="rounded-2xl border border-zinc-800/70 bg-zinc-900/40 p-5 shadow-xl shadow-black/20 sm:p-6">
                        <div class="mb-4 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                            <div>
                                <h2 class="text-lg font-semibold text-white tracking-tight mb-1">API Key Usage Stats</h2>
                                <p class="text-sm text-zinc-500">Track how many tokens each API key has generated.</p>
                            </div>
                            <button id="refresh-usage-btn" type="button" class="px-3 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 text-white text-xs shadow">
                                Refresh
                            </button>
                        </div>
                        <div class="overflow-x-auto">
                            <table class="w-full text-left text-sm text-zinc-400 border-collapse">
                                <thead class="bg-zinc-900 text-zinc-500 border-b border-zinc-800">
                                    <tr>
                                        <th class="px-4 py-3 font-medium rounded-tl">User Name</th>
                                        <th class="px-4 py-3 font-medium">API Key</th>
                                        <th class="px-4 py-3 font-medium">Tokens Fetched</th>
                                        <th class="px-4 py-3 font-medium">Expiry Date</th>
                                        <th class="px-4 py-3 font-medium">Status</th>
                                        <th class="px-4 py-3 font-medium rounded-tr">Action</th>
                                    </tr>
                                </thead>
                                <tbody id="usage-tbody" class="divide-y divide-zinc-800">
                                    <tr><td colspan="6" class="px-4 py-4 text-center text-zinc-600">Loading...</td></tr>
                                </tbody>
                            </table>
                        </div>
                    </section>

                    <section class="rounded-2xl border border-zinc-800/70 bg-zinc-900/40 p-5 shadow-xl shadow-black/20 sm:p-6">
                        <div class="mb-4 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                            <div>
                                <h2 class="text-lg font-semibold text-white tracking-tight mb-1">Multi-Thread Mode</h2>
                                <p class="text-sm text-zinc-500">Toggle multi-threading to save PC resources. When OFF, only 1 browser is used. When ON, it uses the browser quantity below.</p>
                            </div>
                            <div class="flex flex-wrap items-center gap-3">
                                <label for="browser-quantity" class="text-sm text-zinc-400">Browsers</label>
                                <input id="browser-quantity" type="number" min="1" step="1" value="15" autocomplete="off"
                                    class="w-24 px-3 py-2 rounded bg-zinc-950/80 border border-zinc-800 text-zinc-100 focus:outline-none focus:ring-2 focus:ring-cyan-500/40" />
                                <button id="apply-thread-count-btn" type="button" class="px-3 py-2 rounded bg-zinc-800 hover:bg-zinc-700 text-white font-semibold shadow text-sm">
                                    Apply
                                </button>
                                <span id="thread-status-text" class="text-sm font-medium text-zinc-400">Loading...</span>
                                <button id="toggle-thread-btn" type="button" class="px-4 py-2 rounded bg-zinc-800 hover:bg-zinc-700 text-white font-semibold shadow text-sm transition-colors" style="display:none;">
                                    Toggle
                                </button>
                            </div>
                        </div>
                    </section>

                    <section class="rounded-2xl border border-zinc-800/70 bg-zinc-900/40 p-5 shadow-xl shadow-black/20 sm:p-6">
                        <h2 class="text-lg font-semibold text-white tracking-tight mb-1">Live test solve</h2>
                        <p class="text-sm text-zinc-500 mb-4">Use the real page origin and Turnstile sitekey from that site. The solver opens this URL in a browser and runs the widget there.</p>
                        <div class="space-y-3">
                            <div>
                                <label for="test-url" class="block text-sm font-medium text-zinc-400 mb-1">Website URL</label>
                                <input id="test-url" type="text" inputmode="url" value="__IVAC_SIGNIN_URL__" placeholder="https://your-domain.com" autocomplete="off"
                                    class="w-full px-3 py-2 rounded bg-zinc-950/80 border border-zinc-800 text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-2 focus:ring-cyan-500/40" />
                            </div>
                            <div>
                                <label for="test-sitekey" class="block text-sm font-medium text-zinc-400 mb-1">Site key</label>
                                <input id="test-sitekey" type="text" value="__IVAC_SITEKEY__" placeholder="0x4AAAAAA..." autocomplete="off"
                                    class="w-full px-3 py-2 rounded bg-zinc-950/80 border border-zinc-800 text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-2 focus:ring-cyan-500/40 font-mono text-sm" />
                            </div>
                            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                <div>
                                    <label for="test-action" class="block text-sm font-medium text-zinc-400 mb-1">Action <span class="text-zinc-600 font-normal">(optional)</span></label>
                                    <input id="test-action" type="text" placeholder="login" autocomplete="off"
                                        class="w-full px-3 py-2 rounded bg-zinc-950/80 border border-zinc-800 text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-2 focus:ring-cyan-500/40 text-sm" />
                                </div>
                                <div>
                                    <label for="test-ua" class="block text-sm font-medium text-zinc-400 mb-1">User-Agent <span class="text-zinc-600 font-normal">(optional)</span></label>
                                    <input id="test-ua" type="text" placeholder="Mozilla/5.0 ..." autocomplete="off"
                                        class="w-full px-3 py-2 rounded bg-zinc-950/80 border border-zinc-800 text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-2 focus:ring-cyan-500/40 text-sm" />
                                </div>
                            </div>
                        </div>
                        <div class="mt-4 flex flex-wrap items-center gap-3">
                            <button id="test-solve-btn" type="button"
                                class="rounded-lg bg-cyan-600 px-5 py-2.5 font-semibold text-white shadow-lg shadow-cyan-950/25 transition hover:bg-cyan-500 disabled:cursor-not-allowed disabled:opacity-50">
                                Test solve
                            </button>
                            <span id="test-status" class="text-sm text-zinc-500"></span>
                        </div>
                        <div id="test-error" class="mt-3 hidden rounded-xl border border-rose-500/30 bg-rose-950/30 p-3 text-sm text-rose-200" style="display:none"></div>
                        <div id="test-result-wrap" class="hidden mt-4" style="display:none">
                            <p class="text-sm text-zinc-500 mb-1">Token <span id="test-elapsed" class="font-medium text-emerald-400"></span></p>
                            <textarea id="test-token" readonly rows="4"
                                class="w-full break-all rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-xs text-emerald-300"></textarea>
                            <button type="button" id="copy-token-btn" class="mt-2 text-sm font-medium text-cyan-400 hover:text-cyan-300">Copy token</button>
                        </div>
                    </section>

                    <section class="rounded-2xl border border-zinc-800/70 bg-zinc-900/40 p-5 shadow-xl shadow-black/20 sm:p-6">
                        <h2 class="text-lg font-semibold text-white tracking-tight mb-4">API Documentation</h2>

                        <ol class="ml-0 list-decimal space-y-6 pl-6 marker:font-semibold marker:text-cyan-500/90">
                            <li class="pl-2">
                            <!-- createTask -->
                            <div class="group relative rounded-xl border border-zinc-800/80 bg-zinc-950/50 p-4 sm:p-5">
                                <h3 class="text-lg font-medium text-zinc-100 mb-2">Create Task</h3>
                                <p class="text-sm text-zinc-500 mb-2"><strong class="text-zinc-300">POST</strong> <code class="font-mono text-cyan-400">/createTask</code></p>
                                <button type="button" class="copy-json-btn absolute right-4 top-4 rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-xs text-zinc-400 transition hover:border-zinc-700 hover:bg-zinc-800 hover:text-zinc-200">Copy</button>
                                <pre class="overflow-x-auto rounded-lg bg-zinc-950 p-3 text-xs text-emerald-300"><code class="json-content">{
  "clientKey": "MT-YOUR_API_KEY",
  "task": {
    "type": "TurnstileTaskProxyless",
    "websiteURL": "https://appointment.ivacbd.com/signin",
    "websiteKey": "0x4AAAAAACghKkJHL1t7UkuZ",
    "action": "login"
  }
}</code></pre>
                            </div>
                            </li>

                            <li class="pl-2">
                            <!-- getTaskResult -->
                            <div class="group relative rounded-xl border border-zinc-800/80 bg-zinc-950/50 p-4 sm:p-5">
                                <h3 class="text-lg font-medium text-zinc-100 mb-2">Get Task Result</h3>
                                <p class="text-sm text-zinc-500 mb-2"><strong class="text-zinc-300">POST</strong> <code class="font-mono text-cyan-400">/getTaskResult</code></p>
                                <button type="button" class="copy-json-btn absolute right-4 top-4 rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-xs text-zinc-400 transition hover:border-zinc-700 hover:bg-zinc-800 hover:text-zinc-200">Copy</button>
                                <pre class="overflow-x-auto rounded-lg bg-zinc-950 p-3 text-xs text-emerald-300"><code class="json-content">{
  "clientKey": "MT-YOUR_API_KEY",
  "taskId": "TASK_ID_FROM_CREATE_TASK"
}</code></pre>
                            </div>
                            </li>
                        </ol>
                    </section>
                    </main>
                    <footer class="border-t border-zinc-800/80 py-8 text-center text-xs text-zinc-600">
                        &copy; 2026 Multi Tool. All rights reserved.
                    </footer>
                </div>
                <script>
                (function () {
                    function setPanelHidden(el, hidden) {
                        if (!el) { return; }
                        if (hidden) {
                            el.classList.add('hidden');
                            el.style.display = 'none';
                        } else {
                            el.classList.remove('hidden');
                            el.style.display = '';
                        }
                    }

                    function redirectIfUnauthorized(res) {
                        if (res.status === 401) {
                            window.location.href = '/login';
                            return true;
                        }
                        return false;
                    }

                    function parseResultBody(text) {
                        var t = (text || '').trim();
                        if (!t) { return t; }
                        if (t.charAt(0) === '{' || t.charAt(0) === '[' || t.charAt(0) === '"') {
                            try { return JSON.parse(t); } catch (e) { /* fall through */ }
                        }
                        return t;
                    }

                    function sleep(ms) {
                        return new Promise(function (resolve) { setTimeout(resolve, ms); });
                    }

                    async function pollResult(taskId) {
                        var maxWait = 120000;
                        var start = Date.now();
                        while (Date.now() - start < maxWait) {
                            var res = await fetch('/result?id=' + encodeURIComponent(taskId), { cache: 'no-store' });
                            var raw = await res.text();
                            var data = parseResultBody(raw);
                            if (typeof data === 'string') {
                                if (data === 'CAPTCHA_NOT_READY') {
                                    await sleep(800);
                                    continue;
                                }
                                return { ok: false, error: data, status: res.status };
                            }
                            if (data && data.status === 'error') {
                                return { ok: false, error: data.error || 'Unknown error', status: res.status };
                            }
                            if (data && typeof data.value === 'string') {
                                if (data.value === 'CAPTCHA_FAIL') {
                                    return { ok: false, error: 'Solve failed or timed out.', status: res.status, elapsed: data.elapsed_time };
                                }
                                return { ok: true, token: data.value, elapsed: data.elapsed_time };
                            }
                            await sleep(800);
                        }
                        return { ok: false, error: 'Timed out waiting for result.' };
                    }

                    function wireLiveTest() {
                        var btn = document.getElementById('test-solve-btn');
                        var copyBtn = document.getElementById('copy-token-btn');
                        var statusEl = document.getElementById('test-status');
                        var errEl = document.getElementById('test-error');
                        var wrap = document.getElementById('test-result-wrap');
                        var tokenEl = document.getElementById('test-token');
                        var elapsedEl = document.getElementById('test-elapsed');
                        if (!btn || !statusEl || !errEl || !wrap || !tokenEl || !elapsedEl) {
                            return;
                        }

                        btn.addEventListener('click', function () {
                            var urlEl = document.getElementById('test-url');
                            var skEl = document.getElementById('test-sitekey');
                            var url = urlEl ? urlEl.value.trim() : '';
                            var sitekey = skEl ? skEl.value.trim() : '';
                            var actionEl = document.getElementById('test-action');
                            var uaEl = document.getElementById('test-ua');
                            var action = actionEl ? actionEl.value.trim() : '';
                            var ua = uaEl ? uaEl.value.trim() : '';

                            setPanelHidden(errEl, true);
                            setPanelHidden(wrap, true);
                            tokenEl.value = '';
                            elapsedEl.textContent = '';

                            if (!url || !sitekey) {
                                errEl.textContent = 'Enter both a website URL and site key.';
                                setPanelHidden(errEl, false);
                                return;
                            }

                            var params = new URLSearchParams();
                            params.set('url', url);
                            params.set('sitekey', sitekey);
                            if (action) { params.set('action', action); }
                            if (ua) { params.set('useragent', ua); }

                            btn.disabled = true;
                            statusEl.textContent = 'Starting solve...';

                            fetch('/turnstile?' + params.toString(), { cache: 'no-store', credentials: 'same-origin' })
                                .then(function (startRes) {
                                    return startRes.text().then(function (body) {
                                        var startJson = null;
                                        try { startJson = JSON.parse(body); } catch (e) { startJson = null; }
                                        return { startRes: startRes, startJson: startJson, body: body };
                                    });
                                })
                                .then(function (x) {
                                    if (!x.startRes.ok || !x.startJson || !x.startJson.task_id) {
                                        var msg = (x.startJson && x.startJson.error) ? x.startJson.error : ('HTTP ' + x.startRes.status);
                                        if (!x.startJson && x.body) { msg = x.body.slice(0, 200); }
                                        throw new Error(msg);
                                    }
                                    statusEl.textContent = 'Solving (browser may open)...';
                                    return pollResult(x.startJson.task_id);
                                })
                                .then(function (outcome) {
                                    statusEl.textContent = '';
                                    if (!outcome.ok) {
                                        errEl.textContent = outcome.error + (outcome.elapsed != null ? ' (' + outcome.elapsed + 's)' : '');
                                        setPanelHidden(errEl, false);
                                        return;
                                    }
                                    tokenEl.value = outcome.token;
                                    elapsedEl.textContent = outcome.elapsed != null ? ('(' + outcome.elapsed + 's)') : '';
                                    setPanelHidden(wrap, false);
                                })
                                .catch(function (e) {
                                    statusEl.textContent = '';
                                    errEl.textContent = (e && e.message) ? e.message : String(e);
                                    setPanelHidden(errEl, false);
                                })
                                .finally(function () {
                                    btn.disabled = false;
                                });
                        });

                        if (copyBtn) {
                            copyBtn.addEventListener('click', function () {
                                var t = tokenEl.value;
                                if (navigator.clipboard && navigator.clipboard.writeText) {
                                    navigator.clipboard.writeText(t).then(function () {
                                        copyBtn.textContent = 'Copied';
                                        setTimeout(function () { copyBtn.textContent = 'Copy token'; }, 2000);
                                    }).catch(function () {
                                        try {
                                            tokenEl.select();
                                            document.execCommand('copy');
                                            copyBtn.textContent = 'Copied';
                                            setTimeout(function () { copyBtn.textContent = 'Copy token'; }, 2000);
                                        } catch (e2) { /* ignore */ }
                                    });
                                } else {
                                    try {
                                        tokenEl.select();
                                        document.execCommand('copy');
                                        copyBtn.textContent = 'Copied';
                                        setTimeout(function () { copyBtn.textContent = 'Copy token'; }, 2000);
                                    } catch (e3) { /* ignore */ }
                                }
                            });
                        }

                        var genKeyBtn = document.getElementById('generate-key-btn');
                        var generateKeyModal = document.getElementById('generate-key-modal');
                        var closeGenerateKeyModalBtn = document.getElementById('close-generate-key-modal');
                        var cancelGenerateKeyBtn = document.getElementById('cancel-generate-key-btn');
                        var confirmGenerateKeyBtn = document.getElementById('confirm-generate-key-btn');
                        var copyKeyBtn = document.getElementById('copy-key-btn');
                        var keyResult = document.getElementById('api-key-result');
                        var newKeyInput = document.getElementById('new-api-key');
                        var apiUserNameInput = document.getElementById('api-user-name');
                        var apiExpiryDateInput = document.getElementById('api-expiry-date');

                        function openGenerateKeyModal() {
                            if (!generateKeyModal) return;
                            generateKeyModal.classList.remove('hidden');
                            generateKeyModal.style.display = 'flex';
                            if (apiUserNameInput) { apiUserNameInput.focus(); }
                        }

                        function closeGenerateKeyModal() {
                            if (!generateKeyModal) return;
                            generateKeyModal.classList.add('hidden');
                            generateKeyModal.style.display = 'none';
                        }

                        if (genKeyBtn) {
                            genKeyBtn.addEventListener('click', openGenerateKeyModal);
                        }

                        if (closeGenerateKeyModalBtn) {
                            closeGenerateKeyModalBtn.addEventListener('click', closeGenerateKeyModal);
                        }

                        if (cancelGenerateKeyBtn) {
                            cancelGenerateKeyBtn.addEventListener('click', closeGenerateKeyModal);
                        }

                        if (generateKeyModal) {
                            generateKeyModal.addEventListener('click', function(e) {
                                if (e.target === generateKeyModal) {
                                    closeGenerateKeyModal();
                                }
                            });
                        }

                        if (confirmGenerateKeyBtn) {
                            confirmGenerateKeyBtn.addEventListener('click', async function() {
                                confirmGenerateKeyBtn.disabled = true;
                                confirmGenerateKeyBtn.textContent = 'Generating...';
                                try {
                                    var res = await fetch('/generateKey', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({
                                            userName: apiUserNameInput ? apiUserNameInput.value.trim() : '',
                                            expiryDate: apiExpiryDateInput ? apiExpiryDateInput.value : ''
                                        })
                                    });
                                    if (redirectIfUnauthorized(res)) return;
                                    var data = await res.json();
                                    if (data.key) {
                                        newKeyInput.value = data.key;
                                        setPanelHidden(keyResult, false);
                                        if (apiUserNameInput) { apiUserNameInput.value = ''; }
                                        if (apiExpiryDateInput) { apiExpiryDateInput.value = ''; }
                                        closeGenerateKeyModal();
                                        fetchUsage();
                                    }
                                } catch (e) {
                                    alert('Failed to generate key');
                                } finally {
                                    confirmGenerateKeyBtn.disabled = false;
                                    confirmGenerateKeyBtn.textContent = 'Generate Key';
                                }
                            });
                        }

                        if (copyKeyBtn) {
                            copyKeyBtn.addEventListener('click', function() {
                                newKeyInput.select();
                                document.execCommand('copy');
                                copyKeyBtn.textContent = 'Copied!';
                                setTimeout(function() { copyKeyBtn.textContent = 'Copy'; }, 2000);
                            });
                        }

                        async function fetchUsage() {
                            var tbody = document.getElementById('usage-tbody');
                            if (!tbody) return;
                            try {
                                var res = await fetch('/getUsage', { cache: 'no-store' });
                                if (redirectIfUnauthorized(res)) return;
                                var data = await res.json();
                                if (data.success && (data.records || data.keys)) {
                                    tbody.innerHTML = '';
                                    var records = data.records || data.keys.map(function(k) {
                                        return { key: k, userName: '', expiryDate: '', enabled: true, expired: false, usage: data.usage[k] || 0 };
                                    });
                                    if (records.length === 0) {
                                        tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-4 text-center text-zinc-600">No API keys found.</td></tr>';
                                        return;
                                    }
                                    records.forEach(function(record) {
                                        var key = record.key;
                                        var count = record.usage || 0;
                                        var expired = !!record.expired;
                                        var enabled = !!record.enabled && !expired;
                                        var tr = document.createElement('tr');
                                        tr.className = 'hover:bg-zinc-800/50';

                                        var userTd = document.createElement('td');
                                        userTd.className = 'px-4 py-3 text-sm text-zinc-100';
                                        userTd.textContent = record.userName || '-';

                                        var keyTd = document.createElement('td');
                                        keyTd.className = 'px-4 py-3 font-mono text-xs text-emerald-400/90';
                                        keyTd.textContent = key;

                                        var countTd = document.createElement('td');
                                        countTd.className = 'px-4 py-3';
                                        countTd.textContent = String(count);

                                        var expiryTd = document.createElement('td');
                                        expiryTd.className = 'px-4 py-3 text-sm';
                                        var expiryWrap = document.createElement('div');
                                        expiryWrap.className = 'flex flex-wrap items-center gap-2';
                                        var expiryInput = document.createElement('input');
                                        expiryInput.type = 'date';
                                        expiryInput.className = 'expiry-date-input px-2 py-1 rounded bg-zinc-950/80 border border-zinc-800 text-zinc-100 text-xs focus:outline-none focus:ring-1 focus:ring-cyan-500/30';
                                        expiryInput.value = record.expiryDate || '';
                                        expiryInput.setAttribute('data-key', key);
                                        var updateExpiryBtn = document.createElement('button');
                                        updateExpiryBtn.type = 'button';
                                        updateExpiryBtn.className = 'update-expiry-btn px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-white text-xs font-semibold';
                                        updateExpiryBtn.textContent = 'Update';
                                        updateExpiryBtn.setAttribute('data-key', key);
                                        expiryWrap.appendChild(expiryInput);
                                        expiryWrap.appendChild(updateExpiryBtn);
                                        expiryTd.appendChild(expiryWrap);

                                        var statusTd = document.createElement('td');
                                        statusTd.className = 'px-4 py-3 text-sm font-semibold';
                                        if (expired) {
                                            statusTd.className += ' text-amber-400';
                                            statusTd.textContent = 'Expired';
                                        } else if (enabled) {
                                            statusTd.className += ' text-emerald-400';
                                            statusTd.textContent = 'Enabled';
                                        } else {
                                            statusTd.className += ' text-rose-400';
                                            statusTd.textContent = 'Disabled';
                                        }

                                        var actionTd = document.createElement('td');
                                        actionTd.className = 'px-4 py-3 flex flex-wrap gap-2';
                                        var toggleBtn = document.createElement('button');
                                        toggleBtn.type = 'button';
                                        toggleBtn.className = (enabled ? 'disable-api-key-btn bg-amber-600 hover:bg-amber-500' : 'enable-api-key-btn bg-emerald-600 hover:bg-emerald-500') + ' rounded-md px-3 py-1.5 text-xs font-semibold text-white transition';
                                        toggleBtn.textContent = enabled ? 'Disable' : 'Enable';
                                        toggleBtn.setAttribute('data-key', key);
                                        toggleBtn.setAttribute('data-enabled', enabled ? 'false' : 'true');
                                        if (expired) {
                                            toggleBtn.disabled = true;
                                            toggleBtn.className = 'cursor-not-allowed rounded-md bg-zinc-800 px-3 py-1.5 text-xs font-semibold text-zinc-500';
                                        }
                                        var removeBtn = document.createElement('button');
                                        removeBtn.type = 'button';
                                        removeBtn.className = 'remove-api-key-btn rounded-md bg-rose-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-rose-500';
                                        removeBtn.textContent = 'Remove';
                                        removeBtn.setAttribute('data-key', key);
                                        actionTd.appendChild(toggleBtn);
                                        actionTd.appendChild(removeBtn);

                                        tr.appendChild(userTd);
                                        tr.appendChild(keyTd);
                                        tr.appendChild(countTd);
                                        tr.appendChild(expiryTd);
                                        tr.appendChild(statusTd);
                                        tr.appendChild(actionTd);
                                        tbody.appendChild(tr);
                                    });
                                }
                            } catch (e) {
                                tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-4 text-center text-rose-400">Failed to load usage stats.</td></tr>';
                            }
                        }

                        var usageTbody = document.getElementById('usage-tbody');
                        if (usageTbody) {
                            usageTbody.addEventListener('click', async function(e) {
                                var expiryBtn = e.target && e.target.closest ? e.target.closest('.update-expiry-btn') : null;
                                if (expiryBtn) {
                                    var expiryKey = expiryBtn.getAttribute('data-key') || '';
                                    var expiryInput = usageTbody.querySelector('.expiry-date-input[data-key="' + expiryKey + '"]');
                                    if (!expiryKey || !expiryInput) return;

                                    expiryBtn.disabled = true;
                                    expiryBtn.textContent = 'Updating...';
                                    try {
                                        var expiryRes = await fetch('/updateKeyExpiry', {
                                            method: 'POST',
                                            headers: { 'Content-Type': 'application/json' },
                                            body: JSON.stringify({ key: expiryKey, expiryDate: expiryInput.value })
                                        });
                                        if (redirectIfUnauthorized(expiryRes)) return;
                                        var expiryData = await expiryRes.json();
                                        if (!expiryData.success) {
                                            alert(expiryData.error || 'Failed to update expiry');
                                        }
                                        await fetchUsage();
                                    } catch (errExpiry) {
                                        alert('Failed to update expiry');
                                        expiryBtn.disabled = false;
                                        expiryBtn.textContent = 'Update';
                                    }
                                    return;
                                }

                                var toggleBtn = e.target && e.target.closest ? e.target.closest('.enable-api-key-btn, .disable-api-key-btn') : null;
                                if (toggleBtn) {
                                    var toggleKey = toggleBtn.getAttribute('data-key') || '';
                                    var shouldEnable = toggleBtn.getAttribute('data-enabled') === 'true';
                                    if (!toggleKey) return;

                                    toggleBtn.disabled = true;
                                    try {
                                        var toggleRes = await fetch('/toggleKey', {
                                            method: 'POST',
                                            headers: { 'Content-Type': 'application/json' },
                                            body: JSON.stringify({ key: toggleKey, enabled: shouldEnable })
                                        });
                                        if (redirectIfUnauthorized(toggleRes)) return;
                                        var toggleData = await toggleRes.json();
                                        if (!toggleData.success) {
                                            alert(toggleData.error || 'Failed to update API key');
                                        }
                                        await fetchUsage();
                                    } catch (errToggle) {
                                        alert('Failed to update API key');
                                        toggleBtn.disabled = false;
                                    }
                                    return;
                                }

                                var btn = e.target && e.target.closest ? e.target.closest('.remove-api-key-btn') : null;
                                if (!btn) return;

                                var key = btn.getAttribute('data-key') || '';
                                if (!key) return;

                                if (!confirm('Remove this API key completely?')) {
                                    return;
                                }

                                btn.disabled = true;
                                btn.textContent = 'Removing...';
                                try {
                                    var res = await fetch('/removeKey', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ key: key })
                                    });
                                    if (redirectIfUnauthorized(res)) return;
                                    var data = await res.json();
                                    if (!data.success) {
                                        alert(data.error || 'Failed to remove API key');
                                    }
                                    await fetchUsage();
                                } catch (err) {
                                    alert('Failed to remove API key');
                                    btn.disabled = false;
                                    btn.textContent = 'Remove';
                                }
                            });
                        }
                        
                        var refreshUsageBtn = document.getElementById('refresh-usage-btn');
                        if (refreshUsageBtn) {
                            refreshUsageBtn.addEventListener('click', fetchUsage);
                        }
                        
                        // Initial fetch
                        fetchUsage();

                        var toggleThreadBtn = document.getElementById('toggle-thread-btn');
                        var threadStatusText = document.getElementById('thread-status-text');
                        var browserQuantityInput = document.getElementById('browser-quantity');
                        var applyThreadCountBtn = document.getElementById('apply-thread-count-btn');
                        var isMultiThread = false;

                        async function fetchThreadStatus() {
                            try {
                                var res = await fetch('/getMultiThreadStatus', { cache: 'no-store' });
                                if (redirectIfUnauthorized(res)) return;
                                var data = await res.json();
                                if (data.success) {
                                    isMultiThread = data.multi_thread;
                                    if (browserQuantityInput) {
                                        browserQuantityInput.max = String(data.max_thread_limit || data.max_threads || 1);
                                        browserQuantityInput.value = String(data.max_threads || 1);
                                    }
                                    updateThreadUI(data);
                                }
                            } catch (e) {
                                threadStatusText.textContent = 'Error loading status';
                            }
                        }

                        function updateThreadUI(data) {
                            if (data.max_threads <= 1) {
                                threadStatusText.textContent = 'Max threads is 1 (Multi-thread disabled)';
                                toggleThreadBtn.style.display = 'none';
                                return;
                            }
                            
                            toggleThreadBtn.style.display = '';
                            if (isMultiThread) {
                                threadStatusText.innerHTML = '<span class="font-medium text-emerald-400">ON</span> <span class="text-zinc-500">(' + data.current_browsers + '/' + data.max_threads + ' browsers)</span>';
                                toggleThreadBtn.textContent = 'Turn OFF';
                                toggleThreadBtn.className = 'rounded-lg bg-rose-600 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-rose-950/20 transition hover:bg-rose-500';
                            } else {
                                threadStatusText.innerHTML = '<span class="font-medium text-zinc-500">OFF</span> <span class="text-zinc-600">(' + data.current_browsers + '/' + data.max_threads + ' browsers)</span>';
                                toggleThreadBtn.textContent = 'Turn ON';
                                toggleThreadBtn.className = 'rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-emerald-950/20 transition hover:bg-emerald-500';
                            }
                        }

                        if (applyThreadCountBtn) {
                            applyThreadCountBtn.addEventListener('click', async function() {
                                var count = browserQuantityInput ? Number(browserQuantityInput.value) : 1;
                                applyThreadCountBtn.disabled = true;
                                try {
                                    var res = await fetch('/setThreadCount', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ threadCount: count })
                                    });
                                    if (redirectIfUnauthorized(res)) return;
                                    var data = await res.json();
                                    if (!data.success) {
                                        alert(data.error || 'Failed to update browser quantity');
                                    }
                                    setTimeout(fetchThreadStatus, 1000);
                                    setTimeout(fetchThreadStatus, 3000);
                                } catch (e) {
                                    alert('Failed to update browser quantity');
                                } finally {
                                    applyThreadCountBtn.disabled = false;
                                    fetchThreadStatus();
                                }
                            });
                        }

                        if (toggleThreadBtn) {
                            toggleThreadBtn.addEventListener('click', async function() {
                                toggleThreadBtn.disabled = true;
                                toggleThreadBtn.textContent = 'Wait...';
                                try {
                                    var res = await fetch('/toggleMultiThread', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ enable: !isMultiThread })
                                    });
                                    if (redirectIfUnauthorized(res)) return;
                                    var data = await res.json();
                                    if (data.success) {
                                        isMultiThread = data.multi_thread;
                                        setTimeout(fetchThreadStatus, 1000);
                                        setTimeout(fetchThreadStatus, 3000);
                                    }
                                } catch (e) {
                                    alert('Failed to toggle multi-thread mode');
                                } finally {
                                    toggleThreadBtn.disabled = false;
                                    fetchThreadStatus();
                                }
                            });
                        }
                        
                        fetchThreadStatus();

                        var logoutBtn = document.getElementById('logout-btn');
                        if (logoutBtn) {
                            logoutBtn.addEventListener('click', async function () {
                                try {
                                    await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
                                } catch (e) { }
                                window.location.href = '/login';
                            });
                        }

                        // Add listeners for JSON copy buttons
                        var jsonCopyBtns = document.querySelectorAll('.copy-json-btn');
                        jsonCopyBtns.forEach(function(btn) {
                            btn.addEventListener('click', function() {
                                var codeBlock = this.parentElement.querySelector('.json-content');
                                if (codeBlock) {
                                    var text = codeBlock.textContent;
                                    if (navigator.clipboard && navigator.clipboard.writeText) {
                                        navigator.clipboard.writeText(text).then(function() {
                                            btn.textContent = 'Copied!';
                                            setTimeout(function() { btn.textContent = 'Copy'; }, 2000);
                                        });
                                    } else {
                                        var textArea = document.createElement("textarea");
                                        textArea.value = text;
                                        document.body.appendChild(textArea);
                                        textArea.select();
                                        try {
                                            document.execCommand('copy');
                                            btn.textContent = 'Copied!';
                                            setTimeout(function() { btn.textContent = 'Copy'; }, 2000);
                                        } catch (err) { }
                                        document.body.removeChild(textArea);
                                    }
                                }
                            });
                        });
                    }

                    if (document.readyState === 'loading') {
                        document.addEventListener('DOMContentLoaded', wireLiveTest);
                    } else {
                        wireLiveTest();
                    }
                })();
            """
            + "</script>"
            + """
            </body>
            </html>
            """
        )
        return html.replace("__IVAC_SIGNIN_URL__", DEFAULT_LIVE_TEST_URL).replace(
            "__IVAC_SITEKEY__", DEFAULT_LIVE_TEST_SITEKEY
        )


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Cloudflare Turnstile API Server")

    parser.add_argument('--headless', action='store_true', help='Run the browser in headless mode, without opening a graphical interface. This option requires the --useragent argument to be set (default: False)')
    parser.add_argument('--useragent', type=str, default=None, help='Specify a custom User-Agent string for the browser. If not provided, the default User-Agent is used')
    parser.add_argument('--debug', action='store_true', help='Enable or disable debug mode for additional logging and troubleshooting information (default: False)')
    parser.add_argument('--browser_type', type=str, default='camoufox', help='Specify the browser type for the solver. Supported options: chromium, chrome, msedge, camoufox (default: camoufox)')
    parser.add_argument('--thread', type=int, default=1, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', action='store_true', help='Enable proxy support for the solver (Default: False)')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5000', help='Set the port for the API solver to listen on. (Default: 5000)')
    return parser.parse_args()


def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool) -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support)
    return server.app


if __name__ == '__main__':
    args = parse_args()
    browser_types = [
        'chromium',
        'chrome',
        'msedge',
        'camoufox',
    ]
    if args.browser_type not in browser_types:
        logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    elif args.headless is True and args.useragent is None and "camoufox" not in args.browser_type:
        logger.error(f"You must specify a {COLORS.get('YELLOW')}User-Agent{COLORS.get('RESET')} for Cloudflare Turnstile Solver or use {COLORS.get('GREEN')}camoufox{COLORS.get('RESET')} without useragent")
    else:
        app = create_app(headless=args.headless, debug=args.debug, useragent=args.useragent, browser_type=args.browser_type, thread=args.thread, proxy_support=args.proxy)
        app.run(host=args.host, port=int(args.port))
