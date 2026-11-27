#!/usr/bin/env python3
"""Daily Homily toolkit (one file). Subcommands:
  readings <calendar.txt> <YYYY-MM-DD>       print today's entry from the official calendar text
  tts-en   <homily.txt> <out.wav>             Kokoro am_michael, 24 kHz (skips 3 header lines)
  tts-ml   <homily.txt> <out.wav>             Meta MMS facebook/mms-tts-mal, 16 kHz (skips 3 header lines)
  cover    <out.jpg>                          3000x3000 podcast cover
  feed     <site_dir> <base_url> [keep_days]  build feed.xml + index.html, prune old episodes
"""
import sys, os, re, json, html, glob, subprocess, datetime as dt

def readings(txt, iso):
    d = dt.date.fromisoformat(iso)
    L = open(txt, encoding="utf-8").read().split("\n")
    start = next(i for i, l in enumerate(L) if l.strip() == d.strftime("%B %Y"))
    day_re = re.compile(r"^\s*(\d{1,2})\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b\s*(.*)")
    junk = re.compile(r"Syro-Malabar Liturgical Calendar|^\s*(January|February|March|April|May|June|July|"
                      r"August|September|October|November|December) \d{4}\s*$|^\s*\d{4}\s+[A-Z][a-z]+\s+\d+\s*$|"
                      r"^\s*[A-Z][A-Z \-()]{8,}\s*$")
    out, on = [], False
    for l in L[start:]:
        if on and re.match(r"^\s*Appendix\b", l): break
        m = day_re.match(l)
        if m:
            if on: break
            if int(m.group(1)) == d.day and m.group(2) == d.strftime("%a"):
                on = True; out.append(m.group(3).strip()); continue
        if on and l.strip() and not junk.search(l):
            out.append(re.sub(r"\s+", " ", l.strip()))
    if not out: sys.exit("NO ENTRY")
    print("\n".join(out))

def _paras(path):
    lines = open(path, encoding="utf-8").read().split("\n")[3:]
    return [p.strip().replace("\n", " ") for p in "\n".join(lines).split("\n\n") if p.strip()]

def _write(chunks, sr, out, peak=None):
    import numpy as np, soundfile as sf
    w = np.concatenate(chunks)
    if peak: w = peak * w / max(1e-9, float(np.max(np.abs(w))))
    sf.write(out, w, sr)

def tts_en(src, out):
    import numpy as np
    from kokoro_onnx import Kokoro
    d = os.environ.get("KOKORO_DIR", "/home/claude/tts")
    k = Kokoro(f"{d}/kokoro-v1.0.onnx", f"{d}/voices-v1.0.bin"); sr = 24000; ch = []
    for p in _paras(src):
        ss = [s for s in re.split(r"(?<=[.!?])\s+", p) if s.strip()]
        for i, s in enumerate(ss):
            a, _ = k.create(s, voice="am_michael", speed=0.92, lang="en-us")
            ch += [a.astype(np.float32), np.zeros(int(sr * (0.6 if i == len(ss) - 1 else 0.35)), np.float32)]
    _write(ch, sr, out)

def tts_ml(src, out):
    import numpy as np, torch
    from transformers import VitsModel, AutoTokenizer
    torch.manual_seed(7)
    m = VitsModel.from_pretrained("facebook/mms-tts-mal"); t = AutoTokenizer.from_pretrained("facebook/mms-tts-mal")
    m.config.speaking_rate = 0.95; m.config.noise_scale = 0.6; m.speaking_rate = 0.95; m.noise_scale = 0.6
    sr = 16000; ch = []
    for p in _paras(src):
        p = re.sub(r"[\"“”‘’']", "", p.replace("—", ","))
        ss = [s.strip() for s in re.split(r"[.?!:]", p) if s.strip(" ,")]
        for i, s in enumerate(ss):
            with torch.no_grad():
                a = m(**t(s, return_tensors="pt")).waveform[0].numpy().astype(np.float32)
            ch += [a, np.zeros(int(sr * (0.6 if i == len(ss) - 1 else 0.35)), np.float32)]
    _write(ch, sr, out, peak=0.9)

def cover(out):
    from PIL import Image, ImageDraw, ImageFont
    W = 3000; im = Image.new("RGB", (W, W), (122, 28, 36)); d = ImageDraw.Draw(im); gold = (232, 196, 120)
    d.rectangle([1440, 520, 1560, 1640], fill=gold); d.rectangle([1120, 820, 1880, 940], fill=gold)
    f = sorted(glob.glob("/usr/share/fonts/**/DejaVuSerif-Bold.ttf", recursive=True)) or \
        sorted(glob.glob("/usr/share/fonts/**/*Bold*.ttf", recursive=True))
    for txt, y, s in [("DAILY HOMILY", 1900, 260), ("Syro-Malabar · English & Malayalam", 2280, 110)]:
        F = ImageFont.truetype(f[0], s); w = d.textlength(txt, font=F)
        d.text(((W - w) / 2, y), txt, font=F, fill=(250, 240, 225))
    im.save(out, quality=88)

