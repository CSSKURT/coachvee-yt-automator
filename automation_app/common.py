"""
Shared helpers for the Coach Vee automation app (no AI anywhere in this file).

- Paths, config.json / state.json (state is guarded by a cross-process file lock,
  because the GUI and the detached uploader both write it).
- Weekly schedule math (Asia/Manila, 2 slots per day).
- Where each "Output N" lives on disk, and its pipeline status (derived from files).
- Small Windows helpers: hidden subprocesses, streamed output, toast notifications,
  keep-awake, locating the claude CLI.
"""
from __future__ import annotations

import base64
import contextlib
import glob
import json
import logging
import msvcrt
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
TOKENS_DIR = APP_DIR / "tokens"
LOGS_DIR = APP_DIR / "logs"
METADATA_DIR = APP_DIR / "metadata"
AI_WORK_DIR = APP_DIR / "ai_work"            # neutral cwd for `claude -p` (no project context)
BROWSER_PROFILE_DIR = APP_DIR / "browser_profile"
STATE_LOCK = APP_DIR / "state.lock"
UPLOADER_LOCK = LOGS_DIR / "uploader.lock"
UPLOADER_STOP = LOGS_DIR / "uploader.stop"
GUI_LOCK = LOGS_DIR / "gui.lock"
GPU_LOCK = LOGS_DIR / "gpu.lock"
ICON_PATH = APP_DIR / "coachvee.ico"

for _d in (TOKENS_DIR, LOGS_DIR, METADATA_DIR, AI_WORK_DIR):
    _d.mkdir(parents=True, exist_ok=True)

CREATE_NO_WINDOW = 0x08000000
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}

try:
    from zoneinfo import ZoneInfo
    PHT = ZoneInfo("Asia/Manila")
    PACIFIC = ZoneInfo("America/Los_Angeles")   # YouTube API quota resets at midnight Pacific
except Exception:                                # tzdata missing: Manila has no DST, so +08:00 is exact
    PHT = timezone(timedelta(hours=8), "PHT")
    PACIFIC = timezone(timedelta(hours=-8), "PST")

DEFAULT_CONFIG = {
    # --- existing pipeline (from WHISPER_PIPELINE_HANDOFF.md, do not change) ---
    "whisper_python": r"D:\whisper\venv\Scripts\python.exe",
    "whisper_dir": r"D:\whisper",
    "ffmpeg_path": r"D:\ytdl\ffmpeg.exe",
    "whisper_model": "large-v3",
    "whisper_language": "tl",
    "srt_max_chars": 30,                    # 30 for 9:16 vertical Shorts (45 = 16:9 landscape)
    "cpu_fallback_after_gpu_failures": True,
    # --- where downloads + Whisper output folders go ---
    "staging_dir": r"D:\bot\jobs",
    # --- Google OAuth ---
    "client_secret_path": str(APP_DIR / "tokens" / "client_secret.json"),
    # --- AI (Stage 3 only) ---
    "ai_provider": "gemini",                # "gemini" | "claude_cli"
    "gemini_api_key": "",
    "gemini_model": "gemini-3.5-flash",
    "gemini_fallback_models": ["gemini-3.5-flash", "gemini-3.5-flash-lite"],
    "claude_cli_path": "",                  # blank = auto-detect
    "claude_model": "sonnet",               # lighter on your Claude usage than the default model
    "ai_batch_phrases": 350,
    # --- YouTube ---
    "upload_method": "api",                 # "api" | "playwright"
    "youtube_category_id": "22",
    "playwright_browser_channel": "chrome", # use installed Google Chrome; "" = bundled Chromium
    "playwright_headless": False,
}

DEFAULT_STATE = {
    "next_start_index": 218,
    "batch_size": 14,
    "drive_folder_name": "EDITOR | SEM",
    "drive_email": "your-drive-account@gmail.com",
    "youtube_email": "your-youtube-account@gmail.com",
    "timezone": "Asia/Manila",
    "daily_slots_pht": ["21:00:00", "21:30:00"],
    "completed_outputs": {},
}


# ----------------------------------------------------------------------------- json io
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(50):                          # a reader may hold the file open for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


