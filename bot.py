"""
╔══════════════════════════════════════════════════════════════════╗
║          TERABOX TELEGRAM BOT — PRODUCTION READY                 ║
║          @Terabox_Linkto_Video_bot                               ║
║                                                                  ║
║  Features:                                                       ║
║  • Full Terabox extraction (share page → params → dlink)        ║
║  • yt-dlp fallback                                               ║
║  • Freemium: 3 free / 12h sliding window                        ║
║  • ShrinkForge ad unlock system                                  ║
║  • Log channel forwarding                                        ║
║  • Admin /stats command                                          ║
║  • Keep-alive web server for Render                              ║
║  • No Conflict errors                                            ║
╚══════════════════════════════════════════════════════════════════╝

DEPLOY CHECKLIST (Render):
  ✅ Set WEB_CONCURRENCY=1  ← IMPORTANT, prevents duplicate pollers
  ✅ Start command: python bot.py
  ✅ All env vars set (see table below)

ENV VARS:
  BOT_TOKEN        - Telegram bot token
  TERABOX_NDUS     - Your Terabox account ndus cookie value
  LOG_CHANNEL_ID   - Private log channel (negative ID)
  ADMIN_ID         - Your Telegram user ID
  SHRINKFORGE_API  - ShrinkForge API key
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
import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────
BOT_TOKEN       = os.environ.get("BOT_TOKEN",       "8864865531:AAHjVi2ybEuosLEfStT0zwfcBTAUIjdFFEc")
TERABOX_NDUS    = os.environ.get("TERABOX_NDUS",    "YyDhw33teHuizcOL6HYEuK5ztMi1qKKoQ9QcwaS4")
LOG_CHANNEL_ID  = int(os.environ.get("LOG_CHANNEL_ID", "-1003956558170"))
ADMIN_ID        = int(os.environ.get("ADMIN_ID",    "6294267891"))
SHRINKFORGE_API = os.environ.get("SHRINKFORGE_API", "23f12fc648e44117a4fd3a85030aed862651f6ff")
BOT_USERNAME    = "Terabox_Linkto_Video_bot"

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
FREE_LIMIT      = 3          # free downloads per window
WINDOW_SECONDS  = 12 * 3600  # 12 hours
UNLOCK_SECONDS  = 12 * 3600  # ad unlock duration

USER_DATA_FILE  = "/tmp/terabox_user_data.json"
USERS_LOG_FILE  = "/tmp/terabox_users.txt"
DOWNLOAD_DIR    = "/tmp/terabox_downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─────────────────────────────────────────────
# IN-MEMORY STORES
# ─────────────────────────────────────────────
# pending_tokens: { "TOKEN": {"user_id": int, "expires": float} }
pending_tokens: dict = {}

# ─────────────────────────────────────────────
# HEADERS — mimic a real browser visiting Terabox
# ─────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Terabox domains map — all redirect/resolve to terabox.com API
TERABOX_DOMAINS = [
    "terabox.com",
    "1024terabox.com",
    "terabox.app",
    "teraboxapp.com",
    "nephobox.com",
    "freeterabox.com",
    "mirrobox.com",
    "momerybox.com",
    "tibibox.com",
    "1024tera.com",
]


# ══════════════════════════════════════════════════════════════════
# SECTION 1: USER DATA PERSISTENCE
# ══════════════════════════════════════════════════════════════════

def load_user_data() -> dict:
    """Load user data from JSON file."""
    try:
        with open(USER_DATA_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_user_data(data: dict) -> None:
    """Persist user data to JSON file."""
    with open(USER_DATA_FILE, "w") as f:
        json.dump(data, f)


def log_user(user_id: int) -> None:
    """Add user to the unique users log file (no duplicates)."""
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
    """Return total unique user count."""
    try:
        with open(USERS_LOG_FILE, "r") as f:
            return len([l for l in f.read().splitlines() if l.strip()])
    except FileNotFoundError:
        return 0


def get_user_status(user_id: int) -> dict:
    """
    Returns:
      {
        "can_download": bool,
        "is_unlocked": bool,       # ad-unlock active
        "remaining": int,          # free downloads left (if not unlocked)
        "reset_in": int,           # seconds until window resets
        "unlock_until": float,     # timestamp when unlock expires
      }
    """
    data = load_user_data()
    uid  = str(user_id)
    now  = time.time()

    entry = data.get(uid, {"count": 0, "window_start": now, "unlock_until": 0})

    # Check if ad-unlock is still active
    if entry.get("unlock_until", 0) > now:
        return {
            "can_download": True,
            "is_unlocked":  True,
            "remaining":    999,
            "reset_in":     0,
            "unlock_until": entry["unlock_until"],
        }

    # Check if the 12-hour window has expired → reset counter
    window_start = entry.get("window_start", now)
    if now - window_start >= WINDOW_SECONDS:
        entry["count"]        = 0
        entry["window_start"] = now
        data[uid]             = entry
        save_user_data(data)

    count     = entry.get("count", 0)
    remaining = max(0, FREE_LIMIT - count)
    reset_in  = max(0, int(WINDOW_SECONDS - (now - window_start)))

    return {
        "can_download": remaining > 0,
        "is_unlocked":  False,
        "remaining":    remaining,
        "reset_in":     reset_in,
        "unlock_until": 0,
    }


def increment_download_count(user_id: int) -> None:
    """Bump the download counter for a user."""
    data = load_user_data()
    uid  = str(user_id)
    now  = time.time()

    entry = data.get(uid, {"count": 0, "window_start": now, "unlock_until": 0})

    # Reset if window expired
    if now - entry.get("window_start", now) >= WINDOW_SECONDS:
        entry["count"]        = 0
        entry["window_start"] = now

    entry["count"] = entry.get("count", 0) + 1
    data[uid]      = entry
    save_user_data(data)


def unlock_user(user_id: int) -> None:
    """Grant 12-hour unlimited access after ad watch."""
    data         = load_user_data()
    uid          = str(user_id)
    now          = time.time()
    entry        = data.get(uid, {"count": 0, "window_start": now, "unlock_until": 0})
    entry["unlock_until"]   = now + UNLOCK_SECONDS
    entry["count"]          = 0   # reset free counter too
    entry["window_start"]   = now
    data[uid]               = entry
    save_user_data(data)


# ══════════════════════════════════════════════════════════════════
# SECTION 2: TERABOX VIDEO EXTRACTION
# ══════════════════════════════════════════════════════════════════

def is_terabox_url(url: str) -> bool:
    """Check if a URL belongs to any known Terabox domain."""
    return any(domain in url for domain in TERABOX_DOMAINS)


async def resolve_short_url(url: str, client: httpx.AsyncClient) -> str:
    """
    Follow redirects to get the final URL.
    Some Terabox share links redirect before showing the share page.
    """
    try:
        resp = await client.get(url, follow_redirects=True, timeout=15)
        return str(resp.url)
    except Exception:
        return url


async def fetch_share_page_params(share_url: str, client: httpx.AsyncClient) -> Optional[dict]:
    """
    STEP 1 — Fetch the Terabox share page HTML.
    STEP 2 — Extract dynamic JS parameters: jsToken, sign, timestamp, shareid, uk, surl.

    Uses multiple strategies to extract from various page structures.
    """
    logger.info(f"Fetching share page: {share_url}")

    params = {}

    # ── Extract surl BEFORE URL redirects change it ──────────────────
    # Try /s/ format first
    surl_match = re.search(r"/s/([A-Za-z0-9_\-]+)", share_url)
    if surl_match:
        params["surl"] = surl_match.group(1)

    try:
        resp = await client.get(
            share_url,
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            timeout=20,
        )
        html      = resp.text
        final_url = str(resp.url)
    except Exception as e:
        logger.error(f"Failed to fetch share page: {e}")
        return None

    logger.info(f"Final URL after redirects: {final_url}")

    # ── Extract surl from query params if not found in original URL ──
    # After redirect, might be surl=xxxxx in query string
    if not params.get("surl"):
        query_match = re.search(r"[?&]surl=([A-Za-z0-9_\-]+)", final_url)
        if query_match:
            params["surl"] = query_match.group(1)

    # ── STRATEGY 1: Look for window.yunData = {...} ────────────────
    # Try greedy first, then non-greedy
    yun_matches = [
        re.search(r"window\.yunData\s*=\s*(\{[^}]*\"shareid\"[^}]*\});?", html),
        re.search(r"window\.yunData\s*=\s*(\{.+?\})\s*;", html, re.DOTALL),
        re.search(r"yunData\s*=\s*(\{.+?\})\s*;", html, re.DOTALL),
    ]
    
    for yun_match in yun_matches:
        if yun_match:
            try:
                yun_str = yun_match.group(1)
                # Clean up common issues
                yun_str = yun_str.replace('\\"', '"')
                yun_data = json.loads(yun_str)
                params.update({
                    "shareid":   str(yun_data.get("shareid", "")),
                    "uk":        str(yun_data.get("uk", "")),
                    "sign":      str(yun_data.get("sign", "")),
                    "timestamp": str(yun_data.get("timestamp", "")),
                })
                logger.info(f"[S1] Extracted yunData: shareid={params.get('shareid')}, uk={params.get('uk')}")
                break
            except (json.JSONDecodeError, AttributeError) as e:
                logger.debug(f"yunData parse failed: {e}")
                continue

    # ── STRATEGY 2: Extract as separate assignments ──────────────────
    # Look for patterns like: window.jsToken = "..." or jsToken: "..."
    if not params.get("jsToken"):
        token_patterns = [
            r'window\.jsToken\s*=\s*["\']([^"\']+)["\']',
            r'jsToken\s*:\s*["\']([^"\']+)["\']',
            r'fn\(["\']([^"\']+)["\']\)',
        ]
        for pattern in token_patterns:
            match = re.search(pattern, html)
            if match:
                params["jsToken"] = match.group(1)
                logger.info(f"[S2] Found jsToken: {params['jsToken'][:20]}...")
                break

    # ── STRATEGY 3: Extract individual params from inline JS ────────
    for key in ("shareid", "uk", "sign", "timestamp"):
        if not params.get(key):
            patterns = [
                rf'window\.{key}\s*=\s*["\']?([A-Za-z0-9_\-]+)["\']?',
                rf'{key}["\']?\s*:\s*["\']?([A-Za-z0-9_\-]+)["\']?',
                rf'["\']?{key}["\']?\s*=\s*["\']?([A-Za-z0-9_\-]+)["\']?',
            ]
            for pattern in patterns:
                match = re.search(pattern, html)
                if match:
                    params[key] = match.group(1)
                    logger.debug(f"[S3] Found {key}: {match.group(1)[:30]}...")
                    break

    # ── STRATEGY 4: Parse all JSON blobs in <script> tags ───────────
    script_jsons = re.findall(r"<script[^>]*>\s*(\{.+?\})\s*</script>", html, re.DOTALL)
    for script_json in script_jsons:
        try:
            obj = json.loads(script_json)
            if "shareid" in obj and "uk" in obj:
                params.update({
                    "shareid":   str(obj.get("shareid", "")),
                    "uk":        str(obj.get("uk", "")),
                    "sign":      str(obj.get("sign", "")),
                    "timestamp": str(obj.get("timestamp", "")),
                })
                logger.info(f"[S4] Extracted from script JSON: shareid={params.get('shareid')}")
                break
        except json.JSONDecodeError:
            continue

    logger.info(f"Final extracted params: {list(params.keys())}")
    logger.debug(f"Param values: surl={params.get('surl')}, shareid={params.get('shareid')}, uk={params.get('uk')}, sign={params.get('sign')}")

    # Return if we have at least surl + one other param
    if params.get("surl") and (params.get("shareid") or params.get("uk") or params.get("jsToken")):
        return params
    
    logger.warning(f"Incomplete params extracted: {params}")
    return None


async def get_terabox_dlink(share_url: str) -> Optional[dict]:
    """
    Full Terabox extraction pipeline:

    1. Fetch share page HTML
    2. Extract dynamic params (jsToken, sign, timestamp, shareid, uk, surl)
    3. Call the Terabox share/list API with those params + ndus cookie
    4. Return dict with 'dlink', 'filename', 'size'

    Falls back to external Terabox API service if primary method fails.
    """
    cookies = {"ndus": TERABOX_NDUS}

    async with httpx.AsyncClient(
        cookies=cookies,
        headers=BROWSER_HEADERS,
        follow_redirects=True,
        timeout=30,
    ) as client:

        # ── Resolve any short/redirect URLs ─────────────────────────
        share_url = await resolve_short_url(share_url, client)

        # ── STEP 1+2: Fetch page & extract params ───────────────────
        params = await fetch_share_page_params(share_url, client)
        if not params:
            logger.error("Failed to extract params from share page")
            # Try fallback API
            return await try_fallback_terabox_api(share_url)

        surl      = params.get("surl", "")
        js_token  = params.get("jsToken", "")
        sign      = params.get("sign", "")
        timestamp = params.get("timestamp", "")
        shareid   = params.get("shareid", "")
        uk        = params.get("uk", "")

        logger.info(
            f"Extracted params: surl={surl}, uk={uk}, sign={sign[:20] if sign else 'N/A'}, shareid={shareid}"
        )

        # ── STEP 3: Call the Terabox share/list API ──────────────────
        # Build minimal API call - Terabox MIGHT accept just surl + cookie
        api_url = "https://www.terabox.com/share/list"
        api_params = {
            "app_id":         "250528",
            "web":            "1",
            "channel":        "dubox",
            "clienttype":     "0",
            "page":           "1",
            "num":            "20",
            "shorturl":       surl,
            "root":           "1",
        }

        # Add optional params only if we have them
        if js_token:
            api_params["jsToken"] = js_token
        if sign:
            api_params["sign"] = sign
        if timestamp:
            api_params["timestamp"] = timestamp
        if shareid:
            api_params["shareid"] = shareid
        if uk:
            api_params["uk"] = uk
        if sign and uk:
            # If we have sign + uk, also try setting randsk which Terabox often uses
            import hashlib
            randsk = hashlib.md5(f"{uk}{sign}".encode()).hexdigest()
            api_params["randsk"] = randsk

        logger.info(f"Primary API call with {len(api_params)} params")

        try:
            api_resp = await client.get(
                api_url,
                params=api_params,
                headers={
                    **BROWSER_HEADERS,
                    "Referer": share_url if share_url else "https://www.terabox.com/",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=20,
            )
            api_data = api_resp.json()
            logger.info(f"Primary API errno: {api_data.get('errno', 'unknown')}")
        except Exception as e:
            logger.error(f"Primary API request failed: {e}")
            # Try fallback
            return await try_fallback_terabox_api(share_url)

        # ── STEP 4: Parse response ────────────────────────────────────
        if api_data.get("errno") == 0:
            file_list = api_data.get("list", [])
            if file_list:
                result = extract_dlink_from_list(file_list)
                if result:
                    logger.info(f"Primary API success: {result['filename']}")
                    return result

        logger.warning(f"Primary API failed or empty (errno={api_data.get('errno')}). Trying fallback...")
        return await try_fallback_terabox_api(share_url)


async def try_fallback_terabox_api(share_url: str) -> Optional[dict]:
    """
    Fallback strategies:
    1. Try calling Terabox API with JUST surl parameter
    2. Try external Terabox download APIs
    3. Try direct HEAD request to see if we get redirected to download
    """
    logger.info("Attempting fallback strategies...")

    # Extract surl
    surl_match = re.search(r"/s/([A-Za-z0-9_\-]+)", share_url)
    if not surl_match:
        surl_match = re.search(r"[?&]surl=([A-Za-z0-9_\-]+)", share_url)
    
    if not surl_match:
        return None
    
    surl = surl_match.group(1)

    # ── STRATEGY 1: Try API with just surl ────────────────────────
    logger.info(f"[FB1] Trying Terabox API with minimal params (surl only)...")
    try:
        async with httpx.AsyncClient(
            cookies={"ndus": TERABOX_NDUS},
            headers=BROWSER_HEADERS,
            timeout=20,
        ) as fallback_client:
            minimal_params = {
                "app_id":     "250528",
                "web":        "1",
                "channel":    "dubox",
                "shorturl":   surl,
                "root":       "1",
            }
            resp = await fallback_client.get(
                "https://www.terabox.com/share/list",
                params=minimal_params,
            )
            data = resp.json()
            
            if data.get("errno") == 0:
                file_list = data.get("list", [])
                if file_list:
                    result = extract_dlink_from_list(file_list)
                    if result:
                        logger.info(f"[FB1] Success: {result['filename']}")
                        return result
    except Exception as e:
        logger.debug(f"[FB1] Failed: {e}")

    # ── STRATEGY 2: External Terabox extraction APIs ────────────────
    logger.info(f"[FB2] Trying external Terabox APIs...")
    
    external_apis = [
        # Format: (endpoint_template, response_key)
        (f"https://terabox.app/api/get?link=https://1024terabox.com/s/{surl}", "list"),
        (f"https://teraboxes.com/api?link=https://1024terabox.com/s/{surl}", "data"),
        (f"https://terabox-api.com/api?url=https://1024terabox.com/s/{surl}", "files"),
    ]
    
    for endpoint, key in external_apis:
        try:
            async with httpx.AsyncClient(timeout=15) as ext_client:
                resp = await ext_client.get(endpoint, headers=BROWSER_HEADERS)
                data = resp.json()
                
                file_list = data.get(key) or data.get("list") or data.get("data") or []
                if isinstance(file_list, dict) and "dlink" in file_list:
                    file_list = [file_list]
                
                if file_list:
                    result = extract_dlink_from_list(file_list)
                    if result:
                        logger.info(f"[FB2] Success with {endpoint}: {result['filename']}")
                        return result
        except Exception as e:
            logger.debug(f"[FB2] {endpoint} failed: {e}")

    # ── STRATEGY 3: Try HEAD request to see redirect ────────────────
    # Some Terabox setups might redirect directly to download if properly authenticated
    logger.info(f"[FB3] Trying direct share page download...")
    try:
        async with httpx.AsyncClient(
            cookies={"ndus": TERABOX_NDUS},
            headers={
                **BROWSER_HEADERS,
                "Range": "bytes=0-1",  # Request just 1 byte to trigger download
            },
            follow_redirects=False,
            timeout=10,
        ) as dl_client:
            resp = await dl_client.head(share_url)
            # Check for redirect to download URL
            if resp.status_code in (301, 302, 303, 307, 308):
                download_url = resp.headers.get("Location")
                if download_url and ("download" in download_url or "dlink" in download_url):
                    logger.info(f"[FB3] Got redirect to: {download_url[:80]}")
                    return {
                        "dlink": download_url,
                        "filename": f"terabox_{surl}.mp4",
                        "size": 0,
                    }
    except Exception as e:
        logger.debug(f"[FB3] Failed: {e}")

    logger.error("All fallback strategies exhausted")
    return None


def extract_dlink_from_list(file_list: list) -> Optional[dict]:
    """
    Extract dlink, filename, size from a file list.
    Handles various response formats from different APIs.
    """
    if not file_list:
        return None

    # Prefer video files
    target = None
    for f in file_list:
        if isinstance(f, dict):
            category = str(f.get("category", ""))
            if category == "1":  # 1 = video
                target = f
                break

    if not target:
        target = file_list[0] if file_list else None

    if not target:
        return None

    # Handle different key names from different APIs
    dlink = (
        target.get("dlink") or
        target.get("download_link") or
        target.get("link") or
        ""
    )
    filename = (
        target.get("server_filename") or
        target.get("filename") or
        target.get("name") or
        "video.mp4"
    )
    size = int(target.get("size", 0))

    if not dlink:
        logger.error("No dlink in file entry")
        return None

    logger.info(f"Extracted: {filename} ({size // 1024 // 1024} MB) from dlink: {dlink[:60]}...")
    return {"dlink": dlink, "filename": filename, "size": size}


async def download_terabox_video(share_url: str) -> Optional[str]:
    """
    Download a Terabox video and return local file path.

    Strategy:
      1. Try native Terabox API extraction → httpx download
      2. Fall back to yt-dlp with ndus cookie + proper headers
    """
    # ── Attempt 1: Native API extraction ────────────────────────────
    info = await get_terabox_dlink(share_url)

    if info and info.get("dlink"):
        dlink    = info["dlink"]
        filename = sanitize_filename(info["filename"])
        out_path = os.path.join(DOWNLOAD_DIR, filename)

        logger.info(f"Downloading via dlink: {dlink[:80]}...")
        try:
            async with httpx.AsyncClient(
                cookies={"ndus": TERABOX_NDUS},
                headers={
                    **BROWSER_HEADERS,
                    "Referer": "https://www.terabox.com/",
                },
                follow_redirects=True,
                timeout=300,  # 5 min for large files
            ) as dl_client:
                async with dl_client.stream("GET", dlink) as r:
                    r.raise_for_status()
                    with open(out_path, "wb") as fp:
                        async for chunk in r.aiter_bytes(chunk_size=1024 * 512):  # 512 KB chunks
                            fp.write(chunk)

            if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                logger.info(f"Download complete: {out_path} ({os.path.getsize(out_path) // 1024} KB)")
                return out_path
            else:
                logger.warning("Downloaded file is empty or too small")
        except Exception as e:
            logger.error(f"dlink download failed: {e}")

    # ── Attempt 2: yt-dlp fallback ───────────────────────────────────
    logger.warning("Falling back to yt-dlp...")
    return await download_via_ytdlp(share_url)


async def download_via_ytdlp(url: str) -> Optional[str]:
    """
    yt-dlp download with Terabox-specific options.
    Runs in a thread pool to avoid blocking the event loop.
    """
    out_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")

    ydl_opts = {
        "outtmpl":         out_template,
        "format":          "best[ext=mp4]/best",
        "quiet":           True,
        "no_warnings":     True,
        "nocheckcertificate": True,
        "cookiesfrombrowser": None,
        "http_headers": {
            "User-Agent": BROWSER_HEADERS["User-Agent"],
            "Referer":    url,
            "Cookie":     f"ndus={TERABOX_NDUS}",
        },
        # Terabox needs these
        "extractor_args": {
            "generic": {"impersonate": "chrome"}
        },
    }

    def _run_ytdlp():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                filename = ydl.prepare_filename(info)
                # yt-dlp may change extension
                for ext in ("mp4", "mkv", "webm", "avi", "mov"):
                    alt = os.path.splitext(filename)[0] + "." + ext
                    if os.path.exists(alt):
                        return alt
                if os.path.exists(filename):
                    return filename
        return None

    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(None, _run_ytdlp)
        if path and os.path.exists(path):
            logger.info(f"yt-dlp download complete: {path}")
            return path
        logger.error("yt-dlp: no output file found")
    except Exception as e:
        logger.error(f"yt-dlp failed: {e}")
    return None


def sanitize_filename(name: str) -> str:
    """Remove characters unsafe for filenames."""
    name = re.sub(r'[^\w\s\-.]', '_', name)
    name = name.strip(". ")
    return name or "video.mp4"


# ══════════════════════════════════════════════════════════════════
# SECTION 3: AD SYSTEM (SHRINKFORGE)
# ══════════════════════════════════════════════════════════════════

def generate_verify_token(user_id: int) -> str:
    """Create a unique token for ad verification and store it."""
    token = uuid.uuid4().hex[:16].upper()
    pending_tokens[token] = {
        "user_id": user_id,
        "expires": time.time() + 3600,  # token valid for 1 hour
    }
    return token


def verify_token(token: str, user_id: int) -> bool:
    """
    Verify that the token is valid, unexpired, and belongs to this user.
    If valid, unlock the user and remove the token.
    """
    if token not in pending_tokens:
        return False
    entry = pending_tokens[token]
    if entry["user_id"] != user_id:
        return False
    if time.time() > entry["expires"]:
        del pending_tokens[token]
        return False
    del pending_tokens[token]
    unlock_user(user_id)
    return True


async def create_shrinkforge_link(long_url: str) -> Optional[str]:
    """
    Use ShrinkForge API to shorten a URL.
    Returns the short URL string, or None on failure.
    """
    api_endpoint = f"https://shrinkforearn.in/api?api={SHRINKFORGE_API}&url={long_url}&format=text"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(api_endpoint)
            text = resp.text.strip()
            if text.startswith("http"):
                return text
            logger.error(f"ShrinkForge unexpected response: {text[:100]}")
    except Exception as e:
        logger.error(f"ShrinkForge API error: {e}")
    return None


# ══════════════════════════════════════════════════════════════════
# SECTION 4: LOG CHANNEL
# ══════════════════════════════════════════════════════════════════

async def log_to_channel(context: ContextTypes.DEFAULT_TYPE, user, message_text: str, bot_reply: str):
    """Forward user message and bot reply to the log channel."""
    try:
        user_info = f"👤 {user.full_name} (@{user.username or 'no_username'}) [ID: {user.id}]"
        log_text  = (
            f"{user_info}\n"
            f"📩 User: {message_text[:300]}\n"
            f"🤖 Bot: {bot_reply[:300]}"
        )
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_text)
    except Exception as e:
        logger.warning(f"Log channel error: {e}")


# ══════════════════════════════════════════════════════════════════
# SECTION 5: TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start handler.

    Handles two cases:
      1. Normal /start → welcome message
      2. /start verify_TOKEN_USERID → ad verification deep link
    """
    user    = update.effective_user
    user_id = user.id
    log_user(user_id)

    args = context.args  # list of words after /start

    # ── Deep link verification ───────────────────────────────────────
    if args and args[0].startswith("verify_"):
        parts = args[0].split("_")
        # format: verify_TOKEN_USERID
        if len(parts) >= 3:
            token         = parts[1]
            link_user_id  = int(parts[2]) if parts[2].isdigit() else -1

            if link_user_id != user_id:
                await update.message.reply_text(
                    "⚠️ This verification link is not for your account."
                )
                return

            if verify_token(token, user_id):
                msg = (
                    "✅ *Ad verified!* You now have **12 hours of unlimited downloads**.\n\n"
                    "Send me any Terabox link to get started! 🚀"
                )
                await update.message.reply_text(msg, parse_mode="Markdown")
                await log_to_channel(context, user, f"/start verify (token={token})", "Unlock granted ✅")
            else:
                await update.message.reply_text(
                    "❌ Token invalid or expired. Please generate a new ad link."
                )
        return

    # ── Normal /start ────────────────────────────────────────────────
    status = get_user_status(user_id)
    welcome = (
        f"👋 Welcome, *{user.first_name}*!\n\n"
        f"📦 I download videos from **Terabox** for you.\n\n"
        f"*How to use:*\n"
        f"Just send me any Terabox link and I'll download it!\n\n"
        f"*Free Plan:* {FREE_LIMIT} downloads per 12 hours.\n"
        f"*Watch an ad* to unlock unlimited downloads for 12 hours! 🎬\n\n"
        f"📊 *Your status:* {status['remaining']} free downloads remaining."
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")
    await log_to_channel(context, user, "/start", "Welcome message sent")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only /stats command."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized.")
        return

    total  = get_total_users()
    tokens = len(pending_tokens)
    msg    = (
        f"📊 *Bot Statistics*\n\n"
        f"👥 Total unique users: `{total}`\n"
        f"🔑 Pending ad tokens: `{tokens}`\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main message handler.
    Receives a Terabox URL → checks quota → downloads → sends video.
    """
    user    = update.effective_user
    user_id = user.id
    text    = update.message.text.strip() if update.message.text else ""

    log_user(user_id)

    # ── Validate URL ─────────────────────────────────────────────────
    if not is_terabox_url(text):
        await update.message.reply_text(
            "🔗 Please send a valid Terabox link.\n"
            "Supported: `terabox.com`, `1024terabox.com`, `terabox.app`",
            parse_mode="Markdown",
        )
        return

    # ── Check download quota ─────────────────────────────────────────
    status = get_user_status(user_id)

    if not status["can_download"]:
        # Generate ad unlock link
        token        = generate_verify_token(user_id)
        deep_link    = f"https://t.me/{BOT_USERNAME}?start=verify_{token}_{user_id}"
        ad_link      = await create_shrinkforge_link(deep_link)

        if not ad_link:
            ad_link = deep_link  # fallback to raw deep link if ShrinkForge fails

        hours = status["reset_in"] // 3600
        mins  = (status["reset_in"] % 3600) // 60

        limit_msg = (
            f"⚠️ *Download limit reached!*\n\n"
            f"You've used all {FREE_LIMIT} free downloads for this 12-hour window.\n\n"
            f"⏳ *Free downloads reset in:* {hours}h {mins}m\n\n"
            f"*OR* watch a short ad to unlock **12 hours of unlimited downloads**!\n\n"
            f"👇 Click the button below:"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Watch Ad & Unlock Downloads", url=ad_link)],
        ])
        await update.message.reply_text(limit_msg, parse_mode="Markdown", reply_markup=keyboard)
        await log_to_channel(context, user, text, "Limit reached — ad link sent")
        return

    # ── Proceed with download ─────────────────────────────────────────
    processing_msg = await update.message.reply_text(
        "⬇️ *Downloading your video...*\n\nThis may take a moment ⏳",
        parse_mode="Markdown",
    )

    file_path = None
    try:
        file_path = await download_terabox_video(text)

        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("Download produced no output file")

        size_mb = os.path.getsize(file_path) / 1024 / 1024

        # Telegram bot API limit is 50 MB for sendVideo
        if size_mb > 49:
            await processing_msg.edit_text(
                f"⚠️ File is too large ({size_mb:.1f} MB) for Telegram's 50 MB limit.\n"
                f"Try a shorter video or contact the admin."
            )
            await log_to_channel(context, user, text, f"File too large: {size_mb:.1f} MB")
            return

        # Increment before sending (deduct even on partial failure)
        if not status["is_unlocked"]:
            increment_download_count(user_id)

        await processing_msg.edit_text("📤 *Uploading to Telegram...*", parse_mode="Markdown")

        caption = (
            f"🎬 *{os.path.basename(file_path)}*\n"
            f"📦 Size: {size_mb:.1f} MB\n\n"
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

        new_status = get_user_status(user_id)
        if not new_status["is_unlocked"]:
            remaining_text = f"📊 Downloads remaining: {new_status['remaining']}/{FREE_LIMIT}"
        else:
            h = int((new_status["unlock_until"] - time.time()) / 3600)
            remaining_text = f"🔓 Unlimited — {h}h left"

        await update.message.reply_text(remaining_text)
        await log_to_channel(context, user, text, f"Video sent ✅ ({size_mb:.1f} MB)")

    except Exception as e:
        logger.error(f"Download/send error for {user_id}: {e}", exc_info=True)
        await processing_msg.edit_text(
            "❌ *Download failed.*\n\n"
            "Possible reasons:\n"
            "• The link is private or expired\n"
            "• Terabox blocked the request\n"
            "• File is not a video\n\n"
            "Please try again or try a different link.",
            parse_mode="Markdown",
        )
        await log_to_channel(context, user, text, f"Error: {str(e)[:200]}")

    finally:
        # Clean up temp file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler — log but don't crash."""
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)


