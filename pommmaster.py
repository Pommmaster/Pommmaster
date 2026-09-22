# -*- coding: utf-8 -*-
import asyncio
import hashlib
import hmac
import io
import json
import logging
import math
import os
import random
import shutil
import urllib.parse
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import aiohttp
import aiosqlite
import segno
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template

# ==========================================
# MASTER CONFIGURATION & STORAGE PATHS
# ==========================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PomPomMasterEnterprise")

MASTER_USER = os.getenv("MASTER_USER", "nagato")
MASTER_PASS = os.getenv("MASTER_PASS", "nagato@123")
AUTH_SECRET = os.getenv("SESSION_SECRET", "super_master_session_key_9988_xyz_secure")

DATA_DIR = os.getenv("DATA_DIR", "/app/data" if os.path.exists("/app/data") else ".")
MASTER_DB_NAME = os.path.join(DATA_DIR, "master_database.db")
TENANTS_DIR = os.path.join(DATA_DIR, "tenants")
STATIC_DIR = os.path.join(os.getcwd(), "static")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TENANTS_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

MASTER_DB_LOCK = asyncio.Lock()
TENANT_DB_LOCKS = defaultdict(asyncio.Lock)

# ==========================================
# SECURE ZERO-DEPENDENCY COOKIE TOKENS
# ==========================================
def sign_token(data: str) -> str:
    sig = hmac.new(AUTH_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"

def verify_token(signed_token: str | None) -> str | None:
    if not signed_token or "." not in signed_token:
        return None
    data, sig = signed_token.rsplit(".", 1)
    expected_sig = hmac.new(AUTH_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected_sig):
        return data
    return None

# ==========================================
# MASTER DATABASE LAYER
# ==========================================
async def get_master_db():
    db = await aiosqlite.connect(MASTER_DB_NAME, timeout=30.0)
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA synchronous = NORMAL;")
    return db

async def init_master_db():
    async with MASTER_DB_LOCK:
        db = await get_master_db()
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_name TEXT NOT NULL,
                    telegram TEXT,
                    phone TEXT,
                    slug TEXT UNIQUE NOT NULL,
                    make_date TEXT NOT NULL,
                    expiry_date TEXT NOT NULL,
                    plan_days INTEGER NOT NULL,
                    price REAL DEFAULT 0,
                    status TEXT DEFAULT 'ACTIVE',
                    admin_username TEXT NOT NULL,
                    admin_password TEXT NOT NULL,
                    bot_token TEXT DEFAULT '',
                    bot_type TEXT DEFAULT 'pompom_v2',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            async with db.execute("PRAGMA table_info(clients);") as cur:
                columns = [col[1] for col in await cur.fetchall()]
                if "bot_type" not in columns:
                    await db.execute("ALTER TABLE clients ADD COLUMN bot_type TEXT DEFAULT 'pompom_v2';")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_clients_slug ON clients(slug);")
            await db.commit()
        finally:
            await db.close()

# ==========================================
# TENANT DATABASE LAYER (ISOLATED PER SLUG)
# ==========================================
def get_tenant_db_path(slug: str) -> str:
    return os.path.join(TENANTS_DIR, f"{slug}.db")

async def get_tenant_db(slug: str):
    db = await aiosqlite.connect(get_tenant_db_path(slug), timeout=30.0)
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA synchronous = NORMAL;")
    await db.execute("PRAGMA busy_timeout = 30000;")
    await db.execute("PRAGMA cache_size = -64000;")
    return db

async def init_tenant_db(slug: str, initial_token: str = ""):
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT,
                    username TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    premium_status TEXT DEFAULT 'Free',
                    is_banned INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    plan_name TEXT,
                    amount REAL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    name TEXT,
                    amount REAL,
                    validity TEXT,
                    access_link TEXT DEFAULT ''
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_users_joined ON users(joined_at DESC);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);")

            defaults = {
                "bot_token": initial_token,
                "admin_chat_id": "",
                "maintenance": "off",
                "upi_id": "YOUR_UPI",
                "payee_name": "YOUR_UPI_NAME",
                "welcome_photo": "https://img.sanishtech.com/u/8bb2886fa8263c48500c00cfb26e0b36.jpg",
                "welcome_text": (
                    "🌟 🔥 𝐏𝐑𝐄𝐌𝐈𝐔𝐌 𝐀𝐃𝐔𝐋𝐓 𝐂𝐎𝐋𝐋𝐄𝐂𝐓𝐈𝐎𝐍 𝐔𝐍𝐋𝐎𝐂𝐊𝐄𝐃 🔥\n\n"
                    "𝐇𝐃 + 𝐔𝐥𝐭𝐫𝐚-𝐅𝐫𝐞𝐬𝐡 𝐕𝐢𝐝𝐞𝐨𝐬 𝐀𝐯𝐚𝐢𝐥𝐚𝐛𝐥𝐞 𝐍𝐨𝐰\n\n"
                    "💦 𝐌𝐨𝐦-𝐒𝐨𝐧 𝐅𝐚𝐧𝐭𝐚𝐬𝐲\n💦 𝐁𝐫𝐨𝐭𝐡𝐞𝐫-𝐒𝐢𝐬𝐭𝐞𝐫 𝐓𝐚𝐛𝐨𝐨\n💦 𝐀𝐮𝐧𝐭𝐲 & 𝐁𝐡𝐚𝐛𝐡𝐢 𝐃𝐞𝐬𝐢 𝐇𝐨𝐭\n"
                    "💦 𝐓𝐞𝐞𝐧 𝐈𝐧𝐝𝐢𝐚𝐧 (𝟏𝟖+)\n💦 𝐈𝐧𝐬𝐭𝐚𝐠𝐫𝐚𝐦 𝐑𝐞𝐞𝐥𝐬 𝐒𝐭𝐚𝐫𝐬\n💦 𝐃𝐞𝐬𝐢 𝐁𝐡𝐚𝐛𝐡𝐢 & 𝐀𝐮𝐧𝐭𝐲 𝐒𝐞𝐫𝐢𝐞𝐬\n"
                    "💦 𝐅𝐨𝐫𝐞𝐢𝐠𝐧𝐞𝐫 & 𝐈𝐧𝐭𝐞𝐫𝐧𝐚𝐭𝐢𝐨𝐧𝐚𝐥\n💦 𝐇𝐚𝐫𝐝𝐜𝐨𝐫𝐞 & 𝐑𝐨𝐥𝐞𝐩𝐥𝐚𝐲 𝐂𝐨𝐥𝐥𝐞𝐜𝐭𝐢𝐨𝐧\n\n"
                    "✅ 𝐅𝐮𝐥𝐥 𝐇𝐃 𝐐𝐮𝐚𝐥𝐢𝐭𝐲\n✅ 𝐈𝐧𝐬𝐭𝐚𝐧𝐭 𝐃𝐞𝐥𝐢𝐯𝐞𝐫𝐲\n✅ 𝟏𝟎𝟎% 𝐖𝐨𝐫𝐤𝐢𝐧𝐠 𝐋𝐢𝐧𝐤𝐬"
                ),
                "plans_text": "💎 ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴs\n\n👇 sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘᴀᴄᴋᴀɢᴇ ʙᴇʟᴏᴡ",
                "demo_video": "",
            }
            for k, v in defaults.items():
                await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

            default_packs = [
                ("plan_1", "CHILD POM 🙈💋", 62.0, "Lifetime", "https://t.me/+YourLink1"),
                ("plan_2", "GAYY MIX", 60.0, "Lifetime", "https://t.me/+YourLink2"),
                ("plan_3", "CHILD INDIA 😄", 64.0, "Lifetime", "https://t.me/+YourLink3"),
                ("plan_4", "SMALL SON BIG GIRL 🤩", 61.0, "Lifetime", "https://t.me/+YourLink4"),
                ("plan_5", "BRO SIS AUNTY CHILD", 63.0, "Lifetime", "https://t.me/+YourLink5"),
                ("plan_6", "ANIMAL FUN😂", 59.0, "Lifetime", "https://t.me/+YourLink6"),
                ("plan_7", "IP CAM FAMILY 🤤", 58.0, "Lifetime", "https://t.me/+YourLink7"),
                ("plan_8", "HOUSEWIFE 😱", 49.0, "Lifetime", "https://t.me/+YourLink8"),
                ("plan_9", "MOM AND PAPA 😜 UNCLE AUNTY 😍", 50.0, "Lifetime", "https://t.me/+YourLink9"),
                ("plan_10", "PILLS", 65.0, "Lifetime", "https://t.me/+YourLink10"),
                ("plan_11", "SPA LEAK 🙈", 66.0, "Lifetime", "https://t.me/+YourLink11"),
                ("plan_12", "SNAP LEAK 😲", 67.0, "Lifetime", "https://t.me/+YourLink12"),
                ("plan_13", "INSTA LEAK 😜💋", 69.0, "Lifetime", "https://t.me/+YourLink13"),
                ("plan_14", "CP 35K 😋🤩", 150.0, "Lifetime", "https://t.me/+YourLink14"),
                ("plan_15", "350 ZIPS CP", 70.0, "Lifetime", "https://t.me/+YourLink15"),
                ("plan_16", "SABSE SASTA", 25.0, "Lifetime", "https://t.me/+YourLink16"),
                ("plan_17", "300 ADULT GROUPS", 150.0, "Lifetime", "https://t.me/+YourLink17"),
            ]
            for p in default_packs:
                await db.execute(
                    "INSERT OR IGNORE INTO plans (plan_id, name, amount, validity, access_link) VALUES (?, ?, ?, ?, ?)",
                    p,
                )
            await db.commit()
        finally:
            await db.close()

