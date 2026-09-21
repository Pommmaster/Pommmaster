# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import os
import random
import sqlite3
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==========================================
# CONFIG & AUTHENTICATION
# ==========================================
ADMIN_USER = os.getenv("ADMIN_USER", "nagato")
DEFAULT_PASS = os.getenv("ADMIN_PASS", "nagato@123")
AUTH_COOKIE_NAME = "session_token"
AUTH_SECRET = "admin_authenticated_session_key_99"

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.join(os.getcwd(), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_NAME = os.path.join(DATA_DIR, "nagato_database.db")

INITIAL_BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEFAULT_BACK_BUTTON = "🔙 Back"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pending_verifications = {}
active_admin_uploads = {}

# ==========================================
# DATABASE INITIALIZATION & SCHEMA
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            token TEXT,
            welcome_images_json TEXT,
            welcome_caption TEXT,
            buttons_json TEXT,
            button_videos_json TEXT,
            button_details_json TEXT,
            upi_id TEXT,
            payee_name TEXT,
            admin_chat_id TEXT,
            btn_how_to_use TEXT,
            btn_report_issue TEXT,
            btn_language TEXT,
            msg_how_to_use TEXT,
            msg_report_issue TEXT,
            msg_language TEXT,
            admin_user TEXT,
            admin_pass TEXT,
            license_expiry TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            premium_status TEXT DEFAULT 'Free',
            is_banned INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id TEXT,
            chat_id INTEGER,
            username TEXT,
            pack_name TEXT,
            amount REAL,
            status TEXT DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    for col, col_type in [("premium_status", "TEXT DEFAULT 'Free'"), ("is_banned", "INTEGER DEFAULT 0")]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    try:
        c.execute("ALTER TABLE settings ADD COLUMN license_expiry TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute("SELECT COUNT(*) FROM settings")
    if c.fetchone()[0] == 0:
        default_packs_config = [
            {"btn": "PREMIUM 1",       "pack": "HOUSEWIFE 😱",           "price": "49",   "desc": "HOUSEWIFE IN HOME WITH HUSBAND OR HIS BROTHER", "link": "https://t.me/+YourLink1"},
            {"btn": "INDIAN VIP 2",    "pack": "Indian Webseries Pack",  "price": "99",   "desc": "Full Indian webseries collection in 1080p HD.", "link": "https://t.me/+YourLink2"},
            {"btn": "BOLLYWOOD 3",     "pack": "Bollywood Special",      "price": "149",  "desc": "Exclusive Bollywood entertainment vault.",      "link": "https://t.me/+YourLink3"},
            {"btn": "DESI HUB 4",      "pack": "Desi Hub Collection",    "price": "199",  "desc": "Instant private streaming channel bundle.",     "link": "https://t.me/+YourLink4"},
            {"btn": "WEEKEND COMBO 5", "pack": "Weekend Combo Pass",     "price": "249",  "desc": "Complete 3-day binge-watching pass.",          "link": "https://t.me/+YourLink5"},
            {"btn": "MONTHLY PASS 6",  "pack": "VIP Monthly Access",     "price": "299",  "desc": "30 days unrestricted streaming & downloads.",   "link": "https://t.me/+YourLink6"},
            {"btn": "ULTRA 4K 7",      "pack": "Ultra 4K Stream Vault",  "price": "349",  "desc": "Crystal-clear 4K UHD video collections.",       "link": "https://t.me/+YourLink7"},
            {"btn": "PRO BUNDLE 8",    "pack": "Pro Streaming Bundle",   "price": "399",  "desc": "Multi-category master bundle with daily drops.", "link": "https://t.me/+YourLink8"},
            {"btn": "SPECIAL 9",       "pack": "Special Vault Access",   "price": "449",  "desc": "Private drive archives & premium links.",       "link": "https://t.me/+YourLink9"},
            {"btn": "MEGA PASS 10",    "pack": "Mega VIP Tier",          "price": "499",  "desc": "Full season drops and all categories unlocked.", "link": "https://t.me/+YourLink10"},
            {"btn": "DIAMOND 11",      "pack": "Diamond Membership",     "price": "599",  "desc": "Priority direct access to all private channels.", "link": "https://t.me/+YourLink11"},
            {"btn": "PLATINUM 12",     "pack": "Platinum Vault",         "price": "699",  "desc": "High-speed downloads and backup links included.", "link": "https://t.me/+YourLink12"},
            {"btn": "ANNUAL VIP 13",   "pack": "1-Year Full Access",     "price": "799",  "desc": "365 days of full library access.",               "link": "https://t.me/+YourLink13"},
            {"btn": "SEASON PASS 14",  "pack": "All-Season Vault",       "price": "899",  "desc": "Complete archive including all upcoming series.", "link": "https://t.me/+YourLink14"},
            {"btn": "MASTER PASS 15",  "pack": "Master Access Pass",     "price": "999",  "desc": "Complete cloud library + private VIP bot.",      "link": "https://t.me/+YourLink15"},
            {"btn": "SUPREME 16",      "pack": "Supreme Lifetime Tier",  "price": "1199", "desc": "Permanent access with no renewal fees.",        "link": "https://t.me/+YourLink16"},
            {"btn": "LIFETIME 17",     "pack": "Unlimited Pass 17",      "price": "1499", "desc": "All current and future releases forever.",      "link": "https://t.me/+YourLink17"},
        ]

        default_buttons = [item["btn"] for item in default_packs_config]
        default_images = [
            "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=800",
            "https://images.unsplash.com/photo-1579783902614-a3fb3927b675?w=800"
        ]
        default_button_videos = {str(i): [] for i in range(len(default_packs_config))}
        default_details = {
            str(i): {
                "pack": default_packs_config[i]["pack"],
                "price": default_packs_config[i]["price"],
                "desc": default_packs_config[i]["desc"],
                "link": default_packs_config[i]["link"]
            } for i in range(len(default_packs_config))
        }
        thirty_days_later = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        c.execute("""
            INSERT INTO settings (
                id, token, welcome_images_json, welcome_caption, buttons_json,
                button_videos_json, button_details_json,
                upi_id, payee_name, admin_chat_id,
                btn_how_to_use, btn_report_issue, btn_language,
                msg_how_to_use, msg_report_issue, msg_language,
                admin_user, admin_pass, license_expiry
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            INITIAL_BOT_TOKEN,
            json.dumps(default_images),
            "✨ *Welcome to our Exclusive Hub!*\n\nSelect an option below to preview content:",
            json.dumps(default_buttons),
            json.dumps(default_button_videos),
            json.dumps(default_details),
            "thesalesgod@nyes",
            "Trusted Seller",
            "",
            "📖 How To Use",
            "🚨 Report Issue",
            "🌐 Language",
            "Select any package to preview videos, then complete payment via UPI QR code.",
            "For help or support, contact support directly: @YourSupportHandle",
            "🌐 English is active by default.",
            ADMIN_USER,
            DEFAULT_PASS,
            thirty_days_later
        ))
        conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE id = 1")
    row = c.fetchone()
    conn.close()

    if not row:
        return {}

    expiry = row[18] if len(row) > 18 and row[18] else (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    button_details = {}
    if row[6]:
        try:
            button_details = json.loads(row[6])
        except Exception:
            pass

    button_videos = {}
    if row[5]:
        try:
            button_videos = json.loads(row[5])
        except Exception:
            pass

    return {
        "token": row[1] or "",
        "welcome_images": json.loads(row[2]) if row[2] else [],
        "welcome_caption": row[3] or "",
        "buttons": json.loads(row[4]) if row[4] else [],
        "button_videos": button_videos,
        "button_details": button_details,
        "upi_id": row[7] or "",
        "payee_name": row[8] or "Merchant",
        "admin_chat_id": row[9] or "",
        "btn_how_to_use": row[10] or "📖 How To Use",
        "btn_report_issue": row[11] or "🚨 Report Issue",
        "btn_language": row[12] or "🌐 Language",
        "msg_how_to_use": row[13] or "Select any pack to proceed.",
        "msg_report_issue": row[14] or "Contact support.",
        "msg_language": row[15] or "Current language: English.",
        "admin_user": row[16] if len(row) > 16 and row[16] else ADMIN_USER,
        "admin_pass": row[17] if len(row) > 17 and row[17] else DEFAULT_PASS,
        "license_expiry": expiry
    }

def update_field(field_name: str, value: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(f"UPDATE settings SET {field_name} = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()

def register_user(chat_id: int, username: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (chat_id, username) VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username
    """, (chat_id, username or "N/A"))
    conn.commit()
    conn.close()

def get_user(chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id, username, joined_at, premium_status, is_banned FROM users WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row

def update_user_subscription(chat_id: int, plan_name: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET premium_status = ? WHERE chat_id = ?", (plan_name, chat_id))
    conn.commit()
    conn.close()

def set_user_ban(chat_id: int, is_banned: int):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = ? WHERE chat_id = ?", (is_banned, chat_id))
    conn.commit()
    conn.close()

def log_order(txn_id: str, chat_id: int, username: str, pack_name: str, amount: float):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        INSERT INTO orders (txn_id, chat_id, username, pack_name, amount)
        VALUES (?, ?, ?, ?, ?)
    """, (txn_id, chat_id, username or "N/A", pack_name, amount))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0.0) FROM orders WHERE status = 'Paid'")
    paid_row = c.fetchone()
    paid_orders = paid_row[0] or 0
    revenue = paid_row[1] or 0.0

    c.execute("SELECT txn_id, chat_id, username, pack_name, amount, created_at, status, id FROM orders ORDER BY id DESC LIMIT 10")
    recent_orders = c.fetchall()
    conn.close()
    return total_users, paid_orders, revenue, recent_orders

def get_all_orders():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT txn_id, chat_id, username, pack_name, amount, created_at, status, id FROM orders ORDER BY id DESC")
    orders = c.fetchall()
    conn.close()
    return orders

def get_order_by_id(order_id: int):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT txn_id, chat_id, username, pack_name, amount, status FROM orders WHERE id = ?", (order_id,))
    row = c.fetchone()
    conn.close()
    return row

def update_order_status(order_id: int, status: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()

init_db()

# ==========================================
# UPI & TRANSACTION HELPERS
# ==========================================
def make_upi_uri(upi_id: str, payee_name: str, amount: str, note: str) -> str:
    clean_amount = "".join(c for c in str(amount) if c.isdigit() or c == '.') or "0"
    params = {
        "pa": upi_id.strip(),
        "pn": payee_name.strip() or "Merchant",
        "am": clean_amount,
        "cu": "INR",
        "tn": note[:50]
    }
    return f"upi://pay?{urllib.parse.urlencode(params)}"

def generate_upi_qr_url(upi_uri: str) -> str:
    encoded = urllib.parse.quote(upi_uri)
    return f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&data={encoded}"

def generate_txn_id(pack_name: str) -> str:
    slug = "".join(c for c in pack_name if c.isalnum()).upper()[:8] or "PACK"
    date_str = time.strftime("%y%m%d")
    rnd = f"{random.randint(1000, 99999):05d}"
    return f"TXN-{date_str}-{slug}-{rnd}"

# ==========================================
# BOT CONTROLLER
# ==========================================
class BotManager:
    def __init__(self):
        self.app: Application | None = None
        self.task: asyncio.Task | None = None
        self.status = "Stopped"

    async def _run_bot(self, token: str):
        try:
            builder = ApplicationBuilder().token(token)
            self.app = builder.build()

            bot_info = await self.app.bot.get_me()
            logger.info(f"Bot authenticated as @{bot_info.username}")

            self.app.add_handler(CommandHandler("start", handle_start))
            self.app.add_handler(CommandHandler("cancel", handle_cancel))
            self.app.add_handler(CommandHandler("upload", handle_admin_upload_command))
            self.app.add_handler(CommandHandler("clear", handle_admin_clear_command))
            self.app.add_handler(CommandHandler("done", handle_admin_done_command))
            self.app.add_handler(CallbackQueryHandler(handle_callback))
            self.app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION, handle_incoming_media))

            await self.app.initialize()
            await self.app.bot.delete_webhook(drop_pending_updates=True)
            await self.app.updater.start_polling(drop_pending_updates=True)
            await self.app.start()
            
            self.status = "Running"
            logger.info("Bot is active and polling.")

            while self.status == "Running":
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Bot runtime failure: {e}")
            self.status = f"Error: {e}"
        finally:
            if self.app:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
                if self.app.running:
                    await self.app.stop()
                await self.app.shutdown()
            if self.status == "Running":
                self.status = "Stopped"

    async def stop(self):
        if self.task and not self.task.done():
            self.status = "Stopping"
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.status = "Stopped"

    async def restart(self, token: str):
        await self.stop()
        if token.strip():
            self.task = asyncio.create_task(self._run_bot(token.strip()))
            await asyncio.sleep(1.5)

bot_manager = BotManager()

# ==========================================
# KEYBOARD BUILDERS
# ==========================================
def build_main_keyboard(cfg):
    keyboard = []
    num_buttons = len(cfg["buttons"])
    for idx in range(num_buttons):
        b_name = cfg["buttons"][idx]
        details = cfg["button_details"].get(str(idx), {"price": "299"})
        price = details.get("price", "299")

        display_label = f"🟢 {b_name} - ₹{price}"

        btn = InlineKeyboardButton(
            text=display_label,
            callback_data=f"btn_cat_{idx}"
        )
        keyboard.append([btn])

    row_actions = [
        InlineKeyboardButton(
            text=cfg["btn_how_to_use"],
            callback_data="act_how_to_use"
        ),
        InlineKeyboardButton(
            text=cfg["btn_report_issue"],
            callback_data="act_report_issue"
        )
    ]
    keyboard.append(row_actions)

    btn_lang = InlineKeyboardButton(
        text=cfg["btn_language"],
        callback_data="act_language"
    )
    keyboard.append([btn_lang])
    return InlineKeyboardMarkup(keyboard)

async def send_media_in_chunks(context: ContextTypes.DEFAULT_TYPE, chat_id: int, media_list: list):
    for i in range(0, len(media_list), 10):
        chunk = media_list[i:i + 10]
        if len(chunk) == 1:
            item = chunk[0]
            if isinstance(item, InputMediaPhoto):
                await context.bot.send_photo(chat_id=chat_id, photo=item.media)
            else:
                await context.bot.send_video(chat_id=chat_id, video=item.media, supports_streaming=True)
        else:
            await context.bot.send_media_group(chat_id=chat_id, media=chunk)

def is_admin(chat_id: int) -> bool:
    cfg = get_settings()
    admin_id = cfg.get("admin_chat_id", "").strip()
    return str(chat_id) == admin_id

# ==========================================
# IN-BOT ADMIN COMMANDS
# ==========================================
async def handle_admin_upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ You are not registered as the Admin. Set your Chat ID in Admin Panel.")
        return

    cfg = get_settings()
    total_btns = len(cfg["buttons"])
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"⚠️ Usage: `/upload <number 1-{total_btns}>`\nExample: `/upload 1`", parse_mode="Markdown")
        return

    btn_num = int(context.args[0])
    if not (1 <= btn_num <= total_btns):
        await update.message.reply_text(f"⚠️ Button number must be between 1 and {total_btns}.")
        return

    btn_idx = btn_num - 1
    active_admin_uploads[chat_id] = btn_idx

    pack_name = cfg["button_details"].get(str(btn_idx), {}).get("pack", f"Button {btn_num}")

    msg = (
        f"📥 *Bulk Media Upload Mode Activated!*\n\n"
        f"🎯 **Target Button:** #{btn_num} ({pack_name})\n\n"
        f"Send or forward photos, videos, or documents in bulk.\n"
        f"Type `/done` to finish upload mode."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_admin_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    cfg = get_settings()
    total_btns = len(cfg["buttons"])
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"⚠️ Usage: `/clear <1-{total_btns}>`")
        return

    btn_idx = int(context.args[0]) - 1
    if 0 <= btn_idx < total_btns:
        vids = cfg["button_videos"]
        vids[str(btn_idx)] = []
        update_field("button_videos_json", json.dumps(vids))
        await update.message.reply_text(f"🗑️ Cleared all media for Button #{btn_idx + 1}.")

async def handle_admin_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_admin_uploads:
        btn_idx = active_admin_uploads.pop(chat_id)
        cfg = get_settings()
        count = len(cfg["button_videos"].get(str(btn_idx), []))
        await update.message.reply_text(f"✅ *Upload Finished!*\nSaved **{count}** media items to Button #{btn_idx + 1}.", parse_mode="Markdown")
    else:
        await update.message.reply_text("No active upload session found.")

# ==========================================
# TELEGRAM USER HANDLERS
# ==========================================
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    if user and user[4] == 1:
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return

    register_user(chat_id, update.effective_user.username or "")
    pending_verifications.pop(chat_id, None)
    cfg = get_settings()

    valid_images = [img.strip() for img in cfg["welcome_images"] if img.strip()]
    if valid_images:
        media_group = [InputMediaPhoto(media=url) for url in valid_images]
        try:
            await send_media_in_chunks(context, chat_id, media_group)
        except Exception as e:
            logger.error(f"Failed to send images: {e}")

    caption_text = cfg["welcome_caption"].strip() if cfg["welcome_caption"] else "✨ Select an option below to continue:"

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption_text,
            reply_markup=build_main_keyboard(cfg),
            parse_mode="Markdown"
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption_text,
            reply_markup=build_main_keyboard(cfg)
        )

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending_verifications.pop(chat_id, None)
    active_admin_uploads.pop(chat_id, None)
    await update.message.reply_text("❌ Action cancelled.")
    await handle_start(update, context)

# ==========================================
# CALLBACK QUERY ROUTER
# ==========================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    cfg = get_settings()

    user = get_user(chat_id)
    if user and user[4] == 1:
        await query.answer("You are banned from using this bot.", show_alert=True)
        return

    if data.startswith("adm_ord:"):
        if not is_admin(chat_id):
            await query.answer("Unauthorized.", show_alert=True)
            return

        parts = data.split(":")
        order_id = int(parts[1])
        act = parts[2]
        new_status = "Paid" if act == "accept" else "Rejected"
        
        update_order_status(order_id, new_status)
        order = get_order_by_id(order_id)
        
        if order:
            txn_id, u_chat_id, uname, pack_name, amount, _ = order
            if new_status == "Paid":
                update_user_subscription(u_chat_id, pack_name)
                
                delivery_link = ""
                for idx in range(len(cfg["buttons"])):
                    d = cfg["button_details"].get(str(idx), {})
                    if d.get("pack", "").strip() == pack_name.strip():
                        delivery_link = d.get("link", "").strip()
                        break

                approval_text = (
                    f"🎉 *Payment Verified & Approved!*\n\n"
                    f"📦 *Pack:* {pack_name}\n"
                    f"💰 *Amount:* ₹{amount:.2f}\n"
                    f"🧾 *Txn:* `{txn_id}`\n\n"
                    f"Thank you for your purchase! Access your benefits below:"
                )

                reply_markup = None
                if delivery_link:
                    reply_markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🚀 Access Exclusive Content", url=delivery_link)
                    ]])

                try:
                    await context.bot.send_message(
                        chat_id=u_chat_id,
                        text=approval_text,
                        reply_markup=reply_markup,
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Failed delivering approval: {e}")
            else:
                try:
                    await context.bot.send_message(
                        chat_id=u_chat_id,
                        text=f"❌ Your payment for *{pack_name}* (`{txn_id}`) was rejected. Please contact support.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

        status_tag = "✅ APPROVED & DELIVERED" if new_status == "Paid" else "❌ REJECTED"
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Order #{order_id} status changed to: {status_tag}")
        await query.answer(f"Order #{order_id} {new_status}!")
        return

    if data == "btn_home":
        pending_verifications.pop(chat_id, None)
        await query.answer()
        await handle_start(update, context)

    elif data == "act_how_to_use":
        await query.answer(cfg["msg_how_to_use"], show_alert=True)
    elif data == "act_report_issue":
        await query.answer(cfg["msg_report_issue"], show_alert=True)
    elif data == "act_language":
        await query.answer(cfg["msg_language"], show_alert=True)

    elif data.startswith("btn_cat_"):
        await query.answer()
        btn_idx = data.replace("btn_cat_", "")

        button_videos = cfg["button_videos"].get(btn_idx, [])
        valid_media = [v.strip() for v in button_videos if v.strip()]

        album = []
        fallback_urls = []

        for item in valid_media:
            if item.startswith("http://") or item.startswith("https://"):
                if any(item.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                    album.append(InputMediaPhoto(media=item))
                elif any(item.lower().endswith(ext) for ext in [".mp4", ".mov", ".m4v"]):
                    album.append(InputMediaVideo(media=item, supports_streaming=True))
                else:
                    fallback_urls.append(item)
            else:
                album.append(InputMediaVideo(media=item, supports_streaming=True))

        if album:
            for i in range(0, len(album), 10):
                chunk = album[i:i + 10]
                if len(chunk) == 1:
                    single = chunk[0]
                    try:
                        if isinstance(single, InputMediaPhoto):
                            await context.bot.send_photo(chat_id=chat_id, photo=single.media)
                        else:
                            await context.bot.send_video(chat_id=chat_id, video=single.media, supports_streaming=True)
                    except Exception:
                        try:
                            await context.bot.send_photo(chat_id=chat_id, photo=single.media)
                        except Exception:
                            pass
                else:
                    try:
                        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
                    except Exception as e:
                        logger.warning(f"send_media_group error ({e}), delivering individually...")
                        for m_item in chunk:
                            try:
                                await context.bot.send_video(chat_id=chat_id, video=m_item.media, supports_streaming=True)
                            except Exception:
                                try:
                                    await context.bot.send_photo(chat_id=chat_id, photo=m_item.media)
                                except Exception:
                                    pass

        if fallback_urls:
            watch_keyboard = [
                [InlineKeyboardButton(f"▶️ Watch Preview Clip {i}", url=link)]
                for i, link in enumerate(fallback_urls, 1)
            ]
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🎬 *Sample Preview Clips:*",
                    reply_markup=InlineKeyboardMarkup(watch_keyboard),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        details = cfg["button_details"].get(btn_idx, {
            "pack": f"VIP Pack {int(btn_idx) + 1}",
            "price": "299",
            "desc": "Full premium access."
        })

        pack_name = details.get("pack", f"Pack {int(btn_idx) + 1}")
        price = details.get("price", "0")
        desc = details.get("desc", "Instant access after payment.")

        preview_text = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎀 *Pack*\n"
            f"{pack_name}\n\n"
            f"💰 *Price*\n"
            f"₹{price}\n\n"
            f"📄 *Description*\n"
            f"{desc}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )

        preview_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💳 Buy Now",
                    callback_data=f"action_proceed_pay_{btn_idx}"
                )
            ],
            [
                InlineKeyboardButton(
                    DEFAULT_BACK_BUTTON,
                    callback_data="btn_home"
                )
            ]
        ])

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=preview_text,
                reply_markup=preview_markup,
                parse_mode="Markdown"
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=preview_text.replace("*", ""),
                reply_markup=preview_markup
            )

    elif data.startswith("action_proceed_pay_"):
        await query.answer()
        btn_idx = data.replace("action_proceed_pay_", "")

        details = cfg["button_details"].get(btn_idx, {
            "pack": f"VIP Pack {int(btn_idx) + 1}",
            "price": "299",
            "desc": ""
        })

        pack_name = details.get("pack", f"Pack {int(btn_idx) + 1}")
        price = details.get("price", "0")
        txn_id = generate_txn_id(pack_name)

        try:
            amt_val = float("".join(c for c in str(price) if c.isdigit() or c == '.') or "0")
        except ValueError:
            amt_val = 0.0

        log_order(txn_id, chat_id, update.effective_user.username or "", pack_name, amt_val)

        pending_verifications[chat_id] = {
            "txn_id": txn_id,
            "price": price,
            "pack": pack_name
        }

        upi_link = make_upi_uri(
            upi_id=cfg["upi_id"],
            payee_name=cfg["payee_name"],
            amount=price,
            note=txn_id
        )
        qr_url = generate_upi_qr_url(upi_link)

        payment_text = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💎 *Payment*\n\n"
            f"🎀 *{pack_name}*\n"
            f"💰 *Amount : ₹{price}*\n"
            f"🪪 *UPI :* `{cfg['upi_id']}`\n"
            f"🧾 *Txn :* `{txn_id}`\n\n"
            f"Scan the QR or copy the UPI ID above and pay the exact amount. "
            f"Then tap 📸 Send Payment Screenshot and upload the payment receipt.\n\n"
            f"📲 [Open UPI App]({upi_link})\n"
            f"━━━━━━━━━━━━━━━━━━"
        )

        payment_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📸 Send Payment Screenshot",
                    callback_data=f"btn_send_ss_{btn_idx}"
                )
            ],
            [
                InlineKeyboardButton(
                    DEFAULT_BACK_BUTTON,
                    callback_data=f"btn_cat_{btn_idx}"
                )
            ]
        ])

        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=qr_url,
                caption=payment_text,
                reply_markup=payment_markup,
                parse_mode="Markdown"
            )
        except Exception:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=qr_url,
                caption=payment_text.replace("*", "").replace("`", ""),
                reply_markup=payment_markup
            )

    elif data.startswith("btn_send_ss_"):
        await query.answer()
        session = pending_verifications.get(chat_id)
        if not session:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Session expired. Please choose a package again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(DEFAULT_BACK_BUTTON, callback_data="btn_home")
                ]])
            )
            return

        prompt_msg = (
            f"💎 *Send Payment Screenshot*\n\n"
            f"🧾 `{session['txn_id']}`\n"
            f"💰 *₹{session['price']}*\n\n"
            f"Upload the screenshot of your successful payment here as a photo.\n\n"
            f"Send /cancel to abort."
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=prompt_msg,
                parse_mode="Markdown"
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=prompt_msg.replace("*", "").replace("`", "")
            )

# ==========================================
# INCOMING MEDIA & PROOF ROUTER
# ==========================================
async def handle_incoming_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = update.effective_chat.id

    if chat_id in active_admin_uploads and is_admin(chat_id):
        btn_idx = active_admin_uploads[chat_id]
        f_id = None
        m_type = "file"

        if msg.video:
            f_id = msg.video.file_id
            m_type = "Video"
        elif msg.photo:
            f_id = msg.photo[-1].file_id
            m_type = "Photo"
        elif msg.document:
            f_id = msg.document.file_id
            m_type = "Document"
        elif msg.animation:
            f_id = msg.animation.file_id
            m_type = "GIF"

        if f_id:
            cfg = get_settings()
            vids = cfg["button_videos"]
            if str(btn_idx) not in vids:
                vids[str(btn_idx)] = []
            
            vids[str(btn_idx)].append(f_id)
            update_field("button_videos_json", json.dumps(vids))

            total = len(vids[str(btn_idx)])
            await msg.reply_text(f"📥 Saved **{m_type}** to Button #{btn_idx + 1} (Total: {total}).\nSend next or `/done` to finish.", parse_mode="Markdown")
            return

    session = pending_verifications.get(chat_id)
    if session and msg.photo:
        photo_file = msg.photo[-1]
        cfg = get_settings()

        await update.message.reply_text(
            f"✅ *Screenshot Received!*\n\n"
            f"🧾 *Txn:* `{session['txn_id']}`\n"
            f"📦 *Pack:* {session['pack']}\n\n"
            f"Your transaction is being verified by admin. You will receive your delivery link here shortly.",
            parse_mode="Markdown"
        )

        if cfg["admin_chat_id"]:
            try:
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("SELECT id FROM orders WHERE txn_id = ?", (session['txn_id'],))
                row = c.fetchone()
                order_id = row[0] if row else 0
                conn.close()

                admin_caption = (
                    f"🚨 *New Payment Proof Received!*\n\n"
                    f"🆔 *Order:* #{order_id}\n"
                    f"👤 *User:* @{update.effective_user.username or 'N/A'} (`{chat_id}`)\n"
                    f"📦 *Pack:* {session['pack']}\n"
                    f"💰 *Amount:* ₹{session['price']}\n"
                    f"🧾 *Txn ID:* `{session['txn_id']}`"
                )

                admin_markup = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Accept (Paid)", callback_data=f"adm_ord:{order_id}:accept"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"adm_ord:{order_id}:reject")
                    ]
                ])

                await context.bot.send_photo(
                    chat_id=int(cfg["admin_chat_id"]),
                    photo=photo_file.file_id,
                    caption=admin_caption,
                    reply_markup=admin_markup,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed forwarding proof to admin: {e}")

        del pending_verifications[chat_id]
        return

    if is_admin(chat_id):
        f_id = None
        if msg.video: f_id = msg.video.file_id
        elif msg.photo: f_id = msg.photo[-1].file_id
        elif msg.document: f_id = msg.document.file_id
        if f_id:
            await msg.reply_text(f"📹 **File ID:**\n`{f_id}`\n\n💡 Tip: Use `/upload <1-{len(get_settings()['buttons'])}>` to assign media automatically.", parse_mode="Markdown")

# ==========================================
# FASTAPI LIFESPAN & AUTHENTICATION
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    if cfg.get("token"):
        await bot_manager.restart(cfg["token"])
    yield
    await bot_manager.stop()

app = FastAPI(lifespan=lifespan)

def is_authenticated(request: Request) -> bool:
    return request.cookies.get(AUTH_COOKIE_NAME) == AUTH_SECRET

# ==========================================
# FASTAPI ROUTES & WEB ADMIN TEMPLATES
# ==========================================
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    err_html = f'<div class="p-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-mono">{error}</div>' if error else ''

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login &mdash; Nagato Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700;800;900&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body {{ font-family: 'Plus Jakarta Sans', sans-serif; background-color: #030108; }}
        .font-tech {{ font-family: 'Orbitron', monospace; }}
        .exact-login-card {{
            background: linear-gradient(180deg, rgba(16, 12, 34, 0.94) 0%, rgba(10, 8, 22, 0.96) 100%);
            border: 1px solid rgba(168, 85, 247, 0.5);
            box-shadow: 0 0 28px rgba(168, 85, 247, 0.35), 0 0 70px rgba(168, 85, 247, 0.15);
            border-radius: 26px;
        }}
        .custom-input {{
            background-color: #080613;
            border: 1px solid rgba(147, 51, 234, 0.25);
            transition: all 0.2s ease;
        }}
        .custom-input:focus {{
            outline: none;
            border-color: #38bdf8;
            box-shadow: 0 0 12px rgba(56, 189, 248, 0.3);
        }}
    </style>
</head>
<body class="text-slate-100 min-h-screen flex items-center justify-center p-4 relative overflow-hidden">
    <div class="w-full max-w-[370px] relative z-10">
        <div class="exact-login-card p-8 space-y-6">
            <div class="space-y-1">
                <div class="flex items-center gap-2.5">
                    <span class="text-xl">🚀</span>
                    <h1 class="text-xl font-bold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-purple-300 via-fuchsia-300 to-cyan-300">
                        Nagato Panel
                    </h1>
                </div>
                <div class="font-tech text-[10px] tracking-[0.25em] text-cyan-400/90 font-bold uppercase pl-7">
                    ADMIN PANEL
                </div>
            </div>

            {err_html}

            <form method="POST" action="/login" class="space-y-4 pt-1">
                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-2">Username</label>
                    <input type="text" name="username" required autofocus
                           class="custom-input w-full h-11 rounded-xl px-4 text-sm text-white">
                </div>

                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-2">Password</label>
                    <input type="password" name="password" required
                           class="custom-input w-full h-11 rounded-xl px-4 text-sm text-white">
                </div>

                <button type="submit"
                        class="w-full h-11 mt-3 bg-gradient-to-r from-purple-500 via-fuchsia-500 to-cyan-400 hover:opacity-95 text-white font-semibold rounded-xl text-sm transition shadow-lg shadow-purple-600/30">
                    Sign in &rarr;
                </button>
            </form>

            <div class="pt-2 text-center">
                <a href="https://t.me/NAGATOxOWNER" target="_blank" rel="noopener noreferrer"
                   class="inline-flex items-center gap-1.5 text-xs text-cyan-400/80 hover:text-cyan-300 transition font-mono">
                    ✈️ Contact Developer
                </a>
            </div>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)

@app.post("/login")
async def process_login(username: str = Form(...), password: str = Form(...)):
    cfg = get_settings()
    if username == cfg.get("admin_user") and password == cfg.get("admin_pass"):
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key=AUTH_COOKIE_NAME, value=AUTH_SECRET, httponly=True, max_age=86400 * 7)
        return response
    return RedirectResponse(url="/login?error=Invalid+credentials", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/logout")
async def logout_admin():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=AUTH_COOKIE_NAME)
    return response

# --- Main Cyberpunk Admin Dashboard View ---
@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_view(request: Request, message: str | None = None):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    cfg = get_settings()
    total_users, paid_orders, revenue, recent_orders = get_stats()
    all_orders = get_all_orders()

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id, username, joined_at, premium_status, is_banned FROM users ORDER BY joined_at DESC")
    users_list = c.fetchall()
    conn.close()

    msg_html = f'''<div class="p-4 rounded-xl bg-cyan-500/10 border border-cyan-500/40 text-cyan-300 text-xs font-mono flex items-center gap-2 shadow-[0_0_15px_rgba(0,240,255,0.15)]">
        <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
        <span>{message}</span>
    </div>''' if message else ''

    recent_rows = ""
    for txn_id, cid, uname, item, amt, dt, st, oid in recent_orders:
        badge = '<span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-emerald-500/10 border border-emerald-500/40 text-emerald-400">PAID</span>' if st == 'Paid' else ('<span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-amber-500/10 border border-amber-500/40 text-amber-400">PENDING</span>' if st == 'Pending' else '<span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-rose-500/10 border border-rose-500/40 text-rose-400">REJECTED</span>')
        recent_rows += f"""<tr class="hover:bg-purple-900/20 transition">
            <td class="py-3 px-3 text-cyan-400">{txn_id}</td>
            <td class="py-3 px-3">{cid}<span class="block text-[10px] text-purple-400">@{uname}</span></td>
            <td class="py-3 px-3 text-white font-sans">{item}</td>
            <td class="py-3 px-3 font-semibold text-white">₹{amt:.2f}</td>
            <td class="py-3 px-3 text-right">{badge}</td>
        </tr>"""
    if not recent_rows:
        recent_rows = '<tr><td colspan="5" class="py-8 text-center text-purple-400 text-xs">No orders recorded yet.</td></tr>'

    all_rows = ""
    for txn_id, cid, uname, item, amt, dt, st, oid in all_orders:
        badge = '<span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-emerald-500/10 border border-emerald-500/40 text-emerald-400">PAID</span>' if st == 'Paid' else ('<span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-amber-500/10 border border-amber-500/40 text-amber-400">PENDING</span>' if st == 'Pending' else '<span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-rose-500/10 border border-rose-500/40 text-rose-400">REJECTED</span>')
        all_rows += f"""<tr class="hover:bg-purple-900/20 transition">
            <td class="py-3 px-3 text-cyan-400">{txn_id}</td>
            <td class="py-3 px-3">{cid}<span class="block text-[10px] text-purple-400">@{uname}</span></td>
            <td class="py-3 px-3 text-white font-sans">{item}</td>
            <td class="py-3 px-3 font-semibold text-white">₹{amt:.2f}</td>
            <td class="py-3 px-3 text-purple-300/80 text-[11px]">{dt}</td>
            <td class="py-3 px-3">{badge}</td>
            <td class="py-3 px-3 text-right whitespace-nowrap">
                <a href="/admin/order/update/{oid}/Paid" class="bg-emerald-500/10 border border-emerald-500/40 hover:bg-emerald-500 text-emerald-400 hover:text-white px-2.5 py-1 rounded-lg text-[10px] font-tech transition mr-1">Paid</a>
                <a href="/admin/order/update/{oid}/Rejected" class="bg-rose-500/10 border border-rose-500/40 hover:bg-rose-500 text-rose-400 hover:text-white px-2.5 py-1 rounded-lg text-[10px] font-tech transition">Reject</a>
            </td>
        </tr>"""
    if not all_rows:
        all_rows = '<tr><td colspan="7" class="py-12 text-center text-purple-400 text-xs">No orders recorded in database.</td></tr>'

    users_rows = ""
    for u_id, u_uname, u_joined, u_status, u_banned in users_list:
        uname_display = f'<a href="https://t.me/{u_uname}" target="_blank" class="text-fuchsia-400 hover:underline">@{u_uname}</a>' if u_uname != 'N/A' else '<span class="text-purple-400/60">None</span>'
        
        plan_options = f'<option value="Free" {"selected" if u_status == "Free" else ""}>Free Tier</option>'
        for idx, btn_name in enumerate(cfg["buttons"]):
            p_val = cfg["button_details"].get(str(idx), {}).get("pack", btn_name)
            is_sel = "selected" if u_status == p_val else ""
            plan_options += f'<option value="{p_val}" {is_sel}>{p_val}</option>'

        ban_btn = '<button type="submit" class="bg-emerald-500/10 border border-emerald-500/40 hover:bg-emerald-500 text-emerald-400 hover:text-white px-2.5 py-1 rounded-lg text-[10px] font-tech transition">UNBAN</button>' if u_banned else '<button type="submit" class="bg-rose-500/10 border border-rose-500/40 hover:bg-rose-500 text-rose-400 hover:text-white px-2.5 py-1 rounded-lg text-[10px] font-tech transition">BAN</button>'
        ban_val = 0 if u_banned else 1

        users_rows += f"""<tr class="hover:bg-purple-900/20 transition">
            <td class="py-3 px-3 text-cyan-400 font-mono">{u_id}</td>
            <td class="py-3 px-3">{uname_display}</td>
            <td class="py-3 px-3 text-purple-300/80 text-[11px]">{u_joined}</td>
            <td class="py-3 px-3">
                <form method="POST" action="/admin/users/subscription" class="flex items-center gap-1.5">
                    <input type="hidden" name="chat_id" value="{u_id}">
                    <select name="plan_name" class="bg-[#070410] border border-purple-900/60 rounded-lg px-2 py-1 text-[11px] text-white focus:outline-none focus:border-cyan-400">
                        {plan_options}
                    </select>
                    <button type="submit" class="bg-purple-900/60 hover:bg-cyan-600 text-white px-2 py-1 rounded-lg text-[10px] font-tech transition">SET</button>
                </form>
            </td>
            <td class="py-3 px-3 text-right">
                <form method="POST" action="/admin/users/ban">
                    <input type="hidden" name="chat_id" value="{u_id}">
                    <input type="hidden" name="status" value="{ban_val}">
                    {ban_btn}
                </form>
            </td>
        </tr>"""
    if not users_rows:
        users_rows = '<tr><td colspan="5" class="py-12 text-center text-purple-400 text-xs">No registered users in database yet.</td></tr>'

    pack_cards_html = ""
    num_buttons = len(cfg["buttons"])
    for idx in range(num_buttons):
        b_name = cfg["buttons"][idx]
        v_list = cfg["button_videos"].get(str(idx), [])
        details = cfg["button_details"].get(str(idx), {
            "pack": f"VIP Pack {idx + 1}",
            "price": "299",
            "desc": "Full HD streaming bundle.",
            "link": ""
        })
        v_text = "\n".join(v_list)
        p_name = details.get("pack", "")
        price = details.get("price", "299")
        desc = details.get("desc", "")
        delivery_link = details.get("link", "")

        pack_cards_html += f"""<div class="glass-card rounded-2xl p-4 flex flex-col justify-between space-y-3 border border-purple-900/40">
            <div class="flex items-center justify-between border-b border-purple-900/30 pb-2">
                <span class="text-xs font-tech text-cyan-400 font-bold">🟢 BUTTON #{idx + 1}</span>
                <div class="flex items-center gap-2">
                    <span class="text-[10px] font-mono text-purple-400">{len(v_list)} items</span>
                    <form method="POST" action="/admin/plans/delete-button" onsubmit="return confirm('Delete Button #{idx + 1}?');" style="display:inline;">
                        <input type="hidden" name="btn_index" value="{idx}">
                        <button type="submit" class="text-[10px] font-mono text-rose-400 hover:text-rose-300 bg-rose-500/10 border border-rose-500/30 px-2 py-0.5 rounded transition">🗑️ Delete</button>
                    </form>
                </div>
            </div>
            <div class="space-y-2 text-xs">
                <div>
                    <label class="block text-[10px] font-mono text-purple-300 uppercase">Button Label</label>
                    <input type="text" name="btn_label_{idx}" value="{b_name}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white focus:outline-none focus:border-emerald-400 font-medium">
                </div>
                <div class="grid grid-cols-2 gap-2">
                    <div>
                        <label class="block text-[10px] font-mono text-purple-300 uppercase">Pack Name</label>
                        <input type="text" name="pack_name_{idx}" value="{p_name}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white">
                    </div>
                    <div>
                        <label class="block text-[10px] font-mono text-purple-300 uppercase">Price (₹)</label>
                        <input type="text" name="pack_price_{idx}" value="{price}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white">
                    </div>
                </div>
                <div>
                    <label class="block text-[10px] font-mono text-purple-300 uppercase">Description (Preview Card)</label>
                    <textarea name="pack_desc_{idx}" rows="2" class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white text-[11px]">{desc}</textarea>
                </div>
                <div>
                    <label class="block text-[10px] font-mono text-cyan-300 uppercase font-semibold">🔗 Access/Delivery Link (Sent Upon Paid)</label>
                    <input type="url" name="pack_link_{idx}" value="{delivery_link}" placeholder="https://t.me/+joinlink..." class="w-full bg-[#070410] border border-cyan-500/40 rounded-lg px-2.5 py-1.5 text-cyan-300 text-xs focus:outline-none focus:border-cyan-400">
                </div>
                <div>
                    <label class="block text-[10px] font-mono text-purple-300 uppercase">Stored Media (file_id or URL)</label>
                    <textarea name="btn_videos_{idx}" rows="2" placeholder="One per line" class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white text-[10px] font-mono">{v_text}</textarea>
                </div>
                <div class="pt-1">
                    <label class="block text-[10px] font-mono text-purple-300 uppercase mb-1">📁 Upload Video/Image File(s)</label>
                    <input type="file" name="files_{idx}" multiple accept="video/*,image/*" class="w-full text-[10px] text-slate-400 file:mr-2 file:py-1 file:px-2.5 file:rounded-md file:border-0 file:text-[10px] file:font-semibold file:bg-purple-900/60 file:text-cyan-300 hover:file:bg-purple-800 cursor-pointer">
                </div>
            </div>
        </div>"""

    is_online = (bot_manager.status == "Running" and bool(cfg["token"]))
    status_pulse = 'bg-cyan-400' if is_online else 'bg-rose-500'
    status_text = 'ONLINE' if is_online else 'OFFLINE'
    welcome_images_text = "\n".join(cfg["welcome_images"])

    dashboard_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard &mdash; Nagato Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body {{ font-family: 'Plus Jakarta Sans', sans-serif; background-color: #06040c; }}
        .font-tech {{ font-family: 'Orbitron', monospace; }}
        .glass-card {{
            background: linear-gradient(135deg, rgba(22, 12, 42, 0.75) 0%, rgba(13, 8, 25, 0.85) 100%);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(139, 92, 246, 0.25);
        }}
        .neon-border-pink {{
            box-shadow: 0 0 15px rgba(255, 0, 127, 0.2), inset 0 0 15px rgba(255, 0, 127, 0.05);
            border-color: rgba(255, 0, 127, 0.4);
        }}
    </style>
</head>
<body class="text-slate-100 min-h-screen flex overflow-x-hidden relative">
    <div id="sidebarBackdrop" onclick="toggleSidebar()" class="fixed inset-0 bg-black/70 z-30 backdrop-blur-sm hidden md:hidden"></div>

    <aside id="sidebar" class="fixed inset-y-0 left-0 z-40 w-64 bg-[#090614] border-r border-purple-900/40 p-5 flex flex-col justify-between -translate-x-full md:translate-x-0 transition-transform duration-200 ease-in-out md:static md:h-screen">
        <div class="space-y-6">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-2.5">
                    <span class="text-lg">🚀</span>
                    <div>
                        <span class="font-tech font-bold text-sm text-transparent bg-clip-text bg-gradient-to-r from-fuchsia-400 to-cyan-300 tracking-wider block">Nagato Panel</span>
                        <span class="font-tech text-[10px] tracking-wider text-cyan-300 uppercase block">Control Panel</span>
                    </div>
                </div>
                <button onclick="toggleSidebar()" class="md:hidden text-purple-400 hover:text-white p-1">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                </button>
            </div>

            <nav class="space-y-1 text-xs">
                <button onclick="switchTab('tab-dashboard')" id="nav-tab-dashboard" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-semibold bg-purple-900/40 border border-fuchsia-500/30 text-cyan-400 transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>
                    Dashboard
                </button>
                <button onclick="switchTab('tab-orders')" id="nav-tab-orders" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/></svg>
                    Customer Orders
                </button>
                <button onclick="switchTab('tab-users')" id="nav-tab-users" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"/></svg>
                    Manage Users
                </button>
                <button onclick="switchTab('tab-broadcast')" id="nav-tab-broadcast" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5.882V19.24a1.76 1.76 0 01-3.417.592l-2.147-6.15M18 13a3 3 0 100-6M5.436 13.683A4.001 4.001 0 017 6h1.832c4.1 0 7.625-1.234 9.168-3v14c-1.543-1.766-5.067-3-9.168-3H7a3.988 3.988 0 01-1.564-.317z"/></svg>
                    Broadcast
                </button>
                <button onclick="switchTab('tab-settings')" id="nav-tab-settings" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                    Setting &amp; UPI
                </button>
                <button onclick="switchTab('tab-bot')" id="nav-tab-bot" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/></svg>
                    Bot Token Config
                </button>
                <button onclick="switchTab('tab-media')" id="nav-tab-media" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
                    Media &amp; Greetings
                </button>
                <button onclick="switchTab('tab-plans')" id="nav-tab-plans" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                    Plan Buttons
                </button>
            </nav>
        </div>

        <div class="pt-4 border-t border-purple-900/40">
            <a href="/logout" class="w-full flex items-center justify-center gap-2 py-2 rounded-xl text-xs font-mono font-semibold text-rose-400 hover:bg-rose-500/10 transition">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/></svg>
                Sign Out
            </a>
        </div>
    </aside>

    <main class="flex-1 flex flex-col min-w-0 h-screen overflow-y-auto relative z-10">
        <header class="sticky top-0 z-20 bg-[#06040c]/90 backdrop-blur-md border-b border-purple-900/40 px-4 md:px-8 py-3.5 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <button onclick="toggleSidebar()" class="p-2 rounded-lg bg-[#120b22] border border-purple-900/40 text-purple-300 hover:text-white md:hidden">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/></svg>
                </button>
                <h2 id="sectionTitle" class="font-tech text-base md:text-lg font-bold text-white tracking-wider">Dashboard</h2>
            </div>
            
            <div class="flex items-center gap-3">
                <span class="text-xs text-purple-300 hidden sm:inline font-mono">Nagato</span>
                <span class="flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#120b22] border border-cyan-500/40 text-xs font-mono text-cyan-300 shadow-[0_0_10px_rgba(0,240,255,0.2)]">
                    <span class="w-2 h-2 rounded-full {status_pulse} animate-pulse inline-block"></span>
                    {status_text}
                </span>
            </div>
        </header>

        <div class="p-4 md:p-8 max-w-5xl w-full mx-auto space-y-6">
            {msg_html}

            <!-- TAB: DASHBOARD -->
            <div id="tab-dashboard" class="tab-content space-y-6">
                <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4">
                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between hover:border-cyan-500/40 transition">
                        <span class="font-tech text-2xl md:text-3xl font-extrabold text-white">{paid_orders}</span>
                        <p class="text-[11px] text-purple-300/80 font-mono mt-0.5 uppercase">Paid orders</p>
                    </div>

                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between hover:border-fuchsia-500/40 transition">
                        <div class="flex justify-between items-center">
                            <span class="font-tech text-2xl md:text-3xl font-extrabold text-white">&#8377;{revenue:.2f}</span>
                            <form method="POST" action="/admin/revenue/reset" onsubmit="return confirm('Reset all revenue counters?');">
                                <button type="submit" class="text-[10px] text-purple-400 hover:text-fuchsia-300 font-mono">Reset</button>
                            </form>
                        </div>
                        <p class="text-[11px] text-purple-300/80 font-mono mt-0.5 uppercase">Revenue</p>
                    </div>

                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between hover:border-purple-500/40 transition">
                        <span class="font-tech text-2xl md:text-3xl font-extrabold text-white">{total_users}</span>
                        <p class="text-[11px] text-purple-300/80 font-mono mt-0.5 uppercase">Total users</p>
                    </div>

                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between neon-border-pink">
                        <span id="countdownTimer" class="font-tech text-lg md:text-xl font-black text-transparent bg-clip-text bg-gradient-to-r from-fuchsia-400 to-cyan-300">
                            Calculating...
                        </span>
                        <p class="text-[11px] text-purple-400 mt-1 font-mono">Valid till: <span class="text-slate-300">{cfg['license_expiry']}</span></p>
                    </div>
                </div>

                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <div class="flex justify-between items-center border-b border-purple-900/40 pb-3">
                        <h3 class="font-tech text-base font-bold text-white tracking-wide">Recent orders</h3>
                        <button onclick="switchTab('tab-orders')" class="text-xs text-fuchsia-400 hover:text-fuchsia-300 font-mono">View All Orders &rarr;</button>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="py-3 px-3">Code</th>
                                    <th class="py-3 px-3">UID</th>
                                    <th class="py-3 px-3">Item</th>
                                    <th class="py-3 px-3">Amount</th>
                                    <th class="py-3 px-3 text-right">Status</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30 text-slate-300">
                                {recent_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB: CUSTOMER ORDERS -->
            <div id="tab-orders" class="tab-content space-y-6 hidden">
                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <h2 class="font-tech text-base font-bold text-white tracking-wide border-b border-purple-900/40 pb-3">Customer Orders</h2>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="py-3 px-3">Order Code</th>
                                    <th class="py-3 px-3">Telegram User</th>
                                    <th class="py-3 px-3">Subscription Item</th>
                                    <th class="py-3 px-3">Amount</th>
                                    <th class="py-3 px-3">Timestamp</th>
                                    <th class="py-3 px-3">Status</th>
                                    <th class="py-3 px-3 text-right">Actions</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30 text-slate-300">
                                {all_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB: MANAGE USERS -->
            <div id="tab-users" class="tab-content space-y-6 hidden">
                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white tracking-wide border-b border-purple-900/40 pb-3">Manage Registered Users</h3>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="py-3 px-3">User ID</th>
                                    <th class="py-3 px-3">Username</th>
                                    <th class="py-3 px-3">Joined Date</th>
                                    <th class="py-3 px-3">Manage Subscription</th>
                                    <th class="py-3 px-3 text-right">Ban Status</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30 text-slate-300">
                                {users_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB: BROADCAST -->
            <div id="tab-broadcast" class="tab-content space-y-6 hidden">
                <form method="POST" action="/admin/broadcast/send" class="glass-card rounded-2xl p-6 space-y-5">
                    <h3 class="font-tech text-base font-bold text-white tracking-wide border-b border-purple-900/40 pb-3">Mass Announcement Broadcast</h3>
                    <div class="space-y-4">
                        <div>
                            <label class="block text-xs font-semibold text-purple-300 uppercase tracking-wider mb-2 font-mono">Broadcast Text *</label>
                            <textarea name="broadcast_message" rows="4" required placeholder="Type announcement message..."
                                      class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-400"></textarea>
                        </div>
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <div>
                                <label class="block text-xs font-semibold text-purple-300 uppercase tracking-wider mb-2 font-mono">Photo URL or file_id (Optional)</label>
                                <input type="text" name="broadcast_photo" placeholder="https://... or file_id"
                                       class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-cyan-400">
                            </div>
                            <div>
                                <label class="block text-xs font-semibold text-purple-300 uppercase tracking-wider mb-2 font-mono">Video URL or file_id (Optional)</label>
                                <input type="text" name="broadcast_video" placeholder="https://... or file_id"
                                       class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-cyan-400">
                            </div>
                        </div>
                    </div>
                    <button type="submit" onclick="return confirm('Send mass broadcast to all registered users?');"
                            class="bg-gradient-to-r from-fuchsia-600 to-purple-600 hover:from-fuchsia-500 hover:to-purple-500 text-white font-tech font-bold px-6 py-2.5 rounded-xl text-xs uppercase tracking-wider shadow-lg shadow-fuchsia-600/30 transition">
                        📢 Transmit Broadcast
                    </button>
                </form>
            </div>

            <!-- TAB: SETTINGS & UPI -->
            <div id="tab-settings" class="tab-content space-y-6 hidden">
                <form method="POST" action="/admin/save-bot-settings" class="glass-card rounded-2xl p-6 space-y-5">
                    <input type="hidden" name="token" value="{cfg['token']}">
                    <h3 class="font-tech text-base font-bold text-white tracking-wide border-b border-purple-900/40 pb-3">UPI Payment Configuration</h3>
                    <div class="space-y-4">
                        <div>
                            <label class="block text-xs font-semibold text-purple-300 font-mono uppercase mb-2">UPI ID *</label>
                            <input type="text" name="upi_id" value="{cfg['upi_id']}" required class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white font-mono focus:outline-none focus:border-cyan-400">
                        </div>
                        <div>
                            <label class="block text-xs font-semibold text-purple-300 mb-2 font-mono uppercase">Payee Registered Name</label>
                            <input type="text" name="payee_name" value="{cfg['payee_name']}" required class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-400">
                        </div>
                        <div>
                            <label class="block text-xs font-semibold text-purple-300 mb-2 font-mono uppercase">Admin Telegram Chat ID (For Alerts)</label>
                            <input type="text" name="admin_chat_id" value="{cfg['admin_chat_id']}" placeholder="e.g. 6528792525" class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-2.5 text-sm text-white font-mono focus:outline-none focus:border-cyan-400">
                        </div>
                    </div>
                    <button type="submit" class="bg-gradient-to-r from-fuchsia-600 to-purple-600 text-white font-tech font-bold px-5 py-2.5 rounded-xl text-xs uppercase tracking-wider transition">
                        Save UPI &amp; Payout Settings
                    </button>
                </form>
            </div>

            <!-- TAB: BOT TOKEN CONFIG -->
            <div id="tab-bot" class="tab-content space-y-5 hidden">
                <form method="POST" action="/admin/save-bot-settings" class="glass-card rounded-2xl p-6 space-y-5">
                    <input type="hidden" name="upi_id" value="{cfg['upi_id']}">
                    <input type="hidden" name="payee_name" value="{cfg['payee_name']}">
                    <input type="hidden" name="admin_chat_id" value="{cfg['admin_chat_id']}">
                    <h3 class="font-tech text-base font-bold text-white tracking-wide border-b border-purple-900/40 pb-3">Telegram Bot API Engine</h3>
                    <div>
                        <label class="block text-xs font-semibold text-purple-300 uppercase tracking-wider mb-2 font-mono">Bot Token</label>
                        <input type="text" name="token" value="{cfg['token']}" placeholder="123456:ABC-DEF1234..." required
                               class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white font-mono focus:outline-none focus:border-cyan-400">
                        <p class="text-xs text-purple-400/80 mt-2 font-mono">Validates connection with Telegram and starts polling immediately.</p>
                    </div>
                    <button type="submit" class="bg-gradient-to-r from-cyan-600 to-blue-600 text-white font-tech font-bold px-5 py-2.5 rounded-xl text-xs uppercase tracking-wider transition">
                        Update &amp; Restart Bot
                    </button>
                </form>
            </div>

            <!-- TAB: MEDIA & GREETINGS -->
            <div id="tab-media" class="tab-content space-y-5 hidden">
                <form method="POST" action="/admin/save-message-settings" class="glass-card rounded-2xl p-6 space-y-5">
                    <h3 class="font-tech text-base font-bold text-white tracking-wide border-b border-purple-900/40 pb-3">Interface Templates &amp; Action Buttons</h3>
                    <div>
                        <label class="block text-xs font-semibold text-purple-300 uppercase tracking-wider mb-2 font-mono">Welcome Caption</label>
                        <textarea name="welcome_caption" rows="4" required class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-400">{cfg['welcome_caption']}</textarea>
                    </div>
                    <div>
                        <label class="block text-xs font-semibold text-purple-300 uppercase tracking-wider mb-2 font-mono">Banner Image URLs (One per line)</label>
                        <textarea name="welcome_images" rows="3" class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-400">{welcome_images_text}</textarea>
                    </div>
                    <div class="border-t border-purple-900/40 pt-4 space-y-4">
                        <h4 class="font-tech text-xs font-bold text-cyan-400 tracking-wider">Bottom 3 Action Buttons</h4>
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <div class="p-3 bg-[#070410]/60 border border-purple-900/40 rounded-xl space-y-2">
                                <label class="block text-[11px] text-cyan-300 font-mono">Button 1 (Left)</label>
                                <input type="text" name="btn_how_to_use" value="{cfg['btn_how_to_use']}" required class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-white">
                                <input type="text" name="msg_how_to_use" value="{cfg['msg_how_to_use']}" required class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-slate-300">
                            </div>
                            <div class="p-3 bg-[#070410]/60 border border-purple-900/40 rounded-xl space-y-2">
                                <label class="block text-[11px] text-rose-400 font-mono">Button 2 (Right)</label>
                                <input type="text" name="btn_report_issue" value="{cfg['btn_report_issue']}" required class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-white">
                                <input type="text" name="msg_report_issue" value="{cfg['msg_report_issue']}" required class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-slate-300">
                            </div>
                        </div>
                        <div class="p-3 bg-[#070410]/60 border border-purple-900/40 rounded-xl space-y-2">
                            <label class="block text-[11px] text-cyan-300 font-mono">Button 3 (Full Width)</label>
                            <input type="text" name="btn_language" value="{cfg['btn_language']}" required class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-white">
                            <input type="text" name="msg_language" value="{cfg['msg_language']}" required class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-slate-300">
                        </div>
                    </div>
                    <button type="submit" class="w-full bg-gradient-to-r from-fuchsia-600 via-purple-600 to-cyan-600 text-white font-tech font-bold py-3.5 rounded-xl shadow-lg shadow-fuchsia-500/25 tracking-wider uppercase transition">
                        Save Interface Parameters
                    </button>
                </form>
            </div>

            <!-- TAB: PLAN BUTTONS -->
            <div id="tab-plans" class="tab-content space-y-5 hidden">
                <div class="glass-card rounded-2xl p-5 border border-cyan-500/40">
                    <h3 class="font-tech text-sm text-cyan-300 font-bold tracking-wider mb-3">➕ Add New Plan Button</h3>
                    <form method="POST" action="/admin/plans/add-button" class="grid grid-cols-1 sm:grid-cols-4 gap-3">
                        <div>
                            <label class="block text-[10px] font-mono text-purple-300 uppercase">Button Label</label>
                            <input type="text" name="new_btn_label" required placeholder="e.g. ULTRA VIP" class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-xs text-white">
                        </div>
                        <div>
                            <label class="block text-[10px] font-mono text-purple-300 uppercase">Pack Name</label>
                            <input type="text" name="new_pack_name" required placeholder="e.g. Ultra VIP Pack" class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-xs text-white">
                        </div>
                        <div>
                            <label class="block text-[10px] font-mono text-purple-300 uppercase">Price (₹)</label>
                            <input type="text" name="new_pack_price" required placeholder="e.g. 499" class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-xs text-white">
                        </div>
                        <div class="flex items-end">
                            <button type="submit" class="w-full bg-gradient-to-r from-cyan-600 to-blue-600 hover:from-cyan-500 hover:to-blue-500 text-white font-tech font-bold py-2 rounded-lg text-xs uppercase tracking-wider transition">
                                + Add Button
                            </button>
                        </div>
                    </form>
                </div>

                <form method="POST" action="/admin/save-packs" enctype="multipart/form-data" class="space-y-4">
                    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {pack_cards_html}
                    </div>
                    <button type="submit" class="w-full bg-gradient-to-r from-emerald-600 via-teal-600 to-cyan-600 text-white font-tech font-bold py-3.5 rounded-xl shadow-lg shadow-emerald-500/25 tracking-wider uppercase transition">
                        Save All Plan Buttons &amp; Upload Files
                    </button>
                </form>
            </div>
        </div>
    </main>

    <script>
        function toggleSidebar() {{
            document.getElementById('sidebar').classList.toggle('-translate-x-full');
            document.getElementById('sidebarBackdrop').classList.toggle('hidden');
        }}

        const titles = {{
            'tab-dashboard': 'Dashboard',
            'tab-orders': 'Customer Orders',
            'tab-users': 'Manage Users',
            'tab-broadcast': 'Broadcast',
            'tab-settings': 'Setting & UPI',
            'tab-bot': 'Bot Token Config',
            'tab-media': 'Media & Greetings',
            'tab-plans': 'Plan Buttons'
        }};

        function switchTab(tabId) {{
            document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
            const target = document.getElementById(tabId);
            if (target) target.classList.remove('hidden');

            document.getElementById('sectionTitle').innerText = titles[tabId] || 'Nagato Panel';

            document.querySelectorAll('.nav-btn').forEach(btn => {{
                btn.classList.remove('bg-purple-900/40', 'border', 'border-fuchsia-500/30', 'text-cyan-400');
                btn.classList.add('text-slate-400');
            }});
            const activeNav = document.getElementById('nav-' + tabId);
            if (activeNav) {{
                activeNav.classList.add('bg-purple-900/40', 'border', 'border-fuchsia-500/30', 'text-cyan-400');
                activeNav.classList.remove('text-slate-400');
            }}

            if (window.innerWidth < 768) {{
                const sidebar = document.getElementById('sidebar');
                if (!sidebar.classList.contains('-translate-x-full')) toggleSidebar();
            }}
        }}

        const urlParams = new URLSearchParams(window.location.search);
        const requestedTab = urlParams.get('tab');
        if (requestedTab && titles[requestedTab]) switchTab(requestedTab);

        const expiryDateStr = "{cfg['license_expiry']}";
        const expiryDate = new Date(expiryDateStr.replace(' ', 'T')).getTime();

        function updateCountdown() {{
            const now = new Date().getTime();
            const distance = expiryDate - now;
            const timerEl = document.getElementById("countdownTimer");

            if (!timerEl) return;
            if (isNaN(distance) || distance <= 0) {{
                timerEl.innerText = "EXPIRED";
                timerEl.className = "font-tech text-lg text-rose-400";
                return;
            }}

            const d = Math.floor(distance / (1000 * 60 * 60 * 24));
            const h = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
            const m = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
            const s = Math.floor((distance % (1000 * 60)) / 1000);
            timerEl.innerText = `${{d}}d ${{h}}h ${{m}}m ${{s}}s`;
        }}

        setInterval(updateCountdown, 1000);
        updateCountdown();
    </script>
</body>
</html>"""
    return HTMLResponse(content=dashboard_html)

# ==========================================
# POST ACTION ENDPOINTS
# ==========================================
@app.get("/admin/order/update/{order_id}/{new_status}")
async def change_order_status(order_id: int, new_status: str, request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    if new_status in ["Paid", "Rejected", "Pending"]:
        update_order_status(order_id, new_status)

        if new_status == "Paid" and bot_manager.app:
            order = get_order_by_id(order_id)
            if order:
                txn_id, chat_id, uname, pack_name, amount, _ = order
                cfg = get_settings()
                update_user_subscription(chat_id, pack_name)
                
                delivery_link = ""
                for idx in range(len(cfg["buttons"])):
                    d = cfg["button_details"].get(str(idx), {})
                    if d.get("pack", "").strip() == pack_name.strip():
                        delivery_link = d.get("link", "").strip()
                        break

                approval_text = (
                    f"🎉 *Payment Verified & Approved!*\n\n"
                    f"📦 *Pack:* {pack_name}\n"
                    f"💰 *Amount:* ₹{amount:.2f}\n"
                    f"🧾 *Txn:* `{txn_id}`\n\n"
                    f"Thank you for your purchase! Access your benefits below:"
                )

                reply_markup = None
                if delivery_link:
                    reply_markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🚀 Access Exclusive Content", url=delivery_link)
                    ]])

                try:
                    await bot_manager.app.bot.send_message(
                        chat_id=chat_id,
                        text=approval_text,
                        reply_markup=reply_markup,
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Failed sending approval notification: {e}")

    return RedirectResponse(url="/?tab=tab-orders&message=Order+status+updated", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/users/subscription")
async def handle_user_subscription_change(
    request: Request,
    chat_id: int = Form(...),
    plan_name: str = Form(...),
):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    update_user_subscription(chat_id, plan_name)
    if bot_manager.app:
        try:
            if plan_name == "Free":
                await bot_manager.app.bot.send_message(chat_id, "Your subscription has ended. You are now on the Free tier.")
            else:
                await bot_manager.app.bot.send_message(chat_id, f"🎉 You have been granted active subscription to *{plan_name}*!", parse_mode="Markdown")
        except Exception:
            pass

    return RedirectResponse(url=f"/?tab=tab-users&message=Subscription+updated+for+{chat_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/users/ban")
async def handle_user_ban_toggle(
    request: Request,
    chat_id: int = Form(...),
    status_val: int = Form(..., alias="status"),
):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    set_user_ban(chat_id, status_val)
    action_text = "banned" if status_val == 1 else "unbanned"
    return RedirectResponse(url=f"/?tab=tab-users&message=User+{chat_id}+{action_text}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/plans/add-button")
async def handle_add_plan_button(
    request: Request,
    new_btn_label: str = Form(...),
    new_pack_name: str = Form(...),
    new_pack_price: str = Form(...),
):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    cfg = get_settings()
    buttons = cfg["buttons"]
    button_videos = cfg["button_videos"]
    button_details = cfg["button_details"]

    new_idx = str(len(buttons))
    buttons.append(new_btn_label.strip())
    button_videos[new_idx] = []
    button_details[new_idx] = {
        "pack": new_pack_name.strip(),
        "price": new_pack_price.strip(),
        "desc": "Full VIP pack access.",
        "link": ""
    }

    update_field("buttons_json", json.dumps(buttons))
    update_field("button_videos_json", json.dumps(button_videos))
    update_field("button_details_json", json.dumps(button_details))

    return RedirectResponse(url="/?tab=tab-plans&message=New+plan+button+added+successfully!", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/plans/delete-button")
async def handle_delete_plan_button(
    request: Request,
    btn_index: int = Form(...),
):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    cfg = get_settings()
    buttons = cfg["buttons"]
    button_videos = cfg["button_videos"]
    button_details = cfg["button_details"]

    if 0 <= btn_index < len(buttons):
        final_buttons = [b for i, b in enumerate(buttons) if i != btn_index]
        
        final_videos = {}
        final_details = {}
        curr_new = 0
        for old_i in range(len(buttons)):
            if old_i == btn_index:
                continue
            if str(old_i) in button_videos:
                final_videos[str(curr_new)] = button_videos[str(old_i)]
            if str(old_i) in button_details:
                final_details[str(curr_new)] = button_details[str(old_i)]
            curr_new += 1

        update_field("buttons_json", json.dumps(final_buttons))
        update_field("button_videos_json", json.dumps(final_videos))
        update_field("button_details_json", json.dumps(final_details))

    return RedirectResponse(url="/?tab=tab-plans&message=Plan+button+deleted+successfully!", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/broadcast/send")
async def handle_admin_broadcast(
    request: Request,
    broadcast_message: str = Form(...),
    broadcast_photo: str = Form(""),
    broadcast_video: str = Form(""),
):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    if not bot_manager.app:
        return RedirectResponse(url="/?tab=tab-broadcast&message=Error:+Bot+is+offline", status_code=status.HTTP_303_SEE_OTHER)

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users WHERE is_banned = 0")
    users = c.fetchall()
    conn.close()

    sent = 0
    cleaned_photo = broadcast_photo.strip()
    cleaned_video = broadcast_video.strip()

    for (uid,) in users:
        try:
            if cleaned_video:
                await bot_manager.app.bot.send_video(chat_id=uid, video=cleaned_video, caption=broadcast_message, supports_streaming=True)
            elif cleaned_photo:
                await bot_manager.app.bot.send_photo(chat_id=uid, photo=cleaned_photo, caption=broadcast_message)
            else:
                await bot_manager.app.bot.send_message(chat_id=uid, text=broadcast_message)
            sent += 1
            await asyncio.sleep(0.05)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                if cleaned_video:
                    await bot_manager.app.bot.send_video(chat_id=uid, video=cleaned_video, caption=broadcast_message)
                elif cleaned_photo:
                    await bot_manager.app.bot.send_photo(chat_id=uid, photo=cleaned_photo, caption=broadcast_message)
                else:
                    await bot_manager.app.bot.send_message(chat_id=uid, text=broadcast_message)
                sent += 1
            except Exception:
                pass
        except Exception:
            pass

    return RedirectResponse(url=f"/?tab=tab-broadcast&message=Broadcast+sent+to+{sent}+users", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/save-bot-settings")
async def save_bot_settings(
    request: Request,
    token: str = Form(...),
    upi_id: str = Form(...),
    payee_name: str = Form(...),
    admin_chat_id: str = Form(""),
):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    cleaned_token = token.strip()
    update_field("token", cleaned_token)
    update_field("upi_id", upi_id.strip())
    update_field("payee_name", payee_name.strip())
    update_field("admin_chat_id", admin_chat_id.strip())

    await bot_manager.restart(cleaned_token)
    return RedirectResponse(url="/?tab=tab-settings&message=Settings+and+bot+engine+updated", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/save-message-settings")
async def save_message_settings(
    request: Request,
    welcome_caption: str = Form(...),
    welcome_images: str = Form(""),
    btn_how_to_use: str = Form(...),
    msg_how_to_use: str = Form(...),
    btn_report_issue: str = Form(...),
    msg_report_issue: str = Form(...),
    btn_language: str = Form(...),
    msg_language: str = Form(...),
):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    raw_images = [img.strip() for img in welcome_images.splitlines() if img.strip()]
    update_field("welcome_images_json", json.dumps(raw_images))
    update_field("welcome_caption", welcome_caption.strip())
    update_field("btn_how_to_use", btn_how_to_use.strip())
    update_field("btn_report_issue", btn_report_issue.strip())
    update_field("btn_language", btn_language.strip())
    update_field("msg_how_to_use", msg_how_to_use.strip())
    update_field("msg_report_issue", msg_report_issue.strip())
    update_field("msg_language", msg_language.strip())

    return RedirectResponse(url="/?tab=tab-media&message=Messages+and+actions+saved", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/save-packs")
async def save_packs_settings(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    form = await request.form()
    cfg = get_settings()

    buttons = []
    button_videos = {}
    button_details = {}
    total_btns = len(cfg["buttons"])

    for idx in range(total_btns):
        btn_label = form.get(f"btn_label_{idx}", f"VIP Pack {idx+1}").strip()
        buttons.append(btn_label)

        raw_vids = form.get(f"btn_videos_{idx}", "").splitlines()
        existing_items = [v.strip() for v in raw_vids if v.strip()]

        uploaded_files = form.getlist(f"files_{idx}")
        if uploaded_files and bot_manager.app and cfg["admin_chat_id"]:
            admin_id = int(cfg["admin_chat_id"])
            for f in uploaded_files:
                if hasattr(f, "filename") and f.filename:
                    file_bytes = await f.read()
                    if file_bytes:
                        fname = f.filename.lower()
                        try:
                            if fname.endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv")):
                                sent_msg = await bot_manager.app.bot.send_video(
                                    chat_id=admin_id,
                                    video=file_bytes,
                                    caption=f"Uploaded for {btn_label}",
                                    supports_streaming=True
                                )
                                if sent_msg.video:
                                    existing_items.append(sent_msg.video.file_id)
                            else:
                                sent_msg = await bot_manager.app.bot.send_photo(
                                    chat_id=admin_id,
                                    photo=file_bytes,
                                    caption=f"Uploaded for {btn_label}"
                                )
                                if sent_msg.photo:
                                    existing_items.append(sent_msg.photo[-1].file_id)
                        except Exception as e:
                            logger.error(f"Failed uploading file to Telegram: {e}")

        button_videos[str(idx)] = existing_items

        button_details[str(idx)] = {
            "pack": form.get(f"pack_name_{idx}", f"VIP Pack {idx+1}").strip(),
            "price": form.get(f"pack_price_{idx}", "0").strip(),
            "desc": form.get(f"pack_desc_{idx}", "").strip(),
            "link": form.get(f"pack_link_{idx}", "").strip(),
        }

    update_field("buttons_json", json.dumps(buttons))
    update_field("button_videos_json", json.dumps(button_videos))
    update_field("button_details_json", json.dumps(button_details))

    return RedirectResponse(url="/?tab=tab-plans&message=Plan+buttons+and+media+saved", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/revenue/reset")
async def reset_revenue_stats(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    return RedirectResponse(url="/?tab=tab-dashboard&message=Revenue+counters+reset", status_code=status.HTTP_303_SEE_OTHER)

# ==========================================
# APP ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    # Replace 'main:app' with your file's module name if not using main.py
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)