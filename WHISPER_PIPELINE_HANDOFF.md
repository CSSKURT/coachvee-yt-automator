# Handoff: Local Whisper subtitle pipeline → automated Google Drive → YouTube bot

You are helping me build an automation bot. I already have a **working, tested local pipeline** that turns a Taglish (Tagalog + English) audio file into an English `.srt` subtitle file. I want you to **reuse this pipeline as-is** and build the automation *around* it. Do not rewrite the transcription or subtitle logic unless I ask; call the existing scripts.

---

## 1. Goal of the bot

1. Watch a Google Drive folder for new videos (or audio).
2. Download each new file to the PC.
3. Extract the audio.
4. Run my existing Whisper pipeline to produce timed phrases.
5. Translate the Tagalog/Taglish phrases into English with an LLM (this step is currently done by hand with an AI assistant; it needs to be automated).
6. Build the English `.srt` with my existing builder.
7. **Pause for human review** (strongly recommended, see section 8).
8. Upload the video to YouTube and attach the English `.srt` as captions.
9. Log what happened and move the Drive file to a "done" folder.

---

## 2. Machine and environment (fixed facts)

| Item | Value |
|---|---|
| OS | Windows 11 Pro |
| CPU / RAM | Intel i5-14400F, 32 GB |
| GPU | **Intel Arc B580, 12 GB** (NOT NVIDIA; no CUDA) |
| Python | 3.11, in a venv at `D:\whisper\venv` |
| Python executable | `D:\whisper\venv\Scripts\python.exe` |
| PyTorch | `2.15.0+xpu` (Intel GPU build; device name is `"xpu"`) |
| Whisper | `openai-whisper` (runs on the GPU) and `faster-whisper 1.2.1` (CPU fallback + voice detection) |
| Model | `large-v3`, cached in `D:\whisper\models\openai-whisper\` |
| ffmpeg | `D:\ytdl\ffmpeg.exe` (not on system PATH; scripts add it themselves) |
| Scripts folder | `D:\whisper\` |

Important GPU notes:
- The model is loaded in fp16 **except LayerNorm layers, which must stay fp32**. This is already handled in `transcribe.py`. Do not change it.
- Do not run two transcriptions at the same time. The 12 GB VRAM plus Discord/Firefox/CapCut usage can overflow and slow to a crawl or crash.
- Measured speed with `large-v3` on the GPU: about **0.4–0.6 minutes of processing per minute of audio** (21 min audio ≈ 12.6 min, 52 min audio ≈ 21.5 min).

---

## 3. The pipeline: 4 scripts, run in order

All are command-line scripts. Always call them with the venv Python.

### Step 1: Transcribe
```
D:\whisper\venv\Scripts\python.exe D:\whisper\transcribe.py "<audio file>" --model large-v3 --language tl
```
- `--language tl` is important for Taglish. English words still come out in English. (`auto` can misdetect.)
- Optional: `--out "<folder>"`. Default output folder is `<audio filename without extension>_whisper` next to the audio file.
- Optional: `--engine auto|gpu|cpu` (auto uses the Intel GPU if available).
- Accepts `.mp3`, `.wav`, `.mp4`, etc. (ffmpeg decodes it).
- Prints `Done in X min: N segments, M flagged.` and `Output folder: <path>` when finished. Exit code 0 on success.

Writes into the output folder:
- `segments.json`: Whisper segments with start/end times, text, word-level times, confidence scores, flags.
- `original.srt`: raw untranslated transcript.
- `transcript_review.txt`: human-readable segment list.

### Step 2: Split into timed phrases
```
D:\whisper\venv\Scripts\python.exe D:\whisper\make_phrases.py "<output folder>"
```
Cuts each Whisper segment into short phrases (≤ ~42 characters of source text) at punctuation, pauses (≥ 0.4 s) and natural break words. Each phrase keeps the exact start time of its first word and end time of its last word. **Translation is done per phrase, so subtitle timing stays correct even when English word order differs from Tagalog.**

Writes:
- `phrases.json`: the phrase list (this is what gets translated). Example entry:
  ```json
  {
    "id": 1, "segment": 1, "start": 0.0, "end": 1.24,
    "text": "Sabi ng study,",
    "words": [[0.0, 0.3, "Sabi"], [0.3, 0.5, "ng"], [0.5, 1.24, "study,"]],
    "tagalog": true, "flagged": false, "flag_reasons": [], "unsure_words": []
  }
  ```
- `phrase_review.txt`: one line per phrase, e.g. `[12] 00:00:37.7 tl   Coach, mag-relocate ako?` where the tag is `en`, `tl` (contains Tagalog) or `FLAG` (Whisper had low confidence).

### Step 3: Translate (THIS IS THE STEP TO AUTOMATE WITH AN LLM)
The translation is stored as **edit files** in `<output folder>\edits\*.json` (any filenames, e.g. `001.json`, `002.json`; merged in sorted order). Each edit file is a JSON object keyed by **phrase id as a string**:

```json
{
  "1": "What does the study say?",
  "2": "What kind of women are courted and",
  "3": "married by men earning 3 million a month?",
  "110": null,
  "169": {"text": null, "guess": "you're actually the one who's closed off.", "note": "unclear: 'kasi ikaw din pala yung IHO'"}
}
```

Rules:
- **Only include phrases that need a change.** Any phrase id that is not mentioned keeps Whisper's original text (most English phrases need no edit).
- A **string** value = the English subtitle text for that phrase.
- `null` or `{"text": null, ...}` = leave this subtitle **blank** (the phrase goes to the human review list). Use this when the source is unclear or garbled; **never invent content**. `guess` and `note` are optional and are shown to the human reviewer.
- Keep each English phrase roughly as short as the source phrase. The builder splits anything over 45 characters, but shorter is better because each phrase is one subtitle line.

Translation style (my confirmed preferences):
- Translate **close to the words actually said**; keep the speaker's phrasing and tone. Don't paraphrase freely or "improve" the content.
- Natural English for international viewers, but keep local terms people can follow (e.g. *pandesal*, *BGC*, *Pinay*, *AFAM*, *adobo*, *baby girl*, *lady boss*).
- Common mappings used so far: *kasi* → "because", *diba* → "right?", *talaga* → "really", *sige* → "okay / go ahead", *reto* → "being set up", *katulong* → "maid", *yaya* → "nanny", *hugot* → "where is that coming from?", *swerte mo* → "lucky you", *talo ng malandi ang maganda* → "the flirt beats the pretty one".
- Fix obvious Whisper mishearings only when context makes them certain (e.g. "Books are appealing" → "Looks are appealing"). If not certain, blank it with a `guess`.
- A phrase can be split mid-sentence. Translate it so the pieces read naturally in order (e.g. phrase A "What kind of women are courted and", phrase B "married by men earning 3 million a month?").

To automate: send the LLM batches of ~300–400 lines from `phrase_review.txt` (or the `id` + `text` from `phrases.json`) and have it return **only** a JSON object in the format above. Validate that it is valid JSON and that every key is an existing phrase id. Save each batch as `edits\001.json`, `edits\002.json`, …

Then merge:
```
D:\whisper\venv\Scripts\python.exe D:\whisper\merge_edits.py "<output folder>"
```
Writes `translations.json` (all phrase ids → final English). It also automatically applies spelling fixes for recurring names (e.g. "Coach V"/"Coach B" → "Coach Vee", "Ma'am Larry" → "Ma'am Lerrie"). The list is `NAME_FIXES` near the top of `merge_edits.py`; add new names there.

### Step 4: Build the SRT
```
D:\whisper\venv\Scripts\python.exe D:\whisper\build_srt.py "<output folder>" --name <base name>
```
- Saves as `<base name>_v1.srt`, then `_v2`, `_v3`… on later builds. **It never overwrites an existing version.** Pick the highest version number as the latest.
- `--max-chars 45` is the default (16:9 landscape). Use `--max-chars 30` for 9:16 vertical videos.
- Subtitle style: **one line per subtitle, max 45 characters**, whole sentences kept together when they fit, never ends a line on a lead-in word ("to", "the", "would"…), back-to-back timing.
- Timing: the first build runs voice detection (Silero VAD) on the source audio and caches it as `speech.json`. Subtitle starts and ends are snapped to where the voice actually is, because Whisper's word times drift into silences. **The original audio file must still exist at the path recorded in `segments.json` → `"source"`**, or snapping is skipped.

Also writes:
- `needs_review.txt` / `needs_review.json`: every blank subtitle with its timestamp, what Whisper heard, the guess and the reason.
- `tagalog_check.txt`: every subtitle that came from Tagalog, next to what Whisper heard, for quick spot-checking.

Prints e.g. `m3_english_v1.srt: 680 cues | 331 re-timed to the voice | needs_review: 5 blank parts`.

---

## 4. Output folder contents (after a full run)

```
<name>_whisper\
  segments.json           Whisper output (step 1)
  original.srt            raw transcript, untranslated
  transcript_review.txt   human-readable segments
  phrases.json            timed phrases (step 2) – translation is keyed to these ids
  phrase_review.txt       human-readable phrases, tagged en/tl/FLAG
  edits\001.json …        translation batches (step 3)
  translations.json       merged final text per phrase
  speech.json             voice-activity cache (step 4)
  <name>_v1.srt           FINAL English subtitles  ← upload this
  needs_review.txt/.json  blank parts for a human to fill
  tagalog_check.txt       Tagalog-sourced lines to spot-check
