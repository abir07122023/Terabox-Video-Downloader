"""
╔══════════════════════════════════════════════════════════════════╗
║               TERABOX TELEGRAM BOT — AD-FREE                      ║
║                     @Terabox_Linkto_Video_bot                     ║
║                                                                   ║
║  Freemium model: 3 free videos per 12 hours.                     ║
║  Uses the `terabox-downloader` Python package for reliable       ║
║  video extraction and download.                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import json
import re
import time
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Import the Terabox downloader package
from TeraboxDL import TeraboxDL

# -------------------------------
# LOGGING & CONFIGURATION
# -------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Environment variables (set these in your hosting platform)
BOT_TOKEN       = os.environ.get("BOT_TOKEN")
TERABOX_NDUS    = os.environ.get("TERABOX_NDUS")
LOG_CHANNEL_ID  = int(os.environ.get("LOG_CHANNEL_ID", "-1003956558170"))
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "6294267891"))
BOT_USERNAME    = "Terabox_Linkto_Video_bot"

FREE_LIMIT      = 3
WINDOW_SECONDS  = 12 * 3600      # 12 hours

USER_DATA_FILE  = "/tmp/terabox_user_data.json"
USERS_LOG_FILE  = "/tmp/terabox_users.txt"
DOWNLOAD_DIR    = "/tmp/terabox_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# The cookie string expected by TeraboxDL
TERABOX_COOKIE = f"lang=en; ndus={TERABOX_NDUS};"

# Helper: check if a URL is a Terabox link
def is_terabox_url(url: str) -> bool:
    domains = [
        "terabox.com", "terabox.app", "teraboxapp.com",
        "1024terabox.com", "1024tera.com",
    ]
    return any(domain in url for domain in domains)

# -------------------------------
# USER DATA PERSISTENCE
# -------------------------------

def load_user_data() -> dict:
    try:
        with open(USER_DATA_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
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
    entry = data.get(uid, {"count": 0, "window_start": now})

    # Reset the 12-hour window if expired
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
        "remaining":    remaining,
        "reset_in":     reset_in,
    }

def increment_download_count(user_id: int) -> None:
    data = load_user_data()
    uid = str(user_id)
    now = time.time()
    entry = data.get(uid, {"count": 0, "window_start": now})

    if now - entry.get("window_start", now) >= WINDOW_SECONDS:
        entry["count"] = 0
        entry["window_start"] = now

    entry["count"] = entry.get("count", 0) + 1
    data[uid] = entry
    save_user_data(data)

# -------------------------------
# LOG CHANNEL
# -------------------------------

async def log_to_channel(context: ContextTypes.DEFAULT_TYPE, user, message_text: str, bot_reply: str):
    try:
        user_info = f"👤 {user.full_name} (@{user.username or 'no_username'}) [ID: {user.id}]"
        log_text = f"{user_info}\n📩 User: {message_text[:300]}\n🤖 Bot: {bot_reply[:300]}"
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_text)
    except Exception as e:
        logger.warning(f"Log channel error: {e}")

# -------------------------------
# TERABOX DOWNLOAD LOGIC (USING TeraboxDL)
# -------------------------------

async def download_terabox_video(share_url: str) -> Optional[str]:
    """Uses TeraboxDL to get file info and download the video directly."""
    try:
        # Initialize the TeraboxDL instance with your ndus cookie
        terabox = TeraboxDL(TERABOX_COOKIE)

        # Get file information (including the direct download link)
        # The `direct_url=True` parameter ensures we get a direct download link
        file_info = terabox.get_file_info(share_url, direct_url=True)

        if "error" in file_info:
            logger.error(f"TeraboxDL extraction failed: {file_info['error']}")
            return None

        download_link = file_info.get("download_link")
        if not download_link:
            logger.error("No download_link found in file_info")
            return None

        # Prepare the output filename and path
        raw_filename = file_info.get("file_name", "video.mp4")
        safe_filename = re.sub(r'[^\w\s\-.]', '_', raw_filename).strip(". ")
        file_path = os.path.join(DOWNLOAD_DIR, safe_filename)

        logger.info(f"Downloading from: {download_link[:80]}...")
        # Download the file using httpx (async)
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            async with client.stream("GET", download_link) as response:
                response.raise_for_status()
                with open(file_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 512):  # 512KB chunks
                        f.write(chunk)

        # Verify the download was successful (file size > 0)
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            logger.info(f"Download complete: {file_path}")
            return file_path
        else:
            logger.error("Downloaded file is empty")
            return None

    except Exception as e:
        logger.error(f"Terabox download error: {e}", exc_info=True)
        return None

# -------------------------------
# TELEGRAM HANDLERS
# -------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    log_user(user_id)

    status = get_user_status(user_id)
    welcome_msg = (
        f"👋 Welcome, *{user.first_name}*!\n\n"
        f"📦 I download videos from **Terabox** for you.\n\n"
        f"🔓 *Free Plan:* {FREE_LIMIT} downloads per 12 hours.\n\n"
        f"📊 *Your status:* {status['remaining']} free downloads remaining.\n\n"
        f"Just send me a Terabox link to get started!"
    )
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")
    await log_to_channel(context, user, "/start", "Welcome message sent")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    total_users = get_total_users()
    msg = f"📊 *Bot Statistics*\n\n👥 Total unique users: `{total_users}`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    # Validate the URL is from Terabox
    if not is_terabox_url(text):
        await update.message.reply_text(
            "🔗 Please send a valid Terabox share link.\n"
            "Supported domains: `terabox.com`, `1024terabox.com`, `terabox.app`, etc.",
            parse_mode="Markdown"
        )
        return

    # Check user's download quota
    status = get_user_status(user_id)
    if not status["can_download"]:
        hours = status["reset_in"] // 3600
        mins = (status["reset_in"] % 3600) // 60
        limit_msg = (
            f"⚠️ *Free limit reached!*\n\n"
            f"You've used all {FREE_LIMIT} free downloads for this 12-hour window.\n\n"
            f"⏳ *Free downloads reset in:* {hours}h {mins}m"
        )
        await update.message.reply_text(limit_msg, parse_mode="Markdown")
        return

    # Proceed with the download
    processing_msg = await update.message.reply_text(
        "⬇️ *Downloading your video...*\nThis may take a moment ⏳",
        parse_mode="Markdown"
    )

    file_path = None
    try:
        file_path = await download_terabox_video(text)
        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("Download produced no output file")

        file_size_mb = os.path.getsize(file_path) / 1024 / 1024
        if file_size_mb > 49:
            await processing_msg.edit_text(
                f"⚠️ File is too large ({file_size_mb:.1f} MB) for Telegram's 50 MB limit.\n"
                "Try a shorter video or contact the admin."
            )
            return

        # Increment the user's download count after a successful download
        increment_download_count(user_id)

        await processing_msg.edit_text("📤 *Uploading to Telegram...*", parse_mode="Markdown")

        caption = (
            f"🎬 *{os.path.basename(file_path)}*\n"
            f"📦 Size: {file_size_mb:.1f} MB\n\n"
            f"_Downloaded by @{BOT_USERNAME}_"
        )

        with open(file_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=caption,
                parse_mode="Markdown",
                supports_streaming=True,
            )

        await processing_msg.delete()

        # Send remaining free quota info
        new_status = get_user_status(user_id)
        remaining_text = f"📊 Downloads remaining: {new_status['remaining']}/{FREE_LIMIT}"
        await update.message.reply_text(remaining_text)
        await log_to_channel(context, user, text, f"Video sent ✅ ({file_size_mb:.1f} MB)")

    except Exception as e:
        logger.error(f"Download/send error for {user_id}: {e}", exc_info=True)
        await processing_msg.edit_text(
            "❌ *Download failed.*\n\n"
            "Possible reasons:\n"
            "• The link is private or expired\n"
            "• Terabox blocked the request\n"
            "• File is not a video\n\n"
            "Please try again or try a different link.",
            parse_mode="Markdown"
        )
        await log_to_channel(context, user, text, f"Error: {str(e)[:200]}")

    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)

# -------------------------------
# KEEP-ALIVE WEB SERVER (for Render / Railway)
# -------------------------------

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

# -------------------------------
# MAIN ENTRY POINT
# -------------------------------

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        return
    if not TERABOX_NDUS:
        logger.warning("TERABOX_NDUS not set. Downloads may fail.")

    # Start the keep-alive server (necessary for free tiers on Render)
    threading.Thread(target=run_keep_alive, daemon=True).start()

    # Build and run the Telegram bot
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started, polling for updates...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
