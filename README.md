# Xoyli Stars Referral Bot

Telegram referral bot with a required-channel gate, configurable referral reward, manual withdrawal requests, and an admin panel.

## Features
- Main buttons: **Пригласить** and **Вывести**.
- Default reward: **3 internal Stars per successful referral**.
- Required channel by default: `@voodoo_channels`.
- Admin panel: `/admin` for `@Xoyli` (set `ADMIN_USERNAME=xoyli`).
- Admin commands: `/setreward 3`, `/addchannel @channel`, `/removechannel @channel`.
- Withdrawal requests are handled manually. Admin can mark them paid or refund the user's balance.
- SQLite storage for users, referrals, balances, channels, settings, and withdrawal requests.

## Important setup notes
1. Create a bot with Telegram's `@BotFather` and copy its token.
2. Add the bot as an **administrator** in `@voodoo_channels` and any required channels. The bot needs permission to inspect channel membership.
3. Set `BOT_TOKEN` in the environment. Never commit a real token to GitHub.
4. Set `ADMIN_IDS` to the numeric Telegram user ID for `@Xoyli`. Username matching is included for convenience, but a numeric ID is more reliable. Use a user-ID bot to find the numeric ID.
5. Install and run locally:
   ```bash
   pip install -r requirements.txt
   cp .env.example .env
   # edit .env with your real values
   python main.py
   ```
6. For Render, create a **Background Worker** from this repository. The included `render.yaml` configures a persistent disk at `/var/data` for SQLite. Set the environment variables in Render before starting.

## How the reward works
The referral reward is credited once when a newly referred user starts the bot and passes the required-channel check. Existing users cannot earn a second referral reward by restarting. This bot keeps an **internal balance** called Stars; it does not automatically transfer Telegram Stars. Withdrawal requests are sent for manual processing by the admin.

## Channel management
Use `/addchannel @public_channel` and `/removechannel @public_channel`. Add the bot as administrator to each channel for membership checks. Public `@channel` names work best. For private channels, configure the channel's numeric chat ID (usually beginning `-100`) and make sure users can access it; invite links for private channels need to be configured separately if you want the bot to display them.

## Security
- Keep `.env` and bot tokens out of GitHub.
- Use `ADMIN_IDS` with the correct numeric Telegram ID.
- Use GitHub Secrets / Render environment variables for credentials.
