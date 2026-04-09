import os
import sys
import json
import time
import logging
import requests
import schedule
import xml.etree.ElementTree as ET
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

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
RSS_URL = "https://gameriv.com/valorant/feed/"

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


# ---------------------------------------------------------------------------
# Daily shop checker
# ---------------------------------------------------------------------------

def check_daily_shop(force=False):
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

        if not force and shop_state.get(account_key) == today:
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
            if not force:
                shop_state[account_key] = today
            posted_any = True
            log.info("Daily shop posted for %s (%d skins).", account_name, len(skins))
        else:
            log.error("Failed to send daily shop webhook for %s.", account_name)

    if posted_any and not force:
        state["shop_posted"] = shop_state
        save_state(state)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    once = "--once" in args
    now = "--now" in args

    if now:
        log.info("Running in --now mode (force post, no state changes)...")
        check_daily_shop(force=True)
        log.info("Done.")
        return

    if once:
        log.info("Running daily shop check...")
        check_daily_shop()
        log.info("Done.")
    else:
        log.info("Valorant Bot starting...")
        check_daily_shop()
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
