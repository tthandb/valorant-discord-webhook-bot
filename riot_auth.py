"""
Riot Games authentication script.

Two modes:
  python riot_auth.py           — Browser login (recommended)
  python riot_auth.py --cli     — CLI login with username/password

Opens Riot's login page, you authenticate normally (with 2FA),
then paste the redirect URL. The script extracts your access token,
fetches the ssid cookie, and saves it to .env.
"""

import os
import re
import sys
import json
import getpass
import webbrowser
import subprocess
import requests
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv()

AUTH_URL = "https://auth.riotgames.com/api/v1/authorization"
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# Riot's authorize page — shows login UI, redirects with token in URL fragment
BROWSER_AUTH_URL = (
    "https://auth.riotgames.com/authorize"
    "?client_id=play-valorant-web-prod"
    "&redirect_uri=https%3A%2F%2Fplayvalorant.com%2Fopt_in"
    "&response_type=token%20id_token"
    "&nonce=1"
    "&scope=account%20openid"
)

AUTH_BODY = {
    "client_id": "play-valorant-web-prod",
    "nonce": "1",
    "redirect_uri": "https://playvalorant.com/opt_in",
    "response_type": "token id_token",
    "scope": "account openid",
}


# ---------------------------------------------------------------------------
# URL input helper (handles long URLs that terminals truncate)
# ---------------------------------------------------------------------------

URL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riot_url.txt")


def _read_long_url():
    """Read redirect URL from clipboard, file, or manual paste."""
    # Try clipboard first (macOS pbpaste)
    try:
        result = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=5
        )
        clip = result.stdout.strip()
        if clip.startswith("https://playvalorant.com") and "access_token=" in clip:
            print("Found redirect URL in clipboard!")
            return clip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try riot_url.txt file
    if os.path.exists(URL_FILE):
        with open(URL_FILE, "r") as f:
            file_url = f.read().strip()
        if file_url.startswith("https://") and "access_token=" in file_url:
            print("Found redirect URL in riot_url.txt!")
            os.remove(URL_FILE)
            return file_url

    # Manual input fallback
    print("The redirect URL is very long and may get cut off in the terminal.")
    print("Options:")
    print("  a) Copy the URL, then press Enter (reads from clipboard)")
    print("  b) Save the URL to 'riot_url.txt' in this folder, then press Enter\n")

    input("Press Enter when ready...")

    # Try clipboard again after user copied
    try:
        result = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=5
        )
        clip = result.stdout.strip()
        if clip.startswith("https://") and "access_token=" in clip:
            print("Got URL from clipboard!")
            return clip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try file again
    if os.path.exists(URL_FILE):
        with open(URL_FILE, "r") as f:
            file_url = f.read().strip()
        if file_url:
            os.remove(URL_FILE)
            return file_url

    raise SystemExit(
        "Could not read URL. Please save the full redirect URL to 'riot_url.txt' "
        "and run this script again."
    )


# ---------------------------------------------------------------------------
# Browser login (default)
# ---------------------------------------------------------------------------

