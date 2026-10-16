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

# --- 4. DEBUG HELPERS ---
async def dump_page_state(page, tag):
    """Logs the current state of key elements on the page."""
    logger.info(f"--- PAGE STATE [{tag}] ---")
    try:
        logger.info(f"URL: {page.url}")
    except:
        pass

    # Check step circles
    for i in range(1, 5):
        try:
            cls = await page.get_attribute(f"#stepCircle{i}", "class")
            logger.info(f"stepCircle{i}: {cls}")
        except:
            pass

    # Check step header
    try:
        hdr = await page.locator("#hdr").inner_text()
        logger.info(f"hdr: '{hdr}'")
    except:
        pass

    # Check wait timer label
    try:
        label = await page.locator("#waitTimerLabel").inner_text()
        logger.info(f"waitTimerLabel: '{label}'")
    except:
        pass

    # Check if ad gate is visible
    try:
        gate_visible = await page.is_visible("#adGate:not(.hidden)")
        logger.info(f"adGate visible: {gate_visible}")
        if gate_visible:
            try:
                gate_title = await page.locator("#adGateTitle").inner_text()
                logger.info(f"adGateTitle: '{gate_title}'")
            except:
                pass
            try:
                gate_btn = await page.locator("#adGateOpen").inner_text()
                logger.info(f"adGateOpen text: '{gate_btn}'")
            except:
                pass
            try:
                gate_cont_disabled = await page.get_attribute("#adGateContinue", "disabled")
                logger.info(f"adGateContinue disabled: {gate_cont_disabled}")
            except:
                pass
    except:
        pass

    # Check btnNext
    try:
        btn_next_visible = await page.is_visible("#btnNext")
        btn_next_disabled = await page.get_attribute("#btnNext", "disabled")
        logger.info(f"btnNext visible={btn_next_visible} disabled={btn_next_disabled}")
    except:
        pass

    # Check btnStart
    try:
        btn_start_disabled = await page.get_attribute("#btnStart", "disabled")
        logger.info(f"btnStart disabled: {btn_start_disabled}")
    except:
        pass

    # Check resultCard
    try:
        if await page.is_visible("#resultCard"):
            logger.info("resultCard is visible")
    except:
        pass

    logger.info(f"--- END PAGE STATE [{tag}] ---")

# --- 5. AD HANDLING & BROWSER AUTOMATION ---
async def close_new_page(page):
    try:
        await page.wait_for_timeout(1500)
        await page.close()
    except:
        pass

async def handle_ad_gate(page, step_num):
    """Handles one ad gate cycle."""
    logger.info(f"[Step {step_num}] Checking for ad gate...")
    try:
        await page.wait_for_selector("#adGate:not(.hidden)", timeout=6000)
        logger.info(f"[Step {step_num}] ✅ Ad gate detected.")
    except:
        logger.info(f"[Step {step_num}] No ad gate appeared — skipping.")
        return False

    # Click "Open Ad"
    try:
        if await page.is_visible("#adGateOpen"):
            btn_text = await page.locator("#adGateOpen").inner_text()
            logger.info(f"[Step {step_num}] Clicking '#adGateOpen' (text: '{btn_text}')")
            await page.click("#adGateOpen")
            await page.wait_for_timeout(3000)
            logger.info(f"[Step {step_num}] Clicked. Waiting for continue button...")
    except Exception as e:
        logger.warning(f"[Step {step_num}] adGateOpen click failed: {e}")

    # Wait for the Continue button to become enabled
    try:
        await page.wait_for_selector("#adGateContinue:not([disabled])", timeout=45000)
        logger.info(f"[Step {step_num}] adGateContinue enabled. Clicking...")
        await page.click("#adGateContinue")
        await page.wait_for_timeout(2000)
        logger.info(f"[Step {step_num}] ✅ Ad gate handled.")
    except Exception as e:
        logger.warning(f"[Step {step_num}] adGateContinue failed: {e}")

    return True

