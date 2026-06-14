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
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================== ENVIRONMENT VARIABLES ==================
BOT_TOKEN       = os.environ.get("BOT_TOKEN")
TERABOX_NDUS    = os.environ.get("TERABOX_NDUS")   # optional, for fallback
LOG_CHANNEL_ID  = int(os.environ.get("LOG_CHANNEL_ID", "-1003956558170"))
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "6294267891"))
SHRINKFORGE_API = os.environ.get("SHRINKFORGE_API", "")
BOT_USERNAME    = "Terabox_Linkto_Video_bot"

FREE_LIMIT      = 3
WINDOW_SECONDS  = 12 * 3600
UNLOCK_SECONDS  = 12 * 3600

USER_DATA_FILE  = "/tmp/terabox_user_data.json"
USERS_LOG_FILE  = "/tmp/terabox_users.txt"
DOWNLOAD_DIR    = "/tmp/terabox_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

pending_tokens = {}

TERABOX_DOMAINS = [
    "terabox.com", "1024terabox.com", "terabox.app", "teraboxapp.com",
    "nephobox.com", "freeterabox.com", "mirrobox.com", "momerybox.com",
    "tibibox.com", "1024tera.com"
]

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def is_terabox_url(url: str) -> bool:
    return any(domain in url for domain in TERABOX_DOMAINS)

# ================== USER DATA (unchanged) ==================
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

# ================== AD SYSTEM ==================
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
    except:
        pass
    return deep_link

# ================== MULTI-API DOWNLOADER ==================
# List of known working free Terabox APIs (verified June 2026)
API_LIST = [
    "https://terabox.howdownload.com/api?url=",
    "https://terabox-dl.vercel.app/api?url=",
    "https://terabox-api.vercel.app/api?url=",
    "https://tera-api.vercel.app/api?url=",
    "https://terabox.vercel.app/api?url=",
]

async def try_api_download(share_url: str) -> Optional[str]:
    for api_base in API_LIST:
        try:
            full_url = api_base + share_url
            logger.info(f"Trying API: {api_base}")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(full_url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                # Handle different response formats
                download_url = None
                if data.get("success") and data.get("download_url"):
                    download_url = data["download_url"]
                elif data.get("ok") and data.get("downloadLink"):
                    download_url = data["downloadLink"]
                elif data.get("download_link"):
                    download_url = data["download_link"]
                elif data.get("url"):
                    download_url = data["url"]
                elif isinstance(data, str) and data.startswith("http"):
                    download_url = data
                if download_url and download_url.startswith("http"):
                    out_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex}.mp4")
                    async with httpx.AsyncClient(follow_redirects=True, timeout=300) as dl_client:
                        async with dl_client.stream("GET", download_url) as r:
                            r.raise_for_status()
                            with open(out_path, "wb") as f:
                                async for chunk in r.aiter_bytes(chunk_size=8192):
                                    f.write(chunk)
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                        logger.info(f"API success: {api_base}")
                        return out_path
        except Exception as e:
            logger.warning(f"API {api_base} failed: {e}")
            continue
    return None

async def download_terabox_video(share_url: str) -> Optional[str]:
    # Try all free APIs first
    result = await try_api_download(share_url)
    if result:
        return result

    # Fallback to yt-dlp with ndus cookie
    if TERABOX_NDUS:
        logger.info("All APIs failed, falling back to yt-dlp")
        cookie_content = f"# Netscape HTTP Cookie File\n.terabox.com\tTRUE\t/\tTRUE\t0\tndus\t{TERABOX_NDUS}\n"
        cookie_path = "/tmp/terabox_cookies.txt"
        with open(cookie_path, "w") as f:
            f.write(cookie_content)
        ydl_opts = {
            "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "quiet": True,
            "no_warnings": True,
            "cookiefile": cookie_path,
            "http_headers": BROWSER_HEADERS,
            "extractor_args": {"generic": {"impersonate": "chrome"}},
        }
        loop = asyncio.get_running_loop()
        def _run():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(share_url, download=True)
                    if info:
                        filename = ydl.prepare_filename(info)
                        if os.path.exists(filename):
                            return filename
                        for ext in ("mp4", "mkv", "webm"):
                            alt = os.path.splitext(filename)[0] + "." + ext
                            if os.path.exists(alt):
                                return alt
            except:
                pass
            return None
        file_path = await loop.run_in_executor(None, _run)
        if file_path:
            return file_path

    return None

