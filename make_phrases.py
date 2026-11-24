"""
Step 1b: split Whisper segments into short, exactly-timed phrases for translation.

Each phrase is a run of Whisper words, cut at punctuation, pauses, or before a natural
break word, and kept short enough (~42 chars) to become one subtitle line. Because a phrase
is translated as a unit and keeps its first/last word times, every subtitle line starts and
ends when those words are actually spoken, even when English word order differs from Tagalog.

Usage: D:\\whisper\\venv\\Scripts\\python.exe D:\\whisper\\make_phrases.py "<output folder>"
Writes: phrases.json (same shape as segments.json) and phrase_review.txt
"""
import json, re, sys
from pathlib import Path

MAX_SRC = 42        # max characters of original text per phrase
MIN_SRC = 12        # don't cut at a comma before this many characters
PAUSE = 0.40        # a gap this long between words is a natural cut

BREAK_BEFORE = set("""how what why when where who which that because so and but or if then while
until unless since although though like before after kasi pero tapos para kung kapag pag
dahil kaya nung noong yung""".split())

TAGALOG = re.compile(r"\b(ang|ng|mga|yung|naman|kasi|hindi|siya|ako|talaga|pero|kung|lang|natin|"
                     r"nyo|niya|diba|pala|yan|dito|kayo|tayo|sa|ba|na|ko|mo|ka|eh|daw|pag|ganun|"
                     r"sabi|ito|iyon|kami|namin|nila|sila|po|opo|wala|meron|may|tapos|para|dapat|"
                     r"gusto|alam|ano|bakit|paano|sige|ikaw|akin|sayo|sa'yo|niyo|natin|nung|ibig|"
                     r"sabihin|kanina|siguro|parang|malinaw|sino|tama|eto|siyempre|syempre|oo|"
                     r"ayan|ganyan|ganito|kaya|lagi|ngayon|nga|din|rin|pa|lalaki|babae)\b", re.I)


def ts(sec):
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    return f"{h:02}:{m:02}:{s:04.1f}"


ABBREV = {"dr.", "mr.", "ms.", "mrs.", "st.", "vs.", "sr.", "jr."}


def join(ws):
    """Join Whisper word tokens: no space before '-relocate', ',', or after 'mag-'."""
    out = ""
    for w in ws:
        t = w[2]
        if out and not (t[:1] in "-,.?!;:'" or out.endswith("-")):
            out += " "
        out += t
    return out


def ends_sentence(t):
    return t.endswith((".", "?", "!")) and t.lower() not in ABBREV


def split_words(words):
    """words = [[start, end, text], ...] -> list of word-lists (phrases)."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        text = join(cur)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None:
            break
        gap = nxt[0] - w[1]
        nxt_len = len(join(cur + [nxt]))
        bare_next = nxt[2].lower().strip(".,?!;:\"'")
        # how much is left before the next punctuation? don't strand a 1-2 word scrap
        rest = []
        for x in words[i + 1:]:
            rest.append(x)
            if x[2].endswith((".", "?", "!", ",", ";", ":")):
                break
        too_long = nxt_len > MAX_SRC and not (len(join(rest)) <= 12 and len(join(cur + rest)) <= MAX_SRC + 12)
        cut = (ends_sentence(w[2])
               or (w[2].endswith((",", ";", ":")) and len(text) >= MIN_SRC)
               or (gap >= PAUSE and len(text) >= 8)
               or too_long
               or (bare_next in BREAK_BEFORE and len(text) >= 25))
        if cut:
            out.append(cur)
            cur = []
    if cur:
        # a one-word scrap at the end reads badly: glue it back if it fits
        if out and len(cur) == 1 and len(join(out[-1] + cur)) <= MAX_SRC + 8:
            out[-1] = out[-1] + cur
        else:
            out.append(cur)
    return out


def main():
    folder = Path(sys.argv[1])
    meta = json.loads((folder / "segments.json").read_text(encoding="utf-8-sig"))
    phrases = []
    for seg in meta["segments"]:
        words = [w for w in seg.get("words", []) if w[2]]
        groups = split_words(words) if words else []
        if not groups:
            groups = [[[seg["start"], seg["end"], seg["text"]]]]
        for g in groups:
            text = join(g)
            unsure = [u for u in seg.get("unsure_words", []) if u and u in text]
            phrases.append({
                "id": len(phrases) + 1,
                "segment": seg["id"],
                "start": g[0][0], "end": g[-1][1],
                "text": text,
                "words": g,
                "tagalog": bool(TAGALOG.search(text)),
                "flagged": seg["flagged"] and (bool(unsure) or len(groups) == 1),
                "flag_reasons": seg["flag_reasons"] if seg["flagged"] else [],
                "unsure_words": unsure,
            })
    out = dict(meta, phrase_mode=True, segments=phrases)
    (folder / "phrases.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(folder / "phrase_review.txt", "w", encoding="utf-8") as f:
        for p in phrases:
            tag = "FLAG" if p["flagged"] else ("tl  " if p["tagalog"] else "en  ")
            f.write(f"[{p['id']}] {ts(p['start'])} {tag} {p['text']}\n")
    n_tl = sum(p["tagalog"] for p in phrases)
    print(f"phrases.json: {len(phrases)} phrases from {len(meta['segments'])} segments "
          f"({n_tl} contain Tagalog, {sum(p['flagged'] for p in phrases)} flagged)")


if __name__ == "__main__":
    main()
