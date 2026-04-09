"""
Valorant Session Recap — standalone script.

Tracks team matches via the Henrik-3 unofficial API, detects session
boundaries (45-minute inactivity gap), and posts per-player stat summaries
to a Discord webhook.

Run modes:
    python session_recap.py            # immediate recap + continuous polling
    python session_recap.py --once     # single check and exit (GitHub Actions)
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta

TZ_DISPLAY = timezone(timedelta(hours=7))  # GMT+7 for display

import httpx
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

SESSION_RECAP_WEBHOOK_URL = os.getenv("SESSION_RECAP_WEBHOOK_URL", "")
TEAM_PUUIDS = [p.strip() for p in os.getenv("TEAM_PUUIDS", "").split(",") if p.strip()]
HENRIK_API_KEY = os.getenv("HENRIK_API_KEY", "")
HENRIK_API_REGION = os.getenv("HENRIK_API_REGION", "ap")

HENRIK_API_BASE = "https://api.henrikdev.xyz"
MATCH_HISTORY_URL = HENRIK_API_BASE + "/valorant/v3/by-puuid/matches/{region}/{puuid}"
MMR_URL = HENRIK_API_BASE + "/valorant/v3/by-puuid/mmr/{region}/pc/{puuid}"

SESSION_COOLDOWN_MINUTES = 45
MATCH_WINDOW_HOURS = 8
COLOR_VALORANT_RED = 0xFF4654

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_state.json")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "processed_match_ids": [],
            "active_session": None,
            "initialized": False,
        }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Henrik API
# ---------------------------------------------------------------------------

def fetch_recent_matches(puuid, api_key, region="ap", size=10):
    """Fetch recent matches for a PUUID from the Henrik-3 API.

    Returns a list of match dicts, or [] on error.
    """
    url = MATCH_HISTORY_URL.format(region=region, puuid=puuid)
    headers = {"Authorization": api_key}
    params = {"size": size, "mode": "competitive"}

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=headers, params=params)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "5"))
            log.warning("Henrik API rate limited, waiting %.1fs", retry_after)
            time.sleep(retry_after)
            return []

        if resp.status_code != 200:
            log.warning("Henrik API returned %s for %s", resp.status_code, puuid[:8])
            return []

        data = resp.json()
        return data.get("data", [])

    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("Failed to fetch matches for %s: %s", puuid[:8], e)
        return []


def fetch_player_mmr(puuid, api_key, region="ap"):
    """Fetch current MMR/rank for a PUUID from the Henrik-3 API.

    Returns dict with rank_name, rank_tier, rr, rr_change, or None on error.
    """
    url = MMR_URL.format(region=region, puuid=puuid)
    headers = {"Authorization": api_key}

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=headers)

        if resp.status_code != 200:
            return None

        current = resp.json().get("data", {}).get("current", {})
        tier = current.get("tier", {})

        return {
            "rank_name": tier.get("name", "Unrated"),
            "rank_tier": tier.get("id", 0),
            "rr": current.get("rr", 0),
            "rr_change": current.get("last_change", 0),
        }

    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("Failed to fetch MMR for %s: %s", puuid[:8], e)
        return None


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def get_match_end_time(match):
    """Return the end time of a match as a UTC datetime."""
    meta = match["metadata"]
    start = meta["game_start"]
    length_ms = meta["game_length"]
    return datetime.fromtimestamp(start + length_ms // 1000, tz=timezone.utc)


def extract_player_stats(match, puuid):
    """Extract computed stats for a specific player from a match.

    Returns a dict of stats, or None if the player is not in this match.
    """
    players = match.get("players", {}).get("all_players", [])
    player = None
    for p in players:
        if p.get("puuid") == puuid:
            player = p
            break
    if player is None:
        return None

    stats = player.get("stats", {})
    team_key = player.get("team", "").lower()
    teams = match.get("teams", {})
    team_data = teams.get(team_key, {})

    rounds_won = team_data.get("rounds_won", 0)
    rounds_lost = team_data.get("rounds_lost", 0)
    rounds_played = rounds_won + rounds_lost

    score = stats.get("score", 0)
    kills = stats.get("kills", 0)
    deaths = stats.get("deaths", 0)
    assists = stats.get("assists", 0)
    headshots = stats.get("headshots", 0)
    bodyshots = stats.get("bodyshots", 0)
    legshots = stats.get("legshots", 0)

    total_shots = headshots + bodyshots + legshots
    acs = score / max(rounds_played, 1)
    hs_pct = (headshots / max(total_shots, 1)) * 100
    kda = (kills + assists) / max(deaths, 1)

    # Image assets from the API
    assets = player.get("assets", {})
    agent_icon = assets.get("agent", {}).get("small", "")
    rank_name = player.get("currenttier_patched", "")
    rank_tier = player.get("currenttier", 0)

    return {
        "match_id": match["metadata"]["matchid"],
        "map": match["metadata"].get("map", "Unknown"),
        "game_start": match["metadata"].get("game_start", 0),
        "name": player.get("name", "Unknown"),
        "tag": player.get("tag", ""),
        "agent": player.get("character", ""),
        "agent_icon": agent_icon,
        "rank_name": rank_name,
        "rank_tier": rank_tier,
        "acs": round(acs, 1),
        "hs_pct": round(hs_pct, 1),
        "kda": round(kda, 2),
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "rounds_won": rounds_won,
        "rounds_lost": rounds_lost,
        "won": team_data.get("has_won", False),
    }


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------

def detect_session_end(session_state, new_matches_by_puuid, now=None):
    """Detect if a playing session has ended based on a 45-min activity gap.

    Returns (updated_state, session_ended, session_match_ids | None).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    active = session_state.get("active_session")

    # Collect all new match IDs and their end times
    latest_end_time = None
    new_stats_by_match = {}

    for puuid, matches in new_matches_by_puuid.items():
        for match in matches:
            # Only include ranked (competitive) matches
            mode = match.get("metadata", {}).get("mode", "").lower()
            if mode != "competitive":
                continue

            mid = match["metadata"]["matchid"]
            end_time = get_match_end_time(match)

            if latest_end_time is None or end_time > latest_end_time:
                latest_end_time = end_time

            if mid not in new_stats_by_match:
                new_stats_by_match[mid] = {}

            stats = extract_player_stats(match, puuid)
            if stats:
                new_stats_by_match[mid][puuid] = stats

    # New matches found — update or create active session
    if new_stats_by_match:
        if active is None:
            active = {"match_stats": {}, "last_activity_time": None}

        active["match_stats"].update(new_stats_by_match)
        active["last_activity_time"] = latest_end_time.isoformat()
        session_state["active_session"] = active

        return session_state, False, None

    # No new matches — check if cooldown has expired
    if active and active.get("last_activity_time"):
        last_activity = datetime.fromisoformat(active["last_activity_time"])
        gap = (now - last_activity).total_seconds() / 60

        if gap >= SESSION_COOLDOWN_MINUTES:
            match_ids = list(active["match_stats"].keys())
            return session_state, True, match_ids

    return session_state, False, None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def compute_session_data(active_session):
    """Compute per-match details and per-player averages.

    Returns (matches, averages):
        matches: list of dicts sorted by match time, each with map/score/result
                 and a list of player stats
        averages: {puuid: {name, avg_acs, avg_hs_pct, avg_kda, ...}}
    """
    match_stats = active_session["match_stats"]

    # Build per-match details
    matches = []
    for match_id, players in match_stats.items():
        first_player = next(iter(players.values()))
        match_info = {
            "match_id": match_id,
            "map": first_player.get("map", "Unknown"),
            "won": first_player.get("won", False),
            "rounds_won": first_player.get("rounds_won", 0),
            "rounds_lost": first_player.get("rounds_lost", 0),
            "game_start": first_player.get("game_start", 0),
            "players": [],
        }

        # Sort players in this match by ACS descending
        for puuid, stats in sorted(players.items(),
                                   key=lambda x: x[1].get("acs", 0),
                                   reverse=True):
            match_info["players"].append(stats)

        matches.append(match_info)

    # Sort matches: latest first
    matches.sort(key=lambda m: m["game_start"], reverse=True)

    # Build per-player averages
    player_stats = {}
    for _mid, players in match_stats.items():
        for puuid, stats in players.items():
            player_stats.setdefault(puuid, []).append(stats)

    averages = {}
    for puuid, stats_list in player_stats.items():
        n = len(stats_list)
        last = stats_list[-1]

        avg_acs = sum(s["acs"] for s in stats_list) / n
        avg_hs_pct = sum(s["hs_pct"] for s in stats_list) / n
        avg_kda = sum(s["kda"] for s in stats_list) / n
        total_kills = sum(s["kills"] for s in stats_list)
        total_deaths = sum(s["deaths"] for s in stats_list)
        total_assists = sum(s["assists"] for s in stats_list)
        wins = sum(1 for s in stats_list if s.get("won"))

        # Most played agent icon (for thumbnail)
        agent_icons = {}
        for s in stats_list:
            if s.get("agent_icon"):
                agent_icons[s["agent"]] = s["agent_icon"]

        averages[puuid] = {
            "name": f"{last['name']}#{last['tag']}" if last.get("tag") else last["name"],
            "matches_played": n,
            "avg_acs": round(avg_acs, 1),
            "avg_hs_pct": round(avg_hs_pct, 1),
            "avg_kda": round(avg_kda, 2),
            "total_kills": total_kills,
            "total_deaths": total_deaths,
            "total_assists": total_assists,
            "wins": wins,
            "losses": n - wins,
            "agent_icon": next(iter(agent_icons.values()), ""),
        }

    return matches, averages


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

