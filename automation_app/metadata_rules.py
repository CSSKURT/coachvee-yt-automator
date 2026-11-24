"""
Coach Vee title/description rules, enforced in plain Python (0% AI).

The AI only returns the creative pieces (hook, emojis, hashtags, two paragraphs, SEO phrases).
This module assembles them into the exact required structure and validates/trims the title,
so the format can never drift no matter what the model returns.
"""
from __future__ import annotations

import re

CTA = "Book your Matchmaking Consultation today at www.matchmakingmanila.com/consult"
FIXED_DESC_TAGS = ["#ManilaDating", "#CoachVee", "#VanessaAntonio"]
FALLBACK_DESC_TAGS = ["#datingadvice", "#relationshiptips", "#matchmakingmanila", "#datingmindset",
                      "#highstandards", "#intentionaldating", "#filipinadating", "#highvaluesingles"]
SEO_TAIL = "matchmaking manila consultation"
FALLBACK_SEO = ["coach vee dating advice", "dating tips for filipina women", "intentional dating manila",
                "how to date with high standards"]
TITLE_TAG = "#CoachVee"
TITLE_MAX = 100                      # hard rule: len(title) < 100
HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)

EXAMPLE_DESCRIPTION = (
    "Showing interest in a classy way starts with emotional honesty and grounded discernment. "
    "You can acknowledge mutual attraction without prematurely crowning him as your partner: "
    "\"Yes, I'm starting to like you and I enjoy getting to know you. Does it mean you're definitely "
    "my person yet? We'll see—I want to see more of this consistency.\"\n\n"
    "Give him the warmth and green light to pursue, while reserving your commitment until his "
    "character and actions prove he is worthy of it.\n"
    "Stop second-guessing your romantic approach and date with clarity.\n\n"
    f"{CTA}\n\n\n\n"
    "#datingadvice #relationshiptips #matchmakingmanila #classyflirting #emotionalhonesty "
    "#datingmindset #communication #highstandards #highvaluesingles #ManilaDating #CoachVee #VanessaAntonio\n"
    "how to show interest in a classy way coach vee, playing hard to get the wrong way dating advice, "
    "filipina dating tips emotional honesty, how to flirt without chasing men, "
    "intentional dating communication manila, matchmaking manila consultation"
)
EXAMPLE_TITLE = ("Stop Playing Cold! \U0001F6D1❄️#datingadvice #relationshiptips #classyflirting "
                 "#emotionalhonesty #hardtoget #CoachVee")


def _clean_text(s: str) -> str:
    """YouTube rejects '<' and '>' in titles and descriptions."""
    return (s or "").replace("<", "‹").replace(">", "›")


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def norm_tag(t) -> str | None:
    t = re.sub(r"[^\w]", "", str(t).strip().lstrip("#"), flags=re.UNICODE)
    return f"#{t}" if t else None


def _dedupe(tags, exclude=()) -> list[str]:
    seen = {x.lower() for x in exclude}
    out = []
    for t in tags:
        t = norm_tag(t)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        return v.replace(",", " ").split()
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return []


# ----------------------------------------------------------------------------- title
def title_ok(title: str) -> bool:
    return len(title) < TITLE_MAX and utf16_len(title) <= TITLE_MAX and TITLE_TAG in title


def enforce_title(title: str) -> str:
    """Make any title obey the rules: #CoachVee present and last, len < 100.

    Trimming drops the LAST non-#CoachVee hashtag, one at a time, until it fits.
    """
    t = " ".join(_clean_text(title).split())
    t = re.sub(r"\s*#coachvee\b", "", t, flags=re.I).rstrip()      # remove, then re-add at the end
    t = f"{t} {TITLE_TAG}" if t else TITLE_TAG
    while not title_ok(t):
        tags = [m for m in HASHTAG_RE.finditer(t) if m.group(0) != TITLE_TAG]
        if not tags:
            break
        m = tags[-1]
        t = (t[:m.start()].rstrip() + " " + t[m.end():].lstrip()).strip()
    if not title_ok(t):                                           # no hashtags left: shorten the hook
        head = t[: -len(TITLE_TAG)].rstrip()
        while head and not title_ok(f"{head} {TITLE_TAG}"):
            head = head[:-1].rstrip()
        t = f"{head} {TITLE_TAG}".strip()
    return t


