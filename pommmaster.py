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
            {"btn": "CHILD POM 🙈💋",      "pack": "CHILD POM 🙈💋",          "price": "62",   "desc": "𝗖𝗛𝗜𝗟𝗗 𝗣𝗢𝗠 𝗩𝗜𝗗𝗘𝗢𝗦 𝟰𝟱𝟬𝟬𝟬 𝗩𝗜𝗗𝗘𝗢𝗦 𝗔𝗥𝗘 𝗧𝗛𝗘𝗥𝗘 𝟭𝟬 𝗚𝗥𝗢𝗨𝗣𝗦 𝗢𝗙 𝟰𝟱𝟬𝟬𝟬 𝗩𝗜𝗗𝗘𝗢𝗦 𝟯𝟱𝟬+ 𝗭𝗜𝗣𝗦 𝗢𝗙 𝟱𝟬𝟬𝗚𝗕", "link": "https://t.me/+YourLink1"},
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
            while self.status == "Running":
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Bot runtime failure: {e}")
            self.status = f"Error: {e}"

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
        self.status = "Stopped"

    async def restart(self, token: str):
        await self.stop()
        if token.strip():
            self.task = asyncio.create_task(self._run_bot(token.strip()))
            await asyncio.sleep(1.0)

bot_manager = BotManager()

def build_main_keyboard(cfg):
    keyboard = []
    for idx in range(len(cfg["buttons"])):
        b_name = cfg["buttons"][idx]
        price = cfg["button_details"].get(str(idx), {}).get("price", "299")
        keyboard.append([InlineKeyboardButton(text=f"🟢 {b_name} - ₹{price}", callback_data=f"btn_cat_{idx}")])

    keyboard.append([
        InlineKeyboardButton(text=cfg["btn_how_to_use"], callback_data="act_how_to_use"),
        InlineKeyboardButton(text=cfg["btn_report_issue"], callback_data="act_report_issue")
    ])
    keyboard.append([InlineKeyboardButton(text=cfg["btn_language"], callback_data="act_language")])
    return InlineKeyboardMarkup(keyboard)

async def send_media_in_chunks(context, chat_id, media_list):
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
    return str(chat_id) == cfg.get("admin_chat_id", "").strip()

async def handle_admin_upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id): return
    cfg = get_settings()
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Usage: `/upload <number>`", parse_mode="Markdown")
        return
    idx = int(context.args[0]) - 1
    if 0 <= idx < len(cfg["buttons"]):
        active_admin_uploads[chat_id] = idx
        await update.message.reply_text(f"📥 Send media in bulk for Button #{idx + 1}. Type `/done` when finished.", parse_mode="Markdown")

async def handle_admin_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id): return
    if context.args and context.args[0].isdigit():
        idx = int(context.args[0]) - 1
        cfg = get_settings()
        vids = cfg["button_videos"]
        vids[str(idx)] = []
        update_field("button_videos_json", json.dumps(vids))
        await update.message.reply_text(f"🗑️ Cleared media for Button #{idx + 1}.")

