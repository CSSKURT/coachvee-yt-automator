"""
Step 2: Build the final English SRT + review list from segments.json and translations.json.

SRT style: one short line per cue (~3-9 words, <= 45 chars), no line wrapping,
each cue runs until the next one starts (gaps kept only for real pauses).

translations.json format (written by Claude after reading the transcript):
{
  "1": "Hello everyone, welcome back to the channel.",
  "2": null,                                  <- unsure: left blank, goes to review list
  "3": {"text": null, "guess": "maybe 'the market'", "note": "overlapping voices"}
}
Any segment missing from translations.json is also treated as blank / needs review.

Usage:
    D:\\whisper\\venv\\Scripts\\python.exe D:\\whisper\\build_srt.py "<output folder>"
Outputs: english.srt, needs_review.json, needs_review.txt
"""
import bisect, json, re, sys
from pathlib import Path

MAX_CHARS = 45      # hard limit per line: fits the title-safe area of a 16:9 video
                    # at normal subtitle size. Use --max-chars 30 for 9:16 vertical.
CLOSE_GAP = 1.0     # gaps shorter than this (seconds) are closed so cues are back-to-back
MIN_DUR = 0.6       # shortest cue (seconds)


def ts(sec, sep=","):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


# A line should not END on these (they lead into the next words: "would like to | learn")
WEAK_END = set("""a an the to of and or but nor so for with in on at by from into onto about as
than that which who whom whose my your his her its our their this these those is are was were be
been am would will can could should shall may might must do does did have has had not no very
just i i'm i've i'll i'd you're we're they're it's don't doesn't didn't can't won't wouldn't
if when because like going gonna wanna gotta get some any every more most really how what
where why let's""".split())
# ...and it is natural for a line to START with these ("learn | how to find")
GOOD_START = set("""how what why when where who which that because so and but or if then while
until unless since although though like before after instead especially even""".split())


def _bare(w):
    return w.lower().strip(".,?!;:\"'()[]-")


def chunk_text(text):
    """Break text into lines of <= MAX_CHARS at the most natural phrase boundaries.

    Dynamic programming over every possible break: few lines, whole sentences kept
    together, no line ending on a lead-in word, no tiny orphan lines.
    """
    words = text.split()
    n = len(words)
    INF = float("inf")
    best = [INF] * (n + 1)
    prev = [0] * (n + 1)
    best[0] = 0.0
    for j in range(1, n + 1):
        length = -1
        for i in range(j - 1, -1, -1):          # line = words[i:j]
            length += len(words[i]) + 1
            if length > MAX_CHARS and i < j - 1:
                break
            cost = 40.0                           # every extra line costs
            cost += (MAX_CHARS - length) ** 2 * 0.02   # mild preference for balanced lines
            if length < 15:
                cost += (15 - length) * 4         # avoid orphan scraps like "confidence."
            last, nxt = words[j - 1], (words[j] if j < n else None)
            if nxt is not None:                   # how good is breaking after `last`?
                if last.rstrip('"\')').endswith((".", "?", "!")):
                    cost -= 45                    # end of sentence: break here
                elif last[-1] in ",;:":
                    cost -= 28                    # natural pause
                elif _bare(last) in WEAK_END:
                    cost += 35                    # "would like to | learn"
                elif _bare(nxt) in GOOD_START:
                    cost -= 10                    # "learn | how to"
                else:
                    cost += 12                    # plain mid-phrase break
            if best[i] + cost < best[j]:
                best[j], prev[j] = best[i] + cost, i
    lines, j = [], n
    while j > 0:
        i = prev[j]
        lines.append(" ".join(words[i:j]))
        j = i
    return lines[::-1]


def time_mapper(seg):
    """Map a 0..1 position in the text to a time, following when words were spoken."""
    words = [w for w in seg.get("words", []) if w[2]]
    if not words:
        return lambda f: seg["start"] + f * (seg["end"] - seg["start"])
    total = sum(len(w[2]) + 1 for w in words)
    xs, ys, c = [], [], 0
    for s, e, w in words:
        xs.append(c / total); ys.append(s)
        c += len(w) + 1
        xs.append(c / total); ys.append(e)

    def f(p):
        if p <= xs[0]: return ys[0]
        if p >= xs[-1]: return ys[-1]
        i = bisect.bisect_right(xs, p)
        x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
        return y0 if x1 == x0 else y0 + (p - x0) / (x1 - x0) * (y1 - y0)
    return f


MAX_JOIN_GAP = 1.5   # join Whisper segments into one sentence if the pause is shorter than this


def ends_sentence(text):
    t = text.rstrip().rstrip('"\')]')
    return t.endswith((".", "?", "!", "..."))


