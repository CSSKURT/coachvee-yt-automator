"""
Optional fallback - upload + schedule through the YouTube Studio web UI with Playwright (0% AI).

Use when the Google Cloud project is unverified (API uploads get locked to private) or the
API quota is used up. It drives a real Chrome window with its own persistent profile in
automation_app\\browser_profile, which YOU sign in to once by hand (the "Studio Login Setup"
button in the GUI, or: .venv\\Scripts\\python.exe youtube_playwright_fallback.py --login).
This script never types a password.

YouTube Studio changes its page layout from time to time. All selectors are collected in
SEL below so they are easy to fix. On any failure a screenshot is saved to logs\\.
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import BROWSER_PROFILE_DIR, LOGS_DIR, load_config

STUDIO = "https://studio.youtube.com"
SEL = {
    "create_button": "#create-icon, ytcp-button#create-icon, button[aria-label='Create']",
    "upload_menu_item": "#text-item-0, tp-yt-paper-item[test-id='upload-beta']",
    "file_input":"ytcp-uploads-file-picker input[type='file'], input[type='file'][name='Filedata']",
    "title_box": "#title-textarea #textbox",
    "desc_box": "#description-textarea #textbox",
    "not_for_kids": "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK'], [name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
    "next": "#next-button",
    "done": "#done-button",
    "video_link": "ytcp-video-info a, .video-url-fadeable a, a[href*='youtu.be/'], a[href*='/shorts/']",
    "progress": "ytcp-video-upload-progress .progress-label, .progress-label",
    "subtitles_button": "#subtitles-button, ytcp-button#subtitles-button",
    "schedule_expand": "#second-container-expand-button",
    "schedule_radio": "#schedule-radio-button",
    "date_trigger": "#datepicker-trigger",
    "date_input": "ytcp-date-picker tp-yt-paper-input input, #textbox input[aria-label*='date' i]",
    "time_input": "#time-of-day-container input, ytcp-form-input-container#time-of-day-trigger input",
    "close_after": "ytcp-uploads-still-processing-dialog #close-button, #close-button",
}


class StudioError(Exception):
    def __init__(self, msg, before_upload=False, done_clicked=False, video_id=None):
        super().__init__(msg)
        self.before_upload = before_upload      # True = no video was created yet, safe to retry
        self.done_clicked = done_clicked        # True = Schedule/Save was pressed: check Studio before calling it failed
        self.video_id = video_id


def _launch(p, cfg, headless=None):
    kwargs = dict(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        headless=cfg.get("playwright_headless", False) if headless is None else headless,
        locale="en-US",                      # date/time formats below assume en-US
        timezone_id="Asia/Manila",           # Studio schedules in the browser's time zone
        color_scheme="light",                # keep Studio in light mode (same look every run)
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    ch = (cfg.get("playwright_browser_channel") or "").strip()
    if ch:
        try:
            return p.chromium.launch_persistent_context(channel=ch, **kwargs)
        except Exception:
            pass
    return p.chromium.launch_persistent_context(**kwargs)


def open_login_browser() -> None:
    """Opens Chrome on YouTube Studio so the user can sign in to the channel's account by hand."""
    from playwright.sync_api import sync_playwright
    cfg = load_config()
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = _launch(p, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(STUDIO)
        try:
            ctx.wait_for_event("close", timeout=0)       # until the user closes the window
        except Exception:
            pass


def _shot(page, tag: str) -> str:
    path = LOGS_DIR / f"studio_{tag}_{datetime.now():%Y%m%d_%H%M%S}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass
    return str(path)


def _type_multiline(page, locator, text: str):
    locator.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            page.keyboard.press("Enter")


def _click_next(page, times=1):
    for _ in range(times):
        page.locator(SEL["next"]).first.click()
        page.wait_for_timeout(1500)


def _wait_upload_finished(page, log, timeout_min=60):
    """Do not close the dialog/browser while the file is still uploading."""
    end = time.time() + timeout_min * 60
    last = ""
    while time.time() < end:
        try:
            txt = " ".join(page.locator(SEL["progress"]).all_inner_texts()).strip()
        except Exception:
            txt = ""
        if txt and txt != last:
            log(f"    Studio: {txt}")
            last = txt
        if txt and not re.search(r"upload(ing)?\s*\d+\s*%|uploading", txt, re.I):
            return
        page.wait_for_timeout(3000)
    raise StudioError("upload did not finish in time")


def _try_subtitles(page, srt_path: str, log) -> bool:
    """Best effort: Video elements -> Add subtitles -> Upload file -> With timing -> choose .srt."""
    try:
        page.locator(SEL["subtitles_button"]).first.click(timeout=8000)
        page.wait_for_timeout(1500)
        page.get_by_text(re.compile(r"^\s*Upload file\s*$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(1000)
        page.get_by_text(re.compile(r"With timing", re.I)).first.click(timeout=8000)
        with page.expect_file_chooser(timeout=15000) as fc:
            page.get_by_role("button", name=re.compile(r"Continue", re.I)).first.click()
        fc.value.set_files(srt_path)
        page.wait_for_timeout(4000)
        page.get_by_role("button", name=re.compile(r"^\s*Done\s*$", re.I)).last.click(timeout=10000)
        page.wait_for_timeout(1500)
        log("    Studio: subtitles attached")
        return True
    except Exception as e:
        log(f"    Studio: could not attach subtitles automatically ({str(e).splitlines()[0][:120]})")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


CONFIRM_RE = re.compile(r"Video (scheduled|published|saved|uploaded)|will be set to (public|private|unlisted) on|"
                        r"(saved|set) as private|Video processing", re.I)


def _set_schedule(page, publish_at: datetime, log):
    """Visibility step: Schedule -> date -> time (Manila)."""
    try:
        page.locator(SEL["schedule_expand"]).first.click(timeout=8000)
    except Exception:
        page.locator(SEL["schedule_radio"]).first.click(timeout=8000)
    page.wait_for_timeout(1000)
    page.locator(SEL["date_trigger"]).first.click()
    d = page.locator(SEL["date_input"]).first
    d.click()
    page.keyboard.press("Control+A")
    page.keyboard.insert_text(f"{publish_at.strftime('%b')} {publish_at.day}, {publish_at.year}")
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)
    t = page.locator(SEL["time_input"]).first
    t.click()
    page.keyboard.press("Control+A")
    page.keyboard.insert_text(publish_at.strftime("%I:%M %p").lstrip("0"))
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)
    shown_time = t.input_value() if t.count() else ""
    log(f"    Studio: schedule set to {publish_at:%b %d %Y} {shown_time or publish_at.strftime('%I:%M %p')} (Asia/Manila)")


_VISIBLE_DIALOG_TEXT_JS = r"""() => [...document.querySelectorAll(
    'tp-yt-paper-dialog, ytcp-dialog, ytcp-uploads-still-processing-dialog, ytcp-video-share-dialog, [role=dialog]')]
    .filter(d => d.offsetParent !== null && getComputedStyle(d).visibility !== 'hidden')
    .map(d => d.innerText || '').join('\n---\n')"""

_CLICK_VISIBLE_GOT_IT_JS = r"""() => {
    const b = [...document.querySelectorAll('button, ytcp-button, tp-yt-paper-button')]
      .find(x => x.offsetParent !== null && /^\s*Got it\s*$/i.test(x.innerText || ''));
    if (b) { b.click(); return true; } return false; }"""


def _finish_and_confirm(page, log, timeout_s: int = 120, state: dict | None = None):
    """Press Schedule/Save ONCE, then poll (every 0.5 s) the VISIBLE dialogs for Studio's confirmation.

    - Only visible dialogs count: the hidden 'still checking your content' text stays in the page
      after 'Got it' and used to make this loop spin for 30-70 s (2026-11-24 bug).
    - 'Got it' is clicked instantly via JS, only when that button is really on screen.
    - Schedule is never pressed twice.
    """
    page.locator(SEL["done"]).first.click()
    if state is not None:
        state["done_clicked"] = True
    end = time.time() + timeout_s
    while time.time() < end:
        dialogs = page.evaluate(_VISIBLE_DIALOG_TEXT_JS) or ""
        m = CONFIRM_RE.search(dialogs)
        if m:
            log(f"    Studio: confirmed - '{m.group(0)}'")
            try:
                page.locator(SEL["close_after"]).first.click(timeout=2000)
            except Exception:
                pass
            return
        if re.search(r"still checking your content", dialogs, re.I) and page.evaluate(_CLICK_VISIBLE_GOT_IT_JS):
            log("    Studio: 'still checking your content' notice - clicked Got it")
        page.wait_for_timeout(500)
    raise StudioError("Studio never confirmed the schedule (video may still be a draft)", done_clicked=True)


def list_studio_shorts(cfg: dict, max_rows: int = 40, log=print) -> list:
    """Read-only: [{video_id, title, status, date}] from Content -> Shorts (newest first)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _launch(p, cfg)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(STUDIO, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            m = re.search(r"/channel/(UC[\w-]+)", page.url)
            if not m:
                page.wait_for_url(re.compile(r"/channel/UC"), timeout=30000)
                m = re.search(r"/channel/(UC[\w-]+)", page.url)
            page.goto(f"{STUDIO}/channel/{m.group(1)}/videos/short", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            rows, out = page.locator("ytcp-video-row"), []
            for k in range(min(rows.count(), max_rows)):
                r = rows.nth(k)
                try:
                    r.scroll_into_view_if_needed(timeout=5000)
                    page.wait_for_timeout(200)
                except Exception:
                    pass
                html, text = r.inner_html(), r.inner_text()
                ids = re.findall(r"/vi(?:_webp)?/([\w-]{11})/|/video/([\w-]{11})/", html)
                vid = next((a or b for a, b in ids), None)
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                status = next((l for l in lines if l in ("Scheduled", "Draft", "Private", "Public", "Unlisted")), "")
                date = next((l for l in lines if re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", l)), "")
                title = next((l for l in lines if not re.fullmatch(r"\d+:\d{2}", l)), "")
                out.append({"video_id": vid, "title": title, "status": status, "date": date})
            return out
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def verify_in_studio(cfg: dict, title: str, video_id, log=print):
    """After an unclear ending (browser closed, timeout): look the video up in Studio by id or title."""
    key = re.sub(r"\s+", " ", title).strip()[:30]
    for row in list_studio_shorts(cfg, log=log):
        if (video_id and row["video_id"] == video_id) or (key and row["title"].startswith(key)):
            return row
    return None


def schedule_existing_draft(video_id: str, publish_at: datetime, cfg: dict, log=print) -> dict:
    """Open an already-uploaded DRAFT by its video ID and schedule it. Nothing is uploaded again."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _launch(p, cfg)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(STUDIO, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            if "accounts.google.com" in page.url:
                raise StudioError("Not signed in to YouTube Studio - click 'Studio Login Setup' in the app.")
            m = re.search(r"/channel/(UC[\w-]+)", page.url)
            if not m:
                page.wait_for_url(re.compile(r"/channel/UC"), timeout=30000)
                m = re.search(r"/channel/(UC[\w-]+)", page.url)
            page.goto(f"{STUDIO}/channel/{m.group(1)}/videos/short", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            rows = page.locator("ytcp-video-row")
            row = None
            for _ in range(8):                       # rows below the fold only fill in once scrolled to
                for i in range(rows.count()):
                    r = rows.nth(i)
                    try:
                        r.scroll_into_view_if_needed(timeout=5000)
                        page.wait_for_timeout(250)
                    except Exception:
                        pass
                    if video_id in r.inner_html():
                        row = r
                        break
                if row is not None:
                    break
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1500)
            if row is None:
                raise StudioError(f"draft {video_id} not found in Content -> Shorts")
            row.scroll_into_view_if_needed()
            btn = row.locator("ytcp-button.edit-draft-button")
            if not btn.count():
                raise StudioError(f"{video_id} is not a draft any more (already scheduled/published?)")
            btn.first.click()
            page.wait_for_timeout(3000)
            for _ in range(5):                       # Next until the Visibility step
                if page.locator(f'{SEL["schedule_expand"]}, {SEL["schedule_radio"]}').first.is_visible():
                    break
                page.locator(SEL["next"]).first.click()
                page.wait_for_timeout(1800)
            _set_schedule(page, publish_at, log)
            _finish_and_confirm(page, log)
            return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}
        except Exception as e:
            shot = _shot(page, "draft_error")
            raise StudioError(f"{str(e).splitlines()[0][:300]} (screenshot: {shot})")
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def upload_via_studio(video: str, srt: str, title: str, description: str, publish_at: datetime | None,
                      cfg: dict, log=print) -> dict:
    from playwright.sync_api import sync_playwright
    if not BROWSER_PROFILE_DIR.exists() or not any(BROWSER_PROFILE_DIR.iterdir()):
        raise StudioError("Studio browser profile is empty - click 'Studio Login Setup' in the app first.")
    with sync_playwright() as p:
        ctx = _launch(p, cfg)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30000)
        selected = False
        flow: dict = {}
        video_id = None
        try:
            page.goto(STUDIO, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            if "accounts.google.com" in page.url:
                raise StudioError("Not signed in to YouTube Studio - click 'Studio Login Setup' in the app.")
            m = re.search(r"/channel/(UC[\w-]+)", page.url)
            if not m:
                page.wait_for_url(re.compile(r"/channel/UC"), timeout=30000)
                m = re.search(r"/channel/(UC[\w-]+)", page.url)
            page.goto(f"{STUDIO}/channel/{m.group(1)}/videos/upload?d=ud", wait_until="domcontentloaded")
            picker = page.locator(SEL["file_input"]).first
            try:
                picker.wait_for(state="attached", timeout=20000)
            except Exception:                     # Studio sometimes ignores ?d=ud: open it via Create
                log("    Studio: upload window didn't open - using Create -> Upload videos")
                page.locator(SEL["create_button"]).first.click(timeout=15000)
                page.wait_for_timeout(1000)
                try:
                    page.locator(SEL["upload_menu_item"]).first.click(timeout=8000)
                except Exception:
                    page.get_by_text(re.compile(r"Upload videos?", re.I)).first.click(timeout=8000)
                picker.wait_for(state="attached", timeout=30000)
            picker.set_input_files(video, timeout=60000)
            selected = True
            log(f"    Studio: file selected ({Path(video).name})")

            title_box = page.locator(SEL["title_box"]).first
            title_box.wait_for(timeout=120000)
            page.wait_for_timeout(2000)
            _type_multiline(page, title_box, title)
            _type_multiline(page, page.locator(SEL["desc_box"]).first, description)
            page.locator(SEL["not_for_kids"]).first.click()

            video_id, url = None, None
            try:
                href = page.locator(SEL["video_link"]).first.get_attribute("href", timeout=60000) or ""
                mm = re.search(r"(?:youtu\.be/|/shorts/|v=)([\w-]{11})", href)
                if mm:
                    video_id, url = mm.group(1), f"https://youtu.be/{mm.group(1)}"
            except Exception:
                pass

            _click_next(page)                                # -> Video elements
            captions = _try_subtitles(page, srt, log) if srt else True     # text-only: nothing to attach
            _click_next(page, 2)                             # -> Checks -> Visibility

            if publish_at is not None:
                _set_schedule(page, publish_at, log)
            else:
                page.get_by_text(re.compile(r"^\s*Private\s*$")).first.click()

            _wait_upload_finished(page, log)
            _finish_and_confirm(page, log, state=flow)
            return {"video_id": video_id, "url": url, "captions_done": captions}
        except Exception as e:
            closed = "has been closed" in str(e)
            shot = "" if closed else f" (screenshot: {_shot(page, 'error')})"
            raise StudioError(("Chrome window was closed" if closed else str(e).splitlines()[0][:300]) + shot,
                              before_upload=not selected, done_clicked=flow.get("done_clicked", False),
                              video_id=video_id)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    if "--login" in sys.argv:
        open_login_browser()
    else:
        print(__doc__)
