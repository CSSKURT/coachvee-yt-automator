"""
Step 1: Transcribe a long audio/video file with Whisper and score confidence.

Uses the Intel Arc GPU (PyTorch XPU + openai-whisper) when available,
otherwise falls back to faster-whisper on the CPU.

Usage:
    D:\\whisper\\venv\\Scripts\\python.exe D:\\whisper\\transcribe.py "path\\to\\audio.mp3"
Options:
    --model large-v3 | large-v3-turbo | medium   (default large-v3: best for Taglish)
    --language auto | tl | en                     (default auto)
    --engine auto | gpu | cpu                     (default auto)
    --out <folder>                                (default: <audio name>_whisper next to the audio)

Outputs (in the --out folder):
    segments.json          every segment with timings, text, confidence metrics, flag reasons
    original.srt           raw Whisper transcript (original language, no translation)
    transcript_review.txt  compact "[id] time FLAG text" listing, for translating/reviewing
"""
import argparse, json, os, sys, time
from pathlib import Path

os.environ.setdefault("HF_HOME", r"D:\whisper\models")
MODEL_DIR = r"D:\whisper\models\openai-whisper"
FFMPEG_DIR = r"D:\ytdl"                      # openai-whisper needs ffmpeg on PATH
os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

# A natural Taglish prompt (tested better than an English description of the language mix).
PROMPT = ("Okay, ano ibig sabihin nun? Kasi diba, yung mga lalaki ngayon, "
          "they want a woman na marunong. Sige, tanong natin.")

# Confidence thresholds (tweak if too many / too few segments get flagged)
LOW_WORD_PROB = 0.45        # a word below this counts as "unsure"
MAX_UNSURE_RATIO = 0.30     # flag if >30% of words are unsure
MIN_MEAN_WORD_PROB = 0.60
MIN_AVG_LOGPROB = -0.90
MAX_COMPRESSION = 2.4       # high = repetitive text, typical hallucination
MAX_NO_SPEECH = 0.60


def ts(sec: float, sep=",") -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


def gpu_available():
    try:
        import torch
        return torch.xpu.is_available()
    except Exception:
        return False


def run_gpu(audio, model_name, language):
    """openai-whisper on Intel Arc (XPU). Yields normalized segment dicts."""
    import torch, whisper
    print(f"Engine: GPU ({torch.xpu.get_device_name(0)})", flush=True)
    # load on CPU, convert to fp16, then move: ~3 GB VRAM instead of ~9 GB
    model = whisper.load_model(model_name, device="cpu", download_root=MODEL_DIR)
    model = model.half()
    for m in model.modules():                   # whisper runs LayerNorm in fp32
        if isinstance(m, torch.nn.LayerNorm):
            m.float()
    model = model.to("xpu")
    result = whisper.transcribe(
        model, str(audio),
        language=None if language == "auto" else language,
        task="transcribe",
        fp16=True,
        beam_size=5, best_of=5,
        word_timestamps=True,
        condition_on_previous_text=True,        # keeps sentence context: cleaner Taglish sentences
        hallucination_silence_threshold=2.0,    # skip made-up text in long silences
        compression_ratio_threshold=2.2,        # retry windows that start repeating themselves
        initial_prompt=PROMPT,
        verbose=False,                          # shows a progress bar
    )
    print(f"Detected language: {result['language']}", flush=True)
    for s in result["segments"]:
        yield {
            "start": s["start"], "end": s["end"], "text": s["text"],
            "avg_logprob": s["avg_logprob"], "no_speech_prob": s["no_speech_prob"],
            "compression_ratio": s["compression_ratio"],
            "words": [(w["word"], w["probability"], w["start"], w["end"]) for w in s.get("words", [])],
        }, result["language"]


