"""
Daily Islamic reminder bot for Telegram.

Each run sends one Quran ayah (Arabic + English translation) and one
authentic hadith (Sahih al-Bukhari or Sahih Muslim), each followed by a
short AI reflection on how to apply it in daily life.

The Quran text, translation and hadith text are copied verbatim from the
data sources below. The AI only writes the reflection, and it is given
the tafsir (Ibn Kathir) as grounding.

Data (free, no API key, served from GitHub):
  - Quran Arabic + translation: github.com/fawazahmed0/quran-api
  - Tafsir Ibn Kathir (English): github.com/spa5k/tafsir_api
  - Hadith: github.com/fawazahmed0/hadith-api

Environment variables:
  TELEGRAM_BOT_TOKEN  from @BotFather
  TELEGRAM_CHAT_ID    your chat id
  GEMINI_API_KEY      from aistudio.google.com (free tier)
  GEMINI_MODEL        optional, default "gemini-flash-latest"
  DRY_RUN=1           optional: print the message instead of sending it,
                      and skip the AI call if GEMINI_API_KEY is unset
"""

import html
import json
import os
import random
import urllib.request

RAW = "https://raw.githubusercontent.com"
QURAN_TRANSLATION = f"{RAW}/fawazahmed0/quran-api/1/editions/eng-ummmuhammad.json"  # Saheeh International
QURAN_ARABIC = f"{RAW}/fawazahmed0/quran-api/1/editions/ara-quranuthmanihaf.json"
TAFSIR = RAW + "/spa5k/tafsir_api/main/tafsir/en-tafisr-ibn-kathir/{s}/{a}.json"
# Only the "books" (chapters) about character, the heart and remembrance, so the
# pool is full of everyday-life hadith rather than legal or historical narrations.
HADITH_COLLECTIONS = {
    "bukhari": ("Sahih al-Bukhari", f"{RAW}/fawazahmed0/hadith-api/1/editions/eng-bukhari.json",
                {2, 78, 80, 81}),           # Belief, Good Manners, Invocations, Heart-Softening
    "muslim": ("Sahih Muslim", f"{RAW}/fawazahmed0/hadith-api/1/editions/eng-muslim.json",
               {1, 38, 45, 48, 49, 50, 55}),  # Faith, Manners, Virtue & Kinship, Remembrance,
                                              # Heart-Melting, Repentance, Zuhd
}
HADITH_CANDIDATES = 8          # the AI picks the most practical one from these
MAX_HADITH_CHARS = 1200        # skip very long narrations
MAX_TAFSIR_CHARS = 6000        # keep the prompt small


def get_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- data ----------

def pick_ayah():
    translation = get_json(QURAN_TRANSLATION)["quran"]
    arabic = get_json(QURAN_ARABIC)["quran"]
    i = random.randrange(len(translation))
    s, a = translation[i]["chapter"], translation[i]["verse"]
    try:
        tafsir = get_json(TAFSIR.format(s=s, a=a))["text"][:MAX_TAFSIR_CHARS]
    except Exception:
        tafsir = ""
    return {
        "surah": s,
        "ayah": a,
        "arabic": arabic[i]["text"],
        "translation": translation[i]["text"],
        "tafsir": tafsir,
    }


def hadith_candidates():
    key = random.choice(list(HADITH_COLLECTIONS))
    name, url, books = HADITH_COLLECTIONS[key]
    fragment_markers = ("narrated this hadith", "this hadith has been", "same chain",
                        "chain of transmitters", "with a slight variation", "with the addition",
                        "similar hadith", "like this hadith", "the same hadith")
    hadiths = [
        h for h in get_json(url)["hadiths"]
        if h["reference"]["book"] in books
        and 120 < len(h["text"].strip()) <= MAX_HADITH_CHARS
        and not any(m in h["text"].lower() for m in fragment_markers)
    ]
    picks = random.sample(hadiths, HADITH_CANDIDATES)
    out = []
    for h in picks:
        if key == "bukhari":
            ref = f"Sahih al-Bukhari {h['hadithnumber']}"
            link = f"https://sunnah.com/bukhari:{h['hadithnumber']}"
        else:
            ref = f"Sahih Muslim, Book {h['reference']['book']}, Hadith {h['reference']['hadith']}"
            link = None
        out.append({"collection": name, "ref": ref, "link": link, "text": h["text"].strip()})
    return out