async def handle_admin_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_admin_uploads:
        active_admin_uploads.pop(chat_id)
        await update.message.reply_text("✅ Upload session finished.")

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    if user and user[4] == 1: return
    register_user(chat_id, update.effective_user.username or "")
    cfg = get_settings()
    for img in cfg["welcome_images"]:
        if img.strip():
            try: await context.bot.send_photo(chat_id=chat_id, photo=img.strip())
            except Exception: pass
    await context.bot.send_message(chat_id=chat_id, text=cfg["welcome_caption"], reply_markup=build_main_keyboard(cfg), parse_mode="Markdown")

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending_verifications.pop(chat_id, None)
    active_admin_uploads.pop(chat_id, None)
    await update.message.reply_text("❌ Action cancelled.")
    await handle_start(update, context)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data
    cfg = get_settings()

    if data.startswith("adm_ord:"):
        if not is_admin(chat_id): return
        _, oid, act = data.split(":")
        order_id = int(oid)
        new_status = "Paid" if act == "accept" else "Rejected"
        update_order_status(order_id, new_status)
        order = get_order_by_id(order_id)
        if order and new_status == "Paid":
            _, u_chat_id, _, pack_name, amount, _ = order
            update_user_subscription(u_chat_id, pack_name)
            link = ""
            for idx, name in enumerate(cfg["buttons"]):
                if cfg["button_details"].get(str(idx), {}).get("pack") == pack_name:
                    link = cfg["button_details"][str(idx)].get("link", "")
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Access Link", url=link)]]) if link else None
            await context.bot.send_message(chat_id=u_chat_id, text=f"🎉 Payment verified for *{pack_name}*!", reply_markup=markup, parse_mode="Markdown")
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Order #{order_id} marked as {new_status}")
        return

    if data == "btn_home":
        await handle_start(update, context)
    elif data == "act_how_to_use":
        await query.answer(cfg["msg_how_to_use"], show_alert=True)
    elif data == "act_report_issue":
        await query.answer(cfg["msg_report_issue"], show_alert=True)
    elif data == "act_language":
        await query.answer(cfg["msg_language"], show_alert=True)
    elif data.startswith("btn_cat_"):
        idx = data.replace("btn_cat_", "")
        vids = cfg["button_videos"].get(idx, [])
        for m in vids[:5]:
            try:
                if m.startswith("http"): await context.bot.send_photo(chat_id=chat_id, photo=m)
                else: await context.bot.send_video(chat_id=chat_id, video=m, supports_streaming=True)
            except Exception: pass
        details = cfg["button_details"].get(idx, {"pack": "Pack", "price": "299", "desc": ""})
        txn_id = generate_txn_id(details["pack"])
        log_order(txn_id, chat_id, update.effective_user.username or "", details["pack"], float(details["price"]))
        upi_uri = make_upi_uri(cfg["upi_id"], cfg["payee_name"], details["price"], txn_id)
        qr_url = generate_upi_qr_url(upi_uri)
        pay_text = f"💎 <b>Payment Details</b>\n\n📦 <b>Pack:</b> {details['pack']}\n💰 <b>Amount:</b> ₹{details['price']}\n🧾 <b>Txn:</b> <code>{txn_id}</code>\n\nScan QR and send payment screenshot here."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="btn_home")]])
        await context.bot.send_photo(chat_id=chat_id, photo=qr_url, caption=pay_text, reply_markup=markup, parse_mode="HTML")

async def handle_incoming_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = update.effective_chat.id
    if chat_id in active_admin_uploads and is_admin(chat_id):
        idx = active_admin_uploads[chat_id]
        f_id = msg.video.file_id if msg.video else (msg.photo[-1].file_id if msg.photo else None)
        if f_id:
            cfg = get_settings()
            vids = cfg["button_videos"]
            if str(idx) not in vids: vids[str(idx)] = []
            vids[str(idx)].append(f_id)
            update_field("button_videos_json", json.dumps(vids))
            await msg.reply_text(f"📥 Saved media to Button #{idx + 1}. Send next or `/done`.")
            return

    if msg.photo:
        cfg = get_settings()
        await msg.reply_text("✅ Screenshot Received! Verification in progress.")
        if cfg["admin_chat_id"]:
            try:
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("SELECT id, pack_name, amount FROM orders WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (chat_id,))
                row = c.fetchone()
                conn.close()
                oid, pack, amt = row if row else (0, "Pack", 0)
                markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Accept", callback_data=f"adm_ord:{oid}:accept"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"adm_ord:{oid}:reject")
                ]])
                await context.bot.send_photo(chat_id=int(cfg["admin_chat_id"]), photo=msg.photo[-1].file_id, caption=f"🚨 Payment Proof\nOrder: #{oid}\nUser: @{update.effective_user.username}\nPack: {pack}\nAmount: ₹{amt}", reply_markup=markup)
            except Exception: pass

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

def is_authenticated(request: Request) -> bool:
    return request.cookies.get(AUTH_COOKIE_NAME) == AUTH_SECRET

