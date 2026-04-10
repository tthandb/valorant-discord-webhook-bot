import os
import sys
import json
import time
import logging
import requests
import schedule
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
RSS_URL = "https://gameriv.com/valorant/feed/"


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen_article_links": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# RSS feed
# ---------------------------------------------------------------------------

def fetch_articles():
    """Fetch Valorant patch notes and leaks from Gameriv RSS feed."""
    try:
        resp = requests.get(RSS_URL, timeout=15, headers={"User-Agent": "ValorantDiscordBot/1.0"})
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Failed to fetch RSS: %s", e)
        return []

    root = ET.fromstring(resp.text)
    articles = []
    for item in root.findall(".//item"):
        title = (item.find("title").text or "").strip()
        link = (item.find("link").text or "").strip()
        pub_date = (item.find("pubDate").text or "").strip()
        categories = [c.text.lower() for c in item.findall("category") if c.text]
        desc_raw = item.find("description").text or ""

        # Only include patch notes and leaks
        is_patch = "patch" in title.lower()
        is_leak = "leaks" in categories or "leak" in title.lower()
        if not is_patch and not is_leak:
            continue

        # Clean HTML, extract summary
        desc_text = BeautifulSoup(desc_raw, "html.parser").get_text()
        summary = ""
        for line in desc_text.split("\n"):
            line = line.strip()
            if len(line) > 50 and "gameriv" not in line.lower() and "the post" not in line.lower():
                summary = line
                break

        articles.append({
            "title": title,
            "link": link,
            "summary": summary,
            "pub_date": pub_date,
            "is_leak": is_leak,
        })

    return articles


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------

def send_webhook(embeds, webhook_url=None, thread_name=None, applied_tags=None, content=None):
    url = webhook_url
    if not url:
        log.error("Webhook URL not configured")
        return False

    payload = {"embeds": embeds}
    if thread_name:
        payload["thread_name"] = thread_name[:100]
    if applied_tags:
        payload["applied_tags"] = applied_tags
    if content:
        payload["content"] = content[:2000]

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                return True
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 5)
                log.warning("Rate limited, waiting %.1fs", retry_after)
                time.sleep(retry_after)
                continue
            log.error("Webhook returned %s: %s", resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            log.warning("Webhook attempt %d failed: %s", attempt + 1, e)
            time.sleep(2 ** attempt)

    return False
