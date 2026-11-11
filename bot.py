"""
Daily Islamic reminder bot for Telegram.

Each run sends one Quran ayah (Arabic + English translation) and one
authentic hadith (Sahih al-Bukhari or Sahih Muslim), each followed by a
short AI reflection on how to apply it in daily life.

The Quran text, translation and hadith text are copied verbatim from the
data sources below. The AI only writes the reflection, and it is given
the tafsir (Ibn Kathir) as grounding. If the AI fails even after retries,
the sacred texts are still sent, just without the reflection sections.

Data (free, no API key, served from GitHub):
  - Quran Arabic + translation: github.com/fawazahmed0/quran-api
  - Tafsir Ibn Kathir (English): github.com/spa5k/tafsir_api
  - Hadith: github.com/fawazahmed0/hadith-api

Environment variables:
  TELEGRAM_BOT_TOKEN  from @BotFather
  TELEGRAM_CHAT_ID    your chat id
  GEMINI_API_KEY      from aistudio.google.com (free tier)
  GEMINI_MODEL        optional, default "gemini-flash-lite-latest"
  DATA_DIR            optional: directory caching the big source datasets
  HISTORY_FILE        optional, default "history.json": no-repeat memory
  DRY_RUN=1           optional: print the message instead of sending it,
                      skip the AI call if GEMINI_API_KEY is unset, and
                      don't record anything in the history file
"""

import datetime
import html
import json
import os
import random
import time
import urllib.error
import urllib.parse
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
MIN_HADITH_CHARS = 120         # skip stubs and bare chain references
MAX_HADITH_CHARS = 1200        # skip very long narrations
MAX_TAFSIR_CHARS = 6000        # keep the prompt small
MAX_AYAH_CHARS = 3000          # Arabic + translation combined, so the message fits
TELEGRAM_LIMIT = 4096
HISTORY_FILE = os.environ.get("HISTORY_FILE", "history.json")
HISTORY_DAYS = 365             # don't repeat an ayah or hadith within this window


def get_json(url, data=None, headers=None, attempts=3, base_delay=2, timeout=60):
    """GET/POST a JSON url, retrying with exponential backoff."""
    host = urllib.parse.urlsplit(url).netloc  # never log the URL path: it may hold a token
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as err:
            if isinstance(err, urllib.error.HTTPError):
                try:
                    body = err.read().decode("utf-8", "replace")[:300]
                except Exception:
                    body = ""
                err = RuntimeError(f"HTTP {err.code} from {host}: {body}")
            if attempt == attempts - 1:
                raise err
            print(f"{host}: attempt {attempt + 1}/{attempts} failed ({err}); retrying")
            time.sleep(base_delay * (2 ** attempt))


# ---------- data ----------

def get_dataset(url):
    """Fetch a big dataset, through a local file cache when DATA_DIR is set."""
    cache_dir = os.environ.get("DATA_DIR")
    if not cache_dir:
        return get_json(url)
    path = os.path.join(cache_dir, url.rsplit("/", 1)[-1])
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass  # not cached yet, or corrupted: fetch and rewrite
    data = get_json(url)
    os.makedirs(cache_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    return data


def pick_ayah(skip=frozenset()):
    translation = get_dataset(QURAN_TRANSLATION)["quran"]
    arabic = get_dataset(QURAN_ARABIC)["quran"]
    if len(translation) != len(arabic):
        raise RuntimeError("Quran editions are misaligned (different ayah counts)")
    eligible = [i for i in range(len(translation))
                if len(arabic[i]["text"]) + len(translation[i]["text"]) <= MAX_AYAH_CHARS
                and f"{translation[i]['chapter']}:{translation[i]['verse']}" not in skip]
    i = random.choice(eligible)
    t, a = translation[i], arabic[i]
    if (t["chapter"], t["verse"]) != (a["chapter"], a["verse"]):
        raise RuntimeError(f"Quran editions are misaligned at index {i}")
    s, v = t["chapter"], t["verse"]
    try:
        tafsir = get_json(TAFSIR.format(s=s, a=v))["text"][:MAX_TAFSIR_CHARS]
    except Exception:
        tafsir = ""
    return {
        "surah": s,
        "ayah": v,
        "arabic": a["text"],
        "translation": t["text"],
        "tafsir": tafsir,
    }


def hadith_candidates(skip=frozenset()):
    key = random.choice(list(HADITH_COLLECTIONS))
    name, url, books = HADITH_COLLECTIONS[key]
    fragment_markers = ("narrated this hadith", "this hadith has been", "same chain",
                        "chain of transmitters", "with a slight variation", "with the addition",
                        "similar hadith", "like this hadith", "the same hadith")
    hadiths = [
        h for h in get_dataset(url)["hadiths"]
        if h["reference"]["book"] in books
        and MIN_HADITH_CHARS < len(h["text"].strip()) <= MAX_HADITH_CHARS
        and not any(m in h["text"].lower() for m in fragment_markers)
        and f"{key}:{h['hadithnumber']}" not in skip
    ]
    if not hadiths:
        raise RuntimeError(f"no eligible hadith found in {name}")
    picks = random.sample(hadiths, min(HADITH_CANDIDATES, len(hadiths)))
    out = []
    for h in picks:
        if key == "bukhari":
            ref = f"Sahih al-Bukhari {h['hadithnumber']}"
            link = f"https://sunnah.com/bukhari:{h['hadithnumber']}"
        else:
            # the dataset's hadithnumber is a sequential index, not the sunnah.com
            # number, so link by sunnah.com's in-book reference instead
            book, num = h["reference"]["book"], h["reference"]["hadith"]
            ref = f"Sahih Muslim, Book {book}, Hadith {num}"
            link = f"https://sunnah.com/muslim/{book}/{num}"
        out.append({"collection": name, "key": f"{key}:{h['hadithnumber']}",
                    "ref": ref, "link": link, "text": h["text"].strip()})
    return out


# ---------- no-repeat history ----------

def load_history():
    """Keys of items sent in the last HISTORY_DAYS days, by kind."""
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    cutoff = (datetime.date.today() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    return {kind: {k: d for k, d in raw.get(kind, {}).items() if d >= cutoff}
            for kind in ("ayahs", "hadiths")}


def save_history(history, ayah_key, hadith_key):
    today = datetime.date.today().isoformat()
    history["ayahs"][ayah_key] = today
    history["hadiths"][hadith_key] = today
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=1, sort_keys=True)
        f.write("\n")


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
- Write in simple, everyday English that a 10-year-old could understand:
  short sentences, common words, no scholarly or complicated vocabulary.
  If you must use an Arabic or technical term, explain it in plain words.
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


def validate_reflection(result, n_hadiths):
    """Return a cleaned reflection dict, or raise ValueError if the AI output is unusable."""
    if not isinstance(result, dict):
        raise ValueError("AI output is not a JSON object")
    idx = result.get("hadith_index")
    if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < n_hadiths:
        raise ValueError(f"hadith_index out of range: {idx!r}")
    out = {"hadith_index": idx}
    for key in ("ayah_reflection", "hadith_reflection"):
        v = result.get(key)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{key} is missing or empty")
        out[key] = v.strip()
    return out


def reflect(ayah, hadiths):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        if os.environ.get("DRY_RUN"):
            return {"hadith_index": 0, "ayah_reflection": "(AI skipped: no GEMINI_API_KEY)",
                    "hadith_reflection": "(AI skipped: no GEMINI_API_KEY)"}
        raise RuntimeError("GEMINI_API_KEY is not set")
    # the lite tier has "thinking" off by default; the full flash models think
    # by default and routinely blew through even a 240s read timeout here
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
    hadith_list = "\n\n".join(f"[{i}] ({h['ref']}) {h['text']}" for i, h in enumerate(hadiths))
    prompt = PROMPT.format(hadith_list=hadith_list, **ayah)
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6,
                             "maxOutputTokens": 2048},
    }).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    last_err = None
    for _ in range(2):  # one retry if the output doesn't validate
        # the free tier rate-limits aggressively (hence the slow backoff), and
        # current "thinking" models often need well over 60s to respond
        resp = get_json(url, data=body, headers={
            "Content-Type": "application/json", "x-goog-api-key": api_key},
            attempts=3, base_delay=10, timeout=240)
        try:
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            return validate_reflection(json.loads(text), len(hadiths))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            last_err = e
    raise ValueError(f"AI output failed validation twice: {last_err}")


