import base64
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---- Config (env vars override defaults) ----
TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://console.cloud.google.com/auth/audience?project=scoutwise-01&pli=1",
)
EXPECTED_TEXT = os.getenv("EXPECTED_TEXT", "ademola24434.come@gmail.com")
MAX_WAIT_SECONDS = int(os.getenv("MAX_WAIT_SECONDS", "60"))
PROFILE_DIR = Path(os.getenv("PROFILE_DIR", "/tmp/chrome_profile"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/outputs"))
TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN", "")  # optional; if set, required in X-Token header


class HeadlessChromeCheck:
    LAUNCH_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    def __init__(self, url, expected_text, max_wait, profile_dir, output_dir):
        self.url = url
        self.expected_text = expected_text
        self.max_wait = max_wait
        self.profile_dir = profile_dir
        self.output_dir = output_dir

    def run(self, include_screenshot: bool = False) -> dict:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel="chromium",
                headless=True,
                viewport={"width": 1366, "height": 900},
                args=self.LAUNCH_ARGS,
                ignore_default_args=["--enable-automation"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._mask_headless_ua(context, page)

                start = time.time()
                loaded, reason = self._open_and_wait(page, start)
                elapsed = round(time.time() - start, 1)
                final_url = page.url

                try:
                    title = page.title()
                except Exception:
                    title = "?"

                if not loaded and "accounts.google.com" in final_url:
                    reason += " | redirected to Google sign-in (session not valid in this profile)"

                screenshot_path, screenshot_b64 = self._screenshot(page, stamp, include_screenshot)
                cookies = self._cookies(context, stamp)

                return {
                    "loaded": loaded,
                    "result": "LOADED OK" if loaded else "NOT VERIFIED",
                    "reason": reason,
                    "elapsed_seconds": elapsed,
                    "final_url": final_url,
                    "page_title": title,
                    "browser_version": context.browser.version if context.browser else None,
                    "screenshot_path": screenshot_path,
                    "screenshot_base64": screenshot_b64,
                    "cookie_count": len(cookies["list"]),
                    "cookies_file": cookies["file"],
                    "cookies": cookies["list"],  # name + domain only, no values
                }
            finally:
                context.close()

    def _mask_headless_ua(self, context, page):
        ua = page.evaluate("navigator.userAgent")
        if "HeadlessChrome" in ua:
            cdp = context.new_cdp_session(page)
            cdp.send(
                "Emulation.setUserAgentOverride",
                {"userAgent": ua.replace("HeadlessChrome", "Chrome")},
            )

    def _open_and_wait(self, page, start):
        try:
            page.goto(self.url, wait_until="domcontentloaded", timeout=self.max_wait * 1000)
            remaining = max(1.0, self.max_wait - (time.time() - start))
            page.locator("td[role='gridcell']", has_text=self.expected_text).first.wait_for(
                state="visible", timeout=remaining * 1000
            )
            return True, ""
        except PWTimeout:
            return False, f"expected text not seen within {self.max_wait}s"
        except Exception as e:
            return False, f"error: {e}"

    def _screenshot(self, page, stamp, include_b64):
        path = self.output_dir / f"headless_check_{stamp}.png"
        try:
            page.screenshot(path=str(path))
        except Exception as e:
            return f"failed: {e}", None
        b64 = base64.b64encode(path.read_bytes()).decode() if include_b64 else None
        return str(path), b64

    def _cookies(self, context, stamp):
        try:
            raw = context.cookies()
        except Exception as e:
            return {"file": f"failed: {e}", "list": []}
        out_file = self.output_dir / f"cookies_headless_{stamp}.json"
        out_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        listing = [
            {"name": c["name"], "domain": c["domain"]}
            for c in sorted(raw, key=lambda c: (c["domain"], c["name"]))
        ]
        return {"file": str(out_file), "list": listing}


app = FastAPI()
checker = HeadlessChromeCheck(TARGET_URL, EXPECTED_TEXT, MAX_WAIT_SECONDS, PROFILE_DIR, OUTPUT_DIR)
run_lock = threading.Lock()  # one browser at a time: same profile dir + memory limits


@app.get("/")
def health():
    return {"status": "ok"}


# Plain `def` (not async): FastAPI runs it in a worker thread, which Playwright's sync API requires.
@app.get("/run")
def run(screenshot: bool = False, x_token: str = Header(default="")):
    if TRIGGER_TOKEN and x_token != TRIGGER_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="a run is already in progress")
    try:
        return checker.run(include_screenshot=screenshot)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"browser run failed: {e}")
    finally:
        run_lock.release()