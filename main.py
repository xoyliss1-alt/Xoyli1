import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# -------------------- Configuration --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot.db"))
ADMIN_USERNAME = "xoyli"  # Telegram username, without @
DEFAULT_REFERRAL_REWARD = 10

router = Router()


# -------------------- Health check for Render Web Service --------------------
async def health_check(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "WoodooGifi"})


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/healthz", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("Health server listening on 0.0.0.0:%s", port)
    return runner


# -------------------- Database --------------------
async def init_db() -> None:
    """Create all tables and the default referral reward setting."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                balance INTEGER NOT NULL DEFAULT 0,
                referrer_id INTEGER,
                is_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL UNIQUE,
                invite_url TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Unique invited user prevents paying the same referral twice.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_rewards (
                invited_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                paid_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("referral_reward", str(DEFAULT_REFERRAL_REWARD)),
        )
        await db.commit()


async def get_referral_reward() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'referral_reward'"
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else DEFAULT_REFERRAL_REWARD


async def get_channels() -> list[tuple[int, str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, channel_id, invite_url FROM channels ORDER BY id"
        ) as cursor:
            return await cursor.fetchall()


def admin_user(username: str | None) -> bool:
    return (username or "").casefold() == ADMIN_USERNAME.casefold()


async def register_user(user, referrer_id: int | None = None) -> bool:
    """Insert a new user once. Returns True only for a newly inserted user."""
    if referrer_id == user.id:
        referrer_id = None

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user.id,)
        ) as cursor:
            existing = await cursor.fetchone()

        if existing:
            # Refresh profile fields, but do not overwrite an existing referrer.
            await db.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (user.username, user.first_name or "", user.id),
            )
            await db.commit()
            return False

        # Accept only an already registered referrer.
        if referrer_id is not None:
            async with db.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)
            ) as cursor:
                referrer = await cursor.fetchone()
            if not referrer:
                referrer_id = None

        await db.execute(
            """INSERT INTO users
               (user_id, username, first_name, balance, referrer_id)
               VALUES (?, ?, ?, 0, ?)""",
            (user.id, user.username, user.first_name or "", referrer_id),
        )
        await db.commit()
        return True


async def mark_verified_and_pay_referrer(user_id: int) -> int:
    """Mark a user verified and pay their referrer at most once. Returns reward."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT referrer_id, is_verified FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await db.commit()
            return 0

        referrer_id, was_verified = row
        await db.execute(
            "UPDATE users SET is_verified = 1 WHERE user_id = ?", (user_id,)
        )

        paid_amount = 0
        if referrer_id and not was_verified:
            async with db.execute(
                "SELECT value FROM settings WHERE key = 'referral_reward'"
            ) as cursor:
                setting = await cursor.fetchone()
            reward = max(0, int(setting[0])) if setting else DEFAULT_REFERRAL_REWARD

            # The primary key on invited_id is the final duplicate-payment guard.
            cursor = await db.execute(
                """INSERT OR IGNORE INTO referral_rewards
                   (invited_id, referrer_id, amount) VALUES (?, ?, ?)""",
                (user_id, referrer_id, reward),
            )
            if cursor.rowcount == 1:
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (reward, referrer_id),
                )
                paid_amount = reward

        await db.commit()
        return paid_amount


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT user_id, username, first_name, balance, referrer_id,
                      is_verified
               FROM users WHERE user_id = ?""",
            (user_id,),
        ) as cursor:
            return await cursor.fetchone()


async def set_referral_reward(reward: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO settings (key, value) VALUES ('referral_reward', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (str(reward),),
        )
        await db.commit()


# -------------------- Keyboards --------------------
def main_menu_keyboard(is_admin: bool = False):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile"),
        InlineKeyboardButton(text="⭐ Баланс", callback_data="balance"),
    )
    builder.row(
        InlineKeyboardButton(text="🎁 Заработать звезды", callback_data="earn"),
        InlineKeyboardButton(text="📤 Вывести звезды", callback_data="withdraw"),
    )
    builder.row(
        InlineKeyboardButton(text="ℹ️ О проекте", callback_data="about"),
    )
    if is_admin:
        builder.row(
            InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")
        )
    return builder.as_markup()


async def subscription_keyboard():
    builder = InlineKeyboardBuilder()
    for _, channel_id, invite_url in await get_channels():
        label = channel_id[1:] if channel_id.startswith("@") else channel_id
        builder.row(InlineKeyboardButton(text=f"📢 {label}", url=invite_url))
    builder.row(
        InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")
    )
    return builder.as_markup()


def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить канал", callback_data="admin_add_channel"))
    builder.row(InlineKeyboardButton(text="➖ Удалить канал", callback_data="admin_delete_channel"))
    builder.row(InlineKeyboardButton(text="⚙️ Изменить награду за реферала", callback_data="admin_reward"))
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="home"))
    return builder.as_markup()


def channels_delete_keyboard(channels: list[tuple[int, str, str]]):
    builder = InlineKeyboardBuilder()
    for row_id, channel_id, _ in channels:
        builder.row(
            InlineKeyboardButton(
                text=f"❌ {channel_id}",
                callback_data=f"delete_channel:{row_id}",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin"))
    return builder.as_markup()


# -------------------- FSM states --------------------
class AdminStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_reward = State()


# -------------------- Subscription helpers --------------------
async def is_subscribed_to_all(bot: Bot, user_id: int) -> bool:
    for _, channel_id, _ in await get_channels():
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                return False
            if member.status == ChatMemberStatus.RESTRICTED and not getattr(member, "is_member", False):
                return False
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logging.warning("Cannot check subscription for %s: %s", channel_id, exc)
            # Fail closed: do not allow access when a channel cannot be checked.
            return False
    return True


def parse_channel_input(raw: str) -> tuple[str, str] | None:
    """Accept @username, t.me URL, or 'channel_id | invite_url' for private channels."""
    value = raw.strip()
    if "|" in value:
        channel_id, invite_url = [part.strip() for part in value.split("|", 1)]
        if channel_id and invite_url.startswith(("https://t.me/", "http://t.me/")):
            return channel_id, invite_url.replace("http://", "https://", 1)
        return None

    if value.startswith("@"):
        username = value[1:]
        if re.fullmatch(r"[A-Za-z0-9_]{5,}", username):
            return f"@{username}", f"https://t.me/{username}"

    if value.startswith(("https://t.me/", "http://t.me/")):
        parsed = urlparse(value.replace("http://", "https://", 1))
        path = parsed.path.strip("/")
        if path and not path.startswith("+") and not path.startswith("joinchat/"):
            username = path.split("/")[0]
            if re.fullmatch(r"[A-Za-z0-9_]{5,}", username):
                return f"@{username}", f"https://t.me/{username}"
        return None

    if value.lstrip("-").isdigit():
        return value, ""

    return None


async def add_channel(channel_id: str, invite_url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO channels (channel_id, invite_url) VALUES (?, ?)",
            (channel_id, invite_url),
        )
        await db.commit()
        return cursor.rowcount == 1


# -------------------- Start and force subscribe --------------------
@router.message(CommandStart())
async def start_handler(message: Message, bot: Bot):
    args = (message.text or "").split(maxsplit=1)
    referrer_id = None
    if len(args) > 1:
        match = re.fullmatch(r"REF_(\d+)", args[1].strip())
        if match:
            referrer_id = int(match.group(1))

    await register_user(message.from_user, referrer_id)

    if not await is_subscribed_to_all(bot, message.from_user.id):
        await message.answer(
            "⚠️ Для использования бота подпишись на наши каналы:",
            reply_markup=await subscription_keyboard(),
        )
        return

    await mark_verified_and_pay_referrer(message.from_user.id)
    await show_main_menu(message)


async def show_main_menu(message: Message):
    name = message.from_user.first_name or "друг"
    text = (
        "🌟 Добро пожаловать в WoodooGifi! 🌟\n\n"
        f"👋 Привет, {name}!\n"
        "🚀 Получай звезды за приглашение людей! WoodooGifi кормит!\n\n"
        "✨ Зарабатывай Telegram Stars, собирай команду друзей и выводи их в любое время!\n\n"
        "👇 Выбери нужный раздел в меню:"
    )
    await message.answer(
        text,
        reply_markup=main_menu_keyboard(admin_user(message.from_user.username)),
    )


@router.callback_query(F.data == "check_sub")
async def check_subscription_callback(callback: CallbackQuery, bot: Bot):
    if not await is_subscribed_to_all(bot, callback.from_user.id):
        await callback.answer("Вы подписались не на все каналы!", show_alert=True)
        return

    await mark_verified_and_pay_referrer(callback.from_user.id)
    await callback.answer("Подписка проверена!")
    await callback.message.answer(
        "🌟 Добро пожаловать в WoodooGifi! 🌟\n\n"
        f"👋 Привет, {callback.from_user.first_name or 'друг'}!\n"
        "🚀 Получай звезды за приглашение людей! WoodooGifi кормит!\n\n"
        "✨ Зарабатывай Telegram Stars, собирай команду друзей и выводи их в любое время!\n\n"
        "👇 Выбери нужный раздел в меню:",
        reply_markup=main_menu_keyboard(admin_user(callback.from_user.username)),
    )


# -------------------- Main menu callbacks --------------------
@router.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery, bot: Bot):
    if not await is_subscribed_to_all(bot, callback.from_user.id):
        await callback.message.answer(
            "⚠️ Для использования бота подпишись на наши каналы:",
            reply_markup=await subscription_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.answer(
        "👇 Выбери нужный раздел в меню:",
        reply_markup=main_menu_keyboard(admin_user(callback.from_user.username)),
    )
    await callback.answer()


@router.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала отправь /start", show_alert=True)
        return
    user_id, username, first_name, balance, referrer_id, _ = user
    username_text = f"@{username}" if username else "не указан"
    text = (
        "👤 <b>Мой профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👋 Имя: {first_name}\n"
        f"🔗 Username: {username_text}\n"
        f"⭐ Баланс: {balance}"
    )
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "balance")
async def balance_callback(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    balance = user[3] if user else 0
    await callback.message.answer(f"⭐ Твой баланс: <b>{balance}</b> звёзд.")
    await callback.answer()


@router.callback_query(F.data == "earn")
async def earn_callback(callback: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=REF_{callback.from_user.id}"
    reward = await get_referral_reward()
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📨 Поделиться ссылкой", url=link))
    await callback.message.answer(
        "🎁 <b>Заработать звезды</b>\n\n"
        f"Приглашай друзей по своей ссылке. За каждого нового пользователя, "
        f"который подпишется на все обязательные каналы, ты получишь {reward} звёзд.\n\n"
        f"Твоя ссылка:\n<code>{link}</code>",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "withdraw")
async def withdraw_callback(callback: CallbackQuery):
    await callback.message.answer(
        "📤 <b>Вывод звёзд</b>\n\n"
        "Запрос вывода пока не подключён. Свяжись с администратором проекта для уточнения условий."
    )
    await callback.answer()


@router.callback_query(F.data == "about")
async def about_callback(callback: CallbackQuery):
    await callback.message.answer(
        "ℹ️ <b>О WoodooGifi</b>\n\n"
        "Приглашай друзей, собирай команду и получай звёзды за новых пользователей, "
        "которые выполнили условие обязательной подписки."
    )
    await callback.answer()


# -------------------- Admin panel --------------------
@router.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    if not admin_user(message.from_user.username):
        await message.answer("⛔ Нет доступа.")
        return
    await state.clear()
    await message.answer("🛠 <b>Админ-панель WoodooGifi</b>", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin")
async def admin_callback(callback: CallbackQuery, state: FSMContext):
    if not admin_user(callback.from_user.username):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.message.answer(
        "🛠 <b>Админ-панель WoodooGifi</b>",
        reply_markup=admin_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_add_channel")
async def admin_add_channel_callback(callback: CallbackQuery, state: FSMContext):
    if not admin_user(callback.from_user.username):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_channel)
    await callback.message.answer(
        "Отправь @username или публичную ссылку канала.\n"
        "Для приватного канала отправь в формате:\n"
        "<code>-1001234567890 | https://t.me/+invite_code</code>\n\n"
        "Для числового ID без ссылки бот сохранит канал, но кнопку подписки показать не сможет."
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_channel)
async def admin_save_channel(message: Message, state: FSMContext):
    if not admin_user(message.from_user.username):
        await state.clear()
        await message.answer("⛔ Нет доступа.")
        return
    parsed = parse_channel_input(message.text or "")
    if not parsed:
        await message.answer(
            "Не удалось распознать канал. Отправь @username, публичную ссылку или "
            "ID | ссылку-приглашение."
        )
        return

    channel_id, invite_url = parsed
    if not invite_url:
        await message.answer(
            "Для кнопки подписки нужна ссылка на канал. Отправь ID и ссылку вместе:\n"
            "<code>-1001234567890 | https://t.me/+invite_code</code>"
        )
        return

    inserted = await add_channel(channel_id, invite_url)
    if inserted:
        await message.answer(f"✅ Канал сохранён: {channel_id}")
    else:
        await message.answer("Этот канал уже добавлен.")
    await state.clear()


@router.callback_query(F.data == "admin_delete_channel")
async def admin_delete_channel_callback(callback: CallbackQuery, state: FSMContext):
    if not admin_user(callback.from_user.username):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    channels = await get_channels()
    if not channels:
        await callback.message.answer("Список каналов пуст.", reply_markup=admin_keyboard())
    else:
        await callback.message.answer(
            "Выбери канал для удаления:",
            reply_markup=channels_delete_keyboard(channels),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_channel:"))
async def admin_delete_channel_confirm(callback: CallbackQuery):
    if not admin_user(callback.from_user.username):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        channel_row_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректный ID", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM channels WHERE id = ?", (channel_row_id,))
        await db.commit()
    if cursor.rowcount:
        await callback.message.answer("✅ Канал удалён.")
    else:
        await callback.message.answer("Канал уже удалён или не найден.")
    await callback.answer()


@router.callback_query(F.data == "admin_reward")
async def admin_reward_callback(callback: CallbackQuery, state: FSMContext):
    if not admin_user(callback.from_user.username):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_reward)
    current_reward = await get_referral_reward()
    await callback.message.answer(
        f"Текущая награда: {current_reward} звёзд.\n"
        "Отправь новое целое число (0 или больше)."
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_reward)
async def admin_save_reward(message: Message, state: FSMContext):
    if not admin_user(message.from_user.username):
        await state.clear()
        await message.answer("⛔ Нет доступа.")
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) > 1_000_000:
        await message.answer("Введи целое число от 0 до 1000000.")
        return
    reward = int(raw)
    await set_referral_reward(reward)
    await state.clear()
    await message.answer(f"✅ Награда за реферала установлена: {reward} звёзд.")


# -------------------- Application entry point --------------------
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError(
            "Укажи токен бота в переменной окружения BOT_TOKEN вместо YOUR_BOT_TOKEN."
        )

    await init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    runner = None
    try:
        runner = await start_health_server()
        await bot.delete_webhook(drop_pending_updates=False)
        logging.info("WoodooGifi bot is starting...")
        await dp.start_polling(bot)
    finally:
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