def run_cpu(audio, model_name, language):
    """faster-whisper on CPU (slower)."""
    from faster_whisper import WhisperModel
    print("Engine: CPU (faster-whisper int8)", flush=True)
    model = WhisperModel(model_name, device="cpu", compute_type="int8",
                         cpu_threads=os.cpu_count() or 8)
    segments, info = model.transcribe(
        str(audio),
        language=None if language == "auto" else language,
        beam_size=5, word_timestamps=True,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False, initial_prompt=PROMPT,
    )
    print(f"Detected language: {info.language}, duration {ts(info.duration, '.')}", flush=True)
    t0 = time.time()
    for s in segments:
        pct = s.end / info.duration * 100 if info.duration else 0
        el = time.time() - t0
        print(f"\r  {pct:5.1f}%  elapsed {el/60:5.1f} min  ETA {el/max(pct,0.1)*(100-pct)/60:5.1f} min ",
              end="", flush=True)
        yield {
            "start": s.start, "end": s.end, "text": s.text,
            "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob,
            "compression_ratio": s.compression_ratio,
            "words": [(w.word, w.probability, w.start, w.end) for w in (s.words or [])],
        }, info.language
    print()


def score(seg, idx):
    words = seg["words"]
    probs = [w[1] for w in words]
    mean_p = sum(probs) / len(probs) if probs else 0.0
    unsure = [w[0].strip() for w in words if w[1] < LOW_WORD_PROB]
    ratio = len(unsure) / len(words) if words else 1.0
    reasons = []
    if seg["avg_logprob"] < MIN_AVG_LOGPROB: reasons.append("low_avg_logprob")
    if mean_p < MIN_MEAN_WORD_PROB: reasons.append("low_word_confidence")
    if ratio > MAX_UNSURE_RATIO: reasons.append("many_unsure_words")
    if seg["compression_ratio"] > MAX_COMPRESSION: reasons.append("repetitive_possible_hallucination")
    if seg["no_speech_prob"] > MAX_NO_SPEECH: reasons.append("maybe_no_speech")
    return {
        "id": idx,
        "start": round(seg["start"], 3), "end": round(seg["end"], 3),
        "text": seg["text"].strip(),
        "avg_logprob": round(seg["avg_logprob"], 3),
        "mean_word_prob": round(mean_p, 3),
        "no_speech_prob": round(seg["no_speech_prob"], 3),
        "compression_ratio": round(seg["compression_ratio"], 3),
        "unsure_words": unsure,
        "flagged": bool(reasons), "flag_reasons": reasons,
        # [start, end, word] - used to time the short subtitle lines
        "words": [[round(w[2], 2), round(w[3], 2), w[0].strip()] for w in words],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--engine", default="auto", choices=["auto", "gpu", "cpu"])
    ap.add_argument("--out")
    a = ap.parse_args()

    audio = Path(a.audio).resolve()
    out = Path(a.out) if a.out else audio.with_name(audio.stem + "_whisper")
    out.mkdir(parents=True, exist_ok=True)

    use_gpu = a.engine == "gpu" or (a.engine == "auto" and gpu_available())
    runner = run_gpu if use_gpu else run_cpu

    t0 = time.time()
    rows, lang = [], None
    for seg, lang in runner(audio, a.model, a.language):
        if seg["text"].strip():
            rows.append(score(seg, len(rows) + 1))

    meta = {"source": str(audio), "model": a.model, "language": lang, "segments": rows}
    (out / "segments.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    with open(out / "original.srt", "w", encoding="utf-8") as f:
        for i, r in enumerate(rows, 1):
            f.write(f"{i}\n{ts(r['start'])} --> {ts(r['end'])}\n{r['text']}\n\n")

    with open(out / "transcript_review.txt", "w", encoding="utf-8") as f:
        for r in rows:
            flag = "FLAG" if r["flagged"] else "ok  "
            f.write(f"[{r['id']}] {ts(r['start'], '.')[:-4]} {flag} {r['text']}\n")

    n_flag = sum(r["flagged"] for r in rows)
    print(f"Done in {(time.time()-t0)/60:.1f} min: {len(rows)} segments, {n_flag} flagged.")
    print(f"Output folder: {out}")


if __name__ == "__main__":
    sys.exit(main())
