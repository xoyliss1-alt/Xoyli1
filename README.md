# WoodooGifi Telegram Bot

Telegram bot built with Python, aiogram 3, aiohttp, and SQLite/aiosqlite.

## Features
- Required channel subscription checks
- Referral links in the form `?start=REF_<user_id>`
- One-time referral rewards after successful subscription verification
- SQLite user, channel, and settings tables
- Admin panel for Telegram username `@xoyli`
- Add/remove required channels and update referral reward
- HTTP health endpoint for Render at `/` and `/healthz`

## Run locally
1. Use Python 3.13.5 (see `.python-version`).
2. Install dependencies: `pip install -r requirements.txt`
3. Set the environment variable `BOT_TOKEN` to your Telegram bot token.
4. Run: `python main.py`

The bot creates `bot.db` automatically on first start. If running locally, the health server uses port 10000 unless `PORT` is set.

## Required channel setup
Add the bot as an administrator to each required channel so it can check membership.
Use `/admin` in Telegram as the account `@xoyli` to configure required channels.

For public channels, enter `@channelusername` or its public t.me URL.
For private channels, enter `-1001234567890 | https://t.me/+invite_code` (replace both values).

## Deploy on Render
This repository includes `render.yaml` for a free Web Service. It runs the Telegram polling bot and binds an HTTP health endpoint to Render's `PORT`, so Render's port check can succeed.

1. Create a new Web Service from this GitHub repository, or use the Blueprint from `render.yaml`.
2. Set `BOT_TOKEN` in the Render Environment settings to your token from `@BotFather`. Do not commit the token to GitHub.
3. Use branch `main`, build command `pip install -r requirements.txt`, and start command `python main.py`.
4. Wait for the deploy logs to show that the health server is listening and the bot is starting.
5. Add the bot as an administrator to every required channel before using force-subscription checks.

Important: Render's free Web Services can sleep after a period without inbound HTTP traffic, so a free service may not keep a polling bot online continuously. SQLite data on an ephemeral filesystem may also be lost after a restart or redeploy; configure persistent storage or use a managed database if data must survive.
