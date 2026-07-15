from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


CDP_URL = "http://127.0.0.1:9222"
FRONTEND_URL = "http://127.0.0.1:8501"
BOSS_URL = "https://www.zhipin.com/shanghai/"
LIEPIN_URL = "https://www.liepin.com/"
CHROME_BIN = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_PROFILE = Path.home() / ".ai-job-collector-chrome"


def is_boss_url(url: str) -> bool:
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    lowered = (url or "").lower()
    excluded = ("socket-worker", "/assets/", "service_worker", "chrome://", "localhost")
    return (
        (hostname == "zhipin.com" or hostname.endswith(".zhipin.com"))
        and not any(marker in lowered for marker in excluded)
        and not parsed.path.lower().endswith(".js")
    )


def boss_url_priority(url: str) -> int:
    path = urlparse(url or "").path.lower()
    if "/web/geek/jobs" in path:
        return 0
    if path.startswith("/shanghai"):
        return 1
    if "/job_detail/" in path:
        return 2
    return 3


def is_liepin_url(url: str) -> bool:
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    lowered = (url or "").lower()
    return (
        (hostname == "liepin.com" or hostname.endswith(".liepin.com"))
        and not any(marker in lowered for marker in (
            "assets", "service_worker", "chrome://", "about:blank", "localhost", "127.0.0.1",
        ))
        and not parsed.path.lower().endswith(".js")
    )


def liepin_url_priority(url: str) -> int:
    lowered = (url or "").lower()
    path = urlparse(url or "").path.lower()
    if any(value in lowered for value in ("key=", "keyword=", "dq=", "/zhaopin/")):
        return 0
    if any(value in path for value in ("/job/", "/jobs/", "/search/")):
        return 1
    return 2


def get_cdp_pages(timeout: float = 1.0) -> list[dict[str, Any]]:
    try:
        with urlopen(f"{CDP_URL}/json/list", timeout=timeout) as response:
            payload = json.load(response)
        return payload if isinstance(payload, list) else []
    except (OSError, ValueError):
        return []


def browser_status() -> dict[str, Any]:
    pages = get_cdp_pages()
    boss_pages = [
        page for page in pages
        if str(page.get("type", "")) == "page" and is_boss_url(str(page.get("url", "")))
    ]
    boss_pages.sort(key=lambda page: boss_url_priority(str(page.get("url", ""))))
    boss_url = str(boss_pages[0].get("url", "")) if boss_pages else ""
    boss_path = urlparse(boss_url).path.lower() if boss_url else ""
    login_required = any(marker in boss_path for marker in ("/login", "/passport/", "/verify"))
    return {
        "chrome_running": bool(pages),
        "boss_found": bool(boss_pages),
        "boss_url": boss_url,
        "login_required": login_required,
        "boss_state": (
            "Login required" if login_required else ("Connected" if boss_pages else "Page unavailable")
        ),
        "pages": [str(page.get("url", "")) for page in pages],
    }


def platform_browser_status(platform: str) -> dict[str, Any]:
    if platform == "boss":
        status = browser_status()
        return {
            **status, "platform": "boss", "platform_found": status["boss_found"],
            "page_url": status["boss_url"],
            "platform_state": status.get(
                "boss_state", "Connected" if status.get("boss_found") else "Page unavailable"
            ),
        }
    pages = get_cdp_pages()
    candidates = [
        page for page in pages
        if str(page.get("type", "")) == "page" and is_liepin_url(str(page.get("url", "")))
    ]
    candidates.sort(key=lambda page: liepin_url_priority(str(page.get("url", ""))))
    page_url = str(candidates[0].get("url", "")) if candidates else ""
    path = urlparse(page_url).path.lower() if page_url else ""
    login_required = any(value in path for value in ("login", "passport"))
    return {
        "chrome_running": bool(pages), "platform": "liepin",
        "platform_found": bool(candidates), "page_url": page_url,
        "login_required": login_required,
        "platform_state": (
            "Login required" if login_required else ("Connected" if candidates else "Page unavailable")
        ),
        "pages": [str(page.get("url", "")) for page in pages],
    }


def open_cdp_tab(url: str) -> dict[str, Any]:
    endpoint = f"{CDP_URL}/json/new?{quote(url, safe=':/?=&')}"
    request = Request(endpoint, method="PUT")
    with urlopen(request, timeout=3) as response:
        return json.load(response)


def ensure_cdp_tab(url: str, matcher) -> bool:
    if any(matcher(str(page.get("url", ""))) for page in get_cdp_pages()):
        return False
    open_cdp_tab(url)
    return True