# ==========================================
# TENANT SETTINGS & REVENUE HELPERS
# ==========================================
async def get_tenant_setting(slug: str, key: str) -> str:
    db = await get_tenant_db(slug)
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""
    finally:
        await db.close()

async def update_tenant_setting(slug: str, key: str, value: str):
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            await db.commit()
        finally:
            await db.close()

async def get_tenant_admin_ids(slug: str) -> list[int]:
    chat_id_str = await get_tenant_setting(slug, "admin_chat_id")
    ids = []
    if chat_id_str:
        for x in chat_id_str.split(","):
            x = x.strip()
            if x.lstrip("-").isdigit():
                ids.append(int(x))
    return ids

async def get_tenant_all_plans(slug: str):
    db = await get_tenant_db(slug)
    try:
        async with db.execute("SELECT plan_id, name, amount, validity, COALESCE(access_link, '') FROM plans ORDER BY plan_id ASC") as cur:
            return await cur.fetchall()
    finally:
        await db.close()

async def get_tenant_plan(slug: str, plan_id: str):
    db = await get_tenant_db(slug)
    try:
        async with db.execute("SELECT plan_id, name, amount, validity, COALESCE(access_link, '') FROM plans WHERE plan_id = ?", (plan_id,)) as cur:
            return await cur.fetchone()
    finally:
        await db.close()

async def get_tenant_plan_by_name(slug: str, name: str):
    db = await get_tenant_db(slug)
    try:
        async with db.execute("SELECT plan_id, name, amount, validity, COALESCE(access_link, '') FROM plans WHERE name = ?", (name,)) as cur:
            return await cur.fetchone()
    finally:
        await db.close()

async def update_tenant_plan(slug: str, plan_id: str, name: str, amount: float, validity: str, access_link: str = ""):
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute(
                "UPDATE plans SET name = ?, amount = ?, validity = ?, access_link = ? WHERE plan_id = ?",
                (name, amount, validity, access_link.strip(), plan_id),
            )
            await db.commit()
        finally:
            await db.close()

async def add_tenant_new_plan(slug: str, plan_id: str, name: str, amount: float, validity: str, access_link: str = ""):
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute(
                "INSERT INTO plans (plan_id, name, amount, validity, access_link) VALUES (?, ?, ?, ?, ?)",
                (plan_id, name, amount, validity, access_link.strip()),
            )
            await db.commit()
        finally:
            await db.close()

async def delete_tenant_plan(slug: str, plan_id: str):
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
            await db.commit()
        finally:
            await db.close()

async def add_tenant_user(slug: str, user: types.User):
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute("""
                INSERT INTO users (user_id, full_name, username)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET full_name = excluded.full_name, username = excluded.username
            """, (user.id, user.full_name, user.username or "N/A"))
            await db.commit()
        finally:
            await db.close()