VALORANT_ICON = "https://media.valorant-api.com/gamemodes/96bd3920-4f36-d026-2b28-c683eb0bcac5/displayicon.png"



def _win_bar(wins, losses, length=10):
    """Build a visual win rate bar using unicode blocks."""
    total = wins + losses
    if total == 0:
        return "\u2591" * length
    filled = round((wins / total) * length)
    return "\u2588" * filled + "\u2591" * (length - filled)


def _rr_arrow(rr_change):
    """Format RR change with arrow."""
    if rr_change > 0:
        return f"\u25b2 +{rr_change}"
    elif rr_change < 0:
        return f"\u25bc {rr_change}"
    return "\u25cf 0"


def build_session_embed(matches, averages, mmr_data=None):
    """Build Discord embeds for the session recap.

    Uses the description area for a clean, readable layout instead of fields.
    Returns a list of embed dicts.
    """
    if mmr_data is None:
        mmr_data = {}

    sorted_avg = sorted(averages.items(), key=lambda x: x[1]["avg_acs"], reverse=True)

    # --- Build description ---
    sections = []

    # Per-player session overview with rank
    for puuid, s in sorted_avg:
        n = s["matches_played"]
        wins = s["wins"]
        losses = s["losses"]
        win_pct = round((wins / max(n, 1)) * 100)
        bar = _win_bar(wins, losses)

        mmr = mmr_data.get(puuid, {})
        rank_str = ""
        if mmr:
            rr_str = _rr_arrow(mmr["rr_change"])
            rank_str = f" \u2022 {mmr['rank_name']} \u2022 **{mmr['rr']}** RR ({rr_str})"

        sections.append(
            f"**{s['name']}**{rank_str}\n"
            f"> **{n}** game{'s' if n != 1 else ''}  \u2022  "
            f"**{wins}**W **{losses}**L\n"
            f"> `{bar}` {win_pct}%"
        )

    # Per-match breakdowns
    for match in matches:
        result_emoji = "\u2705" if match["won"] else "\u274c"
        result_text = "WIN" if match["won"] else "LOSS"
        score = f"{match['rounds_won']}\u2013{match['rounds_lost']}"
        game_ts = match.get("game_start", 0)
        if game_ts:
            match_time = datetime.fromtimestamp(game_ts, tz=TZ_DISPLAY)
            time_str = f" \u2022 {match_time.strftime('%d/%m %H:%M')}"
        else:
            time_str = ""

        match_lines = [
            f"\n{result_emoji} **{match['map']}** \u2014 {result_text} ({score}){time_str}"
        ]

        for i, p in enumerate(match["players"]):
            agent = p.get("agent", "")
            kda_str = f"{p['kills']}/{p['deaths']}/{p['assists']}"
            if i > 0:
                match_lines.append("> \u200b")  # spacer between players
            match_lines.append(
                f"> **{p['name']}** \u2022 {agent}\n"
                f"> `ACS` **{p['acs']}** \u2502 "
                f"`HS%` **{p['hs_pct']}%** \u2502 "
                f"`K/D/A` **{kda_str}**"
            )

        sections.append("\n".join(match_lines))

    # Session averages (only if multiple matches)
    if len(matches) > 1:
        avg_section = ["\n\u2500\u2500\u2500 **SESSION AVERAGES** \u2500\u2500\u2500"]
        for j, (puuid, s) in enumerate(sorted_avg):
            badge = "\U0001f451" if j == 0 and len(sorted_avg) > 1 else "\u2003"
            kda_str = f"{s['total_kills']}/{s['total_deaths']}/{s['total_assists']}"
            if j > 0:
                avg_section.append("\u200b")  # spacer between players
            avg_section.append(
                f"{badge} **{s['name']}**\n"
                f"> `ACS` **{s['avg_acs']}** \u2502 "
                f"`HS%` **{s['avg_hs_pct']}%** \u2502 "
                f"`K/D/A` **{kda_str}**"
            )
        sections.append("\n".join(avg_section))

    description = "\n".join(sections)

    # Discord description max is 4096 chars — drop oldest matches until it fits
    while len(description) > 4096 and len(sections) > 2:
        # Remove the last match section (oldest match, before averages)
        # sections layout: [overview, match1, match2, ..., matchN, averages?]
        has_averages = sections[-1].startswith("\n\u2500")
        remove_idx = -2 if has_averages else -1
        sections.pop(remove_idx)
        description = "\n".join(sections)

    if len(description) > 4096:
        description = description[:4093] + "..."

    # Time window from match timestamps (displayed in GMT+7)
    timestamps = [m.get("game_start", 0) for m in matches if m.get("game_start")]
    if timestamps:
        earliest = datetime.fromtimestamp(min(timestamps), tz=TZ_DISPLAY)
        latest = datetime.fromtimestamp(max(timestamps), tz=TZ_DISPLAY)
        if earliest.date() == latest.date():
            time_window = (f"{earliest.strftime('%d-%m-%Y')} "
                           f"\u2022 {earliest.strftime('%H:%M')}\u2013{latest.strftime('%H:%M')}")
        else:
            time_window = (f"{earliest.strftime('%d-%m-%Y %H:%M')}"
                           f" \u2013 {latest.strftime('%d-%m-%Y %H:%M')}")
    else:
        time_window = datetime.now(TZ_DISPLAY).strftime('%d-%m-%Y')

    embed = {
        "author": {
            "name": "Session Summary",
            "icon_url": VALORANT_ICON,
        },
        "title": time_window,
        "description": description,
        "color": COLOR_VALORANT_RED
    }

    return [embed]


