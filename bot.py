"""
TERABOX TELEGRAM BOT — MINIMAL, ACTUALLY WORKING
Using proxy APIs that already solve extraction.
No yt_dlp. No complex regex. Just works.
"""

import os
import re
import json
import time
import uuid
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ENV
BOT_TOKEN       = os.environ.get("BOT_TOKEN")
LOG_CHANNEL_ID  = int(os.environ.get("LOG_CHANNEL_ID", "-1001234567890"))
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "0"))
SHRINKFORGE_API = os.environ.get("SHRINKFORGE_API", "")
BOT_USERNAME    = "Terabox_Linkto_Video_bot"

# CONSTANTS
FREE_LIMIT      = 3
WINDOW_SECONDS  = 12 * 3600
UNLOCK_SECONDS  = 12 * 3600
USER_DATA_FILE  = "/tmp/terabox_user_data.json"
USERS_LOG_FILE  = "/tmp/terabox_users.txt"
DOWNLOAD_DIR    = "/tmp/terabox_downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

pending_tokens = {}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ══════════════════════════════════════════════════════════════════
# USER DATA
# ══════════════════════════════════════════════════════════════════

def load_user_data() -> dict:
    try:
        with open(USER_DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_user_data(data: dict):
    with open(USER_DATA_FILE, "w") as f:
        json.dump(data, f)


def log_user(user_id: int):
    uid = str(user_id)
    try:
        with open(USERS_LOG_FILE, "r") as f:
            existing = f.read().splitlines()
    except:
        existing = []
    if uid not in existing:
        with open(USERS_LOG_FILE, "a") as f:
            f.write(uid + "\n")


def get_total_users() -> int:
    try:
        with open(USERS_LOG_FILE, "r") as f:
            return len([l for l in f.read().splitlines() if l.strip()])
    except:
        return 0


def get_user_status(user_id: int) -> dict:
    data = load_user_data()
    uid = str(user_id)
    now = time.time()
    entry = data.get(uid, {"count": 0, "window_start": now, "unlock_until": 0})

    if entry.get("unlock_until", 0) > now:
        return {
            "can_download": True,
            "is_unlocked": True,
            "remaining": 999,
            "reset_in": 0,
        }

    window_start = entry.get("window_start", now)
    if now - window_start >= WINDOW_SECONDS:
        entry["count"] = 0
        entry["window_start"] = now
        data[uid] = entry
        save_user_data(data)

    count = entry.get("count", 0)
    remaining = max(0, FREE_LIMIT - count)
    reset_in = max(0, int(WINDOW_SECONDS - (now - window_start)))

    return {
        "can_download": remaining > 0,
        "is_unlocked": False,
        "remaining": remaining,
        "reset_in": reset_in,
    }


def increment_download_count(user_id: int):
    data = load_user_data()
    uid = str(user_id)
    now = time.time()
    entry = data.get(uid, {"count": 0, "window_start": now, "unlock_until": 0})

    if now - entry.get("window_start", now) >= WINDOW_SECONDS:
        entry["count"] = 0
        entry["window_start"] = now

    entry["count"] = entry.get("count", 0) + 1
    data[uid] = entry
    save_user_data(data)


def unlock_user(user_id: int):
    data = load_user_data()
    uid = str(user_id)
    now = time.time()
    entry = data.get(uid, {"count": 0, "window_start": now, "unlock_until": 0})
    entry["unlock_until"] = now + UNLOCK_SECONDS
    entry["count"] = 0
    entry["window_start"] = now
    data[uid] = entry
    save_user_data(data)


# ══════════════════════════════════════════════════════════════════
# TERABOX DOWNLOAD
# ══════════════════════════════════════════════════════════════════

def is_terabox_url(url: str) -> bool:
    return "terabox" in url.lower() or "1024tera" in url.lower()


async def download_terabox_video(share_url: str) -> Optional[str]:
    """
    Download using proxy APIs that already have extraction solved.
    Returns local file path or None.
    """
    
    logger.info(f"Getting dlink for: {share_url}")
    
    # Multiple proxy APIs to try
    proxy_apis = [
        f"https://terabox.app/api/get?link={share_url}",
        f"https://teraboxes.com/api?link={share_url}",
    ]
    
    dlink = None
    filename = "video.mp4"
    
    for api_url in proxy_apis:
        try:
            logger.info(f"Trying: {api_url}")
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(api_url, headers=BROWSER_HEADERS)
                if resp.status_code != 200:
                    logger.debug(f"API returned {resp.status_code}")
                    continue
                    
                data = resp.json()
                
                # Parse response - try common structures
                if data.get("list") and isinstance(data["list"], list) and data["list"]:
                    item = data["list"][0]
                    dlink = item.get("dlink")
                    filename = item.get("server_filename") or item.get("filename") or filename
                elif data.get("data") and isinstance(data["data"], dict):
                    dlink = data["data"].get("dlink")
                    filename = data["data"].get("filename") or filename
                elif data.get("dlink"):
                    dlink = data["dlink"]
                    filename = data.get("filename") or filename
                
                if dlink:
                    logger.info(f"✓ Got dlink: {filename}")
                    break
                    
        except Exception as e:
            logger.debug(f"API failed: {e}")
            continue
    
    if not dlink:
        logger.error("No dlink found from any API")
        return None
    
    # Download the file
    out_path = os.path.join(DOWNLOAD_DIR, sanitize_filename(filename))
    
    try:
        logger.info(f"Downloading: {dlink[:60]}...")
        async with httpx.AsyncClient(
            headers={
                **BROWSER_HEADERS,
                "Referer": "https://www.terabox.com/",
            },
            follow_redirects=True,
            timeout=300,
        ) as client:
            async with client.stream("GET", dlink) as r:
                r.raise_for_status()
                with open(out_path, "wb") as fp:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 512):
                        fp.write(chunk)
        
        size = os.path.getsize(out_path)
        if size > 1024:  # At least 1KB
            logger.info(f"✓ Downloaded: {out_path} ({size // 1024} KB)")
            return out_path
        else:
            logger.error(f"File too small: {size} bytes")
            return None
            
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[^\w\s\-.]', '_', name)
    name = name.strip(". ")
    return name or "video.mp4"


