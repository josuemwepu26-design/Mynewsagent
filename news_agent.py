#!/usr/bin/env python3
"""
Free daily news digest -> your Gmail inbox.

  News feeds  ->  Gemini free tier (summaries)  ->  Gmail (email to yourself)

Runs on GitHub Actions (see news.yml). Secrets needed in your repo
(Settings -> Secrets and variables -> Actions):
  GEMINI_API_KEY      free key from Google AI Studio
  GMAIL_ADDRESS       your Gmail address
  GMAIL_APP_PASSWORD  16-character app password from myaccount.google.com/apppasswords
"""
import calendar
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests

# --------------------------------------------------------------------------
# SETTINGS (safe to edit)
# --------------------------------------------------------------------------
LANGUAGE = os.getenv("DIGEST_LANGUAGE", "English")   # "English" or "French"
TIMEZONE = "Africa/Kampala"
MAX_AGE_HOURS = 36       # ignore headlines older than this
PER_FEED = 6             # max headlines kept per feed
PER_REGION = 40          # max headlines sent to the AI per region

# AI provider: any OpenAI-compatible API works (default = Gemini free tier).
# To switch provider later, change only these three settings.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
LLM_MODELS = [m.strip() for m in os.getenv(
    "LLM_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite,gemini-flash-latest").split(",") if m.strip()]
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY", "")

DRY_RUN = os.getenv("DRY_RUN") == "1"                # print instead of sending
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")

# --------------------------------------------------------------------------
# WORDING
# --------------------------------------------------------------------------
TXT = {
    "English": {
        "heads": ["Politics", "Social", "Economy", "Technology"],
        "top": "Top stories", "digest": "News digest",
        "raw": "(AI summary unavailable, showing headlines)",
        "none": "No headlines found today.", "nothing": "Nothing notable today.",
    },
    "French": {
        "heads": ["Politique", "Social", "Économie", "Technologie"],
        "top": "À la une", "digest": "Revue de presse",
        "raw": "(Résumé IA indisponible, titres bruts)",
        "none": "Aucun titre trouvé aujourd'hui.", "nothing": "Rien de notable aujourd'hui.",
    },
}
L = TXT.get(LANGUAGE, TXT["English"])
TOPICS = ["politics", "social", "economy", "technology"]

# --------------------------------------------------------------------------
# NEWS SOURCES (add or remove freely)
# --------------------------------------------------------------------------
GN = "https://news.google.com/rss/search?q={q}+when:1d&hl={hl}&gl={gl}&ceid={ceid}"


def gnews(query, topic, edition, fallback=None):
    """Google News search feed (last 24h). 'fallback' edition is tried if the first is empty."""
    urls = [GN.format(q=quote_plus(query), hl=edition[0], gl=edition[1], ceid=edition[2])]
    if fallback:
        urls.append(GN.format(q=quote_plus(query), hl=fallback[0], gl=fallback[1], ceid=fallback[2]))
    return {"name": "Google News", "topic": topic, "urls": urls}


def rss(name, url, topic=None):
    return {"name": name, "topic": topic, "urls": [url]}


FR_CD, FR_FR = ("fr", "CD", "CD:fr"), ("fr", "FR", "FR:fr")
EN_UG, EN_US = ("en-UG", "UG", "UG:en"), ("en-US", "US", "US:en")

