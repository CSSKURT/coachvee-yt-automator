"""
Stage 3 - on-demand AI translation + copywriting (THE ONLY STEP THAT USES AI).

Runs only when the user clicks "2. Generate Translations, Titles & Descriptions".
One call per video (Shorts are short): the model returns subtitle edits in the exact
edits/*.json format from WHISPER_PIPELINE_HANDOFF.md plus the creative metadata fields.
Everything is validated in Python; an invalid reply is re-asked for that video only.

Providers:
  - "claude_cli": the local `claude -p` in headless print mode. No tools, a tiny system
    prompt, no session saved, neutral working folder, exits after one reply.
  - "gemini":     google-genai SDK with a free Google AI Studio key from config.json.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from common import (AI_EDIT_RE, AI_WORK_DIR, CREATE_NO_WINDOW, find_claude_cli, find_local_video,
                    metadata_path, output_status, read_json_file, whisper_dir_for, write_json_atomic)
from metadata_rules import EXAMPLE_DESCRIPTION, EXAMPLE_TITLE, build_description, build_title, enforce_title

PROVIDERS = {"claude_cli": "Claude CLI (Batch -p)", "gemini": "Gemini Free API"}
SYSTEM_PROMPT = ("You are a careful Taglish-to-English subtitle translator and YouTube Shorts copywriter "
                 "for Coach Vee. You reply with exactly one JSON object and nothing else - no markdown, "
                 "no code fences, no commentary.")


class AIError(Exception):
    pass


# ============================================================================ providers
class ClaudeCLIProvider:
    label = PROVIDERS["claude_cli"]

    def __init__(self, cfg: dict, log=print):
        self.exe = find_claude_cli(cfg)
        if not self.exe:
            raise AIError("Could not find the `claude` CLI. Set its path in Settings "
                          "(it ships with the Claude desktop app under %APPDATA%\\Claude\\claude-code).")
        self.model = (cfg.get("claude_model") or "").strip()
        self.log = log

    def generate(self, prompt: str) -> str:
        cmd = [self.exe, "-p", "--output-format", "json", "--no-session-persistence",
               "--strict-mcp-config", "--tools", "", "--system-prompt", SYSTEM_PROMPT]
        if self.model:
            cmd += ["--model", self.model]
        env = {k: v for k, v in os.environ.items()     # we are not a nested Claude Code session
               if not (k == "CLAUDECODE" or k == "CLAUDE_PID" or k.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")))}
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=900, cwd=str(AI_WORK_DIR), env=env,
                               creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise AIError("claude -p timed out after 15 minutes")
        out = (r.stdout or "").strip()
        if r.returncode != 0 and not out:
            raise AIError(f"claude -p failed (exit {r.returncode}): {(r.stderr or '').strip()[-800:]}")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return out                              # plain text reply: let the JSON extractor try
        if isinstance(data, list):                  # some versions emit a list of events
            data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
        if data.get("is_error"):
            msg = str(data.get("result"))
            if "not logged in" in msg.lower() or "/login" in msg:
                raise AIError("The claude CLI is not signed in (one-time setup). Open a terminal and run:\n"
                              f'  "{self.exe}"\nthen type /login and finish the browser sign-in. '
                              "Or switch AI mode to Gemini Free API. See SETUP_GUIDE.md section 4.")
            raise AIError(f"claude -p returned an error: {msg[:800]}")
        cost = data.get("total_cost_usd")
        if cost is not None:
            self.log(f"    (claude -p: {data.get('num_turns', 1)} turn, est. ${cost:.4f} of usage)")
        return str(data.get("result", ""))


class GeminiProvider:
    label = PROVIDERS["gemini"]

    def __init__(self, cfg: dict, log=print):
        key = (cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY") or "").strip()
        if not key:
            raise AIError("No Gemini API key. Paste your free Google AI Studio key in Settings "
                          "(or config.json -> gemini_api_key). See SETUP_GUIDE.md step 4.")
        from google import genai
        from google.genai import types
        self.types = types
        # 75 s limit per call and no hidden library retries: our own loop below decides
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(
            timeout=75_000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.model = cfg.get("gemini_model") or "gemini-3.5-flash"
        fb = cfg.get("gemini_fallback_models") or ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
        self.models = [self.model] + [m for m in fb if m != self.model]
        self.log = log

    # models that were overloaded recently are skipped for a while (shared by every video in the batch)
    _busy_until: dict = {}
    BUSY_COOLDOWN_S = 15 * 60

    def _order(self) -> list[str]:
        now = time.time()
        fresh = [m for m in self.models if self._busy_until.get(m, 0) <= now]
        busy = [m for m in self.models if m not in fresh]
        return fresh + busy                      # still try busy ones last, never give up early

    def generate(self, prompt: str, images: list[Path] | None = None) -> str:
        cfg = self.types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.4,
                                               response_mime_type="application/json")
        contents = prompt
        if images:                                   # frames of a text-only video, in time order
            contents = [prompt] + [self.types.Part.from_bytes(data=Path(p).read_bytes(), mime_type="image/jpeg")
                                   for p in images]
        last = None
        order = self._order()
        if order[0] != self.model:
            self.log(f"    ({self.model} was overloaded a few minutes ago - using {order[0]} first)")
        for model in order:                          # overloaded model -> next one in the list
            for attempt in range(2):
                try:
                    resp = self.client.models.generate_content(model=model, contents=contents, config=cfg)
                    if model != self.model:
                        self.log(f"    (answered by backup model {model})")
                    return resp.text or ""
                except Exception as e:              # free tier: 429 / 503 / 504 are common
                    msg = str(e)
                    last = e
                    if "404" in msg or "NOT_FOUND" in msg:
                        self.log(f"    Gemini model {model} is not available - trying the next one")
                        break
                    if any(s in msg for s in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500",
                                              "504", "DEADLINE", "deadline", "timed out", "Timeout")):
                        if attempt == 1:
                            self.log(f"    Gemini {model} still busy - switching to the backup model")
                            GeminiProvider._busy_until[model] = time.time() + self.BUSY_COOLDOWN_S
                            break
                        wait = 10
                        self.log(f"    Gemini {model} busy ({msg[:70]}...) - waiting {wait}s")
                        time.sleep(wait)
                        continue
                    raise AIError(f"Gemini error: {msg[:800]}")
        raise AIError(f"Gemini kept failing on every model ({', '.join(self.models)}): {str(last)[:300]}")


def make_provider(cfg: dict, key: str, log=print):
    if key == "claude_cli":
        return ClaudeCLIProvider(cfg, log)
    if key == "gemini":
        return GeminiProvider(cfg, log)
    raise AIError(f"Unknown AI provider {key!r}")


# ============================================================================ prompts
def _phrase_lines(phrases: list[dict]) -> str:
    rows = []
    for p in phrases:
        tag = "FLAG" if p.get("flagged") else ("tl" if p.get("tagalog") else "en")
        line = f"[{p['id']}] {tag} {p['text']}"
        if p.get("flagged") and p.get("unsure_words"):
            line += f"   (unsure: {', '.join(p['unsure_words'][:6])})"
        rows.append(line)
    return "\n".join(rows)


def _edits_rules(max_chars: int) -> str:
    return f"""=== (A) SUBTITLE EDITS ===