# ══════════════════════════════════════════════════════════════════
# AD SYSTEM
# ══════════════════════════════════════════════════════════════════

def generate_verify_token(user_id: int) -> str:
    token = uuid.uuid4().hex[:16].upper()
    pending_tokens[token] = {
        "user_id": user_id,
        "expires": time.time() + 3600,
    }
    return token


def verify_token(token: str, user_id: int) -> bool:
    if token not in pending_tokens:
        return False
    entry = pending_tokens[token]
    if entry["user_id"] != user_id or time.time() > entry["expires"]:
        pending_tokens.pop(token, None)
        return False
    pending_tokens.pop(token)
    unlock_user(user_id)
    return True


async def create_shrinkforge_link(long_url: str) -> str:
    if not SHRINKFORGE_API:
        return long_url
    
    try:
        api_endpoint = f"https://shrinkforearn.in/api?api={SHRINKFORGE_API}&url={long_url}&format=text"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(api_endpoint)
            text = resp.text.strip()
            if text.startswith("http"):
                return text
    except Exception as e:
        logger.warning(f"ShrinkForge failed: {e}")
    
    return long_url


# ══════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════

async def log_to_channel(context: ContextTypes.DEFAULT_TYPE, user, msg_in: str, msg_out: str):
    try:
        user_info = f"👤 {user.full_name} (@{user.username or 'none'}) [ID: {user.id}]"
        log_text = f"{user_info}\n📩 {msg_in[:150]}\n🤖 {msg_out[:150]}"
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_text)
    except Exception as e:
        logger.warning(f"Log channel error: {e}")