def safe_reflect(ayah, hadiths):
    """Reflections, or none — a Gemini outage must not block the texts."""
    try:
        return reflect(ayah, hadiths)
    except Exception as e:
        print(f"::warning::AI reflection failed, sending texts without reflections: {e}")
        return {"hadith_index": 0, "ayah_reflection": None, "hadith_reflection": None}


# ---------- Telegram ----------

def _clamp(make_msg, reflection):
    """Shorten the reflection until the built message fits Telegram's limit."""
    reflection = reflection or ""
    msg = make_msg(reflection)
    while len(msg) > TELEGRAM_LIMIT and reflection:
        cut = len(msg) - TELEGRAM_LIMIT + 1
        reflection = "" if cut >= len(reflection) else reflection[:-cut].rstrip() + "…"
        msg = make_msg(reflection)
    if len(msg) > TELEGRAM_LIMIT:
        raise ValueError("message exceeds Telegram limit even without the reflection")
    return msg


def build_messages(ayah, hadith, r):
    e = lambda s: html.escape(s, quote=False)
    quran_url = f"https://quran.com/{ayah['surah']}/{ayah['ayah']}"

    def msg1(reflection):
        msg = (
            f"📖 <b>Ayah of the day</b> — <a href=\"{quran_url}\">Surah {ayah['surah']}:{ayah['ayah']}</a>\n\n"
            f"{e(ayah['arabic'])}\n\n"
            f"<i>{e(ayah['translation'])}</i>\n"
            f"— Saheeh International"
        )
        if reflection:
            msg += f"\n\n💡 <b>Reflection</b>\n{e(reflection)}"
        return msg

    ref = f"<a href=\"{hadith['link']}\">{e(hadith['ref'])}</a>" if hadith["link"] else e(hadith["ref"])

    def msg2(reflection):
        msg = (
            f"🕌 <b>Hadith of the day</b> — {ref}\n\n"
            f"<i>{e(hadith['text'])}</i>"
        )
        if reflection:
            msg += f"\n\n💡 <b>Reflection</b>\n{e(reflection)}"
        return msg

    return [_clamp(msg1, r["ayah_reflection"]), _clamp(msg2, r["hadith_reflection"])]


def send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    get_json(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
             headers={"Content-Type": "application/json"})


def main():
    history = load_history()
    ayah = pick_ayah(skip=history["ayahs"])
    hadiths = hadith_candidates(skip=history["hadiths"])
    r = safe_reflect(ayah, hadiths)
    chosen = hadiths[r["hadith_index"]]
    # build (and validate) both messages before sending either, so a bad
    # message fails the run instead of sending half the reminder
    messages = build_messages(ayah, chosen, r)
    for m in messages:
        if os.environ.get("DRY_RUN"):
            print(m, "\n" + "-" * 40)
        else:
            send(m)
    if not os.environ.get("DRY_RUN"):
        save_history(history, f"{ayah['surah']}:{ayah['ayah']}", chosen["key"])


if __name__ == "__main__":
    main()
