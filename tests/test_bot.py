"""Offline tests for bot.py — all network access is replaced by fixtures."""

import json
import os

import pytest

import bot

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def fake_get_json(mapping):
    """Return a get_json replacement that routes by URL fragment.

    Values may be dicts (returned as-is) or Exceptions (raised).
    """
    def fake(url, data=None, headers=None, **kwargs):
        for fragment, value in mapping.items():
            if fragment in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"test hit an unexpected URL: {url}")
    return fake


def quran_mapping(translation=None, arabic=None, tafsir=None):
    return {
        "eng-ummmuhammad": translation or load("quran_translation.json"),
        "ara-quranuthmanihaf": arabic or load("quran_arabic.json"),
        "en-tafisr-ibn-kathir": tafsir or load("tafsir.json"),
    }


# ---------- http helper ----------

def test_get_json_reports_http_error_body(monkeypatch):
    import io
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {}, io.BytesIO(b'{"error": "quota exceeded"}'))

    monkeypatch.setattr(bot.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match=r"HTTP 429 from example\.com.*quota exceeded"):
        bot.get_json("https://example.com/secret-token/path", attempts=2)


# ---------- ayah selection ----------

def test_pick_ayah_returns_verbatim_aligned_texts(monkeypatch):
    mapping = quran_mapping()
    monkeypatch.setattr(bot, "get_json", fake_get_json(mapping))
    ayah = bot.pick_ayah()
    t = next(x for x in mapping["eng-ummmuhammad"]["quran"]
             if (x["chapter"], x["verse"]) == (ayah["surah"], ayah["ayah"]))
    a = next(x for x in mapping["ara-quranuthmanihaf"]["quran"]
             if (x["chapter"], x["verse"]) == (ayah["surah"], ayah["ayah"]))
    assert ayah["translation"] == t["text"]
    assert ayah["arabic"] == a["text"]
    assert ayah["tafsir"] == mapping["en-tafisr-ibn-kathir"]["text"]


def test_pick_ayah_skips_overlong_verses(monkeypatch):
    translation = load("quran_translation.json")
    arabic = load("quran_arabic.json")
    # make every verse except the first too long to fit a Telegram message
    for entry in translation["quran"][1:]:
        entry["text"] = "long " * 700
    monkeypatch.setattr(bot, "get_json", fake_get_json(quran_mapping(translation, arabic)))
    for _ in range(10):
        ayah = bot.pick_ayah()
        assert (ayah["surah"], ayah["ayah"]) == (1, 1)


def test_pick_ayah_rejects_misaligned_editions(monkeypatch):
    arabic = load("quran_arabic.json")
    for entry in arabic["quran"]:
        entry["chapter"] += 1
    monkeypatch.setattr(bot, "get_json", fake_get_json(quran_mapping(arabic=arabic)))
    with pytest.raises(RuntimeError, match="misaligned"):
        bot.pick_ayah()


def test_pick_ayah_rejects_different_lengths(monkeypatch):
    arabic = load("quran_arabic.json")
    arabic["quran"].pop()
    monkeypatch.setattr(bot, "get_json", fake_get_json(quran_mapping(arabic=arabic)))
    with pytest.raises(RuntimeError, match="misaligned"):
        bot.pick_ayah()


def test_pick_ayah_survives_tafsir_outage(monkeypatch):
    mapping = quran_mapping(tafsir=ConnectionError("tafsir host down"))
    monkeypatch.setattr(bot, "get_json", fake_get_json(mapping))
    assert bot.pick_ayah()["tafsir"] == ""


# ---------- hadith selection ----------

def test_hadith_filtering(monkeypatch):
    fixture = load("hadith_bukhari.json")
    fixture["hadiths"].append({  # over the length cap
        "hadithnumber": 9999,
        "text": "Narrated So-and-so: " + "a very long narration " * 60,
        "reference": {"book": 2, "hadith": 999},
    })
    monkeypatch.setattr(bot.random, "choice", lambda seq: "bukhari" if "bukhari" in seq else seq[0])
    monkeypatch.setattr(bot, "get_json", fake_get_json({"eng-bukhari": fixture}))
    candidates = bot.hadith_candidates()

    # the fixture holds 5 eligible hadith; the rest are filtered out
    assert len(candidates) == 5
    numbers = {int(c["ref"].split()[-1]) for c in candidates}
    assert 2079 not in numbers   # wrong book (Sales)
    assert 50 not in numbers     # too short
    assert 6500 not in numbers   # narration-chain fragment
    assert 9999 not in numbers   # too long
    for c in candidates:
        assert bot.MIN_HADITH_CHARS < len(c["text"]) <= bot.MAX_HADITH_CHARS
        assert c["ref"].startswith("Sahih al-Bukhari ")
        assert c["link"] == f"https://sunnah.com/bukhari:{c['ref'].split()[-1]}"


def test_hadith_muslim_reference_format(monkeypatch):
    monkeypatch.setattr(bot.random, "choice", lambda seq: "muslim" if "muslim" in seq else seq[0])
    monkeypatch.setattr(bot, "get_json", fake_get_json({"eng-muslim": load("hadith_muslim.json")}))
    candidates = bot.hadith_candidates()
    assert len(candidates) == 2
    for c in candidates:
        assert c["ref"].startswith("Sahih Muslim, Book ")
        assert c["link"] is None