REGIONS = [
    {"name": "DR CONGO", "emoji": "🇨🇩", "feeds": [
        gnews("RDC politique", "politics", FR_CD, FR_FR),
        gnews("RDC société OR santé OR éducation", "social", FR_CD, FR_FR),
        gnews("RDC économie", "economy", FR_CD, FR_FR),
        gnews("RDC technologie OR numérique", "technology", FR_CD, FR_FR),
    ]},
    {"name": "UGANDA", "emoji": "🇺🇬", "feeds": [
        gnews("Uganda politics", "politics", EN_UG, EN_US),
        gnews("Uganda society OR health OR education", "social", EN_UG, EN_US),
        gnews("Uganda economy OR business", "economy", EN_UG, EN_US),
        gnews("Uganda technology OR digital", "technology", EN_UG, EN_US),
    ]},
    {"name": "AFRICA", "emoji": "🌍", "feeds": [
        rss("BBC Africa", "https://feeds.bbci.co.uk/news/world/africa/rss.xml"),
        gnews("Africa politics OR African Union", "politics", EN_US),
        gnews("Africa society OR health OR education", "social", EN_US),
        gnews("Africa economy OR business", "economy", EN_US),
        gnews("Africa tech OR startups", "technology", EN_US),
    ]},
    {"name": "WORLD", "emoji": "🌐", "feeds": [
        rss("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        rss("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        rss("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "economy"),
        rss("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "technology"),
        rss("TechCrunch", "https://techcrunch.com/feed/", "technology"),
    ]},
]

UA = {"User-Agent": "Mozilla/5.0 (compatible; PersonalNewsDigest/1.0)"}


# --------------------------------------------------------------------------
# 1) FETCH HEADLINES
# --------------------------------------------------------------------------
def fetch_feed(feed):
    """Return headlines from the first URL of the feed that works."""
    for url in feed["urls"]:
        try:
            r = requests.get(url, headers=UA, timeout=(5, 25))
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
        except requests.RequestException as e:
            code = getattr(getattr(e, "response", None), "status_code", "")
            print(f"   fetch failed: {urlparse(url).netloc} ({type(e).__name__} {code})")
            continue
        items = []
        for e in parsed.entries:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            source = (e.get("source") or {}).get("title") or feed["name"]
            if title.endswith(" - " + source):
                title = title[: -len(" - " + source)]
            ts = e.get("published_parsed") or e.get("updated_parsed")
            items.append({"title": title, "source": source, "topic": feed["topic"],
                          "ts": calendar.timegm(ts) if ts else None})
        if items:
            return items
    return []


def collect(region):
    """Fresh, de-duplicated headlines for one region."""
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    seen, items = set(), []
    for feed in region["feeds"]:
        got = [i for i in fetch_feed(feed) if i["ts"] is None or i["ts"] >= cutoff]
        got.sort(key=lambda i: i["ts"] or 0, reverse=True)
        print(f" - {feed['name']} [{feed['topic'] or 'general'}]: {len(got)} fresh")
        kept = 0
        for it in got:
            key = re.sub(r"\W+", "", it["title"].lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
            kept += 1
            if kept >= PER_FEED:
                break
    return items[:PER_REGION]


# --------------------------------------------------------------------------
# 2) SUMMARIZE (free AI tier)
# --------------------------------------------------------------------------
def build_prompt(region, items, today):
    heads = L["heads"]
    lines = [f"{n}. [{i['topic'] or 'general'}] {i['title']} ({i['source']})"
             for n, i in enumerate(items, 1)]
    return (
        f"You edit a personal news digest.\n"
        f"Region: {region['name']}. Date: {today}. Write in {LANGUAGE}.\n\n"
        f"Below are today's headlines (number, topic hint, headline, source). "
        f"They may be in French or English.\n\n"
        f"Write the digest under exactly these four headings, in this order: {', '.join(heads)}.\n"
        f"Rules:\n"
        f"- Use ONLY what the headlines say. Never invent facts, numbers, names or background.\n"
        f"- 2 to 3 bullets per heading, most important first. Merge headlines about the same story.\n"
        f"- Each bullet: one plain sentence of at most 25 words, then the source in brackets.\n"
        f"- If a heading has nothing relevant, write: • {L['nothing']}\n"
        f"- Plain text only: headings in single asterisks like *{heads[0]}*, "
        f"bullets start with \"• \". No markdown headers, tables or links.\n"
        f"- Output only the digest, no intro or outro.\n\n"
        f"Headlines:\n" + "\n".join(lines)
    )


def call_llm(prompt):
    """Try each model in turn. Returns text, or None if every model failed."""
    if not LLM_API_KEY:
        print("   no AI key set -> plain headlines")
        return None
    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    for model in LLM_MODELS:
        for attempt in range(2):
            try:
                r = requests.post(
                    url, headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={"model": model, "temperature": 0.2,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=120)
            except requests.RequestException as e:
                print(f"   {model}: network error ({type(e).__name__})")
                time.sleep(5)
                continue
            if r.status_code == 200:
                try:
                    text = r.json()["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError):
                    text = None
                if text and text.strip():
                    print(f"   summarized with {model}")
                    return text.strip()
                print(f"   {model}: empty answer")
                break
            print(f"   {model}: HTTP {r.status_code} {r.text[:200]}")
            daily = "perday" in r.text.lower().replace(" ", "")
            if r.status_code == 429 and not daily and attempt == 0:
                time.sleep(25)                       # per-minute limit: wait, retry once
                continue
            if r.status_code >= 500 and attempt == 0:
                time.sleep(5)
                continue
            break                                    # daily quota gone / bad model -> next model
    return None


def clean(text):
    """Turn typical markdown into plain-text formatting."""
    text = text.replace("```", "").replace("**", "*")
    text = re.sub(r"(?m)^#+\s*(.+?)\s*$", r"*\1*", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
    return text.strip()


def raw_digest(items):
    """Fallback when the AI is unavailable: grouped plain headlines."""
    out = ["(" + L["raw"] + ")"]
    for key, label in zip(TOPICS, L["heads"]):
        picks = [i for i in items if i["topic"] == key][:3]
        if picks:
            out += [f"*{label}*"] + [f"• {i['title']} ({i['source']})" for i in picks]
    general = [i for i in items if not i["topic"]][:6]
    if general:
        out += [f"*{L['top']}*"] + [f"• {i['title']} ({i['source']})" for i in general]
    return "\n".join(out)


# --------------------------------------------------------------------------
# 3) SEND BY EMAIL (Gmail, to yourself)
# --------------------------------------------------------------------------
def send_email(text, subject):
    """Email the digest to your own Gmail. Retries and falls back to port 587."""
    if DRY_RUN:
        print("\n----- Email (dry run) -----\nSubject: " + subject + "\n\n" + text)
        return True
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_ADDRESS
    msg.set_content(text)

    # attempt 1: SSL on 465, attempt 2: STARTTLS on 587, attempt 3: 465 again
    methods = [("smtp.gmail.com", 465, "ssl"), ("smtp.gmail.com", 587, "starttls")]
    for attempt in range(3):
        host, port, mode = methods[attempt % 2]
        try:
            if mode == "ssl":
                s = smtplib.SMTP_SSL(host, port, timeout=60)
            else:
                s = smtplib.SMTP(host, port, timeout=60)
                s.starttls()
            with s:
                s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                s.send_message(msg)
            print(f"   Email sent (port {port})")
            return True
        except smtplib.SMTPAuthenticationError as e:
            # Wrong address or app password: retrying won't help
            print(f"   Email login rejected ({e.smtp_code}). Check GMAIL_ADDRESS and GMAIL_APP_PASSWORD.")
            return False
        except (smtplib.SMTPException, OSError) as e:
            print(f"   Email attempt {attempt + 1} failed on port {port} ({type(e).__name__}: {e})")
            time.sleep(10)
    return False


# --------------------------------------------------------------------------
def main():
    if not DRY_RUN and not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        sys.exit("Missing GMAIL_ADDRESS or GMAIL_APP_PASSWORD secret.")
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%d/%m/%Y")
    sections, any_news = [], False
    for region in REGIONS:
        print(f"\n== {region['name']}")
        items = collect(region)
        if items:
            any_news = True
            summary = call_llm(build_prompt(region, items, today))
            body = clean(summary) if summary else raw_digest(items)
            time.sleep(8)                # stay under free-tier per-minute limits
        else:
            body = L["none"]
        sections.append(f"{region['emoji']} {region['name']}\n\n{body}")

    subject = f"{L['digest']} {today}"
    all_ok = send_email("\n\n".join(sections), subject)
    sys.exit(0 if (all_ok and any_news) else 1)


if __name__ == "__main__":
    main()
