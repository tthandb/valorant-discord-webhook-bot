"""
Valorant Daily Shop — standalone script.

Authenticates via ssid cookie, fetches the daily store for each account,
resolves skin names/images from valorant-api.com, and posts Discord embeds.

Run modes:
    python daily_shop.py           # single check + continuous polling
    python daily_shop.py --once    # single check and exit (GitHub Actions)
    python daily_shop.py --now     # force post (skip dedup), then exit
"""

import os
import sys
import json
import time
import logging
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

import requests
import schedule
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STATE_FILE = os.getenv("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riot_accounts.json")
DAILY_SHOP_WEBHOOK_URL = os.getenv("DAILY_SHOP_WEBHOOK_URL", "")
RIOT_PROXY_URL = os.getenv("RIOT_PROXY_URL", "")

AUTH_URL = "https://auth.riotgames.com/api/v1/authorization"
ENTITLEMENTS_URL = "https://entitlements.auth.riotgames.com/api/token/v1"
USERINFO_URL = "https://auth.riotgames.com/userinfo"
STORE_URL = "https://pd.{region}.a.pvp.net/store/v3/storefront/{puuid}"
SKIN_URL = "https://valorant-api.com/v1/weapons/skinlevels/{uuid}"
SKINS_URL = "https://valorant-api.com/v1/weapons/skins"
VERSION_URL = "https://valorant-api.com/v1/version"

COLOR_RED = 0xFD4556

CLIENT_PLATFORM = "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"

VP_CURRENCY_UUID = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"

TIER_EMOJI = {
    "12683d76-48d7-84a3-4e09-6985794f0445": "\u2b50",           # Select
    "0cebb8be-46d7-c12a-d306-e9907bfc5a25": "\u2b50\u2b50",     # Deluxe
    "60bca009-4182-7998-dee7-b8a2558dc369": "\u2b50\u2b50\u2b50",       # Premium
    "411e4a55-4e59-7757-41f0-86a53f101bb5": "\u2b50\u2b50\u2b50\u2b50", # Ultra
    "e046854e-406c-37f4-6607-19a9ba8426fc": "\u2b50\u2b50\u2b50\u2b50", # Exclusive
}

TIER_NAME = {
    "12683d76-48d7-84a3-4e09-6985794f0445": "Select",
    "0cebb8be-46d7-c12a-d306-e9907bfc5a25": "Deluxe",
    "60bca009-4182-7998-dee7-b8a2558dc369": "Premium",
    "411e4a55-4e59-7757-41f0-86a53f101bb5": "Ultra",
    "e046854e-406c-37f4-6607-19a9ba8426fc": "Exclusive",
}

TIER_COLOR = {
    "12683d76-48d7-84a3-4e09-6985794f0445": 0x5A9FE2,  # Select - blue
    "0cebb8be-46d7-c12a-d306-e9907bfc5a25": 0x009B6D,  # Deluxe - green
    "60bca009-4182-7998-dee7-b8a2558dc369": 0xD1548D,  # Premium - pink
    "411e4a55-4e59-7757-41f0-86a53f101bb5": 0xF5D662,  # Ultra - gold
    "e046854e-406c-37f4-6607-19a9ba8426fc": 0xF5955B,  # Exclusive - orange
}

AUTH_BODY = {
    "client_id": "riot-client",
    "nonce": "1",
    "redirect_uri": "http://localhost/redirect",
    "response_type": "token id_token",
    "scope": "account openid",
}


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Account loading
# ---------------------------------------------------------------------------

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
# Discord webhook
# ---------------------------------------------------------------------------

def send_webhook(embeds, webhook_url):
    if not webhook_url:
        log.error("Webhook URL not configured")
        return False

    payload = {"embeds": embeds}

    for attempt in range(3):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
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
# Riot auth from ssid cookie
# ---------------------------------------------------------------------------

