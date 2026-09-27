"""
AI Radar news scanner.
Reads RSS feeds from scripts/feeds.json, keeps AI-related stories, removes duplicates,
optionally writes a Hebrew headline + one-line summary with Claude, and saves data/news.json.

Only the headline, a short original summary and a link to the source are stored,
never the article text itself.
"""
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "scripts" / "feeds.json"
NEWS_FILE = ROOT / "data" / "news.json"

MAX_ITEMS = 400          # how many stories to keep in total
MAX_AGE_DAYS = 45        # drop automatic stories older than this
MAX_TRANSLATE = 25       # max new stories sent to Claude per run (cost control)
CATEGORIES = ["מודלים חדשים", "חברות גדולות", "מוצרים ששוברים את השוק",
              "מחקר ופריצות דרך", "בטיחות ורגולציה"]

AI_WORDS = re.compile(
    r"\b(ai|a\.i\.|artificial intelligence|llm|gpt|chatgpt|claude|gemini|openai|anthropic|"
    r"deepmind|copilot|mistral|llama|qwen|deepseek|machine learning|neural|agent|agents|"
    r"generative|chatbot|hugging ?face|nvidia)\b|בינה מלאכותית|\bAI\b|צ'אטבוט|מודל שפה",
    re.IGNORECASE,
)


def log(*a):
    print(*a, file=sys.stderr)


def clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def story_id(url):
    return hashlib.sha1(url.split("?")[0].rstrip("/").encode()).hexdigest()[:12]


def entry_date(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def load_existing():
    if NEWS_FILE.exists():
        return json.loads(NEWS_FILE.read_text(encoding="utf-8")).get("items", [])
    return []


def scan(feeds):
    found = []
    for f in feeds:
        try:
            parsed = feedparser.parse(f["url"], agent="AI-Radar/1.0 (+github pages)")
            if parsed.bozo and not parsed.entries:
                raise ValueError(parsed.bozo_exception)
        except Exception as err:  # one broken feed must not stop the rest
            log(f"skip {f['name']}: {err}")
            continue
        for e in parsed.entries[:30]:
            url = e.get("link")
            title = clean(e.get("title"), 200)
            if not url or not title:
                continue
            snippet = clean(e.get("summary"))
            if not f.get("all_ai") and not AI_WORDS.search(title + " " + snippet):
                continue
            found.append({
                "id": story_id(url),
                "type": "news",
                "title": title,
                "title_orig": title,
                "snippet": snippet,           # used only for the summary, removed before saving
                "url": url,
                "source": f["name"],
                "lang": f.get("lang", "en"),
                "date": entry_date(e).strftime("%Y-%m-%d"),
            })
        log(f"{f['name']}: ok")
    return found


def hebrewize(stories):
    """Ask Claude for a Hebrew headline, a one-line summary and a category. Optional."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not stories:
        return
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    batch = stories[:MAX_TRANSLATE]
    payload = [{"i": n, "title": s["title_orig"], "snippet": s["snippet"][:300], "source": s["source"]}
               for n, s in enumerate(batch)]
    prompt = (
        "You edit a Hebrew news site about AI. For each story below return a Hebrew headline, "
        "one short Hebrew sentence explaining what happened (in your own words, based only on the "
        "title and snippet, do not invent details), and exactly one category from this list: "
        + ", ".join(CATEGORIES)
        + ". Keep product and company names in English.\n"
        'Reply with JSON only: [{"i":0,"title":"...","desc":"...","category":"..."}]\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        msg = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        for r in json.loads(text):
            s = batch[int(r["i"])]
            s["title"] = r.get("title") or s["title"]
            s["desc"] = r.get("desc", "")
            if r.get("category") in CATEGORIES:
                s["category"] = r["category"]
            s["lang"] = "he"
    except Exception as err:
        log(f"Claude step failed, keeping original headlines: {err}")


def main():
    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    existing = load_existing()
    known = {s["id"] for s in existing} | {s.get("url") for s in existing}

    fresh, seen = [], set()
    for s in scan(feeds):
        if s["id"] in known or s["url"] in known or s["id"] in seen:
            continue
        seen.add(s["id"])
        fresh.append(s)
    fresh.sort(key=lambda s: s["date"], reverse=True)
    log(f"new stories: {len(fresh)}")

    hebrewize([s for s in fresh if s["lang"] != "he"])
    for s in fresh:
        s.pop("snippet", None)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    items = fresh + existing
    items = [s for s in items if s.get("manual") or s.get("date", "9999") >= cutoff]
    items.sort(key=lambda s: s.get("date", ""), reverse=True)
    items = items[:MAX_ITEMS]

    NEWS_FILE.parent.mkdir(exist_ok=True)
    NEWS_FILE.write_text(json.dumps(
        {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items},
        ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"saved {len(items)} stories")


if __name__ == "__main__":
    main()
