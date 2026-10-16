import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'xoyli').lstrip('@').lower()
ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
DB_PATH = os.getenv('DB_PATH', 'bot.sqlite3')
DEFAULT_CHANNEL = os.getenv('REQUIRED_CHANNEL', '@voodoo_channels').strip()
if not TOKEN:
    raise RuntimeError('Set BOT_TOKEN in environment variables.')

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN)
dp = Dispatcher()


def db():
    global DB_PATH
    try:
        con = sqlite3.connect(DB_PATH)
    except sqlite3.OperationalError:
        # If Render's persistent disk is missing or not mounted, use a
        # writable local file so the bot can start (data may be temporary).
        if DB_PATH == 'bot.sqlite3':
            raise
        logging.warning('Cannot open database at %s; falling back to local bot.sqlite3', DB_PATH)
        DB_PATH = 'bot.sqlite3'
        con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER NOT NULL DEFAULT 0,
            invited_by INTEGER, joined_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS referrals (
            invited_user_id INTEGER PRIMARY KEY, inviter_id INTEGER NOT NULL, rewarded INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS channels (
            channel TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
        );
        ''')
        con.execute('INSERT OR IGNORE INTO channels(channel) VALUES(?)', (DEFAULT_CHANNEL,))
        con.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(\'reward\',\'3\')')


def is_admin(user) -> bool:
    if not user:
        return False
    return user.id in ADMIN_IDS or (user.username or '').lower() == ADMIN_USERNAME


def reward_amount() -> int:
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key='reward'").fetchone()
        return max(0, int(row['value'])) if row else 3


def get_channels():
    with db() as con:
        return [r['channel'] for r in con.execute('SELECT channel FROM channels ORDER BY channel')]


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='👥 Пригласить', callback_data='invite'),
         InlineKeyboardButton(text='⭐ Мой баланс', callback_data='balance')],
        [InlineKeyboardButton(text='💸 Вывести', callback_data='withdraw'),
         InlineKeyboardButton(text='❓ Помощь', callback_data='help')],
    ])


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='⭐ Изменить награду', callback_data='adm_reward')],
        [InlineKeyboardButton(text='➕ Добавить канал', callback_data='adm_add_channel'), InlineKeyboardButton(text='➖ Удалить канал', callback_data='adm_remove_channel')],
        [InlineKeyboardButton(text='📋 Каналы', callback_data='adm_channels'), InlineKeyboardButton(text='🧾 Заявки на вывод', callback_data='adm_withdrawals')],
        [InlineKeyboardButton(text='👤 Статистика', callback_data='adm_stats')],
    ])


async def subscription_status(user_id: int):
    channels = get_channels()
    missing = []
    for channel in channels:
        try:
            chat = channel if channel.startswith('@') or channel.startswith('-100') else '@' + channel
            member = await bot.get_chat_member(chat_id=chat, user_id=user_id)
            if member.status not in ('creator', 'administrator', 'member') and not (member.status == 'restricted' and getattr(member, 'is_member', False)):
                missing.append(channel)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logging.warning('Cannot check channel %s: %s. Ensure the bot is admin/member there.', channel, e)
            missing.append(channel)
    return missing


async def require_subscription(message: Message) -> bool:
    missing = await subscription_status(message.from_user.id)
    if not missing:
        return True
    rows = [[InlineKeyboardButton(text=f'📢 Подписаться: {ch}', url=f'https://t.me/{ch.lstrip("@")}')]
            for ch in missing if ch.startswith('@')]
    rows.append([InlineKeyboardButton(text='✅ Проверить подписку', callback_data='check_sub')])
    await message.answer('Чтобы пользоваться ботом, подпишись на обязательные каналы и нажми «Проверить подписку».',
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    return False


@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    args = (message.text or '').split(maxsplit=1)
    inviter = None
    if len(args) > 1 and args[1].isdigit():
        candidate = int(args[1])
        if candidate != uid:
            inviter = candidate
    with db() as con:
        existing = con.execute('SELECT user_id FROM users WHERE user_id=?', (uid,)).fetchone()
        if not existing:
            con.execute('INSERT INTO users(user_id, username, invited_by, joined_at) VALUES(?,?,?,?)',
                         (uid, message.from_user.username, inviter, datetime.now(timezone.utc).isoformat()))
            if inviter:
                con.execute('INSERT OR IGNORE INTO referrals(invited_user_id, inviter_id, rewarded) VALUES(?,?,0)', (uid, inviter))
        else:
            con.execute('UPDATE users SET username=? WHERE user_id=?', (message.from_user.username, uid))
    if not await require_subscription(message):
        return
    # Credit the inviter only after the new user has joined and passed the channel gate.
    with db() as con:
        ref = con.execute('SELECT inviter_id, rewarded FROM referrals WHERE invited_user_id=?', (uid,)).fetchone()
        if ref and not ref['rewarded']:
            con.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (reward_amount(), ref['inviter_id']))
            con.execute('UPDATE referrals SET rewarded=1 WHERE invited_user_id=?', (uid,))
    await message.answer('Привет! За каждого нового приглашённого пользователя начисляется награда.\nВыбери действие:', reply_markup=main_keyboard())


@dp.callback_query(F.data == 'check_sub')
async def check_sub(call: CallbackQuery):
    missing = await subscription_status(call.from_user.id)
    await call.answer()
    if missing:
        await call.message.answer('Подписка пока не найдена. Подпишись на все каналы и попробуй снова.')
    else:
        reward = reward_amount()
        with db() as con:
            con.execute('INSERT OR IGNORE INTO users(user_id, username, joined_at) VALUES(?,?,?)',
                        (call.from_user.id, call.from_user.username, datetime.now(timezone.utc).isoformat()))
            ref = con.execute('SELECT inviter_id, rewarded FROM referrals WHERE invited_user_id=?',
                              (call.from_user.id,)).fetchone()
            if ref and not ref['rewarded']:
                con.execute('UPDATE users SET balance=balance+? WHERE user_id=?',
                            (reward, ref['inviter_id']))
                con.execute('UPDATE referrals SET rewarded=1 WHERE invited_user_id=?',
                            (call.from_user.id,))
        await call.message.answer('✅ Подписка подтверждена!', reply_markup=main_keyboard())


@dp.callback_query(F.data == 'balance')
async def balance(call: CallbackQuery):
    if await subscription_status(call.from_user.id):
        await call.answer('Сначала подпишись на обязательные каналы.', show_alert=True)
        return
    with db() as con:
        row = con.execute('SELECT balance FROM users WHERE user_id=?', (call.from_user.id,)).fetchone()
        count = con.execute('SELECT COUNT(*) AS n FROM referrals WHERE inviter_id=? AND rewarded=1',
                            (call.from_user.id,)).fetchone()['n']
    balance_value = row['balance'] if row else 0
    await call.message.answer(
        f'⭐ Твой баланс: {balance_value}\n👥 Успешных приглашений: {count}\n'
        f'Награда за приглашение: ⭐ {reward_amount()}',
        reply_markup=main_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == 'help')
async def help_menu(call: CallbackQuery):
    await call.message.answer(
        'ℹ️ Как пользоваться ботом:\n'
        '• «Пригласить» — получить свою ссылку и приглашать друзей.\n'
        '• «Мой баланс» — посмотреть баланс и успешные приглашения.\n'
        '• «Вывести» — отправить заявку администратору.\n\n'
        'Баланс внутри бота не является автоматическим переводом Telegram Stars. '
        'Заявки обрабатываются администратором вручную.',
        reply_markup=main_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == 'invite')
async def invite(call: CallbackQuery):
    if await subscription_status(call.from_user.id):
        await call.answer('Сначала подпишись на обязательные каналы.', show_alert=True)
        return
    me = await bot.get_me()
    link = f'https://t.me/{me.username}?start={call.from_user.id}'
    with db() as con:
        row = con.execute('SELECT balance FROM users WHERE user_id=?', (call.from_user.id,)).fetchone()
        balance = row['balance'] if row else 0
        count = con.execute('SELECT COUNT(*) AS n FROM referrals WHERE inviter_id=? AND rewarded=1', (call.from_user.id,)).fetchone()['n']
    await call.message.answer(f'Твоя ссылка для приглашений:\n{link}\n\nПриглашено: {count}\nБаланс: ⭐ {balance}\nНаграда за человека: ⭐ {reward_amount()}')
    await call.answer()


@dp.callback_query(F.data == 'withdraw')
async def withdraw(call: CallbackQuery):
    if await subscription_status(call.from_user.id):
        await call.answer('Сначала подпишись на обязательные каналы.', show_alert=True)
        return
    with db() as con:
        row = con.execute('SELECT balance FROM users WHERE user_id=?', (call.from_user.id,)).fetchone()
    balance = row['balance'] if row else 0
    if balance <= 0:
        await call.message.answer('Пока нечего выводить. Приглашай друзей, чтобы заработать ⭐.')
    else:
        with db() as con:
            cur = con.execute('INSERT INTO withdrawals(user_id, amount, created_at) VALUES(?,?,?)',
                              (call.from_user.id, balance, datetime.now(timezone.utc).isoformat()))
            request_id = cur.lastrowid
            con.execute('UPDATE users SET balance=0 WHERE user_id=?', (call.from_user.id,))
        await call.message.answer(f'✅ Заявка №{request_id} на вывод {balance} ⭐ отправлена администратору. Выплата обрабатывается вручную.')
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f'🧾 Заявка на вывод №{request_id}\nПользователь: @{call.from_user.username or "без username"} (ID {call.from_user.id})\nСумма: ⭐ {balance}',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Выплачено', callback_data=f'wd_done:{request_id}'), InlineKeyboardButton(text='↩️ Вернуть баланс', callback_data=f'wd_refund:{request_id}')]]))
            except Exception:
                logging.exception('Could not notify admin id %s', admin_id)
        # Username-only admin is supported for panel access; Telegram cannot initiate a DM by username.
        try:
            await bot.send_message(f'@{ADMIN_USERNAME}', f'🧾 Заявка на вывод №{request_id}\nПользователь: @{call.from_user.username or "без username"} (ID {call.from_user.id})\nСумма: ⭐ {balance}')
        except Exception:
            pass
    await call.answer()


@dp.message(Command('admin'))
async def admin(message: Message):
    if not is_admin(message.from_user):
        await message.answer('Нет доступа.')
        return
    await message.answer('⚙️ Админ-панель', reply_markup=admin_keyboard())


@dp.callback_query(F.data.startswith('adm_'))
async def admin_actions(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer('Нет доступа', show_alert=True)
        return
    action = call.data
    if action == 'adm_reward':
        await call.message.answer(f'Текущая награда: ⭐ {reward_amount()}\nОтправь команду: /setreward 3 (можно указать любое целое число от 0 до 100000).')
    elif action == 'adm_add_channel':
        await call.message.answer('Отправь /addchannel @имя_канала. Добавь бота администратором канала, чтобы проверка подписки работала.')
    elif action == 'adm_remove_channel':
        await call.message.answer('Отправь /removechannel @имя_канала')
    elif action == 'adm_channels':
        await call.message.answer('Обязательные каналы:\n' + ('\n'.join(get_channels()) or 'нет'))
    elif action == 'adm_withdrawals':
        with db() as con:
            rows = con.execute("SELECT id,user_id,amount,status,created_at FROM withdrawals WHERE status='pending' ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            await call.message.answer('Ожидающих заявок нет.')
        else:
            for r in rows:
                await call.message.answer(f'Заявка №{r["id"]}\nUser ID: {r["user_id"]}\nСумма: ⭐ {r["amount"]}\nСтатус: {r["status"]}', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Выплачено', callback_data=f'wd_done:{r["id"]}'), InlineKeyboardButton(text='↩️ Вернуть баланс', callback_data=f'wd_refund:{r["id"]}')]]))
    elif action == 'adm_stats':
        with db() as con:
            users = con.execute('SELECT COUNT(*) n FROM users').fetchone()['n']
            refs = con.execute('SELECT COUNT(*) n FROM referrals WHERE rewarded=1').fetchone()['n']
            pending = con.execute("SELECT COUNT(*) n FROM withdrawals WHERE status='pending'").fetchone()['n']
        await call.message.answer(f'Пользователей: {users}\nУспешных приглашений: {refs}\nЗаявок на вывод: {pending}\nНаграда: ⭐ {reward_amount()}')
    await call.answer()


@dp.message(Command('setreward'))
async def setreward(message: Message):
    if not is_admin(message.from_user):
        return await message.answer('Нет доступа.')
    parts = (message.text or '').split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) > 100000:
        return await message.answer('Использование: /setreward 3 (число от 0 до 100000)')
    with db() as con:
        con.execute("INSERT INTO settings(key,value) VALUES('reward',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (parts[1],))
    await message.answer(f'Награда изменена: ⭐ {parts[1]} за приглашение.')


@dp.message(Command('addchannel'))
async def addchannel(message: Message):
    if not is_admin(message.from_user):
        return await message.answer('Нет доступа.')
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer('Использование: /addchannel @имя_канала')
    channel = parts[1].strip()
    if not (channel.startswith('@') or channel.startswith('-100')):
        channel = '@' + channel
    with db() as con:
        con.execute('INSERT OR IGNORE INTO channels(channel) VALUES(?)', (channel,))
    await message.answer(f'Добавлен обязательный канал: {channel}. Убедись, что бот добавлен администратором канала.')


@dp.message(Command('removechannel'))
async def removechannel(message: Message):
    if not is_admin(message.from_user):
        return await message.answer('Нет доступа.')
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer('Использование: /removechannel @имя_канала')
    channel = parts[1].strip()
    if not (channel.startswith('@') or channel.startswith('-100')):
        channel = '@' + channel
    with db() as con:
        con.execute('DELETE FROM channels WHERE channel=?', (channel,))
    await message.answer(f'Канал удалён из списка обязательных: {channel}')


@dp.callback_query(F.data.startswith('wd_done:'))
async def withdrawal_done(call: CallbackQuery):
    if not is_admin(call.from_user):
        return await call.answer('Нет доступа', show_alert=True)
    wid = int(call.data.split(':', 1)[1])
    with db() as con:
        row = con.execute('SELECT user_id,amount,status FROM withdrawals WHERE id=?', (wid,)).fetchone()
        if not row or row['status'] != 'pending':
            await call.answer('Заявка уже обработана или не найдена', show_alert=True); return
        con.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (wid,))
    try: await bot.send_message(row['user_id'], f'✅ Заявка №{wid} отмечена администратором как выплаченная.')
    except Exception: pass
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer('Отмечено как выплачено')


@dp.callback_query(F.data.startswith('wd_refund:'))
async def withdrawal_refund(call: CallbackQuery):
    if not is_admin(call.from_user):
        return await call.answer('Нет доступа', show_alert=True)
    wid = int(call.data.split(':', 1)[1])
    with db() as con:
        row = con.execute('SELECT user_id,amount,status FROM withdrawals WHERE id=?', (wid,)).fetchone()
        if not row or row['status'] != 'pending':
            await call.answer('Заявка уже обработана или не найдена', show_alert=True); return
        con.execute("UPDATE withdrawals SET status='refunded' WHERE id=?", (wid,))
        con.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (row['amount'], row['user_id']))
    try: await bot.send_message(row['user_id'], f'↩️ Заявка №{wid} отменена, ⭐ {row["amount"]} возвращены на баланс.')
    except Exception: pass
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer('Баланс возвращён')


async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