def read_json_file(path: Path, default=None):
    return _read_json(Path(path), default)


# ----------------------------------------------------------------------------- locks
@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0):
    """Blocking cross-process lock (msvcrt). Released automatically if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    deadline = time.time() + timeout
    while True:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.time() > deadline:
                f.close()
                raise TimeoutError(f"Could not lock {path}")
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        f.close()


class HeldLock:
    """A lock held for the life of the process (single-instance GUI / uploader)."""

    def __init__(self, path: Path):
        self.path = path
        self.f = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+b")
        try:
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.f.close()
            self.f = None
            return False

    def release(self):
        if self.f:
            try:
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.f.close()
            self.f = None


def is_locked(path: Path) -> bool:
    """True if another process currently holds the lock at `path`."""
    lk = HeldLock(path)
    if lk.acquire():
        lk.release()
        return False
    return True


# ----------------------------------------------------------------------------- config / state
def load_config() -> dict:
    data = _read_json(CONFIG_PATH, None)
    if data is None:
        data = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    if not CONFIG_PATH.exists():
        write_json_atomic(CONFIG_PATH, merged)
    return merged


def save_config(cfg: dict) -> None:
    write_json_atomic(CONFIG_PATH, cfg)


def _merged_state(data) -> dict:
    s = json.loads(json.dumps(DEFAULT_STATE))
    if isinstance(data, dict):
        s.update(data)
    s.setdefault("completed_outputs", {})
    return s


def load_state() -> dict:
    with file_lock(STATE_LOCK):
        data = _read_json(STATE_PATH, None)
        s = _merged_state(data)
        if data is None:
            write_json_atomic(STATE_PATH, s)
        return s


def update_state(fn) -> dict:
    """Read-modify-write state.json under the cross-process lock. `fn(state)` edits in place."""
    with file_lock(STATE_LOCK):
        s = _merged_state(_read_json(STATE_PATH, None))
        fn(s)
        write_json_atomic(STATE_PATH, s)
        return s


# ----------------------------------------------------------------------------- logging
def setup_logging(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOGS_DIR / f"{name}_{datetime.now():%Y%m%d}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    if sys.stdout and sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        log.addHandler(sh)
    return log


# ----------------------------------------------------------------------------- schedule
def now_pht() -> datetime:
    return datetime.now(PHT)


def next_monday(today: date | None = None) -> date:
    """The Monday after `today` (Manila time). Run on Sunday -> tomorrow."""
    today = today or now_pht().date()
    days = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=days)


def batch_numbers(start: int, size: int, skip=()) -> list[int]:
    """The next `size` Output numbers from `start`, leaving out any in `skip` (e.g. a video you dropped)."""
    skip = {int(x) for x in skip}
    out, n = [], int(start)
    while len(out) < int(size) and n < int(start) + int(size) + 500:
        if n not in skip:
            out.append(n)
        n += 1
    return out


def batch_schedule(nums, size_or_week, week_start=None, slots=None) -> dict[int, datetime]:
    """Output number -> publish datetime (PHT). Slot i goes to day i // len(slots).
    Call as batch_schedule(nums_list, week_start, slots)  (old form (start, size, week, slots) still works)."""
    if isinstance(nums, int):                      # old signature
        nums = list(range(nums, nums + int(size_or_week)))
    else:
        week_start, slots = size_or_week, week_start
    out = {}
    per_day = max(1, len(slots))
    for i, n in enumerate(nums):
        d = week_start + timedelta(days=i // per_day)
        hh, mm, *rest = (int(x) for x in slots[i % per_day].split(":"))
        ss = rest[0] if rest else 0
        out[int(n)] = datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=PHT)
    return out


def parse_skip(text: str) -> list[int]:
    """'241, 245-247' -> [241, 245, 246, 247]"""
    out = []
    for part in re.split(r"[,\s]+", text or ""):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out += list(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.append(int(part))
    return sorted(set(out))


def slot_label(dt: datetime, long: bool = False) -> str:
    t = dt.strftime("%I:%M %p").lstrip("0")
    if long:
        return f"{dt.strftime('%a %b')} {dt.day}, {t} PHT"
    return f"{dt.strftime('%a')} {dt.month}/{dt.day} · {t}"


def next_quota_reset() -> datetime:
    """Next midnight Pacific time (+5 min margin), returned in PHT."""
    now = datetime.now(PACIFIC)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return nxt.astimezone(PHT)


# ----------------------------------------------------------------------------- per-output paths
def job_dir(cfg: dict, n: int) -> Path:
    return Path(cfg["staging_dir"]) / f"Output_{n}"


def find_local_video(cfg: dict, n: int) -> Path | None:
    jd = job_dir(cfg, n)
    info = _read_json(jd / "job.json", None)
    if info and info.get("local_path") and Path(info["local_path"]).exists():
        return Path(info["local_path"])
    if not jd.exists():
        return None
    vids = [p for p in jd.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    return max(vids, key=lambda p: p.stat().st_mtime) if vids else None


OUTPUT_NAME_RE = re.compile(r"(?<![a-z])output[\s_\-]*0*(\d+)(?!\d)", re.I)


def organize_loose_videos(cfg: dict, log=None) -> list[str]:
    """Move loose 'OUTPUT250_FINAL.mp4' files in the jobs folder into 'Output_250\'.

    Skips files still being copied (locked or size still changing) - they are picked up on the next refresh.
    Never overwrites: if the folder already has a different video, the file is left where it is.
    """
    root = Path(cfg["staging_dir"])
    moved = []
    if not root.exists():
        return moved
    for f in sorted(root.iterdir()):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
            continue
        m = OUTPUT_NAME_RE.search(f.stem)
        if not m:
            if log:
                log(f"Can't tell which Output '{f.name}' is (no 'Output <number>' in the name) - left in jobs folder")
            continue
        n = int(m.group(1))
        try:
            size1 = f.stat().st_size
            time.sleep(0.4)
            if f.stat().st_size != size1 or size1 == 0:
                continue                                    # still copying
        except OSError:
            continue
        jd = job_dir(cfg, n)
        jd.mkdir(parents=True, exist_ok=True)
        existing = [p for p in jd.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        target = jd / f.name
        if existing and not (len(existing) == 1 and existing[0].name.lower() == f.name.lower()):
            if log:
                log(f"⚠️ {f.name}: Output_{n} already has {existing[0].name} - not moved (remove one of them)")
            continue
        try:
            os.replace(f, target)                           # same drive: instant move
        except OSError:
            continue                                        # locked (still copying) - next refresh
        info = _read_json(jd / "job.json", None)
        if info and info.get("local_path") and Path(info["local_path"]) != target:
            (jd / "job.json").unlink(missing_ok=True)       # stale pointer to an old file
        moved.append(f"{f.name} -> Output_{n}")
        if log:
            log(f"📁 Moved {f.name} into Output_{n}\\")
    return moved


def whisper_dir_for(video: Path) -> Path:
    """Same default as transcribe.py: <video name without extension>_whisper next to the video."""
    return video.with_name(video.stem + "_whisper")


def metadata_path(n: int) -> Path:
    return METADATA_DIR / f"metadata_Output_{n}.json"


def srt_base_name(n: int) -> str:
    return f"Output_{n}_english"


def latest_srt(wdir: Path, n: int) -> Path | None:
    base = srt_base_name(n)
    best, best_v = None, -1
    if not wdir or not wdir.exists():
        return None
    for p in wdir.glob(f"{base}_v*.srt"):
        m = re.fullmatch(re.escape(base) + r"_v(\d+)\.srt", p.name, re.I)
        if m and int(m.group(1)) > best_v:
            best, best_v = p, int(m.group(1))
    return best


def json_is_valid(path: Path, key: str = "segments") -> bool:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return isinstance(d, dict) and isinstance(d.get(key), list)
    except Exception:
        return False


AI_EDIT_RE = re.compile(r"^\d{3}\.json$")
REVIEW_EDIT_NAME = "zz_review.json"          # sorts after 001.json.. so human fixes always win


def blank_value(v) -> bool:
    """A translations.json value that builds as a blank subtitle (null / {"text": null, ...})."""
    if isinstance(v, dict):
        v = v.get("text")
    return not (isinstance(v, str) and v.strip())


# ----------------------------------------------------------------------------- talking vs text-only
VIDEO_TYPES = ("auto", "talking", "text_only")
TEXT_ONLY_MAX_SPEECH_S = 3.0      # less real talking than this ...
TEXT_ONLY_MAX_RATIO = 0.12        # ... or under 12% of the video = text-only filler


def video_type_info(cfg: dict, n: int) -> dict:
    """{"mode": auto|talking|text_only, "detected": talking|text_only|None, ...} from job folder."""
    info = _read_json(job_dir(cfg, n) / "video_type.json", None)
    info = info if isinstance(info, dict) else {}
    info.setdefault("mode", "auto")
    info.setdefault("detected", None)
    return info


def save_video_type(cfg: dict, n: int, **kw) -> dict:
    info = video_type_info(cfg, n)
    info.update(kw)
    write_json_atomic(job_dir(cfg, n) / "video_type.json", info)
    return info


def effective_type(info: dict) -> str | None:
    """talking | text_only | None (auto and not detected yet)."""
    return info["mode"] if info.get("mode") in ("talking", "text_only") else info.get("detected")


def classify_speech(m: dict) -> str:
    speech, ratio = m.get("speech_seconds", 0), m.get("speech_ratio", 0)
    return "text_only" if speech < TEXT_ONLY_MAX_SPEECH_S or ratio < TEXT_ONLY_MAX_RATIO else "talking"


def output_status(cfg: dict, state: dict, n: int) -> dict:
    """Everything the GUI needs to know about one Output, derived from disk + state.json."""
    st = {
        "n": n, "video": None, "wdir": None, "downloaded": False, "transcribed": False,
        "translated": False, "srt": None, "needs_check": 0, "metadata": None,
        "metadata_ok": False, "srt_stale": False, "text_only": False, "type_info": {},
        "ready": False,
        "upload": state.get("completed_outputs", {}).get(str(n)),
        "error": state.get("pipeline_errors", {}).get(str(n)),
    }
    video = find_local_video(cfg, n)
    if video:
        st["video"], st["downloaded"] = video, True
        st["type_info"] = video_type_info(cfg, n)
        st["text_only"] = effective_type(st["type_info"]) == "text_only"
        wdir = whisper_dir_for(video)
        st["wdir"] = wdir
        st["transcribed"] = json_is_valid(wdir / "phrases.json")
        edits = wdir / "edits"
        st["translated"] = (st["transcribed"] and (wdir / "translations.json").exists()
                            and edits.exists() and any(AI_EDIT_RE.match(p.name) for p in edits.iterdir()))
        srt = latest_srt(wdir, n)
        st["srt"] = srt
        tr = _read_json(wdir / "translations.json", None)
        if isinstance(tr, dict):
            st["needs_check"] = sum(1 for v in tr.values() if blank_value(v))
        if srt:
            rv = edits / REVIEW_EDIT_NAME
            st["srt_stale"] = rv.exists() and rv.stat().st_mtime > srt.stat().st_mtime
    meta = _read_json(metadata_path(n), None)
    if isinstance(meta, dict):
        st["metadata"] = meta
        t, d = meta.get("title") or "", meta.get("description") or ""
        st["metadata_ok"] = bool(t and d and len(t) < 100 and "#CoachVee" in t)
    # text-only filler needs no subtitles: video + title/description is enough
    st["ready"] = bool(st["video"] and st["metadata_ok"] and (st["srt"] or st["text_only"]))
    return st


def stage_label(st: dict) -> tuple[str, str]:
    """(badge text, color) for the furthest stage this Output has reached."""
    up = st.get("upload") or {}
    status = up.get("status")
    if status == "scheduled":
        return "📅 Scheduled", "#15803d"
    if status in ("uploaded", "uploaded_private"):
        return "⬆ Uploaded", "#0e7490"
    if status == "draft":
        return "⚠️ Draft in Studio - not scheduled", "#b45309"
    if status == "failed":
        return "❌ Upload failed", "#b91c1c"
    if st["ready"]:
        return "✅ Ready", "#16a34a"
    if st["text_only"]:
        return "📝 Text-only", "#a16207"
    if st["translated"]:
        return "Translated", "#0f766e"
    if st["transcribed"]:
        return "Transcribed", "#6d28d9"
    if st["downloaded"]:
        return "Downloaded", "#1d4ed8"
    return "Not downloaded", "#4b5563"


# ----------------------------------------------------------------------------- subprocess
def run_streamed(cmd: list[str], on_line=None, cwd=None, env=None, proc_holder=None) -> tuple[int, str]:
    """Run a hidden console process, stream its output line by line (\\r or \\n), return (code, text).

    on_line(text, is_progress): is_progress=True for carriage-return progress updates (tqdm).
    proc_holder: optional list; the Popen object is appended so the caller can terminate it.
    """
    e = dict(os.environ)
    e.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    if env:
        e.update(env)
    p = subprocess.Popen(cmd, cwd=cwd, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    if proc_holder is not None:
        proc_holder.append(p)
    out, buf = [], b""
    while True:
        chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(1)
        if not chunk:
            break
        buf += chunk
        while True:
            m = re.search(rb"\r\n|\r|\n", buf)            # Windows children end lines with \r\n
            if not m:
                break
            if m.group() == b"\r" and m.end() == len(buf):
                break                                     # may be half of a \r\n split across reads
            piece, sep, buf = buf[:m.start()], m.group(), buf[m.end():]
            text = piece.decode("utf-8", "replace").rstrip()
            if text:
                is_prog = sep == b"\r"                    # bare \r = tqdm progress bar update
                if not is_prog:
                    out.append(text)
                if on_line:
                    on_line(text, is_prog)
    if buf.strip():
        text = buf.decode("utf-8", "replace").rstrip()
        out.append(text)
        if on_line:
            on_line(text, False)
    code = p.wait()
    return code, "\n".join(out)


def open_path(path) -> None:
    try:
        os.startfile(str(path))
    except Exception:
        pass


# ----------------------------------------------------------------------------- notifications
def notify(title: str, message: str) -> None:
    """Windows toast via PowerShell (no extra packages). Falls back to a message box."""
    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;").replace("'", "&apos;"))
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{esc(title)}</text><text>{esc(message)}</text></binding></visual></toast>')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast)
"""
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                           capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0:
            return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x40000)  # info icon, topmost
    except Exception:
        pass