def group_cues(group):
    """group = [(seg, english_text), ...] forming one spoken sentence (or run of them).

    Whisper often cuts segments mid-sentence, so the texts are joined first and then
    broken into lines; each segment's share of the joined text keeps that segment's timing.
    """
    spans, pos = [], 0                    # (char_start, char_end, time_mapper) per segment
    for seg, text in group:
        spans.append((pos, pos + len(text) + 1, time_mapper(seg)))
        pos += len(text) + 1
    full = " ".join(t for _, t in group)

    def at(p):                            # char position in `full` -> time
        for c0, c1, tm in spans:
            if p < c1 or (c0, c1, tm) == spans[-1]:
                return tm(min(max((p - c0) / (c1 - c0), 0.0), 1.0))

    cues, p = [], 0
    for line in chunk_text(full):
        a = at(p)
        p += len(line) + 1
        cues.append([a, at(p - 1), line])
    return cues


LEAD_IN = 0.10      # show a line this much before the voice starts
HOLD = 0.15         # keep it this much after the voice stops
MAX_SNAP = 3.0      # never move a boundary further than this


def load_speech(folder, source):
    """Where speech really is in the audio (Silero VAD), cached in speech.json."""
    cache = folder / "speech.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        audio = decode_audio(source, sampling_rate=16000)
        sp = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=250, speech_pad_ms=0))
        sp = [[s["start"] / 16000, s["end"] / 16000] for s in sp]
    except Exception as e:                       # audio moved/missing: build without snapping
        print(f"(speech detection skipped: {e})")
        return None
    cache.write_text(json.dumps(sp), encoding="utf-8")
    return sp


def snap_to_speech(cues, speech):
    """Whisper's word times drift into silences (lines appear before the voice).
    Move a start that falls in silence forward to the next speech onset, and an end
    that falls in silence back to where the voice stopped."""
    ons = [a for a, b in speech]

    def region(t):                               # index of speech region containing t, else None
        i = bisect.bisect_right(ons, t) - 1
        return i if i >= 0 and speech[i][0] - 0.05 <= t <= speech[i][1] + 0.05 else None

    def speech_ahead(t):                         # is there real speech right after t?
        i = region(t)
        return i is not None and speech[i][1] - t >= 0.3

    moved = 0
    for c in cues:
        a, b = c[0], c[1]
        if not speech_ahead(a + LEAD_IN):
            i = bisect.bisect_right(ons, a)
            if i < len(ons) and ons[i] - a <= MAX_SNAP and ons[i] < b - 0.2:
                a = ons[i] - LEAD_IN
        if region(b) is None:
            i = bisect.bisect_right(ons, b) - 1
            if i >= 0 and speech[i][1] > a + 0.3 and b - speech[i][1] <= MAX_SNAP:
                b = speech[i][1] + HOLD
        else:                                    # voice still going at the end: hold until it stops
            i = region(b)
            if speech[i][1] - b <= 1.0:
                b = max(b, speech[i][1] + HOLD)
        moved += (a, b) != (c[0], c[1])
        c[0], c[1] = a, b
    return moved


def phrase_cues(items):
    """Phrase mode: items = [(phrase, english)]. Every cue keeps real word times.

    A phrase that fits on one line is one cue, timed from its first to last word.
    A longer one is split into lines whose boundaries snap to Whisper word boundaries.
    Neighbouring short phrases from the same unpunctuated run are merged when they fit.
    """
    raw = []
    for p, text in items:
        if len(text) <= MAX_CHARS:
            raw.append([p["start"], p["end"], text])
            continue
        lines = chunk_text(text)
        words = p.get("words") or [[p["start"], p["end"], p["text"]]]
        total = sum(len(l) + 1 for l in lines)
        cuts, pos, prev_idx = [], 0, 0
        for l in lines[:-1]:                       # pick a word boundary for each line break
            pos += len(l) + 1
            target = pos / total * len(words)
            idx = min(max(round(target), prev_idx + 1), len(words) - (len(lines) - len(cuts) - 1))
            cuts.append(idx)
            prev_idx = idx
        bounds = [0] + cuts + [len(words)]
        for k, l in enumerate(lines):
            i0, i1 = bounds[k], max(bounds[k + 1], bounds[k] + 1)
            raw.append([words[min(i0, len(words) - 1)][0], words[min(i1, len(words)) - 1][1], l])

    merged = []
    for c in raw:
        if merged:
            m = merged[-1]
            if (not m[2].rstrip('"\'').endswith((".", "?", "!", ",", ";", ":"))
                    and c[0] - m[1] < 0.5
                    and min(len(m[2]), len(c[2])) < 20
                    and len(m[2]) + 1 + len(c[2]) <= MAX_CHARS):
                m[1], m[2] = c[1], m[2] + " " + c[2]
                continue
        merged.append(c)
    return merged


