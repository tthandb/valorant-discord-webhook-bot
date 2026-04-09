"""
Valorant Forum Post — standalone script.

Creates forum posts in a Discord Forum channel for each new patch note,
with full article content and category-based tags.

Run modes:
    python forum_post.py           # single check + continuous polling
    python forum_post.py --once    # single check and exit (GitHub Actions)
"""

import os
import sys
import json
import logging
import time
from email.utils import parsedate_to_datetime

import requests
import schedule
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

from main import fetch_articles, send_webhook, load_state, save_state

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FORUM_WEBHOOK_URL = os.getenv("FORUM_WEBHOOK_URL", "")
try:
    FORUM_TAGS = json.loads(os.getenv("FORUM_TAGS", "{}"))
except json.JSONDecodeError:
    FORUM_TAGS = {}
POLL_MINUTES = int(os.getenv("PATCH_NOTES_POLL_MINUTES", "30"))

COLOR_BLUE = 0x4488FF
COLOR_PURPLE = 0x9B59B6

BOILERPLATE = ["leave a reply", "related", "comment", "final thought",
               "wrapping up", "share this", "subscribe"]


# ---------------------------------------------------------------------------
# Article scraper — concise summaries with image
# ---------------------------------------------------------------------------

def scrape_article(url):
    """Scrape article page. Returns (formatted_text, image_url)."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "ValorantDiscordBot/1.0"})
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Failed to scrape %s: %s", url, e)
        return None, None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract og:image for embed thumbnail
    og = soup.find("meta", property="og:image")
    image_url = og["content"] if og and og.get("content") else None

    content = soup.find("div", class_="entry-content") or soup.find("article")
    if not content:
        return None, image_url

    lines = []
    current_heading = None

    for el in content.children:
        if not hasattr(el, "name") or not el.name:
            continue

        text = el.get_text(strip=True)
        if not text:
            continue

        if any(s in text.lower() for s in BOILERPLATE):
            break

        if el.name in ("h2", "h3"):
            current_heading = text
            lines.append(f"\n> **{text}**")

        elif el.name == "p" and len(text) > 20:
            # Condense: first sentence per paragraph as a bullet point
            sentences = text.replace("...", "\u2026").split(". ")
            first = sentences[0].rstrip(".")
            if not first.endswith(("\u2026", "!", "?", '"')):
                first += "."
            lines.append(f"- {first}")

        elif el.name == "ul":
            for li in el.find_all("li", recursive=False):
                t = li.get_text(strip=True)
                if t:
                    lines.append(f"  - {t[:200]}")

        elif el.name == "ol":
            for i, li in enumerate(el.find_all("li", recursive=False), 1):
                t = li.get_text(strip=True)
                if t:
                    lines.append(f"  {i}. {t[:200]}")

    result = "\n".join(lines).strip()
    return (result or None), image_url


# ---------------------------------------------------------------------------
# Forum post builder
# ---------------------------------------------------------------------------

def build_forum_post(article):
    """Build forum embed with concise article content, image, and tags."""
    link = article["link"]
    is_leak = article.get("is_leak", False)
    pub_date = article["pub_date"]
    rss_summary = article.get("summary", "")

    # Format publish date as DD-MM-YYYY for thread preview
    date_str = ""
    if pub_date:
        try:
            date_str = parsedate_to_datetime(pub_date).strftime("%d-%m-%Y")
        except (ValueError, TypeError):
            pass

    detailed, image_url = scrape_article(link)

    if detailed:
        description = detailed[:4000] + f"\n\n[Read full article]({link})"
    elif rss_summary:
        description = f"{rss_summary}\n\n[Read full article]({link})"
    else:
        description = f"[Read full article]({link})"

    color = COLOR_PURPLE if is_leak else COLOR_BLUE
    footer = "VALORANT Leaks" if is_leak else "VALORANT Patch Notes"

    embed = {
        "title": f"{'🔮' if is_leak else '📋'} {article['title']}"[:256],
        "description": description[:4096],
        "color": color,
        "url": link,
        "footer": {"text": footer},
    }

    if image_url:
        embed["image"] = {"url": image_url}

    if pub_date:
        try:
            embed["timestamp"] = parsedate_to_datetime(pub_date).isoformat()
        except (ValueError, TypeError):
            pass

    # Determine tags from RSS categories (only use valid snowflake IDs)
    tags = []
    if is_leak and FORUM_TAGS.get("leak", "").isdigit():
        tags.append(FORUM_TAGS["leak"])
    elif FORUM_TAGS.get("patch", "").isdigit():
        tags.append(FORUM_TAGS["patch"])

    # Date as message content — shows in forum thread preview/thumbnail
    content = f"Published: {date_str}" if date_str else None

    return embed, tags, content


# ---------------------------------------------------------------------------
# Forum post checker
# ---------------------------------------------------------------------------

def check_forum_posts(force=False):
    """Check for new articles and create forum posts."""
    log.info("Checking for new articles (forum)...")

    articles = fetch_articles()
    if not articles:
        log.info("No patch notes or leaks found in RSS feed.")
        return

    # RSS returns newest first; reverse so oldest posts first,
    # making the newest thread appear at the top of the forum.
    articles = list(reversed(articles))


    state = load_state()
    seen = state.get("seen_forum_links", [])
    first_run = len(seen) == 0
    posted = 0
    max_posts = len(articles) if force else (1 if first_run else 3)

    for article in articles:
        if posted >= max_posts:
            break

        link = article["link"]
        if not force and link in seen:
            continue

        embed, tags, content = build_forum_post(article)
        forum_title = article["title"][:100]

        if send_webhook(
            [embed],
            webhook_url=FORUM_WEBHOOK_URL,
            thread_name=forum_title,
            applied_tags=tags or None,
            content=content,
        ):
            if not force:
                seen.append(link)
            posted += 1
            tag = "leak" if article.get("is_leak") else "patch"
            log.info("Forum post created [%s]: %s", tag, article["title"])
        else:
            log.error("Forum post failed: %s", article["title"])

    if not force:
        state["seen_forum_links"] = seen[-100:]
        save_state(state)
    log.info("Forum check complete. Created %d posts.", posted)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    once = "--once" in args
    now = "--now" in args

    if not FORUM_WEBHOOK_URL:
        raise SystemExit("Error: FORUM_WEBHOOK_URL not set.")

    if now:
        log.info("Running in --now mode (force post, no state changes)...")
        check_forum_posts(force=True)
    elif once:
        log.info("Running forum post check...")
        check_forum_posts()
    else:
        log.info("Forum Post Bot starting (poll every %dm)...", POLL_MINUTES)
        check_forum_posts()
        schedule.every(POLL_MINUTES).minutes.do(check_forum_posts)
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Bot stopped.")

    log.info("Done.")


if __name__ == "__main__":
    main()
