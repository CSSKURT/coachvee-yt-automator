"""
Stage 5 - detached background uploader & scheduler (0% AI).

Started by the GUI's GO button as a detached Windows process. It:
  1. reads state.json["current_batch"] (written by the GUI when GO was clicked),
  2. for each video in slot order, uploads it as PRIVATE with status.publishAt = its slot,
     then attaches the English .srt as captions (language "en", name "English"),
  3. writes the result to state.json["completed_outputs"] right after EACH video, so a crash
     or internet drop on video #9 resumes at #9 without re-uploading #1-#8,
  4. on "quota exceeded" waits until the daily reset (midnight Pacific) and continues,
  5. when every Output in the batch is scheduled, sets next_start_index = last + 1 and shows
     a Windows notification.

Never opens a login window: if the YouTube token is missing/expired it stops and notifies.
Run manually (for debugging):  .venv\\Scripts\\python.exe background_uploader.py
Stop it: the GUI's "Stop uploader" button (creates logs\\uploader.stop).
"""
from __future__ import annotations

import json
import mimetypes
import os
import random
import socket
import ssl
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (PHT, UPLOADER_LOCK, UPLOADER_STOP, HeldLock, keep_awake, load_config, load_state,
                    metadata_path, next_quota_reset, notify, read_json_file, setup_logging, update_state)
from metadata_rules import clean_description, enforce_title, youtube_tags

log = setup_logging("uploader")

RETRY_STATUS = {500, 502, 503, 504}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "rateLimitExceeded",
                 "userRateLimitExceeded"}
DONE_STATUSES = ("scheduled", "uploaded_private")   # uploaded_private = slot had already passed


class QuotaExceeded(Exception):
    pass


class StopRequested(Exception):
    pass


class LoginProblem(Exception):
    pass


def _check_stop():
    if UPLOADER_STOP.exists():
        raise StopRequested()


def _set_uploader(**kw):
    def f(s):
        b = s.get("current_batch") or {}
        u = b.setdefault("uploader", {})
        u.update(kw, updated_at=datetime.now(PHT).isoformat(timespec="seconds"))
        s["current_batch"] = b
    update_state(f)


def _record(n: int, **kw):
    def f(s):
        rec = s["completed_outputs"].setdefault(str(n), {})
        rec.update(kw, updated_at=datetime.now(PHT).isoformat(timespec="seconds"))
    update_state(f)


def _http_reason(e) -> str:
    try:
        data = json.loads(e.content.decode("utf-8", "replace"))
        errs = data.get("error", {}).get("errors") or []
        if errs:
            return errs[0].get("reason", "")
        return data.get("error", {}).get("status", "")
    except Exception:
        return ""


def _sleep_checked(seconds: float, status_msg: str | None = None):
    end = time.time() + seconds
    while time.time() < end:
        _check_stop()
        time.sleep(min(30, max(0.5, end - time.time())))


# ============================================================================ YouTube Data API
def _youtube(cfg, state):
    from googleapiclient.discovery import build
    from google_auth import AuthError, get_credentials
    try:
        creds = get_credentials("youtube", cfg, state["youtube_email"], interactive=False, log=log.info)
    except AuthError as e:
        raise LoginProblem(str(e))
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _transient(e) -> bool:
    import httplib2
    if isinstance(e, (FileNotFoundError, PermissionError, IsADirectoryError)):
        return False
    return isinstance(e, (ConnectionError, socket.timeout, TimeoutError, ssl.SSLError,
                          httplib2.HttpLib2Error, OSError))