def _playwright_boss_page(context, current_page=None, excluded_ids: set[int] | None = None):
    excluded_ids = excluded_ids or set()
    candidates = []
    if current_page is not None:
        candidates.append(current_page)
    try:
        candidates.extend(reversed(list(context.pages)))
    except Exception:
        return None
    valid = []
    seen: set[int] = set()
    for page in candidates:
        if id(page) in seen or id(page) in excluded_ids:
            continue
        seen.add(id(page))
        try:
            if page.is_closed():
                continue
            url = page.url
        except Exception:
            continue
        if is_boss_url(url):
            valid.append(page)
    return min(valid, key=lambda page: boss_url_priority(page.url), default=None)


def _boss_page_needs_login(page) -> bool:
    try:
        path = urlparse(page.url or "").path.lower()
        if "/login" in path or "/passport/" in path or "/verify" in path:
            return True
    except Exception:
        return True
    selectors = (
        'input[placeholder*="手机号"]',
        'input[placeholder*="手机号码"]',
        '[class*="login-register"]',
        '[class*="login-dialog"]',
        'a[ka*="header-login"]',
        'a[href*="/web/user/"][href*="login"]',
        'button:has-text("登录")',
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return True
        except Exception:
            continue
    return False


def ensure_boss_page_health(*, create_if_missing: bool = True, timeout: float = 10.0,
                            playwright_factory=None, open_tab_fn=None,
                            sleep_fn=time.sleep) -> dict[str, Any]:
    """Validate a real page through Playwright/CDP and recreate it when needed."""
    result: dict[str, Any] = {
        "ok": False,
        "state": "Page unavailable",
        "chrome_running": False,
        "boss_url": "",
        "created": False,
        "login_required": False,
        "message": "",
    }
    if not get_cdp_pages(timeout=min(timeout, 2.0)):
        result["message"] = "Dedicated Chrome is not running or the CDP endpoint is unavailable"
        return result
    result["chrome_running"] = True
    if playwright_factory is None:
        from playwright.sync_api import sync_playwright
        playwright_factory = sync_playwright
    open_tab_fn = open_tab_fn or open_cdp_tab
    playwright = None
    try:
        playwright = playwright_factory().start()
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            result["message"] = "Dedicated Chrome has no available browser context"
            return result
        context = browser.contexts[0]
        excluded_ids: set[int] = set()
        page = _playwright_boss_page(context)
        for _attempt in range(2):
            if page is None and create_if_missing:
                result["state"] = "Reconnecting"
                open_tab_fn(BOSS_URL)
                result["created"] = True
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    page = _playwright_boss_page(context, excluded_ids=excluded_ids)
                    if page is not None:
                        break
                    sleep_fn(0.25)
            if page is None:
                result["message"] = "No usable BOSS Zhipin page was found"
                return result
            try:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=int(timeout * 1000))
                except Exception:
                    pass
                if page.is_closed() or not is_boss_url(page.url):
                    raise RuntimeError("The BOSS Zhipin page is closed or its URL is no longer valid")
                page.bring_to_front()
                result["boss_url"] = page.url
                result["login_required"] = _boss_page_needs_login(page)
                result["state"] = "Login required" if result["login_required"] else "Connected"
                result["ok"] = not result["login_required"]
                result["message"] = (
                    "Sign in on the BOSS Zhipin page, then continue"
                    if result["login_required"] else "BOSS Zhipin page health check passed"
                )
                return result
            except Exception as exc:
                excluded_ids.add(id(page))
                page = _playwright_boss_page(context, excluded_ids=excluded_ids)
                result["message"] = f"Previous BOSS Zhipin page is invalid: {exc}"
        result["state"] = "Page unavailable"
        return result
    except Exception as exc:
        result["state"] = "Page unavailable"
        result["message"] = f"BOSS Zhipin page health check failed: {exc}"
        return result
    finally:
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


