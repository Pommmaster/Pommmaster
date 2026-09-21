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
            "🌟 🔥 𝐏𝐑𝐄𝐌𝐈𝐔𝐌 𝐀𝐃𝐔𝐋𝐓 𝐂𝐎𝐋𝐋𝐄𝐂𝐓𝐈𝐎𝐍 𝐔𝐍𝐋𝐎𝐂𝐊𝐄𝐃 🔥",
            json.dumps(default_buttons),
            json.dumps(default_button_videos),
            json.dumps(default_details),
            "YOUR_UPI",
            "YOUR_UPI_NAME",
            "",
            "📖 How To Use",
            "🚨 Report Issue",
            "🌐 Language",
            "Select any package to preview videos.",
            "Contact support.",
            "English active.",
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
    return {
        "token": row[1] or "",
        "welcome_images": json.loads(row[2]) if row[2] else [],
        "welcome_caption": row[3] or "",
        "buttons": json.loads(row[4]) if row[4] else [],
        "button_videos": json.loads(row[5]) if row[5] else {},
        "button_details": json.loads(row[6]) if row[6] else {},
        "upi_id": row[7] or "",
        "payee_name": row[8] or "Merchant",
        "admin_chat_id": row[9] or "",
        "btn_how_to_use": row[10] or "📖 How To Use",
        "btn_report_issue": row[11] or "🚨 Report Issue",
        "btn_language": row[12] or "🌐 Language",
        "msg_how_to_use": row[13] or "Select any pack to proceed.",
        "msg_report_issue": row[14] or "Contact support.",
        "msg_language": row[15] or "English active.",
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
# BOT LIFECYCLE MANAGER
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

def build_main_keyboard(cfg):
    keyboard = []
    for idx in range(len(cfg["buttons"])):
        b_name = cfg["buttons"][idx]
        details = cfg["button_details"].get(str(idx), {"price": "299"})
        price = details.get("price", "299")
        keyboard.append([InlineKeyboardButton(text=f"🟢 {b_name} - ₹{price}", callback_data=f"btn_cat_{idx}")])

    keyboard.append([
        InlineKeyboardButton(text=cfg["btn_how_to_use"], callback_data="act_how_to_use"),
        InlineKeyboardButton(text=cfg["btn_report_issue"], callback_data="act_report_issue")
    ])
    keyboard.append([InlineKeyboardButton(text=cfg["btn_language"], callback_data="act_language")])
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
    return str(chat_id) == cfg.get("admin_chat_id", "").strip()

async def handle_admin_upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return
    cfg = get_settings()
    total_btns = len(cfg["buttons"])
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"⚠️ Usage: `/upload <1-{total_btns}>`", parse_mode="Markdown")
        return
    btn_idx = int(context.args[0]) - 1
    if 0 <= btn_idx < total_btns:
        active_admin_uploads[chat_id] = btn_idx
        await update.message.reply_text(f"📥 Send media in bulk for Button #{btn_idx + 1}. Type `/done` when finished.", parse_mode="Markdown")

async def handle_admin_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return
    cfg = get_settings()
    if context.args and context.args[0].isdigit():
        idx = int(context.args[0]) - 1
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
    if user and user[4] == 1:
        return
    register_user(chat_id, update.effective_user.username or "")
    cfg = get_settings()
    for img in cfg["welcome_images"]:
        if img.strip():
            try:
                await context.bot.send_photo(chat_id=chat_id, photo=img.strip())
            except Exception:
                pass
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
            await context.bot.send_message(chat_id=u_chat_id, text=f"🎉 Payment approved for *{pack_name}*!", reply_markup=markup, parse_mode="Markdown")
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Order updated: {new_status}")
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
            except Exception:
                pass
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
            except Exception:
                pass

# ==========================================
# FASTAPI LIFECYCLE & SERVER
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    if cfg["token"]:
        await bot_manager.restart(cfg["token"])
    yield
    await bot_manager.stop()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

def is_authenticated(request: Request) -> bool:
    return request.cookies.get(AUTH_COOKIE_NAME) == AUTH_SECRET

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

DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Dashboard &mdash; Nagato Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#06040c] text-white p-6 max-w-4xl mx-auto space-y-6">
    <div class="flex justify-between items-center border-b border-purple-900/40 pb-4">
        <h1 class="font-bold text-lg text-cyan-400">Nagato Master Dashboard</h1>
        <a href="/logout" class="text-rose-400 text-xs font-mono">Sign Out</a>
    </div>
    {msg_html}
    <div class="bg-[#120b22] p-6 rounded-2xl border border-purple-900/50 space-y-4">
        <h3 class="text-sm font-bold text-fuchsia-300">Bot Token &amp; UPI Configuration</h3>
        <form method="POST" action="/admin/save-bot-settings" class="space-y-3 text-xs font-mono">
            <div>
                <label class="block text-purple-300 mb-1">Bot Token</label>
                <input type="text" name="token" value="{cfg_token}" required class="w-full bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            </div>
            <div>
                <label class="block text-purple-300 mb-1">UPI ID</label>
                <input type="text" name="upi_id" value="{cfg_upi}" required class="w-full bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            </div>
            <div>
                <label class="block text-purple-300 mb-1">Payee Name</label>
                <input type="text" name="payee_name" value="{cfg_payee}" required class="w-full bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            </div>
            <div>
                <label class="block text-purple-300 mb-1">Admin Chat ID</label>
                <input type="text" name="admin_chat_id" value="{cfg_admin_id}" class="w-full bg-[#070410] border border-purple-900 rounded p-2.5 text-white">
            </div>
            <button type="submit" class="bg-cyan-600 text-white font-bold py-2.5 px-6 rounded uppercase">Save &amp; Restart Bot</button>
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
        resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(key=AUTH_COOKIE_NAME, value=AUTH_SECRET, httponly=True, max_age=86400 * 7)
        return resp
    return RedirectResponse(url="/login?error=Invalid+Credentials", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/logout")
async def logout_admin():
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key=AUTH_COOKIE_NAME)
    return resp

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, message: str | None = None):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    cfg = get_settings()
    msg = f'<div class="p-3 bg-cyan-500/20 text-cyan-300 text-xs rounded">{message}</div>' if message else ''
    
    html = DASHBOARD_PAGE.replace("{msg_html}", msg)\
                         .replace("{cfg_token}", cfg["token"])\
                         .replace("{cfg_upi}", cfg["upi_id"])\
                         .replace("{cfg_payee}", cfg["payee_name"])\
                         .replace("{cfg_admin_id}", cfg["admin_chat_id"])
    return HTMLResponse(html)

@app.post("/admin/save-bot-settings")
async def save_bot(request: Request, token: str = Form(...), upi_id: str = Form(...), payee_name: str = Form(...), admin_chat_id: str = Form("")):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    update_field("token", token.strip())
    update_field("upi_id", upi_id.strip())
    update_field("payee_name", payee_name.strip())
    update_field("admin_chat_id", admin_chat_id.strip())
    await bot_manager.restart(token.strip())
    return RedirectResponse(url="/?message=Settings+saved+successfully", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/health")
async def health():
    return {"status": "ok", "bot_status": bot_manager.status}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("__main__:app", host="0.0.0.0", port=port)
