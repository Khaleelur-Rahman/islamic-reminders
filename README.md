# Daily Islamic Reminder Bot

Sends one Quran ayah and one authentic hadith (Sahih al-Bukhari / Sahih Muslim)
to a private Telegram chat every morning at 7:00 AM Singapore time, each with a
short AI-written reflection. Quran and hadith text are always inserted verbatim
from the source datasets — the AI only writes the reflections.

Runs entirely on free tiers: GitHub Actions (scheduler), Gemini API (reflections),
Telegram Bot API (delivery). No server, no database, Python stdlib only.

## Setup

1. **Telegram bot** — message [@BotFather](https://t.me/BotFather), `/newbot`,
   copy the token.
2. **Chat id** — send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
   `message.chat.id`.
3. **Gemini key** — create one at [aistudio.google.com](https://aistudio.google.com)
   (free tier).
4. **GitHub secrets** — in the repo: Settings → Secrets and variables → Actions →
   New repository secret. Add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   `GEMINI_API_KEY`.
5. **Test it** — Actions tab → "Daily reminder" → Run workflow. Both messages
   should arrive within ~2 minutes.

After that the schedule (23:00 UTC = 7:00 AM SGT, sometimes 5–15 min late)
runs on its own. A failed run shows up as a failed Action; if only Gemini
fails, the texts are still sent, just without the reflection sections.

Each run records what it sent in `history.json` (committed back by the
workflow) and won't repeat an ayah or hadith within 365 days. The ~13 MB of
source datasets are cached between runs with `actions/cache`.

## Local testing

```bash
DRY_RUN=1 python3 bot.py                        # prints messages, skips AI if no key
DRY_RUN=1 GEMINI_API_KEY=... python3 bot.py     # prints messages incl. real reflections
pytest                                          # offline tests, fixture data only
```

## Content rules

- The LLM never generates, translates or paraphrases Quran or hadith text.
- Only sahih collections (currently Bukhari and Muslim), drawn from chapters on
  belief, manners, kinship, remembrance, repentance and softening the heart.
- Reflections are grounded in Tafsir Ibn Kathir and never issue rulings.
- Every message links its source so the reader can verify it.
