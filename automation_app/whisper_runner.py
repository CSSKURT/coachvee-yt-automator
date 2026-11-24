"""
Stage 2 - sequential local Whisper runner (0% AI).

Calls the EXISTING, UNCHANGED scripts in D:\\whisper with the Whisper venv's own python.exe,
exactly as documented in WHISPER_PIPELINE_HANDOFF.md:

  1. transcribe.py "<video>" --model large-v3 --language tl --out "<folder>"
  2. make_phrases.py "<folder>"
  3. merge_edits.py "<folder>"                       (after translation)
  4. build_srt.py "<folder>" --name <base> --max-chars 30

GPU rule: only ONE transcription at a time (thread lock + cross-process lock file), and each
process must exit 0 before the next video starts.
"""
from __future__ import annotations

import gc
import shutil
import json
import re
import threading
import time
from pathlib import Path

from common import GPU_LOCK, file_lock, json_is_valid, latest_srt, read_json_file, run_streamed, srt_base_name

_GPU_THREAD_LOCK = threading.Lock()
OOM_RE = re.compile(r"out of memory|OutOfMemory|OUT_OF_DEVICE_MEMORY|UR_RESULT_ERROR_OUT_OF|"
                    r"not enough memory|DefaultCPUAllocator|MemoryError", re.I)


class WhisperError(Exception):
    pass