# ---------- AI ----------

PROMPT = """You write short, practical daily Islamic reminders.

You are given one Quran ayah (with its Ibn Kathir tafsir) and a numbered list
of authentic hadith. Do the following:

1. Choose the ONE hadith from the list that is most clearly applicable to
   everyday life (character, worship, family, work, speech, kindness, etc.).
   Avoid narrations that are mainly historical events or detailed legal rulings.
2. For the ayah: write 2-4 sentences on what it means (grounded in the tafsir
   provided, don't invent claims) and 2-3 concrete ways to apply it today.
3. For the chosen hadith: write 2-4 sentences on its lesson and 2-3 concrete
   ways to apply it today.

Rules:
- Do NOT quote, re-translate or paraphrase the ayah or hadith text itself;
  it is shown to the reader separately.
- No fatwas or rulings. Keep it humble, warm and practical. Plain text, no markdown.

Return JSON only:
{{"hadith_index": <number>, "ayah_reflection": "...", "hadith_reflection": "..."}}

AYAH (Surah {surah}, Ayah {ayah}):
{translation}

TAFSIR IBN KATHIR (may cover neighbouring ayat too):
{tafsir}

HADITH LIST:
{hadith_list}
"""


def reflect(ayah, hadiths):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"hadith_index": 0, "ayah_reflection": "(AI skipped: no GEMINI_API_KEY)",
                "hadith_reflection": "(AI skipped: no GEMINI_API_KEY)"}
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    hadith_list = "\n\n".join(f"[{i}] ({h['ref']}) {h['text']}" for i, h in enumerate(hadiths))
    prompt = PROMPT.format(hadith_list=hadith_list, **ayah)
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6},
    }).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    resp = get_json(url, data=body, headers={
        "Content-Type": "application/json", "x-goog-api-key": api_key})
    text = resp["candidates"][0]["content"]["parts"][0]["text"]
    result = json.loads(text)
    idx = result.get("hadith_index", 0)
    if not isinstance(idx, int) or not 0 <= idx < len(hadiths):
        result["hadith_index"] = 0
    return result


# ---------- Telegram ----------

def build_messages(ayah, hadith, r):
    e = lambda s: html.escape(s, quote=False)
    quran_url = f"https://quran.com/{ayah['surah']}/{ayah['ayah']}"
    msg1 = (
        f"📖 <b>Ayah of the day</b> — <a href=\"{quran_url}\">Surah {ayah['surah']}:{ayah['ayah']}</a>\n\n"
        f"{e(ayah['arabic'])}\n\n"
        f"<i>{e(ayah['translation'])}</i>\n"
        f"— Saheeh International\n\n"
        f"💡 <b>Reflection</b>\n{e(r['ayah_reflection'])}"
    )
    ref = f"<a href=\"{hadith['link']}\">{e(hadith['ref'])}</a>" if hadith["link"] else e(hadith["ref"])
    msg2 = (
        f"🕌 <b>Hadith of the day</b> — {ref}\n\n"
        f"<i>{e(hadith['text'])}</i>\n\n"
        f"💡 <b>Reflection</b>\n{e(r['hadith_reflection'])}\n\n"
        f"<span class=\"tg-spoiler\">Reflections are AI-generated; verify with a scholar.</span>"
    )
    return [msg1, msg2]


def send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = json.dumps({"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    get_json(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
             headers={"Content-Type": "application/json"})


def main():
    ayah = pick_ayah()
    hadiths = hadith_candidates()
    r = reflect(ayah, hadiths)
    messages = build_messages(ayah, hadiths[r["hadith_index"]], r)
    for m in messages:
        if os.environ.get("DRY_RUN"):
            print(m, "\n" + "-" * 40)
        else:
            send(m)


if __name__ == "__main__":
    main()