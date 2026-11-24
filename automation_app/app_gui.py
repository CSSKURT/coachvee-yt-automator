"""
Coach Vee YouTube Shorts Automator - desktop GUI (CustomTkinter, dark mode).

  Step 1  Connect Drive & Run Local Whisper (No AI)    -> drive_service + whisper_runner
  Step 2  Generate Translations, Titles & Descriptions -> ai_processor (the ONLY AI step)
  Review  edit titles/descriptions, fix blank/misheard subtitle lines, Rebuild .SRT
  GO      spawns background_uploader.py as a detached process and you can close this window

Launch: Launch_CoachVee_Automation.bat (or the Desktop shortcut).
"""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import BooleanVar, StringVar, filedialog, messagebox

import customtkinter as ctk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (APP_DIR, GUI_LOCK, ICON_PATH, PHT, REVIEW_EDIT_NAME, UPLOADER_LOCK, UPLOADER_STOP,
                    HeldLock, app_python, batch_numbers, batch_schedule, blank_value, find_claude_cli, parse_skip,
                    organize_loose_videos,
                    find_local_video, is_locked, load_config, load_state, metadata_path, next_monday,
                    now_pht, open_path, output_status, read_json_file, save_config, setup_logging,
                    slot_label, stage_label, update_state, whisper_dir_for, write_json_atomic)
from metadata_rules import clean_description, description_problems, enforce_title, title_report

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

AI_LABELS = {"claude_cli": "Claude CLI (Batch -p)", "gemini": "Gemini Free API"}
AI_KEYS = {v: k for k, v in AI_LABELS.items()}
GREEN, GREEN_HOVER = "#16a34a", "#15803d"
ORANGE, RED, MUTED = "#d97706", "#dc2626", "#9ca3af"
ROW_BG, ROW_SEL = "#262a33", "#3b82f6"
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

applog = setup_logging("app")


