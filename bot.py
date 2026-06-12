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

# ── Get dynamic parameters from share page ──────────────────────────
async def get_share_params(share_url: str) -> dict | None:
    """Extract sign, timestamp, shareid, uk from the share page."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        resp = await client.get(share_url, timeout=15)
        if resp.status_code != 200:
            logger.error(f"Failed to fetch share page: {resp.status_code}")
            return None
        html = resp.text
        # Look for the data embedded in a script tag
        # Typical pattern: window.yourData = {...}
        match = re.search(r'window\.yourData\s*=\s*({.+?});', html, re.DOTALL)
        if not match:
            # Alternative pattern
            match = re.search(r'window\.initData\s*=\s*({.+?});', html, re.DOTALL)
        if not match:
            # Fallback: look for a JSON object containing shareid, uk, sign
            match = re.search(r'"shareid"\s*:\s*(\d+).*?"uk"\s*:\s*(\d+).*?"sign"\s*:\s*"([^"]+)"', html, re.DOTALL)
            if match:
                shareid = match.group(1)
                uk = match.group(2)
                sign = match.group(3)
                timestamp = str(int(time.time()))
                return {"shareid": shareid, "uk": uk, "sign": sign, "timestamp": timestamp}
            logger.error("Could not extract share parameters from page")
            return None
        data = json.loads(match.group(1))
        # Navigate through the nested structure (depends on Terabox's current format)
        share_info = data.get("shareInfo") or data.get("share") or data
        shareid = share_info.get("shareid")
        uk = share_info.get("uk")
        sign = share_info.get("sign")
        if not shareid or not uk or not sign:
            # Try to find in list
            file_list = data.get("fileList", [])
            if file_list and len(file_list) > 0:
                shareid = file_list[0].get("shareid")
                uk = file_list[0].get("uk")
                sign = file_list[0].get("sign")
        if not shareid or not uk or not sign:
            logger.error("Missing required parameters (shareid, uk, sign) in extracted data")
            return None
        timestamp = str(int(time.time()))
        return {"shareid": str(shareid), "uk": str(uk), "sign": sign, "timestamp": timestamp}

# ── Get direct download link using the extracted parameters ─────────
async def get_terabox_direct_link(share_url: str) -> str | None:
    """Extract direct download link using full API flow."""
    try:
        # Step 1: Get dynamic parameters from share page
        params = await get_share_params(share_url)
        if not params:
            return None
        # Step 2: Call the list API with these parameters
        api_url = "https://www.terabox.com/share/list"
        query = {
            "shareid": params["shareid"],
            "uk": params["uk"],
            "sign": params["sign"],
            "timestamp": params["timestamp"],
            "page": "1",
            "num": "100",
            "order": "time",
            "desc": "1",
            "showmore": "1",
            "app_id": "250528",
            "web": "1",
            "channel": "dubox",
            "clienttype": "0",
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.terabox.com/",
        }
        cookies = {"ndus": TERABOX_NDUS}
        async with httpx.AsyncClient(cookies=cookies, timeout=10) as client:
            resp = await client.get(api_url, params=query, headers=headers)
            if resp.status_code != 200:
                logger.error(f"API returned {resp.status_code}")
                return None
            data = resp.json()
            if data.get("errno") != 0:
                logger.error(f"API error: {data.get('errmsg')} (errno {data.get('errno')})")
                return None
            files = data.get("list", [])
            if not files:
                logger.error("No files in share")
                return None
            dlink = files[0].get("dlink")
            if not dlink:
                logger.error("No dlink in response")
                return None
            return dlink
    except Exception as e:
        logger.error(f"Extraction error: {e}")
        return None

# ── Download using direct link or fallback ───────────────────────────
async def download_terabox(url, tmpdir):
    # 1. Try to get direct link via full API flow
    direct_link = await get_terabox_direct_link(url)
    if direct_link:
        logger.info("Downloading via direct link from API")
        async with httpx.AsyncClient() as client:
            resp = await client.get(direct_link, timeout=90, follow_redirects=True)
            if resp.status_code == 200:
                file_path = os.path.join(tmpdir, "video.mp4")
                with open(file_path, "wb") as f:
                    f.write(resp.content)
                size_mb = os.path.getsize(file_path) / 1024 / 1024
                return file_path, size_mb
            else:
                logger.warning(f"Direct download failed with {resp.status_code}")

    # 2. Fallback: yt-dlp with ndus cookie
    logger.warning("Falling back to yt-dlp")
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

# ── Keep‑alive web server (to prevent Render idle) ──────────────────
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

    # Start keep‑alive server
    thread = threading.Thread(target=run_keep_alive, daemon=True)
    thread.start()
    logger.info(f"Keep-alive server running on port {os.environ.get('PORT', '8000')}")

    # Force delete webhook to avoid conflict
    async def del_webhook():
        async with httpx.AsyncClient() as client:
            await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    asyncio.run(del_webhook())

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    logger.info("Bot started, polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