def send_webhook(embeds, webhook_url):
    """Post embeds to a Discord webhook with retry and rate-limit handling."""
    payload = {"embeds": embeds}

    for attempt in range(3):
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(webhook_url, json=payload)

            if resp.status_code in (200, 204):
                return True

            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 5)
                log.warning("Rate limited, waiting %.1fs", retry_after)
                time.sleep(retry_after)
                continue

            log.error("Webhook returned %s: %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as e:
            log.warning("Webhook attempt %d failed: %s", attempt + 1, e)
            time.sleep(2 ** attempt)

    return False


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------

def post_recap_now():
    """Immediate mode: fetch recent matches, aggregate stats, post recap.

    Fetches the last 8 hours of matches for all team PUUIDs, computes
    per-player stats, and posts the summary. No session detection or
    state tracking.
    """
    if not SESSION_RECAP_WEBHOOK_URL or not TEAM_PUUIDS or not HENRIK_API_KEY:
        log.error("Missing config: SESSION_RECAP_WEBHOOK_URL, TEAM_PUUIDS, or HENRIK_API_KEY")
        return

    log.info("Fetching recent matches for immediate recap...")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=MATCH_WINDOW_HOURS)
    all_player_stats = {}  # puuid -> [stat_dict, ...]
    seen_match_ids = set()

    for puuid in TEAM_PUUIDS:
        matches = fetch_recent_matches(puuid, HENRIK_API_KEY, region=HENRIK_API_REGION)
        time.sleep(1)  # courtesy delay between API calls

        for match in matches:
            mid = match["metadata"]["matchid"]
            end_time = get_match_end_time(match)

            if end_time < cutoff:
                continue

            # Only include ranked (competitive) matches
            mode = match.get("metadata", {}).get("mode", "").lower()
            if mode != "competitive":
                continue

            # Extract stats for all team members in this match
            if mid not in seen_match_ids:
                seen_match_ids.add(mid)
                for team_puuid in TEAM_PUUIDS:
                    stats = extract_player_stats(match, team_puuid)
                    if stats:
                        all_player_stats.setdefault(team_puuid, []).append(stats)

    if not all_player_stats:
        log.info("No matches found in the last %d hours.", MATCH_WINDOW_HOURS)
        return

    # Build active_session structure for compute_session_data
    match_stats = {}
    for puuid, stats_list in all_player_stats.items():
        for stats in stats_list:
            mid = stats["match_id"]
            match_stats.setdefault(mid, {})[puuid] = stats

    # Fetch current MMR for all players with matches
    mmr_data = {}
    for puuid in all_player_stats:
        mmr = fetch_player_mmr(puuid, HENRIK_API_KEY, region=HENRIK_API_REGION)
        time.sleep(1)
        if mmr:
            mmr_data[puuid] = mmr

    match_details, averages = compute_session_data({"match_stats": match_stats})
    embeds = build_session_embed(match_details, averages, mmr_data)

    if send_webhook(embeds, SESSION_RECAP_WEBHOOK_URL):
        log.info("Immediate recap posted. %d players, %d matches.",
                 len(averages), len(seen_match_ids))
    else:
        log.error("Failed to send immediate recap webhook.")