def riot_login_browser():
    """Open Riot login in browser, user pastes redirect URL."""
    print("Opening Riot login page in your browser...\n")
    webbrowser.open(BROWSER_AUTH_URL)

    print("Steps:")
    print("  1. Log in with your Riot account (2FA will be handled automatically)")
    print("  2. After login, you'll be redirected to playvalorant.com")
    print("  3. Copy the FULL URL from your browser's address bar")
    print("     (it will look like: https://playvalorant.com/opt_in#access_token=...)\n")

    url = _read_long_url()
    if not url:
        raise SystemExit("No URL provided.")

    # Parse access_token from URL fragment
    fragment = urlparse(url).fragment
    if not fragment:
        raise SystemExit(
            "Invalid URL — no token found.\n"
            "Make sure you copied the full URL including the # part."
        )

    params = parse_qs(fragment)
    access_token = params.get("access_token", [None])[0]
    if not access_token:
        raise SystemExit("Could not find access_token in the URL.")

    print("Access token extracted!")

    # Now we need the ssid cookie for persistent auth.
    # Use the access_token to verify it works, then ask for ssid.
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get("https://auth.riotgames.com/userinfo", headers=headers)
    if resp.status_code != 200:
        raise SystemExit("Access token verification failed.")

    userinfo = resp.json()
    acct = userinfo.get("acct", {})
    game_name = acct.get("game_name", "")
    tag_line = acct.get("tag_line", "")
    display = f"{game_name}#{tag_line}" if game_name else userinfo.get("sub", "unknown")
    print(f"Logged in as: {display}\n")

    # Ask for ssid cookie for persistent auth
    print("For automated daily shop checks, the bot needs the 'ssid' cookie.")
    print("In the same browser where you just logged in:")
    print("  1. Open DevTools (F12) or right-click > Inspect")
    print("  2. Go to Application tab > Cookies > https://auth.riotgames.com")
    print("  3. Find 'ssid' and copy its Value")
    print("  (Skip this if you only want a one-time check)\n")

    ssid = input("Paste ssid cookie value (or press Enter to skip): ").strip()

    if ssid:
        # Verify the cookie works
        print("Verifying cookie...")
        session = requests.Session()
        session.headers.update({
            "User-Agent": "RiotClient/99.0.0.0 rso-auth (Windows;10;;Professional, x64)",
        })
        session.cookies.set("ssid", ssid, domain="auth.riotgames.com")

        resp = session.post(AUTH_URL, json=AUTH_BODY, allow_redirects=False)
        resp.raise_for_status()
        data = resp.json()

        if data.get("type") != "response":
            raise SystemExit("Cookie verification failed. Make sure you copied the full ssid value.")

        print("Cookie verified!")
        return ssid
    else:
        # No ssid — save access_token directly (short-lived, ~1 hour)
        print("No ssid cookie provided. Saving access token instead.")
        print("Note: access tokens expire in ~1 hour. For daily automated checks,")
        print("      re-run this script and provide the ssid cookie.\n")
        return None, access_token


# ---------------------------------------------------------------------------
# CLI login (--cli flag)
# ---------------------------------------------------------------------------

def riot_login_cli():
    """Perform CLI-based Riot login and return the ssid cookie value."""
    username = os.getenv("RIOT_USERNAME") or input("Riot username: ").strip()
    password = os.getenv("RIOT_PASSWORD") or getpass.getpass("Riot password: ")

    print(f"Logging in as: {username}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "RiotClient/99.0.0.0 rso-auth (Windows;10;;Professional, x64)",
        "Content-Type": "application/json",
    })

    # Step 1: Init auth session
    resp = session.post(AUTH_URL, json=AUTH_BODY)
    resp.raise_for_status()

    # Step 2: Send credentials
    resp = session.put(AUTH_URL, json={
        "type": "auth",
        "username": username,
        "password": password,
        "remember": True,
    })
    resp.raise_for_status()
    data = resp.json()

    # Step 3: Handle 2FA if required
    if data.get("type") == "multifactor":
        print(f"\n2FA required. Check your email ({data.get('multifactor', {}).get('email', 'N/A')}).")
        code = input("Enter 2FA code: ").strip()
        resp = session.put(AUTH_URL, json={
            "type": "multifactor",
            "code": code,
            "rememberDevice": True,
        })
        resp.raise_for_status()
        data = resp.json()

    # Step 4: Verify login succeeded
    if data.get("type") != "response":
        error = data.get("error", data.get("type", "unknown"))
        if error == "auth_failure":
            raise SystemExit(
                "Login failed: wrong username or password.\n"
                "Note: username is your Riot account login (not in-game name).\n"
                "Try browser mode instead: python riot_auth.py"
            )
        raise SystemExit(f"Login failed: {error}")

    uri = data["response"]["parameters"]["uri"]
    fragment = urlparse(uri).fragment
    params = parse_qs(fragment)
    if "access_token" not in params:
        raise SystemExit("Login failed: no access_token in response")

    print("Login successful!")

    # Step 5: Extract ssid cookie
    ssid = session.cookies.get("ssid", domain="auth.riotgames.com")
    if not ssid:
        ssid = session.cookies.get("ssid")
    if not ssid:
        raise SystemExit("Login succeeded but ssid cookie not found in session.")

    return ssid


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------

ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riot_accounts.json")