async def get_tenant_user(slug: str, user_id: int):
    db = await get_tenant_db(slug)
    try:
        async with db.execute("SELECT user_id, full_name, username, joined_at, premium_status, is_banned FROM users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()
    finally:
        await db.close()

async def get_tenant_paginated_users(slug: str, limit: int = 50, offset: int = 0, search: str = ""):
    db = await get_tenant_db(slug)
    try:
        search_query = f"%{search.strip().lstrip('@')}%"
        if search.strip():
            async with db.execute(
                "SELECT COUNT(*) FROM users WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ? OR full_name LIKE ?",
                (search_query, search_query, search_query)
            ) as cur:
                total_count = (await cur.fetchone())[0]

            async with db.execute(
                "SELECT user_id, full_name, username, joined_at, premium_status, is_banned FROM users WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ? OR full_name LIKE ? ORDER BY joined_at DESC LIMIT ? OFFSET ?",
                (search_query, search_query, search_query, limit, offset),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                total_count = (await cur.fetchone())[0]

            async with db.execute(
                "SELECT user_id, full_name, username, joined_at, premium_status, is_banned FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cur:
                rows = await cur.fetchall()

        return rows, total_count
    finally:
        await db.close()

async def update_tenant_user_subscription(slug: str, user_id: int, plan_name: str):
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute("UPDATE users SET premium_status = ? WHERE user_id = ?", (plan_name, user_id))
            await db.commit()
        finally:
            await db.close()

async def set_tenant_user_ban_status(slug: str, user_id: int, is_banned: int):
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (int(is_banned), int(user_id)))
            await db.commit()
        finally:
            await db.close()

async def get_tenant_dashboard_metrics(slug: str):
    db = await get_tenant_db(slug)
    try:
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments WHERE status='approved'") as cur:
            row = await cur.fetchone()
            paid_orders = row[0] if row else 0
            revenue = row[1] if row else 0.0

        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total_users = (await cur.fetchone())[0]

        async with db.execute("""
            SELECT p.id, p.user_id, COALESCE(u.username, 'N/A'), p.plan_name, p.amount, p.status, p.created_at
            FROM payments p
            LEFT JOIN users u ON p.user_id = u.user_id
            ORDER BY p.id DESC 
            LIMIT 50
        """) as cur:
            all_orders = await cur.fetchall()

        return {
            "paid_orders": paid_orders,
            "revenue": f"{revenue:,.2f}",
            "total_users": total_users,
            "recent_orders": all_orders[:20],
            "all_orders": all_orders,
        }
    finally:
        await db.close()

# ==========================================
# UPI QR GENERATOR & HELPER
# ==========================================
def _generate_qr_sync(upi_url: str) -> io.BytesIO:
    qr = segno.make(upi_url, error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=8, border=2)
    buffer.seek(0)
    return buffer

async def generate_tenant_upi_qr(slug: str, plan_name: str, amount: float) -> io.BytesIO:
    upi_id = await get_tenant_setting(slug, "upi_id")
    payee_name = await get_tenant_setting(slug, "payee_name")
    upi_params = {
        "pa": upi_id,
        "pn": payee_name,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": f"Payment for {plan_name}",
    }
    upi_url = "upi://pay?" + urllib.parse.urlencode(upi_params)
    return await asyncio.to_thread(_generate_qr_sync, upi_url)

# ==========================================
# TELEGRAM BOT ENGINE
# ==========================================
class PaymentStates(StatesGroup):
    waiting_for_screenshot = State()

class MultiBotEngine:
    def __init__(self):
        self.active_bots: dict[str, Bot] = {}
        self.active_dispatchers: dict[str, Dispatcher] = {}
        self.running_tasks: dict[str, asyncio.Task] = {}
        self.active_sessions: dict[str, AiohttpSession] = {}
        self.user_messages: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))

    def track(self, slug: str, chat_id: int, message_id: int):
        if message_id not in self.user_messages[slug][chat_id]:
            self.user_messages[slug][chat_id].append(message_id)

    async def delete_old_messages(self, slug: str, chat_id: int):
        bot = self.active_bots.get(slug)
        if not bot:
            return
        all_ids = self.user_messages[slug].get(chat_id, [])
        self.user_messages[slug][chat_id] = []
        for mid in all_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

    async def notify_payment_approved(self, slug: str, user_id: int, plan_name: str):
        bot = self.active_bots.get(slug)
        if not bot:
            return
        plan_info = await get_tenant_plan_by_name(slug, plan_name)
        access_link = plan_info[4] if plan_info and len(plan_info) > 4 else ""

        builder = InlineKeyboardBuilder()
        if access_link and access_link.strip().startswith(("http://", "https://", "t.me/")):
            link_url = access_link.strip()
            if link_url.startswith("t.me/"):
                link_url = "https://" + link_url
            builder.button(text="🔗 Join VIP Channel / Access Link", url=link_url)
        builder.button(text="🏠 Home", callback_data="btn_home")
        builder.adjust(1)

        caption = f"🎉 <b>Payment Approved!</b>\n\nYour subscription for <b>{plan_name}</b> is active!\n"
        if access_link:
            caption += "\n👉 Click below to access your content:"

        try:
            await bot.send_message(chat_id=user_id, text=caption, reply_markup=builder.as_markup())
        except Exception as e:
            logger.warning(f"[{slug}] Approval notification error: {e}")

    async def send_welcome_flow(self, slug: str, chat_id: int):
        bot = self.active_bots.get(slug)
        if not bot:
            return
        photo_url = await get_tenant_setting(slug, "welcome_photo")
        caption = await get_tenant_setting(slug, "welcome_text")

        builder = InlineKeyboardBuilder()
        plans = await get_tenant_all_plans(slug)
        for pid, name, price, _, _ in plans:
            builder.button(text=f"🟢 {name} - ₹{int(price)}", callback_data=f"buy_plan:{pid}")
        builder.adjust(1)

        sent_photo = False
        if photo_url and photo_url.startswith(("http://", "https://", "AgAC")):
            try:
                async with asyncio.timeout(2.5):
                    m1 = await bot.send_photo(chat_id=chat_id, photo=photo_url, caption=caption, reply_markup=builder.as_markup())
                    self.track(slug, chat_id, m1.message_id)
                    sent_photo = True
            except Exception:
                pass

        if not sent_photo:
            m1 = await bot.send_message(chat_id=chat_id, text=caption, reply_markup=builder.as_markup())
            self.track(slug, chat_id, m1.message_id)

    async def start_tenant_bot(self, slug: str, token: str):
        await self.stop_tenant_bot(slug)
        token = token.strip()
        if not token or ":" not in token:
            return

        session = AiohttpSession(timeout=20.0)
        bot = Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=MemoryStorage())

        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass

        self._attach_handlers(dp, slug)

        async def runner():
            while True:
                try:
                    await dp.start_polling(bot, drop_pending_updates=True, allowed_updates=["message", "callback_query"])
                    break
                except TelegramConflictError:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[{slug}] Polling error: {e}")
                    await asyncio.sleep(4)

        self.active_bots[slug] = bot
        self.active_dispatchers[slug] = dp
        self.active_sessions[slug] = session
        self.running_tasks[slug] = asyncio.create_task(runner())

    async def stop_tenant_bot(self, slug: str):
        if slug in self.running_tasks:
            task = self.running_tasks.pop(slug)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if slug in self.active_dispatchers:
            try:
                await self.active_dispatchers.pop(slug).stop_polling()
            except Exception:
                pass
        if slug in self.active_bots:
            bot = self.active_bots.pop(slug)
            session = self.active_sessions.pop(slug, None)
            try:
                if bot.session:
                    await bot.session.close()
            except Exception:
                pass

    def _attach_handlers(self, dp: Dispatcher, slug: str):
        engine = self

        class SecurityMiddleware(BaseMiddleware):
            async def __call__(self, handler, event: types.TelegramObject, data: dict):
                user = data.get("event_from_user")
                admin_ids = await get_tenant_admin_ids(slug)
                if not user or user.id in admin_ids:
                    return await handler(event, data)
                user_record = await get_tenant_user(slug, user.id)
                if user_record and user_record[5] == 1:
                    if isinstance(event, types.Message):
                        await event.answer("You are banned from using this bot.")
                    return
                if await get_tenant_setting(slug, "maintenance") == "on":
                    if isinstance(event, types.Message):
                        await event.answer("Bot is under maintenance.")
                    return
                return await handler(event, data)

        dp.message.outer_middleware(SecurityMiddleware())
        dp.callback_query.outer_middleware(SecurityMiddleware())

        @dp.message(Command("cancel"))
        async def cancel_handler(message: types.Message, state: FSMContext):
            await state.clear()
            await message.answer("❌ Cancelled.")
            await engine.send_welcome_flow(slug, message.chat.id)

        @dp.message(CommandStart())
        async def handle_start(message: types.Message):
            await add_tenant_user(slug, message.from_user)
            await engine.send_welcome_flow(slug, message.chat.id)

        @dp.callback_query(F.data == "btn_home")
        async def nav_home(callback: types.CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer()
            await engine.delete_old_messages(slug, callback.message.chat.id)
            try:
                await callback.message.delete()
            except Exception:
                pass
            await engine.send_welcome_flow(slug, callback.message.chat.id)

        @dp.callback_query(F.data.startswith("buy_plan:"))
        async def process_plan_selection(callback: types.CallbackQuery, state: FSMContext):
            await callback.answer()
            try:
                await callback.message.delete()
            except Exception:
                pass
            bot = engine.active_bots.get(slug)
            if not bot:
                return

            pid = callback.data.split(":")[1]
            plan = await get_tenant_plan(slug, pid)
            if not plan:
                return
            _, plan_name, amount, validity, _ = plan
            qr_buf = await generate_tenant_upi_qr(slug, plan_name, amount)
            photo_file = BufferedInputFile(qr_buf.getvalue(), filename="qr.png")
            upi_id = await get_tenant_setting(slug, "upi_id")
            payee = await get_tenant_setting(slug, "payee_name")

            caption = (
                f"📲 <b>UPI PAYMENT</b>\n\n"
                f"📦 <b>Pack:</b> {plan_name}\n"
                f"💰 <b>Amount:</b> ₹{amount:.2f}\n"
                f"⏳ <b>Validity:</b> {validity}\n\n"
                f"👤 <b>Name:</b> {payee}\n"
                f"📱 <b>UPI ID:</b> <code>{upi_id}</code>\n\n"
                f"1️⃣ Scan the QR code above\n"
                f"2️⃣ Pay exactly ₹{amount:.2f}\n"
                f"3️⃣ Click <b>CHECK PAYMENT</b> below"
            )

            await state.update_data(current_plan=plan_name, current_amount=amount)
            builder = InlineKeyboardBuilder()
            builder.button(text="📥 CHECK PAYMENT", callback_data="check_payment")
            builder.button(text="🔙 BACK", callback_data="btn_home")
            builder.adjust(1)

            msg = await bot.send_photo(
                chat_id=callback.message.chat.id,
                photo=photo_file,
                caption=caption,
                reply_markup=builder.as_markup(),
            )
            engine.track(slug, callback.message.chat.id, msg.message_id)

        @dp.callback_query(F.data == "check_payment")
        async def handle_check_payment(callback: types.CallbackQuery, state: FSMContext):
            await callback.answer()
            try:
                await callback.message.delete()
            except Exception:
                pass
            bot = engine.active_bots.get(slug)
            if not bot:
                return
            await state.set_state(PaymentStates.waiting_for_screenshot)
            msg = await bot.send_message(
                chat_id=callback.message.chat.id,
                text="📸 Please send the payment screenshot image now.\n\nType /cancel to abort.",
            )
            engine.track(slug, callback.message.chat.id, msg.message_id)

        @dp.message(PaymentStates.waiting_for_screenshot, F.photo | (F.document & F.document.mime_type.startswith("image/")))
        async def process_payment_proof(message: types.Message, state: FSMContext):
            bot = engine.active_bots.get(slug)
            if not bot:
                return
            data = await state.get_data()
            plan_name = data.get("current_plan", "VIP Pack")
            amount = data.get("current_amount", 0)

            async with TENANT_DB_LOCKS[slug]:
                db = await get_tenant_db(slug)
                try:
                    cur = await db.execute("INSERT INTO payments (user_id, plan_name, amount) VALUES (?, ?, ?)", (message.from_user.id, plan_name, amount))
                    pid = cur.lastrowid
                    await db.commit()
                finally:
                    await db.close()

            await state.clear()
            await message.answer("✅ Screenshot received! Admin verification in progress.")

            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Accept", callback_data=f"adm_pay:{pid}:approved")
            builder.button(text="❌ Reject", callback_data=f"adm_pay:{pid}:rejected")
            builder.adjust(2)

            caption = (
                f"🚨 <b>New Payment Proof</b>\n\n"
                f"<b>Order:</b> #{pid}\n"
                f"<b>User ID:</b> <code>{message.from_user.id}</code>\n"
                f"<b>Username:</b> @{message.from_user.username or 'N/A'}\n"
                f"<b>Pack:</b> {plan_name}\n"
                f"<b>Amount:</b> ₹{amount}"
            )
            file_id = message.photo[-1].file_id if message.photo else message.document.file_id
            for admin_id in await get_tenant_admin_ids(slug):
                try:
                    await bot.send_photo(chat_id=admin_id, photo=file_id, caption=caption, reply_markup=builder.as_markup())
                except Exception:
                    pass

        @dp.callback_query(F.data.startswith("adm_pay:"))
        async def handle_admin_pay_approval(callback: types.CallbackQuery):
            bot = engine.active_bots.get(slug)
            if not bot:
                return
            admin_ids = await get_tenant_admin_ids(slug)
            if callback.from_user.id in admin_ids:
                _, pid, act = callback.data.split(":")
                async with TENANT_DB_LOCKS[slug]:
                    db = await get_tenant_db(slug)
                    try:
                        async with db.execute("SELECT user_id, plan_name FROM payments WHERE id=?", (int(pid),)) as cur:
                            p = await cur.fetchone()
                        if not p:
                            await callback.answer("Not found.")
                            return
                        t_uid, pl = p
                        if act == "approved":
                            await db.execute("UPDATE payments SET status='approved' WHERE id=?", (int(pid),))
                            await db.execute("UPDATE users SET premium_status=? WHERE user_id=?", (pl, t_uid))
                            await db.commit()
                            await engine.notify_payment_approved(slug, t_uid, pl)
                            await callback.message.edit_caption(caption=callback.message.caption + "\n\nSTATUS: APPROVED")
                        else:
                            await db.execute("UPDATE payments SET status='rejected' WHERE id=?", (int(pid),))
                            await db.commit()
                            try:
                                await bot.send_message(t_uid, "Payment verification failed.")
                            except Exception:
                                pass
                            await callback.message.edit_caption(caption=callback.message.caption + "\n\nSTATUS: REJECTED")
                    finally:
                        await db.close()
                await callback.answer("Status updated.")

bot_engine = MultiBotEngine()

# ==========================================
# FASTAPI LIFECYCLE
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_master_db()
    db = await get_master_db()
    try:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT slug, bot_token, expiry_date, status FROM clients") as cur:
            clients = await cur.fetchall()
        today = datetime.now().date()
        for c in clients:
            exp_date = datetime.strptime(c["expiry_date"], "%Y-%m-%d").date()
            if c["status"] == "ACTIVE" and exp_date >= today:
                await init_tenant_db(c["slug"], c["bot_token"])
                if c["bot_token"]:
                    await bot_engine.start_tenant_bot(c["slug"], c["bot_token"])
    finally:
        await db.close()

    yield

    for slug in list(bot_engine.running_tasks.keys()):
        await bot_engine.stop_tenant_bot(slug)

app = FastAPI(title="Pom Pom Master Enterprise", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ==========================================
# MASTER CONSOLE & LOGIN INTERFACES
# ==========================================
MASTER_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Master Console &mdash; Nagato Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700;800;900&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #03010a; }
        .font-tech { font-family: 'Orbitron', monospace; }
        .console-frame {
            background: linear-gradient(180deg, rgba(14, 8, 30, 0.96) 0%, rgba(6, 3, 16, 0.98) 100%);
            border: 2px solid #00f0ff;
            box-shadow: 0 0 25px rgba(0, 240, 255, 0.35);
            border-radius: 26px;
        }
    </style>
</head>
<body class="flex items-center justify-center p-4 min-h-screen">
    <div class="w-full max-w-[360px]">
        <div class="console-frame p-8 space-y-6">
            <h1 class="text-2xl font-bold font-tech text-cyan-300 text-center">MASTER CONSOLE</h1>
            {% if error %}<div class="p-2 rounded bg-rose-500/20 text-rose-300 text-xs text-center">{{ error }}</div>{% endif %}
            <form method="POST" action="/master/login" class="space-y-4 text-xs font-mono">
                <div>
                    <label class="block text-cyan-300 mb-1">USERNAME</label>
                    <input type="text" name="username" required class="w-full bg-[#080416] border border-cyan-500/40 rounded-xl p-3 text-white">
                </div>
                <div>
                    <label class="block text-cyan-300 mb-1">PASSWORD</label>
                    <input type="password" name="password" required class="w-full bg-[#080416] border border-cyan-500/40 rounded-xl p-3 text-white">
                </div>
                <button type="submit" class="w-full bg-gradient-to-r from-pink-500 to-cyan-400 py-3 rounded-xl font-bold uppercase text-white">Authenticate</button>
            </form>
        </div>
    </div>
</body>
</html>"""

@app.get("/master/login", response_class=HTMLResponse)
async def master_login_view(error: str | None = None):
    return HTMLResponse(Template(MASTER_LOGIN_PAGE).render(error=error))

@app.post("/master/login")
async def master_login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == MASTER_USER and password == MASTER_PASS:
        token = sign_token("master_admin")
        response = RedirectResponse(url="/master/clients", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="master_auth", value=token, httponly=True, max_age=86400 * 7)
        return response
    return HTMLResponse(Template(MASTER_LOGIN_PAGE).render(error="Incorrect master credentials."), status_code=401)

@app.get("/master/logout")
async def master_logout():
    response = RedirectResponse(url="/master/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="master_auth")
    return response

@app.get("/master/clients", response_class=HTMLResponse)
async def master_clients_dashboard(request: Request, message: str | None = None):
    auth_data = verify_token(request.cookies.get("master_auth"))
    if auth_data != "master_admin":
        return RedirectResponse(url="/master/login", status_code=status.HTTP_303_SEE_OTHER)

    today = datetime.now().date()
    db = await get_master_db()
    try:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clients ORDER BY id DESC") as cur:
            rows = await cur.fetchall()
        clients, active_count, total_revenue = [], 0, 0.0
        for r in rows:
            c = dict(r)
            exp_date = datetime.strptime(c["expiry_date"], "%Y-%m-%d").date()
            c["days_left"] = max(0, (exp_date - today).days)
            if c["status"] == "ACTIVE" and c["days_left"] > 0:
                active_count += 1
            total_revenue += float(c["price"] or 0)
            clients.append(c)
    finally:
        await db.close()

    master_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Master Reseller Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700&display=swap" rel="stylesheet">
</head>
<body class="bg-[#06040c] text-white p-8 max-w-5xl mx-auto space-y-6 font-mono">
    <div class="flex justify-between items-center border-b border-purple-900/40 pb-4">
        <h1 class="font-bold text-xl text-cyan-400 font-tech">🚀 Master Reseller Dashboard</h1>
        <a href="/master/logout" class="text-rose-400 text-xs underline">Sign Out</a>
    </div>
    {% if message %}<div class="p-3 bg-cyan-500/20 text-cyan-300 text-xs rounded">{{ message }}</div>{% endif %}
    
    <div class="bg-[#120b22] p-6 rounded-2xl border border-purple-900/50 space-y-4">
        <h3 class="text-sm font-bold text-fuchsia-300">Create New Client Panel (Pom Pom Bot v2)</h3>
        <form method="POST" action="/master/client/new" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
            <input type="text" name="client_name" required placeholder="Client Name" class="bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <input type="text" name="slug" required placeholder="URL Slug (e.g. vip-store)" class="bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <input type="text" name="bot_token" placeholder="Telegram Bot Token" class="bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <input type="text" name="admin_password" required placeholder="Admin Passcode" class="bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <button type="submit" class="sm:col-span-2 lg:col-span-4 bg-gradient-to-r from-fuchsia-600 to-cyan-500 py-2.5 rounded font-bold uppercase text-white">Provision Panel</button>
        </form>
    </div>

    <div class="bg-[#120b22] p-6 rounded-2xl border border-purple-900/50 space-y-4">
        <h3 class="text-sm font-bold text-cyan-300">All Client Panels</h3>
        <table class="w-full text-left text-xs">
            <thead class="text-purple-400 border-b border-purple-900/40">
                <tr><th class="p-2">Name</th><th class="p-2">Slug</th><th class="p-2">Expiry</th><th class="p-2">Admin Panel</th><th class="p-2 text-right">Action</th></tr>
            </thead>
            <tbody>
                {% for c in clients %}
                <tr class="border-b border-purple-900/20">
                    <td class="p-2 font-bold">{{ c.client_name }}</td>
                    <td class="p-2 text-cyan-400">/c/{{ c.slug }}/admin</td>
                    <td class="p-2">{{ c.expiry_date }}</td>
                    <td class="p-2"><a href="/c/{{ c.slug }}/admin" target="_blank" class="text-fuchsia-400 underline">Open Panel ↗</a></td>
                    <td class="p-2 text-right">
                        <form method="POST" action="/master/client/delete" onsubmit="return confirm('Delete client?');" class="inline">
                            <input type="hidden" name="client_id" value="{{ c.id }}">
                            <button type="submit" class="text-rose-400 underline">Delete</button>
                        </form>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5" class="p-4 text-center text-purple-400">No client panels created yet.</td></tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</body>
</html>"""
    return HTMLResponse(Template(master_html).render(clients=clients, message=message))

@app.post("/master/client/new")
async def master_create_client(
    request: Request,
    client_name: str = Form(...),
    slug: str = Form(...),
    bot_token: str = Form(""),
    admin_password: str = Form(...),
):
    auth_data = verify_token(request.cookies.get("master_auth"))
    if auth_data != "master_admin":
        return RedirectResponse(url="/master/login", status_code=status.HTTP_303_SEE_OTHER)

    clean_slug = slug.strip().lower().replace(" ", "-")
    today = datetime.now().date()
    expiry = (today + timedelta(days=30)).strftime("%Y-%m-%d")

    async with MASTER_DB_LOCK:
        db = await get_master_db()
        try:
            await db.execute("""
                INSERT INTO clients (client_name, slug, make_date, expiry_date, plan_days, price, admin_username, admin_password, bot_token, bot_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (client_name, clean_slug, today.strftime("%Y-%m-%d"), expiry, 30, 199.0, "admin", admin_password.strip(), bot_token.strip(), "pompom_v2"))
            await db.commit()
        except Exception:
            raise HTTPException(status_code=400, detail="Slug already in use.")
        finally:
            await db.close()

    await init_tenant_db(clean_slug, bot_token.strip())
    if bot_token.strip():
        await bot_engine.start_tenant_bot(clean_slug, bot_token.strip())

    return RedirectResponse(url="/master/clients?message=Client+provisioned+successfully", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/master/client/delete")
async def master_delete_client(request: Request, client_id: int = Form(...)):
    auth_data = verify_token(request.cookies.get("master_auth"))
    if auth_data != "master_admin":
        return RedirectResponse(url="/master/login", status_code=status.HTTP_303_SEE_OTHER)
    db = await get_master_db()
    try:
        async with db.execute("SELECT slug FROM clients WHERE id = ?", (client_id,)) as cur:
            row = await cur.fetchone()
            if row:
                slug = row[0]
                await bot_engine.stop_tenant_bot(slug)
                await db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
                await db.commit()
                db_path = get_tenant_db_path(slug)
                if os.path.exists(db_path):
                    os.remove(db_path)
    finally:
        await db.close()
    return RedirectResponse(url="/master/clients?message=Client+deleted", status_code=status.HTTP_303_SEE_OTHER)

# ==========================================
# TENANT CLIENT ADMIN WORKFLOWS
# ==========================================
async def get_tenant_meta(slug: str):
    db = await get_master_db()
    try:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clients WHERE slug = ?", (slug,)) as cur:
            return await cur.fetchone()
    finally:
        await db.close()

@app.get("/c/{slug}/login", response_class=HTMLResponse)
async def tenant_login_page(slug: str, error: str | None = None):
    client = await get_tenant_meta(slug)
    if not client:
        raise HTTPException(status_code=404, detail="Store panel not found.")
    return HTMLResponse(MASTER_LOGIN_PAGE.replace("MASTER CONSOLE", client["client_name"]).replace('action="/master/login"', f'action="/c/{slug}/login"'))

@app.post("/c/{slug}/login")
async def tenant_login_post(slug: str, request: Request, username: str = Form(...), password: str = Form(...)):
    client = await get_tenant_meta(slug)
    if client and username == client["admin_username"] and password == client["admin_password"]:
        token = sign_token(f"tenant_{slug}")
        response = RedirectResponse(url=f"/c/{slug}/admin", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key=f"tenant_auth_{slug}", value=token, httponly=True, max_age=86400 * 7)
        return response
    return RedirectResponse(url=f"/c/{slug}/login?error=Invalid+credentials", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/c/{slug}/logout")
async def tenant_logout(slug: str):
    response = RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=f"tenant_auth_{slug}")
    return response

@app.get("/c/{slug}/admin", response_class=HTMLResponse)
async def tenant_admin_panel(
    slug: str,
    request: Request,
    message: str | None = None,
    page: int = 1,
    user_search: str = "",
):
    auth_data = verify_token(request.cookies.get(f"tenant_auth_{slug}"))
    if auth_data != f"tenant_{slug}":
        return RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)

    client = await get_tenant_meta(slug)
    if not client:
        raise HTTPException(status_code=404, detail="Store not found.")

    try:
        metrics = await get_tenant_dashboard_metrics(slug)
        limit = 50
        page = max(1, page)
        offset = (page - 1) * limit
        users_list, users_total = await get_tenant_paginated_users(slug, limit=limit, offset=offset, search=user_search)
        total_pages = max(1, math.ceil(users_total / limit))

        plans = await get_tenant_all_plans(slug)

        pack_cards_html = ""
        for idx, (pid, p_name, p_price, p_validity, p_link) in enumerate(plans):
            pack_cards_html += f"""
            <div class="glass-card rounded-2xl p-4 flex flex-col justify-between space-y-3 border border-purple-900/40">
                <div class="flex items-center justify-between border-b border-purple-900/30 pb-2">
                    <span class="text-xs font-tech text-cyan-400 font-bold">🟢 PACK #{idx + 1}</span>
                    <form method="POST" action="/c/{slug}/admin/plans/delete" onsubmit="return confirm('Delete plan?');" style="display:inline;">
                        <input type="hidden" name="plan_id" value="{pid}">
                        <button type="submit" class="text-[10px] font-mono text-rose-400 hover:text-rose-300 bg-rose-500/10 border border-rose-500/30 px-2 py-0.5 rounded transition">🗑️ Delete</button>
                    </form>
                </div>
                <form method="POST" action="/c/{slug}/admin/plans/update" class="space-y-2 text-xs">
                    <input type="hidden" name="plan_id" value="{pid}">
                    <div>
                        <label class="block text-[10px] font-mono text-purple-300 uppercase">Pack Name</label>
                        <input type="text" name="name" value="{p_name}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white">
                    </div>
                    <div class="grid grid-cols-2 gap-2">
                        <div>
                            <label class="block text-[10px] font-mono text-purple-300 uppercase">Price (₹)</label>
                            <input type="number" step="any" name="amount" value="{p_price}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white">
                        </div>
                        <div>
                            <label class="block text-[10px] font-mono text-purple-300 uppercase">Validity</label>
                            <input type="text" name="validity" value="{p_validity}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white">
                        </div>
                    </div>
                    <div>
                        <label class="block text-[10px] font-mono text-cyan-300 uppercase font-semibold">🔗 Access/Delivery Link</label>
                        <input type="url" name="access_link" value="{p_link}" placeholder="https://t.me/+..." class="w-full bg-[#070410] border border-cyan-500/40 rounded-lg px-2.5 py-1.5 text-cyan-300 text-xs">
                    </div>
                    <button type="submit" class="w-full bg-purple-900/60 hover:bg-cyan-600 text-white py-1.5 rounded-lg text-xs font-tech transition">Update Pack</button>
                </form>
            </div>"""

        recent_rows = "".join([f"<tr class='hover:bg-purple-900/20 transition'><td class='py-3 px-3 text-cyan-400'>FT{o[0]}ORD</td><td class='py-3 px-3'>{o[1]}<span class='block text-[10px] text-purple-400'>@{o[2]}</span></td><td class='py-3 px-3 text-white font-sans'>{o[3]}</td><td class='py-3 px-3 font-semibold text-white'>₹{o[4]:.2f}</td><td class='py-3 px-3 text-right'><span class='px-2.5 py-1 rounded-md text-[10px] font-tech bg-emerald-500/10 border border-emerald-500/40 text-emerald-400'>{o[5]}</span></td></tr>" for o in metrics.get("recent_orders", [])]) or '<tr><td colspan="5" class="py-8 text-center text-purple-400 text-xs">No orders recorded yet.</td></tr>'
        all_rows = "".join([f"<tr class='hover:bg-purple-900/20 transition'><td class='py-3 px-3 text-cyan-400'>FT{o[0]}ORD</td><td class='py-3 px-3'>{o[1]}<span class='block text-[10px] text-purple-400'>@{o[2]}</span></td><td class='py-3 px-3 text-white font-sans'>{o[3]}</td><td class='py-3 px-3 font-semibold text-white'>₹{o[4]:.2f}</td><td class='py-3 px-3 text-purple-300/80 text-[11px]'>{o[6]}</td><td class='py-3 px-3'>{o[5]}</td><td class='py-3 px-3 text-right'><a href='/c/{slug}/admin/orders/status?order_id={o[0]}&action=approved' class='bg-emerald-500/10 text-emerald-400 px-2 py-1 rounded text-[10px] mr-1'>Accept</a><a href='/c/{slug}/admin/orders/status?order_id={o[0]}&action=rejected' class='bg-rose-500/10 text-rose-400 px-2 py-1 rounded text-[10px]'>Reject</a></td></tr>" for o in metrics.get("all_orders", [])]) or '<tr><td colspan="7" class="py-12 text-center text-purple-400 text-xs">No orders recorded.</td></tr>'
        users_rows = "".join([f"<tr class='hover:bg-purple-900/20 transition'><td class='py-3 px-3 text-cyan-400 font-mono'>{u[0]}</td><td class='py-3 px-3'>@{u[2]}</td><td class='py-3 px-3 text-purple-300/80 text-[11px]'>{u[3]}</td><td class='py-3 px-3'><b>{u[4]}</b></td><td class='py-3 px-3 text-right'>{'Banned' if u[5] else 'Active'}</td></tr>" for u in users_list]) or '<tr><td colspan="5" class="py-12 text-center text-purple-400 text-xs">No users registered yet.</td></tr>'

        is_online = slug in bot_engine.active_bots
        status_pulse = "bg-cyan-400" if is_online else "bg-rose-500"
        status_text = "ONLINE" if is_online else "OFFLINE"

        dashboard_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Nagato Panel &mdash; {client['client_name']}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700&display=swap" rel="stylesheet">
    <style>
        body {{ font-family: sans-serif; background-color: #06040c; color: #fff; }}
        .font-tech {{ font-family: 'Orbitron', monospace; }}
        .glass-card {{ background: linear-gradient(135deg, rgba(22, 12, 42, 0.85) 0%, rgba(13, 8, 25, 0.92) 100%); border: 1px solid rgba(139, 92, 246, 0.25); }}
    </style>
</head>
<body class="p-6 max-w-6xl mx-auto space-y-6">
    <header class="flex justify-between items-center border-b border-purple-900/40 pb-4">
        <div>
            <h1 class="text-xl font-bold font-tech text-transparent bg-clip-text bg-gradient-to-r from-purple-300 to-cyan-300">{client['client_name']}</h1>
            <p class="text-xs text-purple-400 font-mono">Store Administrator: {client['admin_username']}</p>
        </div>
        <div class="flex items-center gap-3">
            <span class="flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#120b22] border border-cyan-500/40 text-xs font-mono text-cyan-300">
                <span class="w-2 h-2 rounded-full {status_pulse} animate-pulse inline-block"></span>
                {status_text}
            </span>
            <a href="/c/{slug}/logout" class="text-rose-400 text-xs font-mono underline">Sign Out</a>
        </div>
    </header>

    {'<div class="p-3 bg-cyan-500/20 text-cyan-300 text-xs rounded font-mono">' + message + '</div>' if message else ''}

    <div class="grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs font-mono">
        <div class="glass-card p-4 rounded-xl">Total Users: <b class="text-cyan-400">{metrics.get('total_users', 0)}</b></div>
        <div class="glass-card p-4 rounded-xl">Paid Orders: <b class="text-emerald-400">{metrics.get('paid_orders', 0)}</b></div>
        <div class="glass-card p-4 rounded-xl">Revenue: <b class="text-fuchsia-400">₹{metrics.get('revenue', '0.00')}</b></div>
    </div>

    <!-- 17 Multi-Packs Section -->
    <div class="glass-card p-6 rounded-2xl space-y-4">
        <div class="flex justify-between items-center border-b border-purple-900/40 pb-3">
            <h3 class="font-tech text-sm text-cyan-300 font-bold uppercase">Multi-Packs Inventory ({len(plans)})</h3>
            <form method="POST" action="/c/{slug}/admin/plans/add" class="flex gap-2">
                <input type="text" name="name" required placeholder="New Pack Name" class="bg-[#070410] border border-purple-900 rounded px-2 py-1 text-xs text-white">
                <input type="number" step="any" name="amount" required placeholder="Price" class="bg-[#070410] border border-purple-900 rounded px-2 py-1 text-xs text-white w-20">
                <button type="submit" class="bg-cyan-600 font-tech px-3 py-1 rounded text-xs uppercase font-bold text-white">+ Add Pack</button>
            </form>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {pack_cards_html}
        </div>
    </div>

    <!-- Bot Configuration -->
    <div class="glass-card p-6 rounded-2xl space-y-4 text-xs font-mono">
        <h3 class="font-tech text-sm text-fuchsia-300 font-bold uppercase">Telegram Bot Token &amp; UPI</h3>
        <form method="POST" action="/c/{slug}/admin/save" class="space-y-3">
            <div>
                <label class="block text-purple-300 mb-1">Bot Token</label>
                <input type="text" name="token" value="{await get_tenant_setting(slug, 'bot_token')}" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white">
            </div>
            <button type="submit" class="bg-cyan-600 px-4 py-2 rounded text-white font-tech font-bold uppercase">Restart Bot</button>
        </form>

        <form method="POST" action="/c/{slug}/admin/settings/upi" class="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-3 border-t border-purple-900/40">
            <div>
                <label class="block text-purple-300 mb-1">UPI ID</label>
                <input type="text" name="upi_id" value="{await get_tenant_setting(slug, 'upi_id')}" required class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white">
            </div>
            <div>
                <label class="block text-purple-300 mb-1">Payee Name</label>
                <input type="text" name="payee_name" value="{await get_tenant_setting(slug, 'payee_name')}" required class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white">
            </div>
            <div>
                <label class="block text-purple-300 mb-1">Admin Chat ID</label>
                <input type="text" name="admin_chat_id" value="{await get_tenant_setting(slug, 'admin_chat_id')}" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white">
            </div>
            <button type="submit" class="sm:col-span-3 bg-fuchsia-600 px-4 py-2 rounded text-white font-tech font-bold uppercase">Save UPI &amp; Admin</button>
        </form>
    </div>

    <!-- Orders Table -->
    <div class="glass-card p-6 rounded-2xl space-y-4">
        <h3 class="font-tech text-sm text-white font-bold uppercase">Customer Orders</h3>
        <table class="w-full text-left text-xs font-mono">
            <thead class="text-purple-400 border-b border-purple-900/40">
                <tr><th class="p-2">Order</th><th class="p-2">User</th><th class="p-2">Pack</th><th class="p-2">Amount</th><th class="p-2">Date</th><th class="p-2">Status</th><th class="p-2 text-right">Action</th></tr>
            </thead>
            <tbody>{all_rows}</tbody>
        </table>
    </div>
</body>
</html>"""
        return HTMLResponse(dashboard_html)
    except Exception as err:
        logger.error(f"[{slug}] Dashboard render error: {err}")
        return HTMLResponse(f"<h3>Dashboard Error: {err}</h3>", status_code=500)

@app.post("/c/{slug}/admin/save")
async def tenant_save_general_settings(
    slug: str,
    request: Request,
    bot_token: str = Form(""),
    token: str = Form(""),
):
    auth_data = verify_token(request.cookies.get(f"tenant_auth_{slug}"))
    if auth_data != f"tenant_{slug}":
        return RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)

    cleaned_token = (bot_token or token).strip()
    if cleaned_token:
        await update_tenant_setting(slug, "bot_token", cleaned_token)
        async with MASTER_DB_LOCK:
            db = await get_master_db()
            try:
                await db.execute("UPDATE clients SET bot_token = ? WHERE slug = ?", (cleaned_token, slug))
                await db.commit()
            finally:
                await db.close()
        await bot_engine.start_tenant_bot(slug, cleaned_token)

    return RedirectResponse(url=f"/c/{slug}/admin?message=Configuration+saved", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/settings/upi")
async def tenant_save_upi(slug: str, request: Request, upi_id: str = Form(...), payee_name: str = Form(...), admin_chat_id: str = Form("")):
    auth_data = verify_token(request.cookies.get(f"tenant_auth_{slug}"))
    if auth_data != f"tenant_{slug}":
        return RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)
    await update_tenant_setting(slug, "upi_id", upi_id.strip())
    await update_tenant_setting(slug, "payee_name", payee_name.strip())
    await update_tenant_setting(slug, "admin_chat_id", admin_chat_id.strip())
    return RedirectResponse(url=f"/c/{slug}/admin?message=UPI+saved", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/plans/add")
async def tenant_add_plan_endpoint(slug: str, request: Request, name: str = Form(...), amount: float = Form(...), validity: str = Form("Lifetime"), access_link: str = Form("")):
    auth_data = verify_token(request.cookies.get(f"tenant_auth_{slug}"))
    if auth_data != f"tenant_{slug}":
        return RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)
    plan_id = f"plan_{int(datetime.now().timestamp())}"
    await add_tenant_new_plan(slug, plan_id, name.strip(), amount, validity.strip(), access_link.strip())
    return RedirectResponse(url=f"/c/{slug}/admin?message=Plan+added", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/plans/update")
async def tenant_update_plan_endpoint(slug: str, request: Request, plan_id: str = Form(...), name: str = Form(...), amount: float = Form(...), validity: str = Form(...), access_link: str = Form("")):
    auth_data = verify_token(request.cookies.get(f"tenant_auth_{slug}"))
    if auth_data != f"tenant_{slug}":
        return RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)
    await update_tenant_plan(slug, plan_id.strip(), name.strip(), amount, validity.strip(), access_link.strip())
    return RedirectResponse(url=f"/c/{slug}/admin?message=Plan+updated", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/plans/delete")
async def tenant_delete_plan_endpoint(slug: str, request: Request, plan_id: str = Form(...)):
    auth_data = verify_token(request.cookies.get(f"tenant_auth_{slug}"))
    if auth_data != f"tenant_{slug}":
        return RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)
    await delete_tenant_plan(slug, plan_id.strip())
    return RedirectResponse(url=f"/c/{slug}/admin?message=Plan+deleted", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/c/{slug}/admin/orders/status")
async def tenant_order_status_get(slug: str, request: Request, order_id: int, action: str):
    auth_data = verify_token(request.cookies.get(f"tenant_auth_{slug}"))
    if auth_data != f"tenant_{slug}":
        return RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            async with db.execute("SELECT user_id, plan_name FROM payments WHERE id = ?", (int(order_id),)) as cur:
                row = await cur.fetchone()
            if row:
                user_id, plan_name = row
                if action == "approved":
                    await db.execute("UPDATE payments SET status = 'approved' WHERE id = ?", (int(order_id),))
                    await db.execute("UPDATE users SET premium_status = ? WHERE user_id = ?", (plan_name, user_id))
                    await db.commit()
                    await bot_engine.notify_payment_approved(slug, user_id, plan_name)
                else:
                    await db.execute("UPDATE payments SET status = 'rejected' WHERE id = ?", (int(order_id),))
                    await db.commit()
        finally:
            await db.close()
    return RedirectResponse(url=f"/c/{slug}/admin?message=Order+status+updated", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/")
async def root():
    return RedirectResponse(url="/master/login", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/health")
async def health():
    return {"status": "ok", "active_bots": len(bot_engine.active_bots)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("pommmaster:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