def test_hadith_raises_when_nothing_eligible(monkeypatch):
    monkeypatch.setattr(bot.random, "choice", lambda seq: "bukhari" if "bukhari" in seq else seq[0])
    monkeypatch.setattr(bot, "get_json", fake_get_json({"eng-bukhari": {"hadiths": []}}))
    with pytest.raises(RuntimeError, match="no eligible hadith"):
        bot.hadith_candidates()


# ---------- AI output validation ----------

GOOD = {"hadith_index": 2, "ayah_reflection": "Reflect.", "hadith_reflection": "Apply."}


def test_validate_reflection_accepts_good_output():
    assert bot.validate_reflection(dict(GOOD), 8) == GOOD


@pytest.mark.parametrize("bad", [
    "not a dict",
    {**GOOD, "hadith_index": 8},        # out of range
    {**GOOD, "hadith_index": -1},
    {**GOOD, "hadith_index": "2"},      # wrong type
    {**GOOD, "hadith_index": True},
    {**GOOD, "ayah_reflection": ""},
    {**GOOD, "ayah_reflection": "   "},
    {k: v for k, v in GOOD.items() if k != "hadith_reflection"},
])
def test_validate_reflection_rejects_bad_output(bad):
    with pytest.raises(ValueError):
        bot.validate_reflection(bad, 8)


def gemini_response(payload):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}


AYAH = {"surah": 1, "ayah": 1, "arabic": "بِسْمِ ٱللَّهِ", "translation": "In the name of Allah...",
        "tafsir": "Some tafsir."}
HADITHS = [{"collection": "Sahih al-Bukhari", "ref": f"Sahih al-Bukhari {i}",
            "link": f"https://sunnah.com/bukhari:{i}", "text": f"Hadith text {i}"} for i in range(8)]


def test_reflect_retries_once_on_bad_output(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    responses = [gemini_response({**GOOD, "hadith_index": 99}), gemini_response(GOOD)]
    calls = []

    def fake(url, *a, **kw):
        calls.append(url)
        return responses[len(calls) - 1]

    monkeypatch.setattr(bot, "get_json", fake)
    assert bot.reflect(AYAH, HADITHS) == GOOD
    assert len(calls) == 2


def test_reflect_gives_up_after_two_bad_outputs(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(bot, "get_json",
                        lambda *a, **kw: gemini_response({"hadith_index": 99}))
    with pytest.raises(ValueError, match="failed validation twice"):
        bot.reflect(AYAH, HADITHS)


def test_safe_reflect_omits_reflections_on_total_failure(monkeypatch):
    def boom(ayah, hadiths):
        raise ConnectionError("Gemini is down")
    monkeypatch.setattr(bot, "reflect", boom)
    r = bot.safe_reflect(AYAH, HADITHS)
    assert r == {"hadith_index": 0, "ayah_reflection": None, "hadith_reflection": None}
    # the texts still go out, just without the reflection sections
    msg1, msg2 = bot.build_messages(AYAH, HADITHS[0], r)
    assert AYAH["arabic"] in msg1 and AYAH["translation"] in msg1
    assert HADITHS[0]["text"] in msg2
    for m in (msg1, msg2):
        assert "Reflection" not in m
        assert "💡" not in m
        assert "AI-generated" not in m


# ---------- message building ----------

def test_build_messages_escapes_html():
    hadith = dict(HADITHS[0], text="He said: never say <b>bad</b> words & lies.")
    r = dict(GOOD, ayah_reflection="Watch out for <script> & co.")
    msg1, msg2 = bot.build_messages(AYAH, hadith, r)
    assert "<script>" not in msg1 and "&lt;script&gt;" in msg1
    assert "<b>bad</b>" not in msg2 and "&lt;b&gt;bad&lt;/b&gt;" in msg2
    assert "& lies" not in msg2 and "&amp; lies" in msg2


def test_build_messages_structure_and_links():
    msg1, msg2 = bot.build_messages(AYAH, HADITHS[3], GOOD)
    assert '<a href="https://quran.com/1/1">' in msg1
    assert AYAH["arabic"] in msg1 and AYAH["translation"] in msg1
    assert '<a href="https://sunnah.com/bukhari:3">' in msg2
    assert HADITHS[3]["text"] in msg2
    assert "AI-generated" not in msg2


def test_build_messages_without_link_has_no_anchor():
    hadith = dict(HADITHS[0], link=None, ref="Sahih Muslim, Book 1, Hadith 5")
    _, msg2 = bot.build_messages(AYAH, hadith, GOOD)
    assert "<a href" not in msg2
    assert "Sahih Muslim, Book 1, Hadith 5" in msg2


def test_build_messages_clamps_to_telegram_limit():
    r = dict(GOOD, ayah_reflection="reflect deeply " * 1000,
             hadith_reflection="apply gently " * 1000)
    msg1, msg2 = bot.build_messages(AYAH, HADITHS[0], r)
    assert len(msg1) <= bot.TELEGRAM_LIMIT and msg1.endswith("…")
    assert len(msg2) <= bot.TELEGRAM_LIMIT and "…" in msg2


def test_build_messages_raises_if_base_text_is_too_long():
    hadith = dict(HADITHS[0], text="x" * 5000)  # bypasses the normal 1200-char cap
    with pytest.raises(ValueError, match="exceeds Telegram limit"):
        bot.build_messages(AYAH, hadith, GOOD)
