import asyncio
import logging
import os
import re
import random
import sqlite3
import threading
import time
import requests
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from playwright.async_api import async_playwright

# --- 1. CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable is not set!")

TARGET_URL = "https://unlockffbeta.com"
DB_NAME = "/data/users.db" if os.path.exists("/data") else "users.db"
DEFAULT_TIMER_SECONDS = 24 * 60 * 60

# =====================================================================
# 🌐 FREE PROXY FETCHER
# =====================================================================
PROXY_SOURCES = [
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt",
]

VALID_PROXIES = []

def fetch_and_validate_proxies():
    global VALID_PROXIES
    VALID_PROXIES = []
    raw_proxies = []
    for url in PROXY_SOURCES:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                for line in r.text.strip().split("\n"):
                    if line and not line.startswith("#"):
                        raw_proxies.append(line.strip())
        except Exception as e:
            logging.warning(f"Failed to fetch proxy source: {e}")

    logging.info(f"Downloaded {len(raw_proxies)} raw proxies. Validating...")
    for proxy in raw_proxies[:50]:
        try:
            resp = requests.get(
                "https://httpbin.org/ip",
                proxies={"http": f"http://{proxy}", "https": f"http://{proxy}"},
                timeout=8
            )
            if resp.status_code == 200:
                VALID_PROXIES.append(proxy)
        except:
            pass
    logging.info(f"Validated {len(VALID_PROXIES)} working proxies.")

