# Valorant Discord Webhook Bot

Posts **patch notes**, **daily shop**, and **session recaps** to Discord automatically.

## Features

- **Patch Notes & Leaks** — Polls Gameriv RSS, scrapes summaries, posts embeds
- **Daily Shop** — Posts rotating shop skins with images (requires Riot auth)
- **Session Recap** — Tracks team matches via [Henrik-3 API](https://henrikdev.xyz), posts per-player stats (ACS, HS%, K/D/A) with rank and agent icons

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env   # edit with your webhook URLs
python main.py          # patch notes + daily shop
python session_recap.py # session recap (standalone)
```

## Configuration

All config is in `.env`. Only `PATCH_NOTES_WEBHOOK_URL` is required — other features activate when their variables are set.

| Variable | Description |
|---|---|
| `PATCH_NOTES_WEBHOOK_URL` | Patch notes webhook (required) |
| `PATCH_NOTES_POLL_MINUTES` | Poll interval (default: `30`) |
| `DAILY_SHOP_WEBHOOK_URL` | Daily shop webhook |
| `RIOT_SSID_COOKIE` | Riot auth cookie (run `python riot_auth.py`) |
| `RIOT_REGION` | Riot region — `ap`, `na`, `eu`, `kr` (default: `ap`) |
| `RIOT_DISPLAY_NAME` | Display name in shop embeds |
| `SESSION_RECAP_WEBHOOK_URL` | Session recap webhook |
| `TEAM_PUUIDS` | Comma-separated PUUIDs to track |
| `HENRIK_API_KEY` | Henrik-3 API key |
| `HENRIK_API_REGION` | Match lookup region (default: `ap`) |

## Run Modes

| Command | Behavior |
|---|---|
| `python main.py` | Long-running — polls every 30 min |
| `python main.py --once` | Single check, then exit (CI/CD) |
| `python session_recap.py` | Immediate recap + polls every 10 min |
| `python session_recap.py --once` | Single session check (CI/CD) |

## GitHub Actions

Add secrets in **Settings > Secrets > Actions**, then the included workflows handle everything:

- `valorant-webhook.yml` — patch notes (every 30 min) + daily shop (00:00 UTC)
- `session-recap.yml` — session tracking (every 10 min)

## Notes

- Riot SSID cookie expires every ~3 weeks — re-run `python riot_auth.py`
- Session recap first run initializes state without posting
- Henrik-3 free tier: 30 req/min rate limit