def load_accounts():
    """Load accounts from riot_accounts.json."""
    try:
        with open(ACCOUNTS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_accounts(accounts):
    """Save accounts to riot_accounts.json."""
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f, indent=2)


def add_account(ssid, display_name):
    """Add or update an account in riot_accounts.json."""
    accounts = load_accounts()

    region = input(f"Region (ap/na/eu/kr) [ap]: ").strip() or "ap"
    name_override = input(f"Display name override (Enter to use '{display_name}'): ").strip()

    # Check if account already exists (by name)
    updated = False
    for acc in accounts:
        if acc.get("name", "") == display_name or acc.get("ssid_cookie") == ssid:
            acc["ssid_cookie"] = ssid
            acc["region"] = region
            if name_override:
                acc["name"] = name_override
            updated = True
            print(f"Updated existing account: {display_name}")
            break

    if not updated:
        accounts.append({
            "ssid_cookie": ssid,
            "region": region,
            "name": name_override or "",
        })
        print(f"Added account: {display_name}")

    save_accounts(accounts)
    print(f"Saved to riot_accounts.json ({len(accounts)} account(s))")


def push_accounts_to_github():
    """Push riot_accounts.json to GitHub Actions secrets."""
    choice = input("\nPush accounts to GitHub Actions secret? (y/n): ").strip().lower()
    if choice != "y":
        print("Skipped.")
        return

    try:
        with open(ACCOUNTS_FILE, "r") as f:
            content = f.read()
        subprocess.run(
            ["gh", "secret", "set", "RIOT_ACCOUNTS", "--body", content],
            check=True,
        )
        print("Pushed RIOT_ACCOUNTS to GitHub Actions secrets.")
    except FileNotFoundError:
        print("Error: gh CLI not found. Install it from https://cli.github.com/")
    except subprocess.CalledProcessError as e:
        print(f"Error pushing secret: {e}")


def list_accounts():
    """List all configured accounts."""
    accounts = load_accounts()
    if not accounts:
        print("No accounts configured. Run 'python riot_auth.py' to add one.")
        return

    print(f"\n{'#':<4} {'Name':<30} {'Region':<8} {'Cookie'}")
    print("-" * 70)
    for i, acc in enumerate(accounts):
        name = acc.get("name", "") or "(auto-detect)"
        region = acc.get("region", "ap")
        cookie = acc.get("ssid_cookie", "")[:20] + "..." if acc.get("ssid_cookie") else "missing"
        print(f"{i+1:<4} {name:<30} {region:<8} {cookie}")


def remove_account():
    """Remove an account by index."""
    accounts = load_accounts()
    if not accounts:
        print("No accounts to remove.")
        return

    list_accounts()
    idx = input("\nAccount number to remove: ").strip()
    try:
        idx = int(idx) - 1
        removed = accounts.pop(idx)
        save_accounts(accounts)
        print(f"Removed: {removed.get('name', 'account ' + str(idx + 1))}")
    except (ValueError, IndexError):
        print("Invalid number.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Riot Games Authentication ===\n")

    if "--list" in sys.argv:
        list_accounts()
        return

    if "--remove" in sys.argv:
        remove_account()
        return

    # Login and get ssid cookie
    if "--cli" in sys.argv:
        ssid = riot_login_cli()
        display_name = "CLI account"
    else:
        result = riot_login_browser()
        if isinstance(result, tuple):
            _, access_token = result
            print("\nNo ssid cookie provided. Cannot save for daily checks.")
            print("Re-run and provide the ssid cookie.")
            return
        ssid = result
        display_name = "Browser account"

    # Get account display name from Riot
    try:
        from daily_shop import riot_auth_from_cookie
        _, _, _, fetched_name = riot_auth_from_cookie(ssid)
        if fetched_name:
            display_name = fetched_name
    except Exception:
        pass

    add_account(ssid, display_name)
    push_accounts_to_github()

    print("\nDone! Run 'python riot_auth.py --list' to see all accounts.")


if __name__ == "__main__":
    main()