def feed(site, base, keep=45):
    from email.utils import format_datetime
    from zoneinfo import ZoneInfo
    base = base.rstrip("/"); keep = int(keep); ep = os.path.join(site, "episodes"); LA = ZoneInfo("America/Los_Angeles")
    cutoff = (dt.datetime.now(LA) - dt.timedelta(days=keep)).date().isoformat()
    for f in os.listdir(ep):
        d = f.split("_")[1] if f.startswith("Homily_") else f[:10]
        if len(d) == 10 and d < cutoff: os.remove(os.path.join(ep, f))
    items, rows = [], []
    for mf in sorted((f for f in os.listdir(ep) if f.endswith(".json") and len(f) == 15 and f[:4].isdigit()), reverse=True):
        m = json.load(open(os.path.join(ep, mf), encoding="utf-8")); d = m["date"]
        for i, lang in enumerate(["English", "Malayalam"]):
            fn = f"Homily_{d}_{lang}.mp3"; p = os.path.join(ep, fn)
            if not os.path.exists(p): continue
            s = int(round(float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "csv=p=0", p]).decode())))
            hms = f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
            pub = dt.datetime.fromisoformat(d).replace(hour=4, minute=1 - i, tzinfo=LA)
            title = f"{dt.date.fromisoformat(d):%a %b} {int(d[8:])} · {lang} · {m['liturgical_day']}"
            desc = f"{m['liturgical_day']} ({m['weekday']}). Readings: {m['readings']}. Gospel theme: {m['theme']}."
            if lang == "Malayalam" and m.get("liturgical_day_ml"): desc = m["liturgical_day_ml"] + ". " + desc
            txt = f"Homily_{d}_{lang}.txt"
            if os.path.exists(os.path.join(ep, txt)): desc += f" Text: {base}/episodes/{txt}"
            items.append(f"""  <item>
    <title>{html.escape(title)}</title>
    <description>{html.escape(desc)}</description>
    <enclosure url="{base}/episodes/{fn}" length="{os.path.getsize(p)}" type="audio/mpeg"/>
    <guid isPermaLink="false">homily-{d}-{lang.lower()}</guid>
    <pubDate>{format_datetime(pub)}</pubDate>
    <itunes:duration>{hms}</itunes:duration>
    <itunes:episodeType>full</itunes:episodeType>
    <itunes:explicit>false</itunes:explicit>
  </item>""")
            tl = f' · <a href="episodes/{txt}">text</a>' if os.path.exists(os.path.join(ep, txt)) else ""
            rows.append(f'<li><b>{html.escape(title)}</b> ({s // 60}:{s % 60:02d}){tl}<br>'
                        f'<audio controls preload="none" src="episodes/{fn}"></audio></li>')
    now = format_datetime(dt.datetime.now(dt.timezone.utc))
    open(os.path.join(site, "feed.xml"), "w", encoding="utf-8").write(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Daily Homily – Syro-Malabar</title>
  <link>{base}/</link>
  <atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml"/>
  <language>en</language>
  <description>Daily Syro-Malabar homilies in English and Malayalam, following the official liturgical calendar readings.</description>
  <itunes:author>Daily Homily</itunes:author>
  <itunes:image href="{base}/cover.jpg"/>
  <itunes:category text="Religion &amp; Spirituality"><itunes:category text="Christianity"/></itunes:category>
  <itunes:explicit>false</itunes:explicit>
  <itunes:block>Yes</itunes:block>
  <lastBuildDate>{now}</lastBuildDate>
{chr(10).join(items)}
</channel>
</rss>
""")
    open(os.path.join(site, "index.html"), "w", encoding="utf-8").write(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Daily Homily</title>
<style>body{{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;padding:16px;background:#fbf7f2;color:#222}}
img{{width:120px;border-radius:12px}}li{{margin:14px 0;list-style:none}}audio{{width:100%;margin-top:6px}}ul{{padding:0}}code{{word-break:break-all}}</style></head>
<body><img src="cover.jpg" alt=""><h1>Daily Homily</h1>
<p>Podcast feed: <code>{base}/feed.xml</code><br>Apple Podcasts → Library → ⋯ → Follow a Show by URL.</p>
<ul>{''.join(rows)}</ul></body></html>
""")
    print(f"{len(items)} episodes in feed")

if __name__ == "__main__":
    cmd, *a = sys.argv[1:]
    {"readings": readings, "tts-en": tts_en, "tts-ml": tts_ml, "cover": cover, "feed": feed}[cmd](*a)