def check_session():
    """Poll-based session check with state tracking.

    Detects new matches, accumulates them in an active session, and posts
    a recap when no team member has played for 45 minutes.
    """
    if not SESSION_RECAP_WEBHOOK_URL or not TEAM_PUUIDS or not HENRIK_API_KEY:
        return

    log.info("Checking for session activity...")

    state = load_state()

    # First-run initialization: mark existing matches as processed
    if not state.get("initialized"):
        all_ids = []
        for puuid in TEAM_PUUIDS:
            matches = fetch_recent_matches(puuid, HENRIK_API_KEY, region=HENRIK_API_REGION)
            time.sleep(1)
            for m in matches:
                mid = m["metadata"]["matchid"]
                if mid not in all_ids:
                    all_ids.append(mid)

        state["processed_match_ids"] = all_ids[-200:]
        state["initialized"] = True
        state["active_session"] = None
        save_state(state)
        log.info("Session tracker initialized. %d existing matches marked as processed.",
                 len(all_ids))
        return

    # Fetch recent matches, filter out already-known IDs
    processed = set(state.get("processed_match_ids", []))
    active_ids = set()
    if state.get("active_session"):
        active_ids = set(state["active_session"].get("match_stats", {}).keys())
    already_seen = processed | active_ids

    new_matches_by_puuid = {}
    for puuid in TEAM_PUUIDS:
        matches = fetch_recent_matches(puuid, HENRIK_API_KEY, region=HENRIK_API_REGION)
        time.sleep(1)

        new = [m for m in matches
               if m["metadata"]["matchid"] not in already_seen
               and m.get("metadata", {}).get("mode", "").lower() == "competitive"]
        if new:
            new_matches_by_puuid[puuid] = new

    # Run session detection
    updated_state, session_ended, session_match_ids = detect_session_end(
        state, new_matches_by_puuid
    )

    if session_ended and session_match_ids:
        active = updated_state.get("active_session")
        if active:
            match_details, averages = compute_session_data(active)

            if averages:
                mmr_data = {}
                for puuid in averages:
                    mmr = fetch_player_mmr(puuid, HENRIK_API_KEY, region=HENRIK_API_REGION)
                    time.sleep(1)
                    if mmr:
                        mmr_data[puuid] = mmr

                embeds = build_session_embed(match_details, averages, mmr_data)

                if send_webhook(embeds, SESSION_RECAP_WEBHOOK_URL):
                    log.info("Session recap posted. %d players, %d matches.",
                             len(averages), len(session_match_ids))

                    # Move to processed, cap at 200
                    processed_list = updated_state.get("processed_match_ids", [])
                    processed_list.extend(session_match_ids)
                    updated_state["processed_match_ids"] = processed_list[-200:]
                    updated_state["active_session"] = None
                else:
                    log.error("Failed to send session recap. Will retry next cycle.")
                    save_state(updated_state)
                    return

    save_state(updated_state)
    log.info("Session check complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    once = "--once" in args
    now = "--now" in args

    if now:
        # Force-post immediately — for local testing
        if not SESSION_RECAP_WEBHOOK_URL or not TEAM_PUUIDS or not HENRIK_API_KEY:
            raise SystemExit(
                "Error: SESSION_RECAP_WEBHOOK_URL, TEAM_PUUIDS, and HENRIK_API_KEY must be set."
            )
        log.info("Running in --now mode (immediate recap, no state changes)...")
        post_recap_now()
        log.info("Done.")
        return

    if once:
        log.info("Running session check (single run)...")
        check_session()
        log.info("Done.")
    else:
        if not SESSION_RECAP_WEBHOOK_URL or not TEAM_PUUIDS or not HENRIK_API_KEY:
            raise SystemExit(
                "Error: SESSION_RECAP_WEBHOOK_URL, TEAM_PUUIDS, and HENRIK_API_KEY must be set."
            )

        log.info("Session Recap starting...")
        post_recap_now()

        schedule.every(10).minutes.do(check_session)

        log.info("Polling every 10 minutes. Press Ctrl+C to stop.")
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Session Recap stopped.")


if __name__ == "__main__":
    main()
