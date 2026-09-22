# -*- coding: utf-8 -*-
import asyncio
import io
import json
import logging
import math
import os
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
from starlette.middleware.sessions import SessionMiddleware

# ==========================================
# MASTER CONFIGURATION & STORAGE PATHS
# ==========================================
logging.basicConfig(level=logging.INFO)

MASTER_USER = os.getenv("MASTER_USER", "admin")
MASTER_PASS = os.getenv("MASTER_PASS", "admin@123")
SESSION_SECRET = os.getenv("SESSION_SECRET", "super_master_session_key_9988_xyz")

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
                    bot_type TEXT DEFAULT 'pompom_v1',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            async with db.execute("PRAGMA table_info(clients);") as cur:
                columns = [col[1] for col in await cur.fetchall()]
                if "bot_type" not in columns:
                    await db.execute("ALTER TABLE clients ADD COLUMN bot_type TEXT DEFAULT 'pompom_v1';")
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

async def init_tenant_db(slug: str, initial_token: str = "", bot_type: str = "pompom_v1"):
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
            await db.execute("CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);")

            defaults = {
                "bot_token": initial_token,
                "admin_chat_id": "",
                "maintenance": "off",
                "upi_id": "YOUR_UPI",
                "payee_name": "YOUR_UPI_NAME",
                "welcome_photo": "https://kommodo.ai/i/vMd2KH7PZC8bgMH9mGWm",
                "welcome_text": (
                    "🎉 Welcome to VIP Access Bot!\n\n✨ Get exclusive access to premium content\n💰 Affordable plans starting at just ₹99\n✨ Content quality aisi ki dekhi nahi hogi \n✨ Only Premium Content\n✨ Daily New Uploads\n✨ Cp, Rp, Indian, Foreign, Dark everything\n✨ 10000+ Cp videos\n✨  25000+ Rp videos \n✨  M0m S0n 5k Videos\n\n✨ TRY OUR ANY PLAN FOR CHECKING THE QUALITY ✨"
                ),
                "plans_text": (
                    "💎 ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴs\n\n━━━━━━━━━━━━━━━━━\n👇 sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘʟᴀɴ ʙᴇʟᴏᴡ"
                ),
                "demo_video": "https://www.image2url.com/r2/default/videos/1788689350793-635df470-449c-4fd5-8ed3-96f503e8b88b.mp4",
                "v2_buttons": "[]",
                "v2_details": "{}",
                "v2_media": "{}"
            }
            for k, v in defaults.items():
                await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

            if bot_type == "pompom_v2":
                async with db.execute("SELECT value FROM settings WHERE key='v2_buttons'") as cur:
                    row = await cur.fetchone()
                    if row and row[0] == "[]":
                        v2_btns = [
                            "CHILD POM 🙈💋", "GAYY MIX", "CHILD INDIA 😄", "SMALL SON BIG GIRL 🤩",
                            "BRO SIS AUNTY CHILD", "ANIMAL FUN😂", "IP CAM FAMILY 🤤", "HOUSEWIFE 😱",
                            "MOM AND PAPA 😜 UNCLE AUNTY 😍", "PILLS", "SPA LEAK 🙈", "SNAP LEAK 😲",
                            "INSTA LEAK 😜💋", "CP 35K 😋🤩", "350 ZIPS CP", "SABSE SASTA", "300 ADULT GROUPS"
                        ]
                        prices = [62, 60, 64, 61, 63, 59, 58, 49, 50, 65, 66, 67, 69, 150, 70, 25, 150]
                        details = {}
                        for i, btn in enumerate(v2_btns):
                            details[str(i)] = {"pack": btn, "price": str(prices[i]), "link": ""}
                        await db.execute("UPDATE settings SET value=? WHERE key='v2_buttons'", (json.dumps(v2_btns),))
                        await db.execute("UPDATE settings SET value=? WHERE key='v2_details'", (json.dumps(details),))
            else:
                default_plans = [
                    ("plan_1", "😚ᴀᴅɪᴛʏ ᴍɪsʀʏ ᴀʟʟ 🥵", 99.0, "30 Days", ""),
                    ("plan_2", "🌽ᴄʜ!ᴅ ᴄᴏʀɴ 🌽", 49.0, "30 Days", ""),
                    ("plan_3", "💦ɪɴsᴛᴀɢʀᴀᴍ ʟᴇᴀᴋᴇᴅ 🍑", 59.0, "30 Days", ""),
                    ("plan_4", "💋ʙʀᴏᴛʜᴇʀ ᴀɴᴅ sɪsᴛᴇʀ 💦", 69.0, "30 Days", ""),
                ]
                for p in default_plans:
                    await db.execute(
                        "INSERT OR IGNORE INTO plans (plan_id, name, amount, validity, access_link) VALUES (?, ?, ?, ?, ?)",
                        p,
                    )
            await db.commit()
        finally:
            await db.close()

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

async def get_tenant_demo_video_list(slug: str) -> list[str]:
    raw = await get_tenant_setting(slug, "demo_video")
    if not raw:
        return []
    return [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]

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

async def get_tenant_user_payment_stats(slug: str, user_id: int):
    db = await get_tenant_db(slug)
    try:
        async with db.execute("""
            SELECT 
                COUNT(CASE WHEN status = 'approved' THEN 1 END),
                COUNT(CASE WHEN status = 'pending' THEN 1 END),
                COUNT(*)
            FROM payments WHERE user_id = ?
        """, (user_id,)) as cur:
            row = await cur.fetchone()
            return {"approved": row[0] or 0, "pending": row[1] or 0, "total": row[2] or 0}
    finally:
        await db.close()

# ==========================================
# UPI QR GENERATION & NPCI DETECTION
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

async def detect_payee_name_from_upi(upi_id: str) -> str:
    upi_id = upi_id.strip()
    if "@" not in upi_id:
        return ""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}
    try:
        url = f"https://upier.vercel.app/api/check?vpa={urllib.parse.quote(upi_id)}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5), headers=headers) as s:
            async with s.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    name = data.get("name") or data.get("payeeAccountName")
                    if name and name.strip():
                        return name.strip()
    except Exception:
        pass
    return ""

