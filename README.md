# Valorant Discord Webhook Bot

Automatically posts **Valorant patch notes** and **leaks** to your Discord channel via the [Gameriv](https://gameriv.com/valorant/) RSS feed. No API key required.

---

## Features

- **Patch Notes** — Automatically detects new Valorant patch notes with detailed summaries
- **Leaks** — Posts leaked content (new agents, bundles, game modes)
- **Rich Content** — Scrapes full articles for agent changes, buffs/nerfs, and more
- **Deduplication** — Never posts the same update twice
- **Daily Shop** — Posts your Valorant daily shop skins with images to a separate channel
- **Two Run Modes** — Long-running process or single-check for CI/CD

---

## Prerequisites

- Python 3.9+
- A Discord webhook URL ([how to create one](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks))

---

## Setup

### 1. Clone the repo

```bash
git clone <your-repo-url>
cd valorant-discord-webhook-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_WEBHOOK_URL` | Yes | — | Discord webhook endpoint |
| `POLL_MINUTES` | No | `30` | Poll interval in minutes |
| `SHOP_WEBHOOK_URL` | No | — | Separate webhook for daily shop channel |
| `RIOT_SSID_COOKIE` | No | — | Riot `ssid` cookie (see Daily Shop section) |
| `RIOT_REGION` | No | `ap` | Riot region (`ap`, `na`, `eu`, `kr`) |
| `RIOT_ACCOUNT_NAME` | No | `Player` | Display name in shop embeds |

---

## Usage

### Run locally (long-running)

```bash
python main.py
```

Checks patch notes immediately, then every **30 minutes**. Press `Ctrl+C` to stop.

### Run once (single check)

```bash
python main.py --once
```

Checks once and exits. Used by GitHub Actions.

---

## Daily Shop Setup (optional)

Posts your Valorant daily shop skins with images at **7:00 GMT+7** (00:00 UTC) to a separate Discord channel.

### 1. Get your Riot SSID cookie

Run the auth script — it logs in, handles 2FA, and saves the cookie to `.env`:

```bash
python riot_auth.py
```

The script will also ask if you want to push the cookie to GitHub Actions secrets.

### 2. Configure `.env`

```
SHOP_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_SHOP_ID/YOUR_SHOP_TOKEN
RIOT_SSID_COOKIE=<auto-filled by riot_auth.py>
RIOT_REGION=ap
RIOT_ACCOUNT_NAME=YourName
```

### 3. Cookie expiry

The SSID cookie lasts **~3 weeks**. When it expires, re-run `python riot_auth.py`.

### Limitations

- Uses Riot's unofficial internal API — endpoints may change
- One Riot account per bot instance
- Account credentials are only used during `riot_auth.py` and are not stored

---

## Deploy to GitHub Actions (free)

### Step 1: Create a GitHub repo

```bash
git init
git add -A
git commit -m "Initial commit"
gh repo create valorant-webhook --public --source=. --push
```

### Step 2: Add secrets

**Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL |
| `SHOP_WEBHOOK_URL` | Daily shop webhook URL (optional) |
| `RIOT_SSID_COOKIE` | Riot ssid cookie (optional, from `riot_auth.py`) |
| `RIOT_REGION` | Riot region, e.g. `ap` (optional) |
| `RIOT_ACCOUNT_NAME` | Display name for shop embeds (optional) |

### Step 3: Done

The workflow runs every 30 minutes. After each run it commits `state.json` back to the repo.

### Manual trigger

Go to **Actions** tab → **Valorant Webhook Bot** → **Run workflow**

---

## Project structure

```
├── main.py                                  # Bot logic (patch notes + daily shop)
├── daily_shop.py                            # Daily shop auth, fetch, embeds
├── riot_auth.py                             # Interactive Riot login script
├── requirements.txt                         # Python dependencies
├── .env.example                             # Secrets template
├── .env                                     # Your secrets (git-ignored)
├── .gitignore                               # Ignores .env and cache
├── .github/workflows/valorant-webhook.yml   # GitHub Actions workflow
├── state.json                               # Auto-generated dedup state
├── README.md                                # This file
├── PLAN.md                                  # Architecture & design notes
└── CLAUDE.md                                # Claude Code guidance
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Bot posts nothing | Check webhook URL is correct. Run `python main.py --once` and read the logs. |
| Duplicate posts after restart | Make sure `state.json` exists and isn't being deleted. |
| RSS feed unavailable | Check if `https://gameriv.com/valorant/feed/` is accessible. |
| GitHub Actions not running | Check the Actions tab for errors. Ensure secrets are set. |
| Daily shop not posting | Check `SHOP_WEBHOOK_URL` and `RIOT_SSID_COOKIE` are set. Cookie may have expired — re-run `python riot_auth.py`. |
| Riot auth failed | Cookie expired (~3 weeks). Re-run `python riot_auth.py` to get a fresh one. |