def get_random_proxy():
    return random.choice(VALID_PROXIES) if VALID_PROXIES else None

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. DATABASE FUNCTIONS ---
def init_db():
    if "/" in DB_NAME:
        os.makedirs(os.path.dirname(DB_NAME), exist_ok=True)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, account_id TEXT, next_run TIMESTAMP)''')
    conn.commit()
    conn.close()

def save_user(user_id, account_id, next_run_time):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''INSERT INTO users (user_id, account_id, next_run)
        VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
        account_id=excluded.account_id, next_run=excluded.next_run''',
        (user_id, account_id, next_run_time))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT account_id, next_run FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def get_expired_users():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute('SELECT user_id, account_id FROM users WHERE next_run <= ?', (now_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- 3. PROGRESS BAR HELPER ---
def make_progress_bar(percent, length=20):
    filled = int(length * percent // 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent}%"

async def update_progress(message, percent, step_text):
    bar = make_progress_bar(percent)
    text = f"⏳ *Processing Account*\n\n`{bar}`\n\n🔹 {step_text}"
    try:
        await message.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Failed to edit progress: {e}")

# --- 4. STEALTH PATCHES ---
async def apply_stealth(page):
    """Hides common automation fingerprints from anti-bot detection."""
    try:
        # Hide navigator.webdriver
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        # Add fake plugins
        await page.add_init_script("""
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
        """)

        # Add fake languages
        await page.add_init_script("""
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
        """)

        # Override chrome runtime
        await page.add_init_script("""
            window.chrome = {
                runtime: {}
            };
        """)

        # Override permissions
        await page.add_init_script("""
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)

        # Hide headless user agent features
        await page.add_init_script("""
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 8
            });
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8
            });
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Win32'
            });
        """)

        logger.info("Stealth patches applied.")
    except Exception as e:
        logger.warning(f"Stealth patch error: {e}")

# --- 5. DEBUG HELPERS ---
async def dump_page_state(page, tag):
    logger.info(f"--- PAGE STATE [{tag}] ---")
    try:
        logger.info(f"URL: {page.url}")
    except: pass
    for i in range(1, 5):
        try:
            cls = await page.get_attribute(f"#stepCircle{i}", "class")
            logger.info(f"stepCircle{i}: {cls}")
        except: pass
    try:
        hdr = await page.locator("#hdr").inner_text()
        logger.info(f"hdr: '{hdr}'")
    except: pass
    try:
        label = await page.locator("#waitTimerLabel").inner_text()
        logger.info(f"waitTimerLabel: '{label}'")
    except: pass
    try:
        result_visible = await page.is_visible("#resultCard")
        result_text = ""
        if result_visible:
            result_text = await page.locator("#resultCard").inner_text()
        logger.info(f"resultCard visible: {result_visible}, text: '{result_text[:100]}'")
    except: pass
    try:
        gate_visible = await page.is_visible("#adGate:not(.hidden)")
        logger.info(f"adGate visible: {gate_visible}")
    except: pass
    try:
        btn_next_visible = await page.is_visible("#btnNext")
        btn_next_disabled = await page.get_attribute("#btnNext", "disabled")
        logger.info(f"btnNext visible={btn_next_visible} disabled={btn_next_disabled}")
    except: pass
    try:
        btn_guest_visible = await page.is_visible("#btnGuest")
        logger.info(f"btnGuest visible: {btn_guest_visible}")
    except: pass
    try:
        body = await page.locator("body").inner_text()
        logger.info(f"Body preview: '{body[:200]}'")
    except: pass
    logger.info(f"--- END PAGE STATE [{tag}] ---")

# --- 6. AD HANDLING & BROWSER AUTOMATION ---
async def close_new_page(page):
    try:
        await page.wait_for_timeout(1500)
        await page.close()
    except: pass

async def handle_ad_gate(page, step_num):
    try:
        await page.wait_for_selector("#adGate:not(.hidden)", timeout=6000)
        logger.info(f"[Step {step_num}] Ad gate detected.")
    except:
        logger.info(f"[Step {step_num}] No ad gate appeared.")
        return False
    try:
        if await page.is_visible("#adGateOpen"):
            btn_text = await page.locator("#adGateOpen").inner_text()
            logger.info(f"[Step {step_num}] Clicking adGateOpen (text: '{btn_text}')")
            await page.click("#adGateOpen")
            await page.wait_for_timeout(3000)
    except Exception as e:
        logger.warning(f"[Step {step_num}] adGateOpen click failed: {e}")
    try:
        await page.wait_for_selector("#adGateContinue:not([disabled])", timeout=45000)
        logger.info(f"[Step {step_num}] adGateContinue enabled. Clicking...")
        await page.click("#adGateContinue")
        await page.wait_for_timeout(2000)
    except Exception as e:
        logger.warning(f"[Step {step_num}] adGateContinue failed: {e}")
    return True

def parse_timer_from_text(text):
    if not text:
        return None
    total = 0
    h = re.search(r'(\d+)\s*h', text, re.IGNORECASE)
    m = re.search(r'(\d+)\s*m', text, re.IGNORECASE)
    s = re.search(r'(\d+)\s*s', text, re.IGNORECASE)
    if h: total += int(h.group(1)) * 3600
    if m: total += int(m.group(1)) * 60
    if s: total += int(s.group(1))
    if total > 0:
        return total
    tm = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', text)
    if tm:
        hh, mm, ss = map(int, tm.groups())
        return hh * 3600 + mm * 60 + ss
    return None

async def run_unlock_process(user_id, account_id, context, progress_message=None):
    logger.info(f"========== START UNLOCK for {account_id} ==========")

    async with async_playwright() as p:
        proxy_url = get_random_proxy()
        launch_options = {
            "headless": False,  # We control headless via --headless=new
            "args": [
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--no-first-run",
                "--no-zygote",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-breakpad",
                "--disable-component-extensions-with-background-pages",
                "--disable-default-apps",
                "--disable-features=TranslateUI,BlinkGenPropertyTrees",
                "--disable-ipc-flooding-protection",
                "--disable-renderer-backgrounding",
                "--memory-pressure-off",
                "--max_old_space_size=256",
                "--window-size=360,640",
                "--lang=en-US,en",
            ]
        }
        if proxy_url:
            launch_options["proxy"] = {"server": f"http://{proxy_url}"}
            logger.info(f"Using proxy: {proxy_url}")
        else:
            logger.info("No proxy — using direct connection.")

        try:
            browser = await p.chromium.launch(channel="chromium", **launch_options)
            logger.info("Browser launched with channel=chromium and --headless=new.")
        except Exception as e:
            logger.error(f"Chromium channel failed: {e}. Falling back to default.")
            try:
                browser = await p.chromium.launch(**launch_options)
                logger.info("Browser launched with default channel.")
            except Exception as e2:
                logger.error(f"Browser launch completely failed: {e2}")
                if progress_message:
                    await update_progress(progress_message, 100, "❌ Browser failed to launch")
                return False, 0

        context_browser = await browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            viewport={'width': 360, 'height': 640},
            locale="en-US",
            timezone_id="Africa/Lagos",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        page = await context_browser.new_page()

        # Apply stealth patches BEFORE any navigation
        await apply_stealth(page)

        context_browser.on("page", lambda np: asyncio.create_task(close_new_page(np)))

        try:
            # --- Load ---
            if progress_message:
                await update_progress(progress_message, 10, "🌐 Opening website...")
            logger.info("Navigating to site...")
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(4000)
            logger.info("Site loaded.")
            await dump_page_state(page, "after_load")

            # --- Check if page is actually rendered ---
            try:
                step1_class = await page.get_attribute("#stepCircle1", "class")
                if "active" not in (step1_class or ""):
                    logger.warning("⚠️ Step 1 is NOT active. Page may not have rendered properly.")
                    logger.warning("This usually means the site detected the bot or blocked the IP.")
            except: pass

            # --- Enter ID ---
            if progress_message:
                await update_progress(progress_message, 20, "🖱️ Looking for Continue button...")
            logger.info("Waiting for #btnGuest (up to 45s)...")
            try:
                await page.wait_for_selector("#btnGuest", timeout=45000, state="visible")
                logger.info("Found #btnGuest. Clicking...")
                await page.click("#btnGuest")
                logger.info("Clicked #btnGuest.")
            except Exception as e:
                logger.error(f"#btnGuest not found: {e}")
                await dump_page_state(page, "btnGuest_missing")
                try:
                    await page.screenshot(path="/tmp/btnGuest_missing.png", full_page=True)
                    logger.info("Saved screenshot to /tmp/btnGuest_missing.png")
                except: pass
                raise Exception(f"#btnGuest not found after 45s")

            await page.wait_for_timeout(2500)

            if progress_message:
                await update_progress(progress_message, 30, f"⌨️ Entering ID: {account_id}")
            logger.info("Waiting for #acctInput...")
            await page.wait_for_selector("#acctInput", timeout=20000)
            await page.fill("#acctInput", account_id)
            logger.info(f"Filled #acctInput with {account_id}.")

            if progress_message:
                await update_progress(progress_message, 40, "📤 Submitting ID...")
            logger.info("Waiting for #btnStart to enable...")
            await page.wait_for_selector("#btnStart:not([disabled])", timeout=20000)
            await page.click("#btnStart")
            logger.info("Clicked #btnStart.")
            await page.wait_for_timeout(3000)
            await dump_page_state(page, "after_btnStart")

            # --- Multi-step loop ---
            max_steps = 6
            for step in range(1, max_steps + 1):
                logger.info(f">>>>>>>> STEP {step} <<<<<<<<")
                percent = min(40 + (step * 10), 90)
                if progress_message:
                    await update_progress(progress_message, percent, f"🎬 Ad step {step}/{max_steps}...")

                try:
                    if await page.is_visible("#resultCard"):
                        content = await page.locator("#resultCard").inner_text()
                        if content.strip():
                            logger.info(f"[Step {step}] ✅ Result card populated! Unlock complete.")
                            break
                except: pass

                try:
                    unlocked = await page.evaluate(
                        "() => { const el = document.querySelector('#waitTimerLabel'); "
                        "return el && el.innerText.includes('Unlocked'); }"
                    )
                    logger.info(f"[Step {step}] Unlock label shows: {unlocked}")
                except Exception as e:
                    logger.warning(f"Check failed: {e}")

                await handle_ad_gate(page, step)

                try:
                    await page.wait_for_selector("#btnNext:not([disabled])", timeout=15000)
                    await page.click("#btnNext")
                    logger.info(f"[Step {step}] Clicked #btnNext.")
                    await page.wait_for_timeout(4000)
                except Exception as e:
                    logger.warning(f"[Step {step}] Could not click #btnNext: {e}")
                    break

            # --- Wait for final result ---
            if progress_message:
                await update_progress(progress_message, 95, "⏳ Waiting for final result...")
            logger.info("Waiting for #resultCard (up to 60s)...")
            try:
                await page.wait_for_selector("#resultCard:not(:empty)", timeout=60000)
                logger.info("✅ Result card found.")
            except Exception as e:
                logger.error(f"Final result wait failed: {e}")
                await dump_page_state(page, "result_timeout")
                raise

            # --- Read the timer ---
            detected_timer = None
            try:
                await page.wait_for_timeout(2000)
                if await page.is_visible("#expireCountdown"):
                    timer_text = await page.locator("#expireCountdown").inner_text()
                    detected_timer = parse_timer_from_text(timer_text)
                    logger.info(f"Detected timer: '{timer_text}' -> {detected_timer}s")
            except Exception as e:
                logger.warning(f"Could not read timer: {e}")

            await browser.close()
            final_timer = detected_timer if detected_timer else DEFAULT_TIMER_SECONDS

            if progress_message:
                await update_progress(progress_message, 100, "✅ Unlock complete!")
            logger.info(f"========== END UNLOCK (success) for {account_id} ==========")
            return True, final_timer

        except Exception as e:
            logger.error(f"❌ Automation error: {e}")
            await dump_page_state(page, "error_state")
            try:
                await page.screenshot(path="/tmp/error_screenshot.png", full_page=True)
            except: pass
            await browser.close()
            if progress_message:
                await update_progress(progress_message, 100, f"❌ Error: {str(e)[:50]}")
            logger.info(f"========== END UNLOCK (failed) for {account_id} ==========")
            return False, 0

# --- 7. BACKGROUND TIMER LOOP ---
async def timer_loop(context):
    while True:
        try:
            expired_users = get_expired_users()
            for user_id, account_id in expired_users:
                logger.info(f"Timer expired for {user_id}. Re-unlocking...")
                progress_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text="🔄 *Auto-unlock triggered...*",
                    parse_mode="Markdown"
                )
                success, timer_seconds = await run_unlock_process(
                    user_id, account_id, context, progress_msg
                )
                if success:
                    next_run = datetime.now() + timedelta(seconds=timer_seconds)
                    save_user(user_id, account_id, next_run.isoformat())
                    minutes = timer_seconds // 60
                    seconds = timer_seconds % 60
                    time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"✅ *Auto-unlock successful!*\n\n"
                             f"📌 ID: `{account_id}`\n"
                             f"⏱️ Website granted: {time_str}\n"
                             f"🔄 Next auto-unlock scheduled.",
                        parse_mode="Markdown"
                    )
                else:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"❌ Auto-unlock failed. Retrying in 5 minutes."
                    )
                    next_run = datetime.now() + timedelta(minutes=5)
                    save_user(user_id, account_id, next_run.isoformat())
        except Exception as e:
            logger.error(f"Error in timer loop: {e}")
        await asyncio.sleep(60)

# --- 8. TELEGRAM COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if user_data:
        account_id, next_run_str = user_data
        next_run = datetime.fromisoformat(next_run_str)
        time_left = next_run - datetime.now()
        if time_left.total_seconds() > 0:
            minutes = int(time_left.total_seconds() // 60)
            seconds = int(time_left.total_seconds() % 60)
            await update.message.reply_text(
                f"👋 *Welcome back!*\n\n"
                f"📌 Saved ID: `{account_id}`\n"
                f"⏱️ Next auto-unlock in: {minutes}m {seconds}s\n\n"
                f"To change your ID, simply send a new one.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"👋 ID `{account_id}` is due. Please wait.",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            "👋 Welcome! Please send me the Account ID you want to verify."
        )

async def process_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_id_input = update.message.text.strip()
    logger.info(f"Received ID from user {user_id}: {user_id_input}")

    if not user_id_input.isdigit() or len(user_id_input) > 11:
        await update.message.reply_text("❌ Please send a valid numeric ID (max 11 digits).")
        return

    progress_msg = await update.message.reply_text(
        f"⏳ *Processing Account: {user_id_input}*\n\n`{make_progress_bar(0)}`\n\n🔹 Starting...",
        parse_mode="Markdown"
    )

    success, timer_seconds = await run_unlock_process(
        user_id, user_id_input, context, progress_msg
    )

    if success:
        next_run = datetime.now() + timedelta(seconds=timer_seconds)
        save_user(user_id, user_id_input, next_run.isoformat())
        minutes = timer_seconds // 60
        seconds = timer_seconds % 60
        time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
        await progress_msg.edit_text(
            f"✅ *Account Unlocked!*\n\n"
            f"`{make_progress_bar(100)}`\n\n"
            f"📌 ID: `{user_id_input}`\n"
            f"⏱️ Website granted: {time_str}\n"
            f"🔄 Auto re-unlock when expires.\n\n"
            f"Send /start to check status.",
            parse_mode="Markdown"
        )
    else:
        await progress_msg.edit_text(
            f"❌ *Unlock Failed*\n\n"
            f"`{make_progress_bar(100)}`\n\n"
            f"Please try again later.",
            parse_mode="Markdown"
        )

async def post_init(application):
    init_db()
    # Proxies disabled — none of the free ones work
    asyncio.create_task(timer_loop(application))
    logger.info("Bot initialized with stealth mode.")

# --- 9. KEEP-ALIVE ---
def keep_alive():
    space_host = os.getenv("SPACE_HOST")
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    url = None
    if space_host:
        url = f"https://{space_host}"
    elif render_url:
        url = render_url
    if not url:
        logger.info("⚠️ Not on Hugging Face or Render. Keep-alive disabled.")
        return
    logger.info(f"✅ Keep-alive started for: {url}")
    while True:
        try:
            time.sleep(600)
            r = requests.get(url, timeout=15)
            logger.info(f"Keep-alive ping sent (status: {r.status_code})")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")

# --- 10. HEALTH CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"✅ Health check server running on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.warning(f"Health server failed to start: {e}")

# --- 11. MAIN ENTRY POINT ---
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_id))

    print("🤖 Bot running with Stealth Mode + Multi-Step Ad Loop + Progress Bar...")
    app.run_polling()

if __name__ == "__main__":
    main()