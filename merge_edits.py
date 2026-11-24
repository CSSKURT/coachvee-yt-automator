"""
Merge Claude's edit files (edits/*.json) over Whisper's original text into translations.json.

Segments not mentioned in any edit file keep Whisper's text as-is (already clean English).
An edit value of a string replaces the text; null or {"text": null, ...} blanks it for review.

Usage: D:\\whisper\\venv\\Scripts\\python.exe D:\\whisper\\merge_edits.py "<output folder>"
"""
import json, re, sys
from pathlib import Path

# Spelling fixes applied to every final line (Whisper often hears "Coach V" as "Coach B").
NAME_FIXES = [
    (re.compile(r"\bCoach\s+(V|B|Vi)\b(?!ee)", re.I), "Coach Vee"),
    (re.compile(r"\b(Ma'am|Maam|Mam|Dr\.?)\s+L[ae]rr?(y|ie|i)\b", re.I),
     lambda m: f"{m.group(1)} Lerrie"),
    (re.compile(r"\b(mom|mum)\s+L[ae]rr?(y|ie|i)\b", re.I), "Ma'am Lerrie"),   # Whisper hears "Ma'am" as "mom"
]


def fix_names(v):
    if isinstance(v, str):
        for pat, rep in NAME_FIXES:
            v = pat.sub(rep, v)
    elif isinstance(v, dict):
        v = {k: fix_names(x) for k, x in v.items()}
    return v

folder = Path(sys.argv[1])
src = folder / "phrases.json" if (folder / "phrases.json").exists() else folder / "segments.json"
segs = json.loads(src.read_text(encoding="utf-8-sig"))["segments"]
tr = {str(s["id"]): s["text"] for s in segs}
n = 0
for f in sorted((folder / "edits").glob("*.json")):
    edits = json.loads(f.read_text(encoding="utf-8-sig"))
    for k, v in edits.items():
        if k not in tr:
            print(f"warning: {f.name} has unknown id {k}")
        tr[k] = v
        n += 1
tr = {k: fix_names(v) for k, v in tr.items()}
(folder / "translations.json").write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"translations.json: {len(tr)} segments, {n} edited by Claude")