# ==========================================
# FULL CYBERPUNK ADMIN WEB PANEL HTML
# ==========================================
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Admin Login &mdash; Nagato Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#030108] text-white flex items-center justify-center min-h-screen">
    <div class="bg-[#100c22] p-8 rounded-2xl border border-purple-500/40 w-96 space-y-4 shadow-2xl">
        <h1 class="font-bold text-lg text-fuchsia-400">Nagato Panel Login</h1>
        {err_html}
        <form method="POST" action="/login" class="space-y-3 text-xs font-mono">
            <input type="text" name="username" required placeholder="Username" class="w-full bg-[#080613] border border-purple-900 rounded p-3 text-white">
            <input type="password" name="password" required placeholder="Password" class="w-full bg-[#080613] border border-purple-900 rounded p-3 text-white">
            <button type="submit" class="w-full bg-gradient-to-r from-fuchsia-600 to-cyan-500 py-3 rounded font-bold uppercase">Sign In</button>
        </form>
    </div>
</body>
</html>"""

@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    err = f'<div class="p-2 bg-rose-500/20 text-rose-300 text-xs rounded">{error}</div>' if error else ''
    return HTMLResponse(LOGIN_PAGE.replace("{err_html}", err))

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

    msg_html = f'<div class="p-3 bg-cyan-500/20 text-cyan-300 text-xs rounded font-mono"><span>{message}</span></div>' if message else ''

    recent_rows = ""
    for txn_id, cid, uname, item, amt, dt, st, oid in recent_orders:
        badge = '<span class="text-emerald-400">PAID</span>' if st == 'Paid' else '<span class="text-amber-400">PENDING</span>'
        recent_rows += f"<tr><td class='p-2'>{txn_id}</td><td class='p-2'>{cid} (@{uname})</td><td class='p-2'>{item}</td><td class='p-2'>₹{amt}</td><td class='p-2 text-right'>{badge}</td></tr>"

    all_rows = ""
    for txn_id, cid, uname, item, amt, dt, st, oid in all_orders:
        all_rows += f"<tr><td class='p-2'>{txn_id}</td><td class='p-2'>{cid}</td><td class='p-2'>{item}</td><td class='p-2'>₹{amt}</td><td class='p-2'>{st}</td><td class='p-2 text-right'><a href='/admin/order/update/{oid}/Paid' class='text-emerald-400 underline mr-2'>Accept</a><a href='/admin/order/update/{oid}/Rejected' class='text-rose-400 underline'>Reject</a></td></tr>"

    pack_cards_html = ""
    for idx, b_name in enumerate(cfg["buttons"]):
        details = cfg["button_details"].get(str(idx), {"pack": "", "price": "299", "desc": "", "link": ""})
        v_list = cfg["button_videos"].get(str(idx), [])
        pack_cards_html += f"""
        <div class="bg-[#120b22] p-4 rounded-xl border border-purple-900/40 space-y-2 text-xs">
            <h4 class="font-bold text-cyan-400">Button #{idx+1}: {b_name}</h4>
            <input type="text" name="btn_label_{idx}" value="{b_name}" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white">
            <input type="text" name="pack_name_{idx}" value="{details.get('pack','')}" placeholder="Pack Name" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white">
            <input type="text" name="pack_price_{idx}" value="{details.get('price','299')}" placeholder="Price" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white">
            <input type="url" name="pack_link_{idx}" value="{details.get('link','')}" placeholder="Access Link (t.me/...)" class="w-full bg-[#070410] border border-cyan-500/40 rounded p-2 text-cyan-300">
            <textarea name="btn_videos_{idx}" rows="2" placeholder="Media file_ids" class="w-full bg-[#070410] border border-purple-900 rounded p-2 text-white font-mono">{"\n".join(v_list)}</textarea>
            <input type="file" name="files_{idx}" multiple accept="video/*,image/*" class="w-full text-[10px] text-slate-400">
        </div>"""

    dashboard_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Nagato Panel Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#06040c] text-white p-6 max-w-5xl mx-auto space-y-6">
    <div class="flex justify-between items-center border-b border-purple-900/40 pb-4">
        <h1 class="font-bold text-lg text-cyan-400">Nagato Master Dashboard (Pom Pom V2)</h1>
        <a href="/logout" class="text-rose-400 text-xs font-mono">Sign Out</a>
    </div>
    {msg_html}
    <div class="grid grid-cols-3 gap-4 text-xs font-mono">
        <div class="bg-[#120b22] p-4 rounded-xl border border-purple-900">Total Users: <b class="text-cyan-400">{total_users}</b></div>
        <div class="bg-[#120b22] p-4 rounded-xl border border-purple-900">Paid Orders: <b class="text-emerald-400">{paid_orders}</b></div>
        <div class="bg-[#120b22] p-4 rounded-xl border border-purple-900">Revenue: <b class="text-fuchsia-400">₹{revenue}</b></div>
    </div>

    <div class="bg-[#120b22] p-6 rounded-2xl border border-purple-900/50 space-y-4">
        <h3 class="text-sm font-bold text-fuchsia-300">Bot &amp; UPI Settings</h3>
        <form method="POST" action="/admin/save-bot-settings" class="space-y-3 text-xs font-mono">
            <input type="text" name="token" value="{cfg['token']}" placeholder="Bot Token" required class="w-full bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <input type="text" name="upi_id" value="{cfg['upi_id']}" placeholder="UPI ID" required class="w-full bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <input type="text" name="payee_name" value="{cfg['payee_name']}" placeholder="Payee Name" required class="w-full bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <input type="text" name="admin_chat_id" value="{cfg['admin_chat_id']}" placeholder="Admin Chat ID" class="w-full bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            <button type="submit" class="bg-cyan-600 text-white font-bold py-2 px-4 rounded uppercase">Save Settings</button>
        </form>
    </div>

    <div class="bg-[#120b22] p-6 rounded-2xl border border-purple-900/50 space-y-4">
        <h3 class="text-sm font-bold text-fuchsia-300">Manage 17 Plan Buttons &amp; Media</h3>
        <form method="POST" action="/admin/save-packs" enctype="multipart/form-data" class="space-y-4">
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {pack_cards_html}
            </div>
            <button type="submit" class="w-full bg-emerald-600 text-white font-bold py-3 rounded uppercase">Save All Buttons &amp; Upload Files</button>
        </form>
    </div>
</body>
</html>"""
    return HTMLResponse(dashboard_html)

