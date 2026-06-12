import os
import json
import logging
import tempfile
import time
import asyncio
import httpx
import uuid
import threading
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Environment variables ─────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TERABOX_NDUS = os.environ.get("TERABOX_NDUS")
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "-1003956558170"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6294267891"))
SHRINKFORGE_API = os.environ.get("SHRINKFORGE_API")
SHRINKFORGE_API_URL = "https://shrinkforge.com/api"

USER_DATA_FILE = '/tmp/terabox_user_data.json'
pending_tokens = {}

executor = ThreadPoolExecutor(max_workers=4)

# ── ShrinkForge helper ───────────────────────────────────────────────
async def get_shrinkforge_link(destination_url: str) -> str | None:
    if not SHRINKFORGE_API:
        return None
    try:
        params = {"api": SHRINKFORGE_API, "url": destination_url, "format": "text"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(SHRINKFORGE_API_URL, params=params, timeout=10)
            if resp.status_code == 200:
                short_url = resp.text.strip()
                if short_url.startswith("http"):
                    return short_url
    except Exception as e:
        logger.error(f"ShrinkForge error: {e}")
    return None

# ── User data functions (3 free videos per 12h) ─────────────────────
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

# ── Direct Terabox extraction using ndus cookie (no external API) ─────
async def get_terabox_direct_link(share_url: str) -> str | None:
    """
    Extract direct download link from a Terabox share URL using the ndus cookie.
    Returns the direct video URL or None.
    """
    try:
        # Normalize URL: ensure it points to the sharing page
        if "1024terabox.com" in share_url:
            # Already a sharing link
            pass
        elif "terabox.com/s/" in share_url:
            # Already a sharing link
            pass
        else:
            # If a different domain, try to extract the share ID
            match = re.search(r'/s/([a-zA-Z0-9]+)', share_url)
            if not match:
                logger.error(f"Could not extract share ID from {share_url}")
                return None
            share_id = match.group(1)
            share_url = f"https://terabox.com/s/{share_id}"

        # Prepare cookies for the request
        cookies = {"ndus": TERABOX_NDUS}

        # Step 1: Get the share page to extract the file ID and sign
        async with httpx.AsyncClient(cookies=cookies, follow_redirects=True) as client:
            resp = await client.get(share_url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                logger.error(f"Failed to fetch share page: {resp.status_code}")
                return None

            # Extract the file ID from the page (look for "fs_id" or similar)
            # Modern Terabox pages embed data in a JSON blob.
            # Look for "window.yourData" or "window.initData"
            html = resp.text
            # Try to find the file ID in the HTML
            match = re.search(r'"fs_id"\s*:\s*(\d+)', html)
            if not match:
                # Alternative: look for file_id in a script tag
                match = re.search(r'"file_id"\s*:\s*"(\d+)"', html)
            if not match:
                logger.error("Could not find file ID in share page")
                return None
            file_id = match.group(1)

            # Extract the sign (short URL) – often the last part of the share link
            sign_match = re.search(r'/s/([a-zA-Z0-9]+)', share_url)
            sign = sign_match.group(1) if sign_match else None

            # Step 2: Call the Terabox download API
            api_url = "https://www.terabox.com/share/list"
            params = {
                "app_id": "250528",
                "web": "1",
                "channel": "dubox",
                "clienttype": "0",
                "jsToken": "",
                "page": "1",
                "num": "100",
                "order": "time",
                "desc": "1",
                "showmore": "1",
                "shorturl": sign or file_id,
                "root": "1",
            }
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.terabox.com/",
            }
            resp = await client.get(api_url, params=params, headers=headers)
            if resp.status_code != 200:
                logger.error(f"API call failed: {resp.status_code}")
                return None

            data = resp.json()
            if data.get("errno") != 0:
                logger.error(f"API error: {data.get('errmsg')}")
                return None

            # Extract the first file's dlink (download link)
            list_data = data.get("list", [])
            if not list_data:
                logger.error("No files found in share")
                return None

            dlink = list_data[0].get("dlink")
            if not dlink:
                logger.error("No dlink in response")
                return None

            return dlink

    except Exception as e:
        logger.error(f"Terabox extraction error: {e}")
        return None

# ── Download using direct extraction + yt-dlp fallback ───────────────
async def download_terabox(url, tmpdir):
    # 1. Try to get direct link via official Terabox API
    direct_url = await get_terabox_direct_link(url)
    if direct_url:
        logger.info(f"Got direct link from Terabox API")
        async with httpx.AsyncClient() as client:
            resp = await client.get(direct_url, timeout=90, follow_redirects=True)
            if resp.status_code == 200:
                file_path = os.path.join(tmpdir, "video.mp4")
                with open(file_path, "wb") as f:
                    f.write(resp.content)
                size_mb = os.path.getsize(file_path) / 1024 / 1024
                return file_path, size_mb
            else:
                logger.warning(f"Direct download failed with {resp.status_code}")

    # 2. Fallback: yt-dlp with ndus cookie
    cookie_content = f"# Netscape HTTP Cookie File\n.terabox.com\tTRUE\t/\tTRUE\t0\tndus\t{TERABOX_NDUS}\n"
    cookie_path = '/tmp/terabox_cookies.txt'
    with open(cookie_path, 'w') as f:
        f.write(cookie_content)
    opts = {
        'outtmpl': f'{tmpdir}/video.%(ext)s',
        'format': 'best',
        'quiet': False,
        'cookiefile': cookie_path,
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.terabox.com/'
        }
    }
    loop = asyncio.get_running_loop()
    def _sync_dl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [f for f in os.listdir(tmpdir) if f.startswith('video')]
        if not files:
            raise Exception("No file downloaded")
        file_path = os.path.join(tmpdir, files[0])
        size_mb = os.path.getsize(file_path) / 1024 / 1024
        return file_path, size_mb
    return await loop.run_in_executor(executor, _sync_dl)

