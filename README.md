# Valorant Discord Webhook Bot

Automatically posts **Valorant patch notes** and **leaks** to your Discord channel via the [Gameriv](https://gameriv.com/valorant/) RSS feed. No API key required.

---

## Features

- **Patch Notes** — Automatically detects new Valorant patch notes with detailed summaries
- **Leaks** — Posts leaked content (new agents, bundles, game modes)
- **Rich Content** — Scrapes full articles for agent changes, buffs/nerfs, and more
- **Deduplication** — Never posts the same update twice
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

### Step 3: Done

The workflow runs every 30 minutes. After each run it commits `state.json` back to the repo.

### Manual trigger

Go to **Actions** tab → **Valorant Webhook Bot** → **Run workflow**

---

## Project structure

```
├── main.py                                  # Bot logic
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