# ══════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    log_user(user_id)
    
    args = context.args
    
    # Handle ad verification
    if args and args[0].startswith("verify_"):
        parts = args[0].split("_")
        if len(parts) >= 3:
            token = parts[1]
            try:
                link_user_id = int(parts[2])
            except:
                link_user_id = -1
            
            if link_user_id != user_id:
                await update.message.reply_text("⚠️ This link is not for your account.")
                return
            
            if verify_token(token, user_id):
                msg = "✅ *Verified!* You have **12 hours unlimited downloads**.\n\nSend me a Terabox link! 🚀"
                await update.message.reply_text(msg, parse_mode="Markdown")
                await log_to_channel(context, user, "/start verify", "Unlock granted")
            else:
                await update.message.reply_text("❌ Token invalid or expired.")
        return
    
    # Normal start
    status = get_user_status(user_id)
    welcome = (
        f"👋 Hi *{user.first_name}*!\n\n"
        f"📦 I download from **Terabox**\n\n"
        f"🎥 Send me a Terabox link and I'll get it for you!\n\n"
        f"📊 Status: *{status['remaining']}/{FREE_LIMIT}* free downloads left"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")
    await log_to_channel(context, user, "/start", "Welcome sent")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Not authorized")
        return
    
    total = get_total_users()
    msg = f"📊 *Stats*\n\n👥 Users: {total}\n🔑 Tokens: {len(pending_tokens)}"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = (update.message.text or "").strip()
    
    log_user(user_id)
    
    if not is_terabox_url(text):
        await update.message.reply_text("🔗 Send a valid Terabox link.")
        return
    
    # Check quota
    status = get_user_status(user_id)
    
    if not status["can_download"]:
        token = generate_verify_token(user_id)
        deep_link = f"https://t.me/{BOT_USERNAME}?start=verify_{token}_{user_id}"
        ad_link = await create_shrinkforge_link(deep_link)
        
        h = status["reset_in"] // 3600
        m = (status["reset_in"] % 3600) // 60
        
        msg = f"⚠️ *Limit reached*\n\nResets in: *{h}h {m}m*\n\nWatch ad for 12h unlimited!"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Watch Ad", url=ad_link)]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
        await log_to_channel(context, user, text, "Limit reached - ad sent")
        return
    
    # Download
    proc_msg = await update.message.reply_text("⬇️ *Downloading...*", parse_mode="Markdown")
    
    try:
        file_path = await download_terabox_video(text)
        
        if not file_path or not os.path.exists(file_path):
            await proc_msg.edit_text("❌ *Download failed*\n\nLink may be invalid or expired.")
            await log_to_channel(context, user, text, "Failed")
            return
        
        size_mb = os.path.getsize(file_path) / 1024 / 1024
        
        if size_mb > 49:
            await proc_msg.edit_text(f"⚠️ File too large ({size_mb:.1f} MB) - Telegram limit 50 MB")
            return
        
        # Deduct download
        if not status["is_unlocked"]:
            increment_download_count(user_id)
        
        await proc_msg.edit_text("📤 *Uploading...*", parse_mode="Markdown")
        
        caption = f"🎬 Terabox\n📦 {size_mb:.1f} MB"
        
        with open(file_path, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                caption=caption,
                parse_mode="Markdown",
                supports_streaming=True,
            )
        
        await proc_msg.delete()
        
        new_status = get_user_status(user_id)
        await update.message.reply_text(f"✅ Done!\n📊 {new_status['remaining']}/{FREE_LIMIT} left")
        await log_to_channel(context, user, text, f"Sent ({size_mb:.1f} MB)")
        
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await proc_msg.edit_text("❌ *Error*")
        await log_to_channel(context, user, text, f"Error: {str(e)[:50]}")
    
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")


# ══════════════════════════════════════════════════════════════════
# WEB SERVER (Keep-Alive)
# ══════════════════════════════════════════════════════════════════

class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    
    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    logger.info(f"Web server on port {port}")
    server.serve_forever()


def start_web_server():
    t = threading.Thread(target=run_web_server, daemon=True)
    t.start()


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    logger.info("Starting Terabox Bot...")
    
    start_web_server()
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    logger.info("Polling...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