async def click_step_continue(page, step_num):
    """Clicks the main #btnNext button for the current step."""
    logger.info(f"[Step {step_num}] Looking for #btnNext...")
    try:
        await page.wait_for_selector("#btnNext:not([disabled])", timeout=10000)
        btn_text = await page.locator("#btnNext").inner_text()
        logger.info(f"[Step {step_num}] Clicking #btnNext (text: '{btn_text}')")
        await page.click("#btnNext")
        await page.wait_for_timeout(2500)
        logger.info(f"[Step {step_num}] ✅ Clicked #btnNext.")
        return True
    except Exception as e:
        logger.warning(f"[Step {step_num}] #btnNext click failed: {e}")
        return False

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
            "headless": True,
            "args": [
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-extensions", "--single-process", "--disable-setuid-sandbox",
                "--disable-accelerated-2d-canvas", "--no-first-run", "--no-zygote",
                "--disable-background-networking", "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows", "--disable-breakpad",
                "--disable-component-extensions-with-background-pages", "--disable-default-apps",
                "--disable-features=TranslateUI", "--disable-ipc-flooding-protection",
                "--disable-renderer-backgrounding", "--memory-pressure-off",
                "--max_old_space_size=256",
            ]
        }
        if proxy_url:
            launch_options["proxy"] = {"server": f"http://{proxy_url}"}
            logger.info(f"Using proxy: {proxy_url}")
        else:
            logger.info("No proxy — using direct connection.")

        try:
            browser = await p.chromium.launch(**launch_options)
            logger.info("Browser launched.")
        except Exception as e:
            logger.error(f"Browser launch failed: {e}")
            if progress_message:
                await update_progress(progress_message, 100, "❌ Browser failed to launch")
            return False, 0

        context_browser = await browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            viewport={'width': 360, 'height': 640}
        )
        page = await context_browser.new_page()
        context_browser.on("page", lambda np: asyncio.create_task(close_new_page(np)))

        try:
            # --- Load ---
            if progress_message:
                await update_progress(progress_message, 10, "🌐 Opening website...")
            logger.info("Navigating to site...")
            await page.goto(TARGET_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            logger.info("Site loaded.")
            await dump_page_state(page, "after_load")

            # --- Enter ID ---
            if progress_message:
                await update_progress(progress_message, 20, "🖱️ Clicking 'Continue without Discord'...")
            logger.info("Waiting for #btnGuest...")
            await page.wait_for_selector("#btnGuest", timeout=15000)
            await page.click("#btnGuest")
            logger.info("Clicked #btnGuest.")
            await page.wait_for_timeout(1500)
            await dump_page_state(page, "after_btnGuest")

            if progress_message:
                await update_progress(progress_message, 30, f"⌨️ Entering ID: {account_id}")
            logger.info("Waiting for #acctInput...")
            await page.wait_for_selector("#acctInput", timeout=15000)
            await page.fill("#acctInput", account_id)
            logger.info(f"Filled #acctInput with {account_id}.")

            if progress_message:
                await update_progress(progress_message, 40, "📤 Submitting ID...")
            logger.info("Waiting for #btnStart to enable...")
            await page.wait_for_selector("#btnStart:not([disabled])", timeout=15000)
            await page.click("#btnStart")
            logger.info("Clicked #btnStart.")
            await page.wait_for_timeout(2500)
            await dump_page_state(page, "after_btnStart")

            # --- Multi-step loop ---
            max_steps = 6
            for step in range(1, max_steps + 1):
                logger.info(f">>>>>>>> LOOP ITERATION {step} <<<<<<<<")
                percent = min(40 + (step * 8), 90)
                if progress_message:
                    await update_progress(
                        progress_message, percent,
                        f"🎬 Ad step {step}/{max_steps}..."
                    )

                await dump_page_state(page, f"step_{step}_start")

                # Check if already unlocked
                try:
                    unlocked = await page.evaluate(
                        "() => { const el = document.querySelector('#waitTimerLabel'); "
                        "return el && el.innerText.includes('Unlocked'); }"
                    )
                    if unlocked:
                        logger.info(f"[Step {step}] ✅ Already unlocked! Breaking loop.")
                        break
                except Exception as e:
                    logger.warning(f"Unlock check failed: {e}")

                # Handle ad gate
                await handle_ad_gate(page, step)

                # Click step continue
                clicked = await click_step_continue(page, step)
                if not clicked:
                    logger.info(f"[Step {step}] Could not click #btnNext. Assuming done.")
                    break

                await page.wait_for_timeout(2500)

            # --- Wait for unlock ---
            await dump_page_state(page, "before_final_wait")
            if progress_message:
                await update_progress(progress_message, 95, "⏳ Waiting for unlock timer...")
            logger.info("Waiting for 'Unlocked' text (up to 60s)...")
            try:
                await page.wait_for_function(
                    "document.querySelector('#waitTimerLabel') && "
                    "document.querySelector('#waitTimerLabel').innerText.includes('Unlocked')",
                    timeout=60000
                )
                logger.info("✅ Unlock confirmed.")
            except Exception as e:
                logger.error(f"Final unlock wait failed: {e}")
                await dump_page_state(page, "unlock_timeout")
                raise

            # --- Read timer ---
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
                await page.screenshot(path="/tmp/error_screenshot.png")
                logger.info("Saved error screenshot to /tmp/error_screenshot.png")
            except:
                pass
            await browser.close()
            if progress_message:
                await update_progress(progress_message, 100, f"❌ Error: {str(e)[:50]}")
            logger.info(f"========== END UNLOCK (failed) for {account_id} ==========")
            return False, 0

# --- 6. BACKGROUND TIMER LOOP ---
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

# --- 7. TELEGRAM COMMANDS ---
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
    # Proxies disabled for now (they were all dead)
    # loop = asyncio.get_event_loop()
    # loop.run_in_executor(None, fetch_and_validate_proxies)
    asyncio.create_task(timer_loop(application))
    logger.info("Bot initialized.")

# --- 8. KEEP-ALIVE ---
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

# --- 9. HEALTH CHECK SERVER ---
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

# --- 10. MAIN ENTRY POINT ---
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_id))

    print("🤖 Bot running with DETAILED LOGGING + Multi-Step Ad Handling...")
    app.run_polling()

if __name__ == "__main__":
    main()