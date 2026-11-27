"""Once-daily Syro-Malabar readings and homily, with chapter-aware Google narration."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
import hashlib
import html
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import wave
import zipfile
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
from google.api_core.exceptions import PreconditionFailed
from openai import OpenAI
from pydantic import BaseModel, Field
import requests

PACIFIC = ZoneInfo("America/Los_Angeles")
PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "voiceagent-ios-dj-20261112")
REPO = "jdayalu/homily-podcast"
BASE = "https://jdayalu.github.io/homily-podcast"
CALENDAR = "https://www.syromalabarliturgy.org/assets/uploads/pdfs/beef9-2025-2026-panchangam-english-pdf-text-.pdf"
MODEL = os.getenv("HOMILY_TTS_MODEL", "gemini-3.8-flash-tts")
NARRATION = json.loads(Path(__file__).with_name("narration.json").read_text(encoding="utf-8"))
MAX_HOMILY_SECONDS = 600.0
HOMILY_PROMPT = Path(__file__).with_name("homily_prompt.txt").read_text(encoding="utf-8")
SCRIPTURES = {
    "English": {"id": "eng-web-c", "name": "World English Bible, Catholic book order", "license": "Public domain"},
    "Malayalam": {"id": "mal2015", "name": "Malayalam Bible 1910, contemporary orthography", "license": "© 2015 The Free Bible Foundation · CC BY-SA 4.0"},
}
ALIASES = """
GEN Gen Gn Genesis
EXO Ex Exod Exodus
LEV Lev Lv Leviticus
NUM Num Nm Numbers
DEU Deut Dt Deuteronomy
JOS Josh Jos Joshua
JDG Judg Jdg Judges
RUT Ruth Ru
1SA 1Sam 1Sm 1Samuel
2SA 2Sam 2Sm 2Samuel
1KI 1Kgs 1Kg 1Kings
2KI 2Kgs 2Kg 2Kings
1CH 1Chr 1Chronicles
2CH 2Chr 2Chronicles
EZR Ezra Ezr
NEH Neh Nehemiah
TOB Tob Tb Tobit
JDT Jdt Judith
EST Est Esther
JOB Job Jb
PSA Ps Pss Psalms Psalm
PRO Prov Pr Proverbs
ECC Eccl Qoh Ecclesiastes
SOL Song Sg Cant
WIS Wis Wisdom
SIR Sir Ecclus Sirach
ISA Is Isa Isaiah
JER Jer Jeremiah
LAM Lam Lamentations
BAR Bar Baruch
EZE Ezek Ez Ezk Ezekiel
DNG Dan Dn Daniel
HOS Hos Hosea
JOE Joel Jl
AMO Amos Am
OBA Ob Obad
JON Jonah Jon
MIC Mic Mi
NAH Nah Na
HAB Hab
ZEP Zeph Zep
HAG Hag
ZEC Zech Zec
MAL Mal
MAT Mt Mat Matt Matthew
MAR Mk Mark
LUK Lk Luke
JOH Jn John
ACT Acts Act
ROM Rom Rm Romans
1CO 1Cor 1Corinthians
2CO 2Cor 2Corinthians
GAL Gal Galatians
EPH Eph Ephesians
PHI Phil Philippians
COL Col Colossians
1TH 1Thess 1Thes 1Thessalonians
2TH 2Thess 2Thes 2Thessalonians
1TI 1Tim 1Timothy
2TI 2Tim 2Timothy
TIT Tit Titus
PHM Phlm Philemon
HEB Heb Hebrews
JAM Jas Jam James
1PE 1Pet 1Pt 1Peter
2PE 2Pet 2Pt 2Peter
1JO 1Jn 1John
2JO 2Jn 2John
3JO 3Jn 3John
JUD Jude
REV Rev Rv Revelation
"""
BOOKS = {a.lower(): row.split()[0] for row in ALIASES.strip().splitlines() for a in row.split()}
ML_BOOKS = {"HEB": "ഹെബ്രായർക്കെഴുതിയ ലേഖനം", "LUK": "ലൂക്കായുടെ സുവിശേഷം", "MAT": "മത്തായിയുടെ സുവിശേഷം", "MAR": "മർക്കോസിന്റെ സുവിശേഷം", "JOH": "യോഹന്നാന്റെ സുവിശേഷം"}

VERSE = r"\d+[a-c]?"
SPAN = rf"(?:\d+\s*:\s*)?{VERSE}(?:\s*[-–]\s*(?:\d+\s*:\s*)?{VERSE})?"
REFERENCE_LINE = re.compile(rf"^([1-3]?\s*[A-Za-z]+)\s+(\d+\s*:\s*{VERSE}(?:\s*[-–]\s*(?:\d+\s*:\s*)?{VERSE})?(?:\s*[,;+]\s*{SPAN})*)")


def reading_from_line(line):
    match = REFERENCE_LINE.match(line)
    if not match:
        return None
    alias = re.sub(r"\s", "", match[1]).lower()
    if alias not in BOOKS:
        return None
    reference = re.sub(r"\s+", " ", match[1]) + " " + match[2].strip()
    # Verse subdivisions differ between translations. Retain the official reference,
    # but transparently include each affected verse in full rather than truncate it.
    spoken_reference = re.sub(r"(?<=\d)[a-c]", "", reference)
    return {"reference": reference, "spoken_reference": spoken_reference, "book": BOOKS[alias],
            "theme": line[match.end():].strip(), "whole_verse_expansion": spoken_reference != reference}


def get(url):
    response = requests.get(url, timeout=45)
    response.raise_for_status()
    return response.content


def calendar_entry(text, day):
    lines = text.splitlines()
    month = day.strftime("%B %Y")
    starts = [i for i, line in enumerate(lines) if line.strip() == month]
    day_pattern = re.compile(r"^\s*(\d{1,2})\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b\s*(.*)")
    for start in starts:
        result = []
        active = False
        for line in lines[start:]:
            match = day_pattern.match(line)
            if match:
                if active:
                    break
                if int(match[1]) == day.day and match[2] == day.strftime("%a"):
                    active = True
                    result.append(match[3].strip().lstrip(" "))
                    continue
            if active and line.strip():
                result.append(re.sub(r"\s+", " ", line.strip()))
        if result:
            return result
    raise ValueError("No official calendar entry for " + day.isoformat())


def assignments(day, work):
    import pymupdf
    urls = [CALENDAR] if day.year <= 2026 else []
    if not urls:
        index = BeautifulSoup(get("https://www.syromalabarliturgy.org/liturgical_texts"), "html.parser")
        from urllib.parse import urljoin, urlsplit
        for link in index.select("a[href]"):
            url = urljoin(CALENDAR, link["href"])
            label = link.get_text(" ") + " " + url
            if urlsplit(url).hostname == "www.syromalabarliturgy.org" and url.lower().endswith(".pdf") and str(day.year) in label and re.search(r"english|calendar", label, re.I):
                urls.append(url)
    for url in urls:
        pdf = get(url)
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            text = "\n".join(page.get_text(sort=True) for page in document)
        try:
            entry = calendar_entry(text, day)
        except ValueError:
            continue
        readings = []
        for line in entry[1:]:
            reading = reading_from_line(line)
            if not reading:
                continue
            readings.append(reading)
            if reading["book"] in {"MAT", "MAR", "LUK", "JOH"}:
                break
        if not readings or readings[-1]["book"] not in {"MAT", "MAR", "LUK", "JOH"}:
            raise ValueError("Official entry has no complete main reading sequence")
        (work / "official-calendar-entry.txt").write_text("\n".join(entry), encoding="utf-8")
        return {"date": day.isoformat(), "weekday": day.strftime("%A, %B %d, %Y"), "liturgical_day": entry[0],
                "readings": "; ".join(r["reference"] for r in readings), "gospel": readings[-1]["reference"],
                "theme": readings[-1]["theme"], "calendar_source": url, "assignments": readings}
    raise ValueError("No current official calendar is available; publication stopped")


def bible(translation, work):
    archive = zipfile.ZipFile(io.BytesIO(get("https://ebible.org/Scriptures/" + translation + "_vpl.zip")))
    names = [n for n in archive.namelist() if n.endswith("_vpl.txt")]
    if len(names) != 1:
        raise ValueError("Unexpected Scripture archive")
    result = {}
    for line in archive.read(names[0]).decode("utf-8-sig").splitlines():
        match = re.match(r"^(\S+) (\d+):(\d+) (.+)$", line)
        if match:
            result.setdefault(match[1], {})[(int(match[2]), int(match[3]))] = match[4]
    return result


def passage(books, reference):
    match = re.fullmatch(r"([1-3]?[A-Za-z]+)([\d:;,\-+]+)", re.sub(r"[.\s]", "", reference).replace("–", "-"))
    if not match or match[1].lower() not in BOOKS:
        raise ValueError("Unsupported Scripture reference: " + reference)
    book = BOOKS[match[1].lower()]
    verses = books.get(book) or (books.get("DAN") if book == "DNG" else None)
    if not verses:
        raise ValueError("Licensed translation lacks assigned book: " + reference)
    chapter = None
    selected = []
    for span in re.split(r"[,;+]", match[2]):
        part = re.fullmatch(r"(?:(\d+):)?(\d+)(?:-(?:(\d+):)?(\d+))?", span)
        if not part or (not part[1] and chapter is None):
            raise ValueError("Unsupported verse range: " + reference)
        chapter = int(part[1]) if part[1] else chapter
        first = chapter, int(part[2])
        last = int(part[3]) if part[3] else chapter, int(part[4] or part[2])
        if first not in verses or last not in verses or last < first:
            raise ValueError("Missing verses: " + reference)
        selected.extend(value for key, value in sorted(verses.items()) if first <= key <= last)
        chapter = last[0]
    return "\n".join(selected)


class Homilies(BaseModel):
    liturgical_day_ml: str
    references_ml: list[str]
    english: str = Field(min_length=6500, max_length=16000)
    malayalam: str = Field(min_length=3500, max_length=28000)


def write_homilies(meta, texts, site):
    client = OpenAI(timeout=240, max_retries=1)
    prompt = HOMILY_PROMPT
    material = {"calendar": meta, "scripture": texts["English"]}
    for attempt in range(3):
        response = client.responses.parse(model=os.getenv("HOMILY_TEXT_MODEL", "gpt-6.2-sol"),
                                          instructions=prompt, input=json.dumps(material, ensure_ascii=False), text_format=Homilies,
                                          reasoning={"effort": "low"}, store=False)
        result = response.output_parsed
        count = len(result.english.split()) if result else 0
        if result and 1350 <= count <= 1450 and len(result.references_ml) == len(meta["assignments"]):
            break
        prompt += f" The previous draft failed validation (English words: {count}); enforce 1350–1450 words and one Malayalam reference per reading."
    else:
        raise ValueError("Homily text did not meet length/reference validation")
    meta["liturgical_day_ml"] = result.liturgical_day_ml
    meta["references_ml"] = result.references_ml
    for language, body in [("English", result.english), ("Malayalam", result.malayalam)]:
        heading = ["Homily for " + meta["liturgical_day"], meta["weekday"], "Readings: " + meta["readings"]] if language == "English" else [result.liturgical_day_ml + " · പ്രസംഗം", meta["date"], "വായനകൾ: " + "; ".join(result.references_ml)]
        (site / "episodes" / f"Homily_{meta['date']}_{language}.txt").write_text("\n".join(heading) + "\n\n" + body.strip() + "\n", encoding="utf-8")
    meta["english_homily_words"] = count


class ShortHomily(BaseModel):
    body: str = Field(min_length=500, max_length=16000)


def tighten_homily(language, body, measured_seconds):
    count = len(body.split())
    upper = int(count * 555 / measured_seconds)
    lower = 1350 if language == "English" else max(80, int(upper * 0.85))
    upper = max(1375, min(1450, upper)) if language == "English" else upper
    if upper >= count:
        return body
    prompt = (HOMILY_PROMPT + f"\nEdit only the supplied {language} body to {lower}–{upper} words. "
              "Remove repetition while preserving the Gospel interpretation, the practical applications, the Holy Qurbana link, "
              "and the required opening and closing. Preserve all factual meaning; add no new claims. Return only the revised body in the schema.")
    for _ in range(3):
        response = OpenAI(timeout=180, max_retries=1).responses.parse(
            model=os.getenv("HOMILY_TEXT_MODEL", "gpt-6.2-sol"), instructions=prompt,
            input=body, text_format=ShortHomily, reasoning={"effort": "low"}, store=False)
        result = response.output_parsed
        if result and lower <= len(result.body.split()) <= upper:
            return result.body.strip()
    raise ValueError("Could not tighten the homily within the requested word limits")


def validate_audio_duration(duration, offset, homily_seconds):
    if not all(math.isfinite(v) for v in (duration, offset, homily_seconds)):
        raise ValueError("Non-finite audio duration")
    if not 5 < offset < duration - 60 or not 60 < homily_seconds <= MAX_HOMILY_SECONDS:
        raise ValueError("Readings/homily duration validation failed")
    if duration - offset > MAX_HOMILY_SECONDS:
        raise ValueError("Encoded homily exceeds the ten-minute maximum")


def authorized_session():
    cli = os.getenv("HOMILY_LOCAL_GCLOUD")
    if cli:
        session = requests.Session()
        token = subprocess.check_output([cli, "auth", "print-access-token"], text=True).strip()
        session.headers["Authorization"] = "Bearer " + token
        return session
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


def chunks(text, limit=1500):
    paragraphs = text.strip().split("\n\n")
    result = []
    for paragraph in paragraphs:
        current = ""
        for word in paragraph.split():
            if len(current) + len(word) + 1 > limit and current:
                result.append(current); current = ""
            current += (" " if current else "") + word
        if current:
            result.append(current)
    return result


def synthesize(text, language, target, homily=False, retry=False):
    profile = NARRATION[language]
    style = profile["style"]
    style += " No singing, background sounds or theatrical voices. Read exactly the supplied text, adding no words."
    if homily:
        style += " Maintain a steady conversational sermon flow with brief natural pauses, without stretching syllables or adding long silences."
        if language == "English":
            style += f" Aim for about {160 if retry else 150} words per minute."
    pcm = bytearray()
    rate = 24000
    with authorized_session() as session:
        for part in chunks(text):
            body = {"contents": [{"role": "user", "parts": [{"text": part, "speechMetadata": {"style": style}}]}],
                    "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"voice": profile["voice"]}}}}
            for attempt in range(3):
                response = session.post(f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/publishers/google/models/{MODEL}:generateContent", json=body, timeout=180)
                if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                    response.raise_for_status(); break
                time.sleep(2 ** (attempt + 1))
            candidate = response.json()["candidates"][0]
            if candidate.get("finishReason") != "STOP":
                raise ValueError("Narration was incomplete")
            data = next(p["inlineData"] for p in candidate["content"]["parts"] if "inlineData" in p)
            raw = base64.b64decode(data["data"], validate=True)
            with wave.open(io.BytesIO(raw), "rb") as clip:
                if (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) != (1, 2, rate):
                    raise ValueError("Unexpected narration audio format")
                seconds = clip.getnframes() / rate
                if seconds < len(part.split()) * 0.12 or seconds > 300:
                    raise ValueError("Narration duration is inconsistent with the text")
                pcm.extend(clip.readframes(clip.getnframes()))
            pcm.extend(bytes(int(rate * 0.35) * 2))
    with wave.open(str(target), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "")); output.writeframes(pcm)
    return len(pcm) / (rate * 2)


def render_language(language, meta, passages, site, work):
    profile = NARRATION[language]
    day = meta["date"]
    ep = site / "episodes"
    full = (ep / f"Homily_{day}_{language}.txt").read_text(encoding="utf-8")
    body = "\n".join(full.splitlines()[3:]).strip()
    spoken = []
    for index, reading in enumerate(meta["assignments"]):
        reference = reading["reference"]
        if language == "Malayalam":
            reference = meta.get("references_ml", [])[index] if len(meta.get("references_ml", [])) == len(passages) else ML_BOOKS.get(reading["book"], reference)
            introduction = "ഇന്നത്തെ സുവിശേഷ വായന. " if index == len(passages) - 1 else "ഇന്നത്തെ വചനവായന. "
        else:
            introduction = "Today's Gospel reading. " if index == len(passages) - 1 else "A reading from "
        spoken.append(introduction + reference + ".\n\n" + passages[index])
    reading_text = "\n\n".join(spoken)
    source = SCRIPTURES[language]
    attribution = f"\n\nScripture: {source['name']}. {source['license']}. https://ebible.org/{source['id']}/copyright.htm\n"
    if language == "Malayalam":
        attribution += "The Scripture text and its synthesized reading are shared under CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/). Adaptation: Google AI audio narration; introductions added.\n"
    if any(r.get("whole_verse_expansion") for r in meta["assignments"]):
        attribution += "Where the official calendar assigns part of a verse (a/b/c), this study recording includes that verse in full. Official references are retained in the metadata.\n"
    (ep / f"Readings_{day}_{language}.txt").write_text(reading_text + attribution, encoding="utf-8")
    readings_wav, homily_wav = work / f"{language}-readings.wav", work / f"{language}-homily.wav"
    reading_seconds = synthesize(reading_text, language, readings_wav)
    for attempt in range(3):
        homily_seconds = synthesize(body, language, homily_wav, homily=True, retry=attempt > 0)
        print(json.dumps({"stage": "homily duration measured", "language": language,
                          "seconds": round(homily_seconds, 3), "attempt": attempt + 1}), flush=True)
        # Leave room for MP3 encoder padding; the final encoded chapter is checked too.
        if homily_seconds <= MAX_HOMILY_SECONDS - 0.5:
            break
        if attempt == 2:
            raise ValueError(language + " homily remains over ten minutes; publication stopped")
        body = tighten_homily(language, body, homily_seconds)
    headings = "\n".join(full.splitlines()[:3])
    (ep / f"Homily_{day}_{language}.txt").write_text(headings + "\n\n" + body + "\n", encoding="utf-8")
    combined = work / f"{language}-combined.wav"
    with wave.open(str(combined), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", ""))
        for path in [readings_wav, homily_wav]:
            with wave.open(str(path), "rb") as source_audio:
                output.writeframes(source_audio.readframes(source_audio.getnframes()))
            if path == readings_wav:
                output.writeframes(bytes(24000 * 2))
    offset = round(reading_seconds + 1, 3)
    audio = ep / f"Homily_{day}_{language}.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(combined), "-af", "highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", "-id3v2_version", "3",
                    "-metadata", f"title=Readings and Homily – {day} ({language})", "-metadata", "album=Daily Homilies",
                    "-metadata", f"artist={profile['reader']} · AI narration", str(audio)], check=True)
    duration = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)], text=True))
    validate_audio_duration(duration, offset, homily_seconds)
    chapters = {"version": "1.2.0", "chapters": [{"startTime": 0, "title": "Bible readings"}, {"startTime": offset, "title": "Homily"}]}
    (ep / f"Chapters_{day}_{language}.json").write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8")
    return language, {"duration_seconds": round(duration, 3), "homily_start_seconds": offset,
                      "homily_duration_seconds": round(duration - offset, 3), "homily_words": len(body.split()),
                      "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                      "reader": profile["reader"], "voice": profile["voice"], "voice_gender": profile["gender"], "voice_style": profile["style"], "model": MODEL,
                      "scripture_translation": source["name"], "scripture_license": source["license"], "scripture_source": f"https://ebible.org/{source['id']}/copyright.htm"}


def build_feed(site):
    items, rows = [], []
    ep = site / "episodes"
    for file in sorted(ep.glob("????-??-??.json"), reverse=True):
        meta = json.loads(file.read_text())
        for language in ["Malayalam", "English"]:
            audio = ep / f"Homily_{meta['date']}_{language}.mp3"
            if not audio.exists():
                continue
            info = meta.get("audio", {}).get(language, {})
            digest = info.get("sha256") or hashlib.sha256(audio.read_bytes()).hexdigest()
            url = f"{BASE}/episodes/{audio.name}?v=2&rev={digest}"
            text = f"{BASE}/episodes/{audio.stem}.txt"
            title = f"{meta['date']} · {language} · {meta['liturgical_day']}"
            description = f"{meta['liturgical_day']}. Readings: {meta['readings']}. Text: {text}"
            extra = ""
            if info:
                description += f" Readings text: {BASE}/episodes/Readings_{meta['date']}_{language}.txt. Scripture: {info['scripture_translation']}; {info['scripture_license']}. Original AI-assisted homily for personal reflection; not an official Church homily."
                extra = f"<voiceagent:homilyStart>{info['homily_start_seconds']}</voiceagent:homilyStart><voiceagent:reader>{html.escape(info['reader'])}</voiceagent:reader>"
                extra += f'<podcast:chapters url="{BASE}/episodes/Chapters_{meta["date"]}_{language}.json?rev={digest}" type="application/json+chapters"/>'
            pub = datetime.fromisoformat(meta["date"]).replace(hour=4, tzinfo=PACIFIC)
            duration = info.get("duration_seconds") or float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)], text=True))
            items.append(f'<item><title>{html.escape(title)}</title><description>{html.escape(description)}</description><enclosure url="{html.escape(url,quote=True)}" length="{audio.stat().st_size}" type="audio/mpeg"/><guid isPermaLink="false">homily-{meta["date"]}-{language.lower()}</guid><pubDate>{format_datetime(pub)}</pubDate><itunes:duration>{round(duration)}</itunes:duration><itunes:explicit>false</itunes:explicit>{extra}</item>')
            skip = f'<button onclick="this.parentNode.querySelector(\'audio\').currentTime={info["homily_start_seconds"]};this.parentNode.querySelector(\'audio\').play()">Skip to homily</button>' if info else ""
            rows.append(f'<article><h2>{html.escape(title)}</h2><p>{html.escape(meta["readings"])}</p><audio controls preload="none" src="{html.escape(url,quote=True)}"></audio>{skip}<p><a href="{text}">Homily text</a></p></article>')
    (site / "feed.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:voiceagent="https://jdayalu.github.io/homily-podcast/ns/1.0"><channel><title>Daily Homily – Syro-Malabar</title><link>' + BASE + '/</link><language>ml</language><description>Daily readings and original homilies in Malayalam and English.</description><itunes:author>Daily Homily</itunes:author><itunes:image href="' + BASE + '/cover.jpg"/><itunes:category text="Religion &amp; Spirituality"><itunes:category text="Christianity"/></itunes:category><itunes:explicit>false</itunes:explicit><lastBuildDate>' + format_datetime(datetime.now(timezone.utc)) + '</lastBuildDate>' + '\n'.join(items) + '</channel></rss>', encoding="utf-8")
    (site / "index.html").write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Daily Homily</title><style>body{font-family:system-ui;max-width:700px;margin:auto;padding:20px;background:#f8f6f1;color:#172822}article{background:white;border-radius:18px;padding:20px;margin:18px 0}h2{font-size:19px}audio{width:100%}button{background:#087f73;color:white;border:0;border-radius:10px;padding:12px;margin-top:12px}</style><h1>Daily Homily</h1><p>Daily at 4:00 AM Pacific · Malayalam and English · Bible readings first</p><p><a href="feed.xml">Podcast feed</a></p>' + ''.join(rows) + '</html>', encoding="utf-8")


def git(site, *args, env=None):
    return subprocess.run(["git", *args], cwd=site, env=env, check=True, capture_output=True, text=True, timeout=120).stdout.strip()


def preserved_audio(site, previous, rendered_languages):
    preserved = {}
    for language in SCRIPTURES:
        if language in rendered_languages:
            continue
        info = previous.get("audio", {}).get(language)
        day = date.fromisoformat(previous.get("date", "")).isoformat()
        audio = site / "episodes" / f"Homily_{day}_{language}.mp3"
        if not info or not audio.exists() or hashlib.sha256(audio.read_bytes()).hexdigest() != info.get("sha256"):
            raise ValueError("Cannot preserve an unverified " + language + " recording")
        preserved[language] = info
    return preserved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(PACIFIC).date())
    parser.add_argument("--site", type=Path)
    parser.add_argument("--reuse-text", action="store_true")
    parser.add_argument("--language", choices=list(SCRIPTURES), help="Re-record only this language using existing text")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check", action="store_true", help="Verify cloud credentials and providers without publishing")
    args = parser.parse_args()
    if args.language and not args.reuse_text:
        parser.error("--language requires --reuse-text so other published languages are preserved")
    languages = [args.language] if args.language else list(SCRIPTURES)
    with tempfile.TemporaryDirectory(prefix="homily-") as temporary:
        work = Path(temporary)
        env = os.environ.copy()
        site = args.site
        if site is None:
            key = work / "key"
            key.write_text(os.environ["HOMILY_GITHUB_DEPLOY_KEY"].rstrip() + "\n"); key.chmod(0o600)
            env.update(GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND=f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile={Path(__file__).with_name('github_known_hosts')}")
            site = work / "site"
            subprocess.run(["git", "clone", "--depth", "1", "git@github.com:" + REPO + ".git", str(site)], env=env, check=True, capture_output=True, timeout=120)
        metadata_path = site / "episodes" / f"{args.date}.json"
        if args.check:
            result = OpenAI(timeout=45).responses.create(model=os.getenv("HOMILY_TEXT_MODEL", "gpt-6.2-sol"),
                                                        input="Reply with exactly OK.", reasoning={"effort": "low"}, store=False)
            if result.output_text.strip() != "OK":
                raise ValueError("Text generation probe did not succeed")
            seconds = synthesize("ഈശോമിശിഹായിൽ പ്രിയ സഹോദരീസഹോദരന്മാരേ, ഇന്നത്തെ ദൈവവചനം നമുക്ക് ശ്രദ്ധയോടെ കേൾക്കാം.", "Malayalam", work / "probe.wav")
            print(json.dumps({"status": "cloud providers verified", "repository": REPO, "voice": NARRATION["Malayalam"]["voice"], "model": MODEL, "probe_seconds": seconds})); return
        old = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        if old.get("format_version") == 2 and not args.force:
            print(json.dumps({"status": "already published", "date": str(args.date)})); return
        lease = None
        if os.getenv("HOMILY_LOCK_BUCKET"):
            lease = storage.Client().bucket(os.environ["HOMILY_LOCK_BUCKET"]).blob(f"homily-publisher/{args.date}.lock")
            try:
                lease.upload_from_string(str(time.time()), if_generation_match=0)
            except PreconditionFailed:
                lease.reload()
                if time.time() - lease.updated.timestamp() < 3600:
                    print('{"status":"another publisher is running"}'); return
                lease.upload_from_string(str(time.time()), if_generation_match=lease.generation)
        try:
            meta = assignments(args.date, work)
            print(json.dumps({"stage": "official readings verified", "date": str(args.date), "readings": meta["readings"]}), flush=True)
            texts = {}
            for language in languages:
                source = SCRIPTURES[language]
                books = bible(source["id"], work)
                texts[language] = [passage(books, r.get("spoken_reference", r["reference"])) for r in meta["assignments"]]
            if args.reuse_text:
                meta["liturgical_day_ml"] = old.get("liturgical_day_ml", "")
                meta["references_ml"] = old.get("references_ml", [])
                for language in languages:
                    if not (site / "episodes" / f"Homily_{args.date}_{language}.txt").exists():
                        raise ValueError("Existing text is missing")
            else:
                write_homilies(meta, texts, site)
            preserved = preserved_audio(site, old, languages)
            print(json.dumps({"stage": "narrating", "date": str(args.date), "voices": {lang: NARRATION[lang]["voice"] for lang in languages}}), flush=True)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(render_language, lang, meta, texts[lang], site, work) for lang in languages]
                meta["audio"] = preserved | dict(f.result() for f in futures)
            meta.update(format_version=2, generated_at=datetime.now(timezone.utc).isoformat(), schedule_timezone="America/Los_Angeles",
                        english_homily_words=meta["audio"]["English"]["homily_words"],
                        homily_max_seconds=MAX_HOMILY_SECONDS, homily_limit_excludes_readings=True,
                        prompt_sha256=old.get("prompt_sha256") if args.reuse_text and old.get("prompt_sha256") else hashlib.sha256(HOMILY_PROMPT.encode()).hexdigest(),
                        narration_config_sha256=hashlib.sha256(json.dumps(NARRATION, sort_keys=True).encode()).hexdigest())
            metadata_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            build_feed(site)
            if args.publish:
                day = args.date.isoformat()
                allowed = ["feed.xml", "index.html", f"episodes/{day}.json"]
                allowed += [f"episodes/{prefix}_{day}_{lang}.{suffix}" for lang in languages for prefix, suffix in [("Homily", "mp3"), ("Homily", "txt"), ("Readings", "txt"), ("Chapters", "json")]]
                git(site, "add", "--", *allowed, env=env)
                git(site, "-c", "user.name=VoiceAgent Homily", "-c", "user.email=jdayalu@users.noreply.github.com", "commit", "-m", "Publish readings and homily narration " + day, env=env)
                git(site, "push", "origin", "HEAD:main", env=env)
            print(json.dumps({"status": "published" if args.publish else "rendered", "date": str(args.date), "audio": meta["audio"]}, ensure_ascii=False))
        finally:
            if lease:
                lease.delete(if_generation_match=lease.generation)


if __name__ == "__main__":
    main()