# ── Telegram helpers (unchanged) ─────────────────────────────────────
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

# ── Handlers ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.message.from_user.id
    username = update.message.from_user.username or "no_username"
    log_user(user_id, username)

    if context.args and context.args[0].startswith("verify_"):
        parts = context.args[0].split("_")
        if len(parts) == 3:
            token = parts[1]
            try:
                token_user_id = int(parts[2])
            except:
                token_user_id = None
            if token in pending_tokens and pending_tokens[token] == user_id and token_user_id == user_id:
                set_unlocked(user_id, 12)
                del pending_tokens[token]
                await update.message.reply_text(
                    "✅ *Ad watched! Thank you!*\n\n"
                    "You now have *12 hours* of unlimited downloads! 🎉\n"
                    "Send any Terabox link to start! 🚀",
                    parse_mode='Markdown'
                )
                return
            else:
                await update.message.reply_text("❌ Invalid or expired link.")
                return

    await update.message.reply_text(
        "📥 *Terabox Video Downloader Bot*\n\n"
        "Send a Terabox share link.\n\n"
        "🎁 *Free*: 3 videos per 12 hours\n"
        "⭐ *Watch an ad*: Unlock 12 hours unlimited\n\n"
        "Paste a link! 🚀",
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
    log_user(user_id, username)

    if not any(domain in url for domain in ["terabox.com", "terabox.app", "1024terabox.com"]):
        return

    start_time = time.time()

    if not can_use_free_video(user_id):
        token = str(uuid.uuid4())[:8]
        pending_tokens[token] = user_id
        deep_link = f"https://t.me/Terabox_Linkto_Video_bot?start=verify_{token}_{user_id}"
        ad_link = await get_shrinkforge_link(deep_link)
        if ad_link:
            keyboard = [[InlineKeyboardButton("📺 Watch Ad to Unlock 12h →", url=ad_link)]]
            await update.message.reply_text(
                "⚠️ *Free limit reached (3 videos in 12h)*\n\n"
                "Watch an ad to unlock 12 hours of unlimited downloads.\n"
                "You'll be automatically redirected back after the ad.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text("❌ Ad system unavailable. Try later.")
        return

    status_msg = await update.message.reply_text("⏳ Downloading...")
    if not is_unlocked(user_id):
        increment_free_count(user_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            file_path, size_mb = await download_terabox(url, tmpdir)
            elapsed = round(time.time() - start_time, 1)
            caption = f"📱 *Terabox*\n⏱️ {elapsed}s 🔥\n🤖 @Terabox_Linkto_Video_bot"

            await status_msg.edit_text("✅ Sending...")
            with open(file_path, 'rb') as f:
                if size_mb < 49:
                    sent_msg = await update.message.reply_video(f, caption=caption, parse_mode='Markdown')
                elif size_mb < 2000:
                    sent_msg = await update.message.reply_document(f, caption=caption + "\n📦 (Sent as file)", parse_mode='Markdown')
                else:
                    await status_msg.edit_text("❌ Too large (>2GB)")
                    return
            await log_to_channel(context, update.message, sent_msg)
            await status_msg.delete()
        except Exception as e:
            logger.error(f"Download error: {e}")
            await status_msg.edit_text("❌ Failed. Link may be invalid or cookie expired.")

# ── Keep-alive web server ───────────────────────────────────────────
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_keep_alive():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(('0.0.0.0', port), KeepAliveHandler)
    server.serve_forever()

# ── Main ─────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        return
    if not SHRINKFORGE_API:
        logger.warning("SHRINKFORGE_API not set – ad system disabled")

    thread = threading.Thread(target=run_keep_alive, daemon=True)
    thread.start()
    logger.info(f"Keep-alive server running on port {os.environ.get('PORT', '8000')}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    logger.info("Bot started, polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
