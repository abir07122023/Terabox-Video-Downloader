"""
╔══════════════════════════════════════════════════════════════════╗
║           TERABOX TELEGRAM BOT — WORKING WITH FREE API           ║
║          Uses the official tera-api worker (no cookies)          ║
║          @Terabox_Linkto_Video_bot                               ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
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
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================== ENVIRONMENT VARIABLES ==================
BOT_TOKEN       = os.environ.get("BOT_TOKEN")
# TERABOX_NDUS is no longer required for the main downloader
# We keep it in case you want to re-add ShrinkForge ads later
LOG_CHANNEL_ID  = int(os.environ.get("LOG_CHANNEL_ID", "-1003956558170"))
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "6294267891"))
SHRINKFORGE_API = os.environ.get("SHRINKFORGE_API", "")
BOT_USERNAME    = "Terabox_Linkto_Video_bot"

# Freemium constants
FREE_LIMIT      = 3
WINDOW_SECONDS  = 12 * 3600      # 12 hours
UNLOCK_SECONDS  = 12 * 3600

# File paths
USER_DATA_FILE  = "/tmp/terabox_user_data.json"
USERS_LOG_FILE  = "/tmp/terabox_users.txt"
DOWNLOAD_DIR    = "/tmp/terabox_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

pending_tokens = {}

TERABOX_DOMAINS = ["terabox.com", "1024terabox.com", "terabox.app", "teraboxapp.com",
                   "nephobox.com", "freeterabox.com", "mirrobox.com", "momerybox.com",
                   "tibibox.com", "1024tera.com"]

def is_terabox_url(url: str) -> bool:
    return any(domain in url for domain in TERABOX_DOMAINS)

# ================== USER DATA ==================
def load_user_data() -> dict:
    try:
        with open(USER_DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_user_data(data: dict) -> None:
    with open(USER_DATA_FILE, "w") as f:
        json.dump(data, f)

def log_user(user_id: int) -> None:
    uid = str(user_id)
    try:
        with open(USERS_LOG_FILE, "r") as f:
            existing = f.read().splitlines()
    except FileNotFoundError:
        existing = []
    if uid not in existing:
        with open(USERS_LOG_FILE, "a") as f:
            f.write(uid + "\n")

def get_total_users() -> int:
    try:
        with open(USERS_LOG_FILE, "r") as f:
            return len([l for l in f.read().splitlines() if l.strip()])
    except FileNotFoundError:
        return 0

def get_user_status(user_id: int) -> dict:
    data = load_user_data()
    uid = str(user_id)
    now = time.time()
    entry = data.get(uid, {"count": 0, "window_start": now, "unlock_until": 0})

    if entry.get("unlock_until", 0) > now:
        return {"can_download": True, "is_unlocked": True, "remaining": 999, "reset_in": 0}

    window_start = entry.get("window_start", now)
    if now - window_start >= WINDOW_SECONDS:
        entry["count"] = 0
        entry["window_start"] = now
        data[uid] = entry
        save_user_data(data)

    count = entry.get("count", 0)
    remaining = max(0, FREE_LIMIT - count)
    reset_in = max(0, int(WINDOW_SECONDS - (now - window_start)))
    return {"can_download": remaining > 0, "is_unlocked": False, "remaining": remaining, "reset_in": reset_in}

def increment_download_count(user_id: int) -> None:
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

def unlock_user(user_id: int) -> None:
    data = load_user_data()
    uid = str(user_id)
    now = time.time()
    entry = data.get(uid, {"count": 0, "window_start": now, "unlock_until": 0})
    entry["unlock_until"] = now + UNLOCK_SECONDS
    entry["count"] = 0
    entry["window_start"] = now
    data[uid] = entry
    save_user_data(data)

# ================== AD SYSTEM (optional) ==================
def generate_verify_token(user_id: int) -> str:
    token = uuid.uuid4().hex[:16].upper()
    pending_tokens[token] = {"user_id": user_id, "expires": time.time() + 3600}
    return token

def verify_token(token: str, user_id: int) -> bool:
    if token not in pending_tokens:
        return False
    entry = pending_tokens[token]
    if entry["user_id"] != user_id or time.time() > entry["expires"]:
        if token in pending_tokens:
            del pending_tokens[token]
        return False
    del pending_tokens[token]
    unlock_user(user_id)
    return True

async def create_ad_link(deep_link: str) -> str:
    if not SHRINKFORGE_API:
        return deep_link
    api_endpoint = f"https://shrinkforearn.in/api?api={SHRINKFORGE_API}&url={deep_link}&format=text"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(api_endpoint)
            text = resp.text.strip()
            if text.startswith("http"):
                return text
    except Exception as e:
        logger.error(f"ShrinkForge error: {e}")
    return deep_link

# ================== TERABOX DOWNLOAD (WORKING FREE API) ==================
async def download_terabox_video(share_url: str) -> Optional[str]:
    """Use the official tera-api (free) to get direct download link and download the video."""
    try:
        # The official, free API endpoint that does NOT require cookies
        api_url = f"https://terabox-worker.robinkumarshakya103.workers.dev/api?url={share_url}"
        logger.info(f"Contacting API: {api_url}")
        
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(api_url)
            logger.info(f"API response status: {resp.status_code}")
            
            if resp.status_code != 200:
                logger.error(f"API returned {resp.status_code}")
                return None
                
            data = resp.json()
            logger.info(f"API response structure: {list(data.keys())}")
            
            # Check if the API returned a successful response with files
            if data.get("success") and data.get("files"):
                file_info = data["files"][0]  # Get the first file
                direct_link = file_info.get("download_url")
                filename = file_info.get("file_name", "video.mp4")
                
                if not direct_link:
                    logger.error("No download_url in API response")
                    return None
                    
                out_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex}.mp4")
                logger.info(f"Downloading from: {direct_link[:80]}...")
                
                # Download the file
                async with httpx.AsyncClient(follow_redirects=True, timeout=300) as dl_client:
                    async with dl_client.stream("GET", direct_link) as r:
                        r.raise_for_status()
                        with open(out_path, "wb") as f:
                            async for chunk in r.aiter_bytes(chunk_size=8192):
                                f.write(chunk)
                
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    logger.info(f"Download complete: {out_path}")
                    return out_path
                else:
                    logger.error("Downloaded file is empty or missing")
                    return None
            else:
                error_msg = data.get("error", "Unknown error from API")
                logger.error(f"API error: {error_msg}")
                return None
                
    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        return None

# ================== LOG CHANNEL ==================
async def log_to_channel(context: ContextTypes.DEFAULT_TYPE, user, message_text: str, bot_reply: str):
    try:
        user_info = f"👤 {user.full_name} (@{user.username or 'no_username'}) [ID: {user.id}]"
        log_text = f"{user_info}\n📩 User: {message_text[:300]}\n🤖 Bot: {bot_reply[:300]}"
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_text)
    except Exception as e:
        logger.warning(f"Log channel error: {e}")

# ================== TELEGRAM HANDLERS ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    log_user(user_id)
    args = context.args

    if args and args[0].startswith("verify_"):
        parts = args[0].split("_")
        if len(parts) >= 3:
            token = parts[1]
            link_user_id = int(parts[2]) if parts[2].isdigit() else -1
            if link_user_id != user_id:
                await update.message.reply_text("⚠️ This verification link is not for your account.")
                return
            if verify_token(token, user_id):
                msg = "✅ *Ad verified!* You now have **12 hours of unlimited downloads**.\n\nSend me any Terabox link!"
                await update.message.reply_text(msg, parse_mode="Markdown")
                await log_to_channel(context, user, f"/start verify {token}", "Unlock granted")
            else:
                await update.message.reply_text("❌ Invalid or expired token.")
        return

    status = get_user_status(user_id)
    welcome = (
        f"👋 Welcome, *{user.first_name}*!\n\n"
        f"📦 I download videos from **Terabox**.\n\n"
        f"🎁 *Free Plan:* {FREE_LIMIT} downloads per 12 hours.\n"
        f"📊 *Your status:* {status['remaining']} free downloads remaining.\n\n"
        f"Just send a Terabox link and I'll fetch it for you!"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")
    await log_to_channel(context, user, "/start", "Welcome message sent")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized.")
        return
    total = get_total_users()
    msg = f"📊 *Bot Statistics*\n👥 Total users: `{total}`\n🔑 Pending tokens: `{len(pending_tokens)}`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    if not is_terabox_url(text):
        await update.message.reply_text(
            "🔗 Please send a valid Terabox link.\nSupported: `terabox.com`, `1024terabox.com`, `terabox.app`, etc.",
            parse_mode="Markdown"
        )
        return

    status = get_user_status(user_id)
    if not status["can_download"]:
        token = generate_verify_token(user_id)
        deep_link = f"https://t.me/{BOT_USERNAME}?start=verify_{token}_{user_id}"
        ad_link = await create_ad_link(deep_link)
        hours = status["reset_in"] // 3600
        mins = (status["reset_in"] % 3600) // 60
        limit_msg = (
            f"⚠️ *Download limit reached!*\n\n"
            f"You've used all {FREE_LIMIT} free downloads for this 12-hour window.\n\n"
            f"⏳ *Free downloads reset in:* {hours}h {mins}m\n\n"
            f"*OR* watch a short ad to unlock **12 hours of unlimited downloads**!\n\n"
            f"👇 Click the button below:"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Watch Ad & Unlock", url=ad_link)]])
        await update.message.reply_text(limit_msg, parse_mode="Markdown", reply_markup=keyboard)
        await log_to_channel(context, user, text, "Limit reached — ad link sent")
        return

    processing_msg = await update.message.reply_text("⬇️ *Downloading your video...* ⏳", parse_mode="Markdown")
    file_path = None
    try:
        file_path = await download_terabox_video(text)
        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("No output file")

        size_mb = os.path.getsize(file_path) / 1024 / 1024
        if size_mb > 49:
            await processing_msg.edit_text(f"⚠️ File too large ({size_mb:.1f} MB) > 50 MB limit.")
            return

        if not status["is_unlocked"]:
            increment_download_count(user_id)

        await processing_msg.edit_text("📤 *Uploading to Telegram...*", parse_mode="Markdown")
        caption = f"🎬 *{os.path.basename(file_path)}*\n📦 Size: {size_mb:.1f} MB\n\n_Downloaded by @{BOT_USERNAME}_"
        with open(file_path, "rb") as f:
            await update.message.reply_video(video=f, caption=caption, parse_mode="Markdown", supports_streaming=True)
        await processing_msg.delete()

        new_status = get_user_status(user_id)
        if not new_status["is_unlocked"]:
            remaining_text = f"📊 Downloads remaining: {new_status['remaining']}/{FREE_LIMIT}"
        else:
            h = int((new_status["unlock_until"] - time.time()) / 3600)
            remaining_text = f"🔓 Unlimited — {h}h left"
        await update.message.reply_text(remaining_text)
        await log_to_channel(context, user, text, f"Video sent ✅ ({size_mb:.1f} MB)")

    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        await processing_msg.edit_text(
            "❌ *Download failed.*\n\n"
            "Possible reasons:\n"
            "• The API is currently rate-limited\n"
            "• The link is private or expired\n"
            "• Terabox blocked the request\n\n"
            "Please try again in a few minutes or try a different link.",
            parse_mode="Markdown"
        )
        await log_to_channel(context, user, text, f"Error: {str(e)[:200]}")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)

# ================== KEEP-ALIVE SERVER ==================
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_keep_alive():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    server.serve_forever()

# ================== MAIN ==================
async def delete_webhook_and_wait():
    async with httpx.AsyncClient() as client:
        await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    await asyncio.sleep(3)

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        return

    threading.Thread(target=run_keep_alive, daemon=True).start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(delete_webhook_and_wait())
    loop.close()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Starting polling...")
    # The 'pool_timeout' argument was removed as it's not compatible with your library version
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