# ============================================================================ small dialogs
class ConfirmDialog(ctk.CTkToplevel):
    """Modal dialog. With `checkbox`, the OK button only enables once it is ticked."""

    def __init__(self, master, title: str, text: str, ok_text: str, checkbox: str | None = None,
                 extra_buttons: list[str] | None = None):
        super().__init__(master)
        self.title(title)
        self.result = None
        self.resizable(False, False)
        self.transient(master)
        ctk.CTkLabel(self, text=text, justify="left", wraplength=560, anchor="w").pack(padx=22, pady=(20, 10), fill="x")
        self.var = BooleanVar(value=checkbox is None)
        if checkbox:
            ctk.CTkCheckBox(self, text=checkbox, variable=self.var, command=self._sync).pack(padx=22, pady=8, anchor="w")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=22, pady=(10, 20), fill="x")
        ctk.CTkButton(row, text="Cancel", fg_color="#4b5563", hover_color="#374151", width=110,
                      command=self.destroy).pack(side="right", padx=(8, 0))
        for b in reversed(extra_buttons or []):
            ctk.CTkButton(row, text=b, width=150, command=lambda b=b: self._done(b)).pack(side="right", padx=(8, 0))
        self.ok = ctk.CTkButton(row, text=ok_text, fg_color=GREEN, hover_color=GREEN_HOVER, width=200,
                                command=lambda: self._done(ok_text))
        self.ok.pack(side="right")
        self._sync()
        self.after(50, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _sync(self):
        self.ok.configure(state="normal" if self.var.get() else "disabled")

    def _done(self, value):
        self.result = value
        self.destroy()

    def show(self):
        self.wait_window()
        return self.result


class SettingsDialog(ctk.CTkToplevel):
    FIELDS = [
        ("gemini_api_key", "Gemini API key (free, Google AI Studio)", True),
        ("gemini_model", "Gemini model", False),
        ("claude_cli_path", "Claude CLI path (blank = auto-detect)", False),
        ("claude_model", "Claude model for claude -p (e.g. sonnet, haiku, opus)", False),
        ("staging_dir", "Download / job folder", False),
        ("client_secret_path", "Google OAuth client_secret.json", False),
        ("srt_max_chars", "Subtitle max characters per line (30 = vertical Shorts)", False),
        ("whisper_model", "Whisper model", False),
    ]

    def __init__(self, master, cfg: dict):
        super().__init__(master)
        self.title("Settings")
        self.cfg = cfg
        self.saved = False
        self.transient(master)
        self.grid_columnconfigure(1, weight=1)
        self.vars = {}
        for i, (key, label, secret) in enumerate(self.FIELDS):
            ctk.CTkLabel(self, text=label, anchor="w").grid(row=i, column=0, padx=(18, 8), pady=6, sticky="w")
            v = StringVar(value=str(cfg.get(key, "")))
            e = ctk.CTkEntry(self, textvariable=v, width=430, show="•" if secret else "")
            e.grid(row=i, column=1, padx=(0, 18), pady=6, sticky="ew")
            self.vars[key] = v
        found = find_claude_cli(cfg) or "not found"
        ctk.CTkLabel(self, text=f"Auto-detected claude CLI: {found}", text_color=MUTED, wraplength=640,
                     anchor="w", justify="left").grid(row=len(self.FIELDS), column=0, columnspan=2, padx=18, sticky="w")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=len(self.FIELDS) + 1, column=0, columnspan=2, pady=16, padx=18, sticky="e")
        ctk.CTkButton(row, text="Cancel", fg_color="#4b5563", width=100, command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(row, text="Save", fg_color=GREEN, hover_color=GREEN_HOVER, width=120, command=self._save).pack(side="right")
        self.after(50, lambda: (self.grab_set(), self.lift()))

    def _save(self):
        for key, v in self.vars.items():
            val = v.get().strip()
            if key == "srt_max_chars":
                try:
                    val = int(val)
                except ValueError:
                    messagebox.showerror("Settings", "Max characters must be a number", parent=self)
                    return
            self.cfg[key] = val
        save_config(self.cfg)
        self.saved = True
        self.destroy()


# ============================================================================ main window
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Coach Vee YouTube Shorts Automator")
        self.geometry("1440x920")
        self.minsize(1150, 720)
        if ICON_PATH.exists():
            self.after(300, lambda: self._set_icon())
        self.cfg = load_config()
        self.app_state = load_state()
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.runners: list = []
        self.selected: int | None = None
        self.rows: dict[int, dict] = {}
        self.status: dict[int, dict] = {}
        self.schedule: dict[int, datetime] = {}
        self.sub_entries: dict[str, tuple] = {}
        self._subs_for: int | None = None
        self.meta_dirty = False
        self._last_co = ""
        self.f_title = ctk.CTkFont(size=19, weight="bold")
        self.f_bold = ctk.CTkFont(size=14, weight="bold")
        self.f_small = ctk.CTkFont(size=12)
        self.f_mono = ctk.CTkFont(family="Consolas", size=12)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_top()
        self._build_main()
        self._build_log()
        self._build_bottom()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reload_batch()
        self.after(100, self._drain)
        self.after(1500, self._poll_uploader)
        self.log("Ready. Step 1 downloads + transcribes (no AI). Step 2 is the only AI step.")

    def _set_icon(self):
        try:
            self.iconbitmap(str(ICON_PATH))
        except Exception:
            pass

    def report_callback_exception(self, exc, val, tb):
        applog.error("".join(traceback.format_exception(exc, val, tb)))
        messagebox.showerror("Unexpected error", f"{val}\n\nDetails are in the logs folder.")

    # ------------------------------------------------------------------ layout
    def _build_top(self):
        top = ctk.CTkFrame(self)
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        r1 = ctk.CTkFrame(top, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(r1, text="Coach Vee Shorts Automator", font=self.f_title).pack(side="left", padx=(0, 24))
        ctk.CTkLabel(r1, text="Batch: Output").pack(side="left")
        self.start_var = StringVar(value=str(self.app_state.get("next_start_index", 218)))
        e = ctk.CTkEntry(r1, textvariable=self.start_var, width=70)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda _e: self.reload_batch())
        e.bind("<FocusOut>", lambda _e: self.reload_batch(only_if_changed=True))
        self.end_label = ctk.CTkLabel(r1, text="")
        self.end_label.pack(side="left", padx=(0, 14))
        ctk.CTkLabel(r1, text="Videos:").pack(side="left")
        self.count_var = StringVar(value=str(self.app_state.get("batch_size", 14)))
        c = ctk.CTkEntry(r1, textvariable=self.count_var, width=44)
        c.pack(side="left", padx=(6, 14))
        c.bind("<Return>", lambda _e: self.reload_batch())
        c.bind("<FocusOut>", lambda _e: self.reload_batch(only_if_changed=True))
        ctk.CTkLabel(r1, text="Skip:").pack(side="left")
        self.skip_var = StringVar(value=", ".join(map(str, self.app_state.get("skipped_outputs", []))))
        k = ctk.CTkEntry(r1, textvariable=self.skip_var, width=80, placeholder_text="e.g. 241")
        k.pack(side="left", padx=(6, 14))
        k.bind("<Return>", lambda _e: self.reload_batch())
        k.bind("<FocusOut>", lambda _e: self.reload_batch(only_if_changed=True))
        ctk.CTkLabel(r1, text="First day:").pack(side="left")
        self.week_var = StringVar(value=str(self.default_first_day()))
        w = ctk.CTkEntry(r1, textvariable=self.week_var, width=110)
        w.pack(side="left", padx=6)
        w.bind("<Return>", lambda _e: self.reload_batch())
        w.bind("<FocusOut>", lambda _e: self.reload_batch(only_if_changed=True))
        ctk.CTkButton(r1, text="⚙ Settings", width=100, fg_color="#374151", hover_color="#4b5563",
                      command=self.open_settings).pack(side="right")
        self.ai_var = StringVar(value=AI_LABELS.get(self.cfg.get("ai_provider", "gemini"), AI_LABELS["gemini"]))
        ctk.CTkOptionMenu(r1, values=list(AI_LABELS.values()), variable=self.ai_var, width=200,
                          command=self._on_ai_change).pack(side="right", padx=8)
        ctk.CTkLabel(r1, text="AI mode:").pack(side="right")

        r2 = ctk.CTkFrame(top, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=(4, 10))
        self.btn_step1 = ctk.CTkButton(r2, text="1. Connect Drive & Run Local Whisper (No AI)", height=38,
                                       font=self.f_bold, command=self.on_step1)
        self.btn_step1.pack(side="left")
        self.btn_step2 = ctk.CTkButton(r2, text="2. Generate Translations, Titles & Descriptions (AI)", height=38,
                                       font=self.f_bold, fg_color="#7c3aed", hover_color="#6d28d9", command=self.on_step2)
        self.btn_step2.pack(side="left", padx=10)
        self.btn_stop = ctk.CTkButton(r2, text="⏹ Stop", width=80, height=38, fg_color="#4b5563",
                                      hover_color="#374151", state="disabled", command=self.on_stop)
        self.btn_stop.pack(side="left")
        ctk.CTkButton(r2, text="↻ Refresh", width=90, height=38, fg_color="#374151", hover_color="#4b5563",
                      command=self.on_refresh).pack(side="left", padx=10)
        self.progress = ctk.CTkProgressBar(r2, width=220)
        self.progress.set(0)
        self.progress.pack(side="right")
        self.progress_label = ctk.CTkLabel(r2, text="Idle", text_color=MUTED, anchor="e", width=420)
        self.progress_label.pack(side="right", padx=10)

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)
        self.left = ctk.CTkScrollableFrame(main, width=370, label_text="This week's Shorts")
        self.left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        self.left.grid_columnconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(main, command=self._on_tab)
        self.tabs.grid(row=0, column=1, sticky="nsew")
        t1 = self.tabs.add("Title & Description")
        t2 = self.tabs.add("Subtitles Quick-Fix")

        # ---- tab 1
        t1.grid_columnconfigure(0, weight=1)
        t1.grid_rowconfigure(6, weight=1)
        self.hdr = ctk.CTkLabel(t1, text="Select a video on the left", font=self.f_title, anchor="w")
        self.hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        tools = ctk.CTkFrame(t1, fg_color="transparent")
        tools.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        for text, cmd in (("▶ Play video", self.on_play), ("📂 Open folder", self.on_open_folder),
                          ("↻ Regenerate with AI", self.on_regenerate), ("🎙 Re-run Whisper…", self.on_rewhisper)):
            ctk.CTkButton(tools, text=text, width=150, fg_color="#374151", hover_color="#4b5563",
                          command=cmd).pack(side="left", padx=4)
        ctk.CTkButton(tools, text="⏭ Skip video", width=110, fg_color="#7c2d12", hover_color="#9a3412",
                      command=self.on_skip_video).pack(side="left", padx=(16, 4))
        ctk.CTkButton(tools, text="✕ Remove from batch", width=150, fg_color="#7f1d1d", hover_color="#991b1b",
                      command=self.on_remove_video).pack(side="left", padx=4)
        trow0 = ctk.CTkFrame(t1, fg_color="transparent")
        trow0.grid(row=8, column=0, sticky="ew", padx=10, pady=(0, 8))
        ctk.CTkLabel(trow0, text="Video type:", font=self.f_bold).pack(side="left")
        self.type_seg = ctk.CTkSegmentedButton(trow0, values=["Auto", "Talking", "Text-only"],
                                               command=self.on_type_change)
        self.type_seg.pack(side="left", padx=8)
        self.type_info = ctk.CTkLabel(trow0, text="", text_color=MUTED, anchor="w")
        self.type_info.pack(side="left", padx=6)
        ctk.CTkLabel(t1, text="Title", font=self.f_bold, anchor="w").grid(row=2, column=0, sticky="ew", padx=10)
        trow = ctk.CTkFrame(t1, fg_color="transparent")
        trow.grid(row=3, column=0, sticky="ew", padx=10)
        trow.grid_columnconfigure(0, weight=1)
        self.title_var = StringVar()
        self.title_entry = ctk.CTkEntry(trow, textvariable=self.title_var, height=36, font=ctk.CTkFont(size=14))
        self.title_entry.grid(row=0, column=0, sticky="ew")
        self.counter = ctk.CTkLabel(trow, text="", corner_radius=6, fg_color="#374151", width=290)
        self.counter.grid(row=0, column=1, padx=(8, 0))
        self.title_var.trace_add("write", lambda *_: self._on_title_change())
        drow = ctk.CTkFrame(t1, fg_color="transparent")
        drow.grid(row=4, column=0, sticky="ew", padx=10, pady=(10, 0))
        ctk.CTkLabel(drow, text="Description", font=self.f_bold).pack(side="left")
        self.desc_warn = ctk.CTkLabel(drow, text="", text_color=ORANGE, anchor="w")
        self.desc_warn.pack(side="left", padx=12)
        self.meta_info = ctk.CTkLabel(t1, text="", text_color=MUTED, anchor="w")
        self.meta_info.grid(row=5, column=0, sticky="ew", padx=10)
        self.desc = ctk.CTkTextbox(t1, wrap="word", font=ctk.CTkFont(size=13))
        self.desc.grid(row=6, column=0, sticky="nsew", padx=10, pady=(2, 6))
        self.desc.bind("<<Modified>>", self._on_desc_modified)
        srow = ctk.CTkFrame(t1, fg_color="transparent")
        srow.grid(row=7, column=0, sticky="ew", padx=10, pady=(0, 8))
        ctk.CTkButton(srow, text="💾 Save title & description", fg_color=GREEN, hover_color=GREEN_HOVER,
                      command=lambda: self.save_metadata(show=True)).pack(side="left")
        ctk.CTkButton(srow, text="✂ Apply title rules", fg_color="#374151", hover_color="#4b5563",
                      command=self.on_fix_title).pack(side="left", padx=8)
        self.saved_label = ctk.CTkLabel(srow, text="", text_color=MUTED)
        self.saved_label.pack(side="left", padx=8)

        # ---- tab 2
        t2.grid_columnconfigure(0, weight=1)
        t2.grid_rowconfigure(2, weight=1)
        srow2 = ctk.CTkFrame(t2, fg_color="transparent")
        srow2.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        ctk.CTkButton(srow2, text="🔨 Rebuild .SRT", fg_color=GREEN, hover_color=GREEN_HOVER,
                      command=self.on_rebuild).pack(side="left", padx=4)
        ctk.CTkButton(srow2, text="Open .SRT", width=100, fg_color="#374151", hover_color="#4b5563",
                      command=self.on_open_srt).pack(side="left", padx=4)
        ctk.CTkButton(srow2, text="tagalog_check.txt", width=130, fg_color="#374151", hover_color="#4b5563",
                      command=lambda: self._open_in_wdir("tagalog_check.txt")).pack(side="left", padx=4)
        self.show_all = BooleanVar(value=False)
        ctk.CTkCheckBox(srow2, text="Show all lines (not only problems + Tagalog)", variable=self.show_all,
                        command=self.render_subs).pack(side="left", padx=14)
        self.sub_info = ctk.CTkLabel(t2, text="", anchor="w", text_color=MUTED)
        self.sub_info.grid(row=1, column=0, sticky="ew", padx=12)
        self.sub_scroll = ctk.CTkScrollableFrame(t2)
        self.sub_scroll.grid(row=2, column=0, sticky="nsew", padx=6, pady=6)
        self.sub_scroll.grid_columnconfigure(0, weight=1)

    def _build_log(self):
        self.logbox = ctk.CTkTextbox(self, height=120, font=self.f_mono, wrap="word")
        self.logbox.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        self.logbox.configure(state="disabled")

    def _build_bottom(self):
        bot = ctk.CTkFrame(self)
        bot.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        bot.grid_columnconfigure(1, weight=1)
        left = ctk.CTkFrame(bot, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=10, pady=10)
        self.confirm_var = BooleanVar(value=False)
        ctk.CTkCheckBox(left, text=f"I confirm upload to YouTube Studio account: {self.app_state['youtube_email']}",
                        variable=self.confirm_var, command=self._sync_go).pack(anchor="w")
        prow = ctk.CTkFrame(left, fg_color="transparent")
        prow.pack(anchor="w", pady=(8, 0))
        self.pw_var = BooleanVar(value=self.cfg.get("upload_method") == "playwright")
        ctk.CTkSwitch(prow, text="Use YouTube Studio Browser Automation (Playwright) instead of API",
                      variable=self.pw_var, command=self._on_pw_toggle).pack(side="left")
        ctk.CTkButton(prow, text="🔑 Studio Login Setup", width=150, fg_color="#374151", hover_color="#4b5563",
                      command=self.on_studio_login).pack(side="left", padx=10)
        mid = ctk.CTkFrame(bot, fg_color="transparent")
        mid.grid(row=0, column=1, sticky="ew", padx=10)
        self.up_label = ctk.CTkLabel(mid, text="Uploader: idle", anchor="w", justify="left", wraplength=420)
        self.up_label.pack(anchor="w", fill="x")
        urow = ctk.CTkFrame(mid, fg_color="transparent")
        urow.pack(anchor="w", pady=(4, 0))
        self.btn_stop_up = ctk.CTkButton(urow, text="Stop uploader", width=120, fg_color="#4b5563",
                                         hover_color="#374151", state="disabled", command=self.on_stop_uploader)
        self.btn_stop_up.pack(side="left")
        ctk.CTkButton(urow, text="Open logs", width=100, fg_color="#374151", hover_color="#4b5563",
                      command=lambda: open_path(APP_DIR / "logs")).pack(side="left", padx=8)
        self.go_btn = ctk.CTkButton(bot, text="GO — UPLOAD & SCHEDULE ALL 14 IN BACKGROUND", height=54, width=430,
                                    font=ctk.CTkFont(size=15, weight="bold"), fg_color=GREEN,
                                    hover_color=GREEN_HOVER, command=self.on_go)
        self.go_btn.grid(row=0, column=2, padx=12, pady=10)
        self._sync_go()

    # ------------------------------------------------------------------ thread-safe plumbing
    def log(self, msg: str):
        applog.info(msg)
        self.q.put(("log", msg))

    def set_progress(self, text: str, frac: float | None = None):
        self.q.put(("progress", text, frac))

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", f"{datetime.now():%H:%M:%S}  {item[1]}\n")
                    lines = int(self.logbox.index("end-1c").split(".")[0])
                    if lines > 3000:
                        self.logbox.delete("1.0", f"{lines - 2500}.0")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "progress":
                    self.progress_label.configure(text=item[1][:90])
                    if item[2] is not None:
                        self.progress.set(max(0.0, min(1.0, item[2])))
                elif kind == "refresh":
                    self.refresh(item[1], force_inspector=len(item) > 2 and item[2])
                elif kind == "busy":
                    busy = item[1]
                    for b in (self.btn_step1, self.btn_step2):
                        b.configure(state="disabled" if busy else "normal")
                    self.btn_stop.configure(state="normal" if busy else "disabled")
                    if not busy:
                        self.progress_label.configure(text=f"{item[2]} finished")
                        self.progress.set(0)
                    self._sync_go()
                elif kind == "error":
                    messagebox.showerror(item[1], item[2])
                elif kind == "info":
                    messagebox.showinfo(item[1], item[2])
                elif kind == "call":
                    item[1]()
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def run_job(self, name: str, fn, *args):
        if self.busy():
            messagebox.showinfo("Busy", "Another step is still running. Wait for it or click Stop.")
            return
        self.stop_event = threading.Event()

        def wrap():
            self.q.put(("busy", True, name))
            try:
                fn(*args)
            except Exception as e:
                applog.error(traceback.format_exc())
                self.log(f"❌ {name} failed: {e}")
                self.q.put(("error", f"{name} failed", str(e)))
            finally:
                self.runners.clear()
                self.q.put(("busy", False, name))
                self.q.put(("refresh", None))
        self.worker = threading.Thread(target=wrap, daemon=True)
        self.worker.start()

    def _set_error(self, n: int, msg: str | None):
        def f(s):
            errs = s.setdefault("pipeline_errors", {})
            if msg:
                errs[str(n)] = msg[:500]
            else:
                errs.pop(str(n), None)
        update_state(f)

    # ------------------------------------------------------------------ batch + list
    def batch(self) -> list[int]:
        try:
            start = int(self.start_var.get().strip())
        except ValueError:
            start = int(self.app_state.get("next_start_index", 218))
            self.start_var.set(str(start))
        try:
            count = max(1, min(60, int(self.count_var.get().strip())))
        except (ValueError, AttributeError):
            count = int(self.app_state.get("batch_size", 14))
            if hasattr(self, "count_var"):
                self.count_var.set(str(count))
        skip = parse_skip(self.skip_var.get()) if hasattr(self, "skip_var") else []
        return batch_numbers(start, count, skip)

    def default_first_day(self) -> date:
        """The day after the last scheduled slot (so batches never overlap), else the next Monday."""
        last = None
        for rec in (self.app_state.get("completed_outputs") or {}).values():
            if rec.get("status") in ("scheduled", "uploaded_private") and rec.get("publish_at"):
                d = datetime.fromisoformat(rec["publish_at"]).date()
                last = d if last is None or d > last else last
        tomorrow = now_pht().date() + timedelta(days=1)
        if last and last + timedelta(days=1) > tomorrow:
            return last + timedelta(days=1)
        return next_monday()

    def taken_slots(self, nums: list[int]) -> dict:
        """publish time -> Output number, for videos already scheduled outside this batch."""
        out = {}
        for k, rec in (self.app_state.get("completed_outputs") or {}).items():
            if int(k) not in nums and rec.get("status") in ("scheduled", "uploaded_private") and rec.get("publish_at"):
                out[datetime.fromisoformat(rec["publish_at"])] = int(k)
        return out

    def slot_conflicts(self) -> list[str]:
        nums = self.batch()
        taken = self.taken_slots(nums)
        return [f"Output {n} → {slot_label(t, long=True)} is already used by Output {taken[t]}"
                for n, t in self.schedule.items() if t in taken]

    def week_start(self) -> date:
        try:
            return date.fromisoformat(self.week_var.get().strip())
        except ValueError:
            d = next_monday()
            self.week_var.set(str(d))
            return d

    def organize(self):
        try:
            return organize_loose_videos(self.cfg, self.log)
        except Exception as e:
            self.log(f"Could not organize the jobs folder: {e}")
            return []

    def on_refresh(self):
        self.organize()
        self.refresh(None, force_inspector=True)

    def reload_batch(self, only_if_changed: bool = False):
        if not only_if_changed:
            self.organize()
        nums = self.batch()
        key = (tuple(nums), self.week_var.get())
        if only_if_changed and getattr(self, "_batch_key", None) == key:
            return
        self.app_state = load_state()
        cb = self.app_state.get("current_batch") or {}
        if not getattr(self, "_batch_key", None) and cb.get("start") == nums[0] and cb.get("week_start"):
            self.week_var.set(cb["week_start"])           # reopening mid-week: show the scheduled week
        wk = self.week_start()
        last_day = wk + timedelta(days=(len(nums) - 1) // max(1, len(self.app_state["daily_slots_pht"])))
        self.log(f"Batch: Output {nums[0]}–{nums[-1]} ({len(nums)} videos), {wk:%a %b} {wk.day} → {last_day:%a %b} {last_day.day}")
        self._batch_key = (tuple(nums), self.week_var.get())
        skip = parse_skip(self.skip_var.get())
        if skip != self.app_state.get("skipped_outputs", []):
            update_state(lambda st: st.__setitem__("skipped_outputs", skip))
            self.app_state["skipped_outputs"] = skip
        skipped_here = [x for x in skip if nums[0] <= x <= nums[-1]]
        if skipped_here:
            self.log(f"Skipping Output {', '.join(map(str, skipped_here))} (not in this batch)")
        self.schedule = batch_schedule(nums, wk, self.app_state["daily_slots_pht"])
        taken = self.taken_slots(nums)
        first_clash = next((i for i, t in enumerate(self.schedule.values()) if t in taken), None)
        if first_clash:                                 # auto-fit: end the batch before the taken slots
            self.log(f"Videos reduced from {len(nums)} to {first_clash} so the batch ends before slots that are "
                     f"already scheduled ({slot_label(list(self.schedule.values())[first_clash], long=True)}).")
            self.count_var.set(str(first_clash))
            nums = self.batch()
            self._batch_key = (tuple(nums), self.week_var.get())
            self.schedule = batch_schedule(nums, wk, self.app_state["daily_slots_pht"])
        self.end_label.configure(text=f"to Output {nums[-1]}")
        for msg in self.slot_conflicts():
            self.log(f"⚠️ Slot clash: {msg}. Change 'First day'.")
        self.go_btn.configure(text=f"GO — UPLOAD & SCHEDULE ALL {len(nums)} IN BACKGROUND")
        for child in self.left.winfo_children():
            child.destroy()
        self.rows.clear()
        for i, n in enumerate(nums):
            self._make_row(i, n)
        self.refresh(None)
        if self.selected not in nums:
            self.selected = None
            self.select(nums[0])

    def _make_row(self, i: int, n: int):
        f = ctk.CTkFrame(self.left, corner_radius=8, fg_color=ROW_BG, border_width=2, border_color=ROW_BG)
        f.grid(row=i, column=0, sticky="ew", pady=3, padx=2)
        f.grid_columnconfigure(0, weight=1)
        name = ctk.CTkLabel(f, text=f"Output {n}", font=self.f_bold, anchor="w")
        name.grid(row=0, column=0, sticky="w", padx=10, pady=(6, 0))
        slot = ctk.CTkLabel(f, text="", text_color=MUTED, anchor="e", font=self.f_small)
        slot.grid(row=0, column=1, sticky="e", padx=10, pady=(6, 0))
        badges = ctk.CTkFrame(f, fg_color="transparent")
        badges.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 7))
        for w in (f, name, slot, badges):
            w.bind("<Button-1>", lambda _e, n=n: self.select(n))
        self.rows[n] = {"frame": f, "slot": slot, "badges": badges}

    def _badge(self, parent, text, color, n):
        b = ctk.CTkLabel(parent, text=f" {text} ", fg_color=color, corner_radius=6, font=self.f_small, height=22)
        b.pack(side="left", padx=2)
        b.bind("<Button-1>", lambda _e, n=n: self.select(n))

    def refresh(self, n: int | None = None, force_inspector: bool = False):
        self.app_state = load_state()
        targets = [n] if n is not None and n in self.rows else list(self.rows)
        for m in targets:
            st = output_status(self.cfg, self.app_state, m)
            self.status[m] = st
            row = self.rows[m]
            up = st.get("upload") or {}
            when = datetime.fromisoformat(up["publish_at"]) if up.get("publish_at") else self.schedule.get(m)
            row["slot"].configure(text=slot_label(when) if when else "")
            for c in row["badges"].winfo_children():
                c.destroy()
            text, color = stage_label(st)
            self._badge(row["badges"], text, color, m)
            if st["needs_check"] and not up.get("status"):
                self._badge(row["badges"], f"⚠️ Needs Subtitle Check ({st['needs_check']})", ORANGE, m)
            if st["srt_stale"] and not up.get("status"):
                self._badge(row["badges"], "rebuild .SRT", "#a16207", m)
            if st["text_only"] and text != "📝 Text-only":
                self._badge(row["badges"], "📝 Text-only", "#a16207", m)
            if st["error"]:
                self._badge(row["badges"], "❌ error", RED, m)
            if up.get("status") == "scheduled" and up.get("captions_done") is False:
                self._badge(row["badges"], "no captions", ORANGE, m)
        if self.selected in self.rows and (n is None or n == self.selected):
            if force_inspector or not self.meta_dirty:
                self.load_inspector(reload_subs=force_inspector or n == self.selected, force_subs=force_inspector)
        self._sync_go()

    def save_pending_sub_changes(self) -> int:
        """Store typed-but-not-rebuilt subtitle fixes in edits/zz_review.json (GO rebuilds stale SRTs)."""
        changes = self.collect_sub_changes()
        st = self.status.get(self._subs_for) or {}
        if not changes or not st.get("wdir"):
            return 0
        rv_path = st["wdir"] / "edits" / REVIEW_EDIT_NAME
        rv = read_json_file(rv_path, {}) or {}
        rv.update(changes)
        write_json_atomic(rv_path, rv)
        for k, (e, _orig) in self.sub_entries.items():
            if k in changes:
                self.sub_entries[k] = (e, changes[k] or "")
        self.log(f"Output {self._subs_for}: saved {len(changes)} subtitle fix(es) (click Rebuild .SRT to apply)")
        return len(changes)

    def select(self, n: int):
        if n == self.selected:
            return
        if self.selected is not None and self.meta_dirty:
            self.save_metadata(show=False)
        if self.sub_entries:
            self.save_pending_sub_changes()
        if self.selected in self.rows:
            self.rows[self.selected]["frame"].configure(border_color=ROW_BG)
        self.selected = n
        self.rows[n]["frame"].configure(border_color=ROW_SEL)
        self.load_inspector(reload_subs=True)

    # ------------------------------------------------------------------ inspector: metadata
    def load_inspector(self, reload_subs: bool = True, force_subs: bool = True):
        n = self.selected
        if n is None:
            return
        st = self.status.get(n) or output_status(self.cfg, self.app_state, n)
        when = self.schedule.get(n)
        up = st.get("upload") or {}
        extra = f"  ·  {up['status']}: {up.get('url') or ''}" if up.get("status") else ""
        self.hdr.configure(text=f"Output {n}  —  {slot_label(when, long=True) if when else ''}{extra}")
        meta = st.get("metadata") or {}
        self._loading = True
        self.title_var.set(meta.get("title", ""))
        self.desc.delete("1.0", "end")
        self.desc.insert("1.0", meta.get("description", ""))
        self.desc.edit_modified(False)
        self._loading = False
        self.meta_dirty = False
        self._on_title_change(mark_dirty=False)
        self._update_desc_warn()
        if meta:
            who = "edited by you" if meta.get("edited_by_user") else f"generated by {meta.get('provider', 'AI')}"
            self.meta_info.configure(text=f"{who} · {meta.get('edited_at') or meta.get('generated_at', '')}")
        else:
            self.meta_info.configure(text="No title/description yet - run Step 2, or type them here and Save.")
        self.saved_label.configure(text="")
        ti = st.get("type_info") or {}
        self.type_seg.set({"talking": "Talking", "text_only": "Text-only"}.get(ti.get("mode"), "Auto"))
        if not st.get("video"):
            self.type_info.configure(text="(not downloaded yet)")
        elif ti.get("detected"):
            det = "text-only" if ti["detected"] == "text_only" else "talking"
            self.type_info.configure(text=f"auto-detected: {det} ({ti.get('speech_seconds', 0):.1f}s of speech in "
                                          f"{ti.get('duration', 0):.0f}s)" + ("" if ti.get("mode") == "auto" else
                                          "  ·  your choice overrides it"))
        else:
            self.type_info.configure(text="detected during Step 1")
        if reload_subs:
            self.render_subs(force=force_subs)

    def on_type_change(self, label: str):
        from common import save_video_type
        n = self.selected
        st = self.status.get(n) or {}
        if not st.get("video"):
            messagebox.showinfo("Video type", "Download the video first (Step 1).")
            self.load_inspector(reload_subs=False)
            return
        mode = {"Auto": "auto", "Talking": "talking", "Text-only": "text_only"}[label]
        save_video_type(self.cfg, n, mode=mode)
        self.log(f"Output {n}: video type set to {label}")
        self.refresh(n, force_inspector=True)
        st = self.status.get(n) or {}
        if not st["text_only"] and not st["transcribed"]:
            messagebox.showinfo("Video type", f"Output {n} is now a talking video. Run Step 1 again to transcribe "
                                              f"it, then Step 2.")
        elif st["text_only"] and not (st.get("metadata") or {}).get("onscreen_text"):
            messagebox.showinfo("Video type", f"Output {n} is now text-only (no subtitles). Click 'Regenerate with "
                                              f"AI' (or Step 2) to write the title from its on-screen text.")

    def _on_title_change(self, mark_dirty: bool = True):
        text, ok = title_report(self.title_var.get())
        self.counter.configure(text=text, fg_color="#14532d" if ok else "#7f1d1d")
        if mark_dirty and not getattr(self, "_loading", False):
            self.meta_dirty = True
            self.saved_label.configure(text="unsaved changes")

    def _on_desc_modified(self, _e=None):
        if self.desc.edit_modified():
            if not getattr(self, "_loading", False):
                self.meta_dirty = True
                self.saved_label.configure(text="unsaved changes")
            self.desc.edit_modified(False)
            self._update_desc_warn()

    def _update_desc_warn(self):
        d = self.desc.get("1.0", "end-1c")
        probs = description_problems(d) if d.strip() else []
        self.desc_warn.configure(text=("⚠️ " + "; ".join(probs)) if probs else ("✅ structure OK" if d.strip() else ""),
                                 text_color=ORANGE if probs else "#4ade80")

    def on_fix_title(self):
        self.title_var.set(enforce_title(self.title_var.get()))

    def save_metadata(self, show: bool = False, n: int | None = None):
        n = n if n is not None else self.selected
        if n is None:
            return
        title = self.title_var.get().strip()
        desc = clean_description(self.desc.get("1.0", "end-1c"))
        if not title and not desc:
            self.meta_dirty = False
            return
        fixed = enforce_title(title)
        if fixed != title:
            self.log(f"Output {n}: title adjusted to the rules ({len(fixed)} chars)")
            self._loading = True
            self.title_var.set(fixed)
            self._loading = False
        mp = metadata_path(n)
        meta = read_json_file(mp, None) or {"output": n}
        meta.update(title=fixed, description=desc, edited_by_user=True,
                    edited_at=datetime.now().isoformat(timespec="seconds"))
        write_json_atomic(mp, meta)
        self.meta_dirty = False
        self.saved_label.configure(text=f"saved {datetime.now():%H:%M:%S}")
        self._on_title_change(mark_dirty=False)
        self.refresh(n)
        if show:
            self.log(f"Output {n}: title & description saved")

    # ------------------------------------------------------------------ inspector: subtitles
    def render_subs(self, force: bool = True):
        n = self.selected
        if not force and self._subs_for == n and self.collect_sub_changes():
            return                                   # don't wipe fixes the user is typing
        for c in self.sub_scroll.winfo_children():
            c.destroy()
        self.sub_entries.clear()
        self._subs_for = n
        st = self.status.get(n) or {}
        wdir = st.get("wdir")
        if st.get("text_only"):
            self.sub_info.configure(text="📝 Text-only video - no subtitles are needed or uploaded.")
            txt = (st.get("metadata") or {}).get("onscreen_text")
            if txt:
                ctk.CTkLabel(self.sub_scroll, text="On-screen text read by the AI:", font=self.f_bold,
                             anchor="w").pack(fill="x", padx=6, pady=(10, 2))
                ctk.CTkLabel(self.sub_scroll, text=txt, anchor="w", justify="left",
                             wraplength=900).pack(fill="x", padx=10)
            return
        if not st.get("transcribed"):
            self.sub_info.configure(text="No transcript yet - run Step 1.")
            return
        phrases = (read_json_file(wdir / "phrases.json", {}) or {}).get("segments", [])
        tr = read_json_file(wdir / "translations.json", None)
        reviewed = read_json_file(wdir / "edits" / REVIEW_EDIT_NAME, {}) or {}
        rows = []
        for p in phrases:
            k = str(p["id"])
            v = tr.get(k, p["text"]) if isinstance(tr, dict) else p["text"]
            guess = note = None
            if isinstance(v, dict):
                guess, note, v = v.get("guess"), v.get("note"), v.get("text")
            blank = blank_value(v)
            problem = blank or bool(guess) or bool(note) or bool(p.get("flagged"))
            rows.append(dict(id=k, start=p["start"], heard=p["text"], text=v or "", guess=guess, note=note,
                             blank=blank, problem=problem, tl=bool(p.get("tagalog")), reviewed=k in reviewed))
        probs = [r for r in rows if r["problem"]]
        tls = [r for r in rows if r["tl"] and not r["problem"]]
        rest = [r for r in rows if not r["problem"] and not r["tl"]]
        show = probs + tls + (rest if self.show_all.get() else [])
        srt = st.get("srt")
        info = (f"{srt.name if srt else 'no .SRT built yet'}  ·  {sum(r['blank'] for r in rows)} blank  ·  "
                f"{len(probs)} to check  ·  {len(tls)} Tagalog lines  ·  {len(rows)} phrases")
        if not isinstance(tr, dict):
            info += "  ·  not translated yet (showing Whisper text)"
        if st.get("srt_stale"):
            info += "  ·  ⚠️ edits newer than the .SRT - click Rebuild"
        self.sub_info.configure(text=info)
        if probs:
            self._sub_header(f"⚠️  Needs a look ({len(probs)}) - blank, guessed or low-confidence lines")
        for i, r in enumerate(show):
            if r is (tls[0] if tls else None):
                self._sub_header(f"Tagalog lines ({len(tls)}) - spot-check against the audio")
            if rest and r is rest[0] and self.show_all.get():
                self._sub_header(f"Other lines ({len(rest)})")
            self._sub_row(r)

    def _sub_header(self, text):
        ctk.CTkLabel(self.sub_scroll, text=text, font=self.f_bold, anchor="w").pack(fill="x", padx=6, pady=(10, 2))

    def _sub_row(self, r: dict):
        border = ORANGE if r["problem"] else "#333842"
        fr = ctk.CTkFrame(self.sub_scroll, fg_color=ROW_BG, border_width=1, border_color=border)
        fr.pack(fill="x", padx=4, pady=2)
        fr.grid_columnconfigure(1, weight=1)
        m, s = divmod(r["start"], 60)
        tag = " ✓" if r["reviewed"] else ""
        ctk.CTkLabel(fr, text=f"#{r['id']}  {int(m):02}:{s:04.1f}{tag}", font=self.f_mono, width=110,
                     anchor="w").grid(row=0, column=0, rowspan=2, sticky="nw", padx=8, pady=6)
        ctk.CTkLabel(fr, text=f"Heard: {r['heard']}", text_color=MUTED, anchor="w", justify="left",
                     wraplength=620).grid(row=0, column=1, sticky="ew", padx=4, pady=(6, 0))
        ph = f"guess: {r['guess']}" if r["guess"] else "(blank - type the English subtitle)"
        e = ctk.CTkEntry(fr, placeholder_text=ph)
        if r["text"]:
            e.insert(0, r["text"])
        e.grid(row=1, column=1, sticky="ew", padx=4, pady=(2, 6))
        if r["guess"] or r["note"]:
            hint = "💡 " + " — ".join(x for x in (r["guess"], r["note"]) if x)
            ctk.CTkLabel(fr, text=hint, text_color=ORANGE, anchor="w", justify="left", wraplength=620,
                         font=self.f_small).grid(row=2, column=1, sticky="ew", padx=4, pady=(0, 6))
        if r["guess"]:
            ctk.CTkButton(fr, text="Use guess", width=90, fg_color="#374151", hover_color="#4b5563",
                          command=lambda e=e, g=r["guess"]: (e.delete(0, "end"), e.insert(0, g))
                          ).grid(row=1, column=2, padx=(4, 8))
        self.sub_entries[r["id"]] = (e, r["text"] or "")

    def collect_sub_changes(self) -> dict:
        changes = {}
        for k, (e, orig) in self.sub_entries.items():
            new = " ".join(e.get().split())
            if new != " ".join(orig.split()):
                changes[k] = new or None
        return changes

    def on_rebuild(self):
        n = self.selected
        st = self.status.get(n) or {}
        if not st.get("transcribed"):
            messagebox.showinfo("Rebuild .SRT", "Run Step 1 first.")
            return
        changes = self.collect_sub_changes()
        if not changes and not messagebox.askyesno("Rebuild .SRT", "No subtitle edits found. Rebuild anyway?"):
            return
        wdir: Path = st["wdir"]
        if changes:
            rv_path = wdir / "edits" / REVIEW_EDIT_NAME
            rv = read_json_file(rv_path, {}) or {}
            rv.update(changes)
            write_json_atomic(rv_path, rv)
            self.log(f"Output {n}: saved {len(changes)} subtitle fix(es) to edits/{REVIEW_EDIT_NAME}")
        self.run_job("Rebuild .SRT", self._rebuild_worker, n, wdir)

    def _rebuild_worker(self, n, wdir):
        from whisper_runner import WhisperRunner
        r = WhisperRunner(self.cfg, log=self.log, on_progress=lambda t: self.set_progress(t))
        self.runners.append(r)
        self.set_progress(f"Rebuilding Output {n} .SRT ...")
        res = r.merge_and_build(n, wdir)
        self.log(f"Output {n}: {res['summary'] or res['srt']}")
        self.q.put(("refresh", n, True))

    # ------------------------------------------------------------------ inspector: tools
    def _st(self):
        return self.status.get(self.selected) or {}

    def on_play(self):
        v = self._st().get("video")
        open_path(v) if v else messagebox.showinfo("Play", "Not downloaded yet.")

    def on_open_folder(self):
        st = self._st()
        target = st.get("wdir") if st.get("wdir") and st["wdir"].exists() else (st["video"].parent if st.get("video") else None)
        open_path(target) if target else messagebox.showinfo("Folder", "Not downloaded yet.")

    def on_open_srt(self):
        s = self._st().get("srt")
        open_path(s) if s else messagebox.showinfo("SRT", "No .SRT built yet.")

    def _open_in_wdir(self, name):
        w = self._st().get("wdir")
        if w and (w / name).exists():
            open_path(w / name)
        else:
            messagebox.showinfo(name, f"{name} does not exist yet.")

    def _on_tab(self):
        pass

    # ------------------------------------------------------------------ settings / toggles
    def open_settings(self):
        d = SettingsDialog(self, self.cfg)
        self.wait_window(d)
        if d.saved:
            self.cfg = load_config()
            self.log("Settings saved.")
            self.refresh(None)

    def _on_ai_change(self, label):
        self.cfg["ai_provider"] = AI_KEYS.get(label, "gemini")
        save_config(self.cfg)
        self.log(f"AI mode: {label}")

    def _on_pw_toggle(self):
        self.cfg["upload_method"] = "playwright" if self.pw_var.get() else "api"
        save_config(self.cfg)
        self.log("Upload method: " + ("YouTube Studio browser automation (Playwright)" if self.pw_var.get()
                                      else "YouTube Data API v3"))

    def _sync_go(self):
        running = is_locked(UPLOADER_LOCK)
        ok = self.confirm_var.get() and not self.busy() and not running
        self.go_btn.configure(state="normal" if ok else "disabled")

    # ------------------------------------------------------------------ STEP 1
    def on_step1(self):
        nums = self.batch()
        s = self.app_state
        dlg = ConfirmDialog(
            self, "Connect Google Drive",
            f"Step 1 downloads Output {nums[0]}–{nums[-1]} from Google Drive and transcribes them one at a "
            f"time on the Intel Arc GPU. No AI is used.\n\nThe Drive account is different from your "
            f"Claude/YouTube account. A browser window opens for sign-in the first time (or when the saved login expired).",
            "Confirm & Connect Drive",
            checkbox=f"Confirm Google Drive login for '{s['drive_email']}' (Target Folder: '{s['drive_folder_name']}')",
            extra_buttons=["Skip Drive (local files only)"])
        res = dlg.show()
        if not res:
            return
        self.run_job("Step 1", self._step1_worker, nums, res == "Confirm & Connect Drive")

    def _step1_worker(self, nums: list[int], use_drive: bool):
        from whisper_runner import WhisperRunner
        cfg, state = self.cfg, load_state()
        organize_loose_videos(cfg, self.log)
        found, drive = {}, None
        if use_drive:
            from drive_service import DriveService
            from google_auth import get_credentials
            self.set_progress("Connecting to Google Drive ...", 0)
            creds = get_credentials("drive", cfg, state["drive_email"], interactive=True, log=self.log)
            drive = DriveService(creds, self.log)
            email = drive.account_email()
            if email.lower() != state["drive_email"].lower():
                raise RuntimeError(f"Drive is signed in as {email}, expected {state['drive_email']}")
            self.log(f"Connected to Google Drive as {email}")
            found = drive.find_outputs(state["drive_folder_name"], nums)
            self.log(f"Found {len(found)} of {len(nums)} videos in '{state['drive_folder_name']}'")
        missing = [n for n in nums if n not in found and not find_local_video(cfg, n)]
        if missing:
            self.log(f"⚠️ Not found: {', '.join(f'Output {n}' for n in missing)}")

        jobs: queue.Queue = queue.Queue()

        def downloader():
            try:
                for n in nums:
                    if self.stop_event.is_set():
                        break
                    try:
                        if n in found:
                            path = drive.download(found[n], cfg, n, on_progress=lambda f, n=n: self.set_progress(
                                f"Downloading Output {n}: {f * 100:.0f}%"))
                        else:
                            path = find_local_video(cfg, n)
                        if path:
                            jobs.put((n, path))
                        self.q.put(("refresh", n))
                    except Exception as e:
                        self.log(f"❌ Output {n}: download failed: {e}")
                        self._set_error(n, f"Download failed: {e}")
                        self.q.put(("refresh", n))
            finally:
                jobs.put(None)

        t = threading.Thread(target=downloader, daemon=True)
        t.start()
        runner = WhisperRunner(cfg, log=self.log, on_progress=lambda txt: self.set_progress(txt),
                               stop_event=self.stop_event)
        self.runners.append(runner)
        done, failed, text_only = 0, [], []
        while True:
            item = jobs.get()
            if item is None or self.stop_event.is_set():
                break
            n, path = item
            self.set_progress(f"Checking for talking: Output {n}", done / max(1, len(nums)))
            try:
                if runner.detect_type(n, Path(path)) == "text_only":
                    self.log(f"  Output {n}: text-only filler - skipping Whisper (no subtitles needed)")
                    text_only.append(n)
                else:
                    self.set_progress(f"Whisper (GPU): Output {n}", done / max(1, len(nums)))
                    runner.transcribe_and_phrase(n, Path(path), whisper_dir_for(Path(path)))
                self._set_error(n, None)
            except Exception as e:
                failed.append(n)
                self.log(f"❌ Output {n}: {e}")
                self._set_error(n, f"Whisper failed: {str(e)[:300]}")
            done += 1
            self.q.put(("refresh", n))
        t.join(timeout=10)
        if self.stop_event.is_set():
            self.log("Step 1 stopped.")
            return
        msg = f"Step 1 done: {done - len(failed)} of {len(nums)} ready for Step 2."
        if text_only:
            msg += (f"\nText-only (no talking, no subtitles): {', '.join(map(str, text_only))}. "
                    f"Wrong? Select it and change 'Video type'.")
        if failed:
            msg += f"\nWhisper failed: {', '.join(map(str, failed))} (select it and use 'Re-run Whisper…')"
        if missing:
            msg += f"\nNot found in Drive: {', '.join(map(str, missing))}"
        self.log(msg.replace("\n", "  "))
        self.q.put(("info", "Step 1 finished", msg))

    # ------------------------------------------------------------------ STEP 2 (AI)
    def on_step2(self):
        nums = self.batch()
        self.refresh(None)
        S = self.status
        ready = [n for n in nums if S[n]["transcribed"] or S[n]["text_only"]]
        todo = [n for n in ready if not S[n]["metadata"]
                or (not S[n]["text_only"] and not S[n]["translated"])]
        if not ready:
            messagebox.showinfo("Step 2", "No transcripts yet. Run Step 1 first.")
            return
        if not todo:
            messagebox.showinfo("Step 2", "All transcribed videos already have translations and titles.\n"
                                          "To redo one, select it and click 'Regenerate with AI'.")
            return
        label = self.ai_var.get()
        if not messagebox.askyesno("Generate with AI", f"Use {label} for {len(todo)} video(s)?\n\n"
                                   f"Output {', '.join(map(str, todo))}\n\nThis is the only step that uses AI. "
                                   f"Nothing is uploaded - you review everything afterwards."):
            return
        self.run_job("Step 2", self._step2_worker, todo, AI_KEYS[label], set())

    def on_regenerate(self):
        n = self.selected
        st = self.status.get(n, {})
        if not (st.get("transcribed") or st.get("text_only")):
            messagebox.showinfo("Regenerate", "Run Step 1 for this video first.")
            return
        label = self.ai_var.get()
        if not messagebox.askyesno("Regenerate with AI",
                                   f"Re-run {label} for Output {n}?\n\nThe AI subtitle edits and the title/"
                                   f"description are replaced (old versions are backed up). Your own subtitle "
                                   f"fixes in {REVIEW_EDIT_NAME} are kept."):
            return
        self.meta_dirty = False
        self.run_job("Regenerate", self._step2_worker, [n], AI_KEYS[label], {n})

    def _step2_worker(self, nums: list[int], provider_key: str, force: set):
        from ai_processor import generate_for_output, make_provider
        from whisper_runner import WhisperRunner
        provider = make_provider(self.cfg, provider_key, self.log)
        runner = WhisperRunner(self.cfg, log=self.log, on_progress=lambda t: self.set_progress(t),
                               stop_event=self.stop_event)
        self.runners.append(runner)
        failed = []
        for i, n in enumerate(nums):
            if self.stop_event.is_set():
                self.log("Step 2 stopped.")
                break
            self.set_progress(f"AI ({provider.label}): Output {n}  ({i + 1}/{len(nums)})", i / len(nums))
            try:
                generate_for_output(self.cfg, load_state(), n, provider, runner, self.log, force=n in force)
                self._set_error(n, None)
            except Exception as e:
                failed.append(n)
                applog.error(traceback.format_exc())
                self.log(f"❌ Output {n}: {e}")
                self._set_error(n, f"AI step failed: {str(e)[:300]}")
            self.q.put(("refresh", n, n == self.selected))
        if failed:
            self.q.put(("error", "Step 2", f"These videos failed: {', '.join(map(str, failed))}\nSee the log for details."))
        else:
            self.log("Step 2 done. Review titles, descriptions and ⚠️ subtitle lines, then tick the box and click GO.")

    def _add_skip(self, n: int):
        skip = sorted(set(parse_skip(self.skip_var.get())) | {n})
        self.skip_var.set(", ".join(map(str, skip)))

    def on_skip_video(self):
        n = self.selected
        if n is None:
            return
        if not messagebox.askyesno("Skip video", f"Skip Output {n}?\n\nIt is left out of this batch and the next "
                                   f"Output number takes its place, so the batch keeps the same size and dates."):
            return
        self._add_skip(n)
        self.log(f"Output {n} skipped - the next number moves in.")
        self.selected = None
        self.reload_batch()

    def on_remove_video(self):
        n = self.selected
        if n is None:
            return
        if not messagebox.askyesno("Remove from batch", f"Remove Output {n} from this batch?\n\nThe batch becomes one "
                                   f"video shorter. Output {n} is not deleted; it can go in a later batch."):
            return
        nums = self.batch()
        if n != nums[-1]:                 # from the middle: skip it, and shrink so nothing new moves in
            self._add_skip(n)
        self.count_var.set(str(max(1, len(nums) - 1)))
        self.log(f"Output {n} removed - batch is now {len(nums) - 1} video(s).")
        self.selected = None
        self.reload_batch()

    def on_rewhisper(self):
        n = self.selected
        st = self.status.get(n) or {}
        if not st.get("video"):
            messagebox.showinfo("Re-run Whisper", "Not downloaded yet.")
            return
        res = ConfirmDialog(self, "Re-run Whisper",
                            f"Re-transcribe Output {n}? This creates a new phrase list, so the existing "
                            f"translations for this video are moved to a backup folder. Run 'Regenerate with "
                            f"AI' afterwards.\n\nGPU = Intel Arc (fast). CPU = slower, but works if the GPU keeps failing.",
                            "Re-run on GPU", extra_buttons=["Re-run on CPU"]).show()
        if not res:
            return
        engine = "cpu" if "CPU" in res else "gpu"
        self.run_job("Re-run Whisper", self._rewhisper_worker, n, st["video"], st["wdir"], engine)

    def _rewhisper_worker(self, n, video: Path, wdir: Path, engine: str):
        from whisper_runner import WhisperRunner
        edits = wdir / "edits"
        if edits.exists() or (wdir / "translations.json").exists():
            bk = wdir / "edits_backup" / f"{datetime.now():%Y%m%d_%H%M%S}_before_rewhisper"
            bk.mkdir(parents=True, exist_ok=True)
            if edits.exists():
                shutil.move(str(edits), str(bk / "edits"))
            if (wdir / "translations.json").exists():
                shutil.move(str(wdir / "translations.json"), str(bk / "translations.json"))
        runner = WhisperRunner(self.cfg, log=self.log, on_progress=lambda t: self.set_progress(t),
                               stop_event=self.stop_event)
        self.runners.append(runner)
        runner.transcribe_and_phrase(n, video, wdir, force_engine=engine)
        self._set_error(n, None)
        self.log(f"Output {n}: re-transcribed on {engine.upper()}. Now click 'Regenerate with AI'.")
        self.q.put(("refresh", n, True))

    def on_stop(self):
        self.stop_event.set()
        for r in list(self.runners):
            r.cancel()
        self.log("Stopping after the current item ...")

    # ------------------------------------------------------------------ GO
    def on_go(self):
        if not self.confirm_var.get():
            messagebox.showwarning("GO", f"Tick the confirmation box for {self.app_state['youtube_email']} first.")
            return
        if is_locked(UPLOADER_LOCK):
            messagebox.showinfo("GO", "The background uploader is already running.")
            return
        if self.meta_dirty:
            self.save_metadata(show=False)
        nums = self.batch()
        self.refresh(None)
        if self.sub_entries and self.save_pending_sub_changes():
            self.refresh(self.selected)          # its .SRT is now stale and gets rebuilt below

        done, ready, not_ready = [], [], []
        for n in nums:
            st = self.status[n]
            up = (st.get("upload") or {}).get("status")
            if up in ("scheduled", "uploaded_private"):
                done.append(n)
            elif st["ready"]:
                ready.append(n)
            else:
                why = ("not downloaded" if not st["video"]
                       else "no .SRT (or set it to Text-only)" if not (st["srt"] or st["text_only"])
                       else "title/description missing or invalid")
                not_ready.append(f"Output {n}: {why}")
        if not ready:
            messagebox.showinfo("GO", "Nothing to upload." + (f"\n\nNot ready:\n" + "\n".join(not_ready) if not_ready else
                                                             "\n\nEverything in this batch is already scheduled."))
            return
        if not_ready and not messagebox.askyesno(
                "Some videos are not ready",
                "Not ready:\n" + "\n".join(not_ready) + f"\n\nSchedule the {len(ready)} ready video(s) now? "
                "You can click GO again later for the rest; already-scheduled ones are never re-uploaded."):
            return
        clashes = self.slot_conflicts()
        if clashes:
            messagebox.showerror("Slots already taken", "These times are already used by scheduled videos:\n\n"
                                 + "\n".join(clashes[:10])
                                 + "\n\nChange 'First day' (or 'Videos') at the top and try again.")
            return
        soon = now_pht() + timedelta(minutes=20)
        late = [n for n in ready if self.schedule[n] <= soon]
        method = "playwright" if self.pw_var.get() else "api"
        lines = [f"Output {n}  →  {slot_label(self.schedule[n], long=True)}" + ("   (slot passed: private, no schedule)" if n in late else "")
                 for n in ready]
        if not messagebox.askyesno(
                "Confirm upload & schedule",
                f"Upload {len(ready)} video(s) to YouTube account {self.app_state['youtube_email']} as PRIVATE, "
                f"each set to go public at its slot:\n\n" + "\n".join(lines) +
                f"\n\nMethod: {'YouTube Studio browser (Playwright)' if method == 'playwright' else 'YouTube Data API v3'}"
                f"\nUploads continue in the background - you can close this window."):
            return
        self.run_job("GO", self._go_worker, nums, ready, method)

    def _go_worker(self, nums, ready, method):
        from whisper_runner import WhisperRunner
        runner = WhisperRunner(self.cfg, log=self.log, on_progress=lambda t: self.set_progress(t))
        self.runners.append(runner)
        for n in ready:                                 # rebuild any .SRT whose fixes are newer
            st = output_status(self.cfg, load_state(), n)
            if st["srt_stale"] and not st["text_only"]:
                self.log(f"Output {n}: rebuilding .SRT with your latest subtitle fixes ...")
                runner.merge_and_build(n, st["wdir"])
        if method == "api":
            from google_auth import get_credentials
            self.set_progress("Checking YouTube login ...")
            get_credentials("youtube", self.cfg, self.app_state["youtube_email"], interactive=True, log=self.log)
        items = []
        for n in ready:
            st = output_status(self.cfg, load_state(), n)
            srt = "" if st["text_only"] or not st["srt"] else str(st["srt"])   # text-only: no captions
            items.append({"output": n, "video_path": str(st["video"]), "srt_path": srt,
                          "metadata_path": str(metadata_path(n)), "publish_at": self.schedule[n].isoformat()})

        def f(s):
            s["current_batch"] = {
                "start": nums[0], "end": nums[-1], "outputs": list(nums), "week_start": str(self.week_start()), "method": method,
                "items": items, "launched_at": now_pht().isoformat(timespec="seconds"),
                "uploader": {"status": "starting", "message": "Starting ..."},
            }
        update_state(f)
        UPLOADER_STOP.unlink(missing_ok=True)
        py = app_python(windowless=True)
        script = str(APP_DIR / "background_uploader.py")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        kw = dict(cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
        try:
            subprocess.Popen([py, script], creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:
            subprocess.Popen([py, script], creationflags=flags, **kw)
        self.log(f"🚀 Background uploader started for {len(items)} video(s). You can close this window; "
                 f"you'll get a Windows notification when it finishes.")
        self.q.put(("info", "Uploading in the background",
                    f"{len(items)} video(s) are uploading and scheduling in the background.\n\n"
                    f"You can close this window. Progress: bottom bar / logs\\uploader_*.log"))

    def on_stop_uploader(self):
        if messagebox.askyesno("Stop uploader", "Stop the background uploader after the current step?\n"
                                                "Click GO later to resume where it stopped."):
            UPLOADER_STOP.write_text("stop", encoding="utf-8")
            self.log("Stop requested for the background uploader.")

    def _poll_uploader(self):
        try:
            running = is_locked(UPLOADER_LOCK)
            st = load_state()
            u = (st.get("current_batch") or {}).get("uploader") or {}
            msg = u.get("message") or ""
            if running:
                text = f"⬆ Uploader running — {msg}"
            elif u.get("status"):
                text = f"Uploader {u['status'].replace('_', ' ')}: {msg}"
            else:
                text = "Uploader: idle"
            self.up_label.configure(text=text[:300])
            self.btn_stop_up.configure(state="normal" if running else "disabled")
            co = json.dumps(st.get("completed_outputs", {}), sort_keys=True)
            if co != self._last_co:
                self._last_co = co
                self.refresh(None)
            else:
                self._sync_go()
        except Exception as e:
            applog.warning(f"poll failed: {e}")
        self.after(4000, self._poll_uploader)

    def on_studio_login(self):
        if is_locked(UPLOADER_LOCK):
            messagebox.showinfo("Studio login", "Wait until the background uploader has finished.")
            return
        if not messagebox.askyesno(
                "Studio Login Setup",
                f"A Chrome window opens on YouTube Studio using the app's own browser profile.\n\n"
                f"Sign in there by hand as {self.app_state['youtube_email']}, check that the right channel shows, "
                f"then simply close that window. This is only needed once (or when Google logs you out)."):
            return
        subprocess.Popen([app_python(windowless=True), str(APP_DIR / "youtube_playwright_fallback.py"), "--login"],
                         cwd=str(APP_DIR))
        self.log("Opened the Studio login browser. Close it when you're signed in.")

    def on_close(self):
        if self.meta_dirty:
            self.save_metadata(show=False)
        if self.sub_entries:
            self.save_pending_sub_changes()
        if self.busy() and not messagebox.askyesno("Quit", "A step is still running (Whisper/AI). Stop it and quit?"):
            return
        self.on_stop()
        self.destroy()


def main():
    lock = HeldLock(GUI_LOCK)
    if not lock.acquire():
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        messagebox.showinfo("Coach Vee Automator", "The app is already open.")
        return
    try:
        App().mainloop()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
