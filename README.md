# WoodooGifi Telegram Bot

Telegram bot built with Python, aiogram 3, and SQLite/aiosqlite.

## Features
- Required channel subscription checks
- Referral links in the form `?start=REF_<user_id>`
- One-time referral rewards after successful subscription verification
- SQLite user, channel, and settings tables
- Admin panel for Telegram username `@xoyli`
- Add/remove required channels and update referral reward

## Run locally
1. Install Python 3.10+.
2. Install dependencies: `pip install -r requirements.txt`
3. Set the environment variable `BOT_TOKEN` to your Telegram bot token.
4. Run: `python main.py`

The bot creates `bot.db` automatically on first start.

## Required channel setup
Add the bot as an administrator to each required channel so it can check membership.
Use `/admin` in Telegram as the account `@xoyli` to configure required channels.

For public channels, enter `@channelusername` or its public t.me URL.
For private channels, enter `-1001234567890 | https://t.me/+invite_code` (replace both values).

## Deployment
Run this as a long-running polling process/background worker, not as a web service expecting an HTTP port.
Set `BOT_TOKEN` in your hosting provider's environment variables. SQLite data may be lost on hosts with ephemeral filesystems; use a persistent disk or a managed database if durable storage is required.
