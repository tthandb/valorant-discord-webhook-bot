import os
import sys
import json
import time
import logging
import requests
import schedule
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from daily_shop import riot_auth_from_cookie, fetch_daily_shop, fetch_skin_info, build_shop_embeds

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("PATCH_NOTES_WEBHOOK_URL", "")
PATCH_NOTES_POLL_MINUTES = int(os.getenv("PATCH_NOTES_POLL_MINUTES", "30"))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

RSS_URL = "https://gameriv.com/valorant/feed/"
COLOR_BLUE = 0x4488FF
COLOR_PURPLE = 0x9B59B6

# Daily shop config (optional — feature disabled if not set)
DAILY_SHOP_WEBHOOK_URL = os.getenv("DAILY_SHOP_WEBHOOK_URL", "")
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riot_accounts.json")


def load_accounts():
    """Load Riot accounts from env var, JSON file, or .env single account."""
    accounts = []

    # 1. From RIOT_ACCOUNTS env var (GitHub Actions - JSON string)
    env_accounts = os.getenv("RIOT_ACCOUNTS", "")
    if env_accounts:
        try:
            accounts = json.loads(env_accounts)
        except json.JSONDecodeError:
            pass

    # 2. From riot_accounts.json file
    if not accounts:
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                accounts = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    # 3. Fallback: single account from .env (backward compatible)
    if not accounts:
        ssid = os.getenv("RIOT_SSID_COOKIE", "")
        if ssid:
            accounts = [{
                "ssid_cookie": ssid,
                "region": os.getenv("RIOT_REGION", "ap"),
                "name": os.getenv("RIOT_DISPLAY_NAME", ""),
            }]

    return accounts


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
# Article scraper
# ---------------------------------------------------------------------------

