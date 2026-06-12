import os
import json
import logging
import tempfile
import time
import asyncio
import httpx
import uuid
from concurrent.futures import ThreadPoolExecutor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Environment variables (set these in your host) ─────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TERABOX_NDUS = os.environ.get("TERABOX_NDUS")
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "-1003956558170"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6294267891"))

# ShrinkForge API credentials
SHRINKFORGE_API = os.environ.get("SHRINKFORGE_API", "23f12fc648e44117a4fd3a85030aed862651f6ff")
SHRINKFORGE_API_URL = "https://shrinkforge.com/api"

# File for storing user data (free count and unlock timestamp)
USER_DATA_FILE = '/tmp/terabox_user_data.json'

# In-memory dictionary to temporarily store pending verification tokens
pending_tokens = {}  # {token: user_id}

executor = ThreadPoolExecutor(max_workers=4)

# ── Helper: ShrinkForge short link generation ─────────────────────────────
async def get_shrinkforge_link(destination_url: str) -> str | None:
    """Returns a ShrinkForge short URL that redirects to destination_url."""
    try:
        params = {
            "api": SHRINKFORGE_API,
            "url": destination_url,
            "format": "text"
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(SHRINKFORGE_API_URL, params=params, timeout=10)
            if resp.status_code == 200:
                short_url = resp.text.strip()
                if short_url.startswith("http"):
                    return short_url
                else:
                    logger.error(f"ShrinkForge returned non-URL: {short_url}")
            else:
                logger.error(f"ShrinkForge API error: {resp.status_code}")
    except Exception as e:
        logger.error(f"ShrinkForge request failed: {e}")
    return None

# ── User data functions (3 free videos per 12h, then ad unlocks 12h) ──────
def load_user_data():
    try:
        with open(USER_DATA_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_user_data(data):
    try:
        with open(USER_DATA_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

def get_user_state(user_id):
    data = load_user_data()
    uid = str(user_id)
    if uid not in data:
        return 0, 0
    return data[uid].get("count", 0), data[uid].get("unlock_until", 0)

def increment_free_count(user_id):
    data = load_user_data()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"count": 1, "unlock_until": 0}
    else:
        data[uid]["count"] = data[uid].get("count", 0) + 1
    save_user_data(data)

def set_unlocked(user_id, hours=12):
    data = load_user_data()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"count": 0, "unlock_until": time.time() + hours * 3600}
    else:
        data[uid]["unlock_until"] = time.time() + hours * 3600
        data[uid]["count"] = 0
    save_user_data(data)

def is_unlocked(user_id):
    data = load_user_data()
    uid = str(user_id)
    if uid not in data:
        return False
    return data[uid].get("unlock_until", 0) > time.time()

def can_use_free_video(user_id):
    if is_unlocked(user_id):
        return True
    count, _ = get_user_state(user_id)
    return count < 3

# ── Terabox download (public API + yt-dlp fallback) ────────────────────────
async def download_terabox(url, tmpdir):
    # Try public API first
    try:
        async with httpx.AsyncClient() as client:
            api_url = "https://terabox-worker.robinkumarshakya103.workers.dev/api"
            resp = await client.get(api_url, params={"url": url}, timeout=15)
            data = resp.json()
            if data.get("ok") and data.get("download_url"):
                download_link = data["download_url"]
                async with httpx.AsyncClient() as dl_client:
                    dl_resp = await dl_client.get(download_link, timeout=60)
                    file_path = os.path.join(tmpdir, "video.mp4")
                    with open(file_path, "wb") as f:
                        f.write(dl_resp.content)
                    size_mb = os.path.getsize(file_path) / 1024 / 1024
                    return file_path, size_mb
    except Exception as e:
        logger.warning(f"Public API failed: {e}")

    # Fallback: yt-dlp with ndus cookie
    cookie_content = f"# Netscape HTTP Cookie File\n.terabox.com\tTRUE\t/\tTRUE\t0\tndus\t{TERABOX_NDUS}\n"
    cookie_path = '/tmp/terabox_cookies.txt'
    with open(cookie_path, 'w') as f:
        f.write(cookie_content)
    opts = {
        'outtmpl': f'{tmpdir}/video.%(ext)s',
        'format': 'best',
        'quiet': False,
        'cookiefile': cookie_path,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [f for f in os.listdir(tmpdir) if f.startswith('video')]
        if not files:
            raise Exception("No file downloaded")
        file_path = os.path.join(tmpdir, files[0])
        size_mb = os.path.getsize(file_path) / 1024 / 1024
        return file_path, size_mb
    except Exception as e:
        logger.error(f"yt-dlp fallback failed: {e}")
        raise

# ── Telegram helpers ──────────────────────────────────────────────────────
def log_user(user_id, username):
    try:
        with open('/tmp/terabox_users.txt', 'a') as f:
            f.write(f"{user_id}|{username}\n")
    except:
        pass

async def log_to_channel(context, user_message, bot_message):
    try:
        await user_message.forward(LOG_CHANNEL_ID)
        await bot_message.forward(LOG_CHANNEL_ID)
    except:
        pass

# ── Command handlers ──────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.message.from_user.id
    username = update.message.from_user.username or "no_username"
    log_user(user_id, username)

    # Check for verification token from ShrinkForge redirect
    if context.args and context.args[0].startswith("verify_"):
        parts = context.args[0].split("_")
        if len(parts) == 3:
            token = parts[1]
            try:
                token_user_id = int(parts[2])
            except:
                token_user_id = None
            if token in pending_tokens and pending_tokens[token] == user_id and token_user_id == user_id:
                set_unlocked(user_id, hours=12)   # unlock 12 hours
                del pending_tokens[token]
                await update.message.reply_text(
                    "✅ *Ad watched! Thank you!*\n\n"
                    "You now have *12 hours* of unlimited Terabox downloads! 🎉\n"
                    "Send me any Terabox link to get started! 🚀",
                    parse_mode='Markdown'
                )
                return
            else:
                await update.message.reply_text("❌ Invalid or expired verification link. Please try again.")
                return

    # Normal start message
    await update.message.reply_text(
        "📥 *Terabox Video Downloader Bot*\n\n"
        "Send me a Terabox share link (e.g., terabox.com/s/...)\n\n"
        "🎁 *Free tier*: 3 videos per 12 hours\n"
        "⭐ *Watch an ad*: Unlock 12 hours of unlimited downloads\n\n"
        "Just paste a link! 🚀",
        parse_mode='Markdown'
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.from_user.id != ADMIN_ID:
        return
    try:
        with open('/tmp/terabox_users.txt', 'r') as f:
            users = set(line.split('|')[0] for line in f if line.strip())
        await update.message.reply_text(f"📊 Total users: {len(users)}")
    except:
        await update.message.reply_text("📊 Total users: 0")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()
    user_id = update.message.from_user.id
    username = update.message.from_user.username or "no_username"
    chat_type = update.message.chat.type
    log_user(user_id, username)

    if "terabox.com" not in url and "terabox.app" not in url:
        return

    start_time = time.time()
    is_group = chat_type in ('group', 'supergroup')

    # ── Ad gate (everyone, including groups and admin, must watch ads) ────
    if not can_use_free_video(user_id):
        # No free videos left and not unlocked → show ad
        token = str(uuid.uuid4())[:8]
        pending_tokens[token] = user_id
        deep_link = f"https://t.me/Terabox_Linkto_Video_bot?start=verify_{token}_{user_id}"
        ad_link = await get_shrinkforge_link(deep_link)
        if ad_link:
            keyboard = [[InlineKeyboardButton("📺 Watch Ad to Unlock 12h →", url=ad_link)]]
            await update.message.reply_text(
                "⚠️ *Daily free limit reached (3 videos in 12h)*\n\n"
                "👉 Watch a short ad to unlock **12 hours of unlimited downloads**!\n\n"
                "After the ad, you will be automatically redirected back here.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Fallback – ad system unavailable
            await update.message.reply_text(
                "❌ Ad system temporarily unavailable. Please try again later."
            )
        return

    # ── Download video (user still has free tries OR is unlocked) ───────────
    status_msg = await update.message.reply_text("⏳ Downloading Terabox video...")
    if not is_unlocked(user_id):
        increment_free_count(user_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            loop = asyncio.get_running_loop()
            file_path, size_mb = await loop.run_in_executor(executor, download_terabox, url, tmpdir)
            elapsed = round(time.time() - start_time, 1)
            caption = f"📱 *Terabox Video*\n⏱️ {elapsed}s 🔥\n🤖 @Terabox_Linkto_Video_bot"

            await status_msg.edit_text("✅ Sending...")
            with open(file_path, 'rb') as f:
                if size_mb < 49:
                    sent_msg = await update.message.reply_video(
                        f, caption=caption, parse_mode='Markdown',
                        read_timeout=120, write_timeout=120
                    )
                elif size_mb < 2000:
                    sent_msg = await update.message.reply_document(
                        f, caption=caption + "\n📦 (Sent as file)", parse_mode='Markdown',
                        read_timeout=180, write_timeout=180
                    )
                else:
                    await status_msg.edit_text("❌ Too large (>2GB)")
                    return
            await log_to_channel(context, update.message, sent_msg)
            await status_msg.delete()
        except Exception as e:
            logger.error(f"Download error: {e}")
            await status_msg.edit_text("❌ Failed to download. Link may be invalid or expired.")

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        return
    if not TERABOX_NDUS:
        logger.warning("TERABOX_NDUS not set – downloads may fail")
    if not SHRINKFORGE_API:
        logger.warning("SHRINKFORGE_API not set – ad system disabled")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    logger.info("Bot starting with polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