def api_upload_video(yt, item: dict, title: str, description: str, cfg: dict) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    publish_at: datetime = item["_publish_dt"]
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    if item.get("_schedule"):
        status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {
        "snippet": {"title": title, "description": description,
                    "categoryId": str(cfg.get("youtube_category_id", "22")),
                    "tags": youtube_tags(description)},
        "status": status,
    }
    video = item["video_path"]
    mime = mimetypes.guess_type(video)[0] or "video/*"
    media = MediaFileUpload(video, mimetype=mime, chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response, errors, last_pct = None, 0, -10
    while response is None:
        _check_stop()
        try:
            prog, response = req.next_chunk()
            if prog:
                pct = int(prog.progress() * 100)
                if pct >= last_pct + 10:
                    log.info(f"    uploading Output {item['output']}: {pct}%")
                    _set_uploader(message=f"Uploading Output {item['output']}: {pct}%")
                    last_pct = pct
            errors = 0
        except HttpError as e:
            reason = _http_reason(e)
            if e.resp.status in (403, 429) and reason in QUOTA_REASONS:
                raise QuotaExceeded(reason)
            if e.resp.status in RETRY_STATUS and errors < 10:
                errors += 1
                wait = min(300, 2 ** errors + random.random())
                log.warning(f"    server error {e.resp.status}, retry {errors} in {wait:.0f}s")
                _sleep_checked(wait)
                continue
            raise
        except Exception as e:
            if _transient(e) and errors < 30:       # internet drop: resumable upload continues
                errors += 1
                wait = min(300, 5 * errors)
                log.warning(f"    network problem ({e}); retry {errors} in {wait}s")
                _set_uploader(message=f"Network problem - retrying in {wait}s")
                _sleep_checked(wait)
                continue
            raise
    vid = response["id"]
    log.info(f"    uploaded Output {item['output']} -> https://youtu.be/{vid}")
    return vid


def api_insert_captions(yt, video_id: str, srt_path: str):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    body = {"snippet": {"videoId": video_id, "language": "en", "name": "English", "isDraft": False}}
    for attempt in range(1, 7):
        _check_stop()
        try:
            media = MediaFileUpload(srt_path, mimetype="application/octet-stream", resumable=True)
            yt.captions().insert(part="snippet", body=body, media_body=media, sync=False).execute()
            return
        except HttpError as e:
            reason = _http_reason(e)
            if e.resp.status in (403, 429) and reason in QUOTA_REASONS:
                raise QuotaExceeded(reason)
            if attempt < 6 and (e.resp.status in RETRY_STATUS or e.resp.status in (404, 409)):
                log.warning(f"    captions not accepted yet ({e.resp.status} {reason}); retry in 30s")
                _sleep_checked(30)
                continue
            raise
        except Exception as e:
            if _transient(e) and attempt < 6:
                _sleep_checked(20 * attempt)
                continue
            raise


# ============================================================================ main loop
def _prepare(item: dict) -> dict:
    item = dict(item)
    item["_publish_dt"] = datetime.fromisoformat(item["publish_at"])
    item["_schedule"] = item["_publish_dt"] > datetime.now(PHT) + timedelta(minutes=20)
    return item


def _metadata(n: int) -> tuple[str, str]:
    meta = read_json_file(metadata_path(n), None)
    if not isinstance(meta, dict) or not meta.get("title") or not meta.get("description"):
        raise RuntimeError(f"metadata_Output_{n}.json is missing a title or description")
    return enforce_title(meta["title"]), clean_description(meta["description"])


def _verify_after_unclear_end(n: int, item: dict, title: str, err, cfg: dict) -> dict | None:
    """The browser closed / timed out AFTER the file was uploaded. Look the video up in Studio:
    Scheduled (or Private when no slot) = success; Draft = record as draft (GO schedules it in place);
    not found = None (real failure). Never uploads again."""
    from youtube_playwright_fallback import verify_in_studio
    log.warning(f"  Output {n}: unclear ending ({err}) - checking YouTube Studio ...")
    try:
        row = verify_in_studio(cfg, title, getattr(err, "video_id", None), log.info)
    except Exception as e:
        log.warning(f"  Output {n}: could not check Studio ({e})")
        row = None
    if not row:
        log.warning(f"  Output {n}: not found in Studio - recorded as failed")
        return None
    ok_status = "Scheduled" if item["_schedule"] else "Private"
    log.info(f"  Output {n}: found in Studio as {row['status']} {row['date']} ({row['video_id']})")
    if row["status"] == ok_status or row["status"] == "Scheduled":
        return {"video_id": row["video_id"], "url": f"https://youtu.be/{row['video_id']}",
                "captions_done": True, "verified": True}
    if row["status"] == "Draft" and row["video_id"]:
        _record(n, status="draft", method="playwright", video_id=row["video_id"], publish_at=item["publish_at"],
                title=title, error=None, note="Found as a draft after the browser closed; GO schedules it")
        raise RuntimeError(f"left as a DRAFT in Studio ({row['video_id']}) - click GO to schedule it")
    return None


def process_item(item: dict, cfg: dict, state: dict, method: str, yt_holder: dict):
    n = item["output"]
    rec = state["completed_outputs"].get(str(n)) or {}
    if rec.get("status") in DONE_STATUSES and rec.get("captions_done", True):
        log.info(f"  Output {n}: already {rec['status']} ({rec.get('video_id')}) - skipping")
        return
    has_srt = bool(item.get("srt_path"))                  # empty = text-only video, no captions
    for k in ("video_path", "srt_path") if has_srt else ("video_path",):
        if not Path(item[k]).exists():
            raise RuntimeError(f"{k} not found: {item[k]}")
    title, desc = _metadata(n)
    when = item["_publish_dt"].strftime("%a %b %d %I:%M %p PHT")
    if not item["_schedule"]:
        log.warning(f"  Output {n}: slot {when} has already passed - uploading as PRIVATE without a schedule")

    if rec.get("status") == "draft" and rec.get("video_id"):
        # already uploaded to Studio but left as a draft: schedule that draft, never upload again
        if not item["_schedule"]:
            raise RuntimeError(f"slot {when} has passed - schedule draft {rec['video_id']} by hand in Studio")
        from youtube_playwright_fallback import schedule_existing_draft
        _set_uploader(message=f"Studio browser: scheduling draft of Output {n}")
        log.info(f"  Output {n}: scheduling existing draft {rec['video_id']} -> {when}")
        schedule_existing_draft(rec["video_id"], item["_publish_dt"], cfg, log.info)
        _record(n, status="scheduled", publish_at=item["publish_at"], error=None, note=None)
        log.info(f"  Output {n}: draft scheduled for {when} (Studio confirmed)")
        return

    if method == "playwright":
        from youtube_playwright_fallback import StudioError, upload_via_studio
        _set_uploader(message=f"Studio browser: uploading Output {n}")
        for attempt in (1, 2):
            try:
                res = upload_via_studio(item["video_path"], item["srt_path"], title, desc,
                                        item["_publish_dt"] if item["_schedule"] else None, cfg, log.info)
                break
            except StudioError as e:
                if attempt == 1 and e.before_upload:      # nothing was created on YouTube yet
                    log.warning(f"  Output {n}: Studio didn't open the uploader ({e}) - retrying once")
                    _sleep_checked(10)
                    continue
                if not e.before_upload:                   # the video exists on YouTube: ask Studio what happened
                    res = _verify_after_unclear_end(n, item, title, e, cfg)
                    if res:
                        break
                raise
        captions = res.get("captions_done", False) or not has_srt
        if not captions and res.get("video_id"):
            try:                                   # best effort: add captions through the API instead
                yt = yt_holder.get("yt") or _youtube(cfg, state)
                yt_holder["yt"] = yt
                api_insert_captions(yt, res["video_id"], item["srt_path"])
                captions = True
            except Exception as e:
                log.warning(f"    captions via API also failed ({e}); add the .srt in Studio by hand")
        _record(n, status="scheduled" if item["_schedule"] else "uploaded_private", method="playwright",
                video_id=res.get("video_id"), url=res.get("url"), publish_at=item["publish_at"],
                captions_done=captions, title=title, srt=item["srt_path"], error=None)
        return

    yt = yt_holder.get("yt") or _youtube(cfg, state)
    yt_holder["yt"] = yt
    vid = rec.get("video_id") if rec.get("status") in ("uploaded", "uploaded_private") else None
    if not vid:
        _set_uploader(message=f"Uploading Output {n} ({when})")
        log.info(f"  Output {n}: uploading '{title}' -> {when}")
        vid = api_upload_video(yt, item, title, desc, cfg)
        _record(n, status="uploaded", method="api", video_id=vid, url=f"https://youtu.be/{vid}",
                publish_at=item["publish_at"], captions_done=False, title=title, error=None)
    if has_srt:
        _set_uploader(message=f"Adding captions to Output {n}")
        api_insert_captions(yt, vid, item["srt_path"])
    _record(n, status="scheduled" if item["_schedule"] else "uploaded_private", captions_done=True,
            srt=item["srt_path"], uploaded_at=datetime.now(PHT).isoformat(timespec="seconds"))
    log.info(f"  Output {n}: " + (f"scheduled for {when}" + (" with English captions" if has_srt else " (text-only, no captions)") if item["_schedule"]
                                  else "uploaded as PRIVATE with captions (slot had passed - schedule it in Studio)"))


def main() -> int:
    lock = HeldLock(UPLOADER_LOCK)
    if not lock.acquire():
        log.info("Another uploader is already running - exiting.")
        return 0
    try:
        UPLOADER_STOP.unlink(missing_ok=True)
        keep_awake(True)
        cfg = load_config()
        state = load_state()
        batch = state.get("current_batch")
        if not batch or not batch.get("items"):
            log.info("No current_batch in state.json - nothing to do.")
            return 0
        method = batch.get("method", "api")
        items = [_prepare(i) for i in sorted(batch["items"], key=lambda i: i["publish_at"])]
        log.info(f"=== Uploader started: {len(items)} video(s), method={method}, pid={os.getpid()} ===")
        _set_uploader(status="running", pid=os.getpid(), message="Starting", started_at=datetime.now(PHT).isoformat(timespec="seconds"))
        yt_holder: dict = {}
        failed = []
        i = 0
        while i < len(items):
            item = items[i]
            n = item["output"]
            try:
                _check_stop()
                state = load_state()
                process_item(item, cfg, state, method, yt_holder)
                i += 1
            except QuotaExceeded as q:
                until = next_quota_reset()
                msg = (f"YouTube daily quota reached ({q}). Waiting until "
                       f"{until.strftime('%a %I:%M %p')} PHT, then continuing with Output {n}.")
                log.warning(msg)
                _set_uploader(status="waiting_quota", message=msg, waiting_until=until.isoformat())
                notify("Coach Vee uploader paused", msg)
                _sleep_checked((until - datetime.now(PHT)).total_seconds())
                _set_uploader(status="running", waiting_until=None)
                yt_holder.clear()                     # fresh client/token after a long wait
            except StopRequested:
                raise
            except LoginProblem as e:
                log.error(str(e))
                _set_uploader(status="stopped", message=f"Login needed: {e}")
                notify("Coach Vee uploader stopped", "YouTube login expired. Open the app and click GO again.")
                return 2
            except Exception as e:
                log.error(f"  Output {n}: FAILED - {e}\n{traceback.format_exc()}")
                cur = (load_state()["completed_outputs"].get(str(n)) or {}).get("status")
                _record(n, status="draft" if cur == "draft" else "failed", error=str(e)[:500])
                failed.append(n)
                i += 1

        state = load_state()
        nums = batch.get("outputs") or list(range(int(batch["start"]), int(batch["end"]) + 1))
        recs = {x: state["completed_outputs"].get(str(x)) or {} for x in nums}
        done = [x for x in nums if recs[x].get("status") in DONE_STATUSES]
        late = [x for x in done if recs[x].get("status") == "uploaded_private"]
        if len(done) == len(nums):
            def fin(s):
                s["next_start_index"] = max(int(s.get("next_start_index", 0)), int(batch["end"]) + 1)
                s["current_batch"]["status"] = "complete"
            update_state(fin)
            msg = f"Coach Vee Batch {batch['start']}–{batch['end']} Scheduled Successfully!"
            if late:
                msg += (f" (Output {', '.join(map(str, late))}: slot had passed, uploaded as private - "
                        f"set a time in YouTube Studio.)")
            _set_uploader(status="done", message=msg)
            log.info(msg)
            notify("Coach Vee Automator", msg)
        else:
            left = [x for x in nums if x not in done]
            msg = (f"{len(done)}/{len(nums)} scheduled. Not scheduled yet: "
                   + ", ".join(str(x) for x in left) + (f" (failed: {', '.join(map(str, failed))})" if failed else ""))
            _set_uploader(status="done_with_issues", message=msg)
            log.warning(msg)
            notify("Coach Vee uploader finished", msg)
        return 0
    except StopRequested:
        log.info("Stop requested - exiting. Run GO again to resume where it left off.")
        _set_uploader(status="stopped", message="Stopped by user. Click GO to resume.")
        return 0
    except Exception as e:
        log.error(f"Uploader crashed: {e}\n{traceback.format_exc()}")
        try:
            _set_uploader(status="crashed", message=str(e)[:300])
            notify("Coach Vee uploader crashed", str(e)[:200])
        except Exception:
            pass
        return 1
    finally:
        UPLOADER_STOP.unlink(missing_ok=True)
        keep_awake(False)
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