# ================== LOG CHANNEL ==================
async def log_to_channel(context: ContextTypes.DEFAULT_TYPE, user, msg_in: str, msg_out: str):
    try:
        user_info = f"👤 {user.full_name} (@{user.username or 'no_username'}) [ID: {user.id}]"
        log_text = f"{user_info}\n📩 User: {msg_in[:300]}\n🤖 Bot: {msg_out[:300]}"
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_text)
    except:
        pass

# ================== HANDLERS ==================
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
                await update.message.reply_text("⚠️ This link is not for your account.")
                return
            if verify_token(token, user_id):
                msg = "✅ *Ad verified!* You now have **12 hours unlimited downloads**.\n\nSend me a Terabox link!"
                await update.message.reply_text(msg, parse_mode="Markdown")
                await log_to_channel(context, user, "/start verify", "Unlock granted")
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
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized.")
        return
    total = get_total_users()
    msg = f"📊 *Stats*\n👥 Users: {total}\n🔑 Tokens: {len(pending_tokens)}"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()
    if not is_terabox_url(text):
        await update.message.reply_text("🔗 Send a valid Terabox link.")
        return
    status = get_user_status(user_id)
    if not status["can_download"]:
        token = generate_verify_token(user_id)
        deep_link = f"https://t.me/{BOT_USERNAME}?start=verify_{token}_{user_id}"
        ad_link = await create_ad_link(deep_link)
        h, m = divmod(status["reset_in"], 60)
        h //= 60
        m %= 60
        msg = f"⚠️ *Limit reached!*\nResets in {h}h {m}m.\nWatch ad for 12h unlimited!"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Watch Ad", url=ad_link)]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
        await log_to_channel(context, user, text, "Limit reached - ad sent")
        return

    proc = await update.message.reply_text("⬇️ *Downloading...*", parse_mode="Markdown")
    file_path = None
    try:
        file_path = await download_terabox_video(text)
        if not file_path or not os.path.exists(file_path):
            raise Exception("No file")
        size_mb = os.path.getsize(file_path) / 1024 / 1024
        if size_mb > 49:
            await proc.edit_text(f"⚠️ File too large ({size_mb:.1f} MB)")
            return
        if not status["is_unlocked"]:
            increment_download_count(user_id)
        await proc.edit_text("📤 *Uploading...*", parse_mode="Markdown")
        caption = f"🎬 *{os.path.basename(file_path)}*\n📦 {size_mb:.1f} MB\n_Downloaded by @{BOT_USERNAME}_"
        with open(file_path, "rb") as f:
            await update.message.reply_video(video=f, caption=caption, parse_mode="Markdown")
        await proc.delete()
        new_status = get_user_status(user_id)
        remaining = f"📊 {new_status['remaining']}/{FREE_LIMIT} left" if not new_status["is_unlocked"] else f"🔓 Unlimited — {int((new_status['unlock_until']-time.time())/3600)}h left"
        await update.message.reply_text(remaining)
        await log_to_channel(context, user, text, f"Sent ({size_mb:.1f} MB)")
    except Exception as e:
        logger.error(f"Error: {e}")
        await proc.edit_text("❌ *Download failed.*\n\nAll APIs failed. Please try again later.")
        await log_to_channel(context, user, text, f"Error: {str(e)[:100]}")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")

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
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        return
    threading.Thread(target=run_keep_alive, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("Starting polling...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
