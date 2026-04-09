"""
Valorant Daily Shop module.

Authenticates via ssid cookie, fetches the daily store for an account,
resolves skin names/images from valorant-api.com, and builds Discord embeds.
"""

import logging
from urllib.parse import urlparse, parse_qs
import requests

log = logging.getLogger(__name__)

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
    session.cookies.set("ssid", ssid_cookie, domain="auth.riotgames.com")

    # Re-auth using cookie
    resp = session.post(AUTH_URL, json=AUTH_BODY, allow_redirects=False)
    resp.raise_for_status()
    data = resp.json()

    if data.get("type") != "response":
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

    resp = requests.post(url, headers=headers, json={}, timeout=15)
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