def main():
    global MAX_CHARS
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS,
                    help="max characters per line: 45 for 16:9 landscape (default), ~30 for 9:16 vertical")
    ap.add_argument("--name", default="english",
                    help="base name; each build is saved as <name>_v<N>.srt with N = next free number")
    a = ap.parse_args()
    MAX_CHARS = a.max_chars
    base = a.name[:-4] if a.name.lower().endswith(".srt") else a.name
    base = re.sub(r"_v\d+$", "", base)
    existing = [int(m.group(1)) for p in Path(a.folder).glob(f"{base}_v*.srt")
                if (m := re.fullmatch(re.escape(base) + r"_v(\d+)\.srt", p.name, re.I))]
    out_name = f"{base}_v{max(existing, default=0) + 1}.srt"   # never overwrites a version
    folder = Path(a.folder)
    src = folder / "phrases.json" if (folder / "phrases.json").exists() else folder / "segments.json"
    meta = json.loads(src.read_text(encoding="utf-8-sig"))
    segs, phrase_mode = meta["segments"], meta.get("phrase_mode", False)
    tr = json.loads((folder / "translations.json").read_text(encoding="utf-8-sig"))

    cues, review, group = [], [], []

    def flush():
        if group:
            cues.extend(phrase_cues(group) if phrase_mode else group_cues(group))
            group.clear()

    for s in segs:
        v = tr.get(str(s["id"]))
        guess = note = None
        if isinstance(v, dict):
            guess, note, v = v.get("guess"), v.get("note"), v.get("text")
        if v and v.strip():
            v = " ".join(v.split())
            if group and (ends_sentence(group[-1][1])
                          or s["start"] - group[-1][0]["end"] > MAX_JOIN_GAP):
                flush()
            group.append((s, v))
        else:
            flush()                       # a blank part always breaks the sentence
            review.append({
                "id": s["id"],
                "start": ts(s["start"], "."), "end": ts(s["end"], "."),
                "whisper_heard": s["text"],
                "best_guess": guess,
                "why": note or ", ".join(s["flag_reasons"]) or "not translated",
            })
    flush()                               # the last sentence group (was dropped before)

    # line timing follows the real voice in the audio, not just Whisper's word estimates
    speech = load_speech(folder, meta.get("source", ""))
    snapped = snap_to_speech(cues, speech) if speech else 0

    # tidy timing: no overlaps, close small gaps, enforce a minimum duration
    cues.sort(key=lambda c: c[0])
    # drop zero-length repeats (Whisper sometimes emits a line twice at the same instant)
    cues = [c for i, c in enumerate(cues)
            if not (c[1] - c[0] < 0.05 and i > 0
                    and c[2].rstrip(".,").lower() in cues[i - 1][2].lower())]
    for i, c in enumerate(cues):
        nxt = cues[i + 1][0] if i + 1 < len(cues) else None
        if c[1] - c[0] < MIN_DUR:
            c[1] = c[0] + MIN_DUR
        if nxt is not None and (nxt - c[1] < CLOSE_GAP or c[1] > nxt):
            c[1] = nxt

    with open(folder / out_name, "w", encoding="utf-8") as f:
        for i, (a, b, t) in enumerate(cues, 1):
            f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n\n")

    (folder / "needs_review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(folder / "needs_review.txt", "w", encoding="utf-8") as f:
        f.write(f"{len(review)} parts left blank in {out_name} - listen and fill these in:\n\n")
        for r in review:
            f.write(f"#{r['id']}  {r['start']} -> {r['end']}\n"
                    f"   Whisper heard: {r['whisper_heard']}\n"
                    f"   Best guess:    {r['best_guess'] or '-'}\n"
                    f"   Why:           {r['why']}\n\n")

    if phrase_mode:                       # every Tagalog line, side by side, for spot-checking
        rows = []
        for s in segs:
            v = tr.get(str(s["id"]))
            v = v.get("text") if isinstance(v, dict) else v
            if s.get("tagalog") and v:
                rows.append(f"#{s['id']}  {ts(s['start'], '.')}\n   Whisper heard: {s['text']}\n   English:       {v}\n")
        (folder / "tagalog_check.txt").write_text(
            f"{len(rows)} subtitle lines came from Tagalog. Whisper sometimes mishears Tagalog,\n"
            f"so check these against the audio first:\n\n" + "\n".join(rows), encoding="utf-8")
    print(f"{out_name}: {len(cues)} cues | {snapped} re-timed to the voice | "
          f"needs_review: {len(review)} blank parts")


if __name__ == "__main__":
    main()
