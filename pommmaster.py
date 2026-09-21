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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
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
DB_NAME = os.path.join(DATA_DIR, "pompom_v2_database.db")

STATIC_DIR = os.path.join(os.getcwd(), "static")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

INITIAL_BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEFAULT_BACK_BUTTON = "🔙 Back"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pending_verifications = {}
active_admin_uploads = {}

# --- Database Setup & Migration ---
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
            {"btn": "CHILD POM 🙈💋",      "pack": "CHILD POM 🙈💋",          "price": "62",   "desc": "𝗖𝗛𝗜𝗟𝗗 𝗣𝗢𝗠 𝗩𝗜𝗗𝗘𝗢𝗦 𝟰𝟱000 𝗩𝗜𝗗𝗘𝗢𝗦 𝗔𝗥𝗘 𝗧𝗛𝗘𝗥𝗘 𝟭𝟬 𝗚𝗥𝗢𝗨𝗣𝗦 𝗢𝗙 𝟰𝟱𝟬𝟬𝟬 𝗩𝗜𝗗𝗘𝗢𝗦 𝟯𝟱𝟬+ 𝗭𝗜𝗣𝗦 𝗢𝗙 𝟱𝟬𝟬𝗚𝗕", "link": "https://t.me/+YourLink1"},
            {"btn": "GAYY MIX",   "pack": "GAYY MIX",  "price": "60",   "desc": "GAY POM VIDEOS 10,000 VIDEOS.", "link": "https://t.me/+YourLink2"},
            {"btn": "CHILD INDIA 😄",    "pack": "CHILD INDIA 😄",      "price": "64",  "desc": "INDIAN CHILD VIDEOS ONLY 150₹ 50,000 CHILD INDIAN.",      "link": "https://t.me/+YourLink3"},
            {"btn": "SMALL SON BIG GIRL 🤩",     "pack": "SMALL SON BIG GIRL 🤩",    "price": "61",  "desc": "SMALL SON BIG GIRL 🤩 FUN BOY HAVING FUN WITH MOMMY PLAYING WITH 🙈.",     "link": "https://t.me/+YourLink4"},
            {"btn": "BRO SIS AUNTY CHILD", "pack": "BRO SIS AUNTY CHILD",     "price": "63",  "desc": "SMALL CHILD WITH BIG AUNTY 💋.",          "link": "https://t.me/+YourLink5"},
            {"btn": "ANIMAL FUN😂", "pack": "ANIMAL FUN😂",     "price": "59",  "desc": "ANIMAL HAVING FUN WITH GIRLS 😂.",   "link": "https://t.me/+YourLink6"},
            {"btn": "IP CAM FAMILY 🤤",     "pack": "IP CAM FAMILY 🤤",  "price": "58",  "desc": "FAMILY CAPTURED IN IP CAM HAVING FUN 💋🔥.",       "link": "https://t.me/+YourLink7"},
            {"btn": "HOUSEWIFE 😱",   "pack": "HOUSEWIFE 😱",   "price": "49",  "desc": "HOUSEWIFE IN HOME WITH HUSBAND OR HIS BROTHER.", "link": "https://t.me/+YourLink8"},
            {"btn": "MOM AND PAPA 😜 UNCLE AUNTY 😍",      "pack": "MOM AND PAPA 😜 UNCLE AUNTY 😍",   "price": "50",  "desc": "UNCLE AND AUNTY ENJOYING 😁.",       "link": "https://t.me/+YourLink9"},
            {"btn": "PILLS",   "pack": "PILLS",          "price": "65",  "desc": "GIVING PILLS TO GIRL AND HAVING FUN 🌽.", "link": "https://t.me/+YourLink10"},
            {"btn": "SPA LEAK 🙈",     "pack": "SPA LEAK 🙈",     "price": "66",  "desc": "LEAK VIDEOS INSIDE SPA 🎬.", "link": "https://t.me/+YourLink11"},
            {"btn": "SNAP LEAK 😲",    "pack": "SNAP LEAK 😲",         "price": "67",  "desc": "BEUTIFUL GIRLS SNAP GOT LEAKED 🤩.", "link": "https://t.me/+YourLink12"},
            {"btn": "INSTA LEAK 😜💋",  "pack": "INSTA LEAK 😜💋",     "price": "69",  "desc": "GIRLS VIDEO GOT LEAKED IN INSTAGRAM 💎.",               "link": "https://t.me/+YourLink13"},
            {"btn": "CP 35K 😋🤩", "pack": "CP 35K 😋🤩",       "price": "150",  "desc": "35,000+ PREMIUM CP COLLECTION 😋.", "link": "https://t.me/+YourLink14"},
            {"btn": "350 ZIPS CP", "pack": "350 ZIPS CP",     "price": "70",  "desc": "350+ PREMIUM CP ZIPS 💋.",      "link": "https://t.me/+YourLink15"},
            {"btn": "SABSE SASTA",     "pack": "SABSE SASTA",  "price": "25", "desc": "SABSE SASTA AND SABSE ACCHA MAAL 😁.",        "link": "https://t.me/+YourLink16"},
            {"btn": "300 ADULT GROUPS",    "pack": "300 ADULT GROUPS",      "price": "150", "desc": "300 ADULT GROUPS ONLYY.",      "link": "https://t.me/+YourLink17"},
        ]

        default_buttons = [item["btn"] for item in default_packs_config]
        default_images = [
            "https://img.sanishtech.com/u/8bb2886fa8263c48500c00cfb26e0b36.jpg",
            "https://img.sanishtech.com/u/bd876a56e90b36cf4d2a05dfc5fa8dc1.jpg"
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
            "🌟 🔥 𝐏𝐑𝐄𝐌𝐈𝐔𝐌 𝐀𝐃𝐔𝐋𝐓 𝐂𝐎𝐋𝐋𝐄𝐂𝐓𝐈𝐎𝐍 𝐔𝐍𝐋𝐎𝐂𝐊𝐄𝐃 🔥\n\n𝐇𝐃 + 𝐔𝐥𝐭𝐫𝐚-𝐅𝐫𝐞𝐬𝐡 𝐕𝐢𝐝𝐞𝐨𝐬 𝐀𝐯𝐚𝐢𝐥𝐚𝐛𝐥e 𝐍𝐨𝐰\n\n💦 𝐌𝐨𝐦-𝐒𝐨𝐧 𝐅𝐚𝐧𝐭𝐚𝐬𝐲\n💦 𝐁𝐫𝐨𝐭𝐡𝐞𝐫-𝐒𝐢𝐬𝐭e𝐫 𝐓𝐚𝐛𝐨𝐨\n💦 𝐀𝐮𝐧𝐭𝐲 & 𝐁𝐡𝐚𝐛𝐡𝐢 𝐃𝐞𝐬𝐢 𝐇𝐨𝐭\n💦 𝐓eеn 𝐈𝐧𝐝𝐢𝐚𝐧 (𝟏𝟖+)\n💦 𝐈𝐧𝐬𝐭𝐚𝐠𝐫𝐚𝐦 𝐑𝐞e𝐥𝐬 𝐒𝐭𝐚𝐫𝐬\n💦 𝐃𝐞𝐬𝐢 𝐁𝐡𝐚𝐛𝐡𝐢 & 𝐀𝐮𝐧𝐭𝐲 𝐒e𝐫𝐢e𝐬\n💦 𝐅𝐨𝐫e𝐢𝐠𝐧e𝐫 & 𝐈𝐧𝐭e𝐫𝐧𝐚𝐭𝐢𝐨𝐧𝐚𝐥\n💦 𝐇𝐚𝐫𝐝𝐜𝐨𝐫e & 𝐑𝐨𝐥e𝐩𝐥𝐚𝐲 𝐂𝐨𝐥𝐥e𝐜𝐭𝐢𝐨𝐧\n🫦 𝐂𝐇*𝐋𝐃-𝐏*𝐑𝐍🫦💦\n🫦 SLEEPING *🫦💦 \n\n𝐀𝐥𝐥 𝐂𝐚𝐭e𝐠𝐨𝐫𝐢e𝐬 𝐢𝐧 𝐎𝐧e 𝐏𝐚𝐜𝐤𝐚𝐠e\n\n✅ 𝐅𝐮𝐥𝐥⚡ 𝐇𝐃  𝐐𝐮𝐚𝐥𝐢𝐭𝐲\n✅ 𝐈𝐧𝐬𝐭𝐚𝐧𝐭 𝐃e𝐥𝐢𝐯e𝐫y\n✅ 𝟏𝟎𝟎% 𝐖𝐨𝐫𝐤𝐢𝐧𝐠 & 𝐔𝐩𝐝𝐚𝐭e𝐝 𝐋𝐢𝐧𝐤𝐬\n\n𝐋𝐚𝐬𝐭 𝐟e𝐰 𝐬𝐥𝐨𝐭𝐬 𝐚𝐭 59₹ → 𝐃𝐨𝐧’𝐭 𝐦𝐢𝐬𝐬 𝐢𝐭!\n\n👇 𝐁𝐔𝐘 𝐏𝐑𝐄𝐌𝐈𝐔𝐌 👇\n\n@GENOS_JOD\n@GENOS_JOD",
            json.dumps(default_buttons),
            json.dumps(default_button_videos),
            json.dumps(default_details),
            "YOUR_UPI",
            "YOUR_UPI_NAME",
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

# --- Dynamic UPI Helpers ---
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
# ROBUST BOT LIFECYCLE MANAGER (ALWAYS ONLINE)
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

# --- Keyboard Builders ---
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
            callback_data=f"btn_cat_{idx}",
            api_kwargs={"style": "success"}
        )
        keyboard.append([btn])

    row_actions = [
        InlineKeyboardButton(
            text=cfg["btn_how_to_use"],
            callback_data="act_how_to_use",
            api_kwargs={"style": "primary"}
        ),
        InlineKeyboardButton(
            text=cfg["btn_report_issue"],
            callback_data="act_report_issue",
            api_kwargs={"style": "danger"}
        )
    ]
    keyboard.append(row_actions)

    btn_lang = InlineKeyboardButton(
        text=cfg["btn_language"],
        callback_data="act_language",
        api_kwargs={"style": "primary"}
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

# --- In-Bot Admin Commands ---
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

# --- Telegram User Handlers ---
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

# --- Callback Router ---
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
                    callback_data=f"action_proceed_pay_{btn_idx}",
                    api_kwargs={"style": "success"}
                )
            ],
            [
                InlineKeyboardButton(
                    DEFAULT_BACK_BUTTON,
                    callback_data="btn_home",
                    api_kwargs={"style": "primary"}
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
                    callback_data=f"btn_send_ss_{btn_idx}",
                    api_kwargs={"style": "success"}
                )
            ],
            [
                InlineKeyboardButton(
                    DEFAULT_BACK_BUTTON,
                    callback_data=f"btn_cat_{btn_idx}",
                    api_kwargs={"style": "primary"}
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
                    InlineKeyboardButton(DEFAULT_BACK_BUTTON, callback_data="btn_home", api_kwargs={"style": "primary"})
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

# --- Media Router ---
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

# --- FastAPI Server Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    if cfg["token"]:
        await bot_manager.restart(cfg["token"])
    yield
    await bot_manager.stop()

app = FastAPI(lifespan=lifespan)
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
    if username == cfg["admin_user"] and password == cfg["admin_pass"]:
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
            <td class="py-3 px-3 text-white font-sans">🌽 {item}</td>
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

    rendered_html = DASHBOARD_PAGE.replace("{status_pulse}", status_pulse)\
                                  .replace("{status_text}", status_text)\
                                  .replace("{msg_html}", msg_html)\
                                  .replace("{paid_orders}", str(paid_orders))\
                                  .replace("{revenue:.2f}", f"{revenue:.2f}")\
                                  .replace("{total_users}", str(total_users))\
                                  .replace("{recent_rows}", recent_rows)\
                                  .replace("{all_rows}", all_rows)\
                                  .replace("{users_rows}", users_rows)\
                                  .replace("{pack_cards_html}", pack_cards_html)\
                                  .replace("{cfg['license_expiry']}", cfg["license_expiry"])\
                                  .replace("{cfg['upi_id']}", cfg["upi_id"])\
                                  .replace("{cfg['payee_name']}", cfg["payee_name"])\
                                  .replace("{cfg['admin_chat_id']}", cfg["admin_chat_id"])\
                                  .replace("{cfg['token']}", cfg["token"])\
                                  .replace("{cfg['welcome_caption']}", cfg["welcome_caption"])\
                                  .replace("{welcome_images_text}", welcome_images_text)\
                                  .replace("{cfg['btn_how_to_use']}", cfg["btn_how_to_use"])\
                                  .replace("{cfg['msg_how_to_use']}", cfg["msg_how_to_use"])\
                                  .replace("{cfg['btn_report_issue']}", cfg["btn_report_issue"])\
                                  .replace("{cfg['msg_report_issue']}", cfg["msg_report_issue"])\
                                  .replace("{cfg['btn_language']}", cfg["btn_language"])\
                                  .replace("{cfg['msg_language']}", cfg["msg_language"])

    return HTMLResponse(content=rendered_html)

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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("__main__:app", host="0.0.0.0", port=port)
