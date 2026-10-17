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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from playwright.async_api import async_playwright

# Try to import playwright-stealth
try:
    from playwright_stealth import stealth_async
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

# --- 1. CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable is not set!")

TARGET_URL = "https://unlockffbeta.com"
DB_NAME = "/data/users.db" if os.path.exists("/data") else "users.db"
DEFAULT_TIMER_SECONDS = 24 * 60 * 60

# ⚠️ SET YOUR CHANNELS HERE ⚠️
CHANNELS = [
    {"name": "NCK Dev", "url": "https://t.me/NCK_Dev", "id": "@NCK_Dev"},
    {"name": "NCK Free", "url": "https://t.me/NCKDEVFreeSC", "id": "@NCKDEVFreeSC"},
    {"name": "Error 404", "url": "https://t.me/ERRORX404E", "id": "@ERRORX404E"}
]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 2. DATABASE FUNCTIONS ---
def init_db():
    if "/" in DB_NAME:
        os.makedirs(os.path.dirname(DB_NAME), exist_ok=True)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        account_id TEXT,
        next_run TIMESTAMP,
        UNIQUE(user_id, account_id) ON CONFLICT IGNORE)''')
    conn.commit()
    conn.close()

def add_account(user_id, account_id, next_run_time):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM accounts WHERE user_id = ? AND account_id = ?', (user_id, account_id))
    if cursor.fetchone():
        conn.close()
        return False
    cursor.execute('INSERT INTO accounts (user_id, account_id, next_run) VALUES (?, ?, ?)', 
                   (user_id, account_id, next_run_time))
    conn.commit()
    conn.close()
    return True

def get_user_accounts(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT id, account_id, next_run FROM accounts WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def remove_account(user_id, account_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM accounts WHERE user_id = ? AND account_id = ?', (user_id, account_id))
    conn.commit()
    conn.close()

def get_expired_users():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute('SELECT id, user_id, account_id FROM accounts WHERE next_run <= ?', (now_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_account_timer(acc_id, next_run_time):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE accounts SET next_run = ? WHERE id = ?', (next_run_time, acc_id))
    conn.commit()
    conn.close()

# --- 3. HELPERS ---
def make_progress_bar(percent, length=20):
    filled = int(length * percent // 100)
    return f"[{'█' * filled}{'░' * (length - filled)}] {percent}%"

async def update_progress(message, percent, step_text):
    try:
        await message.edit_text(f"⏳ *Processing...*\n\n`{make_progress_bar(percent)}`\n\n🔹 {step_text}", parse_mode="Markdown")
    except: pass

def parse_timer_from_text(text):
    if not text: return None
    total = 0
    h = re.search(r'(\d+)\s*h', text, re.IGNORECASE)
    m = re.search(r'(\d+)\s*m', text, re.IGNORECASE)
    s = re.search(r'(\d+)\s*s', text, re.IGNORECASE)
    if h: total += int(h.group(1)) * 3600
    if m: total += int(m.group(1)) * 60
    if s: total += int(s.group(1))
    if total > 0: return total
    tm = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', text)
    if tm:
        hh, mm, ss = map(int, tm.groups())
        return hh * 3600 + mm * 60 + ss
    return None

def format_time_left(seconds):
    """Converts seconds into a readable string like '1h 15m' or '45m 30s'."""
    if seconds <= 0:
        return "⏳ Unlocking now..."
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

# --- 4. FORCE JOIN CHECKER ---
async def check_joined(user_id, context):
    missing = []
    for ch in CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=ch["id"], user_id=user_id)
            if member.status in ['left', 'kicked']:
                missing.append(ch)
        except Exception:
            # If bot is not admin, we can't check. We assume they joined to prevent blocking.
            pass 
    return missing

# --- 5. BROWSER AUTOMATION ---
async def close_new_page(page):
    try:
        await page.wait_for_timeout(1500)
        await page.close()
    except: pass

async def handle_ad_gate(page, step_num):
    try:
        await page.wait_for_selector("#adGate:not(.hidden)", timeout=5000)
    except:
        return False
    try:
        if await page.is_visible("#adGateOpen"):
            await page.click("#adGateOpen")
            await page.wait_for_timeout(2500)
    except: pass
    try:
        await page.wait_for_selector("#adGateContinue:not([disabled])", timeout=40000)
        await page.click("#adGateContinue")
        await page.wait_for_timeout(1500)
    except: pass
    return True

async def run_unlock_process(user_id, account_id, context, progress_message=None):
    logger.info(f"========== START UNLOCK for {account_id} ==========")
    
    async with async_playwright() as p:
        launch_options = {
            "headless": False,
            "channel": "chromium",
            "args": [
                "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-extensions", "--single-process", "--disable-setuid-sandbox",
                "--no-zygote", "--window-size=360,640", "--disable-blink-features=AutomationControlled"
            ]
        }
        
        try:
            browser = await p.chromium.launch(**launch_options)
        except Exception as e:
            logger.error(f"Browser launch failed: {e}")
            return False, 0

        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            viewport={'width': 360, 'height': 640},
            locale="en-US",
            timezone_id="Africa/Lagos"
        )
        page = await ctx.new_page()
        ctx.on("page", lambda np: asyncio.create_task(close_new_page(np)))

        if STEALTH_AVAILABLE:
            await stealth_async(page)

        try:
            if progress_message: await update_progress(progress_message, 10, "🌐 Opening website...")
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(4000)

            if progress_message: await update_progress(progress_message, 20, "🖱️ Clicking Continue...")
            await page.wait_for_selector("#btnGuest", timeout=30000, state="visible")
            await page.click("#btnGuest")
            await page.wait_for_timeout(2000)

            if progress_message: await update_progress(progress_message, 30, f"⌨️ Entering ID: {account_id}")
            await page.wait_for_selector("#acctInput", timeout=20000)
            await page.fill("#acctInput", account_id)

            if progress_message: await update_progress(progress_message, 40, "📤 Submitting...")
            await page.wait_for_selector("#btnStart:not([disabled])", timeout=20000)
            await page.click("#btnStart")
            await page.wait_for_timeout(3000)

            # Multi-step loop
            for step in range(1, 7):
                if progress_message: await update_progress(progress_message, min(40 + (step * 10), 90), f"🎬 Ad step {step}...")
                
                if await page.is_visible("#resultCard"):
                    content = await page.locator("#resultCard").inner_text()
                    if content.strip(): break

                await handle_ad_gate(page, step)

                try:
                    await page.wait_for_selector("#btnNext:not([disabled])", timeout=10000)
                    await page.click("#btnNext")
                    await page.wait_for_timeout(3500)
                except: break

            if progress_message: await update_progress(progress_message, 95, "⏳ Waiting for final result...")
            await page.wait_for_selector("#resultCard:not(:empty)", timeout=60000)

            detected_timer = None
            if await page.is_visible("#expireCountdown"):
                timer_text = await page.locator("#expireCountdown").inner_text()
                detected_timer = parse_timer_from_text(timer_text)

            await browser.close()
            return True, detected_timer if detected_timer else DEFAULT_TIMER_SECONDS

        except Exception as e:
            logger.error(f"❌ Automation error: {e}")
            await browser.close()
            return False, 0

# --- 6. BACKGROUND TIMER LOOP ---
async def timer_loop(context):
    while True:
        try:
            expired = get_expired_users()
            for db_id, user_id, account_id in expired:
                logger.info(f"Timer expired for {account_id}. Re-unlocking...")
                
                try:
                    progress_msg = await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🔄 *Auto-unlocking:* `{account_id}`...",
                        parse_mode="Markdown"
                    )
                except: progress_msg = None

                success, timer_seconds = await run_unlock_process(user_id, account_id, context, progress_msg)
                
                if success:
                    next_run = datetime.now() + timedelta(seconds=timer_seconds)
                    update_account_timer(db_id, next_run.isoformat())
                    if progress_msg:
                        await progress_msg.edit_text(f"✅ *Auto-Unlock Success!*\n\n📌 ID: `{account_id}`\n⏱️ Granted: {timer_seconds // 60}m", parse_mode="Markdown")
                else:
                    next_run = datetime.now() + timedelta(minutes=5)
                    update_account_timer(db_id, next_run.isoformat())
                    if progress_msg:
                        await progress_msg.edit_text(f"❌ *Auto-Unlock Failed* for `{account_id}`. Retrying in 5m.", parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in timer loop: {e}")
        await asyncio.sleep(60)

# --- 7. TELEGRAM UI ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check Force Join
    missing = await check_joined(user_id, context)
    if missing:
        keyboard = [[InlineKeyboardButton(f"Join {ch['name']}", url=ch["url"])] for ch in missing]
        keyboard.append([InlineKeyboardButton("✅ I have joined", callback_data="check_join")])
        await update.message.reply_text(
            "⚠️ *You must join our channels to use this bot!*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    await show_menu(update, context)

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add UID", callback_data="menu_add"), InlineKeyboardButton("📋 My UIDs", callback_data="menu_list")],
        [InlineKeyboardButton("ℹ️ Bot Status", callback_data="menu_status")]
    ]
    text = "👋 *Welcome to the Auto-Unlock Bot!*\n\nSelect an option below:"
    
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "check_join":
        missing = await check_joined(user_id, context)
        if not missing:
            await query.message.edit_text("✅ Thank you for joining!")
            await show_menu(update, context)
        else:
            await query.message.reply_text("❌ You still haven't joined all channels.")

    elif data == "menu_add":
        await query.message.edit_text("✏️ *Send me the Account ID you want to add:*\n\n(Send it as a normal message)", parse_mode="Markdown")
        context.user_data['awaiting_id'] = True

    elif data == "menu_list":
        accounts = get_user_accounts(user_id)
        if not accounts:
            await query.message.edit_text("You have no saved UIDs. Click 'Add UID' to start.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add UID", callback_data="menu_add")], [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]]))
            return
        
        text = "📋 *Your Saved UIDs & Unlock Time:*\n\n"
        keyboard = []
        now = datetime.now()
        
        for db_id, acc_id, next_run_str in accounts:
            next_run = datetime.fromisoformat(next_run_str)
            time_left_seconds = (next_run - now).total_seconds()
            
            # Format the remaining time
            time_str = format_time_left(time_left_seconds)
            
            text += f"• `{acc_id}`\n   ⏱️ Time left: *{time_str}*\n\n"
            keyboard.append([InlineKeyboardButton(f"❌ Remove {acc_id}", callback_data=f"rem_{acc_id}")])
        
        keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="menu_list")])
        keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")])
        
        try:
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception:
            # Ignore if message content is exactly the same (prevents Telegram errors)
            pass

    elif data.startswith("rem_"):
        acc_id = data.split("_")[1]
        remove_account(user_id, acc_id)
        await query.message.edit_text(f"🗑️ Removed `{acc_id}`.\n\nReturning to menu...", parse_mode="Markdown")
        await asyncio.sleep(1)
        await show_menu(update, context)

    elif data == "menu_status":
        accounts = get_user_accounts(user_id)
        await query.message.edit_text(
            f"📊 *Bot Status*\n\n"
            f"🟢 Bot is Online\n"
            f"📁 Your Active UIDs: {len(accounts)}\n"
            f"⏱️ Check interval: 1 minute",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]]),
            parse_mode="Markdown"
        )

    elif data == "menu_back":
        await show_menu(update, context)

async def process_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_id'):
        return

    user_id = update.effective_user.id
    acc_id = update.message.text.strip()
    context.user_data['awaiting_id'] = False

    if not acc_id.isdigit() or len(acc_id) > 11:
        await update.message.reply_text("❌ Invalid ID. Please send a numeric ID (max 11 digits). Send /start to try again.")
        return

    progress_msg = await update.message.reply_text(f"⏳ *Adding ID:* `{acc_id}`...", parse_mode="Markdown")
    
    success, timer_seconds = await run_unlock_process(user_id, acc_id, context, progress_msg)

    if success:
        next_run = datetime.now() + timedelta(seconds=timer_seconds)
        added = add_account(user_id, acc_id, next_run.isoformat())
        
        if added:
            time_str = format_time_left(timer_seconds)
            await progress_msg.edit_text(f"✅ *ID Added & Unlocked!*\n\n📌 ID: `{acc_id}`\n⏱️ Granted: {time_str}\n\nUse the buttons below to manage.", parse_mode="Markdown")
            await show_menu(update, context)
        else:
            await progress_msg.edit_text(f"⚠️ ID `{acc_id}` is already in your list.")
            await show_menu(update, context)
    else:
        await progress_msg.edit_text("❌ Failed to unlock this ID. Please try later.")

# --- 8. SERVER & MAIN ---
def keep_alive():
    url = os.getenv("RENDER_EXTERNAL_URL") or (f"https://{os.getenv('SPACE_HOST')}" if os.getenv('SPACE_HOST') else None)
    if not url: return
    while True:
        try:
            time.sleep(600)
            requests.get(url, timeout=15)
        except: pass

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): pass

def main():
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), HealthCheckHandler).serve_forever(), daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_id_input))

    print("🤖 Bot running with Timers displayed...")
    app.run_polling()

if __name__ == "__main__":
    main()