@app.get("/admin/order/update/{order_id}/{new_status}")
async def change_order_status(order_id: int, new_status: str, request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    update_order_status(order_id, new_status)
    return RedirectResponse(url="/?message=Order+updated", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/save-bot-settings")
async def save_bot_settings(request: Request, token: str = Form(...), upi_id: str = Form(...), payee_name: str = Form(...), admin_chat_id: str = Form("")):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    update_field("token", token.strip())
    update_field("upi_id", upi_id.strip())
    update_field("payee_name", payee_name.strip())
    update_field("admin_chat_id", admin_chat_id.strip())
    await bot_manager.restart(token.strip())
    return RedirectResponse(url="/?message=Settings+saved", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/save-packs")
async def save_packs_settings(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    form = await request.form()
    cfg = get_settings()

    buttons, button_videos, button_details = [], {}, {}
    total_btns = len(cfg["buttons"])

    for idx in range(total_btns):
        btn_label = form.get(f"btn_label_{idx}", f"Pack {idx+1}").strip()
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
                        try:
                            if f.filename.lower().endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv")):
                                sent_msg = await bot_manager.app.bot.send_video(chat_id=admin_id, video=file_bytes, supports_streaming=True)
                                if sent_msg.video: existing_items.append(sent_msg.video.file_id)
                            else:
                                sent_msg = await bot_manager.app.bot.send_photo(chat_id=admin_id, photo=file_bytes)
                                if sent_msg.photo: existing_items.append(sent_msg.photo[-1].file_id)
                        except Exception as e:
                            logger.error(f"File upload error: {e}")

        button_videos[str(idx)] = existing_items
        button_details[str(idx)] = {
            "pack": form.get(f"pack_name_{idx}", "").strip(),
            "price": form.get(f"pack_price_{idx}", "299").strip(),
            "desc": form.get(f"pack_desc_{idx}", "").strip(),
            "link": form.get(f"pack_link_{idx}", "").strip(),
        }

    update_field("buttons_json", json.dumps(buttons))
    update_field("button_videos_json", json.dumps(button_videos))
    update_field("button_details_json", json.dumps(button_details))
    return RedirectResponse(url="/?message=Packs+saved+successfully", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/revenue/reset")
async def reset_revenue(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    return RedirectResponse(url="/?message=Revenue+reset", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/health")
async def health_check():
    return {"status": "ok", "bot_online": bot_manager.status == "Running"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