# ==========================================
# MULTI-TENANT BOT ENGINE & FACTORY
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
        self.admin_upload_states: dict[str, dict[int, int]] = defaultdict(dict)

    def track(self, slug: str, chat_id: int, message_id: int):
        if message_id not in self.user_messages[slug][chat_id]:
            self.user_messages[slug][chat_id].append(message_id)

    async def delete_old_messages(self, slug: str, chat_id: int, exclude_ids: list[int] | None = None):
        bot = self.active_bots.get(slug)
        if not bot:
            return
        exclude = set(exclude_ids or [])
        all_ids = [mid for mid in self.user_messages[slug].get(chat_id, []) if mid not in exclude]
        self.user_messages[slug][chat_id] = [mid for mid in self.user_messages[slug].get(chat_id, []) if mid in exclude]
        if not all_ids:
            return

        for i in range(0, len(all_ids), 100):
            chunk = all_ids[i : i + 100]
            try:
                await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
            except TelegramBadRequest:
                for mid in chunk:
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=mid)
                    except Exception:
                        pass
            except Exception:
                pass

    async def get_access_link(self, slug: str, bot_type: str, plan_name: str) -> str:
        if bot_type == "pompom_v2":
            details_str = await get_tenant_setting(slug, "v2_details")
            details = json.loads(details_str) if details_str else {}
            for v in details.values():
                if v.get("pack") == plan_name:
                    return v.get("link", "")
        else:
            db = await get_tenant_db(slug)
            try:
                async with db.execute("SELECT access_link FROM plans WHERE name = ?", (plan_name,)) as cur:
                    row = await cur.fetchone()
                    return row[0] if row else ""
            finally:
                await db.close()
        return ""

    async def notify_payment_approved(self, slug: str, user_id: int, plan_name: str, bot_type: str):
        bot = self.active_bots.get(slug)
        if not bot:
            return
        access_link = await self.get_access_link(slug, bot_type, plan_name)

        builder = InlineKeyboardBuilder()
        if access_link and access_link.strip().startswith(("http://", "https://", "t.me/")):
            link_url = access_link.strip()
            if link_url.startswith("t.me/"):
                link_url = "https://" + link_url
            builder.button(text="🔗 Join VIP Channel / Access Link", url=link_url)
        builder.button(text="🏠 Home", callback_data="btn_home")
        builder.adjust(1)

        caption = f"🎉 <b>Payment Approved!</b>\n\nYour subscription for <b>{plan_name}</b> is now active!\n"
        if access_link:
            caption += "\n👉 Click the button below to claim your access:"

        try:
            await bot.send_message(chat_id=user_id, text=caption, reply_markup=builder.as_markup())
        except Exception as e:
            logging.warning(f"[{slug}] Could not deliver approval message to {user_id}: {e}")

    async def start_tenant_bot(self, slug: str, token: str):
        await self.stop_tenant_bot(slug)
        token = token.strip()
        if not token or token == "YOUR_BOT_TOKEN_HERE" or ":" not in token:
            return

        session = AiohttpSession(timeout=20.0)
        bot = Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=MemoryStorage())

        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.session.close()
            session = AiohttpSession(timeout=20.0)
            bot.session = session
        except Exception as e:
            logging.warning(f"[{slug}] Initial reset warning: {e}")

        db = await get_master_db()
        try:
            async with db.execute("SELECT bot_type FROM clients WHERE slug = ?", (slug,)) as cur:
                row = await cur.fetchone()
                b_type = row[0] if row else "pompom_v1"
        finally:
            await db.close()

        if b_type == "pompom_v2":
            self._attach_pompom_v2_handlers(dp, slug)
        else:
            self._attach_pompom_v1_handlers(dp, slug)

        async def runner():
            while True:
                try:
                    logging.info(f"[{slug}] Polling loop started for [{b_type}]...")
                    await dp.start_polling(bot, drop_pending_updates=True, allowed_updates=["message", "callback_query"])
                    break
                except TelegramConflictError:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logging.error(f"[{slug}] Polling error: {e}. Retrying in 4s...")
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
            dp = self.active_dispatchers.pop(slug)
            try:
                await dp.stop_polling()
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

    # ==========================================
    # V1 HANDLERS (CLASSIC PLANS)
    # ==========================================
    def _attach_pompom_v1_handlers(self, dp: Dispatcher, slug: str):
        engine = self

        class TenantTrackerMiddleware(BaseMiddleware):
            async def __call__(self, handler, event: types.TelegramObject, data: dict):
                if isinstance(event, types.Message):
                    engine.track(slug, event.chat.id, event.message_id)
                return await handler(event, data)

        class TenantSecurityMiddleware(BaseMiddleware):
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
                        await event.answer("Bot is under maintenance. Please try again later.")
                    return
                return await handler(event, data)

        dp.message.outer_middleware(TenantTrackerMiddleware())
        dp.message.outer_middleware(TenantSecurityMiddleware())
        dp.callback_query.outer_middleware(TenantSecurityMiddleware())

        async def send_welcome_flow(chat_id: int):
            bot = engine.active_bots.get(slug)
            if not bot: return
            photo_url = await get_tenant_setting(slug, "welcome_photo")
            caption = await get_tenant_setting(slug, "welcome_text")

            builder = InlineKeyboardBuilder()
            builder.button(text="🎬 View Demo", callback_data="btn_view_demo:0")
            builder.button(text="⭐ My Premium", callback_data="btn_my_premium")
            builder.button(text="👤 My Profile", callback_data="btn_my_profile")
            builder.adjust(1)
            kb = builder.as_markup()

            sent_photo = False
            if photo_url and photo_url.startswith(("http://", "https://", "AgAC")):
                try:
                    async with asyncio.timeout(2.5):
                        m1 = await bot.send_photo(chat_id=chat_id, photo=photo_url, caption=caption, reply_markup=kb)
                        engine.track(slug, chat_id, m1.message_id)
                        sent_photo = True
                except Exception: pass
            if not sent_photo:
                m1 = await bot.send_message(chat_id=chat_id, text=caption, reply_markup=kb)
                engine.track(slug, chat_id, m1.message_id)

            plans_txt = await get_tenant_setting(slug, "plans_text")
            b_plans = InlineKeyboardBuilder()
            plans = await get_tenant_all_plans(slug)
            for pid, name, price, _, _ in plans:
                b_plans.button(text=f"🔥 {name} (Rs.{int(price)})", callback_data=f"buy_plan:{pid}")
            b_plans.button(text="🏠 Home", callback_data="btn_home")
            b_plans.adjust(*(1 for _ in range(len(plans) + 1)))
            m2 = await bot.send_message(chat_id=chat_id, text=plans_txt, reply_markup=b_plans.as_markup())
            engine.track(slug, chat_id, m2.message_id)

        @dp.message(Command("cancel"))
        async def cancel_handler(message: types.Message, state: FSMContext):
            await state.clear()
            await message.answer("❌ Current operation cancelled.")
            await send_welcome_flow(message.chat.id)

        @dp.message(CommandStart())
        async def handle_start(message: types.Message):
            await add_tenant_user(slug, message.from_user)
            await send_welcome_flow(message.chat.id)

        @dp.callback_query(F.data == "btn_home")
        async def nav_home(callback: types.CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer()
            await engine.delete_old_messages(slug, callback.message.chat.id)
            try: await callback.message.delete()
            except Exception: pass
            await send_welcome_flow(callback.message.chat.id)

        @dp.callback_query(F.data == "btn_get_premium")
        async def nav_plans(callback: types.CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer()
            try: await callback.message.delete()
            except Exception: pass
            bot = engine.active_bots.get(slug)
            if not bot: return
            text = await get_tenant_setting(slug, "plans_text")
            b_plans = InlineKeyboardBuilder()
            plans = await get_tenant_all_plans(slug)
            for pid, name, price, _, _ in plans:
                b_plans.button(text=f"🔥 {name} (Rs.{int(price)})", callback_data=f"buy_plan:{pid}")
            b_plans.button(text="🏠 Home", callback_data="btn_home")
            b_plans.adjust(*(1 for _ in range(len(plans) + 1)))
            msg = await bot.send_message(chat_id=callback.message.chat.id, text=text, reply_markup=b_plans.as_markup())
            engine.track(slug, callback.message.chat.id, msg.message_id)

        @dp.callback_query(F.data == "noop")
        async def handle_noop(callback: types.CallbackQuery):
            await callback.answer()

        @dp.callback_query(F.data.startswith("btn_view_demo"))
        async def nav_demo(callback: types.CallbackQuery):
            await callback.answer()
            bot = engine.active_bots.get(slug)
            if not bot: return
            chat_id = callback.message.chat.id
            try: await bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)
            except Exception: pass
            parts = callback.data.split(":")
            idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            videos = await get_tenant_demo_video_list(slug)
            
            def demo_kb(c_idx, t_vid):
                b = InlineKeyboardBuilder()
                nav_row = []
                if c_idx > 0: nav_row.append(types.InlineKeyboardButton(text="◀️ Previous", callback_data=f"btn_view_demo:{c_idx - 1}"))
                if t_vid > 1: nav_row.append(types.InlineKeyboardButton(text=f"[{c_idx + 1}/{t_vid}]", callback_data="noop"))
                if c_idx < t_vid - 1: nav_row.append(types.InlineKeyboardButton(text="Next ▶️", callback_data=f"btn_view_demo:{c_idx + 1}"))
                if nav_row: b.row(*nav_row)
                b.row(types.InlineKeyboardButton(text="💎 Get Premium", callback_data="btn_get_premium"), types.InlineKeyboardButton(text="🏠 Home", callback_data="btn_home"))
                return b.as_markup()

            if not videos:
                msg = await bot.send_message(chat_id=chat_id, text="📺 No demo videos available right now.", reply_markup=demo_kb(0, 0))
                engine.track(slug, chat_id, msg.message_id)
                return
            idx = max(0, min(idx, len(videos) - 1))
            try:
                async with asyncio.timeout(5.0):
                    msg = await bot.send_video(chat_id=chat_id, video=videos[idx], caption=f"📺 <b>Demo Video</b> ({idx + 1}/{len(videos)})", reply_markup=demo_kb(idx, len(videos)))
                    engine.track(slug, chat_id, msg.message_id)
            except Exception:
                msg = await bot.send_message(chat_id=chat_id, text=f"📺 <b>Demo Video</b> ({idx + 1}/{len(videos)})\n\n🔗 {videos[idx]}", reply_markup=demo_kb(idx, len(videos)))
                engine.track(slug, chat_id, msg.message_id)

        @dp.callback_query(F.data == "btn_my_premium")
        async def nav_my_premium(callback: types.CallbackQuery):
            await callback.answer()
            try: await callback.message.delete()
            except Exception: pass
            bot = engine.active_bots.get(slug)
            if not bot: return
            user = await get_tenant_user(slug, callback.from_user.id)
            plan = user[4] if user else "Free"
            builder = InlineKeyboardBuilder()
            if plan != "Free":
                p_info = await get_tenant_plan_by_name(slug, plan)
                if p_info and p_info[4]:
                    link = p_info[4].strip()
                    if link.startswith("t.me/"): link = "https://" + link
                    builder.button(text="🔗 Open Premium Channel / Link", url=link)
            builder.button(text="💎 Upgrade", callback_data="btn_get_premium")
            builder.button(text="🏠 Home", callback_data="btn_home")
            builder.adjust(1)
            msg = await bot.send_message(chat_id=callback.message.chat.id, text=f"⭐ My Premium Membership\n\nPlan: <b>{plan}</b>\nStatus: <b>{'Active' if plan != 'Free' else 'Free Tier'}</b>", reply_markup=builder.as_markup())
            engine.track(slug, callback.message.chat.id, msg.message_id)

        @dp.callback_query(F.data == "btn_my_profile")
        async def nav_my_profile(callback: types.CallbackQuery):
            await callback.answer()
            try: await callback.message.delete()
            except Exception: pass
            bot = engine.active_bots.get(slug)
            if not bot: return
            user = await get_tenant_user(slug, callback.from_user.id)
            if not user: return
            uid, full_name, username, joined_at, plan, _ = user
            payments = await get_tenant_user_payment_stats(slug, uid)
            text = f"👤 MY PROFILE\nName: {full_name}\nUsername: @{username}\nID: {uid}\nJoined: {joined_at}\nPlan: {plan}\nPayments: Approved: {payments['approved']} | Pending: {payments['pending']}"
            builder = InlineKeyboardBuilder()
            builder.button(text="💎 Get Premium", callback_data="btn_get_premium")
            builder.button(text="🏠 Home", callback_data="btn_home")
            builder.adjust(2)
            msg = await bot.send_message(chat_id=callback.message.chat.id, text=text, reply_markup=builder.as_markup())
            engine.track(slug, callback.message.chat.id, msg.message_id)

        @dp.callback_query(F.data.startswith("buy_plan:"))
        async def process_plan_selection(callback: types.CallbackQuery, state: FSMContext):
            await callback.answer()
            try: await callback.message.delete()
            except Exception: pass
            bot = engine.active_bots.get(slug)
            if not bot: return
            pid = callback.data.split(":")[1]
            plan = await get_tenant_plan(slug, pid)
            if not plan: return
            _, plan_name, amount, validity, _ = plan
            qr_buf = await generate_tenant_upi_qr(slug, plan_name, amount)
            photo_file = BufferedInputFile(qr_buf.getvalue(), filename="qr.png")
            upi_id = await get_tenant_setting(slug, "upi_id")
            payee = await get_tenant_setting(slug, "payee_name")
            caption = f"📲 <b>UPI PAYMENT</b>\n\n━━━━━━━━━━━━━━━━━\n📦 PLAN: {plan_name}\n💰 AMOUNT: ₹{amount:.2f}\n⏳ VALITY: {validity}\n━━━━━━━━━━━━━━━━━\n\n👤 NAME: {payee}\n📱 UPI ID: <code>{upi_id}</code>\n\n📋 STEPS:\n1️⃣ SCAN THE QR CODE ABOVE\n2️⃣ ₹{amount:.2f} AMOUNT AND NOTE WILL BE BILLED AUTOMATICALLY\n3️⃣ ENTER YOUR PIN AND COMPLETE PAYMENT\n4️⃣ TAKE A SCREENSHOT AND CLICK CHECK PAYMENT ✅"
            await state.update_data(current_plan=plan_name, current_amount=amount)
            b = InlineKeyboardBuilder()
            b.button(text="📥 CHECK PAYMENT", callback_data="check_payment")
            b.button(text="🔙 BACK TO PLANS", callback_data="btn_get_premium")
            b.adjust(1)
            msg = await bot.send_photo(chat_id=callback.message.chat.id, photo=photo_file, caption=caption, parse_mode="HTML", reply_markup=b.as_markup())
            engine.track(slug, callback.message.chat.id, msg.message_id)

        @dp.callback_query(F.data == "check_payment")
        async def handle_check_payment(callback: types.CallbackQuery, state: FSMContext):
            await callback.answer()
            try: await callback.message.delete()
            except Exception: pass
            bot = engine.active_bots.get(slug)
            if not bot: return
            await state.set_state(PaymentStates.waiting_for_screenshot)
            msg = await bot.send_message(chat_id=callback.message.chat.id, text="📸 SEND PAYMENT SCREENSHOT\n\n✅ SEND THE SCREENSHOT HERE AFTER COMPLITING UPI PAYMENT.\n\n⚠️ ONLINE AN IMAGE OR SCREENSHOT IS ACCEPTED\n\nType /cancel to abort.")
            engine.track(slug, callback.message.chat.id, msg.message_id)

        @dp.message(PaymentStates.waiting_for_screenshot, F.photo | (F.document & F.document.mime_type.startswith("image/")))
        async def process_payment_proof(message: types.Message, state: FSMContext):
            bot = engine.active_bots.get(slug)
            if not bot: return
            data = await state.get_data()
            plan_name = data.get("current_plan", "VIP Plan")
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
            b_home = InlineKeyboardBuilder()
            b_home.button(text="🎬 View Demo", callback_data="btn_view_demo:0")
            b_home.button(text="⭐ My Premium", callback_data="btn_my_premium")
            b_home.button(text="👤 My Profile", callback_data="btn_my_profile")
            b_home.adjust(1)
            msg = await message.answer("✅ Screenshot Received! Verification is in progress.", reply_markup=b_home.as_markup())
            engine.track(slug, message.chat.id, msg.message_id)

            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Approve", callback_data=f"adm_pay:{pid}:approved")
            builder.button(text="❌ Reject", callback_data=f"adm_pay:{pid}:rejected")
            builder.adjust(2)
            caption = f"🔔 <b>New Payment Screenshot Received</b>\n\n<b>Order ID:</b> #{pid}\n<b>User ID:</b> <code>{message.from_user.id}</code>\n<b>Username:</b> @{message.from_user.username or 'N/A'}\n<b>Plan:</b> {plan_name}\n<b>Amount:</b> Rs.{amount}"
            file_id = message.photo[-1].file_id if message.photo else message.document.file_id
            for admin_id in await get_tenant_admin_ids(slug):
                try: await bot.send_photo(chat_id=admin_id, photo=file_id, caption=caption, reply_markup=builder.as_markup())
                except Exception as e:
                    try: await bot.send_message(chat_id=admin_id, text=f"{caption}\n\n⚠️ (Could not render photo)", reply_markup=builder.as_markup())
                    except Exception: pass

        @dp.message(PaymentStates.waiting_for_screenshot)
        async def invalid_proof(message: types.Message, state: FSMContext):
            if message.text and message.text.strip().lower() in ["/cancel", "cancel"]:
                await state.clear()
                await message.answer("❌ Payment verification cancelled.")
                await send_welcome_flow(message.chat.id)
                return
            msg = await message.answer("⚠️ ONLINE AN IMAGE OR SCREENSHOT IS ACCEPTED\n\nPlease upload your payment screenshot image.\nType /cancel to abort.")
            engine.track(slug, message.chat.id, message.message_id)

        @dp.callback_query(F.data.startswith("adm_pay:"))
        async def handle_admin_pay_approval(callback: types.CallbackQuery):
            bot = engine.active_bots.get(slug)
            if not bot: return
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
                            await engine.notify_payment_approved(slug, t_uid, pl, "pompom_v1")
                            await callback.message.edit_caption(caption=callback.message.caption + "\n\nSTATUS: APPROVED")
                        else:
                            await db.execute("UPDATE payments SET status='rejected' WHERE id=?", (int(pid),))
                            await db.commit()
                            try: await bot.send_message(t_uid, "Payment verification failed.")
                            except Exception: pass
                            await callback.message.edit_caption(caption=callback.message.caption + "\n\nSTATUS: REJECTED")
                    finally:
                        await db.close()
                await callback.answer("Status updated.")

    # ==========================================
    # V2 HANDLERS (17 PACKS + BULK MEDIA UPLOAD)
    # ==========================================
    def _attach_pompom_v2_handlers(self, dp: Dispatcher, slug: str):
        engine = self

        class TenantTrackerMiddleware(BaseMiddleware):
            async def __call__(self, handler, event: types.TelegramObject, data: dict):
                if isinstance(event, types.Message):
                    engine.track(slug, event.chat.id, event.message_id)
                return await handler(event, data)

        class TenantSecurityMiddleware(BaseMiddleware):
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
                        await event.answer("Bot is under maintenance. Please try again later.")
                    return
                return await handler(event, data)

        dp.message.outer_middleware(TenantTrackerMiddleware())
        dp.message.outer_middleware(TenantSecurityMiddleware())
        dp.callback_query.outer_middleware(TenantSecurityMiddleware())

        async def send_welcome_v2(chat_id: int):
            bot = engine.active_bots.get(slug)
            if not bot: return
            photo_url = await get_tenant_setting(slug, "welcome_photo")
            caption = await get_tenant_setting(slug, "welcome_text")
            btns_str = await get_tenant_setting(slug, "v2_buttons")
            details_str = await get_tenant_setting(slug, "v2_details")
            btns = json.loads(btns_str) if btns_str else []
            details = json.loads(details_str) if details_str else {}

            b = InlineKeyboardBuilder()
            for i, name in enumerate(btns):
                price = details.get(str(i), {}).get("price", "299")
                b.button(text=f"🟢 {name} - ₹{price}", callback_data=f"v2_cat_{i}")
            b.adjust(1)
            kb = b.as_markup()

            sent_photo = False
            if photo_url and photo_url.startswith(("http://", "https://", "AgAC")):
                try:
                    async with asyncio.timeout(2.5):
                        m1 = await bot.send_photo(chat_id=chat_id, photo=photo_url, caption=caption, reply_markup=kb)
                        engine.track(slug, chat_id, m1.message_id)
                        sent_photo = True
                except Exception: pass
            if not sent_photo:
                m1 = await bot.send_message(chat_id=chat_id, text=caption, reply_markup=kb)
                engine.track(slug, chat_id, m1.message_id)

        @dp.message(CommandStart())
        async def v2_start(message: types.Message):
            await add_tenant_user(slug, message.from_user)
            await send_welcome_v2(message.chat.id)

        @dp.message(Command("cancel"))
        async def v2_cancel(message: types.Message, state: FSMContext):
            await state.clear()
            engine.admin_upload_states[slug].pop(message.from_user.id, None)
            await message.answer("❌ Action cancelled.")
            await send_welcome_v2(message.chat.id)

        @dp.callback_query(F.data == "btn_home")
        async def v2_home(callback: types.CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer()
            await engine.delete_old_messages(slug, callback.message.chat.id)
            try: await callback.message.delete()
            except Exception: pass
            await send_welcome_v2(callback.message.chat.id)

        @dp.message(Command("upload"))
        async def v2_upload(message: types.Message):
            admin_ids = await get_tenant_admin_ids(slug)
            if message.from_user.id not in admin_ids: return
            args = message.text.split()
            if len(args) < 2 or not args[1].isdigit():
                await message.answer("Usage: /upload <button_number>")
                return
            idx = int(args[1]) - 1
            engine.admin_upload_states[slug][message.from_user.id] = idx
            await message.answer(f"Send media for Button {idx+1}. Type /done to finish.")

        @dp.message(Command("clear"))
        async def v2_clear(message: types.Message):
            admin_ids = await get_tenant_admin_ids(slug)
            if message.from_user.id not in admin_ids: return
            args = message.text.split()
            if len(args) < 2 or not args[1].isdigit(): return
            idx = int(args[1]) - 1
            media_str = await get_tenant_setting(slug, "v2_media")
            media = json.loads(media_str) if media_str else {}
            media[str(idx)] = []
            await update_tenant_setting(slug, "v2_media", json.dumps(media))
            await message.answer(f"Cleared media for Button {idx+1}")

        @dp.message(Command("done"))
        async def v2_done(message: types.Message):
            if message.from_user.id in engine.admin_upload_states[slug]:
                engine.admin_upload_states[slug].pop(message.from_user.id)
                await message.answer("✅ Upload session finished.")

        @dp.message(F.photo | F.video | F.document)
        async def v2_media_handler(message: types.Message, state: FSMContext):
            bot = engine.active_bots.get(slug)
            if not bot: return

            current_state = await state.get_state()
            if current_state == PaymentStates.waiting_for_screenshot.state:
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
                b = InlineKeyboardBuilder()
                b.button(text="🏠 Home", callback_data="btn_home")
                await message.answer("✅ Screenshot Received! Admin verification in progress.", reply_markup=b.as_markup())

                ab = InlineKeyboardBuilder()
                ab.button(text="✅ Accept", callback_data=f"adm_pay:{pid}:approved")
                ab.button(text="❌ Reject", callback_data=f"adm_pay:{pid}:rejected")
                ab.adjust(2)

                caption = f"🚨 <b>New Payment Proof</b>\n\n<b>Order:</b> #{pid}\n<b>User ID:</b> <code>{message.from_user.id}</code>\n<b>Username:</b> @{message.from_user.username or 'N/A'}\n<b>Pack:</b> {plan_name}\n<b>Amount:</b> ₹{amount}"
                file_id = message.photo[-1].file_id if message.photo else (message.video.file_id if message.video else message.document.file_id)
                admin_ids = await get_tenant_admin_ids(slug)
                for adm in admin_ids:
                    try: await bot.send_photo(chat_id=adm, photo=file_id, caption=caption, reply_markup=ab.as_markup())
                    except Exception: pass
                return

            admin_ids = await get_tenant_admin_ids(slug)
            if message.from_user.id in engine.admin_upload_states[slug] and message.from_user.id in admin_ids:
                idx = engine.admin_upload_states[slug][message.from_user.id]
                file_id = message.photo[-1].file_id if message.photo else (message.video.file_id if message.video else message.document.file_id)
                media_str = await get_tenant_setting(slug, "v2_media")
                media = json.loads(media_str) if media_str else {}
                if str(idx) not in media: media[str(idx)] = []
                media[str(idx)].append(file_id)
                await update_tenant_setting(slug, "v2_media", json.dumps(media))
                await message.answer(f"📥 Saved media to Button {idx+1}. Send next or /done.")

        @dp.callback_query(F.data.startswith("v2_cat_"))
        async def v2_category(callback: types.CallbackQuery, state: FSMContext):
            await callback.answer()
            try: await callback.message.delete()
            except Exception: pass
            idx = callback.data.split("_")[2]
            
            media_str = await get_tenant_setting(slug, "v2_media")
            media = json.loads(media_str) if media_str else {}
            vids = media.get(idx, [])
            
            bot = engine.active_bots.get(slug)
            if bot:
                for m in vids[:5]:
                    try: await bot.send_video(callback.message.chat.id, m)
                    except Exception:
                        try: await bot.send_photo(callback.message.chat.id, m)
                        except Exception: pass

            details_str = await get_tenant_setting(slug, "v2_details")
            details = json.loads(details_str) if details_str else {}
            d = details.get(idx, {})
            name = d.get("pack", f"Pack {int(idx)+1}")
            amt = float(d.get("price", "299"))

            qr_buf = await generate_tenant_upi_qr(slug, name, amt)
            upi = await get_tenant_setting(slug, "upi_id")
            payee = await get_tenant_setting(slug, "payee_name")
            
            await state.update_data(p_name=name, p_amt=amt)
            cap = f"📦 Pack: {name}\n💰 Amount: ₹{amt:.2f}\n📱 UPI: <code>{upi}</code> ({payee})\n\nScan & pay, then click Check Payment."
            b = InlineKeyboardBuilder()
            b.button(text="📥 Check Payment", callback_data="check_pay")
            b.button(text="🔙 Back", callback_data="btn_home")
            b.adjust(1)
            if bot:
                msg = await bot.send_photo(chat_id=callback.message.chat.id, photo=BufferedInputFile(qr_buf.getvalue(), "qr.png"), caption=cap, reply_markup=b.as_markup())
                engine.track(slug, callback.message.chat.id, msg.message_id)

        @dp.callback_query(F.data == "check_pay")
        async def v2_check(callback: types.CallbackQuery, state: FSMContext):
            await callback.answer()
            try: await callback.message.delete()
            except Exception: pass
            await state.set_state(PaymentStates.waiting_for_screenshot)
            bot = engine.active_bots.get(slug)
            if bot:
                msg = await bot.send_message(chat_id=callback.message.chat.id, text="📸 Please send your payment screenshot here.\nSend /cancel to abort.")
                engine.track(slug, callback.message.chat.id, msg.message_id)

        @dp.callback_query(F.data.startswith("adm_pay:"))
        async def v2_adm_pay(callback: types.CallbackQuery):
            bot = engine.active_bots.get(slug)
            if not bot: return
            admin_ids = await get_tenant_admin_ids(slug)
            if callback.from_user.id not in admin_ids: return
            _, pid, act = callback.data.split(":")
            async with TENANT_DB_LOCKS[slug]:
                db = await get_tenant_db(slug)
                try:
                    async with db.execute("SELECT user_id, plan_name FROM payments WHERE id=?", (int(pid),)) as cur:
                        p = await cur.fetchone()
                    if p:
                        if act == "approved":
                            await db.execute("UPDATE payments SET status='approved' WHERE id=?", (int(pid),))
                            await db.execute("UPDATE users SET premium_status=? WHERE user_id=?", (p[1], p[0]))
                            await db.commit()
                            await engine.notify_payment_approved(slug, p[0], p[1], "pompom_v2")
                            await callback.message.edit_caption(caption=callback.message.caption + "\n\nSTATUS: APPROVED")
                        else:
                            await db.execute("UPDATE payments SET status='rejected' WHERE id=?", (int(pid),))
                            await db.commit()
                            try: await bot.send_message(p[0], "Payment verification failed.")
                            except Exception: pass
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
        async with db.execute("SELECT slug, bot_token, expiry_date, status, bot_type FROM clients") as cur:
            clients = await cur.fetchall()
        today = datetime.now().date()
        for c in clients:
            exp_date = datetime.strptime(c["expiry_date"], "%Y-%m-%d").date()
            if c["status"] == "ACTIVE" and exp_date >= today:
                await init_tenant_db(c["slug"], c["bot_token"], c["bot_type"])
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
# NATIVE STRING TEMPLATES (NO DEPENDENCIES)
# ==========================================
def render_template(template_str: str, **kwargs) -> str:
    res = template_str
    for k, v in kwargs.items():
        res = res.replace(f"{{{{{k}}}}}", str(v))
    return res

MASTER_LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{{title}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700&display=swap" rel="stylesheet">
    <style>body { font-family: sans-serif; background-color: #03010a; } .font-tech { font-family: 'Orbitron', monospace; }</style>
</head>
<body class="flex items-center justify-center p-4 min-h-screen text-white">
    <div class="w-full max-w-[360px] bg-[#100c22] p-8 rounded-2xl border border-cyan-500/40 shadow-2xl">
        <h1 class="text-xl font-bold font-tech text-cyan-300 text-center uppercase">{{title}}</h1>
        {{error_html}}
        <form method="POST" action="{{action_url}}" class="space-y-4 mt-6 text-xs font-mono">
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
</body>
</html>"""

MASTER_DASHBOARD_TEMPLATE = """<!DOCTYPE html>
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
    {{message_html}}
    
    <div class="bg-[#120b22] p-6 rounded-2xl border border-purple-900/50 space-y-4">
        <h3 class="text-sm font-bold text-fuchsia-300">Provision New Client Panel</h3>
        <form method="POST" action="/master/client/new" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3 text-xs">
            <input type="text" name="client_name" required placeholder="Client Name" class="bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <input type="text" name="slug" required placeholder="URL Slug" class="bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <select name="bot_type" class="bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
                <option value="pompom_v2">Pom Pom V2 (17 Packs)</option>
                <option value="pompom_v1">Pom Pom V1 (Classic)</option>
            </select>
            <input type="text" name="admin_password" required placeholder="Admin Pass" class="bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <button type="submit" class="bg-gradient-to-r from-fuchsia-600 to-cyan-500 py-2.5 rounded font-bold uppercase text-white">Create</button>
        </form>
    </div>

    <div class="bg-[#120b22] p-6 rounded-2xl border border-purple-900/50 space-y-4">
        <h3 class="text-sm font-bold text-cyan-300">All Client Panels</h3>
        <table class="w-full text-left text-xs">
            <thead class="text-purple-400 border-b border-purple-900/40">
                <tr><th class="p-2">Name</th><th class="p-2">Engine</th><th class="p-2">Admin URL</th><th class="p-2 text-right">Action</th></tr>
            </thead>
            <tbody>
                {{clients_html}}
            </tbody>
        </table>
    </div>
</body>
</html>"""

TENANT_DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{{client_name}} &mdash; Admin</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: sans-serif; background-color: #06040c; color: #fff; }
        .font-tech { font-family: 'Orbitron', monospace; }
        .glass-card { background: linear-gradient(135deg, rgba(22, 12, 42, 0.85) 0%, rgba(13, 8, 25, 0.92) 100%); border: 1px solid rgba(139, 92, 246, 0.25); }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
    </style>
</head>
<body class="p-6 max-w-6xl mx-auto space-y-6">
    <header class="flex justify-between items-center border-b border-purple-900/40 pb-4">
        <div>
            <h1 class="text-xl font-bold font-tech text-transparent bg-clip-text bg-gradient-to-r from-purple-300 to-cyan-300">{{client_name}} Admin</h1>
            <p class="text-xs text-purple-400 font-mono">Engine: {{bot_type}}</p>
        </div>
        <div class="flex items-center gap-3">
            <span class="flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#120b22] border border-cyan-500/40 text-xs font-mono text-cyan-300">
                <span class="w-2 h-2 rounded-full {{status_pulse}} animate-pulse inline-block"></span>
                {{status_text}}
            </span>
            <a href="/c/{{slug}}/logout" class="text-rose-400 text-xs font-mono underline">Sign Out</a>
        </div>
    </header>

    {{message_html}}

    <nav class="flex gap-2 border-b border-purple-900/40 pb-4 text-xs font-tech font-bold text-slate-400 overflow-x-auto">
        <button onclick="switchTab('tab-dashboard')" class="nav-btn text-cyan-400 px-3 py-1 rounded">Metrics</button>
        <button onclick="switchTab('tab-orders')" class="nav-btn hover:text-cyan-400 px-3 py-1 rounded">Orders</button>
        <button onclick="switchTab('tab-users')" class="nav-btn hover:text-cyan-400 px-3 py-1 rounded">Users</button>
        <button onclick="switchTab('tab-broadcast')" class="nav-btn hover:text-cyan-400 px-3 py-1 rounded">Broadcast</button>
        <button onclick="switchTab('tab-plans')" class="nav-btn hover:text-cyan-400 px-3 py-1 rounded">Plans & Packs</button>
        <button onclick="switchTab('tab-media')" class="nav-btn hover:text-cyan-400 px-3 py-1 rounded">Media & Settings</button>
        <button onclick="switchTab('tab-settings')" class="nav-btn hover:text-cyan-400 px-3 py-1 rounded">Bot & Config</button>
    </nav>

    <!-- DASHBOARD -->
    <div id="tab-dashboard" class="tab-content active space-y-6">
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs font-mono">
            <div class="glass-card p-4 rounded-xl">Total Users: <b class="text-cyan-400">{{total_users}}</b></div>
            <div class="glass-card p-4 rounded-xl">Paid Orders: <b class="text-emerald-400">{{paid_orders}}</b></div>
            <div class="glass-card p-4 rounded-xl">Revenue: <b class="text-fuchsia-400">₹{{revenue}}</b></div>
        </div>
    </div>

    <!-- ORDERS -->
    <div id="tab-orders" class="tab-content space-y-6">
        <div class="glass-card p-6 rounded-2xl space-y-4">
            <h3 class="font-tech text-sm text-white font-bold uppercase">Customer Orders</h3>
            <table class="w-full text-left text-xs font-mono">
                <thead class="text-purple-400 border-b border-purple-900/40">
                    <tr><th class="p-2">Order</th><th class="p-2">User</th><th class="p-2">Pack</th><th class="p-2">Amount</th><th class="p-2">Status</th><th class="p-2 text-right">Action</th></tr>
                </thead>
                <tbody>
                    {{all_orders_html}}
                </tbody>
            </table>
        </div>
    </div>

    <!-- USERS -->
    <div id="tab-users" class="tab-content space-y-6">
        <div class="glass-card p-6 rounded-2xl space-y-4">
            <h3 class="font-tech text-sm text-white font-bold uppercase">Registered Users</h3>
            <table class="w-full text-left text-xs font-mono">
                <thead class="text-purple-400 border-b border-purple-900/40">
                    <tr><th class="p-2">ID</th><th class="p-2">Username</th><th class="p-2">Sub Tier</th><th class="p-2 text-right">Ban Status</th></tr>
                </thead>
                <tbody>
                    {{users_html}}
                </tbody>
            </table>
        </div>
    </div>

    <!-- BROADCAST -->
    <div id="tab-broadcast" class="tab-content space-y-6">
        <form method="POST" action="/c/{{slug}}/admin/broadcast/send" class="glass-card p-6 rounded-2xl space-y-4 text-xs font-mono">
            <h3 class="font-tech text-sm text-white font-bold uppercase">Mass Broadcast</h3>
            <textarea name="broadcast_message" rows="4" required placeholder="Type message..." class="w-full bg-[#070410] border border-purple-900 rounded p-3 text-white"></textarea>
            <button type="submit" class="bg-cyan-600 px-4 py-2 rounded text-white font-tech font-bold uppercase">Send Broadcast</button>
        </form>
    </div>

    <!-- MEDIA & GREETINGS -->
    <div id="tab-media" class="tab-content space-y-6">
        <form method="POST" action="/c/{{slug}}/admin/save/media" class="glass-card p-6 rounded-2xl space-y-4 text-xs font-mono">
            <h3 class="font-tech text-sm text-white font-bold uppercase">Media & Greetings</h3>
            <div><label class="block text-purple-300 mb-1">Welcome Text</label><textarea name="welcome_text" rows="3" required class="w-full bg-[#070410] border border-purple-900 rounded p-3 text-white">{{welcome_text}}</textarea></div>
            <div><label class="block text-purple-300 mb-1">Welcome Photo URL</label><input type="url" name="welcome_photo" value="{{welcome_photo}}" required class="w-full bg-[#070410] border border-purple-900 rounded p-3 text-white"></div>
            <div><label class="block text-purple-300 mb-1">Demo Video URLs (V1 Only - Comma separated)</label><textarea name="demo_video" rows="2" class="w-full bg-[#070410] border border-purple-900 rounded p-3 text-white">{{demo_video}}</textarea></div>
            <button type="submit" class="bg-fuchsia-600 px-4 py-2 rounded text-white font-tech font-bold uppercase">Save Media</button>
        </form>
    </div>

    <!-- PLANS -->
    <div id="tab-plans" class="tab-content space-y-6">
        {{plans_html}}
    </div>

    <!-- SETTINGS -->
    <div id="tab-settings" class="tab-content space-y-6">
        <form method="POST" action="/c/{{slug}}/admin/save/bot" class="glass-card p-6 rounded-2xl space-y-4 text-xs font-mono">
            <h3 class="font-tech text-sm text-fuchsia-300 font-bold uppercase">Bot Configuration</h3>
            <div><label class="block text-purple-300 mb-1">Telegram Bot Token</label><input type="text" name="bot_token" value="{{bot_token}}" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white"></div>
            <div><label class="block text-purple-300 mb-1">Admin Chat ID</label><input type="text" name="admin_chat_id" value="{{admin_chat_id}}" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white"></div>
            <div><label class="block text-purple-300 mb-1">UPI ID</label><input type="text" name="upi_id" value="{{upi_id}}" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white"></div>
            <div><label class="block text-purple-300 mb-1">Payee Name</label><input type="text" name="payee_name" value="{{payee_name}}" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white"></div>
            <button type="submit" class="bg-emerald-600 px-4 py-2 rounded text-white font-tech font-bold uppercase">Save & Restart Bot</button>
        </form>
        <form method="POST" action="/c/{{slug}}/admin/revenue/reset" onsubmit="return confirm('Reset all revenue counters?');" class="glass-card p-6 rounded-2xl space-y-4 text-xs font-mono">
             <h3 class="font-tech text-sm text-rose-300 font-bold uppercase">Danger Zone</h3>
             <button type="submit" class="bg-rose-600 px-4 py-2 rounded text-white font-tech font-bold uppercase">Reset Revenue</button>
        </form>
    </div>

    <script>
        function switchTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            document.querySelectorAll('.nav-btn').forEach(btn => {
                btn.classList.remove('text-cyan-400');
                btn.classList.add('text-slate-400');
            });
            event.target.classList.add('text-cyan-400');
            event.target.classList.remove('text-slate-400');
        }
    </script>
</body>
</html>"""

# ==========================================
# MASTER ROUTES
# ==========================================
@app.get("/master/login", response_class=HTMLResponse)
async def master_login_view(error: str | None = None):
    err_html = f'<div class="p-2 mt-4 rounded bg-rose-500/20 text-rose-300 text-xs text-center">{error}</div>' if error else ""
    return HTMLResponse(render_template(MASTER_LOGIN_TEMPLATE, title="Master Console", action_url="/master/login", error_html=err_html))

@app.post("/master/login")
async def master_login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == MASTER_USER and password == MASTER_PASS:
        resp = RedirectResponse(url="/master/clients", status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(key="master_auth", value=sign_token("master_admin"), httponly=True, max_age=86400 * 7)
        return resp
    err_html = '<div class="p-2 mt-4 rounded bg-rose-500/20 text-rose-300 text-xs text-center">Invalid credentials</div>'
    return HTMLResponse(render_template(MASTER_LOGIN_TEMPLATE, title="Master Console", action_url="/master/login", error_html=err_html), status_code=401)

@app.get("/master/logout")
async def master_logout():
    resp = RedirectResponse(url="/master/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key="master_auth")
    return resp

@app.get("/master/clients", response_class=HTMLResponse)
async def master_clients_dashboard(request: Request, message: str | None = None):
    if verify_token(request.cookies.get("master_auth")) != "master_admin":
        return RedirectResponse(url="/master/login", status_code=status.HTTP_303_SEE_OTHER)

    db = await get_master_db()
    try:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clients ORDER BY id DESC") as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()

    clients_html = ""
    for r in rows:
        c = dict(r)
        clients_html += f"""
        <tr class="border-b border-purple-900/20">
            <td class="p-2 font-bold">{c['client_name']}</td>
            <td class="p-2 text-purple-300 uppercase">{c['bot_type']}</td>
            <td class="p-2"><a href="/c/{c['slug']}/admin" target="_blank" class="text-cyan-400 underline">/c/{c['slug']}/admin ↗</a></td>
            <td class="p-2 text-right">
                <form method="POST" action="/master/client/delete" onsubmit="return confirm('Delete client?');" class="inline">
                    <input type="hidden" name="client_id" value="{c['id']}">
                    <button type="submit" class="text-rose-400 underline">Delete</button>
                </form>
            </td>
        </tr>"""
    
    if not clients_html:
        clients_html = '<tr><td colspan="4" class="p-4 text-center text-purple-400">No client panels created yet.</td></tr>'

    msg_html = f'<div class="p-3 bg-cyan-500/20 text-cyan-300 text-xs rounded">{message}</div>' if message else ""
    return HTMLResponse(render_template(MASTER_DASHBOARD_TEMPLATE, clients_html=clients_html, message_html=msg_html))

@app.post("/master/client/new")
async def master_create_client(request: Request, client_name: str = Form(...), slug: str = Form(...), bot_type: str = Form("pompom_v2"), admin_password: str = Form(...)):
    if verify_token(request.cookies.get("master_auth")) != "master_admin": return RedirectResponse(url="/master/login")
    clean_slug = slug.strip().lower().replace(" ", "-")
    today = datetime.now().date()
    expiry = (today + timedelta(days=30)).strftime("%Y-%m-%d")

    async with MASTER_DB_LOCK:
        db = await get_master_db()
        try:
            await db.execute("INSERT INTO clients (client_name, slug, make_date, expiry_date, plan_days, admin_username, admin_password, bot_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                             (client_name, clean_slug, today.strftime("%Y-%m-%d"), expiry, 30, "admin", admin_password.strip(), bot_type))
            await db.commit()
        except Exception:
            raise HTTPException(status_code=400, detail="Slug already in use.")
        finally:
            await db.close()

    await init_tenant_db(clean_slug, bot_type=bot_type)
    return RedirectResponse(url="/master/clients?message=Client+provisioned", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/master/client/delete")
async def master_delete_client(request: Request, client_id: int = Form(...)):
    if verify_token(request.cookies.get("master_auth")) != "master_admin": return RedirectResponse(url="/master/login")
    db = await get_master_db()
    try:
        async with db.execute("SELECT slug FROM clients WHERE id = ?", (client_id,)) as cur:
            row = await cur.fetchone()
            if row:
                slug = row[0]
                await bot_engine.stop_tenant_bot(slug)
                await db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
                await db.commit()
                if os.path.exists(get_tenant_db_path(slug)): os.remove(get_tenant_db_path(slug))
    finally:
        await db.close()
    return RedirectResponse(url="/master/clients?message=Client+deleted", status_code=status.HTTP_303_SEE_OTHER)

# ==========================================
# TENANT ROUTES
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
    if not client: raise HTTPException(status_code=404, detail="Store panel not found.")
    err_html = f'<div class="p-2 mt-4 rounded bg-rose-500/20 text-rose-300 text-xs text-center">{error}</div>' if error else ""
    return HTMLResponse(render_template(MASTER_LOGIN_TEMPLATE, title=client["client_name"] + " Admin", action_url=f"/c/{slug}/login", error_html=err_html))

@app.post("/c/{slug}/login")
async def tenant_login_post(slug: str, request: Request, username: str = Form(...), password: str = Form(...)):
    client = await get_tenant_meta(slug)
    if client and username == client["admin_username"] and password == client["admin_password"]:
        resp = RedirectResponse(url=f"/c/{slug}/admin", status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(key=f"tenant_auth_{slug}", value=sign_token(f"tenant_{slug}"), httponly=True, max_age=86400 * 7)
        return resp
    return RedirectResponse(url=f"/c/{slug}/login?error=Invalid+credentials", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/c/{slug}/logout")
async def tenant_logout(slug: str):
    resp = RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key=f"tenant_auth_{slug}")
    return resp

@app.get("/c/{slug}/admin", response_class=HTMLResponse)
async def tenant_admin_panel(slug: str, request: Request, message: str | None = None):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}":
        return RedirectResponse(url=f"/c/{slug}/login", status_code=status.HTTP_303_SEE_OTHER)

    client = await get_tenant_meta(slug)
    metrics = await get_tenant_dashboard_metrics(slug)

    db = await get_tenant_db(slug)
    try:
        async with db.execute("SELECT user_id, full_name, username, joined_at, premium_status, is_banned FROM users ORDER BY joined_at DESC LIMIT 50") as cur:
            users_list = await cur.fetchall()
        async with db.execute("SELECT plan_id, name, amount, validity, access_link FROM plans") as cur:
            plans = await cur.fetchall()
    finally:
        await db.close()

    bot_type = client["bot_type"]
    msg_html = f'<div class="p-3 bg-cyan-500/20 text-cyan-300 text-xs rounded font-mono">{message}</div>' if message else ""
    
    # Render Orders
    all_orders_html = ""
    for o in metrics.get("all_orders", []):
        all_orders_html += f"""
        <tr class="border-b border-purple-900/20">
            <td class="p-2 text-cyan-400">FT{o[0]}</td>
            <td class="p-2">{o[1]} (@{o[2]})</td>
            <td class="p-2">{o[3]}</td>
            <td class="p-2">₹{o[4]:.2f}</td>
            <td class="p-2">{o[5]}</td>
            <td class="p-2 text-right">
                <a href="/c/{slug}/admin/orders/status?order_id={o[0]}&action=approved" class="text-emerald-400 underline mr-2">Accept</a>
                <a href="/c/{slug}/admin/orders/status?order_id={o[0]}&action=rejected" class="text-rose-400 underline">Reject</a>
            </td>
        </tr>"""
    if not all_orders_html: all_orders_html = '<tr><td colspan="6" class="p-4 text-center text-purple-400">No orders yet.</td></tr>'

    # Render Users
    users_html = ""
    for u in users_list:
        ban_btn = "Unban" if u[5] else "Ban"
        ban_color = "text-emerald-400" if u[5] else "text-rose-400"
        users_html += f"""
        <tr class="border-b border-purple-900/20">
            <td class="p-2 text-cyan-400">{u[0]}</td>
            <td class="p-2">@{u[2]}</td>
            <td class="p-2"><b>{u[4]}</b></td>
            <td class="p-2 text-right">
                <form method="POST" action="/c/{slug}/admin/users/ban" class="inline">
                    <input type="hidden" name="user_id" value="{u[0]}">
                    <input type="hidden" name="status" value="{0 if u[5] else 1}">
                    <button type="submit" class="{ban_color} underline">{ban_btn}</button>
                </form>
            </td>
        </tr>"""
    if not users_html: users_html = '<tr><td colspan="4" class="p-4 text-center text-purple-400">No users yet.</td></tr>'

    # Render Plans Based on Engine
    plans_html = ""
    if bot_type == "pompom_v2":
        v2_btns_str = await get_tenant_setting(slug, "v2_buttons")
        v2_details_str = await get_tenant_setting(slug, "v2_details")
        v2_btns = json.loads(v2_btns_str) if v2_btns_str else []
        v2_details = json.loads(v2_details_str) if v2_details_str else {}

        pack_cards = ""
        for i, name in enumerate(v2_btns):
            d = v2_details.get(str(i), {})
            pack_cards += f"""
            <div class="glass-card rounded-xl p-4 border border-purple-900/40 space-y-3">
                <div class="flex justify-between items-center border-b border-purple-900/30 pb-2">
                    <span class="text-xs font-tech text-cyan-400 font-bold">🟢 PACK #{i + 1}</span>
                    <form method="POST" action="/c/{slug}/admin/v2/plans/delete" onsubmit="return confirm('Delete pack?');">
                        <input type="hidden" name="btn_index" value="{i}">
                        <button type="submit" class="text-[10px] text-rose-400 underline">Delete</button>
                    </form>
                </div>
                <form method="POST" action="/c/{slug}/admin/v2/plans/update" class="space-y-2 text-xs font-mono">
                    <input type="hidden" name="btn_index" value="{i}">
                    <input type="text" name="pack_name" value="{d.get('pack','')}" placeholder="Pack Name" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white">
                    <input type="number" step="any" name="price" value="{d.get('price','299')}" placeholder="Price" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white">
                    <input type="url" name="link" value="{d.get('link','')}" placeholder="Access Link (t.me/...)" class="w-full bg-[#070410] border border-cyan-500/40 rounded p-2 text-cyan-300">
                    <button type="submit" class="w-full bg-purple-900/60 hover:bg-cyan-600 text-white py-1.5 rounded font-tech">Save</button>
                </form>
            </div>"""

        plans_html = f"""
        <div class="glass-card p-6 rounded-2xl space-y-4">
            <div class="flex justify-between items-center border-b border-purple-900/40 pb-3">
                <h3 class="font-tech text-sm text-cyan-300 font-bold uppercase">Multi-Packs Inventory</h3>
                <form method="POST" action="/c/{slug}/admin/v2/plans/add" class="flex gap-2 text-xs">
                    <input type="text" name="name" required placeholder="New Pack Name" class="bg-[#070410] border border-purple-900 rounded px-2 py-1 text-white">
                    <button type="submit" class="bg-cyan-600 font-tech px-3 py-1 rounded font-bold text-white">+ Add Pack</button>
                </form>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {pack_cards}
            </div>
        </div>"""
    else:
        # V1 Plans
        v1_rows = ""
        for p in plans:
            v1_rows += f"""
            <tr class="border-b border-purple-900/20">
                <td class="p-2">{p[1]}</td><td class="p-2">₹{p[2]}</td><td class="p-2 text-cyan-400">{p[4]}</td>
                <td class="p-2 text-right">
                    <form method="POST" action="/c/{slug}/admin/v1/plans/delete"><input type="hidden" name="plan_id" value="{p[0]}"><button type="submit" class="text-rose-400 underline">Del</button></form>
                </td>
            </tr>"""
        
        plans_html = f"""
        <div class="glass-card p-6 rounded-2xl space-y-4">
            <h3 class="font-tech text-sm text-cyan-300 font-bold uppercase">Add Subscription Tier (V1)</h3>
            <form method="POST" action="/c/{slug}/admin/v1/plans/add" class="flex gap-2 text-xs font-mono">
                <input type="text" name="plan_id" required placeholder="plan_id" class="bg-[#070410] border border-purple-900 rounded p-2 text-white">
                <input type="text" name="name" required placeholder="Name" class="bg-[#070410] border border-purple-900 rounded p-2 text-white">
                <input type="number" step="any" name="amount" required placeholder="Price" class="bg-[#070410] border border-purple-900 rounded p-2 text-white">
                <input type="text" name="validity" required placeholder="Validity" class="bg-[#070410] border border-purple-900 rounded p-2 text-white">
                <input type="url" name="access_link" placeholder="Access Link" class="bg-[#070410] border border-cyan-500/40 rounded p-2 text-cyan-300">
                <button type="submit" class="bg-cyan-600 font-tech px-4 py-2 rounded font-bold text-white">+ Add Plan</button>
            </form>
            <table class="w-full text-left text-xs font-mono mt-4">
                <thead class="text-purple-400 border-b border-purple-900/40"><tr><th class="p-2">Name</th><th class="p-2">Price</th><th class="p-2">Link</th><th class="p-2 text-right">Delete</th></tr></thead>
                <tbody>{v1_rows}</tbody>
            </table>
        </div>"""

    replacements = {
        "slug": slug,
        "client_name": client["client_name"],
        "bot_type": bot_type.upper(),
        "status_pulse": "bg-cyan-400" if slug in bot_engine.active_bots else "bg-rose-500",
        "status_text": "ONLINE" if slug in bot_engine.active_bots else "OFFLINE",
        "message_html": msg_html,
        "total_users": metrics["total_users"],
        "paid_orders": metrics["paid_orders"],
        "revenue": metrics["revenue"],
        "all_orders_html": all_orders_html,
        "users_html": users_html,
        "plans_html": plans_html,
        "welcome_text": await get_tenant_setting(slug, "welcome_text"),
        "welcome_photo": await get_tenant_setting(slug, "welcome_photo"),
        "demo_video": await get_tenant_setting(slug, "demo_video"),
        "bot_token": await get_tenant_setting(slug, "bot_token"),
        "admin_chat_id": await get_tenant_setting(slug, "admin_chat_id"),
        "upi_id": await get_tenant_setting(slug, "upi_id"),
        "payee_name": await get_tenant_setting(slug, "payee_name")
    }

    return HTMLResponse(render_template(TENANT_DASHBOARD_TEMPLATE, **replacements))

@app.post("/c/{slug}/admin/save/bot")
async def tenant_save_bot(slug: str, request: Request, bot_token: str = Form(""), upi_id: str = Form(""), payee_name: str = Form(""), admin_chat_id: str = Form("")):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    await update_tenant_setting(slug, "bot_token", bot_token.strip())
    await update_tenant_setting(slug, "upi_id", upi_id.strip())
    await update_tenant_setting(slug, "payee_name", payee_name.strip())
    await update_tenant_setting(slug, "admin_chat_id", admin_chat_id.strip())
    
    async with MASTER_DB_LOCK:
        db = await get_master_db()
        try:
            await db.execute("UPDATE clients SET bot_token = ? WHERE slug = ?", (bot_token.strip(), slug))
            await db.commit()
        finally:
            await db.close()

    if bot_token.strip(): await bot_engine.start_tenant_bot(slug, bot_token.strip())
    return RedirectResponse(url=f"/c/{slug}/admin?message=Bot+Configuration+Saved", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/save/media")
async def tenant_save_media(slug: str, request: Request, welcome_text: str = Form(""), welcome_photo: str = Form(""), demo_video: str = Form("")):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    await update_tenant_setting(slug, "welcome_text", welcome_text.strip())
    await update_tenant_setting(slug, "welcome_photo", welcome_photo.strip())
    await update_tenant_setting(slug, "demo_video", demo_video.strip())
    return RedirectResponse(url=f"/c/{slug}/admin?message=Media+Configuration+Saved", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/v2/plans/add")
async def tenant_v2_add_plan(slug: str, request: Request, name: str = Form(...)):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    btns_str = await get_tenant_setting(slug, "v2_buttons")
    details_str = await get_tenant_setting(slug, "v2_details")
    btns = json.loads(btns_str) if btns_str else []
    details = json.loads(details_str) if details_str else {}
    
    idx = str(len(btns))
    btns.append(name.strip())
    details[idx] = {"pack": name.strip(), "price": "299", "link": ""}
    
    await update_tenant_setting(slug, "v2_buttons", json.dumps(btns))
    await update_tenant_setting(slug, "v2_details", json.dumps(details))
    return RedirectResponse(url=f"/c/{slug}/admin?message=Pack+Added", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/v2/plans/update")
async def tenant_v2_update_plan(slug: str, request: Request, btn_index: str = Form(...), pack_name: str = Form(...), price: str = Form(...), link: str = Form("")):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    details_str = await get_tenant_setting(slug, "v2_details")
    details = json.loads(details_str) if details_str else {}
    details[btn_index] = {"pack": pack_name.strip(), "price": price.strip(), "link": link.strip()}
    await update_tenant_setting(slug, "v2_details", json.dumps(details))
    return RedirectResponse(url=f"/c/{slug}/admin?message=Pack+Updated", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/v2/plans/delete")
async def tenant_v2_delete_plan(slug: str, request: Request, btn_index: int = Form(...)):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    btns_str = await get_tenant_setting(slug, "v2_buttons")
    details_str = await get_tenant_setting(slug, "v2_details")
    media_str = await get_tenant_setting(slug, "v2_media")
    btns = json.loads(btns_str) if btns_str else []
    details = json.loads(details_str) if details_str else {}
    media = json.loads(media_str) if media_str else {}

    if 0 <= btn_index < len(btns):
        fb, fd, fm = [], {}, {}
        curr = 0
        for old_i in range(len(btns)):
            if old_i == btn_index: continue
            fb.append(btns[old_i])
            if str(old_i) in details: fd[str(curr)] = details[str(old_i)]
            if str(old_i) in media: fm[str(curr)] = media[str(old_i)]
            curr += 1
        await update_tenant_setting(slug, "v2_buttons", json.dumps(fb))
        await update_tenant_setting(slug, "v2_details", json.dumps(fd))
        await update_tenant_setting(slug, "v2_media", json.dumps(fm))
    return RedirectResponse(url=f"/c/{slug}/admin?message=Pack+Deleted", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/v1/plans/add")
async def tenant_v1_add_plan(slug: str, request: Request, plan_id: str = Form(...), name: str = Form(...), amount: float = Form(...), validity: str = Form(...), access_link: str = Form("")):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    await add_tenant_new_plan(slug, plan_id.strip(), name.strip(), amount, validity.strip(), access_link.strip())
    return RedirectResponse(url=f"/c/{slug}/admin?message=Plan+Added", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/v1/plans/delete")
async def tenant_v1_delete_plan(slug: str, request: Request, plan_id: str = Form(...)):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    await delete_tenant_plan(slug, plan_id.strip())
    return RedirectResponse(url=f"/c/{slug}/admin?message=Plan+Deleted", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/c/{slug}/admin/orders/status")
async def tenant_order_status(slug: str, request: Request, order_id: int, action: str):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    
    client = await get_tenant_meta(slug)
    bot_type = client["bot_type"] if client else "pompom_v1"

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
                    await bot_engine.notify_payment_approved(slug, bot_type, user_id, plan_name)
                else:
                    await db.execute("UPDATE payments SET status = 'rejected' WHERE id = ?", (int(order_id),))
                    await db.commit()
        finally:
            await db.close()
    return RedirectResponse(url=f"/c/{slug}/admin?message=Order+Status+Updated", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/users/ban")
async def tenant_ban_user(slug: str, request: Request, user_id: int = Form(...), status_val: int = Form(..., alias="status")):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (status_val, user_id))
            await db.commit()
        finally:
            await db.close()
    return RedirectResponse(url=f"/c/{slug}/admin?message=User+Ban+Status+Updated", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/broadcast/send")
async def tenant_broadcast_send(slug: str, request: Request, broadcast_message: str = Form(...)):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    bot = bot_engine.active_bots.get(slug)
    if not bot: return RedirectResponse(url=f"/c/{slug}/admin?message=Bot+offline", status_code=status.HTTP_303_SEE_OTHER)
    db = await get_tenant_db(slug)
    try:
        async with db.execute("SELECT user_id FROM users WHERE is_banned=0") as cur:
            users = await cur.fetchall()
    finally:
        await db.close()
    sent = 0
    for (uid,) in users:
        try:
            await bot.send_message(chat_id=uid, text=broadcast_message)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    return RedirectResponse(url=f"/c/{slug}/admin?message=Broadcast+sent+to+{sent}+users", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/c/{slug}/admin/revenue/reset")
async def tenant_reset_revenue(slug: str, request: Request):
    if verify_token(request.cookies.get(f"tenant_auth_{slug}")) != f"tenant_{slug}": return RedirectResponse(url=f"/c/{slug}/login")
    async with TENANT_DB_LOCKS[slug]:
        db = await get_tenant_db(slug)
        try:
            await db.execute("DELETE FROM payments")
            await db.commit()
        finally:
            await db.close()
    return RedirectResponse(url=f"/c/{slug}/admin?message=Revenue+reset", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/")
async def root():
    return RedirectResponse(url="/master/login", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/health")
async def health():
    return {"status": "ok", "active_bots": len(bot_engine.active_bots)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("pommmaster:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