def ensure_liepin_page_health(*, create_if_missing: bool = True, timeout: float = 10.0,
                              playwright_factory=None, open_tab_fn=None,
                              sleep_fn=time.sleep) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False, "state": "Page unavailable", "chrome_running": False,
        "page_url": "", "created": False, "login_required": False, "message": "",
    }
    if not get_cdp_pages(timeout=min(timeout, 2.0)):
        result["message"] = "Dedicated Chrome is not running or the CDP endpoint is unavailable"
        return result
    result["chrome_running"] = True
    if playwright_factory is None:
        from playwright.sync_api import sync_playwright
        playwright_factory = sync_playwright
    open_tab_fn = open_tab_fn or open_cdp_tab
    playwright = None
    try:
        playwright = playwright_factory().start()
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            result["message"] = "Dedicated Chrome has no available browser context"
            return result
        context = browser.contexts[0]

        def select_page():
            candidates = []
            for page in reversed(list(context.pages)):
                try:
                    if not page.is_closed() and is_liepin_url(page.url):
                        candidates.append(page)
                except Exception:
                    continue
            return min(candidates, key=lambda item: liepin_url_priority(item.url), default=None)

        page = select_page()
        if page is None and create_if_missing:
            result["state"] = "Reconnecting"
            open_tab_fn(LIEPIN_URL)
            result["created"] = True
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                page = select_page()
                if page is not None:
                    break
                sleep_fn(0.25)
        if page is None:
            result["message"] = "No usable Liepin page was found"
            return result
        page.bring_to_front()
        result["page_url"] = page.url
        path = urlparse(page.url or "").path.lower()
        login_required = any(value in path for value in ("login", "passport"))
        if not login_required:
            for selector in ('input[placeholder*="手机号"]', 'button:has-text("登录")'):
                try:
                    locator = page.locator(selector).first
                    if locator.count() and locator.is_visible():
                        login_required = True
                        break
                except Exception:
                    continue
        result["login_required"] = login_required
        result["state"] = "Login required" if login_required else "Connected"
        result["ok"] = not login_required
        result["message"] = (
            "Sign in on the Liepin page, then continue"
            if login_required else "Liepin page health check passed"
        )
        return result
    except Exception as exc:
        result["message"] = f"Liepin page health check failed: {exc}"
        return result
    finally:
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


def ensure_platform_page_health(platform: str, **kwargs: Any) -> dict[str, Any]:
    if platform == "boss":
        result = ensure_boss_page_health(**kwargs)
        return {**result, "page_url": result.get("boss_url", ""), "platform": "boss"}
    if platform == "liepin":
        return {**ensure_liepin_page_health(**kwargs), "platform": "liepin"}
    raise ValueError(f"Platform not implemented: {platform}")


def read_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if pid > 0 else None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate_pid_file(path: Path, expected_command: str, timeout: float = 5.0) -> bool:
    pid = read_pid(path)
    if not pid_alive(pid):
        path.unlink(missing_ok=True)
        return False
    command = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    ).stdout
    if expected_command not in command:
        raise RuntimeError(f"PID {pid} does not belong to the collector; stop refused: {command.strip()}")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.1)
    if pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    return True


def _dedicated_chrome_pids() -> list[int]:
    result = subprocess.run(
        ["/usr/bin/pgrep", "-f", str(CHROME_PROFILE)], capture_output=True, text=True
    )
    return [int(value) for value in result.stdout.split() if value.isdigit()]


def stop_dedicated_chrome(pid_dir: Path) -> bool:
    pids = _dedicated_chrome_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 8
    while time.time() < deadline and any(pid_alive(pid) for pid in pids):
        time.sleep(0.2)
    for pid in pids:
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    (pid_dir / "chrome.pid").unlink(missing_ok=True)
    return bool(pids)


def restart_dedicated_chrome(pid_dir: Path, log_dir: Path) -> int:
    stop_dedicated_chrome(pid_dir)
    if not CHROME_BIN.exists():
        raise RuntimeError(f"Google Chrome was not found: {CHROME_BIN}")
    log_dir.mkdir(parents=True, exist_ok=True)
    handle = (log_dir / "chrome.log").open("a", encoding="utf-8")
    process = subprocess.Popen(
        [
            str(CHROME_BIN), "--remote-debugging-port=9222",
            f"--user-data-dir={CHROME_PROFILE}", "--new-window", BOSS_URL,
        ],
        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
    )
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "chrome.pid").write_text(str(process.pid), encoding="utf-8")
    for _ in range(40):
        if get_cdp_pages(timeout=0.5):
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("The CDP endpoint did not become ready after Chrome restarted")
    ensure_cdp_tab(BOSS_URL, is_boss_url)
    ensure_cdp_tab(FRONTEND_URL, lambda value: value.startswith(FRONTEND_URL))
    return process.pid


def stop_service(pid_dir: Path, close_chrome: bool, delay: float = 1.0) -> None:
    time.sleep(delay)
    terminate_pid_file(pid_dir / "scanner.pid", "main.py")
    if close_chrome:
        stop_dedicated_chrome(pid_dir)
    terminate_pid_file(pid_dir / "streamlit.pid", "streamlit run app.py")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["stop-service"])
    parser.add_argument("--pid-dir", required=True)
    parser.add_argument("--close-chrome", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()
    stop_service(Path(args.pid_dir), args.close_chrome, args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