class WhisperRunner:
    def __init__(self, cfg: dict, log=print, on_progress=None, stop_event: threading.Event | None = None):
        self.cfg = cfg
        self.log = log
        self.on_progress = on_progress or (lambda text: None)
        self.stop_event = stop_event or threading.Event()
        self.py = cfg["whisper_python"]
        self.wd = Path(cfg["whisper_dir"])
        self._procs: list = []

    # ------------------------------------------------------------------ helpers
    def cancel(self):
        self.stop_event.set()
        for p in self._procs:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass

    def _run(self, args: list[str], label: str) -> tuple[int, str]:
        cmd = [self.py, *args]
        self.log(f"    $ {label}")

        def on_line(text, is_prog):
            if is_prog:
                self.on_progress(text[-120:])
            else:
                self.log(f"      {text}")
        self._procs.clear()
        return run_streamed(cmd, on_line=on_line, cwd=str(self.wd), proc_holder=self._procs)

    def _check_paths(self):
        if not Path(self.py).exists():
            raise WhisperError(f"Whisper venv python not found: {self.py}")
        for s in ("transcribe.py", "make_phrases.py", "merge_edits.py", "build_srt.py"):
            if not (self.wd / s).exists():
                raise WhisperError(f"Missing pipeline script: {self.wd / s}")

    # ------------------------------------------------------------------ talking or text-only?
    def detect_type(self, n: int, video: Path) -> str:
        """Measure real speech with Silero VAD (CPU, a few seconds). Saves job/video_type.json."""
        from common import classify_speech, effective_type, save_video_type, video_type_info
        info = video_type_info(self.cfg, n)
        if info.get("detected") and info.get("source") == str(video):
            return effective_type(info)
        helper = Path(__file__).resolve().parent / "detect_speech.py"
        code, out = run_streamed([self.py, str(helper), str(video)], cwd=str(self.wd))
        try:
            m = json.loads(next(l for l in reversed(out.splitlines()) if l.startswith("{")))
        except (StopIteration, ValueError):
            self.log(f"  Output {n}: could not measure speech (exit {code}) - treating as a talking video")
            return effective_type(save_video_type(self.cfg, n, detected="talking", source=str(video)))
        kind = classify_speech(m)
        info = save_video_type(self.cfg, n, detected=kind, source=str(video), **m)
        label = "📝 TEXT-ONLY (no talking)" if kind == "text_only" else "🎙 talking"
        self.log(f"  Output {n}: {m['speech_seconds']:.1f}s of speech in {m['duration']:.0f}s -> {label}"
                 + ("" if info["mode"] == "auto" else f"  (you set it to: {info['mode']})"))
        return effective_type(info)

    # ------------------------------------------------------------------ missed-speech guard
    @staticmethod
    def _coverage(seg_file: Path) -> float:
        segs = (read_json_file(seg_file, {}) or {}).get("segments", [])
        return sum(max(0.0, s["end"] - s["start"]) for s in segs)

    def _cpu_retry_if_gap(self, n: int, video: Path, wdir: Path):
        """The GPU pass sometimes drops a whole 30 s chunk (music/acting under the voice) or replaces it
        with 'Thank you for watching!'. If it covered under half of the measured talking, transcribe
        again on the CPU engine (which first finds the voice) and keep whichever result covers more.
        Songs at the end are NOT a reason to retry (they stay under the threshold, and are better left out)."""
        from common import video_type_info
        speech = float(video_type_info(self.cfg, n).get("speech_seconds") or 0)
        got = self._coverage(wdir / "segments.json")
        if speech < 8 or got >= 0.5 * speech:
            return
        self.log(f"  Output {n}: Whisper only caught {got:.0f}s of ~{speech:.0f}s of talking - "
                 f"re-checking on the CPU engine (about 1 min) ...")
        alt = wdir.with_name(wdir.name + "_cpu_check")
        args = [str(self.wd / "transcribe.py"), str(video), "--model", self.cfg.get("whisper_model", "large-v3"),
                "--language", self.cfg.get("whisper_language", "tl"), "--engine", "cpu", "--out", str(alt)]
        code, _out = self._run(args, f"transcribe.py \"{video.name}\" --engine cpu")
        if code == 0 and json_is_valid(alt / "segments.json") and self._coverage(alt / "segments.json") > got:
            for f in ("segments.json", "original.srt", "transcript_review.txt"):
                if (alt / f).exists():
                    shutil.copy2(alt / f, wdir / f)
            self.log(f"  Output {n}: CPU pass caught {self._coverage(wdir / 'segments.json'):.0f}s - using it")
        else:
            self.log(f"  Output {n}: CPU pass was not better - keeping the GPU result")
        shutil.rmtree(alt, ignore_errors=True)

    # ------------------------------------------------------------------ steps 1-2
    def transcribe_and_phrase(self, n: int, video: Path, wdir: Path, force_engine: str | None = None) -> Path:
        """Runs transcribe.py (if needed) then make_phrases.py. Returns the output folder."""
        self._check_paths()
        if not force_engine and json_is_valid(wdir / "phrases.json"):
            self.log(f"  Output {n}: phrases.json already exists - skipping Whisper")
            return wdir
        if not video.exists():
            raise WhisperError(f"Video file is missing: {video}")

        seg_ok = json_is_valid(wdir / "segments.json")
        if seg_ok and not force_engine:
            src = (read_json_file(wdir / "segments.json", {}) or {}).get("source", "")
            seg_ok = Path(src).resolve() == video.resolve() if src else False
        if not seg_ok or force_engine:
            engines = [force_engine] if force_engine else ["auto", "auto"]
            if not force_engine and self.cfg.get("cpu_fallback_after_gpu_failures", True):
                engines.append("cpu")
            last = ""
            for i, engine in enumerate(engines, 1):
                if self.stop_event.is_set():
                    raise WhisperError("Stopped by user")
                args = [str(self.wd / "transcribe.py"), str(video),
                        "--model", self.cfg.get("whisper_model", "large-v3"),
                        "--language", self.cfg.get("whisper_language", "tl"),
                        "--out", str(wdir)]
                if engine != "auto":
                    args += ["--engine", engine]
                self.log(f"  Output {n}: transcribing (attempt {i}, engine={engine}) ...")
                t0 = time.time()
                with _GPU_THREAD_LOCK, file_lock(GPU_LOCK, timeout=6 * 3600):
                    code, out = self._run(args, f"transcribe.py \"{video.name}\" --language tl")
                if code == 0 and json_is_valid(wdir / "segments.json"):
                    self.log(f"  Output {n}: transcribed in {(time.time() - t0) / 60:.1f} min")
                    if engine != "cpu":
                        self._cpu_retry_if_gap(n, video, wdir)
                    break
                last = out[-1500:]
                if self.stop_event.is_set():
                    raise WhisperError("Stopped by user")
                oom = bool(OOM_RE.search(out))
                self.log(f"  Output {n}: transcribe.py failed (exit {code})"
                         + (" - GPU out of memory (close CapCut/Discord/games to free VRAM)" if oom else ""))
                if i < len(engines):
                    wait = 45 if oom else 15
                    self.log(f"  Output {n}: waiting {wait}s for GPU memory to clear, then retrying ...")
                    gc.collect()
                    for _ in range(wait):
                        if self.stop_event.is_set():
                            raise WhisperError("Stopped by user")
                        time.sleep(1)
            else:
                raise WhisperError(f"Transcription failed after {len(engines)} attempts.\n{last}")

        code, out = self._run([str(self.wd / "make_phrases.py"), str(wdir)], "make_phrases.py")
        if code != 0 or not json_is_valid(wdir / "phrases.json"):
            raise WhisperError(f"make_phrases.py failed (exit {code}):\n{out[-1500:]}")
        return wdir

    # ------------------------------------------------------------------ steps 3-4
    def merge_and_build(self, n: int, wdir: Path) -> dict:
        """merge_edits.py + build_srt.py. Returns {srt, blanks, speech_skipped, summary}."""
        self._check_paths()
        code, out = self._run([str(self.wd / "merge_edits.py"), str(wdir)], "merge_edits.py")
        if code != 0:
            raise WhisperError(f"merge_edits.py failed (exit {code}):\n{out[-1500:]}")
        unknown = [l for l in out.splitlines() if "unknown id" in l]
        args = [str(self.wd / "build_srt.py"), str(wdir), "--name", srt_base_name(n),
                "--max-chars", str(self.cfg.get("srt_max_chars", 30))]
        code, out = self._run(args, f"build_srt.py --name {srt_base_name(n)}")
        if code != 0:
            raise WhisperError(f"build_srt.py failed (exit {code}):\n{out[-1500:]}")
        speech_skipped = "speech detection skipped" in out
        if speech_skipped:
            self.log(f"  Output {n}: WARNING voice snapping was skipped - the source video moved. "
                     f"Keep the video in its job folder until the SRT is built.")
        review = read_json_file(wdir / "needs_review.json", []) or []
        srt = latest_srt(wdir, n)
        summary = next((l for l in out.splitlines() if ".srt:" in l), "")
        return {"srt": srt, "blanks": len(review), "speech_skipped": speech_skipped,
                "unknown_ids": unknown, "summary": summary}