def build_title(hook: str, emojis: str, hashtags) -> str:
    """'[Hook!] [emojis][#tag1 #tag2 ...] #CoachVee', e.g. the EXAMPLE_TITLE layout."""
    hook = " ".join(_clean_text(hook).split()).strip()
    emojis = "".join(_clean_text(emojis).split())
    tags = _dedupe(_as_list(hashtags), exclude=[TITLE_TAG])[:5]
    head = f"{hook} {emojis}".strip() if emojis else hook
    joiner = "" if emojis else " "
    title = f"{head}{joiner}{' '.join(tags + [TITLE_TAG])}"
    return enforce_title(title)


def title_report(title: str) -> tuple[str, bool]:
    """Text for the live counter badge, and whether the title passes."""
    n = len(title)
    has = TITLE_TAG in title
    ok = title_ok(title)
    parts = [f"{n}/{TITLE_MAX} chars"]
    parts.append("✅ #CoachVee present" if has else "❌ #CoachVee missing")
    if n >= TITLE_MAX:
        parts.append("❌ too long")
    return " — ".join(parts), ok


# ----------------------------------------------------------------------------- description
def _sentences(v) -> list[str]:
    if isinstance(v, list):
        return [" ".join(str(x).split()) for x in v if str(x).strip()]
    s = " ".join(str(v or "").split())
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", s) if x.strip()]


def build_description(p1, p2, topic_tags, seo_phrases) -> str:
    para1 = " ".join(_sentences(p1))
    para2 = "\n".join(_sentences(p2)[:2])
    tags = _dedupe(_as_list(topic_tags), exclude=FIXED_DESC_TAGS)[:9]
    for t in FALLBACK_DESC_TAGS:                          # 10-12 hashtags in total
        if len(tags) + len(FIXED_DESC_TAGS) >= 10:
            break
        if t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    block = " ".join(tags + FIXED_DESC_TAGS)
    seo = []
    for p in (seo_phrases if isinstance(seo_phrases, list) else str(seo_phrases or "").split(",")):
        p = " ".join(str(p).replace(",", " ").split()).lower().strip(" .")
        if p and p != SEO_TAIL and p not in seo:
            seo.append(p)
    seo = seo[:5]
    for p in FALLBACK_SEO:                                # 5-6 phrases in total
        if len(seo) >= 4:
            break
        if p not in seo:
            seo.append(p)
    footer = ", ".join(seo + [SEO_TAIL])
    return _clean_text(f"{para1}\n\n{para2}\n\n{CTA}\n\n\n\n{block}\n{footer}")


def description_problems(desc: str) -> list[str]:
    """Soft checks shown in the GUI for hand-edited descriptions."""
    probs = []
    if CTA not in desc:
        probs.append("call-to-action line is missing or changed")
    if not re.search(r"#ManilaDating #CoachVee #VanessaAntonio\s*(\n|$)", desc):
        probs.append("hashtag block should end with #ManilaDating #CoachVee #VanessaAntonio")
    lines = [l for l in desc.strip().splitlines() if l.strip()]
    if not lines or not lines[-1].strip().endswith(SEO_TAIL):
        probs.append(f"SEO line should end with '{SEO_TAIL}'")
    if len(desc.encode("utf-8")) > 5000:
        probs.append("longer than YouTube's 5000-byte limit")
    if "<" in desc or ">" in desc:
        probs.append("contains < or > (YouTube rejects these; they become ‹ › on upload)")
    return probs


def clean_description(desc: str) -> str:
    return _clean_text(desc).strip()


def youtube_tags(description: str, limit_chars: int = 450) -> list[str]:
    """snippet.tags from the description hashtags (without '#'), within YouTube's 500-char cap."""
    out, total = [], 0
    for t in HASHTAG_RE.findall(description):
        w = t[1:]
        if w.lower() in {x.lower() for x in out}:
            continue
        if total + len(w) + 1 > limit_chars:
            break
        out.append(w)
        total += len(w) + 1
    return out