Return "edits": an object keyed by phrase id (as a string). Include ONLY phrases that need a change:
- a string value = the English subtitle for that phrase.
- null, or {{"text": null, "guess": "...", "note": "..."}} = leave it BLANK for human review. Use this when the
  source is unclear, garbled, or overlapping voices. NEVER invent content. "guess" and "note" are optional and
  are shown to the human reviewer.
- Leave out phrases that are already fine English (most "en" lines). Every "tl" line needs an English string
  (or null with a guess if it is unclear).
- Keep each English phrase about as short as the source phrase - one subtitle line, ideally under
  {max_chars} characters, never over 45.
- A phrase can be cut mid-sentence. Translate so consecutive phrases read naturally in order, e.g.
  [2] "What kind of women are courted and" [3] "married by men earning 3 million a month?"
Style (the creator's confirmed preferences):
- Translate close to the words actually said; keep the speaker's phrasing and tone. Do not paraphrase
  freely or "improve" the content.
- Natural English for international viewers, but keep local terms people can follow: Pinay, AFAM,
  pandesal, BGC, adobo, baby girl, lady boss.
- Usual mappings: kasi -> "because", diba -> "right?", talaga -> "really", sige -> "okay / go ahead",
  reto -> "being set up", katulong -> "maid", yaya -> "nanny", hugot -> "where is that coming from?",
  swerte mo -> "lucky you", talo ng malandi ang maganda -> "the flirt beats the pretty one".
- Fix obvious Whisper mishearings only when context makes them certain (e.g. "Books are appealing" ->
  "Looks are appealing"). If not certain, blank it with a "guess".
- The coach is "Coach Vee"."""


_META_RULES = f"""=== (B) YOUTUBE SHORTS METADATA ===
Write in Coach Vee's voice (Vanessa Antonio, dating coach and matchmaker at Matchmaking Manila): warm,
discerning, classy, high standards, intentional dating. It must match what is actually said in THIS video.
Return these fields - the app assembles the final title and description from them:
- "title_hook": 2-3 fast-to-read punchy words ending in "!" or "?" (e.g. "Stop Playing Cold!").
- "title_emojis": 1-2 emojis that fit the topic (e.g. "\U0001F6D1❄️").
- "title_hashtags": 4-5 niche lowercase hashtags (e.g. "#classyflirting"). Do NOT include #CoachVee.
  The finished title (hook + emojis + hashtags + #CoachVee) must stay under 100 characters, so prefer
  short hashtags; the app drops trailing hashtags if it is too long.
- "description_paragraph_1": 2-3 brief sentences explaining the video's lesson, including a direct
  conversational quote or example from (or matching) the video, in double quotes.
- "description_paragraph_2": a list of exactly 2 brief sentences encouraging intentional dating clarity
  and healthy boundaries.
- "description_hashtags": 7-9 relevant hashtags. Do NOT include #ManilaDating, #CoachVee or #VanessaAntonio.
- "seo_phrases": 4-5 lowercase long-tail search phrases for this topic (no commas inside a phrase).
  Do NOT include "matchmaking manila consultation".

Finished style reference (from a DIFFERENT video - do not copy its content):
TITLE: {enforce_title(EXAMPLE_TITLE)}
DESCRIPTION:
{EXAMPLE_DESCRIPTION}"""

_META_SHAPE = ('"metadata": {"title_hook": "...", "title_emojis": "...", "title_hashtags": ["#..."], '
               '"description_paragraph_1": "...", "description_paragraph_2": ["...", "..."], '
               '"description_hashtags": ["#..."], "seo_phrases": ["..."]}')


def combined_prompt(n: int, phrases: list[dict], max_chars: int) -> str:
    return f"""COACH VEE SHORTS JOB - Output {n}
Do two things for one short vertical video: (A) English subtitle edits and (B) YouTube Shorts metadata.

=== INPUT: TIMED PHRASES (Whisper transcript, Taglish) ===
Each line: [id] tag text   (tag: en = English, tl = contains Tagalog, FLAG = Whisper was unsure)
{_phrase_lines(phrases)}

{_edits_rules(max_chars)}

{_META_RULES}

=== OUTPUT ===
Return ONLY this JSON object:
{{"edits": {{"<id>": "..."}}, {_META_SHAPE}}}"""


def edits_prompt(n: int, phrases: list[dict], part: int, parts: int, max_chars: int) -> str:
    return f"""COACH VEE SUBTITLE JOB - Output {n}, part {part} of {parts}

=== INPUT: TIMED PHRASES (Whisper transcript, Taglish) ===
Each line: [id] tag text   (tag: en = English, tl = contains Tagalog, FLAG = Whisper was unsure)
{_phrase_lines(phrases)}

{_edits_rules(max_chars)}

=== OUTPUT ===
Return ONLY this JSON object: {{"edits": {{"<id>": "..."}}}}"""


def metadata_prompt(n: int, phrases: list[dict]) -> str:
    text = " ".join(p["text"] for p in phrases)
    return f"""COACH VEE METADATA JOB - Output {n}

=== INPUT: FULL TRANSCRIPT (Taglish) ===
{text[:20000]}

{_META_RULES}

=== OUTPUT ===
Return ONLY this JSON object: {{{_META_SHAPE}}}"""


# ============================================================================ validation
def extract_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        return json.loads(t[i:j + 1])
    raise ValueError("no JSON object found in the reply")


def validate_edits(obj, valid_ids: set[str], final: bool):
    e = obj.get("edits") if isinstance(obj, dict) else None
    if not isinstance(e, dict):
        return None, ["'edits' must be a JSON object keyed by phrase id"]
    clean, problems, unknown = {}, [], []
    for k, v in e.items():
        k = str(k).strip().strip("[]#")
        if k not in valid_ids:
            unknown.append(k)
            continue
        if v is None:
            clean[k] = None
        elif isinstance(v, str):
            v = " ".join(v.split())
            clean[k] = v or None
        elif isinstance(v, dict):
            text = v.get("text")
            if text is not None and not isinstance(text, str):
                problems.append(f"id {k}: 'text' must be a string or null")
                continue
            item = {"text": (" ".join(text.split()) or None) if isinstance(text, str) else None}
            for f in ("guess", "note"):
                if isinstance(v.get(f), str) and v[f].strip():
                    item[f] = v[f].strip()
            clean[k] = item if (item["text"] is None or len(item) > 1) else item["text"]
        else:
            problems.append(f"id {k}: value must be a string, null, or an object")
    if unknown and not final:
        problems.append(f"these keys are not phrase ids from the list: {', '.join(unknown[:12])}")
    return clean, problems


def validate_metadata(obj, final: bool):
    m = obj.get("metadata") if isinstance(obj, dict) else None
    if not isinstance(m, dict):
        return None, ["missing the 'metadata' object"]
    problems = []
    hook = " ".join(str(m.get("title_hook") or "").split())
    if not hook or len(hook) > 45 or len(hook.split()) > 6:
        problems.append("title_hook must be 2-3 short punchy words")
    if not str(m.get("description_paragraph_1") or "").strip():
        problems.append("description_paragraph_1 is empty")
    p2 = m.get("description_paragraph_2")
    if not p2 or (isinstance(p2, list) and not any(str(x).strip() for x in p2)):
        problems.append("description_paragraph_2 must be a list of 2 sentences")
    for f in ("title_hashtags", "description_hashtags", "seo_phrases"):
        v = m.get(f)
        if not isinstance(v, (list, str)) or not v:
            problems.append(f"{f} must be a non-empty list")
    return m, problems


def _ask(provider, prompt: str, validators, log, label: str, attempts: int = 3, images=None) -> dict:
    """Call the model, validate, and re-ask (this item only) with the reason when invalid."""
    feedback, last_err = "", ""
    for a in range(1, attempts + 1):
        final = a == attempts
        text = provider.generate(prompt + feedback, images) if images else provider.generate(prompt + feedback)
        try:
            obj = extract_json(text)
            result, problems = {}, []
            for name, fn in validators.items():
                val, probs = fn(obj, final)
                result[name] = val
                problems += probs
            if not problems:
                return result
            last_err = "; ".join(problems[:8])
        except (ValueError, json.JSONDecodeError) as e:
            last_err = f"invalid JSON ({e})"
        log(f"    {label}: reply {a} rejected - {last_err}" + ("" if final else " - asking again"))
        feedback = ("\n\n=== YOUR PREVIOUS REPLY WAS REJECTED ===\n"
                    f"Reason: {last_err}\nReturn the complete JSON object again with this fixed. JSON only.")
    raise AIError(f"{label}: no valid reply after {attempts} attempts ({last_err})")


# ============================================================================ text-only filler videos
def text_only_prompt(n: int) -> str:
    return f"""COACH VEE TEXT-ONLY SHORT - Output {n}
The images are frames, in time order, from one short vertical video by Coach Vee. It has ON-SCREEN TEXT
(and maybe music) but nobody talks. Do two things:
1. "onscreen_text": write out all the on-screen text in reading order, each piece once (consecutive frames
   repeat the same text - do not repeat it). Keep the original wording; if it is Tagalog/Taglish, add an
   English translation in brackets after it. Ignore watermarks, usernames and app/UI labels.
2. "metadata": YouTube Shorts metadata based on that on-screen message.

{_META_RULES.replace("from (or matching) the video", "from (or matching) the on-screen text")}

=== OUTPUT ===
Return ONLY this JSON object: {{"onscreen_text": "...", {_META_SHAPE}}}"""


def extract_frames(cfg: dict, video: Path, out_dir: Path, duration: float | None, max_frames: int = 16) -> list[Path]:
    """Evenly spaced stills (540 px wide JPEG) with ffmpeg - enough to read every text card."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("f_*.jpg"):
        old.unlink()
    dur = duration or 60.0
    fps = max(max_frames / max(dur, 1.0), 1 / 6)          # at least one frame every 6 s
    r = subprocess.run([cfg.get("ffmpeg_path") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
                        "-vf", f"fps={fps:.4f},scale=540:-2", "-frames:v", str(max_frames), "-q:v", "4",
                        str(out_dir / "f_%03d.jpg")], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    frames = sorted(out_dir.glob("f_*.jpg"))
    if not frames:
        raise AIError(f"ffmpeg could not read frames from {video.name}: {r.stderr[-400:]}")
    return frames


def generate_text_only(cfg: dict, n: int, st: dict, provider, log=print) -> dict:
    if not isinstance(provider, GeminiProvider):
        try:
            provider = GeminiProvider(cfg, log)
            log(f"  Output {n}: text-only video - using Gemini to read the on-screen text (Claude CLI can't see images)")
        except AIError:
            raise AIError(f"Output {n} is text-only: reading on-screen text needs the free Gemini key "
                          f"(Settings -> Gemini API key), or type the title/description by hand.")
    frames = extract_frames(cfg, st["video"], st["video"].parent / "frames", st["type_info"].get("duration"))
    log(f"  Output {n}: text-only - reading on-screen text from {len(frames)} frames ({provider.label}) ...")

    def v_text(obj, final):
        t = obj.get("onscreen_text") if isinstance(obj, dict) else None
        ok = isinstance(t, str) and t.strip()
        return (t or "").strip(), ([] if ok or final else ["'onscreen_text' is empty"])

    res = _ask(provider, text_only_prompt(n), {"onscreen_text": v_text, "metadata": validate_metadata}, log,
               f"Output {n}", images=frames)
    fields = res["metadata"]
    title = build_title(fields.get("title_hook", ""), fields.get("title_emojis", ""), fields.get("title_hashtags", []))
    desc = build_description(fields.get("description_paragraph_1", ""), fields.get("description_paragraph_2", []),
                             fields.get("description_hashtags", []), fields.get("seo_phrases", []))
    mp = metadata_path(n)
    if mp.exists():
        shutil.copy2(mp, mp.with_suffix(".prev.json"))
    write_json_atomic(mp, {
        "output": n, "title": title, "description": desc, "fields": fields, "video_type": "text_only",
        "onscreen_text": res["onscreen_text"], "provider": provider.label, "model": getattr(provider, "model", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"), "edited_by_user": False,
    })
    (st["video"].parent / "onscreen_text.txt").write_text(res["onscreen_text"], encoding="utf-8")
    log(f"  Output {n}: on-screen text read ({len(res['onscreen_text'])} chars); title {len(title)} chars")
    return {"skipped": False, "title": title, "text_only": True}


# ============================================================================ per-video job
def generate_for_output(cfg: dict, state: dict, n: int, provider, runner, log=print, force: bool = False) -> dict:
    st = output_status(cfg, state, n)
    if st["text_only"]:
        if st["metadata"] and not force:
            log(f"  Output {n}: text-only video already has a title/description - skipping")
            return {"skipped": True}
        return generate_text_only(cfg, n, st, provider, log)
    if not st["transcribed"]:
        raise AIError(f"Output {n} has no phrases.json yet - run Step 1 first")
    wdir: Path = st["wdir"]
    if st["translated"] and st["metadata"] and not force:
        log(f"  Output {n}: already translated + has title/description - skipping (use Regenerate to redo)")
        if not st["srt"]:
            runner.merge_and_build(n, wdir)
        return {"skipped": True}

    phrases = (read_json_file(wdir / "phrases.json", {}) or {}).get("segments", [])
    if not phrases:
        raise AIError(f"Output {n}: Whisper found no speech; write the subtitles/title by hand")
    valid_ids = {str(p["id"]) for p in phrases}
    max_chars = int(cfg.get("srt_max_chars", 30))
    size = max(50, int(cfg.get("ai_batch_phrases", 350)))
    chunks = [phrases[i:i + size] for i in range(0, len(phrases), size)]
    ev = lambda obj, final: validate_edits(obj, valid_ids, final)   # noqa: E731

    log(f"  Output {n}: asking {provider.label} ({len(phrases)} phrases) ...")
    if len(chunks) == 1:
        res = _ask(provider, combined_prompt(n, phrases, max_chars),
                   {"edits": ev, "metadata": validate_metadata}, log, f"Output {n}")
        edits_list, fields = [res["edits"]], res["metadata"]
    else:
        edits_list = []
        for i, ch in enumerate(chunks, 1):
            r = _ask(provider, edits_prompt(n, ch, i, len(chunks), max_chars), {"edits": ev}, log,
                     f"Output {n} part {i}")
            edits_list.append(r["edits"])
        fields = _ask(provider, metadata_prompt(n, phrases), {"metadata": validate_metadata}, log,
                      f"Output {n} metadata")["metadata"]

    # ---- write edits/001.json ... (older AI edits are backed up, human zz_review.json is kept)
    edits_dir = wdir / "edits"
    edits_dir.mkdir(exist_ok=True)
    old = [p for p in edits_dir.iterdir() if AI_EDIT_RE.match(p.name)]
    if old:
        bk = wdir / "edits_backup" / datetime.now().strftime("%Y%m%d_%H%M%S")
        bk.mkdir(parents=True, exist_ok=True)
        for p in old:
            shutil.move(str(p), str(bk / p.name))
    for i, e in enumerate(edits_list, 1):
        write_json_atomic(edits_dir / f"{i:03}.json", e)
    n_blank = sum(1 for e in edits_list for v in e.values()
                  if v is None or (isinstance(v, dict) and v.get("text") is None))

    # ---- metadata_Output_<N>.json (title/description assembled + validated in Python)
    title = build_title(fields.get("title_hook", ""), fields.get("title_emojis", ""), fields.get("title_hashtags", []))
    desc = build_description(fields.get("description_paragraph_1", ""), fields.get("description_paragraph_2", []),
                             fields.get("description_hashtags", []), fields.get("seo_phrases", []))
    mp = metadata_path(n)
    if mp.exists():
        shutil.copy2(mp, mp.with_suffix(".prev.json"))
    write_json_atomic(mp, {
        "output": n, "title": title, "description": desc, "fields": fields,
        "provider": provider.label, "model": getattr(provider, "model", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"), "edited_by_user": False,
    })
    log(f"  Output {n}: {sum(len(e) for e in edits_list)} subtitle edits ({n_blank} blank for review); "
        f"title {len(title)} chars")

    # ---- existing pipeline: merge_edits.py + build_srt.py
    built = runner.merge_and_build(n, wdir)
    log(f"  Output {n}: {built['summary'] or built['srt']}")
    return {"skipped": False, "title": title, **built}
