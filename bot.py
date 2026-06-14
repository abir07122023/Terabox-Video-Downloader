import os
import re
import json
import time
import uuid
import asyncio
import logging
import threading
import random
import string
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "-1001234567890"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
SHRINKFORGE_API = os.environ.get("SHRINKFORGE_API", "")
BOT_USERNAME = "Terabox_Linkto_Video_bot"

FREE_LIMIT = 3
WINDOW_SECONDS = 12 * 3600
UNLOCK_SECONDS = 12 * 3600
USER_DATA_FILE = "/tmp/terabox_user_data.json"
USERS_LOG_FILE = "/tmp/terabox_users.txt"
DOWNLOAD_DIR = "/tmp/terabox_downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

pending_tokens = {}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.terabox.app/",
}

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

def is_terabox_url(url: str) -> bool:
    return "terabox" in url.lower() or "1024tera" in url.lower()

async def extract_share_page_params(share_url: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=15) as client:
            resp = await client.get(share_url, follow_redirects=True)
            html = resp.text
            
            jstoken_match = re.search(r'jsToken["\s:=]+["\']([A-F0-9]+)["\']', html, re.IGNORECASE)
            jstoken = jstoken_match.group(1) if jstoken_match else "D3518032E0D7352EF12869D0A069B4AB1559DC99AE4CE1522A6EA2EAFF0945EBAEEAF996DD9F4A182AF49E9EFF4340953F7DF14C0395FF056A53E1251A0B720434FAF215156AC733A6FDFF440BD3F6B4C3BE31F62A428EFCED33102B4BA34329"
            
            surl_match = re.search(r'/s/([A-Za-z0-9_\-]+)', share_url)
            surl = surl_match.group(1) if surl_match else None
            
            if not surl:
                return None
            
            return {"jsToken": jstoken, "surl": surl}
    except Exception as e:
        logger.error(f"Failed to extract params: {e}")
        return None

async def get_terabox_dlink(share_url: str) -> Optional[dict]:
    params = await extract_share_page_params(share_url)
    if not params:
        return None
    
    surl = params["surl"]
    jstoken = params["jsToken"]
    
    api_url = "https://www.terabox.app/api/shorturlinfo"
    
    query_params = {
        "clientfrom": "h5",
        "psign": "0",
        "pcftoken": "c9833e7b083bb179f2cfa8eb8ea43fa5",
        "clienttype": "0",
        "channel": "dubox",
        "shorturl": surl,
        "root": "1",
        "scene": "",
        "app_id": "250528",
        "web": "1",
        "jsToken": jstoken,
        "dp-logid": "".join(random.choices(string.digits, k=20)),
    }
    
    try:
        async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=20) as client:
            resp = await client.get(api_url, params=query_params)
            data = resp.json()
            
            if data.get("errno") != 0:
                logger.error(f"API error: {data.get('errno')}")
                return None
            
            file_list = data.get("list", [])
            if not file_list:
                return None
            
            file_info = file_list[0]
            filename = file_info.get("server_filename", "video.mp4")
            
            dlink = file_info.get("dlink")
            fs_id = file_info.get("fs_id")
            
            if dlink:
                return {"dlink": dlink, "filename": filename, "size": int(file_info.get("size", 0))}
            
            logger.error("No dlink in response")
            return None
            
    except Exception as e:
        logger.error(f"Terabox API failed: {e}")
        return None

async def download_file(dlink: str, filename: str) -> Optional[str]:
    out_path = os.path.join(DOWNLOAD_DIR, filename.replace("/", "_")[:100])
    
    try:
        async with httpx.AsyncClient(headers=BROWSER_HEADERS, follow_redirects=True, timeout=300) as client:
            async with client.stream("GET", dlink) as r:
                r.raise_for_status()
                with open(out_path, "wb") as fp:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 512):
                        fp.write(chunk)
        
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            return out_path
    except Exception as e:
        logger.error(f"Download failed: {e}")
    
    return None

def generate_verify_token(user_id: int) -> str:
    token = uuid.uuid4().hex[:16].upper()
    pending_tokens[token] = {"user_id": user_id, "expires": time.time() + 3600}
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

async def log_to_channel(context: ContextTypes.DEFAULT_TYPE, user, msg_in: str, msg_out: str):
    try:
        user_info = f"👤 {user.full_name} (@{user.username or 'none'}) [ID: {user.id}]"
        log_text = f"{user_info}\n📩 {msg_in[:150]}\n🤖 {msg_out[:150]}"
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_text)
    except:
        pass

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    log_user(user_id)
    
    args = context.args
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
    
    status = get_user_status(user_id)
    welcome = (
        f"👋 Hi *{user.first_name}*!\n\n"
        f"📦 I download from **Terabox**\n\n"
        f"🎥 Send me a Terabox link!\n\n"
        f"📊 Status: *{status['remaining']}/{FREE_LIMIT}* free downloads"
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
    
    proc_msg = await update.message.reply_text("⬇️ *Downloading...*", parse_mode="Markdown")
    
    try:
        info = await get_terabox_dlink(text)
        if not info:
            await proc_msg.edit_text("❌ *Failed to get download link*")
            await log_to_channel(context, user, text, "Failed")
            return
        
        file_path = await download_file(info["dlink"], info["filename"])
        
        if not file_path or not os.path.exists(file_path):
            await proc_msg.edit_text("❌ *Download failed*")
            await log_to_channel(context, user, text, "Failed")
            return
        
        size_mb = os.path.getsize(file_path) / 1024 / 1024
        if size_mb > 49:
            await proc_msg.edit_text(f"⚠️ File too large ({size_mb:.1f} MB)")
            return
        
        if not status["is_unlocked"]:
            increment_download_count(user_id)
        
        await proc_msg.edit_text("📤 *Uploading...*", parse_mode="Markdown")
        
        caption = f"🎬 Terabox\n📦 {size_mb:.1f} MB"
        with open(file_path, "rb") as vf:
            await update.message.reply_video(video=vf, caption=caption, parse_mode="Markdown", supports_streaming=True)
        
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