```

---

## 5. Suggested bot architecture

Language: Python (it can reuse the same venv, or use its own venv that calls the scripts via `subprocess`). Run on this Windows PC, because it needs the Intel GPU.

1. **Drive watcher.** Use the Google Drive API v3 with OAuth for my Google account. Poll an "Inbox" folder every N minutes with `files.list` (`'<folderId>' in parents and trashed = false`). Track processed file IDs in a local SQLite/JSON file so nothing runs twice.
2. **Download** the file to e.g. `D:\bot\jobs\<driveFileId>\video.mp4`.
3. **Extract audio:** `D:\ytdl\ffmpeg.exe -y -i video.mp4 -vn -ac 1 -ar 16000 audio.wav` (or skip this; `transcribe.py` can read the video directly).
4. **Run steps 1–2** with `subprocess.run([...], check=True)`. Process one job at a time (GPU limit).
5. **LLM translation (step 3).** Batch `phrases.json`, call the LLM with the rules in section 3, validate the JSON, and write `edits\NNN.json`. Retry on invalid JSON. Then run `merge_edits.py`.
6. **Build (step 4)**, then find the newest `*_vN.srt`.
7. **Review gate (recommended).** Notify me (email/Telegram/Discord webhook) with `needs_review.txt` and `tagalog_check.txt`, and wait for approval. I may edit `edits\*.json` and rebuild, which creates a new `_vN`.
8. **YouTube upload.** Use the YouTube Data API v3 with OAuth for the channel owner:
   - `videos.insert` (resumable upload) with title/description/tags/privacy. Start as **private or unlisted**, not public.
   - `captions.insert` with `snippet.videoId`, `snippet.language = "en"`, `snippet.name = "English"`, and the `.srt` as the media body.
   - Check the current API quota costs. Uploads use a large share of the default daily quota, so a handful of videos per day may be the limit.
9. **Finish:** move the Drive file to a "Done" folder (`files.update` with `addParents`/`removeParents`), and log the YouTube URL, SRT version and review counts.

Credentials: store OAuth client secrets and tokens outside the code (e.g. `D:\bot\secrets\`, not in git). Use the narrowest scopes needed (Drive read/modify for those folders; `youtube.upload` plus `youtube.force-ssl` for captions).

---

## 6. Error handling to include

- `transcribe.py` non-zero exit → retry once with `--engine cpu` (much slower) or mark the job failed and notify.
- "out of memory" from PyTorch → another GPU job or app is using VRAM; wait and retry, and never run jobs in parallel.
- LLM returns invalid JSON or unknown ids → re-ask for that batch only.
- `build_srt.py` prints "speech detection skipped" → the source audio path moved; keep the audio until the build is done.
- If a folder already has `phrases.json` and `edits\`, don't re-transcribe; resume from the next step.

---

## 7. Things NOT to change

- The fp16 + fp32-LayerNorm loading in `transcribe.py` (needed for the Intel Arc GPU).
- `condition_on_previous_text=True` and the Taglish `initial_prompt` in `transcribe.py`. They were tested and give the cleanest Taglish sentences.
- Phrase splitting and per-phrase translation. This is what fixed the subtitle timing.
- Voice snapping in `build_srt.py`. This fixed lines appearing before the speaker talks.
- The versioned output names (`_v1`, `_v2`, …).

Note: the drag-and-drop file `D:\whisper\Transcribe (drag audio here).bat` does not pass `--language tl`. The bot should call `transcribe.py` directly with `--language tl`.

---

## 8. Known limitations (why a human review step is recommended)

- **Whisper sometimes mishears Tagalog**, e.g. it hears "Sabi ng study" when the speaker said "Ano ibig sabihin ng study". No setting fixed this, so the translation can only be as good as what Whisper heard. `tagalog_check.txt` exists for this.
- **Names are often misheard** (one person came out as "Jot", "George", "Josh", "Joy" and "Judge" when the real name is "Joj"). Add confirmed names to `NAME_FIXES` in `merge_edits.py`.
- **Overlapping speakers and crowd reactions** come out garbled; those become blanks in `needs_review.txt`.
- **Timing:** after voice snapping, typically fewer than 1–3% of lines start noticeably early, mostly during fast back-and-forth between speakers.

---

## 9. What I want from you (Gemini)

1. Confirm you understand the pipeline above, and ask me anything unclear before writing code.
2. Propose the bot's folder structure and config file (Drive folder IDs, YouTube defaults, notification method, which LLM to use for translation).
3. Write the bot in Python step by step, starting with the Drive watcher and job runner that calls the existing scripts. Then add the LLM translation step, then YouTube upload with captions.
4. Keep the existing `D:\whisper\` scripts unchanged; the bot only calls them.