def riot_auth_from_cookie(ssid_cookie):
    """
    Authenticate using a saved ssid cookie.

    Returns (access_token, entitlements_token, puuid, account_name) or raises on failure.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "RiotClient/99.0.0.0 rso-auth (Windows;10;;Professional, x64)",
    })
    if RIOT_PROXY_URL:
        session.proxies = {"https": RIOT_PROXY_URL, "http": RIOT_PROXY_URL}
    session.cookies.set("ssid", ssid_cookie, domain="auth.riotgames.com")

    # Re-auth using cookie
    resp = session.post(AUTH_URL, json=AUTH_BODY, allow_redirects=False)
    resp.raise_for_status()
    data = resp.json()

    if data.get("type") != "response":
        log.error("Riot auth response: %s", json.dumps(data)[:500])
        raise RuntimeError(
            "SSID cookie expired or invalid. Re-run: python riot_auth.py"
        )

    # Parse access token from redirect URI
    uri = data["response"]["parameters"]["uri"]
    fragment = urlparse(uri).fragment
    params = parse_qs(fragment)
    access_token = params["access_token"][0]

    headers = {"Authorization": f"Bearer {access_token}"}

    # Get entitlements token
    resp = session.post(ENTITLEMENTS_URL, headers=headers, json={})
    resp.raise_for_status()
    entitlements_token = resp.json()["entitlements_token"]

    # Get PUUID and account name
    resp = session.get(USERINFO_URL, headers=headers)
    resp.raise_for_status()
    userinfo = resp.json()
    puuid = userinfo["sub"]
    acct = userinfo.get("acct", {})
    game_name = acct.get("game_name", "")
    tag_line = acct.get("tag_line", "")
    account_name = f"{game_name}#{tag_line}" if game_name else ""

    return access_token, entitlements_token, puuid, account_name


# ---------------------------------------------------------------------------
# Fetch daily shop
# ---------------------------------------------------------------------------

def _get_client_version():
    """Fetch current Valorant client version from valorant-api.com."""
    try:
        resp = requests.get(VERSION_URL, timeout=10)
        resp.raise_for_status()
        return resp.json()["data"]["riotClientVersion"]
    except requests.RequestException as e:
        log.warning("Failed to fetch client version: %s", e)
        return "release-12.06-shipping-19-4440219"


def fetch_daily_shop(access_token, entitlements_token, puuid, region="ap"):
    """Fetch the daily shop data including skin UUIDs and prices."""
    url = STORE_URL.format(region=region, puuid=puuid)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Riot-Entitlements-JWT": entitlements_token,
        "X-Riot-ClientPlatform": CLIENT_PLATFORM,
        "X-Riot-ClientVersion": _get_client_version(),
    }

    proxies = {"https": RIOT_PROXY_URL, "http": RIOT_PROXY_URL} if RIOT_PROXY_URL else None
    resp = requests.post(url, headers=headers, json={}, timeout=15, proxies=proxies)
    resp.raise_for_status()
    data = resp.json()

    layout = data["SkinsPanelLayout"]

    # Build price map: offer_id -> VP cost
    prices = {}
    for offer in layout.get("SingleItemStoreOffers", []):
        vp = offer.get("Cost", {}).get(VP_CURRENCY_UUID, 0)
        prices[offer["OfferID"]] = vp

    return {
        "skin_uuids": layout["SingleItemOffers"],
        "prices": prices,
        "remaining_seconds": layout.get("SingleItemOffersRemainingDurationInSeconds", 0),
    }


# ---------------------------------------------------------------------------
# Skin info from valorant-api.com
# ---------------------------------------------------------------------------

_skin_db = None


def _get_skin_db():
    """Fetch and cache the full skin database for tier lookups."""
    global _skin_db
    if _skin_db is not None:
        return _skin_db

    try:
        resp = requests.get(SKINS_URL, timeout=15)
        resp.raise_for_status()
        # Map skin level UUID -> parent skin data
        db = {}
        for skin in resp.json()["data"]:
            for level in skin.get("levels", []):
                db[level["uuid"]] = {
                    "content_tier": skin.get("contentTierUuid"),
                    "parent_name": skin.get("displayName", ""),
                }
        _skin_db = db
        return db
    except requests.RequestException as e:
        log.warning("Failed to fetch skin database: %s", e)
        return {}


def fetch_skin_info(uuid):
    """Fetch skin name, image, and tier from valorant-api.com."""
    url = SKIN_URL.format(uuid=uuid)
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        skin = resp.json()["data"]

        # Get tier from parent skin database
        db = _get_skin_db()
        parent = db.get(uuid, {})
        tier_uuid = parent.get("content_tier")

        return {
            "name": skin.get("displayName", "Unknown Skin"),
            "image": skin.get("displayIcon"),
            "tier_uuid": tier_uuid,
        }
    except requests.RequestException as e:
        log.warning("Failed to fetch skin info for %s: %s", uuid, e)
        return {"name": "Unknown Skin", "image": None, "tier_uuid": None}


# ---------------------------------------------------------------------------
# Discord embed builder
# ---------------------------------------------------------------------------

def _format_time(seconds):
    """Format seconds into hours and minutes."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def build_shop_embeds(account_name, skins, shop_data):
    """
    Build Discord embeds for the daily shop.

    Returns a list of embeds: 1 header + 1 per skin.
    """
    remaining = shop_data.get("remaining_seconds", 0)
    prices = shop_data.get("prices", {})
    skin_uuids = shop_data.get("skin_uuids", [])

    # --- Header embed ---
    total_vp = sum(prices.get(uid, 0) for uid in skin_uuids)
    header = {
        "title": f"\U0001f6d2  Daily Shop \u2014 {account_name}",
        "description": (
            f"\u23f0 Resets in **{_format_time(remaining)}**\n"
            f"\U0001f4b0 Total: **{total_vp:,} VP**"
        ),
        "color": COLOR_RED,
        "footer": {"text": "VALORANT Daily Shop"},
    }
    embeds = [header]

    # --- Skin embeds ---
    for i, skin in enumerate(skins):
        uuid = skin_uuids[i] if i < len(skin_uuids) else None
        vp = prices.get(uuid, 0) if uuid else 0
        tier_uuid = skin.get("tier_uuid")

        tier_label = TIER_NAME.get(tier_uuid, "")
        tier_emoji = TIER_EMOJI.get(tier_uuid, "")
        color = TIER_COLOR.get(tier_uuid, COLOR_RED)

        description = f"\U0001f4b0 **{vp:,} VP**"
        if tier_label:
            description += f"\n{tier_emoji} {tier_label}"

        embed = {
            "title": skin["name"],
            "description": description,
            "color": color,
        }
        if skin.get("image"):
            embed["thumbnail"] = {"url": skin["image"]}

        embeds.append(embed)

    return embeds


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
        log.info("Daily Shop Bot starting...")
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