# ══════════════════════════════════════════════════════════════════
# SECTION 6: KEEP-ALIVE WEB SERVER (for Render free tier)
# ══════════════════════════════════════════════════════════════════

class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Terabox bot is alive!")

    def log_message(self, format, *args):
        pass  # suppress HTTP access logs


def run_keep_alive_server():
    """Run the HTTP server in a background thread."""
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    logger.info(f"Keep-alive server running on port {port}")
    server.serve_forever()


def start_keep_alive():
    """Launch the keep-alive server in a daemon thread."""
    thread = threading.Thread(target=run_keep_alive_server, daemon=True)
    thread.start()


# ══════════════════════════════════════════════════════════════════
# SECTION 7: MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def main():
    logger.info("═" * 60)
    logger.info("  Terabox Bot starting up...")
    logger.info(f"  Bot username: @{BOT_USERNAME}")
    logger.info(f"  Admin ID: {ADMIN_ID}")
    logger.info(f"  Log channel: {LOG_CHANNEL_ID}")
    logger.info("═" * 60)

    # Start keep-alive HTTP server (prevents Render from idling)
    start_keep_alive()

    # Build the Telegram application
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    app.add_error_handler(error_handler)

    # Start polling
    # drop_pending_updates=True prevents processing stale messages on restart
    # This also avoids Conflict errors when redeploying
    logger.info("Starting polling...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
