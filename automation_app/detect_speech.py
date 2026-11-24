"""
How much talking is in a video? (0% AI - Silero voice detection from faster-whisper)

Runs with the WHISPER venv (it already has faster-whisper), e.g.
    D:\\whisper\\venv\\Scripts\\python.exe detect_speech.py "<video>"
Prints one JSON line: {"duration": s, "speech_seconds": s, "speech_ratio": r, "longest_speech": s}
Used to spot text-only filler videos (music + on-screen text, nobody talking).
"""
import json
import os
import sys

os.environ["PATH"] = r"D:\ytdl" + os.pathsep + os.environ.get("PATH", "")


def main():
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    audio = decode_audio(sys.argv[1], sampling_rate=16000)
    dur = len(audio) / 16000
    sp = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300, min_speech_duration_ms=250))
    lens = [(s["end"] - s["start"]) / 16000 for s in sp]
    speech = sum(lens)
    print(json.dumps({"duration": round(dur, 2), "speech_seconds": round(speech, 2),
                      "speech_ratio": round(speech / dur, 3) if dur else 0.0,
                      "longest_speech": round(max(lens, default=0), 2)}))


if __name__ == "__main__":
    main()