def scrape_article_summary(url):
    """Scrape an article page and extract key bullet-point changes."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "ValorantDiscordBot/1.0"})
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Failed to scrape %s: %s", url, e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    content = soup.find("div", class_="entry-content") or soup.find("article")
    if not content:
        return None

    # Strategy: find the TL;DR summary list in patch notes,
    # or collect bullet lists grouped by heading for leaks/other articles.

    # 1) Look for a TL;DR summary list (Gameriv patch notes pattern):
    #    a paragraph with "real changes" or after April Fools disclaimer,
    #    followed by a short <ul> with concise items.
    tldr_list = None
    for p in content.find_all("p"):
        text = p.get_text(strip=True).lower()
        if "real changes" in text or "not real" in text or "april fools" in text:
            # Find the next <ul> after this paragraph
            ul = p.find_next_sibling("ul")
            if ul:
                items = [li.get_text(strip=True) for li in ul.find_all("li", recursive=False)]
                if 2 <= len(items) <= 10:
                    tldr_list = items
                    break

    # 2) Collect detailed changes from h3 subsections (ALL PLATFORMS, PC ONLY, etc.)
    sections = []
    in_full_notes = False
    for heading in content.find_all(["h2", "h3"]):
        text = heading.get_text(strip=True)
        text_lower = text.lower()

        # Stop at boilerplate
        if any(s in text_lower for s in ["leave a reply", "related", "comment"]):
            break

        # Start collecting once we hit the "Full patch notes" section
        if "full" in text_lower and "patch" in text_lower:
            in_full_notes = True
            continue

        # For h3 subsections within the full notes, collect bullet items
        if heading.name == "h3" and in_full_notes:
            items = []
            for sib in heading.find_next_siblings():
                if sib.name in ["h2", "h3"]:
                    break
                if sib.name == "ul":
                    for li in sib.find_all("li", recursive=False):
                        t = li.get_text(strip=True)
                        if t and len(t) > 10:
                            items.append(t)
            if items:
                sections.append((text, items))

    # 3) If no full notes section found, collect lists from any h2/h3
    if not sections and not tldr_list:
        for heading in content.find_all(["h2", "h3"]):
            text = heading.get_text(strip=True)
            text_lower = text.lower()
            if any(s in text_lower for s in ["leave a reply", "related", "comment",
                                              "final thought", "wrapping up"]):
                break
            items = []
            for sib in heading.find_next_siblings():
                if sib.name in ["h2", "h3"]:
                    break
                if sib.name == "ul":
                    for li in sib.find_all("li", recursive=False):
                        t = li.get_text(strip=True)
                        if t and len(t) > 10:
                            items.append(t)
            if items:
                sections.append((text, items))

    # Build output
    lines = []

    if tldr_list:
        lines.append("__**Summary**__")
        for item in tldr_list:
            lines.append(f"- {item}")

    for name, items in sections:
        lines.append(f"\n__**{name}**__")
        for item in items[:8]:
            lines.append(f"- {item[:150]}")
        if len(items) > 8:
            lines.append(f"*... +{len(items) - 8} more*")

    summary = "\n".join(lines).strip()
    return summary[:3500] if summary else None


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------

def send_webhook(embeds, webhook_url=None):
    url = webhook_url or WEBHOOK_URL
    if not url:
        log.error("Webhook URL not configured")
        return False

    payload = {"embeds": embeds}

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


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------

def build_embed(article):
    """Build Discord embed from an RSS article with full scraped summary."""
    title = article["title"]
    link = article["link"]
    rss_summary = article["summary"]
    pub_date = article["pub_date"]
    is_leak = article.get("is_leak", False)

    # Scrape the full article for a detailed summary
    detailed = scrape_article_summary(link)

    if detailed:
        description = f"{detailed}\n\n🔗 {link}"
    elif rss_summary:
        description = f"{rss_summary}\n\n🔗 {link}"
    else:
        description = f"🔗 {link}"

    if is_leak:
        icon = "🔮"
        color = COLOR_PURPLE
        footer = "VALORANT Leaks"
    else:
        icon = "📋"
        color = COLOR_BLUE
        footer = "VALORANT Patch Notes"

    embed = {
        "title": f"{icon} {title}"[:256],
        "description": description[:4096],
        "color": color,
        "url": link,
        "footer": {"text": footer},
    }

    if pub_date:
        try:
            embed["timestamp"] = parsedate_to_datetime(pub_date).isoformat()
        except (ValueError, TypeError):
            pass

    return embed


# ---------------------------------------------------------------------------
# Patch notes checker
# ---------------------------------------------------------------------------

def check_articles():
    log.info("Checking for new articles...")

    articles = fetch_articles()
    if not articles:
        log.info("No patch notes or leaks found in RSS feed.")
        return

    state = load_state()
    first_run = len(state["seen_article_links"]) == 0
    posted = 0
    max_posts = 1 if first_run else 3

    for article in articles:
        if posted >= max_posts:
            break

        link = article["link"]
        if link in state["seen_article_links"]:
            continue

        embed = build_embed(article)
        if send_webhook([embed]):
            state["seen_article_links"].append(link)
            posted += 1
            tag = "leak" if article.get("is_leak") else "patch"
            log.info("Posted [%s]: %s", tag, article["title"])

    state["seen_article_links"] = state["seen_article_links"][-100:]
    save_state(state)
    log.info("Check complete. Posted %d new articles.", posted)


# ---------------------------------------------------------------------------
# Daily shop checker
# ---------------------------------------------------------------------------

def check_daily_shop():
    if not DAILY_SHOP_WEBHOOK_URL:
        return

    accounts = load_accounts()
    if not accounts:
        return

    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    shop_state = state.get("shop_posted", {})

    posted_any = False
    for i, account in enumerate(accounts):
        ssid = account.get("ssid_cookie", "")
        if not ssid:
            continue

        region = account.get("region", "ap")
        override_name = account.get("name", "")
        account_key = f"account_{i}"

        if shop_state.get(account_key) == today:
            continue

        log.info("Checking daily shop for account %d...", i + 1)

        try:
            access_token, entitlements_token, puuid, fetched_name = riot_auth_from_cookie(ssid)
        except Exception as e:
            log.error("Riot auth failed for account %d: %s", i + 1, e)
            continue

        try:
            shop_data = fetch_daily_shop(access_token, entitlements_token, puuid, region)
        except Exception as e:
            log.error("Failed to fetch daily shop for account %d: %s", i + 1, e)
            continue

        account_name = override_name or fetched_name or "Player"
        skins = [fetch_skin_info(uuid) for uuid in shop_data["skin_uuids"]]
        embeds = build_shop_embeds(account_name, skins, shop_data)

        if send_webhook(embeds, webhook_url=DAILY_SHOP_WEBHOOK_URL):
            shop_state[account_key] = today
            posted_any = True
            log.info("Daily shop posted for %s (%d skins).", account_name, len(skins))
        else:
            log.error("Failed to send daily shop webhook for %s.", account_name)

    if posted_any:
        state["shop_posted"] = shop_state
        save_state(state)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    once = "--once" in args
    only_articles = "--articles" in args
    only_shop = "--shop" in args

    if once:
        if only_articles:
            if not WEBHOOK_URL:
                raise SystemExit("Error: PATCH_NOTES_WEBHOOK_URL not set.")
            log.info("Running article check...")
            check_articles()
        elif only_shop:
            log.info("Running daily shop check...")
            check_daily_shop()
        else:
            # Default: run both
            if not WEBHOOK_URL:
                raise SystemExit("Error: PATCH_NOTES_WEBHOOK_URL not set.")
            log.info("Running all checks...")
            check_articles()
            check_daily_shop()
        log.info("Done.")
    else:
        if not WEBHOOK_URL:
            raise SystemExit("Error: PATCH_NOTES_WEBHOOK_URL not set. Copy .env.example to .env and configure it.")

        log.info("Valorant Bot starting...")
        log.info("Poll interval: %dm", PATCH_NOTES_POLL_MINUTES)

        check_articles()
        check_daily_shop()

        schedule.every(PATCH_NOTES_POLL_MINUTES).minutes.do(check_articles)
        schedule.every().day.at("00:00").do(check_daily_shop)  # 00:00 UTC = 07:00 GMT+7

        log.info("Bot running. Press Ctrl+C to stop.")
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Bot stopped.")


if __name__ == "__main__":
    main()