def keep_awake(on: bool) -> None:
    """Stop Windows from sleeping while uploads run (ES_CONTINUOUS | ES_SYSTEM_REQUIRED)."""
    try:
        import ctypes
        flags = 0x80000000 | 0x00000001 if on else 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


# ----------------------------------------------------------------------------- claude CLI
def _version_key(p: Path):
    parts = re.findall(r"\d+", p.parent.parent.name)
    return tuple(int(x) for x in parts) if parts else (0,)


def find_claude_cli(cfg: dict) -> str | None:
    """config path -> PATH -> the CLI bundled with the Claude desktop app (newest version)."""
    p = (cfg.get("claude_cli_path") or "").strip()
    if p and Path(p).exists():
        return p
    w = shutil.which("claude")
    if w:
        return w
    cands = []
    bases = {os.environ.get("APPDATA", ""), str(Path.home() / "AppData" / "Roaming")}
    for base in filter(None, bases):
        cands += [Path(x) for x in glob.glob(str(Path(base) / "Claude" / "claude-code" / "*" / "*" / "claude.exe"))]
    for extra in (Path.home() / ".local" / "bin" / "claude.exe",
                  Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"):
        if extra.exists():
            cands.append(extra)
    if not cands:
        return None
    return str(max(cands, key=lambda c: (_version_key(c), c.stat().st_mtime)))


def app_python(windowless: bool = True) -> str:
    """The automation app's own interpreter (not the Whisper venv)."""
    exe = Path(sys.executable)
    if windowless:
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    else:
        c = exe.with_name("python.exe")
        if c.exists():
            return str(c)
    return str(exe)
